#!/usr/bin/env python3
# mmu_sync_test_harness.py
# Minimal CLI to exercise MmuSyncFeedbackManager with mocked Klipper/MMU objects.
#
# Usage:
#   - Put your module in the same folder as this file, named:
#         mmu_sync_feedback_manager.py
#     and ensure it defines `MmuSyncFeedbackManager`.
#   - Run:  python3 mmu_sync_test_harness.py
#
# Defaults per request:
#   - Single gate only
#   - Autotune always ON
#   - Two sensors default PRESENT=1, ACTIVE=0 (enabled but not triggered)
#   - sync_multiplier_high = 1.05, sync_multiplier_low = 0.95
#   - default rotation_distance = 20.0
#
# Commands:
#   h                                      : help
#   s                                      : invoke _handle_mmu_synced()
#   u                                      : invoke _handle_mmu_unsynced()
#   f <-1|0|1>                             : set sensors to match state and send _handle_sync_feedback()
#   m <mm>                                 : move extruder by <mm> (float, +extrude / -retract) and simulate _check_extruder_movement()
#   l                                      : show log of set_rotation_distance() calls
#   sensor <tp> <ta> <cp> <ca>             : set sensor PRESENT/ACTIVE for tension & compression
#                                            (fires mmu:sync_feedback automatically if effective state changes)
#   status                                 : print brief status of sensors/manager
#   q                                      : quit
#
# After every command, current rotation_distance and state are printed,
# along with the current gate's rd_clamp and sensor flags.
#
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
import time, sys

try:
    from mmu_sync_feedback_manager import MmuSyncFeedbackManager
except Exception as e:
    print("ERROR: Could not import MmuSyncFeedbackManager from mmu_sync_feedback_manager.py")
    print("Detail:", e)
    sys.exit(1)


# ----------------------------- Minimal mocks -----------------------------

class Reactor:
    NOW   = 0.0
    NEVER = float("inf")
    def __init__(self): self._timer = None; self._when = self.NEVER
    def register_timer(self, func): self._timer = func; return func
    def update_timer(self, timer, when):
        if timer is self._timer: self._when = when
    def run_due(self, now):
        if self._timer and self._when <= now:
            self._when = self._timer(now)

class MockMCU:
    def estimated_print_time(self, t): return t

class Extruder:
    def __init__(self): self.position = 0.0
    def find_past_position(self, _): return self.position

class Toolhead:
    def __init__(self, extruder): self._ex = extruder
    def get_extruder(self): return self._ex

class SensorManager:
    def __init__(self, mmu):
        self.mmu = mmu
        # Defaults: PRESENT=1, ACTIVE=0
        self._present = {mmu.SENSOR_TENSION: True,  mmu.SENSOR_COMPRESSION: True}
        self._active  = {mmu.SENSOR_TENSION: False, mmu.SENSOR_COMPRESSION: False}
    def has_sensor(self, t): return self._present.get(t, False)
    def check_sensor(self, t): return self._active.get(t, False)
    # Helpers for CLI
    def set_present(self, t, present: bool): self._present[t] = bool(present)
    def set_active(self, t, active: bool):  self._active[t]   = bool(active)
    def flags(self):
        # returns dict of present/active for both sensors
        return {
            'tension_present': self._present[self.mmu.SENSOR_TENSION],
            'tension_active':  self._active[self.mmu.SENSOR_TENSION],
            'compression_present': self._present[self.mmu.SENSOR_COMPRESSION],
            'compression_active':  self._active[self.mmu.SENSOR_COMPRESSION],
        }

class Printer:
    def __init__(self, reactor, toolhead, mcu):
        self._handlers = {}; self.reactor = reactor; self._toolhead = toolhead; self._mcu = mcu
    def register_event_handler(self, name, func): self._handlers.setdefault(name, []).append(func)
    def send_event(self, name, *args):
        for f in self._handlers.get(name, []):
            if name == "mmu:sync_feedback": f(time.time(), *args)
            else: f()
    def lookup_object(self, name):
        if name == 'mcu': return self._mcu
        raise KeyError(name)

