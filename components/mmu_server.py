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

CONSOLE_TAB = '\u00A0' * 3 # Special space characters used as they will be displayed in gcode console

class MmuServer:
    def __init__(self, config: ConfigHelper):
        self.config = config
        self.server = config.get_server()
        self.printer_info = self.server.get_host_info()
        self.spoolman: SpoolManager = self.server.load_component(config, "spoolman", None)
        self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
        self.http_client: HttpClient = self.server.lookup_component("http_client")
        self.nb_gates = None # Set by Happy Hare when calling remote_gate_map
        self.machine_occupation = {}
        self.gate_occupation = {}

        # Spoolman filament info retrieval functionality and update reporting
        self.server.register_remote_method(
            "spoolman_get_filaments", self.get_filaments
        )
        self.server.register_remote_method(
            "spoolman_set_active_gate", self.set_active_gate
        )
        self.server.register_remote_method(
            "spoolman_set_gate_map", self.set_gate_map
        )

        # Additional remote methods not yet directly called by Happy Hare
        self.server.register_remote_method(
            "spoolman_get_spool_info", self.get_spool_info
        )
        self.server.register_remote_method(
            "spoolman_remote_gate_map", self.remote_gate_map
        )
        self.server.register_remote_method(
            "spoolman_set_spool_gate", self.set_spool_gate
        )
        self.server.register_remote_method(
            "spoolman_unset_spool_gate", self.unset_spool_gate
        )
        self.server.register_remote_method(
            "spoolman_clear_spools_for_machine", self.clear_spools_for_machine
        )

        self.setup_placeholder_processor(config) # Replaces file_manager/metadata with this file

    async def component_init(self) -> None:
        # Just refetch gate_occupation from spoolman db
        await self.remote_gate_map(silent=True, dump=False)

    async def _log_n_send(self, msg, prompt=False):
        ''' logs and sends msg to the klipper console'''
        logging.error(msg)
        # !TODO: implement mainsail/fluidd gui prompts
        await self.klippy_apis.run_gcode(f"M118 {msg}", None)

    async def _get_active_spool(self):
        spool_id = await self.spoolman.database.get_item(
            DB_NAMESPACE, ACTIVE_SPOOL_KEY, None
        )
        return spool_id

    async def unset_spool_id(self, spool_id: int) -> bool:
        '''
        Removes the machine + gate allocation in spoolman db for spool_id

        parameters:
            @param spool_id: id of the spool to set
        returns:
            @return: True if successful, False otherwise
        '''
        if spool_id is None:
            await self._log_n_send("Trying to unset spool but no spool id provided.")
            return False

        # use the PATCH method on the spoolman api
        # get current printer hostname
        machine_hostname = self.printer_info["hostname"]
        logging.info(
            f"Unsetting spool if {spool_id} for machine: {machine_hostname}")
        try:
            response = await self.http_client.request(
                method="PATCH",
                url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
                body=json.dumps({"machine_name": ""}),
            )
            if response.status_code == 404:
                logging.error(f"'{self.spoolman.spoolman_url}/v1/spool/{spool_id}' not found")
                return False
            response = await self.http_client.request(
                method="PATCH",
                url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
                body=json.dumps({"extra": {"mmu_gate_map": -1}}),
            )
            if response.status_code == 404:
                logging.error(f"'{self.spoolman.spoolman_url}/v1/spool/{spool_id}' not found")
                return False
        except Exception as e:
            logging.error(
                f"Failed to unset spool {spool_id} for machine {machine_hostname}: {e}")
            await self._log_n_send(f"Failed to unset spool {spool_id} for machine {machine_hostname}")
            return False
        await self.remote_gate_map(silent=True, dump=False)
        return True

    async def get_info_for_spool(self, spool_id : int):
        response = await self.http_client.request(
                method="GET",
                url=f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}",
            )
        if response.status_code == 404:
            await self._log_n_send(f"Spool ID {spool_id} not found")
            return {}
        elif response.has_error():
            err_msg = self.spoolman._get_response_error(response)
            logging.info(f"Attempt to get spool info failed: {err_msg}")
            return {}
        else:
            logging.info(f"Found Spool ID {spool_id} on spoolman instance")

        spool_info = response.json()
        logging.info(f"Spool info: {spool_info}")

        return spool_info

    async def get_extra_fields(self, entity_type):
        '''
        Gets all extra fields for the entity type
        '''
        response = await self.http_client.get(
            url=f'{self.spoolman.spoolman_url}/v1/field/{entity_type}',
        )
        if response.status_code == 404:
            logging.info(f"'{self.spoolman.spoolman_url}/v1/field/{entity_type}' not found")
            return False
        elif response.has_error():
            err_msg = self._get_response_error(response)
            logging.info(f"Attempt to get extra fields failed: {err_msg}")
            await self._log_n_send("Failed to get extra fields")
            return False
        else:
            logging.info(f"Extra fields for {entity_type} found")
            return [ r['name'] for r in response.json()]

    async def add_extra_field(self, entity_type, field_name, type, default_value):
        '''
        Adds a new field to the extra field of the spoolman db
        '''
        default_value = json.dumps(default_value) if type == 'text' else default_value
        response = await self.http_client.post(
            url=f'{self.spoolman.spoolman_url}/v1/field/{entity_type}/{field_name}',
            body={"name" : field_name, "field_type" : type, "default_value" : default_value}
        )
        if response.status_code == 404:
            logging.info(f"'{self.spoolman.spoolman_url}/v1/field/spool/{field_name}' not found")
            return False
        elif response.has_error():
            err_msg = self._get_response_error(response)
            logging.info(f"Attempt add field {field_name} failed: {err_msg}")
            await self._log_n_send(f"Failed to set field {field_name}")
            return False
        logging.info(f"Field {field_name} added to spoolman db for entity type {entity_type}")
        logging.info(f"{CONSOLE_TAB}-fields : %s", response.json())
        return True

    # Logic to provide spoolman integration.
    # Leverage configuration from Spoolman component
    async def get_filaments(self, gate_ids):
        gate_dict = {}
        if self.spoolman:
            for gate_id, spool_id in gate_ids:
                full_url = f"{self.spoolman.spoolman_url}/v1/spool/{spool_id}"

                response = await self.spoolman.http_client.request(method="GET", url=full_url, body=None)
                if not response.has_error():
                    record = response.json()
                    filament = record["filament"]

                    material = filament.get('material', '')
                    color_hex = filament.get('color_hex', '')[:6] # Strip alpha channel if it exists

                    gate_dict[gate_id] = {'spool_id': spool_id, 'material': material, 'color': color_hex, 'name': filament.get('name', '')}
                elif response.status_code != 404:
                    response.raise_for_status()
            try:
                await self.klippy_apis.run_gcode(f"MMU_GATE_MAP MAP=\"{gate_dict}\" QUIET=1")
            except self.server.error as e:
                logging.info("mmu_server: Exception running MMU gcode: %s" % str(e))

        return gate_dict

    def setup_placeholder_processor(self, config):
        # Switch out the metadata processor with this module which handles placeholders
        args = " -m" if config.getboolean("enable_file_preprocessor", True) else ""
        args += " -n" if config.getboolean("enable_toolchange_next_pos", True) else ""
        from .file_manager import file_manager
        file_manager.METADATA_SCRIPT = os.path.abspath(__file__) + args

    async def set_active_gate(self, gate = None) -> None:
        '''
        Search for spool id matching the gate number and set it as active
        '''
        if gate is None:
            logging.error("Gate number not provided")
            return

        for spool in self.gate_occupation:
            logging.info(
                f"found spool: {spool['filament']['name']} at gate {spool['extra']['mmu_gate_map']}")
            if spool['extra']['mmu_gate_map'] == gate:
                self.spoolman.set_active_spool(spool['id'])
                return

        logging.error(f"Could not find a matching spool for gate {gate}")

    async def get_spool_info(self, sid: int = None):
        '''
        Gets info for active spool id and sends it to the klipper console
        '''
        if not sid:
            logging.info("Fetching active spool")
            spool_id = await self._get_active_spool()
        else:
            logging.info("Setting spool id: %s", sid)
            spool_id = sid
        self.server.send_event(
            "spoolman:get_spool_info", {"id": spool_id}
        )
        if not spool_id:
            msg = "No active spool set"
            await self._log_n_send(msg)
            return False

        spool_info = await self.get_info_for_spool(spool_id)
        if not spool_info:
            msg = f"Spool id {spool_id} not found"
            await self._log_n_send(msg)
            return False
        msg = f"Active spool is: {spool_info['filament']['name']} (id : {spool_info['id']})"
        await self._log_n_send(msg)
        msg = f"{CONSOLE_TAB}- used: {int(spool_info['used_weight'])} g"
        await self._log_n_send(msg)
        msg = f"{CONSOLE_TAB}- remaining: {int(spool_info['remaining_weight'])} g"
        await self._log_n_send(msg)
        # if spool_id not in filament_gates:
        found = False
        for spool in self.gate_occupation:
            if int(spool['id']) == spool_id:
                msg = "{}- gate: {}".format(CONSOLE_TAB, spool['extra']['mmu_gate_map'])
                await self._log_n_send(msg)
                found = True
        if not found:
            msg = f"Spool id {spool_id} is not assigned to this machine"
            await self._log_n_send(msg)
            msg = "Run :"
            await self._log_n_send(msg)
            msg = f"{CONSOLE_TAB}SET_SPOOL_GATE ID={spool_id} GATE=integer"
            await self._log_n_send(msg)
            return False

    async def remote_gate_map(self, nb_gates=None, silent=False, dump=True) -> List[Dict[str, Any]]:
        '''
        Get all spools assigned to the current machine from spoolman db and set them in the gates
        '''
        if nb_gates is not None:
            self.nb_gates = nb_gates
            self.gate_occupation = [None for __ in range(self.nb_gates)]
        # Get current printer hostname
        machine_hostname = self.printer_info["hostname"]
        logging.info(f"Getting spools for machine: {machine_hostname}")
        try:
            spools = []
            reponse = await self.http_client.get(
                url=f'{self.spoolman.spoolman_url}/v1/spool',
            )
            for spool in reponse.json():
                if 'extra' in spool and 'machine_name' in spool['extra'] and json.loads(spool['extra']['machine_name']) == machine_hostname:
                    tmp_spool = copy.deepcopy(spool)
                    tmp_spool['extra']['machine_name'] = json.loads(tmp_spool['extra']['machine_name'])
                    tmp_spool['extra']['mmu_gate_map'] = int(tmp_spool['extra']['mmu_gate_map'])
                    spools.append(tmp_spool)
        except Exception as e:
            if not silent:
                await self._log_n_send(f"Failed to retrieve spools from spoolman: {e}")
            return []
        self.machine_occupation = spools
        if nb_gates is not None:
            if self.nb_gates < len(spools):
                if not silent:
                    await self._log_n_send(f"Number of spools assigned to machine {machine_hostname} is greater than the number of gates available on the machine. Please check the spoolman or moonraker [spoolman] setup.")
                return []
        if not silent and not spools:
            await self._log_n_send(f"No spools assigned to machine: {machine_hostname}")
            return []
        if self.machine_occupation:
            if not silent:
                await self._log_n_send("Spools for machine:")
            # Create a table of size len(spools)
            for spool in self.machine_occupation:
                gate = None
                if 'mmu_gate_map' in spool['extra']:
                    gate = int(spool['extra']['mmu_gate_map'])
                if gate is None:
                    if not silent:
                        await self._log_n_send(f"'mmu_gate_map' extra field for {spool['filament']['name']} @ {spool['id']} in spoolman db seems to not be set. Please check the spoolman setup.")
                else:
                    self.gate_occupation[int(gate)] = spool
            if not silent:
                for i, spool in enumerate(self.gate_occupation):
                    if spool:
                        await self._log_n_send(f"{CONSOLE_TAB}{i} : {spool['filament']['name']}")
                    else:
                        await self._log_n_send(f"{CONSOLE_TAB}{i} : empty")
        if dump:
            gate_dict = {}
            for i, spool in enumerate(self.gate_occupation):
                if spool:
                    gate_dict[i] = {
                                        'spool_id': spool['id'],
                                        'material': spool['filament']['material'],
                                        'color': spool['filament']['color_hex'][:6],
                                        'name': spool['filament']['name']
                                    }
            await self.klippy_apis.run_gcode("MMU_GATE_MAP MAP=\"{}\" QUIET=1 SYNC=0".format(gate_dict))
            if not silent:
                await self.klippy_apis.run_gcode("MMU_GATE_MAP")
        return True

    async def set_spool_gate(self, spool_id : int | None, gate : int | None) -> bool:
        '''
        Sets the spool with id=id for the current machine into optional gate number if mmu is enabled.

        parameters:
            @param spool_id: id of the spool to set
            @param gate: optional gate number to set the spool into. If not provided (and number of gates = 1), the spool will be set into gate 0.
        returns:
            @return: True if successful, False otherwise
        '''
        await self.remote_gate_map(silent=True, dump=False)
        if spool_id is None:
            msg = "Trying to set spool but no spool id provided."
            await self._log_n_send(msg)
            return False

        logging.info(f"Setting spool {spool_id} for machine: {self.printer_info['hostname']} @ gate {gate}")
        self.server.send_event("spoolman:spoolman_set_spool_gate", {"id": spool_id, "gate": gate})
        # Check that gate not higher than number of gates available
        if (gate is None) and (self.nb_gates > 1):
            msg = f"Trying to set spool {spool_id} for machine {self.printer_info['hostname']} but no gate number provided."
            await self._log_n_send(msg)
            return False
        elif not gate and (self.nb_gates == 1):
            gate = 0
        elif gate > self.nb_gates - 1:
            msg = f"Trying to set spool {spool_id} for machine {self.printer_info['hostname']} @ gate {gate} but only {self.nb_gates} gates are available. Please check the spoolman or moonraker [spoolman] setup."
            await self._log_n_send(msg)
            return False

        # First check if the spool is not already assigned to a machine
        spool_info = await self.get_info_for_spool(spool_id)
        if not spool_info:
            return False
        mmu_gate_map = None
        machine_name = None
        extra = {}
        if 'extra' in spool_info:
            extra = copy.deepcopy(spool_info['extra'])
            mmu_gate_map = extra.get('mmu_gate_map', None)
            machine_name = extra.get('machine_name', None)
            if machine_name is None:
                if 'machine_name' not in await self.get_extra_fields("spool"):
                    await self.add_extra_field("spool", "machine_name", type="text", default_value="")
            if mmu_gate_map is None:
                if 'mmu_gate_map' not in await self.get_extra_fields("spool"):
                    await self.add_extra_field("spool", "mmu_gate_map", type="integer", default_value=-1)
            if mmu_gate_map:
                mmu_gate_map = int(mmu_gate_map)
            if machine_name:
                machine_name = json.loads(machine_name)

        identical = False
        if machine_name:
            if mmu_gate_map != -1 and mmu_gate_map is not None:
                # If the spool is already assigned to current machine
                if machine_name == self.printer_info["hostname"]:
                    await self._log_n_send(f"Spool {spool_info['filament']['name']} (id: {spool_id}) is already assigned to this machine @ gate {mmu_gate_map}")
                    if mmu_gate_map == gate:
                        identical = True
                        await self._log_n_send(f"{CONSOLE_TAB}No update needed for spool {spool_info['filament']['name']} (id: {spool_id}) @ gate {gate}")
                    else:
                        await self._log_n_send(f"{CONSOLE_TAB}Updating gate for spool {spool_info['filament']['name']} (id: {spool_id}) to gate {gate}")
            # If the spool is already assigned to another machine
            else:
                await self._log_n_send(f"Spool {spool_info['filament']['name']} (id: {spool_id}) is already assigned to another machine: {machine_name}")
                return False

        # Then check that no spool is already assigned to the gate of this machine (if not previously identified as the same spool)
        if not identical:
            for g, spool in enumerate(self.gate_occupation):
                if spool:
                    if g == gate:
                        await self._log_n_send(f"Gate {gate} is already assigned to spool {spool['filament']['name']} (id: {spool['id']})")
                        await self._log_n_send(f"{CONSOLE_TAB}- Overwriting gate assignment")
                        if not await self.unset_spool_id(spool['id']):
                            await self._log_n_send(f"{CONSOLE_TAB*2}Failed to unset spool {spool['filament']['name']} (id: {spool['id']}) from gate {gate}")
                            return False
                        await self._log_n_send(f"{CONSOLE_TAB*2}Spool {spool['filament']['name']} (id: {spool['id']}) unset from gate {gate}")

        # Check if spool is not allready archived
        if spool_info['archived']:
            msg = f"Spool {spool_id} is archived. Please check the spoolman setup."
            await self._log_n_send(msg)
            return False

        # Use the PATCH method on the spoolman api
        # Get current printer hostname
        machine_hostname = self.printer_info["hostname"]
        logging.info(f"Setting spool {spool_info['filament']['name']} (id: {spool_info['id']}) for machine: {machine_hostname} @ gate {gate}")
        # Get spool info from spoolman
        extra.update({
            "machine_name" : f"\"{machine_hostname}\"",
            "mmu_gate_map" : gate
        })
        response = await self.http_client.request(
            method='PATCH',
            url=f'{self.spoolman.spoolman_url}/v1/spool/{spool_id}',
            body=json.dumps({"extra" : extra}),
        )
        if response.status_code == 404:
            logging.info(f"'{self.spoolman.spoolman_url}/v1/spool/{spool_id}/extra' not found")
            return False
        elif response.has_error():
            err_msg = self._get_response_error(response)
            logging.info(f"Attempt to set spool failed: {err_msg}")
            await self._log_n_send(f"Failed to set spool {spool_id} for machine {machine_hostname}")
            return False
        await self._log_n_send(f"Spool {spool_id} set for machine {machine_hostname} @ gate {gate} in spoolman db")
        await self.remote_gate_map(silent=True, dump=False)
        if gate == 0 and (self.nb_gates == 1):
            await self.set_active_gate(gate)
            await self._log_n_send(f"{CONSOLE_TAB*2}Setting gate 0 as active (single gate machine)")
        return True

    async def unset_spool_gate(self, gate: int = 0) -> bool:
        '''
        Unsets the gate number for the current machine
        '''
        # Get spools assigned to current machine
        if self.gate_occupation not in [False, None]:
            for spool in self.gate_occupation:
                if spool:
                    if 'mmu_gate_map' in spool['extra'] and spool['extra']['mmu_gate_map'] == gate:
                        logging.info(f"Clearing gate {gate} for machine: {self.printer_info['hostname']}")
                        self.server.send_event("spoolman:unset_spool_gate", {"gate": gate})
                        await self.unset_spool_id(spool['id'])
                        await self._log_n_send(f"Gate {gate} cleared")
                        return True
            await self._log_n_send(f"No spool assigned to gate {gate}")
            return False
        else:
            msg = "No spools found for this machine"
            await self._log_n_send(msg)
            return False

    async def set_gate_map(self, gate_map) -> bool:
        '''
        Sets the gate map for the current machine
        '''
        if gate_map is None:
            logging.error("Gate map not provided")
            return False
        for gate, spool_id in enumerate(gate_map):
            if (spool_id == -1 or spool_id == 0) and self.gate_occupation[gate] is not None:
                await self.unset_spool_gate(gate)
            elif spool_id != -1 and spool_id != 0:
                if (self.gate_occupation[gate] and self.gate_occupation[gate]['id'] != spool_id) or self.gate_occupation[gate] is None:
                    await self.set_spool_gate(spool_id, gate)
        return

    async def clear_spools_for_machine(self) -> bool:
        '''
        Clears all gates for the current machine
        '''
        logging.info(f"Clearing spool gates for machine: {self.printer_info['hostname']}")
        self.server.send_event("spoolman:clear_spool_gates", {})
        # Get spools assigned to current machine
        for i, __ in enumerate(self.gate_occupation):
            await self.unset_spool_gate(i)
        else:
            msg = f"No spools for machine {self.printer_info['hostname']}"
            await self._log_n_send(msg)
            return False
        return True

