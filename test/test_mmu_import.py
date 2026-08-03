# Happy Hare test harness - milestone A0.
#
# The smallest useful test: does Happy Hare import at all, outside Klipper?
#
# This is deliberately the first milestone because it has the highest
# information-per-line in the whole harness. It proves:
#   - the fake klippy overlay reproduces the install layout well enough for HH's
#     relative imports (`from .... import bus`, `from ... import led`, ...) and its
#     top-level ones (`import mcu`, `from kinematics.extruder import ...`) to resolve
#   - the repo's own extras/ does NOT leak in via namespace-package unioning
#   - the pkgutil-driven registries in selectors/ and commands/ successfully import
#     EVERY module they contain - which is why extras/homing.py must exist even for
#     configs that never home (mmu_linear_servo_selector.py:35 imports it at module
#     scope and selectors/__init__.py imports every selector unconditionally)
#
# It needs no third-party dependencies and no config.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import glob
import os
import unittest
import warnings

from test.hh import install

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# SyntaxWarnings that exist upstream and are not fixed here. Add (relpath, lineno)
# entries only alongside a self-healing companion test, so the exclusion cannot outlive
# the problem.
#
# Empty because the one entry it held - mmu_controller.py:1080,1095, where f"|\{...}"
# used an invalid `\{` escape that becomes a hard SyntaxError in a future Python - has
# been fixed upstream (`\\{`, byte-identical output).
KNOWN_SYNTAX_WARNINGS = set()

SCAN_PATTERNS = ('extras/**/*.py', 'components/*.py', 'installer/*.py')


def scan_syntax_warnings():
    """[(relpath, lineno, description)] for every shipped module. Sorted."""
    problems = []
    for pattern in SCAN_PATTERNS:
        for path in sorted(glob.glob(os.path.join(REPO_ROOT, pattern), recursive=True)):
            rel = os.path.relpath(path, REPO_ROOT)
            if '/temp/' in rel or rel.startswith('installer/lib/'):
                continue  # extras/temp/ is stale scratch; installer/lib is vendored
            with open(path, encoding='utf-8') as f:
                src = f.read()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                try:
                    compile(src, rel, 'exec')
                except SyntaxError as e:
                    problems.append((rel, e.lineno or 0, 'SyntaxError: %s' % (e,)))
                    continue
            for w in caught:
                problems.append((rel, w.lineno or 0,
                                 '%s: %s' % (w.category.__name__, w.message)))
    return sorted(problems)


