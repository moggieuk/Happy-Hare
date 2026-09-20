# TD-1 Kconfig / template installer tests.
#
# TD-1 is the one optional feature with no Klipper hardware behind it: the scanner is a
# USB device owned by Moonraker, so the installer collects a serial number and a capture
# policy, and nothing else. There is deliberately no scan geometry - a reading comes from
# filament travelling its normal path past the scanner. These tests pin that split down:
# serials land on [mmu_unit], policy lands on [mmu_unit_parameters], and a machine with
# the feature switched off gets neither.
#
# Single file run:
#   make test UT='test_td1.py' JOBS=1

import unittest

from test.hh import cfg, profiles


SHARED = {
    "MMU_HAS_TD1": True,
    "PARAM_TD1_DEVICE": "TD1-0042",
}

PER_GATE = dict(SHARED, **{
    "MMU_HAS_PER_GATE_TD1": True,
    "PARAM_TD1_DEVICE": "",
    "PARAM_TD1_DEVICE_0": "TD1-0042",
    "PARAM_TD1_DEVICE_1": "TD1-0043",
    "PARAM_TD1_DEVICE_GATE_2": False,
    "PARAM_TD1_DEVICE_2": "",
    "PARAM_TD1_DEVICE_3": "TD1-0042",
})


def kconfig(label, syms):
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        return cfg._kconfig(label, syms)


def assembled(label, syms, base="boxturtle"):
    return cfg.assemble(cfg.render(profiles.get(base).derive(label, syms=syms)))


class TestTd1Disabled(unittest.TestCase):
    """The feature is opt-in: off must cost nothing and configure nothing."""

    def test_feature_defaults_off(self):
        kc = kconfig("td1_default", {})
        self.assertFalse(kc.is_enabled("MMU_HAS_TD1"))
        self.assertFalse(kc.is_enabled("MMU_HAS_PER_GATE_TD1"))
        self.assertFalse(kc.is_enabled("BOOL_TD1_ADVANCED"))

    def test_nothing_is_rendered_when_disabled(self):
        rendered = cfg.render(profiles.get("boxturtle"))
        hardware = rendered["config/base/mmu_hardware.cfg"]
        parameters = rendered["config/base/mmu_parameters.cfg"]
        self.assertNotIn("td1_device", hardware)
        self.assertNotIn("td1_devices", hardware)
        for key in ("td1_capture_timeout", "td1_auto_update", "td1_capture_on_load"):
            self.assertNotIn(key, parameters)

    def test_policies_default_off_when_the_feature_is_on(self):
        # Enabling TD-1 must not start moving filament or writing gate metadata
        params = assembled("td1_policies", SHARED)["mmu_unit_parameters unit0"]
        self.assertEqual(params.get("td1_auto_update"), "0")
        self.assertEqual(params.get("td1_capture_on_load"), "0")


class TestTd1SharedScanner(unittest.TestCase):
    def test_serial_on_unit_geometry_on_parameters(self):
        rendered = assembled("td1_shared_split", SHARED)
        unit = rendered["mmu_unit unit0"]
        params = rendered["mmu_unit_parameters unit0"]
        self.assertEqual(unit.get("td1_device"), "TD1-0042")
        self.assertNotIn("td1_devices", unit)
        # Tunables belong with every other unit tunable, not on the hardware section
        self.assertNotIn("td1_auto_update", unit)
        self.assertEqual(params.get("td1_auto_update"), "0")
        self.assertEqual(params.get("td1_capture_timeout"), "5")

    def test_hidden_advanced_option_still_renders_its_default(self):
        # The advanced PROMPT is conditional; the symbol is not. If the symbol were
        # hidden instead, the key would render empty and fail to parse at boot
        params = assembled("td1_defaults", SHARED)["mmu_unit_parameters unit0"]
        self.assertEqual(params.get("td1_capture_timeout"), "5")

    def test_no_scan_geometry_is_rendered_at_all(self):
        # Measurement is by traversal, so there is nothing positional to configure
        params = assembled("td1_nogeom", SHARED)["mmu_unit_parameters unit0"]
        for key in ("td1_scan_distance", "td1_scan_distances", "td1_scan_max",
                    "td1_scan_maxes", "td1_scan_speed", "td1_scan_step",
                    "td1_capture_on_preload"):
            self.assertNotIn(key, params)


