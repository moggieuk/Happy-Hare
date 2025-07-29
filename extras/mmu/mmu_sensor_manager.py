# Happy Hare MMU Software
#
# Manager to centralize mmu_sensor operations and utilities
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager to centralize mmu_sensor operations
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import random, logging, math, re

# Happy Hare imports
from .mmu_sensor_utils import MmuRunoutHelper
from .mmu_shared       import *

class MmuSensorManager:
    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu_machine = mmu.mmu_machine

        # Determine sensor maps now from every perspective: all, per-unit and per-gate. Note that keys are the simplest
        # form to disambiguate with unit_sensors dropping unit prefix and gate_sensors dropping gate suffix
        self.all_sensors_map = {}    # Map of all sensors on mmu_machine with fully qualified names
        self.unit_sensors = []       # Sensors on each mmu_unit without unit prefix (indexed by unit index)
        self.gate_sensors = []       # Sensors on each gate with names stripped of gate and unit prefix/suffix (indexed by gate index)
        self.bypass_sensor_map = {}  # Map of sensors when bypass is selected (extruder and toolhead only)
        self.active_sensors_map = {} # Points to current version of gate_sensors (simple names). Resets on gate change

        def collect_sensors(pairs):
            return {key: sensor for sensor, key in pairs if sensor}

        common_sensors = collect_sensors([
            (self.mmu_machine.extruder_sensor, self.mmu.SENSOR_EXTRUDER_ENTRY),
            (self.mmu_machine.toolhead_sensor, self.mmu.SENSOR_TOOLHEAD),
        ])

        for mmu_unit in self.mmu_machine.units:
            unit_sensors = collect_sensors([
                (mmu_unit.sensors.gate_sensor, self.mmu.SENSOR_GATE),
                (mmu_unit.buffer and mmu_unit.buffer.compression_sensor, self.mmu.SENSOR_COMPRESSION),
                (mmu_unit.buffer and mmu_unit.buffer.tension_sensor, self.mmu.SENSOR_TENSION),
            ])
            qualified_unit_sensors = collect_sensors([
                (mmu_unit.sensors.gate_sensor, self.get_unit_sensor_name(self.mmu.SENSOR_GATE, mmu_unit.unit_index)),
                (mmu_unit.buffer and mmu_unit.buffer.compression_sensor, self.get_unit_sensor_name(self.mmu.SENSOR_COMPRESSION, mmu_unit.unit_index)),
                (mmu_unit.buffer and mmu_unit.buffer.tension_sensor, self.get_unit_sensor_name(self.mmu.SENSOR_TENSION, mmu_unit.unit_index)),
            ])
            self.all_sensors_map.update(qualified_unit_sensors)

            for gate in range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates):
                self.gate_sensors.append(collect_sensors([
                    (mmu_unit.sensors.pre_gate_sensors.get(gate), self.mmu.SENSOR_PRE_GATE_PREFIX),
                    (mmu_unit.sensors.post_gear_sensors.get(gate), self.mmu.SENSOR_GEAR_PREFIX),
                    (mmu_unit.sensors.gate_sensor, self.mmu.SENSOR_GATE),
                    (mmu_unit.buffer and mmu_unit.buffer.compression_sensor, self.mmu.SENSOR_COMPRESSION),
                    (mmu_unit.buffer and mmu_unit.buffer.tension_sensor, self.mmu.SENSOR_TENSION),
                    (self.mmu_machine.extruder_sensor, self.mmu.SENSOR_EXTRUDER_ENTRY),
                    (self.mmu_machine.toolhead_sensor, self.mmu.SENSOR_TOOLHEAD),
                ]))
                qualified_gate_sensors = collect_sensors([
                    (mmu_unit.sensors.pre_gate_sensors.get(gate), self.get_gate_sensor_name(self.mmu.SENSOR_PRE_GATE_PREFIX, gate)),
                    (mmu_unit.sensors.post_gear_sensors.get(gate), self.get_gate_sensor_name(self.mmu.SENSOR_GEAR_PREFIX, gate)),
                ])
                unit_sensors.update(qualified_gate_sensors)
                self.all_sensors_map.update(qualified_gate_sensors)

            unit_sensors.update(common_sensors)
            self.unit_sensors.append(unit_sensors)

        self.all_sensors_map.update(common_sensors)
        self.bypass_sensors_map = common_sensors

