# Happy Hare MMU Software
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Easy setup of all filament sensors for mmu_unit
#
# Pre-gate sensors:
#   Simplifed filament switch sensor easy configuration of pre-gate sensors used to detect runout and insertion of filament
#   and preload into gate and update gate_map when possible to do so based on MMU state, not printer state
#   Essentially this uses the default `filament_switch_sensor` but then replaces the runout_helper
#   Each has name `mmu_pre_gate_X` where X is gate number
#
# mmu_gear sensor(s):
#   Wrapper around `filament_switch_sensor` setting up insert/runout callbacks with modified runout event handling
#   Named `mmu_gear`
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time

# Happy Hare imports
from ..mmu_constants import *


class MmuSensors:

    def __init__(self, config, mmu_unit, params):
        logging.info("PAUL: init() for MmuSensors")
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]

        event_delay = config.get('event_delay', 0.5)
        sf = self.mmu_machine.sensor_factory
        first_gate = mmu_unit.first_gate
        num_gates = mmu_unit.num_gates

        # Setup "mmu_pre_gate" sensors...
        self.pre_gate_sensors = {}
        for i, gate in enumerate(range(first_gate, first_gate + num_gates)):
            switch_pin = config.get('pre_gate_switch_pin_%d' % i, None)
            self.pre_gate_sensors[gate] = sf.create_mmu_sensor(
                config,
                SENSOR_PRE_GATE_PREFIX,
                gate,
                switch_pin,
                event_delay,
                insert=True,
                remove=True,
                runout=True,
                insert_remove_in_print=True
            )


        # Setup single "mmu_gate" sensor for unit...
        switch_pin = config.get('gate_switch_pin', None)
        self.gate_sensor = sf.create_mmu_sensor(
            config,
            "unit%d_%s" % (self.mmu_unit.unit_index, SENSOR_GATE),
            None,
            switch_pin,
            event_delay,
            runout=True
        )

        # Setup "mmu_gear" sensors...
        self.post_gear_sensors = {}
        for i, gate in enumerate(range(first_gate, first_gate + num_gates)):
            switch_pin = config.get('post_gear_switch_pin_%d' % i, None)

            if switch_pin:
                a_range = config.getfloatlist('post_gear_analog_range_%d' % gate, None, count=2)
                if a_range is not None:
                    a_pullup = config.getfloat('post_gear_analog_pullup_resister_%d' % gate, 4700.)
                    self.post_gear_sensors[gate] = MmuAdcSwitchSensor(
                        config,
                        SENSOR_GEAR_PREFIX,
                        gate,
                        switch_pin,
                        event_delay,
                        a_range,
                        runout=True,
                        a_pullup=a_pullup)
                else:
                    self.post_gear_sensors[gate] = sf.create_mmu_sensor(
                        config,
                        SENSOR_GEAR_PREFIX,
                        gate,
                        switch_pin,
                        event_delay,
                        runout=True)

# PAUL from v342 (reference)
# --------
#        # Setup "mmu_gear" sensors...
#        for gate in range(23):
#            switch_pin = config.get('post_gear_switch_pin_%d' % gate, None)
#            if switch_pin:
#                a_range = config.getfloatlist('post_gear_analog_range_%d' % gate, None, count=2)
#                if a_range is not None:
#                    a_pullup = config.getfloat('post_gear_analog_pullup_resister_%d' % gate, 4700.)
#                    s = MmuAdcSwitchSensor(config, SENSOR_GEAR_PREFIX, gate, switch_pin, event_delay, a_range, runout=True, a_pullup=a_pullup)
#                    self.sensors["%s_%d" % (SENSOR_GEAR_PREFIX, gate)] = s
#                else:
#                    self._create_mmu_sensor(config, SENSOR_GEAR_PREFIX, gate, switch_pin, event_delay, runout=True)
# --------
#
#        # Setup single extruder (entrance) sensor...
#        switch_pin = config.get('extruder_switch_pin', None)
#        if switch_pin:
#            self._create_mmu_sensor(config, SENSOR_EXTRUDER_ENTRY, None, switch_pin, event_delay, insert=True, runout=True)
#
#        # Setup single toolhead sensor...
#        switch_pin = config.get('toolhead_switch_pin', None)
#        if switch_pin:
#            self._create_mmu_sensor(config, SENSOR_TOOLHEAD, None, switch_pin, event_delay)
#
# --------
#
#        # For Qidi printers or any other that use a hall_filament_width_sensor as an endstop
#        hall_sensor_endstop = config.get('hall_sensor_endstop', None)
#        if hall_sensor_endstop is not None:
#            if hall_sensor_endstop == 'gate':
#                target_name = SENSOR_GATE
#            elif hall_sensor_endstop == 'extruder':
#                target_name = SENSOR_EXTRUDER_ENTRY
#            elif hall_sensor_endstop == 'toolhead':
#                target_name = SENSOR_TOOLHEAD
#            else:
#                target_name = hall_sensor_endstop
#
#            self.hall_pin1 = config.get('hall_adc1')
#            self.hall_pin2 = config.get('hall_adc2')
#            self.hall_dia1 = config.getfloat('hall_cal_dia1', 1.5)
#            self.hall_dia2 = config.getfloat('hall_cal_dia2', 2.0)
#            self.hall_rawdia1 = config.getint('hall_raw_dia1', 9500)
#            self.hall_rawdia2 = config.getint('hall_raw_dia2', 10500)
#            self.hall_runout_dia = config.getfloat('hall_min_diameter', 1.0)
#            # self.hall_runout_dia_max = config.getfloat('hall_max_diameter', 2.0) - Unused for trigger
#
#            s = MmuHallEndstop(config, target_name, self.hall_pin1, self.hall_pin2,
#                               self.hall_dia1, self.hall_rawdia1, self.hall_dia2, self.hall_rawdia2,
#                               hall_runout_dia=self.hall_runout_dia,
#                               insert=True, runout=True)
#            self.sensors[target_name] = s
#
# --------
#
#        # Setup motor syncing feedback sensors...
#        switch_pins = list(config.getlist('sync_feedback_tension_pin', []))
#        if switch_pins:
#            if len(switch_pins) not in [1, num_units]:
#                raise config.error("Invalid number of pins specified with sync_feedback_tension_pin. Expected 1 or %d but counted %d" % (num_units, len(switch_pins)))
#            self._create_mmu_sensor(config, SENSOR_TENSION, None, switch_pins, 0, clog=True, tangle=True, button_handler=self._sync_tension_callback)
#        switch_pins = list(config.getlist('sync_feedback_compression_pin', []))
#        if switch_pins:
#            if len(switch_pins) not in [1, num_units]:
#                raise config.error("Invalid number of pins specified with sync_feedback_compression_pin. Expected 1 or %d but counted %d" % (num_units, len(switch_pins)))
#            self._create_mmu_sensor(config, SENSOR_COMPRESSION, None, switch_pins, 0, clog=True, tangle=True, button_handler=self._sync_compression_callback)
#
#        # Setup analog (proportional) sync feedback
#        # Uses single analog input; value scaled in [-1, 1]
#        analog_pin = config.get('sync_feedback_analog_pin', None)
#        if analog_pin:
#            self.sensors[SENSOR_PROPORTIONAL] = MmuProportionalSensor(config, name=SENSOR_PROPORTIONAL)
#
# --------