def load_component(config):
    return MmuServer(config)



#
# Beyond this point this module acts like an extended file_manager/metadata module
#

MMU_REGEX = r"^; processed by HappyHare"
SLICER_REGEX = r"^;.*generated by ([a-z]*) .*$"

TOOL_DISCOVERY_REGEX = r"((^MMU_CHANGE_TOOL(_STANDALONE)? .*?TOOL=)|(^T))(?P<tool>\d{1,2})"
METADATA_TOOL_DISCOVERY = "!referenced_tools!"

# PS/SS uses "extruder_colour", Orca uses "filament_colour" but extruder_colour can exist with empty or single color
COLORS_REGEX = r"^;\s*(?:extruder_colour|filament_colour)\s*=\s*(#.*;.*)$"
METADATA_COLORS = "!colors!"

TEMPS_REGEX = r"^;\s*(nozzle_)?temperature\s*=\s*(.*)$" # Orca Slicer has the 'nozzle_' prefix, others might not
METADATA_TEMPS = "!temperatures!"

MATERIALS_REGEX = r"^;\s*filament_type\s*=\s*(.*)$"
METADATA_MATERIALS = "!materials!"

PURGE_VOLUMES_REGEX = r"^;\s*(flush_volumes_matrix|wiping_volumes_matrix)\s*=\s*(.*)$" # flush.. in Orca, wiping... in PS
METADATA_PURGE_VOLUMES = "!purge_volumes!"

