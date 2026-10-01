# Happy Hare installer tests: renaming, removing and reordering multi-unit MMU units.
#
# Each test builds a scratch checkout (.mmu_config + .mmu_config_<unit>) and a scratch
# installed config home (mmu/base/*.cfg, mmu_vars.cfg) and drives installer.unit_migration.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import io
import os
import tempfile
import unittest
from unittest.mock import patch

import sequence_edit
from installer import unit_migration as um

EXCLUDE = ("# EXCLUDE FROM CONFIG BUILDER -- IMPORTANT do not alter or remove this line. "
           "Config below is never upgraded")


class Scratch(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.kconfig = os.path.join(self.root, ".mmu_config")
        self.home = os.path.join(self.root, "config")
        os.makedirs(os.path.join(self.home, "mmu", "base"))
        self.vars_file = os.path.join(self.home, "mmu", "mmu_vars.cfg")

    # Checkout side

    def top(self, units, **extra):
        lines = ["CONFIG_MULTI_UNIT=y", 'CONFIG_MMU_UNITS="%s"' % units,
                 'CONFIG_KLIPPER_CONFIG_HOME="%s"' % self.home]
        lines += ["CONFIG_%s=%s" % kv for kv in extra.items()]
        self.write(self.kconfig, "\n".join(lines) + "\n")

    def unit(self, name, index=0, gates=4, lines=()):
        body = ['CONFIG_UNIT_NAME="%s"' % name, 'CONFIG_MCU_NAME="%s"' % name,
                "CONFIG_UNIT_INDEX=%d" % index, "CONFIG_PARAM_NUM_GATES=%d #~DEFAULT~#" % gates]
        self.write(um.unit_file(self.kconfig, name), "\n".join(body + list(lines)) + "\n")

    def edit(self, baseline, fn):
        """Edit the list as the sequence_editor would and save its sidecar."""
        model = sequence_edit.SequenceModel(baseline, list(baseline))
        fn(model)
        self.top(",".join(model.names()))
        sequence_edit.write_changes(sequence_edit.changes_path(self.kconfig, um.SYMBOL), model)
        return model

    # Installed side

    def install(self, units, gates=None, variables=None):
        gates = gates or {u: 4 for u in units}
        self.write(um._base(self.home, "mmu.cfg"),
                   "[mmu_machine]\nunits: %s\n\n[mmu_parameters]\ndefault_extruder_temp: 215\n"
                   % ",".join(units))
        self.write(um._base(self.home, "mmu_macro_vars.cfg"),
                   "[save_variables]\nfilename: %s\n" % self.vars_file)
        for u in units:
            self.write(um._base(self.home, "mmu_hardware_%s.cfg" % u),
                       "[mmu_unit %s]\nnum_gates: %d\n" % (u, gates[u]))
            self.write(um._base(self.home, "mmu_parameters_%s.cfg" % u),
                       "[mmu_unit_parameters %s]\n" % u)
        if variables is not None:
            um.write_vars(self.vars_file, dict(variables, mmu__revision=0))

    @staticmethod
    def write(path, text):
        with open(path, "w") as f:
            f.write(text)

    @staticmethod
    def read(path):
        with open(path) as f:
            return f.read()

    def kvalues(self, name=None):
        return um.read_kconfig(um.unit_file(self.kconfig, name) if name else self.kconfig)

    def check(self, mode="replace", base=None):
        out = io.StringIO()
        base = base if base is not None else um.baseline(self.kconfig, self.home)
        return um.check(self.kconfig, self.home, base, mode, out), out.getvalue()

    def run_all(self, base):
        um.migrate_kconfig(self.kconfig, base, io.StringIO())
        um.prepare(self.kconfig, self.home, base, io.StringIO())
        with patch.object(um, "stop_klipper"):
            um.apply(self.kconfig, out=io.StringIO())


class TestBaseline(Scratch):

    def test_installed_list_wins(self):
        self.top("a,b,c")
        self.install(["a", "b"])
        self.assertEqual(um.baseline(self.kconfig, self.home), ["a", "b"])

    def test_multi_unit_kconfig_when_not_installed(self):
        self.top("a,b")
        self.assertEqual(um.baseline(self.kconfig, self.home), ["a", "b"])

    def test_single_unit_kconfig_is_unit0(self):
        self.write(self.kconfig, 'CONFIG_MMU_UNITS="unit0"\n')
        self.assertEqual(um.baseline(self.kconfig, self.home), ["unit0"])

    def test_fresh(self):
        self.assertEqual(um.baseline(self.kconfig, self.home), [])

    def test_mmu_cfg_without_a_unit_list_is_not_an_install(self):
        # v3 layout: [mmu_machine] has no units option
        self.write(um._base(self.home, "mmu.cfg"), "[mmu_machine]\nnum_gates: 4\n")
        self.write(self.kconfig, 'CONFIG_MMU_UNITS="unit0"\n')
        self.assertIsNone(um.installed_units(self.home))
        self.assertEqual(um.baseline(self.kconfig, self.home), ["unit0"])

    def test_pending_state_wins_over_installed_list(self):
        self.install(["a"])
        um.write_state(self.kconfig, {"baseline": ["x"], "applied": {"x": "x"}})
        self.assertEqual(um.baseline(self.kconfig, self.home), ["x"])


class TestCheck(Scratch):

    def setUp(self):
        super().setUp()
        self.install(["a", "b"])
        self.unit("a", 0)
        self.unit("b", 1)

    def test_no_change(self):
        self.top("a,b")
        self.assertEqual(self.check("refresh")[0], um.EXIT_NONE)

    def test_append_allowed_in_any_mode(self):
        self.edit(["a", "b"], lambda m: m.add("c"))
        for mode in ("refresh", "merge", "replace"):
            with self.subTest(mode=mode):
                code, out = self.check(mode)
                self.assertEqual(code, um.EXIT_APPEND)
                self.assertIn("add c", out)

    def test_structural_refused_outside_replace(self):
        self.edit(["a", "b"], lambda m: m.rename(1, "box"))
        code, out = self.check("refresh")
        self.assertEqual(code, um.EXIT_REFUSED)
        self.assertIn("Replace", out)

    def test_structural_allowed_in_replace(self):
        self.edit(["a", "b"], lambda m: m.rename(1, "box"))
        code, out = self.check("replace")
        self.assertEqual(code, um.EXIT_STRUCTURAL)
        self.assertIn("rename b -> box", out)

    def test_the_saved_state_file_is_shown(self):
        self.edit(["a", "b"], lambda m: m.rename(1, "box"))
        self.assertIn("Saved state will be migrated in %s" % self.vars_file, self.check("replace")[1])

    def test_the_saved_state_file_falls_back_to_the_menuconfig_path(self):
        self.write(um._base(self.home, "mmu_macro_vars.cfg"), "[gcode_macro X]\ngcode:\n")
        self.edit(["a", "b"], lambda m: m.rename(1, "box"))
        with open(self.kconfig, "a") as f:
            f.write('CONFIG_PARAM_MMU_VARS_CFG="~/elsewhere_vars.cfg"\n')
        self.assertIn("Saved state will be migrated in %s" % os.path.expanduser("~/elsewhere_vars.cfg"),
                      self.check("replace")[1])

    def test_hand_edited_list_is_matched_by_name(self):
        self.top("a,box")
        code, out = self.check("replace")
        self.assertEqual(code, um.EXIT_STRUCTURAL)
        self.assertIn("remove b", out)
        self.assertIn("No renames were recorded", out)

    def test_removing_a_unit_another_one_shares_is_refused(self):
        self.unit("b", 1, lines=['CONFIG_PARAM_ENCODER_NAME="a"'])
        self.edit(["a", "b"], lambda m: m.remove(0))
        code, out = self.check("replace")
        self.assertEqual(code, um.EXIT_REFUSED)
        self.assertIn("PARAM_ENCODER_NAME", out)

    def test_removing_a_unit_whose_nfc_reader_is_shared_is_refused(self):
        self.unit("b", 1, lines=['CONFIG_PARAM_NFC_READER="a_nfc"'])
        self.edit(["a", "b"], lambda m: m.remove(0))
        code, out = self.check("replace")
        self.assertEqual(code, um.EXIT_REFUSED)
        self.assertIn("PARAM_NFC_READER", out)

    def test_a_similar_name_is_not_a_shared_reference(self):
        self.unit("b", 1, lines=['CONFIG_PARAM_NFC_READER="ab_nfc"', 'CONFIG_PIN_X="b:PA1"'])
        self.edit(["a", "b"], lambda m: m.remove(0))
        self.assertEqual(self.check("replace")[0], um.EXIT_STRUCTURAL)

    def test_unrecorded_divergence_outside_replace_is_left_alone(self):
        # E.g. renamed with the old free-text editor, which recorded nothing
        self.top("a,box")
        code, out = self.check("refresh")
        self.assertEqual(code, um.EXIT_UNTRACKED)
        self.assertIn("nothing is migrated", out)

    def test_a_recorded_change_outside_replace_is_refused_with_a_way_out(self):
        self.edit(["a", "b"], lambda m: m.remove(1))
        code, out = self.check("refresh")
        self.assertEqual(code, um.EXIT_REFUSED)
        self.assertIn("[u]", out)

    def test_duplicate_names_refused(self):
        self.top("a,a")
        self.assertEqual(self.check("replace")[0], um.EXIT_REFUSED)

    def test_loaded_filament_in_a_moving_gate_is_flagged(self):
        um.write_vars(self.vars_file, {"mmu_state_filament_pos": 10, "mmu_state_gate_selected": 1})
        self.edit(["a", "b"], lambda m: m.move(0, 1))
        self.assertIn("Unload it", self.check("replace")[1])

    def test_loaded_filament_is_flagged_when_any_gate_moves(self):
        # The selected gate 0 keeps its number, but removing b drops b's gates
        um.write_vars(self.vars_file, {"mmu_state_filament_pos": 10, "mmu_state_gate_selected": 0})
        self.edit(["a", "b"], lambda m: m.remove(1))
        out = self.check("replace")[1]
        self.assertIn("Unload it", out)
        self.assertIn("will be reset to unknown", out)

    def test_loaded_filament_in_a_gate_that_stays_is_not_flagged(self):
        um.write_vars(self.vars_file, {"mmu_state_filament_pos": 10, "mmu_state_gate_selected": 1})
        self.edit(["a", "b"], lambda m: m.rename(1, "box"))
        out = self.check("replace")[1]
        self.assertNotIn("Unload it", out)
        self.assertNotIn("will be reset", out)


class TestKconfigMigration(Scratch):

    def test_rename_moves_the_unit_file_and_rewrites_explicit_values(self):
        self.unit("a", 0, lines=[
            'CONFIG_PIN_GEAR_STEP="a:PB1"',
            'CONFIG_PIN_GEAR_DIR="a:PB2" #~DEFAULT~#',
            'CONFIG_PARAM_EXIT_LEDS="neopixel:_a_leds (1-4)"',
            'CONFIG_PARAM_ENDSTOP="tmc2209_a_gear:virtual_endstop"',
        ])
        self.unit("b", 1, lines=['CONFIG_PARAM_ENCODER_NAME="a"'])
        self.edit(["a", "b"], lambda m: m.rename(0, "left"))
        out = io.StringIO()
        um.migrate_kconfig(self.kconfig, ["a", "b"], out)

        self.assertFalse(os.path.exists(um.unit_file(self.kconfig, "a")))
        left = self.kvalues("left")
        self.assertEqual(left["UNIT_NAME"], "left")
        self.assertEqual(left["MCU_NAME"], "left")
        self.assertEqual(left["PIN_GEAR_STEP"], "left:PB1")
        self.assertEqual(left["PIN_GEAR_DIR"], "a:PB2")  # A default is recomputed on load
        self.assertEqual(left["PARAM_EXIT_LEDS"], "neopixel:_left_leds (1-4)")
        self.assertEqual(left["PARAM_ENDSTOP"], "tmc2209_a_gear:virtual_endstop")
        self.assertIn("PARAM_ENDSTOP", out.getvalue())  # ...but flagged
        self.assertEqual(self.kvalues("b")["PARAM_ENCODER_NAME"], "left")

    def test_a_longer_unit_name_is_not_mangled(self):
        self.unit("box", 0, lines=['CONFIG_PIN_X="box:PA1"'])
        self.unit("box_2", 1, lines=['CONFIG_PIN_X="box_2:PA1"', 'CONFIG_PARAM_ENCODER_NAME="box_2"'])
        self.edit(["box", "box_2"], lambda m: m.rename(0, "left"))
        um.migrate_kconfig(self.kconfig, ["box", "box_2"], io.StringIO())
        self.assertEqual(self.kvalues("left")["PIN_X"], "left:PA1")
        self.assertEqual(self.kvalues("box_2")["PIN_X"], "box_2:PA1")
        self.assertEqual(self.kvalues("box_2")["PARAM_ENCODER_NAME"], "box_2")

    def test_reorder_updates_unit_index(self):
        self.unit("a", 0)
        self.unit("b", 1)
        self.edit(["a", "b"], lambda m: m.move(0, 1))
        um.migrate_kconfig(self.kconfig, ["a", "b"], io.StringIO())
        self.assertEqual(self.kvalues("b")["UNIT_INDEX"], "0")
        self.assertEqual(self.kvalues("a")["UNIT_INDEX"], "1")

    def test_swapping_names_and_rerunning_is_stable(self):
        self.unit("a", 0, lines=['CONFIG_PIN_X="a:PA1"'])
        self.unit("b", 1, lines=['CONFIG_PIN_X="b:PA1"'])

        def swap(m):
            m.rename(0, "tmp")
            m.rename(1, "a")
            m.rename(0, "b")
        self.edit(["a", "b"], swap)
        for _ in range(2):
            um.migrate_kconfig(self.kconfig, ["a", "b"], io.StringIO())
            self.assertEqual(self.kvalues("b")["PIN_X"], "b:PA1")
            self.assertEqual(self.kvalues("b")["UNIT_INDEX"], "0")
            self.assertEqual(self.kvalues("a")["PIN_X"], "a:PA1")
            self.assertEqual(self.kvalues("a")["UNIT_INDEX"], "1")
        # The file that was unit 'a' is now called 'b' (first in the list)
        self.assertEqual(self.kvalues("b")["UNIT_NAME"], "b")

    def test_undoing_a_pending_rename_restores_the_file(self):
        self.unit("a", 0, lines=['CONFIG_PIN_X="a:PA1"'])
        model = self.edit(["a"], lambda m: m.rename(0, "left"))
        um.migrate_kconfig(self.kconfig, ["a"], io.StringIO())
        self.assertTrue(os.path.exists(um.unit_file(self.kconfig, "left")))

        model.reset()
        self.top("a")
        sequence_edit.write_changes(sequence_edit.changes_path(self.kconfig, um.SYMBOL), model)
        um.migrate_kconfig(self.kconfig, ["a"], io.StringIO())
        self.assertFalse(os.path.exists(um.unit_file(self.kconfig, "left")))
        self.assertEqual(self.kvalues("a")["PIN_X"], "a:PA1")

    def test_removed_unit_file_is_set_aside_and_can_come_back(self):
        self.unit("a", 0)
        self.unit("b", 1, lines=['CONFIG_PIN_X="b:PA1"'])
        model = self.edit(["a", "b"], lambda m: m.remove(1))
        um.migrate_kconfig(self.kconfig, ["a", "b"], io.StringIO())
        self.assertFalse(os.path.exists(um.unit_file(self.kconfig, "b")))
        self.assertTrue(os.path.exists(um.removed_file(self.kconfig, "b")))

        model.restore("b")
        self.top("a,b")
        sequence_edit.write_changes(sequence_edit.changes_path(self.kconfig, um.SYMBOL), model)
        um.migrate_kconfig(self.kconfig, ["a", "b"], io.StringIO())
        self.assertEqual(self.kvalues("b")["PIN_X"], "b:PA1")


class TestNoChange(Scratch):

    def test_a_fresh_install_is_not_a_change(self):
        # Nothing installed and no .mmu_config when the baseline was taken
        self.write(self.kconfig, 'CONFIG_MMU_UNITS="unit0"\n')
        self.assertEqual(self.check("refresh", base=[]), (um.EXIT_NONE, ""))
        self.top("a,b")
        self.assertEqual(self.check("refresh", base=[]), (um.EXIT_NONE, ""))
        um.migrate_kconfig(self.kconfig, [], io.StringIO())
        um.prepare(self.kconfig, self.home, [], io.StringIO())
        self.assertFalse(os.path.exists(um.state_path(self.kconfig)))

    def test_single_unit_is_never_a_change(self):
        self.install(["unit0"])
        self.write(self.kconfig, 'CONFIG_MMU_UNITS="unit0"\nCONFIG_PARAM_NUM_GATES=4\n')
        self.assertEqual(self.check("refresh"), (um.EXIT_NONE, ""))

    def test_an_unexpected_error_is_reported_not_raised(self):
        self.top("a,b")
        out = io.StringIO()
        with patch.object(um, "check", side_effect=RuntimeError("boom")), \
                patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            code = um.main(["check", "--kconfig", self.kconfig, "--config-home", self.home])
        self.assertEqual(code, um.EXIT_FAILED)
        self.assertIn("boom", out.getvalue())

    def test_an_unchanged_list_leaves_no_state(self):
        self.install(["a", "b"])
        self.unit("a", 0)
        self.unit("b", 1)
        self.top("a,b")
        um.migrate_kconfig(self.kconfig, ["a", "b"], io.StringIO())
        um.prepare(self.kconfig, self.home, ["a", "b"], io.StringIO())
        self.assertFalse(os.path.exists(um.state_path(self.kconfig)))


class TestVarsMigration(unittest.TestCase):

    @staticmethod
    def install(frm, to, origin, old_gates, new_gates):
        return {"from": frm, "to": to, "origin": origin, "old_gates": old_gates,
                "new_gates": new_gates, "default_extruder_temp": 215}

    def test_unit_keys_are_renamed_exactly(self):
        variables = {
            "mmu_box_bowden_lengths": [1, 2],
            "mmu_box_statistics_gate_1": {"x": 1},
            "mmu_box_encoder_resolution": 0.7,
            "mmu_box_2_bowden_lengths": [3, 4],
            "mmu_state_gate_status": [1, 1, 1, 1],
        }
        install = self.install(["box", "box_2"], ["left", "box_2"],
                               {"left": "box", "box_2": "box_2"},
                               {"box": 2, "box_2": 2}, {"left": 2, "box_2": 2})
        result, _ = um.migrate_vars(variables, install)
        self.assertEqual(result, {
            "mmu_left_bowden_lengths": [1, 2],
            "mmu_left_statistics_gate_1": {"x": 1},
            "mmu_left_encoder_resolution": 0.7,
            "mmu_box_2_bowden_lengths": [3, 4],
            "mmu_state_gate_status": [1, 1, 1, 1],
        })

    def test_removed_unit_keys_are_dropped_and_its_gates_cut_out(self):
        variables = {
            "mmu_a_bowden_lengths": [1, 2],
            "mmu_b_bowden_lengths": [3, 4],
            "mmu_state_gate_color": ["a0", "a1", "b0", "b1"],
            "mmu_state_tool_to_gate_map": [0, 1, 2, 3],
            "mmu_state_gate_selected": 1,
            "mmu_state_tool_selected": 1,
        }
        install = self.install(["a", "b"], ["b"], {"b": "b"}, {"a": 2, "b": 2}, {"b": 2})
        result, _ = um.migrate_vars(variables, install)
        self.assertNotIn("mmu_a_bowden_lengths", result)
        self.assertEqual(result["mmu_b_bowden_lengths"], [3, 4])
        self.assertEqual(result["mmu_state_gate_color"], ["b0", "b1"])
        self.assertEqual(result["mmu_state_tool_to_gate_map"], [0, 1])
        self.assertEqual(result["mmu_state_gate_selected"], -1)
        self.assertEqual(result["mmu_state_tool_selected"], -1)

    def test_reorder_moves_gate_maps_with_their_units(self):
        variables = {
            "mmu_state_gate_color": ["a0", "a1", "b0", "b1", "b2"],
            "mmu_state_gate_spool_id": [10, 11, 20, 21, 22],
            "mmu_state_tool_to_gate_map": [0, 1, 2, 3, 4],
            "mmu_state_endless_spool_groups": [0, 1, 0, 1, 2],
            "mmu_state_gate_selected": 3,
            "mmu_state_tool_selected": 2,
            "mmu_state_sensor_enabled": {"mmu_entry_0": False, "b:mmu_shared_exit": False,
                                         "mmu_exit_4": False},
        }
        install = self.install(["a", "b"], ["b", "a"], {"b": "b", "a": "a"},
                               {"a": 2, "b": 3}, {"a": 2, "b": 3})
        result, _ = um.migrate_vars(variables, install)
        self.assertEqual(result["mmu_state_gate_color"], ["b0", "b1", "b2", "a0", "a1"])
        self.assertEqual(result["mmu_state_gate_spool_id"], [20, 21, 22, 10, 11])
        # Tools keep their spools: T0 was gate 0 (a0), now gate 3
        self.assertEqual(result["mmu_state_tool_to_gate_map"], [3, 4, 0, 1, 2])
        self.assertEqual(result["mmu_state_endless_spool_groups"], [0, 1, 2, 0, 1])
        self.assertEqual(result["mmu_state_gate_selected"], -1)
        self.assertEqual(result["mmu_state_tool_selected"], -1)
        self.assertEqual(result["mmu_state_sensor_enabled"],
                         {"mmu_entry_3": False, "b:mmu_shared_exit": False, "mmu_exit_2": False})

    def test_append_pads_gate_maps(self):
        variables = {
            "mmu_state_gate_color": ["a0", "a1"],
            "mmu_state_gate_temperature": [200, 210],
            "mmu_state_gate_status": [1, 0],
            "mmu_state_endless_spool_groups": [0, 1],
            "mmu_state_tool_to_gate_map": [1, 0],
            "mmu_state_gate_selected": 1,
            "mmu_state_tool_selected": 0,
        }
        install = self.install(["a"], ["a", "c"], {"a": "a", "c": None}, {"a": 2}, {"a": 2, "c": 2})
        result, _ = um.migrate_vars(variables, install)
        # No existing gate was renumbered, so the selection stands
        self.assertEqual(result["mmu_state_gate_selected"], 1)
        self.assertEqual(result["mmu_state_tool_selected"], 0)
        self.assertEqual(result["mmu_state_gate_color"], ["a0", "a1", "", ""])
        self.assertEqual(result["mmu_state_gate_temperature"], [200, 210, 215, 215])
        self.assertEqual(result["mmu_state_gate_status"], [1, 0, -1, -1])
        self.assertEqual(result["mmu_state_endless_spool_groups"], [0, 1, 2, 3])
        self.assertEqual(result["mmu_state_tool_to_gate_map"], [1, 0, 2, 3])

    def test_gate_count_change_keeps_the_common_gates(self):
        variables = {"mmu_state_gate_color": ["a0", "a1", "a2", "b0"]}
        install = self.install(["a", "b"], ["a", "b"], {"a": "a", "b": "b"},
                               {"a": 3, "b": 1}, {"a": 2, "b": 1})
        result, _ = um.migrate_vars(variables, install)
        self.assertEqual(result["mmu_state_gate_color"], ["a0", "a1", "b0"])

    def test_a_rename_keeps_the_selection(self):
        variables = {"mmu_state_gate_selected": 3, "mmu_state_tool_selected": 3, "mmu_a_bowden_lengths": [1, 2]}
        install = self.install(["a", "b"], ["a", "box"], {"a": "a", "box": "b"},
                               {"a": 2, "b": 2}, {"a": 2, "box": 2})
        result, _ = um.migrate_vars(variables, install)
        self.assertEqual(result["mmu_state_gate_selected"], 3)
        self.assertEqual(result["mmu_state_tool_selected"], 3)

    def test_selection_is_reset_when_an_earlier_unit_changes(self):
        # The selected gate itself keeps its number but b's gates shift
        variables = {"mmu_state_gate_selected": 0, "mmu_state_tool_selected": 0}
        install = self.install(["a", "b"], ["a", "b"], {"a": "a", "b": "b"},
                               {"a": 2, "b": 2}, {"a": 3, "b": 2})
        result, notes = um.migrate_vars(variables, install)
        self.assertEqual(result["mmu_state_gate_selected"], -1)
        self.assertEqual(result["mmu_state_tool_selected"], -1)
        self.assertIn("Selected gate and tool reset to unknown", notes)

    def test_bypass_selection_is_reset_too(self):
        variables = {"mmu_state_gate_selected": -2, "mmu_state_tool_selected": -2}
        install = self.install(["a", "b"], ["b", "a"], {"b": "b", "a": "a"},
                               {"a": 2, "b": 2}, {"a": 2, "b": 2})
        result, _ = um.migrate_vars(variables, install)
        self.assertEqual(result["mmu_state_gate_selected"], -1)
        self.assertEqual(result["mmu_state_tool_selected"], -1)

    def test_identity_changes_nothing(self):
        variables = {"mmu_state_gate_color": ["a0", "a1"], "mmu_a_bowden_lengths": [1, 2]}
        install = self.install(["a"], ["a"], {"a": "a"}, {"a": 2}, {"a": 2})
        self.assertEqual(um.migrate_vars(variables, install)[0], variables)

    def test_unknown_gate_counts_leave_gate_maps_alone(self):
        variables = {"mmu_state_gate_color": ["a0", "b0"], "mmu_b_bowden_lengths": [1],
                     "mmu_state_gate_selected": 1}
        install = self.install(["a", "b"], ["b", "a"], {"b": "b", "a": "a"},
                               {"a": None, "b": 1}, {"a": 1, "b": 1})
        result, notes = um.migrate_vars(variables, install)
        self.assertEqual(result["mmu_state_gate_color"], ["a0", "b0"])
        self.assertTrue(any("not migrated" in n for n in notes))
        self.assertEqual(result["mmu_state_gate_selected"], -1)


class TestInstall(Scratch):

    def test_rename_end_to_end(self):
        self.install(["a", "b"], variables={
            "mmu_a_bowden_lengths": [1, 2, 3, 4],
            "mmu_state_gate_color": list("01234567"),
        })
        self.unit("a", 0)
        self.unit("b", 1)
        self.edit(["a", "b"], lambda m: m.rename(0, "left"))
        base = um.baseline(self.kconfig, self.home)
        self.run_all(base)

        variables = um.read_vars(self.vars_file)
        self.assertEqual(variables["mmu_left_bowden_lengths"], [1, 2, 3, 4])
        self.assertNotIn("mmu_a_bowden_lengths", variables)
        self.assertEqual(variables["mmu_state_gate_color"], list("01234567"))
        self.assertIn("mmu__revision", variables)
        for name in ("mmu_hardware_a.cfg", "mmu_parameters_a.cfg"):
            self.assertFalse(os.path.exists(um._base(self.home, name)))
        self.assertFalse(os.path.exists(um.state_path(self.kconfig)))
        self.assertFalse(os.path.exists(sequence_edit.changes_path(self.kconfig, um.SYMBOL)))
        # The mmu directory backup made by 'make install' already covers it
        backups = [f for f in os.listdir(os.path.dirname(self.vars_file)) if ".old-" in f]
        self.assertEqual(backups, [])

    def test_vars_outside_the_mmu_directory_get_their_own_backup(self):
        self.vars_file = os.path.join(self.home, "elsewhere_vars.cfg")
        self.install(["a"], variables={"mmu_a_bowden_lengths": [1]})
        self.unit("a", 0)
        self.edit(["a"], lambda m: m.rename(0, "left"))
        self.run_all(um.baseline(self.kconfig, self.home))
        self.assertEqual(len([f for f in os.listdir(self.home) if f.startswith("elsewhere_vars.cfg.old-")]), 1)
        self.assertIn("mmu_left_bowden_lengths", um.read_vars(self.vars_file))

    def test_nothing_to_do_leaves_everything_alone(self):
        self.install(["a"], variables={"mmu_a_bowden_lengths": [1]})
        self.unit("a", 0)
        self.top("a")
        before = self.read(self.vars_file)
        self.run_all(um.baseline(self.kconfig, self.home))
        self.assertEqual(self.read(self.vars_file), before)
        self.assertFalse(os.path.exists(um.state_path(self.kconfig)))

    def test_klipper_is_stopped_only_when_there_is_state_to_migrate(self):
        self.install(["a"], variables={})
        self.unit("a", 0)
        self.edit(["a"], lambda m: m.add("b"))
        base = um.baseline(self.kconfig, self.home)
        um.prepare(self.kconfig, self.home, base, io.StringIO())
        with patch.object(um, "stop_klipper") as stop:
            um.apply(self.kconfig, out=io.StringIO())
        stop.assert_called_once()
        with patch.object(um, "stop_klipper") as stop:
            um.apply(self.kconfig, out=io.StringIO())
        stop.assert_not_called()


class TestRenameIntoADeletedName(Scratch):
    """unit0, unit1, unit2 -> delete unit1 and unit2, then rename unit0 to unit1."""

    def setUp(self):
        super().setUp()
        self.install(["unit0", "unit1", "unit2"], gates={"unit0": 2, "unit1": 3, "unit2": 1}, variables={
            "mmu_unit0_bowden_lengths": [10, 11],
            "mmu_unit1_bowden_lengths": [20, 21, 22],
            "mmu_unit2_bowden_lengths": [30],
            "mmu_unit0_statistics_gate_1": {"u": 0},
            "mmu_unit1_statistics_gate_1": {"u": 1},
            "mmu_unit0_encoder_resolution": 0.5,
            "mmu_unit1_encoder_resolution": 0.9,
            "mmu_state_gate_color": ["a0", "a1", "b0", "b1", "b2", "c0"],
            "mmu_state_tool_to_gate_map": [5, 4, 3, 2, 1, 0],
            "mmu_state_sensor_enabled": {"unit0:mmu_shared_exit": False, "unit1:encoder": False},
        })
        self.unit("unit0", 0, gates=2, lines=['CONFIG_PIN_X="unit0:PA1"'])
        self.unit("unit1", 1, gates=3, lines=['CONFIG_PIN_X="unit1:PA1"'])
        self.unit("unit2", 2, gates=1, lines=['CONFIG_PIN_X="unit2:PA1"'])

        def edit(m):
            self.assertIsNone(m.remove(1))
            self.assertIsNone(m.remove(1))
            self.assertIsNone(m.rename(0, "unit1"))
        self.model = self.edit(["unit0", "unit1", "unit2"], edit)
        self.base = um.baseline(self.kconfig, self.home)

    def test_editor_records_it(self):
        self.assertEqual(self.model.origins(), {"unit1": "unit0"})
        self.assertEqual(self.model.removed, ["unit1", "unit2"])

    def test_check_allows_it_before_and_after_the_kconfig_step(self):
        self.assertEqual(self.check("replace", self.base)[0], um.EXIT_STRUCTURAL)
        um.migrate_kconfig(self.kconfig, self.base, io.StringIO())
        # A re-run (e.g. after an aborted install) must not mistake the renamed
        # unit's own new name for a reference to the deleted unit1
        code, out = self.check("replace", self.base)
        self.assertEqual(code, um.EXIT_STRUCTURAL, out)

    def test_sharing_the_deleted_units_encoder_is_still_refused(self):
        self.unit("unit0", 0, gates=2, lines=['CONFIG_PARAM_ENCODER_NAME="unit1"'])
        code, out = self.check("replace", self.base)
        self.assertEqual(code, um.EXIT_REFUSED)
        self.assertIn("PARAM_ENCODER_NAME", out)

    def test_end_to_end(self):
        self.run_all(self.base)

        # The old unit0 file is now unit1; old unit1 and unit2 are set aside
        self.assertEqual(self.kvalues("unit1")["PIN_X"], "unit1:PA1")
        self.assertEqual(self.kvalues("unit1")["UNIT_INDEX"], "0")
        self.assertEqual(um.read_kconfig(um.removed_file(self.kconfig, "unit1"))["PIN_X"], "unit1:PA1")
        self.assertTrue(os.path.exists(um.removed_file(self.kconfig, "unit2")))
        self.assertFalse(os.path.exists(um.unit_file(self.kconfig, "unit0")))
        self.assertFalse(os.path.exists(um.unit_file(self.kconfig, "unit2")))

        # unit0's saved state now lives under unit1; the old unit1/unit2 state is gone
        variables = um.read_vars(self.vars_file)
        self.assertEqual(variables["mmu_unit1_bowden_lengths"], [10, 11])
        self.assertEqual(variables["mmu_unit1_statistics_gate_1"], {"u": 0})
        self.assertEqual(variables["mmu_unit1_encoder_resolution"], 0.5)
        for key in variables:
            self.assertFalse(key.startswith(("mmu_unit0_", "mmu_unit2_")), key)
        self.assertEqual(variables["mmu_state_gate_color"], ["a0", "a1"])
        self.assertEqual(variables["mmu_state_tool_to_gate_map"], [0, 1])
        self.assertEqual(variables["mmu_state_sensor_enabled"], {"unit1:mmu_shared_exit": False})

        # Installed files of the unit names that no longer exist are removed
        for name in ("unit0", "unit2"):
            self.assertFalse(os.path.exists(um._base(self.home, "mmu_hardware_%s.cfg" % name)))
        self.assertTrue(os.path.exists(um._base(self.home, "mmu_hardware_unit1.cfg")))


class TestSingleUnitRename(Scratch):
    """A single unit is renamed through its own 'Klipper object name' prompt."""

    def single(self, name, mcu, *lines):
        body = ['CONFIG_UNIT_NAME="%s"' % name, 'CONFIG_MCU_NAME="%s"' % mcu, "CONFIG_UNIT_INDEX=0",
                "CONFIG_PARAM_NUM_GATES=4 #~DEFAULT~#",
                'CONFIG_KLIPPER_CONFIG_HOME="%s"' % self.home] + list(lines)
        self.write(self.kconfig, "\n".join(body) + "\n")

    def test_first_install_rename_fixes_the_kconfig_and_needs_no_migration(self):
        # Menuconfig ran with UNIT_NAME=unit0, so MCU_NAME and defaults still say unit0
        self.single("box", "unit0", 'CONFIG_PIN_X="unit0:PA1"',
                    'CONFIG_PIN_Y="unit0:PA2" #~DEFAULT~#')
        self.assertEqual(self.check("refresh", base=[]), (um.EXIT_NONE, ""))
        um.migrate_kconfig(self.kconfig, [], io.StringIO())
        values = self.kvalues()
        self.assertEqual(values["MCU_NAME"], "box")
        self.assertEqual(values["PIN_X"], "box:PA1")
        self.assertEqual(values["PIN_Y"], "unit0:PA2")   # Recomputed by the forced olddefconfig
        um.migrate_kconfig(self.kconfig, [], io.StringIO())
        self.assertEqual(self.kvalues()["PIN_X"], "box:PA1")

    def test_an_unchanged_single_unit_is_left_alone(self):
        self.install(["unit0"])
        self.single("unit0", "unit0")
        before = self.read(self.kconfig)
        self.assertEqual(self.check("refresh"), (um.EXIT_NONE, ""))
        um.migrate_kconfig(self.kconfig, ["unit0"], io.StringIO())
        um.prepare(self.kconfig, self.home, ["unit0"], io.StringIO())
        self.assertEqual(self.read(self.kconfig), before)
        self.assertFalse(os.path.exists(um.state_path(self.kconfig)))

    def test_baseline_of_an_uninstalled_single_unit_is_its_name(self):
        self.single("box", "box")
        self.assertEqual(um.baseline(self.kconfig, self.home), ["box"])

    def test_renaming_an_installed_single_unit_needs_replace(self):
        self.install(["unit0"])
        self.single("box", "unit0")
        code, out = self.check("refresh")
        self.assertEqual(code, um.EXIT_REFUSED)
        self.assertIn("set 'Klipper object name' back to 'unit0'", out)
        code, out = self.check("replace")
        self.assertEqual(code, um.EXIT_STRUCTURAL)
        self.assertIn("rename unit0 -> box", out)
        self.assertNotIn("will be reset", out)

    def test_renaming_an_installed_single_unit_end_to_end(self):
        self.install(["unit0"], variables={
            "mmu_unit0_bowden_lengths": [1, 2, 3, 4],
            "mmu_state_gate_color": ["r", "g", "b", "w"],
            "mmu_state_gate_selected": 2, "mmu_state_tool_selected": 1,
        })
        self.single("box", "unit0", 'CONFIG_PIN_X="unit0:PA1"')
        self.run_all(um.baseline(self.kconfig, self.home))

        self.assertEqual(self.kvalues()["PIN_X"], "box:PA1")
        variables = um.read_vars(self.vars_file)
        self.assertEqual(variables["mmu_box_bowden_lengths"], [1, 2, 3, 4])
        self.assertNotIn("mmu_unit0_bowden_lengths", variables)
        self.assertEqual(variables["mmu_state_gate_color"], ["r", "g", "b", "w"])
        # A rename doesn't renumber any gate, so the selection stands
        self.assertEqual(variables["mmu_state_gate_selected"], 2)
        self.assertEqual(variables["mmu_state_tool_selected"], 1)
        for name in ("mmu_hardware_unit0.cfg", "mmu_parameters_unit0.cfg"):
            self.assertFalse(os.path.exists(um._base(self.home, name)))
        self.assertFalse(os.path.exists(um.state_path(self.kconfig)))


class TestManualEditWarnings(Scratch):

    def test_excluded_blocks_and_printer_cfg_are_scanned(self):
        self.install(["a", "b"])
        with open(um._base(self.home, "mmu_hardware_a.cfg"), "a") as f:
            f.write("[mmu_unit a]\nnum_gates: 4\n%s\n[temperature_sensor a_enclosure]\ni2c_mcu: a\n" % EXCLUDE)
        self.write(os.path.join(self.home, "printer.cfg"),
                   "[gcode_macro EJECT]\ngcode: MMU_EJECT UNIT=a\n# UNIT=a in a comment\n")
        os.makedirs(os.path.join(self.home, "mmu", "addons"))
        self.write(os.path.join(self.home, "mmu", "addons", "mine.cfg"), "[mmu_led_effect x]\nunit: b\n")
        self.top("left,b")
        plan = um.Plan(self.kconfig, ["a", "b"])
        warnings = um.manual_edit_warnings(plan, self.home, self.kconfig)
        joined = "\n".join(warnings)
        self.assertIn("a_enclosure", joined)
        self.assertIn("i2c_mcu: a", joined)
        self.assertIn("MMU_EJECT UNIT=a", joined)
        self.assertNotIn("in a comment", joined)
        self.assertNotIn("unit: b", joined)  # 'b' isn't changing


class TestGateListWarnings(Scratch):

    def test_hand_set_lists_must_match_the_new_gate_count(self):
        path = um._base(self.home, "mmu.cfg")
        self.write(path, "[mmu_machine]\nunits: a,b\n\n[mmu_parameters]\n"
                         "default_gate_color: red, blue\nendless_spool_groups:\n")
        warnings = um.gate_list_warnings(path, 4)
        self.assertEqual(len(warnings), 1)
        self.assertIn("default_gate_color", warnings[0])
        self.assertEqual(um.gate_list_warnings(path, 2), [])


if __name__ == "__main__":
    unittest.main()
