# Happy Hare MMU Software
# Mmu Steppers
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Create flexible MMU steppers that can have multiple homing endstops, support quick
#       direction reversal, flexible position min/max and lots of syncing options.
#
# Implements [mmu_stepper] klipper object
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging, collections, math

# Klipper imports
import stepper
from .homing import HomingMove
from .manual_stepper import ManualStepper


# -----------------------------------------------------------------------------------------------------------
# MmuGenericRail is similar to GenericPrinterRail but supports multiple endstops, direction reversal, etc
# -----------------------------------------------------------------------------------------------------------

class MmuGenericRail:

    def __init__(self, config, need_position_minmax=True, default_position_endstop=None, units_in_radians=False):

        self.stepper_units_in_radians = units_in_radians
        self.printer = config.get_printer()
        self.config = config
        self.name = config.get_name()

        self.steppers = []
        self.endstops = []          # default/shared rail endstops
        self.endstop_map = {}       # normalized pin -> metadata

        # MMU-specific tracking
        self.extra_endstops = []    # list of (mcu_endstop, symbolic_name)
        self.virtual_endstops = []  # symbolic names for virtual extra endstops

        # Optional default endstop
        self.endstop_pin = config.get('endstop_pin', None)
        self.default_mcu_endstop = None

        # Endstop/query support
        self.query_endstops = self.printer.load_object(config, 'query_endstops')

        if self.endstop_pin is not None:
            self.default_mcu_endstop = self.lookup_endstop(self.endstop_pin, self.name, register=False)

        # Primary endstop position
        if self.default_mcu_endstop is not None:
            if hasattr(self.default_mcu_endstop, "get_position_endstop"):
                self.position_endstop = self.default_mcu_endstop.get_position_endstop()
            elif default_position_endstop is None:
                self.position_endstop = config.getfloat('position_endstop')
            else:
                self.position_endstop = config.getfloat('position_endstop', default_position_endstop)
        else:
            # No default endstop configured
            if default_position_endstop is None:
                self.position_endstop = config.getfloat('position_endstop', 0.)
            else:
                self.position_endstop = config.getfloat('position_endstop', default_position_endstop)

        # Axis range
        if need_position_minmax:
            self.position_min = config.getfloat('position_min', 0.)
            self.position_max = config.getfloat('position_max', above=self.position_min)
        else:
            self.position_min = 0.
            self.position_max = self.position_endstop

        if self.default_mcu_endstop is not None:
            if (
                self.position_endstop < self.position_min or
                self.position_endstop > self.position_max
            ):
                raise config.error(f"position_endstop in section '{config.get_name()}' must be between position_min and position_max")

        # Homing mechanics
        self.homing_speed = config.getfloat('homing_speed', 10.0, above=0.)
        self.second_homing_speed = config.getfloat('second_homing_speed', self.homing_speed / 2., above=0.)
        self.homing_retract_speed = config.getfloat('homing_retract_speed', self.homing_speed, above=0.)
        self.homing_retract_dist = config.getfloat('homing_retract_dist', 5., minval=0.)
        self.homing_positive_dir = config.getboolean('homing_positive_dir', None)
        self.homing_move_dist = config.getfloat('homing_move_dist', None, above=0.) # PAUL added
        self.homing_accel = config.getfloat('homing_accel', 800., minval=0.) # PAUL added

        if self.default_mcu_endstop is not None:
            if self.homing_positive_dir is None:
                axis_len = self.position_max - self.position_min
                if self.position_endstop <= self.position_min + axis_len / 4.:
                    self.homing_positive_dir = False
                elif self.position_endstop >= self.position_max - axis_len / 4.:
                    self.homing_positive_dir = True
                else:
                    raise config.error(f"Unable to infer homing_positive_dir in section '{config.get_name()}'")

                config.getboolean('homing_positive_dir', self.homing_positive_dir)

            elif ((self.homing_positive_dir
                   and self.position_endstop == self.position_min)
                  or (not self.homing_positive_dir
                      and self.position_endstop == self.position_max)):
                raise config.error(f"Invalid homing_positive_dir / position_endstop in '{config.get_name()}'")
        else:
            if self.homing_positive_dir is None:
                self.homing_positive_dir = False

        # Parse selectable endstop names.
        # Each entry is:
        #   name=default
        # or
        #   name=<pin>
        for endstop_name, endstop_target in self._parse_extra_endstops(config):
            self.add_extra_endstop(endstop_target, endstop_name)


    # -------------------------------------------------------------------------
    # Generic rail-like API
    # -------------------------------------------------------------------------

    def get_name(self, short=False):
        if short:
            short = self.name.split()[-1]
            if "_" in short:
                return short.split("_")[0]
            return short
        return self.name


    def get_range(self):
        return self.position_min, self.position_max

    def get_homing_info(self):
        HomingInfo = collections.namedtuple(
            'homing_info',
            [
                'speed',
                'position_endstop',
                'retract_speed',
                'retract_dist',
                'positive_dir',
                'second_homing_speed',
                'move_dist', # PAUL added
            ],
        )
        homing_info = HomingInfo(
            self.homing_speed,
            self.position_endstop,
            self.homing_retract_speed,
            self.homing_retract_dist,
            self.homing_positive_dir,
            self.second_homing_speed,
            self.homing_move_dist, # PAUL added
        )
        return homing_info


    def get_steppers(self):
        return list(self.steppers)


    def get_endstops(self):
        return list(self.endstops)


    def lookup_endstop(self, endstop_pin, name, register=True, extra=False):
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(endstop_pin, True, True)

        # Normalize pin name
        pin_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])

        # Look for already-registered endstop
        endstop = self.endstop_map.get(pin_name, None)
        if endstop is None:
            mcu_endstop = ppins.setup_pin('endstop', endstop_pin)
            self.endstop_map[pin_name] = {
                'endstop': mcu_endstop,
                'invert': pin_params['invert'],
                'pullup': pin_params['pullup'],
            }
            if not extra:
                self.endstops.append((mcu_endstop, name))
            if register:
                self.query_endstops.register_endstop(mcu_endstop, name)
        else:
            mcu_endstop = endstop['endstop']
            changed_invert = pin_params['invert'] != endstop['invert']
            changed_pullup = pin_params['pullup'] != endstop['pullup']
            if changed_invert or changed_pullup:
                raise self.printer.config_error(
                    "Printer rail %s shared endstop pin %s must specify "
                    "the same pullup/invert settings" % (
                        self.get_name(), pin_name))
        return mcu_endstop


    def add_stepper(self, stepper_obj, endstop_pin=None, endstop_name=None):
        if not self.steppers:
            self.get_commanded_position = stepper_obj.get_commanded_position
            self.calc_position_from_coord = \
                stepper_obj.calc_position_from_coord

        self.steppers.append(stepper_obj)

        # Bind to explicit/default rail endstop only if one exists
        if endstop_pin is not None:
            mcu_endstop = self.lookup_endstop(endstop_pin, endstop_name or stepper_obj.get_name(short=True))
            mcu_endstop.add_stepper(stepper_obj)

        elif self.endstop_pin is not None:
            mcu_endstop = self.lookup_endstop(self.endstop_pin, self.name)
            mcu_endstop.add_stepper(stepper_obj)

        # Bind to all extra named endstops
        for mcu_endstop, name in self.extra_endstops:
            is_virtual = self.is_endstop_virtual(name)
            should_bind = (not is_virtual) or (stepper_obj is self.steppers[-1])
            if should_bind:
                try:
                    mcu_endstop.add_stepper(stepper_obj)
                except Exception as e:
                    logging.info("MMU: Not possible to add stepper %s to endstop %s because: %s", stepper_obj.get_name(), name, str(e))


    def add_stepper_from_config(self, config):
        stepper_obj = stepper.PrinterStepper(
            config, self.stepper_units_in_radians)
        self.add_stepper(stepper_obj, config.get('endstop_pin', None))


    def setup_itersolve(self, alloc_func, *params):
        for stepper_obj in self.steppers:
            stepper_obj.setup_itersolve(alloc_func, *params)


    def set_trapq(self, trapq):
        for stepper_obj in self.steppers:
            stepper_obj.set_trapq(trapq)


    def set_position(self, coord):
        for stepper_obj in self.steppers:
            stepper_obj.set_position(coord)


    # -------------------------------------------------------------------------
    # MMU extra endstop support
    # -------------------------------------------------------------------------

    def _parse_extra_endstops(self, config):
        raw = config.get('extra_endstops', None)
        if raw is None:
            return []

        result = []
        for entry in raw.replace('\n', ',').split(','):
            entry = entry.strip()
            if not entry:
                continue
            if '=' not in entry:
                raise config.error(
                    "Invalid extra_endstops entry '%s' in section '%s' "
                    "(expected name=pin or name=default)"
                    % (entry, config.get_name()))
            name, target = entry.split('=', 1)
            name = name.strip()
            target = target.strip()
            if not name or not target:
                raise config.error(
                    "Invalid extra_endstops entry '%s' in section '%s'"
                    % (entry, config.get_name()))
            if name == "default":
                raise config.error(
                    "extra_endstops may not use reserved name 'default' "
                    "in section '%s'" % (config.get_name(),))
            result.append((name, target))
        return result


    def add_extra_endstop(self, pin, name, register=True, bind_rail_steppers=True, mcu_endstop=None):
        # 'name' is the selectable symbolic name.
        # 'pin' is either:
        #   - 'default'
        #   - a real pin spec
        #   - a virtual endstop pin spec
        if name == "default":
            raise self.config.error(
                "Extra endstop may not use reserved name 'default'")
        if self.has_endstop(name):
            raise self.config.error(
                "Extra endstop '%s' defined more than once" % (name,))

        is_default_alias = (pin == "default")
        is_virtual = (not is_default_alias and 'virtual_endstop' in pin)

        if is_default_alias and self.default_mcu_endstop is None:
            raise self.config.error(
                "extra_endstops entry '%s=default' requires endstop_pin "
                "to be configured in section '%s'"
                % (name, self.config.get_name()))

        if is_virtual:
            if name not in self.virtual_endstops:
                self.virtual_endstops.append(name)
            else:
                raise self.config.error(
                    "Extra virtual endstop '%s' defined more than once" % (name,))

        if mcu_endstop is None:
            if is_default_alias:
                # Alias to existing default endstop
                mcu_endstop = self.default_mcu_endstop
                bind_rail_steppers = False

            elif is_virtual:
                ppins = self.printer.lookup_object('pins')
                mcu_endstop = ppins.setup_pin('endstop', pin)

            else:
                # Physical extra endstop
                display_name = "%s:%s" % (self.get_name(short=True), name)
                mcu_endstop = self.lookup_endstop(pin, display_name, register=False, extra=True)

        self.extra_endstops.append((mcu_endstop, name))

        if bind_rail_steppers and self.steppers:
            steppers = self.steppers if not is_virtual else [self.steppers[-1]]
            for s in steppers:
                try:
                    mcu_endstop.add_stepper(s)
                except Exception as e:
                    logging.info(
                        "MMU: Not possible to add stepper %s to endstop %s because: %s",
                        s.get_name(), name, str(e))

        if register:
            display_name = "%s:%s" % (self.get_name(short=True), name)
            self.query_endstops.register_endstop(mcu_endstop, display_name)

        return mcu_endstop


    def get_extra_endstop_names(self):
        return [x[1] for x in self.extra_endstops]


    # Returns the selected endstop as list to match get_endstops()
    def get_extra_endstop(self, name):
        for x in self.extra_endstops:
            if x[1] == name:
                return [x]
        return None


    def is_endstop_virtual(self, name):
        return name in self.virtual_endstops if name else False


    def get_homing_endstops(self, endstop_name=None):
        # Default rail endstop if omitted or explicitly 'default'
        if endstop_name in (None, "", "default"):
            if self.default_mcu_endstop is None:
                raise self.printer.command_error("No default endstop configured for rail '%s'" % (self.get_name(short=True),))
            return self.get_endstops()

        extra = self.get_extra_endstop(endstop_name)
        if extra is not None:
            return extra

        valid = self.get_extra_endstop_names()
        if self.default_mcu_endstop is not None:
            valid = ["default"] + valid

        raise self.printer.command_error(
            "Unknown endstop '%s' for rail '%s' (valid: %s)"
            % (endstop_name, self.get_name(short=True),
               ", ".join(valid) if valid else "<none>"))


    def has_endstop(self, endstop_name):
        if endstop_name in (None, "", "default"):
            return self.default_mcu_endstop is not None
        return self.get_extra_endstop(endstop_name) is not None

    # -------------------------------------------------------------------------
    # MMU rail homing support
    # -------------------------------------------------------------------------

    def set_direction(self, direction):
        for stepper_obj in self.steppers:
            stepper_obj.set_dir_inverted(direction)


    def home(self, mstepper, forcepos, movepos, endstop_name=None):
        hi = self.get_homing_info()
        endstops = self.get_homing_endstops(endstop_name)
    
        # Convert to vector form
        startpos = [forcepos, 0., 0., 0.]
        homepos = [movepos, 0., 0., 0.]
    
        # Force logical start position
        mstepper.set_position(startpos)

        init_mcu_pos = mstepper.get_steppers()[0].get_mcu_position()
    
        # First pass
        hmove = HomingMove(mstepper.printer, endstops, mstepper)
        trigpos = hmove.homing_move(homepos, hi.speed)
    
        # Second pass (retract + re-home)
        if hi.retract_dist:
            # IMPORTANT: use ORIGINAL homing vector (forcepos -> homepos)
            move_d = movepos - forcepos
    
            if move_d:
                retract = min(abs(move_d), hi.retract_dist)
                retractpos_x = movepos - math.copysign(retract, move_d)
                retractpos = [retractpos_x, 0., 0., 0.]
    
                mstepper.do_move(retractpos[0], hi.retract_speed, mstepper.homing_accel) # <toolhead>mstepper.move()
    
                second_start_x = retractpos_x - math.copysign(retract, move_d)
                second_start = [second_start_x, 0., 0., 0.]
                mstepper.set_position(second_start)
    
                hmove = HomingMove(mstepper.printer, endstops, mstepper)
                trigpos = hmove.homing_move(homepos, hi.second_homing_speed)
    
                if hmove.check_no_movement() is not None:
                    raise mstepper.printer.command_error("Endstop still triggered after retract")
    
        # Finalize
        trig_mcu_pos = None
        stepper_name = mstepper.get_name()
        for sp in hmove.stepper_positions:
            if sp.stepper_name == stepper_name:
                trig_mcu_pos = sp.trig_pos
                break
        if trig_mcu_pos is None:
            raise mstepper.printer.command_error(f"Unable to determine trigger position for stepper '{stepper_name}'")
        final_mcu_pos = mstepper.get_steppers()[0].get_mcu_position()
        travelled = (trig_mcu_pos - init_mcu_pos) * mstepper.get_steppers()[0].get_step_dist()
    
        # Return distance travelled to home (trigger point)
        return travelled
    
    
