# Happy Hare test harness - installer/parser.py, the config reader/writer used by install.sh.
#
# TWO THINGS ARE PINNED HERE, both of which have bitten real users:
#
# 1. A whole-line comment inside a multi-line value must not end the value. Klipper reads
#    configs with configparser, which treats such a line as invisible and resumes the value
#    on the next indented line, so a user commenting out one of their [quad_gantry_level]
#    "points:" lines has a perfectly valid printer.cfg. HH's parser used to end the value at
#    the comment and then choke on "50, 10" as if it were a new option, and `make install`
#    died with a traceback.
#
# 2. Error recovery must never damage the file. install_includes()/uninstall_includes()
#    rewrite the user's printer.cfg wholesale, so a config HH could not fully parse still
#    gets written back out. The skipped text is therefore kept verbatim and the round trip
#    has to be byte-exact (bar the marker comment), and re-running has to be idempotent -
#    otherwise a parse error silently corrupts a printer config, which is worse than the
#    traceback we replaced.
#
#   ./venv/bin/python -m unittest test.installer.test_parser
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import configparser
import logging
import unittest

from installer.parser import ConfigBuilder, Parser, PARSE_ERROR_MARKER

logging.getLogger().setLevel(logging.CRITICAL)    # _recover() logs at ERROR by design


def build(buf, marker=True, recover=True):
    builder = ConfigBuilder(parser=Parser(recover=recover, error_marker=marker))
    builder.read_buf(buf)
    return builder


def klipper_value(buf, section, option):
    """What Klipper itself would read for this option, via the same configparser call"""
    cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(";", "#"))
    cp.read_string(buf)
    return cp.get(section, option)


# The reported failure: a dual QuattroBox user with two of their gantry points commented out
QGL = """[quad_gantry_level]
gantry_corners:
\t-60,-10
\t410,420
points:
### 30, 5
\t50, 10
\t150, 225
### 270, 5
speed: 100

[stepper_x]
step_pin: PB1
"""


class TestCommentedOutValueLines(unittest.TestCase):

    def test_the_reported_config_parses(self):
        builder = build(QGL)
        self.assertEqual(builder.parse_errors(), [])
        self.assertEqual(builder.sections(), ["quad_gantry_level", "stepper_x"])

    def test_value_matches_what_klipper_reads(self):
        got = build(QGL).get("quad_gantry_level", "points")
        expected = klipper_value(QGL, "quad_gantry_level", "points")
        # HH preserves indentation verbatim and strips the value; configparser does neither
        self.assertEqual([l.strip() for l in got.splitlines() if l.strip()],
                         [l.strip() for l in expected.splitlines() if l.strip()])

    def test_commented_lines_are_not_options(self):
        builder = build(QGL)
        self.assertEqual(builder.options("quad_gantry_level"), ["gantry_corners", "points", "speed"])

    def test_following_option_is_still_seen(self):
        self.assertEqual(build(QGL).get("quad_gantry_level", "speed"), "100")

    def test_round_trip_is_byte_exact(self):
        self.assertEqual(build(QGL).write(), QGL)

    def test_trailing_comment_after_value_stays_out_of_it(self):
        """The '### 270, 5' line is followed by a non-indented option, so the value ends"""
        self.assertNotIn("270", build(QGL).get("quad_gantry_level", "points"))

    def test_blank_line_still_ends_a_value(self):
        buf = "[s]\npoints:\n\t1,2\n\nother: 3\n"
        builder = build(buf)
        self.assertEqual(builder.get("s", "points"), "1,2")
        self.assertEqual(builder.get("s", "other"), "3")

    def test_comment_run_then_indented_line_continues(self):
        buf = "[s]\npoints:\n# one\n; two\n\t1,2\nother: 3\n"
        builder = build(buf)
        self.assertEqual(builder.get("s", "points"), "1,2")
        self.assertEqual(builder.get("s", "other"), "3")
        self.assertEqual(builder.write(), buf)

    def test_comment_then_non_indented_option_does_not_continue(self):
        buf = "[s]\npoints: 1,2\n# a comment\nother: 3\n"
        builder = build(buf)
        self.assertEqual(builder.get("s", "points"), "1,2")
        self.assertEqual(builder.get("s", "other"), "3")

    def test_comment_at_end_of_file_does_not_continue(self):
        buf = "[s]\npoints: 1,2\n# trailing\n"
        builder = build(buf)
        self.assertEqual(builder.get("s", "points"), "1,2")
        self.assertEqual(builder.write(), buf)

    def test_gcode_options_are_untouched_by_the_new_rule(self):
        buf = "[gcode_macro FOO]\ngcode:\n    G1 X1\n# note\n    G1 X2\n\n[stepper_x]\nstep_pin: PB1\n"
        builder = build(buf)
        self.assertEqual(builder.sections(), ["gcode_macro FOO", "stepper_x"])
        self.assertEqual(builder.write(), buf)


# Genuinely broken: '50, 10' at column 0 is neither a section nor an option, so nothing can
# make sense of it - Klipper rejects this file too. All HH has to do is survive and say so
BROKEN = """[quad_gantry_level]
points:
### 30, 5
50, 10
150, 225
speed: 100

[stepper_x]
step_pin: PB1
"""


