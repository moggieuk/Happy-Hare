#!/usr/bin/env bash
# Simple wrapper to run sync_feedback.py with the proper PYTHONPATH
#
# Simulation cmd line options:
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
# E.g. realistic type-P proportional sensor simulation:
# ./sim_sync_feedback.sh --sensor-type P --initial-sensor=random --stride-mm=2.5 --chaos=2 --sample-error=0.5 --sensor-lag=0
#
# E.g. realistic type-CO switch sensor simulation:
# ./sim_sync_feedback.sh --sensor-type CO --initial-sensor=random --stride-mm=2.5 --chaos=2 --sample-error=0.5 --sensor-lag=0 --switch-hysteresis=0.2

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MMU_DIR="${SCRIPT_DIR}/../extras/mmu"
export PYTHONPATH="${MMU_DIR}:${PYTHONPATH}"

python "${SCRIPT_DIR}/sync_feedback_sim.py" "$@"

