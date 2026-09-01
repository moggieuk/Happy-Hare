#!/usr/bin/env python3

# Regression tests for Happy Hare's custom menuconfig cursor behavior.

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import menuconfig
from kconfiglib import COMMENT


def option():
    return SimpleNamespace(item=object())


def comment():
    return SimpleNamespace(item=COMMENT)


class FakeWindow:
    def getmaxyx(self):
        return 10, 80


class FakeAsciiWindow(FakeWindow):
    def __init__(self):
        self.writes = []

    def getyx(self):
        return 0, 0

    def addnstr(self, y, x, text, maxlen, *args):
        # Match an ASCII-configured curses window on embedded systems without
        # an installed UTF-8 locale.
        text.encode("ascii")
        self.writes.append(text[:maxlen])


class TestMenuCursorSkipsComments(unittest.TestCase):

    def setUp(self):
        menuconfig._menu_win = FakeWindow()
        menuconfig._menu_scroll = 0

    def test_first_entry_skips_leading_comments(self):
        menuconfig._shown = [comment(), comment(), option(), option()]
        menuconfig._sel_node_i = 3

        menuconfig._select_first_menu_entry()

        self.assertEqual(menuconfig._sel_node_i, 2)
        self.assertEqual(menuconfig._menu_scroll, 0)

    def test_down_and_up_skip_comment_runs(self):
        menuconfig._shown = [option(), comment(), comment(), option()]
        menuconfig._sel_node_i = 0

        menuconfig._select_next_menu_entry()
        self.assertEqual(menuconfig._sel_node_i, 3)

        menuconfig._select_prev_menu_entry()
        self.assertEqual(menuconfig._sel_node_i, 0)

    def test_navigation_stops_when_only_comments_remain(self):
        menuconfig._shown = [option(), comment(), comment()]
        menuconfig._sel_node_i = 0

        menuconfig._select_next_menu_entry()

        self.assertEqual(menuconfig._sel_node_i, 0)

    def test_last_entry_skips_trailing_comments(self):
        menuconfig._shown = [option(), option(), comment(), comment()]
        menuconfig._sel_node_i = 0

        menuconfig._select_last_menu_entry()

        self.assertEqual(menuconfig._sel_node_i, 1)

    def test_entering_menu_selects_first_non_comment(self):
        entries = [comment(), comment(), option()]
        submenu = SimpleNamespace(item=object(), is_menuconfig=True)
        menuconfig._cur_menu = SimpleNamespace()
        menuconfig._shown = [submenu]
        menuconfig._sel_node_i = 0
        menuconfig._parent_screen_rows = []

        with patch.object(menuconfig, "_shown_nodes", return_value=entries):
            self.assertTrue(menuconfig._enter_menu(submenu))

        self.assertEqual(menuconfig._sel_node_i, 2)

    def test_comment_only_menu_is_not_entered(self):
        submenu = SimpleNamespace(item=object(), is_menuconfig=True)
        menuconfig._cur_menu = SimpleNamespace()
        menuconfig._shown = [submenu]
        menuconfig._sel_node_i = 0
        menuconfig._parent_screen_rows = []

        with patch.object(menuconfig, "_shown_nodes",
                          return_value=[comment(), comment()]):
            self.assertFalse(menuconfig._enter_menu(submenu))


class TestMenuRendering(unittest.TestCase):

    def test_non_ascii_text_has_readable_ascii_fallbacks(self):
        win = FakeAsciiWindow()

        menuconfig._safe_addstr(
            win, 0, 0, "─────── → FANS (°C); other arrows: ← ↑ ↓", 1)

        self.assertEqual(
            win.writes, ["------- > FANS (^C); other arrows: ? ? ?"])


class TestArrayEditorValidation(unittest.TestCase):

    @staticmethod
    def angle_symbol(gates=4):
        return SimpleNamespace(
            orig_type=menuconfig.STRING,
            array_editor=",",
            array_size_sym=SimpleNamespace(
                orig_type=menuconfig.INT,
                str_value=str(gates),
                name="PARAM_NUM_GATES",
            ),
        )

    def test_gate_sized_array_rejects_the_wrong_number_of_values(self):
        with patch.object(menuconfig, "_error") as error:
            self.assertFalse(menuconfig._check_valid(
                self.angle_symbol(), "26,58,90"))

        error.assert_called_once()
        self.assertIn("Expected 4 value(s)", error.call_args.args[0])

    def test_empty_array_remains_available_as_calibration_sentinel(self):
        with patch.object(menuconfig, "_error") as error:
            self.assertTrue(menuconfig._check_valid(self.angle_symbol(), ""))

        error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
