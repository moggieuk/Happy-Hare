# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Easy setup of all filament sensors for mmu_unit
#
# Pre-gate sensors:
#   Simplifed filament switch sensor easy configuration of mmu entry sensors used to detect runout and insertion of filament
#   and preload into gate and update gate_map when possible to do so based on MMU state, not printer state
#   Essentially this uses the default `filament_switch_sensor` but then replaces the runout_helper
#   Each has name `mmu_entry_X` where X is gate number
#
# mmu_exit sensor(s):
#   Wrapper around `filament_switch_sensor` setting up insert/runout callbacks with modified runout event handling
#   Named `mmu_exit`
#
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
from ..mmu_sensor_utils import MmuSensorFactory, MmuVirtualEndstopSensor


class MmuSensors:

    def __init__(self, config, mmu_unit, params):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]

        register = bool(config.getint('register_mmu_sensors', 1, minval=0, maxval=1))
        event_delay = config.get('event_delay', 0.5)
        first_gate = mmu_unit.first_gate
        num_gates = mmu_unit.num_gates

        self.sensor_factory = sf = MmuSensorFactory(self.printer)

        # Setup "mmu_entry" sensors...
        self.entry_sensors = {}
        for i, gate in enumerate(range(first_gate, first_gate + num_gates)):
            switch_pin = config.get('mmu_entry_switch_pin_%d' % i, None)
            self.entry_sensors[gate] = sf.create_mmu_sensor(
                config,
                SENSOR_ENTRY_PREFIX,
                gate,
                switch_pin,
                event_delay=event_delay,
                events=('insert', 'remove', 'runout'),
                insert_remove_in_print=True,
                register=register
            )


        # Setup single "mmu_shared_exit" sensor for unit...
        switch_pin = config.get('mmu_shared_exit_switch_pin', None)
        self.shared_exit_sensor = sf.create_mmu_sensor(
            config,
            f"{self.mmu_unit.name}:{SENSOR_SHARED_EXIT}",
            None,
            switch_pin,
            event_delay=event_delay,
            events=('runout'),
            register=register
        )

        # Setup "mmu_exit" sensors...
        self.exit_sensors = {}
        for i, gate in enumerate(range(first_gate, first_gate + num_gates)):
            switch_pin = config.get('mmu_exit_switch_pin_%d' % i, None)

             # Support early BTT ViViD analog (hall-effect) buffer
            a_range = config.getfloatlist('mmu_exit_analog_range_%d' % gate, None, count=2)
            if switch_pin and a_range is not None:
                a_pullup = config.getfloat('mmu_exist_analog_pullup_resister_%d' % gate, 4700.)
                self.exit_sensors[gate] = MmuAdcSwitchSensor(
                    config,
                    SENSOR_EXIT_PREFIX,
                    gate,
                    switch_pin,
                    event_delay,
                    a_range,
                    events=('runout'),
                    a_pullup=a_pullup,
                    register=register
                )
                continue

            self.exit_sensors[gate] = sf.create_mmu_sensor(
                config, SENSOR_EXIT_PREFIX, gate, switch_pin,
                event_delay=event_delay,
                events=('runout'),
                register=register
            )



# -----------------------------------------------------------------------------------------------------------
# EXPERIMENTAL
# Support ViViD analog buffer "endstops"
# This class implments both the filament switch sensor and endstop. However:
#  * it will not display in UI because no filament_switch_sensor exists in config
#  * does not involve the mcu in the homing process so it can't be accurate
#  * suffers from inherent averaging lag for analog inputs
# Currently only parsed as option for 'mmu_exit' sensors (where clog/tangle flags must be False)
# -----------------------------------------------------------------------------------------------------------

class MmuAdcSwitchSensor(MmuVirtualEndstopSensor):

    def __init__(self, config, name_prefix, gate, switch_pin, event_delay, a_range,
        events=(),
        insert_remove_in_print=False, button_handler=None,
        a_pullup=4700.,
        register=True,
    ):

        super().__init__(
            config, name_prefix, gate,
            event_delay=event_delay,
            events=events,
            insert_remove_in_print=insert_remove_in_print,
            button_handler=button_handler,
            register=register,
        )

        self._pin = switch_pin
        buttons = self.printer.load_object(config, 'buttons')
        a_min, a_max = a_range
        buttons.register_adc_button(switch_pin, a_min, a_max, a_pullup, self.trigger_handler)

        logging.info("MMU: Created MmuAdcSwitchSensor(%s)" % self.name)
