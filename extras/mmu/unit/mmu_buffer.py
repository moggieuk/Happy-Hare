# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Easy setup of sync-feedback "buffer"
#
# sync feedback sensor(s):
#   Creates buttons handlers (with filament_switch_sensor for visibility and control) and publishes events based on state change
#   Named `sync_feedback_compression` & `sync_feedback_tension`
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time

# Happy Hare imports
from ..mmu_constants    import *
from ..mmu_sensor_utils import (
    MmuSensorFactory,
    MmuAdcHelper,
    MmuRunoutHelper,
    MmuSwitchSensor,
    MmuVirtualEndstopSensor,
    EVENT_GCODES
)


class MmuBuffer:

    def __init__(self, config, mmu_unit, params):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]

        self.connected_units = [mmu_unit]       # mmu_unit is just the first to load, not necessarily all

        register = bool(config.getint('register_buffer_sensors', 1, minval=0, maxval=1))
        event_delay = config.get('event_delay', 0)
        sf = MmuSensorFactory(self.printer)

        self.buffer_range = config.getfloat('buffer_range', 10.0, minval=0.)
        self.buffer_maxrange = config.getfloat('buffer_maxrange', 10.0, minval=0.)

        # Setup motor syncing feedback compression sensor for unit...
        switch_pin = config.get('compression_pin', None)
        self.compression_sensor = sf.create_mmu_sensor(
            config,
            f"{self.name}:{SENSOR_COMPRESSION}",
            None,
            switch_pin,
            event_delay=event_delay,
            button_handler=self.sync_compression_callback,
            register=register
        )

        # Setup motor syncing feedback tension sensor for unit...
        switch_pin = config.get('tension_pin', None)
        self.tension_sensor = sf.create_mmu_sensor(
            config,
            f"{self.name}:{SENSOR_TENSION}",
            None,
            switch_pin,
            event_delay=event_delay,
            button_handler=self.sync_tension_callback,
            register=register
        )

        # Setup analog (proportional) sync feedback
        # Uses single analog input; value scaled in [-1, 1]
        analog_pin = config.get('analog_pin', None)
        self.proportional_sensor = None
        if analog_pin:
            self.proportional_sensor = MmuProportionalSensor(
                config,
                f"{self.name}:{SENSOR_PROPORTIONAL}",
                virtual_compression_sensor = f"{self.name}:{SENSOR_COMPRESSION}" if self.compression_sensor is None else None,
                virtual_tension_sensor     = f"{self.name}:{SENSOR_TENSION}" if self.tension_sensor is None else None,
                register=register
            )

            if self.compression_sensor is None:
                self.compression_sensor = self.proportional_sensor.compression_vsensor

            if self.tension_sensor is None:
                self.tension_sensor = self.proportional_sensor.tension_vsensor

        # Klipper event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)


    def handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller      # Shared MMU controller class


    def add_unit(self, mmu_unit):
        self.connected_units.append(mmu_unit)


    def sync_tension_callback(self, eventtime, t_sensor_name, tension_state, runout_helper):
        """
        Button event handler for sync-feedback tension switch
        """
        c_sensor_name = t_sensor_name.replace(SENSOR_TENSION, SENSOR_COMPRESSION)
        compression_sensor = self.mmu.sensor_manager.get_sensor_obj(c_sensor_name)
        compression_enabled = compression_sensor.runout_helper.sensor_enabled if compression_sensor else False
        compression_state = compression_sensor.runout_helper.filament_present if compression_enabled else False

        if compression_enabled:
            event_value = 0 if tension_state == compression_state else (-1 if tension_state else 1) # {-1,0,1}
        else:
            event_value = -tension_state # {0,-1}

        # Send event now so it is processed as early as possible
        self.printer.send_event("mmu:sync_feedback", eventtime, event_value)


    def sync_compression_callback(self, eventtime, c_sensor_name, compression_state, runout_helper):
        """
        Button event handler for sync-feedback compression switch
        """
        t_sensor_name = c_sensor_name.replace(SENSOR_COMPRESSION, SENSOR_TENSION)
        tension_sensor = self.mmu.sensor_manager.get_sensor_obj(t_sensor_name)
        tension_enabled = tension_sensor.runout_helper.sensor_enabled if tension_sensor else False
        tension_state = tension_sensor.runout_helper.filament_present if tension_enabled else False

        if tension_enabled:
            event_value = 0 if compression_state == tension_state else (1 if compression_state else -1) # {-1,0,1}
        else:
            event_value = compression_state # {1,0}

        # Send event now so it is processed as early as possible
        self.printer.send_event("mmu:sync_feedback", eventtime, event_value)



# -----------------------------------------------------------------------------------------------------------
# Analog Filament Tension Sensor used for proportional sync-feedback
# Maps sensor range to [-1,1]
# -----------------------------------------------------------------------------------------------------------

