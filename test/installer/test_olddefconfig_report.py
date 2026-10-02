# olddefconfig reports every saved value the refresh changed without the user
# asking: a default moved by updated Kconfig rules, an explicit setting that no
# longer applies, or a symbol that no longer exists. On a config that is
# already current the report must be empty, or it is noise on every install.

import os
import tempfile
import unittest

import olddefconfig
import shared_components

from test.hh import cfg, profiles
from test.installer.test_shared_buffer import (
    BUFFER_OWNERS, BUFFER_SHARERS, _Install, _sharing)


class TestOlddefconfigReport(unittest.TestCase):

    def _refresh(self, write_before, env=cfg._SINGLE_UNIT_ENV):
        """Run olddefconfig's load/write over a file produced by write_before(path)."""
        with cfg._env(env), tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '.mmu_config')
            write_before(path)
            kc = cfg._new_kconfig('olddefconfig_report')
            before = olddefconfig.read_values(kc, path)
            kc.load_config(path)
            kc.write_config(path)
            return olddefconfig.change_report(kc, before, olddefconfig.read_values(kc, path))

    def _refresh_text(self, body):
        def write(path):
            with open(path, 'w') as handle:
                handle.write(body)
        return self._refresh(write)

    def test_current_config_reports_nothing(self):
        for profile in profiles.PROFILES.values():
            if profile.units:
                continue
            with self.subTest(profile=profile.name):
                with cfg._env(cfg._SINGLE_UNIT_ENV):
                    kc = cfg._kconfig(profile.name, profile.syms)
                self.assertEqual(self._refresh(lambda path: kc.write_config(path)), [])

    def test_current_multi_unit_config_reports_nothing(self):
        """
        Saved and refreshed the way install.sh does: in one directory, in unit order, each
        unit with KCONFIG_PARENT, so a unit sharing another's component can see its owner.
        """
        multi = [p for p in profiles.PROFILES.values() if p.units]
        self.assertTrue(multi)
        for profile in multi:
            with tempfile.TemporaryDirectory() as tmp:
                install = _Install(tmp, [u.name for u in profile.units], profile.syms)
                with self.subTest(profile=profile.name, unit='entry point'):
                    self.assertEqual(self._refresh_install(install, None), [])
                for unit in profile.units:
                    install.save(unit.name, unit.syms)
                for unit in profile.units:
                    with self.subTest(profile=profile.name, unit=unit.name):
                        self.assertEqual(self._refresh_install(install, unit.name), [])
                        saved = shared_components.read_values(install.path(unit.name))
                        for kind in shared_components.KINDS.values():
                            if unit.syms.get(kind.shared):
                                self.assertEqual(saved.get(kind.shared), 'y', kind.shared)

    def _refresh_install(self, install, unit):
        """olddefconfig over one of install's files (None: the top-level one)."""
        path = install.parent if unit is None else install.path(unit)
        env = install.entry_env if unit is None else install.env(unit)
        with cfg._env(env):
            kc = cfg._new_kconfig('olddefconfig_report')
        kc.warn = False
        before = olddefconfig.read_values(kc, path)
        kc.load_config(path)
        kc.write_config(path)
        return olddefconfig.change_report(kc, before, olddefconfig.read_values(kc, path))

    def test_a_sharer_whose_owner_changed_its_buffer_reports_only_derived_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['qidi'])           # tension only
            install.save('unit1', _sharing(BUFFER_SHARERS['tradrack']))
            self.assertEqual(self._refresh_install(install, 'unit1'), [])

            install.save('unit0', BUFFER_OWNERS['boxturtle'])      # compression + tension
            self.assertEqual(self._refresh_install(install, 'unit1'), [
                '  Defaults recomputed:',
                '    CHOICE_EXTRUDER_HOMING_ENDSTOP: NONE -> COMPRESSION',
                '    PARAM_EXTRUDER_HOMING_ENDSTOP: "none" -> "filament_compression"',
                '    PARAM_AUTOCAL_BOWDEN_LENGTH: 0 -> 1',
                '  1 new option(s) set to their defaults'])

    def test_a_sharer_whose_owner_lost_its_buffer_reports_the_pick_it_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])
            install.save('unit1', _sharing(BUFFER_SHARERS['tradrack'], owner='unit0'))
            install.save('unit0', profiles.get('tradrack').syms)
            report = self._refresh_install(install, 'unit1')
            self.assertIn('  No longer defined, dropped: CHOICE_SHARED_BUFFER_UNIT0', report)
            self.assertNotIn('PARAM_SYNC_FEEDBACK_BUFFER_NAME', '\n'.join(report))

    def _refresh_renamed(self, syms, renames):
        """Write syms, then spell each line the way a config from before the rename did."""
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig('olddefconfig_renamed', syms)

        def write(path):
            kc.write_config(path)
            with open(path) as handle:
                text = handle.read()
            for current, old in renames.items():
                self.assertIn(current, text)
                text = text.replace(current, old)
            with open(path, 'w') as handle:
                handle.write(text)
        return self._refresh(write)

    def test_migrated_choice_selection_is_not_reported(self):
        report = self._refresh_renamed(
            {'MMU_TYPE_ERCF_2_0': True, 'CHOICE_GEAR_TMC2240_UART': True},
            {'CONFIG_CHOICE_GEAR_TMC2240_UART=y\n': 'CONFIG_CHOICE_GEAR_TMC2240=y\n'})
        self.assertEqual(report, [])

    def test_migrated_string_to_float_value_is_not_reported(self):
        report = self._refresh_renamed(
            {'MMU_HAS_BLOBIFIER': True, 'CHOICE_BLOBIFIER_TYPE_STEPPER': True,
             'PARAM_BLOBIFIER_RUN_CURRENT': '0.45'},
            {'CONFIG_PARAM_BLOBIFIER_RUN_CURRENT=0.45\n':
             'CONFIG_PARAM_BLOBIFIER_STEPPER_RUN_CURRENT="0.45"\n'})
        self.assertEqual(report, [])

    def test_moved_choice_default_is_reported(self):
        report = self._refresh_text(
            'CONFIG_MMU_HAS_BLOBIFIER=y\n'
            'CONFIG_CHOICE_PURGE_MACRO_PURGE=y #~DEFAULT~#\n'
            'CONFIG_PARAM_PURGE_MACRO="_MMU_PURGE" #~DEFAULT~#\n')
        self.assertIn('  Defaults recomputed:', report)
        self.assertIn('    CHOICE_PURGE_MACRO: PURGE -> BLOBIFIER', report)
        self.assertIn('    PARAM_PURGE_MACRO: "_MMU_PURGE" -> "BLOBIFIER"', report)

    def test_explicit_setting_that_no_longer_applies_is_reported(self):
        report = self._refresh_text(
            '# CONFIG_MMU_HAS_BLOBIFIER is not set #~DEFAULT~#\n'
            'CONFIG_CHOICE_PURGE_MACRO_BLOBIFIER=y\n')
        self.assertIn('  Explicit settings that no longer apply:', report)
        self.assertIn('    CHOICE_PURGE_MACRO: BLOBIFIER -> PURGE', report)

    def test_undefined_symbol_is_reported_as_dropped(self):
        report = self._refresh_text('CONFIG_PARAM_TEST_NO_SUCH_SYMBOL="x"\n')
        self.assertIn('  No longer defined, dropped: PARAM_TEST_NO_SUCH_SYMBOL', report)
