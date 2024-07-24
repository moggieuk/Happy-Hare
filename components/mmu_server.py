# Happy Hare MMU Software
# Moonraker support for a file-preprocessor that injects MMU metadata into gcode files
#
# Copyright (C) 2023  Kieran Eglin <@kierantheman (discord)>, <kieran.eglin@gmail.com>
#
# Spoolman integration, colors & temp extension
# Copyright (C) 2023  moggieuk#6538 (discord) moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
from __future__ import annotations
import copy
from distutils import version
from distutils.command import check
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
MIN_SM_VER       = version.LooseVersion('0.18.1')

DB_NAMESPACE     = "moonraker"
ACTIVE_SPOOL_KEY = "spoolman.spool_id"

class MmuServer:
    def __init__(self, config: ConfigHelper):
        self.config = config
        self.server = config.get_server()
        self.printer_info = self.server.get_host_info()
        self.spoolman: SpoolManager = self.server.load_component(config, "spoolman", None)
        self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
        self.http_client: HttpClient = self.server.lookup_component("http_client")

        # Full cache of spools and location (printer, gate, attr_dict))
        # Example: {2: ('BigRed', 0), 3: ('BigRed', 3), 5: ('BabyBlue', 0), 6: ('BigRed', 5), 7: ('BigRed', 8)}
        self.spool_location = {}

        # Similar to Happy Hare gate_spool_id array but -1 -> None
        # Example: [2, None, None, 3, 5, 6, None, None, 7]
        self.remote_gate_spool_id = [] # !TODO IMPORTANT this needs to be a dict of arrays indexed by printer_name.  Only local printer is populated by default. Otherwise remote printers won't work
        self.nb_gates = None             # Set by Happy Hare on first call
        self.cache_lock = asyncio.Lock() # Lock to synchronize cache updates

        # Spoolman filament info retrieval functionality and update reporting
        self.server.register_remote_method("spoolman_get_filaments", self.get_filaments)
        self.server.register_remote_method("spoolman_set_gate_map", self.set_gate_map)
        self.server.register_remote_method("spoolman_clear_spools_for_printer", self.clear_spools_for_printer)
        self.server.register_remote_method("spoolman_get_remote_gate_map", self.get_remote_gate_map)
        self.server.register_remote_method("spoolman_refresh", self.refresh_cache)
        self.server.register_remote_method("spoolman_set_spool_gate", self.set_spool_gate)
        self.server.register_remote_method("spoolman_unset_spool_gate", self.unset_spool_gate)
        self.server.register_remote_method("spoolman_get_spool_info", self.display_spool_info)
        self.server.register_remote_method("spoolman_display_spool_location", self.display_spool_location)

        # Replace file_manager/metadata with this file
        self.setup_placeholder_processor(config)

        # Options
        self.update_location = self.config.getboolean("update_spoolman_location", True)

    async def _get_spoolman_version(self) -> version.LooseVersion:
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
            return version.LooseVersion(response.json()['version'])

    async def component_init(self) -> None:
        # Get current printer hostname
        self.printer_hostname = self.printer_info["hostname"]
        spoolman_version = await self._get_spoolman_version()
        if spoolman_version >= MIN_SM_VER:
            # Make sure db has required extra fields
            fields = await self.get_extra_fields("spool")
            if MMU_NAME_FIELD not in fields:
                await self.add_extra_field("spool", field_name="Printer Name", field_key=MMU_NAME_FIELD, field_type="text", default_value="")
            if MMU_GATE_FIELD not in await self.get_extra_fields("spool"):
                await self.add_extra_field("spool", field_name="MMU Gate", field_key=MMU_GATE_FIELD, field_type="integer", default_value=None)
        else :
            logging.error(f"Could not initialize mmu_server component. Spoolman db version too old (found {spoolman_version} < {MIN_SM_VER})")

        # Create cache of spool location from spoolman db for effeciency
        await self.build_spool_location_cache(silent=True)

    # !TODO: implement mainsail/fluidd gui prompts
    async def _log_n_send(self, msg, error=False, prompt=False, silent=False):
        '''
        logs and sends msg to the klipper console
        '''
        logging.info(msg)
        if not silent:
            error_flag = "ERROR=1" if error else ""
            msg = msg.replace("\n", "\\n") # Get through klipper filtering
            await self.klippy_apis.run_gcode(f"MMU_LOG MSG='{msg}' {error_flag}")

    def _initialize_mmu(self, nb_gates):
        '''
        Initialize mmu gate map if not already done

        parameters:
            @param nb_gates: number of gates on the MMU
        returns:
            @return: True if initialized, False otherwise
        '''
        if self.nb_gates:
            return True # Already initialized

        if nb_gates:
            self.nb_gates = nb_gates
            self.remote_gate_spool_id = [None] * self.nb_gates

            # Build remote_gate_spool_id list and establish number of gates
            return self.build_remote_gate_spool_id(nb_gates=nb_gates)
        return False

    async def get_extra_fields(self, entity_type) -> bool:
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
            return [r['name'] for r in response.json()]
        return True

    async def add_extra_field(self, entity_type, field_key, field_name, field_type, default_value) -> bool:
        '''
        Helper to add a new field to the extra field of the spoolman db
        '''
        default_value = json.dumps(default_value) if field_type == 'text' else default_value
        response = await self.http_client.post(
            url=f'{self.spoolman.spoolman_url}/v1/field/{entity_type}/{field_key}',
            body={"name" : field_name, "field_type" : field_type, "default_value" : default_value}
        )
        if response.status_code == 404:
            logging.info(f"'{self.spoolman.spoolman_url}/v1/field/spool/{field_key}' not found")
            return False
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.error(f"Attempt add field {field_name} failed: {err_msg}")
            return False
        logging.info(f"Field {field_name} added to spoolman db for entity type {entity_type}")
        logging.info(f"  -fields: %s", response.json())
        return True

    def get_filament_attr(self, spool_info) -> dict:
        spool_id = spool_info["id"]
        filament = spool_info["filament"]
        material = filament.get('material', '')
        color_hex = filament.get('color_hex', '')[:6] # Strip alpha channel if it exists
        name = filament.get('name', '')
        return {'spool_id': spool_id, 'material': material, 'color': color_hex, 'name': name}

    async def get_filaments(self, gate_ids, silent=False) -> bool:
        '''
        Retrieve filament attributes for list of (gate, spool_id) tuples
        Pass back to Happy Hare with MMU_GATE_MAP call
        '''
        gate_dict = {}
        if self.spoolman:
            for gate_id, spool_id in gate_ids:
                full_url = f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}"
                response = await self.spoolman.http_client.request(method="GET", url=full_url, body=None)
                if response.status_code == 404:
                    logging.error(f"'{self.spoolman.spoolman_url}/v1/spool/{spool_id}' not found")
                    return False
                elif response.has_error():
                    err_msg = self.spoolman._get_response_error(response)
                    logging.info(f"Attempt to fetch spool info failed: {err_msg}")
                    return False
                spool_info = response.json()
                gate_dict[gate_id] = self.get_filament_attr(spool_info)
            try:
                await self.klippy_apis.run_gcode(f"MMU_GATE_MAP MAP=\"{gate_dict}\" QUIET=1")
            except Exception as e:
                await self._log_n_send("Exception running MMU_GATE_MAP gcode: %s" % str(e), silent=silent)
                return False
        return True

    async def refresh_cache(self, nb_gates=None, silent=False) -> bool:
        '''
        Helper to initialy setup cache or refresh cache
        '''
        logging.info(f"PAUL refresh_cache()")
        await self.build_spool_location_cache(silent=silent)

        # Build remote_gate_spool_id list and establish number of gates
        self.build_remote_gate_spool_id(nb_gates=nb_gates)
        return True

    async def build_spool_location_cache(self, silent=False) -> bool:
        '''
        Helper to get all spools and gates assigned to printers from spoolman db and cache them
        '''
        try:
            async with self.cache_lock:
                self.spool_location = {}
                # Fetch all spools
                errors = ""
                reponse = await self.http_client.get(url=f'{self.spoolman.spoolman_url}/v1/spool')
                for spool_info in reponse.json():
                    spool_id = spool_info['id']
                    printer_name = json.loads(spool_info['extra'].get(MMU_NAME_FIELD, None))
                    mmu_gate = int(spool_info['extra'].get(MMU_GATE_FIELD, -1))
                    if printer_name and mmu_gate >= 0:
                        filament_attr = self.get_filament_attr(spool_info)
                        self.spool_location[spool_id] = (printer_name.strip('"'), mmu_gate, filament_attr)

                    # Highlight errors
                    if printer_name and mmu_gate < 0:
                        errors += f"\n  - spool_id {spool_id} has printer {printer_name} but no mmu_gate assigned"
                    if mmu_gate >= 0 and not printer_name:
                        errors += f"\n  - spool_id {spool_id} has mmu_gate {mmu_gate} but no printer assigned"
        except Exception as e:
            await self._log_n_send(f"Failed to retrieve spools from spoolman: {e}", silent=silent)
            return False

        if errors:
            await self._log_n_send(f"Errors found in spoolman db:{errors}", silent=silent)
        return True

    def build_remote_gate_spool_id(self, nb_gates=None, silent=False) -> bool:
        '''
        Helper to build remote gate map for current printer
        '''
        if self.nb_gates is None:
            return False

        # Build remote gate map
        logging.info(f"Building gate map for printer: {self.printer_hostname}")
        self.remote_gate_spool_id = [self._find_spool_id(self.printer_hostname, gate) for gate in range(self.nb_gates)]
        logging.info(f"PAUL *******:\nremote_gate_spool_id={self.remote_gate_spool_id}")
        return True

    # Function to find the spool_location entry with a matching 'printer/gate' or just 'gate' if 'printer' in None
    def _find_spool_id(self, target_printer, target_gate):
        return next((spoolid
                for spoolid, (printer, gate, _) in self.spool_location.items()
                if (target_printer is None or printer == target_printer) and gate == target_gate
            ), None)

    async def set_gate_map(self, gate_ids=None, printer=None, nb_gates=None, silent=False) -> bool:
        '''
        Store the gate map for the printer for a list of (gate, spool_id) tuples
        '''
        if not self._initialize_mmu(nb_gates):
            return False
        logging.info(f"PAUL set_gate_map(gate_ids={gate_ids}, silent={silent})")

        printer_name = printer or self.printer_hostname
        if not gate_ids:
            logging.error("Gate spool id mapping not provided or empty")
            return False

        # We know which spool_id's have changed but we only need persist the last change
        # to spool especially since this is done in parallel writes
        last_occurrence = {}
        cleanup = []
        for gate, spool_id in gate_ids:
            old_sid_for_gate = self._find_spool_id(printer_name, gate)
            if old_sid_for_gate:
                cleanup.append((-1, old_sid_for_gate))
            last_occurrence[gate] = (gate, spool_id)
        gate_ids = cleanup + list(last_occurrence.values())
        logging.info(f"PAUL reduced gate_ids={gate_ids}, cleanup={cleanup}")

        # If setting a full gate map, include updates for "dirty" spool id's
        # that are not going to overwritten
        if len(gate_ids) == self.nb_gates:
            for spool_id, (p_name, gate, _) in self.spool_location.items():
                logging.info(f"PAUL checking spool_id:{spool_id}, p_name:{p_name}, gate:{gate}")
                if p_name == printer_name and not any(s == spool_id for _, s in gate_ids):
                    gate_ids.append((-1, spool_id))
        logging.info(f"PAUL finialized gate_ids={gate_ids}")

        # Write to spoolman db
        tasks = []
        for gate, spool_id in gate_ids:
            if spool_id <= 0:
                tasks.append(self.unset_spool_gate(gate=gate, silent=True))
            elif spool_id > 0 and gate >=0:
                tasks.append(self.set_spool_gate(spool_id, gate, printer=printer_name, silent=silent))
            else:
                tasks.append(self.unset_spool_gate(spool_id=spool_id, silent=True))

        logging.info(f"PAUL BEFORE *******: spool_location={self.spool_location}")
        results = await asyncio.gather(*tasks) # Run in parallel
        logging.info(f"PAUL AFTER *******: spool_location={self.spool_location}")

        # Rebuild remote gate_spool_id map
        self.build_remote_gate_spool_id()
        return True

    async def clear_spools_for_printer(self, printer=None, nb_gates=None, silent=False) -> bool:
        '''
        Clears all gates for the printer
        '''
        if not self._initialize_mmu(nb_gates):
            return False

        printer_name = printer or self.printer_hostname
        logging.info(f"Clearing spool gates for printer: {printer_name}")

        # Write to spoolman db
        tasks = []
        spool_location_copy = self.spool_location.copy() # Async tasks are modifing!
        for spool_id, (p_name, gate, _) in spool_location_copy.items():
            if p_name == printer_name:
                tasks.append(self.unset_spool_gate(spool_id=spool_id))

        logging.info(f"PAUL BEFORE *******: spool_location={self.spool_location}")
        results = await asyncio.gather(*tasks)  # Run in parallel and collect results
        logging.info(f"PAUL AFTER *******: spool_location={self.spool_location}")

        # Rebuild remote gate_spool_id map
        self.build_remote_gate_spool_id()
        self.server.send_event("spoolman:clear_spool_gates", {"printer": printer})
        return True

    async def set_spool_gate(self, spool_id : int | None, gate : int | None, printer=None, nb_gates=None, silent=False) -> bool:
        '''
        Sets the spool in spoolman sb with id=id for the current printer into optional gate number if mmu is enabled.

        parameters:
            @param spool_id: id of the spool to set
            @param gate: optional gate number to set the spool into. If not provided (and not an mmu), the spool will be set into gate 0.
        returns:
            @return: True if successful, False otherwise
        '''
        if not self._initialize_mmu(nb_gates):
            return False
        logging.info(f"PAUL set_spool_gate(spool_id={spool_id}, gate={gate}, silent={silent})")

        printer_name = printer or self.printer_hostname
        current_printer_name, current_gate, _ = self.spool_location.get(spool_id, (None, None, None))

        # Sanity checking...
        if gate is not None and gate < 0:
            await self._log_n_send("Trying to set spool {spool_id} for printer {printer_name} but gate {gate} is invalid.", error=True, silent=silent)
            return False
        if gate is not None and gate > self.nb_gates:
            await self._log_n_send(f"Trying to set spool {spool_id} for printer {printer_name} @ gate {gate} but only {self.nb_gates} gates are available. Please check the spoolman or moonraker [spoolman] setup.", error=True, silent=silent)
            return False
        if gate is None:
            if self.nb_gates:
                await self._log_n_send(f"Trying to set spool {spool_id} for printer {printer_name} but printer has an MMU with {self.nb_gates} gates. Please check the spoolman or moonraker [spoolman] setup.", error=True, silent=silent)
                return False
            gate = 0

        # If another spool was previously assigned to gate on this printer make sure that is cleared
        old_sid_for_gate = self._find_spool_id(printer_name, gate)
        if old_sid_for_gate:
            await self.unset_spool_gate(spool_id=old_sid_for_gate, printer=printer_name, silent=silent)

        # Use the PATCH method on the spoolman api
        logging.info(f"Setting spool_id (spool_id) for printer {printer_name} @ gate {gate}")
        data = {'extra': {MMU_NAME_FIELD: f"\"{printer_name}\"", MMU_GATE_FIELD: gate}}
        if self.update_location:
            data['location'] = f"{printer_name} @ MMU Gate:{gate}"
        response = await self.http_client.request(
            method="PATCH",
            url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
            body=json.dumps(data)
        )
        if response.status_code == 404:
            logging.error(f"'{self.spoolman.spoolman_url}/v1/spool/{spool_id}' not found")
            return False
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.error(f"Attempt to set spool failed: {err_msg}")
            await self._log_n_send(f"Failed to set spool {spool_id} for printer {printer_name}", silent=silent)
            return False

        async with self.cache_lock:
            filament_attr = self.spool_location.get(spool_id, (None, None, None))[2]
            self.spool_location[spool_id] = (printer_name, gate, filament_attr)

        self.server.send_event("spoolman:spoolman_set_spool_gate", {"id": spool_id, "gate": gate})
        await self._log_n_send(f"Spool {spool_id} set for printer {printer_name} @ gate {gate} in spoolman db", silent=silent)
        return True

    async def unset_spool_gate(self, spool_id=None, gate=None, printer=None, nb_gates=None, silent=False) -> bool:
        '''
        Removes the printer + gate allocation in spoolman db for gate or spool_id (if supplied)
        '''
        if not self._initialize_mmu(nb_gates):
            return False
        logging.info(f"PAUL unset_spool_gate(spool_id={spool_id}, gate={gate}, silent={silent})")

        # Sanity checking...
        spool_id = spool_id or self._find_spool_id(None, gate)
        current_printer_name, current_gate, _ = self.spool_location.get(spool_id, (None, None, None))
        if not spool_id:
            await self._log_n_send("Trying to unset spool {spool_id} but not found in cache", error=True, silent=silent)
            return False

        # Use the PATCH method on the spoolman api
        logging.info(f"Unsetting printer {current_printer_name} and gate {current_gate} from spool id {spool_id}")
        data = {'extra': {MMU_NAME_FIELD: "\"\"", MMU_GATE_FIELD: -1}}
        if self.update_location:
            data['location'] = ""
        response = await self.http_client.request(
            method="PATCH",
            url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
            body=json.dumps(data)
        )
        if response.status_code == 404:
            logging.error(f"'{self.spoolman.spoolman_url}/v1/spool/{spool_id}' not found")
            return False
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.error(f"Attempt to unset spool failed: {err_msg}")
            await self._log_n_send(f"Failed to unset spool {spool_id}", error=True, silent=silent)
            return False

        async with self.cache_lock:
            self.spool_location.pop(spool_id, None)

        self.server.send_event("spoolman:unset_spool_gate", {"spool_id": spool_id, "gate": gate})
        await self._log_n_send(f"Spool {spool_id} gate cleared in spoolman db", silent=silent)
        return True

    async def get_remote_gate_map(self, nb_gates=None, silent=False) -> bool:
        '''
        Get all spools assigned to the current printer from spoolman db and map them to gates
        Pass back to Happy Hare with MMU_GATE_MAP call
        '''
        if not self._initialize_mmu(nb_gates):
            return False

        # Refresh the spool_location cache if any filament attributes are missing for spools of interest
        if any(
            self.spool_location[spool_id][2] is None
            for spool_id in self.remote_gate_spool_id
            if spool_id is not None and spool_id in self.spool_location
        ):
            await self.build_spool_location_cache(silent=True)

        # Tell Happy Hare to update it's local gate map
        gate_dict = {}
        for gate, spool_id in enumerate(self.remote_gate_spool_id):
            if spool_id:
                gate_dict[gate] = self.spool_location[spool_id][2] # PAUL KeyError: 3 ??
            else:
                gate_dict[gate] = {'spool_id': -1} # PAUL uset other filament attributes??
        try:
            await self.klippy_apis.run_gcode(f"MMU_GATE_MAP MAP=\"{gate_dict}\" REPLACE=1")
        except Exception as e:
            await self._log_n_send("Exception running MMU_GATE_MAP gcode: %s" % str(e), silent=silent)
            return False
        return True

    async def display_spool_info(self, spool_id: int | None = None):
        '''
        Gets info for active spool id and sends it to the klipper console
        '''
        printer_name = self.printer_hostname
        active = "Spool"

        if not spool_id:
            logging.info("Fetching active spool")
            spool_id = await self.spoolman.database.get_item(DB_NAMESPACE, ACTIVE_SPOOL_KEY, None)
            active = "Active spool"

        if not spool_id:
            msg = "No active spool set and no spool id supplied"
            await self._log_n_send(msg, error=True)
            return False

        spool_info = await self.get_info_for_spool(spool_id)
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
        spool = next((gate for sid, (printer, gate, _) in self.spool_location.items() if spool_id == sid and printer_name == printer), None)
        if spool is not None:
            msg += f"  - Gate: {spool}"
        else:
            msg += f"Spool id {spool_id} is not assigned to this printer!\n"
            msg += f"Run: MMU_SPOOLMAN UPDATE=1 SPOOLID={spool_id} GATE=.."
        await self._log_n_send(msg)
        return True

    async def get_info_for_spool(self, spool_id : int):
        '''
        Gets and returns spool info json for desired spool
        '''
        response = await self.http_client.request(
               method="GET",
               url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
            )
        if response.status_code == 404:
            return {}
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.error(f"Attempt to get spool info failed: {err_msg}")
            return {}
        else:
            logging.info(f"Found Spool id {spool_id} on spoolman instance")
        spool_info = response.json()
        logging.info(f"Spool info: {spool_info}")
        return spool_info

    async def display_spool_location(self, printer=None):
        printer_name = printer or self.printer_hostname
        filtered = sorted(
            ((spool_id, gate) for spool_id, (printer, gate, _) in self.spool_location.items() if printer == printer_name),
            key=lambda x: x[1]
        )
        if filtered:
            msg = f"Gate assignment for printer: {printer_name}\n"
            msg += "Gate | Spool ID\n"
            msg += "-----+---------\n"
            for spool_id, gate in filtered:
                msg += f"{gate:<5}| {spool_id:<8}\n"
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
AUTHORZIED_SLICERS = ['PrusaSlicer', 'SuperSlicer', 'OrcaSlicer']

