# A board header feeds one feature at a time. Where a board file offers the same pin
# as the default for two features, the lower-priority one only defaults to it while the
# other is fitted AND still on that pin: sensors before TMC diag pins, the selector
# endstop and entry sensors before the shared exit sensor, the encoder before an eSpooler
# and an analog buffer before a cutter servo.

import collections
import re
import unittest

from test.hh import cfg, profiles

BOXTURTLE = {'MMU_TYPE_BOX_TURTLE_1_0': True}
TRADRACK = {'MMU_TYPE_TRADRACK_1_0': True}
ERCF = {'MMU_FAMILY_ERCF': True, 'MMU_TYPE_ERCF_1_1': True}
PICO_MMU = {'MMU_TYPE_PICO_MMU_1_0': True, 'MMU_HAS_SENSOR_SHARED_EXIT': True}

_TEMPLATES = ('config/base/mmu_hardware.cfg', 'config/base/mmu.cfg')
_PIN = re.compile(r'[~^!]*(\w+:(?:P[A-K]\d+|gpio\d+))')
# Several TMC drivers can share one UART pin, told apart by uart_address
_SHAREABLE_KEYS = ('uart_pin',)


def _kconfig(label, syms):
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        return cfg._kconfig(label, syms)


def _pin_uses(label, syms):
    rendered = cfg.render(profiles.Profile(label, syms=syms))
    uses = collections.defaultdict(list)
    for template in _TEMPLATES:
        section = None
        for line in rendered[template].splitlines():
            m = re.match(r'\[(.+)\]', line)
            if m:
                section = m.group(1)
                continue
            m = re.match(r'\s*(\w+)\s*:(.*)', line)
            if not m or m.group(1) in _SHAREABLE_KEYS:
                continue
            for pin in _PIN.findall(m.group(2).split('#')[0]):
                uses[pin].append('[%s] %s' % (section, m.group(1)))
    return uses


def _duplicates(label, syms):
    return {pin: where for pin, where in _pin_uses(label, syms).items() if len(where) > 1}


class TestNoPinIsRenderedTwice(unittest.TestCase):

    CASES = {
        'boxturtle_mmb_1_1': dict(BOXTURTLE, BOARD_TYPE_MMB_1_1=True),
        'boxturtle_mmb_1_0': dict(BOXTURTLE, BOARD_TYPE_MMB_1_0=True),
        'boxturtle_mmb_1_1_encoder': dict(BOXTURTLE, BOARD_TYPE_MMB_1_1=True, MMU_HAS_ENCODER=True),
        'tradrack_mmb_1_1': dict(TRADRACK, BOARD_TYPE_MMB_1_1=True),
        'tradrack_mmb_2_0': dict(TRADRACK, BOARD_TYPE_MMB_2_0=True),
        'ercf_tzb_buffer_cutter': dict(
            ERCF, BOARD_TYPE_TZB_1_0=True, MMU_HAS_SYNC_FEEDBACK_BUFFER=True,
            MMU_HAS_SENSOR_BUFFER_PROPORTIONAL=True, MMU_HAS_SERVO_CUTTER=True),
    }

    def test_each_pin_has_one_job(self):
        for label, syms in self.CASES.items():
            with self.subTest(case=label):
                self.assertEqual(_duplicates(label, syms), {})


class TestLosingPinsAreBlank(unittest.TestCase):

    CASES = {
        'blank_boxturtle_mmb_1_1': (
            dict(BOXTURTLE, BOARD_TYPE_MMB_1_1=True),
            {'PIN_GEAR_DIAG': '', 'PIN_GEAR_DIAG_2': '', 'PIN_GEAR_DIAG_3': '',
             'PIN_SHARED_EXIT_SENSOR': '', 'PIN_ENTRY_SENSOR_1': 'unit0:PA3'}),
        'blank_boxturtle_mmb_1_0': (
            dict(BOXTURTLE, BOARD_TYPE_MMB_1_0=True),
            {'PIN_GEAR_DIAG': '', 'PIN_GEAR_DIAG_2': '', 'PIN_GEAR_DIAG_3': '',
             'PIN_SHARED_EXIT_SENSOR': '', 'PIN_ENTRY_SENSOR_1': 'unit0:PA3'}),
        'blank_boxturtle_mmb_1_1_encoder': (
            dict(BOXTURTLE, BOARD_TYPE_MMB_1_1=True, MMU_HAS_ENCODER=True),
            {'PIN_ESPOOLER_RWD_1': '', 'PIN_ENCODER': '^unit0:PA1'}),
        'blank_tradrack_mmb_1_1': (
            dict(TRADRACK, BOARD_TYPE_MMB_1_1=True),
            {'PIN_GEAR_DIAG': '', 'PIN_SHARED_EXIT_SENSOR': '^unit0:PA3'}),
        'blank_tradrack_mmb_2_0': (
            dict(TRADRACK, BOARD_TYPE_MMB_2_0=True),
            {'PIN_SHARED_EXIT_SENSOR': '', 'PIN_SELECTOR_ENDSTOP': '^unit0:PA15'}),
        'blank_ercf_tzb': (
            dict(ERCF, BOARD_TYPE_TZB_1_0=True, MMU_HAS_SYNC_FEEDBACK_BUFFER=True,
                 MMU_HAS_SENSOR_BUFFER_PROPORTIONAL=True, MMU_HAS_SERVO_CUTTER=True),
            {'PIN_SERVO_CUTTER_SERVO': '', 'PIN_BUFFER_ANALOG': 'unit0:PA3'}),
    }

    def test_lower_priority_pin_is_left_unset(self):
        for label, (syms, expected) in self.CASES.items():
            with self.subTest(case=label):
                kconfig = _kconfig(label, syms)
                self.assertEqual({sym: kconfig.syms[sym].str_value for sym in expected}, expected)


