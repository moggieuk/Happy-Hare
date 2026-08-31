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
from ..mmu_utils       import MmuError, MmuColorUtils
from .mmu_base_command import *


class MmuGateMapCommand(BaseCommand):

    CMD = "MMU_GATE_MAP"

    HELP_BRIEF = "Display or define the type and color of filaments on each gate"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "QUIET        = 1 To minimize console reporting\n"
        + "DETAILS      = 1 Include the complete Spoolman RFID UID set for each gate\n"
        + "RESET        = 1 To reset specified GATE/GATES filament attributes to configured defaults\n"
        + "GATES        = g,g,g comma separated list of gates; required with RESET unless GATE is used\n"
        + "GATE         = g Specify a single gate; required with RESET unless GATES is used\n"
        + "BYPASS       = 1 Set filament attributes for the bypass\n"
        + "NEXT_SPOOLID = id Specify the spoolman id of the next filament loaded - automatically assigned (0 to cancel)\n"
        + "NAME         = # Filament name\n"
        + "MATERIAL     = # Material type\n"
        + "VENDOR       = # Filament vendor/brand name\n"
        + "COLOR        = # Filament color as w3c name or RRGGBB or RRGGBBaa (without #)\n"
        + "SPOOLID      = # Optionally the spoolman ID for the filament (don't need to specify other attributes)\n"
        + "TEMP         = # Default temperature of filament\n"
        + "SPEED        = % Speed override (use <100 for soft TPU types)\n"
        + "RFID         = # Single hexadecimal RFID tag UID read at the gate (blank to clear)\n"
        + "AVAILABLE    = [-1|0|1|2] Filament availability: Unknown | Empty | Available | Available from filament buffer\n"
        + "(no parameters for status report)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} DETAILS=1                      ...Display the gate map with all cached RFID UIDs\n"
        + f"{CMD} GATES=0,1,2,3 AVAILABLE=1      ...Mark gates 0-3 as having filament available\n"
        + f"{CMD} GATE=5 COLOR=red MATERIAL=pla  ...Set filament attributes for gate 5\n"
        + f"{CMD} NEXT_SPOOLID=45                ...Automatically mark the next spool preloaded or loaded with spoolman id 45\n"
        + f"{CMD} GATE=0 SPEED=50                ...Set load/unload speed of gate 0 to 50% - great for TPU!\n"
        + f"{CMD} GATE=0 RFID=E2003412           ...Record the RFID tag read for the spool loaded in gate 0\n"
        + f"{CMD} RESET=1 GATES=4,5              ...Reset gates 4 and 5 to defaults configured in mmu.cfg\n"
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
        mmu = self.mmu

        if self.check_if_disabled(): return

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        details = bool(gcmd.get_int('DETAILS', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        gates = gcmd.get('GATES', "!")
        gmapstr = gcmd.get('MAP', "{}")                                # Hidden option for bulk filament update (from moonraker/ui components)
        replace = bool(gcmd.get_int('REPLACE', 0, minval=0, maxval=1)) # Hidden option for bulk filament update from spoolman
        from_spoolman = bool(gcmd.get_int('FROM_SPOOLMAN', 0, minval=0, maxval=1)) # Hidden option for bulk filament update from spoolman
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=mmu.num_gates - 1)
        bypass = bool(gcmd.get_int('BYPASS', 0, minval=0, maxval=1)) # Target 'active_filament' for the bypass (no gate-map row)
        next_spool_id = gcmd.get_int('NEXT_SPOOLID', None, minval=-2)
        lookup = gcmd.get_int('LOOKUP', None, minval=-2, maxval=-1)  # Hidden: failed per-gate lookup result from Moonraker
        created = bool(gcmd.get_int('CREATED', 0, minval=0, maxval=1)) # Set by Moonraker when the UID minted a new spool
        rfids = gcmd.get('RFIDS', None)                              # Hidden: complete UID set registered to the resolved spool

        gate_map = None
        try:
            gate_map = ast.literal_eval(gmapstr)
        except Exception as e:
            mmu.log_error("Received unparsable gate map update. See log for more details")
            mmu.log_debug("Exception whilst parsing gate map in MMU_GATE_MAP: %s" % str(e))
            return

        if reset and gates == "!" and gate < 0:
            raise gcmd.error("RESET=1 requires GATE or GATES")

        if next_spool_id is not None:
            # Completion of an in-flight shared NFC lookup (or a manual assignment/cancel).
            #   >0  success - assign as the pending spool
            #    0  manual user cancellation (spool ids are 1-based)
            #   -1  recoverable failure (e.g. Spoolman comms) - allow immediate re-read
            #   -2  definitive "unknown tag" - release guard but don't re-read
            if next_spool_id <= 0:
                # 0 (or a negative with no lookup in flight) is a deliberate cancel, not a failure
                failed_lookup = mmu.nfc_lookup_pending and next_spool_id < 0
                mmu.set_pending_spool_id(-1) # Cancel any stale pending assignment
                if failed_lookup:
                    mmu._nfc_led_on_fail()   # Shared-reader lookup failed -> failure flash
                    # Surface to console too (LED alone is easy to miss), matching per-gate
                    if next_spool_id == -2:
                        mmu.log_error("NFC: scanned tag is not registered against any spool in Spoolman")
                    else:
                        mmu.log_error("NFC: could not reach Spoolman to resolve scanned tag - will re-read")
                reread = (next_spool_id == -1)
            elif mmu.p.spoolman_support != SPOOLMAN_PULL:
                mmu.set_pending_spool_id(next_spool_id)
                if created:
                    mmu.log_always("Spool ID: created new Spoolman spool %d for scanned tag" % next_spool_id)
                reread = False
            else:
                mmu.log_error("Cannot use NEXT_SPOOLID feature with spoolman_support: pull. Use 'push' or 'readonly' modes")
                reread = False
            mmu.nfc_lookup_resolved(reread=reread)
            return

        if lookup is not None:
            # Failed PER-GATE NFC lookup result from Moonraker. The gate map is untouched;
            # LED fail flash (queued behind the gate's read flash) + console error, matching
            # the shared-reader failure feedback.
            #   -1  recoverable failure (e.g. Spoolman comms)
            #   -2  definitive "unknown tag"
            if gate >= 0:
                mmu._nfc_led_on_gate_fail(gate)
                if lookup == -2:
                    mmu.log_error("NFC: scanned tag on gate %d is not registered against any spool in Spoolman" % gate)
                else:
                    mmu.log_error("NFC: could not reach Spoolman to resolve scanned tag on gate %d" % gate)
            return

        if bypass:
            # Filament attributes for the bypass "gate" -> active_filament only (there is no
            # gate-map row). Normally sent by Moonraker (async, after a bypass spool
            # activation requested attributes) but can also be set manually
            if mmu.gate_selected != TOOL_GATE_BYPASS:
                mmu.log_debug("Ignoring bypass filament attribute update - bypass no longer selected")
                return
            color = gcmd.get('COLOR', '').lower()
            validated_color = MmuColorUtils.validate_color(color)
            if validated_color is None:
                mmu.log_debug("Invalid COLOR '%s' in bypass filament update - ignored" % color)
                validated_color = ''
            mmu.active_filament = {
                'filament_name': gcmd.get('NAME', ''),
                'material': gcmd.get('MATERIAL', '').upper(),
                'vendor': gcmd.get('VENDOR', ''),
                'color': validated_color,
                'spool_id': gcmd.get_int('SPOOLID', -1),
                'temperature': max(gcmd.get_int('TEMP', int(mmu.p.default_extruder_temp)), int(mmu.p.default_extruder_temp)),
            }
            if not quiet:
                mmu.log_always("Bypass filament attributes updated")
            return

        # Ensure webhooks always sees a change if we edit the map (the callback-style
        # blocks above don't touch the gate map, so they skip this churn)
        mmu.gate_maps.renew_gate_map()

        changed_gate_ids = []

        gatelist = []
        if gates != "!":
            try:
                for gate_str in gates.split(','):
                    gate_idx = int(gate_str)
                    if not 0 <= gate_idx < mmu.num_gates:
                        if reset:
                            raise ValueError
                        continue
                    gatelist.append(gate_idx)
            except ValueError:
                raise gcmd.error("Invalid GATES parameter: %s" % gates)
        elif gate >= 0:
            gatelist.append(gate)

        if reset:
            mmu.gate_maps.reset_gate_map(gatelist)
            if not quiet:
                mmu.log_always(mmu.gate_maps.gate_map_to_string(details=details), color=True)
            return

        if gate_map: # --------- BATCH UPDATE from spoolman or UI --------
            try:
                mmu.log_debug("Received gate map update (replace: %s)" % replace)
                if replace:
                    # Replace complete map including spool_id (should only be in spoolman "pull" mode)
                    if mmu.p.spoolman_support != SPOOLMAN_PULL:
                        mmu.log_assertion("Received gate map replacement update but not in spoolman 'pull' mode")

                    # If from spoolman gate_map should be a full gate list with spool_id = -1 for unset gates
                    for gate_idx, fil in gate_map.items():
                        if not (0 <= gate_idx < mmu.num_gates):
                            mmu.log_assertion("Illegal gate number %d supplied in gate map update - ignored" % gate_idx)
                            continue

                        # Update gate attributes if we have valid spool_id
                        spool_id = self._safe_int(fil.get('spool_id', -1))
                        mmu.gate_spool_id[gate_idx] = spool_id
                        mmu.gate_filament_name[gate_idx] = fil.get('name', '')
                        mmu.gate_material[gate_idx] = fil.get('material', '')
                        mmu.gate_vendor[gate_idx] = fil.get('vendor', '')
                        mmu.gate_color[gate_idx] = fil.get('color', '')
                        mmu.gate_temperature[gate_idx] = max(
                            self._safe_int(fil.get('temp', mmu.p.default_extruder_temp)),
                            mmu.p.default_extruder_temp
                        )
                        # The observed RFID is local hardware state and is never supplied by
                        # Spoolman. A non-Spoolman map may still set one validated UID explicitly.
                        if not from_spoolman and 'rfid' in fil:
                            self._set_gate_rfid(gate_idx, fil.get('rfid'))
                        # REPLACE is authoritative for spool assignments, so absent aliases
                        # clear any set belonging to the outgoing spool.
                        mmu.gate_maps.set_gate_rfid_aliases(gate_idx, fil.get('rfids', ''))
                        # gate_speed_override and gate_status can be set locally
                else:
                    # Update map (ui or from spoolman in "readonly" and "push" modes)
                    ids_dict = {}
                    for gate_idx, fil in gate_map.items():
                        if not (0 <= gate_idx < mmu.num_gates):
                            mmu.log_assertion("Illegal gate number %d supplied in gate map update - ignored" % gate_idx)
                            continue

                        spool_id = self._safe_int(fil.get('spool_id', -1))
                        if (not from_spoolman or spool_id != -1):
                            # Update attributes but don't allow spoolman to accidentally clear
                            status = self._safe_int(fil.get('status', mmu.gate_status[gate_idx]))
                            if status == GATE_EMPTY and mmu.gate_status[gate_idx] != GATE_EMPTY:
                                mmu.gate_maps.clear_gate_attributes(gate_idx)
                                ids_dict[gate_idx] = -1
                            mmu.gate_filament_name[gate_idx] = fil.get('name', '')
                            mmu.gate_material[gate_idx] = fil.get('material', '')
                            mmu.gate_vendor[gate_idx] = fil.get('vendor', '')
                            mmu.gate_color[gate_idx] = fil.get('color', '')
                            mmu.gate_temperature[gate_idx] = max(
                                self._safe_int(fil.get('temp', mmu.p.default_extruder_temp)),
                                mmu.p.default_extruder_temp
                            )
                            mmu.gate_speed_override[gate_idx] = self._safe_int(fil.get('speed_override', mmu.gate_speed_override[gate_idx]))
                            mmu.gate_status[gate_idx] = status # For UI manual fixing of availability

                        # Only a local/UI map may update the observed UID. Spoolman
                        # supplies its complete UID set through rfids instead.
                        if not from_spoolman:
                            if 'rfid' in fil:
                                self._set_gate_rfid(gate_idx, fil.get('rfid'))

                        # If spool_id has changed, clean up possible stale use of old one
                        if spool_id != mmu.gate_spool_id[gate_idx]:
                            mmu.log_debug("Spool_id changed for gate %d in MMU_GATE_MAP" % gate_idx)
                            mod_gate_ids = mmu.gate_maps.assign_spool_id(gate_idx, spool_id)
                            for (g, sid) in mod_gate_ids:
                                ids_dict[g] = sid

                        # Apply after assign_spool_id(), which clears aliases whenever the
                        # spool changes.
                        if 'rfids' in fil:
                            mmu.gate_maps.set_gate_rfid_aliases(gate_idx, fil.get('rfids'))

                    changed_gate_ids = list(ids_dict.items())

            except Exception as e:
                mmu.log_debug("Invalid MAP parameter: %s\nException: %s" % (gate_map, str(e)))
                raise gcmd.error("Invalid MAP parameter. See mmu.log for details")

        elif gatelist:
            ids_dict = {}
            for gate_idx in gatelist:
                available = gcmd.get_int('AVAILABLE', mmu.gate_status[gate_idx], minval=-1, maxval=2)
                name = gcmd.get('NAME', None)
                material = gcmd.get('MATERIAL', None)
                vendor = gcmd.get('VENDOR', None)
                color = gcmd.get('COLOR', None)
                spool_id = gcmd.get_int('SPOOLID', None, minval=-1)
                temperature = gcmd.get_int('TEMP', int(mmu.p.default_extruder_temp))
                speed_override = gcmd.get_int('SPEED', None, minval=10, maxval=150)
                rfid = gcmd.get('RFID', None)

                if available == GATE_EMPTY and mmu.gate_status[gate_idx] != GATE_EMPTY:
                    mmu.gate_maps.clear_gate_attributes(gate_idx)
                    ids_dict[gate_idx] = -1

                # RFID is one locally observed UID. Invalid values, including lists,
                # are reported and ignored without disturbing the existing gate value.
                if rfid is not None:
                    self._set_gate_rfid(gate_idx, rfid)

                if mmu.p.spoolman_support != SPOOLMAN_PULL:
                    # Local gate map, can update attributes
                    spool_id = spool_id or mmu.gate_spool_id[gate_idx]
                    name = name if name is not None else mmu.gate_filament_name[gate_idx]
                    material = (material if material is not None else mmu.gate_material[gate_idx]).upper()
                    vendor = vendor if vendor is not None else mmu.gate_vendor[gate_idx]
                    color = (color if color is not None else mmu.gate_color[gate_idx]).lower()
                    temperature = temperature or mmu.gate_temperature[gate_idx]
                    speed_override = speed_override if speed_override is not None else mmu.gate_speed_override[gate_idx]
                    color = MmuColorUtils.validate_color(color)
                    if color is None:
                        raise gcmd.error("Color specification must be in form 'rrggbb' or 'rrggbbaa' hexadecimal value (no '#') or valid color name or empty string")
                    mmu.gate_filament_name[gate_idx] = name
                    mmu.gate_material[gate_idx] = material
                    mmu.gate_vendor[gate_idx] = vendor
                    mmu.gate_color[gate_idx] = color
                    mmu.gate_temperature[gate_idx] = temperature
                    mmu.gate_speed_override[gate_idx] = speed_override
                    mmu.gate_status[gate_idx] = available

                    if spool_id != mmu.gate_spool_id[gate_idx]:
                        mmu.log_debug("Spool_id changed for gate %d in MMU_GATE_MAP" % gate_idx)
                        mod_gate_ids = mmu.gate_maps.assign_spool_id(gate_idx, spool_id)
                        for (g, sid) in mod_gate_ids:
                            ids_dict[g] = sid

                    if rfids is not None:
                        mmu.gate_maps.set_gate_rfid_aliases(gate_idx, rfids)

                    # A per-gate NFC scan that auto-created a Spoolman spool (console log
                    # complements the gate's LED feedback)
                    if created and spool_id and spool_id > 0:
                        mmu.log_always("Spool ID: created new Spoolman spool %d for scanned tag (gate %d)" % (spool_id, gate_idx))

                else:
                    # Remote (spoolman) gate map, don't update local attributes that are set by spoolman
                    if rfids is not None:
                        mmu.gate_maps.set_gate_rfid_aliases(gate_idx, rfids)
                    mmu.gate_status[gate_idx] = available
                    if speed_override is not None:
                        mmu.gate_speed_override[gate_idx] = speed_override
                    if any(x is not None for x in [material, vendor, color, spool_id, name]):
                        mmu.log_error("Spoolman mode is '%s': Can only set gate status and speed override locally\nUse MMU_SPOOLMAN or update spoolman directly" % SPOOLMAN_PULL)
                        break

            changed_gate_ids = list(ids_dict.items())

        # Ensure everything is synced
        mmu.gate_maps.update_gate_color_rgb()

        # Caution, make sure that an update from spoolman does end up in infinite loop!
        mmu.gate_maps.persist_gate_map(spoolman_sync=bool(changed_gate_ids) and not from_spoolman, gate_ids=changed_gate_ids) # This will also update LED status

        if not quiet:
            mmu.log_always(mmu.gate_maps.gate_map_to_string(details=details), color=True)


    # Helper to ensure int when strings may be passed from UI
    def _safe_int(self, i, default=0):
        try:
            return int(i)
        except ValueError:
            return default


    def _set_gate_rfid(self, gate, rfid):
        """Apply one valid UID (or blank clear); invalid/list values are ignored."""
        normalized = self.mmu.gate_maps.normalize_gate_rfid(rfid)
        if normalized is None:
            self.mmu.log_error(
                "Ignoring invalid RFID value for gate %d: expected one even-length hexadecimal UID; "
                "comma-separated UID lists belong in Spoolman" % gate)
            return False
        self.mmu.gate_spool_rfid[gate] = normalized
        return True
