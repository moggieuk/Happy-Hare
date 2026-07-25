# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Original slicer parsing
# Copyright (C) 2023  Kieran Eglin <@kierantheman (discord)>, <kieran.eglin@gmail.com>
#
# RFID support
# Copyright (C) 2026  WoodWorker
#
# RFID Spool auto-create
# Copyright (C) 2026 lameandboard
#
# Happy Hare's Moonraker component. It runs inside Moonraker (not Klipper) and
# performs two largely independent jobs:
# 
# 1. Spoolman bridge (the MmuServer class)
#    -------------------------------------
#    Provides the asynchronous glue between Happy Hare (running in Klipper) and a
#    Spoolman filament database. Happy Hare invokes the methods here as Moonraker
#    remote methods (via webhooks.call_remote_method), they talk to Spoolman over
#    its REST API using Moonraker's async HttpClient, and results are handed back
#    to Klipper by running MMU_* gcode commands. Nothing here blocks Klipper's
#    reactor; every network call is awaited on Moonraker's event loop.
# 
#    Spool metadata that Happy Hare owns is stored in Spoolman "extra" fields,
#    which this module auto-creates on the Spoolman server if missing:
#      - printer_name : which printer a spool is assigned to
#      - mmu_gate_map : which gate on that printer the spool sits in
#      - rfid         : the NFC/RFID tag UID registered against the spool
#    A cache of spool -> (printer, gate, attributes) is maintained locally for
#    efficiency, along with a reverse UID -> spool_id map for fast tag lookups.
# 
#    Capabilities include: pushing/pulling the gate<->spool map, setting/clearing
#    spool-gate assignments, reporting filament attributes and spool info,
#    pushing lane data for slicer integration, and resolving a scanned NFC/RFID
#    tag UID to a spool (get_spool_by_uid) or registering one (set_spool_uid).
# 
# 2. GCode metadata pre-processor (the code below the MmuServer class)
#    ----------------------------------------------------------------
#    When invoked as a standalone script it replaces/extends Moonraker's
#    file_manager metadata processor, scanning sliced gcode to substitute Happy
#    Hare placeholders (referenced tools, colors, temperatures, materials, purge
#    volumes, filament names) and optionally injecting MMU_CHANGE_TOOL commands
#    with next-position hints for supported slicers.
#
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# See <https://www.gnu.org/licenses/>.
#
from __future__ import annotations
import json
import logging, os, sys, re, time, asyncio
import runpy, argparse, shutil, traceback, tempfile, filecmp
import urllib.parse
from typing import (
    TYPE_CHECKING,
    List,
    Dict,
    Any,
    Optional,
    Union,
    cast
)

if TYPE_CHECKING:
    from .spoolman import SpoolManager, DB_NAMESPACE, ACTIVE_SPOOL_KEY
    from ..common import WebRequest
    from ..common import RequestType
    from ..confighelper import ConfigHelper
    from .http_client import HttpClient, HttpResponse
    from .database import MoonrakerDatabase
    from .announcements import Announcements
    from .klippy_apis import KlippyAPI as APIComp
    from .history import History
    from tornado.websocket import WebSocketClientConnection

MMU_NAME_FIELD   = 'printer_name'
MMU_GATE_FIELD   = 'mmu_gate_map'
MMU_RFID_FIELD   = 'rfid'          # NFC/RFID tag UID registered against a spool
MIN_SM_VER       = (0, 18, 1)

NFC_UID_MISS_TTL = 10.0            # Seconds to remember a UID that isn't in Spoolman (avoids re-querying on every scan)

# ─── RFID spool auto-create: SpoolmanDB reference data + offline fallbacks ─────
# Reference filament data fetched (once per process) from SpoolmanDB, the
# community database maintained by Spoolman's author. Used only to enrich
# auto-created records (density, temps, canonical vendor/colour names); every
# field degrades gracefully to the fallbacks below when the host is offline.
SPOOLMANDB_MATERIALS_URL = "https://donkie.github.io/SpoolmanDB/materials.json"
SPOOLMANDB_BAMBU_URL     = "https://donkie.github.io/SpoolmanDB/filaments/bambulab.json"

DENSITY_FALLBACK = {               # g/cm³, used when SpoolmanDB is unreachable
    "pla": 1.24, "pla+": 1.24, "abs": 1.04, "petg": 1.27, "nylon": 1.52,
    "pa": 1.52, "tpu": 1.21, "flexible": 1.21, "asa": 1.05, "pc": 1.30,
    "hips": 1.03, "pva": 1.23, "tpe": 1.21, "peek": 1.32, "pei": 1.27, "pom": 1.41,
}
DENSITY_DEFAULT        = 1.24      # PLA - safe default for unknown materials
DEFAULT_SPOOL_WEIGHT_G = 1000      # Net filament weight when the tag doesn't supply one

TAG_FORMAT_BRANDS = {              # Brand deduced from parser tag_format when the tag has none
    "elegoo": "ELEGOO", "anycubic_ace": "Anycubic", "creality_cfs": "Creality",
    "qidi": "QIDI", "opentag3d": "Generic", "openspool": "Generic",
    "openprinttag": "Generic", "simplyprint_url": "Generic", "generic_ndef_json": "Generic",
}

DB_NAMESPACE     = "moonraker"
ACTIVE_SPOOL_KEY = "spoolman.spool_id"


