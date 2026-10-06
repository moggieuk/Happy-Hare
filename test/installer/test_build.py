# Happy Hare installer refresh integration tests.
#
# A same-version refresh is the baseline for every future upgrade: before a migration can
# transform renamed or moved settings, the installer must be able to rebuild the current
# templates without losing existing user values.  This test drives build_config_file(), not
# a test double, against the real BoxTurtle Kconfig profile and all four rendered base files.
#
# The fixture is deliberately a compact installed-config fragment rather than a frozen copy
# of every generated line.  It records only user-owned state.  Everything else must come from
# today's real templates, which prevents a second stale template tree growing under test/.
# Outputs are written to temporary directories; fixture files are never modified.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import glob
import os
import re
import tempfile
import unittest

from installer.parser import ConfigBuilder
from test.hh import cfg, profiles


class TestV400Refresh(unittest.TestCase):
    """Refresh an installed v4.00 configuration to v4.00 twice."""

    FIXTURE = os.path.join(os.path.dirname(__file__), "refresh", "4_00", "input")
    INSTALLED_NAMES = {
        "config/base/mmu.cfg": "mmu.cfg",
        "config/base/mmu_hardware.cfg": "mmu_hardware_unit0.cfg",
        "config/base/mmu_macro_vars.cfg": "mmu_macro_vars.cfg",
        "config/base/mmu_parameters.cfg": "mmu_parameters_unit0.cfg",
    }

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.first = os.path.join(cls.tmpdir.name, "first")
        cls.second = os.path.join(cls.tmpdir.name, "second")

        profile = profiles.get("boxturtle")
        env = dict(cfg._SINGLE_UNIT_ENV, F_CFG_UPGRADE_MODE="refresh")
        with cfg._env(env):
            kconfig = cfg._kconfig("installer-refresh-4.00", profile.syms)
            cls._build_pass(kconfig, cls.fixture_files(), cls.first)
            cls._build_pass(kconfig, cls.output_files(cls.first), cls.second)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    @classmethod
    def fixture_files(cls):
        return sorted(glob.glob(os.path.join(cls.FIXTURE, "*.cfg")))

    @classmethod
    def output_files(cls, root):
        return sorted(glob.glob(os.path.join(root, "mmu", "base", "*.cfg")))

    @classmethod
    def _build_pass(cls, kconfig, input_files, out_root):
        from installer import build

        dest_dir = os.path.join(out_root, "mmu", "base")
        os.makedirs(dest_dir)
        extra = {"PARAM_TOTAL_NUM_GATES": kconfig.getint("PARAM_NUM_GATES")}
        env = dict(cfg._SINGLE_UNIT_ENV,
                   OUT=out_root,
                   F_CFG_UPGRADE_MODE="refresh")
        with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
            for template, installed_name in cls.INSTALLED_NAMES.items():
                build.build_config_file(
                    template,
                    os.path.join(dest_dir, installed_name),
                    kconfig,
                    input_files,
                    extra,
                )

    @classmethod
    def parsed(cls, root, name):
        return ConfigBuilder(os.path.join(root, "mmu", "base", name))

    def test_existing_user_values_survive_refresh(self):
        mmu = self.parsed(self.first, "mmu.cfg")
        self.assertEqual(mmu.get("mmu_machine", "happy_hare_version"), "4.0.0")
        self.assertEqual(mmu.get("mmu_parameters", "log_level"), "4")

        macro_vars = self.parsed(self.first, "mmu_macro_vars.cfg")
        self.assertEqual(
            macro_vars.get(
                "gcode_macro _MMU_SEQUENCE_VARS",
                "variable_user_pre_load_extension",
            ),
            '"CUSTOM_PRE_LOAD"',
        )

        parameters = self.parsed(self.first, "mmu_parameters_unit0.cfg")
        section = "mmu_unit_parameters unit0"
        self.assertEqual(parameters.get(section, "gear_load_speed"), "123")
        self.assertEqual(parameters.get(section, "gear_buzz_accel"), "987")

    def test_refresh_has_no_printer_wide_vent_settings(self):
        # Vent settings are per-unit heater_vent_* parameters
        macro_vars = self.parsed(self.first, "mmu_macro_vars.cfg")
        self.assertFalse(macro_vars.has_section("gcode_macro _MMU_VENT_VARS"))

    def test_user_defined_excluded_config_survives_refresh(self):
        mmu = self.parsed(self.first, "mmu.cfg")
        self.assertTrue(mmu.has_section("gcode_macro USER_REFRESH_SENTINEL"))
        self.assertEqual(
            mmu.get("gcode_macro USER_REFRESH_SENTINEL", "gcode"),
            "M118 refresh fixture survived",
        )

        hardware = self.parsed(self.first, "mmu_hardware_unit0.cfg")
        self.assertTrue(hardware.has_section("temperature_sensor fixture_chamber"))
        self.assertEqual(
            hardware.get("temperature_sensor fixture_chamber", "sensor_pin"),
            "unit0:PA0",
        )

    def test_second_refresh_is_byte_identical(self):
        first = self.output_files(self.first)
        second = self.output_files(self.second)
        self.assertEqual([os.path.basename(path) for path in first],
                         [os.path.basename(path) for path in second])
        for left, right in zip(first, second):
            with self.subTest(file=os.path.basename(left)):
                with open(left, "rb") as f:
                    first_bytes = f.read()
                with open(right, "rb") as f:
                    second_bytes = f.read()
                self.assertEqual(first_bytes, second_bytes)