def MmuLookupRail(config, need_position_minmax=True, default_position_endstop=None, units_in_radians=False):
    rail = MmuGenericRail(config, need_position_minmax, default_position_endstop, units_in_radians)
    rail.add_stepper_from_config(config)
    return rail


def MmuLookupMultiRail(config, need_position_minmax=True, default_position_endstop=None, units_in_radians=False):
    rail = MmuLookupRail(config, need_position_minmax, default_position_endstop, units_in_radians)
    for i in range(1, 99):
        if not config.has_section(config.get_name() + str(i)):
            break
        rail.add_stepper_from_config(
            config.getsection(config.get_name() + str(i)))
    return rail



# -----------------------------------------------------------------------------------------------------------
# Extend manual stepper to allow selection of endstops on mmu rail
# -----------------------------------------------------------------------------------------------------------

class MmuStepper(ManualStepper):

    cmd_MMU_STEPPER_help = (
        "Command a manually configured MMU stepper. "
        "Use ENDSTOP=<name> with STOP_ON_ENDSTOP to select an extra endstop. "
        "If ENDSTOP is omitted or 'default', the default rail endstop is used."
    )

    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()

        has_default_endstop = config.get('endstop_pin', None) is not None
        has_extra_endstops = config.get('extra_endstops', None) is not None
