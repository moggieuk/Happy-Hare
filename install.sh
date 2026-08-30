#!/usr/bin/env sh
#
# Happy Hare MMU Software
#
# Installer / Updater launch script with familiar options
#
# Carefully written to only use options that are widely available
# Please report any incompatibility via github issue
#

# Exit immediately on error (really important to catch menuconfig errors / non-saves / aborts)
set -e

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
SCRIPT_NAME=$(basename "$0")

# Get current HH version from the mmu_constants.py file
export HH_VERSION=$(sed -n 's/^VERSION = "\(.*\)".*/\1/p' "$SCRIPT_DIR/extras/mmu/mmu_constants.py")

if [ -n "$(which tput 2>/dev/null)" ]; then
    C_OFF=$(tput -Txterm-256color sgr0)
    C_DEBUG=$(tput -Txterm-256color setaf 5)
    C_INFO=$(tput -Txterm-256color setaf 6)
    C_NOTICE=$(tput -Txterm-256color bold)$(tput -Txterm-256color setaf 2)
    C_WARNING=$(tput -Txterm-256color setaf 3)
    C_ERROR=$(tput -Txterm-256color bold)$(tput -Txterm-256color setaf 1)
fi

usage() {
    USAGE="Usage: $0"
    SPACE=$(echo "${USAGE}" | tr "[:print:]" " ")
    echo "${C_INFO}"
    echo "${USAGE} [-i] [-u] [-d] [-z] [-s] [-t] [-r]"
    echo "${SPACE} [-b <branch>]"
    echo "${SPACE} [-k <klipper_home_dir>] [-c <klipper_config_dir>] [-m <moonraker_home_dir>]"
    echo "${SPACE} [-a <kiauh_alternate_klipper>] [config_file]" # [-p <repetier_server stub>]"
    echo "${C_OFF}"
    echo "${C_INFO}(no flags for safe re-install / upgrade)${C_OFF}"
    echo "${C_INFO}[config_file]${C_OFF} is optional, if not specified the default config filename (.mmu_config) will be used."
    echo "  -i for interactive install (open menuconfig)"
    echo "  -u, -d for uninstall"
    echo "  -f to just restore klipper/moonraker symlinks (recover after hard klipper update)"
    echo "  -z skip github update check (nullifies -b <branch>)"
    echo "  -s to skip restart of services"
    echo "  -b <branch> to switch to specified feature branch (sticky)"
    echo "  -n to specify a multiple MMU unit setup"
    echo "  -k <dir> non-default klipper home directory"
    echo "  -c <dir> non-default klipper config directory"
    echo "  -m <dir> non-default moonraker home directory"
    # TODO: Repetier-Server stub support
    # echo "  -p specify Repetier-Server <stub> to override printer.cfg and klipper.service names"
    echo "  -a <name> to specify alternative klipper-service-name when installed with Kiauh"
    echo "  -r allow running ${SCRIPT_NAME} from a root login (not through sudo)"
    echo "  -e, --emu Enables multi MCU support (e.g. for EMU design)"
    echo "  -o override compatibility checks (e.g. Kalico detection)"
    echo "  -t  activate test mode - write config to /tmp/mmu_test instead of your real install"
    echo "  (-q verbose make for debugging)"
    echo "  (-v verbose builder for debugging)"
    echo
    exit 0
}

ordinal() {
    case "$1" in
    *1[0-9] | *[04-9]) echo "$1"th ;;
    *1) echo "$1"st ;;
    *2) echo "$1"nd ;;
    *3) echo "$1"rd ;;
    esac
}

prompt_yn() {
    while true; do
        printf "%s (y/n)? " "$*"
        if ! read -r yn; then
            echo
            echo "${C_ERROR}Aborting: no answer available on stdin${C_OFF}" >&2
            exit 1
        fi
        case "${yn}" in
        Y | y)
            return 1
            ;;
        N | n)
            return 0
            ;;
        esac
    done
}

prompt_n() {
    max=$1
    shift

    while :; do
        printf "%s (1-%s)? " "$*" "$max" >&2
        read -r sel
        case $sel in
            ''|*[!0-9]*)
                continue
                ;;
        esac
        if [ "$sel" -ge 1 ] && [ "$sel" -le "$max" ]; then
            printf "%s" "$sel"
            return 0
        fi
    done
}

