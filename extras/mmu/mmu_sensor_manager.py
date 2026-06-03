# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager to centralize mmu_sensor operations accross mmu_units and to swap in the
#       appropriate set of "active" sensors as selected gate/unit changes (via events)
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

        # Determine sensor maps now from every perspective: logical mmu machine, per-unit and per-gate.
        # Note that keys are the simplest form to disambiguate with unit_sensors dropping unit prefix
        # (or buffer_name, toolhead_name) and gate_sensors dropping gate suffix.
        # Note: all_sensors_map keys are fully qualified
        
        self.all_sensors_map = {}    # Map of all sensors on mmu_machine with fully qualified names
        self.unit_sensors = []       # Sensors on each mmu_unit without unit prefix ('unit0_'). List indexed by unit index
        self.gate_sensors = []       # Sensors on each gate with names stripped of gate suffix and unit prefix (indexed by gate index)
        self.bypass_sensors_map = {} # Map of sensors when bypass is selected (likely just extruder and toolhead)
        self.active_sensors_map = {} # Points to current version of gate_sensors (simple names). Resets on gate change

        def collect_sensors(pairs):
            return {key: sensor for sensor, key in pairs if sensor}

        for mmu_unit in self.mmu_machine.units:

            sf_buffer = mmu_unit.buffer
            sf_buffer_name = sf_buffer.name if sf_buffer is not None else None
            sensor_defs = [
                (mmu_unit.sensors.shared_exit_sensor, SENSOR_SHARED_EXIT, mmu_unit.name),
                (sf_buffer.compression_sensor if sf_buffer else None, SENSOR_COMPRESSION, sf_buffer_name),
                (sf_buffer.tension_sensor if sf_buffer else None, SENSOR_TENSION, sf_buffer_name),
                (sf_buffer.proportional_sensor if sf_buffer else None, SENSOR_PROPORTIONAL, sf_buffer_name),
            ]

            unit_sensors = collect_sensors([
                (sensor, sensor_type)
                for sensor, sensor_type, _ in sensor_defs
            ])

            prefixed_unit_sensors = collect_sensors([
                (sensor, self.get_prefixed_sensor_name(sensor_type, name)) if sensor and name else (sensor, None)
                for sensor, sensor_type, name in sensor_defs
            ])

            unit_toolhead_sensors = collect_sensors([
                (sensor, key)
                for key, sensor in mmu_unit.toolhead_wrapper.sensors.items()
            ])

            prefixed_unit_toolhead_sensors = collect_sensors([
                (sensor, sensor.runout_helper.name if sensor else "")
                for sensor in mmu_unit.toolhead_wrapper.sensors.values()
            ])

            self.all_sensors_map.update(prefixed_unit_sensors)
            self.all_sensors_map.update(prefixed_unit_toolhead_sensors)

            for gate in range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates):
                gate_sensors = collect_sensors([
                    (mmu_unit.sensors.entry_sensors.get(gate), SENSOR_ENTRY_PREFIX),
                    (mmu_unit.sensors.exit_sensors.get(gate), SENSOR_EXIT_PREFIX),
                    (mmu_unit.sensors.shared_exit_sensor, SENSOR_SHARED_EXIT),
                    (sf_buffer and mmu_unit.buffer.compression_sensor, SENSOR_COMPRESSION),
                    (sf_buffer and mmu_unit.buffer.tension_sensor, SENSOR_TENSION),
                    (sf_buffer and mmu_unit.buffer.proportional_sensor, SENSOR_PROPORTIONAL),
                ])
                gate_sensors.update(unit_toolhead_sensors)

                self.gate_sensors.append(gate_sensors)

