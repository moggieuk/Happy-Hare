# Happy Hare test harness - switch sensors on Klipper without debounce support.
#
# buttons.register_debounce_button arrived in Klipper v0.12.0-460. Older Klipper (e.g.
# RatOS, issue #1454) lacks it, so switch sensors must fall back to register_buttons.
#
#   ./venv/bin/python -m unittest test.test_mmu_buttons_compat
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import install, session

install()   # make `extras.*` importable

from extras.buttons import PrinterButtons  # noqa: E402

logging.getLogger().setLevel(logging.CRITICAL)


class TestOldKlipperButtons(unittest.TestCase):

    def setUp(self):
        saved = PrinterButtons.register_debounce_button
        del PrinterButtons.register_debounce_button
        self.addCleanup(setattr, PrinterButtons, 'register_debounce_button', saved)

        self.hh = session('boxturtle')
        self.addCleanup(self.hh.close)

    def test_switch_sensors_work_without_debounce_api(self):
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')

        sensor = self.hh.sensor('mmu_exit_0')
        sensor.set(True)
        self.assertTrue(sensor.present)
        sensor.set(False)
        self.assertFalse(sensor.present)


if __name__ == '__main__':
    unittest.main()