@dataclass
class Config:
    values: Dict[str, float] = field(default_factory=lambda: {
        'sync_feedback_enabled': 1,
        'sync_feedback_buffer_range': 10.0,
        'sync_feedback_buffer_maxrange': 10.0,
        'sync_multiplier_high': 1.05,
        'sync_multiplier_low': 0.95,
    })
    def getint(self, k, d, **_): return int(self.values.get(k, d))
    def getfloat(self, k, d, minval=None, maxval=None, above=None):
        v = float(self.values.get(k, d))
        if above is not None and v <= above: v = above + 1e-9
        if minval is not None: v = max(v, minval)
        if maxval is not None: v = min(v, maxval)
        return v

class LoggerMixin:
    def log_trace(self, msg): print("[TRACE]", msg)
    def log_debug(self, msg): print("[DEBUG]", msg)
    def log_info(self, msg):  print("[INFO ]", msg)
    def log_warning(self, msg): print("[WARN ]", msg)
    def log_error(self, msg): print("[ERROR]", msg)
    def log_always(self, msg): print("[ALWAYS]", msg)

class MMUMachine:
    def __init__(self): self.filament_always_gripped = False

class MockMMU(LoggerMixin):
    SENSOR_TENSION = 1
    SENSOR_COMPRESSION = 2
    DIRECTION_LOAD = 1
    DIRECTION_UNLOAD = -1
    def __init__(self, reactor, printer, toolhead, config):
        self.reactor = reactor; self.printer = printer; self.toolhead = toolhead; self.config = config
        self.sensor_manager = SensorManager(self)
        self.gate_selected = 0
        self.is_enabled = True
        self.autotune_rotation_distance = True    # always on
        self._is_running_test = True
        self.mmu_machine = MMUMachine()
        self._rd = 20.0
        self._rd_set_log: List[Tuple[float, float]] = []
    def get_rotation_distance(self, gate): return self._rd
    def set_rotation_distance(self, rd):
        self._rd_set_log.append((time.time(), rd))
        print(f"[RD-SET] rd={rd:.4f}")
    def save_rotation_distance(self, gate, rd):
        self._rd = float(rd)
        print(f"[RD-SAVE] autotuned_rd={self._rd:.4f}")
    def rd_log(self): return self._rd_set_log

# ----------------------------- Helpers -----------------------------

def compute_effective_state(mmu: MockMMU) -> float:
    """
    Mirror MmuSyncFeedbackManager._reset_current_sync_state() logic to compute
    -1 (tension), 0 (neutral), or 1 (compression) from PRESENT/ACTIVE flags.
    """
    has_tension = mmu.sensor_manager.has_sensor(mmu.SENSOR_TENSION)
    has_compression = mmu.sensor_manager.has_sensor(mmu.SENSOR_COMPRESSION)
    tension_active = mmu.sensor_manager.check_sensor(mmu.SENSOR_TENSION)
    compression_active = mmu.sensor_manager.check_sensor(mmu.SENSOR_COMPRESSION)

    SYNC_STATE_NEUTRAL = 0
    SYNC_STATE_COMPRESSION = 1
    SYNC_STATE_TENSION = -1

    if has_tension and has_compression:
        if tension_active == compression_active:
            return float(SYNC_STATE_NEUTRAL)
        elif tension_active and not compression_active:
            return float(SYNC_STATE_TENSION)
        else:
            return float(SYNC_STATE_COMPRESSION)
    elif has_compression and not has_tension:
        return float(SYNC_STATE_COMPRESSION if compression_active else SYNC_STATE_TENSION)
    elif has_tension and not has_compression:
        return float(SYNC_STATE_TENSION if tension_active else SYNC_STATE_COMPRESSION)
    else:
        # No sensors present; treat as neutral
        return float(SYNC_STATE_NEUTRAL)

