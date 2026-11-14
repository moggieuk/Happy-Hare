# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Ordered macro annotations; no motion, synchronization, or MMU state changes.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
from .mmu_base_command import BaseCommand, CATEGORY_GENERAL, CATEGORY_INTERNAL


class MmuSendTelemetryCommand(BaseCommand):
    CMD = '_MMU_SEND_TELEMETRY'

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run,
                      help_brief='Record a macro animation event',
                      help_params='_MMU_SEND_TELEMETRY EVENT=cut|release',
                      category=CATEGORY_INTERNAL, log=False)

    def _run(self, gcmd):
        telemetry = self.mmu.movement_telemetry
        if not telemetry.listeners:
            return
        event = gcmd.get('EVENT').strip().strip('"').lower()
        if event not in ('cut', 'release'):
            raise gcmd.error('Unsupported telemetry EVENT: %s' % event)
        variables = self.mmu.cut_tip_vars() or {}
        blade = variables.get('blade_pos')
        telemetry.state(event, cutter='toolhead',
                        blade_pos=float(blade) if blade is not None else None,
                        unit=self.mmu.mmu_unit().name, gate=self.mmu.gate_selected,
                        origin='macro', phase=event.upper())


class MmuTelemetryCommand(BaseCommand):
    CMD = 'MMU_TELEMETRY'

    HELP_BRIEF = 'Record motion telemetry to the Moonraker log'
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + f"{CMD} RECORD=[off|continuous|toolchange]\n"
        + "  RECORD=continuous   ...record everything until switched off\n"
        + "  RECORD=toolchange   ...record the next load, unload or toolchange command, then stop\n"
        + "  RECORD=off          ...stop recording (no RECORD shows the current mode)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} RECORD=toolchange ...then run T1 to capture just that toolchange\n"
        + f"{CMD} RECORD=off ...close the log\n"
    )
    MODES = ('off', 'continuous', 'toolchange')

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(name=self.CMD, handler=self._run, help_brief=self.HELP_BRIEF, help_params=self.HELP_PARAMS,
                      help_supplement=self.HELP_SUPPLEMENT, category=CATEGORY_GENERAL)

    def _run(self, gcmd):
        api = self.mmu.telemetry_api
        mode = gcmd.get('RECORD', None)
        if mode is None:
            self.mmu.log_always("Telemetry recording: %s" % api.stream.record_mode)
            return
        mode = mode.strip().lower()
        if mode not in self.MODES:
            raise gcmd.error("RECORD must be one of: %s" % ', '.join(self.MODES))
        api.set_record_mode(mode)
        self.mmu.log_always("Telemetry recording: %s" % mode)
