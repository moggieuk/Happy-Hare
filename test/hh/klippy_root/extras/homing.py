# Fake Klipper `klippy/extras/homing.py` for the Happy Hare test harness.
#
# This module must EXIST even for configs that never home, because
# extras/mmu/unit/selectors/__init__.py pkgutil-imports every selector module and
# mmu_linear_servo_selector.py:35 does `from ....homing import Homing, HomingMove`
# at module scope. So a missing symbol here breaks importing HH at all.
#
# Callers:
#   extras/mmu_stepper.py:427,443  HomingMove(printer, endstops, stepper)
#                                  .homing_move(homepos, speed)
#                                  .check_no_movement()
#                                  .stepper_positions  -> [.stepper, .halt_pos, .trig_pos]
#   extras/mmu_stepper.py:884      phoming.manual_home(...)
#   extras/mmu/mmu_filament_movement.py:39  HomingMove
#
# At the config-load/bootup tier the bodies raise: nothing should be homing yet,
# and a loud failure is better than a silent no-move. Real behaviour arrives with
# the filament-path model (harness plan, phase C), which drives the endstops
# through incremental motion and completes the first trip.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

HOMING_START_DELAY = 0.001
ENDSTOP_SAMPLE_TIME = .000015
ENDSTOP_SAMPLE_COUNT = 4


class StepperPosition:
    def __init__(self, stepper, endstop_name):
        self.stepper = stepper
        self.endstop_name = endstop_name
        self.stepper_name = stepper.get_name()
        self.start_pos = stepper.get_mcu_position()
        self.start_cmd_pos = stepper.mcu_to_commanded_position(self.start_pos)
        self.halt_pos = self.trig_pos = None

    def note_home_end(self, trigger_time):
        self.halt_pos = self.trig_pos = self.stepper.get_mcu_position()


class HomingMove:
    def __init__(self, printer, endstops, toolhead=None):
        self.printer = printer
        self.endstops = endstops
        self.toolhead = toolhead
        self.stepper_positions = []

    def get_mcu_endstops(self):
        return [es for es, _name in self.endstops]

    def calc_toolhead_pos(self, kin_spos, offsets):
        raise NotImplementedError(_msg('HomingMove.calc_toolhead_pos'))

    def homing_move(self, movepos, speed, probe_pos=False,
                    triggered=True, check_triggered=True):
        raise NotImplementedError(_msg('HomingMove.homing_move'))

    def check_no_movement(self):
        raise NotImplementedError(_msg('HomingMove.check_no_movement'))


class Homing:
    def __init__(self, printer):
        self.printer = printer
        self.toolhead = printer.lookup_object('toolhead', None)
        self.changed_axes = []

    def set_axes(self, axes):
        self.changed_axes = axes

    def get_axes(self):
        return self.changed_axes

    def home_rails(self, rails, forcepos, movepos):
        raise NotImplementedError(_msg('Homing.home_rails'))


class PrinterHoming:
    def __init__(self, config):
        self.printer = config.get_printer()

    def manual_home(self, toolhead, endstops, pos, speed,
                    triggered=True, check_triggered=True):
        raise NotImplementedError(_msg('PrinterHoming.manual_home'))

    def probing_move(self, mcu_probe, pos, speed):
        raise NotImplementedError(_msg('PrinterHoming.probing_move'))


def _msg(what):
    return ("%s is not implemented at this harness tier. Homing needs the filament-"
            "path model (harness plan, phase C) so an endstop actually trips; until "
            "then a config-load/bootup test should not be homing. If this fired "
            "during bootup, check the profile uses VirtualSelector - a physical "
            "selector autohomes at extras/mmu/mmu_controller.py:385-405." % (what,))
