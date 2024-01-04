# Happy Hare MMU Software
# Moonraker support for a file-preprocessor that injects MMU metadata into gcode files
#
# Copyright (C) 2023  Kieran Eglin <@kierantheman (discord)>, <kieran.eglin@gmail.com>
#
# Spoolman integration:
# Copyright (C) 2023  moggieuk#6538 (discord) moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging, os, re, fileinput

class MmuServer:
    TOOL_DISCOVERY_REGEX = r"((^MMU_CHANGE_TOOL(_STANDALONE)? .*?TOOL=)|(^T))(?P<tool>\d{1,2})"
    METADATA_REPLACEMENT_STRING = "!referenced_tools!"

    def __init__(self, config):
        self.config = config
        self.server = config.get_server()

        # Gcode prerocessing functionality
        self.file_manager = self.server.lookup_component("file_manager")
        self.enable_file_preprocessor = config.getboolean("enable_file_preprocessor", True)
        self.server.register_event_handler("file_manager:filelist_changed", self._filelist_changed)

        # Spoolman filament info retrieval functionality and update reporting
        self.server.register_remote_method("spoolman_get_filaments", self.get_filaments)

    def _filelist_changed(self, response):
        if not self.enable_file_preprocessor:
            return

        if response["action"] == "create_file":
            filepath = os.path.join(self.file_manager.get_directory(), response["item"]["path"])

            if filepath.endswith(".gcode"):
                self._write_mmu_metadata(filepath)

    def _write_mmu_metadata(self, file_path):
        self._log("Checking for MMU metadata placeholder in file: " + file_path)
        has_placeholder, tools_used = self._enumerate_used_tools(file_path)

        # An edit-in-place is seen by Moonraker as a file change (rightly so),
        # BUT it's seen this way even if no changes are made. We use `has_placeholder`
        # to determine whether there are any changes to make to prevent an infinite loop.
        if has_placeholder:
            self._log("Writing MMU metadata to file: " + file_path)
            return self._inject_tool_usage(file_path, tools_used)
        else:
            self._log("No MMU metadata placeholder found in file: " + file_path)
            return False

    def _log(self, message):
        logging.info("mmu_server: " + message)

    def _enumerate_used_tools(self, file_path):
        regex = re.compile(self.TOOL_DISCOVERY_REGEX, re.IGNORECASE)
        tools_used = set()
        has_placeholder = False

        with open(file_path, "r") as f:
            for line in f:
                if not has_placeholder and not line.startswith(";") and self.METADATA_REPLACEMENT_STRING in line:
                    has_placeholder = True

                match = regex.match(line)
                if match:
                    tool = match.group("tool")
                    tools_used.add(int(tool))

        return (has_placeholder, sorted(tools_used))

    def _inject_tool_usage(self, file_path, tools_used):
        with fileinput.FileInput(file_path, inplace=1) as file:
            for line in file:
                if not line.startswith(";") and self.METADATA_REPLACEMENT_STRING in line:
                    # Ignore comment lines to preserve slicer metadata comments
                    print(line.replace(self.METADATA_REPLACEMENT_STRING, ",".join(map(str, tools_used))), end="")
                else:
                    print(line, end="")

        return True

    # Logic to provide spoolman integration.
    # Leverage configuration from Spoolman component
    async def get_filaments(self, gate_ids):
        spoolman = self.server.lookup_component("spoolman")
        kapis = self.server.lookup_component("klippy_apis")

        gate_dict = {}
        for gate_id, spool_id in gate_ids:
            full_url = f"{spoolman.spoolman_url}/v1/spool/{spool_id}"

            response = await spoolman.http_client.request(method="GET", url=full_url, body=None)
            response.raise_for_status()

            record = response.json()
            filament = record["filament"]

            material = filament.get('material', '')[:6] # Keep material spec short for Klipperscreen
            color_hex = filament.get('color_hex', '')[:6] # Strip alpha channel if it exists

            gate_dict[gate_id] = {'spool_id': spool_id, 'material': material, 'color': color_hex}

        try:
            await kapis.run_gcode(f"MMU_GATE_MAP MAP=\"{gate_dict}\" QUIET=1")
        except self.server.error as e:
            logging.info(f"mmu_server: Exception running MMU gcode: %s" % str(e))

        return gate_dict
   
def load_component(config):
    return MmuServer(config)