trim() {
    name=${1-}
    name=${name#"${name%%[![:space:]]*}"} # Remove leading whitespace
    name=${name%"${name##*[![:space:]]}"} # Remove trailing whitespace
    echo "${name}"
}

time_elapsed() {
    START_TIME=$("${INSTALLER_PY}" -c "import time; print(time.time())")
    "${@}"
    END_TIME=$("${INSTALLER_PY}" -c "import time; print(time.time())")
    echo "${START_TIME} ${END_TIME}" | awk '{printf "Elapsed: %.1f seconds", $2 - $1}'
    echo
}

# Best-effort guess at the printer config directory, used only to detect a v3 install
# before self-update/uninstall/menuconfig have touched anything. Kconfig resolves this
# authoritatively later in the script; this just needs to be good enough for the
# one-time upgrade prompt below.
guess_klipper_config_home() {
    if [ -n "${TESTDIR}" ]; then
        echo "${TESTDIR}/printer_data/config"
    elif [ -n "${CONFIG_KLIPPER_CONFIG_HOME}" ]; then
        echo "${CONFIG_KLIPPER_CONFIG_HOME}"
    elif [ -d "/usr/data/printer_data/config" ]; then
        echo "/usr/data/printer_data/config"
    else
        echo "${HOME}/printer_data/config"
    fi
}

# Point Moonraker's update manager at a specific branch, so future automatic updates
# keep tracking it instead of drifting back to whatever primary_branch was there before.
# Scoped to the [update_manager happy-hare] section only - other [update_manager ...]
# sections (for other repos) must not be touched.
set_moonraker_primary_branch() {
    branch="$1"
    moonraker_conf="$(guess_klipper_config_home)/moonraker.conf"
    if [ -f "${moonraker_conf}" ] && grep -q '^\[update_manager happy-hare\]' "${moonraker_conf}"; then
        echo "${C_INFO}Pointing Moonraker's update manager at the '${branch}' branch...${C_OFF}"
        awk -v branch="${branch}" '
            /^\[update_manager happy-hare\]/ { in_section=1; print; next }
            /^\[/ { in_section=0 }
            in_section && /^primary_branch:/ { print "primary_branch: " branch; next }
            { print }
        ' "${moonraker_conf}" >"${moonraker_conf}.tmp" && mv "${moonraker_conf}.tmp" "${moonraker_conf}"
    fi
}


##### V3 Upgrade support vvv ##################################################################

# Detect a v3 install: no v4 Kconfig file yet, but a v3-era mmu_parameters.cfg already
# on disk with a "happy_hare_version: 3.x" stamp (the value Happy Hare itself writes
# and trusts for exactly this purpose).
v3_detected() {
    guessed_kconfig="${KCONFIG_CONFIG:-${SCRIPT_DIR}/.mmu_config}"
    v3_cfg="$(guess_klipper_config_home)/mmu/base/mmu_parameters.cfg"
    [ ! -e "${guessed_kconfig}" ] \
        && [ -f "${v3_cfg}" ] \
        && grep -qE '^happy_hare_version:[[:space:]]*3\.' "${v3_cfg}"
}

# The v3 -> v4 choice ("blue pill" stay on v3 / "red pill" upgrade to v4) has to be
# settled before anything else in this script touches git, the filesystem, or Klipper/
# Moonraker config - including self-update's own git pull, -f's symlink fix, and
# uninstall. Runs for any invocation (default, -i, -u, -f, ...); the only exemptions
# are -h/usage (already exited by then) and the internal re-exec after self-update or
# after switching branches below (F_SKIP_UPDATE=force), so this never re-prompts itself
# in a loop.
offer_v3_v4_choice() {
    echo
    echo "${C_WARNING}------------------------------------------------------------------------${C_OFF}"
    echo "${C_WARNING}Happy Hare v4 is a major rework with breaking changes, and your existing${C_OFF}"
    echo "${C_WARNING}install looks like the previous (v3) release.${C_OFF}"
    echo "${C_WARNING}Much like The Matrix you have a choice..${C_OFF}"
    echo
    echo "${C_WARNING}1) Take the Blue 'ignorance' pill and stay on v3${C_OFF}"
    echo "   This switches this checkout to the 'v3' branch and points Moonraker's update"
    echo "   manager at it, so future updates keep tracking v3 instead of v4."
    echo "   You are free to upgrade at a later date."
    echo
    echo "${C_WARNING}2) Take the Red 'awakening' pill and upgrade to v4${C_OFF}"
    echo "   Your whole v3 'mmu' config directory is renamed to 'mmu.V3' (untouched, not"
    echo "   deleted), old includes are removed from printer.cfg and moonraker.conf, and"
    echo "   you're walked through a FRESH v4 setup via menuconfig. Nothing from mmu.V3"
    echo "   is carried over automatically - it's there for you to copy values back out of."
    echo "   ${C_WARNING}NOTE: DOES NOT YET WORK ON KALICO - stay on v3 for now${C_OFF}"
    echo
    echo "More details: https://moggieuk.github.io/Happy-Hare-Doc/Upgrade-v3-v4/"
    echo "${C_WARNING}------------------------------------------------------------------------${C_OFF}"
    echo

    sel=$(prompt_n 2 "Choose your pill (option)")
    echo

    if [ "${sel}" = "1" ]; then
        set_moonraker_primary_branch v3
        echo "${C_INFO}Switching to the 'v3' branch...${C_OFF}"
        export BRANCH=v3
        "$SCRIPT_DIR/installer/self_update.sh" || exit 1
        F_SKIP_UPDATE=force exec "$0" "$@"
    else
        echo "${C_WARNING}Proceeding with the v4 upgrade. Your v3 'mmu' config directory will be${C_OFF}"
        echo "${C_WARNING}renamed to 'mmu.V3' for reference - nothing is deleted.${C_OFF}"
        echo
        # Consulted once Kconfig has resolved real paths (see v3_upgrade_cleanup),
        # since CONFIG_KLIPPER_CONFIG_HOME etc. don't exist yet at this point in the
        # script. export, not just assignment, so it survives the self-update re-exec.
        export F_V3_UPGRADE=y
    fi
}

# Give a completely clean start to a v3 -> v4 upgrade ("red pill", F_V3_UPGRADE=y):
# back up the whole old 'mmu' config directory (never delete it), then strip HH's old
# includes/sections from printer.cfg and moonraker.conf so the imminent `make install`
# rebuilds them fresh rather than merging into stale v3 content.
#
# Must run AFTER Kconfig has resolved real paths (menuconfig/olddefconfig, just above
# this function's call site) - CONFIG_KLIPPER_CONFIG_HOME and friends do not exist
# before that, and guess_klipper_config_home()'s best-effort guess is not good enough
# to risk a mv/rm against. Every path used below is guarded for exactly this reason:
# an unresolved variable here would turn a directory move into an operation on the
# filesystem root.
v3_upgrade_cleanup() {
    unset CONFIG_KLIPPER_CONFIG_HOME CONFIG_PRINTER_CONFIG_FILE CONFIG_MOONRAKER_CONFIG_FILE
    # shellcheck source=.mmu_config
    . "${KCONFIG_CONFIG}"

    case "${CONFIG_KLIPPER_CONFIG_HOME:-}" in
        "~"*) CONFIG_KLIPPER_CONFIG_HOME="${HOME}${CONFIG_KLIPPER_CONFIG_HOME#\~}" ;;
    esac
    case "${CONFIG_PRINTER_CONFIG_FILE:-}" in
        "~"*) CONFIG_PRINTER_CONFIG_FILE="${HOME}${CONFIG_PRINTER_CONFIG_FILE#\~}" ;;
    esac
    case "${CONFIG_MOONRAKER_CONFIG_FILE:-}" in
        "~"*) CONFIG_MOONRAKER_CONFIG_FILE="${HOME}${CONFIG_MOONRAKER_CONFIG_FILE#\~}" ;;
    esac

    if [ -z "${CONFIG_KLIPPER_CONFIG_HOME:-}" ]; then
        echo "${C_ERROR}Could not resolve the printer config directory from '${KCONFIG_CONFIG}' -" \
            "skipping v3 cleanup to be safe. Your old 'mmu' directory is untouched;" \
            "clean it up by hand once you've confirmed the new install works.${C_OFF}"
        return 0
    fi

    printer_cfg="${CONFIG_KLIPPER_CONFIG_HOME}/${CONFIG_PRINTER_CONFIG_FILE:-printer.cfg}"
    moonraker_conf="${CONFIG_KLIPPER_CONFIG_HOME}/${CONFIG_MOONRAKER_CONFIG_FILE:-moonraker.conf}"
    mmu_dir="${CONFIG_KLIPPER_CONFIG_HOME}/mmu"
    backup_dir="${CONFIG_KLIPPER_CONFIG_HOME}/mmu.V3"

    if [ -d "${mmu_dir}" ]; then
        if [ -e "${backup_dir}" ]; then
            backup_dir="${backup_dir}-$(date +%Y%m%d-%H%M%S)"
        fi
        echo "${C_INFO}Backing up your v3 config: '$(basename "${mmu_dir}")' -> '$(basename "${backup_dir}")'${C_OFF}"
        mv "${mmu_dir}" "${backup_dir}"
    fi

    echo "${C_INFO}Removing old v3 includes from '$(basename "${printer_cfg}")' and" \
        "'$(basename "${moonraker_conf}")'...${C_OFF}"
    # -m installer.build needs SCRIPT_DIR on PYTHONPATH so it resolves regardless of the
    # caller's cwd (unlike `make -C`, a bare `python -m` doesn't take a directory to run
    # from). installer.build also imports the vendored kconfiglib at module scope
    # regardless of which function is called, so that needs to be on PYTHONPATH too,
    # matching what the Makefile exports for its own installer.build invocations.
    PYTHONPATH="${SCRIPT_DIR}:${SCRIPT_DIR}/installer/lib/kconfiglib:${PYTHONPATH}" \
        "${INSTALLER_PY}" -m installer.build --uninstall-includes "${printer_cfg}"
    PYTHONPATH="${SCRIPT_DIR}:${SCRIPT_DIR}/installer/lib/kconfiglib:${PYTHONPATH}" \
        "${INSTALLER_PY}" -m installer.build --uninstall-moonraker "${moonraker_conf}"

    echo "${C_INFO}Cleaning up stale v3 Klipper/Moonraker symlinks...${C_OFF}"
    time_elapsed run_make F_NO_SERVICE=y fix_links
}

