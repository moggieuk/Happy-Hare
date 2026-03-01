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

from .mmu_base_selectors  import VirtualSelector
from .mmu_linear_selector import LinearSelector


class LinearMultiGearSelector(LinearSelector, VirtualSelector):
    """
    Linear selector for type-C MMUs with one gear stepper per gate.

    Combines VirtualSelector (for selecting the per-gate gear driver) with
    LinearSelector (for moving the physical selector mechanism with an endstop).
    The MRO ensures gear selection occurs before selector movement when using
    super() in select_gate().
    """

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