HAPPY_HARE_FINGERPRINT = "; processed by HappyHare"
MMU_REGEX = r"^" + HAPPY_HARE_FINGERPRINT
SLICER_REGEX = r"^;.*generated by ([a-z]*) .*$"

TOOL_DISCOVERY_REGEX = r"((^MMU_CHANGE_TOOL(_STANDALONE)? .*?TOOL=)|(^T))(?P<tool>\d{1,2})"
METADATA_TOOL_DISCOVERY = "!referenced_tools!"

# PS/SS uses "extruder_colour", Orca uses "filament_colour" but extruder_colour can exist with empty or single color
COLORS_REGEX = {
    'PrusaSlicer' : r"^;\s*extruder_colour\s*=\s*(#.*;.*)$",
    'SuperSlicer' : r"^;\s*extruder_colour\s*=\s*(#.*;.*)$",
    'OrcaSlicer' : r"^;\s*filament_colour\s*=\s*(#.*;.*)$"
}
METADATA_COLORS = "!colors!"

TEMPS_REGEX = r"^;\s*(nozzle_)?temperature\s*=\s*(.*)$" # Orca Slicer has the 'nozzle_' prefix, others might not
METADATA_TEMPS = "!temperatures!"

MATERIALS_REGEX = r"^;\s*filament_type\s*=\s*(.*)$"
METADATA_MATERIALS = "!materials!"

