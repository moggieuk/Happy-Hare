# Happy Hare installer: importing hand-edited .cfg values into menuconfig before a Merge.
#
# Each test builds a real install the way Replace does - a saved .mmu_config plus the base
# files rendered from it - then edits the installed files as a user would and checks what
# cfg_import offers, what it writes back, and what a Merge build makes of the result.
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

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "config")
        os.makedirs(os.path.join(self.home, "mmu", "base"))
        self.kconfig = os.path.join(self.tmp.name, ".mmu_config")
        self.env = dict(cfg._SINGLE_UNIT_ENV, HH_VERSION=cfg.hh_version(), KCONFIG_CONFIG=self.kconfig)
        with cfg._env(self.env):
            kc = cfg._kconfig("cfg-import", profiles.get(self.PROFILE).syms)
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
            m = re.match(r"^(%s\s*[:=]\s*)(\S.*?)(\s+[#;].*)?$" % re.escape(option), line)
            if m and not done and (section is None or current == section):
                lines[i] = m.group(1) + value + (m.group(3) or "")
                done = True
        self.assertTrue(done, "no %s in %s" % (option, name))
        with open(self.path(name), "w") as f:
            f.write("\n".join(lines))

    def analyse(self, kconfig=None, env=None):
        with cfg._env(env or self.env), cfg._chdir(cfg.INSTALLER):
            return cfg_import.Analysis(kconfig or self.kconfig, cfg_import.installed_files(self.home))

    def merge_build(self, template, name):
        """A Merge build of one file over the current install, returned parsed"""
        out = os.path.join(self.tmp.name, "merged")
        os.makedirs(os.path.join(out, "mmu", "base"), exist_ok=True)
        with cfg._env(self.env), cfg._chdir(cfg.INSTALLER):
            kcfg = build.KConfig(self.kconfig)
        with cfg._env(dict(self.env, OUT=out, F_CFG_UPGRADE_MODE="merge")), cfg._chdir(cfg.REPO_ROOT):
            build.build_config_file(template, os.path.join(out, "mmu", "base", name), kcfg,
                                    cfg_import.installed_files(self.home), self.extra)
        return ConfigBuilder(os.path.join(out, "mmu", "base", name))

    def saved(self):
        with open(self.kconfig) as f:
            return f.read()

    def by_symbol(self, analysis):
        return {c.sym_name: c for c in analysis.candidates}

    def reasons(self, analysis):
        return {key: reason for key, _value, reason in analysis.blocked}


class TestCandidates(_Install):

    def test_an_untouched_install_has_nothing_to_import(self):
        analysis = self.analyse()
        self.assertEqual(analysis.candidates, [])
        self.assertEqual(analysis.blocked, [])

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
                         cfg_import.HIDDEN)

    def test_capability_flag_is_never_used_as_a_toggle(self):
        # PARAM_LOGO_EFFECT is hidden and its defaults are conditional on MMU_HAS_LEDS
        self.edit("mmu_hardware_unit0.cfg", "logo_effect", "off")
        analysis = self.analyse()
        self.assertEqual([c.set_name for c in analysis.candidates], [])
        self.assertIn(("mmu_leds unit0", "logo_effect"), self.reasons(analysis))

    def test_value_that_changes_another_rendered_value_is_reported(self):
        # The LED chain length defaults from the gate count
        self.edit("mmu_hardware_unit0.cfg", "num_gates", "6", section="mmu_unit unit0")
        analysis = self.analyse()
        self.assertNotIn("PARAM_NUM_GATES", self.by_symbol(analysis))
        reason = self.reasons(analysis)[("mmu_unit unit0", "num_gates")]
        self.assertTrue(reason.startswith("would also change") and "PARAM_CHAIN_COUNT" in reason, reason)

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

    def test_option_merge_overwrites_by_name_alone_is_reported(self):
        # [mmu_unit] toolhead is a literal in the template, but shares PARAM_TOOLHEAD's name
        self.edit("mmu_hardware_unit0.cfg", "toolhead", "other", section="mmu_unit unit0")
        self.assertEqual(self.reasons(self.analyse())[("mmu_unit unit0", "toolhead")],
                         "not a plain menuconfig value")

    def test_symbol_set_differently_in_two_places_is_reported(self):
        shared = [(sym, keys) for (sym, _literal), keys in self.symbol_uses().items() if len(keys) > 1]
        if not shared:
            self.skipTest("no symbol renders into two options Merge overwrites")
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
        excluded_params, excluded_vars = build.merge_exclusions(values)
        uses = {}
        for template in SINGLE_UNIT_FILES:
            with open(os.path.join(cfg.REPO_ROOT, template)) as f:
                marked = env.from_string(cfg_import.mark_template(f.read())).render(values)
            for key, ref in cfg_import.symbol_lines(marked).items():
                if ref and build.kconfig_wins(key[0], key[1], excluded_params, set(excluded_vars)):
                    uses.setdefault(ref, []).append(key)
        return uses

    def test_changed_default_is_not_taken_for_a_hand_edit(self):
        # The saved default was 77 when this install was built; the current default differs
        text = re.sub(r"(?m)^CONFIG_PARAM_GEAR_LOAD_SPEED=.*$",
                      "CONFIG_PARAM_GEAR_LOAD_SPEED=77" + DEFAULT_TOKEN, self.saved())
        with open(self.kconfig, "w") as f:
            f.write(text)
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "77")
        analysis = self.analyse()
        self.assertNotIn("PARAM_GEAR_LOAD_SPEED", self.by_symbol(analysis))
        self.assertNotIn(("mmu_unit_parameters unit0", "gear_load_speed"), self.reasons(analysis))


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

    def test_merge_keeps_imported_values(self):
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "123")
        self.edit("mmu_macro_vars.cfg", "variable_user_pre_initialize_extension", '"MY_INIT"')

        reverted = self.merge_build("config/base/mmu_parameters.cfg", "mmu_parameters_unit0.cfg")
        self.assertNotEqual(reverted.get("mmu_unit_parameters unit0", "gear_load_speed").strip(), "123")

        analysis = self.analyse()
        analysis.apply(analysis.candidates)

        params = self.merge_build("config/base/mmu_parameters.cfg", "mmu_parameters_unit0.cfg")
        self.assertEqual(params.get("mmu_unit_parameters unit0", "gear_load_speed").strip(), "123")
        macro_vars = self.merge_build("config/base/mmu_macro_vars.cfg", "mmu_macro_vars.cfg")
        self.assertEqual(macro_vars.get("gcode_macro _MMU_SOFTWARE_VARS",
                                        "variable_user_pre_initialize_extension").strip(), '"MY_INIT"')


