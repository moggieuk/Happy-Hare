# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements composable MMU step commands:
#   MMU_STEP_LOAD_GATE
#   MMU_STEP_UNLOAD_GATE
#   MMU_STEP_LOAD_BOWDEN
#   MMU_STEP_UNLOAD_BOWDEN
#   MMU_STEP_HOME_EXTRUDER
#   MMU_STEP_LOAD_TOOLHEAD
#   MMU_STEP_UNLOAD_TOOLHEAD
#   MMU_STEP_HOMING_MOVE
#   MMU_STEP_MOVE
#   MMU_STEP_SET_FILAMENT
#   MMU_STEP_SET_ACTION
#   MMU_M400
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


class MmuStepLoadGateCommand(BaseCommand):

    CMD = "_MMU_STEP_LOAD_GATE"
    HELP_BRIEF = "User composable loading step: Move filament from gate to start of bowden"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu._load_gate()
        except MmuError as ee:
            self.mmu.handle_mmu_error("_MMU_STEP_LOAD_GATE: %s" % str(ee))


class MmuStepUnloadGateCommand(BaseCommand):

    CMD = "_MMU_STEP_UNLOAD_GATE"
    HELP_BRIEF = "User composable unloading step: Move filament from start of bowden and park in the gate"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "FULL   = [0|1]\n"
    )
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        full = bool(gcmd.get_int('FULL', 0, minval=0, maxval=1))
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu._unload_gate(
                    homing_max=self.mmu.mmu_unit().calibrator.get_bowden_length() if full else None
                )
        except MmuError as ee:
            self.mmu.handle_mmu_error("_MMU_STEP_UNLOAD_GATE: %s" % str(ee))


class MmuStepLoadBowdenCommand(BaseCommand):

    CMD = "_MMU_STEP_LOAD_BOWDEN"
    HELP_BRIEF = "User composable loading step: Smart loading of bowden"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        length = gcmd.get_float('LENGTH', None, minval=0.)
        start_pos = gcmd.get_float('START_POS', 0.)
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu._load_bowden(length, start_pos=start_pos)
        except MmuError as ee:
            self.mmu.handle_mmu_error("_MMU_STEP_LOAD_BOWDEN: %s" % str(ee))


class MmuStepUnloadBowdenCommand(BaseCommand):

    CMD = "_MMU_STEP_UNLOAD_BOWDEN"
    HELP_BRIEF = "User composable unloading step: Smart unloading of bowden"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        length = gcmd.get_float('LENGTH', self.mmu._get_bowden_length(self.mmu.gate_selected))
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu._unload_bowden(length)
        except MmuError as ee:
            self.mmu.handle_mmu_error("_MMU_STEP_UNLOAD_BOWDEN: %s" % str(ee))


class MmuStepHomeExtruderCommand(BaseCommand):

    CMD = "_MMU_STEP_HOME_EXTRUDER"
    HELP_BRIEF = "User composable loading step: Home to extruder sensor or entrance through collision detection"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu._home_to_extruder(self.mmu.mmu_unit().p.extruder_homing_max)
        except MmuError as ee:
            self.mmu.handle_mmu_error("_MMU_STEP_HOME_EXTRUDER: %s" % str(ee))


class MmuStepLoadToolheadCommand(BaseCommand):

    CMD = "_MMU_STEP_LOAD_TOOLHEAD"
    HELP_BRIEF = "User composable loading step: Toolhead loading"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        extruder_only = gcmd.get_int('EXTRUDER_ONLY', 0)
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu._load_extruder(extruder_only)
        except MmuError as ee:
            self.mmu.handle_mmu_error("_MMU_STEP_LOAD_TOOLHEAD: %s" % str(ee))


class MmuStepUnloadToolheadCommand(BaseCommand):

    CMD = "_MMU_STEP_UNLOAD_TOOLHEAD"
    HELP_BRIEF = "User composable unloading step: Toolhead unloading"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0))
        park_pos = gcmd.get_float('PARK_POS', -self.mmu._get_filament_position())
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                park_pos = min(
                    self.mmu.p.toolhead_extruder_to_nozzle,
                    max(0, park_pos)
                )
                self.mmu._set_filament_position(-park_pos)
                self.mmu._unload_extruder(extruder_only=extruder_only)
        except MmuError as ee:
            self.mmu.handle_mmu_error("_MMU_STEP_UNLOAD_TOOLHEAD: %s" % str(ee))


class MmuStepHomingMoveCommand(BaseCommand):

    CMD = "_MMU_STEP_HOMING_MOVE"
    HELP_BRIEF = "User composable loading step: Generic homing move"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        allow_bypass = bool(gcmd.get_int('ALLOW_BYPASS', 0, minval=0, maxval=1))
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu._homing_move_cmd(
                    gcmd,
                    "User defined step homing move",
                    allow_bypass=allow_bypass
                )
        except MmuError as ee:
            self.mmu.handle_mmu_error("_MMU_STEP_HOMING_MOVE: %s" % str(ee))


class MmuStepMoveCommand(BaseCommand):

    CMD = "_MMU_STEP_MOVE"
    HELP_BRIEF = "User composable loading step: Generic move"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        allow_bypass = bool(gcmd.get_int('ALLOW_BYPASS', 0, minval=0, maxval=1))
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu._move_cmd(
                    gcmd,
                    "User defined step move",
                    allow_bypass=allow_bypass
                )
        except MmuError as ee:
            self.mmu.handle_mmu_error("_MMU_STEP_MOVE: %s" % str(ee))


class MmuStepSetFilamentCommand(BaseCommand):

    CMD = "_MMU_STEP_SET_FILAMENT"
    HELP_BRIEF = "User composable loading step: Set filament position state"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        state = gcmd.get_int('STATE', minval=FILAMENT_POS_UNKNOWN, maxval=FILAMENT_POS_LOADED)
        silent = gcmd.get_int('SILENT', 0)
        self.mmu._set_filament_pos_state(state, silent)


class MmuStepSetActionCommand(BaseCommand):

    CMD = "_MMU_STEP_SET_ACTION"
    HELP_BRIEF = "User composable loading step: Set action state"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        if gcmd.get_int('RESTORE', 0):
            if self.mmu._old_action is not None:
                self.mmu._set_action(self.mmu._old_action)
            self.mmu._old_action = None
        else:
            state = gcmd.get_int('STATE', minval=ACTION_IDLE, maxval=ACTION_PURGING)
            if self.mmu._old_action is None:
                self.mmu._old_action = self.mmu._set_action(state)
            else:
                self.mmu._set_action(state)


class MmuM400Command(BaseCommand):

    CMD = "_MMU_M400"
    HELP_BRIEF = "Wait on both move queues"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT)

    def _run(self, gcmd):
        self.mmu.mmu_toolhead().quiesce()
