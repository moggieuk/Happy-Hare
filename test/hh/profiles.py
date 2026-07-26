# Happy Hare test harness - machine profiles.
#
# A profile is a set of Kconfig symbol overrides, exactly as menuconfig would
# produce, from which the REAL shipped templates under config/ are rendered. We do
# not hand-write .cfg fixtures: the ones already in test/installer/ demonstrably
# rotted (they are still on the v3.00 `{param_x}` placeholder format with sections
# that no longer exist), and rendering the real templates means a renamed parameter
# or a broken [% if %] guard shows up as a bootup failure.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class Profile:
    """
    syms:         Kconfig symbol -> value. True/False for bool/tristate, str/int
                  otherwise. Applied in order, so a later entry can refine an
                  earlier one.
    extra_params: jinja render params applied AFTER the Kconfig dict, for the
                  handful the installer computes rather than stores
                  (PARAM_TOTAL_NUM_GATES / UNIT_NAME / MCU_NAME are filled in
                  automatically by cfg.render()).
    """

    def __init__(self, name, syms=None, extra_params=None, description=''):
        self.name = name
        self.syms = dict(syms or {})
        self.extra_params = dict(extra_params or {})
        self.description = description

    def derive(self, name, syms=None, extra_params=None, description=''):
        merged_syms = dict(self.syms)
        merged_syms.update(syms or {})
        merged_params = dict(self.extra_params)
        merged_params.update(extra_params or {})
        return Profile(name, merged_syms, merged_params, description or self.description)

    def __repr__(self):
        return 'Profile(%r)' % (self.name,)


# BoxTurtle is the first-milestone profile on purpose: it is a Type-B machine with
# selector_type VirtualSelector, and cmd_MMU_BOOTUP skips home_unit for a virtual
# selector (extras/mmu/mmu_controller.py:385-405). That means bootup can be reached
# without a working HomingMove, which the harness does not have until the
# filament-path model lands. It still exercises 4 gates, multigear steppers, an
# espooler, LEDs, a buffer and entry/exit/shared-exit sensors.
BOXTURTLE = Profile(
    'boxturtle',
    syms={'MMU_TYPE_BOX_TURTLE_1_0': True},
    description='BoxTurtle 1.0 - Type B, VirtualSelector, 4 gates, multigear')

# BoxTurtle + one NFC reader on the unit (RC522 over SPI).
#
# Terminology trap: Kconfig's MMU_HAS_SHARED_NFC_READER means "shared across MMU
# UNITS" and `depends on MULTI_UNIT`, so a single-unit machine cannot select it. The
# single-reader branch is what a one-unit machine with one reader uses, and HH still
# treats that reader as the unit's shared (non-per-gate) reader.
NFC_SINGLE = BOXTURTLE.derive(
    'nfc_single',
    syms={'MMU_HAS_NFC_READER': True},
    description='BoxTurtle + one unit-level NFC reader (RC522/SPI)')

# One reader per gate. This is the profile that exercises the per-gate read/LED path
# and MmuNfcEndstop, and the one that surfaces the mmu_nfc_reader.py:132 crash.
NFC_PER_GATE = BOXTURTLE.derive(
    'nfc_per_gate',
    syms={'MMU_HAS_NFC_READER': True, 'MMU_HAS_PER_GATE_NFC_READERS': True},
    description='BoxTurtle + per-gate NFC readers')

# The round-trip profile: per-gate NFC + Spoolman in a WRITABLE mode + auto-create
# + deep read. All four are needed or the interesting paths gate themselves off:
#   spoolman_support defaults to 'off' (mmu_machine_parameters.py), and MMU_GATE_MAP
#     refuses NEXT_SPOOLID in 'pull' mode, so 'push' is the mode to test
#   spoolman_nfc_auto_create gates the create-from-tag path
#   nfc_deep_read (per-unit) gates whether tag metadata is applied at all, and
#     nfc_auto_create_enabled() is deep_read AND auto_create AND writable
NFC_SPOOLMAN = NFC_PER_GATE.derive(
    'nfc_spoolman',
    syms={
        'CHOICE_SPOOLMAN_SUPPORT_PUSH': True,
        'BOOL_SPOOLMAN_NFC_AUTO_CREATE': True,
        'BOOL_NFC_DEEP_READ': True,
    },
    description='Per-gate NFC + Spoolman push + auto-create + deep read')

# Same but with a unit-level (non-per-gate) reader, for the shared-reader pending
# spool_id flow. NOTE: the reader is currently orphaned by a template bug (see
# test_mmu_nfc.TestSingleReaderWiring), so tests needing a live shared reader inject
# through _MMU_TEST NFC_READ=1 instead, which bypasses the reader layer by design.
NFC_SPOOLMAN_SHARED = NFC_SINGLE.derive(
    'nfc_spoolman_shared',
    syms={
        'CHOICE_SPOOLMAN_SUPPORT_PUSH': True,
        'BOOL_SPOOLMAN_NFC_AUTO_CREATE': True,
        'BOOL_NFC_DEEP_READ': True,
    },
    description='Unit-level NFC + Spoolman push + auto-create + deep read')

PROFILES = {p.name: p for p in (BOXTURTLE, NFC_SINGLE, NFC_PER_GATE,
                                NFC_SPOOLMAN, NFC_SPOOLMAN_SHARED)}


def get(name):
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError("unknown profile %r; known: %s"
                       % (name, ', '.join(sorted(PROFILES))))
