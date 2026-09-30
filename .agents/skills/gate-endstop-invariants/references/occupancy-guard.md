# Shared-gate occupancy guard — reference

Verified against the current tree. Citations are by symbol name plus file
path, not line number — grep the name to find it.

## 1. `gate_homing_endstop` and which endstops are shared

`gate_homing_endstop` is a per-unit `MmuUnitParameters` choice field selecting
which sensor homes/parks filament at a gate — its `ParamSpec` in
`extras/mmu/unit/mmu_unit_parameters.py` defaults to `encoder`, takes its
choices from `GATE_ENDSTOPS`, and wires `validator=_validate_gate_homing_endstop`
and `on_change=_on_gate_homing_endstop`.

Valid values (`GATE_ENDSTOPS`, `extras/mmu/mmu_constants.py`): `mmu_shared_exit`,
`encoder`, `mmu_exit`, `extruder`. Of these, `SHARED_GATE_ENDSTOPS`
(same file) marks which are a **per-unit resource shared by every
gate on that unit** rather than owned by one gate: `mmu_shared_exit`,
`extruder` (entry sensor), `encoder`. `mmu_exit` is per-gate and not in this
set.

- Hardware config: `config/base/mmu_hardware.cfg` — one
  `mmu_shared_exit_switch_pin` in each unit's `[mmu_sensors <unit>]` section.
- Sensor qualification: `MmuSensorManager.get_qualified_endstop_name()` in
  `extras/mmu/mmu_sensor_manager.py` — shared endstops are qualified by unit name
  (`"<unitName>:mmu_shared_exit"`), confirming the sharing scope is per-*unit*.

## 2. The guard itself

`_shared_gate_path_occupied(self, endstop, gate)` in
`extras/mmu/mmu_filament_movement.py`.

- Returns `False` immediately if `endstop not in SHARED_GATE_ENDSTOPS`.
- Encoder case: `filament_pos != FILAMENT_POS_UNLOADED and gate_selected != gate
  and unit.owns_gate(gate_selected)`. **Must be checked before the caller's own
  `select_gate(gate)`** — it goes inert once `gate_selected` already equals
  `gate` (documented in the docstring).
- Switch-based case (`mmu_shared_exit`, `extruder`): resolves the sensor
  against the **target gate's** unit and reads it via
  `sensor_manager.check_event_sensor(name, gate)` — order-independent.

### Resolve against the TARGET gate's unit, not the selected one

Use `check_event_sensor(name, gate)` with the name qualified by
`mmu_unit(gate)`. It resolves against `all_sensors_map`, the stable global
registry, so it reads the switch that actually sits downstream of `gate`.

`check_sensor(get_qualified_endstop_name(endstop))` is the trap: with no
`mmu_unit` the name is qualified with the *selected* gate's unit, and
`check_sensor` then strips that prefix and looks the generic name up in
`active_sensors_map` — itself only a pointer at the selected gate's map. On a
multi-unit machine that reads the wrong unit entirely and lets a sweep proceed
into an occupied shared path.

### Trap: the no-bowden `mmu_shared_exit` alias

`MmuSensorManager` aliases `gate_sensors[SENSOR_SHARED_EXIT]` to the extruder
sensor on a unit with `not require_bowden_move` and no physical shared-exit
switch (search `Special case for "no bowden" designs`). **That alias lives only
in the per-gate map, never in `all_sensors_map`** — so `check_event_sensor`
cannot see it and returns `None`, i.e. the guard reads `False` unconditionally.

`gate_homing_endstop` can never name the alias (`_validate_gate_homing_endstop`
forces `extruder` when `require_bowden_move == 0`), and `gate_preload_endstop`
is refused likewise by `_validate_gate_preload_endstop`
(`extras/mmu/unit/mmu_unit_parameters.py`) — that validator exists *only* to
keep this guard alive, so don't delete it as redundant. If you add another
`SHARED_GATE_ENDSTOPS` consumer that takes an endstop name from config, check
it cannot name the alias either. Note `has_sensor()` *does* see the alias
(it goes through `active_sensors_map`) while `resolve_sensor()` does not; that
asymmetry is the shape of the bug.

