# Happy Hare MMU Software
# Gate map / TTG map state manager
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Encapsulate gate map, TTG map, EndlessSpool map and related persistence / validation
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging

# Happy Hare imports
from .mmu_constants import *
from .mmu_utils     import MmuError, MmuColorUtils


class MmuGateMaps:
    """
    Encapsulates all per-gate filament metadata and persistence.
      - gate map state
      - tool-to-gate (TTG) mapping
      - EndlessSpool grouping / enablement
      - cached gate_color_rgb and slicer_color_rgb maps
      - persistence / reset / validation logic for all of the above
    """

    def __init__(self, mmu):
        self.mmu = mmu
        self.config = mmu.config
        self.printer = mmu.printer
        self.p = mmu.p
        self.num_gates = mmu.num_gates

        # Endless spool groups
        self.endless_spool_enabled = self.p.endless_spool_enabled
        if len(self.p.default_endless_spool_groups) > 0:
            if self.endless_spool_enabled == 1 and len(self.p.default_endless_spool_groups) != self.num_gates:
                raise self.config.error("endless_spool_groups has a different number of values than the number of gates")
        else:
            self.p.default_endless_spool_groups = list(range(self.num_gates))
        self.endless_spool_groups = list(self.p.default_endless_spool_groups)

        # Components of the gate map (status, material, color, spool_id, filament name, temperature, and speed override)
        self._gate_map_vars = [
            (VARS_MMU_GATE_STATUS,         'gate_status', GATE_UNKNOWN),
            (VARS_MMU_GATE_FILAMENT_NAME,  'gate_filament_name', ""),
            (VARS_MMU_GATE_MATERIAL,       'gate_material', ""),
            (VARS_MMU_GATE_COLOR,          'gate_color', ""),
            (VARS_MMU_GATE_TEMPERATURE,    'gate_temperature', int(self.p.default_extruder_temp)),
            (VARS_MMU_GATE_SPOOL_ID,       'gate_spool_id', -1),
            (VARS_MMU_GATE_SPEED_OVERRIDE, 'gate_speed_override', 100),
        ]

        for _, attr, default in self._gate_map_vars:
            default_attr = getattr(self.p, "default_" + attr)
            if len(default_attr) > 0:
                if len(default_attr) != self.num_gates:
                    raise self.config.error("%s has different number of entries than the number of gates" % attr)
            else:
                default_attr.extend([default] * self.num_gates)
            setattr(self, attr, list(default_attr))
        self.update_gate_color_rgb()

        # Helper RGB map used by slicer color projection / UI / LEDs
        self.slicer_color_rgb = [(0., 0., 0.)] * self.num_gates

        # Tool to gate mapping
        if len(self.p.default_ttg_map) > 0:
            if not len(self.p.default_ttg_map) == self.num_gates:
                raise self.config.error("tool_to_gate_map has different number of values than the number of gates")
        else:
            self.p.default_ttg_map = list(range(self.num_gates))
        self.ttg_map = list(self.p.default_ttg_map)

        # Slicer tool map is populated only at start of print
        self.slicer_tool_map = None             # Set by startup gcode from slicer during print


