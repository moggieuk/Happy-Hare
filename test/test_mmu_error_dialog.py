# Happy Hare test harness - the pause dialog shown in Mainsail / Fluidd / KlipperScreen.
#
# handle_mmu_error() sends the dialog as '// action:prompt_*' host command lines (respond_raw),
# so its text is never re-parsed as gcode. Gcode that the print start macros send to
# MMU_ERROR_DIALOG is checked with Klipper's own parsing rules (';' truncates the line even inside
# quotes, extended parameters go through posix shlex) because the harness parser is more forgiving.
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
        return len(self.hh.gcode.raw)

    def prompts_since(self, mark):
        """Every action:prompt_* host command emitted since mark, without Klipper's '// ' prefix."""
        return [m[len('// '):] for m in self.hh.gcode.raw[mark:] if m.startswith('// action:prompt_')]

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

    def test_line_breaks_in_custom_paragraphs_keep_the_dialog_valid(self):
        self.start_print()
        for separator in ('\n', '\r\n', '\r'):
            with self.subTest(separator=repr(separator)):
                self.mmu.error_dialog.set(lines=['Check the tube.' + separator + 'Then reload.',
                                                 'Keep this as a separate paragraph.'])
                mark = self.mark()
                self.mmu._show_mmu_error_dialog('Print paused', 'Gate 1 jammed')
                dialog = self.last_dialog(self.prompts_since(mark))
                self.assertEqual(self.texts(dialog)[-2:], [
                    'Check the tube. Then reload.', 'Keep this as a separate paragraph.'])
                self.assertEqual(len(self.footer_buttons(dialog)), 2)

    def test_reason_with_gcode_special_characters_is_shown_unchanged(self):
        self.start_print()
        mark = self.mark()
        self.mmu.handle_mmu_error('Klipper reports: sensor "mmu_gate; it\'s stuck at #3 in C:\\path')
        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertIn('Klipper reports: sensor "mmu_gate; it\'s stuck at #3 in C:\\path', texts)

    def test_multi_line_reason_reads_as_one_paragraph_and_the_action_is_last(self):
        self.start_print()
        mark = self.mark()
        self.mmu.handle_mmu_error(
            "Load sequence failed because:\n"
            "Couldn't pick up filament at gate\n"
            "Gate marked as empty. Use 'MMU_GATE_MAP GATE=1 AVAILABLE=1' to reset")

        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertEqual(texts, [
            "Load sequence failed because: Couldn't pick up filament at gate. "
            "Gate marked as empty. Use 'MMU_GATE_MAP GATE=1 AVAILABLE=1' to reset",
            "After fixing, press RESUME to continue printing (UNLOCK restores the nozzle temperature first)",
        ])

    def test_error_context_is_kept_through_rewording(self):
        error = MmuError('Gate 1 jammed', hint='Free it').with_context('Occurred when loading tool')
        self.assertEqual(str(error), 'Gate 1 jammed.\nOccurred when loading tool')
        self.assertEqual(error.context, 'Occurred when loading tool')
        reworded = error.with_message('Load sequence failed because:\n%s' % str(error))
        self.assertEqual((reworded.hint, reworded.context), ('Free it', 'Occurred when loading tool'))

    def test_context_leads_the_dialog_and_still_ends_the_logged_reason(self):
        self.start_print()
        mark = self.mark()
        error = MmuError("Load sequence failed because:\nGate 1 jammed", hint='Free gate 1, then RESUME')
        self.mmu.handle_mmu_error(error.with_context('Occurred when changing tool: T0 > T1'))

        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertEqual(texts, [
            "Occurred when changing tool: T0 > T1",
            "Load sequence failed because: Gate 1 jammed.",
            "Fix: UNLOCK to restore heating, then free gate 1, then RESUME",
        ])
        self.assertEqual(self.mmu.psm.reason_for_pause,
                         "Load sequence failed because:\nGate 1 jammed.\nOccurred when changing tool: T0 > T1")

    def test_title_and_a_fresh_pause_has_no_headline(self):
        self.start_print()
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 1 jammed')
        dialog = self.last_dialog(self.prompts_since(mark))
        self.assertEqual(dialog[0], 'action:prompt_begin Happy Hare Pause Notice')
        self.assertNotIn('Print paused', self.texts(dialog))
        self.assertEqual(self.texts(dialog)[0], 'Gate 1 jammed')

    def test_still_paused_headline_only_when_the_dialog_is_shown_again(self):
        self.start_print()
        self.gcode('MMU_CHANGE_TOOL TOOL=3') # Gate 3 never preloaded
        self.mmu.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN, silent=True)
        mark = self.mark()
        self.gcode('RESUME')
        prompts = self.prompts_since(mark)
        dialog = self.last_dialog(prompts[prompts.index('action:prompt_end'):])
        self.assertEqual(self.texts(dialog)[0], 'Print is still paused')
        # One sentence runs on across the message's lines rather than being split by periods
        self.assertIn('run MMU_RECOVER LOADED=1 or MMU_RECOVER LOADED=0 or just MMU_RECOVER to reset state, then RESUME again', self.texts(dialog)[1])
        self.assertNotIn('MMU_RECOVER.', self.texts(dialog)[1])

    def test_buttons_are_footer_buttons_and_abort_is_optional(self):
        self.start_print()
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 2 is empty')
        dialog = self.last_dialog(self.prompts_since(mark))
        self.assertEqual(self.footer_buttons(dialog), [
            ['RESUME', 'RESUME', 'warning'],
            ['UNLOCK', 'MMU_UNLOCK', 'primary'],
        ])
        self.assertFalse([p for p in dialog if p.startswith('action:prompt_button')])

        self.gcode('RESUME')
        self.gcode('MMU_ERROR_DIALOG ABORT=1')
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 2 is empty')
        dialog = self.last_dialog(self.prompts_since(mark))
        self.assertEqual(self.footer_buttons(dialog)[0], ['ABORT', 'CANCEL_PRINT', 'error'])

    def test_custom_msg_replaces_the_default_action_line(self):
        self.start_print()
        self.mmu.error_dialog.set(lines=['Print requires tools: T0 (Gate0)', 'Manually ensure that T0 is loaded'])
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 2 is empty')
        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertEqual(texts[-2:], ['Print requires tools: T0 (Gate0)', 'Manually ensure that T0 is loaded'])

    def test_custom_msg_with_gcode_special_characters_is_shown_unchanged(self):
        self.start_print()
        self.mmu.error_dialog.set(lines=['Check the 3" tube', "Don't skip it", 'See C:\\notes'])
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 2 is empty')
        dialog = self.last_dialog(self.prompts_since(mark))
        self.assertEqual(self.texts(dialog)[-3:], ['Check the 3" tube', "Don't skip it", 'See C:\\notes'])
        self.assertEqual(len(self.footer_buttons(dialog)), 2)

    def test_semicolons_in_a_custom_line_are_kept(self):
        self.start_print()
        self.mmu.error_dialog.set(lines=['Check the tube; then the gate'])
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 2 is empty')
        dialog = self.last_dialog(self.prompts_since(mark))
        self.assertIn('Check the tube; then the gate', self.texts(dialog))
        self.assertEqual(len(self.footer_buttons(dialog)), 2)


