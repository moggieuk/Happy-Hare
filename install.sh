#!/bin/bash
# Happy Hare MMU Software
# Installer
#
# Copyright (C) 2022  moggieuk#6538 (discord) moggieuk@hotmail.com
#
KLIPPER_HOME="${HOME}/klipper"
KLIPPER_CONFIG_HOME="${HOME}/printer_data/config"
KLIPPER_LOGS_HOME="${HOME}/printer_data/logs"
OLD_KLIPPER_CONFIG_HOME="${HOME}/klipper_config"

declare -A PIN 2>/dev/null || {
    echo "Please run this script with ./bash $0"
    exit 1
}

# Pins for Fysetc Burrows ERB board, EASY-BRD and EASY-BRD with Seed Studio XIAO RP2040
PIN[ERB,gear_uart_pin]="mmu:gpio20";         PIN[EASY-BRD,gear_uart_pin]="mmu:PA8";          PIN[EASY-BRD-RP2040,gear_uart_pin]="mmu:gpio6"
PIN[ERB,gear_step_pin]="mmu:gpio10";         PIN[EASY-BRD,gear_step_pin]="mmu:PA4";          PIN[EASY-BRD-RP2040,gear_step_pin]="mmu:gpio27"
PIN[ERB,gear_dir_pin]="mmu:gpio9";           PIN[EASY-BRD,gear_dir_pin]="mmu:PA10";          PIN[EASY-BRD-RP2040,gear_dir_pin]="mmu:gpio28"
PIN[ERB,gear_enable_pin]="mmu:gpio8";        PIN[EASY-BRD,gear_enable_pin]="mmu:PA2";        PIN[EASY-BRD-RP2040,gear_enable_pin]="mmu:gpio26"
PIN[ERB,gear_diag_pin]="mmu:gpio13";         PIN[EASY-BRD,gear_diag_pin]="";                 PIN[EASY-BRD-RP2040,gear_diag_pin]=""
PIN[ERB,selector_uart_pin]="mmu:gpio17";     PIN[EASY-BRD,selector_uart_pin]="mmu:PA8";      PIN[EASY-BRD-RP2040,selector_uart_pin]="mmu:gpio6"
PIN[ERB,selector_step_pin]="mmu:gpio16";     PIN[EASY-BRD,selector_step_pin]="mmu:PA9";      PIN[EASY-BRD-RP2040,selector_step_pin]="mmu:gpio7"
PIN[ERB,selector_dir_pin]="mmu:gpio15";      PIN[EASY-BRD,selector_dir_pin]="mmu:PB8";       PIN[EASY-BRD-RP2040,selector_dir_pin]="mmu:gpio0"
PIN[ERB,selector_enable_pin]="mmu:gpio14";   PIN[EASY-BRD,selector_enable_pin]="mmu:PA11";   PIN[EASY-BRD-RP2040,selector_enable_pin]="mmu:gpio29"
PIN[ERB,selector_diag_pin]="mmu:gpio19";     PIN[EASY-BRD,selector_diag_pin]="mmu:PA7";      PIN[EASY-BRD-RP2040,selector_diag_pin]="mmu:gpio2"
PIN[ERB,selector_endstop_pin]="mmu:gpio24";  PIN[EASY-BRD,selector_endstop_pin]="mmu:PB9";   PIN[EASY-BRD-RP2040,selector_endstop_pin]="mmu:gpio1"
PIN[ERB,servo_pin]="mmu:gpio23";             PIN[EASY-BRD,servo_pin]="mmu:PA5";              PIN[EASY-BRD-RP2040,servo_pin]="mmu:gpio4"
PIN[ERB,encoder_pin]="mmu:gpio22";           PIN[EASY-BRD,encoder_pin]="mmu:PA6";            PIN[EASY-BRD-RP2040,encoder_pin]="mmu:gpio3"

# Screen Colors
OFF='\033[0m'             # Text Reset
BLACK='\033[0;30m'        # Black
RED='\033[0;31m'          # Red
GREEN='\033[0;32m'        # Green
YELLOW='\033[0;33m'       # Yellow
BLUE='\033[0;34m'         # Blue
PURPLE='\033[0;35m'       # Purple
CYAN='\033[0;36m'         # Cyan
WHITE='\033[0;37m'        # White

B_RED='\033[1;31m'        # Bold Red
B_GREEN='\033[1;32m'      # Bold Green
B_YELLOW='\033[1;33m'     # Bold Yellow
B_CYAN='\033[1;36m'       # Bold Cyan

DETAIL="${BLUE}"
INFO="${CYAN}"
EMPHASIZE="${B_CYAN}"
ERROR="${B_RED}"
WARNING="${B_YELLOW}"
PROMPT="${CYAN}"
INPUT="${OFF}"