# -----------------------------------------------------------------------------------------------------------
# PERSISTED STATE
# -----------------------------------------------------------------------------------------------------------

    def load_persisted_state(self):
        """
        Load the gate-map-specific portion of persisted MMU state.

        Handles:
          - EndlessSpool config
          - TTG map
          - gate map
          - persisted selected gate/tool validation

        Returns:
            List[str]: any validation / length / sanity warnings encountered
        """
        errors = []
        var_manager = self.mmu.var_manager

        # Load EndlessSpool config
        self.endless_spool_enabled = var_manager.get(VARS_MMU_ENABLE_ENDLESS_SPOOL, self.endless_spool_enabled)
        endless_spool_groups = var_manager.get(VARS_MMU_ENDLESS_SPOOL_GROUPS, self.endless_spool_groups)
        if len(endless_spool_groups) == self.num_gates:
            self.endless_spool_groups = endless_spool_groups
        else:
            errors.append("Incorrect number of gates specified in %s" % VARS_MMU_ENDLESS_SPOOL_GROUPS)

        # Load TTG map
        tool_to_gate_map = var_manager.get(VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map)
        if len(tool_to_gate_map) == self.num_gates:
            self.ttg_map = tool_to_gate_map
        else:
            errors.append("Incorrect number of gates specified in %s" % VARS_MMU_TOOL_TO_GATE_MAP)

        # Load gate map
        for var, attr, _ in self._gate_map_vars:
            value = var_manager.get(var, getattr(self, attr))
            if len(value) == self.num_gates:
                setattr(self, attr, value)
            else:
                errors.append("Incorrect number of gates specified with %s" % var)
        self.update_gate_color_rgb()

        # Load selected gate and tool
        gate_selected = var_manager.get(VARS_MMU_GATE_SELECTED, self.mmu.gate_selected)
        tool_selected = var_manager.get(VARS_MMU_TOOL_SELECTED, self.mmu.tool_selected)
        if not (TOOL_GATE_BYPASS <= gate_selected < self.num_gates):
            if gate_selected != TOOL_GATE_UNKNOWN:
                errors.append("Invalid gate specified with %s or %s" % (VARS_MMU_TOOL_SELECTED, VARS_MMU_GATE_SELECTED))
            tool_selected = gate_selected = TOOL_GATE_UNKNOWN

        # No need for unknown gate on type-B MMU's (could also be first time bootup)
        if self.mmu.mmu_unit().multigear and gate_selected == TOOL_GATE_UNKNOWN:
            gate_selected = self.mmu.mmu_unit().first_gate

        selector = self.mmu.selector(gate_selected)
        if gate_selected != TOOL_GATE_UNKNOWN and not selector.is_homed:
            errors.append(f"Persisted gate/tool {gate_selected}/{tool_selected} dropped because selector isn't homed")
            tool_selected = gate_selected = TOOL_GATE_UNKNOWN

        self.mmu._set_gate_selected(gate_selected) # Will send gate_selected/unit_selected events to set active sensor map and activate unit
        self.mmu._set_tool_selected(tool_selected)
        self.ensure_ttg_match()                    # Ensure tool/gate consistency. Will change tool if necessary

        return errors


    def persist_ttg_map(self):
        self.mmu.var_manager.set(VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map, write=True)


    def persist_endless_spool(self):
        self.mmu.var_manager.set(VARS_MMU_ENABLE_ENDLESS_SPOOL, self.endless_spool_enabled)
        self.mmu.var_manager.set(VARS_MMU_ENDLESS_SPOOL_GROUPS, self.endless_spool_groups)
        self.mmu.var_manager.write()


    def persist_gate_status(self):
        self.mmu.var_manager.set(VARS_MMU_GATE_STATUS, self.gate_status, write=True)


    def persist_gate_map(self, spoolman_sync=False, gate_ids=None):
        self.mmu.var_manager.set(VARS_MMU_GATE_STATUS, self.gate_status)
        self.mmu.var_manager.set(VARS_MMU_GATE_FILAMENT_NAME, self.gate_filament_name)
        self.mmu.var_manager.set(VARS_MMU_GATE_MATERIAL, self.gate_material)
        self.mmu.var_manager.set(VARS_MMU_GATE_COLOR, self.gate_color)
        self.mmu.var_manager.set(VARS_MMU_GATE_TEMPERATURE, self.gate_temperature)
        self.mmu.var_manager.set(VARS_MMU_GATE_SPOOL_ID, self.gate_spool_id)
        self.mmu.var_manager.set(VARS_MMU_GATE_SPEED_OVERRIDE, self.gate_speed_override)
        self.mmu.var_manager.write()
        self.update_t_macros()

        # Also persist to spoolman db if pushing updates for visability
        if spoolman_sync:
            if self.p.spoolman_support == SPOOLMAN_PUSH:
                if gate_ids is None:
                    gate_ids = list(enumerate(self.gate_spool_id))
                if gate_ids:
                    self.mmu._spoolman_push_gate_map(gate_ids)
            elif self.p.spoolman_support == SPOOLMAN_READONLY:
                self.mmu._spoolman_update_filaments(gate_ids)

        self.mmu.led_manager.gate_map_changed(None) # Force full LED update
        if self.printer.lookup_object("gcode_macro %s" % self.p.mmu_event_macro, None) is not None:
            self.mmu.mmu_macro_event(MACRO_EVENT_GATE_MAP_CHANGED, "GATE=-1")


