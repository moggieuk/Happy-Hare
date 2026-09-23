# Carrying a saved value across a symbol rename.
#
# kconfiglib drops an assignment to a symbol it does not know: it records the
# orphan in missing_syms and moves on, with nothing the user sees. So renaming
# a Kconfig symbol silently discards whatever they had configured - for a pin
# that defaults to "" that means the machine comes back with the pin blank.
#
# HH_RENAMED_SYMBOLS plus Kconfig._migrate_renamed_symbols close that. These
# tests drive the mechanism with a SYNTHETIC table, so it is proven before any
# real rename depends on it, and so they keep testing the mechanism rather
# than whichever renames happen to be in flight.

import os
import tempfile
import unittest
from unittest import mock

import kconfiglib

from test.hh import cfg

# Old names that exist nowhere, pointed at real symbols of three different
# types: a string pin, a float, and a bool.
# The blobifier symbols below are only visible when the feature is enabled,
# and kconfiglib discards a user value for an invisible symbol - so every
# fixture has to turn it on before the migrated value means anything.
BLOBIFIER_ON = ('CONFIG_MMU_HAS_BLOBIFIER=y\n'
                'CONFIG_CHOICE_BLOBIFIER_TYPE_STEPPER=y\n')

SYNTHETIC = {
    'PIN_TEST_LEGACY_UART': 'PIN_BLOBIFIER_UART',
    'PARAM_TEST_LEGACY_RESISTOR': 'PARAM_BLOBIFIER_SENSE_RESISTOR',
    'MMU_HAS_TEST_LEGACY_BLOBIFIER': 'MMU_HAS_BLOBIFIER',
    # Defaults to y, and is not pinned by BLOBIFIER_ON, so the `is not set`
    # form has something it can actually flip.
    'MMU_HAS_TEST_LEGACY_BUCKET': 'MMU_HAS_BLOBIFIER_BUCKET_SWITCH',
}


