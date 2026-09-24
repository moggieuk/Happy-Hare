# Happy Hare installer refresh integration tests.
#
# A same-version refresh is the baseline for every future upgrade: before a migration can
# transform renamed or moved settings, the installer must be able to rebuild the current
# templates without losing existing user values.  This test drives build_config_file(), not
# a test double, against the real BoxTurtle Kconfig profile and all four base files.
# The legacy fixture has no theme marker; its existing effect_* edits must survive.
#
# The fixture is deliberately a compact installed-config fragment rather than a frozen copy
# of every generated line.  It records only user-owned state.  Everything else must come from
# today's real templates, which prevents a second stale template tree growing under test/.
# Outputs are written to temporary directories; fixture files are never modified.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import glob
import os
import pickle
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from contextlib import contextmanager
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

    def test_legacy_effect_values_survive_in_hardware(self):
        """Introducing presets must preserve edits in pre-theme hardware files."""
        theme = self.parsed(self.first, "mmu/base/mmu_hardware_unit0.cfg")
        section = "mmu_leds unit0"
        self.assertEqual(effect_value(theme, section, "effect_error"),
                         ("mmu_sparkle", (1.0, 0.0, 0.0), 3.0))
        self.assertEqual(effect_value(theme, section, "effect_heating"),
                         ("mmu_breathing_blue_slow", (0.5, 0.2, 0.0), None))
        # Unedited (stock) values arrive too, matching the template.
        self.assertEqual(effect_value(theme, section, "effect_initialized"),
                         ("mmu_rainbow", (0.5, 0.2, 0.0), 8.0))

    def test_migrated_effect_values_survive_the_second_refresh(self):
        theme = self.parsed(self.second, "mmu/base/mmu_hardware_unit0.cfg")
        self.assertEqual(effect_value(theme, "mmu_leds unit0", "effect_error"),
                         ("mmu_sparkle", (1.0, 0.0, 0.0), 3.0))
        self.assertEqual(effect_value(theme, "mmu_leds unit0", "effect_heating"),
                         ("mmu_breathing_blue_slow", (0.5, 0.2, 0.0), None))