class MmuServer:

    def __init__(self, config: ConfigHelper):
        self.config = config
        self.server = config.get_server()
        self.printer_info = self.server.get_host_info()
        self.spoolman = None
        if config.has_section("spoolman"): # Avoid exception if spoolman not configured
            self.spoolman: SpoolManager = self.server.load_component(config, "spoolman", None)
        self.spoolman: SpoolManager = self.server.lookup_component("spoolman", None)
        self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
        self.http_client: HttpClient = self.server.lookup_component("http_client")
        self.database: MoonrakerDatabase = self.server.lookup_component("database")

        # Full cache of spool_ids and location + key attributes (printer, gate, attr_dict))
        # Example: {2: ('BigRed', 0, {"material": "pla", "color": "ff56e0"}), 3: ('BigRed', 3, {"material": "abs"}), ...
        self.spool_location = {}

        # Reverse map of normalised NFC/RFID tag UID -> spool_id, built alongside
        # spool_location so a tag scan can be resolved without a per-lookup fetch
        self.uid_to_spool_id = {}

        # Negative cache of normalised UID -> expiry (monotonic) for tags recently
        # confirmed absent from Spoolman, so frequent scans of an unknown tag
        # don't trigger a full spool fetch every time
        self.uid_miss_cache = {}

        # Process-lifetime caches of SpoolmanDB reference data, fetched lazily on
        # the first tag auto-create. None = not yet fetched; {}/[] = fetch failed
        # (kept so a failed fetch isn't retried on every scan).
        self._spoolmandb_materials = None   # {material_lower: density}
        self._spoolmandb_bambu     = None   # [filament dict, ...] from bambulab.json
        self._spoolmandb_bambu_mfr = None   # top-level manufacturer name (e.g. "Bambu Lab")

        self.nb_gates = None             # Set during initialization to the size of the MMU or 1 if standalone
        self.cache_lock = asyncio.Lock() # Lock to serialize a async calls for Happy Hare

        # Spoolman filament info retrieval functionality and update reporting
        if self.spoolman:
            self.server.register_remote_method("spoolman_refresh", self.refresh_cache)
            self.server.register_remote_method("spoolman_get_filaments", self.get_filaments) # "get" mode
            self.server.register_remote_method("spoolman_push_gate_map", self.push_gate_map) # "push" mode
            self.server.register_remote_method("spoolman_pull_gate_map", self.pull_gate_map) # "pull" mode
            self.server.register_remote_method("spoolman_clear_spools_for_printer", self.clear_spools_for_printer)
            self.server.register_remote_method("spoolman_set_spool_gate", self.set_spool_gate)
            self.server.register_remote_method("spoolman_unset_spool_gate", self.unset_spool_gate)
            self.server.register_remote_method("spoolman_get_spool_info", self.display_spool_info)
            self.server.register_remote_method("spoolman_display_spool_location", self.display_spool_location)

            # NFC/RFID tag support (see mmu_nfc_manager on the Klipper side)
            self.server.register_remote_method("spoolman_get_spool_by_uid", self.get_spool_by_uid)     # tag scan -> pending spool_id
            self.server.register_remote_method("spoolman_set_spool_uid", self.set_spool_uid)           # register a tag UID onto a spool

        # Moonraker lane data push for slicer integration
        self.server.register_remote_method("moonraker_push_lane_data", self.push_lane_data)
        self.server.register_remote_method("moonraker_cleanup_lane_data", self.cleanup_lane_data)

        # Replace file_manager/metadata with this file
        self.setup_placeholder_processor(config)

        # Options
        self.update_location = self.config.getboolean("update_spoolman_location", True)


    async def _get_spoolman_version(self) -> tuple[int, int, int] | None:
        response = await self.http_client.get(url=f'{self.spoolman.spoolman_url}/v1/info')
        if response.status_code == 404:
            logging.info(f"'{self.spoolman.spoolman_url}/v1/info' not found")
            return None
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.error(f"Attempt to get info from spoolman failed: {err_msg}")
            return None
        else:
            logging.info("info field in spoolman retrieved")
            return tuple([int(n) for n in response.json()['version'].split('.')])


    async def component_init(self) -> None:
        if self.spoolman is None:
            logging.warning("Spoolman not available. Happy Hare remote methods not available")
            return

        # Get current printer hostname
        self.printer_hostname = self.printer_info["hostname"]
        self.spoolman_has_extras = False
        asyncio.create_task(self._init_spoolman(retry=3)) # Spoolman may start up after us so retry a few times


    async def _init_spoolman(self, retry=1) -> bool:
        '''
        Return True if connected, False if not. Set's self.spoolman_has_extras is
        '''
        async with self.cache_lock:
            for _ in range(retry):
                self.spoolman_version = await self._get_spoolman_version()
                if self.spoolman_version:
                    logging.info("Contacted Spoolman")
                    break
                logging.warning(f"Spoolman not available. {'Retrying in 2 seconds...' if retry > 1 else ''}")
                await asyncio.sleep(2)

            extras = False
            if self.spoolman_version and self.spoolman_version >= MIN_SM_VER:
                # Make sure db has required extra fields
                extras = True
                fields = await self._get_extra_fields("spool")
                if MMU_NAME_FIELD not in fields:
                    extras = extras and await self._add_extra_field("spool", field_name="Printer Name", field_key=MMU_NAME_FIELD, field_type="text", default_value="")
                if MMU_GATE_FIELD not in fields:
                    extras = extras and await self._add_extra_field("spool", field_name="MMU Gate", field_key=MMU_GATE_FIELD, field_type="integer", default_value=-1)
                if MMU_RFID_FIELD not in fields:
                    extras = extras and await self._add_extra_field("spool", field_name="RFID", field_key=MMU_RFID_FIELD, field_type="text", default_value="")

                # Create cache of spool location from Spoolman db for effeciency
                if extras:
                    await self._build_spool_location_cache(silent=True)
                self.spoolman_has_extras = extras

            elif self.spoolman_version:
                logging.error(f"Could not initialize Spoolman db for Happy Hare. Spoolman db version too old (found {self.spoolman_version} < {MIN_SM_VER})")
            else:
                logging.error("Could not connect to Spoolman db. Perhaps it is not initialized yet? Will try again on next request")
                return False
        return True


    async def _check_init_spoolman(self, silent=False) -> bool:
        if not self.spoolman_has_extras:
            db_awake = await self._init_spoolman()
            if not silent:
                if not db_awake:
                    await self._log_n_send("Couldn't connect to Spoolman. Maybe not configured/running yet (check moonraker.log).\nUse MMU_SPOOLMAN REFRESH=1 to force retry")
                elif not self.spoolman_has_extras:
                    await self._log_n_send("Incompatible Spoolman version for this feature. Check moonraker.log")
        return self.spoolman_has_extras


    # !TODO: implement mainsail/fluidd gui prompts?
    async def _log_n_send(self, msg, error=False, prompt=False, silent=False):
        '''
        logs and sends msg to the klipper console
        '''
        if error:
            logging.error(msg)
        else:
            logging.info(msg)
        if not silent:
            if self._mmu_backend_enabled():
                error_flag = "ERROR=1" if error else ""
                msg = msg.replace("\n", "\\n") # Get through klipper filtering
                await self.klippy_apis.run_gcode(f"MMU_LOG MSG='{msg}' {error_flag}")
            else:
                for msg in msg.split("\n"):
                    await self.klippy_apis.run_gcode(f"M118 {msg}")
                if error :
                    await self.klippy_apis.pause_print()


    async def _init_mmu_backend(self):
        '''
        Initialize MMU backend and check if enabled

        returns:
            @return: True if initialized, False otherwise
        '''
        self.mmu_backend_present = 'mmu' in await self.klippy_apis.get_object_list()
        if self.mmu_backend_present:
            self.mmu_backend_config = await self.klippy_apis.query_objects({"mmu": None})
            self.mmu_enabled = self.mmu_backend_config.get('mmu', {}).get('enabled', False)
        else:
            self.mmu_enabled = False
        logging.info(f"MMU backend present: {self.mmu_backend_present}")
        logging.info(f"MMU backend enabled: {self.mmu_enabled}")
        return True


    def _mmu_backend_enabled(self):
        if not hasattr(self, 'mmu_backend_present'):
            return False
        return self.mmu_backend_present and self.mmu_enabled


    async def _initialize_mmu(self):
        '''
        Initialize mmu gate map if not already done

        returns:
            @return: True once initialized
        '''
        if not hasattr(self, 'mmu_backend_present'):
            await self._init_mmu_backend()
            if self._mmu_backend_enabled():
                if self.config.has_option("num_gates"):
                    logging.warning("The 'num_gates' option in the moonraker [mmu_server] section is ignored when an MMU backend is present and enabled.")
                self.nb_gates = self.mmu_backend_config.get('mmu', {}).get('num_gates', 0)
            else:
                self.nb_gates = self.config.getint("num_gates", 1) # for standalone usage (no mmu backend considering standard or (custom defined) printer setup)
            logging.info(f"MMU num_gates: {self.nb_gates}")
        return True


    async def _get_extra_fields(self, entity_type) -> bool:
        '''
        Helper to gets all extra fields for the entity type
        '''
        response = await self.http_client.get(url=f'{self.spoolman.spoolman_url}/v1/field/{entity_type}')
        if response.status_code == 404:
            logging.info(f"'{self.spoolman.spoolman_url}/v1/field/{entity_type}' not found")
            return False
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.error(f"Attempt to get extra fields failed: {err_msg}")
            return False
        else:
            logging.info(f"Extra fields for {entity_type} found")
            return [r['key'] for r in response.json()]


    async def _add_extra_field(self, entity_type, field_key, field_name, field_type, default_value) -> bool:
        '''
        Helper to add a new field to the extra field of the Spoolman db
        '''
        #default_value = json.dumps(default_value) if field_type == 'text' else default_value
        response = await self.http_client.post(
            url=f'{self.spoolman.spoolman_url}/v1/field/{entity_type}/{field_key}',
            body={"name" : field_name, "field_type" : field_type, "default_value" : json.dumps(default_value)}
        )
        if response.status_code == 404:
            logging.info(f"'{self.spoolman.spoolman_url}/v1/field/spool/{field_key}' not found")
            return False
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.error(f"Attempt add field {field_name} failed: {err_msg}")
            return False
        logging.info(f"Field {field_name} added to Spoolman db for entity type {entity_type}")
        logging.info("  -fields: %s", response.json())
        return True


    async def _fetch_spool_info(self, spool_id) -> dict | None:
        '''
        Retrieve an individual spool_info record
        '''
        response = await self.spoolman.http_client.request(
            method="GET",
            url=f'{self.spoolman.spoolman_url}/v1/spool/{spool_id}',
            body=None)
        if response.status_code == 404:
            logging.error(f"'{self.spoolman.spoolman_url}/v1/spool/{spool_id}' not found")
            return None
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.info(f"Attempt to fetch spool info failed: {err_msg}")
            return None
        spool_info = response.json()
        return spool_info


    @staticmethod
    def _normalise_uid(uid_str) -> str:
        '''
        Normalise an NFC/RFID tag UID for comparison: strip surrounding quotes
        and separators and uppercase, so e.g. '"04:a2:3b"' == "04A23B"
        '''
        return (str(uid_str).strip('"\'')
                            .upper()
                            .replace(':', '')
                            .replace('-', '')
                            .replace(' ', ''))


    def _get_uid_from_extra(self, extra) -> str:
        '''
        Read and normalise the tag UID stored in a spool's extra fields.
        Values are JSON-encoded on the wire (like the other extra fields) but
        tolerate a bare string in case it was set manually. Returns '' if unset.
        '''
        raw = (extra or {}).get(MMU_RFID_FIELD)
        if not raw:
            return ''
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            pass # Fall back to the raw value if it isn't valid JSON
        return self._normalise_uid(raw) if raw else ''


    def _get_filament_attr(self, spool_info) -> dict:
        spool_id = spool_info["id"]
        filament = spool_info["filament"]
        name = filament.get('name', '')
        material = filament.get('material', '')
        color_hex = filament.get('color_hex', '').strip('#')[:8].lower() # Remove problematic First # character if present
        temp = filament.get('settings_extruder_temp', '')
        bed_temp = filament.get('settings_bed_temp', '')
        vendor = filament.get('vendor', {}).get('name', '')
        filament_id = filament.get('id', '')
        rfid = self._get_uid_from_extra(spool_info.get('extra'))
        return {'spool_id': spool_id, 'material': material, 'color': color_hex, 'name': name, 'temp': temp, 'bed_temp': bed_temp, 'vendor': vendor, 'filament_id': filament_id, 'rfid': rfid}


    async def _build_spool_location_cache(self, fix=False, silent=False) -> bool:
        '''
        Helper to get all spools and gates assigned to printers from Spoolman db and cache them
        '''
        logging.info("Building spool location cache from Spoolman db")
        try:
            # Build into local dicts and only commit on success so a failed or
            # timed-out fetch leaves the existing cache intact
            new_spool_location = {}
            new_uid_to_spool_id = {}
            # Fetch all spools
            errors = ""
            assignments = {}
            sids_to_fix = []
            response = await self.http_client.get(url=f'{self.spoolman.spoolman_url}/v1/spool')
            if response.has_error():
                raise RuntimeError(self.spoolman._get_response_error(response))
            for spool_info in response.json():
                spool_id = spool_info['id']
                printer_name = json.loads(spool_info['extra'].get(MMU_NAME_FIELD, "\"\"")).strip('"')
                mmu_gate = int(spool_info['extra'].get(MMU_GATE_FIELD, -1))
                filament_attr = self._get_filament_attr(spool_info)
                new_spool_location[spool_id] = (printer_name, mmu_gate, filament_attr)

                # Maintain reverse UID -> spool_id map for fast tag resolution
                uid = filament_attr.get('rfid')
                if uid:
                    new_uid_to_spool_id[uid] = spool_id

                if printer_name and mmu_gate >= 0:
                    if printer_name not in assignments:
                        assignments[printer_name] = {}
                    if mmu_gate not in assignments[printer_name]:
                        assignments[printer_name][mmu_gate] = []
                    assignments[printer_name][mmu_gate].append(spool_id)

                # Highlight errors
                if printer_name and mmu_gate < 0:
                    errors += f"\n  - Spool {spool_id} has printer {printer_name} but no mmu_gate assigned"
                    sids_to_fix.append(spool_id)
                if mmu_gate >= 0 and not printer_name:
                    errors += f"\n  - Spool {spool_id} has mmu_gate {mmu_gate} but no printer assigned"
                    sids_to_fix.append(spool_id)

            for p, gates in assignments.items():
                for g, spool_list in gates.items():
                    if len(spool_list) > 1:
                        errors += f"\n  - Printer {p} @ gate {g} has multiple spool ids: {spool_list}"
                        sids_to_fix.extend(spool_list[1:])
        except Exception as e:
            await self._log_n_send(f"Failed to retrieve spools from spoolman: {str(e)}", error=True, silent=silent)
            return False

        # Fetch and parse succeeded - commit the freshly built cache atomically
        self.spool_location = new_spool_location
        self.uid_to_spool_id = new_uid_to_spool_id

        if errors:
            if fix:
                errors += "\nWill attempt to fix..."
            await self._log_n_send(f"Warning - Inconsistencies found in Spoolman db:{errors}", silent=silent)

        if fix:
            tasks = {sid: self._unset_spool_gate(sid, silent=silent) for sid in sids_to_fix}
            results = await asyncio.gather(*tasks.values())

            # Log results and update cache
            for sid, result in zip(tasks.keys(), results):
                if result:
                    old_printer, old_gate, filament_attr = self.spool_location.get(sid, ('', -1, {}))
                    self.spool_location[sid] = ('', -1, filament_attr)
                    await self._log_n_send(f"Spool {sid} unassigned from printer {old_printer} and gate {old_gate}", silent=silent)
        return True


    # Function to find the first spool_id with a matching 'printer/gate', just 'gate' or just 'printer'
    def _find_first_spool_id(self, target_printer, target_gate):
        return next((spoolid
                for spoolid, (printer, gate, _) in self.spool_location.items()
                if (target_printer is None or printer == target_printer) and gate == target_gate
            ), -1)


    # Function to find all the spool_ids with a matching 'printer/gate', just 'gate' or just 'printer'
    def _find_all_spool_ids(self, target_printer, target_gate):
        return [
            spoolid
            for spoolid, (printer, gate, _) in self.spool_location.items()
            if (target_printer is None or printer == target_printer) and (target_gate is None or gate == target_gate)
        ]


    async def _set_spool_gate(self, spool_id, printer, gate, silent=False) -> bool:
        if not await self._check_init_spoolman(): return False

        # Use the PATCH method on the spoolman api
        if not silent:
            logging.info(f"Setting spool {spool_id} for printer {printer} @ gate {gate}")
        data = {'extra': {MMU_NAME_FIELD: json.dumps(f"{printer}"), MMU_GATE_FIELD: json.dumps(gate)}}
        if self.update_location:
            data['location'] = f"{printer} @ MMU Gate:{gate}"
        response = await self.http_client.request(
            method="PATCH",
            url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
            body=data
        )
        if response.status_code == 404:
            logging.error(f"'{self.spoolman.spoolman_url}/v1/spool/{spool_id}' not found")
            await self._log_n_send(f"SpoolId {spool_id} not found", error=True, silent=False)
            return False
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.error(f"Attempt to set spool failed: {err_msg}")
            await self._log_n_send(f"Failed to set spool {spool_id} for printer {printer}. Look at moonraker.log for more details.", error=True, silent=False)
            return False
        return True


    async def _unset_spool_gate(self, spool_id, silent=False) -> bool:
        if not await self._check_init_spoolman(): return False

        # Use the PATCH method on the spoolman api
        if not silent:
            logging.info(f"Unsetting gate map on spool id {spool_id}")
        data = {'extra': {MMU_NAME_FIELD: json.dumps(""), MMU_GATE_FIELD: json.dumps(-1)}}
        if self.update_location:
            data['location'] = ""
        response = await self.http_client.request(
            method="PATCH",
            url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
            body=data
        )
        if response.status_code == 404:
            logging.error(f"'{self.spoolman.spoolman_url}/v1/spool/{spool_id}' not found")
            await self._log_n_send(f"SpoolId {spool_id} not found", error=True, silent=False)
            return False
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.error(f"Attempt to unset spool failed: {err_msg}")
            await self._log_n_send(f"Failed to unset spool {spool_id}. Look at moonraker.log for more details", error=True, silent=False)
            return False
        return True


    async def _send_gate_map_update(self, gate_ids, replace=False, silent=False) -> bool:
        '''
        Retrieve filament attributes for list of (gate, spool_id) tuples
        Pass back to Happy Hare.

        If no mmu backend has been detected, ignore the request
        '''
        if self._mmu_backend_enabled():
            gate_dict = {
                gate: (
                    {'spool_id': -1} if spool_id < 0 else
                    self.spool_location.get(spool_id)[2].copy()
                    if self.spool_location.get(spool_id)
                    else logging.error(f"Spool id {spool_id} requested but not found in spoolman")
                )
                for gate, spool_id in gate_ids
            }
            try:
                await self.klippy_apis.run_gcode(f"MMU_GATE_MAP MAP=\"{gate_dict}\" {'REPLACE=1' if replace else ''} FROM_SPOOLMAN=1 QUIET=1")
            except Exception as e:
                await self._log_n_send(f"Exception running MMU_GATE_MAP gcode: {str(e)}", error=True, silent=silent)
                return False
        return True


    async def refresh_cache(self, fix=False, silent=False) -> bool:
        '''
        Rebuilds the local cache of essential spool information
        '''
        if not await self._check_init_spoolman(): return False
        async with self.cache_lock:
            await self._initialize_mmu()
            return await self._build_spool_location_cache(fix=fix, silent=silent)


    async def get_filaments(self, gate_ids, silent=False) -> bool:
        '''
        Retrieve filament attributes for list of (gate, spool_id) tuples
        Pass back to Happy Hare. Does not require extended Spoolman db
        '''
        async with self.cache_lock:
            return await self._send_gate_map_update(gate_ids, silent=silent)


    async def push_gate_map(self, gate_ids=None, silent=False) -> bool:
        '''
        Store the gate map for the printer for a list of (gate, spool_id) tuples.
        This attempts to reduce the number of necessary tasks and then run them in parallel
        Then updates Happy Hare with filament attributes
        '''
        if not await self._check_init_spoolman(): return False
        async with self.cache_lock:
            await self._initialize_mmu()

            if not gate_ids:
                logging.error("Gate spool id mapping not provided or empty")
                return False

            # Make sure we cleanup all the gate's old spool_id association
            updates = {}
            for gate, spool_id in gate_ids:
                old_sids = self._find_all_spool_ids(self.printer_hostname, gate)
                for old_sid in old_sids:
                    updates[old_sid] = -1

            # Now layer in the supplied gate map
            for gate, spool_id in gate_ids:
                if spool_id > 0:
                    updates[spool_id] = gate

            # If setting a full gate map, include updates for "dirty" spool id's
            # that are not otherwise going to be overwritten
            if len(gate_ids) == self.nb_gates:
                for spool_id, (p_name, gate, _) in self.spool_location.items():
                    if p_name == self.printer_hostname and not any(s == spool_id for _, s in gate_ids):
                        updates[spool_id] = -1

            # Create minimal set of async tasks to update Spoolman db and run them in parallel
            tasks = {
                sid: (
                    self._unset_spool_gate(sid, silent=silent),
                    None
                ) if updates[sid] < 0 else (
                    self._set_spool_gate(sid, self.printer_hostname, updates[sid], silent=silent),
                    updates[sid]
                )
                for sid in updates.keys()
            }
            results = await asyncio.gather(*[task for task,_ in tasks.values()])

            # Log results and update cache
            for sid, result in zip(tasks.keys(), results):
                if result:
                    old_printer, old_gate, filament_attr = self.spool_location.get(sid, ('', -1, {}))
                    gate = tasks[sid][1]
                    if updates[sid] < 0: # 'unset' case
                        self.spool_location[sid] = ('', -1, filament_attr)
                        self.server.send_event("spoolman:unset_spool_gate", {"spool_id": sid, "printer": old_printer, "gate": old_gate})
                        await self._log_n_send(f"Spool {sid} unassigned from printer {old_printer} and gate {old_gate} in Spoolman db", silent=silent)
                    else: # 'set' case
                        self.spool_location[sid] = (self.printer_hostname, gate, filament_attr)
                        self.server.send_event("spoolman:set_spool_gate", {"spool_id": sid, "printer": self.printer_hostname, "gate": gate})
                        await self._log_n_send(f"Spool {sid} assigned to printer {self.printer_hostname} @ gate {gate} in Spoolman db", silent=silent)

            # Send update of filament attributes back to Happy Hare
            return await self._send_gate_map_update(gate_ids, silent=silent)


    async def pull_gate_map(self, silent=False) -> bool:
        '''
        Get all spools assigned to the current printer from Spoolman db and map them to gates
        Pass back to Happy Hare
        '''
        if not await self._check_init_spoolman(): return False
        async with self.cache_lock:
            await self._initialize_mmu()

            gate_ids = [(gate, self._find_first_spool_id(self.printer_hostname, gate)) for gate in range(self.nb_gates)]
            return await self._send_gate_map_update(gate_ids, replace=True, silent=silent)


    async def clear_spools_for_printer(self, printer=None, sync=False, silent=False) -> bool:
        '''
        Clears all gates for the printer
        '''
        if not await self._check_init_spoolman(): return False
        async with self.cache_lock:
            await self._initialize_mmu()

            printer_name = printer or self.printer_hostname
            if not silent:
                logging.info(f"Clearing gate map for printer: {printer_name}")

            # Create minimal set of async tasks to update Spoolman db and run them in parallel
            old_sids = self._find_all_spool_ids(printer_name, None)
            tasks = {sid: self._unset_spool_gate(sid, silent=silent) for sid in old_sids}
            results = await asyncio.gather(*tasks.values())

            # Log results and update cache
            updated_gate_ids = {}
            for sid, result in zip(tasks.keys(), results):
                if result:
                    old_printer, old_gate, filament_attr = self.spool_location.get(sid, ('', -1, {}))
                    if old_printer == self.printer_hostname and 0 <= old_gate < self.nb_gates and not updated_gate_ids.get(old_gate):
                        updated_gate_ids[old_gate] = -1
                    self.spool_location[sid] = ('', -1, filament_attr)
                    self.server.send_event("spoolman:unset_spool_gate", {"spool_id": sid, "printer": old_printer, "gate": old_gate})
                    await self._log_n_send(f"Spool {sid} unassigned from printer {old_printer} and gate {old_gate}", silent=silent)

            self.server.send_event("spoolman:clear_spool_gates", {"printer": printer_name})
            if sync and updated_gate_ids:
                gate_ids = [(gate, spool_id) for gate, spool_id in updated_gate_ids.items()]
                return await self._send_gate_map_update(gate_ids, replace=True, silent=silent)
            return True


    async def set_spool_gate(self, spool_id=None, gate=None, sync=False, silent=False) -> bool:
        '''
        Associate spool_id with the printer and gate and clear up any old associations

        parameters:
            @param spool_id: id of the spool to set
            @param gate: optional gate number to set the spool into. If not provided (and not an mmu), the spool will be set into gate 0.
        returns:
            @return: True if successful, False otherwise
        Removes the printer + gate allocation in Spoolman db for gate (if supplied)
        '''
        if not await self._check_init_spoolman(): return False
        async with self.cache_lock:
            await self._initialize_mmu()

            # Sanity checking...
            if gate is not None and gate < 0:
                await self._log_n_send("Trying to set spool {spool_id} for printer {self.printer_hostname} but gate {gate} is invalid.", error=True, silent=silent)
                return False
            if gate is not None and gate > self.nb_gates - 1:
                await self._log_n_send(f"Trying to set spool {spool_id} for printer {self.printer_hostname} @ gate {gate} but only {self.nb_gates} gates are available. Please check the spoolman or moonraker [spoolman] setup.", error=True, silent=silent)
                return False
            if gate is None:
                if self.nb_gates:
                    await self._log_n_send(f"Trying to set spool {spool_id} for printer {self.printer_hostname} but printer has an MMU with {self.nb_gates} gates. Please check the spoolman or moonraker [spoolman] setup.", error=True, silent=silent)
                    return False
                gate = 0

            if not silent:
                logging.info(f"Attempting to set gate {gate} for printer {self.printer_hostname}")

            # Create minimal set of async tasks to update Spoolman db and run them in parallel
            old_sids = self._find_all_spool_ids(self.printer_hostname, gate)
            tasks = {
                sid: (self._unset_spool_gate(sid, silent=silent), None)
                for sid in old_sids if sid != spool_id
            }
            tasks[spool_id] = (self._set_spool_gate(spool_id, self.printer_hostname, gate, silent=silent), gate)
            results = await asyncio.gather(*[task for task,_ in tasks.values()])

            # Log results and update cache
            updated_gate_ids = {}
            for sid, result in zip(tasks.keys(), results):
                if result:
                    old_printer, old_gate, filament_attr = self.spool_location.get(sid, ('', -1, {}))
                    gate = tasks[sid][1]
                    if sid in old_sids and sid != spool_id:
                        # 'unset' case
                        if old_printer == self.printer_hostname and 0 <= old_gate < self.nb_gates and not updated_gate_ids.get(old_gate):
                            updated_gate_ids[old_gate] = -1
                        self.spool_location[sid] = ('', -1, filament_attr)
                        self.server.send_event("spoolman:unset_spool_gate", {"spool_id": sid, "printer": old_printer, "gate": old_gate})
                        await self._log_n_send(f"Spool {sid} unassigned from printer {old_printer} and gate {old_gate} in Spoolman db", silent=silent)
                    else:
                        # 'set' case
                        if 0 <= gate < self.nb_gates:
                            if old_printer == self.printer_hostname and 0 <= old_gate < self.nb_gates and not updated_gate_ids.get(old_gate):
                                updated_gate_ids[old_gate] = -1
                            updated_gate_ids[gate] = sid
                        self.spool_location[sid] = (self.printer_hostname, gate, filament_attr)
                        self.server.send_event("spoolman:set_spool_gate", {"spool_id": sid, "printer": self.printer_hostname, "gate": gate})
                        await self._log_n_send(f"Spool {sid} assigned to printer {self.printer_hostname} @ gate {gate} in Spoolman db", silent=silent)

            # Sync with Happy Hare if required
            if sync and updated_gate_ids:
                gate_ids = [(gate, spool_id) for gate, spool_id in updated_gate_ids.items()]
                return await self._send_gate_map_update(gate_ids, replace=True, silent=silent)
            return True


    async def unset_spool_gate(self, spool_id=None, gate=None, sync=False, silent=False) -> bool:
        '''
        Removes the printer + gate allocation in Spoolman db for gate or spool_id (if supplied)
        '''
        if not await self._check_init_spoolman(): return False
        async with self.cache_lock:
            await self._initialize_mmu()

            # Sanity checking...
            if spool_id is None and gate is None:
                await self._log_n_send("Trying to unset spool but no spool_id or gate provided", error=True, silent=silent)
                return False
            if spool_id is not None and gate is not None:
                await self._log_n_send(f"Trying to unset spool but both spool_id {spool_id} and gate {gate} provided. Only one or the other expected", error=True, silent=silent)
                return False
            if spool_id is not None:
                if not self.spool_location.get(spool_id, ('', -1, {})):
                    await self._log_n_send(f"Trying to unset spool {spool_id} but not found in cache. Perhaps try refreshing cache", error=True, silent=silent)
                    return False

            # Create minimal set of async tasks to update Spoolman db and run them in parallel
            sids = self._find_all_spool_ids(self.printer_hostname, gate) if gate is not None else [spool_id]
            tasks = {sid: self._unset_spool_gate(sid, silent=silent) for sid in sids}
            results = await asyncio.gather(*tasks.values())

            # Log results and update cache
            updated_gate_ids = {}
            for sid, result in zip(tasks.keys(), results):
                if result:
                    old_printer, old_gate, filament_attr = self.spool_location.get(sid, ('', -1, {}))
                    if old_printer == self.printer_hostname and 0 <= old_gate < self.nb_gates and not updated_gate_ids.get(old_gate):
                        updated_gate_ids[old_gate] = -1
                    self.spool_location[sid] = ('', -1, filament_attr)
                    self.server.send_event("spoolman:unset_spool_gate", {"spool_id": sid, "old_printer": self.printer_hostname, "old_gate": gate})
                    await self._log_n_send(f"Spool {sid} unassigned from printer {old_printer} and gate {old_gate} in Spoolman db", silent=silent)

            # Sync with Happy Hare if required
            if sync and updated_gate_ids:
                gate_ids = [(gate, spool_id) for gate, spool_id in updated_gate_ids.items()]
                return await self._send_gate_map_update(gate_ids, replace=True, silent=silent)
            return True


    async def _send_next_spoolid(self, value):
        '''
        Send a bare 'MMU_GATE_MAP NEXT_SPOOLID=<value>' back to Happy Hare. Used to
        report the terminal outcome of a shared-reader lookup so the Klipper side
        can release its in-flight guard:
          >0  resolved spool_id     -1  recoverable failure (re-read allowed)
          -2  definitive unknown tag (release guard, do not re-read)
        '''
        if not self._mmu_backend_enabled():
            return
        try:
            await self.klippy_apis.run_gcode(f"MMU_GATE_MAP NEXT_SPOOLID={value} QUIET=1")
        except Exception as e:
            logging.error(f"NFC: failed to send NEXT_SPOOLID={value}: {str(e)}")


    async def get_spool_by_uid(self, uid=None, gate=None, metadata=None, save=False, silent=False) -> bool:
        '''
        Resolve a scanned NFC/RFID tag UID to a spool_id and hand it back to
        Happy Hare.

        When the UID is unknown to Spoolman, 'save' is True and 'metadata' (the
        parsed tag payload from the Klipper-side reader) carries a usable
        'material', a new vendor/filament/spool is auto-created from the tag data
        and the UID registered against it (see the RFID SPOOL AUTO-CREATE section
        below). This turns what would be a NEXT_SPOOLID=-2 "unknown" into a
        positive resolution. Otherwise the unknown-tag path is unchanged.

        This is the async counterpart to the NFC reader on the Klipper side
        (see mmu_nfc_manager): the reader reads only the tag UID and calls this
        remote method, which looks the UID up in Spoolman and, on success, runs
        an MMU_GATE_MAP command back on Klipper.

        The callback flavour depends on 'gate':
          - gate is None -> 'MMU_GATE_MAP NEXT_SPOOLID=<spool_id>' (a shared
            reader with no known gate; Klipper decides via set_pending_spool_id
            according to its configured spoolman_support mode)
          - gate is a number -> 'MMU_GATE_MAP GATE=<gate> SPOOLID=<spool_id>'
            (a per-gate reader that knows exactly which gate the tag is on)
        '''
        if not await self._check_init_spoolman(): return False
        async with self.cache_lock:
            await self._initialize_mmu()

            if not uid:
                await self._log_n_send("NFC: tag scan with no UID supplied", error=True, silent=silent)
                return False

            uid_norm = self._normalise_uid(uid)
            spool_id = self.uid_to_spool_id.get(uid_norm)

            if spool_id is None:
                # Skip the fetch if we recently confirmed this tag is unknown, so
                # rapid re-scans of an unregistered tag don't hammer Spoolman
                now = time.monotonic()
                miss_expiry = self.uid_miss_cache.get(uid_norm)
                if miss_expiry is not None and now < miss_expiry:
                    logging.debug(f"NFC: tag {uid_norm} still in miss cache, skipping Spoolman lookup")
                    return False

                # A freshly-registered tag may not be in the cache yet - refresh once
                # and retry. Distinguish a genuine miss from a Spoolman outage: a
                # failed rebuild preserves the existing cache, so don't cache a miss.
                if not await self._build_spool_location_cache(silent=True):
                    await self._log_n_send(f"NFC: couldn't reach Spoolman to resolve tag {uid_norm} (check moonraker.log)", error=True, silent=silent)
                    # Recoverable failure. For a shared-reader lookup signal
                    # NEXT_SPOOLID=-1 so Klipper releases the in-flight guard and
                    # allows the tag to be re-read (a retry may succeed).
                    if gate is None:
                        await self._send_next_spoolid(-1)
                    return False
                spool_id = self.uid_to_spool_id.get(uid_norm)

            created = False
            if spool_id is None and save and metadata and metadata.get("material"):
                # Tag isn't registered but carried usable filament metadata and
                # auto-create is enabled: build the vendor/filament/spool now and
                # register this UID against the new spool.
                new_id = await self._create_spool_from_metadata(metadata, uid_norm)
                if new_id is not None:
                    # Targeted cache insert - no full rebuild (see _cache_insert_spool)
                    spool_info = await self._fetch_spool_info(new_id)
                    if spool_info:
                        self._cache_insert_spool(spool_info)
                    else:
                        self.uid_to_spool_id[uid_norm] = new_id
                    self.uid_miss_cache.pop(uid_norm, None)
                    await self._log_n_send(f"NFC: created Spoolman spool {new_id} for new tag {uid_norm}", silent=silent)
                    spool_id = new_id
                    created = True

            if spool_id is None:
                # Remember the miss for a while and prune any expired entries
                now = time.monotonic()
                self.uid_miss_cache = {u: exp for u, exp in self.uid_miss_cache.items() if exp > now}
                self.uid_miss_cache[uid_norm] = now + NFC_UID_MISS_TTL
                await self._log_n_send(f"NFC: unknown tag {uid_norm} - not registered against any spool in Spoolman", silent=silent)
                # Definitive miss. For a shared-reader lookup signal NEXT_SPOOLID=-2
                # so Klipper releases the guard WITHOUT re-reading (a re-scan of the
                # same unregistered tag won't help and would just loop).
                if gate is None:
                    await self._send_next_spoolid(-2)
                return False

            # Positive result - drop any stale negative-cache entry for this tag
            self.uid_miss_cache.pop(uid_norm, None)

            logging.info(f"NFC: tag {uid_norm} resolved to spool_id {spool_id}" + (f" for gate {gate}" if gate is not None else ""))

            # Hand the resolved spool back to Happy Hare (which releases the guard).
            if self._mmu_backend_enabled():
                # CREATED=1 lets Happy Hare log that this tag minted a new spool record
                created_flag = ' CREATED=1' if created else ''
                if gate is None:
                    cmd = f"MMU_GATE_MAP NEXT_SPOOLID={spool_id}{created_flag} QUIET=1"
                else:
                    cmd = f"MMU_GATE_MAP GATE={gate} SPOOLID={spool_id}{created_flag} QUIET=1"
                try:
                    await self.klippy_apis.run_gcode(cmd)
                except Exception as e:
                    await self._log_n_send(f"NFC: exception running '{cmd}': {str(e)}", error=True, silent=silent)
                    return False
            return True



    # ══════════════════════════════════════════════════════════════════════════
    # RFID SPOOL AUTO-CREATE
    #
    # Build a Spoolman vendor -> filament -> spool from parsed NFC/RFID tag
    # metadata when a scanned UID isn't yet registered, and register the UID
    # against the new spool. Entry point: _create_spool_from_metadata(), called
    # from the unknown-tag branch of get_spool_by_uid() above.
    #
    # Ported from the standalone lameandboard Spoolman client so that it runs
    # Moonraker-side (async, self.http_client) - all Spoolman *and* SpoolmanDB
    # socket traffic originates here rather than from Klipper. Reference
    # enrichment (density, temps, canonical vendor name) comes from SpoolmanDB
    # (donkie.github.io) and degrades gracefully to the module-level fallbacks.
    # ══════════════════════════════════════════════════════════════════════════

    async def _create_spool_from_metadata(self, metadata, uid_norm) -> int | None:
        '''
        Create a Spoolman vendor -> filament -> spool from parsed tag metadata.
        Returns the new spool_id, or None if the metadata is insufficient or any
        Spoolman call fails.

        'metadata' is the flat dict produced by the Klipper-side tag parser
        (keys: material, material_detail, color_hex, brand, weight_g,
        spool_weight_g, diameter_mm, material_id, tray_uid, min/max/bed_temp,
        tag_format, is_bambu, ...). The UID is written into the spool's
        '{MMU_RFID_FIELD}' extra field at creation time.
        '''
        material = str(metadata.get("material") or "").strip()
        if not material:
            logging.info("NFC: auto-create skipped - tag metadata has no material")
            return None

        color_hex = str(metadata.get("color_hex") or "").strip().lstrip("#").upper() or None
        diameter  = self._to_float(metadata.get("diameter_mm"), 1.75)
        weight    = self._to_float(metadata.get("weight_g"), DEFAULT_SPOOL_WEIGHT_G)
        brand     = str(metadata.get("brand") or "").strip()
        if not brand:
            brand = TAG_FORMAT_BRANDS.get(str(metadata.get("tag_format") or ""), "Generic")
        is_bambu    = bool(metadata.get("is_bambu")) or "bambu" in brand.lower()
        material_id = str(metadata.get("material_id") or "").strip() or None

        logging.info(f"NFC: auto-creating spool material={material!r} brand={brand!r} "
                     f"color={color_hex} material_id={material_id} weight={weight}g")

        # 1. Density + Bambu enrichment (may adopt the DB's canonical colour hex)
        density, bambu_match, canonical_color_hex = await self._resolve_density(
            material, color_hex, material_id, is_bambu)
        if canonical_color_hex:
            color_hex = canonical_color_hex

        # 2. Vendor (find or create, with Generic fallback)
        vendor_id, vendor_name = await self._resolve_vendor(brand, is_bambu)

        # 3+4. Filament (find existing or create from tag + SpoolmanDB data)
        filament_id = await self._find_or_create_filament(
            metadata, color_hex, density, diameter, weight, vendor_id, vendor_name, bambu_match)
        if filament_id is None:
            return None

        # 5. Spool, with the UID written inline into the extra field
        return await self._create_spool(filament_id, metadata, uid_norm, weight, bambu_match)


    async def _resolve_density(self, material, color_hex, material_id, is_bambu):
        '''
        Determine filament density (g/cm³) and, for Bambu tags, locate the
        matching SpoolmanDB entry (which carries temps/name/spool_weight).
        Order: SpoolmanDB Bambu (by SKU then material+colour) -> SpoolmanDB
        materials.json -> DENSITY_FALLBACK table -> DENSITY_DEFAULT.
        Returns (density, bambu_match, canonical_color_hex): bambu_match is None
        unless a usable Bambu entry (with density) was found; canonical_color_hex
        is the DB's hex for a matched SKU (else None).
        '''
        material_lower = material.lower().strip()
        bambu_match = None
        canonical_color_hex = None

        if is_bambu:
            bambu_filaments = await self._fetch_spoolmandb_bambu()
            # By Bambu SKU (material_id) via colors[].id - most precise match
            if material_id:
                for entry in bambu_filaments:
                    for c in (entry.get("colors") or []):
                        if str(c.get("id") or "").upper() == material_id.upper():
                            bambu_match = entry
                            if c.get("hex"):
                                canonical_color_hex = str(c["hex"]).upper().lstrip("#")
                            break
                    if bambu_match is not None:
                        break
            # Fall back to material type + colour hex
            if bambu_match is None:
                for entry in bambu_filaments:
                    if str(entry.get("material") or "").lower().strip() == material_lower:
                        bambu_match = entry
                        if color_hex:
                            for c in (entry.get("colors") or []):
                                if str(c.get("hex") or "").upper().lstrip("#") == color_hex:
                                    canonical_color_hex = color_hex
                                    break
                        break
            if bambu_match is not None:
                try:
                    return float(bambu_match["density"]), bambu_match, canonical_color_hex
                except (KeyError, TypeError, ValueError):
                    bambu_match = None  # no usable density - fall through to materials

        mat_db = await self._fetch_spoolmandb_materials()
        density = mat_db.get(material_lower)
        if density is None:
            density = DENSITY_FALLBACK.get(material_lower, DENSITY_DEFAULT)
        return density, bambu_match, canonical_color_hex


    async def _resolve_vendor(self, brand, is_bambu):
        '''
        Resolve a Spoolman vendor_id, creating the vendor if needed. Candidate
        order: SpoolmanDB manufacturer (Bambu) -> tag brand -> "Generic".
        Returns (vendor_id, vendor_name), or (None, None) if all candidates fail.
        '''
        candidates = []
        mfr = self._spoolmandb_bambu_mfr
        if is_bambu and mfr and mfr.lower() != "generic":
            candidates.append(mfr)
            if brand and brand.lower() != "generic" and brand != mfr:
                candidates.append(brand)
        elif brand and brand.lower() != "generic":
            candidates.append(brand)
        candidates.append("Generic")

        for name in candidates:
            vendor_id = await self._vendor_id_for_name(name)
            if vendor_id is not None:
                return vendor_id, name
        return None, None


    async def _vendor_id_for_name(self, name):
        '''Find (case-insensitive) or create a Spoolman vendor by name. Returns id or None.'''
        base = self.spoolman.spoolman_url
        resp = await self.http_client.get(url=f"{base}/v1/vendor?{urllib.parse.urlencode({'name': name})}")
        if not resp.has_error():
            items = resp.json()
            if not isinstance(items, list):
                items = items.get("items", []) if isinstance(items, dict) else []
            for v in items:
                if str(v.get("name", "")).lower() == name.lower():
                    return int(v["id"])
        resp = await self.http_client.post(url=f"{base}/v1/vendor", body={"name": name})
        if resp.has_error():
            logging.warning(f"NFC: vendor create failed for {name!r}: {self.spoolman._get_response_error(resp)}")
            return None
        created = resp.json()
        if isinstance(created, dict) and created.get("id") is not None:
            return int(created["id"])
        return None


    async def _find_or_create_filament(self, metadata, color_hex, density,
                                       diameter, weight, vendor_id, vendor_name, bambu_match) -> int | None:
        '''
        Find an existing filament (by Bambu external_id, then material + vendor
        with colour preference) or create one from tag + SpoolmanDB metadata.
        Returns filament_id, or None on failure.
        '''
        base = self.spoolman.spoolman_url
        material = str(metadata.get("material") or "").strip()
        material_id = str(metadata.get("material_id") or "").strip() or None

        # --- Search by Bambu SKU (external_id) first ---
        if material_id:
            resp = await self.http_client.get(
                url=f"{base}/v1/filament?{urllib.parse.urlencode({'external_id': material_id})}")
            if not resp.has_error():
                items = resp.json()
                if isinstance(items, list) and items:
                    return int(items[0]["id"])

        # --- Search by material (+ vendor), preferring a colour match ---
        params = {"material": material}
        if vendor_id is not None and vendor_name:
            params["vendor_name"] = vendor_name
        resp = await self.http_client.get(url=f"{base}/v1/filament?{urllib.parse.urlencode(params)}")
        if resp.has_error():
            logging.warning(f"NFC: filament search failed: {self.spoolman._get_response_error(resp)}")
            return None
        items = resp.json()
        if isinstance(items, list) and items:
            if color_hex:
                for f in items:
                    if str(f.get("color_hex") or "").upper() == color_hex:
                        return int(f["id"])
            return int(items[0]["id"])

        # --- Create: tag data first, filling gaps from SpoolmanDB (Bambu) ---
        # Vendor is stored separately (vendor_id), so keep it OUT of the filament
        # name - this keeps a spool-resolved gate's name consistent with the
        # metadata-only gate-map path (which also stores vendor separately).
        db_name = str(bambu_match.get("name") or "").strip() if bambu_match else ""
        name = db_name or str(metadata.get("material_detail") or material).strip().replace("_", " ")

        body = {
            "name": name,
            "material": material,
            "density": float(density),
            "diameter": float(diameter),
            "weight": float(weight),
        }
        if color_hex:
            body["color_hex"] = color_hex
        if vendor_id is not None:
            body["vendor_id"] = vendor_id
        if material_id:
            body["external_id"] = material_id

        # Temperatures: tag data first, then SpoolmanDB (Bambu). Spoolman treats
        # settings_extruder_temp as a recommended default, so use the median of
        # the min/max range when both are known (safer than running at the
        # ceiling), falling back to whichever bound is available. The max is kept
        # separately in settings_extruder_temp_max.
        min_temp = metadata.get("min_temp")
        max_temp = metadata.get("max_temp")
        bed_temp = metadata.get("bed_temp")
        if bambu_match is not None:
            if min_temp is None:
                min_temp = bambu_match.get("extruder_temp")
            if max_temp is None:
                max_temp = bambu_match.get("extruder_temp_max")
            if bed_temp is None:
                bed_temp = bambu_match.get("bed_temp")
        ext_min = self._to_int_safe(min_temp)
        ext_max = self._to_int_safe(max_temp)
        bed     = self._to_int_safe(bed_temp)
        if ext_min is not None and ext_max is not None:
            body["settings_extruder_temp"] = round((ext_min + ext_max) / 2)
        elif ext_max is not None:
            body["settings_extruder_temp"] = ext_max
        elif ext_min is not None:
            body["settings_extruder_temp"] = ext_min
        if ext_max is not None:
            body["settings_extruder_temp_max"] = ext_max
        if bed is not None:
            body["settings_bed_temp"] = bed

        logging.info(f"NFC: creating Spoolman filament: {json.dumps(body, default=str)}")
        resp = await self.http_client.post(url=f"{base}/v1/filament", body=body)
        if resp.has_error():
            logging.warning(f"NFC: filament create failed: {self.spoolman._get_response_error(resp)}")
            return None
        created = resp.json()
        if not isinstance(created, dict) or created.get("id") is None:
            logging.warning(f"NFC: filament create returned unexpected response: {created!r}")
            return None
        return int(created["id"])


    async def _create_spool(self, filament_id, metadata, uid_norm, weight, bambu_match) -> int | None:
        '''
        Create a Spoolman spool for filament_id, writing the RFID UID into the
        '{MMU_RFID_FIELD}' extra field inline (so no follow-up PATCH is needed -
        the field already exists, created during _init_spoolman). Returns the new
        spool_id, or None on failure.
        '''
        base = self.spoolman.spoolman_url

        spool_weight = metadata.get("spool_weight_g")
        if spool_weight is None and bambu_match is not None:
            spool_weight = bambu_match.get("spool_weight")
        tray_uid = self._valid_tray_uid(metadata.get("tray_uid"))

        # A freshly registered tag is assumed to be a full spool: remaining_weight
        # equals the filament's net weight (initial_weight is left for Spoolman to infer).
        body = {
            "filament_id": int(filament_id),
            "remaining_weight": float(weight),
            "extra": {MMU_RFID_FIELD: json.dumps(uid_norm)},
        }
        sw = self._to_float(spool_weight, None)
        if sw is not None:
            body["spool_weight"] = sw
        if tray_uid:
            body["lot_nr"] = tray_uid

        logging.info(f"NFC: creating Spoolman spool: {json.dumps(body, default=str)}")
        resp = await self.http_client.post(url=f"{base}/v1/spool", body=body)
        if resp.has_error():
            logging.warning(f"NFC: spool create failed: {self.spoolman._get_response_error(resp)}")
            return None
        created = resp.json()
        if not isinstance(created, dict) or created.get("id") is None:
            logging.warning(f"NFC: spool create returned unexpected response: {created!r}")
            return None
        return int(created["id"])


    def _cache_insert_spool(self, spool_info) -> int:
        '''
        Insert/refresh a single spool in the location + UID caches without a full
        rebuild. Mirrors the per-spool logic in _build_spool_location_cache so a
        freshly auto-created spool resolves on the very next scan.
        '''
        spool_id = spool_info['id']
        extra = spool_info.get('extra') or {}
        printer_name = json.loads(extra.get(MMU_NAME_FIELD, "\"\"")).strip('"')
        mmu_gate = int(extra.get(MMU_GATE_FIELD, -1))
        filament_attr = self._get_filament_attr(spool_info)
        self.spool_location[spool_id] = (printer_name, mmu_gate, filament_attr)
        uid = filament_attr.get('rfid')
        if uid:
            self.uid_to_spool_id[uid] = spool_id
        return spool_id


    async def _fetch_spoolmandb_materials(self) -> dict:
        '''
        Fetch + cache the SpoolmanDB materials.json density table
        ({material_lower: density}). Returns {} on failure (cached so it isn't
        retried on every scan); callers then fall back to DENSITY_FALLBACK.
        '''
        if self._spoolmandb_materials is not None:
            return self._spoolmandb_materials
        result = {}
        try:
            resp = await self.http_client.get(url=SPOOLMANDB_MATERIALS_URL)
            if resp.has_error():
                raise RuntimeError(f"HTTP {resp.status_code}")
            raw = resp.json()
            if isinstance(raw, list):
                for entry in raw:
                    name = str(entry.get("name") or "").strip().lower()
                    density = entry.get("density")
                    if name and density is not None:
                        try:
                            result[name] = float(density)
                        except (TypeError, ValueError):
                            pass
            elif isinstance(raw, dict):
                for name, entry in raw.items():
                    density = entry.get("density") if isinstance(entry, dict) else entry
                    if density is not None:
                        try:
                            result[str(name).lower()] = float(density)
                        except (TypeError, ValueError):
                            pass
            logging.info(f"NFC: SpoolmanDB materials loaded ({len(result)} entries)")
        except Exception as e:
            logging.info(f"NFC: SpoolmanDB materials fetch failed ({e}); using fallback densities")
        self._spoolmandb_materials = result
        return result


    async def _fetch_spoolmandb_bambu(self) -> list:
        '''
        Fetch + cache the SpoolmanDB Bambu Lab filament list (bambulab.json) and
        the top-level manufacturer name (used for vendor normalisation). Returns
        [] on failure (cached so it isn't retried on every scan).
        '''
        if self._spoolmandb_bambu is not None:
            return self._spoolmandb_bambu
        filaments = []
        self._spoolmandb_bambu_mfr = None
        try:
            resp = await self.http_client.get(url=SPOOLMANDB_BAMBU_URL)
            if resp.has_error():
                raise RuntimeError(f"HTTP {resp.status_code}")
            raw = resp.json()
            if isinstance(raw, dict):
                filaments = raw.get("filaments") or []
                mfr = str(raw.get("manufacturer") or "").strip()
                if mfr:
                    self._spoolmandb_bambu_mfr = mfr
            elif isinstance(raw, list):
                filaments = raw
            if not isinstance(filaments, list):
                filaments = []
            logging.info(f"NFC: SpoolmanDB Bambu filaments loaded ({len(filaments)} entries, "
                         f"manufacturer={self._spoolmandb_bambu_mfr!r})")
        except Exception as e:
            logging.info(f"NFC: SpoolmanDB Bambu fetch failed ({e}); falling back to materials lookup")
            filaments = []
            self._spoolmandb_bambu_mfr = None
        self._spoolmandb_bambu = filaments
        return filaments


    @staticmethod
    def _to_int_safe(val):
        '''Coerce to int, or None on failure/None.'''
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None


    @staticmethod
    def _to_float(val, default):
        '''Coerce to float, or return default on failure/None.'''
        if val is None:
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            return default


    @staticmethod
    def _valid_tray_uid(raw):
        '''
        Return an uppercase even-length hex string (a spool lot_nr), or None.
        Guards against sending a malformed value Spoolman would reject with 422.
        '''
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            return raw.hex().upper()
        s = str(raw).strip()
        if re.fullmatch(r"[0-9A-Fa-f]+", s) and len(s) % 2 == 0:
            return s.upper()
        return None


    async def set_spool_uid(self, spool_id=None, uid=None, silent=False) -> bool:
        '''
        Register (write) an NFC/RFID tag UID onto a spool record in Spoolman,
        so future scans of that tag resolve to this spool_id.

        NOTE: This is provided for tag-registration workflows but is not yet
        wired to a Klipper-side command in the private_rfid branch - there is
        currently no caller. It is registered as a remote method so a future
        'register this tag to this spool' gcode can use it.
        '''
        if not await self._check_init_spoolman(): return False
        async with self.cache_lock:
            if spool_id is None or not uid:
                await self._log_n_send(f"NFC: cannot register tag - spool_id={spool_id} uid={uid}", error=True, silent=silent)
                return False

            uid_norm = self._normalise_uid(uid)
            data = {'extra': {MMU_RFID_FIELD: json.dumps(uid_norm)}}
            response = await self.http_client.request(
                method="PATCH",
                url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
                body=data
            )
            if response.status_code == 404:
                logging.error(f"'{self.spoolman.spoolman_url}/v1/spool/{spool_id}' not found")
                await self._log_n_send(f"NFC: SpoolId {spool_id} not found", error=True, silent=silent)
                return False
            elif response.has_error():
                err_msg = self.spoolman._get_response_error(response)
                logging.error(f"Attempt to register tag failed: {err_msg}")
                await self._log_n_send(f"NFC: Failed to register tag {uid_norm} on spool {spool_id}. See moonraker.log for details.", error=True, silent=silent)
                return False

            # Update caches to reflect the new UID association
            self.uid_to_spool_id = {u: sid for u, sid in self.uid_to_spool_id.items() if sid != spool_id}
            self.uid_to_spool_id[uid_norm] = spool_id
            self.uid_miss_cache.pop(uid_norm, None) # This tag is now known
            if spool_id in self.spool_location:
                printer, gate, filament_attr = self.spool_location[spool_id]
                filament_attr['rfid'] = uid_norm
                self.spool_location[spool_id] = (printer, gate, filament_attr)

            await self._log_n_send(f"NFC: tag {uid_norm} registered against spool {spool_id} in Spoolman db", silent=silent)
            return True


    async def display_spool_info(self, spool_id: int | None = None):
        '''
        Gets info for active spool id and sends it to the klipper console. Does not require Spoolman db extension
        '''
        async with self.cache_lock:
            active = "Spool"

            if not spool_id:
                logging.info("Fetching active spool")
                spool_id = await self.spoolman.database.get_item(DB_NAMESPACE, ACTIVE_SPOOL_KEY, None)
                active = "Active spool"

            if not spool_id:
                msg = "No active spool set and no spool id supplied"
                await self._log_n_send(msg, error=True)
                return False

            spool_info = await self._fetch_spool_info(spool_id)
            if not spool_info:
                msg = f"Spool id {spool_id} not found"
                await self._log_n_send(msg, error=True)
                return False

            material = spool_info.get('material', "n/a")
            used_weight = int(spool_info.get('used_weight', -1))
            f_used_weight = f"{used_weight} g" if used_weight >= 0 else "n/a"
            remaining_weight = int(spool_info.get('remaining_weight', -1))
            f_remaining_weight = f"{remaining_weight} g" if remaining_weight >= 0 else "n/a"
            msg = f"{active} is: {spool_info['filament']['name']} (id: {spool_info['id']})\n"
            msg += f"  - Material: {material}\n"
            msg += f"  - Used: {f_used_weight}\n"
            msg += f"  - Remaining: {f_remaining_weight}\n"

            # Check if spool_id is assigned
            spool = next((gate for sid, (printer, gate, _) in self.spool_location.items() if spool_id == sid and self.printer_hostname == printer), None)
            if spool is not None:
                msg += f"  - Gate: {spool}"
            else:
                msg += f"Spool id {spool_id} is not assigned to this printer!\n"
                msg += f"Run: MMU_SPOOLMAN SPOOLID={spool_id} GATE=.. to add"
            await self._log_n_send(msg)
            return True


    async def display_spool_location(self, printer=None):
        '''
        Builds a sorted table of gate to spool association for the specified printer and sends to klipper console
        '''
        if not await self._check_init_spoolman(): return
        async with self.cache_lock:
            await self._initialize_mmu()
            printer_name = printer or self.printer_hostname
            filtered = sorted(((spool_id, gate) for spool_id, (printer, gate, _) in self.spool_location.items() if printer == printer_name), key=lambda x: x[1])
            if filtered:
                msg = f"Spoolman gate assignment for printer: {printer_name}\n"
                msg += "Gate | SpoolId\n"
                msg += "-----+--------\n"
                if self.nb_gates:
                    for mmu_gate in range(self.nb_gates):
                        sids = [spool_id for (spool_id, gate) in filtered if gate == mmu_gate]
                        sids_str = ",".join(map(str, sids))
                        warning = " Error: Can only have a single spool assigned" if len(sids) > 1 else ""
                        msg += f"{mmu_gate:<5}| {sids_str}{warning}\n"
                else:
                    # If not initialize_mmu() we will get here
                    for spool_id, gate in filtered:
                        msg += f"{gate:<5}| {spool_id}\n"
                    msg += "Run: MMU_SPOOLMAN REFRESH=1 to reset number of MMU gates"
            else:
                msg = f"No gates assigned for printer: {printer_name}"
            await self._log_n_send(msg)


    async def push_lane_data(self, gate_ids):
        '''
        Pushes lane data to Moonraker database for slicer integration (OrcaSlicer)
        gate_ids: list of tuples [(gate, spool_id), ...]
        '''
        try:
            from datetime import datetime, timezone

            # Get MMU state from Klipper
            mmu_state = await self.klippy_apis.query_objects({"mmu": None})
            mmu = mmu_state.get('mmu', {})

            gate_material = mmu.get('gate_material', [])
            gate_vendor = mmu.get('gate_vendor', [])
            gate_color = mmu.get('gate_color', [])
            gate_temperature = mmu.get('gate_temperature', [])
            gate_status = mmu.get('gate_status', [])
            gate_filament_name = mmu.get('gate_filament_name', [])

            # Build batch of lane data
            batch_data = {}

            for gate, spool_id in gate_ids:
                if gate < 0:
                    continue

                # Lane uses same 0-based numbering as gate
                lane = gate
                lane_key = f"lane{lane}"

                # Check if gate is empty (status -1/unknown or 0/empty, or spool_id -1)
                gate_status_val = gate_status[gate] if gate < len(gate_status) else -1
                is_empty = gate_status_val in [-1, 0] or spool_id == -1

                if is_empty:
                    # Empty gate format
                    lane_data = {
                        "vendor_name": None,
                        "name": None,
                        "color": None,
                        "material": None,
                        "bed_temp": None,
                        "nozzle_temp": None,
                        "scan_time": None,
                        "td": None,
                        "lane": str(lane),
                        "spool_id": None,
                        "filament_id": None
                    }
                else:
                    spool_attrs = self.spool_location.get(spool_id, ('', -1, {}))[2] if spool_id in self.spool_location else {}
                    # Populated gate format
                    lane_data = {
                        "vendor_name": (gate_vendor[gate] if gate < len(gate_vendor) else None) or spool_attrs.get('vendor', None) or None,
                        "name": (gate_filament_name[gate] if gate < len(gate_filament_name) else None) or spool_attrs.get('name', None) or None,
                        "color": gate_color[gate] if gate < len(gate_color) else None,
                        "td": None, # we don't currently capture transmision distance and isn't standard in spoolman
                        "material": gate_material[gate] if gate < len(gate_material) else None,
                        "bed_temp": spool_attrs.get('bed_temp', None) or None,
                        "nozzle_temp": gate_temperature[gate] if gate < len(gate_temperature) else 200,
                        "scan_time": None,
                        "lane": str(lane), # currently orca reads this as a string, but it is actually an int representing the gate number
                        "spool_id": spool_id if spool_id > 0 else None,
                        "filament_id": spool_attrs.get('filament_id', None) or None
                    }

                batch_data[lane_key] = lane_data

            # Push all lane data in a single batch
            if batch_data:
                await self.database.insert_batch("lane_data", batch_data)

        except Exception as e:
            logging.error(f"Error pushing lane data: {e}")


    async def cleanup_lane_data(self, num_gates):
        '''
        Removes lane data for gates that no longer exist (e.g., if MMU size was reduced)
        num_gates: current number of gates in the MMU
        '''
        try:
            # Get all items in the lane_data namespace
            lane_items = await self.database.get_item("lane_data", None, {})

            # Delete lanes beyond the current num_gates (0-based: valid lanes are 0 to num_gates-1)
            keys_to_delete = []
            for lane_key, lane_value in lane_items.items():
                # Extract lane number from the data object's lane field
                if isinstance(lane_value, dict):
                    lane_str = lane_value.get('lane', '')
                    try:
                        lane_num = int(lane_str)
                        # If lane number >= num_gates, it's out of bounds, mark for deletion
                        if lane_num >= num_gates:
                            keys_to_delete.append(lane_key)
                    except (ValueError, TypeError):
                        continue

            # Delete the old lanes
            await self.database.delete_batch("lane_data", keys_to_delete)
            logging.info(f"Removed old lane data: {keys_to_delete}")

        except Exception as e:
            logging.error(f"Error cleaning up lane data: {e}")



    # Switch out the metadata processor with this module which handles placeholders
    def setup_placeholder_processor(self, config):
        args = " -m" if config.getboolean("enable_file_preprocessor", True) else ""
        args += " -n" if config.getboolean("enable_toolchange_next_pos", True) else ""
        from .file_manager import file_manager
        file_manager.METADATA_SCRIPT = os.path.abspath(__file__) + args


