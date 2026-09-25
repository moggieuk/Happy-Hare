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
