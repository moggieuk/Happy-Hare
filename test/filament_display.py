# Happy Hare MMU Software
#
# Calls the REAL MmuController.get_filament_position_string()
# (extras/mmu/mmu_controller.py) against a duck-typed stand-in for `self`,
# so the filament-position status line can be exercised in bulk -- every
# sensor/position/gate_homing_endstop combination in milliseconds, no
# reactor or greenlets -- without maintaining a hand-ported copy of the
# method's logic. Used by filament_display_review.py (`make filament_display`
# -- not part of `make test`, see that file for why).
#
# get_filament_position_string() is a plain instance method (no
# isinstance/super/decorator tricks), so any object exposing the same
# attribute/method names it touches can stand in for `self`. FilamentDisplayState
# below is that stand-in; _RealMmuController adapts it to the real names.
#
# Importing extras.mmu.mmu_controller at all still requires the fake klippy
# tree (test/hh) -- mmu_filament_movement.py does `import mcu` and
# `from ..homing import HomingMove`, real Klipper modules -- so this module
# depends on test/hh purely to make that import succeed. No printer is
# booted; test.hh.install() only symlinks a fake klippy/ layout onto
# sys.path once, then the real MmuController class is used exactly as
# extras/mmu itself would use it.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os, sys
from dataclasses import dataclass, field
from typing import Dict, Optional

if __package__ in (None, ''):                       # allow `python test/filament_display.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.hh import install as _install_fake_klippy  # noqa: E402
_install_fake_klippy()

from extras.mmu.mmu_controller import MmuController  # noqa: E402
from extras.mmu.mmu_constants import (                # noqa: E402
    FILAMENT_POS_UNKNOWN, FILAMENT_POS_UNLOADED, FILAMENT_POS_HOMED_GATE, FILAMENT_POS_START_BOWDEN,
    FILAMENT_POS_IN_BOWDEN, FILAMENT_POS_END_BOWDEN, FILAMENT_POS_HOMED_ENTRY, FILAMENT_POS_HOMED_EXTRUDER,
    FILAMENT_POS_EXTRUDER_ENTRY, FILAMENT_POS_HOMED_TS, FILAMENT_POS_IN_EXTRUDER, FILAMENT_POS_LOADED,
    DIRECTION_UNKNOWN, SENSOR_ENCODER, GATE_ENDSTOPS, GATE_AVAILABLE,
)

# All the FILAMENT_POS_* values, in position order, for combination generators
FILAMENT_POSITIONS = [
    FILAMENT_POS_UNKNOWN, FILAMENT_POS_UNLOADED, FILAMENT_POS_HOMED_GATE, FILAMENT_POS_START_BOWDEN,
    FILAMENT_POS_IN_BOWDEN, FILAMENT_POS_END_BOWDEN, FILAMENT_POS_HOMED_ENTRY, FILAMENT_POS_HOMED_EXTRUDER,
    FILAMENT_POS_EXTRUDER_ENTRY, FILAMENT_POS_HOMED_TS, FILAMENT_POS_IN_EXTRUDER, FILAMENT_POS_LOADED,
]

# Gate homing endstop choices (mirrors GATE HOMING param spec choices in
# extras/mmu/unit/mmu_unit_parameters.py). gate_preload_endstop additionally
# allows '' meaning "inherit gate_homing_endstop".
GATE_HOMING_ENDSTOPS = list(GATE_ENDSTOPS)
GATE_PRELOAD_ENDSTOPS = list(GATE_ENDSTOPS) + ['']


