# ViViD board sections must be scoped to their unit.
#
# The VVD board's PARAM_MISC_HARDWARE block writes raw Klipper sections. A
# section name that ignores $(UNIT_NAME) only works for one unit name: the
# NFC readers were hard-coded as unit1_*, so a VVD named anything else
# pointed its gates at readers that don't exist, and two VVDs shared one
# temperature sensor (Klipper silently merges duplicate sections).

import re
import unittest

from test.hh import cfg, profiles, session

SECTION = re.compile(r'^\[([^\]]+)\]', re.M)
NFC_READERS = re.compile(r'^nfc_readers\s*:\s*(.*)$', re.M)


def _vvd(*unit_names):
    vvd = [unit for unit in profiles.get('ercf_vvd').units if unit.name == 'unit1'][0]
    base = profiles.Profile('vvd', syms=vvd.syms)
    return profiles.clone_across_units('vvd_' + '_'.join(unit_names), base, unit_names)


class TestVvdSectionsAreUnitScoped(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rendered = cfg.render(_vvd('unit0', 'unit1'))
        cls.text = '\n'.join(cls.rendered.values())

    def test_every_gate_nfc_reader_is_defined(self):
        defined = {name.split(None, 1)[1] for name in SECTION.findall(self.text)
                   if name.startswith('mmu_nfc_reader ')}
        referenced = {name.strip() for line in NFC_READERS.findall(self.text)
                      for name in line.split(',') if name.strip()}
        self.assertEqual(referenced, {'unit0_nfc01', 'unit0_nfc23', 'unit1_nfc01', 'unit1_nfc23'})
        self.assertEqual(referenced - defined, set())

    def test_no_section_is_defined_by_both_units(self):
        counts = {}
        for text in self.rendered.values():
            for name in SECTION.findall(text):
                counts[name] = counts.get(name, 0) + 1
        self.assertEqual(sorted(name for name, count in counts.items() if count > 1), [])

    def test_ptc_sensor_is_named_per_unit(self):
        sensors = [name for name in SECTION.findall(self.text) if 'ptc_ntc100k' in name]
        self.assertEqual(sensors, ['temperature_sensor unit0_ptc_ntc100k',
                                   'temperature_sensor unit1_ptc_ntc100k'])


    def test_two_vvds_boot_with_their_own_nfc_readers(self):
        hh = session(_vvd('unit0', 'unit1'))
        try:
            hh.boot()
            self.assertEqual(hh.errors, [])
            for unit in ('unit0', 'unit1'):
                with self.subTest(unit=unit):
                    for reader in ('nfc01', 'nfc23'):
                        self.assertIsNotNone(hh.printer.lookup_object(
                            'mmu_nfc_reader %s_%s' % (unit, reader), None))
        finally:
            hh.close()

if __name__ == '__main__':
    unittest.main()
