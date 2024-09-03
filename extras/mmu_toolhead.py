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
# Based on code by Kevin O'Connor <kevin@koconnor.net>
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


# Main code to track events (and their timing) on the MMU Machine implemented as additional "toolhead"
# (code pulled from toolhead.py)
class MmuToolHead(toolhead.ToolHead, object):

    # Gear/Extruder synchronization modes (None = unsynced)
    EXTRUDER_SYNCED_TO_GEAR = 1
    EXTRUDER_ONLY_ON_GEAR   = 2
    GEAR_SYNCED_TO_EXTRUDER = 3

    def __init__(self, config, mmu, homing_extruder):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.all_mcus = [m for n, m in self.printer.lookup_objects(module='mcu')]
        self.mcu = self.all_mcus[0]
        if hasattr(toolhead, 'LookAheadQueue'):
            self.lookahead = toolhead.LookAheadQueue(self) # Happy Hare: Use base class LookAheadQueue
            self.lookahead.set_flush_time(toolhead.BUFFER_TIME_HIGH) # Happy Hare: Use base class
        else:
            self.move_queue = toolhead.MoveQueue(self) # Happy Hare: Use base class MoveQueue (older klipper)
            self.move_queue.set_flush_time(toolhead.BUFFER_TIME_HIGH) # Happy Hare: Use base class (older klipper)
        self.commanded_pos = [0., 0., 0., 0.]

        # MMU velocity and acceleration control
        self.gear_max_velocity = config.getfloat('gear_max_velocity', 300, above=0.)
        self.gear_max_accel = config.getfloat('gear_max_accel', 500, above=0.)
        self.selector_max_velocity = config.getfloat('selector_max_velocity', 250, above=0.)
        self.selector_max_accel = config.getfloat('selector_max_accel', 1500, above=0.)

        self.max_velocity = max(self.selector_max_velocity, self.gear_max_velocity)
        self.max_accel = max(self.selector_max_accel, self.gear_max_accel)

        min_cruise_ratio = 0.5
        if config.getfloat('minimum_cruise_ratio', None) is None:
            req_accel_to_decel = config.getfloat('max_accel_to_decel', None, above=0.)
            if req_accel_to_decel is not None:
                config.deprecate('max_accel_to_decel')
                min_cruise_ratio = 1. - min(1., (req_accel_to_decel / self.max_accel))
        self.min_cruise_ratio = config.getfloat('minimum_cruise_ratio', min_cruise_ratio, below=1., minval=0.)
        self.square_corner_velocity = config.getfloat('square_corner_velocity', 5., minval=0.)
        self.junction_deviation = self.max_accel_to_decel = 0.
        self.requested_accel_to_decel = self.min_cruise_ratio * self.max_accel # Backward klipper compatibility 31de734d193d
        self._calc_junction_deviation()

        # Input stall detection
        self.check_stall_time = 0.
        self.print_stall = 0
        # Input pause tracking
        self.can_pause = True
        if self.mcu.is_fileoutput():
            self.can_pause = False
        self.need_check_pause = -1.
        # Print time tracking
        self.print_time = 0.
        self.special_queuing_state = "NeedPrime"
        self.priming_timer = None
        self.drip_completion = None
        # Flush tracking
        self.flush_timer = self.reactor.register_timer(self._flush_handler)
        self.do_kick_flush_timer = True
        self.last_flush_time = self.last_sg_flush_time = self.min_restart_time = 0. # last_sg_flush_time deprecated
        self.need_flush_time = self.step_gen_time = self.clear_history_time = 0.
        # Kinematic step generation scan window time tracking
        self.kin_flush_delay = toolhead.SDS_CHECK_TIME # Happy Hare: Use base class
        self.kin_flush_times = []
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

        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)
        
        # Create MMU kinematics
        try:
            self.kin = MmuKinematics(self, config)
            self.all_gear_rail_steppers = self.kin.rails[1].get_steppers()
        except config.error as e:
            raise
        except self.printer.lookup_object('pins').error as e:
            raise
        except:
            msg = "Error loading MMU kinematics"
            logging.exception(msg)
            raise config.error(msg)

        self.mmu = mmu
        self.mmu_extruder_stepper = None
        self.homing_extruder = homing_extruder
        if self.homing_extruder:
            # Create MmuExtruderStepper for later insertion into PrinterExtruder on Toolhead (on klippy:connect)
            self.mmu_extruder_stepper = MmuExtruderStepper(config.getsection('extruder'), self.kin.rails[1]) # Only first extruder is handled

            # Nullify original extruder stepper definition so Klipper doesn't try to create it again. Restore in handle_connect()
            self.old_ext_options = {}
            self.config = config
            options = [ 'step_pin', 'dir_pin', 'enable_pin', 'endstop_pin', 'rotation_distance', 'gear_ratio',
                        'microsteps', 'full_steps_per_rotation', 'pressure_advance', 'pressure_advance_smooth_time']
            for i in options:
                if config.fileconfig.has_option('extruder', i):
                    self.old_ext_options[i] = config.fileconfig.get('extruder', i)
                    config.fileconfig.remove_option('extruder', i)

        self.printer.register_event_handler('klippy:connect', self.handle_connect)

        # Add useful debugging command
        gcode.register_command('_MMU_DUMP_TOOLHEAD', self.cmd_DUMP_RAILS, desc=self.cmd_DUMP_RAILS_help)

        # Bi-directional sync management of gear(s) and extruder(s)
        self.inactive_gear_steppers = []
        self.mmu_toolhead = self
        self.sync_mode = None

    def handle_connect(self):
        self.printer_toolhead = self.printer.lookup_object('toolhead')

        printer_extruder = self.printer_toolhead.get_extruder()
        if self.homing_extruder:
            # Restore original extruder options in case user macros reference them
            for key in self.old_ext_options:
                value = self.old_ext_options[key]
                self.config.fileconfig.set('extruder', key, value)

            # Now we can switch in homing MmuExtruderStepper
            printer_extruder.extruder_stepper = self.mmu_extruder_stepper
            self.mmu_extruder_stepper.stepper.set_trapq(printer_extruder.get_trapq())
        else:
            self.mmu_extruder_stepper = printer_extruder.extruder_stepper

    # Ensure the correct number of axes for convenience - MMU only has two
    # Also, handle case when gear rail is synced to extruder
    def set_position(self, newpos, homing_axes=()):
        for _ in range(4 - len(newpos)):
            newpos.append(0.)
        super(MmuToolHead, self).set_position(newpos, homing_axes)

    def get_selector_limits(self):
        return self.selector_max_velocity, self.selector_max_accel

    def get_gear_limits(self):
        return self.gear_max_velocity, self.gear_max_accel

    def is_synced(self):
        return self.sync_mode is not None

    # Is extruder stepper synced to gear rail (general MMU synced movement)
    def is_extruder_synced_to_gear(self):
        return self.sync_mode in [self.EXTRUDER_SYNCED_TO_GEAR, self.EXTRUDER_ONLY_ON_GEAR]

    # Is gear rail synced to extruder (for in print syncing)
    def is_gear_synced_to_extruder(self):
        return self.sync_mode == self.GEAR_SYNCED_TO_EXTRUDER

    def sync(self, new_sync_mode):
        if new_sync_mode == self.sync_mode: return new_sync_mode
        prev_sync_mode = self.sync_mode
        self.unsync()
        if new_sync_mode is None: return prev_sync_mode # Lazy way to unsync()
        self.mmu.log_stepper("sync(mode=%s)" % new_sync_mode)
        self.printer_toolhead.flush_step_generation()
        self.mmu_toolhead.flush_step_generation()

        ffi_main, ffi_lib = chelper.get_ffi()
        if new_sync_mode in [self.EXTRUDER_SYNCED_TO_GEAR, self.EXTRUDER_ONLY_ON_GEAR]:
            driving_toolhead = self.mmu_toolhead
            following_toolhead = self.printer_toolhead
            following_steppers = [self.printer_toolhead.get_extruder().extruder_stepper.stepper]
            self._prev_trapq = following_steppers[0].get_trapq()
            driving_trapq = driving_toolhead.get_trapq()
            s_alloc = ffi_lib.cartesian_stepper_alloc(b"y")
            pos = [0., self.mmu_toolhead.get_position()[1], 0.]

            # Cripple unused/unwanted gear steppers
            # Inject the extruder steppers into the gear rail
            rail = self.mmu_toolhead.get_kinematics().rails[1]
            if new_sync_mode == self.EXTRUDER_ONLY_ON_GEAR:
                self.inactive_gear_steppers = list(rail.steppers)
                for s in self.inactive_gear_steppers:
                    self.mmu_toolhead.step_generators.remove(s.generate_steps)
            rail.steppers.extend(following_steppers)

        elif new_sync_mode == self.GEAR_SYNCED_TO_EXTRUDER:
            driving_toolhead = self.printer_toolhead
            following_toolhead = self.mmu_toolhead
            following_steppers = self.mmu_toolhead.get_kinematics().rails[1].get_steppers()
            self._prev_trapq = self.mmu_toolhead.get_trapq()
            driving_trapq = self.printer_toolhead.get_extruder().get_trapq()
            s_alloc = ffi_lib.extruder_stepper_alloc()
            pos = [self.printer_toolhead.get_position()[3], 0., 0.]

        else:
            raise Exception("Invalid sync_mode: %d" % new_sync_mode)

        self._prev_sk, self._prev_rd = [], []
        for s in following_steppers:
            s_kinematics = ffi_main.gc(s_alloc, ffi_lib.free)
            self._prev_sk.append(s.set_stepper_kinematics(s_kinematics))
            self._prev_rd.append(s.get_rotation_distance()[0])
            following_toolhead.step_generators.remove(s.generate_steps)
            driving_toolhead.register_step_generator(s.generate_steps)
            s.set_trapq(driving_trapq)
            s.set_position(pos)

        self.sync_mode = new_sync_mode
        if self.sync_mode == self.GEAR_SYNCED_TO_EXTRUDER:
            self.printer.send_event("mmu:synced")
        return prev_sync_mode

    def unsync(self):
        if self.sync_mode is None: return None
        self.mmu.log_stepper("unsync()")
        prev_sync_mode = self.sync_mode
        self.printer_toolhead.flush_step_generation()
        self.mmu_toolhead.flush_step_generation()

        if self.sync_mode in [self.EXTRUDER_SYNCED_TO_GEAR, self.EXTRUDER_ONLY_ON_GEAR]:
            driving_toolhead = self.mmu_toolhead
            following_toolhead = self.printer_toolhead
            following_steppers = [self.printer_toolhead.get_extruder().extruder_stepper.stepper]
            pos = [self.printer_toolhead.get_position()[3], 0., 0.]

            # Restore previously unused/unwanted gear steppers
            # Remove extruder steppers from gear rail
            rail = self.mmu_toolhead.get_kinematics().rails[1]
            if self.sync_mode == self.EXTRUDER_ONLY_ON_GEAR: # I.e. self.inactive_gear_steppers is not None
                for s in self.inactive_gear_steppers:
                    self.mmu_toolhead.register_step_generator(s.generate_steps)
                    s.set_position([0., self.mmu_toolhead.get_position()[1], 0.])
                self.inactive_gear_steppers = [] # python3 - self.inactive_gear_steppers.clear()
            rail.steppers = rail.steppers[:-len(following_steppers)]

        elif self.sync_mode == self.GEAR_SYNCED_TO_EXTRUDER:
            driving_toolhead = self.printer_toolhead
            following_toolhead = self.mmu_toolhead
            following_steppers = self.mmu_toolhead.get_kinematics().rails[1].get_steppers()
            pos = [0., self.mmu_toolhead.get_position()[1], 0.]

        else:
            raise Exception("Invalid sync_mode: %d" % sync_mode)

        for i, s in enumerate(following_steppers):
            s.set_stepper_kinematics(self._prev_sk[i])
            s.set_rotation_distance(self._prev_rd[i])
            driving_toolhead.step_generators.remove(s.generate_steps)
            following_toolhead.register_step_generator(s.generate_steps)
            s.set_trapq(self._prev_trapq)
            s.set_position(pos)

        if self.sync_mode == self.GEAR_SYNCED_TO_EXTRUDER:
            self.printer.send_event("mmu:unsynced")
        self.sync_mode = None
        return prev_sync_mode

    def is_selector_homed(self):
        return self.kin.get_status(self.reactor.monotonic())["selector_homed"]

    def get_status(self, eventtime):
        res = super(MmuToolHead, self).get_status(eventtime)
        res.update(dict(self.get_kinematics().get_status(eventtime)))
        res.extend({
            'filament_pos': self.mmu_toolhead.get_position()[1],
            'sync_mode': self.sync_mode
        })
        return res

    cmd_DUMP_RAILS_help = "For debugging: dump current configuration of MMU Toolhead rails"
    def cmd_DUMP_RAILS(self, gcmd):
        msg = self.dump_rails()
        gcmd.respond_raw(msg)

    def dump_rails(self):
        msg =  "MMU TOOLHEAD: %s\n" % self.get_position()
        extruder_name = self.printer_toolhead.get_extruder().get_name()
        for axis, rail in enumerate(self.get_kinematics().rails):
            msg += "\n" if axis > 0 else ""
            header = "RAIL: %s (Steppers: %d, Default endstops: %d, Extra endstops: %d) %s" % (rail.rail_name, len(rail.steppers), len(rail.endstops), len(rail.extra_endstops), '-' * 100)
            msg += header[:100] + "\n"
            for idx, s in enumerate(rail.get_steppers()):
                msg += "Stepper %d: %s%s\n" % (idx, s.get_name(), "(INACTIVE)" if axis == 1 and s in self.inactive_gear_steppers else "")
                msg += "- Commanded Pos: %.2f, " % s.get_commanded_position()
                msg += "MCU Pos: %.2f, " % s.get_mcu_position()
                rd = s.get_rotation_distance()
                msg += "Rotation Dist: %.6f (in %d steps, step_dist=%.6f)\n" % (rd[0], rd[1], s.get_step_dist())
            msg += "Endstops:\n"
            for (mcu_endstop, name) in rail.endstops:
                if mcu_endstop.__class__.__name__ == "MockEndstop":
                    msg += "- None (Mock - cannot home rail)\n"
                else:
                    msg += "- %s%s, mcu: %s, pin: %s" % (name," (virtual)" if rail.is_endstop_virtual(name) else "", mcu_endstop.get_mcu().get_name(), mcu_endstop._pin)
                    msg += " on: %s\n" % ["%d: %s" % (idx, s.get_name()) for idx, s in enumerate(mcu_endstop.get_steppers())]
            msg += "Extra Endstops:\n"
            for (mcu_endstop, name) in rail.extra_endstops:
                msg += "- %s%s, mcu: %s, pin: %s" % (name, " (virtual)" if rail.is_endstop_virtual(name) else "", mcu_endstop.get_mcu().get_name(), mcu_endstop._pin)
                msg += " on: %s\n" % ["%d: %s" % (idx, s.get_name()) for idx, s in enumerate(mcu_endstop.get_steppers())]
            if axis == 1: # Gear rail
                if self.is_gear_synced_to_extruder():
                    msg += "SYNCHRONIZED: Gear rail synced to extruder '%s'\n" % extruder_name
                if self.is_extruder_synced_to_gear():
                    msg += "SYNCHRONIZED: Extruder '%s' synced to gear rail\n" % extruder_name

        e_stepper = self.printer_toolhead.get_extruder().extruder_stepper.stepper
        msg +=  "\nPRINTER TOOLHEAD: %s\n" % self.printer_toolhead.get_position()
        header = "Extruder Stepper: %s %s %s" % (extruder_name, "(MmuExtruderStepper)" if isinstance(self.printer_toolhead.get_extruder().extruder_stepper, MmuExtruderStepper) else "", '-' * 100)
        msg += header[:100] + "\n"
        msg += "- Commanded Pos: %.2f, " % e_stepper.get_commanded_position()
        msg += "MCU Pos: %.2f, " % e_stepper.get_mcu_position()
        rd = e_stepper.get_rotation_distance()
        msg += "Rotation Dist: %.6f (in %d steps, step_dist=%.6f)\n" % (rd[0], rd[1], e_stepper.get_step_dist())
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
        positions = []
        for i, r in enumerate(self.rails):
            #logging.info("DEBUG: * %d. rail=%s, initial_stepper_name=%s", i, r.get_name(), r.steppers[0].get_name())
            if i == 1:
                stepper = next((s for s in r.steppers if s not in self.toolhead.inactive_gear_steppers), None)
                if stepper:
                    positions.append(stepper_positions[stepper.get_name()])
                else:
                    positions.append(stepper_positions[r.get_name()])
            else:
                positions.append(stepper_positions[r.get_name()])
        return positions

    def set_position(self, newpos, homing_axes):
        for i, rail in enumerate(self.rails):
            if i == 1 and self.toolhead.is_gear_synced_to_extruder():
                continue
            rail.set_position(newpos)
            if i in homing_axes:
                self.limits[i] = rail.get_range()

    def home(self, homing_state):
        for axis in homing_state.get_axes():
            if not axis == 0: # Saftey: Only selector (axis[0]) can be homed
                continue
            rail = self.rails[axis]
            position_min, position_max = rail.get_range()
            hi = rail.get_homing_info()
            homepos = [None, None, None, None]
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
            move.limit_speed(self.selector_max_velocity, min(self.selector_max_accel, self.move_accel or self.selector_max_accel))
        elif move.axes_d[1]: # Gear
            move.limit_speed(self.gear_max_velocity, min(self.gear_max_accel, self.move_accel or self.gear_max_accel))

    def get_status(self, eventtime):
        return {
            'selector_homed': self.limits[0][0] <= self.limits[0][1],
        }


