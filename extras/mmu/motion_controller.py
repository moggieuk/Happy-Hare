# -----------------------------------------------------------------------------------------------------------
# MMU stepper integration helpers for existing controller
# Assumes existing controller already provides:
#
#   self.printer
#   self.toolhead
#   self.p
#   self.mmu_unit()
#   self.gear()                    -> currently selected gear MmuStepper
#   self.extruder_stepper()        -> currently selected extruder MmuStepper
#   self.printer_extruder()        -> currently selected PrinterExtruder
#   self.get_encoder_distance()
#   self.can_use_encoder()
#   self.movequeues_wait()
#   self.wrap_accel()
#   self._wrap_espooler()
#   self._adjust_gear_current()
#   self._restore_gear_current()
#   self._track_gate_statistics()
#   self.log_trace()
#   self.log_error()
#   self.log_stepper()
#   self.log_assertion()
#   self.log_enabled()
#   self.get_mapped_endstop_name()   # or sensor_manager equivalent
#
# plus existing state like:
#   self.gate_selected
#   self.gate_status
#   self.gate_speed_override
#   self.gate_statistics
# -----------------------------------------------------------------------------------------------------------


def on_selected_gear_changed(self, old_gear, new_gear, extruder_name="extruder"):
    """
    Called by existing controller when selected gear changes.

    Simplified ownership rule:
      - if old and new differ, force old gear back to unsynced/manual
      - do not attempt to track all stepper state globally
    """
    if old_gear is None or old_gear is new_gear:
        return

    try:
        old_gear.set_drive_sync_mode(DRIVE_UNSYNCED, extruder_name)

    except Exception as e:
        self.log_error("Failed to unsync previous gear '%s': %s" % (old_gear.full_name, str(e)))


def on_selected_extruder_changed(self, old, new):
    """
    Called when the active PrinterExtruder changes.

    If the selected extruder changes, detach the currently selected gear
    from the old extruder using the official drive-sync transition path.
    """
    if old is None or old is new:
        return

    try:
        gear = self.gear()
        old_name = old.get_name()
        gear.set_drive_sync_mode(DRIVE_UNSYNCED, old_name)

    except Exception as e:
        self.log_error("Failed to unsync gear '%s' after extruder change: %s" % (gear.full_name, str(e)))


def _set_active_sync_mode(self, motor, gear=None, extruder_name="extruder"):
    """
    Map compatibility motor names to the new MmuStepper sync modes.
    """
    gear = gear or self.gear()
    u = self.mmu_unit()

    if motor == "gear":
        # Old DRIVE_GEAR_ONLY -> new DRIVE_UNSYNCED
        gear.set_drive_sync_mode(DRIVE_UNSYNCED, extruder_name)
        self._restore_gear_current()

    elif motor == "gear+extruder":
        # gear leads, extruder follows manually
        gear.set_drive_sync_mode(DRIVE_EXTRUDER_SYNCED_TO_GEAR, extruder_name)
        self._restore_gear_current()

    elif motor == "extruder":
        # extruder-only-on-gear semantics
        gear.set_drive_sync_mode(DRIVE_EXTRUDER_ONLY_ON_GEAR, extruder_name)
        self._restore_gear_current()

    elif motor == "synced":
        # extruder leads, gear follows extruder
        gear.set_drive_sync_mode(DRIVE_GEAR_SYNCED_TO_EXTRUDER, extruder_name)
        self._adjust_gear_current(percent=u.p.sync_gear_current, reason="for extruder synced move")

    else:
        raise self.printer.command_error("Invalid motor specification '%s'" % (motor,))


def _validate_homing_endstop(self, gear, motor, endstop_name):
    """
    Validate endstop for homing-style moves.
    """
    if motor == "synced":
        raise self.printer.command_error("Not possible to perform homing move while synced")

    mapped = self.get_mapped_endstop_name(endstop_name) # PAUL should be on sensor manager

    if not hasattr(gear, 'rail') or not hasattr(gear.rail, 'has_endstop'):
        raise self.printer.command_error("No endstop support on gear '%s'" % (gear.full_name,))

    if not gear.rail.has_endstop(mapped):
        raise self.printer.command_error("Endstop '%s' not found" % (mapped,))

    return mapped


