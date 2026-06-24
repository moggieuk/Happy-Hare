# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Class to handle sync-feedback and adjustment of gear stepper rotation distance
#       to keep MMU in sync with extruder as well as some filament tension routines.
#       This will always exist even in the absense of a mmu_buffer - flowguard is
#       available with just encoder.
#
# FlowGuard: It also implements protection for all modes/sensor types that will trigger
#            on clog (at extruder) or tangle (at MMU) conditions.
#
# Autotune: An autotuning option can be enabled for dynamic tuning (and persistence) of
#           calibrated MMU gear rotation_distance.
#
# Implements commands:
#   MMU_SYNC_FEEDBACK
#   MMU_FLOWGUARD
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, math, time, os

# Happy Hare imports
from ..mmu_constants      import *
from ..mmu_utils          import MmuError
from .mmu_sync_controller import SyncControllerConfig, SyncController

SF_STATE_NEUTRAL     = 0
SF_STATE_COMPRESSION = 1
SF_STATE_TENSION     = -1


class MmuSyncFeedback:

    def __init__(self, config, mmu_unit, params):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()

        self.estimated_state = float(SF_STATE_NEUTRAL)
        self.active = False           # Sync-feedback actively operating?
        self.flowguard_active = False # FlowGuard armed in sync-feedback controller or just using encoder
        self.ctrl = None
        self.flow_rate = 100.         # Estimated % flowrate (calc only for proportional sensors)

        # Coalescing state for deferred gear rd updates: keep only the latest
        # target with a single in-flight lookahead callback (all on reactor greenlet)
        self._pending_rd = None
        self._rd_cb_scheduled = False

        # Fractional dead-band so sub-0.3% EKF chatter doesn't schedule an update
        # every tick. Gated against the last committed rd, so sustained drift still
        # accumulates and crosses the band (controller envelope clamp bounds excursions)
        self._last_scheduled_rd = None
        self._rd_deadband_frac = 0.003

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler('klippy:disconnect', self.handle_disconnect)
        self.printer.register_event_handler('mmu:initialized', self.handle_mmu_initialized)

        # Initial flowguard status (when using sync-feedback controller)
        self.flowguard_status = {'trigger': '', 'reason': '', 'level': 0.0, 'max_clog': 0.0, 'max_tangle': 0.0, 'active': False, 'enabled': False}

        # Tangle prevention - gear current boost on high tension (spool resistance)
        self._tangle_prevention_boosted = False # Gear current currently boosted to 100%
        self._tangle_prevention_active  = False # Armed (only while filament monitoring is active, not during load/unload)


    def reinit(self):
        pass


    def handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller # Shared MMU controller class

        if self.mmu_unit.has_buffer():
            # Setup events for managing motor synchronization
            self.printer.register_event_handler("mmu:synced", self._handle_mmu_synced)
            self.printer.register_event_handler("mmu:unsynced", self._handle_mmu_unsynced)
            self.printer.register_event_handler("mmu:sync_feedback", self._handle_sync_feedback)
            self.printer.register_event_handler("mmu:printing", self._handle_printing)
            self.printer.register_event_handler("mmu:gate_selected", self._handle_gate_selected)


    def handle_disconnect(self):
        # Release any cached sync-controller jsonl log handle
        if self.ctrl is not None:
            try:
                self.ctrl.close_log()
            except Exception:
                pass


    def handle_mmu_initialized(self):
        if self.mmu_unit.has_buffer():
            self._init_controller()


    def _handle_printing(self, print_time=None):
        """
        On transition into the printing state: clear stale telemetry logs and, if a gate
        is already synced/active (so no sync transition will reset the controller), start
        a fresh telemetry log for it. This ensures each print produces clean telemetry.
        """
        if not self.mmu_unit.has_buffer(): return

        # Wipe any stale telemetry from previous prints
        self.wipe_telemetry_logs()

        # If already synced/active the controller won't reset (no sync transition occurs),
        # so begin a fresh log here for the active gate. Otherwise the upcoming sync will
        # create it via _reset_controller.
        if self.active and self.ctrl is not None and self.ctrl.cfg.log_sync:
            self.ctrl._current_log_file = self._telemetry_log_path()
            self.ctrl._init_log()


    def is_enabled(self):
        """
        This is whether the user has enabled the sync-feedback feature (the "big" switch)
        """
        return self.p.sync_feedback_enabled


    def is_active(self):
        """
        Returns whether the sync-feedback is currently active (when synced)
        """
        return self.active


    def get_sync_feedback_string(self, state=None, detail=False):
        if not self.mmu_unit.has_buffer():
            return "unavailable"

        if state is None:
            state = self._get_sensor_state()
        if (self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active) or detail:
            # Polarity varies slightly between modes on proportional sensor so ask controller
            polarity = self.ctrl.polarity(state)
            return 'compressed' if polarity > 0 else 'tension' if polarity < 0 else 'neutral'
        elif self.mmu.is_enabled and self.p.sync_feedback_enabled:
            return "inactive"
        return "disabled"


    def activate_flowguard(self, eventtime):
        u = self.mmu_unit
        msg = None

        if u.has_buffer() and self.p.flowguard_enabled and not self.flowguard_active:
            self.flowguard_active = True
            # This resets controller with last good autotuned RD, resets Flowguard then resumes Autotune
            self._reset_controller(eventtime, hard_reset=False)
            self.ctrl.autotune.resume()
            msg = "FlowGuard monitoring activated and Autotune resumed"

        # Enable encoder based Flowguard
        if u.has_encoder() and not u.encoder.is_flowguard_enabled():
            if not u.encoder.enable_flowguard(u):
                return # Must in in off mode
            self.flowguard_active = True
            msg = msg or "FlowGuard monitoring with encoder activated"

        if msg:
            self.mmu.log_info(msg)


    def deactivate_flowguard(self, eventtime):
        u = self.mmu_unit
        msg = None

        if u.has_buffer() and self.p.flowguard_enabled and self.flowguard_active:
            self.flowguard_active = False
            self.ctrl.autotune.pause()
            msg = "FlowGuard monitoring deactivated and Autotune paused"

        # Enable encoder based "flowguard"
        if u.has_encoder() and u.encoder.is_flowguard_enabled():
            if not u.encoder.disable_flowguard():
                return # Must in in off mode
            self.flowguard_active = False
            msg = msg or "FlowGuard monitoring with encoder deactivated"

        if msg:
            self.mmu.log_info(msg)


    # Tangle prevention: boost gear current on high tension (spool resistance) to pull filament
    # before a tangle develops. Parallels FlowGuard, armed/disarmed with filament monitoring so
    # it never interferes with load/unload moves

    def activate_tangle_prevention(self, eventtime):
        # Arm only with a proportional sensor present (filament monitoring enabled)
        if self.p.tangle_prevention_enabled and self.mmu.sensor_manager.has_sensor(SENSOR_PROPORTIONAL):
            self._tangle_prevention_active = True
            self.mmu.log_debug("Tangle Prevention: Armed")


    def deactivate_tangle_prevention(self, eventtime):
        # Disarm and restore boosted current (filament monitoring disabled)
        if self._tangle_prevention_active:
            self._tangle_prevention_active = False
            self.mmu.log_debug("Tangle Prevention: Disarmed")
        self._restore_tangle_prevention_current(eventtime, "disarmed")


    def _reset_tangle_prevention(self, eventtime):
        # Disarm and restore boosted current on unsync
        self._tangle_prevention_active = False
        self._restore_tangle_prevention_current(eventtime, "reset")


    def _restore_tangle_prevention_current(self, eventtime, reason):
        if self._tangle_prevention_boosted:
            self._tangle_prevention_boosted = False
            restore_percent = self.p.sync_gear_current
            self.mmu.log_debug("Tangle Prevention: Restoring gear current to %d%%" % restore_percent)
            self.mmu._adjust_gear_current(percent=restore_percent, reason="tangle prevention %s" % reason)


    def _check_tangle_prevention(self, eventtime):
        """
        Boost gear current to 100% when tension exceeds threshold (gear struggling to pull from
        spool), restore to sync_gear_current once it eases below release. Hysteresis (separate
        trigger/release) prevents thrashing. This is tangle prevention, not clog detection
        """
        if not (self.p.tangle_prevention_enabled and self._tangle_prevention_active): return

        # Tension is the negative half of the sensor range; work with abs value
        tension_level = -self._get_sensor_state()

        if not self._tangle_prevention_boosted:
            if tension_level >= self.p.tangle_prevention_threshold:
                self._tangle_prevention_boosted = True
                self.mmu.log_info("Tangle Prevention: High tension detected (%.0f%%), boosting gear current to 100%%" % (tension_level * 100.))
                # Defer current change to its own greenlet so SET_TMC_CURRENT's get_last_move_time()
                # doesn't flush the lookahead in this timer-driven path (risking a move stall)
                self.mmu.reactor.register_callback(
                    lambda pt: self.mmu._adjust_gear_current(percent=100, reason="for tangle prevention"))
        else:
            if tension_level <= self.p.tangle_prevention_release:
                self._tangle_prevention_boosted = False
                restore_percent = self.p.sync_gear_current
                self.mmu.log_info("Tangle Prevention: Tension eased (%.0f%%), restoring gear current to %d%%" % (tension_level * 100., restore_percent))
                self.mmu.reactor.register_callback(
                    lambda pt, p=restore_percent: self.mmu._adjust_gear_current(percent=p, reason="tangle prevention release"))


    def adjust_filament_tension(self, use_gear_motor=True, max_move=None):
        """
        Relax the filament tension, preferring proportional control if available else sync-feedback sensor switches.
        By default uses gear stepper to achive the result but optionally can use just extruder stepper for
        extruder entry check using compression sensor 'max_move' is advisory maximum travel distance
        Returns distance of the correction move and whether operation was successful (or None if not performed)
        """
        if not self.mmu_unit.has_buffer(): return 0.0, None

        has_tension, has_compression, has_proportional = self.get_active_sensors()
        max_move = max_move or self.mmu_unit.buffer.buffer_maxrange

        if has_proportional:
            return self._adjust_filament_tension_proportional() # Doesn't yet support extruder stepper or max_move parameter

        if has_tension or has_compression:
            return self._adjust_filament_tension_switch(use_gear_motor=use_gear_motor, max_move=max_move)

        # All sensors must be disabled...
        return 0.0, None


    def wipe_telemetry_logs(self):
        """
        Called to wipe any sync debug files on print start
        """
        if not self.mmu_unit.has_buffer(): return

        for gate in range(self.mmu.num_gates):
            log_path = self._telemetry_log_path(gate)

            # Can't wipe if already synced and active
            if gate != self.mmu.gate_selected or not self.active:
                if os.path.exists(log_path):
                    try:
                        os.remove(log_path)
                    except OSError as e:
                        self.mmu.log_debug("Unable to wipe sync feedback debug log: %s" % log_path)


    def get_active_sensors(self):
        """
        Returns tuple of active sync-feedback sensors
        """
        sm = self.mmu.sensor_manager
        has_tension      = sm.has_sensor(SENSOR_TENSION)
        has_compression  = sm.has_sensor(SENSOR_COMPRESSION)
        has_proportional = sm.has_sensor(SENSOR_PROPORTIONAL)
        return has_tension, has_compression, has_proportional


    def get_status(self, eventtime=None):

        # Buffer controlled sync feedback
        if self.mmu_unit.has_buffer() and self.ctrl:
            if self.mmu_unit.has_encoder():
                self.flowguard_status['encoder_mode'] = self.p.flowguard_encoder_mode # Ok to mutate status
            return {
                'sync_feedback_state': self.get_sync_feedback_string(),
                'sync_feedback_enabled': self.is_enabled(),
                'sync_feedback_bias_raw': round(self._get_sync_bias_raw(), 2),
                'sync_feedback_bias_modelled': round(self._get_sync_bias_modelled(), 2),
                'sync_feedback_flow_rate': self.flow_rate,
                'flowguard': self.flowguard_status,
                'tangle_prevention': {
                    'enabled': bool(self.p.tangle_prevention_enabled),
                    'active': self._tangle_prevention_active,
                    'boosted': self._tangle_prevention_boosted,
                    'threshold': self.p.tangle_prevention_threshold,
                    'release': self.p.tangle_prevention_release,
                },
            }

        # Encoder flowguard only
        if self.mmu_unit.has_encoder():
            return {
                'flowguard': {
                    'active': self.flowguard_active,
                    'enabled': self.p.flowguard_enabled,
                    'encoder_mode': self.p.flowguard_encoder_mode,
                }
            }

        return {}


    #
    # Internal implementation --------------------------------------------------
    #

    def _invalidate_rd_scheduler(self):
        """
        Drop scheduler memory after the stepper rd was snapped outside _schedule_rd_update
        (unsync, controller reset, gate change). Clearing _last_scheduled_rd makes the
        dead-band fire on the next tick to re-cohere model and stepper; clearing _pending_rd
        makes any in-flight lookahead callback early-exit. _rd_cb_scheduled is left to drain.
        """
        self._last_scheduled_rd = None
        self._pending_rd = None


    def _telemetry_log_path(self, gate=None):
        if gate is None: gate = self.mmu.gate_selected

        logfile_path = self.mmu.printer.start_args['log_file']
        dirname = os.path.dirname(logfile_path)

        if not dirname:
            dirname = "/tmp"

        return os.path.join(dirname, 'sync_%d.jsonl' % gate)


    def _handle_mmu_synced(self, eventtime=None):
        """
        Event indicating that gear stepper is now synced with extruder
        """
        # Ignore event if not for this unit
        if not self.mmu_unit.manages_gate(self.mmu.gate_selected): return

        if not self.mmu.is_enabled: return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        msg = "MmuSyncFeedback: Synced MMU to extruder%s" % (" (sync feedback activated)" if self.p.sync_feedback_enabled else "")
        if self.mmu_unit.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        if self.active: return

        # Enable sync feedback
        self.active = True
        self.new_autotuned_rd = None

        # Throw away current autotune info and reset rd
        self._reset_controller(eventtime)

        # Turn on extruder movement events
        self.mmu_unit.extruder_monitor().register_callback(self._handle_extruder_movement, self.p.sync_feedback_extrude_threshold)


    def _handle_mmu_unsynced(self, eventtime=None):
        """
        Event indicating that gear stepper has been unsynced from extruder
        """
        # Ignore event if not for this unit
        if not self.mmu_unit.manages_gate(self.mmu.gate_selected): return

        if not (self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active): return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        msg = "MmuSyncFeedback: Unsynced MMU from extruder%s" % (" (sync feedback deactivated)" if self.p.sync_feedback_enabled else "")
        if self.mmu_unit.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        if not self.active: return

        # Deactivate sync feedback
        self.active = False

        # Reset tangle prevention state and restore current if boosted
        self._reset_tangle_prevention(eventtime)

        if self.new_autotuned_rd is not None:
            self.mmu_unit.calibrator.note_rd_telemetry(self.mmu.gate_selected, self.new_autotuned_rd)

        # Restore default (last tuned) rotation distance in case it wasn't "autotune-saved" above.
        # This snaps rd outside the scheduler so invalidate its memory
        self.mmu_unit.calibrator.restore_gear_rd()
        self._invalidate_rd_scheduler()

        # Optional but let's turn off extruder movement events
        self.mmu_unit.extruder_monitor().remove_callback(self._handle_extruder_movement)


    def _handle_gate_selected(self, gate, prev_gate):
        """
        On gate swap the calibrator snaps the stepper to the new gate's calibrated rd
        outside the scheduler (incl. type-B swaps that keep sync active), so invalidate.
        Log handle is left to the controller's reset()/_init_log() to close+reopen.
        """
        if not self.mmu_unit.has_buffer(): return
        self._invalidate_rd_scheduler()


    def _handle_extruder_movement(self, eventtime, move):
        """
        Event call when extruder has moved more than threshold. This also allows for
        periodic rotation_distance adjustment, autotune and flowguard checking.

        Keeps the ExtruderMonitor timer body short by capturing the coherent
        (move, state) pair and deferring the heavy EKF/FlowGuard/rd work to _do_update().
        """
        if not (self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active): return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        self.mmu.log_trace("MmuSyncFeedback: Extruder movement event, move=%.1f" % move)

        # Read sensor state now so (move, state) stays coherent if the deferred update lags
        state = self._get_sensor_state()
        self.mmu.reactor.register_callback(
            lambda pt, et=eventtime, m=move, s=state: self._do_update(et, m, s))


    def _do_update(self, eventtime, move, state):
        """
        Deferred worker for _handle_extruder_movement. Re-checks gating since state
        may have changed between scheduling and execution (e.g. unsync or reset)
        """
        if not (self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active): return
        if self.ctrl is None: return
        status = self.ctrl.update(eventtime, move, state)
        self._process_status(eventtime, status)


    def _schedule_rd_update(self, rd):
        """
        Defer a gear stepper rd change to the printer toolhead's lookahead queue so it
        lands between moves rather than mutating step_dist on an actively-stepping stepper.
        Coalesces to the latest target with a single outstanding callback; fires
        synchronously when the lookahead queue is empty (responsive when not printing)
        """
        self._pending_rd = rd
        if self._rd_cb_scheduled:
            return
        self._rd_cb_scheduled = True
        try:
            self.mmu.toolhead.register_lookahead_callback(self._apply_pending_rd)
        except Exception:
            # Toolhead refused the callback; apply directly to keep model and stepper synced
            self._rd_cb_scheduled = False
            target = self._pending_rd
            self._pending_rd = None
            if target is not None:
                self.mmu_unit.calibrator.apply_gear_rd(target)


    def _apply_pending_rd(self, print_time):
        """
        Lookahead callback that mutates the stepper's rd, sequenced with extruder
        motion. Consumes the latest coalesced target.
        """
        target = self._pending_rd
        self._pending_rd = None
        self._rd_cb_scheduled = False
        if target is None:
            return
        # Skip if sync-feedback was turned off since scheduling (unsync restores rd itself)
        if not (self.mmu.is_enabled and self.active):
            return
        self.mmu_unit.calibrator.apply_gear_rd(target)


    def _handle_sync_feedback(self, eventtime, state):
        """
        Event call when sync-feedback discrete state changes.
        'state' should be -1 (tension), 0 (neutral), 1 (compressed)
        or can be a proportional float value between -1.0 and 1.0
        """
        # Ignore event if not for this unit
        if not self.mmu_unit.manages_gate(self.mmu.gate_selected): return

        if not (self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active): return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        msg = "MmuSyncFeedback: Sync state changed to %s" % (self.get_sync_feedback_string(state))
        if self.mmu_unit.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        move = self.mmu_unit.extruder_monitor().get_and_reset_accumulated(self._handle_extruder_movement)
        status = self.ctrl.update(eventtime, move, state)
        self._process_status(eventtime, status)


    def _process_status(self, eventtime, status):
        """
        Common logic to process the rotation distance recommendations of the sync controller
        """
        output = status['output']

        # Handle estimated sensor position
        self.estimated_state = output['sensor_ui']

        # Handle flowguard trip
        self.flowguard_status = dict(output['flowguard'])
        self.flowguard_status['enabled'] = bool(self.p.flowguard_enabled)
        f_trigger = self.flowguard_status.get('trigger', None)
        f_reason = self.flowguard_status.get('reason', "")
        if f_trigger:
            if self.p.flowguard_enabled and self.flowguard_active:
                self.mmu.log_error("FlowGuard detected a %s.\nReason for trip: %s" % (f_trigger, f_reason))

                # Pick most appropriate sensor to assign event to (primariliy for optics)
                has_tension, has_compression, has_proportional = self.get_active_sensors()

                if has_proportional:
                    sensor_key = SENSOR_PROPORTIONAL
                elif has_compression and not has_tension:
                    sensor_key = SENSOR_COMPRESSION
                elif has_tension and not has_compression:
                    sensor_key = SENSOR_TENSION
                elif f_trigger == "clog":
                    sensor_key = SENSOR_COMPRESSION
                else: # "tangle"
                    sensor_key = SENSOR_TENSION
                sm = self.mmu.sensor_manager
                sensor = sm.get_sensor_obj(sensor_key)

                if sensor is not None:
                    sensor.runout_helper.note_clog_tangle(f_trigger)
                self.deactivate_flowguard(eventtime)
            else:
                self.mmu.log_debug("FlowGuard detected a %s, but handling is disabled.\nReason for trip: %s" % (f_trigger, f_reason))
                self.ctrl.flowguard.reset() # Prevent repetitive messages

        # Handle new autotune suggestions
        autotune = output['autotune']
        rd = autotune.get('rd', None)
        note = autotune.get('note', None)
        save = autotune.get('save', None)
        if rd is not None:
            msg = "MmuSyncFeedback: Autotune suggested new operational reference rd: %.4f\n%s" % (rd, note)
            self.new_autotuned_rd = rd
            self.mmu.log_debug(msg)

        # Update gear stepper rd, subject to a fractional dead-band against the last
        # committed rd. Drops sub-0.3% EKF chatter; sustained drift still accumulates
        # and crosses the band, and the controller's envelope clamp bounds both sides.
        rd_current, rd_prev, rd_tuned = output['rd_current'], output['rd_prev'], output['rd_tuned']
        if rd_current != rd_prev:
            # Controller model moved; whether it reaches the stepper is gated below
            self.mmu.log_debug("MmuSyncFeedback: Recalculated rotation distance for gate %d from %.4f to %.4f" % (self.mmu.gate_selected, rd_prev, rd_current))
            last = self._last_scheduled_rd
            # First tick after activation/reset (last is None): always schedule
            if last is None or last == 0.0 or abs(rd_current - last) / abs(last) > self._rd_deadband_frac:
                # Dead-band fired: log accumulated delta since last commit (what crossed the band)
                if last is None or last == 0.0:
                    self.mmu.log_debug("MmuSyncFeedback: Altered rotation distance for gate %d to %.4f (initial)" % (self.mmu.gate_selected, rd_current))
                else:
                    delta = rd_current - last
                    frac_pct = abs(delta) / abs(last) * 100.0
                    self.mmu.log_debug("MmuSyncFeedback: Altered rotation distance for gate %d from %.4f to %.4f (delta %+.4f, %.2f%%)" % (self.mmu.gate_selected, last, rd_current, delta, frac_pct))
                self._last_scheduled_rd = rd_current
                self._schedule_rd_update(rd_current) # Defer to lookahead callback (lands between moves)

        # Proportional sensor (with autotune) allows for estimation of flow rate!
        if self.mmu.sensor_manager.has_sensor(SENSOR_PROPORTIONAL):
            # if rd_current > rd_true then flowrate must be reduced
            self.flow_rate = round(min(1.0, (rd_tuned / rd_current)) * 100., 2)

        # Tangle prevention: boost gear current on high tension (spool resistance)
        self._check_tangle_prevention(eventtime)


    def _reset_controller(self, eventtime, hard_reset=True):
        """
        hard_reset: Completely reset sync-feedback: throw away autotune info, reset rd to
                    last calibrated value. Typically called when handling sync but also can
                    be explicitly called but MMU_SYNC_FEEDBACK command
        soft_reset: Rebase sync-feedback to last autotuned value. Typically called when
                    resuming flowguard (after some activity we want to exclude from tuning)
        """
        # Allow dynamic changing of effective "sensor type" based on currently enabled sensors
        self.ctrl.cfg.sensor_type = self._get_sensor_type()

        # Reset controller with initial rd and sensor reading (will also reset flowguard and autotune on hard_reset)
        starting_state = self._get_sensor_state()
        self.estimated_state = starting_state
        if hard_reset:
            rd_start = self.mmu_unit.calibrator.get_gear_rd()
        else:
            rd_start = self.ctrl.autotune.get_rec_rd()
        # Fresh controller state: invalidate so a stale queued lookahead callback
        # can't apply an out-of-date target after activation
        self._invalidate_rd_scheduler()
        status = self.ctrl.reset(eventtime, rd_start, starting_state, log_file=self._telemetry_log_path(), hard_reset=hard_reset)
        self._process_status(eventtime, status) # May adjust rotation_distance


    def _init_controller(self):
        """
        The controller logic is in a completely standalone module for simulation
        and debugging purposes so instantiate it here with current config
        Returns: the SyncController object
        """
        rd_start = self.mmu_unit.calibrator.get_gear_rd(self.mmu_unit.first_gate) # Any RD is ok for startup
        cfg = SyncControllerConfig(
            log_sync = bool(self.p.sync_feedback_debug_log),
            buffer_range_mm = self.mmu_unit.buffer.buffer_range,
            buffer_max_range_mm = self.mmu_unit.buffer.buffer_maxrange,
            sensor_type = self._get_sensor_type(),
            use_twolevel_for_type_p = self.p.sync_feedback_force_twolevel,
            rd_start = rd_start,
            flowguard_relief_mm = self.p.flowguard_max_relief,
        )
        # EKF "extreme" threshold tracks the analog sensor trigger point (type-P only)
        if self.mmu_unit.buffer.proportional_sensor:
            cfg.flowguard_extreme_threshold = self.mmu_unit.buffer.proportional_sensor.analog_sensor_threshold
        self.ctrl = SyncController(cfg)
        return self.ctrl


    def apply_flowguard_tuning(self):
        """
        Push live FlowGuard tuning to a running controller so MMU_TEST_CONFIG
        changes take effect immediately without a controller reset.
        """
        if not self.mmu_unit.has_buffer() or self.ctrl is None: return
        self.ctrl.cfg.flowguard_relief_mm = self.p.flowguard_max_relief


    def apply_extrude_threshold(self):
        """
        Re-register the extruder movement callback so a changed
        sync_feedback_extrude_threshold takes effect immediately while active.
        """
        if not self.mmu_unit.has_buffer() or not self.active: return
        self.mmu_unit.extruder_monitor().register_callback(self._handle_extruder_movement, self.p.sync_feedback_extrude_threshold)


    def config_flowguard_feature(self, enable):
        if not self.mmu_unit.has_buffer(): return
        if enable:
            self.mmu.log_info("FlowGuard monitoring feature %senabled" % ("already " if self.p.flowguard_enabled else ""))
            if not self.p.flowguard_enabled:
                self.p.flowguard_enabled = True
                if self.ctrl:
                    self.ctrl.flowguard.reset()
        else:
            self.mmu.log_info("FlowGuard monitoring feature %sdisabled" % ("already " if not self.p.flowguard_enabled else ""))
            self.p.flowguard_enabled = False


    def _get_sync_bias_raw(self):
        return float(self._get_sensor_state())


    def _get_sync_bias_modelled(self):
        if self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active and self.mmu.is_printing():
            # This is a better representation for UI when the controller is active
            return self.estimated_state
        else:
            # Otherwise return the real state
            return float(self._get_sensor_state())


    def _get_sensor_state(self, use_virtual_threshold=False):
        """
        Get current tension state based on current sensor feedback.
        Arg 'use_virtual_threshold' forces a descrete {-1, 0, 1) output even from proportional sensor
        Returns float in range [-1.0 .. 1.0] for proportional, {-1, 0, 1) for switch
        """
        sm = self.mmu.sensor_manager
        has_proportional   = sm.has_sensor(SENSOR_PROPORTIONAL)
        if has_proportional and not use_virtual_threshold:
            sensor = sm.get_sensor_obj(SENSOR_PROPORTIONAL)
            return sensor.get_status(0).get('value', 0.)

        tension_active     = sm.check_sensor(SENSOR_TENSION)
        compression_active = sm.check_sensor(SENSOR_COMPRESSION)

        if tension_active == compression_active:
            ss = SF_STATE_NEUTRAL
        elif compression_active:
            ss = SF_STATE_COMPRESSION
        elif tension_active:
            ss = SF_STATE_TENSION
        else:
            ss = SF_STATE_NEUTRAL
        return ss


    def _get_sensor_type(self):
        """
        Return symbolic sensor type based on current active sensors
          "P" => proportional z ∈ [-1, +1]; enables EKF
          "D" => discrete dual-switch z ∈ {-1,0,+1}; Optional EKF
          "CO" => compression-only switch z ∈ {0,+1}
          "TO" => tension_only switch z ∈ {-1,0}
        """
        has_tension, has_compression, has_proportional = self.get_active_sensors()
        return (
            "P" if has_proportional
            else "D" if has_compression and has_tension
            else "CO" if has_compression
            else "TO" if has_tension
            else "Unknown"
        )


    def _adjust_filament_tension_switch(self, use_gear_motor=True, max_move=None):
        """
        Helper to relax filament tension using the sync-feedback buffer. This can be performed either with the
        gear motor (default) or extruder motor (which is also good as an extruder loading check)
        Returns distance moved and whether operation was successful and neutral was found (or None if not performed)
        """
        fhomed = None
        actual = 0.

        state = self._get_sensor_state()
        if state == SF_STATE_NEUTRAL:
            return actual, True

        has_tension, has_compression, _ = self.get_active_sensors()
        if not (has_tension or has_compression):
            self.mmu.log_debug("No active sync feedback sensors; cannot adjust filament tension")
            return actual, fhomed

        max_move = max_move or self.mmu_unit.buffer.buffer_maxrange
        self.mmu.log_debug("Monitoring extruder entrance transition for up to %.1fmm..." % max_move)

        motor = "gear" if use_gear_motor else "extruder"
        speed = min(self.mmu_unit.p.gear_homing_speed, self.mmu.p.extruder_homing_speed) # Keep this tension adjustment slow

        # Determine direction based on state and motor type
        # Note that if buffer_range is 0, it implies
        # special case where neutral point overlaps both sensors
        if state == SF_STATE_COMPRESSION:
            self.mmu.log_debug("Relaxing filament compression")
            direction = -1 if use_gear_motor else 1

            if self.mmu_unit.buffer.buffer_range == 0:
                msg = "Homing to tension sensor"
                sensor = SENSOR_TENSION
                homing_dir = 1
            elif has_compression:
                msg = "Reverse homing off compression sensor"
                sensor = SENSOR_COMPRESSION
                homing_dir = -1
            else:
                msg = "Homing to tension sensor"
                sensor = SENSOR_TENSION
                homing_dir = 1

        else:
            # Tension state
            self.mmu.log_debug("Relaxing filament tension")
            direction = 1 if use_gear_motor else -1

            if self.mmu_unit.buffer.buffer_range == 0:
                msg = "Homing to compression sensor"
                sensor = SENSOR_COMPRESSION
                homing_dir = 1
            elif has_tension:
                msg = "Reverse homing off tension sensor"
                sensor = SENSOR_TENSION
                homing_dir = -1
            else:
                msg = "Homing to compression sensor"
                sensor = SENSOR_COMPRESSION
                homing_dir = 1

        actual,fhomed,_,_ = self.mmu.move_filament(
            msg,
            max_move * direction,
            speed=speed,
            motor=motor,
            homing_move=homing_dir,
            endstop_name=sensor,
        )

        if fhomed and self.mmu_unit.buffer.buffer_range != 0:
            if use_gear_motor:
                # Move just a little more to find perfect neutral spot between sensors
                _,_,_,_ = self.mmu.move_filament("Centering sync feedback buffer", (self.mmu_unit.buffer.buffer_range * direction) / 2.)
        else:
            self.mmu.log_debug("Failed to reach neutral filament tension after moving %.1fmm" % max_move)

        return actual, fhomed


    def _adjust_filament_tension_proportional(self):
        """
        Helper to relax filament tension using the proportional sync-feedback buffer.
        Returns: actual distance moved (mm), success bool
        """

        # nudge_mm:     per-move adjustment distance in mm (small feed or retract)
        # neutral_band: absolute value of proportional sensor reading considered "neutral".
        #               This can be loosely interpreted as a % over the max range of detection of the sensor.
        #               For example for a sensor with 14mm range, a 0.15 tolerance is approx 1.4mm either side of centre.
        # settle_time:  delay between moves to allow sensor feedback to update
        # timeout_s:    hard stop to avoid hanging if the sensor never clears
        neutral_band = 0.1
        settle_time  = 0.1
        timeout_s    = 10.0

        # Wait for all moves to clear
        self.mmu.movequeue_wait()

        # sanity-check parameters before doing anything
        # neutral band needs to have a non zero and non trivial value. Enforce 5% (0.05)
        # as the lower limit of acceptable neutral band tolerance.
        if neutral_band < 0.05:
            neutral_band = 0.05

        # maxrange is full end-to-end sensor span; use half as the per-side budget from neutral to either end
        maxrange_span_mm = float(self.mmu_unit.buffer.buffer_maxrange)
        if maxrange_span_mm <= 0.0:
            self.mmu.log_debug("Proportional adjust skipped: buffer maxrange <= 0")
            return 0., False
        per_side_budget_mm = 0.5 * maxrange_span_mm
        nudge_mm = per_side_budget_mm * neutral_band

        # Cap total nudge iterations to stay within the overall sensor range
        max_steps = int(math.ceil(maxrange_span_mm / nudge_mm))

        moved_total_mm   = 0.0  # total net distance moved during this adjustment
        moved_nudges_mm  = 0.0  # sum of all nudge moves
        moved_initial_mm = 0.0  # size of the initial proportional move (if any)
        steps            = 0    # total moves performed
        t_start          = self.mmu.reactor.monotonic()

        # --- Initial proportional correction ---
        # Negative sensor state = tension -> feed filament. positive sensor state = compression -> retract filament
        prop_state = self._get_sensor_state() # [-1..+1], 0 ≈ neutral
        if abs(prop_state) > neutral_band:
            # Initial move distance as a proportion to how off centre we are based on the sensor readings.
            # this will get the sensor close but likely will need a few fine adjustments (nudges) to get it
            # within the centre range depending on how large the bowden tube slack is.
            initial_move_mm = -prop_state * per_side_budget_mm
            if abs(initial_move_mm) >= nudge_mm:
                self.mmu.move_filament(
                    "Proportional initial adjust - extruder load",
                    initial_move_mm, motor="gear", wait=True
                )
                moved_total_mm += initial_move_mm
                moved_initial_mm = initial_move_mm
                steps += 1
                try:
                    self.mmu.reactor.pause(settle_time)
                except Exception:
                    time.sleep(settle_time)

        # --- Check proportional sensor state after initial move and return if within neutral deadband ---
        prop_state = self._get_sensor_state()
        if abs(prop_state) <= neutral_band:
            self.mmu.log_info(
                "Proportional adjust: neutral after initial "
                "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f, success=yes)" %
                (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, prop_state)
            )
            return moved_total_mm, True

        # --- Fine adjustment loop (nudges) ---
        while abs(moved_total_mm) < maxrange_span_mm and steps < max_steps:
            prop_state = self._get_sensor_state()
            # timeout safety: avoid hanging if the sensor never clears
            if (self.mmu.reactor.monotonic() - t_start) > timeout_s:
                self.mmu.log_info(
                    "Proportional adjust: timed out "
                    "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f)" %
                    (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, prop_state)
                )
                return moved_total_mm, False

            if abs(prop_state) <= neutral_band:
                # confirm neutral after a short wait
                try:
                    self.mmu.reactor.pause(settle_time)
                except Exception:
                    time.sleep(settle_time)
                prop_state = self._get_sensor_state()
                if abs(prop_state) <= neutral_band:
                    break

            # Direction: tension -> feed forward; compression -> retract
            nudge_move_mm = nudge_mm if prop_state < 0.0 else -nudge_mm
            # don't exceed the end to end sensor span (maxrange_span_mm). Serves as "ultimate" failsafe.
            if abs(moved_total_mm + nudge_move_mm) >= maxrange_span_mm:
                self.mmu.log_info(
                    "Proportional adjust: aborted (exceeded buffer) "
                    "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f)" %
                    (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, prop_state)
                )
                return moved_total_mm, False

            self.mmu.move_filament(
                "Proportional adjust - extruder load",
                nudge_move_mm, motor="gear", wait=True
            )
            moved_total_mm  += nudge_move_mm
            moved_nudges_mm += nudge_move_mm
            steps           += 1
            try:
                self.mmu.reactor.pause(settle_time)
            except Exception:
                time.sleep(settle_time)

        # Final check
        final_state = self._get_sensor_state()
        success = abs(final_state) <= neutral_band
        self.mmu.log_info(
            "Proportional adjust: complete "
            "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f, success=%s)" %
            (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, final_state, "yes" if success else "no")
        )
        return moved_total_mm, success
