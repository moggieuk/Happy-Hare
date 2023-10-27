# Happy Hare MMU Software
# Implementation of "MMU Toolhead" to allow for:
#   - "drip" homing and movement without pauses
#   - bi-directional syncing of extruder to gear rail or gear rail to extruder
#   - extra "standby" endstops
#   - extruder endstops
#
# Copyright (C) 2023  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# Based heavily on code by Kevin O'Connor <kevin@koconnor.net>
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, importlib, math, os, time
import stepper, chelper, toolhead
from extras.homing import Homing, HomingMove
from kinematics.extruder import PrinterExtruder, DummyExtruder, ExtruderStepper

SDS_CHECK_TIME = 0.001 # step+dir+step filter in stepcompress.c

# Main code to track events (and their timing) on the MMU Machine implemented as additional "toolhead"
# (code pulled from toolhead.py)
class MmuToolHead(toolhead.ToolHead, object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.all_mcus = [m for n, m in self.printer.lookup_objects(module='mcu')]
        self.mcu = self.all_mcus[0]
        self.can_pause = True
        if self.mcu.is_fileoutput():
            self.can_pause = False
        self.move_queue = toolhead.MoveQueue(self) # Use base class MoveQueue
        self.gear_motion_queue = self.extruder_synced_to_gear = None # For bi-directional syncing of gear and extruder
        self.prev_rail_steppers = self.prev_g_sk = self.prev_sk = self.prev_trapq = None
        self.commanded_pos = [0., 0., 0., 0.]
        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)

        # Velocity and acceleration control
        self.gear_max_velocity = config.getfloat('gear_max_velocity', 300, above=0.)
        self.gear_max_accel = config.getfloat('gear_max_accel', 500, above=0.)
        self.selector_max_velocity = config.getfloat('selector_max_velocity', 250, above=0.)
        self.selector_max_accel = config.getfloat('selector_max_accel', 1500, above=0.)

        self.max_velocity = max(self.selector_max_velocity, self.gear_max_velocity)
        self.max_accel = max(self.selector_max_accel, self.gear_max_accel)

        # The following aren't very interesting for MMU control so leave to klipper defaults
        self.requested_accel_to_decel = config.getfloat('max_accel_to_decel', self.max_accel * 0.5, above=0.)
        self.max_accel_to_decel = self.requested_accel_to_decel
        self.square_corner_velocity = config.getfloat('square_corner_velocity', 5., minval=0.)
        self.junction_deviation = 0.
        self._calc_junction_deviation()
        # Print time tracking
        self.buffer_time_low = config.getfloat('buffer_time_low', 1.000, above=0.)
        self.buffer_time_high = config.getfloat('buffer_time_high', 2.000, above=self.buffer_time_low)
        self.buffer_time_start = config.getfloat('buffer_time_start', 0.250, above=0.)
        self.move_flush_time = config.getfloat('move_flush_time', 0.050, above=0.)
        self.print_time = 0.
        self.special_queuing_state = "Flushed"
        self.need_check_stall = -1.
        self.flush_timer = self.reactor.register_timer(self._flush_handler)
        self.move_queue.set_flush_time(self.buffer_time_high)
        self.idle_flush_print_time = 0.
        self.print_stall = 0
        self.drip_completion = None
        # Kinematic step generation scan window time tracking
        self.kin_flush_delay = SDS_CHECK_TIME
        self.kin_flush_times = []
        self.force_flush_time = self.last_kin_move_time = 0.
        # Setup iterative solver
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.step_generators = []
        # Create kinematics class
        gcode = self.printer.lookup_object('gcode')
        self.Coord = gcode.Coord
        self.extruder = DummyExtruder(self.printer)

        # Setup extruder kinematics for when gear rail is synced to extruder
        ffi_main, ffi_lib = chelper.get_ffi()
        self.sk_extruder = ffi_main.gc(ffi_lib.extruder_stepper_alloc(), ffi_lib.free)

        # Normal gear rail kinematics when extruder is synced to gear rail
        ffi_main, ffi_lib = chelper.get_ffi()
        self.sk_default = ffi_main.gc(ffi_lib.cartesian_stepper_alloc(b'y'), ffi_lib.free)
        
        # Create MMU kinematics
        try:
            self.kin = MmuKinematics(self, config)
        except config.error as e:
            raise
        except self.printer.lookup_object('pins').error as e:
            raise
        except:
            msg = "Error loading MMU kinematics"
            logging.exception(msg)
            raise config.error(msg)

        # Create MmuExtruderStepper for later insertion into PrinterExtruder on Toolhead (on klippy:connect)
        self.mmu_extruder_stepper = MmuExtruderStepper(config.getsection('extruder'), self.kin.rails[1]) # Only first extruder is handled

        # Nullify original extruder stepper definition so Klipper doesn't try to create it again
        options = [ 'step_pin', 'dir_pin', 'enable_pin', 'endstop_pin', 'rotation_distance', 'gear_ratio',
                    'microsteps', 'full_steps_per_rotation', 'pressure_advance', 'pressure_advance_smooth_time']
        for i in options:
            if config.fileconfig.has_option('extruder', i):
                config.fileconfig.remove_option('extruder', i)
        self.printer.register_event_handler('klippy:connect', self.handle_connect)

        # Add useful debugging command
        gcode.register_command('_MMU_DUMP_TOOLHEAD', self.cmd_DUMP_RAILS, desc=self.cmd_DUMP_RAILS_help)

    def handle_connect(self):
        # Now we can switch in MmuExtruderStepper
        toolhead = self.printer.lookup_object('toolhead')
        printer_extruder = toolhead.get_extruder()
        printer_extruder.extruder_stepper = self.mmu_extruder_stepper
        self.mmu_extruder_stepper.stepper.set_trapq(printer_extruder.get_trapq())

    # Ensure the correct number of axes for convenience - MMU only has two
    # Also, handle case when gear rail is synced to extruder
    def set_position(self, newpos, homing_axes=()):
        for _ in range(4 - len(newpos)):
            newpos.append(0.)
        super(MmuToolHead, self).set_position(newpos, homing_axes)
        self.resync_gear_position_to_extruder()

    def get_selector_limits(self):
        return self.selector_max_velocity, self.selector_max_accel

    def get_gear_limits(self):
        return self.gear_max_velocity, self.gear_max_accel

    # Is gear rail synced to extruder (for in print syncing)
    def is_gear_synced_to_extruder(self):
        return self.gear_motion_queue is not None

    # Is extruder stepper synced to gear rail (general MMU synced movement)
    def is_extruder_synced_to_gear(self):
        return self.extruder_synced_to_gear is not None

    def is_synced(self):
        return self.is_gear_synced_to_extruder() or self.is_extruder_synced_to_gear()

    def sync_gear_to_extruder(self, extruder_name):
        if self.extruder_synced_to_gear:
            self.sync_extruder_to_gear(None) # Mutually exclusive so unsync first

        printer_toolhead = self.printer.lookup_object('toolhead')
        printer_toolhead.flush_step_generation()
        self.flush_step_generation()
        gear_rail = self.get_kinematics().rails[1]

        if extruder_name:
            # Syncing
            if self.gear_motion_queue: return
            extruder = self.printer.lookup_object(extruder_name, None)
            if extruder is None or not isinstance(extruder, PrinterExtruder):
                raise self.printer.command_error("'%s' is not a valid extruder" % extruder_name)

            self.prev_g_sk = [s.set_stepper_kinematics(self.sk_extruder) for s in gear_rail.get_steppers()]
            gear_rail.set_trapq(extruder.get_trapq())
            e_pos = extruder.last_position
            gear_rail.set_position([e_pos, 0., 0.])

            # Shift gear rail step generator to printer toolhead. Each stepper is registered individually
            for s in gear_rail.get_steppers():
                handler = s.generate_steps
                self.step_generators.remove(handler)
                printer_toolhead.register_step_generator(handler)

            self.gear_motion_queue = extruder_name # We are synced!
        else:
            # Unsyncing
            if not self.gear_motion_queue: return
            for s, sk in zip(gear_rail.get_steppers(), self.prev_g_sk):
                s.set_stepper_kinematics(sk)
            gear_rail.set_trapq(self.get_trapq())
            g_pos = self.get_position()[1]
            gear_rail.set_position([0., g_pos, 0.])

            # Shift gear rail steppers step generator back to MMU toolhead
            for s in gear_rail.get_steppers():
                handler = s.generate_steps
                printer_toolhead.step_generators.remove(handler)
                self.register_step_generator(handler)

            self.gear_motion_queue = None

    def resync_gear_position_to_extruder(self):
        if self.gear_motion_queue:
            extruder = self.printer.lookup_object(self.gear_motion_queue, None)
            e_pos = extruder.last_position
            gear_rail = self.get_kinematics().rails[1]
            gear_rail.set_position([e_pos, 0., 0.])

    def sync_extruder_to_gear(self, extruder_name, extruder_only=False):
        if self.gear_motion_queue:
            self.sync_gear_to_extruder(None) # Mutually exclusive so unsync first

        printer_toolhead = self.printer.lookup_object('toolhead')
        printer_toolhead.flush_step_generation()
        self.flush_step_generation()
        gear_rail = self.get_kinematics().rails[1]

        if extruder_name:
            # Syncing
            if self.extruder_synced_to_gear: return
            extruder = self.printer.lookup_object(extruder_name, None)
            if extruder is None or not isinstance(extruder, PrinterExtruder):
                raise self.printer.command_error("'%s' is not a valid extruder" % extruder_name)
            extruder_stepper = extruder.extruder_stepper.stepper

            # Switch extruder stepper to use MMU toolhead kinematics and trapq
            self.prev_sk = extruder_stepper.set_stepper_kinematics(self.sk_default)
            self.prev_trapq = extruder_stepper.set_trapq(self.get_trapq())
            g_pos = gear_rail.get_commanded_position()
            extruder_stepper.set_position([0., g_pos, 0.])

            # Injecting the extruder stepper into the gear rail
            if extruder_only:
                self.prev_rail_steppers = gear_rail.steppers
                gear_rail.steppers = [extruder_stepper]
                gear_rail.get_commanded_position = extruder_stepper.get_commanded_position
                gear_rail.calc_position_from_coord = extruder_stepper.calc_position_from_coord
            else:
                gear_rail.steppers.append(extruder_stepper)

            # Shift extruder step generator to mmu toolhead
            handler = extruder_stepper.generate_steps
            printer_toolhead.step_generators.remove(handler)
            self.register_step_generator(handler)

            # Remove handlers for default gear steppers if necessary
            if extruder_only:
                for s in self.prev_rail_steppers:
                    handler = s.generate_steps
                    self.step_generators.remove(handler)

            self.extruder_synced_to_gear = extruder_name # We are synced!
        else:
            # Unsyncing
            if not self.extruder_synced_to_gear: return
            extruder = self.printer.lookup_object(self.extruder_synced_to_gear)
            extruder_stepper = extruder.extruder_stepper.stepper

            # Restore handlers for normal gear steppers and reset position if necessary
            if self.prev_rail_steppers: # Rail contains only extruder
                for s in self.prev_rail_steppers:
                    handler = s.generate_steps
                    self.register_step_generator(handler)

                g_pos = gear_rail.get_commanded_position()
                gear_rail.steppers = self.prev_rail_steppers
                gear_rail.get_commanded_position = gear_rail.steppers[0].get_commanded_position
                gear_rail.calc_position_from_coord = gear_rail.steppers[0].calc_position_from_coord
                gear_rail.set_position([0., g_pos, 0.])
                self.prev_rail_steppers = None
            else:
                gear_rail.steppers.pop() # Extruder stepper

            # Restore extruder kinematics and trap queue
            extruder_stepper.set_trapq(self.prev_trapq)
            extruder_stepper.set_stepper_kinematics(self.prev_sk)
            e_pos = printer_toolhead.get_position()[3]
            extruder_stepper.set_position([e_pos, 0., 0.])

            # Shift extruder step generator back to printer toolhead
            handler = extruder_stepper.generate_steps
            self.step_generators.remove(handler)
            printer_toolhead.register_step_generator(handler)

            self.extruder_synced_to_gear = None

    def get_status(self, eventtime):
        res = super(MmuToolHead, self).get_status(eventtime)
        res.update(dict(self.get_kinematics().get_status(eventtime)))
        res.update({ 'filament_pos': self.mmu_toolhead.get_position()[1] })
        return res

    cmd_DUMP_RAILS_help = "For debugging: dump current configuration of MMU Toolhead rails"
    def cmd_DUMP_RAILS(self, gcmd):
        msg = self.dump_rails()
        gcmd.respond_raw(msg)

    def dump_rails(self):
        printer_toolhead = self.printer.lookup_object('toolhead')
        msg =  "MMU TOOLHEAD: %s\n" % self.get_position()
        extruder_name = "extruder"
        for axis, rail in enumerate(self.get_kinematics().rails):
            msg += "\n" if axis > 0 else ""
            header = "RAIL: %s (Steppers: %d, Default endstops: %d, Extra endstops: %d) %s" % (rail.rail_name, len(rail.steppers), len(rail.endstops), len(rail.extra_endstops), '-' * 100)
            msg += header[:100] + "\n"
            for idx, s in enumerate(rail.get_steppers()):
                msg += "- Stepper %d: %s\n" % (idx, s.get_name())
                msg += "- - Commanded Position: %.2f, " % s.get_commanded_position()
                msg += "MCU Position: %.2f, " % s.get_mcu_position()
                msg += "Rotation Distance: %.6f (in %d steps)\n" % s.get_rotation_distance()
            msg += "Endstops:\n"
            for (mcu_endstop, name) in rail.endstops:
                if mcu_endstop.__class__.__name__ == "MockEndstop":
                    msg += "- None (Mock - cannot home rail)\n"
                else:
                    msg += "- '%s', mcu: '%s', pin: '%s', obj_id: %s" % (name, mcu_endstop.get_mcu().get_name(), mcu_endstop._pin, id(mcu_endstop))
                    msg += " (virtual)\n" if rail.is_endstop_virtual(name) else "\n"
                    msg += "- - Registed on steppers: %s\n" % ["%d: %s" % (idx, s.get_name()) for idx, s in enumerate(mcu_endstop.get_steppers())]
            msg += "Extra Endstops:\n"
            for (mcu_endstop, name) in rail.extra_endstops:
                msg += "- '%s', mcu: '%s', pin: '%s', obj_id: %s" % (name, mcu_endstop.get_mcu().get_name(), mcu_endstop._pin, id(mcu_endstop))
                msg += " (virtual)\n" if rail.is_endstop_virtual(name) else "\n"
                msg += "- - Registed on steppers: %s\n" % ["%d: %s" % (idx, s.get_name()) for idx, s in enumerate(mcu_endstop.get_steppers())]
            if axis == 1:
                if self.gear_motion_queue:
                    msg += "Gear rail SYNCED to extruder '%s'\n" % self.gear_motion_queue
                    extruder_name = self.gear_motion_queue
                if self.extruder_synced_to_gear:
                    msg += "Extruder '%s' SYNCED to gear rail\n" % self.extruder_synced_to_gear
                    extruder_name = self.extruder_synced_to_gear

        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder and isinstance(extruder, PrinterExtruder):
            msg +=  "\nPRINTER TOOLHEAD: %s\n" % printer_toolhead.get_position()
            header = "Extruder Stepper: %s %s" % (extruder_name, '-' * 100)
            msg += header[:100] + "\n"
            extruder_stepper = extruder.extruder_stepper.stepper
            msg += "- - Commanded Position: %.2f, " % extruder_stepper.get_commanded_position()
            msg += "MCU Position: %.2f, " % extruder_stepper.get_mcu_position()
            msg += "Rotation Distance: %.6f (in %d steps)\n" % extruder_stepper.get_rotation_distance()
        return msg


