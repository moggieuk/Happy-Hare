# Happy Hare MMU Software
#
# TigerTag registry download (components/mmu_server.py, tigertag_registry): Moonraker keeps
# copies of TigerTag's ID tables in config/mmu/optional/tigertag, downloading only what
# changed, and never replaces a good copy with a bad download.
#
#   ./venv/bin/python -m unittest test.test_mmu_tigertag_registry
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import logging
import os
import shutil
import tempfile
import unittest

from test.hh.moonraker import harness

logging.getLogger().setLevel(logging.CRITICAL)

STAMPS = {'versions': 50, 'types': 60, 'filament_diameters': 70, 'filament_materials': 100, 'brands': 200, 'aspects': 300, 'measure_units': 400, 'products': 999}
REGISTRY = {
    'last_update': STAMPS,
    'id_version': [{'id': 1542820452, 'version': '1.0', 'name': 'TigerTag'}],
    'id_type': [{'id': 142, 'label': 'Filament'}, {'id': 173, 'label': 'Resin'}],
    'id_diameter': [{'id': 56, 'label': '1.75'}, {'id': 221, 'label': '2.85'}],
    'id_material': [{'id': 24629, 'label': 'PLA High Speed', 'material_type': 'PLA'}],
    'id_brand': [{'id': 628, 'name': 'Buddy3D'}],
    'id_aspect': [{'id': 132, 'label': 'Fiber'}],
    'id_measure_unit': [{'id': 21, 'label': 'g'}],
}
TABLES = ('id_version', 'id_type', 'id_diameter', 'id_material', 'id_brand', 'id_aspect', 'id_measure_unit')


class RegistryTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = harness()
        self.server = self.hh.mmu_server
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, 'mmu', 'optional', 'tigertag')
        self.server._tigertag_cache_dir = lambda: self.cache
        self.hh.http.tigertag = json.loads(json.dumps(REGISTRY))

    def tearDown(self):
        self.hh.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def refresh(self):
        return self.hh.run(self.server._refresh_tigertag_registry())

    def fetched(self, since=0):
        return [u.rsplit('/', 1)[-1] for _, u, _ in self.hh.http.requests[since:] if 'TigerTag-RFID-Guide' in u]

    def cached(self, name):
        with open(os.path.join(self.cache, name + '.json'), encoding='utf-8') as f:
            return json.load(f)


class TestDownload(RegistryTestCase):

    def test_first_run_downloads_every_table(self):
        self.assertTrue(self.refresh())
        for name in TABLES:
            self.assertEqual(self.cached(name), REGISTRY[name])
        self.assertTrue(os.path.exists(os.path.join(self.cache, 'README.txt')))
        stamps = self.cached('last_update')
        keys = ('versions', 'types', 'filament_diameters', 'filament_materials', 'brands', 'aspects', 'measure_units')
        self.assertEqual({k: stamps[k] for k in keys}, {k: STAMPS[k] for k in keys})

    def test_unchanged_registry_downloads_only_the_stamps(self):
        self.refresh()
        mtimes = {n: os.stat(os.path.join(self.cache, n + '.json')).st_mtime_ns for n in TABLES + ('last_update',)}
        n = len(self.hh.http.requests)
        self.assertTrue(self.refresh())
        self.assertEqual(self.fetched(n), ['last_update.json'])
        self.assertEqual(mtimes, {k: os.stat(os.path.join(self.cache, k + '.json')).st_mtime_ns for k in mtimes},
                         'nothing rewritten, so the parser has nothing to re-read')

    def test_only_a_changed_table_is_downloaded(self):
        self.refresh()
        self.hh.http.tigertag['last_update']['brands'] = 201
        self.hh.http.tigertag['id_brand'].append({'id': 3308, 'name': 'American Filament'})
        n = len(self.hh.http.requests)
        self.assertTrue(self.refresh())
        self.assertEqual(self.fetched(n), ['last_update.json', 'id_brand.json'])
        self.assertEqual(len(self.cached('id_brand')), 2)
        self.assertEqual(self.cached('last_update')['brands'], 201)

    def test_nothing_is_sent_but_the_request(self):
        """GitHub gets no printer name: the header wrapper only targets Spoolman."""
        self.refresh()
        for _, url, headers in self.hh.http.requests:
            if 'TigerTag-RFID-Guide' in url:
                self.assertEqual(headers, {}, url)


