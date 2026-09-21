# Happy Hare MMU Software
# One physical TD-1 scanner
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Hold everything true of one physical scanner, so a serial named by several
#       gates - or several units - is one object rather than several caches
#
# Registered in the klipper object registry as 'mmu_td1_device <serial>', the same
# lookup-or-create trick MmuNfcManager uses for a reader shared between gates.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#


class MmuTd1Device:
    """
    The live state of one TD-1 scanner.

    Runtime only. 'enabled' and the auto override do not persist, following the NFC
    readers rather than the filament sensors.
    """

    def __init__(self, serial):
        self.serial = serial
        self.connected = False
        self.enabled = True

        # Runtime override of td1_auto_update (MMU_TD1 AUTO=). None follows the unit's
        # configured value, which is resolved at the point of use
        self.auto_override = None

        # Latest reading, as reported by Moonraker and normalized by measurement()
        self.td = None
        self.color = None
        self.scan_time = None

        self.error = None
        self.error_kind = None
        self.last_outcome = None

        # Attribution. 'owner' is the token armed by a load expected to produce a
        # reading; 'unclaimed' is a gate still owed one - see claimable()
        self.owner = None
        self.unclaimed = None


    def record(self):
        """The cached reading in the shape measurement() validates."""
        return {'td': self.td, 'color': self.color,
                'scan_time': self.scan_time, 'error': self.error}


    def cache(self, valid):
        """Adopt a validated measurement."""
        self.td = valid['td']
        self.color = valid['color']
        self.scan_time = valid['scan_time']


    def forget_reading(self):
        """
        Drop the cached reading, for a scanner that was power cycled or rebooted.

        Keeping it would let status quote a measurement the scanner no longer stands
        behind, and hand measurement() a record carrying both a value and an error.
        """
        self.td = None
        self.color = None
        self.scan_time = None


# -----------------------------------------------------------------------------------------------------------
# ATTRIBUTION
# -----------------------------------------------------------------------------------------------------------

    def owe(self, gate):
        """
        Record that this scanner may still report a reading produced by 'gate'.

        One slot is enough: a claimant only asks "is the next reading certainly mine".
        """
        self.unclaimed = gate


    def claimable(self, gate):
        """
        True when a reading arriving now is certainly attributable to 'gate'.

        False while the scanner owes a different gate. Moonraker timestamps on receipt,
        so a late reading from the previous gate is indistinguishable by time - the debt
        is the only thing separating them.
        """
        return self.unclaimed in (None, gate)


    def settle(self):
        """
        Consume the outstanding debt with the reading that just arrived.

        The reading is written nowhere - it belongs to a gate that stopped waiting.
        Assumes the scanner reports in traversal order; if a reading could overtake an
        older one the claimant loses a measurement, but never gains a wrong one.
        """
        self.unclaimed = None


    def arm(self, token):
        """Take ownership of the next reading on behalf of a load in progress."""
        self.owner = token


    def release(self, gate=None):
        """
        Drop armed attribution, recording what this scanner is still owed.

        A token armed but never satisfied means filament crossed the scanner with nobody
        left to claim the reading. The debt is recorded here rather than only where an
        explicit wait times out, because during a print nothing waits at all.
        """
        owner = self.owner
        if owner is None or (gate is not None and owner['gate'] != gate):
            return
        self.owner = None
        if not owner.get('satisfied'):
            self.owe(owner['gate'])


    def disconnected(self):
        """
        Reset attribution because the scanner went away.

        Dropped rather than released - a disconnect invalidates what we believe is in
        front of the scanner, so there is nothing left to owe.
        """
        self.connected = False
        self.owner = None
        self.unclaimed = None