def _resolve_filament_move_speed(self, dist, motor, homing_move, speed, accel, speed_override=True):
    """
    Direct port of previous speed selection logic.
    """
    u = self.mmu_unit()

    if motor in ["gear"]:
        if homing_move != 0:
            speed = speed or u.p.gear_homing_speed
            accel = accel or min(u.p.gear_from_filament_buffer_accel, u.p.gear_from_spool_accel)
        else:
            if abs(dist) > u.p.gear_short_move_threshold:
                if dist < 0:
                    speed = speed or u.p.gear_unload_speed
                    accel = accel or u.p.gear_unload_accel
                elif (not u.p.has_filament_buffer or
                        (self.gate_selected >= 0 and self.gate_status[self.gate_selected] != GATE_AVAILABLE_FROM_BUFFER)):
                    speed = speed or u.p.gear_from_spool_speed
                    accel = accel or u.p.gear_from_spool_accel
                else:
                    speed = speed or u.p.gear_from_filament_buffer_speed
                    accel = accel or u.p.gear_from_filament_buffer_accel
            else:
                speed = speed or u.p.gear_short_move_speed
                accel = accel or u.p.gear_short_move_accel

    elif motor in ["gear+extruder", "synced"]:
        if homing_move != 0:
            speed = speed or min(u.p.gear_homing_speed, self.p.extruder_homing_speed)
            accel = accel or min(max(u.p.gear_from_filament_buffer_accel, u.p.gear_from_spool_accel), self.p.extruder_accel)
        else:
            speed = speed or (self.p.extruder_sync_load_speed if dist > 0 else self.p.extruder_sync_unload_speed)
            accel = accel or min(max(u.p.gear_from_filament_buffer_accel, u.p.gear_from_spool_accel), self.p.extruder_accel)

    elif motor in ["extruder"]:
        if homing_move != 0:
            speed = speed or self.p.extruder_homing_speed
            accel = accel or self.p.extruder_accel
        else:
            speed = speed or (self.p.extruder_load_speed if dist > 0 else self.p.extruder_unload_speed)
            accel = accel or self.p.extruder_accel

    else:
        raise self.printer.command_error("Invalid motor specification '%s'" % (motor,))

    if self.gate_selected >= 0 and speed_override:
        adjust = self.gate_speed_override[self.gate_selected] / 100.
        speed *= adjust
        accel *= adjust

    return speed, accel


def _move_active_gear_stepper(self, gear, dist, speed, accel, homing_move=0, endstop_name="default"):
    """
    Execute a relative move on the selected gear MmuStepper.
    Returns: actual, homed
    """
    start_pos = gear.commanded_pos
    target_pos = start_pos + dist

    if homing_move != 0:
        home_result = gear.do_homing_move(
            target_pos, speed, accel,
            probe_pos=True,
            triggered=(homing_move > 0),
            check_trigger=True,
            endstop_name=endstop_name
        )

        halt_pos = gear.commanded_pos
        actual = halt_pos - start_pos
        homed = True

        # PAUL check
        # Old code kept special virtual endstop logic:
        #
        # if self.gear_rail().is_endstop_virtual(endstop_name):
        #     if abs(trig_pos[1] - dist) < 1.0:
        #         homed = False
        #
        # New MmuStepper.do_homing_move() returns:
        #   { "trig_pos": ..., "halt_pos": ..., "move_pos": ... }
        # but exact virtual-endstop interpretation may still need tuning.
        if isinstance(home_result, dict) and hasattr(gear, 'rail') and hasattr(gear.rail, 'is_endstop_virtual'):
            try:
                if gear.rail.is_endstop_virtual(endstop_name):
                    trig_rel = home_result["trig_pos"] - start_pos
                    if abs(trig_rel - dist) < 1.0:
                        homed = False
            except Exception:
                pass

        return actual, homed

    gear.do_move(target_pos, speed, accel, sync=False)
    return dist, False


def _move_active_extruder(self, dist, speed):
    """
    Standard extruder-led move for motor='synced'.
    """
    ext_pos = self.toolhead.get_position()
    ext_pos[3] += dist
    self.toolhead.move(ext_pos, speed)
    return dist


