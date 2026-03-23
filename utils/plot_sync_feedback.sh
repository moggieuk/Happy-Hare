#!/usr/bin/env bash
# Simple wrapper to source klipper python env and run sync_feedback.py with the proper PYTHONPATH

# ----- Argument validation -----
[ "$#" -gt 1 ] && {
    echo "Usage: $0 [<sync_?.jsonl>]"
    exit 1
}

log="${1:-`ls -1 ~/printer_data/logs/sync_?.jsonl 2>/dev/null`}"

if ! [ -e "$log" ]; then
   echo $log Flowguard telemetry file doesn\'t exist
   exit 1
fi

echo Processing ${log} Flowguard telemetry file

source ~/klippy-env/bin/activate
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MMU_DIR="${SCRIPT_DIR}/../extras/mmu"
export PYTHONPATH="${MMU_DIR}:${PYTHONPATH}"

python "${SCRIPT_DIR}/sync_feedback_sim.py" --plot "$log"
