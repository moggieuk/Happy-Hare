# Happy Hare residual-temperature regression tests for PR #1448.
# Drives real load/unload commands; injected macro failures model interrupted purges.
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest
from test import test_mmu_extruder_heating as heating

_heater_commands = heating._heater_commands

class TestResidualHeatingRecovery(unittest.TestCase):
    setUp = heating.TestResidualFilamentHeating.setUp
    tearDown = heating.TestResidualFilamentHeating.tearDown

    def test_slicer_purge_keeps_temperature_control(self):
        # There is no slicer purge-completion callback, so HH must not take ownership.
        self.mmu.p.force_purge_standalone = False
        self.hh.heat_extruder(220)
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        self.hh.run_gcode('MMU_CHANGE_TOOL RESTORE=0 TOOL=1')
        self.assertFalse(self.mmu.is_mmu_paused())
        self.assertEqual(self.extruder.get_heater().target_temp, 220)
        self.assertNotIn('M104 S270.0', _heater_commands(self.hh.gcode.executed))
        self.assertEqual(self.wrapper.filament_remaining_temp, 270)

    def test_standalone_unload_must_not_leave_restore_for_next_command(self):
        # Only load_sequence clears the residual raise; standalone unload leaks it.
        self.wrapper.filament_remaining_temp = 0
        self.hh.heat_extruder(270)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0 RESTORE=0')
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_UNLOAD SKIP_TIP=1 RESTORE=0')
        self.assertEqual(self.mmu.filament_pos, 0)
        self.assertIsNone(self.mmu._residual_restore)
        self.assertEqual(self.extruder.get_heater().target_temp, 220)

    def test_later_load_respects_new_user_target_after_unload(self):
        # A stale restore from the previous command overrides the user's new target.
        self.wrapper.filament_remaining_temp = 0
        self.hh.heat_extruder(270)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0 RESTORE=0')
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_UNLOAD SKIP_TIP=1 RESTORE=0')
        self.hh.heat_extruder(250)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1 RESTORE=0')
        self.assertEqual(self.extruder.get_heater().target_temp, 250)

    def test_failed_standalone_unload_restores_temperature(self):
        # No load_sequence finally is reached when the preceding unload fails.
        self.wrapper.filament_remaining_temp = 0
        self.hh.heat_extruder(270)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0 RESTORE=0')
        self.hh.heat_extruder(220)
        def fail(macro, gcmd):
            raise self.hh.printer.command_error('injected tip macro failure')
        self.hh.printer.harness_macro_effects[self.mmu.p.form_tip_macro.split()[0]] = fail
        try:
            self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1 RESTORE=0')
        except self.hh.printer.command_error:
            pass
        self.assertEqual(self.extruder.get_heater().target_temp, 220)

    def test_unreadable_saved_temperature_is_unknown(self):
        # Saved-variable get() has no type validation, contrary to the PR description.
        key = self.mmu.var_manager.namespace('mmu_state_filament_remaining_temp', self.wrapper.name)
        self.mmu.var_manager.save_variables.allVariables[key] = 'unknown'
        self.wrapper._handle_connect()
        self.hh.run_gcode('MMU_SELECT GATE=1')
        self.mmu._ensure_safe_extruder_temperature()
        self.assertEqual(self.extruder.get_heater().target_temp, self.mmu.p.default_extruder_temp)

    def test_restore_keeps_minimum_extrusion_temperature(self):
        # Completion rereads raw gate temperature, bypassing the earlier safety fallback.
        self.mmu.gate_temperature[1] = 100
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.assertGreaterEqual(self.extruder.get_heater().target_temp,
                                self.extruder.get_heater().min_extrude_temp)

    def test_retry_initial_load_while_paused_reheats_residual(self):
        # The first failure restores 220, then source='pause' prevents reheating the PC.
        self.hh.heat_extruder(220)
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        self.mmu.p.retry_tool_change_on_error = False
        original = self.mmu._load_extruder
        def fail(*args, **kwargs):
            self.mmu._ensure_safe_extruder_temperature(wait=True)
            from extras.mmu.mmu_utils import MmuError
            raise MmuError('injected failure before feeding new filament into nozzle')
        self.mmu._load_extruder = fail
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.mmu._load_extruder = original
        self.assertTrue(self.mmu.is_mmu_paused())
        self.assertFalse(self.mmu._print_toolchange_done)
        self.assertEqual(self.extruder.get_heater().target_temp, 220)
        self.hh.run_gcode('MMU_UNLOCK')
        mark = len(self.hh.gcode.executed)
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.assertIn('M104 S270.0', _heater_commands(self.hh.gcode.executed[mark:]))
        self.assertEqual(self.extruder.get_heater().target_temp, 220)
        self.assertTrue(self.mmu._print_toolchange_done)

    def test_upgrade_missing_temperature_stays_unknown(self):
        key = self.mmu.var_manager.namespace('mmu_state_filament_remaining_temp', self.wrapper.name)
        self.assertNotIn(key, self.mmu.var_manager.save_variables.allVariables)
        self.wrapper.filament_remaining_temp = 0
        self.wrapper._handle_connect()
        self.assertEqual(self.wrapper.filament_remaining_temp, 0)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1 RESTORE=0')
        self.assertEqual(self.extruder.get_heater().target_temp, 220)
        self.assertEqual(self.wrapper.filament_remaining_temp, 220)
        self.assertEqual(self.mmu.var_manager.get('mmu_state_filament_remaining_temp', 0, self.wrapper.name), 220)

    def start_print(self, temp=220):
        self.hh.heat_extruder(temp)
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')

    def test_purge_runs_hot_before_restoring_slicer_target(self):
        self.start_print()
        temperatures = []
        self.hh.printer.harness_macro_effects[self.mmu.p.purge_macro] = (
            lambda macro, gcmd: temperatures.append(self.extruder.get_heater().target_temp))
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.assertEqual(temperatures, [270])
        self.assertEqual(self.extruder.get_heater().target_temp, 220)
        self.assertEqual(self.wrapper.filament_remaining_temp, 220)
        self.assertIsNone(self.mmu._residual_restore)

    def test_cold_failed_purge_returns_to_heater_off(self):
        def fail(macro, gcmd):
            self.assertEqual(self.extruder.get_heater().target_temp, 270)
            raise self.hh.printer.command_error('injected purge failure')
        self.hh.printer.harness_macro_effects[self.mmu.p.purge_macro] = fail
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.assertTrue(any('injected purge failure' in line for line in self.hh.errors))
        self.assertEqual(self.extruder.get_heater().target_temp, 0)
        self.assertEqual(self.wrapper.filament_remaining_temp, 270)
        self.assertIsNone(self.mmu._residual_restore)

    def test_skipped_purge_preserves_temperature_and_residual(self):
        self.start_print()
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 SKIP_PURGE=1 RESTORE=0 TOOL=1')
        self.assertNotIn('M104 S270.0', _heater_commands(self.hh.gcode.executed))
        self.assertEqual(self.extruder.get_heater().target_temp, 220)
        self.assertEqual(self.wrapper.filament_remaining_temp, 270)

    def test_missing_purge_macro_preserves_temperature_and_residual(self):
        self.start_print()
        self.mmu.p.purge_macro = 'MISSING_PURGE_MACRO'
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.assertNotIn('M104 S270.0', _heater_commands(self.hh.gcode.executed))
        self.assertEqual(self.extruder.get_heater().target_temp, 220)
        self.assertEqual(self.wrapper.filament_remaining_temp, 270)

    def test_midprint_and_paused_midprint_swaps_remain_slicer_owned(self):
        self.start_print()
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=0')
        self.assertTrue(self.mmu._print_toolchange_done)
        # A stale hotter residual must not change temperatures on mid-print swaps.
        self.wrapper.filament_remaining_temp = 270
        mark = len(self.hh.gcode.executed)
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.assertNotIn('M104 S270.0', _heater_commands(self.hh.gcode.executed[mark:]))
        self.mmu.handle_mmu_error('test mid-print pause')
        self.hh.run_gcode('MMU_UNLOCK')
        mark = len(self.hh.gcode.executed)
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=0')
        self.assertNotIn('M104 S270.0', _heater_commands(self.hh.gcode.executed[mark:]))
        self.assertEqual(self.extruder.get_heater().target_temp, 220)

    def test_invalid_saved_values_are_unknown_after_connect(self):
        key = self.mmu.var_manager.namespace('mmu_state_filament_remaining_temp', self.wrapper.name)
        for value in (None, True, False, '270', [], {}, -1, 100, 301, float('inf'), float('nan'), 10 ** 1000):
            with self.subTest(value=repr(value)[:40]):
                self.mmu.var_manager.save_variables.allVariables[key] = value
                self.wrapper._handle_connect()
                self.assertEqual(self.wrapper.filament_remaining_temp, 0)
        for value in (220, 270.0):
            with self.subTest(value=value):
                self.mmu.var_manager.save_variables.allVariables[key] = value
                self.wrapper._handle_connect()
                self.assertEqual(self.wrapper.filament_remaining_temp, value)

    def test_cold_swap_uses_incoming_gate_temperature(self):
        self.wrapper.filament_remaining_temp = 0
        self.hh.heat_extruder(270)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0 RESTORE=0')
        self.hh.heat_extruder(0)
        self.mmu.gate_temperature[0] = 250
        self.mmu.gate_temperature[1] = 290
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1 RESTORE=0')
        self.assertEqual(self.extruder.get_heater().target_temp, 290)
        self.assertEqual(self.wrapper.filament_remaining_temp, 290)
        self.assertIsNone(self.mmu._residual_restore)


    def test_unknown_residual_preserves_existing_cold_swap_behavior(self):
        self.hh.heat_extruder(270)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0 RESTORE=0')
        # Model an upgrade with filament loaded but no persisted temperature.
        self.wrapper.filament_remaining_temp = 0
        self.hh.heat_extruder(0)
        self.mmu.gate_temperature[0] = 200
        self.mmu.gate_temperature[1] = 250
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1 RESTORE=0')
        # Without a residual override, preserve the existing warm target selection.
        self.assertEqual(self.extruder.get_heater().target_temp, 200)

    def test_automatic_retry_reheats_residual(self):
        from extras.mmu.mmu_utils import MmuError
        self.start_print()
        self.mmu.p.retry_tool_change_on_error = True
        original = self.mmu._load_extruder
        attempts = []

        def fail_once(*args, **kwargs):
            self.mmu._ensure_safe_extruder_temperature(wait=True)
            attempts.append(self.extruder.get_heater().target_temp)
            if len(attempts) == 1:
                raise MmuError('injected first-attempt load failure')
            return original(*args, **kwargs)

        self.mmu._load_extruder = fail_once
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.assertEqual(attempts, [270, 270])
        self.assertFalse(self.mmu.is_mmu_paused())
        self.assertEqual(self.extruder.get_heater().target_temp, 220)
        self.assertTrue(self.mmu._print_toolchange_done)


    def test_cold_initial_failure_saves_safe_resume_target(self):
        from extras.mmu.mmu_utils import MmuError
        self.start_print(temp=0)
        self.mmu.p.retry_tool_change_on_error = False
        original = self.mmu._load_extruder

        def fail(*args, **kwargs):
            self.mmu._ensure_safe_extruder_temperature(wait=True)
            raise MmuError('injected cold initial-load failure')

        self.mmu._load_extruder = fail
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.mmu._load_extruder = original
        self.assertTrue(self.mmu.is_mmu_paused())
        self.assertEqual(self.mmu.psm.paused_extruder_temp, self.mmu.p.default_extruder_temp)
        self.hh.run_gcode('MMU_UNLOCK')
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.mmu._continue_after('resume', restore=False)
        self.assertEqual(self.extruder.get_heater().target_temp, self.mmu.p.default_extruder_temp)


    def test_cancel_after_failed_purge_keeps_residual_temperature(self):
        self.start_print()
        self.mmu.p.retry_tool_change_on_error = False

        def fail(macro, gcmd):
            raise self.hh.printer.command_error('injected purge failure before clearing residual')

        self.hh.printer.harness_macro_effects[self.mmu.p.purge_macro] = fail
        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')
        self.assertTrue(self.mmu.is_mmu_paused())
        self.assertEqual(self.extruder.get_heater().target_temp, 220)
        self.mmu.psm.on_print_end('cancelled')
        self.assertEqual(self.wrapper.filament_remaining_temp, 270)


