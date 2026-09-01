# Happy Hare test harness - machine profile breadth.
#
# Everything else in the suite runs on BoxTurtle. This file is the regression net for
# CONFIG BREADTH: it boots genuinely different machines, so a renamed parameter, a broken
# [% if %] guard or a missing template section shows up here rather than on a user's
# printer.
#
# There are 19 shipped machine types. Nine profiles boot in the harness today:
#
#   boxturtle  4 gates,  VirtualSelector       - Type B, the default everywhere else
#   tradrack  10 gates,  LinearServoSelector   - a PHYSICAL selector, so the suite is not
#                                                shaped around one selector type
#   3ms        4 gates,  VirtualSelector       - zero-length Bowden; gate homing aliases the
#                                                extruder-entry sensor as mmu_shared_exit
#   chameleon  4 gates,  RotarySelector        - a fourth selector class, and the only one
#                                                with no servo: releasing drives the carriage
#                                                to the OPPOSING gate's offset
#   pico_mmu   4 gates,  ServoSelector         - boots uncalibrated because its 140-degree
#                                                servo has no universally safe gate defaults
#   mmx        4 gates,  ServoSelector         - vendor-supplied gate angles, including a
#                                                full load/unload behavioral test
#   kms        4 gates,  VirtualSelector       - default KMS buffer with per-gate exit sensors
#   qidi       4 gates,  VirtualSelector       - fixed QIDI v2 board, shared hub sensor,
#                                                THR-hosted extruder sensor and stock dryer
#   emu        5 gates,  VirtualSelector       - the only shipped profile with a
#                                                PROPORTIONAL (analog) buffer sensor
#   emu_ebb    5 gates,  VirtualSelector       - EMU on per-gate EBB36/42 gen1 boards,
#                                                with one shared exit LED chain
#   ercf 1.1   9 gates,  LinearServoSelector   - unit0 of ercf_vvd; encoder gate homing
#   vvd 1.0    4 gates,  IndexedSelector       - unit1 of ercf_vvd; a third selector class
#
# The last two arrive together as `ercf_vvd`, the only MULTI-UNIT profile (13 gates across
# two units). Getting them in closed all three of the buckets this comment used to list as
# blockers, because a real user's config supplied exactly what was missing:
#
#   "13 need the machine x board pin selection" - a board choice is all that was needed;
#       ercf_vvd carries BOARD_TYPE_ERB_1 and BOARD_TYPE_VVD_1_0.
#   "2 need a heater_generic fake" - added (klippy_root/extras/heater_generic.py); ViViD
#       and KMS now both boot with their real heater configuration.
#   "1 needs an unselected choice param" - the MMU serial device, whose symbol NAME comes
#       from `ls /dev/serial/by-id/*` (Kconfig:112-116) and so does not exist on a host with
#       no MMU attached. The answer is to omit it: the choice falls back to ..._OTHER and
#       the harness fakes every [mcu] anyway.
#
# The remaining machines should mostly be a board selection away.
#
#   ./venv/bin/python -m unittest test.test_mmu_profiles
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import re
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

# profile -> (gates, selector class name of the FIRST unit)
BOOTABLE = {
    'boxturtle': (4, 'VirtualSelector'),
    'tradrack': (10, 'LinearServoSelector'),
    '3ms': (4, 'VirtualSelector'),
    # Needs two symbols the vendor Kconfig leaves open (a board and a gate homing sensor) or it
    # does not render at all - see the profile's own comment in test/hh/profiles.py.
    'chameleon': (4, 'RotarySelector'),
    'pico_mmu': (4, 'ServoSelector'),
    'mmx': (4, 'ServoSelector'),
    'kms': (4, 'VirtualSelector'),
    'qidi': (4, 'VirtualSelector'),
    'emu': (5, 'VirtualSelector'),
    'emu_ebb': (5, 'VirtualSelector'),
    # The only multi-unit entry. 13 is a CROSS-UNIT SUM (unit0 9 + unit1 4), not one unit's
    # count, and the selector named here is unit0's - unit1 is an IndexedSelector and gets
    # its own assertions in TestMultiUnitMachine below.
    'ercf_vvd': (13, 'LinearServoSelector'),
}

# Selector classes reached only through a non-first unit, so the BOOTABLE table above cannot
# name them. Keep in step with TestSelectorCoverage.
EXERCISED_BY_LATER_UNITS = {'IndexedSelector'}

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_LOADED = 10
TIP_AT_GATE = -40.0


