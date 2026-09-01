# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager to centralize mmu_sensor operations across mmu_units and to swap in the
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
from .mmu_sensor_utils import MmuRunoutHelper, MmuVirtualEndstopSensor


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
        self._suspended_sensors = [] # Exactly what suspend_sensor_events(True) touched, so restore matches
        self._suspend_depth = 0      # ...and how many nested blocks are holding it

        def collect_sensors(pairs):
            return {key: sensor for sensor, key in pairs if sensor}

        for mmu_unit in self.mmu_machine.units:

            sf_buffer = mmu_unit.buffer
            sf_buffer_name = sf_buffer.name if sf_buffer is not None else None
            encoder = mmu_unit.encoder
            encoder_name = encoder.name if encoder is not None else None
            sensor_defs = [
                (mmu_unit.sensors.shared_exit_sensor, SENSOR_SHARED_EXIT, mmu_unit.name),
                (sf_buffer.compression_sensor if sf_buffer else None, SENSOR_COMPRESSION, sf_buffer_name),
                (sf_buffer.tension_sensor if sf_buffer else None, SENSOR_TENSION, sf_buffer_name),
                (sf_buffer.proportional_sensor if sf_buffer else None, SENSOR_PROPORTIONAL, sf_buffer_name),
                (encoder.endstop_sensor if encoder else None, SENSOR_ENCODER, encoder_name),
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
                    (encoder and mmu_unit.encoder.endstop_sensor, SENSOR_ENCODER),
                    (mmu_unit.sensors.shared_exit_sensor, SENSOR_SHARED_EXIT),
                ])
                gate_sensors.update(unit_toolhead_sensors)

                self.gate_sensors.append(gate_sensors)

                # TODO: this complicates filament position recovery. Need to address.
                # Special case for "no bowden" designs where mmu_shared_exit is an alias for extruder sensor.
                # This allows "gate loading" to use the extruder sensor
                if (
                    not mmu_unit.require_bowden_move
                    and gate_sensors.get(SENSOR_EXTRUDER_ENTRY)
                    and SENSOR_SHARED_EXIT not in gate_sensors
                ):
                    gate_sensors[SENSOR_SHARED_EXIT] = gate_sensors[SENSOR_EXTRUDER_ENTRY]

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
                ('mmu_gear', SENSOR_EXIT_PREFIX),
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
                    suffix = "(v)" if isinstance(obj, MmuVirtualEndstopSensor) else ""
                    self._map[obj_id] = self._to_label(self._next) + suffix
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

        # Reverse index so a MmuRunoutHelper (e.g. from cmd_SET_FILAMENT_SENSOR) can find its
        # own qualified name to persist a live enable/disable change (see set_sensor_enabled())
        self._helper_to_qualified_name = {
            sensor.runout_helper: qname for qname, sensor in self.all_sensors_map.items()
        }

        # Initialize with assumption of unit 0 selected
        self.active_sensors_map = self.unit_sensors[0]


    def _handle_gate_selected(self, gate, prev_gate):
        """
        Handler for gate changed event
        Reset the relevant sensor list based on current gate handling bypass and unknown
        """
        if gate == TOOL_GATE_UNKNOWN:
            unit = self.mmu.unit_selected
            if unit is None:
                self.mmu.log_assertion(f"Unknown unit in _handle_gate_selected()")
                unit = 0
            self.active_sensors_map = self.unit_sensors[unit]

        elif gate == TOOL_GATE_BYPASS:
            self.active_sensors_map = self.bypass_sensors_map

        else:
            self.active_sensors_map = self.gate_sensors[gate]


    def _handle_unit_selected(self, unit, prev_unit):
        """
        Handler for unit changed event
        Activate only sensors for current unit
        """
        # We do this in two steps to allow sensor sharing

        # A shared sensor (e.g. a common toolhead/extruder switch) appears in every unit's map,
        # so it must be left alone here or selecting a unit would disarm its own sensor
        shared = {sensor for sname, sensor in self.unit_sensors[unit].items()
                  if not self.is_gate_sensor_name(sname)}

        # First ensure any excluded unit sensor is completely deactivated
        for i, sensors in enumerate(self.unit_sensors):
            if i == unit:
                continue

            for sname, sensor in sensors.items():
                if not self.is_gate_sensor_name(sname) and sensor not in shared:
                    sensor.runout_helper.enable_runout(False)
                    sensor.runout_helper.enable_button_feedback(False)

        # Activate just active unit sensors
        for sname, sensor in self.unit_sensors[unit].items():
            if not self.is_gate_sensor_name(sname):
                sensor.runout_helper.enable_button_feedback(True)

        # Selecting a unit changes WHICH sensors are in scope, not whether monitoring is on,
        # so re-apply the current state - otherwise the new unit stays disarmed until the
        # next enable, which for a unit-level runout sensor means a missed runout
        self._set_sensor_runout(self.mmu.filament_monitoring_enabled, self.mmu.gate_selected)


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
        and if so, we want to act as if it isn't configured.
        Accepts a generic or fully-qualified/gate-suffixed name (active_sensors_map is keyed
        by generic names for the active gate, so we normalize first).
        """
        sname = self.get_generic_endstop_name(sname)
        if sname in self.active_sensors_map:
            return self.active_sensors_map[sname].runout_helper.sensor_enabled
        else:
            return False


    def get_sensor_obj(self, sname):
        return self.active_sensors_map.get(self.get_generic_endstop_name(sname))


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


    def get_qualified_endstop_name(self, endstop_name, mmu_unit=None):
        """
        Convert simple endstop name into fully qualified sensor based on context
        Harmless if name is already fully qualified
        """
        mmu_unit = mmu_unit or self.mmu.mmu_unit()

        # These have form: "<unitName>:genericName"
        if endstop_name in [SENSOR_SHARED_EXIT]:
            return self.get_prefixed_sensor_name(endstop_name, mmu_unit.name)

        # These have form: "<bufferName>:genericName" (buffer is optional, may not be fitted)
        if endstop_name in [SENSOR_COMPRESSION, SENSOR_TENSION]:
            if mmu_unit.buffer:
                return self.get_prefixed_sensor_name(endstop_name, mmu_unit.buffer.name)
            return endstop_name

        # These have form: "<encoderName>:genericName" (encoder is optional, may not be fitted)
        if endstop_name in [SENSOR_ENCODER]:
            if mmu_unit.encoder:
                return self.get_prefixed_sensor_name(endstop_name, mmu_unit.encoder.name)
            return endstop_name

        # These have form: "<toolheadName>:genericName"
        if endstop_name in [SENSOR_EXTRUDER_ENTRY, SENSOR_TOOLHEAD]:
            return self.get_prefixed_sensor_name(endstop_name, mmu_unit.toolhead_wrapper.name)

        # These have form: "genericName_<gate#>"
        if endstop_name in [SENSOR_ENTRY_PREFIX, SENSOR_EXIT_PREFIX, SENSOR_GEAR_TOUCH, SENSOR_NFC_PREFIX]:
            return self.get_gate_sensor_name(endstop_name, self.mmu.gate_selected)

        # Doesn't map or already a qualified name
        return endstop_name


    def get_generic_endstop_name(self, endstop_name):
        """
        Convert fully qualified sensor name back to generic form.
        Note that fully qualified names never have both unit prefix
        and gate suffix - gate indexes are global
        """

        # Handle "<name>:genericName"
        if ":" in endstop_name:
            prefix, generic = endstop_name.split(":", 1)
            mmu_unit = self.mmu.mmu_unit()

            # Unit-based sensors
            if generic in [SENSOR_SHARED_EXIT]:
                if prefix == mmu_unit.name:
                    return generic

            # Buffer-based sensors (buffer is optional, may not be fitted)
            if generic in [SENSOR_COMPRESSION, SENSOR_TENSION]:
                if mmu_unit.buffer and prefix == mmu_unit.buffer.name:
                    return generic

            # Encoder-based sensors (encoder is optional, may not be fitted)
            if generic in [SENSOR_ENCODER]:
                if mmu_unit.encoder and prefix == mmu_unit.encoder.name:
                    return generic

            # Toolhead-based sensors
            if generic in [SENSOR_EXTRUDER_ENTRY, SENSOR_TOOLHEAD]:
                if prefix == mmu_unit.toolhead_wrapper.name:
                    return generic

        # Handle "genericName_<gate#>"
        for base in [SENSOR_ENTRY_PREFIX, SENSOR_EXIT_PREFIX, SENSOR_GEAR_TOUCH]:
            if endstop_name.startswith(base + "_"):
                return base

        # Doesn't map
        return endstop_name


    def resolve_sensor(self, sname, mmu_unit=None):
        """
        Resolve a user-supplied sensor name against all_sensors_map - the stable, fully-qualified
        registry of every sensor on the machine (unlike active_sensors_map/get_sensor_obj(), which
        only cover what's currently in scope for the selected gate/unit and would miss an
        out-of-scope or already-disabled sensor - both of which still need to be nameable here).

        Accepts:
          - a fully-qualified name exactly as MMU_SENSORS prints it (e.g. 'unit0:mmu_shared_exit',
            'mmu_exit_0') - tried first, always unambiguous.
          - a bare/generic name (e.g. 'mmu_shared_exit', 'filament_tension') - qualified using
            mmu_unit's context if given. Without mmu_unit, a bare name that maps to more than one
            unit's sensor is rejected as ambiguous rather than silently resolved against whichever
            gate/unit happens to be selected right now.

        Returns (qualified_name, sensor, error): error is None on success, else 'unknown' or
        'ambiguous' (qualified_name/sensor are None in the error case).
        """
        if sname in self.all_sensors_map:
            return sname, self.all_sensors_map[sname], None

        if mmu_unit is not None:
            qualified = self.get_qualified_endstop_name(sname, mmu_unit=mmu_unit)
            if qualified in self.all_sensors_map:
                return qualified, self.all_sensors_map[qualified], None

        matches = [k for k in self.all_sensors_map if self.get_unprefixed_sensor_name(k) == sname]
        if len(matches) == 1:
            return matches[0], self.all_sensors_map[matches[0]], None
        if len(matches) > 1:
            return None, None, 'ambiguous'

        if mmu_unit is None:
            qualified = self.get_qualified_endstop_name(sname)
            if qualified in self.all_sensors_map:
                return qualified, self.all_sensors_map[qualified], None

        return None, None, 'unknown'


    def _persist_enabled(self, qualified_name, enabled, write=True):
        """
        Sparse persistence: only entries that are disabled (False) are ever stored. "Enabled" is
        the default and is never written, so a stale entry left behind by a config edit that
        removed/renamed a sensor is simply never looked up again (see load_persisted_state()) -
        it can't block boot.
        """
        persisted = dict(self.mmu.var_manager.get(VARS_MMU_SENSOR_ENABLED, {}))
        changed = False
        if enabled:
            if persisted.pop(qualified_name, None) is not None:
                changed = True
        elif persisted.get(qualified_name) is not False:
            persisted[qualified_name] = False
            changed = True

        if changed:
            self.mmu.var_manager.set(VARS_MMU_SENSOR_ENABLED, persisted, write=write)
        return changed


    def set_sensor_enabled(self, qualified_name, enabled, write=True):
        """
        Enable/disable one sensor by its all_sensors_map key and persist the change so it
        survives a restart (MMU_SENSORS SENSOR=<name> ENABLE=[0|1]).

        Drives runout_helper.sensor_enabled unconditionally, regardless of its current live
        value: SET_FILAMENT_SENSOR (Mainsail) can already have changed the live flag without
        touching the persisted record, so a "only touch it if the live value differs" guard
        would see live already matching, do nothing, and never write the persisted record.
        Only the write itself is conditional, on whether the persisted dict actually changed.

        Returns True if the persisted record changed.
        """
        sensor = self.all_sensors_map[qualified_name]
        sensor.runout_helper.sensor_enabled = bool(enabled)
        return self._persist_enabled(qualified_name, enabled, write=write)


    def persist_sensor_enabled_change(self, runout_helper):
        """
        Called by MmuRunoutHelper.cmd_SET_FILAMENT_SENSOR so a live Mainsail toggle of a
        registered sensor is exactly as sticky as one made via MMU_SENSORS ENABLE= - otherwise a
        sensor disabled via MMU_SENSORS then re-enabled via Mainsail would silently revert back
        to disabled on the next restart.
        """
        qualified_name = self._helper_to_qualified_name.get(runout_helper)
        if qualified_name is not None:
            self._persist_enabled(qualified_name, runout_helper.sensor_enabled)


    def load_persisted_state(self):
        """
        Re-apply persisted per-sensor disables to the current all_sensors_map. Only ever assigns
        False - "enabled" is never persisted - so this can't clobber a live SET_FILAMENT_SENSOR
        toggle made after boot, and is safe to call more than once (klippy:ready and again from
        the MMU_RESET-style re-enable path). A persisted name that no longer resolves (config
        edit removed/renamed that sensor) is silently skipped.
        """
        persisted = self.mmu.var_manager.get(VARS_MMU_SENSOR_ENABLED, {})
        for qualified_name, enabled in persisted.items():
            sensor = self.all_sensors_map.get(qualified_name)
            if sensor is not None:
                sensor.runout_helper.sensor_enabled = bool(enabled)


    def check_sensor(self, name):
        """
        Return sensor state or None if unavailable/disabled.
        Accepts a generic or fully-qualified/gate-suffixed name (active_sensors_map is keyed
        by generic names for the active gate, so we normalize first).
        """
        sensor = self.active_sensors_map.get(self.get_generic_endstop_name(name), None)
        if sensor is not None and sensor.runout_helper.sensor_enabled:
            return bool(sensor.runout_helper.filament_present)
        return None


    def check_event_sensor(self, name, gate=None):
        """
        Return the current state of the exact sensor named by a queued event.

        Unlike check_sensor(), this resolves against the stable global registry rather
        than active_sensors_map. Event delivery may lag behind a gate or unit selection,
        so checking the active generic sensor can inspect a different physical switch.
        """
        mmu_unit = self.mmu.mmu_unit(gate) if gate is not None and gate >= 0 else None
        _qualified, sensor, _error = self.resolve_sensor(name, mmu_unit=mmu_unit)
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
        self._set_sensor_runout(True, gate)


    def disable_runout(self, gate):
        self._set_sensor_runout(False, gate)


    def _runout_sensors(self):
        """
        Sensors that follow the global "monitoring on/off" state: every per-gate sensor on
        the machine (an idle gate's runout still updates the gate map) plus the selected
        unit's own sensors, widening to all units when no unit is selected.

        Deliberately not the active sensor map, which is re-pointed on gate change: that
        would disarm one set and re-arm another, stranding sensors suspended for good.
        """
        sensors = [sensor for name, sensor in self.all_sensors_map.items()
                   if self.is_gate_sensor_name(name)]

        unit = self.mmu.unit_selected
        units = range(len(self.unit_sensors)) if unit is None else (unit,)
        for u in units:
            sensors += [sensor for name, sensor in self.unit_sensors[u].items()
                        if not self.is_gate_sensor_name(name)]
        return sensors


    def _set_sensor_runout(self, enable, gate):
        for sensor in self._runout_sensors():
            sensor.runout_helper.enable_runout(enable and gate >= 0)


    def suspend_sensor_events(self, suspend):
        """
        Suspend (or restore) insert/remove/runout gcode events on every active sensor.

        Needed for an operation that deliberately drives filament across a sensor, where the
        resulting event would start a second operation inside the one that caused it.
        Disabling runout does not cover it: that only gates the runout branch.

        Restores exactly the sensors it suspended, and counts nesting, so neither a gate
        change nor an inner block's exit can leave a sensor stranded either way.
        """
        if suspend:
            self._suspend_depth += 1
            if self._suspend_depth > 1:
                return # Already suspended by an enclosing block
            self._suspended_sensors = list(self.active_sensors_map.values())

        else:
            if self._suspend_depth == 0:
                return # Unbalanced restore
            self._suspend_depth -= 1
            if self._suspend_depth > 0:
                return # Still inside an enclosing block

        for sensor in self._suspended_sensors:
            sensor.runout_helper.suspend_events(suspend)

        if not suspend:
            self._suspended_sensors = []


    def _get_sensors(self, pos, gate, position_condition):
        """
        Common helper that defines sensors and relationship to filament_pos state for easy filament tracing.
        Note:
            Buffer based compression/tension sensor and encoder virtual sensor are excluded since they
            are not simple filament present or not switches
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


    def get_all_sensors_for_gate(self, gate):
        return self._get_sensors(-1, gate, lambda p, pc: pc is not None)


    def get_status(self, eventtime=None):
        return {
            name: bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None
            for name, sensor in self.active_sensors_map.items()
        }