function nextfilename {
    local name="$1"
    printf "%s-%s.%s" ${name%%.*} $(date '+%Y%m%d_%H%M%S') ${name#*.}
}

function nextsuffix {
    local name="$1"
    local -i num=0
    while [ -e "$name.0$num" ]; do
        num+=1
    done
    printf "%s.0%d" "$name" "$num"
}

verify_not_root() {
    if [ "$EUID" -eq 0 ]; then
        echo -e "${ERROR}This script must not run as root"
        exit -1
    fi
}

check_klipper() {
    if [ "$(sudo systemctl list-units --full -all -t service --no-legend | grep -F "klipper.service")" ]; then
        echo -e "${INFO}Klipper service found"
    else
        echo -e "${ERROR}Klipper service not found! Please install Klipper first"
        exit -1
    fi

}

verify_home_dirs() {
    if [ ! -d "${KLIPPER_HOME}" ]; then
        echo -e "${ERROR}Klipper home directory (${KLIPPER_HOME}) not found. Use '-k <dir>' option to override"
        exit -1
    fi
    if [ ! -d "${KLIPPER_CONFIG_HOME}" ]; then
        if [ ! -d "${OLD_KLIPPER_CONFIG_HOME}" ]; then
            echo -e "${ERROR}Klipper config directory (${KLIPPER_CONFIG_HOME} or ${OLD_KLIPPER_CONFIG_HOME}) not found. Use '-c <dir>' option to override"
            exit -1
        fi
        KLIPPER_CONFIG_HOME="${OLD_KLIPPER_CONFIG_HOME}"
    fi
    echo -e "${INFO}Klipper config directory (${KLIPPER_CONFIG_HOME}) found"
}

# Silently cleanup legacy ERCF-Software-V3 files...
cleanup_old_ercf() {
    # Printer configuration files...
    ercf_files=$(cd ${KLIPPER_CONFIG_HOME}; ls ercf_*.cfg 2>/dev/null | wc -l || true)
    if [ "${ercf_files}" -ne 0 ]; then
        echo -e "${INFO}Cleaning up old Happy Hare v1 installation"
        if [ ! -d "${KLIPPER_CONFIG_HOME}/ercf.uninstalled" ]; then
            mkdir ${KLIPPER_CONFIG_HOME}/ercf.uninstalled
        fi
        for file in `cd ${KLIPPER_CONFIG_HOME} ; ls ercf_*.cfg 2>/dev/null`; do
            mv ${KLIPPER_CONFIG_HOME}/${file} ${KLIPPER_CONFIG_HOME}/ercf.uninstalled/${file}
        done
        if [ -f "${KLIPPER_CONFIG_HOME}/client_macros.cfg" ]; then
            mv ${KLIPPER_CONFIG_HOME}/client_macros.cfg ${KLIPPER_CONFIG_HOME}/ercf.uninstalled/client_macros.cfg
        fi
    fi

    # Klipper modules...
    if [ -d "${KLIPPER_HOME}/klippy/extras" ]; then
        rm -f "${KLIPPER_HOME}/klippy/extras/ercf*.py"
    fi

    # Old klipper logs...
    if [ -d "${KLIPPER_LOGS_HOME}" ]; then
        rm -f "${KLIPPER_LOGS_HOME}/ercf*"
    fi

    # Moonraker update manager...
    file="${KLIPPER_CONFIG_HOME}/moonraker.conf"
    if [ -f "${file}" ]; then
        v1_section=$(grep -c '\[update_manager ercf-happy_hare\]' ${file} || true)
        if [ "${v1_section}" -ne 0 ]; then
            cat "${file}" | sed -e " \
                /\[update_manager ercf-happy_hare\]/,+6 d; \
                    " > "${file}.update" && mv "${file}.update" "${file}"
	fi
    fi

    # printer.cfg includes...
    dest=${KLIPPER_CONFIG_HOME}/printer.cfg
    if test -f $dest; then
        next_dest="$(nextfilename "$dest")"
        v1_includes=$(grep -c '\[include ercf_parameters.cfg\]' ${dest} || true)
        if [ "${v1_includes}" -ne 0 ]; then
            cp ${dest} ${next_dest}
            cat "${dest}" | sed -e " \
                /\[include ercf_software.cfg\]/ d; \
                /\[include ercf_parameters.cfg\]/ d; \
                /\[include ercf_hardware.cfg\]/ d; \
                /\[include ercf_menu.cfg\]/ d; \
                /\[include client_macros.cfg\]/ d; \
                    " > "${dest}.tmp" && mv "${dest}.tmp" "${dest}"
        fi
    fi
}

link_mmu_plugins() {
    echo -e "${INFO}Linking mmu extension to Klipper..."
    if [ -d "${KLIPPER_HOME}/klippy/extras" ]; then
        for file in `cd ${SRCDIR}/extras ; ls *.py`; do
            ln -sf "${SRCDIR}/extras/${file}" "${KLIPPER_HOME}/klippy/extras/${file}"
        done
    else
        echo -e "${WARNING}MMU modules not installed because Klipper 'extras' directory not found!"
    fi
}

unlink_mmu_plugins() {
    echo -e "${INFO}Unlinking mmu extension to Klipper..."
    if [ -d "${KLIPPER_HOME}/klippy/extras" ]; then
        for file in `cd ${SRCDIR}/extras ; ls *.py`; do
            rm -f "${KLIPPER_HOME}/klippy/extras/${file}"
        done
    else
        echo -e "${WARNING}MMU modules not uninstalled because Klipper 'extras' directory not found!"
    fi
}

parse_file() {
    filename="$1"
    prefix_filter="$2"

    # Read old config files
    while IFS= read -r line
    do
        # Remove comments
        line="${line%%#*}"

        # Check if line is not empty
        if [ ! -z "$line" ]; then
            # Split the line into parameter and value
            IFS=":=" read -r parameter value <<< "$line"

            # Remove leading and trailing whitespace
            parameter=$(echo "$parameter" | xargs)
            value=$(echo "$value" | xargs)

	    # If parameter is one of interest and it has a value remember it
            if echo "$parameter" | egrep -q "${prefix_filter}"; then
                if [ "${value}" != "" ]; then
                    eval "${parameter}='${value}'"
                fi
            fi
        fi
    done < "${filename}"
}

update_copy_file() {
    src="$1"
    dest="$2"
    prefix_filter="$3"

    # Read the file line by line
    while IFS="" read -r line || [ -n "$line" ]
    do
        # Check if line is a simple comment
        if echo "$line" | egrep -q '^#'; then
            echo "$line"
        else
            # Split the line into the part before # and the part after #
            parameterAndValueAndSpace=$(echo "$line" | cut -d'#' -f1)
            comment=$(echo "$line" | cut -s -d'#' -f2-)
            space=`printf "%s" "$parameterAndValueAndSpace" | sed 's/.*[^[:space:]]\(.*\)$/\1/'`

            if echo "$parameterAndValueAndSpace" | egrep -q "${prefix_filter}"; then
                # If parameter and value exist, substitute the value with the in memory variable of the same name
                if echo "$parameterAndValueAndSpace" | egrep -q '^\['; then
                    echo "$line"
                elif [ -n "$parameterAndValueAndSpace" ]; then
                    parameter=$(echo "$parameterAndValueAndSpace" | cut -d':' -f1)
                    value=$(echo "$parameterAndValueAndSpace" | cut -d':' -f2)
                    new_value=`eval echo \\${${parameter}}`
                    if [ -n "$comment" ]; then
                        echo "${parameter}: ${new_value}${space}#${comment}"
                    else
                        echo "${parameter}: ${new_value}"
                    fi
                else
                    echo "$line"
                fi
            else
                echo "$line"
            fi
        fi
    done < "$src" >"$dest"
}
# Set default parameters from the distribution (reference) config files
read_default_config() {
    echo -e "${INFO}Reading default configuration parameters..."
    parameters_cfg="mmu_parameters.cfg"

    parse_file "${SRCDIR}/config/${parameters_cfg}"
}

# Pull parameters from previous installation
read_previous_config() {
    parameters_cfg="mmu_parameters.cfg"
    dest_parameters_cfg=${KLIPPER_CONFIG_HOME}/mmu/${parameters_cfg}

    if [ ! -f "${dest_parameters_cfg}" ]; then
        echo -e "${WARNING}No previous ${parameters_cfg} found. Will install default"
    else
        echo -e "${INFO}Applying ${parameters_cfg} configuration from previous installation..."
        parse_file "${dest_parameters_cfg}"

        if [ ! -f "${dest_parameters_cfg}" ]; then
            # Upgrade / map / force old parameters
	    echo

# Nothing yet because Happy Hare is brand new and upgrade from ERCF-Software-V3 not supported
# E.g.
#            selector_offsets=${colorselector}
#            if [ "${sync_load_length}" -gt 0 ]; then
#                sync_load_extruder=1
#            fi
        fi
    fi

    software_cfg="mmu_software.cfg"
    dest_software_cfg=${KLIPPER_CONFIG_HOME}/mmu/${software_cfg}

    if [ ! -f "${dest_software_cfg}" ]; then
        echo -e "${WARNING}No previous ${software_cfg} found. Will install default"
    else
        echo -e "${INFO}Applying ${software_cfg} configuration from previous installation..."
        parse_file "${dest_software_cfg}" "variable_"
    fi
}

copy_config_files() {
    echo -e "${INFO}Copying configuration files into ${KLIPPER_CONFIG_HOME}/mmu directory..."
    if [ ! -d "${KLIPPER_CONFIG_HOME}/mmu" ]; then
        mkdir ${KLIPPER_CONFIG_HOME}/mmu
    fi

    for file in `cd ${SRCDIR}/config ; ls *.cfg`; do
        src=${SRCDIR}/config/${file}
        dest=${KLIPPER_CONFIG_HOME}/mmu/${file}

        if [ -f "${dest}" ]; then
	    if [ "${file}" == "mmu_hardware.cfg" -a "${INSTALL}" -eq 0 ]; then
                echo -e "${WARNING}Skipping copy of hardware config file because already exists"
                continue
            else
                next_dest="$(nextfilename "$dest")"
                echo -e "${DETAIL}Config file ${file} already exists - moving old one to ${next_dest}"
                mv ${dest} ${next_dest}
            fi
        fi

        if [ "${file}" == "mmu_parameters.cfg" ]; then
            update_copy_file "$src" "$dest"

	elif [ "${file}" == "mmu_hardware.cfg" ]; then
            if [ "${SETUP_TOOLHEAD_SENSOR}" -eq 1 ]; then
                magic_str1="## MMU Toolhead sensor"
            else
                magic_str1="NO TOOLHEAD"
            fi
            uart_comment=""
            if [ "${brd_type}" == "ERB" -o "${brd_type}" == "EASY-BRD-RP2040" ]; then
                uart_comment="#"
            fi

            if [ "${SETUP_SELECTOR_TOUCH}" -eq 1 ]; then
                cat ${src} | sed -e "\
                    s/^#diag_pin: \^mmu:SEL_DIAG/diag_pin: \^mmu:SEL_DIAG/; \
                    s/^#driver_SGTHRS: 75/driver_SGTHRS: 75/; \
		    s/^#extra_endstop_pins: tmc2209_selector_stepper:virtual_endstop/extra_endstop_pins: tmc2209_selector_stepper:virtual_endstop/; \
		    s/^#extra_endstop_names: mmu_sel_touch/extra_endstop_names: mmu_sel_touch/; \
                    s/^uart_address:/${uart_comment}uart_address:/; \
                    s/{brd_type}/${brd_type}/; \
                        " > ${dest}.tmp
            else
                # This is the default template config without selector touch enabled
                cat ${src} | sed -e "\
                    s/{brd_type}/${brd_type}/; \
                    s/^uart_address:/${uart_comment}uart_address:/; \
                        " > ${dest}.tmp
            fi

            # Now substitute pin tokens for correct brd_type
            if [ "${brd_type}" == "unknown" ]; then
                cat ${dest}.tmp | sed -e "\
                    s/{toolhead_sensor_pin}/${toolhead_sensor_pin}/; \
                    s%{serial}%${serial}%; \
                    /^${magic_str1} START/,/${magic_str1} END/ s/^#//; \
                        " > ${dest} && rm ${dest}.tmp
            else
                cat ${dest}.tmp | sed -e "\
                    s/{gear_uart_pin}/${PIN[$brd_type,gear_uart_pin]}/; \
                    s/{gear_step_pin}/${PIN[$brd_type,gear_step_pin]}/; \
                    s/{gear_dir_pin}/${PIN[$brd_type,gear_dir_pin]}/; \
                    s/{gear_enable_pin}/${PIN[$brd_type,gear_enable_pin]}/; \
                    s/{gear_diag_pin}/${PIN[$brd_type,gear_diag_pin]}/; \
                    s/{selector_uart_pin}/${PIN[$brd_type,selector_uart_pin]}/; \
                    s/{selector_step_pin}/${PIN[$brd_type,selector_step_pin]}/; \
                    s/{selector_dir_pin}/${PIN[$brd_type,selector_dir_pin]}/; \
                    s/{selector_enable_pin}/${PIN[$brd_type,selector_enable_pin]}/; \
                    s/{selector_diag_pin}/${PIN[$brd_type,selector_diag_pin]}/; \
                    s/{selector_endstop_pin}/${PIN[$brd_type,selector_endstop_pin]}/; \
                    s/{servo_pin}/${PIN[$brd_type,servo_pin]}/; \
                    s/{encoder_pin}/${PIN[$brd_type,encoder_pin]}/; \
                    s/{toolhead_sensor_pin}/${toolhead_sensor_pin}/; \
                    s%{serial}%${serial}%; \
                    /^${magic_str1} START/,/${magic_str1} END/ s/^#//; \
                        " > ${dest} && rm ${dest}.tmp
            fi

         elif [ "${file}" == "mmu_software.cfg" ]; then
            if [ "${INSTALL}" -eq 1 ]; then
                cat ${src} | sed -e "\
                    s%{klipper_config_home}%${KLIPPER_CONFIG_HOME}%g; \
                        " > ${dest}
            else
                klipper_config_home="${KLIPPER_CONFIG_HOME}"
                update_copy_file "$src" "$dest" "variable_"
            fi

        else
            cp ${src} ${dest}
	fi
    done
}

install_printer_includes() {
    # Link in all includes if not already present
    dest=${KLIPPER_CONFIG_HOME}/printer.cfg
    if test -f $dest; then
        next_dest="$(nextfilename "$dest")"
        echo -e "${INFO}Copying original printer.cfg file to ${next_dest}"
        cp ${dest} ${next_dest}
        if [ ${MENU_12864} -eq 1 ]; then
            i='\[include mmu/mmu_menu.cfg\]'
            already_included=$(grep -c "${i}" ${dest} || true)
            if [ "${already_included}" -eq 0 ]; then
                sed -i "1i ${i}" ${dest}
            fi
        fi
        if [ ${ERCF_COMPAT} -eq 1 ]; then
            i='\[include mmu/mmu_ercf_compat.cfg\]'
            already_included=$(grep -c "${i}" ${dest} || true)
            if [ "${already_included}" -eq 0 ]; then
                sed -i "1i ${i}" ${dest}
            fi
        fi
        if [ ${CLIENT_MACROS} -eq 1 ]; then
            i='\[include mmu/client_macros.cfg\]'
            already_included=$(grep -c "${i}" ${dest} || true)
            if [ "${already_included}" -eq 0 ]; then
                sed -i "1i ${i}" ${dest}
            fi
        fi
        for i in \
                '\[include mmu/mmu_software.cfg\]' \
                '\[include mmu/mmu_parameters.cfg\]' \
                '\[include mmu/mmu_hardware.cfg\]' ; do
            already_included=$(grep -c "${i}" ${dest} || true)
            if [ "${already_included}" -eq 0 ]; then
                sed -i "1i ${i}" ${dest}
            fi
        done
    else
        echo -e "${WARNING}File printer.cfg file not found! Cannot include MMU configuration files"
    fi
}

uninstall_printer_includes() {
    prefix=${1}_
    echo -e "${INFO}Removing ${prefix} MMU configuration files from ${KLIPPER_CONFIG_HOME}"
    for file in `cd ${SRCDIR}/config ; ls *.cfg`; do
        dest=${KLIPPER_CONFIG_HOME}/mmu/${file}

        if test -f $dest; then
            echo -e "${INFO}Removing config file ${file}"
            mv -f ${dest} /tmp/${file}_uninstalled
        fi
    done

    echo -e "${INFO}Cleaning MMU references from printer.cfg"
    dest=${KLIPPER_CONFIG_HOME}/printer.cfg
    if test -f $dest; then
        next_dest="$(nextfilename "$dest")"
        echo -e "${INFO}Copying original printer.cfg file to ${next_dest} before cleaning"
        cp ${dest} ${next_dest}
        cat "${dest}" | sed -e " \
            /\[include mmu/client_macros.cfg\]/ d; \
            /\[include mmu/mmu_software.cfg\]/ d; \
            /\[include mmu/mmu_parameters.cfg\]/ d; \
            /\[include mmu/mmu_hardware.cfg\]/ d; \
            /\[include mmu/mmu_menu.cfg\]/ d; \
            /\[include mmu/mmu_ercf_compat.cfg\]/ d; \
	        " > "${dest}.tmp" && mv "${dest}.tmp" "${dest}"
    fi
}

install_update_manager() {
    echo -e "${INFO}Adding update manager to moonraker.conf"
    file="${KLIPPER_CONFIG_HOME}/moonraker.conf"
    if [ -f "${file}" ]; then
        update_section=$(grep -c '\[update_manager happy-hare\]' ${file} || true)
        if [ "${update_section}" -eq 0 ]; then
            echo "" >> "${file}"
            while read -r line; do
                echo -e "${line}" >> "${file}"
            done < "${SRCDIR}/moonraker_update.txt"
            echo "" >> "${file}"
            restart_moonraker
        else
            echo -e "${INFO}[update_manager happy-hare] already exist in moonraker.conf - skipping install"
        fi
    else
        echo -e "${WARNING}moonraker.conf not found!"
    fi
}

uninstall_update_manager() {
    echo -e "${INFO}Removing update manager from moonraker.conf"
    file="${KLIPPER_CONFIG_HOME}/moonraker.conf"
    if [ -f "${file}" ]; then
        update_section=$(grep -c '\[update_manager happy-hare\]' ${file} || true)
        if [ "${update_section}" -eq 0 ]; then
            echo -e "${INFO}[update_manager happy-hare] not found in moonraker.conf - skipping removal"
        else
            cat "${file}" | sed -e " \
                /\[update_manager happy-hare\]/,+6 d; \
                    " > "${file}.new" && mv "${file}.new" "${file}"
            restart_moonraker
        fi
    else
        echo -e "${WARNING}moonraker.conf not found!"
    fi
}

restart_klipper() {
    echo -e "${INFO}Restarting Klipper..."
    sudo systemctl restart klipper
}

restart_moonraker() {
    echo -e "${INFO}Restarting Moonraker..."
    sudo systemctl restart moonraker
}

prompt_yn() {
    while true; do
        read -p "$@ (y/n)?" yn
        case "${yn}" in
            Y|y|Yes|yes)
                echo "y" 
                break;;
            N|n|No|no)
                echo "n" 
                break;;
            *)
                ;;
        esac
    done
}

