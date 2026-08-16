# Tests for the interactive console (test/console.py) and install-directory profiles.
#
# A dev tool with no test rots, which is what --script is for: it runs the same code path
# as the prompt, so the loop, the renderer, the meta-commands and the header are all
# exercised without a TTY.
#
# The install-directory tests deliberately SYNTHESISE an install-shaped tree from the
# harness's own render rather than running ./install.sh. Running the installer needs
# menuconfig (a curses TUI), and a headless `olddefconfig` config does not produce a
# bootable machine - it leaves the gate-0 gear pins empty. Synthesising keeps the test
# deterministic and offline while still exercising the real loader.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import contextlib
import io
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from test.hh import cfg as cfg_mod
from test.hh import profiles, session
from test import console as console_mod

logging.getLogger().setLevel(logging.CRITICAL)


NEUTRAL_SGR = {'0', '', '39', '22', '49'}


def check_no_line_leaks(case, rendered):
    """
    No line may end with a colour or bold still open - see the pink-terminal bug.

    Module level rather than a TestRenderer method because it is the strongest assertion
    available about wrap_ansi() too: a wrapped row is a line like any other.
    """
    for line in rendered.split('\n'):
        codes = __import__('re').findall(r'\x1b\[([0-9;]*)m', line)
        if codes:
            case.assertIn(codes[-1], NEUTRAL_SGR,
                          'line ends with attribute %r still open: %r' % (codes[-1], line))


def visible(text):
    """`text` with every escape sequence removed - what the terminal actually shows."""
    return console_mod._CSI.sub('', text)


class FakeTty(io.StringIO):
    """A StringIO that claims to be a terminal, for the tty-gated paths."""

    def isatty(self):
        return True

    def fileno(self):
        return 1


@contextlib.contextmanager
def no_tty():
    """
    The mirror image of FakeTty: force both streams to look like pipes.

    Console derives its interactive defaults from sys.stdout.isatty() (test/console.py:478)
    and _arm_tick re-checks stdout AND stdin at call time, so a test that pins the
    NON-interactive behaviour has to say so rather than inherit it. Without this the
    affected tests pass under `make test | cat` - and under CI, and under an agent's piped
    shell - then fail for anyone running the same suite straight from a terminal, which is
    the one way it is most often run by hand.
    """
    with mock.patch.object(sys, 'stdout', io.StringIO()), \
            mock.patch.object(sys, 'stdin', io.StringIO()):
        yield


def make_install_tree(root, printer_cfg=True, macros=True, mmu_vars=True):
    """An install-shaped directory, laid out exactly as ./install.sh -z -t leaves one."""
    rendered = cfg_mod.render(profiles.get('boxturtle'))
    base = os.path.join(root, 'mmu', 'base')
    os.makedirs(base)
    for tmpl, text in rendered.items():
        with open(os.path.join(base, os.path.basename(tmpl)), 'w', encoding='utf-8') as f:
            f.write(text)
    if macros:
        mdir = os.path.join(root, 'mmu', 'macros')
        os.makedirs(mdir)
        for name, text in cfg_mod.macro_files().items():
            with open(os.path.join(mdir, os.path.basename(name)), 'w',
                      encoding='utf-8') as f:
                f.write(text)
    if mmu_vars:
        # The installer ships this, but printer.cfg does not include it - it is reached
        # via [save_variables]. The loader must skip it.
        with open(os.path.join(root, 'mmu', 'mmu_vars.cfg'), 'w', encoding='utf-8') as f:
            f.write('[save_variables]\nfilename: /nonexistent/mmu_vars.cfg\n')
    if printer_cfg:
        with open(os.path.join(root, 'printer.cfg'), 'w', encoding='utf-8') as f:
            f.write('[include mmu/base/*.cfg]\n')
            if macros:
                f.write('[include mmu/macros/*.cfg]\n')
    return root


def make_multi_unit_tree(root, units=('unit0', 'unit1')):
    """
    A genuine multi-unit install tree, laid out as a real multi-unit install leaves one:
    one mmu_hardware_<unit>.cfg and one mmu_parameters_<unit>.cfg per unit (Makefile
    hh_unit_config_files), with mmu.cfg and mmu_macro_vars.cfg shared.

    Rendered through cfg.render()'s MULTI-UNIT path, which produces exactly those filenames
    and needs no post-processing - so the tree really is what the installer would write.

    This used to fake it by deriving boxturtle per unit with
    extra_params={'UNIT_NAME': unit, 'MCU_NAME': unit} and patching mmu.cfg's `units:` line
    with a regex. That produced sections named 'unit1' whose PINS still said 'unit0:', because
    extra_params are jinja params applied long after kconfiglib expanded $(MCU_NAME) at parse
    time. It did not matter for what these tests assert, but it was a misleading fixture to
    leave lying around now that a real path exists.
    """
    base = os.path.join(root, 'mmu', 'base')
    os.makedirs(base)
    os.makedirs(os.path.join(root, 'mmu', 'macros'))

    rendered = cfg_mod.render(profiles.clone_across_units(
        'boxturtle_x%d' % len(units), profiles.get('boxturtle'), units))
    for name, text in rendered.items():
        with open(os.path.join(base, os.path.basename(name)), 'w', encoding='utf-8') as fh:
            fh.write(text)

    for name, text in cfg_mod.macro_files().items():
        with open(os.path.join(root, 'mmu', 'macros', os.path.basename(name)), 'w',
                  encoding='utf-8') as fh:
            fh.write(text)
    with open(os.path.join(root, 'printer.cfg'), 'w', encoding='utf-8') as fh:
        fh.write('[include mmu/base/*.cfg]\n[include mmu/macros/*.cfg]\n')
    return root


