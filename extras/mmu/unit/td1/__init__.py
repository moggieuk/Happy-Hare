# Happy Hare MMU Software
# TD-1 filament measurement package
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Package definition for the TD-1 filament measurement layer
#
# Contains the device wrapper (mmu_td1_device.py): one object per physical scanner,
# keyed by USB serial and shared through the klipper object registry. No drivers - a
# TD-1 is a USB device owned by Moonraker's [td1] component, reached through the
# machine-level bridge in extras/mmu/mmu_td1.py.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

__version__ = '1.0.0'
