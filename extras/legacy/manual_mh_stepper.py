# Happy Hare MMU Software
# Support for a manual stepper that can be configured with multiple endstops (multi-homed)
#
# Copyright (C) 2023  moggieuk#6538 (discord) moggieuk@hotmail.com
#                     Cambridge Yang <camyang@csail.mit.edu>
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import stepper, chelper, logging, contextlib
from . import manual_stepper


class PrinterRailWithMockEndstop(stepper.PrinterRail, object):
    """PrinterRail that pretends to have an endstop during the initial setup phase.
    The rail is only homable if it has a properly configured endstop at runtime"""

    class MockEndstop:
        def add_stepper(self, *args, **kwargs):
            pass

    def __init__(self, *args, **kwargs):
        self._in_setup = True
        super(PrinterRailWithMockEndstop, self).__init__(*args, **kwargs)
        self.endstops = []

    def add_extra_stepper(self, *args, **kwargs):
        if self._in_setup:
            self.endstops = [(self.MockEndstop(), "mock")] # Hack: pretend we have endstops
        return super(PrinterRailWithMockEndstop, self).add_extra_stepper(*args, **kwargs)


class ManualMhStepper(manual_stepper.ManualStepper, object):
    """Manual stepper that can have multiple separately controlled endstops (only one active at a time)"""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.config_name = config.get_name()
        self.name = config.get_name().split()[-1]
        self.mcu_endstops = {}
        self.can_home = True

        if config.get('endstop_pin', None) is not None:
            self.rail = stepper.PrinterRail(config, need_position_minmax=False, default_position_endstop=0.)
        else:
            self.rail = PrinterRailWithMockEndstop(config, need_position_minmax=False, default_position_endstop=0.)
        self.steppers = self.rail.get_steppers()
        self.default_endstops = self.rail.endstops
        self.stepper = self.steppers[0]

        # Setup default endstop
        self.query_endstops = self.printer.load_object(config, 'query_endstops')
        endstop_pin = config.get('endstop_pin', None)
        if endstop_pin is not None:
            self.mcu_endstops['default']={'mcu_endstop': self.default_endstops[0], 'virtual': "virtual_endstop" in endstop_pin}
            # Vanity rename of default endstop in query_endstops
            endstop_name = config.get('endstop_name', None)
            if endstop_name is not None:
                for idx, es in enumerate(self.query_endstops.endstops):
                    if es[1] == self.default_endstops[0][1]:
                        self.query_endstops.endstops[idx] = (self.default_endstops[0][0], endstop_name)
                        # Also add vanity name so we can lookup
                        self.mcu_endstops[endstop_name.lower()]={'mcu_endstop': self.default_endstops[0], 'virtual': "virtual_endstop" in endstop_pin}
                        break

        # Handle any extra endstops
        extra_endstop_pins = config.getlist('extra_endstop_pins', [])
        extra_endstop_names = config.getlist('extra_endstop_names', [])
        if extra_endstop_pins:
            if len(extra_endstop_pins) != len(extra_endstop_names):
                raise self.config.error("`extra_endstop_pins` and `extra_endstop_names` are different lengths")
            for idx, pin in enumerate(extra_endstop_pins):
                name = extra_endstop_names[idx]
                self._add_endstop(pin, name)

        self.velocity = config.getfloat('velocity', 5., above=0.)
        self.accel = self.homing_accel = config.getfloat('accel', 0., minval=0.)
        self.next_cmd_time = 0.

        # Setup iterative solver
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.rail.setup_itersolve('cartesian_stepper_alloc', b'x')
        self.rail.set_trapq(self.trapq)

        # Register commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command('MANUAL_STEPPER', "STEPPER",
                                   self.name, self.cmd_MANUAL_STEPPER,
                                   desc=self.cmd_MANUAL_STEPPER_help)
        gcode.register_mux_command('DUMP_MANUAL_STEPPER', "STEPPER",
                                   self.name, self.cmd_DUMP_MANUAL_STEPPER,
                                   desc=self.cmd_DUMP_MANUAL_STEPPER_help)

    def _add_endstop(self, pin, name, register=True):
        ppins = self.printer.lookup_object('pins')
        mcu_endstop = ppins.setup_pin('endstop', pin)
        for s in self.steppers:
            mcu_endstop.add_stepper(s)
        if register:
            self.query_endstops.register_endstop(mcu_endstop, name)
        self.mcu_endstops[name.lower()]={'mcu_endstop': (mcu_endstop, self.config_name), 'virtual': "virtual_endstop" in pin}
        return mcu_endstop

    def get_endstop_names(self):
        return self.mcu_endstops.keys()

    def activate_endstop(self, name):
        current_endstop_name = "default"
        if len(self.rail.endstops) > 0:
            current_mcu_endstop, stepper_name = self.rail.endstops[0]
            for i in self.mcu_endstops:
                if self.mcu_endstops[i]['mcu_endstop'][0] == current_mcu_endstop:
                    current_endstop_name = i
                    break
        endstop = self.mcu_endstops.get(name.lower())
        if endstop is not None:
            self.rail.endstops = [endstop['mcu_endstop']]
        else:
            self.rail.endstops = self.default_endstops
        return current_endstop_name

    def get_endstop(self, name):
        endstop = self.mcu_endstops.get(name.lower())
        if endstop is not None:
            return endstop['mcu_endstop'][0]
        return None

    def is_endstop_virtual(self, name):
        endstop = self.mcu_endstops.get(name.lower())
        if endstop is not None:
            return endstop['virtual']
        else:
            return False

    cmd_MANUAL_STEPPER_help = "Command a manually configured stepper"
    def cmd_MANUAL_STEPPER(self, gcmd):
        endstop_name = gcmd.get('ENDSTOP', "default") # Added
        enable = gcmd.get_int('ENABLE', None)
        if enable is not None:
            super(ManualMhStepper, self).do_enable(enable)
        setpos = gcmd.get_float('SET_POSITION', None)
        if setpos is not None:
            super(ManualMhStepper, self).do_set_position(setpos)
        speed = gcmd.get_float('SPEED', self.velocity, above=0.)
        accel = gcmd.get_float('ACCEL', self.accel, minval=0.)
        homing_move = gcmd.get_int('STOP_ON_ENDSTOP', 0)
        if homing_move:
            movepos = gcmd.get_float('MOVE')
            self.do_mh_homing_move(movepos, speed, accel, homing_move > 0, abs(homing_move) == 1, endstop_name)
        elif gcmd.get_float('MOVE', None) is not None:
            movepos = gcmd.get_float('MOVE')
            sync = gcmd.get_int('SYNC', 1)
            super(ManualMhStepper, self).do_move(movepos, speed, accel, sync)
        elif gcmd.get_int('SYNC', 0):
            super(ManualMhStepper, self).sync_print_time()

    cmd_DUMP_MANUAL_STEPPER_help = "For debugging: dump configuration of multi-homed stepper"
    def cmd_DUMP_MANUAL_STEPPER(self, gcmd):
        msg = self.dump_manual_stepper()
        gcmd.respond_raw(msg)

    def dump_manual_stepper(self):
        msg = "Class: %s\n" % self.__class__.__name__
        msg += "Rail:\n"
        msg += "- Num steppers: %d\n" % len(self.rail.steppers)
        msg += "- Num active endstops: %d\n" % len(self.rail.endstops)
        msg += "Steppers:\n"
        for s in self.get_steppers():
            msg += "- Stepper: %s\n" % s.get_name()
            msg += "- - Commanded Position: %.1f\n" % s.get_commanded_position()
            msg += "- - MCU Position: %.1f\n" % s.get_mcu_position()
        msg += "Endstops:\n"
        for (mcu_endstop, name) in self.rail.get_endstops():
            msg += "- Name: '%s', mcu: '%s', pin: '%s', obj_id: %s\n" % (name, mcu_endstop.get_mcu().get_name(), mcu_endstop._pin, id(mcu_endstop))
            for idx2, s in enumerate(mcu_endstop.get_steppers()):
                msg += "- - Stepper %d: '%s'\n" % (idx2, s.get_name())
        msg += "Registered (alternate) Endstops:\n"
        for idx, es in enumerate(self.get_endstop_names()):
            endstop = self.mcu_endstops[es]
            (mcu_endstop, name) = endstop['mcu_endstop']
            msg += "- %d '%s', Name: '%s', mcu: '%s', pin: '%s', obj_id: %s" % (idx+1, es, name, mcu_endstop.get_mcu().get_name(), mcu_endstop._pin, id(mcu_endstop))
            msg += " (virtual)\n" if endstop['virtual'] else "\n"
            for idx2, s in enumerate(mcu_endstop.get_steppers()):
                msg += "- - Stepper %d: '%s'\n" % (idx2, s.get_name())
        if self.__class__.__name__ == "ManualExtruderStepper" and self.is_synced():
            msg += "Synced to extruder '%s'" % self.synced_extruder_name
        return msg

    @contextlib.contextmanager
    def _with_endstop(self, endstop_name=None):
        prev_endstop_name = None
        if endstop_name:
            prev_endstop_name = self.activate_endstop(endstop_name)

        # Yield to caller
        try:
            yield self

        finally:
            # Restore previous endstop if changed
            if prev_endstop_name:
                self.activate_endstop(prev_endstop_name)

    # Perform homing move using specified endstop
    def do_mh_homing_move(self, movepos, speed, accel, triggered=True, check_trigger=True, endstop_name=None):
        with self._with_endstop(endstop_name):
            if len(self.rail.endstops) > 0:
                super(ManualMhStepper, self).do_homing_move(movepos, speed, accel, triggered, check_trigger)
            else:
                raise self.printer.command_error("No active endstops for this manual multi-home stepper")

def load_config_prefix(config):
    return ManualMhStepper(config)
