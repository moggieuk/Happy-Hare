# Happy Hare MMU Software
# Very simple module to allow for early definition of MMU hardware like endstops
#
# Copyright (C) 2023  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

class MmuHardware:
    def __init__(self, config):
        self.selector_homing_pin = config.get('selector_homing_pin', None)
        self.gear_parked_homing_pin = config.get('gear_parked_homing_pin', None)
        self.gear_extruder_homing_pin = config.get('gear_extruder_homing_pin', None)
        self.gear_toolhead_homing_pin = config.get('gear_toolhead_homing_pin', None)

    def get_selector_homing_pin(self):
        return self.selector_homing_pin

    def get_gear_parked_homing_pin(self):
        return self.gear_parked_homing_pin

    def get_gear_extruder_homing_pin(self):
        return self.gear_extruder_homing_pin

    def get_gear_toolhead_homing_pin(self):
        return self.gear_toolhead_homing_pin

def load_config(config):
    return MmuHardware(config)

