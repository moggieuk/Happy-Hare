# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager to centralize mmu_sensor operations accross mmu_units
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, re

# Happy Hare imports
from .mmu_constants    import *
from .mmu_utils        import MmuError
from .mmu_sensor_utils import MmuRunoutHelper


class MmuSensorManager:
    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu_machine = mmu.mmu_machine
    
        # Determine sensor maps now from every perspective: logocal mmu machine, per-unit and per-gate.
        # Note that keys are the simplest form to disambiguate with unit_sensors dropping unit prefix
        # and gate_sensors dropping gate suffix
        self.all_sensors_map = {}    # Map of all sensors on mmu_machine with fully qualified names
        self.unit_sensors = []       # Sensors on each mmu_unit without unit prefix (indexed by unit index)
        self.gate_sensors = []       # Sensors on each gate with names stripped of gate suffix and unit prefix (indexed by gate index)
        self.bypass_sensor_map = {}  # Map of sensors when bypass is selected (extruder and toolhead only)
        self.active_sensors_map = {} # Points to current version of gate_sensors (simple names). Resets on gate change
        
        def collect_sensors(pairs):
            return {key: sensor for sensor, key in pairs if sensor}
        
        common_sensors = collect_sensors([
            (self.mmu_machine.extruder_sensor, SENSOR_EXTRUDER_ENTRY),
            (self.mmu_machine.toolhead_sensor, SENSOR_TOOLHEAD),
        ])

#=======
#        self.all_sensors = {}      # All sensors on mmu unit optionally with unit prefix and gate suffix
#        self.sensors = {}          # All (presence detection) sensors on active unit stripped of unit prefix
#        self.viewable_sensors = {} # Sensors of all types for current gate/unit renamed with simple names
#
#        # Assemble all possible switch sensors in desired display order
#        sensor_names = []
#        sensor_names.extend([self.get_gate_sensor_name(self.mmu.SENSOR_ENTRY_PREFIX, i) for i in range(self.mmu.num_gates)])
#        sensor_names.extend([self.get_gate_sensor_name(self.mmu.SENSOR_EXIT_PREFIX, i) for i in range(self.mmu.num_gates)])
#        sensor_names.extend([
#            self.mmu.SENSOR_SHARED_EXIT,
#            self.mmu.SENSOR_TENSION,
#            self.mmu.SENSOR_COMPRESSION,
#            self.mmu.SENSOR_PROPORTIONAL
#        ])
#        if self.mmu.mmu_machine.num_units > 1:
#            for i in range(self.mmu.mmu_machine.num_units):
#                sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_SHARED_EXIT, i))
#                sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_TENSION, i))
#                sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_COMPRESSION, i))
#                sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_PROPORTIONAL, i))
#        sensor_names.extend([
#            self.mmu.SENSOR_EXTRUDER_ENTRY,
#            self.mmu.SENSOR_TOOLHEAD
#>>>>>>> main
            
        for mmu_unit in self.mmu_machine.units:
            unit_sensors = collect_sensors([
                (mmu_unit.sensors.gate_sensor, SENSOR_SHARED_EXIT),
                (mmu_unit.buffer and mmu_unit.buffer.compression_sensor, SENSOR_COMPRESSION),
                (mmu_unit.buffer and mmu_unit.buffer.tension_sensor, SENSOR_TENSION),
                (mmu_unit.buffer and mmu_unit.buffer.proportional_sensor, SENSOR_PROPORTIONAL),
            ])
            qualified_unit_sensors = collect_sensors([
                (mmu_unit.sensors.gate_sensor, self.get_unit_sensor_name(SENSOR_SHARED_EXIT, mmu_unit.unit_index)),
                (mmu_unit.buffer and mmu_unit.buffer.compression_sensor, self.get_unit_sensor_name(SENSOR_COMPRESSION, mmu_unit.unit_index)),
                (mmu_unit.buffer and mmu_unit.buffer.tension_sensor, self.get_unit_sensor_name(SENSOR_TENSION, mmu_unit.unit_index)),
                (mmu_unit.buffer and mmu_unit.buffer.proportional_sensor, self.get_unit_sensor_name(SENSOR_PROPORTIONAL, mmu_unit.unit_index)),
            ])
            self.all_sensors_map.update(qualified_unit_sensors)
        
            for gate in range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates):
                self.gate_sensors.append(collect_sensors([
                    (mmu_unit.sensors.entry_sensors.get(gate), SENSOR_ENTRY_PREFIX),
                    (mmu_unit.sensors.post_gear_sensors.get(gate), SENSOR_EXIT_PREFIX),
                    (mmu_unit.sensors.gate_sensor, SENSOR_SHARED_EXIT),
                    (mmu_unit.buffer and mmu_unit.buffer.compression_sensor, SENSOR_COMPRESSION),
                    (mmu_unit.buffer and mmu_unit.buffer.tension_sensor, SENSOR_TENSION),
                    (self.mmu_machine.extruder_sensor, SENSOR_EXTRUDER_ENTRY),
                    (self.mmu_machine.toolhead_sensor, SENSOR_TOOLHEAD),
                ]))
                qualified_gate_sensors = collect_sensors([
                    (mmu_unit.sensors.entry_sensors.get(gate), self.get_gate_sensor_name(SENSOR_ENTRY_PREFIX, gate)),
                    (mmu_unit.sensors.post_gear_sensors.get(gate), self.get_gate_sensor_name(SENSOR_EXIT_PREFIX, gate)),
                ])
                unit_sensors.update(qualified_gate_sensors)
                self.all_sensors_map.update(qualified_gate_sensors)

            unit_sensors.update(common_sensors)
            self.unit_sensors.append(unit_sensors)

        self.all_sensors_map.update(common_sensors)
        self.bypass_sensors_map = common_sensors

        self.mmu.printer.register_event_handler("mmu:gate_selected", self._handle_gate_selected)
        self.mmu.printer.register_event_handler("mmu:unit_selected", self._handle_unit_selected)

