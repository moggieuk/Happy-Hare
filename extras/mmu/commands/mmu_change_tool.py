# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_CHANGE_TOOL command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import re

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuChangeToolCommand(BaseCommand):

    CMD = "MMU_CHANGE_TOOL"

    HELP_BRIEF = "Perform a tool swap (called from Tx command)"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "QUIET      = [0|1]\n"
        + "STANDALONE = [0|1]\n"
        + "RESTORE    = [0|1]\n"
        + "SKIP_TIP   = [0|1]\n"
        + "SKIP_PURGE = [0|1]\n"
        + "NEXT_POS   = X,Y (optional; only used when restore_xy_pos is 'next')\n"
        + "TOOL       = #(int)\n"
        + "GATE       = #(int)\n"
    )
    HELP_SUPPLEMENT = (
        ""  # add examples here if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.

        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_bypass(): return
        if self.mmu.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[]): return # TODO Hard to tell what gates to check so don't check for now
        self.mmu._fix_started_state()

        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        standalone = bool(gcmd.get_int('STANDALONE', 0, minval=0, maxval=1))
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))
        skip_tip = bool(gcmd.get_int('SKIP_TIP', 0, minval=0, maxval=1))
        skip_purge = bool(gcmd.get_int('SKIP_PURGE', 0, minval=0, maxval=1))

        # Handle "next_pos" option for toolhead position restoration
        next_pos = None
        sequence_vars_macro = self.mmu.printer.lookup_object("gcode_macro _MMU_SEQUENCE_VARS", None)
        if sequence_vars_macro and sequence_vars_macro.variables.get('restore_xy_pos', 'last') == 'next':
            # Convert next position to absolute coordinates
            next_pos = gcmd.get('NEXT_POS', None)
            if next_pos:
                try:
                    x, y = map(float, next_pos.split(','))
                    gcode_status = self.mmu.gcode_move.get_status(self.mmu.reactor.monotonic())
                    if not gcode_status['absolute_coordinates']:
                        gcode_pos = gcode_status['gcode_position']
                        x += gcode_pos[0]
                        y += gcode_pos[1]
                    next_pos = [x, y]
                except (ValueError, KeyError, TypeError) as ee:
                    # If something goes wrong it is better to ignore next pos completely
                    self.mmu.log_error("Error parsing NEXT_POS: %s" % str(ee))

        # To support Tx commands linked directly (currently not used because of Mainsail visibility which requires macros)
        cmd = gcmd.get_command().strip()
        match = re.match(r'[Tt](\d{1,3})$', cmd)
        if match:
            tool = int(match.group(1))
            if tool < 0 or tool > self.mmu.num_gates - 1:
                raise gcmd.error("Invalid tool")
        else:
            # Special case for UI driven change tool where gate is chosen
            tool = None
            gate = gcmd.get_int('GATE', None, minval=0, maxval=self.mmu.num_gates - 1)
            if gate is not None:
                if gate == self.mmu.gate_selected:
                    self.mmu.log_always("Gate %s is already loaded as %s" % (gate, self.mmu.selected_tool_string(tool)))
                    return

                possible_tools = [tool for tool in range(self.mmu.num_gates) if self.mmu.ttg_map[tool] == gate]
                if not possible_tools:
                    self.mmu.log_error("No tool associated with gate %s. Check tool-to-gate mapping with MMU_TTG_MAP" % gate)
                    return

                if self.mmu.tool_selected in possible_tools:
                    self.mmu._remap_tool(self.mmu.tool_selected, gate)
                    tool = self.mmu.tool_selected
                else:
                    tool = possible_tools[0]

            if tool is None:
                tool = gcmd.get_int('TOOL', minval=0, maxval=self.mmu.num_gates - 1)

        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                with self.mmu._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during tool change
                    with self.mmu.var_manager.wrap_suspend_write_variables(): # Reduce I/O activity to a minimum
