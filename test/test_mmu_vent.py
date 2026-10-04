# Happy Hare enclosure venting tests: the generic vent servo option and the _MMU_VENT /
# _MMU_CLOSE_VENT macros the drying cycle calls through heater_vent_macro.
#
# The harness has no [delayed_gcode], so tests close the vent by calling _MMU_CLOSE_VENT,
# which is all the _MMU_VENT_CLOSE delayed gcode does.

import unittest

from test.hh import cfg, profiles, session

VENT_MACROS = ('_MMU_VENT', '_MMU_CLOSE_VENT')


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


class TestVentRender(unittest.TestCase):

    def test_vent_servo_is_rendered_per_unit(self):
        parser = cfg.assemble(cfg.render(_vent_profile()), macros=False)
        servo = dict(parser.items('mmu_servo unit0_vent_servo'))
        self.assertEqual(servo, {'pin': 'unit0:PA9', 'maximum_servo_angle': '180',
                                 'minimum_pulse_width': '0.001', 'maximum_pulse_width': '0.002'})

    def test_no_vent_servo_by_default(self):
        parser = cfg.assemble(cfg.render(profiles.get('qidi')), macros=False)
        self.assertFalse([s for s in parser.sections() if s.startswith('mmu_servo ')])

    def test_vent_vars_are_rendered_without_a_heater_or_servo(self):
        parser = cfg.assemble(cfg.render(profiles.get('boxturtle')), macros=False)
        self.assertEqual(dict(parser.items('gcode_macro _MMU_VENT_VARS')), {
            'description': 'Happy Hare enclosure venting macro configuration variables',
            'gcode': '',
            'variable_servo_open_angle': '90',
            'variable_servo_closed_angle': '0',
            'variable_servo_duration': '1.0',
            'variable_duration': '10',
            'variable_run_fan': '1',
        })

    def test_vent_servo_without_a_pin_is_warned(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('vent_no_pin', dict(profiles.get('qidi').syms, MMU_HAS_VENT_SERVO=True))
        self.assertTrue(kconfig.is_enabled('W31'))
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('vent_pin', dict(profiles.get('qidi').syms, MMU_HAS_VENT_SERVO=True,
                                                    PIN_VENT_SERVO='unit0:PA9'))
        self.assertFalse(kconfig.is_enabled('W31'))


class _VentSession(unittest.TestCase):

    PROFILE = None

    def setUp(self):
        self.hh = session(self.PROFILE or profiles.get('qidi'))
        self.addCleanup(self.hh.close)
        self.hh.boot()
        for alias in VENT_MACROS:
            self.hh.printer.harness_macro_effects[alias] = lambda macro, gcmd: macro.run_body(gcmd)
        self.unit = self.hh.mmu.mmu_unit(0)

    def _run(self, line):
        at = len(self.hh.gcode.executed)
        self.hh.run_gcode(line)
        self.assertEqual(self.hh.errors, [])
        return self.hh.gcode.executed[at + 1:]

    def _commands(self, executed, prefix):
        return [line for line in executed if line.startswith(prefix)]

    def _console_since(self, line):
        at = len(self.hh.console)
        self._run(line)
        # The console sometimes renders spaces as non-breaking spaces
        messages = [msg.replace('\xa0', ' ') for msg in self.hh.console[at:]]
        return [msg for msg in messages if 'MMU vent' in msg]

    def _state(self):
        return self.hh.printer.lookup_object('gcode_macro _MMU_VENT').variables['vent_state']


class TestVentWithoutServo(_VentSession):

    def test_reports_and_schedules_the_close_as_before(self):
        self.assertEqual(self._console_since('_MMU_VENT UNIT=unit0'), [
            'Opening MMU vent on unit0 for 10s (no vent servo configured, nothing to move)...'])
        executed = self._run('_MMU_VENT UNIT=unit0')
        self.assertEqual(self._commands(executed, 'UPDATE_DELAYED_GCODE'),
                         ['UPDATE_DELAYED_GCODE ID=_MMU_VENT_CLOSE DURATION=10'])
        self.assertEqual(self._commands(executed, 'SET_SERVO') + self._commands(executed, 'MMU_FAN'), [])

        self.assertEqual(self._console_since('_MMU_CLOSE_VENT'), [
            'Closing MMU vent (no vent servo configured, nothing to move)...'])
        executed = self._run('_MMU_CLOSE_VENT')
        self.assertEqual(self._commands(executed, 'SET_SERVO') + self._commands(executed, 'MMU_FAN'), [])

    def test_startup_close_is_silent(self):
        self.assertEqual(self._run('_MMU_CLOSE_VENT STARTUP=1'), [])

    def test_per_gate_vent_names_the_gates(self):
        self.assertEqual(self._console_since('_MMU_VENT UNIT=unit0 GATES=0,1'), [
            'Opening MMU vent on unit0 to dry filaments in gates 0, 1 for 10s '
            '(no vent servo configured, nothing to move)...'])


class TestVentFanWithoutServo(_VentSession):

    PROFILE = _vent_profile(MMU_HAS_VENT_SERVO=False)

    def test_managed_fan_is_left_alone(self):
        executed = self._run('_MMU_VENT UNIT=unit0')
        self.assertEqual(self._commands(executed, 'MMU_FAN'), [])
        self.assertEqual(self._state(), {})


class TestVentServoWithoutFan(_VentSession):

    PROFILE = _vent_profile(MMU_HAS_FANS=False)

    def test_reports_the_servo_move(self):
        self.assertEqual(self._console_since('_MMU_VENT UNIT=unit0'), [
            'Opening MMU vent on unit0 for 10s (vent servo to angle 90)...'])
        self.assertEqual(self._console_since('_MMU_CLOSE_VENT'), [
            'Closing MMU vent on unit0 (vent servo to angle 0)...'])

    def test_moves_the_servo_only(self):
        executed = self._run('_MMU_VENT UNIT=unit0')
        self.assertEqual(self._commands(executed, 'SET_SERVO'),
                         ['SET_SERVO SERVO=unit0_vent_servo ANGLE=90 DURATION=1.0'])
        self.assertEqual(self._commands(executed, 'MMU_FAN'), [])
        self.assertEqual(self._state(), {'unit0': []})

        executed = self._run('_MMU_CLOSE_VENT')
        self.assertEqual(self._commands(executed, 'SET_SERVO'),
                         ['SET_SERVO SERVO=unit0_vent_servo ANGLE=0 DURATION=1.0'])
        self.assertEqual(self._commands(executed, 'MMU_FAN'), [])
        self.assertEqual(self._state(), {})


class TestVentWithServo(_VentSession):

    PROFILE = _vent_profile()

    def _fan_mode(self):
        return self.hh.mmu.get_status(self.hh.reactor.monotonic())['fans'][0]['modes'][0]

    def test_opens_the_servo_and_runs_the_fan_then_restores(self):
        executed = self._run('_MMU_VENT UNIT=unit0')
        self.assertEqual(self._commands(executed, 'SET_SERVO'),
                         ['SET_SERVO SERVO=unit0_vent_servo ANGLE=90 DURATION=1.0'])
        self.assertEqual(self._commands(executed, 'MMU_FAN'), ['MMU_FAN UNIT=unit0 FAN_FORCED=1'])
        self.assertEqual(self._fan_mode(), 1)
        self.assertEqual(self._state(), {'unit0': [[-1, 2]]})

        executed = self._run('_MMU_CLOSE_VENT')
        self.assertEqual(self._commands(executed, 'SET_SERVO'),
                         ['SET_SERVO SERVO=unit0_vent_servo ANGLE=0 DURATION=1.0'])
        self.assertEqual(self._commands(executed, 'MMU_FAN'), ['MMU_FAN UNIT=unit0 FAN_FORCED=2'])
        self.assertEqual(self._fan_mode(), 2)
        self.assertEqual(self._state(), {})

    def test_reports_the_servo_and_fan(self):
        self.assertEqual(self._console_since('_MMU_VENT UNIT=unit0'), [
            'Opening MMU vent on unit0 for 10s (vent servo to angle 90, managed fan on)...'])
        self.assertEqual(self._console_since('_MMU_VENT UNIT=unit0'), [
            'Opening MMU vent on unit0 for 10s (vent servo to angle 90, managed fan stays on)...'])
        self.assertEqual(self._console_since('_MMU_CLOSE_VENT'), [
            'Closing MMU vent on unit0 (vent servo to angle 0, managed fan restored)...'])

    def test_restores_a_mode_set_at_runtime(self):
        self.hh.run_gcode('MMU_FAN FAN_FORCED=0')
        self._run('_MMU_VENT UNIT=unit0')
        self.assertEqual(self._fan_mode(), 1)
        self._run('_MMU_CLOSE_VENT')
        self.assertEqual(self._fan_mode(), 0)

    def test_a_second_open_keeps_the_original_mode(self):
        self._run('_MMU_VENT UNIT=unit0')
        executed = self._run('_MMU_VENT UNIT=unit0')
        self.assertEqual(self._commands(executed, 'MMU_FAN'), [])
        self._run('_MMU_CLOSE_VENT')
        self.assertEqual(self._fan_mode(), 2)

    def test_fan_can_be_left_off(self):
        self.hh.run_gcode('SET_GCODE_VARIABLE MACRO=_MMU_VENT_VARS VARIABLE=run_fan VALUE=0')
        self.assertEqual(self._console_since('_MMU_VENT UNIT=unit0'), [
            'Opening MMU vent on unit0 for 10s (vent servo to angle 90, managed fan not used as run_fan is 0)...'])
        self._run('_MMU_CLOSE_VENT')
        executed = self._run('_MMU_VENT UNIT=unit0')
        self.assertEqual(len(self._commands(executed, 'SET_SERVO')), 1)
        self.assertEqual(self._commands(executed, 'MMU_FAN'), [])
        executed = self._run('_MMU_CLOSE_VENT')
        self.assertEqual(len(self._commands(executed, 'SET_SERVO')), 1)
        self.assertEqual(self._commands(executed, 'MMU_FAN'), [])

    def test_single_unit_needs_no_unit_parameter(self):
        executed = self._run('_MMU_VENT')
        self.assertEqual(len(self._commands(executed, 'SET_SERVO')), 1)
        self.assertIn('unit0', self._state())

    def test_startup_closes_the_servo_only(self):
        executed = self._run('_MMU_CLOSE_VENT STARTUP=1')
        self.assertEqual(self._commands(executed, 'SET_SERVO'),
                         ['SET_SERVO SERVO=unit0_vent_servo ANGLE=0 DURATION=1.0'])
        self.assertEqual(self._commands(executed, 'MMU_FAN'), [])

    def test_drying_cycle_opens_the_vent(self):
        self.hh.run_gcode('MMU_HEATER DRY=1 TEMP=45 TIMER=60 VENT_INTERVAL=1')
        self.hh.settle()
        at = len(self.hh.gcode.executed)
        self.hh.reactor.advance(90)
        executed = self.hh.gcode.executed[at:]
        self.assertEqual(self._commands(executed, 'SET_SERVO'),
                         ['SET_SERVO SERVO=unit0_vent_servo ANGLE=90 DURATION=1.0'])
        self.assertEqual(self._fan_mode(), 1)



class TestVentPerGateFans(_VentSession):

    PROFILE = profiles.get('emu').derive(
        'emu_vent', syms={'MMU_HAS_VENT_SERVO': True, 'PIN_VENT_SERVO': 'unit0_gate0:PB15'})

    def test_runs_only_the_heated_gates_fans_then_restores_them(self):
        self.hh.run_gcode('MMU_FAN GATE=2 FAN_FORCED=0')
        executed = self._run('_MMU_VENT UNIT=unit0 GATES=1,2')
        self.assertEqual(self._commands(executed, 'MMU_FAN'), [
            'MMU_FAN UNIT=unit0 GATE=1 FAN_FORCED=1',
            'MMU_FAN UNIT=unit0 GATE=2 FAN_FORCED=1',
        ])
        self.assertEqual(self._state(), {'unit0': [[1, 2], [2, 0]]})

        executed = self._run('_MMU_CLOSE_VENT')
        self.assertEqual(self._commands(executed, 'MMU_FAN'), [
            'MMU_FAN UNIT=unit0 GATE=1 FAN_FORCED=2',
            'MMU_FAN UNIT=unit0 GATE=2 FAN_FORCED=0',
        ])
        modes = self.hh.mmu.get_status(self.hh.reactor.monotonic())['fans'][0]['modes']
        self.assertEqual(modes, [2, 2, 0, 2, 2])

    def test_reports_the_gates_fans(self):
        self.assertEqual(self._console_since('_MMU_VENT UNIT=unit0 GATES=1,2'), [
            'Opening MMU vent on unit0 to dry filaments in gates 1, 2 for 10s '
            '(vent servo to angle 90, managed fans on for gates 1, 2)...'])
        self.assertEqual(self._console_since('_MMU_CLOSE_VENT'), [
            'Closing MMU vent on unit0 (vent servo to angle 0, managed fans restored)...'])


if __name__ == '__main__':
    unittest.main()