class TestEveryBootableProfile(unittest.TestCase):
    """
    One boot per machine. Deliberately built as separate test methods rather than a loop
    so a failure names the machine that broke.
    """

    def _boot(self, name):
        hh = session(name)
        self.addCleanup(hh.close)       # runs even if an assertion fails
        hh.boot()
        return hh

    def _check(self, name):
        gates, selector = BOOTABLE[name]
        hh = self._boot(name)
        self.assertTrue(hh.fired('mmu:bootup'), '%s never reached bootup' % name)
        self.assertEqual(hh.errors, [], '%s booted with errors' % name)
        self.assertEqual(hh.mmu.num_gates, gates)
        unit = hh.mmu.mmu_unit(0)
        self.assertEqual(type(unit.selector).__name__, selector)
        return hh

    def test_boxturtle(self):
        hh = self._check('boxturtle')
        unit = hh.mmu.mmu_unit(0)
        self.assertEqual(unit.p.gate_homing_endstop, 'mmu_shared_exit')
        self.assertEqual(unit.p.gate_homing_max, 300)
        self.assertEqual(unit.p.gate_parking_distance, -100)
        self.assertEqual(unit.p.gate_preload_endstop, 'mmu_exit')
        self.assertEqual(unit.p.gate_preload_homing_max, 200)
        self.assertEqual(unit.p.gate_preload_parking_distance, 10)
        self.assertEqual(unit.p.sync_gear_current, 70)

        model = hh.filament()
        self.assertEqual(model.layout['mmu_exit'], 0)
        self.assertEqual(model.layout['mmu_shared_exit'], 150)
        hh.place_filament(0, position=-40)
        hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertAlmostEqual(model.tip[0], 10)
        self.assertTrue(model.triggered('mmu_exit_0'))
        self.assertFalse(model.triggered('unit0:mmu_shared_exit'))

    def test_boxturtle_nfc_defaults_are_valid_for_its_split_endstops(self):
        from test.hh import profiles
        profile = profiles.Profile(
            'boxturtle_nfc_defaults',
            syms={
                'MMU_TYPE_BOX_TURTLE_1_0': True,
                'MMU_HAS_NFC_READER': True,
                'MMU_HAS_COMMON_NFC_READER': True,
            })
        hh = session(profile)
        self.addCleanup(hh.close)
        hh.boot()
        unit = hh.mmu.mmu_unit(0)
        self.assertEqual(unit.p.gate_homing_endstop, 'mmu_shared_exit')
        self.assertEqual(unit.p.gate_preload_endstop, 'mmu_exit')
        self.assertEqual(unit.p.nfc_gate_clear_distance, -70)
        self.assertEqual(unit.p.nfc_preload_clear_distance, 70)
        self.assertEqual(hh.errors, [])

    def test_tradrack(self):
        """
        A physical selector, which matters: it takes a different construction path from
        BoxTurtle's VirtualSelector and gets no coverage anywhere else.
        """
        hh = self._check('tradrack')
        self.assertTrue(hasattr(hh.mmu.mmu_unit(0).selector, 'selector_stepper'))

    def test_3ms_load_unload_round_trip(self):
        hh = self._check('3ms')
        unit = hh.mmu.mmu_unit(0)
        self.assertFalse(unit.require_bowden_move)
        self.assertEqual(unit.calibrator.get_bowden_length(), 0)
        self.assertEqual(unit.p.gate_homing_endstop, 'extruder')
        # Preload must cover the complete gate-to-extruder path. Once parked,
        # ordinary gate homing only has to cover the 250 mm parking offset.
        self.assertEqual(unit.p.gate_homing_max, 500)
        self.assertEqual(unit.p.gate_parking_distance, -250)
        self.assertEqual(unit.p.gate_preload_homing_max, 1500)
        self.assertEqual(unit.p.gate_preload_parking_distance, -250)
        self.assertEqual(unit.p.gate_final_eject_distance, 1500)
        extruder_sensor = unit.toolhead_wrapper.sensors['extruder']
        self.assertIsNotNone(extruder_sensor)
        for gate_sensors in hh.mmu.sensor_manager.gate_sensors:
            self.assertIs(gate_sensors['extruder'], extruder_sensor)
            self.assertIs(gate_sensors['mmu_shared_exit'], extruder_sensor)

        # Exercise the complete zero-Bowden choreography, not just construction of
        # the alias: preload parks behind the extruder sensor, load homes straight
        # back to it and enters the toolhead, then unload returns to the same park.
        # A zero load buffer is deliberately smaller than the 8 mm entry-to-extruder
        # offset. It used to make the adjusted buffer negative and turn subtracting
        # that buffer into an unintended positive "fast Bowden" move.
        unit.p.bowden_load_homing_buffer = 0.
        load_bowden_results = []
        unload_bowden_results = []
        load_telemetry = []
        unload_telemetry = []
        unload_extruder_results = []
        state_transitions = []
        original_load_bowden = hh.mmu._load_bowden
        original_unload_bowden = hh.mmu._unload_bowden
        original_unload_extruder = hh.mmu._unload_extruder

        def record_load_bowden(*args, **kwargs):
            result = original_load_bowden(*args, **kwargs)
            load_bowden_results.append(result)
            return result

        def record_unload_bowden(*args, **kwargs):
            result = original_unload_bowden(*args, **kwargs)
            unload_bowden_results.append(result)
            return result

        def record_unload_extruder(*args, **kwargs):
            result = original_unload_extruder(*args, **kwargs)
            unload_extruder_results.append(result)
            return result

        hh.mmu._load_bowden = record_load_bowden
        hh.mmu._unload_bowden = record_unload_bowden
        hh.mmu._unload_extruder = record_unload_extruder
        unit.calibrator.note_load_telemetry = lambda *args: load_telemetry.append(args)
        unit.calibrator.note_unload_telemetry = lambda *args: unload_telemetry.append(args)

        model = hh.filament()
        hh.place_filament(0, position=TIP_AT_GATE)
        hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertAlmostEqual(model.tip[0], TIP_AT_GATE)

        from extras.mmu.mmu_constants import (
            FILAMENT_POS_HOMED_ENTRY,
            FILAMENT_POS_HOMED_EXTRUDER,
            FILAMENT_POS_IN_EXTRUDER,
            GATE_AVAILABLE,
        )
        original_set_filament_pos_state = hh.mmu.set_filament_pos_state

        def record_filament_pos_state(state, *args, **kwargs):
            changed = hh.mmu.filament_pos != state
            result = original_set_filament_pos_state(state, *args, **kwargs)
            if changed:
                state_transitions.append(state)
            return result

        hh.mmu.set_filament_pos_state = record_filament_pos_state

        hh.heat_extruder(220)
        hh.mmu.select_gate(0)
        hh.run_gcode('MMU_LOAD')
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.assertGreater(model.tip[0], model.layout['extruder'])
        self.assertEqual(state_transitions, [
            FILAMENT_POS_HOMED_ENTRY,
            FILAMENT_POS_HOMED_EXTRUDER,
            FILAMENT_POS_LOADED,
        ])

        state_transitions.clear()
        hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertAlmostEqual(model.tip[0], TIP_AT_GATE, places=3)
        self.assertEqual(state_transitions, [
            FILAMENT_POS_IN_EXTRUDER,
            FILAMENT_POS_HOMED_ENTRY,
            FILAMENT_POS_UNLOADED,
        ])
        self.assertEqual(load_bowden_results, [])
        self.assertEqual(unload_bowden_results, [])
        self.assertEqual(len(unload_extruder_results), 1)
        self.assertIs(unload_extruder_results[0][2], True)
        self.assertGreaterEqual(load_telemetry[0][2], 0.)
        self.assertEqual(unload_telemetry[0][2], 0.)
        self.assertEqual(hh.mmu.gate_status[0], GATE_AVAILABLE)

        # mmu_shared_exit is an implementation alias for the extruder sensor,
        # not a second physical switch. The parked filament should occupy its
        # dedicated lane up to one clear sensor in the visual representation.
        unloaded_visual = hh.mmu.get_filament_position_string()
        self.assertEqual(unloaded_visual.count('◯'), 1)
        self.assertGreater(unloaded_visual.split('◯', 1)[0].count('■'), 20)
        self.assertEqual(hh.errors, [])

    def test_3ms_unload_rehomes_when_extruder_datum_was_not_observed(self):
        hh = self._check('3ms')
        model = hh.filament()
        hh.place_filament(0, position=TIP_AT_GATE)
        hh.run_gcode('MMU_PRELOAD GATE=0')
        hh.heat_extruder(220)
        hh.mmu.select_gate(0)
        hh.run_gcode('MMU_LOAD')

        # Model the defensive path in _unload_extruder(): the entry sensor is
        # already reported clear, so HOMED_ENTRY is only an assumption and must
        # not be used as a precise reference for a direct parking move.
        check_sensor = hh.mmu.sensor_manager.check_sensor

        def report_extruder_clear(sensor):
            if sensor == 'extruder':
                return False
            return check_sensor(sensor)

        direct_parks = []
        fallback_homes = []
        fallback_homed_states = []
        park_at_gate = hh.mmu._park_at_gate
        unload_gate = hh.mmu._unload_gate
        set_gate_homed_state = hh.mmu._set_gate_homed_state

        def record_direct_park(*args, **kwargs):
            direct_parks.append((args, kwargs))
            return park_at_gate(*args, **kwargs)

        def record_fallback_home(*args, **kwargs):
            fallback_homes.append((args, kwargs))
            return unload_gate(*args, **kwargs)

        def record_fallback_homed_state(*args, **kwargs):
            result = set_gate_homed_state(*args, **kwargs)
            fallback_homed_states.append(hh.mmu.filament_pos)
            return result

        hh.mmu.sensor_manager.check_sensor = report_extruder_clear
        hh.mmu._park_at_gate = record_direct_park
        hh.mmu._unload_gate = record_fallback_home
        hh.mmu._set_gate_homed_state = record_fallback_homed_state
        hh.run_gcode('MMU_UNLOAD')

        from extras.mmu.mmu_constants import FILAMENT_POS_HOMED_ENTRY
        self.assertEqual(direct_parks, [])
        self.assertEqual(len(fallback_homes), 1)
        self.assertEqual(fallback_homed_states, [FILAMENT_POS_HOMED_ENTRY])
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertAlmostEqual(model.tip[0], TIP_AT_GATE, places=3)
        self.assertEqual(hh.errors, [])

    def test_3ms_recovery_uses_extruder_state_for_shared_entry_sensor(self):
        hh = self._check('3ms')
        model = hh.filament()
        hh.mmu.select_gate(0)

        from extras.mmu.mmu_constants import (
            FILAMENT_POS_IN_EXTRUDER,
            FILAMENT_POS_UNKNOWN,
        )

        # The shared entry sensor cannot prove how far filament extends into the
        # toolhead. Treat a trigger conservatively so a subsequent unload starts
        # with extraction, even when recovery is not permitted to heat and probe.
        hh.place_filament(0, position=model.layout['extruder_entry'])
        hh.mmu.set_filament_pos_state(FILAMENT_POS_UNKNOWN, silent=True)
        hh.mmu.recover_filament_pos(strict=True, can_heat=False, silent=True)
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_IN_EXTRUDER)

        # A clear shared entry sensor, with no other filament detection, is the
        # normal parked state and must still recover as fully unloaded.
        hh.place_filament(0, position=TIP_AT_GATE)
        hh.mmu.set_filament_pos_state(FILAMENT_POS_UNKNOWN, silent=True)
        hh.mmu.recover_filament_pos(strict=True, can_heat=False, silent=True)
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertEqual(hh.errors, [])

    def test_extruder_gate_datum_does_not_change_bowden_unit_state(self):
        hh = self._check('3ms')
        unit = hh.mmu.mmu_unit(0)
        unit.require_bowden_move = True
        hh.place_filament(0, position=TIP_AT_GATE)
        hh.mmu.select_gate(0)
        hh.mmu._load_gate()

        from extras.mmu.mmu_constants import (
            FILAMENT_POS_HOMED_ENTRY,
            FILAMENT_POS_HOMED_GATE,
            FILAMENT_POS_UNKNOWN,
        )
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_HOMED_GATE)

        # The no-Bowden recovery override must not leak into a conventional
        # Bowden unit: the same triggered sensor retains the pre-change
        # HOMED_ENTRY result when a heated extruder check is unavailable.
        hh.mmu.set_filament_pos_state(FILAMENT_POS_UNKNOWN, silent=True)
        hh.mmu.recover_filament_pos(strict=True, can_heat=False, silent=True)
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_HOMED_ENTRY)

    def test_bowden_homing_buffers_reject_negative_config(self):
        from test.hh import profiles

        for parameter in (
                'PARAM_BOWDEN_LOAD_HOMING_BUFFER',
                'PARAM_BOWDEN_UNLOAD_HOMING_BUFFER'):
            with self.subTest(parameter=parameter):
                profile = profiles.get('3ms').derive(
                    '3ms_negative_%s' % parameter.lower(),
                    syms={parameter: -1})
                hh = session(profile)
                self.addCleanup(hh.close)
                with self.assertRaisesRegex(
                        Exception,
                        r"must have minimum of 0"):
                    hh.boot()

    def test_chameleon(self):
        """
        The only RotarySelector, and the only machine whose selector position doubles as the
        filament grip. Its release path is exercised in test_mmu_selector.TestRotarySelector;
        what is checked here is that a 3D Chameleon config renders and loads at all - it did
        not until the bracketed list defaults in Kconfig.3d_chameleon were fixed, which
        Klipper's getintlist rejects outright.
        """
        hh = self._check('chameleon')
        selector = hh.mmu.mmu_unit(0).selector
        self.assertEqual(len(selector.p.selector_release_gates), 4)
        self.assertEqual(len(selector.p.selector_gate_directions), 4)

    def test_pico_mmu(self):
        """PicoMMU must boot safely and require calibration rather than exceed its 140 degree servo range."""
        hh = self._check('pico_mmu')
        selector = hh.mmu.mmu_unit(0).selector
        self.assertEqual(selector.servo_gate_angles, [-1, -1, -1, -1])

    def test_mmx(self):
        """MMX's vendor-specific gate angles must override the unsafe generic 360 degree defaults."""
        hh = self._check('mmx')
        selector = hh.mmu.mmu_unit(0).selector
        self.assertEqual(selector.servo_gate_angles, [60, 0, 180, 120])

    def test_kms(self):
        """KMS defaults keep its per-gate exits active for runtime sensorless experiments."""
        hh = self._check('kms')
        unit = hh.mmu.mmu_unit(0)
        self.assertEqual(unit.p.gate_homing_endstop, 'mmu_exit')
        self.assertEqual(unit.p.gate_preload_endstop, 'mmu_exit')
        for gate in range(4):
            self.assertIsNotNone(hh.sensor('mmu_exit_%d' % gate))

        hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_preload_endstop=none')
        self.assertEqual(unit.p.gate_preload_endstop, 'none')
        self.assertEqual(hh.errors, [])

    def test_qidi(self):
        """QIDI boots with its fixed board, THR sensor and normal dryer setup."""
        with self.assertNoLogs(level='WARNING'):
            hh = self._check('qidi')
        unit = hh.mmu.mmu_unit(0)
        self.assertEqual(unit.p.gate_homing_endstop, 'mmu_shared_exit')
        self.assertEqual(unit.p.gate_preload_endstop, 'none')
        # Simulator override only; the installer/Kconfig default remains 200 mm.
        self.assertEqual(unit.p.gate_preload_homing_max, 100)
        self.assertEqual(unit.p.extruder_homing_endstop, 'extruder')
        self.assertEqual(unit.filament_heater, '')
        self.assertEqual(unit.environment_sensor, 'temperature_sensor unit0_Env')

        model = hh.filament()
        self.assertEqual(model.layout['mmu_entry'], -150)
        self.assertEqual(model.layout['mmu_shared_exit'], 150)
        self.assertEqual(model.layout['extruder'], 900)
        # Reproduce make console's startup preload of every gate. No other
        # filament may leave the shared sensor asserted after the selected gate
        # is ejected and preloaded again.
        for gate in range(4):
            hh.place_filament(gate, position=-40)
            hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.assertFalse(model.triggered('unit0:mmu_shared_exit'))

        hh.run_gcode('MMU_SELECT GATE=0')
        hh.run_gcode('MMU_EJECT')
        hh.run_gcode('MMU_PRELOAD')
        self.assertFalse(model.triggered('unit0:mmu_shared_exit'))
        at = len(hh.console)
        hh.run_gcode('MMU_STATUS')
        status = re.sub(r'<[^>]+>', '', '\n'.join(hh.console[at:]))
        self.assertIn('■◉■■■■■◯', status)

    def test_non_qidi_hardware_controlled_driver_warns_and_boots(self):
        from test.hh import profiles
        profile = profiles.BOXTURTLE.derive(
            'boxturtle_hardware_drivers_runtime',
            syms={'CHOICE_GEAR_TMC_NONE': True})
        hh = session(profile)
        self.addCleanup(hh.close)

        with self.assertLogs(level='WARNING') as captured:
            hh.boot()

        self.assertTrue(hh.fired('mmu:bootup'))
        self.assertEqual(hh.errors, [])
        self.assertTrue(any(
            'has no software-controlled TMC; assuming motor current and driver mode '
            'are controlled in hardware' in line
            for line in captured.output))

    def test_emu(self):
        self._check('emu')

        # EMU's unmodified board choice is SLB, independently wired for every gate.
        from test.hh import cfg, profiles
        parser = cfg.assemble(cfg.render(profiles.get('emu')))
        sensors = dict(parser.items('mmu_sensors unit0'))
        for gate in range(5):
            self.assertEqual(sensors['mmu_entry_switch_pin_%d' % gate],
                             '^unit0_gate%d:PA1' % gate)
            self.assertEqual(
                dict(parser.items('neopixel _unit0_gate%d_leds' % gate))['pin'],
                'unit0_gate%d:PA4' % gate)

    def test_emu_ebb(self):
        hh = self._check('emu_ebb')

        # The shipped EBB defaults must remain internally valid too.  This caught the
        # obsolete BOARD_TYPE_EBB_1_0 guard: it left one-pixel chains mapped as (1-5).
        from test.hh import cfg, profiles
        defaults = profiles.EMU.derive(
            'emu_ebb_defaults', syms={'BOARD_TYPE_EBB_GEN1': True})
        default_parser = cfg.assemble(cfg.render(defaults))
        default_leds = dict(default_parser.items('mmu_leds unit0'))
        self.assertEqual(default_leds['entry_leds'].splitlines(), [
            'neopixel:_unit0_gate%d_leds (5)' % gate for gate in range(5)])
        self.assertEqual(default_leds['exit_leds'].splitlines(), [
            'neopixel:_unit0_gate%d_leds (1,2,3,4)' % gate for gate in range(5)])
        for gate in range(5):
            chain = dict(default_parser.items(
                'neopixel _unit0_gate%d_leds' % gate))
            self.assertEqual(chain['chain_count'], '5')

        # Every gate uses the EBB pin map, but only gate 0's five-pixel chain is assigned
        # to the logical exit segment (one pixel per gate).
        parser = cfg.assemble(cfg.render(profiles.get('emu_ebb')))
        sensors = dict(parser.items('mmu_sensors unit0'))
        for gate in range(5):
            self.assertEqual(sensors['mmu_entry_switch_pin_%d' % gate],
                             '^unit0_gate%d:PB7' % gate)
            chain = dict(parser.items('neopixel _unit0_gate%d_leds' % gate))
            self.assertEqual(chain['pin'], 'unit0_gate%d:PD3' % gate)
            self.assertEqual(chain['chain_count'], '5')
        leds = dict(parser.items('mmu_leds unit0'))
        self.assertEqual(leds['entry_leds'], '')
        self.assertEqual(leds['exit_leds'], 'neopixel:_unit0_gate0_leds (1-5)')
        self.assertEqual(
            len(hh.mmu.mmu_unit(0).leds.virtual_chains['exit'].leds), 5)

    def test_ercf_vvd(self):
        """Two units, two selector classes, 13 gates - see TestMultiUnitMachine."""
        self._check('ercf_vvd')

    def test_each_profile_reaches_a_determinate_filament_state(self):
        """
        A powered-on machine with no filament must know it is unloaded. Anything else and
        HH tells the user to run MMU_RECOVER, which is what the error assertion catches -
        but assert the state directly too, since it is the thing that matters.
        """
        for name in BOOTABLE:
            with self.subTest(profile=name):
                hh = self._boot(name)
                self.assertEqual(hh.mmu.filament_pos, 0)    # FILAMENT_POS_UNLOADED

    def test_gate_count_matches_the_rendered_config(self):
        """Guards against a profile silently rendering a different machine."""
        from test.hh import cfg, profiles
        for name, (gates, _selector) in BOOTABLE.items():
            with self.subTest(profile=name):
                parser = cfg.assemble(cfg.render(profiles.get(name)))
                # SUMMED over units, so the multi-unit entry is checked against the same
                # total HH reports as num_gates rather than against unit0 alone.
                rendered = sum(int(dict(parser.items(section))['num_gates'])
                               for section in parser.sections()
                               if section.startswith('mmu_unit '))
                self.assertEqual(rendered, gates)


