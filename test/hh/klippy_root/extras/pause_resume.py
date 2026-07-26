# Fake Klipper extras/pause_resume.py.
#
# [pause_resume] is a HARD requirement: MmuController.handle_connect raises if it is
# missing (extras/mmu/mmu_controller.py:194-196).
#
# It must also really REGISTER PAUSE/RESUME/CLEAR_PAUSE/CANCEL_PRINT. HH wraps all
# four via the register_command(name, None) return-and-remove idiom
# (extras/mmu/mmu_controller.py:243-252) and log_error()s "No existing X macro
# found!" for each one it cannot find - which the error sentinel would (correctly)
# turn a bootup test red for.


class PauseResume:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.recover_velocity = config.getfloat('recover_velocity', 50.)
        self.is_paused = False
        self.pause_calls = 0
        self.resume_calls = 0
        gcode = self.printer.lookup_object('gcode')
        for cmd in ('PAUSE', 'RESUME', 'CLEAR_PAUSE', 'CANCEL_PRINT'):
            gcode.register_command(cmd, getattr(self, 'cmd_' + cmd),
                                   desc='Fake %s' % cmd)

    def cmd_PAUSE(self, gcmd):
        self.send_pause_command()

    def cmd_RESUME(self, gcmd):
        self.send_resume_command()

    def cmd_CLEAR_PAUSE(self, gcmd):
        self.is_paused = False

    def cmd_CANCEL_PRINT(self, gcmd):
        self.is_paused = False

    def send_pause_command(self):
        self.is_paused = True
        self.pause_calls += 1

    def send_resume_command(self):
        self.is_paused = False
        self.resume_calls += 1

    def get_status(self, eventtime=None):
        return {'is_paused': self.is_paused}


def load_config(config):
    return PauseResume(config)
