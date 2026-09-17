# Happy Hare test harness - SaveVariableManager durability.
#
# Klipper's cmd_SAVE_VARIABLE snapshots `newvars = dict(allVariables)` and renders every
# repr() BEFORE it writes, then throws the live dict away and reloads from the file
# (save_variables.py:48-64). Happy Hare stages its state IN that live dict, so anything
# not in the snapshot is destroyed - on disk and in memory, with no error.
#
# Two ways in, and only one of them is new:
#
#   L1  a set()/delete() lands inside one of the two pauses the modern (threaded-io)
#       command takes. Impossible before klipper 332fbf236, because it never yielded.
#   L2  a FOREIGN writer reloads the file. config/macros/blobifier.cfg:941 issues its own
#       SAVE_VARIABLE, and its trailing loadVariables() clobbers everything HH has staged
#       but not yet flushed. No pause needed - the old klipper does this too.
#
# So L1 is aio-only and L2 must be asserted on both generations.
#
# Run with the repo venv:
#   ./venv/bin/python -m unittest test.test_mmu_save_variables
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import ast
import configparser
import contextlib
import logging
import unittest

from test.hh import cfg, profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

VARS_MMU_REVISION = 'mmu__revision'
VARS_MMU_BOWDEN_LENGTHS = 'mmu_bowden_lengths'


def read_vars_file(hh):
    """Parse mmu_vars.cfg off disk - the only assertion that proves durability."""
    parser = configparser.ConfigParser()
    parser.read(hh.save_variables.filename)
    if not parser.has_section('Variables'):
        return {}
    out = {}
    for name, raw in parser.items('Variables'):
        try:
            out[name] = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            out[name] = raw
    return out


class SaveVariablesMixin:
    def booted(self, klipper_aio=True):
        hh = session('boxturtle', klipper_aio=klipper_aio)
        self.addCleanup(hh.close)
        hh.boot()
        return hh


class TestPauseWindowRace(SaveVariablesMixin, unittest.TestCase):
    """
    L1 - a mutation that lands while SAVE_VARIABLE is paused on its worker thread.

    aio-only by nature: on the old synchronous klipper the command never yields, so
    there is no window to land in.
    """

    def test_set_during_write_pause_is_not_lost(self):
        hh = self.booted()
        vm = hh.mmu.var_manager

        # Fires inside the pause, i.e. after klipper has already snapshotted and
        # rendered the file contents. Exactly what a bare reactor timer does today -
        # see the encoder clog-length timer, mmu_calibrator.py:299.
        hh.save_variables.on_pause = lambda: vm.set('mmu_race_key', 42)

        vm.set('mmu_trigger_key', 1, write=True)
        hh.reactor.advance(0.)

        self.assertEqual(vm.get('mmu_race_key', None), 42, 'lost from the manager')
        self.assertEqual(read_vars_file(hh).get('mmu_race_key'), 42, 'never reached disk')
        self.assertEqual(hh.errors, [])

    def test_delete_during_write_pause_is_not_resurrected(self):
        hh = self.booted()
        vm = hh.mmu.var_manager

        vm.set('mmu_doomed_key', 'gone soon', write=True)
        hh.reactor.advance(0.)
        self.assertIn('mmu_doomed_key', read_vars_file(hh))    # precondition

        hh.save_variables.on_pause = lambda: vm.delete('mmu_doomed_key')
        vm.set('mmu_trigger_key', 2, write=True)
        hh.reactor.advance(0.)

        self.assertIsNone(vm.get('mmu_doomed_key', None), 'resurrected in the manager')
        self.assertNotIn('mmu_doomed_key', read_vars_file(hh), 'resurrected on disk')
        self.assertEqual(hh.errors, [])


