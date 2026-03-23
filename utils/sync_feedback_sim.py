# -*- coding: utf-8 -*-
#
# Happy Hare MMU Software
# Simulator/CLI for the (movement-based) filament tension controller
#
# Simulator invocation:
#  python -m utils.sync_feedback.py
#    --sensor-type=[P|D|CO|TO]
#    --buffer-range-mm=      (default=8.0)
#    --buffer-max-range-mm   (default=12.0)
#    --initial-sensor=[random|neutral]
#    --stride-mm=10          (normal extruder movement between updates)
#    --tick-dt-s             (default dt used only for manual 'tick', 'clog' and 'tangle', default: 1.0)
#    --rd-start              (starting extruder rotation distance, default: 20.0)
#    --sensor-lag-mm         (lag in sensor reacting to movement, default: 0)
#    --chaos=2               (simulates friction and jerky movements, multiple of buffer_max_range)
#    --sample-error=0.25     (simulates "late" updates from extruder movement Eg 0.25 = 100%-125% of stride)
#    --switch-hysteresis=0.2 (factor based on buffer_range)
#    --use-twolevel          (forces P type sensors to operation in twolevel mode instead of EKF default)
#    --log-debug             (display debug trace log entries)
#    --out=<file>            (output PNG filename for plots, default: sim_plot.png)
#    --log=<file>            (simulator json log output, default: sim.jsonl)
# Use --chaos=0 sample-error=0 for "pure" simulation
#
# Grpahing logs:
#  python sync_feedback.py --plot=<sim.jsonl>
#
#
# Requires: mmu_sync_feedback_manager.py (SyncControllerConfig, SyncController)
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import argparse
import json
import math
import os
import time
import random
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np

from mmu_sync_controller import SyncControllerConfig, SyncController
#try:
#    from mmu_sync_controller import SyncControllerConfig, SyncController
#except Exception as e:
#    print("Could not load SyncConfig module. Simulation not possible.")

# ---------- Optional readline for command history (Up/Down arrows) ----------
HAVE_READLINE = False
try:
    import readline  # POSIX / macOS
    HAVE_READLINE = True
except Exception:
    try:
        import pyreadline as readline  # legacy Windows
        HAVE_READLINE = True
    except Exception:
        readline = None  # plain input() fallback

def _setup_readline(history_limit: int = 500):
    if not HAVE_READLINE:
        return
    try:
        readline.set_history_length(history_limit)
        try:
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass
    except Exception:
        pass

def _add_history(line: str):
    if not HAVE_READLINE:
        return
    try:
        n = readline.get_current_history_length()
        if n == 0 or readline.get_history_item(n) != line:
            readline.add_history(line)
    except Exception:
        pass

# ------------------------------ JSON Logger -----------------------------

class SimLogger:
    """Append-only JSONL logger that clears on startup; tick starts at 0."""
    def __init__(self, path: str = "sim.jsonl", truncate_on_init: bool = True):
        self.path = os.path.abspath(path)
        self.records_in_session: List[Dict[str, Any]] = []
        if truncate_on_init:
            self.clear()
        else:
            self.tick = self._load_last_tick_plus_one()

    def clear(self):
        with open(self.path, "w", encoding="utf-8"):
            pass
        self.tick = 0
        self.records_in_session.clear()

    def _load_last_tick_plus_one(self) -> int:
        last_tick = -1
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                            if isinstance(rec, dict):
                                t = rec.get("meta", {}).get("log_tick", None)
                                if t is not None:
                                    last_tick = max(last_tick, int(t))
                        except Exception:
                            continue
            except Exception:
                pass
        return last_tick + 1

    def append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        rec = dict(record)
        rec.setdefault("meta", {})
        rec["meta"]["log_tick"] = self.tick
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self.records_in_session.append(rec)
        self.tick += 1
        return rec

    def write_header(self, header: Dict[str, Any]) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"header": header}) + "\n")
        except Exception:
            pass

    def load_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not os.path.exists(self.path):
            return out
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out

# -------------------------- Printer (physical model) --------------------

class SimplePrinterModel:
    """
    Printer model (normalized spring x_true; spring_mm = x_true * (buffer_range_mm/2)):

      x_true[k+1] = x_true[k] + (2 / buffer_range_mm) * Δ_rel_true

    where the *relative* motion between the filament and the gear during an
    extruder command d_ext is:

      Δ_rel_true = d_ext * (extruder_rd_true / rd_prev - 1.0)

    Notes:
      • If rd_true == rd_prev  → Δ_rel_true = 0 (spring stays flat).
      • If rd_true >  rd_prev  → (ratio - 1) > 0:
        - d_ext > 0 (extrude)  → compression (+x / +spring)
        - d_ext < 0 (retract)  → tension     (−x / −spring)
      • rd_prev must be the RD in effect *for this step* (before it changes).
    """
    def __init__(
        self,
        controller: SyncController,
        extruder_rd_true: Optional[float] = None,
        initial_spring_mm: float = 0.0,
        chaos: float = 0.0,
        hysteresis: float = 0.0,
    ):
        self.ctrl = controller
        self.buffer_range_mm = controller.cfg.buffer_range_mm
        self.buffer_max_range_mm = controller.cfg.buffer_max_range_mm

        # Normalization factors / limits
        self.K = 2.0 / self.buffer_range_mm                     # mm_rel -> normalized x
        self._norm_clip = max(1e-9, self.buffer_max_range_mm / self.buffer_range_mm)

        # State: true normalized spring position and measured proxy
        x0 = (2.0 * initial_spring_mm) / self.buffer_range_mm
        self.x_true = max(-self._norm_clip, min(self._norm_clip, x0))
        rd_ref = controller.cfg.rd_start
        self.extruder_rd_true = extruder_rd_true if extruder_rd_true is not None else rd_ref

        self.chaos = max(0.0, min(2.0, float(chaos)))
        self.x_meas = self.x_true

        # Hysteresis fraction for switch sensors (0..0.4 typical)
        self.hysteresis = max(0.0, min(0.4, float(hysteresis)))

        # Last digital switch state (for D/CO/TO). Initialize from current x_meas.
        self._last_switch_value: int = 0
        self._bootstrap_switch_state()

        # Monotonic simulation clock (seconds since start). Commands must use/advance this.
        self.time_s: float = 0.0

    # ---------- Small physics helpers (new) ----------
    def ratio_true_over_prev(self, rd_prev: float) -> float:
        """Return RD_true / rd_prev with a tiny floor to avoid division by ~0."""
        return self.extruder_rd_true / max(1e-9, float(rd_prev))

    def delta_rel(self, rd_prev: float, d_ext_mm: float) -> float:
        """
        Relative filament-vs-gear motion (mm) for this chunk, using the *current* hardware RD
        and the controller RD *in effect* during this chunk (rd_prev).
        """
        ratio = self.ratio_true_over_prev(rd_prev) # RD_true / RD_prev
        return float(d_ext_mm) * (ratio - 1.0)

    def gain_per_mm(self, rd_in_effect: float) -> float:
        """
        Normalized spring change per +1 mm of extruder motion for the *current* tick.
        g = (2 / buffer_range_mm) * (rd_true / rd_in_effect - 1.0)
        Positive g means +x (compression) for +d_ext when rd_true > rd_in_effect.
        """
        ratio = self.extruder_rd_true / max(1e-9, float(rd_in_effect))
        return (2.0 / self.buffer_range_mm) * (ratio - 1.0)

    def advance_physics(self, rd_in_effect: float, d_ext_mm: float) -> None:
        """
        Advance the physical spring using the RD that was in effect during this motion.
        Δx = g * d_ext_mm  (with g from gain_per_mm)
        """
        self.x_true += self.gain_per_mm(rd_in_effect) * float(d_ext_mm)
        self.x_true = min(max(self.x_true, -self._norm_clip), self._norm_clip) # clip to physical limits

    def apply_motion(self, d_ext: float, rd_used: float) -> float:
        """
        Advance the physical spring for a single commanded extruder motion (in mm),
        using the rotation distance that was actually in effect for that motion.
    
        Physics (normalized spring x_true; spring_mm = x_true * (buffer_range_mm/2)):
          gear_mm = (extruder_rd_true / rd_used) * d_ext
          Δ_rel   = gear_mm - d_ext                     # positive = compression
          x_true += (2/BR) * Δ_rel

        Returns the Δ_rel (mm) applied this step (useful for debugging).
        """
        rd_used = max(1e-9, float(rd_used))
        ratio = self.extruder_rd_true / rd_used
        delta_rel = (ratio * d_ext) - d_ext            # = d_ext * (ratio - 1)
        self.x_true += self.K * delta_rel
        self.x_true = min(max(self.x_true, -self._norm_clip), self._norm_clip)
        return delta_rel

    # ---------- Basic properties ----------
    def set_extruder_rd_true(self, rd_true: float):
        self.extruder_rd_true = float(rd_true)

    def spring_mm(self) -> float:
        return self.x_true * (self.buffer_range_mm / 2.0)

    # ---------- Simulation clock ----------
    def get_time_s(self) -> float:
        """Return the current absolute simulation time (seconds, monotonic)."""
        return float(self.time_s)

    def advance_time(self, dt_s: float) -> float:
        """Advance the internal clock by a non-negative dt (seconds) and return new time."""
        try:
            dt = max(0.0, float(dt_s))
        except Exception:
            dt = 0.0
        self.time_s += dt
        return self.time_s

    def reset_time(self, to: float = 0.0) -> None:
        """Reset the internal clock to a non-negative value (default 0)."""
        self.time_s = max(0.0, float(to))

    # ---------- Sensor modeling ----------
    def get_switch_thresholds(self) -> Tuple[float, float]:
        """
        Returns (thr_on, thr_off) in normalized units.
        thr_on  = magnitude to TRIGGER (go from 0 to active state)
        thr_off = magnitude to UNTRIGGER (leave the active state)
        """
        thr_mid = float(self.ctrl.cfg.flowguard_extreme_threshold)
        thr_on  = min(self._norm_clip, thr_mid * (1.0 + self.hysteresis))
        thr_off = max(0.0, min(thr_on, thr_mid * (1.0 - self.hysteresis)))
        return (thr_on, thr_off)

    def _bootstrap_switch_state(self) -> None:
        """Initialize last switch value from current x_meas against midpoint thresholds."""
        st = self.ctrl.cfg.sensor_type
        thr = float(self.ctrl.cfg.flowguard_extreme_threshold)
        if st == "D":
            if self.x_meas >= thr:
                self._last_switch_value = 1
            elif self.x_meas <= -thr:
                self._last_switch_value = -1
            else:
                self._last_switch_value = 0
        elif st == "CO":
            self._last_switch_value = 1 if self.x_meas >= thr else 0
        elif st == "TO":
            self._last_switch_value = -1 if self.x_meas <= -thr else 0
        else:
            self._last_switch_value = 0

    def measure(self) -> float | int:
        """
        Return a sensor reading (float for 'P', int for 'D'/'CO'/'TO'),
        optionally with stick–slip style lag ('chaos').
        """
        if self.chaos <= 1e-12:
            self.x_meas = self.x_true
        else:
            # Move measured position toward true with a random "jerk".
            jerk_mm_max = self.chaos * self.buffer_max_range_mm
            draw_mm = random.random() * jerk_mm_max
            if draw_mm >= (self.buffer_max_range_mm - 1e-12):
                self.x_meas = self.x_true
            else:
                gap_norm = self.x_true - self.x_meas
                gap_mm = abs(gap_norm) * (self.buffer_range_mm / 2.0)
                if gap_mm > 1e-12:
                    move_mm = min(draw_mm, gap_mm)
                    move_norm = (2.0 / self.buffer_range_mm) * move_mm
                    self.x_meas += math.copysign(move_norm, gap_norm)
                    self.x_meas = min(max(self.x_meas, -self._norm_clip), self._norm_clip)

        if self.ctrl.cfg.sensor_type == "P":
            return max(-1.0, min(1.0, self.x_meas))

        # Switch sensors with hysteresis
        thr_on, thr_off = self.get_switch_thresholds()
        st = self.ctrl.cfg.sensor_type
        x = self.x_meas
        s_prev = self._last_switch_value
        s_new = s_prev

        if st == "D":
            # 3-state: -1, 0, +1
            if s_prev == 0:
                if x >= +thr_on:
                    s_new = 1
                elif x <= -thr_on:
                    s_new = -1
            elif s_prev == 1:
                if x <= +thr_off:
                    s_new = 0
            elif s_prev == -1:
                if x >= -thr_off:
                    s_new = 0

        elif st == "CO":
            # 0 → 1 at +thr_on; 1 → 0 at +thr_off
            if s_prev == 0:
                if x >= +thr_on:
                    s_new = 1
            else:  # s_prev == 1
                if x <= +thr_off:
                    s_new = 0

        elif st == "TO":
            # 0 → -1 at -thr_on; -1 → 0 at -thr_off
            if s_prev == 0:
                if x <= -thr_on:
                    s_new = -1
            else:  # s_prev == -1
                if x >= -thr_off:
                    s_new = 0

        self._last_switch_value = s_new
        return s_new

