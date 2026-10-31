# Happy Hare MMU Software
# Copyright (C) 2022-2026 Paul Morgan. GPLv3.
"""Ordered macro annotations; no motion, synchronization, or MMU state changes."""
from .mmu_base_command import BaseCommand, CATEGORY_INTERNAL


class MmuTelemetryCommand(BaseCommand):
    CMD = '_MMU_TELEMETRY'

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run,
                      help_brief='Record a macro animation event',
                      help_params='_MMU_TELEMETRY EVENT=cut|release',
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