def load_component(config):
    return MmuServer(config)





# ══════════════════════════════════════════════════════════════════════════
# Gcode file Metadata parsing extension
#
# Beyond this point this module acts like an extended file_manager/metadata module
#
# ══════════════════════════════════════════════════════════════════════════

AUTHORZIED_SLICERS = ['PrusaSlicer', 'SuperSlicer', 'OrcaSlicer', 'BambuStudio']

HAPPY_HARE_FINGERPRINT = "; processed by HappyHare"
MMU_REGEX = r"^" + HAPPY_HARE_FINGERPRINT
SLICER_REGEX = r"^;.*generated by ([a-z]*) .*$|^; (BambuStudio) .*$"
ORCASLICER_VERSION_REGEX = r"^;\s*generated by OrcaSlicer\s+(\d+(?:\.\d+){0,3})"

TOOL_DISCOVERY_REGEX = r"((^MMU_CHANGE_TOOL(_STANDALONE)? .*?TOOL=)|(^T))(?P<tool>\d{1,2})"

METADATA_TOOL_DISCOVERY = "!referenced_tools!"
METADATA_TOTAL_TOOLCHANGES = "!total_toolchanges!"

METADATA_BEGIN_PURGING = "CP TOOLCHANGE WIPE"
METADATA_END_PURGING = "CP TOOLCHANGE END"

