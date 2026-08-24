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

import ast
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

    def test_every_rendered_macro_variable_is_a_valid_klipper_literal(self):
        """Real Klipper rejects the entire config if any variable is not a Python literal."""
        parser = cfg.assemble(self.rendered, macros=False)
        for section in parser.sections():
            if not section.startswith('gcode_macro '):
                continue
            for option, value in parser.items(section):
                if option.startswith('variable_'):
                    with self.subTest(section=section, option=option, value=value):
                        ast.literal_eval(value)

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


class TestMenuconfigMacroStrings(unittest.TestCase):

    def _pre_unload_value(self, configured, profile_name):
        profile = profiles.get('boxturtle').derive(
            profile_name,
            syms={'VAR_SEQUENCE_USER_PRE_UNLOAD_EXTENSION': configured})
        rendered = cfg.render(profile)
        parser = cfg.assemble(rendered, macros=False)
        raw = dict(parser.items('gcode_macro _MMU_SEQUENCE_VARS'))[
            'variable_user_pre_unload_extension']
        return raw, ast.literal_eval(raw)

    def test_plain_gcode_is_rendered_as_a_string_literal(self):
        raw, value = self._pre_unload_value('MY_PRE_UNLOAD', 'macro_string_plain')
        self.assertEqual(raw, '"MY_PRE_UNLOAD"')
        self.assertEqual(value, 'MY_PRE_UNLOAD')

    def test_empty_hook_is_an_empty_string_not_a_blank_value(self):
        raw, value = self._pre_unload_value('', 'macro_string_empty')
        self.assertEqual(raw, '""')
        self.assertEqual(value, '')

    def test_arbitrary_quotes_backslashes_and_newlines_survive_round_trip(self):
        configured = 'RESPOND MSG="can\'t unload" PATH=C:\\tmp\\tool\\nM117 done'
        raw, value = self._pre_unload_value(configured, 'macro_string_escaping')
        self.assertEqual(value, configured.replace('\\n', '\n'))
        self.assertIn('\\"', raw)
        self.assertIn('\\\\tmp', raw)
        self.assertIn('\\n', raw)

    def test_malformed_quoted_input_does_not_abort_jinja_rendering(self):
        configured = 'RESPOND MSG="unterminated'
        raw, value = self._pre_unload_value(configured, 'macro_string_malformed_quote')
        self.assertEqual(value, configured)
        self.assertTrue(raw.startswith('"'))

    def test_unexpected_internal_value_renders_invalid_sentinel_instead_of_raising(self):
        cfg._prepare_imports()
        from installer import build

        class Unstringable:
            def __str__(self):
                raise RuntimeError('cannot stringify')

        with self.assertLogs(level='ERROR'):
            rendered = build.klipper_string_literal(Unstringable())
        self.assertEqual(rendered, build.INVALID_KLIPPER_STRING_LITERAL)
        with self.assertRaises((SyntaxError, ValueError)):
            ast.literal_eval(rendered)

    def test_existing_manually_quoted_workaround_is_not_double_quoted(self):
        raw, value = self._pre_unload_value("'LEGACY_PRE_UNLOAD'",
                                            'macro_string_legacy_quotes')
        self.assertEqual(raw, '"LEGACY_PRE_UNLOAD"')
        self.assertEqual(value, 'LEGACY_PRE_UNLOAD')

    def test_optional_blobifier_fan_name_uses_the_same_encoding(self):
        profile = profiles.get('boxturtle').derive(
            'macro_string_blobifier',
            syms={'MMU_HAS_BLOBIFIER': True,
                  'VAR_BLOBIFIER_FAN_NAME': 'fan_generic fan0'})
        rendered = cfg.render(profile)
        parser = cfg.assemble(rendered, macros=False)
        raw = dict(parser.items('gcode_macro _BLOBIFIER_VARS'))['variable_fan_name']
        self.assertEqual(raw, '"fan_generic fan0"')
        self.assertEqual(ast.literal_eval(raw), 'fan_generic fan0')


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


