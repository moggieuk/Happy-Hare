# klippy/extras/mmu/mmu_nfc_shared_preload.py
#
# Shared reader preload coordination.
#
# Keeps the shared-reader preload transaction policy separate from NFCGate's
# hardware polling and tag-resolution responsibilities.  Per-lane assignments
# take precedence over shared-reader staging.

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


class MmuNfcSharedPreload:
    def __init__(self, gate):
        self._gate = gate

    def check(self, gcmd):
        gate = self._gate
        expected_spool = gcmd.get_int('EXPECTED_SPOOL_ID', -1)
        gate._shared_clear_preload_approval()
        if gate._debug >= 3:
            logger.info(
                "[%s]: PRELOAD_CHECK entered — pending spool=%s uid=%s",
                gate._name, gate._shared_pending_spool,
                gate._shared_pending_uid)
        if gate._is_printing():
            logger.warning(
                "[%s]: PRELOAD_CHECK skipped — printing",
                gate._name)
            if expected_spool > 0:
                logger.warning(
                    "[%s]: pending spool %d is no longer valid; "
                    "NEXT_SPOOLID not staged",
                    gate._name, expected_spool)
                gcmd.respond_info(color_console_tags(
                    "[WARN] NFC[%s]: PRELOAD_CHECK skipped while printing; "
                    "NEXT_SPOOLID not staged" % gate._name))
            return

        expired = gate._shared_expire_pending_if_needed()
        if gate._shared_pending_spool is None:
            logger.info(
                "[%s]: PRELOAD_CHECK — no pending spool; "
                "advising manual preload",
                gate._name)
            if expected_spool > 0:
                logger.warning(
                    "[%s]: pending spool %d is no longer valid%s; "
                    "NEXT_SPOOLID not staged",
                    gate._name, expected_spool,
                    " (expired)" if expired else "")
                gcmd.respond_info(color_console_tags(
                    "[WARN] NFC[%s]: pending spool %d is no longer valid%s; "
                    "NEXT_SPOOLID not staged"
                    % (gate._name, expected_spool,
                       " (expired)" if expired else "")))
                return
            if gate._shared_force_spool_id:
                logger.error(
                    "[ERROR] NFC[%s]: force_spool_id is set — tap your spool "
                    "tag on the shared reader before loading, or disable "
                    "force_spool_id to allow untagged loads",
                    gate._name)
                gcmd.respond_info(color_console_tags(
                    "[ERROR] NFC[%s]: force_spool_id is set — tap your spool "
                    "tag on the shared reader before loading, or disable "
                    "force_spool_id to allow untagged loads" % gate._name))
                gate._shared_last_action = (
                    "blocked preload without staged spool")
                return
            logger.error(
            "[ERROR] NFC[%s]: no spool staged — tap your spool tag on the "
            "shared reader first, or use MMU_PRELOAD to load without "
            "spool assignment",
            gate._name)
            gcmd.respond_info(color_console_tags(
                "[ERROR] NFC[%s]: no spool staged — tap your spool tag on the "
                "shared reader first, or use MMU_PRELOAD to load without "
                "spool assignment" % gate._name))
            gate._shared_last_action = "preload check found no staged spool"
            return

        spool_id = gate._shared_pending_spool
        auto_created = gate._shared_pending_auto_created
        if expected_spool > 0 and expected_spool != spool_id:
            logger.warning(
                "[%s]: PRELOAD_CHECK — macro saw spool %d but "
                "pending spool is %d; aborting stale bridge",
                gate._name, expected_spool, spool_id)
            logger.warning(
                "[%s]: pending spool changed from %d to %d; "
                "NEXT_SPOOLID not staged — trigger the preload again",
                gate._name, expected_spool, spool_id)
            return

        gate._shared_clear_pending_warning_feedback()
        logger.info(
            "[%s]: PRELOAD_CHECK — staging NEXT_SPOOLID=%d "
            "uid=%s auto_created=%s",
            gate._name, spool_id, gate._shared_pending_uid, auto_created)
        if gate._debug >= 3:
            logger.info(
                "[%s]: PRELOAD_CHECK — sending spool %d to Happy Hare "
                "via MMU_GATE_MAP NEXT_SPOOLID",
                gate._name, spool_id)

        # MMU_GATE_MAP and MMU_SPOOLMAN REFRESH are called from the
        # _NFC_SHARED_PRELOAD macro, not here.  Calling run_script() from
        # inside a GCode command handler deadlocks Klipper's GCode queue.
        gate._shared_preload_spool        = spool_id
        gate._shared_preload_uid          = gate._shared_pending_uid
        gate._shared_preload_auto_created = auto_created
        _ac_note = " [new spool synced]" if auto_created else ""
        logger.info(
            "[OK] NFC[%s]: spool %d approved%s — ready for preload commit",
            gate._name, spool_id, _ac_note)
        gcmd.respond_info(color_console_tags(
            "[OK] NFC[%s]: spool %d approved%s — ready for preload commit"
            % (gate._name, spool_id, _ac_note)))
        logger.info(
            "[%s]: PRELOAD_CHECK — spool %d validated, "
            "waiting for PRELOAD_COMMIT",
            gate._name, spool_id)
        gate._shared_last_action = (
            "approved spool %d for NEXT_SPOOLID" % spool_id)

    def commit(self, gcmd):
        gate = self._gate
        spool_id = gcmd.get_int('SPOOL_ID', -1)
        if gate._shared_preload_spool is None:
            logger.warning(
                "[%s]: PRELOAD_COMMIT without approved spool; "
                "pending spool kept",
                gate._name)
            gcmd.respond_info(color_console_tags(
                "[WARN] NFC[%s]: PRELOAD_COMMIT without approved spool; "
                "pending spool kept" % gate._name))
            return
        if spool_id != gate._shared_preload_spool:
            logger.warning(
                "[%s]: PRELOAD_COMMIT spool mismatch "
                "(got %d, approved %d); pending spool kept",
                gate._name, spool_id, gate._shared_preload_spool)
            gcmd.respond_info(color_console_tags(
                "[WARN] NFC[%s]: PRELOAD_COMMIT spool mismatch "
                "(got %d, approved %d); pending spool kept"
                % (gate._name, spool_id, gate._shared_preload_spool)))
            return
        if gate._shared_pending_spool != spool_id:
            logger.warning(
                "[%s]: pending spool changed before commit "
                "(got %s, approved %d); pending spool kept",
                gate._name, gate._shared_pending_spool, spool_id)
            gcmd.respond_info(color_console_tags(
                "[WARN] NFC[%s]: pending spool changed before commit "
                "(got %s, approved %d); pending spool kept"
                % (gate._name, gate._shared_pending_spool, spool_id)))
            return
        gate._shared_clear_pending()
        gate._shared_last_action = (
            "staged spool %d via NEXT_SPOOLID" % spool_id)
        gate._shared_read_deadline = 0.0
        gate._polling = True
        gate.reactor.update_timer(gate._poll_timer, gate.reactor.NOW)
        logger.info(
            "[%s]: PRELOAD_CHECK complete — pending cleared, "
            "polling restarted",
            gate._name)
        logger.info(
            "[OK] NFC[%s]: spool %d loaded — ready for next tag",
            gate._name, spool_id)
        gcmd.respond_info(color_console_tags(
            "[OK] NFC[%s]: spool %d loaded — ready for next tag"
            % (gate._name, spool_id)))

    def clear_assigned(self, gcmd):
        gate = self._gate
        spool_id = gcmd.get_int('SPOOL_ID', -1)
        assigned_gate = gcmd.get_int('GATE', -1)
        if spool_id <= 0:
            logger.warning(
                "[%s]: PRELOAD_CLEAR_ASSIGNED ignored — "
                "invalid SPOOL_ID=%d",
                gate._name, spool_id)
            gate._shared_last_action = (
                "ignored per-lane clear with invalid spool id")
            return
        expired = gate._shared_expire_pending_if_needed()
        if gate._shared_pending_spool is None:
            logger.info(
                "[%s]: PRELOAD_CLEAR_ASSIGNED — spool %d already "
                "owned by Happy Hare; shared pending was already clear%s",
                gate._name, spool_id, " after expiry" if expired else "")
            gate._shared_last_action = (
                "ignored per-lane clear for spool %d; no shared pending"
                % spool_id)
            return
        if gate._shared_pending_spool != spool_id:
            logger.warning(
                "[%s]: PRELOAD_CLEAR_ASSIGNED — Happy Hare owns spool %d "
                "but shared pending is spool %d; leaving pending intact",
                gate._name, spool_id, gate._shared_pending_spool)
            gate._shared_last_action = (
                "ignored per-lane clear for spool %d; pending spool is %d"
                % (spool_id, gate._shared_pending_spool))
            return
        if gate._has_per_lane_readers:
            logger.info(
                "[%s]: PRELOAD_CLEAR_ASSIGNED — spool %d already assigned "
                "by per-lane reader; clearing pending (no NEXT_SPOOLID needed)",
                gate._name, spool_id)
        else:
            where = ("gate %d" % assigned_gate
                     if assigned_gate >= 0 else "a gate")
            logger.info(
                "[%s]: PRELOAD_CLEAR_ASSIGNED — spool %d already assigned "
                "to gate %d; clearing stale Happy Hare assignment",
                gate._name, spool_id, assigned_gate)
            logger.warning(
                "NFC[%s]: spool %d already assigned to %s; clearing stale "
                "Happy Hare assignment",
                gate._name, spool_id, where)
            gcmd.respond_info(color_console_tags(
                "[WARN] NFC[%s]: spool %d already assigned to %s; clearing "
                "stale Happy Hare assignment"
                % (gate._name, spool_id, where)))
        gate._shared_clear_pending()
        gate._shared_last_action = (
            "cleared spool %d because Happy Hare already had it assigned" % spool_id)
        gate._shared_read_deadline = 0.0
        gate._polling = True
        gate.reactor.update_timer(gate._poll_timer, gate.reactor.NOW)
        logger.info(
            "[OK] NFC[%s]: spool %d loaded — ready for next tag",
            gate._name, spool_id)
        gcmd.respond_info(color_console_tags(
            "[OK] NFC[%s]: spool %d loaded — ready for next tag"
            % (gate._name, spool_id)))
