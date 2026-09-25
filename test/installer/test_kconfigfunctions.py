# Tests for Happy Hare's Python-backed Kconfig preprocessor helpers.

import os
import tempfile
import unittest
from unittest import mock

from installer.lib.kconfiglib import kconfigfunctions as funcs


class TestBasicFunctions(unittest.TestCase):

    def test_environment_default_distinguishes_empty_from_unset(self):
        with mock.patch.dict(os.environ, {'HH_KCONFIG_TEST': ''}, clear=False):
            self.assertEqual(funcs.env_default(None, None, 'HH_KCONFIG_TEST', 'fallback'), '')
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HH_KCONFIG_MISSING', None)
            self.assertEqual(funcs.env_default(None, None, 'HH_KCONFIG_MISSING', 'fallback'),
                             'fallback')

    def test_boolean_and_path_helpers(self):
        with tempfile.TemporaryDirectory() as root:
            filename = os.path.join(root, 'file')
            with open(filename, 'w') as handle:
                handle.write('x')
            self.assertEqual(funcs.nonempty(None, None, 'value'), 'y')
            self.assertEqual(funcs.nonempty(None, None, ''), 'n')
            self.assertEqual(funcs.path_exists(None, None, filename), 'y')
            self.assertEqual(funcs.path_exists(None, None, filename + '.missing'), 'n')
            self.assertEqual(funcs.path_is_dir(None, None, root), 'y')
            self.assertEqual(funcs.path_is_dir(None, None, filename), 'n')

    def test_path_join_decodes_serialized_kconfig_value(self):
        self.assertEqual(
            funcs.path_join(None, None, '"~/printer_data/config"',
                            'mmu/mmu_vars.cfg'),
            '~/printer_data/config/mmu/mmu_vars.cfg')
        self.assertEqual(
            funcs.path_join(None, None, '~/printer_data/config',
                            'mmu/mmu_vars.cfg'),
            '~/printer_data/config/mmu/mmu_vars.cfg')

    def test_word_helpers_are_one_based(self):
        self.assertEqual(funcs.word_count(None, None, ' can0:a   can1:b '), '2')
        self.assertEqual(funcs.word_at(None, None, 'can0:a can1:b', '2'), 'can1:b')
        self.assertEqual(funcs.word_at(None, None, 'can0:a', '2'), '')

    def test_padding_never_truncates(self):
        self.assertEqual(funcs.pad(None, None, '5', 'x'), 'x    ')
        self.assertEqual(funcs.pad(None, None, '2', 'long'), 'long')

    def test_multiline_uses_literal_newline_escapes(self):
        self.assertEqual(funcs.multiline(None, None, 'one\ntwo'), r'one\ntwo\n')
        self.assertEqual(funcs.multiline(None, None, r'one\ntwo'), r'one\ntwo\n')
        self.assertEqual(funcs.multiline(None, None, ''), '')


class TestLedThemeFunctions(unittest.TestCase):

    def themes(self, files, max_themes='10'):
        with tempfile.TemporaryDirectory() as root:
            for name, first_line in files.items():
                with open(os.path.join(root, name), 'w') as handle:
                    handle.write(first_line + '\neffect_error: mmu_red_strobe, (1, 0, 0)\n')
            with mock.patch.object(funcs, '_LED_THEME_DIR', root):
                names = funcs.led_themes(None, None, max_themes)
                return names, [funcs.led_theme_label(None, None, n) for n in names.split()]

    def test_names_are_sorted_and_labelled_from_the_header(self):
        self.assertEqual(self.themes({'standard.cfg': '[# LED THEME: Standard #]',
                                      'emu.cfg': '[# LED THEME: EMU #]',
                                      'README': 'not a theme'}),
                         ('emu standard', ['EMU', 'Standard']))

    def test_label_falls_back_to_the_name(self):
        self.assertEqual(self.themes({'plain.cfg': '# no label'}), ('plain', ['plain']))

    def test_help_is_read_from_the_header_comment(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'emu.cfg'), 'w') as handle:
                handle.write('[# LED THEME: EMU\n  First line.\n  Second line.\n#]\neffect_error: x\n')
            with open(os.path.join(root, 'plain.cfg'), 'w') as handle:
                handle.write('[# LED THEME: Plain #]\neffect_error: x\n')
            with mock.patch.object(funcs, '_LED_THEME_DIR', root):
                self.assertEqual(funcs.led_theme_label(None, None, 'emu'), 'EMU')
                self.assertEqual(funcs.led_theme_help(None, None, 'emu'), 'First line.\nSecond line.')
                self.assertEqual(funcs.led_theme_label(None, None, 'plain'), 'Plain')
                self.assertEqual(funcs.led_theme_help(None, None, 'plain'),
                                 'LED effects from config/led_theme/plain.cfg')

    def test_bad_names_and_too_many_themes_are_errors(self):
        import kconfiglib
        with self.assertRaisesRegex(kconfiglib.KconfigError, 'lowercase'):
            self.themes({'My-Theme.cfg': ''})
        with self.assertRaisesRegex(kconfiglib.KconfigError, 'only 1'):
            self.themes({'a.cfg': '', 'b.cfg': ''}, max_themes='1')

    def test_choice_names(self):
        self.assertEqual(funcs.led_theme_choice(None, None, 'emu'), 'CHOICE_LED_THEME_EMU')
        self.assertEqual(funcs.led_theme_choice(None, None, ''), 'CHOICE_LED_THEME_NONE')

