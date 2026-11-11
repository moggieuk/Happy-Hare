# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Viewer controls and source telemetry; no simulator animation observations.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import re


def plain(value):
    from extras.mmu.telemetry.capture import plain as serialize
    return serialize(value)


class SourceViewer:
    """Console controls and source capture; no simulator animation observations."""

    def __init__(self, session):
        from extras.mmu.telemetry.capture import TelemetryCapture
        self.hh = session
        self.macro_catalog = self._macro_catalog()
        self.telemetry_capture = TelemetryCapture(session.mmu, limit=6000)
        self.session_id = session.mmu.movement_telemetry.session_id

    def read_telemetry(self, after=None):
        return self.telemetry_capture.read(after)

    def close(self):
        self.telemetry_capture.close()

    def _macro_catalog(self):
        macros = {name.split(' ', 1)[1]: obj for name, obj in self.hh.printer.objects.items()
                  if name.startswith('gcode_macro ') and hasattr(obj, 'variables')}
        assigned = set()
        for obj in macros.values():
            script = getattr(getattr(obj, 'template', None), 'script', '')
            for line in script.splitlines():
                if 'SET_GCODE_VARIABLE' not in line.upper():
                    continue
                macro = re.search(r'\bMACRO=([A-Za-z0-9_]+)', line, re.I)
                variable = re.search(r'\bVARIABLE=([A-Za-z0-9_]+)', line, re.I)
                if macro and variable:
                    assigned.add((macro[1].upper(), variable[1]))
        result = []
        for macro, obj in sorted(macros.items()):
            for name in sorted(obj.variables):
                if (name.startswith(('_', 'output_', 'saved_')) or
                        (macro.upper(), name) in assigned or
                        not re.fullmatch(r'[a-z][a-z0-9_]*', name)):
                    continue
                result.append((macro, name))
        return result

    def macro_parameters(self):
        result = []
        for macro, name in self.macro_catalog:
            obj = self.hh.printer.lookup_object('gcode_macro ' + macro)
            value = obj.variables[name]
            result.append(dict(id='macro:' + macro + ':' + name, name=name, macro=macro,
                               value=plain(value), literal=repr(value), kind='macro',
                               group=macro, section='Macro variables'))
        return result

    def parameters(self, unit_index):
        # Exactly the parameter families used by MMU_TEST_CONFIG. Guards remain
        # authoritative, and edits are dispatched through that command itself.
        mmu = self.hh.mmu
        unit = mmu.mmu_machine.units[unit_index]
        sets = [mmu.p, unit.p, unit.selector.p, unit.toolhead_wrapper.p]
        if mmu.gate_selected >= 0:
            from extras.mmu.commands.mmu_test_config import MmuCalibrationParameters
            sets.append(MmuCalibrationParameters(mmu))
        groups = ['Machine Wide', f'Unit {unit_index}', f'Unit {unit_index}',
                  f"Toolhead '{unit.toolhead_wrapper.name}' · Unit {unit_index}"]
        if mmu.gate_selected >= 0:
            groups.append(f'Other · Calibration ({mmu.mmu_unit().name} / gate {mmu.gate_selected})')
        result = {}
        for index, params in enumerate(sets):
            for spec in params._SPECS:
                if params._is_available(spec):
                    value = getattr(params, spec.name)
                    result[spec.name] = dict(name=spec.name, kind=spec.kind,
                        value=plain(value), section=spec.section, choices=spec.choices,
                        scope='machine' if index == 0 else 'unit' if index < 4 else 'other',
                        group=groups[index])
        return list(result.values())
