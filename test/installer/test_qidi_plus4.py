# QIDI Plus4 toolhead preset: Kconfig defaults and rendering.
#
# Only what a booted machine can't show lives here - which options the preset implies
# and what lands in the rendered files. Behaviour on a booted Plus4 is in
# test/test_mmu_qidi_plus4.py.

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
        self.assertFalse(self.kconfig.is_enabled("OPTION_QIDI_BOX_BUFFER"))
        self.assertTrue(self.kconfig.is_enabled("MMU_HAS_ENCODER"))
        self.assertFalse(self.kconfig.is_enabled("MMU_HAS_SYNC_FEEDBACK_BUFFER"))

    def test_hub_buffer_can_still_be_chosen(self):
        kconfig = _parse("qidi_plus4_buffer_hub", dict(PLUS4_ON_BOX, OPTION_QIDI_BOX_BUFFER=True))
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

    def test_placeholder_values_are_marked_unmeasured_where_they_render(self):
        # Until a commissioned Plus4 is measured, nobody reading the rendered config may
        # mistake these for calibration
        macro_vars = self.rendered["config/base/mmu_macro_vars.cfg"]
        for text, values in ((self.machine, ("toolhead_extruder_to_nozzle", "hall_raw_dia1")),
                             (macro_vars, ("blade_pos", "pin_loc_xy"))):
            for value in values:
                with self.subTest(value=value):
                    self.assertRegex(text, r"(?s)# UNMEASURED:.*%s" % value)

    def test_other_toolheads_carry_no_unmeasured_marker(self):
        rendered = cfg.render(profiles.Profile("qidi_q2_defaults",
                                               syms={"MMU_TYPE_QIDI_BOX_1_0": True}))
        for name, text in rendered.items():
            with self.subTest(file=name):
                self.assertNotIn("UNMEASURED", text)


if __name__ == "__main__":
    unittest.main()
