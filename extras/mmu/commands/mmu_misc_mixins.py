# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Mixins for various commands
#   ClogTangleMixin
#   UnloadEjectMixin
#   MoveMixin
#       
#   
# (\_/) 
# ( *,*)
# (")_(") Happy Hare Ready
#       
# This file may be distributed under the terms of the GNU GPLv3 license.
#       
import logging
    
# Happy Hare imports
from ..mmu_constants    import * 
from ..mmu_utils        import MmuError
from ..mmu_sensor_utils import MmuCompoundEndstop
from ..unit.mmu_leds    import MmuLeds


class ClogTangleMixin:
    """
    Mixin providing shared handling logic for MMU clog/tangle sensor events.

    Intended for use by:
      MMU_SENSOR_CLOG, MMU_SENSOR_TANGLE

    Note: 'pause_resume.send_pause_command()' will already have been issued by the
          sensor/runout handling layer before this command executes, but no actual
          PAUSE gcode command will have run yet.
    """

    def _handle_clog_tangle(self, gcmd, event_type):
        """
        Process a clog/tangle sensor event.
        gcmd params:
          EVENTTIME will contain reactor time that the sensor triggered and command was queued
          SENSOR will contain sensor name
        """
        mmu = self.mmu

        if not mmu.is_enabled:
            # Undo what runout sensor handling did
            mmu.pause_resume.send_resume_command()
            return

        mmu.fix_started_state()

        eventtime = gcmd.get_float('EVENTTIME', mmu.reactor.monotonic())
        sensor = gcmd.get('SENSOR', "")

        mmu._runout(event_type=event_type, sensor=sensor) # Will send_resume_command() or fail and pause


class UnloadEjectMixin:
    """
    Mixin providing shared logic for unload/eject handling

    Intended for use by:
      MMU_UNLOAD and MMU_EJECT
    """

    def _handle_unload(self, gcmd):
        """
        Common logic
        """
        mmu = self.mmu

        in_bypass = mmu.gate_selected == TOOL_GATE_BYPASS

        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1)) or in_bypass
        skip_tip = bool(gcmd.get_int('SKIP_TIP', 0, minval=0, maxval=1))
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))

        do_form_tip = FORM_TIP_STANDALONE if not skip_tip else FORM_TIP_NONE

        mmu._note_toolchange("< %s" % mmu.selected_tool_string())

        if extruder_only:
            mmu.set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER, silent=True) # Ensure tool tip is performed
            mmu.unload_sequence(bowden_move=0., form_tip=do_form_tip, extruder_only=True)
            if in_bypass:
                mmu.set_filament_pos_state(FILAMENT_POS_UNLOADED)
                mmu.log_always("Please pull the filament out from the MMU")
        else:
            if mmu.filament_pos != FILAMENT_POS_UNLOADED:
                mmu.last_statistics = {}
                mmu._save_toolhead_position_and_park('unload')
                mmu._unload_tool(form_tip=do_form_tip)
                mmu._persist_gate_statistics()
                mmu._continue_after('unload', restore=restore)

        mmu._persist_swap_statistics()


