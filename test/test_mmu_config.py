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
# Requires jinja2 + dill (installer/requirements.txt) - run with the repo venv:
#   ./venv/bin/python -m unittest test.test_mmu_config
#
# This file may be distributed under the terms of the GNU GPLv3 license.

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


if __name__ == '__main__':
    unittest.main()
