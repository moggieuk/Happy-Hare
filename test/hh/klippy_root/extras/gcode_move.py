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
                    'GET_POSITION', 'G0', 'G1', 'G2', 'G3', 'G90', 'G91', 'G92',
                    'M82', 'M83', 'M220', 'M221'):
            gcode.register_command(cmd, getattr(self, 'cmd_' + cmd))

    # -- moves ---------------------------------------------------------------
    # Ported from klippy/extras/gcode_move.py. Moves go through the transform chain
    # (skew_correction) to the toolhead, which is what lets a test see the final kinematic
    # position. E is applied as a DELTA to the toolhead's own extruder position: Happy Hare
    # moves the extruder outside gcode_move, and real Klipper resyncs last_position on those
    # moves (toolhead:manual_move) rather than replaying the gap. The toolhead reports the
    # delta to the filament model (test/hh/bootstrap.py:_on_toolhead_move).
    def set_move_transform(self, transform, force=False):
        if self.move_transform is not None and not force:
            raise self.printer.config_error("G-Code move transform already specified")
        old = self.move_transform or self.printer.lookup_object('toolhead')
        self.move_transform = transform
        return old

    def _move_with_transform(self, speed, e_delta=0.):
        toolhead = self.printer.lookup_object('toolhead')
        target = self.move_transform or toolhead
        extra = toolhead.get_position()[3:]
        extra[0] += e_delta
        target.move(list(self.last_position[:3]) + extra, speed)

    def cmd_G1(self, gcmd):
        params = gcmd.get_command_parameters()
        previous = list(self.last_position)
        moved = False
        try:
            for pos, axis in enumerate('XYZE'):
                if axis in params:
                    v = float(params[axis])
                    absolute_coord = self.absolute_coord
                    if axis == 'E':
                        v *= self.extrude_factor
                        if not self.absolute_extrude:
                            absolute_coord = False
                    if absolute_coord:
                        self.last_position[pos] = v + self.base_position[pos]
                    else:
                        self.last_position[pos] += v
                    moved = True
            if 'F' in params:
                gcode_speed = float(params['F'])
                if gcode_speed <= 0.:
                    raise gcmd.error("Invalid speed in '%s'" % (gcmd.get_commandline(),))
                self.speed = gcode_speed * self.speed_factor
        except ValueError:
            self.last_position = previous
            raise gcmd.error("Unable to parse move '%s'" % (gcmd.get_commandline(),))
        if moved:
            try:
                self._move_with_transform(self.speed, self.last_position[3] - previous[3])
            except Exception:
                # Klipper resets last_position on gcode:command_error
                self.last_position = previous
                raise

    # Arcs: the same endpoint and extrusion as a straight line, which is all that is modelled
    cmd_G0 = cmd_G2 = cmd_G3 = cmd_G1

    def cmd_G90(self, gcmd):
        self.absolute_coord = True

    def cmd_G91(self, gcmd):
        self.absolute_coord = False

    def cmd_M82(self, gcmd):
        self.absolute_extrude = True

    def cmd_M83(self, gcmd):
        self.absolute_extrude = False

    def cmd_G92(self, gcmd):
        offsets = [gcmd.get_float(a, None) for a in 'XYZE']
        for i, offset in enumerate(offsets):
            if offset is not None:
                if i == 3:
                    offset *= self.extrude_factor
                self.base_position[i] = self.last_position[i] - offset
        if offsets == [None, None, None, None]:
            self.base_position[:4] = self.last_position[:4]

    def cmd_M220(self, gcmd):
        value = gcmd.get_float('S', 100., above=0.) / (60. * 100.)
        self.speed = self.speed / self.speed_factor * value
        self.speed_factor = value

    def cmd_M221(self, gcmd):
        new_extrude_factor = gcmd.get_float('S', 100., above=0.) / 100.
        last_e_pos = self.last_position[3]
        e_value = (last_e_pos - self.base_position[3]) / self.extrude_factor
        self.base_position[3] = last_e_pos - e_value * new_extrude_factor
        self.extrude_factor = new_extrude_factor

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
        e_diff = self.last_position[3]-state['last_position'][3]
        self.base_position = list(state['base_position'])
        # Restore the G-code origin, not physical extrusion already performed.
        self.base_position[3] += e_diff
        self.homing_position = list(state['homing_position'])
        self.speed = state['speed']
        self.speed_factor = state['speed_factor']
        self.extrude_factor = state['extrude_factor']
        if gcmd.get_int('MOVE', 0):
            self.last_position[:3] = state['last_position'][:3]
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
            # As Klipper reports them: the override as a multiplier and the speed in gcode
            # units. HH turns the reported factor back into the internal one by dividing by
            # 60 (extras/mmu/mmu_controller.py:2880), so reporting the raw 1/60 here made
            # every in-print toolchange slow the print down 60x
            'speed_factor': self.speed_factor * 60.,
            'speed': self.speed / self.speed_factor,
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
