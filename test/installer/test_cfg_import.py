# Happy Hare installer: importing the values the installed .cfg files hold into menuconfig
# at the start of an interactive Refresh, and the baseline the build compares against.
#
# Each test builds a real install the way Replace does - a saved .mmu_config plus the base
# files rendered from it - then edits the installed files as a user would and checks what
# cfg_import offers, what it writes back, and what a build makes of the result.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import contextlib
import io
import os
import re
import tempfile
import unittest
from unittest import mock

from installer.parser import ConfigBuilder
from test.hh import cfg, profiles

cfg._prepare_imports()

from installer import build, cfg_import  # noqa: E402 (needs kconfiglib on the path)

DEFAULT_TOKEN = " #~DEFAULT~#"

# make test exports the installer's colour codes
NO_COLOUR = {"C_NOTICE": "", "C_INFO": "", "C_OFF": ""}

SINGLE_UNIT_FILES = {
    "config/base/mmu.cfg": "mmu.cfg",
    "config/base/mmu_macro_vars.cfg": "mmu_macro_vars.cfg",
    "config/base/mmu_hardware.cfg": "mmu_hardware_{unit}.cfg",
    "config/base/mmu_parameters.cfg": "mmu_parameters_{unit}.cfg",
}


def build_files(env, home, kcfg, templates, extra, unit="unit0", mode="replace", installed=()):
    """Render templates into <home>/mmu/base the way make install names them"""
    with cfg._env(dict(env, OUT=home, F_CFG_UPGRADE_MODE=mode)), cfg._chdir(cfg.REPO_ROOT):
        for template in templates:
            dest = os.path.join(home, "mmu", "base", SINGLE_UNIT_FILES[template].format(unit=unit))
            build.build_config_file(template, dest, kcfg, list(installed), extra)


class _Install(unittest.TestCase):
    """A single-unit BoxTurtle, installed in Replace mode"""

    PROFILE = "boxturtle"
    SYMS = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "config")
        os.makedirs(os.path.join(self.home, "mmu", "base"))
        self.kconfig = os.path.join(self.tmp.name, ".mmu_config")
        self.env = dict(cfg._SINGLE_UNIT_ENV, HH_VERSION=cfg.hh_version(), KCONFIG_CONFIG=self.kconfig)
        with cfg._env(self.env):
            kc = cfg._kconfig("cfg-import", dict(profiles.get(self.PROFILE).syms, **self.SYMS))
        kc.write_config(self.kconfig)
        self.extra = {"PARAM_TOTAL_NUM_GATES": kc.getint("PARAM_NUM_GATES")}
        build_files(self.env, self.home, kc, SINGLE_UNIT_FILES, self.extra)

    def path(self, name):
        return os.path.join(self.home, "mmu", "base", name)

    def edit(self, name, option, value, section=None):
        """Set an option in an installed file (inside 'section' if given) as a user would"""
        with open(self.path(name)) as f:
            lines = f.read().split("\n")
        current, done = None, False
        for i, line in enumerate(lines):
            m = re.match(r"^\[([^\]]+)\]", line)
            if m:
                current = m.group(1)
            m = re.match(r"^(%s\s*[:=]\s*)(\S.*?)?(\s+[#;].*)?$" % re.escape(option), line)
            if m and not done and (section is None or current == section):
                lines[i] = m.group(1) + value + (m.group(3) or "")
                done = True
        self.assertTrue(done, "no %s in %s" % (option, name))
        with open(self.path(name), "w") as f:
            f.write("\n".join(lines))

    def analyse(self, kconfig=None, env=None):
        with cfg._env(env or self.env), cfg._chdir(cfg.INSTALLER):
            return cfg_import.Analysis(kconfig or self.kconfig, cfg_import.installed_files(self.home))

    def build_file(self, template, name, mode="replace"):
        """One file built from the saved config over the current install, returned parsed.
        A Replace build shows exactly what .mmu_config now holds"""
        out = os.path.join(self.tmp.name, "built")
        os.makedirs(os.path.join(out, "mmu", "base"), exist_ok=True)
        with cfg._env(self.env), cfg._chdir(cfg.INSTALLER):
            kcfg = build.KConfig(self.kconfig)
        with cfg._env(dict(self.env, OUT=out, F_CFG_UPGRADE_MODE=mode)), cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file(template, os.path.join(out, "mmu", "base", name), kcfg,
                                    cfg_import.installed_files(self.home), self.extra)
        return ConfigBuilder(os.path.join(out, "mmu", "base", name))

    def saved(self):
        with open(self.kconfig) as f:
            return f.read()

    def by_symbol(self, analysis):
        return {c.sym_name: c for c in analysis.candidates}

    def reasons(self, analysis):
        return {key: reason for key, _value, reason in analysis.kept}


