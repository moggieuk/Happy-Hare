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
from ..mmu_utils       import MmuError, MmuGateHomingMiss
from .mmu_base_command import *


class MmuCheckGateCommand(BaseCommand):

    CMD = "MMU_CHECK_GATE"

    HELP_BRIEF = "Automatically inspects gate(s), parks filament and marks availability"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "QUIET  = [0|1]\n"
        + "TOOLS  = comma,separated,tools\n"
        + "GATES  = comma,separated,gates\n"
        + "TOOL   = t (single tool)\n"
        + "GATE   = g (single gate)\n"
        + "ALL    = [0|1]\n"
        + "TD1    = [0|1] Also capture TD/color on unmeasured gates (costs a bowden traverse)\n"
        + "TD1_UPDATE = [0|1] As TD1=1 but re-measure gates that already have a reading\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}             ...Check the current gate\n"
        + f"{CMD} ALL=1       ...Check every gate and update availability\n"
        + f"{CMD} GATES=0,2,4 ...Check gates 0, 2 and 4\n"
        + f"{CMD} TOOL=1      ...Check the gate mapped to tool 1\n"
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
        mmu.fix_started_state()

        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        # Never enters the extruder, so no heat or idle-printer check - TD1= must work
        # from _MMU_PRINT_START, which runs with print_state 'started'
        td1 = gcmd.get_int('TD1', 0, minval=0, maxval=1)
        td1_force = gcmd.get_int('TD1_UPDATE', 0, minval=0, maxval=1)
        td1 = td1 or td1_force
        # These three parameters are mutually exclusive so we only process one
        tools = gcmd.get('TOOLS', "!")
        gates = gcmd.get('GATES', "!")
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=mmu.num_gates - 1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=mmu.num_gates - 1)
        all_gates = gcmd.get_int('ALL', 0, minval=0, maxval=1)

        # Gross simplification of calibration check - all units must have essential
        # calibration unless called for a specific gate
        units = (
            [mmu.mmu_unit(gate)]
            if gate >= 0
            else mmu.mmu_machine.units
        )
        for u in units:
            if self.check_if_not_calibrated(
                CALIBRATED_ESSENTIAL,
                check_gates=[gate] if gate >= 0 else None,
                mmu_unit=u,
            ):
                return

        recover_on_error = True
        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu.wrap_suspend_filament_monitoring(): # Don't want runout accidentally triggering during gate check
                    with mmu.var_manager.wrap_suspend_write_variables(): # Reduce I/O activity to a minimum
                        with mmu.wrap_action(ACTION_CHECKING):
                            tool_selected = mmu.tool_selected
                            filament_pos = mmu.filament_pos
                            gates_tools = []
                            if gate >= 0:
                                # Individual gate (priority)
                                gates_tools.append([gate, -1])
                            elif tool >= 0:
                                # Individual tool
                                gate = mmu.ttg_map[tool]
                                gates_tools.append([gate, tool])
                            elif all_gates:
                                for gate in range(mmu.num_gates):
                                    gates_tools.append([gate, -1])
                            elif gates != "!":
                                # List of gates
                                try:
                                    for gate in gates.split(','):
                                        gate = int(gate)
                                        if 0 <= gate < mmu.num_gates:
                                            gates_tools.append([gate, -1])
                                except ValueError:
                                    raise MmuError("Invalid GATES parameter: %s" % tools)
                            elif tools != "!":
                                # Tools used in print (may be empty list)
                                try:
                                    for tool in tools.split(','):
                                        if not tool == "":
                                            tool = int(tool)
                                            if 0 <= tool < mmu.num_gates:
                                                gate = mmu.ttg_map[tool]
                                                gates_tools.append([gate, tool])
                                    if len(gates_tools) == 0:
                                        mmu.log_debug("No tools to check, assuming default tool is already loaded")
                                        return
                                except ValueError:
                                    raise MmuError("Invalid TOOLS parameter: %s" % tools)
                            elif mmu.gate_selected >= 0:
                                # No parameters means current gate
                                gates_tools.append([mmu.gate_selected, -1])
                            else:
                                raise MmuError("Current gate is invalid")

                            # An already loaded gate is proven present - mark available and skip rather than unload to re-verify
                            if not td1 and filament_pos == FILAMENT_POS_LOADED and mmu.gate_selected >= 0 and any(g == mmu.gate_selected for g, _t in gates_tools):
                                mmu.gate_maps.set_gate_status(mmu.gate_selected, max(mmu.gate_status[mmu.gate_selected], GATE_AVAILABLE))
                                mmu.log_info("Gate %d already loaded - marked available, skipping check" % mmu.gate_selected)
                                gates_tools = [[g, t] for g, t in gates_tools if g != mmu.gate_selected]
                                if not gates_tools:
                                    if not quiet:
                                        mmu.log_info(mmu._mmu_visual_to_string(), color=True)
                                    return

                            # Force initial eject
                            if filament_pos != FILAMENT_POS_UNLOADED:
                                mmu.log_info("Unloading current tool prior to checking gates")

                                # Perform full unload sequence including parking
                                mmu._note_toolchange("< %s" % mmu.selected_tool_string())
                                mmu.last_statistics = {}
                                mmu._save_toolhead_position_and_park('unload')
                                mmu._unload_tool(form_tip=FORM_TIP_STANDALONE)
                                mmu._persist_gate_statistics()
                                mmu._continue_after('unload')

                            if len(gates_tools) > 1:
                                mmu.log_info("Will check gates: %s" % ', '.join(str(g) for g,t in gates_tools))
                            with mmu.wrap_suppress_visual_log():
                                mmu._set_tool_selected(TOOL_GATE_UNKNOWN)
                                checked_gates = set()
                                empty_gates = set()
                                failed_gates = set()
                                failures = []
                                if td1:
                                    # One probe for the batch - a dead bridge must not
                                    # turn every gate into a failure
                                    try:
                                        mmu.td1.refresh()
                                    except MmuError as ee:
                                        mmu.log_warning("TD-1 measurement skipped: %s" % str(ee))
                                        td1 = 0
                                for gate, tool in gates_tools:
                                    # One gate can serve several tools. Reuse a verified gate,
                                    # but re-resolve every tool mapped to an empty one.
                                    if gate in checked_gates or gate in failed_gates:
                                        continue
                                    self._check_path_unloaded(gate)
                                    if (gate in empty_gates
                                            or mmu.gate_occupancy(gate) == OCCUPANCY_EMPTY):
                                        empty_gates.add(gate)
                                        mmu.gate_maps.set_gate_status(gate, GATE_EMPTY)
                                        msg = ("Tool T%d on gate %d marked EMPTY" % (tool, gate)
                                               if tool >= 0 else "Gate %d marked EMPTY" % gate)
                                        if tool >= 0 and mmu.endless_spool_enabled and mmu.p.endless_spool_on_load:
                                            next_gate, es_msg = mmu.gate_maps.get_next_endless_spool_gate(
                                                tool, gate, exclude_gates=empty_gates | failed_gates)
                                            if next_gate >= 0:
                                                mmu.log_always("%s! Checking for alternative gates %s" % (msg, es_msg))
                                                mmu.log_info("Remapping T%d to gate %d" % (tool, next_gate))
                                                mmu.gate_maps.remap_tool(tool, next_gate)
                                                gates_tools.append([next_gate, tool])
                                                continue
                                        failures.append(("Required " if mmu.is_printing() else "") + msg)
                                        mmu.log_always(msg)
                                        continue

                                    try:
                                        mmu.select_gate(gate)
                                        # Suspension snapshots the selected gate's sensors.
                                        with mmu.wrap_suspend_insert_events():
                                            mmu.log_info("Checking gate %d..." % gate)
                                            # Skip gates that already have a reading
                                            # unless TD1_UPDATE=1 asks for a fresh one
                                            td1_mgr = mmu.mmu_unit(gate).td1_manager
                                            measure = bool(td1 and (td1_force or td1_mgr.needs_measurement(gate)))
                                            baseline = None
                                            try:
                                                if measure:
                                                    try:
                                                        baseline = td1_mgr.baseline(gate)
                                                    except MmuError as ee:
                                                        # Don't pay for a traverse that can't produce a reading
                                                        mmu.log_warning("Gate %d - TD-1 unavailable: %s" % (gate, str(ee)))
                                                        measure = False
                                                mmu._load_gate(allow_retry=False, mark_empty_on_failure=False)
                                            except MmuGateHomingMiss:
                                                # Empty, but discovered by a failed pickup rather
                                                # than a switch - not grounds for a remap.
                                                failed_gates.add(gate)
                                                msg = ("Tool T%d on gate %d" % (tool, gate) if tool >= 0
                                                       else "Gate %d" % gate)
                                                msg += " marked EMPTY (no filament detected during homing)"
                                                failures.append(("Required " if mmu.is_printing() else "") + msg)
                                                mmu.log_always(msg)
                                                continue
                                            except MmuError as ee:
                                                # Safe to carry on only if nothing moved. An unresolved
                                                # position means no further gate can be selected.
                                                if mmu.filament_pos != FILAMENT_POS_UNLOADED:
                                                    recover_on_error = False
                                                    raise
                                                msg = "Gate %d could not be checked: %s" % (gate, str(ee))
                                                failed_gates.add(gate)
                                                failures.append(msg)
                                                mmu.log_warning(msg)
                                                continue
                                            if tool >= 0:
                                                mmu.log_info("Tool T%d - Filament detected. Gate %d marked available" % (tool, gate))
                                            else:
                                                mmu.log_info("Gate %d - Filament detected. Marked available" % gate)
                                            mmu.gate_maps.set_gate_status(gate, max(mmu.gate_status[gate], GATE_AVAILABLE))
                                            if measure:
                                                # Down the bowden past the scanner, never
                                                # into the extruder (so no tip forming)
                                                try:
                                                    mmu.load_sequence(skip_extruder=True)
                                                    try:
                                                        td1_mgr.capture(gate, baseline)
                                                    except MmuError as ee:
                                                        # A scanner failure doesn't make the gate unavailable
                                                        mmu.log_warning("Gate %d - filament found but not measured: %s" % (gate, str(ee)))
                                                    mmu.unload_sequence()
                                                except MmuError:
                                                    recover_on_error = False
                                                    raise
                                            else:
                                                if td1 and td1_mgr.has_gate_td1(gate):
                                                    mmu.log_info("Gate %d already measured - use TD1_UPDATE=1 to re-read" % gate)
                                                elif td1:
                                                    mmu.log_info("Gate %d has no TD-1 scanner - availability checked only" % gate)
                                                u = mmu.mmu_unit(gate)
                                                extra_homing = u.p.gate_homing_max if u.p.gate_homing_endstop == SENSOR_ENCODER else 0
                                                try:
                                                    mmu._unload_gate(extra_homing)
                                                except MmuError:
                                                    recover_on_error = False
                                                    raise
                                            checked_gates.add(gate)
                                    except MmuError as ee:
                                        raise MmuError("Failure during check gate %d %s:\n%s" % (
                                            gate, "(T%d)" % tool if tool >= 0 else "", str(ee)))
                                    finally:
                                        mmu.initialize_encoder() # Encoder 0000

                            # Empty or unverifiable gates don't abort the sweep, but a print
                            # must still pause once every requested gate has been checked.
                            if failures and mmu.is_in_print():
                                raise MmuError("Gate check completed with unavailable or unchecked gates:\n"
                                               + "\n".join(failures))

                            # If not printing select original tool and load filament if necessary
                            # We don't do this when printing because this is expected to precede loading initial tool
                            if not mmu.is_printing():
                                try:
                                    if tool_selected == TOOL_GATE_BYPASS:
                                        mmu.select_bypass()
                                    elif tool_selected != TOOL_GATE_UNKNOWN:
                                        if filament_pos == FILAMENT_POS_LOADED:
                                            mmu.log_info("Restoring tool loaded prior to checking gates")

                                            # Perform full load sequence including parking
                                            mmu._note_toolchange("> %s" % mmu.selected_tool_string(tool=tool_selected))
                                            mmu.last_statistics = {}
                                            mmu._save_toolhead_position_and_park('load')
                                            mmu._select_and_load_tool(tool_selected, purge=PURGE_NONE)
                                            mmu._persist_gate_statistics()
                                            mmu._continue_after('load')
                                        else:
                                            mmu.select_tool(tool_selected)
                                except MmuError as ee:
                                    raise MmuError("Failure re-selecting Tool %d:\n%s" % (tool_selected, str(ee)))
                            else:
                                # At least restore the selected tool, but don't re-load filament
                                mmu.select_tool(tool_selected)

                            if not quiet:
                                mmu.log_info(mmu._mmu_visual_to_string(), color=True)

        except MmuError as ee:
            # recover_on_error is cleared only where motion was left unresolved: a sensor
            # sweep cannot establish a position the failure itself made unknown.
            mmu.handle_mmu_error(str(ee), recover=recover_on_error)

    def _check_path_unloaded(self, gate):
        """Refuse a gate whose path isn't clear, before anything is selected or moved."""
        mmu = self.mmu
        unit = mmu.mmu_unit(gate)
        # Must precede select_gate(): the encoder case reads the previous selection.
        toolhead_sensor = mmu.sensor_manager.get_qualified_endstop_name(SENSOR_TOOLHEAD, mmu_unit=unit)
        if (mmu.filament_pos != FILAMENT_POS_UNLOADED
                or mmu.sensor_manager.check_event_sensor(toolhead_sensor, gate) is True):
            raise MmuError("Cannot check gate %d: filament path is not safely unloaded.\n"
                           "Run MMU_UNLOAD, or MMU_RECOVER if the position is unknown" % gate)
        for endstop in SHARED_GATE_ENDSTOPS:
            if mmu._shared_gate_path_occupied(endstop, gate):
                raise MmuError("Cannot check gate %d: shared '%s' path is occupied by another gate.\n"
                               "Clear it with MMU_UNLOAD, or MMU_RECOVER if the position is unknown"
                               % (gate, endstop))