##### V3 Upgrade support ^^^ ##################################################################


# Resolve embedded layouts before any operation that could write to the checkout or config.
detect_platform() {
    os_type=""
    machine_arch=$(uname -m 2>/dev/null || true)
    os_release_name=$(awk -F= '
        $1 == "NAME" {
            value = substr($0, index($0, "=") + 1)
            sub(/^"/, "", value)
            sub(/"$/, "", value)
            print value
            exit
        }
    ' /etc/os-release 2>/dev/null || true)

    if [ "${machine_arch}" = "mips" ] && [ -d "/usr/data/creality" ]; then
        os_type="creality-k1"
        [ -n "${CONFIG_KLIPPER_HOME:-}" ]        || CONFIG_KLIPPER_HOME=/usr/share/klipper
        [ -n "${CONFIG_MOONRAKER_HOME:-}" ]      || CONFIG_MOONRAKER_HOME=/usr/data/moonraker/moonraker
        [ -n "${CONFIG_KLIPPER_CONFIG_HOME:-}" ] || CONFIG_KLIPPER_CONFIG_HOME=/usr/data/printer_data/config
    elif [ "${os_release_name}" = "FlyOS-Fast" ]; then
        os_type="flyos-fast"
        [ -n "${CONFIG_KLIPPER_HOME:-}" ]        || CONFIG_KLIPPER_HOME=/data/klipper
        [ -n "${CONFIG_MOONRAKER_HOME:-}" ]      || CONFIG_MOONRAKER_HOME=/data/moonraker
        [ -n "${CONFIG_KLIPPER_CONFIG_HOME:-}" ] || CONFIG_KLIPPER_CONFIG_HOME=/usr/share/printer_data/config
    elif [ "${machine_arch}" = "mips" ] && [ "$(id -u)" -eq 0 ] && [ -d "/root/printer_software" ]; then
        os_type="guppy-k1"
        [ -n "${CONFIG_KLIPPER_HOME:-}" ]        || CONFIG_KLIPPER_HOME=/root/printer_software/klipper
        [ -n "${CONFIG_MOONRAKER_HOME:-}" ]      || CONFIG_MOONRAKER_HOME=/root/printer_software/moonraker/moonraker
        [ -n "${CONFIG_KLIPPER_CONFIG_HOME:-}" ] || CONFIG_KLIPPER_CONFIG_HOME=/root/printer_data/config
    elif [ "${machine_arch}" = "mips" ] &&
         { [ -z "${CONFIG_KLIPPER_HOME:-}" ] ||
           [ -z "${CONFIG_KLIPPER_CONFIG_HOME:-}" ] ||
           [ -z "${CONFIG_MOONRAKER_HOME:-}" ]; }; then
        if [ "${F_OVERRIDE_CHECKS:-n}" = "y" ]; then
            echo "${C_WARNING}WARNING: Unknown MIPS layout; continuing without platform-specific path defaults.${C_OFF}" >&2
        else
            echo "${C_ERROR}ERROR: Unknown MIPS layout. Specify all required paths with -k, -c and -m, or use -o to continue without platform defaults.${C_OFF}" >&2
            if [ "$(id -u)" -ne 0 ]; then
                echo "${C_INFO}Logging in as root may also allow ${SCRIPT_NAME} to identify the platform.${C_OFF}" >&2
            fi
            exit 1
        fi
    fi

    export CONFIG_KLIPPER_HOME CONFIG_KLIPPER_CONFIG_HOME CONFIG_MOONRAKER_HOME
}

enforce_root_policy() {
    if [ -n "${SUDO_COMMAND:-}" ]; then
        echo "${C_ERROR}ERROR: Do not run ${SCRIPT_NAME} with sudo. Use the Klipper user, or log in as root on an embedded system.${C_OFF}" >&2
        exit 1
    fi

    case "${os_type}" in
    creality-k1 | flyos-fast | guppy-k1)
        if [ "$(id -u)" -ne 0 ]; then
            echo "${C_ERROR}ERROR: ${SCRIPT_NAME} must be run while logged in as root for ${os_type}.${C_OFF}" >&2
            exit 1
        fi
        ;;
    *)
        if [ "$(id -u)" -eq 0 ] && [ "${F_ALLOW_ROOT:-n}" != "y" ]; then
            echo "${C_ERROR}ERROR: Running ${SCRIPT_NAME} as root is not recommended. Run it as the user that installed Klipper.${C_OFF}" >&2
            echo "${C_INFO}Use -r to allow execution from a root login (not through sudo).${C_OFF}" >&2
            exit 1
        fi
        ;;
    esac
}


# Convert long options to short options
for arg in "$@"; do
    shift
    case "$arg" in
        --emu) set -- "$@" -e ;;
        --help) set -- "$@" -h ;;
        *)     set -- "$@" "$arg" ;;
    esac
