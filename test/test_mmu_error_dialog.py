# Happy Hare test harness - the pause dialog shown in Mainsail / Fluidd / KlipperScreen.
#
# handle_mmu_error() calls the shipped _MMU_ERROR_DIALOG macro, whose RESPOND lines become
# action:prompt_* host commands. The real macro body is run here, and every line it and HH emit
# is re-parsed with Klipper's own rules (';' truncates the line even inside quotes, extended
# parameters go through posix shlex) because the harness parser is more forgiving than Klipper.
#
#   ./venv/bin/python -m unittest test.test_mmu_error_dialog
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import shlex
import unittest
from unittest.mock import patch

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

TIP_AT_GATE = -40.0
FILAMENT_POS_IN_BOWDEN = 3


def MmuError(*args, **kwargs):
    # Imported late: extras must resolve to the harness's klippy tree, which session() installs
    from extras.mmu.mmu_utils import MmuError
    return MmuError(*args, **kwargs)


def klipper_params(line):
    """Extended gcode parameters exactly as klippy/gcode.py parses them."""
    cpos = line.find(';')
    if cpos >= 0:
        line = line[:cpos]
    raw = line.strip().partition(' ')[2]
    s = shlex.shlex(raw, posix=True)
    s.whitespace_split = True
    s.commenters = '#;'
    return {k.upper(): v for k, v in (arg.split('=', 1) for arg in s)}


class ErrorDialogTestCase(unittest.TestCase):
    PROFILE = 'boxturtle'

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        for gate in (0, 1):
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.assertEqual(self.hh.errors, [], 'preload was not clean')
        self.hh.heat_extruder(220)
        self.hh.printer.harness_macro_effects['_MMU_ERROR_DIALOG'] = \
            lambda macro, gcmd: macro.run_body(gcmd)

    def tearDown(self):
        self.hh.close()

    def gcode(self, script, settle=0.5):
        self.hh.run_gcode(script)
        self.hh.settle(settle)

    def start_print(self, tool=0):
        self.gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.gcode('MMU_PRINT_START', settle=1.0)
        self.assertEqual(self.hh.errors, [], 'print start failed')
        self.assertTrue(self.mmu.is_printing())

    def mark(self):
        return len(self.hh.gcode.executed)

    def lines_since(self, mark):
        return self.hh.gcode.executed[mark:]

    def prompts_since(self, mark):
        """Every action:prompt_* host command emitted since mark, as Klipper would send it."""
        prompts = []
        for line in self.lines_since(mark):
            if line.split(None, 1)[0].upper() == 'RESPOND':
                msg = klipper_params(line).get('MSG', '')
                if msg.startswith('action:prompt_'):
                    prompts.append(msg)
        return prompts

    def last_dialog(self, prompts):
        begins = [i for i, p in enumerate(prompts) if p.startswith('action:prompt_begin')]
        self.assertTrue(begins, 'no dialog was shown: %r' % (prompts,))
        dialog = prompts[begins[-1]:]
        self.assertEqual(dialog[-1], 'action:prompt_show', dialog)
        return dialog

    @staticmethod
    def texts(dialog):
        return [p[len('action:prompt_text '):] for p in dialog if p.startswith('action:prompt_text ')]

    @staticmethod
    def footer_buttons(dialog):
        return [p[len('action:prompt_footer_button '):].split('|')
                for p in dialog if p.startswith('action:prompt_footer_button ')]


