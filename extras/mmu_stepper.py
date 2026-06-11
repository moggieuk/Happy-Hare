# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implements [mmu_stepper] klipper object
#
# The MmuStepper is a hybrid stepper abstraction that combines Klipper’s
# ExtruderStepper and ManualStepper behaviors into a single, flexible object
# adding additional homing capabilities.
#
# It allows a stepper (typically an MMU gear motor or extruder motor) to
# dynamically switch between:
#   - manual/cartesian motion (independent or synced to another MMU stepper)
#   - extruder motion (following a PrinterExtruder’s motion queue)
#
# Key capabilities:
#   - Operate as a standalone manual stepper with optional homing/endstops
#   - Sync to another MmuStepper’s manual motion (shared trapq)
#   - Sync to a PrinterExtruder (standard Klipper extruder semantics)
#
# The class is designed for MMU systems where multiple gear steppers and
# an extruder must coordinate motion, hand off control, and remain in sync
# without losing position tracking.
#
# Internally, a single PrinterStepper is reused while switching between
# kinematics (cartesian/manual vs extruder) and motion queues as needed.
#
# Implementation overview:
#  - ExtruderStepper creates and owns the single real PrinterStepper
#  - MmuGenericRail wraps that existing stepper when rail/endstop behavior is needed
#  - manual mode and extruder mode both reuse that same underlying stepper
#  - mode switching is done by changing the stepper kinematics and attached trapq
#  - written with self-contained gcode commands for standalone operation but
#    designed to be wrapped by higher level driver logic
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
from kinematics.extruder import ExtruderStepper, PrinterExtruder
from .                   import force_move
from .homing             import HomingMove


# -----------------------------------------------------------------------------------------------------------
# MmuGenericRail: single-stepper rail with multiple endstops, direction reversal, etc
# -----------------------------------------------------------------------------------------------------------

