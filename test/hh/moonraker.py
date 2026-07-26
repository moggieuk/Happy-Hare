# Happy Hare test harness - a fake Moonraker for components/mmu_server.py.
#
# mmu_server.py is a Moonraker component: async, asyncio-based, and completely
# separate from the Klipper half. It needs no Klipper at all to test, which is why
# this milestone is independent of the bootup work.
#
# THE CONSTRUCTION BLOCKER. MmuServer.__init__ unconditionally calls
# setup_placeholder_processor (mmu_server.py:174), which does
# `from .file_manager import file_manager` (:1691) to swap Moonraker's gcode metadata
# script. On a real install this file lives at
# $(MOONRAKER_HOME)/moonraker/components/mmu_server.py so `.` is moonraker.components
# and file_manager is a sibling. In this repo `components/` holds only mmu_server.py,
# so the import fails and MmuServer cannot even be constructed - which is why the
# legacy test/components/test_mmu_server.py has been failing at setUp. We inject a
# stub into sys.modules before import.
#
# THE ACTIVATION GATE. Almost every public method early-returns unless
# _mmu_backend_enabled() (:293-296) is true, and that needs klippy_apis to report an
# 'mmu' object that is enabled. A fake that does not do this leaves every
# MMU_GATE_MAP-emitting branch silently no-op, so tests would pass having exercised
# nothing. FakeKlippyApis therefore serves {"mmu": {"enabled": True, ...}} by default.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import sys
import json
import types
import asyncio
import logging

from . import spoolman as spoolman_mod

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPOOLMAN_URL = 'http://spoolman.test/api'


def install_component_stubs():
    """
    Make `components.mmu_server` importable outside Moonraker. Idempotent.
    Returns the imported mmu_server module.
    """
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    if 'components.file_manager' not in sys.modules:
        pkg = sys.modules.get('components')
        if pkg is None:
            pkg = types.ModuleType('components')
            pkg.__path__ = [os.path.join(REPO_ROOT, 'components')]
            sys.modules['components'] = pkg
        fm_pkg = types.ModuleType('components.file_manager')
        inner = types.ModuleType('components.file_manager.file_manager')
        # The only thing mmu_server touches (:1692)
        inner.METADATA_SCRIPT = None
        fm_pkg.file_manager = inner
        sys.modules['components.file_manager'] = fm_pkg
        sys.modules['components.file_manager.file_manager'] = inner

    from components import mmu_server
    return mmu_server


class FakeHttpClient:
    """
    Moonraker's HttpClient surface as used by mmu_server: get / post / request, each
    returning an object with .status_code / .has_error() / .json().

    Routes Spoolman /v1/... traffic into the in-memory store and serves the two
    hardcoded donkie.github.io SpoolmanDB URLs from fixtures. Those two have no
    config flag to disable them (mmu_server.py:95-96), so if they were not
    intercepted every test would make real internet calls.
    """

    def __init__(self, spoolman, spoolmandb=True):
        self.spoolman = spoolman
        self.spoolmandb = spoolmandb
        self.requests = []          # [(method, url)] assertion surface

    async def request(self, method, url, body=None, **kwargs):
        self.requests.append((method, url))
        if 'donkie.github.io' in url:
            return self._spoolmandb(url)
        return self.spoolman.handle(method.upper(), url, body)

    async def get(self, url, **kwargs):
        return await self.request('GET', url, None, **kwargs)

    async def post(self, url, body=None, **kwargs):
        return await self.request('POST', url, body, **kwargs)

    async def patch(self, url, body=None, **kwargs):
        return await self.request('PATCH', url, body, **kwargs)

    async def delete(self, url, **kwargs):
        return await self.request('DELETE', url, None, **kwargs)

    def _spoolmandb(self, url):
        if not self.spoolmandb:
            # Exercise HH's documented offline degradation to DENSITY_FALLBACK
            return spoolman_mod.Response(503, error='SpoolmanDB unreachable (test)')
        if 'materials.json' in url:
            return spoolman_mod.Response(200, spoolman_mod.SPOOLMANDB_MATERIALS)
        if 'bambulab.json' in url:
            return spoolman_mod.Response(200, spoolman_mod.SPOOLMANDB_BAMBU)
        return spoolman_mod.Response(404)

    def requests_to(self, fragment):
        return [(m, u) for m, u in self.requests if fragment in u]


class FakeSpoolmanComponent:
    """
    Moonraker's own [spoolman] component. mmu_server reaches through it for
    spoolman_url, its http_client (_fetch_spool_info uses
    self.spoolman.http_client.request, :361) and _get_response_error.
    """

    def __init__(self, http_client, url=SPOOLMAN_URL):
        self.spoolman_url = url
        self.http_client = http_client
        self.set_active_calls = []      # served by Moonraker, not mmu_server

    def _get_response_error(self, response):
        return getattr(response, 'error', None) or 'http %s' % (response.status_code,)

    async def set_active_spool(self, spool_id):
        self.set_active_calls.append(spool_id)


