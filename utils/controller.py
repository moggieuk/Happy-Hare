# -*- coding: utf-8 -*-
#
# Sync Feedback Controller
#
# This module implements a motion-triggered filament tension controller — that adapts gear
# stepper rotation distance (RD) dynamically based on sensor feedback. It offers modes of operation:
#
# 1) Simple dual level RD selection that works with CO (Compression only switch),
#    TO (Tension only switch), and optionally with  D (Dual switch) or P (Proportional) sensors
#
# 2) Combined proportional-derivative (PD) controller with Extended Kalman Filter
#    (EKF) for optimal results with D (Dual switch) or P (Proportional) sensors
#
# It also implements flowguard protection for all modes/sensor types that will trigger
# on clog (at extruder) or tangle (at MMU) conditions.
#
# An autotuning option can be enabled for dynamic tuning (and persistence) of
# calibrated MMU gear rotation_distance.
#

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Literal, Dict, Any, Union
import math

# P = proportional float [-1,1], D = discrete {-1,0,1}, CO = compression-only {0,1}, TO = tension-only {0,-1}
SensorType = Literal["P", "D", "CO", "TO"]

# -----------------------------------------------------------------------------
# ControllerConfig reference
# -----------------------------------------------------------------------------
#
# Mechanics
# - buffer_range_mm (mm)    Usable sensor travel that maps linearly to x ∈ [-1,+1].       # PAUL existing parameter
#                           All control logic is normalized by this. Increase if your
#                           sensor saturates too easily; decrease for a “tighter” x scale.
# - buffer_max_range_mm (mm)Physical clamp of the spring/buffer travel (|x| clipping).    # PAUL existing parameter
#                           Must be ≥ buffer_range_mm. Used by the simulator and for
#                           visualization/safety margins.
# - sensor_type             "P" => proportional z ∈ [-1, +1]; enables KD
#                           "D" => discrete dual switch z ∈ {-1,0,+1}; KD is ignored
#                           "CO" => discrete one switch z ∈ {0,+1}; KD is ignored
#                           "TO" => discrete one switch z ∈ {-1,0}; KD is ignored
#
# Core lag tuning (readiness r)
# - sensor_lag_mm (mm)      Motion required before treating sensor changes as “fresh info”.
#                           r ramps from 0→1 across this distance (gates smoothing/rates).
#                           0 disables gating (r=1 always).
# - info_delta_a            For Type-A only: minimum |Δz| to count as “new info”.
#                           Helps suppress tiny noise from constantly resetting the lag meter.
#
# Gains (PD on x with deadband)
# - kp                      Proportional gain on x (after deadband). Larger → stronger pull
#                           toward neutral; too high can oscillate near zero.
# - kd                      Derivative on x (Type-A only; requires dt>0). Dampens fast x
#                           changes. Set 0 to disable if your signal is noisy.
# - ctrl_deadband           No-action band around x=0. Prevents over-correcting tiny errors.
#
# EKF noises
# - q_x                     Process noise on x. Larger trusts the model less → faster tracking,
#                           but noisier estimates.
# - q_c                     Process noise on c (calibration). Larger lets c drift/learn faster.
# - r_type_prop             Measurement noise for Type-A. Larger trusts the sensor less.
# - r_type_switch_extreme   Effective measurement noise for Type-B when z is ±1 (extremes only).
#
# Calibration bounds
# - c_min, c_max            Hard clamps for c (effective compliance/throughput factor).
#                           Keep wide enough to cover materials but not so wide that c runs away.
#
# FlowGuard (distance-based)
# - flowguard_extreme_threshold  Threshold in x or z treated as “pegged” (≈ jam/tangle).
#                                Used for detection, readiness floor, and relief logic.
# - flowguard_relief_mm (mm)     Required accumulated “relief” motion to prove we tried to      # PAUL user parameter
#                                correct an extreme. If None, defaults to 0.5*buffer_range_mm.
# - flowguard_motion_mm (mm)     Required accumulated motion while extreme to declare a fault.  # PAUL user parameter
#                                If None, defaults to rd_filter_len_mm.
# - flowguard_gate_by_sensor     True: use raw sensor (z) to determine extremes when available;
#                                False: use estimated x only.
#
# Rotation distance
# - rd_start (mm)                Default/persisted baseline RD. Used as mirror reference for mapping  # PAUL last calibrated value
# - rd_min_max_speed_multiplier  Allowed RD bounds based on % speed                           # PAUL user parameter
# - rd_twolevel_speed_multiplier Min/Max RD based on % speed for twolevel operation           # PAUL user parameter
#
# Distance-based smoothing & slew
# - rd_filter_len_mm (mm)   Exponential smoothing length vs extruder motion. Alpha =
#                           1 - exp(-|Δmm| / L). Larger L = slower RD changes.
# - rd_rate_per_mm          Hard rate limit on |ΔRD| per mm of motion (scaled by readiness r).
#                           None disables. Works together with rd_filter_len_mm; the tighter one wins.
#
# Extreme behavior
# - readiness_extreme_floor Minimum readiness r when the sensor/estimate is pegged. Ensures
#                           RD can change quickly enough under clear faults.
# - rate_extreme_multiplier Multiplier on rd_rate_per_mm when pegged (speed up corrections).
# - snap_at_extremes        If True, apply relief-biased snap when pegged
#                           per update) to move away from the peg.
# - extreme_relief_frac     Fraction of |d_ext| used to compute a relief RD step each update
#                           when snap_at_extremes is active. Typical 0.15–0.35.
#
# Neutral trim near zero
# - k_trim                  Small multiplicative trim near x≈0 to bias RD toward exact neutral.
#                           Too high can introduce bias; 0 disables. (Direction-aware in code.)
# - trim_band               Apply trim only when |x| ≤ trim_band. If None, uses max(0.06, ctrl_deadband).
#
# Autotune (persistence)
# - autotune_enabled         Is autotune configured enabled or not. Can be dynamicall enabled/disabled.
# - autotune_motion_exponent Settle scaling: ~ (min_delta/delta)**exponent
#
# - autotune_stable_x_thresh Consider “near neutral” if |x| ≤ this. Determines when we
#                            accumulate samples for autotune.
# - autotune_stable_time_s   Minimum time spent near neutral before we consider autotuning.
# - autotune_deadband_frac   Relative deadband vs previous baseline (e.g., 0.02 = 2%) to
#                            avoid recommending trivial changes.
# - autotune_var_rel_frac    Max allowed std(speed) near neutral required for autotune to propose an update
# - autotune_basis           "time" | "motion" | "either" | "both" — which gates must pass.
# - autotune_motion_mm       Motion near neutral required if basis uses motion.
# - autotune_cooldown_s/mm   Minimum time/motion since the last autotune before another suggestion.
# - autotune_min_delta_abs   Absolute minimum |ΔRD| vs baseline to consider for autotune.
# - autotune_min_delta_frac  Fractional minimum vs baseline (take max of abs and frac checks).
# - autotune_significance_z  Z-score gate for two-level estimator (0 disables, 2≈95% conf).
#
# Tuning tips:
# - If RD reacts too sluggishly in normal operation, decrease rd_filter_len_mm and/or increase
#   rd_rate_per_mm (watch stability near neutral).
# - If you see chatter near x=0, raise ctrl_deadband, reduce kp and/or kd, or increase r_type_prop.
# - If FlowGuard trips too early, raise flowguard_motion_mm and/or flowguard_relief_mm slightly.
# - If autotune fires too often, increase autotune_deadband_frac or the cooldowns; if it
#   never fires, reduce autotune_stable_time_s and/or autotune_motion_mm.

