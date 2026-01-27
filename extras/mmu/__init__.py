# Happy Hare MMU Software
# Main module wrapper
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Firmware to control any Klipper based MMU
#
# Note that code is written in a system to support Python v2 given the widespread use in
# the klipper community. Hopefully this will change soon.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
from .mmu import Mmu

def load_config(config):
    return Mmu(config)
