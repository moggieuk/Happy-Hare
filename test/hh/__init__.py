# Happy Hare test harness.
#
# Public entry point. See root.py for how the fake klippy tree is assembled and
# why it must reproduce the real install layout rather than shim sys.path.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from .root import build_overlay, install


def session(profile='boxturtle', **kwargs):
    """Deferred import: bootstrap pulls in the fake klippy tree, which must not
    happen just because someone imported test.hh (A0 runs with no deps)."""
    from .bootstrap import Session
    return Session(profile, **kwargs)


__all__ = ['build_overlay', 'install', 'session']