@dataclass
class ControllerConfig:
    # Mechanics
    buffer_range_mm: float = 8.0           # sensor usable travel (maps to normalized [-1,+1])
    buffer_max_range_mm: float = 14.0      # physical max travel (spring clamp) ≥ buffer_range_mm
    sensor_type: SensorType = "P"

    # Core lag tuning (readiness r)
    sensor_lag_mm: float = 0.0             # expected motion to see new info; 0 => no lag gating (r=1)
    info_delta_a: float = 0.08             # Type-P: min sensor delta to count as "new info"

    # Gains (PD on x with deadband)
    kp: float = 0.4
    kd: float = 0.3                        # derivative term (used for Type-P)
    ctrl_deadband: float = 0.1             # neutral deadband for PD around x=0

    # EKF noises
    q_x: float = 1e-3
    q_c: float = 5e-5
    r_type_prop: float = 2.5e-2
    r_type_switch_extreme: float = 1e-2

    # Calibration bounds
    c_min: float = 0.25
    c_max: float = 4.0

    # FlowGuard (distance-based)
    flowguard_extreme_threshold: float = 0.95
    flowguard_relief_mm: Optional[float] = None
    flowguard_motion_mm: Optional[float] = None
    flowguard_gate_by_sensor: bool = True

    # Rotation distance
    rd_start: float = 20.0                     # nominal baseline used for mapping
    rd_min_max_speed_multiplier:  float = 0.5  # ±50% speed
    rd_twolevel_speed_multiplier: float = 0.05 # ±5% speed

    # Distance-based smoothing & slew
    rd_filter_len_mm: float = 40.0         # exp smoothing length (mm of extruder motion for ~63% step @ r=1)
    rd_rate_per_mm: Optional[float] = 0.06 # per-mm hard rate limit on ΔRD (scaled by readiness)

    # Extreme behavior control
    readiness_extreme_floor: float = 0.7   # when pegged, raise r to at least this
    rate_extreme_multiplier: float = 3.0   # multiply rate cap when pegged
    snap_at_extremes: bool = True          # enable relief-biased snap when pegged
    extreme_relief_frac: float = 0.25      # fraction of |d_ext| of guaranteed relief per update

    # Neutral trim near zero
    k_trim: float = 0.06
    trim_band: Optional[float] = None      # if None, uses max(0.06, ctrl_deadband)

    # Autotune master switch + scaling
    autotune_enabled: bool = True          # if False, autotune never proposes/applies updates
    autotune_motion_exponent: float = 1.0  # settle scaling: required ~ (min_delta/delta)**exponent

    # Autotune - PD window (time/motion near neutral)
    autotune_stable_x_thresh: float = 0.12
    autotune_stable_time_s: float = 6.0
    autotune_deadband_frac: float = 0.02   # Allow ≈2%
    autotune_var_rel_frac: float = 0.003   # Allow ≈0.3% relative speed std
    autotune_basis: str = "both"
    autotune_motion_mm: Optional[float] = None
    autotune_cooldown_s: float = 10.0
    autotune_cooldown_mm: float = 40.0
    autotune_min_delta_abs: float = 0.02
    autotune_min_delta_frac: float = 0.005
    autotune_significance_z: float = 2.0   # z-score (confidence) threshold to accept new RD (0 disables, 2≈95%)

    os_min_flip_mm: float = 5.0            # minimum motion between flips (anti-chatter)

    # Optional two-level for P/D
    use_twolevel_for_type_pd: bool = True
    pd_twolevel_threshold: float = 0.66    # P extreme if z>=+thr or z<=-thr

    # Two-level estimation robustness
    twolevel_require_bracket: bool = True  # require seeing both sensor states at expected RD levels

    def __post_init__(self):
        if self.buffer_range_mm <= 0:
            raise ValueError("buffer_range_mm must be > 0")
        if self.buffer_max_range_mm <= 0:
            raise ValueError("buffer_max_range_mm must be > 0")
        if self.buffer_max_range_mm < self.buffer_range_mm:
            raise ValueError("buffer_max_range_mm must be ≥ buffer_range_mm")
        if self.flowguard_relief_mm is None:
            self.flowguard_relief_mm = 0.5 * self.buffer_range_mm
        if self.flowguard_motion_mm is None:
            self.flowguard_motion_mm = self.rd_filter_len_mm
        if self.autotune_motion_mm is None:
            self.autotune_motion_mm = 2.0 * self.rd_filter_len_mm


# ------------------------------- EKF State ------------------------------

@dataclass
class EKFState:
    """EKF state for [x, c] with covariance."""
    x: float = 0.0
    c: float = 1.0
    P11: float = 0.5
    P12: float = 0.0
    P22: float = 0.2
    x_prev: float = 0.0


# ------------------------------ Autotune Engine -------------------------

