# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Record selector stepper and servo commands without modeling filament travel.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import contextlib


@contextlib.contextmanager
def selector_operation(owner, actuator, **fields):
    telemetry = owner.mmu.movement_telemetry
    outcome = dict(status='submitted')
    if not telemetry.listeners:
        yield outcome
        return
    telemetry.move_id += 1
    move_id = telemetry.move_id
    identity = dict(unit=owner.mmu_unit.name, selector=owner.mmu_unit.selector_type,
                    actuator=actuator, gate=owner.mmu.gate_selected)
    start = telemetry.emit('selector_movement_start', move_id, **identity, **fields)
    try:
        yield outcome
    except BaseException as exc:
        outcome.update(status='error', error=str(exc))
        raise
    finally:
        telemetry.emit('selector_movement_outcome', move_id, **identity,
                       elapsed_s=max(0., telemetry.reactor.monotonic()-start['timestamp']), **outcome)


def selector_move(owner, trace_str, pos, new_pos, speed, accel, wait, homing_move=0, endstop_name=None):
    """Telemetry scope for one selector stepper move (plain, or homing against an endstop)"""
    return selector_operation(owner, 'stepper', label=trace_str or 'Move selector',
            position_mm=pos, target_position_mm=new_pos, requested_mm=new_pos-pos,
            speed_mm_s=speed, acceleration_mm_s2=accel, wait=wait,
            homing=bool(homing_move), endstop=(endstop_name or 'default') if homing_move else None,
            trigger_state=(homing_move > 0) if homing_move else None)


def finish_selector_move(owner, outcome, actual, homed, homing_move, wait):
    outcome.update(position_mm=owner.selector_stepper.commanded_pos, actual_mm=actual,
                   homed=homed if homing_move else None,
                   status=('completed' if homed else 'not_homed') if homing_move else 'completed' if wait else 'submitted')


def home_selector_rail(owner):
    stepper = owner.selector_stepper
    if not owner.mmu.movement_telemetry.listeners:
        return stepper.do_home_rail()
    # Rail homing rebases coordinates, so the net trigger travel differs from the final commanded position
    with selector_operation(owner, 'stepper', label='Home selector rail', homing=True, endstop='default',
            trigger_state=True, target_position_mm=stepper.rail.get_homing_info().position_endstop) as outcome:
        traveled = stepper.do_home_rail()
        outcome.update(status='completed', homed=True, position_mm=stepper.commanded_pos)
        return traveled


def move_selector_servo(owner, **command):
    angle = command.get('angle')
    with selector_operation(owner, 'servo', label='Selector servo off' if command.get('width') == 0 else 'Move selector servo',
            position_angle_deg=getattr(owner, '_telemetry_servo_angle', None),
            target_angle_deg=angle, pulse_width_s=command.get('width'), duration_s=command.get('duration')) as outcome:
        result = owner.servo.set_position(**command)
        owner._telemetry_servo_angle = angle
        outcome.update(position_angle_deg=angle, enabled=command.get('width') != 0)
        return result
