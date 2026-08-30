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
        f"{CMD}: {HELP_BRIEF}\n"
        + "QUIET                 = [0|1]\n"
        + "STANDALONE            = [0|1]\n"
        + "RESTORE               = [0|1]\n"
        + "SKIP_TIP              = [0|1]\n"
        + "SKIP_PURGE            = [0|1]\n"
        + "NEXT_POS              = X,Y              (optional; only used when restore_xy_pos is 'next')\n"
        + "TOOL                  = #(int)\n"
        + "GATE                  = #(int)\n"
        + "SLICER_PURGE          = #(mm)            (optional; captures the slicer calculated purge volume)\n"
        + "SLICER_RETRACTION     = #(mm)            (optional; captures the slicer retraction length)\n"
        + "SLICER_FW_RETRACTION  = true|false|0|1   (optional; captures the slicer firmware retraction setting. Ignored if not enabled in printer)\n"

    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} TOOL=2              ...Change to tool 2 (equivalent to running T2)\n"
        + f"{CMD} TOOL=0 STANDALONE=1 ...Change to tool 0 forcing standalone tip forming/purging (not slicer)\n"
        + f"{CMD} GATE=3              ...Change to whichever tool is mapped to gate 3 (UI driven)\n"
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
        mmu = self.mmu

        if self.check_if_disabled(): return
        if self.check_if_bypass(): return

        # Ensure full calibration before we allow this command to run
        for u in mmu.mmu_machine.units:
            if self.check_if_not_calibrated(CALIBRATED_ESSENTIAL, mmu_unit=u):
                return

        mmu.fix_started_state()

        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        standalone = bool(gcmd.get_int('STANDALONE', 0, minval=0, maxval=1))
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))
        skip_tip = bool(gcmd.get_int('SKIP_TIP', 0, minval=0, maxval=1))
        skip_purge = bool(gcmd.get_int('SKIP_PURGE', 0, minval=0, maxval=1))

        # Capture slicer retraction and purge settings for later use.
        mmu.slicer_purge       = gcmd.get_float('SLICER_PURGE', -1)
        mmu.slicer_retraction  = gcmd.get_float('SLICER_RETRACTION', -1)
        fw_retraction          = gcmd.get('SLICER_FW_RETRACTION', '0').strip().lower()
        mmu.slicer_fw_retraction = fw_retraction in ('true', '1')
        if fw_retraction not in ('true', '1', 'false', '0'):
            mmu.log_error("Invalid slicer FW retraction setting ignored")

        # Check if slicer firmware retraction is enabled in the printer
        if mmu.slicer_fw_retraction:
            fw_retraction_obj = mmu.printer.lookup_object('firmware_retraction', None)
            if fw_retraction_obj:
                mmu.slicer_retraction = -1
            else:
                mmu.log_warning("Print gcode specifies firmware retraction but it's not enabled in the printer")
                mmu.slicer_fw_retraction = False
  
        # Handle "next_pos" option for toolhead position restoration
        next_pos = None
        sequence_vars_macro = mmu.printer.lookup_object("gcode_macro _MMU_SEQUENCE_VARS", None)
        if sequence_vars_macro and sequence_vars_macro.variables.get('restore_xy_pos', 'last') == 'next':
            # Convert next position to absolute coordinates
            next_pos = gcmd.get('NEXT_POS', None)
            if next_pos:
                try:
                    x, y = map(float, next_pos.split(','))
                    gcode_status = mmu.gcode_move.get_status(mmu.reactor.monotonic())
                    if not gcode_status['absolute_coordinates']:
                        gcode_pos = gcode_status['gcode_position']
                        x += gcode_pos[0]
                        y += gcode_pos[1]
                    next_pos = [x, y]
                except (ValueError, KeyError, TypeError) as ee:
                    # If something goes wrong it is better to ignore next pos completely
                    mmu.log_error("Error parsing NEXT_POS: %s" % str(ee))

        # To support Tx commands linked directly (currently not used because of Mainsail visibility which requires macros)
        cmd = gcmd.get_command().strip()
        match = re.match(r'[Tt](\d{1,3})$', cmd)
        if match:
            tool = int(match.group(1))
            if tool < 0 or tool > mmu.num_gates - 1:
                raise gcmd.error("Invalid tool")
        else:
            # Special case for UI driven change tool where gate is chosen
            tool = None
            gate = gcmd.get_int('GATE', None, minval=0, maxval=mmu.num_gates - 1)
            if gate is not None:
                if gate == mmu.gate_selected:
                    mmu.log_always("Gate %s is already loaded as %s" % (gate, mmu.selected_tool_string(tool)))
                    return

                possible_tools = [tool for tool in range(mmu.num_gates) if mmu.ttg_map[tool] == gate]
                if not possible_tools:
                    mmu.log_error("No tool associated with gate %s. Check tool-to-gate mapping with MMU_TTG_MAP" % gate)
                    return

                if mmu.tool_selected in possible_tools:
                    mmu.gate_maps.remap_tool(mmu.tool_selected, gate)
                    tool = mmu.tool_selected
                else:
                    tool = possible_tools[0]

            if tool is None:
                tool = gcmd.get_int('TOOL', minval=0, maxval=mmu.num_gates - 1)

        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu.wrap_suspend_filament_monitoring(): # Don't want runout accidentally triggering during tool change
                    with mmu.var_manager.wrap_suspend_write_variables(): # Reduce I/O activity to a minimum

                        # Good place to update automatic clog detection length if applicable
                        if mmu.has_encoder():
                            mmu.encoder().note_clog_detection_length()

                        do_form_tip = FORM_TIP_STANDALONE
                        if skip_tip:
                            do_form_tip = FORM_TIP_NONE
                        elif mmu.is_printing() and not (standalone or mmu.p.force_form_tip_standalone):
                            do_form_tip = FORM_TIP_SLICER

                        do_purge = PURGE_STANDALONE
                        if skip_purge:
                            do_purge = PURGE_NONE
                        elif mmu.is_printing() and not (standalone or mmu.p.force_purge_standalone):
                            do_purge = PURGE_SLICER

                        tip_msg = ("with slicer tip forming" if do_form_tip == FORM_TIP_SLICER else
                                   "with standalone MMU tip forming" if do_form_tip == FORM_TIP_STANDALONE else
                                   "without tip forming")
                        purge_msg = ("slicer purging" if do_purge == PURGE_SLICER else
                                     "standalone MMU purging" if do_purge == PURGE_STANDALONE else
                                     "without purging")
                        mmu.log_debug("Tool change initiated %s and %s" % (tip_msg, purge_msg))

                        current_tool_string = mmu.selected_tool_string()
                        new_tool_string = mmu.selected_tool_string(tool)

                        # Check if we are already loaded
                        if (
                            tool == mmu.tool_selected and
                            mmu.ttg_map[tool] == mmu.gate_selected and
                            mmu.filament_pos == FILAMENT_POS_LOADED
                        ):
                            mmu.log_always("Tool %s is already loaded" % mmu.selected_tool_string(tool))
                            return

                        # Load only case
                        if mmu.filament_pos == FILAMENT_POS_UNLOADED:
                            msg = "Tool change requested: %s" % new_tool_string
                            m117_msg = "> %s" % new_tool_string
                        elif mmu.tool_selected == tool:
                            msg = "Reloading: %s" % new_tool_string
                            m117_msg = "> %s" % new_tool_string
                        else:
                            # Normal toolchange case
                            msg = "Tool change requested, from %s to %s" % (current_tool_string, new_tool_string)
                            m117_msg = "%s > %s" % (current_tool_string, new_tool_string)

                        mmu._note_toolchange(m117_msg)
                        mmu.log_always(msg)

                        # Check if new tool is mapped to current gate
                        if mmu.ttg_map[tool] == mmu.gate_selected and mmu.filament_pos == FILAMENT_POS_LOADED:
                            mmu.select_tool(tool)
                            mmu._note_toolchange(mmu.selected_tool_string(tool))
                            return

                        # Ok, now ready to park and perform the swap
                        mmu._next_tool = tool # Valid only during the change process - cleared in _continue_after()
                        mmu.last_statistics = {}

                        mmu._save_toolhead_position_and_park('toolchange', next_pos=next_pos)

                        # Determine retraction options post load to compensate for unhandled orca/prusa/super slicer
                        # toolchange retraction when slicer settings are passed to mmu_change_tool
                        slicer_retract_len   = 0
                        slicer_retract_speed = 30
                        retract_fallback     = False
                        park_macro           = mmu.printer.lookup_object("gcode_macro _MMU_PARK", None)

                        # Only compensate when printing post initial change (firmware flag is true regardless if enabled,
                        # slicer_retraction is only > 0 when it needs to be applied)
                        if mmu.is_printing() and mmu.num_toolchanges >= 1:
                            if mmu.slicer_fw_retraction:
                                fw_retract = mmu.printer.lookup_object('firmware_retraction', None)
                                if fw_retract: # translate G10 into distance/speed for compensation
                                    slicer_retract_len = fw_retract.retract_length
                                    if fw_retract.retract_speed > 0:
                                        slicer_retract_speed = fw_retract.retract_speed
                            elif mmu.slicer_retraction > 0:
                                sequence_vars        = mmu.printer.lookup_object("gcode_macro _MMU_SEQUENCE_VARS", None)
                                slicer_retract_len   = mmu.slicer_retraction
                                slicer_retract_speed = sequence_vars.variables.get('retract_speed', slicer_retract_speed) if sequence_vars else slicer_retract_speed

                        mmu._set_next_position(next_pos) # This can also clear next_position
                        mmu._track_time_start('total')
                        mmu.printer.send_event("mmu:toolchange", mmu._last_tool, mmu._next_tool)

                        # Remember the tool that was actually in use before any load attempts
                        prev_tool = mmu.tool_selected

                        # Load attempts
                        attempts = 2 if mmu.p.retry_tool_change_on_error and (mmu.is_printing() or standalone) else 1 # TODO Replace with inattention timer
                        try:
                            for i in range(attempts):
                                try:
                                    if mmu.filament_pos != FILAMENT_POS_UNLOADED:
                                        mmu._unload_tool(form_tip=do_form_tip, prev_tool=prev_tool)
                                    mmu._select_and_load_tool(tool, purge=do_purge)
                                    break
                                except MmuError as ee:
                                    if i == attempts - 1:
                                        raise MmuError("%s.\nOccurred when changing tool: %s" % (str(ee), mmu._last_toolchange))
                                    mmu.log_error("%s.\nOccurred when changing tool: %s. Retrying..." % (str(ee), mmu._last_toolchange))
                                    # Try again but recover_filament_pos will ensure conservative treatment of unload
                                    mmu.recover_filament_pos()

                            mmu._track_swap_completed()
                            if mmu.p.log_m117_messages:
                                mmu.gcode.run_script_from_command("M117 T%s" % tool)
                        finally:
                            mmu._track_time_end('total')

                    # Updates swap statistics
                    mmu.num_toolchanges += 1
                    mmu._dump_statistics(job=not quiet, gate=not quiet)
                    mmu._persist_swap_statistics()
                    mmu._persist_gate_statistics()

                    # Compensate for unhandled slicer toolchange retraction by reducing _mmu_park un-retraction
                    if slicer_retract_len and park_macro:
                        retracted_length = float(park_macro.variables.get('retracted_length', 0) or 0) - slicer_retract_len
                        if retracted_length > 0:
                            mmu.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=_MMU_PARK VARIABLE=retracted_length VALUE=%s" % (retracted_length))
                            mmu.log_info("Adjusting un-retraction to %.1fmm to compensate for unhandled slicer %.1fmm retraction during toolchange" % (retracted_length, slicer_retract_len))
                        else:
                            mmu.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=_MMU_PARK VARIABLE=retracted_length VALUE=%s" % 0)
                            retract_fallback = True

                    # Restore to print deliberately outside of _wrap_gear_synced_to_extruder() to minimise delay after restoring position
                    mmu._continue_after('toolchange', restore=restore)

                    # Fall back / edge case - if _mmu_park parking/retraction is bypassed or slicer retraction > retracted_length
                    if slicer_retract_len and park_macro:
                        if retract_fallback or float(park_macro.variables.get('retracted_length', 0) or 0):
                            mmu.gcode.run_script_from_command("G1 E-%.2f F%d " % (slicer_retract_len, slicer_retract_speed * 60))
                            mmu.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=_MMU_PARK VARIABLE=retracted_length VALUE=%s" % 0)
                            mmu.log_info("Retracting %.1fmm to compensate for unhandled slicer retraction during toolchange" % (slicer_retract_len))

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