class TestTd1PerGateScanners(unittest.TestCase):
    def test_list_follows_local_gate_order_with_a_placeholder(self):
        rendered = assembled("td1_per_gate_render", PER_GATE)
        unit = rendered["mmu_unit unit0"]
        self.assertEqual(unit.get("td1_devices"), "TD1-0042, TD1-0043, -, TD1-0042")
        self.assertNotIn("td1_device", unit)

    def test_a_repeated_serial_is_one_shared_scanner(self):
        # Gates 0 and 3 name the same device on purpose - that is how a scanner is
        # shared, and it must not be mistaken for a duplicate definition
        unit = assembled("td1_repeat", PER_GATE)["mmu_unit unit0"]
        serials = [s.strip() for s in unit.get("td1_devices").split(",")]
        self.assertEqual(serials[0], serials[3])
        self.assertEqual(len(serials), 4)

    def test_shared_and_per_gate_are_mutually_exclusive_in_the_template(self):
        for label, syms in (("td1_x_shared", SHARED), ("td1_x_per_gate", PER_GATE)):
            with self.subTest(label=label):
                unit = assembled(label, syms)["mmu_unit unit0"]
                self.assertEqual(
                    sum(key in unit for key in ("td1_device", "td1_devices")), 1)


class TestTd1Advanced(unittest.TestCase):
    ADVANCED = dict(PER_GATE, **{
        "BOOL_TD1_ADVANCED": True,
        "PARAM_TD1_CAPTURE_TIMEOUT": "8",
        "PARAM_TD1_AUTO_UPDATE": True,
        "PARAM_TD1_CAPTURE_ON_LOAD": True,
    })

    def test_every_advanced_value_reaches_the_configuration(self):
        params = assembled("td1_advanced_render", self.ADVANCED)["mmu_unit_parameters unit0"]
        self.assertEqual(params.get("td1_capture_timeout"), "8")

    def test_booleans_render_as_integers(self):
        # boolint, not bool: the runtime parses these as 0/1 ints like every other
        # boolean unit parameter
        params = assembled("td1_bools", self.ADVANCED)["mmu_unit_parameters unit0"]
        for key in ("td1_auto_update", "td1_capture_on_load"):
            with self.subTest(key=key):
                self.assertEqual(params.get(key), "1")

    def test_rendered_tree_is_sane(self):
        rendered = cfg.render(
            profiles.get("boxturtle").derive("td1_sane", syms=self.ADVANCED))
        cfg.assert_sane(rendered)


class TestTd1MultiUnit(unittest.TestCase):
    def test_a_serial_may_be_reused_across_units(self):
        # One physical scanner serving two units is legal and needs no second definition
        profile = profiles.clone_across_units(
            "td1_two_units", profiles.get("boxturtle").derive("td1_base", syms=SHARED),
            ["unit0", "unit1"])
        rendered = cfg.assemble(cfg.render(profile))
        self.assertEqual(rendered["mmu_unit unit0"].get("td1_device"), "TD1-0042")
        self.assertEqual(rendered["mmu_unit unit1"].get("td1_device"), "TD1-0042")


class TestTd1InvalidInput(unittest.TestCase):
    """Bad geometry must be refused at load, not silently used to drive filament."""

    def test_non_positive_timeout_is_rejected(self):
        from test.hh import session
        profile = profiles.get("boxturtle").derive("td1_zero_timeout", syms=dict(
            SHARED, BOOL_TD1_ADVANCED=True, PARAM_TD1_CAPTURE_TIMEOUT="0"))
        with self.assertRaisesRegex(Exception, "td1_capture_timeout"):
            with session(profile):
                pass

    def test_missing_shared_serial_is_rejected(self):
        from test.hh import session
        profile = profiles.get("boxturtle").derive("td1_blank", syms=dict(
            SHARED, PARAM_TD1_DEVICE=""))
        with self.assertRaisesRegex(Exception, "requires a Moonraker TD-1"):
            with session(profile):
                pass


class TestTd1Regeneration(unittest.TestCase):
    def test_no_runtime_state_is_written_by_the_installer(self):
        # Measurements and their provenance live in mmu_vars.cfg, which the installer
        # does not generate. If either appeared in a template, re-running setup would
        # silently overwrite what the machine measured
        rendered = cfg.render(
            profiles.get("boxturtle").derive("td1_rerun", syms=SHARED))
        for name, text in rendered.items():
            with self.subTest(name=name):
                self.assertNotIn("mmu_state_td1_records", text)
                self.assertNotIn("mmu_state_gate_td", text)

    def test_regeneration_is_deterministic(self):
        profile = profiles.get("boxturtle").derive("td1_twice", syms=PER_GATE)
        self.assertEqual(cfg.render(profile), cfg.render(profile))


if __name__ == "__main__":
    unittest.main()
