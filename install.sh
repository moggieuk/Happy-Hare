#!/usr/bin/env sh
#
# Happy Hare MMU Software
#
# Installer / Updater launch script with familar options
#
# Carefully written to only use options that are widely available
# Please report any incompatability via github issue
#

# Exit immediately on error (really important to catch menuconfig errors / non-saves / aborts)
set -e

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. ~/klippy-env/bin/activate

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
    echo "${USAGE} [-i] [-u] [-d] [-z] [-s] [-t]"
    echo "${SPACE} [-b <branch>]"
    echo "${SPACE} [-k <klipper_home_dir>] [-c <klipper_config_dir>] [-m <moonraker_home_dir>]"
    echo "${SPACE} [-a <kiauh_alternate_klipper>] [config_file]" # [-r <repetier_server stub>]"
    echo "${C_OFF}"
    echo "${C_INFO}(no flags for safe re-install / upgrade)${C_OFF}"
    echo "${C_INFO}[config_file]${C_OFF} is optional, if not specified the default config filename (.mmu_config) will be used."
    echo "  -i for interactive install"
    echo "  -u or -d for uninstall"
    echo "  -f to just restore klipper/moonraker symlinks (recover after hard klipper update)"
    echo "  -z skip github update check (nullifies -b <branch>)"
    echo "  -s to skip restart of services"
    echo "  -b to switch to specified feature branch (sticky)"
    echo "  -n to specify a multiple MMU unit setup"
    echo "  -k <dir> to specify location of non-default klipper home directory"
    echo "  -c <dir> to specify location of non-default klipper config directory"
    echo "  -m <dir> to specify location of non-default moonraker home directory"
    # TODO: Repetier-Server stub support
    # echo "-r specify Repetier-Server <stub> to override printer.cfg and klipper.service names"
    echo "  -a <name> to specify alternative klipper-service-name when installed with Kiauh"
    echo "  -t activate test mode to create test config files in /tmp"
    echo "  (-q verbose make --no-print-directory)"
    echo "  (-v verbose builder)"
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
        read -r yn
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
    START_TIME=$(python -c "import time; print(time.time())")
    "${@}"
    END_TIME=$(python -c "import time; print(time.time())")
    echo "${START_TIME} ${END_TIME}" | awk '{printf "Elapsed: %.1f seconds", $2 - $1}'
    echo
}

while getopts "hfiudzsb:nk:c:m:a:tqv" arg; do
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
    # r)
    #     PRINTER_CONFIG=${OPTARG}.cfg
    #     KLIPPER_SERVICE=klipper_${OPTARG}.service
    #     echo "Repetier-Server <stub> specified. Over-riding printer.cfg to [${PRINTER_CONFIG}] & klipper.service to [${KLIPPER_SERVICE}]"
    #     ;;
    a) export CONFIG_KLIPPER_SERVICE="${OPTARG}.service" ;;
    t) export TESTDIR=/tmp/mmu_test ;;
    q) export Q= ;;   # Developer: Disable quiet mode in Makefile
    v) export V=-v ;; # Developer: Enable verbose mode in builder and debug in Makefile
    h) usage ;;
    *) usage ;;
    esac
done

# Handle git self update or branch change
if [ "${F_SKIP_UPDATE}" = "force" ]; then
    : # If we just restarted with a forced skip, do nothing
elif [ ! "${F_SKIP_UPDATE}" ] && [ ! "${F_UNINSTALL}" ]; then
    [ -t 1 ] && clear
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

if [ "${F_MENUCONFIG}" ] && [ "${F_UNINSTALL}" ]; then
    echo "${C_ERROR}Can't install and uninstall at the same time!${C_OFF}"
    usage
fi

if [ "${TESTDIR}" ]; then
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

if [ -n "${CONFIG_KLIPPER_CONFIG_HOME+x}" ] && [ ! -d "${CONFIG_KLIPPER_CONFIG_HOME}" ]; then
    echo "${C_ERROR}Klipper config directory not found: ${CONFIG_KLIPPER_CONFIG_HOME}${C_OFF}"
    exit 1