class TestMachineNfcDefaults(unittest.TestCase):
    """Machine-specific NFC defaults must override the generic Kconfig values."""

    def _defaults(self, profile_name, unit_name=None):
        from test.hh import cfg, profiles
        profile = profiles.get(profile_name)
        syms = dict(profile.syms)
        if unit_name is not None:
            syms = dict(next(unit.syms for unit in profile.units if unit.name == unit_name))
        # EMU offers NFC as an addition rather than forcing the hardware capability on.
        # Activate it here so the hidden runtime parameter derived from the menu bool is
        # resolved exactly as it will be in an NFC-equipped EMU configuration.
        syms['MMU_HAS_NFC_READER'] = True
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig('%s-nfc-defaults' % profile_name, syms)
        return {
            name: kc.get(name)
            for name in (
                'PARAM_NFC_GATE_JOG_SCAN_WINDOW',
                'PARAM_NFC_PRELOAD_JOG_SCAN_WINDOW',
                'PARAM_NFC_GATE_CLEAR_DISTANCE',
                'PARAM_NFC_PRELOAD_CLEAR_DISTANCE',
                'PARAM_NFC_NEIGHBOR_CHECK',
            )
        }

    def test_emu(self):
        self.assertEqual(self._defaults('emu'), {
            'PARAM_NFC_GATE_JOG_SCAN_WINDOW': '0, 480',
            'PARAM_NFC_PRELOAD_JOG_SCAN_WINDOW': '0, 480',
            'PARAM_NFC_GATE_CLEAR_DISTANCE': '70',
            'PARAM_NFC_PRELOAD_CLEAR_DISTANCE': '70',
            'PARAM_NFC_NEIGHBOR_CHECK': '1',
        })

    def test_vivid(self):
        self.assertEqual(self._defaults('ercf_vvd', 'unit1'), {
            'PARAM_NFC_GATE_JOG_SCAN_WINDOW': '-300, 200',
            'PARAM_NFC_PRELOAD_JOG_SCAN_WINDOW': '-300, 200',
            'PARAM_NFC_GATE_CLEAR_DISTANCE': '-70',
            'PARAM_NFC_PRELOAD_CLEAR_DISTANCE': '-70',
            'PARAM_NFC_NEIGHBOR_CHECK': '1',
        })


