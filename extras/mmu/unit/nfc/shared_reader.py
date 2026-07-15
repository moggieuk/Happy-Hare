# klippy/extras/mmu/mmu_nfc_shared_reader.py
#
# NFC Gate Reader — shared-reader (single in-body reader) manager
# SPDX-License-Identifier: GPL-3.0-or-later
#
# MmuSharedNfcReader is a single physical NFC reader shared across every gate on
# the MMU.
# It answers a different question than a lane does: not "is my filament at my
# reader," but "which gate, if any, is Happy Hare about to load, and what tag
# was just tapped against the shared reader for it." That's a stage (tag
# tapped, resolve via Spoolman, dispatch MMU_GATE_MAP NEXT_SPOOLID) -> validate
# -> commit protocol driven by Happy Hare's own post-preload hook
# (_NFC_SHARED_PRELOAD -> NFC_SHARED PRELOAD_CHECK/PRELOAD_COMMIT/
# PRELOAD_CLEAR_ASSIGNED), not the per-lane scan-jog path in scan_jog.py.
#
# Subclasses NFCGate rather than duplicating its plumbing (reader-driver init,
# poll-timer registration, LED manager wiring, Happy Hare status reads) --
# that generic plumbing is expected to move into Happy Hare itself over time,
# so it isn't worth re-deriving here. _poll_timer_event()/_poll() are
# deliberately left on NFCGate, still branching on self._shared internally,
# rather than split out onto this class -- that split is a separate, larger
# piece of design work, not part of this extraction.
#
# All of these methods only ever run when self._shared is True, i.e. only on
# a MmuSharedNfcReader instance -- they were previously defined directly on
# NFCGate, gated by "if self._shared:" checks at each call site.

from .mmu_nfc_log import logger

try:
    from .mmu_nfc_log import color_console_tags
except ImportError:
    def color_console_tags(text):
        text = str(text)
        text = text.replace('[WARN]', '<span style="color:#FFFF00">[WARN]</span>')
        text = text.replace('[OK]', '<span style="color:#90EE90">[OK]</span>')
        text = text.replace('[ERROR]', '<span style="color:#FF6060">[ERROR]</span>')
        return text

from .unit.nfc import rc522_driver
from . import mmu_nfc_shared_preload as shared_preload
EVENT_AUTO_CREATE = 'auto_create'
EVENT_SPOOL_READY = 'spool_ready'
EVENT_TAG_READ = 'tag_read'
EVENT_UNRESOLVED = 'unresolved'
from .mmu_nfc_gate_state import (
    EVENT_CHANGED, EVENT_REMOVED, EVENT_UID_ONLY, DIRECT_METADATA_SPOOL)
from .mmu_constants import TOOL_GATE_BYPASS
from .mmu_nfc_manager import (
    NFCGate, _raw_klipper_config, _shared_preload_hook_message)


