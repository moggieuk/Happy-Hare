# mmu td1 — TD-1 filament measurement package marker.
#
# Contains the device wrapper (mmu_td1_device.py): one object per physical scanner,
# keyed by USB serial and shared through the klipper object registry. No drivers - a
# TD-1 is a USB device owned by Moonraker's [td1] component, reached through the
# machine-level bridge in extras/mmu/mmu_td1.py.

__version__ = '1.0.0'