class MoveMixin:
    """
    Mixin providing shared logic for standard and homing move commands

    Intended for use by:
      MMU_TEST_MOVE, _MMU_STEP_MOVE
      MMU_TEST_HOMING_MOVE, _MMU_STEP_HOMING_MOVE
    """

    def _move_cmd(self, gcmd, trace_str, allow_bypass=False):
        mmu = self.mmu
        selector = mmu.selector()

        if self.check_if_disabled():
            return (0., False, 0., 0.)

        if not allow_bypass and self.check_if_bypass():
            return (0., False, 0., 0.)

        move = gcmd.get_float('MOVE', 100.)
        speed = gcmd.get_float('SPEED', None)
        accel = gcmd.get_float('ACCEL', None)
        motor = gcmd.get('MOTOR', "gear")
        wait = bool(gcmd.get_int('WAIT', 1, minval=0, maxval=1)) # Wait for move to complete (make move synchronous)

        if motor not in ["gear", "extruder", "gear+extruder", "synced"]:
            raise gcmd.error("Valid motor names are 'gear', 'extruder', 'gear+extruder' or 'synced'")

        mmu.log_debug("Moving '%s' motor %.1fmm..." % (motor, move))
        return mmu.move_filament(trace_str, move, speed=speed, accel=accel, motor=motor, wait=wait)


    def _homing_move_cmd(self, gcmd, trace_str, allow_bypass=False):
        mmu = self.mmu
        selector = mmu.selector()

        if self.check_if_disabled():
            return (0., False, 0., 0.)
        if not allow_bypass and self.check_if_bypass():
            return (0., False, 0., 0.)

        endstop = gcmd.get('ENDSTOP', "default")
        endstops = [
            e.strip()
            for e in gcmd.get('ENDSTOPS', "").split(',')
            if e.strip()
        ]
        move = gcmd.get_float('MOVE', 100.)
        speed = gcmd.get_float('SPEED', None)
        accel = gcmd.get_float('ACCEL', None) # Ignored for extruder led moves
        motor = gcmd.get('MOTOR', "gear").lower()

        if endstops and endstop != 'default':
            raise gcmd.error("Can only specify ENDSTOPS= or ENDSTOP=, not both")

        if motor not in ["gear", "extruder", "gear+extruder"]:
            raise gcmd.error("Valid motor names are 'gear', 'extruder', 'gear+extruder'")

        direction = -1 if move < 0 else 1
        stop_on_endstop = gcmd.get_int('STOP_ON_ENDSTOP', direction, minval=-1, maxval=1)
        if abs(stop_on_endstop) != 1:
            raise gcmd.error("STOP_ON_ENDSTOP can only be 1 (extrude direction) or -1 (retract direction)")

        drive_stepper = mmu.drive().mmu_extruder_stepper if motor == "extruder" else mmu.drive().mmu_gear_stepper
        valid_endstops = list({
            mmu.sensor_manager.get_generic_endstop_name(name)
            for name in drive_stepper.rail.get_all_endstop_names()
        })
        valid_endstops.sort()

        endstops = endstops or [endstop]
        endstop_objs = []

        for endstop in endstops:
            if endstop not in valid_endstops:
                raise gcmd.error("Endstop name '%s' is not valid for motor '%s'. Options are: %s" % (endstop, motor, ', '.join(valid_endstops)))

            qual_endstop = mmu.sensor_manager.get_qualified_endstop_name(endstop)

            if drive_stepper.rail.is_endstop_virtual(qual_endstop) and stop_on_endstop == -1:
                raise gcmd.error("Cannot reverse home on virtual (TMC stallguard) endstop '%s'" % endstop)

            endstop_objs.append(drive_stepper.rail.get_extra_endstop(qual_endstop))

        endstop_name = None
        compound_endstop = None
        try:
            if len(endstop_objs) > 1:
                endstop_name = "compound"
                compound_endstop = MmuCompoundEndstop(mmu.printer, name=endstop_name, endstops=endstop_objs)
                drive_stepper.rail.add_compound_endstop(endstop_name, compound_endstop)
            else:
                endstop_name = endstops[0]
            
            mmu.log_debug("Homing '%s' motor to '%s' endstop, up to %.1fmm..." % (motor, endstop_name, move))
            result = mmu.move_filament(trace_str, move, speed=speed, accel=accel, motor=motor, homing_move=stop_on_endstop, endstop_name=endstop_name)

            # Add triggered endstop to return tuple
            moved, homed, actual, delta = result
            if homed:
                if compound_endstop is not None:
                    triggered_name = compound_endstop.get_triggered_endstop_name()
                else:
                    triggered_name = endstop_name
            else:
                triggered_name = None

            return moved, homed, actual, delta, triggered_name

        finally:
            if compound_endstop is not None:
                drive_stepper.rail.remove_compound_endstop(endstop_name)


class LedMixin:
    """
    Mixin providing shared logic for led commands

    Intended for use by:
      MMU_LED, MMU_SET_LED
    """

    EXIT_OPTIONS =   ["off", "gate_status", "filament_color", "slicer_color"]
    ENTRY_OPTIONS =  ["off", "gate_status", "filament_color", "slicer_color"]
    STATUS_OPTIONS = ["off", "on",          "filament_color", "slicer_color"]
    LOGO_OPTIONS =   ["off"]


    def _validate_effect(self, gcmd, name, mmu_unit, effect, options):
        if effect is None:
            return None

        if isinstance(effect, str):
            effect = effect.strip()

        # Standard option?
        if effect in options:
            return effect

        # RGB value?
        try:
            return MmuLeds.string_to_rgb(effect)
        except Exception:
            pass

        # User configured effect name?
        effect_names = sorted(mmu_unit.leds.get_effect_names())
        if effect in effect_names:
            return effect

        raise gcmd.error(
            f"{name} has invalid value '{effect}'\n"
            f"Valid values are: {', '.join(options)}, (r,g,b), "
            f"or one of the configured effects:\n"
            f"{', '.join(effect_names)}"
        )