# PS/SS uses "extruder_colour", Orca uses "filament_colour" but extruder_colour can exist with empty or single color
COLORS_REGEX = {
    'PrusaSlicer' : r"^;\s*(?:extruder|filament)_colour\s*=\s*(#.*;*.*)$", #if extruder colour is not set, check filament colour
    'SuperSlicer' : r"^;\s*(?:extruder|filament)_colour\s*=\s*(#.*;*.*)$", #if extruder colour is not set, check filament colour
    'OrcaSlicer'  : r"^;\s*filament_colour\s*=\s*(#.*;*.*)$",
    'BambuStudio' : r"^;\s*filament_colour\s*=\s*(#.*;*.*)$",
}
METADATA_COLORS = "!colors!"

TEMPS_REGEX = r"^;\s*(nozzle_)?temperature\s*=\s*(.*)$" # Orca Slicer/Bambu Studio has the 'nozzle_' prefix, others might not
METADATA_TEMPS = "!temperatures!"

MATERIALS_REGEX = r"^;\s*filament_type\s*=\s*(.*)$"
METADATA_MATERIALS = "!materials!"

PURGE_VOLUMES_REGEX = r"^;\s*(flush_volumes_matrix|wiping_volumes_matrix)\s*=\s*(.*)$" # flush.. in Orca/Bambu, wiping... in PS
METADATA_PURGE_VOLUMES = "!purge_volumes!"

