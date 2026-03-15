# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_CALC_PURGE_VOLUMES command
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


class MmuCalcPurgeVolumesCommand(BaseCommand):

    CMD = "MMU_CALC_PURGE_VOLUMES"

    HELP_BRIEF = "Calculate purge volume matrix based on filament color overriding slicer tool map import"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "MIN        = #    Minimum purge volume (mm^3)\n"
        + "MAX        = #    Maximum purge volume (mm^3)\n"
        + "MULTIPLIER = #    Scale multiplier (float)\n"
        + "SOURCE     = [gatemap|slicer]  Color source to build matrix from\n"
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
            category=CATEGORY_GENERAL
        )

    def _run(self, gcmd):
        # BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if mmu.check_if_disabled(): return

        try:
            mmu._fix_started_state()

            min_purge = gcmd.get_int('MIN', 0, minval=0)
            max_purge = gcmd.get_int('MAX', 800, minval=1)
            multiplier = gcmd.get_float('MULTIPLIER', 1., above=0.)
            source = gcmd.get('SOURCE', 'gatemap')
            if source not in ['gatemap', 'slicer']:
                raise gcmd.error("Invalid color source: %s. Options are: gatemap, slicer" % source)
            if min_purge >= max_purge:
                raise gcmd.error("MAX purge volume must be greater than MIN")

            tool_rgb_colors = []
            if source == 'slicer':
                # Pull colors from existing slicer map
                for tool in range(mmu.num_gates):
                    tool_info = mmu.slicer_tool_map['tools'].get(str(tool))
                    if tool_info:
                        tool_rgb_colors.append(mmu._color_to_rgb_hex(tool_info.get('color', '')))
                    else:
                        tool_rgb_colors.append(mmu._color_to_rgb_hex(''))
            else:
                # Logic to use tools mapped to gate colors with current ttg map
                for tool in range(mmu.num_gates):
                    gate = mmu.ttg_map[tool]
                    tool_rgb_colors.append(mmu._color_to_rgb_hex(mmu.gate_color[gate]))

            try:
                mmu.slicer_tool_map['purge_volumes'] = mmu._generate_purge_matrix(
                    tool_rgb_colors, min_purge, max_purge, multiplier
                )
                mmu.log_always("Purge map updated. Use 'MMU_SLICER_TOOL_MAP PURGE_MAP=1' to view")
            except Exception as e:
                # Convert unexpected exceptions into MmuError so caller wrapper handles them consistently
                raise MmuError("Error generating purge volues: %s" % str(e))

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
