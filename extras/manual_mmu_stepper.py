# Happy Hare MMU Software
# Support for a manual stepper that can be configured with multiple endstops
#
# Copyright (C) 2023  Cambridge Yang <camyang@csail.mit.edu>
#                     moggieuk#6538 (discord) moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import stepper, chelper, logging
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
            self.endstops = [(self.MockEndstop(), "")] # Hack: pretend we have endstops
        return super(PrinterRailWithMockEndstop, self).add_extra_stepper(*args, **kwargs)


class ManualMmuStepper(manual_stepper.ManualStepper, object):
    """Manual stepper that can have multiple separately controlled endstops (only one active at a time)"""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.mcu_endstops = {}
        self.can_home = True

        if config.get('endstop_pin', None) is not None:
            self.rail = stepper.PrinterRail(config, need_position_minmax=False, default_position_endstop=0.)
        else:
            self.rail = PrinterRailWithMockEndstop(config, need_position_minmax=False, default_position_endstop=0.)
        self.steppers = self.rail.get_steppers()
        self.default_endstop = self.rail.endstops

        # Setup default endstop
        self.query_endstops = self.printer.load_object(config, 'query_endstops')
        endstop_pin = config.get('endstop_pin', None)
        if endstop_pin is not None:
            self.mcu_endstops['default']={'mcu_endstop': self.default_endstop[0], 'virtual': "virtual_endstop" in endstop_pin}
            # Vanity rename of default endstop in query_endstops
            endstop_pin_name = config.get('endstop_pin_name', None)
            if endstop_pin_name is not None:
                for idx, es in enumerate(self.query_endstops.endstops):
                    if es[1] == self.default_endstop[0][1]:
                        self.query_endstops.endstops[idx] = (self.default_endstop[0][0], endstop_pin_name)
                        # Also add vanity name so we can lookup
                        self.mcu_endstops[endstop_pin_name]={'mcu_endstop': self.default_endstop[0], 'virtual': "virtual_endstop" in endstop_pin}
                        break
        # Handle any extra endstops
        extra_endstop_pins = config.getlist('extra_endstop_pins', [])
        extra_endstop_names = config.getlist('extra_endstop_names', [])
        if extra_endstop_pins:
            if len(extra_endstop_pins) != len(extra_endstop_names):
                raise self.config.error("`extra_endstop_pins` and `extra_endstop_names` are different lengths")
            for idx, pin in enumerate(extra_endstop_pins):
                name = extra_endstop_names[idx]
                self._add_endstop(config, pin, name)

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

        logging.info("PAUL: endstop_names=%s" % self.get_endstop_names()) # PAUL temp

    def _add_endstop(self, config, pin, name):
        ppins = self.printer.lookup_object('pins')
#        logging.info("PAUL: Allowing multiuse of pin: %s" % pin)
#        ppins.allow_multi_use_pin(pin) # Always allow reuse of `extra_endstop_pins`
        mcu_endstop = ppins.setup_pin('endstop', pin)
        for s in self.steppers:
            mcu_endstop.add_stepper(s)
        self.query_endstops.register_endstop(mcu_endstop, name)
        self.mcu_endstops[name]={'mcu_endstop': mcu_endstop, 'virtual': "virtual_endstop" in pin}
        return mcu_endstop

    def get_endstop_names(self):
        return self.mcu_endstops.keys()

    def activate_endstop(self, name):
        endstop = self.mcu_endstops.get(name)
        if name is not None:
            logging.info("PAUL: Activating endstop: name=%s" % name)
            self.rail.endstops = [(endstop['mcu_endstop'], name)]
        else:
            logging.info("PAUL: Restoring default endstop")
            self.rail.endstops = self.default_endstop

    def get_endstop(self, name):
        endstop = self.mcu_endstops.get(name)
        if endstop is not None:
            return endstop['mcu_endstop']
        return None

    def is_endstop_virtual(self, name):
        endstop = self.mcu_endstops.get(name)
        if endstop is not None:
            return endstop['virtual']

def load_config_prefix(config):
    return ManualMmuStepper(config)

