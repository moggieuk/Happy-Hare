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
        "The mainline-Klipper code paths never need one: extras/mmu/unit/"
        "mmu_drive.py:20 imports chelper and never references it, and "
        "extras/mmu_stepper.py only reaches for it inside "
        "MotionQueuingEmulator, which the pre-motion_queuing-generation "
        "sessions (Session(kalico=True) / old_klipper=True) stand in for "
        "with a pure-Python ffi (test/hh/bootstrap.py). If a MAINLINE path "
        "now needs the iterative solver, that belongs in the real-Klipper "
        "tier - see the harness plan.")
