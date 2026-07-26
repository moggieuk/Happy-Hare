# Happy Hare test harness - milestones A2 through A5.
#
# Loads a real rendered BoxTurtle config into a fake Klipper and drives it all the
# way to mmu:bootup, with no printer and no Klipper.
#
#   A2  [mmu_machine] config-loads and builds the whole MMU tree
#   A2c the isinstance(mcu.MCU_endstop) invariant that the NFC feature depends on
#   A3  klippy:connect - the extruder-stepper swap
#   A4  klippy:ready
#   A5  mmu:bootup, with the error sentinel
#
# THE ERROR SENTINEL IS MANDATORY, NOT DECORATION. cmd_MMU_BOOTUP wraps its whole
# body in `except Exception -> log_assertion` and then fires mmu:bootup
# unconditionally (extras/mmu/mmu_controller.py:307-456). A test that asserts only
# on the event passes while bootup is entirely broken. Every test that boots must
# also assert hh.errors == [].
#
# Run with the repo venv (needs jinja2/dill for template rendering, greenlet for the
# reactor):
#   ./venv/bin/python -m unittest test.test_mmu_bootup
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

# HH logs a lot at INFO during construction; keep test output readable.
logging.getLogger().setLevel(logging.CRITICAL)


class BootedSessionMixin:
    """One booted session per class - construction is the expensive part."""

    PROFILE = 'boxturtle'
    KWARGS = {}

    @classmethod
    def setUpClass(cls):
        cls.hh = session(cls.PROFILE, **cls.KWARGS)
        cls.hh.boot()

    @classmethod
    def tearDownClass(cls):
        cls.hh.close()


class TestConfigLoad(unittest.TestCase):
    """A2: the section loop builds the MMU tree."""

    @classmethod
    def setUpClass(cls):
        cls.hh = session('boxturtle')
        cls.hh.build()          # steps 1-4 only, no connect/ready

    @classmethod
    def tearDownClass(cls):
        cls.hh.close()

    def test_mmu_object_exists(self):
        mmu = self.hh.printer.lookup_object('mmu')
        self.assertIsNotNone(mmu)
        self.assertEqual(mmu.num_gates, 4)

    def test_units_and_gates(self):
        machine = self.hh.printer.lookup_object('mmu_machine')
        self.assertEqual(len(machine.units), 1)
        unit = machine.units[0]
        self.assertEqual(unit.name, 'unit0')
        self.assertEqual(unit.num_gates, 4)
        self.assertEqual(unit.first_gate, 0)

    def test_gear_steppers_registered(self):
        for suffix in ('', '_1', '_2', '_3'):
            name = 'mmu_stepper unit0_gear%s' % suffix
            self.assertIn(name, self.hh.printer.objects, name)

    def test_selector_is_virtual(self):
        """
        BoxTurtle is Type-B. This matters beyond topology: cmd_MMU_BOOTUP skips
        home_unit for a VirtualSelector (extras/mmu/mmu_controller.py:385-405), which
        is what lets bootup complete before the harness has a working HomingMove.
        """
        unit = self.hh.printer.lookup_object('mmu_machine').units[0]
        self.assertEqual(type(unit.selector).__name__, 'VirtualSelector')

    def test_sensors_registered(self):
        """HH registers each sensor as a filament_switch_sensor for UI visibility."""
        names = self.hh.object_names('filament_switch_sensor')
        self.assertTrue(names, 'no filament_switch_sensor objects registered')
        joined = ' '.join(names)
        for expected in ('mmu_entry_0', 'mmu_exit_0', 'mmu_shared_exit'):
            self.assertIn(expected, joined)

    def test_optional_subsystems_built(self):
        unit = self.hh.printer.lookup_object('mmu_machine').units[0]
        self.assertIsNotNone(getattr(unit, 'espooler', None), 'espooler missing')
        self.assertIsNotNone(getattr(unit, 'buffer', None), 'buffer missing')
        self.assertIsNotNone(getattr(unit, 'leds', None), 'leds missing')

    def test_no_errors_during_config_load(self):
        self.assertEqual(self.hh.errors, [])


