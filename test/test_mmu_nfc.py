# Happy Hare test harness - milestone A2b: the NFC hardware layer config-loads.
#
# Until this file existed, nothing in the NFC/RFID feature had ever run. The dev
# handoffs (FUTURE/nfc_session*.md) all say "static-verified only (ast.parse) -
# nothing run on hardware", across five sessions of work.
#
# These tests found two real bugs. One is fixed and guarded here; the other is
# captured as an expectedFailure so it flips green when the template is corrected.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import cfg, profiles, session

logging.getLogger().setLevel(logging.CRITICAL)


class TestPerGateNfcBoots(unittest.TestCase):
    """
    REGRESSION GUARD for a crash-on-config-load.
    extras/mmu/unit/nfc/mmu_nfc_reader.py:132 used to call
        printer.load_object('mmu_nfc_reader', None)
    but the signature is load_object(config, section, default=sentinel), so the
    arguments were shifted: section arrived as None and Klipper's section.split()
    raised AttributeError. Every machine with an [mmu_nfc_reader NAME] section failed
    to start Klipper - which no one had noticed because the feature had never been
    run. Now lookup_object(..., None), which is the right call anyway.
    """

    @classmethod
    def setUpClass(cls):
        cls.hh = session('nfc_per_gate')
        cls.hh.boot()

    @classmethod
    def tearDownClass(cls):
        cls.hh.close()

    def test_boots_without_error(self):
        self.assertTrue(self.hh.fired('mmu:bootup'))
        self.assertEqual(self.hh.errors, [])

    def test_one_reader_per_gate(self):
        mgr = self.unit().nfc_manager
        self.assertIsNotNone(mgr)
        self.assertEqual(len(mgr.gate_readers), 4)
        for gate, reader in enumerate(mgr.gate_readers):
            self.assertIsNotNone(reader, 'gate %d has no reader' % gate)
            self.assertEqual(reader.reader_type, 'rc522')

    def test_no_shared_reader_in_per_gate_mode(self):
        self.assertIsNone(self.unit().nfc_manager.shared_reader)

    def test_reader_sections_are_registered_objects(self):
        for i in range(4):
            self.assertIn('mmu_nfc_reader unit0_nfc%d' % i, self.hh.printer.objects)

    def test_defaults_inheritance_is_currently_inert(self):
        """
        Documents a KNOWN LIMITATION rather than asserting desired behaviour.

        With the argument order fixed, the call still yields None: nothing ever
        registers a bare 'mmu_nfc_reader' printer object (the manager only ever
        creates 'mmu_nfc_reader <name>', mmu_nfc_manager.py:138-146) and there is no
        klippy/extras/mmu_nfc_reader.py module either. So the optional bare
        [mmu_nfc_reader] section documented as supplying shared defaults
        (reader_type / i2c_bus / debug / timings) never takes effect - each reader
        falls back to its own hardcoded defaults.

        Change this test when that feature is actually wired up.
        """
        for reader in self.unit().nfc_manager.gate_readers:
            self.assertIsNone(reader._defaults)

    def unit(self):
        return self.hh.printer.lookup_object('mmu_machine').units[0]