class TestCandidates(_Install):

    def test_an_untouched_install_has_nothing_to_import(self):
        analysis = self.analyse()
        self.assertEqual(analysis.candidates, [])
        self.assertEqual(analysis.kept, [])

    def test_plain_param_is_offered(self):
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "123")
        candidate = self.by_symbol(self.analyse())["PARAM_GEAR_LOAD_SPEED"]
        self.assertEqual(candidate.locations, [("mmu_unit_parameters unit0", "gear_load_speed")])
        self.assertEqual((candidate.set_name, candidate.kconfig_value), ("PARAM_GEAR_LOAD_SPEED", "123"))

    def test_hidden_macro_variable_is_offered_through_its_toggle(self):
        self.edit("mmu_macro_vars.cfg", "variable_home_mmu", "True")
        candidate = self.by_symbol(self.analyse())["VAR_SOFTWARE_HOME_MMU"]
        self.assertEqual((candidate.set_name, candidate.kconfig_value), ("BOOL_SOFTWARE_HOME_MMU", "y"))

    def test_hidden_value_its_toggle_cant_produce_is_reported(self):
        self.edit("mmu_macro_vars.cfg", "variable_home_mmu", "Maybe")
        analysis = self.analyse()
        self.assertNotIn("VAR_SOFTWARE_HOME_MMU", self.by_symbol(analysis))
        self.assertEqual(self.reasons(analysis)[("gcode_macro _MMU_SOFTWARE_VARS", "variable_home_mmu")],
                         "no setting of BOOL_SOFTWARE_HOME_MMU gives this value")

    def test_hidden_value_is_offered_through_its_choice(self):
        self.edit("mmu.cfg", "log_file_level", "4")
        candidate = self.by_symbol(self.analyse())["PARAM_LOG_FILE_LEVEL"]
        self.assertEqual((candidate.set_name, candidate.kconfig_value), ("CHOICE_LOG_FILE_LEVEL_STEPPER", "y"))
        self.assertEqual(candidate.set_key, "CHOICE_LOG_FILE_LEVEL")

    def test_log_file_turned_off_is_offered_through_its_choice(self):
        self.edit("mmu.cfg", "log_file_level", "-1")
        analysis = self.analyse()
        candidate = self.by_symbol(analysis)["PARAM_LOG_FILE_LEVEL"]
        self.assertEqual(candidate.set_name, "CHOICE_LOG_FILE_LEVEL_OFF")
        analysis.apply([candidate])
        mmu = self.build_file("config/base/mmu.cfg", "mmu.cfg")
        self.assertEqual(mmu.get("mmu_parameters", "log_file_level").strip(), "-1")

    def test_value_no_choice_option_gives_is_reported(self):
        self.edit("mmu.cfg", "log_file_level", "7")
        analysis = self.analyse()
        self.assertNotIn("PARAM_LOG_FILE_LEVEL", self.by_symbol(analysis))
        self.assertEqual(self.reasons(analysis)[("mmu_parameters", "log_file_level")],
                         "no CHOICE_LOG_FILE_LEVEL option gives this value")

    def test_one_choice_needed_two_ways_is_reported(self):
        analysis = self.analyse()
        for name, member, key in (("PARAM_A", "CHOICE_X_ONE", ("s", "a")), ("PARAM_B", "CHOICE_X_TWO", ("s", "b"))):
            candidate = cfg_import.Candidate(name, False, [key], "0", "1")
            candidate.set_name, candidate.set_key, candidate.kconfig_value = member, "CHOICE_X", "y"
            analysis.candidates.append(candidate)
        analysis._drop_conflicts()
        self.assertEqual(analysis.candidates, [])
        self.assertEqual(self.reasons(analysis), {("s", "a"): "CHOICE_X is needed two ways",
                                                  ("s", "b"): "CHOICE_X is needed two ways"})

    def test_capability_flag_is_never_used_as_a_toggle(self):
        # PARAM_LOGO_EFFECT is hidden and its defaults are conditional on MMU_HAS_LEDS
        self.edit("mmu_hardware_unit0.cfg", "logo_effect", "off")
        analysis = self.analyse()
        self.assertEqual([c.set_name for c in analysis.candidates], [])
        self.assertIn(("mmu_leds unit0", "logo_effect"), self.reasons(analysis))

    def test_value_that_changes_another_rendered_value_is_reported(self):
        # The gate count also adds per-gate options
        self.edit("mmu_hardware_unit0.cfg", "num_gates", "6", section="mmu_unit unit0")
        analysis = self.analyse()
        self.assertNotIn("PARAM_NUM_GATES", self.by_symbol(analysis))
        reason = self.reasons(analysis)[("mmu_unit unit0", "num_gates")]
        self.assertTrue(reason.startswith("would also change ["), reason)

    def test_templated_option_name_is_recognised(self):
        # [[ ("driver_" ~ PARAM_GEAR_TMC_STALLGUARD_FIELD).ljust(25) ]]: [[PARAM_GEAR_STALLGUARD_THRESHOLD]].
        # Gears 1-3 render the same threshold, so an edit to gear 0 alone stays in the .cfg
        self.edit("mmu_hardware_unit0.cfg", "driver_SGTHRS", "77", section="tmc2209 mmu_stepper unit0_gear")
        analysis = self.analyse()
        self.assertNotIn("PARAM_GEAR_STALLGUARD_THRESHOLD", self.by_symbol(analysis))
        self.assertEqual(self.reasons(analysis)[("tmc2209 mmu_stepper unit0_gear", "driver_SGTHRS")],
                         "PARAM_GEAR_STALLGUARD_THRESHOLD is set differently elsewhere")

    def test_threshold_edited_on_every_gear_is_imported(self):
        gears = ["tmc2209 mmu_stepper unit0_gear"] + ["tmc2209 mmu_stepper unit0_gear_%d" % i for i in (1, 2, 3)]
        for section in gears:
            self.edit("mmu_hardware_unit0.cfg", "driver_SGTHRS", "77", section=section)
        candidate = self.by_symbol(self.analyse())["PARAM_GEAR_STALLGUARD_THRESHOLD"]
        self.assertEqual(sorted(candidate.locations), [(section, "driver_SGTHRS") for section in gears])
        self.assertEqual(candidate.kconfig_value, "77")

    def test_summary_sections_are_spaced_and_coloured(self):
        self.edit("mmu_hardware_unit0.cfg", "step_pin", "unit0:PA9", section="mmu_stepper unit0_gear")
        self.edit("mmu_hardware_unit0.cfg", "mmu_entry_switch_pin_1", "^unit0:PA10", section="mmu_sensors unit0")
        out = io.StringIO()
        with cfg._env({"C_NOTICE": "<N>", "C_INFO": "<I>", "C_OFF": "</>"}):
            cfg_import.show_edits("Heading", cfg_import_rows(self.analyse()), False, out)
        self.assertEqual(out.getvalue(), "<N>Heading</>\n\n<I>> mmu_hardware_unit0.cfg:</>\n"
                         "<I>[mmu_stepper unit0_gear]</>\n<I>step_pin: unit0:PA9</>\n\n"
                         "<I>[mmu_sensors unit0]</>\n<I>mmu_entry_switch_pin_1: ^unit0:PA10</>\n\n")

    def test_string_literal_value_is_decoded(self):
        self.edit("mmu_macro_vars.cfg", "variable_user_pre_initialize_extension", '"MY_INIT"')
        candidate = self.by_symbol(self.analyse())["VAR_SOFTWARE_USER_PRE_INITIALIZE_EXTENSION"]
        self.assertEqual(candidate.kconfig_value, "MY_INIT")

    def test_out_of_range_value_is_reported(self):
        self.edit("mmu_hardware_unit0.cfg", "frame_rate", "99")
        analysis = self.analyse()
        self.assertNotIn("PARAM_FRAME_RATE", self.by_symbol(analysis))
        self.assertEqual(self.reasons(analysis)[("mmu_leds unit0", "frame_rate")],
                         "outside the range menuconfig allows")

    def test_malformed_number_is_reported(self):
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "fast")
        reason = self.reasons(self.analyse())[("mmu_unit_parameters unit0", "gear_load_speed")]
        self.assertTrue(reason.startswith("not a valid"), reason)

    def test_option_menuconfig_doesnt_render_is_left_in_the_cfg(self):
        # [mmu_unit] toolhead is a literal in the template, so there is nothing to import
        self.edit("mmu_hardware_unit0.cfg", "toolhead", "other", section="mmu_unit unit0")
        analysis = self.analyse()
        self.assertEqual(analysis.candidates, [])
        self.assertEqual(self.reasons(analysis), {("mmu_unit unit0", "toolhead"): "not set by one menuconfig value"})

    def test_version_the_upgrade_sets_isnt_an_edit(self):
        # A config saved by an older release still holds its version until olddefconfig runs
        with open(self.kconfig) as f:
            text = f.read()
        with open(self.kconfig, "w") as f:
            f.write(re.sub(r'(?m)^CONFIG_HAPPY_HARE_VERSION=.*$', 'CONFIG_HAPPY_HARE_VERSION="4.0.0"', text))
        analysis = self.analyse()
        self.assertEqual((analysis.candidates, analysis.kept), ([], []))

    def test_symbol_this_parse_lacks_is_left_in_the_cfg(self):
        # e.g. a per-gate symbol from an environment this parse doesn't share
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "123")
        lines = cfg_import.symbol_lines

        def missing(rendered):
            found = lines(rendered)
            found[("mmu_unit_parameters unit0", "gear_load_speed")] = ("PARAM_NOT_IN_THIS_PARSE_0", False)
            return found
        with mock.patch.object(cfg_import, "symbol_lines", missing):
            analysis = self.analyse()
        self.assertEqual(analysis.candidates, [])
        self.assertEqual(self.reasons(analysis), {("mmu_unit_parameters unit0", "gear_load_speed"):
                                                  "not set by one menuconfig value"})

    def test_computed_value_is_left_in_the_cfg(self):
        self.edit("mmu_hardware_unit0.cfg", "exit_leds", "neopixel:_unit0_leds (1-3)")
        self.assertEqual(self.reasons(self.analyse()),
                         {("mmu_leds unit0", "exit_leds"): "not set by one menuconfig value"})

    def test_edit_to_an_option_menuconfig_always_sets_is_listed_as_replaced(self):
        self.edit("mmu_hardware_unit0.cfg", "buffer", "other", section="mmu_unit unit0")
        analysis = self.analyse()
        self.assertEqual((analysis.candidates, analysis.kept), ([], []))
        self.assertEqual(analysis.replaced, [(("mmu_unit unit0", "buffer"), "other", "unit0")])

    def test_value_mmu_config_cant_hold_is_kept(self):
        # install.sh and make read .mmu_config as shell and make syntax
        for value in ("My\n    Box", "My $HOME", "My `id`"):
            self.edit("mmu_hardware_unit0.cfg", "display_name", value)
            analysis = self.analyse()
            self.assertNotIn("PARAM_DISPLAY_NAME", self.by_symbol(analysis), value)
            self.assertEqual(self.reasons(analysis)[("mmu_unit unit0", "display_name")],
                             "has a newline, $ or ` that .mmu_config can't hold")
            self.edit("mmu_hardware_unit0.cfg", "display_name", "BoxTurtle-0")

    def test_import_hidden_by_another_import_is_kept(self):
        # Each is fine alone, but max relief isn't shown once flowguard is off
        self.edit("mmu_parameters_unit0.cfg", "flowguard_enabled", "0")
        self.edit("mmu_parameters_unit0.cfg", "flowguard_max_relief", "45")
        analysis = self.analyse()
        self.assertEqual(list(self.by_symbol(analysis)), ["PARAM_FLOWGUARD_ENABLED"])
        self.assertEqual(self.reasons(analysis)[("mmu_unit_parameters unit0", "flowguard_max_relief")],
                         "not shown in menuconfig with the other imported values")

    def test_symbol_set_differently_in_two_places_is_reported(self):
        shared = [(sym, keys) for (sym, _literal), keys in self.symbol_uses().items() if len(keys) > 1]
        if not shared:
            self.skipTest("no symbol renders into two options")
        sym, keys = shared[0]
        name = {"mmu_parameters": "mmu.cfg"}.get(keys[0][0])
        if name is None:
            self.skipTest("shared symbol %s isn't in mmu.cfg" % sym)
        self.edit(name, keys[0][1], "77", section=keys[0][0])
        analysis = self.analyse()
        self.assertNotIn(sym, self.by_symbol(analysis))
        self.assertEqual(self.reasons(analysis)[keys[0]], "%s is set differently elsewhere" % sym)

    def symbol_uses(self):
        with cfg._env(self.env), cfg._chdir(cfg.INSTALLER):
            values = build.KConfig(self.kconfig).as_dict()
        values.update(self.extra)
        env = build._jinja_env(cfg.REPO_ROOT)
        uses = {}
        for template in SINGLE_UNIT_FILES:
            with open(os.path.join(cfg.REPO_ROOT, template)) as f:
                marked = env.from_string(cfg_import.mark_template(f.read())).render(values)
            for key, ref in cfg_import.symbol_lines(marked).items():
                if ref and not ref[0].startswith("PIN_"):
                    uses.setdefault(ref, []).append(key)
        return uses

    def test_old_default_still_in_the_cfg_is_imported(self):
        # The default was 77 when this install was built; a release has since changed it
        text = re.sub(r"(?m)^CONFIG_PARAM_GEAR_LOAD_SPEED=.*$",
                      "CONFIG_PARAM_GEAR_LOAD_SPEED=77" + DEFAULT_TOKEN, self.saved())
        with open(self.kconfig, "w") as f:
            f.write(text)
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "77")
        candidate = self.by_symbol(self.analyse())["PARAM_GEAR_LOAD_SPEED"]
        self.assertEqual((candidate.menuconfig_value, candidate.kconfig_value), ("100", "77"))

    def test_value_equal_to_the_default_isnt_imported(self):
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "100.0")
        self.assertNotIn("PARAM_GEAR_LOAD_SPEED", self.by_symbol(self.analyse()))


