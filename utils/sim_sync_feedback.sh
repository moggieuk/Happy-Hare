#!/usr/bin/env bash
# Simple wrapper to run sync_feedback.py with the proper PYTHONPATH

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MMU_DIR="${SCRIPT_DIR}/../extras/mmu"
export PYTHONPATH="${MMU_DIR}:${PYTHONPATH}"

python "${SCRIPT_DIR}/sync_feedback_sim.py" "$@"