class TestFailures(RegistryTestCase):

    def test_offline_writes_nothing_and_retries(self):
        self.hh.http.tigertag = {}
        self.assertFalse(self.refresh())
        self.assertFalse(os.path.exists(self.cache))

    def test_invalid_table_keeps_the_previous_copy_and_is_retried(self):
        self.refresh()
        self.hh.http.tigertag['last_update']['brands'] = 201
        self.hh.http.tigertag['id_brand'] = {'error': 'not a table'}
        self.assertFalse(self.refresh(), 'retry sooner')
        self.assertEqual(self.cached('id_brand'), REGISTRY['id_brand'], 'good copy kept')
        self.assertEqual(self.cached('last_update')['brands'], 200, 'stamp not advanced, so it is fetched again')

    def test_entries_without_an_id_or_name_are_refused(self):
        self.hh.http.tigertag['id_brand'] = [{'id': 'x', 'name': 'Bad'}]
        self.assertFalse(self.refresh())
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'id_brand.json')))
        self.assertEqual(self.cached('id_material'), REGISTRY['id_material'], 'the other tables still land')

    def test_a_newly_issued_version_is_downloaded(self):
        self.refresh()
        self.hh.http.tigertag['last_update']['versions'] = 51
        self.hh.http.tigertag['id_version'].append({'id': 0x1234ABCD, 'version': '1.1', 'name': 'TigerTag 1.1'})
        n = len(self.hh.http.requests)
        self.assertTrue(self.refresh())
        self.assertEqual(self.fetched(n), ['last_update.json', 'id_version.json'])
        self.assertEqual(len(self.cached('id_version')), 2)

    def test_boolean_ids_are_refused(self):
        """JSON true is an int in Python; a version table must never contain one."""
        self.hh.http.tigertag['id_version'] = [{'id': True, 'version': '1.0', 'name': 'Bad'}]
        self.assertFalse(self.refresh())
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'id_version.json')))

    def test_rows_failing_the_schema_are_dropped_not_the_table(self):
        self.hh.http.tigertag['id_brand'] = [{'id': 628, 'name': 'Buddy3D'}, {'id': 70000, 'name': 'Too wide'},
                                             {'id': 629, 'name': 'Semi;colon'}]
        self.assertTrue(self.refresh())
        self.assertEqual(self.cached('id_brand'), [{'id': 628, 'name': 'Buddy3D'}])

    def test_empty_chip_version_is_dropped_quietly(self):
        self.hh.http.tigertag['id_version'] = [{'id': 0, 'version': '0.0', 'name': 'RFID Empty'},
                                               {'id': 1542820452, 'version': '1.0', 'name': 'TigerTag'}]
        self.assertTrue(self.refresh())
        self.assertEqual([r['id'] for r in self.cached('id_version')], [1542820452])

    def test_missing_cached_file_is_downloaded_again(self):
        self.refresh()
        os.remove(os.path.join(self.cache, 'id_aspect.json'))
        n = len(self.hh.http.requests)
        self.refresh()
        self.assertEqual(self.fetched(n), ['last_update.json', 'id_aspect.json'])