class TestButtonGcodeRefresh(unittest.TestCase):

    def test_existing_semicolon_messages_survive_repeated_refresh(self):
        from installer.build import HHConfig

        commands = [
            'RESPOND PREFIX="BLOBIFIER" MSG="Bucket switch released (bucket removed); resetting count"',
            'RESPOND PREFIX="BLOBIFIER" MSG="Bucket switch released during startup; reset ignored"',
        ]
        installed = (
            '[gcode_button bucket]\n'
            'pin: ^PA0\n'
            'press_gcode:\n  RESPOND MSG="User message; keep me"\n'
            'release_gcode:\n'
            '  {% if printer["gcode_macro _BLOBIFIER_BUCKET_SWITCH"].armed|int %}\n'
            '    ' + commands[0] + '\n'
            '    _BLOBIFIER_COUNT_RESET\n'
            '  {% else %}\n'
            '    ' + commands[1] + '\n'
            '  {% endif %}\n'
        )
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "mmu.cfg")
            previous = installed
            for _ in range(2):
                with open(path, "w") as f:
                    f.write(previous)
                existing = HHConfig([path])
                # New templates use commas, but upgrades preserve user commands.
                target = ConfigBuilder()
                target.read_buf(installed.replace(";", ","))
                existing.update_builder(target, [], [])
                output = target.write()
                self.assertEqual(output, previous)
                self.assertEqual(target.parse_errors(), [])
                for command in commands:
                    self.assertIn(command, target.get("gcode_button bucket", "release_gcode"))
                self.assertIn('"User message; keep me"', target.get("gcode_button bucket", "press_gcode"))
                previous = output


class TestUnitListRefresh(unittest.TestCase):
    """[mmu_machine] units is owned by Kconfig: an installed list must never win."""

    MULTI_UNIT_ENV = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT="y", F_MULTI_UNIT_ENTRY_POINT="y")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.installed = self.build("replace", "unit0", [])

    def build(self, mode, units, input_files):
        out = tempfile.mkdtemp(dir=self.tmp.name)
        dest = os.path.join(out, "mmu.cfg")
        with cfg._env(dict(self.MULTI_UNIT_ENV, UNIT_NAME=units)):
            kconfig = cfg._kconfig("unit-list-refresh", {"MMU_UNITS": units})
        from installer import build
        with cfg._env(dict(self.MULTI_UNIT_ENV, OUT=out, F_CFG_UPGRADE_MODE=mode)), \
                cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file("config/base/mmu.cfg", dest, kconfig, input_files,
                                    {"PARAM_TOTAL_NUM_GATES": 8})
        return dest

    def test_added_unit_reaches_the_installed_list(self):
        for mode in ("refresh", "merge", "replace"):
            with self.subTest(mode=mode):
                built = self.build(mode, "unit0,unit1", [self.installed])
                self.assertEqual(ConfigBuilder(built).get("mmu_machine", "units"), "unit0,unit1")