## PAUL Don't ,think we need this or can gate homing be extruder sensor in v4?
# From v340 vvv
#        # Special case for "no bowden" (one unit) designs where mmu_shared_exit is an alias for extruder sensor
#        if not self.mmu.mmu_machine.require_bowden_move and self.all_sensors.get(self.mmu.SENSOR_EXTRUDER_ENTRY, None) and self.mmu.SENSOR_SHARED_EXIT not in self.all_sensors:
#            self.all_sensors[self.mmu.SENSOR_SHARED_EXIT] = self.all_sensors[self.mmu.SENSOR_EXTRUDER_ENTRY]
#        logging.info("PAUL: all_sensors=%s\n" % self.all_sensors.keys())
## From v340 ^^^

# PAUL.. testing how did we do?
        logging.info("PAUL: all_sensors_map=%s\n" % self.all_sensors_map.keys())
        for unit in self.mmu_machine.units:
            logging.info("PAUL: unit_sensors[%d]=%s\n" % (unit.unit_index, self.unit_sensors[unit.unit_index].keys()))
        for gate in range(self.mmu_machine.num_gates):
            logging.info("PAUL: gate_sensors[%d]=%s\n" % (gate, self.gate_sensors[gate].keys()))
        logging.info("PAUL: bypass_sensor_map=%s\n" % self.bypass_sensor_map)
        logging.info("PAUL: active_sensosr_map=%s\n" % self.active_sensors_map)