def cfg_import_rows(analysis):
    return analysis.report()["imported"]


class TestApply(_Install):

    def test_only_accepted_values_are_saved_and_other_defaults_stay_defaults(self):
        before = self.saved()
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "123")
        self.edit("mmu_parameters_unit0.cfg", "gear_unload_speed", "88")
        self.edit("mmu_macro_vars.cfg", "variable_home_mmu", "True")
        analysis = self.analyse()
        candidates = self.by_symbol(analysis)
        analysis.apply([candidates["PARAM_GEAR_LOAD_SPEED"], candidates["VAR_SOFTWARE_HOME_MMU"]])

        after = self.saved()
        self.assertIn("\nCONFIG_PARAM_GEAR_LOAD_SPEED=123\n", after)
        self.assertIn("\nCONFIG_BOOL_SOFTWARE_HOME_MMU=y\n", after)
        self.assertNotIn("CONFIG_PARAM_GEAR_UNLOAD_SPEED=88", after)

        def defaults(text):
            return set(re.findall(r"(?m)^(?:# )?CONFIG_([A-Z0-9_]+)[= ].*%s$" % re.escape(DEFAULT_TOKEN), text))
        # Only the accepted values stop being recorded defaults
        self.assertEqual(defaults(before) - defaults(after),
                         {"PARAM_GEAR_LOAD_SPEED", "BOOL_SOFTWARE_HOME_MMU"} & defaults(before))

    def test_menuconfig_holds_the_imported_values(self):
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "123")
        self.edit("mmu.cfg", "log_file_level", "4")
        self.edit("mmu_macro_vars.cfg", "variable_user_pre_initialize_extension", '"MY_INIT"')

        before = self.build_file("config/base/mmu_parameters.cfg", "mmu_parameters_unit0.cfg")
        self.assertNotEqual(before.get("mmu_unit_parameters unit0", "gear_load_speed").strip(), "123")

        analysis = self.analyse()
        analysis.apply(analysis.candidates)

        params = self.build_file("config/base/mmu_parameters.cfg", "mmu_parameters_unit0.cfg")
        self.assertEqual(params.get("mmu_unit_parameters unit0", "gear_load_speed").strip(), "123")
        self.assertIn("\nCONFIG_CHOICE_LOG_FILE_LEVEL_STEPPER=y\n", self.saved())
        mmu = self.build_file("config/base/mmu.cfg", "mmu.cfg")
        self.assertEqual(mmu.get("mmu_parameters", "log_file_level").strip(), "4")
        macro_vars = self.build_file("config/base/mmu_macro_vars.cfg", "mmu_macro_vars.cfg")
        self.assertEqual(macro_vars.get("gcode_macro _MMU_SOFTWARE_VARS",
                                        "variable_user_pre_initialize_extension").strip(), '"MY_INIT"')


class TestPins(_Install):
    """Pins are imported like any other value Refresh keeps in the .cfg files"""

    def test_plain_pin_is_imported(self):
        self.edit("mmu_hardware_unit0.cfg", "step_pin", "unit0:PA9", section="mmu_stepper unit0_gear")
        analysis = self.analyse()
        candidate = self.by_symbol(analysis)["PIN_GEAR_STEP"]
        self.assertEqual(candidate.kconfig_value, "unit0:PA9")
        analysis.apply([candidate])
        self.assertIn('\nCONFIG_PIN_GEAR_STEP="unit0:PA9"\n', self.saved())
        hardware = self.build_file("config/base/mmu_hardware.cfg", "mmu_hardware_unit0.cfg")
        self.assertEqual(hardware.get("mmu_stepper unit0_gear", "step_pin").strip(), "unit0:PA9")

    def test_per_gate_pin_maps_to_its_gate(self):
        self.edit("mmu_hardware_unit0.cfg", "mmu_entry_switch_pin_1", "^unit0:PA10", section="mmu_sensors unit0")
        candidate = self.by_symbol(self.analyse())["PIN_ENTRY_SENSOR_1"]
        self.assertEqual(candidate.locations, [("mmu_sensors unit0", "mmu_entry_switch_pin_1")])

    def test_invalid_pin_is_kept_not_listed(self):
        self.edit("mmu_hardware_unit0.cfg", "dir_pin", "foo bar", section="mmu_stepper unit0_gear")
        analysis = self.analyse()
        self.assertEqual(analysis.candidates, [])
        self.assertEqual([(key, reason) for key, _, reason in analysis.kept],
                         [(("mmu_stepper unit0_gear", "dir_pin"), "rejected by menuconfig's validation")])

    def test_pin_that_switches_other_config_is_kept(self):
        # Without a diag pin the template drops the gear steppers' extra_endstops
        self.edit("mmu_hardware_unit0.cfg", "diag_pin", "", section="tmc2209 mmu_stepper unit0_gear")
        analysis = self.analyse()
        self.assertEqual(analysis.candidates, [])
        reason = analysis.kept[0][2]
        self.assertTrue(reason.startswith("would also change [mmu_stepper unit0_gear] extra_endstops"), reason)