class TestMultiUnitMachine(unittest.TestCase):
    """
    `ercf_vvd` is the only profile with two units, so everything here is unreachable
    elsewhere: contiguous gate numbering ACROSS units, per-unit selector classes and homing
    strategies, and a sparse per-gate device list.
    """

    @classmethod
    def setUpClass(cls):
        cls.hh = session('ercf_vvd')
        cls.hh.boot()

    @classmethod
    def tearDownClass(cls):
        cls.hh.close()

    def test_boots_clean(self):
        self.assertTrue(self.hh.fired('mmu:bootup'))
        self.assertEqual(self.hh.errors, [])

    def test_gates_are_numbered_contiguously_across_units(self):
        """
        unit1 owns gates 9-12, not 0-3. Every per-gate lookup in HH goes through
        mmu_unit(gate) + local_gate(gate), so an off-by-one here would silently address the
        wrong unit's hardware.
        """
        units = self.hh.mmu.mmu_machine.units
        self.assertEqual([(u.name, u.first_gate, u.num_gates) for u in units],
                         [('unit0', 0, 9), ('unit1', 9, 4)])
        self.assertEqual(self.hh.mmu.num_gates, 13)
        for gate, expected in ((0, 'unit0'), (8, 'unit0'), (9, 'unit1'), (12, 'unit1')):
            self.assertEqual(self.hh.mmu.mmu_unit(gate).name, expected,
                             'gate %d resolved to the wrong unit' % gate)

    def test_each_unit_keeps_its_own_selector_and_homing_strategy(self):
        """
        The units disagree on both, which is the point: a printer-wide assumption about
        either would pass on every other profile and fail here.
        """
        by_name = {u.name: u for u in self.hh.mmu.mmu_machine.units}
        self.assertEqual(type(by_name['unit0'].selector).__name__, 'LinearServoSelector')
        self.assertEqual(type(by_name['unit1'].selector).__name__, 'IndexedSelector')
        self.assertEqual(by_name['unit0'].p.gate_homing_endstop, 'encoder')
        self.assertEqual(by_name['unit1'].p.gate_homing_endstop, 'mmu_exit')

    def test_indexed_selector_self_calibrates_and_self_homes(self):
        """
        IndexedSelector marks itself homed and calibrated at handle_ready
        (mmu_indexed_selector.py:137-140) - "design doesn't need homing or calibration".
        Its LinearServoSelector neighbour does NOT, so this asserts the two coexist.
        """
        by_name = {u.name: u for u in self.hh.mmu.mmu_machine.units}
        self.assertTrue(by_name['unit1'].selector.is_homed)
        self.assertFalse(by_name['unit0'].selector.is_homed,
                         'unit0 is uncalibrated in the harness, so it must NOT claim homed')

    def test_per_gate_nfc_readers_are_shared_across_a_gate_pair(self):
        """
        The ViViD hand-writes two physical readers, each covering an adjacent gate pair
        (boards/custom/Kconfig.vvd:120-133), so nfc_readers renders DENSE - every gate
        populated, with gates 0/1 and 2/3 naming the SAME reader - rather than sparse.
        mmu_nfc_manager._lookup_or_create_reader dedupes by name for exactly this case
        ("Already created (e.g. shared between gates)").

        unit1 has CUSTOM_NFC_READER_SETUP set, which hides the generic wiring menu
        entirely (installer/Kconfig.nfc_reader), so it has no common reader of its own -
        that lives on unit0 instead (test_a_common_reader_can_coexist_with_per_gate_ones).
        """
        from test.hh import cfg, profiles
        parser = cfg.assemble(cfg.render(profiles.get('ercf_vvd')))
        raw = dict(parser.items('mmu_unit unit1'))['nfc_readers']
        self.assertEqual([p.strip() for p in raw.split(',')],
                         ['unit1_nfc01', 'unit1_nfc01', 'unit1_nfc23', 'unit1_nfc23'])

        unit1 = {u.name: u for u in self.hh.mmu.mmu_machine.units}['unit1']
        self.assertEqual(len(unit1.nfc_readers), 4)
        self.assertEqual([bool(r) for r in unit1.nfc_readers], [True, True, True, True])
        self.assertEqual(unit1.nfc_reader, '', 'unit1 must have no common reader')

    def test_a_common_reader_can_coexist_with_per_gate_ones(self):
        """
        THE regression test for blank-preserving getlist, moved here from unit1 once
        unit1's per-gate list stopped being sparse (see the test above). unit0 renders a
        common reader (the generic wiring prompts, unaffected by CUSTOM_NFC_READER_SETUP)
        alongside unit1's own per-gate list on the SAME multi-unit machine - two
        different mechanisms coexisting without either one clobbering the other.
        """
        unit0 = {u.name: u for u in self.hh.mmu.mmu_machine.units}['unit0']
        self.assertEqual(unit0.nfc_reader, 'unit0_nfc')
        self.assertEqual(unit0.nfc_readers, [])

    def test_filament_heater_resolves(self):
        """
        ViViD selects MMU_HAS_HEATER, so [mmu_machine] carries filament_heater and
        mmu_unit.py:145-162 resolves it with the SENTINEL default - a missing
        heater_generic fake is a hard config error, not a skipped section.
        """
        unit1 = {u.name: u for u in self.hh.mmu.mmu_machine.units}['unit1']
        self.assertEqual(unit1.filament_heater, 'heater_generic unit1_heater')
        heater = self.hh.printer.lookup_object(unit1.filament_heater)
        self.assertIn('temperature', heater.get_status(0))

    def test_encoder_resolution_follows_the_binky_12_wheel(self):
        """
        Pinned in the profile rather than derived: ERCF 1.1 + MOD_BINKY now defaults to
        Binky-8 (1.469), but this machine has a Binky-12 (0.979).
        """
        encoder = self.hh.printer.lookup_object('mmu_encoder unit0')
        self.assertAlmostEqual(encoder.resolution, 0.979, places=4)


