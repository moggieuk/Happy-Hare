# Happy Hare MMU Software
# Tests for MmuToolHead's trapq_finalize_moves call (extras/mmu_machine.py)
#
# Creality's klipper fork (K1 / K1 Max / Ender 3 V3) has the older
# trapq_finalize_moves(tq, print_time) and newer klipper has
# trapq_finalize_moves(tq, print_time, clear_history_time). Calling the wrong
# form raises TypeError and stops prints from starting (#892).
#
# mmu_machine.py is loaded by path against stub klippy modules: only the
# unsync path of _resync_no_lock is exercised, with everything around it mocked.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), '..', 'extras', 'mmu_machine.py')


def _load_mmu_machine():
    class _Main:
        NULL = None

    stubs = {
        'stepper': types.ModuleType('stepper'),
        'chelper': types.ModuleType('chelper'),
        'toolhead': types.ModuleType('toolhead'),
        'kinematics': types.ModuleType('kinematics'),
        'kinematics.extruder': types.ModuleType('kinematics.extruder'),
        'hh_extras': types.ModuleType('hh_extras'),
        'hh_extras.homing': types.ModuleType('hh_extras.homing'),
        'hh_extras.mmu_leds': types.ModuleType('hh_extras.mmu_leds'),
    }
    stubs['stepper'].PrinterRail = type('PrinterRail', (), {})
    stubs['chelper'].get_ffi = lambda: (_Main(), None)
    stubs['toolhead'].ToolHead = type('ToolHead', (), {})
    stubs['toolhead'].SDS_CHECK_TIME = 0.001
    for name in ('PrinterExtruder', 'DummyExtruder', 'ExtruderStepper'):
        setattr(stubs['kinematics.extruder'], name, type(name, (), {}))
    stubs['hh_extras'].__path__ = []
    stubs['hh_extras.homing'].Homing = type('Homing', (), {})
    stubs['hh_extras.homing'].HomingMove = type('HomingMove', (), {})
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location('hh_extras.mmu_machine', _PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


mmu_machine = _load_mmu_machine()


class _FinalizeLib:
    """Records trapq_finalize_moves calls with the given C arity."""

    def __init__(self, nargs):
        self.calls = []
        if nargs == 2:
            def trapq_finalize_moves(tq, print_time):
                self.calls.append((tq, print_time))
        else:
            def trapq_finalize_moves(tq, print_time, clear_history_time):
                self.calls.append((tq, print_time, clear_history_time))
        self.trapq_finalize_moves = trapq_finalize_moves


class TestTrapqFinalizeArity(unittest.TestCase):

    def _toolhead(self, nargs):
        th = mmu_machine.MmuToolHead.__new__(mmu_machine.MmuToolHead)
        th.lib = _FinalizeLib(nargs)
        th.reactor = mock.Mock(NEVER=9999999999999999.9)
        th.trapq = object()
        th.trapq_finalize_moves = th.lib.trapq_finalize_moves
        th.finalize_has_history_arg = th._probe_finalize_history_arg()
        th.lib.calls.clear()

        th.mmu = mock.MagicMock()
        th.printer = mock.MagicMock()
        th.motion_queuing = None
        th.printer_toolhead = mock.MagicMock()
        th.mmu_toolhead = mock.MagicMock()
        th.mmu_toolhead.get_last_move_time.return_value = 0.
        th._quiesce_align_get_tcut = mock.Mock(return_value=10.)
        th._prev_sk = th._prev_rd = []
        th._prev_trapq = object()
        th.sync_mode = th.GEAR_SYNCED_TO_EXTRUDER
        self.old_trapq = th.printer_toolhead.get_extruder().get_trapq()
        return th

    def test_unsync_with_two_argument_finalize(self):
        th = self._toolhead(2)
        th._resync_no_lock(None)
        self.assertEqual(th.lib.calls, [(self.old_trapq, 10.)])
        self.assertIsNone(th.sync_mode)

    def test_unsync_with_three_argument_finalize(self):
        th = self._toolhead(3)
        th._resync_no_lock(None)
        self.assertEqual(th.lib.calls,
                         [(self.old_trapq, 10., 10. - mmu_machine.MOVE_HISTORY_EXPIRE)])
        self.assertIsNone(th.sync_mode)


if __name__ == '__main__':
    unittest.main()
