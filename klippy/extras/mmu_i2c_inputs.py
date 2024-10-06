# Simulated I2C Inputs support
#
# Copyright (C) 2024  Ivan Dubrov <dubrov.ivan@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import pins
from . import bus

# Used to ignore acks from the buttons code
class MmuI2cInputsSendAck():
    def send(self, data=(), minclock=0, reqclock=0):
        return

class MmuI2cInputs(object):
    def __init__(self, config):
        self._printer = config.get_printer()
        name = config.get_name().split()[1]
        i2c = bus.MCU_I2C_from_config(config, default_speed=400000)
        mcu = i2c.get_mcu()
        mcu.register_config_callback(self._build_inputs_config)
        # Input pins support
        self._init_cmds = []
        self._restart_cmds = []
        self._config_cmds = []
        self._config_callbacks = []
        self._oid_to_pins = []
        self._oid_to_callbacks = []
        self._ack_count = 0

        # Interrupt pin triggers button events
        interrupt_pin = config.get('interrupt_pin', None)
        if interrupt_pin is not None:
            buttons = self._printer.load_object(config, 'buttons')
            buttons.register_button_push(interrupt_pin, self._send_buttons_state)
        self._printer.register_event_handler("klippy:ready", self._ready)
    def _ready(self):
        self._send_buttons_state(self._printer.get_reactor().monotonic())
    def _build_inputs_config(self):
        # Build config commands
        for cb in self._config_callbacks:
            cb()

        # Interpret the commands to handle the buttons
        for cmdlist in (self._config_cmds, self._init_cmds):
            for cmd in cmdlist:
                self._interpret_init_cmd(cmd)
    def add_config_cmd(self, cmd, is_init=False, on_restart=False):
        if is_init:
            self._init_cmds.append(cmd)
        elif on_restart:
            self._restart_cmds.append(cmd)
        else:
            self._config_cmds.append(cmd)
    def _interpret_init_cmd(self, cmd):
        parts = cmd.split()
        name = parts[0]
        argparts = dict(arg.split('=', 1) for arg in parts[1:])
        if name == "config_buttons":
            # Setup button pos to pin mapping
            oid = int(argparts['oid'])
            count = int(argparts['button_count'])
            self._oid_to_pins[oid] = [-1] * count
        elif name == "buttons_add":
            # Setup pin in the button pos to pin mapping
            oid = int(argparts['oid'])
            pos = int(argparts['pos'])
            pull_up = int(argparts['pull_up'])
            if argparts['pin'][0:4] != "PIN_":
                raise pins.error("Wrong pin: %s!" % (argparts['pin'][0:4]))
            pin = int(argparts['pin'][4:])
            self._oid_to_pins[oid][pos] = pin
            self.setup_input_pin(pin, pull_up)
        elif name == "buttons_query":
            # FIXME: What should we do here?
            pass
        else:
            raise pins.error("Command is not supported by I2C inputs: %s" % name)
    def register_config_callback(self, cb):
        self._config_callbacks.append(cb)
    def create_oid(self):
        self._oid_to_pins.append([])
        self._oid_to_callbacks.append(None)
        return len(self._oid_to_pins) - 1
    def alloc_command_queue(self):
        return None
    def get_query_slot(self, oid):
        return 0
    def seconds_to_clock(self, time):
        return 0
    def register_response(self, callback, name, oid):
        if name != "buttons_state":
            raise pins.error("I2C inputs only supports buttons_state response callback")
        self._oid_to_callbacks[oid] = callback
    def lookup_command(self, msgformat, cq=None):
        parts = msgformat.split()
        name = parts[0]
        if name != "buttons_ack":
            raise pins.error("I2C inputs does not support '%s' command" % msgformat)
        return MmuI2cInputsSendAck()
    def setup_input_pin(self, pin_idx, pullup):
        # Must be overridden to handle setup of the I2C input pin
        pass
    def read_input_pins(self):
        return 0
    def _send_buttons_state(self, eventtime):
        pins = self.read_input_pins()

        # Send updated buttons state through the callbacks
        self._ack_count = (self._ack_count + 1) & 0xff
        for oid, cb in enumerate(self._oid_to_callbacks):
            state = 0
            for i, pin_idx in enumerate(self._oid_to_pins[oid]):
                state |= ((pins >> pin_idx) & 1) << i

            if cb is not None:
                params = {
                    'ack_count': self._ack_count,
                    'oid': oid,
                    'state': [state],
                    '#receive_time': eventtime
                }
                cb(params)