@dataclass
class FilamentDisplayState:
    """
    Plain-data stand-in for everything get_filament_position_string() reads
    off `self` (MmuController), passed through _RealMmuController. Sensors
    are modelled as a single dict rather than separate check_sensor()/
    has_sensor() calls into a live MmuSensorManager: a sensor absent from
    the dict (or mapped to None) is "not present", any bool value is
    "present, triggered=value".
    """
    # Console formatting flags (console_show_bold_filament / console_show_filament_color)
    bold: bool = False
    color: bool = False

    # Selection / position / direction
    tool: int = 0
    gate: int = 0
    pos: int = FILAMENT_POS_UNLOADED
    direction: int = DIRECTION_UNKNOWN

    # Per-unit gate endstop config
    gate_homing_endstop: str = SENSOR_ENCODER
    gate_preload_endstop: str = ''  # NOTE: not currently read by get_filament_position_string()

    # Whether this gate has a spool in it at all (GATE_EMPTY/GATE_AVAILABLE/
    # GATE_UNKNOWN, mmu_constants.py) -- tracked per-gate, independent of pos
    gate_status: int = GATE_AVAILABLE

    sensors: Dict[str, Optional[bool]] = field(default_factory=dict)

    # Encoder / buffer / sync-feedback
    has_encoder: bool = False
    encoder_move_validation: bool = False
    encoder_distance: float = 0.0
    has_buffer: bool = False
    sync_feedback_state: str = "unavailable"
    sync_feedback_bias_modelled: Optional[float] = None

    # Residual filament (hotend) / gate spool color
    filament_remaining: float = 0.0
    filament_remaining_color: str = ""
    gate_color: str = ""

    # Drive (stepper) position, mm
    filament_position: float = 0.0

    def check_sensor(self, name):
        return self.sensors.get(name)

    def has_sensor(self, name):
        return self.sensors.get(name) is not None


class _ConstantIndex:
    """`self.gate_status[gate]` / `self.gate_color[gate]` only ever need one
    gate's value here, regardless of the index the real method indexes with."""
    def __init__(self, value):
        self._value = value

    def __getitem__(self, index):
        return self._value


class _RealMmuController:
    """Duck-typed `self` for MmuController.get_filament_position_string(),
    backed by a FilamentDisplayState -- exposes exactly the attribute/method
    names that method touches, nothing more."""

    def __init__(self, state):
        self._state = state
        self.p = _ConsoleParams(state)
        self.tool_selected = state.tool
        self.gate_selected = state.gate
        self.filament_pos = state.pos
        self.filament_direction = state.direction
        self.gate_status = _ConstantIndex(state.gate_status)
        self.gate_color = _ConstantIndex(state.gate_color)
        self.sensor_manager = _SensorManagerShim(state)

    def mmu_unit(self):
        return _MmuUnitShim(self._state)

    def get_status(self, flags):
        return {
            'filament_remaining': self._state.filament_remaining,
            'filament_remaining_color': self._state.filament_remaining_color,
            'sync_feedback_state': self._state.sync_feedback_state,
            'sync_feedback_bias_modelled': self._state.sync_feedback_bias_modelled,
        }

    def has_encoder(self):
        return self._state.has_encoder

    def get_encoder_distance(self, dwell=None):
        return self._state.encoder_distance

    def drive(self):
        return _DriveShim(self._state)


class _ConsoleParams:
    def __init__(self, state):
        self.console_show_bold_filament = state.bold
        self.console_show_filament_color = state.color


class _MmuUnitShim:
    def __init__(self, state):
        self._state = state
        self.p = _UnitParams(state)

    def has_buffer(self):
        return self._state.has_buffer


class _UnitParams:
    def __init__(self, state):
        self.gate_homing_endstop = state.gate_homing_endstop
        self.encoder_move_validation = state.encoder_move_validation


class _SensorManagerShim:
    def __init__(self, state):
        self._state = state

    def check_sensor(self, name):
        return self._state.check_sensor(name)

    def has_sensor(self, name):
        return self._state.has_sensor(name)


class _DriveShim:
    def __init__(self, state):
        self._state = state

    def get_filament_position(self):
        return self._state.filament_position


def get_filament_position_string(state):
    return MmuController.get_filament_position_string(_RealMmuController(state))


def strip_color_markup(visual):
    """Strip the {0}..{6} / {{RRGGBB}} / {{}} tokens that MmuLogger._color_message()
    (extras/mmu/mmu_logger.py) would otherwise resolve, leaving the plain glyphs."""
    import re
    return re.sub(r'\{\{[0-9A-Fa-f]*\}\}|\{[0-9]\}', '', visual)
