# Happy Hare MMU Software
#
# Native NFC runtime component. Physical chips are [mmu_nfc_reader] objects;
# this component creates their lane/shared state managers during MmuUnit init.

import logging

from .nfc import manager as manager
from .nfc.manager import NFCGate
from .nfc.shared_reader import MmuSharedNfcReader


_current_printer = None


class MmuNfc:
    def __init__(self, config, mmu_unit, params):
        global _current_printer
        self.unit = mmu_unit
        self.managers = []

        if _current_printer is not mmu_unit.printer:
            _current_printer = mmu_unit.printer
            del manager._lane_instances[:]
            manager._shared_instance = None
            manager._shared_configured = False
            del manager._diagnostic_warnings[:]

        if mmu_unit.nfc_reader and not manager._shared_configured:
            shared = MmuSharedNfcReader(
                config, mmu_unit=mmu_unit, shared=True,
                name='shared')
            manager._shared_configured = True
            manager._lane_instances.append(shared)
            self.managers.append(shared)
            mmu_unit.printer.add_object('nfc_gate shared', shared)
            logging.info("MMU: Created shared NFC manager for unit %s"
                         % mmu_unit.name)

        for lgate, reader_name in enumerate(mmu_unit.nfc_readers):
            if not reader_name:
                continue
            gate = mmu_unit.first_gate + lgate
            lane = NFCGate(
                config, mmu_unit=mmu_unit, mmu_gate=gate, shared=False,
                name='lane%d' % gate)
            manager._lane_instances.append(lane)
            self.managers.append(lane)
            mmu_unit.printer.add_object('nfc_gate lane%d' % gate, lane)
            logging.info("MMU: Created NFC manager for gate %d using %s"
                         % (gate, reader_name))

    def reinit(self):
        # Manager state is reset by its normal Klipper lifecycle handlers.
        pass
