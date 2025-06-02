# Happy Hare MMU Software
#
# Easy setup of sync-feedback "buffer"
#
# sync feedback sensor(s):
#   Creates buttons handlers (with filament_switch_sensor for visibility and control) and publishes events based on state change
#   Named `sync_feedback_compression` & `sync_feedback_tension`
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
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
        from .mmu                  import Mmu # For sensor names
        from .mmu.mmu_sensor_utils import MmuSensorFactory

        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        event_delay = config.get('event_delay', 0.5)
        sf = MmuSensorFactory(self.printer)

        # Setup motor syncing feedback sensors for unit...
        switch_pin = config.get('sync_feedback_compression_pin', None)
        self.compression_sensor = sf.create_mmu_sensor(
            config,
            "%s_%s" % (Mmu.SENSOR_COMPRESSION, self.name),
            None,
            switch_pin,
            0,
            button_handler=sf.sync_compression_callback
        )

        switch_pin = config.get('sync_feedback_tension_pin', None)
        self.tension_sensor = sf.create_mmu_sensor(
            config,
            "%s_%s" % (Mmu.SENSOR_TENSION, self.name),
            None,
            switch_pin,
            0,
            button_handler=sf.sync_tension_callback
        )

def load_config_prefix(config):
    return MmuBuffer(config)