FLUSH_MULTIPLIER_REGEX = r"^;\s*flush_multiplier\s*=\s*(.*)$" #flush multiplier in Orca/Bambu. Used to multiply the values in the purge volumes to match the slicer UI settings

FILAMENT_NAMES_REGEX = r"^;\s*(filament_settings_id)\s*=\s*(.*)$"
METADATA_FILAMENT_NAMES = "!filament_names!"

# Detection for next pos processing
T_PATTERN  = r'^T(\d+)\s*(?:;.*)?$'
G1_PATTERN = r'^G[01](?=.*\sX(-?[\d.]+))(?=.*\sY(-?[\d.]+)).*$'


def _parse_version_tuple(version_str: str, parts: int = 3):
    """Parse a version like '2.3.2-dev'/'2.3.2' into a comparable tuple (2, 3, 2).

    Only the numeric dot-separated prefix is considered; missing parts are padded with zeros.
    """
    if not version_str:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)*)", version_str)
    if not m:
        return None
    nums = m.group(1).split(".")
    out = []
    for s in nums[:parts]:
        try:
            out.append(int(s))
        except ValueError:
            out.append(0)
    while len(out) < parts:
        out.append(0)
    return tuple(out)


def _format_volume(v: float) -> str:
    """Format a purge volume number without trailing .0, keeping up to 1 decimal place."""
    v = round(float(v), 1)
    s = f"{v:.1f}"
    return s.rstrip("0").rstrip(".")