# ------------------------------- Plotting -------------------------------

def plot_progress(
    records: List[Dict[str, Any]],
    out_path: Optional[str] = None,
    dt_s: Optional[float] = None,
    sensor_label: Optional[str] = None,   # "D", "CO", "TO" or "P"
    stop_on_fg_trip: bool = False,
    rd_start: Optional[float] = None,
    show_rd_true: bool = True,
    show_ticks: bool = False,
    show_mm_axis: bool = True,
    show_tick_times: bool = True,
    mm_axis_mode: str = "abs",            # "abs" or "signed"
    summary_txt: str = "",
    title_txt: str = "Filament Sync Simulation",
):
    """
    Plot RD (left axis), sensor reading/UI (right axis), Bowden/Buffer spring (2nd right), and autotune-recommendation markers.
    """
    if not records:
        raise ValueError("No records to plot.")

    RD_COLOR        = "#0000ff80" # "tab:blue"   alpha=0.5
    RD_TRUE_COLOR   = "#4c4c4c20" # grey 0.3     alpha=0.125
    RD_REF_COLOR    = "#00800080" # green
    SENSOR_COLOR    = "#ff7f0e80" # "tab:orange" alpha=0.5
    SENSOR_UI_COLOR = "tab:brown"
    SPRING_COLOR    = "#ff000080" # red          alpha=0.5
    C_EST_COLOR     = "#30303080" # grey         alpha=0.5
    X_EST_COLOR     = "#bcbd2280" # "tab:olive"  alpha=0.5
    AUTOTUNE_COLOR  = "#2ca02cff" # "tab:green"  alpha=1.0
    TICK_MARK_COLOR = "#40404080" # grey         alpha=0.5
    HEADROOM_COLOR  = "#e377c280" # "tab:pink"   alpha=0.5
    LOWLIGHT_TEXT   = "0.5"       # grey

    # On RD axis
    BOLD_LW         = 3.0
    MAIN_LW         = 2.0
    DEBUG_LW        = 1.0
    SECONDARY_LW    = 1.0

    HEADER_ZORDER   = 999
    TRIPBOX_ZORDER  = 1000
    AUTOTUNE_ZORDER = 1001

    # Prefer absolute t_s if present, else reconstruct from dt_s grid
    have_ts = all(("meta" in r) and ("t_s" in r["meta"]) for r in records)
    if have_ts:
        t_axis = [float(r["meta"]["t_s"]) for r in records]
    else:
        if dt_s is None:
            dt_s = records[0].get("meta", {}).get("dt_s", 1.0)
        t_axis = []
        t_acc = 0.0
        for r in records:
            step_dt = r.get("meta", {}).get("dt_s")
            if step_dt is None:
                step_dt = dt_s
            t_axis.append(t_acc)
            t_acc += float(step_dt)

    # Flowguard
    clog           = [ (str(r["output"]["flowguard"].get("trigger","")) == "clog")   for r in records ]
    tangle         = [ (str(r["output"]["flowguard"].get("trigger","")) == "tangle") for r in records ]
    first_trip_idx = next((i for i, (c, t) in enumerate(zip(clog, tangle)) if c or t), None)
    last_trip_idx  = next((i for i in range(len(records) - 1, -1, -1) if clog[i] or tangle[i]), None)

    # Capture trip kind/reason *before* truncation so we can show a box
    trip_kind = None
    trip_reason = None
    trip_index = first_trip_idx if stop_on_fg_trip else last_trip_idx if last_trip_idx == len(records) - 1 else None
    if trip_index is not None:
        trip_kind = "CLOG" if clog[trip_index] else "TANGLE"
        trip_reason = records[trip_index].get("output", {}).get("flowguard", {}).get("reason")

    # Truncate all series on limit
    end_idx = (first_trip_idx + 1) if (stop_on_fg_trip and first_trip_idx is not None) else len(records)
    t_axis = t_axis[:end_idx]

    # Helper for optional series
    def get_optional_series(records, key, end_idx, section="output"):
        vals = [r.get(section, {}).get(key) for r in records[:end_idx]]
        return [] if all(v is None for v in vals) else vals

    # Core series
    rd              = [r["output"]["rd_current"] for r in records[:end_idx]]
    rd_tuned        = [r["output"].get("rd_tuned", None) for r in records[:end_idx]]
    z               = [r["input"]["sensor"] for r in records[:end_idx]]
    z_ui            = [r["output"]["sensor_ui"] for r in records[:end_idx]]
    mm_deltas       = [r["input"]["d_mm"] for r in records[:end_idx]]

    # Normalized headroom: -1 = full, 0 = trigger
    max_r_headroom  = max(r["output"]["flowguard"]["relief_headroom"] for r in records[:end_idx])
    fg_r_headroom   = [-r["output"]["flowguard"]["relief_headroom"] / max_r_headroom for r in records[:end_idx]]

    # Optional debug series
    x_est           = get_optional_series(records, "x_est", end_idx)
    c_est           = get_optional_series(records, "c_est", end_idx)
    rd_target       = get_optional_series(records, "rd_target", end_idx)
    rd_ref          = get_optional_series(records, "rd_ref", end_idx)
    rd_ref_smoothed = get_optional_series(records, "rd_ref_smoothed", end_idx)
    rd_note         = get_optional_series(records, "rd_note", end_idx)

    has_x_est       = any(v is not None for v in x_est)
    has_c_est       = any(v is not None for v in c_est)
    has_rd_target   = any(v is not None for v in rd_target)
    has_rd_ref      = any(v is not None for v in rd_ref)
    has_rd_ref_smoothed = any(v is not None for v in rd_ref_smoothed)

    # Simulator only series
    rd_true         = get_optional_series(records, "rd_true", end_idx, section="truth")
    spring_mm       = get_optional_series(records, "spring_mm", end_idx, section="truth")

    has_rd_true = any(v is not None for v in rd_true)
    has_spring_mm = any(v is not None for v in spring_mm)

    # Autotune markers (where controller reported a new default RD)
    autotune_recommendation = []
    for i, r in enumerate(records[:end_idx]):
        auto_rd = r.get("output", {}).get("autotune", {}).get("rd")
        if auto_rd is not None:
            autotune_recommendation.append((i, float(auto_rd)))

    # RD update "note" markers
    rd_notes = []
    for i, n in enumerate(rd_note[:end_idx]):
        if rd_note[i] is not None:
            rd_notes.append((i, rd[i]))

    # FlowGuard armed markers
    fg_armed = []
    has_fg_armed = False
    for i, r in enumerate(records[:end_idx]):
        armed = r.get("output", {}).get("flowguard", {}).get("active")
        if armed is False:
            has_fg_armed = True
            fg_armed.append((i, -1.0))

    # Header sparators representing reset() points in controller
    header_break_idxs = [
        i for i, r in enumerate(records[:end_idx])
        if r.get("meta", {}).get("header_break") is True
    ]

    # Main axes setup -------------------------------------------------------------
    fig, ax_rd = plt.subplots(figsize=(12, 6), constrained_layout=True)
    ax_sensor = ax_rd.twinx()

    ax_rd.set_ylabel("Rotation Distance (mm)")
    ax_rd.set_xlabel("Time (s)")
    ax_rd.grid(True, axis="both", alpha=0.3)

    rd0 = rd[0] if rd else 0.0
    rd_min_required = rd0 * 0.75
    rd_max_required = rd0 * 1.25
    candidates_min = [rd_min_required]
    candidates_max = [rd_max_required]
    if rd:
        candidates_min.append(min(rd)); candidates_max.append(max(rd))
    if show_rd_true and rd_true and all(v is not None for v in rd_true):
        candidates_min.append(min(rd_true)); candidates_max.append(max(rd_true))
    rd_min = min(candidates_min)
    rd_max = max(candidates_max)
    if rd_min == rd_max:
        span = abs(rd0) * 0.25 if rd0 != 0 else 1.0
        rd_min, rd_max = rd0 - span, rd0 + span
    ax_rd.set_ylim(rd_min, rd_max)

    # Sensor axes/grid
    ax_sensor.set_ylabel("Sensor")
    ax_sensor.set_ylim(-1.1, 1.3)
    #ax_sensor.axhline(0.0, linewidth=1.0, alpha=0.4, color="0.5", zorder=0)
    ax_sensor.grid(True, axis="y", alpha=0.2)

    sensor_txt = f"Type {sensor_label}" if sensor_label else "Sensor"
    title_txt = f"{title_txt} — {sensor_txt}"
    if trip_kind is not None:
        title_txt += f" | TRIPPED at {trip_kind}"
    ax_rd.set_title(title_txt, pad=18) # Extra space above the top x-axis
    ax_rd.text(0.5, 1.1, summary_txt, transform=ax_rd.transAxes, ha="center", va="bottom", fontsize=6, color="0.4", wrap=True, zorder=999)


    # Plots against RD axis (left) ------------------------------------------------
    if len(t_axis) >= 2:
        # Core...
        if show_ticks:
            ax_rd.plot(t_axis, rd,              label="rotation_distance",   linestyle="-",  linewidth=MAIN_LW,  color=RD_COLOR, marker="o", markersize=2.5, markevery=1)
        else:
            ax_rd.plot(t_axis, rd,              label="rotation_distance",   linestyle="-",  linewidth=MAIN_LW,  color=RD_COLOR)
        ax_rd.plot(t_axis, rd_tuned,            label="rd_tuned",            linestyle="-.", linewidth=DEBUG_LW, color=RD_COLOR)

        # Debug...
        if has_rd_true and show_rd_true:
            ax_rd.plot(t_axis, rd_true,         label="rd_true (simulator)", linestyle="-",  linewidth=MAIN_LW,  color=RD_TRUE_COLOR)
        if has_rd_ref:
            ax_rd.plot(t_axis, rd_ref,          label="rd_ref",              linestyle="-",  linewidth=DEBUG_LW, color=RD_REF_COLOR)
        if has_rd_ref_smoothed:
            ax_rd.plot(t_axis, rd_ref_smoothed, label="rd_ref_smoothed",     linestyle="--", linewidth=DEBUG_LW, color=RD_REF_COLOR)
        if has_rd_target:
            ax_rd.plot(t_axis, rd_target,       label="rd_target",           linestyle="-.", linewidth=DEBUG_LW, color=RD_REF_COLOR)

    elif t_axis:
        ax_rd.scatter(t_axis, rd, label="rotation_distance", color=RD_COLOR, zorder=3)
        if show_rd_true and has_rd_true:
            ax_rd.scatter(t_axis, rd_true, label="rd_true (simulator)", color=RD_TRUE_COLOR, zorder=3)

    # Autotune dots (where controller reported a new default RD)
    if autotune_recommendation:
        t_marks = [t_axis[i] for (i, _) in autotune_recommendation]
        y_marks = [v for (_, v) in autotune_recommendation]
        ax_rd.scatter(t_marks, y_marks, marker="o", s=34, color=AUTOTUNE_COLOR, edgecolors="black", linewidths=0.6, label="RD autotune", zorder=AUTOTUNE_ZORDER)

    # RD note markers
    if rd_notes:
        t_marks = [t_axis[i] for (i, _) in rd_notes]
        y_marks = [v for (_, v) in rd_notes]
        ax_rd.scatter(t_marks, y_marks, marker="x", s=15, color=RD_COLOR, label="rd_note (extreme bias)")

    # Flowguard terminator line
    for i, (is_clog, is_tangle) in enumerate(zip(clog[:end_idx], tangle[:end_idx])):
        if is_clog or is_tangle:
            ax_rd.axvline(t_axis[i], linestyle="-.", linewidth=BOLD_LW, alpha=0.7, color="red")

    # Draw header separators
    for i in header_break_idxs:
        ax_rd.axvline(
            t_axis[i],
            linestyle="--",
            linewidth=1.5,
            color="0.25",
            alpha=0.6,
            zorder=HEADER_ZORDER,
        )

    # Plots against sensor axis (right) -------------------------------------------
    if len(t_axis) >= 2:
        # Core...
        ax_sensor.plot(t_axis, z,         label="sensor_reading", linestyle="-",  linewidth=MAIN_LW,      color=SENSOR_COLOR)
        ax_sensor.plot(t_axis, z_ui,      label="sensor_ui",      linestyle=":",  linewidth=SECONDARY_LW, color=SENSOR_UI_COLOR)

        # Debug...
        if has_x_est:
            ax_sensor.plot(t_axis, x_est, label="x_est (debug)",  linestyle="--", linewidth=DEBUG_LW,     color=X_EST_COLOR)
        if has_c_est:
            ax_sensor.plot(t_axis, c_est, label="c_est (debug)",  linestyle="--", linewidth=DEBUG_LW,     color=C_EST_COLOR)

        # FlowGuard: Headroom normalized and reflected to fit on this scale
        ax_sensor.plot(t_axis, fg_r_headroom, label="flowguard headroom (norm. relief)", linestyle="-.",  linewidth=SECONDARY_LW, color=HEADROOM_COLOR)

    elif t_axis:
        ax_sensor.scatter(t_axis, z,      label="sensor_reading", color=SENSOR_COLOR, zorder=2)
        ax_sensor.scatter(t_axis, z_ui,   label="sensor_ui",      color=SENSOR_COLOR, zorder=2)

    # FlowGuard armed markers
    if has_fg_armed:
        t_marks = [t_axis[i] for (i, _) in fg_armed]
        y_marks = [v for (_, v) in fg_armed]
        ax_sensor.scatter(t_marks, y_marks, marker="x", s=15, color=HEADROOM_COLOR, label="flowguard not active")


    # Plots against bowden/buffer spring (far right) ------------------------------
    if has_spring_mm:
        ax_spring = ax_rd.twinx()
        ax_spring.spines["right"].set_position(("axes", 1.08))
        ax_spring.spines["right"].set_visible(True)
        ax_spring.spines["right"].set_color(LOWLIGHT_TEXT)
        ax_spring.set_frame_on(True)
        ax_spring.set_facecolor("none")

        ax_spring.axhline(0.0, linewidth=1.0, alpha=0.2, color="grey")
        ax_spring.set_ylabel("Simulated bowden/buffer spring (mm)", color=LOWLIGHT_TEXT, fontsize=8)
        ax_spring.tick_params(axis="y", colors=LOWLIGHT_TEXT, which="both", width=1, length=4)

        y_spring = [float("nan") if v is None else float(v) for v in spring_mm]
        finite_vals = [v for v in y_spring if not (math.isnan(v) or math.isinf(v))]
        span = max(abs(min(finite_vals)), abs(max(finite_vals))) if finite_vals else 1.0
        lim = max(span * 1.1, 0.5)
        ax_spring.set_ylim(-lim, +lim)

        if len(t_axis) >= 2:
            ax_spring.plot(t_axis, y_spring,    label="bowden spring (simulator)", linestyle=":", linewidth=DEBUG_LW, color=SPRING_COLOR)
        elif t_axis:
            ax_spring.scatter(t_axis, y_spring, label="bowden spring (simulator)", color=SPRING_COLOR, zorder=2)


    # Top non-linear extruder mm x-axis -------------------------------------------
    if show_mm_axis and len(t_axis) >= 2 and len(mm_deltas) >= 1:
        # Fixed-distance ticks mapped to main time axis
        t_series = np.asarray(t_axis, dtype=float)

        mode = (mm_axis_mode or "abs").lower()
        if mode == "abs":
            mm_series = np.cumsum(np.abs(mm_deltas)).astype(float)
            top_label = "Extruder distance (mm)"
            # ensure strictly increasing for stable inverse
            if np.any(np.diff(mm_series) <= 0):
                mm_series = mm_series + 1e-12 * np.arange(mm_series.size)
        else:
            # NOTE signed displacement may be non-monotonic → inverse not unique.
            # We'll still place ticks using the ABS distance for spacing,
            # but show signed labels at those times.
            mm_series_signed = np.cumsum(mm_deltas).astype(float)
            mm_series = np.cumsum(np.abs(mm_deltas)).astype(float) # for monotonic inverse
            top_label = "Extruder displacement (mm)"
            if np.any(np.diff(mm_series) <= 0):
                mm_series = mm_series + 1e-12 * np.arange(mm_series.size)

        # Make sure first label is exactly 0 at t0
        if mm_series.size:
            mm_series[0] = 0.0

        # Add small symmetric margin so both axes inset equally
        ax_rd.margins(x=0.03)

        # Create the top axis that shares time-limits with the bottom
        ax_mm = ax_rd.twiny()
        ax_mm.set_xlim(ax_rd.get_xlim())
        ax_mm.spines["top"].set_color(LOWLIGHT_TEXT)
        ax_mm.tick_params(axis="x", colors=LOWLIGHT_TEXT, pad=6, labelsize=6)
        ax_mm.xaxis.label.set_color(LOWLIGHT_TEXT)
        ax_mm.set_xlabel(top_label, fontsize=8)
        ax_mm.xaxis.get_offset_text().set_size(6)

        # Fixed-distance tick placement
        mm_end = float(mm_series[-1])

        def _nice_step(x):
            # 1–2–5 stepping
            if x <= 0:
                return 1.0
            exp = math.floor(math.log10(x))
            frac = x / (10 ** exp)
            if frac <= 1.0:
                nice = 1.0
            elif frac <= 2.0:
                nice = 2.0
            elif frac <= 5.0:
                nice = 5.0
            else:
                nice = 10.0
            return nice * (10 ** exp)

        # Aim for ~10 major ticks
        desired_ticks = 10 # tweak: 8, 10, 12, ...
        target = max(1.0, mm_end / desired_ticks)
        mm_step = _nice_step(target)

        # Build distance ticks: 0, step, 2*step, ...
        m_ticks = np.arange(0.0, mm_end + 0.5 * mm_step, mm_step)

        # Optionally drop the terminal tick if it lands essentially at the end
        # to avoid the last label colliding with the frame.
        if len(m_ticks) >= 2 and abs(m_ticks[-1] - mm_end) < 0.25 * mm_step:
            m_ticks = m_ticks[:-1]

        # Map distance ticks → time via inverse interp
        t_ticks = np.interp(m_ticks, mm_series, t_series)

        # Keep ticks within the current (padded) visible time range
        xmin, xmax = ax_rd.get_xlim()
        keep = (t_ticks >= xmin) & (t_ticks <= xmax)
        t_ticks = t_ticks[keep]
        m_ticks = m_ticks[keep]

        # Labels: show signed value at those times if in 'signed' mode
        if mode == "signed":
            signed_vals = np.interp(t_ticks, t_series, mm_series_signed)
            labels = [f"{v:.0f}" if mm_step >= 5 else f"{v:.1f}" for v in signed_vals]
        else:
            labels = [f"{m:.0f}" if mm_step >= 5 else f"{m:.1f}" for m in m_ticks]

        ax_mm.set_xticks(t_ticks)
        ax_mm.set_xticklabels(labels)


    # Plot times where an actual movement happened (ticks) ------------------------
    if show_tick_times:
        tick_times = [t for t, d in zip(t_axis, mm_deltas)]

        # Keep them just under the top, and a bit lower if the top distance axis is shown
        y_top = 0.99
        h     = 0.015   # height = 1.5% of axes
        y0, y1 = max(0, y_top - h), min(1, y_top)

        for x in tick_times:
            ax_rd.axvline(x, ymin=y0, ymax=y1, color=TICK_MARK_COLOR, linewidth=1.0, clip_on=False, label="_nolegend_")


    # Flowguard trip banner -------------------------------------------------------
    if trip_kind is not None:
        ax_rd.text(
            0.01, 0.98,
            f"{trip_kind} reason:\n{trip_reason}",
            transform=ax_rd.transAxes,
            va="top", ha="left",
            fontsize=9,
            wrap=True,
            bbox=dict(boxstyle="round", facecolor="0.92", edgecolor="0.6", alpha=0.8),
            zorder=TRIPBOX_ZORDER,
            clip_on=False,
        )


    # Legend box ------------------------------------------------------------------
    rd_lines,     rd_labels     = ax_rd.get_legend_handles_labels()
    sensor_lines, sensor_labels = ax_sensor.get_legend_handles_labels()
    if has_spring_mm:
        spring_lines, spring_labels = ax_spring.get_legend_handles_labels()
    else:
        spring_lines, spring_labels = [], []

    legend = ax_sensor.legend(
        rd_lines + sensor_lines + spring_lines,
        rd_labels + sensor_labels + spring_labels,
        loc="lower left",
        ncol=3,
        fontsize=8,
        framealpha=0.6,
        borderpad=0.2,
        labelspacing=0.25,
        handlelength=2.0,
        handletextpad=0.4,
        columnspacing=1.2,
    )

    # Plot ------------------------------------------------------------------------
    if out_path:
        plt.savefig(out_path, dpi=150)
        plt.close(fig)
        print(f"Saved plot to {out_path}")
    else:
        plt.show()