class TestEnvironmentSensors(_Install):
    """Per-gate [temperature_sensor] options, including those a blank value leaves out"""

    PROFILE = "emu"

    def add(self, section, option, value):
        installed = ConfigBuilder(self.path("mmu_hardware_unit0.cfg"))
        self.assertFalse(installed.has_option(section, option))
        installed.set(section, option, value)
        with open(self.path("mmu_hardware_unit0.cfg"), "w") as f:
            f.write(installed.write())

    def test_blank_temperature_limits_are_imported(self):
        # Copy-back drops managed options the template omits, so only an import keeps them
        self.add("temperature_sensor unit0_Env0", "min_temp", "0")
        self.add("temperature_sensor unit0_Env0", "max_temp", "100")
        analysis = self.analyse()
        candidates = self.by_symbol(analysis)
        self.assertEqual(candidates["PARAM_ENVIRONMENT_SENSOR_MIN_TEMP_0"].kconfig_value, "0")
        self.assertEqual(candidates["PARAM_ENVIRONMENT_SENSOR_MAX_TEMP_0"].kconfig_value, "100")
        analysis.apply(analysis.candidates)
        hardware = self.build_file("config/base/mmu_hardware.cfg", "mmu_hardware_unit0.cfg", mode="refresh")
        self.assertEqual(hardware.get("temperature_sensor unit0_Env0", "min_temp").strip(), "0")
        self.assertEqual(hardware.get("temperature_sensor unit0_Env0", "max_temp").strip(), "100")

    def test_limits_that_differ_by_sensor_stay_in_the_cfg(self):
        # One symbol sets every MCU sensor's limit, so these can't be imported but must be kept
        self.add("temperature_sensor unit0_mcu0", "max_temp", "80")
        self.add("temperature_sensor unit0_mcu1", "max_temp", "90")
        analysis = self.analyse()
        self.assertNotIn("PARAM_MCU_ENVIRONMENT_SENSOR_MAX_TEMP", self.by_symbol(analysis))
        self.assertEqual(self.reasons(analysis)[("temperature_sensor unit0_mcu1", "max_temp")],
                         "PARAM_MCU_ENVIRONMENT_SENSOR_MAX_TEMP is set differently elsewhere")
        analysis.apply(analysis.candidates)
        hardware = self.build_file("config/base/mmu_hardware.cfg", "mmu_hardware_unit0.cfg", mode="refresh")
        self.assertEqual(hardware.get("temperature_sensor unit0_mcu0", "max_temp").strip(), "80")
        self.assertEqual(hardware.get("temperature_sensor unit0_mcu1", "max_temp").strip(), "90")

    def test_per_gate_report_time_is_imported(self):
        # [[ (PARAM_SENSOR_REPORT_TIME_PARAM_|d)[i] ]]: the option name is itself indexed
        self.edit("mmu_hardware_unit0.cfg", "aht10_report_time", "10", section="temperature_sensor unit0_Env1")
        candidate = self.by_symbol(self.analyse())["PARAM_ENVIRONMENT_SENSOR_REPORT_TIME_1"]
        self.assertEqual(candidate.locations, [("temperature_sensor unit0_Env1", "aht10_report_time")])


class TestGearOverrides(_Install):
    """A gear whose override is on renders its values from its own per-gate symbols"""

    SYMS = {
        "BOOL_GEAR_OVERRIDE_1": True,
        "PARAM_GEAR_RUN_CURRENT_1": "0.6",
        "PARAM_GEAR_MICROSTEPS_1": "32",
    }

    def test_tmc_value_is_imported_to_its_gate(self):
        self.edit("mmu_hardware_unit0.cfg", "run_current", "0.7", section="tmc2209 mmu_stepper unit0_gear_1")
        analysis = self.analyse()
        candidate = self.by_symbol(analysis)["PARAM_GEAR_RUN_CURRENT_1"]
        self.assertEqual(candidate.locations, [("tmc2209 mmu_stepper unit0_gear_1", "run_current")])
        self.assertEqual((candidate.set_name, candidate.kconfig_value), ("PARAM_GEAR_RUN_CURRENT_1", "0.7"))
        analysis.apply([candidate])
        self.assertIn('\nCONFIG_PARAM_GEAR_RUN_CURRENT_1="0.7"\n', self.saved())
        hardware = self.build_file("config/base/mmu_hardware.cfg", "mmu_hardware_unit0.cfg")
        self.assertEqual(hardware.get("tmc2209 mmu_stepper unit0_gear_1", "run_current").strip(), "0.7")

    def test_stepper_value_is_imported_to_its_gate(self):
        self.edit("mmu_hardware_unit0.cfg", "microsteps", "64", section="mmu_stepper unit0_gear_1")
        candidate = self.by_symbol(self.analyse())["PARAM_GEAR_MICROSTEPS_1"]
        self.assertEqual((candidate.set_name, candidate.kconfig_value), ("PARAM_GEAR_MICROSTEPS_1", "64"))

    def test_stallguard_is_imported_to_its_gate(self):
        self.edit("mmu_hardware_unit0.cfg", "driver_SGTHRS", "77", section="tmc2209 mmu_stepper unit0_gear_1")
        candidate = self.by_symbol(self.analyse())["PARAM_GEAR_STALLGUARD_THRESHOLD_1"]
        self.assertEqual(candidate.locations, [("tmc2209 mmu_stepper unit0_gear_1", "driver_SGTHRS")])
        self.assertEqual(candidate.kconfig_value, "77")

    def test_stallguard_on_a_gear_without_override_is_gear_0s(self):
        self.edit("mmu_hardware_unit0.cfg", "driver_SGTHRS", "77", section="tmc2209 mmu_stepper unit0_gear_2")
        analysis = self.analyse()
        self.assertEqual(analysis.candidates, [])
        self.assertEqual(self.reasons(analysis)[("tmc2209 mmu_stepper unit0_gear_2", "driver_SGTHRS")],
                         "PARAM_GEAR_STALLGUARD_THRESHOLD is set differently elsewhere")