def gcode_processed_already(file_path):
    """Expects first line of gcode to be the HAPPY_HARE_FINGERPRINT '; processed by HappyHare'"""

    mmu_regex = re.compile(MMU_REGEX, re.IGNORECASE)

    with open(file_path, 'r') as in_file:
        line = in_file.readline()
        return mmu_regex.match(line)


def parse_gcode_file(file_path):
    slicer_regex = re.compile(SLICER_REGEX, re.IGNORECASE)
    orca_version_regex = re.compile(ORCASLICER_VERSION_REGEX, re.IGNORECASE)
    has_tools_placeholder = has_total_toolchanges = has_colors_placeholder = has_temps_placeholder = has_materials_placeholder = has_purge_volumes_placeholder = filament_names_placeholder = False
    found_colors = found_temps = found_materials = found_purge_volumes = found_filament_names = found_flush_multiplier = False
    slicer = None
    orca_version = None

    tools_used = set()
    total_toolchanges = 0
    colors = []
    temps = []
    materials = []
    purge_volumes = []
    filament_names = []
    flush_multiplier = 1.0 # Initialize flush_multiplier to 1.0

    with open(file_path, 'r') as in_file:
        for line in in_file:
            if not line.startswith(";"):
                continue

            # Discover slicer
            if not slicer:
                match = slicer_regex.match(line)
                if match:
                    slicer = match.group(1) or match.group(2)

            # Capture OrcaSlicer version (numeric prefix only, e.g. 2.3.2 from 2.3.2-dev)
            if orca_version is None:
                mver = orca_version_regex.match(line)
                if mver:
                    orca_version = _parse_version_tuple(mver.group(1))

    if slicer in AUTHORZIED_SLICERS:
        if isinstance(TOOL_DISCOVERY_REGEX, dict):
            tools_regex = re.compile(TOOL_DISCOVERY_REGEX[slicer], re.IGNORECASE)
        else:
            tools_regex = re.compile(TOOL_DISCOVERY_REGEX, re.IGNORECASE)
        if isinstance(COLORS_REGEX, dict):
            colors_regex = re.compile(COLORS_REGEX[slicer], re.IGNORECASE)
        else:
            colors_regex = re.compile(COLORS_REGEX, re.IGNORECASE)
        if isinstance(TEMPS_REGEX, dict):
            temps_regex = re.compile(TEMPS_REGEX[slicer], re.IGNORECASE)
        else:
            temps_regex = re.compile(TEMPS_REGEX, re.IGNORECASE)
        if isinstance(MATERIALS_REGEX, dict):
            materials_regex = re.compile(MATERIALS_REGEX[slicer], re.IGNORECASE)
        else:
            materials_regex = re.compile(MATERIALS_REGEX, re.IGNORECASE)
        if isinstance(PURGE_VOLUMES_REGEX, dict):
            purge_volumes_regex = re.compile(PURGE_VOLUMES_REGEX[slicer], re.IGNORECASE)
        else:
            purge_volumes_regex = re.compile(PURGE_VOLUMES_REGEX, re.IGNORECASE)
        if isinstance(FILAMENT_NAMES_REGEX, dict):
            filament_names_regex = re.compile(FILAMENT_NAMES_REGEX[slicer], re.IGNORECASE)
        else:
            filament_names_regex = re.compile(FILAMENT_NAMES_REGEX, re.IGNORECASE)

        if isinstance(FLUSH_MULTIPLIER_REGEX, dict):
            flush_multiplier_regex = re.compile(FLUSH_MULTIPLIER_REGEX[slicer], re.IGNORECASE)
        else:
            flush_multiplier_regex = re.compile(FLUSH_MULTIPLIER_REGEX, re.IGNORECASE)

        with open(file_path, 'r') as in_file:
            for line in in_file:
                # !referenced_tools! and !total_toolchanges! processing
                if not has_tools_placeholder and METADATA_TOOL_DISCOVERY in line:
                    has_tools_placeholder = True

                if not has_total_toolchanges and METADATA_TOTAL_TOOLCHANGES in line:
                    has_total_toolchanges = True

                match = tools_regex.match(line)
                if match:
                    tool = match.group("tool")
                    tools_used.add(int(tool))
                    total_toolchanges += 1

                # !colors! processing
                if not has_colors_placeholder and METADATA_COLORS in line:
                    has_colors_placeholder = True

                if not found_colors:
                    match = colors_regex.match(line)
                    if match:
                        colors_csv = [color.strip().lstrip('#') for color in match.group(1).split(';')]
                        if not colors:
                            colors.extend(colors_csv)
                        else:
                            colors = [n if o == '' else o for o,n in zip(colors,colors_csv)]
                        found_colors = all(len(c) > 0 for c in colors)

                # !temperatures! processing
                if not has_temps_placeholder and METADATA_TEMPS in line:
                    has_temps_placeholder = True

                if not found_temps:
                    match = temps_regex.match(line)
                    if match:
                        temps_csv = re.split(';|,', match.group(2).strip())
                        temps.extend(temps_csv)
                        found_temps = True

                # !materials! processing
                if not has_materials_placeholder and METADATA_MATERIALS in line:
                    has_materials_placeholder = True

                if not found_materials:
                    match = materials_regex.match(line)
                    if match:
                        materials_csv = match.group(1).strip().split(';')
                        materials.extend(materials_csv)
                        found_materials = True

                # flush_multiplier processing
                if not found_flush_multiplier:
                    match = flush_multiplier_regex.match(line)
                    if match:
                        try:
                            flush_multiplier = float(match.group(1).strip())
                        except ValueError:
                            flush_multiplier = 1.0  # Default to 1.0 if conversion fails
                        found_flush_multiplier = True

                # !purge_volumes! processing
                if not has_purge_volumes_placeholder and METADATA_PURGE_VOLUMES in line:
                    has_purge_volumes_placeholder = True

                if not found_purge_volumes:
                    match = purge_volumes_regex.match(line)
                    if match:
                        purge_volumes_csv = [v.strip() for v in match.group(2).strip().split(',')]
                        
                        # OrcaSlicer 2.3.2+ already bakes flush_multiplier into the flush_volumes_matrix.
                        # OrcaSlicer <=2.3.1 requires applying flush_multiplier here to match the UI.
                        apply_flush_multiplier = True
                        if slicer == "OrcaSlicer" and orca_version is not None and orca_version >= (2, 3, 2):
                            apply_flush_multiplier = False
                        
                        for volume_str in purge_volumes_csv:
                            # If we shouldn't apply multiplier, keep the raw value as-is (preserves integer formatting).
                            if not apply_flush_multiplier or flush_multiplier == 1.0:
                                purge_volumes.append(volume_str)
                                continue
                            try:
                                volume = float(volume_str)
                                multiplied_volume = volume * flush_multiplier
                                purge_volumes.append(_format_volume(multiplied_volume))
                            except ValueError:
                                # If conversion fails, keep the original value
                                purge_volumes.append(volume_str)
                        found_purge_volumes = True

                # !filament_names! processing
                if not filament_names_placeholder and METADATA_FILAMENT_NAMES in line:
                    filament_names_placeholder = True

                if not found_filament_names:
                    match = filament_names_regex.match(line)
                    if match:
                        filament_names_csv = [e.strip() for e in re.split(',|;', match.group(2).strip())]
                        filament_names.extend(filament_names_csv)
                        found_filament_names = True

    return (has_tools_placeholder or has_total_toolchanges or has_colors_placeholder or has_temps_placeholder or has_materials_placeholder or has_purge_volumes_placeholder or filament_names_placeholder,
            sorted(tools_used), total_toolchanges, colors, temps, materials, purge_volumes, filament_names, slicer)