# Convenience wrapper around all gear and extruder motor movement that retains sync state, tracks movement and creates trace log
# motor = "gear"             - gear motor(s) only on rail
#         "gear+extruder"    - gear and extruder included on rail
#         "extruder"         - extruder only on gear rail
#         "synced"           - gear synced with extruder as in print (homing move not possible)
#
# If homing move then endstop name can be specified.
#         "mmu_shared_exit"  - at the gate on MMU (when motor includes "gear")
#         "mmu_exit_N"       - post past the filament drive gear
#         "extruder"         - just before extruder entrance (motor includes "gear" or "extruder")
#         "toolhead"         - after extruder entrance (motor includes "gear" or "extruder")
#         "mmu_gear_touch"   - stallguard on gear (when motor includes "gear", only useful for motor="gear")
#         "mmu_ext_touch"    - stallguard on nozzle (when motor includes "extruder", only useful for motor="extruder")
#
# All move distances are interpreted as relative
# 'wait' will wait on appropriate move queue(s) after completion of move (forced to True if need encoder reading)
# 'measure' whether we need to wait and measure encoder for movement
# 'encoder_dwell' delay some additional time to ensure we have accurate encoder reading (if encoder fitted and required for measuring)
#
# All moves return: actual (relative), homed (0 if no encoder), measured, delta (0 if no encoder)
#
def move_filament(self, trace_str, dist, speed=None, accel=None, motor="gear", homing_move=0,
                  endstop_name="default", track=False, wait=False, encoder_dwell=False,
                  speed_override=True):
    """
    Execute a traced filament move using selected gear/extruder MmuStepper objects.

    Returns:
        tuple(actual, homed, measured, delta)
    """
    u = self.mmu_unit()
    gear = self.gear()
    extruder = self.extruder_stepper()
    printer_extruder = self.printer_extruder()

    encoder_start = self.get_encoder_distance(dwell=encoder_dwell)
    homed = False
    actual = dist
    delta = 0.
    null_rtn = (0., False, 0., 0.)

    if motor not in ["gear", "gear+extruder", "extruder", "synced"]:
        self.log_assertion("Invalid motor specification '%s'" % motor)
        return null_rtn

    try:
        if homing_move != 0:
            endstop_name = self._validate_homing_endstop(gear, motor, endstop_name)

        speed, accel = self._resolve_filament_move_speed(
            dist, motor, homing_move, speed, accel, speed_override=speed_override)

    except Exception as e:
        self.log_error(str(e))
        return null_rtn

    with self._wrap_espooler(motor, dist, speed, accel, homing_move):
        wait = wait or self._wait_for_espooler

        try:
            self._set_active_sync_mode(motor, gear=gear, extruder_name=printer_extruder.get_name())

            start_pos = gear.commanded_pos

            # Gear-side move authority
            if motor in ["gear", "gear+extruder", "extruder"]:
                if self.log_enabled(LOG_STEPPER):
                    if homing_move != 0:
                        self.log_stepper("%s HOMING MOVE: max dist=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s, wait=%s" % (
                            motor.upper(), dist, speed, accel, endstop_name, wait))
                    else:
                        self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, wait=%s" % (
                            motor.upper(), dist, speed, accel, wait))

                actual, homed = self._move_active_gear_stepper(
                    gear, dist, speed, accel,
                    homing_move=homing_move,
                    endstop_name=endstop_name)

                # PAUL check logic...
                # Old special-case logic:
                #
                # if motor == "extruder" and not u.extruder_wrapper.homing_extruder:
                #     halt_pos[1] += ext_actual
                #     self.mmu_toolhead().set_position(halt_pos)
                #
                # With the new MmuStepper design this may or may not still be necessary
                # depending on how you want "extruder only on gear rail" to define
                # positional authority. Leaving this comment so you can reintroduce
                # any correction if required.

            # Extruder-side move authority
            elif motor == "synced":
                if homing_move != 0:
                    self.log_error("Not possible to perform homing move while synced")
                    return null_rtn

                if self.log_enabled(LOG_STEPPER):
                    self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, wait=%s" % (
                        motor.upper(), dist, speed, accel, wait))

                with self.wrap_accel(accel):
                    actual = self._move_active_extruder(dist, speed)

# PAUL unecessary
#            try:
#                gear.flush_step_generation()
#            except Exception:
#                pass
#            try:
#                extruder.flush_step_generation()
#            except Exception:
#                pass
#            try:
#                self.toolhead.flush_step_generation()
#            except Exception:
#                pass

            if wait:
                self.movequeues_wait() # PAUL change to klipper movequeue_wait()

        except self.printer.command_error as e:
            self.log_error("Stepper move failed: %s" % str(e))
            if homing_move != 0:
                try:
                    actual = gear.commanded_pos - start_pos
                except Exception:
                    actual = 0.
                homed = False
                # preserve old behavior of returning measured data rather than re-raising
            else:
                return null_rtn

    encoder_end = self.get_encoder_distance(dwell=encoder_dwell)
    measured = encoder_end - encoder_start
    delta = abs(actual) - measured # +ve means measured less than moved, -ve means measured more than moved

    if trace_str:
        if homing_move != 0:
            trace_str += ". Stepper: '%s' %s after moving %.1fmm (of max %.1fmm), encoder measured %.1fmm (delta %.1fmm)"
            trace_str = trace_str % (motor, ("homed" if homed else "did not home"), actual, dist, measured, delta)
        else:
            trace_str += ". Stepper: '%s' moved %.1fmm, encoder measured %.1fmm (delta %.1fmm)"
            trace_str = trace_str % (motor, dist, measured, delta)

        trace_str += " --> Pos: @%.1f, (e: %.1fmm)" % (gear.commanded_pos, encoder_end)
        self.log_trace(trace_str)

    if motor == "gear" and track and self.can_use_encoder():
        if dist > 0:
            self._track_gate_statistics('load_distance', self.gate_selected, dist)
            self._track_gate_statistics('load_delta', self.gate_selected, delta)
        else:
            self._track_gate_statistics('unload_distance', self.gate_selected, -dist)
            self._track_gate_statistics('unload_delta', self.gate_selected, delta)
        if dist != 0:
            quality = abs(1. - delta / dist)
            cur_quality = self.gate_statistics[self.gate_selected]['quality']
            if cur_quality < 0:
                self.gate_statistics[self.gate_selected]['quality'] = quality
            else:
                # Average (EMA) down over 10 swaps
                self.gate_statistics[self.gate_selected]['quality'] = (cur_quality * 9 + quality) / 10

    return actual, homed, measured, delta
