# Fake Klipper extras/gcode_move.py. HH reads get_status() (extras/mmu_stepper.py:928)
# and issues SAVE/RESTORE_GCODE_STATE around its sequences.
#
# saved_states entries MUST be Klipper's state DICT, not just a position. Happy Hare reads
# and mutates the contents directly:
#     saved_states[TOOLHEAD_POSITION_STATE]['last_position'][:2] = next_pos
#     mmu_state['speed_factor'] * 60   /   mmu_state['extrude_factor']
# (extras/mmu/mmu_controller.py:2129-2136). Storing a bare list here made MMU_LOAD crash
# with "list indices must be integers or slices, not str". Shape ported verbatim from
# klippy/extras/gcode_move.py:226-234.


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
        self.saved_states[gcmd.get('NAME', 'default')] = {
            'absolute_coord': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            'base_position': list(self.base_position),
            'last_position': list(self.last_position),
            'homing_position': list(self.homing_position),
            'speed': self.speed,
            'speed_factor': self.speed_factor,
            'extrude_factor': self.extrude_factor,
        }

    def cmd_RESTORE_GCODE_STATE(self, gcmd):
        name = gcmd.get('NAME', 'default')
        state = self.saved_states.get(name)
        if state is None:
            raise gcmd.error('Unknown g-code state: %s' % (name,))
        self.absolute_coord = state['absolute_coord']
        self.absolute_extrude = state['absolute_extrude']
        self.base_position = list(state['base_position'])
        self.last_position = list(state['last_position'])
        self.homing_position = list(state['homing_position'])
        self.speed = state['speed']
        self.speed_factor = state['speed_factor']
        self.extrude_factor = state['extrude_factor']

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