class TestTurnedOffDeletesTheDownload(RegistryTestCase):
    """Turning nfc_tigertag_registry off deletes the downloaded copies - but only the files the
    download writes, and only on Klipper's word that it is off."""

    def klipper_ready(self, value, config=None):
        self.hh.klippy.config_sections = config if config is not None else \
            {'mmu_parameters': {'nfc_tigertag_registry': value}}
        self.hh.run(self.server._handle_klippy_ready())

    def downloaded(self):
        self.assertTrue(self.refresh())
        self.assertTrue(os.path.isfile(os.path.join(self.cache, 'id_brand.json')))

    def test_turned_off_deletes_the_folder(self):
        self.downloaded()
        self.klipper_ready('0')
        self.assertFalse(os.path.exists(self.cache))

    def test_left_over_temp_files_go_too(self):
        self.downloaded()
        open(os.path.join(self.cache, 'id_brand.json.tmp'), 'w').close()
        self.klipper_ready('0')
        self.assertFalse(os.path.exists(self.cache))

    def test_files_it_did_not_write_are_kept(self):
        self.downloaded()
        with open(os.path.join(self.cache, 'my notes.txt'), 'w') as f:
            f.write('mine')
        self.klipper_ready('0')
        self.assertEqual(os.listdir(self.cache), ['my notes.txt'])

    def test_on_keeps_the_download(self):
        self.downloaded()
        self.klipper_ready('1')
        self.assertTrue(os.path.isfile(os.path.join(self.cache, 'id_brand.json')))

    def test_off_with_nothing_downloaded_is_harmless(self):
        self.klipper_ready('0')
        self.assertFalse(os.path.exists(self.cache))

    def test_no_happy_hare_section_deletes_nothing(self):
        self.downloaded()
        self.klipper_ready(None, config={'printer': {}})
        self.assertTrue(os.path.isfile(os.path.join(self.cache, 'id_brand.json')))

    def test_unreadable_klipper_config_deletes_nothing(self):
        from unittest import mock
        self.downloaded()

        async def fail(*args, **kwargs):
            raise RuntimeError('Klipper went away')
        with mock.patch.object(self.server.klippy_apis, 'query_objects', fail):
            self.hh.run(self.server._handle_klippy_ready())
        self.assertTrue(os.path.isfile(os.path.join(self.cache, 'id_brand.json')))


class TestSwitch(unittest.TestCase):
    """Klipper owns the switch: nfc_tigertag_registry in [mmu_parameters], one per printer."""

    def setUp(self):
        self.hh = harness()
        self.server = self.hh.mmu_server
        self.addCleanup(self.hh.close)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hh.http.tigertag = json.loads(json.dumps(REGISTRY))
        self.klipper_identified()

    def klipper_identified(self):
        self.server.server.klippy_info = {'config_file': os.path.join(self.tmp, 'printer.cfg')}
        self.hh.run(self.server._update_tigertag_dir())

    def klipper_ready(self, value=None, units=None):
        """Klipper's config as Moonraker sees it: [mmu_parameters] with the switch (omitted if
        value is None), plus {unit name: options} as [mmu_unit_parameters <unit>] sections"""
        config = {f'mmu_unit_parameters {name}': options for name, options in (units or {}).items()}
        config['mmu_parameters'] = {} if value is None else {'nfc_tigertag_registry': value}
        self.hh.klippy.config_sections = config
        self.hh.run(self.server._handle_klippy_ready())

    def test_off_by_default(self):
        self.klipper_ready()
        self.assertIsNone(self.server._tigertag_task)

    def test_on_in_mmu_parameters_turns_it_on(self):
        self.klipper_ready('1')
        self.assertIsNotNone(self.server._tigertag_task)

    def test_a_unit_section_does_not_turn_it_on(self):
        """It is printer-wide: an nfc_tigertag_registry left in a unit's section is ignored"""
        self.klipper_ready('0', units={'unit0': {'nfc_tigertag_registry': '1', 'nfc_deep_read': '1'}})
        self.assertIsNone(self.server._tigertag_task)

    def test_no_mmu_parameters_section_is_off(self):
        self.hh.klippy.config_sections = {'mmu_unit_parameters unit0': {'nfc_deep_read': '1'}}
        self.hh.run(self.server._handle_klippy_ready())
        self.assertIsNone(self.server._tigertag_task)

    def test_turned_off_stops_the_job(self):
        self.klipper_ready('1')
        task = self.server._tigertag_task
        self.klipper_ready('0')
        self.assertIsNone(self.server._tigertag_task)
        self.hh.run(__import__('asyncio').sleep(0))
        self.assertTrue(task.cancelled())

    def test_klipper_restart_keeps_one_job(self):
        self.klipper_ready('1')
        task = self.server._tigertag_task
        self.klipper_ready('1')
        self.assertIs(self.server._tigertag_task, task)

    def moonraker_restart(self):
        """Same database, new Moonraker process: Klipper hasn't identified yet"""
        if self.server._tigertag_task is not None:
            self.hh.run(self.server.close())
        self.server._tigertag_dir = None
        self.server._tigertag_wanted = False
        self.hh.run(self.server.component_init())

    def test_restart_waits_for_klipper_to_identify(self):
        """Last seen on: it doesn't download until Klipper reports its config file..."""
        self.klipper_ready('1')
        self.moonraker_restart()
        self.assertIsNone(self.server._tigertag_task, 'no folder yet, so nowhere to write')
        self.klipper_identified()
        self.assertIsNotNone(self.server._tigertag_task, '...then starts without waiting for ready')

    def test_restart_stays_off_when_last_seen_off(self):
        self.klipper_ready('1')
        self.klipper_ready('0')
        self.moonraker_restart()
        self.klipper_identified()
        self.assertIsNone(self.server._tigertag_task)

    def test_first_ever_start_waits_for_klipper(self):
        self.moonraker_restart()
        self.klipper_identified()
        self.assertIsNone(self.server._tigertag_task)

    def test_klipper_corrects_a_stale_setting(self):
        self.klipper_ready('1')
        self.moonraker_restart()
        self.klipper_identified()          # starts from the remembered value...
        self.klipper_ready('0')            # ...then Klipper says it was turned off
        self.assertIsNone(self.server._tigertag_task)

    def test_close_stops_the_job(self):
        self.klipper_ready('1')
        self.hh.run(self.server.close())
        self.assertIsNone(self.server._tigertag_task)