class TestPreviousKconfigSelections(unittest.TestCase):
    """Unavailable dynamic devices remain selectable through a stable previous option."""

    KCONFIG_TEMPLATE = '''\
mainmenu "Previous selection test"

config CURRENT_AVAILABLE
  bool
  default %s

choice DEVICE_CHOICE
  prompt "Select device"
  default DEVICE_PREVIOUS if "$(saved-config-value,PARAM_DEVICE)" != ""
  default DEVICE_CURRENT if CURRENT_AVAILABLE

  config DEVICE_PREVIOUS
    bool "Previous selection: $(saved-config-value,PARAM_DEVICE)"
    depends on "$(saved-config-value,PARAM_DEVICE)" != ""

  config DEVICE_CURRENT
    bool "Current device"
    depends on CURRENT_AVAILABLE

  config DEVICE_OTHER
    bool "Other"
endchoice

config PARAM_DEVICE
  string
  default "$(saved-config-value,PARAM_DEVICE)" if DEVICE_PREVIOUS
  default "/dev/current" if DEVICE_CURRENT
  default "/dev/other" if DEVICE_OTHER
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

    def _parse(self, current_available):
        with open(self.kconfig_path, 'w') as f:
            f.write(self.KCONFIG_TEMPLATE % ('y' if current_available else 'n'))
        with cfg._env({'KCONFIG_CONFIG': self.config_path}):
            return self.kconfiglib.Kconfig(self.kconfig_path, warn=False)

    def test_previous_option_survives_discovery_changes_and_tracks_new_selection(self):
        kc = self._parse(current_available=True)
        self.assertEqual(kc.named_choices['DEVICE_CHOICE'].selection.name,
                         'DEVICE_CURRENT')
        kc.write_config(self.config_path, save_old=False)

        with open(self.config_path) as f:
            saved = f.read()
        self.assertIn('CONFIG_PARAM_DEVICE="/dev/current" #~DEFAULT~#', saved)

        kc = self._parse(current_available=False)
        kc.load_config(self.config_path)
        self.assertEqual(kc.named_choices['DEVICE_CHOICE'].selection.name,
                         'DEVICE_PREVIOUS')
        self.assertEqual(kc.syms['PARAM_DEVICE'].str_value, '/dev/current')
        self.assertEqual(kc.syms['DEVICE_PREVIOUS'].nodes[0].prompt[0],
                         'Previous selection: /dev/current')

        kc.syms['DEVICE_OTHER'].set_value(2)
        kc.write_config(self.config_path, save_old=False)
        kc = self._parse(current_available=False)
        kc.load_config(self.config_path)
        self.assertEqual(kc.named_choices['DEVICE_CHOICE'].selection.name,
                         'DEVICE_OTHER')
        self.assertEqual(kc.syms['PARAM_DEVICE'].str_value, '/dev/other')

    def test_canbus_discovery_aggregates_interfaces_and_splits_selection(self):
        klipper_home = os.path.join(self.tmpdir.name, 'klipper')
        python_path = os.path.join(self.tmpdir.name, 'klippy-env', 'bin', 'python')
        mock_bin = os.path.join(self.tmpdir.name, 'bin')
        os.makedirs(os.path.dirname(python_path))
        os.makedirs(mock_bin)
        os.makedirs(os.path.join(klipper_home, 'scripts'))
        ip_path = os.path.join(mock_bin, 'ip')
        with open(ip_path, 'w') as f:
            f.write('''#!/bin/sh
echo "2: can0: <NOARP,UP> mtu 16 qdisc pfifo_fast state UNKNOWN mode DEFAULT group default"
echo "3: vlan-1: <NOARP,UP> mtu 16 qdisc pfifo_fast state UNKNOWN mode DEFAULT group default"
echo "4: can1: <NOARP,UP> mtu 16 qdisc pfifo_fast state UNKNOWN mode DEFAULT group default"
''')
        os.chmod(ip_path, 0o755)
        with open(python_path, 'w') as f:
            f.write('''#!/bin/sh
case "$2" in
  can0) echo "Found canbus_uuid=aaa111aaa111" ;;
  vlan-1) echo "Found canbus_uuid=bbb222bbb222" ;;
  can1) echo "Found canbus_uuid=ccc333ccc333" ;;
