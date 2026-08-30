# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implementation of LinearMultiGearSelector:
#  Implements Linear Selector for type-C MMU's with multiple gear steppers:
#   - Uses gear driver stepper per-gate
#   - Uses selector stepper for gate selection with endstop
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

from .mmu_linear_selector import LinearSelector


class LinearMultiGearSelector(LinearSelector):
    """
    Linear selector for type-C MMUs with one gear stepper per gate.

    Gate-specific drives are resolved by MmuUnit/MmuController. The selector
    remains a physical LinearSelector and therefore must home normally before
    making absolute-position moves.
    """

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