class TestNfcEndstopWiring(unittest.TestCase):
    """
    The per-gate NFC reader doubles as a homing endstop so a tag can be detected
    during preload/jog. The compound endstop requires EXACTLY ONE real
    mcu.MCU_endstop plus any number of virtual ones
    (extras/mmu/mmu_sensor_utils.py:520), so the split asserted here is what makes
    _build_gate_nfc_compound succeed instead of returning None and silently falling
    back to a plain load (extras/mmu/mmu_filament_movement.py:329).
    """

    @classmethod
    def setUpClass(cls):
        cls.hh = session('nfc_per_gate')
        cls.hh.boot()

    @classmethod
    def tearDownClass(cls):
        cls.hh.close()

    def test_nfc_endstop_registered_on_gear_rail(self):
        rail = self.hh.printer.lookup_object('mmu_stepper unit0_gear').rail
        self.assertIn('mmu_nfc_0', rail.get_all_endstop_names())

    def test_nfc_endstop_is_virtual_but_gate_switch_is_real(self):
        import mcu
        from extras.mmu.mmu_sensor_utils import MmuVirtualEndstopSensor
        rail = self.hh.printer.lookup_object('mmu_stepper unit0_gear').rail
        nfc = rail.get_extra_endstop('mmu_nfc_0')[0]
        gate = rail.get_extra_endstop('mmu_exit_0')[0]
        self.assertIsInstance(nfc, MmuVirtualEndstopSensor)
        self.assertNotIsInstance(nfc, mcu.MCU_endstop)
        self.assertIsInstance(
            gate, mcu.MCU_endstop,
            'the gate switch must be a real MCU endstop or NFC-compound preload '
            'silently degrades to a plain load (mmu_filament_movement.py:329)')

    def test_endstop_uses_global_gate_numbering(self):
        """
        Per-gate sensor names use the GLOBAL gate number (SENSOR_NFC_PREFIX +
        global gate), which is what makes multi-unit setups address the right rail.
        """
        mgr = self.hh.printer.lookup_object('mmu_machine').units[0].nfc_manager
        self.assertEqual(sorted(mgr.gate_endstops), [0, 1, 2, 3])
        self.assertEqual(mgr.gate_endstops[0].name, 'mmu_nfc_0')


class TestCommonReaderWiring(unittest.TestCase):
    """
    REGRESSION GUARD for a config-template bug the harness found and that is now fixed.

    The [mmu_unit] `nfc_reader:` key used to be gated on MMU_HAS_SHARED_NFC_READER,
    which meant "shared across MMU UNITS" and carried `depends on MULTI_UNIT` - so on a
    single-unit machine it was unreachable. The [mmu_nfc_reader NAME] section rendered
    anyway, leaving the reader ORPHANED: MmuNfcManager never instantiated it and NFC
    silently did nothing, with no error and no warning.

    That was the primary use case, per the Kconfig's own help text: a single reader you
    present filament to before preload.

    The fix replaced it with MMU_HAS_COMMON_NFC_READER ("common NFC reader that can be
    used for all gates and bypass"), with no MULTI_UNIT dependency, gating BOTH the
    section and the unit key. These tests assert the reader is now genuinely wired up
    end to end, which is the part that was broken.
    """

    def test_reader_section_is_rendered(self):
        parser = cfg.assemble(cfg.render(profiles.get('nfc_single')))
        self.assertIn('mmu_nfc_reader unit0_nfc', parser.sections())

    def test_reader_is_attached_to_the_unit(self):
        parser = cfg.assemble(cfg.render(profiles.get('nfc_single')))
        unit = dict(parser.items('mmu_unit unit0'))
        self.assertEqual(unit.get('nfc_reader'), 'unit0_nfc',
                         'the common reader must be referenced by [mmu_unit], or it is '
                         'configured but never instantiated')
        self.assertNotIn('nfc_readers', unit,
                         'a common reader must not also declare per-gate readers')

    def test_a_reader_is_opt_in(self):
        """
        MMU_HAS_NFC_READER alone renders NO reader: both COMMON and PER_GATE default to
        n. Worth pinning so a profile cannot silently end up reader-less.
        """
        bare = profiles.BOXTURTLE.derive('nfc_bare', syms={'MMU_HAS_NFC_READER': True})
        parser = cfg.assemble(cfg.render(bare))
        self.assertFalse([s for s in parser.sections() if s.startswith('mmu_nfc_reader')])

    def test_common_reader_is_actually_instantiated(self):
        """
        The end-to-end proof: config load must produce a live reader object on the unit.
        This is exactly what the orphaning bug prevented.
        """
        hh = session('nfc_single')
        try:
            hh.boot()
            self.assertEqual(hh.errors, [])
            mgr = hh.printer.lookup_object('mmu_machine').units[0].nfc_manager
            self.assertIsNotNone(mgr.shared_reader,
                                 'the common reader was configured but not created')
            self.assertEqual(mgr.shared_reader.reader_type, 'rc522')
            self.assertTrue(all(r is None for r in mgr.gate_readers),
                            'a common reader must not populate per-gate slots')
        finally:
            hh.close()


if __name__ == '__main__':
    unittest.main()