class TestCli(_Install):

    def setUp(self):
        super().setUp()
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "123")
        self.edit("mmu_parameters_unit0.cfg", "gear_unload_speed", "88")
        self.edit("mmu_macro_vars.cfg", "variable_home_mmu", "True")
        self.edit("mmu_hardware_unit0.cfg", "toolhead", "other", section="mmu_unit unit0")
        self.edit("mmu_hardware_unit0.cfg", "dir_pin", "foo bar", section="mmu_stepper unit0_gear")
        self.report = os.path.join(self.tmp.name, "report")

    def run_cli(self, *args, env=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), cfg._env(env or self.env), cfg._chdir(cfg.INSTALLER):
            status = cfg_import.main(list(args))
        return status, out.getvalue()

    def report_pass(self, kconfig=None, env=None):
        return self.run_cli("--kconfig", kconfig or self.kconfig, "--config-home", self.home,
                            "--report", self.report, env=env)

    def show(self, verbose=False):
        out = io.StringIO()
        with cfg._env(NO_COLOUR):
            status = cfg_import.show(self.report, verbose, out=out)
        return status, out.getvalue()

    def test_report_writes_nothing_to_menuconfig(self):
        before = self.saved()
        self.assertEqual(self.report_pass()[0], cfg_import.EXIT_NOTHING)
        self.assertEqual(self.saved(), before)
        self.assertEqual(len(cfg_import.read_report(self.report)), 1)

    def test_show_lists_edits_by_file_and_section(self):
        self.report_pass()
        status, out = self.show()
        self.assertEqual(status, cfg_import.EXIT_SHOWN)
        self.assertIn(
            "These .cfg values will be imported into menuconfig:\n"
            "\n"
            "> mmu_macro_vars.cfg:\n"
            "[gcode_macro _MMU_SOFTWARE_VARS]\n"
            "variable_home_mmu: True\n"
            "\n"
            "> mmu_parameters_unit0.cfg:\n"
            "[mmu_unit_parameters unit0]\n"
            "gear_load_speed: 123\n"
            "gear_unload_speed: 88\n", out)
        self.assertNotIn("toolhead", out)
        self.assertNotIn("dir_pin", out)

    def test_verbose_show_says_what_each_value_replaces(self):
        self.report_pass()
        _, out = self.show(verbose=True)
        self.assertIn("gear_load_speed: 123 (was 100; PARAM_GEAR_LOAD_SPEED)\n", out)
        self.assertIn("variable_home_mmu: True (was False; VAR_SOFTWARE_HOME_MMU, set by BOOL_SOFTWARE_HOME_MMU)\n",
                      out)
        self.assertIn("These .cfg values stay in the .cfg files but can't be imported into menuconfig:\n"
                      "\n"
                      "> mmu_hardware_unit0.cfg:\n"
                      "[mmu_unit unit0]\n"
                      "toolhead: other (not set by one menuconfig value)\n"
                      "\n"
                      "[mmu_stepper unit0_gear]\n"
                      "dir_pin: foo bar (rejected by menuconfig's validation)\n", out)

    def test_apply_imports_what_was_reported(self):
        self.report_pass()
        status, out = self.run_cli("--kconfig", self.kconfig, "--apply", self.report)
        self.assertEqual(status, cfg_import.EXIT_NOTHING)
        self.assertIn("Imported 3 value(s)", out)
        self.assertIn("\nCONFIG_PARAM_GEAR_LOAD_SPEED=123\n", self.saved())
        self.assertIn("\nCONFIG_PARAM_GEAR_UNLOAD_SPEED=88\n", self.saved())
        self.assertIn("\nCONFIG_BOOL_SOFTWARE_HOME_MMU=y\n", self.saved())

    def test_report_saves_the_baseline_with_defaults_recomputed(self):
        baseline = os.path.join(self.tmp.name, "baseline")
        before = self.saved()
        self.run_cli("--kconfig", self.kconfig, "--config-home", self.home, "--report", self.report,
                     "--baseline", baseline)
        self.assertEqual(self.saved(), before)
        with open(os.path.join(baseline, ".mmu_config")) as f:
            saved = f.read()
        self.assertIn("CONFIG_PARAM_GEAR_LOAD_SPEED=100" + DEFAULT_TOKEN, saved)

    def test_apply_rewrites_the_baseline_with_the_imported_values(self):
        baseline = os.path.join(self.tmp.name, "baseline")
        self.run_cli("--kconfig", self.kconfig, "--config-home", self.home, "--report", self.report,
                     "--baseline", baseline)
        self.run_cli("--kconfig", self.kconfig, "--apply", self.report, "--baseline", baseline)
        with open(os.path.join(baseline, ".mmu_config")) as f:
            self.assertEqual(f.read(), self.saved())
        self.assertIn("\nCONFIG_PARAM_GEAR_LOAD_SPEED=123\n", self.saved())

    def test_apply_rewrites_the_baseline_of_a_file_with_nothing_imported(self):
        # A unit's defaults can follow values another file just imported
        baseline = os.path.join(self.tmp.name, "baseline")
        os.makedirs(baseline)
        with open(self.report, "w") as f:
            f.write('{"kconfig": "/other", "imported": [], "kept": [], "settings": []}\n')
        self.run_cli("--kconfig", self.kconfig, "--apply", self.report, "--baseline", baseline)
        self.assertTrue(os.path.exists(os.path.join(baseline, ".mmu_config")))

    def test_edits_menuconfig_replaces_are_always_shown(self):
        self.edit("mmu_hardware_unit0.cfg", "buffer", "other", section="mmu_unit unit0")
        self.report_pass()
        _, out = self.show()
        self.assertIn("These .cfg values are always set by menuconfig, so your edits will be replaced:\n"
                      "\n"
                      "> mmu_hardware_unit0.cfg:\n"
                      "[mmu_unit unit0]\n"
                      "buffer: other\n", out)
        _, out = self.show(verbose=True)
        self.assertIn("buffer: other (menuconfig sets unit0)\n", out)

    def test_apply_ignores_other_files_entries(self):
        self.report_pass()
        other = os.path.join(self.tmp.name, ".mmu_config_other")
        with open(other, "w") as f:
            f.write(self.saved())
        before = self.saved()
        self.assertEqual(self.run_cli("--kconfig", other, "--apply", self.report)[1], "")
        self.assertEqual(self.saved(), before)

    def test_nothing_to_show_asks_nothing(self):
        with open(self.report, "w") as f:
            f.write('{"kconfig": "/x", "imported": [], "kept": [], "settings": []}\n')
        self.assertEqual(self.show(), (cfg_import.EXIT_NOTHING, ""))

    def test_failed_report_fails_open(self):
        before = self.saved()
        with mock.patch.object(cfg_import, "Analysis", side_effect=RuntimeError("boom")):
            self.assertEqual(self.report_pass()[0], cfg_import.EXIT_FAILED)
        status, out = self.show()
        self.assertEqual(status, cfg_import.EXIT_NOTHING)
        self.assertIn("continuing without", out)
        self.assertEqual(self.saved(), before)

    def test_failed_report_still_saves_the_baseline(self):
        baseline = os.path.join(self.tmp.name, "baseline")
        with mock.patch.object(cfg_import, "Analysis", side_effect=RuntimeError("boom")):
            self.run_cli("--kconfig", self.kconfig, "--config-home", self.home, "--report", self.report,
                         "--baseline", baseline)
        self.assertTrue(os.path.exists(os.path.join(baseline, ".mmu_config")))

    def test_report_without_an_install_does_nothing(self):
        empty = os.path.join(self.tmp.name, "empty")
        status, _ = self.run_cli("--kconfig", self.kconfig, "--config-home", empty, "--report", self.report)
        self.assertEqual(status, cfg_import.EXIT_NOTHING)
        self.assertFalse(os.path.exists(self.report))


class _Pipeline(object):
    """
    install.sh's interactive Refresh, step by step: report (saving baselines), optionally
    apply, menuconfig, the post-menuconfig olddefconfig, then the build's pre-parse and
    build. Compared with a plain Refresh build of the same saved config
    """

    def out(self):
        return os.path.join(self.tmp.name, "out")

    def baselines(self):
        return os.path.join(self.out(), "menuconfig_base")

    def import_pass(self, files, apply):
        report = os.path.join(self.tmp.name, "report")
        for kconfig, env in files:
            with cfg._env(env), cfg._chdir(cfg.INSTALLER), contextlib.redirect_stdout(io.StringIO()):
                cfg_import.main(["--kconfig", kconfig, "--config-home", self.home, "--report", report,
                                 "--baseline", self.baselines()])
        if apply:
            for kconfig, env in files:
                with cfg._env(env), cfg._chdir(cfg.INSTALLER), contextlib.redirect_stdout(io.StringIO()):
                    cfg_import.main(["--kconfig", kconfig, "--apply", report, "--baseline", self.baselines()])

    def menuconfig(self, kconfig, env, **syms):
        """What saving menuconfig does: recompute defaults, set what was changed, write"""
        with cfg._env(env), cfg._chdir(cfg.INSTALLER):
            kconf = build.KConfig(kconfig)
            kconf.load_config(kconfig, filter_defaults=True)
            for name, value in syms.items():
                kconf.syms[name].set_value(value)
            kconf.write_config(kconfig)

    olddefconfig = menuconfig

    def build_all(self, targets, build_env, baseline):
        """{installed name: text} built from the pre-parsed configs, as make install does"""
        out = self.out()
        with cfg._env(dict(build_env, OUT=out)), cfg._chdir(cfg.INSTALLER):
            for kconfig in {k for k, _, _ in targets}:
                build.pre_parse_kconfig(kconfig)
                if baseline:
                    base = os.path.join(self.baselines(), os.path.basename(kconfig))
                    build.pre_parse_kconfig(base, base + ".pickle")
        dest_dir = os.path.join(out, "mmu", "base")
        os.makedirs(dest_dir, exist_ok=True)
        built = {}
        env = dict(build_env, OUT=out, F_CFG_UPGRADE_MODE="refresh", F_MENUCONFIG_BASELINE="y" if baseline else "")
        with cfg._env(env), cfg._chdir(cfg.REPO_ROOT), self.assertNoLogs(level="ERROR"):
            for kconfig, template, name in targets:
                kcfg = build.load_parsed_kconfig(kconfig)
                build.build_config_file(template, os.path.join(dest_dir, name), kcfg,
                                        cfg_import.installed_files(self.home), self.extra)
                with open(os.path.join(dest_dir, name)) as f:
                    built[name] = f.read()
        return built