# ------------------------------ CLI Helpers ----------------------------

def _print_cli_help():
    print("""
Commands (history enabled — use Up/Down arrows):
  sim <v_mm_s> <time_s> [rd]             - Simulate average extruder speed for time. Uses --stride-mm chunk size per update.
  inout <d_ext_mm> <inter> [v_mm_s [rd]] - Simulate average extruder speed for time. Uses --stride-mm chunk size per update.
  t|tick <d_ext_mm> [<sensor|auto>]      - Manual one update. If sensor omitted or 'auto', uses simulator sensor state.
  rd <value>                             - Set printer's *true* extruder rotation_distance immediately.
  clog                                   - Realistic compression-extreme test (build + stuck), stop on FlowGuard.
  tangle                                 - Realistic tension-extreme test (build + stuck), stop on FlowGuard.
  clear                                  - Reset controller (full), printer spring, and jsonl log file (tick=0).
  p | plot                               - Save plot to sim_plot.png (always saves to file).
  d | display                            - Display plot window (does not save).
  status                                 - Show controller/printer state
  quit | q                               - Exit
""")

def _summary_txt(ctrl: SyncController, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard: Optional[Dict[str, Any]], spring_mm: float):
    autotune_str = "N/A" if last_autotune_rd is None else f"{last_autotune_rd:.4f}"
    sensor_str = "N/A" if last_sensor is None else (f"{last_sensor:.3f}" if isinstance(last_sensor, float) else str(last_sensor))
    sensor_ui_str = "N/A" if last_sensor_ui is None else f"{last_sensor_ui:.3f}"
    if isinstance(last_flowguard, dict) and last_flowguard.get('trigger'):
        fg_str = f"trigger={last_flowguard.get('trigger', '')}, reason={last_flowguard.get('reason')}"
    else:
        fg_str = "N/A"
    return f"RD={ctrl.rd_current:.4f} | Autotune={autotune_str} | sensor={sensor_str} | sensor_ui={sensor_ui_str} | Bowden/Buffer spring={spring_mm:.3f}mm | FlowGuard: {fg_str}"

