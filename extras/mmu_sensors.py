# Happy Hare MMU Software
#
# Easy setup of all sensors for mmu_unit
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
# mmu_gate sensor(s):
#   Wrapper around `filament_switch_sensor` setting up insert/runout callbacks with modified runout event handling
#   Named `mmu_gate`
#
# sync feedback sensor(s):
#   Creates buttons handlers (with filament_switch_sensor for visibility and control) and publishes events based on state change
#   Named `sync_feedback_compression` & `sync_feedback_tension`
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# RunoutHelper based on:
# Generic Filament Sensor Module Copyright (C) 2019  Eric Callahan <arksine.code@gmail.com>
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time

# Happy Hare imports
from .mmu.mmu_sensor_utils import MmuSensorFactory

class MmuSensors:

    def __init__(self, config):
        from .mmu import Mmu # For sensor names

        self.printer = config.get_printer()
        event_delay = config.get('event_delay', 0.5)
        sf = MmuSensorFactory(self.printer)

        # Setup "mmu_pre_gate" sensors...
        self.pre_gate_sensors = {}
        for gate in range(23):
            switch_pin = config.get('pre_gate_switch_pin_%d' % gate, None)
            if switch_pin:
                self.pre_gate_sensors[gate] = sf.create_mmu_sensor(config, Mmu.SENSOR_PRE_GATE_PREFIX, gate, switch_pin, event_delay, insert=True, remove=True, runout=True, insert_remove_in_print=True)

        # Setup single "mmu_gate" sensor...
        switch_pin = config.get('gate_switch_pin', None)
        self.gate_sensor = sf.create_mmu_sensor(config, Mmu.SENSOR_GATE, None, switch_pin, event_delay, runout=True)

        # Setup "mmu_gear" sensors...
        self.post_gear_sensors = {}
        for gate in range(23):
            switch_pin = config.get('post_gear_switch_pin_%d' % gate, None)
            if switch_pin:
                self.post_gear_sensors[gate] = sf.create_mmu_sensor(config, Mmu.SENSOR_GEAR_PREFIX, gate, switch_pin, event_delay, runout=True)

        # Setup motor syncing feedback sensors...
        switch_pin = config.get('sync_feedback_compression_pin', None)
        self.compression_sensor = sf.create_mmu_sensor(config, Mmu.SENSOR_COMPRESSION, None, switch_pin, 0, button_handler=sf.sync_compression_callback)
        switch_pin = config.get('sync_feedback_tension_pin', None)
        self.tension_sensor = sf.create_mmu_sensor(config, Mmu.SENSOR_TENSION, None, switch_pin, 0, button_handler=sf.sync_tension_callback)

def load_config_prefix(config):
    return MmuSensors(config)