FILAMENT_USED_REGEX = r"^;\s*(filament\sused\s\[g\])\s*=\s*(.*)$" # flush.. in Orca, wiping... in PS
METADATA_FILAMENT_USED = "!filament_used!"

FILAMENT_NAMES_REGEX = r"^;\s*(filament_settings_id)\s*=\s*(.*)$"
METADATA_FILAMENT_NAMES = "!filament_names!"

# Detection for next pos processing
T_PATTERN  = r'^T(\d+)$'
G1_PATTERN = r'^G[01](?:\s+X([\d.]*)|\s+Y([\d.]*))+.*$'

def gcode_processed_already(file_path):
    """Expects first line of gcode to be '; processed by HappyHare'"""

    mmu_regex = re.compile(MMU_REGEX, re.IGNORECASE)

    with open(file_path, 'r') as in_file:
        line = in_file.readline()
        return mmu_regex.match(line)

def parse_gcode_file(file_path):
    slicer_regex = re.compile(SLICER_REGEX, re.IGNORECASE)
    tools_regex = re.compile(TOOL_DISCOVERY_REGEX, re.IGNORECASE)
    colors_regex = re.compile(COLORS_REGEX, re.IGNORECASE)
    temps_regex = re.compile(TEMPS_REGEX, re.IGNORECASE)
    materials_regex = re.compile(MATERIALS_REGEX, re.IGNORECASE)
    purge_volumes_regex = re.compile(PURGE_VOLUMES_REGEX, re.IGNORECASE)
    filament_used_regex = re.compile(FILAMENT_USED_REGEX, re.IGNORECASE)
    filament_names_regex = re.compile(FILAMENT_NAMES_REGEX, re.IGNORECASE)

    has_tools_placeholder = has_colors_placeholder = has_temps_placeholder = has_materials_placeholder = has_purge_volumes_placeholder = has_filament_used_placeholder = filament_names_placeholder = False
    found_colors = found_temps = found_materials = found_purge_volumes = found_filament_used = found_filament_names = False
    slicer = None

    tools_used = set()
    colors = []
    temps = []
    materials = []
    purge_volumes = []
    filament_used = []
    filament_names = []

    with open(file_path, 'r') as in_file:
        for line in in_file:
            # Discover slicer
            if not slicer and line.startswith(";"):
                match = slicer_regex.match(line)
                if match:
                    slicer = match.group(1).lower()
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

            # !filament_used! processing
            if not has_filament_used_placeholder and METADATA_FILAMENT_USED in line:
                has_filament_used_placeholder = True

            if not found_filament_used:
                match = filament_used_regex.match(line)
                if match:
                    filament_used_csv = [e.strip() for e in match.group(2).strip().split(',')]
                    filament_used.extend(filament_used_csv)
                    found_filament_used = True

            # !filament_names! processing
            if not filament_names_placeholder and METADATA_FILAMENT_NAMES in line:
                filament_names_placeholder = True

            if not found_filament_names:
                match = filament_names_regex.match(line)
                if match:
                    filament_names_csv = [e.strip() for e in re.split(',|;', match.group(2).strip())]
                    filament_names.extend(filament_names_csv)
                    found_filament_names = True

    return (has_tools_placeholder or has_colors_placeholder or has_temps_placeholder or has_materials_placeholder or has_purge_volumes_placeholder or has_filament_used_placeholder or filament_names_placeholder,
            sorted(tools_used), colors, temps, materials, purge_volumes, filament_used, filament_names, slicer)