PURGE_VOLUMES_REGEX = r"^;\s*(flush_volumes_matrix|wiping_volumes_matrix)\s*=\s*(.*)$" # flush.. in Orca, wiping... in PS
METADATA_PURGE_VOLUMES = "!purge_volumes!"

FILAMENT_NAMES_REGEX = r"^;\s*(filament_settings_id)\s*=\s*(.*)$"
METADATA_FILAMENT_NAMES = "!filament_names!"

# Detection for next pos processing
T_PATTERN  = r'^T(\d+)$'
G1_PATTERN = r'^G[01](?:\s+X([\d.]*)|\s+Y([\d.]*))+.*$'

def gcode_processed_already(file_path):
    """Expects first line of gcode to be the HAPPY_HARE_FINGERPRINT '; processed by HappyHare'"""

    mmu_regex = re.compile(MMU_REGEX, re.IGNORECASE)

    with open(file_path, 'r') as in_file:
        line = in_file.readline()
        return mmu_regex.match(line)

def parse_gcode_file(file_path):
    slicer_regex = re.compile(SLICER_REGEX, re.IGNORECASE)
    has_tools_placeholder = has_colors_placeholder = has_temps_placeholder = has_materials_placeholder = has_purge_volumes_placeholder = filament_names_placeholder = False
    found_colors = found_temps = found_materials = found_purge_volumes = found_filament_names = False
    slicer = None

    tools_used = set()
    colors = []
    temps = []
    materials = []
    purge_volumes = []
    filament_names = []

    with open(file_path, 'r') as in_file:
        for line in in_file:
            # Discover slicer
            if not slicer and line.startswith(";"):
                match = slicer_regex.match(line)
                if match:
                    slicer = match.group(1)
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
        with open(file_path, 'r') as in_file:
            for line in in_file:
                # !referenced_tools! processing
                if not has_tools_placeholder and METADATA_TOOL_DISCOVERY in line:
                    has_tools_placeholder = True

                match = tools_regex.match(line)
                if match:
                    tool = match.group("tool")
                    tools_used.add(int(tool))

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

                # !purge_volumes! processing
                if not has_purge_volumes_placeholder and METADATA_PURGE_VOLUMES in line:
                    has_purge_volumes_placeholder = True

                if not found_purge_volumes:
                    match = purge_volumes_regex.match(line)
                    if match:
                        purge_volumes_csv = match.group(2).strip().split(',')
                        purge_volumes.extend(purge_volumes_csv)
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

    return (has_tools_placeholder or has_colors_placeholder or has_temps_placeholder or has_materials_placeholder or has_purge_volumes_placeholder or filament_names_placeholder,
            sorted(tools_used), colors, temps, materials, purge_volumes, filament_names, slicer)

