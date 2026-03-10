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
#        sensor_names.extend([self.get_gate_sensor_name(self.mmu.SENSOR_PRE_GATE_PREFIX, i) for i in range(self.mmu.num_gates)])
#        sensor_names.extend([self.get_gate_sensor_name(self.mmu.SENSOR_GEAR_PREFIX, i) for i in range(self.mmu.num_gates)])
#        sensor_names.extend([
#            self.mmu.SENSOR_GATE,
#            self.mmu.SENSOR_TENSION,
#            self.mmu.SENSOR_COMPRESSION,
#            self.mmu.SENSOR_PROPORTIONAL
#        ])
#        if self.mmu.mmu_machine.num_units > 1:
#            for i in range(self.mmu.mmu_machine.num_units):
#                sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_GATE, i))
#                sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_TENSION, i))
#                sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_COMPRESSION, i))
#                sensor_names.append(self.get_unit_sensor_name(self.mmu.SENSOR_PROPORTIONAL, i))
#        sensor_names.extend([
#            self.mmu.SENSOR_EXTRUDER_ENTRY,
#            self.mmu.SENSOR_TOOLHEAD
#>>>>>>> main
            
        for mmu_unit in self.mmu_machine.units:
            unit_sensors = collect_sensors([
                (mmu_unit.sensors.gate_sensor, SENSOR_GATE),
                (mmu_unit.buffer and mmu_unit.buffer.compression_sensor, SENSOR_COMPRESSION),
                (mmu_unit.buffer and mmu_unit.buffer.tension_sensor, SENSOR_TENSION),
                (mmu_unit.buffer and mmu_unit.buffer.proportional_sensor, SENSOR_PROPORTIONAL),
            ])
            qualified_unit_sensors = collect_sensors([
                (mmu_unit.sensors.gate_sensor, self.get_unit_sensor_name(SENSOR_GATE, mmu_unit.unit_index)),
                (mmu_unit.buffer and mmu_unit.buffer.compression_sensor, self.get_unit_sensor_name(SENSOR_COMPRESSION, mmu_unit.unit_index)),
                (mmu_unit.buffer and mmu_unit.buffer.tension_sensor, self.get_unit_sensor_name(SENSOR_TENSION, mmu_unit.unit_index)),
                (mmu_unit.buffer and mmu_unit.buffer.proportional_sensor, self.get_unit_sensor_name(SENSOR_PROPORTIONAL, mmu_unit.unit_index)),
            ])
            self.all_sensors_map.update(qualified_unit_sensors)
        
            for gate in range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates):
                self.gate_sensors.append(collect_sensors([
                    (mmu_unit.sensors.pre_gate_sensors.get(gate), SENSOR_PRE_GATE_PREFIX),
                    (mmu_unit.sensors.post_gear_sensors.get(gate), SENSOR_GEAR_PREFIX),
                    (mmu_unit.sensors.gate_sensor, SENSOR_GATE),
                    (mmu_unit.buffer and mmu_unit.buffer.compression_sensor, SENSOR_COMPRESSION),
                    (mmu_unit.buffer and mmu_unit.buffer.tension_sensor, SENSOR_TENSION),
                    (self.mmu_machine.extruder_sensor, SENSOR_EXTRUDER_ENTRY),
                    (self.mmu_machine.toolhead_sensor, SENSOR_TOOLHEAD),
                ]))
                qualified_gate_sensors = collect_sensors([
                    (mmu_unit.sensors.pre_gate_sensors.get(gate), self.get_gate_sensor_name(SENSOR_PRE_GATE_PREFIX, gate)),
                    (mmu_unit.sensors.post_gear_sensors.get(gate), self.get_gate_sensor_name(SENSOR_GEAR_PREFIX, gate)),
                ])
                unit_sensors.update(qualified_gate_sensors)
                self.all_sensors_map.update(qualified_gate_sensors)

            unit_sensors.update(common_sensors)
            self.unit_sensors.append(unit_sensors)

        self.all_sensors_map.update(common_sensors)
        self.bypass_sensors_map = common_sensors

        self.mmu.printer.register_event_handler("mmu:gate_selected", self._handle_gate_selected)
        self.mmu.printer.register_event_handler("mmu:unit_selected", self._handle_unit_selected)

