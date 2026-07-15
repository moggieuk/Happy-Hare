# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Mixin for the NFC reader command surface
#   NfcMixin
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..unit.nfc import manager as nfc_manager


class NfcMixin:
    """
    Mixin providing shared reader/spool resolution for the NFC commands.

    Intended for use by:
      NFC (per-lane), NFC_SHARED, NFC_REGISTER

    NFC gate objects own reader and spool state; the commands own all Klipper
    GCode registration and routing. These helpers resolve the object a command
    should act on and raise a clear gcmd error when nothing suitable exists.
    """

    def _lane(self, gcmd):
        gate_number = gcmd.get_int(
            'GATE', None, minval=0, maxval=self.mmu.num_gates - 1)
        if gate_number is None:
            raise gcmd.error('NFC requires GATE=<gate>')
        gate = nfc_manager.nfc_gate_for_gate_number(gate_number)
        if gate is None:
            raise gcmd.error(
                'No enabled nfc_gate is configured for MMU gate %d'
                % gate_number)
        return gate

    def _shared(self, gcmd):
        shared = nfc_manager._shared_instance
        if shared is None or not getattr(shared, '_enabled', True):
            raise gcmd.error('No enabled shared NFC reader is configured')
        return shared

    def _defaults(self):
        return self.printer.lookup_object('nfc_gate', None)

    def _spoolman(self):
        defaults = self._defaults()
        if defaults is not None:
            return getattr(defaults, '_spoolman', None)
        for gate in nfc_manager._lane_instances:
            if getattr(gate, '_enabled', True):
                return getattr(gate, '_spoolman', None)
        return None