**Call sites today** (grep `_shared_gate_path_occupied` to confirm):
- `extras/mmu/commands/mmu_nfc_scan.py` — `can_continue` predicate:
  `active_unit.can_crossload and not mmu._shared_gate_path_occupied(scan_unit.p.gate_homing_endstop, gate)`.
- `extras/mmu/commands/mmu_preload.py` — same pattern, using
  `preload_endstop = preload_unit.p.gate_preload_endstop or preload_unit.p.gate_homing_endstop`.
  Both of these short-circuit on `... is not active_unit`, so neither can reach
  the cross-unit case.
- `extras/mmu/mmu_filament_movement.py` inside `_preload_gate` and `_jog_scan`.
- `extras/mmu/mmu_nfc_arbiter.py` `_evict_reject()` — advisory only: a neighbor
  whose own `gate_homing_endstop` path is occupied is skipped as an eviction
  candidate.
- `extras/mmu/commands/mmu_check_gate.py` `_check_path_unloaded()` — iterates all
  of `SHARED_GATE_ENDSTOPS`, so it is the widest consumer.

**Concrete failure if you remove or bypass this:** with
`gate_homing_endstop = mmu_shared_exit` on a crossload-capable unit (e.g.
BoxTurtle), gate 1 already has filament parked past the shared exit switch.
`MMU_NFC_SCAN GATE=0` or `MMU_PRELOAD GATE=0` on gate 0 — without the guard,
`can_crossload` alone lets it proceed, the gear motor sweeps gate 0 forward
into the already-occupied shared exit path, driving gate 0's filament into
gate 1's at the hub. No error, no warning — a physical jam.

## 3. gate_parking_distance re-validation on a live endstop change

`gate_parking_distance` (its `ParamSpec` in
`extras/mmu/unit/mmu_unit_parameters.py`): negative = retraction (safe on any
endstop), positive = park forward past the sensor — **only safe when
`gate_homing_endstop == mmu_exit`**, a per-gate sensor. Its validator,
`_validate_gate_parking_distance()` (same file), raises `ValueError` for a
positive value on any other endstop.

**Why a validator alone isn't enough:** setting both fields in one
`MMU_TEST_CONFIG` call re-validates correctly (`MmuBaseParameters` applies
supplied fields in sorted order, so `gate_homing_endstop` lands first). Two
*separate* commands are different: set a positive `gate_parking_distance` while
on `mmu_exit` (legal), then later switch `gate_homing_endstop` to a shared
endstop — only the endstop's own `on_change` hook can catch the now-unsafe
value.

`_on_gate_homing_endstop()` in `extras/mmu/unit/mmu_unit_parameters.py` is that
hook. On a real change it adjusts bowden lengths via the calibrator, then
re-runs every validator whose legal sign depends on the endstop:
`_validate_gate_parking_distance`, `_validate_nfc_neighbor_evict_distance` and
`_validate_nfc_gate_clear_distance` always, plus
`_validate_gate_preload_parking_distance` and
`_validate_nfc_preload_clear_distance` when `gate_preload_endstop` is empty
(i.e. inherits `gate_homing_endstop`). The hook runs *after* the new value is
stored, so a failing re-check raises but leaves the new endstop in place.

Companion hook `_on_gate_preload_endstop()` (same file) re-runs
`_validate_gate_preload_parking_distance`, `_validate_nfc_preload_clear_distance`
and `_validate_sensorless_preload_profile` whenever `gate_preload_endstop`
itself changes. The preload validators resolve the effective endstop as
`gate_preload_endstop or gate_homing_endstop`.

**If you add a parameter with the same shape of dependency** (validity
depends on another field that can change live), wire it through an
`on_change` hook the same way — a load-time-only validator isn't enough.

## 4. `gate_occupancy()` — a per-gate verdict built partly from shared sensors

`gate_occupancy(gate)` in `extras/mmu/mmu_filament_movement.py` (just below
`_shared_gate_path_occupied`) classifies one gate as `OCCUPANCY_PRESENT` /
`OCCUPANCY_EMPTY` / `OCCUPANCY_UNKNOWN` (`mmu_constants.py`). Its inputs are of
mixed scope:

| sensor | scope |
|---|---|
| `mmu_exit_<g>`, `mmu_entry_<g>` | per-gate |
| `mmu_shared_exit`, `extruder`, `toolhead` | shared by every gate on the unit |

