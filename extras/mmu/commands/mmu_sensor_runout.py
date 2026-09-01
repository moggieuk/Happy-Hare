# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SENSOR_RUNOUT command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuSensorRunoutCommand(BaseCommand):
    """
    Callback to handle runout event from an MMU sensor.

    Note that pause_resume.send_pause_command() will have already been
    issued but no PAUSE command.

    Params:
        EVENTTIME will contain reactor time that the sensor triggered
                  and command was queued
        SENSOR    will contain sensor name
        GATE      will be set if specific mmu entry or mmu exit sensor
    """

    CMD = "__MMU_SENSOR_RUNOUT"

    HELP_BRIEF = "Internal MMU filament runout handler"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "EVENTTIME = #(float)\n"
        + "SENSOR    = _sensor_name_\n"
        + "GATE      = #(int)\n"
    )
    HELP_SUPPLEMENT = ""  # Internal callback command

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_INTERNAL
        )

    def _run(self, gcmd):
        # BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if not mmu.is_enabled:
            # Undo what runout sensor handling did
            mmu.pause_resume.send_resume_command()
            return

        mmu.fix_started_state()

        eventtime = gcmd.get_float('EVENTTIME', mmu.reactor.monotonic())
        gate = gcmd.get_int('GATE', None)
        raw_sensor = gcmd.get('SENSOR', "")
        sensor = mmu.sensor_manager.get_unprefixed_sensor_name(raw_sensor)
        process_runout = False

        # Delivery is slow by design: the handler blocks on the gcode mutex, so an event can arrive
        # long after it was raised. Only drop it if it was raised before a runout we already handled,
        # or inside a window where monitoring was deliberately suspended
        duplicate = eventtime < mmu.runout_last_handled_time
        suspended = mmu.runout_last_disable_time <= eventtime < mmu.runout_last_enable_time

        try:
            with mmu.wrap_sync_gear_to_extruder():

                # Events may be delivered after another gate or unit was selected. Read
                # the exact sensor named by the event, not a generic sensor in the active map.
                sensor_state = mmu.sensor_manager.check_event_sensor(raw_sensor, gate)

                if sensor and sensor_state is not False:
                    state = "still detects filament" if sensor_state is True else "cannot be read"
                    mmu.log_assertion(
                        "Runout handler suspects sensor malfunction on %s (%s). Ignored"
                        % (raw_sensor, state)
                    )

                else:
                    is_entry = sensor.startswith(SENSOR_ENTRY_PREFIX) and gate is not None
                    is_eject_gate = (
                        is_entry
                        and mmu.endless_spool_enabled
                        and mmu.p.endless_spool_eject_gate == gate
                    )

                    # Always update the gate map from a confirmed-clear entry sensor,
                    # including the selected gate and stale/suspended events. The one
                    # exception is a designated waste gate actively consuming filament.
                    if is_entry and not is_eject_gate:
                        mmu.gate_maps.set_gate_status(gate, GATE_EMPTY)

                    if duplicate or suspended:
                        msg = (
                            "%s sensor runout event on %s. Ignored (event=%.3f disable=%.3f enable=%.3f handled=%.3f now=%.3f)"
                            % ("Duplicate" if duplicate else "Suspended", raw_sensor, eventtime,
                               mmu.runout_last_disable_time, mmu.runout_last_enable_time,
                               mmu.runout_last_handled_time, mmu.reactor.monotonic())
                        )
                        if duplicate:
                            mmu.log_trace(msg) # Expected: second sensor reporting a runout we already handled
                        else:
                            mmu.log_debug(msg)

                    # Real runout to process...
                    elif is_entry and gate == mmu.gate_selected:
                        if is_eject_gate:
                            mmu.log_trace(
                                "Ignoring filament runout detected by %s because endless_spool_eject_gate is active on that gate"
                                % raw_sensor
                            )
                        else:
                            process_runout = True

                    elif sensor == SENSOR_SHARED_EXIT and gate is None:
                        process_runout = True

                    elif sensor.startswith(SENSOR_EXIT_PREFIX) and gate == mmu.gate_selected:
                        process_runout = True

                    elif sensor.startswith(SENSOR_EXTRUDER_ENTRY):
                        raise MmuError("Filament runout occurred at extruder. Manual intervention is required")

                    # An idle lane emptying is normal (the user pulled the spool). The gate map
                    # was updated above if it was an entry sensor; there is nothing else to do
                    elif sensor.startswith((SENSOR_ENTRY_PREFIX, SENSOR_EXIT_PREFIX)):
                        mmu.log_debug(
                            "Runout event on %s which is not the selected gate. Ignored" % raw_sensor
                        )

                    else:
                        mmu.log_assertion(
                            "Unexpected/unhandled sensor runout event on %s. Ignored"
                            % raw_sensor
                        )

                if process_runout:
                    # Will send_resume_command() or fail and pause
                    mmu._runout(event_type="runout", sensor=sensor)
                else:
                    # Undo what runout sensor handling did
                    mmu.pause_resume.send_resume_command()

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