# PAUL we can't assume here and anyway, we might be changing between units!
# PAUL select_gate on selector need to check homing
# PAUL                        self.mmu._auto_home(tool=tool)
                        if self.mmu.has_encoder():
                            self.mmu.encoder().note_clog_detection_length()

                        do_form_tip = FORM_TIP_STANDALONE
                        if skip_tip:
                            do_form_tip = FORM_TIP_NONE
                        elif self.mmu.is_printing() and not (standalone or self.mmu.p.force_form_tip_standalone):
                            do_form_tip = FORM_TIP_SLICER

                        do_purge = PURGE_STANDALONE
                        if skip_purge:
                            do_purge = PURGE_NONE
                        elif self.mmu.is_printing() and not (standalone or self.mmu.p.force_purge_standalone):
                            do_purge = PURGE_SLICER

                        tip_msg = ("with slicer tip forming" if do_form_tip == FORM_TIP_SLICER else
                                   "with standalone MMU tip forming" if do_form_tip == FORM_TIP_STANDALONE else
                                   "without tip forming")
                        purge_msg = ("slicer purging" if do_purge == PURGE_SLICER else
                                     "standalone MMU purging" if do_purge == PURGE_STANDALONE else
                                     "without purging")
                        self.mmu.log_debug("Tool change initiated %s and %s" % (tip_msg, purge_msg))

                        current_tool_string = self.mmu.selected_tool_string()
                        new_tool_string = self.mmu.selected_tool_string(tool)

                        # Check if we are already loaded
                        if (
                            tool == self.mmu.tool_selected and
                            self.mmu.ttg_map[tool] == self.mmu.gate_selected and
                            self.mmu.filament_pos == FILAMENT_POS_LOADED
                        ):
                            self.mmu.log_always("Tool %s is already loaded" % self.mmu.selected_tool_string(tool))
                            return

                        # Load only case
                        if self.mmu.filament_pos == FILAMENT_POS_UNLOADED:
                            msg = "Tool change requested: %s" % new_tool_string
                            m117_msg = "> %s" % new_tool_string
                        elif self.mmu.tool_selected == tool:
                            msg = "Reloading: %s" % new_tool_string
                            m117_msg = "> %s" % new_tool_string
                        else:
                            # Normal toolchange case
                            msg = "Tool change requested, from %s to %s" % (current_tool_string, new_tool_string)
                            m117_msg = "%s > %s" % (current_tool_string, new_tool_string)

                        self.mmu._note_toolchange(m117_msg)
                        self.mmu.log_always(msg)

                        # Check if new tool is mapped to current gate
                        if self.mmu.ttg_map[tool] == self.mmu.gate_selected and self.mmu.filament_pos == FILAMENT_POS_LOADED:
                            self.mmu.select_tool(tool)
                            self.mmu._note_toolchange(self.mmu.selected_tool_string(tool))
                            return

                        # Ok, now ready to park and perform the swap
                        self.mmu._next_tool = tool # Valid only during the change process - cleared in _continue_after()
                        self.mmu.last_statistics = {}
                        self.mmu._save_toolhead_position_and_park('toolchange', next_pos=next_pos)
                        self.mmu._set_next_position(next_pos) # This can also clear next_position
                        self.mmu._track_time_start('total')
                        self.mmu.printer.send_event("mmu:toolchange", self.mmu._last_tool, self.mmu._next_tool)

                        # Remember the tool that was actually in use before any load attempts
                        prev_tool = self.mmu.tool_selected

                        attempts = 2 if self.mmu.p.retry_tool_change_on_error and (self.mmu.is_printing() or standalone) else 1 # TODO Replace with inattention timer
                        try:
                            for i in range(attempts):
                                try:
                                    if self.mmu.filament_pos != FILAMENT_POS_UNLOADED:
                                        self.mmu._unload_tool(form_tip=do_form_tip, prev_tool=prev_tool)
                                    self.mmu._select_and_load_tool(tool, purge=do_purge)
                                    break
                                except MmuError as ee:
                                    if i == attempts - 1:
                                        raise MmuError("%s.\nOccured when changing tool: %s" % (str(ee), self.mmu._last_toolchange))
                                    self.mmu.log_error("%s.\nOccured when changing tool: %s. Retrying..." % (str(ee), self.mmu._last_toolchange))
                                    # Try again but recover_filament_pos will ensure conservative treatment of unload
                                    self.mmu.recover_filament_pos()

                            self.mmu._track_swap_completed()
                            if self.mmu.p.log_m117_messages:
                                self.mmu.gcode.run_script_from_command("M117 T%s" % tool)
                        finally:
                            self.mmu._track_time_end('total')

                    # Updates swap statistics
                    self.mmu.num_toolchanges += 1
                    self.mmu._dump_statistics(job=not quiet, gate=not quiet)
                    self.mmu._persist_swap_statistics()
                    self.mmu._persist_gate_statistics()

                    # Deliberately outside of _wrap_gear_synced_to_extruder() so there is no absolutely no delay after restoring position
                    self.mmu._continue_after('toolchange', restore=restore)
        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
