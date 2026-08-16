#!/usr/bin/env bash
# Select and plot a FlowGuard sync_<gate>.jsonl telemetry log.

set -u

usage() {
    cat <<EOF
Usage: $0 [<sync_<gate>.jsonl>] [options]

Options:
  --log FILE       Plot FILE without prompting
  --log-dir DIR    Search DIR (default: ~/printer_data/logs)
  --out FILE       Save the graph to FILE (default: sim_plot.png)
  -h, --help       Show this help

Additional options after -- are passed to sync_feedback_sim.py.
EOF
}

log=""
log_dir="${LOG_DIR:-${HOME}/printer_data/logs}"
out="${PLOT_OUT:-sim_plot.png}"
plot_args=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --log)
            [ "$#" -ge 2 ] || { echo "--log requires a file" >&2; exit 2; }
            log="$2"
            shift 2
            ;;
        --log-dir)
            [ "$#" -ge 2 ] || { echo "--log-dir requires a directory" >&2; exit 2; }
            log_dir="$2"
            shift 2
            ;;
        --out)
            [ "$#" -ge 2 ] || { echo "--out requires a file" >&2; exit 2; }
            out="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            plot_args+=("$@")
            break
            ;;
        -*)
            # The remaining arguments belong to sync_feedback_sim.py. Wrapper
            # options therefore need to precede plotting options.
            plot_args+=("$@")
            break
            ;;
        *)
            if [ -n "$log" ]; then
                echo "Only one telemetry log may be specified" >&2
                usage >&2
                exit 2
            fi
            log="$1"
            shift
            ;;
    esac
done

if [ -z "$log" ]; then
    shopt -s nullglob
    logs=("$log_dir"/sync_*.jsonl)
    shopt -u nullglob

    # Ignore directories or other unusual matches.
    files=()
    for candidate in "${logs[@]}"; do
        [ -f "$candidate" ] && files+=("$candidate")
    done
    logs=("${files[@]}")

    if [ "${#logs[@]}" -eq 0 ]; then
        echo "No FlowGuard telemetry logs found in '$log_dir'" >&2
        exit 1
    elif [ "${#logs[@]}" -eq 1 ]; then
        log="${logs[0]}"
    else
        echo "Available FlowGuard telemetry logs:"
        index=1
        for candidate in "${logs[@]}"; do
            name="${candidate##*/}"
            gate="${name#sync_}"
            gate="${gate%.jsonl}"
            printf "  %d) Gate %s  %s\n" "$index" "$gate" "$candidate"
            index=$((index + 1))
        done

        if [ ! -t 0 ]; then
            echo "More than one log was found but input is not interactive; use LOG=<file>" >&2
            exit 1
        fi

        while :; do
            printf "Choose a log [1-%d]: " "${#logs[@]}"
            IFS= read -r choice || exit 1
            case "$choice" in
                ''|*[!0-9]*) echo "Enter a number from 1 to ${#logs[@]}." ;;
                *)
                    if [ "$choice" -ge 1 ] && [ "$choice" -le "${#logs[@]}" ]; then
                        log="${logs[$((choice - 1))]}"
                        break
                    fi
                    echo "Enter a number from 1 to ${#logs[@]}."
                    ;;
            esac
        done
    fi
fi

if [ ! -f "$log" ]; then
    echo "FlowGuard telemetry file '$log' does not exist" >&2
    exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mmu_dir="${script_dir}/../extras/mmu"
export PYTHONPATH="${mmu_dir}${PYTHONPATH:+:${PYTHONPATH}}"

# make plot-sync supplies the managed venv explicitly. Direct invocations reuse
# that venv when present, then fall back to Klipper's environment or PATH.
if [ -n "${PYTHON:-}" ]; then
    python_bin="$PYTHON"
elif [ -x "${script_dir}/../venv/bin/python" ]; then
    python_bin="${script_dir}/../venv/bin/python"
elif [ -x "${HOME}/klippy-env/bin/python" ]; then
    python_bin="${HOME}/klippy-env/bin/python"
else
    python_bin="python"
fi

echo "Processing '$log' FlowGuard telemetry file"
"$python_bin" "${script_dir}/sync_feedback_sim.py" --plot "$log" --out "$out" "${plot_args[@]}"