class TestForeignWriter(SaveVariablesMixin, unittest.TestCase):
    """
    A SAVE_VARIABLE Happy Hare did not issue - config/macros/blobifier.cfg:941 runs one
    during BLOBIFIER_INIT, reached from mmu_controller.py:458 init_macros().
    """

    def test_staged_values_survive_a_foreign_save_variable(self):
        """
        Documents a NON-hazard, and guards it.

        A foreign write reloads allVariables wholesale, which looks like it should
        destroy anything HH has staged but not flushed. It does not: the snapshot it
        writes from is taken off the live dict, so HH's staged values are already in
        it and come back through the reload. A foreign write is a free flush, not a
        clobber. This must stay true once the journal exists.
        """
        for klipper_aio in (True, False):
            with self.subTest(klipper_aio=klipper_aio):
                hh = self.booted(klipper_aio)
                vm = hh.mmu.var_manager

                with vm.wrap_suspend_write_variables():
                    vm.set('mmu_staged_a', [1, 2, 3])
                    vm.set('mmu_staged_b', {'x': 1})
                    hh.run_gcode('SAVE_VARIABLE VARIABLE=blobifier VALUE=1')

                    # Assert INSIDE the window - after the exit flush it would pass
                    # whether or not get() had been broken.
                    self.assertEqual(vm.get('mmu_staged_a', None), [1, 2, 3])
                    self.assertEqual(vm.get('mmu_staged_b', None), {'x': 1})

                hh.reactor.advance(0.)
                on_disk = read_vars_file(hh)
                self.assertEqual(on_disk.get('mmu_staged_a'), [1, 2, 3])
                self.assertEqual(on_disk.get('mmu_staged_b'), {'x': 1})
                self.assertEqual(hh.errors, [])

    def test_set_during_a_foreign_write_pause_is_not_lost(self):
        """
        The hazard that IS real, and the one the free-flush case disguises.

        HH does not control when a foreign SAVE_VARIABLE runs, so it cannot avoid
        mutating during its pause. That mutation misses the foreign snapshot and is
        then erased by the foreign reload - HH never issued a write, so nothing in
        the old design would ever notice. Only a journal that outlives the reload
        recovers it. aio-only: no executor, no pause, no window.
        """
        hh = self.booted()
        vm = hh.mmu.var_manager

        hh.save_variables.on_pause = lambda: vm.set('mmu_orphan_key', 'survivor')
        hh.run_gcode('SAVE_VARIABLE VARIABLE=blobifier VALUE=1')

        self.assertEqual(vm.get('mmu_orphan_key', None), 'survivor',
                         'erased by a foreign reload')

        vm.write()                  # HH's next flush must carry it to disk
        hh.reactor.advance(0.)
        self.assertEqual(read_vars_file(hh).get('mmu_orphan_key'), 'survivor')
        self.assertEqual(hh.errors, [])


class TestWriteScheduling(SaveVariablesMixin, unittest.TestCase):
    """Physical writes are deferred and coalesced onto a single reactor callback."""

    def test_write_is_not_synchronous(self):
        """
        Proves deferral only. The stronger claim - that no path can reach a physical
        write while holding the gcode mutex - is carried by STRICT_WRITE_CONTEXT,
        which the harness enables for the whole suite.
        """
        for klipper_aio in (True, False):
            with self.subTest(klipper_aio=klipper_aio):
                hh = self.booted(klipper_aio)
                before = len(hh.save_variables.writes)
                hh.mmu.var_manager.set('mmu_deferred_key', 7, write=True)
                self.assertEqual(len(hh.save_variables.writes), before,
                                 'write happened inline instead of on the reactor')
                hh.reactor.advance(0.)
                self.assertEqual(len(hh.save_variables.writes), before + 1)

    def test_flush_waits_for_the_gcode_mutex_instead_of_blocking(self):
        """
        A flush can come due while a command is mid-flight - a homing move pumps the
        reactor - and it must not park there. run_script would pause the callback until
        the command finished, and a reactor callback parked mid-flight is the hazard
        VirtualReactor.suspended_callbacks documents: the pump can return with it only
        half-run, still holding the mutex.

        The mutex is locked and unlocked directly here. The harness gcode deliberately
        does not lock it for real (see gcode.py GCodeDispatch.__init__), so this is the
        only way to exercise the contended path.
        """
        hh = self.booted()
        mutex = hh.gcode.get_mutex()
        before = len(hh.save_variables.writes)

        mutex.lock()
        try:
            hh.mmu.var_manager.set('mmu_polite_key', 5, write=True)
            hh.reactor.advance(0.)
            self.assertEqual(len(hh.save_variables.writes), before,
                             'wrote while a command held the mutex')
        finally:
            mutex.unlock()

        hh.reactor.advance(0.5)     # past FLUSH_RETRY_DELAY
        self.assertEqual(len(hh.save_variables.writes), before + 1,
                         'flush never retried after the mutex was released')
        self.assertEqual(read_vars_file(hh).get('mmu_polite_key'), 5)

    def test_burst_of_writes_coalesces_to_one(self):
        for klipper_aio in (True, False):
            with self.subTest(klipper_aio=klipper_aio):
                hh = self.booted(klipper_aio)
                vm = hh.mmu.var_manager
                before = len(hh.save_variables.writes)
                for i in range(5):
                    vm.set('mmu_burst_%d' % i, i, write=True)
                hh.reactor.advance(0.)

                self.assertEqual(len(hh.save_variables.writes) - before, 1,
                                 'five set(write=True) should be one file write')
                on_disk = read_vars_file(hh)
                for i in range(5):
                    self.assertEqual(on_disk.get('mmu_burst_%d' % i), i)
                self.assertEqual(hh.errors, [])