done

while getopts "rehfiudzsb:nk:c:m:a:toqv" arg; do
    case $arg in
    f)
        FIX_LINKS=y
        F_SKIP_UPDATE=y
        ;;
    i) F_MENUCONFIG=y ;;
    u | d) F_UNINSTALL=y ;;
    z) export F_SKIP_UPDATE="${F_SKIP_UPDATE:=y}" ;;
    s) export F_NO_SERVICE=y ;;
    b) export BRANCH="${OPTARG}" ;;
    n) export F_MULTI_UNIT=y ;;
    k) export CONFIG_KLIPPER_HOME="${OPTARG}" ;;
    c) export CONFIG_KLIPPER_CONFIG_HOME="${OPTARG}" ;;
    m) export CONFIG_MOONRAKER_HOME="${OPTARG}" ;;
    # TODO: Repetier-Server stub support
    # p)
    #     PRINTER_CONFIG=${OPTARG}.cfg
    #     KLIPPER_SERVICE=klipper_${OPTARG}.service
    #     echo "Repetier-Server <stub> specified. Over-riding printer.cfg to [${PRINTER_CONFIG}] & klipper.service to [${KLIPPER_SERVICE}]"
    #     ;;
    a) export CONFIG_KLIPPER_SERVICE="${OPTARG}.service" ;;
    t) export TESTDIR=/tmp/mmu_test ;;
    r) export F_ALLOW_ROOT=y ;;
    o) export F_OVERRIDE_CHECKS=y ;;
    q) export Q= ;;   # Developer: Disable quiet mode in Makefile
    v) export V=-v ;; # Developer: Enable verbose mode in builder and debug in Makefile
    e) export F_PER_GATE_MCU=y ;; # Allows multiple MCU selection but menuconfig startup time is increased
    h) usage ;;
    *) usage ;;
    esac