# MMU Kinematics class
# (loosely based on corexy.py)
class MmuKinematics:
    def __init__(self, toolhead, config):
        self.printer = config.get_printer()
        self.toolhead = toolhead

        # Setup "axis" rails
        self.axes = [('x', 'selector', True), ('y', 'gear', False)]
        self.rails = [MmuLookupMultiRail(config.getsection('stepper_mmu_' + s), need_position_minmax=mm, default_position_endstop=0.) for a, s, mm in self.axes]
        for rail, axis in zip(self.rails, 'xy'):
            rail.setup_itersolve('cartesian_stepper_alloc', axis.encode())

        for s in self.get_steppers():
            s.set_trapq(toolhead.get_trapq())
            toolhead.register_step_generator(s.generate_steps)

        # Setup boundary checks
        self.selector_max_velocity, self.selector_max_accel = toolhead.get_selector_limits()
        self.gear_max_velocity, self.gear_max_accel = toolhead.get_gear_limits()
        self.move_accel = None
        self.limits = [(1.0, -1.0)] * len(self.rails)
    
    def get_steppers(self):
        return [s for rail in self.rails for s in rail.get_steppers()]

    def calc_position(self, stepper_positions):
        #for r in self.rails:
        #    logging.info("DEBUG: * rail=%s, initial_stepper_name=%s" % (r.get_name, r.steppers[0].get_name()))
        #logging.info("DEBUG: * stepper_positions=%s" % stepper_positions)
        return [stepper_positions[rail.steppers[0].get_name()] for rail in self.rails] # Note can't assume rail name == stepper name

    def set_position(self, newpos, homing_axes):
        for i, rail in enumerate(self.rails):
            rail.set_position(newpos)
            if i == 1:
                self.toolhead.resync_gear_position_to_extruder() # Better done on Rail itself but rail doesn't know it's the mmu gear
            if i in homing_axes:
                self.limits[i] = rail.get_range()
    
    def home(self, homing_state, homepos=None):
        for axis in homing_state.get_axes():
            if not axis == 0: # Saftey: Only selector (axis[0]) can be homed
                continue
            rail = self.rails[axis]
            position_min, position_max = rail.get_range()
            hi = rail.get_homing_info()
            homepos = homepos or [None, None, None, None]
            homepos[axis] = hi.position_endstop
            forcepos = list(homepos)
            if hi.positive_dir:
                forcepos[axis] -= 1.5 * (hi.position_endstop - position_min)
            else:
                forcepos[axis] += 1.5 * (position_max - hi.position_endstop)
            homing_state.home_rails([rail], forcepos, homepos) # Perform homing

    def set_accel_limit(self, accel):
        self.move_accel = accel

    def check_move(self, move):
        limits = self.limits
        xpos, ypos = move.end_pos[:2]
        if xpos != 0. and (xpos < limits[0][0] or xpos > limits[0][1]):
            raise move.move_error()
        
        if move.axes_d[0]: # Selector
            move.limit_speed(self.selector_max_velocity, self.selector_max_accel)
        elif move.axes_d[1]: # Gear
            move.limit_speed(self.gear_max_velocity, min(self.gear_max_accel, self.move_accel) if self.move_accel else self.gear_max_accel)

    def get_status(self, eventtime):
        return {
            'selector_homed': self.limits[0][0] <= self.limits[0][1],
            'gear_synced_to_extruder': self.is_gear_synced_to_extruder(),
            'extruder_synced_to_gear': self.is_extruder_synced_to_gear()
        }


