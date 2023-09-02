# Happy Hare MMU Software
# Optional runtime manipulation and validation of klipper config to ease Happy Hare configuration
#
# Copyright (C) 2023  moggieuk#6538 (discord) moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import chelper, logging


class MmuConfigSetup():
    """Optional runtime manipulation and validation of klipper config to ease Happy Hare configuration"""

    def __init__(self, config):
        # Validate that user has removed all stepper parameters from extruder section
        options = [ 'step_pin', 'dir_pin', 'enable_pin',
                    'rotation_distance', 'gear_ratio', 'microsteps', 'full_steps_per_rotation',
                    'pressure_advance', 'pressure_advance_smooth_time' ]
        for i in options:
            if config.fileconfig.has_option('extruder', i):
                value = config.fileconfig.get('extruder', i)
                if not config.fileconfig.has_option('manual_extruder_stepper extruder', i):
                    logging.error("MMU Info: Automatically moved config option '%s' from '[extruder]' config section to '[manual_extruder_stepper extruder]'" % i)
                    config.fileconfig.set('manual_extruder_stepper extruder', i, value)
                elif value != config.fileconfig.get('manual_extruder_stepper extruder', i):
                    logging.error("MMU Warning: Config option '%s' exists in both '[extruder]' and '[manual_extruder_stepper extruder]' with different values" % i)
                config.fileconfig.remove_option('extruder', i)
            elif not i.startswith("pressure_") and not config.fileconfig.has_option('manual_extruder_stepper extruder', i):
                raise config.error("MMU Config Error: Option '%s' is missing from '[manual_extruder_stepper extruder]' or '[extruder]' config section" % i)

def load_config(config):
    return MmuConfigSetup(config)