done

# This must precede the v3 migration and self-update: both can write files, so a mistaken
# sudo/root invocation needs to fail before either one runs.
detect_platform
enforce_root_policy

# Settle v3 vs v4 before self-update or installation work (see offer_v3_v4_choice for why).
if [ "${F_SKIP_UPDATE}" != "force" ] && v3_detected; then
    offer_v3_v4_choice
fi

# Handle git self update or branch change
if [ "${F_SKIP_UPDATE}" = "force" ]; then
    : # If we just restarted with a forced skip, do nothing
elif [ ! "${F_SKIP_UPDATE}" ] && [ ! "${F_UNINSTALL}" ]; then
    [ -t 1 ] && clear
    # -b <branch> is what self_update.sh is about to act on below - pin Moonraker's
    # update manager to match, or it silently drifts back to the old primary_branch on
    # its next check. Skipped by -z, exactly as -b itself is (see usage: "-z ... nullifies
    # -b <branch>") since self_update.sh never runs to consult BRANCH in that case either.
    [ -n "${BRANCH}" ] && set_moonraker_primary_branch "${BRANCH}"
    "$SCRIPT_DIR/installer/self_update.sh" || exit 1
    F_SKIP_UPDATE=force exec "$0" "$@"
else
    [ -t 1 ] && clear
    echo "${C_NOTICE}Skipping (git) self update${C_OFF}"
fi

shift $((OPTIND - 1))
if [ "$1" ]; then
    KCONFIG_CONFIG="$1"
fi

export KCONFIG_CONFIG="${KCONFIG_CONFIG-.mmu_config}"
export PATH="${SCRIPT_DIR}:${PATH}"

case "${os_type}" in
creality-k1) echo "${C_INFO}Detected Creality K1 series printer${C_OFF}" ;;
flyos-fast)  echo "${C_INFO}Detected FlyOS-Fast${C_OFF}" ;;
guppy-k1)    echo "${C_INFO}Detected Guppy K1 Mod${C_OFF}" ;;
esac

# Summarise directory overrides
[ -n "${CONFIG_KLIPPER_HOME:-}" ]        && echo "${C_INFO}KLIPPER_HOME=${CONFIG_KLIPPER_HOME}${C_OFF}"
[ -n "${CONFIG_KLIPPER_CONFIG_HOME:-}" ] && echo "${C_INFO}KLIPPER_CONFIG_HOME=${CONFIG_KLIPPER_CONFIG_HOME}${C_OFF}"
[ -n "${CONFIG_MOONRAKER_HOME:-}" ]      && echo "${C_INFO}MOONRAKER_HOME=${CONFIG_MOONRAKER_HOME}${C_OFF}"



########################
##### Python setup #####
########################

INSTALLER_PY=""
checked_summary=""
klipper_parent=""
repo_venv=${VENV:-${SCRIPT_DIR}/venv}
[ -z "${CONFIG_KLIPPER_HOME:-}" ] || klipper_parent=$(dirname -- "${CONFIG_KLIPPER_HOME}")

# Prefer the environment adjacent to the selected Klipper checkout, then the conventional
# environments under HOME. Keep the executable explicit instead of sourcing activate: the
# same interpreter is passed to make and used by this script's direct Python calls.
for python_path in \
    "${klipper_parent:+${klipper_parent}/klipper-env/bin/python}" \
    "${klipper_parent:+${klipper_parent}/klippy-env/bin/python}" \
    "${HOME}/klipper-env/bin/python" \
    "${HOME}/klippy-env/bin/python" \
    "${repo_venv}/bin/python"; do
    [ -n "${python_path}" ] || continue

    if [ ! -x "${python_path}" ]; then
        checked_summary="${checked_summary}${python_path}: not found
"
        continue
    fi

    case "$("${python_path}" --version 2>&1)" in
        Python\ 3.*)
            INSTALLER_PY=${python_path}
            echo "${C_INFO}Using Python 3 from ${python_path%/bin/python}${C_OFF}"
            break
            ;;
        *)
            checked_summary="${checked_summary}${python_path}: Python 3 unavailable
"
            ;;
    esac
done

# Fall back to python, or python3 on systems which do not provide the python alias.
if [ -z "${INSTALLER_PY}" ]; then
    for python_cmd in python python3; do
        if ! command -v "${python_cmd}" >/dev/null 2>&1; then
            checked_summary="${checked_summary}System ${python_cmd}: not found
"
            continue
        fi

        case "$("${python_cmd}" --version 2>&1)" in
            Python\ 3.*)
                INSTALLER_PY=${python_cmd}
                echo "${C_WARNING}No suitable Klipper Python 3 venv found; continuing with ${python_cmd}${C_OFF}"
                break
                ;;
            *)
                checked_summary="${checked_summary}System ${python_cmd}: Python 3 unavailable
"
                ;;
        esac
    done
fi

if [ -z "${INSTALLER_PY}" ]; then
    echo "${C_ERROR}ERROR: No suitable Python 3 environment found.${C_OFF}"
    echo "${C_INFO}Checked:${C_OFF}"
    printf '%s\n' "${checked_summary}" | while IFS= read -r entry; do
        [ -n "${entry}" ] || continue
        echo "  - ${entry}"
    done
    exit 1
