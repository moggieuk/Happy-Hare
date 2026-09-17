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
        f"{CMD}: {HELP_BRIEF}\n"
        + "UNIT           = #(int)|_name_ Implied by gate else specify name or number\n"
        + "ENABLE         = [1|0] enable/disable sync feedback control\n"
        + "RESET          = 1 reset sync controller and return RD to last known good value\n"
        + "ADJUST_TENSION = 1 apply correction to neutralize filament tension\n"
        + "RELEASE        = 1 park buffer at its spring rest position ('buffer_spring_state')\n"
        + "AUTOTUNE       = [1|0] allow saving of autotuned rotation distance\n"
        + "(no parameters for status report)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}                  ...Report sync feedback controller status\n"
        + f"{CMD} ENABLE=1         ...Enable sync feedback control on the active unit\n"
        + f"{CMD} ENABLE=0         ...Disable sync feedback control\n"
        + f"{CMD} RESET=1          ...Reset the controller and restore last known good rotation distance\n"
        + f"{CMD} RELEASE=1        ...Relieve the buffer spring by parking it at its resting state\n"
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
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu
        unit = self.get_unit(gcmd, mode="infer")

        if self.check_if_disabled(): return
        if self.check_if_bypass(): return

        if not unit.has_buffer():
            mmu.log_warning("No sync-feedback buffer on unit!")
            return

        # Get sync_feedback associated with unit
        sf = unit.sync_feedback

        has_tension, has_compression, has_proportional = sf.get_active_sensors()
        if not any((has_proportional, has_tension, has_compression)):
            mmu.log_warning("No sync-feedback sensors are enabled!")
            return

        enable = gcmd.get_int("ENABLE", None, minval=0, maxval=1)
        reset = gcmd.get_int("RESET", None, minval=0, maxval=1)
        autotune = gcmd.get_int("AUTOTUNE", None, minval=0, maxval=1)
        adjust_tension = gcmd.get_int("ADJUST_TENSION", 0, minval=0, maxval=1)
        release = gcmd.get_int("RELEASE", 0, minval=0, maxval=1)

        # A sprung buffer left mid-range holds its spring loaded between prints. 'buffer_spring_state'
        # already records where it rests unladen, so that is where RELEASE parks it. Unset ('none')
        # means no reliable resting position is known, so there is nothing to aim at.
        release_target = unit.buffer.buffer_spring_state_num if release else None
        if release and release_target is None:
            mmu.log_debug("Nothing to release: buffer has no configured 'buffer_spring_state'")
            release = 0
        elif release and not has_proportional:
            # Switch buffers can only home to neutral, so there is no way to hold one
            # at a rail. Say that rather than falling through to the generic failure.
            mmu.log_debug("Nothing to release: buffer spring release needs a proportional sensor")
            release = 0

        if enable is not None:
            sf.p.sync_feedback_enabled = enable
            mmu.log_always("Sync feedback feature is %s" % ("enabled" if enable else "disabled"))

        if reset is not None and sf.p.sync_feedback_enabled:
            mmu.log_always("Sync feedback reset")
            eventtime = mmu.reactor.monotonic()
            sf._reset_controller(eventtime)

        if autotune is not None:
            unit.p.autotune_rotation_distance = autotune
            mmu.log_always(
                "Save Autotuned rotation distance feature is %s"
                % ("enabled" if autotune else "disabled")
            )

        if adjust_tension or release:
            if self.check_if_not_loaded(): return
            target = release_target if release else 0.
            try:
                # Cannot adjust sync feedback sensor if gears are not synced
                with mmu.wrap_sync_gear_to_extruder():
                    # Avoid spurious runout during tiny corrective moves (unlikely)
                    with mmu.wrap_suspend_filament_monitoring():
                        actual, success = sf.adjust_filament_tension(target=target)
                        if success:
                            if release:
                                mmu.log_info("Released buffer spring after moving %.2fmm" % actual)
                            else:
                                mmu.log_info("Neutralized tension after moving %.2fmm" % actual)
                        elif success is False:
                            if release:
                                mmu.log_warning("Moved %.2fmm without releasing buffer spring" % actual)
                            else:
                                mmu.log_warning("Moved %.2fmm without neutralizing tension" % actual)
                        else:
                            mmu.log_warning("Operation not possible. Perhaps sensors are disabled?")

            except MmuError as ee:
                mmu.log_error("Error in MMU_SYNC_FEEDBACK: %s" % str(ee))

        # Status report if no "action" parameters were supplied
        if enable is None and reset is None and autotune is None and not adjust_tension and not release:
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
                    msg += "\n- Tangle Prevention: %s" % ("Enabled (BOOSTED)" if sf._tangle_prevention_boosted else "Enabled" if sf.p.tangle_prevention_enabled else "Disabled")

                mmu.log_always(msg)
            else:
                mmu.log_always("Sync feedback feature is disabled")
