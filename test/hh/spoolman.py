# Happy Hare test harness - an in-memory Spoolman.
#
# Deliberately a working store rather than per-test HTTP mocks: auto-create then
# really creates a vendor/filament/spool, and the NEXT lookup of the same tag really
# resolves it. That round trip IS the logic under test - a mock returning a canned
# 200 would prove nothing about whether the UID actually got registered.
#
# Wire-format details that matter (getting these wrong makes HH silently see no data):
#   - `extra` values are JSON-ENCODED STRINGS, not raw values. HH does
#     json.loads(extra.get('printer_name', '""')) at mmu_server.py:437 and tolerates a
#     bare string for 'rfid' (_get_uid_from_extra, :388-401). `mmu_gate` is read with
#     int() so it is stored as a plain numeric string.
#   - a spool embeds its filament, which embeds its vendor:
#     spool['filament']['vendor']['name'] (_get_filament_attr, :404-415).
#   - UIDs are normalised uppercase with ':', '-' and ' ' stripped (_normalise_uid,
#     :376-385), so the store normalises on write too.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import urllib.parse

# Spoolman `extra` field keys. These MUST match mmu_server.py:83-85
# (MMU_NAME_FIELD / MMU_GATE_FIELD / MMU_RFID_FIELD) - note the gate key is
# 'mmu_gate_map', not 'mmu_gate'. test_mmu_moonraker asserts they stay in step so a
# rename cannot silently make the store invisible to HH.
FIELD_PRINTER = 'printer_name'
FIELD_GATE = 'mmu_gate_map'
FIELD_RFID = 'rfid'

SPOOLMAN_VERSION = '0.18.1'      # >= MIN_SM_VER (0,18,1) at mmu_server.py:86

# The two hardcoded donkie.github.io SpoolmanDB URLs (mmu_server.py:95-96) have no
# config flag to disable them, so they MUST be intercepted or tests hit the real
# internet and become non-deterministic.
SPOOLMANDB_MATERIALS = [
    {'material': 'PLA', 'density': 1.24},
    {'material': 'PETG', 'density': 1.27},
    {'material': 'ABS', 'density': 1.04},
]
SPOOLMANDB_BAMBU = {
    'manufacturer': 'Bambu Lab',
    'filaments': [
        {'name': 'PLA Basic', 'material': 'PLA', 'density': 1.24,
         'extruder_temp': 220, 'bed_temp': 55,
         'colors': [{'name': 'Red', 'hex': 'FF0000'}]},
    ],
}


def normalise_uid(uid):
    return (str(uid).strip('"\'').upper()
            .replace(':', '').replace('-', '').replace(' ', ''))


class Response:
    """Mimics Moonraker's HttpClient response object."""

    def __init__(self, status_code=200, payload=None, error=None):
        self.status_code = status_code
        self._payload = payload
        self.error = error

    def has_error(self):
        return self.status_code >= 400

    def json(self):
        return self._payload

    def __repr__(self):
        return 'Response(%d, %r)' % (self.status_code, self._payload)