#PAUL        self.homing_move_dist = config.getfloat('homing_move_dist', None) # PAUL

        if has_default_endstop or has_extra_endstops:
            self.can_home = True
            self.rail = MmuLookupRail(config, need_position_minmax=False, default_position_endstop=0.)
            self.steppers = self.rail.get_steppers()
        else:
            self.can_home = False
            self.rail = stepper.PrinterStepper(config)
            self.steppers = [self.rail]

        self.velocity = config.getfloat('velocity', 80., above=0.) # PAUL was 5
        self.accel = self.homing_accel = config.getfloat('accel', 100., minval=0.) # PAUL was 0
        self.next_cmd_time = 0.
        self.commanded_pos = 0.
        self.pos_min = config.getfloat('position_min', None)
        self.pos_max = config.getfloat('position_max', None)

        # Setup iterative solver
        self.motion_queuing = self.printer.load_object(config, 'motion_queuing')
        self.trapq = self.motion_queuing.allocate_trapq()
        self.trapq_append = self.motion_queuing.lookup_trapq_append()
        self.rail.setup_itersolve('cartesian_stepper_alloc', b'x')
        self.rail.set_trapq(self.trapq)

        # Registered with toolhead as an extra axis
        self.axis_gcode_id = None
        self.instant_corner_v = 0.
        self.gaxis_limit_velocity = self.gaxis_limit_accel = 0.

        # Register MMU_STEPPER instead of MANUAL_STEPPER
        stepper_name = self.name.split()[1]
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command('MMU_STEPPER', "STEPPER", stepper_name, self.cmd_MMU_STEPPER, desc=self.cmd_MMU_STEPPER_help)
        gcode.register_mux_command('MMU_STEPPER_STATUS', "STEPPER", stepper_name, self.cmd_MMU_STEPPER_STATUS, desc=self.cmd_MMU_STEPPER_STATUS_help)


    def do_homing_move(self, movepos, speed, accel, probe_pos, triggered, check_trigger, endstop_name=None):
        if not self.can_home:
            raise self.printer.command_error("No endstop for this MMU stepper")

        self.homing_accel = accel
        pos = [movepos, 0., 0., 0.]

        logging.info(f"PAUL: do_homing_move(endstop_name={endstop_name})")
        if hasattr(self.rail, "get_homing_endstops"):
            endstops = self.rail.get_homing_endstops(endstop_name)
        else:
            if endstop_name not in (None, "", "default"):
                raise self.printer.command_error("This stepper does not support named endstops")
            endstops = self.rail.get_endstops()
        logging.info(f"PAUL: found endstops={endstops}")

        phoming = self.printer.lookup_object('homing')
        trigpos = phoming.manual_home(self, endstops, pos, speed, probe_pos, triggered, check_trigger)
        self.sync_print_time()
        haltpos = self.get_position()

        return {
            "trig_pos": trigpos[0],
            "halt_pos": haltpos[0],
            "move_pos": movepos,
        }


    def do_home_rail(self, endstop_name=None):
        if not self.can_home:
            raise self.printer.command_error("No endstop for this MMU stepper")

        position_min, position_max = self.rail.get_range()
        hi = self.rail.get_homing_info()

        homepos = hi.position_endstop
        if hi.positive_dir:
            forcepos = homepos - 1.5 * (homepos - position_min)
        else:
            forcepos = homepos + 1.5 * (position_max - homepos)

        if forcepos == homepos:
            if hi.move_dist is None or hi.move_dist <= 0.:
                raise self.printer.command_error(
                    "Cannot home mmu_stepper: forcepos equals homepos "
                    f"(forcepos={forcepos}, homepos={homepos}). "
                    "Check position_min, position_max, position_endstop "
                    "or configure homing_move_dist."
                )

            if hi.positive_dir:
                forcepos = homepos - hi.move_dist
            else:
                forcepos = homepos + hi.move_dist

        result = self.rail.home(self, forcepos, homepos, endstop_name=endstop_name)
        self.sync_print_time()
        return result


    def cmd_MMU_STEPPER(self, gcmd):
        if gcmd.get('GCODE_AXIS', None) is not None:
            return self.command_with_gcode_axis(gcmd)

        if self.axis_gcode_id is not None:
            raise gcmd.error("Must unregister from gcode axis first")

        enable = gcmd.get_int('ENABLE', None)
        if enable is not None:
            self.do_enable(enable)

        setpos = gcmd.get_float('SET_POSITION', None)
        if setpos is not None:
            self.do_set_position(setpos)

        speed = gcmd.get_float('SPEED', self.velocity, above=0.)
        accel = gcmd.get_float('ACCEL', self.accel, minval=0.)

        # New test hook: HOME=1 uses the two-pass rail homing path
        home_request = gcmd.get_int('HOME', 0)
        if home_request:
            endstop_name = gcmd.get('ENDSTOP', None)
    
            success = False
            home_result = None
            try:
                home_result = self.do_home_rail(endstop_name)
                success = True
            finally:
                current_pos = self.commanded_pos
    
            used_endstop = (
                endstop_name if endstop_name not in (None, "", "default")
                else "default"
            )
            msg = (
                f"PAUL: {self.name}: HOME {'ok' if success else 'failed'} "
                f"endstop={endstop_name} current_pos={current_pos:.5f} home_result={home_result}"
            )
            gcmd.respond_info(msg)
            return

        homing_move = gcmd.get('STOP_ON_ENDSTOP', None)
        if homing_move is not None:
            old_map = {'-2': 'try_inverted_home', '-1': 'inverted_home', '1': 'home', '2': 'try_home'}.get(homing_move)
            if old_map is not None:
                pconfig = self.printer.lookup_object('configfile')
                pconfig.deprecate_gcode("MMU_STEPPER", "STOP_ON_ENDSTOP", homing_move)
                homing_move = old_map

            is_try = homing_move.startswith('try_')
            homing_move = homing_move[is_try * 4:]

            is_inverted = homing_move.startswith('inverted_')
            homing_move = homing_move[is_inverted * 9:]

            if homing_move not in ["probe", "home"]:
                raise gcmd.error("Unknown STOP_ON_ENDSTOP request")

            is_probe = (homing_move == "probe")
            endstop_name = gcmd.get('ENDSTOP', None)

            movepos = gcmd.get_float('MOVE')
            if (
                (self.pos_min is not None and movepos < self.pos_min) or
                (self.pos_max is not None and movepos > self.pos_max)
            ):
                raise gcmd.error("Move out of range")

            success = False
            try:
                if_probe = True # PAUL FORCE SO I DON"T FORGET
                home_result = self.do_homing_move(movepos, speed, accel, is_probe, not is_inverted, not is_try, endstop_name=endstop_name)
                success = True
            except Exception as e:
                raise e
            finally:
                current_pos = self.commanded_pos

            used_endstop = endstop_name if endstop_name not in (None, "", "default") else "default"
            msg = (
                f"PAUL: {self.name}: homing {'ok' if success else 'failed'} "
                f"endstop={used_endstop} target={movepos:.5f} pos={current_pos:.5f}"
            )

            # If do_homing_move() later returns structured info, include it.
            if success and isinstance(home_result, dict):
                msg += str(home_result)

            gcmd.respond_info(msg)

        elif gcmd.get_float('MOVE', None) is not None:
            movepos = gcmd.get_float('MOVE')
            if (
                (self.pos_min is not None and movepos < self.pos_min) or
                (self.pos_max is not None and movepos > self.pos_max)
            ):
                raise gcmd.error("Move out of range")

            sync = gcmd.get_int('SYNC', 1)
            self.do_move(movepos, speed, accel, sync)
            gcmd.respond_info(f"PAUL: {self.name}: move ok target={movepos:.5f} pos={self.commanded_pos:.5f}")

        elif gcmd.get_int('SYNC', 0):
            self.sync_print_time()

    cmd_MMU_STEPPER_STATUS_help = ("Report MMU stepper position and endstop configuration")
    def cmd_MMU_STEPPER_STATUS(self, gcmd):
        rail = self.rail
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()

        def _format_endstop_state(estop_obj):
            try:
                if hasattr(estop_obj, "query_endstop"):
                    return "TRIGGERED" if estop_obj.query_endstop(print_time) else "open"
            except Exception:
                pass

            try:
                # Some wrappers expose get_status()
                if hasattr(estop_obj, "get_status"):
                    status = estop_obj.get_status(print_time)
                    if isinstance(status, dict):
                        if "triggered" in status:
                            return "TRIGGERED" if status["triggered"] else "open"
                        if "state" in status:
                            return str(status["state"])
            except Exception:
                pass

            return "unknown"

        # Position
        pos = self.commanded_pos
        gcmd.respond_info(f"{self.name}: commanded_pos={pos:.10f}")

        # Default endstop
        default_estop = rail.default_mcu_endstop
        if default_estop:
            gcmd.respond_info(". Default endstop: present")
        else:
            gcmd.respond_info(". Default endstop: NONE")
        gcmd.respond_info(f". Default endstops: {self.rail.get_endstops()}")

        # Extra endstops
        names = rail.get_extra_endstop_names()
        if not names:
            gcmd.respond_info(". Extra endstops: NONE")

        else:
            gcmd.respond_info(". Extra endstops:")
            for name in names:
                is_virtual = False
                is_alias = False
                estop_obj = None
                estop_type = "unknown"
                estop_state = "unknown"

                try:
                    is_virtual = rail.is_endstop_virtual(name)
                except Exception:
                    pass

                try:
                    estop = rail.get_extra_endstop(name)
                    estop_obj = estop[0][0] if estop else None
                    estop_type = estop_obj.__class__.__name__ if estop_obj else "unknown"
                    estop_state = _format_endstop_state(estop_obj) if estop_obj else "unknown"
                    if default_estop is not None and estop_obj is default_estop:
                        is_alias = True
                except Exception:
                    estop_type = "unknown/error"

                flag = " (default, virtual)" if is_alias and is_virtual else \
                       " (default)" if is_alias else \
                       " (virtual)" if is_virtual else ""
                gcmd.respond_info(f".    - {name}{flag} [{estop_type}] [state: {estop_state}]")

        # Stepper positions
        gcmd.respond_info(". Steppers:")
        for s in self.steppers:
            try:
                mcu_pos = s.get_mcu_position()
                cmd_pos = s.get_commanded_position()
                gcmd.respond_info(
                    f".   - {s.get_name(short=True)}: "
                    f"cmd={cmd_pos:.5f} mcu={mcu_pos}"
                )
            except Exception as e:
                gcmd.respond_info(
                    f".   - {s.get_name(short=True)}: (error: {str(e)})"
                )

        # Summary line
        summary = []
        summary.append(f"pos={pos:.3f}")
        summary.append(f"default={'yes' if getattr(rail, 'default_mcu_endstop', None) else 'no'}")
        summary.append(f"extra={len(names)}")

        gcmd.respond_info(". Summary: " + ", ".join(summary))