fi

run_make() {
    make --no-print-directory -C "${SCRIPT_DIR}" PY="${INSTALLER_PY}" "$@"
}

# Handle PEP 668 "externally managed" python environments that refuse pip installs without a venv, so installer's deps
# (installer/requirements.txt) can never be resolved. Ask the Makefile to create and populate
# the repo venv, then use that explicit interpreter for both shell and make commands.
if [ -z "${PIP_ARGS}" ] && \
   ! "${INSTALLER_PY}" -c 'import os, sys, sysconfig; sys.exit(0 if sys.prefix != sys.base_prefix or not os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 1)' 2>/dev/null; then
    export VENV="${VENV:-${SCRIPT_DIR}/venv}"
    echo "${C_INFO}System python is externally managed (PEP 668), using virtualenv '${VENV}'${C_OFF}"
    run_make installer_venv
    INSTALLER_PY="${VENV}/bin/python"
fi



if [ "${F_MENUCONFIG}" ] && [ "${F_UNINSTALL}" ]; then
    echo "${C_ERROR}Can't install and uninstall at the same time!${C_OFF}"
    usage
fi

if [ "${TESTDIR}" ]; then
    # Test mode redirects installation into a synthetic Klipper tree. Preserve the
    # operational Klipper checkout so menuconfig can still run its CAN discovery tool.
    if [ -n "${CONFIG_KLIPPER_HOME:-}" ]; then
        export REAL_KLIPPER_HOME="${CONFIG_KLIPPER_HOME}"
    elif [ -d "/usr/share/klipper" ]; then
        export REAL_KLIPPER_HOME="/usr/share/klipper"
    else
        export REAL_KLIPPER_HOME="${HOME}/klipper"
    fi
    export CONFIG_KLIPPER_HOME="${TESTDIR}/klipper"
    export CONFIG_KLIPPER_CONFIG_HOME="${TESTDIR}/printer_data/config"
    export CONFIG_MOONRAKER_HOME="${TESTDIR}/moonraker"
    export F_NO_SERVICE=y
    export KCONFIG_CONFIG="${TESTDIR}/.mmu_config"
    echo
    echo "${C_WARNING}Running in test mode to simulate without changing real configuration${C_OFF}"
    echo "${C_WARNING}Forcing flags '-s -c ${CONFIG_KLIPPER_CONFIG_HOME} -k ${CONFIG_KLIPPER_HOME} -m ${CONFIG_MOONRAKER_HOME} ${TESTDIR}/.mmu_config' ${C_OFF}"
    mkdir -p "${CONFIG_KLIPPER_HOME}/klippy/extras"
    mkdir -p "${CONFIG_KLIPPER_CONFIG_HOME}"
    mkdir -p "${CONFIG_MOONRAKER_HOME}/moonraker/components"
    touch "${CONFIG_KLIPPER_CONFIG_HOME}/moonraker.conf"
    touch "${CONFIG_KLIPPER_CONFIG_HOME}/printer.cfg"
    if [ ! "${F_UNINSTALL}" ]; then
        echo "${C_INFO}When complete look in ${TESTDIR} for results${C_OFF}"
        echo
        if prompt_yn "Continue"; then
            echo
            exit 0
        fi
        echo
    fi
fi

if [ -n "${CONFIG_KLIPPER_HOME+x}" ] && [ ! -d "${CONFIG_KLIPPER_HOME}" ]; then
    echo "${C_ERROR}Klipper config directory not found: ${CONFIG_KLIPPER_HOME}${C_OFF}"
    exit 1
fi


################################
##### Compatibility checks #####
################################

# Check Kalico is installed (klippy/__init__.py contains APP_NAME = "Kalico")
if [ -d "${CONFIG_KLIPPER_HOME}" ]; then
  kalico="${CONFIG_KLIPPER_HOME}/klippy/__init__.py"
else
  kalico="${HOME}/klipper/klippy/__init__.py"
fi

if [ -f ${kalico} ]; then
    if grep -q '^APP_NAME[[:space:]]*=[[:space:]]*"Kalico"' \
        "${kalico}" 2>/dev/null; then
        if [ "${F_OVERRIDE_CHECKS}" = "y" ]; then
            echo "${C_WARNING}WARNING: Kalico detected. Happy-Hare is not currently compatible with Kalico until Klipper motion-subsystem enhancements have been ported. Proceeding at your own risk.${C_OFF}" >&2
        else
            echo "${C_ERROR}ERROR: Kalico detected. Happy-Hare is not currently compatible with Kalico until Klipper motion-subsystem enhancements have been ported.${C_OFF}" >&2
            exit 1
        fi
    fi
fi

if [ -n "${CONFIG_KLIPPER_CONFIG_HOME+x}" ] && [ ! -d "${CONFIG_KLIPPER_CONFIG_HOME}" ]; then
    echo "${C_ERROR}Klipper config directory not found: ${CONFIG_KLIPPER_CONFIG_HOME}${C_OFF}"
    exit 1
fi

if [ -n "${CONFIG_MOONRAKER_HOME+x}" ] && [ ! -d "${CONFIG_MOONRAKER_HOME}" ]; then
    echo "${C_ERROR}Moonraker home directory not found: ${CONFIG_MOONRAKER_HOME}${C_OFF}"
    exit 1
fi