class TestReview(_Install):

    def setUp(self):
        super().setUp()
        self.edit("mmu_parameters_unit0.cfg", "gear_load_speed", "123")
        self.edit("mmu_parameters_unit0.cfg", "gear_unload_speed", "88")
        self.analysis = self.analyse()

    def review(self, *answers):
        with mock.patch("builtins.input", side_effect=list(answers)):
            return [c.sym_name for c in cfg_import.review(self.analysis, out=io.StringIO())]

    def test_declining_the_review_imports_nothing(self):
        self.assertEqual(self.review("n"), [])

    def test_each_value_is_asked_for(self):
        self.assertEqual(self.review("y", "n", "y"), ["PARAM_GEAR_UNLOAD_SPEED"])

    def test_all_and_quit(self):
        self.assertEqual(self.review("y", "a"), ["PARAM_GEAR_LOAD_SPEED", "PARAM_GEAR_UNLOAD_SPEED"])
        self.assertEqual(self.review("y", "y", "q"), ["PARAM_GEAR_LOAD_SPEED"])

    def test_end_of_input_imports_nothing(self):
        self.assertEqual(self.review(EOFError()), [])

    def test_main_imports_accepted_values(self):
        with mock.patch("builtins.input", side_effect=["y", "y", "n"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                cfg._env(self.env), cfg._chdir(cfg.INSTALLER):
            status = cfg_import.main(["--kconfig", self.kconfig, "--config-home", self.home])
        self.assertEqual(status, cfg_import.EXIT_NOTHING)
        self.assertIn("\nCONFIG_PARAM_GEAR_LOAD_SPEED=123\n", self.saved())
        self.assertNotIn("CONFIG_PARAM_GEAR_UNLOAD_SPEED=88", self.saved())

    def test_main_fails_open(self):
        before = self.saved()
        err = io.StringIO()
        with mock.patch.object(cfg_import, "Analysis", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            status = cfg_import.main(["--kconfig", self.kconfig, "--config-home", self.home])
        self.assertEqual(status, cfg_import.EXIT_FAILED)
        self.assertIn("continuing with Merge", err.getvalue())
        self.assertEqual(self.saved(), before)

    def test_main_without_an_install_does_nothing(self):
        empty = os.path.join(self.tmp.name, "empty")
        self.assertEqual(cfg_import.main(["--kconfig", self.kconfig, "--config-home", empty]),
                         cfg_import.EXIT_NOTHING)


class TestMultiUnit(_Install):
    """Two BoxTurtles: an edit to one unit's file belongs to that unit's config only"""

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

    def test_edit_lands_in_its_unit_only(self):
        self.edit("mmu_parameters_unit1.cfg", "gear_load_speed", "123")
        self.assertEqual(self.analyse().candidates, [])
        self.assertEqual(self.analyse(self.unit_kconfig("unit0"), self.unit_envs["unit0"]).candidates, [])
        candidates = self.analyse(self.unit_kconfig("unit1"), self.unit_envs["unit1"]).candidates
        self.assertEqual([(c.sym_name, c.locations) for c in candidates],
                         [("PARAM_GEAR_LOAD_SPEED", [("mmu_unit_parameters unit1", "gear_load_speed")])])

    def test_shared_macro_variable_lands_in_the_top_level(self):
        self.edit("mmu_macro_vars.cfg", "variable_home_mmu", "True")
        self.assertEqual([c.sym_name for c in self.analyse().candidates], ["VAR_SOFTWARE_HOME_MMU"])
        self.assertEqual(self.analyse(self.unit_kconfig("unit0"), self.unit_envs["unit0"]).candidates, [])


if __name__ == "__main__":
    unittest.main()