class TestDisconnectFlush(SaveVariablesMixin, unittest.TestCase):
    """
    Deferring opens a set-to-disk window. klippy:disconnect is the last chance to
    close it, and is the reason the deferral is safe to do at all.

    Note this one cannot fail before the change - writes are synchronous today, so the
    value is already on disk by the time disconnect fires. It is a guard on the NEW
    risk: remove the disconnect handler after deferring and it goes red.
    """

    def test_a_suspended_batch_is_flushed_on_disconnect(self):
        """
        The nastiest version: shutting down mid-MMU_CHANGE_TOOL.

        wrap_suspend_write_variables holds _can_write_variables false for the whole
        operation, which is exactly when the pending batch is largest. Treating that
        flag as "this data is disposable" at disconnect would throw the batch away -
        in the one handler whose entire job is to stop that happening.
        """
        for klipper_aio in (True, False):
            with self.subTest(klipper_aio=klipper_aio):
                hh = self.booted(klipper_aio)
                vm = hh.mmu.var_manager
                with vm.wrap_suspend_write_variables():
                    vm.set('mmu_mid_batch_a', 'a')
                    vm.set('mmu_mid_batch_b', 'b')
                    hh.printer.send_event('klippy:disconnect')
                    on_disk = read_vars_file(hh)
                    self.assertEqual(on_disk.get('mmu_mid_batch_a'), 'a')
                    self.assertEqual(on_disk.get('mmu_mid_batch_b'), 'b')

    def test_pending_values_are_flushed_on_disconnect(self):
        for klipper_aio in (True, False):
            with self.subTest(klipper_aio=klipper_aio):
                hh = self.booted(klipper_aio)
                hh.mmu.var_manager.set('mmu_last_gasp', 99, write=True)
                # Deliberately NOT pumped - the value is in memory only.
                hh.printer.send_event('klippy:disconnect')
                self.assertEqual(read_vars_file(hh).get('mmu_last_gasp'), 99)


