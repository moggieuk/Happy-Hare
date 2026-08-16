# Happy Hare MMU Software
#
# Bulk sensor/position/gate_homing_endstop sweep for
# MmuController.get_filament_position_string() (extras/mmu/mmu_controller.py),
# via filament_display.py's duck-typed adapter -- the real method runs every
# time, nothing here is a copy of its logic.
#
# This repo's discovery pattern is '*', so `make test` would otherwise sweep this
# in and print its whole render matrix as test output; the load_tests() hook near
# the bottom keeps it out except via the dedicated target below. It's a manual/
# visual review aid, not an assertion-heavy correctness suite -- most methods just
# render every combination in a matrix and print it so a human can eyeball the
# result. Run it on demand instead:
#
#   make filament_display
#   make filament_display ARGS='-k UNKNOWN'
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os, sys, unittest
import itertools

if __package__ in (None, ''):                       # allow `python test/filament_display_review.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.filament_display import (                 # noqa: E402
    FilamentDisplayState, get_filament_position_string, strip_color_markup,
    FILAMENT_POSITIONS, GATE_HOMING_ENDSTOPS, GATE_PRELOAD_ENDSTOPS,
)
from extras.mmu.mmu_constants import (
    FILAMENT_POS_UNKNOWN, FILAMENT_POS_UNLOADED, FILAMENT_POS_HOMED_GATE, FILAMENT_POS_START_BOWDEN,
    FILAMENT_POS_IN_BOWDEN, FILAMENT_POS_END_BOWDEN, FILAMENT_POS_HOMED_ENTRY, FILAMENT_POS_HOMED_EXTRUDER,
    FILAMENT_POS_EXTRUDER_ENTRY, FILAMENT_POS_HOMED_TS, FILAMENT_POS_IN_EXTRUDER, FILAMENT_POS_LOADED,
    DIRECTION_LOAD, DIRECTION_UNLOAD, DIRECTION_UNKNOWN, TOOL_GATE_BYPASS,
    SENSOR_ENCODER, SENSOR_EXTRUDER_ENTRY, SENSOR_TOOLHEAD, SENSOR_TENSION, SENSOR_COMPRESSION,
    SENSOR_PROPORTIONAL, SENSOR_ENTRY_PREFIX, SENSOR_EXIT_PREFIX, SENSOR_SHARED_EXIT,
    GATE_EMPTY, GATE_AVAILABLE, UI_SENSOR_TRIGGERED, UI_SENSOR_EMPTY,
)

POS_NAMES = {
    FILAMENT_POS_UNKNOWN:         "UNKNOWN",
    FILAMENT_POS_UNLOADED:        "UNLOADED",
    FILAMENT_POS_HOMED_GATE:      "HOMED_GATE",
    FILAMENT_POS_START_BOWDEN:    "START_BOWDEN",
    FILAMENT_POS_IN_BOWDEN:       "IN_BOWDEN",
    FILAMENT_POS_END_BOWDEN:      "END_BOWDEN",
    FILAMENT_POS_HOMED_ENTRY:     "HOMED_ENTRY",
    FILAMENT_POS_HOMED_EXTRUDER:  "HOMED_EXTRUDER",
    FILAMENT_POS_EXTRUDER_ENTRY:  "EXTRUDER_ENTRY",
    FILAMENT_POS_HOMED_TS:        "HOMED_TS",
    FILAMENT_POS_IN_EXTRUDER:     "IN_EXTRUDER",
    FILAMENT_POS_LOADED:          "LOADED",
}

# Named sensor availability/state profiles applied on top of whatever gate
# endstop sensor a given combination needs (see build_sensors()).
# None = sensor not fitted, True/False = fitted and triggered/clear.
SENSOR_PROFILES = {
    "no_optional_sensors":     {SENSOR_EXTRUDER_ENTRY: None,  SENSOR_TOOLHEAD: None},
    "optional_sensors_clear":  {SENSOR_EXTRUDER_ENTRY: False, SENSOR_TOOLHEAD: False},
    "optional_sensors_triggered": {SENSOR_EXTRUDER_ENTRY: True, SENSOR_TOOLHEAD: True},
}


def build_sensors(gate_homing_endstop, profile, gate_triggered=True, entry=None, exit_sensor=None, shared_exit=None):
    """
    Compose a sensors dict for FilamentDisplayState: start from a named
    SENSOR_PROFILES entry, then fit (or not) the sensor backing the current
    gate_homing_endstop. SENSOR_ENCODER is a movement-based fake endstop with
    no physical runout sensor, so it never gets a sensors-dict entry.

    entry/exit_sensor/shared_exit (None = not fitted, True/False = fitted and
    triggered/clear) let a caller independently fit the gate-area sensors that
    gate_area_segment() now renders, on top of/overriding whatever
    gate_homing_endstop already implied.
    """
    sensors = dict(SENSOR_PROFILES[profile])
    if gate_homing_endstop != SENSOR_ENCODER:
        sensors[gate_homing_endstop] = gate_triggered
    if entry is not None:
        sensors[SENSOR_ENTRY_PREFIX] = entry
    if exit_sensor is not None:
        sensors[SENSOR_EXIT_PREFIX] = exit_sensor
    if shared_exit is not None:
        sensors[SENSOR_SHARED_EXIT] = shared_exit
    return sensors


