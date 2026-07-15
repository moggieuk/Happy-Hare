# klippy/extras/mmu_nfc_reader.py
#
# Thin Klipper entry point for [mmu_nfc_reader] and [mmu_nfc_reader <name>]
# config sections.
#
# Klipper maps a config section name to a file of the same name in
# klippy/extras/, so [mmu_nfc_reader] requires a file called mmu_nfc_reader.py
# here. All implementation lives in the Happy Hare mmu package; this module
# only re-exports the load hooks so Klipper can build the reader objects.
#
# Install
# ───────
# Run install.sh — it symlinks this file and the mmu package into
# ~/klipper/klippy/extras/ automatically.

from .mmu.unit.nfc.reader import load_config, load_config_prefix

__all__ = ['load_config', 'load_config_prefix']
