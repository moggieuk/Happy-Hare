# Fake Klipper `klippy/gcode.py` for the Happy Hare test harness.
#
# Needs a real (if minimal) parser, because the route into bootup is a gcode string:
# MmuPrintStateMachine.print_event("__MMU_BOOTUP") -> gcode.run_script("__MMU_BOOTUP")
# (extras/mmu/mmu_print_state_machine.py:111) -> cmd_MMU_BOOTUP.
#
# THE ERROR SENTINEL LIVES HERE, and it is not optional. cmd_MMU_BOOTUP wraps its
# entire body in `except Exception -> log_assertion` and then fires mmu:bootup
# UNCONDITIONALLY (extras/mmu/mmu_controller.py:307-456). So "mmu:bootup fired" is a
# vacuous assertion on its own - a bootup test that only checks the event goes green
# while bootup is completely broken. Every HH error funnels through respond_raw with
# a '!!' prefix (MmuLogger.log_assertion at extras/mmu/mmu_logger.py:137,
# log_error at :142), so respond_raw records those into `errors` and bootup tests
# assert it is empty.
#
# register_command(name, None) must RETURN AND REMOVE the previous handler: that is
# the idiom MmuController.handle_ready uses to wrap PAUSE/RESUME/CLEAR_PAUSE/
# CANCEL_PRINT (extras/mmu/mmu_controller.py:245-252).
#
# Unknown commands are recorded and ignored by default (HH legitimately issues
# M104/M117/M220/SET_TMC_CURRENT/... owned by Klipper modules we do not fake), with
# strict=True to turn them into errors for targeted tests. `unhandled` is asserted on
# in one test so the set stays visible and changes get reviewed.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import re, shlex, logging


class error(Exception):
    pass


class CommandError(Exception):
    pass


# KEY=VALUE, allowing quoted values and bare flags
_PARAM_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s]*)')


class GCodeCommand:
    error = CommandError

    def __init__(self, gcode, command, commandline, params, need_ack=True):
        self._gcode = gcode
        self._command = command
        self._commandline = commandline
        self._params = params
        self._need_ack = need_ack

    def get_command(self):
        return self._command

    def get_commandline(self):
        return self._commandline

    def get_command_parameters(self):
        return self._params

    def get_raw_command_parameters(self):
        cmd = self._command
        origline = self._commandline
        if origline.upper().startswith(cmd.upper()):
            return origline[len(cmd):].strip()
        return origline

    def ack(self, msg=None):
        if not self._need_ack:
            return False
        if msg:
            self.respond_info(msg)
        self._need_ack = False
        return True

    def respond_info(self, msg, log=True):
        self._gcode.respond_info(msg, log=log)

    def respond_raw(self, msg):
        self._gcode.respond_raw(msg)

    # -- typed getters, with Klipper's validation semantics ----------------
    def get(self, name, default=None, parser=str, minval=None, maxval=None,
            above=None, below=None):
        value = self._params.get(name)
        if value is None:
            if default is None and name not in self._params:
                return None
            if name not in self._params:
                return default
        try:
            v = parser(value)
        except Exception:
            raise self.error("Unable to parse '%s' in '%s'"
                             % (name, self._commandline))
        if minval is not None and v < minval:
            raise self.error("Error on '%s': %s must have minimum of %s"
                             % (self._commandline, name, minval))
        if maxval is not None and v > maxval:
            raise self.error("Error on '%s': %s must have maximum of %s"
                             % (self._commandline, name, maxval))
        if above is not None and v <= above:
            raise self.error("Error on '%s': %s must be above %s"
                             % (self._commandline, name, above))
        if below is not None and v >= below:
            raise self.error("Error on '%s': %s must be below %s"
                             % (self._commandline, name, below))
        return v

    def get_int(self, name, default=None, minval=None, maxval=None):
        if name not in self._params:
            return default
        return self.get(name, default, parser=int, minval=minval, maxval=maxval)

    def get_float(self, name, default=None, minval=None, maxval=None,
                  above=None, below=None):
        if name not in self._params:
            return default
        return self.get(name, default, parser=float, minval=minval, maxval=maxval,
                        above=above, below=below)


