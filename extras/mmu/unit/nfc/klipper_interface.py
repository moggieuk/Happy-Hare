# klippy/extras/mmu/unit/nfc/klipper_interface.py
#
# EMU NFC Gate Reader — reactor-thread Happy Hare dispatcher
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Writes gate events straight into Happy Hare's gate map by calling its own
# MMU_GATE_MAP / MMU_SPOOLMAN commands directly (gcode.run_script_from_command),
# rather than through a layer of NFC-specific gcode_macros. Those macros
# (_NFC_SPOOL_CHANGED, _NFC_SPOOL_REMOVED, _NFC_TAG_NO_SPOOL) added nothing of
# their own beyond formatting a console message and forwarding fixed params to
# MMU_GATE_MAP/MMU_SPOOLMAN -- this does the same thing in one hop instead of
# two, with nothing left for a user to accidentally desync by re-customizing.
#
# Reuses Happy Hare's own MMU_GATE_MAP/MMU_SPOOLMAN commands rather than
# poking mmu.gate_maps directly, since those commands own real logic this
# package shouldn't duplicate: color validation, spoolman_support=pull-mode
# branching, and persist_gate_map()'s LED-update/webhook-notify side effects.

import re

from .gate_state import (EVENT_CHANGED, EVENT_UID_ONLY, EVENT_REMOVED)
from .log import logger, color_console_tags
from ...mmu_constants import (
    ACTION_LOADING, ACTION_LOADING_EXTRUDER, ACTION_UNLOADING, ACTION_HOMING,
)

# Actions during which a tag briefly dropping out of read range is expected
# motion noise, not a genuine removal. Matches the exact set the retired
# _NFC_SPOOL_REMOVED macro's "load"/"unload"/"homing" substring match caught
# against Happy Hare's action label strings -- ACTION_UNLOADING_EXTRUDER's
# label ("Exiting Ext") never matched that substring check, so it's excluded
# here too, preserving existing behavior rather than "fixing" what may have
# been an oversight.
_BUSY_ACTIONS_IGNORE_REMOVAL = (
    ACTION_LOADING, ACTION_LOADING_EXTRUDER, ACTION_UNLOADING, ACTION_HOMING,
)