def _summary_print(ctrl: SyncController, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard: Optional[Dict[str, Any]], spring_mm: float):
    summary_txt = _summary_txt(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, spring_mm)
    print(f"SUMMARY: {summary_txt}")

def _plot_from_log(
    cfg: SyncControllerConfig,
    logger: SimLogger,
    *,
    mode: str,
    out_path: str = "sim_plot.png",
    show_ticks: bool = False,
    show_mm_axis: bool = False,
    mm_axis_mode: str = "abs",
    summary_txt: str = "",
):
    # Drop any header rows from the in-session simulator log
    raw = logger.load_all()
    records = [r for r in raw if not (isinstance(r, dict) and "header" in r)]
    if not records:
        print("jsonl log file is empty; nothing to plot.")
        return

    use_twolevel = cfg.use_twolevel_for_type_p or cfg.sensor_type in ['CO', 'TO']
    twolevel_txt = " (twoLevel)" if use_twolevel else ""
    sensor_txt = f"{cfg.sensor_type}{twolevel_txt}" if cfg.sensor_type else None
    try:
        plot_progress(
            records,
            out_path=out_path if mode == "save" else None,
            sensor_label=sensor_txt,
            rd_start=cfg.rd_start,
            show_rd_true=True,
            show_ticks=show_ticks,
            show_mm_axis=show_mm_axis,
            mm_axis_mode=mm_axis_mode,
            summary_txt=summary_txt
        )
    except Exception as e:
        print(f"Plotting failed: {e}")
        raise e

# -------------------------- Controller log plotting ---------------------

