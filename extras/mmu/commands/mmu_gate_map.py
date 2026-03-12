# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_GATE_MAP command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import ast

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuGateMapCommand(BaseCommand):

    CMD = "MMU_GATE_MAP"

    HELP_BRIEF = "Display or define the type and color of filaments on each gate"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "QUIET        = [0|1]\n"
        + "DETAIL       = [0|1]\n"
        + "RESET        = [0|1]\n"
        + "GATES        = comma,separated,list OR single GATE\n"
        + "MAP          = dict string for bulk update (hidden)\n"
        + "REPLACE      = [0|1] (hidden - bulk replace)\n"
        + "FROM_SPOOLMAN= [0|1] (hidden)\n"
        + "GATE         = g\n"
        + "NEXT_SPOOLID = id\n"
        + "NAME         = # Filament name\n"
        + "MATERIAL     = # Material type\n"
        + "COLOR        = # Filament color as w3c name or RRGGBB or RRGGBBaa (without #)\n"
        + "SPOOLID      = # Optionally the spoolman ID for the filament (don't need to specify other attributes)\n"
        + "TEMP         = # Default temperature of filament\n"
        + "SPEED        = % Speed override (use <100 for soft TPU types)\n"
        + "AVAILABLE    = [-1|0|1|2] Filament availability: Unknown | Empty | Available | Available from filament buffer\n"
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
        # BaseCommand wrapper already logs commandline + handles HELP=1.

        if self.mmu.check_if_disabled(): return

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        gates = gcmd.get('GATES', "!")
        gmapstr = gcmd.get('MAP', "{}")                                # Hidden option for bulk filament update (from moonraker/ui components)
        replace = bool(gcmd.get_int('REPLACE', 0, minval=0, maxval=1)) # Hidden option for bulk filament update from spoolman
        from_spoolman = bool(gcmd.get_int('FROM_SPOOLMAN', 0, minval=0, maxval=1)) # Hidden option for bulk filament update from spoolman
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu.num_gates - 1)
        next_spool_id = gcmd.get_int('NEXT_SPOOLID', None, minval=-1)

        gate_map = None
        try:
            gate_map = ast.literal_eval(gmapstr)
        except Exception as e:
            self.mmu.log_error("Recieved unparsable gate map update. See log for more details")
            self.mmu.log_debug("Exception whilst parsing gate map in MMU_GATE_MAP: %s" % str(e))
            return

        if reset:
            self.mmu._reset_gate_map()
        else:
            self.mmu._renew_gate_map() # Ensure that webhooks sees changes

        if next_spool_id:
            if self.mmu.p.spoolman_support != SPOOLMAN_PULL:
                if next_spool_id > 0:
                    self.mmu.pending_spool_id = next_spool_id
                    self.mmu.reactor.update_timer(self.mmu.pending_spool_id_timer, self.mmu.reactor.monotonic() + self.mmu.p.pending_spool_id_timeout)
                else:
                    # Disable timer to prevent reuse
                    self.mmu.pending_spool_id = -1
                    self.mmu.reactor.update_timer(self.mmu.pending_spool_id_timer, self.mmu.reactor.NEVER)
            else:
                self.mmu.log_error("Cannot use use NEXT_SPOOLID feature with spoolman_support: pull. Use 'push' or 'readonly' modes")
                return

        changed_gate_ids = []

        if gate_map: # --------- BATCH UPDATE from spoolman or UI --------
            try:
                self.mmu.log_debug("Received gate map update (replace: %s)" % replace)
                if replace:
                    # Replace complete map including spool_id (should only be in spoolman "pull" mode)
                    if self.mmu.p.spoolman_support != SPOOLMAN_PULL:
                        self.mmu.log_assertion("Received gate map replacement update but not in spoolman 'pull' mode")

                    # If from spoolman gate_map should be a full gate list with spool_id = -1 for unset gates
                    for gate_idx, fil in gate_map.items():
                        if not (0 <= gate_idx < self.mmu.num_gates):
                            self.mmu.log_assertion("Illegal gate number %d supplied in gate map update - ignored" % gate_idx)
                            continue

                        # Update gate attributes if we have valid spool_id
                        spool_id = self.mmu.safe_int(fil.get('spool_id', -1))
                        self.mmu.gate_spool_id[gate_idx] = spool_id
                        self.mmu.gate_filament_name[gate_idx] = fil.get('name', '')
                        self.mmu.gate_material[gate_idx] = fil.get('material', '')
                        self.mmu.gate_color[gate_idx] = fil.get('color', '')
                        self.mmu.gate_temperature[gate_idx] = max(
                            self.mmu.safe_int(fil.get('temp', self.mmu.p.default_extruder_temp)),
                            self.mmu.p.default_extruder_temp
                        )
                        # gate_speed_override and gate_status can be set locally
                else:
                    # Update map (ui or from spoolman in "readonly" and "push" modes)
                    ids_dict = {}
                    for gate_idx, fil in gate_map.items():
                        if not (0 <= gate_idx < self.mmu.num_gates):
                            self.mmu.log_assertion("Illegal gate number %d supplied in gate map update - ignored" % gate_idx)
                            continue

                        spool_id = self.mmu.safe_int(fil.get('spool_id', -1))
                        if (not from_spoolman or spool_id != -1):
                            # Update attributes but don't allow spoolman to accidently clear
                            self.mmu.gate_filament_name[gate_idx] = fil.get('name', '')
                            self.mmu.gate_material[gate_idx] = fil.get('material', '')
                            self.mmu.gate_color[gate_idx] = fil.get('color', '')
                            self.mmu.gate_temperature[gate_idx] = max(
                                self.mmu.safe_int(fil.get('temp', self.mmu.p.default_extruder_temp)),
                                self.mmu.p.default_extruder_temp
                            )
                            self.mmu.gate_speed_override[gate_idx] = self.mmu.safe_int(fil.get('speed_override', self.mmu.gate_speed_override[gate_idx]))
                            self.mmu.gate_status[gate_idx] = self.mmu.safe_int(fil.get('status', self.mmu.gate_status[gate_idx])) # For UI manual fixing of availabilty

                        # If spool_id has changed, clean up possible stale use of old one
                        if spool_id != self.mmu.gate_spool_id[gate_idx]:
                            self.mmu.log_debug("Spool_id changed for gate %d in MMU_GATE_MAP" % gate_idx)
                            mod_gate_ids = self.mmu.assign_spool_id(gate_idx, spool_id)
                            for (g, sid) in mod_gate_ids:
                                ids_dict[g] = sid

                    changed_gate_ids = list(ids_dict.items())

            except Exception as e:
                self.mmu.log_debug("Invalid MAP parameter: %s\nException: %s" % (gate_map, str(e)))
                raise gcmd.error("Invalid MAP parameter. See mmu.log for details")

        elif gates != "!" or gate >= 0:
            gatelist = []
            if gates != "!":
                # List of gates
                try:
                    for gate_str in gates.split(','):
                        gate_idx = int(gate_str)
                        if 0 <= gate_idx < self.mmu.num_gates:
                            gatelist.append(gate_idx)
                except ValueError:
                    raise gcmd.error("Invalid GATES parameter: %s" % gates)
            else:
                # Specifying one gate (filament)
                gatelist.append(gate)

            ids_dict = {}
            for gate_idx in gatelist:
                available = gcmd.get_int('AVAILABLE', self.mmu.gate_status[gate_idx], minval=-1, maxval=2)
                name = gcmd.get('NAME', None)
                material = gcmd.get('MATERIAL', None)
                color = gcmd.get('COLOR', None)
                spool_id = gcmd.get_int('SPOOLID', None, minval=-1)
                temperature = gcmd.get_int('TEMP', int(self.mmu.p.default_extruder_temp))
                speed_override = gcmd.get_int('SPEED', self.mmu.gate_speed_override[gate_idx], minval=10, maxval=150)

                if self.mmu.p.spoolman_support != SPOOLMAN_PULL:
                    # Local gate map, can update attributes
                    spool_id = spool_id or self.mmu.gate_spool_id[gate_idx]
                    name = name if name is not None else self.mmu.gate_filament_name[gate_idx]
                    material = (material if material is not None else self.mmu.gate_material[gate_idx]).upper()
                    color = (color if color is not None else self.mmu.gate_color[gate_idx]).lower()
                    temperature = temperature or self.mmu.gate_temperature[gate_idx]
                    color = self.mmu._validate_color(color)
                    if color is None:
                        raise gcmd.error("Color specification must be in form 'rrggbb' or 'rrggbbaa' hexadecimal value (no '#') or valid color name or empty string")
                    self.mmu.gate_filament_name[gate_idx] = name
                    self.mmu.gate_material[gate_idx] = material
                    self.mmu.gate_color[gate_idx] = color
                    self.mmu.gate_temperature[gate_idx] = temperature
                    self.mmu.gate_speed_override[gate_idx] = speed_override
                    self.mmu.gate_status[gate_idx] = available

                    if spool_id != self.mmu.gate_spool_id[gate_idx]:
                        self.mmu.log_debug("Spool_id changed for gate %d in MMU_GATE_MAP" % gate_idx)
                        mod_gate_ids = self.mmu.assign_spool_id(gate_idx, spool_id)
                        for (g, sid) in mod_gate_ids:
                            ids_dict[g] = sid

                else:
                    # Remote (spoolman) gate map, don't update local attributes that are set by spoolman
                    self.mmu.gate_status[gate_idx] = available
                    self.mmu.gate_speed_override[gate_idx] = speed_override
                    if any(x is not None for x in [material, color, spool_id, name]):
                        self.mmu.log_error("Spoolman mode is '%s': Can only set gate status and speed override locally\nUse MMU_SPOOLMAN or update spoolman directly" % SPOOLMAN_PULL)
                        return

            changed_gate_ids = list(ids_dict.items())

        # Ensure everything is synced
        self.mmu._update_gate_color_rgb()

        # Caution, make sure that an update from spoolman does end up in infinite loop!
        self.mmu._persist_gate_map(spoolman_sync=bool(changed_gate_ids) and not from_spoolman, gate_ids=changed_gate_ids) # This will also update LED status

        if not quiet:
            self.mmu.log_always(self.mmu._gate_map_to_string(detail), color=True)