def set_sensors_for_state(mmu: MockMMU, state: float):
    """
    Adjust ACTIVE flags to reflect the desired state, without changing PRESENT flags.
    If neutral is impossible (only one sensor present), leave flags unchanged and warn.
    """
    sm = mmu.sensor_manager
    has_t = sm.has_sensor(mmu.SENSOR_TENSION)
    has_c = sm.has_sensor(mmu.SENSOR_COMPRESSION)

    desired = int(state)
    if desired not in (-1, 0, 1):
        return  # ignore weird values

    if has_t and has_c:
        if desired == -1:   # tension
            sm.set_active(mmu.SENSOR_TENSION, True)
            sm.set_active(mmu.SENSOR_COMPRESSION, False)
        elif desired == 1:  # compression
            sm.set_active(mmu.SENSOR_TENSION, False)
            sm.set_active(mmu.SENSOR_COMPRESSION, True)
        else:               # neutral: both equal; choose both False
            sm.set_active(mmu.SENSOR_TENSION, False)
            sm.set_active(mmu.SENSOR_COMPRESSION, False)
        return

    # Single-sensor cases
    if has_t and not has_c:
        # tension-only design: active=True => tension(-1), active=False => compression(+1)
        if desired == -1:
            sm.set_active(mmu.SENSOR_TENSION, True)
        elif desired == 1:
            sm.set_active(mmu.SENSOR_TENSION, False)
        else:
            print("[WARN ] Neutral state cannot be represented with only a tension sensor present.")
    elif has_c and not has_t:
        # compression-only design: active=True => compression(+1), active=False => tension(-1)
        if desired == 1:
            sm.set_active(mmu.SENSOR_COMPRESSION, True)
        elif desired == -1:
            sm.set_active(mmu.SENSOR_COMPRESSION, False)
        else:
            print("[WARN ] Neutral state cannot be represented with only a compression sensor present.")
    else:
        print("[WARN ] No sensors present; cannot represent any non-neutral state.")

# ----------------------------- CLI -----------------------------

HELP = """Commands:
  h                                      : help
  s                                      : invoke _handle_mmu_synced()
  u                                      : invoke _handle_mmu_unsynced()
  f <-1|0|1>                             : set sensors to match state and send _handle_sync_feedback()
  m <mm>                                 : move extruder by <mm> (float, +extrude / -retract) and simulate _check_extruder_movement()
  l                                      : show log of set_rotation_distance() calls
  sensor <tp> <ta> <cp> <ca>             : set sensor PRESENT/ACTIVE for tension & compression
                                           (fires sync_feedback if effective state changes)
  status                                 : print brief status of sensors/manager
  q                                      : quit
"""

def show_status(mgr: MmuSyncFeedbackManager, mmu: MockMMU, extruder: Extruder):
    gate = mmu.gate_selected
    rd = mmu.get_rotation_distance(gate)
    state = mgr.get_sync_feedback_string(detail=True)
    fl = mmu.sensor_manager.flags()
    dir_map = {0: "static", 1: "load", -1: "unload"}
    direction_label = dir_map.get(mgr.extruder_direction, "unknown")
    print(
        "[STATUS] rd={:.4f}  state={}  active={}  enabled={}  extruder_pos={:.2f}  "
        "direction={}({})  "
        "tension(present={}, active={})  compression(present={}, active={})".format(
            rd, state, mgr.active, mgr.sync_feedback_enabled, extruder.position,
            mgr.extruder_direction, direction_label,
            int(fl['tension_present']), int(fl['tension_active']),
            int(fl['compression_present']), int(fl['compression_active'])
        )
    )
    clamp = mgr.rd_clamps.get(gate)
    if clamp:
        slow_rd, curr_rd, fast_rd, tuned_rd, orig = clamp
        print(f"[RD-CLAMP] slow={slow_rd:.4f} curr={curr_rd:.4f} fast={fast_rd:.4f} tuned={tuned_rd} orig={orig:.4f}")
    else:
        print("[RD-CLAMP] (not initialized yet). Use 's' to sync/init.")