# Extend Klipper homing module to leverage MMU "toolhead"
# (code pulled from homing.py)
class MmuHoming(Homing, object):
    def __init__(self, printer, mmu_toolhead, retract_gear_speed_while_moving_selector=0):
        super(MmuHoming, self).__init__(printer)
        self.toolhead = mmu_toolhead # Override default toolhead
        self.retract_gear_speed_while_moving_selector = retract_gear_speed_while_moving_selector
    
    def home_rails(self, rails, forcepos, movepos):
        # Notify of upcoming homing operation
        self.printer.send_event("homing:home_rails_begin", self, rails)
        # Alter kinematics class to think printer is at forcepos
        homing_axes = [axis for axis in range(3) if forcepos[axis] is not None]
        startpos = self._fill_coord(forcepos)
        homepos = self._fill_coord(movepos)
        self.toolhead.set_position(startpos, homing_axes=homing_axes)
        # Perform first home
        endstops = [es for rail in rails for es in rail.get_endstops()]
        hi = rails[0].get_homing_info()
        hmove = HomingMove(self.printer, endstops, self.toolhead) # Override default toolhead

        if self.retract_gear_speed_while_moving_selector > 0:
            selector_move_dist = homepos[0] - startpos[0]
            speed = math.sqrt(self.retract_gear_speed_while_moving_selector ** 2 + hi.speed ** 2)
            gear_move_dist = selector_move_dist / speed * self.retract_gear_speed_while_moving_selector
            homepos[1] = homepos[1] - gear_move_dist
        else:
            speed = hi.speed

        hmove.homing_move(homepos, speed)
        # Perform second home
        if hi.retract_dist:
            # Retract
            startpos = self._fill_coord(forcepos)
            homepos = self._fill_coord(movepos)
            axes_d = [hp - sp for hp, sp in zip(homepos, startpos)]
            move_d = math.sqrt(sum([d*d for d in axes_d[:3]]))
            retract_r = min(1., hi.retract_dist / move_d)
            retractpos = [hp - ad * retract_r
                          for hp, ad in zip(homepos, axes_d)]
            self.toolhead.move(retractpos, hi.retract_speed)
            # Home again
            startpos = [rp - ad * retract_r
                        for rp, ad in zip(retractpos, axes_d)]
            self.toolhead.set_position(startpos)
            hmove = HomingMove(self.printer, endstops, self.toolhead) # Override default toolhead
            hmove.homing_move(homepos, hi.second_homing_speed)
            if hmove.check_no_movement() is not None:
                raise self.printer.command_error(
                    "Endstop %s still triggered after retract"
                    % (hmove.check_no_movement(),))
        # Signal home operation complete
        self.toolhead.flush_step_generation()
        self.trigger_mcu_pos = {sp.stepper_name: sp.trig_pos
                                for sp in hmove.stepper_positions}
        self.adjust_pos = {}
        self.printer.send_event("homing:home_rails_end", self, rails)
        if any(self.adjust_pos.values()):
            # Apply any homing offsets
            kin = self.toolhead.get_kinematics()
            homepos = self.toolhead.get_position()
            kin_spos = {s.get_name(): (s.get_commanded_position()
                                       + self.adjust_pos.get(s.get_name(), 0.))
                        for s in kin.get_steppers()}
            newpos = kin.calc_position(kin_spos)
            for axis in homing_axes:
                homepos[axis] = newpos[axis]
            self.toolhead.set_position(homepos)