class TestConnectionFunctions(unittest.TestCase):

    DEVICES = ('/dev/serial/by-id/usb-Klipper_stm32-AAA '
               '/dev/serial/by-id/usb-Klipper_rp2040-BBB '
               '/dev/serial/by-id/usb-Klipper_stm32-CCC')

    def test_serial_filter_and_index_match_the_old_pipeline(self):
        self.assertEqual(funcs.serial_device(None, None, self.DEVICES, '1', 'stm32'),
                         '/dev/serial/by-id/usb-Klipper_stm32-AAA')
        self.assertEqual(funcs.serial_device(None, None, self.DEVICES, '2', 'stm32'),
                         '/dev/serial/by-id/usb-Klipper_stm32-CCC')
        self.assertEqual(funcs.serial_device(None, None, self.DEVICES, '3', 'stm32'), '')

    def test_serial_choice_names_preserve_existing_shapes(self):
        device = '/dev/serial/by-id/usb-Klipper_stm32-AAA'
        stem = 'USB_KLIPPER_STM32_AAA'
        self.assertEqual(funcs.mmu_serial_choice(None, None, device),
                         'CHOICE_MMU_SERIAL_DEVICE_' + stem)
        self.assertEqual(funcs.mmu_serial_gate_choice(None, None, '3', device),
                         'CHOICE_MMU_SERIAL_DEVICE_' + stem + '_GATE_3')
        self.assertEqual(funcs.buffer_serial_choice(None, None, device),
                         'CHOICE_BUFFER_SERIAL_DEVICE_' + stem)
        self.assertEqual(funcs.mmu_serial_choice(None, None, ''),
                         'CHOICE_MMU_SERIAL_DEVICE_NONE')

    def test_canbus_connection_fields_and_choice_names(self):
        connection = 'vlan-1:abc123'
        self.assertEqual(funcs.connection_interface(None, None, connection), 'vlan-1')
        self.assertEqual(funcs.connection_uuid(None, None, connection), 'abc123')
        self.assertEqual(funcs.mmu_canbus_choice(None, None, connection),
                         'CHOICE_MMU_CANBUS_UUID_VLAN_1_ABC123')
        self.assertEqual(funcs.mmu_canbus_gate_choice(None, None, '4', connection),
                         'CHOICE_MMU_CANBUS_UUID_VLAN_1_ABC123_GATE_4')
        self.assertEqual(funcs.buffer_canbus_choice(None, None, connection),
                         'CHOICE_BUFFER_CANBUS_UUID_VLAN_1_ABC123')
        self.assertEqual(funcs.mmu_canbus_choice(None, None, ''),
                         'CHOICE_MMU_CANBUS_UUID_NONE')

    def test_saved_interface_defaults_to_can0(self):
        self.assertEqual(funcs.saved_interface(None, None, ''), 'can0')
        self.assertEqual(funcs.saved_interface(None, None, 'can2'), 'can2')


class TestMenuTextFunctions(unittest.TestCase):

    def test_single_and_multi_unit_text(self):
        message = 'Happy Hare v4.0.0'
        suffix = funcs.unit_suffix(None, None, 'unit0')
        self.assertEqual(suffix, 'Unit: [[B]]unit0[[/B]]')
        self.assertEqual(funcs.menu_title(None, None, '', message, suffix),
                         message + ' Configuration - ' + suffix)
        self.assertEqual(funcs.menu_caption(None, None, '', message, suffix),
                         'Configuration - ' + suffix)
        self.assertIn('Multi Unit Setup',
                      funcs.menu_title(None, None, 'y', message, suffix))
        self.assertIn('Shared Config',
                      funcs.menu_caption(None, None, 'y', message, suffix))


if __name__ == '__main__':
    unittest.main()