# console_show_bold_filament: "thin" uses the light line/arrow/home glyphs
# (UI_LINE_LIGHT / UI_ARROW_FILLED_RIGHT / UI_HOME_LIGHT), "thick" uses the
# bold solid-square set (UI_SOLID_SQUARE / UI_HOME_BOLD). Every batch below
# repeats once per style so both render styles can be eyeballed side by side.
BOLD_STYLES = (("thin", False), ("thick", True))


def style_header(style_name, is_bold):
    print(f"\n=== {style_name} filament line (bold={is_bold}) ===")


# Fixed label column width used everywhere below so every printed status line
# lines up, regardless of which test method produced it. unittest's own
# verbose "test_name (...) ... " progress text has no trailing newline, so
# each test method must print() a leading blank line before its first
# render() call -- otherwise that first line gets shifted right, out of
# alignment with the rest. Must be >= the longest label actually produced
# below (currently "[shared_exit fitted, triggered, START_BOWDEN]" at 45
# chars) -- widen this if a longer label gets added rather than shortening
# the label, so the column keeps a little breathing room either side.
LABEL_WIDTH = 48


def render(label, state, verbose=True):
    visual = strip_color_markup(get_filament_position_string(state))
    if verbose:
        print(f"{label:<{LABEL_WIDTH}} {visual}")
    return visual


SYNC_FEEDBACK_STATES = ["unavailable", "disabled", "inactive", "compressed", "tension", "neutral"]


def render_buffer_batch(is_bold):
    """Sync-feedback-buffer displays: tension/compression combos x sync_feedback_state,
    plus the proportional-sensor numeric-bias variant of the neutral state."""
    for sf_state in SYNC_FEEDBACK_STATES:
        for c_sensor, t_sensor in ((False, False), (True, False), (False, True)):
            sensors = build_sensors(SENSOR_ENCODER, "no_optional_sensors")
            sensors[SENSOR_COMPRESSION] = c_sensor
            sensors[SENSOR_TENSION] = t_sensor
            state = FilamentDisplayState(
                pos=FILAMENT_POS_IN_BOWDEN,
                bold=is_bold,
                gate_homing_endstop=SENSOR_ENCODER,
                sensors=sensors,
                has_encoder=True,
                encoder_move_validation=True,
                encoder_distance=1234.5,
                has_buffer=True,
                sync_feedback_state=sf_state,
                filament_position=1234.5,
            )
            render(f"[sf={sf_state} C={c_sensor} T={t_sensor}]", state)

    # Proportional sensor takes over the neutral-state slot with a numeric bias
    sensors = build_sensors(SENSOR_ENCODER, "no_optional_sensors")
    sensors[SENSOR_PROPORTIONAL] = True
    for bias in (-1.0, -0.3, 0.0, 0.3, 1.0):
        state = FilamentDisplayState(
            pos=FILAMENT_POS_IN_BOWDEN,
            bold=is_bold,
            gate_homing_endstop=SENSOR_ENCODER,
            sensors=sensors,
            has_encoder=True,
            encoder_move_validation=True,
            encoder_distance=1234.5,
            has_buffer=True,
            sync_feedback_state="neutral",
            sync_feedback_bias_modelled=bias,
            filament_position=1234.5,
        )
        render(f"[sf=neutral proportional bias={bias:+.1f}]", state)


# Physical order of the 4 gate-area endstop choices, gate-ward to
# extruder-ward -- mirrors GATE_SENSOR_ORDER in filament_display.py, and is
# used the same way: whether a sensor's location has been passed once we've
# reached FILAMENT_POS_HOMED_GATE via a *different*, further-along endstop.
GATE_SENSOR_ORDER = (SENSOR_EXIT_PREFIX, SENSOR_SHARED_EXIT, SENSOR_ENCODER, SENSOR_EXTRUDER_ENTRY)

# Sensors relevant to depth-of-parking at FILAMENT_POS_UNLOADED, in physical
# (gate-ward-most-first) order
PARKING_ORDER = (SENSOR_ENTRY_PREFIX, SENSOR_EXIT_PREFIX, SENSOR_SHARED_EXIT)
SENSOR_SHORT_NAME = {
    SENSOR_ENTRY_PREFIX: "entry",
    SENSOR_EXIT_PREFIX: "exit",
    SENSOR_SHARED_EXIT: "shared_exit",
    SENSOR_EXTRUDER_ENTRY: "extruder",
    SENSOR_TOOLHEAD: "toolhead",
}

# All 5 real, independently-fittable gate-area sensors (encoder isn't a
# "sensors dict" entry -- it has no physical trigger, see
# UI_ENCODER_VIRTUAL_TRIGGER in filament_display.py -- its presence is the
# has_encoder hardware flag instead)
ALL_FITTED = frozenset((SENSOR_ENTRY_PREFIX, SENSOR_EXIT_PREFIX, SENSOR_SHARED_EXIT, SENSOR_EXTRUDER_ENTRY))

SENSOR_COMBO_NAMES = ("entry", "exit", "shared_exit", "encoder", "extruder", "toolhead")
SENSOR_COMBO_TO_CONSTANT = {
    "entry": SENSOR_ENTRY_PREFIX,
    "exit": SENSOR_EXIT_PREFIX,
    "shared_exit": SENSOR_SHARED_EXIT,
    "extruder": SENSOR_EXTRUDER_ENTRY,
    "toolhead": SENSOR_TOOLHEAD,
}

