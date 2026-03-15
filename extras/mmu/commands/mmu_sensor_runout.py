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
        "%s: %s\n" % (CMD, HELP_BRIEF)
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

        mmu._fix_started_state()

        eventtime = gcmd.get_float('EVENTTIME', mmu.reactor.monotonic())
        gate = gcmd.get_int('GATE', None)
        raw_sensor = gcmd.get('SENSOR', "")
        sensor = mmu.sensor_manager.get_unitless_sensor_name(raw_sensor)
        process_runout = False

        try:
            with mmu.wrap_sync_gear_to_extruder():

                if eventtime < mmu.runout_last_enable_time:
                    mmu.log_assertion("Late sensor runout event on %s. Ignored" % raw_sensor)

                elif sensor and mmu.sensor_manager.check_sensor(sensor):
                    mmu.log_assertion("Runout handler suspects sensor malfunction on %s. Ignored" % raw_sensor)

                else:
                    # Always update gate map from mmu entry sensor
                    if sensor.startswith(SENSOR_ENTRY_PREFIX) and gate != mmu.gate_selected:
                        mmu._set_gate_status(gate, GATE_EMPTY)

                    # Real runout to process...
                    if sensor.startswith(SENSOR_ENTRY_PREFIX) and gate == mmu.gate_selected:
                        if mmu.endless_spool_enabled and mmu.p.endless_spool_eject_gate == gate:
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
                        raise MmuError("Filament runout occured at extruder. Manual intervention is required")

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