def process_file(input_filename, output_filename, insert_nextpos, tools_used, total_toolchanges, colors, temps, materials, purge_volumes, filament_names):

    t_pattern = re.compile(T_PATTERN)
    g1_pattern = re.compile(G1_PATTERN)

    with open(input_filename, 'r') as infile, open(output_filename, 'w') as outfile:
        buffer = [] # Buffer lines between a "T" line and the next matching "G1" line
        tool = None # Store the tool number from a "T" line
        outfile.write(f'{HAPPY_HARE_FINGERPRINT}\n')

        for line in infile:
            line = add_placeholder(line, tools_used, total_toolchanges, colors, temps, materials, purge_volumes, filament_names)
            if tool is not None:
                # Buffer subsequent lines after a "T" line until next "G1" x,y move line is found
                buffer.append(line)
                g1_match = g1_pattern.match(line)
                if g1_match:
                    # Now replace "T" line and write buffered lines, including the current "G1" line
                    if insert_nextpos:
                        x, y = g1_match.groups()
                        outfile.write(f'MMU_CHANGE_TOOL TOOL={tool} NEXT_POS="{x},{y}" ; T{tool}\n')
                    else:
                        outfile.write(f'MMU_CHANGE_TOOL TOOL={tool} ; T{tool}\n')
                    for buffered_line in buffer:
                        outfile.write(buffered_line)
                    buffer.clear()
                    tool = None
                continue

            t_match = t_pattern.match(line)
            if t_match:
                tool = t_match.group(1)
            else:
                outfile.write(line)

        # If there is anything left in buffer it means there wasn't a final "G1" line
        if buffer:
            outfile.write(f"T{tool}\n")
            outfile.write(f'MMU_CHANGE_TOOL TOOL={tool} ; T{tool}\n')
            for line in buffer:
                outfile.write(line)

        # Finally append "; referenced_tools =" as new metadata (why won't Prusa pick up my PR?)
        outfile.write("; referenced_tools = %s\n" % ",".join(map(str, tools_used)))


