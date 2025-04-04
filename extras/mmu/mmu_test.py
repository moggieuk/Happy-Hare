# Happy Hare MMU Software
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Define internal test operations to aid development
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import random, logging

# Happy Hare imports
# PAUL from ..            import mmu_machine
from ..mmu_unit    import MmuToolHead

# MMU subcomponent clases
from .mmu_shared   import *
from .mmu_utils    import PurgeVolCalculator, DebugStepperMovement

class MmuTest:

    def __init__(self, mmu):
        self.mmu = mmu
        mmu.gcode.register_command('_MMU_TEST', self.cmd_MMU_TEST, desc = self.cmd_MMU_TEST_help) # Internal for testing

    cmd_MMU_TEST_help = "Internal Happy Hare development tests"
    def cmd_MMU_TEST(self, gcmd):
        self.mmu._is_running_test = True
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.mmu.log_info("SYNC_STATE=['compression'|'tension'|'both'|neutral] : Set the sync state")
            self.mmu.log_info("SYNC_EVENT=[-1.0 ... 1.0] : Generate sync feedback event")
            self.mmu.log_info("DUMP_UNICODE=1 : Display special characters used in display")
            self.mmu.log_info("RUN_SEQUENCE=1 : Run through the set of sequence macros tracking time")
            self.mmu.log_info("GET_POS=1 : Fetch the current filament position state")
            self.mmu.log_info("SET_POS=<pos_state> : Set the current filament position state")
            self.mmu.log_info("GET_POSITION=1 : Fetch the current filament position")
            self.mmu.log_info("SET_POSITION=<pos> : Fetch the current filament position")
            self.mmu.log_info("SYNC_LOAD_TEST=1 : Hammer stepper syncing and movement. Parama: LOOP|HOME")
            self.mmu.log_info("SEL_MOVE=1 : Selector homing move. Params: MOVE|SPEED|ACCEL|WAIT|LOOP")
            self.mmu.log_info("SEL_HOMING_MOVE=1 : Selector homing move. Params: MOVE|SPEED|ACCEL|WAIT|LOOP|ENDSTOP")
            self.mmu.log_info("SEL_LOAD_TEST=1 : Load test selector movements. Params: LOOP|ENDSTOP")
            self.mmu.log_info("TTC_TEST=1 : Provoke known TTC condition. Parms: LOOP|MIX|DEBUG")
            self.mmu.log_info("TTC_TEST2=1 : Provoke known TTC condition. Parms: LOOP|MIX|DEBUG")
            self.mmu.log_info("TTC_TEST3=1 : Provoke known TTC condition. Parms: LOOP|MIX|DEBUG")
            self.mmu.log_info("STEPCOMPRESS_TEST=1 : Provoke stepcompress error. Parms: LOOP|MIX|DEBUG")
            self.mmu.log_info("SYNC_G2E=1 : Sync gear to extruder")
            self.mmu.log_info("SYNC_E2G=1 : Sync extruder to gear. Params: EXTRUDER_ONLY")
            self.mmu.log_info("UNSYNC=1 : Unsync")

        sync_state = gcmd.get('SYNC_STATE', None)
        if sync_state is not None:
            compression_sensor = self.mmu.printer.lookup_object("filament_switch_sensor %s_sensor" % self.mmu.SENSOR_COMPRESSION, None)
            tension_sensor = self.mmu.printer.lookup_object("filament_switch_sensor %s_sensor" % self.mmu.SENSOR_TENSION, None)
            if sync_state == 'loop':
                nb_iterations = gcmd.get_int('LOOP', 1000, minval=1, maxval=10000000)
                gathered_states = []
                tests = []

                def get_float_state(compression=None, tension=None):
                    '''
                    Return the expected float state with given inputs and current sensor states.
                    '''
                    if compression is None:
                        compression = compression_sensor.runout_helper.filament_present
                    if tension is None:
                        tension = tension_sensor.runout_helper.filament_present
                    if compression == tension:
                        return 0.
                    elif compression:
                        return 1.
                    else:
                        return -1.
                def test_2_string(test):
                    return "compression=%s, tension=%s, toggle_compression=%s, toggle_tension=%s" % (test[0], test[1], test[2], test[3])
                def gather_state(state):
                    gathered_states.append(state)
                def wait_for_results():
                    while len(gathered_states) != len(tests):
                        pass
                    display_results()
                def display_results():
                    nb_tests_by_expected = {}
                    self.mmu.log_info("NB Gathered states: %s" % len(gathered_states))
                    self.mmu.log_info("NB Tests: %s" % len(tests))
                    # For each configuration print the number of times it was run
                    self.mmu.log_debug("Configuration repartition")
                    nb_hits = {}
                    for comp in [None, True, False]:
                        for tens in [None, True, False]:
                            for t in tests:
                                if (comp, tens) not in nb_hits:
                                    nb_hits.update({(comp, tens): 0})
                                if (t[0][0], t[0][1]) == (comp, tens):
                                    nb_hits[(comp, tens)] += 1
                    # Print the hits from most to least frequent
                    for key, value in sorted(nb_hits.items(), key=lambda item: item[1], reverse=True):
                        self.mmu.log_debug("   compression : %s - tension : %s -> %s" % (key[0], key[1], value))

                    # Group by expected result and print how many tests should result in that state
                    self.mmu.log_debug("Expected state repartition")
                    for expected in [1.,0.,-1.]:
                        count = len([1 for __, sync_state_float in tests if sync_state_float == expected])
                        self.mmu.log_debug("   Expected state %s -> %s" % (expected, count))
                        nb_tests_by_expected.update({expected: count})

                    mismatches = {}
                    for i, (test, sync_state_float) in enumerate(tests):
                        if test not in mismatches:
                            mismatches.update({test: 0})
                        if sync_state_float != gathered_states[i]:
                            mismatches[test] += 1
                    # Display mismatches
                    self.mmu.log_info("Total Mismatches: "+str(sum(mismatches.values())) + '/' + str(len(tests)) + ' (' + str(round(sum(mismatches.values()) / len(tests) * 100, 2)) +' %)')

                    if mismatches:
                        self.mmu.log_info("See mmu.log for a detailed list")
                        # Sort by most mismatches.values (highest first)
                        mismatches = dict(sorted(mismatches.items(), key=lambda item: item[1], reverse=True))
                        for test, count in mismatches.items():
                            self.mmu.log_debug("MISMATCH: %s -> %s" % (test_2_string(test), count))
                        # Summary displaying which expected state has which percentage of total errors
                        for expected in [1,0,-1]:
                            if nb_tests_by_expected[expected]:
                                count = sum([c for test, c in mismatches.items() if test[1] == expected])
                                self.mmu.log_debug(">>>>>> Expected state " + str(expected) + " -> " + str(count) + '/' + str(nb_tests_by_expected[expected]) + ' (' + str(round(count / nb_tests_by_expected[expected] * 100, 2)) + ' %)')
                                self.mmu.log_debug("   Edge detection error repartition")
                                # Group by compression rising edge
                                count = sum([c for test, c in mismatches.items() if test[1] == expected and test[2] == 'rising edge'])
                                self.mmu.log_debug("      " + str(count) + '/' + str(nb_tests_by_expected[expected]) + ' (' + str(round(count / nb_tests_by_expected[expected] * 100, 2)) + ' %) ' + 'compression rising edge')
                                # Group by compression falling edge
                                count = sum([c for test, c in mismatches.items() if test[1] == expected and test[2] == 'falling edge'])
                                self.mmu.log_debug("      " + str(count) + '/' + str(nb_tests_by_expected[expected]) + ' (' + str(round(count / nb_tests_by_expected[expected] * 100, 2)) + ' %) ' + 'compression falling edge')
                                # Group by tension rising edge
                                count = sum([c for test, c in mismatches.items() if test[1] == expected and test[3] == 'rising edge'])
                                self.mmu.log_debug("      " + str(count) + '/' + str(nb_tests_by_expected[expected]) + ' (' + str(round(count / nb_tests_by_expected[expected] * 100, 2)) + ' %) ' + 'tension rising edge')
                                # Group by tension falling edge
                                count = sum([c for test, c in mismatches.items() if test[1] == expected and test[3] == 'falling edge'])
                                self.mmu.log_debug("      " + str(count) + '/' + str(nb_tests_by_expected[expected]) + ' (' + str(round(count / nb_tests_by_expected[expected] * 100, 2)) + ' %) ' + 'tension falling edge')

                    else:
                        self.mmu.log_info("No mismatches")

                self.mmu.printer.register_event_handler("mmu:sync_feedback_finished", gather_state)
                self.mmu.printer.register_event_handler("mmu:test_gen_finished", wait_for_results)

                while len(tests) < nb_iterations:
                    compression_sensor_filament_present = None
                    tension_sensor_filament_present = None
                    toggle_compression = "no change"
                    toggle_tension = "no change"
                    if random.choice([True, False]):
                        if compression_sensor is not None:
                            compression_sensor_filament_present = random.choice([True, False])
                            sync_state_float = get_float_state(compression=compression_sensor_filament_present, tension=tension_sensor_filament_present)
                            if compression_sensor_filament_present != compression_sensor.runout_helper.filament_present:
                                toggle_compression = "rising edge" if compression_sensor_filament_present else "falling edge"
                                compression_sensor.runout_helper.note_filament_present(compression_sensor_filament_present)
                                tests.append([(compression_sensor_filament_present, tension_sensor_filament_present, toggle_compression, toggle_tension), sync_state_float])
                        if tension_sensor is not None:
                            tension_sensor_filament_present = random.choice([True, False])
                            sync_state_float = get_float_state(compression=compression_sensor_filament_present, tension=tension_sensor_filament_present)
                            if tension_sensor_filament_present != tension_sensor.runout_helper.filament_present:
                                toggle_tension = "rising edge" if tension_sensor_filament_present else "falling edge"
                                tension_sensor.runout_helper.note_filament_present(tension_sensor_filament_present)
                                tests.append([(compression_sensor_filament_present, tension_sensor_filament_present, toggle_compression, toggle_tension), sync_state_float])
                        if len(tests) >= nb_iterations:
                            break
                    else:
                        if tension_sensor is not None:
                            tension_sensor_filament_present = random.choice([True, False])
                            sync_state_float = get_float_state(compression=compression_sensor_filament_present, tension=tension_sensor_filament_present)
                            if tension_sensor_filament_present != tension_sensor.runout_helper.filament_present:
                                toggle_tension = "rising edge" if tension_sensor_filament_present else "falling edge"
                                tension_sensor.runout_helper.note_filament_present(tension_sensor_filament_present)
                                tests.append([(compression_sensor_filament_present, tension_sensor_filament_present, toggle_compression, toggle_tension), sync_state_float])
                        if compression_sensor is not None:
                            compression_sensor_filament_present = random.choice([True, False])
                            sync_state_float = get_float_state(compression=compression_sensor_filament_present, tension=tension_sensor_filament_present)
                            if compression_sensor_filament_present != compression_sensor.runout_helper.filament_present:
                                toggle_compression = "rising edge" if compression_sensor_filament_present else "falling edge"
                                compression_sensor.runout_helper.note_filament_present(compression_sensor_filament_present)
                                tests.append([(compression_sensor_filament_present, tension_sensor_filament_present, toggle_compression, toggle_tension), sync_state_float])

                self.mmu.printer.send_event("mmu:test_gen_finished")

            else:
                if sync_state == 'compression':
                    if compression_sensor is not None:
                        self.mmu.log_info("Setting compression sensor to 'detected'")
                        compression_sensor_filament_present = True
                    if tension_sensor is not None:
                        self.mmu.log_info("Setting tension sensor to 'not detected'")
                        tension_sensor_filament_present = False
                elif sync_state == 'tension':
                    if compression_sensor is not None:
                        self.mmu.log_info("Setting compression sensor to 'not detected'")
                        compression_sensor_filament_present = False
                    if tension_sensor is not None:
                        self.mmu.log_info("Setting tension sensor to 'detected'")
                        tension_sensor_filament_present = True
                elif sync_state in ['both', 'neutral']:
                    if compression_sensor is not None:
                        self.mmu.log_info("Setting compression sensor to 'detected'")
                        compression_sensor_filament_present = True
                    if tension_sensor is not None:
                        self.mmu.log_info("Setting tension sensor to 'detected'")
                        tension_sensor_filament_present = True
                else:
                    self.mmu.log_error("Invalid sync state: %s" % sync_state)
                # Generate a tension or compression event
                self.mmu.log_trace(">>>>>> sync test Testing configuration %s" % (sync_state.upper()))
                if compression_sensor is not None:
                    compression_sensor.runout_helper.note_filament_present(compression_sensor_filament_present)
                if tension_sensor is not None:
                    tension_sensor.runout_helper.note_filament_present(tension_sensor_filament_present)
            # Remove event handlers
            self.mmu.printer.event_handlers.pop("mmu:sync_feedback_finished", None)
            self.mmu.printer.event_handlers.pop("mmu:test_gen_finished", None)
            return


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
                        self.mmu.wrap_gcode_command(self.mmu.pre_unload_macro, exception=False, wait=True)
                    self.mmu.wrap_gcode_command(self.mmu.post_form_tip_macro, exception=False, wait=True)
                    with self.mmu._wrap_track_time('post_unload'):
                        self.mmu.wrap_gcode_command(self.mmu.post_unload_macro, exception=False, wait=True)
                with self.mmu._wrap_track_time('load'):
                    with self.mmu._wrap_track_time('pre_load'):
                        self.mmu.wrap_gcode_command(self.mmu.pre_load_macro, exception=False, wait=True)
                    with self.mmu._wrap_track_time('post_load'):
                        self.mmu.wrap_gcode_command(self.mmu.post_load_macro, exception=False, wait=False)
                        if error:
                            self.mmu.wrap_gcode_command("MMU_PAUSE")
            self.mmu.log_info("Statistics:%s" % self.mmu.last_statistics)
            self.mmu._set_print_state("idle")

        if gcmd.get_int('RUN_CHANGE_SEQUENCE', 0, minval=0, maxval=1):
            pause = gcmd.get_int('PAUSE', 0, minval=0, maxval=1)
            next_pos = gcmd.get('NEXT_POS', "last")
            goto_pos = None
            if next_pos == 'next':
                self.mmu.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=_MMU_SEQUENCE_VARS VARIABLE=restore_xy_pos VALUE='\"%s\"'" % next_pos)
                goto_pos = [11, 11]
                self.mmu._set_next_position(goto_pos)
            if gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1):
                self.mmu._set_print_state("printing")
            self.mmu._save_toolhead_position_and_park('toolchange', next_pos=goto_pos)
            with self.mmu._wrap_track_time('total'):
                try:
                    with self.mmu._wrap_track_time('unload'):
                        with self.mmu._wrap_track_time('pre_unload'):
                            self.mmu.wrap_gcode_command(self.mmu.pre_unload_macro, exception=False, wait=True)
                        self.mmu.wrap_gcode_command(self.mmu.post_form_tip_macro, exception=False, wait=True)
                        with self.mmu._wrap_track_time('post_unload'):
                            self.mmu.wrap_gcode_command(self.mmu.post_unload_macro, exception=False, wait=True)
                    with self.mmu._wrap_track_time('load'):
                        with self.mmu._wrap_track_time('pre_load'):
                            self.mmu.wrap_gcode_command(self.mmu.pre_load_macro, exception=False, wait=True)
                        if pause:
                            raise MmuError("TEST ERROR")
                        else:
                            with self.mmu._wrap_track_time('post_load'):
                                self.mmu.wrap_gcode_command(self.mmu.post_load_macro, exception=False, wait=True)
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

        pos = gcmd.get_float('SET_POS', -1, minval=0, maxval=10)
        if pos >= 0:
            self.mmu._set_filament_pos_state(pos)

        position = gcmd.get_float('SET_POSITION', -1, minval=0)
        if position >= 0:
            self.mmu._set_filament_position(position)

        if gcmd.get_int('GET_POSITION', 0, minval=0, maxval=1):
            self.mmu.log_info("Filament position: %s" % self.mmu._get_filament_position())

        action = gcmd.get_float('SET_ACTION', -1, minval=0)
        if action >= 0:
            self.mmu.action = action

        if gcmd.get_int('SYNC_LOAD_TEST', 0, minval=0, maxval=1):
            try:
                self.mmu._is_running_test = True
                endstop = gcmd.get('ENDSTOP', 'toolhead')
                loop = gcmd.get_int('LOOP', 10, minval=1, maxval=1000)
                select = gcmd.get_int('SELECT', 0, minval=0, maxval=1)
                self.mmu.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=mmu_test")
                self.mmu._initialize_filament_position()
                total = 0.
                for i in range(loop):
                    endstop="toolhead" if random.randint(0, 1) else "extruder"
                    move_type = random.randint(0, 11) # 12 to enable tracking test
                    move = random.randint(0, 100) - 50
                    speed = random.uniform(50, 200)
                    accel = random.randint(50, 1000)
                    homing = random.randint(0, 1)
                    extruder_only = random.randint(0, 1)
                    motor = random.choice(["gear", "gear+extruder", "extruder"])
                    if select and self.mmu.mmu_machine.multigear:
                        if random.randint(0, 1):
                            gate = random.randint(0, self.mmu.num_gates - 1)
                            self.mmu.log_info("Selecting gate: %d" % gate)
                            self.mmu.select_gate(gate)
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
                self.mmu._is_running_test = False

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
            endstop = gcmd.get('ENDSTOP', self.mmu.SENSOR_SELECTOR_TOUCH if self.mmu.selector.use_touch_move() else self.mmu.SENSOR_SELECTOR_HOME)
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
            debug = gcmd.get_int('DEBUG', 0, minval=0, maxval=1)
            mix = gcmd.get_int('MIX', 0, minval=0, maxval=1)
            try:
                self.mmu._is_running_test = True
                for i in range(loop):
                    self.mmu.log_info("Loop: %d" % i)
                    if self.mmu.mmu_machine.multigear: # PAUL should be mmu_unit of current gate
                        self.mmu.select_gate(random.randint(0, self.mmu.num_gates - 1))
                    stop_on_endstop = random.choice([-1, 1])
                    motor = "gear+extruder" if random.randint(0, mix) else "extruder"
                    self.mmu.gcode.run_script_from_command("MMU_TEST_HOMING_MOVE MOTOR=%s MOVE=5 ENDSTOP=toolhead STOP_ON_ENDSTOP=%d DEBUG=%d" % (motor, stop_on_endstop, debug))
                    if random.randint(0, 1):
                        self.mmu.gcode.run_script_from_command("MMU_TEST_MOVE MOTOR=%s MOVE=5 DEBUG=%d" % (motor, debug))
                    if random.randint(0, 1):
                        self.mmu.mmu_toolhead.get_last_move_time() # Try to provoke TTC
            finally:
                self.mmu._is_running_test = False

        if gcmd.get_int('TTC_TEST2', 0, minval=0, maxval=1):
            loop = gcmd.get_int('LOOP', 5, minval=1, maxval=1000)
            debug = gcmd.get_int('DEBUG', 0, minval=0, maxval=1)
            mix = gcmd.get_int('MIX', 0, minval=0, maxval=1)
            try:
                self.mmu._is_running_test = True
                for i in range(loop):
                    stop_on_endstop = random.choice([-1, 0, 1])
                    wait = random.randint(0, 1)
                    self.mmu.log_info("Loop: %d" % i)
                    motor = "gear+extruder" if random.randint(0, mix) else "extruder"
                    self.mmu.trace_filament_move("test", 5, motor=motor, homing_move=stop_on_endstop, endstop_name="toolhead", wait=wait)
                    if random.randint(0, 1):
                        self.mmu.gcode.run_script_from_command("M83")
                        self.mmu.gcode.run_script_from_command("G1 E5 F300")
            finally:
                self.mmu._is_running_test = False

        if gcmd.get_int('TTC_TEST3', 0, minval=0, maxval=1):
            loop = gcmd.get_int('LOOP', 5, minval=1, maxval=1000)
            debug = gcmd.get_int('DEBUG', 0, minval=0, maxval=1)
            try:
                self.mmu._is_running_test = True
                for i in range(loop):
                    self.mmu.log_info("Loop: %d" % i)
                    if self.mmu.mmu_machine.multigear: # PAUL should be mmu_unit of current gate
                        self.mmu.select_gate(random.randint(0, self.mmu.num_gates - 1))
                    stop_on_endstop = random.choice([-1, 1])
                    motor = "gear"
                    self.mmu.gcode.run_script_from_command("MMU_TEST_HOMING_MOVE MOTOR=%s MOVE=-70 SPEED=300 ACCEL=1000 ENDSTOP=toolhead STOP_ON_ENDSTOP=%d DEBUG=%d" % (motor, stop_on_endstop, debug))
                    if random.randint(0, 1):
                        self.mmu.gcode.run_script_from_command("MMU_TEST_MOVE MOTOR=%s MOVE=5 DEBUG=%d" % (motor, debug))
                    if random.randint(0, 1):
                        self.mmu.mmu_toolhead.get_last_move_time() # Try to provoke TTC
            finally:
                self.mmu._is_running_test = False

        if gcmd.get_int('STEPCOMPRESS_TEST', 0, minval=0, maxval=1):
            loop = gcmd.get_int('LOOP', 1, minval=1, maxval=1000)
            debug = gcmd.get_int('DEBUG', 0, minval=0, maxval=1)
            motor = gcmd.get('MOTOR', None)
            wait = gcmd.get_int('WAIT', None, minval=0, maxval=1)
            select = gcmd.get_int('SELECT', 1, minval=0, maxval=1)
            stop_on_endstop = gcmd.get_int('STOP_ON_ENDSTOP', None, minval=-1, maxval=1)
            try:
                self.mmu._is_running_test = True
                for i in range(loop):
                    self.mmu.log_info("Loop: %d" % i)
                    if self.mmu.mmu_machine.multigear and select:
                        self.mmu.select_gate(random.randint(0, self.mmu.num_gates - 1))
                    logging.info("Moving extruder 1mm with G1")
                    self.mmu.gcode.run_script_from_command("M83")
                    self.mmu.gcode.run_script_from_command("G1 E1 F300")
                    if motor is None:
                        motor = "gear+extruder" if random.randint(0, 1) else "extruder"
                    if stop_on_endstop is None:
                        stop_on_endstop = random.choice([-1, 0, 1])
                    if wait is None:
                        wait = random.randint(0, 1)
                    with DebugStepperMovement(self.mmu, debug):
                        self.mmu.trace_filament_move("test", 1, motor=motor, homing_move=stop_on_endstop, endstop_name="toolhead", wait=wait)
            finally:
                self.mmu._is_running_test = False

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

        fil_pos = gcmd.get_int('FILAMENT_POS', -2, minval=-1, maxval=10)
        if fil_pos != -2:
            self.mmu._set_filament_pos_state(fil_pos)
        # Restore non testing context
        self.mmu._is_running_test = False