class TestHardwareThemeUpdateModes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.source = os.path.join(self.root, "mmu_hardware_unit0.cfg")
        self.dest = os.path.join(self.root, "out", "mmu_hardware_unit0.cfg")
        os.makedirs(os.path.dirname(self.dest))
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("theme-update-modes", profiles.get("boxturtle").syms)
        with open(self.source, "w") as f:
            f.write("# HH_LED_THEME unit0: mmu_leds mmu_leds\n"
                    "[mmu_leds unit0]\nframe_rate: 13\n"
                    "effect_error: mmu_sparkle, (0.123, 0, 0), 7\n"
                    "[mmu_led_effect user_test]\nunit: unit0\n"
                    "layers: static 0 0 top (0.123, 0, 0)\n")

    def build_mode(self, mode):
        from installer import build
        env = dict(cfg._SINGLE_UNIT_ENV, OUT=os.path.dirname(self.dest), F_CFG_UPGRADE_MODE=mode)
        with cfg._env(env), cfg._chdir(REPO_ROOT):
            build.build_config_file("config/base/mmu_hardware.cfg", self.dest,
                                    self.kconfig, [self.source], {})
        return ConfigBuilder(self.dest)

    def test_operation_mappings_and_kconfig_settings_in_all_modes(self):
        for mode in ("refresh", "replace", "merge"):
            with self.subTest(mode=mode):
                result = self.build_mode(mode)
                self.assertEqual(result.get("mmu_leds unit0", "frame_rate"),
                                 "13" if mode == "refresh" else str(self.kconfig.get("PARAM_FRAME_RATE")))
                self.assertEqual(effect_value(result, "mmu_leds unit0", "effect_error")[0],
                                 "mmu_red_strobe" if mode == "replace" else "mmu_sparkle")

    def test_replace_discards_user_added_effect_definitions(self):
        self.assertFalse(self.build_mode("replace").has_section("mmu_led_effect user_test"))

    def test_refresh_preserves_edited_mapping_when_theme_selection_changes(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("theme-refresh-switch", dict(
                profiles.get("boxturtle").syms, CHOICE_LED_THEME_EMU=True))
        self.assertEqual(effect_value(self.build_mode("refresh"), "mmu_leds unit0", "effect_error")[0],
                         "mmu_sparkle")

    def test_merge_preserves_edited_mapping_when_theme_selection_changes(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("theme-merge-switch", dict(
                profiles.get("boxturtle").syms, CHOICE_LED_THEME_EMU=True))
        result = self.build_mode("merge")
        self.assertEqual(effect_value(result, "mmu_leds unit0", "effect_error")[0], "mmu_sparkle")
        self.assertEqual(result.get("mmu_leds unit0", "frame_rate"),
                         str(self.kconfig.get("PARAM_FRAME_RATE")))
        self.assertTrue(result.has_section("mmu_led_effect user_test"))
        # A mapping missing from the old config is supplied by the new preset.
        self.assertEqual(effect_value(result, "mmu_leds unit0", "effect_gate_available")[0],
                         "mmu_static_white_dim_unit0")

    def test_refresh_retains_previous_preset_for_missing_values(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("theme-refresh-missing", dict(
                profiles.get("boxturtle").syms, CHOICE_LED_THEME_EMU=True))
        result = self.build_mode("refresh")
        self.assertEqual(effect_value(result, "mmu_leds unit0", "effect_gate_available")[0], "mmu_static_green")
        with open(self.dest) as f:
            self.assertIn("# HH_LED_THEME unit0: mmu_leds mmu_leds", f.read())

    def test_merge_carries_definitions_referenced_by_old_vendor_mappings(self):
        with open(self.source, "w") as f:
            f.write("# HH_LED_THEME unit0: emu_leds emu_leds\n"
                    "[mmu_leds unit0]\neffect_gate_available: mmu_static_white_unit0, (1, 1, 1)\n"
                    "[mmu_led_effect mmu_static_white_unit0]\nunit: unit0\ndefine_on: gates, exit\n"
                    "layers: static 0 0 top (0.123, 0, 0)\n")
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                result = self.build_mode(mode)
                self.assertEqual(effect_value(result, "mmu_leds unit0", "effect_gate_available")[0],
                                 "mmu_static_white_unit0")
                self.assertEqual(result.get("mmu_led_effect mmu_static_white_unit0", "layers"),
                                 "static 0 0 top (0.123, 0, 0)")

    def test_replace_custom_uses_machine_default(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("theme-replace-custom", dict(
                profiles.get("boxturtle").syms, CHOICE_LED_THEME_CUSTOM=True))
        with open(self.source, "w") as f:
            f.write("# HH_LED_THEME unit0: custom emu_leds\n")
        result = self.build_mode("replace")
        self.assertEqual(effect_value(result, "mmu_leds unit0", "effect_gate_available")[0], "mmu_static_green")
        with open(self.dest) as f:
            self.assertIn("# HH_LED_THEME unit0: custom mmu_leds", f.read())


class TestKConfigDefaultThemeName(unittest.TestCase):
    """Verify the KConfig.default_theme_name() method resolves from Kconfig,
    not from a hardcoded MMU_TYPE mapping."""

    def test_emu_returns_emu_leds(self):
        profile = profiles.get("emu")
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV)):
            kc = cfg._kconfig("test-emu-default-theme", dict(profile.syms))
        self.assertEqual(kc.default_theme_name(), "emu_leds")

    def test_boxturtle_returns_mmu_leds(self):
        profile = profiles.get("boxturtle")
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV)):
            kc = cfg._kconfig("test-bt-default-theme", dict(profile.syms))
        self.assertEqual(kc.default_theme_name(), "mmu_leds")

    def test_no_led_unit_returns_default(self):
        # No-LED units: the theme choice is invisible, so the default
        # member resolves to None and the method returns the default theme.
        profile = profiles.get("3ms")
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV)):
            kc = cfg._kconfig("test-3ms-default-theme", dict(profile.syms))
        self.assertEqual(kc.default_theme_name(), "mmu_leds")

    def test_custom_selection_still_reports_machine_default(self):
        # Even when the user selected 'custom', the method returns the
        # machine's default theme (the one that would seed custom_<unit>.cfg).
        profile = profiles.get("emu")
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV)):
            kc = cfg._kconfig("test-emu-custom", dict(profile.syms, CHOICE_LED_THEME_CUSTOM=True))
        self.assertEqual(kc.default_theme_name(), "emu_leds")