class TestCacheFollowsKlipperConfig(unittest.TestCase):
    """The cache goes beside Klipper's active config file, which may be in a subfolder of the
    config root. Moonraker only learns it when Klipper identifies; it never guesses."""

    def setUp(self):
        self.hh = harness()
        self.addCleanup(self.hh.close)
        self.server = self.hh.mmu_server
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hh.http.tigertag = json.loads(json.dumps(REGISTRY))

    def cache(self, *sub):
        return os.path.join(self.tmp, 'config', *sub, 'mmu', 'optional', 'tigertag')

    def has_tables(self, cache_dir):
        return os.path.isfile(os.path.join(cache_dir, 'id_brand.json'))

    def klipper_identified(self, *sub):
        self.server.server.klippy_info = {'config_file': os.path.join(self.tmp, 'config', *sub, 'printer.cfg')}
        self.hh.run(self.server._update_tigertag_dir())

    def klipper_ready(self, value):
        self.hh.klippy.config_sections = {'mmu_parameters': {'nfc_tigertag_registry': value}}
        self.hh.run(self.server._handle_klippy_ready())

    def settle(self):
        """Let a just-started download loop run its first refresh (it then sleeps)"""
        import asyncio
        for _ in range(3):
            self.hh.run(asyncio.sleep(0))

    def test_config_in_a_subfolder_downloads_beside_it(self):
        self.klipper_identified('machine')
        self.klipper_ready('1')
        self.settle()
        self.assertTrue(self.has_tables(self.cache('machine')))
        self.assertFalse(os.path.exists(self.cache()), 'nothing under the config root')

    def test_standard_layout(self):
        self.klipper_identified()
        self.klipper_ready('1')
        self.settle()
        self.assertTrue(self.has_tables(self.cache()))

    def test_nothing_is_downloaded_before_klipper_identifies(self):
        """No config-root fallback: until Klipper says where, there is nowhere to write"""
        self.klipper_ready('1')
        self.settle()
        self.assertIsNone(self.server._tigertag_task)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'config')))

    def test_no_config_file_reported_changes_nothing(self):
        self.klipper_identified('machine')
        self.server.server.klippy_info = {}
        self.hh.run(self.server._update_tigertag_dir())
        self.assertEqual(self.server._tigertag_cache_dir(), self.cache('machine'))

    def test_unchanged_folder_keeps_the_job_and_the_files(self):
        self.klipper_identified('machine')
        self.klipper_ready('1')
        self.settle()
        task = self.server._tigertag_task
        self.klipper_identified('machine')          # A Klipper RESTART
        self.assertIs(self.server._tigertag_task, task, 'no re-check of GitHub on every restart')
        self.assertTrue(self.has_tables(self.cache('machine')))

    def test_moved_config_moves_the_download(self):
        self.klipper_identified()
        self.klipper_ready('1')
        self.settle()
        task = self.server._tigertag_task
        self.klipper_identified('machine')          # Klipper restarted with config/machine/printer.cfg
        self.assertIsNot(self.server._tigertag_task, task, 'the download starts again at once')
        self.settle()
        self.assertTrue(self.has_tables(self.cache('machine')))
        self.assertFalse(os.path.exists(self.cache()), 'the old copies are deleted')

    def test_move_keeps_files_it_did_not_write(self):
        self.klipper_identified()
        self.klipper_ready('1')
        self.settle()
        with open(os.path.join(self.cache(), 'my notes.txt'), 'w') as f:
            f.write('mine')
        self.klipper_identified('machine')
        self.assertEqual(os.listdir(self.cache()), ['my notes.txt'])

    def test_turned_off_after_a_move_deletes_the_current_folder(self):
        self.klipper_identified()
        self.klipper_ready('1')
        self.settle()
        self.klipper_identified('machine')
        self.settle()
        self.klipper_ready('0')
        self.assertFalse(os.path.exists(self.cache('machine')))
        self.assertFalse(os.path.exists(self.cache()))

    def test_moonraker_restart_keeps_no_path(self):
        """Only the switch is remembered: the folder is learned again from Klipper"""
        self.klipper_identified('machine')
        self.klipper_ready('1')
        self.hh.run(self.server.close())
        self.server._tigertag_dir = None
        self.server._tigertag_wanted = False
        self.hh.run(self.server.component_init())
        self.assertIsNone(self.server._tigertag_cache_dir())
        self.assertIsNone(self.server._tigertag_task)
        self.klipper_identified('machine')
        self.assertIsNotNone(self.server._tigertag_task)


