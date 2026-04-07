# Happy Hare MMU Software
# Print/MMU lifecycle state machine
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Own printer/MMU lifecycle transitions and related state.
#       This is really an augmented klipper print state and incorporates MMU readiness
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging, sys

from .mmu_constants import *


class MmuPrintStateMachine:
    """
    MMU job state machine. Possible states:
    initialized | ready | started | printing | complete | cancelled | error | pause_locked | paused | standby | idle
    """

    def __init__(self, mmu):
        self.mmu = mmu
        self.printer = self.mmu.printer

        self.reinit()


    def reinit(self):
        self.last_print_stats = None
        self.paused_extruder_temp = None
        self.reason_for_pause = None
        self.print_state = "ready"
        self.resume_to_state = "ready"


    def register_event_handlers(self):
        """
        Setup events for managing internal print state machine
        """
        self.printer.register_event_handler("idle_timeout:printing", self._handle_idle_timeout_printing)
        self.printer.register_event_handler("idle_timeout:ready",    self._handle_idle_timeout_ready)
        self.printer.register_event_handler("idle_timeout:idle",     self._handle_idle_timeout_idle)


    def _handle_idle_timeout_printing(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "printing")

    def _handle_idle_timeout_ready(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "ready")

    def _handle_idle_timeout_idle(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "idle")

    def _handle_idle_timeout_event(self, eventtime, event_type):
        """
        Track print events simply to ease internal print state transitions. Specificly we want to detect
        the start and end of a print and falling back into 'standby' state on idle
    
        Klipper reference sources for state:
        print_stats: {'filename': '', 'total_duration': 0.0, 'print_duration': 0.0,
                      'filament_used': 0.0, 'state': standby|printing|paused|complete|cancelled|error,
                      'message': '', 'info': {'total_layer': None, 'current_layer': None}}
        idle_status: {'state': Idle|Ready|Printing, 'printing_time': 0.0}
        pause_resume: {'is_paused': True|False}
        """
        if not self.mmu.is_enabled: return
        self.mmu.log_trace("Processing idle_timeout '%s' event" % event_type)

        if self.mmu.print_stats and self.mmu.p.print_start_detection:
            new_ps = self.mmu.print_stats.get_status(eventtime)
            if self.last_print_stats is None:
                self.last_print_stats = dict(new_ps)
                self.last_print_stats['state'] = 'initialized'
            prev_ps = self.last_print_stats
            old_state = prev_ps['state']
            new_state = new_ps['state']
            if new_state != old_state:
                if new_state == "printing" and event_type == "printing":
                    # Figure out the difference between initial job start and resume
                    if prev_ps['state'] == "paused" and prev_ps['filename'] == new_ps['filename'] and prev_ps['total_duration'] < new_ps['total_duration']:
                        # This is a 'resumed' state so ignore
                        self.mmu.log_trace("Automaticaly detected RESUME (ignored), print_stats=%s, current mmu print_state=%s" % (new_state, self.print_state))
                    else:
                        # This is a 'started' state
                        self.mmu.log_trace("Automaticaly detected JOB START, print_status:print_stats=%s, current mmu print_state=%s" % (new_state, self.print_state))
                        if self.print_state not in ["started", "printing"]:
                            self.on_print_start(pre_start_only=True)
                            self.mmu.reactor.register_callback(lambda pt: self.print_event("MMU_PRINT_START AUTOMATIC=1"))
                elif new_state in ["complete", "error"] and event_type == "ready":
                    self.mmu.log_trace("Automatically detected JOB %s, print_stats=%s, current mmu print_state=%s" % (new_state.upper(), new_state, self.print_state))
                    if new_state == "error":
                        self.mmu.reactor.register_callback(lambda pt: self.print_event("MMU_PRINT_END STATE=error AUTOMATIC=1"))
                    else:
                        self.mmu.reactor.register_callback(lambda pt: self.print_event("MMU_PRINT_END STATE=complete AUTOMATIC=1"))
                self.last_print_stats = dict(new_ps)

        # Capture transition to standby
        if event_type == "idle" and self.print_state != "standby":
            self.mmu.reactor.register_callback(lambda pt: self.print_event("MMU_PRINT_END STATE=standby IDLE_TIMEOUT=1"))


    def print_event(self, command):
        self.mmu.log_info(f"PAUL: command={command}")
        try:
            self.mmu.gcode.run_script(command)

        except Exception as e:
            self.mmu.log_assertion(f"MMU: Error running job state initializer/finalizer: {e}", exc_info=sys.exc_info())


    def set_print_state(self, print_state, call_macro=True):
        """
        MMU job state machine: initialized | ready | started | printing | complete | cancelled | error | pause_locked | paused | standby | idle
        """
        if print_state != self.print_state:

            idle_timeout = self.mmu.printer.lookup_object("idle_timeout").idle_timeout
            self.mmu.log_debug("Job State: %s -> %s (MMU State: Encoder: %s, Synced: %s, Paused temp: %s, Resume to state: %s, Position saved for: %s, pause_resume: %s, Idle timeout: %.2fs)"
                    % (self.print_state.upper(), print_state.upper(), self.mmu.get_encoder_state(), self.mmu.mmu_toolhead().is_gear_synced_to_extruder(), self.paused_extruder_temp,
                        self.resume_to_state, self.mmu.saved_toolhead_operation, self.is_printer_paused(), idle_timeout))

            if call_macro:
                self.mmu.led_manager.print_state_changed(print_state, self.print_state)

                if self.mmu.printer.lookup_object("gcode_macro %s" % self.mmu.p.print_state_changed_macro, None) is not None:
                    self.mmu.wrap_gcode_command("%s STATE='%s' OLD_STATE='%s'" % (self.mmu.p.print_state_changed_macro, print_state, self.print_state))

            self.print_state = print_state


    def fix_started_state(self):
        """
        Workaround to force state transistion to printing for any early moves if MMU_PRINT_START not yet run
        """
        if self.is_printer_printing() and not self.is_in_print():
            self.mmu.wrap_gcode_command("MMU_PRINT_START FIX_STATE=1")


    def on_print_start(self, pre_start_only=False):
        """
        If this is called automatically when printing starts. The pre_start_only operations are performed on an idle_timeout
        event so cannot block.  The remainder of moves will be called from the queue but they will be called early so
        don't do anything that requires operating toolhead kinematics (we might not even be homed yet)
        """
        if self.print_state not in ["started", "printing"]:
            self.mmu.log_trace("on_print_start(->started)")
            self.mmu._clear_saved_toolhead_position()
            self.mmu.num_toolchanges = 0
            self.paused_extruder_temp = None
            self.mmu._reset_job_statistics() # Reset job stats but leave persisted totals alone
            self.mmu.reactor.update_timer(self.mmu.hotend_off_timer, self.mmu.reactor.NEVER) # Don't automatically turn off extruder heaters
            self.mmu.is_handling_runout = False
            self.mmu.gate_maps.clear_slicer_tool_map()
            self.mmu._enable_filament_monitoring() # Enable filament monitoring while printing
            self.mmu._initialize_encoder(dwell=None) # Encoder 0000
            self.set_print_state("started", call_macro=False)

        if not pre_start_only and self.print_state not in ["printing"]:
            self.mmu.log_trace("on_print_start(->printing)")
            self.mmu.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=min_lifted_z VALUE=0" % self.mmu.p.park_macro) # Sequential printing movement "floor"
            self.mmu.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=next_pos VALUE=False" % self.mmu.p.park_macro)
            msg = "Happy Hare initialized ready for print"
            if self.mmu.filament_pos == FILAMENT_POS_LOADED:
                msg += " (initial tool %s loaded)" % self.mmu.selected_tool_string()
            else:
                msg += " (no filament preloaded)"
            if self.mmu.ttg_map != self.mmu.p.default_ttg_map:
                msg += "\nWarning: Non default TTG map in effect"
            self.mmu.log_info(msg)
            self.set_print_state("printing")

            # Establish syncing state and grip (servo) position
            # (must call after print_state is set so we know we are printing)
            self.mmu.reset_sync_gear_to_extruder(self.mmu.mmu_unit().p.sync_to_extruder)

            # Ensure espooler wasn't reset
            self.mmu._adjust_espooler_assist()


    def on_print_end(self, state="complete"):
        """
        If this is called automatically it will occur after the user's print ends.
        Therefore don't do anything that requires operating kinematics
        """
        if not self.is_in_endstate():
            self.mmu.log_trace("on_print_end(%s)" % state)
            self.mmu.movequeues_wait()
            self.mmu._clear_saved_toolhead_position()
            self.resume_to_state = "ready"
            self.paused_extruder_temp = None
            self.mmu.reactor.update_timer(self.mmu.hotend_off_timer, self.mmu.reactor.NEVER) # Don't automatically turn off extruder heaters

            self.mmu.gate_maps.restore_automap_option()
            self.mmu._disable_filament_monitoring() # Disable filament monitoring

            if self.mmu.printer.lookup_object("idle_timeout").idle_timeout != self.mmu.p.default_idle_timeout:
                self.mmu.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.mmu.p.default_idle_timeout) # Restore original idle_timeout

            self.mmu._standalone_sync = False # Safer to clear this on print end or idle_timeout to standby to avoid user confusion
            self.set_print_state(state)

            # Establish syncing state and grip (servo) position
            # (must call after print_state is set)
            self.mmu.reset_sync_gear_to_extruder(False) # Intention is not to sync unless we have to

        if state == "standby" and not self.is_in_standby():
            self.set_print_state(state)

        self.mmu._clear_macro_state(reset=True)


    def wakeup(self):
        if self.is_in_standby():
            self.set_print_state("idle")


