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

import logging, os, re, fileinput

class MmuServer:
    TOOL_DISCOVERY_REGEX = r"((^MMU_CHANGE_TOOL(_STANDALONE)? .*?TOOL=)|(^T))(?P<tool>\d{1,2})"
    METADATA_TOOL_DISCOVERY = "!referenced_tools!"

    COLORS1_REGEX = r"^; extruder_colour =(.*)$" # PS/SS
    COLORS2_REGEX = r"^; filament_colour_colour =(.*)$" # Orca slicer
    METADATA_COLORS = "!colors!"

    TEMPS_REGEX = r"^; temperature =(.*)$"
    METADATA_TEMPS = "!temperatures!"

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
        self._log("Checking for MMU metadata placeholders in file: " + file_path)
        has_placeholder, tools_used, colors, temps = self._parse_gcode_file(file_path)

        # An edit-in-place is seen by Moonraker as a file change (rightly so),
        # BUT it's seen this way even if no changes are made. We use `has_placeholder`
        # to determine whether there are any changes to make to prevent an infinite loop.
        if has_placeholder:
            self._log("Writing MMU metadata to file: " + file_path)
            return self._inject_placeholders(file_path, tools_used, colors, temps)
        else:
            self._log("No MMU metadata placeholder found in file: " + file_path)
            return False

    def _log(self, message):
        logging.info("mmu_server: " + message)

    def _parse_gcode_file(self, file_path):
        tools_regex = re.compile(self.TOOL_DISCOVERY_REGEX, re.IGNORECASE)
        colors1_regex = re.compile(self.COLORS1_REGEX, re.IGNORECASE)
        colors2_regex = re.compile(self.COLORS2_REGEX, re.IGNORECASE)
        color_regexes = [colors1_regex, colors2_regex]
        temps_regex = re.compile(self.TEMPS_REGEX, re.IGNORECASE)

        has_tools_placeholder = has_colors_placeholder = has_temps_placeholder = False
        found_colors = found_temps = False

        tools_used = set()
        colors = []
        temps = []

        with open(file_path, "r") as f:
            for line in f:
                # !referenced_tools! processing
                if not has_tools_placeholder and not line.startswith(";") and self.METADATA_TOOL_DISCOVERY in line:
                    has_tools_placeholder = True

                match = tools_regex.match(line)
                if match:
                    tool = match.group("tool")
                    tools_used.add(int(tool))

                # !colors! processing
                if not has_colors_placeholder and not line.startswith(";") and self.METADATA_COLORS in line:
                    has_colors_placeholder = True

                if not found_colors:
                    for regex in color_regexes:
                        match = regex.match(line)
                        if match:
                            colors_csv = [color.strip().lstrip('#') for color in match.group(1).split(';')]
                            colors.extend(colors_csv)
                            found_colors = True
                            break

                # !temps! processing
                if not has_temps_placeholder and not line.startswith(";") and self.METADATA_TEMPS in line:
                    has_temps_placeholder = True

                if not found_temps:
                    match = temps_regex.match(line)
                    if match:
                        temps_csv = match.group(1).strip().split(';')
                        temps.extend(temps_csv)
                        found_temps = True

        return (has_tools_placeholder or has_colors_placeholder or has_temps_placeholder, sorted(tools_used), colors, temps)

    def _inject_placeholders(self, file_path, tools_used, colors, temps):
        with fileinput.FileInput(file_path, inplace=1) as file:
            for line in file:
                # Ignore comment lines to preserve slicer metadata comments
                if not line.startswith(";"):
                    if self.METADATA_TOOL_DISCOVERY in line:
                        line = line.replace(self.METADATA_TOOL_DISCOVERY, ",".join(map(str, tools_used)))
                    if self.METADATA_COLORS in line:
                        line = line.replace(self.METADATA_COLORS, ",".join(map(str, colors)))
                    if self.METADATA_TEMPS in line:
                        line = line.replace(self.METADATA_TEMPS, ",".join(map(str, temps)))
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
            if not response.has_error():

                record = response.json()
                filament = record["filament"]

                material = filament.get('material', '')[:6] # Keep material spec short for Klipperscreen
                color_hex = filament.get('color_hex', '')[:6] # Strip alpha channel if it exists

                gate_dict[gate_id] = {'spool_id': spool_id, 'material': material, 'color': color_hex}
            elif response.status_code != 404:
                response.raise_for_status()
        try:
            await kapis.run_gcode(f"MMU_GATE_MAP MAP=\"{gate_dict}\" QUIET=1")
        except self.server.error as e:
            logging.info(f"mmu_server: Exception running MMU gcode: %s" % str(e))

        return gate_dict
   
def load_component(config):
    return MmuServer(config)