# PAUL.. testing how did we do?
#        logging.info("PAUL: all_sensors_map=%s\n" % self.all_sensors_map.keys())
#        for unit in self.mmu_machine.units:
#            logging.info("PAUL: unit_sensors[%d]=%s\n" % (unit.unit_index, self.unit_sensors[unit.unit_index].keys()))
#        for gate in range(self.mmu_machine.num_gates):
#            logging.info("PAUL: gate_sensors[%d]=%s\n" % (gate, self.gate_sensors[gate].keys()))
# PAUL ^^^

        # Setup filament sensors as homing (endstops) on respective mmu_unit
        for i, sensors in enumerate(self.unit_sensors):
            unit = self.mmu_machine.get_mmu_unit_by_index(i)
            gear_rail = unit.mmu_toolhead.get_kinematics().rails[1]
            for name, sensor in self.unit_sensors[i].items():
                if not name.startswith(self.mmu.SENSOR_PRE_GATE_PREFIX):
# PAUL                    logging.info("PAUL: creating endstop for unit=%d, sensor.name=%s" % (i, sensor.runout_helper.name))
                    sensor_pin = sensor.runout_helper.switch_pin
                    ppins = self.mmu.printer.lookup_object('pins')
                    pin_params = ppins.parse_pin(sensor_pin, True, True)
                    share_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])
                    ppins.allow_multi_use_pin(share_name) # can this be called more than once?
                    if name not in gear_rail.get_extra_endstop_names():
                        mcu_endstop = gear_rail.add_extra_endstop(sensor_pin, name) # paul results in shared gate, compression and tension endtop names!

                        # This ensures rapid stopping of extruder stepper when endstop is hit on synced homing
                        # otherwise the extruder can continue to move a small (speed dependent) distance
                        if self.mmu_machine.homing_extruder and name == self.mmu.SENSOR_TOOLHEAD:
# PAUL                            logging.info("PAUL: adding endstop to mmu_extruder")
                            mcu_endstop.add_stepper(self.mmu_machine.mmu_extruder_stepper.stepper)

        # Register commands
        self.mmu.gcode.register_command('MMU_SENSORS', self.cmd_MMU_SENSORS, desc = self.cmd_MMU_SENSORS_help)

    cmd_MMU_SENSORS_help = "Query state of sensors fitted to mmu"
    cmd_MMU_SENSORS_param_help = (
        "MMU_SENSORS: %s\n" % cmd_MMU_SENSORS_help
        + "UNIT   = #(int)\n"
        + "DETAIL = [0|1]"
    )
    def cmd_MMU_SENSORS(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        unit = gcmd.get_int('UNIT', None, minval=0, maxval=self.mmu_machine.num_units - 1)
        help = bool(gcmd.get_int('HELP', 0, minval=0, maxval=1))
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))

        if help:
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_SENSORS_param_help), color=True)
            return

        msg = ""
        sensors = self.get_active_sensors(all_sensors=True) if unit is None else self.get_unit_sensors(unit)
        if all(v is None for v in sensors.values()) and not detail:
            msg += "No active sensors. Use DETAIL=1 to see all"
        else:
            for name in sorted(sensors):
                state = sensors[name]
                if state is not None or detail:
                    sensor = self.all_sensors_map.get(name) if unit is None else self.unit_sensors[unit].get(name)
                    trig = "%s" % 'TRIGGERED' if sensor.runout_helper.filament_present else 'Open'
                    msg += "%s: %s" % (name, ("(%s, currently disabled)" % trig) if state is None else trig)
                    if detail and sensor.runout_helper.runout_suspended is not None and state is not None:
                        msg += "%s" % (", Runout enabled" if not sensor.runout_helper.runout_suspended else "")
                    msg += "\n"
        self.mmu.log_always(msg)

    # Return dict of all sensor states for just active or all sensors (returns None if sensor disabled)
    def get_active_sensors(self, all_sensors=False):