class TestCacheDir(unittest.TestCase):
    """tigertag_registry.cache_dir: the one rule both Klipper and Moonraker use."""

    @classmethod
    def setUpClass(cls):
        from test.hh import install
        install()
        from extras.mmu.unit.nfc import tigertag_registry
        cls.reg = tigertag_registry

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_beside_the_config_file(self):
        cfg = os.path.join(self.tmp, 'machine', 'printer.cfg')
        self.assertEqual(self.reg.cache_dir(cfg), os.path.join(self.tmp, 'machine', 'mmu', 'optional', 'tigertag'))

    def test_relative_path_is_refused(self):
        """Klipper and Moonraker run from different working folders"""
        self.assertIsNone(self.reg.cache_dir('printer.cfg'))
        self.assertIsNone(self.reg.cache_dir('config/printer.cfg'))

    def test_nothing_is_refused(self):
        for value in (None, '', 42):
            self.assertIsNone(self.reg.cache_dir(value), value)

    def test_home_is_expanded(self):
        self.assertEqual(self.reg.cache_dir('~/printer_data/config/printer.cfg'),
                         self.reg.cache_dir(os.path.expanduser('~/printer_data/config/printer.cfg')))

    def test_symlinked_config_resolves_to_its_real_folder(self):
        real = os.path.join(self.tmp, 'real')
        os.makedirs(real)
        open(os.path.join(real, 'printer.cfg'), 'w').close()
        os.symlink(real, os.path.join(self.tmp, 'link'))
        self.assertEqual(self.reg.cache_dir(os.path.join(self.tmp, 'link', 'printer.cfg')),
                         os.path.join(real, 'mmu', 'optional', 'tigertag'))

    def test_moonraker_uses_the_same_rule(self):
        """Moonraker's copy is loaded by path; it must be this function, not a lookalike"""
        hh = harness()
        self.addCleanup(hh.close)
        cfg = os.path.join(self.tmp, 'machine', 'printer.cfg')
        hh.mmu_server.server.klippy_info = {'config_file': cfg}
        hh.run(hh.mmu_server._update_tigertag_dir())
        self.assertEqual(hh.mmu_server._tigertag_cache_dir(), self.reg.cache_dir(cfg))


