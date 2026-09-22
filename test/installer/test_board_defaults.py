# Board defaults must beat the generic ones they shadow.
#
# A board file steers a symbol by re-declaring it with just an added default.
# Which default actually wins is decided by PARSE ORDER: kconfiglib walks a
# symbol's nodes in the order they were sourced and takes the first whose
# condition holds. boards/Kconfig is sourced at installer/Kconfig:275, ahead of
# the feature files that carry the generic values, and that is the only reason
# a board's pinout reaches the rendered config at all.
#
# Nothing used to check that mechanically. Coverage was four hand-written
# (board, symbol, value) triples across ~20 board files, so re-ordering a
# source could silently revert most of a board's pinout to generic values and
# still leave the suite green. This walks every board instead.
#
# It asserts the PROVENANCE of the winning default rather than its literal
# text, which is what makes it cheap and rename-proof: kconfiglib evaluates the
# conditions, so $(MCU_NAME) expansion, `default X if Y`, @repeat blocks and
# nested `if` guards are all handled by the real parser rather than re-modelled
# here.

import glob
import os
import re
import unittest

from kconfiglib import expr_value

from test.hh import cfg

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Board files are only selectable under a machine type that permits them:
# boards/Kconfig gates the generic set on !KMS && !VVD && !QIDI && !PER_GATE_MCU,
# the three custom boards on their own machine, and per_gate/ on
# MMU_HAS_PER_GATE_MCU (which only EMU implies). OwlFC-Mini additionally carries
# `depends on PARAM_NUM_GATES <= 2`, so it needs NightOwl's 2-gate default.
_TRADRACK = 'MMU_TYPE_TRADRACK_1_0'

BOARD_MACHINE = {
    'boards/Kconfig.afc_lite_1':             ('BOARD_TYPE_AFC_LITE_1_0', _TRADRACK),
    'boards/Kconfig.afc_pro':                ('BOARD_TYPE_AFC_PRO_1_0', _TRADRACK),
    'boards/Kconfig.chameleon_x5_1':         ('BOARD_TYPE_CHAMELEON_X5_1_0', _TRADRACK),
    'boards/Kconfig.easy_brd':               ('BOARD_TYPE_EASY_BRD', _TRADRACK),
    'boards/Kconfig.easy_brd_rp2040':        ('BOARD_TYPE_EASY_BRD_RP2040', _TRADRACK),
    'boards/Kconfig.ebb42_1_2':              ('BOARD_TYPE_EBB_GEN1', _TRADRACK),
    'boards/Kconfig.erb_1':                  ('BOARD_TYPE_ERB_1', _TRADRACK),
    'boards/Kconfig.erb_2':                  ('BOARD_TYPE_ERB_2', _TRADRACK),
    'boards/Kconfig.mellow_easy_brd_can_1':  ('BOARD_TYPE_MELLOW_EASY_BRD_CAN_1', _TRADRACK),
    'boards/Kconfig.mellow_easy_brd_can_2':  ('BOARD_TYPE_MELLOW_EASY_BRD_CAN_2', _TRADRACK),
    'boards/Kconfig.mmb_1_0':                ('BOARD_TYPE_MMB_1_0', _TRADRACK),
    'boards/Kconfig.mmb_1_1':                ('BOARD_TYPE_MMB_1_1', _TRADRACK),
    'boards/Kconfig.mmb_2_0':                ('BOARD_TYPE_MMB_2_0', _TRADRACK),
    'boards/Kconfig.owlfc_mini_1_0':         ('BOARD_TYPE_OWLFC_MINI_1_0', 'MMU_TYPE_NIGHT_OWL_1_0'),
    'boards/Kconfig.skr_pico_1':             ('BOARD_TYPE_SKR_PICO_1', _TRADRACK),
    'boards/Kconfig.tzb_1_0':                ('BOARD_TYPE_TZB_1_0', _TRADRACK),
    'boards/Kconfig.wgb_3_0':                ('BOARD_TYPE_WGB_3_0', _TRADRACK),
    'boards/custom/Kconfig.kms':             ('BOARD_TYPE_KMS_1_0', 'MMU_TYPE_KMS_1_0'),
    'boards/custom/Kconfig.qidi_box':        ('BOARD_TYPE_QIDI_BOX_2_0', 'MMU_TYPE_QIDI_BOX_1_0'),
    'boards/custom/Kconfig.vvd':             ('BOARD_TYPE_VVD_1_0', 'MMU_TYPE_VVD_1_0'),
    # Both of these declare BOARD_TYPE_EBB_GEN1. The per_gate copy is only
    # sourced under MMU_HAS_PER_GATE_MCU, so it has to be probed under EMU -
    # under Tradrack its nodes parse but are dependency-dead, and the file
    # would appear to contribute one default instead of ~60.
    'boards/per_gate/Kconfig.ebb_gen1':      ('BOARD_TYPE_EBB_GEN1', 'MMU_TYPE_EMU_1_0'),
    'boards/per_gate/Kconfig.slb':           ('BOARD_TYPE_SLB_1_0', 'MMU_TYPE_EMU_1_0'),
}