#    # Register as a gcode axis
#    def set_gcode_axis(self, gcode_axis="M", instant_corner_v=1., limit_velocity=999999.9, limit_accel=999999.9):
#        gcode_move = self.printer.lookup_object("gcode_move")
#        toolhead = self.printer.lookup_object('toolhead')
#        gcode_axis = gcode_axis.upper()
#
#        if self.axis_gcode_id is not None:
#            raise gcmd.error("Must unregister axis first")
#
#        if (len(gcode_axis) != 1 or gcode_axis in "XYZEFN"):
#            raise gcmd.error("Not a valid GCODE_AXIS")
#
#        for ea in toolhead.get_extra_axes():
#            if ea is not None and ea.get_axis_gcode_id() == gcode_axis:
#                raise gcmd.error("Axis '%s' already registered" % (gcode_axis,))
#
#        self.axis_gcode_id = gcode_axis
#        self.instant_corner_v = instant_corner_v
#        self.gaxis_limit_velocity = limit_velocity
#        self.gaxis_limit_accel = limit_accel
#        toolhead.add_extra_axis(self, self.commanded_pos)
#        logging.info("PAUL: set_gcode_axis")
#
#
#    def unset_gcode_axis(self): # PAUL added
#        toolhead = self.printer.lookup_object('toolhead')
#        toolhead.remove_extra_axis(self)
#        self.axis_gcode_id = None
#        logging.info("PAUL: unset_gcode_axis")



def load_config_prefix(config):
    return MmuStepper(config)