class TestFreeHeadersKeepTheirDefaults(unittest.TestCase):

    def test_gear_diag_stays_when_the_encoder_frees_the_header(self):
        kconfig = _kconfig('free_tradrack_mmb_1_1_encoder',
                           dict(TRADRACK, BOARD_TYPE_MMB_1_1=True, MMU_HAS_ENCODER=True))
        self.assertEqual(kconfig.syms['PIN_GEAR_DIAG'].str_value, 'unit0:PA3')

    def test_multigear_diag_pins_stay_without_entry_sensors(self):
        kconfig = _kconfig('free_boxturtle_mmb_1_1', dict(
            BOXTURTLE, BOARD_TYPE_MMB_1_1=True, MMU_HAS_SENSOR_ENTRY=False, MMU_HAS_ENCODER=True))
        self.assertEqual(
            [kconfig.syms[s].str_value for s in
             ('PIN_GEAR_DIAG', 'PIN_GEAR_DIAG_1', 'PIN_GEAR_DIAG_2', 'PIN_GEAR_DIAG_3')],
            ['unit0:PA3', 'unit0:PA4', 'unit0:PB9', 'unit0:PA8'])

    def test_shared_exit_stays_on_mmb_1_1_without_entry_sensors(self):
        kconfig = _kconfig('free_boxturtle_mmb_1_1_no_entry', dict(
            BOXTURTLE, BOARD_TYPE_MMB_1_1=True, MMU_HAS_SENSOR_ENTRY=False))
        self.assertEqual(kconfig.syms['PIN_SHARED_EXIT_SENSOR'].str_value, '^unit0:PA3')

    def test_shared_exit_stays_on_mmb_2_0_without_a_selector_stepper(self):
        kconfig = _kconfig('free_pico_mmu_mmb_2_0', dict(PICO_MMU, BOARD_TYPE_MMB_2_0=True))
        self.assertEqual(kconfig.syms['PIN_SHARED_EXIT_SENSOR'].str_value, '^unit0:PA15')

    def test_tzb_cutter_servo_stays_without_an_analog_buffer(self):
        kconfig = _kconfig('free_ercf_tzb_cutter', dict(
            ERCF, BOARD_TYPE_TZB_1_0=True, MMU_HAS_SERVO_CUTTER=True))
        self.assertEqual(kconfig.syms['PIN_SERVO_CUTTER_SERVO'].str_value, 'unit0:PA3')


class TestMovingTheWinnerFreesTheHeader(unittest.TestCase):
    """A user who resolved a clash by moving the winning pin keeps the other default."""

    CASES = {
        'moved_selector_endstop_mmb_2_0': (
            dict(TRADRACK, BOARD_TYPE_MMB_2_0=True, PIN_SELECTOR_ENDSTOP='^unit0:PC7'),
            {'PIN_SHARED_EXIT_SENSOR': '^unit0:PA15'}),
        'moved_entry_sensor_1_mmb_1_1': (
            dict(BOXTURTLE, BOARD_TYPE_MMB_1_1=True, PIN_ENTRY_SENSOR_1='unit0:PC0'),
            {'PIN_SHARED_EXIT_SENSOR': '^unit0:PA3'}),
        'moved_entry_sensors_mmb_1_0': (
            dict(BOXTURTLE, BOARD_TYPE_MMB_1_0=True, MMU_HAS_SENSOR_SHARED_EXIT=False,
                 PIN_ENTRY_SENSOR_1='unit0:PC0', PIN_ENTRY_SENSOR_2='unit0:PC1',
                 PIN_ENTRY_SENSOR_3='unit0:PC2'),
            {'PIN_GEAR_DIAG': 'unit0:PA3', 'PIN_GEAR_DIAG_2': 'unit0:PB9',
             'PIN_GEAR_DIAG_3': 'unit0:PB8'}),
        'moved_shared_exit_mmb_1_1': (
            dict(TRADRACK, BOARD_TYPE_MMB_1_1=True, PIN_SHARED_EXIT_SENSOR='^unit0:PC0'),
            {'PIN_GEAR_DIAG': 'unit0:PA3'}),
        'moved_encoder_mmb_1_1': (
            dict(BOXTURTLE, BOARD_TYPE_MMB_1_1=True, MMU_HAS_ENCODER=True, PIN_ENCODER='^unit0:PC0'),
            {'PIN_ESPOOLER_RWD_1': 'unit0:PA1'}),
        'moved_analog_buffer_tzb': (
            dict(ERCF, BOARD_TYPE_TZB_1_0=True, MMU_HAS_SYNC_FEEDBACK_BUFFER=True,
                 MMU_HAS_SENSOR_BUFFER_PROPORTIONAL=True, MMU_HAS_SERVO_CUTTER=True,
                 PIN_BUFFER_ANALOG='unit0:PA1'),
            {'PIN_SERVO_CUTTER_SERVO': 'unit0:PA3'}),
    }

    def test_lower_priority_pin_keeps_its_default(self):
        for label, (syms, expected) in self.CASES.items():
            with self.subTest(case=label):
                kconfig = _kconfig(label, syms)
                self.assertEqual({sym: kconfig.syms[sym].str_value for sym in expected}, expected)


if __name__ == '__main__':
    unittest.main()
