# Happy Hare MMU Software
# Implementation of various selector variations:
#
# LinearSelector
#  - Stepper controlled linear movement with endstop
#  - Servo controlled filament gripping
#  + Supports type-A classic MMU's like ERCF and Tradrack
#
# VirtualSelector
#  - Used to simply select correct gear stepper
#  - For type-B AMS-like designs like 8-track
#
# Copyright (C) 2022  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

FILAMENT_HOLD_STATE = 2     # PAUL aka servo_move
FILAMENT_DRIVE_STATE = 1    # PAUL aka servo_down
FILAMENT_RELEASE_STATE = 0  # PAUL aka servo_up
FILAMENT_UNKNOWN_STATE = -1 # PAUL aka servo_unkonwn

################################################################################
# Virtual Selector
# Implements selector for type-B MMU's with gear driver per gate
################################################################################
class VirtualSelector():
    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu_toolhead = mmu.mmu_toolhead

    def reinit(self):
        pass

    def handle_connect(self):
        pass

    def select_gate(self, gate):
        self.mmu_toolhead.select_gear_stepper(gate) # Select correct drive stepper
        self.mmu._set_gate_selected(gate)

    def filament_drive(self): # PAUL aka _servo_down
        pass

    def filament_release(self): # PAUL aka _servo_up
        pass

    def filament_hold(self): # PAUL aka _servo_move
        pass

    def disable_motors(self): # PAUL TODO loop
        stepper_enable = self.mmu.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_disable(self.mmu_toolhead.get_last_move_time())

    def enable_motors(self): # PAUL TODO loop
        stepper_enable = self.mmu.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_enable(self.mmu_toolhead.get_last_move_time())

    def buzz_motor(self, motor):
        pass

    def get_status(self):
        return {}

    def get_mmu_config(self):
        return "TODO"


