#!/usr/bin/env sh

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
    echo "                    [-a <kiauh_alternate_klipper>]" # [-r <repetier_server stub>]"
    echo
    echo "-i for interactive install"
    # echo "-e for install of default starter config files for manual configuration"
    echo "-d for uninstall"
    echo "-z skip github update check (nullifies -b <branch>)"
    echo "-s to skip restart of services"
    echo "-b to switch to specified feature branch (sticky)"
    echo "-k <dir> to specify location of non-default klipper home directory"
    echo "-c <dir> to specify location of non-default klipper config directory"
    echo "-m <dir> to specify location of non-default moonraker home directory"
    # TODO: Repetier-Server stub support
    # echo "-r specify Repetier-Server <stub> to override printer.cfg and klipper.service names"
    echo "-a <name> to specify alternative klipper-service-name when installed with Kiauh"
    echo "(no flags for safe re-install / upgrade)"
    exit 0
}

F_INSTALL=0
F_UNINSTALL=0

while getopts "a:b:k:c:m:r:idsze" arg; do
    case $arg in
    a) export CONFIG_KLIPPER_SERVICE="${OPTARG}.service" ;;
    b) export BRANCH="${OPTARG}" ;;
    k) export CONFIG_KLIPPER_HOME="${OPTARG}" ;;
    m) export CONFIG_MOONRAKER_HOME="${OPTARG}" ;;
    c) export CONFIG_KLIPPER_CONFIG_HOME="${OPTARG}" ;;
    # TODO: Repetier-Server stub support
    # r)
    #     PRINTER_CONFIG=${OPTARG}.cfg
    #     KLIPPER_SERVICE=klipper_${OPTARG}.service
    #     echo "Repetier-Server <stub> specified. Over-riding printer.cfg to [${PRINTER_CONFIG}] & klipper.service to [${KLIPPER_SERVICE}]"
    #     ;;
    i) F_INSTALL=1 ;;
    d) F_UNINSTALL=1 ;;
    s) export F_NOSERVICE=1 ;;
    z) export F_SKIP_UPDATE=1 ;;
    *) usage ;;
    esac
done

if [ "${F_INSTALL}" -eq 1 ] && [ "${F_UNINSTALL}" -eq 1 ]; then
    echo "${C_ERROR}Can't install and uninstall at the same time!${C_OFF}"
    usage
fi

if [ "${F_INSTALL}" -eq 1 ]; then
    make install
fi

if [ "${F_UNINSTALL}" -eq 1 ]; then
    make uninstall
fi