#        logging.info("PAUL: active_sensors_map=%s", self.active_sensors_map)
        sensor_map = self.all_sensors_map if all_sensors else self.active_sensors_map
        return {
            sname: (bool(sensor.runout_helper.filament_present) 
                    if sensor.runout_helper.sensor_enabled else None)
            for sname, sensor in sensor_map.items()
        }

    def get_unit_sensors(self, unit):
        sensor_map = self.unit_sensors[unit]
        return {
            sname: (bool(sensor.runout_helper.filament_present) 
                    if sensor.runout_helper.sensor_enabled else None)
            for sname, sensor in sensor_map.items()
        }

    # Reset the relevent sensor list based on current gate
    # handling bypass and unknown
    def reset_active_gate(self, gate):
        self.active_sensors_map = self.gate_sensors[gate] if self.mmu.gate_selected > 0 else self.bypass_sensors_map

    # Activate only sensors for current unit
    def reset_active_unit(self, unit):
        # We do this in two steps to allow sensor sharing
        # First ensure any excluded sensor is completely deactivated
        for sname, sensor in self.all_sensors_map.items():
            if re.match(r'^unit\d+_', sname) and not sname.startswith("unit%d_" % unit):
                sensor.runout_helper.enable_runout(False)
                sensor.runout_helper.enable_button_feedback(False)

        # Activate just this unit sensors
        for sname, sensor in self.all_sensors_map.items():
            if sname.startswith("unit%d_" % unit):
                sensor.runout_helper.enable_button_feedback(True)

    def has_sensor(self, sname):
        if sname in self.active_sensors_map:
            return self.active_sensors_map[sname].runout_helper.sensor_enabled
        else:
            return False

    # Note this looks at sensors on non-active gate
    def has_gate_sensor(self, sname, gate):
        sensor_key = self.get_gate_sensor_name(sname, gate)
        if sensor_key in self.all_sensors_map:
            return self.all_sensors_map[sensor_key].runout_helper.sensor_enabled
        else:
            return False

    def get_gate_sensor_name(self, sname, gate):
        return "%s_%d" % (sname, gate)

    def get_unit_sensor_name(self, sname, unit):
        return "unit%d_%s" % (unit, sname)

    # Get unit or gate specific endstop if it exists
    # Take generic name and look for "<unit>_genericName" and "genericName_<gate>"
    def get_mapped_endstop_name(self, endstop_name):
        if endstop_name in [self.mmu.SENSOR_GATE, self.mmu.SENSOR_COMPRESSION, self.mmu.SENSOR_TENSION]:
            return self.get_unit_sensor_name(endstop_name, mmu.unit_selected)

        if endstop_name in [self.mmu.SENSOR_PRE_GATE_PREFIX, self.mmu.SENSOR_GEAR_PREFIX, self.mmu.SENSOR_GEAR_TOUCH]: # PAUL TODO verify SENSOR_GEAR_TOUCH operation for calibration
            return self.get_gate_sensor_name(endstop_name, mmu.gate_selected)

        return endstop_name

    # Return sensor state or None if not installed
    def check_sensor(self, name):
        sensor = self.active_sensors_map.get(name, None)
        if sensor is not None and sensor.runout_helper.sensor_enabled:
            detected = bool(sensor.runout_helper.filament_present)
            self.mmu.log_trace("(%s sensor %s filament)" % (name, "detects" if detected else "does not detect"))
            return detected
        else:
            return None

    # Return per-gate sensor state or None if not installed
    def check_gate_sensor(self, name, gate):
        sensor_name = self.get_gate_sensor_name(name, gate)
        sensor = self.all_sensors_map.get(sensor_name, None)
        if sensor is not None and sensor.runout_helper.sensor_enabled:
            detected = bool(sensor.runout_helper.filament_present)
            self.mmu.log_trace("(%s sensor %s filament)" % (sensor_name, "detects" if detected else "does not detect"))
            return detected
        else:
            return None

    # Returns True if ALL sensors before position detect filament
    #         None if NO sensors available (disambiguate from non-triggered sensor)
    # Can be used as a "filament continuity test"
    def check_all_sensors_before(self, pos, gate, loading=True):
        sensors = self._get_sensors_before(pos, gate, loading)
        if all(state is None for state in sensors.values()):
            return None
        return all(state is not False for state in sensors.values())

    # Returns True if ANY sensor before position detects filament
    #         None if NO sensors available (disambiguate from non-triggered sensor)
    # Can be used as a filament visibility test over a portion of the travel
    def check_any_sensors_before(self, pos, gate, loading=True):
        sensors = self._get_sensors_before(pos, gate, loading)
        if all(state is None for state in sensors.values()):
            return None
        return any(state is True for state in sensors.values())

    # Returns True if ALL sensors after position detect filament
    #         None if NO sensors available (disambiguate from non-triggered sensor)
    # Can be used as a "filament continuity test"
    def check_all_sensors_after(self, pos, gate, loading=True):
        sensors = self._get_sensors_after(pos, gate, loading)
        if all(state is None for state in sensors.values()):
            return None
        return all(state is not False for state in sensors.values())

    # Returns True if ANY sensor after position detects filament
    #         None if no sensors available (disambiguate from non-triggered sensor)
    # Can be used to validate position
    def check_any_sensors_after(self, pos, gate, loading=True):
        sensors = self._get_sensors_after(pos, gate, loading)
        if all(state is None for state in sensors.values()):
            return None
        return any(state is True for state in sensors.values())

    # Returns True is any sensors in current filament path are triggered (EXCLUDES pre-gate)
    #         None if no sensors available (disambiguate from non-triggered sensor)
    def check_any_sensors_in_path(self):
        sensors = self._get_all_sensors_for_gate(self.mmu.gate_selected)
        if all(state is None for state in sensors.values()):
            return None
        return any(state is True for state in sensors.values())

    # Returns True is any sensors in filament path are not triggered
    #         None if no sensors available (disambiguate from non-triggered sensor)
    # Can be used to spot failure in "continuity" i.e. runout
    def check_for_runout(self):
        sensors = self._get_sensors_before(self.mmu.FILAMENT_POS_LOADED, self.mmu.gate_selected)
        if all(state is None for state in sensors.values()):
            return None
        return any(state is False for state in sensors.values())

    # Error with explanation if any filament sensors don't detect filament
    def confirm_loaded(self):
        sensors = self._get_sensors_before(self.mmu.FILAMENT_POS_LOADED, self.mmu.gate_selected)
        if any(state is False for state in sensors.values()):
            MmuError("Loaded check failed:\nFilament not detected by sensors: %s" % ', '.join([name for name, state in sensors.items() if state is False]))

    def enable_runout(self, gate):
        logging.info("PAUL: enable_runout(gate=%d)" % gate)
        self._set_sensor_runout(True, gate)

    def disable_runout(self, gate):
        logging.info("PAUL: disable_runout(gate=%d)" % gate)
        self._set_sensor_runout(False, gate)

    def _set_sensor_runout(self, enable, gate):
        for name, sensor in self.active_sensors_map.items():
            sensor.runout_helper.enable_runout(enable and gate >= 0)

    # Defines sensors and relationship to filament_pos state for easy filament tracing
    def _get_sensors(self, pos, gate, position_condition):
        result = {}
        if gate >= 0:
            sensor_selection = [
                (self.mmu.SENSOR_PRE_GATE_PREFIX, None),
                (self.mmu.SENSOR_GEAR_PREFIX, self.mmu.FILAMENT_POS_HOMED_GATE if self.mmu.gate_homing_endstop == self.mmu.SENSOR_GEAR_PREFIX else None),
                (self.mmu.SENSOR_GATE, self.mmu.FILAMENT_POS_HOMED_GATE),
                (self.mmu.SENSOR_EXTRUDER_ENTRY, self.mmu.FILAMENT_POS_HOMED_ENTRY),
                (self.mmu.SENSOR_TOOLHEAD, self.mmu.FILAMENT_POS_HOMED_TS),
            ]
            for name, position_check in sensor_selection:
                sensor = self.active_sensors_map.get(name, None)
                if sensor and position_condition(pos, position_check):
                    result[name] = bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None
        return result

    def _get_sensors_before(self, pos, gate, loading=True):
        return self._get_sensors(pos, gate, lambda p, pc: pc is None or (loading and p >= pc) or (not loading and p > pc))

    def _get_sensors_after(self, pos, gate, loading=True):
        return self._get_sensors(pos, gate, lambda p, pc: pc is not None and ((loading and p < pc) or (not loading and p <= pc)))

    def _get_all_sensors_for_gate(self,  gate):
        return self._get_sensors(-1, gate, lambda p, pc: pc is not None)

    def get_status(self):
        return {
            name: bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None
            for name, sensor in self.active_sensors_map.items()
        }
