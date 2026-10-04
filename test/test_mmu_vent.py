# Happy Hare enclosure venting tests. The environment (heater) manager opens the vent every
# heater_vent_interval minutes while drying: it moves the unit's vent servo(s), can run the managed
# fan(s) and calls heater_vent_macro (if set) with OPEN=1, then reverses all of that after
# heater_vent_duration seconds.

import unittest

from test.hh import cfg, profiles, session
from test.hh.bootstrap import PRINTER_STUB
from test.hh.profiles import Profile, UnitProfile

VENT_INTERVAL = 1         # minutes; the countdown runs in 30 s environment checks

# The EMU's per-gate heaters are the user's own [heater_generic] sections
EMU_HEATERS_STUB = PRINTER_STUB + ''.join('''
[heater_generic unit0_heater%d]
heater_pin: unit0_gate%d:PA1
sensor_type: Generic 3950
sensor_pin: unit0_gate%d:PA2
control: watermark
min_temp: 0
max_temp: 100
''' % (g, g, g) for g in range(5))


def _vent_profile(**syms):
    base = {
        'MMU_HAS_VENT_SERVO': True,
        'PIN_VENT_SERVO': 'unit0:PA9',
        'MMU_HAS_FANS': True,
        'PIN_FAN': 'unit0:PA8',
    }
    base.update(syms)
    name = 'qidi_vent' + ''.join('_%s' % k.lower() for k in sorted(syms))
    return profiles.get('qidi').derive(name, syms=base)


def _per_gate_vent_profile():
    syms = {'MMU_HAS_VENT_SERVO': True, 'PARAM_VENT_SERVO_GATE_4': False, 'MMU_HAS_HEATER': True,
            'PARAM_MAX_CONCURRENT_HEATERS': 2}
    syms.update({'PIN_VENT_SERVO_%d' % g: 'unit0_gate%d:PB15' % g for g in range(5)})
    return profiles.get('emu').derive('emu_vent', syms=syms)


def _two_unit_profile():
    # Each unit has its own vent servo and its own vent settings
    def unit(name, index, angle, duration):
        syms = dict(profiles.get('qidi').syms, MMU_HAS_VENT_SERVO=True, PIN_VENT_SERVO='%s:PA9' % name,
                    PARAM_HEATER_VENT_OPEN_ANGLE=angle, PARAM_HEATER_VENT_DURATION=duration)
        return UnitProfile(name, syms=syms, index=index)
    return Profile('qidi_vent_two_units', syms={'MMU_UNITS': 'unit0,unit1'},
                   units=[unit('unit0', 0, 120, 20), unit('unit1', 1, 60, 40)])


