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
import json
import os
import pickle
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
        self.assertEqual(mmu.get("mmu_machine", "happy_hare_version"), cfg.hh_version())
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


class _HardwareRefresh(unittest.TestCase):
    """Builds unit0's mmu_hardware.cfg over an installed one, in an upgrade mode."""

    UNIT = "mmu_unit unit0"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, profile, syms, mode, input_files, baseline=None):
        out = tempfile.mkdtemp(dir=self.tmp.name)
        dest = os.path.join(out, "mmu_hardware_unit0.cfg")
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig("fan-visibility-refresh",
                                   dict(profiles.get(profile).syms, **syms))
        if baseline is not None:
            write_menuconfig_baseline(out, kconfig, baseline)
        from installer import build
        extra = {"PARAM_TOTAL_NUM_GATES": kconfig.getint("PARAM_NUM_GATES")}
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV, OUT=out, F_CFG_UPGRADE_MODE=mode,
                           F_MENUCONFIG_BASELINE="y" if baseline is not None else "")), \
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


class TestFanVisibilityRefresh(_HardwareRefresh):
    """Toggling a fan's UI visibility renames it; [mmu_unit] must follow in every mode."""

    SHARED = {"MMU_HAS_FANS": True, "PIN_FAN": "unit0:PA8"}

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


class TestFanOptionRefresh(_HardwareRefresh):
    TYPES = {"fan_generic", "heater_fan", "controller_fan"}
    EXTENSIONS = {"cycle_time": "0.02", "hardware_pwm": "True",
                  "off_below": "0.15", "tachometer_pin": "^unit0:PA7"}

    def test_shared_and_per_gate_fan_extensions_survive_refresh(self):
        cases = [
            ("qidi", {"MMU_HAS_FANS": True, "PIN_FAN": "unit0:PA8",
                      "CUSTOM_CONTROLLER_FAN_SETUP": False,
                      "PIN_HEATER_FAN": "unit0:PA9",
                      "MMU_HAS_CONTROLLER_FAN": True,
                      "PIN_CONTROLLER_FAN": "unit0:PA10"}),
            ("emu", {"MMU_HAS_HEATER": True, "PIN_HEATER_FAN_0": "unit0:PA9",
                     "MMU_HAS_CONTROLLER_FAN": True,
                     "PIN_CONTROLLER_FAN_0": "unit0:PA10"}),
        ]
        from installer.build import template_managed_options
        with cfg._chdir(cfg.REPO_ROOT):
            policies = template_managed_options("config/base/mmu_hardware.cfg")
        emitted = {kind: set() for kind in self.TYPES}
        for profile, syms in cases:
            for with_heater in (False, True):
                with self.subTest(profile=profile, with_heater=with_heater):
                    initial = dict(syms, BOOL_CONTROLLER_FAN_WITH_HEATER=with_heater)
                    installed = self.build(profile, initial, "replace", [])
                    existing = ConfigBuilder(installed)
                    fans = [section for section in existing.sections(scope="included")
                            if section.split(" ", 1)[0] in self.TYPES]
                    self.assertEqual({s.split(" ", 1)[0] for s in fans}, self.TYPES)
                    for section in fans:
                        emitted[section.split(" ", 1)[0]].update(existing.options(section))
                        for option, value in self.EXTENSIONS.items():
                            existing.set(section, option, value)
                    with open(installed, "w") as f:
                        f.write(existing.write())
                    with cfg._env(cfg._SINGLE_UNIT_ENV):
                        baseline = cfg._kconfig("fan-options-before",
                                                dict(profiles.get(profile).syms, **initial))
                    suffix = "_0" if profile == "emu" else ""
                    changes = {"PARAM_%s_MAX_POWER%s" % (prefix, suffix): 0.6
                               for prefix in ("FAN", "HEATER_FAN", "CONTROLLER_FAN")}
                    built = ConfigBuilder(self.build(profile, dict(initial, **changes),
                                                      "refresh", [installed], baseline))
                    for section in fans:
                        for option, value in self.EXTENSIONS.items():
                            self.assertEqual(built.get(section, option), value)
                        if profile != "emu" or section.endswith("0"):
                            self.assertEqual(built.get(section, "max_power"), "0.6")
                    removed_syms = {"PIN_%s%s" % (prefix, suffix): ""
                                    for prefix in ("FAN", "HEATER_FAN", "CONTROLLER_FAN")}
                    removed = ConfigBuilder(self.build(profile, dict(initial, **removed_syms),
                                                        "refresh", [installed]))
                    for section in fans:
                        if profile != "emu" or section.endswith("0"):
                            self.assertFalse(removed.has_section(section))
        for kind, options in emitted.items():
            self.assertEqual(policies[kind], options, kind)