# Fix links only
# Restore klipper/moonraker symlinks
# (users delete them if they "hard" update klipper/moonraker)
if [ "${FIX_LINKS}" ]; then
    echo "${C_INFO}Restoring Happy Hare klipper extras and moonraker components links${C_OFF}"
    time_elapsed run_make fix_links
    exit 0
fi


#####################
##### Uninstall #####
#####################

if [ "${F_UNINSTALL}" ]; then
    echo "\n${C_WARNING}This will uninstall Happy Hare and cleanup prior config${C_OFF}"
    if prompt_yn "Are you sure you want to continue"; then
        echo
        exit 0
    fi
    echo
    time_elapsed run_make uninstall &&
        [ "${TESTDIR}" ] && rm -rf "${TESTDIR}"
    exit 0
fi



################################
##### Menuconfig / Refresh #####
################################

# Force F_PER_GATE_MCU if existing config already enables per-gate MCU support.
# This preserves the expanded menuconfig behavior on later runs without needing -e.
if [ -r "${KCONFIG_CONFIG}" ] && grep -q '^CONFIG_MMU_HAS_PER_GATE_MCU=y' "${KCONFIG_CONFIG}"; then
    export F_PER_GATE_MCU=y
fi

# Decide whether interactive configuration is required.
if [ ! -e "${KCONFIG_CONFIG}" ] && [ -z "${F_MENUCONFIG:-}" ]; then
    echo "${C_INFO}No '${KCONFIG_CONFIG}' found, forcing interactive menu${C_OFF}"
    echo
    F_MENUCONFIG=y
elif [ -r "${KCONFIG_CONFIG}" ] && [ -z "${F_MENUCONFIG:-}" ] && [ -n "${F_MULTI_UNIT:-}" ]; then
    #shellcheck source=.mmu_config
    . "${KCONFIG_CONFIG}"

    if [ -z "${CONFIG_MULTI_UNIT:-}" ]; then
        echo "${C_NOTICE}Current '${KCONFIG_CONFIG}' is not a multi-unit configuration, updating and forcing interactive menu${C_OFF}"
        echo
        F_MENUCONFIG=y
    fi
fi

# If re-running with -i give the choice of refreshing from Kconfig or retaining custom .cfg modifications
if [ -r "${KCONFIG_CONFIG}" ] && [ -n "${F_MENUCONFIG:-}" ]; then
    echo "${C_WARNING}You are running an interactive install with existing menuconfig ('${KCONFIG_CONFIG}').${C_OFF}"
    echo
    echo "${C_WARNING}Read carefully, you have three options:${C_OFF}"
    echo
    echo "${C_WARNING}1) Refresh (select 1)${C_OFF} (Default upgrade path)"
    echo "   This will NEVER CHANGE any manually edited .cfg parameter value and thus is limited"
    echo "   to only ADDING NEW or missing config sections/options. Note that parameter values shown"
    echo "   in menuconfig may be stale and not reflect your actual .cfg config"
    echo
    echo "${C_WARNING}2) Replace (select 2)${C_OFF} (Recommended)"
    echo "   This will OVERWRITE changes made directly to your .cfg files and create new default"
    echo "   configuration based on choices made in menuconfig (which initializes to your previous config)."
    echo "   This is RECOMMENDED if you make ALL your configuration changes via menuconfig. It is also"
    echo "   useful if you get into trouble and want to reset your starting position or change MMU."
    echo
    echo "${C_WARNING}3) Merge (select 3)${C_OFF}"
    echo "   This will MERGE simple parameter values set in menuconfig but will retain other changes made"
    echo "   directly to your .cfg files. This is useful if you manage most parameters via menuconfig but"
    echo "   don't want, for example, to accidentally overwrite your carefully tweaked hardware configuration"
    echo
    echo "(Note that in all cases a BACKUP of your existing .cfg files will be made for reference)"
    echo

    sel=$(prompt_n 3 "Choose upgrade mode")
    case "$sel" in
        2) export F_CFG_UPGRADE_MODE=replace ;;
        3) export F_CFG_UPGRADE_MODE=merge ;;
        *) export F_CFG_UPGRADE_MODE=refresh ;;
    esac

    echo "${C_INFO}Launching menuconfig (${F_CFG_UPGRADE_MODE} mode)...${C_OFF}"
    if [ -n "${F_PER_GATE_MCU:-}" ]; then
        echo "${C_INFO}Per-gate MCU support enabled. Menuconfig startup will be slower.${C_OFF}"
    fi
    echo
fi


