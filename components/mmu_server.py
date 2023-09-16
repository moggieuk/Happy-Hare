# Happy Hare MMU Software
# Moonraker support for a file-preprocessor that injects MMU metadata into gcode files
#
# Copyright (C) 2023  Kieran Eglin <@kierantheman (discord)>, <kieran.eglin@gmail.com>
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
    METADATA_REPLACEMENT_STRING = "!mmu_inject_referenced_tools!"

    def __init__(self, config):
        self.config = config
        self.server = config.get_server()
        self.file_manager = self.server.lookup_component("file_manager")
        self.enable_file_preprocessor = config.getboolean("enable_file_preprocessor", True)

        self.server.register_event_handler("file_manager:filelist_changed", self._filelist_changed)

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
        logging.info("mmu_file_processor " + message)

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

def load_component(config):
    return MmuServer(config)