class TestSelectorCoverage(unittest.TestCase):
    """
    9 selector classes exist; 5 are reachable through a bootable profile. Recorded as a
    test so the gap is visible in the suite rather than only in a document.
    """

    def test_selector_registry_is_fully_populated(self):
        from extras.mmu.unit.selectors import SELECTOR_REGISTRY
        self.assertGreaterEqual(len(SELECTOR_REGISTRY), 8)

    def test_which_selectors_are_actually_exercised(self):
        exercised = ({selector for _gates, selector in BOOTABLE.values()}
                     | EXERCISED_BY_LATER_UNITS)
        self.assertEqual(
            exercised,
            {'VirtualSelector', 'LinearServoSelector', 'IndexedSelector', 'RotarySelector',
             'ServoSelector'},
            'update this and the README coverage map when a profile adds another selector '
            'type')


class TestProportionalBufferSensor(unittest.TestCase):
    """
    EMU's analog buffer sensor - the only place a shipped profile exercises the ADC path.

    A proportional sensor reports a normalised value in [-1.0, +1.0] and DERIVES the
    virtual filament_compression / filament_tension sensors from it by threshold, rather
    than reading switches. Those derived sensors have no switch_pin at all, which is what
    made this profile fail to load before the harness learned to dispatch by sensor kind.
    """

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [])
        self.prop = self.hh.sensor('filament_proportional')
        self.sm = self.hh.mmu.sensor_manager

    def tearDown(self):
        self.hh.close()

    def compression(self):
        return self.sm.check_sensor('filament_compression')

    def tension(self):
        return self.sm.check_sensor('filament_tension')

    def test_an_adc_pin_really_is_bound(self):
        adc = self.hh.pins.of_type('adc')
        self.assertTrue(adc, 'EMU should bind an analog buffer pin')

    def test_resting_state_matches_the_configured_spring(self):
        """
        The buffer declares buffer_spring_state: tension, so at rest the analog reading
        must sit at the tension end and the derived tension sensor must be the one
        triggered. Expressed as a RAW VALUE and left to derive - forcing the virtual
        sensor directly leaves it stuck, because the derivation only re-evaluates on a
        threshold crossing.
        """
        unit = self.hh.mmu.mmu_unit(0)
        self.assertEqual(unit.buffer.buffer_spring_state, 'tension')
        self.assertAlmostEqual(self.prop.value, -1.0, places=2)
        self.assertTrue(self.tension())
        self.assertFalse(self.compression())

    def test_compression_end(self):
        self.prop.feed(self.prop.neutral_value() + self.prop.sensor._d_pos)
        self.assertAlmostEqual(self.prop.value, 1.0, places=2)
        self.assertTrue(self.compression())
        self.assertFalse(self.tension())

    def test_reading_is_normalised_not_raw(self):
        sensor = self.prop.sensor
        self.prop.feed(sensor._neutral_point)
        self.assertAlmostEqual(self.prop.value, 0.0, places=2)
        self.assertAlmostEqual(sensor.value_raw, sensor._neutral_point, places=3)

    def test_derived_sensors_have_no_switch_pin(self):
        """The property that broke this profile, pinned so it cannot regress silently."""
        for name in ('filament_tension', 'filament_compression'):
            with self.subTest(sensor=name):
                self.assertEqual(self.hh.sensor(name).kind, 'virtual')
        self.assertEqual(self.hh.sensor('filament_proportional').kind, 'proportional')
        self.assertEqual(self.hh.sensor('mmu_entry_0').kind, 'switch')