# PAUL: Is this still needed in v4? Not a good idea because it would complicate filament positon recovery
# PAUL: ...better to allow for no bowden in filament move logic
#                # Special case for "no bowden" (one unit) designs where mmu_shared_exit is an alias for extruder sensor
#                if (
#                    not mmu_unit.require_bowden_move and
#                    gate_sensors.get(SENSOR_EXTRUDER_ENTRY) and
#                    SENSOR_SHARED_EXIT not in self.gate_sensors
#                ):
#                    self.gate_sensors.update(connect_sensors([(mmu_unit.toolhead_wrapper.extruder_sensor, SENSOR_SHARED_EXIT)]))

                suffixed_gate_sensors = collect_sensors([
                    (mmu_unit.sensors.entry_sensors.get(gate), self.get_gate_sensor_name(SENSOR_ENTRY_PREFIX, gate)),
                    (mmu_unit.sensors.exit_sensors.get(gate), self.get_gate_sensor_name(SENSOR_EXIT_PREFIX, gate)),
                ])
                unit_sensors.update(suffixed_gate_sensors)
                self.all_sensors_map.update(suffixed_gate_sensors)

            unit_sensors.update(unit_toolhead_sensors)
            self.unit_sensors.append(unit_sensors)

            if mmu_unit == self.mmu_machine.unit_with_bypass:
                self.bypass_sensors_map.update(unit_toolhead_sensors)

        # If bypass on type-A with shared exit then that would also be seen by bypass
        unit_with_bypass = self.mmu_machine.unit_with_bypass
        if unit_with_bypass is not None:
            extra_bypass_sensors = collect_sensors([
                (mmu_unit.sensors.shared_exit_sensor, SENSOR_SHARED_EXIT),
            ])
            self.bypass_sensors_map.update(extra_bypass_sensors)

        self.mmu.printer.register_event_handler("mmu:gate_selected", self._handle_gate_selected)
        self.mmu.printer.register_event_handler("mmu:unit_selected", self._handle_unit_selected)

        # -----------------------------------------------
        # TODO: This is temporary duplicative mapping to support UI's that assume v3 sensor names
        for gate in range(self.mmu_machine.num_gates):
            s = self.gate_sensors[gate]
            for old, new in (
                ('mmu_pre_gate', SENSOR_ENTRY_PREFIX),
                ('mmu_post_gear', SENSOR_EXIT_PREFIX),
                ('mmu_gate', SENSOR_SHARED_EXIT),
            ):
                value = s.get(new)
                if value is not None:
                    s[old] = value
        # -----------------------------------------------

        # Very useful to put in log file for debugging
        class ObjectLabeller:
            def __init__(self):
                self._map = {}
                self._next = 0

            def label(self, obj):
                obj_id = id(obj)
                if obj_id not in self._map:
                    self._map[obj_id] = self._to_label(self._next)
                    self._next += 1
                return self._map[obj_id]

            def _to_label(self, n):
                # A, B, ..., Z, AA, AB, ...
                label = ""
                while True:
                    n, r = divmod(n, 26)
                    label = chr(65 + r) + label
                    if n == 0:
                        break
                    n -= 1
                return label

        labeller = ObjectLabeller()
        self.mmu.log_debug("SENSORS -----------")
        fmt = lambda d: "{" + ", ".join(f"{k}: {labeller.label(v)}" for k, v in d.items()) + "}"
        self.mmu.log_debug(f"all_sensors_map={fmt(self.all_sensors_map)}")
        for unit in self.mmu_machine.units:
            self.mmu.log_debug(f"unit_sensors[{unit.unit_index}]={fmt(self.unit_sensors[unit.unit_index])}")
        for gate in range(self.mmu_machine.num_gates):
            self.mmu.log_debug(f"gate_sensors[{gate}]={fmt(self.gate_sensors[gate])}")
        self.mmu.log_debug(f"bypass_sensors_map={fmt(self.bypass_sensors_map)}")
        self.mmu.log_debug("-------------------")


    def _handle_gate_selected(self, gate, prev_gate):
        """
        Handler for gate changed event
        Reset the relevent sensor list based on current gate handling bypass and unknown
        """
        self.active_sensors_map = self.gate_sensors[gate] if gate >= 0 else self.bypass_sensors_map
#        self.mmu.log_info("PAUL: EVENT: handle_gate_selected(%d)" % gate)
#        self.mmu.log_info("PAUL: >>> active_sensors_map=%s\n" % self.active_sensors_map.keys())


    def _handle_unit_selected(self, unit, prev_unit):
        """
        Handler for unit changed event
        Activate only sensors for current unit
        """
