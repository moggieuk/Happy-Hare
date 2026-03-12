# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SENSOR_REMOVE command
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


class MmuSensorRemoveCommand(BaseCommand):
    """
    Callback to handle removal event from an MMU sensor (only mmu_entry for now).
    A removal event can happen both in and out of a print.

    Params:
        EVENTTIME will contain reactor time that the sensor triggered
                  and command was queued
        SENSOR    will contain sensor name
        GATE      will be set if specific mmu entry or mmu exit sensor
    """

    CMD = "__MMU_SENSOR_REMOVE"

    HELP_BRIEF = "Internal MMU filament removal handler"
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
                    # Ignore mmu entry runout if endless_spool_eject_gate feature is active
                    # and we want filament to be consumed to clear gate
                    if not (self.mmu.endless_spool_enabled and self.mmu.p.endless_spool_eject_gate > 0):
                        self.mmu._set_gate_status(gate, GATE_EMPTY)
                    else:
                        self.mmu.log_trace(
                            "Ignoring filament removal detected by %s because endless_spool_eject_gate is active"
                            % raw_sensor
                        )

                else:
                    self.mmu.log_assertion(
                        "Unexpected/unhandled sensor remove event on %s. Ignored"
                        % raw_sensor
                    )

        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
