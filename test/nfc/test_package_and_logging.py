import importlib
import unittest
from unittest.mock import MagicMock, patch

from extras.mmu.unit.nfc import log


class TestPackageImports(unittest.TestCase):
    def test_reorganized_modules_import(self):
        modules = (
            'gate_state', 'klipper_interface', 'lameandboard_spoolman', 'log',
            'manager', 'pn532_driver', 'pn7160_driver', 'rc522_driver',
            'reader', 'reader_resolver', 'scan_jog', 'shared_preload',
            'shared_reader', 'spoolman_client', 'tag_handler', 'tag_parser',
        )
        for name in modules:
            with self.subTest(module=name):
                imported = importlib.import_module('extras.mmu.unit.nfc.' + name)
                self.assertIsNotNone(imported)


class TestLoggingAdapter(unittest.TestCase):
    def setUp(self):
        self.old = (log._printer, log._mmu, log._file_level,
                    log._console_enabled, log._console_level)
        log._printer = None
        log._mmu = None

    def tearDown(self):
        (log._printer, log._mmu, log._file_level,
         log._console_enabled, log._console_level) = self.old

    def test_level_and_format_helpers(self):
        self.assertEqual(2, log._normalise_level('warning', 4))
        self.assertEqual(4, log._normalise_level(99, 2))
        self.assertEqual('x=3', log._format('x=%d', (3,)))
        self.assertEqual('NFC hello', log._prefixed('hello'))
        self.assertEqual('NFC hello', log._prefixed('NFC hello'))

    def test_file_and_console_routing(self):
        mmu = MagicMock()
        mmu.gcode = MagicMock()
        printer = MagicMock()
        printer.lookup_object.return_value = mmu
        log.configure(printer=printer, console_output=True,
                      console_log_level='info', file_level='debug')
        log.logger.info('tag %s', 'read')
        mmu.log_to_file.assert_called_once_with('NFC tag read')
        mmu.gcode.respond_info.assert_called_once_with(
            '<span style="color:#4FC3F7">NFC</span> tag read')

    def test_debug_is_never_echoed_to_console(self):
        mmu = MagicMock(); mmu.gcode = MagicMock()
        log._mmu = mmu
        log._file_level = 4
        log._console_enabled = True
        log._console_level = 4
        log.logger.debug('detail')
        mmu.log_to_file.assert_called_once()
        mmu.gcode.respond_info.assert_not_called()


if __name__ == '__main__':
    unittest.main()
