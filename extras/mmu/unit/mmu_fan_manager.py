# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manage optional shared or per-gate fan_generic objects.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


FAN_OFF = 0
FAN_ON = 1
FAN_AUTO = 2

FAN_SOURCE_ENVIRONMENT = "environment"
FAN_SOURCE_MCU = "mcu"
FAN_SOURCE_DEFAULT = "default"
FAN_TEMPERATURE_SOURCES = (
    FAN_SOURCE_ENVIRONMENT,
    FAN_SOURCE_MCU,
)

FAN_STATE_NAMES = {
    FAN_OFF: "OFF",
    FAN_ON: "ON",
    FAN_AUTO: "AUTO",
}


class MmuFanManager:

    def __init__(self, config, mmu_unit, params):
        self.config = config
        self.mmu_unit = mmu_unit
        self.mmu_machine = mmu_unit.mmu_machine
        self.p = params
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.mmu = None

        # A per-gate list deliberately retains blank entries so fan index and
        # local gate index remain identical. A shared fan is a one-item list.
        self.fans = list(mmu_unit.fans) if mmu_unit.fans else ([mmu_unit.fan] if mmu_unit.fan else [])
        self._timer = self.reactor.register_timer(self._check_fans, self.reactor.NEVER)
        self._enabled = False
        self._modes = []
        # Unit parameters seed independent runtime thresholds for each fan.
        self._on_temps = []
        self._off_temps = []
        self._temperature_sources = []

        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.reinit()

    def _handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller

    def _handle_ready(self):
        self.refresh()

    def reinit(self):
        self._enabled = bool(self.p.fan_control_enabled)
        self._modes = [self.p.fan_forced] * len(self.fans)
        self._on_temps = [self.p.default_fan_on_temp] * len(self.fans)
        self._off_temps = [self.p.default_fan_off_temp] * len(self.fans)
        self._temperature_sources = [self.p.default_fan_temperature_source] * len(self.fans)
        if self.mmu is not None:
            self.refresh()

    def has_fans(self):
        return any(self.fans)

    def has_per_gate_fans(self):
        return bool(self.mmu_unit.fans)

    def is_enabled(self):
        return self._enabled

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.p.fan_control_enabled = int(self._enabled)
        if not self._enabled:
            self._set_all_speeds(0.)
        self.refresh()

    def set_mode(self, mode, gates=None):
        if mode not in FAN_STATE_NAMES:
            raise ValueError("Invalid fan mode: %s" % mode)

        indexes = self._indexes_for_gates(gates)
        for index in indexes:
            self._modes[index] = mode

        # Apply explicit ON/OFF immediately. AUTO is also evaluated immediately
        # so returning a hot fan to AUTO never waits for the next polling period.
        self._apply(indexes, self.reactor.monotonic())
        self.refresh()

    def set_thresholds(self, on_temp=None, off_temp=None, gates=None):
        indexes = self._indexes_for_gates(gates)
        updates = []
        for index in indexes:
            effective_on = self._on_temps[index] if on_temp is None else float(on_temp)
            effective_off = self._off_temps[index] if off_temp is None else float(off_temp)
            if effective_on < effective_off:
                gate = self.mmu_unit.first_gate + index
                prefix = "Gate %d: " % gate if self.has_per_gate_fans() else ""
                raise ValueError(prefix + "ON_TEMP must be greater than or equal to OFF_TEMP")
            updates.append((index, effective_on, effective_off))

        for index, effective_on, effective_off in updates:
            self._on_temps[index] = effective_on
            self._off_temps[index] = effective_off
        self.refresh()

    def set_temperature_source(self, source, gates=None):
        if source == FAN_SOURCE_DEFAULT:
            source = self.p.default_fan_temperature_source
        if source not in FAN_TEMPERATURE_SOURCES:
            raise ValueError("Invalid fan temperature source: %s" % (source or "none"))

        indexes = self._indexes_for_gates(gates)
        updates = []
        for index in indexes:
            if not self.fans[index]:
                if gates is not None:
                    gate = self.mmu_unit.first_gate + index
                    raise ValueError("No fan configured for gate %d on %s" % (
                        gate, self.mmu_unit.name))
                continue
            if not self._temperature_source_exists(source, index):
                if self.has_per_gate_fans():
                    target = "gate %d on %s" % (
                        self.mmu_unit.first_gate + index, self.mmu_unit.name)
                else:
                    target = self.mmu_unit.name
                raise ValueError("Temperature source '%s' is not available for %s" % (
                    source, target))
            updates.append(index)

        for index in updates:
            self._temperature_sources[index] = source
        self.refresh()

    def reset_thresholds(self):
        self._on_temps = [self.p.default_fan_on_temp] * len(self.fans)
        self._off_temps = [self.p.default_fan_off_temp] * len(self.fans)
        self.refresh()

    def reset_temperature_sources(self):
        self._temperature_sources = [self.p.default_fan_temperature_source] * len(self.fans)
        self.refresh()

    def refresh(self):
        if not self.has_fans() or not self._enabled:
            self.reactor.update_timer(self._timer, self.reactor.NEVER)
            return
        self.reactor.update_timer(self._timer, self.reactor.monotonic())

    def get_snapshot(self, gates=None, eventtime=None):
        eventtime = self.reactor.monotonic() if eventtime is None else eventtime
        snapshot = []
        for index in self._indexes_for_gates(gates):
            fan_name = self.fans[index]
            if not fan_name:
                continue
            fan_obj = self.printer.lookup_object(fan_name, None)
            status = fan_obj.get_status(eventtime) if fan_obj is not None else {}
            temps = self._get_temperatures(index, eventtime)
            snapshot.append({
                'gate': self.mmu_unit.first_gate + index if self.has_per_gate_fans() else None,
                'name': self._short_name(fan_name),
                'mode': FAN_STATE_NAMES[self._modes[index]],
                'speed': status.get('speed', 0.),
                'temperature': max(temps) if temps else None,
                'source': self._temperature_sources[index],
                'on_temp': self._on_temps[index],
                'off_temp': self._off_temps[index],
            })
        return snapshot

    def _check_fans(self, eventtime):
        if not self._enabled or not self.has_fans():
            return self.reactor.NEVER
        self._apply(range(len(self.fans)), eventtime)
        return eventtime + self.p.fan_polling_time

    def _apply(self, indexes, eventtime):
        for index in indexes:
            fan_name = self.fans[index]
            if not fan_name:
                continue

            mode = self._modes[index]
            if mode == FAN_OFF:
                speed = 0.
            elif mode == FAN_ON:
                speed = 1.
            elif not self._enabled:
                speed = 0.
            else:
                temperatures = self._get_temperatures(index, eventtime)
                if not temperatures:
                    speed = 0.
                else:
                    current_speed = self._get_speed(fan_name, eventtime)
                    temperature = max(temperatures)
                    if current_speed > 0.:
                        speed = 1. if temperature > self._off_temps[index] else 0.
                    else:
                        speed = 1. if temperature >= self._on_temps[index] else 0.
            self._set_speed(fan_name, speed)

    def _get_temperatures(self, fan_index, eventtime):
        source = self._temperature_sources[fan_index]
        sensor_names = self._temperature_sensor_names(source, fan_index)

        temperatures = []
        for sensor_name in sensor_names:
            if not sensor_name:
                continue
            sensor = self.printer.lookup_object(sensor_name, None)
            if sensor is None:
                continue
            temperature = sensor.get_status(eventtime).get('temperature')
            if temperature is not None:
                temperatures.append(float(temperature))
        return temperatures

    def _temperature_sensor_names(self, source, fan_index):
        if source == FAN_SOURCE_ENVIRONMENT:
            return self._associated_object_names(
                self.mmu_unit.environment_sensor,
                self.mmu_unit.environment_sensors,
                fan_index)
        if source == FAN_SOURCE_MCU:
            return self._mcu_temperature_sensor_names(fan_index)
        return []

    def _temperature_source_exists(self, source, fan_index):
        return any(
            sensor_name and self.printer.lookup_object(sensor_name, None) is not None
            for sensor_name in self._temperature_sensor_names(source, fan_index))

    def _associated_object_names(self, shared_name, per_gate_names, fan_index):
        if self.has_per_gate_fans():
            if per_gate_names:
                return [per_gate_names[fan_index]]
            return [shared_name] if shared_name else []
        if shared_name:
            return [shared_name]
        return list(per_gate_names)

    def _mcu_temperature_sensor_names(self, fan_index):
        unit_name = self.mmu_unit.name
        if self.has_per_gate_fans():
            suffix = "%s_mcu%d" % (unit_name, fan_index)
            return ["temperature_sensor _" + suffix,
                    "temperature_sensor " + suffix,
                    "temperature_sensor %s_mcu" % unit_name]

        names = ["temperature_sensor %s_mcu" % unit_name]
        for index in range(self.mmu_unit.num_gates):
            suffix = "%s_mcu%d" % (unit_name, index)
            names.extend(["temperature_sensor _" + suffix,
                          "temperature_sensor " + suffix])
        return names

    def _indexes_for_gates(self, gates):
        if not self.has_per_gate_fans():
            return list(range(len(self.fans)))
        if gates is None:
            return list(range(len(self.fans)))
        return [gate - self.mmu_unit.first_gate for gate in gates]

    def _get_speed(self, fan_name, eventtime):
        fan_obj = self.printer.lookup_object(fan_name, None)
        if fan_obj is None:
            return 0.
        return float(fan_obj.get_status(eventtime).get('speed', 0.))

    def _set_speed(self, fan_name, speed):
        fan_obj = self.printer.lookup_object(fan_name, None)
        if fan_obj is None:
            return
        fan_obj.fan.set_speed_from_command(float(speed))

    def _set_all_speeds(self, speed):
        for fan_name in self.fans:
            if fan_name:
                self._set_speed(fan_name, speed)

    @staticmethod
    def _short_name(name):
        return name.split(' ', 1)[-1]