def process_file(input_filename, output_filename, insert_nextpos, tools_used, colors, temps, materials, purge_volumes, filament_used, filament_names):

    t_pattern = re.compile(T_PATTERN)
    g1_pattern = re.compile(G1_PATTERN)

    with open(input_filename, 'r') as infile, open(output_filename, 'w') as outfile:
        buffer = [] # Buffer lines between a "T" line and the next matching "G1" line
        tool = None # Store the tool number from a "T" line
        outfile.write('; processed by HappyHare\n')

        for line in infile:
            line = add_placeholder(line, tools_used, colors, temps, materials, purge_volumes, filament_used, filament_names)
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

def add_placeholder(line, tools_used, colors, temps, materials, purge_volumes, filament_used, filament_names):
    # Ignore comment lines to preserve slicer metadata comments
    if not line.startswith(";"):
        if METADATA_TOOL_DISCOVERY in line:
            line = line.replace(METADATA_TOOL_DISCOVERY, ",".join(map(str, tools_used)))
        if METADATA_COLORS in line:
            line = line.replace(METADATA_COLORS, ",".join(map(str, colors)))
        if METADATA_TEMPS in line:
            line = line.replace(METADATA_TEMPS, ",".join(map(str, temps)))
        if METADATA_MATERIALS in line:
            line = line.replace(METADATA_MATERIALS, ",".join(map(str, materials)))
        if METADATA_PURGE_VOLUMES in line:
            line = line.replace(METADATA_PURGE_VOLUMES, ",".join(map(str, purge_volumes)))
        if METADATA_FILAMENT_USED in line:
            line = line.replace(METADATA_FILAMENT_USED, ",".join(map(str, filament_used)))
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
                    has_placeholder, tools_used, colors, temps, materials, purge_volumes, filament_used, filament_names, slicer = parse_gcode_file(file_path)
                    metadata.logger.info("Reading placeholders took %.2fs. Detected gcode by slicer: %s" % (time.time() - start, slicer))
                else:
                    tools_used = colors = temps = materials = purge_volumes = filament_used = filament_names = slicer = None
                    has_placeholder = False

                if (insert_nextpos and tools_used is not None and len(tools_used) > 0) or has_placeholder:
                    start = time.time()
                    msg = []
                    if has_placeholder:
                        msg.append("Writing MMU placeholders")
                    if insert_nextpos:
                        msg.append("Inserting next position to tool changes")
                    process_file(file_path, tmp_file, insert_nextpos, tools_used, colors, temps, materials, purge_volumes, filament_used, filament_names)
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