# -----------------------------------------------------------------------------------------------------------
# Convenient state tests
# -----------------------------------------------------------------------------------------------------------

    def is_printing(self, force_in_print=False):
        """
        Actively printing and not paused
        """
        return self.print_state in ["started", "printing"] or force_in_print or self.mmu.p.test_force_in_print

    def is_in_print(self, force_in_print=False):
        """
        Printer printing or paused
        """
        return bool(self.print_state in ["printing", "pause_locked", "paused"] or force_in_print or self.mmu.p.test_force_in_print)

    def is_mmu_paused(self): # The MMU is paused
        return self.print_state in ["pause_locked", "paused"]

    def is_mmu_paused_and_locked(self):
        """
        The MMU is paused and locked (meaning that it is awating a resume command to perhaps restore extruder temps)
        """
        return self.print_state in ["pause_locked"]

    def is_in_endstate(self):
        return self.print_state in ["complete", "cancelled", "error", "ready", "standby", "initialized"]

    def is_in_standby(self):
        return self.print_state in ["standby"]

    def is_printer_printing(self):
        return bool(self.mmu.print_stats and self.mmu.print_stats.state == "printing")

    def is_printer_paused(self):
        return self.mmu.pause_resume.is_paused

    def is_paused(self):
        return self.is_printer_paused() or self.is_mmu_paused()
