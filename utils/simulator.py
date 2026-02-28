# -*- coding: utf-8 -*-

# simulator.py
# Simulator/CLI for the (movement-based) filament tension controller
#
# Example invocations:
#  python simulator.py --sensor-type P --initial-sensor=random --stride-mm=5 --chaos=2 --sample-error=0.5
#  python simulator.py --sensor-type P --initial-sensor=random --stride-mm=5 --chaos=2 --sample-error=0.5 --use-twolevel
#  python simulator.py --sensor-type D --initial-sensor=neutral --stride-mm=10 --chaos=2 --sample-error=0.5
#  python simulator.py --sensor-type CO --initial-sensor=random --stride-mm=10 --chaos=0 --sample-error=0
#
# Notes:
#  chaos=0, sample-error=0 means "pure" simulation
#    chaos .. simulates friction in bowen and randomized moves of sensor (1.0 = buffer_max_range)
#  sample-error .. simulates "late" updates from extruder movement (0.25 = 100%-125% of stride)
#  use-twolevel forces "switch" algorithm rather than EKF for sensor P/D types
#
# Requires: mmu_sync_feedback_manager.py (SyncFeedbackManagerConfig, SyncFeedbackManager)

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

from controller import SyncFeedbackManagerConfig, SyncFeedbackManager

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
    where
      Δ_rel_true = d_ext * (extruder_rd_true / rd_prev - 1.0)
    This is reference-free and depends only on the *hardware* RD and the RD actually
    in effect during this step (rd_prev). It avoids any reaction when the controller
    changes its nominal baseline.
    """
    def __init__(self, controller: SyncFeedbackManager, extruder_rd_true: Optional[float] = None, initial_spring_mm: float = 0.0, chaos: float = 0.0):
        self.ctrl = controller
        self.buffer_range_mm = controller.cfg.buffer_range_mm
        self.buffer_max_range_mm = controller.cfg.buffer_max_range_mm
        self.K = 2.0 / self.buffer_range_mm
        self._norm_clip = max(1e-9, self.buffer_max_range_mm / self.buffer_range_mm)
        x0 = (2.0 * initial_spring_mm) / self.buffer_range_mm
        self.x_true = max(-self._norm_clip, min(self._norm_clip, x0))
        rd_ref = controller.cfg.rd_start
        self.extruder_rd_true = extruder_rd_true if extruder_rd_true is not None else rd_ref

        self.chaos = max(0.0, min(2.0, float(chaos)))
        self.x_meas = self.x_true

    def set_extruder_rd_true(self, rd_true: float):
        self.extruder_rd_true = float(rd_true)

    def spring_mm(self) -> float:
        return self.x_true * (self.buffer_range_mm / 2.0)

    def measure(self) -> float | int:
        if self.chaos <= 1e-12:
            self.x_meas = self.x_true
        else:
            # Move measured position toward true with a random "jerk".
            # Max jerk (mm) scales with chaos in [0..2] up to buffer_max_range_mm.
            jerk_mm_max = self.chaos * self.buffer_max_range_mm
            draw_mm = random.random() * jerk_mm_max
            # If we "hit the limit", friction is overcome -> snap close to true.
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
        thr = self.ctrl.cfg.flowguard_extreme_threshold
        if self.ctrl.cfg.sensor_type == "D":
            if self.x_meas >= thr:
                return 1
            if self.x_meas <= -thr:
                return -1
            return 0
        if self.ctrl.cfg.sensor_type == "CO":
            return 1 if self.x_meas >= thr else 0
        if self.ctrl.cfg.sensor_type == "TO":
            return -1 if self.x_meas <= -thr else 0
        return 0

# ------------------------------- Plotting -------------------------------

def plot_progress(
    records: List[Dict[str, Any]],
    out_path: Optional[str] = None,
    dt_s: Optional[float] = None,
    sensor_label: Optional[str] = None,   # "D", "CO", "TO" or "P"
    stop_on_fg_trip: bool = True,
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
    RD_TRUE_COLOR   = "0.3"       # grey
    RD_REF_COLOR    = "#00800080" # green

    SENSOR_COLOR    = "#ff7f0e80" # "tab:orange" alpha=0.5
    EVENT_COLOR     = "0.5"       # grey
    SPRING_COLOR    = "#ff000080" # red          alpha=0.5
    C_EST_COLOR     = "#30303080" # grey         alpha=0.5
    X_EST_COLOR     = "#ff7f0e80" # "tab:orange" alpha=0.5
    AUTOTUNE_COLOR  = "#2ca02cff" # "tab:green"  alpha=1.0
    TICK_MARK_COLOR = "#40404080" # grey         alpha=0.5
    LOWLIGHT_TEXT   = "0.5"

    # On RD axis
    RD_MAIN_LW      = 2.0
    RD_TRUE_LW      = 1.0
    RD_REF_LW       = 1.0

    # On sensor axis
    SENSOR_INPUT_LW = 1.0
    SENSOR_UI_LW    = 1.0

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
    clog           = [bool(r["output"]["flowguard"]["clog"]) for r in records]
    tangle         = [bool(r["output"]["flowguard"]["tangle"]) for r in records]
    first_trip_idx = next((i for i, (c, t) in enumerate(zip(clog, tangle)) if c or t), None)

    # Capture trip kind/reason *before* truncation so we can show a box
    trip_kind = None
    trip_reason = None
    if first_trip_idx is not None:
        trip_kind = "CLOG" if clog[first_trip_idx] else "TANGLE"
        trip_reason = records[first_trip_idx].get("output", {}).get("flowguard", {}).get("reason")

    # Truncate all series on limit
    end_idx = (first_trip_idx + 1) if (stop_on_fg_trip and first_trip_idx is not None) else len(records)

    # Helper for optional series
    def get_optional_series(records, key, end_idx, section="output"):
        vals = [r.get(section, {}).get(key) for r in records[:end_idx]]
        return [] if all(v is None for v in vals) else vals

    # Core series
    rd              = [r["output"]["rd_current"] for r in records[:end_idx]]
    z               = [r["input"]["sensor"] for r in records[:end_idx]]
    z_ui            = [r["output"]["sensor_ui"] for r in records[:end_idx]]
    mm_deltas       = [r["input"]["d_mm"] for r in records[:end_idx]]

    # Debug series
    x_est           = get_optional_series(records, "x_est", end_idx)
    c_est           = get_optional_series(records, "c_est", end_idx)
    rd_target       = get_optional_series(records, "rd_target", end_idx)
    rd_ref          = get_optional_series(records, "rd_ref", end_idx)
    rd_ref_smoothed = get_optional_series(records, "rd_ref_smoothed", end_idx)

    has_x_est = any(v is not None for v in x_est)
    has_c_est = any(v is not None for v in c_est)
    has_rd_target = any(v is not None for v in rd_target)
    has_rd_ref = any(v is not None for v in rd_ref)
    has_rd_ref_smoothed = any(v is not None for v in rd_ref_smoothed)

    # Simulator only series
    rd_true        = get_optional_series(records, "rd_true", end_idx, section="truth")
    spring_mm      = get_optional_series(records, "spring_mm", end_idx, section="truth")

    has_rd_true = rd_true and any(v is not None for v in rd_true)
    has_spring_mm = spring_mm and any(v is not None for v in spring_mm)

    # Autotune markers (where controller reported a new default RD)
    autotune_recommendation = []
    for i, r in enumerate(records[:end_idx]):
        auto_rd = r.get("output", {}).get("autotune", {}).get("rd")
        if auto_rd is not None:
            autotune_recommendation.append((i, float(auto_rd)))


    # Main axes setup -------------------------------------------------------------
    fig, ax_rd = plt.subplots(figsize=(12, 6))
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
    if stop_on_fg_trip and first_trip_idx is not None:
        title_txt += f" | STOPPED at {trip_kind}"
    ax_rd.set_title(title_txt, pad=18) # Extra space above the top x-axis
    ax_rd.text(0.5, 1.1, summary_txt, transform=ax_rd.transAxes, ha="center", va="bottom", fontsize=6, color="0.4", wrap=True, zorder=999)


    # Plots against RD axis (left) ------------------------------------------------
    if len(t_axis) >= 2:
        # Core...
        if show_ticks:
            ax_rd.plot(t_axis, rd,              label="rotation_distance",   linestyle="-",  linewidth=RD_MAIN_LW, color=RD_COLOR, marker="o", markersize=2.5, markevery=1)
        else:
            ax_rd.plot(t_axis, rd,              label="rotation_distance",   linestyle="-",  linewidth=RD_MAIN_LW, color=RD_COLOR)

        if show_rd_true and has_rd_true:
            ax_rd.plot(t_axis, rd_true,         label="rd_true (simulator)", linestyle=":",  linewidth=RD_TRUE_LW, color=RD_TRUE_COLOR)

        # Debug...
        if has_rd_ref:
            ax_rd.plot(t_axis, rd_ref,          label="rd_ref",              linestyle="-",  linewidth=RD_REF_LW, color=RD_REF_COLOR)
        if has_rd_ref_smoothed:
            ax_rd.plot(t_axis, rd_ref_smoothed, label="rd_ref_smoothed",     linestyle="--", linewidth=RD_REF_LW, color=RD_REF_COLOR)
        if has_rd_target:
            ax_rd.plot(t_axis, rd_target,       label="rd_target",           linestyle="-.", linewidth=RD_REF_LW, color=RD_REF_COLOR)

    elif t_axis:
        ax_rd.scatter(t_axis, rd, label="rotation_distance", color=RD_COLOR, zorder=3)
        if show_rd_true and has_rd_true:
            ax_rd.scatter(t_axis, rd_true, label="rd_true (simulator)", color=RD_TRUE_COLOR, zorder=3)

    # Autotune dots
    if autotune_recommendation:
        t_marks = [t_axis[i] for (i, _) in autotune_recommendation]
        y_marks = [v for (_, v) in autotune_recommendation]
        ax_rd.scatter(t_marks, y_marks, marker="o", s=32, color=AUTOTUNE_COLOR, label="RD autotune", zorder=AUTOTUNE_ZORDER)

    # Flowguard terminator line
    for i, (is_clog, is_tangle) in enumerate(zip(clog[:end_idx], tangle[:end_idx])):
        if is_clog or is_tangle:
            ax_rd.axvline(t_axis[i], linestyle="-.", linewidth=3.0, alpha=0.7, color="red")


    # Plots against sensor axis (right) -------------------------------------------
    if len(t_axis) >= 2:
        # Core...
        ax_sensor.plot(t_axis, z,         label="sensor_reading", linestyle="-",  linewidth=SENSOR_INPUT_LW, color=SENSOR_COLOR)
        ax_sensor.plot(t_axis, z_ui,      label="sensor_ui",      linestyle=":",  linewidth=SENSOR_UI_LW,    color=SENSOR_COLOR)

        # Debug...
        if has_x_est:
            ax_sensor.plot(t_axis, x_est, label="x_est (debug)",  linestyle="--", linewidth=SENSOR_UI_LW,    color=X_EST_COLOR)
        if has_c_est:
            ax_sensor.plot(t_axis, c_est, label="c_est (debug)",  linestyle="--", linewidth=SENSOR_UI_LW,    color=C_EST_COLOR)

    elif t_axis:
        ax_sensor.scatter(t_axis, z,      label="sensor_reading", color=SENSOR_COLOR, zorder=2)
        ax_sensor.scatter(t_axis, z_ui,   label="sensor_ui",      color=SENSOR_COLOR, zorder=2)


    # Plots against bowden/buffer spring (far right) ------------------------------
    if has_spring_mm:
        ax_spring = ax_rd.twinx()
        ax_spring.spines["right"].set_position(("axes", 1.1))
        ax_spring.spines["right"].set_visible(True)
        ax_spring.spines["right"].set_color(LOWLIGHT_TEXT)
        ax_spring.set_frame_on(True)

        ax_spring.axhline(0.0, linewidth=1.0, alpha=0.2, color="grey")
        ax_spring.set_ylabel("Simulated bowden/buffer spring (mm)", color=LOWLIGHT_TEXT, fontsize=8)
        ax_spring.tick_params(axis="y", colors=LOWLIGHT_TEXT, which="both", width=1, length=4)

        y_spring = [float("nan") if v is None else float(v) for v in spring_mm]
        finite_vals = [v for v in y_spring if not (math.isnan(v) or math.isinf(v))]
        span = max(abs(min(finite_vals)), abs(max(finite_vals))) if finite_vals else 1.0
        lim = max(span * 1.1, 0.5)
        ax_spring.set_ylim(-lim, +lim)

        if len(t_axis) >= 2:
            ax_spring.plot(t_axis, y_spring,    label="bowden spring (simulator)", linestyle=":", linewidth=1.0, color=SPRING_COLOR)
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
        h     = 0.015   # height = 1.5% of axes; tweak to taste
        y0, y1 = max(0, y_top - h), min(1, y_top)

        for x in tick_times:
            ax_rd.axvline(x, ymin=y0, ymax=y1, color=TICK_MARK_COLOR, linewidth=1.0, zorder=1200, clip_on=False, label="_nolegend_")


    # Flowguard trip banner -------------------------------------------------------
    if stop_on_fg_trip and first_trip_idx is not None and trip_reason:
        ax_sensor.text(
            0.01, 0.98,
            f"{trip_kind} reason:\n{trip_reason}",
            transform=ax_rd.transAxes,
            va="top", ha="left",
            fontsize=9,
            wrap=True,
            bbox=dict(boxstyle="round", facecolor="0.92", edgecolor="0.6", alpha=0.95),
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

    legend = ax_rd.legend(
        rd_lines + sensor_lines + spring_lines,
        rd_labels + sensor_labels + spring_labels,
        loc="lower left",
        ncol=2,
        handlelength=2.0,
        handletextpad=0.8,
        columnspacing=1.2,
    )

    # Make legend lines more visible
    for lh in legend.legend_handles:
        lh.set_linewidth(1.25)


    # Plot ------------------------------------------------------------------------
    plt.tight_layout()

    # Make legend box smaller
    plt.rcParams.update({
        "legend.fontsize": 8,
        "legend.framealpha": 0.75,
        "legend.borderpad": 0.2,
        "legend.labelspacing": 0.25,
        "legend.handlelength": 1.0,
        "legend.handletextpad": 0.4,
    })

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
  t|tick <d_ext_mm> [<sensor|auto>]  - Manual one update. If sensor omitted or 'auto', uses simulator sensor state.
  sim <v_mm_s> <time_s> [rd]         - Simulate average extruder speed for time. Uses --stride-mm chunk size per update.
  rd <value>                         - Set printer's *true* extruder rotation_distance immediately.
  clog                               - Realistic compression-extreme test (build + stuck), stop on FlowGuard.
  tangle                             - Realistic tension-extreme test (build + stuck), stop on FlowGuard.
  clear                              - Reset controller (full), printer spring, and jsonl log file (tick=0).
  p | plot                           - Save plot to sim_plot.png (always saves to file).
  d | display                        - Display plot window (does not save).
  status                             - Show controller/printer state
  quit | q                           - Plot (save) and exit
""")

