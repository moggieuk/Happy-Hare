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


def home_selector_rail(owner, endstop_name=None):
    stepper = owner.selector_stepper
    command = {} if endstop_name is None else dict(endstop_name=endstop_name)
    if not owner.mmu.movement_telemetry.listeners:
        return stepper.do_home_rail(**command)
    hi = stepper.rail.get_homing_info()
    # Rail homing rebases coordinates and may retract/rehome. Its net trigger
    # travel is separate from the final commanded coordinate.
    with selector_operation(owner, 'stepper', label='Home selector rail', homing=True,
            rail_homing=True, endstop=endstop_name or 'default', trigger_state=True,
            target_position_mm=hi.position_endstop, speed_mm_s=hi.speed,
            retract_mm=hi.retract_dist, retract_speed_mm_s=hi.retract_speed,
            second_homing_speed_mm_s=hi.second_homing_speed,
            position_reference='homing_rebased') as outcome:
        traveled = stepper.do_home_rail(**command)
        outcome.update(status='completed', homed=True, trigger_travel_mm=traveled,
                       position_mm=stepper.commanded_pos)
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
