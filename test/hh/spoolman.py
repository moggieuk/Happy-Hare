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
#     bare string for the RFID field (_get_uid_list_from_extra). The RFID field may hold
#     more than one UID, comma-separated (a spool can have a tag on each side) -
#     _parse_uid_list splits/normalizes/dedupes it; a single UID is just a one-element
#     case of the same format. `mmu_gate` is read with int() so it is stored as a plain
#     numeric string.
#   - a spool embeds its filament, which embeds its vendor:
#     spool['filament']['vendor']['name'] (_get_filament_attr, :404-415).
#   - UIDs are normalized uppercase with ':', '-' and ' ' stripped (_normalise_uid,
#     :376-385), so the store normalizes on write too.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import re
import urllib.parse

# Spoolman `extra` field keys. These MUST match mmu_server.py:83-85
# (MMU_NAME_FIELD / MMU_GATE_FIELD / MMU_RFID_FIELD) - note the gate key is
# 'mmu_gate_map', not 'mmu_gate'. test_mmu_moonraker asserts they stay in step so a
# rename cannot silently make the store invisible to HH.
FIELD_PRINTER = 'printer_name'
FIELD_GATE = 'mmu_gate_map'
FIELD_RFID = 'rfid_tag'

SPOOLMAN_VERSION = '0.18.1'      # >= MIN_SM_VER (0,18,1) at mmu_server.py:86
TAG_API_VERSION = '0.27.0'       # MIN_SM_TAG_VER - first Spoolman with native tag links

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
    uid = (str(uid).strip('"\'').upper()
           .replace(':', '').replace('-', '').replace(' ', ''))
    return uid if len(uid) % 2 == 0 and re.fullmatch(r'[0-9A-F]+', uid) else ''