class TestProportionalVirtualSensorThresholds(unittest.TestCase):
    """
    The configured threshold is the assertion point on both sides of zero.  Once asserted,
    each virtual sensor releases one hysteresis interval inward, producing a stable neutral
    core and preventing chatter at the assertion boundary.
    """

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot()
        self.prop = self.hh.sensor('filament_proportional')
        self.sensor = self.prop.sensor
        self.sm = self.hh.mmu.sensor_manager

    def tearDown(self):
        self.hh.close()

    def feed_normalised(self, value):
        """Feed a raw reading that normalises to `value` in [-1, +1]."""
        span = self.sensor._d_pos if value >= 0 else self.sensor._d_neg
        self.prop.feed(self.sensor._neutral_point + value * span)
        return self.sensor.value

    def test_thresholds_are_symmetric_with_an_inward_release_band(self):
        self.assertAlmostEqual(self.sensor._vsensor_tension_trigger, -0.900, places=3)
        self.assertAlmostEqual(self.sensor._vsensor_tension_release, -0.864, places=3)
        self.assertAlmostEqual(self.sensor._vsensor_compression_release, 0.864, places=3)
        self.assertAlmostEqual(self.sensor._vsensor_compression_trigger, 0.900, places=3)

    def test_a_centred_filament_reads_neither(self):
        self.feed_normalised(0.0)
        self.assertFalse(self.sm.check_sensor('filament_tension'))
        self.assertFalse(self.sm.check_sensor('filament_compression'))

    def test_moderate_tension_is_neutral(self):
        self.feed_normalised(-0.5)
        self.assertFalse(self.sm.check_sensor('filament_tension'))
        self.assertFalse(self.sm.check_sensor('filament_compression'))

    def test_tension_uses_state_dependent_hysteresis(self):
        self.feed_normalised(-0.91)
        self.assertTrue(self.sm.check_sensor('filament_tension'))
        self.feed_normalised(-0.88)
        self.assertTrue(self.sm.check_sensor('filament_tension'),
                        'tension should remain asserted inside its hysteresis band')
        self.feed_normalised(-0.86)
        self.assertFalse(self.sm.check_sensor('filament_tension'))

    def test_compression_uses_state_dependent_hysteresis(self):
        self.feed_normalised(0.91)
        self.assertTrue(self.sm.check_sensor('filament_compression'))
        self.feed_normalised(0.88)
        self.assertTrue(self.sm.check_sensor('filament_compression'),
                        'compression should remain asserted inside its hysteresis band')
        self.feed_normalised(0.86)
        self.assertFalse(self.sm.check_sensor('filament_compression'))

    def test_can_jump_directly_between_extremes(self):
        self.feed_normalised(-1.0)
        self.assertTrue(self.sm.check_sensor('filament_tension'))
        self.feed_normalised(1.0)
        self.assertFalse(self.sm.check_sensor('filament_tension'))
        self.assertTrue(self.sm.check_sensor('filament_compression'))


class TestAdcCompatMatrixOnRealMachine(unittest.TestCase):
    """
    The ADC compat shim across all six combinations, on a real machine rather than in
    isolation: 3 Klipper API generations x 2 callback payload shapes. Only one combination
    ever runs on a given Klipper, so the rest is dead code that only a matrix reaches.
    (test_mmu_adc_compat.py covers the shim's own logic; this proves a whole machine boots
    and reads correctly under each.)
    """

    def test_every_api_and_payload_combination_boots_and_reads(self):
        for api in ('new', 'old', 'oldest'):
            for payload in ('pair', 'samples'):
                with self.subTest(api=api, payload=payload):
                    hh = session('emu', adc_api=api, adc_payload=payload)
                    try:
                        hh.boot()
                        self.assertEqual(hh.errors, [])
                        prop = hh.sensor('filament_proportional')
                        # resting value derived through this API/payload combination
                        self.assertAlmostEqual(prop.value, -1.0, places=2)
                        prop.feed(prop.neutral_value() + prop.sensor._d_pos)
                        self.assertAlmostEqual(prop.value, 1.0, places=2)
                    finally:
                        hh.close()


if __name__ == '__main__':
    unittest.main()