# Extend PrinterRail to allow for multiple (switchable) endstops and to allow for no default endstop
# (defined in stepper.py)
class MmuPrinterRail(stepper.PrinterRail, object):
    def __init__(self, config, **kwargs):
        self.printer = config.get_printer()
        self.rail_name = config.get_name()
        self.query_endstops = self.printer.load_object(config, 'query_endstops')
        self.extra_endstops = []
        self.virtual_endstops = []
        super(MmuPrinterRail, self).__init__(config, **kwargs)

    def add_extra_stepper(self, config, **kwargs):
        if not self.endstops and config.get('endstop_pin', None) is None:
            # No endstop defined, so configure a mock endstop. The rail is, of course, only homable
            # if it has a properly configured endstop at runtime
            self.endstops = [(self.MockEndstop(), "mock")] # Hack: pretend we have a default endstop so super class will work
        super(MmuPrinterRail, self).add_extra_stepper(config, **kwargs)

        # Setup default endstop similarly to "extra" endstops with vanity sensor name
        endstop_pin = config.get('endstop_pin', None)
        if endstop_pin:
            last_mcu_es=self.endstops[-1]
            # Remove the default endstop name if alternative name is specified
            endstop_name = config.get('endstop_name', None)
            if endstop_name:
                self.endstops.pop()
                self.endstops.append((last_mcu_es[0], endstop_name))
                qee = self.query_endstops.endstops
                if qee:
                    qee.pop()
                self.query_endstops.register_endstop(self.endstops[0][0], endstop_name)
                self.extra_endstops.append((last_mcu_es[0], endstop_name))
                self.extra_endstops.append((last_mcu_es[0], 'default'))
                if 'virtual_endstop' in endstop_pin:
                    self.virtual_endstops.append(endstop_name)
            if 'virtual_endstop' in endstop_pin:
                self.virtual_endstops.append('default')

        # Handle any extra endstops
        extra_endstop_pins = config.getlist('extra_endstop_pins', [])
        extra_endstop_names = config.getlist('extra_endstop_names', [])
        if extra_endstop_pins:
            if len(extra_endstop_pins) != len(extra_endstop_names):
                raise self.config.error("`extra_endstop_pins` and `extra_endstop_names` are different lengths")
            for idx, pin in enumerate(extra_endstop_pins):
                name = extra_endstop_names[idx]
                self.add_extra_endstop(pin, name)

    def add_extra_endstop(self, pin, name, register=True, bind_rail_steppers=True):
        if 'virtual_endstop' in pin:
            self.virtual_endstops.append(name)
        ppins = self.printer.lookup_object('pins')
        mcu_endstop = ppins.setup_pin('endstop', pin)
        self.extra_endstops.append((mcu_endstop, name))
        if bind_rail_steppers:
            for s in self.steppers:
                mcu_endstop.add_stepper(s)
        if register: # and not self.is_endstop_virtual(name):
            self.query_endstops.register_endstop(mcu_endstop, name)
        return mcu_endstop

    def get_extra_endstop_names(self):
        return [x[1] for x in self.extra_endstops]

    # Returns the mcu_endstop of given name
    def get_extra_endstop(self, name):
         matches = [x for x in self.extra_endstops if x[1] == name]
         if matches:
             return list(matches)
         else:
             return None

    def is_endstop_virtual(self, name):
        return name in self.virtual_endstops if name else False

    class MockEndstop:
        def add_stepper(self, *args, **kwargs):
            pass

