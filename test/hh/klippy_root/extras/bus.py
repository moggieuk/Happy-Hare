# Fake Klipper `klippy/extras/bus.py` for the Happy Hare test harness.
#
# Used by extras/mmu/unit/nfc/reader_factory.py to build the NFC reader transports:
# MCU_SPI_from_config (RC522/PN5180) and MCU_I2C_from_config (PN532/PN7160).
#
# Note reader_factory passes a `BusDefaultConfig` WRAPPER to the I2C factory (but
# the RAW config to the SPI one), not a real ConfigWrapper - it __getattr__-forwards,
# so only get/getint/getfloat/get_name/error may be relied on here.
#
# The *_from_config functions mirror real Klipper's hardware/software bus selection
# closely on purpose - see _lookup_sw_pins for what must stay faithful and why.
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
        self.sw_pins = sw_pins      # (sclk, mosi, miso) for software SPI, else None

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
        # Software (bit-banged) I2C pins, or None for a hardware bus. Real
        # Klipper keeps these only to build the i2c_set_sw_bus command; the
        # harness stores them so tests can assert which mode was selected.
        self.sw_pins = sw_pins
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


def _lookup_sw_pins(config, prefix, names):
    """
    Resolve a software-bus pin group the way real Klipper does, or return None.

    Mirrors klippy/extras/bus.py: the presence of the FIRST pin option alone
    selects software mode, and every pin is then fetched with a BARE
    config.get(name) - i.e. required, no default. Keeping those calls bare
    matters: reader_factory wraps the config in BusDefaultConfig, and the bare
    call is exactly what exercises its "required option" handling. Substituting
    a default here would hide that.

    NOTE a deliberate fidelity gap: the harness's pins.parse_pin rejects an
    empty pin string ('(?P<pin>.+)' needs one char), whereas real Klipper's
    accepts it and yields pin=''. So a blank software pin raises here but
    reaches the MCU in production. Tests about blank pins must therefore assert
    on the error MESSAGE, not merely that something raised.
    """
    first = '%s_%s_pin' % (prefix, names[0])
    if config.get(first, None) is None:
        return None
    ppins = config.get_printer().lookup_object('pins')
    pin_names = ['%s_%s_pin' % (prefix, name) for name in names]
    params = [ppins.lookup_pin(config.get(name), share_type=name)
              for name in pin_names]
    return tuple(p['pin'] for p in params)


def MCU_SPI_from_config(config, mode, pin_option="cs_pin", default_speed=100000,
                        share_type=None, cs_active_high=False):
    printer = config.get_printer()
    mcu = printer.lookup_object('mcu')
    sw_pins = _lookup_sw_pins(config, 'spi_software', ('sclk', 'mosi', 'miso'))
    bus = None if sw_pins else config.get('spi_bus', None)
    return MCU_SPI(mcu, bus=bus,
                   pin=config.get(pin_option, None), mode=mode,
                   speed=config.getint('spi_speed', default_speed),
                   sw_pins=sw_pins)


def MCU_I2C_from_config(config, default_addr=None, default_speed=100000,
                        share_type=None):
    printer = config.get_printer()
    mcu = printer.lookup_object('mcu')
    if default_addr is None:
        addr = config.getint('i2c_address')
    else:
        addr = config.getint('i2c_address', default_addr)
    # Software pins win outright - Klipper tests i2c_software_scl_pin first and
    # only falls through to i2c_bus, so the two are never both in effect.
    sw_pins = _lookup_sw_pins(config, 'i2c_software', ('scl', 'sda'))
    bus = None if sw_pins else config.get('i2c_bus', None)
    return MCU_I2C(mcu, bus=bus, addr=addr,
                   speed=config.getint('i2c_speed', default_speed),
                   sw_pins=sw_pins)