class KlipperInterface:
    def __init__(self, printer, reactor, debug=2, name='',
                 spoolman_enabled=True):
        self._printer = printer
        self._reactor = reactor
        self._debug = debug
        self._name = name
        self._spoolman_enabled = spoolman_enabled

    def dispatch(self, event_type, gate, uid_hex, spool_id, meta=None,
                 auto_created=False):
        """Schedule a Happy Hare gate-map update for the given gate event."""
        self._reactor.register_callback(
            lambda e, et=event_type, g=gate, u=uid_hex, s=spool_id, m=meta,
                   ac=auto_created:
                self._update_gate_map(et, g, u, s, m, ac))

    @staticmethod
    def _macro_value(value):
        value = str(value or '').strip()
        value = re.sub(r'\s+', '_', value)
        return re.sub(r'[^A-Za-z0-9_#.+-]', '', value)

    def _metadata_name(self, meta):
        meta = meta or {}
        base = meta.get('material_detail') or meta.get('material')
        prefix = meta.get('brand') or meta.get('vendor') or meta.get('tag_format')
        base = self._macro_value(base)
        prefix = self._macro_value(prefix)
        if prefix and prefix.lower() == 'bambu_lab':
            prefix = 'Bambu'
        if prefix and base and not base.lower().startswith(prefix.lower()):
            return "{}_{}".format(prefix, base)
        return base

    def _respond(self, gcode, text):
        gcode.respond_info(color_console_tags(text))

    def _run(self, gcode, script):
        if self._debug >= 3:
            logger.info("mmu_nfc: dispatching: %s", script)
        gcode.run_script_from_command(script)

    def _update_gate_map(self, event_type, gate, uid_hex, spool_id, meta=None,
                          auto_created=False):
        gcode = self._printer.lookup_object('gcode')
        try:
            if event_type == EVENT_CHANGED:
                if spool_id is not None:
                    self._respond(gcode,
                        '[OK] NFC[%s]: spool %d detected (UID %s)%s. '
                        'Sending to Happy Hare.' % (
                            self._name, spool_id, uid_hex,
                            " [new spool]" if auto_created else ""))
                    if self._debug >= 3:
                        logger.info(
                            "mmu_nfc: gate %d → spool %d detected (UID %s%s)",
                            gate, spool_id, uid_hex,
                            " [auto-created]" if auto_created else "")
                    if auto_created:
                        self._run(gcode, "MMU_SPOOLMAN REFRESH=1 QUIET=1")
                    self._run(gcode, "MMU_GATE_MAP GATE=%d SPOOLID=%d AVAILABLE=1 QUIET=1"
                                     % (gate, spool_id))
                else:
                    m        = meta or {}
                    name     = self._metadata_name(m)
                    material = self._macro_value(m.get('material', ''))
                    color    = self._macro_value(m.get('color_hex', ''))
                    brand    = self._macro_value(m.get('brand') or m.get('vendor') or '')
                    min_temp = m.get('min_temp')
                    max_temp = m.get('max_temp')
                    diameter = m.get('diameter_mm')
                    weight   = m.get('weight_g') or m.get('spool_weight_g')
                    self._respond(gcode,
                        '[OK] NFC[%s]: tag metadata detected (UID %s) — no '
                        'Spoolman. brand=%s name=%s material=%s color=%s '
                        'min=%s max=%s dia=%smm weight=%sg' % (
                            self._name, uid_hex, brand, name, material, color,
                            min_temp, max_temp, diameter, weight))
                    if self._debug >= 3:
                        logger.info(
                            "mmu_nfc: gate %d → tag %s metadata-only "
                            "(name=%s material=%s color=%s brand=%s "
                            "min_temp=%s max_temp=%s diameter=%s weight=%s)",
                            gate, uid_hex, name, material, color, brand,
                            min_temp, max_temp, diameter, weight)
                    parts = ["MMU_GATE_MAP", "GATE=%d" % gate]
                    if name:
                        parts.append("NAME=%s" % name)
                    if material:
                        parts.append("MATERIAL=%s" % material)
                    if color:
                        parts.append("COLOR=%s" % color)
                    if max_temp is not None:
                        parts.append("TEMP=%d" % int(max_temp))
                    parts.append("AVAILABLE=1 QUIET=1")
                    self._run(gcode, ' '.join(parts))
                self._run(gcode, "MMU_SPOOLMAN SYNC=1 QUIET=1")

            elif event_type == EVENT_UID_ONLY:
                if self._spoolman_enabled:
                    self._respond(gcode,
                        '[ERROR] NFC[%s]: tag UID %s is not registered in '
                        "Spoolman.\nOpen the spool record in Spoolman, set "
                        "the 'rfid_tag' extra field to: %s"
                        % (self._name, uid_hex, uid_hex))
                    if self._debug >= 3:
                        logger.info(
                            "mmu_nfc: gate %d → tag %s (no spool ID in Spoolman)",
                            gate, uid_hex)
                else:
                    self._respond(gcode,
                        '[WARN] NFC[%s]: tag UID %s read, but no rich '
                        'metadata or spool assignment was found.'
                        % (self._name, uid_hex))
                    if self._debug >= 3:
                        logger.info(
                            "mmu_nfc: gate %d → tag %s "
                            "(Spoolman disabled; no metadata spool)",
                            gate, uid_hex)
                # Mark the gate occupied/available, but clear the spool
                # assignment and visible filament fields so Happy Hare does
                # not retain stale metadata.
                self._run(gcode,
                    "MMU_GATE_MAP GATE=%d SPOOLID=-1 NAME=Unknown "
                    "MATERIAL=Unknown COLOR=FFFFFF55 TEMP=0 AVAILABLE=1 "
                    "QUIET=1" % gate)

            elif event_type == EVENT_REMOVED:
                mmu = self._printer.lookup_object('mmu', None)
                action = getattr(mmu, 'action', None)
                if action in _BUSY_ACTIONS_IGNORE_REMOVAL:
                    self._respond(gcode,
                        "NFC[%s]: tag absent during MMU operation — "
                        "ignoring removal." % self._name)
                    return
                self._respond(gcode,
                    'NFC[%s]: spool removed. Clearing Happy Hare gate.'
                    % self._name)
                if self._debug >= 3:
                    logger.info(
                        "mmu_nfc: gate %d → spool removed (was spool_id=%s)",
                        gate, spool_id)
                self._run(gcode,
                    "MMU_GATE_MAP GATE=%d SPOOLID=-1 AVAILABLE=0 QUIET=1" % gate)
                self._run(gcode, "MMU_SPOOLMAN SYNC=1 QUIET=1")

            else:
                logger.warning("mmu_nfc: unknown event type %r", event_type)
        except Exception:
            logger.exception("mmu_nfc: gate-map update failed for gate %d event %r",
                              gate, event_type)
