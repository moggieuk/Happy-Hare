#!/usr/bin/env sh
#
# Happy Hare MMU Software
#
# Installer / Updater launch script with familar options
#
# Carefully written to only use options that are widely available
# Please report any incompatability via github issue
#

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

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
    echo "${C_INFO}[config_file]${C_OFF} is optional, if not specified the default config filename (.config) will be used."
    echo "  -i for interactive install"
    echo "  -u or -d for uninstall"
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

while getopts "iudzsb:nk:c:m:a:tqv" arg; do
    case $arg in
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
    q) export Q= ;;   # Developer: Disable quite mode in Makefile
    v) export V=-v ;; # Developer: Enable verbose mode in builder and debug in Makefile
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
    echo "${C_NOTICE}Skipping self update${C_OFF}"
fi

shift $((OPTIND - 1))
if [ "$1" ]; then
    KCONFIG_CONFIG="$1"
fi

export KCONFIG_CONFIG="${KCONFIG_CONFIG-.config}"
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
    export KCONFIG_CONFIG="${TESTDIR}/.config"
    echo
    echo "${C_WARNING}Running in test mode to simulate without changing real configuration${C_OFF}"
    echo "${C_WARNING}Forcing flags '-s -c ${CONFIG_KLIPPER_CONFIG_HOME} -k ${CONFIG_KLIPPER_HOME} -c ${CONFIG_MOONRAKER_HOME} ${TESTDIR}/.config' ${C_OFF}"
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

######################
##### Menuconfig #####
######################

# Ensures there’s a valid top-level config, confirms whether it’s single- or multi-unit,
# and—if multi-unit—runs menuconfig once per listed unit to create/update each unit’s own
# config file, passing UNIT_* parameters to the Makefile/Kconfig for customization.

if [ ! -e "${KCONFIG_CONFIG}" ] && [ -z "${F_MENUCONFIG:-}" ]; then
    echo "${C_INFO}No '${KCONFIG_CONFIG}' found, forcing interactive menu${C_OFF}"
    echo
    F_MENUCONFIG=y
elif [ -r "${KCONFIG_CONFIG}" ] && [ -z "${F_MENUCONFIG:-}" ] && [ -n "${F_MULTI_UNIT:-}" ]; then
    echo "${C_NOTICE}Current '${KCONFIG_CONFIG}' is not a multi-unit configuration, updating and forcing interactive menu${C_OFF}"
    echo
    F_MENUCONFIG=y
fi

# If re-running with -i give the choice of refreshing from Kconfig or retaining custom .cfg modifications
if [ -r "${KCONFIG_CONFIG}" ] && [ -n "${F_MENUCONFIG:-}" ]; then
    echo "${C_WARNING}You are running an interactive install with existing menuconfig ('${KCONFIG_CONFIG}').${C_OFF}"
    echo "${C_WARNING}Read carefully, you have two options:${C_OFF}"
    echo
    echo "- Refresh/restore .cfg from menuconfig ${C_WARNING}(select Y)${C_OFF}"
    echo "  This will OVERWRITE changes you have made directly to your Happy Hare .cfg files that are ALSO set by"
    echo "  menuconfig but is the best choice if you make core changes via this interactive installer (recommended)"
    echo "- Retain ALL your .cfg changes ${C_WARNING}(select N)${C_OFF}"
    echo "  This will never change any existing parameter value and thus is limited to only ADDING NEW or missing"
    echo "  config sections/options. Parameter values in menuconfig may not reflect your actual .cfg config"
    echo
    echo "  (Note that in both cases a backup of your existing .cfg files will be made)"
    echo
    if ! prompt_yn "Refresh/restore .cfg"; then
        export F_SKIP_RETAIN_OLD_CFG=y
    fi
    echo
fi

if [ -n "${F_MENUCONFIG:-}" ]; then
    #shellcheck source=.config
    [ -r "${KCONFIG_CONFIG}" ] && . "${KCONFIG_CONFIG}"

    if [ -n "${F_MULTI_UNIT:-}" ] || [ -n "${CONFIG_MULTI_UNIT:-}" ]; then
        if [ -r "${KCONFIG_CONFIG}" ] && [ -z "${CONFIG_MULTI_UNIT:-}" ] && [ -n "${F_MULTI_UNIT:-}" ]; then
            tmpconfig="$(mktemp -t tmpconfig.XXXXXX)"
            cp -- "${KCONFIG_CONFIG}" "${tmpconfig}"
        fi
        make --no-print-directory -C "${SCRIPT_DIR}" F_MULTI_UNIT_ENTRY_POINT=y F_MULTI_UNIT=y menuconfig
    else
        make --no-print-directory -C "${SCRIPT_DIR}" menuconfig
    fi

    if [ ! -e "${KCONFIG_CONFIG}" ]; then
        echo "${C_ERROR}Config '${KCONFIG_CONFIG}' has not been saved, exiting.${C_OFF}"
        exit 1
    fi

    #shellcheck source=.config
    . "${KCONFIG_CONFIG}"

    # Now we are sure of having multi-unit names, move the original combined config
    # to first unit config before running menuconfig on it
    if [ -n "${tmpconfig:-}" ]; then
        first_unit=$(trim "${CONFIG_PARAM_MMU_UNITS%%,*}")
        [ -n "${first_unit}" ] && mv "${tmpconfig}" "${KCONFIG_CONFIG}_${first_unit}"
    fi

    if [ -n "${CONFIG_MULTI_UNIT:-}" ]; then
        i=0
        OLDIFS=${IFS-}
        IFS=,
        set -f # Avoid globbing
        for name in ${CONFIG_PARAM_MMU_UNITS:-}; do
            name=$(trim "$name")
            [ -n "$name" ] || continue
            make --no-print-directory -C "${SCRIPT_DIR}" KCONFIG_CONFIG="${KCONFIG_CONFIG}_${name}" F_MULTI_UNIT=y UNIT_INDEX="${i}" UNIT_NAME="${name}" MMU_MCU="${name}" menuconfig
            i=$((i + 1))
        done
        set +f
        IFS=${OLDIFS}
    fi
fi

#############################
##### Install / Upgrade #####
#############################

time_elapsed make --no-print-directory -C "${SCRIPT_DIR}" install
