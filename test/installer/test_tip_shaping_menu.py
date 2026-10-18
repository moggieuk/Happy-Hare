# The "Tip Forming / Cutting" and toolhead menus.
#
# The gantry bumper belongs to the toolhead cutter, not to the tip forming
# choice, so it nests under a second "Have toolhead cutter?" prompt. That
# prompt is a printer-level flag: a multi-unit entry point shows it but only
# lets "Toolhead sensors/settings" change it.

import unittest
from unittest.mock import patch

import kconfiglib
import menuconfig

from test.hh import cfg, profiles

ENTRY_POINT_ENV = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                       UNIT_NAME='u0,u1', MCU_NAME='u0,u1')
CUTTER = {'MMU_HAS_TOOLHEAD_CUTTER': True, 'MMU_HAS_GANTRY_BUMPER_SERVO': True}


def _kconfig(label, env, syms):
    with cfg._env(env):
        return cfg._kconfig(label, dict(profiles.get('boxturtle').syms, **syms))


def _active_node(sym, filename):
    """sym's prompt in `filename`, in the tree this parse builds (each file is sourced twice)"""
    return next(node for node in sym.nodes
                if node.prompt and node.filename == filename and kconfiglib.expr_value(node.dep))


class TestGantryBumper(unittest.TestCase):

    def test_nests_under_the_toolhead_cutter(self):
        kc = _kconfig('tip_menu_nest', cfg._SINGLE_UNIT_ENV, CUTTER)
        bumper = _active_node(kc.syms['MMU_HAS_GANTRY_BUMPER_SERVO'], 'Kconfig.tip_shaping')
        self.assertIs(bumper.parent.item, kc.syms['MMU_HAS_TOOLHEAD_CUTTER'])

    def test_offered_whatever_the_tip_forming_choice(self):
        kc = _kconfig('tip_menu_slicer', cfg._SINGLE_UNIT_ENV,
                      dict(CUTTER, CHOICE_FORM_TIP_MACRO_SLICER=True))
        self.assertTrue(kc.syms['MMU_HAS_GANTRY_BUMPER_SERVO'].visibility)
        self.assertTrue(kc.syms['PIN_GANTRY_BUMPER_SERVO'].visibility)

    def test_hidden_without_a_toolhead_cutter(self):
        kc = _kconfig('tip_menu_no_cutter', cfg._SINGLE_UNIT_ENV,
                      dict(CUTTER, MMU_HAS_TOOLHEAD_CUTTER=False))
        self.assertFalse(kc.syms['MMU_HAS_GANTRY_BUMPER_SERVO'].visibility)


class TestToolheadCutterPrompt(unittest.TestCase):

    def node(self, env, syms):
        kc = _kconfig('tip_menu_cutter_%s' % env['F_MULTI_UNIT'], env, syms)
        patcher = patch.multiple(menuconfig, create=True, _kconf=kc,
                                 _show_name=False, _show_all=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        return _active_node(kc.syms['MMU_HAS_TOOLHEAD_CUTTER'], 'Kconfig.tip_shaping')

    def test_editable_in_a_single_unit(self):
        node = self.node(cfg._SINGLE_UNIT_ENV, {})
        self.assertTrue(menuconfig._visible(node))
        self.assertTrue(menuconfig._changeable(node))

    def test_shown_locked_at_a_multi_unit_entry_point(self):
        for value in (True, False):
            with self.subTest(cutter=value):
                node = self.node(ENTRY_POINT_ENV, {'MMU_HAS_TOOLHEAD_CUTTER': value})
                self.assertTrue(menuconfig._visible(node))
                self.assertFalse(menuconfig._changeable(node))

    def test_toolhead_menu_still_changes_it_at_a_multi_unit_entry_point(self):
        node = self.node(ENTRY_POINT_ENV, {})
        toolhead = _active_node(node.item, 'Kconfig.toolhead')
        self.assertTrue(menuconfig._changeable(toolhead))


class TestToolheadTypeComment(unittest.TestCase):

    def comment(self, syms):
        kc = _kconfig('toolhead_comment_%d' % len(syms), cfg._SINGLE_UNIT_ENV, syms)
        node = next(node for node in kc.node_iter()
                    if node.item is kconfiglib.COMMENT and 'PARAM_TOOLHEAD_TYPE' in node.prompt[0]
                    and kconfiglib.expr_value(node.dep))
        return kconfiglib.expand_value_markup(kc, node.prompt[0]), kconfiglib.expr_value(node.prompt[1])

    def test_names_a_known_toolhead(self):
        self.assertEqual(self.comment({'TOOLHEAD_TYPE_JABBERWOCKY': True}),
                         ('Toolhead: Jabberwocky', 2))

    def test_hidden_for_other_unknown(self):
        self.assertEqual(self.comment({'CHOICE_TOOLHEAD_TYPE_OTHER': True})[1], 0)


if __name__ == '__main__':
    unittest.main()
