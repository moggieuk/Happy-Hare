# klippy/extras/mmu/mmu_nfc_manager.py
#
# EMU NFC Gate Reader — gate manager
# Version 1.0.0  |  2026-04-14
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Gate coordination logic for per-lane and shared NFC Readers:
#
#   NFCGateDefaults  — shared config defaults from the base [nfc_gate] section
#   NFCGate          — per-lane manager for [nfc_gate laneN]
#
# Internal helpers (not imported externally):
#   GateState        — per-gate debounce state machine; owns process_read(),
#                      removal debounce, and event generation
#   CurrentTag       — dataclass holding the full tag observation for one read
#                      window: UID, reader target identity, raw tag pages,
#                      parsed metadata, parse errors, and resolution path;
#                      stored on GateState.current_tag; populated by
#                      _read_current_tag() and enriched by _resolve_spool()
#   KlipperInterface — thread-safe GCode macro dispatcher
#
# Threading model
# ───────────────
# NFC polling runs on Klipper reactor timers.  Klipper MCU I2C/SPI helpers use
# reactor greenlets internally, so hardware transactions must stay on the
# reactor thread.  Do not move reader polling into a normal Python thread.
#
# Ownership boundaries
# ────────────────────
# Reader drivers are hardware/protocol adapters only.  They read tag
# identity and returns UID values; it does not know about lanes, Spoolman
# records, Happy Hare, or spool assignment policy.
#
# SpoolmanClient is a lookup/cache client only.  It resolves UID → spool record
# / spool_id and may discover the Spoolman URL from Moonraker, but it does not
# own gates and must not issue Happy Hare commands or write gate assignments.
#
# NFCGate owns the lane/gate state machine.  It decides whether a read is
# unchanged, changed, UID-only, or removed, and it is the only layer that
# orchestrates Happy Hare-facing commands.  The default macro boundary uses
# MMU_GATE_MAP so Happy Hare remains the source of truth for gate maps and
# Spoolman synchronization.
#
# Scan-jog triggering
# ────────────────────
# Scan-jog is triggered by Happy Hare's post-preload hook
# (_NFC_SCAN_JOG_PRELOAD / _NFC_SHARED_PRELOAD, calling
# NFC GATE=<n> JOG_SCAN=1 SOURCE=AUTO), not by polling for a gate-status
# transition here.  _poll_timer_event() still reads Happy Hare's gate_status
# every tick, but only to suspend I2C polling while Happy Hare already owns
# the gate and to resume polling / clear the NFC cache once the gate empties
# again — it no longer decides when to start a scan.
#
# Intended command flow:
#   New spool:  _NFC_SPOOL_CHANGED GATE=<gate> SPOOL_ID=<spool_id> UID=<uid>
#   UID only:   _NFC_TAG_NO_SPOOL GATE=<gate> UID=<uid>
#   Removed:    _NFC_SPOOL_REMOVED GATE=<gate>
#   Same tag:   no command

import ast
import os
import re

from .unit.nfc import pn532_driver, rc522_driver
from .unit.nfc import reader_factory
from . import mmu_nfc_reader_resolver as mmu_reader
from . import mmu_nfc_scan_jog as scan_jog
from . import mmu_nfc_tag_handler as tag_handler
from .mmu_nfc_gate_state import (GateState,
                               EVENT_CHANGED, EVENT_UID_ONLY, EVENT_REMOVED,
                               DIRECT_METADATA_SPOOL)
from .mmu_nfc_klipper_interface import KlipperInterface
from .mmu_nfc_log import configure, logger
from .mmu_constants import (
    GATE_EMPTY, GATE_AVAILABLE, GATE_AVAILABLE_FROM_BUFFER,
    FILAMENT_POS_UNLOADED, ACTION_IDLE, ACTION_CHECKING,
)

try:
    from .mmu_nfc_log import color_console_tags
except ImportError:
    def color_console_tags(text):
        text = str(text)
        text = text.replace('[WARN]', '<span style="color:#FFFF00">[WARN]</span>')
        text = text.replace('[OK]', '<span style="color:#90EE90">[OK]</span>')
        text = text.replace('[ERROR]', '<span style="color:#FF6060">[ERROR]</span>')
        text = text.replace('[SCAN]', '<span style="color:#FFA040">[SCAN]</span>')
        text = text.replace('[MOVE]', '<span style="color:#FFA040">[MOVE]</span>')
        text = text.replace('[REWIND]', '<span style="color:#90EE90">[REWIND]</span>')
        return text
from .mmu_nfc_spoolman_client import SpoolmanClient

LANE_LED_TEST_DURATION = 2.0
LANE_LED_TEST_GAP = 0.15
LANE_LED_TEST_DEFAULT_CYCLES = 2
LANE_LED_TEST_MAX_CYCLES = 20
STARTUP_UNKNOWN_GATE_CHECK_DELAY = 5.0
STARTUP_UNKNOWN_GATE_CHECK_STAGGER = 1.0

# ── Direct reads of Happy Hare's live MMU state ─────────────────────────────
#
# This add-on is native to Happy Hare V4: `mmu` is a real MmuController
# object, not a foreign dict to defend against. These helpers read its gate
# map and state attributes directly instead of parsing mmu.get_status(),
# which exists to serialize state for external consumers (webhooks, macro
# templates) — not for internal callers that already have the object.
#
# Formerly hh_status.py, a separate shared module. Folded in here once every
# remaining external touchpoint (scan_jog.py) was reachable through the
# NFCGate instance it already receives instead of a module import.

# Mirrors MmuController._get_action_string()'s labels, for log/console text
# only. Never compare against these strings — compare the ACTION_* ints
# above. Only the two values this add-on actually branches on are listed;
# anything else falls back to its raw numeric form.
_ACTION_LABELS = {
    ACTION_IDLE: "Idle",
    ACTION_CHECKING: "Checking",
}


class _GateSnapshot:
    """Live view of one gate's Happy Hare state, read directly from `mmu`."""

    def __init__(self, present=False, gate=-1, spool=-1, status=GATE_EMPTY,
                 action=ACTION_IDLE, active_gate=-1,
                 filament_pos=FILAMENT_POS_UNLOADED, gate_count=0):
        self.present = present
        self.gate = gate
        self.spool = spool
        self.status = status
        self.action = action
        self.active_gate = active_gate
        self.filament_pos = filament_pos
        self.gate_count = gate_count

    @property
    def assigned(self):
        return self.spool > 0

    @property
    def available(self):
        return self.status >= GATE_AVAILABLE

    @property
    def empty(self):
        return self.status == GATE_EMPTY

    @property
    def idle(self):
        return self.action == ACTION_IDLE

    def action_label(self):
        return _ACTION_LABELS.get(self.action, str(self.action))

    def label(self):
        """Short human-readable summary of this gate's Happy Hare assignment."""
        if not self.present:
            return "Happy Hare: n/a"
        if self.gate < 0 or (self.gate_count > 0 and self.gate >= self.gate_count):
            return "Happy Hare: unknown"
        if self.active_gate == self.gate and self.filament_pos > 0:
            return "Happy Hare: spool %d  loading (pos %d)" % (self.spool, self.filament_pos)
        if self.assigned:
            return "Happy Hare: spool %d  %s" % (
                self.spool, "available" if self.available else "assigned")
        if self.available:
            return "Happy Hare: found/no spool"
        return "Happy Hare: empty"


def _gate_snapshot(mmu, gate, eventtime=None):
    """Direct read of one gate's live Happy Hare state.

    `mmu` is the caller's cached MmuController reference (resolved once at
    klippy:connect, not looked up here) — see NFCGate._get_mmu().
    """
    if mmu is None:
        return _GateSnapshot(gate=gate)

    gate_status = mmu.gate_maps.gate_status
    gate_spool_id = mmu.gate_maps.gate_spool_id
    gate_count = len(gate_status)

    if gate < 0 or gate >= gate_count:
        return _GateSnapshot(
            present=True, gate=gate, action=mmu.action,
            active_gate=mmu.gate_selected, filament_pos=mmu.filament_pos,
            gate_count=gate_count)

    return _GateSnapshot(
        present=True, gate=gate,
        spool=gate_spool_id[gate], status=gate_status[gate],
        action=mmu.action, active_gate=mmu.gate_selected,
        filament_pos=mmu.filament_pos, gate_count=gate_count)


class _FullSnapshot:
    """Live view of Happy Hare's full gate map, read directly from `mmu`."""

    def __init__(self, present=False, action=ACTION_IDLE, active_gate=-1,
                 filament_pos=FILAMENT_POS_UNLOADED, gate_statuses=None,
                 gate_spool_ids=None):
        self.present = present
        self.action = action
        self.active_gate = active_gate
        self.filament_pos = filament_pos
        self.gate_statuses = gate_statuses or []
        self.gate_spool_ids = gate_spool_ids or []

    @property
    def idle(self):
        return self.action == ACTION_IDLE

    def action_label(self):
        return _ACTION_LABELS.get(self.action, str(self.action))


def _full_snapshot(mmu, eventtime=None):
    """Direct read of Happy Hare's full gate-map state.

    `mmu` is the caller's cached MmuController reference (resolved once at
    klippy:connect, not looked up here) — see NFCGate._get_mmu().
    """
    if mmu is None:
        return _FullSnapshot()

    return _FullSnapshot(
        present=True, action=mmu.action, active_gate=mmu.gate_selected,
        filament_pos=mmu.filament_pos,
        gate_statuses=list(mmu.gate_maps.gate_status),
        gate_spool_ids=list(mmu.gate_maps.gate_spool_id))


def _spoolman_url_enabled(url):
    value = str(url or '').strip().lower()
    return value not in ('', 'disabled', 'disable', 'false', 'off', 'none', 'no')


def _get_console_config(config, default_enabled=False, default_level='warning'):
    """
    Read UI/console logging settings.

    console_* is the preferred spelling.  ui_* is accepted as a Happy Hare
    style alias for users already thinking in those terms.
    """
    enabled = config.getboolean('console_output',
                                config.getboolean('ui_output',
                                                  default_enabled))
    level = config.get('console_log_level',
                       config.get('ui_log_level', default_level))
    return enabled, level


def _flag_param(gcmd, name):
    value = gcmd.get(name, None)
    if value is None:
        return False
    if value == '':
        return True
    try:
        return bool(gcmd.get_int(name, minval=0, maxval=1))
    except Exception:
        return bool(value)


def _gcmd_get_any(gcmd, names, default=None):
    for name in names:
        value = gcmd.get(name, None)
        if value is not None:
            return value
    return default


def _get_scan_motion_mode(config, default='continuous'):
    mode = str(config.get('scan_motion_mode', default) or '').strip().lower()
    if mode not in ('stopped', 'continuous'):
        raise config.error(
            "scan_motion_mode must be 'stopped' or 'continuous'")
    return mode


def _normalise_command_uid(uid):
    uid_norm = SpoolmanClient._normalise_uid(str(uid or ''))
    if not uid_norm:
        return None
    if len(uid_norm) % 2 != 0 or not re.match(r'^[0-9A-F]+$', uid_norm):
        return None
    return uid_norm


def _respond_register_error(gcmd, msg):
    logger.error(msg, extra={'nfc_no_console': True})
    gcmd.respond_info(color_console_tags(msg))


def _cmd_register_uid_to_spool(gcode, spoolman, gcmd):
    uid = _normalise_command_uid(_gcmd_get_any(gcmd, ('UID', 'Uid', 'uid')))
    spool_raw = _gcmd_get_any(
        gcmd, ('SPOOL_ID', 'Spool_id', 'spool_id', 'SPOOLID', 'Spoolid'))

    if not uid:
        _respond_register_error(
            gcmd, "[ERROR] NFC: NFC_REGISTER requires UID=TAG_UID")
        return

    try:
        spool_id = int(spool_raw)
    except Exception:
        _respond_register_error(
            gcmd, "[ERROR] NFC: NFC_REGISTER requires SPOOL_ID=SPOOL_ID")
        return
    if spool_id <= 0:
        _respond_register_error(
            gcmd, "[ERROR] NFC: SPOOL_ID must be greater than 0")
        return

    if spoolman is None:
        msg = "[ERROR] NFC: Spoolman is disabled; cannot register UID %s" % uid
        _respond_register_error(gcmd, msg)
        return

    spool = spoolman.lookup_spool_by_id(spool_id)
    if not spool:
        msg = "[ERROR] NFC: Spoolman spool %d was not found" % spool_id
        _respond_register_error(gcmd, msg)
        return

    if not spoolman.set_spool_uid(spool_id, uid):
        msg = ("[ERROR] NFC: failed to assign UID %s to Spoolman spool %d"
               % (uid, spool_id))
        _respond_register_error(gcmd, msg)
        return

    spoolman.clear_cache()

    logger.info("NFC_Register: UID %s assigned to Spoolman spool %d",
                uid, spool_id)
    gcmd.respond_info(color_console_tags(
        "[OK] NFC: UID %s assigned to Spoolman spool %d; NFC cache cleared. "
        "Happy Hare/Fluidd will refresh on their normal Spoolman polling cycle."
        % (uid, spool_id)))


def _enabled_lane_gates():
    return sorted(
        (gate for gate in _lane_instances
         if (not getattr(gate, '_shared', False)
             and getattr(gate, '_enabled', True))),
        key=lambda gate: gate._gate)


def _cmd_led_test_all(gcmd):
    if not _flag_param(gcmd, 'ALL'):
        gcmd.respond_info(color_console_tags(
            "NFC_LED_TEST commands:\n"
            "  NFC_LED_TEST ALL=1 - test configured lane tag-read LED effect "
            "on every enabled lane\n"
            "  NFC_LED_TEST ALL=1 DELAY=0.20 CYCLES=2 - set chase delay "
            "and cycles"))
        return
    lanes = _enabled_lane_gates()
    if not lanes:
        gcmd.respond_info(color_console_tags(
            "[WARN] NFC: no enabled per-lane readers configured"))
        return
    try:
        delay = float(_gcmd_get_any(
            gcmd, ('DELAY', 'Delay', 'delay'), 0.20))
    except Exception:
        delay = 0.20
    delay = max(0.0, min(delay, 5.0))
    cycles = _led_test_cycles_from_gcmd(gcmd)

    scheduled = []
    for index, gate in enumerate(lanes):
        _schedule_lane_led_test(gate, index * delay, cycles)
        scheduled.append(str(gate._gate))
    msg = ("[OK] NFC: lane LED chase test scheduled for gates %s "
           "(delay=%.2fs cycles=%d)" % (", ".join(scheduled), delay, cycles))
    logger.info("NFC_LED_TEST ALL=1 — scheduled=%s delay=%.2fs cycles=%d",
                ",".join(scheduled), delay, cycles)
    gcmd.respond_info(color_console_tags(msg))