questionaire() {
    # PAUL TODO Vendor type and sub selections
    VENDOR="ERCF"
    echo
    echo -e "${INFO}Let me see if I can help you with initial config (you will still have some manual config to perform)...${INPUT}"
    echo
    brd_type="unknown"
    yn=$(prompt_yn "Are you using the EASY-BRD or Fysetc Burrows ERB controller?")
    case $yn in
        y)
            echo -e "${INFO}Great, I can setup almost everything for you. Let's get started"
            serial=""
            echo
            for line in `ls /dev/serial/by-id | egrep "Klipper_samd21|Klipper_rp2040"`; do
                if echo ${line} | grep --quiet "Klipper_samd21"; then
                    brd_type="EASY-BRD"
                else
                    echo -e "${PROMPT}You seem to have a ${EMPHASIZE}RP2040-based${PROMPT} controller serial port.${INPUT}"
                    yn=$(prompt_yn "Are you using the Fysetc Burrows ERB controller?")
                    case $yn in
                    y)
                        brd_type="ERB"
                        ;;
                    n)
                        brd_type="EASY-BRD-RP2040"
                        ;;
                    esac
                fi
                echo -e "${PROMPT}This looks like your ${EMPHASIZE}${brd_type}${PROMPT} controller serial port. Is that correct?${INPUT}"
                yn=$(prompt_yn "/dev/serial/by-id/${line}")
                case $yn in
                    y)
                        serial="/dev/serial/by-id/${line}"
                        break
                        ;;
                    n)
                        brd_type="unknown"
                        ;;
                esac
            done
            if [ "${serial}" == "" ]; then
                echo
                echo -e "${PROMPT}Couldn't find your serial port, but no worries - I'll configure the default and you can manually change later${INPUT}"
                yn=$(prompt_yn "Setup for EASY-BRD? (Answer 'N' for Fysetc Burrows ERB)")
                case $yn in
                    y)
                        yn1=$(prompt_yn "EASY-BRD with SAMD21 (default)? (Answer 'N' for EASY-BRD with RP2040)")
                        case $yn1 in
                            y)
                                brd_type="EASY-BRD"
                                ;;
                            n)
                                brd_type="EASY-BRD-RP2040"
                                ;;
                        esac
                        ;;
                    n)
                        brd_type="ERB"
                        ;;
                esac
                serial='/dev/ttyACM1 # Config guess. Run ls -l /dev/serial/by-id and set manually'
            fi

            echo
            echo -e "${WARNING}Board Type: ${brd_type}"

            echo
            echo -e "${PROMPT}Touch selector operation using TMC Stallguard? This allows for additional selector recovery steps but is difficult to tune${INPUT}"
            yn=$(prompt_yn "Enable selector touch operation (recommend no if you are new to ERCF")
            case $yn in
                y)
                    if [ "${brd_type}" == "EASY-BRD" ]; then
                        echo
                        echo -e "${WARNING}    IMPORTANT: Set the J6 jumper pins to 2-3 and 4-5, i.e. .[..][..]  MAKE A NOTE NOW!!"
                    fi
                    SETUP_SELECTOR_TOUCH=1
                    ;;
                n)
                    if [ "${brd_type}" == "EASY-BRD" ]; then
                        echo
                        echo -e "${WARNING}    IMPORTANT: Set the J6 jumper pins to 1-2 and 4-5, i.e. [..].[..]  MAKE A NOTE NOW!!"
                    fi
                    SETUP_SELECTOR_TOUCH=0
                    ;;
            esac
            ;;

        n)
            easy_brd=0
            echo -e "${INFO}Ok, I can only partially setup non EASY-BRD/ERB installations, but lets see what I can help with"
            serial=""
            echo
            for line in `ls /dev/serial/by-id`; do
                echo -e "${PROMPT}Is this the serial port to your MMU mcu?${INPUT}"
                yn=$(prompt_yn "/dev/serial/by-id/${line}")
                case $yn in
                    y)
                        serial="/dev/serial/by-id/${line}"
                        break
                        ;;
                    n)
                        ;;
                esac
            done
            if [ "${serial}" = "" ]; then
                echo -e "${INFO}Couldn't find your serial port, but no worries - I'll configure the default and you can manually change later as per the docs"
                serial='/dev/ttyACM1 # Config guess. Run ls -l /dev/serial/by-id and set manually'
            fi

            echo
            echo -e "${PROMPT}Touch selector operation using TMC Stallguard? This allows for additional selector recovery steps but is difficult to tune${INPUT}"
            yn=$(prompt_yn "Enable selector touch operation (recommend no for now")
            case $yn in
                y)
                    SETUP_SELECTOR_TOUCH=1
                    ;;
                n)
                    SETUP_SELECTOR_TOUCH=0
                    ;;
            esac
            ;;
    esac

    mmu_num_gates=9
    echo
    echo -e "${PROMPT}How many gates (selectors) do you have (eg 3, 6, 9, 12)?${INPUT}"
    while true; do
        read -p "Number of gates? " mmu_num_gates
        if ! [ "${mmu_num_gates}" -ge 1 ] 2> /dev/null ;then
            echo -e "${INFO}Positive integer value only"
      else
           break
       fi
    done

    echo
    echo -e "${PROMPT}Do you have a toolhead sensor you would like to use? If reliable this provides the smoothest and most reliable loading and unloading operation${INPUT}"
    yn=$(prompt_yn "Enable toolhead sensor")
    case $yn in
        y)
            SETUP_TOOLHEAD_SENSOR=1
            echo -e "${PROMPT}    What is the mcu pin name that your toolhead sensor is connected too?"
            echo -e "${PROMPT}    If you don't know just hit return, I can enter a default and you can change later${INPUT}"
            read -p "    Toolhead sensor pin name? " toolhead_sensor_pin
            if [ "${toolhead_sensor_pin}" = "" ]; then
                toolhead_sensor_pin="{dummy_pin_must_set_me}"
            fi
            ;;
        n)
            SETUP_TOOLHEAD_SENSOR=0
            toolhead_sensor_pin="{dummy_pin_must_set_me}"
            ;;
    esac

    echo
    echo -e "${PROMPT}Using default MG-90S servo? (Answer 'N' for Savox SH0255MG, you can always change it later)${INPUT}"
    yn=$(prompt_yn "MG-90S Servo?")
    case $yn in
        y)
            servo_up_angle=30
            servo_down_angle=140
            ;;
        n)
            servo_up_angle=140
            servo_down_angle=30
            ;;
    esac

    echo
    echo -e "${PROMPT}Clog detection? This uses the MMU encoder movement to detect clogs and can call your filament runout logic${INPUT}"
    yn=$(prompt_yn "Enable clog detection")
    case $yn in
        y)
            enable_clog_detection=1
            echo -e "${PROMPT}    Would you like MMU to automatically adjust clog detection length (recommended)?${INPUT}"
            yn=$(prompt_yn "    Automatic")
            if [ "${yn}" == "y" ]; then
                enable_clog_detection=2
            fi
            ;;
        n)
            enable_clog_detection=0
            ;;
    esac

    echo
    echo -e "${PROMPT}EndlessSpool? This uses filament runout detection to automate switching to new spool without interruption${INPUT}"
    yn=$(prompt_yn "Enable EndlessSpool")
    case $yn in
        y)
            enable_endless_spool=1
            if [ "${enable_clog_detection}" -eq 0 ]; then
                echo
                echo -e "${WARNING}    NOTE: I've re-enabled clog detection which is necessary for EndlessSpool to function"
                enable_clog_detection=2
            fi
            ;;
        n)
           enable_endless_spool=0
           ;;
    esac

    echo
    MENU_12864=0
    ERCF_COMPAT=0
    echo -e "${PROMPT}Finally, would you like me to include all the MMU config files into your printer.cfg file${INPUT}"
    yn=$(prompt_yn "Add include?")
    case $yn in
        y)
            INSTALL_PRINTER_INCLUDES=1
            echo -e "${PROMPT}    Would you like to include Mini 12864 menu configuration extension for MMU${INPUT}"
            yn=$(prompt_yn "    Include menu")
            case $yn in
                y)
                    MENU_12864=1
                    ;;
                n)
                    MENU_12864=0
                    ;;
            esac

            echo -e "${PROMPT}    Would you like to include legacy ERCF_ command compatibility module${INPUT}"
            yn=$(prompt_yn "    Include legacy ERCF command set")
            case $yn in
                y)
                    ERCF_COMPAT=1
                    ;;
                n)
                    ERCF_COMPAT=0
                    ;;
            esac

            echo -e "${PROMPT}    Would you like to include the default pause/resume macros supplied with Happy Hare${INPUT}"
            yn=$(prompt_yn "    Include client_macros.cfg")
            case $yn in
                y)
                    CLIENT_MACROS=1
                    ;;
                n)
                    CLIENT_MACROS=0
                    ;;
            esac
	    ;;
        n)
            INSTALL_PRINTER_INCLUDES=0
            ;;
    esac

    echo
    echo -e "${INFO}"
    echo "    vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv"
    echo
    echo "    NOTES:"
    echo "     What still needs to be done:"
    if [ "${brd_type}" == "unknown" ]; then
        echo "     * Edit *.cfg files and substitute all \{xxx\} tokens to match or setup"
        echo "     * Review all pin configuration and change to match your mcu"
    else
        echo "     * Tweak motor speeds and current, especially if using non BOM motors"
        echo "     * Adjust motor direction with '!' on pin if necessary. No way to know here"
    fi
    echo "     * Move you extruder stepper configuration into mmu_hardware.cfg"
    echo "     * Adjust your config for loading and unloading preferences"
    echo 
    echo "    Advanced:"
    echo "         * Tweak configurations like speed and distance in mmu_parameter.cfg"
    echo 
    echo "    Good luck! MMU is complex to setup. Remember Discord is your friend.."
    echo
    echo "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^"
    echo
}