class _AutotuneEngine:
    """
    Helper object that owns *all* autotune bookkeeping and decisions, but
    reads/writes a few fields on the controller (cfg.rd_start) via a
    controller reference. This keeps update()/reset() in the controller clean
    without changing behavior.
    """

    def __init__(self, ctrl):
        self.ctrl = ctrl
        start = float(ctrl.cfg.rd_start)

        # Core counters
        self._total_motion_mm = 0.0
        self._elapsed_time_s = 0.0

        # PD window stats
        self._stable_time = 0.0
        self._stable_motion_mm_acc = 0.0
        self._rd_sum = 0.0
        self._rd_sum_sq = 0.0
        self._rd_count = 0

        # Autotune anchors & cooldown trackers
        self._autotune_last_value = start
        self._autotune_last_time_s = -1e12
        self._autotune_last_motion_mm = -1e12
        self._autotune_baseline = start
        self._autotune_reset_start = start
        self._last_accepted_rd = start

        # Two-level estimator buckets & evidence
        self._tl_flips = 0
        self._tl_seen_low_ok = False
        self._tl_seen_high_ok = False
        self._tl_updates_since_flip = 0

        # Seen-unseen guards for CO/TO
        self._co_seen_contact_any = False
        self._to_seen_open_any = False

        # Segment/cycle tracking for two-level duty estimator
        self._tl_seg_level = None          # "low" / "high"
        self._tl_seg_mm = 0.0
        self._tl_samples_low = []
        self._tl_samples_high = []
        self._tl_last_unpaired_low = None
        self._tl_last_unpaired_high = None
        self._tl_cycles = []               # list of (low_mm, high_mm)
        self._tl_seg_window = 6            # moving window per level
        self._tl_cycle_window = 4          # moving window of paired cycles

    def restart(self, rd_start, reset_cooldown=True):
        """
        Rebase all autotune anchors/windows on a fresh baseline for new starting rd value
          - Cooldown timers are either reset-to-now (default) or to a large negative origin
            (reset_cooldown=False) to preserve reset() behavior.
        """
        start = float(rd_start)

        # Autotune anchors & cooldown trackers
        self._autotune_last_value = start
        self._autotune_baseline = start
        self._autotune_reset_start = start
        self._last_accepted_rd = start

        # Reset or preserve cooldown origins
        if reset_cooldown:
            self._autotune_last_time_s = self._elapsed_time_s
            self._autotune_last_motion_mm = self._total_motion_mm
        else:
            self._autotune_last_time_s = -1e12
            self._autotune_last_motion_mm = -1e12

        # Core counters
        self._total_motion_mm = 0.0
        self._elapsed_time_s = 0.0

        # PD window stats
        self._stable_time = 0.0
        self._stable_motion_mm_acc = 0.0
        self._rd_sum = 0.0
        self._rd_sum_sq = 0.0
        self._rd_count = 0

        # Two-level estimator buckets & evidence
        self._tl_flips = 0
        self._tl_seen_low_ok = False
        self._tl_seen_high_ok = False
        self._tl_updates_since_flip = 0

        # Seen-unseen guards for CO/TO
        self._co_seen_contact_any = False
        self._to_seen_open_any    = False

        # Clear segment/cycle tracking for two-level duty estimator
        self._tl_seg_level = None
        self._tl_seg_mm = 0.0
        self._tl_samples_low = []
        self._tl_samples_high = []
        self._tl_last_unpaired_low = None
        self._tl_last_unpaired_high = None
        self._tl_cycles = []

    def note_twolevel_tick(self, os_level, flipped, in_contact, move_abs_mm):
        """
        Called once per update() in two-level branch to keep buckets/evidence up-to-date.
        """
        cfg = self.ctrl.cfg

        # Flip handling
        if flipped:
            self._tl_updates_since_flip = 0
            self._tl_flips += 1
        else:
            self._tl_updates_since_flip += 1

        # Count total motion (still keyed to commanded level)
        if move_abs_mm > 0.0:
            self._total_motion_mm += move_abs_mm

        # Segment accumulation and finalize on flip
        if self._tl_seg_level is None:
            self._tl_seg_level = os_level
            self._tl_seg_mm = 0.0

        # Accumulate current segment distance
        if move_abs_mm > 0.0:
            self._tl_seg_mm += move_abs_mm

        # On flip: close previous segment and store sample
        if flipped:
            seg_level = self._tl_seg_level
            seg_mm = self._tl_seg_mm

            if seg_level == "low":
                self._tl_samples_low.append(seg_mm)
                if len(self._tl_samples_low) > self._tl_seg_window:
                    self._tl_samples_low.pop(0)
                # pair with existing high if available
                if self._tl_last_unpaired_high is not None:
                    self._tl_cycles.append((seg_mm, self._tl_last_unpaired_high))
                    if len(self._tl_cycles) > self._tl_cycle_window:
                        self._tl_cycles.pop(0)
                    self._tl_last_unpaired_high = None
                else:
                    self._tl_last_unpaired_low = seg_mm
            else:  # "high"
                self._tl_samples_high.append(seg_mm)
                if len(self._tl_samples_high) > self._tl_seg_window:
                    self._tl_samples_high.pop(0)
                # pair with existing low if available
                if self._tl_last_unpaired_low is not None:
                    self._tl_cycles.append((self._tl_last_unpaired_low, seg_mm))
                    if len(self._tl_cycles) > self._tl_cycle_window:
                        self._tl_cycles.pop(0)
                    self._tl_last_unpaired_low = None
                else:
                    self._tl_last_unpaired_high = seg_mm

            # start new segment for the new level
            self._tl_seg_level = os_level
            self._tl_seg_mm = 0.0

        # Bracket evidence for CO/TO only
        if cfg.sensor_type in ("CO", "TO") and in_contact is not None:
            # Unseen-side guards: do not allow early suggestions
            if cfg.sensor_type == "CO":
                if in_contact:
                    self._co_seen_contact_any = True
                desired_level = "high" if in_contact else "low"
            else:  # "TO"
                if not in_contact:
                    self._to_seen_open_any = True
                desired_level = "low" if in_contact else "high"

            # Mark bracket evidence against the DESIRED level
            if cfg.sensor_type == "CO":
                # low must see OPEN; high must see CONTACT
                if desired_level == "low" and not in_contact:
                    self._tl_seen_low_ok = True
                if desired_level == "high" and in_contact:
                    self._tl_seen_high_ok = True
            else:  # TO
                # low must see CONTACT; high must see OPEN
                if desired_level == "low" and in_contact:
                    self._tl_seen_low_ok = True
                if desired_level == "high" and not in_contact:
                    self._tl_seen_high_ok = True

    def update_autotune(self, d_ext, dt_s, twolevel_active):
        """
        Fast path:
          - If two-level mode is active (CO/TO, or P/D with use_twolevel_for_type_pd=True),
            only query the two-level estimator.
          - Otherwise, only query the PD near-neutral window.

        This avoids unnecessary overhead and prevents double-counting motion/time that
        can occur when both paths are evaluated in the same tick.
        """
        if not self.ctrl.cfg.autotune_enabled:
            return None, None

        if twolevel_active:
            rec_rd, note = self._recommend_rd_from_twolevel(dt_s)
            src = "TwoLevel"
        else:
            rec_rd, note = self._recommend_rd_from_pd_path(d_ext, dt_s)
            src = "PD"

        # No recommendation
        if rec_rd is None:
            return None, None

        # Out of absolute safety bounds
        if not (self.ctrl.rd_min <= rec_rd <= self.ctrl.rd_max):
            # Might be worth a debug message. Perhaps Flowguard not enabled?
            return None, None

        ok, _reason = self._autotune_significant(rec_rd)
        if not ok:
            return None, None

        self._apply_autotune(float(rec_rd)) # PAUL TODO add switch for autotune enable here..
        return float(rec_rd), (note or f"Autotune ({src}): rd≈{rec_rd:.4f}")

    # ----- PD path -----

    def _recommend_rd_from_pd_path(self, d_ext, dt_s):
        """
        Autotune baseline RD using PD path statistics gathered near neutral.
        Returns (rec_rd|None, note|None)
        """
        cfg = self.ctrl.cfg

        move_abs = abs(float(d_ext))
        self._total_motion_mm += move_abs
        self._elapsed_time_s += max(0.0, float(dt_s))

        # Stability accumulation (near neutral)
        stable_gate = abs(self.ctrl.state.x) < cfg.autotune_stable_x_thresh
        if stable_gate:
            self._stable_time += dt_s
            self._stable_motion_mm_acc += move_abs
            self._rd_sum += self.ctrl.rd_current
            self._rd_sum_sq += self.ctrl.rd_current * self.ctrl.rd_current
            self._rd_count += 1
        else:
            self._stable_time = 0.0
            self._stable_motion_mm_acc = 0.0
            self._rd_sum = self._rd_sum_sq = 0.0
            self._rd_count = 0

        # Gating
        time_ok = (self._stable_time >= cfg.autotune_stable_time_s)
        motion_ok = (self._stable_motion_mm_acc >= (cfg.autotune_motion_mm or 0.0))
        if cfg.autotune_basis == "time":
            ready = time_ok
        elif cfg.autotune_basis == "motion":
            ready = motion_ok
        elif cfg.autotune_basis == "either":
            ready = time_ok or motion_ok
        else:
            ready = time_ok and motion_ok

        # Decide
        min_samples = 3
        if not (ready and self._rd_count >= min_samples):
            return None, None

        mean = self._rd_sum / self._rd_count
        var  = max(0.0, self._rd_sum_sq / self._rd_count - mean * mean)
        baseline = self._autotune_baseline
        deadband_ok = (self._frac_speed_delta(mean, baseline) > cfg.autotune_deadband_frac)

        # Speed-relative variance gate
        # Using delta method: Var(1/R) ≈ Var(R)/R^4.
        # Enforce std(speed)/mean(speed) ≤ cfg.autotune_var_rel_frac.
        # This is equivalent to: var(RD) < (f * mean_RD)^2.
        mean_rd = max(mean, 1e-9)
        rel_thresh_rd2 = (cfg.autotune_var_rel_frac * mean_rd) ** 2
        var_ok = (var < rel_thresh_rd2)

        if not (deadband_ok and var_ok):
            return None, None

        note = ("PD autotune: suggest rd≈{:.4f} after {:.1f}s/{:.1f}mm near neutral"
                .format(mean, self._stable_time, self._stable_motion_mm_acc))
        return float(mean), note

    # ----- Two-level path -----

    def _recommend_rd_from_twolevel(self, dt_s):
        """
        Minimal statistical baseline update for two-level mode (CO/TO or optional P/D).
        Returns (rec_rd|None, note|None). Does NOT apply—application is centralized.
        """
        cfg = self.ctrl.cfg
        self._elapsed_time_s += max(0.0, float(dt_s)) # advance elapsed time like the PD path

        # Only evaluate *on flips* (state changes)
        if self._tl_updates_since_flip != 0:
            return None, None

        # Absolutely no two-level suggestion until we've observed the unseen side once
        if cfg.sensor_type == "CO" and not self._co_seen_contact_any:
            return None, None
        if cfg.sensor_type == "TO" and not self._to_seen_open_any:
            return None, None

        # Bracket evidence gate comes FIRST (prevents early duty-based suggestions)
        if cfg.twolevel_require_bracket and not self._twolevel_bracket_ok():
            return None, None

        # Require at least 2 segment samples per state (settled) and ≥2 cycles
        n_low = len(self._tl_samples_low)
        n_high = len(self._tl_samples_high)
        n_cycles = len(self._tl_cycles)
        if n_low < 2 or n_high < 2 or n_cycles < 2:
            return None, None

        # Compute duty fraction using recent cycles: fh_i = high / (low + high)
        fh_list = []
        for (dl, dh) in self._tl_cycles[-self._tl_cycle_window:]:
            tot = max(1e-12, dl + dh)
            fh_list.append(dh / tot)

        if not fh_list:
            return None, None

        # Moving-average fraction in "high" state
        fh_mean = sum(fh_list) / len(fh_list)

        # Duty-weighted RD estimate between low/high
        rd_span = (self.ctrl.rd_high - self.ctrl.rd_low)
        if abs(rd_span) < 1e-9:
            return None, None
        rd_est  = self.ctrl.rd_low + fh_mean * rd_span

        baseline = self._autotune_baseline
        deadband_ok = (self._frac_speed_delta(rd_est, baseline) > cfg.autotune_deadband_frac)
        if not deadband_ok:
            return None, None

        # Significance via z-score from variability of fh across cycles
        z_ok = True
        if cfg.autotune_significance_z > 0.0 and len(fh_list) >= 2:
            # sample std of fh across cycles
            mu = fh_mean
            var_f = sum((f - mu) ** 2 for f in fh_list) / max(1, len(fh_list) - 1)
            std_f = math.sqrt(max(0.0, var_f))
            se_f = std_f / math.sqrt(len(fh_list)) if len(fh_list) > 0 else float("inf")
            se_rd = abs(rd_span) * se_f
            if se_rd <= 1e-9:
                return None, None
            z = abs(rd_est - self._autotune_baseline) / se_rd
            z_ok = (z >= cfg.autotune_significance_z)
            if not z_ok:
                return None, None

        note = f"Two-level autotune: suggest rd≈{rd_est:.4f} (duty {fh_mean:.2f} over {len(fh_list)} cycles)"
        return float(rd_est), note

    # ----- Shared acceptance helpers -----

    def _frac_speed_delta(self, rd_new, rd_ref):
        # |v(new)-v(ref)| / v(ref) with v = 1/rd → |rd_ref/rd_new - 1|
        return abs((rd_ref / max(1e-9, rd_new)) - 1.0)

    def _twolevel_bracket_ok(self) -> bool:
        """
        For one-sided switches:
          CO:  low level should see sensor OPEN (0);   high level should see CONTACT (1).
          TO:  low level should see CONTACT (-1);      high level should see OPEN (0).
        For P/D we do not require this bracket (return True).
        """
        if self.ctrl.cfg.sensor_type not in ("CO", "TO"):
            return True
        return self._tl_seen_low_ok and self._tl_seen_high_ok

    def _autotune_significant(self, rec_rd: float):
        """
        All speed-relative:
          - min fractional speed delta vs LAST SAVED value
          - cooldown scales as (min_frac / frac)**exponent
        Returns: tuple(True|False, reason)
        """
        cfg = self.ctrl.cfg
        last = self._autotune_last_value if self._autotune_last_value is not None else float(cfg.rd_start)

        # Fractional speed change: |v(rec)-v(last)| / v(last) with v = 1/rd  →  |last/rec - 1|
        frac = self._frac_speed_delta(rec_rd, last)
        min_frac = float(cfg.autotune_min_delta_frac)

        if frac < min_frac:
            return False, "below_min_delta"

        # Motion/time since last save
        since_mm = self._total_motion_mm - self._autotune_last_motion_mm
        since_s  = self._elapsed_time_s  - self._autotune_last_time_s

        exponent = max(0.0, cfg.autotune_motion_exponent)
        scale = (min_frac / max(frac, 1e-12)) ** exponent

        req_mm = float(cfg.autotune_cooldown_mm) * scale
        req_s  = float(cfg.autotune_cooldown_s)  * scale

        if since_mm < req_mm or since_s < req_s:
            return False, f"needs_settle_mm≥{req_mm:.1f}, s≥{req_s:.1f}"

        return True, "ok"

    def _apply_autotune(self, rd_new: float):
        # Note: Do NOT change _mirror_ref_rd here; keeps mapping continuous across autotune

        # Reset controllers high/low bounds
        self.ctrl._set_twolevel_rd(float(rd_new))

        # Update autotune anchors and cooldown origins (stay at *current* time/motion).
        self._autotune_last_value   = float(rd_new)
        self._autotune_last_time_s  = self._elapsed_time_s      # keep cooldown honest
        self._autotune_last_motion_mm = self._total_motion_mm    # keep cooldown honest
        self._autotune_baseline     = float(rd_new)
        self.ctrl.cfg.rd_start      = float(rd_new)
        self._last_accepted_rd      = float(rd_new)

        # Light reset of *local* measurement windows (optional, but helpful).
        # (Avoids mixing pre- and post-baseline stats; does NOT bypass cooldown.)
        self._stable_time = 0.0
        self._stable_motion_mm_acc = 0.0
        self._rd_sum = 0.0
        self._rd_sum_sq = 0.0
        self._rd_count = 0

        # Clear bracket-evidence only (as before) so a fresh bracket is required.
        self._tl_seen_low_ok = False
        self._tl_seen_high_ok = False

        # Clear the recent duty samples:
        self._tl_samples_low.clear()
        self._tl_samples_high.clear()
        self._tl_cycles.clear()
        self._tl_seg_level = None
        self._tl_seg_mm = 0.0
        self._tl_last_unpaired_low = None
        self._tl_last_unpaired_high = None