class TestDialogContent(ErrorDialogTestCase):

    def test_reason_with_gcode_special_characters_still_shows(self):
        self.start_print()
        mark = self.mark()
        self.mmu.handle_mmu_error('Klipper reports: sensor "mmu_gate; it\'s stuck at #3')

        invocation = [l for l in self.lines_since(mark) if l.startswith('_MMU_ERROR_DIALOG')]
        self.assertEqual(len(invocation), 1)
        klipper_params(invocation[0]) # Must not be malformed for Klipper

        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertIn("Klipper reports: sensor 'mmu_gate, it's stuck at #3", texts)

    def test_each_reason_line_is_its_own_text_and_the_action_is_last(self):
        self.start_print()
        mark = self.mark()
        self.mmu.handle_mmu_error(
            "Load sequence failed because:\n"
            "Couldn't pick up filament at gate.\n"
            "Gate marked as empty.\n"
            ".\nOccurred when changing tool: T0 > T1")

        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertEqual(texts, [
            "Print paused",
            "Load sequence failed because:",
            "Couldn't pick up filament at gate.",
            "Gate marked as empty.",
            "Occurred when changing tool: T0 > T1",
            "After fixing, press RESUME to continue printing (UNLOCK restores the nozzle temperature first)",
        ])

    def test_buttons_are_footer_buttons_and_abort_is_optional(self):
        self.start_print()
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 2 is empty')
        dialog = self.last_dialog(self.prompts_since(mark))
        self.assertEqual(self.footer_buttons(dialog), [
            ['UNLOCK', 'MMU_UNLOCK', 'secondary'],
            ['RESUME', 'RESUME', 'primary'],
        ])
        self.assertFalse([p for p in dialog if p.startswith('action:prompt_button')])

        self.gcode('RESUME')
        self.gcode('SET_GCODE_VARIABLE MACRO=_MMU_ERROR_DIALOG VARIABLE=show_abort VALUE=True')
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 2 is empty')
        dialog = self.last_dialog(self.prompts_since(mark))
        self.assertEqual(self.footer_buttons(dialog)[0], ['ABORT', 'CANCEL_PRINT', 'error'])

    def test_custom_msg_replaces_the_default_action_line(self):
        self.start_print()
        self.hh.printer.lookup_object('gcode_macro _MMU_ERROR_DIALOG').variables['custom_msg'] = \
            ['Print requires tools: T0 (Gate0)', 'Manually ensure that T0 is loaded']
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 2 is empty')
        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertEqual(texts[-2:], ['Print requires tools: T0 (Gate0)', 'Manually ensure that T0 is loaded'])


class TestDialogLifecycle(ErrorDialogTestCase):

    def test_refused_resume_shows_the_dialog_again(self):
        self.start_print(0)
        self.gcode('MMU_CHANGE_TOOL TOOL=3') # Gate 3 never preloaded
        self.assertTrue(self.mmu.is_mmu_paused())
        self.mmu.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN, silent=True)

        mark = self.mark()
        self.gcode('RESUME')
        self.assertTrue(self.mmu.is_mmu_paused())

        prompts = self.prompts_since(mark)
        self.assertIn('action:prompt_end', prompts)
        texts = self.texts(self.last_dialog(prompts[prompts.index('action:prompt_end'):]))
        self.assertEqual(texts[0], 'Print is still paused')
        self.assertTrue(any(t.startswith('Cannot resume because') for t in texts), texts)

    def test_failed_loaded_check_on_resume_pauses_with_a_dialog(self):
        self.start_print(0)
        self.gcode('MMU_CHANGE_TOOL TOOL=3')
        mark = self.mark()
        self.gcode('RESUME')
        self.assertTrue(self.mmu.is_mmu_paused(), 'print resumed with no filament loaded')
        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertTrue(any('Loaded check failed' in t for t in texts), texts)

    def test_error_while_dialog_is_open_keeps_the_original(self):
        self.start_print()
        self.mmu.handle_mmu_error('First problem')
        mark = self.mark()
        self.mmu.handle_mmu_error('Second problem')
        self.assertEqual(self.prompts_since(mark), [])
        self.assertEqual(self.mmu.psm.reason_for_pause, 'First problem')

    def test_successful_resume_closes_the_dialog(self):
        self.start_print()
        self.mmu.handle_mmu_error('Something happened')
        mark = self.mark()
        self.gcode('RESUME')
        self.assertFalse(self.mmu.is_mmu_paused())
        self.assertEqual(self.prompts_since(mark), ['action:prompt_end'])


