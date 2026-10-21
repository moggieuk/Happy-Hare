# Happy Hare test harness - persisted statistics must reload.
#
# Kalico's toolhead returns numpy.float64 print times. numpy.float64 subclasses float,
# so round() and isinstance(float) both keep it, and numpy 2's repr() - np.float64(4.88)
# - is not a literal: the next boot's ast.literal_eval rejects the whole variable (#877).
#
# Run with the repo venv:
#   ./venv/bin/python -m unittest test.test_mmu_statistics
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import ast
import configparser
import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class NumpyLikeFloat(float):
    """Stands in for numpy.float64, which the test venv does not have."""
    def __repr__(self):
        return "np.float64(%s)" % float.__repr__(self)

    def __add__(self, other):
        return NumpyLikeFloat(float(self) + other)
    __radd__ = __add__

    def __sub__(self, other):
        return NumpyLikeFloat(float(self) - other)

    def __rsub__(self, other):
        return NumpyLikeFloat(other - float(self))

    def __round__(self, ndigits=None):
        return NumpyLikeFloat(round(float(self), ndigits))


def read_raw_var(hh, name):
    parser = configparser.ConfigParser()
    parser.read(hh.save_variables.filename)
    return parser.get('Variables', name)


class TestStatisticsPersistAsLiterals(unittest.TestCase):
    def setUp(self):
        self.hh = session('boxturtle')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.mmu = self.hh.mmu

    def test_swap_time_from_numpy_print_time_reloads(self):
        toolhead = self.mmu.toolhead
        now = toolhead.get_last_move_time()
        toolhead.get_last_move_time = lambda: NumpyLikeFloat(now)
        self.mmu._track_time_start('pre_unload')
        toolhead.get_last_move_time = lambda: NumpyLikeFloat(now + 4.88)
        self.mmu._track_time_end('pre_unload')

        self.mmu._persist_swap_statistics()
        self.hh.reactor.advance(0.)

        stats = ast.literal_eval(read_raw_var(self.hh, 'mmu_statistics_swaps'))
        self.assertIs(type(stats['pre_unload']), float)
        self.assertAlmostEqual(stats['pre_unload'], 4.88)
        self.assertEqual(self.hh.errors, [])

    def test_gate_distance_from_numpy_position_reloads(self):
        self.mmu._track_gate_statistics('load_distance', 0, NumpyLikeFloat(12.5))

        self.mmu._persist_gate_statistics()
        self.hh.reactor.advance(0.)

        unit = self.mmu.mmu_unit(0)
        name = self.mmu.var_manager.namespace('mmu_statistics_gate_%d' % unit.local_gate(0), unit.name)
        stats = ast.literal_eval(read_raw_var(self.hh, name))
        self.assertIs(type(stats['load_distance']), float)
        self.assertEqual(self.hh.errors, [])


if __name__ == '__main__':
    unittest.main()