class InMemorySpoolman:
    """
    A minimal but genuine Spoolman. Every mutation is recorded so a test can assert
    on what HH actually did, not merely that it got a 200.
    """

    def __init__(self, version=SPOOLMAN_VERSION):
        self.version = version
        self.vendors = {}
        self.filaments = {}
        self.spools = {}
        self.fields = {'spool': {}, 'filament': {}, 'vendor': {}}
        self._next_id = {'vendor': 1, 'filament': 1, 'spool': 1}
        # -- assertion surfaces / fault injection --------------------------
        self.requests = []          # [(method, url, body)] every call, in order
        self.fail_next = None       # set to a status code to fail the next request
        self.offline = False        # every request returns 503
        self.created_spools = []
        self.patched_spools = []

    # -- construction helpers used by tests ---------------------------------
    def add_vendor(self, name):
        for vid, v in self.vendors.items():
            if v['name'].lower() == name.lower():
                return vid
        vid = self._take_id('vendor')
        self.vendors[vid] = {'id': vid, 'name': name}
        return vid

    def add_filament(self, name='PLA Basic', material='PLA', vendor='TestVendor',
                     color_hex='FF0000', extruder_temp=220, bed_temp=55,
                     density=1.24, external_id=None):
        vid = self.add_vendor(vendor)
        fid = self._take_id('filament')
        self.filaments[fid] = {
            'id': fid, 'name': name, 'material': material,
            'color_hex': color_hex, 'density': density,
            'settings_extruder_temp': extruder_temp,
            'settings_bed_temp': bed_temp,
            'external_id': external_id,
            'vendor': dict(self.vendors[vid]),
        }
        return fid

    def add_spool(self, filament_id=None, uid=None, printer=None, gate=None,
                  remaining_weight=1000, **filament_kwargs):
        if filament_id is None:
            filament_id = self.add_filament(**filament_kwargs)
        sid = self._take_id('spool')
        extra = {}
        if uid is not None:
            extra[FIELD_RFID] = json.dumps(normalise_uid(uid))
        if printer is not None:
            extra[FIELD_PRINTER] = json.dumps(printer)
        if gate is not None:
            extra[FIELD_GATE] = str(gate)
        self.spools[sid] = {
            'id': sid,
            'filament': dict(self.filaments[filament_id]),
            'extra': extra,
            'remaining_weight': remaining_weight,
            'initial_weight': remaining_weight,
        }
        return sid

    def spool_uid(self, spool_id):
        """The normalised UID registered against a spool, or '' if none."""
        raw = self.spools[spool_id]['extra'].get(FIELD_RFID)
        if not raw:
            return ''
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            pass
        return normalise_uid(raw) if raw else ''

    def spool_gate(self, spool_id):
        return int(self.spools[spool_id]['extra'].get(FIELD_GATE, -1))

    def spool_printer(self, spool_id):
        raw = self.spools[spool_id]['extra'].get(FIELD_PRINTER, '""')
        try:
            return json.loads(raw).strip('"')
        except (ValueError, TypeError):
            return str(raw)

    def find_spool_by_uid(self, uid):
        target = normalise_uid(uid)
        for sid in self.spools:
            if self.spool_uid(sid) == target:
                return sid
        return None

    def _take_id(self, kind):
        v = self._next_id[kind]
        self._next_id[kind] = v + 1
        return v

    # -- the HTTP surface ---------------------------------------------------
    def handle(self, method, url, body=None):
        self.requests.append((method, url, body))
        if self.offline:
            return Response(503, error='offline (test fault injection)')
        if self.fail_next is not None:
            code, self.fail_next = self.fail_next, None
            return Response(code, error='injected failure')

        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        # Strip everything up to and including the /api prefix Moonraker configures
        idx = path.find('/v1/')
        if idx < 0:
            return Response(404)
        parts = path[idx + 1:].strip('/').split('/')      # ['v1', 'spool', '3']

        if parts[:2] == ['v1', 'info']:
            return Response(200, {'version': self.version})
        if len(parts) >= 3 and parts[1] == 'field':
            return self._field(method, parts, body)
        if parts[1] == 'spool':
            return self._spool(method, parts, query, body)
        if parts[1] == 'vendor':
            return self._vendor(method, parts, query, body)
        if parts[1] == 'filament':
            return self._filament(method, parts, query, body)
        return Response(404)

    def _field(self, method, parts, body):
        entity = parts[2]
        if entity not in self.fields:
            return Response(404)
        if method == 'GET':
            return Response(200, [{'key': k, **v}
                                  for k, v in self.fields[entity].items()])
        if method == 'POST' and len(parts) >= 4:
            key = parts[3]
            self.fields[entity][key] = dict(body or {})
            return Response(200, [{'key': k} for k in self.fields[entity]])
        return Response(405)

    def _spool(self, method, parts, query, body):
        if len(parts) >= 3:
            sid = int(parts[2])
            if sid not in self.spools:
                return Response(404)
            if method == 'GET':
                return Response(200, self.spools[sid])
            if method in ('PATCH', 'PUT'):
                self._merge_spool(sid, body or {})
                self.patched_spools.append((sid, body))
                return Response(200, self.spools[sid])
            return Response(405)
        if method == 'GET':
            return Response(200, list(self.spools.values()))
        if method == 'POST':
            return self._create_spool(body or {})
        return Response(405)

    def _merge_spool(self, sid, body):
        spool = self.spools[sid]
        for key, value in body.items():
            if key == 'extra' and isinstance(value, dict):
                # Spoolman merges extra field-by-field
                spool.setdefault('extra', {}).update(value)
            else:
                spool[key] = value

    def _create_spool(self, body):
        filament_id = body.get('filament_id')
        if filament_id not in self.filaments:
            return Response(400, error='unknown filament_id %r' % (filament_id,))
        sid = self._take_id('spool')
        self.spools[sid] = {
            'id': sid,
            'filament': dict(self.filaments[filament_id]),
            'extra': dict(body.get('extra') or {}),
            'remaining_weight': body.get('remaining_weight'),
            'initial_weight': body.get('remaining_weight'),
            'spool_weight': body.get('spool_weight'),
            'lot_nr': body.get('lot_nr'),
        }
        self.created_spools.append(sid)
        return Response(200, self.spools[sid])

    def _vendor(self, method, parts, query, body):
        if method == 'GET':
            name = (query.get('name') or [None])[0]
            vendors = list(self.vendors.values())
            if name:
                vendors = [v for v in vendors if v['name'].lower() == name.lower()]
            return Response(200, vendors)
        if method == 'POST':
            name = (body or {}).get('name', '')
            vid = self.add_vendor(name)
            return Response(200, self.vendors[vid])
        return Response(405)

    def _filament(self, method, parts, query, body):
        if method == 'GET':
            results = list(self.filaments.values())
            ext = (query.get('external_id') or [None])[0]
            if ext:
                results = [f for f in results if f.get('external_id') == ext]
            material = (query.get('material') or [None])[0]
            if material:
                results = [f for f in results
                           if (f.get('material') or '').lower() == material.lower()]
            vendor_name = (query.get('vendor_name') or [None])[0]
            if vendor_name:
                results = [f for f in results
                           if f.get('vendor', {}).get('name', '').lower()
                           == vendor_name.lower()]
            return Response(200, results)
        if method == 'POST':
            data = dict(body or {})
            vendor_id = data.pop('vendor_id', None)
            fid = self._take_id('filament')
            vendor = self.vendors.get(vendor_id, {'id': vendor_id, 'name': ''})
            self.filaments[fid] = {
                'id': fid,
                'name': data.get('name', ''),
                'material': data.get('material', ''),
                'color_hex': data.get('color_hex', ''),
                'density': data.get('density'),
                'settings_extruder_temp': data.get('settings_extruder_temp'),
                'settings_bed_temp': data.get('settings_bed_temp'),
                'external_id': data.get('external_id'),
                'vendor': dict(vendor),
            }
            return Response(200, self.filaments[fid])
        return Response(405)