def _summary_txt(ctrl: SyncFeedbackManager, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard: Optional[Dict[str, Any]], spring_mm: float):
    autotune_str = "N/A" if last_autotune_rd is None else f"{last_autotune_rd:.4f}"
    sensor_str = "N/A" if last_sensor is None else (f"{last_sensor:.3f}" if isinstance(last_sensor, float) else str(last_sensor))
    sensor_ui_str = "N/A" if last_sensor_ui is None else f"{last_sensor_ui:.3f}"
    if isinstance(last_flowguard, dict):
        fg_str = f"clog={last_flowguard.get('clog', False)}, tangle={last_flowguard.get('tangle', False)}, reason={last_flowguard.get('reason')}"
    else:
        fg_str = "N/A"
    return f"RD={ctrl.rd_current:.4f} | Autotune={autotune_str} | sensor={sensor_str} | sensor_ui={sensor_ui_str} | Bowden/Buffer spring={spring_mm:.3f}mm | FlowGuard: {fg_str}"

def _summary_print(ctrl: SyncFeedbackManager, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard: Optional[Dict[str, Any]], spring_mm: float):
    summary_txt = _summary_txt(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, spring_mm)
    print(f"SUMMARY: {summary_txt}")

def _plot_from_log(
    cfg: SyncFeedbackManagerConfig,
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

    use_twolevel = cfg.use_twolevel_for_type_pd or cfg.sensor_type in ['CO', 'TO']
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
        if isinstance(obj, dict) and ("input" in obj or "output" in obj):
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
        rr["meta"] = {"dt_s": dt}
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
    ctrl: SyncFeedbackManager,
    logger: SimLogger,
    printer: SimplePrinterModel,
    kind: str,
    stride_mm: float,
    dt_s_step: float,
    sim_time_s: float,
) -> tuple[List[Dict[str, Any]], float]:
    """
    Two-phase realistic extreme test with printer-side fault prelude.
    Printer evolution uses Δ_rel_true = d_ext * (rd_true / rd_prev - 1.0) with
    a fault applied to the appropriate term (downstream vs upstream).
    Always passes an absolute timestamp into controller.update().
    """
    records: List[Dict[str, Any]] = []

    build_mm = max(stride_mm, ctrl.cfg.buffer_range_mm / 2.0)
    # --- FIX 1: always extrude forward (positive) for both tests ---
    d_ext_build = (+build_mm)  # clog and tangle both use +feed here

    fault_frac = 0.35  # 35% throughput loss while ramping to the peg

    norm_limit = max(1e-9, ctrl.cfg.buffer_max_range_mm / ctrl.cfg.buffer_range_mm)
    peg_thr = max(0.9, ctrl.cfg.flowguard_extreme_threshold)
    K = 2.0 / ctrl.cfg.buffer_range_mm

    # Phase 1: RAMP-IN with fault
    ramp_ticks_max = 400
    peg_need = 2
    peg_count = 0

    for _ in range(ramp_ticks_max):
        z_in = printer.measure()

        rd_prev = ctrl.rd_current
        sim_time_s += dt_s_step
        out = ctrl.update(d_ext_build, z_in, eventtime=sim_time_s, simulation=True)

        ratio = printer.extruder_rd_true / max(1e-9, rd_prev)
        base_gear = ratio * d_ext_build

        if kind == "clog":
            # Upstream restriction → effective push reduced (compression builds)
            d_ext_eff = (1.0 - fault_frac) * d_ext_build
            delta_rel = base_gear - d_ext_eff
        else:  # tangle
            # Downstream drag → effective gear motion reduced (tension builds)
            gear_eff = (1.0 - fault_frac) * base_gear
            delta_rel = gear_eff - d_ext_build

        printer.x_true += K * delta_rel
        printer.x_true = min(max(printer.x_true, -norm_limit), norm_limit)

        rec = {
            **out,
            "truth": {
                "rd_true": printer.extruder_rd_true,
                "spring_mm": printer.spring_mm(),
            },
            "meta": {
                "dt_s": dt_s_step,
                "t_s": sim_time_s,
           },
        }
        logger.append(rec); records.append(rec)

        if ctrl.cfg.sensor_type == "P":
            pegged_now = abs(printer.x_true) >= peg_thr
        elif ctrl.cfg.sensor_type == "D":
            m = printer.measure()
            pegged_now = (m == 1 and kind == "clog") or (m == -1 and kind == "tangle")
        elif ctrl.cfg.sensor_type == "CO":
            if kind == "clog":
                pegged_now = (printer.measure() == 1)
            else:  # tangle side is unseen by sensor → use printer state
                pegged_now = (printer.x_true <= -peg_thr)
        else:  # "TO"
            if kind == "tangle":
                pegged_now = (printer.measure() == -1)
            else:  # clog side is unseen by sensor → use printer state
                pegged_now = (printer.x_true >= peg_thr)

        peg_count = peg_count + 1 if pegged_now else 0
        if peg_count >= peg_need:
            # Pin to the *physical* extreme for clarity; one-sided sensors then read 0 on the unseen side.
            if ctrl.cfg.sensor_type == "CO":
                printer.x_true = +norm_limit if kind == "clog" else -norm_limit
            elif ctrl.cfg.sensor_type == "TO":
                printer.x_true = -norm_limit if kind == "tangle" else +norm_limit
            else:  # P/D
                printer.x_true = +norm_limit if kind == "clog" else -norm_limit
            break

        fg = out["output"]["flowguard"]
        if fg["clog"] or fg["tangle"]:
            print("FlowGuard detected during ramp-in.")
            return records, sim_time_s

    # Phase 2: STUCK (hard jam; sensor pegged)
    stuck_ticks_max = 120
    for _ in range(stuck_ticks_max):
        # Set stuck state first so the *measured* sensor reflects the stuck condition this tick.
        if ctrl.cfg.sensor_type == "CO":
            printer.x_true = +norm_limit if kind == "clog" else -norm_limit
        elif ctrl.cfg.sensor_type == "TO":
            printer.x_true = -norm_limit if kind == "tangle" else +norm_limit
        else:  # P/D
            printer.x_true = +norm_limit if kind == "clog" else -norm_limit

        z_in = printer.measure()

        rd_prev = ctrl.rd_current
        sim_time_s += dt_s_step
        out = ctrl.update(d_ext_build, z_in, eventtime=sim_time_s, simulation=True)

        rec = {
            **out,
            "truth": {
                "rd_true": printer.extruder_rd_true,
                "spring_mm": printer.spring_mm(),
            },
            "meta": {
                "dt_s": dt_s_step,
                "t_s": sim_time_s,
           },
        }
        logger.append(rec); records.append(rec)

        fg = out["output"]["flowguard"]
        if fg["clog"] or fg["tangle"]:
            print("FlowGuard detected in stuck phase.")
            break

    return records, sim_time_s