# Wrapper for dual stepper motor support
def MmuLookupMultiRail(config, need_position_minmax=True, default_position_endstop=None, units_in_radians=False):
    rail = MmuPrinterRail(config, need_position_minmax=need_position_minmax, default_position_endstop=default_position_endstop, units_in_radians=units_in_radians)
    for i in range(1, 99):
        if not config.has_section(config.get_name() + str(i)):
            break
        rail.add_extra_stepper(config.getsection(config.get_name() + str(i)))
    return rail


# Extend ExtruderStepper to allow for adding and managing endstops (useful only when part of gear rail, not operating as an Extruder)
class MmuExtruderStepper(ExtruderStepper, object):
    def __init__(self, config, gear_rail):
        super(MmuExtruderStepper, self).__init__(config)
        tmc_chips = ["tmc2209", "tmc2130", "tmc2208", "tmc2660", "tmc5160", "tmc2240"]
        for chip in tmc_chips:
            tmc = self.printer.lookup_object('%s stepper_mmu_selector' % chip, None)
            if tmc:
                _ = self.printer.load_object(config, '%s extruder' % chip) # Prevent error if loading after real "[extruder]" section
                break

        endstop_pin = config.get('endstop_pin', None)
        if endstop_pin:
            mcu_endstop = gear_rail.add_extra_endstop(endstop_pin, 'mmu_ext_touch', bind_rail_steppers=True)
            mcu_endstop.add_stepper(self.stepper)

