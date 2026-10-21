# The shared servo definition, components/Kconfig.servo.
#
# Every servo's [mmu_servo] options come from one fragment, sourced once per
# servo outside the caller's feature `if` and shown through a prompt condition
# (servo_visible). Three things can go wrong without any render changing, so
# they are pinned here:
#
# - a condition that fails to parse as one silently becomes a junk symbol, so
#   the prompts never show and a user-set value is dropped on the next save
# - an `if` block between the caller's toggle and the fragment breaks the
#   automatic submenu, so the prompts slide out from under the toggle
# - a default declared in the wrong place outranks a servo type's default

import os
import re
import tempfile
import unittest

import kconfiglib

from test.hh import cfg, profiles

ORDER = ('MIN_PULSE_WIDTH', 'MAX_PULSE_WIDTH', 'MAXIMUM_ANGLE')

SERVOS = {
    'MMU_HAS_TOOLHEAD_CUTTER': True, 'MMU_HAS_GANTRY_BUMPER_SERVO': True,
    'PIN_GANTRY_BUMPER_SERVO': 'unit0:PA3', 'MMU_HAS_SERVO_CUTTER': True,
    'PIN_SERVO_CUTTER_SERVO': 'unit0:PA4', 'MMU_HAS_BLOBIFIER': True,
    'PIN_BLOBIFIER_SERVO': 'unit0:PA1', 'MMU_HAS_BLOBIFIER_BUCKET_SWITCH': False,
}
VENT = {'MMU_HAS_VENT_SERVO': True, 'PIN_VENT_SERVO': 'unit0:PA9',
        'MMU_HAS_FANS': True, 'PIN_FAN': 'unit0:PA8'}
PER_GATE_VENT = dict({'MMU_HAS_VENT_SERVO': True, 'MMU_HAS_HEATER': True,
                      'PARAM_MAX_CONCURRENT_HEATERS': 2, 'PARAM_VENT_SERVO_GATE_4': False},
                     **{'PIN_VENT_SERVO_%d' % g: 'unit0_gate%d:PB15' % g for g in range(5)})

# servo -> (base profile, syms that enable it, the menu toggle its prompts nest under)
CASES = {
    'SERVO':           ('tradrack', {}, None),
    'VENT_SERVO':      ('qidi', VENT, None),
    'VENT_SERVO_1':    ('emu', PER_GATE_VENT, 'PARAM_VENT_SERVO_GATE_1'),
    'BLOBIFIER_SERVO': ('boxturtle', SERVOS, 'MMU_HAS_BLOBIFIER'),
    'GANTRY_SERVO':    ('boxturtle', SERVOS, 'MMU_HAS_GANTRY_BUMPER_SERVO'),
    'SERVO_CUTTER':    ('boxturtle', SERVOS, 'MMU_HAS_SERVO_CUTTER'),
}
HAS_INITIAL_ANGLE = ('BLOBIFIER_SERVO', 'GANTRY_SERVO', 'SERVO_CUTTER')

# servo -> syms that switch it off, while keeping the rest of its base profile
HIDDEN = {
    'SERVO':           ('boxturtle', {}),
    'VENT_SERVO':      ('qidi', {}),
    'VENT_SERVO_1':    ('emu', dict(PER_GATE_VENT, PARAM_VENT_SERVO_GATE_1=False)),
    'BLOBIFIER_SERVO': ('boxturtle', dict(SERVOS, CHOICE_BLOBIFIER_TYPE_STEPPER=True)),
    'GANTRY_SERVO':    ('boxturtle', dict(SERVOS, MMU_HAS_TOOLHEAD_CUTTER=False)),
    'SERVO_CUTTER':    ('boxturtle', dict(SERVOS, MMU_HAS_SERVO_CUTTER=False)),
}


def _kconfig(label, base, syms):
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        return cfg._kconfig(label, dict(profiles.get(base).syms, **syms))


def _names(servo, initial=False):
    prefix, _, gate = servo.rpartition('_') if servo[-1].isdigit() else (servo, '', '')
    suffix = '_' + gate if gate else ''
    names = ['PARAM_%s_%s%s' % (prefix, option, suffix) for option in ORDER]
    if initial:
        names += ['BOOL_%s_INITIAL_ANGLE' % prefix, 'PARAM_%s_INITIAL_ANGLE' % prefix]
    return names


def _prompt_node(sym):
    return next(node for node in sym.nodes if node.prompt and kconfiglib.expr_value(node.prompt[1]))


