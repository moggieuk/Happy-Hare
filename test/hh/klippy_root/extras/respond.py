# Fake Klipper extras/respond.py - M118 / RESPOND. HH issues M118 as a console
# fallback; recording is enough.
#
# A web UI bridge (test/hh/webui_bridge.py) sets `host_listener`: RESPOND TYPE=command is then
# relayed to it the way Klipper's `// ` lines reach Moonraker, and kept out of the console text.


class HostResponder:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.messages = []
        self.host_listener = None   # callable(line) for '// ' host commands, e.g. action:prompt_*
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('M118', self.cmd_M118)
        gcode.register_command('RESPOND', self.cmd_RESPOND)

    def cmd_M118(self, gcmd):
        msg = gcmd.get_raw_command_parameters()
        self.messages.append(msg)
        gcmd.respond_info(msg)

    def cmd_RESPOND(self, gcmd):
        msg = gcmd.get('MSG', '')
        self.messages.append(msg)
        if self.host_listener is not None and gcmd.get('TYPE', 'echo') == 'command':
            self.host_listener('// ' + msg)
            return
        gcmd.respond_info(msg)


def load_config(config):
    return HostResponder(config)
