# Fake Klipper extras/gcode_move.py. HH reads get_status() (extras/mmu_stepper.py:928)
# and issues SAVE/RESTORE_GCODE_STATE around its sequences.


class GCodeMove:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.absolute_coord = True
        self.absolute_extrude = True
        self.speed = 25.
        self.speed_factor = 1. / 60.
        self.extrude_factor = 1.
        self.base_position = [0., 0., 0., 0.]
        self.last_position = [0., 0., 0., 0.]
        self.homing_position = [0., 0., 0., 0.]
        self.saved_states = {}
        gcode = self.printer.lookup_object('gcode')
        for cmd in ('SAVE_GCODE_STATE', 'RESTORE_GCODE_STATE', 'SET_GCODE_OFFSET',
                    'GET_POSITION'):
            gcode.register_command(cmd, getattr(self, 'cmd_' + cmd))

    def cmd_SAVE_GCODE_STATE(self, gcmd):
        self.saved_states[gcmd.get('NAME', 'default')] = list(self.last_position)

    def cmd_RESTORE_GCODE_STATE(self, gcmd):
        self.saved_states.pop(gcmd.get('NAME', 'default'), None)

    def cmd_SET_GCODE_OFFSET(self, gcmd):
        pass

    def cmd_GET_POSITION(self, gcmd):
        gcmd.respond_info('Position: %s' % (self.last_position,))

    def get_status(self, eventtime=None):
        return {
            'speed_factor': self.speed_factor,
            'speed': self.speed,
            'extrude_factor': self.extrude_factor,
            'absolute_coordinates': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            'homing_origin': tuple(self.homing_position),
            'position': tuple(self.last_position),
            'gcode_position': tuple(self.last_position),
        }


def load_config(config):
    return GCodeMove(config)
