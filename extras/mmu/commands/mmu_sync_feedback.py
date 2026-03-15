#
# Implements MMU_SYNC_FEEDBACK command
#  - This is a "per-unit" command
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuSyncFeedbackCommand(BaseCommand):

    CMD = "MMU_SYNC_FEEDBACK"

    HELP_BRIEF = "Controls sync feedback and applies filament tension adjustments"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT           = #(int)|_name_|ALL Specify unit by name, number or all-units (optional if single unit)\n"
        + "ENABLE         = [1|0] enable/disable sync feedback control\n"
        + "RESET          = [1|0] reset sync controller and return RD to last known good value\n"
        + "ADJUST_TENSION = [1|0] apply correction to neutralize filament tension\n"
        + "AUTOTUNE       = [1|0] allow saving of autotuned rotation distance\n"
        + "(no parameters for status report)\n"
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
            category=CATEGORY_GENERAL,
            per_unit=True
        )

    def _run(self, gcmd, unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if mmu.check_if_disabled():
            return
        if mmu.check_if_bypass():
            return

        if not unit.has_sync_feedback():
            mmu.log_warning("No sync-feedback sensors on unit!")
            return

        # Get sync_feedback associated with unit
        sf = unit.sync_feedback

        has_tension, has_compression, has_proportional = sf.get_active_sensors()
        if not (has_proportional or has_tension or has_compression):
            mmu.log_warning("No sync-feedback sensors are enabled!")
            return

        enable = gcmd.get_int("ENABLE", None, minval=0, maxval=1)
        reset = gcmd.get_int("RESET", None, minval=0, maxval=1)
        autotune = gcmd.get_int("AUTOTUNE", None, minval=0, maxval=1)
        adjust_tension = gcmd.get_int("ADJUST_TENSION", 0, minval=0, maxval=1)

        if enable is not None:
            sf.p.sync_feedback_enabled = enable
            mmu.log_always("Sync feedback feature is %s" % ("enabled" if enable else "disabled"))

        if reset is not None and sf.p.sync_feedback_enabled:
            mmu.log_always("Sync feedback reset")
            eventtime = mmu.reactor.monotonic()
            sf._reset_controller(eventtime)

        if autotune is not None:
            mmu.UNIT.p.autotune_rotation_distance = autotune
            mmu.log_always(
                "Save Autotuned rotation distance feature is %s"
                % ("enabled" if autotune else "disabled")
            )

        if adjust_tension:
            try:
                # Cannot adjust sync feedback sensor if gears are not synced
                with mmu.wrap_sync_gear_to_extruder():
                    # Avoid spurious runout during tiny corrective moves (unlikely)
                    with mmu._wrap_suspend_filament_monitoring():
                        actual, success = sf.adjust_filament_tension()
                        if success:
                            mmu.log_info("Neutralized tension after moving %.2fmm" % actual)
                        elif success is False:
                            mmu.log_warning("Moved %.2fmm without neutralizing tension" % actual)
                        else:
                            mmu.log_warning("Operation not possible. Perhaps sensors are disabled?")

            except MmuError as ee:
                mmu.log_error("Error in MMU_SYNC_FEEDBACK: %s" % str(ee))

        # Status report if no "action" parameters were supplied
        if enable is None and reset is None and autotune is None and not adjust_tension:
            if sf.p.sync_feedback_enabled:
                mode = sf.ctrl.get_type_mode()
                active = " and currently active" if sf.active else " (not currently active)"
                msg = "Sync feedback feature with type-%s sensor is enabled%s\n" % (mode, active)

                rd_start = unit.calibrator.get_gear_rd()
                rd_current = sf.ctrl.get_current_rd()
                rd_rec = sf.ctrl.autotune.get_rec_rd()
                msg += "- Current RD: %.2f, Autotune recommended: %.2f, Default: %.2f\n" % (rd_current, rd_rec, rd_start)

                msg += "- State: %s\n" % sf.get_sync_feedback_string(detail=True)
                msg += "- FlowGuard: %s" % ("Active" if sf.flowguard_active else "Inactive")
                if has_proportional:
                    msg += " (Flowrate: %.1f%%)" % sf.flow_rate

                mmu.log_always(msg)
            else:
                mmu.log_always("Sync feedback feature is disabled")
