# Fake Klipper `klippy/extras/force_move.py` for the Happy Hare test harness.
#
# Only calc_move_time is used (extras/mmu_stepper.py:857) and it is pure
# arithmetic, so it is ported VERBATIM from Klipper rather than stubbed - there is
# no behaviour to fake and a divergence here would silently change move timing.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import math


def calc_move_time(dist, speed, accel):
    axis_r = 1.
    if dist < 0.:
        axis_r = -1.
        dist = -dist
    if not accel or not dist:
        return axis_r, 0., dist / speed, speed
    max_cruise_v2 = dist * accel
    if max_cruise_v2 < speed**2:
        speed = math.sqrt(max_cruise_v2)
    accel_t = speed / accel
    accel_decel_d = accel_t * speed
    cruise_t = (dist - accel_decel_d) / speed
    return axis_r, accel_t, cruise_t, speed
