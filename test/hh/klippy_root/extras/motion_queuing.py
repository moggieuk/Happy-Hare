# Fake Klipper `klippy/extras/motion_queuing.py` for the Happy Hare test harness.
#
# Worth being explicit about why this is a fake and not a shortcut: this module does
# NOT EXIST in mainline Klipper v0.13.0-111, yet extras/mmu_stepper.py:542-544
# requires it (printer.load_object(config, 'motion_queuing') then allocate_trapq() /
# lookup_trapq_append()). It only appears in a much newer checkout. So on mainline
# there is no "use real Klipper" option for this surface at all - the fake is the
# only way to load an MmuStepper. If the optional real-Klipper tier is ever built it
# must pin the newer checkout.
#
# Surface used by HH, with call counts: check_step_generation_scan_windows x16,
# allocate_trapq x6, lookup_trapq_append x6, note_mcu_movequeue_activity x3,
# drip_update_time x1, wipe_trapq x1.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class Trapq:
    """List-backed stand-in for the chelper trapq struct."""

    def __init__(self, index):
        self.index = index
        self.moves = []

    def __repr__(self):
        return 'Trapq(%d, %d moves)' % (self.index, len(self.moves))


class PrinterMotionQueuing:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.trapqs = []
        self.scan_window_checks = 0
        self.movequeue_activity = []

    def allocate_trapq(self):
        tq = Trapq(len(self.trapqs))
        self.trapqs.append(tq)
        return tq

    def wipe_trapq(self, trapq):
        trapq.moves = []

    def lookup_trapq_append(self):
        def trapq_append(trapq, print_time, accel_t, cruise_t, decel_t,
                         start_pos_x, start_pos_y, start_pos_z,
                         axes_r_x, axes_r_y, axes_r_z,
                         start_v, cruise_v, accel):
            trapq.moves.append({
                'print_time': print_time,
                'accel_t': accel_t, 'cruise_t': cruise_t, 'decel_t': decel_t,
                'start_pos': (start_pos_x, start_pos_y, start_pos_z),
                'axes_r': (axes_r_x, axes_r_y, axes_r_z),
                'start_v': start_v, 'cruise_v': cruise_v, 'accel': accel,
            })
        return trapq_append

    def check_step_generation_scan_windows(self):
        self.scan_window_checks += 1

    def note_mcu_movequeue_activity(self, mq_time, set_step_gen_time=False):
        self.movequeue_activity.append(mq_time)

    def drip_update_time(self, next_print_time, drip_completion):
        return next_print_time

    def check_drip_timing(self, next_print_time, drip_completion):
        return next_print_time

    def setup_mcu_movequeue(self, mcu):
        pass

    def allocate_syncemitter(self, *args, **kwargs):
        return None

    def flush_moves(self, print_time, clear_history_time=0.):
        pass

    def get_status(self, eventtime=None):
        return {'trapqs': len(self.trapqs)}


def load_config(config):
    return PrinterMotionQueuing(config)
