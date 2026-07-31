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
import unittest

from test.hh import cfg as cfg_mod
from test.hh import profiles, session
from test import console as console_mod

logging.getLogger().setLevel(logging.CRITICAL)


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

    NEUTRAL = {'0', '', '39', '22', '49'}

    def assert_no_line_leaks(self, rendered):
        """No line may end with a colour or bold still open - see the pink-terminal bug."""
        for line in rendered.split('\n'):
            codes = __import__('re').findall(r'\x1b\[([0-9;]*)m', line)
            if codes:
                self.assertIn(codes[-1], self.NEUTRAL,
                              'line ends with attribute %r still open: %r'
                              % (codes[-1], line))

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


class TestConsoleScript(unittest.TestCase):
    """
    End to end through main(), the same path the prompt uses.

    PINNED TO BOXTURTLE on purpose. These tests are about console MECHANICS - header groups,
    /sensor, /log - and they name specific sensors (mmu_entry_0) and issue unit-less commands
    (MMU_HOME). Both are properties of the machine, not of the console, so following whatever
    --profile happens to be the default made them fail the moment the default became a
    multi-unit ERCF+ViViD: it has no per-gate entry sensors on unit0, and MMU_HOME there
    requires a UNIT. The default profile gets its own coverage below instead.
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
            rc = console_mod.main(['--profile', self.PROFILE, '--plain', '--script', path]
                                  + list(extra_args))
        return rc, buf.getvalue()

    def _make_console(self, argv):
        """A booted Console on THIS class's profile, closed on teardown."""
        console = console_mod.Console(
            console_mod.parse_args(['--profile', self.PROFILE] + list(argv)))
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

    def test_state_was_renamed_to_vars(self):
        rc, out = self._run(['/state'], ['--header', 'off'])
        self.assertIn('unknown meta-command /state', out)

    def test_sensor_can_be_disabled_so_happy_hare_ignores_it(self):
        rc, out = self._run(['/sensor mmu_entry_1 disable', '/sensor mmu_entry_1 enable'],
                            ['--header', 'off'])
        self.assertEqual(rc, 0, out[-1500:])
        self.assertIn('mmu_entry_1 disabled', out)
        self.assertIn('mmu_entry_1 enabled', out)

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
            self.assertTrue(console.sink, 'nothing to clear')
            console.meta('/clear')
        self.assertEqual(console.sink, [], '/clear did not empty the log')

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
                             '/vars', '/clear', '/log 3', '/errors', '/badmeta'],
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


if __name__ == '__main__':
    unittest.main()
