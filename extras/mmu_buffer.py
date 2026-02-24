# Happy Hare MMU Software
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Easy setup of sync-feedback "buffer"
#
# sync feedback sensor(s):
#   Creates buttons handlers (with filament_switch_sensor for visibility and control) and publishes events based on state change
#   Named `sync_feedback_compression` & `sync_feedback_tension`
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time


class MmuBuffer:

    def __init__(self, config):
        from .mmu.mmu_sensor_utils import MmuSensorFactory # PAUL move me?

        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        event_delay = config.get('event_delay', 0.5)
        sf = MmuSensorFactory(self.printer)

        # Setup motor syncing feedback compression sensor for unit...
        switch_pin = config.get('sync_feedback_compression_pin', None)
        self.compression_sensor = sf.create_mmu_sensor(
            config,
            "%s_%s" % (self.name, SENSOR_COMPRESSION),
            None,
            switch_pin,
            0,
            button_handler=sf.sync_compression_callback
        )

        # Setup motor syncing feedback tension sensor for unit...
        switch_pin = config.get('sync_feedback_tension_pin', None)
        self.tension_sensor = sf.create_mmu_sensor(
            config,
            "%s_%s" % (self.name, SENSOR_TENSION),
            None,
            switch_pin,
            0,
            button_handler=sf.sync_tension_callback
        )

        # Setup analog (proportional) sync feedback
        # Uses single analog input; value scaled in [-1, 1]
        analog_pin = config.get('sync_feedback_analog_pin', None)
        if analog_pin:
            self.proportional_sensor MmuProportionalSensor(config, name=SENSOR_PROPORTIONAL)
# PAUL merge            self.sensors[SENSOR_PROPORTIONAL] = MmuProportionalSensor(config, name=SENSOR_PROPORTIONAL)
# PAUL TODO this doesn't feel correct ^^^

def load_config_prefix(config):
    return MmuBuffer(config)
