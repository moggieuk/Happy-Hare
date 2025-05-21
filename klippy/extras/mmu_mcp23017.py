# MCP23017 Extra
#
# Copyright (C) 2024  Ivan Dubrov <dubrov.ivan@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from . import bus, mmu_i2c_inputs

# Registers
REG_IODIR = 0x00
REG_IPOL = 0x02
REG_GPINTEN = 0x04
REG_DEFVAL = 0x06
REG_INTCON = 0x08
REG_IOCON = 0x0A
REG_GPPU = 0x0C
REG_INTF = 0x0E
REG_INTCAP = 0x10
REG_GPIO = 0x12
REG_OLAT = 0x14

class MmuMCP23017(mmu_i2c_inputs.MmuI2cInputs):
    def __init__(self, config):
        super().__init__(config)
        self._printer = config.get_printer()
        self._name = config.get_name().split()[1]
        self._i2c = bus.MCU_I2C_from_config(config, default_speed=400000)
        self._ppins = self._printer.lookup_object("pins")
        self._ppins.register_chip("mmu_mcp23017_" + self._name, self)
        self._mcu = self._i2c.get_mcu()
        self._mcu.register_config_callback(self._build_config)
        self._oid = self._i2c.get_oid()
        self._last_clock = 0
        # Set up registers default values
        self.reg_dict = {
            # FIXME: direction
            REG_IODIR: 0xFFFF,
            REG_IPOL: 0,
            REG_GPINTEN: 0,
            REG_DEFVAL: 0,
            # FIXME: interrup
            REG_INTCON: 0,
            # FIXME: pullup
            REG_GPPU: 0,
            REG_GPIO: 0,
        }
    def _build_config(self):
        # Reset the defalut configuration
        # MIRROR = 1, The INT pins are internally connected
        # ODR = 1, Configures the INT pin as an open-drain output
        self._mcu.add_config_cmd("i2c_write oid=%d data=%02x%02x" % (self._oid, REG_IOCON, 0x44))
        # Transfer all regs with their initial cached state
        for _reg, _data in self.reg_dict.items():
            self._mcu.add_config_cmd("i2c_write oid=%d data=%02x%02x%02x" % (
                self._oid, _reg, _data & 0xFF, (_data >> 8) & 0xFF), is_init=True)

    def setup_input_pin(self, pin_idx, pullup):
        if pullup == 1:
            self.set_bits_in_register(REG_GPPU, 1 << pin_idx)
        elif pullup == -1:
            raise self._ppins.error("Can not pulldown MCP23017 pins")
        self.set_bits_in_register(REG_IODIR, 1 << pin_idx)
        self.set_bits_in_register(REG_GPINTEN, 1 << pin_idx)
    def read_input_pins(self):
        params = self._i2c.i2c_read([REG_GPIO], 2)
        response = bytearray(params['response'])
        return (response[1] << 8) | response[0]
#     def setup_pin(self, pin_type, pin_params):
#         if pin_type == 'digital_out' and pin_params['pin'][0:4] == "PIN_":
#             return MCP23017_digital_out(self, pin_params)
#         raise pins.error("Wrong pin or incompatible type: %s with type %s! " % (
#             pin_params['pin'][0:4], pin_type))
#     def get_mcu(self):
#         return self._mcu
    def get_oid(self):
        return self._oid
    def clear_bits_in_register(self, reg, bitmask):
        if reg in self.reg_dict:
            self.reg_dict[reg] &= ~(bitmask)
    def set_bits_in_register(self, reg, bitmask):
        if reg in self.reg_dict:
            self.reg_dict[reg] |= bitmask
    def set_register(self, reg, value):
        if reg in self.reg_dict:
            self.reg_dict[reg] = value
    def send_register(self, reg, print_time):
        data = [reg & 0xFF, self.reg_dict[reg] & 0xFF, (self.reg_dict[reg] >> 8) & 0xFF]
        clock = self._mcu.print_time_to_clock(print_time)
        self._i2c.i2c_write(data, minclock=self._last_clock, reqclock=clock)
        self._last_clock = clock

# class MCP23017_digital_out(object):
#     def __init__(self, mcp23017, pin_params):
#         self._mcp23017 = mcp23017
#         self._mcu = mcp23017.get_mcu()
#         self._sxpin = int(pin_params['pin'].split('_')[1])
#         self._bitmask = 1 << self._sxpin
#         self._pin = pin_params['pin']
#         self._invert = pin_params['invert']
#         self._mcu.register_config_callback(self._build_config)
#         self._start_value = self._shutdown_value = self._invert
#         self._max_duration = 2.
#         self._set_cmd = self._clear_cmd = None
#         # Set direction to output
#         self._mcp23017.clear_bits_in_register(REG_DIR, self._bitmask)
#     def _build_config(self):
#         if self._max_duration:
#             raise pins.error("MCP23017 pins are not suitable for heaters")
#     def get_mcu(self):
#         return self._mcu
#     def setup_max_duration(self, max_duration):
#         self._max_duration = max_duration
#     def setup_start_value(self, start_value, shutdown_value):
#         self._start_value = (not not start_value) ^ self._invert
#         self._shutdown_value = self._invert
#         # We need to set the start value here so the register is
#         # updated before the MCP23017 class writes it.
#         if self._start_value:
#             self._mcp23017.set_bits_in_register(REG_DATA, self._bitmask)
#         else:
#             self._mcp23017.clear_bits_in_register(REG_DATA, self._bitmask)
#     def set_digital(self, print_time, value):
#         if int(value) ^ self._invert:
#             self._mcp23017.set_bits_in_register(REG_DATA, self._bitmask)
#         else:
#             self._mcp23017.clear_bits_in_register(REG_DATA, self._bitmask)
#         self._mcp23017.send_register(REG_DATA, print_time)
#     def set_pwm(self, print_time, value, cycle_time=None):
#         self.set_digital(print_time, value >= 0.5)

def load_config_prefix(config):
    return MmuMCP23017(config)