# PAUL ^^^


    def _handle_gate_selected(self, gate):
        """
        Handler for gate changed event
        Reset the relevent sensor list based on current gate handling bypass and unknown
        """
        self.mmu.log_info("PAUL: handle_gate_selected(%d)" % gate)
        self.active_sensors_map = self.gate_sensors[gate] if gate > 0 else self.bypass_sensors_map


    def _handle_unit_selected(self, unit):
        """
        Handler for unit changed event
        Activate only sensors for current unit
        """
        self.mmu.log_info("PAUL: handle_unit_selected(%d)" % unit)
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


    def get_active_sensors(self, all_sensors=False):
        """
        Return dict of all sensor states for just active or all sensors
        (returns None if sensor disabled)
        """
        logging.info("PAUL: active_sensors_map=%s", self.active_sensors_map)
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
        """
        Returns generic sensor name with added "_<gate#>" suffix
        """
        return "%s_%d" % (sname, gate)


    def get_unit_sensor_name(self, sname, unit):
        """
        Returns generic sensor name with added "<unit#>_" prefix
        """
        return "unit%d_%s" % (unit, sname)


    def get_unitless_sensor_name(self, name):
        """
        Returns sensor name stripped of unit prefix
        """
        return re.sub(r'unit_\d+_', '', name)


    def get_mapped_endstop_name(self, endstop_name):
        """
        Get unit or gate specific endstop if it exists
        Take generic name and look for "<unit#>_genericName" and "genericName_<gate#>"
        """
        if endstop_name in [SENSOR_SHARED_EXIT, SENSOR_COMPRESSION, SENSOR_TENSION]:
            return self.get_unit_sensor_name(endstop_name, self.mmu.unit_selected)

        if endstop_name in [SENSOR_ENTRY_PREFIX, SENSOR_EXIT_PREFIX, SENSOR_GEAR_TOUCH]: # PAUL TODO verify SENSOR_GEAR_TOUCH operation for calibration
            return self.get_gate_sensor_name(endstop_name, self.mmu.gate_selected)

        return endstop_name


    def check_sensor(self, name):
        """
        Return sensor state or None if unavailable/disabled.
        """
        sensor = self.active_sensors_map.get(name, None)
        if sensor is not None and sensor.runout_helper.sensor_enabled:
            return bool(sensor.runout_helper.filament_present)
        return None


    def check_gate_sensor(self, name, gate):
        """
        Return per-gate sensor state or None if unavailable/disabled.
        """
        sensor_name = self.get_gate_sensor_name(name, gate)
        sensor = self.all_sensors_map.get(sensor_name, None)
        if sensor is not None and sensor.runout_helper.sensor_enabled:
            return bool(sensor.runout_helper.filament_present)
        return None


    def check_all_sensors_before(self, pos, gate, loading=True):
        """
        Return True if all sensors before position detect filament.
        Returns None if no sensors are available.
        """
        sensors = self.get_sensors_before(pos, gate, loading)
        if all(state is None for state in sensors.values()): return None
        return all(state is not False for state in sensors.values())


    def check_any_sensors_before(self, pos, gate, loading=True):
        """
        Return True if any sensor before position detects filament.
        Returns None if no sensors are available.
        """
        sensors = self.get_sensors_before(pos, gate, loading)
        if all(state is None for state in sensors.values()): return None
        return any(state is True for state in sensors.values())


    def check_all_sensors_after(self, pos, gate, loading=True):
        """
        Return True if all sensors after position detect filament.
        Returns None if no sensors are available.
        """
        sensors = self.get_sensors_after(pos, gate, loading)
        if all(state is None for state in sensors.values()): return None
        return all(state is not False for state in sensors.values())


    def check_any_sensors_after(self, pos, gate, loading=True):
        """
        Return True if any sensor after position detects filament.
        Returns None if no sensors are available.
        """
        sensors = self.get_sensors_after(pos, gate, loading)
        if all(state is None for state in sensors.values()): return None
        return any(state is True for state in sensors.values())


    def check_all_sensors_in_path(self):
        """
        Return True if all sensors in the active filament path are triggered.
        Returns None if no sensors are available.
        """
        sensors = self.get_sensors_before(FILAMENT_POS_LOADED, self.mmu.gate_selected)
        if all(state is None for state in sensors.values()): return None
        return all(state is not False for state in sensors.values())


    def check_any_sensors_in_path(self):
        """
        Return True if any sensor in the active filament path is triggered.
        Excludes mmu entry sensors. Returns None if no sensors are available.
        """
        sensors = self.get_all_sensors_for_gate(self.mmu.gate_selected)
        if all(state is None for state in sensors.values()): return None
        return any(state is True for state in sensors.values())


    def check_for_runout(self):
        """
        Return True if any sensor in the filament path reports runout.
        Returns None if no sensors are available.
        """
        sensors = self.get_sensors_before(FILAMENT_POS_LOADED, self.mmu.gate_selected)
        if all(state is None for state in sensors.values()): return None
        return any(state is False for state in sensors.values())


    def confirm_loaded(self):
        """
        Raise an error if any sensor in the filament path fails to detect filament.
        """
        sensors = self.get_sensors_before(FILAMENT_POS_LOADED, self.mmu.gate_selected)
        if any(state is False for state in sensors.values()):
            MmuError("Loaded check failed:\nFilament not detected by sensors: %s" %
                     ', '.join([n for n, s in sensors.items() if s is False]))


    def enable_runout(self, gate):
        logging.info("PAUL: enable_runout(gate=%d)" % gate)
        self._set_sensor_runout(True, gate)


    def disable_runout(self, gate):
        logging.info("PAUL: disable_runout(gate=%d)" % gate)
        self._set_sensor_runout(False, gate)


    def _set_sensor_runout(self, enable, gate):
        for name, sensor in self.active_sensors_map.items():
            sensor.runout_helper.enable_runout(enable and gate >= 0)


    def _get_sensors(self, pos, gate, position_condition):
        """
        Common helper that defines sensors and relationship to filament_pos state for easy filament tracing.
        Returns {sensor_name: True/False/None} where None means sensor disabled.
        """
        def read_sensor(name):
            sensor = self.active_sensors_map.get(name)
            if not sensor:
                return None, None # (exists, value)
            if not sensor.runout_helper.sensor_enabled:
                return True, None
            return True, bool(sensor.runout_helper.filament_present)

        sensor_selection = []

        if gate >= 0:
            # Note: For mmu exit sensor the position of POS_HOMED_GATE is only valid if is not usually triggered (i.e. parking retract)
            u = self.mmu_machine.get_mmu_unit_by_gate(gate)

            gear_homed_pos = None
            is_gear_homing_endstop = (u.p.gate_homing_endstop == SENSOR_EXIT_PREFIX)
            not_parking_retract = (u.p.gate_parking_distance <= 0) # PAUL check parking distance sign with v4 - direction reversed!
            if is_gear_homing_endstop and not_parking_retract:
                gear_homed_pos = FILAMENT_POS_HOMED_GATE

            sensor_selection = [
                (SENSOR_ENTRY_PREFIX, None),
                (SENSOR_EXIT_PREFIX, gear_homed_pos),
                (SENSOR_SHARED_EXIT, FILAMENT_POS_HOMED_GATE),
                (SENSOR_EXTRUDER_ENTRY, FILAMENT_POS_HOMED_ENTRY),
                (SENSOR_TOOLHEAD, FILAMENT_POS_HOMED_TS),
            ]

        elif gate == TOOL_GATE_BYPASS:
            sensor_selection = [
                (SENSOR_EXTRUDER_ENTRY, FILAMENT_POS_HOMED_ENTRY),
                (SENSOR_TOOLHEAD, FILAMENT_POS_HOMED_TS),
            ]

        result = {}
        for name, position_check in sensor_selection:
            exists, value = read_sensor(name)
            if exists and position_condition(pos, position_check):
                result[name] = value

        return result


    def get_sensors_before(self, pos, gate, loading=True):
        return self._get_sensors(pos, gate, lambda p, pc: pc is None or (loading and p >= pc) or (not loading and p > pc))


    def get_sensors_after(self, pos, gate, loading=True):
        return self._get_sensors(pos, gate, lambda p, pc: pc is not None and ((loading and p < pc) or (not loading and p <= pc)))


    def get_all_sensors_for_gate(self,  gate):
        return self._get_sensors(-1, gate, lambda p, pc: pc is not None)


    def get_status(self, eventtime=None):
        result = {
            name: bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None
            for name, sensor in self.active_sensors_map.items()
        }