# How many defaults each board is currently observed to win. A floor, not an
# equality: adding a pin to a board is routine and should not fail here. What
# this catches is a collapse - if a board stops contributing defaults because a
# refactor moved its symbols or changed how they resolve, the test above would
# pass vacuously without this.
EXPECTED_WINS = {
    'boards/Kconfig.afc_lite_1': 46,            'boards/Kconfig.afc_pro': 78,
    'boards/Kconfig.chameleon_x5_1': 46,        'boards/Kconfig.easy_brd': 13,
    'boards/Kconfig.easy_brd_rp2040': 13,       'boards/Kconfig.ebb42_1_2': 12,
    'boards/Kconfig.erb_1': 27,                 'boards/Kconfig.erb_2': 27,
    'boards/Kconfig.mellow_easy_brd_can_1': 25, 'boards/Kconfig.mellow_easy_brd_can_2': 25,
    'boards/Kconfig.mmb_1_0': 23,               'boards/Kconfig.mmb_1_1': 23,
    'boards/Kconfig.mmb_2_0': 23,               'boards/Kconfig.owlfc_mini_1_0': 18,
    'boards/Kconfig.skr_pico_1': 15,            'boards/Kconfig.tzb_1_0': 32,
    'boards/Kconfig.wgb_3_0': 44,               'boards/custom/Kconfig.kms': 39,
    'boards/custom/Kconfig.qidi_box': 23,       'boards/custom/Kconfig.vvd': 24,
    'boards/per_gate/Kconfig.ebb_gen1': 59,     'boards/per_gate/Kconfig.slb': 64,
}


def _satisfied_default(node):
    """True if this node carries a default whose condition currently holds."""
    for _value, cond in node.defaults:
        if expr_value(cond):
            return True
    return False


class TestBoardDefaultPrecedence(unittest.TestCase):
    """One parsed tree, reset per board - 22 fresh parses would cost ~20s."""

    @classmethod
    def setUpClass(cls):
        cls._env_ctx = cfg._env(cfg._SINGLE_UNIT_ENV)
        cls._env_ctx.__enter__()
        cls.addClassCleanup(cls._env_ctx.__exit__, None, None, None)
        cls.tree = cfg._new_kconfig('board_defaults')

    def _select(self, board_sym, machine):
        kc = cfg._apply_syms(
            self.tree, 'board_defaults',
            {machine: True, board_sym: True}, reset=True)
        self.assertTrue(
            kc.is_enabled(board_sym),
            '%s is not selectable under %s - the BOARD_MACHINE entry is wrong, '
            'or boards/Kconfig changed how it gates this board' % (board_sym, machine))
        return kc

    def test_every_board_file_is_mapped(self):
        """A new board file must be added to BOARD_MACHINE, not silently skipped."""
        on_disk = set()
        for pattern in ('installer/boards/Kconfig.*', 'installer/boards/*/Kconfig.*'):
            for path in glob.glob(os.path.join(REPO_ROOT, pattern)):
                on_disk.add(os.path.relpath(path, os.path.join(REPO_ROOT, 'installer')))
        self.assertEqual(
            sorted(on_disk), sorted(BOARD_MACHINE),
            'board files on disk and BOARD_MACHINE have diverged')

    def test_board_defaults_win_over_the_generic_ones(self):
        for filename, (board_sym, machine) in sorted(BOARD_MACHINE.items()):
            with self.subTest(board=filename):
                kc = self._select(board_sym, machine)
                for sym in kc.unique_defined_syms:
                    board_node = next(
                        (n for n in sym.nodes
                         if n.filename == filename and _satisfied_default(n)), None)
                    if board_node is None:
                        continue  # board says nothing about this symbol right now
                    winner = next(
                        (n for n in sym.nodes if _satisfied_default(n)), None)
                    self.assertIs(
                        winner, board_node,
                        '%s: the board sets this at %s:%d but %s:%d wins (value %r). '
                        'A Kconfig source was re-ordered, or a generic default gained '
                        'a condition that now fires first.'
                        % (sym.name, filename, board_node.linenr,
                           winner.filename, winner.linenr, sym.str_value))

    def test_each_board_still_contributes_its_defaults(self):
        for filename, (board_sym, machine) in sorted(BOARD_MACHINE.items()):
            with self.subTest(board=filename):
                kc = self._select(board_sym, machine)
                wins = sum(
                    1 for sym in kc.unique_defined_syms
                    if any(n.filename == filename and _satisfied_default(n)
                           for n in sym.nodes))
                self.assertGreaterEqual(
                    wins, EXPECTED_WINS[filename],
                    '%s now supplies only %d defaults, down from %d - the board is '
                    'no longer being applied, which would make the precedence test '
                    'above pass vacuously' % (filename, wins, EXPECTED_WINS[filename]))


if __name__ == '__main__':
    unittest.main()