class TestErrorRecovery(unittest.TestCase):

    def test_parse_does_not_raise(self):
        builder = build(BROKEN)
        errors = builder.parse_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("50, 10", errors[0].value)
        self.assertEqual(errors[0].line, 4)

    def test_strict_mode_still_raises(self):
        with self.assertRaises(SyntaxError):
            build(BROKEN, recover=False)

    def test_parsing_resumes_after_the_bad_region(self):
        builder = build(BROKEN)
        self.assertEqual(builder.sections(), ["quad_gantry_level", "stepper_x"])
        self.assertEqual(builder.get("quad_gantry_level", "speed"), "100")
        self.assertEqual(builder.get("stepper_x", "step_pin"), "PB1")

    def test_round_trip_loses_nothing(self):
        """Every original line survives; the only addition is the marker comment"""
        out = build(BROKEN).write()
        added = [l for l in out.splitlines() if l not in BROKEN.splitlines()]
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0].startswith(PARSE_ERROR_MARKER))
        self.assertEqual([l for l in out.splitlines() if l != added[0]], BROKEN.splitlines())

    def test_round_trip_is_byte_exact_without_the_marker(self):
        self.assertEqual(build(BROKEN, marker=False).write(), BROKEN)

    def test_marker_lands_on_its_own_line_before_the_resync_point(self):
        lines = build(BROKEN).write().splitlines()
        idx = [i for i, l in enumerate(lines) if l.startswith(PARSE_ERROR_MARKER)][0]
        self.assertEqual(lines[idx - 1], "150, 225")
        self.assertEqual(lines[idx + 1], "speed: 100")

    def test_re_running_is_idempotent(self):
        """A second install must not stack up a second marker or drift the resync point"""
        once = build(BROKEN).write()
        twice = build(once).write()
        self.assertEqual(twice, once)
        self.assertEqual(once.count(PARSE_ERROR_MARKER), 1)

    def test_marker_survives_an_include_being_added_above_it(self):
        """
        The real install adds [include] sections at the top, shifting every line below. The
        marker must come back out identical or every install would churn the user's config
        """
        builder = build(BROKEN)
        builder.add_section("include mmu/base/*.cfg", at_top=True)
        once = builder.write()
        self.assertEqual(build(once).write(), once)

    def test_several_bad_regions_are_each_reported(self):
        buf = "[a]\nfoo:\n#x\n1,2\nbar: 1\n\n[b]\nbaz:\n#y\n3,4\nqux: 2\n"
        builder = build(buf)
        self.assertEqual(len(builder.parse_errors()), 2)
        self.assertEqual(builder.sections(), ["a", "b"])
        self.assertEqual(builder.get("a", "bar"), "1")
        self.assertEqual(builder.get("b", "qux"), "2")

    def test_includes_are_never_swallowed_by_recovery(self):
        """
        A section hidden inside a skipped region would be invisible to has_section(), so
        install_includes() would add a duplicate and uninstall_includes() would leave a stale
        include behind pointing at deleted files. Recovery stops at any line starting a
        section, so no header can end up inside the skipped text - however far the region
        would otherwise run
        """
        tail = "[include mmu/base/*.cfg]\n"
        for name, buf in [
            ("straight after the bad line", "[a]\nfoo:\n#x\n1,2\n" + tail),
            ("comments in between",         "[a]\nfoo:\n#x\n1,2\n# note\n### more\n" + tail),
            ("blank lines in between",      "[a]\nfoo:\n#x\n1,2\n\n\n" + tail),
            ("header has inline comment",   "[a]\nfoo:\n#x\n1,2\n# note\n[include mmu/base/*.cfg]  # hh\n"),
        ]:
            builder = build(buf)
            self.assertTrue(builder.parse_errors(), name)
            self.assertTrue(builder.has_section("include mmu/base/*.cfg"), name)

            # ...and uninstall_includes() can therefore still take it back out
            builder.remove_section("include mmu/base/*.cfg")
            self.assertNotIn("[include mmu/base/*.cfg]", builder.write(), name)

    def test_add_section_still_works_around_unparsed_text(self):
        builder = build(BROKEN)
        builder.add_section("include mmu/base/*.cfg", at_top=True)
        out = builder.write()
        self.assertTrue(out.startswith("[include mmu/base/*.cfg]"))
        self.assertIn("50, 10", out)
        self.assertEqual(build(out).sections()[0], "include mmu/base/*.cfg")

    def test_include_is_inserted_on_its_own_line_next_to_unparsed_text(self):
        """
        install_includes()' at_top=False path inserts after the last [include], which can be
        immediately followed by an unparsed region - the new header must not land mid-line
        """
        buf = "[include mmu/base/*.cfg]\n50, 10\n\n[stepper_x]\nstep_pin: PB1\n"
        builder = build(buf)
        builder.add_section("include mmu/optional/client_macros.cfg", comment="Client macros", at_top=False)
        out = builder.write()
        self.assertIn("\n[include mmu/optional/client_macros.cfg]\n", out)
        self.assertIn("50, 10", out)
        self.assertEqual(build(out).sections()[:2],
                         ["include mmu/base/*.cfg", "include mmu/optional/client_macros.cfg"])

    def test_option_with_no_value_at_eof_recovers(self):
        builder = build("[a]\nfoo")
        self.assertEqual(len(builder.parse_errors()), 1)
        self.assertEqual(builder.write(), "[a]\nfoo\n{}: Unexpected end of file after 'foo' - line(s) "
                                          "above left untouched, please fix\n".format(PARSE_ERROR_MARKER))

    def test_a_hopeless_file_gives_up_without_losing_anything(self):
        """Past MAX_RECOVERIES the rest of the file is kept verbatim rather than raising"""
        buf = "".join("[s%d]\nfoo:\n#x\n1,2\n" % i for i in range(60))
        builder = build(buf, marker=False)
        self.assertTrue(builder.parse_errors())
        self.assertEqual(builder.write(), buf)

    def test_garbage_only_file_recovers(self):
        builder = build("!!! nonsense\n@@@ more\n")
        self.assertEqual(len(builder.parse_errors()), 1)
        self.assertIn("nonsense", builder.write())


if __name__ == "__main__":
    unittest.main()