class TestTemperatureSensorRefresh(_HardwareRefresh):
    """Refresh preserves extensions while respecting template-owned omissions."""

    SENSOR = "temperature_sensor unit0_Env0"
    SECOND_SENSOR = "temperature_sensor unit0_Env1"
    OPTIONS = {
        "i2c_mcu": "custom_gate0_mcu",
        "zero_temperature_on_error": "False",
        "zero_humidity_on_error": "True",
        "stop_sampling_after_failures": "10",
    }

    def test_sensor_sections_have_blank_separator(self):
        for profile, syms in (("emu", {}),
                              ("boxturtle", {"MMU_HAS_ENVIRONMENT_SENSOR": True})):
            with self.subTest(profile=profile):
                path = self.build(profile, syms, "replace", [])
                with open(path) as f:
                    rendered = f.read()
                sections = re.findall(r"^\[temperature_sensor [^\n]+\]\n(.*?)(?=^\[|\Z)",
                                      rendered, re.MULTILINE | re.DOTALL)
                self.assertGreater(len(sections), 1)
                for body in sections:
                    lines = body.splitlines()
                    last_option = max(i for i, line in enumerate(lines)
                                      if line and not line.startswith("#"))
                    self.assertEqual(lines[last_option + 1:last_option + 2], [""])

    def test_refresh_keeps_complete_sensor_configuration(self):
        installed = self.build("emu", {}, "replace", [])
        existing = ConfigBuilder(installed)
        self.assertIn(self.SENSOR, existing.sections())
        for sensor in (self.SENSOR, self.SECOND_SENSOR):
            self.assertIn(sensor, existing.sections())
            for option, value in self.OPTIONS.items():
                existing.set(sensor, option, value)
        with open(installed, "w") as f:
            f.write(existing.write())

        path = self.build("emu", {}, "refresh", [installed])
        built = ConfigBuilder(path)
        for sensor in (self.SENSOR, self.SECOND_SENSOR):
            for option, value in self.OPTIONS.items():
                with self.subTest(sensor=sensor, option=option):
                    self.assertEqual(built.get(sensor, option), value)

        with open(path) as f:
            rendered = f.read()
        first = rendered.index("[{}]".format(self.SENSOR))
        second = rendered.index("[{}]".format(self.SECOND_SENSOR))
        custom = rendered.index("zero_temperature_on_error: False", first)
        self.assertLess(rendered.index("i2c_mcu", first), custom)
        self.assertLess(custom, second)
        self.assertRegex(rendered[first:second],
                         r"stop_sampling_after_failures: 10\n\n$")

    def test_sensor_type_change_replaces_only_that_sensor(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            baseline = cfg._kconfig("temperature-sensor-before", profiles.get("emu").syms)
        installed = self.build("emu", {}, "replace", [])
        existing = ConfigBuilder(installed)
        for sensor in (self.SENSOR, self.SECOND_SENSOR):
            for option, value in self.OPTIONS.items():
                existing.set(sensor, option, value)
        with open(installed, "w") as f:
            f.write(existing.write())

        built = ConfigBuilder(self.build(
            "emu", {"CHOICE_ENVIRONMENT_SENSOR_TYPE_BME280_0": True}, "refresh",
            [installed], baseline=baseline))
        self.assertEqual(built.get(self.SENSOR, "sensor_type"), "BME280")
        self.assertNotEqual(built.get(self.SENSOR, "i2c_mcu"), self.OPTIONS["i2c_mcu"])
        for option in self.OPTIONS:
            if option != "i2c_mcu":
                with self.subTest(option=option):
                    self.assertFalse(built.has_option(self.SENSOR, option))
        for option, value in self.OPTIONS.items():
            with self.subTest(sensor=self.SECOND_SENSOR, option=option):
                self.assertEqual(built.get(self.SECOND_SENSOR, option), value)

    def test_reset_managed_options_does_not_copy_them_back(self):
        cases = [
            ({}, {"PARAM_ENVIRONMENT_SENSOR_REPORT_TIME_0": 0},
             ("aht10_report_time",)),
            ({"CHOICE_ENVIRONMENT_SENSOR_TYPE_HTU21D_0": True,
              "CHOICE_ENVIRONMENT_SENSOR_HTU21D_RESOLUTION_TEMP14_HUM12_0": True,
              "PARAM_ENVIRONMENT_SENSOR_HTU21D_HOLD_MASTER_0": True},
             {"CHOICE_ENVIRONMENT_SENSOR_HTU21D_RESOLUTION_DEFAULT_0": True,
              "PARAM_ENVIRONMENT_SENSOR_HTU21D_HOLD_MASTER_0": False},
             ("htu21d_resolution", "htu21d_hold_master")),
            ({"CHOICE_ENVIRONMENT_SENSOR_I2C_SOFTWARE_0": True,
              "PIN_ENVIRONMENT_SENSOR_I2C_SCL_0": "unit0:PA1",
              "PIN_ENVIRONMENT_SENSOR_I2C_SDA_0": "unit0:PA2"},
             {"CHOICE_ENVIRONMENT_SENSOR_I2C_HARDWARE_0": True},
             ("i2c_software_scl_pin", "i2c_software_sda_pin")),
        ]
        cases.extend([
            ({"PARAM_ENVIRONMENT_SENSOR_I2C_BUS_0": "i2c1"},
             {"PARAM_ENVIRONMENT_SENSOR_I2C_BUS_0": ""}, ("i2c_bus",)),
            ({"PARAM_ENVIRONMENT_SENSOR_I2C_BUS_0": "i2c1"},
             {"CHOICE_ENVIRONMENT_SENSOR_I2C_SOFTWARE_0": True,
              "PIN_ENVIRONMENT_SENSOR_I2C_SCL_0": "unit0:PA1",
              "PIN_ENVIRONMENT_SENSOR_I2C_SDA_0": "unit0:PA2"}, ("i2c_bus",)),
        ])
        for initial, changes, removed in cases:
            with self.subTest(removed=removed):
                with cfg._env(cfg._SINGLE_UNIT_ENV):
                    baseline = cfg._kconfig("sensor-reset-before",
                                            dict(profiles.get("emu").syms, **initial))
                installed = self.build("emu", initial, "replace", [])
                existing = ConfigBuilder(installed)
                for option in removed:
                    self.assertTrue(existing.has_option(self.SENSOR, option))
                existing.set(self.SENSOR, "custom_driver_option", "42")
                with open(installed, "w") as f:
                    f.write(existing.write())
                built = ConfigBuilder(self.build("emu", dict(initial, **changes),
                                                  "refresh", [installed], baseline))
                for option in removed:
                    self.assertFalse(built.has_option(self.SENSOR, option), option)
                self.assertEqual(built.get(self.SENSOR, "custom_driver_option"), "42")

    def test_removed_sensor_and_replace_mode_do_not_restore_extensions(self):
        installed = self.build("emu", {}, "replace", [])
        existing = ConfigBuilder(installed)
        existing.set(self.SENSOR, "custom_driver_option", "42")
        with open(installed, "w") as f:
            f.write(existing.write())
        removed = ConfigBuilder(self.build("emu", {"PARAM_ENVIRONMENT_SENSOR_GATE_0": False},
                                           "refresh", [installed]))
        self.assertFalse(removed.has_section(self.SENSOR))
        self.assertTrue(removed.has_section(self.SECOND_SENSOR))
        replaced = ConfigBuilder(self.build("emu", {}, "replace", [installed]))
        self.assertFalse(replaced.has_option(self.SENSOR, "custom_driver_option"))

    def test_temperature_limits_render_merge_and_reset(self):
        for profile, symbols, sensor in (
                ("emu", {"PARAM_ENVIRONMENT_SENSOR_MIN_TEMP_0": "-5.5",
                         "PARAM_ENVIRONMENT_SENSOR_MAX_TEMP_0": "100"}, self.SENSOR),
                ("boxturtle", {"MMU_HAS_ENVIRONMENT_SENSOR": True,
                               "PARAM_ENVIRONMENT_SENSOR_MIN_TEMP": "0",
                               "PARAM_ENVIRONMENT_SENSOR_MAX_TEMP": "80.5"},
                 "temperature_sensor unit0_Env"),
                ("boxturtle", {"PARAM_MCU_ENVIRONMENT_SENSOR_MIN_TEMP": "0",
                               "PARAM_MCU_ENVIRONMENT_SENSOR_MAX_TEMP": "90"},
                 "temperature_sensor _unit0_mcu")):
            with self.subTest(profile=profile, sensor=sensor):
                default = ConfigBuilder(self.build(profile,
                    {k: v for k, v in symbols.items() if not k.endswith(("TEMP", "TEMP_0"))},
                    "replace", []))
                self.assertFalse(default.has_option(sensor, "min_temp"))
                self.assertFalse(default.has_option(sensor, "max_temp"))
                installed = self.build(profile, symbols, "replace", [])
                existing = ConfigBuilder(installed)
                for option in ("min_temp", "max_temp"):
                    key = next(k for k in symbols if option.upper() in k)
                    self.assertEqual(existing.get(sensor, option), symbols[key])
                existing.set(sensor, "max_temp", "95")
                with open(installed, "w") as f:
                    f.write(existing.write())
                refreshed = ConfigBuilder(self.build(profile, symbols, "refresh", [installed]))
                self.assertEqual(refreshed.get(sensor, "max_temp"), "95")
                with cfg._env(cfg._SINGLE_UNIT_ENV):
                    baseline = cfg._kconfig("limits-before",
                                            dict(profiles.get(profile).syms, **symbols))
                reset = {k: "" if k.endswith(("TEMP", "TEMP_0")) else v
                         for k, v in symbols.items()}
                refreshed = ConfigBuilder(self.build(profile, reset, "refresh", [installed], baseline))
                self.assertFalse(refreshed.has_option(sensor, "min_temp"))
                self.assertFalse(refreshed.has_option(sensor, "max_temp"))

    def test_other_sensor_changes_keep_custom_options(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            baseline = cfg._kconfig("temperature-sensor-before", profiles.get("emu").syms)
        installed = self.build("emu", {}, "replace", [])
        existing = ConfigBuilder(installed)
        for option in ("zero_temperature_on_error", "zero_humidity_on_error",
                       "stop_sampling_after_failures"):
            existing.set(self.SENSOR, option, self.OPTIONS[option])
        with open(installed, "w") as f:
            f.write(existing.write())

        built = ConfigBuilder(self.build(
            "emu", {"PARAM_ENVIRONMENT_SENSOR_I2C_ADDRESS_0": 57}, "refresh",
            [installed], baseline=baseline))
        self.assertEqual(built.get(self.SENSOR, "i2c_address"), "57")
        for option in ("zero_temperature_on_error", "zero_humidity_on_error",
                       "stop_sampling_after_failures"):
            self.assertEqual(built.get(self.SENSOR, option), self.OPTIONS[option])


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


class TestComponentNameRefresh(_HardwareRefresh):
    """A renamed encoder or buffer renames its section, and [mmu_unit] must follow in every mode."""

    OWNER = {"MMU_HAS_ENCODER": True, "PIN_ENCODER": "unit0:PA6"}

    def test_the_unit_follows_a_renamed_encoder_and_buffer(self):
        installed = self.build("boxturtle", self.OWNER, "replace", [])
        renamed = dict(self.OWNER, PARAM_ENCODER_NAME="box_enc",
                       PARAM_SYNC_FEEDBACK_BUFFER_NAME="box_buf")
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                built = ConfigBuilder(self.build("boxturtle", renamed, mode, [installed]))
                self.assertIn("mmu_encoder box_enc", built.sections())
                self.assertIn("mmu_buffer box_buf", built.sections())
                self.assertNotIn("mmu_encoder unit0", built.sections())
                self.assertEqual(built.get(self.UNIT, "encoder"), "box_enc")
                self.assertEqual(built.get(self.UNIT, "buffer"), "box_buf")

    def test_the_unit_follows_a_new_share(self):
        installed = self.build("boxturtle", self.OWNER, "replace", [])
        shared = dict(self.OWNER, MMU_SHARED_ENCODER=True, PARAM_ENCODER_NAME="xyz")
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                built = ConfigBuilder(self.build("boxturtle", shared, mode, [installed]))
                self.assertNotIn("mmu_encoder unit0", built.sections())
                self.assertEqual(built.get(self.UNIT, "encoder"), "xyz")


class TestFilamentBufferBuild(_HardwareRefresh):
    """[mmu_unit] filament_buffer follows the menuconfig option."""

    def test_filament_buffer_follows_menuconfig(self):
        for ticked, expected in ((True, "1"), (False, "0")):
            with self.subTest(ticked=ticked):
                built = ConfigBuilder(self.build("tradrack", {"MMU_HAS_FILAMENT_BUFFER": ticked},
                                                 "replace", []))
                self.assertEqual(built.get(self.UNIT, "filament_buffer"), expected)


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
        # merge is now an alias of refresh
        for mode, expected in (("refresh", "-58"), ("merge", "-58")):
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


def write_menuconfig_baseline(out, kconfig, baseline):
    """Save 'baseline' as the menuconfig baseline the build compares 'kconfig' with"""
    from installer import build
    path = os.path.join(out, "menuconfig_base", os.path.basename(kconfig.config_file) + ".pickle")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(build.parsed_kconfig_data(baseline, baseline.config_file), f)


class TestMenuconfigChanges(unittest.TestCase):
    """An interactive Refresh lets the Kconfig value win only where menuconfig changed it."""

    SECTION = "gcode_macro _MMU_FORM_TIP_VARS"
    OPTION = "variable_ramming_volume"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.syms = profiles.get("boxturtle").syms
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("menuconfig-after", self.syms)
        self.fresh = self.build(None, [], mode="replace")
        self.kconfig_value = ConfigBuilder(self.fresh).get(self.SECTION, self.OPTION)
        installed = ConfigBuilder(self.fresh)
        installed.set(self.SECTION, self.OPTION, "123")
        with open(self.fresh, "w") as f:
            f.write(installed.write())

    def baseline(self, **syms):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            return cfg._kconfig("menuconfig-before", dict(self.syms, **syms))

    def build(self, baseline, input_files, mode="refresh"):
        from installer import build
        out = tempfile.mkdtemp(dir=self.tmp.name)
        dest = os.path.join(out, "mmu_macro_vars.cfg")
        if baseline is not None:
            write_menuconfig_baseline(out, self.kconfig, baseline)
        extra = {"PARAM_TOTAL_NUM_GATES": self.kconfig.getint("PARAM_NUM_GATES")}
        env = dict(cfg._SINGLE_UNIT_ENV, OUT=out, F_CFG_UPGRADE_MODE=mode,
                   F_MENUCONFIG_BASELINE="y" if baseline is not None else "")
        with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file("config/base/mmu_macro_vars.cfg", dest, self.kconfig,
                                    input_files, extra)
        return dest

    def value(self, built):
        return ConfigBuilder(built).get(self.SECTION, self.OPTION)

    def test_without_a_baseline_the_cfg_value_is_kept(self):
        for mode in ("refresh", "merge"):
            with self.subTest(mode=mode):
                self.assertEqual(self.value(self.build(None, [self.fresh], mode)), "123")

    def test_unchanged_in_menuconfig_the_cfg_value_is_kept(self):
        self.assertEqual(self.value(self.build(self.baseline(), [self.fresh])), "123")

    def test_changed_in_menuconfig_the_kconfig_value_wins(self):
        self.assertNotEqual(self.kconfig_value, "7")
        with self.assertLogs(level="WARNING") as logs:
            built = self.build(self.baseline(VAR_FORM_TIP_RAMMING_VOLUME="7"), [self.fresh])
        self.assertEqual(self.value(built), self.kconfig_value)
        # The 123 was a hand edit menuconfig didn't know about, so the replacement is reported
        self.assertTrue(any("variable_ramming_volume: 123 -> %s" % self.kconfig_value in line
                            for line in logs.output), logs.output)

    def test_value_written_another_way_isnt_reported_as_an_edit(self):
        installed = ConfigBuilder(self.fresh)
        installed.set(self.SECTION, self.OPTION, "7.0")
        with open(self.fresh, "w") as f:
            f.write(installed.write())
        with self.assertNoLogs(level="WARNING"):
            built = self.build(self.baseline(VAR_FORM_TIP_RAMMING_VOLUME="7"), [self.fresh])
        self.assertEqual(self.value(built), self.kconfig_value)

    def test_missing_baseline_is_reported(self):
        from installer import build
        env = dict(cfg._SINGLE_UNIT_ENV, OUT=self.tmp.name, F_MENUCONFIG_BASELINE="y")
        with cfg._env(env), self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(build.menuconfig_baseline(self.kconfig, has_install=True))
        self.assertIn("menuconfig changes", logs.output[0])
        # A unit added in this menuconfig has nothing installed to keep
        with cfg._env(env), self.assertNoLogs(level="WARNING"):
            self.assertIsNone(build.menuconfig_baseline(self.kconfig, has_install=False))

    def test_replaced_edits_are_recorded_for_the_summary(self):
        report = os.path.join(self.tmp.name, "replaced")
        with cfg._env({"HH_REPLACED_REPORT": report}), self.assertLogs(level="WARNING"):
            self.build(self.baseline(VAR_FORM_TIP_RAMMING_VOLUME="7"), [self.fresh])
        with open(report) as f:
            self.assertEqual([json.loads(line) for line in f],
                             [["mmu_macro_vars.cfg", self.SECTION, self.OPTION]])


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

    def test_effect_changed_in_menuconfig_comes_with_its_definition(self):
        # Before menuconfig the gate effect was the stock one the install still has
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            baseline = cfg._kconfig("led-effect-before", dict(
                profiles.get("emu").syms, PARAM_EFFECT_GATE_AVAILABLE="mmu_static_green, (0, 0.5, 0)"))
        write_menuconfig_baseline(os.path.dirname(self.dest), self.kconfig, baseline)
        with cfg._env({"F_MENUCONFIG_BASELINE": "y"}):
            built = self.build("refresh")
        self.assertEqual(self.effect(built, "effect_gate_available"), "mmu_static_white_dim_unit0")
        self.assertTrue(built.has_section("mmu_led_effect mmu_static_white_dim_unit0"))
        self.assertEqual(self.effect(built, "effect_error"), "mmu_sparkle")

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


class TestSpoolWeightDefaultsSurviveRefresh(MmuCfgBuild):
    """The default_gate_*_weight lists aren't in the template, so a refresh must reinsert them."""

    EDITS = (("default_gate_remaining_weight", "400, 400, 400, 400"),
             ("default_gate_initial_weight", "1000, 1000, 1000, 1000"))

    def edit(self, line):
        if line.startswith("#default_gate_speed_override"):
            return "\n".join([line] + ["%s: %s" % edit for edit in self.EDITS])
        return line

    def test_weight_defaults_survive_refresh(self):
        built, _ = self.build("refresh", [self.installed(self.edit)])
        for option, value in self.EDITS:
            with self.subTest(option):
                self.assertEqual(self.option(built, option), value)


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


class TestManagedOptionPolicies(unittest.TestCase):
    def policies(self, source):
        from installer.build import template_managed_options
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "template.cfg")
            with open(path, "w") as f:
                f.write(source)
            with cfg._chdir(root):
                return template_managed_options("template.cfg")

    def test_source_declarations_survive_false_branches(self):
        policies = self.policies("[% if false %]\n"
            "[# @managed-options fan: pin, shutdown_speed #]\n[% endif %]")
        self.assertEqual(policies, {"fan": {"pin", "shutdown_speed"}})

    def test_generic_policy_opt_in_and_exact_name_override(self):
        from installer.build import HHConfig, validate_managed_options
        policies = self.policies("[# @managed-options fan: pin, shutdown_speed #]\n"
                                 "[# @managed-options fan special: pin, vendor_option #]")
        old = HHConfig([])
        old.read_buf("[fan normal]\npin: old\nshutdown_speed: 1\nvendor_option: 42\n"
                     "[fan special]\npin: old\nvendor_option: 42\n"
                     "[temperature_sensor undeclared]\nsensor_type: AHT10\ncustom: 42\n"
                     "[fan removed]\npin: old\nvendor_option: 42\n")
        built = ConfigBuilder()
        built.read_buf("[fan normal]\npin: new\n\n[fan special]\npin: new\n\n"
                       "[temperature_sensor undeclared]\nsensor_type: AHT10\n")
        validate_managed_options(built, policies)
        old.update_builder(built, managed_options=policies)
        self.assertEqual(built.get("fan normal", "pin"), "old")
        self.assertEqual(built.get("fan normal", "vendor_option"), "42")
        self.assertFalse(built.has_option("fan normal", "shutdown_speed"))
        self.assertFalse(built.has_option("fan special", "vendor_option"))
        self.assertFalse(built.has_option("temperature_sensor undeclared", "custom"))
        self.assertFalse(built.has_section("fan removed"))

    def test_invalid_or_duplicate_declarations_are_rejected(self):
        for source in ("[# @managed-options fan pin #]",
                       "[# @managed-options fan: bad-option #]",
                       "[# @managed-options fan: pin #][# @managed-options fan: other #]"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.policies(source)

    def test_emitted_options_must_be_declared(self):
        from installer.build import validate_managed_options
        built = ConfigBuilder()
        built.read_buf("[fan test]\npin: PA1\nshutdown_speed: 0\n")
        with self.assertRaisesRegex(ValueError, "shutdown_speed"):
            validate_managed_options(built, {"fan": {"pin"}})


class TestEncoderValidationRefresh(unittest.TestCase):
    """Encoder validation options that used to be template literals keep hand-edited values."""

    SECTION = "mmu_unit_parameters unit0"
    EDITS = {
        "bowden_pre_unload_error_tolerance": "45",
        "bowden_move_error_tolerance": "65",
        "toolhead_move_error_tolerance": "60",
        "toolhead_post_load_tighten": "30",
        "encoder_move_step_size": "20",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kconfig = cfg._kconfig("encoder-validation-refresh", profiles.get("encoder").syms)
        self.installed = os.path.join(self.tmp.name, "mmu_parameters_unit0.cfg")
        with open(self.installed, "w") as f:
            f.write("[%s]\n" % self.SECTION)
            f.write("".join("%s: %s\n" % item for item in self.EDITS.items()))

    def build(self, with_baseline):
        from installer import build
        out = tempfile.mkdtemp(dir=self.tmp.name)
        dest = os.path.join(out, "mmu_parameters_unit0.cfg")
        if with_baseline:
            write_menuconfig_baseline(out, self.kconfig, self.kconfig)
        extra = {"PARAM_TOTAL_NUM_GATES": self.kconfig.getint("PARAM_NUM_GATES")}
        env = dict(cfg._SINGLE_UNIT_ENV, OUT=out, F_CFG_UPGRADE_MODE="refresh",
                   F_MENUCONFIG_BASELINE="y" if with_baseline else "")
        with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file("config/base/mmu_parameters.cfg", dest, self.kconfig,
                                    [self.installed], extra)
        return ConfigBuilder(dest)

    def test_hand_edited_values_survive_refresh(self):
        for with_baseline in (False, True):
            built = self.build(with_baseline)
            for option, value in self.EDITS.items():
                with self.subTest(baseline=with_baseline, option=option):
                    self.assertEqual(built.get(self.SECTION, option), value)
