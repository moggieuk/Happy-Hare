# QIDI Plus4 toolhead preset: Kconfig defaults and rendering.
#
# Only what a booted machine can't show lives here - which options the preset implies
# and what lands in the rendered files. Behaviour on a booted Plus4 is in
# test/test_mmu_qidi_plus4.py.

from pathlib import Path
import tempfile
import unittest

from test.hh import cfg, profiles


def _parse(label, syms):
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        return cfg._kconfig(label, syms)


PLUS4_ON_BOX = {"MMU_TYPE_QIDI_BOX_1_0": True, "TOOLHEAD_TYPE_QIDI_PLUS4": True}


class TestQidiPlus4Toolhead(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.profile = profiles.Profile("qidi_plus4_defaults", syms=PLUS4_ON_BOX)
        cls.kconfig = _parse(cls.profile.name, cls.profile.syms)
        cls.rendered = cfg.render(cls.profile)
        cls.machine = cls.rendered["config/base/mmu.cfg"]

    def test_hall_sensor_is_the_extruder_entry_sensor(self):
        for expected in (
            "hall_sensor_endstop         : extruder",
            "hall_adc1                   : PA2",
            "hall_adc2                   : PA3",
        ):
            with self.subTest(config=expected):
                self.assertIn(expected, self.machine)

    def test_the_q_series_extruder_switch_is_not_rendered(self):
        self.assertIn("extruder_switch_pin         : \n", self.machine)
        self.assertNotIn("!THR:", self.machine)

    def test_implies_a_cutter_before_the_extruder(self):
        self.assertTrue(self.kconfig.is_enabled("MMU_HAS_TOOLHEAD_CUTTER"))
        self.assertEqual(self.kconfig.get("VAR_CUT_TIP_CUTTER_BEFORE_EXTRUDER"), "True")

    def test_plus4_box_reads_pb0_as_its_motion_sensor(self):
        # The Plus4 hub has a filament motion sensor on PB0, not the Q2/Max4 buffer
        self.assertFalse(self.kconfig.is_enabled("BOOL_QIDI_BOX_BUFFER"))
        self.assertTrue(self.kconfig.is_enabled("MMU_HAS_ENCODER"))
        self.assertFalse(self.kconfig.is_enabled("MMU_HAS_SYNC_FEEDBACK_BUFFER"))

    def test_hub_buffer_can_still_be_chosen(self):
        kconfig = _parse("qidi_plus4_buffer_hub", dict(PLUS4_ON_BOX, BOOL_QIDI_BOX_BUFFER=True))
        self.assertTrue(kconfig.is_enabled("MMU_HAS_SYNC_FEEDBACK_BUFFER"))
        self.assertFalse(kconfig.is_enabled("MMU_HAS_ENCODER"))

    def test_cutter_position_can_be_overridden(self):
        kconfig = _parse("qidi_plus4_cutter_after", dict(
            PLUS4_ON_BOX, BOOL_CUT_TIP_CUTTER_BEFORE_EXTRUDER=False))
        self.assertEqual(kconfig.get("VAR_CUT_TIP_CUTTER_BEFORE_EXTRUDER"), "False")

    def test_cutter_and_hall_sensor_can_be_disabled(self):
        kconfig = _parse("qidi_plus4_bare", dict(
            PLUS4_ON_BOX, MMU_HAS_TOOLHEAD_CUTTER=False, MMU_HAS_HALL_EXTRUDER_SENSOR=False))
        self.assertFalse(kconfig.is_enabled("MMU_HAS_TOOLHEAD_CUTTER"))
        self.assertFalse(kconfig.is_enabled("MMU_HAS_HALL_EXTRUDER_SENSOR"))

    def test_hall_sensor_does_not_need_an_extruder_switch_pin(self):
        # On any MMU: the hall sensor replaces the switch, so an empty switch pin is expected
        kconfig = _parse("boxturtle_plus4", {"MMU_TYPE_BOX_TURTLE_1_0": True,
                                             "TOOLHEAD_TYPE_QIDI_PLUS4": True})
        self.assertTrue(kconfig.is_enabled("MMU_HAS_HALL_EXTRUDER_SENSOR"))
        self.assertEqual(kconfig.get("PIN_EXTRUDER_SENSOR"), "")
        self.assertFalse(kconfig.is_enabled("SW3"))

    def test_hall_sensor_without_its_pins_is_a_warning(self):
        self.assertFalse(self.kconfig.is_enabled("SW9"))
        kconfig = _parse("boxturtle_hall_no_pins", {"MMU_TYPE_BOX_TURTLE_1_0": True,
                                                    "MMU_HAS_SENSOR_EXTRUDER": True,
                                                    "MMU_HAS_HALL_EXTRUDER_SENSOR": True})
        self.assertTrue(kconfig.is_enabled("SW9"))
        self.assertTrue(kconfig.is_enabled("SHOW_SHARED_WARNINGS"))

    def test_sourced_values(self):
        # Cutter: QIDI's stock box_macros.cfg. Distances: qidi-community/plus4-cad and
        # Bunny-Box's Plus4 config, which agree to within about 2mm
        expected = {
            "PARAM_TOOLHEAD_EXTRUDER_TO_NOZZLE": "72",
            "VAR_CUT_TIP_BLADE_POS": "92",
            "VAR_CUT_TIP_PIN_LOC_XY": "304, 20",
            "VAR_CUT_TIP_PIN_LOC_COMPRESSED_XY": "304, 4.3",
            "VAR_CUT_TIP_ITERATIONS": "2",
            "VAR_CUT_TIP_CUT_DWELL_TIME": "500",
        }
        for symbol, value in expected.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(self.kconfig.get(symbol), value)

    def test_pushback_clears_the_gears(self):
        # v4 needs the pushback to carry the fragment 3mm past the gears, within the retract
        blade = float(self.kconfig.get("VAR_CUT_TIP_BLADE_POS"))
        gears = float(self.kconfig.get("PARAM_TOOLHEAD_EXTRUDER_TO_NOZZLE"))
        pushback = float(self.kconfig.get("VAR_CUT_TIP_PUSHBACK_LENGTH"))
        retract = float(self.kconfig.get("VAR_CUT_TIP_RETRACT_LENGTH"))
        residual = float(self.kconfig.get("PARAM_TOOLHEAD_RESIDUAL_FILAMENT"))
        self.assertGreaterEqual(pushback, blade - gears + 3.)
        self.assertGreaterEqual(retract - residual, pushback)

    def test_measured_entry_sensor_sits_above_the_blade(self):
        # Measured on a Plus4: the hall sensor is ~24mm above the gears, above the blade.
        # The cut check needs entry-to-gears above blade-to-gears to look for the sensor
        # above the cut, and 22 keeps the gear-only push short of the gears
        blade = float(self.kconfig.get("VAR_CUT_TIP_BLADE_POS"))
        gears = float(self.kconfig.get("PARAM_TOOLHEAD_EXTRUDER_TO_NOZZLE"))
        entry = float(self.kconfig.get("PARAM_TOOLHEAD_ENTRY_TO_EXTRUDER"))
        self.assertEqual(entry, 22.)
        self.assertGreater(entry, blade - gears)
        self.assertIn("toolhead_entry_to_extruder  : 22", self.machine)



class TestQidiPlus4HubDefaults(unittest.TestCase):

    def _multi(self, toolhead='TOOLHEAD_TYPE_QIDI_PLUS4', buffer=None):
        syms = {'MMU_TYPE_QIDI_BOX_1_0': True}
        if buffer is not None:
            syms['BOOL_QIDI_BOX_BUFFER'] = buffer
        return profiles.Profile('qidi_hub_defaults', syms={toolhead: True}, units=[
            profiles.UnitProfile('unit0', syms),
            profiles.UnitProfile('unit1', syms, index=1)])

    def _assert_hubs(self, rendered, buffer):
        for unit in ('unit0', 'unit1'):
            with self.subTest(unit=unit):
                hardware = rendered['config/base/mmu_hardware_%s.cfg' % unit]
                parsed = cfg.assemble({unit: hardware})
                self.assertEqual(parsed.has_section('mmu_buffer ' + unit), buffer)
                self.assertEqual(parsed.has_section('mmu_encoder ' + unit), not buffer)
                section = ('mmu_buffer ' if buffer else 'mmu_encoder ') + unit
                pin = 'tension_pin' if buffer else 'encoder_pin'
                self.assertEqual(parsed.get(section, pin), '^%s:PB0' % unit)

    def test_multi_unit_plus4_defaults_each_box_to_motion_sensor(self):
        # The toolhead is chosen in the parent, not in the per-unit parses.
        self._assert_hubs(cfg.render_via_files(self._multi()), buffer=False)

    def test_multi_unit_hub_overrides_survive_saved_config_render(self):
        for toolhead, buffer in [('TOOLHEAD_TYPE_QIDI_PLUS4', True),
                                 ('TOOLHEAD_TYPE_QIDI_Q2', False)]:
            with self.subTest(toolhead=toolhead):
                self._assert_hubs(cfg.render_via_files(self._multi(toolhead, buffer)), buffer)

    def test_parent_toolhead_changes_invalidate_cached_unit_parses(self):
        # Reuse the same unit configuration with a different parent selection.
        for toolhead, buffer in [('TOOLHEAD_TYPE_QIDI_PLUS4', False),
                                 ('TOOLHEAD_TYPE_QIDI_Q2', True),
                                 ('TOOLHEAD_TYPE_QIDI_PLUS4', False)]:
            self._assert_hubs(cfg.render(self._multi(toolhead)), buffer)

    def test_saved_default_follows_a_later_toolhead_selection(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV), tempfile.NamedTemporaryFile() as saved:
            kc = cfg._kconfig('qidi_saved_hub', {'MMU_TYPE_QIDI_BOX_1_0': True})
            kc.write_config(saved.name)
            with open(saved.name) as handle:
                self.assertIn('CONFIG_BOOL_QIDI_BOX_BUFFER=y #~DEFAULT~#', handle.read())
            kc.load_config(saved.name, filter_defaults=True)
            kc.syms['TOOLHEAD_TYPE_QIDI_PLUS4'].set_value(2)
            self.assertFalse(kc.is_enabled('BOOL_QIDI_BOX_BUFFER'))
            self.assertTrue(kc.is_enabled('MMU_HAS_ENCODER'))
            kc.write_config(saved.name)
            kc.load_config(saved.name, filter_defaults=True)
            kc.syms['TOOLHEAD_TYPE_QIDI_Q2'].set_value(2)
            self.assertTrue(kc.is_enabled('BOOL_QIDI_BOX_BUFFER'))
            self.assertFalse(kc.is_enabled('MMU_HAS_ENCODER'))

    def test_explicit_saved_hub_choices_still_override_toolhead_defaults(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV), tempfile.NamedTemporaryFile() as saved:
            for buffer, toolhead in [(True, 'TOOLHEAD_TYPE_QIDI_PLUS4'),
                                     (False, 'TOOLHEAD_TYPE_QIDI_Q2')]:
                with self.subTest(buffer=buffer):
                    kc = cfg._kconfig('qidi_explicit_hub', dict(
                        PLUS4_ON_BOX, BOOL_QIDI_BOX_BUFFER=buffer))
                    kc.write_config(saved.name)
                    kc.load_config(saved.name, filter_defaults=True)
                    kc.syms[toolhead].set_value(2)
                    self.assertEqual(kc.is_enabled('BOOL_QIDI_BOX_BUFFER'), buffer)

    def test_legacy_hub_choices_migrate_without_changing_hardware(self):
        # Old OPTION_ saves do not distinguish an accepted default from an explicit
        # choice. Preserve both y and n; do not guess and change installed hardware.
        with cfg._env(cfg._SINGLE_UNIT_ENV), tempfile.TemporaryDirectory() as tmp:
            saved = Path(tmp) / '.mmu_config'
            for buffer in (True, False):
                for filter_defaults in (True, False):
                    with self.subTest(buffer=buffer, filter_defaults=filter_defaults):
                        saved.write_text('CONFIG_MMU_TYPE_QIDI_BOX_1_0=y\n'
                                         'CONFIG_TOOLHEAD_TYPE_QIDI_PLUS4=y\n' +
                                         ('CONFIG_OPTION_QIDI_BOX_BUFFER=y\n' if buffer else
                                          '# CONFIG_OPTION_QIDI_BOX_BUFFER is not set\n'))
                        kc = cfg._new_kconfig('qidi_legacy_hub')
                        kc.load_config(str(saved), filter_defaults=filter_defaults)
                        self.assertEqual(kc.is_enabled('BOOL_QIDI_BOX_BUFFER'), buffer)
                        self.assertEqual(kc.is_enabled('MMU_HAS_SYNC_FEEDBACK_BUFFER'), buffer)
                        self.assertEqual(kc.is_enabled('MMU_HAS_ENCODER'), not buffer)
                        kc.write_config(str(saved))
                        kc.load_config(str(saved), filter_defaults=True)
                        self.assertEqual(kc.is_enabled('BOOL_QIDI_BOX_BUFFER'), buffer)


if __name__ == '__main__':
    unittest.main()
