# -*- coding: utf-8 -*-
#
# Happy Hare MMU Software
# Sync Feedback Manager
#
# This module implements a motion-triggered filament tension controller — that adapts gear
# stepper rotation distance (RD) dynamically based on sensor feedback. It offers modes of operation:
#
# 1) Simple dual level RD selection that works with CO (Compression only switch),
#    TO (Tension only switch), and optionally with D (Dual switch) or P (Proportional) sensors
#
# 2) Combined proportional-derivative (PD) controller with Extended Kalman Filter
#    (EKF) for optimal results with D (Dual switch) or P (Proportional) sensors
#
# Flowguard: It also implements protection for all modes/sensor types that will trigger
#            on clog (at extruder) or tangle (at MMU) conditions.
#
# Autotune: An autotuning option can be enabled for dynamic tuning (and persistence) of
#           calibrated MMU gear rotation_distance.
#
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

from __future__  import annotations
from dataclasses import dataclass
from typing      import Optional, Literal, Dict, Any
from collections import deque
import math
import json # for debug log


# Sync-Feedback sensor type:
#  P  = proportional float [-1.0 - 1.0]
#  D  = dual-switch {-1,0,1}
#  CO = compression-only {0,1}
#  TO = tension-only {0,-1}
SensorType = Literal["P", "D", "CO", "TO"]

# -----------------------------------------------------------------------------
# SyncFeedbackManagerConfig reference
# -----------------------------------------------------------------------------
#
# Mechanics
# - buffer_range_mm (mm)    Usable sensor travel that maps linearly to x ∈ [-1,+1].       # PAUL hook up existing parameter
#                           All control logic is normalized by this. Increase if your
#                           sensor saturates too easily; decrease for a “tighter” x scale.
# - buffer_max_range_mm (mm)Physical clamp of the spring/buffer travel (|x| clipping).    # PAUL hook up existing parameter
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
# - info_delta_a            For Type-P only: minimum |Δz| to count as “new info”.
#                           Helps suppress tiny noise from constantly resetting the lag meter.
#
# Gains (PD on x with deadband)
# - kp                      Proportional gain on x (after deadband). Larger => stronger pull
#                           toward neutral; too high can oscillate near zero.
# - kd                      Derivative on x (Type-P only; requires dt>0). Dampens fast x
#                           changes. Set 0 to disable if your signal is noisy.
# - ctrl_deadband           No-action band around x=0. Prevents over-correcting tiny errors.
#
# EKF noises
# - q_x                     Process noise on x. Larger trusts the model less => faster tracking,
#                           but noisier estimates.
# - q_c                     Process noise on c (calibration). Larger lets c drift/learn faster.
# - r_type_prop             Measurement noise for Type-P. Larger trusts the sensor less.
# - r_type_switch_extreme   Effective measurement noise for Type-D when z is ±1 (extremes only).
#
# Calibration bounds
# - c_min, c_max            Hard clamps for c (effective compliance/throughput factor).
#                           Keep wide enough to cover materials but not so wide that c runs away.
#
# FlowGuard (distance-based)
# - flowguard_extreme_threshold  Threshold in x or z treated as “pegged” (≈ jam/tangle).
#                                Used for detection, readiness floor, and relief logic.
# - flowguard_relief_mm (mm)     Required accumulated “relief” motion to prove we tried to      # PAUL hook up user parameter, recommend buffer_max_range or CO/TO or buffer_max_range / 2 for D/P
#                                correct an extreme. If None, defaults to buffer_max_range_mm.
# - flowguard_motion_mm (mm)     Required accumulated motion while extreme to declare a fault.  # PAUL hook up user parameter
#                                If None, defaults to rd_filter_len_mm.
#
# Rotation distance
# - rd_start (mm)                Default/persisted baseline RD. Used as mirror reference for mapping  # PAUL last calibrated value
# - rd_min_max_speed_multiplier  Allowed RD bounds based on % speed                           # PAUL hook up user parameter
# - rd_twolevel_speed_multiplier Min/Max RD based on % speed for twolevel operation           # PAUL hook up user parameter
#
# Distance-based smoothing & slew
# - rd_filter_len_mm (mm)    Exponential smoothing length vs extruder motion.
#                            Alpha = 1 - exp(-|Δmm| / L). Larger L = slower RD changes.
# - rd_rate_per_mm           Hard rate limit on |ΔRD| per mm of motion (scaled by readiness r).
#                            None disables. Works together with rd_filter_len_mm; the tighter one wins.
#
# Extreme behavior
# - readiness_extreme_floor  Minimum readiness r when the sensor/estimate is pegged. Ensures
#                            RD can change quickly enough under clear faults.
# - rate_extreme_multiplier  Multiplier on rd_rate_per_mm when pegged (speed up corrections).
# - snap_at_extremes         If True, apply relief-biased snap when pegged
#                            per update) to move away from the peg.
# - extreme_relief_frac      Fraction of |d_ext| used to compute a relief RD step each update
#                            when snap_at_extremes is active. Typical 0.15–0.35.
#
# Neutral trim near zero
# - k_trim                   Small multiplicative trim near x≈0 to bias RD toward exact neutral.
#                            Too high can introduce bias; 0 disables. (Direction-aware in code.)
# - trim_band                Apply trim only when |x| ≤ trim_band. If None, uses max(0.06, ctrl_deadband).
#
# Autotune
# - autotune_enabled         Is autotune enabled or not
# EKF logic::
# - autotune_stable_x_thresh Consider “near neutral” if |x| ≤ this.
#                            Determines when we accumulate samples for autotune.
# - autotune_stable_time_s   Minimum time spent near neutral before we consider autotuning.
# - autotune_basis           "time" | "motion" | "either" | "both" — which tests must pass.
# - autotune_motion_mm       Motion near neutral required if basis uses motion.
#                            considered too small to avoid recommending trivial changes.
# - autotune_var_rel_frac    Max allowed std(speed) near neutral required for autotune to propose an update
# - autotune_var_len_mm      Distance over which to estimate RD mean/variance during the near-neutral “stable” window.
# Twolevel logic:
# - autotune_significance_z  Z-score tests for twolevel estimator (0 disables, 2≈95% confidence).
# Shared logic:
# - autotune_cooldown_s/mm   Minimum time/motion since the last autotune before another suggestion.
# - autotune_min_delta_frac  Minimum fractional change in speed (where speed = 1 / RD) required for
#                            an autotune update to be considered meaningful.
#
# Tuning tips:
# - If RD reacts too sluggishly in normal operation, decrease rd_filter_len_mm and/or increase
#   rd_rate_per_mm (watch stability near neutral).
# - If you see chatter near x=0, raise autotune_min_delta_frac, reduce kp and/or kd, or increase r_type_prop.
# - If FlowGuard trips too early, raise flowguard_motion_mm and/or flowguard_relief_mm slightly.
# - If autotune fires too often, increase autotune_min_delta_frac or increase the cooldowns; if it
#   never fires, reduce autotune_stable_time_s and/or autotune_motion_mm.

