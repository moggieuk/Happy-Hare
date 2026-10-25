# Happy Hare installer tests: renaming, removing and reordering multi-unit MMU units.
#
# Each test builds a scratch checkout (.mmu_config + .mmu_config_<unit>) and a scratch
# installed config home (mmu/base/*.cfg, mmu_vars.cfg) and drives installer.unit_migration.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import ast
import configparser
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import sequence_edit
from installer import unit_migration as um
sm = um.state_migration

def read_vars(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    return {k: ast.literal_eval(v) for k, v in parser.items('Variables')}


def write_vars(path, values):
    parser = configparser.ConfigParser(interpolation=None)
    parser['Variables'] = {k: repr(v) for k, v in values.items()}
    with open(path, 'w') as f:
        parser.write(f)


EXCLUDE = ("# EXCLUDE FROM CONFIG BUILDER -- IMPORTANT do not alter or remove this line. "
           "Config below is never upgraded")


ENCODER_OWNER = ["CONFIG_MMU_HAS_ENCODER=y"]
BUFFER_OWNER = ["CONFIG_MMU_HAS_SYNC_FEEDBACK_BUFFER=y"]
ENCODER_SHARER = ENCODER_OWNER + ["CONFIG_MMU_SHARED_ENCODER=y"]
BUFFER_SHARER = BUFFER_OWNER + ["CONFIG_MMU_SHARED_SYNC_FEEDBACK_BUFFER=y"]


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
        history = um.installed_option(self.home, 'mmu.cfg', 'mmu_machine', 'state_migration')
        printer = os.path.join(self.home, "printer.cfg")
        if not os.path.exists(printer):
            self.write(printer, "[include mmu/base/mmu_macro_vars.cfg]\n")
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
        if history:
            builder = um.ConfigBuilder(um._base(self.home, 'mmu.cfg'))
            builder.set('mmu_machine', 'state_migration', history)
            self.write(um._base(self.home, 'mmu.cfg'), builder.write())
        if variables is not None:
            write_vars(self.vars_file, dict(variables, mmu__revision=0))

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

    def history(self):
        return sm.parse_history(um.installed_option(self.home, 'mmu.cfg', 'mmu_machine', 'state_migration'))

    def applied_vars(self):
        history = self.history()
        return sm.apply_history(read_vars(self.vars_file), history['steps'][-1]['to'], history)[0]

    def publish_history(self):
        # Simulate the installed template; real rendering is tested separately below.
        path = um._base(self.home, 'mmu.cfg')
        builder = um.ConfigBuilder(path)
        builder.set('mmu_machine', 'state_migration', um.read_kconfig(self.kconfig).get('PARAM_STATE_MIGRATION', '{}'))
        self.write(path, builder.write())

    def run_all(self, base):
        um.migrate_kconfig(self.kconfig, base, io.StringIO())
        um.prepare(self.kconfig, self.home, base, io.StringIO())
        if os.path.exists(um._base(self.home, 'mmu.cfg')):
            self.publish_history()
        um.apply(self.kconfig, out=io.StringIO())


class TestConfigHistory(Scratch):
    def test_reinstall_with_history_expands_config_home(self):
        self.test_history_survives_chained_installs_and_plain_reinstall()
        history = self.history()
        with open(self.kconfig, 'a') as handle:
            handle.write('CONFIG_KLIPPER_CONFIG_HOME="~/config"\n')
        um.prepare(self.kconfig, self.home, ['c'], io.StringIO())
        self.publish_history()
        with patch('os.path.expanduser', side_effect=lambda path: self.home if path == '~/config' else path):
            self.assertEqual(um.main(['apply', '--kconfig', self.kconfig]), um.EXIT_NONE)
        self.assertEqual(self.history(), history)
        self.assertIsNone(um.read_state(self.kconfig))

    def test_mode_conversions_in_replace_record_new_unit_identity(self):
        for was_multi, baseline, current in ((True, ['a', 'b'], ['mmu']),
                                             (False, ['unit0'], ['a', 'b'])):
            with self.subTest(was_multi=was_multi):
                self.install(baseline, variables={'mmu_state_gate_spool_id': [5] * (4 * len(baseline))})
                self.write(os.path.join(self.home, 'mmu', '.mmu_config'),
                           'CONFIG_MULTI_UNIT=y\n' if was_multi else
                           '# CONFIG_MULTI_UNIT is not set\nCONFIG_UNIT_NAME="unit0"\n')
                if was_multi:
                    self.write(self.kconfig, 'CONFIG_UNIT_NAME="mmu"\nCONFIG_PARAM_NUM_GATES=4\n')
                else:
                    self.top(','.join(current))
                    for unit in current:
                        self.unit(unit)
                code, output = self.check('replace', base=baseline)
                self.assertEqual(code, um.EXIT_STRUCTURAL, output)
                self.assertIn('treated as new', output)
                self.assertNotEqual(self.check('refresh', base=baseline)[0], um.EXIT_STRUCTURAL)
                um.prepare(self.kconfig, self.home, baseline, io.StringIO())
                history = sm.parse_history(self.kvalues()['PARAM_STATE_MIGRATION'])
                self.assertEqual(history['steps'][-1]['origin'], {name: None for name in current})
                migrated = sm.migrate_state(read_vars(self.vars_file), [[name, 4] for name in current],
                                            {'mmu_state_gate_spool_id': [-1] * (4 * len(current))}, history)[0]
                self.assertEqual(migrated['mmu_state_gate_spool_id'], [-1] * (4 * len(current)))
                um.clear_state(self.kconfig)
                # Each scenario represents a separate original installation.
                self.write(um._base(self.home, 'mmu.cfg'), '[mmu_machine]\nunits: unit0\n')

    def test_mode_conversion_uses_custom_kconfig_basename(self):
        self.kconfig = os.path.join(self.root, 'custom_mmu_config')
        self.install(['unit0'])
        self.write(os.path.join(self.home, 'mmu', 'custom_mmu_config'),
                   '# CONFIG_MULTI_UNIT is not set\nCONFIG_UNIT_NAME="unit0"\n')
        # A stale default-named file must not override the selected configuration.
        self.write(os.path.join(self.home, 'mmu', '.mmu_config'), 'CONFIG_MULTI_UNIT=y\n')
        self.top('a,b')
        self.unit('a')
        self.unit('b', index=1)
        code, output = self.check('replace', base=['unit0'])
        self.assertEqual(code, um.EXIT_STRUCTURAL, output)
        self.assertIn('treated as new', output)
        self.assertNotEqual(self.check('refresh', base=['unit0'])[0], um.EXIT_STRUCTURAL)

    def test_fresh_and_parameter_only_installs_have_no_history(self):
        self.top('a')
        self.unit('a')
        um.prepare(self.kconfig, self.home, [], io.StringIO())
        self.assertNotIn('PARAM_STATE_MIGRATION', self.kvalues())
        self.assertIsNone(um.read_state(self.kconfig))
        self.install(['a'], variables={'mmu_a_bowden_lengths': [1, 2, 3, 4]})
        for count in (4, 6, 3):
            with self.subTest(count=count):
                self.unit('a', gates=count, lines=['CONFIG_PARAM_DEFAULT_EXTRUDER_TEMP=220'])
                um.prepare(self.kconfig, self.home, ['a'], io.StringIO())
                self.assertNotIn('PARAM_STATE_MIGRATION', self.kvalues())
                self.assertIsNone(um.read_state(self.kconfig))

    def test_history_survives_chained_installs_and_plain_reinstall(self):
        self.install(['a'], variables={'mmu_a_bowden_lengths': [1, 2, 3, 4]})
        self.unit('a')
        self.edit(['a'], lambda m: m.rename(0, 'b'))
        before = self.read(self.vars_file)
        self.run_all(['a'])
        first = self.history()
        self.assertEqual(self.read(self.vars_file), before)
        self.install(['b'])
        self.edit(['b'], lambda m: m.rename(0, 'c'))
        self.run_all(['b'])
        history = self.history()
        self.assertEqual(history['steps'][0], first['steps'][0])
        self.assertEqual(len(history['steps']), 2)
        self.assertEqual(self.read(self.vars_file), before)
        self.assertEqual(self.applied_vars()['mmu_c_bowden_lengths'], [1, 2, 3, 4])
        self.install(['c'])
        self.top('c')  # menuconfig/recovery without a locally retained history value
        self.run_all(['c'])
        self.assertEqual(self.history(), history)
        self.assertEqual(self.read(self.vars_file), before)

    def test_configured_checkpoint_trims_only_older_steps(self):
        self.test_history_survives_chained_installs_and_plain_reinstall()
        history = self.history()
        values = self.applied_vars()
        write_vars(self.vars_file, values)
        before = self.read(self.vars_file)
        self.top('c', PARAM_MMU_VARS_CFG=json.dumps(self.vars_file))
        self.run_all(['c'])
        self.assertEqual(self.history()['steps'], history['steps'][1:])
        self.assertEqual(self.read(self.vars_file), before)
        self.assertEqual(sm.apply_history(values, ['c'], self.history())[0], values)

    def test_trim_keeps_pending_steps_and_appends_new_change(self):
        self.test_history_survives_chained_installs_and_plain_reinstall()
        history = self.history()
        first = sm.apply_history(read_vars(self.vars_file), ['b'],
                                 dict(history, steps=history['steps'][:1]))[0]
        write_vars(self.vars_file, first)
        self.top('c', PARAM_MMU_VARS_CFG=json.dumps(self.vars_file))
        self.run_all(['c'])
        self.assertEqual(self.history(), history)
        values = sm.apply_history(first, ['c'], history)[0]
        write_vars(self.vars_file, values)
        self.edit(['c'], lambda m: m.rename(0, 'd'))
        with open(self.kconfig, 'a') as handle:
            handle.write('CONFIG_PARAM_MMU_VARS_CFG=%s\n' % json.dumps(self.vars_file))
        self.run_all(['c'])
        trimmed = self.history()
        self.assertEqual(len(trimmed['steps']), 2)
        self.assertEqual(trimmed['steps'][0], history['steps'][-1])
        self.assertEqual(trimmed['steps'][1]['to'], ['d'])
        migrated = sm.apply_history(values, ['d'], trimmed)[0]
        self.assertEqual(migrated['mmu_d_bowden_lengths'], [1, 2, 3, 4])
        self.assertEqual(read_vars(self.vars_file), values)

    def test_checkpoint_lookup_falls_back_without_trimming(self):
        self.test_history_survives_chained_installs_and_plain_reinstall()
        history = self.history()
        for contents in ('invalid ini', '[Variables]\n',
                         '[Variables]\nmmu_state_migration_applied = invalid literal\n',
                         '[Variables]\nmmu_state_migration_applied = []\n',
                         "[Variables]\nmmu_state_migration_applied = 'unknown'\n",
                         "[Variables]\nmmu_state_migration_applied = %r\nbroken = invalid literal\n"
                         % history['steps'][-1]['id']):
            with self.subTest(contents=contents):
                self.write(self.vars_file, contents)
                self.top('c', PARAM_MMU_VARS_CFG=json.dumps(self.vars_file))
                self.run_all(['c'])
                self.assertEqual(self.history(), history)
                self.assertEqual(self.read(self.vars_file), contents)
        os.remove(self.vars_file)
        self.run_all(['c'])
        self.assertEqual(self.history(), history)

    def test_history_is_not_rewritten_when_units_are_renamed_again(self):
        self.top('b')
        record = {'version': 1, 'steps': [{'from': ['a'], 'to': ['b']}]}
        um.write_history(self.kconfig, record)
        um.rewrite_kconfig(self.kconfig, {'a': 'x', 'b': 'c'}, {'a', 'b', 'c'})
        self.assertEqual(ast.literal_eval(um.read_kconfig(self.kconfig)['PARAM_STATE_MIGRATION']), record)

    def test_failed_history_publication_preserves_retry_id(self):
        self.install(['a'])
        self.unit('a')
        self.edit(['a'], lambda m: m.rename(0, 'b'))
        with patch.object(um, 'write_history', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                um.prepare(self.kconfig, self.home, ['a'])
        first = um.read_state(self.kconfig)['install']['migration_id']
        um.prepare(self.kconfig, self.home, ['a'])
        self.assertEqual(um.read_state(self.kconfig)['install']['migration_id'], first)
        self.assertEqual(len(sm.parse_history(um.read_kconfig(self.kconfig)['PARAM_STATE_MIGRATION'])['steps']), 1)



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

    def test_a_failure_prints_nothing_on_stdout(self):
        # install.sh captures stdout as the unit list
        out, err = io.StringIO(), io.StringIO()
        with patch.object(um, "baseline", side_effect=RuntimeError("boom")), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            status = um.main(["baseline", "--kconfig", self.kconfig, "--config-home", self.home])
        self.assertEqual(status, um.EXIT_FAILED)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("boom", err.getvalue())


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

    def test_renumbering_is_only_mentioned_when_gates_move(self):
        for label, fn, renumbered in (("rename", lambda m: m.rename(1, "box"), False),
                                      ("reorder", lambda m: m.move(0, 1), True),
                                      ("remove first", lambda m: m.remove(0), True)):
            with self.subTest(label):
                self.edit(["a", "b"], fn)
                code, out = self.check("replace")
                self.assertEqual(code, um.EXIT_STRUCTURAL)
                self.assertIn("saved state move with each unit", out)
                (self.assertIn if renumbered else self.assertNotIn)("renumbered", out)

    def test_history_destination_is_shown_without_a_variables_path(self):
        self.edit(["a", "b"], lambda m: m.rename(1, "box"))
        self.assertIn("Migration intent will be saved in mmu.cfg", self.check("replace")[1])

    def test_explicit_remove_and_add_is_allowed_as_replacement(self):
        self.edit(["a", "b"], lambda m: (m.remove(1), m.add("box")))
        code, out = self.check("replace")
        self.assertEqual(code, um.EXIT_STRUCTURAL)
        self.assertIn("remove b", out)
        self.assertIn("add box", out)
        self.assertNotIn("Unrecorded", out)

    def test_hand_edited_rename_requires_recorded_intent(self):
        self.top("a,box")
        code, out = self.check("replace")
        self.assertEqual(code, um.EXIT_REFUSED)
        self.assertIn("Unrecorded", out)
        self.assertIn("rename", out)

    def test_removing_a_unit_whose_encoder_another_one_shares_warns(self):
        self.unit("a", 0, lines=ENCODER_OWNER)
        self.unit("b", 1, lines=ENCODER_SHARER + ['CONFIG_PARAM_ENCODER_NAME="a"'])
        self.edit(["a", "b"], lambda m: m.remove(0))
        code, out = self.check("replace")
        self.assertEqual(code, um.EXIT_STRUCTURAL)
        self.assertIn("WARNING: Unit 'b' shares encoder 'a', which no unit owns after this change", out)

    def test_renaming_a_unit_whose_buffer_another_one_shares_warns(self):
        self.unit("a", 0, lines=BUFFER_OWNER)
        self.unit("b", 1, lines=BUFFER_SHARER + ['CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="a" #~DEFAULT~#'])
        self.edit(["a", "b"], lambda m: m.rename(0, "left"))
        for run in ("before", "after"):
            with self.subTest(run=run):
                code, out = self.check("replace")
                self.assertEqual(code, um.EXIT_STRUCTURAL)
                self.assertIn("WARNING: Unit 'b' shares buffer 'a', which is renamed to 'left'", out)
                um.migrate_kconfig(self.kconfig, ["a", "b"], io.StringIO())

    def test_renaming_a_unit_with_a_custom_component_name_does_not_warn(self):
        self.unit("a", 0, lines=BUFFER_OWNER + ['CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="box_buf"'])
        self.unit("b", 1, lines=BUFFER_SHARER + ['CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="box_buf"'])
        self.edit(["a", "b"], lambda m: m.rename(0, "left"))
        code, out = self.check("replace")
        self.assertEqual(code, um.EXIT_STRUCTURAL)
        self.assertNotIn("WARNING", out)

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

    def test_unrecorded_divergence_outside_replace_is_refused(self):
        # E.g. renamed with the old free-text editor, which recorded nothing
        self.top("a,box")
        code, out = self.check("refresh")
        self.assertEqual(code, um.EXIT_REFUSED)
        self.assertIn("Unrecorded", out)

    def test_a_recorded_change_outside_replace_is_refused_with_a_way_out(self):
        self.edit(["a", "b"], lambda m: m.remove(1))
        code, out = self.check("refresh")
        self.assertEqual(code, um.EXIT_REFUSED)
        self.assertIn("[u]", out)

    def test_duplicate_names_refused(self):
        self.top("a,a")
        self.assertEqual(self.check("replace")[0], um.EXIT_REFUSED)

    def test_loaded_filament_in_a_moving_gate_is_flagged(self):
        write_vars(self.vars_file, {"mmu_state_filament_pos": 10, "mmu_state_gate_selected": 1})
        self.edit(["a", "b"], lambda m: m.move(0, 1))
        self.assertIn("Unload filament", self.check("replace")[1])

    def test_loaded_filament_is_flagged_when_any_gate_moves(self):
        # The selected gate 0 keeps its number, but removing b drops b's gates
        write_vars(self.vars_file, {"mmu_state_filament_pos": 10, "mmu_state_gate_selected": 0})
        self.edit(["a", "b"], lambda m: m.remove(1))
        out = self.check("replace")[1]
        self.assertIn("Unload filament", out)
        self.assertIn("will be reset to unknown", out)

    def test_loaded_filament_in_a_gate_that_stays_is_not_flagged(self):
        write_vars(self.vars_file, {"mmu_state_filament_pos": 10, "mmu_state_gate_selected": 1})
        self.edit(["a", "b"], lambda m: m.rename(1, "box"))
        out = self.check("replace")[1]
        self.assertNotIn("Unload filament", out)
        self.assertNotIn("will be reset", out)


class TestKconfigMigration(Scratch):

    def test_rename_moves_the_unit_file_and_rewrites_explicit_values(self):
        self.unit("a", 0, lines=[
            'CONFIG_PIN_GEAR_STEP="a:PB1"',
            'CONFIG_PIN_GEAR_DIR="a:PB2" #~DEFAULT~#',
            'CONFIG_PARAM_EXIT_LEDS="neopixel:_a_leds (1-4)"',
            'CONFIG_PARAM_ENDSTOP="tmc2209_a_gear:virtual_endstop"',
        ])
        self.unit("b", 1, lines=ENCODER_OWNER + ['CONFIG_PARAM_ENCODER_NAME="a"'])
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
        # b's own encoder is called 'a', which isn't a reference to unit a
        self.assertEqual(self.kvalues("b")["PARAM_ENCODER_NAME"], "a")
        self.assertNotIn("PARAM_ENCODER_NAME", out.getvalue())

    def test_an_owners_name_follows_a_rename_only_when_it_is_the_unit_name(self):
        for line, after in (('CONFIG_PARAM_ENCODER_NAME="a"', "left"),
                            ('CONFIG_PARAM_ENCODER_NAME="a" #~DEFAULT~#', "left"),
                            ('CONFIG_PARAM_ENCODER_NAME="a_enc"', "a_enc")):
            with self.subTest(line=line):
                self.unit("a", 0, lines=ENCODER_OWNER + [line])
                self.unit("b", 1)
                if os.path.exists(um.state_path(self.kconfig)):
                    os.remove(um.state_path(self.kconfig))
                self.edit(["a", "b"], lambda m: m.rename(0, "left"))
                out = io.StringIO()
                um.migrate_kconfig(self.kconfig, ["a", "b"], out)
                self.assertEqual(self.kvalues("left")["PARAM_ENCODER_NAME"], after)
                self.assertNotIn("WARNING", out.getvalue())
                self.assertEqual(self.read(um.unit_file(self.kconfig, "left")).count("#~DEFAULT~#"),
                                 line.count("#~DEFAULT~#") + 1)

    def test_a_shared_name_is_never_rewritten(self):
        self.unit("a", 0, lines=ENCODER_OWNER + BUFFER_OWNER)
        self.unit("b", 1, lines=BUFFER_SHARER + ENCODER_SHARER[1:] + [
            'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="a" #~DEFAULT~#',
            'CONFIG_PARAM_ENCODER_NAME="a"'])
        self.edit(["a", "b"], lambda m: m.rename(0, "left"))
        out = io.StringIO()
        um.migrate_kconfig(self.kconfig, ["a", "b"], out)
        self.assertEqual(self.kvalues("b")["PARAM_SYNC_FEEDBACK_BUFFER_NAME"], "a")
        self.assertEqual(self.kvalues("b")["PARAM_ENCODER_NAME"], "a")
        self.assertNotIn("Check for an old unit name", out.getvalue())

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
        err = io.StringIO()
        with patch.object(um, "check", side_effect=RuntimeError("boom")), \
                patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            code = um.main(["check", "--kconfig", self.kconfig, "--config-home", self.home])
        self.assertEqual(code, um.EXIT_FAILED)
        self.assertIn("boom", err.getvalue())

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
    def install(frm, to, origin, old_gates, new_gates, encoders=None):
        return {"from": frm, "to": to, "origin": origin, "old_gates": old_gates,
                "new_gates": new_gates, "encoders": encoders or {}}

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
                               {"box": 2, "box_2": 2}, {"left": 2, "box_2": 2},
                               encoders={"box": "left"})
        result, _ = sm.migrate_unit_state(variables, install)
        self.assertEqual(result, {
            "mmu_left_bowden_lengths": [1, 2],
            "mmu_left_statistics_gate_1": {"x": 1},
            "mmu_left_encoder_resolution": 0.7,
            "mmu_box_2_bowden_lengths": [3, 4],
            "mmu_state_gate_status": [1, 1, 1, 1],
        })

    def test_encoder_keys_move_with_the_encoder_name(self):
        variables = {
            "mmu_box_encoder_resolution": 0.7,         # another unit's encoder called 'box'
            "mmu_box_enc_encoder_resolution": 0.8,
            "mmu_gone_encoder_clog_length": 9,
            "mmu_state_gate_status": [1, 1, 1, 1],
        }
        install = self.install(["box", "gone"], ["left"], {"left": "box"},
                               {"box": 4, "gone": 0}, {"left": 4},
                               encoders={"box_enc": "box_enc", "gone": None})
        result, _ = sm.migrate_unit_state(variables, install)
        self.assertEqual(result, {
            "mmu_box_encoder_resolution": 0.7,
            "mmu_box_enc_encoder_resolution": 0.8,
            "mmu_state_gate_status": [1, 1, 1, 1],
        })

    def test_removed_unit_keys_are_dropped_and_its_gates_left_for_startup(self):
        variables = {
            "mmu_a_bowden_lengths": [1, 2],
            "mmu_b_bowden_lengths": [3, 4],
            "mmu_state_gate_color": ["a0", "a1", "b0", "b1"],
            "mmu_state_tool_to_gate_map": [0, 1, 2, 3],
            "mmu_state_gate_selected": 1,
            "mmu_state_tool_selected": 1,
        }
        install = self.install(["a", "b"], ["b"], {"b": "b"}, {"a": 2, "b": 2}, {"b": 2})
        result, _ = sm.migrate_unit_state(variables, install)
        self.assertNotIn("mmu_a_bowden_lengths", result)
        self.assertEqual(result["mmu_b_bowden_lengths"], [3, 4])
        # Happy Hare cuts out the removed unit's gates at startup, matching by the saved layout
        self.assertEqual(result["mmu_state_gate_color"], ["a0", "a1", "b0", "b1"])
        self.assertEqual(result["mmu_state_tool_to_gate_map"], [0, 1, 2, 3])
        self.assertEqual(result["mmu_state_gate_map_units"], [["", 2], ["b", 2]])
        self.assertEqual(result["mmu_state_gate_selected"], -1)
        self.assertEqual(result["mmu_state_tool_selected"], -1)

    def test_reorder_leaves_gate_maps_for_startup(self):
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
        result, _ = sm.migrate_unit_state(variables, install)
        for var in ("mmu_state_gate_color", "mmu_state_gate_spool_id", "mmu_state_tool_to_gate_map",
                    "mmu_state_endless_spool_groups"):
            self.assertEqual(result[var], variables[var])
        # The layout still describes the lists, so Happy Hare can reorder them at startup
        self.assertEqual(result["mmu_state_gate_map_units"], [["a", 2], ["b", 3]])
        self.assertEqual(result["mmu_state_gate_selected"], -1)
        self.assertEqual(result["mmu_state_tool_selected"], -1)
        # Per-gate sensor keys are renumbered at startup too, with the gate lists
        self.assertEqual(result["mmu_state_sensor_enabled"], variables["mmu_state_sensor_enabled"])

    def test_a_reorder_keeps_the_saved_unit_layout(self):
        variables = {"mmu_state_gate_color": ["a0", "a1", "b0"],
                     "mmu_state_gate_map_units": [["a", 2], ["b", 1]]}
        install = self.install(["a", "b"], ["b", "a"], {"b": "b", "a": "a"}, {"a": 2, "b": 1}, {"a": 2, "b": 1})
        result, _ = sm.migrate_unit_state(variables, install)
        self.assertEqual(result["mmu_state_gate_color"], ["a0", "a1", "b0"])
        self.assertEqual(result["mmu_state_gate_map_units"], [["a", 2], ["b", 1]])

    def test_a_rename_renames_the_saved_unit_layout(self):
        variables = {"mmu_state_gate_color": ["a0", "a1", "b0"],
                     "mmu_state_gate_map_units": [["a", 2], ["b", 1]]}
        install = self.install(["a", "b"], ["x", "b"], {"x": "a", "b": "b"}, {"a": 2, "b": 1}, {"x": 2, "b": 1})
        result, _ = sm.migrate_unit_state(variables, install)
        self.assertEqual(result["mmu_state_gate_color"], ["a0", "a1", "b0"])
        self.assertEqual(result["mmu_state_gate_map_units"], [["x", 2], ["b", 1]])

    def test_a_pending_layout_survives_a_second_install_before_klipper_starts(self):
        first, _ = sm.migrate_unit_state({"mmu_state_gate_color": ["a0", "a1", "b0"]},
                                   self.install(["a", "b"], ["b", "a"], {"b": "b", "a": "a"}, {"a": 2, "b": 1}, {"a": 2, "b": 1}))
        second, _ = sm.migrate_unit_state(first, self.install(["b", "a"], ["c", "a"], {"c": "b", "a": "a"},
                                                        {"b": 1, "a": 2}, {"c": 1, "a": 2}))
        self.assertEqual(second["mmu_state_gate_color"], ["a0", "a1", "b0"])
        self.assertEqual(second["mmu_state_gate_map_units"], [["a", 2], ["c", 1]])

    def test_a_pending_layout_omitting_an_added_unit_survives_a_second_install(self):
        first, _ = sm.migrate_unit_state({"mmu_state_gate_color": ["a0", "a1"]},
                                   self.install(["a"], ["a", "n"], {"a": "a", "n": None}, {"a": 2}, {"a": 2, "n": 1}))
        self.assertEqual(first["mmu_state_gate_map_units"], [["a", 2]])
        second, _ = sm.migrate_unit_state(first, self.install(["a", "n"], ["a", "c"], {"a": "a", "c": "n"},
                                                        {"a": 2, "n": 1}, {"a": 2, "c": 1}))
        self.assertEqual(second["mmu_state_gate_map_units"], [["a", 2]])

    def test_unknown_gate_counts_still_rename_the_saved_unit_layout(self):
        variables = {"mmu_state_gate_map_units": [["a", 2], ["b", 1]]}
        install = self.install(["a", "b"], ["x", "b"], {"x": "a", "b": "b"}, {"a": None, "b": 1}, {"x": 2, "b": 1})
        result, _ = sm.migrate_unit_state(variables, install)
        self.assertEqual(result["mmu_state_gate_map_units"], [["x", 2], ["b", 1]])

    def test_a_stale_saved_unit_layout_is_dropped_when_it_cant_be_rebuilt(self):
        variables = {"mmu_state_gate_map_units": [["old", 3]]}
        install = self.install(["a", "b"], ["b", "a"], {"b": "b", "a": "a"}, {"a": None, "b": 1}, {"a": 1, "b": 1})
        result, _ = sm.migrate_unit_state(variables, install)
        self.assertNotIn("mmu_state_gate_map_units", result)

    def test_a_file_without_a_saved_unit_layout_is_not_given_one(self):
        install = self.install(["a", "b"], ["x", "b"], {"x": "a", "b": "b"}, {"a": 2, "b": 1}, {"x": 2, "b": 1})
        result, _ = sm.migrate_unit_state({"mmu_state_gate_color": ["a0", "a1", "b0"]}, install)
        self.assertNotIn("mmu_state_gate_map_units", result)

    def test_append_leaves_gate_maps_for_startup(self):
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
        result, _ = sm.migrate_unit_state(variables, install)
        # No existing gate was renumbered, so the selection stands
        self.assertEqual(result["mmu_state_gate_selected"], 1)
        self.assertEqual(result["mmu_state_tool_selected"], 0)
        for var in ("mmu_state_gate_color", "mmu_state_gate_temperature", "mmu_state_gate_status",
                    "mmu_state_endless_spool_groups", "mmu_state_tool_to_gate_map"):
            self.assertEqual(result[var], variables[var])
        self.assertEqual(result["mmu_state_gate_map_units"], [["a", 2]])

    def test_gate_count_change_describes_the_old_gates(self):
        variables = {"mmu_state_gate_color": ["a0", "a1", "a2", "b0"]}
        install = self.install(["a", "b"], ["a", "b"], {"a": "a", "b": "b"},
                               {"a": 3, "b": 1}, {"a": 2, "b": 1})
        result, notes = sm.migrate_unit_state(variables, install)
        self.assertEqual(result["mmu_state_gate_color"], ["a0", "a1", "a2", "b0"])
        self.assertEqual(result["mmu_state_gate_map_units"], [["a", 2], ["", 1], ["b", 1]])
        self.assertIn("Gate maps matched using the original unit layout", notes)

    def test_a_rename_keeps_the_selection(self):
        variables = {"mmu_state_gate_selected": 3, "mmu_state_tool_selected": 3, "mmu_a_bowden_lengths": [1, 2]}
        install = self.install(["a", "b"], ["a", "box"], {"a": "a", "box": "b"},
                               {"a": 2, "b": 2}, {"a": 2, "box": 2})
        result, _ = sm.migrate_unit_state(variables, install)
        self.assertEqual(result["mmu_state_gate_selected"], 3)
        self.assertEqual(result["mmu_state_tool_selected"], 3)

    def test_selection_is_reset_when_an_earlier_unit_changes(self):
        # The selected gate itself keeps its number but b's gates shift
        variables = {"mmu_state_gate_selected": 0, "mmu_state_tool_selected": 0}
        install = self.install(["a", "b"], ["a", "b"], {"a": "a", "b": "b"},
                               {"a": 2, "b": 2}, {"a": 3, "b": 2})
        result, notes = sm.migrate_unit_state(variables, install)
        self.assertEqual(result["mmu_state_gate_selected"], -1)
        self.assertEqual(result["mmu_state_tool_selected"], -1)
        self.assertIn("Selected gate and tool reset to unknown", notes)

    def test_bypass_selection_is_reset_too(self):
        variables = {"mmu_state_gate_selected": -2, "mmu_state_tool_selected": -2}
        install = self.install(["a", "b"], ["b", "a"], {"b": "b", "a": "a"},
                               {"a": 2, "b": 2}, {"a": 2, "b": 2})
        result, _ = sm.migrate_unit_state(variables, install)
        self.assertEqual(result["mmu_state_gate_selected"], -1)
        self.assertEqual(result["mmu_state_tool_selected"], -1)

    def test_identity_changes_nothing(self):
        variables = {"mmu_state_gate_color": ["a0", "a1"], "mmu_a_bowden_lengths": [1, 2]}
        install = self.install(["a"], ["a"], {"a": "a"}, {"a": 2}, {"a": 2})
        self.assertEqual(sm.migrate_unit_state(variables, install)[0], variables)

    def test_unknown_gate_counts_leave_gate_maps_alone(self):
        variables = {"mmu_state_gate_color": ["a0", "b0"], "mmu_b_bowden_lengths": [1],
                     "mmu_state_gate_selected": 1}
        install = self.install(["a", "b"], ["b", "a"], {"b": "b", "a": "a"},
                               {"a": None, "b": 1}, {"a": 1, "b": 1})
        result, notes = sm.migrate_unit_state(variables, install)
        self.assertEqual(result["mmu_state_gate_color"], ["a0", "b0"])
        self.assertTrue(any("not migrated" in n for n in notes))
        self.assertEqual(result["mmu_state_gate_selected"], -1)


class TestInstall(Scratch):

    def test_retry_after_installing_history_keeps_original_transaction(self):
        original = {'mmu_a_bowden_lengths': [1, 2, 3, 4]}
        self.install(['a', 'b'], variables=original)
        self.unit('a', 0)
        self.unit('b', 1)
        self.edit(['a', 'b'], lambda m: m.rename(0, 'left'))
        base = ['a', 'b']
        um.migrate_kconfig(self.kconfig, base, io.StringIO())
        um.prepare(self.kconfig, self.home, base, io.StringIO())
        transaction = um.read_state(self.kconfig)['install']
        self.publish_history()
        with patch.object(um, 'clear_state', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                um.apply(self.kconfig, out=io.StringIO())
        self.assertEqual(read_vars(self.vars_file)['mmu_a_bowden_lengths'], original['mmu_a_bowden_lengths'])
        self.install(['left', 'b'])
        um.prepare(self.kconfig, self.home, base, io.StringIO())
        self.assertEqual(um.read_state(self.kconfig)['install'], transaction)
        self.publish_history()
        um.apply(self.kconfig, out=io.StringIO())
        self.assertEqual(len(self.history()['steps']), 1)

    def test_stale_installed_history_keeps_retry_state(self):
        self.install(['a'], variables={'mmu_a_bowden_lengths': [1, 2, 3, 4]})
        self.unit('a', 0)
        self.edit(['a'], lambda m: m.rename(0, 'left'))
        um.migrate_kconfig(self.kconfig, ['a'], io.StringIO())
        um.prepare(self.kconfig, self.home, ['a'], io.StringIO())
        before = self.read(self.vars_file)
        transaction = um.read_state(self.kconfig)
        with self.assertRaisesRegex(ValueError, 'prepared migration history'):
            um.apply(self.kconfig, out=io.StringIO())
        self.assertEqual(self.read(self.vars_file), before)
        self.assertEqual(um.read_state(self.kconfig), transaction)
        self.publish_history()
        um.apply(self.kconfig, out=io.StringIO())
        self.assertEqual(self.applied_vars()['mmu_left_bowden_lengths'], [1, 2, 3, 4])

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

        variables = self.applied_vars()
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

    def test_encoder_vars_follow_the_encoder_name_end_to_end(self):
        self.install(["a", "b"], variables={
            "mmu_a_encoder_resolution": 0.5,
            "mmu_b_encoder_resolution": 0.6,
            "mmu_b_enc_encoder_resolution": 0.7,
        })
        self.unit("a", 0, lines=ENCODER_OWNER)
        self.unit("b", 1, lines=ENCODER_OWNER + ['CONFIG_PARAM_ENCODER_NAME="b_enc"'])
        self.edit(["a", "b"], lambda m: (m.rename(0, "left"), m.rename(1, "right")))
        self.run_all(um.baseline(self.kconfig, self.home))
        variables = self.applied_vars()
        self.assertEqual(variables["mmu_left_encoder_resolution"], 0.5)
        self.assertNotIn("mmu_a_encoder_resolution", variables)
        self.assertEqual(variables["mmu_b_enc_encoder_resolution"], 0.7)
        self.assertEqual(variables["mmu_b_encoder_resolution"], 0.6)   # not b's encoder's
        self.assertNotIn("mmu_right_encoder_resolution", variables)

    def test_a_unit_without_an_encoder_takes_no_encoder_vars(self):
        # b's own encoder is called 'a', which isn't unit a's
        self.install(["a", "b"], variables={"mmu_a_encoder_resolution": 0.5})
        self.unit("a", 0)
        self.unit("b", 1, lines=ENCODER_OWNER + ['CONFIG_PARAM_ENCODER_NAME="a"'])
        self.edit(["a", "b"], lambda m: m.rename(0, "left"))
        self.run_all(um.baseline(self.kconfig, self.home))
        variables = self.applied_vars()
        self.assertEqual(variables["mmu_a_encoder_resolution"], 0.5)
        self.assertNotIn("mmu_left_encoder_resolution", variables)

    def test_installer_does_not_touch_custom_variables_files(self):
        self.vars_file = os.path.join(self.home, "elsewhere_vars.cfg")
        self.install(["a"], variables={"mmu_a_bowden_lengths": [1]})
        self.unit("a", 0)
        self.edit(["a"], lambda m: m.rename(0, "left"))
        self.run_all(um.baseline(self.kconfig, self.home))
        self.assertEqual(len([f for f in os.listdir(self.home) if f.startswith("elsewhere_vars.cfg.old-")]), 0)
        self.assertIn("mmu_left_bowden_lengths", self.applied_vars())

    def test_nothing_to_do_leaves_everything_alone(self):
        self.install(["a"], variables={"mmu_a_bowden_lengths": [1]})
        self.unit("a", 0)
        self.top("a")
        before = self.read(self.vars_file)
        self.run_all(um.baseline(self.kconfig, self.home))
        self.assertEqual(self.read(self.vars_file), before)
        self.assertFalse(os.path.exists(um.state_path(self.kconfig)))



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
        self.unit("unit0", 0, gates=2, lines=ENCODER_OWNER + ['CONFIG_PIN_X="unit0:PA1"'])
        self.unit("unit1", 1, gates=3, lines=ENCODER_OWNER + ['CONFIG_PIN_X="unit1:PA1"'])
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

    def test_sharing_the_deleted_units_encoder_warns(self):
        self.unit("unit0", 0, gates=2, lines=ENCODER_SHARER + ['CONFIG_PARAM_ENCODER_NAME="unit1"'])
        self.unit("unit1", 1, gates=3, lines=ENCODER_OWNER)
        code, out = self.check("replace", self.base)
        self.assertEqual(code, um.EXIT_STRUCTURAL)
        self.assertIn("Unit 'unit1' shares encoder 'unit1', which no unit owns", out)

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
        variables = self.applied_vars()
        self.assertEqual(variables["mmu_unit1_bowden_lengths"], [10, 11])
        self.assertEqual(variables["mmu_unit1_statistics_gate_1"], {"u": 0})
        self.assertEqual(variables["mmu_unit1_encoder_resolution"], 0.5)
        for key in variables:
            self.assertFalse(key.startswith(("mmu_unit0_", "mmu_unit2_")), key)
        # Gate lists are matched at startup: the renamed unit takes old unit0's gates, never
        # those of the deleted unit1, which is why the removed units are recorded nameless
        self.assertEqual(variables["mmu_state_gate_color"], ["a0", "a1", "b0", "b1", "b2", "c0"])
        self.assertEqual(variables["mmu_state_gate_map_units"], [["unit1", 2], ["", 3], ["", 1]])
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

    def test_rename_after_menuconfig_re_parsed_still_rewrites_explicit_values(self):
        # MCU_NAME already follows the new name, so the old one comes from install.sh
        self.single("box", "box", 'CONFIG_PIN_X="unit0:PA1"')
        with patch.dict(os.environ, {"F_UNIT_NAME_BEFORE": "unit0"}):
            um.migrate_kconfig(self.kconfig, [], io.StringIO())
        self.assertEqual(self.kvalues()["PIN_X"], "box:PA1")

    def test_an_owners_explicit_name_follows_a_single_unit_rename(self):
        self.single("box", "unit0", *ENCODER_OWNER + BUFFER_SHARER[1:] + [
            'CONFIG_PARAM_ENCODER_NAME="unit0"', 'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="unit0"'])
        um.migrate_kconfig(self.kconfig, [], io.StringIO())
        self.assertEqual(self.kvalues()["PARAM_ENCODER_NAME"], "box")
        self.assertEqual(self.kvalues()["PARAM_SYNC_FEEDBACK_BUFFER_NAME"], "unit0")

    def test_an_installed_name_is_an_old_name_too(self):
        self.install(["unit0"])
        self.single("box", "box", 'CONFIG_PIN_X="unit0:PA1"')
        with patch.dict(os.environ, {"F_UNIT_NAME_BEFORE": ""}):
            um.migrate_kconfig(self.kconfig, ["unit0"], io.StringIO())
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
        self.assertNotIn("renumbered", out)

    def test_renaming_an_installed_single_unit_end_to_end(self):
        self.install(["unit0"], variables={
            "mmu_unit0_bowden_lengths": [1, 2, 3, 4],
            "mmu_unit0_encoder_resolution": 0.6,
            "mmu_state_gate_color": ["r", "g", "b", "w"],
            "mmu_state_gate_selected": 2, "mmu_state_tool_selected": 1,
        })
        self.single("box", "unit0", 'CONFIG_PIN_X="unit0:PA1"', *ENCODER_OWNER)
        self.run_all(um.baseline(self.kconfig, self.home))

        self.assertEqual(self.kvalues()["PIN_X"], "box:PA1")
        variables = self.applied_vars()
        self.assertEqual(variables["mmu_box_bowden_lengths"], [1, 2, 3, 4])
        self.assertNotIn("mmu_unit0_bowden_lengths", variables)
        self.assertEqual(variables["mmu_box_encoder_resolution"], 0.6)
        self.assertNotIn("mmu_unit0_encoder_resolution", variables)
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