# -----------------------------------------------------------------------------------------------------------
# RUNOUT, ENDLESS SPOOL, TTG MAPPING and GATE HANDLING
# -----------------------------------------------------------------------------------------------------------

    def get_next_endless_spool_gate(self, tool, gate):
        group = self.endless_spool_groups[gate]
        next_gate = -1
        checked_gates = []
        for i in range(self.num_gates - 1):
            check = (gate + i + 1) % self.num_gates
            if self.endless_spool_groups[check] == group:
                checked_gates.append(check)
                if self.gate_status[check] != GATE_EMPTY:
                    next_gate = check
                    break
        alt_gates = "(checked gates: %s)" % ",".join(map(str, checked_gates))
        msg = "for T%d in EndlessSpool Group %s %s" % (tool, chr(ord('A') + group), alt_gates)
        return next_gate, msg


    # Use mmu entry (and gear) sensors to "correct" gate status
    # Return updated gate_status adjusted by sensor readings
    def validate_gate_status(self):
        v_gate_status = list(self.gate_status) # Ensure that webhooks sees get_status() change
        for gate, status in enumerate(v_gate_status):
            gear_detected = self.mmu.sensor_manager.check_gate_sensor(SENSOR_EXIT_PREFIX, gate)
            if gear_detected is True:
                v_gate_status[gate] = GATE_AVAILABLE
            else:
                pre_detected = self.mmu.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, gate)
                if pre_detected is True and status == GATE_EMPTY:
                    v_gate_status[gate] = GATE_UNKNOWN
                elif pre_detected is False and status != GATE_EMPTY:
                    v_gate_status[gate] = GATE_EMPTY
        self.gate_status = v_gate_status


    # Use post-mmu exit sensors to correct the selected gate.
    # Returns the unique detected gate index, or None if zero/multiple detected.
    def validate_gate_selected(self):
        gate = None
        for g in range(self.num_gates):
            if self.mmu.sensor_manager.check_all_sensors_before(FILAMENT_POS_START_BOWDEN, g, loading=True) is True:
                if gate is None:
                    gate = g
                else:
                    return None
        return gate


    # Remap a tool/gate relationship and gate filament availability
    def remap_tool(self, tool, gate, available=None):
        self.ttg_map = list(self.ttg_map) # Ensure that webhook sees get_status() change
        self.ttg_map[tool] = gate
        self.persist_ttg_map()
        self.ensure_ttg_match()
        self.update_slicer_color_rgb() # Indexed by gate
        if available is not None:
            self.set_gate_status(gate, available)


    # Find and set a tool that maps to gate (for recovery)
    def ensure_ttg_match(self):
        if self.mmu.gate_selected in [TOOL_GATE_UNKNOWN, TOOL_GATE_BYPASS]:
            self.mmu._set_tool_selected(self.mmu.gate_selected)
        else:
            possible_tools = [tool for tool in range(self.num_gates) if self.ttg_map[tool] == self.mmu.gate_selected]
            if possible_tools:
                if self.mmu.tool_selected not in possible_tools:
                    self.mmu.log_debug("Resetting tool selected to match TTG map for current gate (%d)" % self.mmu.gate_selected)
                    self.mmu._set_tool_selected(possible_tools[0])
            else:
                self.mmu.log_warning("Resetting tool selected to unknown because current gate (%d) isn't associated with tool in TTG map" % self.mmu.gate_selected)
                self.mmu._set_tool_selected(TOOL_GATE_UNKNOWN)


    def reset_ttg_map(self):
        self.mmu.log_debug("Resetting TTG map")
        self.ttg_map = list(self.p.default_ttg_map)
        self.persist_ttg_map()
        self.ensure_ttg_match()
        self.update_slicer_color_rgb() # Indexed by gate


    def reset_endless_spool(self):
        self.mmu.log_debug("Resetting Endless Spool mapping")
        self.endless_spool_enabled = self.p.endless_spool_enabled
        self.endless_spool_groups = list(self.p.default_endless_spool_groups)
        self.persist_endless_spool()


    def set_gate_status(self, gate, state):
        if 0 <= gate < self.num_gates:
            if state != self.gate_status[gate]:
                self.gate_status = list(self.gate_status) # Ensure that webhooks sees get_status() change
                self.gate_status[gate] = state
                self.persist_gate_status()
                self.mmu.led_manager.gate_map_changed(gate)
                self.mmu.mmu_macro_event(MACRO_EVENT_GATE_MAP_CHANGED, "GATE=%d" % gate)


    def reset_gate_map(self):
        self.mmu.log_debug("Resetting gate map")
        self.gate_status = list(self.p.default_gate_status)
        self.validate_gate_status()
        self.gate_filament_name = list(self.p.default_gate_filament_name)
        self.gate_material = list(self.p.default_gate_material)
        self.gate_color = list(self.p.default_gate_color)
        self.gate_temperature = list(self.p.default_gate_temperature)
        if self.p.spoolman_support in [SPOOLMAN_OFF, SPOOLMAN_PULL]:
            self.gate_spool_id = [-1] * self.num_gates
        else:
            self.gate_spool_id = list(self.p.default_gate_spool_id)
        self.gate_speed_override = list(self.p.default_gate_speed_override)
        self.update_gate_color_rgb()
        self.persist_gate_map(spoolman_sync=True)


    # Assign spool id to gate and clear from other gates returning list of changes
    def assign_spool_id(self, gate, spool_id):
        self.gate_spool_id[gate] = spool_id
        mod_gate_ids = [(gate, spool_id)]
        for i, sid in enumerate(self.gate_spool_id):
            if sid == spool_id and i != gate:
                self.gate_spool_id[i] = -1
                mod_gate_ids.append((i, -1))
        return mod_gate_ids


