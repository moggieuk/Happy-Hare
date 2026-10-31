#!/usr/bin/env python3

# Regression tests for Happy Hare's custom menuconfig cursor behavior.

import re
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


class FakeRecordingWindow(FakeWindow):
    def __init__(self):
        self.writes = []
        self.x = 0

    def move(self, y, x):
        self.x = x

    def getyx(self):
        return 0, self.x

    def addnstr(self, y, x, text, maxlen, *attr):
        self.writes.append((text[:maxlen], attr[0] if attr else None))
        self.x += len(text[:maxlen])


class TestHelpMarkup(unittest.TestCase):

    def test_bold_tags_render_and_other_brackets_stay_literal(self):
        win = FakeRecordingWindow()
        base = 7

        menuconfig._safe_addstr_markup(
            win, 0, 1, "  [[B]]effect_name, (r,g,b) [, duration][[/B]] here", base)

        self.assertEqual(win.writes, [
            ("  ", base),
            ("effect_name, (r,g,b) [, duration]", base | menuconfig.curses.A_BOLD),
            (" here", base),
        ])


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


class TestArrayEditorJoin(unittest.TestCase):

    def test_lines_are_joined_with_the_separator_and_a_space(self):
        self.assertEqual(menuconfig._join_array_lines([" unload", "purge ", "total"], ","),
                         "unload, purge, total")
        self.assertEqual(menuconfig._join_array_lines(["neopixel:a (1)", "neopixel:b (2)"], ";"),
                         "neopixel:a (1); neopixel:b (2)")

    def test_round_trips_a_shipped_default(self):
        default = "total, total_average, job, job_average, last"
        self.assertEqual(menuconfig._join_array_lines(default.split(","), ","), default)

    def test_separator_ending_in_a_space_is_not_doubled(self):
        self.assertEqual(menuconfig._join_array_lines(["a", "b"], ", "), "a, b")

    def test_single_and_blank_lines(self):
        self.assertEqual(menuconfig._join_array_lines([""], ","), "")
        self.assertEqual(menuconfig._join_array_lines(["a"], ","), "a")
        self.assertEqual(menuconfig._join_array_lines(["a", "", "b"], ","), "a, , b")


class TestValidatorValidation(unittest.TestCase):

    @staticmethod
    def symbol(array_editor=None):
        return SimpleNamespace(
            orig_type=menuconfig.STRING,
            array_editor=array_editor,
            array_size_sym=None,
            validator=re.compile("[a-z]+, [0-9]+"),
        )

    def check(self, sym, value):
        with patch.object(menuconfig, "_error") as error:
            result = menuconfig._check_valid(sym, value)
        return result, error

    def test_whole_string_must_match(self):
        valid, error = self.check(self.symbol(), "abc, 12x")

        self.assertFalse(valid)
        error.assert_called_once()
        self.assertIn("'abc, 12x' is not valid syntax", error.call_args.args[0])

    def test_matching_string_is_accepted_ignoring_surrounding_whitespace(self):
        valid, error = self.check(self.symbol(), "  abc, 12 ")

        self.assertTrue(valid)
        error.assert_not_called()

    def test_each_array_element_must_match(self):
        valid, error = self.check(self.symbol(";"), "abc, 1; def, 2;ghi")

        self.assertFalse(valid)
        self.assertIn("Element 3 'ghi' is not valid syntax", error.call_args.args[0])

    def test_matching_array_is_accepted(self):
        valid, error = self.check(self.symbol(";"), "abc, 1; def, 2")

        self.assertTrue(valid)
        error.assert_not_called()

    def test_empty_array_has_no_elements_to_check(self):
        valid, error = self.check(self.symbol(";"), "")

        self.assertTrue(valid)
        error.assert_not_called()

    def test_empty_string_must_match(self):
        valid, _ = self.check(self.symbol(), "")

        self.assertFalse(valid)

    def test_error_points_to_help_without_the_regexp(self):
        sym = self.symbol()
        _, error = self.check(sym, "bad")

        self.assertEqual(error.call_args.args[0], "'bad' is not valid syntax -- see help")
        self.assertNotIn(sym.validator.pattern, error.call_args.args[0])