esac
''')
        os.chmod(python_path, 0o755)

        syms = dict(profiles.get('boxturtle').syms)
        syms['CHOICE_MMU_CONNECTION_TYPE_SERIAL'] = False
        syms['CHOICE_MMU_CONNECTION_TYPE_CANBUS'] = True
        with cfg._env({'KLIPPER_HOME': klipper_home,
                       'PATH': mock_bin + os.pathsep + os.environ['PATH']}):
            kc = cfg._kconfig('multi-interface-canbus-discovery', syms)

        choice = kc.named_choices['CHOICE_MMU_CANBUS_CONNECTION']
        prompts = [node.prompt[0] for symbol in choice.syms
                   for node in symbol.nodes if node.prompt]
        self.assertIn('can0:aaa111aaa111', prompts)
        self.assertIn('vlan-1:bbb222bbb222', prompts)
        self.assertIn('can1:ccc333ccc333', prompts)

        kc.syms['CHOICE_MMU_CANBUS_UUID_VLAN_1_BBB222BBB222'].set_value(2)
        self.assertEqual(kc.syms['PARAM_MMU_CANBUS_UUID'].str_value,
                         'bbb222bbb222')
        self.assertEqual(kc.syms['PARAM_MMU_CANBUS_INTERFACE'].str_value,
                         'vlan-1')

    def test_canbus_discovery_uses_real_klipper_home_override(self):
        install_home = os.path.join(self.tmpdir.name, 'test-install', 'klipper')
        real_home = os.path.join(self.tmpdir.name, 'real', 'klipper')
        python_path = os.path.join(self.tmpdir.name, 'real', 'klippy-env',
                                   'bin', 'python')
        mock_bin = os.path.join(self.tmpdir.name, 'bin')
        os.makedirs(install_home)
        os.makedirs(os.path.dirname(python_path))
        os.makedirs(os.path.join(real_home, 'scripts'))
        os.makedirs(mock_bin)

        query_path = os.path.join(real_home, 'scripts', 'canbus_query.py')
        with open(query_path, 'w') as f:
            f.write('# mock canbus query\n')
        ip_path = os.path.join(mock_bin, 'ip')
        with open(ip_path, 'w') as f:
            f.write('#!/bin/sh\necho "2: can0: <NOARP,UP> mtu 16"\n')
        os.chmod(ip_path, 0o755)
        with open(python_path, 'w') as f:
            f.write('#!/bin/sh\n'
                    'echo "Found canbus_uuid=abc123abc123"\n')
        os.chmod(python_path, 0o755)

        syms = dict(profiles.get('boxturtle').syms)
        syms['CHOICE_MMU_CONNECTION_TYPE_SERIAL'] = False
        syms['CHOICE_MMU_CONNECTION_TYPE_CANBUS'] = True
        with cfg._env({'KLIPPER_HOME': install_home,
                       'REAL_KLIPPER_HOME': real_home,
                       'PATH': mock_bin + os.pathsep + os.environ['PATH']}):
            kc = cfg._kconfig('real-klipper-home-canbus-discovery', syms)

        choice = kc.named_choices['CHOICE_MMU_CANBUS_CONNECTION']
        prompts = [node.prompt[0] for symbol in choice.syms
                   for node in symbol.nodes if node.prompt]
        self.assertIn('can0:abc123abc123', prompts)

    def test_failed_canbus_queries_produce_no_discovered_choices(self):
        klipper_home = os.path.join(self.tmpdir.name, 'klipper')
        python_path = os.path.join(self.tmpdir.name, 'klippy-env', 'bin', 'python')
        mock_bin = os.path.join(self.tmpdir.name, 'bin')
        os.makedirs(os.path.dirname(python_path))
        os.makedirs(mock_bin)
        os.makedirs(os.path.join(klipper_home, 'scripts'))

        ip_path = os.path.join(mock_bin, 'ip')
        with open(ip_path, 'w') as f:
            f.write('#!/bin/sh\necho "2: vlan1: <NOARP,UP> mtu 16"\n')
        os.chmod(ip_path, 0o755)
        with open(python_path, 'w') as f:
            f.write('#!/bin/sh\necho "CAN query failed" >&2\nexit 1\n')
        os.chmod(python_path, 0o755)

        syms = dict(profiles.get('boxturtle').syms)
        syms['CHOICE_MMU_CONNECTION_TYPE_SERIAL'] = False
        syms['CHOICE_MMU_CONNECTION_TYPE_CANBUS'] = True
        with cfg._env({'KLIPPER_HOME': klipper_home,
                       'PATH': mock_bin + os.pathsep + os.environ['PATH']}):
            kc = cfg._kconfig('failed-canbus-query', syms)

        choice = kc.named_choices['CHOICE_MMU_CANBUS_CONNECTION']
        discovered = [node.prompt[0] for symbol in choice.syms
                      for node in symbol.nodes
                      if node.prompt and ':' in node.prompt[0]
                      and not node.prompt[0].startswith('Previous selection:')]
        self.assertEqual(discovered, [])
        self.assertEqual(choice.selection.name,
                         'CHOICE_MMU_CANBUS_UUID_OTHER')

    def test_all_real_connection_choices_expose_their_previous_values(self):
        values = {
            'PARAM_MMU_SERIAL_DEVICE': '/dev/previous-main',
            'PARAM_MMU_CANBUS_UUID': 'abc123def456',
            'PARAM_BUFFER_SERIAL_DEVICE': '/dev/previous-buffer',
            'PARAM_BUFFER_CANBUS_UUID': 'def456abc123',
            'PARAM_BUFFER_CANBUS_INTERFACE': 'can2',
        }
        for gate in range(5):
            values['PARAM_MMU_SERIAL_DEVICE_%d' % gate] = \
                '/dev/previous-gate-%d' % gate
            values['PARAM_MMU_CANBUS_UUID_%d' % gate] = \
                'abc123def4%02d' % gate
            values['PARAM_MMU_CANBUS_INTERFACE_%d' % gate] = \
                'can%d' % (gate % 3)
        with open(self.config_path, 'w') as f:
            for name, value in values.items():
                f.write('CONFIG_%s="%s" #~DEFAULT~#\n' % (name, value))

        env = {
            'KCONFIG_CONFIG': self.config_path,
            'F_PER_GATE_MCU': 'y',
            'UNIT_NAME': 'unit0',
            'MCU_NAME': 'unit0',
            'UNIT_INDEX': '0',
            'F_MULTI_UNIT': '',
            'F_MULTI_UNIT_ENTRY_POINT': '',
        }
        emu_syms = dict(profiles.get('emu').syms)
        emu_syms['CHOICE_MMU_CONNECTION_TYPE_SERIAL'] = True
        # Select the saved entries explicitly.  Board Kconfigs can prefer a currently
        # discovered MCU, and letting /dev/serial/by-id leak into this fixture makes the
        # result depend on whether the test happens to run on a live printer.
        for gate in range(5):
            emu_syms['CHOICE_MMU_SERIAL_DEVICE_PREVIOUS_%d' % gate] = True
        with cfg._env(env):
            kc = cfg._kconfig('previous-connection-selections', emu_syms)

        for gate in range(5):
            choice = kc.named_choices['CHOICE_MMU_SERIAL_CONNECTION_%d' % gate]
            self.assertEqual(choice.selection.name,
                             'CHOICE_MMU_SERIAL_DEVICE_PREVIOUS_%d' % gate)
            name = 'PARAM_MMU_SERIAL_DEVICE_%d' % gate
            self.assertEqual(kc.syms[name].str_value, values[name])
            canbus = kc.syms['CHOICE_MMU_CANBUS_UUID_PREVIOUS_%d' % gate]
            self.assertEqual(canbus.nodes[0].prompt[0],
                             'Previous selection: %s:%s' % (
                                 values['PARAM_MMU_CANBUS_INTERFACE_%d' % gate],
                                 values['PARAM_MMU_CANBUS_UUID_%d' % gate]))

        emu_canbus_syms = dict(profiles.get('emu').syms)
        emu_canbus_syms['CHOICE_MMU_CONNECTION_TYPE_SERIAL'] = False
        emu_canbus_syms['CHOICE_MMU_CONNECTION_TYPE_CANBUS'] = True
        with cfg._env(env):
            kc = cfg._kconfig('previous-canbus-gate-selections',
                              emu_canbus_syms)
        for gate in range(5):
            choice = kc.named_choices['CHOICE_MMU_CANBUS_CONNECTION_%d' % gate]
            self.assertEqual(choice.selection.name,
                             'CHOICE_MMU_CANBUS_UUID_PREVIOUS_%d' % gate)
            interface = 'PARAM_MMU_CANBUS_INTERFACE_%d' % gate
            self.assertEqual(kc.syms[interface].str_value, values[interface])

        vvd = profiles.get('ercf_vvd').units[1]
        buffer_env = {
            'KCONFIG_CONFIG': self.config_path,
            'F_PER_GATE_MCU': '',
            'UNIT_NAME': vvd.name,
            'MCU_NAME': vvd.mcu_name,
            'UNIT_INDEX': str(vvd.index),
            'F_MULTI_UNIT': 'y',
            'F_MULTI_UNIT_ENTRY_POINT': '',
        }
        buffer_syms = dict(vvd.syms)
        buffer_syms['CHOICE_BUFFER_CONNECTION_TYPE_SERIAL'] = True
        buffer_syms['CHOICE_BUFFER_SERIAL_DEVICE_PREVIOUS'] = True
        with cfg._env(buffer_env):
            kc = cfg._kconfig('previous-buffer-selection', buffer_syms)

        buffer_choice = kc.named_choices['CHOICE_BUFFER_SERIAL_CONNECTION']
        self.assertEqual(buffer_choice.selection.name,
                         'CHOICE_BUFFER_SERIAL_DEVICE_PREVIOUS')
        self.assertEqual(kc.syms['PARAM_BUFFER_SERIAL_DEVICE'].str_value,
                         values['PARAM_BUFFER_SERIAL_DEVICE'])
        buffer_canbus = kc.syms['CHOICE_BUFFER_CANBUS_UUID_PREVIOUS']
        self.assertEqual(buffer_canbus.nodes[0].prompt[0],
                         'Previous selection: %s:%s' % (
                             values['PARAM_BUFFER_CANBUS_INTERFACE'],
                             values['PARAM_BUFFER_CANBUS_UUID']))

        buffer_canbus_syms = dict(vvd.syms)
        buffer_canbus_syms['CHOICE_BUFFER_CONNECTION_TYPE_SERIAL'] = False
        buffer_canbus_syms['CHOICE_BUFFER_CONNECTION_TYPE_CANBUS'] = True
        with cfg._env(buffer_env):
            kc = cfg._kconfig('previous-buffer-canbus-selection',
                              buffer_canbus_syms)
        self.assertEqual(kc.syms['PARAM_BUFFER_CANBUS_INTERFACE'].str_value,
                         values['PARAM_BUFFER_CANBUS_INTERFACE'])

        base_env = dict(buffer_env)
        base_env.update({
            'UNIT_NAME': 'unit0',
            'MCU_NAME': 'unit0',
            'UNIT_INDEX': '0',
            'F_MULTI_UNIT': '',
        })
        base_syms = dict(profiles.get('boxturtle').syms)
        base_syms['CHOICE_MMU_CONNECTION_TYPE_CANBUS'] = True
        base_syms['CHOICE_MMU_CANBUS_UUID_PREVIOUS'] = True
        with cfg._env(base_env):
            kc = cfg._kconfig('previous-base-canbus-selection', base_syms)

        base_choice = kc.named_choices['CHOICE_MMU_CANBUS_CONNECTION']
        self.assertEqual(base_choice.selection.name,
                         'CHOICE_MMU_CANBUS_UUID_PREVIOUS')
        self.assertEqual(kc.syms['PARAM_MMU_CANBUS_UUID'].str_value,
                         values['PARAM_MMU_CANBUS_UUID'])
        self.assertEqual(kc.syms['PARAM_MMU_CANBUS_INTERFACE'].str_value,
                         'can0')
        self.assertEqual(base_choice.selection.nodes[0].prompt[0],
                         'Previous selection: can0:%s' %
                         values['PARAM_MMU_CANBUS_UUID'])
        base_serial = kc.syms['CHOICE_MMU_SERIAL_DEVICE_PREVIOUS']
        self.assertEqual(base_serial.nodes[0].prompt[0],
                         'Previous selection: %s' %
                         values['PARAM_MMU_SERIAL_DEVICE'])


if __name__ == '__main__':
    unittest.main()