@dataclass
class SyncFeedbackManagerConfig:
    # Logging
    log_sync: bool = False                 # whether to create log of every tick for debugging purposes
    log_file: str = "/tmp/sync.jsonl"      # debugging/plotting json log

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

    # Rotation distance
    rd_start: float = 20.0                     # initial baseline (previous calibrated value)
    rd_min_max_speed_multiplier:  float = 0.25 # ±25% speed
    rd_twolevel_speed_multiplier: float = 0.05 # ±5% speed
    rd_twolevel_boost_multiplier: float = 0.05 # ±5% extra boost speed

    # Distance-based smoothing & slew
    rd_filter_len_mm: float = 40.0          # exp smoothing length (mm of extruder motion for ~63% step @ r=1)
    rd_rate_per_mm: Optional[float] = 0.06  # per-mm hard rate limit on ΔRD (scaled by readiness)

    # Extreme behavior control
    readiness_extreme_floor: float = 0.7    # when pegged, raise r to at least this
    rate_extreme_multiplier: float = 2.0    # multiply rate cap when pegged
    snap_at_extremes: bool = True           # enable relief-biased snap when pegged
    extreme_relief_frac: float = 0.25       # fraction of |d_ext| of guaranteed relief per update

    # Neutral trim near zero
    k_trim: float = 0.06
    trim_band: Optional[float] = None       # if None, uses max(0.06, ctrl_deadband)

    # Autotune master switch + scaling
    autotune_enabled: bool = True           # if False, autotune never proposes/applies updates
    autotune_save: bool = True              # if False, autotune never persists changes

    # EKF logic tests
    autotune_stable_x_thresh: float = 0.12
    autotune_stable_time_s: float = 4.0
    autotune_basis: str = "both"
    autotune_motion_mm: Optional[float] = None
    autotune_var_rel_frac: float = 0.004    # allow ≈0.4% relative speed std
    autotune_var_len_mm:float = None

    # Twolevel logic tests
    autotune_significance_z: float = 1.0    # z-score (twolevel confidence) threshold to accept new RD (0 disables, 1≈68%, 2≈96%)

    # Shared tests
    autotune_cooldown_s: float = 10.0
    autotune_cooldown_mm: float = 100.0
    autotune_min_delta_frac: float = 0.001  # Only consider > ≈0.1% speed change
    autotune_min_save_frac: float = 0.001   # Only consider > ≈0.1% speed change from last persisted value

    # Certainty tracking of rd recommendations
    autotune_cert_window: int = 8           # fifo length (1..8)
    autotune_cert_tau_rel: float = 0.01     # target relative SE (e.g. 1%)
    autotune_cert_n0: float = 3.0           # prior sample penalty
    autotune_cert_hysteresis: float = 0.001 # min score improvement to accept

    os_min_flip_mm: Optional[float] = None  # minimum motion between flips (anti-chatter)

    # Optional two-level for P/D type sensors
    use_twolevel_for_type_pd: bool = True
    pd_twolevel_threshold: float = 0.70     # P extreme if z>=+thr or z<=-thr

    def __post_init__(self):
        if self.buffer_range_mm <= 0:
            raise ValueError("buffer_range_mm must be > 0")
        if self.buffer_max_range_mm <= 0:
            raise ValueError("buffer_max_range_mm must be > 0")
        if self.buffer_max_range_mm < self.buffer_range_mm:
            raise ValueError("buffer_max_range_mm must be ≥ buffer_range_mm")

        # Autotune window defaults
        if self.autotune_motion_mm is None:
            self.autotune_motion_mm = 2.0 * self.rd_filter_len_mm
        if self.autotune_var_len_mm is None:
            self.autotune_var_len_mm = 1.8 * self.rd_filter_len_mm

        # FlowGuard relief threshold (how much "counter-effort" must be proven)
        if self.flowguard_relief_mm is None:
            if self.sensor_type == "P":
                mult = 1.0
            elif self.sensor_type == "D":
                mult = 1.1
                self.flowguard_relief_mm = 1.1 * self.buffer_range_mm
            else: # "CO" or "TO"
                mult = 0.7
            self.flowguard_relief_mm = mult * self.buffer_range_mm

        # FlowGuard motion threshold (how much motion while pegged before tripping)
        if self.flowguard_motion_mm is None:
            if self.sensor_type == "P":
                mult = 1.25
            elif self.sensor_type == "D":
                mult = 1.75
            else: # "CO" or "TO"
                mult = 2.5
            self.flowguard_motion_mm = mult * self.rd_filter_len_mm

        # Min "flip" times
        if self.os_min_flip_mm is None:
           self.os_min_flip_mm = 3.0 if self.sensor_type in ("D", "CO", "TO") else 2.0


# ------------------------------- EKF State ------------------------------