class TestDialogLifecycle(ErrorDialogTestCase):

    def test_unloaded_bypass_start_reaches_pause_with_espooler(self):
        self.start_print()
        self.gcode('MMU_UNLOAD')
        self.gcode('MMU_SELECT_BYPASS')
        self.assertTrue(self.mmu.has_espooler())
        self.hh.printer.harness_macro_effects['MMU_START_LOAD_INITIAL_TOOL'] = \
            lambda macro, gcmd: macro.run_body(gcmd)
        mark = self.mark()
        self.gcode('MMU_START_LOAD_INITIAL_TOOL')
        self.assertTrue(self.mmu.is_mmu_paused())
        dialog = self.last_dialog(self.prompts_since(mark))
        self.assertIn('Load bypass or initial tool before resuming print', self.texts(dialog))
        self.assertEqual(len(self.footer_buttons(dialog)), 2)

    def test_espooler_status_handles_bypass_and_preserves_gate_values(self):
        from extras.mmu.mmu_constants import ESPOOLER_NONE, ESPOOLER_OFF, TOOL_GATE_UNKNOWN
        self.assertTrue(self.mmu.has_espooler())
        self.assertEqual(self.mmu.get_status(0)['espooler_active'],
                         self.mmu.espooler().get_operation(self.mmu.gate_selected)[0])
        self.mmu._set_gate_selected(TOOL_GATE_UNKNOWN)
        self.assertEqual(self.mmu.get_status(0)['espooler_active'], ESPOOLER_OFF)
        self.gcode('MMU_SELECT_BYPASS')
        self.assertEqual(self.mmu.get_status(0)['espooler_active'], ESPOOLER_NONE)

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
        self.assertTrue(texts[-1].startswith('Fix: Finish loading or unloading the filament, run MMU_RECOVER'), texts)

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