# Helpers to run a Kconfig action (menuconfig or olddefconfig) across all relevant
# relevant configuration files using the correct installation context.
#
# The traversal logic for top-level and per-unit configurations is centralized
# here because multi-unit setups require additional context (UNIT_NAME,
# UNIT_INDEX, MCU_NAME, sensor configuration, etc.) that Make alone does not
# have. This ensures menuconfig and olddefconfig always operate on the same set
# of configs with identical context.
#
# For olddefconfig, the Makefile's kconfig_needs_update target is used to detect
# whether a config is stale by comparing its timestamp against the Kconfig
# source files. Stale configs are automatically refreshed so that newly added
# Kconfig options receive their default values.
#
# This is particularly important when running "./install.sh -i". If the user
# enters menuconfig but exits without saving, the config timestamp remains
# unchanged. A subsequent olddefconfig pass can therefore still detect that the
# config is older than the Kconfig sources and update it with any new defaults.
#
# Expected behavior:
#   - Interactive install (-i):
#       * Run menuconfig for all applicable configs.
#       * Run olddefconfig only on configs that are stale.
#   - Non-interactive install/upgrade:
#       * Skip menuconfig.
#       * Run olddefconfig only on configs that are stale.
#
# This guarantees that new Kconfig defaults are always propagated to existing
# installations while avoiding duplicate traversal logic and preserving the
# correct context for multi-unit configurations.
#
run_kconfig_top() {
    action=$1
    only_if_stale=${2:-n}

    unset CONFIG_MULTI_UNIT CONFIG_MMU_UNITS
    [ -r "${KCONFIG_CONFIG}" ] && . "${KCONFIG_CONFIG}"

    if [ -n "${F_MULTI_UNIT:-}" ] || [ -n "${CONFIG_MULTI_UNIT:-}" ]; then
        run_kconfig_one "${KCONFIG_CONFIG}" "${action}" "${only_if_stale}" \
            F_MULTI_UNIT_ENTRY_POINT=y \
            F_MULTI_UNIT=y
    else
        run_kconfig_one "${KCONFIG_CONFIG}" "${action}" "${only_if_stale}"
    fi
}

run_kconfig_units() {
    action=$1
    only_if_stale=${2:-n}

    [ -r "${KCONFIG_CONFIG}" ] || return 0

    unset CONFIG_MULTI_UNIT CONFIG_MMU_UNITS
    unset CONFIG_MMU_HAS_SENSOR_TOOLHEAD CONFIG_MMU_HAS_SENSOR_EXTRUDER CONFIG_MMU_HAS_TOOLHEAD_CUTTER
    . "${KCONFIG_CONFIG}"

    if [ -n "${CONFIG_MULTI_UNIT:-}" ]; then
        i=0
        IFS=,
        set -f
        for name in ${CONFIG_MMU_UNITS:-}; do
            name=$(trim "$name")
            [ -n "$name" ] || continue

            run_kconfig_one "${KCONFIG_CONFIG}_${name}" "${action}" "${only_if_stale}" \
                F_MULTI_UNIT=y \
                UNIT_INDEX="$i" \
                UNIT_NAME="$name" \
                MCU_NAME="$name" \
                HAS_SENSOR_TOOLHEAD="$CONFIG_MMU_HAS_SENSOR_TOOLHEAD" \
                HAS_SENSOR_EXTRUDER="$CONFIG_MMU_HAS_SENSOR_EXTRUDER" \
                HAS_SENSOR_TOOLHEAD_CUTTER="$CONFIG_MMU_HAS_TOOLHEAD_CUTTER"

            i=$((i + 1))
        done
        set +f
    fi
}

run_kconfig_one() {
    cfg=$1
    action=$2
    only_if_stale=$3
    shift 3

    if [ "${only_if_stale}" = "y" ]; then
        needs=$(run_make \
            KCONFIG_CONFIG="${cfg}" \
            "$@" \
            kconfig_needs_update)

        [ "${needs}" = "y" ] || return 0
        echo "${C_INFO}Updating Kconfig defaults in '${cfg}'${C_OFF}"
    fi

    run_make \
        KCONFIG_CONFIG="${cfg}" \
        "$@" \
        "${action}"
}



################################
##### Menuconfig / Refresh #####
################################

if [ -n "${F_MENUCONFIG:-}" ]; then
    tmpconfig=

    [ -r "${KCONFIG_CONFIG}" ] && . "${KCONFIG_CONFIG}"

    if [ -r "${KCONFIG_CONFIG}" ] &&
       [ -n "${F_MULTI_UNIT:-}" ] &&
       [ -z "${CONFIG_MULTI_UNIT:-}" ]; then
        tmpconfig="$(mktemp -t tmpconfig.XXXXXX)"
        cp -- "${KCONFIG_CONFIG}" "${tmpconfig}"
    fi

    run_kconfig_top menuconfig n

    if [ ! -e "${KCONFIG_CONFIG}" ]; then
        echo "${C_ERROR}Config '${KCONFIG_CONFIG}' has not been saved, exiting.${C_OFF}"
        exit 1
    fi

    if [ -n "${tmpconfig:-}" ]; then
        unset CONFIG_MULTI_UNIT CONFIG_MMU_UNITS
        . "${KCONFIG_CONFIG}"

        first_unit=$(trim "${CONFIG_MMU_UNITS%%,*}")
        if [ -n "${first_unit}" ]; then
            mv "${tmpconfig}" "${KCONFIG_CONFIG}_${first_unit}"
        else
            rm -f "${tmpconfig}"
        fi
    fi

    run_kconfig_units menuconfig n
fi

# Always refresh stale configs after any optional menuconfig pass.
run_kconfig_top olddefconfig y
run_kconfig_units olddefconfig y

# Give the v3 -> v4 upgrade a clean start now that Kconfig has resolved real paths -
# see v3_upgrade_cleanup for why this can't happen any earlier.
if [ -n "${F_V3_UPGRADE:-}" ]; then
    v3_upgrade_cleanup
fi



###########################
##### Install/Upgrade #####
###########################

install_and_clean() {
    run_make install
    if [ -z "${TESTDIR}" ]; then
        run_make clean
    fi
}

time_elapsed install_and_clean