# Every combination of the 6 sensors, none fitted through all fitted (2**6 = 64),
# grouped by size then in SENSOR_COMBO_NAMES order within each size
SENSOR_COMBOS = tuple(
    itertools.chain.from_iterable(
        itertools.combinations(SENSOR_COMBO_NAMES, r) for r in range(len(SENSOR_COMBO_NAMES) + 1)
    )
)


def _combo_config(combo):
    """
    For a given fitted-sensor combo (a tuple of names from SENSOR_COMBO_NAMES):
    has_encoder is simply whether "encoder" is in the combo; the real,
    independently-checkable sensors (entry/exit/shared_exit/extruder/toolhead)
    map straight to their FilamentDisplayState.sensors keys.
    """
    has_encoder = "encoder" in combo
    fitted = frozenset(SENSOR_COMBO_TO_CONSTANT[name] for name in combo if name in SENSOR_COMBO_TO_CONSTANT)
    return has_encoder, fitted


# The 4 gate_homing_endstop choices (mirrors GATE_ENDSTOPS in filament_display.py)
GATE_ENDSTOP_NAME_TO_CONSTANT = {
    "exit": SENSOR_EXIT_PREFIX,
    "shared_exit": SENSOR_SHARED_EXIT,
    "encoder": SENSOR_ENCODER,
    "extruder": SENSOR_EXTRUDER_ENTRY,
}


def _valid_gate_endstops(combo):
    """
    gate_homing_endstop can only be set to something that physically exists
    on the machine -- you can't home against a switch you don't have. So the
    only valid choices for a given combo are whichever of exit/shared_exit/
    encoder/extruder are actually in it (not crossed with every choice
    regardless of fitment, and not "entry", which was never a valid
    gate_homing_endstop choice in the first place -- see GATE_ENDSTOPS in
    filament_display.py). A combo with none of the four (e.g. () or
    ("entry",)) has no valid gate_homing_endstop at all -- gate homing simply
    isn't possible on that machine -- so it contributes no rows.
    """
    return [(name, GATE_ENDSTOP_NAME_TO_CONSTANT[name]) for name in combo if name in GATE_ENDSTOP_NAME_TO_CONSTANT]


def _gate_sensor_triggered(sensor, pos, gate_endstop):
    """Has the tip reached/passed sensor's location, given the active gate_homing_endstop?
    (FILAMENT_POS_UNLOADED is handled by _parking_subrows instead, not here.)"""
    if pos > FILAMENT_POS_HOMED_GATE:
        return True
    if pos == FILAMENT_POS_HOMED_GATE:
        return GATE_SENSOR_ORDER.index(gate_endstop) >= GATE_SENSOR_ORDER.index(sensor)
    return False


def _derive_sensors(pos, gate_endstop, fitted):
    """
    Only ever emits a state for a sensor in `fitted` (True/False) -- a sensor
    left out of `fitted` is entirely absent (None via FilamentDisplayState's
    own has_sensor()), never independently toggled. For every fitted sensor,
    the trigger state is derived from `pos` (and `gate_endstop` for exit/
    shared_exit) so each row is a physically real machine state, not an
    arbitrary/impossible combination.
    """
    sensors = {}
    if SENSOR_ENTRY_PREFIX in fitted:
        sensors[SENSOR_ENTRY_PREFIX] = pos > FILAMENT_POS_UNLOADED
    if SENSOR_EXIT_PREFIX in fitted:
        sensors[SENSOR_EXIT_PREFIX] = _gate_sensor_triggered(SENSOR_EXIT_PREFIX, pos, gate_endstop)
    if SENSOR_SHARED_EXIT in fitted:
        sensors[SENSOR_SHARED_EXIT] = _gate_sensor_triggered(SENSOR_SHARED_EXIT, pos, gate_endstop)
    if SENSOR_EXTRUDER_ENTRY in fitted:
        threshold = FILAMENT_POS_HOMED_GATE if gate_endstop == SENSOR_EXTRUDER_ENTRY else FILAMENT_POS_HOMED_ENTRY
        sensors[SENSOR_EXTRUDER_ENTRY] = pos >= threshold
    if SENSOR_TOOLHEAD in fitted:
        sensors[SENSOR_TOOLHEAD] = pos >= FILAMENT_POS_HOMED_TS
    return sensors


def _position_walk(gate_endstop):
    """
    (label, pos) pairs to render after the UNLOADED parking sub-rows, in order.

    Special case: gate_endstop == SENSOR_EXTRUDER_ENTRY homes the gate using
    the very same physical sensor that later (independently) confirms the
    extruder entry, so reaching FILAMENT_POS_HOMED_GATE via this endstop
    means the tip is already all the way at the extruder, not merely
    "somewhere past the gate". Rather than hand-crafting a "gate homed but
    really at the extruder" state, the "HOMED_GATE" row here renders at
    FILAMENT_POS_HOMED_EXTRUDER instead -- exactly the HOMED_EXTRUDER row's
    own rendering, just reused under the HOMED_GATE label -- and the
    normally-intervening START_BOWDEN/IN_BOWDEN/END_BOWDEN/HOMED_ENTRY rows
    are skipped, since rendering them afterwards would walk backwards from
    where "HOMED_GATE" just landed.
    """
    if gate_endstop == SENSOR_EXTRUDER_ENTRY:
        return [
            ("HOMED_GATE", FILAMENT_POS_HOMED_EXTRUDER),
            ("EXTRUDER_ENTRY", FILAMENT_POS_EXTRUDER_ENTRY),
            ("HOMED_TS", FILAMENT_POS_HOMED_TS),
            ("IN_EXTRUDER", FILAMENT_POS_IN_EXTRUDER),
            ("LOADED", FILAMENT_POS_LOADED),
        ]
    return [
        (POS_NAMES[pos], pos)
        for pos in FILAMENT_POSITIONS
        if pos not in (FILAMENT_POS_UNKNOWN, FILAMENT_POS_UNLOADED)
    ]