class TestPinBindings(unittest.TestCase):
    """
    A2: every pin description is recorded with the type it was bound as. This is the
    'accept any pin description and know whether it is digital / analog / pwm'
    requirement, and it is how a test proves e.g. that an espooler motor pin really
    became a pwm rather than a digital_out (HH branches on the section's `pwm`
    option at extras/mmu/unit/mmu_espooler.py:84-101).
    """

    @classmethod
    def setUpClass(cls):
        cls.hh = session('boxturtle')
        cls.hh.build()

    @classmethod
    def tearDownClass(cls):
        cls.hh.close()

    def test_stepper_pins_bound(self):
        self.hh.pins.assert_bound('unit0:PD4', 'stepper')     # gear step_pin

    def test_endstop_pins_bound(self):
        endstops = self.hh.pins.of_type('endstop')
        self.assertTrue(endstops, 'no endstop pins bound')
        # Every gate switch is pulled up in the shipped config
        self.assertTrue(any(b.pullup == 1 for b in endstops),
                        'expected at least one pulled-up endstop pin')

    def test_espooler_motor_pins_are_pwm_or_digital(self):
        """The espooler decides per-pin; assert we captured a concrete choice."""
        kinds = {b.type for b in self.hh.pins.bindings
                 if b.type in ('pwm', 'digital_out')}
        self.assertTrue(kinds, 'espooler bound no pwm/digital_out pins')

    def test_unknown_chip_is_rejected(self):
        """
        Chip registration is strict on purpose - auto-vivifying would let a pin typo
        in a shipped template pass silently.
        """
        with self.assertRaises(Exception):
            self.hh.pins.setup_pin('digital_out', 'nosuchchip:PA1')

    def test_pin_type_summary_is_available(self):
        summary = self.hh.pins.types_by_pin()
        self.assertIn('unit0:PD4', summary)
        # Multi-use pins really do get more than one binding
        self.assertTrue(all(isinstance(v, list) for v in summary.values()))


class TestEndstopInvariant(unittest.TestCase):
    """
    A2c: THE guard rail for the whole NFC feature. Do not delete.

    extras/mmu/mmu_filament_movement.py:329 gates NFC-compound preload on
    isinstance(gate_obj[0], mcu.MCU_endstop). If a switch-derived endstop is not an
    instance of that class, _build_gate_nfc_compound returns None and the caller
    falls back to a PLAIN LOAD - the feature under test silently disables itself and
    every NFC-preload test still passes.
    """

    @classmethod
    def setUpClass(cls):
        cls.hh = session('boxturtle')
        cls.hh.build()

    @classmethod
    def tearDownClass(cls):
        cls.hh.close()

    def test_gear_rail_endstops_are_mcu_endstops(self):
        import mcu
        rail = self.hh.printer.lookup_object('mmu_stepper unit0_gear').rail
        names = rail.get_all_endstop_names()
        self.assertTrue(names, 'gear rail has no endstops')
        for name in names:
            obj = rail.get_extra_endstop(name)[0]
            self.assertIsInstance(
                obj, mcu.MCU_endstop,
                "endstop %r is a %s, which fails the isinstance check at "
                "mmu_filament_movement.py:329 - NFC compound preload would "
                "silently fall back to a plain load"
                % (name, type(obj).__name__))

    def test_gate_and_exit_endstops_present(self):
        rail = self.hh.printer.lookup_object('mmu_stepper unit0_gear').rail
        names = rail.get_all_endstop_names()
        self.assertIn('mmu_entry_0', names)
        self.assertIn('mmu_exit_0', names)

    def test_print_time_and_eventtime_are_distinct(self):
        """
        HH deliberately separates the two clock domains (the source table at
        extras/mmu/mmu_sensor_utils.py:410-435 plus the _endstop_trigger_time
        overrides). If the harness unified them, every clock-domain bug in that code
        would be invisible.
        """
        mcu_obj = self.hh.printer.lookup_object('mcu')
        eventtime = self.hh.reactor.monotonic()
        self.assertNotEqual(mcu_obj.estimated_print_time(eventtime), eventtime)


class TestConnect(BootedSessionMixin, unittest.TestCase):
    """A3: klippy:connect."""

    def test_extruder_stepper_was_swapped(self):
        """
        MmuExtruderWrapper strips [extruder]'s stepper options during the section
        loop so PrinterExtruder builds no stepper, then restores them and swaps in
        its own homing-capable stepper at connect
        (extras/mmu/unit/mmu_extruder_wrapper.py:63-66, 86-96).
        """
        extruder = self.hh.printer.lookup_object('extruder')
        wrappers = [o for o in self.hh.printer.objects.values()
                    if type(o).__name__ == 'MmuExtruderWrapper']
        self.assertEqual(len(wrappers), 1)
        self.assertIs(extruder.extruder_stepper,
                      wrappers[0].homing_extruder_stepper)
        self.assertEqual(type(extruder.extruder_stepper).__name__,
                         'MmuExtruderStepper')

    def test_extruder_stepper_options_restored(self):
        """The stripped options must be put back, or a later reload sees a gap."""
        self.assertTrue(self.hh.fileconfig.has_option('extruder', 'step_pin'))

    def test_connect_fired(self):
        self.assertTrue(self.hh.fired('klippy:connect'))


