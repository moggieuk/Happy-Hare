# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SENSOR_INSERT command
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


class MmuSensorInsertCommand(BaseCommand):
    """
    Callback to handle insert event from an MMU sensor.

    Params:
        EVENTTIME will contain reactor time that the sensor triggered
                  and command was queued
        SENSOR    will contain sensor name
        GATE      will be set if specific mmu entry or mmu exit sensor
    """

    CMD = "__MMU_SENSOR_INSERT"

    HELP_BRIEF = "Internal MMU filament insertion handler"
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

        if not self.mmu.is_enabled: return
        self.mmu._fix_started_state()

        eventtime = gcmd.get_float('EVENTTIME', self.mmu.reactor.monotonic())
        gate = gcmd.get_int('GATE', None)
        raw_sensor = gcmd.get('SENSOR', "")
        sensor = self.mmu.sensor_manager.get_unitless_sensor_name(raw_sensor)

        try:
            with self.mmu.wrap_sync_gear_to_extruder():

                if sensor.startswith(SENSOR_ENTRY_PREFIX) and gate is not None:
                    self.mmu._set_gate_status(gate, GATE_UNKNOWN)
                    self.mmu._check_pending_spool_id(gate)  # Have spool_id ready?
                    if not self.mmu.is_printing() and self.mmu.mmu_unit().p.gate_autoload:
                        self.mmu.gcode.run_script_from_command("MMU_PRELOAD GATE=%d" % gate)

                elif sensor == SENSOR_EXTRUDER_ENTRY:
                    if self.mmu.gate_selected != TOOL_GATE_BYPASS:
                        msg = "bypass not selected"
                    elif self.mmu.is_printing():
                        msg = "actively printing"  # Should not get here!
                    elif self.mmu.filament_pos != FILAMENT_POS_UNLOADED:
                        msg = "extruder cannot be verified as unloaded. Try running MMU_RECOVER to fix state"
                    elif not self.mmu.p.bypass_autoload:
                        msg = "bypass autoload is disabled"
                    else:
                        self.mmu.log_debug("Autoloading extruder")
                        with self.mmu._wrap_suspend_filament_monitoring():
                            self.mmu._note_toolchange("> Bypass")
                            self.mmu.load_sequence(
                                bowden_move=0.,
                                extruder_only=True,
                                purge=PURGE_NONE
                            )
                        return

                    self.mmu.log_debug("Ignoring extruder insertion because %s" % msg)

                else:
                    self.mmu.log_assertion(
                        "Unexpected/unhandled sensor insert event on %s. Ignored"
                        % raw_sensor
                    )

        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