fi

if [ -n "${CONFIG_MOONRAKER_HOME+x}" ] && [ ! -d "${CONFIG_MOONRAKER_HOME}" ]; then
    echo "${C_ERROR}Moonraker home directory not found: ${CONFIG_MOONRAKER_HOME}${C_OFF}"
    exit 1
fi

# Handle the quick fix of klipper/moonraker symlinks
# (users delete them if they "hard" update klipper/moonraker)
if [ "${FIX_LINKS}" ]; then
    echo "${C_INFO}Restoring Happy Hare klipper extras and moonraker components links${C_OFF}"
    time_elapsed make --no-print-directory -C "${SCRIPT_DIR}" fix_links
    exit 0
fi



#####################
##### Uninstall #####
#####################

if [ "${F_UNINSTALL}" ]; then
    echo "${C_WARNING}This will uninstall and cleanup prior config${C_OFF}"
    echo
    if prompt_yn "Are you sure"; then
        echo
        exit 0
    fi
    echo
    time_elapsed make --no-print-directory -C "${SCRIPT_DIR}" uninstall &&
        [ "${TESTDIR}" ] && rm -rf "${TESTDIR}"
    exit 0
fi



################################
##### Menuconfig / Refresh #####
################################

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
    echo "${C_WARNING}2) Replace (select 2)${C_OFF}"
    echo "   This will OVERWRITE changes made directly to your .cfg files and create new default"
    echo "   configuration based on choices made in menuconfig. It is useful if you get into"
    echo "   trouble and want to reset your starting position. It is also great if you make ALL"
    echo "   your configuration changes via menuconfig"
    echo
    echo "${C_WARNING}3) Merge (select 3)${C_OFF}"
    echo "   This will MERGE simple parameter values set in menuconfig but will retain other changes"
    echo "   made directly in your .cfg files. This is useful if you manage most parameters via menuconfig"
    echo "   but don't want to, for example, overwrite your carefully tweaked hardware configuration"
    echo
    echo "(Note that in all cases a BACKUP of your existing .cfg files will be made for reference)"
    echo

    sel=$(prompt_n 3 "Choose upgrade mode")
    case "$sel" in
        2) export F_CFG_UPGRADE_MODE=replace ;;
        3) export F_CFG_UPGRADE_MODE=merge ;;
        *) export F_CFG_UPGRADE_MODE=refresh ;;
    esac

    echo "${C_INFO}Launching menuconfig (${F_CFG_UPGRADE_MODE})...${C_OFF}"
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
    unset CONFIG_MMU_HAS_SENSOR_TOOLHEAD CONFIG_MMU_HAS_SENSOR_EXTRUDER
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
                HAS_SENSOR_EXTRUDER="$CONFIG_MMU_HAS_SENSOR_EXTRUDER"

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
        needs=$(make --no-print-directory -C "${SCRIPT_DIR}" \
            KCONFIG_CONFIG="${cfg}" \
            "$@" \
            kconfig_needs_update)

        [ "${needs}" = "y" ] || return 0
        echo "${C_INFO}Updating Kconfig defaults in '${cfg}'${C_OFF}"
    fi

    make --no-print-directory -C "${SCRIPT_DIR}" \
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
#    if [ -n "${F_MULTI_UNIT:-}" ] && [ -z "${CONFIG_MULTI_UNIT:-}" ]; then
#        tmpconfig="$(mktemp -t tmpconfig.XXXXXX)"
#        cp -- "${KCONFIG_CONFIG}" "${tmpconfig}"
#    fi

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



###########################
##### Install/Upgrade #####
###########################

time_elapsed sh -ec '
    make --no-print-directory -C "'"${SCRIPT_DIR}"'" install

    if [ -z "'"${TESTDIR}"'" ]; then
        make --no-print-directory -C "'"${SCRIPT_DIR}"'" clean
    fi
'