class TestRenamedSymbolMigration(unittest.TestCase):

    def _load(self, body, filter_defaults, table=SYNTHETIC):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._new_kconfig('rename_migration')
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, '.mmu_config')
                with open(path, 'w') as handle:
                    handle.write(BLOBIFIER_ON + body)
                with mock.patch.dict(kconfiglib.HH_RENAMED_SYMBOLS,
                                     table, clear=True):
                    kc.load_config(path, filter_defaults=filter_defaults)
        return kc

    def test_a_saved_value_reaches_the_renamed_symbol(self):
        for filter_defaults in (True, False):
            with self.subTest(filter_defaults=filter_defaults):
                kc = self._load('CONFIG_PIN_TEST_LEGACY_UART="unit0:PC14"\n',
                                filter_defaults)
                self.assertEqual(kc.get('PIN_BLOBIFIER_UART'),
                                 'unit0:PC14')

    def test_a_quoted_value_is_unquoted_for_a_non_string_successor(self):
        """This is what lets a string-typed symbol become a float."""
        kc = self._load('CONFIG_PARAM_TEST_LEGACY_RESISTOR="0.050"\n', False)
        self.assertEqual(
            kc.syms['PARAM_BLOBIFIER_SENSE_RESISTOR'].orig_type,
            kconfiglib.FLOAT)
        self.assertEqual(kc.get('PARAM_BLOBIFIER_SENSE_RESISTOR'), '0.050')

    def test_a_bool_successor_takes_y_or_n(self):
        kc = self._load('CONFIG_MMU_HAS_TEST_LEGACY_BLOBIFIER=y\n', False)
        self.assertTrue(kc.is_enabled('MMU_HAS_BLOBIFIER'))

    def test_the_unset_form_migrates_too(self):
        """`# CONFIG_X is not set` is matched by a different regex entirely.

        A migration that only handled KEY=VALUE would silently miss every
        disabled bool, which is the half of a saved config that records what
        the user turned OFF.
        """
        self.assertTrue(
            self._load('', False).is_enabled('MMU_HAS_BLOBIFIER_BUCKET_SWITCH'),
            'precondition: the successor defaults to y')

        kc = self._load('# CONFIG_MMU_HAS_TEST_LEGACY_BUCKET is not set\n', False)
        self.assertFalse(
            kc.is_enabled('MMU_HAS_BLOBIFIER_BUCKET_SWITCH'),
            'the saved "not set" did not reach the renamed symbol')

    def test_a_recorded_default_is_not_promoted_to_a_user_value(self):
        """The asymmetry between the two callers, and why it has to exist.

        A #~DEFAULT~# line records what the default happened to be. menuconfig
        clears those so the symbol stays resettable; the builder applies them.
        If the migration ignored the distinction it would turn every migrated
        default into a pinned user value and freeze the old default forever.
        """
        body = 'CONFIG_PIN_TEST_LEGACY_UART="unit0:PC14" #~DEFAULT~#\n'
        self.assertEqual(
            self._load(body, True).get('PIN_BLOBIFIER_UART'), '',
            'menuconfig should have left the successor at its own default')
        self.assertEqual(
            self._load(body, False).get('PIN_BLOBIFIER_UART'), 'unit0:PC14',
            'the builder should have carried the recorded default across')

    def test_an_explicit_new_name_assignment_wins(self):
        """A hand-edited file keeps the name it actually asked for."""
        kc = self._load('CONFIG_PIN_BLOBIFIER_UART="unit0:PB1"\n'
                        'CONFIG_PIN_TEST_LEGACY_UART="unit0:PC14"\n', False)
        self.assertEqual(kc.get('PIN_BLOBIFIER_UART'), 'unit0:PB1')

    def test_the_migration_is_idempotent(self):
        """One write under the new name and the old line is gone for good.

        That is what makes this need no marker, no version stamp and no state:
        it only fires while the old name is still in the file.
        """
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._new_kconfig('rename_idempotent')
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, '.mmu_config')
                with open(path, 'w') as handle:
                    handle.write(BLOBIFIER_ON +
                                 'CONFIG_PIN_TEST_LEGACY_UART="unit0:PC14"\n')
                with mock.patch.dict(kconfiglib.HH_RENAMED_SYMBOLS,
                                     SYNTHETIC, clear=True):
                    kc.load_config(path, filter_defaults=True)
                    self.assertEqual(
                        [name for name, _ in kc.missing_syms],
                        ['PIN_TEST_LEGACY_UART'])
                    kc.write_config(path, save_old=False)

                    with open(path) as handle:
                        saved = handle.read()
                    self.assertIn('CONFIG_PIN_BLOBIFIER_UART="unit0:PC14"',
                                  saved)
                    self.assertNotIn('PIN_TEST_LEGACY_UART', saved)

                    again = cfg._new_kconfig('rename_idempotent_reload')
                    again.load_config(path, filter_defaults=True)

        self.assertEqual(again.missing_syms, [])
        self.assertEqual(again.get('PIN_BLOBIFIER_UART'), 'unit0:PC14')

    def test_a_migrated_value_is_written_without_the_default_marker(self):
        """It was a user value before the rename; it stays one after."""
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._new_kconfig('rename_marker')
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, '.mmu_config')
                with open(path, 'w') as handle:
                    handle.write(BLOBIFIER_ON +
                                 'CONFIG_PIN_TEST_LEGACY_UART="unit0:PC14"\n')
                with mock.patch.dict(kconfiglib.HH_RENAMED_SYMBOLS,
                                     SYNTHETIC, clear=True):
                    kc.load_config(path, filter_defaults=True)
                    kc.write_config(path, save_old=False)
                with open(path) as handle:
                    saved = handle.read()
        self.assertIn('CONFIG_PIN_BLOBIFIER_UART="unit0:PC14"\n', saved)
        self.assertNotIn('CONFIG_PIN_BLOBIFIER_UART="unit0:PC14" #~DEFAULT~#',
                         saved)

    def test_an_unmapped_orphan_is_still_dropped(self):
        """The mechanism must not start rescuing arbitrary unknown symbols."""
        kc = self._load('CONFIG_PIN_COMPLETELY_UNKNOWN="ghost"\n', False)
        self.assertEqual(kc.missing_syms, [('PIN_COMPLETELY_UNKNOWN', '"ghost"')])


