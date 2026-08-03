# Happy Hare test harness - software (bit-banged) I2C for the PN532/PN7160 readers.
#
# WHY THIS FILE EXISTS. A PN532 is hardwired to I2C address 0x24, so only one can live
# on a hardware bus - which makes a PN532-per-gate build impossible. Klipper can bit-bang
# I2C on any two GPIO pins (src/i2c_software.c), so each reader gets its own private
# two-pin bus and the address stops mattering. That is the capability under test.
#
# The reader DRIVERS are untouched by all this: MCU_I2C uses the same i2c_write/i2c_read
# commands either way (klippy/extras/bus.py), so only config plumbing changes. Which is
# precisely why the risk lives in config handling, and why most of this file is about
# what Klipper does NOT check for you:
#
#   - parse_pin('') succeeds in real Klipper (chip 'mcu', pin ''), so a blank software
#     pin sails through config and fails obscurely at MCU config time.
#   - lookup_pin() re-uses an existing registration when share_type matches, and
#     share_type is the literal option name - identical for every reader. So two readers
#     given the SAME pin pair silently become ONE shared bus. Two PN532s at 0x24 on one
#     bus is exactly the collision software I2C exists to avoid, and Klipper reports
#     nothing.
#
# reader_factory validates both, because nothing below it will.
#
#   ./venv/bin/python -m unittest test.test_mmu_nfc_i2c
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import install
from test.hh import cfg, profiles

install()   # put the fake klippy tree on sys.path so `extras.*` resolves

from extras.mmu.unit.nfc import reader_factory  # noqa: E402


# ---- config-rendering tests (no boot, just the template) --------------------

class TestRenderedConfig(unittest.TestCase):
    """What the installer actually writes into mmu_hardware.cfg."""

    def reader_sections(self, profile_name):
        hw = cfg.render(profiles.get(profile_name))['config/base/mmu_hardware.cfg']
        out, name = {}, None
        for line in hw.splitlines():
            stripped = line.strip()
            if stripped.startswith('[mmu_nfc_reader'):
                name = stripped.strip('[]').split(None, 1)[-1]
                out[name] = {}
                continue
            if stripped.startswith('['):
                name = None
                continue
            if name and ':' in stripped and not stripped.startswith('#'):
                key, _, val = stripped.partition(':')
                out[name][key.strip()] = val.split('#')[0].strip()
        return out

    def test_software_profile_emits_pins_and_no_bus(self):
        for name, keys in self.reader_sections('nfc_pn532_sw_i2c').items():
            if keys.get('reader_type') != 'pn532':
                continue
            self.assertIn('i2c_software_scl_pin', keys, name)
            self.assertIn('i2c_software_sda_pin', keys, name)
            # Klipper tests i2c_software_scl_pin FIRST and only falls through to
            # i2c_bus, so emitting both would leave a line that silently does
            # nothing - confusing to read back off a machine.
            self.assertNotIn('i2c_bus', keys, name)

    def test_each_software_reader_gets_a_distinct_pin_pair(self):
        """The whole point: same address, different buses."""
        pairs, addrs = [], set()
        for keys in self.reader_sections('nfc_pn532_sw_i2c').values():
            if keys.get('reader_type') != 'pn532':
                continue
            pairs.append((keys['i2c_software_scl_pin'], keys['i2c_software_sda_pin']))
            addrs.add(keys['i2c_address'])
        self.assertEqual(len(pairs), 2, 'expected two PN532 readers')
        self.assertEqual(len(set(pairs)), 2, 'readers must not share a pin pair')
        self.assertEqual(addrs, {'36'}, 'both readers should be at 0x24')

    def test_hardware_profile_is_unchanged(self):
        """Regression guard: adding the software option must not disturb hardware i2c."""
        keys = self.reader_sections('nfc_pn532')['unit0_nfc']
        self.assertEqual(keys['i2c_bus'], 'i2c1')
        self.assertNotIn('i2c_software_scl_pin', keys)
        self.assertNotIn('i2c_software_sda_pin', keys)

    def test_speed_floor_matches_klipper(self):
        """Klipper enforces i2c_speed minval=100000; the rendered value must clear it."""
        for profile in ('nfc_pn532', 'nfc_pn532_sw_i2c'):
            for name, keys in self.reader_sections(profile).items():
                if 'i2c_speed' in keys:
                    self.assertGreaterEqual(int(keys['i2c_speed']), 100000,
                                            '%s/%s' % (profile, name))