def process_file(input_filename, output_filename, insert_nextpos, tools_used, colors, temps, materials, purge_volumes, filament_names):

    t_pattern = re.compile(T_PATTERN)
    g1_pattern = re.compile(G1_PATTERN)

    with open(input_filename, 'r') as infile, open(output_filename, 'w') as outfile:
        buffer = [] # Buffer lines between a "T" line and the next matching "G1" line
        tool = None # Store the tool number from a "T" line
        outfile.write(f'{HAPPY_HARE_FINGERPRINT}\n')

        for line in infile:
            line = add_placeholder(line, tools_used, colors, temps, materials, purge_volumes, filament_names)
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

def add_placeholder(line, tools_used, colors, temps, materials, purge_volumes, filament_names):
    # Ignore comment lines to preserve slicer metadata comments
    if not line.startswith(";"):
        if METADATA_TOOL_DISCOVERY in line:
            if tools_used:
                line = line.replace(METADATA_TOOL_DISCOVERY, ",".join(map(str, tools_used)))
            else:
                line = line.replace(METADATA_TOOL_DISCOVERY, "0")
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
                    has_placeholder, tools_used, colors, temps, materials, purge_volumes, filament_names, slicer = parse_gcode_file(file_path)
                    metadata.logger.info("Reading placeholders took %.2fs. Detected gcode by slicer: %s" % (time.time() - start, slicer))
                else:
                    tools_used = colors = temps = materials = purge_volumes = filament_names = slicer = None
                    has_placeholder = False

                if (insert_nextpos and tools_used is not None and len(tools_used) > 0) or has_placeholder:
                    start = time.time()
                    msg = []
                    if has_placeholder:
                        msg.append("Writing MMU placeholders")
                    if insert_nextpos:
                        msg.append("Inserting next position to tool changes")
                    process_file(file_path, tmp_file, insert_nextpos, tools_used, colors, temps, materials, purge_volumes, filament_names)
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
    parser.add_argument("-f", "--filename", metavar='<filename>', help="name gcode file to parse")
    parser.add_argument("-p", "--path", default=os.path.abspath(os.path.dirname(__file__)), metavar='<path>', help="optional absolute path for file")
    parser.add_argument("-u", "--ufp", metavar="<ufp file>", default=None, help="optional path of ufp file to extract")
    parser.add_argument("-o", "--check-objects", dest='check_objects', action='store_true', help="process gcode file for exclude object functionality")
    parser.add_argument("-m", "--placeholders", dest='placeholders', action='store_true', help="process happy hare mmu placeholders")
    parser.add_argument("-n", "--nextpos", dest='nextpos', action='store_true', help="add next position to tool change")
    args = parser.parse_args()
    check_objects = args.check_objects
    enabled_msg = "enabled" if check_objects else "disabled"
    metadata.logger.info(f"Object Processing is {enabled_msg}")

    # Original metadata parser
    metadata.main(args.path, args.filename, args.ufp, check_objects)

    # Second parsing for mmu placeholders and next pos insertion
    main(args.path, args.filename, args.placeholders, args.nextpos)