def _parking_subrows(fitted):
    """
    Depth-of-parking sub-states at FILAMENT_POS_UNLOADED: a forward-parked
    filament triggers every gate-ward sensor up to however far forward it
    sits and none past that point, so this enumerates "how far forward" --
    not an independent per-sensor toggle (which would emit physically
    impossible combinations, e.g. shared_exit triggered but exit clear).

    "No sensor triggered" is genuinely ambiguous between two different
    gate_status readings, both rendered here: a genuinely empty gate
    (GATE_EMPTY) and filament present but parked before even the first
    relevant sensor (GATE_AVAILABLE) -- a sensor reading alone (e.g. entry
    clear) can't tell those apart, but gate_status is Happy Hare's actual,
    separately-tracked answer to that question. The sensors keep reporting
    their own real state regardless of which one it is (entry stays clear at
    every later depth too, until "past entry"). A combo fitting none of
    entry/exit/shared_exit still gets this same pair of rows ("empty" /
    "parked"), just with no sensor evidence to show either way.

    Any OTHER fitted sensor (extruder_entry, toolhead) is downstream of the
    gate entirely -- at FILAMENT_POS_UNLOADED it's definitely, not just
    presumably, clear -- so every row here marks it False rather than leaving
    it out of the sensors dict (which would read as "not fitted" and fall
    back to the ambiguous ellipsis fill instead of its own real ◯ glyph).
    """
    relevant = [s for s in PARKING_ORDER if s in fitted]
    downstream_clear = {s: False for s in fitted if s not in PARKING_ORDER}
    all_clear = {**{s: False for s in relevant}, **downstream_clear}
    before_label = f"before {SENSOR_SHORT_NAME[relevant[0]]}" if relevant else "parked"
    rows = [("empty", all_clear, GATE_EMPTY), (before_label, all_clear, GATE_AVAILABLE)]
    for depth in range(1, len(relevant) + 1):
        sensors = {**{s: (i < depth) for i, s in enumerate(relevant)}, **downstream_clear}
        rows.append((f"past {SENSOR_SHORT_NAME[relevant[depth - 1]]}", sensors, GATE_AVAILABLE))
    return rows


# Column offsets of entry/exit/shared_exit's marker glyph within the fixed-
# width gate_area_segment() prefix -- stable because every combo in this
# sweep renders with the same tool="[T0] " and has_buffer=False, so the
# segment widths leading up to each marker never vary (see
# gate_area_segment() in filament_display.py: presence(1)+entry(1)+gap(2)+
# exit(1)+gap(2)+shared_exit(1)+gap(2), 10 chars total).
_TOOL_TEXT_LEN = len("[T0] ")
_GATE_AREA_MARKER_OFFSET = {
    SENSOR_ENTRY_PREFIX: _TOOL_TEXT_LEN + 1,
    SENSOR_EXIT_PREFIX: _TOOL_TEXT_LEN + 4,
    SENSOR_SHARED_EXIT: _TOOL_TEXT_LEN + 7,
}


def _assert_sensor_markers(test_case, visual, sensors, label):
    """
    For every sensor with a real (non-None) reading in `sensors`, assert its
    marker glyph (◉/◯) actually appears at its known column in `visual`,
    instead of relying on a human to spot a missing/wrong marker by eye.
    extruder/toolhead sit downstream of variable-width bowden/buffer fill, so
    their column is anchored relative to the literal "Ex"/"Nz" text instead of
    a fixed offset from the start (see optional_sensor()/homed_segment() and
    nozzle_segment() in filament_display.py: 3-char sensor block, marker in
    the middle, immediately before each label).
    """
    for sensor, triggered in sensors.items():
        if triggered is None:
            continue
        if sensor in _GATE_AREA_MARKER_OFFSET:
            index = _GATE_AREA_MARKER_OFFSET[sensor]
        elif sensor == SENSOR_EXTRUDER_ENTRY:
            index = visual.index("Ex") - 3
        elif sensor == SENSOR_TOOLHEAD:
            index = visual.index("Nz") - 5
        else:
            continue
        expected = UI_SENSOR_TRIGGERED if triggered else UI_SENSOR_EMPTY
        test_case.assertEqual(
            visual[index], expected,
            f"{label}: {SENSOR_SHORT_NAME[sensor]} marker wrong at column {index} in {visual!r}"
        )