class TestFanVisibilityRefresh(unittest.TestCase):
    """Toggling a fan's UI visibility renames it; [mmu_unit] must follow in every mode."""

    UNIT = "mmu_unit unit0"
    SHARED = {"MMU_HAS_FANS": True, "PIN_FAN": "unit0:PA8"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, profile, syms, mode, input_files):
        out = tempfile.mkdtemp(dir=self.tmp.name)
        dest = os.path.join(out, "mmu_hardware_unit0.cfg")
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig("fan-visibility-refresh",
                                   dict(profiles.get(profile).syms, **syms))
        from installer import build
        extra = {"PARAM_TOTAL_NUM_GATES": kconfig.getint("PARAM_NUM_GATES")}
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV, OUT=out, F_CFG_UPGRADE_MODE=mode)), \
                cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file("config/base/mmu_hardware.cfg", dest, kconfig,
                                    input_files, extra)
        return dest

    def edit(self, path, old, new):
        with open(path) as f:
            text = f.read()
        self.assertIn(old, text)
        with open(path, "w") as f:
            f.write(text.replace(old, new))

    def test_shared_fan_reference_follows_visibility(self):
        installed = self.build("qidi", self.SHARED, "replace", [])
        shown = dict(self.SHARED, BOOL_HIDE_FAN=False)
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                built = ConfigBuilder(self.build("qidi", shown, mode, [installed]))
                self.assertIn("fan_generic unit0_fan", built.sections())
                self.assertEqual(built.get(self.UNIT, "fan"), "unit0_fan")

    def test_per_gate_fan_references_follow_visibility(self):
        installed = self.build("emu", {}, "replace", [])
        shown = {"BOOL_HIDE_FAN": False}
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                built = ConfigBuilder(self.build("emu", shown, mode, [installed]))
                self.assertEqual(built.get(self.UNIT, "fans").split(", "),
                                 ["unit0_fan%d" % i for i in range(5)])

    def test_hand_edited_fan_reference_is_kept(self):
        installed = self.build("emu", {}, "replace", [])
        self.edit(installed, "_unit0_fan2,", "my_fan,")
        shown = {"BOOL_HIDE_FAN": False}
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                built = ConfigBuilder(self.build("emu", shown, mode, [installed]))
                self.assertEqual(built.get(self.UNIT, "fans").split(", "),
                                 ["unit0_fan0", "unit0_fan1", "my_fan",
                                  "unit0_fan3", "unit0_fan4"])


def _hidden_name_symbols():
    """Kconfig name symbols with a "_" hiding default, "$(i)" repeats as their "X_" template form."""
    symbols, symbol = set(), None
    for root, dirs, files in os.walk(os.path.join(cfg.REPO_ROOT, "installer")):
        dirs[:] = [d for d in dirs if d != "lib"]
        for name in files:
            if not name.startswith("Kconfig"):
                continue
            with open(os.path.join(root, name)) as f:
                for line in f:
                    match = re.match(r"\s*(?:menu)?config\s+(\S+)", line)
                    if match:
                        symbol = re.sub(r"\$\([^)]*\)$", "", match.group(1))
                    elif re.match(r'\s*default\s+"_\$\(UNIT_NAME\)', line):
                        symbols.add(symbol)
    return symbols


def _visibility_references(lines, symbols):
    """(section type, option) pairs whose rendered value uses one of symbols."""
    found, section, local = set(), None, {}

    def resolve(expr):
        refs = set()
        for token in re.findall(r"[A-Za-z_]\w*", expr):
            indexed = re.sub(r"_\d+$", "_", token)
            if token in symbols or indexed in symbols:
                refs.add(token if token in symbols else indexed)
            else:
                refs |= local.get(token, set())
        return refs

    for line in lines:
        header = re.match(r"\[(\w+)[ \]]", line)
        if header:
            section = header.group(1)
            continue
        for stmt in re.findall(r"\[%-?\s*(.*?)\s*-?%\]", line):
            assign = re.match(r"set\s+(\w+)\s*=(.*)", stmt)
            refs = resolve(assign.group(2) if assign else stmt)
            if assign:
                local[assign.group(1)] = refs
            for target in re.findall(r"(\w+)\.(?:append|extend)\(", stmt):
                local[target] = local.get(target, set()) | refs
        option = re.match(r"(\w+)\s*[:=]", line)
        if option and any(resolve(expr) for expr in re.findall(r"\[\[(.*?)\]\]", line)):
            found.add((section, option.group(1)))
    return found