def main():
    reactor = Reactor()
    extruder = Extruder()
    toolhead = Toolhead(extruder)
    printer = Printer(reactor, toolhead, MockMCU())
    config = Config()
    mmu = MockMMU(reactor, printer, toolhead, config)
    mgr = MmuSyncFeedbackManager(mmu)

    # Initialize starting state & rd_clamps as requested
    mgr.reset_sync_starting_state_for_gate(0)

    print("== Minimal MmuSyncFeedbackManager Test Harness (single gate) ==")
    print(HELP)
    show_status(mgr, mmu, extruder)
    print()

    while True:
        try:
            raw = input("mmu> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            show_status(mgr, mmu, extruder)
            continue

        parts = raw.split()
        cmd = parts[0].lower()
        try:
            if cmd == 'h':
                print(HELP)

            elif cmd == 'q':
                break

            elif cmd == 's':
                # Show current sensor state before syncing
                fl = mmu.sensor_manager.flags()
                print(
                    f"[SENSORS] before sync: tension(present={int(fl['tension_present'])}, active={int(fl['tension_active'])})  "
                    f"compression(present={int(fl['compression_present'])}, active={int(fl['compression_active'])})"
                )
                mmu.printer.send_event("mmu:synced")

            elif cmd == 'u':
                mmu.printer.send_event("mmu:unsynced")

            elif cmd == 'f':
                if len(parts) < 2:
                    print("usage: f <-1|0|1>")
                else:
                    requested = float(parts[1])
                    # Adjust sensor ACTIVE flags to represent the requested state
                    set_sensors_for_state(mmu, requested)
                    # Compute prior state (after adjustment this is the new effective state)
                    eff = compute_effective_state(mmu)
                    print(f"[EVENT] mmu:sync_feedback {eff:+.0f} (sensors aligned to requested {requested:+.0f})")
                    mmu.printer.send_event("mmu:sync_feedback", eff)

            elif cmd == 'm':
                if len(parts) < 2:
                    print("usage: m <mm>")
                else:
                    delta = float(parts[1])
                    before = extruder.position
                    extruder.position += delta
                    now = time.time()
                    reactor.run_due(now)
                    mgr._check_extruder_movement(now)
                    print(f"[MOVE] extruder {before:.2f} -> {extruder.position:.2f} (Δ={delta:+.2f})")

            elif cmd == 'l':
                print("set_rotation_distance log:")
                for ts, rd in mmu.rd_log():
                    print(f"  {time.strftime('%H:%M:%S', time.localtime(ts))}  rd={rd:.4f}")

            elif cmd == 'sensor':
                if len(parts) != 5:
                    print("usage: sensor <tension_present 0|1> <tension_active 0|1> <compression_present 0|1> <compression_active 0|1>")
                else:
                    # Compute prior effective state
                    prev_state = compute_effective_state(mmu)

                    tp = bool(int(parts[1])); ta = bool(int(parts[2]))
                    cp = bool(int(parts[3])); ca = bool(int(parts[4]))
                    sm = mmu.sensor_manager
                    sm.set_present(mmu.SENSOR_TENSION, tp)
                    sm.set_active(mmu.SENSOR_TENSION, ta)
                    sm.set_present(mmu.SENSOR_COMPRESSION, cp)
                    sm.set_active(mmu.SENSOR_COMPRESSION, ca)
                    fl = sm.flags()
                    print(f"[SENSORS] set: tension(present={int(fl['tension_present'])}, active={int(fl['tension_active'])})  "
                          f"compression(present={int(fl['compression_present'])}, active={int(fl['compression_active'])})")

                    # Compute new effective state and fire event if changed
                    new_state = compute_effective_state(mmu)
                    if new_state != prev_state:
                        print(f"[EVENT] mmu:sync_feedback {new_state:+.0f} (from {prev_state:+.0f})")
                        mmu.printer.send_event("mmu:sync_feedback", new_state)

            elif cmd == 'status':
                pass  # fall through to always-print status

            else:
                print("Unknown command. Type 'h' for help.")

        except Exception as e:
            print("[EXC] ", e)

        # Always show current rotation_distance and state after every command
        show_status(mgr, mmu, extruder)
        print()

    print("Bye!")

if __name__ == "__main__":
    main()