class MmuGenericRail:

    mcu_endstop_tracking = {} # To aid debugging only

    def __init__(self, stepper_obj, config, need_position_minmax=True, default_position_endstop=None, units_in_radians=False):

        self.printer = config.get_printer()
        self.config = config
        self.name = config.get_name()
        self.stepper_units_in_radians = units_in_radians

        # Single wrapped stepper supplied by caller
        self.stepper = stepper_obj

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

            # Save mcu_endstop for debugging
            MmuGenericRail.record_mcu_endstop(self.default_mcu_endstop, self.name)

        # Primary endstop position
        if self.default_mcu_endstop is not None:
            if hasattr(self.default_mcu_endstop, "get_position_endstop"):
                self.position_endstop = self.default_mcu_endstop.get_position_endstop()
            elif default_position_endstop is None:
                self.position_endstop = config.getfloat('position_endstop')
            else:
                self.position_endstop = config.getfloat('position_endstop', default_position_endstop)
        else:
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
            if (self.position_endstop < self.position_min
                    or self.position_endstop > self.position_max):
                raise config.error("position_endstop in section '%s' must be between position_min and position_max" % (self.name))

        # Homing mechanics
        self.endstop_is_virtual = (self.endstop_pin is not None and 'virtual_endstop' in self.endstop_pin)
        self.homing_speed = config.getfloat('homing_speed', 10.0, above=0.)
        self.second_homing_speed = config.getfloat('second_homing_speed', self.homing_speed / 2., above=0.)
        self.homing_retract_speed = config.getfloat('homing_retract_speed', self.homing_speed, above=0.)
        self.homing_retract_dist = config.getfloat('homing_retract_dist', 0 if self.endstop_is_virtual else 5., minval=0.)
        self.homing_positive_dir = config.getboolean('homing_positive_dir', None)
        self.homing_move_dist = config.getfloat('homing_move_dist', None, above=0.)

        if self.default_mcu_endstop is not None:
            if self.homing_positive_dir is None:
                axis_len = self.position_max - self.position_min
                if self.position_endstop <= self.position_min + axis_len / 4.:
                    self.homing_positive_dir = False
                elif self.position_endstop >= self.position_max - axis_len / 4.:
                    self.homing_positive_dir = True
                else:
                    raise config.error("Unable to infer homing_positive_dir in section '%s'" % (self.name))
                config.getboolean('homing_positive_dir', self.homing_positive_dir)

            elif (
                (self.homing_positive_dir and self.position_endstop == self.position_min) or
                (not self.homing_positive_dir and self.position_endstop == self.position_max)
            ):
                raise config.error("Invalid homing_positive_dir / position_endstop in '%s'" % (self.name))
        else:
            if self.homing_positive_dir is None:
                self.homing_positive_dir = False

        # Bind the wrapped stepper to the default endstop, if any
        if self.default_mcu_endstop is not None:
            self.default_mcu_endstop.add_stepper(self.stepper)
            self.endstops.append((self.default_mcu_endstop, self.name))

        # Parse and bind extra selectable endstops
        for endstop_name, endstop_target in self._parse_extra_endstops(config):
            self.add_extra_endstop(endstop_target, endstop_name)

        # Expose same helpers GenericPrinterRail-style callers may expect
        self.get_commanded_position = self.stepper.get_commanded_position
        self.calc_position_from_coord = self.stepper.calc_position_from_coord

        # Event handlers
        self.printer.register_event_handler('klippy:disconnect', self.handle_disconnect)


    def handle_disconnect(self):
        # Avoid stale debugging info
        MmuGenericRail.mcu_endstop_tracking = {}


    @staticmethod
    def record_mcu_endstop(mcu_endstop, name):
        # Record mcu_endstop for debugging
        if mcu_endstop in MmuGenericRail.mcu_endstop_tracking:
            logging.info(f"MMU: mcu_endstop already recorded for {name}")
            MmuGenericRail.mcu_endstop_tracking[mcu_endstop].append(name)
        else:
            MmuGenericRail.mcu_endstop_tracking[mcu_endstop] = [name]


    # -------------------------------------------------------------------------
    # Generic rail-like API
    # -------------------------------------------------------------------------

    def get_name(self, short=False):
        if short:
            short_name = self.name.split()[-1]
            if "_" in short_name:
                return short_name.split("_")[0]
            return short_name
        return self.name


    def get_range(self):
        return self.position_min, self.position_max


    def get_homing_info(self):
        HomingInfo = collections.namedtuple('homing_info',
            ['speed', 'position_endstop', 'retract_speed', 'retract_dist',
             'positive_dir', 'second_homing_speed', 'move_dist'])
        return HomingInfo(
            self.homing_speed,
            self.position_endstop,
            self.homing_retract_speed,
            self.homing_retract_dist,
            self.homing_positive_dir,
            self.second_homing_speed,
            self.homing_move_dist)


    def get_steppers(self):
        return [self.stepper]


    def get_endstops(self):
        return list(self.endstops)


    def lookup_endstop(self, endstop_pin, name, register=True):
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(endstop_pin, True, True)

        pin_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])

        estop = self.endstop_map.get(pin_name, None)
        if estop is None:
            mcu_endstop = ppins.setup_pin('endstop', endstop_pin)
            self.endstop_map[pin_name] = {
                'endstop': mcu_endstop,
                'invert': pin_params['invert'],
                'pullup': pin_params['pullup'],
            }
            if register:
                self.query_endstops.register_endstop(mcu_endstop, name)
        else:
            mcu_endstop = estop['endstop']
            changed_invert = pin_params['invert'] != estop['invert']
            changed_pullup = pin_params['pullup'] != estop['pullup']
            if changed_invert or changed_pullup:
                raise self.printer.config_error("Printer rail %s shared endstop pin %s must specify the same pullup/invert settings" % (self.get_name(), pin_name))

        return mcu_endstop


    def setup_itersolve(self, alloc_func, *params):
        self.stepper.setup_itersolve(alloc_func, *params)


    def set_trapq(self, trapq):
        self.stepper.set_trapq(trapq)


    def set_position(self, coord):
        self.stepper.set_position(coord) # Resets mcu position on underlying MCU_Stepper


    # -------------------------------------------------------------------------
    # Extra endstop support
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
                raise config.error("Invalid extra_endstops entry '%s' in section '%s' (expected name=pin or name=default)" % (entry, config.get_name()))
            name, target = entry.split('=', 1)
            name = name.strip()
            target = target.strip()
            if not name or not target:
                raise config.error("Invalid extra_endstops entry '%s' in section '%s'" % (entry, config.get_name()))
            if name == "default":
                raise config.error("extra_endstops may not use reserved name 'default' in section '%s'" % (config.get_name(),))
            result.append((name, target))
        return result


    def add_extra_endstop(self, pin, name, register=True, bind_steppers=True, mcu_endstop=None):
        if name == "default":
            raise self.config.error("Extra endstop may not use reserved name 'default'")

        if self.has_endstop(name):
            raise self.config.error("Extra endstop '%s' defined more than once" % (name))

        # If pin is None, then expect mcu_endstop to be set. This is used for
        # software implemented endstop like that of an analog sensor
        is_default_alias = (pin == "default")
        is_virtual = (not is_default_alias and pin and 'virtual_endstop' in pin)

        if is_default_alias and self.default_mcu_endstop is None:
            raise self.config.error("extra_endstops entry '%s=default' requires endstop_pin to be configured in section '%s'" % (name, self.config.get_name()))

        if is_virtual:
            if name not in self.virtual_endstops:
                self.virtual_endstops.append(name)
            else:
                raise self.config.error("Extra virtual endstop '%s' defined more than once" % (name))

        if mcu_endstop is None:
            if is_default_alias:
                mcu_endstop = self.default_mcu_endstop
                bind_steppers = False

            elif is_virtual:
                ppins = self.printer.lookup_object('pins')
                mcu_endstop = ppins.setup_pin('endstop', pin)

            else:
                mcu_endstop = self.lookup_endstop(pin, name, register=False)

            MmuGenericRail.record_mcu_endstop(mcu_endstop, name)

        if (mcu_endstop, name) not in self.extra_endstops:
            self.extra_endstops.append((mcu_endstop, name))

        if bind_steppers:
            self.bind_stepper(mcu_endstop, name)

        if register:
            self.query_endstops.register_endstop(mcu_endstop, name)

        return mcu_endstop


    def bind_stepper(self, mcu_endstop, name):
        try:
            mcu_endstop.add_stepper(self.stepper)
        except Exception as e:
            logging.info(
                f"MMU: Not possible to add stepper {self.stepper.get_name()} "
                f"to endstop {name} because: {e}"
            )


    def get_all_endstop_names(self):
        all_names = ['default'] if self.default_mcu_endstop is not None else []
        return all_names + self.get_extra_endstop_names()


    def get_extra_endstop_names(self):
        return [x[1] for x in self.extra_endstops]


    def get_extra_endstop(self, name):
        for x in self.extra_endstops:
            if x[1] == name:
                return [x]
        return None


    def is_endstop_virtual(self, name):
        return name in self.virtual_endstops if name else False


    def get_homing_endstops(self, endstop_name=None):
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

        raise self.printer.command_error("Unknown endstop '%s' for rail '%s' (valid: %s)" % (endstop_name, self.get_name(short=True), ", ".join(valid) if valid else "<none>"))


    def has_endstop(self, endstop_name):
        if endstop_name in (None, "", "default"):
            return self.default_mcu_endstop is not None
        return self.get_extra_endstop(endstop_name) is not None


    # -------------------------------------------------------------------------
    # Homing support
    # -------------------------------------------------------------------------

    def home(self, mstepper, forcepos, movepos, endstop_name=None):
        """
        Home the rail. Note it is not possible to use Klipper's Homing class because of assumptions about "toolhead"
        """
        hi = self.get_homing_info()
        endstops = self.get_homing_endstops(endstop_name)

        startpos = [forcepos, 0., 0., 0.]
        homepos = [movepos, 0., 0., 0.]

        mstepper.set_position(startpos)
        init_mcu_pos = mstepper.get_steppers()[0].get_mcu_position()

        hmove = HomingMove(mstepper.printer, endstops, mstepper)
        hmove.homing_move(homepos, hi.speed)

        if hi.retract_dist:
            move_d = movepos - forcepos
            if move_d:
                retract = min(abs(move_d), hi.retract_dist)
                retractpos_x = movepos - math.copysign(retract, move_d)
                retractpos = [retractpos_x, 0., 0., 0.]

                mstepper.do_move(retractpos[0], hi.retract_speed, mstepper.homing_accel)

                second_start_x = retractpos_x - math.copysign(retract, move_d)
                second_start = [second_start_x, 0., 0., 0.]
                mstepper.set_position(second_start)

                hmove = HomingMove(mstepper.printer, endstops, mstepper)
                hmove.homing_move(homepos, hi.second_homing_speed)

                if hmove.check_no_movement() is not None:
                    raise mstepper.printer.command_error("Endstop still triggered after retract")

        trig_mcu_pos = None
        stepper_name = mstepper.get_steppers()[0].get_name()
        for sp in hmove.stepper_positions:
            if sp.stepper_name == stepper_name:
                trig_mcu_pos = sp.trig_pos
                break
        if trig_mcu_pos is None:
            raise mstepper.printer.command_error("Unable to determine trigger position for stepper '%s'" % (stepper_name,))

        travelled = ((trig_mcu_pos - init_mcu_pos) * mstepper.get_steppers()[0].get_step_dist())
        return travelled