class TestActionableReasons(ErrorDialogTestCase):

    def test_hint_survives_the_load_and_toolchange_wraps(self):
        self.start_print(0)
        mark = self.mark()
        with patch.object(self.mmu, '_load_bowden', side_effect=MmuError('Bowden jammed', hint='Clear the bowden, then RESUME')):
            self.gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertTrue(self.mmu.is_mmu_paused())

        reason = self.mmu.psm.reason_for_pause
        self.assertIn('Load sequence failed because:\nBowden jammed', reason)
        self.assertIn('Occurred when changing tool: T0 > T1', reason)
        self.assertEqual(self.mmu.psm.pause_hint, 'Clear the bowden, then RESUME')
        self.assertEqual(self.mmu.get_status(0)['pause_hint'], 'Clear the bowden, then RESUME')
        self.assertIn('Clear the bowden, then RESUME', self.hh.console)

        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertEqual(texts[-1], 'Clear the bowden, then RESUME')

    def test_hint_follows_custom_msg_as_the_last_line(self):
        self.start_print()
        self.hh.printer.lookup_object('gcode_macro _MMU_ERROR_DIALOG').variables['custom_msg'] = ['Print requires tools: T0']
        mark = self.mark()
        self.mmu.handle_mmu_error(MmuError('Gate 1 jammed', hint='Free gate 1'))
        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertEqual(texts[-2:], ['Print requires tools: T0', 'Free gate 1'])

    def test_resume_clears_the_hint(self):
        self.start_print()
        self.mmu.handle_mmu_error(MmuError('Something', hint='Do this'))
        self.gcode('RESUME')
        self.assertFalse(self.mmu.is_mmu_paused())
        self.assertIsNone(self.mmu.psm.pause_hint)
        self.assertEqual(self.mmu.get_status(0)['pause_hint'], '')

    def test_clog_names_tool_gate_and_flowguard_reason(self):
        self.start_print(0)
        self.mmu.flowguard_trip_reason = 'Compression stuck after 30.0mm motion'
        self.gcode('__MMU_SENSOR_CLOG SENSOR=mmu_compression')
        self.assertTrue(self.mmu.is_mmu_paused())

        reason = self.mmu.psm.reason_for_pause
        self.assertIn('A clog has been detected on T0 (gate 0) by mmu_compression', reason)
        self.assertIn('FlowGuard trip reason: Compression stuck after 30.0mm motion', reason)
        self.assertTrue(self.mmu.psm.pause_hint.startswith('Check the nozzle and extruder for a clog'))
        self.assertIsNone(self.mmu.flowguard_trip_reason)

    def test_standalone_failure_does_not_suggest_resume(self):
        self.assertFalse(self.mmu.is_in_print())
        mark = len(self.hh.console)
        self.mmu.handle_mmu_error(MmuError('Gate 0 jammed', hint='Free gate 0, then reload the tool and RESUME'))
        self.assertTrue(any('Gate 0 jammed' in e for e in self.hh.errors), self.hh.errors)
        self.assertFalse([l for l in self.hh.console[mark:] + self.hh.errors if 'RESUME' in l])

    def test_bare_mmu_pause_says_no_reason_was_given(self):
        self.start_print()
        self.gcode('MMU_PAUSE')
        self.assertEqual(self.mmu.psm.reason_for_pause, 'Paused by MMU_PAUSE (no MSG reason given)')


