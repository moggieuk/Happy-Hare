# Fake Klipper `klippy/chelper/` for the Happy Hare test harness.
#
# Deliberately a LOUD stub. extras/mmu/unit/mmu_drive.py:20 does `import chelper`
# and never references it - the import is vestigial. This stub proves that stays
# true: if anything ever actually calls get_ffi(), the test fails immediately and
# legibly rather than silently pulling in a C build (real chelper compiles
# c_helper.so on import via os.system("gcc ..."), needs cffi, and uses
# GCC-specific flags that Apple clang may reject).
#
# If a future HH change genuinely needs the iterative solver, that is the signal
# to move that surface to the optional real-Klipper tier, not to fill this in.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


def get_ffi():
    raise AssertionError(
        "chelper.get_ffi() was called, but the test harness has no C helper.\n"
        "As of this writing the only `import chelper` in Happy Hare is the unused "
        "one at extras/mmu/unit/mmu_drive.py:20. If real step generation is now "
        "needed, that belongs in the real-Klipper tier - see the harness plan.")
