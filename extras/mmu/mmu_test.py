# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Define internal test operations to aid development. Note these tests are "raw"
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import random, logging, math

from ..mmu_sensors import MmuSensors

# Happy Hare imports
from ..            import mmu_machine
from ..mmu_machine import MmuToolHead

# MMU subcomponent clases
from .mmu_shared   import *
from .mmu_utils    import PurgeVolCalculator, DebugStepperMovement

class SyncStateTest(object):
    '''
    This class describes what a sync_state test case is and offers methods to manipulate them.
    '''
    def __init__(self, compression_sensor, tension_sensor, compression_state, tension_state, toggle_compression, toggle_tension, triggered_sensor, id):
        self.compression_sensor = compression_sensor.runout_helper.sensor_enabled
        self.tension_sensor = tension_sensor.runout_helper.sensor_enabled
        self.compression_state = compression_state
        self.tension_state = tension_state
        self.toggle_compression = toggle_compression
        self.toggle_tension = toggle_tension
        self.expected = None
        self.triggered_sensor = triggered_sensor
        self.id = id
        self.set_expected()

    def set_expected(self):
        '''
        Return the expected float state with given inputs and current sensor states.
        '''
        if self.compression_sensor and self.tension_sensor:
            if self.compression_state == self.tension_state:
                self.expected = 0.
            elif self.compression_state and not self.tension_state:
                self.expected = 1.
            elif not self.compression_state and self.tension_state:
                self.expected = -1.
        elif self.compression_sensor:
            if self.compression_state:
                self.expected = 1.
            else:
                self.expected = -1.
        elif self.tension_sensor:
            if self.tension_state:
                self.expected = -1.
            else:
                self.expected = 1.
        else :
            self.expected = 0.

    def __str__(self):
        return "compression_sensor=%s, tension_sensor=%s, compression_state=%s, tension_state=%s, toggle_compression=%s, toggle_tension=%s, triggered_sensor=%s" % (self.compression_sensor, self.tension_sensor, self.compression_state, self.tension_state, self.toggle_compression, self.toggle_tension, self.triggered_sensor)

    def __repr__(self):
        return self.__str__()

