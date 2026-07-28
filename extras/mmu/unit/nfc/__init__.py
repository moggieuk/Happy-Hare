# mmu nfc — NFC/RFID subsystem package marker.
#
# Contains the reader wrapper (mmu_nfc_reader.py, the [mmu_nfc_reader] config object),
# the chip drivers (pn532_driver.py, pn5180_driver.py, pn7160_driver.py,
# rc522_driver.py) and reader_factory.py which builds the right driver from
# config, plus the tag decoder (tag_parser.py) used for deep reads.

__version__ = '1.0.0'