# ---- BusDefaultConfig ------------------------------------------------------

class _FakePrinter:
    """Just an attribute bag - the software-bus registry hangs off the printer."""


class _FakeConfig:
    """Minimal ConfigWrapper: raises for a missing option with no default, like Klipper."""

    class error(Exception):
        pass

    def __init__(self, name='t', printer=None, **opts):
        self._opts = opts
        self._name = name
        self._printer = printer if printer is not None else _FakePrinter()
        self.asked = []

    _sentinel = object()

    def get(self, option, default=_sentinel):
        self.asked.append(option)
        if option in self._opts:
            return self._opts[option]
        if default is self._sentinel:
            raise self.error(
                "Option '%s' in section 'mmu_nfc_reader %s' must be specified"
                % (option, self._name))
        return default

    def getint(self, option, default=_sentinel, minval=None, maxval=None):
        return int(self.get(option, default))

    def get_printer(self):
        return self._printer

    def get_name(self):
        return 'mmu_nfc_reader %s' % self._name


class TestBusDefaultConfig(unittest.TestCase):
    """
    The wrapper exists to inject bus defaults inherited from a base [mmu_nfc_reader]
    section. It must do that WITHOUT flattening Klipper's "this option is required"
    behaviour, because Klipper fetches the software pins with a bare config.get(name).
    """

    def wrap(self, cfg_obj, bus='i2c9', speed=100000):
        return reader_factory.BusDefaultConfig(cfg_obj, bus, speed)

    def test_missing_required_option_still_raises(self):
        """
        THE BUG THIS PINS. With default=None hard-coded, a bare get() for a required
        option returned None instead of raising, so a user who set SCL but omitted SDA
        got an internal traceback from lookup_pin(None) rather than a clear message.
        """
        c = _FakeConfig(i2c_software_scl_pin='mmu:PB8')
        with self.assertRaises(_FakeConfig.error):
            self.wrap(c).get('i2c_software_sda_pin')

    def test_explicit_default_is_still_honored(self):
        c = _FakeConfig()
        self.assertIsNone(self.wrap(c).get('i2c_software_scl_pin', None))

    def test_present_option_passes_through(self):
        c = _FakeConfig(i2c_software_scl_pin='mmu:PB8')
        self.assertEqual(self.wrap(c).get('i2c_software_scl_pin'), 'mmu:PB8')

    def test_i2c_bus_inheritance_survives_an_explicit_none(self):
        """
        Klipper asks with config.get('i2c_bus', None) - an EXPLICIT None. The wrapper
        must still substitute the inherited bus there, or a shared base-section i2c_bus
        silently stops being inherited. This is the regression a naive sentinel
        refactor introduces.
        """
        c = _FakeConfig()
        self.assertEqual(self.wrap(c, bus='i2c3').get('i2c_bus', None), 'i2c3')

    def test_own_i2c_bus_beats_the_inherited_one(self):
        c = _FakeConfig(i2c_bus='i2c1')
        self.assertEqual(self.wrap(c, bus='i2c3').get('i2c_bus', None), 'i2c1')

    def test_no_inherited_bus_yields_none(self):
        c = _FakeConfig()
        self.assertIsNone(self.wrap(c, bus=None).get('i2c_bus', None))


# ---- the software-pin validator --------------------------------------------