def _make_seed_record(ctrl: SyncFeedbackManager, printer: SimplePrinterModel, t_s: float, sensor_val: float | int) -> Dict[str, Any]:
    """Builds the very first log row in the new schema."""
    sensor_ui = float(max(-1.0, min(1.0, float(sensor_val)))) if isinstance(sensor_val, (int, float)) else 0.0
    return {
        "input": {
            "tick": 0,
            "t_s": t_s,
            "d_mm": 0.0,
            "sensor": sensor_val,
        },
        "output": {
            "rd_target": ctrl.rd_ref,
            "rd_ref": ctrl.rd_ref,
            "rd_ref_smoothed": ctrl.rd_ref,
            "rd_current": ctrl.rd_ref,
            "rd_reason": "seed",
            "x_est": ctrl.state.x,
            "c_est": ctrl.state.c,
            "sensor_ui": sensor_ui,
            "flowguard": {"clog": False, "tangle": False, "reason": None},
            "autotune": {"rd": None, "note": None},
        },
        "truth": {
            "rd_true": printer.extruder_rd_true,
            "spring_mm": printer.spring_mm(),
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
    ap.add_argument("--use-twolevel", dest="use_twolevel", action="store_true", help="Enable user two-level behavior")
    ap.add_argument("--no-use-twolevel", dest="use_twolevel", action="store_false")
    ap.set_defaults(use_twolevel=False)
    ap.add_argument("--tick-dt-s", type=float, default=0.5, help="default dt used only for manual 'tick', 'clog' and 'tangle'")
    ap.add_argument("--rd-start", type=float, default=20.0, help="starting extruder rotation distance")
    ap.add_argument("--sensor-lag-mm", type=float, default=0.0)
    ap.add_argument("--stride-mm", type=float, default=5.0, help="movement per controller update during 'sim' and extreme tests")
    ap.add_argument("--initial-sensor", choices=["neutral", "random"], default="neutral", help="Initial sensor reading used for startup/reset (default: neutral).")
    ap.add_argument("--chaos", type=float, default=0.0, help="Stick-slip in measured sensor: 0.0=exact (today), 2.0=max jerk.")
    ap.add_argument("--show-ticks", dest="show_ticks", action="store_true", help="Show individual updates on plot")
    ap.add_argument("--log-debug", dest="log_debug", action="store_true", help="Display debug trace log entries (autotune)")
    ap.add_argument("--x-mm", choices=["off", "abs", "signed"], default="abs", help="Top x-axis in extruder mm: 'abs' = total distance (monotonic), 'signed' = net displacement.")
    ap.add_argument("--sample-error", type=float, default=0,
                    help="Randomize each sim tick to be stride * (1 + u*sample_error) with u∈[0,1]. "
                         "Only increases per-tick size; the final tick catches up so total distance is exact. "
                         "Use 0.0 to retain the prior exact, regular tick behavior.")
    # Controller log plotting (non-sim mode)
    ap.add_argument("--plot", type=str, default=None, help="Path to a controller-generated JSON log (JSONL or JSON array). If set, the log is parsed, a plot is saved to --out and displayed,")
    ap.add_argument("--out", type=str, default="sim_plot.png", help="Output PNG filename for plots (default: sim_plot.png).")
    ap.add_argument("--log", type=str, default="sim.jsonl", help="Simulator json log output.")
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

    cfg = SyncFeedbackManagerConfig(
        log_sync=True, # PAUL testing
        buffer_range_mm=args.buffer_range_mm,
        buffer_max_range_mm=args.buffer_max_range_mm,
        use_twolevel_for_type_pd=args.use_twolevel,
        sensor_type=args.sensor_type,
        rd_start=args.rd_start,
        sensor_lag_mm=args.sensor_lag_mm,
    )
    default_dt_s = float(args.tick_d_t if False else args.tick_dt_s)

    ctrl = SyncFeedbackManager(cfg)
    logger = SimLogger(args.log, truncate_on_init=True)

    logger.write_header({
        "rd_start": cfg.rd_start,
        "sensor_type": cfg.sensor_type,
        "twolevel_active": bool(cfg.use_twolevel_for_type_pd),
        "buffer_range_mm": cfg.buffer_range_mm,
        "buffer_max_range_mm": cfg.buffer_max_range_mm,
    })

    printer = SimplePrinterModel(ctrl, extruder_rd_true=None, initial_spring_mm=0.0, chaos=max(0.0, min(1.0, args.chaos)))

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
    # ---------------------------------------------------------------------

    # Simulation clock (absolute time seconds since start)
    sim_time_s = 0.0

    # Take ONE reading and use it consistently for reset and the seed log row
    z0 = printer.measure()
    ctrl.reset(rd_init=cfg.rd_start, sensor_reading=z0, eventtime=sim_time_s, simulation=True)

    # Append a seed sample at t=0 so plots (and top mm axis) start at zero
    seed_rec = _make_seed_record(ctrl, printer, sim_time_s, z0)
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
    print(f" Use TwoLevel          : {cfg.use_twolevel_for_type_pd}")
    print(f" Buffer Range (sensor) : {cfg.buffer_range_mm} mm")
    print(f" Buffer Max Range      : {cfg.buffer_max_range_mm} mm  (physical clamp)")
    print(f" Autotune motion       : {cfg.autotune_motion_mm} mm")
    print(f" Flowguard relief      : {cfg.flowguard_relief_mm} mm")
    print(f" Flowguard motion      : {cfg.flowguard_motion_mm} mm")
    print(f" MMU Gear RD start     : {cfg.rd_start} mm")
    print(f" Sensor lag            : {cfg.sensor_lag_mm} mm")
    print(f" Simulator:")
    print(f"   Chaos factor        : {args.chaos}")
    print(f"   Initial sensor mode : {args.initial_sensor}")
    print(f"   Stride per update   : {args.stride_mm} mm   (sim, clog & tangle)")
    print(f"   Default dt          : {default_dt_s} s    (manual 'tick' & clog/tangle test)")
    print(f"   Sample error factor : {args.sample_error}")
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
            _plot_from_log(
                cfg, logger,
                mode="save",
                out_path=args.out,
                show_ticks=args.show_ticks,
                show_mm_axis=(args.x_mm != "off"),
                mm_axis_mode=("abs" if args.x_mm == "abs" else "signed"),
                summary_txt=_summary_txt(ctrl, last_autotune_rd, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            )
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
            # Re-write header after clearing
            logger.write_header({
                "rd_start": cfg.rd_start,
                "sensor_type": cfg.sensor_type,
                "twolevel_active": bool(cfg.use_twolevel_for_type_pd),
                "buffer_range_mm": cfg.buffer_range_mm,
                "buffer_max_range_mm": cfg.buffer_max_range_mm,
            })
            printer = SimplePrinterModel(
                ctrl,
                extruder_rd_true=printer.extruder_rd_true,
                initial_spring_mm=0.0,
                chaos=max(0.0, min(2.0, args.chaos)),
            )

            # Reset clock and controller with timestamp
            sim_time_s = 0.0
            z0 = printer.measure()
            ctrl.reset(rd_init=cfg.rd_start, sensor_reading=z0, eventtime=sim_time_s, simulation=True)

            # Seed t=0 record
            seed_rec = _make_seed_record(ctrl, printer, sim_time_s, z0)
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
            print(f"Realistic {kind.upper()} test (autotune state, stride={args.stride_mm} mm, dt={default_dt_s}s, stop on FlowGuard)...")
            recs, sim_time_s = _forced_extreme_test(
                ctrl, logger, printer,
                kind=kind,
                stride_mm=args.stride_mm,
                dt_s_step=default_dt_s,
                sim_time_s=sim_time_s,
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
            n_steps = max(1, int(round(abs(total_mm) / stride)))
            per_step = math.copysign(abs(total_mm) / n_steps, total_mm)
            dt_s = (abs(per_step) / abs(v)) if abs(v) > 1e-12 else default_dt_s
            max_str = f"-{max_stride:.1f}mm" if args.sample_error > 0 else ""
            print(f"Running sim: total={total_mm:.3f}mm at {v}mm/s | approx steps={n_steps} | stride≈{stride:.1f}mm{max_str}"
                  f" | printer_RD_true={printer.extruder_rd_true:.4f}mm | spring0={printer.spring_mm():.3f}mm ...")

            def _tick_plan(total_mm: float, base_stride: float, sample_err: float):
                """
                Yields signed per-tick targets (mm).
                - If sample_err == 0: reproduce previous behavior with equal steps (old code path).
                - If sample_err  > 0: each full-size tick is base_stride * (1 + u*sample_err), u∈[0,1].
                  We never exceed the remaining distance; the last tick is a 'catch-up' tick.
                """
                sgn = 1.0 if total_mm >= 0 else -1.0
                rem = abs(total_mm)
                stride_abs = max(1e-9, abs(base_stride))
                sample_err = max(0.0, float(sample_err))

                if sample_err <= 1e-12:
                    # Old behavior: same-sized ticks (except the last if needed), matching previous logic
                    n_steps = max(1, int(round(rem / stride_abs)))
                    per = rem / n_steps
                    for _ in range(n_steps - 1):
                        yield sgn * per
                        rem -= per
                    yield sgn * rem
                    return

                # New behavior: randomized enlargement per tick, final catch-up tick exact
                while rem > 1e-12:
                    if rem <= stride_abs + 1e-12:
                        step_abs = rem  # final catch-up tick (can be < stride_abs)
                    else:
                        # Only increase the tick size (never shrink below base stride)
                        extra = random.random() * (sample_err * stride_abs)  # in [0, sample_err*stride]
                        step_abs = stride_abs + extra
                        if step_abs > rem:
                            step_abs = rem  # don't overshoot the total
                    yield sgn * step_abs
                    rem -= step_abs

            # Drive the simulation using the planned ticks
            for step_target in _tick_plan(total_mm, args.stride_mm, args.sample_error):
                remaining = step_target
                while abs(remaining) > 1e-12:
                    # PRE-MOVE sensor (may be stick-slip affected)
                    z = printer.measure()

                    rd_prev = ctrl.rd_current
                    ratio = printer.extruder_rd_true / max(1e-9, rd_prev)
                    K = 2.0 / cfg.buffer_range_mm
                    g = K * (ratio - 1.0)  # x change per +1mm extruder

                    d_chunk = remaining  # default: take all of this tick's plan

                    if cfg.sensor_type != "P":
                        # Predict when the measured switch state will flip (same as your current code) ...
                        thr = cfg.flowguard_extreme_threshold
                        s = 1.0 if remaining >= 0 else -1.0
                        state0 = int(z)
                        x = printer.x_true
                        dir_pos = (g * s) > 0.0

                        anchor = None
                        st = cfg.sensor_type
                        if st == "D":
                            if state0 == 0:
                                anchor = (thr if dir_pos else -thr)
                            elif state0 == 1 and not dir_pos:
                                anchor = thr
                            elif state0 == -1 and dir_pos:
                                anchor = -thr
                        elif st == "CO":
                            if (state0 == 0 and dir_pos) or (state0 == 1 and not dir_pos):
                                anchor = thr
                        elif st == "TO":
                            if (state0 == 0 and not dir_pos) or (state0 == -1 and dir_pos):
                                anchor = -thr

                        if anchor is not None and abs(g) > 1e-16:
                            lag_norm = 0.0
                            if printer.chaos > 1e-12:
                                lag_mm_max = min(cfg.buffer_max_range_mm, float(printer.chaos) * cfg.buffer_max_range_mm)
                                lag_norm_max = (2.0 / cfg.buffer_range_mm) * lag_mm_max
                                lag_norm = random.random() * lag_norm_max

                            x_target = anchor + (lag_norm if dir_pos else -lag_norm)

                            norm_clip = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)
                            x_target = max(-norm_clip + 1e-12, min(norm_clip - 1e-12, x_target))

                            d_needed = (x_target - x) / g
                            if d_needed * s > 1e-12:
                                d_chunk = math.copysign(min(abs(d_needed), abs(remaining)), s)

                    # Time for this chunk
                    dt_chunk = (abs(d_chunk) / abs(v)) if abs(v) > 1e-12 else default_dt_s
                    sim_time_s += dt_chunk

                    # Controller + printer update (unchanged)
                    out = ctrl.update(d_chunk, z, eventtime=sim_time_s, simulation=True)
                    delta_rel = d_chunk * (ratio - 1.0)
                    printer.x_true += (2.0 / cfg.buffer_range_mm) * delta_rel
                    norm_clip = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)
                    printer.x_true = min(max(printer.x_true, -norm_clip), norm_clip)

                    rec = {
                        **out,
                        "truth": {
                            "rd_true": printer.extruder_rd_true,
                            "spring_mm": printer.spring_mm(),
                        },
                        "meta": {
                            "dt_s": dt_chunk,
                            "t_s": sim_time_s,
                        },
                    }
                    logger.append(rec)

                    last_sensor = z
                    oo = out["output"]
                    last_sensor_ui = oo["sensor_ui"]
                    last_flowguard = oo["flowguard"]

                    remaining -= d_chunk

                    auto = out["output"]["autotune"]
                    if auto["rd"]:
                        last_autotune_rd = auto["rd"]
                        print(f"AUTOTUNE: rd: {auto['rd']:.4f}, reason: {auto['note']}")
                    elif auto["note"] and args.log_debug:
                        print(f"DEBUG: {auto['note']}")

                    fg = out["output"]["flowguard"]
                    if fg["clog"] or fg["tangle"]:
                        trip_type = "clog" if fg["clog"] else "tangle"
                        print(f"FlowGuard trip; {trip_type}. Stopping simulation.")
                        remaining = 0.0
                        break

                fg = out["output"]["flowguard"]
                if fg["clog"] or fg["tangle"]:
                    break

            print(f"Simulation complete. Current RD={ctrl.rd_current:.4f}.")
            print("Type 'plot' to save a plot, or 'display' to open a window.")
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

            # Controller update (uses rd_prev for printer; new RD applies next tick)
            rd_prev = ctrl.rd_current
            sim_time_s += default_dt_s
            out = ctrl.update(d_ext, z, eventtime=sim_time_s, simulation=True)

            # Reference-free printer evolution
            ratio = printer.extruder_rd_true / max(1e-9, rd_prev)
            delta_rel = d_ext * (ratio - 1.0)
            printer.x_true += (2.0 / cfg.buffer_range_mm) * delta_rel
            norm_clip = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)
            printer.x_true = min(max(printer.x_true, -norm_clip), norm_clip)

            rec = {
                **out,
                "truth": {
                    "rd_true": printer.extruder_rd_true,
                    "spring_mm": printer.spring_mm(),
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

