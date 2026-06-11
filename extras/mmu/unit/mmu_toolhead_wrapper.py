# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Encapsulation of toolhead physical dimensions and sensors so that it can be shared accross mmu units
#       or to allow of IDEX support where different mmu units connect to potentially different toolheads
#       Also optional implementation of a modified extruder stepper that has homing ability
#
# Note: Currently only a single toolhead named "default" is supported by installer but this wrapper remains
#       per-unit to provide future flexibility
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging
from typing                import Sequence

# Happy Hare imports
from ..mmu_constants       import *
from ..mmu_sensor_utils    import MmuSensorFactory, MmuVirtualSensor
from ..mmu_base_parameters import TunableParametersBase, ParamSpec


# -----------------------------------------------------------------------------------------------------------
# Parameters for printer toolhead wrapper
# -----------------------------------------------------------------------------------------------------------

class MmuToolheadParameters(TunableParametersBase):

    def _guard_has_sensor(sensor):
        return lambda self: self._mmu_unit.mmu.sensor_manager.has_sensor(sensor)

    _SPECS: Sequence[ParamSpec] = (
        # Configuration for extruder dimensions
        ParamSpec('toolhead_extruder_to_nozzle', 'float',  0.0, section="TOOLHEAD", limits=dict(minval=5.0)),
        ParamSpec('toolhead_sensor_to_nozzle',   'float',  0.0, section="TOOLHEAD", limits=dict(minval=1.0), guard=_guard_has_sensor(SENSOR_TOOLHEAD)),
        ParamSpec('toolhead_entry_to_extruder',  'float',  0.0, section="TOOLHEAD", limits=dict(minval=0.0), guard=_guard_has_sensor(SENSOR_EXTRUDER_ENTRY)),
        ParamSpec('toolhead_residual_filament',  'float',  0.0, section="TOOLHEAD", limits=dict(minval=0.0,  maxval=50.0)),
        ParamSpec('toolhead_ooze_reduction',     'float',  0.0, section="TOOLHEAD", limits=dict(minval=-5.0, maxval=20.0)),
    )

    def __init__(self, config, mmu_unit):
        self._mmu_unit = mmu_unit
        super().__init__(config)



# -----------------------------------------------------------------------------------------------------------
# Metadata wrapper for printer toolhead
# -----------------------------------------------------------------------------------------------------------

class MmuToolheadWrapper():

    PARAMS_CLS = MmuToolheadParameters

    def __init__(self, config, mmu_unit):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()

        self.p = self.PARAMS_CLS(config, self)  # mmu_toolhead has own params

        self.connected_units = [mmu_unit]       # mmu_unit is just the first to load, not necessarily all
        self.sensors = {}

        # Setup 'extruder' & 'toolhead' sensors connected to this toolhead
        # Allows for visibility, enable/disable control & insert/runout on extruder sensor
        event_delay = config.get('event_delay', 0.5)
        self.sensor_factory = sf = MmuSensorFactory(self.printer)

        # --------------------------------------------
        # Setup single extruder (entrance) sensor
        # --------------------------------------------
        switch_pin = config.get('extruder_switch_pin', None)
        sensor = sf.create_mmu_sensor(
            config,
            f"{self.name}:{SENSOR_EXTRUDER_ENTRY}",
            None,
            switch_pin,
            event_delay=event_delay,
            insert=True,
            runout=True
        )
        if sensor is not None:
            self.sensors[SENSOR_EXTRUDER_ENTRY] = sensor

        # --------------------------------------------
        # Setup single toolhead sensor
        # --------------------------------------------
        switch_pin = config.get('toolhead_switch_pin', None)
        sensor = sf.create_mmu_sensor(
            config,
            f"{self.name}:{SENSOR_TOOLHEAD}",
            None,
            switch_pin,
            event_delay=event_delay
        )
        if sensor is not None:
            self.sensors[SENSOR_TOOLHEAD] = sensor

        # --------------------------------------------
        # For Qidi printers or any other that use a
        # hall_filament_width_sensor allow it to act as
        # either an extruder entry or toolhead sensor
        # (or additional sensor)
        # --------------------------------------------
        hall_sensor_endstop = config.get('hall_sensor_endstop', None)
        if hall_sensor_endstop is not None:
            if hall_sensor_endstop == 'extruder':
                target_name = SENSOR_EXTRUDER_ENTRY
            elif hall_sensor_endstop == 'toolhead':
                target_name = SENSOR_TOOLHEAD
            else:
                target_name = hall_sensor_endstop

            self.hall_pin1 = config.get('hall_adc1')
            self.hall_pin2 = config.get('hall_adc2')
            self.hall_dia1 = config.getfloat('hall_cal_dia1', 1.5)
            self.hall_dia2 = config.getfloat('hall_cal_dia2', 2.0)
            self.hall_rawdia1 = config.getint('hall_raw_dia1', 9500)
            self.hall_rawdia2 = config.getint('hall_raw_dia2', 10500)
            self.hall_runout_dia = config.getfloat('hall_min_diameter', 1.0)
            # self.hall_runout_dia_max = config.getfloat('hall_max_diameter', 2.0) - Unused for trigger

            s = MmuHallEndstop(config, target_name, self.hall_pin1, self.hall_pin2,
                               self.hall_dia1, self.hall_rawdia1, self.hall_dia2, self.hall_rawdia2,
                               hall_runout_dia=self.hall_runout_dia,
                               events=('insert', 'runout'))

            # Likley overriding entry or toolhead sensor but can also define a brand new toolhead sensor
            self.sensors[target_name] = s


        # Register event handlers
        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)


    def add_unit(self, mmu_unit):
        self.connected_units.append(mmu_unit)


    def _handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller


    def _handle_ready(self):
        pass



