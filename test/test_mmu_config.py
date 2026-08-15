# Happy Hare test harness - milestone A1.
#
# Pure strings: render the real shipped templates and assert the output is what the
# rest of the harness (and a real user) depends on. No fakes, no printer, no Klipper.
#
# This also stands as a direct regression test on the two installer/build.py
# functions the harness leans on (render_template, KConfig.as_dict), so a breaking
# change there fails fast and legibly here rather than surfacing later as a
# mysterious bootup failure.
#
# Requires jinja2 (installer/requirements.txt) - run with the repo venv:
#   ./venv/bin/python -m unittest test.test_mmu_config
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import re
import tempfile
import unittest

from test.hh import cfg, profiles

HARDWARE = 'config/base/mmu_hardware.cfg'
MMU = 'config/base/mmu.cfg'
PARAMS = 'config/base/mmu_parameters.cfg'
MACRO_VARS = 'config/base/mmu_macro_vars.cfg'


class TestBoxTurtleRender(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rendered = cfg.render(profiles.get('boxturtle'))

    def test_no_silent_misrender(self):
        """
        Chip-less pins (':PD5' rather than 'unit0:PD5') and leftover template
        tokens are the two ways this pipeline produces wrong-but-parseable config.
        Neither raises on its own, so assert explicitly.
        """
        cfg.assert_sane(self.rendered)

    def test_hardware_sections(self):
        secs = cfg.sections(self.rendered[HARDWARE])
        for expected in ('mcu unit0', 'mmu_unit unit0', 'mmu_sensors unit0',
                         'mmu_espooler unit0', 'mmu_leds unit0', 'mmu_buffer unit0',
                         'neopixel _unit0_leds',
                         'tmc2209 mmu_stepper unit0_gear', 'mmu_stepper unit0_gear'):
            self.assertIn(expected, secs)
        # 4 gates -> gear + gear_1..gear_3, each with its own TMC section
        for i in ('', '_1', '_2', '_3'):
            self.assertIn('mmu_stepper unit0_gear%s' % i, secs)
            self.assertIn('tmc2209 mmu_stepper unit0_gear%s' % i, secs)

    def test_mmu_sections(self):
        secs = cfg.sections(self.rendered[MMU])
        for expected in ('mmu_machine', 'mmu_parameters', 'mmu_toolhead default'):
            self.assertIn(expected, secs)
        # The LED effect palette the session-5 work renamed
        for effect in ('mmu_led_effect mmu_breathing_purple_slow',
                       'mmu_led_effect mmu_breathing_purple_fast',
                       'mmu_led_effect mmu_red_strobe',
                       'mmu_led_effect mmu_green_strobe_fast'):
            self.assertIn(effect, secs)

    def test_parameters_and_macro_var_sections(self):
        self.assertIn('mmu_unit_parameters unit0', cfg.sections(self.rendered[PARAMS]))
        secs = cfg.sections(self.rendered[MACRO_VARS])
        # HH reads .variables off these at connect/ready
        # (extras/mmu/mmu_controller.py:205-207, 219-221)
        for expected in ('save_variables', 'pause_resume', 'extruder',
                         'virtual_sdcard', 'display_status',
                         'gcode_macro _MMU_SEQUENCE_VARS'):
            self.assertIn(expected, secs)

    def test_unit_topology_is_self_consistent(self):
        """
        The [mmu_unit] body drives the whole construction tree, so a template
        regression here would surface as a confusing failure much later.
        """
        parser = cfg.assemble(self.rendered)
        unit = dict(parser.items('mmu_unit unit0'))
        self.assertEqual(unit['selector_type'], 'VirtualSelector')
        gears = [g.strip() for g in unit['gear_steppers'].split(',')]
        self.assertEqual(gears, ['unit0_gear', 'unit0_gear_1',
                                 'unit0_gear_2', 'unit0_gear_3'])
        # num_gates is per-unit; [mmu_machine] only names the units
        self.assertEqual(int(unit['num_gates']), 4)
        machine = dict(parser.items('mmu_machine'))
        self.assertEqual([u.strip() for u in machine['units'].split(',')], ['unit0'])

    def test_happy_hare_version_is_substituted(self):
        """
        [mmu_machine] happy_hare_version comes from $HH_VERSION via the Kconfig.
        If the env var is unset it renders as the literal '$HH_VERSION', and
        extras/mmu_machine.py:44-47 then either raises "not installed correctly" or
        blows up comparing major.minor - so pin it to the canonical VERSION.
        """
        machine = dict(cfg.assemble(self.rendered).items('mmu_machine'))
        self.assertEqual(machine['happy_hare_version'], cfg.hh_version())
        self.assertNotIn('$', machine['happy_hare_version'])

    def test_section_order_is_load_bearing(self):
        """
        [mmu_machine] MUST be processed before [mmu_stepper unit0_gear].

        MmuUnit force-loads each gear stepper itself with force_rail=True
        (extras/mmu/mmu_unit.py:305-312). If Klipper's generic section loop reached
        [mmu_stepper unit0_gear] first - it has its own load_config_prefix
        (extras/mmu_stepper.py:1311) - the stepper would be built WITHOUT a rail and
        HH's subsequent add_object would collide. Production works only because the
        sorted glob of mmu/base/*.cfg puts mmu.cfg ahead of mmu_hardware.cfg.
        """
        parser = cfg.assemble(self.rendered)
        order = parser.sections()
        self.assertLess(order.index('mmu_machine'),
                        order.index('mmu_stepper unit0_gear'),
                        'mmu.cfg must sort before mmu_hardware.cfg - see '
                        'test/hh/cfg.py BASE_TEMPLATES')

    def test_assemble_tolerates_duplicate_extruder_section(self):
        """
        [extruder] appears in both the printer stub (stepper options, which
        MmuExtruderWrapper needs at extras/mmu/unit/mmu_extruder_wrapper.py:58-59)
        and mmu_macro_vars.cfg (extrude limits). RawConfigParser(strict=False) must
        merge rather than raise.
        """
        stub = ('[extruder]\n'
                'step_pin: mcu:PA1\n'
                'dir_pin: mcu:PA2\n'
                'enable_pin: !mcu:PA3\n'
                'rotation_distance: 22.0\n'
                'microsteps: 16\n'
                'full_steps_per_rotation: 200\n')
        parser = cfg.assemble(self.rendered, printer_stub=stub)
        extruder = dict(parser.items('extruder'))
        self.assertEqual(extruder['step_pin'], 'mcu:PA1')          # from the stub
        self.assertIn('max_extrude_only_distance', extruder)       # from the template


# Deliberately TWO IDENTICAL BOXTURTLES rather than the real ercf_vvd profile. The point is
# to test the multi-unit RENDER PATH, so both units being the machine every other test
# already trusts means a failure here is the path and not an ERCF or ViViD quirk. It lives
# in this file rather than the PROFILES registry because it is a test fixture, not a machine
# anyone would run.
# Note there are NO shared syms. MULTI_UNIT, MULTI_UNIT_ENTRY_POINT and MMU_UNITS all have
# no prompt (Kconfig:146-162) - they are driven purely by env, so setting them here would warn
# and do nothing. Supplying `units` is the whole declaration; cfg.py derives the rest, which is
# why clone_across_units() needs to say so little.
TWO_UNIT = profiles.clone_across_units(
    'two_boxturtles', profiles.get('boxturtle'), ('unit0', 'unit1'),
    description='two BoxTurtles, for the multi-unit render path')


class TestMultiUnitRender(unittest.TestCase):
    """
    A multi-unit render is THREE Kconfig parses with different env, not one
    (install.sh:385-432). Everything here is a way for that plumbing to be silently wrong.
    """

    @classmethod
    def setUpClass(cls):
        cls.rendered = cfg.render(TWO_UNIT)

    def test_no_silent_misrender(self):
        """
        The cheapest and most specific check on the per-unit env: get MCU_NAME wrong and
        unit1's pins render as ':PD5' rather than 'unit1:PD5'.
        """
        cfg.assert_sane(self.rendered)

    def test_produces_the_installers_file_set_in_include_order(self):
        """
        Per-unit files carry a _<unit> suffix (Makefile:151-158) while mmu.cfg and
        mmu_macro_vars.cfg stay single. Insertion order IS include order for assemble(),
        and Klipper's glob is sorted.
        """
        self.assertEqual(list(self.rendered), [
            'config/base/mmu.cfg',
            'config/base/mmu_hardware_unit0.cfg',
            'config/base/mmu_hardware_unit1.cfg',
            'config/base/mmu_macro_vars.cfg',
            'config/base/mmu_parameters_unit0.cfg',
            'config/base/mmu_parameters_unit1.cfg',
        ])

    def test_each_unit_gets_its_own_mcu_pins(self):
        """
        THE test for per-parse env. kconfiglib expands $(MCU_NAME) into every board pin
        default at parse time, so if the second parse reused the first's env, unit1's
        hardware would be full of 'unit0:' pins - a valid-looking config wired to the wrong
        board.
        """
        for unit in ('unit0', 'unit1'):
            hw = self.rendered['config/base/mmu_hardware_%s.cfg' % unit]
            chips = {m.group(1) for m in re.finditer(r'[:!^~]?(unit\d+):', hw)}
            self.assertEqual(chips, {unit},
                             '%s references chips %s' % (unit, sorted(chips)))

    def test_units_are_declared_on_mmu_machine(self):
        parser = cfg.assemble(self.rendered)
        self.assertEqual(dict(parser.items('mmu_machine'))['units'], 'unit0,unit1')

    def test_gate_counts_are_per_unit_and_the_total_is_their_sum(self):
        """
        PARAM_TOTAL_NUM_GATES is the CROSS-UNIT SUM (build.py:481-492), not this unit's
        count. It drives the Tx macro wrappers, which are printer-wide - so a machine with
        4+4 gates needs T0..T7, and getting it wrong silently loses half the tools.
        """
        parser = cfg.assemble(self.rendered)
        for unit in ('unit0', 'unit1'):
            self.assertEqual(int(dict(parser.items('mmu_unit %s' % unit))['num_gates']), 4)
        tools = sorted(int(m.group(1)) for m in
                       (re.fullmatch(r'gcode_macro T(\d+)', s) for s in parser.sections())
                       if m)
        self.assertEqual(tools, list(range(8)),
                         'expected T0..T7 for a 4+4 gate machine')

    def test_a_multi_unit_render_does_not_leak_env_into_later_renders(self):
        """
        The env-leak guard, and the reason _env() restores rather than just assigns.

        ORDER IS LOAD-BEARING: the multi-unit render has to happen BETWEEN the two
        single-unit ones, because a leak only shows in a parse that follows it. A leaked
        MCU_NAME=unit1 would re-render boxturtle against unit1's MCU - wrong output, not an
        error, and assert_sane cannot see it because the pins are still well-formed.

        This also fails if the _render_cache key ever stops accounting for the units, since
        a stale cache would hand back the first render and hide a genuine leak.
        """
        before = cfg.render(profiles.get('boxturtle'))
        cfg.render(TWO_UNIT)
        # A distinct name so the cache cannot answer this from the first call
        again = cfg.render(profiles.Profile(
            'boxturtle_after_multi_unit',
            syms=dict(profiles.get('boxturtle').syms),
            description='env-leak guard'))

        self.assertEqual(sorted(before), sorted(again))
        for name in before:
            self.assertEqual(before[name], again[name],
                             '%s changed after an intervening multi-unit render - env '
                             'leaked out of _env()' % name)


class TestSelectorTypeChoice(unittest.TestCase):
    """
    The CHOICE_SELECTOR_TYPE menu (installer/Kconfig.selector_type, depends on MMU_CUSTOM) is
    not exercised by any Profile - every real machine picks its selector implicitly, through
    its own mmu_types/Kconfig.<vendor> defaults, never through this menu. So a broken choice
    here has no other test to catch it; hence going straight at kconfiglib rather than adding a
    whole custom-MMU profile just to reach one menu.

    LinearMultiGearSelector specifically had a typo'd symbol name (LINEAR_MUTLI_GEAR_SELECTOR)
    at the choice-option declaration, while the PARAM_SELECTOR_TYPE default (and
    Kconfig.speeds' depends on) used the correctly-spelled LINEAR_MULTI_GEAR_SELECTOR - a
    symbol that therefore never existed, so selecting this option in the installer silently
    fell through to the "VirtualSelector # Safety" catch-all instead.
    """

    def test_choosing_linear_multi_gear_selector_sets_the_matching_param(self):
        with cfg._env({}):
            kc = cfg._kconfig('linear-multi-gear-selector-choice', {
                'MMU_CUSTOM': True,
                'CHOICE_SELECTOR_TYPE_LINEAR_MULTI_GEAR_SELECTOR': True,
            })
        self.assertEqual(kc.syms['PARAM_SELECTOR_TYPE'].str_value, 'LinearMultiGearSelector')


class TestStickyKconfigSymbols(unittest.TestCase):
    """The sticky property pins selected hardware identifiers across installer runs."""

    KCONFIG_TEMPLATE = '''\
mainmenu "Sticky property test"

config PARAM_FOLLOWS_DEFAULT
  string
  default "%s"

config PARAM_STICKY
  string
  sticky
  default "%s"
'''

    @classmethod
    def setUpClass(cls):
        cfg._prepare_imports()
        import kconfiglib
        cls.kconfiglib = kconfiglib

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.kconfig_path = os.path.join(self.tmpdir.name, 'Kconfig')
        self.config_path = os.path.join(self.tmpdir.name, '.config')

    def tearDown(self):
        self.tmpdir.cleanup()

    def _parse(self, default):
        with open(self.kconfig_path, 'w') as f:
            f.write(self.KCONFIG_TEMPLATE % (default, default))
        return self.kconfiglib.Kconfig(self.kconfig_path, warn=False)

    def test_sticky_default_is_saved_as_an_explicit_assignment(self):
        kc = self._parse('first')
        kc.write_config(self.config_path, save_old=False)

        with open(self.config_path) as f:
            saved = f.read()

        self.assertIn('CONFIG_PARAM_FOLLOWS_DEFAULT="first" #~DEFAULT~#', saved)
        self.assertIn('CONFIG_PARAM_STICKY="first"\n', saved)
        self.assertNotIn('CONFIG_PARAM_STICKY="first" #~DEFAULT~#', saved)
        self.assertFalse(kc.syms['PARAM_FOLLOWS_DEFAULT'].sticky)
        self.assertTrue(kc.syms['PARAM_STICKY'].sticky)

    def test_reload_keeps_sticky_assignment_but_recalculates_normal_default(self):
        self._parse('first').write_config(self.config_path, save_old=False)
        kc = self._parse('second')
        kc.load_config(self.config_path)

        normal = kc.syms['PARAM_FOLLOWS_DEFAULT']
        sticky = kc.syms['PARAM_STICKY']
        self.assertIsNone(normal.user_value)
        self.assertEqual(normal.str_value, 'second')
        self.assertEqual(sticky.user_value, 'first')
        self.assertTrue(sticky._was_set)
        self.assertFalse(sticky._was_default)

    def test_all_connection_parameters_opt_in_to_sticky(self):
        env = {
            'F_PER_GATE_MCU': 'y',
            'UNIT_NAME': 'unit0',
            'MCU_NAME': 'unit0',
            'UNIT_INDEX': '0',
            'F_MULTI_UNIT': '',
            'F_MULTI_UNIT_ENTRY_POINT': '',
        }
        with cfg._env(env):
            kc = cfg._kconfig('sticky-connection-parameters', {})

        names = [
            'PARAM_MMU_SERIAL_DEVICE',
            'PARAM_MMU_CANBUS_UUID',
            'PARAM_BUFFER_SERIAL_DEVICE',
            'PARAM_BUFFER_CANBUS_UUID',
        ]
        for gate in range(10):
            names.extend((
                'PARAM_MMU_SERIAL_DEVICE_%d' % gate,
                'PARAM_MMU_CANBUS_UUID_%d' % gate,
            ))

        for name in names:
            self.assertIn(name, kc.syms)
            self.assertTrue(kc.syms[name].sticky, '%s must be sticky' % name)


if __name__ == '__main__':
    unittest.main()
