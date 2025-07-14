#!/usr/bin/env bash

if [ -n "$(which tput 2>/dev/null)" ]; then
    C_OFF=$(tput -Txterm-256color sgr0)
    C_DEBUG=$(tput -Txterm-256color setaf 5)
    C_INFO=$(tput -Txterm-256color setaf 6)
    C_NOTICE=$(tput -Txterm-256color setaf 2)
    C_WARNING=$(tput -Txterm-256color setaf 3)
    C_ERROR=$(tput -Txterm-256color setaf 1)
fi

usage() {
    echo "Usage: $0 [-i] [-e] [-d] [-z] [-s]"
    echo "                    [-b <branch>] [-k <klipper_home_dir>] [-c <klipper_config_dir>] [-m <moonraker_home_dir>]"
    echo "                    [-a <kiauh_alternate_klipper>] [config_file]" # [-r <repetier_server stub>]"
    echo
    echo "-i for interactive install"
    # echo "-e for install of default starter config files for manual configuration"
    echo "-d for uninstall"
    echo "-z skip github update check (nullifies -b <branch>)"
    echo "-s to skip restart of services"
    echo "-b to switch to specified feature branch (sticky)"
    echo "-n to specify a multiple MMU units setup"
    echo "-k <dir> to specify location of non-default klipper home directory"
    echo "-c <dir> to specify location of non-default klipper config directory"
    echo "-m <dir> to specify location of non-default moonraker home directory"
    # TODO: Repetier-Server stub support
    # echo "-r specify Repetier-Server <stub> to override printer.cfg and klipper.service names"
    echo "-a <name> to specify alternative klipper-service-name when installed with Kiauh"
    echo "[config_file] is optional, if not specified the default config filename (.config) will be used."
    echo "(no flags for safe re-install / upgrade)"
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

while getopts "a:b:k:c:m:nidszev" arg; do
    case $arg in
    a) export CONFIG_KLIPPER_SERVICE="${OPTARG}.service" ;;
    b) export BRANCH="${OPTARG}" ;;
    k) export CONFIG_KLIPPER_HOME="${OPTARG}" ;;
    m) export CONFIG_MOONRAKER_HOME="${OPTARG}" ;;
    c) export CONFIG_KLIPPER_CONFIG_HOME="${OPTARG}" ;;
    n) export F_MULTI_UNIT=y ;;
    # TODO: Repetier-Server stub support
    # r)
    #     PRINTER_CONFIG=${OPTARG}.cfg
    #     KLIPPER_SERVICE=klipper_${OPTARG}.service
    #     echo "Repetier-Server <stub> specified. Over-riding printer.cfg to [${PRINTER_CONFIG}] & klipper.service to [${KLIPPER_SERVICE}]"
    #     ;;
    i) F_MENUCONFIG=y ;;
    d) F_UNINSTALL=y ;;
    s) export F_NO_SERVICE=y ;;
    z) export F_SKIP_UPDATE=y ;;
    v) export Q= ;;
    *) usage ;;
    esac
done

shift $((OPTIND - 1))
if [ "$1" ]; then
    KCONFIG_CONFIG="$1"
fi

export KCONFIG_CONFIG="${KCONFIG_CONFIG-.config}"

if [ "${F_MENUCONFIG}" ] && [ "${F_UNINSTALL}" ]; then
    echo "${C_ERROR}Can't install and uninstall at the same time!${C_OFF}"
    usage
fi

if [ "${F_UNINSTALL}" ]; then
    make uninstall
    exit 0
fi

if [ "${F_MULTI_UNIT}" ]; then
    if [ "${F_MENUCONFIG}" ] || [ ! -e "${KCONFIG_CONFIG}" ]; then
        make F_MULTI_UNIT_ENTRY_POINT=y menuconfig
    fi

    if [ ! -e "${KCONFIG_CONFIG}" ]; then
        echo "${C_ERROR}Config '${KCONFIG_CONFIG}' has not been saved, exiting.${C_OFF}"
        exit 1
    fi

    # shellcheck source=./.config
    . "$(realpath "${KCONFIG_CONFIG}")"

    if [ ! "${CONFIG_MULTI_UNIT}" ]; then
        echo "${C_NOTICE}Current '${KCONFIG_CONFIG}' is not a multi-unit configuration, forcing interactive menu"
        make F_MULTI_UNIT_ENTRY_POINT=y menuconfig
        F_MENUCONFIG=y
        # shellcheck source=./.config
        . "$(realpath "${KCONFIG_CONFIG}")"
    fi

    i=0
    IFS=,
    for name in ${CONFIG_PARAM_MMU_UNITS}; do
        name=${name#"${name%%[![:space:]]*}"} # remove leading spaces
        name=${name%"${name##*[![:space:]]}"} # remove trailing spaces
        if [ "${F_MENUCONFIG}" ] || [ ! -e "${KCONFIG_CONFIG}.${name}" ]; then
            make KCONFIG_CONFIG="${KCONFIG_CONFIG}.${name}" UNIT_INDEX=$i UNIT_NAME="${name}" menuconfig
            i=$((i + 1))
        fi
    done
else
    # If a `.config` already exists check if it's a single-unit setup else force menuconfig
    if [ -e "${KCONFIG_CONFIG}" ]; then
        # shellcheck source=./.config
        . "$(realpath "${KCONFIG_CONFIG}")"
        if [ "${CONFIG_MULTI_UNIT}" ]; then
            echo "${C_NOTICE}Current '${KCONFIG_CONFIG}' is a multi-unit configuration, forcing interactive menu${C_OFF}"
            F_MENUCONFIG=y
        fi
    fi

    if [ "${F_MENUCONFIG}" ]; then
        make menuconfig
    fi
fi

make install
