# klippy/extras/mmu/unit/nfc/reader_factory.py
#
# Reader driver factory for mmu_nfc_reader.
#
# Originally part of a larger NFC gate-management extension; extracted here
# as a standalone hardware layer for use with Happy Hare.  Reader drivers own
# hardware/protocol details only and expose a small common interface:
# init(), is_alive(), read_tag(), read_target(), plus optional rich-read
# helpers (ntag_read_user_memory, mifare_authenticate, etc.). They know
# nothing about lanes, Spoolman, or Happy Hare.

from .... import bus as bus_module

from .pn532_driver import PN532Driver
from .pn7160_driver import PN7160Driver
from .rc522_driver import RC522Driver

SUPPORTED_READER_TYPES = ('pn532', 'pn7160', 'rc522')
DEFAULT_READER_TYPE = 'pn532'
DEFAULT_I2C_ADDRESS = {
    'pn532': 0x24,
    'pn7160': 0x28,
}
DEFAULT_I2C_SPEED = {
    'pn532': 100000,
    'pn7160': 100000,
}
DEFAULT_SPI_SPEED = {
    'rc522': 1000000,
}
PN7160_I2C_ADDRESSES = (0x28, 0x29, 0x2A, 0x2B)


class BusDefaultConfig:
    """Wrap a Klipper ConfigWrapper to supply inherited bus defaults."""
    def __init__(self, config, default_bus, default_speed):
        self._cfg = config
        self._default_bus = default_bus
        self._default_speed = default_speed

    def get(self, key, default=None):
        if key == 'i2c_bus':
            return self._cfg.get(
                key, self._default_bus if default is None else default)
        if key == 'i2c_speed':
            return self._cfg.get(
                key, self._default_speed if default is None else default)
        return self._cfg.get(key, default)

    def __getattr__(self, name):
        return getattr(self._cfg, name)


def reader_type_from_config(config, default=DEFAULT_READER_TYPE):
    reader_type = str(config.get('reader_type', default)).strip().lower()
    if reader_type not in SUPPORTED_READER_TYPES:
        raise config.error(
            "Invalid reader_type '%s' in [%s]; supported values: %s"
            % (reader_type, config.get_name(),
               ', '.join(SUPPORTED_READER_TYPES)))
    return reader_type


def default_i2c_address(reader_type):
    return DEFAULT_I2C_ADDRESS.get(reader_type, DEFAULT_I2C_ADDRESS['pn532'])


def default_i2c_speed(reader_type):
    return DEFAULT_I2C_SPEED.get(reader_type, DEFAULT_I2C_SPEED['pn532'])


def default_spi_speed(reader_type):
    return DEFAULT_SPI_SPEED.get(reader_type, 1000000)


def validate_reader_i2c_address(config, reader_type, address):
    if reader_type == 'pn7160' and address not in PN7160_I2C_ADDRESSES:
        allowed = ', '.join("%d" % addr for addr in PN7160_I2C_ADDRESSES)
        raise config.error(
            "nfc_gate [%s]: PN7160 i2c_address must be one of %s "
            "(0x28-0x2B); got %d"
            % (config.get_name().split()[-1], allowed, address))


def create_reader(config, defaults, reader_type, gate, debug,
                  low_level_debug=False, sleep_fn=None,
                  transceive_delay=0.250, crc_delay=0.050):
    if reader_type == 'rc522':
        spi = bus_module.MCU_SPI_from_config(
            config, 0, default_speed=default_spi_speed(reader_type))
        rc522_delay = config.getfloat(
            'rc522_transceive_delay', 0.035, minval=0.001, maxval=1.0)
        return RC522Driver(
            spi, gate, transceive_delay=rc522_delay, debug=debug,
            sleep_fn=sleep_fn)

    default_addr = (defaults.i2c_address if defaults is not None
                    else default_i2c_address(reader_type))
    i2c_address = config.getint('i2c_address', default_addr,
                                minval=0, maxval=127)
    validate_reader_i2c_address(config, reader_type, i2c_address)
    default_bus = defaults.i2c_bus if defaults is not None else None
    default_speed = default_i2c_speed(reader_type)
    i2c = bus_module.MCU_I2C_from_config(
        BusDefaultConfig(config, default_bus, default_speed),
        default_addr=i2c_address,
        default_speed=default_speed)

    if reader_type == 'pn532':
        return PN532Driver(
            i2c, gate, transceive_delay, crc_delay, debug,
            low_level_debug=low_level_debug,
            sleep_fn=sleep_fn)

    if reader_type == 'pn7160':
        return PN7160Driver(config, i2c, gate, debug=debug, sleep_fn=sleep_fn)

    raise config.error(
        "nfc_gate [%s]: reader_type '%s' is recognized, but its driver is "
        "not integrated yet"
        % (config.get_name().split()[-1], reader_type))
