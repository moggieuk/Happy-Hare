# Fake Klipper extras/skew_correction.py. The skew math is copied verbatim from
# klippy/extras/skew_correction.py (calc_skew / calc_unskew) and the object installs
# itself as a gcode move transform, so a G1 lands at the same kinematic position as on
# a real printer. Tests set the live factors directly and add stored profiles with
# add_profile(); SKEW_PROFILE itself is not modelled.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class PrinterSkew:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.current_profile_name = ""
        self.xy_factor = 0.
        self.xz_factor = 0.
        self.yz_factor = 0.
        self.skew_profiles = {}
        self.next_transform = None
        self.printer.register_event_handler("klippy:connect", self._handle_connect)

    def _handle_connect(self):
        gcode_move = self.printer.lookup_object('gcode_move')
        self.next_transform = gcode_move.set_move_transform(self, force=True)

    def calc_skew(self, pos):
        skewed_x = pos[0] - pos[1] * self.xy_factor \
            - pos[2] * (self.xz_factor - (self.xy_factor * self.yz_factor))
        skewed_y = pos[1] - pos[2] * self.yz_factor
        return [skewed_x, skewed_y] + pos[2:]

    def calc_unskew(self, pos):
        skewed_x = pos[0] + pos[1] * self.xy_factor \
            + pos[2] * self.xz_factor
        skewed_y = pos[1] + pos[2] * self.yz_factor
        return [skewed_x, skewed_y] + pos[2:]

    def get_position(self):
        return self.calc_unskew(self.next_transform.get_position())

    def move(self, newpos, speed):
        self.next_transform.move(self.calc_skew(newpos), speed)

    def set_factors(self, xy=0., xz=0., yz=0., profile="test"):
        self.xy_factor, self.xz_factor, self.yz_factor = xy, xz, yz
        self.current_profile_name = profile

    def add_profile(self, name, xy=0., xz=0., yz=0.):
        self.skew_profiles[name] = {'xy_skew': xy, 'xz_skew': xz, 'yz_skew': yz}

    def get_status(self, eventtime=None):
        return {'current_profile_name': self.current_profile_name}


def load_config(config):
    return PrinterSkew(config)