class TestFilamentDisplayVisualReview(unittest.TestCase):
    """
    Curated, ordered walkthrough for manual visual review (as opposed to the
    brute-force cartesian dumps in the other test classes below): for each
    line style (thin then thick), all 64 combinations of which of {entry,
    exit, shared_exit, encoder, extruder, toolhead} are fitted (SENSOR_COMBOS
    -- the full powerset, none through all six), crossed ONLY with the
    gate_homing_endstop choices that combo can actually have (_valid_gate_endstops
    -- you can't home against a switch that doesn't exist, so "exit" is only
    ever the active endstop in combos that fit it, etc; a combo fitting none
    of exit/shared_exit/encoder/extruder can't home its gate at all and is
    skipped). Within a group, only the fitted sensors' triggered state and
    filament_position vary, and the former is *derived from* the latter (see
    _derive_sensors) so every row is a physically real machine state,
    walking UNKNOWN first, then every filament_position in order with
    UNLOADED expanded into its depth-of-parking sub-rows.
    """

    def _render_combo(self, combo, gate_endstop, is_bold):
        has_encoder, fitted = _combo_config(combo)

        # UNKNOWN first (before boot/recovery has determined a position at
        # all) -- _derive_sensors naturally reads every fitted sensor as
        # clear here since every one of its `pos >=/>` thresholds is above
        # FILAMENT_POS_UNKNOWN, which is itself a physically real state
        # rather than a special case.
        unknown_sensors = _derive_sensors(FILAMENT_POS_UNKNOWN, gate_endstop, fitted)
        state = FilamentDisplayState(
            tool=0, gate=0, pos=FILAMENT_POS_UNKNOWN, bold=is_bold, has_encoder=has_encoder,
            gate_homing_endstop=gate_endstop, sensors=unknown_sensors,
            encoder_move_validation=has_encoder, encoder_distance=1234.5,
            filament_position=1234.5,
        )
        visual = render("[UNKNOWN]", state)
        _assert_sensor_markers(self, visual, unknown_sensors, "UNKNOWN")

        # Same position, opposite extreme: every fitted sensor reads
        # triggered despite the position genuinely being unknown -- a
        # physically odd but not impossible combination (e.g. stale/noisy
        # sensor state on a freshly connected MCU, before anything has
        # actually been homed), and a real stress case for any fill logic
        # that assumes "no known position" implies "no possible progress".
        unknown_all_triggered = {s: True for s in fitted}
        state = FilamentDisplayState(
            tool=0, gate=0, pos=FILAMENT_POS_UNKNOWN, bold=is_bold, has_encoder=has_encoder,
            gate_homing_endstop=gate_endstop, sensors=unknown_all_triggered,
            encoder_move_validation=has_encoder, encoder_distance=1234.5,
            filament_position=1234.5,
        )
        visual = render("[UNKNOWN, all triggered]", state)
        _assert_sensor_markers(self, visual, unknown_all_triggered, "UNKNOWN, all triggered")

        # Fabricated, distinctly-not-zero mm values -- 0.0 reads as "nothing
        # happened yet" and, more importantly, silently hid the "(e:...)"
        # encoder-distance suffix (needs encoder_move_validation=True too,
        # which a bare has_encoder=True doesn't imply).
        for sub_label, sensors, gate_status in _parking_subrows(fitted):
            state = FilamentDisplayState(
                tool=0, gate=0, pos=FILAMENT_POS_UNLOADED, bold=is_bold, has_encoder=has_encoder,
                gate_homing_endstop=gate_endstop, sensors=sensors, gate_status=gate_status,
                encoder_move_validation=has_encoder, encoder_distance=1234.5,
                filament_position=1234.5,
            )
            visual = render(f"[{sub_label}]", state)
            _assert_sensor_markers(self, visual, sensors, sub_label)

        for label, pos in _position_walk(gate_endstop):
            sensors = _derive_sensors(pos, gate_endstop, fitted)
            state = FilamentDisplayState(
                tool=0, gate=0, pos=pos, bold=is_bold, has_encoder=has_encoder,
                gate_homing_endstop=gate_endstop, sensors=sensors,
                encoder_move_validation=has_encoder, encoder_distance=1234.5,
                filament_position=1234.5,
            )
            visual = render(f"[{label}]", state)
            _assert_sensor_markers(self, visual, sensors, label)

    def test_visual_review(self):
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)

            print("\n--- sync-feedback buffer ---")
            render_buffer_batch(is_bold)

            for n, combo in enumerate(SENSOR_COMBOS, start=1):
                combo_desc = ", ".join(combo) if combo else "None"
                valid_endstops = _valid_gate_endstops(combo)
                if not valid_endstops:
                    print(f"\n--- {n}) {combo_desc} / no valid gate_homing_endstop -- skipped ---")
                    continue
                for endstop_label, gate_endstop in valid_endstops:
                    print(f"\n--- {n}) {combo_desc} / gate_endstop={endstop_label} ---")
                    self._render_combo(combo, gate_endstop, is_bold)


class TestFilamentDisplayAllSensorsAlwaysFalse(unittest.TestCase):
    """
    All 5 real, independently-fittable sensors (entry/exit/shared_exit/
    extruder/toolhead) fitted, but forced to read False at every single
    filament_position -- regardless of how far the tip has actually
    travelled, unlike _derive_sensors' physically-consistent derivation used
    by TestFilamentDisplayVisualReview. This is a broken/miscalibrated-sensor
    scenario: it exercises the "sensor fitted but never triggers" fallback
    glyph (empty_sensor, not trig_sensor) at positions where a working sensor
    would normally read triggered, and confirms the pos-driven "already
    passed" line fill (past()/_passed_gate_sensor()) doesn't secretly depend
    on a sensor ever having reported True.
    """

    ALWAYS_FALSE_SENSORS = {
        SENSOR_ENTRY_PREFIX: False,
        SENSOR_EXIT_PREFIX: False,
        SENSOR_SHARED_EXIT: False,
        SENSOR_EXTRUDER_ENTRY: False,
        SENSOR_TOOLHEAD: False,
    }

    def test_all_sensors_always_false(self):
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            for pos in FILAMENT_POSITIONS:
                state = FilamentDisplayState(
                    tool=0, gate=0, pos=pos, bold=is_bold, has_encoder=True,
                    gate_homing_endstop=SENSOR_ENCODER, sensors=dict(self.ALWAYS_FALSE_SENSORS),
                    gate_status=GATE_AVAILABLE,
                    encoder_move_validation=True, encoder_distance=1234.5,
                    filament_position=1234.5,
                )
                visual = render(f"[{POS_NAMES[pos]}]", state)
                self.assertTrue(visual)