def MmuLookupRailFromStepper(stepper_obj, config, need_position_minmax=True, default_position_endstop=None, units_in_radians=False):
    return MmuGenericRail(
        stepper_obj, config,
        need_position_minmax=need_position_minmax,
        default_position_endstop=default_position_endstop,
        units_in_radians=units_in_radians)




# -----------------------------------------------------------------------------------------------------------
# Single-stepper MMU stepper with:
# - manual/MMU behavior but with ability to sync to other MmuSteppers
# - extruder-stepper compatible behavior including ability to sync to other extruders
# -----------------------------------------------------------------------------------------------------------

class MmuStepper(ExtruderStepper):

    MODE_MANUAL = "manual"
    MODE_EXTRUDER = "extruder"

    cmd_MMU_STEPPER_help = (
        "Command an MMU extruder stepper in manual mode. "
        "Use ENDSTOP=<name> with STOP_ON_ENDSTOP to select an extra endstop. "
        "If ENDSTOP is omitted or 'default', the default rail endstop is used."
    )
    cmd_MMU_STEPPER_STATUS_help = "Report MMU extruder position, endstop configuration, and motion mode"
    cmd_MMU_STEPPER_SYNC_MANUAL_MOTION_help = "Set MMU stepper manual motion queue"
    cmd_MMU_STEPPER_SET_MODE_help = "Set mode (manual/extruder) for this stepper"


    def __init__(self, config, default_mode=MODE_MANUAL, force_rail=False):

        # ------------------------------------------------------------------
        # ExtruderStepper initialization
        # ------------------------------------------------------------------
        # This creates:
        #   self.printer
        #   self.name                      (last token of section name)
        #   self.stepper                   (single PrinterStepper)
        #   self.sk_extruder
        #   stock PA config/state
        #   stock extruder commands
        #
        # It leaves the stepper configured for extruder kinematics initially
        # ------------------------------------------------------------------
        ExtruderStepper.__init__(self, config)


        # ------------------------------------------------------------------
        # ManualStepper initialization
        # ------------------------------------------------------------------

        # Preserve full section name for MMU commands / diagnostics
        self.full_name = config.get_name()

        # Manual/MMU configuration
        self.velocity = config.getfloat('velocity', 5., above=0.)
        self.accel = config.getfloat('accel', 0., minval=0.)
        self.homing_accel = config.getfloat('homing_accel', self.accel, minval=0.)
        self.next_cmd_time = 0.
        self.commanded_pos = 0.
        self.pos_min = config.getfloat('position_min', None)
        self.pos_max = config.getfloat('position_max', None)

        # Optional homing rail support
        self.has_default_endstop = config.get('endstop_pin', None) is not None
        self.has_extra_endstops = config.get('extra_endstops', None) is not None

        if force_rail or self.has_default_endstop or self.has_extra_endstops:
            self.can_home = self.has_default_endstop
            self.rail = MmuLookupRailFromStepper(self.stepper, config, need_position_minmax=False, default_position_endstop=0.)
            self.steppers = self.rail.get_steppers()
        else:
            self.can_home = False
            self.rail = self.stepper # A PrinterStepper created by ExtruderStepper init()
            self.steppers = [self.stepper]

        # Setup iterative solver and manual-mode trapq
        self.motion_queuing = self.printer.load_object(config, 'motion_queuing')
        self.manual_trapq = self.motion_queuing.allocate_trapq()
        self.trapq_append = self.motion_queuing.lookup_trapq_append()
        self.trapq = self.manual_trapq

        # Create manual/cartesian kinematics
        self.rail.setup_itersolve('cartesian_stepper_alloc', b'x')
        self.rail.set_trapq(self.trapq)
        self.sk_manual = self.stepper.get_stepper_kinematics()

        # Manual mode state
        self.manual_motion_queue = None
        self._manual_followers = set()

        # Registered with toolhead as an extra axis only in manual mode
        self.axis_gcode_id = None
        self.instant_corner_v = 0.
        self.gaxis_limit_velocity = self.gaxis_limit_accel = 0.

        # Set initial operating mode
        default_mode = config.getchoice('default_mode', {'manual': self.MODE_MANUAL, 'extruder': self.MODE_EXTRUDER}, default=default_mode)
        if default_mode == self.MODE_MANUAL:
            self._activate_manual_mode(initial=True)
        else:
            self._activate_extruder_mode_detached(initial=True)

        # Register MMU/manual commands
        stepper_name = self.full_name.split()[-1]
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command('MMU_STEPPER', 'STEPPER', stepper_name, self.cmd_MMU_STEPPER, desc=self.cmd_MMU_STEPPER_help)
        gcode.register_mux_command('MMU_STEPPER_STATUS', 'STEPPER', stepper_name, self.cmd_MMU_STEPPER_STATUS, desc=self.cmd_MMU_STEPPER_STATUS_help)
        gcode.register_mux_command("MMU_STEPPER_SYNC_MANUAL_MOTION", "STEPPER", stepper_name, self.cmd_MMU_STEPPER_SYNC_MANUAL_MOTION, desc=self.cmd_MMU_STEPPER_SYNC_MANUAL_MOTION_help)
        gcode.register_mux_command("MMU_STEPPER_SET_MODE", "STEPPER", stepper_name, self.cmd_MMU_STEPPER_SET_MODE, desc=self.cmd_MMU_STEPPER_SET_MODE_help)


    def get_mode_position(self):
        """
        Get simple position regardless of mode or synchronization state
        """
        if self.motion_mode == self.MODE_MANUAL:
            if self.manual_motion_queue is not None:
                source = self.printer.lookup_object(self.manual_motion_queue, None)
                if source is not None and isinstance(source, MmuStepper):
                    return source.commanded_pos
            return self.commanded_pos
        return self.stepper.get_commanded_position()


    # ----------------------------------------------------------------------
    # Mode helpers
    # ----------------------------------------------------------------------

    def _require_manual_mode(self, operation):
        if self.motion_mode != self.MODE_MANUAL:
            raise self.printer.command_error("%s is not allowed while '%s' is in extruder mode (motion_queue=%s)" % (operation, self.full_name, self.motion_queue))


    def _require_standalone_manual_mode(self, operation):
        self._require_manual_mode(operation)
        if self.manual_motion_queue is not None:
            raise self.printer.command_error("%s is not allowed while '%s' is synced to manual motion from '%s'" % (operation, self.full_name, self.manual_motion_queue))


    def _require_extruder_mode(self, operation):
        if self.motion_mode != self.MODE_EXTRUDER:
            raise self.printer.command_error("%s is not allowed while '%s' is in manual mode" % (operation, self.full_name))


    def _require_standalone_extruder_mode(self, operation):
        self._require_extruder_mode(operation)
        if self.motion_queue is not None:
            raise self.printer.command_error("%s is not allowed while '%s' is synced to extruder motion from '%s'" % (operation, self.full_name, self.motion_queue))


    def _require_detached_for_mode_change(self, operation):
        if self.motion_mode == self.MODE_EXTRUDER and self.motion_queue is not None:
            raise self.printer.command_error(
                "%s is not allowed while '%s' is synced to extruder motion from '%s'"
                % (operation, self.full_name, self.motion_queue))
        if self.motion_mode == self.MODE_MANUAL and self.manual_motion_queue is not None:
            raise self.printer.command_error(
                "%s is not allowed while '%s' is synced to manual motion from '%s'"
                % (operation, self.full_name, self.manual_motion_queue))


    def is_standalone_manual(self):
        return (self.motion_mode == self.MODE_MANUAL and self.manual_motion_queue is None)


    # ----------------------------------------------------------------------
    # Kinematics switching
    # ----------------------------------------------------------------------

    def activate_manual_mode(self):
        self._require_detached_for_mode_change("Activate manual mode")
        self._activate_manual_mode()


    def activate_extruder_mode(self):
        self._require_detached_for_mode_change("Activate extruder mode")
        self._activate_extruder_mode_detached()


    def _activate_manual_mode(self, initial=False):
        if not initial:
            self.flush_step_generation()

        pos = self.stepper.get_commanded_position()

        # Detach current trapq
        self.stepper.set_trapq(None)

        # Restore manual/cartesian kinematics
        self.stepper.set_stepper_kinematics(self.sk_manual)
        self.stepper.set_position([pos, 0., 0.])
        self.stepper.set_trapq(self.manual_trapq)

        self.commanded_pos = pos
        self.motion_mode = self.MODE_MANUAL
        self.motion_queue = None
        self.manual_motion_queue = None
        self.motion_queuing.check_step_generation_scan_windows()


    def _activate_extruder_mode_detached(self, initial=False):
        if not initial:
            self.flush_step_generation()

        pos = self.stepper.get_commanded_position()

        # Detach current trapq first
        self.stepper.set_trapq(None)

        self.stepper.set_stepper_kinematics(self.sk_extruder)

        printer_extruder = self.printer.lookup_object(self.name, None)
        if isinstance(printer_extruder, PrinterExtruder):
            self.stepper.set_position([printer_extruder.last_position, 0., 0.])
            self.stepper.set_trapq(printer_extruder.trapq)
        else:
            self.stepper.set_position([pos, 0., 0.])

        self.commanded_pos = pos
        self.motion_mode = self.MODE_EXTRUDER
        self.motion_queue = None
        self.manual_motion_queue = None
        self.motion_queuing.check_step_generation_scan_windows()


    def _activate_extruder_motion_queue(self, extruder):
        self.flush_step_generation()

        # Detach current trapq first
        self.stepper.set_trapq(None)

        # Restore extruder kinematics allocated by stock ExtruderStepper
        self.stepper.set_stepper_kinematics(self.sk_extruder)
        self.stepper.set_position([extruder.last_position, 0., 0.])
        self.stepper.set_trapq(extruder.get_trapq())

        self.commanded_pos = extruder.last_position
        self.motion_mode = self.MODE_EXTRUDER
        self.motion_queue = extruder.get_name()
        self.manual_motion_queue = None
        self.motion_queuing.check_step_generation_scan_windows()


    # ----------------------------------------------------------------------
    # Sync targets
    # ----------------------------------------------------------------------

    def unsync_manual(self):
        self._require_manual_mode("Unsync manual stepper")

        # Unregister with the source stepper
        source = self.printer.lookup_object(self.manual_motion_queue, None)
        if source:
            source.remove_manual_follower(self)

        pos = self.get_mode_position()
        self._activate_manual_mode()
        self.do_set_position(pos)


    def unsync_extruder(self):
        self._require_extruder_mode("Unsync extruder")
        self._activate_extruder_mode_detached()


    def switch_to_manual_mode(self):
        if self.motion_mode == self.MODE_EXTRUDER:
            if self.motion_queue is not None:
                self.unsync_extruder()
            self._activate_manual_mode()
        elif self.manual_motion_queue is not None:
            self.unsync_manual()


    def switch_to_extruder_mode(self):
        if self.motion_mode == self.MODE_MANUAL:
            if self.manual_motion_queue is not None:
                self.unsync_manual()
            self._activate_extruder_mode_detached()


    def sync_to_extruder(self, extruder_name):

        if not extruder_name:
            self._require_extruder_mode('Sync to extruder')
            self.unsync_extruder()
            return

        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder is None or not isinstance(extruder, PrinterExtruder):
            raise self.printer.command_error("'%s' is not a valid extruder" % (extruder_name,))

        self._require_standalone_extruder_mode('Sync to extruder')
        self._activate_extruder_motion_queue(extruder)


    def sync_to_manual_stepper(self, stepper_name):
        if not stepper_name:
            self._require_manual_mode('Sync to manual stepper')
            self.unsync_manual()
            return

        stepper_name = f"mmu_stepper {stepper_name}"
        source = self.printer.lookup_object(stepper_name, None)
        if source is None or not isinstance(source, MmuStepper):
            raise self.printer.command_error("'%s' is not a valid MMU stepper." % (stepper_name,))

        if source is self:
            # Unsync and reset manual mode
            self.unsync_manual()
            return

        if not source.is_standalone_manual():
            raise self.printer.command_error("MMU stepper '%s' is not in standalone manual mode." % (stepper_name,))

        self._require_standalone_manual_mode('Sync to manual stepper')
        self.flush_step_generation()
        source_pos = source.stepper.get_commanded_position()

        self.stepper.set_trapq(None)
        self.stepper.set_stepper_kinematics(self.sk_manual)
        self.stepper.set_position([source_pos, 0., 0.])
        self.stepper.set_trapq(source.manual_trapq)

        # Register with the source stepper for set_position() synchronization
        source.add_manual_follower(self)

        self.commanded_pos = source_pos
        self.motion_mode = self.MODE_MANUAL
        self.motion_queue = None
        self.manual_motion_queue = stepper_name
        self.motion_queuing.check_step_generation_scan_windows()


    def add_manual_follower(self, follower):
        self._manual_followers.add(follower)


    def remove_manual_follower(self, follower):
        self._manual_followers.discard(follower)


    # ----------------------------------------------------------------------
    # ManualStepper-compatible helpers
    # ----------------------------------------------------------------------

    def get_name(self):
        return self.name


    def sync_print_time(self):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        if self.next_cmd_time > print_time:
            toolhead.dwell(self.next_cmd_time - print_time)
        else:
            self.next_cmd_time = print_time


    def do_enable(self, enable):
        stepper_names = [s.get_name() for s in self.steppers]
        stepper_enable = self.printer.lookup_object('stepper_enable')
        stepper_enable.set_motors_enable(stepper_names, enable)


    def do_set_position(self, setpos):
        #self._require_standalone_manual_mode("SET_POSITION")
        if self.motion_mode == self.MODE_MANUAL and self.manual_motion_queue is None:
            # We are the driving manual stepper
            self.flush_step_generation()
            self.commanded_pos = setpos
            self.rail.set_position([setpos, 0., 0.])

            # Keep manual followers in sync to avoid stepcompress issues
            for follower in self._manual_followers:
                follower.commanded_pos = setpos
                follower.rail.set_position([setpos, 0., 0.])
        else:
            self.commanded_pos = setpos


    def _submit_move(self, movetime, movepos, speed, accel):
        self._require_manual_mode("MOVE")
        cp = self.commanded_pos
        dist = movepos - cp
        axis_r, accel_t, cruise_t, cruise_v = force_move.calc_move_time(dist, speed, accel)
        self.trapq_append(self.manual_trapq, movetime, accel_t, cruise_t, accel_t,
                          cp, 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
        self.commanded_pos = movepos
        return movetime + accel_t + cruise_t + accel_t


    def do_move(self, movepos, speed, accel, sync=True):
        self._require_standalone_manual_mode("MOVE")
        self.sync_print_time()
        self.next_cmd_time = self._submit_move(self.next_cmd_time, movepos, speed, accel)
        self.motion_queuing.note_mcu_movequeue_activity(self.next_cmd_time)
        if sync:
            self.sync_print_time()


    def do_homing_move(self, movepos, speed, accel, probe_pos, triggered, check_trigger, endstop_name=None):
        self._require_standalone_manual_mode("STOP_ON_ENDSTOP/HOMING_MOVE")
        self.homing_accel = accel
        pos = [movepos, 0., 0., 0.]

        if not isinstance(self.rail, MmuGenericRail):
            raise self.printer.command_error("No endstop for this MMU stepper")

        endstops = self.rail.get_homing_endstops(endstop_name)

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
        self._require_standalone_manual_mode("HOME")
        if not self.can_home:
            raise self.printer.command_error("No default endstop for this MMU stepper")

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
                    "(forcepos=%s, homepos=%s). "
                    "Check position_min, position_max, position_endstop "
                    "or configure homing_move_dist."
                    % (forcepos, homepos))
            if hi.positive_dir:
                forcepos = homepos - hi.move_dist
            else:
                forcepos = homepos + hi.move_dist

        result = self.rail.home(self, forcepos, homepos, endstop_name=endstop_name)
        self.sync_print_time()
        return result


    def command_with_gcode_axis(self, gcmd):
        gcode_move = self.printer.lookup_object("gcode_move")
        toolhead = self.printer.lookup_object('toolhead')
        gcode_axis = gcmd.get('GCODE_AXIS').upper()
        instant_corner_v = gcmd.get_float('INSTANTANEOUS_CORNER_VELOCITY', 1.,
                                          minval=0.)
        limit_velocity = gcmd.get_float('LIMIT_VELOCITY', 999999.9, above=0.)
        limit_accel = gcmd.get_float('LIMIT_ACCEL', 999999.9, above=0.)
        if self.axis_gcode_id is not None:
            if gcode_axis:
                raise gcmd.error("Must unregister axis first")
            # Unregister
            toolhead.remove_extra_axis(self)
            self.axis_gcode_id = None
            return
        if (len(gcode_axis) != 1 or not gcode_axis.isupper()
            or gcode_axis in "XYZEFN"):
            if not gcode_axis:
                # Request to unregister already unregistered axis
                return
            raise gcmd.error("Not a valid GCODE_AXIS")
        for ea in toolhead.get_extra_axes():
            if ea is not None and ea.get_axis_gcode_id() == gcode_axis:
                raise gcmd.error("Axis '%s' already registered" % (gcode_axis,))
        self.axis_gcode_id = gcode_axis
        self.instant_corner_v = instant_corner_v
        self.gaxis_limit_velocity = limit_velocity
        self.gaxis_limit_accel = limit_accel
        toolhead.add_extra_axis(self, self.commanded_pos)


    def process_move(self, print_time, move, ea_index):
        axis_r = move.axes_r[ea_index]
        start_pos = move.start_pos[ea_index]
        accel = move.accel * axis_r
        start_v = move.start_v * axis_r
        cruise_v = move.cruise_v * axis_r
        self.trapq_append(self.trapq, print_time,
                          move.accel_t, move.cruise_t, move.decel_t,
                          start_pos, 0., 0.,
                          1., 0., 0.,
                          start_v, cruise_v, accel)
        self.commanded_pos = move.end_pos[ea_index]


    def check_move(self, move, ea_index):
        # Check move is in bounds
        movepos = move.end_pos[ea_index]
        if ((self.pos_min is not None and movepos < self.pos_min)
            or (self.pos_max is not None and movepos > self.pos_max)):
            raise move.move_error()
        # Check if need to limit maximum velocity and acceleration
        axis_ratio = move.move_d / abs(move.axes_d[ea_index])
        limit_velocity = self.gaxis_limit_velocity * axis_ratio
        limit_accel = self.gaxis_limit_accel * axis_ratio
        if not move.is_kinematic_move and self.accel:
            limit_accel = min(limit_accel, self.accel * axis_ratio)
        move.limit_speed(limit_velocity, limit_accel)


    def calc_junction(self, prev_move, move, ea_index):
        diff_r = move.axes_r[ea_index] - prev_move.axes_r[ea_index]
        if diff_r:
            return (self.instant_corner_v / abs(diff_r))**2
        return move.max_cruise_v2


    def get_axis_gcode_id(self):
        return self.axis_gcode_id


    def get_trapq(self):
        return self.trapq


    # ----------------------------------------------------------------------
    # Toolhead wrappers to support homing
    # ----------------------------------------------------------------------

    def flush_step_generation(self):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()


    def get_position(self):
        return [self.commanded_pos, 0., 0., 0.]


    def set_position(self, newpos, homing_axes=""):
        self.do_set_position(newpos[0])


    def get_last_move_time(self):
        self.sync_print_time()
        return self.next_cmd_time


    def dwell(self, delay):
        self.next_cmd_time += max(0., delay)


    def drip_move(self, newpos, speed, drip_completion):
        # Submit move to trapq
        self.sync_print_time()
        start_time = self.next_cmd_time
        end_time = self._submit_move(start_time, newpos[0],
                                     speed, self.homing_accel)
        # Drip updates to motors
        self.motion_queuing.drip_update_time(start_time, end_time,
                                             drip_completion)
        # Clear trapq of any remaining parts of movement
        self.motion_queuing.wipe_trapq(self.trapq)


    def get_kinematics(self):
        return self


    def get_steppers(self):
        return [self.stepper]


    def calc_position(self, stepper_positions):
        return [stepper_positions[self.rail.get_name()], 0., 0.]


    # ----------------------------------------------------------------------
    # Status
    # ----------------------------------------------------------------------

    def get_status(self, eventtime):
        status = ExtruderStepper.get_status(self, eventtime) # pressure_advance, smooth_time, motion_queue
        status.update({
            'motion_mode': self.motion_mode,
            'manual_motion_queue': self.manual_motion_queue,
            'commanded_pos': self.commanded_pos,
        })
        return status


    # ----------------------------------------------------------------------
    # Gcode commands & helpers
    # ----------------------------------------------------------------------

    def _parse_stop_on_endstop(self, gcmd):
        homing_move = gcmd.get('STOP_ON_ENDSTOP', None)
        if homing_move is None:
            return None

        old_map = {
            '-2': 'try_inverted_home',
            '-1': 'inverted_home',
            '1': 'home',
            '2': 'try_home'
        }.get(homing_move)
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

        return {
            'is_probe': homing_move == "probe",
            'triggered': not is_inverted,
            'check_trigger': not is_try,
            'endstop_name': gcmd.get('ENDSTOP', None),
        }


    def cmd_MMU_STEPPER(self, gcmd):
        """
        Follow ManualStepper.cmd_MANUAL_STEPPER() as closely as possible,
        while adding:
          - HOME=1 support
          - ENDSTOP=<name> support for STOP_ON_ENDSTOP
        """
        if gcmd.get('GCODE_AXIS', None) is not None:
            self._require_standalone_manual_mode("GCODE_AXIS registration")
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
            msg = ("%s: HOME %s endstop=%s current_pos=%.5f home_result=%s" % (self.full_name, "ok" if success else "failed", used_endstop, current_pos, home_result))
            gcmd.respond_info(msg)
            return

        stop_cfg = self._parse_stop_on_endstop(gcmd)
        if stop_cfg is not None:
            movepos = gcmd.get_float('MOVE')
            if ((self.pos_min is not None and movepos < self.pos_min)
                    or (self.pos_max is not None and movepos > self.pos_max)):
                raise gcmd.error("Move out of range")

            success = False
            home_result = None
            try:
                home_result = self.do_homing_move(
                    movepos, speed, accel,
                    stop_cfg['is_probe'],
                    stop_cfg['triggered'],
                    stop_cfg['check_trigger'],
                    endstop_name=stop_cfg['endstop_name'])
                success = True
            finally:
                current_pos = self.commanded_pos

            used_endstop = (
                stop_cfg['endstop_name']
                if stop_cfg['endstop_name'] not in (None, "", "default")
                else "default")
            msg = ("%s: homing %s endstop=%s target=%.5f pos=%.5f" % (self.full_name, "ok" if success else "failed", used_endstop, movepos, current_pos))
            if success:
                msg += " " + str(home_result)
            gcmd.respond_info(msg)
            return

        elif gcmd.get_float('MOVE', None) is not None:
            movepos = gcmd.get_float('MOVE')
            if ((self.pos_min is not None and movepos < self.pos_min)
                    or (self.pos_max is not None and movepos > self.pos_max)):
                raise gcmd.error("Move out of range")
            sync = gcmd.get_int('SYNC', 1)
            self.do_move(movepos, speed, accel, sync)
            gcmd.respond_info("%s: move ok target=%.5f pos=%.5f" % (self.full_name, movepos, self.commanded_pos))
            return

        elif gcmd.get_int('SYNC', 0):
            self.sync_print_time()


    def cmd_MMU_STEPPER_STATUS(self, gcmd):
        endstops = gcmd.get_int('ENDSTOPS', 1) # Default to show endstops
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()

        def _format_endstop_state(estop_obj):
            try:
                if hasattr(estop_obj, "query_endstop"):
                    return "TRIGGERED" if estop_obj.query_endstop(print_time) else "open"
            except Exception:
                pass

            try:
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

        mcu_pos = self.stepper.get_mcu_position()
        cmd_pos = self.stepper.get_commanded_position()

        lines = [
            f"Stepper: {self.full_name}: mode_pos={self.get_mode_position():.4f}, cmd_pos={self.commanded_pos}, stepper.cmd_pos={cmd_pos:.4f}, stepper.mcu_pos={mcu_pos:.1f}",
            f"Motion mode: {self.motion_mode}"
        ]
        if self.motion_mode == self.MODE_EXTRUDER:
            lines.extend([
                f"Motion queue: {self.motion_queue}",
                f"Pressure advance: {self.pressure_advance:.6f}",
                f"Smooth time: {self.pressure_advance_smooth_time:.6f}"
            ])
        else:
            lines.extend([
                f"Manual motion queue: {self.manual_motion_queue}"
            ])

        # Endstops (if a rail)
        if endstops:
            if isinstance(self.rail, MmuGenericRail):
                default_estop = self.rail.default_mcu_endstop
                if default_estop:
                    for (estop_obj, estop_name) in self.rail.get_endstops():
                        estop_type = estop_obj.__class__.__name__
                        estop_pin = getattr(estop_obj, "_pin", "unknown")
                        if hasattr(estop_obj, "get_mcu"):
                            estop_type += f"({estop_obj.get_mcu().get_name()},{estop_pin},{id(estop_obj)})"
                        estop_state = _format_endstop_state(estop_obj)
                        lines.append(f"Default manual endstop: {estop_name} {estop_type} [state: {estop_state}]")
                else:
                    lines.append("Default manual endstop: NONE (cannot home rail)")

                names = self.rail.get_extra_endstop_names() if self.rail else []
                if not names:
                    lines.append("Extra manual endstops: NONE")
                else:
                    lines.append("Extra manual endstops:")
                    for name in names:
                        is_virtual = self.rail.is_endstop_virtual(name)
                        estop = self.rail.get_extra_endstop(name)
                        estop_obj = estop[0][0] if estop else None
                        if estop_obj:
                            estop_type = estop_obj.__class__.__name__
                            estop_pin = getattr(estop_obj, "_pin", "unknown")
                            if hasattr(estop_obj, "get_mcu"):
                                estop_type += f"({estop_obj.get_mcu().get_name()},{estop_pin},{id(estop_obj)})"
                        else:
                            estop_type = "unknown"
                        estop_state = _format_endstop_state(estop_obj) if estop_obj else "unknown"
                        is_alias = default_estop is not None and estop_obj is default_estop

                        flag = (
                            " (default, virtual)" if is_alias and is_virtual else
                            " (default)" if is_alias else
                            " (virtual)" if is_virtual else
                            ""
                        )
                        lines.append(f"- {name}{flag} {estop_type} [state: {estop_state}]")
            else:
                lines.append("No rail/endstops")

        gcmd.respond_info("\n".join(lines))


    def cmd_MMU_STEPPER_SYNC_MANUAL_MOTION(self, gcmd):
        source = gcmd.get('MOTION_SOURCE', None)
        if source == "":
            source = None
        self.sync_to_manual_stepper(source)
        gcmd.respond_info(f"MMU stepper '{self.full_name}' now syncing manual motion with '{source}'")


    def cmd_MMU_STEPPER_SET_MODE(self, gcmd):
        mode = gcmd.get('MODE')

        if mode == self.MODE_MANUAL:
            self.activate_manual_mode()
        elif mode == self.MODE_EXTRUDER:
            self.activate_extruder_mode()
        else:
            raise gcmd.error(f"Unknown mode: {mode}. Choices are manual/extruder")


# -----------------------------------------------------------------------------------------------------------
# Supports klipper loading of [mmu_stepper] object
# -----------------------------------------------------------------------------------------------------------

def load_config_prefix(config):
    return MmuStepper(config)
