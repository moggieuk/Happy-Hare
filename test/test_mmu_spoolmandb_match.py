# Happy Hare test harness - SpoolmanDB enrichment for tags of any brand.
#
# A tag written with a phone app (OpenSpool...) carries brand, material and colour, and
# often the app's generic temperatures. When an unknown tag creates a filament, Moonraker
# looks the brand, material and closest colour up in Spoolman's copy of SpoolmanDB
# (/v1/external/filament). A match gives the manufacturer's name, temperatures, density and
# spool weight, and its SpoolmanDB id as external_id, so a filament already imported from
# SpoolmanDB in Spoolman's UI is reused instead of duplicated.
#
#   ./venv/bin/python -m unittest test.test_mmu_spoolmandb_match
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.test_mmu_moonraker import MoonrakerTestCase, UNKNOWN_UID

logging.getLogger().setLevel(logging.CRITICAL)

TAG = {
    'material': 'PETG',
    'brand': 'SUNLU',
    'color_hex': '1E9E3C',          # an app's green, close to Sunlu's
    'material_detail': 'PETG',
    'min_temp': 190,                # app defaults, not Sunlu's
    'max_temp': 230,
}
SUNLU_GREEN = {
    'id': 'sunlu_petg_green_1000_175_p', 'manufacturer': 'Sunlu', 'name': 'Green',
    'material': 'PETG', 'density': 1.23, 'weight': 1000.0, 'spool_weight': 135.0,
    'diameter': 1.75, 'color_hex': '1A9B3A', 'color_hexes': None,
    'extruder_temp': 255, 'bed_temp': 75,
}
SUNLU_TEAL = dict(SUNLU_GREEN, id='sunlu_petg_teal_1000_175_p', name='Teal', color_hex='00807F')
SUNLU_GREEN_PLA = dict(SUNLU_GREEN, id='sunlu_pla_green_1000_175_p', material='PLA')
ESUN_GREEN = dict(SUNLU_GREEN, id='esun_petg_green_1000_175_n', manufacturer='eSUN')


class SpoolmanDbMatchTestCase(MoonrakerTestCase):

    def setUp(self):
        super().setUp()
        self.hh.db.external_filaments = [SUNLU_TEAL, SUNLU_GREEN_PLA, ESUN_GREEN, SUNLU_GREEN]

    def scan(self, tag=TAG, prefer_db_temps=False):
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=1,
                            metadata=tag, save=True, silent=True, prefer_db_temps=prefer_db_temps)
        self.assertEqual(len(self.hh.db.created_spools), 1)
        return self.hh.db.spools[self.hh.db.created_spools[0]]

    def test_manufacturer_data_fills_the_new_filament(self):
        filament = self.scan()['filament']
        self.assertEqual(filament['external_id'], 'sunlu_petg_green_1000_175_p')
        self.assertEqual(filament['name'], 'Green')
        self.assertEqual(filament['density'], 1.23)
        self.assertEqual(filament['vendor']['name'], 'Sunlu')

    def test_tag_temperatures_win_by_default(self):
        filament = self.scan()['filament']
        self.assertEqual(filament['settings_extruder_temp'], 210)   # the tag's 190-230
        self.assertEqual(filament['settings_bed_temp'], 75)         # tag has none: filled from SpoolmanDB

    def test_manufacturer_temperatures_win_when_preferred(self):
        filament = self.scan(prefer_db_temps=True)['filament']
        self.assertEqual(filament['settings_extruder_temp'], 255)
        self.assertEqual(filament['settings_bed_temp'], 75)

    def test_spool_weight_comes_from_the_match(self):
        self.assertEqual(self.scan()['spool_weight'], 135.0)

    def test_a_filament_imported_from_spoolmandb_is_reused(self):
        fid = self.hh.db.add_filament(name='Green', material='PETG', vendor='Sunlu',
                                      color_hex='1A9B3A', external_id='sunlu_petg_green_1000_175_p')
        self.assertEqual(self.scan()['filament']['id'], fid)

    def test_a_distant_colour_does_not_match(self):
        filament = self.scan(dict(TAG, color_hex='FF00FF'))['filament']
        self.assertIsNone(filament['external_id'])
        self.assertEqual(filament['settings_extruder_temp'], 210)   # the tag's range

    def test_unknown_brand_keeps_the_tag_data(self):
        filament = self.scan(dict(TAG, brand='NoSuchBrand'))['filament']
        self.assertIsNone(filament['external_id'])
        self.assertEqual(filament['settings_extruder_temp'], 210)

    def test_material_detail_breaks_a_colour_tie(self):
        matte = dict(SUNLU_GREEN, id='sunlu_petg_mattegreen_1000_175_p', name='Matte Green')
        self.hh.db.external_filaments = [SUNLU_GREEN, matte]
        filament = self.scan(dict(TAG, material_detail='PETG Matte'))['filament']
        self.assertEqual(filament['external_id'], 'sunlu_petg_mattegreen_1000_175_p')

    def test_an_empty_list_is_not_cached(self):
        self.hh.db.external_filaments = []
        self.assertIsNone(self.scan()['filament']['external_id'])
        self.hh.db.external_filaments = [SUNLU_GREEN]
        self.hh.db.created_spools.clear()
        self.hh.call_remote('spoolman_get_spool_by_uid', uid='0A0B0C0D', gate=1,
                            metadata=dict(TAG, color_hex='1C9D3B'), save=True, silent=True)
        spool = self.hh.db.spools[self.hh.db.created_spools[0]]
        self.assertEqual(spool['filament']['external_id'], 'sunlu_petg_green_1000_175_p')

    def test_spoolman_without_the_endpoint_keeps_the_tag_data(self):
        self.hh.db.external_filaments = None
        filament = self.scan()['filament']
        self.assertIsNone(filament['external_id'])
        self.assertEqual(filament['settings_extruder_temp'], 210)


if __name__ == '__main__':
    unittest.main()