class TestBlobifierRenameEndToEnd(unittest.TestCase):
    """The real table, against a config written before the rename shipped."""

    MMU = 'config/base/mmu.cfg'

    # What an installed stepper-tray Blobifier looked like, with pins the user
    # had to set by hand (they default to "") so there is genuine state to lose.
    PRE_RENAME = (
        'CONFIG_MMU_HAS_BLOBIFIER=y\n'
        'CONFIG_CHOICE_BLOBIFIER_TYPE_STEPPER=y\n'
        'CONFIG_CHOICE_BLOBIFIER_TMC5160=y\n'
        'CONFIG_PIN_BLOBIFIER_STEPPER_STEP="unit0:PD4"\n'
        'CONFIG_PIN_BLOBIFIER_STEPPER_DIR="!unit0:PD3"\n'
        'CONFIG_PIN_BLOBIFIER_STEPPER_ENABLE="!unit0:PD6"\n'
        'CONFIG_PIN_BLOBIFIER_STEPPER_ENDSTOP="^!unit0:PC15"\n'
        'CONFIG_PIN_BLOBIFIER_STEPPER_CS="unit0:PC14"\n'
        'CONFIG_PARAM_BLOBIFIER_STEPPER_SPI_BUS="spi2"\n'
        'CONFIG_PARAM_BLOBIFIER_STEPPER_RUN_CURRENT="0.45"\n'
        'CONFIG_PARAM_BLOBIFIER_STEPPER_SENSE_RESISTOR=0.050\n')

    def _render_driver(self, body, table=None):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._new_kconfig('rename_e2e')
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, '.mmu_config')
                with open(path, 'w') as handle:
                    handle.write(body)
                if table is None:
                    kc.load_config(path, filter_defaults=True)
                else:
                    with mock.patch.dict(kconfiglib.HH_RENAMED_SYMBOLS,
                                         table, clear=True):
                        kc.load_config(path, filter_defaults=True)
            mmu = cfg._render_templates(
                (self.MMU,), kc, {'PARAM_TOTAL_NUM_GATES': 4})[self.MMU]
        parsed = cfg.assemble({self.MMU: mmu})
        section = 'tmc5160 manual_stepper stepper_blobifier'
        return dict(parsed.items(section)) if parsed.has_section(section) else {}

    def test_a_pre_rename_config_renders_what_it_always_did(self):
        """The acceptance test: an installed machine is unaffected."""
        migrated = self._render_driver(self.PRE_RENAME)
        self.assertEqual(migrated.get('cs_pin'), 'unit0:PC14')
        self.assertEqual(migrated.get('spi_bus'), 'spi2')
        self.assertEqual(migrated.get('run_current'), '0.45')
        self.assertEqual(migrated.get('sense_resistor'), '0.050')

        # ...and identically to the same settings spelled the new way. Note
        # run current is unquoted here: it is float now, and kconfiglib
        # rejects a quoted value for a float. That the pre-rename file can
        # still say "0.45" is exactly the unquoting the migration does.
        post = (self.PRE_RENAME
                .replace('BLOBIFIER_STEPPER_', 'BLOBIFIER_')
                .replace('CONFIG_PARAM_BLOBIFIER_RUN_CURRENT="0.45"',
                         'CONFIG_PARAM_BLOBIFIER_RUN_CURRENT=0.45'))
        self.assertEqual(migrated, self._render_driver(post))

    def test_without_the_table_the_saved_pins_are_silently_lost(self):
        """Documents the failure the table exists to prevent.

        If someone later drops these entries too early, or 'fixes'
        _undef_assign to raise instead, this is the test that explains what
        the mechanism was for.
        """
        unmigrated = self._render_driver(self.PRE_RENAME, table={})
        self.assertEqual(unmigrated.get('cs_pin'), '')
        self.assertNotIn('spi_bus', unmigrated)
        self.assertEqual(unmigrated.get('run_current'), '0.6')


class TestShippedRenameTable(unittest.TestCase):

    def test_every_entry_names_a_retired_symbol_and_a_live_one(self):
        """Guards against a table entry outliving the rename it describes."""
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._new_kconfig('rename_table')
        for old_name, new_name in sorted(kconfiglib.HH_RENAMED_SYMBOLS.items()):
            with self.subTest(old=old_name):
                self.assertNotIn(
                    old_name, kc.syms,
                    '%s is still defined - a rename table entry must describe '
                    'a symbol that no longer exists' % old_name)
                self.assertIn(
                    new_name, kc.syms,
                    '%s does not exist, so %s has nowhere to migrate to'
                    % (new_name, old_name))


if __name__ == '__main__':
    unittest.main()