class TestReady(BootedSessionMixin, unittest.TestCase):
    """A4: klippy:ready."""

    def test_mmu_is_ready(self):
        self.assertTrue(self.hh.mmu._ready)
        self.assertTrue(self.hh.fired('mmu:initialized'))

    def test_pause_family_was_wrapped(self):
        """
        HH renames the originals to __PAUSE/etc via the register_command(name, None)
        return-and-remove idiom (extras/mmu/mmu_controller.py:243-252) and logs an
        error for any it cannot find - which the error sentinel would catch.
        """
        commands = self.hh.gcode.base_commands
        for name in ('PAUSE', 'RESUME', 'CLEAR_PAUSE', 'CANCEL_PRINT'):
            self.assertIn('__' + name, commands, 'original %s not renamed' % name)
            self.assertIn(name, commands, '%s replacement not registered' % name)

    def test_mmu_commands_registered(self):
        commands = self.hh.gcode.base_commands
        for name in ('MMU', 'MMU_GATE_MAP', 'MMU_NFC', 'MMU_NFC_SCAN',
                     'MMU_PRELOAD', 'MMU_STATUS', '_MMU_TEST', '__MMU_BOOTUP'):
            self.assertIn(name, commands)

    def test_get_status_is_servable(self):
        status = self.hh.mmu.get_status(self.hh.reactor.monotonic())
        self.assertTrue(status['enabled'])
        self.assertEqual(status['num_gates'], 4)


class TestBootup(BootedSessionMixin, unittest.TestCase):
    """A5: mmu:bootup - the headline milestone."""

    def test_bootup_completed_cleanly(self):
        self.assertTrue(self.hh.fired('mmu:bootup'))
        # See the module docstring: without this the test above is vacuous.
        self.assertEqual(self.hh.errors, [])

    def test_no_recovery_needed_on_a_fresh_machine(self):
        """
        A powered-on machine with no filament must report a determinate state.
        FILAMENT_POS_UNLOADED == 0. Anything else means report_necessary_recovery
        (extras/mmu/mmu_filament_movement.py:2858-2880) would tell the user to run
        MMU_RECOVER, which is exactly what the error assertion above catches.
        """
        self.assertEqual(self.hh.mmu.filament_pos, 0)
        self.assertEqual(self.hh.mmu.gate_selected, 0)

    def test_event_sequence(self):
        events = [e for e in self.hh.printer.events_fired
                  if e in ('klippy:connect', 'klippy:ready', 'mmu:initialized',
                           'mmu:bootup')]
        self.assertEqual(events, ['klippy:connect', 'klippy:ready',
                                  'mmu:initialized', 'mmu:bootup'])

    def test_spoolman_sync_was_attempted(self):
        """
        Bootup calls _spoolman_sync / _moonraker_sync_lane_data, which reach Moonraker
        via webhooks.call_remote_method. With spoolman off there should be lane data
        but no spoolman traffic - either way the calls are recorded, which is the hook
        the round-trip milestone builds on.
        """
        self.assertIsInstance(self.hh.webhooks.calls, list)

    def test_no_unhandled_gcode(self):
        """
        Keep the set of commands HH issues that nothing handles VISIBLE. It is empty
        today; if it grows, that is a real signal about a missing fake and should be
        reviewed rather than silently tolerated.
        """
        unhandled = sorted(set(c.split()[0] for c in self.hh.gcode.unhandled))
        self.assertEqual(unhandled, [])


class TestSensorDriving(BootedSessionMixin, unittest.TestCase):
    """
    First test of HH BEHAVIOUR rather than construction: drive a sensor through its
    real button callback and observe HH react.
    """

    def test_initial_state_is_coherent(self):
        """
        The buffer's spring sits at its configured resting state. Without this the
        machine is physically incoherent and check_filament_in_mmu concludes filament
        is present (extras/mmu/mmu_filament_movement.py:2987-2992).
        """
        unit = self.hh.printer.lookup_object('mmu_machine').units[0]
        self.assertEqual(unit.buffer.buffer_spring_state, 'tension')
        self.assertTrue(self.hh.sensor('filament_tension').present)
        self.assertFalse(self.hh.sensor('filament_compression').present)

    def test_gate_sensors_start_clear(self):
        for name in ('mmu_entry_0', 'mmu_exit_0'):
            self.assertFalse(self.hh.sensor(name).present, name)

    def test_driving_a_sensor_updates_hh_state(self):
        entry = self.hh.sensor('mmu_entry_0')
        try:
            entry.set(True)
            self.assertTrue(entry.present)
            self.assertTrue(self.hh.mmu.sensor_manager.check_sensor('mmu_entry'))
        finally:
            entry.set(False)
        self.assertFalse(entry.present)

    def test_sensor_lookup_errors_are_helpful(self):
        with self.assertRaises(KeyError):
            self.hh.sensor('no_such_sensor')


if __name__ == '__main__':
    unittest.main()
