# Happy Hare MMU Software
# NFC/RFID subsystem package
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Package definition for the NFC/RFID reader layer
#
# Contains the reader wrapper (mmu_nfc_reader.py, the [mmu_nfc_reader] config object),
# the chip drivers (pn532_driver.py, pn5180_driver.py, pn7160_driver.py,
# rc522_driver.py) and reader_factory.py which builds the right driver from
# config, the status-checked I2C helper the I2C drivers share (i2c_transport.py),
# plus the tag decoder (tag_parser.py) used for deep reads.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

__version__ = '1.0.0'