class TestBareUnitNames(unittest.TestCase):
    """
    The single-unit "bare_unit_names" install option (installer/Kconfig.options),
    end-to-end through a real boot rather than just the rendered .cfg - see
    test.test_mmu_config.TestBareUnitNames for the render-only checks.
    """

    def test_a_single_unit_is_unnamed_by_default(self):
        """One unit has nothing to be told apart from, so its name is left out."""
        hh = session('boxturtle')
        self.addCleanup(hh.close)
        hh.boot()

        self.assertTrue(hh.mmu.mmu_machine.bare_unit_names)
        vm = hh.mmu.var_manager
        self.assertEqual(vm.namespace('mmu_foo', namespace='unit0'), 'mmu_foo')

    def test_keeping_the_name_is_still_available(self):
        hh = session('boxturtle_named_unit')
        self.addCleanup(hh.close)
        hh.boot()

        self.assertFalse(hh.mmu.mmu_machine.bare_unit_names)
        vm = hh.mmu.var_manager
        self.assertEqual(vm.namespace('mmu_foo', namespace='unit0'), 'mmu_unit0_foo')

        vm.set('mmu_foo', 1, namespace='unit0', write=True)
        hh.reactor.advance(0.)
        self.assertEqual(read_vars_file(hh).get('mmu_unit0_foo'), 1)
        self.assertNotIn('mmu_foo', read_vars_file(hh))

    def test_enabled_keys_are_bare(self):
        hh = session('boxturtle_bare_names')
        self.addCleanup(hh.close)
        hh.boot()

        self.assertTrue(hh.mmu.mmu_machine.bare_unit_names)
        vm = hh.mmu.var_manager
        # Same call, same unit - only the install option differs from the test above.
        self.assertEqual(vm.namespace('mmu_foo', namespace='unit0'), 'mmu_foo')

        vm.set('mmu_foo', 1, namespace='unit0', write=True)
        hh.reactor.advance(0.)
        self.assertEqual(read_vars_file(hh).get('mmu_foo'), 1)
        self.assertNotIn('mmu_unit0_foo', read_vars_file(hh))

    def test_encoder_and_extruder_sensor_names_drop_the_prefix(self):
        """
        The other two call sites the option touches (mmu_encoder.py, mmu_toolhead_wrapper.py)
        - covered here rather than in test_mmu_config since the sensor object only exists
        after a real boot, not in a rendered .cfg string.
        """
        hh = session('boxturtle_bare_names')
        self.addCleanup(hh.close)
        hh.boot()

        unit = hh.mmu.mmu_machine.units[0]
        self.assertEqual(unit.encoder.endstop_sensor.name, 'encoder')
        self.assertIn('extruder', unit.toolhead_wrapper.sensors)

    def test_a_multi_unit_machine_refuses_the_setting(self):
        """
        The installer never offers bare_unit_names on multi-unit, but mmu.cfg is a
        hand-editable file and adding a second unit to 'units' is a one-line edit. Honoring
        the stale setting would give both units ONE set of unprefixed variable and sensor
        names - i.e. silent cross-unit calibration corruption - so MmuMachine forces it off.

        extra_params overrides the jinja render context, which is how a config the installer
        would never produce gets built here.
        """
        profile = profiles.get('ercf_vvd').derive(
            'ercf_vvd_stale_bare_unit_names',
            extra_params={'MULTI_UNIT': False, 'PARAM_BARE_UNIT_NAMES': '1'})

        # Not vacuous: prove the malformed setting really is in the config being booted.
        machine = dict(cfg.assemble(cfg.render(profile)).items('mmu_machine'))
        self.assertEqual(machine['bare_unit_names'], '1')

        hh = session(profile)
        self.addCleanup(hh.close)
        hh.boot()

        self.assertGreater(hh.mmu.mmu_machine.num_units, 1)
        self.assertFalse(hh.mmu.mmu_machine.bare_unit_names)
        vm = hh.mmu.var_manager
        self.assertEqual(vm.namespace('mmu_foo', namespace='unit0'), 'mmu_unit0_foo')
        self.assertEqual(vm.namespace('mmu_foo', namespace='unit1'), 'mmu_unit1_foo')

    def _rebuild_var_manager(self, hh):
        """Re-run SaveVariableManager.__init__ against the same printer, i.e. a restart."""
        from extras.mmu.mmu_utils import SaveVariableManager
        return SaveVariableManager(hh.mmu.var_manager.config, hh.mmu.mmu_machine)

    @contextlib.contextmanager
    def captured_warnings(self):
        """
        Collect root-logger warnings, so a test can assert on their ABSENCE.

        assertNoLogs would say this in one line but needs Python 3.10, and klipper hosts
        run whatever the distro ships - same constraint that produced
        test_mmu_nfc_uart.capture_records. The level dance is needed because this module
        pins the root logger to CRITICAL, which would otherwise filter the records out
        before any handler sees them and make every assertion here vacuously true.
        """
        collected = []

        class Collector(logging.Handler):
            def emit(self, record):
                collected.append(record.getMessage())

        root = logging.getLogger()
        handler = Collector(level=logging.WARNING)
        previous = root.level
        root.setLevel(logging.WARNING)
        root.addHandler(handler)
        try:
            yield collected
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)

    def test_unit_names_are_recorded_for_the_next_boot(self):
        hh = session('boxturtle')
        self.addCleanup(hh.close)
        hh.boot()
        hh.reactor.advance(0.)
        self.assertEqual(read_vars_file(hh).get('mmu__unit_names'), ['unit0'])

    def test_calibration_moves_across_when_the_name_is_dropped(self):
        """
        The upgrade path for every existing single-unit install: their data is stored under
        'unit0', the installer now leaves a single unit unnamed, and the switch happens on
        the reinstall that regenerates mmu.cfg. Nothing may be lost in the move.
        """
        hh = session('boxturtle')       # unnamed - the "after" side of the switch
        self.addCleanup(hh.close)
        hh.boot()
        self.assertTrue(hh.mmu.mmu_machine.bare_unit_names)   # precondition

        # mmu_vars.cfg as the previous, named, boot left it.
        hh.mmu.var_manager.save_variables.allVariables.update({
            'mmu__bare_unit_names': False,
            'mmu__unit_names': ['unit0'],
            'mmu_unit0_bowden_lengths': [1623.6],
            'mmu_unit0_gear_rotation_distances': [22.7],
            'mmu_unit0_encoder_resolution': 1.05,
            'mmu_unit0_statistics_gate_0': {'pauses': 2},
        })

        vm = self._rebuild_var_manager(hh)

        self.assertEqual(vm.get(VARS_MMU_BOWDEN_LENGTHS, None), [1623.6])
        self.assertEqual(vm.get('mmu_gear_rotation_distances', None), [22.7])
        self.assertEqual(vm.get('mmu_encoder_resolution', None), 1.05)
        self.assertEqual(vm.get('mmu_statistics_gate_0', None), {'pauses': 2})
        # ...and the named copies are gone, not merely duplicated.
        allvars = vm.save_variables.allVariables
        self.assertEqual([k for k in allvars if k.startswith('mmu_unit0_')], [])

    def test_calibration_moves_back_when_the_name_is_restored(self):
        """The same switch in reverse, so turning the option off is equally safe."""
        hh = session('boxturtle_named_unit')
        self.addCleanup(hh.close)
        hh.boot()
        self.assertFalse(hh.mmu.mmu_machine.bare_unit_names)  # precondition

        hh.mmu.var_manager.save_variables.allVariables.update({
            'mmu__bare_unit_names': True,
            'mmu__unit_names': ['unit0'],
            'mmu_bowden_lengths': [1623.6],
        })

        vm = self._rebuild_var_manager(hh)

        self.assertEqual(vm.get(VARS_MMU_BOWDEN_LENGTHS, None, namespace='unit0'), [1623.6])
        self.assertIn('mmu_unit0_bowden_lengths', vm.save_variables.allVariables)
        self.assertNotIn('mmu_bowden_lengths', vm.save_variables.allVariables)

    def test_global_variables_are_not_swept_up_in_the_move(self):
        """
        'mmu_statistics_gate_0' is per-unit but 'mmu_statistics_swaps' is not, and neither
        is anything under 'mmu_state_'. Renaming those would corrupt printer-wide state.
        """
        hh = session('boxturtle')
        self.addCleanup(hh.close)
        hh.boot()
        hh.mmu.var_manager.save_variables.allVariables.update({
            'mmu__bare_unit_names': False,
            'mmu__unit_names': ['unit0'],
            'mmu_statistics_swaps': {'total_swaps': 12},
            'mmu_state_gate_status': [1, 1, 0, 0],
        })

        vm = self._rebuild_var_manager(hh)

        self.assertEqual(vm.get('mmu_statistics_swaps', None), {'total_swaps': 12})
        self.assertEqual(vm.get('mmu_state_gate_status', None), [1, 1, 0, 0])


if __name__ == '__main__':
    unittest.main()