# -----------------------------------------------------------------------------------------------------------
# COLOR / MACRO SUPPORT
# -----------------------------------------------------------------------------------------------------------

    # Keep parallel RGB color map updated when color changes
    def update_gate_color_rgb(self):
        # Recalculate RGB map for easy LED support
        self.gate_color_rgb = [MmuColorUtils.color_to_rgb_tuple(i) for i in self.gate_color]


    # Keep parallel RGB color map updated when slicer color or TTG changes
    # Will also update the t_macro colors
    def update_slicer_color_rgb(self):
        self.slicer_color_rgb = [(0.,0.,0.)] * self.num_gates
        for tool_key, tool_value in self.slicer_tool_map['tools'].items():
            tool = int(tool_key)
            gate = self.ttg_map[tool]
            self.slicer_color_rgb[gate] = MmuColorUtils.color_to_rgb_tuple(tool_value['color'])
        self.update_t_macros()
        self.mmu.led_manager.gate_map_changed(None) # Force full LED update


    # Set 'color' and 'spool_id' variable on the Tx macro for Mainsail/Fluidd to pick up
    # We don't use SET_GCODE_VARIABLE because the macro variable may not exist ahead of time
    def update_t_macros(self):
        for tool in range(self.num_gates):
            gate = self.ttg_map[tool]
            t_macro = self.printer.lookup_object("gcode_macro T%d" % tool, None)

            if t_macro:
                t_vars = dict(t_macro.variables) # So Mainsail sees the update

                spool_id = self.gate_spool_id[gate]
                if (self.p.t_macro_color != T_MACRO_COLOR_OFF and
                    spool_id >= 0 and
                    self.p.spoolman_support != SPOOLMAN_OFF and
                    self.gate_status[gate] != GATE_EMPTY):

                    t_vars['spool_id'] = self.gate_spool_id[gate]
                else:
                    t_vars.pop('spool_id', None)

                if self.p.t_macro_color == T_MACRO_COLOR_SLICER:
                    st = self.slicer_tool_map['tools'].get(str(tool), None)
                    rgb_hex = MmuColorUtils.color_to_rgb_hex(st.get('color', None)) if st else None
                    if rgb_hex:
                        t_vars['color'] = rgb_hex
                    else:
                        t_vars.pop('color', None)

                elif self.p.t_macro_color in [T_MACRO_COLOR_GATEMAP, T_MACRO_COLOR_ALLGATES]:
                    rgb_hex = MmuColorUtils.color_to_rgb_hex(self.gate_color[gate])
                    if self.gate_status[gate] != GATE_EMPTY or self.p.t_macro_color == T_MACRO_COLOR_ALLGATES:
                        t_vars['color'] = rgb_hex
                    else:
                        t_vars.pop('color', None)

                else: # 'off' case
                    t_vars.pop('color', None)

                t_macro.variables = t_vars


    def clear_slicer_tool_map(self):
        skip = self.slicer_tool_map.get('skip_automap', False) if self.slicer_tool_map else False
        self.slicer_tool_map = {'tools': {}, 'referenced_tools': [], 'initial_tool': None, 'purge_volumes': [], 'total_toolchanges': None}
        self.restore_automap_option(skip)
        self.slicer_color_rgb = [(0.,0.,0.)] * self.num_gates
        self.update_t_macros() # Clear 'color' on Tx macros if displaying slicer colors
            