class TestFilamentDisplayLoadUnloadDirection(unittest.TestCase):
    """Mid-move states (pos not LOADED/UNLOADED/UNKNOWN) show a direction arrow instead."""

    def test_direction_arrows(self):
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            for direction, label in ((DIRECTION_LOAD, "LOAD"), (DIRECTION_UNLOAD, "UNLOAD")):
                state = FilamentDisplayState(
                    pos=FILAMENT_POS_IN_BOWDEN,
                    bold=is_bold,
                    direction=direction,
                    gate_homing_endstop=SENSOR_ENCODER,
                    sensors=build_sensors(SENSOR_ENCODER, "optional_sensors_clear"),
                    has_encoder=True,
                    encoder_move_validation=True,
                    encoder_distance=1234.5,
                    filament_position=1234.5,
                )
                visual = render(f"[dir={label}]", state)
                self.assertTrue(visual)


class TestFilamentDisplayGatePreloadEndstop(unittest.TestCase):
    """
    gate_preload_endstop is read into FilamentDisplayState but the ported
    render logic (like the original get_filament_position_string) never
    consults it -- this test documents that today, so it turns red the
    moment display work in this file starts factoring preload endstop into
    the render, which is a useful nudge to update this test alongside it.
    """

    def test_preload_endstop_currently_has_no_effect(self):
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            renders = set()
            for gate_preload_endstop in GATE_PRELOAD_ENDSTOPS:
                state = FilamentDisplayState(
                    pos=FILAMENT_POS_HOMED_GATE,
                    bold=is_bold,
                    gate_homing_endstop=GATE_HOMING_ENDSTOPS[0],
                    gate_preload_endstop=gate_preload_endstop,
                    sensors=build_sensors(GATE_HOMING_ENDSTOPS[0], "optional_sensors_clear"),
                    has_encoder=True,
                    encoder_move_validation=True,
                    encoder_distance=1234.5,
                    filament_position=1234.5,
                )
                visual = render(f"[preload={gate_preload_endstop or '(inherit)'}]", state)
                renders.add(visual)

            self.assertEqual(len(renders), 1, "gate_preload_endstop changed the render - update this test")


