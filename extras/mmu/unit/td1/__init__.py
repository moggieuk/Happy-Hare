# mmu td1 — TD-1 filament measurement package marker.
#
# Contains the device wrapper (mmu_td1_device.py): one object per physical scanner,
# keyed by USB serial and shared through the klipper object registry so a serial
# named by several gates - or several units - resolves to a single object.
#
# Unlike the NFC readers there are no drivers here. A TD-1 is a USB device owned by
# Moonraker's [td1] component, reached through the machine-level bridge in
# extras/mmu/mmu_td1.py.

__version__ = '1.0.0'