class TestKlipperErrors(ErrorDialogTestCase):
    """A Klipper command_error raised part way through an MMU operation in a print."""

    KLIPPER_ERROR = "Requested temperature (300.0) out of range (0.0:290.0)" # M104 from a gate map temperature

    def command_error(self):
        return self.hh.printer.command_error(self.KLIPPER_ERROR)

    def test_toolchange_pauses_with_a_dialog(self):
        self.start_print(0)
        mark = self.mark()
        with patch.object(self.mmu, '_load_bowden', side_effect=self.command_error()):
            self.gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertTrue(self.mmu.is_mmu_paused())

        reason = self.mmu.psm.reason_for_pause
        self.assertIn('Requested temperature (300.0) out of range', reason)
        self.assertIn('Occurred when changing tool: T0 > T1', reason)
        self.assertTrue(self.mmu.psm.pause_hint.startswith('Fix the cause of the Klipper error'))

        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertIn('Klipper error: Requested temperature (300.0) out of range (0.0:290.0).', texts)
        self.assertEqual(texts[-1], self.mmu.psm.pause_hint)

    def test_toolchange_does_not_retry_a_klipper_error(self):
        self.mmu.p.retry_tool_change_on_error = 1
        self.start_print(0)
        with patch.object(self.mmu, '_load_bowden', side_effect=self.command_error()) as load_bowden:
            self.gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertTrue(self.mmu.is_mmu_paused())
        self.assertEqual(load_bowden.call_count, 1)

    def test_load_pauses(self):
        self.start_print(0)
        self.gcode('MMU_UNLOAD')
        with patch.object(self.mmu, '_load_bowden', side_effect=self.command_error()):
            self.gcode('MMU_LOAD')
        self.assertTrue(self.mmu.is_mmu_paused())
        self.assertIn('Requested temperature (300.0) out of range', self.mmu.psm.reason_for_pause)
        self.assertIn('Occurred when loading tool', self.mmu.psm.reason_for_pause)

    def test_unload_pauses(self):
        self.start_print(0)
        with patch.object(self.mmu, '_unload_bowden', side_effect=self.command_error()):
            self.gcode('MMU_UNLOAD')
        self.assertTrue(self.mmu.is_mmu_paused())
        self.assertIn('Requested temperature (300.0) out of range', self.mmu.psm.reason_for_pause)
        self.assertIn('Occurred when unloading tool', self.mmu.psm.reason_for_pause)

    def failing_restore_move(self, times):
        """The post-toolchange RESTORE_GCODE_STATE move raises 'Move out of range' the first 'times' times."""
        real = self.mmu.gcode.run_script_from_command
        left = [times]
        def run_script_from_command(script):
            if script.startswith('RESTORE_GCODE_STATE NAME=MMU_state') and 'MOVE=1' in script and left[0] > 0:
                left[0] -= 1
                raise self.hh.printer.command_error('Move out of range: 400.000 10.000 1.000 [0.000]')
            return real(script)
        return patch.object(self.mmu.gcode, 'run_script_from_command', side_effect=run_script_from_command)

    def test_failed_restore_move_pauses_and_resume_completes(self):
        self.start_print(0)
        with self.failing_restore_move(1):
            self.gcode('MMU_CHANGE_TOOL TOOL=1')
            self.assertTrue(self.mmu.is_mmu_paused())
            reason = self.mmu.psm.reason_for_pause
            self.assertIn('Klipper error: Move out of range', reason)
            self.assertIn('Occurred when changing tool: T0 > T1', reason)

            self.gcode('RESUME')
        self.assertFalse(self.mmu.is_mmu_paused())
        self.assertTrue(self.mmu.is_printing())
        self.assertFalse(self.mmu.saved_toolhead_operation)

    def test_restore_move_that_keeps_failing_pauses_again_on_resume(self):
        self.start_print(0)
        with self.failing_restore_move(99):
            self.gcode('MMU_CHANGE_TOOL TOOL=1')
            self.assertTrue(self.mmu.is_mmu_paused())
            mark = self.mark()
            self.gcode('RESUME') # Must not raise, or the print carries on from the park position
        self.assertTrue(self.mmu.is_mmu_paused())
        self.assertTrue(self.hh.printer.lookup_object('pause_resume').is_paused)
        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertTrue(any('Move out of range' in t for t in texts), texts)

    def test_out_of_a_print_the_klipper_error_is_raised_unchanged(self):
        self.assertFalse(self.mmu.is_in_print())
        with patch.object(self.mmu, '_load_bowden', side_effect=self.command_error()):
            with self.assertRaises(self.hh.printer.command_error) as raised:
                self.gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertEqual(str(raised.exception), self.KLIPPER_ERROR)
        self.assertFalse(self.mmu.is_mmu_paused())

    def test_after_a_shutdown_the_klipper_error_is_raised_unchanged(self):
        self.start_print(0)
        with patch.object(self.mmu, '_load_bowden', side_effect=self.command_error()), \
             patch.object(self.hh.printer, 'is_shutdown', return_value=True):
            with self.assertRaises(self.hh.printer.command_error) as raised:
                self.gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertEqual(str(raised.exception), self.KLIPPER_ERROR)
        self.assertFalse(self.mmu.is_mmu_paused())


class TestEndlessSpoolSwapFailure(ErrorDialogTestCase):

    def setUp(self):
        self.hh = session('boxturtle_test')
        self.hh.boot()
        self.mmu = self.hh.mmu
        for gate in range(4):
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_ENDLESS_SPOOL GROUPS=1,1,1,1 ENABLE=1')
        self.assertEqual(self.hh.errors, [], 'setup was not clean')

    def test_failed_swap_says_it_was_an_endless_spool_swap(self):
        self.start_print(0)
        gate = self.mmu.gate_selected
        with patch.object(self.mmu, '_select_and_load_tool', side_effect=MmuError('Load failed', hint='Fix the gate')):
            self.hh.filament().exhaust(gate) # The armed runout sensor starts the swap
            self.hh.settle()
        self.assertTrue(self.mmu.is_mmu_paused())

        reason = self.mmu.psm.reason_for_pause
        self.assertIn('Load failed', reason)
        self.assertIn('Occurred during EndlessSpool swap of T0 (gate %d) to gate %d' % (gate, self.mmu.gate_selected), reason)
        self.assertEqual(self.mmu.psm.pause_hint, 'Fix the gate')

    def test_klipper_error_during_the_swap_pauses(self):
        self.start_print(0)
        gate = self.mmu.gate_selected
        with patch.object(self.mmu, '_select_and_load_tool', side_effect=self.hh.printer.command_error('Move out of range')):
            self.hh.filament().exhaust(gate)
            self.hh.settle()
        self.assertTrue(self.mmu.is_mmu_paused())

        reason = self.mmu.psm.reason_for_pause
        self.assertIn('Klipper error: Move out of range', reason)
        self.assertIn('Occurred during EndlessSpool swap of T0 (gate %d)' % gate, reason)
        self.assertTrue(self.mmu.psm.pause_hint.startswith('Fix the cause of the Klipper error'))


if __name__ == '__main__':
    unittest.main()
