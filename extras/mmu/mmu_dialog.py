# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Pop-up dialogs in Mainsail/Fluidd/KlipperScreen using the "action:prompt_*" host protocol
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import re

ERROR_DIALOG_TITLE = "Happy Hare Pause Notice"
DIALOG_RESUME_HINT = "After fixing, press RESUME to continue printing (UNLOCK restores the nozzle temperature first)"

# Footer buttons, left to right. UNLOCK is usually the first step (reheat, then fix) so is the right-most, primary button
ABORT_BUTTON  = ('ABORT',  'CANCEL_PRINT', 'error')
RESUME_BUTTON = ('RESUME', 'RESUME',       'warning')
UNLOCK_BUTTON = ('UNLOCK', 'MMU_UNLOCK',   'primary')


def flatten(text):
    """One dialog line: any line breaks or runs of whitespace become single spaces"""
    return " ".join(str(text).split())


def dialog_lines(text):
    """Non-empty lines of text, whitespace flattened and trailing repeated periods dropped"""
    lines = []
    for line in str(text).split("\n"):
        line = re.sub(r'([.:!?])\.+$', r'\1', flatten(line))
        if line and line != '.':
            lines.append(line)
    return lines


def as_paragraph(lines):
    """Lines read on as sentences; the last is left as written"""
    return " ".join([l if l[-1] in ".:!?" else l + "." for l in lines[:-1]] + lines[-1:])


class MmuDialog:
    """
    Writes the prompt protocol directly to the host, one response per line, so text needs no
    gcode quoting and UIs never have to split a multi-line message
    """

    def __init__(self, gcode):
        self.gcode = gcode
        self.active = False

    def _send(self, action):
        self.gcode.respond_raw("// action:prompt_%s" % action)

    def show(self, title, lines=(), buttons=(), footer_buttons=()):
        """
        lines are strings, each sent as one flattened line; buttons are (label, gcode, style)
        """
        self._send("begin %s" % flatten(title))
        for line in lines:
            self._send("text %s" % flatten(line))
        if len(buttons) > 1:
            self._send("button_group_start")
        for button in buttons:
            self._send("button %s|%s|%s" % button)
        if len(buttons) > 1:
            self._send("button_group_end")
        for button in footer_buttons:
            self._send("footer_button %s|%s|%s" % button)
        self._send("show")
        self.active = True

    def close(self):
        # Always sent: UIs can restore a prompt from console history after a restart
        self._send("end")
        self.active = False


class MmuErrorDialog(MmuDialog):
    """
    MMU pause dialog, with optional per-print text and ABORT button set by the print start
    macros via MMU_ERROR_DIALOG
    """

    def __init__(self, mmu):
        super().__init__(mmu.gcode)
        self.mmu = mmu
        self.extra_lines = [] # Shown after the reason, replacing DIALOG_RESUME_HINT when there is no action
        self.show_abort = False

    def set(self, lines=None, show_abort=None):
        if lines is not None:
            self.extra_lines = [flatten(l) for l in lines if flatten(l)]
        if show_abort is not None:
            self.show_abort = show_abort

    def reset(self):
        self.extra_lines = []
        self.show_abort = False

    def buttons(self):
        return ([ABORT_BUTTON] if self.show_abort else []) + [RESUME_BUTTON, UNLOCK_BUTTON]

    def show_error(self, headline, reason, hint=None, context=None, still_paused=False, unlock_first=False):
        """
        The reason is one paragraph, led by what was being attempted (the error's context, which
        ends the reason) and followed by what to do next. Some UIs (KlipperScreen) only show the
        last text line, so the action goes last
        """
        reason = str(reason or "")
        body = reason[:-len(context)] if context and reason.endswith(context) else reason
        lines = []
        if still_paused: # The headline only says something when the pause is a repeat
            lines.append(headline)
        if context:
            lines.append(" ".join(dialog_lines(context)))
        paragraph = as_paragraph(dialog_lines(body))
        if paragraph:
            lines.append(paragraph)
        lines.extend(self.extra_lines)
        action = " ".join(dialog_lines(hint or ""))
        if action:
            if unlock_first:
                action = "UNLOCK to restore heating, then " + action[:1].lower() + action[1:]
            lines.append("Fix: %s" % action)
        elif not self.extra_lines:
            lines.append(DIALOG_RESUME_HINT)
        self.show(ERROR_DIALOG_TITLE, [l for l in lines if l], footer_buttons=self.buttons())
