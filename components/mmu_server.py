# Happy Hare MMU Software
# Moonraker support for a file-preprocessor that injects MMU metadata into gcode files
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Original slicer parsing
# Copyright (C) 2023  Kieran Eglin <@kierantheman (discord)>, <kieran.eglin@gmail.com>
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
from __future__ import annotations
import json
import logging, os, sys, re, time, asyncio
import runpy, argparse, shutil, traceback, tempfile, filecmp
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
MIN_SM_VER       = (0, 18, 1)

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

        # Full cache of spool_ids and location + key attributes (printer, gate, attr_dict))
        # Example: {2: ('BigRed', 0, {"material": "pla", "color": "ff56e0"}), 3: ('BigRed', 3, {"material": "abs"}), ...
        self.spool_location = {}

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

        # Replace file_manager/metadata with this file
        self.setup_placeholder_processor(config)

        # Options
        self.update_location = self.config.getboolean("update_spoolman_location", True)

    async def _get_spoolman_version(self) -> tuple[int, int, int] | None:
        response = await self.http_client.get(url=f'{self.spoolman.spoolman_url}/v1/info')
        if response.status_code == 404:
            logging.info(f"'{self.spoolman.spoolman_url}/v1/info' not found")
            return False
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.error(f"Attempt to get info from spoolman failed: {err_msg}")
            return False
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

    def _get_filament_attr(self, spool_info) -> dict:
        spool_id = spool_info["id"]
        filament = spool_info["filament"]
        name = filament.get('name', '')
        material = filament.get('material', '')
        color_hex = filament.get('color_hex', '').strip('#')[:8].lower() # Remove problematic First # character if present
        temp = filament.get('settings_extruder_temp', '')
        return {'spool_id': spool_id, 'material': material, 'color': color_hex, 'name': name, 'temp': temp}

    async def _build_spool_location_cache(self, fix=False, silent=False) -> bool:
        '''
        Helper to get all spools and gates assigned to printers from Spoolman db and cache them
        '''
        logging.info("Building spool location cache from Spoolman db")
        try:
            self.spool_location.clear()
            # Fetch all spools
            errors = ""
            assignments = {}
            sids_to_fix = []
            reponse = await self.http_client.get(url=f'{self.spoolman.spoolman_url}/v1/spool')
            for spool_info in reponse.json():
                spool_id = spool_info['id']
                printer_name = json.loads(spool_info['extra'].get(MMU_NAME_FIELD, "\"\"")).strip('"')
                mmu_gate = int(spool_info['extra'].get(MMU_GATE_FIELD, -1))
                filament_attr = self._get_filament_attr(spool_info)
                self.spool_location[spool_id] = (printer_name, mmu_gate, filament_attr)

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
        if not await self._check_init_spoolman(): return

        # Use the PATCH method on the spoolman api
        if not silent:
            logging.info(f"Setting spool {spool_id} for printer {printer} @ gate {gate}")
        data = {'extra': {MMU_NAME_FIELD: json.dumps(f"{printer}"), MMU_GATE_FIELD: json.dumps(gate)}}
        if self.update_location:
            data['location'] = f"{printer} @ MMU Gate:{gate}"
        response = await self.http_client.request(
            method="PATCH",
            url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
            body=json.dumps(data)
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
        if not await self._check_init_spoolman(): return

        # Use the PATCH method on the spoolman api
        if not silent:
            logging.info(f"Unsetting gate map on spool id {spool_id}")
        data = {'extra': {MMU_NAME_FIELD: json.dumps(""), MMU_GATE_FIELD: json.dumps(-1)}}
        if self.update_location:
            data['location'] = ""
        response = await self.http_client.request(
            method="PATCH",
            url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
            body=json.dumps(data)
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
        if not await self._check_init_spoolman(): return
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
        if not await self._check_init_spoolman(): return
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
        if not await self._check_init_spoolman(): return
        async with self.cache_lock:
            await self._initialize_mmu()

            gate_ids = [(gate, self._find_first_spool_id(self.printer_hostname, gate)) for gate in range(self.nb_gates)]
            return await self._send_gate_map_update(gate_ids, replace=True, silent=silent)

    async def clear_spools_for_printer(self, printer=None, sync=False, silent=False) -> bool:
        '''
        Clears all gates for the printer
        '''
        if not await self._check_init_spoolman(): return
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
        if not await self._check_init_spoolman(): return
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
        if not await self._check_init_spoolman(): return
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



    # Switch out the metadata processor with this module which handles placeholders
    def setup_placeholder_processor(self, config):
        args = " -m" if config.getboolean("enable_file_preprocessor", True) else ""
        args += " -n" if config.getboolean("enable_toolchange_next_pos", True) else ""
        from .file_manager import file_manager
        file_manager.METADATA_SCRIPT = os.path.abspath(__file__) + args

def load_component(config):
    return MmuServer(config)



##################################################################################
#
# Beyond this point this module acts like an extended file_manager/metadata module
#
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
    'SuperSlicer' : r"^;\s*extruder_colour\s*=\s*(#.*;*.*)$",
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
