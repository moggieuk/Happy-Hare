---
name: gate-endstop-invariants
description: Explains the shared-gate endstop occupancy invariant in Happy Hare's extras/mmu code — the rule that gate_homing_endstop values shared across gates on a unit (mmu_shared_exit, extruder, encoder) must never be homed or swept into while a different gate's filament still occupies them, and how gate_parking_distance sign validation is re-checked when gate_homing_endstop changes live. Use this whenever touching gate homing, gate endstops, gate_parking_distance, crossload logic, mmu_filament_movement.py, or shared-gate/shared-exit sensor behavior in extras/mmu/ — even if the request doesn't mention "invariant" or "occupancy" by name, e.g. adding a new endstop type, changing parking/recovery logic, or debugging a jam or tangle at a hub.
---

# Gate/endstop occupancy invariant

Happy Hare lets several gates on one unit share a single physical endstop
(`mmu_shared_exit`, the `extruder` entry sensor, or the `encoder`). Any code
path that homes or sweeps a gate onto one of those **must** confirm no other
gate's filament is still sitting on it first — `can_crossload` alone only
certifies the selector mechanism won't jam moving between gates. It says
nothing about a shared sensor further down the path. Skip the check and a
gear motor can drive one gate's filament straight into another's at the hub,
with no error raised beforehand.

## The invariant

`_shared_gate_path_occupied(endstop, gate)` in
[`extras/mmu/mmu_filament_movement.py`](../../../extras/mmu/mmu_filament_movement.py)
is the single source of truth. Call it — or make sure it's already being
called on your path — before selecting/homing/sweeping a gate onto any
endstop in `SHARED_GATE_ENDSTOPS` (`mmu_constants.py`):

- **Switch-based** (`mmu_shared_exit`, `extruder`): reads the live qualified
  sensor directly, so call order doesn't matter.
- **Encoder**: true iff filament is loaded, a *different* gate is selected,
  and that gate belongs to the same unit — this becomes inert once the
  caller has already switched `gate_selected` to the target gate, so **this
  check must run before `select_gate()`**, not after.

Existing call sites to model a new one on: `commands/mmu_nfc_scan.py`,
`commands/mmu_preload.py`, and the direct re-checks inside `_preload_gate`
and `_jog_scan` in `mmu_filament_movement.py` itself.

If you're changing or extending this, read
[references/occupancy-guard.md](references/occupancy-guard.md) first — it
has exact file:line citations, the concrete failure scenario, and the
reference tests to run this against
(`test/test_mmu_nfc_scan.py::TestSharedGateOccupancy`).

## gate_parking_distance and live endstop changes

`gate_parking_distance > 0` (park *past* the sensor) is only safe when
`gate_homing_endstop == mmu_exit` — a per-gate sensor. It's unsafe on a
shared endstop, because parking forward would leave filament sitting in the
shared merge zone. The validator runs automatically when both fields are set
in one `MMU_TEST_CONFIG` call (alphabetical field order saves you), but two
*separate* commands — set a positive parking distance while on `mmu_exit`,
then later switch to a shared endstop — used to leave the now-unsafe value
unchecked. It's fixed via an `on_change` hook that re-validates when the
endstop choice changes live; see
[references/occupancy-guard.md](references/occupancy-guard.md) §3 for the
exact mechanism and where the equivalent preload-endstop hook lives.

**If you're adding a new parameter whose validity depends on
`gate_homing_endstop` or `gate_preload_endstop`**, this is the pattern to
follow — a bare load-time validator is not enough if the field can change
after boot.