class MmuSharedNfcReader(NFCGate):
    """Single shared NFC reader used across every gate on the MMU."""

    def _shared_led_target(self):
        segment = getattr(self, '_shared_led_segment', 'exit')
        segment = (segment or 'exit').strip().lower()
        mcu_index = getattr(self, '_shared_mcu_index', None)
        if segment == 'gate':
            unit = getattr(self, '_mmu_unit', None)
            selected_gate = (getattr(self.mmu, 'gate_selected', None)
                             if self.mmu is not None else None)
            gate = (selected_gate
                    if unit is not None and unit.manages_gate(selected_gate)
                    else mcu_index)
            if (gate is None or unit is None
                    or not unit.manages_gate(int(gate))):
                if unit is not None:
                    logger.warning(
                        "[%s]: shared_led_segment=gate resolved GATE=%s but "
                        "unit %s manages gates %s; using unit exit LEDs instead",
                        self._name, gate, unit.name, unit.gate_range())
                else:
                    logger.warning(
                        "[%s]: shared_led_segment=gate has no valid gate; "
                        "using UNIT LEDs instead",
                        self._name)
                segment = 'exit'
                mcu_index = None
            else:
                segment = 'exit'
                mcu_index = int(gate)
        return getattr(self, '_mmu_unit', None), segment, mcu_index

    def _shared_gate_effect_name(self, base):
        """Return the Happy Hare LED effect name for a shared reader event.

        shared_led_segment selects the target style:
          gate -> legacy per-gate effect {base}_exit_{index}
          else -> whole-chain effect {unit}_{base}_{segment}
        """
        unit, segment, gate = self._shared_led_target()
        if unit is None:
            return base
        if gate is not None:
            return "%s_%s_%d" % (base, segment, gate)
        return "unit%d_%s_%s" % (unit.unit_index, base, segment)

    def _shared_play_led_effect(self, effect_name, gcmd=None, event='',
                                duration=None):
        if not effect_name:
            if gcmd is not None:
                logger.warning(
                    "[%s]: no LED effect configured", self._name)
                gcmd.respond_info(
                    "[WARN] NFC[%s]: no LED effect configured" % self._name)
            return False
        unit, segment, gate = self._shared_led_target()
        gate_effect = self._shared_gate_effect_name(effect_name)
        logger.info(
            "[%s]: LED effect %s scheduled",
            self._name, gate_effect)
        if gcmd is not None:
            gcmd.respond_info(
                "NFC[%s]: LED effect %s started" % (self._name, gate_effect))
        if self.mmu is None or unit is None:
            return False
        return self.mmu.led_manager.set_transient_effect(
            unit, effect_name, segment=segment, gate=gate,
            duration=duration)

    def _shared_arm_led_failsafe(self, timeout, reason):
        if not self._shared:
            return
        try:
            timeout = float(timeout)
        except Exception:
            timeout = 0.0
        if timeout <= 0.0:
            return
        self._shared_led_failsafe_reason = reason
        self._shared_led_failsafe_deadline = self.reactor.monotonic() + timeout
        self.reactor.update_timer(
            self._shared_led_failsafe_timer,
            self._shared_led_failsafe_deadline)
        if self._debug >= 4:
            logger.debug(
                "[%s]: shared LED failsafe armed for %.2fs (%s)",
                self._name, timeout, reason)

    def _shared_cancel_led_failsafe(self):
        if not getattr(self, '_shared', False):
            return
        self._shared_led_failsafe_deadline = 0.0
        self._shared_led_failsafe_reason = None
        self.reactor.update_timer(
            self._shared_led_failsafe_timer, self.reactor.NEVER)

    def _shared_led_failsafe_event(self, eventtime):
        if (not self._shared
                or self._shared_led_failsafe_deadline <= 0.0):
            return self.reactor.NEVER
        if eventtime < self._shared_led_failsafe_deadline:
            return self._shared_led_failsafe_deadline
        reason = self._shared_led_failsafe_reason or "unknown"
        self._shared_led_failsafe_deadline = 0.0
        self._shared_led_failsafe_reason = None
        if self._shared_pending_spool is None:
            logger.info(
                "[%s]: shared LED failsafe released Happy Hare ownership (%s)",
                self._name, reason)
            self._shared_restore_hh_leds()
        return self.reactor.NEVER

    def _shared_play_tag_read_effect(self, gcmd=None, effect_name=None,
                                     duration=None):
        effect_name = effect_name or self._shared_tag_read_effect
        started = self._shared_play_led_effect(
            effect_name, gcmd, event=EVENT_TAG_READ, duration=duration)
        if started and duration is None:
            self._shared_arm_led_failsafe(
                self._shared_read_effect_duration, EVENT_TAG_READ)
        return started

    def _shared_play_spool_ready_effect(self):
        # Normal staged-ready feedback stays active until the shared-reader
        # lifecycle releases Happy Hare ownership on preload commit/cancel/timeout.
        self._shared_cancel_led_failsafe()
        self._shared_play_led_effect(
            self._shared_spool_ready_effect, event=EVENT_SPOOL_READY)

    def _shared_clear_pending_warning_feedback(self):
        self.reactor.update_timer(self._warning_timer, self.reactor.NEVER)
        if self._shared_pending_warning_fired:
            self._shared_restore_hh_leds()

    def _shared_play_tag_unresolved_effect(self):
        self._shared_cancel_led_failsafe()
        self._shared_play_led_effect(
            self._shared_tag_unresolved_effect, event=EVENT_UNRESOLVED,
            duration=self._shared_unresolved_effect_duration)

    def _shared_play_auto_create_effect(self):
        self._shared_cancel_led_failsafe()
        self._shared_play_led_effect(
            self._shared_auto_create_effect, event=EVENT_AUTO_CREATE)

    def _shared_restore_hh_leds(self):
        self._shared_cancel_led_failsafe()
        unit, segment, gate = self._shared_led_target()
        if self.mmu is not None and unit is not None:
            self.mmu.led_manager.restore_transient_effect(
                unit, segment=segment, gate=gate)

    def _shared_clear_cache(self, gcmd):
        """Clear shared reader tag/cache state while keeping staged spool."""
        pending_spool = self._shared_pending_spool
        pending_uid   = self._shared_pending_uid
        self._state.reset()
        if self._spoolman is not None:
            self._spoolman.clear_cache()
        if hasattr(self._reader, '_clear_current_card'):
            self._reader._clear_current_card()
        logger.info(
            "[%s]: shared tag cache cleared; "
            "pending spool=%s uid=%s kept",
            self._name, pending_spool, pending_uid)
        gcmd.respond_info(color_console_tags(
            "NFC[%s]: shared tag cache cleared; pending spool kept"
            % self._name))

    def _shared_preload_hook_ready(self):
        raw_config = _raw_klipper_config(self.printer)
        macro = raw_config.get('gcode_macro _MMU_SEQUENCE_VARS', {})
        hook = str(macro.get('variable_user_post_preload_extension', ''))
        return _shared_preload_hook_message(hook, self._name) is None

    def _shared_preload_hook_hint(self):
        if self._shared_preload_hook_ready():
            return ""
        return (" Check mmu_macro_vars.cfg: "
                "variable_user_post_preload_extension must be "
                "'_NFC_SHARED_PRELOAD', not 'NFC JOG_SCAN=1'.")

    def _shared_bypass_selected(self):
        """Return True when Happy Hare currently has bypass selected."""
        if not getattr(self, '_shared_bypass_enabled', True):
            return False
        mmu = self._get_mmu()
        if mmu is None:
            return False
        return mmu.tool_selected == TOOL_GATE_BYPASS

    def _shared_apply_bypass_spool(self, spool, uid, auto_created=False):
        if not self._shared_bypass_selected():
            return False

        script = "_NFC_SHARED_BYPASS_SPOOL_CHANGED SPOOL_ID=%d UID=%s" % (
            spool, uid or "")
        self.reactor.register_async_callback(
            lambda et, _s=script: self._safe_run_script(_s))
        self._shared_pending_uid            = None
        self._shared_pending_spool          = None
        self._shared_pending_deadline       = 0.0
        self._shared_pending_warning_fired  = False
        self._shared_pending_auto_created   = False
        self._shared_last_error             = None
        self._shared_read_deadline          = 0.0
        self._shared_missed_resolutions     = 0
        self._shared_clear_preload_approval()
        if self._shared_bypass_spool_ready_effect:
            self._shared_play_led_effect(
                self._shared_bypass_spool_ready_effect,
                event=EVENT_SPOOL_READY,
                duration=self._shared_bypass_ready_effect_duration)
        elif self._shared_spool_ready_effect:
            self._shared_play_led_effect(
                self._shared_spool_ready_effect, event=EVENT_SPOOL_READY,
                duration=self._shared_bypass_ready_effect_duration)
        self._shared_last_action = (
            "bypass active spool set to %d uid=%s auto_created=%s"
            % (spool, uid, auto_created))
        logger.info(
            "[%s]: shared tag resolved while bypass selected — "
            "setting active spool=%d uid=%s auto_created=%s",
            self._name, spool, uid, auto_created)
        self._console("[OK] NFC[shared]: bypass active spool set to %d (UID %s)"
                      % (spool, uid or ""))
        return True

    def _shared_stage_next_spool_id(self, spool, auto_created=False):
        script_lines = []
        if auto_created:
            script_lines.append("MMU_SPOOLMAN REFRESH=1 QUIET=1")
        script_lines.append("MMU_GATE_MAP NEXT_SPOOLID=%d QUIET=1" % spool)
        script = "\n".join(script_lines)
        self.reactor.register_async_callback(
            lambda et, _s=script: self._safe_run_script(_s))

    def _shared_handle_event(self, event_type, uid, spool):
        if event_type == EVENT_CHANGED and spool is DIRECT_METADATA_SPOOL:
            # Rich tag without a Spoolman spool ID — NEXT_SPOOLID requires an
            # integer.  Treat as unresolved unless spoolman_auto_create creates
            # a spool first (auto_create returns a real ID, not this sentinel).
            self._shared_last_error = (
                "rich tag has no Spoolman spool ID — "
                "enable spoolman_auto_create to create one automatically")
            if self._shared_missed_resolutions < self._shared_missed_limit:
                self._shared_missed_resolutions += 1
                logger.info(
                    "[%s]: shared rich tag uid=%s — no Spoolman spool ID; "
                    "enable spoolman_auto_create or register the spool manually "
                    "(attempt %d/%d)",
                    self._name, uid,
                    self._shared_missed_resolutions, self._shared_missed_limit)
                if (self._shared_tag_unresolved_effect
                        and self._shared_missed_resolutions == 1):
                    self._shared_play_tag_unresolved_effect()
                if self._shared_missed_resolutions == self._shared_missed_limit:
                    self._shared_unresolved_limit_reached(uid)
            return

        if event_type == EVENT_CHANGED and spool is not None:
            self._shared_expire_pending_if_needed()
            if self._shared_pending_spool is not None:
                pending_spool = self._shared_pending_spool
                if pending_spool == spool:
                    logger.info(
                        "[%s]: shared duplicate tag ignored — "
                        "spool=%d uid=%s",
                        self._name, spool, uid)
                    self._shared_last_action = (
                        "ignored duplicate read for pending spool %d" % spool)
                else:
                    logger.warning(
                        "[%s]: spool %d already pending; new spool=%d "
                        "uid=%s ignored — use NFC_SHARED REPLACE=1 to replace",
                        self._name, pending_spool, spool, uid)
                    self._shared_last_action = (
                        "ignored spool %d while spool %d pending"
                        % (spool, pending_spool))
                return
            auto_created = False
            if self._state.current_tag is not None:
                res = self._state.current_tag.resolution or {}
                auto_created = isinstance(res, dict) and res.get('path') == 'auto_create'
            if self._shared_apply_bypass_spool(spool, uid, auto_created):
                return
            now = self.reactor.monotonic()
            self._shared_pending_uid            = uid
            self._shared_pending_spool          = spool
            self._shared_pending_deadline       = now + self._shared_pending_timeout
            self._shared_pending_warning_fired  = False
            self._shared_pending_auto_created   = auto_created
            self._shared_last_error             = None
            self._shared_read_deadline          = 0.0
            self._shared_missed_resolutions     = 0
            self._shared_stage_next_spool_id(spool, auto_created)
            # Stop polling — pending spool survives tag removal.
            self._polling = False
            self.reactor.update_timer(
                self._poll_timer, now + 0.8 * self._shared_pending_timeout)
            # Warning timer fires at 80% of the pending timeout.
            self.reactor.update_timer(
                self._warning_timer,
                now + 0.8 * self._shared_pending_timeout)
            logger.info(
                "[%s]: shared tag resolved — spool=%d uid=%s "
                "auto_created=%s pending for %.0fs",
                self._name, spool, uid, auto_created,
                self._shared_pending_timeout)
            if self._debug >= 3:
                logger.info(
                    "[%s]: shared CHANGED — spool=%d uid=%s "
                    "auto_created=%s; polling stopped, awaiting PRELOAD_CHECK",
                    self._name, spool, uid, auto_created)
            _ac_note = " [new spool]" if auto_created else ""
            logger.info(
                "[%s]: spool %d detected (UID %s)%s — "
                "load spool into gate now",
                self._name, spool, uid, _ac_note)
            if self._gcode is not None:
                self._gcode.respond_info(color_console_tags(
                    "[OK] NFC[%s]: read tag — spool %d staged%s"
                    % (self._name, spool, _ac_note)))
            if self._shared_spool_ready_effect:
                self._shared_play_spool_ready_effect()
            self._shared_last_action = (
                "tag staged spool %d uid=%s auto_created=%s"
                % (spool, uid, auto_created))

        elif event_type == EVENT_UID_ONLY:
            if self._shared_pending_spool is None:
                self._shared_last_error = "tag uid=%s not in Spoolman" % uid
                if self._shared_missed_resolutions < self._shared_missed_limit:
                    self._shared_missed_resolutions += 1
                    logger.info(
                        "[%s]: shared UID-only — %s (attempt %d/%d)",
                        self._name, self._shared_last_error,
                        self._shared_missed_resolutions,
                        self._shared_missed_limit)
                    if (self._shared_tag_unresolved_effect
                            and self._shared_missed_resolutions == 1):
                        self._shared_play_tag_unresolved_effect()
                    if self._shared_missed_resolutions == 1 and self._debug >= 2:
                        logger.warning(
                            "[%s]: uid=%s not in Spoolman",
                            self._name, uid)
                    if self._shared_missed_resolutions == self._shared_missed_limit:
                        self._shared_unresolved_limit_reached(uid)
                tag  = self._state.current_tag
                meta = (tag.meta
                        if tag is not None and isinstance(tag.meta, dict)
                        else {})
                if not any(k not in ('uid',) for k in meta):
                    self._state.current_uid   = None
                    self._state.current_spool = None
            elif self._debug >= 3:
                logger.info(
                    "[%s]: shared UID-only ignored — pending "
                    "spool=%s uid=%s kept; new uid=%s unresolved",
                    self._name, self._shared_pending_spool,
                    self._shared_pending_uid, uid)

        elif event_type == EVENT_REMOVED:
            # Restore Happy Hare only when no spool is staged.
            # If a spool is pending, the ready/warning effect must stay visible.
            if self._shared_pending_spool is None:
                self._shared_restore_hh_leds()
            self._shared_missed_resolutions = 0
            if self._debug >= 3:
                logger.info(
                    "[%s]: shared tag removed — "
                    "pending spool=%s kept",
                    self._name, self._shared_pending_spool)

    def _shared_unresolved_limit_reached(self, uid):
        self._shared_missed_resolutions = 0
        self._state.current_uid   = None
        self._state.current_spool = None
        self._shared_read_deadline = 0.0
        logger.error(
            "[%s]: uid=%s not in Spoolman after %d attempts",
            self._name, uid, self._shared_missed_limit)
        logger.info(
            "[%s]: reader ready for next tag",
            self._name)

    def _shared_expire_pending_if_needed(self):
        if (self._shared_pending_spool is not None
                and self.reactor.monotonic() >= self._shared_pending_deadline):
            spool_id = self._shared_pending_spool
            logger.info(
                "[%s]: shared pending spool=%d timed out after %.0fs",
                self._name, spool_id, self._shared_pending_timeout)
            self._shared_clear_pending()
            self._shared_last_error = (
                "pending spool %d expired; tap tag again" % spool_id)
            self._shared_last_action = "pending spool %d expired" % spool_id
            return True
        return False

    def _shared_clear_pending(self):
        if self._debug >= 4:
            logger.debug(
                "[%s]: shared pending cleared "
                "(was spool=%s uid=%s)",
                self._name,
                self._shared_pending_spool,
                self._shared_pending_uid)
        self.reactor.update_timer(self._warning_timer, self.reactor.NEVER)
        self._shared_restore_hh_leds()
        self._shared_pending_uid            = None
        self._shared_pending_spool          = None
        self._shared_pending_deadline       = 0.0
        self._shared_pending_warning_fired  = False
        self._shared_pending_auto_created   = False
        self._shared_missed_resolutions     = 0
        self._shared_clear_preload_approval()

    def _shared_clear_preload_approval(self):
        self._shared_preload_spool        = None
        self._shared_preload_uid          = None
        self._shared_preload_auto_created = False

    def _shared_resume_startup_polling(self):
        if (self._startup_polling == 1 and not self._failed
                and not self._is_printing()
                and self._shared_pending_spool is None):
            self._shared_read_deadline = 0.0
            self._polling = True
            self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
            return True
        return False

    def _shared_expire_pending_and_maybe_resume(self):
        if self._shared_expire_pending_if_needed():
            # Always restart polling after timeout (equivalent to
            # NFC_SHARED REPLACE=1) so the user can tap a new tag
            # immediately without a manual command.
            polling_resumed = not self._failed and not self._is_printing()
            if polling_resumed:
                self._shared_missed_resolutions = 0
                self._shared_last_error = None
                self._shared_read_deadline = (
                    self.reactor.monotonic() + self._shared_read_timeout)
                self._polling = True
                self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
                logger.info(
                    "[%s]: shared pending timeout — "
                    "polling restarted (NFC_SHARED REPLACE=1 behavior)",
                    self._name)
            else:
                logger.info(
                    "[%s]: shared pending timeout — "
                    "polling not resumed (failed=%s printing=%s)",
                    self._name, self._failed, self._is_printing())
            resume_note = " Reader polling resumed." if polling_resumed else ""
            hook_hint = self._shared_preload_hook_hint()
            logger.error(
                "[ERROR] NFC[%s]: timeout after %.0fs — no spool was "
                "loaded.%s%s Tap tag to stage again.",
                self._name, self._shared_pending_timeout,
                resume_note, hook_hint)
            return True
        return False

    def _shared_preload_check(self, gcmd):
        self._shared_preload_policy().check(gcmd)

    def _shared_preload_commit(self, gcmd):
        self._shared_preload_policy().commit(gcmd)

    def _shared_preload_clear_assigned(self, gcmd):
        self._shared_preload_policy().clear_assigned(gcmd)

    def _shared_preload_policy(self):
        coordinator = getattr(self, '_shared_preload_coordinator', None)
        if coordinator is None:
            coordinator = shared_preload.MmuNfcSharedPreload(self)
            self._shared_preload_coordinator = coordinator
        return coordinator

    def _shared_state_text(self):
        if not getattr(self, '_enabled', True):
            return "disabled by config"
        now = self.reactor.monotonic()
        if self._failed:
            return "READER FAILED (check wiring)"
        if self._shared_pending_spool is not None:
            remaining = max(0.0, self._shared_pending_deadline - now)
            if remaining <= 0.0:
                spool_id = self._shared_pending_spool
                uid = self._shared_pending_uid or ''
                self._shared_expire_pending_and_maybe_resume()
                return "expired  spool %d  uid=%s" % (spool_id, uid)
            return ("pending spool %d  uid=%s  expires in %.0fs"
                    % (self._shared_pending_spool,
                       self._shared_pending_uid or '',
                       remaining))
        if self._shared_last_error:
            return "error  %s" % self._shared_last_error
        if self._polling:
            return "polling, no tag pending"
        return "idle"

    def shared_status_line(self):
        return "  shared (%s):  %s" % (
            self._reader_type, self._shared_state_text())

    def _shared_next_action(self):
        if not getattr(self, '_enabled', True):
            return "set enabled: True and restart Klipper"
        if self._failed:
            return "run NFC_SHARED INIT=1 after fixing wiring"
        if self._is_printing():
            return "wait for printing to finish; shared reads are blocked"
        if self._shared_pending_spool is not None:
            return "insert filament before timeout, or run NFC_SHARED REPLACE=1"
        if self._shared_last_error:
            last_action = self._shared_last_action or ''
            if "expired" in self._shared_last_error:
                return "tap the tag again"
            if "not in Spoolman" in self._shared_last_error:
                return "register the tag in Spoolman, or use MMU_PRELOAD"
            return "fix the reported issue, then trigger preload again"
        if self._polling:
            return "tap a spool tag"
        if self._startup_polling == 1:
            return "polling should resume automatically; run NFC_SHARED READ=1 if needed"
        return "run NFC_SHARED READ=1 to scan a spool"

    def shared_summary_line(self):
        return "%s  next: %s" % (
            self.shared_status_line().strip(), self._shared_next_action())

    def shared_status_detail(self):
        now = self.reactor.monotonic()
        lines = [self.shared_status_line()]
        if self._failed:
            lines.append("    recovery: run NFC_SHARED INIT=1 after fixing wiring")
        lines.append("    polling: %s" % ("on" if self._polling else "off"))
        lines.append("    startup_polling: %s" %
                     ("on" if self._startup_polling == 1 else "off"))
        lines.append("    read_deadline: %s" %
                     ("none" if self._shared_read_deadline <= 0.0
                      else "in %.0fs" % max(0.0, self._shared_read_deadline - now)))
        lines.append("    pending_spool: %s" %
                     (self._shared_pending_spool
                      if self._shared_pending_spool is not None else "none"))
        lines.append("    pending_uid: %s" %
                     (self._shared_pending_uid or "none"))
        lines.append("    pending_auto_created: %s" %
                     ("yes" if self._shared_pending_auto_created else "no"))
        lines.append("    preload_spool: %s" %
                     (self._shared_preload_spool
                      if self._shared_preload_spool is not None else "none"))
        lines.append("    preload_auto_created: %s" %
                     ("yes" if self._shared_preload_auto_created else "no"))
        lines.append("    pending_timeout: %.0fs" % self._shared_pending_timeout)
        lines.append("    read_timeout: %.0fs" % self._shared_read_timeout)
        lines.append("    missed_resolutions: %d/%d" %
                     (self._shared_missed_resolutions,
                      self._shared_missed_limit))
        lines.append("    force_spool_id: %s" %
                     ("on" if self._shared_force_spool_id else "off"))
        lines.append("    tag_read_effect:    %s" %
                     (self._shared_tag_read_effect or "none"))
        lines.append("    spool_ready_effect: %s" %
                     (self._shared_spool_ready_effect or "none"))
        lines.append("    tag_unresolved_effect: %s" %
                     (self._shared_tag_unresolved_effect or "none"))
        lines.append("    auto_create_effect: %s" %
                     (self._shared_auto_create_effect or "none"))
        lines.append("    last_action: %s" %
                     (self._shared_last_action or "none"))
        lines.append("    next: %s" % self._shared_next_action())
        if self._is_printing():
            lines.append("    safety: printing; shared reads are blocked")
        if self._shared_last_error:
            lines.append("    last_error: %s" % self._shared_last_error)
        return "\n".join(lines)

    def _shared_replace_pending(self, gcmd):
        if self._failed:
            logger.error(
                "[%s]: shared REPLACE=1 refused — reader failed; "
                "run INIT=1 first",
                self._name)
            gcmd.respond_info(color_console_tags(
                "[WARN] NFC[%s]: reader failed; run INIT=1 first"
                % self._name))
            return
        if self._is_printing():
            logger.warning(
                "[%s]: shared REPLACE=1 refused — printing",
                self._name)
            gcmd.respond_info(
                "[WARN] NFC[%s]: shared polling not started while printing"
                % self._name)
            return
        pending_spool = self._shared_pending_spool
        if pending_spool is not None:
            self._shared_clear_pending()
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: discarded pending spool %s; polling restarted"
                % (self._name, pending_spool)))
        else:
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: no pending spool to replace; polling started"
                % self._name))
        self._shared_missed_resolutions = 0
        self._shared_last_error = None
        self._shared_last_action = "replacement scan started"
        self._shared_read_deadline = (
            self.reactor.monotonic() + self._shared_read_timeout)
        self._polling = True
        self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
        logger.info(
            "[%s]: shared REPLACE=1 — discarded spool=%s; "
            "polling restarted with %.0fs read timeout",
            self._name, pending_spool, self._shared_read_timeout)

    def _shared_reset_and_poll(self, gcmd):
        if self._failed:
            logger.error(
                "[%s]: shared RESET=1 refused — reader failed; "
                "run INIT=1 first",
                self._name)
            gcmd.respond_info(color_console_tags(
                "[WARN] NFC[%s]: reader failed; run INIT=1 first"
                % self._name))
            return
        if self._is_printing():
            logger.warning(
                "[%s]: shared RESET=1 refused — printing",
                self._name)
            gcmd.respond_info(
                "[WARN] NFC[%s]: shared reset not started while printing"
                % self._name)
            return
        pending_spool = self._shared_pending_spool
        self._shared_clear_pending()
        self._shared_clear_preload_approval()
        self._shared_missed_resolutions = 0
        self._shared_last_error = None
        self._shared_last_action = "shared reset; polling restarted"
        self._state.current_uid = None
        self._state.current_spool = None
        self._shared_read_deadline = (
            self.reactor.monotonic() + self._shared_read_timeout)
        self._polling = True
        self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
        logger.info(
            "[%s]: shared RESET=1 — cleared spool=%s; "
            "polling restarted with %.0fs read timeout",
            self._name, pending_spool, self._shared_read_timeout)
        gcmd.respond_info(color_console_tags(
            "NFC[%s]: shared reset; LEDs restored and polling restarted"
            % self._name))

    def _shared_help(self, gcmd):
        lines = [
            "NFC_SHARED commands:",
            "  Add =1 to action flags; Klipper rejects bare forms like NFC_SHARED CANCEL.",
            "  NFC_SHARED READ=1          - start polling (rejected while printing)",
            "  NFC_SHARED READ=0          - stop polling (keeps pending spool)",
            "  NFC_SHARED STATUS=1        - show detailed shared reader state",
            "  NFC_SHARED SUMMARY=1       - show one-line shared reader state",
            "  NFC_SHARED HELP=1          - show this help",
            "  NFC_SHARED CANCEL=1        - cancel pending spool and stop polling",
            "  NFC_SHARED REPLACE=1       - discard pending spool and scan another",
            "  NFC_SHARED RESET=1         - clear shared state, restore LEDs, and poll",
            "  NFC_SHARED LED_TEST=1      - test configured shared tag-read LED effect",
            "",
            "Advanced shared-reader commands:",
            "  NFC_SHARED CLEAR=1         - clear pending state and stop polling",
            "  NFC_SHARED PRELOAD_CHECK=1 - Happy Hare hook command; approve NEXT_SPOOLID if valid",
            "  NFC_SHARED PRELOAD_COMMIT=1 SPOOL_ID=<n> - Happy Hare hook command; clear pending after NEXT_SPOOLID",
            "  NFC_SHARED PRELOAD_CLEAR_ASSIGNED=1 SPOOL_ID=<n> GATE=<n> - Happy Hare hook command; clear already-assigned shared spool",
            "  NFC_SHARED POLL=1          - run one full read/resolve cycle (skips printing)",
            "  NFC_SHARED SCAN=1          - raw hardware scan only (skips printing)",
            "  NFC_SHARED INIT=1          - re-run NFC Reader init; resumes startup polling if enabled",
            "  NFC_SHARED CLEAR_CACHE=1   - clear tag cache (keeps pending spool)",
        ]
        if self._low_level_debug and self._reader_type == 'rc522':
            lines.extend(rc522_driver.low_level_debug_help_lines("NFC_SHARED"))
        gcmd.respond_info('\n'.join(lines))

    def _read_mmu_pending_timeout(self, default=30.0):
        try:
            configfile = self.printer.lookup_object('configfile', None)
            if configfile is not None:
                raw_config = configfile.get_status(0).get('config', {})
                val = raw_config.get('mmu', {}).get('pending_spool_id_timeout', None)
                if val is not None:
                    timeout = float(val)
                    if timeout >= 1.0:
                        return timeout
        except Exception:
            logger.warning(
                "[%s]: could not read pending_spool_id_timeout from [mmu] config; "
                "using %.0fs default", self._name, default)
        return default

    def get_status(self, _eventtime=None):
        status = super().get_status(_eventtime)
        if not status.get('enabled', True):
            return status
        status['pending_spool_id'] = (
            self._shared_pending_spool
            if self._shared_pending_spool is not None else -1)
        status['pending_auto_created'] = bool(
            getattr(self, '_shared_pending_auto_created', False))
        status['preload_spool_id'] = (
            self._shared_preload_spool
            if getattr(self, '_shared_preload_spool', None) is not None else -1)
        status['preload_auto_created'] = bool(
            getattr(self, '_shared_preload_auto_created', False))
        status['has_per_lane_readers'] = bool(
            getattr(self, '_has_per_lane_readers', False))
        return status
