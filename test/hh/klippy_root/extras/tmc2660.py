# Fake Klipper extras/tmc2660.py - thin re-export of the shared TMC fake.
# HH's TMC_CHIPS list (extras/mmu/mmu_constants.py:315) covers all six.
from . import tmc

load_config_prefix = tmc.make_load_config_prefix('tmc2660')