## From v340 vvv
#        mmu_sensors = self.mmu.printer.lookup_object("mmu_sensors") # PAUL use this instead
#        self.all_sensors = mmu_sensors.sensors # PAUL use this instead
#        # Special case for "no bowden" (one unit) designs where mmu_gate is an alias for extruder sensor
#        if not self.mmu.mmu_machine.require_bowden_move and self.all_sensors.get(self.mmu.SENSOR_EXTRUDER_ENTRY, None) and self.mmu.SENSOR_GATE not in self.all_sensors:
#            self.all_sensors[self.mmu.SENSOR_GATE] = self.all_sensors[self.mmu.SENSOR_EXTRUDER_ENTRY]
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

# Orig v4... vvv
# TEMP COMMENT .. moving to mmu_unit
#        # Setup filament sensors as homing (endstops) on respective mmu_unit
#        for i, sensors in enumerate(self.unit_sensors):
#            unit = self.mmu_machine.get_mmu_unit_by_index(i)
#            gear_rail = unit.mmu_toolhead.get_kinematics().rails[1]
#            for name, sensor in self.unit_sensors[i].items():
#                if not name.startswith(SENSOR_PRE_GATE_PREFIX):
#                    logging.info("PAUL: creating endstop for unit=%d, sensor.name=%s" % (i, sensor.runout_helper.name))
#                    sensor_pin = sensor.runout_helper.switch_pin
#                    ppins = self.mmu.printer.lookup_object('pins')
#                    pin_params = ppins.parse_pin(sensor_pin, True, True)
#                    share_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])
#                    ppins.allow_multi_use_pin(share_name) # can this be called more than once?
#                    if name not in gear_rail.get_extra_endstop_names():
#                        mcu_endstop = gear_rail.add_extra_endstop(sensor_pin, name) # paul results in shared gate, compression and tension endtop names!
#
#                    # This ensures rapid stopping of extruder stepper when endstop is hit on synced homing
#                    # otherwise the extruder can continue to move a small (speed dependent) distance
#                    if unit.extruder_stepper_obj() is not None and name in [SENSOR_TOOLHEAD, SENSOR_COMPRESSION, SENSOR_TENSION]:
#                        mcu_endstop.add_stepper(unit.extruder_stepper_obj().stepper)
#                else:
#                    logging.warning("MMU: Filament sensor %s is not defined in [mmu_sensors]" % name)
# Orig v4... ^^^


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


    # Return dict of all sensor states for just active or all sensors (returns None if sensor disabled)
    def get_active_sensors(self, all_sensors=False):
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
        return "%s_%d" % (sname, gate)

    def get_unit_sensor_name(self, sname, unit):
        return "unit%d_%s" % (unit, sname)

    def get_unitless_sensor_name(self, name):
        return re.sub(r'unit_\d+_', '', name)

    # Get unit or gate specific endstop if it exists
    # Take generic name and look for "<unit>_genericName" and "genericName_<gate>"
    def get_mapped_endstop_name(self, endstop_name):
        if endstop_name in [SENSOR_GATE, SENSOR_COMPRESSION, SENSOR_TENSION]:
            return self.get_unit_sensor_name(endstop_name, mmu.unit_selected)

        if endstop_name in [SENSOR_PRE_GATE_PREFIX, SENSOR_GEAR_PREFIX, SENSOR_GEAR_TOUCH]: # PAUL TODO verify SENSOR_GEAR_TOUCH operation for calibration
            return self.get_gate_sensor_name(endstop_name, mmu.gate_selected)

        return endstop_name

    # Return sensor state or None if not installed
    def check_sensor(self, name):
        sensor = self.active_sensors_map.get(name, None)
        if sensor is not None and sensor.runout_helper.sensor_enabled:
            detected = bool(sensor.runout_helper.filament_present)
            return detected
        else:
            return None

    # Return per-gate sensor state or None if not installed
    def check_gate_sensor(self, name, gate):
        sensor_name = self.get_gate_sensor_name(name, gate)
        sensor = self.all_sensors_map.get(sensor_name, None)
        if sensor is not None and sensor.runout_helper.sensor_enabled:
            detected = bool(sensor.runout_helper.filament_present)
            return detected
        else:
            return None

    # Returns True if ALL sensors before position detect filament
    #         None if NO sensors available (disambiguate from non-triggered sensor)
    # Can be used as a "filament continuity test"
    def check_all_sensors_before(self, pos, gate, loading=True):
        sensors = self.get_sensors_before(pos, gate, loading)
        if all(state is None for state in sensors.values()):
            return None
        return all(state is not False for state in sensors.values())

    # Returns True if ANY sensor before position detects filament
    #         None if NO sensors available (disambiguate from non-triggered sensor)
    # Can be used as a filament visibility test over a portion of the travel
    def check_any_sensors_before(self, pos, gate, loading=True):
        sensors = self.get_sensors_before(pos, gate, loading)
        if all(state is None for state in sensors.values()):
            return None
        return any(state is True for state in sensors.values())

    # Returns True if ALL sensors after position detect filament
    #         None if NO sensors available (disambiguate from non-triggered sensor)
    # Can be used as a "filament continuity test"
    def check_all_sensors_after(self, pos, gate, loading=True):
        sensors = self.get_sensors_after(pos, gate, loading)
        if all(state is None for state in sensors.values()):
            return None
        return all(state is not False for state in sensors.values())

    # Returns True if ANY sensor after position detects filament
    #         None if no sensors available (disambiguate from non-triggered sensor)
    # Can be used to validate position
    def check_any_sensors_after(self, pos, gate, loading=True):
        sensors = self.get_sensors_after(pos, gate, loading)
        if all(state is None for state in sensors.values()):
            return None
        return any(state is True for state in sensors.values())

    # Returns True if all sensors in current filament path are triggered
    #         None if no sensors available (disambiguate from non-triggered sensor)
    def check_all_sensors_in_path(self):
        sensors = self.get_sensors_before(FILAMENT_POS_LOADED, self.mmu.gate_selected)
        if all(state is None for state in sensors.values()):
            return None
        return all(state is not False for state in sensors.values())

    # Returns True if any sensors in current filament path are triggered (EXCLUDES pre-gate)
    #         None if no sensors available (disambiguate from non-triggered sensor)
    def check_any_sensors_in_path(self):
        sensors = self.get_all_sensors_for_gate(self.mmu.gate_selected)
        if all(state is None for state in sensors.values()):
            return None
        return any(state is True for state in sensors.values())

    # Returns True is any sensors in filament path are not triggered
    #         None if no sensors available (disambiguate from non-triggered sensor)
    # Can be used to spot failure in "continuity" i.e. runout
    def check_for_runout(self):
        sensors = self.get_sensors_before(FILAMENT_POS_LOADED, self.mmu.gate_selected)
        if all(state is None for state in sensors.values()):
            return None
        return any(state is False for state in sensors.values())

    # Error with explanation if any filament sensors don't detect filament
    def confirm_loaded(self):
        sensors = self.get_sensors_before(FILAMENT_POS_LOADED, self.mmu.gate_selected)
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
            # Note: For gear sensor the position of POS_HOMED_GATE is only valid if is not usually triggered (i.e. parking retract)
            sensor_selection = [
                (SENSOR_PRE_GATE_PREFIX, None),
                (SENSOR_GEAR_PREFIX, FILAMENT_POS_HOMED_GATE if self.mmu.UNIT.p.gate_homing_endstop == SENSOR_GEAR_PREFIX and self.mmu.UNIT.p.gate_parking_distance <= 0 else None), # PAUL check parking distance sign with v4
                (SENSOR_GATE, FILAMENT_POS_HOMED_GATE),
                (SENSOR_EXTRUDER_ENTRY, FILAMENT_POS_HOMED_ENTRY),
                (SENSOR_TOOLHEAD, FILAMENT_POS_HOMED_TS),
            ]
            for name, position_check in sensor_selection:
                sensor = self.active_sensors_map.get(name, None)
                if sensor and position_condition(pos, position_check):
                    result[name] = bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None
        return result # TODO handle bypass and return only EXTRUDER_ENTRY and TOOLHEAD sensors

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