class TestVentRender(unittest.TestCase):

    def test_shared_vent_servo_is_linked_from_the_unit(self):
        parser = cfg.assemble(cfg.render(_vent_profile()), macros=False)
        unit = dict(parser.items('mmu_unit unit0'))
        self.assertEqual(unit['vent_servo'], 'unit0_vent_servo')
        self.assertNotIn('vent_servos', unit)
        self.assertEqual(dict(parser.items('mmu_servo unit0_vent_servo')), {
            'pin': 'unit0:PA9', 'maximum_servo_angle': '180',
            'minimum_pulse_width': '0.001', 'maximum_pulse_width': '0.002'})
        params = dict(parser.items('mmu_unit_parameters unit0'))
        self.assertEqual({k: params[k] for k in (
            'heater_vent_duration', 'heater_vent_run_fan', 'heater_vent_open_angle',
            'heater_vent_close_angle', 'heater_vent_servo_duration', 'heater_vent_macro')}, {
            'heater_vent_duration': '10', 'heater_vent_run_fan': '1', 'heater_vent_open_angle': '90',
            'heater_vent_close_angle': '0', 'heater_vent_servo_duration': '1.0', 'heater_vent_macro': ''})

    def test_without_a_vent_servo(self):
        parser = cfg.assemble(cfg.render(profiles.get('qidi')), macros=False)
        self.assertFalse([s for s in parser.sections() if s.startswith('mmu_servo ')])
        self.assertNotIn('vent_servo', dict(parser.items('mmu_unit unit0')))
        params = dict(parser.items('mmu_unit_parameters unit0'))
        self.assertEqual((params['heater_vent_duration'], params['heater_vent_run_fan'],
                          params['heater_vent_macro']), ('10', '1', '_MMU_VENT'))
        self.assertNotIn('heater_vent_open_angle', params)

    def test_per_gate_vent_servos(self):
        parser = cfg.assemble(cfg.render(_per_gate_vent_profile()), macros=False)
        unit = dict(parser.items('mmu_unit unit0'))
        self.assertNotIn('vent_servo', unit)
        self.assertEqual([n.strip() for n in unit['vent_servos'].split(',')],
                         ['unit0_vent_servo%d' % g for g in range(4)] + [''])
        self.assertEqual(dict(parser.items('mmu_servo unit0_vent_servo2'))['pin'], 'unit0_gate2:PB15')
        self.assertNotIn('mmu_servo unit0_vent_servo4', parser.sections())

    def test_vent_servo_without_a_pin_is_warned(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            shared = cfg._kconfig('vent_no_pin', dict(profiles.get('qidi').syms, MMU_HAS_VENT_SERVO=True))
            per_gate = cfg._kconfig('vent_no_gate_pin', dict(
                _per_gate_vent_profile().syms, PIN_VENT_SERVO_1=''))
            fitted = cfg._kconfig('vent_pin', dict(profiles.get('qidi').syms, MMU_HAS_VENT_SERVO=True,
                                                   PIN_VENT_SERVO='unit0:PA9'))
        self.assertTrue(shared.is_enabled('W31'))
        self.assertTrue(per_gate.is_enabled('W31'))
        self.assertFalse(fitted.is_enabled('W31'))

    def test_each_unit_has_its_own_vent_servo_and_settings(self):
        rendered = cfg.render(_two_unit_profile())
        for unit, angle, duration in (('unit0', '120', '20'), ('unit1', '60', '40')):
            with self.subTest(unit=unit):
                hardware = cfg.assemble({'h': rendered['config/base/mmu_hardware_%s.cfg' % unit]}, macros=False)
                self.assertEqual(dict(hardware.items('mmu_unit %s' % unit))['vent_servo'], '%s_vent_servo' % unit)
                self.assertEqual(dict(hardware.items('mmu_servo %s_vent_servo' % unit))['pin'], '%s:PA9' % unit)
                params = cfg.assemble({'p': rendered['config/base/mmu_parameters_%s.cfg' % unit]}, macros=False)
                values = dict(params.items('mmu_unit_parameters %s' % unit))
                self.assertEqual((values['heater_vent_open_angle'], values['heater_vent_duration']),
                                 (angle, duration))

    def test_no_printer_wide_vent_settings(self):
        rendered = cfg.render(_vent_profile())
        self.assertNotIn('_MMU_VENT_VARS', rendered['config/base/mmu_macro_vars.cfg'])


class _VentSession(unittest.TestCase):

    PROFILE = None
    SESSION = {}

    def setUp(self):
        self.hh = session(self.PROFILE or profiles.get('qidi'), **self.SESSION)
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.hh.settle()
        self.unit = self.hh.mmu.mmu_unit(0)
        self.manager = self.unit.environment_manager
        self.macro = self.hh.printer.lookup_object('gcode_macro _MMU_VENT')

    def _messages(self, at):
        # The console sometimes renders spaces as non-breaking spaces
        messages = [msg.replace('\xa0', ' ') for msg in self.hh.console[at:]]
        return [msg for msg in messages if 'MMU vent' in msg]

    def _dry(self, gates=None):
        cmd = 'MMU_HEATER DRY=1 TEMP=45 TIMER=60 VENT_INTERVAL=%d' % VENT_INTERVAL
        if gates:
            cmd += ' GATES=%s' % ','.join(map(str, gates))
        self.hh.run_gcode(cmd)
        self.hh.settle()

    def _open(self):
        at = len(self.hh.console)
        for _ in range(VENT_INTERVAL * 60 + 60):
            self.hh.reactor.advance(1)
            if self._messages(at):
                break
        return self._messages(at)

    def _close(self):
        at = len(self.hh.console)
        self.hh.reactor.advance(self.unit.p.heater_vent_duration + 1)
        return self._messages(at)

    def _servo(self, name='mmu_servo unit0_vent_servo'):
        return self.hh.printer.lookup_object(name)

    def _servo_values(self, name='mmu_servo unit0_vent_servo', since=0):
        return [value for _, value in self._servo(name).mcu_servo.timeline[since:]]

    def _pwm(self, angle, name='mmu_servo unit0_vent_servo'):
        return self._servo(name)._get_pwm_from_angle(angle)

    def _fan_modes(self):
        return self.unit.fan_manager.get_status()['modes']


class TestVentServoAndFan(_VentSession):

    PROFILE = _vent_profile()

    def test_drying_opens_then_closes_the_vent(self):
        since = len(self._servo().mcu_servo.timeline)
        self._dry()
        self.assertEqual(self._open(), [
            'Opening MMU vent on unit0 for 10s (vent servo to angle 90, managed fan on)...'])
        self.assertEqual(self._servo_values(since=since), [self._pwm(90), 0.0])
        self.assertEqual(self._fan_modes(), [1])

        self.assertEqual(self._close(), [
            'Closing MMU vent on unit0 (vent servo to angle 0, managed fan restored)...'])
        self.assertEqual(self._servo_values(since=since), [self._pwm(90), 0.0, self._pwm(0), 0.0])
        self.assertEqual(self._fan_modes(), [2])
        self.assertEqual(self.hh.errors, [])

    def test_no_macro_by_default_with_a_servo(self):
        self.assertEqual(self.unit.p.heater_vent_macro, '')
        self._dry()
        self._open()
        self._close()
        self.assertEqual(self.macro.calls, [])

    def test_macro_is_called_on_open_and_close(self):
        self.unit.p.heater_vent_macro = '_MMU_VENT'
        self._dry()
        self.assertEqual(self._open(), [
            'Opening MMU vent on unit0 for 10s (vent servo to angle 90, managed fan on, calling _MMU_VENT)...'])
        self._close()
        self.assertEqual(self.macro.calls, ['_MMU_VENT UNIT=unit0 OPEN=1', '_MMU_VENT UNIT=unit0 OPEN=0'])

    def test_restores_a_fan_mode_set_at_runtime(self):
        self.hh.run_gcode('MMU_FAN FAN_FORCED=0')
        self._dry()
        self._open()
        self.assertEqual(self._fan_modes(), [1])
        self._close()
        self.assertEqual(self._fan_modes(), [0])

    def test_fan_can_be_left_alone(self):
        self.unit.p.heater_vent_run_fan = 0
        self._dry()
        self.assertEqual(self._open(), [
            'Opening MMU vent on unit0 for 10s (vent servo to angle 90, '
            'managed fan not used as heater_vent_run_fan is 0)...'])
        self.assertEqual(self._fan_modes(), [2])
        self.assertEqual(self._close(), ['Closing MMU vent on unit0 (vent servo to angle 0)...'])

    def test_zero_servo_duration_keeps_the_servo_driven(self):
        self.unit.p.heater_vent_servo_duration = 0.
        since = len(self._servo().mcu_servo.timeline)
        self._dry()
        self._open()
        self._close()
        self.assertEqual(self._servo_values(since=since), [self._pwm(90), self._pwm(0)])

    def test_reopening_keeps_the_original_fan_mode(self):
        self._dry()
        self._open()
        at = len(self.hh.console)
        self.manager._vent_open()
        self.assertEqual(self._messages(at), [
            'Opening MMU vent on unit0 for 10s (vent servo to angle 90, managed fan stays on)...'])
        self._close()
        self.assertEqual(self._fan_modes(), [2])

    def test_stopping_drying_closes_the_vent(self):
        self._dry()
        self._open()
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_HEATER STOP=1')
        self.assertEqual(self._messages(at), [
            'Closing MMU vent on unit0 (vent servo to angle 0, managed fan restored)...'])
        self.assertEqual(self._fan_modes(), [2])
        self.assertEqual(self._close(), [])

    def test_heater_status_reports_venting(self):
        self._dry()
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_HEATER')
        status = '\n'.join(self.hh.console[at:]).replace('\xa0', ' ')
        self.assertRegex(status, r'Venting operational \(opening for 10s every .*; vent servo, managed fan\)')

    def test_vent_servo_is_closed_at_startup(self):
        self.assertEqual(self._servo_values()[:2], [self._pwm(0), 0.0])

    def test_unit_status_lists_the_vent_servo(self):
        status = self.hh.printer.lookup_object('mmu_machine').get_status(0)
        self.assertEqual(status['unit_0']['vent_servos'], ['mmu_servo unit0_vent_servo'])


class TestVentFanWithoutServo(_VentSession):

    PROFILE = _vent_profile(MMU_HAS_VENT_SERVO=False)

    def test_runs_the_fan_and_calls_the_macro(self):
        self._dry()
        self.assertEqual(self._open(), [
            'Opening MMU vent on unit0 for 10s (no vent servo, managed fan on, calling _MMU_VENT)...'])
        self.assertEqual(self._fan_modes(), [1])
        self.assertEqual(self._close(), [
            'Closing MMU vent on unit0 (no vent servo, managed fan restored, calling _MMU_VENT)...'])
        self.assertEqual(self._fan_modes(), [2])
        self.assertEqual(self.macro.calls, ['_MMU_VENT UNIT=unit0 OPEN=1', '_MMU_VENT UNIT=unit0 OPEN=0'])


class TestVentPerGate(_VentSession):

    PROFILE = _per_gate_vent_profile()
    SESSION = {'printer_stub': EMU_HEATERS_STUB}

    def test_only_the_heated_gates_vents_and_fans_open(self):
        names = ['mmu_servo unit0_vent_servo%d' % g for g in range(4)]
        since = {name: len(self._servo(name).mcu_servo.timeline) for name in names}
        self.unit.p.heater_vent_macro = '_MMU_VENT'
        self.hh.run_gcode('MMU_FAN GATE=2 FAN_FORCED=0')
        self._dry(gates=[1, 2])
        self.assertEqual(self._open(), [
            'Opening MMU vent on unit0 to dry filaments in gates 1, 2 for 10s '
            '(vent servos to angle 90, managed fans on for gates 1, 2, calling _MMU_VENT)...'])
        moved = [name for name in names if self._servo_values(name, since[name])]
        self.assertEqual(moved, names[1:3])
        self.assertEqual(self._fan_modes()[:3], [2, 1, 1])
        self.assertEqual(self.macro.calls, ['_MMU_VENT UNIT=unit0 OPEN=1 GATES=1,2'])

        self._close()
        self.assertEqual(self._fan_modes()[:3], [2, 2, 0])
        self.assertEqual(self.macro.calls[-1], '_MMU_VENT UNIT=unit0 OPEN=0 GATES=1,2')


class TestVentTwoUnits(_VentSession):

    PROFILE = _two_unit_profile()

    def test_each_unit_vents_with_its_own_settings(self):
        servos = {u: 'mmu_servo %s_vent_servo' % u for u in ('unit0', 'unit1')}
        for unit, name in servos.items():
            with self.subTest(unit=unit):
                self.assertEqual(self._servo_values(name)[:2], [self._pwm(0, name), 0.0])
        since = {u: len(self._servo(n).mcu_servo.timeline) for u, n in servos.items()}

        at = len(self.hh.console)
        for unit in servos:
            self.hh.run_gcode('MMU_HEATER UNIT=%s DRY=1 TEMP=45 TIMER=60 VENT_INTERVAL=1' % unit)
        self.hh.settle()
        for _ in range(120):
            self.hh.reactor.advance(1)
            if len([m for m in self._messages(at) if m.startswith('Opening')]) == 2:
                break
        self.assertEqual(sorted(m for m in self._messages(at) if m.startswith('Opening')), [
            'Opening MMU vent on unit0 for 20s (vent servo to angle 120)...',
            'Opening MMU vent on unit1 for 40s (vent servo to angle 60)...'])

        # unit0 closes after its 20 s while unit1 stays open for its 40 s
        self.hh.reactor.advance(25)
        closing = [m for m in self._messages(at) if m.startswith('Closing')]
        self.assertEqual(closing, ['Closing MMU vent on unit0 (vent servo to angle 0)...'])
        self.hh.reactor.advance(20)
        closing = [m for m in self._messages(at) if m.startswith('Closing')]
        self.assertEqual(closing[1:], ['Closing MMU vent on unit1 (vent servo to angle 0)...'])
        for unit, angle in (('unit0', 120), ('unit1', 60)):
            name = servos[unit]
            with self.subTest(unit=unit):
                self.assertEqual(self._servo_values(name, since[unit]),
                                 [self._pwm(angle, name), 0.0, self._pwm(0, name), 0.0])
        self.assertEqual(self.hh.errors, [])


class TestShippedVentMacro(_VentSession):

    def test_only_logs_the_call(self):
        self.hh.printer.harness_macro_effects['_MMU_VENT'] = lambda macro, gcmd: macro.run_body(gcmd)
        at = len(self.hh.gcode.executed)
        self.hh.run_gcode('_MMU_VENT UNIT=unit0 OPEN=1 GATES=1,2')
        executed = self.hh.gcode.executed[at + 1:]
        self.assertEqual(len(executed), 1, executed)
        self.assertIn('_MMU_VENT called for unit0 to open the vent for gates 1, 2', executed[0])
        self.assertEqual(self.hh.errors, [])


if __name__ == '__main__':
    unittest.main()
