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

BUTTON_STYLES = ('primary', 'secondary', 'info', 'warning', 'error')

BUILTIN_BUTTONS = {
    'abort':  ('ABORT',  'CANCEL_PRINT', 'error'),
    'resume': ('RESUME', 'RESUME',       'warning'),
    'unlock': ('UNLOCK', 'MMU_UNLOCK',   'primary'),
}

DEFAULT_ERROR_DIALOG_TITLE = "Happy Hare Pause Notice"
DEFAULT_ERROR_DIALOG_BUTTONS = ["resume", "unlock"]
DIALOG_RESUME_HINT = "After fixing, press RESUME to continue printing (UNLOCK restores the nozzle temperature first)"


def parse_buttons(entries):
    """
    Convert button entries into (label, gcode, style) tuples. Each entry is either a
    BUILTIN_BUTTONS key or a custom 'LABEL|GCODE[|style]'. Raises ValueError if invalid
    """
    buttons = []
    for entry in entries:
        entry = str(entry).strip()
        builtin = BUILTIN_BUTTONS.get(entry.lower())
        if builtin:
            buttons.append(builtin)
            continue
        parts = [p.strip() for p in entry.split('|')]
        if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
            raise ValueError(
                "button '%s' must be one of %s or 'LABEL|GCODE[|style]'"
                % (entry, ", ".join(BUILTIN_BUTTONS)))
        style = parts[2].lower() if len(parts) == 3 and parts[2] else 'primary'
        if style not in BUTTON_STYLES:
            raise ValueError(
                "button '%s' has invalid style '%s' (choose from %s)"
                % (entry, style, ", ".join(BUTTON_STYLES)))
        buttons.append((parts[0], parts[1], style))
    return buttons


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
    MMU pause dialog. Title and buttons come from parameters, with optional per-print text and
    extra buttons set by the print start macros via MMU_ERROR_DIALOG
    """

    def __init__(self, mmu):
        super().__init__(mmu.gcode)
        self.mmu = mmu
        self.extra_lines = []   # Shown after the reason, replacing DIALOG_RESUME_HINT when there is no action
        self.extra_buttons = [] # Shown ahead (left) of the configured buttons

    def set(self, lines=None, buttons=None):
        if lines is not None:
            self.extra_lines = [flatten(l) for l in lines if flatten(l)]
        if buttons is not None:
            self.extra_buttons = parse_buttons(buttons)

    def reset(self):
        self.extra_lines = []
        self.extra_buttons = []

    def title(self):
        return flatten(self.mmu.p.error_dialog_title) or DEFAULT_ERROR_DIALOG_TITLE

    def buttons(self):
        result = []
        configured = self.mmu.p.error_dialog_buttons or DEFAULT_ERROR_DIALOG_BUTTONS
        for button in self.extra_buttons + parse_buttons(configured):
            if button not in result:
                result.append(button)
        return result

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
        self.show(self.title(), [l for l in lines if l], footer_buttons=self.buttons())
