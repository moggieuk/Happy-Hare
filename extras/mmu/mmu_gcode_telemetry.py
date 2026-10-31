# Happy Hare MMU Software
# Copyright (C) 2022-2026 Paul Morgan. GPLv3.
"""Scoped observation of MMU-owned G1 extrusion. No idle G1 wrapper."""
import contextlib
import math
import greenlet


class GcodeExtrusionTelemetry:
    def __init__(self, mmu):
        self.mmu = mmu
        self.owners = {}
        self.original = None
        self.handler = self._handle_g1
        self.handling = set()

    @contextlib.contextmanager
    def scope(self, name):
        telemetry = self.mmu.movement_telemetry
        if not telemetry.listeners:
            yield
            return
        owner = greenlet.getcurrent()
        outer = not self.owners
        if outer:
            original = self.mmu.gcode.register_command('G1', None)
            if original is None:
                yield
                return
            self.original = original
            try:
                self.mmu.gcode.register_command('G1', self.handler)
            except BaseException:
                self.mmu.gcode.register_command('G1', original)
                self.original = None
                raise
        stack = self.owners.setdefault(owner, [])
        stack.append(name)
        try:
            yield
        finally:
            stack.pop()
            if not stack:
                del self.owners[owner]
            if not self.owners:
                current = self.mmu.gcode.register_command('G1', None)
                # Do not overwrite a replacement installed by another extension.
                self.mmu.gcode.register_command('G1', self.original if current == self.handler else current)
                self.original = None

    def _handle_g1(self, gcmd):
        mmu = self.mmu
        owner = greenlet.getcurrent()
        telemetry = mmu.movement_telemetry
        if (owner not in self.owners or owner in self.handling or
                'E' not in gcmd.get_command_parameters() or telemetry.operations):
            return self.original(gcmd)
        move = mmu.printer.lookup_object('gcode_move')
        before = list(move.last_position)
        e_index = getattr(move, 'axis_map', {}).get('E', 3)
        # Klipper's coordinate rules resolve the requested physical E delta.
        # The accepted endpoint is read again after the original handler returns.
        try:
            requested_e = float(gcmd.get_command_parameters()['E']) * move.extrude_factor
        except (ValueError, TypeError):
            return self.original(gcmd)
        if move.absolute_coord and move.absolute_extrude:
            requested_e += move.base_position[e_index] - before[e_index]
        if not math.isfinite(requested_e) or requested_e == 0:
            return self.original(gcmd)
        extruder = mmu.printer.lookup_object('toolhead').get_extruder()
        extruder_name = extruder.get_name()
        speed = move.speed
        try:
            if 'F' in gcmd.get_command_parameters():
                speed = float(gcmd.get_command_parameters()['F']) * move.speed_factor
        except (ValueError, TypeError):
            pass  # Let the original handler report malformed commands.
        self.handling.add(owner)
        try:
            with telemetry.operation() as operation:
                operation.start(source='mmu_gcode', label=gcmd.get_commandline(),
                    command='G1', context=list(self.owners[owner]),
                    unit=mmu.mmu_unit().name, gate=mmu.gate_selected,
                    motor_mode='extruder', extruder=extruder_name, motors=[extruder_name],
                    requested_mm=requested_e, speed_mm_s=speed if math.isfinite(speed) else None,
                    acceleration_mm_s2=None, position_mm=before[e_index],
                    position_reference='gcode_extruder', homing=False, endstop=None)
                result = self.original(gcmd)
                after = list(move.last_position)
                delta = after[e_index] - before[e_index]
                # Mixed XYZ/E feed rate belongs to the coordinated path. Keep XYZ
                # out of telemetry, but use it to estimate extrusion pacing.
                xyz = math.sqrt(sum((after[i]-before[i])**2 for i in range(3)))
                travel = xyz if xyz else abs(delta)
                seconds = travel / move.speed if move.speed > 0 else None
                operation.finish(status='submitted', actual_mm=delta,
                    position_mm=after[e_index], speed_mm_s=move.speed,
                    extrusion_speed_mm_s=abs(delta)/seconds if seconds else 0.,
                    estimated_duration_s=seconds)
                return result
        finally:
            self.handling.remove(owner)