class TestRefreshPipeline(_Pipeline, _Install):

    def targets(self):
        return [(self.kconfig, template, name.format(unit="unit0")) for template, name in SINGLE_UNIT_FILES.items()]

    def files(self):
        return [(self.kconfig, self.env)]

    def test_accepted_import_without_menuconfig_changes_builds_as_plain_refresh(self):
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "123")
        self.edit("mmu_hardware_unit0.cfg", "step_pin", "unit0:PA9", section="mmu_stepper unit0_gear")
        self.import_pass(self.files(), apply=True)
        self.menuconfig(self.kconfig, self.env)
        self.olddefconfig(self.kconfig, self.env)
        self.assertEqual(self.build_all(self.targets(), self.env, baseline=True),
                         self.build_all(self.targets(), self.env, baseline=False))

    def test_declined_import_and_unsaved_menuconfig_build_as_plain_refresh(self):
        # A default saved by an older release (55) and a hand edit (66) the user declines to import
        text = re.sub(r"(?m)^CONFIG_PARAM_GEAR_UNLOAD_SPEED=.*$",
                      "CONFIG_PARAM_GEAR_UNLOAD_SPEED=55" + DEFAULT_TOKEN, self.saved())
        with open(self.kconfig, "w") as f:
            f.write(text)
        self.edit("mmu_parameters_unit0.cfg", "gear_unload_speed", "66")
        self.import_pass(self.files(), apply=False)

        # Menuconfig quit without saving leaves the raw file, which olddefconfig must
        # bring in line with the baseline or the stale default reads as a change
        raw = self.build_all(self.targets(), self.env, baseline=True)
        self.assertIn("gear_unload_speed           : 55", raw["mmu_parameters_unit0.cfg"])
        self.olddefconfig(self.kconfig, self.env)
        built = self.build_all(self.targets(), self.env, baseline=True)
        self.assertEqual(built, self.build_all(self.targets(), self.env, baseline=False))
        self.assertIn("gear_unload_speed           : 66", built["mmu_parameters_unit0.cfg"])

    def test_only_what_menuconfig_changed_is_applied(self):
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "123")
        self.edit("mmu_hardware_unit0.cfg", "toolhead", "other", section="mmu_unit unit0")
        self.import_pass(self.files(), apply=True)
        self.menuconfig(self.kconfig, self.env, PARAM_GEAR_UNLOAD_SPEED="77")
        self.olddefconfig(self.kconfig, self.env)
        built = self.build_all(self.targets(), self.env, baseline=True)
        plain = self.build_all(self.targets(), self.env, baseline=False)

        params = ConfigBuilder()
        params.read_buf(built["mmu_parameters_unit0.cfg"])
        self.assertEqual(params.get("mmu_unit_parameters unit0", "gear_unload_speed").strip(), "77")
        self.assertEqual(params.get("mmu_unit_parameters unit0", "gear_load_speed").strip(), "123")
        self.assertIn("toolhead                 : other", built["mmu_hardware_unit0.cfg"])
        # Nothing else differs from a plain Refresh, which ignores the menuconfig change
        changed = [name for name in built if built[name] != plain[name]]
        self.assertEqual(changed, ["mmu_parameters_unit0.cfg"])


class TestMissingHardwarePins(_Pipeline, _Install):
    SYMS = {"MMU_HAS_FANS": True, "PIN_FAN": "",
            "MMU_HAS_EJECT_BUTTONS": True, "PIN_EJECT_BUTTON_0": ""}

    def targets(self):
        return [(self.kconfig, template, name.format(unit="unit0")) for template, name in SINGLE_UNIT_FILES.items()]

    def files(self):
        return [(self.kconfig, self.env)]

    def add_hardware(self, excluded=False, pin="unit0:PA8"):
        path = self.path("mmu_hardware_unit0.cfg")
        with open(path) as f:
            text = f.read()
        fragment = ("[fan_generic _unit0_fan]\npin: %s\nmax_power: 0.6\nkick_start_time: 0.25\n"
                    "cycle_time: 0.02\n\n[gcode_button unit0_eject0]\n"
                    "pin: ^unit0:PA9\npress_gcode: MMU_EJECT UNIT=unit0 LGATE=0\n\n") % pin
        marker = "# EXCLUDE FROM CONFIG BUILDER"
        self.assertIn(marker, text)
        with open(path, "w") as f:
            f.write(text + fragment if excluded else text.replace(marker, fragment + marker, 1))

    def test_missing_pins_and_fan_settings_are_reported_without_mutating_config(self):
        self.add_hardware()
        before = self.saved()
        analysis = self.analyse()
        candidates = self.by_symbol(analysis)
        self.assertEqual(candidates["PIN_FAN"].cfg_value, "unit0:PA8")
        self.assertEqual(candidates["PIN_EJECT_BUTTON_0"].cfg_value, "^unit0:PA9")
        self.assertEqual(candidates["PARAM_FAN_MAX_POWER"].cfg_value, "0.6")
        self.assertEqual(candidates["PARAM_FAN_KICK_START_TIME"].cfg_value, "0.25")
        self.assertEqual(self.saved(), before)
        self.assertEqual(analysis.kconf.get("PIN_FAN"), "")

    def test_accepted_import_preserves_hardware_and_explicit_removal_still_works(self):
        self.add_hardware()
        self.import_pass(self.files(), apply=True)
        self.menuconfig(self.kconfig, self.env)
        built = self.build_all(self.targets(), self.env, baseline=True)
        hardware = ConfigBuilder()
        hardware.read_buf(built["mmu_hardware_unit0.cfg"])
        self.assertEqual(hardware.get("fan_generic _unit0_fan", "pin"), "unit0:PA8")
        self.assertEqual(hardware.get("fan_generic _unit0_fan", "max_power"), "0.6")
        self.assertEqual(hardware.get("fan_generic _unit0_fan", "kick_start_time"), "0.25")
        self.assertEqual(hardware.get("fan_generic _unit0_fan", "cycle_time"), "0.02")
        self.assertEqual(hardware.get("mmu_unit unit0", "fan"), "")
        self.assertEqual(hardware.get("gcode_button unit0_eject0", "pin"), "^unit0:PA9")
        self.menuconfig(self.kconfig, self.env, PIN_FAN="", PIN_EJECT_BUTTON_0="")
        removed = self.build_all(self.targets(), self.env, baseline=True)
        hardware = ConfigBuilder()
        hardware.read_buf(removed["mmu_hardware_unit0.cfg"])
        self.assertFalse(hardware.has_section("fan_generic _unit0_fan"))
        self.assertFalse(hardware.has_section("gcode_button unit0_eject0"))

    def test_declined_import_keeps_empty_pin_in_baseline(self):
        self.add_hardware()
        self.import_pass(self.files(), apply=False)
        with cfg._env(self.env), cfg._chdir(cfg.INSTALLER):
            baseline = build.KConfig(os.path.join(self.baselines(), ".mmu_config"))
        self.assertEqual(baseline.get("PIN_FAN"), "")
        self.assertEqual(baseline.get("PIN_EJECT_BUTTON_0"), "")

    def test_user_owned_sections_are_not_imported(self):
        self.add_hardware(excluded=True)
        self.assertNotIn("PIN_FAN", self.by_symbol(self.analyse()))

    def test_invalid_or_multi_pin_is_not_imported(self):
        self.add_hardware(pin="multi_pin:custom_fans")
        self.assertNotIn("PIN_FAN", self.by_symbol(self.analyse()))
        self.edit("mmu_hardware_unit0.cfg", "pin", "foo bar", "fan_generic _unit0_fan")
        self.assertNotIn("PIN_FAN", self.by_symbol(self.analyse()))


class TestMissingPerGateFanPin(_Pipeline, _Install):
    PROFILE = "emu"
    SYMS = {"PIN_FAN_0": ""}

    def test_only_matching_gate_is_imported_and_disabled_gate_stays_disabled(self):
        path = self.path("mmu_hardware_unit0.cfg")
        with open(path) as f:
            text = f.read()
        fragment = "[fan_generic _unit0_fan0]\npin: unit0:PA8\nmax_power: 0.6\n\n"
        with open(path, "w") as f:
            f.write(text.replace("# EXCLUDE FROM CONFIG BUILDER",
                                 fragment + "# EXCLUDE FROM CONFIG BUILDER", 1))
        candidates = self.by_symbol(self.analyse())
        self.assertEqual(candidates["PIN_FAN_0"].cfg_value, "unit0:PA8")
        self.assertEqual(candidates["PARAM_FAN_MAX_POWER_0"].cfg_value, "0.6")
        self.assertNotIn("PIN_FAN_1", candidates)
        self.menuconfig(self.kconfig, self.env, PARAM_FAN_GATE_0="n")
        self.assertNotIn("PIN_FAN_0", self.by_symbol(self.analyse()))


