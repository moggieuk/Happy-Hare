# Fake Klipper extras/respond.py - M118 / RESPOND. HH issues M118 as a console
# fallback; recording is enough.
#
# RESPOND TYPE=command is sent as a raw '// ' line, as Klipper does, so output handlers (Happy Hare's
# dialog, a web UI bridge) see host commands such as a UI's 'action:prompt_end'.


class HostResponder:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.messages = []
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
        if gcmd.get('TYPE', 'echo').lower() == 'command':
            gcmd.respond_raw('// ' + msg)
            return
        gcmd.respond_info(msg)


def load_config(config):
    return HostResponder(config)