def parse_uid_list(raw):
    """Split a (possibly comma-separated) UID value into normalized UIDs. Mirrors
    MmuServer._parse_uid_list (mmu_server.py) so the store's notion of 'the UIDs
    on a spool' matches what HH itself would parse from the same extra value."""
    if not raw:
        return []
    seen = []
    for part in str(raw).split(','):
        part = part.strip()
        if not part:
            continue
        norm = normalise_uid(part)
        if norm and norm not in seen:
            seen.append(norm)
    return seen


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
        # Spoolman's copy of SpoolmanDB (GET /v1/external/filament); None = endpoint missing
        self.external_filaments = None
        # Native tag links (>= 0.27 only): uid -> ('spool'|'filament', id)
        self.tag_links = {}
        self.tag_formats = {}
        self.scans = []             # bodies of POST /v1/tag/scan, in order

    @property
    def has_tag_api(self):
        try:
            return tuple(int(n) for n in self.version.split('.')) >= (0, 27, 0)
        except ValueError:
            return False

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
                  remaining_weight=1000, initial_weight=None, **filament_kwargs):
        if filament_id is None:
            filament_id = self.add_filament(**filament_kwargs)
        sid = self._take_id('spool')
        extra = {}
        if uid is not None:
            # 'uid' may be a single UID, a comma-separated string of several, or
            # an actual list/tuple of UIDs (a spool can have more than one tag).
            uids = uid if isinstance(uid, (list, tuple)) else [uid]
            uids = parse_uid_list(','.join(str(u) for u in uids))
            extra[FIELD_RFID] = json.dumps(','.join(uids))
        if printer is not None:
            extra[FIELD_PRINTER] = json.dumps(printer)
        if gate is not None:
            extra[FIELD_GATE] = str(gate)
        self.spools[sid] = {
            'id': sid,
            'filament': dict(self.filaments[filament_id]),
            'extra': extra,
            'remaining_weight': remaining_weight,
            'initial_weight': remaining_weight if initial_weight is None else initial_weight,
        }
        return sid

    def spool_uids(self, spool_id):
        """The list of normalized UIDs registered against a spool ([] if none)."""
        raw = self.spools[spool_id]['extra'].get(FIELD_RFID)
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            pass
        return parse_uid_list(raw)

    def link_tag(self, uid, spool_id=None, filament_id=None, fmt=None):
        """Test helper: natively link a tag (as the Spoolman UI would)."""
        uid = normalise_uid(uid)
        self.tag_links[uid] = ('spool', spool_id) if spool_id is not None else ('filament', filament_id)
        self.tag_formats[uid] = fmt
        return uid

    def spool_native_uids(self, spool_id):
        return [u for u, holder in self.tag_links.items() if holder == ('spool', spool_id)]

    def _tags_for(self, kind, ident):
        return [{'uid': u, 'format': self.tag_formats.get(u), 'added': '2026-01-01T00:00:00Z'}
                for u, holder in self.tag_links.items() if holder == (kind, ident)]

    def _render_spool(self, sid):
        spool = self.spools[sid]
        if not self.has_tag_api:
            return spool
        out = dict(spool)
        out['tags'] = self._tags_for('spool', sid)
        return out

    def spool_uid(self, spool_id):
        """Comma-joined display string of the UID(s) registered against a spool,
        or '' if none. Use spool_uids() to test against the individual UIDs."""
        return ','.join(self.spool_uids(spool_id))

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
        holder = self.tag_links.get(target)
        if holder and holder[0] == 'spool':
            return holder[1]
        for sid in self.spools:
            if target in self.spool_uids(sid):
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
        if self.has_tag_api and parts[1:3] == ['tag', 'scan'] and method == 'POST':
            return self._tag_scan(body or {})
        if self.has_tag_api and len(parts) >= 4 and parts[1] in ('spool', 'filament') and parts[3] == 'tag':
            return self._tag(method, parts, body)
        if parts[1] == 'spool':
            return self._spool(method, parts, query, body)
        if parts[1] == 'vendor':
            return self._vendor(method, parts, query, body)
        if parts[1] == 'filament':
            return self._filament(method, parts, query, body)
        if parts[1:3] == ['external', 'filament'] and method == 'GET':
            if self.external_filaments is None:
                return Response(404)
            return Response(200, [dict(f) for f in self.external_filaments])
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
                return Response(200, self._render_spool(sid))
            if method in ('PATCH', 'PUT'):
                bad = self._undefined_extra(body)
                if bad:
                    return Response(400, {'message': 'Unknown extra field %s' % bad}, error='unknown extra field')
                self._merge_spool(sid, body or {})
                self.patched_spools.append((sid, body))
                return Response(200, self._render_spool(sid))
            return Response(405)
        if method == 'GET':
            return Response(200, [self._render_spool(sid) for sid in self.spools])
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

    def _undefined_extra(self, body):
        """Spoolman rejects writes to an extra field that hasn't been defined."""
        for key in ((body or {}).get('extra') or {}):
            if key not in self.fields['spool']:
                return key
        return None

    def _create_spool(self, body):
        bad = self._undefined_extra(body)
        if bad:
            return Response(400, {'message': 'Unknown extra field %s' % bad}, error='unknown extra field')
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

    def _tag_scan(self, body):
        self.scans.append(dict(body))
        uid = normalise_uid(body.get('uid', ''))
        if not uid:
            return Response(400, {'message': 'invalid uid'})
        kind, ident = self.tag_links.get(uid, (None, None))
        return Response(200, {
            'uid': uid, 'reader_id': body.get('reader_id'), 'name': body.get('name'),
            'format': body.get('format'), 'payload_b64': None,
            'matched_spool_id': ident if kind == 'spool' else None,
            'spool': self._render_spool(ident) if kind == 'spool' else None,
            'matched_filament_id': ident if kind == 'filament' else None,
            'filament': dict(self.filaments[ident]) if kind == 'filament' else None,
            'decoded': None, 'created': False,
        })

    def _tag(self, method, parts, body):
        """POST /v1/{spool|filament}/{id}/tag and DELETE .../tag/{uid} (Spoolman >= 0.27)."""
        kind, ident = parts[1], int(parts[2])
        store = self.spools if kind == 'spool' else self.filaments
        if ident not in store:
            return Response(404, {'message': '%s %d not found' % (kind, ident)})
        if method == 'POST' and len(parts) == 4:
            uid = normalise_uid((body or {}).get('uid', ''))
            if not uid:
                return Response(400, {'message': 'invalid uid'})
            holder = self.tag_links.get(uid)
            if holder is not None and holder != (kind, ident):
                return Response(409, {'message': 'tag %s already linked' % uid,
                                      'spool_id': holder[1] if holder[0] == 'spool' else None,
                                      'filament_id': holder[1] if holder[0] == 'filament' else None},
                                error='conflict')
            self.tag_links[uid] = (kind, ident)
            if (body or {}).get('format') or uid not in self.tag_formats:
                self.tag_formats[uid] = (body or {}).get('format')
            return Response(201, {'uid': uid, 'format': self.tag_formats[uid], 'added': '2026-01-01T00:00:00Z'})
        if method == 'DELETE' and len(parts) == 5:
            uid = normalise_uid(parts[4])
            if self.tag_links.get(uid) != (kind, ident):
                return Response(404, {'message': 'tag not linked'})
            del self.tag_links[uid]
            self.tag_formats.pop(uid, None)
            return Response(204)
        return Response(405)

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