class TestFilamentDisplayGateArea(unittest.TestCase):
    """
    gate_area_segment() -- the 10-char block right after the tool tag that
    shows a leading gate_presence_marker() char, then the entry sensor plus
    BOTH exit and shared_exit sensors independently (a unit can have both
    fitted even though only one is the active gate_homing_endstop). Same
    total rendered width either way: an unfitted sensor falls back to plain
    past()-style arrow/space filler instead of a marker glyph. The exit/
    shared_exit gaps are 2 chars each (not 1) so the three markers (exit,
    shared_exit, encoder) read as visually distinct beats; funded by
    shrinking encoder ("En" -> "e") by 1 and the bowden fill on both sides of
    the sync-feedback buffer by 1-2 chars. Exit's and shared_exit's markers
    always show their own real reading independently, never collapsed to
    plain fill just because the other has also been reached -- a fitted
    sensor's real data is never stale, no matter which one is the active
    gate_homing_endstop. At FILAMENT_POS_UNLOADED specifically (forward-parked,
    no discrete "homed" event), the entry-to-exit gap (entry_exit_gap()) shows
    a token one-char tip -- not the gate_presence_marker()'s plain presence
    indication, and not a full arrow run -- only once entry itself has
    actually confirmed the tip got that far; the exit/shared_exit gaps
    (gate_sensor_gap()) show the same split pattern once THEIR sensor has been
    reached but not yet passed. The char right before whichever sensor is the
    active gate_homing_endstop becomes `home` exactly when homed there (see
    _with_home), and no OTHER char anywhere in
    the whole rendered string is left eligible to also become the
    leading-edge tip at that point: the final global pass in
    get_filament_position_string() only restores an arrow-as-tip when no
    `home` glyph appears anywhere, so once we're exactly homed, `home` alone
    marks "arrived here" -- matching homed_segment()'s "no tip when exactly
    at target" convention used everywhere else in this file (HOMED_ENTRY,
    HOMED_EXTRUDER, HOMED_TS included). In "thick" style the home-char swap
    is invisible either way since home and arrow share the same glyph
    (UI_HOME_BOLD) by original design.
    """

    def _prefix(self, state):
        # gate_area_segment() output is always the 10 chars right after tool_text
        visual = strip_color_markup(get_filament_position_string(state))
        tool_text_len = len(f"[T{state.tool}] ") if state.tool >= 0 else len("[T?] ")
        return visual[tool_text_len:tool_text_len + 10]

    def test_pinned_examples(self):
        """
        Pins the exact 10-char sequences from the design proposal so a future
        display tweak can't silently drift this back out of shape. Entry
        sensor fitted+triggered throughout (leading '◉', preceded by the
        gate_presence_marker() line char); gate_homing_endstop only matters
        once pos==HOMED_GATE (decides which of exit/shared_exit/encoder
        counts as "already passed").

        Thin-style expected prefixes were captured from a real run rather than
        hand-derived: which single arrow in the whole string stays as the
        "leading edge" glyph (instead of collapsing to a plain line char) is
        decided by scanning the *entire* rendered string for the last arrow,
        not just this 10-char prefix, so it isn't obvious by inspection alone.
        """
        print()
        cases = [
            ("exit not activated, UNLOADED",
             dict(pos=FILAMENT_POS_UNLOADED, gate_homing_endstop=SENSOR_EXIT_PREFIX,
                  sensors={SENSOR_ENTRY_PREFIX: True, SENSOR_EXIT_PREFIX: False, SENSOR_SHARED_EXIT: False}),
             "━◉▶┈◯┈┈◯┈┈", "■◉■┈◯┈┈◯┈┈"),  # no evidence exit's location was reached -- only a token sliver assumed
            ("exit set, UNLOADED (fwd-parked)",
             dict(pos=FILAMENT_POS_UNLOADED, gate_homing_endstop=SENSOR_EXIT_PREFIX,
                  sensors={SENSOR_ENTRY_PREFIX: True, SENSOR_EXIT_PREFIX: True, SENSOR_SHARED_EXIT: False}),
             "━◉━━◉▶┈◯┈┈", "■◉■■◉■┈◯┈┈"),  # exit's own outgoing gap splits (just arrived, not passed beyond)
            ("exit set, homed@exit",
             dict(pos=FILAMENT_POS_HOMED_GATE, gate_homing_endstop=SENSOR_EXIT_PREFIX,
                  sensors={SENSOR_ENTRY_PREFIX: True, SENSOR_EXIT_PREFIX: True, SENSOR_SHARED_EXIT: False}),
             "━◉━┫◉┈┈◯┈┈", "■◉■■◉┈┈◯┈┈"),  # home char before exit's own marker, no stray tip nearby
                                            # (bold: home glyph == arrow glyph anyway)
            ("exit set, homed@shared_exit",
             dict(pos=FILAMENT_POS_HOMED_GATE, gate_homing_endstop=SENSOR_SHARED_EXIT,
                  sensors={SENSOR_ENTRY_PREFIX: True, SENSOR_EXIT_PREFIX: True, SENSOR_SHARED_EXIT: True}),
             "━◉━━◉━┫◉┈┈", "■◉■■◉■■◉┈┈"),  # exit's own real reading still shows even though shared_exit
                                            # is the active endstop; home char before shared_exit's marker
            ("exit set, homed@encoder",
             dict(pos=FILAMENT_POS_HOMED_GATE, gate_homing_endstop=SENSOR_ENCODER,
                  sensors={SENSOR_ENTRY_PREFIX: True, SENSOR_EXIT_PREFIX: True, SENSOR_SHARED_EXIT: True}),
             "━◉━━◉━━◉━┫", "■◉■■◉■■◉■■"),  # exit's and shared_exit's own real readings both show;
                                            # home char right before "e"/ê
            ("start of bowden (encoder)",
             dict(pos=FILAMENT_POS_START_BOWDEN, gate_homing_endstop=SENSOR_ENCODER,
                  sensors={SENSOR_ENTRY_PREFIX: True, SENSOR_EXIT_PREFIX: True, SENSOR_SHARED_EXIT: True}),
             "━◉━━◉━━◉━━", "■◉■■◉■■◉■■"),  # thick prefix unchanged; "e"+bowden arrows differ after it
        ]
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            for label, kwargs, thin_prefix, thick_prefix in cases:
                expected_prefix = thick_prefix if is_bold else thin_prefix
                state = FilamentDisplayState(tool=0, gate=0, bold=is_bold, has_encoder=True,
                                              encoder_move_validation=True, encoder_distance=1234.5,
                                              filament_position=1234.5, **kwargs)
                visual = render(f"[{label}]", state)
                self.assertEqual(self._prefix(state), expected_prefix, f"{label}: prefix drifted from pinned design")
                self.assertTrue(visual)

    def test_entry_sensor_optional(self):
        """No entry sensor fitted -> P1 falls back to the plain past(UNLOADED) arrow (old behaviour)."""
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            for entry in (None, False, True):
                state = FilamentDisplayState(
                    tool=0, gate=0, pos=FILAMENT_POS_HOMED_GATE, bold=is_bold, has_encoder=True,
                    gate_homing_endstop=SENSOR_EXIT_PREFIX,
                    sensors=build_sensors(SENSOR_EXIT_PREFIX, "no_optional_sensors", entry=entry,
                                          exit_sensor=True, shared_exit=False),
                    encoder_move_validation=True, encoder_distance=1234.5,
                    filament_position=1234.5,
                )
                label = f"[entry={'not fitted' if entry is None else entry}]"
                visual = render(label, state)
                self.assertEqual(len(self._prefix(state)), 10)

    def test_exit_and_shared_exit_independent_fitment(self):
        """All four fitted/not-fitted combinations of exit x shared_exit, same total width throughout."""
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            lengths = set()
            for exit_fitted in (None, False, True):
                for shared_fitted in (None, False, True):
                    state = FilamentDisplayState(
                        tool=0, gate=0, pos=FILAMENT_POS_HOMED_GATE, bold=is_bold, has_encoder=True,
                        gate_homing_endstop=SENSOR_EXIT_PREFIX,
                        sensors=build_sensors(SENSOR_EXIT_PREFIX, "no_optional_sensors",
                                              entry=True, exit_sensor=exit_fitted, shared_exit=shared_fitted),
                        encoder_move_validation=True, encoder_distance=1234.5,
                        filament_position=1234.5,
                    )
                    label = f"[exit={exit_fitted} shared_exit={shared_fitted}]"
                    visual = render(label, state)
                    lengths.add(len(visual))
            self.assertEqual(len(lengths), 1, "fitted vs not-fitted changed the total rendered width")

    def test_total_length_unchanged_from_original_design(self):
        """
        Locks in the length-neutral trade (gate area +4 overall, 'En'->'e' -1,
        2 fewer pre-buffer bowden chars, 1 fewer post-buffer bowden char ==
        net 0) against the length of the original design's equivalent render,
        so future edits here don't creep the total width back up. Length
        doesn't depend on bold (thin/thick only swap glyphs, not segment
        character counts), so both styles pin the same expected length.

        Three further, deliberate changes on top of that: at FILAMENT_POS_LOADED,
        nozzle_segment() now only shows a single tip char past "Nz" (-1);
        gate_area_segment() gained a leading gate_presence_marker() char (+1);
        and the post-buffer bowden fill was shrunk by 1 more char (-1), so the
        pinned length here is 76 - 1 + 1 - 1 = 75.
        """
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            state = FilamentDisplayState(
                tool=0, gate=0, pos=FILAMENT_POS_LOADED, bold=is_bold,
                gate_homing_endstop=SENSOR_EXIT_PREFIX,
                sensors={SENSOR_EXIT_PREFIX: True, SENSOR_EXTRUDER_ENTRY: True, SENSOR_TOOLHEAD: False,
                         SENSOR_COMPRESSION: False, SENSOR_TENSION: False},
                has_encoder=True, encoder_move_validation=True, encoder_distance=817.5,
                has_buffer=True, sync_feedback_state="neutral",
                filament_position=814.6,
            )
            visual = render("[length-neutral check]", state)
            self.assertEqual(len(visual), 75)  # 76 (pre-gate-area-redesign) - 1 (nozzle tip)
                                                # + 1 (presence marker) - 1 (bowden fill shrink)