class TestHappyHareImports(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.klippy = install()

    def test_overlay_is_authoritative(self):
        """The fake tree must win over the repo's own extras/ directory."""
        import extras
        self.assertEqual(
            os.path.realpath(os.path.dirname(extras.__file__)),
            os.path.realpath(os.path.join(self.klippy, 'extras')))

    def test_hh_modules_resolve_to_repo_source(self):
        """
        The overlay symlinks must point at the working tree, so a test can never
        silently exercise a stale copy.
        """
        import extras.mmu_machine as mmu_machine
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(os.path.realpath(mmu_machine.__file__),
                         os.path.realpath(os.path.join(repo, 'extras/mmu_machine.py')))

    def test_klipper_registered_modules_import(self):
        """The four modules Klipper discovers by section name."""
        import extras.mmu_machine as mmu_machine
        import extras.mmu_stepper as mmu_stepper
        import extras.mmu_servo as mmu_servo
        import extras.mmu_led_effect as mmu_led_effect
        self.assertTrue(hasattr(mmu_machine, 'load_config'))
        self.assertTrue(hasattr(mmu_stepper, 'load_config_prefix'))
        self.assertTrue(hasattr(mmu_servo, 'load_config_prefix'))
        self.assertTrue(hasattr(mmu_led_effect, 'load_config'))
        self.assertTrue(hasattr(mmu_led_effect, 'load_config_prefix'))

    def test_command_registry_populates(self):
        """commands/__init__.py pkgutil-imports every command module."""
        from extras.mmu.commands import COMMAND_REGISTRY
        self.assertGreater(len(COMMAND_REGISTRY), 30)
        for expected in ('MmuGateMapCommand', 'MmuNfcCommand', 'MmuNfcScanCommand',
                         'MmuTestCommand', 'MmuPreloadCommand'):
            self.assertIn(expected, COMMAND_REGISTRY)

    def test_selector_registry_populates(self):
        """selectors/__init__.py pkgutil-imports every selector module."""
        from extras.mmu.unit.selectors import SELECTOR_REGISTRY
        self.assertIn('VirtualSelector', SELECTOR_REGISTRY)
        self.assertIn('LinearSelector', SELECTOR_REGISTRY)

    def test_nfc_package_imports(self):
        """
        The NFC package reaches 4 levels up for Klipper's bus module
        (reader_factory.py:12) - the deepest relative import in the codebase.
        """
        from extras.mmu.unit.nfc import reader_factory
        self.assertTrue(hasattr(reader_factory, 'DEFAULT_READER_TYPE'))
        import extras.mmu.unit.nfc.mmu_nfc_reader as reader
        import extras.mmu.unit.nfc.mmu_nfc_endstop as endstop
        import extras.mmu.unit.nfc.tag_parser as tag_parser
        self.assertTrue(hasattr(reader, 'MmuNfcReader'))
        self.assertTrue(hasattr(endstop, 'MmuNfcEndstop'))
        self.assertTrue(hasattr(tag_parser, 'parse_tag'))

    def test_mcu_endstop_is_the_isinstance_anchor(self):
        """
        Two HH sites gate on isinstance(x, mcu.MCU_endstop), and at
        extras/mmu/mmu_filament_movement.py:329 a miss SILENTLY disables NFC-compound
        preload. Assert base and concrete switch live in one module so class
        identity can't drift, and that the clock domains stay distinct.
        """
        import mcu
        self.assertTrue(hasattr(mcu, 'MCU_endstop'))
        self.assertTrue(issubclass(mcu.MCU_endstop, object))
        self.assertTrue(hasattr(mcu, 'TRSYNC_TIMEOUT'))
        # print_time must never equal reactor eventtime - see mcu.HOST_OFFSET
        m = mcu.MCU('test')
        self.assertNotEqual(m.estimated_print_time(1000.), 1000.)

    def test_no_syntax_warnings(self):
        """
        Compile every shipped module with warnings escalated. This is the ast.parse
        check the dev handoffs relied on, but stricter: SyntaxWarnings here (e.g.
        invalid escape sequences) become hard SyntaxErrors in a future Python, so a
        module that "parses fine" today can stop loading after a Klipper host
        upgrade. Cheap, and it covers files no test imports.

        Known-outstanding warnings are excluded via KNOWN_SYNTAX_WARNINGS so this
        stays a live guard for the rest of the repo; the companion test below tracks
        the known ones.
        """
        problems = [p for p in scan_syntax_warnings()
                    if (p[0], p[1]) not in KNOWN_SYNTAX_WARNINGS]
        self.assertEqual(problems, [],
                         '\n'.join([''] + ['%s:%s %s' % p for p in problems]))

    def test_no_known_syntax_warnings_are_outstanding(self):
        """
        Companion to the scan above. While KNOWN_SYNTAX_WARNINGS holds entries this
        should carry @unittest.expectedFailure, which makes the exclusion self-healing:
        unittest reports an unexpected success as a FAILURE and exits non-zero, so the
        suite goes red the moment the underlying problem is fixed and prompts removing
        both the entry and the decorator. That is exactly how the last entry got
        cleaned up.
        """
        outstanding = [p for p in scan_syntax_warnings()
                       if (p[0], p[1]) in KNOWN_SYNTAX_WARNINGS]
        self.assertEqual(outstanding, [],
                         '\n'.join([''] + ['%s:%s %s' % p for p in outstanding]))

    def test_chelper_is_a_loud_stub(self):
        """
        The only `import chelper` in HH is unused (mmu_drive.py:20). If that ever
        changes, this fails rather than silently requiring a C build.
        """
        import chelper
        with self.assertRaises(AssertionError):
            chelper.get_ffi()


if __name__ == '__main__':
    unittest.main()