class TestOneSchema(unittest.TestCase):
    """The generator, Moonraker's download and Klipper's loader all follow tigertag_registry.py.
    These fail if any of them grows its own idea of which tables exist or what a row may hold."""

    @classmethod
    def setUpClass(cls):
        from test.hh import install
        install()
        from extras.mmu.unit.nfc import tag_parser, tigertag_registry
        cls.parser, cls.schema = tag_parser, tigertag_registry

    def generator(self):
        import importlib.util
        path = os.path.join(os.path.dirname(__file__), '..', 'utils', 'update_tigertag_tables.py')
        spec = importlib.util.spec_from_file_location('update_tigertag_tables', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def registry_dir(self, overrides=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        files = dict(REGISTRY, **(overrides or {}))
        files['id_version'] = files['id_version'] + [{'id': 1816240865, 'version': '1.0', 'name': 'TigerTag Init'}]
        for name, rows in files.items():
            with open(os.path.join(tmp, name + '.json'), 'w', encoding='utf-8') as f:
                json.dump(rows, f)
        return tmp

    def test_generated_block_is_exactly_the_schema_tables(self):
        import re
        with open(self.parser.__file__, encoding='utf-8') as f:
            src = f.read()
        block = src[src.index('# BEGIN TigerTag registry tables'):src.index('# END TigerTag registry tables')]
        self.assertEqual(re.findall(r'^(_TIGERTAG_\w+): dict', block, re.M), [t.attr for t in self.schema.TABLES])

    def test_built_in_tables_pass_the_schema_rules(self):
        for table in self.schema.TABLES:
            for row_id, name in getattr(self.parser, table.attr).items():
                row = {'id': row_id, table.field: name, 'version': '1.0'}
                self.assertTrue(self.schema.row_ok(table, row), (table.key, row_id, name))

    def test_moonraker_downloads_exactly_the_schema_tables(self):
        hh = harness()
        self.addCleanup(hh.close)
        cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cache, True)
        hh.mmu_server._tigertag_cache_dir = lambda: cache
        hh.http.tigertag = json.loads(json.dumps(REGISTRY))
        hh.run(hh.mmu_server._refresh_tigertag_registry())
        fetched = [u.rsplit('/', 1)[-1] for _, u, _ in hh.http.requests if 'TigerTag-RFID-Guide' in u]
        self.assertEqual(fetched, [self.schema.STAMPS_FILE + '.json'] + [t.file + '.json' for t in self.schema.TABLES])

    def test_moonraker_uses_the_same_module(self):
        """Loaded by path from the component's real location, i.e. this checkout's file"""
        hh = harness()
        self.addCleanup(hh.close)
        loaded = hh.mmu_server._load_tigertag_schema()
        self.assertEqual(os.path.realpath(loaded.__file__), os.path.realpath(self.schema.__file__))

    def test_missing_schema_disables_the_download(self):
        hh = harness()
        self.addCleanup(hh.close)
        hh.mmu_server_mod.TIGERTAG_SCHEMA = os.path.join('no', 'such', 'tigertag_registry.py')
        self.addCleanup(setattr, hh.mmu_server_mod, 'TIGERTAG_SCHEMA',
                        os.path.join('extras', 'mmu', 'unit', 'nfc', 'tigertag_registry.py'))
        hh.run(hh.mmu_server._tigertag_registry_loop())     # returns instead of looping
        self.assertEqual([u for _, u, _ in hh.http.requests if 'TigerTag-RFID-Guide' in u], [])

    def test_loader_reads_every_schema_table(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = [(t, dict(t)) for t in (getattr(self.parser, s.attr) for s in self.schema.TABLES)]
        self.addCleanup(lambda: [(t.clear(), t.update(c)) for t, c in saved])
        for table in self.schema.TABLES:
            row = {'id': 0x12345678 if table.id_bits == 32 else (1 << table.id_bits) - 2,
                   table.field: '9.5' if table.check == 'diameter' else 'New ' + table.key, 'version': '1.0'}
            with open(os.path.join(tmp, table.file + '.json'), 'w', encoding='utf-8') as f:
                json.dump([row], f)
        self.assertEqual(self.parser.load_tigertag_registry(tmp), len(self.schema.TABLES))

    def test_generator_builds_every_schema_table(self):
        gen = self.generator()
        block, _ = gen.build_block(self.registry_dir())
        for table in self.schema.TABLES:
            self.assertIn(table.attr + ': dict[int, str] = {', block)

    def test_generator_refuses_a_row_the_schema_rejects(self):
        gen = self.generator()
        with self.assertRaisesRegex(ValueError, 'schema rules'):
            gen.build_block(self.registry_dir({'id_brand': [{'id': 628, 'name': 'Bad;name'}]}))


if __name__ == '__main__':
    unittest.main()