class TestAdditionalHardwarePins(_Pipeline, _Install):
    SYMS = {"MMU_HAS_HEATER": True, "MMU_HAS_HEATER_FANS": True,
            "PARAM_FILAMENT_HEATER": "unit0_heater",
            "MMU_HAS_CONTROLLER_FAN": True, "MMU_HAS_VENT_SERVO": True,
            "PIN_HEATER_FAN": "unit0:PA8", "PIN_CONTROLLER_FAN": "unit0:PA9",
            "PIN_VENT_SERVO": "unit0:PA10"}
    SUFFIX = ""

    def setUp(self):
        super().setUp()
        self.devices = []
        hardware = ConfigBuilder(self.path("mmu_hardware_unit0.cfg"))
        for kind, section_type, option, value in (
                ("HEATER_FAN", "heater_fan", "max_power", "0.6"),
                ("CONTROLLER_FAN", "controller_fan", "idle_timeout", "75"),
                ("VENT_SERVO", "mmu_servo", "maximum_servo_angle", "160")):
            symbol = "PIN_" + kind + self.SUFFIX
            pin = self.SYMS[symbol]
            sections = [section for section in hardware.sections(scope="included")
                        if section.startswith(section_type + " ") and
                        hardware.get(section, "pin") == pin]
            self.assertEqual(len(sections), 1, symbol)
            section = sections[0]
            self.edit("mmu_hardware_unit0.cfg", option, value, section)
            self.devices.append((symbol, section, pin, option, value))
        self.menuconfig(self.kconfig, self.env, **{d[0]: "" for d in self.devices})

    def test_import_and_deliberate_removal(self):
        analysis = self.analyse()
        candidates = self.by_symbol(analysis)
        for symbol, section, pin, option, value in self.devices:
            self.assertEqual(candidates[symbol].locations, [(section, "pin")])
            self.assertEqual(candidates[symbol].cfg_value, pin)
        self.import_pass([(self.kconfig, self.env)], apply=True)
        targets = [(self.kconfig, "config/base/mmu_hardware.cfg", "mmu_hardware_unit0.cfg")]
        result = self.build_all(targets, self.env, baseline=True)
        hardware = ConfigBuilder()
        hardware.read_buf(result["mmu_hardware_unit0.cfg"])
        for symbol, section, pin, option, value in self.devices:
            self.assertEqual(hardware.get(section, "pin"), pin)
            self.assertEqual(hardware.get(section, option), value)
        self.menuconfig(self.kconfig, self.env, **{d[0]: "" for d in self.devices})
        result = self.build_all(targets, self.env, baseline=True)
        hardware = ConfigBuilder()
        hardware.read_buf(result["mmu_hardware_unit0.cfg"])
        for _, section, _, _, _ in self.devices:
            self.assertFalse(hardware.has_section(section))

    def test_missing_heater_name_rejects_fan_without_leaking_trial_pin(self):
        self.menuconfig(self.kconfig, self.env, **{"PARAM_FILAMENT_HEATER" + self.SUFFIX: ""})
        analysis = self.analyse()
        self.assertNotIn("PIN_HEATER_FAN" + self.SUFFIX, self.by_symbol(analysis))
        self.assertEqual(analysis.kconf.get("PIN_HEATER_FAN" + self.SUFFIX), "")
        # A rejected trial must not prevent later hardware from being discovered.
        for kind in ("CONTROLLER_FAN", "VENT_SERVO"):
            self.assertIn("PIN_" + kind + self.SUFFIX, self.by_symbol(analysis))


class TestAdditionalPerGateHardwarePins(TestAdditionalHardwarePins):
    PROFILE = "emu"
    SUFFIX = "_0"
    SYMS = {"MMU_HAS_HEATER": True, "MMU_HAS_CONTROLLER_FAN": True,
            "MMU_HAS_VENT_SERVO": True, "PIN_HEATER_FAN_0": "unit0:PA8",
            "PIN_CONTROLLER_FAN_0": "unit0:PA9", "PIN_VENT_SERVO_0": "unit0:PA10"}


class _MultiUnitInstall(_Install):
    """Two BoxTurtles, installed in Replace mode"""

    UNITS = ("unit0", "unit1")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "config")
        os.makedirs(os.path.join(self.home, "mmu", "base"))
        self.kconfig = os.path.join(self.tmp.name, ".mmu_config")
        base = profiles.get(self.PROFILE)
        common = dict(cfg._SINGLE_UNIT_ENV, HH_VERSION=cfg.hh_version(), KCONFIG_CONFIG=self.kconfig)

        self.env = dict(common, F_MULTI_UNIT="y", F_MULTI_UNIT_ENTRY_POINT="y",
                        UNIT_NAME=",".join(self.UNITS), MCU_NAME=",".join(self.UNITS))
        with cfg._env(self.env):
            entry = cfg._kconfig("cfg-import", {"MMU_UNITS": ",".join(self.UNITS)})
        entry.write_config(self.kconfig)

        self.unit_envs, units = {}, {}
        for index, unit in enumerate(self.UNITS):
            self.unit_envs[unit] = dict(common, F_MULTI_UNIT="y", F_MULTI_UNIT_ENTRY_POINT="",
                                        UNIT_NAME=unit, MCU_NAME=unit, UNIT_INDEX=str(index),
                                        KCONFIG_CONFIG=self.unit_kconfig(unit), KCONFIG_PARENT=self.kconfig)
            with cfg._env(self.unit_envs[unit]):
                units[unit] = cfg._kconfig("cfg-import:" + unit, base.syms)
            units[unit].write_config(self.unit_kconfig(unit))

        self.extra = {"PARAM_TOTAL_NUM_GATES": sum(kc.getint("PARAM_NUM_GATES") for kc in units.values())}
        build_files(self.env, self.home, entry, cfg_import.SHARED_TEMPLATES, self.extra)
        for unit, kc in units.items():
            build_files(self.unit_envs[unit], self.home, kc, cfg_import.UNIT_TEMPLATES, self.extra, unit=unit)

    def unit_kconfig(self, unit):
        return "%s_%s" % (self.kconfig, unit)


class TestMultiUnit(_MultiUnitInstall):
    """An edit to one unit's file belongs to that unit's config only"""

    def test_edit_lands_in_its_unit_only(self):
        self.edit("mmu_parameters_unit1.cfg", "gear_load_speed", "123")
        self.assertEqual(self.analyse().candidates, [])
        self.assertEqual(self.analyse(self.unit_kconfig("unit0"), self.unit_envs["unit0"]).candidates, [])
        candidates = self.analyse(self.unit_kconfig("unit1"), self.unit_envs["unit1"]).candidates
        self.assertEqual([(c.sym_name, c.locations) for c in candidates],
                         [("PARAM_GEAR_LOAD_SPEED", [("mmu_unit_parameters unit1", "gear_load_speed")])])

    def test_one_summary_covers_every_unit(self):
        self.edit("mmu_parameters_unit1.cfg", "gear_load_speed", "123")
        self.edit("mmu_macro_vars.cfg", "variable_home_mmu", "True")
        report = os.path.join(self.tmp.name, "report")
        for kconfig, env in [(self.kconfig, self.env)] + \
                [(self.unit_kconfig(u), self.unit_envs[u]) for u in self.UNITS]:
            with cfg._env(env), cfg._chdir(cfg.INSTALLER):
                cfg_import.main(["--kconfig", kconfig, "--config-home", self.home, "--report", report])
        out = io.StringIO()
        with cfg._env(NO_COLOUR):
            self.assertEqual(cfg_import.show(report, out=out), cfg_import.EXIT_SHOWN)
        self.assertEqual(out.getvalue().count("will be imported into menuconfig"), 1)
        self.assertIn("> mmu_macro_vars.cfg:", out.getvalue())
        self.assertIn("> mmu_parameters_unit1.cfg:", out.getvalue())

    def test_report_says_which_file_it_is_checking(self):
        report = os.path.join(self.tmp.name, "report")
        said = []
        for kconfig, env in [(self.kconfig, self.env)] + \
                [(self.unit_kconfig(u), self.unit_envs[u]) for u in self.UNITS]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), cfg._env(dict(env, **NO_COLOUR)), cfg._chdir(cfg.INSTALLER):
                cfg_import.main(["--kconfig", kconfig, "--config-home", self.home, "--report", report])
            said.append(out.getvalue())
        self.assertEqual(said, ["  shared settings\n", "  unit 'unit0'\n", "  unit 'unit1'\n"])

    def test_shared_macro_variable_lands_in_the_top_level(self):
        self.edit("mmu_macro_vars.cfg", "variable_home_mmu", "True")
        self.assertEqual([c.sym_name for c in self.analyse().candidates], ["VAR_SOFTWARE_HOME_MMU"])
        self.assertEqual(self.analyse(self.unit_kconfig("unit0"), self.unit_envs["unit0"]).candidates, [])


def locations(kconfig, env):
    """(Kconfig, symbol_locations, choice_locations) for a saved config"""
    with cfg._env(env), cfg._chdir(cfg.INSTALLER):
        kconf = build.KConfig(kconfig)
        symbols = cfg_import.symbol_locations(kconf.as_dict(), kconfig)
        return kconf, symbols, cfg_import.choice_locations(kconf, symbols)


def installed_once(test_class, install_class):
    """Build one install for every test in the class"""
    install = install_class()
    install.setUp()
    test_class.addClassCleanup(install.doCleanups)
    return install


