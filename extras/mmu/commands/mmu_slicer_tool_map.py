# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SLICER_TOOL_MAP command
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


class MmuSlicerToolMapCommand(BaseCommand):

    CMD = "MMU_SLICER_TOOL_MAP"

    HELP_BRIEF = "Display or define the tools used in print as specified by slicer"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "DETAIL           = [0|1]\n"
        + "PURGE_MAP        = [0|1]\n"
        + "SPARSE_PURGE_MAP = [0|1]\n"
        + "RESET            = [0|1]\n"
        + "INITIAL_TOOL     = t\n"
        + "TOTAL_TOOLCHANGES= #\n"
        + "TOOL             = t\n"
        + "MATERIAL         = _text_\n"
        + "COLOR            = _text_\n"
        + "NAME             = _text_\n"
        + "TEMP             = #\n"
        + "USED             = [0|1]\n"
        + "PURGE_VOLUMES    = comma,separated,values\n"
        + "NUM_SLICER_TOOLS = # (optional, <= num_gates)\n"
        + "AUTOMAP          = strategy (optional)\n"
        + "SKIP_AUTOMAP     = [0|1] (one-print option)\n"
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
        # BaseCommand already logs commandline + handles HELP=1.
        mmu = self.mmu

        if mmu.check_if_disabled(): return
        mmu._fix_started_state()

        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        purge_map = bool(gcmd.get_int('PURGE_MAP', 0, minval=0, maxval=1))
        sparse_purge_map = bool(gcmd.get_int('SPARSE_PURGE_MAP', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        initial_tool = gcmd.get_int('INITIAL_TOOL', None, minval=0, maxval=mmu.num_gates - 1)
        total_toolchanges = gcmd.get_int('TOTAL_TOOLCHANGES', None, minval=0)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=mmu.num_gates - 1)
        material = gcmd.get('MATERIAL', "unknown")
        color = gcmd.get('COLOR', "").lower()
        name = gcmd.get('NAME', "")  # Filament name
        temp = gcmd.get_int('TEMP', 0, minval=0)
        used = bool(gcmd.get_int('USED', 1, minval=0, maxval=1))  # Is used in print
        purge_volumes = gcmd.get('PURGE_VOLUMES', "")
        num_slicer_tools = gcmd.get_int('NUM_SLICER_TOOLS', mmu.num_gates, minval=1, maxval=mmu.num_gates)
        automap_strategy = gcmd.get('AUTOMAP', None)
        skip_automap = gcmd.get_int('SKIP_AUTOMAP', None, minval=0, maxval=1)

        quiet = False
        if reset:
            mmu._clear_slicer_tool_map()
            quiet = True
        else:
            # Ensure webhooks see a change if we edit map in place
            mmu.slicer_tool_map = dict(mmu.slicer_tool_map)

        # One-print option to suppress automatic automap
        if skip_automap is not None:
            mmu._restore_automap_option(bool(skip_automap))

        if tool >= 0:
            mmu.slicer_tool_map['tools'][str(tool)] = {'color': color, 'material': material, 'temp': temp, 'name': name, 'in_use': used}
            if used:
                mmu.slicer_tool_map['referenced_tools'] = sorted(set(mmu.slicer_tool_map['referenced_tools'] + [tool]))
                if not mmu.slicer_tool_map.get('skip_automap', False) and automap_strategy and automap_strategy != AUTOMAP_NONE:
                    mmu._automap_gate(tool, automap_strategy)
            if color:
                mmu._update_slicer_color_rgb()
            quiet = True

        if initial_tool is not None:
            mmu.slicer_tool_map['initial_tool'] = initial_tool
            mmu.slicer_tool_map['referenced_tools'] = sorted(set(mmu.slicer_tool_map['referenced_tools'] + [initial_tool]))
            quiet = True

        if total_toolchanges is not None:
            mmu.slicer_tool_map['total_toolchanges'] = total_toolchanges
            quiet = True

        if purge_volumes != "":
            try:
                volumes = list(map(float, purge_volumes.split(',')))
                n = len(volumes)
                num_tools = mmu.num_gates
                if n == 1:
                    calc = lambda x, y: volumes[0] * 2
                elif n == num_slicer_tools:
                    calc = lambda x, y: volumes[y] + volumes[x]
                elif n == num_slicer_tools ** 2:
                    calc = lambda x, y: volumes[y + x * num_slicer_tools]
                elif n == num_slicer_tools * 2:
                    calc = lambda x, y: volumes[y] + volumes[num_slicer_tools + x]
                else:
                    raise gcmd.error(
                        "Incorrect number of values for PURGE_VOLUMES. Expected 1, %d, %d, or %d, got %d"
                        % (num_tools, num_tools * 2, num_tools ** 2, n)
                    )

                should_calc = lambda x, y: x < num_slicer_tools and y < num_slicer_tools and x != y
                # Build purge volume map (x=to_tool, y=from_tool)
                mmu.slicer_tool_map['purge_volumes'] = [
                    [
                        calc(x, y) if should_calc(x, y) else 0
                        for y in range(mmu.num_gates)
                    ]
                    for x in range(mmu.num_gates)
                ]
            except ValueError as e:
                raise gcmd.error("Error parsing PURGE_VOLUMES: %s" % str(e))
            quiet = True

        if not quiet:
            colors = sum(1 for t in mmu.slicer_tool_map['tools'] if mmu.slicer_tool_map['tools'][t]['in_use'])
            have_purge_map = len(mmu.slicer_tool_map.get('purge_volumes', [])) > 0
            msg = "No slicer tool map loaded"
            if colors > 0 or mmu.slicer_tool_map.get('initial_tool') is not None:
                msg = "--------- Slicer MMU Tool Summary ---------\n"
                msg += "Single color print" if colors <= 1 else "%d color print" % colors
                msg += " (Purge volume map loaded)\n" if colors > 1 and have_purge_map else "\n"
                for t, params in mmu.slicer_tool_map['tools'].items():
                    if params['in_use'] or detail:
                        msg += "T%d (gate %d, %s, %s, %d%sC)" % (int(t), mmu.ttg_map[int(t)], params['material'], params['color'], params['temp'], UI_DEGREE)
                        msg += " Not used\n" if detail and not params['in_use'] else "\n"
                if mmu.slicer_tool_map.get('initial_tool') is not None:
                    msg += "Initial Tool: T%d" % mmu.slicer_tool_map['initial_tool']
                    msg += " (will use bypass)\n" if colors <= 1 and mmu.tool_selected == TOOL_GATE_BYPASS else "\n"
                msg += "-------------------------------------------"
            if detail or purge_map or sparse_purge_map:
                if have_purge_map:
                    rt = mmu.slicer_tool_map['referenced_tools']
                    volumes = [row[:num_slicer_tools] for row in mmu.slicer_tool_map['purge_volumes'][:num_slicer_tools]]
                    msg += "\nPurge Volume Map (mm^3):\n"
                    msg += "To ->" + UI_SEPARATOR.join("{}T{: <2}".format(UI_SPACE, i) for i in range(num_slicer_tools)) + "\n"
                    msg += '\n'.join([
                        "T{: <2}{}{}".format(y, UI_SEPARATOR, ' '.join(
                            map(lambda v, x, y=y: str(round(v)).rjust(4, UI_SPACE)
                                if (not sparse_purge_map or (y in rt and x in rt)) and v > 0
                                else "{}{}-{}".format(UI_SPACE, UI_SPACE, UI_SPACE),
                                row, range(len(row))
                            )
                        ))
                        for y, row in enumerate(volumes)
                    ])
                elif have_purge_map:
                    msg += "\nDETAIL=1 to see purge volume map"
            mmu.log_always(msg)