# -------------------------- Controller Core ----------------------------

class FilamentTensionControllerRD:
    """
    Movement-triggered filament tension controller.

    update(extruder_delta_mm, sensor_reading, t_s):
      - Propagates EKF with motion & measurement
      - Computes desired effective gear motion to pull x→0 (PD with derivative if Type-P)
      - Converts to RD target via configurable gear mapping (symmetric/asymmetric), then distance-smoothed
      - Relief-biased snap when sensor pegged
      - Neutral trim near zero
      - FlowGuard detection
      - Autotune of baseline RD (time/motion near neutral, or two-level duty estimator)
    """

    def __init__(self, cfg: ControllerConfig, c0: float = 1.0, x0: Optional[float] = None):
        self.cfg = cfg
        self.K = 2.0 / cfg.buffer_range_mm   # mm → normalized delta in x
        self.state = EKFState()
        self.state.c = max(cfg.c_min, min(cfg.c_max, c0))
        if x0 is not None:
            self.state.x = max(-1.0, min(1.0, x0))
        self.rd_current = float(cfg.rd_start)
        self._set_min_max_rd(self.rd_current)
        self._set_twolevel_rd(self.rd_current)

        # Decoupled mirror reference used for mapping/relief (prevents RD jumps on autotune)
        self._mirror_ref_rd = float(cfg.rd_start)

        # FlowGuard accumulators
        self._comp_motion_mm = 0.0
        self._tens_motion_mm = 0.0
        self._relief_comp_mm = 0.0
        self._relief_tens_mm = 0.0

        # Readiness (lag-aware)
        self._mm_since_info = 0.0
        self._last_info_z: Optional[float] = None

        # UI visualization
        self._vis_est = 0.0

        # Two-level flip-flop state (reused for CO/TO and optional P/D)
        self._os_target_level = "low"   # "low" or "high"
        self._os_since_flip_mm = 0.0
        self._os_contact_prev = False

        # Autotune helper (owns all autotune state/logic)
        self.autotune = _AutotuneEngine(self)

    def _set_twolevel_rd(self, rd):
        """
        Set twolevel rd high/low respecting system limits
        """
        f_raw = max(0.0, min(0.8, self.cfg.rd_twolevel_speed_multiplier))
        # Ideal (unclamped) low/high
        rd_low_ideal  = rd / (1.0 + f_raw) # "low" = faster (smaller RD)
        rd_high_ideal = rd / (1.0 - f_raw) # "high" = slower (larger RD)

        # Respect bounds by shrinking f if needed, preserving symmetry around 'rd'
        f = f_raw
        if rd_low_ideal < self.rd_min or rd_high_ideal > self.rd_max:
            # Solve for the largest f that keeps both inside bounds.
            # rd/(1+f) >= rd_min  -> f <= rd/rd_min - 1
            # rd/(1-f) <= rd_max  -> f <= 1 - rd/rd_max
            f_max_low  = max(0.0, (rd / max(self.rd_min, 1e-9)) - 1.0)
            f_max_high = max(0.0, 1.0 - (rd / max(self.rd_max, 1e-9)))
            f = max(0.0, min(f_raw, f_max_low, f_max_high, 0.8))

        self.rd_low  = self._clamp_to_envelope(rd / (1.0 + f))
        self.rd_high = self._clamp_to_envelope(rd / (1.0 - f))

    def _set_min_max_rd(self, rd):
        """
        Set absolute min/max rd "speeds"
        """
        f = max(0.0, min(0.8, self.cfg.rd_min_max_speed_multiplier))  # keep <1 for safety
        self.rd_min = rd / (1.0 + f)  # "low" = faster (smaller RD)
        self.rd_max = rd / (1.0 - f)  # "high" = slower (larger RD)

    def _clamp_to_envelope(self, rd: float) -> float:
        return max(self.rd_min, min(self.rd_max, rd))

    # ---------- Mapping helpers ----------

    def _gear_mm_from_rd(self, d_ext: float, rd: float) -> float:
        """
        Map RD -> effective gear motion for this update.
        - symmetric: u = d_ext * (rd_ref / rd)   [same formula for ±d_ext]
        - asymmetric (legacy):
            if d_ext > 0: u = d_ext * (rd_ref / rd)
            else:         u = d_ext * (rd / rd_ref)
        """
        rd_ref = self._mirror_ref_rd
        d_ext = float(d_ext)
        if abs(d_ext) < 1e-12:
            return 0.0

        return d_ext * (rd_ref / max(1e-9, rd))

    def _rd_from_desired_gear_mm(self, d_ext: float, u_des: float) -> Optional[float]:
        """
        Invert the mapping to get the RD target from desired effective gear motion.
        Enforces no in-step reversal: u_des * d_ext must be > 0.
        """
        rd_ref = self._mirror_ref_rd
        d_ext = float(d_ext)
        if abs(d_ext) < 1e-12:
            return None
        if u_des * d_ext <= 0:  # can't reverse within an update
            return self.rd_max if d_ext > 0 else self.rd_min

        # u_des = d_ext * (rd_ref / rd)  =>  rd = rd_ref * d_ext / u_des
        denom = u_des if abs(u_des) > 1e-12 else (1e-12 if d_ext > 0 else -1e-12)
        return rd_ref * d_ext / denom

    # --------------------------------- EKF ----------------------------------

    def _ekf_predict(self, extruder_mm: float, gear_cmd_mm: float):
        s, cfg = self.state, self.cfg
        x_pred = s.x + (2.0 / cfg.buffer_range_mm) * (s.c * gear_cmd_mm - extruder_mm)
        c_pred = s.c

        F11 = 1.0
        F12 = (2.0 / cfg.buffer_range_mm) * gear_cmd_mm
        F21 = 0.0
        F22 = 1.0

        P11, P12, P22 = s.P11, s.P12, s.P22
        FP11 = F11*P11 + F12*P12
        FP12 = F11*P12 + F12*P22
        FP21 = F21*P11 + F22*P12
        FP22 = F21*P12 + F22*P22

        s.P11 = FP11*F11 + FP12*F12 + cfg.q_x
        s.P12 = FP11*F21 + FP12*F22
        s.P22 = FP21*F21 + FP22*F22 + cfg.q_c

        s.x = max(-1.25, min(1.25, x_pred))  # soft clamp in estimate space
        s.c = max(cfg.c_min, min(cfg.c_max, c_pred))

    def _ekf_update_type_prop(self, z: float):
        s, cfg = self.state, self.cfg
        z = max(-1.0, min(1.0, float(z)))
        R = cfg.r_type_prop
        y = z - s.x
        S = s.P11 + R
        if S <= 0:
            return
        Kx = s.P11 / S
        Kc = s.P12 / S
        s.x += Kx * y
        s.c += Kc * y
        s.c = max(cfg.c_min, min(cfg.c_max, s.c))
        s.P22 -= (s.P12 * Kc)
        s.P12 *= (1 - Kx)
        s.P11 *= (1 - Kx)

    def _ekf_update_type_switch(self, z: int):
        if z == 0:
            return
        s, cfg = self.state, self.cfg
        target = float(max(-1.0, min(1.0, z)))
        R = cfg.r_type_switch_extreme
        y = target - s.x
        S = s.P11 + R
        if S <= 0:
            return
        Kx = s.P11 / S
        Kc = s.P12 / S
        s.x += Kx * y
        s.c += Kc * y
        s.c = max(cfg.c_min, min(cfg.c_max, s.c))
        s.P22 -= (s.P12 * Kc)
        s.P12 *= (1 - Kx)
        s.P11 *= (1 - Kx)

    # ------------------------------ Sensors -------------------------------

    def _onesided_contact(self, sensor_reading) -> bool:
        """True iff the one-sided sensor is in-contact for this sample."""
        cfg = self.cfg
        thr = max(0.9, cfg.flowguard_extreme_threshold)

        st = cfg.sensor_type

        if st == "CO":  # compression-only
            z = int(sensor_reading)
            return z == 1 or float(z) >= thr

        if st == "TO":  # tension-only
            z = int(sensor_reading)
            return z == -1 or float(z) <= -thr

        return False

    def _onesided_twolevel_target(self, rd_prev: float, d_ext: float, sensor_reading) -> float:
        """
        CO/TO: Pure two-level control.
          - CO:  open -> rd_low (seek compression), contact -> rd_high (relieve)
          - TO:  open -> rd_high (seek tension),    contact -> rd_low  (relieve)
        Uses a small hysteresis on motion (os_min_flip_mm) so we don't chatter.
        Returns the desired RD target for this update (before smoothing/rate limiting).
        """
        cfg = self.cfg
        move = abs(float(d_ext))
        if move > 0:
            self._os_since_flip_mm += move

        in_contact = self._onesided_contact(sensor_reading)

        # Desired level from current contact state
        if cfg.sensor_type == "CO":
            desired_level = "high" if in_contact else "low"
        else:  # "TO"
            desired_level = "low" if in_contact else "high"

        # Flip only if we've moved enough since the last flip
        if desired_level != self._os_target_level and self._os_since_flip_mm >= max(0.0, cfg.os_min_flip_mm):
            self._os_target_level = desired_level
            self._os_since_flip_mm = 0.0
            # flips counted in autotune.note_twolevel_tick()

        # Map level to RD
        return self.rd_low if self._os_target_level == "low" else self.rd_high

    # --- P/D two-level helpers ---

    def _pd_extreme_polarity(self, sensor_reading) -> int:
        """
        +1 = compression extreme, -1 = tension extreme, 0 = neutral.
        P: extreme if z >= +threshold or z <= -threshold.
        D: extreme if z == +1 or z == -1, neutral if z == 0.
        """
        cfg = self.cfg
        if cfg.sensor_type == "P":
            z = float(sensor_reading)
            thr = abs(cfg.pd_twolevel_threshold)
            if z >= thr:
                return +1
            if z <= -thr:
                return -1
            return 0
        elif cfg.sensor_type == "D":
            z = int(sensor_reading)
            return +1 if z == 1 else (-1 if z == -1 else 0)
        return 0

    def _pd_twolevel_target(self, rd_prev: float, d_ext: float, sensor_reading) -> float:
        """
        P/D: Two-level mode (optional via config).
        - Flip only at extremes (neutral band does not change RD).
        - Compression extreme -> rd_high; Tension extreme -> rd_low.
        """
        cfg = self.cfg
        move = abs(float(d_ext))
        if move > 0:
            self._os_since_flip_mm += move

        pol = self._pd_extreme_polarity(sensor_reading)  # +1, -1, 0
        desired_level = self._os_target_level
        if pol > 0:
            desired_level = "high"
        elif pol < 0:
            desired_level = "low"
        # neutral: keep previous target level; no change

        if desired_level != self._os_target_level and self._os_since_flip_mm >= max(0.0, cfg.os_min_flip_mm):
            self._os_target_level = desired_level
            self._os_since_flip_mm = 0.0
            # flips counted in autotune.note_twolevel_tick()

        return self.rd_low if self._os_target_level == "low" else self.rd_high

    # ------------------------------ Flowguard -------------------------------

    def _is_extreme(self, sensor_reading) -> bool:
        """Helper: True if current reading is pegged per sensor type."""
        cfg = self.cfg
        if cfg.sensor_type == "P":
            z = float(sensor_reading)
            peg_thr = max(0.9, cfg.flowguard_extreme_threshold)
            return abs(z) >= peg_thr
        else:
            z = int(sensor_reading)
            if cfg.sensor_type == "D":
                return z in (-1, 1)
            elif cfg.sensor_type == "CO":
                return (z == 1)
            elif cfg.sensor_type == "TO":
                return (z == -1)
        return False

    def _update_flowguard(self, d_ext: float, gear_cmd_mm: float, sensor_reading) -> Dict[str, Any]:
        """
        Distance-based FlowGuard with symmetric handling for one-sided switches.

        - For P/D sensors:
            Uses _extreme_flags() (sensor first, may fall back to x̂ for P/D only).
        - For CO/TO sensors:
            Uses the sensor directly for the *seen* side, and an additional
            open-side gate while z==0 to infer the *unseen* extreme based on:
              * accumulated motion, and
              * accumulated "relief effort" (sign of delta_rel opposite of the extreme)

            CO (compression-only): unseen = TENSION; relief effort is COMPRESSION (delta_rel > 0)
            TO (tension-only)   : unseen = COMPRESSION; relief effort is TENSION   (delta_rel < 0)
        """
        cfg = self.cfg
        move_mm = abs(float(d_ext))

        # Relative motion sign: + compression effort, - tension effort
        # (ĉ * gear_cmd_mm is what the filament *should* experience from gear;
        #  d_ext is what the printer commanded through the nozzle.)
        c_hat = self.state.c
        delta_rel = c_hat * gear_cmd_mm - d_ext  # + compression, - tension

        clog = False
        tangle = False
        reason = None

        # --- capture pre-update accumulator values so we can tell which gate crossed this tick ---
        prev_comp_motion = self._comp_motion_mm
        prev_comp_relief = self._relief_comp_mm
        prev_tens_motion = self._tens_motion_mm
        prev_tens_relief = self._relief_tens_mm

        # Start with direct extremes (sensor-gated; P/D may fall back to x̂)
        comp_ext, tens_ext = self._extreme_flags(sensor_reading)

        # ---------------- One-sided open-side gate (while switch is open) ----------------
        # This only augments CO/TO; it never affects P/D.
        if cfg.sensor_type in ("CO", "TO"):
            z_now = int(sensor_reading)

            # Reset episodic open-side tracking on any state change of the one-sided switch
            if self._last_onesided_z is None or z_now != self._last_onesided_z:
                self._co_open_motion_mm = 0.0
                self._co_open_relief_mm = 0.0
                self._to_open_motion_mm = 0.0
                self._to_open_relief_mm = 0.0
                self._last_onesided_z = z_now

            if z_now == 0:
                # Sensor open: unseen extreme may be present; we accumulate motion and relief effort.
                if cfg.sensor_type == "CO":
                    # Unseen extreme is TENSION; relief is COMPRESSION effort (delta_rel > 0)
                    self._co_open_motion_mm += move_mm
                    if delta_rel > 0:
                        self._co_open_relief_mm += delta_rel
                    if (self._co_open_motion_mm >= cfg.flowguard_motion_mm and
                        self._co_open_relief_mm >= cfg.flowguard_relief_mm):
                        tens_ext = True
                else:  # "TO"
                    # Unseen extreme is COMPRESSION; relief is TENSION effort (delta_rel < 0)
                    self._to_open_motion_mm += move_mm
                    if delta_rel < 0:
                        self._to_open_relief_mm += (-delta_rel)
                    if (self._to_open_motion_mm >= cfg.flowguard_motion_mm and
                        self._to_open_relief_mm >= cfg.flowguard_relief_mm):
                        comp_ext = True
            else:
                # When the one-sided switch is in contact, clear open-side accumulation.
                self._co_open_motion_mm = 0.0
                self._co_open_relief_mm = 0.0
                self._to_open_motion_mm = 0.0
                self._to_open_relief_mm = 0.0

        # ---------------- Mutually exclusive extreme path ----------------
        if comp_ext:
            self._comp_motion_mm += move_mm
            # Relief for compression is *tension* effort (delta_rel < 0)
            if delta_rel < 0:
                self._relief_comp_mm += (-delta_rel)

            comp_motion_ok = (self._comp_motion_mm >= cfg.flowguard_motion_mm)
            comp_relief_ok = (self._relief_comp_mm >= cfg.flowguard_relief_mm)

            if comp_motion_ok and comp_relief_ok:
                crossed_motion = (prev_comp_motion < cfg.flowguard_motion_mm) and comp_motion_ok
                crossed_relief = (prev_comp_relief < cfg.flowguard_relief_mm) and comp_relief_ok
                if crossed_motion and not crossed_relief:
                    gate = "flowguard.motion"
                elif crossed_relief and not crossed_motion:
                    gate = "flowguard.relief"
                elif crossed_motion and crossed_relief:
                    gate = "flowguard.motion and flowguard.relief"
                else:
                    gate = "none"
                clog = True
                reason = "Compression stuck after %.2f mm motion and %.2f mm relief (controlling parameter: %s)" % (
                    self._comp_motion_mm, self._relief_comp_mm, gate
                )

            # reset the other side when compression extreme is active
            self._tens_motion_mm = 0.0
            self._relief_tens_mm = 0.0

        elif tens_ext:
            self._tens_motion_mm += move_mm
            # Relief for tension is *compression* effort (delta_rel > 0)
            if delta_rel > 0:
                self._relief_tens_mm += delta_rel

            tens_motion_ok = (self._tens_motion_mm >= cfg.flowguard_motion_mm)
            tens_relief_ok = (self._relief_tens_mm >= cfg.flowguard_relief_mm)

            if tens_motion_ok and tens_relief_ok:
                crossed_motion = (prev_tens_motion < cfg.flowguard_motion_mm) and tens_motion_ok
                crossed_relief = (prev_tens_relief < cfg.flowguard_relief_mm) and tens_relief_ok
                if crossed_motion and not crossed_relief:
                    gate = "flowguard.motion"
                elif crossed_relief and not crossed_motion:
                    gate = "flowguard.relief"
                elif crossed_motion and crossed_relief:
                    gate = "flowguard.motion and flowguard.relief"
                else:
                    gate = "none"
                tangle = True
                reason = "Tension stuck after %.2f mm motion and %.2f mm relief (controlling parameter: %s)" % (
                    self._tens_motion_mm, self._relief_tens_mm, gate
                )

            # reset the other side when tension extreme is active
            self._comp_motion_mm = 0.0
            self._relief_comp_mm = 0.0

        else:
            # no extreme: reset both sides
            self._comp_motion_mm = 0.0
            self._relief_comp_mm = 0.0
            self._tens_motion_mm = 0.0
            self._relief_tens_mm = 0.0

        return {"clog": clog, "tangle": tangle, "reason": reason}

    def _extreme_flags(self, sensor_reading) -> tuple[bool, bool]:
        cfg = self.cfg
        extreme = cfg.flowguard_extreme_threshold
        comp_ext = tens_ext = False

        if cfg.flowguard_gate_by_sensor:
            if cfg.sensor_type == "P":
                z = float(sensor_reading)
                comp_ext = (z >= extreme)
                tens_ext = (z <= -extreme)
            elif cfg.sensor_type == "D":
                z = int(sensor_reading)
                comp_ext = (z == 1)
                tens_ext = (z == -1)
            elif cfg.sensor_type == "CO":
                # CO directly observes compression only
                z = int(sensor_reading)
                comp_ext = (z == 1)
                tens_ext = False
                return comp_ext, tens_ext  # no x̂ fallback for unseen side
            elif cfg.sensor_type == "TO":
                # TO directly observes tension only
                z = int(sensor_reading)
                comp_ext = False
                tens_ext = (z == -1)
                return comp_ext, tens_ext  # no x̂ fallback for unseen side

        # Only P/D fall back to the estimate if sensor didn't decide
        if cfg.sensor_type in ("P", "D") and not (comp_ext or tens_ext):
            x = self.state.x
            comp_ext = (x >= extreme)
            tens_ext = (x <= -extreme)

        return comp_ext, tens_ext

    # --- control & smoothing ---

    def _desired_effective_gear_mm(self, d_ext: float, dt_s: float) -> float:
        s, cfg = self.state, self.cfg
        dead = max(0.0, cfg.ctrl_deadband)
        x = s.x
        x_ctrl = 0.0 if abs(x) < dead else (x - math.copysign(dead, x))

        kd_eff = cfg.kd if (cfg.sensor_type == "P" and dt_s > 0) else 0.0
        dx = (s.x - s.x_prev) / max(1e-9, dt_s) if kd_eff != 0.0 else 0.0

        return d_ext - cfg.kp * x_ctrl - kd_eff * dx

    def _smooth_rd_by_distance(self, rd_prev: float, rd_target: float, d_ext: float, sensor_reading=None) -> float:
        cfg = self.cfg
        move = abs(float(d_ext))
        r = self._update_readiness_and_get_r(sensor_reading, move) if sensor_reading is not None else 1.0

        # Extreme detection for rate multiplier
        is_extreme = self._is_extreme(sensor_reading) if sensor_reading is not None else False

        # Exponential smoothing
        if cfg.rd_filter_len_mm <= 0:
            alpha_base = 1.0
        else:
            alpha_base = 1.0 - math.exp(-move / cfg.rd_filter_len_mm)
        alpha = r * alpha_base
        rd_filtered = rd_prev + alpha * (rd_target - rd_prev)

        # Rate limit (with extreme multiplier)
        if cfg.rd_rate_per_mm is not None and move > 0:
            rate_mult = (cfg.rate_extreme_multiplier if is_extreme else 1.0)
            max_step = abs(cfg.rd_rate_per_mm) * move * r * rate_mult
            rd_delta = rd_filtered - rd_prev
            if rd_delta >  max_step:
                rd_filtered = rd_prev + max_step
            elif rd_delta < -max_step:
                rd_filtered = rd_prev - max_step

        return self._clamp_to_envelope(rd_filtered)

    # --- readiness (lag-aware) ---

    def _update_readiness_and_get_r(self, sensor_reading, move_abs_mm: float) -> float:
        cfg = self.cfg

        # If user disables lag gating
        if cfg.sensor_lag_mm <= 0:
            r = 1.0
        else:
            self._mm_since_info += move_abs_mm
            if cfg.sensor_type == "P":
                z = float(sensor_reading)
                if self._last_info_z is None or abs(z - self._last_info_z) >= cfg.info_delta_a:
                    self._last_info_z = z
                    self._mm_since_info = 0.0
            else:
                z = int(sensor_reading)
                if self._last_info_z is None or z != self._last_info_z:
                    self._last_info_z = z
                    self._mm_since_info = 0.0
            L = max(1e-6, cfg.sensor_lag_mm)
            r = max(0.0, min(1.0, self._mm_since_info / L))

        # Extreme boost
        if self._is_extreme(sensor_reading):
            r = max(r, cfg.readiness_extreme_floor)

        return r

    # --- UI helper ---

    def _expected_sensor_reading(self, sensor_reading, d_ext: float) -> float:
        cfg = self.cfg
        move = abs(float(d_ext))

        if cfg.sensor_type == "P" or (cfg.sensor_type == "D" and not cfg.use_twolevel_for_type_pd):
            self._vis_est = sensor_reading
            return self._vis_est

        z = int(sensor_reading)
        if z == 1:
            self._vis_est = 1.0
            return self._vis_est
        if z == -1:
            self._vis_est = -1.0
            return self._vis_est

        neutral_lim = max(0.0, cfg.flowguard_extreme_threshold - 0.03)
        xhat = max(-neutral_lim, min(neutral_lim, float(self.state.x)))

        vis_len = 5.0  # mm; small damping for Type-D neutral visualization
        alpha = 1.0 - math.exp(-move / vis_len) if vis_len > 0 else 1.0
        self._vis_est = self._vis_est + alpha * (xhat - self._vis_est)
        self._vis_est = max(-neutral_lim, min(neutral_lim, self._vis_est))
        return self._vis_est


    # ------------------------------ PUBLIC API ------------------------------

    def reset(self, rd_init: float, sensor_input: Union[float, int], t_s: float) -> None:
        """
        Full controller reset for a gear motor swap or new cold start.
        Seeds internal time to `t_s` and zeroes elapsed time.
        """
        cfg = self.cfg

        # --- Rotation distance & baseline (always rebase) ---
        self._set_min_max_rd(rd_init)
        rd_clamped = float(rd_init)
        self.rd_current = rd_clamped
        self.cfg.rd_start = rd_clamped      # default baseline
        self._set_twolevel_rd(self.rd_current)
        self._mirror_ref_rd = rd_clamped    # mirror mapping ref also rebased on reset

        # Rebase autotune helper on the new start
        self.autotune.restart(rd_clamped)

        # --- Seed x̂ from sensor reading ---
        if cfg.sensor_type == "P":
            z = float(sensor_input)
            x0 = max(-1.0, min(1.0, z))
        else:
            z = int(sensor_input)
            z = 1 if z > 0 else (-1 if z < 0 else 0)
            x0 = float(z)

        # --- EKF state & covariance ---
        self.state.x = float(x0)
        self.state.x_prev = self.state.x
        self.state.c = 1.0
        self.state.P11 = 0.5
        self.state.P12 = 0.0
        self.state.P22 = 0.2

        # --- FlowGuard accumulators ---
        self._comp_motion_mm = 0.0
        self._tens_motion_mm = 0.0
        self._relief_comp_mm = 0.0
        self._relief_tens_mm = 0.0

        # --- One-sided open-side gate ---
        self._last_onesided_z = None
        self._co_open_motion_mm = 0.0
        self._co_open_relief_mm = 0.0
        self._to_open_motion_mm = 0.0
        self._to_open_relief_mm = 0.0

        # --- Readiness (lag-aware) ---
        self._mm_since_info = 0.0
        self._last_info_z = float(x0) if cfg.sensor_type == "P" else int(round(x0))

        # --- Time (for update()) ---
        self._time_origin_s = float(t_s)
        self._last_time_s = float(t_s)

        # --- UI ---
        self._vis_est = float(x0) # PAUL TODO initialize to initial sensor position?

        # --- Two-level init for CO/TO ---
        if self.cfg.sensor_type in ("CO", "TO"):
            in_contact0 = self._onesided_contact(sensor_input)
            if self.cfg.sensor_type == "CO":
                self._os_target_level = "high" if in_contact0 else "low"
            else:
                self._os_target_level = "low" if in_contact0 else "high"
            self._os_since_flip_mm = 0.0
            self._os_contact_prev = bool(in_contact0)

        # --- Two-level init for P/D (optional) ---
        if self.cfg.sensor_type in ("P", "D") and self.cfg.use_twolevel_for_type_pd:
            pol0 = self._pd_extreme_polarity(sensor_input)
            if pol0 > 0:
                self._os_target_level = "high"
            elif pol0 < 0:
                self._os_target_level = "low"
            else:
                self._os_target_level = "low"  # neutral start; will flip on first extreme
            self._os_since_flip_mm = 0.0
            self._os_contact_prev = (pol0 != 0)

    def update(self, extruder_delta_mm: float, sensor_reading: Union[float, int], t_s: float) -> Dict[str, Any]:
        """
        Required absolute timestamp `t_s` (seconds). Internally computes dt from the
        last call (or reset). The timestamp must be monotonic non-decreasing.
        """
        if self._last_time_s is None:
            raise RuntimeError("Controller must be reset(t_s=...) before first update().")

        # Compute dt and advance time cursors
        t_s = float(t_s)
        dt_s = max(0.0, t_s - self._last_time_s)
        self._last_time_s = t_s

        cfg = self.cfg
        s = self.state
        d_ext = float(extruder_delta_mm)

        rd_prev = self.rd_current
        gear_cmd_mm = self._gear_mm_from_rd(d_ext, rd_prev)

        # ---------------- CLEAN TWO-LEVEL BRANCH ----------------
        twolevel_active = (cfg.sensor_type in ("CO", "TO")) or (cfg.use_twolevel_for_type_pd and cfg.sensor_type in ("P", "D"))
        if twolevel_active:
            # --- Shadow EKF for FlowGuard only (does NOT affect RD) ---
            # Predict with the RD actually used last update (rd_prev):
            self._ekf_predict(extruder_mm=d_ext, gear_cmd_mm=gear_cmd_mm)

            # Update EKF with whatever the sensor can tell us:
            if cfg.sensor_type == "P":
                self._ekf_update_type_prop(float(sensor_reading))
                sensor_for_logic = sensor_reading
            else:
                # D/CO/TO path
                if cfg.sensor_type == "D":
                    z = int(sensor_reading)
                    if z not in (-1, 0, 1):
                        raise ValueError("Discrete sensor (D) must be -1, 0, or 1")
                    sensor_for_logic = z
                elif cfg.sensor_type == "CO":
                    z_raw = int(sensor_reading)
                    z = 1 if z_raw > 0 else 0
                    if z not in (0, 1):
                        raise ValueError("Compression-only sensor must be 0 or 1")
                    sensor_for_logic = z
                else:  # "TO"
                    z_raw = int(sensor_reading)
                    z = -1 if z_raw < 0 else 0
                    if z not in (0, -1):
                        raise ValueError("Tension-only sensor must be 0 or -1")
                    sensor_for_logic = z
                self._ekf_update_type_switch(int(sensor_for_logic))

            # Determine immediate RD target from two-level rules (no smoothing/PD/trim).
            prev_level = self._os_target_level  # capture before helper (detect flips)
            if cfg.sensor_type in ("CO", "TO"):
                rd_target = self._onesided_twolevel_target(rd_prev, d_ext, sensor_for_logic)
                rd_reason = "Two-level (CO/TO)"
            else:
                rd_target = self._pd_twolevel_target(rd_prev, d_ext, sensor_reading)
                rd_reason = "Two-level (P/D)"

            # --- Flip detection & contact state (for helper bookkeeping) ---
            flipped_this_tick = (self._os_target_level != prev_level)
            in_contact = None
            if cfg.sensor_type in ("CO", "TO"):
                in_contact = self._onesided_contact(sensor_for_logic)

            # Delegate two-level buckets/evidence to helper
            move_abs = abs(d_ext)
            self.autotune.note_twolevel_tick(self._os_target_level, flipped_this_tick, in_contact, move_abs)

            # Clamp and apply immediately.
            rd_applied = self._clamp_to_envelope(rd_target)
            self.rd_current = rd_applied

            # Now compute the gear motion that *this* tick will actually command at the gear.
            gear_cmd_mm_eff = self._gear_mm_from_rd(d_ext, rd_applied)

            # FlowGuard now can fall back to x̂ when the one-sided sensor is neutral (0).
            flowguard = self._update_flowguard(d_ext, gear_cmd_mm_eff, sensor_for_logic if cfg.sensor_type in ("CO", "TO") else sensor_reading)

            # UI helper
            sensor_expected = self._expected_sensor_reading(sensor_reading if cfg.sensor_type == "P" else sensor_for_logic, d_ext)

            # Autotune decision (may update cfg.rd_start)
            auto_rd, auto_note = self.autotune.update_autotune(d_ext, dt_s, True)

            # Outputs
            x_mm = self.state.x * (cfg.buffer_range_mm / 2.0)
            sensor_expected_mm = sensor_expected * (cfg.buffer_range_mm / 2.0)
            out = {
                "rd_prev": float(rd_prev),
                "rd_instant_target": float(rd_target),
                "rd_applied": float(rd_applied),
                "rd_change_applied": float(rd_applied - rd_prev),
                "rd_reason": rd_reason,
                "gear_mm_effect_this_update": float(gear_cmd_mm_eff),
                "x_est": float(self.state.x),
                "x_est_mm": float(x_mm),
                "c_est": float(self.state.c),
                "sensor_expected": float(sensor_expected),
                "sensor_expected_mm": float(sensor_expected_mm),
                "flowguard": flowguard,
                "autotune_rd": auto_rd,
                "autotune_note": auto_note,
            }

            # Keep EKF coherent across calls (again, only for FlowGuard/telemetry)
            self.state.x_prev = self.state.x
            return out

        # ---------------- KALMAN/PD BRANCH ----------------
        # EKF: predict → update
        self._ekf_predict(extruder_mm=d_ext, gear_cmd_mm=gear_cmd_mm)
        if cfg.sensor_type == "P":
            self._ekf_update_type_prop(float(sensor_reading))
        else:
            z = int(sensor_reading)
            if cfg.sensor_type == "D":
                if z not in (-1, 0, 1):
                    raise ValueError("Discrete sensor (D) must be -1, 0, or 1")
            elif cfg.sensor_type == "CO":
                if z not in (0, 1):
                    raise ValueError("Compression-only sensor must be 0 or 1")
            elif cfg.sensor_type == "TO":
                if z not in (0, -1):
                    raise ValueError("Tension-only sensor must be 0 or -1")
            self._ekf_update_type_switch(z)

        # Control: compute instant RD target
        desired_eff = self._desired_effective_gear_mm(d_ext, dt_s)  # = ĉ * u_des
        c_hat = max(cfg.c_min, min(cfg.c_max, s.c))
        u_des = desired_eff / c_hat
        rd_instant = self._rd_from_desired_gear_mm(d_ext, u_des)
        rd_reason = None
        if rd_instant is None:
            rd_instant = rd_prev  # no extruder motion; hold RD
            rd_reason = "Extruder idle; RD held."

        # Relief-biased snap at extremes (guaranteed relief per update)
        comp_ext, tens_ext = self._extreme_flags(sensor_reading)
        if cfg.snap_at_extremes and d_ext != 0.0 and (comp_ext or tens_ext):
            zsign = 1 if comp_ext else -1  # +1 compression, -1 tension
            relief_frac = max(0.05, min(0.60, float(cfg.extreme_relief_frac)))
            rd_ref = self._mirror_ref_rd

            # Derived from: delta_rel = d_ext * (c_hat * rd_ref / rd - 1)
            sgn = 1.0 if d_ext > 0 else -1.0
            denom = 1.0 - (sgn * zsign) * relief_frac
            denom = max(0.05, denom)  # avoid blow-up
            rd_relief = (c_hat * rd_ref) / denom

            rd_instant = self._clamp_to_envelope(rd_relief)
            rd_reason = "Relief-biased snap at extreme"

        # Neutral trim near zero (direction aware)
        if not (comp_ext or tens_ext) and d_ext != 0.0:
            trim_band = cfg.trim_band if cfg.trim_band is not None else max(0.06, cfg.ctrl_deadband)
            xhat = float(s.x)
            if abs(xhat) <= trim_band:
                # Make trim relieve error in the *current motion direction*
                dir_sign = 1.0 if d_ext > 0 else -1.0       # forward(+), retract(-)
                factor = 1.0 + cfg.k_trim * xhat * dir_sign
                factor = max(0.90, min(1.10, factor))       # keep the same safety clamps
                rd_instant *= factor
                rd_reason = (rd_reason + " + neutral trim") if rd_reason else "Neutral trim near zero (dir-aware)"

        # Clamp & smooth
        rd_instant = self._clamp_to_envelope(rd_instant)
        rd_applied = self._smooth_rd_by_distance(
            rd_prev, rd_instant, d_ext, sensor_reading=sensor_reading
        )
        self.rd_current = rd_applied

        # FlowGuard should use the *effective* gear motion we will apply this tick
        gear_cmd_mm_eff = self._gear_mm_from_rd(d_ext, rd_applied)
        flowguard = self._update_flowguard(d_ext, gear_cmd_mm_eff, sensor_reading)

        sensor_expected = self._expected_sensor_reading(sensor_reading, d_ext)

        # Autotune decision (PD path)
        auto_rd, auto_note = self.autotune.update_autotune(d_ext, dt_s, False)

        # Outputs
        x_mm = self.state.x * (cfg.buffer_range_mm / 2.0)
        sensor_expected_mm = sensor_expected * (cfg.buffer_range_mm / 2.0)
        out = {
            "rd_prev": float(rd_prev),
            "rd_instant_target": float(rd_instant),
            "rd_applied": float(rd_applied),
            "rd_change_applied": float(rd_applied - rd_prev),
            "rd_reason": rd_reason,
            "gear_mm_effect_this_update": float(gear_cmd_mm_eff),
            "x_est": float(self.state.x),
            "x_est_mm": float(x_mm),
            "c_est": float(self.state.c),
            "sensor_expected": float(sensor_expected),
            "sensor_expected_mm": float(sensor_expected_mm),
            "flowguard": flowguard,
            "autotune_rd": auto_rd,
            "autotune_note": auto_note,
        }

        s.x_prev = s.x
        return out