@dataclass
class EKFState:
    """
    EKF state for [x, c] with covariance.
    """
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
    controller reference.
    """

    def __init__(self, ctrl):
        self.ctrl = ctrl

        # Core counters
        self._total_motion_mm = 0.0
        self._total_time_s = 0.0

        # PD window stats
        self._stable_time = 0.0
        self._stable_motion_mm = 0.0
        self._rd_ema_mean = None
        self._rd_ema_var = 0.0

        # Autotune anchors & cooldown trackers
        self._autotune_last_time_s = -1e12
        self._autotune_last_motion_mm = -1e12
        self._autotune_baseline = self.ctrl.rd_ref # Persisted rd setting
        self._autotune_current = self.ctrl.rd_ref  # Current recommendation

        # Suggestion tracking
        self._rd_cert_fifo = deque(maxlen=int(max(1, ctrl.cfg.autotune_cert_window)))
        self._rd_cert_last_score = -1.0

        # Two-level estimator buckets & evidence
        self._tl_flips = 0
        self._tl_updates_since_flip = 0

        # Segment/cycle tracking for two-level duty estimator
        self._tl_seg_level = None          # "low" / "high"
        self._tl_seg_mm = 0.0
        self._tl_samples_low = []          # FIFO of extruder distances traveled while in "low" state
        self._tl_samples_high = []         # FIFO of extruder distances traveled while in "high" state
        self._tl_last_unpaired_low = None
        self._tl_last_unpaired_high = None
        self._tl_cycles = []               # List of (low_mm, high_mm)
        self._tl_seg_window = 6            # Moving window per level
        self._tl_cycle_window = 4          # Moving window of paired cycles
        if ctrl.cfg.sensor_type in ['CO', 'TO']:
            self._tl_min_cycles = 4        # Required minimum number of samples
        else:
            self._tl_min_cycles = 2        # Less because using full "buffer_range"

        # Transition tracking for type-D in EKF mode
        self._ekf_seen_sensor_states = set()

    # -------------------------------- API -----------------------------------

    def restart(self, rd_init, reset_totals=True, reset_cooldown=True, reset_confidence=True):
        """
        Rebase all autotune anchors/windows on a fresh baseline for new starting rd value
          - Cooldown timers are either reset-to-now (default) or to a large negative origin
          - Total counts are optionally reset
        """
        self._autotune_current = rd_init

        # Reset or preserve cooldown origins
        if reset_cooldown:
            self._autotune_last_time_s = self._total_time_s
            self._autotune_last_motion_mm = self._total_motion_mm
        else:
            self._autotune_last_time_s = -1e12
            self._autotune_last_motion_mm = -1e12

        # Suggestion tracking
        if reset_confidence:
            self._rd_cert_fifo = deque(maxlen=int(max(1, self.ctrl.cfg.autotune_cert_window)))
            self._rd_cert_last_score = -1.0

        # Reset or preserve core counters
        if reset_totals:
            self._total_motion_mm = 0.0
            self._total_time_s = 0.0

        # PD window stats
        self._stable_time = 0.0
        self._stable_motion_mm = 0.0

        # Two-level estimator buckets & evidence
        self._tl_flips = 0
        self._tl_updates_since_flip = 0

        # Clear segment/cycle tracking for two-level duty estimator
        self._tl_seg_level = None
        self._tl_seg_mm = 0.0
        self._tl_samples_low = []
        self._tl_samples_high = []
        self._tl_last_unpaired_low = None
        self._tl_last_unpaired_high = None
        self._tl_cycles = []

        # Transition tracking for type-D in EKF mode
        self._ekf_seen_sensor_states.clear()


    def note_twolevel_tick(self, os_level, flipped, d_ext):
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

        # Only accumulate segments after the first flip to remove starting conditions
        if self._tl_flips < 1:
            return

        # Accumulate current segment distance
        self._tl_seg_mm += abs(d_ext)

        # On flip: close previous segment (if started) and store sample
        if flipped:
            seg_level = self._tl_seg_level
            seg_mm = self._tl_seg_mm

            if seg_level == "low":
                self._tl_samples_low.append(seg_mm)
                if len(self._tl_samples_low) > self._tl_seg_window:
                    self._tl_samples_low.pop(0)
                # Pair with existing high if available
                if self._tl_last_unpaired_high is not None:
                    self._tl_cycles.append((seg_mm, self._tl_last_unpaired_high))
                    if len(self._tl_cycles) > self._tl_cycle_window:
                        self._tl_cycles.pop(0)
                    self._tl_last_unpaired_high = None
                else:
                    self._tl_last_unpaired_low = seg_mm

            elif seg_level == "high":
                self._tl_samples_high.append(seg_mm)
                if len(self._tl_samples_high) > self._tl_seg_window:
                    self._tl_samples_high.pop(0)
                # Pair with existing low if available
                if self._tl_last_unpaired_low is not None:
                    self._tl_cycles.append((self._tl_last_unpaired_low, seg_mm))
                    if len(self._tl_cycles) > self._tl_cycle_window:
                        self._tl_cycles.pop(0)
                    self._tl_last_unpaired_low = None
                else:
                    self._tl_last_unpaired_high = seg_mm

            # Start new segment for the new level
            self._tl_seg_level = os_level
            self._tl_seg_mm = 0.0


    def note_d_sensor(self, sensor_reading):
        """
        Called by controller when a D-sensor sample (-1,0,+1) is read.
        """
        self._ekf_seen_sensor_states.add(sensor_reading)


    def update(self, d_ext, dt_s, report_trivial=False):
        """
        On sensor update, recommend rd update based on mode:
          - If two-level mode is active (CO/TO, or P/D with use_twolevel_for_type_pd=True),
            only query the two-level estimator.
          - Otherwise, only query the PD near-neutral window.
        If rd is recommended, run through shared statistical tests
        """
        cfg = self.ctrl.cfg

        if not cfg.autotune_enabled:
            return {"rd": None, "note": None}

        # Track time/movement
        self._total_time_s += max(0.0, float(dt_s))
        self._total_motion_mm += abs(float(d_ext))
        travel = "@{:.0f}s/{:.0f}mm".format(self._total_time_s, self._total_motion_mm)

        # Cooldown - sufficent motion/time since last save
        since_mm = self._total_motion_mm - self._autotune_last_motion_mm
        since_s  = self._total_time_s  - self._autotune_last_time_s
        req_mm = cfg.autotune_cooldown_mm
        req_s  = cfg.autotune_cooldown_s
        if since_mm < req_mm or since_s < req_s:
            return {"rd": None, "note": None}

        if self.ctrl.twolevel_active:
            rec_rd, note = self._recommend_rd_from_twolevel()
        else:
            rec_rd, note = self._recommend_rd_from_ekf_path(d_ext, dt_s)

        # No recommendation but optional reject note
        if rec_rd is None:
            return {"rd": None, "note": "Autotune: {} {}".format(travel, note) if note else None}

        # Perform final shared checks on recommendation...

        if not (self.ctrl.rd_low <= rec_rd <= self.ctrl.rd_high):
            return {"rd": None, "note": "Autotune: {} Rejected rd {:.4f} because out of bounds!".format(travel, rec_rd)}

        # This makes is progressively harder to accept autotune
        rec_rd, _note = self._autotune_confident(rec_rd)
        if rec_rd is None:
            return {"rd": None, "note": "Autotune: {} {}".format(travel, _note) if _note else None}

        # Do nothing on truly trivial changes
        if not report_trivial and math.isclose(rec_rd, self._autotune_current, abs_tol=1e-3):
            return {"rd": None, "note": "Autotune: {} Rejected rd {:.4f} because too trival a delta".format(travel, rec_rd)}

        # We have new tuned rd value...
        self.restart(rec_rd, reset_totals=False, reset_cooldown=False, reset_confidence=False)
        return {"rd": rec_rd, "note": "Autotune: {} {} and {}".format(travel, note, _note)}


    # PAUL: TODO Call this at the END of a sync period when gear is unsynced.
    def save_autotune(self, rd_new):
        """
        Called to perist the new rd and update bowden length
        """
        cfg = self.ctrl.cfg

        frac = self._frac_speed_delta(rd_new, self._autotune_baseline)
        min_frac = cfg.autotune_min_save_frac
        if frac < min_frac:
            print("Autotune: Did not persist rd {:.4f} because fractional speed change {:.2f}% is below threshold {:.2f}%".format(rd_new, frac * 100.0, min_frac * 100.0))
            return

        #print("PAUL TODO persist the tuned rd and updated bowden length")
        self._autotune_baseline = rd_new

    # ---------------------------- Internal Impl -----------------------------

    def _recommend_rd_from_ekf_path(self, d_ext, dt_s):
        """
        Autotune baseline RD using EKF path statistics gathered near neutral.
        Returns: Tuple (rec_rd|None, note|None)
        """
        cfg = self.ctrl.cfg

        # Stability tests near neutral
        stability_test = abs(self.ctrl.state.x) < cfg.autotune_stable_x_thresh

        # Require at least one transition for D-type sensors
        if cfg.sensor_type == "D" and len(self._ekf_seen_sensor_states) < 2:
            stability_test = False

        # Accrue stable time/motion
        move = abs(d_ext)
        if stability_test:
            self._stable_time += dt_s
            self._stable_motion_mm += move

            if move > 0.0:
                L = max(1e-9, cfg.autotune_var_len_mm)
                alpha = 1.0 - math.exp(-move / L)
                x = float(self.ctrl.rd_current)

                if self._rd_ema_mean is None:
                    # Seed on first accepted sample
                    self._rd_ema_mean = x
                    self._rd_ema_var = 0.0
                else:
                    # EWMA mean + West's EW variance
                    m_prev = self._rd_ema_mean
                    d = x - m_prev
                    m_new = m_prev + alpha * d
                    v_new = (1.0 - alpha) * (self._rd_ema_var + alpha * d * d)

                    self._rd_ema_mean = m_new
                    self._rd_ema_var = max(0.0, v_new)
            # if move == 0.0: leave EMA unchanged this tick

        else:
            # Leaving stable test -> drop stats so we don't carry trendy junk
            self._stable_time = 0.0
            self._stable_motion_mm = 0.0
            self._rd_ema_mean = None
            self._rd_ema_var = 0.0

        if self._rd_ema_mean is None:
            return None, None

        # Gate on D-sensor precondition
        if cfg.sensor_type == "D" and len(self._ekf_seen_sensor_states) < 2:
            return None, None

        time_ok = (self._stable_time >= cfg.autotune_stable_time_s)
        motion_ok = (self._stable_motion_mm >= (cfg.autotune_motion_mm or 0.0))
        if cfg.autotune_basis == "time":
            ready = time_ok
        elif cfg.autotune_basis == "motion":
            ready = motion_ok
        elif cfg.autotune_basis == "either":
            ready = time_ok or motion_ok
        else:
            ready = time_ok and motion_ok
        if not ready:
            return None, None

        mean_rd = max(self._rd_ema_mean, 1e-9)
        var_rd  = max(0.0, self._rd_ema_var)

        # Speed-relative variance test: Var(1/R) ≈ Var(R)/R^4  => std(speed)/mean(speed) ≤ f
        f = cfg.autotune_var_rel_frac
        rel_thresh_rd2 = (f * mean_rd) ** 2
        if var_rd > rel_thresh_rd2:
            note = (f"Rejected rd {mean_rd:.4f} due to speed-relative variance {var_rd:.4f} > {rel_thresh_rd2:.4f}")
            return None, note

        # Potential new candidate
        note = f"EKF logic suggests rd≈{mean_rd:.4f} after {self._stable_time:.1f}s/{self._stable_motion_mm:.1f}mm near neutral"
        return mean_rd, note


    def _recommend_rd_from_twolevel(self):
        """
        Minimal statistical baseline update for two-level mode for CO/TO sensor types or
        optionally P/D types if configured in towlevel mode.
        Returns: Tuple (rec_rd|None, note|None)
        """
        cfg = self.ctrl.cfg

        # Only evaluate *on flips* (state changes)
        if self._tl_updates_since_flip != 0:
            return None, None

        # Require min segment samples per state and min total cycles
        n_low = len(self._tl_samples_low)
        n_high = len(self._tl_samples_high)
        n_cycles = len(self._tl_cycles)
        if n_low < self._tl_min_cycles or n_high < self._tl_min_cycles or n_cycles < self._tl_min_cycles:
            return None, None

        # Compute per-cycle fractions for variance (fh_list) and ratio-of-sums for mean duty
        fh_list = []
        dl_sum = 0.0
        dh_sum = 0.0
        for (dl, dh) in self._tl_cycles[-self._tl_cycle_window:]:
            tot = max(1e-12, dl + dh)
            fh_list.append(dh / tot)  # per-cycle fraction (for variance)
            dl_sum += dl
            dh_sum += dh

        if not fh_list:
            return None, None

        # Duty (mean) via ratio-of-sums
        tot_sum = max(1e-12, dl_sum + dh_sum)
        fh_mean = dh_sum / tot_sum

        # Duty-weighted *speed* estimate, then map back to RD to remove RD-space bias
        v_low  = 1.0 / max(1e-9, self.ctrl.rd_low)
        v_high = 1.0 / max(1e-9, self.ctrl.rd_high)
        v_est  = (1.0 - fh_mean) * v_low + fh_mean * v_high
        rd_est = 1.0 / max(1e-9, v_est)

        # rd_est significance test via z-score from variability of fh across cycles to see if it
        # statistically distinguishable from the baseline given the observed variability
        z = None
        if cfg.autotune_significance_z > 0.0 and len(fh_list) >= 2:
            # Sample std of fh across cycles
            mu = sum(fh_list) / float(len(fh_list))
            var_f = sum((f - mu) ** 2 for f in fh_list) / float(max(1, len(fh_list) - 1))
            std_f = math.sqrt(var_f if var_f > 0.0 else 0.0)
            se_f = (std_f / math.sqrt(len(fh_list))) if len(fh_list) > 0 else float("inf")

            # Propagate fh uncertainty through rd = 1 / ((1-f)/rd_low + f/rd_high)
            #  drd/df = rd^2 * (1/rd_low - 1/rd_high) = rd^2 * (v_low - v_high)
            sensitivity = (rd_est ** 2) * abs(v_low - v_high)
            se_rd = sensitivity * se_f # Std error in rd

            if se_rd >= 1e-9:
                z = abs(rd_est - self._autotune_current) / se_rd
                if z < float(cfg.autotune_significance_z):
                    note = ("Rejected rd {:.4f} because z-score {:.2f} not significant (<{:.2f})").format(rd_est, z, cfg.autotune_significance_z)
                    return None, note
            # else: se_rd ~ 0 => treat as pass (perfect/no-variance case)

        # Potential new candidate
        score = ("%.2f" % z) if z is not None else "perfect"
        note = ("Two-level logic suggests rd≈{:.4f} (duty {:.2f} over {} cycles, z-score={})").format(rd_est, fh_mean, len(fh_list), score)
        return rd_est, note


    def _certainty_score(self, samples, tau_rel=0.01, n0=3.0, eps=1e-12):
        """
        Certainty in [0,1]. Higher = more certain.
          - tau_rel: target relative SE; smaller => stricter (e.g., 0.01 = 1%)
          - n0: prior sample penalty; larger => more skepticism with small n
        Returns: (score, mean, se, n)
        """
        vals = [float(v) for v in samples if v is not None]
        n = len(vals)
        if n == 0:
            return 0.0, None, None, 0, None

        m = sum(vals) / float(n)

        if n >= 2:
            mean_sq = sum(v*v for v in vals) / float(n)
            var = max(0.0, mean_sq - m*m) * n / float(max(1, n - 1))  # unbiased
            s = math.sqrt(var)
        else:
            s = 0.0

        se = s / math.sqrt(n) if n > 0 else float('inf')
        rel_se = se / max(abs(m), eps) if m != 0.0 else float('inf')

        # Precision shrinks as relative SE grows; bounded (0,1]
        prec = 1.0 / (1.0 + (rel_se / max(tau_rel, eps)))

        # Sample-size prior; bounded [0,1)
        size = n / (n + float(n0))

        # Enforce "n<2 -> effectively no certainty"
        if n < 2:
            prec = 0.0

        score = prec * size
        return score, m, se, n

    def _frac_speed_delta(self, rd_new, rd_ref):
        # |v(new)-v(ref)| / v(ref) with v = 1/rd => |rd_ref/rd_new - 1|
        return abs((rd_ref / max(1e-9, rd_new)) - 1.0)

    def _autotune_confident(self, rec_rd):
        """
        All speed-relative:
          - min fractional speed delta vs last saved value
        Returns: tuple(True|False, reason)
        """
        cfg = self.ctrl.cfg

        # Require ever increasing certainty score (std error + n)
        fifo_max = cfg.autotune_cert_window
        tau_rel  = cfg.autotune_cert_tau_rel
        n0       = cfg.autotune_cert_n0
        hyster   = cfg.autotune_cert_hysteresis

        # Push proposal and score the window
        self._rd_cert_fifo.append(rec_rd)
        score, mean, se, n = self._certainty_score(self._rd_cert_fifo, tau_rel=tau_rel, n0=n0)
        prev = self._rd_cert_last_score
        threshold = 0 if prev == 0 else max(prev + hyster, 0)
        improved = score > threshold

        if not improved:
            if prev < 0:
                self._rd_cert_last_score = 0.
                note = "Rejected new rd {:.4f} due to certainty score of zero (n={})".format(rec_rd, n)
            else:
                note = "Rejected new rd {:.4f} due to certainty score {:.3f} ≤ prev {:.3f} (n={})".format(rec_rd, score, prev, n)
            return None, note

        self._rd_cert_last_score = score
        note = "with certainty score of {:.3f} (prev {:.3f}), n={}, mean {:.4f}, SE {:.4f}".format(
                score, prev, n, mean if mean is not None else float('nan'), se if se is not None else float('nan'))
        return mean, note

# ----------------------------- Flowguard Engine -------------------------

class _FlowguardEngine:
    """
    Encapsulates FlowGuard state and logic. Determines based on total filament movement
    and amount of rd correction applied if a clog or tangle is likely to have occured.
    A reason string explains the reason for the trigger.
    A single update() entry point to be called on each tick.
    """

    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.reset()

    # -------------------------------- API -----------------------------------

    def reset(self):
        # Accumulators
        self._comp_motion_mm = 0.0
        self._tens_motion_mm = 0.0
        self._relief_comp_mm = 0.0
        self._relief_tens_mm = 0.0

        # One-sided open-side episodic tracking
        self._last_onesided_z = None
        self._co_open_motion_mm = 0.0
        self._co_open_relief_mm = 0.0
        self._to_open_motion_mm = 0.0
        self._to_open_relief_mm = 0.0

        self._min_headroom = self.ctrl.cfg.flowguard_motion_mm

        # FlowGuard arming test
        self._armed = False        # disarmed until a state change while moving
        self._arm_motion_mm = 0.0  # motion since last (or initial) state sample
        self._arm_last_state = None

    def update(self, d_ext, d_gear, sensor_reading):
        """
        Distance-based FlowGuard with symmetric handling for one-sided switches.

        - For P/D sensors:
            Uses controller._extreme_flags() (sensor first, may fall back to x̂ for P/D only).
        - For CO/TO sensors:
            Uses the sensor directly for the *seen* side, and an additional
            open-side test while z==0 to infer the *unseen* extreme based on:
              * accumulated motion, and
              * accumulated "relief effort" (sign of delta_rel opposite of the extreme)

            CO (compression-only): unseen = TENSION; relief effort is COMPRESSION (delta_rel > 0)
            TO (tension-only)    : unseen = COMPRESSION; relief effort is TENSION (delta_rel < 0)
        Returns: Dict {"clog", "tangle", "reason": reason}
        """
        cfg = self.ctrl.cfg
        move_mm = abs(float(d_ext))

        # Relative motion sign: + compression effort, - tension effort
        # (ĉ * d_gear is what the filament *should* experience from gear;
        #  d_ext is what the printer commanded the extruder to move)
        c_hat = self.ctrl.state.c
        delta_rel = c_hat * d_gear - d_ext  # +ve compression, -ve tension

        clog = False
        tangle = False
        reason = None

        # Arming logic to prevent false triggers on startup if thresholds are tight
        self._arm_motion_mm += move_mm
        state_now = self.ctrl._extreme_polarity(sensor_reading)
        if self._arm_last_state is None:
            self._arm_last_state = state_now

        headroom = cfg.flowguard_motion_mm
        relief_headroom = cfg.flowguard_relief_mm

        if not self._armed:
            # Arm when we've moved and observed any change in coarse state
            changed = (state_now != self._arm_last_state)
            fallback_dist = 0.5 * cfg.flowguard_motion_mm # In case sensor is stuck
            if (self._arm_motion_mm > 0.0 and changed) or (self._arm_motion_mm >= fallback_dist):
                print(f"PAUL: Armed: c1={(self._arm_motion_mm > 0.0 and changed)}, c2={(self._arm_motion_mm >= fallback_dist)}")
                self._armed = True
            else:
                return {"clog": clog, "tangle": tangle, "reason": reason, "headroom": headroom, "min_headroom": self._min_headroom, "relief_headroom": relief_headroom, "armed": self._armed} # PAUL armed added
        self._arm_last_state = state_now

        # Capture pre-update accumulator values so we can tell which test crossed this tick
        prev_comp_motion = self._comp_motion_mm
        prev_comp_relief = self._relief_comp_mm
        prev_tens_motion = self._tens_motion_mm
        prev_tens_relief = self._relief_tens_mm

        # Start with direct extremes (sensor-gated; P/D may fall back to x̂)
        comp_ext, tens_ext = self.ctrl._extreme_flags(sensor_reading)

        # One-sided open-side test (while switch is open)
        # This only augments CO/TO; it never affects PD types.
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
                # Sensor open: unseen extreme may be present; we accumulate motion and relief effort
                if cfg.sensor_type == "CO":
                    # Unseen extreme is TENSION; relief is COMPRESSION effort (delta_rel > 0)
                    self._co_open_motion_mm += move_mm
                    if delta_rel > 0:
                        # PAUL self._co_open_relief_mm += delta_rel
                        self._co_open_relief_mm += self._relief_mm_from_rd(d_ext)

                    if (self._co_open_motion_mm >= cfg.flowguard_motion_mm and
                        self._co_open_relief_mm >= cfg.flowguard_relief_mm):
                        tens_ext = True
                else: # "TO"
                    # Unseen extreme is COMPRESSION; relief is TENSION effort (delta_rel < 0)
                    self._to_open_motion_mm += move_mm
                    if delta_rel < 0:
                        self._to_open_relief_mm += (-delta_rel)
                        self._to_open_relief_mm += self._relief_mm_from_rd(d_ext)

                    if (self._to_open_motion_mm >= cfg.flowguard_motion_mm and
                        self._to_open_relief_mm >= cfg.flowguard_relief_mm):
                        comp_ext = True
            else:
                # When the one-sided switch is in contact, clear open-side accumulation
                self._co_open_motion_mm = 0.0
                self._co_open_relief_mm = 0.0
                self._to_open_motion_mm = 0.0
                self._to_open_relief_mm = 0.0

        if comp_ext: # Extreme compression
            self._comp_motion_mm += move_mm

            # Relief for compression is *tension* effort (delta_rel < 0)
            if delta_rel < 0:
                # PAUL self._relief_comp_mm += (-delta_rel)
                self._relief_comp_mm += self._relief_mm_from_rd(d_ext)

            comp_motion_trig = (self._comp_motion_mm >= cfg.flowguard_motion_mm)
            comp_relief_trig = (self._relief_comp_mm >= cfg.flowguard_relief_mm)
            headroom -= self._comp_motion_mm
            relief_headroom -= self._relief_comp_mm

            if comp_motion_trig or comp_relief_trig:
                crossed_motion = (prev_comp_motion < cfg.flowguard_motion_mm) and comp_motion_trig
                crossed_relief = (prev_comp_relief < cfg.flowguard_relief_mm) and comp_relief_trig
                if crossed_motion and not crossed_relief:
                    test = "flowguard_motion"
                elif crossed_relief and not crossed_motion:
                    test = "flowguard_relief"
                elif crossed_motion and crossed_relief:
                    test = "flowguard_motion and flowguard_relief"
                else:
                    test = "none"
                clog = True
                reason = "Compression stuck after %.2f mm motion and %.2f mm relief (triggering parameter: %s)" % (
                    self._comp_motion_mm, self._relief_comp_mm, test
                )

            # Reset the tension side when compression extreme is active
            self._tens_motion_mm = 0.0
            self._relief_tens_mm = 0.0

        elif tens_ext: # Extreme Tension
            self._tens_motion_mm += move_mm

            # Relief for tension is *compression* effort (delta_rel > 0)
            if delta_rel > 0:
                # PAUL self._relief_tens_mm += delta_rel
                self._relief_tens_mm += self._relief_mm_from_rd(d_ext)

            tens_motion_trig = (self._tens_motion_mm >= cfg.flowguard_motion_mm)
            tens_relief_trig = (self._relief_tens_mm >= cfg.flowguard_relief_mm)
            headroom -= self._tens_motion_mm
            relief_headroom -= self._relief_tens_mm

            if tens_motion_trig or tens_relief_trig:
                crossed_motion = (prev_tens_motion < cfg.flowguard_motion_mm) and tens_motion_trig
                crossed_relief = (prev_tens_relief < cfg.flowguard_relief_mm) and tens_relief_trig
                if crossed_motion and not crossed_relief:
                    test = "flowguard_motion"
                elif crossed_relief and not crossed_motion:
                    test = "flowguard_relief"
                elif crossed_motion and crossed_relief:
                    test = "flowguard_motion and flowguard_relief"
                else:
                    test = "none"
                tangle = True
                reason = "Tension stuck after %.2f mm motion and %.2f mm relief (triggering parameter: %s)" % (
                    self._tens_motion_mm, self._relief_tens_mm, test
                )

            # Reset the compression side when tension extreme is active
            self._comp_motion_mm = 0.0
            self._relief_comp_mm = 0.0

        else: # No extreme: reset both sides
            self._comp_motion_mm = 0.0
            self._relief_comp_mm = 0.0
            self._tens_motion_mm = 0.0
            self._relief_tens_mm = 0.0

        # Maintain min_headroom marker
        if headroom < self._min_headroom:
            self._min_headroom = headroom

        # Capture stats before potential reset
        min_headroom = self._min_headroom

        # Auto-reset to allow print continuation
        if clog or tangle:
            self.reset()

        return {"clog": clog, "tangle": tangle, "reason": reason, "headroom": headroom, "min_headroom": min_headroom, "relief_headroom": relief_headroom, "armed": self._armed} # PAUL armed added


    def _relief_mm_from_rd(self, d_ext):
        """
        Relief contribution this tick, in mm, derived from the *commanded* RD offset.
        Magnitude is |rd_ref/rd_current - 1| * |d_ext|.
        (Independent of c_hat so EKF drift can't zero it out.)
        """
        rd_ref = self.ctrl._rd_ref
        rd_cur = self.ctrl.rd_current
        if abs(rd_cur) < 1e-9:
            return 0.0
        relief_factor = abs((rd_ref / rd_cur) - 1.0) # per-mm relative differential
        return abs(d_ext) * relief_factor

# PAUL idea...
#    def _relief_mm_from_rd_model(self, d_ext, delta_rel, mix=0.7):
#        # mix in [0,1]: 1.0 => RD-only, 0.0 => model-only
#        rd_part = self._relief_mm_from_rd(d_ext)
#        model_part = abs(delta_rel)
#        return mix * rd_part + (1.0 - mix) * model_part



# -------------------------- Controller Core ----------------------------

class SyncFeedbackManager:
    """
    Movement-triggered filament tension controller.

    update(extruder_delta_mm, sensor_reading, eventtime):
      - Propagates EKF with motion & measurement
      - Computes desired effective gear motion to pull x→0 (PD with derivative if Type-P)
      - Converts to RD target via configurable gear mapping (symmetric/asymmetric), then distance-smoothed
      - Relief-biased snap when sensor pegged
      - Neutral trim near zero
      - FlowGuard detection
      - Autotune of baseline RD (time/motion near neutral, or two-level duty estimator)
    """

    def __init__(self, cfg: SyncFeedbackManagerConfig, c0= 1.0, x0: Optional[float] = None):
        self.cfg = cfg

        self._tick = 0
        self._log_ready = False

        self.twolevel_active = (
            self.cfg.sensor_type in ("CO", "TO")
            or (self.cfg.use_twolevel_for_type_pd and self.cfg.sensor_type in ("P", "D"))
        )

        self.K = 2.0 / cfg.buffer_range_mm   # mm => normalized delta in x
        self.state = EKFState()
        self.state.c = max(cfg.c_min, min(cfg.c_max, c0))
        if x0 is not None:
            self.state.x = max(-1.0, min(1.0, x0))

        rd_init = float(cfg.rd_start)
        self.rd_current = rd_init # Current rd in effect
        self.rd_ref = rd_init     # Last "tuned" rd
        self._rd_ref = rd_init    # Smoothed version of rd_ref

        # Allows initial wider range of rd until first autotune candidate
        self._twolevel_boost_active = True

        # Set absolute limits for rd range
        self._set_min_max_rd(rd_init)

        # Readiness (lag-aware)
        self._mm_since_info = 0.0
        self._last_info_z: Optional[float] = None

        # UI visualization
        self._vis_est = 0.0

        # Two-level flip-flop state (reused for CO/TO and optional P/D)
        self._os_target_level = "low" # "low" or "high"
        self._os_since_flip_mm = 2.0

        # FlowGuard engine (encapsulates non-shared FlowGuard state/logic)
        self.flowguard = _FlowguardEngine(self)

        # Autotune helper (encapsulates all autotune state/logic)
        self.autotune = _AutotuneEngine(self)

    # ------------------------------------ PUBLIC API ------------------------------------

    def reset(self, rd_init, sensor_reading, eventtime, simulation=False):
        """
        Full controller reset for a gear motor swap or new cold start.
        Seeds internal time to `t_s` and zeroes elapsed time.
        """
        cfg = self.cfg
        self._log_ready = False

        # Rotation distance & baseline (always rebase)
        self.rd_current = rd_init
        self.rd_ref = rd_init
        self._rd_ref = rd_init
        self._twolevel_boost_active = True
        self._set_min_max_rd(rd_init)

        # Rebase autotune helper on the new start
        self.autotune.restart(rd_init)

        # Seed x̂ from sensor reading
        if cfg.sensor_type == "P":
            z = float(sensor_reading)
            x0 = max(-1.0, min(1.0, z))
        else:
            z = int(sensor_reading)
            z = 1 if z > 0 else (-1 if z < 0 else 0)
            x0 = float(z)

        # EKF state & covariance
        self.state.x = float(x0)
        self.state.x_prev = self.state.x
        self.state.c = 1.0
        self.state.P11 = 0.5
        self.state.P12 = 0.0
        self.state.P22 = 0.2

        # Readiness (lag-aware)
        self._mm_since_info = 0.0
        self._last_info_z = float(x0) if cfg.sensor_type == "P" else int(round(x0))

        # Time (for update())
        self._tick = 0
        self._last_time_s = eventtime

        # For UI
        self._vis_est = float(sensor_reading)

        # Two-level init for CO/TO
        if self.cfg.sensor_type in ("CO", "TO"):
            in_contact0 = self._onesided_contact(sensor_reading)
            if self.cfg.sensor_type == "CO":
                self._os_target_level = "high" if in_contact0 else "low"
            else:
                self._os_target_level = "low" if in_contact0 else "high"
            self._os_since_flip_mm = 0.0

        # Two-level init for P/D (optional)
        if self.cfg.sensor_type in ("P", "D") and self.cfg.use_twolevel_for_type_pd:
            pol0 = self._extreme_polarity(sensor_reading, self.cfg.pd_twolevel_threshold)
            if pol0 > 0:
                self._os_target_level = "high"
            elif pol0 < 0:
                self._os_target_level = "low"
            else:
                self._os_target_level = "low"  # neutral start; will flip on first extreme
            self._os_since_flip_mm = 0.0

        # Reset FlowGuard engine state on controller reset
        self.flowguard.reset()

        # Setup special json debug log
        if self.cfg.log_sync:
            self._init_log()

        return self.update(0.0, sensor_reading, eventtime, simulation=simulation)


    def update(self, extruder_delta_mm, sensor_reading, eventtime, simulation=False):
        """
        Required absolute timestamp `t_s` (seconds). Internally computes dt from the
        last call (or reset). The timestamp must be monotonic non-decreasing.
        """
        cfg = self.cfg

        if self._last_time_s is None:
            raise RuntimeError("Controller must be reset(t_s=...) before first update().")

        # Compute dt and advance time cursors
        dt_s = max(0.0, float(eventtime - self._last_time_s)) # Protect against non-monotonic clock
        self._last_time_s = eventtime
        d_ext = float(extruder_delta_mm)

        rd_prev = self.rd_current
        rd_note = None
        d_gear = self._gear_mm_from_rd(d_ext, rd_prev)

        if self.twolevel_active:
            # ------------------- TWO-LEVEL BRANCH ------------------

            self._ekf_predict(extruder_mm=d_ext, gear_mm=d_gear) # Shadow EKF for FlowGuard only

            if cfg.sensor_type == "P":
                self._ekf_update_type_prop(sensor_reading) # Update EKF with whatever the sensor can tell us
            else: # Type-D/CO/TO
                self._ekf_update_type_switch(int(sensor_reading))

            # FlowGuard update
            flowguard_out = self.flowguard.update(d_ext, d_gear, int(sensor_reading) if cfg.sensor_type in ("CO", "TO", "D") else sensor_reading)

            # Determine immediate RD target from two-level rules
            prev_level = self._os_target_level  # Capture before helper changes (detect flips)
            rd_target = self._twolevel_rd_target(rd_prev, d_ext, sensor_reading)
            flipped_this_tick = (self._os_target_level != prev_level)

            # Delegate two-level evidence collection to autotune helper
            self.autotune.note_twolevel_tick(self._os_target_level, flipped_this_tick, d_ext)

        else:
            # ------------- KALMAN/TYPE-PD BRANCH --------------

            self._ekf_predict(extruder_mm=d_ext, gear_mm=d_gear)

            if cfg.sensor_type == "P":
                self._ekf_update_type_prop(float(sensor_reading))
            else: # Type-D
                z = int(sensor_reading)
                self.autotune.note_d_sensor(z)
                self._ekf_update_type_switch(z)

            # FlowGuard update
            flowguard_out = self.flowguard.update(d_ext, d_gear, int(sensor_reading) if cfg.sensor_type == "D" else sensor_reading)

            # Compute immediate RD target
            desired_eff = self._desired_effective_gear_mm(d_ext, dt_s) # = ĉ * u_des
            c_hat = max(cfg.c_min, min(cfg.c_max, self.state.c))
            u_des = desired_eff / c_hat
            rd_target = self._rd_from_desired_gear_mm(d_ext, u_des)
            if rd_target is None:
                rd_target = rd_prev # no extruder motion; hold RD

            # Relief-biased snap at extremes (guaranteed relief per update)
            comp_ext, tens_ext = self._extreme_flags(sensor_reading)
            if cfg.snap_at_extremes and d_ext != 0.0 and (comp_ext or tens_ext):
                zsign = 1 if comp_ext else -1  # +1 compression, -1 tension
                relief_frac = max(0.05, min(0.60, float(cfg.extreme_relief_frac)))
                rd_ref = self._rd_ref # Smoothed reference

                # Derived from: delta_rel = d_ext * (c_hat * rd_ref / rd - 1)
                sgn = 1.0 if d_ext > 0 else -1.0
                denom = 1.0 - (sgn * zsign) * relief_frac
                denom = max(0.05, denom)
                rd_target = (c_hat * rd_ref) / denom
                rd_note = "Relief-biased snap at extreme"

            # Neutral trim near zero (direction aware)
            if not (comp_ext or tens_ext) and d_ext != 0.0:
                trim_band = cfg.trim_band if cfg.trim_band is not None else max(0.06, cfg.ctrl_deadband)
                xhat = float(self.state.x)
                if abs(xhat) <= trim_band:
                    # Make trim relieve error in the *current motion direction*.
                    dir_sign = 1.0 if d_ext > 0 else -1.0       # forward(+), retract(-)
                    factor = 1.0 + cfg.k_trim * xhat * dir_sign
                    factor = max(0.90, min(1.10, factor))       # safety
                    rd_target *= factor

            # Smooth target
            rd_clamped = self._clamp_to_envelope(rd_target)
            rd_target = self._smooth_rd_by_distance(rd_prev, rd_clamped, d_ext, sensor_reading=sensor_reading)

        # ------------- SHARED --------------

        # Now clamp and apply the newly decided RD for future motion
        rd_applied = self._clamp_to_envelope(rd_target)
        self._apply_rd(rd_applied)

        # Update UI helper
        sensor_expected = self._expected_sensor_reading(sensor_reading, d_ext)

        # Autotune decision
        autotune_out = self.autotune.update(d_ext, dt_s, report_trivial=self._twolevel_boost_active)
        auto_rd = autotune_out.get('rd')
        if auto_rd is not None:
            self.rd_ref = auto_rd
            if self.twolevel_active:
                # Reset twolevel rd high/low after first autotune candidate
                self._twolevel_boost_active = False
                self._set_low_high_rd(auto_rd)
        if flowguard_out.get('clog') or flowguard_out.get('tangle'):
            self.autotune.restart(self.rd_ref)

        # Maintain smoothed rd_ref to prevent sudden estimation spikes
        self._update_rd_ref_by_distance(d_ext, sensor_reading)

        # Outputs
        if cfg.log_sync or simulation:
            out = {
                "input": {
                    "tick": self._tick,
                    "t_s": eventtime,
                    "dt_s": dt_s,
                    "d_mm": extruder_delta_mm,
                    "sensor": sensor_reading,
                },
                "output": {
                    "rd_target": rd_target,          # Debugging
                    "rd_ref": self.rd_ref,           # Debugging
                    "rd_ref_smoothed": self._rd_ref, # Debugging
                    "rd_current": self.rd_current,
                    "rd_note": rd_note,              # Debugging
                    "x_est": self.state.x,           # Debugging
                    "c_est": self.state.c,           # Debugging
                    "sensor_ui": sensor_expected,
                    "flowguard": flowguard_out, # Keys: "clog", "tangle", "reason", "headroom", "min_headroom"
                    "autotune": autotune_out,   # Keys: "rd", "note"
                }
            }

            if cfg.log_sync:
                self._append_log(out)
        else:
            out = None

        self.state.x_prev = self.state.x
        self._tick += 1
        return out

    # --------------------------------- Internal Impl ------------------------------------

    def _set_min_max_rd(self, rd):
        """
        Set absolute immutable min/max rd "speeds"
        """
        f_minmax = max(0.0, min(0.99, self.cfg.rd_min_max_speed_multiplier))

        self.rd_min  = rd / (1.0 + f_minmax)
        self.rd_max = rd / (1.0 - f_minmax)
        self._set_low_high_rd(rd) # Also set current low/high in effect


    def _set_low_high_rd(self, rd):
        """
        Set high/low rd "speeds"
        """
        f_minmax = max(0.0, min(0.99, self.cfg.rd_min_max_speed_multiplier))
        if self.twolevel_active:
            f_norm = self.cfg.rd_twolevel_speed_multiplier
            f_boost = self.cfg.rd_twolevel_boost_multiplier if self._twolevel_boost_active else 0.0
            f = max(0.0, min(f_minmax, (f_norm + f_boost)))
        else:
            f = f_minmax

        self.rd_low  = rd / (1.0 + f)
        self.rd_high = rd / (1.0 - f)


    def _clamp_to_envelope(self, rd):
        """
        Never allow rd outside of limits
        """
        return max(self.rd_min, min(self.rd_max, rd))

    # -------------- Mapping helpers -----------------

    def _gear_mm_from_rd(self, d_ext, rd):
        """
        Map RD -> effective gear motion for this update.
        - symmetric: u = d_ext * (rd_ref / rd)   [same formula for ±d_ext]
        - asymmetric (legacy):
            if d_ext > 0: u = d_ext * (rd_ref / rd)
            else:         u = d_ext * (rd / rd_ref)
        """
        rd_ref = self._rd_ref # Smoothed reference
        d_ext = float(d_ext)
        if abs(d_ext) < 1e-12:
            return 0.0

        return d_ext * (rd_ref / max(1e-9, rd))


    def _rd_from_desired_gear_mm(self, d_ext, u_des):
        """
        Invert the mapping to get the RD target from desired effective gear motion.
        Enforces no in-step reversal: u_des * d_ext must be > 0.
        """
        rd_ref = self._rd_ref # Smoothed reference
        d_ext = float(d_ext)
        if abs(d_ext) < 1e-12:
            return None
        if u_des * d_ext <= 0: # Can't reverse within an update
            return self.rd_high if d_ext > 0 else self.rd_low

        # u_des = d_ext * (rd_ref / rd)  =>  rd = rd_ref * d_ext / u_des
        denom = u_des if abs(u_des) > 1e-12 else (1e-12 if d_ext > 0 else -1e-12)
        return rd_ref * d_ext / denom

    # --------------------- EKF ----------------------

    def _ekf_predict(self, extruder_mm, gear_mm):
        """
        Predict with the RD actually used last update (rd_prev):
        """
        s, cfg = self.state, self.cfg
        x_pred = s.x + (2.0 / cfg.buffer_range_mm) * (s.c * gear_mm - extruder_mm)
        c_pred = s.c

        F11 = 1.0
        F12 = (2.0 / cfg.buffer_range_mm) * gear_mm
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

        s.x = max(-1.25, min(1.25, x_pred))  # Soft clamp in estimate space
        s.c = max(cfg.c_min, min(cfg.c_max, c_pred))


    def _ekf_update_type_prop(self, z):
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

    # ----------- Sensor reading helpers  ----------

    def _onesided_contact(self, sensor_reading):
        """ True if the one-sided sensor is in-contact (triggered) """
        cfg = self.cfg

        if cfg.sensor_type == "CO":
            return int(sensor_reading) == 1

        if cfg.sensor_type == "TO":
            return int(sensor_reading) == -1

        return False


    def _extreme_polarity(self, sensor_reading, threshold=None):
        """
        Reduce sensor to a coarse (extreme) states
          P : +1 if ≥ threshold, -1 if ≤ -threshold, else 0
          D : {-1,0,+1} as-is
          CO: {0, +1} as-is
          TO: {-1, 0} as-is
        Normally this is the flowguard threshold but the P/D twolevel is
        usually a slightly lesser test
        """
        cfg = self.cfg
        if cfg.sensor_type == "P":
            z = float(sensor_reading)
            thr = abs(float(cfg.flowguard_extreme_threshold if threshold is None else cfg.pd_twolevel_threshold))
            return 1 if z >= thr else -1 if z <= -thr else 0
        else:
            return int(sensor_reading)


    def _is_extreme(self, sensor_reading):
        """ True if current reading is pegged per sensor type """
        return self._extreme_polarity(sensor_reading) != 0


    def _extreme_flags(self, sensor_reading):
        """ Return (compression_extreme, tension_extreme) """
        p = self._extreme_polarity(sensor_reading)
        return (p == 1, p == -1)

    # -------------- Twolevel helpers  -------------

    def _twolevel_rd_target(self, rd_prev, d_ext, sensor_reading):
        """
        CO/TO: Pure two-level control.
          - CO:  open -> rd_low (seek compression), contact -> rd_high (relieve)
          - TO:  open -> rd_high (seek tension),    contact -> rd_low  (relieve)
        Uses a small hysteresis on motion (os_min_flip_mm) so we don't chatter.
        Returns the desired RD target for this update (before smoothing/rate limiting).

        P/D: Two-level mode (optional via config).
        - Flip only at extremes (neutral band does not change RD).
        - Compression extreme -> rd_high; Tension extreme -> rd_low.
        """
        cfg = self.cfg
        move = abs(d_ext)
        self._os_since_flip_mm += move

        if cfg.sensor_type in ("CO", "TO"):
            # Desired level from current contact state
            in_contact = self._onesided_contact(sensor_reading)

            if cfg.sensor_type == "CO":
                desired_level = "high" if in_contact else "low"
            else: # TO
                desired_level = "low" if in_contact else "high"
        else:
            # Desired level from polarity
            pol = self._extreme_polarity(sensor_reading, cfg.pd_twolevel_threshold) # +1, -1, 0

            if pol > 0:
                desired_level = "high"
            elif pol < 0:
                desired_level = "low"
            else:
                # Neutral: keep previous target level
                desired_level = self._os_target_level

        # Flip only if we've moved enough since the last flip
        if desired_level != self._os_target_level and self._os_since_flip_mm >= max(0.0, cfg.os_min_flip_mm):
            self._os_target_level = desired_level
            self._os_since_flip_mm = 0.0

        # Map level to RD
        rd_target = self.rd_low if self._os_target_level == "low" else self.rd_high

        return rd_target

    # ----------------- EKF helpers  ---------------

    def _desired_effective_gear_mm(self, d_ext, dt_s):
        s, cfg = self.state, self.cfg
        dead = max(0.0, cfg.ctrl_deadband)
        x = s.x
        x_ctrl = 0.0 if abs(x) < dead else (x - math.copysign(dead, x))

        kd_eff = cfg.kd if (cfg.sensor_type == "P" and dt_s > 0) else 0.0
        dx = (s.x - s.x_prev) / max(1e-9, dt_s) if kd_eff != 0.0 else 0.0

        return d_ext - cfg.kp * x_ctrl - kd_eff * dx


    def _smooth_rd_by_distance(self, rd_prev, rd_target, d_ext, sensor_reading=None):
        """
        Glide the current RD towards target using a fixed rd_filter_len_mm motion length
        respecting readiness factor and extreme limits.
        """
        move = abs(float(d_ext))

        # Exponential smoothing for soft glide from rd_prev toward rd_target.
        # Bigger move or higher r => bigger step.
        L = max(1e-9, self.cfg.rd_filter_len_mm)
        alpha_base = 1.0 - math.exp(-move / L)
        r = self._update_readiness_and_get_r(sensor_reading, move) if sensor_reading is not None else 1.0
        alpha = r * alpha_base

        rd_filtered = rd_prev + alpha * (rd_target - rd_prev)

        # Rate limit (with extreme multiplier)
        is_extreme = self._is_extreme(sensor_reading) if sensor_reading is not None else False
        if self.cfg.rd_rate_per_mm is not None and move > 0:
            rate_mult = (self.cfg.rate_extreme_multiplier if is_extreme else 1.0)
            max_step = abs(self.cfg.rd_rate_per_mm) * move * r * rate_mult
            rd_delta = rd_filtered - rd_prev
            if rd_delta >  max_step:
                rd_filtered = rd_prev + max_step
            elif rd_delta < -max_step:
                rd_filtered = rd_prev - max_step

        return rd_filtered


    def _update_rd_ref_by_distance(self, d_ext, sensor_reading=None):
        """
        Glide the internal reference RD (used for mapping calculations) toward the
        autotuner's current recommendation using the same motion-length constant
        as RD smoothing.
        """
        move = abs(float(d_ext))

        # Exponential smoothing. Reusing readiness/boost so we converge faster under clear extremes
        L = max(1e-9, self.cfg.rd_filter_len_mm)
        alpha_base = 1.0 - math.exp(-move / L)
        r = self._update_readiness_and_get_r(sensor_reading, move) if sensor_reading is not None else 1.0
        alpha = r * alpha_base

        # Cache smoothed value
        self._rd_ref = self._rd_ref + alpha * (self.rd_ref - self._rd_ref)


    def _update_readiness_and_get_r(self, sensor_reading, move_abs_mm):
        """
        Returns (lag-aware) readiness value for 0=not ready, to 1=ready now
        The purpose is so that we don’t react fully until we’ve seen enough
        motionor a meaningful sensor change.
        """
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


    def _expected_sensor_reading(self, sensor_reading, d_ext):
        """
        UI helper for prediction of idealized sensor reading
        Can therefore be used to derive buffer piston position
        """
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


    def _apply_rd(self, rd):
        """
        Called to immediately apply the new rd to the gear stepper
        """
        if not math.isclose(self.rd_current, rd):
            self.rd_current = rd
            #print(f"PAUL: FINISH ME -----> Apply rd: {rd:.4f}")

    # -------------- Logging helpers  --------------

    def _init_log(self):
        """
        (Re)create the log file and write a single header entry.
        Clears any existing file.
        """
        header = {
            "header": {
                "rd_start": self.cfg.rd_start,
                "sensor_type": self.cfg.sensor_type,
                "twolevel_active": self.twolevel_active,
                "buffer_range_mm": self.cfg.buffer_range_mm,
                "buffer_max_range_mm": self.cfg.buffer_max_range_mm,
            }
        }

        with open(self.cfg.log_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
        self._log_ready = True

    def _append_log(self, record: dict):
        """
        Append a single JSON object to the log as one line.
        """
        if not self._log_ready:
            return

        with open(self.cfg.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

