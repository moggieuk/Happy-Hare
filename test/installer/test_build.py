# Happy Hare installer refresh integration tests.
#
# A same-version refresh is the baseline for every future upgrade: before a migration can
# transform renamed or moved settings, the installer must be able to rebuild the current
# templates without losing existing user values.  This test drives build_config_file(), not
# a test double, against the real BoxTurtle Kconfig profile, all four rendered base files and
# the LED theme file. The 4.00 fixture predates the theme move and still carries the effect_*
# assignments in the hardware file; the refresh must migrate them, user edits included.
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
import subprocess
import tempfile
import unittest

from installer.parser import ConfigBuilder
from test.hh import cfg, profiles

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 'effect_name, (r, g, b)[, duration]' with the refresh's value-column re-alignment ignored.
EFFECT_RE = re.compile(r"^\s*([\w_]+),\s*\(([^)]*)\)(?:,\s*([0-9.]+))?\s*$")


def effect_value(parser, section, option):
    match = EFFECT_RE.match(parser.get(section, option))
    assert match is not None, "unparseable effect value: %r" % parser.get(section, option)
    color = tuple(float(x) for x in match.group(2).split(","))
    duration = float(match.group(3)) if match.group(3) else None
    return match.group(1), color, duration


class TestV400Refresh(unittest.TestCase):
    """Refresh an installed v4.00 configuration to v4.00 twice."""

    FIXTURE = os.path.join(os.path.dirname(__file__), "refresh", "4_00", "input")
    INSTALLED_NAMES = {
        "config/base/mmu.cfg": "mmu/base/mmu.cfg",
        "config/base/mmu_hardware.cfg": "mmu/base/mmu_hardware_unit0.cfg",
        "config/base/mmu_macro_vars.cfg": "mmu/base/mmu_macro_vars.cfg",
        "config/base/mmu_parameters.cfg": "mmu/base/mmu_parameters_unit0.cfg",
        "config/led_theme/mmu_leds.cfg": "mmu/led_theme/mmu_leds_unit0.cfg",
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
        return sorted(
            glob.glob(os.path.join(root, "mmu", "base", "*.cfg"))
            + glob.glob(os.path.join(root, "mmu", "led_theme", "*.cfg"))
        )

    @classmethod
    def _build_pass(cls, kconfig, input_files, out_root):
        from installer import build

        extra = {"PARAM_TOTAL_NUM_GATES": kconfig.getint("PARAM_NUM_GATES")}
        env = dict(cfg._SINGLE_UNIT_ENV,
                   OUT=out_root,
                   F_CFG_UPGRADE_MODE="refresh")
        with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
            for template, installed_name in cls.INSTALLED_NAMES.items():
                dest = os.path.join(out_root, installed_name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                build.build_config_file(
                    template,
                    dest,
                    kconfig,
                    input_files,
                    extra,
                )

    @classmethod
    def parsed(cls, root, name):
        return ConfigBuilder(os.path.join(root, name))

    def test_existing_user_values_survive_refresh(self):
        mmu = self.parsed(self.first, "mmu/base/mmu.cfg")
        self.assertEqual(mmu.get("mmu_machine", "happy_hare_version"), "4.0.0")
        self.assertEqual(mmu.get("mmu_parameters", "log_level"), "4")

        macro_vars = self.parsed(self.first, "mmu/base/mmu_macro_vars.cfg")
        self.assertEqual(
            macro_vars.get(
                "gcode_macro _MMU_SEQUENCE_VARS",
                "variable_user_pre_load_extension",
            ),
            '"CUSTOM_PRE_LOAD"',
        )

        parameters = self.parsed(self.first, "mmu/base/mmu_parameters_unit0.cfg")
        section = "mmu_unit_parameters unit0"
        self.assertEqual(parameters.get(section, "gear_load_speed"), "123")
        self.assertEqual(parameters.get(section, "gear_buzz_accel"), "987")

    def test_user_defined_excluded_config_survives_refresh(self):
        mmu = self.parsed(self.first, "mmu/base/mmu.cfg")
        self.assertTrue(mmu.has_section("gcode_macro USER_REFRESH_SENTINEL"))
        self.assertEqual(
            mmu.get("gcode_macro USER_REFRESH_SENTINEL", "gcode"),
            "M118 refresh fixture survived",
        )

        hardware = self.parsed(self.first, "mmu/base/mmu_hardware_unit0.cfg")
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

    def test_edited_effect_values_migrate_to_the_led_theme(self):
        """The 4.00 fixture still carries effect_* in the hardware file (the
        pre-theme layout). A refresh moves them to the theme file, so a user's
        edits must arrive there, and the hardware file must no longer carry
        effect options at all (the theme is included ahead of it, so any that
        stayed would shadow the theme)."""
        theme = self.parsed(self.first, "mmu/led_theme/mmu_leds_unit0.cfg")
        section = "mmu_leds unit0"
        self.assertEqual(effect_value(theme, section, "effect_error"),
                         ("mmu_sparkle", (1.0, 0.0, 0.0), 3.0))
        self.assertEqual(effect_value(theme, section, "effect_heating"),
                         ("mmu_breathing_blue_slow", (0.5, 0.2, 0.0), None))
        # Unedited (stock) values arrive too, matching the template.
        self.assertEqual(effect_value(theme, section, "effect_initialized"),
                         ("mmu_rainbow", (0.5, 0.2, 0.0), 8.0))

        hardware = self.parsed(self.first, "mmu/base/mmu_hardware_unit0.cfg")
        for option in ("effect_error", "effect_heating", "effect_initialized"):
            self.assertFalse(hardware.has_option(section, option), option)
        # The hardware file points at the theme with Klipper's include syntax, the
        # path relative to the hardware file's own directory.
        self.assertIn("include ../led_theme/mmu_leds_unit0.cfg", hardware.sections())

    def test_migrated_effect_values_survive_the_second_refresh(self):
        theme = self.parsed(self.second, "mmu/led_theme/mmu_leds_unit0.cfg")
        self.assertEqual(effect_value(theme, "mmu_leds unit0", "effect_error"),
                         ("mmu_sparkle", (1.0, 0.0, 0.0), 3.0))
        self.assertEqual(effect_value(theme, "mmu_leds unit0", "effect_heating"),
                         ("mmu_breathing_blue_slow", (0.5, 0.2, 0.0), None))


class TestLedThemeMakefileSelection(unittest.TestCase):
    """The Makefile's led_theme_files_for(): which theme template a unit's build
    renders, read from the unit's values file. 'custom' has no template of its own -
    its build renders the machine's default theme, which is what seeds the
    machine-local custom_<unit>.cfg (build.py:seed_custom_theme). Rendering nothing
    for it leaves the hardware file's include dangling and klippy refuses to start."""

    def _variables_output(self, values_lines):
        env = dict(os.environ)
        env.pop("MAKEFLAGS", None)
        with tempfile.TemporaryDirectory() as tmpdir:
            values = os.path.join(tmpdir, "mmu_config")
            with open(values, "w") as f:
                f.write("\n".join(values_lines) + "\n")
            result = subprocess.run(
                ["make", "--no-print-directory", "variables",
                 "KCONFIG_CONFIG=%s" % values],
                cwd=REPO_ROOT, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            return re.sub(r"\x1b(?:\[[0-9;]*[A-Za-z]|\([A-B])", "", result.stdout)

    def _var_files(self, variable, values_lines):
        files = None
        for line in self._variables_output(values_lines).splitlines():
            if line.startswith(variable + " "):
                files = {t for t in line.split() if t.startswith("led_theme/")}
                break
        self.assertIsNotNone(files, "%s line not found" % variable)
        return files

    def _theme_files(self, values_lines):
        return self._var_files("hh_unit_config_files", values_lines)

    def _custom_files(self, values_lines):
        return self._var_files("hh_custom_theme_files", values_lines)

    def test_custom_builds_the_machine_default_theme(self):
        self.assertEqual(
            self._theme_files(['CONFIG_PARAM_LED_THEME="custom"']),
            {"led_theme/mmu_leds_unit0.cfg"})

    def test_an_explicit_shipped_selection_builds_just_that_theme(self):
        self.assertEqual(
            self._theme_files(['CONFIG_PARAM_LED_THEME="mmu_leds"']),
            {"led_theme/mmu_leds_unit0.cfg"})

    def test_a_values_file_without_a_selection_builds_the_machine_default(self):
        self.assertEqual(self._theme_files([]),
                         {"led_theme/mmu_leds_unit0.cfg"})

    def test_a_user_maintained_theme_name_builds_nothing(self):
        self.assertEqual(
            self._theme_files(['CONFIG_PARAM_LED_THEME="mytheme"']),
            set())

    def test_custom_installs_the_seeded_machine_local_theme(self):
        """The seeded custom file must join the install set - the seed only
        writes to the build output dir, and without this the hardware file's
        include dangles on the printer."""
        self.assertEqual(
            self._custom_files(['CONFIG_PARAM_LED_THEME="custom"']),
            {"led_theme/custom_unit0.cfg"})

    def test_non_custom_selections_install_no_custom_theme(self):
        for lines in ([],
                      ['CONFIG_PARAM_LED_THEME="mmu_leds"'],
                      ['CONFIG_PARAM_LED_THEME="mytheme"']):
            self.assertEqual(self._custom_files(lines), set())

    def test_an_installed_custom_theme_is_never_parsed_by_the_build(self):
        """The machine-local theme is user-owned once installed: no build may
        read it back as an upgrade input (hh_configs_to_parse)."""
        env = dict(os.environ)
        env.pop("MAKEFLAGS", None)
        with tempfile.TemporaryDirectory() as tmpdir:
            values = os.path.join(tmpdir, "mmu_config")
            with open(values, "w") as f:
                f.write('CONFIG_PARAM_LED_THEME="custom"\n')
            home = os.path.join(tmpdir, "config")
            for name in ("mmu/base/mmu.cfg", "mmu/led_theme/custom_unit0.cfg"):
                path = os.path.join(home, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w").close()
            result = subprocess.run(
                ["make", "--no-print-directory", "variables",
                 "KCONFIG_CONFIG=%s" % values,
                 "KLIPPER_CONFIG_HOME=%s" % home],
                cwd=REPO_ROOT, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = re.sub(r"\x1b(?:\[[0-9;]*[A-Za-z]|\([A-B])", "", result.stdout)
            line = next(l for l in out.splitlines()
                        if l.startswith("hh_configs_to_parse "))
            self.assertIn("mmu/base/mmu.cfg", line)
            self.assertNotIn("custom_unit0.cfg", line)

    def test_custom_on_an_emu_builds_the_vendor_theme(self):
        self.assertEqual(
            self._theme_files(['CONFIG_MMU_TYPE_EMU_1_0=y',
                               'CONFIG_PARAM_LED_THEME="custom"']),
            {"led_theme/emu_leds_unit0.cfg"})

    def test_custom_on_an_emu_installs_the_seeded_machine_local_theme(self):
        self.assertEqual(
            self._custom_files(['CONFIG_MMU_TYPE_EMU_1_0=y',
                                'CONFIG_PARAM_LED_THEME="custom"']),
            {"led_theme/custom_unit0.cfg"})

    def test_a_selection_less_values_file_on_an_emu_builds_the_vendor_theme(self):
        self.assertEqual(self._theme_files(['CONFIG_MMU_TYPE_EMU_1_0=y']),
                         {"led_theme/emu_leds_unit0.cfg"})

    def test_an_emu_can_still_pick_the_stock_theme(self):
        self.assertEqual(
            self._theme_files(['CONFIG_MMU_TYPE_EMU_1_0=y',
                               'CONFIG_PARAM_LED_THEME="mmu_leds"']),
            {"led_theme/mmu_leds_unit0.cfg"})


class TestCustomThemeSeeding(unittest.TestCase):
    """build_config_file() seeds mmu/led_theme/custom_<unit>.cfg from the machine's
    default theme when PARAM_LED_THEME is 'custom' - and leaves the file alone on
    later builds: once created, it belongs to the user."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        profile = profiles.get("boxturtle")
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV)):
            cls.kconfig = cfg._kconfig(
                "custom-theme-seed",
                dict(profile.syms, PARAM_LED_THEME="custom"))

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=self.tmpdir.name)

    def _build_theme(self):
        from installer import build
        dest = os.path.join(self.root, "mmu", "led_theme", "mmu_leds_unit0.cfg")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        extra = {"PARAM_TOTAL_NUM_GATES": self.kconfig.getint("PARAM_NUM_GATES")}
        env = dict(cfg._SINGLE_UNIT_ENV, OUT=self.root, F_CFG_UPGRADE_MODE="replace")
        with cfg._env(env), cfg._chdir(REPO_ROOT):
            build.build_config_file(
                "config/led_theme/mmu_leds.cfg", dest, self.kconfig, [], extra)

    def test_custom_selection_seeds_the_machine_local_theme(self):
        self._build_theme()
        custom = os.path.join(self.root, "mmu", "led_theme", "custom_unit0.cfg")
        self.assertTrue(os.path.exists(custom),
                        "custom theme was not seeded by the default theme's build")
        theme = ConfigBuilder(custom)
        # Seeded from the stock set: the section and its effect defaults are there.
        self.assertTrue(theme.has_section("mmu_leds unit0"))
        self.assertEqual(effect_value(theme, "mmu_leds unit0", "effect_initialized"),
                         ("mmu_rainbow", (0.5, 0.2, 0.0), 8.0))

    def test_a_rebuild_leaves_the_custom_theme_untouched(self):
        self._build_theme()
        custom = os.path.join(self.root, "mmu", "led_theme", "custom_unit0.cfg")
        with open(custom, "a") as f:
            f.write("\n# user-owned\n")
        self._build_theme()
        with open(custom) as f:
            self.assertIn("# user-owned", f.read())

    def test_custom_on_an_emu_seeds_from_the_vendor_theme(self):
        """On an EMU the machine default is the vendor set, so the seed
        comes from emu_leds.cfg: the seeded gate effects are the static
        ones, not the stock breathing set."""
        from installer import build
        profile = profiles.get("emu")
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV)):
            kconfig = cfg._kconfig(
                "custom-theme-seed-emu",
                dict(profile.syms, PARAM_LED_THEME="custom"))
        dest = os.path.join(self.root, "mmu", "led_theme", "emu_leds_unit0.cfg")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        extra = {"PARAM_TOTAL_NUM_GATES": kconfig.getint("PARAM_NUM_GATES")}
        env = dict(cfg._SINGLE_UNIT_ENV, OUT=self.root, F_CFG_UPGRADE_MODE="replace")
        with cfg._env(env), cfg._chdir(REPO_ROOT):
            build.build_config_file(
                "config/led_theme/emu_leds.cfg", dest, kconfig, [], extra)

        custom = os.path.join(self.root, "mmu", "led_theme", "custom_unit0.cfg")
        self.assertTrue(os.path.exists(custom),
                        "EMU custom theme was not seeded by the vendor theme's build")
        theme = ConfigBuilder(custom)
        self.assertEqual(effect_value(theme, "mmu_leds unit0", "effect_gate_available"),
                         ("mmu_static_white_dim_unit0", (0.1, 0.1, 0.1), None))


class TestLedThemePrune(unittest.TestCase):
    """The install step's prune_led_themes(): when the menuconfig selection changes,
    installed shipped-theme files the unit no longer selects are removed. 'custom' and
    user-maintained theme files are never touched, and a unit without LEDs keeps no
    shipped theme files at all."""

    SEED = ("mmu_leds_unit0.cfg", "emu_leds_unit0.cfg",
            "custom_unit0.cfg", "usertheme_unit0.cfg")

    def _run(self, profile_name, overrides):
        profile = profiles.get(profile_name)
        env = dict(cfg._SINGLE_UNIT_ENV)
        with cfg._env(env):
            kcfg = cfg._kconfig("prune-%s" % profile_name,
                                dict(profile.syms, **overrides))
        from installer import build  # after _kconfig: it puts the vendored kconfiglib first
        tmpdir = tempfile.TemporaryDirectory()
        try:
            theme_dir = os.path.join(tmpdir.name, "mmu", "led_theme")
            os.makedirs(theme_dir)
            for name in self.SEED:
                with open(os.path.join(theme_dir, name), "w") as f:
                    f.write("[mmu_leds unit0]\n")
            env = dict(cfg._SINGLE_UNIT_ENV, OUT=os.path.join(tmpdir.name, "out"))
            with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
                build.prune_led_themes(tmpdir.name, [kcfg])
            return {os.path.basename(p) for p in glob.glob(os.path.join(theme_dir, "*.cfg"))}
        finally:
            tmpdir.cleanup()

    def test_stock_selection_drops_the_other_shipped_theme(self):
        self.assertEqual(self._run("boxturtle", {}),
                         {"mmu_leds_unit0.cfg", "custom_unit0.cfg", "usertheme_unit0.cfg"})

    def test_emu_selection_drops_the_stock_theme(self):
        self.assertEqual(self._run("boxturtle", {"PARAM_LED_THEME": "emu_leds"}),
                         {"emu_leds_unit0.cfg", "custom_unit0.cfg", "usertheme_unit0.cfg"})

    def test_custom_selection_drops_all_shipped_themes(self):
        self.assertEqual(self._run("boxturtle", {"PARAM_LED_THEME": "custom"}),
                         {"custom_unit0.cfg", "usertheme_unit0.cfg"})

    def test_unit_without_leds_drops_all_shipped_themes(self):
        self.assertEqual(self._run("3ms", {}),
                         {"custom_unit0.cfg", "usertheme_unit0.cfg"})

    def test_printer_without_a_theme_dir_is_a_noop(self):
        profile = profiles.get("boxturtle")
        env = dict(cfg._SINGLE_UNIT_ENV)
        with cfg._env(env):
            kcfg = cfg._kconfig("prune-noop", dict(profile.syms))
        from installer import build
        tmpdir = tempfile.TemporaryDirectory()
        try:
            home = os.path.join(tmpdir.name, "config")
            os.makedirs(home)
            env = dict(cfg._SINGLE_UNIT_ENV, OUT=os.path.join(tmpdir.name, "out"))
            with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
                build.prune_led_themes(home, [kcfg])
            self.assertFalse(os.path.exists(os.path.join(home, "mmu", "led_theme")))
        finally:
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
