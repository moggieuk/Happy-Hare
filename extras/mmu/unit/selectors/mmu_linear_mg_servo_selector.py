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

from .mmu_base_selectors        import VirtualSelector
from .mmu_linear_servo_selector import LinearServoSelector


class LinearMultiGearServoSelector(LinearServoSelector, VirtualSelector):
    """
    Linear selector for type-C MMUs with one gear stepper per gate and a servo
    for filament gripping.

    Combines VirtualSelector (for selecting the per-gate gear driver) with
    LinearServoSelector (for endstop-based selector movement plus servo grip/
    release). The MRO ensures gear selection occurs before selector movement
    when using super() in select_gate().
    """

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
