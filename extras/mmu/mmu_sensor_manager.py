# Happy Hare MMU Software
# Manager to centralize mmu_sensor operations
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
from ..mmu_sensors import MmuRunoutHelper
from .mmu_shared   import MmuError

class MmuSensorManager:
    def __init__(self, mmu):
        self.mmu = mmu
        self.all_sensors = {}

        # Assemble all possible switch sensors in desired display order
        sensor_names = []
        sensor_names.extend([self.get_gate_sensor_name(self.mmu.SENSOR_PRE_GATE_PREFIX, i) for i in range(self.mmu.num_gates)])
        sensor_names.extend([self.get_gate_sensor_name(self.mmu.SENSOR_GEAR_PREFIX, i) for i in range(self.mmu.num_gates)])
        sensor_names.extend([
            self.mmu.SENSOR_GATE, 
            self.mmu.SENSOR_TENSION,
            self.mmu.SENSOR_COMPRESSION
        ])
        for i in range(self.mmu.mmu_machine.num_units):
            sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_GATE, i))
            sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_TENSION, i))
            sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_COMPRESSION, i))
        sensor_names.extend([
            self.mmu.SENSOR_EXTRUDER_ENTRY, 
            self.mmu.SENSOR_TOOLHEAD
        ])
        for name in sensor_names:
            sensor_name = name if re.search(r'_(\d+)$', name) else "%s_sensor" % name # Must match mmu_sensors
            sensor = self.mmu.printer.lookup_object("filament_switch_sensor %s" % sensor_name, None)
            if sensor is not None and isinstance(sensor.runout_helper, MmuRunoutHelper):
                self.all_sensors[name] = sensor

        # Special case for "no bowden" (one unit) designs where mmu_gate is an alias for extruder sensor
        if not self.mmu.mmu_machine.require_bowden_move and self.sensors.get(self.mmu.SENSOR_EXTRUDER_ENTRY, None) and self.mmu.SENSOR_GATE not in self.sensors:
            self.all_sensors[self.mmu.SENSOR_GATE] = self.sensors[self.mmu.SENSOR_EXTRUDER_ENTRY]

        # Setup subset of filament sensors that are also used for homing (endstops)
        self.endstop_names = []
        self.endstop_names.extend([self.get_gate_sensor_name(self.mmu.SENSOR_GEAR_PREFIX, i) for i in range(self.mmu.num_gates)])
        self.endstop_names.extend([
            self.mmu.SENSOR_GATE, 
            self.mmu.SENSOR_COMPRESSION
        ])
        for i in range(self.mmu.mmu_machine.num_units):
            self.endstop_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_GATE, i))
            self.endstop_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_COMPRESSION, i))
        self.endstop_names.extend([
            self.mmu.SENSOR_EXTRUDER_ENTRY, 
            self.mmu.SENSOR_TOOLHEAD
        ])
        for name in self.endstop_names:
            sensor_name = name if re.search(r'_(\d+)$', name) else "%s_sensor" % name # Must match mmu_sensors
            sensor = self.mmu.printer.lookup_object("filament_switch_sensor %s" % sensor_name, None)
            if sensor is not None and isinstance(sensor.runout_helper, MmuRunoutHelper):
                # Add sensor pin as an extra endstop for gear rail
                sensor_pin = sensor.runout_helper.switch_pin
                ppins = self.mmu.printer.lookup_object('pins')
                pin_params = ppins.parse_pin(sensor_pin, True, True)
                share_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])
                ppins.allow_multi_use_pin(share_name)
                mcu_endstop = self.mmu.gear_rail.add_extra_endstop(sensor_pin, name)

                # This ensures rapid stopping of extruder stepper when endstop is hit on synced homing
                # otherwise the extruder can continue to move a small (speed dependent) distance
                if self.mmu.homing_extruder and name == self.mmu.SENSOR_TOOLHEAD:
                    mcu_endstop.add_stepper(self.mmu.mmu_extruder_stepper.stepper)
            else:
                logging.warning("MMU: Improper setup: Filament sensor %s is not defined in [mmu_sensors]" % name)

    # Activate only sensors for current unit and rename for access
    def set_active_unit(self, unit):
        self.sensors = {}
        for name, sensor in self.all_sensors.items():
            if name.startswith("unit_"):
                if unit != self.mmu.UNIT_UNKNOWN and name.startswith("unit_" + str(unit)):
                    self.sensors[re.sub(r'unit_\d+_', '', name)] = sensor
                    sensor.runout_helper.enable_button_feedback(True)
                else:
                    # Ensure any excluded sensor is completely deactivated
                    sensor.runout_helper.enable_runout(False)
                    sensor.runout_helper.enable_button_feedback(False)
            else:
                self.sensors[name] = sensor

    # Return dict of all sensor states (or None if sensor disabled)
    def get_all_sensors(self, inactive=False):
        result = {}
        for name, sensor in self.sensors.items() if not inactive else self.all_sensors.items():
            result[name] = bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None
        return result

    def has_sensor(self, name):
        return self.sensors[name].runout_helper.sensor_enabled if name in self.sensors else False

    def has_gate_sensor(self, name, gate):
        return self.sensors[self.get_gate_sensor_name(name, gate)].runout_helper.sensor_enabled if self.get_gate_sensor_name(name, gate) in self.sensors else False

    def get_gate_sensor_name(self, name, gate):
        return "%s_%d" % (name, gate) # Must match mmu_sensors

    def get_unit_sensor_name(self, name, unit):
        return "unit_%d_%s" % (unit, name) # Must match mmu_sensors

    # Get unit or gate specific endstop if it exists
    # Take generic name and look for "<unit>_genericName" and "genericName_<gate>"
    def get_mapped_endstop_name(self, endstop_name):
        mapped_name = self.get_unit_sensor_name(endstop_name, self.mmu.unit_selected)
        if mapped_name in self.endstop_names:
            return mapped_name

        mapped_name = self.get_gate_sensor_name(endstop_name, self.mmu.gate_selected)
        if mapped_name in self.endstop_names:
            return mapped_name

        return endstop_name

    # Return sensor state or None if not installed
    def check_sensor(self, name):
        sensor = self.sensors.get(name, None)
        if sensor is not None and sensor.runout_helper.sensor_enabled:
            detected = bool(sensor.runout_helper.filament_present)
            self.mmu.log_trace("(%s sensor %s filament)" % (name, "detects" if detected else "does not detect"))
            return detected
        else:
            return None

    # Return per-gate sensor state or None if not installed
    def check_gate_sensor(self, name, gate):
        sensor_name = self.get_gate_sensor_name(name, gate)
        sensor = self.sensors.get(sensor_name, None)
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
        for name, state in self.get_all_sensors(inactive=True).items():
            if state is not None or detail:
                sensor = self.all_sensors.get(name)
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
        for name, sensor in self.sensors.items():
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
                sensor = self.sensors.get(name, None)
                if sensor and position_condition(pos, position_check):
                    result[name] = bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None
            self.mmu.log_debug("Sensors: %s" % result)
        return result

    def _get_sensors_before(self, pos, gate, loading=True):
        return self._get_sensors(pos, gate, lambda p, pc: pc is None or (loading and p >= pc) or (not loading and p > pc))

    def _get_sensors_after(self, pos, gate, loading=True):
        return self._get_sensors(pos, gate, lambda p, pc: pc is not None and ((loading and p < pc) or (not loading and p <= pc)))

    def _get_all_sensors_for_gate(self,  gate):
        return self._get_sensors(-1, gate, lambda p, pc: pc is not None)
