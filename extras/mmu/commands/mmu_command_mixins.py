# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Mixins (base class) for various commands
#       
#   
# (\_/) 
# ( *,*)
# (")_(") Happy Hare Ready
#       
# This file may be distributed under the terms of the GNU GPLv3 license.
#       
    
# Happy Hare imports
from ..mmu_constants   import * 
from ..mmu_utils       import MmuError


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

        mmu._fix_started_state()

        eventtime = gcmd.get_float('EVENTTIME', mmu.reactor.monotonic())
        sensor = gcmd.get('SENSOR', "")

        mmu._runout(event_type=event_type, sensor=sensor) # Will send_resume_command() or fail and pause


class UnloadEjectMixin:
    """
    Mixin providing shared logic for unload/eject handling

    Intended for use by:
      MMU_UNLOAD and MMU_EJECT
    """

    def _handle_unload_eject(self, gcmd):
        mmu = self.mmu

        in_bypass = mmu.gate_selected == TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1)) or in_bypass
        skip_tip = bool(gcmd.get_int('SKIP_TIP', 0, minval=0, maxval=1))
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))
        do_form_tip = FORM_TIP_STANDALONE if not skip_tip else FORM_TIP_NONE

        mmu._note_toolchange("< %s" % mmu.selected_tool_string())

        if extruder_only:
            mmu._set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER, silent=True) # Ensure tool tip is performed
            mmu.unload_sequence(bowden_move=0., form_tip=do_form_tip, extruder_only=True)
            if in_bypass:
                mmu._set_filament_pos_state(FILAMENT_POS_UNLOADED)
                mmu.log_always("Please pull the filament out from the MMU")
        else:
            if mmu.filament_pos != FILAMENT_POS_UNLOADED:
                mmu.last_statistics = {}
                mmu._save_toolhead_position_and_park('unload')
                mmu._unload_tool(form_tip=do_form_tip)
                mmu._persist_gate_statistics()
                mmu._continue_after('unload', restore=restore)


class MoveMixin:
    """
    Mixin providing shared logic for standard and homing move commands

    Intended for use by:
      MMU_TEST_MOVE, _MMU_STEP_MOVE
      MMU_TEST_HOMING_MOVE, _MMU_STEP_HOMING_MOVE
    """

    def _move_cmd(self, gcmd, trace_str, allow_bypass=False):
        mmu = self.mmu
        sel = mmu.selector()

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

        if motor == "extruder":
            sel.filament_release()
        else:
            sel.filament_drive()
        mmu.log_debug("Moving '%s' motor %.1fmm..." % (motor, move))
        return mmu.trace_filament_move(trace_str, move, speed=speed, accel=accel, motor=motor, wait=wait)

    def _homing_move_cmd(self, gcmd, trace_str, allow_bypass=False):
        mmu = mmu
        sel = mmu.selector()

        if self.check_if_disabled():
            return (0., False, 0., 0.)
        if not allow_bypass and self.check_if_bypass():
            return (0., False, 0., 0.)

        endstop = gcmd.get('ENDSTOP', "default")
        move = gcmd.get_float('MOVE', 100.)
        speed = gcmd.get_float('SPEED', None)
        accel = gcmd.get_float('ACCEL', None) # Ignored for extruder led moves
        motor = gcmd.get('MOTOR', "gear")

        if motor not in ["gear", "extruder", "gear+extruder"]:
            raise gcmd.error("Valid motor names are 'gear', 'extruder', 'gear+extruder'")

        direction = -1 if move < 0 else 1
        stop_on_endstop = gcmd.get_int('STOP_ON_ENDSTOP', direction, minval=-1, maxval=1)
        if abs(stop_on_endstop) != 1:
            raise gcmd.error("STOP_ON_ENDSTOP can only be 1 (extrude direction) or -1 (retract direction)")

        endstop = mmu.sensor_manager.get_mapped_endstop_name(endstop)
        valid_endstops = list(mmu.gear_rail().get_extra_endstop_names())
        if endstop not in valid_endstops:
            raise gcmd.error("Endstop name '%s' is not valid for motor '%s'. Options are: %s" % (endstop, motor, ', '.join(valid_endstops)))

        if mmu.gear_rail().is_endstop_virtual(endstop) and stop_on_endstop == -1:
            raise gcmd.error("Cannot reverse home on virtual (TMC stallguard) endstop '%s'" % endstop)

        if motor == "extruder":
            sel.filament_release()
        else:
            sel.filament_drive()

        mmu.log_debug("Homing '%s' motor to '%s' endstop, up to %.1fmm..." % (motor, endstop, move))
        return mmu.trace_filament_move(trace_str, move, speed=speed, accel=accel, motor=motor, homing_move=stop_on_endstop, endstop_name=endstop)