class TestValueMarkup(unittest.TestCase):
    """[[VALUE:SYM]] shows a symbol's live value wherever menuconfig draws text."""

    KCONFIG = """
config NAME
    string "Name"
    default "unit0"

comment "Unit: [[VALUE:NAME:8]]|"
"""

    def setUp(self):
        import os
        import tempfile
        import kconfiglib
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "Kconfig")
        with open(path, "w") as f:
            f.write(self.KCONFIG)
        self.kconf = kconfiglib.Kconfig(path, warn=False)
        patcher = patch.multiple(menuconfig, create=True, _kconf=self.kconf,
                                 _show_name=False, _show_all=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_value_follows_the_symbol(self):
        self.assertEqual(menuconfig._expand_values("[[VALUE:NAME]]!"), "unit0!")
        self.kconf.syms["NAME"].set_value("box")
        self.assertEqual(menuconfig._expand_values("[[VALUE:NAME]]!"), "box!")

    def test_width_pads_and_truncates(self):
        self.assertEqual(menuconfig._expand_values("[[VALUE:NAME:7]]|"), "unit0  |")
        self.assertEqual(menuconfig._expand_values("[[VALUE:NAME:3]]|"), "uni|")

    def test_unknown_symbol_shows_a_question_mark(self):
        self.assertEqual(menuconfig._expand_values("[[VALUE:NOPE]]"), "?")
        with patch.object(menuconfig, "_kconf", None):
            self.assertEqual(menuconfig._expand_values("[[VALUE:NAME]]"), "?")

    def test_comment_rows_show_the_live_value(self):
        node = [n for n in self.kconf.node_iter() if n.item is COMMENT][0]
        self.kconf.syms["NAME"].set_value("box")
        self.assertIn("*** Unit: box     | ***", menuconfig._node_str(node))

    def test_drawn_text_is_expanded(self):
        win = FakeRecordingWindow()
        menuconfig._safe_addstr_markup(win, 0, 0, "[[B]][[VALUE:NAME]][[/B]]", 0)
        self.assertEqual([text for text, _ in win.writes], ["unit0"])

    def test_display_length_ignores_markup(self):
        self.assertEqual(menuconfig._display_len("[[B]][[VALUE:NAME]][[/B]]x"), 6)


class TestDimComment(unittest.TestCase):
    """'dim' on a comment dims the whole row, including the '***' decoration."""

    KCONFIG = """
comment "Plain"

comment "Quiet"
    dim

comment "_Heading"
    dim
"""

    def parse(self, text):
        import os
        import tempfile
        import kconfiglib
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "Kconfig")
        with open(path, "w") as f:
            f.write(text)
        return kconfiglib.Kconfig(path, warn=False)

    def setUp(self):
        self.kconf = self.parse(self.KCONFIG)
        patcher = patch.multiple(menuconfig, create=True, _kconf=self.kconf,
                                 _show_name=False, _show_all=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rows = {n.prompt[0]: menuconfig._node_str(n)
                     for n in self.kconf.node_iter() if n.item is COMMENT}

    def test_plain_comment_is_not_dimmed(self):
        self.assertNotIn("[[DIM]]", self.rows["Plain"])
        self.assertTrue(self.rows["Plain"].endswith("*** Plain ***"))

    def test_dim_comment_dims_the_decoration(self):
        self.assertTrue(self.rows["Quiet"].startswith("[[DIM]]"))
        self.assertTrue(self.rows["Quiet"].endswith("*** Quiet ***[[/DIM]]"))

    def test_dim_heading(self):
        self.assertTrue(self.rows["_Heading"].startswith("[[DIM]]───"))
        self.assertTrue(self.rows["_Heading"].endswith("[[/DIM]]"))

    def test_dim_is_only_valid_on_comments(self):
        import kconfiglib
        with self.assertRaisesRegex(kconfiglib.KconfigError, "dim is only valid for comments"):
            self.parse('config FOO\n    bool "Foo"\n    dim\n')


class TestMenuPathMarkup(unittest.TestCase):
    """The menu path bar is drawn as plain text, so a menu title's markup must not leak into it."""

    def test_markup_is_stripped_from_menu_path(self):
        import os
        import tempfile
        import kconfiglib
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "Kconfig")
        with open(path, "w") as f:
            f.write('menu "Outer"\nmenu "Inner [[DIM]](ADVANCED)[[/DIM]]"\n'
                    'config FOO\n    bool "Foo"\nendmenu\nendmenu\n')
        kconf = kconfiglib.Kconfig(path, warn=False)
        inner = kconf.syms["FOO"].nodes[0].parent

        with patch.object(menuconfig, "_kconf", kconf, create=True):
            self.assertEqual(menuconfig._menu_path_prompts(inner),
                             ["(Top)", "Outer", "Inner (ADVANCED)"])