def _led_test_cycles_from_gcmd(gcmd):
    try:
        value = int(_gcmd_get_any(
            gcmd, ('CYCLES', 'Cycles', 'cycles'),
            LANE_LED_TEST_DEFAULT_CYCLES))
    except Exception:
        value = LANE_LED_TEST_DEFAULT_CYCLES
    return max(1, min(value, LANE_LED_TEST_MAX_CYCLES))


def _schedule_lane_led_test(gate, delay, cycles):
    if delay <= 0:
        gate._lane_led_test(gcmd=None, respond=False, cycles=cycles)
        return

    reactor = gate.reactor

    def _run(eventtime, lane=gate):
        lane._lane_led_test(gcmd=None, respond=False, cycles=cycles)
        return lane.reactor.NEVER

    eventtime = reactor.monotonic() + delay
    try:
        reactor.register_timer(_run, eventtime)
    except TypeError:
        timer = reactor.register_timer(_run)
        reactor.update_timer(timer, eventtime)


# ─────────────────────────────────────────────────────────────────────────────
# NFCGateDefaults / NFCGate — per-lane/shared NFC Reader path
# ─────────────────────────────────────────────────────────────────────────────
#
# One NFCGate instance per [nfc_gate laneN] config section.
# Each manages one configured NFC Reader.
#
# NFCGateDefaults holds shared values from the optional base [nfc_gate]
# section.  Lane sections inherit these and can override any key locally.

# Module-level registry for NFC_STATUS across all configured lanes.
_lane_instances = []

# Single shared reader instance, set by NFCGate._handle_connect when shared=true.
_shared_instance = None
_shared_configured = False

# Internal gate number for the shared reader.  Not exposed to users — the shared
# reader has no Happy Hare gate assignment and does not use this value for Happy Hare
# orchestration.  It serves only as a unique key for reader drivers / GateState
# logging and (internally) as a guard against accidentally seeding from Happy Hare.
_SHARED_GATE_SENTINEL            = 255
_SHARED_MISSED_RESOLUTION_LIMIT  = 3
_SHARED_READ_EFFECT_DURATION        = 4.0
_SHARED_BYPASS_READ_EFFECT_DURATION = 4.0
_SHARED_BYPASS_READY_EFFECT_DURATION = 2.0
_SHARED_UNRESOLVED_EFFECT_DURATION  = 2.0
_diagnostic_warnings = []


def _add_diagnostic_warning(message):
    if message not in _diagnostic_warnings:
        _diagnostic_warnings.append(message)
        logger.warning("nfc_gate: %s", message)


def nfc_gate_for_gate_number(gate_number):
    for candidate in _lane_instances:
        if (candidate._gate == gate_number
                and getattr(candidate, '_enabled', True)):
            return candidate
    return None


def _status_html_words(text):
    text = re.sub(r'\bavailable\b',
                  '<span style="color:#90EE90">available</span>', text)
    text = re.sub(r'\bempty\b',
                  '<span style="color:#87CEEB">empty</span>', text)
    text = re.sub(r'\bassigned\b',
                  '<span style="color:#FFFF00">assigned</span>', text)
    return text


def _reader_label(reader_type):
    return "NFC Reader (%s)" % (reader_type,)


def _reader_wiring_hint(reader_type):
    if reader_type == 'rc522':
        return "check SPI wiring, cs_pin, spi_bus/software SPI pins, power, and ground"
    return "check wiring and I2C address"


def _lookup_objects_safe(printer, name):
    try:
        return list(printer.lookup_objects(name))
    except Exception:
        return []


def _optional_float_config(config, key, default=None, minval=None, maxval=None):
    raw = config.get(key, None)
    if raw is None:
        return default
    try:
        value = float(raw)
    except Exception:
        raise config.error("Option '%s' must be a number" % key)
    if minval is not None and value < minval:
        raise config.error("Option '%s' must be at least %.3f" % (key, minval))
    if maxval is not None and value > maxval:
        raise config.error("Option '%s' must be at most %.3f" % (key, maxval))
    return value


def _raw_klipper_config(printer):
    try:
        configfile = printer.lookup_object('configfile', None)
        if configfile is None:
            return {}
        return configfile.get_status(0).get('config', {}) or {}
    except Exception:
        return {}


def _detect_happy_hare_version(printer):
    """Return the Happy Hare software version string, or None if unavailable."""
    try:
        mmu = printer.lookup_object('mmu', None)
        if mmu is None:
            return None
        version = getattr(mmu, 'version', None)
        if version is None:
            mmu_machine = getattr(mmu, 'mmu_machine', None)
            version = getattr(mmu_machine, 'happy_hare_version', None)
        return version
    except Exception:
        return None


def _shared_preload_hook_message(hook, name='shared'):
    hook = str(hook or '').strip()
    if '_NFC_SHARED_PRELOAD' in hook:
        return None
    if 'JOG_SCAN' in hook or 'NFC JOG_SCAN' in hook:
        return ("[%s]: shared reader is enabled but Happy Hare "
                "user_post_preload_extension is wired to per-lane "
                "NFC JOG_SCAN; set "
                "_MMU_SEQUENCE_VARS.variable_user_post_preload_extension "
                "to '_NFC_SHARED_PRELOAD'" % name)
    if hook:
        return ("[%s]: shared reader is enabled but Happy Hare "
                "user_post_preload_extension is '%s'; set "
                "_MMU_SEQUENCE_VARS.variable_user_post_preload_extension "
                "to '_NFC_SHARED_PRELOAD'" % (name, hook))
    return ("[%s]: shared reader is enabled but "
            "_MMU_SEQUENCE_VARS.variable_user_post_preload_extension "
            "does not contain _NFC_SHARED_PRELOAD" % name)


def _lane_status_lines(printer):
    """Build NFC_STATUS output lines cross-referenced against the MMU
    lane MCUs registered in Klipper (mirrors how Happy Hare reads [board_pins lane]).

    For each lane MCU (e.g. lane0…lane4):
      - If an NFCGate is configured for that MCU → show its spool/UID state.
      - If no NFCGate is configured         → note that no reader is set up.
    Falls back to listing _lane_instances directly when no lane MCUs are found.
    """
    # Collect MCU names that match "lane<N>" from Klipper's object registry.
    lane_names = []
    for obj_name, _ in _lookup_objects_safe(printer, 'mcu'):
        parts = obj_name.split(None, 1)
        if len(parts) == 2 and re.match(r'^lane\d+$', parts[1]):
            lane_names.append(parts[1])
    lane_names.sort(key=lambda n: int(n[4:]))

    nfc_by_lane = {gate._name: gate for gate in _lane_instances
                   if not getattr(gate, '_shared', False)}

    if not lane_names:
        # No MMU lane MCUs visible — fall back to plain list.
        if not nfc_by_lane:
            return ["No [nfc_gate] sections are configured."]
        lines = ["NFC gate status  (%d gate%s configured):"
                 % (len(nfc_by_lane), 's' if len(nfc_by_lane) != 1 else '')]
        for gate in sorted(_lane_instances, key=lambda g: g._gate):
            if not getattr(gate, '_shared', False):
                lines.append(gate.status_line())
        _append_shared_status(lines)
        return lines

    lines = ["NFC gate status — %d MMU lane(s), %d NFC reader(s) configured:"
             % (len(lane_names), len(nfc_by_lane))]
    for lane in lane_names:
        if lane in nfc_by_lane:
            lines.append(nfc_by_lane[lane].status_line())
        else:
            lines.append("  %-8s  no NFC reader configured" % (lane + ':'))
    _append_shared_status(lines)
    return lines


def _append_shared_status(lines):
    if (_shared_instance is not None
            and getattr(_shared_instance, '_enabled', True)):
        lines.append(_shared_instance.shared_status_line())


def _doctor_lines(printer):
    lines = ["NFC doctor:"]
    raw_config = _raw_klipper_config(printer)
    lane_readers = [gate for gate in _lane_instances
                    if not getattr(gate, '_shared', False)]
    enabled_lanes = [gate for gate in lane_readers
                     if getattr(gate, '_enabled', True)]
    disabled_lanes = [gate for gate in lane_readers
                      if not getattr(gate, '_enabled', True)]
    shared_readers = [gate for gate in _lane_instances
                      if getattr(gate, '_shared', False)]
    enabled_shared = [gate for gate in shared_readers
                      if getattr(gate, '_enabled', True)]

    def mark(ok):
        return "[OK]" if ok else "[WARN]"

    lines.append("  %s lane readers: %d enabled, %d disabled" %
                 (mark(bool(enabled_lanes) or bool(enabled_shared)),
                  len(enabled_lanes), len(disabled_lanes)))
    for gate in sorted(lane_readers, key=lambda g: g._gate):
        if not getattr(gate, '_enabled', True):
            lines.append("    Gate %d [%s/%s]: disabled by config" %
                         (gate._gate, gate._name, gate._reader_type))
        else:
            state = "failed" if gate._failed else "ready/pending init"
            lines.append("    Gate %d [%s/%s]: enabled, %s" %
                         (gate._gate, gate._name, gate._reader_type, state))

    if enabled_shared:
        shared = enabled_shared[0]
        lines.append("  %s shared reader: enabled [%s/%s]" %
                     (mark(not shared._failed), shared._name,
                      shared._reader_type))
    elif shared_readers:
        lines.append("  [OK] shared reader: configured but disabled")
    else:
        lines.append("  [OK] shared reader: not configured")

    hh_version = _detect_happy_hare_version(printer)
    if hh_version is None:
        lines.append("  [WARN] Happy Hare version: unknown")
    else:
        lines.append("  [OK] Happy Hare version: %s" % hh_version)

    defaults = printer.lookup_object('nfc_gate', None)
    spoolman = getattr(defaults, '_spoolman', None)
    if spoolman is not None:
        lines.append("  [OK] Spoolman: enabled")
    else:
        url = getattr(defaults, 'spoolman_url', '')
        lines.append("  [WARN] Spoolman: disabled or unavailable"
                     if not url else "  [OK] Spoolman: disabled by config")

    if enabled_shared:
        macro = raw_config.get('gcode_macro _MMU_SEQUENCE_VARS', {})
        hook = str(macro.get('variable_user_post_preload_extension', ''))
        warning = _shared_preload_hook_message(hook)
        if warning is None:
            lines.append("  [OK] shared preload hook: _NFC_SHARED_PRELOAD")
        else:
            lines.append("  [WARN] shared preload hook: %s" % warning)

    warnings = list(_diagnostic_warnings)
    for gate in _lane_instances:
        warnings.extend(getattr(gate, '_diagnostic_warnings', []))
    if warnings:
        lines.append("  Warnings:")
        for warning in sorted(set(warnings)):
            lines.append("    [WARN] %s" % warning)
    else:
        lines.append("  [OK] no static config warnings")
    return lines


def _nfc_help(gcmd=None):
    advanced = bool(gcmd.get_int('ADVANCED', 0, minval=0, maxval=1)
                    if gcmd is not None else False)
    callbacks = bool(gcmd.get_int('CALLBACKS', 0, minval=0, maxval=1)
                     if gcmd is not None else False)
    low_level = bool(gcmd.get_int('LOW_LEVEL', 0, minval=0, maxval=1)
                     if gcmd is not None else False)
    lane_gates = sorted(gate._gate for gate in _lane_instances
                        if (not getattr(gate, '_shared', False)
                            and getattr(gate, '_enabled', True)))
    has_shared = _shared_configured or _shared_instance is not None or any(
        getattr(gate, '_shared', False) and getattr(gate, '_enabled', True)
        for gate in _lane_instances)

    lines = [
        "NFC Reader commands: (use NFC_HELP ADVANCED=1 CALLBACKS=1 "
        "LOW_LEVEL=1 for full command set)",
        "NFC_HELP : Display the complete set of NFC commands and functions",
        "NFC_STATUS : Show every configured NFC reader",
        "NFC_DOCTOR : Check NFC config, readers, Spoolman, and Happy Hare hooks",
        "NFC_REGISTER UID=TAG_UID SPOOL_ID=SPOOL_ID : Assign a UID to an existing Spoolman spool",
        "NFC_LED_TEST ALL=1 CYCLES=2 : Test configured lane tag-read LED effect on every enabled lane",
        "NFC GATE=<#> HELP=1 : Show commands for one per-lane reader",
        "NFC GATE=<#> STATUS : Show one per-lane reader state",
        "NFC GATE=<#> INIT=1 : Re-run reader hardware init",
        "NFC GATE=<#> SCAN=1 : Scan hardware once, no Spoolman/Happy Hare dispatch",
        "NFC GATE=<#> JOG_SCAN=1 : Start scan-jog to find tag on a loaded spool",
        "NFC GATE=<#> LED_TEST=1 CYCLES=2 : Test configured lane tag-read LED effect",
        "NFC GATE=<#> POLL=1 : Run one full read/resolve cycle",
        "NFC GATE=<#> APPLY=1 : Send cached spool to Happy Hare now",
        "NFC GATE=<#> CLEAR_CACHE=1 : Clear cached spool/UID, no Happy Hare dispatch",
        "NFC GATE=<#> HH_SYNC=1 SPOOL_ID=<n> : Seed lane cache from Happy Hare gate map",
        "NFC GATE=<#> READ=1 : Start timer polling",
        "NFC GATE=<#> READ=0 : Stop timer polling",
    ]
    if lane_gates:
        lines.append("Configured lane gates : %s" %
                     ", ".join(str(gate) for gate in lane_gates))
    else:
        lines.append("Configured lane gates : none")

    if has_shared:
        lines.extend([
            "",
            "Shared reader commands:",
            "NFC_SHARED HELP=1 : Show shared reader commands",
            "NFC_SHARED STATUS=1 : Show detailed shared reader state",
            "NFC_SHARED SUMMARY=1 : Show one-line shared reader state",
            "NFC_SHARED READ=1 : Start shared polling",
            "NFC_SHARED READ=0 : Stop shared polling",
            "NFC_SHARED CANCEL=1 : Cancel a staged shared spool",
            "NFC_SHARED REPLACE=1 : Discard a staged spool and scan another",
            "NFC_SHARED RESET=1 : Clear shared state, restore LEDs, and poll",
            "NFC_SHARED LED_TEST=1 : Test configured shared tag-read LED effect",
        ])
        if advanced:
            lines.extend([
                "",
                "Advanced shared-reader commands:",
                "NFC_SHARED CLEAR=1 : Clear pending state and stop polling",
                "NFC_SHARED PRELOAD_CHECK=1 : Happy Hare hook command; approve NEXT_SPOOLID if valid",
                "NFC_SHARED PRELOAD_COMMIT=1 SPOOL_ID=<n> : Happy Hare hook command; clear pending after NEXT_SPOOLID",
                "NFC_SHARED PRELOAD_CLEAR_ASSIGNED=1 SPOOL_ID=<n> GATE=<n> : Happy Hare hook command; clear already-assigned shared spool",
                "NFC_SHARED POLL=1 : Run one full read/resolve cycle",
                "NFC_SHARED SCAN=1 : Raw hardware scan only",
                "NFC_SHARED INIT=1 : Re-run NFC Reader init",
                "NFC_SHARED CLEAR_CACHE=1 : Clear tag cache, keeping pending spool",
            ])

    if callbacks:
        lines.extend([
            "",
            "Callbacks and macros:",
            "_NFC_SPOOL_CHANGED : Per-lane spool assignment callback",
            "_NFC_TAG_NO_SPOOL : Per-lane UID-only callback",
            "_NFC_SPOOL_REMOVED : Per-lane spool removal callback",
            "_NFC_HH_SYNC_ONE : Re-seed one lane cache from Happy Hare",
            "NFC_HH_SYNC_CACHE : Re-seed all lane caches from Happy Hare",
            "_NFC_SHARED_PRELOAD : Happy Hare pre-load hook for shared reader",
        ])

    if low_level:
        lines.extend([
            "",
            "Low-level debug commands:",
            "PN532 I2C/frame debug:",
            "NFC GATE=<#> STEP=HELP : Show PN532 low-level debug help",
            "NFC GATE=<#> STEP=WAKEUP : Write wake byte to PN532",
            "NFC GATE=<#> STEP=READY : Read PN532 ready status byte",
            "NFC GATE=<#> STEP=FIRMWARE_WRITE : Send GetFirmwareVersion frame",
            "NFC GATE=<#> STEP=FIRMWARE_RESPONSE : Read firmware response",
            "NFC GATE=<#> STEP=SAM_WRITE : Send SAMConfiguration frame",
            "NFC GATE=<#> STEP=SAM_RESPONSE : Read SAMConfiguration response",
            "RC522 SPI/register debug:",
            "NFC_SHARED RC522_DUMP_REGS=1 : Read key RC522 registers",
            "NFC_SHARED RC522_REGISTER=TxControlReg : Read one RC522 register",
            "NFC_SHARED RC522_REGISTER=TxControlReg VALUE=83 : Write one RC522 register",
            "NFC_SHARED RC522_ANTENNA_ENABLE=1 : Enable RC522 antenna TX bits",
            "NFC_SHARED RC522_TAG_WAKE=1 : Run a 7-bit REQA tag-wake probe",
            "NFC_SHARED RC522_FIFO_TRANSCEIVE='93 20' BIT_FRAMING=0 : Raw FIFO transceive",
        ])
    return lines


