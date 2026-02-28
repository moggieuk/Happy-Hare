#!/usr/bin/env bash
# Simple wrapper to run sync_feedback.py with the proper PYTHONPATH

# ----- Argument validation -----
[ "$#" -gt 1 ] && {
    echo "Usage: $0 [<debug_log.jsonl>]"
    exit 1
}
log="${1:-/tmp/sync.jsonl}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MMU_DIR="${SCRIPT_DIR}/../extras/mmu"
export PYTHONPATH="${MMU_DIR}:${PYTHONPATH}"

python "${SCRIPT_DIR}/sync_feedback_sim.py" --plot "$log"

