# klippy/extras/mmu/mmu_nfc_klipper_interface.py
#
# EMU NFC Gate Reader — reactor-thread GCode macro dispatcher
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Receives gate change events and dispatches them as GCode macro calls in the
# Klipper reactor thread.
#
# Macros called (define in printer.cfg / nfc_macros.cfg):
#
#   _NFC_SPOOL_CHANGED  GATE=<n>  SPOOL_ID=<id>  UID=<hex>  [AUTO_CREATED=1] [SCAN_FINISH=1]
#   _NFC_SPOOL_CHANGED  GATE=<n>  [NAME=<str>]  [MATERIAL=<str>]  [COLOR=<hex>]  [TEMP=<int>]  UID=<hex>
#                       [SCAN_FINISH=1]
#   _NFC_SPOOL_REMOVED  GATE=<n>
#   _NFC_TAG_NO_SPOOL   GATE=<n>  UID=<hex>  [SPOOLMAN_DISABLED=1] [SCAN_FINISH=1]

import re

from .mmu_nfc_gate_state import (DIRECT_METADATA_SPOOL,
                         EVENT_CHANGED, EVENT_UID_ONLY, EVENT_REMOVED)
from .mmu_nfc_log import logger


class KlipperInterface:
    def __init__(self, printer, reactor, debug=2, name='',
                 spoolman_enabled=True):
        self._printer = printer
        self._reactor = reactor
        self._debug = debug
        self._name = name
        self._spoolman_enabled = spoolman_enabled

    def dispatch(self, event_type, gate, uid_hex, spool_id, meta=None,
                 auto_created=False, scan_finish=False):
        """Schedule a GCode macro call for the given gate event."""
        self._reactor.register_callback(
            lambda e, et=event_type, g=gate, u=uid_hex, s=spool_id, m=meta,
                   ac=auto_created, sf=scan_finish:
                self._run_gcode(et, g, u, s, m, ac, sf))

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

    def _run_gcode(self, event_type, gate, uid_hex, spool_id, meta=None,
                   auto_created=False, scan_finish=False):
        gcode = self._printer.lookup_object('gcode')
        try:
            if event_type == EVENT_CHANGED:
                if spool_id is not None:
                    script = "_NFC_SPOOL_CHANGED GATE={} READER={} SPOOL_ID={} UID={}{}{}".format(
                        gate, self._name, spool_id, uid_hex,
                        " AUTO_CREATED=1" if auto_created else "",
                        " SCAN_FINISH=1" if scan_finish else "")
                    if self._debug >= 3:
                        logger.info(
                            "mmu_nfc: gate %d → spool %d detected (UID %s%s)",
                            gate, spool_id, uid_hex,
                            " [auto-created]" if auto_created else "")
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
                    parts = ['_NFC_SPOOL_CHANGED', 'GATE={}'.format(gate), 'READER={}'.format(self._name)]
                    parts.append('NAME={}'.format(name))
                    parts.append('MATERIAL={}'.format(material))
                    parts.append('COLOR={}'.format(color))
                    parts.append('BRAND={}'.format(brand))
                    parts.append('MIN_TEMP={}'.format(int(min_temp) if min_temp is not None else ''))
                    parts.append('TEMP={}'.format(int(max_temp) if max_temp is not None else ''))
                    parts.append('DIAMETER={}'.format(diameter if diameter is not None else ''))
                    parts.append('WEIGHT={}'.format(int(weight) if weight is not None else ''))
                    parts.append('UID={}'.format(uid_hex))
                    if scan_finish:
                        parts.append('SCAN_FINISH=1')
                    script = ' '.join(parts)
                    if self._debug >= 3:
                        logger.info(
                            "mmu_nfc: gate %d → tag %s metadata-only "
                            "(name=%s material=%s color=%s brand=%s "
                            "min_temp=%s max_temp=%s diameter=%s weight=%s)",
                            gate, uid_hex, name, material, color, brand,
                            min_temp, max_temp, diameter, weight)
            elif event_type == EVENT_UID_ONLY:
                script = "_NFC_TAG_NO_SPOOL GATE={} READER={} UID={}{}{}".format(
                    gate, self._name, uid_hex,
                    " SPOOLMAN_DISABLED=1" if not self._spoolman_enabled else "",
                    " SCAN_FINISH=1" if scan_finish else "")
                if self._debug >= 3:
                    if self._spoolman_enabled:
                        logger.info(
                            "mmu_nfc: gate %d → tag %s "
                            "(no spool ID in Spoolman)",
                            gate, uid_hex)
                    else:
                        logger.info(
                            "mmu_nfc: gate %d → tag %s "
                            "(Spoolman disabled; no metadata spool)",
                            gate, uid_hex)
            elif event_type == EVENT_REMOVED:
                script = "_NFC_SPOOL_REMOVED GATE={} READER={}".format(gate, self._name)
                if self._debug >= 3:
                    logger.info(
                        "mmu_nfc: gate %d → spool removed (was spool_id=%s)",
                        gate, spool_id)
            else:
                logger.warning("mmu_nfc: unknown event type %r", event_type)
                return
            if self._debug >= 3:
                logger.info("mmu_nfc: dispatching GCode: %s", script)
            gcode.run_script(script)
            if self._debug >= 3:
                logger.info("mmu_nfc: dispatched GCode OK: %s", script)
        except Exception:
            logger.exception("mmu_nfc: GCode dispatch failed for gate %d event %r",
                              gate, event_type)
