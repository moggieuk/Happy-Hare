# klippy/extras/mmu/mmu_nfc_gate_state.py
#
# EMU NFC Gate Reader — per-gate debounce state machine and tag observation
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later

from dataclasses import dataclass, field

EVENT_CHANGED  = 'changed'   # New or replaced spool
EVENT_UID_ONLY = 'uid_only'  # Tag present but UID not in Spoolman
EVENT_REMOVED  = 'removed'   # Tag gone after absent_threshold misses

# Sentinel returned by _resolve_spool when Spoolman is disabled but the tag
# carries filament metadata.  Stored on GateState.current_spool so downstream
# code can distinguish "no tag" (None) from "tag present, metadata-only path".
DIRECT_METADATA_SPOOL = object()


@dataclass
class CurrentTag:
    uid: str
    spool_id: object = None
    target_info: object = None
    raw_tag_data: object = None
    meta: dict = field(default_factory=dict)
    spool_identity: object = None
    parse_error: object = None
    resolution: object = None
    read_incomplete: bool = False
    read_retry_reason: object = None
    mifare_auth_failed_sectors: object = field(default_factory=list)
    mifare_read_failed_blocks: object = field(default_factory=list)


class GateState:
    def __init__(self, gate, absent_threshold=3):
        self.gate             = gate
        self._current_uid     = None
        self._current_spool   = None
        self.current_tag      = None
        self.miss_count       = 0
        self.absent_threshold = absent_threshold

    @property
    def current_uid(self):
        return self._current_uid

    @current_uid.setter
    def current_uid(self, uid_hex):
        self._current_uid = uid_hex
        self._sync_current_tag()

    @property
    def current_spool(self):
        return self._current_spool

    @current_spool.setter
    def current_spool(self, spool_id):
        self._current_spool = spool_id
        self._sync_current_tag()

    def _sync_current_tag(self):
        if self._current_uid is None:
            self.current_tag = None
            return
        if self.current_tag is None or self.current_tag.uid != self._current_uid:
            self.current_tag = CurrentTag(uid=self._current_uid,
                                          spool_id=self._current_spool)
            return
        self.current_tag.spool_id = self._current_spool

    def reset(self):
        self._current_uid = None
        self._current_spool = None
        self.current_tag = None
        self.miss_count = 0

    def process_read(self, uid_hex, spool_id, scan_mode=False):
        if uid_hex is not None:
            self.miss_count = 0
            if spool_id is DIRECT_METADATA_SPOOL:
                if (self.current_uid == uid_hex
                        and self.current_spool is DIRECT_METADATA_SPOOL):
                    return None
                self.current_uid = uid_hex
                self.current_spool = DIRECT_METADATA_SPOOL
                return (EVENT_CHANGED, self.gate, uid_hex, None)
            if self.current_uid == uid_hex and self.current_spool == spool_id:
                return None
            self.current_uid   = uid_hex
            self.current_spool = spool_id
            if spool_id is not None:
                return (EVENT_CHANGED, self.gate, uid_hex, spool_id)
            return (EVENT_UID_ONLY, self.gate, uid_hex, None)
        else:
            if not scan_mode:
                self.miss_count += 1
                if (self.miss_count >= self.absent_threshold
                        and self.current_uid is not None):
                    old_spool          = self.current_spool
                    self.current_uid   = None
                    self.current_spool = None
                    self.miss_count    = 0
                    return (EVENT_REMOVED, self.gate, None, old_spool)
            return None

    def __repr__(self):
        if self.current_uid is None:
            return "Gate({} empty, misses={})".format(self.gate, self.miss_count)
        return "Gate({} uid={} spool={} misses={})".format(
            self.gate, self.current_uid, self.current_spool, self.miss_count)