class TestKconfigOwnedVisibility(unittest.TestCase):
    """Every option rendered from a hide-prefixed name must follow visibility on refresh."""

    @classmethod
    def setUpClass(cls):
        cls.symbols = _hidden_name_symbols()
        cls.references = set()
        for path in glob.glob(os.path.join(cfg.REPO_ROOT, "config", "**", "*.cfg"),
                              recursive=True):
            with open(path) as f:
                cls.references |= _visibility_references(f, cls.symbols)

    def test_scan_finds_known_symbols_and_references(self):
        self.assertTrue({"PARAM_FAN_NAME", "PARAM_FAN_NAME_", "PARAM_HEATER_FAN_NAME"}
                        <= self.symbols)
        self.assertTrue({("mmu_unit", "fan"), ("mmu_unit", "fans")} <= self.references)

    def test_scan_flags_direct_use_and_ignores_reset_lists(self):
        lines = [
            "[mmu_unit x]",
            "heater_fan : [[ PARAM_HEATER_FAN_NAME ]]",
            "[% set names = [] %]",
            "[% set _ = names.append((PARAM_FAN_NAME_|d)[i]) %]",
            "[% set names = [] %]",
            "[% set _ = names.append((PARAM_ENVIRONMENT_SENSOR_|d)[i]) %]",
            "environment_sensors : [[ names|join(', ') ]]",
        ]
        self.assertEqual(_visibility_references(lines, self.symbols),
                         {("mmu_unit", "heater_fan")})

    def test_every_reference_is_kconfig_owned(self):
        cfg._prepare_imports()
        from installer.build import KCONFIG_OWNED_VISIBILITY
        for pair in sorted(self.references):
            with self.subTest(pair=pair):
                self.assertIn(pair, KCONFIG_OWNED_VISIBILITY)


class TestSupplementalParamRefresh(unittest.TestCase):
    """A supplemental param is only reinserted when the template doesn't already render it."""

    SECTION = "mmu_unit_parameters unit0"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, profile, mode, input_files):
        from installer import build
        out = tempfile.mkdtemp(dir=self.tmp.name)
        dest = os.path.join(out, "mmu_parameters_unit0.cfg")
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig("supplemental-refresh", profiles.get(profile).syms)
        extra = {"PARAM_TOTAL_NUM_GATES": kconfig.getint("PARAM_NUM_GATES")}
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV, OUT=out, F_CFG_UPGRADE_MODE=mode)), \
                cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file("config/base/mmu_parameters.cfg", dest, kconfig,
                                    input_files, extra)
        with open(dest) as f:
            return dest, f.read()

    def count(self, text, option):
        return sum(1 for line in text.splitlines() if line.split(":")[0].strip() == option)

    def test_htlf_refresh_matches_fresh_render(self):
        fresh, fresh_text = self.build("htlf", "replace", [])
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                _, text = self.build("htlf", mode, [fresh])
                self.assertEqual(text, fresh_text)

    def test_htlf_edited_geometry_appears_once(self):
        fresh, text = self.build("htlf", "replace", [])
        with open(fresh, "w") as f:
            f.write(text.replace("cad_gate_width         : -60", "cad_gate_width         : -58"))
        for mode, expected in (("refresh", "-58"), ("merge", "-60")):
            with self.subTest(mode=mode):
                built, text = self.build("htlf", mode, [fresh])
                self.assertEqual(self.count(text, "cad_gate_width"), 1)
                self.assertEqual(ConfigBuilder(built).get(self.SECTION, "cad_gate_width"), expected)

    def test_commented_out_param_is_still_reinserted(self):
        source = os.path.join(self.tmp.name, "mmu_parameters_unit0.cfg")
        with open(source, "w") as f:
            f.write("[%s]\ncad_gate_width: 33\n" % self.SECTION)
        built, text = self.build("chameleon", "refresh", [source])
        self.assertEqual(self.count(text, "cad_gate_width"), 1)
        self.assertEqual(ConfigBuilder(built).get(self.SECTION, "cad_gate_width"), "33")