class TestSymbolLocations(unittest.TestCase):
    """Where each symbol menuconfig sets lands in the installed files, for its help label"""

    @classmethod
    def setUpClass(cls):
        install = installed_once(cls, _Install)
        _, cls.symbols, cls.choices = locations(install.kconfig, install.env)

    def test_plain_param(self):
        self.assertEqual(self.symbols["PARAM_GEAR_LOAD_SPEED"],
                         [("mmu_parameters_unit0.cfg", "mmu_unit_parameters unit0", "gear_load_speed")])

    def test_shared_file_keeps_its_name(self):
        self.assertEqual(self.symbols["VAR_SOFTWARE_HOME_MMU"],
                         [("mmu_macro_vars.cfg", "gcode_macro _MMU_SOFTWARE_VARS", "variable_home_mmu")])

    def test_per_gate_element_lands_in_its_gate(self):
        self.assertEqual(self.symbols["PIN_GEAR_DIR_1"],
                         [("mmu_hardware_unit0.cfg", "mmu_stepper unit0_gear_1", "dir_pin")])

    def test_symbol_used_on_every_gear_lists_each(self):
        where = self.symbols["PARAM_GEAR_STALLGUARD_THRESHOLD"]
        self.assertEqual(len(where), 4)
        self.assertEqual(where[0], ("mmu_hardware_unit0.cfg", "tmc2209 mmu_stepper unit0_gear", "driver_SGTHRS"))

    def test_toggles_and_capabilities_have_no_location(self):
        self.assertFalse([name for name in self.symbols if name.startswith(("BOOL_", "MMU_HAS_"))])

    def test_choice_lands_where_its_param_does(self):
        self.assertEqual(self.choices["CHOICE_LOG_LEVEL"], [("mmu.cfg", "mmu_parameters", "log_level")])

    def test_param_named_after_the_choice_comes_first(self):
        where = self.choices["CHOICE_GATE_HOMING_ENDSTOP"]
        self.assertEqual(where[0][2], "gate_homing_endstop")
        self.assertGreater(len(where), 1)


class TestChoiceLocations(unittest.TestCase):
    """Only a promptless symbol set by one choice's members alone takes the choice's label"""

    KCONFIG = """
choice CHOICE_LEVEL
    prompt "Level"
config CHOICE_LEVEL_LOW
    bool "Low"
config CHOICE_LEVEL_HIGH
    bool "High"
endchoice

config FLAG
    bool "Flag"

config PARAM_OTHER
    int
    default 5 if CHOICE_LEVEL_HIGH

config PARAM_LEVEL
    int
    default 0 if CHOICE_LEVEL_LOW
    default 1

config PARAM_PROMPTED
    int "Prompted"
    default 2 if CHOICE_LEVEL_HIGH

config PARAM_MIXED
    int
    default 3 if CHOICE_LEVEL_HIGH && FLAG

if CHOICE_LEVEL_HIGH
config PARAM_INSIDE
    int
    default 4
endif

config PARAM_SHARED
    int
    default 6 if CHOICE_LEVEL_LOW

if FLAG
config PARAM_SHARED
    int
    default 7 if PARAM_OTHER = 5
endif
"""

    NAMES = ("PARAM_OTHER", "PARAM_LEVEL", "PARAM_PROMPTED", "PARAM_MIXED", "PARAM_INSIDE", "PARAM_SHARED")

    def choice_locations(self, flag):
        import kconfiglib
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Kconfig")
            with open(path, "w") as f:
                f.write(self.KCONFIG)
            kconf = kconfiglib.Kconfig(path, warn=False)
        kconf.syms["FLAG"].set_value("y" if flag else "n")
        where = {name: [("mmu.cfg", "mmu_parameters", name.lower())] for name in self.NAMES}
        return [option for _, _, option in cfg_import.choice_locations(kconf, where).get("CHOICE_LEVEL", [])]

    def test_only_symbols_the_choice_alone_sets(self):
        self.assertEqual(self.choice_locations(flag=False), ["param_level", "param_other", "param_shared"])

    def test_a_definition_its_if_block_enables_counts(self):
        # Like PARAM_BUFFER_SPRING_STATE, whose shared-buffer definition only applies to a shared buffer
        self.assertNotIn("param_shared", self.choice_locations(flag=True))


class TestMultiUnitSymbolLocations(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.install = installed_once(cls, _MultiUnitInstall)

    def test_unit_parse_names_its_own_files(self):
        _, symbols, _ = locations(self.install.unit_kconfig("unit1"), self.install.unit_envs["unit1"])
        self.assertEqual(symbols["PARAM_GEAR_LOAD_SPEED"],
                         [("mmu_parameters_unit1.cfg", "mmu_unit_parameters unit1", "gear_load_speed")])
        self.assertNotIn("VAR_SOFTWARE_HOME_MMU", symbols)

    def test_entry_point_has_only_shared_files(self):
        _, symbols, _ = locations(self.install.kconfig, self.install.env)
        self.assertIn("VAR_SOFTWARE_HOME_MMU", symbols)
        self.assertEqual({f for where in symbols.values() for f, _, _ in where}, {"mmu.cfg", "mmu_macro_vars.cfg"})

    def test_entry_point_before_any_unit_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            kconfig = os.path.join(tmp, ".mmu_config")
            with open(self.install.kconfig) as f, open(kconfig, "w") as out:
                out.write(f.read())
            _, symbols, _ = locations(kconfig, dict(self.install.env, KCONFIG_CONFIG=kconfig))
        self.assertIn("VAR_SOFTWARE_HOME_MMU", symbols)


class TestMultiUnitRefreshPipeline(_Pipeline, _MultiUnitInstall):

    def test_no_menuconfig_changes_build_as_plain_refresh(self):
        self.edit("mmu_parameters_unit1.cfg", "gear_load_speed", "123")
        self.edit("mmu_macro_vars.cfg", "variable_home_mmu", "True")
        files = [(self.kconfig, self.env)] + [(self.unit_kconfig(u), self.unit_envs[u]) for u in self.UNITS]
        self.import_pass(files, apply=True)
        for kconfig, env in files:
            self.menuconfig(kconfig, env)
            self.olddefconfig(kconfig, env)

        # make install pre-parses and builds every file in the top-level environment
        build_env = dict(cfg._SINGLE_UNIT_ENV, HH_VERSION=cfg.hh_version(), KCONFIG_CONFIG=self.kconfig)
        targets = [(self.kconfig, t, os.path.basename(t)) for t in cfg_import.SHARED_TEMPLATES]
        for unit in self.UNITS:
            targets += [(self.unit_kconfig(unit), t, SINGLE_UNIT_FILES[t].format(unit=unit))
                        for t in cfg_import.UNIT_TEMPLATES]
        self.assertEqual(self.build_all(targets, build_env, baseline=True),
                         self.build_all(targets, build_env, baseline=False))


class TestCleanMultiUnitInstall(unittest.TestCase):
    """An untouched multi-unit install built the way make install builds it has nothing to import"""

    def test_nothing_listed(self):
        prof = profiles.SHARED_ENCODER
        rendered = cfg.render_via_files(prof)
        parent = os.path.join(cfg._scratch[0], ".mmu_config")
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "mmu", "base"))
            for key, text in rendered.items():
                with open(os.path.join(home, "mmu", "base", os.path.basename(key)), "w") as f:
                    f.write(text)
            names = [u.name for u in prof.units]
            common = dict(cfg._SINGLE_UNIT_ENV, HH_VERSION=cfg.hh_version(), F_MULTI_UNIT="y")
            runs = [(parent, dict(common, F_MULTI_UNIT_ENTRY_POINT="y", UNIT_NAME=",".join(names),
                                  MCU_NAME=",".join(names), KCONFIG_CONFIG=parent))]
            runs += [("%s_%s" % (parent, u.name),
                      dict(common, UNIT_NAME=u.name, MCU_NAME=u.mcu_name, UNIT_INDEX=str(u.index),
                           KCONFIG_CONFIG="%s_%s" % (parent, u.name), KCONFIG_PARENT=parent))
                     for u in prof.units]
            for kconfig, env in runs:
                with self.subTest(kconfig=os.path.basename(kconfig)), cfg._env(env), cfg._chdir(cfg.INSTALLER):
                    analysis = cfg_import.Analysis(kconfig, cfg_import.installed_files(home))
                    self.assertEqual(([c.sym_name for c in analysis.candidates], analysis.kept), ([], []))


if __name__ == "__main__":
    unittest.main()
