# Happy Hare MMU Software
#
# Easy setup of all filament sensors for mmu_unit
#
# Pre-gate sensors:
#   Simplifed filament switch sensor easy configuration of pre-gate sensors used to detect runout and insertion of filament
#   and preload into gate and update gate_map when possible to do so based on MMU state, not printer state
#   Essentially this uses the default `filament_switch_sensor` but then replaces the runout_helper
#   Each has name `mmu_pre_gate_X` where X is gate number
#
# mmu_gear sensor(s):
#   Wrapper around `filament_switch_sensor` setting up insert/runout callbacks with modified runout event handling
#   Named `mmu_gear`
#
# mmu_gate sensor(s):
#   Wrapper around `filament_switch_sensor` setting up insert/runout callbacks with modified runout event handling
#   Named `mmu_gate`
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time

class MmuSensors:

    def __init__(self, config, *args):
        from .mmu import Mmu # For sensor names

        if len(args) < 2:
            raise config.error("[%s] cannot be instantiated directly. It must be loaded by [mmu_unit]" % config.get_name())
        self.mmu_machine, self.mmu_unit, self.first_gate, self.num_gates = args
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()

        event_delay = config.get('event_delay', 0.5)
        sf = self.mmu_machine.sensor_factory

        # Setup "mmu_pre_gate" sensors...
        self.pre_gate_sensors = {}
        for i, gate in enumerate(range(self.first_gate, self.first_gate + self.num_gates)):
            switch_pin = config.get('pre_gate_switch_pin_%d' % i, None)
            self.pre_gate_sensors[gate] = sf.create_mmu_sensor(
                config,
                Mmu.SENSOR_PRE_GATE_PREFIX,
                gate,
                switch_pin,
                event_delay,
                insert=True,
                remove=True,
                runout=True,
                insert_remove_in_print=True
            )


        # Setup single "mmu_gate" sensor for unit...
        switch_pin = config.get('gate_switch_pin', None)
        self.gate_sensor = sf.create_mmu_sensor(
            config,
            "unit%d_%s" % (self.mmu_unit.unit_index, Mmu.SENSOR_GATE),
            None,
            switch_pin,
            event_delay,
            runout=True
        )

        # Setup "mmu_gear" sensors...
        self.post_gear_sensors = {}
        for i, gate in enumerate(range(self.first_gate, self.first_gate + self.num_gates)):
            switch_pin = config.get('post_gear_switch_pin_%d' % i, None)

            # EXPERIMENT/HACK to support ViViD analog buffer "endstops"
            a_range = config.getfloatlist('post_gear_analog_range_%d' % gate, None, count=2)
            if a_range is not None: # PAUL TEST ME
                a_pullup = config.getfloat('post_gear_analog_pullup_resister_%d' % gate, 4700.)
                self.post_gear_sensors[gate] = MmuAdcSwitchSensor(
                    config,
                    Mmu.SENSOR_GEAR_PREFIX,
                    gate,
                    switch_pin,
                    event_delay,
                    a_range,
                    runout=True,
                    a_pullup=a_pullup)
            else:
                self.post_gear_sensors[gate] = sf.create_mmu_sensor(
                    config,
                    Mmu.SENSOR_GEAR_PREFIX,
                    gate,
                    switch_pin,
                    event_delay,
                    runout=True
                )


def load_config_prefix(config):
    return MmuSensors(config)