class TestConditionalForceshow(unittest.TestCase):
    """'forceshow if <expr>' shows a hidden entry only while <expr> holds."""

    KCONFIG = """
config FLAG
    bool "Flag"

config ALWAYS
    bool "Always" if n
    forceshow

config SOMETIMES
    bool "Sometimes" if n
    forceshow if FLAG
"""

    parse = TestDimComment.parse

    def setUp(self):
        self.kconf = self.parse(self.KCONFIG)
        patcher = patch.multiple(menuconfig, create=True, _kconf=self.kconf,
                                 _show_name=False, _show_all=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def node(self, name):
        return self.kconf.syms[name].nodes[0]

    def test_plain_forceshow_is_unconditional(self):
        self.assertTrue(menuconfig._visible(self.node("ALWAYS")))

    def test_the_condition_decides(self):
        self.assertFalse(menuconfig._visible(self.node("SOMETIMES")))
        self.kconf.syms["FLAG"].set_value(2)
        self.assertTrue(menuconfig._visible(self.node("SOMETIMES")))



class TestLockedSecondPrompt(unittest.TestCase):
    """A force-shown prompt that is off shows locked, even when another prompt can change it."""

    KCONFIG = """
config MMU_HAS_CUTTER
    bool "Cutter"

config MMU_HAS_CUTTER
    prompt "Cutter" if n
    forceshow
"""

    parse = TestDimComment.parse

    def setUp(self):
        self.kconf = self.parse(self.KCONFIG)
        self.kconf.syms["MMU_HAS_CUTTER"].set_value(2)
        patcher = patch.multiple(menuconfig, create=True, _kconf=self.kconf,
                                 _show_name=False, _show_all=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.editable, self.locked = self.kconf.syms["MMU_HAS_CUTTER"].nodes

    def test_the_locked_row_looks_fixed(self):
        self.assertEqual(menuconfig._value_str(self.locked), "-*-")
        row = menuconfig._node_str(self.locked)
        self.assertIn("(FIXED)", row)
        self.assertNotIn("NOT DEFAULT", row)

    def test_y_and_n_keys_leave_the_locked_row_alone(self):
        for node, expected in ((self.locked, "y"), (self.editable, "n")):
            with self.subTest(locked=node is self.locked), \
                    patch.multiple(menuconfig, create=True, _shown=[node], _sel_node_i=0,
                                   _conf_changed=False, _update_menu=lambda: None):
                menuconfig._set_sel_node_tri_val(0)
                self.assertEqual(self.kconf.syms["MMU_HAS_CUTTER"].str_value, expected)
                self.kconf.syms["MMU_HAS_CUTTER"].set_value(2)

    def test_the_editable_row_is_unchanged(self):
        self.assertEqual(menuconfig._value_str(self.editable), "[*]")
        row = menuconfig._node_str(self.editable)
        self.assertNotIn("(FIXED)", row)
        self.assertIn("NOT DEFAULT", row)

class TestReparseEnv(unittest.TestCase):
    """Changing a 'reparse_env' symbol re-parses with the new value, in place.

    Only values saved as defaults (#~DEFAULT~#, i.e. PARAM_/PIN_ ... names) are
    recomputed; an explicit value is the user's and is kept as it is.
    """

    KCONFIG = """
config NAME
    string "Name"
    default "$(NAME)"
    reparse_env "NAME MCU"

config PIN_X
    string "Pin"
    default "$(MCU):PA1"

config PARAM_OTHER
    string "Other"
    default "x"

menu "Sub"
config PARAM_INNER
    string "Inner"
    default "$(NAME)_inner"
endmenu
"""

    def setUp(self):
        import os
        import tempfile
        import kconfiglib
        self.os = os
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "Kconfig")
        with open(path, "w") as f:
            f.write(self.KCONFIG)
        for var in ("NAME", "MCU"):
            self.addCleanup(lambda v=var, old=os.environ.get(var):
                            os.environ.__setitem__(v, old) if old is not None else os.environ.pop(v, None))
            os.environ[var] = "unit0"
        self.kconf = kconfiglib.Kconfig(path, warn=False)
        patcher = patch.multiple(menuconfig, create=True, _kconf=self.kconf, _show_all=False,
                                 _show_name=False, _conf_changed=False, _menu_scroll=0,
                                 _show_busy=lambda text: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def at(self, menu, symbol):
        menuconfig._cur_menu = menu
        menuconfig._shown = menuconfig._shown_nodes(menu)
        menuconfig._sel_node_i = [n.item for n in menuconfig._shown].index(self.kconf.syms[symbol])

    def selected(self):
        return menuconfig._shown[menuconfig._sel_node_i].item.name

    def test_defaults_follow_the_new_value_and_user_values_survive(self):
        self.kconf.syms["PARAM_OTHER"].set_value("kept")
        self.at(self.kconf.top_node, "NAME")
        menuconfig._set_val(self.kconf.syms["NAME"], "box")

        kconf = menuconfig._kconf
        self.assertIsNot(kconf, self.kconf)
        self.assertEqual(kconf.syms["NAME"].str_value, "box")
        self.assertEqual(kconf.syms["PIN_X"].str_value, "box:PA1")
        self.assertEqual(kconf.syms["PARAM_OTHER"].str_value, "kept")
        self.assertEqual((self.os.environ["NAME"], self.os.environ["MCU"]), ("box", "box"))
        self.assertTrue(menuconfig._conf_changed)
        self.assertIs(menuconfig._cur_menu, kconf.top_node)
        self.assertEqual(self.selected(), "NAME")

    def test_navigation_inside_a_submenu_is_kept(self):
        sub = [n for n in self.kconf.node_iter() if n.item is menuconfig.MENU][0]
        self.at(sub, "PARAM_INNER")
        self.kconf.syms["NAME"].set_value("box")
        menuconfig._reparse(self.kconf.syms["NAME"])
        self.assertEqual(menuconfig._cur_menu.prompt[0], "Sub")
        self.assertIs(menuconfig._cur_menu.kconfig, menuconfig._kconf)
        self.assertEqual(self.selected(), "PARAM_INNER")
        self.assertEqual(menuconfig._kconf.syms["PARAM_INNER"].str_value, "box_inner")

    def test_unchanged_value_does_not_reparse(self):
        self.at(self.kconf.top_node, "NAME")
        menuconfig._reparse(self.kconf.syms["NAME"])
        self.assertIs(menuconfig._kconf, self.kconf)

    def test_a_failed_parse_keeps_the_old_tree_and_environment(self):
        self.at(self.kconf.top_node, "NAME")
        self.kconf.syms["NAME"].set_value("box")
        with patch.object(menuconfig, "Kconfig", side_effect=RuntimeError("boom")), \
                patch.object(menuconfig, "_error") as error:
            menuconfig._reparse(self.kconf.syms["NAME"])
        self.assertIs(menuconfig._kconf, self.kconf)
        self.assertEqual(self.os.environ["NAME"], "unit0")
        self.assertIn("boom", error.call_args.args[0])


class TestReparseEnvBool(unittest.TestCase):
    """A bool 'reparse_env' symbol re-parses to turn its variable on, however it changes,
    e.g. through an imply."""

    KCONFIG = """
config TYPE
    bool "Type"
    imply PER_GATE

config PER_GATE
    bool "Per gate" if TYPE
    reparse_env "PER_GATE_ENV"

@if PER_GATE_ENV@
if PER_GATE
config PARAM_GATE_UUID
    string "Gate UUID"
    default "x"
endif
@endif@

config OFF
    bool

if OFF
config NAME
    string
    default "$(NAME_ENV)"
    reparse_env "NAME_ENV"
endif
"""

    def setUp(self):
        import os
        import tempfile
        import kconfiglib
        self.os = os
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "Kconfig")
        with open(path, "w") as f:
            f.write(self.KCONFIG)
        for var, value in (("PER_GATE_ENV", None), ("NAME_ENV", "box")):
            self.addCleanup(lambda v=var, old=os.environ.get(var):
                            os.environ.__setitem__(v, old) if old is not None else os.environ.pop(v, None))
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        self.kconf = kconfiglib.Kconfig(path, warn=False)
        patcher = patch.multiple(menuconfig, create=True, _kconf=self.kconf, _show_all=False,
                                 _show_name=False, _conf_changed=False, _menu_scroll=0,
                                 _show_busy=lambda text: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        menuconfig._cur_menu = self.kconf.top_node
        menuconfig._shown = menuconfig._shown_nodes(self.kconf.top_node)
        menuconfig._sel_node_i = 0

    def test_an_implied_change_reparses_and_unset_is_off(self):
        # Neither unset (off) nor a symbol whose dependencies are off re-parses
        menuconfig._reparse_stale()
        self.assertIs(menuconfig._kconf, self.kconf)

        menuconfig._set_val(self.kconf.syms["TYPE"], 2)
        kconf = menuconfig._kconf
        self.assertIsNot(kconf, self.kconf)
        self.assertEqual(self.os.environ["PER_GATE_ENV"], "y")
        self.assertIn("PARAM_GATE_UUID", kconf.syms)

        # Turning it off only hides what it added
        menuconfig._set_val(kconf.syms["PER_GATE"], 0)
        self.assertIs(menuconfig._kconf, kconf)
        self.assertEqual(kconf.syms["PARAM_GATE_UUID"].visibility, 0)


class TestLink(unittest.TestCase):
    """A comment with 'link SYMBOL' jumps to SYMBOL; leaving that menu comes back."""

    KCONFIG = """
config FLAG
    bool "Flag"
    default y

menu "Source"
config A
    bool "A"
comment "Plain"
comment "Go to target"
    link TARGET
comment "Go nowhere"
    link HIDDEN
comment "Up top"
    link TOP
endmenu

menu "Dest"
comment "_Heading"
config FIRST
    bool "First"
config TARGET
    bool "Target"
menu "Deeper"
config DEEP
    bool "Deep"
comment "Back to source"
    link A
endmenu
endmenu

config HIDDEN
    bool "Hidden" if !FLAG

config TOP
    bool "Top"
"""

    parse = TestDimComment.parse

    def setUp(self):
        self.kconf = self.parse(self.KCONFIG)
        patcher = patch.multiple(menuconfig, create=True, _kconf=self.kconf,
                                 _show_name=False, _show_all=False, _menu_win=FakeWindow(),
                                 _menu_scroll=0, _parent_screen_rows=[], _link_return=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.nodes = {n.prompt[0]: n for n in self.kconf.node_iter() if n.prompt}

    def at(self, menu, prompt):
        menuconfig._cur_menu = self.nodes[menu]
        menuconfig._shown = menuconfig._shown_nodes(self.nodes[menu])
        menuconfig._sel_node_i = menuconfig._shown.index(self.nodes[prompt])

    def where(self):
        menu = menuconfig._cur_menu
        menu = "(Top)" if menu is self.kconf.top_node else menu.prompt[0]
        return menu, menuconfig._shown[menuconfig._sel_node_i].prompt[0]

    def follow(self):
        return menuconfig._follow_link(menuconfig._shown[menuconfig._sel_node_i])

    def test_link_is_only_valid_on_comments(self):
        import kconfiglib
        with self.assertRaisesRegex(kconfiglib.KconfigError, "link is only valid for comments"):
            self.parse('config FOO\n    bool "Foo"\n    link FOO\n')

    def test_link_rows_are_selectable_and_plain_comments_are_not(self):
        self.at("Source", "A")
        menuconfig._select_next_menu_entry()
        self.assertEqual(self.where(), ("Source", "Go to target"))

    def test_following_lands_on_the_target_in_its_menu(self):
        self.at("Source", "Go to target")
        self.assertTrue(self.follow())
        self.assertEqual(self.where(), ("Dest", "Target"))

    def test_leaving_the_target_menu_returns_to_the_link(self):
        self.at("Source", "Go to target")
        self.follow()
        menuconfig._leave_menu()
        self.assertEqual(self.where(), ("Source", "Go to target"))
        menuconfig._leave_menu()
        self.assertEqual(self.where(), ("(Top)", "Source"))

    def test_a_submenu_of_the_target_is_left_normally(self):
        self.at("Source", "Go to target")
        self.follow()
        menuconfig._enter_menu(self.nodes["Deeper"])
        menuconfig._leave_menu()
        self.assertEqual(self.where(), ("Dest", "Deeper"))
        menuconfig._leave_menu()
        self.assertEqual(self.where(), ("Source", "Go to target"))

    def test_chained_links_unwind_in_order(self):
        self.at("Source", "Go to target")
        self.follow()
        menuconfig._enter_menu(self.nodes["Deeper"])
        menuconfig._select_next_menu_entry()
        self.follow()
        self.assertEqual(self.where(), ("Source", "A"))
        menuconfig._leave_menu()
        self.assertEqual(self.where(), ("Deeper", "Back to source"))
        menuconfig._leave_menu()
        menuconfig._leave_menu()
        self.assertEqual(self.where(), ("Source", "Go to target"))

    def test_a_top_level_target_returns_instead_of_quitting(self):
        self.at("Source", "Up top")
        self.follow()
        self.assertEqual(self.where(), ("(Top)", "Top"))
        self.assertTrue(menuconfig._in_link_target())
        menuconfig._leave_menu()
        self.assertEqual(self.where(), ("Source", "Up top"))
        self.assertFalse(menuconfig._in_link_target())

    def test_a_hidden_target_is_reported_without_moving(self):
        self.at("Source", "Go nowhere")
        with patch.object(menuconfig, "_error") as error:
            self.assertFalse(self.follow())
        self.assertIn("Go nowhere", error.call_args.args[0])
        self.assertFalse(menuconfig._show_all)
        self.assertEqual(self.where(), ("Source", "Go nowhere"))
        self.assertEqual(menuconfig._link_return, [])

    def test_row_and_help_name_the_destination(self):
        link = self.nodes["Go to target"]
        self.assertTrue(menuconfig._node_str(link).endswith(" Go to target  --->"))
        self.assertIn("Jumps to: [[B]](Top) -> Dest[[/B]]", menuconfig._link_help(link))

    def test_link_rows_are_not_saved(self):
        import os
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, ".config")
        self.kconf.write_config(path, save_old=False)
        with open(path) as f:
            saved = f.read()
        self.assertIn("Plain", saved)
        self.assertNotIn("Go to target", saved)


class TestShippedLinks(unittest.TestCase):
    """The links in the real tree reach their targets."""

    def links(self, kconf):
        return [n for n in kconf.node_iter() if getattr(n, "link", None)]

    def test_every_link_targets_a_prompted_symbol(self):
        from test.installer.test_kconfig_references import _SHAPES
        from test.hh import cfg
        for shape, env in sorted(_SHAPES.items()):
            with self.subTest(shape=shape):
                with cfg._env(env):
                    kconf = cfg._new_kconfig('links_' + shape)
                for link in self.links(kconf):
                    self.assertTrue(any(n.prompt for n in link.link.nodes),
                                    "%s:%d links to %s, which has no prompt"
                                    % (link.filename, link.linenr, link.link.name))

    # Link prompt -> (menu it sits in, values that show it, values that hide it,
    # Macro Variables submenu it opens)
    SHIPPED = {
        "Blobifier macro variables": (
            "Purging", {"MMU_HAS_BLOBIFIER": 2, "CHOICE_PURGE_MACRO_BLOBIFIER": 2},
            {"MMU_HAS_BLOBIFIER": 2, "CHOICE_PURGE_MACRO_PURGE": 2}, "Blobifier"),
        "Simple purge macro variables": (
            "Purging", {"CHOICE_PURGE_MACRO_PURGE": 2}, {"CHOICE_PURGE_MACRO_SLICER": 2}, "Purge"),
        "Servo cutter macro variables": (
            "Tip Forming / Cutting", {"MMU_HAS_SERVO_CUTTER": 2}, {"MMU_HAS_SERVO_CUTTER": 0},
            "Servo cutter at MMU"),
        "Tip forming macro variables": (
            "Tip Forming / Cutting", {"CHOICE_FORM_TIP_MACRO_FORM_TIP": 2},
            {"CHOICE_FORM_TIP_MACRO_SLICER": 2}, "Tip forming"),
        "Tip cutting macro variables": (
            "Tip Forming / Cutting",
            {"MMU_HAS_TOOLHEAD_CUTTER": 2, "CHOICE_FORM_TIP_MACRO_CUT_TIP": 2},
            {"MMU_HAS_TOOLHEAD_CUTTER": 2, "CHOICE_FORM_TIP_MACRO_FORM_TIP": 2},
            "Toolhead tip cutting"),
    }

    def test_each_link_opens_its_macro_variables(self):
        from test.hh import cfg
        shapes = {
            'single unit': cfg._SINGLE_UNIT_ENV,
            'multi-unit entry point': {
                'UNIT_NAME': 'unit0,unit1', 'MCU_NAME': 'unit0,unit1', 'UNIT_INDEX': '0',
                'F_MULTI_UNIT': 'y', 'F_MULTI_UNIT_ENTRY_POINT': 'y'},
        }

        def apply(kconf, values):
            for name, value in values.items():
                kconf.syms[name].set_value(value)

        for shape, env in sorted(shapes.items()):
            with cfg._env(env):
                kconf = cfg._new_kconfig('shipped_links_' + shape)
            for prompt, (menu, on, off, opens) in sorted(self.SHIPPED.items()):
                with self.subTest(shape=shape, link=prompt), \
                        patch.multiple(menuconfig, create=True, _kconf=kconf, _show_all=False):
                    kconf.unset_values()
                    link = [n for n in self.links(kconf)
                            if n.prompt[0] == prompt
                            and menuconfig._parent_menu(n).prompt[0] == menu]
                    self.assertTrue(link)
                    apply(kconf, off)
                    self.assertFalse(any(menuconfig._reachable(n) for n in link))
                    apply(kconf, on)
                    link = [n for n in link if menuconfig._reachable(n)]
                    self.assertEqual(len(link), 1)
                    target = menuconfig._link_target(link[0])
                    self.assertIsNotNone(target)
                    path = menuconfig._menu_path_prompts(menuconfig._parent_menu(target))
                    self.assertEqual(path[1], "Macro Variables")
                    self.assertTrue(path[2].startswith(opens + " "), path[2])
            self.assertEqual(
                sorted(self.SHIPPED),
                sorted({n.prompt[0] for n in self.links(kconf)}),
                "every shipped link should be in SHIPPED")


class TestCfgHelpLabel(unittest.TestCase):
    """The help separator shows the .cfg option the selected symbol or choice sets"""

    KCONFIG = """
max_gate := 11

config PARAM_SPEED
    int "Speed"
    default 1

config BOOL_TOGGLE
    bool "Toggle"

choice CHOICE_LEVEL
    prompt "Level"
    default CHOICE_LEVEL_LOW

config CHOICE_LEVEL_LOW
    bool "Low"

config CHOICE_LEVEL_HIGH
    bool "High"

endchoice
"""

    SPEED = ("mmu_parameters_unit0.cfg", "mmu_unit_parameters unit0", "gear_load_speed")
    LEVEL = ("mmu.cfg", "mmu_parameters", "log_level")

    def setUp(self):
        import os
        import tempfile
        import kconfiglib
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        path = os.path.join(tmp.name, "Kconfig")
        with open(path, "w") as f:
            f.write(self.KCONFIG)
        self.kconf = kconfiglib.Kconfig(path, warn=False)
        patcher = patch.multiple(menuconfig, create=True, _kconf=self.kconf, _show_all=False,
                                 _conf_filename=os.path.join(tmp.name, ".mmu_config"),
                                 _cfg_locs={"PARAM_SPEED": [self.SPEED], "CHOICE_LEVEL": [self.LEVEL]},
                                 _cfg_locs_renders=0, _show_name=False, _conf_changed=False, _menu_scroll=0)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HH_MENUCONFIG_CFG_TIMING", None)
        self.select(self.kconf.top_node, self.kconf.syms["PARAM_SPEED"])

    def select(self, menu, item):
        menuconfig._cur_menu = menu
        menuconfig._shown = menuconfig._shown_nodes(menu)
        menuconfig._sel_node_i = [n.item for n in menuconfig._shown].index(item)

    def labels(self, width):
        """(file label, option label) as _draw_main places them, with ' Help ' centred"""
        help_x = (width - 6) // 2
        return menuconfig._cfg_labels(1, help_x, help_x + 6, width - 1)

    def texts(self, width):
        return [label[1] if label else None for label in self.labels(width)]

    def test_file_left_and_option_right(self):
        option = " [mmu_unit_parameters unit0] gear_load_speed "
        self.assertEqual(self.labels(200), ((1, " (mmu_parameters_unit0.cfg) "), (199 - len(option), option)))

    def test_each_side_shortens_or_goes_before_help(self):
        self.assertEqual(self.texts(100), [" (mmu_parameters_unit0.cfg) ", " gear_load_speed "])
        self.assertEqual(self.texts(60), [None, " gear_load_speed "])
        self.assertEqual(self.texts(40), [None, None])

    def test_labels_stay_clear_of_help(self):
        for width in range(20, 200):
            help_x = (width - 6) // 2
            file_label, option_label = self.labels(width)
            if file_label:
                self.assertLessEqual(file_label[0] + len(file_label[1]), help_x - 2, width)
            if option_label:
                self.assertGreaterEqual(option_label[0], help_x + 8, width)
                self.assertLessEqual(option_label[0] + len(option_label[1]), width - 1, width)

    def test_other_locations_are_counted(self):
        menuconfig._cfg_locs["PARAM_SPEED"].append(self.SPEED)
        self.assertEqual(self.texts(200)[1], " [mmu_unit_parameters unit0] gear_load_speed (+1 more) ")
        self.assertEqual(self.texts(100)[1], " gear_load_speed (+1 more) ")

    def test_choice_and_its_members_show_what_the_choice_sets(self):
        expected = [" (mmu.cfg) ", " [mmu_parameters] log_level "]
        choice = self.kconf.named_choices["CHOICE_LEVEL"]
        self.select(self.kconf.top_node, choice)
        self.assertEqual(self.texts(100), expected)
        self.select(choice.nodes[0], self.kconf.syms["CHOICE_LEVEL_HIGH"])
        self.assertEqual(self.texts(100), expected)

    def test_symbol_without_a_location_has_no_label(self):
        self.select(self.kconf.top_node, self.kconf.syms["BOOL_TOGGLE"])
        self.assertEqual(self.labels(200), (None, None))

    def test_failed_render_shows_no_label(self):
        menuconfig._cfg_locs = None
        with patch("installer.cfg_import.symbol_locations", side_effect=RuntimeError("boom")):
            self.assertEqual(self.labels(200), (None, None))
        self.assertEqual(menuconfig._cfg_locs, {})

    def test_a_change_renders_again(self):
        menuconfig._set_val(self.kconf.syms["PARAM_SPEED"], "5")
        self.assertIsNone(menuconfig._cfg_locs)

    def test_timing_log(self):
        import os
        log = os.path.join(self.tmp, "timing")
        os.environ["HH_MENUCONFIG_CFG_TIMING"] = log
        menuconfig._cfg_locs = None
        with patch("installer.cfg_import.symbol_locations", return_value={"PARAM_SPEED": [self.SPEED]}), \
                patch("installer.cfg_import.choice_locations", return_value={}):
            menuconfig._cfg_locations()
            menuconfig._cfg_locations_changed()
            menuconfig._cfg_locations()
        with open(log) as f:
            lines = f.read().splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["startup", "refresh"])
        self.assertTrue(all(line.endswith(" 1 entries") for line in lines))


class FakeDialogWindow(FakeWindow):
    def __init__(self, height=20, width=80):
        self.height, self.width = height, width
        self.rows, self.selected = {}, []
        self.y = self.x = 0

    def getmaxyx(self):
        return self.height, self.width

    def resize(self, height, width):
        self.height, self.width = height, width

    def mvwin(self, y, x):
        pass

    def erase(self):
        self.rows, self.selected = {}, []

    def noutrefresh(self):
        pass

    def move(self, y, x):
        self.y, self.x = y, x

    def getyx(self):
        return self.y, self.x

    def addnstr(self, y, x, text, maxlen, *attr):
        row = self.rows.setdefault(y, [" "] * self.width)
        for i, c in enumerate(text[:maxlen]):
            if x + i < self.width:
                row[x + i] = c
        if attr and attr[0] & menuconfig.curses.A_BOLD:
            self.selected.append(text[:maxlen].rstrip())
        self.y, self.x = y, x + len(text[:maxlen])

    def addstr(self, y, x, text, *attr):
        self.addnstr(y, x, text, len(text), *attr)

    def attron(self, *args):
        pass

    def attroff(self, *args):
        pass

    def hline(self, *args):
        pass

    def vline(self, *args):
        pass

    def addch(self, *args):
        pass

    def text(self):
        return "\n".join("".join(self.rows[y]).rstrip() for y in sorted(self.rows))


class TestSequenceEditor(unittest.TestCase):

    KCONFIG = """
config RESTRUCTURE
    bool
    default "$(RESTRUCTURE)"

config UNITS
    string "Units"
    default "a,b"
    sequence_editor "," "a,b,c"
    append_only_unless RESTRUCTURE
"""

    def setUp(self):
        import os
        import tempfile
        import kconfiglib
        self.os = os
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = os.path.join(self.tmp.name, "Kconfig")
        with open(path, "w") as f:
            f.write(self.KCONFIG)
        self.config = os.path.join(self.tmp.name, ".config")
        os.environ["RESTRUCTURE"] = ""
        self.addCleanup(os.environ.pop, "RESTRUCTURE", None)
        self.kconf = kconfiglib.Kconfig(path, warn=False)
        self.sym = self.kconf.syms["UNITS"]
        patcher = patch.multiple(menuconfig, create=True, _kconf=self.kconf,
                                 _conf_filename=self.config, _sequence_models={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_attributes_are_parsed(self):
        self.assertEqual(self.sym.sequence_editor, ",")
        self.assertEqual(self.sym.sequence_baseline, "a,b,c")
        self.assertIsNotNone(self.sym.append_only_unless)
        self.assertIsNone(self.sym.array_editor)

    def test_model_starts_from_baseline_and_current_value(self):
        model = menuconfig._sequence_model(self.sym)
        self.assertEqual(model.baseline, ["a", "b", "c"])
        self.assertEqual(model.names(), ["a", "b"])
        self.assertEqual(model.removed, ["c"])
        self.assertTrue(model.append_only)

    def test_append_only_follows_the_expression(self):
        import kconfiglib
        self.os.environ["RESTRUCTURE"] = "y"
        kconf = kconfiglib.Kconfig(self.os.path.join(self.tmp.name, "Kconfig"), warn=False)
        self.assertFalse(menuconfig._sequence_model(kconf.syms["UNITS"]).append_only)

    def test_save_writes_changes_only_for_edited_symbols(self):
        import sequence_edit
        changes = sequence_edit.changes_path(self.config, "UNITS")
        menuconfig._write_config(self.config)
        self.assertFalse(self.os.path.exists(changes))

        model = menuconfig._sequence_model(self.sym)
        model.append_only = False
        model.rename(0, "x")
        menuconfig._sequence_models["UNITS"] = model
        menuconfig._write_config(self.config)
        self.assertEqual(sequence_edit.read_changes(changes)["origin"], {"x": "a", "b": "b"})

    def drive(self, keys, inputs=(), restructure=True, pending=()):
        """Run the dialog on UNITS with the given key presses and input dialog answers."""
        if restructure:
            import kconfiglib
            self.os.environ["RESTRUCTURE"] = "y"
            self.kconf = kconfiglib.Kconfig(self.os.path.join(self.tmp.name, "Kconfig"), warn=False)
            self.sym = self.kconf.syms["UNITS"]
            menuconfig._kconf = self.kconf
        queued = list(pending)

        def get_wch():
            if not queued:
                raise menuconfig.curses.error()
            return queued.pop(0)

        window = SimpleNamespace(keypad=lambda flag: None, timeout=lambda ms: None, get_wch=get_wch)
        stubs = dict(_styled_win=lambda style: window,
                     _resize_sequence_dialog=lambda *a: None, _draw_sequence_dialog=lambda *a: None,
                     _draw_main=lambda: None, _resize_main=lambda: None, _update_menu=lambda: None)
        with patch.multiple(menuconfig, **stubs), \
                patch.object(menuconfig.curses, "doupdate"), \
                patch.object(menuconfig, "_getch_compat", side_effect=list(keys)), \
                patch.object(menuconfig, "_input_dialog", side_effect=list(inputs)), \
                patch.object(menuconfig, "_msg"), \
                patch.object(menuconfig, "_error") as error:
            menuconfig._sequence_dialog(self.sym.nodes[0])
        return error

    def test_dialog_rename_and_move(self):
        # Rows: a, b, then removed c. Rename a, move it below b
        error = self.drive(["r", "J", "\n"], ["box"])
        error.assert_not_called()
        self.assertEqual(self.sym.str_value, "b, box")
        self.assertEqual(menuconfig._sequence_models["UNITS"].origins(), {"b": "b", "box": "a"})

    def test_dialog_restore_removed_entry(self):
        self.drive(["j", "j", "d", "\n"])
        self.assertEqual(self.sym.str_value, "a, b, c")
        self.assertFalse(menuconfig._sequence_models["UNITS"].is_changed())

    def test_dialog_add_and_insert(self):
        self.drive(["a", "k", "k", "i", "\n"], ["z", "y"])
        self.assertEqual(self.sym.str_value, "y, a, b, z")

    def test_dialog_unedited_list_keeps_its_spelling(self):
        with patch.object(menuconfig, "_conf_changed", False):
            self.drive(["\n"])
            self.assertFalse(menuconfig._conf_changed)
        self.assertEqual(self.sym.str_value, "a,b")

    def test_dialog_escape_discards(self):
        self.drive(["r", "\x1b"], ["box"])
        self.assertEqual(self.sym.str_value, "a,b")
        self.assertNotIn("UNITS", menuconfig._sequence_models)

    def test_dialog_append_only_refuses_rename(self):
        error = self.drive(["r", "a", "\n"], ["box", "d"], restructure=False)
        error.assert_called_once()
        self.assertIn("appending", error.call_args.args[0])
        self.assertEqual(self.sym.str_value, "a, b, d")

    def test_dialog_keeps_at_least_one_entry(self):
        error = self.drive(["d", "d", "\n"])
        error.assert_called_once()
        self.assertIn("at least one", error.call_args.args[0])
        self.assertEqual(self.sym.str_value, "b")

    def test_raw_shift_arrow_sequence_moves_instead_of_cancelling(self):
        # ESC is followed by an xterm Shift-Down the terminfo didn't translate
        self.drive(["\x1b", "\n"], pending=list("[1;2B"))
        self.assertEqual(self.sym.str_value, "b, a")

    def test_escape_sequence_stops_at_its_final_byte(self):
        # The key after the sequence is still handled as a key
        self.drive(["\x1b", "\n"], pending=list("[1;2B") + ["J"])
        self.assertEqual(self.sym.str_value, "b, a")

    def test_unknown_escape_sequence_is_ignored(self):
        self.drive(["r", "\x1b", "\n"], ["box"], pending=list("[1;5C"))
        self.assertEqual(self.sym.str_value, "box, b")

    def test_dialog_draws_rows_status_and_keys(self):
        import sequence_edit
        model = sequence_edit.SequenceModel(["a", "b", "c"], ["a", "b", "c"])
        model.rename(1, "box")
        model.remove(2)
        model.add("left")
        entries = [(n, o, False) for n, o in model.rows] + [(n, n, True) for n in model.removed]
        win = FakeDialogWindow()
        with patch.object(menuconfig, "_style", {"list": 0, "frame": 0}, create=True), \
                patch.object(menuconfig, "_stdscr", FakeDialogWindow(40, 100), create=True):
            menuconfig._resize_sequence_dialog(win, "Units (list)", len(entries))
            menuconfig._draw_sequence_dialog(win, "Units (list)", entries, 1, model)
        text = win.text()
        for expected in ("   1. a", "  > 2. box  (renamed from b)", "   3. left  [new]",
                         "   x  c  [will be removed]", "rename b -> box; add left; remove c",
                         "[r] Rename", "[K/J] Move entry up/down", "[Enter] Done"):
            self.assertIn(expected, text)
        self.assertEqual(win.selected, ["> 2. box  (renamed from b)"])

    def test_row_labels(self):
        self.assertEqual(menuconfig._sequence_row_str(0, "a", "a", False), " 1. a")
        self.assertEqual(menuconfig._sequence_row_str(1, "x", None, False), " 2. x  [new]")
        self.assertEqual(menuconfig._sequence_row_str(2, "y", "b", False), " 3. y  (renamed from b)")
        self.assertEqual(menuconfig._sequence_row_str(3, "c", "c", True), " x  c  [will be removed]")

    def test_shift_arrows_move_entries(self):
        self.drive([menuconfig.curses.KEY_SF, "\n"])
        self.assertEqual(self.sym.str_value, "b, a")
        self.drive(["j", menuconfig.curses.KEY_SR, "\n"])
        self.assertEqual(self.sym.str_value, "a,b")

    def test_move_keys(self):
        with patch.object(menuconfig.curses, "keyname", side_effect=lambda c: {600: b"kUP2", 601: b"kDN2"}.get(c, b"x")):
            self.assertEqual([menuconfig._sequence_move_key(k) for k in ("K", "J", 600, 601, 999, "x")],
                             [-1, 1, -1, 1, 0, 0])

    def test_array_editor_still_allows_duplicates_and_empty(self):
        sym = SimpleNamespace(orig_type=menuconfig.STRING, array_editor=",",
                              array_size_sym=None, validator=None)
        with patch.object(menuconfig, "_error") as error:
            self.assertTrue(menuconfig._check_valid(sym, "a,a"))
            self.assertTrue(menuconfig._check_valid(sym, ""))
        error.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestResetMarksChanged(unittest.TestCase):
    """'r' on a value the user set must make quitting save, even when nothing else changed"""

    KCONFIG = """
config PARAM_DISTANCE
    int "Distance"
    default 40

choice CHOICE_LEVEL
    prompt "Level"
    default CHOICE_LEVEL_LOW
config CHOICE_LEVEL_LOW
    bool "Low"
config CHOICE_LEVEL_HIGH
    bool "High"
endchoice

config MMU_HAS_CUTTER
    bool "Cutter"

config MMU_HAS_CUTTER
    prompt "Cutter" if n
    forceshow
"""

    def setUp(self):
        import os
        import tempfile
        import kconfiglib
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "Kconfig")
        with open(path, "w") as f:
            f.write(self.KCONFIG)
        self.kconf = kconfiglib.Kconfig(path, warn=False)
        patches = [patch.object(menuconfig, "_update_menu"), patch.object(menuconfig, "_leave_menu"),
                   patch.object(menuconfig, "_kconf", self.kconf, create=True),
                   patch.object(menuconfig, "_conf_changed", False, create=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def node(self, name):
        item = self.kconf.named_choices.get(name) or self.kconf.syms[name]
        return item.nodes[0]

    def test_resetting_a_user_value_marks_the_config_changed(self):
        self.kconf.syms["PARAM_DISTANCE"].set_value("50")
        menuconfig._reset_key(self.node("PARAM_DISTANCE"))
        self.assertTrue(menuconfig._conf_changed)
        self.assertEqual(self.kconf.syms["PARAM_DISTANCE"].str_value, "40")

    def test_resetting_a_chosen_list_option_marks_the_config_changed(self):
        self.kconf.syms["CHOICE_LEVEL_HIGH"].set_value(2)
        menuconfig._reset_key(self.node("CHOICE_LEVEL"))
        self.assertTrue(menuconfig._conf_changed)
        self.assertEqual(self.kconf.named_choices["CHOICE_LEVEL"].selection.name, "CHOICE_LEVEL_LOW")

    def test_resetting_a_default_changes_nothing(self):
        menuconfig._reset_key(self.node("PARAM_DISTANCE"))
        self.assertFalse(menuconfig._conf_changed)

    def test_a_force_shown_read_only_entry_is_not_reset(self):
        self.kconf.syms["MMU_HAS_CUTTER"].set_value(2)
        menuconfig._reset_key(self.kconf.syms["MMU_HAS_CUTTER"].nodes[1])
        self.assertFalse(menuconfig._conf_changed)
        self.assertEqual(self.kconf.syms["MMU_HAS_CUTTER"].str_value, "y")
