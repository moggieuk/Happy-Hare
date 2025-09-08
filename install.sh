#!/usr/bin/env bash
#
# Happy Hare MMU Software
#
# Installer / Updater launch script with familar options
#

clear

if [ -n "$(which tput 2>/dev/null)" ]; then
    C_OFF=$(tput -Txterm-256color sgr0)
    C_DEBUG=$(tput -Txterm-256color setaf 5)
    C_INFO=$(tput -Txterm-256color setaf 6)
    C_NOTICE=$(tput -Txterm-256color setaf 2)
    C_WARNING=$(tput -Txterm-256color setaf 3)
    C_ERROR=$(tput -Txterm-256color setaf 1)
fi

usage() {
    USAGE="Usage: $0"
    SPACE=$(echo ${USAGE} | tr "[:print:]" " ")
    echo ${C_INFO}
    echo "${USAGE} [-i] [-u] [-d] [-z] [-s] [-t]"
    echo "${SPACE} [-b <branch>]"
    echo "${SPACE} [-k <klipper_home_dir>] [-c <klipper_config_dir>] [-m <moonraker_home_dir>]"
    echo "${SPACE} [-a <kiauh_alternate_klipper>] [config_file]" # [-r <repetier_server stub>]"
    echo ${C_OFF}
    echo "${C_INFO}(no flags for safe re-install / upgrade)${C_OFF}"
    echo "-i for interactive install"
    echo "-u or -d for uninstall"
    echo "-z skip github update check (nullifies -b <branch>)"
    echo "-s to skip restart of services"
    echo "-b to switch to specified feature branch (sticky)"
    echo "-n to specify a multiple MMU unit setup"
    echo "-k <dir> to specify location of non-default klipper home directory"
    echo "-c <dir> to specify location of non-default klipper config directory"
    echo "-m <dir> to specify location of non-default moonraker home directory"
    # TODO: Repetier-Server stub support
    # echo "-r specify Repetier-Server <stub> to override printer.cfg and klipper.service names"
    echo "-a <name> to specify alternative klipper-service-name when installed with Kiauh"
    echo "-t activate test mode to create test config files in /tmp"
    echo "${C_INFO}[config_file]${C_OFF} is optional, if not specified the default config filename (.config) will be used."
    echo "(-q verbose make; -v verbose builder)"
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
        read -n1 -p "$@ (y/n)? " yn
        case "${yn}" in 
            Y|y) echo -n "y"; break ;;
            N|n) echo -n "n"; break ;;
        esac        
    done
}  

while getopts "iudzsb:nk:c:m:a:tqv" arg; do
    case $arg in
        i) F_MENUCONFIG=y ;;
        u|d) F_UNINSTALL=y ;;
        z) export F_SKIP_UPDATE=y ;;
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
        t) export TEST_MODE=y ;;
        q) export Q= ;;   # Developer: Disable quite mode in Makefile
        v) export V=-v ;; # Developer: Enable verbose mode in builder
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

if [ "${TEST_MODE}" ]; then
    export TEST_DIR="/tmp/mmu_test"
    export CONFIG_KLIPPER_HOME="${TEST_DIR}/klipper"
    export CONFIG_KLIPPER_CONFIG_HOME="${TEST_DIR}/printer_data/config"
    export CONFIG_MOONRAKER_HOME="${TEST_DIR}/moonraker"
    export KCONFIG_CONFIG=${TEST_DIR}/test_config
    export F_NO_SERVICE=y
    echo -e "\n${C_WARNING}Running in test mode to simulate update without changing real configuration${C_OFF}"
    echo -e "${C_WARNING}Forcing flags '-s -c ${CONFIG_KLIPPER_CONFIG_HOME} -k ${CONFIG_KLIPPER_HOME} -c ${CONFIG_MOONRAKER_HOME} ${TEST_DIR}/.config' ${C_OFF}"
    mkdir -p "${CONFIG_KLIPPER_HOME}/klippy/extras"
    mkdir -p "${CONFIG_KLIPPER_CONFIG_HOME}"
    mkdir -p "${CONFIG_MOONRAKER_HOME}/moonraker/components"
    touch "${CONFIG_KLIPPER_CONFIG_HOME}/moonraker.conf"
    touch "${CONFIG_KLIPPER_CONFIG_HOME}/printer.cfg"
    if [ ! "${F_UNINSTALL}" ]; then
        echo -e "${C_INFO}When complete look in ${TEST_DIR} for results${C_OFF}\n"
        [ $(prompt_yn "Continue") != "y" ] && { echo; exit 0; } || echo
    fi
fi

if [ "${F_UNINSTALL}" ]; then
    echo -e "${C_WARNING}This will uninstall and cleanup prior config${C_OFF}\n"
    [ $(prompt_yn "Are you sure") != "y" ] && { echo; exit 0; } || echo
    make uninstall
    [ "${TEST_MODE}" ] && rm -rf "${TEST_DIR}"
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
            make KCONFIG_CONFIG="${KCONFIG_CONFIG}.${name}" UNIT_INDEX=$i UNIT_NAME="${name}" MMU_MCU="${name}" menuconfig
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

echo "PAUL calling make install..."
make install
