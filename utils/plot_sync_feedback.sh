#!/usr/bin/env bash
# Simple wrapper to run sync_feedback.py with the proper PYTHONPATH

# ----- Argument validation -----
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <debug_log.jsonl>"
    echo
    echo "Example:"
    echo "  $0 /tmp/sync.jsonl"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MMU_DIR="${SCRIPT_DIR}/../extras/mmu"
export PYTHONPATH="${MMU_DIR}:${PYTHONPATH}"

python "${SCRIPT_DIR}/sync_feedback_sim.py" --plot "$1"