# -----------------------------------------------------------------------------------------------------------
# LOGGING FORMATTING HELPERS
# -----------------------------------------------------------------------------------------------------------

    def ttg_map_to_string(self, tool=None, show_groups=True):
        """
        Format the TTG map (and optionally EndlessSpool groups) into a human-readable string.

        Args:
            tool: Specify the specific tool to display else all tools will be displayed
            show_groups: Flag to include the endless spool groups if available
        """
        if show_groups:
            msg = "TTG Map & EndlessSpool Groups:\n"
        else:
            msg = "TTG Map:\n" # String used to filter in KS-HH

        num_tools = self.num_gates
        tools = range(num_tools) if tool is None else [tool]

        for i in tools:
            gate = self.ttg_map[i]
            filament_char = self.mmu._get_filament_char(gate, show_swatch=True)
            msg += "\n" if i and tool is None else ""
            msg += "T{:<2}-> Gate{:>2}({})".format(i, gate, filament_char)

            if show_groups and self.endless_spool_enabled:
                group = self.endless_spool_groups[gate]
                msg += " Group %s:" % chr(ord('A') + group)
                gates_in_group = [(j + gate) % num_tools for j in range(num_tools)]
                msg += " >".join("{:>2}".format(g) for g in gates_in_group if self.endless_spool_groups[g] == group)

            if i == self.mmu.tool_selected:
                msg += " [SELECTED]"
        return msg


    def es_groups_to_string(self, title=None):
        """
        Return a formatted string listing EndlessSpool groups and their member gates.

        Args:
            title: Optionally supply a non-default title
        """
        msg = "%s:\n" % title if title else "EndlessSpool Groups:\n"
        groups = {}
        for gate in range(self.num_gates):
            group = self.endless_spool_groups[gate]
            if group not in groups:
                groups[group] = [gate]
            else:
                groups[group].append(gate)
        msg += "\n".join(
            "Group %s: Gates: %s" % (chr(ord('A') + group), ", ".join(map(str, gates)))
            for group, gates in groups.items()
        )
        return msg


    def gate_map_to_string(self):
        """
        Format per-gate filament details into a readable summary.
        """
        msg = "Gates / Filaments:" # String used to filter in KlipperScreen-HH
        available_status = {
            GATE_AVAILABLE_FROM_BUFFER: "Buffered",
            GATE_AVAILABLE: "On spool",
            GATE_EMPTY: "Empty",
            GATE_UNKNOWN: "Unknown"
        }

        for g in range(self.num_gates):
            available = available_status[self.gate_status[g]]
            name = self.gate_filament_name[g] or "Unknown"
            material = self.gate_material[g] or "Unknown"
            color = MmuColorUtils.format_color(self.gate_color[g] or "n/a")
            temperature = self.gate_temperature[g] or "n/a"

            gate_fstr = ""
            filament_char = self.mmu._get_filament_char(g, show_swatch=True)
            tools = ",".join("T{}".format(t) for t in range(self.num_gates) if self.ttg_map[t] == g)
            tools_fstr = (" [{}]".format(tools) if tools else "")
            gate_fstr = "{}".format(g).ljust(2, UI_SPACE)
            gate_fstr = "{}({}){}:".format(gate_fstr, filament_char, tools_fstr).ljust(14 + len(filament_char), UI_SPACE)

            available_fstr = "{};".format(available).ljust(11, UI_SPACE)
            fil_fstr = "{} | {}{}C | {} | {}".format(material, temperature, UI_DEGREE, color, name)

            spool_option = (str(self.gate_spool_id[g]) if self.gate_spool_id[g] > 0 else "n/a")
            if self.p.spoolman_support == SPOOLMAN_OFF:
                spool_fstr = ""
            elif self.gate_spool_id[g] <= 0:
                spool_fstr = "Id: {};".format(spool_option).ljust(12, UI_SPACE)
            else:
                spool_fstr = "Id: {}".format(spool_option).ljust(8, UI_SPACE) + "--> "

            speed_fstr = " [Speed:{}%]".format(self.gate_speed_override[g]) if self.gate_speed_override[g] != 100 else ""
            extra_fstr = " [SELECTED]" if g == self.mmu.gate_selected else ""

            msg += "\n{}{}{}{}{}{}".format(gate_fstr, available_fstr, spool_fstr, fil_fstr, speed_fstr, extra_fstr)
        return msg