# -----------------------------------------------------------------------------------------------------------
# EXPERIMENTAL
# Standalone Hall Filament Sensor Endstop using Multi-Use Pins
# Can coexists with standard Klipper hall_filament_width_sensor by sharing the ADC pins
# If configured this replaces the typical physical switches for extruder/toolhead sensors
# -----------------------------------------------------------------------------------------------------------

class MmuHallEndstop(MmuVirtualSensor):

    def __init__(
        self, config, name, pin1, pin2,
        cal_dia1, raw_dia1, cal_dia2, raw_dia2,
        hall_runout_dia=1.,
        events=(),
    ):

        super().__init__(config, name, None,
            event_delay=0.5,
            events=events,
            insert_remove_in_print=False,
            button_handler=None,
            register=True,
        )

        # Configurable sampling for fast endstop response
        # Defaults: 1ms sample, 8 samples = 8ms. Report every 10ms.
        self.sample_time = config.getfloat('hall_sample_time', 0.001, above=0.0)
        self.sample_count = config.getint('hall_sample_count', 8, minval=1)
        self.report_time = config.getfloat('hall_report_time', 0.010, above=0.0)

        # Sensor configuration for diameter calculation
        self.pin1_name = pin1
        self.pin2_name = pin2
        self.dia1 = cal_dia1
        self.rawdia1 = raw_dia1
        self.dia2 = cal_dia2
        self.rawdia2 = raw_dia2
        self.hall_min_diameter = hall_runout_dia

        # State
        self.lastFilamentWidthReading = 0
        self.lastFilamentWidthReading2 = 0
        self.diameter = 0

        # Setup Hardware (Multi-Use)
        ppins = self.printer.lookup_object('pins')

        # ADC 1
        self.mcu_adc = None
        if self.pin1_name:
            ppins.allow_multi_use_pin(self.pin1_name)
            self.mcu_adc = ppins.setup_pin('adc', self.pin1_name)
            MmuAdcHelper.setup_adc_compat(
                self.mcu_adc,
                self.report_time,
                self.sample_time,
                self.sample_count,
                self._adc_callback,
            )

        # ADC 2 (Optional)
        self.mcu_adc2 = None
        if self.pin2_name:
            ppins.allow_multi_use_pin(self.pin2_name)
            self.mcu_adc2 = ppins.setup_pin('adc', self.pin2_name)
            MmuAdcHelper.setup_adc_compat(
                self.mcu_adc2,
                self.report_time,
                self.sample_time,
                self.sample_count,
                self._adc2_callback,
            )

        logging.info("MMU: Created [mmu_hall_endstop %s]" % name)


    def _calc_diameter(self):
        # Duplicate of Klipper hall_filament_width_sensor logic
        try:
            val_sum = self.lastFilamentWidthReading + self.lastFilamentWidthReading2
            slope = (self.dia2 - self.dia1) / (self.rawdia2 - self.rawdia1)
            diameter_new = round(slope * (val_sum - self.rawdia1) + self.dia1, 2)
            # Use same smoothing factor as Klipper? Or faster for endstop?
            # Klipper: self.diameter = (5.0 * self.diameter + diameter_new) / 6
            # For endstop we probably want instant reaction or less smoothing
            self.diameter = (2.0 * self.diameter + diameter_new) / 3 # Slightly faster smoothing
        except ZeroDivisionError:
            self.diameter = 1.75 # Default fallback


    def _adc_callback(self, *args):
        read_time, read_value = MmuAdcHelper.unpack_adc_callback(*args)
        self.lastFilamentWidthReading = round(read_value * 10000)
        self._update_from_adc(read_time)


    def _adc2_callback(self, *args):
        read_time, read_value = MmuAdcHelper.unpack_adc_callback(*args)
        self.lastFilamentWidthReading2 = round(read_value * 10000)
        self._update_from_adc(read_time)


    def _update_from_adc(self, eventtime):
        self._calc_diameter()
        is_present = self.diameter > self.hall_min_diameter
        self.note_filament_present(eventtime, is_present)


    def get_status(self, eventtime):
        status = self.runout_helper.get_status(eventtime)
        status.update({
            "Diameter": self.diameter,
            "Raw": self.lastFilamentWidthReading + self.lastFilamentWidthReading2,
        })
        return status
