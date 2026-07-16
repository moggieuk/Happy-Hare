# klippy/extras/mmu_nfc_endstop.py
#
# Thin Klipper entry point for [mmu_nfc_endstop <name>] config sections.
#
# Klipper maps a config section name to a file of the same name in
# klippy/extras/, so [mmu_nfc_endstop <name>] requires a file called
# mmu_nfc_endstop.py here. All implementation lives in the Happy Hare mmu
# package; this module only re-exports the load hook so Klipper can build
# the endstop objects.
#
# Install
# ───────
# Run install.sh — it symlinks this file and the mmu package into
# ~/klipper/klippy/extras/ automatically.

from .mmu.unit.nfc.endstop import load_config_prefix

__all__ = ['load_config_prefix']