class MmuProportionalSensor:

    def __init__(self, config, name,
        virtual_compression_sensor = False,
        virtual_tension_sensor = False,
        register=True
    ):

        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = name
        self._last_extreme = None

        # Virtual compression/tension sensors (if created)
        self.compression_vsensor = None
        self.tension_vsensor = None

        # Config
        self._pin       = config.get('analog_pin')
        max_tension     = config.getfloat('analog_max_tension', 1)
        max_compression = config.getfloat('analog_max_compression', 0)

        # Determine the actual raw min/max sensor values
        raw_min = min(max_tension, max_compression)
        raw_max = max(max_tension, max_compression)
        mid_point = (max_tension + max_compression) / 2.0

        self._neutral_point     = config.getfloat('analog_neutral_point', mid_point, minval=raw_min, maxval=raw_max)
        self._vsensor_threshold = config.getfloat('analog_sensor_threshold', 0.75, minval=0.5, maxval=1.0)
        self._gamma        = config.getfloat('analog_gamma', 1)
        self._sample_time  = config.getfloat('analog_sample_time', 0.005) # Not exposed
        self._sample_count = config.getint('analog_sample_count', 5)      # Not exposed
        self._report_time  = config.getfloat('analog_report_time', 0.100) # Not exposed
        self._vsensor_hysteresis = config.getfloat('analog_vsensor_hysteresis', 0.04) # Not exposed (4%)

        self._reversed = (max_compression < max_tension)
        eps = 1e-12
        if not self._reversed:
            # Tension low, Compression high value
            self._d_neg = max(self._neutral_point - max_tension, eps)
            self._d_pos = max(max_compression - self._neutral_point, eps)
        else:
            # Compression low, Tension high value
            self._d_pos = max(self._neutral_point - max_compression, eps)
            self._d_neg = max(max_tension - self._neutral_point, eps)

        # State
        self.value_raw = 0.0 # Raw ADC value
        self.value = 0.0     # In [-1.0, 1.0]

        # Virtual sensor setup
        h = abs(self._vsensor_threshold) * self._vsensor_hysteresis
        self._vsensor_threshold_low = self._vsensor_threshold - h
        self._vsensor_threshold_high = self._vsensor_threshold + h
        self._vsensor_state = None

        # Setup ADC
        ppins = self.printer.lookup_object('pins')
        self.mcu_adc = ppins.setup_pin('adc', self._pin)

        MmuAdcHelper.setup_adc_compat(
            self.mcu_adc,
            self._report_time,
            self._sample_time,
            self._sample_count,
            self._adc_callback,
        )

        self.runout_helper = MmuRunoutHelper(
            self.printer,
            self.name,
            gcodes={
                event: "%s SENSOR=%s" % (EVENT_GCODES[event], self.name)
                for event in ("clog", "tangle")
            },
            register=False,
        )

        # Create virtual compression sensor?
        if virtual_compression_sensor:
            self.compression_vsensor = MmuVirtualEndstopSensor(config, virtual_compression_sensor, None, register=register)

        # Create virtual tension sensor?
        if virtual_tension_sensor:
            self.tension_vsensor = MmuVirtualEndstopSensor(config, virtual_tension_sensor, None, register=register)
       
        # Expose status
        self.printer.add_object(self.name, self)
        logging.info("MMU: Created Proportional sync-feedback sensor %s]" % self.name)


    def _map_reading(self, v_raw):
        n = self._neutral_point
        v = float(v_raw)

        # Map around neutral_point into [-1, 1]
        if not self._reversed:
            if v >= n:
                y = (v - n) / self._d_pos
            else:
                y = -(n - v) / self._d_neg
        else:
            if v <= n:
                y = (n - v) / self._d_pos
            else:
                y = -(v - n) / self._d_neg

        # Optional shaping (gamma=1 => linear)
        if self._gamma != 1.0:
            y = (abs(y) ** self._gamma) * (1.0 if y >= 0 else -1.0)

        # Clamp
        if y < -1.0: y = -1.0
        if y >  1.0: y =  1.0
        return y


    def _adc_callback(self, *args):
        read_time, read_value = MmuAdcHelper.unpack_adc_callback(*args)
        self.value_raw = float(read_value)
        self.value = self._map_reading(read_value) # Mapped & scaled value

        prev_state = self._vsensor_state

        # Apply hysteresis before calling trigger and ititilize on first call
        if self._vsensor_state is None:
            if self.value > self._vsensor_threshold_high:
                self._vsensor_state = 1
            elif self.value < self._vsensor_threshold_low:
                self._vsensor_state = -1
            else:
                self._vsensor_state = 0
        elif self.value > self._vsensor_threshold_high:
            self._vsensor_state = 1
        elif self.value < self._vsensor_threshold_low:
            self._vsensor_state = -1
        elif self._vsensor_threshold_low <= self.value <= self._vsensor_threshold_high:
            self._vsensor_state = 0

        # Service virtual endstop sensors only if hysteresis state changes...
        if self._vsensor_state != prev_state:
            if self.compression_vsensor is not None:
                self.compression_vsensor.trigger_handler(read_time, self._vsensor_state > 0)
            if self.tension_vsensor is not None:
                self.tension_vsensor.trigger_handler(read_time, self._vsensor_state < 0)

        # Publish sync-feedback event immediately if extreme to match switch sensors
        # TODO perhaps this should use 'analog_sensor_threshold' to signify extreme or
        # TODO determined by is_extreme() in mmu_sync_feedback manager (with hysteresis)
        # TODO ...or do we even need this?
        if abs(self.value) >= 1.0:
            extreme = 1 if self.value > 0 else -1
            if extreme != self._last_extreme: # Avoid repeated events
                self._last_extreme = extreme
                self.printer.send_event("mmu:sync_feedback", read_time, self.value)


    def get_status(self, eventtime):
        return {
            "enabled":          bool(self.runout_helper.sensor_enabled),
            "value":            self.value,             # in [-1.0, 1.0] (mapped)
            "value_raw":        self.value_raw,         # raw
        }