class TestMacroVarsMerge(unittest.TestCase):
    """Merge mode lets the Kconfig value of a macro variable win over the installed one."""

    SECTION = "gcode_macro _MMU_FORM_TIP_VARS"
    OPTION = "variable_ramming_volume"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("macro-vars-merge", profiles.get("boxturtle").syms)

    def build(self, mode, input_files):
        from installer import build
        out = tempfile.mkdtemp(dir=self.tmp.name)
        dest = os.path.join(out, "mmu_macro_vars.cfg")
        extra = {"PARAM_TOTAL_NUM_GATES": self.kconfig.getint("PARAM_NUM_GATES")}
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV, OUT=out, F_CFG_UPGRADE_MODE=mode)), \
                cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file("config/base/mmu_macro_vars.cfg", dest, self.kconfig,
                                    input_files, extra)
        return dest

    def test_merge_takes_the_kconfig_form_tip_value(self):
        fresh = self.build("replace", [])
        kconfig_value = ConfigBuilder(fresh).get(self.SECTION, self.OPTION)
        self.assertNotEqual(kconfig_value, "123")
        installed = ConfigBuilder(fresh)
        installed.set(self.SECTION, self.OPTION, "123")
        with open(fresh, "w") as f:
            f.write(installed.write())
        for mode, expected in (("refresh", "123"), ("merge", kconfig_value)):
            with self.subTest(mode=mode):
                built = self.build(mode, [fresh])
                self.assertEqual(ConfigBuilder(built).get(self.SECTION, self.OPTION), expected)


if __name__ == "__main__":
    unittest.main()


