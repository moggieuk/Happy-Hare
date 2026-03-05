# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_CHECK_GATE command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuCheckGateCommand(BaseCommand):

    CMD = "MMU_CHECK_GATE"

    HELP_BRIEF = "Automatically inspects gate(s), parks filament and marks availability"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "QUIET  = [0|1]\n"
        + "TOOLS  = comma,separated,tools\n"
        + "GATES  = comma,separated,gates\n"
        + "TOOL   = t (single tool)\n"
        + "GATE   = g (single gate)\n"
        + "ALL    = [0|1]\n"
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
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.

        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_not_homed(): return
        if self.mmu.check_if_bypass(): return
        self.mmu._fix_started_state()

        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        # These three parameters are mutually exclusive so we only process one
        tools = gcmd.get('TOOLS', "!")
        gates = gcmd.get('GATES', "!")
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu.num_gates - 1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu.num_gates - 1)
        all_gates = gcmd.get_int('ALL', 0, minval=0, maxval=1)
        if self.mmu.check_if_not_calibrated(self.mmu.CALIBRATED_ESSENTIAL, check_gates = None if gate == -1 else [gate]): return # TODO Incomplete/simplified gate selection

        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                with self.mmu._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during gate check
                    with self.mmu._wrap_suspendwrite_variables():  # Reduce I/O activity to a minimum
                        with self.mmu.wrap_action(ACTION_CHECKING):
                            tool_selected = self.mmu.tool_selected
                            filament_pos = self.mmu.filament_pos
                            gates_tools = []
                            if gate >= 0:
                                # Individual gate
                                gates_tools.append([gate, -1])
                            elif tool >= 0:
                                # Individual tool
                                gate = self.mmu.ttg_map[tool]
                                gates_tools.append([gate, tool])
                            elif all_gates:
                                for gate in range(self.mmu.num_gates):
                                    gates_tools.append([gate, -1])
                            elif gates != "!":
                                # List of gates
                                try:
                                    for gate in gates.split(','):
                                        gate = int(gate)
                                        if 0 <= gate < self.mmu.num_gates:
                                            gates_tools.append([gate, -1])
                                except ValueError:
                                    raise MmuError("Invalid GATES parameter: %s" % tools)
                            elif tools != "!":
                                # Tools used in print (may be empty list)
                                try:
                                    for tool in tools.split(','):
                                        if not tool == "":
                                            tool = int(tool)
                                            if 0 <= tool < self.mmu.num_gates:
                                                gate = self.mmu.ttg_map[tool]
                                                gates_tools.append([gate, tool])
                                    if len(gates_tools) == 0:
                                        self.mmu.log_debug("No tools to check, assuming default tool is already loaded")
                                        return
                                except ValueError:
                                    raise MmuError("Invalid TOOLS parameter: %s" % tools)
                            elif self.mmu.gate_selected >= 0:
                                # No parameters means current gate
                                gates_tools.append([self.mmu.gate_selected, -1])
                            else:
                                raise MmuError("Current gate is invalid")

                            # Force initial eject
                            if filament_pos != FILAMENT_POS_UNLOADED:
                                self.mmu.log_info("Unloading current tool prior to checking gates")

                                # Perform full unload sequence including parking
                                self.mmu._note_toolchange("< %s" % self.mmu.selected_tool_string())
                                self.mmu.last_statistics = {}
                                self.mmu._save_toolhead_position_and_park('unload')
                                self.mmu._unload_tool(form_tip=FORM_TIP_STANDALONE)
                                self.mmu._persist_gate_statistics()
                                self.mmu._continue_after('unload')

                            if len(gates_tools) > 1:
                                self.mmu.log_info("Will check gates: %s" % ', '.join(str(g) for g,t in gates_tools))
                            with self.mmu.wrap_suppress_visual_log():
                                self.mmu._set_tool_selected(TOOL_GATE_UNKNOWN)
                                for gate, tool in gates_tools:
                                    try:
                                        self.mmu.select_gate(gate)
                                        self.mmu.log_info("Checking gate %d..." % gate)
                                        _ = self.mmu._load_gate(allow_retry=False)
                                        if tool >= 0:
                                            self.mmu.log_info("Tool T%d - Filament detected. Gate %d marked available" % (tool, gate))
                                        else:
                                            self.mmu.log_info("Gate %d - Filament detected. Marked available" % gate)
                                        self.mmu._set_gate_status(gate, max(self.mmu.gate_status[gate], GATE_AVAILABLE))
                                        try:
                                            _,_ = self.mmu._unload_gate()
                                        except MmuError as ee:
                                            raise MmuError("Failure during check gate %d %s:\n%s" % (gate, "(T%d)" % tool if tool >= 0 else "", str(ee)))
                                    except MmuError as ee:
                                        self.mmu._set_gate_status(gate, GATE_EMPTY)
                                        self.mmu._set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=True)
                                        if tool >= 0:
                                            msg = "Tool T%d on gate %d marked EMPTY" % (tool, gate)
                                        else:
                                            msg = "Gate %d marked EMPTY" % gate
                                        self.mmu.log_debug("Gate marked empty because: %s" % str(ee))
                                        if self.mmu.is_in_print():
                                            raise MmuError("%s%s" % ("Required " if self.mmu.is_printing() else "", msg))
                                        else:
                                            self.mmu.log_always(msg)
                                    finally:
                                        self.mmu._initialize_encoder() # Encoder 0000

                            # If not printing select original tool and load filament if necessary
                            # We don't do this when printing because this is expected to preceed loading initial tool
                            if not self.mmu.is_printing():
                                try:
                                    if tool_selected == TOOL_GATE_BYPASS:
                                        self.mmu.select_bypass()
                                    elif tool_selected != TOOL_GATE_UNKNOWN:
                                        if filament_pos == FILAMENT_POS_LOADED:
                                            self.mmu.log_info("Restoring tool loaded prior to checking gates")

                                            # Perform full load sequence including parking
                                            self.mmu._note_toolchange("> %s" % self.mmu.selected_tool_string(tool=tool_selected))
                                            self.mmu.last_statistics = {}
                                            self.mmu._save_toolhead_position_and_park('load')
                                            self.mmu._select_and_load_tool(tool_selected, purge=PURGE_NONE)
                                            self.mmu._persist_gate_statistics()
                                            self.mmu._continue_after('load')
                                        else:
                                            self.mmu.select_tool(tool_selected)
                                except MmuError as ee:
                                    raise MmuError("Failure re-selecting Tool %d:\n%s" % (tool_selected, str(ee)))
                            else:
                                # At least restore the selected tool, but don't re-load filament
                                self.mmu.select_tool(tool_selected)

                            if not quiet:
                                self.mmu.log_info(self.mmu._mmu_visual_to_string())

        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
