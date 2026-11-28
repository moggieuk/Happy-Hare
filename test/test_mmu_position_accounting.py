# Happy Hare test harness - unsynced extrusion across resets and manual handoffs.
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest
from unittest import mock

from test.hh import session
from test.hh.bootstrap import PRINTER_STUB

PRE_UNLOAD = '''
[gcode_macro ACCOUNTING_PRE_UNLOAD]
gcode:
    M83
    G1 E-10 F600
'''


class TestUnsyncedPositionAccounting(unittest.TestCase):
    def setUp(self):
        # No encoder and an already released servo: neither introduces an
        # incidental wait before an extruder-only composable step.
        self.hh = session('tradrack', printer_stub=PRINTER_STUB + PRE_UNLOAD)
        self.addCleanup(self.hh.close)
        self.hh.boot(calibrate=True, selected_gate=0)
        self.hh.heat_extruder(220)
        self.hh.place_filament(0, position=-10.)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.hh.run_gcode('MMU_LOAD')
        self.mmu = self.hh.mmu
        self.drive = self.mmu.drive()
        self.assertEqual(self.hh.errors, [])
        from extras.mmu.mmu_constants import DRIVE_UNSYNCED, FILAMENT_POS_LOADED
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.assertEqual(self.drive.get_sync_mode(), DRIVE_UNSYNCED)
        self.assertFalse(self.mmu.has_encoder())

    def _unload_with_retraction(self, poll):
        self.mmu.p.pre_unload_macro = 'ACCOUNTING_PRE_UNLOAD'
        def pre_unload(macro, gcmd):
            if poll:
                self.mmu.get_status(self.hh.reactor.monotonic())
            macro.run_body(gcmd)
        self.hh.printer.harness_macro_effects['ACCOUNTING_PRE_UNLOAD'] = pre_unload
        positions = []
        original = self.mmu._unload_extruder
        def unload(*args, **kwargs):
            positions.append(self.drive.get_filament_position())
            return original(*args, **kwargs)
        with mock.patch.object(self.mmu, '_unload_extruder', side_effect=unload):
            self.hh.run_gcode('MMU_UNLOAD SKIP_TIP=1')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(positions, [-10.], 'The pre-unload retraction must survive the internal position reset')

    def test_pre_unload_retraction_without_status_poll(self):
        self._unload_with_retraction(poll=False)

    def test_pre_unload_retraction_with_status_poll(self):
        self._unload_with_retraction(poll=True)

    def _queued_retraction(self, command, wait_first=False, synced=False):
        if synced:
            from extras.mmu.mmu_constants import DRIVE_GEAR_SYNCED_TO_EXTRUDER
            self.drive.sync_mode(DRIVE_GEAR_SYNCED_TO_EXTRUDER)
        toolhead = self.mmu.toolhead
        stepper = self.drive.mmu_extruder_stepper.stepper
        original_note = stepper.harness_note_motion
        original_flush = toolhead.flush_step_generation
        original_wait = toolhead.wait_moves
        pending = []
        def drain(original):
            while pending:
                original_note(pending.pop(0))
            return original()
        start = self.drive.get_filament_position()
        # The fake normally updates stepper positions during G1 immediately.
        # Defer only that update until a real HH flush/wait, modeling Klipper's
        # queued step generation without modifying any production call path.
        with mock.patch.object(toolhead, 'flush_step_generation', side_effect=lambda: drain(original_flush)), \
             mock.patch.object(toolhead, 'wait_moves', side_effect=lambda: drain(original_wait)):
            self.hh.run_gcode('M83')
            with mock.patch.object(stepper, 'harness_note_motion', side_effect=pending.append):
                self.hh.run_gcode('G1 E-10 F600')
            self.assertEqual(pending, [-10.])
            if wait_first:
                self.hh.run_gcode('M400')
            self.hh.run_gcode(command)
        self.assertEqual(pending, [])
        self.assertEqual(self.hh.errors, [])
        return self.drive.get_filament_position() - start

    def test_composable_step_keeps_pending_extrusion(self):
        delta = self._queued_retraction('_MMU_STEP_MOVE MOVE=-5 MOTOR=extruder')
        self.assertAlmostEqual(delta, -15.)

    def test_composable_step_keeps_already_flushed_extrusion(self):
        delta = self._queued_retraction('_MMU_STEP_MOVE MOVE=-5 MOTOR=extruder', wait_first=True)
        self.assertAlmostEqual(delta, -15.)

    def test_composable_step_keeps_pending_synced_extrusion(self):
        delta = self._queued_retraction('_MMU_STEP_MOVE MOVE=-5 MOTOR=extruder', synced=True)
        self.assertAlmostEqual(delta, -15.)
