# NFC package for Happy Hare.
#
# Holds the full NFC stack behind the [mmu_nfc_reader] hardware objects:
#   reader.py                       — mmu_nfc_reader chip-object impl (Klipper
#                                     loads it via the top-level shim
#                                     extras/mmu_nfc_reader.py)
#   reader_factory.py, *_driver.py  — chip drivers (pn532, pn7160, rc522)
#   driver_log.py                   — minimal logging shim for the drivers
#   manager.py, gate_state.py, ...  — per-lane/shared gate coordination,
#                                     Spoolman, tag parsing, scan-jog, logging
#
# The unit-owned wrapper (../mmu_nfc.py) builds the runtime managers from these
# during MmuUnit init.

__version__ = '1.0.0'
