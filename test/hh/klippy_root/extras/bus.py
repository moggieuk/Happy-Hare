# Fake Klipper `klippy/extras/bus.py` for the Happy Hare test harness.
#
# Used by extras/mmu/unit/nfc/reader_factory.py:89,104 to build the NFC reader
# transports: MCU_SPI_from_config (RC522) and MCU_I2C_from_config (PN532/PN7160).
#
# Note reader_factory passes a `BusDefaultConfig` WRAPPER (reader_factory.py:34-51),
# not a real ConfigWrapper - it __getattr__-forwards, so only get/getint/getfloat/
# get_name/error may be relied on here.
#
# Methods the drivers actually invoke: spi_send, spi_transfer (pn532_driver.py,
# rc522_driver.py); i2c_write, i2c_read, i2c_transfer_cmd.send, get_i2c_address
# (pn7160_driver.py:195,297-313).
#
# Responses are SCRIPTED: a test loads a deque of canned byte replies and the real
# driver code runs against them. When the script runs dry we raise ScriptExhausted
# carrying the transcript so far - that error message is how you capture a boot
# transcript for a new chip the first time, which then gets committed as a fixture.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from collections import deque


class ScriptExhausted(AssertionError):
    pass


class _BusRecorder:
    def __init__(self, label):
        self.label = label
        self.transcript = []        # [(op, payload), ...] every call, in order
        self.script = deque()       # canned responses; tests populate this

    def _next(self, op, payload):
        self.transcript.append((op, payload))
        if not self.script:
            raise ScriptExhausted(
                "%s: %s ran out of scripted responses after %d call(s).\n"
                "Transcript so far (commit this as a fixture):\n%r"
                % (self.label, op, len(self.transcript), self.transcript))
        return self.script.popleft()


class MCU_SPI(_BusRecorder):
    def __init__(self, mcu, bus=None, pin=None, mode=0, speed=1000000, sw_pins=None,
                 cs_active_high=False):
        _BusRecorder.__init__(self, 'spi[%s]' % (pin,))
        self._mcu = mcu
        self.bus, self.pin, self.mode, self.speed = bus, pin, mode, speed

    def get_mcu(self):
        return self._mcu

    def get_oid(self):
        return 0

    def get_command_queue(self):
        return None

    def spi_send(self, data, minclock=0, reqclock=0):
        self.transcript.append(('spi_send', list(data)))

    def spi_transfer(self, data, minclock=0, reqclock=0):
        return {'response': self._next('spi_transfer', list(data))}

    def spi_transfer_with_preface(self, preface_data, data, minclock=0, reqclock=0):
        self.transcript.append(('spi_preface', list(preface_data)))
        return self.spi_transfer(data, minclock, reqclock)


class MCU_I2C(_BusRecorder):
    def __init__(self, mcu, bus=None, addr=0, speed=100000, sw_pins=None):
        _BusRecorder.__init__(self, 'i2c[0x%02x]' % (addr,))
        self._mcu = mcu
        self.bus, self.addr, self.speed = bus, addr, speed
        self.i2c_transfer_cmd = _I2CTransferCmd(self)

    def get_mcu(self):
        return self._mcu

    def get_oid(self):
        return 0

    def get_i2c_address(self):
        return self.addr

    def get_command_queue(self):
        return None

    def i2c_write(self, data, minclock=0, reqclock=0):
        self.transcript.append(('i2c_write', list(data)))

    def i2c_read(self, write, read_len):
        return {'response': self._next('i2c_read', (list(write), read_len))}


class _I2CTransferCmd:
    """pn7160_driver.py calls `i2c_transfer_cmd.send([oid, data])`."""

    def __init__(self, owner):
        self._owner = owner

    def send(self, args, minclock=0, reqclock=0):
        return {'response': self._owner._next('i2c_transfer', list(args))}


def MCU_SPI_from_config(config, mode, pin_option="cs_pin", default_speed=100000,
                        share_type=None, cs_active_high=False):
    printer = config.get_printer()
    mcu = printer.lookup_object('mcu')
    return MCU_SPI(mcu, bus=config.get('spi_bus', None),
                   pin=config.get(pin_option, None), mode=mode,
                   speed=config.getint('spi_speed', default_speed))


def MCU_I2C_from_config(config, default_addr=None, default_speed=100000,
                        share_type=None):
    printer = config.get_printer()
    mcu = printer.lookup_object('mcu')
    if default_addr is None:
        addr = config.getint('i2c_address')
    else:
        addr = config.getint('i2c_address', default_addr)
    return MCU_I2C(mcu, bus=config.get('i2c_bus', None), addr=addr,
                   speed=config.getint('i2c_speed', default_speed))