class NFCGateDefaults:
    def __init__(self, config):
        self.reader_type        = reader_factory.reader_type_from_config(config)
        self.spoolman_url       = config.get('spoolman_url', '')
        self.moonraker_url      = config.get('moonraker_url',
                                             'http://127.0.0.1:7125')
        self.spoolman_rfid_key  = config.get('spoolman_rfid_key', 'rfid_tag')
        self.spoolman_timeout   = config.getfloat('spoolman_timeout', 5.0,
                                                   minval=0.5, maxval=30.0)
        self.spoolman_cache_ttl = config.getfloat('spoolman_cache_ttl', 300.0,
                                                   minval=0., maxval=3600.)
        self.poll_interval      = config.getfloat('poll_interval', 10.,
                                                   minval=1., maxval=3600.)
        self.startup_polling    = config.getint('startup_polling', -1,
                                                 minval=-1, maxval=1)
        self.startup_poll_delay = config.getfloat('startup_poll_delay', 0.,
                                                   minval=0., maxval=3600.)
        self.absent_threshold   = config.getint('absent_threshold', 3,
                                                 minval=1, maxval=255)
        self.transceive_delay   = config.getfloat('transceive_delay', 0.250,
                                                   minval=0.050, maxval=2.0)
        self.crc_delay          = config.getfloat('crc_delay', 0.050,
                                                   minval=0.005, maxval=1.0)
        self.debug              = config.getint('debug', 2, minval=0, maxval=4)
        self.console_output, self.console_log_level = _get_console_config(config)
        self.low_level_debug    = pn532_driver.get_low_level_debug(config)
        self.i2c_address        = config.getint('i2c_address', 0x24,
                                                 minval=0, maxval=127)
        self.i2c_bus            = config.get('i2c_bus', None)
        self.scan_jog_mm        = config.getfloat('scan_jog_mm', 50.0,
                                                   minval=1.0, maxval=500.0)
        self.scan_jog_max       = _optional_float_config(
            config, 'scan_jog_max', None, minval=1.0, maxval=5000.0)
        self.scan_rewind_buffer_mm = config.getfloat(
            'scan_rewind_buffer_mm', 30.0,
            minval=0.0, maxval=500.0)
        self.scan_decode_retry_mm = config.getfloat(
            'scan_decode_retry_mm', 5.0,
            minval=0.0, maxval=50.0)
        self.scan_decode_retry_rounds = config.getint(
            'scan_decode_retry_rounds', 5,
            minval=0, maxval=10)
        self.scan_reads_per_position = config.getint(
            'scan_reads_per_position', 3,
            minval=1, maxval=20)
        self.scan_poll_interval = config.getfloat('scan_poll_interval', 0.1,
                                                   minval=0.1, maxval=5.0)
        self.scan_motion_mode = _get_scan_motion_mode(config, 'continuous')
        self.scan_continuous_step_mm = config.getfloat(
            'scan_continuous_step_mm', 150.0,
            minval=1.0, maxval=500.0)
        self.scan_continuous_speed = config.getfloat(
            'scan_continuous_speed', 150.0,
            minval=1.0, maxval=500.0)
        self.scan_continuous_accel = config.getfloat(
            'scan_continuous_accel', 2000.0,
            minval=1.0, maxval=10000.0)
        self.scan_continuous_poll_interval = config.getfloat(
            'scan_continuous_poll_interval', 0.05,
            minval=0.01, maxval=5.0)
        self.scan_enabled         = config.getboolean('scan_enabled', True)
        self.tag_parsing          = config.getboolean('tag_parsing', False)
        self.tag_max_pages        = config.getint('tag_max_pages', 16,
                                                   minval=4, maxval=135)
        self.bambu_reads          = config.getboolean('bambu_reads', False)
        self.spoolman_auto_create     = config.getboolean('spoolman_auto_create', False)
        self.scan_searching_effect    = config.get('scan_searching_effect',   'mmu_clockwise_slow')
        self.scan_tag_read_effect     = config.get('scan_tag_read_effect',    'mmu_RFID_read')
        self.scan_rewind_effect       = config.get('scan_rewind_effect',      'mmu_anticlock_fast')
        self.lane_auto_create_effect  = config.get('lane_auto_create_effect', 'mmu_RFID_creating')
        self.lane_unresolved_effect   = config.get('lane_unresolved_effect',  'mmu_RFID_unresolved')
        if self.bambu_reads and not self.tag_parsing:
            _add_diagnostic_warning(
                "bambu_reads=True has no effect while tag_parsing=False")
        if self.spoolman_auto_create and not self.tag_parsing:
            _add_diagnostic_warning(
                "spoolman_auto_create=True has no effect while tag_parsing=False")
        if (self.spoolman_auto_create
                and not _spoolman_url_enabled(self.spoolman_url)):
            _add_diagnostic_warning(
                "spoolman_auto_create=True requires Spoolman to be enabled")

        self._printer = config.get_printer()
        log_file = config.get('log_file', '')
        try:
            configure(log_file, printer=self._printer,
                      console_output=self.console_output,
                      console_log_level=self.console_log_level)
        except Exception as e:
            import logging
            logging.getLogger().warning(
                "nfc_gate: could not configure NFC logging %r: %s",
                log_file, e)

        if _spoolman_url_enabled(self.spoolman_url):
            self._spoolman = SpoolmanClient(
                self.spoolman_url,
                rfid_key=self.spoolman_rfid_key,
                timeout=self.spoolman_timeout,
                cache_ttl=self.spoolman_cache_ttl,
                debug=self.debug,
                moonraker_url=self.moonraker_url)
            logger.info("nfc_gate: Spoolman enabled — url=%s rfid_key=%s",
                        self.spoolman_url, self.spoolman_rfid_key)
        else:
            self._spoolman = None
            if self.spoolman_url:
                logger.info("nfc_gate: Spoolman disabled by config")
            else:
                logger.warning(
                    "nfc_gate: spoolman_url not set — set spoolman_url in "
                    "[nfc_gate]. Use 'auto' to read Moonraker.")

