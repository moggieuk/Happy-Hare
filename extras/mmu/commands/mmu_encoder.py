# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_ENCODER command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants    import *
from ..mmu_utils        import MmuError
from .mmu_base_command  import *


class MmuEncoderCommand(BaseCommand):

    CMD = "MMU_ENCODER"

    HELP_BRIEF = "Display encoder position and stats or reset position"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "POS    = #(float) Sets the encoder as close as possible to specified position (subject to resolution)\n"
        + "VALUE  = #(float) Alias for POS=\n"
        + "QUIET  = 1 for less verbose output\n"
        + "(no parameters for status)\n"
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


    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        self.mmu_unit = mmu_unit
        mmu = self.mmu

        if self.check_if_disabled(): return
        if self.check_if_no_encoder(mmu_unit): return
        encoder = self.mmu_unit.encoder

        def show():
            status = encoder.get_status(0)
            
            lines = []
            lines.append(f"Encoder {encoder.name} position: {status['encoder_pos']:.1f}")
            
            if not quiet:
                mode = status["detection_mode"]
                clog = ENCODER_DETECTION_MODES[mode] if 0 <= mode < len(ENCODER_DETECTION_MODES) else "Unknown"
                flowguard = 'Off' if mode == 0 else 'Active' if status['enabled'] else 'Inactive'

                lines.append(f"FlowGuard/Runout: {flowguard}")
                lines.append(f"- Detection mode: {clog}")
                lines.append(f"- Detection length: {status['detection_length']:.1f}mm")
                lines.append(f"- Remaining headroom before trigger: {status['headroom']:.1f}mm (min: {status['min_headroom']:.1f}mm)")
                lines.append(f"- Flowrate: {status['flow_rate']}%")
            
            mmu.log_info("\n".join(lines))

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        pos = gcmd.get_float('POS', None, minval=0.)
        if pos is None:
            pos = gcmd.get_float('VALUE', None, minval=0.)

        if pos is not None:
            mmu.set_encoder_distance(pos)
            quiet = True

        show()