A shared sensor reading present therefore makes `gate_occupancy()` return
`PRESENT` for **every** gate on that unit, whichever gate's filament is
actually sitting there.

The two call sites want different things from it:

- `commands/mmu_check_gate.py` — "may I declare this lane empty without moving
  anything?" The wide OR is correct and conservative here: a remnant anywhere
  downstream should stop you calling the lane empty.
- `_home_to_gate()` — the `empty` decision needs `UNKNOWN` *specifically*, to let
  a clean homing miss stand as an empty lane on a machine with no entry switches
  (Tradrack, Chameleon, PicoMMU/MMX, encoder-homing ERCF). That branch is only
  live with `mark_empty_on_failure=False`, which only `MMU_CHECK_GATE` passes
  (via `_load_gate()`). A shared sensor that
  flips `UNKNOWN` to `PRESENT` makes `empty` false: position is left `UNKNOWN`,
  the sweep aborts and recovery is suppressed. On those machines that is the
  difference between gate discovery working and not.

**Why it is safe today.** `_check_path_unloaded()` (`commands/mmu_check_gate.py`)
runs before each gate is selected and refuses when `mmu_shared_exit`, `extruder`
or `toolhead` reads present — so all three are guaranteed clear by the time
either call site runs, and at the `MMU_CHECK_GATE` site the shared arm is
strictly redundant. Measured: instrumenting `gate_occupancy` across `3ms` /
`tradrack` / `boxturtle`, with and without the extruder sensor jammed
permanently `True`, the shared arm fired in **0 of 73 calls** — a genuinely
triggered shared sensor makes the command refuse before reaching it, and
machines without those sensors read `None`.

**The invariant to preserve:** any caller that acts on `UNKNOWN` must have ruled
the shared path out first. Declining to act on `PRESENT` is always safe. If you
add a caller outside `MMU_CHECK_GATE`, either put it behind the same guard or
split out a per-gate-only variant for that one decision — otherwise you
reintroduce the cross-gate misattribution §2 exists to prevent. The docstring on
`gate_occupancy()` states this; keep the two in sync.

## 5. Reference tests

`test/test_mmu_nfc_scan.py`:

- `TestReparkDrift` — covers `_park_after_scan` rewind/re-park settling off
  `mmu_exit`.
- `TestSharedGateOccupancy` — the reference class for this guard, single-unit:
  1. `test_shared_exit_rewind_settles_and_does_not_drift_on_repeated_scans` —
     same drift invariant, off the `mmu_shared_exit` datum instead.
  2. `test_shared_exit_scan_is_refused_when_a_sibling_gate_occupies_it` —
     places filament on gate 1 past `mmu_shared_exit`, then
     `MMU_NFC_SCAN GATE=0` on the same unit must refuse **before any motion**
     (`fil.history == []`).
  3. `test_encoder_scan_is_refused_when_the_active_filament_is_on_a_sibling_gate` —
     boots a dedicated profile with a real encoder (`nfc_per_gate` has none);
     gate 1 loaded/selected, `MMU_NFC_SCAN GATE=0` on a crossload-capable unit
     must still refuse via the command-level check.
  4. `test_switching_to_a_shared_endstop_rechecks_a_stale_parking_distance` —
     the §3 hook: set a legal positive parking distance on `mmu_exit`, then a
     *separate* command switching to `encoder` must raise naming
     `gate_parking_distance`, with `gate_homing_endstop` left at `encoder`.
- `TestSharedGateOccupancyAcrossUnits` — the **cross-unit** resolution:
  `test_guard_reads_the_target_units_shared_exit` builds a two-unit fixture
  with `profiles.clone_across_units()` and asserts the guard is `True` for a
  gate on the occupied unit and `False` for one on the other.

Elsewhere:

- `test/test_mmu_check_gate.py::TestSharedPathTargetUnit` — end-to-end on the
  same two-unit fixture: `MMU_CHECK_GATE` on a gate whose unit has an occupied
  shared exit must refuse before selecting or moving anything.
- `test/test_mmu_profiles.py::test_no_bowden_mode_rejects_shared_exit_preload_endstop`
  — keeps the no-bowden alias trap in §2 closed.

Each new occupancy-guard test was confirmed to fail cleanly against the
pre-guard code — if you're refactoring this area, re-run that check (revert
the guard locally, confirm these tests fail) before trusting a green suite.