class TestLedEffectUpgrade(unittest.TestCase):
    """An existing EMU install (stock effect lines, no EMU effect sections, an excluded
    block) must never end up with effect mappings whose definitions are missing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = os.path.join(self.tmp.name, "mmu_hardware_unit0.cfg")
        self.dest = os.path.join(self.tmp.name, "out", "mmu_hardware_unit0.cfg")
        os.makedirs(os.path.dirname(self.dest))
        with open(self.source, "w") as f:
            f.write("[mmu_leds unit0]\n"
                    "effect_gate_available: mmu_static_green, (0, 0.5, 0)\n"
                    "effect_error: mmu_sparkle, (0.123, 0, 0), 7\n\n"
                    "# EXCLUDE FROM CONFIG BUILDER -- IMPORTANT do not alter or remove this line. "
                    "Config below is never upgraded\n"
                    "[temperature_sensor user_sensor]\nsensor_type: Generic 3950\n"
                    "sensor_pin: unit0:PA0\n")
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("led-effect-upgrade", profiles.get("emu").syms)

    def build(self, mode):
        from installer import build
        env = dict(cfg._SINGLE_UNIT_ENV, OUT=os.path.dirname(self.dest), F_CFG_UPGRADE_MODE=mode)
        with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file("config/base/mmu_hardware.cfg", self.dest,
                                    self.kconfig, [self.source], {})
        return ConfigBuilder(self.dest)

    def effect(self, built, option):
        return built.get("mmu_leds unit0", option).split(",")[0].strip()

    def test_merge_applies_emu_mappings_with_their_definitions(self):
        built = self.build("merge")
        self.assertEqual(self.effect(built, "effect_gate_available"), "mmu_static_white_dim_unit0")
        self.assertTrue(built.has_section("mmu_led_effect mmu_static_white_dim_unit0"))
        self.assertEqual(self.effect(built, "effect_error"), "mmu_red_strobe")

    def test_refresh_keeps_installed_mappings_and_adds_definitions(self):
        built = self.build("refresh")
        self.assertEqual(self.effect(built, "effect_gate_available"), "mmu_static_green")
        self.assertEqual(self.effect(built, "effect_error"), "mmu_sparkle")
        self.assertTrue(built.has_section("mmu_led_effect mmu_static_white_dim_unit0"))

    def test_excluded_block_is_preserved(self):
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                self.assertTrue(self.build(mode).has_section("temperature_sensor user_sensor"))


class MmuCfgBuild(unittest.TestCase):
    """Builds mmu.cfg for a single-unit BoxTurtle over an installed copy."""

    SECTION = "mmu_parameters"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fresh = self.build("replace", [])[1]

    def build(self, mode, input_files):
        from installer import build
        out = tempfile.mkdtemp(dir=self.tmp.name)
        dest = os.path.join(out, "mmu.cfg")
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig("mmu-cfg-build", profiles.get("boxturtle").syms)
        extra = {"PARAM_TOTAL_NUM_GATES": kconfig.getint("PARAM_NUM_GATES")}
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV, OUT=out, F_CFG_UPGRADE_MODE=mode)), \
                cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file("config/base/mmu.cfg", dest, kconfig, input_files, extra)
        with open(dest) as f:
            return dest, f.read()

    def installed(self, edit):
        """The fresh render with each line passed through edit(line)."""
        source = os.path.join(tempfile.mkdtemp(dir=self.tmp.name), "mmu.cfg")
        with open(source, "w") as f:
            f.write("\n".join(edit(line) for line in self.fresh.splitlines()) + "\n")
        return source

    def option(self, built, option):
        return ConfigBuilder(built).get(self.SECTION, option) or None


class TestEndlessSpoolGroupsRetired(MmuCfgBuild):
    """endless_spool_groups duplicated default_endless_spool_groups and was never read."""

    def test_the_template_only_documents_the_default(self):
        builder = ConfigBuilder()
        builder.read_buf(self.fresh)
        self.assertFalse(builder.has_option(self.SECTION, "endless_spool_groups"))
        self.assertFalse(builder.has_option(self.SECTION, "default_endless_spool_groups"))
        self.assertIn("#default_endless_spool_groups:", self.fresh)

    def test_an_installed_endless_spool_groups_line_is_dropped(self):
        def old_layout(line):
            if line.startswith("endless_spool_eject_gate"):
                return line + "\nendless_spool_groups     : %s\t\t# EndlessSpool grouping" % self.value
            return line
        for self.value in ("", "0, 1, 0, 1"):
            source = self.installed(old_layout)
            for mode in ("refresh", "merge", "replace"):
                with self.subTest(value=self.value, mode=mode):
                    built, _ = self.build(mode, [source])
                    self.assertFalse(ConfigBuilder(built).has_option(self.SECTION, "endless_spool_groups"))


class TestRefreshKeepsEditsAfterGcodeSequence(MmuCfgBuild):
    """gcode_load_sequence was parsed as G-code, so it swallowed the rest of
    [mmu_parameters] and a refresh reset every edit below it."""

    EDITS = (("gcode_unload_sequence", "1"), ("pause_macro", "MY_PAUSE"),
             ("default_ttg_map", "3, 2, 1, 0"), ("default_endless_spool_groups", "0, 1, 0, 1"))

    def edit(self, line):
        name = line.lstrip("#").split(":")[0].strip()
        for option, value in self.EDITS:
            if name == option and ":" in line:
                return "%s: %s" % (option, value)
        return line

    def test_edits_after_gcode_load_sequence_survive_refresh(self):
        source = self.installed(self.edit)
        built, _ = self.build("refresh", [source])
        for option, value in self.EDITS:
            with self.subTest(option):
                self.assertEqual(self.option(built, option), value)

    def test_a_set_default_survives_every_mode_once(self):
        source = self.installed(self.edit)
        for mode in ("refresh", "merge", "replace"):
            with self.subTest(mode=mode):
                built, text = self.build(mode, [source])
                self.assertEqual(self.option(built, "default_endless_spool_groups"), "0, 1, 0, 1")
                self.assertEqual(sum(1 for line in text.splitlines()
                                     if line.split(":")[0].strip() == "default_endless_spool_groups"), 1)