class TestFilamentDisplayBufferAndSyncFeedback(unittest.TestCase):
    """Tension/compression/proportional sensor combinations feed the buffer_segment() bracket."""

    def test_buffer_matrix(self):
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            render_buffer_batch(is_bold)


class TestFilamentDisplayToolAndGateEdgeCases(unittest.TestCase):

    def test_bypass_tool(self):
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            state = FilamentDisplayState(
                tool=TOOL_GATE_BYPASS,
                gate=-1,
                pos=FILAMENT_POS_LOADED,
                bold=is_bold,
                gate_homing_endstop=SENSOR_ENCODER,
                sensors=build_sensors(SENSOR_ENCODER, "optional_sensors_triggered"),
                has_encoder=True,
                encoder_move_validation=True,
                encoder_distance=1234.5,
                filament_position=1234.5,
            )
            visual = render("[BYPASS tool, LOADED]", state)
            self.assertIn("[BYPASS]", visual)

    def test_unknown_tool(self):
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            state = FilamentDisplayState(
                tool=-1,
                gate=-1,
                pos=FILAMENT_POS_UNKNOWN,
                bold=is_bold,
                gate_homing_endstop=SENSOR_ENCODER,
                sensors=build_sensors(SENSOR_ENCODER, "no_optional_sensors"),
            )
            visual = render("[T? tool, UNKNOWN pos]", state)
            self.assertIn("[T?]", visual)

    def test_gate_color(self):
        print()
        for style_name, is_bold in BOLD_STYLES:
            style_header(style_name, is_bold)
            state = FilamentDisplayState(
                tool=0,
                gate=0,
                pos=FILAMENT_POS_LOADED,
                bold=is_bold,
                gate_homing_endstop=SENSOR_ENCODER,
                sensors=build_sensors(SENSOR_ENCODER, "optional_sensors_triggered"),
                has_encoder=True,
                encoder_move_validation=True,
                encoder_distance=817.5,  # matches the original design-proposal sample string
                color=True,
                gate_color="#FF0000",
                filament_position=814.6,
            )
            # color=True path exercises _color_filament(); strip_color_markup() should
            # remove the resulting {{RRGGBB}}...{{}} tokens same as the plain case
            visual = render("[gate_color=#FF0000]", state)
            self.assertNotIn("{{", visual)


def load_tests(loader, tests, pattern):
    # unittest's discover() (and any other loadTestsFromModule call) checks for
    # this hook and uses its return value instead of collecting TestCase
    # subclasses -- so `make test`'s pattern='*' sweep gets nothing from this
    # module. HH_FILAMENT_DISPLAY_REVIEW is set only by the `filament_display`
    # Makefile target, so the classes below still run there.
    if os.environ.get('HH_FILAMENT_DISPLAY_REVIEW'):
        return tests
    return unittest.TestSuite()


if __name__ == '__main__':
    unittest.main(verbosity=2)