#        self.mmu.log_info("PAUL: EVENT: handle_unit_selected(%d)" % unit)
        # We do this in two steps to allow sensor sharing

        # First ensure any excluded unit sensor is completely deactivated
        for i, sensors in enumerate(self.unit_sensors):
            if i == unit:
                continue

            for sname, sensor in sensors.items():
                if not self.is_gate_sensor_name(sname):
                    sensor.runout_helper.enable_runout(False)
                    sensor.runout_helper.enable_button_feedback(False)

        # Activate just active unit sensors
        for sname, sensor in self.unit_sensors[unit].items():
            if not self.is_gate_sensor_name(sname):
                sensor.runout_helper.enable_button_feedback(True)


    def get_sensor_states(self, unit=None, all_sensors=False):
        """
        Return dict of sensor names and (state, sensor) tuples for:
            all sensors: (all_sensors=True)
            just active on gate: (unit=None, all_sensors=False)
            active on unit: (unit=index)

        (returns state of None if sensor disabled)
        """
        sensor_map = (
            self.all_sensors_map if all_sensors
            else self.active_sensors_map if unit is None
            else self.unit_sensors[unit]
        )

        return {
            sname: (
                bool(sensor.runout_helper.filament_present)
                if sensor.runout_helper.sensor_enabled
                else None,
                sensor,
            )
            for sname, sensor in sensor_map.items()
        }


    def has_sensor(self, sname):
        """
        Returns True if sensor is currently in active set and enabled.
        We use the runout_helper to determine is sensor has been disabled by the user
        and if so, we want to act as if it isn't configured
        """
        if sname in self.active_sensors_map:
            return self.active_sensors_map[sname].runout_helper.sensor_enabled
        else:
            return False


    def get_sensor_obj(self, sname):
        return self.active_sensors_map.get(sname)


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


    def is_gate_sensor_name(self, sname):
        """
        Returns True if sensor name is a per-gate sensor
        """
        return re.search(r'_\d+$', sname)


    def get_prefixed_sensor_name(self, sname, prefix):
        """
        Returns generic sensor name with added "<prefix>:" prefix
        """
        return f"{prefix}:{sname}"


    def get_unprefixed_sensor_name(self, name):
        """
        Returns sensor name stripped of namespace prefix
        """
        return name.split(":", 1)[-1]


    def get_qualified_endstop_name(self, endstop_name):
        """
        Convert simple endstop name into fully qualified sensor based on context
        """
        # These have form: "<unitName>:genericName"
        if endstop_name in [SENSOR_SHARED_EXIT]:
            return self.get_prefixed_sensor_name(endstop_name, self.mmu.mmu_unit().name)

        # These have form: "<bufferName>:genericName"
        if endstop_name in [SENSOR_COMPRESSION, SENSOR_TENSION]:
            return self.get_prefixed_sensor_name(endstop_name, self.mmu.mmu_unit().buffer.name)

        # These have form: "<toolheadName>:genericName"
        if endstop_name in [SENSOR_EXTRUDER_ENTRY, SENSOR_TOOLHEAD]:
            return self.get_prefixed_sensor_name(endstop_name, self.mmu.mmu_unit().toolhead_wrapper.name)

        # These have form: "genericName_<gate#>"
        if endstop_name in [SENSOR_ENTRY_PREFIX, SENSOR_EXIT_PREFIX, SENSOR_GEAR_TOUCH]:
            return self.get_gate_sensor_name(endstop_name, self.mmu.gate_selected)

        # Doesn't map
        return endstop_name


    def get_generic_endstop_name(self, endstop_name):
        """
        Convert fully qualified sensor name back to generic form
        """

        # Handle "<name>:genericName"
        if ":" in endstop_name:
            prefix, generic = endstop_name.split(":", 1)

            # Unit-based sensors
            if generic in [SENSOR_SHARED_EXIT]:
                if prefix == self.mmu.mmu_unit().name:
                    return generic

            # Buffer-based sensors
            if generic in [SENSOR_SHARED_EXIT, SENSOR_COMPRESSION, SENSOR_TENSION]:
                if prefix == self.mmu.mmu_unit().buffer.name:
                    return generic

            # Toolhead-based sensors
            if generic in [SENSOR_EXTRUDER_ENTRY, SENSOR_TOOLHEAD]:
                if prefix == self.mmu.mmu_unit().toolhead_wrapper.name:
                    return generic

        # Handle "genericName_<gate#>"
        for base in [SENSOR_ENTRY_PREFIX, SENSOR_EXIT_PREFIX, SENSOR_GEAR_TOUCH]:
            if endstop_name.startswith(base + "_"):
                return base

        # Doesn't map
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
#        logging.info("PAUL: enable_runout(gate=%d)" % gate)
        self._set_sensor_runout(True, gate)


    def disable_runout(self, gate):
#        logging.info("PAUL: disable_runout(gate=%d)" % gate)
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
            u = self.mmu.mmu_unit(gate)

            gear_homed_pos = None
            is_gear_homing_endstop = (u.p.gate_homing_endstop == SENSOR_EXIT_PREFIX)
            is_parking_retract = (u.p.gate_parking_distance < 0)
            if is_gear_homing_endstop and is_parking_retract:
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
        return {
            name: bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None
            for name, sensor in self.active_sensors_map.items()
        }
