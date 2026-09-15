# Shared-gate occupancy guard — reference

Verified against the `private_v4` tree. Line numbers will drift; re-grep the
symbol names below if they don't match.

## 1. `gate_homing_endstop` and which endstops are shared

`gate_homing_endstop` is a per-unit `MmuUnitParameters` choice field selecting
which sensor homes/parks filament at a gate —
`extras/mmu/unit/mmu_unit_parameters.py:173`:

```python
ParamSpec('gate_homing_endstop', 'choice', SENSOR_ENCODER, section="GATE HOMING",
           choices={o: o for o in GATE_ENDSTOPS}, on_change=_on_gate_homing_endstop),
```

Valid values (`GATE_ENDSTOPS`, `extras/mmu/mmu_constants.py:167`): `mmu_shared_exit`,
`encoder`, `mmu_exit`, `extruder`. Of these, `SHARED_GATE_ENDSTOPS`
(`mmu_constants.py:171`) marks which are a **per-unit resource shared by every
gate on that unit** rather than owned by one gate: `mmu_shared_exit`,
`extruder` (entry sensor), `encoder`. `mmu_exit` is per-gate and not in this
set.

- Hardware config: `config/base/mmu_hardware.cfg:459-461` — one
  `mmu_shared_exit_switch_pin` under `[mmu_sensors]`.
- Sensor qualification: `extras/mmu/mmu_sensor_manager.py:318-337`
  (`get_qualified_endstop_name`) — shared endstops are qualified by unit name
  (`"<unitName>:mmu_shared_exit"`), confirming the sharing scope is per-*unit*.

## 2. The guard itself

`_shared_gate_path_occupied(self, endstop, gate)` —
`extras/mmu/mmu_filament_movement.py` (search the name; line numbers drift).

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
is now refused likewise by `_validate_gate_preload_endstop`
(`extras/mmu/unit/mmu_unit_parameters.py`) — that validator exists *only* to
keep this guard alive, so don't delete it as redundant. If you add another
`SHARED_GATE_ENDSTOPS` consumer that takes an endstop name from config, check
it cannot name the alias either. Note `has_sensor()` *does* see the alias
(it goes through `active_sensors_map`) while `resolve_sensor()` does not; that
asymmetry is the shape of the bug.

**Call sites today** (grep rather than trust these line numbers):
- `extras/mmu/commands/mmu_nfc_scan.py` — `can_continue` predicate:
  `active_unit.can_crossload and not mmu._shared_gate_path_occupied(scan_unit.p.gate_homing_endstop, gate)`.
- `extras/mmu/commands/mmu_preload.py` — same pattern, using
  `preload_endstop = preload_unit.p.gate_preload_endstop or preload_unit.p.gate_homing_endstop`.
  Both of these short-circuit on `... is not active_unit`, so neither can reach
  the cross-unit case.
- `extras/mmu/mmu_filament_movement.py` inside `_preload_gate` and `_jog_scan`.
- `extras/mmu/mmu_nfc_arbiter.py` — advisory only, skips a candidate.
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

`gate_parking_distance` (spec at `mmu_unit_parameters.py:175`): negative =
retraction (safe on any endstop), positive = park forward past the sensor —
**only safe when `gate_homing_endstop == mmu_exit`**, a per-gate sensor.
Validator `_validate_gate_parking_distance` (`mmu_unit_parameters.py:153-158`):

```python
def _validate_gate_parking_distance(self, value):
    if value > 0 and self.gate_homing_endstop != SENSOR_EXIT_PREFIX:
        raise ValueError(...)
```

**The gap:** setting both fields in one `MMU_TEST_CONFIG` call re-validates
correctly (fields apply alphabetically, so `gate_homing_endstop` lands first).
Two *separate* commands didn't: set a positive `gate_parking_distance` while
on `mmu_exit` (legal), then later switch `gate_homing_endstop` to a shared
endstop — the now-unsafe positive value went unchecked.

**Fix**, `_on_gate_homing_endstop` (`mmu_unit_parameters.py:70-82`):

```python
def _on_gate_homing_endstop(self, old, new):
    if new != old:
        self._mmu_unit.calibrator.adjust_bowden_lengths_on_homing_change()
        self._validate_gate_parking_distance(self.gate_parking_distance)
        if not self.gate_preload_endstop:
            self._validate_gate_preload_parking_distance(self.gate_preload_parking_distance)
```

Companion hook `_on_gate_preload_endstop` (`mmu_unit_parameters.py:84-86`)
re-runs `_validate_gate_preload_parking_distance` whenever `gate_preload_endstop`
itself is set explicitly (validator at lines 160-166, resolving the effective
endstop as `gate_preload_endstop or gate_homing_endstop`).

**If you add a parameter with the same shape of dependency** (validity
depends on another field that can change live), wire it through an
`on_change` hook the same way — a load-time-only validator isn't enough.

## 4. Reference tests

For the **cross-unit** resolution specifically, the class below cover only the
single-unit case. See `test/test_mmu_check_gate.py::TestSharedPathTargetUnit`,
which builds a two-unit fixture with `profiles.clone_across_units()` and asserts
the guard is `True` for a gate on the occupied unit and `False` for one on the
other. And `test/test_mmu_profiles.py::
test_no_bowden_mode_rejects_shared_exit_preload_endstop` is what keeps the
no-bowden alias trap above closed.

`test/test_mmu_nfc_scan.py`:

- `TestReparkDrift` (line ~623) — pre-existing, covers `_park_after_scan`
  rewind/re-park settling off `mmu_exit`.
- `TestSharedGateOccupancy` (line ~730) — the reference class for this guard:
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
     the §3 fix: set a legal positive parking distance on `mmu_exit`, then a
     *separate* command switching to `encoder` must raise, and must not leave
     `gate_homing_endstop` half-applied.

Each new occupancy-guard test was confirmed to fail cleanly against the
pre-guard code — if you're refactoring this area, re-run that check (revert
the guard locally, confirm these tests fail) before trusting a green suite.