# -----------------------------------------------------------------------------------------------------------
# AUTOMAP SUPPORT
# -----------------------------------------------------------------------------------------------------------

    def automap_gate(self, tool, strategy):
        if tool is None:
            self.mmu.log_error("Automap tool called without a tool argument")
            return
        tool_to_remap = self.slicer_tool_map['tools'][str(tool)]

        # strategy checks
        if strategy in ['spool_id']:
            self.mmu.log_error("'%s' automapping strategy is not yet supported. Support for this feature is on the way, please be patient." % strategy)
            return

        # Create printable strategy string
        strategy_str = strategy.replace("_", " ").title()

        # Deduct search_in and tool_field based on strategy
        # tool fields are like {'color': color, 'material': material, 'temp': temp, 'name': name, 'in_use': used}
        if strategy == AUTOMAP_FILAMENT_NAME:
            search_in = self.gate_filament_name
            tool_field = 'name'
        elif strategy == AUTOMAP_SPOOL_ID:
            search_in = self.gate_spool_id
            tool_field = 'spool_id' # Placeholders for future support
        elif strategy == AUTOMAP_MATERIAL:
            search_in = self.gate_material
            tool_field = 'material'
        elif strategy in [AUTOMAP_CLOSEST_COLOR, AUTOMAP_COLOR]:
            search_in = self.gate_color
            tool_field = 'color'
        else:
            self.mmu.log_error("Invalid automap strategy '%s'" % strategy)
            return

        # Automapping logic
        errors = []
        warnings = []
        messages = []
        remaps = []

        if not tool_to_remap[tool_field]:
            errors.append("%s of tool %s must be set. When using automapping all referenced tools must have a %s" % (tool_field, tool, strategy_str))

        if not errors:
            # 'standard' exactly matching fields
            if strategy != AUTOMAP_CLOSEST_COLOR:
                for gn, gate_feature in enumerate(search_in):
                    # When matching by name normalize possible unicode characters and match case-insensitive
                    if strategy == AUTOMAP_FILAMENT_NAME:
                        equal = self._compare_unicode(tool_to_remap[tool_field], gate_feature)
                    elif strategy == AUTOMAP_COLOR:
                        equal = tool_to_remap[tool_field].upper().ljust(8,'F') == gate_feature.upper().ljust(8,'F')
                    else:
                        equal = tool_to_remap[tool_field] == gate_feature
                    if equal:
                        remaps.append("T%s --> G%s (%s)" % (tool, gn, gate_feature))
                        self.mmu.wrap_gcode_command("MMU_TTG_MAP TOOL=%d GATE=%d QUIET=1" % (tool, gn))
                if not remaps:
                    errors.append("No gates found for tool %s with %s %s" % (tool, strategy_str, tool_to_remap[tool_field]))

            # 'colors' search for closest
            elif strategy == AUTOMAP_CLOSEST_COLOR:
                if tool_to_remap['material'] == "unknown":
                    errors.append("When automapping with closest color, the tool material must be set.")
                if tool_to_remap['material'] not in self.gate_material:
                    errors.append("No gate has a filament matching the desired material (%s). Available are : %s" % (tool_to_remap['material'], self.gate_material))
                if not errors:
                    color_list = []
                    for gn, color in enumerate(search_in):
                        gm = "".join(self.gate_material[gn].strip()).replace('#', '').lower()
                        if gm == tool_to_remap['material'].lower():
                            color_list.append(color)
                    if not color_list:
                        errors.append("Gates with %s are missing color information..." % tool_to_remap['material'])

                if not errors:
                    closest, distance = MmuColorUtils.find_closest_color(tool_to_remap['color'], color_list)
                    for gn, color in enumerate(search_in):
                        gm = "".join(self.gate_material[gn].strip()).replace('#', '').lower()
                        if gm == tool_to_remap['material'].lower():
                            if closest == color:
                                t = self.p.console_gate_stat
                                if distance > 0.5:
                                    warnings.append("Color matching is significantly different ! %s" % (UI_EMOTICONS[7] if t == 'emoticon' else ''))
                                elif distance > 0.2:
                                    warnings.append("Color matching might be noticebly different %s" % (UI_EMOTICONS[5] if t == 'emoticon' else ''))
                                elif distance > 0.05:
                                    warnings.append("Color matching seems quite good %s" % (UI_EMOTICONS[3] if t == 'emoticon' else ''))
                                elif distance > 0.02:
                                    warnings.append("Color matching is excellent %s" % (UI_EMOTICONS[2] if t == 'emoticon' else ''))
                                elif distance < 0.02:
                                    warnings.append("Color matching is perfect %s" % (UI_EMOTICONS[1] if t == 'emoticon' else ''))
                                remaps.append("T%s --> G%s (%s with closest color: %s)" % (tool, gn, gm, color))
                                self.mmu.wrap_gcode_command("MMU_TTG_MAP TOOL=%d GATE=%d QUIET=1" % (tool, gn))

                if not remaps:
                    errors.append("Unable to find a suitable color for tool %s (color: %s)" % (tool, tool_to_remap['color']))

            if len(remaps) > 1:
                warnings.append("Multiple gates found for tool %s with %s '%s'" % (tool, strategy_str, tool_to_remap[tool_field]))

        # Display messages while automapping
        if remaps:
            remaps.insert(0, "Automatically mapped tool %s based on %s" % (tool, strategy_str))
            for msg in remaps:
                self.mmu.log_always(msg)
        if messages:
            for msg in messages:
                self.mmu.log_always(msg)

        # Display warnings while automapping
        for msg in warnings:
            self.mmu.log_info(msg)

        # Display errors while automapping
        if errors:
            reason = ["Error during automapping"]
            if self.mmu.is_printing():
                self.mmu.handle_mmu_error("\n".join(reason+errors))
            else:
                self.mmu.log_error(reason[0])
                for e in errors:
                    self.mmu.log_error(e)


    # Helper to compare unicode strings with optional case insensitivity
    def _compare_unicode(self, a, b, case_insensitive=True):
        a = unicodedata.normalize('NFKC', a)
        b = unicodedata.normalize('NFKC', b)
        if case_insensitive:
            a = a.lower()
            b = b.lower()
        return a == b


    def restore_automap_option(self, skip=False):
        self.slicer_tool_map['skip_automap'] = skip