usage() {
    echo -e "${EMPHASIZE}"
    echo "Usage: $0 [-k <klipper_home_dir>] [-c <klipper_config_dir>] [-i] [-u]"
    echo
    echo "-i for interactive install"
    echo "-u for uninstall"
    echo "(no flags for safe re-install / upgrade)"
    echo
    exit 1
}

# Force script to exit if an error occurs
set -e
clear

# Find SRCDIR from the pathname of this script
SRCDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )"/ && pwd )"
SETUP_TOOLHEAD_SENSOR=0
SETUP_SELECTOR_TOUCH=0

INSTALL=0
UNINSTALL=0
INSTALL_KLIPPER_SCREEN_ONLY=0
while getopts "k:c:id" arg; do
    case $arg in
        k) KLIPPER_HOME=${OPTARG};;
        c) KLIPPER_CONFIG_HOME=${OPTARG};;
        i) INSTALL=1;;
        d) UNINSTALL=1;;
        *) usage;;
    esac
done

if [ "${INSTALL}" -eq 1 -a "${UNINSTALL}" -eq 1 ]; then
    echo -e "${ERROR}Can't install and uninstall at the same time!"
    usage
fi

verify_not_root
verify_home_dirs
check_klipper
cleanup_old_ercf
if [ "$UNINSTALL" -eq 0 ]; then
    # Set in memory parameters from default file
    read_default_config
    if [ "${INSTALL}" -eq 1 ]; then
        # Update in memory parameters from questionaire
        questionaire
        if [ "${INSTALL_PRINTER_INCLUDES}" -eq 1 ]; then
            install_printer_includes
        fi
    else
        # Update in memory parameters from previous install
        read_previous_config
    fi
    copy_config_files
    link_mmu_plugins
    install_update_manager
else
    echo
    echo -e "${WARNING}You have asked me to remove Happy Hare and cleanup"
    echo
    yn=$(prompt_yn "Are you sure you want to proceed with deleting Happy Hare?")
    case $yn in
        y)
            unlink_mmu_plugins
            remove_template_files
            uninstall_update_manager
            echo -e "${INFO}Uninstall complete except for the Happy-Hare directory - you can now safely delete that as well"
            ;;
        n)
            echo -e "${INFO}Well that was a close call!  Everything still intact"
            echo
	    exit 0
            ;;
    esac
fi
restart_klipper

if [ "$UNINSTALL" -eq 0 ]; then
    echo -e "${EMPHASIZE}"
    echo "Done.  Enjoy ERCF (and thank you Ette for a wonderful design)..."
    echo -e "${INFO}"
    echo '(\_/)'
    echo '( *,*)'
    echo '(")_(") MMU Ready'
    echo
else
    echo -e "${EMPHASIZE}"
    echo "Done.  Sad to see you go (but maybe you'll be back)..."
    echo -e "${INFO}"
    echo '(\_/)'
    echo '( v,v)'
    echo '(")^(") MMU Unready'
    echo
fi
