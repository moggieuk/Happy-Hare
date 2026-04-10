# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Encapsulation of toolhead physical dimensions and sensors so that it can be shared accross mmu units
#       or to allow of IDEX support where different mmu units connect to potentially different toolheads
#       Also optional implementation of a modified extruder stepper that has homing ability
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
from ..mmu_sensor_utils    import MmuSensorFactory
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

        # Setup 'extruder' & 'toolhead' sensors connected to this toolhead
        # Allows for visibility, enable/disable control & insert/runout on extruder sensor
        event_delay = config.get('event_delay', 0.5)
        self.sensor_factory = sf = MmuSensorFactory(self.printer)

        # Setup single extruder (entrance) sensor...
        switch_pin = config.get('extruder_switch_pin', None)
        self.extruder_sensor = sf.create_mmu_sensor(
            config,
            f"{self.name}:{SENSOR_EXTRUDER_ENTRY}",
            None,
            switch_pin,
            event_delay,
            insert=True,
            runout=True
        )

        # Setup single toolhead sensor...
        switch_pin = config.get('toolhead_switch_pin', None)
        self.toolhead_sensor = sf.create_mmu_sensor(
            config,
            f"{self.name}:{SENSOR_TOOLHEAD}",
            None,
            switch_pin,
            event_delay
        )

        # Register event handlers
        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)


    def add_unit(self, mmu_unit):
        self.connected_units.append(mmu_unit)


    def _handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller


    def _handle_ready(self):
        pass