class TestResidualHeatingAcrossExtruders(unittest.TestCase):
    setUp = heating.TestDualExtruderHeating.setUp
    tearDown = heating.TestDualExtruderHeating.tearDown

    def test_swap_restores_each_heater_and_records_only_loaded_extruder(self):
        from extras.mmu.mmu_constants import PURGE_STANDALONE
        self.mmu.select_gate(0)
        old_wrapper = self.mmu.mmu_unit().extruder_wrapper
        old_wrapper.filament_remaining_temp = 270
        self.extruder.get_heater().set_temp(220)
        self.extruder1.get_heater().set_temp(240)
        with self.mmu._wrap_residual_heating(PURGE_STANDALONE):
            self.mmu._ensure_safe_extruder_temperature()
            self.assertEqual(self.extruder.get_heater().target_temp, 270)
            self.mmu.select_gate(9)
            new_wrapper = self.mmu.mmu_unit().extruder_wrapper
            new_wrapper.filament_remaining_temp = 280
            self.mmu._ensure_safe_extruder_temperature()
            self.assertEqual(self.extruder1.get_heater().target_temp, 280)
            self.mmu._end_residual_heating()
        self.assertEqual(self.extruder.get_heater().target_temp, 220)
        self.assertEqual(self.extruder1.get_heater().target_temp, 240)
        self.assertEqual(old_wrapper.filament_remaining_temp, 270)
        self.assertEqual(new_wrapper.filament_remaining_temp, 240)
        self.assertIsNone(self.mmu._residual_restore)
