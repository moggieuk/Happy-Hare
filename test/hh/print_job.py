# Happy Hare test harness - loading a print job for the console's `/print job`.
#
# A job is prepared the way Moonraker prepares an upload: a file already carrying the
# "; processed by HappyHare" fingerprint is played as written, anything else goes through the
# REAL preprocessor in components/mmu_server.py (placeholder substitution, T<n> ->
# MMU_CHANGE_TOOL ... NEXT_POS). The only liberty taken is where missing slicer metadata comes
# from: a file without a slicer footer gets the colours, temperatures, materials and names of
# the gates its tools map to.
#
# The built-in job is the exception that is MOLDED to the machine first: its tool numbers are
# folded onto the gates that exist and its metadata is exactly num_gates long, so it runs clean
# on every profile. A user's file is never molded - a job that needs more tools than the MMU
# has must fail inside Happy Hare, as it would on a printer.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import re
from types import SimpleNamespace

from test.hh.moonraker import install_component_stubs

BUILTIN_JOB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'support', 'print_job.gcode')

_TOOL_WORD_RE = re.compile(r'^(\s*T)(\d+)\b', re.I)
_TOOL_PARAM_RE = re.compile(r'\b((?:INITIAL_)?TOOL=)(\d+)', re.I)

DEFAULT_COLOR = '000000'
DEFAULT_MATERIAL = 'PLA'


def mold(lines, num_gates):
    """Fold every tool number in the built-in job onto the gates this machine has."""
    def fold(m):
        return m.group(1) + str(int(m.group(2)) % num_gates)

    molded = []
    for line in lines:
        if not line.lstrip().startswith(';'):
            line = _TOOL_PARAM_RE.sub(fold, _TOOL_WORD_RE.sub(fold, line))
        molded.append(line)
    return molded


def gate_map_metadata(gate_maps, num_tools, default_temp):
    """Slicer metadata for tools 0..num_tools-1, read from the gates those tools map to."""
    colors, temps, materials, names = [], [], [], []
    for tool in range(num_tools):
        ttg = gate_maps.ttg_map
        gate = ttg[tool] if tool < len(ttg) else -1
        if 0 <= gate < len(gate_maps.gate_color):
            colors.append(gate_maps.gate_color[gate] or DEFAULT_COLOR)
            temps.append(str(gate_maps.gate_temperature[gate] or default_temp))
            materials.append(gate_maps.gate_material[gate] or DEFAULT_MATERIAL)
            names.append(gate_maps.gate_filament_name[gate] or '')
        else:
            colors.append(DEFAULT_COLOR)
            temps.append(str(default_temp))
            materials.append(DEFAULT_MATERIAL)
            names.append('')
    return colors, temps, materials, names


def prepare(path, mmu, workdir, builtin=False):
    """
    The lines to play for the job at `path`, and a short note on how it was prepared.
    """
    mmu_server = install_component_stubs()
    source = path
    if builtin:
        with open(path, encoding='utf-8') as fh:
            lines = mold(fh.read().split('\n'), mmu.num_gates)
        source = os.path.join(workdir, 'molded.gcode')
        with open(source, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines))
    elif mmu_server.gcode_processed_already(path):
        return _read(path), 'already processed by Happy Hare, played as written'

    (_has_placeholder, tools, total_toolchanges, colors, temps, materials, purge_volumes,
     names, slicer) = mmu_server.parse_gcode_file(source)
    if slicer is None:
        # What Moonraker would do too: the preprocessor only acts on files it can attribute
        return _read(source), 'no recognised slicer header, played unprocessed'

    if builtin:
        num_tools = mmu.num_gates
    else:
        num_tools = max([len(colors), len(temps), len(materials), len(names),
                         (max(tools) + 1) if tools else 1])
    fill = gate_map_metadata(mmu.gate_maps, num_tools, int(mmu.p.default_extruder_temp))
    lists = [colors, temps, materials, names]
    for i, found in enumerate(lists):
        if builtin or not found:
            lists[i] = fill[i]
    colors, temps, materials, names = lists

    out = os.path.join(workdir, 'processed.gcode')
    # process_file() reads the preprocessor script's own global `args`
    mmu_server.args = SimpleNamespace(average_toolchange_time=0.)
    mmu_server.process_file(source, out, True, tools, total_toolchanges, colors, temps,
                            materials, purge_volumes, names)
    note = 'processed by the Happy Hare preprocessor (%s)' % slicer
    if builtin:
        note = 'molded to %d gates, then %s' % (mmu.num_gates, note)
    return _read(out), note


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read().split('\n')