class TestSelectedThemeBuild(unittest.TestCase):
    """Exercise the actual make graph and builder in an isolated config tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.out = os.path.join(self.root, "out")
        self.live = os.path.join(self.root, "live")
        self.values = os.path.join(self.root, "values")
        self.env = dict(os.environ, HH_VERSION=cfg.hh_version())
        self.env.update(cfg._SINGLE_UNIT_ENV)
        self.env.pop("MAKEFLAGS", None)
        self.env.pop("F_CFG_UPGRADE_MODE", None)

    def save(self, profile="emu", theme=None, legacy=False):
        syms = dict(profiles.get(profile).syms)
        if theme:
            syms["CHOICE_LED_THEME_" + theme.upper()] = True
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig("selected-theme-%s-%s" % (profile, theme), syms)
            kc.write_config(self.values)
        if legacy:
            with open(self.values) as f:
                lines = f.readlines()
            with open(self.values, "w") as f:
                f.writelines(line for line in lines if "LED_THEME" not in line)

    def make(self, *goals):
        # install.sh cleans the staging directory after each successful install.
        # Match that lifecycle, without depending on make's timestamp resolution.
        shutil.rmtree(self.out, ignore_errors=True)
        result = subprocess.run(
            ["make", "--no-print-directory", *goals, "OUT=" + self.out,
             "KCONFIG_CONFIG=" + self.values, "KLIPPER_CONFIG_HOME=" + self.live,
             "PY=" + sys.executable], cwd=REPO_ROOT, env=self.env,
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def hardware(self):
        return os.path.join(self.out, "mmu", "base", "mmu_hardware_unit0.cfg")

    def install_output(self):
        shutil.copytree(os.path.join(self.out, "mmu"), os.path.join(self.live, "mmu"), dirs_exist_ok=True)

    def test_old_values_render_only_hardware(self):
        self.save(legacy=True)
        self.make("build")
        self.assertFalse(os.path.exists(os.path.join(self.out, "mmu", "led_theme")))
        theme = ConfigBuilder(self.hardware())
        self.assertEqual(effect_value(theme, "mmu_leds unit0", "effect_gate_available")[0],
                         "mmu_static_white_dim_unit0")
        self.assertTrue(theme.has_section("mmu_led_effect mmu_static_white_unit0"))

    def test_no_leds_builds_no_effects(self):
        self.save(profile="3ms")
        self.make("build")
        self.assertFalse(ConfigBuilder(self.hardware()).has_section("mmu_leds unit0"))
        self.assertFalse(os.path.exists(os.path.join(self.out, "mmu", "led_theme")))

    def test_refresh_and_merge_preserve_edits_until_replace(self):
        self.save()
        self.make("build")
        self.install_output()
        hardware = os.path.join(self.live, "mmu", "base", "mmu_hardware_unit0.cfg")
        with open(hardware) as f:
            text = f.read()
        text = re.sub(r"(?m)^effect_error\s*:.*$",
                      "effect_error: mmu_red_strobe, (0.123, 0, 0), 7", text)
        text = re.sub(r"(?m)^white_light\s*:.*$", "white_light: (0.25, 0.25, 0.25)", text)
        with open(hardware, "w") as f:
            f.write(text)
        self.make("build")
        self.assertEqual(effect_value(ConfigBuilder(self.hardware()), "mmu_leds unit0", "effect_error"),
                         ("mmu_red_strobe", (0.123, 0, 0), 7))
        self.save(theme="standard")
        for mode in ("refresh", "merge"):
            self.env["F_CFG_UPGRADE_MODE"] = mode
            self.make("build")
            built = ConfigBuilder(self.hardware())
            self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_gate_available")[0],
                             "mmu_static_white_dim_unit0")
            self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_error"),
                             ("mmu_red_strobe", (0.123, 0, 0), 7))
            self.assertEqual(built.get("mmu_leds unit0", "white_light"), "(0.25, 0.25, 0.25)")
            self.assertTrue(built.has_section("mmu_led_effect mmu_static_white_unit0"))
            with open(self.hardware()) as f:
                preset = "emu_leds" if mode == "refresh" else "mmu_leds"
                self.assertIn("# HH_LED_THEME unit0: %s %s" % (preset, preset), f.read())
            self.install_output()
        self.env["F_CFG_UPGRADE_MODE"] = "replace"
        self.make("build")
        built = ConfigBuilder(self.hardware())
        self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_gate_available")[0], "mmu_static_green")
        self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_error")[1], (1, 0, 0))
        self.assertFalse(built.has_section("mmu_led_effect mmu_static_white_unit0"))

    def test_custom_keeps_current_source_across_refreshes(self):
        self.save(profile="boxturtle", theme="emu")
        self.make("build")
        self.install_output()
        self.save(profile="boxturtle", theme="custom")
        self.make("build")
        self.install_output()
        self.make("build")
        built = ConfigBuilder(self.hardware())
        self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_gate_available")[0], "mmu_static_white_dim_unit0")
        self.assertTrue(built.has_section("mmu_led_effect mmu_static_white_unit0"))
        with open(self.hardware()) as f:
            self.assertIn("# HH_LED_THEME unit0: custom emu_leds", f.read())


class TestOldPickleReparsing(unittest.TestCase):
    """A pickle lacking the schema version or default_theme_name key is
    detected as outdated and triggers a fresh parse."""

    @contextmanager
    def _emu_env(self):
        """Temp EMU values file + out/ + env for load_parsed_kconfig.

        Yields (values_file, pickle_file, env); the caller writes
        <pickle_file> (or runs pre_parse_kconfig) then calls inside the body.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out")
            os.makedirs(out)
            values_file = os.path.join(tmpdir, "mmu_config")
            with open(values_file, "w") as f:
                f.write("CONFIG_MMU_TYPE_EMU_1_0=y\n")
                f.write('CONFIG_UNIT_NAME="unit0"\n')
            env = dict(os.environ)
            env.pop("MAKEFLAGS", None)
            env["OUT"] = out
            env["SRC"] = cfg.REPO_ROOT
            env["srctree"] = os.path.join(cfg.REPO_ROOT, "installer")
            env.update(cfg._SINGLE_UNIT_ENV)
            yield values_file, os.path.join(out, "mmu_config.pickle"), env

    def test_old_pickle_triggers_fresh_parse(self):
        from installer import build
        with self._emu_env() as (values_file, pickle_file, env):
            # 4.00-era pickle: no version, no default_theme_name
            with open(pickle_file, "wb") as f:
                pickle.dump({"config_file": values_file, "values": {},
                             "choices": {}}, f)
            with cfg._env(env):
                result = build.load_parsed_kconfig(values_file)
            self.assertIsInstance(result, build.KConfig)
            self.assertEqual(result.default_theme_name(), "emu_leds")

    def test_fresh_pickle_returns_parsed(self):
        from installer import build
        with self._emu_env() as (values_file, pickle_file, env):
            with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
                build.pre_parse_kconfig(values_file)
            with cfg._env(env):
                result = build.load_parsed_kconfig(values_file)
            self.assertIsInstance(result, build.ParsedKConfig)
            self.assertEqual(result.default_theme_name(), "emu_leds")

    def test_missing_theme_key_triggers_fresh_parse(self):
        from installer import build
        with self._emu_env() as (values_file, pickle_file, env):
            # Has the schema version but no default_theme_name key
            with open(pickle_file, "wb") as f:
                pickle.dump({"config_file": values_file, "values": {},
                             "choices": {}, "pickle_version": 2}, f)
            with cfg._env(env):
                result = build.load_parsed_kconfig(values_file)
            self.assertIsInstance(result, build.KConfig)
            self.assertEqual(result.default_theme_name(), "emu_leds")


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


if __name__ == "__main__":
    unittest.main()
