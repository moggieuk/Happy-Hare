# Happy Hare test harness
#
# Goal: build a fake `klippy/` tree so Happy Hare's modules import exactly as they
#       do on a real install, with no Klipper, no chelper and no hardware.
#
# Happy Hare installs by symlinking its `extras/**.py` into
# $(KLIPPER_HOME)/klippy/extras/ (Makefile:368). Its imports only resolve inside
# that shape:
#
#   extras/mmu/unit/nfc/reader_factory.py : from .... import bus   -> extras.bus
#   extras/mmu/unit/mmu_leds.py           : from ... import led    -> extras.led
#   extras/mmu/mmu_filament_movement.py   : from ..homing import.. -> extras.homing
#   extras/mmu_stepper.py                 : from kinematics.extruder import ...
#   extras/mmu/mmu_sensor_utils.py        : import mcu
#
# So we reproduce the install layout rather than shimming sys.path: real
# directories, per-file symlinks using the *same glob patterns as Makefile:114*
# (which deliberately excludes the five stale copies in extras/temp/).
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os, sys, glob, atexit, shutil, tempfile

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
FAKE_SRC    = os.path.join(HARNESS_DIR, 'klippy_root')
REPO_ROOT   = os.path.dirname(os.path.dirname(HARNESS_DIR))

# Must stay in lockstep with Makefile:114 `hh_klipper_extras_files`. Each pattern
# is asserted non-empty so a repo reorganisation fails loudly here rather than
# silently shipping a partial overlay.
HH_GLOBS = (
    'extras/*.py',
    'extras/mmu/*.py',
    'extras/mmu/unit/*.py',
    'extras/mmu/unit/nfc/*.py',
    'extras/mmu/unit/selectors/*.py',
    'extras/mmu/commands/*.py',
)

_overlay = None  # module-level singleton; one build serves the whole test session


def build_overlay():
    """
    Assemble the fake klippy tree in a temp dir and return its path. Cached, so
    repeated calls in one process are free. Registers an atexit cleanup.
    """
    global _overlay
    if _overlay is not None:
        return _overlay

    klippy = os.path.join(tempfile.mkdtemp(prefix='hh-klippy-'), 'klippy')
    os.makedirs(klippy)

    # 1. The fake Klipper core: mcu.py, chelper.py, stepper.py, kinematics/,
    #    extras/{bus,led,output_pin,pulse_counter,homing,force_move,...}.py
    #    Symlinked (not copied) so editing a fake is immediately live.
    for dirpath, _dirnames, filenames in os.walk(FAKE_SRC):
        if '__pycache__' in dirpath:
            continue
        rel = os.path.relpath(dirpath, FAKE_SRC)
        dest_dir = klippy if rel == '.' else os.path.join(klippy, rel)
        os.makedirs(dest_dir, exist_ok=True)
        for fn in filenames:
            if fn.endswith('.py'):
                _link(os.path.join(dirpath, fn), os.path.join(dest_dir, fn))

    # 2. Happy Hare's own modules, overlaid exactly as the Makefile does.
    for pattern in HH_GLOBS:
        matches = sorted(glob.glob(os.path.join(REPO_ROOT, pattern)))
        assert matches, (
            "HH_GLOBS pattern %r matched nothing under %s - has the repo layout "
            "changed? Keep this list in lockstep with Makefile:114." % (pattern, REPO_ROOT))
        for src in matches:
            rel = os.path.relpath(src, REPO_ROOT)
            dest = os.path.join(klippy, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _link(src, dest)

    # 3. `extras` must be a REGULAR package, not a namespace package.
    #
    # The repo root is also on sys.path under test discovery, and the repo's own
    # `extras/` has no __init__.py. Namespace packages union __path__ across every
    # matching sys.path entry, so without this file `extras.__path__` would become
    # [<tmp>/klippy/extras, <repo>/extras] and which copy wins would depend on
    # sys.path order. An __init__.py terminates the merge. HH's own subpackages
    # (extras/mmu, extras/mmu/unit) stay namespace portions beneath it, which is
    # exactly what they are on a real install too.
    init = os.path.join(klippy, 'extras', '__init__.py')
    if not os.path.exists(init):
        with open(init, 'w') as f:
            f.write('# Regular package: stops namespace-union leakage from the repo\n')

    atexit.register(shutil.rmtree, os.path.dirname(klippy), ignore_errors=True)
    _overlay = klippy
    return _overlay


def _link(src, dest):
    if os.path.lexists(dest):
        os.unlink(dest)
    os.symlink(src, dest)


def install():
    """
    Build the overlay, put it first on sys.path, and verify nothing leaked. Safe
    to call repeatedly. Returns the klippy root path.
    """
    klippy = build_overlay()
    if sys.path[0] != klippy:
        # Remove any stale entry, then take priority over the repo root.
        while klippy in sys.path:
            sys.path.remove(klippy)
        sys.path.insert(0, klippy)

    import extras
    got = os.path.realpath(os.path.dirname(extras.__file__ or ''))
    want = os.path.realpath(os.path.join(klippy, 'extras'))
    assert got == want, (
        "`extras` resolved to %s, not the fake tree at %s (__path__=%r). The repo's "
        "own extras/ has leaked in - check sys.path order and that "
        "<klippy>/extras/__init__.py exists." % (got, want, list(extras.__path__)))
    return klippy