# Extend Klipper homing module to leverage MMU "toolhead"
# (code pulled from homing.py)
class MmuHoming(Homing, object):
    def __init__(self, printer, mmu_toolhead):
        super(MmuHoming, self).__init__(printer)
        self.toolhead = mmu_toolhead # Override default toolhead
    
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
        hmove = HomingMove(self.printer, endstops, self.toolhead) # Happy Hare: Override default toolhead
        hmove.homing_move(homepos, hi.speed)
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
            hmove = HomingMove(self.printer, endstops, self.toolhead) # Happy Hare: Override default toolhead
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

# Wrapper for multiple stepper motor support
def MmuLookupMultiRail(config, need_position_minmax=True, default_position_endstop=None, units_in_radians=False):
    rail = MmuPrinterRail(config, need_position_minmax=need_position_minmax, default_position_endstop=default_position_endstop, units_in_radians=units_in_radians)
    for i in range(23):
        section_name = "%s_%s" % (config.get_name(), str(i))
        if not config.has_section(section_name):
            continue
        rail.add_extra_stepper(config.getsection(section_name))
    return rail


# Extend ExtruderStepper to allow for adding and managing endstops (useful only when part of gear rail, not operating as an Extruder)
class MmuExtruderStepper(ExtruderStepper, object):
    def __init__(self, config, gear_rail):
        super(MmuExtruderStepper, self).__init__(config)

        # Ensure sure corresponding TMC section is loaded so endstops can be added and to prevent error later when toolhead is created
        tmc_chips = ["tmc2209", "tmc2130", "tmc2208", "tmc2660", "tmc5160", "tmc2240"]
        for chip in tmc_chips:
            try:
                _ = self.printer.load_object(config, '%s extruder' % chip)
                break
            except:
                pass

        # This allows for setup of stallguard as an option for nozzle homing
        endstop_pin = config.get('endstop_pin', None)
        if endstop_pin:
            mcu_endstop = gear_rail.add_extra_endstop(endstop_pin, 'mmu_ext_touch', bind_rail_steppers=True)
            mcu_endstop.add_stepper(self.stepper)