class TestDialogConfiguration(ErrorDialogTestCase):

    def pause(self):
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 2 is empty')
        return self.last_dialog(self.prompts_since(mark))

    def test_disabled_dialog_still_logs_to_the_console(self):
        self.start_print()
        self.gcode('MMU_TEST_CONFIG show_error_dialog=0 QUIET=1')
        mark = self.mark()
        self.mmu.handle_mmu_error('Gate 2 is empty')
        self.assertEqual(self.prompts_since(mark), [])
        self.assertTrue(any('Gate 2 is empty' in e for e in self.hh.errors))

    def test_abort_can_be_turned_off_again(self):
        self.start_print()
        self.gcode('MMU_ERROR_DIALOG ABORT=1')
        self.gcode('MMU_ERROR_DIALOG ABORT=0')
        self.assertEqual(self.footer_buttons(self.pause()), [
            ['RESUME', 'RESUME', 'warning'],
            ['UNLOCK', 'MMU_UNLOCK', 'primary'],
        ])

    def test_start_macro_message_survives_klipper_parsing(self):
        # The line MMU_START_SETUP renders from {custom_msg|join('\\n')}
        line = 'MMU_ERROR_DIALOG MSG="Print requires tools: T0 (Gate0), Initial Tool: T0\\nManually ensure that T0 is loaded"'
        self.assertEqual(klipper_params(line)['MSG'],
                         'Print requires tools: T0 (Gate0), Initial Tool: T0\\nManually ensure that T0 is loaded')
        self.start_print()
        self.gcode(line)
        self.assertEqual(self.texts(self.pause())[-2:], [
            'Print requires tools: T0 (Gate0), Initial Tool: T0', 'Manually ensure that T0 is loaded'])

    def test_overrides_reset_at_print_end_and_next_print_start(self):
        self.start_print()
        self.gcode('MMU_ERROR_DIALOG ABORT=1 MSG="Stale"')
        self.gcode('MMU_PRINT_END STATE=cancelled')
        self.assertEqual((self.mmu.error_dialog.extra_lines, self.mmu.error_dialog.show_abort), ([], False))

        self.mmu.error_dialog.set(lines=['Stale'], show_abort=True) # e.g. a manual call between prints
        self.start_print()
        self.assertEqual((self.mmu.error_dialog.extra_lines, self.mmu.error_dialog.show_abort), ([], False))

    def test_overrides_set_during_print_start_survive_it(self):
        # MMU_START_SETUP runs MMU_PRINT_START before it sets its overrides, and print_stats
        # detection may run the full print start (AUTOMATIC=1) after them
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.mmu.psm.on_print_start(pre_start_only=True)
        self.gcode('MMU_ERROR_DIALOG ABORT=1 MSG="Start-up help"')
        self.gcode('MMU_PRINT_START AUTOMATIC=1')
        self.gcode('MMU_PRINT_START')
        dialog = self.pause()
        self.assertEqual(self.footer_buttons(dialog)[0], ['ABORT', 'CANCEL_PRINT', 'error'])
        self.assertIn('Start-up help', self.texts(dialog))

    def test_reset_command_restores_the_defaults(self):
        self.start_print()
        self.gcode('MMU_ERROR_DIALOG ABORT=1 MSG="Custom"')
        self.gcode('MMU_ERROR_DIALOG RESET=1')
        dialog = self.pause()
        self.assertEqual(self.texts(dialog)[-1], 'After fixing, press RESUME to continue printing (UNLOCK restores the nozzle temperature first)')
        self.assertEqual(len(self.footer_buttons(dialog)), 2)

    def assert_closes(self, script):
        self.start_print()
        self.pause()
        mark = self.mark()
        self.gcode(script)
        self.assertEqual(self.prompts_since(mark).count('action:prompt_end'), 1)
        self.assertFalse(self.mmu.error_dialog.active)

    def test_unlock_closes_the_dialog(self):
        self.assert_closes('MMU_UNLOCK')

    def test_clear_pause_closes_the_dialog(self):
        self.assert_closes('CLEAR_PAUSE')

    def test_cancel_print_closes_the_dialog(self):
        self.assert_closes('CANCEL_PRINT')

    def test_close_is_sent_when_no_dialog_is_known_to_be_open(self):
        # After a Klipper restart a UI can still show a dialog restored from its console history
        mark = self.mark()
        self.gcode('CLEAR_PAUSE')
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
        self.assertEqual(texts[-1], 'Fix: UNLOCK to restore heating, then clear the bowden, then RESUME')

    def test_hint_follows_custom_msg_as_the_last_line(self):
        self.start_print()
        self.mmu.error_dialog.set(lines=['Print requires tools: T0'])
        mark = self.mark()
        self.mmu.handle_mmu_error(MmuError('Gate 1 jammed', hint='Free gate 1'))
        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertEqual(texts[-2:], ['Print requires tools: T0', 'Fix: UNLOCK to restore heating, then free gate 1'])

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
        self.assertTrue(self.mmu.psm.pause_hint.startswith('Check the nozzle and extruder of T0 (gate 0) for a clog'))
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
        self.assertTrue(self.mmu.psm.pause_hint.startswith('Resolve the cause of the Klipper error'))

        texts = self.texts(self.last_dialog(self.prompts_since(mark)))
        self.assertIn('Klipper error: Requested temperature (300.0) out of range (0.0:290.0).', texts)
        self.assertTrue(texts[0].startswith('Occurred when changing tool'), texts)
        hint = self.mmu.psm.pause_hint
        self.assertEqual(texts[-1], 'Fix: UNLOCK to restore heating, then ' + hint[0].lower() + hint[1:])

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
        self.assertTrue(self.mmu.psm.pause_hint.startswith('Resolve the cause of the Klipper error'))


if __name__ == '__main__':
    unittest.main()
