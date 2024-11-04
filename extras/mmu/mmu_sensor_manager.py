# Happy Hare MMU Software
# Manager to centralize mmu_sensor operations
#
# Copyright (C) 2024  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import random, logging, math, re

# Happy Hare imports
from extras.mmu_sensors  import MmuRunoutHelper
from extras.mmu.mmu_shared import MmuError

class MmuSensorManager:
    def __init__(self, mmu):
        self.mmu = mmu
        self.sensors = {}

        for name in (
            [self.mmu.ENDSTOP_TOOLHEAD, self.mmu.ENDSTOP_GATE, self.mmu.ENDSTOP_EXTRUDER_ENTRY] +
            [self.get_gate_sensor_name(self.mmu.PRE_GATE_SENSOR_PREFIX, i) for i in range(self.mmu.num_gates)] +
            [self.get_gate_sensor_name(self.mmu.ENDSTOP_GEAR_PREFIX, i) for i in range(self.mmu.num_gates)]
        ):
            sensor_name = name if re.search(r"_[0-9]+$", name) else "%s_sensor" % name
            sensor = self.mmu.printer.lookup_object("filament_switch_sensor %s" % sensor_name, None)
            if sensor is not None and isinstance(sensor.runout_helper, MmuRunoutHelper):
                self.sensors[name] = sensor

        # Special case for "no bowden" designs where mmu_gate is an alias for extruder sensor
        if not self.mmu.mmu_machine.require_bowden_move and self.sensors[self.mmu.ENDSTOP_EXTRUDER_ENTRY] and self.mmu.ENDSTOP_GATE not in self.sensors:
            self.sensors[self.mmu.ENDSTOP_GATE] = self.sensors[self.mmu.ENDSTOP_EXTRUDER_ENTRY]

    # Return dict of all sensor states (or None if sensor disabled)
    def get_all_sensors(self):
        result = {}
        for name, sensor in self.sensors.items():
            result[name] = bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None
        return result

    def has_sensor(self, name):
        return self.sensors[name].runout_helper.sensor_enabled if name in self.sensors else False

    def has_gate_sensor(self, name, gate):
        return self.sensors[self.get_gate_sensor_name(name, gate)].runout_helper.sensor_enabled if self.get_gate_sensor_name(name, gate) in self.sensors else False

    def get_gate_sensor_name(self, name, gate):
        return "%s_%d" % (name, gate)

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

    def get_sensor_summary(self, include_disabled=False):
        summary = ""
        sensors = self.get_all_sensors()
        for name, state in sensors.items():
            if state is not None or include_disabled:
                summary += "%s: %s\n" % (name, 'TRIGGERED' if state is True else 'open' if state is False else '(disabled)')
        return summary

    def enable_runout(self, gate):
        self._set_sensor_runout(True, gate)

    def disable_runout(self, gate):
        self._set_sensor_runout(False, gate)

    def _set_sensor_runout(self, enable, gate):
        for name, sensor in self.sensors.items():
            if isinstance(sensor.runout_helper, MmuRunoutHelper):
                per_gate = re.search(r'_(\d+)$', name)
                if per_gate:
                    sensor.runout_helper.enable_runout(enable and (int(per_gate.group(1)) == gate))
                else:
                    sensor.runout_helper.enable_runout(enable and (gate != self.mmu.TOOL_GATE_UNKNOWN))

    # Defines sensors and relationship to filament_pos state for easy filament tracing
    def _get_sensors(self, pos, gate, position_condition):
        result = {}
        sensor_selection = [
            (self.get_gate_sensor_name(self.mmu.PRE_GATE_SENSOR_PREFIX, gate), None),
            (self.get_gate_sensor_name(self.mmu.ENDSTOP_GEAR_PREFIX, gate), self.mmu.FILAMENT_POS_HOMED_GATE if self.mmu.gate_homing_endstop == self.mmu.ENDSTOP_GEAR_PREFIX else None),
            (self.mmu.ENDSTOP_GATE, self.mmu.FILAMENT_POS_HOMED_GATE),
            (self.mmu.ENDSTOP_EXTRUDER_ENTRY, self.mmu.FILAMENT_POS_HOMED_ENTRY),
            (self.mmu.ENDSTOP_TOOLHEAD, self.mmu.FILAMENT_POS_HOMED_TS),
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