class FakeKlippyApis:
    """
    The Klipper-facing side. run_gcode is the ONLY channel from Moonraker back into
    Klipper, and every MMU_GATE_MAP / MMU_LOG callback goes through it.

    By default gcode is queued rather than executed: production is fire-and-forget
    async, and the round-trip driver alternates draining this queue with draining
    webhooks, which is what keeps ordering deterministic without nested event loops.
    Set `sink` to dispatch straight into a live Klipper gcode dispatcher instead.
    """

    def __init__(self, num_gates=4, mmu_enabled=True):
        self.num_gates = num_gates
        self.mmu_enabled = mmu_enabled
        self.mmu_present = True
        self.gcode = []             # every command sent, in order
        self.queue = []             # undrained commands
        self.sink = None
        self.pause_calls = 0
        self.extra_status = {}

    async def get_object_list(self):
        objects = ['gcode', 'toolhead', 'print_stats']
        if self.mmu_present:
            objects.append('mmu')
        return objects

    async def query_objects(self, objects, default=None):
        if 'mmu' in objects and self.mmu_present:
            status = {'enabled': self.mmu_enabled, 'num_gates': self.num_gates}
            status.update(self.extra_status)
            return {'mmu': status}
        return {}

    async def subscribe_objects(self, objects, callback=None, default=None):
        return await self.query_objects(objects)

    async def run_gcode(self, script, default=None):
        self.gcode.append(script)
        if self.sink is not None:
            self.sink(script)
        else:
            self.queue.append(script)
        return 'ok'

    async def pause_print(self, default=None):
        self.pause_calls += 1
        return 'ok'

    # -- test-facing --------------------------------------------------------
    def drain(self):
        pending, self.queue = self.queue, []
        return pending

    def commands(self, startswith=None):
        if startswith is None:
            return list(self.gcode)
        return [c for c in self.gcode if c.startswith(startswith)]


class FakeDatabase:
    """Moonraker's database component - mmu_server stores lane data here."""

    def __init__(self):
        self.store = {}

    async def get_item(self, namespace, key=None, default=None):
        ns = self.store.setdefault(namespace, {})
        if key is None:
            return ns
        # Moonraker supports dotted keys
        node = ns
        for part in key.split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    async def insert_item(self, namespace, key, value):
        ns = self.store.setdefault(namespace, {})
        parts = key.split('.')
        for part in parts[:-1]:
            ns = ns.setdefault(part, {})
        ns[parts[-1]] = value

    async def delete_item(self, namespace, key, drop_empty_db=False):
        ns = self.store.setdefault(namespace, {})
        parts = key.split('.')
        for part in parts[:-1]:
            ns = ns.get(part, {})
        return ns.pop(parts[-1], None)

    async def list_namespaces(self):
        return list(self.store)


class FakeConfigHelper:
    """Moonraker's ConfigHelper, as used by MmuServer.__init__."""

    def __init__(self, server, options=None, sections=('spoolman',)):
        self._server = server
        self._options = dict(options or {})
        self._sections = set(sections)

    def get_server(self):
        return self._server

    def has_section(self, section):
        return section in self._sections

    def has_option(self, option):
        return option in self._options

    def get(self, option, default=None):
        return self._options.get(option, default)

    def getint(self, option, default=None):
        return int(self._options.get(option, default))

    def getfloat(self, option, default=None):
        return float(self._options.get(option, default))

    def getboolean(self, option, default=None):
        return bool(self._options.get(option, default))


class FakeServer:
    """Moonraker's Server object."""

    def __init__(self, spoolman_db=None, num_gates=4, hostname='testprinter',
                 spoolmandb=True, mmu_enabled=True):
        self.spoolman_db = spoolman_db or spoolman_mod.InMemorySpoolman()
        self.http_client = FakeHttpClient(self.spoolman_db, spoolmandb=spoolmandb)
        self.klippy_apis = FakeKlippyApis(num_gates=num_gates,
                                         mmu_enabled=mmu_enabled)
        self.database = FakeDatabase()
        self.spoolman = FakeSpoolmanComponent(self.http_client)
        self.hostname = hostname
        self.components = {
            'http_client': self.http_client,
            'klippy_apis': self.klippy_apis,
            'database': self.database,
            'spoolman': self.spoolman,
        }
        # -- assertion surfaces -------------------------------------------
        self.remote_methods = {}    # name -> handler
        self.events = []            # [(name, payload)] every send_event
        self.warnings = []

    def get_host_info(self):
        return {'hostname': self.hostname, 'address': '127.0.0.1', 'port': 7125}

    def lookup_component(self, name, default=None):
        return self.components.get(name, default)

    def load_component(self, config, name, default=None):
        return self.components.get(name, default)

    def register_remote_method(self, name, handler):
        self.remote_methods[name] = handler

    def register_endpoint(self, *args, **kwargs):
        pass

    def register_notification(self, *args, **kwargs):
        pass

    def register_event_handler(self, event, handler):
        pass

    def send_event(self, name, *args):
        payload = args[0] if len(args) == 1 else args
        self.events.append((name, payload))

    def add_warning(self, msg, **kwargs):
        self.warnings.append(msg)

    def get_app_args(self):
        return {'software_version': 'v0.9.3-harness'}

    def is_running(self):
        return True

    # -- test-facing --------------------------------------------------------
    def events_named(self, name):
        return [p for n, p in self.events if n == name]


