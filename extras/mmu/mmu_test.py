# Happy Hare MMU Software
#
# Copyright (C) 2022  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# Goal: Define internal test operations to aid development
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import random

# Happy Hare imports
from extras import mmu_machine
from extras.mmu_machine import MmuToolHead

# MMU subcomponent clases
from .mmu_shared import *
from .mmu_utils import PurgeVolCalculator

class MmuTest:

    def __init__(self, mmu):
        self.mmu = mmu
        mmu.gcode.register_command('_MMU_TEST', self.cmd_MMU_TEST, desc = self.cmd_MMU_TEST_help) # Internal for testing

    cmd_MMU_TEST_help = "Internal Happy Hare development tests"
    def cmd_MMU_TEST(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.mmu.log_info("SYNC_EVENT=[-1.0 ... 1.0] : Generate sync feedback event")
            self.mmu.log_info("DUMP_UNICODE=1 : Display special characters used in display")
            self.mmu.log_info("RUN_SEQUENCE=1 : Run through the set of sequence macros tracking time")
            self.mmu.log_info("GET_POS=1 : Fetch the current filament position")
            self.mmu.log_info("SET_POS=<pos> : Set the current filament position")
            self.mmu.log_info("SYNC_LOAD_TEST=1 : Hammer stepper syncing and movement. Parama: LOOP|HOME")
            self.mmu.log_info("SEL_MOVE=1 : Selector homing move. Params: MOVE|SPEED|ACCEL|WAIT|LOOP")
            self.mmu.log_info("SEL_HOMING_MOVE=1 : Selector homing move. Params: MOVE|SPEED|ACCEL|WAIT|LOOP|ENDSTOP")
            self.mmu.log_info("SEL_LOAD_TEST=1 : Load test selector movements. Params: HOME|LOOP")
            self.mmu.log_info("TTC_TEST=1 : Provoke known TTC condition. Parms: LOOP")
            self.mmu.log_info("SYNC_G2E=1 : Sync gear to extruder")
            self.mmu.log_info("SYNC_E2G=1 : Sync extruder to gear. Params: EXTRUDER_ONLY")
            self.mmu.log_info("UNSYNC=1 : Unsync")

        feedback = gcmd.get_float('SYNC_EVENT', None, minval=-1., maxval=1.)
        if feedback is not None:
            self.mmu.log_info("Sending 'mmu:sync_feedback %.2f' event" % feedback)
            self.mmu.printer.send_event("mmu:sync_feedback", self.mmu.reactor.monotonic(), feedback)

        if gcmd.get_int('DUMP_UNICODE', 0, minval=0, maxval=1):
            self.mmu.log_info("UI_SPACE=%s, UI_SEPARATOR=%s, UI_DASH=%s, UI_DEGREE=%s, UI_BLOCK=%s, UI_CASCADE=%s" % (UI_SPACE, UI_SEPARATOR, UI_DASH, UI_DEGREE, UI_BLOCK, UI_CASCADE))
            self.mmu.log_info("{}{}{}{}".format(UI_BOX_TL, UI_BOX_T, UI_BOX_H, UI_BOX_TR))
            self.mmu.log_info("{}{}{}{}".format(UI_BOX_L,  UI_BOX_M, UI_BOX_H, UI_BOX_R))
            self.mmu.log_info("{}{}{}{}".format(UI_BOX_V,  UI_BOX_V, UI_SPACE, UI_BOX_V))
            self.mmu.log_info("{}{}{}{}".format(UI_BOX_BL, UI_BOX_B, UI_BOX_H, UI_BOX_BR))
            self.mmu.log_info("UI_EMOTICONS=%s" % UI_EMOTICONS)

        if gcmd.get_int('RUN_SEQUENCE', 0, minval=0, maxval=1):
            error = gcmd.get_int('ERROR', 0, minval=0, maxval=1)
            if gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1):
                self.mmu._set_print_state("printing")
            with self.mmu._wrap_track_time('total'):
                with self.mmu._wrap_track_time('unload'):
                    with self.mmu._wrap_track_time('pre_unload'):
                        self.mmu._wrap_gcode_command(self.mmu.pre_unload_macro, exception=False, wait=True)
                    self.mmu._wrap_gcode_command(self.mmu.post_form_tip_macro, exception=False, wait=True)
                    with self.mmu._wrap_track_time('post_unload'):
                        self.mmu._wrap_gcode_command(self.mmu.post_unload_macro, exception=False, wait=True)
                with self.mmu._wrap_track_time('load'):
                    with self.mmu._wrap_track_time('pre_load'):
                        self.mmu._wrap_gcode_command(self.mmu.pre_load_macro, exception=False, wait=True)
                    with self.mmu._wrap_track_time('post_load'):
                        self.mmu._wrap_gcode_command(self.mmu.post_load_macro, exception=False, wait=False)
                        if error:
                            self.mmu._wrap_gcode_command("MMU_PAUSE")
            self.mmu.log_info("Statistics:%s" % self.mmu.last_statistics)
            self.mmu._set_print_state("idle")

        if gcmd.get_int('RUN_CHANGE_SEQUENCE', 0, minval=0, maxval=1):
            pause = gcmd.get_int('PAUSE', 0, minval=0, maxval=1)
            next_pos = gcmd.get('NEXT_POS', "last")
            goto_pos = None
            if next_pos == 'next':
                self.mmu._wrap_gcode_command("SET_GCODE_VARIABLE MACRO=_MMU_SEQUENCE_VARS VARIABLE=restore_xy_pos VALUE='\"%s\"'" % next_pos)
                goto_pos = [11, 11]
                self.mmu._set_next_position(goto_pos)
            if gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1):
                self.mmu._set_print_state("printing")
            self.mmu._save_toolhead_position_and_park('toolchange', next_pos=goto_pos)
            with self.mmu._wrap_track_time('total'):
                try:
                    with self.mmu._wrap_track_time('unload'):
                        with self.mmu._wrap_track_time('pre_unload'):
                            self.mmu._wrap_gcode_command(self.mmu.pre_unload_macro, exception=False, wait=True)
                        self.mmu._wrap_gcode_command(self.mmu.post_form_tip_macro, exception=False, wait=True)
                        with self.mmu._wrap_track_time('post_unload'):
                            self.mmu._wrap_gcode_command(self.mmu.post_unload_macro, exception=False, wait=True)
                    with self.mmu._wrap_track_time('load'):
                        with self.mmu._wrap_track_time('pre_load'):
                            self.mmu._wrap_gcode_command(self.mmu.pre_load_macro, exception=False, wait=True)
                        if pause:
                            raise MmuError("TEST ERROR")
                        else:
                            with self.mmu._wrap_track_time('post_load'):
                                self.mmu._wrap_gcode_command(self.mmu.post_load_macro, exception=False, wait=True)
                            self.mmu._restore_toolhead_position('toolchange')
                except MmuError as ee:
                    self.mmu.handle_mmu_error(str(ee))
            self.mmu.log_info("Statistics:%s" % self.mmu.last_statistics)
            self.mmu._set_print_state("idle")

        if gcmd.get_int('SYNC_G2E', 0, minval=0, maxval=1):
            self.mmu.mmu_toolhead.sync(MmuToolHead.GEAR_SYNCED_TO_EXTRUDER)

        if gcmd.get_int('SYNC_E2G', 0, minval=0, maxval=1):
            extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1))
            self.mmu.mmu_toolhead.sync(MmuToolHead.EXTRUDER_ONLY_ON_GEAR if extruder_only else MmuToolHead.EXTRUDER_SYNCED_TO_GEAR)

        if gcmd.get_int('UNSYNC', 0, minval=0, maxval=1):
            self.mmu.mmu_toolhead.unsync()

        pos = gcmd.get_float('SET_POS', -1, minval=0)
        if pos >= 0:
            self.mmu._set_filament_position(pos)

        if gcmd.get_int('GET_POS', 0, minval=0, maxval=1):
            self.mmu.log_info("Filament position: %s" % self.mmu._get_filament_position())

        if gcmd.get_int('SYNC_LOAD_TEST', 0, minval=0, maxval=1):
            try:
                self.mmu.internal_test = True
                endstop = gcmd.get('ENDSTOP', 'extruder')
                loop = gcmd.get_int('LOOP', 10, minval=1, maxval=1000)
                self.mmu.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=mmu_test")
                self.mmu._initialize_filament_position()
                total = 0.
                for i in range(loop):
                    move_type = random.randint(0, 11) # 12 to enable tracking test
                    move = random.randint(0, 100) - 50
                    speed = random.uniform(50, 200)
                    accel = random.randint(50, 1000)
                    homing = random.randint(0, 1)
                    extruder_only = random.randint(0, 1)
                    motor = random.choice(["gear", "gear+extruder", "extruder"])
                    if move_type in (0, 1):
                        self.mmu.log_info("Loop: %d - Synced gear to extruder movement: %.1fmm" % (i, move))
                        self.mmu.mmu_toolhead.sync(MmuToolHead.GEAR_SYNCED_TO_EXTRUDER)
                        self.mmu.gcode.run_script_from_command("G1 E%.2f F%d" % (move, speed * 60))
                    elif move_type == 2:
                        self.mmu.log_info("Loop: %d - Unsynced extruder movement: %.1fmm" % (i, move))
                        self.mmu.mmu_toolhead.unsync()
                        self.mmu.gcode.run_script_from_command("G1 E%.2f F%d" % (move, speed * 60))
                    elif move_type == 3:
                        self.mmu.log_info("Loop: %d - Regular move: %.1fmm, MOTOR=%s" %  (i, move, motor))
                        self.mmu.gcode.run_script_from_command("MMU_TEST_MOVE MOTOR=%s MOVE=%.2f SPEED=%d" % (motor, move, speed))
                        total += move
                    elif move_type in (4, 5, 6):
                        self.mmu.log_info("Loop: %d - HOMING MOVE: %.1fmm, MOTOR=%s" % (i, move, motor))
                        self.mmu.gcode.run_script_from_command("MMU_TEST_HOMING_MOVE MOTOR=%s MOVE=%.2f SPEED=%d ENDSTOP=%s STOP_ON_ENDSTOP=1" % (motor, move, speed, endstop))
                        total += move
                    elif move_type == 7:
                        if random.randint(0, 1):
                            new_pos = random.uniform(0, 300)
                            self.mmu.log_info("Loop: %d - Set filament position" % i)
                            self.mmu._set_filament_position(new_pos)
                            total = new_pos
                        else:
                            self.mmu.log_info("Loop: %d - Initialized filament position" % i)
                            self.mmu._initialize_filament_position()
                            total = 0.
                    elif move_type == 8:
                        self.mmu.log_info("Loop: %d - Save gcode state" % i)
                        self.mmu.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=mmu_test")
                    elif move_type == 9:
                        self.mmu.log_info("Loop: %d - Restore gcode state" % i)
                        self.mmu.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=mmu_test")
                    elif move_type == 10:
                        self.mmu.log_info("Loop: %d - legacy 'synced' movement: %.1fmm" % (i, move))
                        self.mmu.gcode.run_script_from_command("MMU_TEST_MOVE MOTOR=synced MOVE=%.2f SPEED=%d" % (move, speed))
                    elif move_type == 11:
                        self.mmu.log_info("Loop: %d - legacy 'both' movement: %.1fmm" % (i, move))
                        self.mmu.gcode.run_script_from_command("MMU_TEST_MOVE MOTOR=both MOVE=%.2f SPEED=%d" % (move, speed))
                        total += move
                    else:
                        sync = "---" if self.mmu.mmu_toolhead.sync_mode is None else "E2G" if self.mmu.mmu_toolhead.sync_mode == MmuToolHead.EXTRUDER_SYNCED_TO_GEAR else "G2E" if self.mmu.mmu_toolhead.sync_mode == MmuToolHead.GEAR_SYNCED_TO_EXTRUDER else "Ext"
                        self.mmu.movequeues_wait()
                        tracking = abs(self.mmu._get_filament_position() - total) < 0.1
                        self.mmu.log_info(">>>>>> STATUS: sync: %s, pos=%.2f, total=%.2f" % (sync, self.mmu._get_filament_position(), total))
                        if not tracking:
                            self.mmu.log_error(">>>>>> Position tracking error")
                            break
                self.mmu.gcode.run_script_from_command("_MMU_DUMP_TOOLHEAD")
                self.mmu.log_info("Aggregate move distance: %.1fmm, Toolhead reports: %.1fmm" % (total, self.mmu._get_filament_position()))
            finally:
                self.mmu.internal_test = True

        if gcmd.get_int('SEL_MOVE', 0, minval=0, maxval=1):
            move = gcmd.get_float('MOVE', 10.)
            speed = gcmd.get_float('SPEED', None)
            accel = gcmd.get_float('ACCEL', None)
            wait = gcmd.get_int('WAIT', 1, minval=0, maxval=1)
            loop = gcmd.get_int('LOOP', 1, minval=1)
            for i in range(loop):
                pos = self.mmu.mmu_toolhead.get_position()[0]
                actual = self.mmu.selector.move("Test move", pos + move, speed=speed, accel=accel, wait=wait)
                self.mmu.log_always("%d. Rail starting pos: %s, Selector moved to %.4fmm" % (i, pos, actual))
                if actual != pos + move:
                    self.mmu.log_always("Off target position by: %.4f" % (actual - (pos + move)))

        if gcmd.get_int('SEL_HOMING_MOVE', 0, minval=0, maxval=1):
            move = gcmd.get_float('MOVE', 10.)
            speed = gcmd.get_float('SPEED', None)
            accel = gcmd.get_float('ACCEL', None)
            wait = gcmd.get_int('WAIT', 1, minval=0, maxval=1)
            loop = gcmd.get_int('LOOP', 1, minval=1)
            endstop = gcmd.get('ENDSTOP', self.mmu.ENDSTOP_SELECTOR_TOUCH if self.mmu.selector.use_touch_move() else self.mmu.ENDSTOP_SELECTOR_HOME)
            for i in range(loop):
                pos = self.mmu.mmu_toolhead.get_position()[0]
                self.mmu.log_always("Rail starting pos: %s" % pos)
                actual,homed = self.mmu.selector.homing_move("Test homing move", pos + move, speed=speed, accel=accel, homing_move=1, endstop_name=endstop)
                self.mmu.log_always("%d. Rail starting pos: %s, Selector moved to %.4fmm homing to %s (%s)" % (i, pos, actual, endstop, "homed" if homed else "DID NOT HOME"))
                if actual != pos + move:
                    self.mmu.log_always("Off target position by: %.4f" % (actual - (pos + move)))

        if gcmd.get_int('SEL_LOAD_TEST', 0, minval=0, maxval=1):
            loop = gcmd.get_int('LOOP', 10, minval=1, maxval=1000)
            if gcmd.get_int('HOME', 0, minval=0, maxval=1):
                self.mmu.gcode.run_script_from_command("MMU_HOME")
            for i in range(loop):
                move_type = random.randint(0, 2)
                move = random.randint(10, 100)
                speed = random.uniform(50, 200)
                accel = random.randint(50, 2000)
                homing = random.randint(0, 2)
                wait = random.randint(0, 1)
                pos = self.mmu.mmu_toolhead.get_position()[0]
                if move_type in (1, 2):
                    endstop = "mmu_sel_touch" if move_type == 2 else "mmu_sel_home"
                    actual,homed = self.mmu.selector.homing_move("Test homing move", move, speed=speed, accel=accel, homing_move=1, endstop_name=endstop)
                    self.mmu.log_always("%d. Homing move: Rail starting pos: %s, Selector moved to %.4fmm homing to %s (%s)" % (i, pos, actual, endstop, "homed" if homed else "DID NOT HOME"))
                else:
                    actual = self.mmu.selector.move("Test move", move, speed=speed, accel=accel, wait=wait)
                    self.mmu.log_always("%d. Move: Rail starting pos: %s, Selector moved to %.4fmm" % (i, pos, actual))

        if gcmd.get_int('TTC_TEST', 0, minval=0, maxval=1):
            loop = gcmd.get_int('LOOP', 5, minval=1, maxval=1000)
            for i in range(loop):
                stop_on_endstop = random.randint(0, 1) * 2 - 1
                self.mmu.gcode.run_script_from_command("MMU_TEST_HOMING_MOVE MOTOR=extruder MOVE=10 ENDSTOP=extruder STOP_ON_ENDSTOP=%d DEBUG=1" % stop_on_endstop)
                self.mmu.mmu_toolhead.get_last_move_time() # Try to provoke TTC

        if gcmd.get_int('AUTO_CALIBRATE', 0, minval=0, maxval=1):
            gate = gcmd.get_int('GATE', 0, minval=-2, maxval=8)
            direction = gcmd.get_int('DIRECTION', 1, minval=-1, maxval=1)
            ratio = gcmd.get_float('RATIO', 1., minval=-1, maxval=2)
            homing_movement = gcmd.get_float('HOMING', None, minval=0, maxval=100)
            self.mmu.gate_selected = gate
            self.mmu._auto_calibrate(direction, ratio, homing_movement)

        select_gate = gcmd.get_int('GATE_MOTOR', -99, minval=self.mmu.TOOL_GATE_BYPASS, maxval=self.mmu.num_gates)
        if not select_gate == -99:
            self.mmu.mmu_toolhead.select_gear_stepper(select_gate)

        if gcmd.get_int('CALC_PURGE', 0, minval=0, maxval=1):
            purge_vol_calc = PurgeVolCalculator(0, 800, 1.0)

            purge_vol = purge_vol_calc.calc_purge_vol_by_rgb(192, 192, 192, 247, 35, 35)
            self.mmu.log_always("The purge vol from RGB color 192, 192, 192 to 247, 35, 35 is: {}".format(purge_vol))

            purge_vol = purge_vol_calc.calc_purge_vol_by_hex("#C0C0C0", "#F72323")
            self.mmu.log_always("The purge vol from hex color #C0C0C0 to #F72323 is: {}".format(purge_vol))

            tool_colors = ["FFFF00","80FFFF","FFFFFF","FF8000"]
            purge_volumes = self.mmu._generate_purge_matrix(tool_colors, 0, 800, 1.0)
            self.mmu.log_always("\ntool_colors={}\npurge_volumes={}".format(tool_colors, purge_volumes))
            purge_volumes = self.mmu._generate_purge_matrix(tool_colors, 0, 800, 0.5)
            self.mmu.log_always("\ntool_colors={}\npurge_volumes={}".format(tool_colors, purge_volumes))

        runout = gcmd.get_int('RUNOUT', -1, minval=0, maxval=1)
        if runout == 1:
            self.mmu._enable_runout()
        elif runout == 0:
            self.mmu._disable_runout()

        if gcmd.get_int('SENSOR', 0, minval=0, maxval=1):
            pos = gcmd.get_int('POS', 0, minval=-1)
            gate = gcmd.get_int('GATE', 0, minval=-2, maxval=8)
            loading = bool(gcmd.get_int('LOADING', 1, minval=0, maxval=1))
            loop = gcmd.get_int('LOOP', 0, minval=0, maxval=1)

            if not loop:
                sensors = self.mmu.sensor_manager._get_sensors_before(pos, gate, loading=loading)
                self.mmu.log_always("check_all_sensors_before(%s,%s)=%s" % (pos, gate, self.mmu.sensor_manager.check_all_sensors_before(pos, gate, loading=loading)))
                self.mmu.log_always("sensors before=%s" % sensors)

                sensors = self.mmu.sensor_manager._get_sensors_after(pos, gate, loading=loading)
                self.mmu.log_always("check_all_sensors_after(%s,%s)=%s" % (pos, gate, self.mmu.sensor_manager.check_all_sensors_after(pos, gate, loading=loading)))
                self.mmu.log_always("sensors after=%s" % sensors)
            else:
                for pos in range(-1,11):
                    self.mmu.log_always("check_all_sensors_before(%s,%s)=%s" % (pos, gate, self.mmu.sensor_manager.check_all_sensors_before(pos, gate, loading=loading)))
                self.mmu.log_always("")
                for pos in range(-1,11):
                    self.mmu.log_always("check_all_sensors_after(%s,%s)=%s" % (pos, gate, self.mmu.sensor_manager.check_all_sensors_after(pos, gate, loading=loading)))
            self.mmu.log_always("check_any_sensors_in_path()=%s" % self.mmu.sensor_manager.check_any_sensors_in_path())
            self.mmu.log_always("check_for_runout()=%s" % self.mmu.sensor_manager.check_for_runout())
