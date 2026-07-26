# Fake Klipper extras/display_status.py. load_object'ed by extras/mmu_led_effect.py:209
# and read at :263.


class DisplayStatus:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.expire_time = None
        self.message = None
        self.progress = 0.
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('M117', self.cmd_M117)
        gcode.register_command('M73', self.cmd_M73)

    def cmd_M117(self, gcmd):
        self.message = gcmd.get_raw_command_parameters() or None

    def cmd_M73(self, gcmd):
        self.progress = gcmd.get_float('P', 0.) / 100.

    def get_status(self, eventtime=None):
        return {'message': self.message or '', 'progress': self.progress}


def load_config(config):
    return DisplayStatus(config)
