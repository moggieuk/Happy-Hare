# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_ENCODER_RUNOUT command
#
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


class MmuEncoderRunoutCommand(BaseCommand):
    """
    Internal encoder filament runout handler.
    """

    CMD = "__MMU_ENCODER_RUNOUT"

    HELP_BRIEF = "Internal encoder filament runout handler"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "EVENTTIME  = #(float)\n"
        + "GENERATION = #(int)\n"
    )
    HELP_SUPPLEMENT = ""  # Internal callback command

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_INTERNAL
        )

    def _run(self, gcmd):
        # BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if not mmu.is_enabled:
            # Undo what runout sensor handling did
            mmu.pause_resume.send_resume_command()
            return

        mmu.fix_started_state()

        eventtime = gcmd.get_float('EVENTTIME', mmu.reactor.monotonic())
        generation = gcmd.get_int('GENERATION', None)
        encoder = mmu.encoder() if mmu.has_encoder() else None

        # Encoder runout is inferred state, so re-enabling starts a new observation
        # epoch. Never deliver an event queued against an earlier epoch after a load,
        # unload, toolchange, pause, or other monitoring suspension has reset it.
        stale = (
            encoder is None
            or not encoder.active
            or not encoder.is_flowguard_enabled()
            or generation is None
            or generation != encoder.get_flowguard_generation()
            or eventtime < mmu.runout_last_handled_time
        )
        if stale:
            mmu.log_debug(
                "Stale encoder runout event ignored "
                "(event=%.3f generation=%s current_generation=%s now=%.3f)"
                % (eventtime, generation,
                   encoder.get_flowguard_generation() if encoder else None,
                   mmu.reactor.monotonic())
            )
            # Undo what encoder runout event handling did before waiting on gcode.
            mmu.pause_resume.send_resume_command()
            return

        try:
            with mmu.wrap_sync_gear_to_extruder():
                # Could be clog/tangle or runout
                mmu._runout(sensor="Encoder")

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