class MmuTest:

    def __init__(self, mmu):
        self.mmu = mmu
        mmu.gcode.register_command('_MMU_TEST', self.cmd_MMU_TEST, desc = self.cmd_MMU_TEST_help) # Internal for testing

    cmd_MMU_TEST_help = "Internal Happy Hare developer tests"
    def cmd_MMU_TEST(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.mmu.log_info("SYNC_STATE=['compression'|'tension'|'both'|neutral] : Set the sync state")
            self.mmu.log_info("SYNC_EVENT=[-1.0 ... 1.0] : Generate sync feedback event")
            self.mmu.log_info("DUMP_UNICODE=1 : Display special characters used in display")
            self.mmu.log_info("RUN_SEQUENCE=1 : Run through the set of sequence macros tracking time")
            self.mmu.log_info("GET_POS=1 : Fetch the current filament position state")
            self.mmu.log_info("SET_POS=<pos_state> : Set the current filament position state")
            self.mmu.log_info("SET_RD=<gear_rd> [GATE=]: Update the specified gate's rotation distance")
            self.mmu.log_info("GET_POSITION=1 : Fetch the current filament position")
            self.mmu.log_info("SET_POSITION=<pos> : Fetch the current filament position")
            self.mmu.log_info("SYNC_LOAD_TEST=1 : Hammer stepper syncing and movement. Params: LOOP|HOME|WAIT")
            self.mmu.log_info("REALISTIC_SYNC_TEST=1 : Load test normal stepper syncing and movement. Params: LOOP|SELECT|ENDSTOP")
            self.mmu.log_info("QUIESCE_TEST=1 : Quick test of problematic sync changes")
            self.mmu.log_info("SEL_MOVE=1 : Selector homing move. Params: MOVE|SPEED|ACCEL|WAIT|LOOP")
            self.mmu.log_info("SEL_HOMING_MOVE=1 : Selector homing move. Params: MOVE|SPEED|ACCEL|WAIT|LOOP|ENDSTOP")
            self.mmu.log_info("SEL_LOAD_TEST=1 : Load test selector movements. Params: LOOP|ENDSTOP")
            self.mmu.log_info("TTC_TEST=1 : Provoke known TTC condition. Params: LOOP|MIX|DEBUG|WAIT")
            self.mmu.log_info("TTC_TEST2=1 : Provoke known TTC condition. Params: LOOP|MIX|DEBUG|WAIT")
            self.mmu.log_info("TTC_TEST3=1 : Provoke known TTC condition. Params: LOOP|MIX|DEBUG|WAIT")
            self.mmu.log_info("STEPCOMPRESS_TEST=1 : Provoke stepcompress error. Parms: LOOP|MIX|DEBUG|WAIT")
            self.mmu.log_info("AUTO_CALIBRATE=1 [GATE=] [DIRECTION=] [RATIO=] [HOMING=] : Call auto-calibrate function directly")
            self.mmu.log_info("GATE_MOTOR=n : Select the specified gear motor on type-B designs")
            self.mmu.log_info("SYNC_G2E=1 : Sync gear to extruder (user mode)")
            self.mmu.log_info("SYNC_E2G=1 [EXTRUDER_ONLY=] : Sync extruder to gear optionally just the extruder on rail")
            self.mmu.log_info("UNSYNC=1 [GEAR_ONLY=]: Unsync (user mode) or unsynced with assuption of no extruder movement")
            return

        def log(msg):
            self.mmu.log_info(msg)
            logging.info("MMU: %s" % msg)

        try:
            self.mmu._is_running_test = True
            have_run_test = False

            sync_state = gcmd.get('SYNC_STATE', None)
            if sync_state is not None:
                have_run_test = True
                # Create phony sensors for testing purposes (will be removed after the test)
                mmu_sensors = self.mmu.printer.lookup_object("mmu_sensors")
                config = self.mmu.config.getsection('mmu_sensors')
                sensors_to_remove = []
                compression_sensor_filament_present = tension_sensor_filament_present = False

                # Use the temporary sensors for the test if the real ones are not present or disabled
                compression_test_sensor = mmu_sensors.sensors.get(self.mmu.SENSOR_COMPRESSION, None)
                if compression_test_sensor is None or not compression_test_sensor.runout_helper.sensor_enabled:
                    mmu_sensors._create_mmu_sensor(config, self.mmu.SENSOR_COMPRESSION, None, 'test_'+self.mmu.SENSOR_COMPRESSION+'_pin', 0, button_handler=mmu_sensors._sync_compression_callback)
                    compression_test_sensor = mmu_sensors.sensors.get(self.mmu.SENSOR_COMPRESSION, None)
                    sensors_to_remove.append(self.mmu.SENSOR_COMPRESSION)
                tension_test_sensor = mmu_sensors.sensors.get(self.mmu.SENSOR_TENSION, None)
                if tension_test_sensor is None or not tension_test_sensor.runout_helper.sensor_enabled:
                    mmu_sensors._create_mmu_sensor(config, self.mmu.SENSOR_TENSION, None, 'test_'+self.mmu.SENSOR_TENSION+'_pin', 0, button_handler=mmu_sensors._sync_tension_callback)
                    tension_test_sensor = mmu_sensors.sensors.get(self.mmu.SENSOR_TENSION, None)
                    sensors_to_remove.append(self.mmu.SENSOR_TENSION)

                if sync_state == 'loop':
                    nb_iterations = gcmd.get_int('LOOP', 1000, minval=1, maxval=10000000)
                    gathered_states = []
                    tests = []
                    global finished
                    finished = False
                    # save the sensor state
                    saved_compr = compression_test_sensor.runout_helper.sensor_enabled
                    saved_tens = tension_test_sensor.runout_helper.sensor_enabled

                    def gather_state(state):
                        self.mmu.log_trace(" -- Gathered state: %s" % len(gathered_states))
                        gathered_states.append(state)
                    def wait_for_results():
                        while len(gathered_states) < len(tests):
                            pass
                        self.mmu.log_trace(" -- All states were gatherred : %s" % len(gathered_states))
                        display_results()
                        global finished
                        finished = True
                    def wait_run():
                        global finished
                        while not finished:
                            pass
                        finished = False
                    def display_results():
                        nb_tests_by_expected = {}
                        # For each configuration print the number of times it was run
                        self.mmu.log_debug("Configuration repartition")
                        nb_hits = {}
                        for comp in [None, True, False]:
                            for tens in [None, True, False]:
                                for t in tests:
                                    if (comp, tens) not in nb_hits:
                                        nb_hits.update({(comp, tens): 0})
                                    if (t.compression_state, t.tension_state) == (comp, tens):
                                        nb_hits[(comp, tens)] += 1
                        # Print the hits from most to least frequent
                        for key, value in sorted(nb_hits.items(), key=lambda item: item[1], reverse=True):
                            if value : self.mmu.log_debug("   compression : %s - tension : %s -> %s" % (key[0], key[1], value))

                        # Group by expected result and print how many tests should result in that state
                        self.mmu.log_debug("Expected state repartition")
                        for expected in [1.,0.,-1.]:
                            count = len([1 for t in tests if t.expected == expected])
                            self.mmu.log_debug("   Expected state %s -> %s" % (expected, count))
                            nb_tests_by_expected.update({expected: count})

                        mismatches = {}
                        for i, test in enumerate(tests):
                            if str(test) not in mismatches:
                                mismatches[str(test)] = {'test' : test, 'count' : 0}
                            if test.expected != gathered_states[i]:
                                self.mmu.log_debug("MISMATCH on test id:%s (expected : %s) -> %s" % (str(test.id), test.expected, gathered_states[i]))
                                self.mmu.log_debug("   Test : %s" % (str(test)))
                                mismatches[str(test)]['count'] += 1
                        # Display mismatches
                        nb_mismatches = sum([v['count'] for v in mismatches.values()])
                        self.mmu.log_info("Total Mismatches: "+str(nb_mismatches) + '/' + str(len(tests)) + ' (' + str(round(nb_mismatches / len(tests) * 100, 2)) +' %)')

                        if mismatches:
                            # Sort by most mismatches.values (highest first)
                            mismatches = dict(sorted(mismatches.items(), key=lambda item: item[1]['count'], reverse=True))
                            for test_str, info in mismatches.items():
                                if info['count'] : self.mmu.log_debug("MISMATCH: %s (expected : %s) -> %s" % (test_str, info['test'].expected, info['count']))
                            # Summary displaying which expected state has which percentage of total errors
                            for expected in [1,0,-1]:
                                if nb_tests_by_expected[expected]:
                                    nb_err_count = sum([info['count'] for __, info in mismatches.items() if info['test'].expected == expected])
                                    self.mmu.log_debug(">>>>>> Expected state " + str(expected) + " -> " + str(nb_err_count) + '/' + str(nb_tests_by_expected[expected]) + ' (' + str(round(nb_err_count / nb_tests_by_expected[expected] * 100, 2)) + ' %)')
                                    self.mmu.log_debug("   Edge detection error repartition")
                                    # Group by compression rising edge
                                    count = sum([info['count'] for __, info in mismatches.items() if info['test'].expected == expected and info['test'].toggle_compression == 'rising edge'])
                                    if count : self.mmu.log_debug("      " + str(count) + '/' + str(nb_err_count) + ' (' + str(round(((count / nb_err_count) if nb_err_count else 0) * 100, 2)) + ' %) ' + 'compression rising edge')
                                    # Group by compression falling edge
                                    count = sum([info['count'] for __, info in mismatches.items() if info['test'].expected == expected and info['test'].toggle_compression == 'falling edge'])
                                    if count : self.mmu.log_debug("      " + str(count) + '/' + str(nb_err_count) + ' (' + str(round(((count / nb_err_count) if nb_err_count else 0) * 100, 2)) + ' %) ' + 'compression falling edge')
                                    # Group by tension rising edge
                                    count = sum([info['count'] for __, info in mismatches.items() if info['test'].expected == expected and info['test'].toggle_tension == 'rising edge'])
                                    if count : self.mmu.log_debug("      " + str(count) + '/' + str(nb_err_count) + ' (' + str(round(((count / nb_err_count) if nb_err_count else 0) * 100, 2)) + ' %) ' + 'tension rising edge')
                                    # Group by tension falling edge
                                    count = sum([info['count'] for __, info in mismatches.items() if info['test'].expected == expected and info['test'].toggle_tension == 'falling edge'])
                                    if count : self.mmu.log_debug("      " + str(count) + '/' + str(nb_err_count) + ' (' + str(round(((count / nb_err_count) if nb_err_count else 0) * 100, 2)) + ' %) ' + 'tension falling edge')

                        else:
                            self.mmu.log_info("No mismatches")

                    self.mmu.printer.register_event_handler("mmu:sync_feedback_finished", gather_state)
                    self.mmu.printer.register_event_handler("mmu:test_gen_finished", wait_for_results)
                    # randomly remove tension or compression sensor
                    for sensor_scenario in ['compression_only', 'tension_only', 'both', 'none']:
                        if sensor_scenario == 'compression_only':
                            compression_test_sensor.runout_helper.sensor_enabled = True
                            tension_test_sensor.runout_helper.sensor_enabled = False
                        elif sensor_scenario == 'tension_only':
                            compression_test_sensor.runout_helper.sensor_enabled = False
                            tension_test_sensor.runout_helper.sensor_enabled = True
                        elif sensor_scenario == 'both':
                            compression_test_sensor.runout_helper.sensor_enabled = True
                            tension_test_sensor.runout_helper.sensor_enabled = True
                        else:
                            compression_test_sensor.runout_helper.sensor_enabled = False
                            tension_test_sensor.runout_helper.sensor_enabled = False

                        self.mmu.log_info(">>>>>> Testing sensor configuration '%s'" % sensor_scenario)

                        while len(tests) < nb_iterations:
                            compression_sensor_filament_present = compression_test_sensor.runout_helper.filament_present
                            tension_sensor_filament_present = tension_test_sensor.runout_helper.filament_present
                            toggle_compression = "no change"
                            toggle_tension = "no change"
                            # generate a random sync state
                            if random.choice([True, False]): # we consider only one sensor can change state at a time
                                compression_sensor_filament_present = random.choice([True, False])
                                if compression_sensor_filament_present != compression_test_sensor.runout_helper.filament_present:
                                    compression_test_sensor.runout_helper.note_filament_present(compression_sensor_filament_present)
                                    triggered_sensor = "compression"
                                    toggle_compression = "rising edge" if compression_sensor_filament_present else "falling edge"
                                    self.mmu.log_trace(" -- Generated test: %s" % len(tests))
                                    tests.append(SyncStateTest(compression_test_sensor, tension_test_sensor, compression_sensor_filament_present, tension_sensor_filament_present, toggle_compression, toggle_tension, triggered_sensor, len(tests)))
                            else :
                                tension_sensor_filament_present = random.choice([True, False])
                                if tension_sensor_filament_present != tension_test_sensor.runout_helper.filament_present:
                                    tension_test_sensor.runout_helper.note_filament_present(tension_sensor_filament_present)
                                    triggered_sensor = "tension"
                                    toggle_tension = "rising edge" if tension_sensor_filament_present else "falling edge"
                                    self.mmu.log_trace(" -- Generated test: %s" % len(tests))
                                    tests.append(SyncStateTest(compression_test_sensor, tension_test_sensor, compression_sensor_filament_present, tension_sensor_filament_present, toggle_compression, toggle_tension, triggered_sensor, len(tests)))


                        self.mmu.log_trace(" -- All %d were generated" % len(tests))
                        self.mmu.printer.send_event("mmu:test_gen_finished")
                        wait_run()
                        gathered_states = []
                        tests = []
                    # remove test sensors and associated config
                    ppins = self.mmu.printer.lookup_object('pins')
                    for sensor in sensors_to_remove:
                        self.mmu.printer.objects.pop("filament_switch_sensor %s_sensor"  % sensor)
                        config.fileconfig.pop("filament_switch_sensor %s_sensor"  % sensor)
                        mmu_sensors.sensors.pop(sensor)
                        share_name = "%s:%s" % (ppins.parse_pin('test_'+sensor+'_pin')['chip_name'], ppins.parse_pin('test_'+sensor+'_pin')['pin'])
                        ppins.active_pins.pop(share_name)
                        for cmd, (__, val) in self.mmu.gcode.mux_commands.items() :
                            if ("%s_sensor" % sensor) in [k for k in [v for v in val.keys() if v]]:
                                self.mmu.gcode.mux_commands[cmd][1].pop(("%s_sensor" % sensor))

                    # restore the original sensor state
                    compression_test_sensor.runout_helper.sensor_enabled = saved_compr
                    tension_test_sensor.runout_helper.sensor_enabled = saved_tens
                    self.mmu.log_info("See mmu.log for a more details")

                else:
                    if sync_state == 'compression':
                        if compression_test_sensor is not None:
                            self.mmu.log_info("Setting compression sensor to 'detected'")
                            compression_sensor_filament_present = True
                        if tension_test_sensor is not None:
                            self.mmu.log_info("Setting tension sensor to 'not detected'")
                            tension_sensor_filament_present = False
                    elif sync_state == 'tension':
                        if compression_test_sensor is not None:
                            self.mmu.log_info("Setting compression sensor to 'not detected'")
                            compression_sensor_filament_present = False
                        if tension_test_sensor is not None:
                            self.mmu.log_info("Setting tension sensor to 'detected'")
                            tension_sensor_filament_present = True
                    elif sync_state in ['both', 'neutral']:
                        if compression_test_sensor is not None:
                            self.mmu.log_info("Setting compression sensor to 'detected'")
                            compression_sensor_filament_present = True
                        if tension_test_sensor is not None:
                            self.mmu.log_info("Setting tension sensor to 'detected'")
                            tension_sensor_filament_present = True
                    else:
                        self.mmu.log_error("Invalid sync state: %s" % sync_state)
                    # Generate a tension or compression event
                    self.mmu.log_trace(">>>>>> sync test Testing configuration %s" % (sync_state.upper()))
                    if compression_test_sensor is not None:
                        compression_test_sensor.runout_helper.note_filament_present(compression_sensor_filament_present)
                    if tension_test_sensor is not None:
                        tension_test_sensor.runout_helper.note_filament_present(tension_sensor_filament_present)
                # Remove event handlers
                self.mmu.printer.event_handlers.pop("mmu:sync_feedback_finished", None)
                self.mmu.printer.event_handlers.pop("mmu:test_gen_finished", None)
                return


            feedback = gcmd.get_float('SYNC_EVENT', None, minval=-1., maxval=1.)
            if feedback is not None:
                have_run_test = True
                self.mmu.log_info("Sending 'mmu:sync_feedback %.2f' event" % feedback)
                self.mmu.printer.send_event("mmu:sync_feedback", self.mmu.toolhead.get_last_move_time(), feedback)


            if gcmd.get_int('DUMP_UNICODE', 0, minval=0, maxval=1):
                have_run_test = True
                self.mmu.log_info("UI_SPACE=%s, UI_SEPARATOR=%s, UI_DASH=%s, UI_DEGREE=%s, UI_BLOCK=%s, UI_CASCADE=%s" % (UI_SPACE, UI_SEPARATOR, UI_DASH, UI_DEGREE, UI_BLOCK, UI_CASCADE))
                self.mmu.log_info("{}{}{}{}".format(UI_BOX_TL, UI_BOX_T, UI_BOX_H, UI_BOX_TR))
                self.mmu.log_info("{}{}{}{}".format(UI_BOX_L,  UI_BOX_M, UI_BOX_H, UI_BOX_R))
                self.mmu.log_info("{}{}{}{}".format(UI_BOX_V,  UI_BOX_V, UI_SPACE, UI_BOX_V))
                self.mmu.log_info("{}{}{}{}".format(UI_BOX_BL, UI_BOX_B, UI_BOX_H, UI_BOX_BR))
                self.mmu.log_info("UI_EMOTICONS=%s" % UI_EMOTICONS)


            if gcmd.get_int('RUN_SEQUENCE', 0, minval=0, maxval=1):
                have_run_test = True
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
                have_run_test = True
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
                have_run_test = True
                self.mmu.mmu_toolhead.sync(MmuToolHead.GEAR_SYNCED_TO_EXTRUDER)


            if gcmd.get_int('SYNC_E2G', 0, minval=0, maxval=1):
                have_run_test = True
                extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1))
                self.mmu.mmu_toolhead.sync(MmuToolHead.EXTRUDER_ONLY_ON_GEAR if extruder_only else MmuToolHead.EXTRUDER_SYNCED_TO_GEAR)


            if gcmd.get_int('UNSYNC', 0, minval=0, maxval=1):
                have_run_test = True
                gear_only = bool(gcmd.get_int('GEAR_ONLY', 0, minval=0, maxval=1))
                self.mmu.mmu_toolhead.sync(MmuToolHead.GEAR_ONLY if gear_only else None)


            pos = gcmd.get_float('SET_POS', -1, minval=0, maxval=10)
            if pos >= 0:
                have_run_test = True
                self.mmu._set_filament_pos_state(pos)


            rd = gcmd.get_float('SET_RD', None, above=0)
            if rd is not None:
                have_run_test = True
                gate = gcmd.get_int('GATE', -1, minval=-2, maxval=self.mmu.num_gates)
                if gate >= 0:
                    self.mmu.calibration_manager.update_gear_rd(rd, gate)


            position = gcmd.get_float('SET_POSITION', -1, minval=0)
            if position >= 0:
                have_run_test = True
                self.mmu._set_filament_position(position)


            if gcmd.get_int('GET_POSITION', 0, minval=0, maxval=1):
                have_run_test = True
                self.mmu.log_info("Filament position: %s" % self.mmu._get_filament_position())


            action = gcmd.get_float('SET_ACTION', -1, minval=0)
            if action >= 0:
                have_run_test = True
                self.mmu.action = action


            if gcmd.get_int('QUIESCE_TEST', 0, minval=0, maxval=1):
                have_run_test = True
                self.mmu.log_warning(
                    "Setup:\n"
                    "toolhead sensor should be present or dummy ones created\n"
                    "Extruder should be hot (able to extrude)"
                )

                # Simple dispatcher so the sequence below stays clean and declarative
                def _exec(ops):
                    for i, (op, arg) in enumerate(ops, start=1):
                        if op == "g":
                            self.mmu.log_info("Step %d: %s" % (i, arg))
                            self.mmu.gcode.run_script_from_command(arg)
                        elif op == "sync":
                            self.mmu.log_info("Step %d: %s" % (i, arg))
                            self.mmu.mmu_toolhead.sync(arg)
                        elif op == "unsync":
                            self.mmu.log_info("Step %d: unsync()" % i)
                            self.mmu.mmu_toolhead.unsync()
                        else:
                            self.mmu.log_warning("Step %d: unknown op %s" % (i, op))

                ops = [
                    ("unsync", None),
                    ("g", f"G1 E-24 F6000"),
                    ("sync", MmuToolHead.GEAR_SYNCED_TO_EXTRUDER),
                    ("g", f"G1 E8 F6000"),
                    ("unsync", None),
                    ("g", f"G1 E2 F6000"),
                    ("g", "MMU_TEST_HOMING_MOVE MOTOR=gear MOVE=30 ENDSTOP=toolhead STOP_ON_ENDSTOP=1"),
                    ("g", f"G1 E-24 F6000"),
                    ("sync", MmuToolHead.EXTRUDER_SYNCED_TO_GEAR),
                    ("g", f"G1 E-2 F6000"),
                    ("g", "MMU_TEST_MOVE MOTOR=gear MOVE=30 WAIT=1"),
                    ("g", "MMU_TEST_HOMING_MOVE MOTOR=gear+extruder MOVE=30 ENDSTOP=toolhead STOP_ON_ENDSTOP=1"),
                    ("g", "MMU_TEST_MOVE MOTOR=gear+extruder MOVE=30"),
                    ("g", f"G1 E8 F6000"),
                    ("unsync", None),
                    ("g", f"G1 E-10 F6000"),
                    ("g", f"G1 E8 F6000"),
                    ("g", "MMU_TEST_MOVE MOTOR=gear MOVE=30 WAIT=0"),
                    ("g", f"G1 E-10 F6000"),
                    ("g", f"G1 E8 F6000"),
                    ("g", "MMU_TEST_MOVE MOTOR=gear MOVE=30 WAIT=0"),
                    ("g", f"G1 E-10 F6000"),
                    ("g", f"G1 E8 F6000"),
                    ("g", "MMU_TEST_MOVE MOTOR=gear MOVE=30 WAIT=0"),
                    ("g", "MMU_TEST_MOVE MOTOR=gear MOVE=30 WAIT=0"),
                    ("g", "MMU_TEST_MOVE MOTOR=gear MOVE=30 WAIT=0"),
                    ("g", "MMU_TEST_MOVE MOTOR=gear MOVE=30 WAIT=0"),
                    ("g", f"G1 E-10 F6000"),
                ]
                _exec(ops)

            # Test of all expected MMU and extruder movement (this must be bulletproof)
            # e.g. _MMU_TEST REALISTIC_SYNC_TEST=1 ENDSTOP=toolhead LOOP=100 SELECT=1 SERVO=1
            if gcmd.get_int('REALISTIC_SYNC_TEST', 0, minval=0, maxval=1):
                have_run_test = True
                self.mmu.log_warning("Setup:\ntoolhead sensor should be present or dummy ones created or use ENDSTOP=xxx\nExtruder should be hot (able to extrude)\nSELECT=1 turns on gate selection on type-B designs")
                loop = gcmd.get_int('LOOP', 10, minval=1, maxval=1000)
                select = gcmd.get_int('SELECT', 0, minval=0, maxval=1)
                servo = gcmd.get_int('SERVO', 0, minval=0, maxval=1)
                endstop = gcmd.get('ENDSTOP', "toolhead")

                try:
                    if servo:
                        self.mmu._is_running_test = False # Else servo won't move

                    for i in range(loop):
                        log("Loop: %d..." % i)

                        # Run a few randomized moves on the mmu toolhead to simulate load/unload logic (optional gate switch)
                        tracked = 0.
                        initial_pos = self.mmu._get_filament_position()
                        for j in range(6):
                            move_type = random.randint(0, 10) # 11 to enable tracking test
                            move = random.randint(0, 100) - 50
                            speed = random.uniform(50, 200)
                            accel = random.randint(50, 1000)
                            homing_move = random.randint(-1, 1)
                            motor = random.choice(["gear", "gear+extruder", "extruder"])
                            wait = random.randint(0, 1)
                            ed = random.randint(0, 4)
                            encoder_dwell = True if ed == 0 else None if ed == 1 else False

                            # Sometimes switch gate. This will test interleaved selector movement / gear rail reconfiguration
                            if select and random.randint(0, 3) == 0:
                                gate = random.randint(0, self.mmu.num_gates - 1)
                                log("Selecting gate: %d" % gate)
                                self.mmu.select_gate(gate)

                            # Sometimes inject servo movement for selectors with servo
                            if servo and random.randint(0, 4) == 0:
                                pos = "move" if random.randint(0, 1) else "down"
                                log("Moving servo to position: %s" % pos)
                                self.mmu.gcode.run_script_from_command("MMU_SERVO POS=%s" % pos)

                            log("> Internal mmu movement %d: move(%s, motor=%s, speed=%.2f, accel=%s, homing_move=%s, endstop_name=%s, encoder_dwell=%s, wait=%s)" % (j, move, motor, speed, accel, homing_move, endstop, encoder_dwell, wait))
                            actual,_,_,_ = self.mmu.trace_filament_move("REALISTIC_SYNC_TEST", move, motor=motor, speed=speed, accel=accel, homing_move=homing_move, endstop_name=endstop, encoder_dwell=encoder_dwell, speed_override=False, wait=wait)
                            tracked += actual

                        # Check MMU position is correct
                        final_pos = self.mmu._get_filament_position()
                        expected = final_pos - initial_pos
                        if not math.isclose(expected, tracked):
                            raise MmuError("TEST ERROR: inital_pos=%.6f, final_pos=%.6f, tracked=%.6f (expected=%.6f)" % (initial_pos, final_pos, tracked, expected))

                        # Run a few randomized moves on the printer toolhead to simulate user movement
                        # Sync state must either be unsynced or GEAR_SYNCED_TO_EXTRUDER
                        sync = None if random.randint(0, 1) else MmuToolHead.GEAR_SYNCED_TO_EXTRUDER
                        self.mmu.mmu_toolhead.sync(sync)
                        log(">> Extruder movement (%s)..." % ("not synced" if sync is None else "synced to extruder"))
                        for j in range(5):
                            move = random.randint(-10, 10)
                            self.mmu.gcode.run_script_from_command("G1 E%d F6000" % move)

                            # Simulate a call back into _MMU_STEP_MOVE or similar call - e.g. user calls from post_load_macro
                            if random.randint(0, 4) == 0:
                                log(">>> Calling _MMU_STEP_** move")
                                if random.randint(0, 1):
                                    self.mmu.gcode.run_script_from_command("_MMU_STEP_MOVE MOVE=10")
                                else:
                                    self.mmu.gcode.run_script_from_command("_MMU_STEP_HOMING_MOVE MOVE=10 ENDSTOP=%s" % endstop)

                    log("TEST COMPLETE")

                except MmuError as ee:
                    log("TEST TERMINATED WITH MMU EXCEPTION: %s" % str(ee))


            if gcmd.get_int('SYNC_LOAD_TEST', 0, minval=0, maxval=1):
                have_run_test = True
                self.mmu.log_warning("Setup:\ntoolhead sensor should be present or dummy ones created or use ENDSTOP=xxx\nExtruder should be hot (able to extrude)\nSELECT=0 turns off gate selection on type-B designs\nWAIT=[0|1] forces wait on movequeues condition after non-homing moves")
                endstop = gcmd.get('ENDSTOP', 'toolhead')
                loop = gcmd.get_int('LOOP', 10, minval=1, maxval=1000)
                select = gcmd.get_int('SELECT', 1, minval=0, maxval=1)
                wait = gcmd.get_int('WAIT', None, minval=0, maxval=1)
                self.mmu.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=mmu_test")
                self.mmu._initialize_filament_position()
                total = 0.
                for i in range(loop):
                    move_type = random.randint(0, 10) # 11 to enable tracking test
                    move = random.randint(0, 100) - 50
                    speed = random.uniform(50, 200)
                    accel = random.randint(50, 1000)
                    homing = random.randint(0, 1)
                    extruder_only = random.randint(0, 1)
                    motor = random.choice(["gear", "gear+extruder", "extruder"])
                    w = random.randint(0, 1) if wait is None else wait
                    if select and self.mmu.mmu_machine.multigear:
                        if random.randint(0, 1):
                            gate = random.randint(0, self.mmu.num_gates - 1)
                            log("Selecting gate: %d" % gate)
                            self.mmu.select_gate(gate)
                    if move_type in (0, 1):
                        log("Loop: %d - Synced extruder movement with G1 Ex: %.1fmm" % (i, move))
                        self.mmu.mmu_toolhead.sync(MmuToolHead.GEAR_SYNCED_TO_EXTRUDER)
                        self.mmu.gcode.run_script_from_command("G1 E%.2f F%d" % (move, speed * 60))
                    elif move_type == 2:
                        log("Loop: %d - Unsynced extruder movement with G1 Ex: %.1fmm" % (i, move))
                        self.mmu.mmu_toolhead.unsync()
                        self.mmu.gcode.run_script_from_command("G1 E%.2f F%d" % (move, speed * 60))
                    elif move_type == 3:
                        log("Loop: %d - Regular mmu move: %.1fmm, MOTOR=%s" %  (i, move, motor))
                        self.mmu.gcode.run_script_from_command("MMU_TEST_MOVE MOTOR=%s MOVE=%.2f SPEED=%d WAIT=%d" % (motor, move, speed, w))
                        total += move
                    elif move_type in (4, 5, 6):
                        log("Loop: %d - HOMING MOVE: %.1fmm, MOTOR=%s" % (i, move, motor))
                        self.mmu.gcode.run_script_from_command("MMU_TEST_HOMING_MOVE MOTOR=%s MOVE=%.2f SPEED=%d ENDSTOP=%s STOP_ON_ENDSTOP=1" % (motor, move, speed, endstop))
                        total += move
                    elif move_type == 7:
                        if random.randint(0, 1):
                            new_pos = random.uniform(0, 300)
                            log("Loop: %d - Set filament position" % i)
                            self.mmu._set_filament_position(new_pos)
                            total = new_pos
                        else:
                            log("Loop: %d - Initialized filament position" % i)
                            self.mmu._initialize_filament_position()
                            total = 0.
                    elif move_type == 8:
                        log("Loop: %d - Save gcode state" % i)
                        self.mmu.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=mmu_test")
                    elif move_type == 9:
                        log("Loop: %d - Restore gcode state" % i)
                        self.mmu.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=mmu_test")
                    elif move_type == 10:
                        log("Loop: %d - Synced extruder movement: %.1fmm" % (i, move))
                        self.mmu.gcode.run_script_from_command("MMU_TEST_MOVE MOTOR=synced MOVE=%.2f SPEED=%d WAIT=%d" % (move, speed, w))
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


            if gcmd.get_int('SEL_MOVE', 0, minval=0, maxval=1):
                have_run_test = True
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
                have_run_test = True
                move = gcmd.get_float('MOVE', 10.)
                speed = gcmd.get_float('SPEED', None)
                accel = gcmd.get_float('ACCEL', None)
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
                have_run_test = True
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
                        actual = self.mmu.selector.move("Test move", move, speed=speed, accel=accel, wait=w)
                        self.mmu.log_always("%d. Move: Rail starting pos: %s, Selector moved to %.4fmm" % (i, pos, actual))


            if gcmd.get_int('TTC_TEST', 0, minval=0, maxval=1):
                have_run_test = True
                loop = gcmd.get_int('LOOP', 10, minval=1, maxval=1000)
                debug = gcmd.get_int('DEBUG', 0, minval=0, maxval=1)
                mix = gcmd.get_int('MIX', 0, minval=0, maxval=1)
                wait = gcmd.get_int('WAIT', None, minval=0, maxval=1)
                for i in range(loop):
                    self.mmu.log_info("Loop: %d" % i)
                    w = random.randint(0, 1) if wait is None else wait
                    if self.mmu.mmu_machine.multigear:
                        self.mmu.select_gate(random.randint(0, self.mmu.num_gates - 1))
                    stop_on_endstop = random.choice([-1, 1])
                    motor = "gear+extruder" if random.randint(0, mix) else "extruder"
                    self.mmu.gcode.run_script_from_command("MMU_TEST_HOMING_MOVE MOTOR=%s MOVE=5 ENDSTOP=toolhead STOP_ON_ENDSTOP=%d DEBUG=%d" % (motor, stop_on_endstop, debug))
                    if random.randint(0, 1):
                        self.mmu.gcode.run_script_from_command("MMU_TEST_MOVE MOTOR=%s MOVE=5 DEBUG=%d WAIT=%d" % (motor, debug, w))
                    if random.randint(0, 1):
                        self.mmu.mmu_toolhead.get_last_move_time() # Try to provoke TTC


            if gcmd.get_int('TTC_TEST2', 0, minval=0, maxval=1):
                have_run_test = True
                loop = gcmd.get_int('LOOP', 10, minval=1, maxval=1000)
                debug = gcmd.get_int('DEBUG', 0, minval=0, maxval=1)
                mix = gcmd.get_int('MIX', 0, minval=0, maxval=1)
                wait = gcmd.get_int('WAIT', None, minval=0, maxval=1)
                for i in range(loop):
                    stop_on_endstop = random.choice([-1, 0, 1])
                    w = random.randint(0, 1) if wait is None else wait
                    self.mmu.log_info("Loop: %d" % i)
                    motor = "gear+extruder" if random.randint(0, mix) else "extruder"
                    self.mmu.trace_filament_move("test", 5, motor=motor, homing_move=stop_on_endstop, endstop_name="toolhead", wait=w)
                    if random.randint(0, 1):
                        self.mmu.gcode.run_script_from_command("M83")
                        self.mmu.gcode.run_script_from_command("G1 E5 F300")


            if gcmd.get_int('TTC_TEST3', 0, minval=0, maxval=1):
                have_run_test = True
                loop = gcmd.get_int('LOOP', 10, minval=1, maxval=1000)
                debug = gcmd.get_int('DEBUG', 0, minval=0, maxval=1)
                wait = gcmd.get_int('WAIT', None, minval=0, maxval=1)
                for i in range(loop):
                    self.mmu.log_info("Loop: %d" % i)
                    w = random.randint(0, 1) if wait is None else wait
                    if self.mmu.mmu_machine.multigear:
                        self.mmu.select_gate(random.randint(0, self.mmu.num_gates - 1))
                    stop_on_endstop = random.choice([-1, 1])
                    motor = "gear"
                    self.mmu.gcode.run_script_from_command("MMU_TEST_HOMING_MOVE MOTOR=%s MOVE=-70 SPEED=300 ACCEL=1000 ENDSTOP=toolhead STOP_ON_ENDSTOP=%d DEBUG=%d" % (motor, stop_on_endstop, debug))
                    if random.randint(0, 1):
                        self.mmu.gcode.run_script_from_command("MMU_TEST_MOVE MOTOR=%s MOVE=5 DEBUG=%d WAIT=%d" % (motor, debug, w))
                    if random.randint(0, 1):
                        self.mmu.mmu_toolhead.get_last_move_time() # Try to provoke TTC


            if gcmd.get_int('STEPCOMPRESS_TEST', 0, minval=0, maxval=1):
                have_run_test = True
                loop = gcmd.get_int('LOOP', 1, minval=10, maxval=1000)
                debug = gcmd.get_int('DEBUG', 0, minval=0, maxval=1)
                motor = gcmd.get('MOTOR', None)
                wait = gcmd.get_int('WAIT', None, minval=0, maxval=1)
                select = gcmd.get_int('SELECT', 1, minval=0, maxval=1)
                stop_on_endstop = gcmd.get_int('STOP_ON_ENDSTOP', None, minval=-1, maxval=1)
                for i in range(loop):
                    self.mmu.log_info("Loop: %d" % i)
                    w = random.randint(0, 1) if wait is None else wait
                    if self.mmu.mmu_machine.multigear and select:
                        self.mmu.select_gate(random.randint(0, self.mmu.num_gates - 1))
                    self.mmu.gcode.run_script_from_command("M83")
                    self.mmu.gcode.run_script_from_command("G1 E1 F300")
                    if motor is None:
                        motor = "gear+extruder" if random.randint(0, 1) else "extruder"
                    if stop_on_endstop is None:
                        stop_on_endstop = random.choice([-1, 0, 1])
                    if wait is None:
                        w = random.randint(0, 1)
                    with DebugStepperMovement(self.mmu, debug):
                        self.mmu.trace_filament_move("test", 1, motor=motor, homing_move=stop_on_endstop, endstop_name="toolhead", wait=w)


            if gcmd.get_int('AUTO_CALIBRATE', 0, minval=0, maxval=1):
                have_run_test = True
                gate = gcmd.get_int('GATE', 0, minval=-2, maxval=8)
                direction = gcmd.get_int('DIRECTION', 1, minval=-1, maxval=1)
                ratio = gcmd.get_float('RATIO', 1., minval=-1, maxval=2)
                homing_movement = gcmd.get_float('HOMING', None, minval=0, maxval=100)
                self.mmu.gate_selected = gate
                self.mmu._auto_calibrate(direction, ratio, homing_movement)


            select_gate = gcmd.get_int('GATE_MOTOR', -99, minval=self.mmu.TOOL_GATE_BYPASS, maxval=self.mmu.num_gates)
            if not select_gate == -99:
                have_run_test = True
                self.mmu.mmu_toolhead.select_gear_stepper(select_gate)


            if gcmd.get_int('CALC_PURGE', 0, minval=0, maxval=1):
                have_run_test = True
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


            runout = gcmd.get_int('RUNOUT', None, minval=0, maxval=1)
            if runout is not None:
                have_run_test = True
                if runout == 1:
                    self.mmu._enable_runout()
                elif runout == 0:
                    self.mmu._disable_runout()


            if gcmd.get_int('SENSOR', 0, minval=0, maxval=1):
                have_run_test = True
                pos = gcmd.get_int('POS', 0, minval=-1)
                gate = gcmd.get_int('GATE', 0, minval=-2, maxval=8)
                loading = bool(gcmd.get_int('LOADING', 1, minval=0, maxval=1))
                loop = gcmd.get_int('LOOP', 0, minval=0, maxval=1)

                if not loop:
                    sensors = self.mmu.sensor_manager.get_sensors_before(pos, gate, loading=loading)
                    self.mmu.log_always("check_all_sensors_before(%s,%s)=%s" % (pos, gate, self.mmu.sensor_manager.check_all_sensors_before(pos, gate, loading=loading)))
                    self.mmu.log_always("sensors before=%s" % sensors)

                    sensors = self.mmu.sensor_manager.get_sensors_after(pos, gate, loading=loading)
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
                have_run_test = True
                with self.mmu.wrap_sync_gear_to_extruder():
                    self.mmu._set_filament_pos_state(fil_pos)

            if not have_run_test:
                self.mmu.log_warning("Not a valid test. Use HELP=1 for tests")

        finally:
            self.mmu._is_running_test = False # Restore non testing context

