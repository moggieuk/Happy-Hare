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
import subprocess
import sys
import tempfile
import unittest

from installer.parser import ConfigBuilder
from test.hh import cfg, profiles

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


    def test_existing_effect_values_survive_refresh(self):
        for root in (self.first, self.second):
            hardware = self.parsed(root, "mmu_hardware_unit0.cfg")
            self.assertEqual(effect_value(hardware, "mmu_leds unit0", "effect_error"),
                             ("mmu_sparkle", (1.0, 0.0, 0.0), 3.0))
            self.assertEqual(effect_value(hardware, "mmu_leds unit0", "effect_heating"),
                             ("mmu_breathing_blue_slow", (0.5, 0.2, 0.0), None))
            self.assertEqual(effect_value(hardware, "mmu_leds unit0", "effect_initialized"),
                             ("mmu_rainbow", (0.5, 0.2, 0.0), 8.0))


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


class TestLedThemeUpdateModes(unittest.TestCase):
    """A theme selection change is applied by Replace; Refresh and Merge keep the
    effect settings already installed and take only missing ones from the theme."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = os.path.join(self.tmp.name, "mmu_hardware_unit0.cfg")
        self.dest = os.path.join(self.tmp.name, "out", "mmu_hardware_unit0.cfg")
        os.makedirs(os.path.dirname(self.dest))
        with open(self.source, "w") as f:
            f.write("[mmu_leds unit0]\n"
                    "effect_error: mmu_sparkle, (0.123, 0, 0), 7\n"
                    "effect_gate_available: mmu_static_green, (0, 0.5, 0)\n")
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("led-theme-update-modes", dict(
                profiles.get("boxturtle").syms, CHOICE_LED_THEME_EMU=True))

    def build_mode(self, mode):
        from installer import build
        env = dict(cfg._SINGLE_UNIT_ENV, OUT=os.path.dirname(self.dest), F_CFG_UPGRADE_MODE=mode)
        with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file("config/base/mmu_hardware.cfg", self.dest,
                                    self.kconfig, [self.source], {})
        return ConfigBuilder(self.dest)

    def test_refresh_and_merge_keep_installed_effects(self):
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                built = self.build_mode(mode)
                self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_error"),
                                 ("mmu_sparkle", (0.123, 0.0, 0.0), 7.0))
                self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_gate_available")[0],
                                 "mmu_static_green")
                # Missing settings come from the selected theme
                self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_gate_empty_sel")[0],
                                 "mmu_static_red_unit0")

    def test_refresh_keeps_effect_sections_installed_mappings_reference(self):
        with open(self.source, "w") as f:
            f.write("[mmu_leds unit0]\n"
                    "effect_gate_available: mmu_static_white_dim_unit0, (0.1, 0.1, 0.1)\n"
                    "[mmu_led_effect mmu_static_white_dim_unit0]\nunit: unit0\n"
                    "layers: static 0 0 top (0.123, 0, 0)\n")
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("led-theme-back-to-standard", profiles.get("boxturtle").syms)
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                built = self.build_mode(mode)
                self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_gate_available")[0],
                                 "mmu_static_white_dim_unit0")
                self.assertEqual(built.get("mmu_led_effect mmu_static_white_dim_unit0", "layers"),
                                 "static 0 0 top (0.123, 0, 0)")
        self.assertFalse(self.build_mode("replace").has_section("mmu_led_effect mmu_static_white_dim_unit0"))

    def test_replace_applies_the_selected_theme(self):
        built = self.build_mode("replace")
        self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_error"),
                         ("mmu_red_strobe", (1.0, 0.0, 0.0), 10.0))
        self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_gate_available")[0],
                         "mmu_static_white_dim_unit0")


class TestLedThemeMakeBuild(unittest.TestCase):
    """The theme directory lookup and include work through the real make graph."""

    def test_values_without_a_theme_build_the_machine_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            values = os.path.join(tmp, "values")
            with cfg._env(cfg._SINGLE_UNIT_ENV):
                cfg._kconfig("led-theme-make-emu", profiles.get("emu").syms).write_config(values)
            with open(values) as f:
                lines = [line for line in f if "LED_THEME" not in line]
            with open(values, "w") as f:
                f.writelines(lines)

            env = dict(os.environ, HH_VERSION=cfg.hh_version(), **cfg._SINGLE_UNIT_ENV)
            env.pop("MAKEFLAGS", None)
            env.pop("F_CFG_UPGRADE_MODE", None)
            result = subprocess.run(
                ["make", "--no-print-directory", "build", "OUT=" + out,
                 "KCONFIG_CONFIG=" + values, "KLIPPER_CONFIG_HOME=" + os.path.join(tmp, "live"),
                 "PY=" + sys.executable], cwd=cfg.REPO_ROOT, env=env,
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            self.assertFalse(os.path.exists(os.path.join(out, "mmu", "led_theme")))
            built = ConfigBuilder(os.path.join(out, "mmu", "base", "mmu_hardware_unit0.cfg"))
            self.assertEqual(effect_value(built, "mmu_leds unit0", "effect_gate_available")[0],
                             "mmu_static_white_dim_unit0")
