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
# lookup-or-create trick MmuNfcManager uses for a reader shared between gates. That is
# what makes "one scanner, one debt" true by construction rather than by bookkeeping.
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

    Everything here is runtime state. 'enabled' and the auto override deliberately do
    not persist, following the NFC readers rather than the filament sensors - turning a
    scanner off is a "not right now" action, and the configuration is the record of what
    the machine is supposed to do.
    """

    def __init__(self, serial):
        self.serial = serial
        self.connected = False
        self.enabled = True

        # Runtime override of td1_auto_update, set by MMU_TD1 AUTO=. None means
        # "follow the configuration", which is a per-unit parameter read at the point
        # of use - so this is the only piece of auto-update policy that lives here
        self.auto_override = None

        # Latest reading, as reported by Moonraker and normalized by measurement()
        self.td = None
        self.color = None
        self.scan_time = None

        self.error = None
        self.error_kind = None
        self.last_outcome = None

        # Attribution. 'owner' is the token armed by a load that is expected to produce
        # a reading; 'unclaimed' is the gate this scanner may still report a reading for
        # after everyone stopped waiting - see claimable()
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
        Drop the cached reading.

        A device that HAD a reading and now reports none has been power cycled or
        rebooted. Leaving the old one behind makes status quote a measurement the
        scanner no longer stands behind, and hands measurement() a record carrying both
        a value and an error.
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

        One slot is enough: a second unclaimed traverse only makes the pending reading
        older, and the question a claimant asks is never "how many" but "is the next
        reading certainly mine".
        """
        self.unclaimed = gate


    def claimable(self, gate):
        """
        True when a reading arriving now is certainly attributable to 'gate'.

        False while the scanner owes a different gate. Moonraker timestamps a reading
        when it receives it, not when filament entered the scanner, so a late reading
        from the previous gate is indistinguishable by time from this gate's own - the
        debt is the only thing that separates them.
        """
        return self.unclaimed in (None, gate)


    def settle(self):
        """
        Consume the outstanding debt with the reading that just arrived.

        The reading itself is written nowhere: it belongs to a gate that has already
        stopped waiting for it. Assumes the scanner reports in traversal order, which
        holds for one optical sensor on one filament path but is on the list of things
        still to confirm against real hardware - if it turns out a reading can overtake
        an older one, this settles with the wrong one and the claimant loses a
        measurement (it never gains a wrong one).
        """
        self.unclaimed = None


    def arm(self, token):
        """Take ownership of the next reading on behalf of a load in progress."""
        self.owner = token


    def release(self, gate=None):
        """
        Drop armed attribution, recording what this scanner is still owed.

        A token that was armed and never produced a reading means filament crossed the
        scanner with nobody left to claim what it measured. Forgetting that is what lets
        the next gate adopt the previous gate's measurement, so the debt is recorded
        here rather than only where an explicit wait times out - during a print nothing
        waits at all, and the token is simply dropped at the next tool change.
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

        Dropped rather than released: a disconnect invalidates the model of what is in
        front of the scanner, so there is nothing left to owe.
        """
        self.connected = False
        self.owner = None
        self.unclaimed = None
