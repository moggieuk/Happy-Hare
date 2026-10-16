# Happy Hare test selector - the start-up line printed before any file finishes
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test import select


class TestStartLine(unittest.TestCase):

    TIMES = {'a': 40.0, 'b': 30.0, 'c': 20.0, 'd': 10.0, 'e': 5.0}

    def test_parallel_first_result_is_the_quickest_of_the_first_batch(self):
        self.assertEqual(select._start_line(['a', 'b', 'c', 'd', 'e'], self.TIMES, 2),
                         'Running 5 files on 2 workers, slowest first'
                         ' - first result in ~30s, all done in ~55s')

    def test_serial_has_no_first_result_estimate(self):
        self.assertEqual(select._start_line(['a', 'e'], self.TIMES, 1),
                         'Running 2 files serially, all done in ~45s')

    def test_untimed_files_make_the_total_a_lower_bound(self):
        self.assertEqual(select._start_line(['a', 'b', 'x'], self.TIMES, 2),
                         'Running 3 files on 2 workers, slowest first'
                         ' - first result in ~30s, all done in >40s')

    def test_an_untimed_file_starting_at_once_drops_the_first_result_estimate(self):
        self.assertEqual(select._start_line(['a', 'x'], self.TIMES, 4),
                         'Running 2 files on 2 workers, slowest first, all done in >40s')

    def test_no_timings_gives_no_estimate(self):
        self.assertEqual(select._start_line(['x'], {}, 4), 'Running 1 file serially')


if __name__ == '__main__':
    unittest.main()
