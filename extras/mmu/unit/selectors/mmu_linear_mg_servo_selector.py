# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implementation of LinearMultiGearServoSelector:
#  Implements Linear Selector for type-C MMU's with multiple gear steppers:
#   - Uses gear driver stepper per-gate
#   - Uses selector stepper for gate selection with endstop
#   - Servo controlled filament gripping
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

from .mmu_linear_servo_selector import LinearServoSelector


class LinearMultiGearServoSelector(LinearServoSelector):
    """
    Linear selector for type-C MMUs with one gear stepper per gate and a servo
    for filament gripping.

    Gate-specific drives are resolved by MmuUnit/MmuController. The selector
    remains a physical LinearServoSelector and therefore retains normal homing
    and persisted-position semantics.
    """

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