def add_placeholder(line, tools_used, total_toolchanges, colors, temps, materials, purge_volumes, filament_names):
    # Ignore comment lines to preserve slicer metadata comments
    if not line.startswith(";"):
        if METADATA_TOOL_DISCOVERY in line:
            if tools_used:
                line = line.replace(METADATA_TOOL_DISCOVERY, ",".join(map(str, tools_used)))
            else:
                line = line.replace(METADATA_TOOL_DISCOVERY, "0")
        if METADATA_TOTAL_TOOLCHANGES in line:
            line = line.replace(METADATA_TOTAL_TOOLCHANGES, str(total_toolchanges))
        if METADATA_COLORS in line:
            line = line.replace(METADATA_COLORS, ",".join(map(str, colors)))
        if METADATA_TEMPS in line:
            line = line.replace(METADATA_TEMPS, ",".join(map(str, temps)))
        if METADATA_MATERIALS in line:
            line = line.replace(METADATA_MATERIALS, ",".join(map(str, materials)))
        if METADATA_PURGE_VOLUMES in line:
            line = line.replace(METADATA_PURGE_VOLUMES, ",".join(map(str, purge_volumes)))
        if METADATA_FILAMENT_NAMES in line:
            line = line.replace(METADATA_FILAMENT_NAMES, ",".join(map(str, filament_names)))
    else:
        if METADATA_BEGIN_PURGING in line:
            line = line + "_MMU_STEP_SET_ACTION STATE=12\n"
        elif METADATA_END_PURGING in line:
            line = line + "_MMU_STEP_SET_ACTION RESTORE=1\n"
    return line


def main(path, filename, insert_placeholders=False, insert_nextpos=False):
    file_path = os.path.join(path, filename)
    if not os.path.isfile(file_path):
        metadata.logger.info(f"File Not Found: {file_path}")
        sys.exit(-1)
    try:
        metadata.logger.info(f"mmu_server: Pre-processing file: {file_path}")
        fname = os.path.basename(file_path)
        if fname.endswith(".gcode") and not gcode_processed_already(file_path):
            with tempfile.TemporaryDirectory() as tmp_dir_name:
                tmp_file = os.path.join(tmp_dir_name, fname)

                if insert_placeholders:
                    start = time.time()
                    has_placeholder, tools_used, total_toolchanges, colors, temps, materials, purge_volumes, filament_names, slicer = parse_gcode_file(file_path)
                    metadata.logger.info("Reading placeholders took %.2fs. Detected gcode by slicer: %s" % (time.time() - start, slicer))
                else:
                    tools_used = total_toolchanges = colors = temps = materials = purge_volumes = filament_names = slicer = None
                    has_placeholder = False

                if (insert_nextpos and tools_used is not None and len(tools_used) > 0) or has_placeholder:
                    start = time.time()
                    msg = []
                    if has_placeholder:
                        msg.append("Writing MMU placeholders")
                    if insert_nextpos:
                        msg.append("Inserting next position to tool changes")
                    process_file(file_path, tmp_file, insert_nextpos, tools_used, total_toolchanges, colors, temps, materials, purge_volumes, filament_names)
                    metadata.logger.info("mmu_server: %s took %.2fs" % (",".join(msg), time.time() - start))

                    # Move temporary file back in place
                    if os.path.islink(file_path):
                        file_path = os.path.realpath(file_path)
                    if not filecmp.cmp(tmp_file, file_path):
                        shutil.move(tmp_file, file_path)
                    else:
                        metadata.logger.info(f"Files are the same, skipping replacement of: {file_path} by {tmp_file}")
                else:
                    metadata.logger.info(f"No MMU metadata placeholders found in file: {file_path}")

    except Exception:
        metadata.logger.info(traceback.format_exc())
        sys.exit(-1)


# When run separately this module wraps metadata to extend pre-processing functionality
if __name__ == "__main__":
    # Make it look like we are running in the file_manager directory
    directory = os.path.dirname(os.path.abspath(__file__))
    target_dir = directory + "/file_manager"
    os.chdir(target_dir)
    sys.path.insert(0, target_dir)

    import metadata
    metadata.logger.info("mmu_server: Running MMU enhanced version of metadata")

    # We need to re-parse arguments anyway, so this way, whilst relaxing need to copy code, isn't useful
    #runpy.run_module('metadata', run_name="__main__", alter_sys=True)

    # Parse start arguments (copied from metadata.py)
    parser = argparse.ArgumentParser(description="GCode Metadata Extraction Utility")
    parser.add_argument("-c", "--config", metavar='<config_file>', default=None, help="Optional json configuration file for metadata.py")
    parser.add_argument("-f", "--filename", metavar='<filename>', help="name gcode file to parse")
    parser.add_argument("-p", "--path", default=os.path.abspath(os.path.dirname(__file__)), metavar='<path>', help="optional absolute path for file")
    parser.add_argument("-u", "--ufp", metavar="<ufp file>", default=None, help="optional path of ufp file to extract")
    parser.add_argument("-o", "--check-objects", dest='check_objects', action='store_true', help="process gcode file for exclude object functionality")
    parser.add_argument("-m", "--placeholders", dest='placeholders', action='store_true', help="process happy hare mmu placeholders")
    parser.add_argument("-n", "--nextpos", dest='nextpos', action='store_true', help="add next position to tool change")
    args = parser.parse_args()
    config: Dict[str, Any] = {}
    if args.config is None:
        if args.filename is None:
            metadata.logger.info(
                "The '--filename' (-f) option must be specified when "
                " --config is not set"
            )
            sys.exit(-1)
        config["filename"] = args.filename
        config["gcode_dir"] = args.path
        config["ufp_path"] = args.ufp
        config["check_objects"] = args.check_objects
    else:
        # Config file takes priority over command line options
        try:
            with open(args.config, "r") as f:
                config = (json.load(f))
        except Exception:
            metadata.logger.info(traceback.format_exc())
            sys.exit(-1)
        if config.get("filename") is None:
            metadata.logger.info("The 'filename' field must be present in the configuration")
            sys.exit(-1)
    if config.get("gcode_dir") is None:
        config["gcode_dir"] = os.path.abspath(os.path.dirname(__file__))
    enabled_msg = "enabled" if config["check_objects"] else "disabled"
    metadata.logger.info(f"Object Processing is {enabled_msg}")

    # Parsing for mmu placeholders and next pos insertion. We do this first so we can add additonal metadata
    main(config["gcode_dir"], config["filename"], args.placeholders, args.nextpos)

    # Original metadata parser
    metadata.main(config)