# -----------------------------------------------------------------------------------------------------------

    def renew_gate_map(self):
        """
        Helper to ensure that webhooks sees get_status() change after gate map update.
        """
        self.gate_status = list(self.gate_status)
        self.gate_filament_name = list(self.gate_filament_name)
        self.gate_material = list(self.gate_material)
        self.gate_color = list(self.gate_color)
        self.gate_temperature = list(self.gate_temperature)
        self.gate_spool_id = list(self.gate_spool_id)
        self.gate_speed_override = list(self.gate_speed_override)


    def get_status(self, eventtime):
        return {
            'ttg_map': self.ttg_map,

            'endless_spool_groups': self.endless_spool_groups,
            'endless_spool_enabled': self.endless_spool_enabled,
            'endless_spool': self.endless_spool_enabled, # DEPRECATED But still used by klipperscreen (and in interface for Mainsail/Fluidd)

            'gate_status': self.gate_status,
            'gate_filament_name': self.gate_filament_name,
            'gate_material': self.gate_material,
            'gate_color': self.gate_color,
            'gate_temperature': self.gate_temperature,
            'gate_spool_id': self.gate_spool_id,
            'gate_speed_override': self.gate_speed_override,
            'gate_color_rgb': self.gate_color_rgb,

            'slicer_tool_map': self.slicer_tool_map,
            'slicer_color_rgb': self.slicer_color_rgb,
        }