class TestServoPrompts(unittest.TestCase):

    def test_every_servo_shows_its_prompts_when_fitted(self):
        for servo, (base, syms, _) in sorted(CASES.items()):
            with self.subTest(servo=servo):
                kc = _kconfig('servo_shown_' + servo, base, syms)
                names = _names(servo, servo in HAS_INITIAL_ANGLE)[:4]
                self.assertEqual([n for n in names if not kc.syms[n].visibility], [])

    def test_no_servo_shows_its_prompts_when_not_fitted(self):
        for servo, (base, syms) in sorted(HIDDEN.items()):
            with self.subTest(servo=servo):
                kc = _kconfig('servo_hidden_' + servo, base, syms)
                names = _names(servo, servo in HAS_INITIAL_ANGLE)[:4]
                self.assertEqual([n for n in names if kc.syms[n].visibility], [])

    def test_prompts_are_in_the_same_order_under_the_callers_toggle(self):
        for servo, (base, syms, toggle) in sorted(CASES.items()):
            with self.subTest(servo=servo):
                kc = _kconfig('servo_order_' + servo, base, syms)
                names = _names(servo, servo in HAS_INITIAL_ANGLE)[:4]
                nodes = [_prompt_node(kc.syms[n]) for n in names]
                parents = {node.parent for node in nodes}
                self.assertEqual(len(parents), 1, 'prompts split across menus')
                if toggle:
                    self.assertIs(parents.pop().item, kc.syms[toggle])
                siblings = []
                node = nodes[0].parent.list
                while node:
                    siblings.append(node)
                    node = node.next
                self.assertEqual(sorted(nodes, key=siblings.index), nodes)

    def test_initial_angle_value_nests_under_its_toggle(self):
        for servo in HAS_INITIAL_ANGLE:
            with self.subTest(servo=servo):
                base, syms, _ = CASES[servo]
                bool_name, value_name = _names(servo, True)[3:]
                kc = _kconfig('servo_initial_' + servo, base, dict(syms, **{bool_name: True}))
                self.assertIs(_prompt_node(kc.syms[value_name]).parent.item, kc.syms[bool_name])

    def test_only_servos_that_offer_an_initial_angle_write_one(self):
        """The others still declare the two symbols, but they must stay inert."""
        kc = _kconfig('servo_inert', 'emu', PER_GATE_VENT)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '.mmu_config')
            kc.write_config(path, header='')
            with open(path) as handle:
                written = handle.read()
        for prefix in ('SERVO', 'VENT_SERVO'):
            with self.subTest(servo=prefix):
                pattern = r'CONFIG_(BOOL|PARAM)_%s_INITIAL_ANGLE(_\d+)?\b' % prefix
                self.assertEqual(re.findall(pattern, written), [])
                self.assertFalse(kc.syms['BOOL_%s_INITIAL_ANGLE' % prefix].visibility)

    def test_help_fits_the_menuconfig_help_window(self):
        for servo, (base, syms, _) in sorted(CASES.items()):
            kc = _kconfig('servo_help_' + servo, base, syms)
            for name in _names(servo, servo in HAS_INITIAL_ANGLE):
                with self.subTest(symbol=name):
                    help_text = next(node.help for node in kc.syms[name].nodes if node.help)
                    self.assertLessEqual(len(help_text.split('\n')), 7)
                    self.assertEqual(help_text, help_text.strip())


class TestServoValues(unittest.TestCase):

    def test_a_user_value_survives_a_save_and_reload(self):
        """A hidden prompt would make kconfiglib discard exactly these."""
        values = {'PARAM_BLOBIFIER_SERVO_MIN_PULSE_WIDTH': '0.0006',
                  'PARAM_GANTRY_SERVO_MAXIMUM_ANGLE': '270',
                  'PARAM_SERVO_CUTTER_INITIAL_ANGLE': '15',
                  'BOOL_SERVO_CUTTER_INITIAL_ANGLE': True}
        for base, syms, extra in (('boxturtle', SERVOS, values),
                                  ('qidi', VENT, {'PARAM_VENT_SERVO_MAX_PULSE_WIDTH': '0.0025'}),
                                  ('emu', PER_GATE_VENT, {'PARAM_VENT_SERVO_MAXIMUM_ANGLE_1': '270'})):
            with self.subTest(profile=base):
                kc = _kconfig('servo_saved_' + base, base, dict(syms, **extra))
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, '.mmu_config')
                    kc.write_config(path, header='')
                    with open(path) as handle:
                        written = handle.read()
                    with cfg._env(cfg._SINGLE_UNIT_ENV):
                        reloaded = cfg._new_kconfig('servo_reloaded_' + base)
                        reloaded.load_config(path, filter_defaults=True)
                for name, value in extra.items():
                    expected = 'y' if value is True else value
                    self.assertEqual(reloaded.syms[name].str_value, expected, name)
                    self.assertNotIn('CONFIG_%s=%s #~DEFAULT~#' % (name, expected), written)

    def test_a_servo_type_still_sets_the_selector_pulse_widths(self):
        """mmu_types/ -> servos/ is parsed before the component and must win."""
        unit = profiles.get('ercf_vvd').units[0]
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig('servo_type_mg90s', dict(unit.syms, SERVO_TYPE_MG_90S=True))
        self.assertEqual(kc.syms['PARAM_SERVO_MIN_PULSE_WIDTH'].str_value, '0.00085')
        self.assertEqual(kc.syms['PARAM_SERVO_MAX_PULSE_WIDTH'].str_value, '0.00240')

    def test_defaults_hold_while_the_prompts_are_hidden(self):
        kc = _kconfig('servo_defaults_hidden', 'boxturtle', {})
        self.assertEqual({name: kc.syms[name].str_value for name in (
            'PARAM_SERVO_MIN_PULSE_WIDTH', 'PARAM_VENT_SERVO_MAX_PULSE_WIDTH_3',
            'PARAM_BLOBIFIER_SERVO_MIN_PULSE_WIDTH', 'PARAM_GANTRY_SERVO_MAX_PULSE_WIDTH',
            'PARAM_SERVO_CUTTER_MAXIMUM_ANGLE', 'PARAM_SERVO_CUTTER_INITIAL_ANGLE')}, {
            'PARAM_SERVO_MIN_PULSE_WIDTH': '0.001', 'PARAM_VENT_SERVO_MAX_PULSE_WIDTH_3': '0.002',
            'PARAM_BLOBIFIER_SERVO_MIN_PULSE_WIDTH': '0.00053',
            'PARAM_GANTRY_SERVO_MAX_PULSE_WIDTH': '0.00225',
            'PARAM_SERVO_CUTTER_MAXIMUM_ANGLE': '180', 'PARAM_SERVO_CUTTER_INITIAL_ANGLE': '70'})


if __name__ == '__main__':
    unittest.main()
