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
from ..mmu_constants import TELEMETRY_MODES
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
        + f"{CMD} [MODE=[off|on|toolchange]] [DEFAULT=[off|on|toolchange]]\n"
        + "  MODE=on          ...record everything until switched off\n"
        + "  MODE=toolchange  ...record the next load, unload or toolchange command, then stop\n"
        + "  MODE=off         ...stop recording\n"
        + "  DEFAULT=...      ...mode to use after a restart (default_telemetry_mode), not saved\n"
        + "  (no parameters)  ...report the current mode and the default\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} MODE=toolchange ...then run T1 to capture just that toolchange\n"
        + f"{CMD} MODE=off ...close the log\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(name=self.CMD, handler=self._run, help_brief=self.HELP_BRIEF, help_params=self.HELP_PARAMS,
                      help_supplement=self.HELP_SUPPLEMENT, category=CATEGORY_GENERAL)

    def _mode(self, gcmd, name):
        value = gcmd.get(name, None)
        if value is None:
            return None
        value = value.strip().lower()
        if value not in TELEMETRY_MODES:
            raise gcmd.error("%s must be one of: %s" % (name, ', '.join(TELEMETRY_MODES)))
        return value

    def _run(self, gcmd):
        mmu = self.mmu
        mode, default = self._mode(gcmd, 'MODE'), self._mode(gcmd, 'DEFAULT')
        if default is not None:
            mmu.p.set_param('default_telemetry_mode', default)
        if mode is not None:
            mmu.telemetry_api.set_record_mode(mode)
        mmu.log_always("Telemetry mode: %s (default: %s)" % (mmu.telemetry_api.stream.record_mode, mmu.p.default_telemetry_mode))
