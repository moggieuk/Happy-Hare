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
        self.cancel_calls = 0                       # a playing /print job stops on these
        gcode = self.printer.lookup_object('gcode')
        for cmd in ('PAUSE', 'RESUME', 'CLEAR_PAUSE', 'CANCEL_PRINT'):
            gcode.register_command(cmd, getattr(self, 'cmd_' + cmd),
                                   desc='Fake %s' % cmd)

    # Like klippy/extras/pause_resume.py, PAUSE saves PAUSE_STATE and RESUME moves back to it
    def cmd_PAUSE(self, gcmd):
        if self.is_paused:
            return
        self.send_pause_command()
        self.printer.lookup_object('gcode').run_script_from_command("SAVE_GCODE_STATE NAME=PAUSE_STATE")

    def cmd_RESUME(self, gcmd):
        if not self.is_paused:
            return
        velocity = gcmd.get_float('VELOCITY', self.recover_velocity)
        self.printer.lookup_object('gcode').run_script_from_command(
            "RESTORE_GCODE_STATE NAME=PAUSE_STATE MOVE=1 MOVE_SPEED=%.4f" % velocity)
        self.send_resume_command()

    def cmd_CLEAR_PAUSE(self, gcmd):
        self.is_paused = False

    def cmd_CANCEL_PRINT(self, gcmd):
        self.is_paused = False
        self.cancel_calls += 1

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