class MoonrakerHarness:
    """
    One fake-Moonraker session holding a REAL MmuServer.

    Drives its own asyncio loop: mmu_server is async and serialises everything
    through an asyncio.Lock (mmu_server.py:151), so tests need a loop to run
    coroutines on. run() is the pump.
    """

    def __init__(self, spools=None, num_gates=4, hostname='testprinter',
                 spoolman_version=spoolman_mod.SPOOLMAN_VERSION,
                 spoolmandb=True, mmu_enabled=True, options=None,
                 with_extra_fields=True):
        self.mmu_server_mod = install_component_stubs()
        self.db = spoolman_mod.InMemorySpoolman(version=spoolman_version)
        if with_extra_fields:
            # A Spoolman that HH has already initialised once. Without this the very
            # first _init_spoolman POSTs the three extra fields, which is itself
            # worth testing - hence the flag.
            for key in (spoolman_mod.FIELD_PRINTER, spoolman_mod.FIELD_GATE,
                        spoolman_mod.FIELD_RFID):
                self.db.fields['spool'][key] = {'name': key}
        for spec in (spools or ()):
            self.db.add_spool(**spec)
        self.server = FakeServer(self.db, num_gates=num_gates, hostname=hostname,
                                 spoolmandb=spoolmandb, mmu_enabled=mmu_enabled)
        self.config = FakeConfigHelper(self.server, options=options)
        self.loop = asyncio.new_event_loop()
        self.mmu_server = self.mmu_server_mod.MmuServer(self.config)
        # Keep the miss-cache clock under test control rather than sleeping 10s
        self._monotonic = 1000.
        self.mmu_server_mod.time = _FakeTime(self)

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        try:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
        except RuntimeError:
            pass
        self.loop.close()

    def run(self, coro):
        """Run a coroutine to completion on this harness's loop."""
        return self.loop.run_until_complete(coro)

    def component_init(self):
        """
        Moonraker's post-construction hook. It fires _init_spoolman as a
        fire-and-forget task (mmu_server.py:202), so we must let the loop drain or
        none of its effects are visible.
        """
        self.run(self.mmu_server.component_init())
        self.drain_tasks()
        return self

    def drain_tasks(self):
        pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
        if pending:
            self.run(asyncio.gather(*pending, return_exceptions=True))

    def call_remote(self, name, **kwargs):
        """Invoke a registered remote method exactly as Klipper's webhooks would."""
        handler = self.server.remote_methods.get(name)
        if handler is None:
            raise KeyError('no remote method %r; registered: %s'
                           % (name, ', '.join(sorted(self.server.remote_methods))))
        return self.run(handler(**kwargs))

    # -- clock -------------------------------------------------------------
    def advance(self, seconds):
        """
        Move the miss-cache clock. NFC_UID_MISS_TTL is 10s (mmu_server.py:88) and is
        measured with time.monotonic(), so this beats sleeping.
        """
        self._monotonic += seconds

    def reset_spoolmandb_cache(self):
        """
        Process-lifetime caches: None = unfetched, {}/[] = fetched-and-failed, and a
        failed fetch is never retried (mmu_server.py:146-148). Must be reset between
        tests that care.
        """
        self.mmu_server._spoolmandb_materials = None
        self.mmu_server._spoolmandb_bambu = None
        self.mmu_server._spoolmandb_bambu_mfr = None

    # -- shortcuts ---------------------------------------------------------
    @property
    def klippy(self):
        return self.server.klippy_apis

    @property
    def http(self):
        return self.server.http_client

    def gcode(self, startswith=None):
        return self.klippy.commands(startswith)


class _FakeTime:
    """Stands in for the `time` module inside mmu_server so monotonic is ours."""

    def __init__(self, harness):
        self._harness = harness

    def monotonic(self):
        return self._harness._monotonic

    def time(self):
        return self._harness._monotonic

    def __getattr__(self, name):
        import time as _real
        return getattr(_real, name)


def harness(**kwargs):
    return MoonrakerHarness(**kwargs)
