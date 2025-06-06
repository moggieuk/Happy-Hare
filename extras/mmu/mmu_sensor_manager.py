# Happy Hare MMU Software
#
# Manager to centralize mmu_sensor operations and utilities
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
import random, logging, math, re

# Happy Hare imports
from .mmu_sensor_utils import MmuRunoutHelper
from .mmu_shared       import MmuError

class MmuSensorManager:
    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu_machine = mmu.mmu_machine

        # Determine sensor maps now from every perspective: all, per-unit and per-gate
        self.gate_sensors = []       # Sensors on each gate with names stripped of gate and unit prefix/suffix (indexed by gate index)
        self.unit_sensors = []       # Sensors on each mmu_unit with fully qualified names (indexed by unit index)
        self.all_sensor_map = {}     # Map of all sensors on mmu_machine with fully qualified names
        self.bypass_sensor_map = {}  # Map of sensors when bypass is selected (extruder and toolhead only)
        self.active_sensors_map = {} # Map of currently visible sensors for MMU operation. Resets on gate change

        def collect_sensors(pairs):
            return {key: sensor for sensor, key in pairs if sensor}

        common_sensors = collect_sensors([
            (self.mmu_machine.extruder_sensor, self.mmu.SENSOR_EXTRUDER_ENTRY),
            (self.mmu_machine.toolhead_sensor, self.mmu.SENSOR_TOOLHEAD),
        ])
        for unit in self.mmu_machine.units:
            unit_sensors = collect_sensors([
                (unit.sensors.gate_sensor, self.get_unit_sensor_name(self.mmu.SENSOR_GATE, unit.unit_index)),
                (unit.buffer and unit.buffer.compression_sensor, self.get_unit_sensor_name(self.mmu.SENSOR_COMPRESSION, unit.unit_index)),
                (unit.buffer and unit.buffer.tension_sensor, self.get_unit_sensor_name(self.mmu.SENSOR_TENSION, unit.unit_index)),
            ])

            for gate in range(unit.first_gate, unit.first_gate + unit.num_gates):
                self.gate_sensors.append(collect_sensors([
                    (unit.sensors.pre_gate_sensors.get(gate), self.mmu.SENSOR_PRE_GATE_PREFIX),
                    (unit.sensors.post_gear_sensors.get(gate), self.mmu.SENSOR_GEAR_PREFIX),
                    (unit.sensors.gate_sensor, self.mmu.SENSOR_GATE),
                    (unit.buffer and unit.buffer.compression_sensor, self.mmu.SENSOR_COMPRESSION),
                    (unit.buffer and unit.buffer.tension_sensor, self.mmu.SENSOR_TENSION),
                    (self.mmu_machine.extruder_sensor, self.mmu.SENSOR_EXTRUDER_ENTRY),
                    (self.mmu_machine.toolhead_sensor, self.mmu.SENSOR_TOOLHEAD),
                ]))

                named_gate_sensors = collect_sensors([
                    (unit.sensors.pre_gate_sensors.get(gate), self.get_gate_sensor_name(self.mmu.SENSOR_PRE_GATE_PREFIX, gate)),
                    (unit.sensors.post_gear_sensors.get(gate), self.get_gate_sensor_name(self.mmu.SENSOR_GEAR_PREFIX, gate)),
                ])

                unit_sensors.update(named_gate_sensors)

            self.all_sensor_map.update(unit_sensors)
            unit_sensors.update(common_sensors)
            self.unit_sensors.append(unit_sensors)

        self.all_sensor_map.update(common_sensors)
        self.bypass_sensors_map = common_sensors

        # PAUL.. how did we do?
        logging.info("PAUL: all_sensor_map=%s\n" % self.all_sensor_map.keys())
        for unit in self.mmu_machine.units:
            logging.info("PAUL: unit_sensors[%d]=%s\n" % (unit.unit_index, self.unit_sensors[unit.unit_index].keys()))
        for gate in range(self.mmu_machine.num_gates):
            logging.info("PAUL: gate_sensors[%d]=%s\n" % (gate, self.gate_sensors[gate].keys()))
        # PAUL ^^^

        # Setup filament sensors as homing (endstops) on respective mmu_unit
        for i, sensors in enumerate(self.unit_sensors):
            unit = self.mmu_machine.get_mmu_unit_by_index(i)
            gear_rail = unit.mmu_toolhead.get_kinematics().rails[1]
            for name, sensor in self.unit_sensors[i].items():
                if not name.startswith(self.mmu.SENSOR_PRE_GATE_PREFIX):
                    logging.info("PAUL: unit=%d, sensor.name=%s" % (i, sensor.runout_helper.name))
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
                            logging.info("PAUL: adding mmu_extruder")
                            mcu_endstop.add_stepper(self.mmu_machine.mmu_extruder_stepper.stepper)


    # Reset the relevent sensor list based on current gate
    # handling bypass and unknown
    def reset_active_gate(self, gate):
        self.active_sensors_map = self.gate_sensors[gate] if self.mmu.gate_selected > 0 else self.bypass_sensors_map

    # Activate only sensors for current unit
    def reset_active_unit(self, unit):
        # We do this in two steps to allow sensor sharing
        # First esure any excluded sensor is completely deactivated
        for sname, sensor in self.all_sensor_map.items():
            if unit == self.mmu.UNIT_UNKNOWN or (re.match(r'^unit\d+_', sname) and not sname.startswith("unit%d_" % unit)):
                sensor.runout_helper.enable_runout(False) # PAUL how does runout get re-enabled?
                sensor.runout_helper.enable_button_feedback(False)

        # Activate just this unit sensors
        if unit != self.mmu.UNIT_UNKNOWN:
            for sname, sensor in self.unit_sensors[unit].items():
                if sname.startswith("unit%d_" % unit):
                    sensor.runout_helper.enable_button_feedback(True)

    # Return dict of all sensor states (or None if sensor disabled)
    def get_all_sensors(self, inactive=False):
        result = {}
        for sname, sensor in self.active_sensors_map.items() if not inactive else self.all_sensor_map.items():
            result[sname] = bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None
        return result

    def has_sensor(self, sname):
        if sname in self.active_sensors_map:
            return self.active_sensors_map[sname].runout_helper.sensor_enabled
        else:
            return False

    def has_gate_sensor(self, sname, gate):
        sensor_key = self.get_gate_sensor_name(sname, gate)
        if sensor_key in self.active_sensors_map:
            return self.active_sensors_map[sensor_key].runout_helper.sensor_enabled
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

        if endstop_name in [self.mmu.SENSOR_GEAR_PREFIX]:
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
        sensor = self.active_sensors_map.get(sensor_name, None)
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

    # Return formatted summary of all sensors under management (include all mmu units)
    def get_sensor_summary(self, detail=False):
        summary = ""
        all_sensors = self.get_all_sensors(inactive=True)
        for name in sorted(all_sensors):
            state = all_sensors[name]
            if state is not None or detail:
                sensor = self.all_sensor_map.get(name)
                trig = "%s" % 'TRIGGERED' if sensor.runout_helper.filament_present else 'Open'
                summary += "%s: %s" % (name, ("(%s, currently disabled)" % trig) if state is None else trig)
                if detail and sensor.runout_helper.runout_suspended is not None and state is not None:
                    summary += "%s" % (", Runout enabled" if not sensor.runout_helper.runout_suspended else "")
                summary += "\n"
        return summary

    def enable_runout(self, gate):
        self._set_sensor_runout(True, gate)

    def disable_runout(self, gate):
        self._set_sensor_runout(False, gate)

    def _set_sensor_runout(self, enable, gate):
        for name, sensor in self.active_sensors_map.items():
            if isinstance(sensor.runout_helper, MmuRunoutHelper):
                per_gate = re.search(r'_(\d+)$', name) # Must match mmu_sensors
                if per_gate:
                    sensor.runout_helper.enable_runout(enable and (int(per_gate.group(1)) == gate))
                else:
                    sensor.runout_helper.enable_runout(enable and (gate != self.mmu.TOOL_GATE_UNKNOWN))

    # Defines sensors and relationship to filament_pos state for easy filament tracing
    def _get_sensors(self, pos, gate, position_condition):
        result = {}
        if gate >= 0:
            sensor_selection = [
                (self.get_gate_sensor_name(self.mmu.SENSOR_PRE_GATE_PREFIX, gate), None),
                (self.get_gate_sensor_name(self.mmu.SENSOR_GEAR_PREFIX, gate), self.mmu.FILAMENT_POS_HOMED_GATE if self.mmu.gate_homing_endstop == self.mmu.SENSOR_GEAR_PREFIX else None),
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