class GCodeDispatch:
    error = CommandError

    def __init__(self, printer, strict=False):
        self.printer = printer
        self.strict = strict
        self.base_commands = {}
        self.mux_commands = {}
        self.gcode_help = {}
        # -- assertion surfaces ------------------------------------------
        self.console = []       # respond_info output
        self.errors = []        # respond_raw output beginning with '!!'
        self.raw = []           # everything respond_raw emitted
        self.executed = []      # every commandline dispatched, in order
        self.unhandled = []     # commandlines with no registered handler

    # -- registration -------------------------------------------------------
    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        if func is None:
            # Return and REMOVE the old handler - the PAUSE/RESUME wrap idiom
            old = self.base_commands.pop(cmd, None)
            self.gcode_help.pop(cmd, None)
            return old
        if cmd in self.base_commands:
            raise self.printer.config_error(
                "gcode command %s already registered" % (cmd,))
        self.base_commands[cmd] = func
        if desc is not None:
            self.gcode_help[cmd] = desc
        return None

    def register_mux_command(self, cmd, key, value, func, desc=None):
        prev = self.mux_commands.setdefault(cmd, (key, {}))
        prev_key, prev_values = prev
        if prev_key != key:
            raise self.printer.config_error(
                "mux command %s %s %s conflicts with %s" % (cmd, key, value, prev_key))
        if value in prev_values:
            raise self.printer.config_error(
                "mux command %s %s %s already registered" % (cmd, key, value))
        prev_values[value] = func
        if desc is not None:
            self.gcode_help[cmd] = desc

    def get_command_help(self):
        return dict(self.gcode_help)

    def register_output_handler(self, cb):
        pass

    # -- output -------------------------------------------------------------
    def respond_info(self, msg, log=True):
        self.console.append(msg)
        if log:
            logging.debug('gcode respond_info: %s', msg)

    def respond_raw(self, msg):
        self.raw.append(msg)
        if msg.startswith('!!'):
            # THE sentinel - see module docstring
            self.errors.append(msg)
            logging.warning('gcode error: %s', msg)

    def respond_error(self, msg):
        self.respond_raw('!! ' + msg)

    # -- execution ----------------------------------------------------------
    def _parse(self, line):
        line = line.strip()
        if not line or line.startswith(';') or line.startswith('#'):
            return None, None, None
        # command word is up to the first whitespace
        parts = line.split(None, 1)
        command = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ''
        params = {}
        for m in _PARAM_RE.finditer(rest):
            key, val = m.group(1).upper(), m.group(2)
            if len(val) >= 2 and val[0] in '"\'' and val[-1] == val[0]:
                val = val[1:-1]
            params[key] = val
        return command, params, line

    def run_script_from_command(self, script):
        self._run(script)

    def run_script(self, script):
        self._run(script)

    def _run(self, script):
        for line in script.split('\n'):
            command, params, raw = self._parse(line)
            if command is None:
                continue
            self.executed.append(raw)
            handler = self.base_commands.get(command)
            if handler is not None:
                handler(GCodeCommand(self, command, raw, params))
                continue
            mux = self.mux_commands.get(command)
            if mux is not None:
                key, values = mux
                keyval = params.get(key)
                func = values.get(keyval, values.get(None))
                if func is not None:
                    func(GCodeCommand(self, command, raw, params))
                    continue
            self.unhandled.append(raw)
            if self.strict:
                raise self.error("Unknown command %r (strict mode)" % (raw,))

    def create_gcode_command(self, command, commandline, params):
        return GCodeCommand(self, command, commandline, params)

    def get_status(self, eventtime=None):
        return {'commands': dict.fromkeys(self.base_commands, None)}


class GCodeIO:
    """Klipper registers this separately; HH never touches it."""

    def __init__(self, printer):
        self.printer = printer


def add_early_printer_objects(printer):
    printer.add_object('gcode', GCodeDispatch(printer))
