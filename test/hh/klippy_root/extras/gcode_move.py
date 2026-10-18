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

import collections

Coord = collections.namedtuple('Coord', ('x', 'y', 'z', 'e'))


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
        self.move_transform = None
        gcode = self.printer.lookup_object('gcode')
        for cmd in ('SAVE_GCODE_STATE', 'RESTORE_GCODE_STATE', 'SET_GCODE_OFFSET',
                    'GET_POSITION', 'G0', 'G1', 'G90', 'G91'):
            gcode.register_command(cmd, getattr(self, 'cmd_' + cmd))

    # -- moves ---------------------------------------------------------------
    # XYZ only: E is ignored so Python-issued `G1 E..` stays a no-op, as it was when
    # G1 was unhandled. Moves go through the transform chain (skew_correction) to the
    # toolhead, which is what lets a test see the final kinematic position.
    def set_move_transform(self, transform, force=False):
        if self.move_transform is not None and not force:
            raise self.printer.config_error("G-Code move transform already specified")
        old = self.move_transform or self.printer.lookup_object('toolhead')
        self.move_transform = transform
        return old

    def _move_with_transform(self, speed):
        toolhead = self.printer.lookup_object('toolhead')
        target = self.move_transform or toolhead
        target.move(list(self.last_position[:3]) + toolhead.get_position()[3:], speed)

    def cmd_G1(self, gcmd):
        params = gcmd.get_command_parameters()
        moved = False
        for pos, axis in enumerate('XYZ'):
            if axis in params:
                v = float(params[axis])
                if self.absolute_coord:
                    self.last_position[pos] = v + self.base_position[pos]
                else:
                    self.last_position[pos] += v
                moved = True
        if 'F' in params:
            self.speed = float(params['F']) * self.speed_factor
        if moved:
            self._move_with_transform(self.speed)

    cmd_G0 = cmd_G1

    def cmd_G90(self, gcmd):
        self.absolute_coord = True

    def cmd_G91(self, gcmd):
        self.absolute_coord = False

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
        if gcmd.get_int('MOVE', 0):
            self.speed = gcmd.get_float('MOVE_SPEED', self.speed * 60.) / 60.
            self._move_with_transform(self.speed)

    def cmd_SET_GCODE_OFFSET(self, gcmd):
        # Ported from klippy/extras/gcode_move.py (MOVE= not supported)
        for pos, axis in enumerate('XYZ'):
            offset = gcmd.get_float(axis, None)
            if offset is None:
                offset = gcmd.get_float(axis + '_ADJUST', None)
                if offset is None:
                    continue
                offset += self.homing_position[pos]
            delta = offset - self.homing_position[pos]
            self.base_position[pos] += delta
            self.homing_position[pos] = offset

    def cmd_GET_POSITION(self, gcmd):
        gcmd.respond_info('Position: %s' % (self.last_position,))

    def get_status(self, eventtime=None):
        return {
            'speed_factor': self.speed_factor,
            'speed': self.speed,
            'extrude_factor': self.extrude_factor,
            'absolute_coordinates': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            'homing_origin': Coord(*self.homing_position[:4]),
            'position': Coord(*self.last_position[:4]),
            'gcode_position': Coord(*[lp - bp for lp, bp in
                                      zip(self.last_position[:4], self.base_position[:4])]),
        }


def load_config(config):
    return GCodeMove(config)