class NFCGate:
    _active_scan_gate = None  # class-level scan lock; shared across all instances
    _scan_queue = []  # gate numbers waiting for their turn; only AUTO (hook) requests queue
    # Extension-owned virtual-endstop registry. Happy Hare owns the gear rail,
    # but the NFC endstop objects and their gate bindings belong to this add-on.
    _nfc_endstops_by_gate = {}

    def __init__(self, config, defaults=None, mmu_unit=None, mmu_gate=None,
                 shared=None, name=None):
        self.printer  = config.get_printer()
        self.reactor  = self.printer.get_reactor()
        self._name    = name or config.get_name().split()[-1]
        self._mmu_unit = mmu_unit
        native = mmu_unit is not None

        d = defaults
        self._defaults = defaults
        self._reader_object = None

        # Read shared first — it controls how subsequent params are parsed.
        self._shared = (bool(shared) if native
                        else config.getboolean('shared', False))
        self._enabled = config.getboolean('enabled', True)
        self._reader_type = reader_factory.reader_type_from_config(
            config, d.reader_type if d else 'pn532')
        if self._shared and self._enabled:
            global _shared_configured
            _shared_configured = True
        _i2c_mcu_name = config.get('i2c_mcu', 'mcu')
        _m = re.search(r'(\d+)$', _i2c_mcu_name)
        self._shared_mcu_index = int(_m.group(1)) if _m else None
        if self._shared and self._enabled:
            for existing in _lane_instances:
                if (getattr(existing, '_shared', False)
                        and getattr(existing, '_enabled', True)):
                    raise config.error(
                        "nfc_gate [%s]: only one shared reader may be configured"
                        % self._name)

        # Gate number: required for lane readers; internal sentinel for shared.
        # The shared reader has no Happy Hare gate assignment — the sentinel is never
        # passed to Happy Hare and is not user-configurable.
        if self._shared:
            self._gate = _SHARED_GATE_SENTINEL
        elif native:
            self._gate = int(mmu_gate)
        else:
            self._gate = config.getint('mmu_gate', minval=0)
        self._diagnostic_warnings = []
        if not self._enabled:
            self._poll_interval = 0.0
            self._startup_polling = 0
            self._startup_poll_delay = 0.0
            self._absent_threshold = 3
            self._debug = d.debug if d else 2
            self._low_level_debug = False
            self._reader_type = reader_factory.reader_type_from_config(
                config, d.reader_type if d else 'pn532')
            self._console_output = d.console_output if d else False
            self._console_log_level = d.console_log_level if d else 'warning'
            self._spoolman = d._spoolman if d is not None else None
            self._reader = None
            self._state = GateState(self._gate, self._absent_threshold)
            self._failed = False
            self._polling = False
            self._scan_enabled = False
            self._scan_motion_mode = d.scan_motion_mode if d else 'continuous'
            self._scan_continuous_step_mm = d.scan_continuous_step_mm if d else 150.0
            self._scan_continuous_speed = d.scan_continuous_speed if d else 150.0
            self._scan_continuous_accel = d.scan_continuous_accel if d else 2000.0
            self._scan_continuous_poll_interval = d.scan_continuous_poll_interval if d else 0.05
            self._tag_parsing = False
            self._bambu_reads = False
            self._spoolman_auto_create = False
            self._scan_searching_effect   = d.scan_searching_effect   if d else 'mmu_clockwise_slow'
            self._scan_tag_read_effect    = d.scan_tag_read_effect    if d else 'mmu_RFID_read'
            self._scan_rewind_effect      = d.scan_rewind_effect      if d else 'mmu_anticlock_fast'
            self._lane_auto_create_effect = d.lane_auto_create_effect if d else 'mmu_RFID_creating'
            self._lane_unresolved_effect  = d.lane_unresolved_effect  if d else 'mmu_RFID_unresolved'
            self._hh_load_paused = False
            self._shared_pending_spool = None
            self._shared_pending_uid = None
            self._shared_last_error = None
            self._shared_read_deadline = 0.0
            self._has_per_lane_readers = False
            self._gcode = None
            logger.info("[%s]: NFC reader disabled by config", self._name)
            return
        self._poll_interval    = config.getfloat('poll_interval',
                                                  d.poll_interval if d else 10.,
                                                  minval=1., maxval=3600.)
        self._startup_polling  = config.getint('startup_polling',
                                                d.startup_polling if d else -1,
                                                minval=-1, maxval=1)
        self._startup_poll_delay = config.getfloat(
            'startup_poll_delay',
            d.startup_poll_delay if d else 0.,
            minval=0., maxval=3600.)
        self._absent_threshold = config.getint('absent_threshold',
                                                d.absent_threshold if d else 3,
                                                minval=1, maxval=255)
        transceive_delay       = config.getfloat('transceive_delay',
                                                  d.transceive_delay if d else 0.250,
                                                  minval=0.050, maxval=2.0)
        crc_delay              = config.getfloat('crc_delay',
                                                  d.crc_delay if d else 0.050,
                                                  minval=0.005, maxval=1.0)
        self._debug            = config.getint('debug',
                                               d.debug if d else 2,
                                               minval=0, maxval=4)
        self._low_level_debug  = pn532_driver.get_low_level_debug(
            config, d.low_level_debug if d else False)
        console_output, console_log_level = _get_console_config(
            config,
            d.console_output if d else False,
            d.console_log_level if d else 'warning')
        self._console_output = console_output
        self._console_log_level = console_log_level
        if d is None:
            log_file = config.get('log_file', '')
            configure(log_file, printer=self.printer,
                      console_output=console_output,
                      console_log_level=console_log_level)

        if d is not None:
            # Share the single SpoolmanClient created by NFCGateDefaults.
            self._spoolman = d._spoolman
        else:
            # No base [nfc_gate] section — create a per-lane client as fallback.
            spoolman_url      = config.get('spoolman_url', '')
            moonraker_url     = config.get('moonraker_url', 'http://127.0.0.1:7125')
            spoolman_rfid_key = config.get('spoolman_rfid_key', 'rfid_tag')
            spoolman_timeout  = config.getfloat('spoolman_timeout', 5.0,
                                                 minval=0.5, maxval=30.0)
            spoolman_cache_ttl = config.getfloat('spoolman_cache_ttl', 300.0,
                                                  minval=0., maxval=3600.)
            if _spoolman_url_enabled(spoolman_url):
                self._spoolman = SpoolmanClient(
                    spoolman_url,
                    rfid_key=spoolman_rfid_key,
                    timeout=spoolman_timeout,
                    cache_ttl=spoolman_cache_ttl,
                    debug=self._debug,
                    moonraker_url=moonraker_url)
                logger.info("[%s]: Spoolman enabled — url=%s rfid_key=%s",
                            self._name, spoolman_url, spoolman_rfid_key)
            else:
                self._spoolman = None
                if spoolman_url:
                    logger.info("[%s]: Spoolman disabled by config",
                                self._name)
                else:
                    logger.warning(
                        "[%s]: spoolman_url not set — set spoolman_url in "
                        "[nfc_gate] or [nfc_gate %s]. Use 'auto' to read Moonraker.",
                        self._name, self._name)

        # Per-lane hardware is owned by the MmuUnit built from
        # mmu_hardware.cfg.  Defer resolving its nfc_reader/nfc_readers entry
        # until klippy:connect, when the complete MmuMachine exists.  A shared
        # nfc_gate is not associated with one MMU gate and retains its legacy
        # explicit hardware construction until it is migrated separately.
        if self._shared and not native:
            self._reader = reader_factory.create_reader(
                config, d, self._reader_type, self._gate, self._debug,
                low_level_debug=self._low_level_debug,
                sleep_fn=self._reactor_sleep,
                transceive_delay=transceive_delay,
                crc_delay=crc_delay)
        else:
            self._reader = None
        self._state      = GateState(self._gate, self._absent_threshold)
        self._suppress_next_dispatch_uid   = None
        self._suppress_next_dispatch_spool = None  # paired with uid — suppress only when both match
        self._hh_seed_spool_id   = None  # set on startup from Happy Hare gate map; cleared after first match
        self._hh_seed_available  = False  # True only when Happy Hare had the gate marked available at seed time
        self._hh_confirmed_spool = None  # last spool Happy Hare acknowledged; enables _check_hh_cleared
        self._hh_load_paused     = False  # True while Happy Hare owns this gate assignment
        self._failed     = False
        self._klipper    = KlipperInterface(
            self.printer, self.reactor, self._debug, name=self._name,
            spoolman_enabled=self._spoolman is not None)
        self._polling    = False
        self._poll_timer    = self.reactor.register_timer(self._poll_timer_event)
        self._startup_check_timer = self.reactor.register_timer(
            self._startup_check_unknown_gate_event)
        self._warning_timer = self.reactor.register_timer(
            self._warning_timer_event)
        # _shared_led_failsafe_event lives on MmuSharedNfcReader now (moved
        # 2026-07-14) -- registering it here unconditionally would raise
        # AttributeError for every plain lane NFCGate. Only read by
        # MmuSharedNfcReader's own _shared_arm_led_failsafe()/
        # _shared_cancel_led_failsafe(), so None is fine for lane instances.
        self._shared_led_failsafe_timer = (
            self.reactor.register_timer(self._shared_led_failsafe_event)
            if self._shared else None)

        self._scan_jog_mm   = config.getfloat('scan_jog_mm',
                                               d.scan_jog_mm if d else 50.0,
                                               minval=1.0, maxval=500.0)
        self._scan_jog_max = _optional_float_config(
            config, 'scan_jog_max',
            d.scan_jog_max if d else None,
            minval=1.0, maxval=5000.0)
        self._scan_rewind_buffer_mm = config.getfloat(
            'scan_rewind_buffer_mm',
            d.scan_rewind_buffer_mm if d else 30.0,
            minval=0.0, maxval=500.0)
        self._scan_decode_retry_mm = config.getfloat(
            'scan_decode_retry_mm',
            d.scan_decode_retry_mm if d else 5.0,
            minval=0.0, maxval=50.0)
        self._scan_decode_retry_rounds = config.getint(
            'scan_decode_retry_rounds',
            d.scan_decode_retry_rounds if d else 5,
            minval=0, maxval=10)
        self._scan_reads_per_position = config.getint(
            'scan_reads_per_position',
            d.scan_reads_per_position if d else 3,
            minval=1, maxval=20)
        self._scan_max_mm   = None
        self._mmu_vars_path = None
        self._bowden_lengths = None
        self._scan_poll_interval = config.getfloat('scan_poll_interval',
                                                    d.scan_poll_interval if d else 0.1,
                                                    minval=0.1, maxval=5.0)
        self._scan_motion_mode = _get_scan_motion_mode(
            config, d.scan_motion_mode if d else 'continuous')
        self._scan_continuous_step_mm = config.getfloat(
            'scan_continuous_step_mm',
            d.scan_continuous_step_mm if d else 150.0,
            minval=1.0, maxval=500.0)
        self._scan_continuous_speed = config.getfloat(
            'scan_continuous_speed',
            d.scan_continuous_speed if d else 150.0,
            minval=1.0, maxval=500.0)
        self._scan_continuous_accel = config.getfloat(
            'scan_continuous_accel',
            d.scan_continuous_accel if d else 2000.0,
            minval=1.0, maxval=10000.0)
        self._scan_continuous_poll_interval = config.getfloat(
            'scan_continuous_poll_interval',
            d.scan_continuous_poll_interval if d else 0.05,
            minval=0.01, maxval=5.0)
        # scan_enabled: forced false for shared (no physical EMU lane for jog).
        if self._shared:
            self._scan_enabled = False
        else:
            self._scan_enabled = config.getboolean('scan_enabled',
                                                    d.scan_enabled if d else True)
        self._tag_parsing          = config.getboolean('tag_parsing',
                                                        d.tag_parsing if d else False)
        self._tag_max_pages        = config.getint('tag_max_pages',
                                                    d.tag_max_pages if d else 16,
                                                    minval=4, maxval=135)
        self._bambu_reads          = config.getboolean('bambu_reads',
                                                        d.bambu_reads if d else False)
        if self._bambu_reads and not self._tag_parsing:
            warning = ("[%s]: bambu_reads=True has no effect when "
                       "tag_parsing=False — set tag_parsing: True to enable "
                       "Bambu/MIFARE reads" % self._name)
            logger.warning(warning)
            self._diagnostic_warnings.append(warning)
        self._spoolman_auto_create = config.getboolean('spoolman_auto_create',
                                                        d.spoolman_auto_create if d else False)
        if self._spoolman_auto_create and not self._tag_parsing:
            warning = ("[%s]: spoolman_auto_create=True has no effect when "
                       "tag_parsing=False" % self._name)
            logger.warning(warning)
            self._diagnostic_warnings.append(warning)
        if self._spoolman_auto_create and self._spoolman is None:
            warning = ("[%s]: spoolman_auto_create=True requires Spoolman to "
                       "be enabled" % self._name)
            logger.warning(warning)
            self._diagnostic_warnings.append(warning)
        self._scan_searching_effect   = config.get(
            'scan_searching_effect',
            d.scan_searching_effect if d else 'mmu_clockwise_slow')
        self._scan_tag_read_effect    = config.get(
            'scan_tag_read_effect',
            d.scan_tag_read_effect if d else 'mmu_RFID_read')
        self._scan_rewind_effect      = config.get(
            'scan_rewind_effect',
            d.scan_rewind_effect if d else 'mmu_anticlock_fast')
        self._lane_auto_create_effect = config.get(
            'lane_auto_create_effect',
            d.lane_auto_create_effect if d else 'mmu_RFID_creating')
        self._lane_unresolved_effect  = config.get(
            'lane_unresolved_effect',
            d.lane_unresolved_effect if d else 'mmu_RFID_unresolved')

        self._scan_timer           = None
        self._scan_mode            = False
        self._scan_mm_total        = 0.0
        self._scan_next_chunk_time = 0.0
        self._scan_continuous_move_inflight = False
        self._scan_continuous_move_complete_time = 0.0
        self._scan_continuous_last_move_mm = 0.0
        self._scan_continuous_tag_pending = False
        self._scan_continuous_direct_available = True
        self._scan_decode_retry_attempts = 0
        self._scan_decode_retry_uid      = None
        self._scan_decode_retry_offset   = 0.0
        self._scan_left_neighbor_gate = -1
        self._scan_left_neighbor_shift_mm = 0.0
        self._scan_left_neighbor_shifted = False
        self._scan_left_neighbor_identity = None
        self._scan_left_neighbor_attempts = 0
        self._scan_found_event     = None  # cached event suppressed during jog; dispatched after rewind

        # ── Shared reader config and state ───────────────────────────────────
        # (_shared, _gate, _scan_enabled already set above)
        if self._shared:
            self._shared_pending_timeout = 30.0  # overwritten at connect from [mmu] pending_spool_id_timeout
            self._shared_read_timeout = config.getfloat(
                'shared_read_timeout', 120.0, minval=1.0)
            self._shared_led_segment = config.get(
                'shared_led_segment', 'exit').strip().lower()
            self._shared_tag_read_effect = config.get(
                'shared_tag_read_effect', '')
            self._shared_read_effect_duration = config.getfloat(
                'read_effect_duration', _SHARED_READ_EFFECT_DURATION,
                minval=0.1)
            self._shared_bypass_tag_read_effect = config.get(
                'shared_bypass_tag_read_effect', 'mmu_RFID_bypass_read')
            self._shared_bypass_read_effect_duration = config.getfloat(
                'bypass_read_effect_duration',
                _SHARED_BYPASS_READ_EFFECT_DURATION, minval=0.1)
            self._shared_spool_ready_effect = config.get(
                'shared_spool_ready_effect', '')
            self._shared_bypass_spool_ready_effect = config.get(
                'shared_bypass_spool_ready_effect', 'mmu_RFID_bypass_ready')
            self._shared_bypass_ready_effect_duration = config.getfloat(
                'bypass_ready_effect_duration',
                _SHARED_BYPASS_READY_EFFECT_DURATION, minval=0.1)
            self._shared_tag_unresolved_effect = config.get(
                'shared_tag_unresolved_effect', '')
            self._shared_unresolved_effect_duration = config.getfloat(
                'unresolved_effect_duration',
                _SHARED_UNRESOLVED_EFFECT_DURATION, minval=0.1)
            self._shared_spool_warning_effect = config.get(
                'shared_spool_warning_effect', 'mmu_RFID_warning')
            self._shared_auto_create_effect = config.get(
                'shared_auto_create_effect', '')
            self._shared_force_spool_id  = config.getboolean(
                'force_spool_id', True)
            self._shared_missed_limit    = config.getint(
                'shared_missed_limit', _SHARED_MISSED_RESOLUTION_LIMIT,
                minval=1)
        else:
            self._shared_pending_timeout = 30.0
            self._shared_read_timeout    = 120.0
            self._shared_tag_read_effect    = ''
            self._shared_read_effect_duration = _SHARED_READ_EFFECT_DURATION
            self._shared_bypass_tag_read_effect = ''
            self._shared_bypass_read_effect_duration = (
                _SHARED_BYPASS_READ_EFFECT_DURATION)
            self._shared_spool_ready_effect = ''
            self._shared_bypass_spool_ready_effect = ''
            self._shared_bypass_ready_effect_duration = (
                _SHARED_BYPASS_READY_EFFECT_DURATION)
            self._shared_tag_unresolved_effect = ''
            self._shared_unresolved_effect_duration = (
                _SHARED_UNRESOLVED_EFFECT_DURATION)
            self._shared_spool_warning_effect  = ''
            self._shared_auto_create_effect = ''
            self._shared_force_spool_id     = False
            self._shared_missed_limit    = _SHARED_MISSED_RESOLUTION_LIMIT

        self._shared_pending_uid            = None
        self._shared_pending_spool          = None
        self._shared_pending_deadline       = 0.0
        self._shared_pending_warning_fired  = False
        self._shared_pending_auto_created   = False
        self._shared_last_error             = None
        self._shared_last_action            = None
        self._shared_read_deadline          = 0.0
        self._shared_missed_resolutions     = 0
        self._shared_warning_timer          = None
        self._shared_led_failsafe_deadline  = 0.0
        self._shared_led_failsafe_reason    = None
        self._shared_preload_spool        = None
        self._shared_preload_uid          = None
        self._shared_preload_auto_created = False
        self._shared_preload_coordinator  = None
        self._shared_polling_suspended_for_print = False
        self._has_per_lane_readers        = False
        self._mmu_led_unit                = 'unit0'

        # delayed-init state
        self._gcode = None
        self.mmu = None

        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._handle_disconnect)
        if self._shared and self._enabled:
            self.printer.register_event_handler(
                'idle_timeout:printing', self._handle_print_start)
            self.printer.register_event_handler(
                'idle_timeout:ready', self._handle_print_end)

    def _happy_hare_allows_scan_action(self, action):
        return action == ACTION_IDLE or action == ACTION_CHECKING

    def _cmd_help(self, gcmd):
        lines = [
            "NFC GATE=%d commands:" % self._gate,
            "  NFC GATE=%d HELP     - show this help" % self._gate,
            "  NFC GATE=%d STATUS   - show this gate state" % self._gate,
            "  NFC GATE=%d INIT=1    - re-run reader init" % self._gate,
            "  NFC GATE=%d SCAN=1    - scan hardware once, no Spoolman/Happy Hare dispatch" % self._gate,
            "  NFC GATE=%d LED_TEST=1 CYCLES=2 - test configured lane tag-read LED effect" % self._gate,
            "  NFC GATE=%d JOG_SCAN=1 - start scan-jog (same as automatic pre-load trigger)" % self._gate,
            "  NFC GATE=%d POLL=1    - run one full NFC_Manager poll for this gate" % self._gate,
            "  NFC GATE=%d APPLY=1   - send cached spool to Happy Hare now" % self._gate,
            "  NFC GATE=%d CLEAR_CACHE=1 - clear cached spool lookup, no Happy Hare dispatch" % self._gate,
            "  NFC GATE=%d HH_SYNC=1 SPOOL_ID=<n> - seed lane cache from Happy Hare gate map (called by NFC_HH_SYNC_CACHE macro)" % self._gate,
            "  NFC GATE=%d READ=1    - start timer polling" % self._gate,
            "  NFC GATE=%d READ=0    - stop timer polling" % self._gate,
        ]
        if self._low_level_debug and self._reader_type == 'pn532':
            lines.extend(pn532_driver.low_level_debug_help_lines(
                "NFC GATE=%d" % self._gate))
        if self._low_level_debug and self._reader_type == 'rc522':
            lines.extend(rc522_driver.low_level_debug_help_lines(
                "NFC GATE=%d" % self._gate))
        gcmd.respond_info('\n'.join(lines))

    def _manual_scan(self, gcmd):
        if self._shared and self._is_printing():
            logger.warning(
                "[%s]: shared scan skipped while printing",
                self._name)
            gcmd.respond_info(
                "[WARN] NFC[%s]: shared scan skipped while printing" % self._name)
            return
        try:
            target_info = self._reader.read_target()
            if target_info is None:
                logger.info("[%s]: no tag detected", self._name)
                gcmd.respond_info(color_console_tags(
                    "NFC[%s]: no tag detected" % self._name))
                return
            sens_res = int(target_info.get('sens_res', 0) or 0)
            sak = target_info.get('sak')
            sak_text = "N/A" if sak is None else "0x%02X" % int(sak)
            logger.info(
                "[%s]: UID=%s Tg=%s SENS_RES=0x%04X SAK=%s UIDLen=%d",
                self._name, target_info['uid'], target_info['target'],
                sens_res, sak_text, target_info['uid_length'])
            gcmd.respond_info(
                color_console_tags(
                    "NFC[%s]: UID=%s Tg=%s SENS_RES=0x%04X SAK=%s UIDLen=%d"
                    % (self._name, target_info['uid'], target_info['target'],
                       sens_res, sak_text, target_info['uid_length'])))
        finally:
            if hasattr(self._reader, '_release_current_target'):
                self._reader._release_current_target(reason="manual_scan")

    def _manual_init(self, gcmd):
        self._failed = False
        try:
            self._reader.init()
            alive = self._reader.is_alive()
            if self._reader_object is not None:
                self._reader_object.alive = bool(alive)
            self._failed = not alive
            reader_label = _reader_label(self._reader_type)
            if alive:
                logger.info("[%s]: %s OK", self._name, reader_label)
            else:
                logger.error(
                    "[%s]: %s did not respond — %s",
                    self._name, reader_label,
                    _reader_wiring_hint(self._reader_type))
            gcmd.respond_info(color_console_tags(
                "%s NFC[%s]: %s %s" %
                ("[OK]" if alive else "[WARN]", self._name, reader_label,
                 "OK" if alive else "not responding")))
            if (self._shared and alive and self._startup_polling == 1
                    and not self._is_printing()
                    and self._shared_pending_spool is None):
                self._shared_read_deadline = 0.0
                self._polling = True
                self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
                logger.info(
                    "[%s]: startup polling enabled; first poll in %.1fs",
                    self._name, 0.0)
                gcmd.respond_info(color_console_tags(
                    "NFC[%s]: startup polling resumed" % self._name))
        except Exception as e:
            self._failed = True
            logger.error("[%s]: init error: %s", self._name, e)
            gcmd.respond_info(color_console_tags(
                "[WARN] NFC[%s]: init failed: %s" % (self._name, e)))

    def _lane_led_test(self, gcmd=None, respond=True, cycles=None):
        effect = (getattr(self, '_scan_tag_read_effect', '') or '').strip()
        if not effect:
            msg = "[WARN] NFC[%s]: no lane tag-read LED effect configured" % self._name
            logger.warning(msg)
            if respond and gcmd is not None:
                gcmd.respond_info(color_console_tags(msg))
            return False
        if cycles is None:
            cycles = (_led_test_cycles_from_gcmd(gcmd)
                      if gcmd is not None
                      else LANE_LED_TEST_DEFAULT_CYCLES)
        cycles = max(1, min(int(cycles), LANE_LED_TEST_MAX_CYCLES))
        result = self._play_lane_led_test_effect(effect)
        gate_effect = result.effect
        if not result.ok:
            msg = ("[WARN] NFC[%s]: LED test failed for effect %s: %s"
                   % (self._name, gate_effect, result.error))
            logger.warning(msg)
            if respond and gcmd is not None:
                gcmd.respond_info(color_console_tags(msg))
            return False
        self._schedule_lane_led_test_cycles(effect, cycles)
        logger.info("[%s]: lane LED test started effect=%s cycles=%d",
                    self._name, gate_effect, cycles)
        if respond and gcmd is not None:
            gcmd.respond_info(color_console_tags(
                "[OK] NFC[%s]: lane LED test started (%s cycles=%d)"
                % (self._name, gate_effect, cycles)))
        return True

    def _play_lane_led_test_effect(self, effect):
        class Result:
            pass
        result = Result()
        result.effect = effect
        result.error = None
        try:
            result.ok = self.mmu.led_manager.set_transient_effect(
                self._mmu_unit, effect, segment='exit', gate=self._gate)
        except Exception as e:
            result.ok = False
            result.error = e
        return result

    def _schedule_lane_led_test_cycles(self, effect, cycles):
        if not effect:
            return
        stride = LANE_LED_TEST_DURATION + LANE_LED_TEST_GAP
        for cycle in range(1, cycles):
            def replay(eventtime, _effect=effect):
                self.mmu.led_manager.set_transient_effect(
                    self._mmu_unit, _effect, segment='exit', gate=self._gate)
                return self.reactor.NEVER
            self.reactor.register_timer(
                replay, self.reactor.monotonic() + cycle * stride)

        def restore(eventtime):
            if not getattr(self, '_scan_mode', False):
                self.mmu.led_manager.restore_transient_effect(
                    self._mmu_unit, segment='exit', gate=self._gate)
            return self.reactor.NEVER
        self.reactor.register_timer(
            restore, self.reactor.monotonic()
            + (cycles - 1) * stride + LANE_LED_TEST_DURATION + 0.1)


    def _set_reading(self, gcmd, enabled):
        if enabled:
            if self._failed:
                if self._shared:
                    logger.error(
                        "[%s]: shared READ=1 refused — "
                        "reader failed; run INIT=1 first",
                        self._name)
                else:
                    logger.error(
                        "[%s]: gate %d READ=1 refused — "
                        "reader failed; run INIT=1 first",
                        self._name, self._gate)
                gcmd.respond_info(color_console_tags(
                    "[WARN] NFC[%s]: reader failed; run INIT=1 first"
                    % self._name))
                return
            if self._shared:
                if self._is_printing():
                    logger.warning(
                        "[%s]: shared READ=1 refused — printing",
                        self._name)
                    gcmd.respond_info(
                        "[WARN] NFC[%s]: shared polling not started while printing"
                        % self._name)
                    return
                if self._shared_pending_spool is not None:
                    logger.warning(
                        "[%s]: shared READ=1 refused — "
                        "spool %s already pending",
                        self._name, self._shared_pending_spool)
                    gcmd.respond_info(
                        "[WARN] NFC[%s]: spool %s is already pending; use "
                        "NFC_SHARED REPLACE=1 to discard it and scan another, "
                        "or NFC_SHARED CANCEL=1 to cancel"
                        % (self._name, self._shared_pending_spool))
                    return
                self._shared_missed_resolutions = 0
                self._shared_last_error = None
                self._shared_read_deadline = (
                    self.reactor.monotonic() + self._shared_read_timeout)
                logger.info(
                    "[%s]: shared READ=1 — polling started "
                    "with %.0fs read timeout",
                    self._name, self._shared_read_timeout)
            self._polling = True
            self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
            if not self._shared:
                logger.info(
                    "[%s]: gate %d READ=1 — polling started",
                    self._name, self._gate)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: polling started" % self._name))
        else:
            if self._shared:
                self._shared_read_deadline = 0.0
                logger.info(
                    "[%s]: shared READ=0 — polling stopped; "
                    "pending spool=%s kept",
                    self._name, self._shared_pending_spool)
                if self._shared_pending_spool is None:
                    self._shared_restore_hh_leds()
            else:
                logger.info(
                    "[%s]: gate %d READ=0 — polling stopped",
                    self._name, self._gate)
            self._polling = False
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: polling stop requested" % self._name))

    def _clear_spool_cache(self, gcmd):
        """Clear cached spool resolution without dispatching a state change."""
        old_spool = self._state.current_spool
        self._state.current_spool = None
        self._suppress_next_dispatch_uid   = self._state.current_uid
        self._suppress_next_dispatch_spool = old_spool  # only suppress if spool is also unchanged
        if self._spoolman is not None:
            self._spoolman.clear_cache()
        if hasattr(self._reader, '_clear_current_card'):
            self._reader._clear_current_card()
        logger.info(
            "[%s]: gate %d — spool cache cleared "
            "(uid=%s old_spool=%s); next read will resolve Spoolman again",
            self._name, self._gate, self._state.current_uid, old_spool)
        gcmd.respond_info(
            color_console_tags(
                "NFC[%s]: cleared cached spool_id for gate %d; "
                "no NFC_Manager event was dispatched. Next tag read will resolve "
                "Spoolman again."
                % (self._name, self._gate)))


    def _apply_current_spool(self, gcmd):
        """Dispatch the current cached spool to Happy Hare immediately."""
        if self._state.current_spool is None:
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: no cached spool_id to apply; run POLL=1 first"
                % self._name))
            return
        uid_hex = self._state.current_uid or ''
        spool_id = self._state.current_spool
        if spool_id is DIRECT_METADATA_SPOOL:
            meta = (self._state.current_tag.meta
                    if self._state.current_tag is not None else {})
            logger.info(
                "[%s]: gate %d — manual apply metadata uid=%s",
                self._name, self._gate, uid_hex)
            self._klipper.dispatch(EVENT_CHANGED, self._gate, uid_hex,
                                   None, meta=meta)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: dispatched cached tag metadata for gate %d to "
                "Happy Hare" % (self._name, self._gate)))
            return
        logger.info(
            "[%s]: gate %d — manual apply spool=%s uid=%s",
            self._name, self._gate, spool_id, uid_hex)
        self._klipper.dispatch(EVENT_CHANGED, self._gate, uid_hex, spool_id)
        gcmd.respond_info(color_console_tags(
            "NFC[%s]: dispatched cached spool_id=%s for gate %d to "
            "Happy Hare"
            % (self._name, spool_id, self._gate)))

    def _cmd_low_level_debug(self, gcmd):
        if rc522_driver.low_level_debug_requested(gcmd):
            if self._polling:
                self._polling = False
                self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
                gcmd.respond_info(color_console_tags(
                    "NFC[%s]: polling paused for low-level RC522 debug" %
                    self._name))
            try:
                command_base = (
                    "NFC_SHARED" if self._shared else
                    "NFC GATE=%d" % self._gate)
                return rc522_driver.run_low_level_debug(
                    gcmd, self._reader, self._name, command_base,
                    self._low_level_debug)
            except Exception as e:
                gcmd.respond_info(color_console_tags(
                    "NFC[%s]: RC522 low-level debug failed: %s"
                    % (self._name, e)))
                return True
        if (pn532_driver.low_level_debug_requested(gcmd)
                and self._reader_type != 'pn532'):
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: PN532 low-level commands are not valid for "
                "reader_type=%s" % (self._name, self._reader_type)))
            return True
        if pn532_driver.low_level_debug_requested(gcmd) and self._polling:
            self._polling = False
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: polling paused for low-level PN532 debug" %
                self._name))
        try:
            return pn532_driver.run_low_level_debug(
                gcmd, self._reader, self._name,
                "NFC GATE=%d" % self._gate,
                self._low_level_debug)
        except Exception as e:
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: low-level debug failed: %s" % (self._name, e)))
            return True

    def _get_mmu(self):
        """Return Happy Hare's MmuController, resolved once and cached.

        Bound eagerly in _handle_connect(); this lazy fallback only matters
        if config include order means 'mmu' was not yet loaded at that point.
        """
        if self.mmu is None:
            self.mmu = self.printer.lookup_object('mmu', None)
        return self.mmu

    def _read_hh_status(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        return _gate_snapshot(self._get_mmu(), self._gate, eventtime)

    def _read_hh_status_for_gate(self, target_gate, eventtime=None):
        """Read another gate's live Happy Hare state (e.g. a left neighbor)."""
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        return _gate_snapshot(self._get_mmu(), target_gate, eventtime)

    def _seed_cache_from_hh(self, eventtime):
        """Read Happy Hare's gate map and pre-seed this lane's spool cache.

        Called once from _delayed_init() after the NFC Reader initialises
        successfully.  Prevents a spurious _NFC_SPOOL_CHANGED dispatch on the
        very first poll after a Klipper restart — Happy Hare already knows
        which spool is in this gate, so we should not re-tell it.

        The seed is one-shot: it is consumed (cleared) on the first
        EVENT_CHANGED poll result, regardless of whether the spool matches.
        Mismatches still dispatch normally.
        """
        try:
            hh = self._read_hh_status(eventtime)
            if not hh.present:
                logger.info(
                    "[%s]: gate %d — Happy Hare MMU object not found; "
                    "skipping startup cache seed", self._name, self._gate)
                return
            if self._gate >= hh.gate_count:
                logger.info(
                    "[%s]: gate %d — gate index exceeds Happy Hare map length "
                    "(%d gates); skipping seed", self._name, self._gate,
                    hh.gate_count)
                return

            if hh.assigned:
                self._hh_seed_spool_id  = hh.spool
                self._hh_seed_available = hh.available

                if hh.available and self._spoolman is not None:
                    # Gate is physically loaded — pre-populate NFC cache from
                    # Spoolman so status is correct before the first physical scan.
                    uid = self._spoolman.get_uid_for_spool(hh.spool)
                    if uid:
                        self._state.current_uid   = uid
                        self._state.current_spool = hh.spool
                        self._hh_confirmed_spool  = hh.spool
                        logger.info(
                            "[%s]: gate %d — startup: seeded from "
                            "Happy Hare+Spoolman spool_id=%d uid=%s",
                            self._name, self._gate, hh.spool, uid)
                    else:
                        logger.info(
                            "[%s]: gate %d — Happy Hare seed: spool_id=%d "
                            "available (no UID in Spoolman — will verify on "
                            "first poll)",
                            self._name, self._gate, hh.spool)
                else:
                    logger.info(
                        "[%s]: gate %d — Happy Hare seed: spool_id=%d  "
                        "gate_status=%s  (will verify on first physical scan)",
                        self._name, self._gate, hh.spool, hh.status)
            else:
                logger.info(
                    "[%s]: gate %d — Happy Hare reports gate %s "
                    "(spool_id=%s); no seed applied",
                    self._name, self._gate,
                    "found/no spool" if hh.available else "empty/unknown",
                    hh.spool)

        except Exception:
            logger.exception(
                "[%s]: gate %d — error reading Happy Hare gate map for "
                "startup cache seed (non-fatal, polling continues)",
                self._name, self._gate)

    def _hh_sync(self, gcmd):
        """Receive a spool_id from NFC_HH_SYNC_CACHE and set the lane seed.

        Called by NFC GATE=<#> HH_SYNC=1 SPOOL_ID=<n>.
        The macro reads Happy Hare template vars (which GCode macros can access) and
        passes the resolved spool_id here so Python can update the seed without
        needing to walk the Happy Hare object itself.
        """
        spool_id = gcmd.get_int('SPOOL_ID', -1)
        if spool_id > 0:
            self._hh_seed_spool_id = spool_id
            logger.info(
                "[%s]: gate %d — HH_SYNC: seed set to spool_id=%d",
                self._name, self._gate, spool_id)
            gcmd.respond_info(
                "NFC[%s]: Happy Hare seed → spool_id=%d  "
                "(next poll matching this spool will not re-dispatch to Happy Hare)"
                % (self._name, spool_id))
        else:
            self._hh_seed_spool_id = None
            logger.info(
                "[%s]: gate %d — HH_SYNC: gate empty/unknown, "
                "seed cleared", self._name, self._gate)
            gcmd.respond_info(
                "NFC[%s]: Happy Hare reports gate empty — seed cleared" % self._name)

    def _validate_startup_config(self):
        if not getattr(self, '_enabled', True):
            return
        if self._shared:
            raw_config = _raw_klipper_config(self.printer)
            macro = raw_config.get('gcode_macro _MMU_SEQUENCE_VARS', {})
            hook = str(macro.get('variable_user_post_preload_extension', ''))
            warning = _shared_preload_hook_message(hook, self._name)
            if warning:
                warnings = getattr(self, '_diagnostic_warnings', None)
                if warnings is None:
                    warnings = []
                    self._diagnostic_warnings = warnings
                if warning not in warnings:
                    warnings.append(warning)
                    logger.warning(warning)
                    if self._gcode is not None:
                        self._gcode.respond_info(color_console_tags(
                            "[WARN] NFC[%s]: shared preload hook is not wired; "
                            "set variable_user_post_preload_extension: "
                            "'_NFC_SHARED_PRELOAD'" % self._name))


    def _handle_connect(self):
        global _shared_instance
        self._gcode = self.printer.lookup_object('gcode')
        self.mmu = self.printer.lookup_object('mmu', None)
        if self._enabled and getattr(self, '_mmu_unit', None) is not None:
            self._bind_native_mmu_reader()
        elif not self._shared and self._enabled:
            self._bind_mmu_reader()
        elif self._shared and self.mmu is not None:
            machine = self.mmu.mmu_machine
            self._mmu_unit = (next((u for u in machine.units
                                    if u.nfc_reader or u.nfc_readers), None)
                              or machine.unit_with_bypass
                              or (machine.units[0] if machine.units else None))
        self._apply_mmu_nfc_parameters()
        self._apply_macro_presentation_vars()
        self._validate_startup_config()
        if self._shared:
            self._shared_pending_timeout = self._read_mmu_pending_timeout()
        if self._shared:
            for section_name, _ in self.printer.lookup_objects('mmu_leds'):
                self._mmu_led_unit = section_name.split()[-1]
                break
        if self._shared:
            _shared_instance = self

        # All [nfc_gate ...] sections have finished loading by the time any
        # instance's klippy:connect handler runs, so _lane_instances is
        # stable here -- this is only actually read by
        # MmuNfcSharedPreload.clear_assigned() on the
        # shared instance, but it's cheap enough to just compute for every
        # instance rather than special-case it.
        self._has_per_lane_readers = any(
            not getattr(g, '_shared', False) and getattr(g, '_enabled', True)
            for g in _lane_instances)

        logger.info("[%s]: connected", self._name)
        self._gcode.respond_info(f"[CONNECTED] NFC Gate [{self._name}] connected")

        # Schedule NFC Reader init after the rest of Klippy/I2C has settled.
        self.reactor.register_timer(
            self._delayed_init,
            self.reactor.monotonic() + 2.0
        )

    def _bind_native_mmu_reader(self):
        """Bind a manager created by MmuUnit to that unit's reader object."""
        unit = self._mmu_unit
        if self._shared:
            reader_name = unit.nfc_reader
        else:
            reader_name = unit.nfc_readers[unit.local_gate(self._gate)]
        reader_object = self.printer.lookup_object(reader_name, None)
        if reader_object is None:
            raise self.printer.config_error(
                "NFC manager [%s] cannot find physical reader [%s]"
                % (self._name, reader_name))
        self._reader_object = reader_object
        self._reader = reader_object.reader
        self._reader_type = reader_object.reader_type
        self._low_level_debug = bool(getattr(
            self._reader, '_low_level_debug', self._low_level_debug))
        logger.info("[%s]: using [%s] (%s) from MMU unit [%s]",
                    self._name, reader_name, self._reader_type, unit.name)

    def _bind_mmu_reader(self):
        """Bind this lane to the reader selected in its MmuUnit config."""
        reader_object, unit, reader_name = mmu_reader.resolve_gate_reader(
            self.printer, self.mmu, self._gate, self._name, _lane_instances)

        self._reader_object = reader_object
        self._mmu_unit = unit
        self._reader = reader_object.reader
        self._reader_type = reader_object.reader_type
        # Low-level command availability follows the physical reader object,
        # not obsolete bus options on [nfc_gate laneN].
        self._low_level_debug = bool(getattr(
            self._reader, '_low_level_debug', self._low_level_debug))
        logger.info(
            "[%s]: gate %d using [%s] (%s) from MMU unit [%s]",
            self._name, self._gate, reader_name, self._reader_type, unit.name)

    def _apply_mmu_nfc_parameters(self):
        """Apply NFC runtime settings from the owning MmuUnit parameters."""
        unit = getattr(self, '_mmu_unit', None)
        p = getattr(unit, 'p', None)
        if p is None:
            return

        self._poll_interval = p.poll_interval
        self._startup_polling = p.startup_polling
        self._startup_poll_delay = p.startup_poll_delay
        self._absent_threshold = p.absent_threshold
        self._debug = p.debug
        self._console_output = p.console_output
        self._console_log_level = p.console_log_level
        self._low_level_debug = p.low_level_debug
        self._scan_enabled = False if self._shared else p.scan_enabled
        self._scan_jog_mm = p.scan_jog_mm
        try:
            self._scan_jog_max = (float(p.scan_jog_max)
                                  if str(p.scan_jog_max).strip() else None)
        except (TypeError, ValueError):
            raise self.printer.config_error(
                "Invalid scan_jog_max=%r in [mmu_unit_parameters %s]"
                % (p.scan_jog_max, unit.name))
        self._scan_rewind_buffer_mm = p.scan_rewind_buffer_mm
        self._scan_decode_retry_mm = p.scan_decode_retry_mm
        self._scan_decode_retry_rounds = p.scan_decode_retry_rounds
        self._scan_reads_per_position = p.scan_reads_per_position
        self._scan_poll_interval = p.scan_poll_interval
        self._scan_motion_mode = p.scan_motion_mode
        self._scan_continuous_step_mm = p.scan_continuous_step_mm
        self._scan_continuous_speed = p.scan_continuous_speed
        self._scan_continuous_accel = p.scan_continuous_accel
        self._scan_continuous_poll_interval = p.scan_continuous_poll_interval
        self._tag_parsing = p.tag_parsing
        self._tag_max_pages = p.tag_max_pages
        self._bambu_reads = p.bambu_reads
        self._spoolman_auto_create = p.spoolman_auto_create
        self._state.absent_threshold = p.absent_threshold

        # These delays are runtime policy parameters now, while the physical
        # reader object is still constructed from mmu_hardware.cfg.
        if self._reader is not None:
            if hasattr(self._reader, '_scan_delay'):
                self._reader._scan_delay = p.transceive_delay
            if hasattr(self._reader, '_release_delay'):
                self._reader._release_delay = p.crc_delay
            if hasattr(self._reader, '_transceive_delay'):
                self._reader._transceive_delay = p.transceive_delay

        if self._shared:
            self._shared_bypass_enabled = p.shared_bypass_enabled
            self._shared_led_segment = p.shared_led_segment
            self._shared_missed_limit = p.shared_missed_limit
            self._shared_force_spool_id = p.force_spool_id

        signature = (p.spoolman_url, p.spoolman_rfid_key,
                     p.spoolman_timeout, p.spoolman_cache_ttl, p.debug)
        if getattr(unit, '_nfc_spoolman_signature', None) != signature:
            unit._nfc_spoolman_signature = signature
            unit._nfc_spoolman = (
                SpoolmanClient(
                    p.spoolman_url,
                    rfid_key=p.spoolman_rfid_key,
                    timeout=p.spoolman_timeout,
                    cache_ttl=p.spoolman_cache_ttl,
                    debug=p.debug,
                    moonraker_url='http://127.0.0.1:7125')
                if _spoolman_url_enabled(p.spoolman_url) else None)
        self._spoolman = unit._nfc_spoolman

        configure(p.log_file, printer=self.printer,
                  console_output=p.console_output,
                  console_log_level=p.console_log_level)

    def _apply_macro_presentation_vars(self):
        """Apply NFC presentation settings from _MMU_NFC_VARS."""
        macro = self.printer.lookup_object('gcode_macro _MMU_NFC_VARS', None)
        variables = getattr(macro, 'variables', None)
        if not isinstance(variables, dict):
            return

        for variable, attribute in {
                'scan_searching_effect': '_scan_searching_effect',
                'scan_tag_read_effect': '_scan_tag_read_effect',
                'scan_rewind_effect': '_scan_rewind_effect',
                'lane_auto_create_effect': '_lane_auto_create_effect',
                'lane_unresolved_effect': '_lane_unresolved_effect',
                'shared_tag_read_effect': '_shared_tag_read_effect',
                'shared_spool_ready_effect': '_shared_spool_ready_effect',
                'shared_tag_unresolved_effect': '_shared_tag_unresolved_effect',
                'shared_spool_warning_effect': '_shared_spool_warning_effect',
                'shared_auto_create_effect': '_shared_auto_create_effect',
                'shared_bypass_tag_read_effect': '_shared_bypass_tag_read_effect',
                'shared_bypass_spool_ready_effect': '_shared_bypass_spool_ready_effect',
        }.items():
            if variable in variables:
                setattr(self, attribute, str(variables[variable]))

        for variable, attribute in {
                'shared_read_effect_duration': '_shared_read_effect_duration',
                'shared_unresolved_effect_duration': '_shared_unresolved_effect_duration',
                'shared_bypass_read_effect_duration': '_shared_bypass_read_effect_duration',
                'shared_bypass_ready_effect_duration': '_shared_bypass_ready_effect_duration',
        }.items():
            if variable in variables:
                try:
                    setattr(self, attribute, float(variables[variable]))
                except (TypeError, ValueError):
                    raise self.printer.config_error(
                        "Invalid _MMU_NFC_VARS.%s=%r"
                        % (variable, variables[variable]))

        if self._shared and not getattr(self, '_shared_bypass_enabled', True):
            self._shared_bypass_tag_read_effect = ''
            self._shared_bypass_spool_ready_effect = ''

    def _startup_run_check_gate(self, gate_number, reason):
        script = "MMU_CHECK_GATE GATE=%d" % gate_number
        try:
            logger.info("[%s]: startup check-gate — %s; running %s",
                        self._name, reason, script)
            self._gcode.run_script(script)
            return True
        except Exception as e:
            logger.warning("[%s]: startup check-gate failed (%s): %s",
                           self._name, script, e)
            return False

    def _startup_check_unknown_gate(self, eventtime):
        """Ask Happy Hare to classify this gate if it still reports unknown."""
        if self._gcode is None:
            return
        if self._is_printing():
            logger.info(
                "[%s]: gate %d — startup check-gate skipped while printing",
                self._name, self._gate)
            return

        hh = self._read_hh_status(eventtime)
        if not hh.present or self._gate >= hh.gate_count:
            return
        if hh.status != -1:
            return
        if not hh.idle:
            logger.info(
                "[%s]: gate %d — startup check-gate skipped because "
                "Happy Hare is busy (action=%s)",
                self._name, self._gate, hh.action_label())
            return
        if hh.filament_pos != FILAMENT_POS_UNLOADED:
            logger.info(
                "[%s]: gate %d — startup check-gate skipped because "
                "filament is not parked (filament_pos=%d)",
                self._name, self._gate, hh.filament_pos)
            return

        if self._startup_run_check_gate(
                self._gate,
                "Happy Hare reports gate %d status=-1" % self._gate):
            refreshed = self._read_hh_status(self.reactor.monotonic())
            logger.info(
                "[%s]: gate %d — startup check-gate complete; "
                "Happy Hare status=%s spool=%s",
                self._name, self._gate, refreshed.status, refreshed.spool)

    def _startup_check_unknown_gate_event(self, eventtime):
        was_polling = self._polling
        if was_polling:
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
        try:
            self._startup_check_unknown_gate(eventtime)
            seed_time = self.reactor.monotonic()
            self._seed_cache_from_hh(seed_time)
        finally:
            if was_polling and not self._failed:
                self.reactor.update_timer(
                    self._poll_timer,
                    self.reactor.monotonic() + self._poll_interval)
        return self.reactor.NEVER

    def _delayed_init(self, eventtime):
        """Initialise the NFC Reader after other I2C devices have settled.

        Runs in the reactor thread 2 seconds after klippy:connect fires.
        Returns reactor.NEVER so the timer does not repeat.
        """
        if self._debug >= 4:
            logger.debug(
                "[%s]: delayed init — %s",
                self._name, _reader_label(self._reader_type))

        try:
            # mmu_nfc_reader owns automatic hardware initialization. Shared
            # readers still use the legacy nfc_gate-owned hardware path.
            if self._reader_object is None:
                self._reader.init()
            reader_label = _reader_label(self._reader_type)
            alive = (bool(self._reader_object.alive)
                     if self._reader_object is not None
                     else bool(self._reader.is_alive()))
            if alive:
                self._failed = False
                logger.info("[%s]: %s OK", self._name, reader_label)
            else:
                self._failed = True
                logger.error(
                    "[%s]: %s did not respond — %s",
                    self._name, reader_label,
                    _reader_wiring_hint(self._reader_type))
        except Exception as e:
            self._failed = True
            logger.error("[%s]: init error: %s", self._name, e)

        # Seed lane cache from Happy Hare's current gate map so the first poll
        # after restart does not re-dispatch a spool Happy Hare already knows about.
        # Shared reader has no Happy Hare gate assignment to seed.
        if not self._failed and not self._shared:
            seed_time = self.reactor.monotonic()
            self._seed_cache_from_hh(seed_time)
            if self._spoolman is None:
                delay = (STARTUP_UNKNOWN_GATE_CHECK_DELAY
                         + (self._gate
                            * STARTUP_UNKNOWN_GATE_CHECK_STAGGER))
                self.reactor.update_timer(
                    self._startup_check_timer,
                    self.reactor.monotonic() + delay)
                if self._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — Spoolman disabled; startup "
                        "unknown-gate check scheduled in %.1fs",
                        self._name, self._gate, delay)

        if self._gcode is not None:
            if self._failed:
                init_cmd = ("NFC_SHARED INIT=1" if self._shared
                            else "NFC GATE=%d INIT=1" % self._gate)
                logger.warning(
                    "[%s]: not ready — %s. Run %s after fixing.",
                    self._name, _reader_wiring_hint(self._reader_type),
                    init_cmd)
                self._gcode.respond_info(scan_jog._color_tags(
                    "[WARN] NFC[%s]: not ready — %s. "
                    "Run %s after fixing."
                    % (self._name, _reader_wiring_hint(self._reader_type),
                       init_cmd)))
            else:
                if self._shared:
                    seed_note = ""
                    read_cmd = "NFC_SHARED READ=1"
                else:
                    seed_note = ("  Happy Hare seed: spool_id=%d" % self._hh_seed_spool_id
                                 if self._hh_seed_spool_id is not None
                                 else "  Happy Hare reports gate empty")
                    read_cmd = "NFC GATE=%d READ=1" % self._gate
                if self._debug >= 3:
                    logger.info(
                        "[%s]: ready.%s  %s",
                        self._name,
                        seed_note,
                        ("Startup polling is enabled; first poll in %.1fs."
                         % self._startup_poll_delay)
                        if self._startup_polling == 1
                        else ("Run %s to start polling." % read_cmd))
                self._gcode.respond_info(scan_jog._color_tags(
                    "[OK] NFC[%s]: ready.%s  %s"
                    % (self._name,
                       seed_note,
                       "Startup polling is enabled; first poll in %.1fs."
                       % self._startup_poll_delay
                       if self._startup_polling == 1
                       else "Run %s to start polling." % read_cmd)))

        if not self._failed and self._startup_polling == 1:
            self._polling = True
            first_poll = self.reactor.monotonic() + self._startup_poll_delay
            self.reactor.update_timer(self._poll_timer, first_poll)
            logger.info("[%s]: startup polling enabled; first poll in %.1fs",
                        self._name, self._startup_poll_delay)

        return self.reactor.NEVER

    def _handle_disconnect(self):
        if self._debug >= 4:
            logger.debug("[%s]: disconnect — stopping polling timer",
                         self._name)
        self._polling = False
        self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
        self.reactor.update_timer(self._startup_check_timer,
                                  self.reactor.NEVER)
        if self._shared and self._shared_pending_spool is None:
            self._shared_restore_hh_leds()
        if self._scan_timer is not None:
            self.reactor.update_timer(self._scan_timer, self.reactor.NEVER)
        if self._scan_mode:
            scan_jog.disconnect_cleanup(self)
        if NFCGate._active_scan_gate == self._gate:
            NFCGate._active_scan_gate = None

    def _reactor_sleep(self, duration):
        self.reactor.pause(self.reactor.monotonic() + duration)

    def _handle_print_start(self, print_time):
        if not self._polling:
            return
        if not self._is_printing():
            return
        logger.info(
            "[%s]: printing started — shared polling suspended",
            self._name)
        self._shared_polling_suspended_for_print = True
        self._polling = False
        self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
        if self._shared_pending_spool is None:
            self._shared_restore_hh_leds()

    def _handle_print_end(self, print_time):
        if not self._shared_polling_suspended_for_print:
            return
        if self._polling or self._failed:
            self._shared_polling_suspended_for_print = False
            return
        self._shared_polling_suspended_for_print = False
        if self._startup_polling == 1:
            self._shared_expire_pending_if_needed()
            # Don't restart polling while a valid spool is already staged —
            # the design keeps polling stopped between a successful tag read
            # and PRELOAD_CHECK so the pending spool is not accidentally
            # overwritten by the next tag that drifts into range.
            if self._shared_pending_spool is not None:
                now = self.reactor.monotonic()
                if (self._shared_pending_deadline <= 0.0
                        or now < self._shared_pending_deadline):
                    logger.info(
                        "[%s]: printing complete — spool %d still "
                        "pending; polling stays stopped until PRELOAD_CHECK",
                        self._name, self._shared_pending_spool)
                    return
            logger.info(
                "[%s]: printing complete — shared polling resumed",
                self._name)
            self._shared_read_deadline = 0.0
            self._polling = True
            self.reactor.update_timer(self._poll_timer, self.reactor.NOW)

    def _warning_timer_event(self, eventtime):
        if not self._shared or self._shared_pending_spool is None:
            return self.reactor.NEVER
        if self._shared_pending_warning_fired:
            if eventtime >= self._shared_pending_deadline:
                self._shared_expire_pending_and_maybe_resume()
                return self.reactor.NEVER
            return self._shared_pending_deadline
        if eventtime >= self._shared_pending_deadline:
            self._shared_expire_pending_and_maybe_resume()
            return self.reactor.NEVER
        self._shared_pending_warning_fired = True
        remaining = max(1.0, self._shared_pending_deadline - eventtime)
        if self._shared_spool_warning_effect:
            self._shared_play_led_effect(
                self._shared_spool_warning_effect, event='warning')
            # Re-arm this timer for the actual deadline so timeout cleanup does
            # not depend on the polling timer firing while polling is paused.
            self.reactor.update_timer(
                self._warning_timer, self._shared_pending_deadline)
        msg = ("[WARN] NFC[%s]: spool %d staged — load into gate soon "
               "or tap tag again (%.0fs remaining)"
               % (self._name, self._shared_pending_spool, remaining))
        logger.warning(msg, extra={'nfc_no_console': True})
        if self._gcode is not None:
            self._gcode.respond_info(color_console_tags(msg))
        return self._shared_pending_deadline

    def _poll_timer_event(self, eventtime):
        if not self._polling:
            if self._shared:
                if (self._shared_pending_spool is not None
                        and not self._shared_pending_warning_fired):
                    warning_time = (self._shared_pending_deadline
                                    - 0.2 * self._shared_pending_timeout)
                    if eventtime >= warning_time:
                        self._warning_timer_event(eventtime)
                self._shared_expire_pending_and_maybe_resume()
                if self._polling:
                    return self.reactor.NOW
                if self._shared_pending_spool is not None:
                    return self._shared_pending_deadline
            return self.reactor.NEVER
        if self._failed:
            init_cmd = "NFC_SHARED INIT=1" if self._shared else "NFC GATE=%d INIT=1" % self._gate
            logger.warning("[%s]: polling stopped — reader failed; "
                           "run %s first",
                           self._name, init_cmd)
            self._polling = False
            if self._shared and self._shared_pending_spool is None:
                self._shared_restore_hh_leds()
            return self.reactor.NEVER

        # Shared read-timeout: stop polling if READ=1 has been active too long
        # without resolving a valid tag.
        if self._shared and self._shared_read_deadline > 0.0:
            if eventtime >= self._shared_read_deadline:
                logger.info(
                    "[%s]: shared read timeout (%.0fs) — stopping poll",
                    self._name, self._shared_read_timeout)
                self._shared_read_deadline = 0.0
                self._polling = False
                if self._shared_pending_spool is None:
                    self._shared_restore_hh_leds()
                return self.reactor.NEVER

        # Poll suppression while Happy Hare already has an opinion about this
        # gate (loaded+matched, or assigned). Reads Happy Hare's gate_status
        # on every tick — Python dict only, no I2C. Scan-jog is no longer
        # triggered from here: Happy Hare's post-preload hook
        # (_NFC_SCAN_JOG_PRELOAD / _NFC_SHARED_PRELOAD) calls
        # NFC GATE=<n> JOG_SCAN=1 SOURCE=AUTO directly instead.
        if self._scan_enabled:
            hh = self._read_hh_status(eventtime)
            if hh.present and self._gate < hh.gate_count:
                curr = hh.status
                if self._debug >= 4:
                    logger.debug(
                        "[%s]: gate %d — Happy Hare poll: "
                        "curr=%s action=%s printing=%s load_paused=%s",
                        self._name, self._gate,
                        curr, hh.action_label(),
                        self._is_printing(),
                        self._hh_load_paused)
                if (curr >= 1 and self._state.current_spool is not None
                        and self._state.current_spool is not DIRECT_METADATA_SPOOL):
                    if not self._hh_load_paused:
                        self._hh_load_paused = True
                        logger.info(
                            "[%s]: gate %d — Happy Hare reports filament "
                            "present; NFC already has spool=%s — "
                            "suspending poll",
                            self._name, self._gate,
                            self._state.current_spool)
                    self._state.miss_count = 0
                    return self.reactor.monotonic() + self._poll_interval
                if curr <= 0:
                    nfc_spool = self._state.current_spool
                    if hh.assigned and nfc_spool == hh.spool:
                        if not self._hh_load_paused:
                            self._hh_load_paused = True
                            logger.info(
                                "[%s]: gate %d — Happy Hare has assigned "
                                "spool=%d; suspending NFC poll",
                                self._name, self._gate, hh.spool)
                        self._state.miss_count = 0
                        return self.reactor.monotonic() + self._poll_interval
                    if self._hh_load_paused:
                        self._hh_load_paused      = False
                        self._state.current_uid   = None
                        self._state.current_spool = None
                        self._state.miss_count    = 0
                        self._hh_confirmed_spool  = None
                        logger.info(
                            "[%s]: gate %d — gate ejected; "
                            "resuming poll and clearing NFC cache",
                            self._name, self._gate)
                        return self.reactor.monotonic() + 1.0
                    return self.reactor.monotonic() + self._poll_interval

        if self._debug >= 4:
            logger.debug("[%s]: poll cycle start — "
                         "current state: uid=%s spool=%s misses=%d",
                         self._name,
                         self._state.current_uid or 'none',
                         self._state.current_spool
                         if self._state.current_spool is not None else 'none',
                         self._state.miss_count)
        try:
            self._poll()
        except Exception:
            logger.exception("[%s]: poll error", self._name)
        next_interval = (self._scan_poll_interval
                         if self._shared else self._poll_interval)
        if self._debug >= 4:
            logger.debug("[%s]: poll cycle done — "
                         "next poll in %.2fs", self._name, next_interval)
        return self.reactor.monotonic() + next_interval

    def _read_current_tag(self):
        return tag_handler.read_current_tag(self)

    def _resolve_spool(self, uid_hex):
        return tag_handler.resolve_spool(self, uid_hex)

    def _check_hh_cleared(self):
        """Reset lane cache if Happy Hare cleared this gate from outside the NFC system.

        Only active after Happy Hare has confirmed the spool at least once (_hh_confirmed_spool
        is set when Happy Hare's gate_spool_id matches what NFC dispatched).  This prevents a
        loop where NFC dispatches spool 49, Happy Hare hasn't processed it yet, the check sees
        Happy Hare=-1, clears the cache, NFC dispatches again next poll, and so on forever.
        """
        if self._state.current_spool is None:
            return  # Lane cache already empty — nothing to cross-check
        if self._hh_confirmed_spool != self._state.current_spool:
            return  # Happy Hare hasn't acknowledged this spool yet — don't second-guess it
        hh = self._read_hh_status()
        if not hh.present:
            return
        nfc_spool = self._state.current_spool
        hh_differs = (not hh.assigned) or (hh.spool != nfc_spool)
        if hh_differs:
            if not hh.assigned:
                reason = "Happy Hare cleared gate externally (NFC cache had spool=%d)" % nfc_spool
            else:
                reason = ("Happy Hare has spool=%d but NFC cache has spool=%d "
                          "(manual gate map change?)" % (hh.spool, nfc_spool))
            logger.info(
                "[%s]: gate %d — %s; resetting lane cache so "
                "next tag read re-dispatches _NFC_SPOOL_CHANGED",
                self._name, self._gate, reason)
            self._state.current_uid   = None
            self._state.current_spool = None
            self._state.miss_count    = 0
            self._hh_confirmed_spool  = None

    def _hh_gate_matches_current_spool(self):
        """Return True when Happy Hare already owns this gate's current spool.

        Happy Hare may report a gate as merely assigned (gate_spool_id > 0,
        gate_status == 0) or available/loaded (gate_status >= 1).  Once NFC has
        read and cached that same spool, either state is enough to stop NFC
        polling until Happy Hare clears the assignment.
        """
        nfc_spool = self._state.current_spool
        if nfc_spool is None:
            return False
        hh = self._read_hh_status()
        return hh.present and hh.spool == nfc_spool

    def _poll(self):
        if self._poll_hh_pause_check():
            return
        self._check_hh_cleared()
        uid_hex  = self._read_current_tag()
        new_shared_uid = uid_hex is not None and uid_hex != self._state.current_uid
        if (self._shared and new_shared_uid
                and self._shared_missed_resolutions == 0):
            if self._shared_tag_read_effect or self._shared_bypass_tag_read_effect:
                if self._shared_bypass_selected() and self._shared_bypass_tag_read_effect:
                    self._shared_play_tag_read_effect(
                        effect_name=self._shared_bypass_tag_read_effect)
                else:
                    self._shared_play_tag_read_effect()
            if self._debug >= 2:
                logger.info(
                    "[%s]: tag read uid=%s — resolving...",
                    self._name, uid_hex)
        spool_id = self._resolve_spool(uid_hex)
        event    = self._state.process_read(uid_hex, spool_id,
                                            scan_mode=self._scan_mode)
        self._poll_debug_trace(uid_hex, event)
        if event is not None:
            self._poll_dispatch_event(event)
        elif (self._shared and uid_hex is not None
                and self._state.current_spool is None):
            if self._shared_missed_resolutions < self._shared_missed_limit:
                self._shared_missed_resolutions += 1
                if (self._shared_tag_unresolved_effect
                        and self._shared_missed_resolutions == 1):
                    self._shared_play_tag_unresolved_effect()
                if self._shared_missed_resolutions == 1 and self._debug >= 2:
                    logger.warning(
                        "[%s]: uid=%s not in Spoolman",
                        self._name, uid_hex)
                if self._shared_missed_resolutions == self._shared_missed_limit:
                    self._shared_unresolved_limit_reached(uid_hex)
        return uid_hex is not None


    def _poll_hh_pause_check(self):
        """Suspend polling while Happy Hare says filament is still present."""
        if not self._scan_mode:
            hh = self._read_hh_status()
            if hh.present and hh.available:
                if not self._hh_load_paused:
                    self._hh_load_paused = True
                    logger.info(
                        "[%s]: gate %d — Happy Hare reports filament "
                        "present (status=%s spool=%s); suspending NFC poll "
                        "until ejected",
                        self._name, self._gate, hh.status, hh.spool)
                self._state.miss_count = 0
                return True
        if (not self._scan_mode
                and self._hh_gate_matches_current_spool()
                and self._state.current_spool is not None):
            if not self._hh_load_paused:
                self._hh_load_paused = True
                logger.info(
                    "[%s]: gate %d — spool confirmed by NFC; "
                    "Happy Hare owns same spool — suspending poll until ejected",
                    self._name, self._gate)
            self._state.miss_count = 0
            return True
        if self._hh_load_paused:
            if self._state.current_spool is None:
                self._hh_load_paused = False
                return False
            hh = self._read_hh_status()
            if hh.present and hh.available:
                self._state.miss_count = 0
                if self._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — Happy Hare still reports filament "
                        "present (status=%s spool=%s); keeping NFC spool=%s",
                        self._name, self._gate, hh.status, hh.spool,
                        self._state.current_spool)
                return True
            self._hh_load_paused      = False
            self._state.current_uid   = None
            self._state.current_spool = None
            self._state.miss_count    = 0
            self._hh_confirmed_spool  = None
            logger.info(
                "[%s]: gate %d — filament unloaded; resuming NFC scan",
                self._name, self._gate)
        return False

    def _poll_debug_trace(self, uid_hex, event):
        if self._debug < 4:
            return
        if uid_hex is not None:
            read_str = "tag=%-16s" % uid_hex
        else:
            read_str = "no tag  miss=%d/%d" % (
                self._state.miss_count, self._state.absent_threshold)
        if event is None:
            if uid_hex is not None:
                action_str = "quiet  (spool=%s, uid unchanged)" % (
                    self._state.current_spool,)
            else:
                action_str = "quiet  (waiting, %d more miss(es) until removal)" % (
                    max(0, self._state.absent_threshold - self._state.miss_count),)
        else:
            etype = event[0]
            if etype == EVENT_CHANGED:
                action_str = "CHANGED  →  spool=%s  uid=%s" % (event[3], event[2])
            elif etype == EVENT_REMOVED:
                action_str = "REMOVED  (tag absent for %d consecutive polls)" % (
                    self._state.absent_threshold,)
            elif etype == EVENT_UID_ONLY:
                if self._spoolman is None:
                    action_str = "NO_SPOOL  (uid=%s no metadata/spool assignment)" % (
                        event[2],)
                else:
                    action_str = "NO_SPOOL  (uid=%s not registered in Spoolman)" % (
                        event[2],)
            else:
                action_str = str(etype)
        logger.debug("[%s]: POLL  gate=%-2d  %-28s  →  %s",
                     self._name, self._gate, read_str, action_str)

    def _poll_dispatch_event(self, event):
        event_type, gate, uid, spool = event
        if self._debug >= 3:
            logger.info("[%s]: gate %d — %s uid=%s spool=%s",
                        self._name, gate, event_type, uid, spool)

        if self._shared:
            self._shared_handle_event(event_type, uid, spool)
            return

        suppress = (self._hh_seed_spool_id is not None
                    and event_type == EVENT_CHANGED
                    and spool == self._hh_seed_spool_id
                    and self._hh_seed_available)
        self._hh_seed_spool_id  = None  # one-shot, always clear
        self._hh_seed_available = False

        if self._is_printing():
            if self._debug >= 3:
                logger.info(
                    "[%s]: gate %d — %s detected during print; "
                    "Spoolman and Happy Hare dispatch suppressed",
                    self._name, gate, event_type)
        elif self._scan_mode:
            meta = None
            if (event_type == EVENT_CHANGED
                    and self._state.current_spool is DIRECT_METADATA_SPOOL
                    and self._state.current_tag is not None):
                meta = self._state.current_tag.meta
            self._scan_found_event = (event_type, gate, uid, spool, meta)
            if self._debug >= 3:
                logger.info(
                    "[%s]: gate %d — %s detected during scan-jog; "
                    "dispatch deferred until rewind complete",
                    self._name, gate, event_type)
        else:
            if suppress:
                if self._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — startup seed match "
                        "spool=%s; skipping Happy Hare dispatch",
                        self._name, gate, spool)
            else:
                self._poll_klipper_dispatch(event_type, gate, uid, spool)

    def _poll_klipper_dispatch(self, event_type, gate, uid, spool,
                               scan_finish=False):
        meta = None
        auto_created = False
        if event_type == EVENT_CHANGED and self._state.current_tag is not None:
            res = self._state.current_tag.resolution or {}
            auto_created = isinstance(res, dict) and res.get('path') == 'auto_create'
            if self._state.current_spool is DIRECT_METADATA_SPOOL:
                meta = self._state.current_tag.meta
        self._klipper.dispatch(event_type, gate, uid, spool,
                               meta=meta, auto_created=auto_created,
                               scan_finish=scan_finish)
        if event_type == EVENT_CHANGED and spool is not None:
            self._hh_confirmed_spool = spool
        elif event_type == EVENT_REMOVED:
            self._hh_confirmed_spool = None

    # ── Scan-and-jog mode ────────────────────────────────────────────────────

    def _manual_jog_scan(self, gcmd):
        return scan_jog.manual_jog_scan(self, gcmd)

    def _all_lanes_parked_or_empty(self, eventtime=None):
        status = _full_snapshot(
            self._get_mmu(),
            eventtime if eventtime is not None else self.reactor.monotonic())
        if not status.present:
            return False, "Happy Hare status unavailable"

        if status.filament_pos != FILAMENT_POS_UNLOADED:
            if status.active_gate >= 0 and status.action:
                return False, "lane %d is %s; filament is not parked (filament_pos=%d)" % (
                    status.active_gate, status.action_label(),
                    status.filament_pos)
            return False, "filament is not parked (filament_pos=%d)" % (
                status.filament_pos,)

        if not status.gate_statuses:
            return False, "Happy Hare gate status unavailable"

        for lane, gate_state in enumerate(status.gate_statuses):
            safe = gate_state in (GATE_EMPTY,
                                  GATE_AVAILABLE,
                                  GATE_AVAILABLE_FROM_BUFFER)
            if self._debug >= 3:
                logger.info(
                    "[%s]: scan preflight — lane %d gate_status=%d %s",
                    self._name, lane, gate_state,
                    "safe" if safe else "not safe")
            if not safe:
                return False, "lane %d is not parked or empty (status=%d)" % (
                    lane, gate_state)

        return True, None

    def _expand_mmu_vars_path(self, path):
        path = os.path.expanduser(str(path).strip())
        if os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(
            os.path.expanduser('~/printer_data/config'), path))


    def _resolve_mmu_vars_path(self):
        cached = getattr(self, '_mmu_vars_path', None)
        if cached:
            return cached

        configfile = self.printer.lookup_object('configfile', None)
        if configfile is not None and hasattr(configfile, 'get_status'):
            try:
                raw_config = configfile.get_status(0).get('config', {})
                save_vars = raw_config.get('save_variables', {})
                filename = save_vars.get('filename', None)
                if filename:
                    self._mmu_vars_path = self._expand_mmu_vars_path(filename)
                    return self._mmu_vars_path
            except Exception:
                logger.exception(
                    "[%s]: could not read [save_variables] filename",
                    self._name)

        fallback = '~/printer_data/config/mmu/mmu_vars.cfg'
        self._mmu_vars_path = self._expand_mmu_vars_path(fallback)
        return self._mmu_vars_path

    def _load_bowden_lengths(self):
        path = self._resolve_mmu_vars_path()
        if not path or not os.path.exists(path):
            return None

        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if not line.startswith('mmu_calibration_bowden_lengths'):
                        continue
                    parts = line.split('=', 1)
                    if len(parts) != 2:
                        return None
                    values = ast.literal_eval(parts[1].strip())
                    if not isinstance(values, (list, tuple)):
                        return None
                    lengths = []
                    for value in values:
                        length = float(value)
                        if length <= 0.0:
                            return None
                        lengths.append(length)
                    self._bowden_lengths = lengths
                    return lengths
        except Exception:
            logger.exception(
                "[%s]: could not read Bowden lengths from %s",
                self._name, path)
            return None

        return None

    def _get_lane_scan_max_mm(self):
        if self._scan_jog_max is not None:
            return float(self._scan_jog_max)
        lengths = self._load_bowden_lengths()
        if lengths is None:
            return None
        if self._gate < 0 or self._gate >= len(lengths):
            return None
        return float(lengths[self._gate])

    def _prepare_scan_jog(self, eventtime=None):
        ok, reason = self._all_lanes_parked_or_empty(eventtime)
        if not ok:
            return False, reason, None
        max_mm = self._get_lane_scan_max_mm()
        if max_mm is None:
            return False, "missing Bowden calibration length for gate %d" % self._gate, None
        return True, None, max_mm

    def _is_printing(self):
        return scan_jog.is_printing(self)

    def _get_scan_speed(self):
        return scan_jog.get_speed(self)

    def _scan_chunk_interval(self, mm):
        return scan_jog.chunk_interval(self, mm)

    def _scan_next_event_time(self, mm):
        return scan_jog.next_event_time(self, mm)

    def _resume_poll_after_rewind(self):
        return scan_jog.resume_poll_after_rewind(self)

    def _scan_step_event(self, eventtime):
        return scan_jog.step_event(self, eventtime)

    def _finish_scan(self):
        return scan_jog.finish(self)

    def _rewind_and_exit_scan(self):
        return scan_jog.rewind_and_exit(self)

    def _console(self, msg):
        return scan_jog.console(self, msg)

    def _run_jog(self, mm):
        return scan_jog.run_jog(self, mm)

    def _run_rewind(self):
        return scan_jog.run_rewind(self)

    def _nfc_gate_for_gate_number(self, gate_number):
        return nfc_gate_for_gate_number(gate_number)

    def status_line(self):
        label = ("shared" if self._shared
                 else "Gate %d" % self._gate)
        label = "%s (%s)" % (label, self._reader_type)
        if not getattr(self, '_enabled', True):
            return "  %s  [%s]:  disabled by config" % (label, self._name)
        if self._failed:
            return ("  %s  [%s]:  READER FAILED (check wiring, address 0x24)"
                    % (label, self._name))
        if self._hh_load_paused:
            poll_state = "polling suspended"
        elif self._polling:
            poll_state = "polling"
        else:
            poll_state = "not polling"
        hh = self._read_hh_status()
        if hh.present and hh.available and not self._scan_mode:
            poll_state = "polling suspended"
        hh_label = hh.label()
        sync_note = ''
        nfc_spool = self._state.current_spool
        hh_empty = (hh.present and not hh.available
                    and not (hh.active_gate == self._gate
                             and hh.filament_pos > 0))
        if (hh.present and hh.assigned and nfc_spool is not None
                and nfc_spool is not DIRECT_METADATA_SPOOL
                and hh.spool != nfc_spool):
            sync_note = "  [SYNC MISMATCH: NFC spool %s, Happy Hare spool %s]" % (
                nfc_spool, hh.spool)
        elif (hh.present and hh.assigned and nfc_spool is None):
            hh_label = hh_label + ", NFC cache empty"
        elif (hh.present and not hh.assigned and nfc_spool is not None
                and nfc_spool is not DIRECT_METADATA_SPOOL):
            if hh.available:
                sync_note = "  [NFC has spool %s; Happy Hare found/no spool]" % nfc_spool
            else:
                sync_note = "  [NFC has spool %s; Happy Hare empty]" % nfc_spool
        if hh_empty:
            return _status_html_words(
                "  %s:  empty   [%s]%s  [%s]"
                % (label, poll_state, sync_note, hh_label))
        if self._state.current_spool is DIRECT_METADATA_SPOOL:
            tag = self._state.current_tag
            meta = tag.meta if tag is not None else {}
            material = (meta or {}).get('material', '')
            color = (meta or {}).get('color_hex', '')
            return _status_html_words(
                "  %s:  tag %s  metadata material=%s color=%s   [%s]%s  [%s]"
                % (label, self._state.current_uid,
                   material, color, poll_state, sync_note, hh_label))
        if self._state.current_spool is not None:
            return _status_html_words(
                "  %s:  spool %-2d  UID %s   [%s]%s   [%s]"
                % (label,
                   self._state.current_spool, self._state.current_uid,
                   poll_state, sync_note, hh_label))
        if self._state.current_uid is not None:
            return _status_html_words(
                "  %s:  tag %s  (UID not in Spoolman)   [%s]%s  [%s]"
                % (label, self._state.current_uid, poll_state,
                   sync_note, hh_label))
        if hh.present and hh.available:
            return _status_html_words(
                "  %s:  occupied   [%s]%s  [%s]"
                % (label, poll_state, sync_note, hh_label))
        return _status_html_words(
            "  %s:  empty   [%s]%s  [%s]"
            % (label, poll_state, sync_note, hh_label))

    # ── Shared reader ────────────────────────────────────────────────────────


    def _safe_run_script(self, script):
        try:
            self._gcode.run_script(script)
            return True
        except Exception as e:
            logger.debug(
                "[%s]: deferred run_script failed: %s",
                self._name, e)
            return False


    def get_status(self, _eventtime=None):
        if not getattr(self, '_enabled', True):
            return {
                'gate':                self._gate,
                'enabled':             False,
                'reader_type':         self._reader_type,
                'tag_present':         False,
                'spool_id':            -1,
                'uid':                 '',
                'failed':              False,
                'resolution':          'disabled',
                'pending_spool_id':    -1,
                'pending_auto_created': False,
                'preload_spool_id':    -1,
                'preload_auto_created': False,
                'has_per_lane_readers': False,
            }
        tag = self._state.current_tag
        is_meta_direct = self._state.current_spool is DIRECT_METADATA_SPOOL
        tag_present = self._state.current_uid is not None
        resolution = ''
        if is_meta_direct:
            resolution = 'metadata_direct'
        elif tag is not None and isinstance(tag.resolution, dict):
            resolution = tag.resolution.get('path', '')
        return {
            'gate':                self._gate,
            'enabled':             True,
            'reader_type':         self._reader_type,
            'tag_present':         tag_present,
            'spool_id':            (-1 if is_meta_direct
                                    else self._state.current_spool
                                    if self._state.current_spool is not None else -1),
            'uid':                 self._state.current_uid or '',
            'failed':              self._failed,
            'resolution':          resolution,
            'pending_spool_id':    -1,
            'pending_auto_created': False,
            'preload_spool_id':    -1,
            'preload_auto_created': False,
            'has_per_lane_readers': False,
        }