class TestMultiUnit(unittest.TestCase):
    """
    Two units, 8 gates. The harness copes with this; the console used to assume one unit in
    two places (LED effect_state[0], and stripping the unit: prefix off sensor names).
    """

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='hh-multiunit-')
        make_multi_unit_tree(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_the_harness_boots_two_units_cleanly(self):
        hh = session(self.root)
        self.addCleanup(hh.close)
        hh.boot()
        machine = hh.mmu.mmu_machine
        self.assertEqual([u.name for u in machine.units], ['unit0', 'unit1'])
        self.assertEqual([u.num_gates for u in machine.units], [4, 4])
        self.assertEqual([u.first_gate for u in machine.units], [0, 4],
                         'gates must be contiguous across units')
        self.assertEqual(hh.mmu.num_gates, 8)
        self.assertEqual(hh.errors, [], 'multi-unit bootup was not clean')

    def test_each_units_pins_name_its_own_mcu(self):
        """
        Guards the FIXTURE, not the console. The tree used to be built by injecting
        UNIT_NAME/MCU_NAME as jinja params, which renamed the sections but left every pin
        pointing at unit0's board - a config wired to the wrong hardware that still loaded and
        booted. Nothing here noticed, so assert it directly.
        """
        import re
        base = os.path.join(self.root, 'mmu', 'base')
        for unit in ('unit0', 'unit1'):
            with open(os.path.join(base, 'mmu_hardware_%s.cfg' % unit),
                      encoding='utf-8') as fh:
                chips = {m.group(1) for m in re.finditer(r'[:!^~]?(unit\d+):', fh.read())}
            self.assertEqual(chips, {unit},
                             '%s hardware references chips %s' % (unit, sorted(chips)))

    def test_sensors_are_qualified_per_unit(self):
        hh = session(self.root)
        self.addCleanup(hh.close)
        hh.boot()
        names = hh.sensors()
        self.assertIn('unit0:mmu_shared_exit', names)
        self.assertIn('unit1:mmu_shared_exit', names)
        # A bare name is now ambiguous and must say so rather than pick one silently
        with self.assertRaises(KeyError) as ctx:
            hh.sensor('mmu_shared_exit')
        self.assertIn('ambiguous', str(ctx.exception))

    def _console(self, argv):
        console = console_mod.Console(console_mod.parse_args(
            ['--profile', self.root, '--plain', '--no-log'] + argv))
        self.addCleanup(console.close)
        console.boot()
        return console

    def test_the_console_sees_both_units(self):
        console = self._console(['--no-preload'])
        self.assertEqual(console.num_units, 2)
        self.assertEqual([u.name for u in console.units], ['unit0', 'unit1'])
        self.assertEqual(console.unit_of(0).name, 'unit0')
        self.assertEqual(console.unit_of(7).name, 'unit1')
        self.assertIsNone(console.unit_of(99))

    def test_the_sensor_header_keeps_unit_prefixes_when_multi_unit(self):
        """Otherwise both units' shared_exit render as the same ambiguous cell."""
        console = self._console(['--no-preload', '--header', 'sensors'])
        text = '\n'.join(console.header_lines())
        self.assertIn('unit0:mmu_shared_exit=', text)
        self.assertIn('unit1:mmu_shared_exit=', text)

    def test_the_filament_header_groups_gates_by_unit(self):
        console = self._console(['--no-preload', '--header', 'filament'])
        text = '\n'.join(console.header_lines())
        self.assertIn('unit0', text)
        self.assertIn('unit1', text)
        self.assertIn('gate 7', text, 'the second unit\'s gates are missing')

    def test_every_header_group_survives_multi_unit(self):
        console = self._console(['--no-preload', '--header',
                                 ','.join(console_mod.Console.GROUPS)])
        self.assertTrue(console.header_lines())      # must not raise


class TestInstallDirLoader(unittest.TestCase):
    """cfg.load_install_dir - reading a real install instead of rendering templates."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='hh-installdir-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_printer_cfg_includes_drive_the_file_set_and_order(self):
        make_install_tree(self.root)
        loaded = cfg_mod.load_install_dir(profiles.get(self.root))
        names = list(loaded)
        # mmu.cfg carries [mmu_machine] and MUST be parsed before the steppers in
        # mmu_hardware*.cfg, which is why Klipper's include glob is sorted.
        self.assertTrue(names[0].endswith('mmu.cfg'), names)
        self.assertTrue(any('mmu_hardware' in n for n in names), names)
        self.assertTrue(any('/macros/' in n for n in names), names)

    def test_mmu_vars_is_skipped(self):
        make_install_tree(self.root)
        loaded = cfg_mod.load_install_dir(profiles.get(self.root))
        self.assertFalse([n for n in loaded if n.endswith('mmu_vars.cfg')], sorted(loaded))

    def test_falls_back_to_globs_when_there_is_no_printer_cfg(self):
        """Pointing straight at the mmu/ directory has to work too."""
        make_install_tree(self.root, printer_cfg=False)
        mmu_dir = os.path.join(self.root, 'mmu')
        loaded = cfg_mod.load_install_dir(profiles.get(mmu_dir))
        self.assertTrue(list(loaded)[0].endswith('mmu.cfg'), list(loaded))

    def test_a_directory_that_is_not_an_install_says_so(self):
        with self.assertRaises(AssertionError) as ctx:
            cfg_mod.load_install_dir(profiles.get(self.root))
        self.assertIn('install.sh', str(ctx.exception))

    def test_macro_files_are_not_asserted_token_free(self):
        """
        config/macros/*.cfg are copied verbatim, not rendered, and legitimately contain
        the installer's own [[ ]] delimiter as a nested Klipper list literal. Asserting
        them would fail every install-directory boot.
        """
        make_install_tree(self.root)
        loaded = cfg_mod.load_install_dir(profiles.get(self.root))
        self.assertTrue(any('|min' in t or '|max' in t
                            for n, t in loaded.items() if '/macros/' in n),
                        'fixture no longer contains the token that used to false-positive')
        cfg_mod.assert_sane(loaded)                 # must not raise

    def test_unknown_profile_name_still_errors_helpfully(self):
        with self.assertRaises(KeyError) as ctx:
            profiles.get('not_a_profile')
        self.assertIn('boxturtle', str(ctx.exception))


class TestInstallDirBoots(unittest.TestCase):
    """An install directory must produce the same machine the profile does."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='hh-installboot-')
        make_install_tree(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_boots_cleanly_and_matches_the_boxturtle_profile(self):
        hh = session(self.root)
        self.addCleanup(hh.close)
        hh.boot()
        self.assertEqual(hh.errors, [], 'bootup from an install dir was not clean')
        self.assertEqual(hh.mmu.num_gates, 4)

        ref = session('boxturtle')
        self.addCleanup(ref.close)
        ref.boot()
        self.assertEqual(hh.mmu.num_gates, ref.mmu.num_gates)
        # A drift between the harness profiles and real installer output should be a test
        # failure, not a surprise in the console. Compare the whole parameter set rather
        # than a hand-picked few - same rendered templates in, so it must match exactly.
        keys = [k for k in dir(ref.mmu.p)
                if not k.startswith('_') and not callable(getattr(ref.mmu.p, k))]
        self.assertGreater(len(keys), 50, 'parameter introspection found almost nothing')
        differing = {k: (getattr(hh.mmu.p, k, '<missing>'), getattr(ref.mmu.p, k))
                     for k in keys
                     if getattr(hh.mmu.p, k, '<missing>') != getattr(ref.mmu.p, k)}
        self.maxDiff = None
        self.assertEqual(differing, {}, 'install dir and boxturtle profile disagree')

    def test_filament_moves_so_the_console_is_actually_usable(self):
        hh = session(self.root)
        self.addCleanup(hh.close)
        hh.boot()
        fil = hh.filament()
        hh.place_filament(0, position=-40.0)
        hh.run_gcode('MMU_PRELOAD GATE=0')
        hh.reactor.advance(0.)
        self.assertEqual(hh.mmu.gate_status[0], 1, 'preload did not mark the gate available')
        self.assertAlmostEqual(fil.tip[0], -100.0, places=1)
        self.assertEqual(hh.errors, [])


class TestOutputHandler(unittest.TestCase):
    """The gcode.register_output_handler hook the console streams through."""

    def test_handlers_see_info_and_raw_interleaved_in_order(self):
        hh = session('boxturtle')
        self.addCleanup(hh.close)
        hh.boot()
        seen = []
        hh.gcode.register_output_handler(seen.append)
        hh.gcode.respond_info('first')
        hh.gcode.respond_raw('!! second')
        hh.gcode.respond_info('third')
        self.assertEqual(seen, ['first', '!! second', 'third'])

    def test_a_broken_handler_cannot_break_the_machine(self):
        hh = session('boxturtle')
        self.addCleanup(hh.close)
        hh.boot()
        hh.gcode.register_output_handler(lambda msg: 1 / 0)
        before = len(hh.gcode.console)
        hh.gcode.respond_info('still recorded')
        self.assertEqual(len(hh.gcode.console), before + 1)


class TestRenderer(unittest.TestCase):
    """html_to_ansi - HH emits HTML, not ANSI (extras/mmu/mmu_logger.py:96)."""

    SAMPLE = '<span style="color:#90EE90">green</span> <b>bold</b> gap'

    def test_colour_becomes_ansi_in_every_mode(self):
        for mode, expect in (('truecolor', '\033[38;2;144;238;144m'),   # 90EE90
                             ('256', '\033[38;5;120m'),
                             ('16', '\033[92m')):
            out = console_mod.html_to_ansi(self.SAMPLE, color=True, mode=mode)
            self.assertIn(expect, out, mode)
            self.assertIn('\033[1m', out, mode)
            self.assertNotIn('<span', out, mode)
            self.assertNotIn('<b>', out, mode)

    def test_the_default_mode_is_not_truecolor(self):
        """Truecolor must be opt-in - see the pink-background bug."""
        out = console_mod.html_to_ansi(self.SAMPLE, color=True)
        self.assertNotIn('\033[38;2;', out)
        self.assertIn('\033[38;5;', out)

    def test_plain_strips_markup_entirely(self):
        out = console_mod.html_to_ansi(self.SAMPLE, color=False)
        self.assertEqual(out, 'green bold gap')
        self.assertNotIn('\033', out)

    NEUTRAL = NEUTRAL_SGR

    def assert_no_line_leaks(self, rendered):
        check_no_line_leaks(self, rendered)

    def test_a_multiline_span_does_not_leak_colour_across_lines(self):
        """
        Happy Hare's warnings are ONE multi-line message whose span opens before the first
        newline and closes after the last. Balanced per message, but a colour crossing a
        line break is what turned the terminal pink: ESC[2K and scrolling both act with the
        current attributes.
        """
        msg = ('<span style="color:#FF69B4">Warning: not calibrated\n'
               '- Use MMU_CALIBRATE_BOWDEN\n'
               '- and then re-run</span>')
        out = console_mod.html_to_ansi(msg, color=True, mode='truecolor')
        self.assert_no_line_leaks(out)
        # ...but the colour must still be applied to every line, not just the first
        for line in out.split('\n'):
            self.assertIn('\x1b[38;2;255;105;180m', line, line)
        # and the same must hold in the default mode
        self.assert_no_line_leaks(console_mod.html_to_ansi(msg, color=True))

    def test_an_unclosed_span_still_cannot_leak(self):
        out = console_mod.html_to_ansi('<span style="color:#FF69B4">no closing tag',
                                       color=True)
        self.assert_no_line_leaks(out)

    def test_bold_is_re_established_per_line_too(self):
        out = console_mod.html_to_ansi('<b>bold first\nbold second</b>', color=True)
        self.assert_no_line_leaks(out)
        self.assertEqual(out.count('\x1b[1m'), 2, out)

    def test_real_happy_hare_warnings_never_leak(self):
        """The regression, driven through the actual MMU rather than a fixture."""
        hh = session('boxturtle')
        self.addCleanup(hh.close)
        hh.boot()
        hh.filament()
        hh.run_gcode('MMU_STATUS')
        seen_colour = False
        for msg in hh.gcode.console + hh.gcode.raw:
            out = console_mod.html_to_ansi(msg, color=True)
            self.assert_no_line_leaks(out)
            seen_colour = seen_colour or '\x1b[38;' in out     # 38;5 by default, 38;2 if truecolor
        self.assertTrue(seen_colour, 'no coloured output at all - test proves nothing')

    # HH's palette (mmu_logger.py:100-106) plus a dynamic gate colour and a shorthand
    HH_COLOURS = ('C0C0C0', 'FF69B4', '90EE90', '87CEEB', '4169E1', 'F0A')

    def test_no_emitted_parameter_can_be_read_as_a_background_colour(self):
        """
        THE pink-background bug. A terminal without truecolor does not ignore
        ESC[38;2;R;G;Bm - it reads the channels as separate SGR codes. #FF69B4's green is
        0x69 = 105, and SGR 105 is bright-magenta BACKGROUND. So no default-mode parameter
        may ever land in 40-47 or 100-107.
        """
        import re
        for mode in ('256', '16'):
            for hexcol in self.HH_COLOURS:
                seq = console_mod.fg(*console_mod._hex_to_rgb(hexcol), mode=mode)
                for param in re.findall(r'\d+', seq):
                    self.assertFalse(40 <= int(param) <= 47 or 100 <= int(param) <= 107,
                                     'mode %s colour #%s emits %r, and %s is a background '
                                     'colour on a terminal that cannot parse it'
                                     % (mode, hexcol, seq, param))

    def test_truecolor_is_only_used_when_the_terminal_advertises_it(self):
        import unittest.mock as mock
        for env, expect in ((None, '256'), ('', '256'), ('truecolor', 'truecolor'),
                            ('24bit', 'truecolor'), ('8bit', '256')):
            environ = {} if env is None else {'COLORTERM': env}
            with mock.patch.dict(os.environ, environ, clear=True):
                got = 'truecolor' if console_mod.truecolor_supported() else '256'
                self.assertEqual(got, expect, 'COLORTERM=%r' % env)

    def test_the_16_colour_mode_only_uses_plain_foreground_codes(self):
        for hexcol in self.HH_COLOURS:
            seq = console_mod.fg(*console_mod._hex_to_rgb(hexcol), mode='16')
            code = int(seq[2:-1])
            self.assertTrue(30 <= code <= 37 or 90 <= code <= 97, (hexcol, seq))

    def test_each_mode_keeps_hh_palette_colours_distinguishable(self):
        """A safe mode is no good if every colour collapses to the same one."""
        probe = ('FF69B4', '90EE90', '87CEEB')      # HH's pink, green and cyan
        for mode in ('truecolor', '256'):
            seqs = {console_mod.fg(*console_mod._hex_to_rgb(h), mode=mode) for h in probe}
            self.assertEqual(len(seqs), 3, 'mode %s collapsed distinct colours' % mode)
        # 16 colours genuinely cannot represent everything, but these three are different
        # hues and must not all land on the same slot (they used to all become white).
        seqs = {console_mod.fg(*console_mod._hex_to_rgb(h), mode='16') for h in probe}
        self.assertEqual(len(seqs), 3, '16-colour mode collapsed three distinct hues')

    def test_greys_do_not_become_a_random_hue_in_16_colour_mode(self):
        for hexcol, expect in (('C0C0C0', '\033[37m'), ('000000', '\033[30m'),
                               ('FFFFFF', '\033[97m')):
            self.assertEqual(console_mod.fg(*console_mod._hex_to_rgb(hexcol), mode='16'),
                             expect, hexcol)

    def test_short_and_long_hex_colours_are_handled(self):
        """HH's CONSOLE_COLOR_SPAN_RE accepts 3-8 hex digits, not just 6."""
        for digits, expect in (('F0A', '\x1b[38;2;255;0;170m'),
                               ('FF69B4', '\x1b[38;2;255;105;180m'),
                               ('FF69B4AA', '\x1b[38;2;255;105;180m')):
            out = console_mod.html_to_ansi(
                '<span style="color:#%s">x</span>' % digits, color=True, mode='truecolor')
            self.assertIn(expect, out, digits)
            self.assertNotIn('<span', out, digits)

    def test_non_breaking_spaces_become_spaces(self):
        """UI_SPACE is \\u00a0; the file logger substitutes it but the console path does not."""
        self.assertNotIn(' ', console_mod.html_to_ansi('a b', color=True))


class TestBootupOutput(unittest.TestCase):
    """
    cmd_MMU_BOOTUP prints Happy Hare's welcome and its calibration warnings. The console
    used to register its output handler AFTER boot(), so the entire startup was captured by
    the harness and never shown - the welcome simply vanished.
    """

    def test_the_handler_is_registered_before_boot_so_startup_is_captured(self):
        console = console_mod.Console(console_mod.parse_args(
            ['--no-preload', '--no-log', '--plain']))
        self.addCleanup(console.close)
        console.boot()
        self.assertTrue(console.startup_output, 'no bootup output captured at all')
        self.assertTrue(console.hh.fired('mmu:bootup'), 'bootup never ran')

    def test_the_welcome_message_reaches_the_banner(self):
        console = console_mod.Console(console_mod.parse_args(
            ['--no-preload', '--no-log', '--plain']))
        self.addCleanup(console.close)
        console.boot()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            console.banner()
        out = buf.getvalue()
        self.assertIn('Happy Hare', out)
        self.assertIn('Ready', out, 'the welcome banner is missing')
        self.assertIn('(\\_/)', out, 'the rabbit is missing')

    def test_startup_output_is_not_swallowed_by_the_preload(self):
        """_preload_all clears the sink; it must not clear the bootup output with it."""
        console = console_mod.Console(console_mod.parse_args(['--no-log', '--plain']))
        self.addCleanup(console.close)
        console.boot()
        joined = ' '.join(console.startup_output)
        self.assertIn('Ready', joined)
        self.assertEqual(console.sink, [], 'preload noise leaked into the console history')

    def _avail_row(self, console):
        """The gate-availability row of the bootup table. One sink entry is one MESSAGE, and
        the whole table arrives as a single multi-line one, so split before matching."""
        for entry in console.startup_output:
            for line in entry.split('\n'):
                if line.startswith('Avail:'):
                    return line
        self.fail('bootup printed no gate table')

    def test_bootup_reports_the_gates_the_console_is_about_to_preload(self):
        """
        __MMU_BOOTUP prints the gate table, and _preload_all() runs after boot() returns - so
        the banner used to say the whole machine was unknown about one that is fully loaded by
        the time the prompt appears, and that banner is the last thing on screen. boot() now
        seeds it (Session.seed_loaded_gates) exactly as a real printer's mmu_vars.cfg would.

        The default profile is ercf_vvd, which is what makes this worth asserting: ERCF has no
        per-gate switches and takes the persisted map, while ViViD re-derives its gates from
        mmu_entry_9..12 at bootup. Seeding the map alone left ViViD's four reading EMPTY.
        """
        console = console_mod.Console(console_mod.parse_args(['--no-log', '--plain']))
        self.addCleanup(console.close)
        console.boot()
        # AFTER boot(), never before: importing `extras` while the fake klippy tree is not yet
        # on sys.path binds the repo's own extras/ into sys.modules and every later session in
        # this process fails root.install()'s leak assertion.
        from extras.mmu.mmu_constants import GATE_AVAILABLE
        mmu = console.hh.mmu
        self.assertEqual(mmu.gate_status, [GATE_AVAILABLE] * mmu.num_gates)
        row = self._avail_row(console)
        self.assertNotIn('?', row, 'a gate still reads unknown in the bootup table: %r' % row)
        self.assertNotIn('-', row, 'a gate still reads empty in the bootup table: %r' % row)
        self.assertEqual(console.hh.errors, [], 'seeding made bootup dirty')

    def test_no_preload_still_boots_an_unknown_machine(self):
        """The seeding is tied to the preload, not to booting - --no-preload is a cold start."""
        console = console_mod.Console(console_mod.parse_args(
            ['--no-preload', '--no-log', '--plain']))
        self.addCleanup(console.close)
        console.boot()
        self.assertIn('?', self._avail_row(console))


class TestMmuLog(unittest.TestCase):
    """
    Happy Hare's own mmu.log, which the harness otherwise writes into a doomed tmpdir.

    These run the console in a SUBPROCESS on purpose. MmuLogger binds to the process-global
    logging.getLogger('mmu') and skips handler creation when one already exists
    (extras/mmu/mmu_logger.py:51), so the FIRST session to boot in a process fixes the log
    path for every later one. One session per process is exactly how the console runs, but
    in this shared test process an in-process assertion would depend on test ordering.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='hh-log-')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _console(self, extra):
        import subprocess
        script = os.path.join(self.dir, 'cmds.txt')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write('MMU_STATUS\n')
        proc = subprocess.run(
            [sys.executable, '-m', 'test.console', '--plain', '--header', 'off',
             '--no-preload', '--script', script] + extra,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stdout[-1500:] + proc.stderr[-1500:])
        return proc.stdout

    def test_the_log_lands_in_log_dir_and_survives_the_session(self):
        out = self._console(['--log-dir', self.dir])
        path = os.path.join(self.dir, 'mmu.log')
        self.assertIn(path, out, 'the banner did not report the log path')
        self.assertTrue(os.path.exists(path), 'no log written')
        with open(path, encoding='utf-8', errors='replace') as fh:
            body = fh.read()
        self.assertIn('Happy Hare', body)

    def test_the_log_is_replaced_each_run(self):
        stale = os.path.join(self.dir, 'mmu.log')
        with open(stale, 'w', encoding='utf-8') as fh:
            fh.write('STALE CONTENT FROM A PREVIOUS RUN\n')
        self._console(['--log-dir', self.dir])
        with open(stale, encoding='utf-8', errors='replace') as fh:
            body = fh.read()
        self.assertNotIn('STALE CONTENT', body, "the previous run's log was kept")
        self.assertIn('Happy Hare', body)

    def test_no_log_writes_nothing_outside_the_temp_dir(self):
        out = self._console(['--no-log', '--log-dir', self.dir])
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'mmu.log')))
        self.assertNotIn('Log: ', out, '--no-log still advertised a log path')

    def test_without_log_dir_the_default_stays_in_the_doomed_tmpdir(self):
        """The harness default must not change - parallel test runs would collide."""
        hh = session('boxturtle')
        self.addCleanup(hh.close)
        self.assertEqual(os.path.dirname(hh.mmu_log), hh.tmpdir)


class TestPinnedHeader(unittest.TestCase):
    """
    The scroll-region header. The property that matters is that ESC[r is always emitted on
    the way out: leaving DECSTBM set shrinks the user's terminal permanently, with nothing
    on screen to explain why.
    """

    def _pin(self):
        console = console_mod.Console(console_mod.parse_args(['--header', 'machine']))
        console.header_lines = lambda: ['line one', 'line two']
        return console_mod.PinnedHeader(console)

    def _capture(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_install_clears_the_screen_before_the_first_render(self):
        pin = self._pin()
        out = self._capture(pin.install)
        self.assertIn('\x1b[2J', out, 'screen not cleared')
        self.assertIn('\x1b[H', out, 'cursor not homed')
        # The region must be reserved during install - after the clear, before the banner -
        # or the very first repaint paints over the banner.
        region = __import__('re').search(r'\x1b\[\d+;\d+r', out)
        self.assertIsNotNone(region, 'no scroll region reserved during install')
        self.assertLess(out.index('\x1b[2J'), region.start(),
                        'region reserved before the screen was cleared')
        self._capture(pin.restore)

    def test_each_header_row_resets_before_erasing(self):
        """ESC[2K erases with the CURRENT attributes, so a leaked colour would fill rows."""
        pin = self._pin()
        out = self._capture(lambda: (pin.install(), pin.repaint()))
        self.assertEqual(out.count('\x1b[0m\x1b[2K'), out.count('\x1b[2K'),
                         'an erase was not preceded by a reset')
        self._capture(pin.restore)

    def test_repaint_sets_a_region_and_homes_each_row(self):
        pin = self._pin()
        out = self._capture(lambda: (pin.install(), pin.repaint()))
        self.assertRegex(out, r'\x1b\[\d+;\d+r')     # DECSTBM
        self.assertIn('\x1b[1;1H', out)              # painted from the top row
        self.assertIn('\x1b[2K', out)                # each row cleared before writing
        self.assertEqual(out.count('\x1b7'), out.count('\x1b8'), 'unbalanced cursor save')
        self._capture(pin.restore)

    def test_restore_always_resets_the_region(self):
        pin = self._pin()
        self._capture(lambda: (pin.install(), pin.repaint()))
        out = self._capture(pin.restore)
        self.assertIn('\x1b[r', out)

    def test_restore_is_idempotent_and_safe_when_never_installed(self):
        pin = self._pin()
        self.assertEqual(self._capture(pin.restore), '', 'reset a region it never set')
        self._capture(lambda: (pin.install(), pin.restore()))
        self.assertEqual(self._capture(pin.restore), '', 'reset twice')

    def test_a_taller_header_re_sets_the_region(self):
        pin = self._pin()
        self._capture(lambda: (pin.install(), pin.repaint()))
        first = pin.height
        pin.console.header_lines = lambda: ['a', 'b', 'c', 'd', 'e']
        out = self._capture(pin.repaint)
        self.assertGreater(pin.height, first)
        self.assertRegex(out, r'\x1b\[\d+;\d+r')
        self._capture(pin.restore)

    def test_pinning_requires_a_tty_a_header_and_no_opt_out(self):
        """
        --script and piped stdout must stay on the inline path or the escape sequences end
        up in the captured output. This mirrors interact()'s guard exactly.
        """
        def would_pin(argv, isatty):
            args = console_mod.parse_args(argv)
            return bool(args.header) and not args.inline_header and isatty

        self.assertTrue(would_pin(['--header', 'machine'], True))
        self.assertFalse(would_pin(['--header', 'machine'], False), 'pinned on a pipe')
        self.assertFalse(would_pin(['--header', 'off'], True), 'pinned with no header')
        self.assertFalse(would_pin(['--header', 'machine', '--inline-header'], True),
                         '--inline-header ignored')


class TestScrollbackTee(unittest.TestCase):
    """
    The buffer behind /scroll.

    It exists because a DECSTBM scroll region is not backed by the terminal's own scrollback:
    rows that scroll out of the region are discarded, so with a pinned header there is
    nothing to scroll back to unless the console keeps the lines itself.
    """

    def _tee(self, maxlen=100):
        import collections
        stream = io.StringIO()
        buf = collections.deque(maxlen=maxlen)
        return console_mod.ScrollbackTee(stream, buf), stream, buf

    def test_every_write_is_forwarded_verbatim(self):
        tee, stream, _ = self._tee()
        tee.write('one\ntwo')
        tee.write('\n')
        self.assertEqual(stream.getvalue(), 'one\ntwo\n')

    def test_a_line_split_across_writes_is_reassembled(self):
        """print() emits the text and the '\\n' as two separate write() calls."""
        tee, _, buf = self._tee()
        tee.write('hello')
        self.assertEqual(list(buf), [], 'committed a line before its newline arrived')
        tee.write('\n')
        self.assertEqual(list(buf), ['hello'])

    def test_a_multi_line_write_is_split_and_the_tail_held(self):
        tee, _, buf = self._tee()
        tee.write('a\nb\nc')
        self.assertEqual(list(buf), ['a', 'b'])
        tee.write('\n')
        self.assertEqual(list(buf), ['a', 'b', 'c'])

    def test_the_buffer_is_bounded(self):
        tee, _, buf = self._tee(maxlen=3)
        tee.write(''.join('%d\n' % i for i in range(10)))
        self.assertEqual(list(buf), ['7', '8', '9'])

    def test_the_tee_forwards_everything_input_needs(self):
        """
        NOT tidiness. input() only takes the readline path when sys.stdout has a real
        fileno(), isatty() and str encoding/errors. Replace __getattr__ with a handful of
        explicit methods and history, completion and the Shift-Up binding vanish - silently,
        only on a real terminal, never in this suite.
        """
        tee, _, _ = self._tee()
        tee.raw_stream = FakeTty()
        self.assertTrue(tee.isatty())
        self.assertEqual(tee.fileno(), 1)
        for name in ('encoding', 'errors', 'flush', 'writable'):
            self.assertTrue(hasattr(tee, name), 'the tee hides stdout.%s' % name)

    def test_control_sequences_bypass_the_buffer(self):
        """
        The whole reason raw_stdout() exists. The pager repaints a full pane per keypress;
        if those frames were captured, the buffer would grow every time it was scrolled.
        """
        console = console_mod.Console(console_mod.parse_args(['--header', 'machine']))
        console.header_lines = lambda: ['line one', 'line two']
        real = FakeTty()
        with mock.patch.object(sys, 'stdout',
                               console_mod.ScrollbackTee(real, console.scrollback)):
            console.tee = sys.stdout
            pin = console_mod.PinnedHeader(console)
            pin.install()
            pin.repaint()
            console.clear_log()
            pin.restore()
        self.assertEqual(list(console.scrollback), [],
                         'cursor control leaked into the scrollback buffer')
        self.assertIn('\x1b[2K', real.getvalue(), 'the escapes did not reach the terminal')

    def test_clear_repairs_the_status_band_not_just_the_log(self):
        """
        /clear is the only way back from a corrupted display, so it has to re-reserve the
        scroll region and repaint the band - not merely erase below it. repaint() on its own
        will not do it: it re-runs _set_region only when the header changes HEIGHT, so a
        band scribbled on by a stray escape sequence would stay scribbled on forever.
        """
        console = console_mod.Console(console_mod.parse_args(['--header', 'machine']))
        console.header_lines = lambda: ['line one', 'line two']
        real = FakeTty()
        with mock.patch.object(sys, 'stdout', real):
            console.pinned = console_mod.PinnedHeader(console).install()
            real.truncate(0), real.seek(0)
            console.clear_log()
            console.pinned.restore()
        out = real.getvalue()
        self.assertIn('\x1b[2J', out, 'the screen was not cleared')
        self.assertRegex(out, r'\x1b\[\d+;\d+r', 'the scroll region was not re-reserved')
        self.assertIn('\x1b[1;1H', out, 'the status band was not repainted')
        self.assertIn('line one', out, 'the header content was not redrawn')
        self.assertIn('\x1b[?7h', out, 'autowrap was not restored')

    def test_simulator_lines_are_marked_and_dimmed(self):
        """
        info() exists so the banner cannot be mistaken for MMU output - it lands directly
        under cmd_MMU_BOOTUP's real text. '#' rather than '!' because '!!' is already this
        console's marker for a command that raised.
        """
        console = console_mod.Console(console_mod.parse_args([]))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            console.color = True
            console.info('first line\nsecond line')
        rendered = buf.getvalue()
        for line in rendered.rstrip('\n').split('\n'):
            self.assertIn(console_mod.INFO_PREFIX, visible(line), line)
            self.assertIn('\x1b[%sm' % console_mod.INFO_COLOUR, line, 'not dimmed: %r' % line)
        self.assertEqual(visible(rendered), '# first line\n# second line\n')
        check_no_line_leaks(self, rendered)

    def test_the_info_colour_survives_being_wrapped_by_the_pager(self):
        """
        Why the mark is grey and not SGR 2. _sgr_state() tracks foreground and bold, so a
        colour is re-opened on each wrapped row; faint would be dropped at the wrap and the
        continuation rows would come back at full brightness.
        """
        line = console_mod.paint(console_mod.INFO_PREFIX + 'a banner line long enough to wrap',
                                 console_mod.INFO_COLOUR, True)
        rows = console_mod.wrap_ansi(line, 12)
        self.assertGreater(len(rows), 1, 'nothing wrapped')
        for row in rows:
            self.assertIn('\x1b[%sm' % console_mod.INFO_COLOUR, row, row)

    def test_redraw_repairs_the_screen_and_keeps_the_log(self):
        """The difference from /clear: the history is repainted, not thrown away."""
        console = console_mod.Console(console_mod.parse_args(['--header', 'machine']))
        console.header_lines = lambda: ['line one', 'line two']
        console.scrollback.extend(['first log line', 'second log line'])
        real = FakeTty()
        with mock.patch.object(sys, 'stdout', real):
            console.pinned = console_mod.PinnedHeader(console).install()
            real.truncate(0), real.seek(0)
            console.redraw()
            console.pinned.restore()
        out = real.getvalue()
        self.assertIn('line one', out, 'the status band was not redrawn')
        self.assertIn('second log line', out, 'the log was not repainted')
        self.assertEqual(len(console.scrollback), 2, '/redraw discarded the history')

    def test_the_tee_is_removed_even_when_the_body_raises(self):
        console = console_mod.Console(console_mod.parse_args([]))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(RuntimeError):
                with console.scrollback_stdout(True):
                    self.assertIsNot(sys.stdout, buf)
                    raise RuntimeError('boom')
            self.assertIs(sys.stdout, buf, 'stdout was left teed')
        self.assertIsNone(console.tee)

    def test_no_tee_is_installed_when_disabled(self):
        console = console_mod.Console(console_mod.parse_args(['--scrollback', '0']))
        self.assertIsNone(console.scrollback)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with console.scrollback_stdout(True) as tee:
                self.assertIsNone(tee)
                self.assertIs(sys.stdout, buf)

    def test_echo_records_what_readline_wrote_but_stdout_never_carried(self):
        console = console_mod.Console(console_mod.parse_args([]))
        with contextlib.redirect_stdout(io.StringIO()):
            with console.scrollback_stdout(True):
                console.echo('mmu[T0 g0]> MMU_STATUS')
        self.assertEqual(list(console.scrollback), ['mmu[T0 g0]> MMU_STATUS'])


class TestAnsiWrap(unittest.TestCase):
    """wrap_ansi() - logical buffer lines into the rows a terminal would show."""

    COLOURED = console_mod.html_to_ansi(
        '<span style="color:#FF69B4">a warning long enough to need wrapping</span>',
        color=True)

    def test_width_counts_printable_columns_not_escape_bytes(self):
        self.assertEqual(len(console_mod.wrap_ansi(self.COLOURED[:0] + 'x' * 10, 10)), 1)
        rows = console_mod.wrap_ansi(console_mod.html_to_ansi('<b>0123456789</b>'), 10)
        self.assertEqual(len(rows), 1, rows)

    def test_a_long_line_splits_at_the_width(self):
        for row in console_mod.wrap_ansi(self.COLOURED, 12):
            self.assertLessEqual(len(visible(row)), 12, repr(row))

    def test_the_visible_text_survives_wrapping(self):
        rows = console_mod.wrap_ansi(self.COLOURED, 7)
        self.assertEqual(''.join(visible(r) for r in rows), visible(self.COLOURED))

    def test_each_row_reopens_the_colour_and_closes_it(self):
        rows = console_mod.wrap_ansi(self.COLOURED, 12)
        self.assertGreater(len(rows), 1, 'nothing was wrapped')
        check_no_line_leaks(self, '\n'.join(rows))
        for row in rows:
            self.assertIn('\x1b[38;5;', row, 'a wrapped row lost the colour: %r' % row)

    def test_an_escape_sequence_is_never_split_across_rows(self):
        for row in console_mod.wrap_ansi(self.COLOURED, 3):
            self.assertNotIn('\x1b', visible(row), 'a sequence was cut in half: %r' % row)

    def test_degenerate_inputs_terminate(self):
        self.assertEqual(console_mod.wrap_ansi('', 10), [''])
        self.assertEqual(len(console_mod.wrap_ansi('abc', 1)), 3)
        self.assertEqual(console_mod.wrap_ansi('abc', 0), ['abc'])


class TestLedSwatches(unittest.TestCase):
    """
    Console._swatches - the LED row, with no printer in sight.

    A lit LED is a solid block rather than '##' because '##' was painted in the LED's own
    colour, and a white or grey LED (mmu_breathing_white_fast, mmu_sparkle, white_light for
    an uncoloured gate) then looked exactly like ordinary text - which is what made a lit
    row read as "some default-coloured thing I do not recognise".
    """

    def swatches(self, data, per_gate, color=False):
        console = console_mod.Console(console_mod.parse_args(['--plain', '--no-log']))
        console.color = color
        return console._swatches(data, per_gate)

    ON, OFF = console_mod.Console.LED_ON, console_mod.Console.LED_OFF

    def test_lit_and_unlit_use_different_glyphs(self):
        got = self.swatches([(1., 0., 0., 0.), (0., 0., 0., 0.)], 1)
        self.assertEqual(got, '%s %s' % (self.ON, self.OFF))

    def test_one_led_per_gate_is_spaced_by_gate(self):
        self.assertEqual(self.swatches([(0., 0.5, 0., 0.)] * 4, 1),
                         ' '.join([self.ON] * 4))

    def test_several_leds_per_gate_run_together_within_the_gate(self):
        """
        ViViD is 28 exit LEDs over 4 gates. Ungrouped that was a 117-column row, and
        PinnedHeader.repaint writes header rows by absolute position without wrapping, so it
        soft-wrapped over the row beneath. Grouped it fits, and you can see the gates.
        """
        got = self.swatches([(0., 0., 1., 0.)] * 28, 7)
        self.assertEqual(got, ' '.join([self.ON * 7] * 4))
        # The width claim, separately: len() is the column count only because this call is
        # unpainted and a block is one column. 59 + the '  led unit1 exit    ' prefix and the
        # '  [gate_status]' suffix is 95, which fits the 100-column band; ungrouped 2-column
        # swatches were 83, i.e. a 117-column row.
        self.assertEqual(len(visible(got)), 59)

    def test_a_whole_segment_in_one_group_has_no_gaps(self):
        """How status and logo arrive - neither is indexed by gate."""
        self.assertEqual(self.swatches([(1., 1., 1., 0.)] * 4, 4), self.ON * 4)

    def test_the_white_channel_counts_as_lit(self):
        """An RGBW chain lit only on W read as off while rgbw[:3] was dropping it."""
        self.assertEqual(self.swatches([(0., 0., 0., 1.)], 1), self.ON)

    def test_a_near_black_led_is_marked_dim_and_painted_visibly(self):
        """
        black_light (0.01,0,0.02) - an idle status segment under filament_color, and any
        black filament - is (3,0,5) of 255, which paints to xterm 16: blacker than the grey
        used for OFF. Lit has to be more visible than unlit, not less.
        """
        got = self.swatches([(0.01, 0., 0.02, 0.)], 1, color=True)
        self.assertIn(console_mod.Console.LED_DIM, got, 'a near-black LED must say it is dim')
        self.assertNotIn('\x1b[38;5;16m', got, 'still painted pure black')

    def test_a_bright_led_is_left_exactly_as_it_is(self):
        """The floor is for the bottom of the range only - it must not touch anything else."""
        self.assertEqual(self.swatches([(0.25, 0.25, 0.25, 0.)], 1, color=True),
                         console_mod.paint(self.ON, console_mod.fg(64, 64, 64, '256')[2:-1]))

    def test_a_lit_led_is_painted_in_its_own_colour(self):
        got = self.swatches([(1., 0., 0., 0.), (0., 0., 0., 0.)], 1, color=True)
        self.assertIn('\x1b[38;5;', got, 'the lit LED lost its colour')
        self.assertIn('\x1b[90m', got, 'the unlit LED should be grey')
        check_no_line_leaks(self, got)

    def test_a_degenerate_group_size_still_terminates(self):
        self.assertEqual(self.swatches([(0., 0., 0., 0.)] * 2, 0), self.OFF + ' ' + self.OFF)


class TestPagerView(unittest.TestCase):
    """LogPager.move() - all of the pager's arithmetic, with no terminal in sight."""

    move = staticmethod(console_mod.LogPager.move)

    def test_the_view_opens_at_the_live_tail(self):
        self.assertEqual(self.move(0, None, 100, 20), 0)

    def test_scrolling_stops_at_both_ends(self):
        self.assertEqual(self.move(0, 'down', 100, 20), 0, 'scrolled past the newest line')
        self.assertEqual(self.move(80, 'up', 100, 20), 80, 'scrolled past the oldest line')

    def test_a_page_is_a_screenful(self):
        self.assertEqual(self.move(0, 'pgup', 100, 20), 20)
        self.assertEqual(self.move(20, 'pgdn', 100, 20), 0)

    def test_home_and_end_go_to_the_extremes(self):
        self.assertEqual(self.move(5, 'home', 100, 20), 80)
        self.assertEqual(self.move(50, 'end', 100, 20), 0)

    def test_a_buffer_shorter_than_the_screen_never_scrolls(self):
        for key in ('up', 'pgup', 'home'):
            self.assertEqual(self.move(0, key, 5, 20), 0, key)

    def test_an_opening_offset_is_clamped(self):
        self.assertEqual(self.move(9999, None, 100, 20), 80)


class TestPagerKeys(unittest.TestCase):
    """
    LogPager._read_key() over a pipe, so no tty is needed.

    Esc is both the prefix of every arrow key and the pager's own quit key, and nothing tells
    them apart except waiting - which is what the timeout is for and why it is tested.
    """

    def setUp(self):
        self.read_fd, self.write_fd = os.pipe()
        self.addCleanup(os.close, self.read_fd)
        self.pager = console_mod.LogPager(
            console_mod.Console(console_mod.parse_args([])))
        self.pager.ESC_WAIT = 0.05

    def _key(self, data):
        os.write(self.write_fd, data)
        stdin = mock.Mock()
        stdin.fileno.return_value = self.read_fd
        with mock.patch.object(sys, 'stdin', stdin):
            return self.pager._read_key()

    def test_the_arrow_keys_are_decoded(self):
        self.assertEqual(self._key(b'\x1b[A'), 'up')
        self.assertEqual(self._key(b'\x1bOB'), 'down')

    def test_a_modified_arrow_falls_back_to_its_final_byte(self):
        """Shift-Up is ESC[1;2A. Anything else that ends in 'A' should still scroll up."""
        self.assertEqual(self._key(b'\x1b[1;2A'), 'up')

    def test_paging_and_jump_keys_are_decoded(self):
        self.assertEqual(self._key(b'\x1b[5~'), 'pgup')
        self.assertEqual(self._key(b'\x1b[6~'), 'pgdn')
        self.assertEqual(self._key(b'\x1b[H'), 'home')
        self.assertEqual(self._key(b'\x1b[F'), 'end')

    def test_the_bare_keys_work_too(self):
        self.assertEqual(self._key(b'k'), 'up')
        self.assertEqual(self._key(b'G'), 'end')
        self.assertEqual(self._key(b'q'), 'quit')
        self.assertEqual(self._key(b'\x03'), 'quit')

    def test_a_bare_escape_quits_rather_than_hanging(self):
        self.assertEqual(self._key(b'\x1b'), 'quit')

    def test_an_unknown_sequence_is_ignored_rather_than_acted_on(self):
        self.assertIsNone(self._key(b'\x1b[99~'))

    def test_an_unknown_plain_key_does_nothing(self):
        self.assertIsNone(self._key(b'z'))


class TestScrollBindings(unittest.TestCase):
    """
    The Shift-Up key binding.

    The two readline dialects are NOT interchangeable and the wrong one is worse than none:
    libedit ignores GNU's '"key": "macro"' form and inserts the tail of the escape sequence
    as literal text, so Shift-Up types ';2A' at the prompt.
    """

    def test_the_libedit_dialect_uses_bind_dash_s(self):
        lines = console_mod.scroll_binding_lines('editline')
        self.assertTrue(all(line.startswith('bind -s ') for line in lines), lines)

    def test_the_gnu_dialect_uses_the_colon_form(self):
        lines = console_mod.scroll_binding_lines('readline')
        self.assertTrue(all(line.startswith('"\\e') for line in lines), lines)
        self.assertNotIn('bind -s', ''.join(lines))

    def test_both_dialects_bracket_the_line_with_ctrl_a_and_ctrl_e(self):
        """\\001 and \\005 are what let a half-typed line survive as trailing arguments."""
        for backend in ('editline', 'readline'):
            for line in console_mod.scroll_binding_lines(backend):
                self.assertIn(r'\001/scroll ', line, backend)
                self.assertIn(r'\005\n', line, backend)

    def test_shift_up_is_bound_but_plain_up_is_not(self):
        """Plain Up must stay command history - that is the point of a modal pager."""
        joined = ''.join(console_mod.scroll_binding_lines('editline'))
        self.assertIn(r'\e[1;2A', joined)
        self.assertNotIn(r'"\e[A"', joined)

    def test_the_backend_falls_back_to_the_docstring(self):
        """readline.backend only exists on 3.13+, and `make console` picks its interpreter."""
        with mock.patch.object(console_mod, 'readline') as fake:
            fake.backend = None
            fake.__doc__ = 'Importing this module enables ... using libedit readline.'
            self.assertEqual(console_mod.readline_backend(), 'editline')
            fake.__doc__ = 'Importing this module enables ... using GNU readline.'
            self.assertEqual(console_mod.readline_backend(), 'readline')

    def test_nothing_is_bound_on_libedit(self):
        """
        Measured, not assumed: libedit delivers only the FIRST character of a macro
        immediately and holds the rest until the next input event, so Shift-Up would leave a
        lone '/' on the line and then corrupt whatever the user typed next. No key beats a
        key that does that.
        """
        console = console_mod.Console(console_mod.parse_args([]))
        with mock.patch.object(console_mod, 'readline') as fake:
            fake.backend = 'editline'
            self.assertFalse(console._install_scroll_bindings())
        fake.parse_and_bind.assert_not_called()

    def test_the_bindings_are_installed_on_gnu_readline(self):
        console = console_mod.Console(console_mod.parse_args([]))
        with mock.patch.object(console_mod, 'readline') as fake:
            fake.backend = 'readline'
            self.assertTrue(console._install_scroll_bindings())
        self.assertTrue(fake.parse_and_bind.called)

    def test_installing_the_bindings_never_raises(self):
        console = console_mod.Console(console_mod.parse_args([]))
        with mock.patch.object(console_mod, 'readline') as fake:
            fake.parse_and_bind.side_effect = RuntimeError('no such dialect')
            fake.backend = 'readline'
            self.assertFalse(console._install_scroll_bindings())


class TestTimestamps(unittest.TestCase):
    """
    /timestamp. The clock is the VIRTUAL one, so these can drive it with advance() rather
    than waiting - which is also the only reason the feature is worth having.
    """

    def _console(self):
        with no_tty():                              # pin the non-interactive defaults
            console = console_mod.Console(console_mod.parse_args(['--plain', '--no-log']))
        console.color = True
        console.wall_start = 1000000.0              # a fixed instant, so the text is stable
        console.clock_epoch = 1000.0
        console.hh = mock.Mock()
        console.hh.reactor.monotonic.return_value = 1000.0
        return console

    def _emit(self, console, text):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            console.emit(text)
        return buf.getvalue()

    def test_off_by_default_and_output_is_untouched(self):
        console = self._console()
        self.assertFalse(console.timestamps)
        self.assertEqual(self._emit(console, 'a\nb'), 'a\nb\n')

    def test_only_the_first_line_is_stamped_and_the_rest_line_up(self):
        console = self._console()
        console.timestamps = True
        rendered = visible(self._emit(console, 'first\nsecond\nthird'))
        head, second, third = rendered.rstrip('\n').split('\n')
        stamp = console.sim_time()
        self.assertTrue(head.startswith(stamp + ' '), head)
        # The indent has to equal the stamp's width or the block reads ragged
        self.assertEqual(second, ' ' * (len(stamp) + 1) + 'second')
        self.assertEqual(third, ' ' * (len(stamp) + 1) + 'third')

    def test_the_stamp_is_dimmed_and_leaks_no_colour(self):
        console = self._console()
        console.timestamps = True
        rendered = self._emit(console, 'a line')
        self.assertIn('\x1b[%sm' % console_mod.TIME_COLOUR, rendered)
        check_no_line_leaks(self, rendered)

    def test_a_blank_continuation_line_is_not_padded(self):
        """Padding an empty line only leaves trailing whitespace behind."""
        console = self._console()
        console.timestamps = True
        rendered = visible(self._emit(console, 'head\n\ntail'))
        self.assertEqual(rendered.rstrip('\n').split('\n')[1], '')

    def test_the_clock_follows_the_virtual_one_not_the_wall(self):
        """/advance an hour and the stamp moves an hour, however long you actually sat there."""
        console = self._console()
        before = console.sim_time()
        console.hh.reactor.monotonic.return_value = 1000.0 + 3600
        after = console.sim_time()
        self.assertNotEqual(before, after, 'the stamp ignored the virtual clock')
        self.assertEqual(
            time.strftime(console_mod.TIME_FORMAT, time.localtime(1000000.0 + 3600)), after)

    def test_the_stamp_is_a_fixed_width(self):
        """Continuation lines are indented by len(stamp), so it must not vary."""
        console = self._console()
        widths = set()
        for offset in (0, 3600, 7200, 3600 * 13):
            console.hh.reactor.monotonic.return_value = 1000.0 + offset
            widths.add(len(console.sim_time()))
        self.assertEqual(len(widths), 1, widths)

    def test_the_meta_command_toggles(self):
        console = self._console()
        with contextlib.redirect_stdout(io.StringIO()):
            console.meta('/timestamp')
            self.assertTrue(console.timestamps)
            console.meta('/timestamp')
            self.assertFalse(console.timestamps)
            console.meta('/timestamp on')
            self.assertTrue(console.timestamps)
            console.meta('/timestamp off')
            self.assertFalse(console.timestamps)


class TestLiveClock(unittest.TestCase):
    """
    Live mode - the clock running while you sit at the prompt.

    A signal and not a thread, and that is not a style choice: the reactor is greenlet-based
    and greenlets belong to the thread that created them, so pumping it from a worker dies
    with 'greenlet.error: Cannot switch to a different thread'. Everything here therefore
    checks main-thread behaviour and the arm/disarm discipline that keeps a tick out of a
    dispatch.
    """

    def _console(self, argv=()):
        with no_tty():                              # pin the non-interactive defaults
            console = console_mod.Console(console_mod.parse_args(list(argv)))
        console.hh = mock.Mock()
        console.hh.reactor.monotonic.return_value = 1000.0
        # Explicitly None: a bare Mock attribute is truthy, and the tick reads _g_dispatch
        # to tell whether the reactor is mid-callback.
        console.hh.reactor._g_dispatch = None
        console.clock_epoch = 1000.0
        console._at_prompt = True                   # where a tick is allowed to run
        return console

    def test_both_default_off_when_not_interactive(self):
        """--script must stay byte-for-byte reproducible; a clock in the output cannot."""
        console = self._console()                   # _console() pins stdout to a pipe
        self.assertFalse(console.live)
        self.assertFalse(console.timestamps)

    def test_the_flags_override_the_default_either_way(self):
        self.assertTrue(self._console(['--live', '--timestamp']).live)
        self.assertTrue(self._console(['--live', '--timestamp']).timestamps)
        self.assertFalse(self._console(['--no-live']).live)
        self.assertFalse(self._console(['--no-timestamp']).timestamps)

    def test_a_real_prompt_turns_both_on(self):
        with mock.patch.object(sys, 'stdout', FakeTty()):
            console = console_mod.Console(console_mod.parse_args([]))
        self.assertTrue(console.live, 'live should default on at a terminal')
        self.assertTrue(console.timestamps)

    def test_script_mode_stays_off_even_on_a_terminal(self):
        with mock.patch.object(sys, 'stdout', FakeTty()):
            console = console_mod.Console(console_mod.parse_args(['--script', 'f.txt']))
        self.assertFalse(console.live)
        self.assertFalse(console.timestamps)

    def test_disarming_is_not_gated_on_the_flag(self):
        """
        _arm_tick(False) has to fire whatever self.live says, or /live off would clear the
        flag and leave the itimer running - a signal every second with nothing to catch it.
        """
        console = self._console(['--no-live'])
        with mock.patch('signal.setitimer') as setitimer:
            console._arm_tick(False)
        setitimer.assert_called_once()
        self.assertEqual(setitimer.call_args[0][1], 0, 'did not cancel the timer')

    def test_a_tick_does_nothing_when_live_is_off(self):
        console = self._console(['--no-live'])
        console.advance = mock.Mock()
        console._tick()
        console.advance.assert_not_called()

    def test_a_tick_delivered_late_does_not_run(self):
        """
        The race the _at_prompt flag exists for. setitimer(0) stops FUTURE signals, but one
        already taken by the C handler is still flagged and Python runs the Python-level
        handler at the next bytecode boundary - by then inside the command that just
        started, where advance() asserts 'called from inside a callback'.
        """
        console = self._console(['--live'])
        console._at_prompt = False                  # what interact() sets before dispatching
        console.advance = mock.Mock()
        console._tick()
        console.advance.assert_not_called()

    def test_a_tick_stays_out_of_a_live_dispatch(self):
        console = self._console(['--live'])
        console.hh.reactor._g_dispatch = object()   # the reactor is inside a callback
        console.advance = mock.Mock()
        console._tick()
        console.advance.assert_not_called()

    def test_a_tick_cannot_re_enter_itself(self):
        """Python runs a pending handler at the next bytecode boundary - including one
        inside the handler."""
        console = self._console(['--live'])
        console._ticking = True
        console.advance = mock.Mock()
        console._tick()
        console.advance.assert_not_called()

    def test_a_tick_advances_by_real_elapsed_time_and_is_capped(self):
        console = self._console(['--live'])
        console.advance = mock.Mock()
        with mock.patch.object(console_mod.time, 'monotonic', return_value=500.0):
            console._last_tick = 499.0
            console._tick()
        self.assertAlmostEqual(console.advance.call_args[0][0], 1.0, places=3)

        console.advance.reset_mock()
        with mock.patch.object(console_mod.time, 'monotonic', return_value=9999.0):
            console._last_tick = 0.0                # a slept laptop, or a SIGSTOP
            console._tick()
        self.assertEqual(console.advance.call_args[0][0], console_mod.LIVE_MAX_CATCHUP)

    def test_a_failing_tick_stops_the_clock_instead_of_repeating(self):
        console = self._console(['--live'])
        console.advance = mock.Mock(side_effect=RuntimeError('reactor went bang'))
        console._reprint = lambda fn: fn()
        with mock.patch('signal.setitimer'), contextlib.redirect_stdout(io.StringIO()) as buf:
            with mock.patch.object(console_mod.time, 'monotonic', return_value=500.0):
                console._last_tick = 499.0
                console._tick()
        self.assertFalse(console.live, 'a broken clock kept ticking')
        self.assertIn('reactor went bang', buf.getvalue())

    def test_the_meta_command_toggles_and_reports(self):
        console = self._console(['--live'])
        with mock.patch('signal.setitimer'), mock.patch('signal.signal'), \
                mock.patch.object(sys, 'stdout', FakeTty()), \
                mock.patch.object(sys, 'stdin', FakeTty()):
            console.meta('/live off')
            self.assertFalse(console.live)
            console.meta('/live')
            self.assertTrue(console.live)
            printed = sys.stdout.getvalue()
        self.assertIn('live clock off', printed)
        self.assertIn('live clock on', printed)

    def test_live_refuses_to_arm_off_a_terminal(self):
        """
        script() has no arm/disarm discipline, so a '/live on' in a command file would leave
        an itimer running and the next tick would land inside a dispatch.
        """
        console = self._console(['--live'])
        # no_tty() has to cover the CALL, not just construction: _arm_tick re-reads both
        # streams every time, which is exactly the discipline under test.
        with no_tty(), mock.patch('signal.setitimer') as setitimer, mock.patch('signal.signal'):
            self.assertFalse(console._arm_tick(True), 'armed a timer with no terminal')
        setitimer.assert_not_called()

    def test_live_says_so_when_it_cannot_arm(self):
        console = self._console(['--no-live'])
        with mock.patch('signal.setitimer'), contextlib.redirect_stdout(io.StringIO()) as buf:
            console.meta('/live on')
        self.assertFalse(console.live)
        self.assertIn('unavailable', buf.getvalue())


class TestSlicedAdvance(unittest.TestCase):
    """
    advance() has a per-call iteration cap and the LED effects animate at 24fps, so a single
    long call dies partway through the seventh virtual minute. Slicing gets there because
    the counter resets per call.
    """

    def _console(self):
        console = console_mod.Console(console_mod.parse_args([]))
        console.hh = mock.Mock()
        return console

    def test_a_short_advance_is_one_call(self):
        console = self._console()
        console.advance(5.0)
        console.hh.reactor.advance.assert_called_once_with(5.0)

    def test_a_long_advance_is_sliced_and_totals_correctly(self):
        console = self._console()
        console.advance(600.0)
        steps = [c[0][0] for c in console.hh.reactor.advance.call_args_list]
        self.assertGreater(len(steps), 1, 'not sliced - this is what used to raise')
        self.assertAlmostEqual(sum(steps), 600.0, places=6)
        self.assertLessEqual(max(steps), console_mod.ADVANCE_SLICE)

    def test_zero_and_negative_do_nothing(self):
        console = self._console()
        console.advance(0.)
        console.advance(-5.)
        console.hh.reactor.advance.assert_not_called()


class TestHeaderGroups(unittest.TestCase):
    """
    header_groups() - shared by --header and /header so the two cannot drift. They used to
    parse the value separately, which is exactly how 'all' ends up working in one and not
    the other.
    """

    GROUPS = console_mod.Console.GROUPS

    def test_all_expands_to_every_group(self):
        self.assertEqual(console_mod.header_groups('all', self.GROUPS), list(self.GROUPS))

    def test_off_and_its_synonyms_are_empty(self):
        for text in ('off', 'none', '', '  ', None):
            self.assertEqual(console_mod.header_groups(text, self.GROUPS), [], repr(text))

    def test_case_does_not_matter(self):
        self.assertEqual(console_mod.header_groups('ALL', self.GROUPS), list(self.GROUPS))
        self.assertEqual(console_mod.header_groups('OFF', self.GROUPS), [])

    def test_a_list_is_kept_in_the_order_given(self):
        self.assertEqual(console_mod.header_groups('leds,machine', self.GROUPS),
                         ['leds', 'machine'])

    def test_an_unknown_group_is_rejected_and_says_so(self):
        with self.assertRaises(ValueError) as caught:
            console_mod.header_groups('machine,bogus', self.GROUPS)
        self.assertIn('bogus', str(caught.exception))
        self.assertIn('all/off', str(caught.exception))

    def test_the_flag_accepts_all_too(self):
        self.assertEqual(console_mod.parse_args(['--header', 'all']).header,
                         list(self.GROUPS))

    def test_the_meta_command_accepts_all(self):
        console = console_mod.Console(console_mod.parse_args(['--header', 'machine']))
        with contextlib.redirect_stdout(io.StringIO()):
            console.meta('/header all')
            self.assertEqual(console.args.header, list(self.GROUPS))
            console.meta('/header off')
            self.assertEqual(console.args.header, [])


class TestScrollArguments(unittest.TestCase):
    """parse_scroll_args() and the history tidy-up that follows the key macro."""

    def test_a_leading_integer_is_an_offset(self):
        self.assertEqual(console_mod.parse_scroll_args('50'), (50, ''))
        self.assertEqual(console_mod.parse_scroll_args(''), (0, ''))

    def test_the_rest_is_the_recovered_half_typed_line(self):
        self.assertEqual(console_mod.parse_scroll_args('MMU_SELECT GATE=1'),
                         (0, 'MMU_SELECT GATE=1'))
        self.assertEqual(console_mod.parse_scroll_args('50 MMU_SELECT'), (50, 'MMU_SELECT'))

    def test_the_scroll_line_is_dropped_and_the_typing_pushed_back(self):
        with mock.patch.object(console_mod, 'readline') as fake:
            fake.get_current_history_length.return_value = 4
            fake.get_history_item.return_value = '/scroll MMU_ST'
            console_mod.Console._recover_typed('MMU_ST')
        fake.remove_history_item.assert_called_once_with(3)
        fake.add_history.assert_called_once_with('MMU_ST')

    def test_a_hand_typed_scroll_leaves_an_unrelated_history_entry_alone(self):
        with mock.patch.object(console_mod, 'readline') as fake:
            fake.get_current_history_length.return_value = 4
            fake.get_history_item.return_value = 'MMU_STATUS'
            console_mod.Console._recover_typed('')
        fake.remove_history_item.assert_not_called()
        fake.add_history.assert_not_called()


class TestConsoleScript(unittest.TestCase):
    """
    End to end through main(), the same path the prompt uses.

    PINNED TO BOXTURTLE on purpose. These tests are about console MECHANICS - header groups,
    /sensor, /log - and they name specific sensors (mmu_entry_0) and issue unit-less commands
    (MMU_HOME). Both are properties of the machine, not of the console, so following whatever
    --profile happens to be the default made them fail the moment the default became a
    multi-unit ERCF+ViViD: it has no per-gate entry sensors on unit0, and MMU_HOME there
    requires a UNIT. The default profile gets its own coverage below instead.

    ALSO PINNED TO --pace 0, now purely because unpaced is faster. It used to be a finding:
    BoxTurtle and EMU are the two shipped profiles with per-gate entry sensors AND
    gate_autoload, and paced they PRELOADED TWICE:

        > MMU_PRELOAD GATE=1
        Preloading filament in gate 1...
        Preloading...
        Preloading filament in current gate...     <- nobody asked for this one
        Preloading...

    A preload crosses the entry sensor, so it raises an insert event, and with gate_autoload
    set HH answered by starting another preload. Unpaced it never surfaced, because the entry
    sensor's event_delay defers the insert by 0.5s and no virtual time ever passed. Paced, the
    deferred event landed mid-operation, so an MMU_LOAD (which preloads first when the gate is
    not already available) picked it up during the bowden move and logged "Operation not
    possible. Filament is loaded" - by then the machine WAS loaded.

    Fixed in __MMU_SENSOR_INSERT: an entry insert only means "the user pushed filament in"
    when mmu.action is ACTION_IDLE. During a preload, load or scan the MMU is driving filament
    across that sensor itself, so the event is logged and ignored. Both symptoms are gone
    under --pace 0.5; the pin stays for speed.
    """

    PROFILE = 'boxturtle'

    def _run(self, lines, extra_args=()):
        fd, path = tempfile.mkstemp(suffix='.txt')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        self.addCleanup(os.unlink, path)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # --profile first so a caller can still override it in extra_args (argparse keeps
            # the last occurrence)
            # --pace 0: this class is about console MECHANICS on boxturtle, and boxturtle is
            # one of the two shipped profiles where a PACED load raises a spurious insert event
            # mid-operation (see the note on TestConsoleScript). Unpaced is also faster.
            rc = console_mod.main(['--profile', self.PROFILE, '--plain', '--pace', '0',
                                   '--script', path] + list(extra_args))
        return rc, buf.getvalue()

    def _make_console(self, argv):
        """A booted Console on THIS class's profile, closed on teardown."""
        console = console_mod.Console(
            console_mod.parse_args(['--profile', self.PROFILE, '--pace', '0'] + list(argv)))
        self.addCleanup(console.close)
        console.boot()
        return console

    def test_a_clean_session_runs_every_command_and_exits_zero(self):
        rc, out = self._run(['MMU_STATUS', 'MMU_HOME', 'MMU_CHANGE_TOOL TOOL=1',
                             '/advance 12'])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertIn('Ready', out)                  # the bootup welcome
        self.assertIn('Happy Hare', out)             # MMU_STATUS banner
        self.assertIn('LOADED IN NOZZLE', out)       # the tool change really loaded
        self.assertNotIn('!!', out)

    def test_the_log_path_is_reported_and_tailable(self):
        root = tempfile.mkdtemp(prefix='hh-scriptlog-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        rc, out = self._run(['MMU_STATUS', '/log 3'],
                            ['--header', 'off', '--log-dir', root])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertIn(os.path.join(root, 'mmu.log'), out)
        self.assertIn('| ', out, '/log printed no tail')

    def test_the_header_reports_state_and_follows_the_clock(self):
        rc, out = self._run(['/advance 5'], ['--header', 'machine'])
        self.assertEqual(rc, 0, out)
        self.assertIn('t=+', out)
        # /advance must move the virtual clock, which is otherwise frozen at the prompt
        stamps = [float(s) for s in __import__('re').findall(r't=\+([0-9.]+)s', out)]
        self.assertTrue(stamps and max(stamps) >= 5.0, stamps)

    def test_every_header_group_renders(self):
        rc, out = self._run(['MMU_STATUS'],
                            ['--header', ','.join(console_mod.Console.GROUPS)])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertIn('gate 0', out)                 # filament group
        self.assertIn('mmu_entry_0=', out)           # sensors group

    def test_sensor_rows_have_no_v3_alias_duplicates(self):
        """
        status['sensors'] injects v3 aliases (mmu_pre_gate IS mmu_entry, mmu_gear IS
        mmu_exit, mmu_gate IS mmu_shared_exit), so iterating it shows phantom rows. The
        header must use get_sensor_states(all_sensors=True) instead.
        """
        rc, out = self._run(['MMU_STATUS'], ['--header', 'sensors'])
        self.assertEqual(rc, 0, out[-2000:])
        for phantom in ('mmu_pre_gate=', 'mmu_gear='):
            self.assertNotIn(phantom, out)

    def test_an_unknown_command_is_reported_rather_than_swallowed(self):
        rc, out = self._run(['NOT_A_REAL_COMMAND'], ['--header', 'off'])
        self.assertEqual(rc, 0, out)
        self.assertIn('no such command', out)

    def test_a_bad_parameter_does_not_end_the_session(self):
        rc, out = self._run(['MMU_TEST_MOVE MOVE=abc', 'MMU_STATUS'], ['--header', 'off'])
        self.assertIn('Unable to parse', out)
        self.assertIn('Happy Hare', out)             # the next command still ran
        self.assertEqual(rc, 1, 'a session that reported !! should exit non-zero')

    def test_a_bare_T_command_explains_that_macro_bodies_do_not_run(self):
        """T1 is registered, so it is not 'unknown' - it just silently does nothing."""
        rc, out = self._run(['T1'], ['--header', 'off'])
        self.assertEqual(rc, 0, out)
        self.assertIn('does not run macro bodies', out)
        self.assertIn('MMU_CHANGE_TOOL TOOL=1', out)

    def test_mmu_help_works(self):
        """
        MMU_HELP enumerates gcode.ready_gcode_handlers (mmu_help.py:155). The fake dispatch
        only had base_commands, so this used to fail with
        "'GCodeDispatch' object has no attribute 'ready_gcode_handlers'".
        """
        rc, out = self._run(['MMU_HELP'], ['--header', 'off'])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertNotIn('ready_gcode_handlers', out)
        self.assertIn('Happy Hare MMU commands', out)
        self.assertIn('MMU_CHANGE_TOOL', out)

    def test_vars_reports_both_status_objects(self):
        rc, out = self._run(['/vars'], ['--header', 'off'])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertIn('[mmu] live', out)
        self.assertIn('[mmu_machine]', out)
        self.assertIn('filament_pos', out)           # from mmu
        self.assertIn('num_units', out)              # from mmu_machine
        self.assertIn('selector_type', out)          # from its per-unit sub-dict

    def test_vars_can_select_one_object(self):
        rc, out = self._run(['/vars machine'], ['--header', 'off'])
        self.assertEqual(rc, 0, out)
        self.assertIn('[mmu_machine]', out)
        self.assertNotIn('[mmu] live', out)
        rc, out = self._run(['/vars mmu'], ['--header', 'off'])
        self.assertIn('[mmu] live', out)
        self.assertNotIn('[mmu_machine]', out)

    def test_vars_file_shows_the_sessions_own_mmu_vars_cfg(self):
        """
        The saved-variables file is real and per-session. Showing it is the answer to "does
        the harness even have an mmu_vars.cfg?" - it does, in a tempdir, discarded on exit.
        """
        rc, out = self._run(['/vars file'], ['--header', 'off'])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertIn('mmu_vars.cfg', out)
        self.assertIn('[Variables]', out)
        self.assertIn('mmu__revision', out)

    def test_a_default_boot_does_not_warn_that_calibration_is_incomplete(self):
        """
        Calibration is seeded INSIDE boot(), before __MMU_BOOTUP runs. Seeding afterwards
        (which is what this used to do) left the banner warning about a machine that was
        calibrated a millisecond later.
        """
        rc, out = self._run(['MMU_STATUS'], ['--header', 'off'])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertNotIn('Calibration steps are not complete', out)
        self.assertNotIn('not found in mmu_vars.cfg', out)

    def test_no_calibrate_boots_cold_so_the_warnings_are_real(self):
        """The counterpart: --no-calibrate is how you drive MMU_CALIBRATE_* for real."""
        rc, out = self._run(['MMU_STATUS'], ['--header', 'off', '--no-calibrate'])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertIn('Calibration steps are not complete', out)

    def test_state_was_renamed_to_vars(self):
        rc, out = self._run(['/state'], ['--header', 'off'])
        self.assertIn('unknown meta-command /state', out)

    def test_sensor_can_be_disabled_so_happy_hare_ignores_it(self):
        rc, out = self._run(['/sensor mmu_entry_1 disable', '/sensor mmu_entry_1 enable'],
                            ['--header', 'off'])
        self.assertEqual(rc, 0, out[-1500:])
        self.assertIn('mmu_entry_1 disabled', out)
        self.assertIn('mmu_entry_1 enabled', out)

    def test_gate_map_empty_removes_filament_from_the_gate_sensors(self):
        console = self._make_console(['--no-log', '--plain', '--header', 'off'])
        hh = console.hh
        # Put this gate through every fitted sensor; other gates remain merely parked.
        hh.place_filament(0, position=800.)
        affected = [name for name in console.fil.sensor_names()
                    if console.fil.gate_of(name) in (None, 0)]
        self.assertTrue(affected, 'profile has no modelled sensors for gate 0')
        self.assertTrue(all(hh.sensor(name).present for name in affected),
                        'precondition: filament did not cover every sensor')

        with contextlib.redirect_stdout(io.StringIO()):
            console.run_command('MMU_GATE_MAP GATE=0 AVAILABLE=0 QUIET=1')

        self.assertEqual(hh.mmu.gate_maps.gate_status[0], 0)
        self.assertTrue(all(not hh.sensor(name).present for name in affected),
                        'EMPTY left one or more gate-path sensors triggered')

    def test_gate_map_reset_restores_the_primed_filament_defaults(self):
        console = self._make_console(['--no-log', '--plain', '--header', 'off'])
        hh = console.hh
        maps = hh.mmu.gate_maps
        attrs = ('gate_filament_name', 'gate_material', 'gate_vendor', 'gate_color',
                 'gate_temperature', 'gate_speed_override', 'gate_spool_id',
                 'gate_spool_rfid')
        initial = {attr: getattr(maps, attr)[0] for attr in attrs}

        with contextlib.redirect_stdout(io.StringIO()):
            console.run_command('MMU_GATE_MAP GATE=0 AVAILABLE=0 QUIET=1')
            console.run_command('MMU_GATE_MAP GATE=0 RESET=1 QUIET=1')

        self.assertEqual({attr: getattr(maps, attr)[0] for attr in attrs}, initial)
        self.assertEqual(maps.gate_status[0], 0,
                         'RESET must not recreate filament removed from the simulator')

    def test_gate_map_available_parks_filament_through_the_entry_sensor(self):
        console = self._make_console(['--no-preload', '--no-log', '--plain',
                                      '--header', 'off'])
        hh = console.hh

        for available in (1, 2):
            with self.subTest(available=available):
                # Force a real status transition for both availability values.
                with contextlib.redirect_stdout(io.StringIO()):
                    console.run_command('MMU_GATE_MAP GATE=0 AVAILABLE=0 QUIET=1')
                    console.run_command(
                        'MMU_GATE_MAP GATE=0 AVAILABLE=%d QUIET=1' % available)
                self.assertEqual(hh.mmu.gate_maps.gate_status[0], available)
                self.assertTrue(hh.sensor('mmu_entry_0').present)
                self.assertFalse(hh.sensor('mmu_exit_0').present)

    def test_gate_map_unknown_does_not_move_filament_or_change_sensors(self):
        console = self._make_console(['--no-preload', '--no-log', '--plain',
                                      '--header', 'off'])
        hh = console.hh
        hh.place_filament(0, position=20.)
        before_tip = console.fil.tip[0]
        before_sensors = {name: hh.sensor(name).present for name in hh.sensors()}

        with contextlib.redirect_stdout(io.StringIO()):
            console.run_command('MMU_GATE_MAP GATE=0 AVAILABLE=-1 QUIET=1')

        self.assertEqual(hh.mmu.gate_maps.gate_status[0], -1)
        self.assertEqual(console.fil.tip[0], before_tip)
        self.assertEqual({name: hh.sensor(name).present for name in hh.sensors()},
                         before_sensors)

    def test_bulk_gate_map_status_update_does_not_move_filament(self):
        """Moonraker/UI MAP callbacks describe state; only AVAILABLE is a console action."""
        console = self._make_console(['--no-preload', '--no-log', '--plain',
                                      '--header', 'off'])
        hh = console.hh
        hh.place_filament(0, position=20.)
        before_tip = console.fil.tip[0]
        before_sensors = {name: hh.sensor(name).present for name in hh.sensors()}

        with contextlib.redirect_stdout(io.StringIO()):
            console.run_command(
                'MMU_GATE_MAP MAP="{0: {\'status\': 0, \'spool_id\': 5}}" '
                'FROM_SPOOLMAN=1 QUIET=1')

        self.assertEqual(hh.mmu.gate_maps.gate_status[0], 0,
                         'precondition: bulk callback did not change map status')
        self.assertEqual(console.fil.tip[0], before_tip)
        self.assertEqual({name: hh.sensor(name).present for name in hh.sensors()},
                         before_sensors)

    def test_a_disabled_sensor_reads_as_the_third_state(self):
        """Disabled is None, distinct from clear - the header must show it differently."""
        console = self._make_console(['--no-preload', '--no-log', '--plain', '--header', 'sensors'])
        # /sensor echoes the new state on stdout (console.py _meta_sensor), so the
        # meta() calls must be captured - a bare one leaks '  mmu_entry_1 disabled'
        # into the test runner's output. header_lines() returns rather than prints,
        # so the assertions stay outside the capture and keep their diagnostics.
        def quiet_meta(cmd):
            with contextlib.redirect_stdout(io.StringIO()):
                console.meta(cmd)

        self.assertIn('mmu_entry_1=0', '\n'.join(console.header_lines()))
        quiet_meta('/sensor mmu_entry_1 disable')
        self.assertIn('mmu_entry_1=-', '\n'.join(console.header_lines()))
        quiet_meta('/sensor mmu_entry_1 enable')
        self.assertIn('mmu_entry_1=0', '\n'.join(console.header_lines()))

    def test_sensor_rejects_a_bad_action(self):
        rc, out = self._run(['/sensor mmu_entry_1 sideways'], ['--header', 'off'])
        self.assertIn('unknown action', out)

    def test_clear_empties_the_log_history(self):
        console = self._make_console(['--no-preload', '--no-log', '--plain', '--header', 'off'])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            console.run_command('MMU_STATUS')
            console.scrollback.append('something to scroll back to')
            self.assertTrue(console.sink, 'nothing to clear')
            console.meta('/clear')
        self.assertEqual(console.sink, [], '/clear did not empty the log')
        # sink is raw MMU messages, scrollback is what the terminal showed. Two objects,
        # two purposes, and /clear has to empty both or the pager still holds the old log.
        self.assertEqual(list(console.scrollback), [], '/clear left the scrollback behind')

    def test_the_status_section_ends_in_a_heavy_rule(self):
        """The boundary between state and output has to be visually distinct from the top."""
        console = self._make_console(['--no-preload', '--no-log', '--header', 'machine'])
        block = console.header_block()
        self.assertIn('━', block[-1], 'no heavy rule closing the status section')
        self.assertNotIn('━', console.rule(), 'the light rule should not be heavy')
        self.assertIn('─', console.rule())

    def test_meta_commands_do_not_crash(self):
        rc, out = self._run(['/help', '/filament', '/exhaust 0', '/trace 2', '/heat 240',
                             '/sensor mmu_entry_1 off', '/sensor mmu_exit_0 disable',
                             '/vars', '/clear', '/redraw', '/log 3', '/errors', '/scroll',
                             '/scroll 5', '/s', '/timestamp', '/timestamp off',
                             '/live on', '/live off', '/advance 600', '/badmeta'],
                            ['--header', 'off'])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertIn('Meta-commands', out)
        self.assertIn('unknown meta-command', out)

    def test_an_install_directory_works_as_a_profile(self):
        root = tempfile.mkdtemp(prefix='hh-consoledir-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        make_install_tree(root)
        rc, out = self._run(['MMU_STATUS'], ['--profile', root, '--header', 'off'])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertIn('Happy Hare', out)


class TestTheDefaultProfile(unittest.TestCase):
    """
    Whatever `make console` boots with NO arguments, which is the way almost everyone runs it.

    Deliberately does not name the profile: the point is that the DEFAULT works, so this keeps
    holding if the default changes again. TestConsoleScript pins boxturtle precisely so that
    this class is the only one asserting on the default.
    """

    def setUp(self):
        # No --profile: take whatever the default is
        self.console = console_mod.Console(
            console_mod.parse_args(['--plain', '--no-log', '--header', 'off']))
        self.addCleanup(self.console.close)
        self.console.boot()

    def test_it_boots_cleanly(self):
        self.assertTrue(self.console.hh.fired('mmu:bootup'))
        self.assertEqual(self.console.hh.errors, [])

    def test_it_moves_filament_on_the_last_gate(self):
        """
        The LAST gate, so a multi-unit default is exercised on its final unit - the case that
        needs contiguous gate numbering, per-unit selector homing and per-unit calibration all
        to be right at once. On a single-unit default it is simply the last gate.

        A load and unload rather than a bare boot, because "the default boots" was never the
        weak claim; "the default can do the thing the console exists for" was. boot() has
        already preloaded every gate and prepared the selectors.
        """
        hh = self.console.hh
        last = hh.mmu.num_gates - 1
        hh.errors.clear()
        for command in ('MMU_SELECT GATE=%d' % last, 'MMU_LOAD', 'MMU_UNLOAD'):
            self.console._dispatch(command)
            self.assertEqual(hh.errors, [], 'failed on %r' % command)
        self.assertEqual(hh.mmu.filament_pos, 0, 'did not end up unloaded')

    def test_every_gate_starts_with_plausible_filament(self):
        """
        A fresh machine reports "Unknown | 200C | Unknown" on every gate, which makes anything
        that presents filament attributes - the gate table, the LED filament_color effect, the
        Spoolman paths - impossible to eyeball. Priming gives each gate real-looking metadata.
        """
        hh = self.console.hh
        maps = hh.mmu.gate_maps
        num_gates = hh.mmu.num_gates
        low, high = hh.FILAMENT_TEMP_RANGE
        self.assertEqual(len(self.console.hh.primed), num_gates)
        for gate in range(num_gates):
            with self.subTest(gate=gate):
                self.assertIn(maps.gate_vendor[gate], hh.FILAMENT_VENDORS)
                self.assertIn(maps.gate_material[gate], hh.FILAMENT_MATERIALS)
                self.assertTrue(maps.gate_color[gate], 'no colour on gate %d' % gate)
                self.assertTrue(low <= maps.gate_temperature[gate] <= high,
                                maps.gate_temperature[gate])
                # The name is a product name and HH renders it next to the vendor, so it
                # must not repeat it ("Prusa | Prusa PLA")
                self.assertNotIn(maps.gate_vendor[gate], maps.gate_filament_name[gate])

    def test_the_bootup_banner_shows_a_homed_machine_with_a_gate_selected(self):
        """
        __MMU_BOOTUP renders the selector row and the filament row, so homing after boot()
        returned left the banner reporting 'Selct: XXXX' and tool 'T?' about a machine a later
        MMU_STATUS described as homed with a gate selected.

        The two rows are read out of the banner rather than off the objects, because the
        complaint was about what the banner SAYS.
        """
        joined = '\n'.join(self.console.startup_output)
        selct = next(l for l in joined.split('\n') if l.startswith('Selct:'))
        self.assertNotIn('X', selct, 'the selector reads as unhomed at bootup')
        self.assertNotIn('[T?]', joined, 'bootup rendered an unknown tool')

    def test_the_homing_chatter_stays_out_of_the_banner(self):
        """
        Homing has to precede bootup, but "Homing MMU unit0... / Homed" is setup rather than
        bootup output - and startup_output prints under the welcome, so leaving it in put three
        lines of it above the rabbit.
        """
        joined = '\n'.join(self.console.startup_output)
        self.assertNotIn('Homing MMU', joined)
        self.assertIn('Ready', joined, 'the welcome should still be there')

    def test_the_bootup_banner_already_shows_the_primed_map(self):
        """
        __MMU_BOOTUP prints the gate/filament table, so priming has to happen BEFORE it.
        Priming afterwards left the banner showing "Unknown" on every gate while a later
        MMU_STATUS showed the real thing - the same class of bug as the stale calibration
        warnings, and fixed the same way.
        """
        joined = ' '.join(self.console.startup_output)
        self.assertIn('Ready', joined, 'precondition: this is the bootup output')
        self.assertNotIn('Unknown', joined)

    def test_priming_is_reproducible_and_varied(self):
        """
        Seeded on purpose: the point is data that LOOKS real, not data that changes every run.
        Varied across gates, identical across runs with the same seed.
        """
        first = self.console.hh.prime_gate_map(seed=7)
        second = self.console.hh.prime_gate_map(seed=7)
        self.assertEqual(first, second, 'same seed produced different data')
        self.assertGreater(len({(a['vendor'], a['material']) for a in first.values()}), 1,
                           'every gate got the same filament')

    def test_pacing_makes_an_operation_take_virtual_time(self):
        """
        At pace 0 a whole MMU_LOAD completes without the clock moving, so nothing time-driven
        is observable - the LED effect never reaches a second frame and every action transition
        lands in the same instant. Pacing spends each move's real duration in virtual time.
        """
        hh = self.console.hh
        self.assertEqual(hh.pacing_wall, 0., 'the suite must never sleep')
        hh.set_pacing(0.)                           # the console default is 0.5; measure from 0
        gate = hh.mmu.num_gates - 1
        self.console._dispatch('MMU_SELECT GATE=%d' % gate)

        before = hh.reactor.monotonic()
        self.console._dispatch('MMU_LOAD')
        instant = hh.reactor.monotonic() - before
        self.console._dispatch('MMU_UNLOAD')

        hh.set_pacing(1.)
        self.addCleanup(hh.set_pacing, 0.)
        before = hh.reactor.monotonic()
        self.console._dispatch('MMU_LOAD')
        paced = hh.reactor.monotonic() - before

        self.assertAlmostEqual(instant, 0., places=3, msg='pace 0 should not move the clock')
        self.assertGreater(paced, 1., 'pacing did not spend virtual time')

        # AND THE OPERATION MUST STILL HAVE WORKED. Pacing runs timers between moves, which is
        # the same hazard reactor-level dispatch hit: it left every gate GATE_EMPTY instead of
        # GATE_AVAILABLE, silently, with no error (see Console._dispatch). So assert the
        # OUTCOME - an empty error list was measurably not enough to catch that.
        from extras.mmu.mmu_constants import FILAMENT_POS_LOADED, FILAMENT_POS_UNLOADED
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.console._dispatch('MMU_UNLOAD')
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertNotIn(0, hh.mmu.gate_maps.gate_status, 'a gate went EMPTY during a paced run')
        self.assertEqual(hh.errors, [])

    def test_output_is_stamped_when_happy_hare_said_it(self):
        """
        _drain() runs AFTER a command returns, so stamping there gave every line of an
        operation the same reading - the clock as it stood at the END. Under /pace a load
        reported eight identical stamps while the clock had moved 11 seconds, which hid the
        very progression /timestamp exists to show.
        """
        hh = self.console.hh
        hh.set_pacing(1.)
        self.addCleanup(hh.set_pacing, 0.)
        gate = hh.mmu.num_gates - 1
        self.console._dispatch('MMU_SELECT GATE=%d' % gate)

        mark = len(self.console.sink)
        self.console._dispatch('MMU_LOAD')
        stamps = self.console.sink_stamp[mark:]
        self.assertGreater(len(stamps), 2, 'expected several lines from a load')
        self.assertGreater(len(set(stamps)), 1,
                           'every line of a paced load carried the same timestamp')
        self.assertEqual(stamps, sorted(stamps), 'stamps must not go backwards')
        self.assertEqual(len(self.console.sink), len(self.console.sink_stamp),
                         'the two lists are indexed in lockstep')

    def test_output_is_printed_while_the_command_is_still_running(self):
        """
        Streaming, not buffering. Output used to be held until the command returned, so a paced
        load printed a dozen correct-looking timestamps all in one burst - the timings said the
        operation took 11 seconds and the screen said it took none.

        Asserting on WHEN emit() is called, by recording the clock at each call: more than one
        distinct reading is only possible if lines went out mid-command.
        """
        hh = self.console.hh
        hh.set_pacing(1.)
        self.addCleanup(hh.set_pacing, 0.)
        gate = hh.mmu.num_gates - 1
        self.console._dispatch('MMU_SELECT GATE=%d' % gate)

        clocks = []
        original = self.console.emit
        self.console.emit = lambda text, stamp=None: clocks.append(hh.reactor.monotonic())
        self.addCleanup(setattr, self.console, 'emit', original)

        self.console.run_command('MMU_LOAD')
        self.assertGreater(len(clocks), 2, 'expected several lines from a load')
        self.assertGreater(len(set(clocks)), 1,
                           'every line was printed at the same instant - still buffering')

    def test_clearing_the_sink_clears_the_stamps_with_it(self):
        """Out of step and _drain() would stamp lines with another line's clock."""
        self.assertEqual(len(self.console.sink), len(self.console.sink_stamp))
        self.console._dispatch('MMU_STATUS')
        self.assertEqual(len(self.console.sink), len(self.console.sink_stamp))
        self.console._clear_sink()
        self.assertEqual(self.console.sink, [])
        self.assertEqual(self.console.sink_stamp, [])

    def test_the_header_reports_pacing_only_when_it_is_on(self):
        """
        Next to the clock, because that is the field it explains: with pacing on, t= moves
        during an operation. Absent at 0 - the default means "instant", which is the absence
        of a mode rather than a mode, and a permanent realtime=0% would be noise.
        """
        hh = self.console.hh
        self.console.args.header = ['machine']
        self.addCleanup(hh.set_pacing, hh.pacing)

        hh.set_pacing(0.)
        self.assertNotIn('realtime', '\n'.join(self.console.header_lines()))
        hh.set_pacing(0.5)
        self.assertIn('realtime=50%', '\n'.join(self.console.header_lines()))
        hh.set_pacing(1.)
        self.assertIn('realtime=100%', '\n'.join(self.console.header_lines()))
        hh.set_pacing(0.)
        self.assertNotIn('realtime', '\n'.join(self.console.header_lines()))

    def test_a_paced_preload_still_marks_the_gate_available(self):
        """
        The specific silent failure reactor dispatch caused. Preload is the most timer-sensitive
        path in the harness - HH's own tail concludes the gate is not loaded if the entry sensor
        is still triggered afterwards - so it is the one to pin.
        """
        hh = self.console.hh
        hh.set_pacing(1.)
        self.addCleanup(hh.set_pacing, 0.)
        hh.place_filament(0, position=console_mod.TIP_AT_GATE)
        self.console._dispatch('MMU_PRELOAD GATE=0')
        self.assertNotEqual(hh.mmu.gate_maps.gate_status[0], 0,
                            'paced preload left the gate EMPTY')
        self.assertEqual(hh.errors, [])

    def test_a_paced_move_is_walked_rather_than_jumped(self):
        """
        A single long move used to do one advance() and one sleep(), so it FROZE for the whole
        of it - no LED frames, no repaint, no intermediate position. A load looked like three
        blocks with nothing happening in between.

        Each move is now walked in slices, the model and the clock advancing together, so the
        filament genuinely travels and there are hundreds of repaint opportunities.
        """
        hh = self.console.hh
        gate = hh.mmu.num_gates - 1
        self.console._dispatch('MMU_SELECT GATE=%d' % gate)

        seen = []
        hh.printer.harness_pace_observer = lambda: seen.append(round(self.console.fil.tip[gate], 2))
        self.addCleanup(setattr, hh.printer, 'harness_pace_observer', None)
        hh.set_pacing(1.)
        self.addCleanup(hh.set_pacing, 0.)
        self.console._dispatch('MMU_LOAD')

        self.assertGreater(len(seen), 50, 'a whole load produced only a handful of updates')
        self.assertGreater(len(set(seen)), 50, 'the filament did not move between updates')
        self.assertEqual(seen, sorted(seen), 'a load should only ever feed filament forwards')

    def test_pacing_does_not_change_where_the_filament_ends_up(self):
        """Slicing a move must be exact - the totals cannot drift from the unpaced answer."""
        hh = self.console.hh
        gate = hh.mmu.num_gates - 1
        self.console._dispatch('MMU_SELECT GATE=%d' % gate)
        self.console._dispatch('MMU_LOAD')
        unpaced = round(self.console.fil.tip[gate], 4)
        self.console._dispatch('MMU_UNLOAD')

        hh.set_pacing(1.)
        self.addCleanup(hh.set_pacing, 0.)
        self.console._dispatch('MMU_LOAD')
        self.assertAlmostEqual(round(self.console.fil.tip[gate], 4), unpaced, places=3)
        self.assertEqual(hh.errors, [])

    def test_pacing_is_skipped_inside_a_reactor_callback(self):
        """
        advance() asserts if it re-enters a callback, so the pacer must no-op there. Only
        top-level dispatch - the console, and the tests - is paced.
        """
        hh = self.console.hh
        hh.set_pacing(1.)
        self.addCleanup(hh.set_pacing, 0.)
        mq = hh.printer.lookup_object('motion_queuing')
        self.assertIsNotNone(mq._pace_factor(), 'precondition: pacing is on at top level')
        inside = []
        hh.reactor.register_callback(lambda et: inside.append(mq._pace_factor()))
        hh.reactor.advance(0.)
        self.assertEqual(inside, [None], 'a move inside a callback must not be paced')

    def test_a_spool_lookup_resolves_instead_of_timing_out(self):
        """
        The fake Moonraker holds a REAL MmuServer, seeded to agree with the primed gate map.
        Without it a UID lookup is dispatched and nothing ever answers, so an NFC read ends in
        "Automatic assignment of id timed out" 20 seconds later.
        """
        hh = self.console.hh
        self.assertIsNotNone(hh.moonraker_link, 'Moonraker should be attached by default')
        before = len(hh.console)
        # run_command prints; keep it out of the runner's output
        with contextlib.redirect_stdout(io.StringIO()):
            self.console.run_command('_MMU_TEST NFC_READ=1 DEEP=1 UID=BADCAFE03')
        said = ' '.join(hh.console[before:])
        self.assertIn('Spool ID', said)
        self.assertNotIn('timed out', said)

    def test_no_moonraker_leaves_calls_unanswered(self):
        """The counterpart - what a printer with Moonraker down looks like."""
        console = console_mod.Console(console_mod.parse_args(
            ['--plain', '--no-log', '--header', 'off', '--no-preload', '--no-moonraker']))
        self.addCleanup(console.close)
        console.boot()
        self.assertIsNone(console.hh.moonraker_link)

    def test_no_prime_leaves_the_gate_map_alone(self):
        console = console_mod.Console(console_mod.parse_args(
            ['--plain', '--no-log', '--header', 'off', '--no-prime']))
        self.addCleanup(console.close)
        console.boot()
        self.assertEqual(console.hh.primed, {})
        self.assertEqual(console.hh.mmu.gate_maps.gate_vendor[0], '')

    def test_the_selector_header_reports_the_carriage_and_the_servo(self):
        """
        The default profile has physical selectors, so the group must render something. Both
        positions are shown because a lasting disagreement between them is a bug - see
        Console._hdr_selector.
        """
        self.console.args.header = ['selector']
        lines = self.console.header_lines()
        self.assertTrue(lines, 'the selector group rendered nothing')
        text = '\n'.join(lines)
        self.assertIn('carriage=', text)
        self.assertIn('cmd=', text)
        self.assertIn('HOMED', text)
        self.assertIn('servo=', text)                # unit0 is a LinearServoSelector

    def test_showconfig_works_on_a_gate_of_every_unit(self):
        """
        SHOWCONFIG used to die with "'NoneType' object has no attribute
        'get_clog_detection_length'" on any unit without an encoder: mmu.encoder() returns
        None there, and mmu_status.py called it unguarded.

        The gate is selected explicitly on purpose. The bug only showed up because boot
        happens to leave the LAST gate selected, and a test that relied on that would stop
        testing anything the day boot order changed.

        _dispatch, not run_command: run_command catches and prints '!!', so a regression
        would pass silently. _dispatch lets the exception out, which is the assertion.
        """
        for unit in self.console.hh.mmu.mmu_machine.units:
            self.console._dispatch('MMU_SELECT GATE=%d' % unit.first_gate)
            with contextlib.redirect_stdout(io.StringIO()):
                self.console._dispatch('MMU_STATUS SHOWCONFIG=1')

    def _encoder_line(self):
        del self.console.sink[:]
        with contextlib.redirect_stdout(io.StringIO()):
            self.console.run_command('MMU_STATUS')
        for msg in self.console.sink:
            for line in msg.split('\n'):
                if 'Encoder reads' in line:
                    return line.strip()
        return ''

    def test_the_status_reports_each_units_own_encoder(self):
        """
        The reading printed under a unit's heading has to be THAT unit's.

        mmu.get_encoder_distance() takes no gate and resolves through gate_selected, so with
        a gate on the non-encoder unit selected it returned 0. and printed it under the
        encoder unit's heading - the right guard, the wrong object.
        """
        units = self.console.hh.mmu.mmu_machine.units
        with_encoder = [u for u in units if u.has_encoder()]
        if not with_encoder:
            self.skipTest('the default profile has no encoder to mis-report')
        expected = with_encoder[0].encoder.get_distance()
        self.assertNotEqual(expected, 0., 'nothing to distinguish a wrong reading from')
        # Select a gate on a DIFFERENT unit - the case that used to zero the reading
        elsewhere = [u for u in units if not u.has_encoder()]
        if not elsewhere:
            self.skipTest('single-unit default: nothing to select away to')
        self.console._dispatch('MMU_SELECT GATE=%d' % elsewhere[0].first_gate)
        self.assertIn('%.1fmm' % expected, self._encoder_line())

    def test_placing_the_carriage_by_hand_moves_the_tracked_position(self):
        """/selector is how a user stands in for physically sliding the carriage."""
        axis = self.console.hh.printer.harness_selectors[0]
        # meta() prints; keep it out of the runner's output
        with contextlib.redirect_stdout(io.StringIO()):
            self.console.meta('/selector gate 0')
            self.assertAlmostEqual(axis.carriage, axis.nominal_gate_offsets()[0], places=3)
            self.console.meta('/selector end')
            self.assertAlmostEqual(axis.carriage, axis.travel_max, places=3)
            self.console.meta('/selector home')
            self.assertAlmostEqual(axis.carriage, axis.travel_min, places=3)


if __name__ == '__main__':
    unittest.main()
