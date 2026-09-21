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


# One scanner in the shared bowden: in the filament path, so it renders as one
# td1_devices entry per gate. 'td1_device' is the OFF-path one - see OFFPATH
SHARED = {
    "MMU_HAS_TD1": True,
    "PARAM_TD1_BOWDEN_DEVICE": "TD1-0042",
}

OFFPATH = {
    "MMU_HAS_TD1": True,
    "BOOL_TD1_OFFPATH": True,
    "PARAM_TD1_DEVICE": "TD1-0099",
}

PER_GATE = dict(SHARED, **{
    "MMU_HAS_PER_GATE_TD1": True,
    "PARAM_TD1_BOWDEN_DEVICE": "",
    "PARAM_TD1_DEVICE_0": "TD1-0042",
    "PARAM_TD1_DEVICE_1": "TD1-0043",
    # Gate 2's scanner is switched off but its serial is left behind on purpose:
    # kconfig keeps the value of a symbol whose prompt is hidden, so this is the state
    # a user who types a serial and then changes their mind actually leaves
    "PARAM_TD1_DEVICE_GATE_2": False,
    "PARAM_TD1_DEVICE_2": "TD1-0099",
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
        # A bowden scanner is IN the filament path, so it lands as one entry per gate
        self.assertEqual(unit.get("td1_devices"), "TD1-0042, TD1-0042, TD1-0042, TD1-0042")
        self.assertNotIn("td1_device", unit)
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
        self.assertEqual(unit.get("td1_devices"), "TD1-0042, TD1-0043, , TD1-0042")
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


class TestTd1OffPathScanner(unittest.TestCase):
    """
    'td1_device' is the scanner filament does NOT pass through.

    The same split as nfc_reader / nfc_readers, and for the same reason: an off-path
    scanner serves no gate, so it gets no gate assignment. Its readings are staged as
    pending for the next gate preloaded.
    """

    def test_it_lands_as_td1_device_with_no_gate_assignment(self):
        unit = assembled("td1_offpath", OFFPATH)["mmu_unit unit0"]
        self.assertEqual(unit.get("td1_device"), "TD1-0099")
        self.assertNotIn("td1_devices", unit)

    def test_the_two_topologies_are_independent(self):
        # Not alternatives. A machine may have a bowden scanner AND a bench one
        unit = assembled("td1_both_kinds", dict(SHARED, **OFFPATH))["mmu_unit unit0"]
        self.assertEqual(unit.get("td1_device"), "TD1-0099")
        self.assertEqual(unit.get("td1_devices"), "TD1-0042, TD1-0042, TD1-0042, TD1-0042")

    def test_per_gate_and_off_path_together(self):
        unit = assembled("td1_pergate_offpath", dict(PER_GATE, **OFFPATH))["mmu_unit unit0"]
        self.assertEqual(unit.get("td1_device"), "TD1-0099")
        self.assertEqual(unit.get("td1_devices"), "TD1-0042, TD1-0043, , TD1-0042")

    def test_an_off_path_scanner_serves_no_gate_at_runtime(self):
        from test.hh import session
        profile = profiles.get("boxturtle").derive("td1_offpath_boot", syms=OFFPATH)
        with session(profile) as hh:
            hh.boot(calibrate=True)
            self.assertEqual(hh.errors, [])
            mgr = hh.mmu.mmu_unit(0).td1_manager
            self.assertEqual(mgr.shared_device.serial, "TD1-0099")
            self.assertEqual([mgr.serial_for(g) for g in range(4)], [""] * 4)
            # ...so it costs no bowden traverse
            self.assertFalse(any(mgr.needs_measurement(g) for g in range(4)))


class TestTd1MultiUnit(unittest.TestCase):
    def test_a_serial_may_be_reused_across_units(self):
        # One physical scanner serving two units is legal and needs no second definition
        profile = profiles.clone_across_units(
            "td1_two_units", profiles.get("boxturtle").derive("td1_base", syms=SHARED),
            ["unit0", "unit1"])
        rendered = cfg.assemble(cfg.render(profile))
        for unit in ("mmu_unit unit0", "mmu_unit unit1"):
            self.assertEqual(rendered[unit].get("td1_devices"),
                             "TD1-0042, TD1-0042, TD1-0042, TD1-0042")


class TestTd1InvalidInput(unittest.TestCase):
    """Bad geometry must be refused at load, not silently used to drive filament."""

    def test_non_positive_timeout_is_rejected(self):
        from test.hh import session
        profile = profiles.get("boxturtle").derive("td1_zero_timeout", syms=dict(
            SHARED, BOOL_TD1_ADVANCED=True, PARAM_TD1_CAPTURE_TIMEOUT="0"))
        with self.assertRaisesRegex(Exception, "td1_capture_timeout"):
            with session(profile):
                pass

    def test_the_feature_enabled_with_no_serial_renders_no_assignment(self):
        # Ticking "Has TD-1 scanner(s)?" and entering nothing used to render an empty
        # 'td1_device:', which klipper rejects - a printer that will not start because
        # of a box the user ticked. The line is omitted instead, so the feature is
        # simply inert until a serial is given. mmu_unit still rejects a blank value in
        # a hand-written mmu_hardware.cfg
        from test.hh import session
        profile = profiles.get("boxturtle").derive("td1_blank", syms=dict(
            SHARED, PARAM_TD1_BOWDEN_DEVICE=""))
        unit = cfg.assemble(cfg.render(profile))["mmu_unit unit0"]
        self.assertNotIn("td1_device", unit)
        self.assertNotIn("td1_devices", unit)
        with session(profile) as hh:
            hh.boot(calibrate=True)
            self.assertEqual(hh.errors, [])
            mgr = hh.mmu.mmu_unit(0).td1_manager
            self.assertEqual([mgr.serial_for(g) for g in range(4)], [""] * 4)

    def test_a_switched_off_gate_renders_no_serial_even_if_one_was_typed(self):
        # kconfig keeps the value behind a hidden prompt, and build.py reads the raw
        # user value regardless of visibility - so the template, not the symbol, has to
        # honour the decision to switch the gate's scanner off
        unit = assembled("td1_stale_gate", PER_GATE)["mmu_unit unit0"]
        self.assertNotIn("TD1-0099", unit.get("td1_devices"))
        self.assertEqual(unit.get("td1_devices"), "TD1-0042, TD1-0043, , TD1-0042")

    def test_a_switched_off_gate_has_no_scanner_at_runtime(self):
        from test.hh import session
        profile = profiles.get("boxturtle").derive("td1_stale_boot", syms=PER_GATE)
        with session(profile) as hh:
            hh.boot(calibrate=True)
            self.assertEqual(hh.errors, [])
            mgr = hh.mmu.mmu_unit(0).td1_manager
            self.assertEqual([mgr.serial_for(g) for g in range(4)],
                             ["TD1-0042", "TD1-0043", "", "TD1-0042"])
            self.assertFalse(mgr.needs_measurement(2),
                             "gate 2 has no scanner, so it must not cost a traverse")


class TestTd1BlankEntries(unittest.TestCase):
    """
    A gate with no scanner is a blank entry, as in 'nfc_readers' - not a sentinel.

    Blanks survive klipper's getlist: for a flat list the empty-filtering branch is
    only taken for nested lists, so 'A, B, , A' parses to four elements. The position
    of the blank is what makes this worth pinning - a trailing one leaves the rendered
    line ending in a comma, and it is only the comma surviving |trim that keeps the
    list the right length.
    """

    BASE = {"MMU_HAS_TD1": True, "MMU_HAS_PER_GATE_TD1": True}

    def rendered(self, label, syms):
        from test.hh import cfg as _cfg
        unit = _cfg.assemble(_cfg.render(
            profiles.get("boxturtle_test").derive(label, syms=dict(self.BASE, **syms))
        ))["mmu_unit unit0"]
        return unit.get("td1_devices")

    def booted(self, label, syms):
        from test.hh import session
        profile = profiles.get("boxturtle_test").derive(label, syms=dict(self.BASE, **syms))
        with session(profile) as hh:
            hh.boot(calibrate=True)
            self.assertEqual(hh.errors, [])
            mgr = hh.mmu.mmu_unit(0).td1_manager
            return [mgr.serial_for(g) for g in range(4)]

    def test_a_blank_survives_in_every_position(self):
        cases = {
            "middle": ({"PARAM_TD1_DEVICE_0": "A", "PARAM_TD1_DEVICE_1": "B",
                        "PARAM_TD1_DEVICE_GATE_2": False, "PARAM_TD1_DEVICE_3": "A"},
                       "A, B, , A", ["A", "B", "", "A"]),
            "last":   ({"PARAM_TD1_DEVICE_0": "A", "PARAM_TD1_DEVICE_1": "B",
                        "PARAM_TD1_DEVICE_2": "C", "PARAM_TD1_DEVICE_GATE_3": False},
                       "A, B, C,", ["A", "B", "C", ""]),
            "first":  ({"PARAM_TD1_DEVICE_GATE_0": False, "PARAM_TD1_DEVICE_1": "B",
                        "PARAM_TD1_DEVICE_2": "C", "PARAM_TD1_DEVICE_3": "A"},
                       ", B, C, A", ["", "B", "C", "A"]),
        }
        for name, (syms, line, serials) in cases.items():
            with self.subTest(position=name):
                self.assertEqual(self.rendered("td1_blank_" + name, syms), line)
                self.assertEqual(self.booted("td1_blankboot_" + name, syms), serials)

    def test_no_gate_with_a_scanner_renders_no_list_at_all(self):
        # Rather than a line reading ', , ,'
        syms = {"PARAM_TD1_DEVICE_GATE_%d" % g: False for g in range(4)}
        self.assertIsNone(self.rendered("td1_none", syms))
        self.assertEqual(self.booted("td1_noneboot", syms), [""] * 4)

    def test_the_old_dash_placeholder_is_rejected_rather_than_taken_as_a_serial(self):
        # Nothing generates '-' any more, but a hand-written or copied config might.
        # Accepting it silently would give a gate a scanner named '-', which only
        # surfaces later as one that is permanently disconnected.
        # extra_params injects the list straight into the jinja render, standing in
        # for a config that was not produced by this installer
        from test.hh import session
        profile = profiles.get("boxturtle_test").derive(
            "td1_dash", syms=dict(self.BASE, **{
                "PARAM_TD1_DEVICE_0": "A", "PARAM_TD1_DEVICE_1": "B",
                "PARAM_TD1_DEVICE_2": "C", "PARAM_TD1_DEVICE_3": "A"}),
            extra_params={"PARAM_TD1_DEVICE_": ("A", "B", "-", "A")})
        self.assertEqual(
            cfg.assemble(cfg.render(profile))["mmu_unit unit0"].get("td1_devices"),
            "A, B, -, A")
        with self.assertRaisesRegex(Exception, r"uses a blank entry.*not '-'"):
            with session(profile) as hh:
                hh.boot(calibrate=True)


class TestTd1UpgradeRetention(unittest.TestCase):
    """
    Switching a gate's scanner off must survive re-running the installer.

    PARAM_TD1_DEVICE_GATE_$(i) matches the special-default prefix list, so an
    untouched one is written with the #~DEFAULT~# token and stays a modifiable
    default, while one the user actually switched off is written as a plain
    assignment that olddefconfig preserves. Getting that backwards would silently
    re-enable a gate's scanner on upgrade.
    """

    SYMS = {"MMU_HAS_TD1": True, "MMU_HAS_PER_GATE_TD1": True,
            "PARAM_TD1_DEVICE_0": "A", "PARAM_TD1_DEVICE_1": "B",
            "PARAM_TD1_DEVICE_2": "STALE", "PARAM_TD1_DEVICE_3": "A"}

    def render_from(self, kc):
        return next(
            (l for l in cfg._render_templates(
                ["config/base/mmu_hardware.cfg"], kc,
                {"PARAM_TOTAL_NUM_GATES": kc.getint("PARAM_NUM_GATES")}
             )["config/base/mmu_hardware.cfg"].splitlines()
             if l.startswith("td1_devices")), None)

    def test_a_switched_off_gate_survives_save_and_reload(self):
        import tempfile, os
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig("td1_up", dict(self.SYMS, PARAM_TD1_DEVICE_GATE_2=False))
            fd, path = tempfile.mkstemp(suffix=".mmu_config")
            os.close(fd)
            try:
                kc.write_config(path)
                with open(path) as fh:
                    saved = fh.read()
                # An explicit 'off' carries no #~DEFAULT~# token, so it is a real
                # assignment; an untouched gate does, so it stays resettable
                self.assertIn("# CONFIG_PARAM_TD1_DEVICE_GATE_2 is not set", saved)
                self.assertIn("CONFIG_PARAM_TD1_DEVICE_GATE_1=y #~DEFAULT~#", saved)

                reloaded = cfg._kconfig("td1_up_reload", {})
                reloaded.load_config(path)
                self.assertFalse(reloaded.is_enabled("PARAM_TD1_DEVICE_GATE_2"))
                self.assertTrue(reloaded.is_enabled("PARAM_TD1_DEVICE_GATE_1"))
                self.assertEqual(self.render_from(reloaded).split(":", 1)[1].strip(),
                                 "A, B, , A")

                # Writing it back out again must not drift
                second = path + "2"
                reloaded.write_config(second)
                try:
                    keep = lambda t: [l for l in t.splitlines() if "TD1" in l]
                    with open(second) as fh:
                        self.assertEqual(keep(saved), keep(fh.read()))
                finally:
                    os.unlink(second)
            finally:
                os.unlink(path)

    def test_a_serial_left_in_the_file_does_not_resurrect_the_gate(self):
        # An earlier session saved the serial while the gate was on; a later one
        # switched the gate off. build.py reads the raw user value regardless of
        # visibility, so the serial is still there - the toggle has to win
        import tempfile, os
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            seed = cfg._kconfig("td1_seed", self.SYMS)
            fd, path = tempfile.mkstemp(suffix=".mmu_config")
            os.close(fd)
            try:
                seed.write_config(path)
                with open(path) as fh:
                    text = fh.read()
                self.assertIn('CONFIG_PARAM_TD1_DEVICE_2="STALE"', text)
                with open(path, "w") as fh:
                    fh.write(text.replace(
                        "CONFIG_PARAM_TD1_DEVICE_GATE_2=y #~DEFAULT~#",
                        "# CONFIG_PARAM_TD1_DEVICE_GATE_2 is not set"))

                kc = cfg._kconfig("td1_upgraded", {})
                kc.load_config(path)
                self.assertEqual(kc.get("PARAM_TD1_DEVICE_2"), "STALE",
                                 "the stale serial really is still readable")
                self.assertEqual(self.render_from(kc).split(":", 1)[1].strip(),
                                 "A, B, , A")
            finally:
                os.unlink(path)


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