def _load_log_file(path: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Load a controller-generated JSON log.
    Supports JSONL (one JSON object per line) or a single JSON array.
    First object/element may be {"header": {...}}; subsequent entries are {"input": {...}, "output": {...}}.
    Returns (header_dict_or_empty, records_list).
    """
    header: Dict[str, Any] = {}
    records: List[Dict[str, Any]] = []
    pending_break = False

    if not os.path.exists(path):
        raise FileNotFoundError(f"No such file: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()

    if not raw:
        return header, records

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and "header" in obj and not header:
            maybe = obj.get("header")
            if isinstance(maybe, dict):
                header = maybe
            continue

        if isinstance(obj, dict) and "header" in obj and header:
            pending_break = True
            continue

        if isinstance(obj, dict) and ("input" in obj or "output" in obj):
            if pending_break:
                obj.setdefault("meta", {})
                obj["meta"]["header_break"] = True
                pending_break = False
            records.append(obj)

    return header, records

def _plot_log_file(path: str, *, out_path: str, show_ticks: bool, show_mm_axis: bool, mm_axis_mode: str):
    header, raw_records = _load_log_file(path)
    if not raw_records:
        print("Controller log has no data rows to plot.")
        return

    # Build plot-ready records:
    # - Copy each record
    # - Inject per-record meta.dt_s from input.dt_s (preferred)
    # - Fall back to any existing meta.dt_s if input.dt_s is absent (e.g., simulator logs)
    records: List[Dict[str, Any]] = []
    for r in raw_records:
        rr = dict(r)
        try:
            dt = float(r.get("input", {}).get("dt_s"))
        except Exception:
            dt = r.get("meta", {}).get("dt_s", 1.0)  # defensive fallback

        rr_meta = dict(r.get("meta", {}))
        rr_meta["dt_s"] = dt
        rr["meta"] = rr_meta

        records.append(rr)

    # Detect whether truth is present (simulator logs carry this)
    has_truth = any(isinstance(r.get("truth"), dict) for r in raw_records)

    # Pull values used only for labeling/summary if in existing log
    rd_start = header.get("rd_start", None)
    sensor_type = header.get("sensor_type", None)
    twolevel_active = header.get("twolevel_active", None)

    # Compose a small summary from the last record
    last = records[-1]
    sensor_last = last.get("input", {}).get("sensor")
    rd_current_last = last.get("output", {}).get("rd_current")
    sensor_ui_last = last.get("output", {}).get("sensor_ui")
    fg_last = last.get("output", {}).get("flowguard", {})
    # Last non-None autotune rd
    autotune_last = next(
        (r["output"]["autotune"]["rd"]
         for r in reversed(records)
         if r.get("output", {}).get("autotune", {}).get("rd") is not None),
        None,
    )
    spring_mm = last.get("truth", {}).get("spring_mm")

    twolevel_txt = " (twoLevel)" if twolevel_active else ""
    sensor_txt = f"{sensor_type}{twolevel_txt}" if sensor_type else None

    summary_parts = []
    if rd_start is not None and rd_current_last is not None:
        summary_parts.append(f"RD start={rd_start:.4f}, end={rd_current_last:.4f}")
    if autotune_last is not None:
        summary_parts.append(f"Autotune={autotune_last:.4f}")
    if sensor_last is not None:
        try:
            summary_parts.append(f"sensor={float(sensor_last):.3f}")
        except Exception:
            summary_parts.append(f"sensor={sensor_last}")
    if sensor_ui_last is not None:
        summary_parts.append(f"sensor_ui={float(sensor_ui_last):.3f}")
    if spring_mm is not None:
        summary_parts.append(f"Bowden/Buffer spring={float(spring_mm):.3f}mm")
    #if fg_last:
    #    summary_parts.append(f"FlowGuard: clog={fg_last.get('clog')}, tangle={fg_last.get('tangle')}, reason={fg_last.get('reason')}")
    summary_txt = " | ".join(summary_parts)

    # Save… then display...
    for p in [out_path, None]:
        plot_progress(
            records,
            out_path=p,
            dt_s=None,                    # let plot() use per-record meta.dt_s
            sensor_label=sensor_txt,
            rd_start=rd_start,
            show_rd_true=has_truth,       # show truth if present (sim logs)
            show_ticks=show_ticks,
            show_mm_axis=show_mm_axis,
            mm_axis_mode=mm_axis_mode,
            summary_txt=summary_txt,
            title_txt="Debug Sync Plot"
        )

# -------------------------- Extreme Test (realistic) -------------------

def _forced_extreme_test(
    ctrl: SyncController,
    logger: SimLogger,
    printer: SimplePrinterModel,
    kind: str,                 # "clog" or "tangle"
    stride_mm: float,
    dt_s_step: float,
) -> List[Dict[str, Any]]:
    """
    Two-phase extreme test with a printer-side fault prelude.
    Physics is driven by the model helpers so the sign convention is consistent
    with the rest of the simulator.

    - Always feeds forward (+d_ext) in the ramp.
    - "clog" adds *compression* per mm (positive Δx overlay).
    - "tangle" adds *tension* per mm (negative Δx overlay).
    - Always passes an absolute timestamp into ctrl.update().
    """
    cfg = ctrl.cfg
    records: List[Dict[str, Any]] = []

    # Feed length per tick during the test.
    build_mm = max(stride_mm, cfg.buffer_range_mm / 2.0)
    d_ext_build = +abs(build_mm)  # forward extrude for both tests

    # Fault strength (fraction of commanded mm converted into extra relative motion)
    fault_frac = 0.35

    # Normalization and limits
    K = 2.0 / cfg.buffer_range_mm
    norm_limit = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)

    # When do we consider the sensor "pegged" during the ramp?
    peg_thr = max(0.9, cfg.flowguard_extreme_threshold)
    peg_need = 2           # require N consecutive pegged samples
    peg_count = 0

    # Sign of the overlay term added by the fault
    fault_sign = +1.0 if kind == "clog" else -1.0

    # ---------------------------
    # Phase 1: RAMP with a fault
    # ---------------------------
    ramp_ticks_max = 400
    for _ in range(ramp_ticks_max):
        # PRE-MOVE read (for the controller's input at this boundary)
        z_in = printer.measure()

        # Advance time first, then update controller with the commanded motion
        rd_prev = ctrl.rd_current
        sim_time_s = printer.advance_time(dt_s_step)
        out = ctrl.update(sim_time_s, d_ext_build, z_in, simulation=True)

        # Physical evolution for this tick:
        # 1) Base RD-mismatch physics driven by rd_prev
        printer.advance_physics(rd_prev, d_ext_build)

        # 2) Fault overlay: +compression for clog, -tension for tangle
        printer.x_true += K * (fault_sign * fault_frac * d_ext_build)

        # Clip to physical limits
        printer.x_true = min(max(printer.x_true, -norm_limit), norm_limit)

        # Log row
        rec = {
            **out,
            "truth": {
                "rd_true": printer.extruder_rd_true,
                "spring_mm": printer.spring_mm(),
                "x_true": printer.x_true,
                "x_meas": printer.x_meas,
            },
            "meta": {
                "dt_s": dt_s_step,
                "t_s": sim_time_s,
                "phase": "ramp",
                "fault": kind,
            },
        }
        logger.append(rec); records.append(rec)

        # Check pegging
        if cfg.sensor_type == "P":
            pegged_now = (abs(printer.x_true) >= peg_thr)
        elif cfg.sensor_type == "D":
            m = printer.measure()
            pegged_now = (m == +1 and kind == "clog") or (m == -1 and kind == "tangle")
        elif cfg.sensor_type == "CO":
            # CO sees compression side only; for tangle, look at the hidden side via state
            pegged_now = (printer.measure() == 1) if kind == "clog" else (printer.x_true <= -peg_thr)
        else:  # "TO"
            # TO sees tension side only; for clog, look at the hidden side via state
            pegged_now = (printer.measure() == -1) if kind == "tangle" else (printer.x_true >= +peg_thr)

        peg_count = peg_count + 1 if pegged_now else 0
        if peg_count >= peg_need:
            # Snap to the physical extreme so the stuck phase starts fully pegged.
            printer.x_true = (+norm_limit if kind == "clog" else -norm_limit)
            break

        # FlowGuard?
        fg = out["output"]["flowguard"]
        trip_kind = fg.get("trigger")
        if trip_kind:
            trip_kind = trip_kind.upper()
            print(f"FlowGuard {trip_kind} detected during ramp-in")
            ctrl.flowguard.reset()
            return records

    # -----------------------------
    # Phase 2: STUCK (hard jam)
    # -----------------------------
    stuck_ticks_max = 120
    for _ in range(stuck_ticks_max):
        # Force the state to the extreme *before* sampling so measured matches stuck
        printer.x_true = (+norm_limit if kind == "clog" else -norm_limit)
        z_in = printer.measure()

        rd_prev = ctrl.rd_current
        sim_time_s = printer.advance_time(dt_s_step)
        out = ctrl.update(sim_time_s, d_ext_build, z_in, simulation=True)

        # Keep it pinned (no need to evolve physics here; the jam dominates)
        printer.x_true = (+norm_limit if kind == "clog" else -norm_limit)

        rec = {
            **out,
            "truth": {
                "rd_true": printer.extruder_rd_true,
                "spring_mm": printer.spring_mm(),
                "x_true": printer.x_true,
                "x_meas": printer.x_meas,
            },
            "meta": {
                "dt_s": dt_s_step,
                "t_s": sim_time_s,
                "phase": "stuck",
                "fault": kind,
            },
        }
        logger.append(rec); records.append(rec)

        fg = out["output"]["flowguard"]
        trip_kind = fg.get("trigger")
        if trip_kind:
            trip_kind = trip_kind.upper()
            print(f"FlowGuard {trip_kind} detected")
            ctrl.flowguard.reset()
            break

    return records

def _make_seed_record(ctrl: SyncController, printer: SimplePrinterModel, t_s: float, sensor_val: float | int) -> Dict[str, Any]:
    """Builds the very first log row in the new schema."""
    sensor_ui = float(max(-1.0, min(1.0, float(sensor_val)))) if isinstance(sensor_val, (int, float)) else 0.0
    return {
        "input": {
            "tick": 0,
            "dt_s": 0.0,
            "t_s": t_s,
            "d_mm": 0.0,
            "sensor": sensor_val,
        },
        "output": {
            "rd_target": ctrl.rd_ref,
            "rd_ref": ctrl.rd_ref,
            "rd_ref_smoothed": ctrl.rd_ref,
            "rd_current": ctrl.rd_ref,
            "rd_note": "seed",
            "x_est": ctrl.state.x,
            "c_est": ctrl.state.c,
            "sensor_ui": sensor_ui,
            "flowguard": {"trigger": "", "reason": "", "level": 0.0, "max_clog": 0.0, "max_tangle": 0.0, "active": False, "relief_headroom": -1.0},
            "autotune": {"rd": None, "note": None},
        },
        "truth": {
            "rd_true": printer.extruder_rd_true,
            "spring_mm": printer.spring_mm(),
            "x_true": printer.x_true,
            "x_meas": printer.x_meas,
        },
        "meta": {
            "dt_s": 0.0,
            "t_s": t_s,
        },
    }

# ---------------------------------- CLI --------------------------------

def _run_cli():
    random.seed(time.time_ns())
    _setup_readline(history_limit=500)

    ap = argparse.ArgumentParser(description="Filament Tension Controller + Printer Simulator CLI (movement-based)")
    ap.add_argument("--sensor-type", choices=["P", "D", "CO", "TO"], default="P")
    ap.add_argument("--buffer-range-mm", type=float, default=8.0)
    ap.add_argument("--buffer-max-range-mm", type=float, default=12.0)
    ap.add_argument("--use-twolevel", dest="use_twolevel", action="store_true", help="Enable user two-level behavior for type-P sensor")
    ap.set_defaults(use_twolevel=False)
    ap.add_argument("--tick-dt-s", type=float, default=1.0, help="default dt used only for manual 'tick', 'clog' and 'tangle'")
    ap.add_argument("--rd-start", type=float, default=20.0, help="starting extruder rotation distance")
    ap.add_argument("--sensor-lag-mm", type=float, default=0.0)
    ap.add_argument("--stride-mm", type=float, default=5.0, help="movement per controller update during 'sim' and extreme tests")
    ap.add_argument("--initial-sensor", choices=["neutral", "random"], default="neutral",
                    help="Initial sensor reading used for startup/reset (default: neutral).")
    ap.add_argument("--stride-only", dest="stride_only", action="store_true", help="Only update on stride boundary (suppress real-time flips)")
    ap.add_argument("--chaos", type=float, default=0.0, help="Stick-slip in measured sensor: 0.0=exact (today), 2.0=max jerk.")
    ap.add_argument("--sample-error", type=float, default=0,
                    help="Randomize each sim tick to be stride * (1 + u*sample_error) with u∈[0,1]. "
                         "Only increases per-tick size; the final tick catches up so total distance is exact. "
                         "Use 0.0 to retain the prior exact, regular tick behavior.")
    ap.add_argument("--switch-hysteresis", type=float, default=0.2,
                    help="Hysteresis factor for switch sensors (D/CO/TO). "
                         "Trigger at thr*(1+factor), release at thr*(1-factor). 0 disables.")
    # Artifact outputs
    ap.add_argument("--log-debug", dest="log_debug", action="store_true", help="Display debug trace log entries (autotune)")
    ap.set_defaults(log_debug=False)
    ap.add_argument("--out", type=str, default="sim_plot.png", help="Output PNG filename for plots (default: sim_plot.png).")
    ap.add_argument("--log", type=str, default="sim.jsonl", help="Simulator json log output.")

    # Controller log plotting
    ap.add_argument("--plot", type=str, default=None,
                    help="Path to a controller-generated JSON log (JSONL or JSON array). "
                         "If set, the log is parsed, a plot is saved to --out and displayed,")
    ap.add_argument("--show-ticks", dest="show_ticks", action="store_true", help="Show individual updates on plot")
    ap.add_argument("--x-mm", choices=["off", "abs", "signed"], default="abs",
                    help="Top x-axis in extruder mm: 'abs' = total distance (monotonic), 'signed' = net displacement.")
    args = ap.parse_args()

    # If a controller log is provided, plot it and exit (no simulator session).
    if args.plot:
        try:
            _plot_log_file(
                args.plot,
                out_path=args.out,
                show_ticks=bool(args.show_ticks),
                show_mm_axis=(args.x_mm != "off"),
                mm_axis_mode=("abs" if args.x_mm == "abs" else "signed"),
            )
        except Exception as e:
            print(f"Failed to plot controller log: {e}")
            raise e
        return

    cfg = SyncControllerConfig(
        log_sync=True, # tell controller to also create log trace for debugging
        buffer_range_mm=args.buffer_range_mm,
        buffer_max_range_mm=args.buffer_max_range_mm,
        use_twolevel_for_type_p=args.use_twolevel,
        sensor_type=args.sensor_type,
        rd_start=args.rd_start,
        sensor_lag_mm=args.sensor_lag_mm,
    )
    default_dt_s = float(args.tick_d_t if False else args.tick_dt_s)

    ctrl = SyncController(cfg)
    logger = SimLogger(args.log, truncate_on_init=True)

    logger.write_header({
        "rd_start": cfg.rd_start,
        "sensor_type": cfg.sensor_type,
        "twolevel_active": bool(cfg.use_twolevel_for_type_p or cfg.sensor_type in ['CO', 'TO']),
        "buffer_range_mm": cfg.buffer_range_mm,
        "buffer_max_range_mm": cfg.buffer_max_range_mm,
        "switch_hysteresis": args.switch_hysteresis,
        "chaos": args.chaos,
        "sample_error": args.sample_error,
    })

    printer = SimplePrinterModel(
        ctrl,
        extruder_rd_true=None,
        initial_spring_mm=0.0,
        chaos=max(0.0, min(2.0, args.chaos)),
        hysteresis=max(0.0, min(0.4, args.switch_hysteresis)),
    )

    # --------- Set initial sensor state (neutral/random) BEFORE reset ----------
    if args.initial_sensor == "random":
        thr = cfg.flowguard_extreme_threshold
        norm_clip = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)

        if cfg.sensor_type == "P":
            x0 = random.uniform(-1.0, 1.0)
        elif cfg.sensor_type == "D":
            choice = random.choice([-1, 0, 1])
            if choice == 0:
                x0 = random.uniform(-0.8 * thr, 0.8 * thr)
            elif choice == 1:
                x0 = min(norm_clip, random.uniform(thr + 0.05, thr + 0.5))
            else:
                x0 = -min(norm_clip, random.uniform(thr + 0.05, thr + 0.5))
        elif cfg.sensor_type == "CO":
            choice = random.choice([0, 1])
            if choice == 1:
                x0 = min(norm_clip, random.uniform(thr + 0.05, thr + 0.5))
            else:
                x0 = random.uniform(-min(norm_clip, thr - 0.05), thr - 0.05)
        elif cfg.sensor_type == "TO":
            choice = random.choice([0, -1])
            if choice == -1:
                x0 = -min(norm_clip, random.uniform(thr + 0.05, thr + 0.5))
            else:
                x0 = random.uniform(-thr + 0.05, min(norm_clip, thr - 0.05))
        else:
            x0 = 0.0

        printer.x_true = max(-norm_clip, min(norm_clip, x0))
        printer.x_meas = printer.x_true  # start measured at modeled state
        # re-bootstrap last switch value after manual override of x
        printer._bootstrap_switch_state()
    # ---------------------------------------------------------------------

    # Simulation clock (absolute time seconds since start) lives on the printer
    printer.reset_time(0.0)

    # Take ONE reading and use it consistently for reset and the seed log row
    z0 = printer.measure()
    ctrl.reset(printer.get_time_s(), cfg.rd_start, z0, simulation=True)

    # Append a seed sample at t=0 so plots (and top mm axis) start at zero
    seed_rec = _make_seed_record(ctrl, printer, printer.get_time_s(), z0)
    logger.append(seed_rec)

    # Make the initial summary reflect the seed
    last_sensor = z0
    oo = seed_rec["output"]
    last_sensor_ui = oo["sensor_ui"]
    last_flowguard = oo["flowguard"]
    last_autotune_rd = None

    # Show whichever attribute exists on cfg
    print("=== Filament Tension Controller CLI ===")
    print(f" Sensor Type           : {cfg.sensor_type}")
    print(f" Use TwoLevel          : {cfg.use_twolevel_for_type_p}")
    print(f" Buffer Range (sensor) : {cfg.buffer_range_mm} mm")
    print(f" Buffer Max Range      : {cfg.buffer_max_range_mm} mm  (physical limit)")
    print(f" Autotune motion       : {cfg.autotune_motion_mm} mm")
    print(f" Flowguard relief      : {cfg.flowguard_relief_mm} mm")
    print(f" MMU Gear RD start     : {cfg.rd_start} mm")
    print(f" Sensor lag            : {cfg.sensor_lag_mm} mm")
    print(f" Simulator:")
    print(f"   Chaos factor        : {args.chaos}")
    print(f"   Ext sample error    : {args.sample_error}")
    switch_hysteresis = args.switch_hysteresis if cfg.sensor_type != "P" else "n/a"
    print(f"   Switch hysteresis   : {switch_hysteresis}")
    print(f"   Initial sensor mode : {args.initial_sensor}")
    print(f"   Stride per update   : {args.stride_mm} mm   (sim, clog & tangle)")
    print(f"   Default dt          : {default_dt_s} s    (manual 'tick' & clog/tangle test)")
    print(f"   JSON log            : {logger.path}")
    if not HAVE_READLINE:
        print(" (Tip: install 'pyreadline3' on Windows to enable Up/Down history)")
    _print_cli_help()

    # ---------------------------------------------------------------------

    while True:
        try:
            line = input("cmd> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            _plot_from_log(
                cfg, logger,
                mode="save",
                out_path=args.out,
                show_ticks=args.show_ticks,
                show_mm_axis=(args.x_mm != "off"),
                mm_axis_mode=("abs" if args.x_mm == "abs" else "signed"),
                summary_txt=_summary_txt(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            )  # save on exit
            break

        if not line:
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        _add_history(line)
        low = line.lower()

        # ----------------------------------------------------------------------
        if low in ("q", "quit", "exit"):
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            break

        # ----------------------------------------------------------------------
        if low in ("h", "help"):
            _print_cli_help()
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # ----------------------------------------------------------------------
        if low in ("p", "plot"):
            _plot_from_log(
                cfg, logger,
                mode="save",
                out_path=args.out,
                show_ticks=args.show_ticks,
                show_mm_axis=(args.x_mm != "off"),
                mm_axis_mode=("abs" if args.x_mm == "abs" else "signed"),
                summary_txt=_summary_txt(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            )
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # ----------------------------------------------------------------------
        if low in ("d", "display"):
            _plot_from_log(
                cfg, logger,
                mode="display",
                show_ticks=args.show_ticks,
                show_mm_axis=(args.x_mm != "off"),
                mm_axis_mode=("abs" if args.x_mm == "abs" else "signed"),
                summary_txt=_summary_txt(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            )
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # ----------------------------------------------------------------------
        if low == "status":
            print(f" RD: {ctrl.rd_current:.4f} mm | x={ctrl.state.x:.3f} | c={ctrl.state.c:.4f} | "
                  f"Bowden/Buffer spring={printer.spring_mm():.3f}mm | printer_RD_true={printer.extruder_rd_true:.4f}")
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # ----------------------------------------------------------------------
        if low == "clear":
            # Clear log and reconstruct printer first
            logger.clear()
            # Re-write header after clearing (kept consistent with startup)
            logger.write_header({
                "rd_start": cfg.rd_start,
                "sensor_type": cfg.sensor_type,
                "twolevel_active": bool(cfg.use_twolevel_for_type_p),
                "buffer_range_mm": cfg.buffer_range_mm,
                "buffer_max_range_mm": cfg.buffer_max_range_mm,
                "switch_hysteresis": args.switch_hysteresis,
                "chaos": args.chaos,
                "sample_error": args.sample_error,
            })
            printer = SimplePrinterModel(
                ctrl,
                extruder_rd_true=printer.extruder_rd_true,
                initial_spring_mm=0.0,
                chaos=max(0.0, min(2.0, args.chaos)),
                hysteresis=args.switch_hysteresis,
            )

            # Reset clock and controller with timestamp
            printer.reset_time(0.0)
            z0 = printer.measure()
            ctrl.reset(printer.get_time_s(), cfg.rd_start, z0, simulation=True)

            # Seed t=0 record
            seed_rec = _make_seed_record(ctrl, printer, printer.get_time_s(), z0)
            logger.append(seed_rec)

            # Make the initial SUMMARY reflect the seed
            last_sensor = z0
            oo = seed_rec["output"]
            last_sensor_ui = oo["sensor_ui"]
            last_flowguard = oo["flowguard"]
            last_autotune_rd = None

            print("Controller reset, printer spring rebased, and log cleared. Tick counter reset to 0.")
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # ----------------------------------------------------------------------
        if low.startswith("rd "):
            parts = line.split()
            if len(parts) != 2:
                print("Usage: rd <new_true_extruder_rotation_distance>")
                _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
            try:
                new_rd = float(parts[1])
            except ValueError:
                print("Bad numeric value.")
                _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
            printer.set_extruder_rd_true(new_rd)
            print(f"Printer true extruder RD set to {printer.extruder_rd_true:.4f} mm.")
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # ----------------------------------------------------------------------
        if low == "clog" or low == "tangle":
            kind = "clog" if low == "clog" else "tangle"
            print(f"Realistic {kind} test (autotune state, stride={args.stride_mm} mm, dt={default_dt_s}s, stop on FlowGuard)...")
            recs = _forced_extreme_test(
                ctrl, logger, printer,
                kind=kind,
                stride_mm=args.stride_mm,
                dt_s_step=default_dt_s,
            )
            if recs:
                last_sensor = recs[-1].get("input", {}).get("sensor")
                oo = recs[-1].get("output", {})
                last_sensor_ui = oo.get("sensor_ui")
                last_flowguard = oo.get("flowguard")
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # ----------------------------------------------------------------------
        # Distance-based simulation: "sim <vel_mm_s> <time_period_s> [rd]"
        if line.startswith("sim "):
            parts = line.split()
            if len(parts) not in (3, 4):
                print("Usage: sim <avg_extruder_speed_mm_s> <time_s> [<extruder_rotation_distance>]")
                _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
            try:
                v = float(parts[1]); T = float(parts[2])
                rd_true = float(parts[3]) if len(parts) == 4 else None
            except ValueError:
                print("Bad numeric values.")
                _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
        
            if rd_true is not None:
                printer.set_extruder_rd_true(rd_true)
        
            total_mm = v * T
            stride = abs(args.stride_mm)
            max_stride = stride * (1.0 + max(0.0, args.sample_error))
            n_steps = max(1, int(round(abs(total_mm) / max(1e-9, stride))))
            per_step = math.copysign(abs(total_mm) / n_steps, total_mm)
            dt_s = (abs(per_step) / abs(v)) if abs(v) > 1e-12 else default_dt_s
            max_str = f"-{max_stride:.1f}mm" if args.sample_error > 0 else ""
        
            print(
                f"Running sim: total={total_mm:.3f}mm at {v}mm/s | approx steps={n_steps} | "
                f"stride≈{stride:.1f}mm{max_str} | printer_RD_true={printer.extruder_rd_true:.4f}mm | "
                f"spring0={printer.spring_mm():.3f}mm ..."
            )
        
            # --- Helpers -------------------------------------------------------
            def _new_stride_len(rem: float) -> float:
                base = max(1e-9, abs(args.stride_mm))
                if args.sample_error <= 1e-12:
                    return min(base, rem)
                return min(base * (1.0 + random.random() * args.sample_error), rem)
        
            def _read_sensor():
                raw = printer.measure()
                return int(raw) if cfg.sensor_type in ("CO", "TO", "D") else float(raw)
        
            def _next_flip_distance(sensor_type: str, z0: int, x: float, g: float, s: float):
                """
                Distance (>=0) along commanded extrusion to next threshold crossing, else inf.
                g = (2/BR)*(RD_true/RD_prev - 1) is Δx per +1 mm extruder (normalized).
                """
                if sensor_type == "P" or abs(g) <= 1e-16:
                    return math.inf, None
                thr_on, thr_off = printer.get_switch_thresholds()
                xvel_pos = (g * s) > 0.0
                targets = []
                if sensor_type == "CO":
                    if z0 == 0 and xvel_pos: targets.append(+thr_on)
                    elif z0 == 1 and not xvel_pos: targets.append(+thr_off)
                elif sensor_type == "TO":
                    if z0 == 0 and not xvel_pos: targets.append(-thr_on)
                    elif z0 == -1 and xvel_pos:  targets.append(-thr_off)
                elif sensor_type == "D":
                    if z0 == 0:
                        targets.append(+thr_on if xvel_pos else -thr_on)
                    elif z0 == 1 and not xvel_pos:
                        targets.append(+thr_off)
                    elif z0 == -1 and xvel_pos:
                        targets.append(-thr_off)
        
                lag_norm = 0.0
                if targets and printer.chaos > 1e-12:
                    lag_mm_max = min(cfg.buffer_max_range_mm, float(printer.chaos) * cfg.buffer_max_range_mm)
                    lag_norm = (2.0 / cfg.buffer_range_mm) * (random.random() * lag_mm_max)
        
                best_d = math.inf
                best_anchor = None
                norm_clip = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)
                for anchor in targets:
                    x_target = anchor + (lag_norm if xvel_pos else -lag_norm)
                    x_target = max(-norm_clip + 1e-12, min(norm_clip - 1e-12, x_target))
                    d_needed = (x_target - x) / g
                    if d_needed * s > 1e-12:
                        d_abs = abs(d_needed)
                        if d_abs < best_d:
                            best_d = d_abs
                            best_anchor = anchor
                return best_d, best_anchor
        
            # --- Rolling-stride event scheduler -------------------------------
            sgn = 1.0 if total_mm >= 0 else -1.0
            remaining = abs(total_mm)
            stride_left = _new_stride_len(remaining)
            eps = 1e-12
            is_discrete = cfg.sensor_type in ("CO", "TO", "D")
        
            while remaining > eps:
                # PRE-MOVE read
                z0 = _read_sensor()
        
                # RD in effect for the chunk we are about to simulate
                rd_in_effect = getattr(ctrl, "rd_prev", ctrl.rd_current)
        
                # Normalized gain per +1 mm extruder
                g = printer.gain_per_mm(rd_in_effect)
                s = 1.0 if sgn > 0 else -1.0
        
                # Predict next flip and stride
                x_now = printer.x_true
                if cfg.sensor_type == "P":
                    d_to_flip, _flip_anchor = math.inf, None
                else:
                    d_to_flip, _flip_anchor = _next_flip_distance(cfg.sensor_type, int(z0), x_now, g, s)
                d_to_stride = stride_left
        
                # Advance to earliest of (remaining, flip, stride)
                chunk_abs = min(remaining, d_to_stride, d_to_flip)
                d_chunk = sgn * chunk_abs
                dt_chunk = (chunk_abs / abs(v)) if abs(v) > 1e-12 else default_dt_s
                sim_time_s = printer.advance_time(dt_chunk)
        
                # Physical spring evolution using rd_in_effect
                printer.advance_physics(rd_in_effect, d_chunk)
        
                # Measure at event boundary
                z1 = _read_sensor()
        
                # Event classification
                if is_discrete:
                    actually_flipped = (int(z1) != int(z0))
                else:
                    actually_flipped = False
                predicted_flip = abs(chunk_abs - d_to_flip) <= 1e-12
                hit_stride = abs(chunk_abs - d_to_stride) <= 1e-12
        
                emit = (actually_flipped and not args.stride_only) or hit_stride
                if emit:
                    out = ctrl.update(sim_time_s, d_chunk, z1, simulation=True)
        
                    rec = {
                        **out,
                        "truth": {
                            "rd_true": printer.extruder_rd_true,
                            "spring_mm": printer.spring_mm(),
                            "x_true": printer.x_true,
                            "x_meas": printer.x_meas,
                        },
                        "meta": {
                            "dt_s": dt_chunk,
                            "t_s": sim_time_s,
                            "d_to_stride": d_to_stride,
                            "d_to_flip": d_to_flip,
                            "event": ("flip" if actually_flipped else "stride"),
                            "sensor_at_event": int(z1) if is_discrete else float(z1),
                        },
                    }
                    logger.append(rec)
        
                    # Session summary state
                    last_sensor = int(z1) if is_discrete else 0
                    oo = out.get("output", {})
                    last_sensor_ui = oo.get("sensor_ui", None)
                    last_flowguard = oo.get("flowguard", {"trigger": ""})
        
                    auto = oo.get("autotune", {})
                    if auto.get("rd") is not None:
                        last_autotune_rd = auto["rd"]
                        print(f"AUTOTUNE: rd: {auto['rd']:.4f}, reason: {auto.get('note')}")
                    elif auto.get("note") and args.log_debug:
                        print(f"DEBUG: {auto['note']}")
        
                    # FlowGuard trip?
                    fg = oo.get("flowguard", {"trigger": ""})
                    trip_kind = fg.get("trigger")
                    if trip_kind:
                        trip_kind = trip_kind.upper()
                        print(f"FlowGuard trip; {trip_kind}. Stopping simulation.")
                        ctrl.flowguard.reset() # Allow continuation after trigger
                        break
        
                    # Reset stride from this instant after an event
                    stride_left = _new_stride_len(remaining - chunk_abs)
                else:
                    # No controller update on non-event chunk
                    stride_left = max(0.0, stride_left - chunk_abs)
        
                # Consume distance
                remaining -= chunk_abs
        
            print(f"Simulation complete. Current RD={ctrl.rd_current:.4f}.")
            print("Type 'plot' to save a plot, or 'display' to open a window.")
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue
        
        # ----------------------------------------------------------------------
        # Event-driven ping-pong: "inout <move_mm> <iterations> [<speed_mm_s> [<rd_true>]]"
        if line.startswith("inout "):
            parts = line.split()
            if len(parts) not in (3, 4, 5):
                print("Usage: inout <move_mm> <iterations> [<speed_mm_s> [<extruder_rotation_distance>]]")
                _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
        
            try:
                move_mm = float(parts[1])
                iters   = int(parts[2])
                v_mm_s  = float(parts[3]) if len(parts) >= 4 else None
                rd_true = float(parts[4]) if len(parts) == 5 else None
            except ValueError:
                print("Bad numeric values.")
                _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
        
            if iters <= 0 or abs(move_mm) < 1e-12:
                print("Nothing to do (iters<=0 or move too small).")
                _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
        
            # Defaults
            default_speed = max(1e-9, abs(args.stride_mm) / max(1e-9, default_dt_s))
            v = abs(v_mm_s) if (v_mm_s is not None and abs(v_mm_s) > 1e-12) else default_speed
            if rd_true is not None:
                printer.set_extruder_rd_true(rd_true)
        
            stride = max(1e-9, abs(args.stride_mm))
            eps = 1e-12
            is_discrete = cfg.sensor_type in ("CO", "TO", "D")
            norm_clip = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)
        
            def _read_sensor():
                raw = printer.measure()
                return int(raw) if is_discrete else float(raw)
        
            def _next_flip_distance(sensor_type: str, z0: int, x: float, g: float, s: float):
                if sensor_type == "P" or abs(g) <= 1e-16:
                    return math.inf, None
                thr_on, thr_off = printer.get_switch_thresholds()
                xvel_pos = (g * s) > 0.0
                targets = []
                if sensor_type == "CO":
                    if z0 == 0 and xvel_pos: targets.append(+thr_on)
                    elif z0 == 1 and not xvel_pos: targets.append(+thr_off)
                elif sensor_type == "TO":
                    if z0 == 0 and not xvel_pos: targets.append(-thr_on)
                    elif z0 == -1 and xvel_pos:  targets.append(-thr_off)
                elif sensor_type == "D":
                    if z0 == 0:
                        targets.append(+thr_on if xvel_pos else -thr_on)
                    elif z0 == 1 and not xvel_pos:
                        targets.append(+thr_off)
                    elif z0 == -1 and xvel_pos:
                        targets.append(-thr_off)
        
                lag_norm = 0.0
                if targets and printer.chaos > 1e-12:
                    lag_mm_max = min(cfg.buffer_max_range_mm, float(printer.chaos) * cfg.buffer_max_range_mm)
                    lag_norm = (2.0 / cfg.buffer_range_mm) * (random.random() * lag_mm_max)
        
                best_d = math.inf
                best_anchor = None
                for anchor in targets:
                    x_target = anchor + (lag_norm if xvel_pos else -lag_norm)
                    x_target = max(-norm_clip + 1e-12, min(norm_clip - 1e-12, x_target))
                    d_needed = (x_target - x) / g
                    if d_needed * s > 1e-12:
                        d_abs = abs(d_needed)
                        if d_abs < best_d:
                            best_d = d_abs
                            best_anchor = anchor
                return best_d, best_anchor
        
            def _d_to_stride_net(net_since_evt: float, s: float) -> float:
                """
                Distance (>=0) along current commanded direction s (+1/-1)
                until |net_since_evt + s*d| >= stride. If we move opposite the
                current net, abs(net) shrinks so the threshold is unreachable.
                """
                a = abs(net_since_evt)
                if a >= stride - 1e-12:
                    return 0.0
                if abs(net_since_evt) <= 1e-12:
                    return stride
                if math.copysign(1.0, net_since_evt) == math.copysign(1.0, s):
                    return max(0.0, stride - a)
                return math.inf
        
            print(
                f"Running inout: move={move_mm:.3f}mm, iters={iters}, "
                f"speed={v:.3f}mm/s | stride={stride:.3f}mm | "
                f"printer_RD_true={printer.extruder_rd_true:.4f}mm"
            )
        
            emitted = 0
            net_since_event = 0.0    # signed net motion since last emitted event
        
            # Alternate +move, then -move, repeated
            for rep in range(iters):
                for phase_sign in (+1.0, -1.0):
                    seg_len = abs(move_mm)
                    sgn = math.copysign(1.0, phase_sign * (move_mm if move_mm != 0 else 1.0))
                    remaining = seg_len
        
                    while remaining > eps:
                        # PRE-MOVE read
                        z0 = _read_sensor()
        
                        # RD in effect for this chunk
                        rd_in_effect = getattr(ctrl, "rd_prev", ctrl.rd_current)
        
                        # Motion gain and direction
                        g = printer.gain_per_mm(rd_in_effect)
                        s = 1.0 if sgn > 0 else -1.0
        
                        # Predict next flip and next stride crossing of |net|
                        x_now = printer.x_true
                        if cfg.sensor_type == "P":
                            d_to_flip, _ = math.inf, None
                        else:
                            d_to_flip, _ = _next_flip_distance(cfg.sensor_type, int(z0), x_now, g, s)
                        d_to_stride = _d_to_stride_net(net_since_event, s)
        
                        # Advance to the earliest of (segment end, flip, stride)
                        chunk_abs = min(remaining, d_to_flip, d_to_stride)
                        d_chunk = sgn * chunk_abs
                        dt_chunk = (chunk_abs / v) if v > 1e-12 else default_dt_s
                        sim_time_s = printer.advance_time(dt_chunk)
        
                        # Physical spring evolution with rd_in_effect
                        printer.advance_physics(rd_in_effect, d_chunk)
        
                        # Read AFTER motion
                        z1 = _read_sensor()
        
                        # Event classification
                        if is_discrete:
                            actually_flipped = (int(z1) != int(z0))
                        else:
                            actually_flipped = False
                        predicted_flip = abs(chunk_abs - d_to_flip) <= 1e-12
                        hit_stride = (d_to_stride < math.inf) and (abs(chunk_abs - d_to_stride) <= 1e-12)

                        emit = (actually_flipped and not args.stride_only) or hit_stride
                        if emit:
                            label = "flip" if actually_flipped else "stride"
                            out = ctrl.update(sim_time_s, d_chunk, z1, simulation=True)
        
                            rec = {
                                **out,
                                "truth": {
                                    "rd_true": printer.extruder_rd_true,
                                    "spring_mm": printer.spring_mm(),
                                    "x_true": printer.x_true,
                                    "x_meas": printer.x_meas,
                                },
                                "meta": {
                                    "dt_s": dt_chunk,
                                    "t_s": sim_time_s,
                                    "event": label,
                                    "phase": ("in" if sgn > 0 else "out"),
                                    "sensor_at_event": int(z1) if is_discrete else float(z1),
                                },
                            }
                            logger.append(rec)
                            emitted += 1
        
                            # Session summary state
                            last_sensor = int(z1) if is_discrete else float(z1)
                            oo = out.get("output", {})
                            last_sensor_ui = oo.get("sensor_ui", None)
                            last_flowguard = oo.get("flowguard", {"trigger": ""})
        
                            auto = oo.get("autotune", {})
                            if auto.get("rd") is not None:
                                last_autotune_rd = auto["rd"]
                                print(f"AUTOTUNE: rd: {auto['rd']:.4f}, reason: {auto.get('note')}")
                            elif auto.get("note") and args.log_debug:
                                print(f"DEBUG: {auto['note']}")
        
                            # FlowGuard trip?
                            fg = oo.get("flowguard", {"trigger": ""})
                            trip_kind = fg.get("trigger")
                            if trip_kind:
                                trip_kind = trip_kind.upper()
                                print(f"FlowGuard trip; {trip_kind}. Stopping inout.")
                                ctrl.flowguard.reset() # Allow continuation after trigger
                                remaining = 0.0
                                rep = iters  # break outer loops
                                break
        
                            # Reset net tracker AFTER an emitted event
                            net_since_event = 0.0
                        else:
                            # Silent advance: accumulate signed net, no controller update
                            net_since_event += d_chunk
        
                        # Consume portion of the segment
                        remaining -= chunk_abs
                    # end while
                # end for phase
            # end for iters
        
            print(f"InOut complete. Emitted {emitted} event(s). Current RD={ctrl.rd_current:.4f}.")
            if emitted == 0:
                print("Note: No stride/flip events occurred (net never reached stride and no sensor flips).")
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue
        
        # ----------------------------------------------------------------------
        # Manual update: "t|tick <d_ext_mm> [<sensor|'auto'>]"
        if line.startswith("t ") or line.startswith("tick "):
            parts = line.split()
            if parts[0] in ("t", "tick"):
                parts = parts[1:]
        
            if len(parts) == 0:
                print("Usage: t <d_ext_mm> [<sensor|auto>]")
                _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
            if len(parts) > 2:
                print("Too many parameters. Usage: t <d_ext_mm> [<sensor|auto>]")
                _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
        
            try:
                d_ext = float(parts[0])
            except ValueError:
                print("Bad numeric value for d_ext_mm.")
                _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
        
            # Sensor handling (pre-move)
            if len(parts) == 1 or parts[1].strip().lower() == "auto":
                z = printer.measure()
            else:
                if cfg.sensor_type == "P":
                    try:
                        z = float(parts[1])
                    except ValueError:
                        print("Bad sensor value. Expect float in [-1,1].")
                        _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                        continue
                else:
                    try:
                        z = int(parts[1])
                    except ValueError:
                        print("Bad sensor value. Expect integer.")
                        _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                        continue
                    allowed = {-1, 0, 1} if cfg.sensor_type == "D" else ({0, 1} if cfg.sensor_type == "CO" else {-1, 0})
                    if z not in allowed:
                        if cfg.sensor_type == "D":
                            print("Discrete sensor (D) must be -1, 0, or 1.")
                        elif cfg.sensor_type == "CO":
                            print("Compression-only sensor (CO) must be 0 or 1.")
                        else:
                            print("Tension-only sensor (TO) must be -1 or 0.")
                        _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                        continue
        
            # RD that was in effect during this tick (before controller updates)
            rd_in_effect = getattr(ctrl, "rd_prev", ctrl.rd_current)
        
            # Advance time and update controller
            sim_time_s = printer.advance_time(default_dt_s)
            out = ctrl.update(sim_time_s, d_ext, z, simulation=True)
        
            # Physical evolution using rd_in_effect for this motion
            printer.advance_physics(rd_in_effect, d_ext)
        
            rec = {
                **out,
                "truth": {
                    "rd_true": printer.extruder_rd_true,
                    "spring_mm": printer.spring_mm(),
                    "x_true": printer.x_true,
                    "x_meas": printer.x_meas,
                },
                "meta": {
                    "dt_s": default_dt_s,
                    "t_s": sim_time_s,
               },
            }
            logger.append(rec)
        
            last_sensor = z
            oo = out["output"]
            last_sensor_ui = oo["sensor_ui"]
            last_flowguard = oo["flowguard"]
        
            print(f"RD={oo['rd_current']:.4f} | x={oo['x_est']:.3f} | c={oo['c_est']:.4f} | "
                  f"sensor_ui={oo['sensor_ui']:.3f} | Bowden/Buffer spring={printer.spring_mm():.3f}mm | "
                  f"FlowGuard: {oo['flowguard']} | Autotune: {oo['autotune']}")
            _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # Fallback
        print("Unknown command. Type 'help' for usage.")
        _summary_print(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())

# ------------------------------- Main ----------------------------------

if __name__ == "__main__":
    _run_cli()