################################################################################
# Linear Selector
# Implements Linear Selector for type-A MMU's that uses stepper conrolled rail[0] on mmu toolhead
################################################################################
class LinearSelector():
    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu_toolhead = mmu.mmu_toolhead

        # Initialize required resources
        self.selector_rail = self.mmu_toolhead.get_kinematics().rails[0]
        self.selector_stepper = self.selector_rail.steppers[0]
        self.is_homed = False

        # Sub components
        self.servo = LinearSelectorServo(mmu)

        # Register GCODE commands specific to this module
        gcode = self.mmu.printer.lookup_object('gcode')
        gcode.register_command('MMU_SOAKTEST_SELECTOR', self.cmd_MMU_SOAKTEST_SELECTOR, desc = self.cmd_MMU_SOAKTEST_SELECTOR_help)

    def reinit(self):
        self.servo.reinit()

    def handle_connect(self):
        self.servo.handle_connect()

    cmd_MMU_SOAKTEST_SELECTOR_help = "Soak test of selector movement"
    def cmd_MMU_SOAKTEST_SELECTOR(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu._check_is_disabled(): return
        if self.mmu._check_is_loaded(): return
        if self.mmu._check_is_calibrated(self.CALIBRATED_SELECTOR): return
        loops = gcmd.get_int('LOOP', 100)
        servo = bool(gcmd.get_int('SERVO', 0))
        home = bool(gcmd.get_int('HOME', 1))
        try:
            if home:
                self._home()
            for l in range(loops):
                self.log_always("Testing loop %d / %d" % (l + 1, loops))
                tool = random.randint(0, self.mmu_num_gates)
                if tool == self.mmu_num_gates:
                    self._select_bypass()
                else:
                    if random.randint(0, 10) == 0 and home:
                        self._home(tool)
                    else:
                        self.mmu._select_tool(tool, move_servo=servo) # PAUL select tool or gate?
                if servo:
                    self.selector.filament_drive()
        except MmuError as ee:
            self.mmu._handle_mmu_error("Soaktest abandoned because of error: %s" % str(ee))

    def select_gate(self, gate):
        if gate == self.mmu.gate_selected: return

        with self.mmu._wrap_action(self.mmu.ACTION_SELECTING): # PAUL -- need to move servo control to mmu_toolhead. Call it actuate
            self._servo_move()
            if gate == self.TOOL_GATE_BYPASS:
                offset = self.mmu.bypass_offset
            else:
                offset = self.mmu.selector_offsets[gate]
            self.position(offset)
            self.mmu._set_gate_selected(gate)

    def filament_drive(self, buzz_gear=True): # PAUL aka _servo_down
        self.servo._servo_down(buzz_gear=buzz_gear)

    def filament_release(self, measure=False): # PAUL aka _servo_up
        self.servo._servo_up(measure=measure)

    def filament_hold(self): # PAUL aka _servo_move
        self.servo._servo_move()

    def get_filament_grip_state(self):
        return self.servo.get_filament_grip_state(self)

    def disable_motors(self):
        stepper_enable = self.mmu.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_disable(self.mmu_toolhead.get_last_move_time())
        self.is_homed = False

        self.servo.disable_motors()

    def enable_motors(self):
        stepper_enable = self.mmu.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_enable(self.mmu_toolhead.get_last_move_time())

    def buzz_motor(self, motor):
        if motor == "selector":
            pos = self.mmu_toolhead.get_position()[0]
            self.move(None, pos + 5, wait=False)
            self.move(None, pos - 5, wait=False)
            self.move(None, pos, wait=False)
        elif motor == "servo":
            self.servo.buzz_motor(motor)
        else:
            return False
        return True

    def get_status(self):
        status = {
            'grip': "release" if self.servo.servo_state == self.servo.SERVO_UP_STATE else
                    "drive" if self.servo.servo_state == self.servo.SERVO_DOWN_STATE else
                    "hold" if self.servo.servo_state == self.servo.SERVO_MOVE_STATE else
                    "unknown",
        }
        status.update(self.servo.get_status())
        return status

    def get_mmu_config(self):
        msg = "\nSelector is %s" % ("HOMED" if self.is_homed else "NOT HOMED")
        msg += self.servo.get_mmu_config()
        return msg

    def _home_selector(self):
        self.mmu.gate_selected = self.mmu.TOOL_GATE_UNKNOWN
        self.mmu._servo_move()
        self.mmu.movequeues_wait()
        homing_state = MmuHoming(self.mmu.printer, self.mmu_toolhead)
        homing_state.set_axes([0])
        try:
            self.mmu.mmu_kinematics.home(homing_state)
            self.is_homed = True
        except Exception as e: # Homing failed
            self.mmu._set_tool_selected(self.mmu.TOOL_GATE_UNKNOWN)
            raise MmuError("Homing selector failed because of blockage or malfunction. Klipper reports: %s" % str(e))

    def position(self, target):
        if not self.use_touch_move():
            self.move("Positioning selector", target)
        else:
            init_pos = self.mmu_toolhead.get_position()[0]
            halt_pos,homed = self.homing_move("Positioning selector with 'touch' move", target, homing_move=1, endstop_name=self.mmu.ENDSTOP_SELECTOR_TOUCH)
            if homed: # Positioning move was not successful
                with self.mmu._wrap_suppress_visual_log():
                    travel = abs(init_pos - halt_pos)
                    if travel < 4.0: # Filament stuck in the current gate (based on ERCF design)
                        self.mmu.log_info("Selector is blocked by filament inside gate, will try to recover...")
                        self.move("Realigning selector by a distance of: %.1fmm" % -travel, init_pos)
                        self.mmu_toolhead.flush_step_generation() # TTC mitigation when homing move + regular + get_last_move_time() is close succession

                        # See if we can detect filament in the encoder
                        found = self.mmu._check_filament_at_gate()
                        if not found:
                            # Push filament into view of the gate endstop
                            self.mmu._servo_down()
                            _,_,measured,delta = self.mmu._trace_filament_move("Locating filament", self.mmu.gate_parking_distance + self.mmu.gate_endstop_to_encoder + 10.)
                            if measured < self.mmu.encoder_min:
                                raise MmuError("Unblocking selector failed bacause unable to move filament to clear")

                        # Try a full unload sequence
                        try:
                            self.mmu._unload_sequence(check_state=True)
                        except MmuError as ee:
                            raise MmuError("Unblocking selector failed because: %s" % (str(ee)))

                        # Check if selector can now reach proper target
                        self._home_selector()
                        halt_pos,homed = self.homing_move("Positioning selector with 'touch' move", target, homing_move=1, endstop_name=self.mmu.ENDSTOP_SELECTOR_TOUCH)
                        if homed: # Positioning move was not successful
                            self.is_homed = False
                            self.mmu._unselect_tool()
                            raise MmuError("Unblocking selector recovery failed. Path is probably internally blocked")

                    else: # Selector path is blocked, probably externally
                        self.is_homed = False
                        self.mmu._unselect_tool()
                        raise MmuError("Selector is externally blocked perhaps by filament in another gate")

    def move(self, trace_str, new_pos, speed=None, accel=None, wait=False):
        return self._trace_selector_move(trace_str, new_pos, speed=speed, accel=accel, wait=wait)[0]

    def homing_move(self, trace_str, new_pos, speed=None, accel=None, homing_move=0, endstop_name=None):
        return self._trace_selector_move(trace_str, new_pos, speed=speed, accel=accel, homing_move=homing_move, endstop_name=endstop_name)

    # Internal raw wrapper around all selector moves except rail homing
    # Returns position after move, if homed (homing moves)
    def _trace_selector_move(self, trace_str, new_pos, speed=None, accel=None, homing_move=0, endstop_name=None, wait=False):
        if trace_str:
            self.mmu.log_trace(trace_str)

        # Set appropriate speeds and accel if not supplied
        if homing_move != 0:
            speed = speed or (self.mmu.selector_touch_speed if self.mmu.selector_touch_enable or endstop_name == self.mmu.ENDSTOP_SELECTOR_TOUCH else self.mmu.selector_homing_speed)
        else:
            speed = speed or self.mmu.selector_move_speed
        accel = accel or self.mmu_toolhead.get_selector_limits()[1]

        pos = self.mmu_toolhead.get_position()
        homed = False
        if homing_move != 0:
            # Check for valid endstop
            endstop = self.selector_rail.get_extra_endstop(endstop_name) if endstop_name is not None else self.selector_rail.get_endstops()
            if endstop is None:
                self.mmu.log_error("Endstop '%s' not found on selector rail" % endstop_name)
                return pos[0], homed

            hmove = HomingMove(self.mmu.printer, endstop, self.mmu_toolhead)
            try:
                trig_pos = [0., 0., 0., 0.]
                with self.mmu._wrap_accel(accel):
                    pos[0] = new_pos
                    trig_pos = hmove.homing_move(pos, speed, probe_pos=True, triggered=homing_move > 0, check_triggered=True)
                    if hmove.check_no_movement():
                        self.mmu.log_stepper("No movement detected")
                    if self.selector_rail.is_endstop_virtual(endstop_name):
                        # Try to infer move completion is using Stallguard. Note that too slow speed or accelaration
                        delta = abs(new_pos - trig_pos[0])
                        if delta < 1.0:
                            homed = False
                            self.mmu.log_trace("Truing selector %.4fmm to %.2fmm" % (delta, new_pos))
                            self.mmu_toolhead.move(pos, speed)
                        else:
                            homed = True
                    else:
                        homed = True
            except self.mmu.printer.command_error as e:
                homed = False
            finally:
                self.mmu_toolhead.flush_step_generation() # TTC mitigation when homing move + regular + get_last_move_time() is close succession
                pos = self.mmu_toolhead.get_position()
                if self.mmu.log_enabled(self.mmu.LOG_STEPPER):
                    self.mmu.log_stepper("SELECTOR HOMING MOVE: requested position=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s >> %s" % (new_pos, speed, accel, endstop_name, "%s actual pos=%.2f, trig_pos=%.2f" % ("HOMED" if homed else "DID NOT HOMED",  pos[0], trig_pos[0])))
        else:
            pos = self.mmu_toolhead.get_position()
            with self.mmu._wrap_accel(accel):
                pos[0] = new_pos
                self.mmu_toolhead.move(pos, speed)
            if self.mmu.log_enabled(self.mmu.LOG_STEPPER):
                self.mmu.log_stepper("SELECTOR MOVE: position=%.1f, speed=%.1f, accel=%.1f" % (new_pos, speed, accel))
            if wait:
                self.mmu.movequeues_wait(toolhead=False, mmu_toolhead=True)

        return pos[0], homed

    def set_position(self, position):
        pos = self.mmu_toolhead.get_position()
        pos[0] = position
        self.mmu_toolhead.set_position(pos, homing_axes=(0,))
        self.is_homed = True
        self.enable_motors()
        return position

    def measure_to_home(self):
        init_mcu_pos = self.selector_stepper.get_mcu_position()
        homed = False
        try:
            homing_state = MmuHoming(self.mmu.printer, self.mmu_toolhead)
            homing_state.set_axes([0])
            self.mmu.mmu_kinematics.home(homing_state)
            homed = True
        except Exception as e:
            pass # Home not found
        mcu_position = self.selector_stepper.get_mcu_position()
        traveled = abs(mcu_position - init_mcu_pos) * self.selector_stepper.get_step_dist()
        return traveled, homed

    def use_touch_move(self):
        return self.mmu.ENDSTOP_SELECTOR_TOUCH in self.selector_rail.get_extra_endstop_names() and self.mmu.selector_touch_enable


################################################################################
# LinearSelectorServo
# Implements servo control for typical linear selector
################################################################################
class LinearSelectorServo():

    # mmu_vars.cfg variables
    VARS_MMU_SERVO_ANGLES = "mmu_servo_angles"

    # Servo states
    SERVO_MOVE_STATE      = FILAMENT_HOLD_STATE
    SERVO_DOWN_STATE      = FILAMENT_DRIVE_STATE
    SERVO_UP_STATE        = FILAMENT_RELEASE_STATE
    SERVO_UNKNOWN_STATE   = FILAMENT_UNKNOWN_STATE

    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu_toolhead = mmu.mmu_toolhead

        # Servo parameters
        self.servo_angles = {}
        self.servo_angles['down'] = mmu.config.getint('servo_down_angle', 90)
        self.servo_angles['up'] = mmu.config.getint('servo_up_angle', 90)
        self.servo_angles['move'] = mmu.config.getint('servo_move_angle', self.servo_angles['up'])
        self.servo_duration = mmu.config.getfloat('servo_duration', 0.2, minval=0.1)
        self.servo_always_active = mmu.config.getint('servo_always_active', 0, minval=0, maxval=1)
        self.servo_active_down = mmu.config.getint('servo_active_down', 0, minval=0, maxval=1)
        self.servo_dwell = mmu.config.getfloat('servo_dwell', 0.4, minval=0.1)
        self.servo_buzz_gear_on_down = mmu.config.getint('servo_buzz_gear_on_down', 3, minval=0, maxval=10)

        self.servo = self.mmu.printer.lookup_object('mmu_servo mmu_servo', None)
        if not self.servo:
            raise self.mmu.config.error("No [mmu_servo] definition found in mmu_hardware.cfg")

        self.reinit()

        # Register GCODE commands specific to this module
        gcode = self.mmu.printer.lookup_object('gcode')
        gcode.register_command('MMU_SERVO', self.cmd_MMU_SERVO, desc = self.cmd_MMU_SERVO_help)

    def reinit(self):
        self.servo_state = self.SERVO_UNKNOWN_STATE
        self.servo_angle = self.SERVO_UNKNOWN_STATE

    def handle_connect(self):
        # Override with saved/calibrated servo positions
        try:
            servo_angles = self.mmu.save_variables.allVariables.get(self.VARS_MMU_SERVO_ANGLES, {})
            self.servo_angles.update(servo_angles)
        except Exception as e:
            raise self.mmu.config.error("Exception whilst parsing servo angles from 'mmu_vars.cfg': %s" % str(e))

# PAUL TODO change self.
    cmd_MMU_SERVO_help = "Move MMU servo to position specified position or angle"
    def cmd_MMU_SERVO(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        save = gcmd.get_int('SAVE', 0)
        pos = gcmd.get('POS', "").lower()
        if pos == "off":
            self._servo_off() # For 'servo_always_active' case
        elif pos == "up":
            if save:
                self._servo_save_pos(pos)
            else:
                self.mmu.selector.filament_release()
        elif pos == "move":
            if save:
                self._servo_save_pos(pos)
            else:
                self.mmu.selector.filament_hold()
        elif pos == "down":
            if self._check_in_bypass(): return
            if save:
                self._servo_save_pos(pos)
            else:
                self.mmu.selector.filament_drive()
        elif save:
            self.log_error("Servo position not specified for save")
        elif pos == "":
            if self._check_in_bypass(): return
            angle = gcmd.get_int('ANGLE', None)
            if angle is not None:
                self.log_debug("Setting servo to angle: %d" % angle)
                self._servo_set_angle(angle)
            else:
                self.log_always("Current servo angle: %d, Positions: %s" % (self.servo_angle, self.servo_angles))
                self.log_info("Use POS= or ANGLE= to move position")
        else:
            self.log_error("Unknown servo position '%s'" % pos)

    def _servo_set_angle(self, angle):
        self.servo.set_value(angle=angle, duration=None if self.servo_always_active else self.servo_duration)
        self.servo_angle = angle
        self.servo_state = self.SERVO_UNKNOWN_STATE

    def _servo_save_pos(self, pos):
        if self.servo_angle != self.SERVO_UNKNOWN_STATE:
            self.servo_angles[pos] = self.servo_angle
            self.mmu._save_variable(self.VARS_MMU_SERVO_ANGLES, self.servo_angles, write=True)
            self.mmu.log_info("Servo angle '%d' for position '%s' has been saved" % (self.servo_angle, pos))
        else:
            self.mmu.log_info("Servo angle unknown")

    def _servo_down(self, buzz_gear=True):
        if self.mmu.internal_test: return # Save servo while testing
        if self.mmu.gate_selected == self.mmu.TOOL_GATE_BYPASS: return
        if self.servo_state == self.SERVO_DOWN_STATE: return
        self.mmu.log_debug("Setting servo to down (filament drive) position at angle: %d" % self.servo_angles['down'])
        self.mmu.movequeues_wait()
        self.servo.set_value(angle=self.servo_angles['down'], duration=None if self.servo_active_down or self.servo_always_active else self.servo_duration)
        if self.servo_angle != self.servo_angles['down'] and buzz_gear and self.servo_buzz_gear_on_down > 0:
            for i in range(self.servo_buzz_gear_on_down):
                self.mmu._trace_filament_move(None, 0.8, speed=25, accel=self.mmu.gear_buzz_accel, encoder_dwell=None)
                self.mmu._trace_filament_move(None, -0.8, speed=25, accel=self.mmu.gear_buzz_accel, encoder_dwell=None)
            self.mmu.movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))
        self.servo_angle = self.servo_angles['down']
        self.servo_state = self.SERVO_DOWN_STATE
        self.mmu._mmu_macro_event(self.MACRO_EVENT_FILAMENT_ENGAGED)

    def _servo_move(self): # Position servo for selector movement
        if self.mmu.internal_test: return # Save servo while testing
        if self.servo_state == self.SERVO_MOVE_STATE: return
        self.mmu.log_debug("Setting servo to move (filament hold) position at angle: %d" % self.servo_angles['move'])
        if self.servo_angle != self.servo_angles['move']:
            self.mmu.movequeues_wait()
            self.servo.set_value(angle=self.servo_angles['move'], duration=None if self.servo_always_active else self.servo_duration)
            self.mmu.movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))
            self.servo_angle = self.servo_angles['move']
            self.servo_state = self.SERVO_MOVE_STATE

    def _servo_up(self, measure=False):
        if self.mmu.internal_test: return 0. # Save servo while testing
        if self.servo_state == self.SERVO_UP_STATE: return 0.
        self.mmu.log_debug("Setting servo to up (filament released) position at angle: %d" % self.servo_angles['up'])
        delta = 0.
        if self.servo_angle != self.servo_angles['up']:
            self.mmu.movequeues_wait()
            if measure:
                initial_encoder_position = self.mmu._get_encoder_distance(dwell=None)
            self.servo.set_value(angle=self.servo_angles['up'], duration=None if self.servo_always_active else self.servo_duration)
            self.mmu.movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))
            if measure:
                # Report on spring back in filament then revert counter
                delta = self.mmu._get_encoder_distance() - initial_encoder_position
                if delta > 0.:
                    self.mmu.log_debug("Spring in filament measured  %.1fmm - adjusting encoder" % delta)
                    self.mmu._set_encoder_distance(initial_encoder_position, dwell=None)
        self.servo_angle = self.servo_angles['up']
        self.servo_state = self.SERVO_UP_STATE
        return delta

    def _servo_auto(self):
        if self.mmu._is_printing() and self.mmu_toolhead.is_gear_synced_to_extruder():
            self._servo_down()
        elif not self.mmu.selector.is_homed or self.mmu.tool_selected < 0 or self.mmu.gate_selected < 0:
            self._servo_move()
        else:
            self._servo_up()

    # De-energize servo if 'servo_always_active' or 'servo_active_down' are being used
    def _servo_off(self):
        self.servo.set_value(width=0, duration=None)

    def get_filament_grip_state(self):
        return self.servo_state

    def disable_motors(self):
        self._servo_move()
        self._servo_off()
        self.reinit()

    def enable_motors(self):
        self._servo_move()

    def buzz_motor(self):
        self.mmu.movequeues_wait()
        old_state = self.servo_state
        small=min(self.servo_angles['down'], self.servo_angles['up'])
        large=max(self.servo_angles['down'], self.servo_angles['up'])
        mid=(self.servo_angles['down'] + self.servo_angles['up'])/2
        duration=None if self.servo_always_active else self.servo_duration
        self.set_value(angle=mid, duration=duration)
        self.mmu.movequeues_dwell(max(self.servo_duration, 0.5), mmu_toolhead=False)
        self.set_value(angle=abs(mid+small)/2, duration=duration)
        self.mmu.movequeues_dwell(max(self.servo_duration, 0.5), mmu_toolhead=False)
        self.set_value(angle=abs(mid+large)/2, duration=duration)
        self.mmu.movequeues_dwell(max(self.servo_duration, 0.5), mmu_toolhead=False)
        self.mmu.movequeues_wait()
        if old_state == self.SERVO_DOWN_STATE:
            self._servo_down(buzz_gear=False)
        elif old_state == self.SERVO_MOVE_STATE:
            self._servo_move()
        else:
            self._servo_up()

    def get_mmu_config(self):
        msg = ". Servo in %s position" % ("UP" if self.servo_state == self.SERVO_UP_STATE else \
                "DOWN" if self.servo_state == self.SERVO_DOWN_STATE else "MOVE" if self.servo_state == self.SERVO_MOVE_STATE else "unknown")
        return msg

    def get_status(self):
        return {
            'servo': "Up" if self.servo_state == self.SERVO_UP_STATE else
                     "Down" if self.servo_state == self.SERVO_DOWN_STATE else
                     "Move" if self.servo_state == self.SERVO_MOVE_STATE else
                     "Unknown",
        }
