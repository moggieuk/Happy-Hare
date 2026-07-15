"""Tests for the hardware-independent NFC unit."""

import os
import sys
import types

# Klipper loads extras as a top-level module directory.  Reproduce that import
# layout so reader_factory's standalone ``import bus`` fallback is exercised.
EXTRAS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..',
                                         '..', 'extras'))
if EXTRAS_DIR not in sys.path:
    sys.path.insert(0, EXTRAS_DIR)

# ``bus`` is supplied by Klipper at runtime, not by this repository.  Imports
# only need its constructors to exist; individual factory tests can mock them.
if 'bus' not in sys.modules:
    bus = types.ModuleType('bus')
    bus.MCU_I2C_from_config = None
    bus.MCU_SPI_from_config = None
    sys.modules['bus'] = bus