class TestSoftwareI2cValidation(unittest.TestCase):
    """
    Every case here is accepted WITHOUT COMPLAINT by Klipper, which is the entire
    reason reader_factory checks it. See this file's header for the two mechanisms.
    """

    def check(self, cfg_obj, reader_type='pn532', address=36, mcu='mmu'):
        return reader_factory._validate_software_i2c(
            cfg_obj, reader_type, address, mcu)

    def test_hardware_bus_is_not_flagged(self):
        self.assertFalse(self.check(_FakeConfig()))

    def test_valid_software_pins_accepted(self):
        c = _FakeConfig(i2c_software_scl_pin='mmu:PB8', i2c_software_sda_pin='mmu:PB9')
        self.assertTrue(self.check(c))

    def test_blank_pin_is_rejected(self):
        """parse_pin('') SUCCEEDS in real Klipper, so nothing downstream catches this."""
        c = _FakeConfig(i2c_software_scl_pin='mmu:PB8', i2c_software_sda_pin='')
        with self.assertRaises(_FakeConfig.error) as ctx:
            self.check(c)
        self.assertIn('i2c_software_sda_pin', str(ctx.exception))

    def test_sda_without_scl_is_rejected(self):
        """Klipper selects software mode on SCL alone, so a lone SDA is ignored."""
        c = _FakeConfig(i2c_software_sda_pin='mmu:PB9')
        with self.assertRaises(_FakeConfig.error) as ctx:
            self.check(c)
        self.assertIn('i2c_software_scl_pin', str(ctx.exception))

    def test_same_pin_for_scl_and_sda_is_rejected(self):
        c = _FakeConfig(i2c_software_scl_pin='mmu:PB8', i2c_software_sda_pin='mmu:PB8')
        with self.assertRaises(_FakeConfig.error) as ctx:
            self.check(c)
        self.assertIn('same pin', str(ctx.exception))

    def test_pin_on_a_different_mcu_than_the_bus_is_rejected(self):
        """Klipper's SPI path enforces same-mcu; its I2C path does not."""
        c = _FakeConfig(i2c_software_scl_pin='other:PB8', i2c_software_sda_pin='mmu:PB9')
        with self.assertRaises(_FakeConfig.error) as ctx:
            self.check(c, mcu='mmu')
        self.assertIn('same mcu', str(ctx.exception))

    def test_two_readers_same_bus_same_address_is_rejected(self):
        """
        THE COLLISION THIS FEATURE EXISTS TO PREVENT. Klipper happily shares the pins
        (matching share_type), leaving two PN532s both answering to 0x24 on one bus.
        """
        printer = _FakePrinter()
        first = _FakeConfig(name='g0', printer=printer,
                            i2c_software_scl_pin='mmu:PB8',
                            i2c_software_sda_pin='mmu:PB9')
        second = _FakeConfig(name='g1', printer=printer,
                             i2c_software_scl_pin='mmu:PB8',
                             i2c_software_sda_pin='mmu:PB9')
        self.assertTrue(self.check(first, address=36))
        with self.assertRaises(_FakeConfig.error) as ctx:
            self.check(second, address=36)
        self.assertIn('g1', str(ctx.exception))

    def test_two_readers_same_bus_different_addresses_is_allowed(self):
        """
        Guards the validator against over-rejecting: several PN7160s at 0x28-0x2B on ONE
        bit-banged bus is a legitimate setup, and must keep working.
        """
        printer = _FakePrinter()
        for name, addr in (('g0', 40), ('g1', 41), ('g2', 42)):
            c = _FakeConfig(name=name, printer=printer,
                            i2c_software_scl_pin='mmu:PB8',
                            i2c_software_sda_pin='mmu:PB9')
            self.assertTrue(self.check(c, reader_type='pn7160', address=addr))

    def test_distinct_buses_at_the_same_address_is_allowed(self):
        """The actual PN532-per-gate configuration."""
        printer = _FakePrinter()
        for name, scl, sda in (('g0', 'mmu:PB8', 'mmu:PB9'),
                               ('g1', 'mmu:PC4', 'mmu:PC5')):
            c = _FakeConfig(name=name, printer=printer,
                            i2c_software_scl_pin=scl, i2c_software_sda_pin=sda)
            self.assertTrue(self.check(c, address=36))

    def test_registry_is_scoped_per_printer(self):
        """A Klipper restart builds a new printer; stale entries must not look like
        collisions."""
        for _ in range(2):
            printer = _FakePrinter()
            c = _FakeConfig(printer=printer, i2c_software_scl_pin='mmu:PB8',
                            i2c_software_sda_pin='mmu:PB9')
            self.assertTrue(self.check(c, address=36))


if __name__ == '__main__':
    unittest.main()
