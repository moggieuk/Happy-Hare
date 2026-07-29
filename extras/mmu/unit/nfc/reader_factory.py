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
from .pn5180_driver import PN5180Driver
from .pn7160_driver import PN7160Driver
from .rc522_driver import RC522Driver

SUPPORTED_READER_TYPES = ('pn532', 'pn5180', 'pn7160', 'rc522')
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
    'pn5180': 1000000,
    'rc522': 1000000,
}
PN7160_I2C_ADDRESSES = (0x28, 0x29, 0x2A, 0x2B)


_UNSET = object()   # "caller passed no default", distinct from an explicit None


class BusDefaultConfig:
    """Wrap a Klipper ConfigWrapper to supply inherited bus defaults.

    Only get() is overridden; getint/getfloat/get_printer/error/... reach the real
    ConfigWrapper through __getattr__. Note that means getint('i2c_speed', ...)
    bypasses this wrapper entirely - see the i2c_speed branch below.
    """
    def __init__(self, config, default_bus, default_speed):
        self._cfg = config
        self._default_bus = default_bus
        self._default_speed = default_speed

    def get(self, key, default=_UNSET):
        # Klipper asks for the bus with an EXPLICIT None (config.get('i2c_bus', None)),
        # so for these two keys None and "omitted" must mean the same thing: fall back
        # to the value inherited from the base [mmu_nfc_reader] section. Do NOT fold
        # them into the _UNSET check - that silently breaks i2c_bus inheritance.
        if key == 'i2c_bus':
            if default is _UNSET or default is None:
                return self._cfg.get(key, self._default_bus)
            return self._cfg.get(key, default)
        if key == 'i2c_speed':
            # Currently unreachable: mainline Klipper reads the speed with
            # getint('i2c_speed', default_speed, minval=100000), which goes straight
            # to the real config via __getattr__. Kept as cheap insurance in case a
            # fork reads it with get().
            if default is _UNSET or default is None:
                return self._cfg.get(key, self._default_speed)
            return self._cfg.get(key, default)
        # Everything else: preserve Klipper's "option is required" semantics. It
        # fetches the software I2C pins with a bare config.get(name), and turning that
        # into get(name, None) would hand lookup_pin() a None - an internal traceback
        # instead of "Option 'i2c_software_sda_pin' ... must be specified".
        if default is _UNSET:
            return self._cfg.get(key)
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


def _software_i2c_buses(config):
    """
    Software-I2C bus registry for this printer: (i2c_mcu, scl, sda) -> {address, ...}.

    Exists because Klipper will not catch a bus collision for us. lookup_pin() re-uses an
    existing pin registration whenever share_type matches, and share_type is the literal
    option name - the same for every reader. So two readers handed the SAME pin pair
    quietly end up on ONE bit-banged bus, with no error at all. Two PN532s there means two
    devices answering to 0x24, which is exactly what per-reader software buses exist to
    prevent, and it shows up as intermittent read failures rather than a config error.

    Sharing a software bus is only wrong when the ADDRESSES clash - several PN7160s at
    0x28-0x2B on one bit-banged bus is a perfectly sensible setup - so this is keyed on
    (bus, address), not the pins alone.

    Stored on the printer rather than in a module global: readers are built at config
    time, and a Klipper restart builds a new printer, so this scopes itself and cannot
    carry stale entries forward as phantom collisions. All units share one printer, so
    collisions are still caught across units.
    """
    printer = config.get_printer()
    registry = getattr(printer, '_mmu_nfc_software_i2c_buses', None)
    if registry is None:
        registry = {}
        printer._mmu_nfc_software_i2c_buses = registry
    return registry


def _validate_software_i2c(config, reader_type, i2c_address, i2c_mcu):
    """
    Check the software-I2C pins for one reader, if it uses them. Returns True when this
    reader is on a software bus.

    Klipper checks none of this: parse_pin('') succeeds (yielding chip 'mcu', pin ''),
    and unlike its SPI branch the I2C one never verifies the pins live on i2c_mcu.
    """
    scl = config.get('i2c_software_scl_pin', None)
    sda = config.get('i2c_software_sda_pin', None)
    if scl is None and sda is None:
        return False # Hardware bus

    section = config.get_name().split()[-1]
    # Klipper selects software mode on the SCL pin alone, so a lone SDA would be
    # silently ignored - say so rather than quietly using a hardware bus.
    if scl is None:
        raise config.error(
            "[mmu_nfc_reader %s]: 'i2c_software_sda_pin' is set but "
            "'i2c_software_scl_pin' is not. Software I2C needs both." % section)
    for name, value in (('i2c_software_scl_pin', scl), ('i2c_software_sda_pin', sda)):
        if value is None or not str(value).strip():
            raise config.error(
                "[mmu_nfc_reader %s]: '%s' must name a pin when using software I2C "
                "(e.g. %s: mmu:PB8). Klipper accepts a blank pin here and then fails "
                "obscurely when the MCU is configured." % (section, name, name))

    scl, sda = str(scl).strip(), str(sda).strip()
    if scl == sda:
        raise config.error(
            "[mmu_nfc_reader %s]: software I2C SCL and SDA are the same pin (%s)."
            % (section, scl))

    # The pin's mcu prefix must agree with i2c_mcu. Klipper's SPI path enforces the
    # equivalent ("spi pins must be on same mcu"); its I2C path does not, and the
    # mismatch surfaces as an unrelated-looking MCU config failure.
    for name, value in (('i2c_software_scl_pin', scl), ('i2c_software_sda_pin', sda)):
        chip = value.split(':', 1)[0].strip() if ':' in value else 'mcu'
        if i2c_mcu and chip != i2c_mcu:
            raise config.error(
                "[mmu_nfc_reader %s]: '%s' is on mcu '%s' but i2c_mcu is '%s'. "
                "Software I2C pins must be on the same mcu as the bus."
                % (section, name, chip, i2c_mcu))

    key = (i2c_mcu, scl, sda)
    addresses = _software_i2c_buses(config).setdefault(key, set())
    if i2c_address in addresses:
        raise config.error(
            "[mmu_nfc_reader %s]: another reader already uses software I2C pins "
            "%s/%s at address %d (0x%02X). Two devices at one address on one bus "
            "cannot be addressed separately - give this reader its own pin pair. "
            "(A %s is fixed at one address, so each one needs its own bus.)"
            % (section, scl, sda, i2c_address, i2c_address, reader_type))
    addresses.add(i2c_address)
    return True


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

    if reader_type == 'pn5180':
        # PN5180 paces itself with BUSY rather than a fixed delay, so the
        # shared transceive_delay/crc_delay timings do not apply; the driver
        # reads its own pn5180_* tuning and its reset/busy pins from config.
        spi = bus_module.MCU_SPI_from_config(
            config, 0, default_speed=default_spi_speed(reader_type))
        return PN5180Driver(config, spi, gate, debug=debug, sleep_fn=sleep_fn)

    default_addr = (defaults.i2c_address if defaults is not None
                    else default_i2c_address(reader_type))
    i2c_address = config.getint('i2c_address', default_addr,
                                minval=0, maxval=127)
    validate_reader_i2c_address(config, reader_type, i2c_address)
    # Vet the software-I2C pins BEFORE handing the config to Klipper, which accepts
    # blank pins and colliding buses without complaint.
    _validate_software_i2c(config, reader_type, i2c_address,
                           config.get('i2c_mcu', 'mcu'))
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
