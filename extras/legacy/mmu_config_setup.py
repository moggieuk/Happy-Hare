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
        # Unfortunately not possible to rename config section because Klipper is iterating over list
        # E.g. self._rename_section(config, "tmc2209 extruder", "tmc2209 manual_extruder_stepper extruder")
        # or! config._sections['tmc2209 manual_extruder_stepper extruder'] = config._sections.pop('extruder')

        # Pull extruder stepper definition from default [extruder] and nullify original if present
        options = [ 'step_pin', 'dir_pin', 'enable_pin',
                    'rotation_distance', 'gear_ratio', 'microsteps', 'full_steps_per_rotation',
                    'pressure_advance', 'pressure_advance_smooth_time' ]

        for i in options:
            if config.fileconfig.has_option('extruder', i):
                value = config.fileconfig.get('extruder', i)
                if not config.fileconfig.has_option('manual_extruder_stepper extruder', i):
                    logging.warning("MMU Info: Automatically moved config option '%s' from '[extruder]' config section to '[manual_extruder_stepper extruder]'" % i)
                    config.fileconfig.set('manual_extruder_stepper extruder', i, value)
                elif value != config.fileconfig.get('manual_extruder_stepper extruder', i):
                    logging.warning("MMU Warning: Config option '%s' exists in both '[extruder]' and '[manual_extruder_stepper extruder]' with different values" % i)
                config.fileconfig.remove_option('extruder', i)
            elif i not in ("pressure_advance", "pressure_advance_smooth_time", "gear_ratio") and not config.fileconfig.has_option('manual_extruder_stepper extruder', i):
                raise config.error("MMU Config Error: Option '%s' is missing from '[manual_extruder_stepper extruder]' or '[extruder]' config section" % i)

        # Some klipper modules reference the stepper name in their config. Change references from 'extruder'
        # to 'manual_extruder_stepper extruder' to prevent user frustration
        for s in [('controller_fan', 'stepper', True, True), ('homing_heaters', 'steppers', False, True), ('angle', 'stepper', True, False)]:
            for i in config.fileconfig.sections():
                update = False
                if s[2] and i.split()[0] == s[0] or not s[2] and i == s[0]:
                    update = True

                if update and config.fileconfig.has_option(i, s[1]):
                    v = orig_v = config.fileconfig.get(i, s[1])
                    if s[3]: # treat as list
                        vl = [j.strip() for j in v.split(',')]
                        vl[:] = ['manual_extruder_stepper extruder' if j == 'extruder' else j for j in vl]
                        v = ", ".join(vl)
                    else:
                        v = 'manual_extruder_stepper extruder' if v == 'extruder' else v
                    config.fileconfig.set(i, s[1], v)
                    logging.warning("MMU Info: Changed config section '%s' option '%s' from: '%s' to: '%s'" % (i, s[1], orig_v, v))

    def _rename_section(self, cp, section_from, section_to):
        items = cp.fileconfig.items(section_from)
        cp.fileconfig.add_section(section_to)
        for item in items:
            cp.fileconfig.set(section_to, item[0], item[1])
        cp.fileconfig.remove_section(section_from)

def load_config(config):
    return MmuConfigSetup(config)
