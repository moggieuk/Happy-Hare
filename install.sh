#!/bin/bash
# Happy Hare MMU Software
# Installer / Updater script
#
# Copyright (C) 2022  moggieuk#6538 (discord) moggieuk@hotmail.com
#
VERSION=2.4 # Important: Keep synced with mmy.py

KLIPPER_HOME="${HOME}/klipper"
MOONRAKER_HOME="${HOME}/moonraker"
KLIPPER_CONFIG_HOME="${HOME}/printer_data/config"
KLIPPER_LOGS_HOME="${HOME}/printer_data/logs"
OLD_KLIPPER_CONFIG_HOME="${HOME}/klipper_config"
SENSORS_SECTION="FILAMENT SENSORS"
LED_SECTION="MMU OPTIONAL NEOPIXEL"


declare -A PIN 2>/dev/null || {
    echo "Please run this script with bash $0"
    exit 1
}

# Pins for Fysetc Burrows ERB board, Original EASY-BRD and EASY-BRD with Seed Studio XIAO RP2040
# Note: uart pin is shared on original EASY-BRD (with different uart addresses)
#
PIN[ERB,gear_uart_pin]="gpio20";         PIN[EASY-BRD,gear_uart_pin]="PA8";         PIN[EASY-BRD-RP2040,gear_uart_pin]="gpio6"
PIN[ERB,gear_step_pin]="gpio10";         PIN[EASY-BRD,gear_step_pin]="PA4";         PIN[EASY-BRD-RP2040,gear_step_pin]="gpio27"
PIN[ERB,gear_dir_pin]="gpio9";           PIN[EASY-BRD,gear_dir_pin]="PA10";         PIN[EASY-BRD-RP2040,gear_dir_pin]="gpio28"
PIN[ERB,gear_enable_pin]="gpio8";        PIN[EASY-BRD,gear_enable_pin]="PA2";       PIN[EASY-BRD-RP2040,gear_enable_pin]="gpio26"
PIN[ERB,gear_diag_pin]="gpio13";         PIN[EASY-BRD,gear_diag_pin]="";            PIN[EASY-BRD-RP2040,gear_diag_pin]=""
PIN[ERB,selector_uart_pin]="gpio17";     PIN[EASY-BRD,selector_uart_pin]="PA8";     PIN[EASY-BRD-RP2040,selector_uart_pin]="gpio6"
PIN[ERB,selector_step_pin]="gpio16";     PIN[EASY-BRD,selector_step_pin]="PA9";     PIN[EASY-BRD-RP2040,selector_step_pin]="gpio7"
PIN[ERB,selector_dir_pin]="gpio15";      PIN[EASY-BRD,selector_dir_pin]="PB8";      PIN[EASY-BRD-RP2040,selector_dir_pin]="gpio0"
PIN[ERB,selector_enable_pin]="gpio14";   PIN[EASY-BRD,selector_enable_pin]="PA11";  PIN[EASY-BRD-RP2040,selector_enable_pin]="gpio29"
PIN[ERB,selector_diag_pin]="gpio19";     PIN[EASY-BRD,selector_diag_pin]="PA7";     PIN[EASY-BRD-RP2040,selector_diag_pin]="gpio2"
PIN[ERB,selector_endstop_pin]="gpio24";  PIN[EASY-BRD,selector_endstop_pin]="PB9";  PIN[EASY-BRD-RP2040,selector_endstop_pin]="gpio1"
PIN[ERB,servo_pin]="gpio23";             PIN[EASY-BRD,servo_pin]="PA5";             PIN[EASY-BRD-RP2040,servo_pin]="gpio4"
PIN[ERB,encoder_pin]="gpio22";           PIN[EASY-BRD,encoder_pin]="PA6";           PIN[EASY-BRD-RP2040,encoder_pin]="gpio3"
PIN[ERB,neopixel_pin]="gpio21";          PIN[EASY-BRD,neopixel_pin]="";             PIN[EASY-BRD-RP2040,neopixel_pin]=""
PIN[ERB,gate_sensor_pin]="gpio22";       PIN[EASY-BRD,gate_sensor_pin]="PA6";       PIN[EASY-BRD-RP2040,gate_sensor_pin]="gpio3";
PIN[ERB,pre_gate_0_pin]="";              PIN[EASY-BRD,pre_gate_0_pin]="";           PIN[EASY-BRD-RP2040,pre_gate_0_pin]="";
PIN[ERB,pre_gate_1_pin]="";              PIN[EASY-BRD,pre_gate_1_pin]="";           PIN[EASY-BRD-RP2040,pre_gate_1_pin]="";
PIN[ERB,pre_gate_2_pin]="";              PIN[EASY-BRD,pre_gate_2_pin]="";           PIN[EASY-BRD-RP2040,pre_gate_2_pin]="";
PIN[ERB,pre_gate_3_pin]="";              PIN[EASY-BRD,pre_gate_3_pin]="";           PIN[EASY-BRD-RP2040,pre_gate_3_pin]="";
PIN[ERB,pre_gate_4_pin]="";              PIN[EASY-BRD,pre_gate_4_pin]="";           PIN[EASY-BRD-RP2040,pre_gate_4_pin]="";
PIN[ERB,pre_gate_5_pin]="";              PIN[EASY-BRD,pre_gate_5_pin]="";           PIN[EASY-BRD-RP2040,pre_gate_5_pin]="";
PIN[ERB,pre_gate_6_pin]="";              PIN[EASY-BRD,pre_gate_6_pin]="";           PIN[EASY-BRD-RP2040,pre_gate_6_pin]="";
PIN[ERB,pre_gate_7_pin]="";              PIN[EASY-BRD,pre_gate_7_pin]="";           PIN[EASY-BRD-RP2040,pre_gate_7_pin]="";
PIN[ERB,pre_gate_8_pin]="";              PIN[EASY-BRD,pre_gate_8_pin]="";           PIN[EASY-BRD-RP2040,pre_gate_8_pin]="";
PIN[ERB,pre_gate_9_pin]="";              PIN[EASY-BRD,pre_gate_9_pin]="";           PIN[EASY-BRD-RP2040,pre_gate_9_pin]="";
PIN[ERB,pre_gate_10_pin]="";             PIN[EASY-BRD,pre_gate_10_pin]="";          PIN[EASY-BRD-RP2040,pre_gate_10_pin]="";
PIN[ERB,pre_gate_11_pin]="";             PIN[EASY-BRD,pre_gate_11_pin]="";          PIN[EASY-BRD-RP2040,pre_gate_11_pin]="";

# Pins for Mellow EASY-BRD with CANbus
#
PIN[MELLOW-EASY-BRD-CAN,gear_uart_pin]="ercf:gpio9";
PIN[MELLOW-EASY-BRD-CAN,gear_step_pin]="ercf:gpio7";
PIN[MELLOW-EASY-BRD-CAN,gear_dir_pin]="!ercf:gpio8";
PIN[MELLOW-EASY-BRD-CAN,gear_enable_pin]="!ercf:gpio6";
PIN[MELLOW-EASY-BRD-CAN,gear_diag_pin]="ercf:gpio23";
PIN[MELLOW-EASY-BRD-CAN,selector_uart_pin]="ercf:gpio0";
PIN[MELLOW-EASY-BRD-CAN,selector_step_pin]="ercf:gpio2";
PIN[MELLOW-EASY-BRD-CAN,selector_dir_pin]="!ercf:gpio1";
PIN[MELLOW-EASY-BRD-CAN,selector_enable_pin]="!ercf:gpio3";
PIN[MELLOW-EASY-BRD-CAN,selector_diag_pin]="^ercf:gpio22";
PIN[MELLOW-EASY-BRD-CAN,selector_endstop_pin]="ercf:gpio20";
PIN[MELLOW-EASY-BRD-CAN,servo_pin]="ercf:gpio21";
PIN[MELLOW-EASY-BRD-CAN,encoder_pin]="ercf:gpio15";
PIN[MELLOW-EASY-BRD-CAN,neopixel_pin]="";
PIN[MELLOW-EASY-BRD-CAN,gate_sensor_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_0_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_1_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_2_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_3_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_4_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_5_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_6_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_7_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_8_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_9_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_10_pin]="";
PIN[MELLOW-EASY-BRD-CAN,pre_gate_11_pin]="";

# Pins for BTT MMB board (gear on motor1, selector on motor2, endstop on STP11, optional gate sensor on STP1 if no gear DIAG use)
#
PIN[MMB,gear_uart_pin]="PA10";       # M1
PIN[MMB,gear_step_pin]="PB15";
PIN[MMB,gear_dir_pin]="PB14";
PIN[MMB,gear_enable_pin]="PA8";
PIN[MMB,gear_diag_pin]="PA3";	     # Aka STP1
PIN[MMB,selector_uart_pin]="PC7";    # M2
PIN[MMB,selector_step_pin]="PD2";
PIN[MMB,selector_dir_pin]="PB13";
PIN[MMB,selector_enable_pin]="PD1";
PIN[MMB,selector_diag_pin]="PA4";    # Aka STP2
PIN[MMB,selector_endstop_pin]="PB2"; # STP11
PIN[MMB,servo_pin]="PA0";
PIN[MMB,encoder_pin]="PA1";
PIN[MMB,neopixel_pin]="PA2";
PIN[MMB,gate_sensor_pin]="PA3";      # STP1 (if not DIAG)
PIN[MMB,pre_gate_0_pin]="PB9";       # STP3
PIN[MMB,pre_gate_1_pin]="PB8";       # STP4
PIN[MMB,pre_gate_2_pin]="PC15";      # STP5
PIN[MMB,pre_gate_3_pin]="PC13";      # STP6
PIN[MMB,pre_gate_4_pin]="PC14";      # STP7
PIN[MMB,pre_gate_5_pin]="PB12";      # STP8
PIN[MMB,pre_gate_6_pin]="PB11";      # STP9
PIN[MMB,pre_gate_7_pin]="PB10";      # STP10
PIN[MMB,pre_gate_8_pin]="";
PIN[MMB,pre_gate_9_pin]="";
PIN[MMB,pre_gate_10_pin]="";
PIN[MMB,pre_gate_11_pin]="";

# These pins will usually be on main mcu for wiring simplification
#
PIN[toolhead_sensor_pin]=""
PIN[extruder_sensor_pin]=""
PIN[gantry_servo_pin]=""

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
B_WHITE='\033[1;37m'      # Bold White

TITLE="${B_WHITE}"
DETAIL="${BLUE}"
INFO="${CYAN}"
EMPHASIZE="${B_CYAN}"
ERROR="${B_RED}"
WARNING="${B_YELLOW}"
PROMPT="${CYAN}"
INPUT="${OFF}"
SECTION="----------\n"

function nextfilename {
    local name="$1"
    if [ -d "${name}" ]; then
        printf "%s-%s" ${name%%.*} $(date '+%Y%m%d_%H%M%S')
    else
        printf "%s-%s.%s-old" ${name%%.*} $(date '+%Y%m%d_%H%M%S') ${name#*.}
    fi
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
    if [ "$NOSERVICE" -ne 1 ]; then
        if [ "$(sudo systemctl list-units --full -all -t service --no-legend | grep -F "klipper.service")" ]; then
            echo -e "${INFO}Klipper service found"
        else
            echo -e "${ERROR}Klipper service not found! Please install Klipper first"
            exit -1
        fi
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

    if [ ! -d "${MOONRAKER_HOME}" ]; then
        echo -e "${ERROR}Moonraker home directory (${MOONRAKER_HOME}) not found. Use '-m <dir>' option to override"
        exit -1
    fi
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

# TEMPORARY: Upgrade to mmu-toolhead version from manual_stepper
cleanup_manual_stepper_version() {
    # Legacy klipper modules...
    if [ -d "${KLIPPER_HOME}/klippy/extras" ]; then
        rm -f "${KLIPPER_HOME}/klippy/extras/manual_mh_stepper.py"
        rm -f "${KLIPPER_HOME}/klippy/extras/manual_extruder_stepper.py"
        # Used as upgrade reminder rm -f "${KLIPPER_HOME}/klippy/extras/mmu_config_setup.py"
    fi

    # Upgrade mmu_hardware.cfg...
    hardware_cfg="${KLIPPER_CONFIG_HOME}/mmu/base/mmu_hardware.cfg"
    found_manual_stepper=$(egrep -c "\[mmu_config_setup\]|\[manual_extruder_stepper extruder\]" ${hardware_cfg} || true)
    if [ "${found_manual_stepper}" -ne 0 ]; then
        cat "${hardware_cfg}" | sed -e " \
            /\[mmu_config_setup\]/ d; \
            /^velocity: .*/ d; \
            /^accel: .*/ d; \
            s%\[\(.*\) manual_extruder_stepper extruder\]%# REMOVE/MOVE THIS SECTION vvv\n\[\1 manual_extruder_stepper extruder\]%; \
            s%\[manual_extruder_stepper extruder\]%# REMOVE/MOVE THIS SECTION vvv\n\[manual_extruder_stepper extruder\]%; \
            s%\[\(.*\) manual_extruder_stepper gear_stepper\]%\[\1 stepper_mmu_gear\]%; \
            s%\[manual_extruder_stepper gear_stepper\]%\[stepper_mmu_gear\]%; \
            s%\[\(.*\) manual_mh_stepper selector_stepper\]%\[\1 stepper_mmu_selector\]%; \
            s%\[manual_mh_stepper selector_stepper\]%\[stepper_mmu_selector\]%; \
            s%: \(.*\)_gear_stepper:virtual_endstop%: \1_stepper_mmu_gear:virtual_endstop%; \
            s%: \(.*\)_selector_stepper:virtual_endstop%: \1_stepper_mmu_selector:virtual_endstop%; \
                " > "${hardware_cfg}.tmp" && mv "${hardware_cfg}.tmp" "${hardware_cfg}"

        echo -e "${WARNING}"
        echo "------------------------- IMPORTANT INFO ON NEW MMU TOOLHEAD DEFINITION - READ ME --------------------------"
        echo "  This version of Happy Hare no longer requires the move of the [extruder] definition into mmu_hardware.cfg"
        echo "  You need to restore the sections marked in your mmu_hardware.cfg back to your original extruder config"
        echo "  and delete those sections from mmu_hardware.cfg.  Also note that the gear are selector stepper definitions"
        echo "  have been modified to be compatible with the new MMU toolhead feature of this version"
        echo
        echo "  If you see an error similar to:"
        echo -e "${ERROR}  Option 'microsteps' in section 'manual_extruder_stepper extruder' must be specified"
        echo -e "${WARNING}"
        echo "  Edit mmu_hardware.cfg and restart Klipper to complete the upgrade"
        echo "------------------------------------------------------------------------------------------------------------"
        echo
    fi
}

# TEMPORARY: Upgrade mmu sensors part of mmu_hardware.cfg
upgrade_mmu_sensors() {
    hardware_cfg="${KLIPPER_CONFIG_HOME}/mmu/base/mmu_hardware.cfg"
    found_mmu_sensors=$(egrep -c "${SENSORS_SECTION}" ${hardware_cfg} || true)

    if [ "${found_mmu_sensors}" -eq 0 ]; then
        # Form new section ready for insertion at end of existing mmu_hardware.cfg
        sed -n "/${SENSORS_SECTION}/,+26p" "${SRCDIR}/config/base/mmu_hardware.cfg" | sed -e " \
                    s/^/#/; \
                " > "${hardware_cfg}.tmp"

        # Add new mmu sensors config section
        echo -e "${INFO}Adding new mmu sensors section (commented out) to mmu_hardware.cfg..."
        cat "${hardware_cfg}.tmp" >> "${hardware_cfg}" && rm "${hardware_cfg}.tmp"
    fi
}

# TEMPORARY: Upgrade led effects part of mmu_hardware.cfg (assumed last part of file)
upgrade_led_effects() {
    hardware_cfg="${KLIPPER_CONFIG_HOME}/mmu/base/mmu_hardware.cfg"
    found_led_effects=$(egrep -c "${LED_SECTION}" ${hardware_cfg} || true)

    if [ "${found_led_effects}" -eq 0 ]; then
        # Form new section ready for insertion at end of existing mmu_hardware.cfg
        sed -n "/${LED_SECTION}/,\$p" "${SRCDIR}/config/base/mmu_hardware.cfg" | sed -e " \
                    s/^/#/; \
                    s/{mmu_num_gates}/${mmu_num_gates}/; \
                    s/{mmu_num_leds}/${mmu_num_leds}/; \
                " > "${hardware_cfg}.tmp"

        # Add new led config section
        echo -e "${INFO}Adding new LED control section (commented out) to mmu_hardware.cfg..."
        cat "${hardware_cfg}.tmp" >> "${hardware_cfg}" && rm "${hardware_cfg}.tmp"
    fi
}

link_mmu_plugins() {
    echo -e "${INFO}Linking mmu extensions to Klipper..."
    if [ -d "${KLIPPER_HOME}/klippy/extras" ]; then
        for file in `cd ${SRCDIR}/extras ; ls *.py`; do
            ln -sf "${SRCDIR}/extras/${file}" "${KLIPPER_HOME}/klippy/extras/${file}"
        done
    else
        echo -e "${WARNING}Klipper extensions not installed because Klipper 'extras' directory not found!"
    fi

    echo -e "${INFO}Linking mmu extension to Moonraker..."
    if [ -d "${MOONRAKER_HOME}/moonraker/components" ]; then
        for file in `cd ${SRCDIR}/components ; ls *.py`; do
            ln -sf "${SRCDIR}/components/${file}" "${MOONRAKER_HOME}/moonraker/components/${file}"
        done
    else
        echo -e "${WARNING}Moonraker extensions not installed because Moonraker 'components' directory not found!"
    fi
}

unlink_mmu_plugins() {
    echo -e "${INFO}Unlinking mmu extensions from Klipper..."
    if [ -d "${KLIPPER_HOME}/klippy/extras" ]; then
        for file in `cd ${SRCDIR}/extras ; ls *.py`; do
            rm -f "${KLIPPER_HOME}/klippy/extras/${file}"
        done
    else
        echo -e "${WARNING}MMU modules not uninstalled because Klipper 'extras' directory not found!"
    fi

    echo -e "${INFO}Unlinking mmu extension from Moonraker..."
    if [ -d "${MOONRAKER_HOME}/moonraker/components" ]; then
        for file in `cd ${SRCDIR}/components ; ls *.py`; do
            rm -f "${MOONRAKER_HOME}/moonraker/components/${file}"
        done
    else
        echo -e "${WARNING}MMU modules not uninstalled because Moonraker 'components' directory not found!"
    fi
}

parse_file() {
    filename="$1"
    prefix_filter="$2"
    namespace="$3"

    # Read old config files
    while IFS= read -r line
    do
        # Remove comments
        line="${line%%#*}"
        line="${line%%;*}"

        # Check if line is not empty and contains variable or parameter
        if [ ! -z "$line" ] && { [ -z "$prefix_filter" ] || [ "${line#$prefix_filter}" != "$line" ]; }; then
            # Split the line into parameter and value
            IFS=":=" read -r parameter value <<< "$line"

            # Remove leading and trailing whitespace
            parameter=$(echo "$parameter" | xargs)
            # Need to be more careful with value because it can be quoted
            value=$(echo "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

	    # If parameter is one of interest and it has a value remember it
            if echo "$parameter" | egrep -q "${prefix_filter}"; then
                if [ "${value}" != "" ]; then
                    if echo "$value" | grep -q '^{.*}$'; then
                        eval "${namespace}${parameter}=\$${value}"
                    else
                        eval "${namespace}${parameter}='${value}'"
                    fi
                fi
            fi
        fi
    done < "${filename}"
}

update_copy_file() {
    src="$1"
    dest="$2"
    prefix_filter="$3"
    namespace="$4"

    # Read the file line by line
    while IFS="" read -r line || [ -n "$line" ]
    do
        if echo "$line" | egrep -q '^#'; then
            # Just copy simple comments
            echo "$line"
        elif [ ! -z "$line" ] && { [ -z "$prefix_filter" ] || [ "${line#$prefix_filter}" != "$line" ]; }; then
            # Line of interest
            # Split the line into the part before # and the part after #
            parameterAndValueAndSpace=$(echo "$line" | sed 's/^[[:space:]]*//' | cut -d'#' -f1)
            comment=$(echo "$line" | cut -s -d'#' -f2-)
            space=`printf "%s" "$parameterAndValueAndSpace" | sed 's/.*[^[:space:]]\(.*\)$/\1/'`

            if echo "$parameterAndValueAndSpace" | egrep -q "${prefix_filter}"; then
                # If parameter and value exist, substitute the value with the in memory variable of the same name
                if echo "$parameterAndValueAndSpace" | egrep -q '^\['; then
                    echo "$line"
                elif [ -n "$parameterAndValueAndSpace" ]; then
                    parameter=$(echo "$parameterAndValueAndSpace" | cut -d':' -f1)
                    value=$(echo "$parameterAndValueAndSpace" | cut -d':' -f2)
                    if [ -n "${namespace}${parameter}" ]; then
                        # If 'parameter' is set and not empty, evaluate its value
                        new_value=$(eval echo "\$${namespace}${parameter}")
                        if [ -n "${namespace}" ]; then
                            # Namespaced, use once
                            eval unset ${namespace}${parameter}
                        fi
                    elif [ -n "${parameter}" ]; then
                        # Try non-namespaced name, multi-use
                        new_value=$(eval echo "\$${parameter}")
                    else
                        # If 'parameter' is unset or empty leave as token
                        new_value="{$parameter}"
                    fi
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
        else
            # Just copy simple comments
            echo "$line"
        fi
    done < "$src" >"$dest"
}

# Set default token values to the tokens themselves to avoid being parsed out
set_default_tokens() {
    brd_type="unknown"
    for var in mmu_num_gates mmu_num_leds serial servo_up_angle servo_move_angle servo_down_angle; do
        eval "${var}='{$var}'"
    done
    for var in gear_uart_pin gear_step_pin gear_dir_pin gear_enable_pin gear_diag_pin selector_uart_pin selector_step_pin selector_dir_pin selector_enable_pin selector_diag_pin selector_endstop_pin servo_pin encoder_pin; do
        eval "PIN[unknown,${var}]='{$var}'"
    done
}


# Set default parameters from the distribution (reference) config files
read_default_config() {
    echo -e "${INFO}Reading default configuration parameters..."
    parse_file "${SRCDIR}/config/base/mmu_parameters.cfg" "" "_param_"
    parse_file "${SRCDIR}/config/base/mmu_software.cfg" "variable_"
    parse_file "${SRCDIR}/config/base/mmu_filametrix.cfg" "variable_"
    parse_file "${SRCDIR}/config/base/mmu_sequence.cfg" "variable_"
}

# Pull parameters from previous installation
read_previous_config() {
    cfg="mmu_parameters.cfg"
    dest_cfg=${KLIPPER_CONFIG_HOME}/mmu/base/${cfg}

    if [ ! -f "${dest_cfg}" ]; then
        echo -e "${WARNING}No previous ${cfg} found. Will install default"
    else
        echo -e "${INFO}Reading ${cfg} configuration from previous installation..."
        parse_file "${dest_cfg}" "" "_param_"

        # Upgrade / map / force old parameters
        if [ ! "${_param_encoder_unload_buffer}" == "" ]; then
            _param_gate_unload_buffer=${_param_encoder_unload_buffer}
        fi
        if [ ! "${_param_encoder_unload_max}" == "" ]; then
            _param_gate_homing_max=${_param_encoder_unload_max}
        fi
        if [ ! "${_param_encoder_load_retries}" == "" ]; then
            _param_gate_load_retries=${_param_encoder_load_retries}
        fi
        if [ "${_param_toolhead_ignore_load_error}" == "1" ]; then
            _param_toolhead_move_error_tolerance=100
        fi
        if [ ! "${_param_bowden_load_tolerance}" == "" ]; then
            _param_bowden_allowable_load_delta=${_param_bowden_load_tolerance}
        fi

        if [ "${_param_servo_buzz_gear_on_down}" == "" ]; then
            if [ "${_param_mmu_vendor}" == "Tradrack" ]; then
                _param_servo_buzz_gear_on_down=0
            else
                _param_servo_buzz_gear_on_down=3
            fi
        fi
        if [ "${_param_gate_parking_distance}" == "" ]; then
            if [ ! "${_param_mmu_version}" == "1.1" ]; then
                _param_gate_parking_distance=23
            else
                _param_gate_parking_distance=13
            fi
        fi
        if [ "${_param_gate_endstop_to_encoder}" == "" ]; then
            _param_gate_endstop_to_encoder=0
        fi
    fi

    cfg="mmu_filametrix.cfg"
    dest_cfg=${KLIPPER_CONFIG_HOME}/mmu/base/${cfg}

    if [ ! -f "${dest_cfg}" ]; then
        echo -e "${WARNING}No previous ${cfg} found. Will install default"
    else
        echo -e "${INFO}Reading ${cfg} configuration from previous installation..."
        parse_file "${dest_cfg}" "variable_"

        # Convert from mm/min to mm/sec and 'spd' to 'speed' for consistency in other macros
        #
        if [ ! "${variable_rip_speed}" == "" ]; then
            variable_rip_speed=$(echo "$variable_rip_speed" | awk '{if ($1 > 250) print int($1 / 60); else print $1}')
        fi
        if [ ! "${variable_evacuate_speed}" == "" ]; then
            variable_evacuate_speed=$(echo "$variable_evacuate_speed" | awk '{if ($1 > 250) print int($1 / 60); else print $1}')
        fi
        if [ ! "${variable_extruder_move_speed}" == "" ]; then
            variable_extruder_move_speed=$(echo "$variable_extruder_move_speed" | awk '{if ($1 > 250) print int($1 / 60); else print $1}')
        fi
        if [ ! "${variable_travel_spd}" == "" ]; then
            variable_travel_speed=$(echo "$variable_travel_spd" | awk '{if ($1 > 250) print int($1 / 60); else print $1}')
        fi
        if [ ! "${variable_cut_fast_move_spd}" == "" ]; then
            variable_cut_fast_move_speed=$(echo "$variable_cut_fast_move_spd" | awk '{if ($1 > 250) print int($1 / 60); else print $1}')
        fi
        if [ ! "${variable_cut_slow_move_spd}" == "" ]; then
            variable_cut_slow_move_speed=$(echo "$variable_cut_slow_move_spd" | awk '{if ($1 > 250) print int($1 / 60); else print $1}')
        fi
    fi

    for cfg in mmu_software.cfg mmu_sequence.cfg ; do
        dest_cfg=${KLIPPER_CONFIG_HOME}/mmu/base/${cfg}

        if [ ! -f "${dest_cfg}" ]; then
            echo -e "${WARNING}No previous ${cfg} found. Will install default"
        else
            echo -e "${INFO}Reading ${cfg} configuration from previous installation..."
            parse_file "${dest_cfg}" "variable_"
        fi
    done

    if [ ! "${mmu_num_gate}" == "{mmu_num_gate}" -a ! "${mmu_num_gate}" == "" ] 2>/dev/null; then
        mmu_num_leds=$(expr $mmu_num_gates + 1)
    fi
}

copy_config_files() {
    mmu_dir="${KLIPPER_CONFIG_HOME}/mmu"
    next_mmu_dir="$(nextfilename "${mmu_dir}")"

    echo -e "${INFO}Copying configuration files into ${mmu_dir} directory..."
    if [ ! -d "${mmu_dir}" ]; then
        mkdir ${mmu_dir}
        mkdir ${mmu_dir}/base
        mkdir ${mmu_dir}/optional
    else
        echo -e "${DETAIL}Config directory ${mmu_dir} already exists - backing up old config files to ${next_mmu_dir}"
        mkdir ${next_mmu_dir}
        (cd "${mmu_dir}"; cp -r * "${next_mmu_dir}")
    fi

    for file in `cd ${SRCDIR}/config/base ; ls *.cfg`; do
        src=${SRCDIR}/config/base/${file}
        dest=${mmu_dir}/base/${file}
        next_dest=${next_mmu_dir}/base/${file}

        if [ -f "${dest}" ]; then
            if [ "${file}" == "mmu_hardware.cfg" -a "${INSTALL}" -eq 0 ] || [ "${file}" == "mmu.cfg"  -a "${INSTALL}" -eq 0 ]; then
                echo -e "${WARNING}Skipping copy of hardware config file ${file} because already exists"
                continue
            else
                echo -e "${INFO}Installing/Upgrading configuration file ${file}"
                mv ${dest} ${next_dest}
            fi
        fi

        # Hardware files: Special token substitution -----------------------------------------
	if [ "${file}" == "mmu.cfg" -o "${file}" == "mmu_hardware.cfg" ]; then
            cp ${src} ${dest}

            # Correct shared uart_address for EASY-BRD
            if [ "${brd_type}" == "EASY-BRD" ]; then
                # Share uart_pin to avoid duplicate alias problem
                cat ${dest} | sed -e "\
                    /^uart_pin: mmu:MMU_SEL_UART/uart_pin: mmu:MMU_GEAR_UART/; \
                        " > ${dest}.tmp && mv ${dest}.tmp ${dest}
            else:
                # Remove uart_address lines
                cat ${dest} | sed -e "\
                    /^uart_address:/ d; \
                        " > ${dest}.tmp && mv ${dest}.tmp ${dest}
            fi

            if [ "${SETUP_SELECTOR_TOUCH}" -eq 1 ]; then
                cat ${dest} | sed -e "\
                    s/^#\(diag_pin: \^mmu:MMU_SEL_DIAG\)/\1/; \
                    s/^#\(driver_SGTHRS: 75\)/\1/; \
		    s/^#\(extra_endstop_pins: tmc2209_stepper_mmu_selector:virtual_endstop\)/\1/; \
		    s/^#\(extra_endstop_names: mmu_sel_touch\)/\1/; \
                    s/^uart_address:/${uart_comment}uart_address:/; \
                        " > ${dest}.tmp && mv ${dest}.tmp ${dest}
            fi

            # Now substitute tokens given brd_type
            cat ${dest} | sed -e "\
                s/{brd_type}/${brd_type}/; \
                s%{serial}%${serial}%; \
                s/{mmu_num_gates}/${mmu_num_gates}/; \
                s/{mmu_num_leds}/${mmu_num_leds}/; \
                s/{gear_gear_ratio}/${gear_gear_ratio}/; \
                s/{gear_run_current}/${gear_run_current}/; \
                s/{gear_hold_current}/${gear_hold_current}/; \
                s/{sel_run_current}/${sel_run_current}/; \
                s/{sel_hold_current}/${sel_hold_current}/; \
                s/{maximum_servo_angle}/${maximum_servo_angle}/; \
                s/{minimum_pulse_width}/${minimum_pulse_width}/; \
                s/{maximum_pulse_width}/${maximum_pulse_width}/; \
                s/{toolhead_sensor_pin}/${PIN[toolhead_sensor_pin]}/; \
                s/{extruder_sensor_pin}/${PIN[extruder_sensor_pin]}/; \
                s/{gantry_servo_pin}/${PIN[gantry_servo_pin]}/; \
                s/{gate_sensor_pin}/${PIN[$brd_type,gate_sensor_pin]}/; \
                s/{pre_gate_0_pin}/${PIN[$brd_type,pre_gate_0_pin]}/; \
                s/{pre_gate_1_pin}/${PIN[$brd_type,pre_gate_1_pin]}/; \
                s/{pre_gate_2_pin}/${PIN[$brd_type,pre_gate_2_pin]}/; \
                s/{pre_gate_3_pin}/${PIN[$brd_type,pre_gate_3_pin]}/; \
                s/{pre_gate_4_pin}/${PIN[$brd_type,pre_gate_4_pin]}/; \
                s/{pre_gate_5_pin}/${PIN[$brd_type,pre_gate_5_pin]}/; \
                s/{pre_gate_6_pin}/${PIN[$brd_type,pre_gate_6_pin]}/; \
                s/{pre_gate_7_pin}/${PIN[$brd_type,pre_gate_7_pin]}/; \
                s/{pre_gate_8_pin}/${PIN[$brd_type,pre_gate_8_pin]}/; \
                s/{pre_gate_9_pin}/${PIN[$brd_type,pre_gate_9_pin]}/; \
                s/{pre_gate_10_pin}/${PIN[$brd_type,pre_gate_10_pin]}/; \
                s/{pre_gate_11_pin}/${PIN[$brd_type,pre_gate_11_pin]}/; \
                s/{gear_gear_ratio}/${gear_gear_ratio}/; \
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
                s/{neopixel_pin}/${PIN[$brd_type,neopixel_pin]}/; \
                    " > ${dest}.tmp && mv ${dest}.tmp ${dest}

            # Handle LED option - Comment out if disabled
	    if [ "${file}" == "mmu_hardware.cfg" -a "$SETUP_LED" -eq 0 ]; then
                sed "/${LED_SECTION}/,\$s/^/#/" ${dest} > ${dest}.tmp && mv ${dest}.tmp ${dest}
            fi

        # Conifguration parameters -----------------------------------------------------------
        elif [ "${file}" == "mmu_parameters.cfg" ]; then
            update_copy_file "$src" "$dest" "" "_param_"

            # Ensure that supplemental user added params are retained. These are those that are
            # by default set internally in Happy Hare based on vendor and version settings but
            # can be overridden.  This set also includes a couple of hidden test parameters.
            supplemental_params="cad_gate0_pos cad_gate_width cad_bypass_offset cad_last_gate_offset cad_block_width cad_bypass_block_width cad_bypass_block_delta gate_parking_distance encoder_default_resolution"
            hidden_params="virtual_selector homing_extruder test_random_failures"
            for var in $(set | grep '^_param_' | cut -d'=' -f1); do
                param=${var#_param_}
                for item in ${supplemental_params} ${hidden_params}; do
                    if [ "$item" = "$param" ]; then
                        value=$(eval echo "\$${var}")
                        echo "${param}: ${value} # User added and retained after upgrade"
                    fi
                done
            done >> $dest

        # Software macros --------------------------------------------------------------------
        elif [ "${file}" == "mmu_software.cfg" ]; then
            tx_macros=""
            if [ "${mmu_num_gates}" == "{mmu_num_gates}" ]; then
                mmu_num_gates=12
	    fi 
            for (( i=0; i<=$(expr $mmu_num_gates - 1); i++ ))
            do
                tx_macros+="[gcode_macro T${i}]\n"
                tx_macros+="gcode: MMU_CHANGE_TOOL TOOL=${i}\n"
            done

            if [ "${INSTALL}" -eq 1 ]; then
                cat ${src} | sed -e "\
                    s%{klipper_config_home}%${KLIPPER_CONFIG_HOME}%g; \
                    s%{tx_macros}%${tx_macros}%g; \
                    s%{led_enable}%${SETUP_LED}%g; \
                        " > ${dest}
            else
                cat ${src} | sed -e "\
                    s%{klipper_config_home}%${KLIPPER_CONFIG_HOME}%g; \
                    s%{tx_macros}%${tx_macros}%g; \
                    s%{led_enable}%${SETUP_LED}%g; \
                        " > ${dest}.tmp
                update_copy_file "${dest}.tmp" "${dest}" "variable_" && rm ${dest}.tmp
            fi

        elif [ "${file}" == "mmu_filametrix.cfg" ]; then
            if [ "${INSTALL}" -eq 1 ]; then
                cat ${src} > ${dest}
            else
                cat ${src} > ${dest}.tmp
                update_copy_file "${dest}.tmp" "${dest}" "variable_" && rm ${dest}.tmp
            fi

        else
            cp ${src} ${dest}
	fi
    done

    for file in `cd ${SRCDIR}/config/optional ; ls *.cfg`; do
        src=${SRCDIR}/config/optional/${file}
        dest=${KLIPPER_CONFIG_HOME}/mmu/optional/${file}
        cp ${src} ${dest}
    done

    src=${SRCDIR}/config/mmu_vars.cfg
    dest=${KLIPPER_CONFIG_HOME}/mmu/mmu_vars.cfg
    if [ -f "${dest}" ]; then
        echo -e "${WARNING}Skipping copy of mmu_vars.cfg file because already exists"
    else
        cp ${src} ${dest}
    fi
}

uninstall_config_files() {
    if [ -d "${KLIPPER_CONFIG_HOME}/mmu" ]; then
        echo -e "${INFO}Removing MMU configuration files from ${KLIPPER_CONFIG_HOME}"
        mv "${KLIPPER_CONFIG_HOME}/mmu" /tmp/mmu.uninstalled
    fi
}

install_printer_includes() {
    # Link in all includes if not already present
    dest=${KLIPPER_CONFIG_HOME}/printer.cfg
    if test -f $dest; then

        klippain_included=$(grep -c "[include config/hardware/mmu.cfg]" ${dest} || true)
        if [ "${klippain_included}" -eq 1 ]; then
            echo -e "${WARNING}This looks like a Klippain config installation - skipping automatic config install. Please add config includes by hand"
        else
            next_dest="$(nextfilename "$dest")"
            echo -e "${INFO}Copying original printer.cfg file to ${next_dest}"
            cp ${dest} ${next_dest}
            if [ ${MENU_12864} -eq 1 ]; then
                i='\[include mmu/optional/mmu_menu.cfg\]'
                already_included=$(grep -c "${i}" ${dest} || true)
                if [ "${already_included}" -eq 0 ]; then
                    sed -i "1i ${i}" ${dest}
                fi
            fi
            if [ ${ERCF_COMPAT} -eq 1 ]; then
                i='\[include mmu/optional/mmu_ercf_compat.cfg\]'
                already_included=$(grep -c "${i}" ${dest} || true)
                if [ "${already_included}" -eq 0 ]; then
                    sed -i "1i ${i}" ${dest}
                fi
            fi
            if [ ${CLIENT_MACROS} -eq 1 ]; then
                i='\[include mmu/optional/client_macros.cfg\]'
                already_included=$(grep -c "${i}" ${dest} || true)
                if [ "${already_included}" -eq 0 ]; then
                    sed -i "1i ${i}" ${dest}
                fi
            fi
            for i in \
                    '\[include mmu/base/\*.cfg\]' ; do
                already_included=$(grep -c "${i}" ${dest} || true)
                if [ "${already_included}" -eq 0 ]; then
                    sed -i "1i ${i}" ${dest}
                fi
            done
        fi
    else
        echo -e "${WARNING}File printer.cfg file not found! Cannot include MMU configuration files"
    fi
}

uninstall_printer_includes() {
    echo -e "${INFO}Cleaning MMU references from printer.cfg"
    dest=${KLIPPER_CONFIG_HOME}/printer.cfg
    if test -f $dest; then
        next_dest="$(nextfilename "$dest")"
        echo -e "${INFO}Copying original printer.cfg file to ${next_dest} before cleaning"
        cp ${dest} ${next_dest}
        cat "${dest}" | sed -e " \
            /\[include mmu\/optional\/client_macros.cfg\]/ d; \
            /\[include mmu\/optional\/mmu_menu.cfg\]/ d; \
            /\[include mmu\/optional\/mmu_ercf_compat.cfg\]/ d; \
            /\[include mmu\/mmu_software.cfg\]/ d; \
            /\[include mmu\/mmu_parameters.cfg\]/ d; \
            /\[include mmu\/mmu_hardware.cfg\]/ d; \
            /\[include mmu\/mmu_filametrix.cfg\]/ d; \
            /\[include mmu\/mmu.cfg\]/ d; \
            /\[include mmu\/base\/\*.cfg\]/ d; \
	        " > "${dest}.tmp" && mv "${dest}.tmp" "${dest}"
    fi
}

install_update_manager() {
    echo -e "${INFO}Adding update manager to moonraker.conf"
    file="${KLIPPER_CONFIG_HOME}/moonraker.conf"
    if [ -f "${file}" ]; then
        restart=0

        update_section=$(grep -c '\[update_manager happy-hare\]' ${file} || true)
        if [ "${update_section}" -eq 0 ]; then
            echo "" >> "${file}"
            while read -r line; do
                echo -e "${line}" >> "${file}"
            done < "${SRCDIR}/moonraker_update.txt"
            echo "" >> "${file}"
            restart=1
        else
            echo -e "${WARNING}[update_manager happy-hare] already exists in moonraker.conf - skipping install"
        fi

        # Quick "catch-up" update for new mmu_service
        enable_preprocessor="True"
        update_section=$(grep -c '\[mmu_server\]' ${file} || true)
        if [ "${update_section}" -eq 0 ]; then
            echo "" >> "${file}"
            echo "[mmu_server]" >> "${file}"
            echo "enable_file_preprocessor: ${enable_preprocessor}" >> "${file}"
            echo "" >> "${file}"
            restart=1
        else
            echo -e "${WARNING}[mmu_server] already exists in moonraker.conf - skipping install"
        fi

        if [ "$restart" -eq 1 ]; then
            restart_moonraker
        fi
    else
        echo -e "${WARNING}moonraker.conf not found!"
    fi
}

uninstall_update_manager() {
    echo -e "${INFO}Removing update manager from moonraker.conf"
    file="${KLIPPER_CONFIG_HOME}/moonraker.conf"
    if [ -f "${file}" ]; then
        restart=0

        update_section=$(grep -c '\[update_manager happy-hare\]' ${file} || true)
        if [ "${update_section}" -eq 0 ]; then
            echo -e "${INFO}[update_manager happy-hare] not found in moonraker.conf - skipping removal"
        else
            cat "${file}" | sed -e " \
                /\[update_manager happy-hare\]/,+6 d; \
                    " > "${file}.new" && mv "${file}.new" "${file}"
            restart=1
        fi

        update_section=$(grep -c '\[mmu_server\]' ${file} || true)
        if [ "${update_section}" -eq 0 ]; then
            echo -e "${INFO}[mmu_server] not found in moonraker.conf - skipping removal"
        else
            cat "${file}" | sed -e " \
                /\[mmu_server\]/,+1 d; \
                    " > "${file}.new" && mv "${file}.new" "${file}"
            restart=1
        fi

        if [ "$restart" -eq 1 ]; then
            restart_moonraker
        fi
    else
        echo -e "${WARNING}moonraker.conf not found!"
    fi
}

restart_klipper() {
    if [ "$NOSERVICE" -ne 1 ]; then
        echo -e "${INFO}Restarting Klipper..."
        sudo systemctl restart klipper
    else
        echo -e "${WARNING}Klipper restart suppressed - Please restart by hand"
    fi
}

restart_moonraker() {
    if [ "$NOSERVICE" -ne 1 ]; then
        echo -e "${INFO}Restarting Moonraker..."
        sudo systemctl restart moonraker
    else
        echo -e "${WARNING}Moonraker restart suppressed - Please restart by hand"
    fi
}

prompt_yn() {
    while true; do
        read -n1 -p "$@ (y/n)? " yn
        case "${yn}" in
            Y|y)
                echo "y" 
                break;;
            N|n)
                echo "n" 
                break;;
            *)
                ;;
        esac
    done
}

prompt_123() {
    prompt=$1
    max=$2
    while true; do
        read -p "${prompt} (1-${max})? " -n 1 number
        if [[ "$number" =~ [1-${max}] ]]; then
            echo ${number}
            break
        fi
    done
}

questionaire() {
    # Set default substitution tokens
    mmu_vendor="Other"
    mmu_version="1.0"
    extruder_homing_endstop="collision"
    gate_homing_endstop="encoder"
    gate_parking_distance=23.0
    gate_endstop_to_encoder=0
    servo_buzz_gear_on_down=0

    # mmu_hardware.cfg only...
    gear_gear_ratio="80:20"
    gear_run_current=0.5
    gear_hold_current=0.1
    sel_run_current=0.4
    sel_hold_current=0.2
    maximum_servo_angle=180
    minimum_pulse_width=0.00075
    maximum_pulse_width=0.00225

    echo
    echo -e "${INFO}Let me see if I can get you started with initial configuration"
    echo -e "You will still have some manual editing to perform but I will explain that later"
    echo -e "(Note that all this script does is set a lot of the time consuming parameters in the config"
    echo
    echo -e "${PROMPT}${SECTION}What type of MMU are you running?${INPUT}"
    echo -e "1) ERCF v1.1 (inc TripleDecky, Springy, Binky mods)"
    echo -e "2) ERCF v2.0 (inc ThumperBlocks mod)"
    echo -e "3) Tradrack v1.0"
    echo -e "4) Other (or just want starter config files)"
    num=$(prompt_123 "MMU Type?" 4)
    echo
    case $num in
        1)
            HAS_ENCODER=yes
            mmu_vendor="ERCF"
            mmu_version="1.1"
            servo_buzz_gear_on_down=3

            maximum_servo_angle=180
            minimum_pulse_width=0.00085
            maximum_pulse_width=0.00215
            echo
            echo -e "${PROMPT}Some popular upgrade options for ERCF v1.1 can automatically be setup. Let me ask you about them...${INPUT}"
            yn=$(prompt_yn "Are you using the 'Springy' sprung servo selector cart")
            echo
            case $yn in
            y)
                mmu_version+="s"
                ;;
            esac
            yn=$(prompt_yn "Are you using the improved 'Binky' encoder")
            echo
            case $yn in
            y)
                mmu_version+="b"
                ;;
            esac
            yn=$(prompt_yn "Are you using the wider 'Triple-Decky' filament blocks")
            echo
            case $yn in
            y)
                mmu_version+="t"
                ;;
            esac
            ;;
        2)
            HAS_ENCODER=yes
            mmu_vendor="ERCF"
            mmu_version="2.0"
            gate_parking_distance=13.0
            servo_buzz_gear_on_down=3

            maximum_servo_angle=180
            minimum_pulse_width=0.00085
            maximum_pulse_width=0.00215
            echo
            echo -e "${PROMPT}Some popular upgrade options for ERCF v2.0 can automatically be setup. Let me ask you about them...${INPUT}"
            yn=$(prompt_yn "Are you using 'ThumperBlocks' filament block option")
            echo
            case $yn in
            y)
                mmu_version+="h"
                gate_parking_distance=11.0
                ;;
            esac
            ;;
        3)
            HAS_ENCODER=no
            mmu_vendor="Tradrack"
            mmu_version="1.0"
            extruder_homing_endstop="none"
            gate_homing_endstop="mmu_gate"
            gate_parking_distance=17.0
            servo_buzz_gear_on_down=0

            gear_gear_ratio="50:17"
            gear_run_current=1.27
            gear_hold_current=0.2
            sel_run_current=0.63
            sel_hold_current=0.2
            maximum_servo_angle=131
            minimum_pulse_width=0.00070
            maximum_pulse_width=0.00220
            echo -e "${PROMPT}Some popular upgrade options for Tradrack v1.0 can automatically be setup. Let me ask you about them...${INPUT}"
            yn=$(prompt_yn "Are you using the 'Binky' encoder modification")
            echo
            case $yn in
            y)
                HAS_ENCODER=yes
                mmu_version+="e"
                extruder_homing_endstop="collision"
                gate_homing_endstop="encoder"
                gate_parking_distance=48.0
                gate_endstop_to_encoder=31.0
                ;;
            esac
            ;;
        4)
            HAS_ENCODER=yes
            echo
            echo -e "${WARNING}    IMPORTANT: Since you have a custom MMU you will need to setup some CAD dimensions and other key parameters... See doc"
            ;;
    esac

    mmu_num_gates=8
    echo
    echo -e "${PROMPT}${SECTION}How many gates (selectors) do you have?${INPUT}"
    while true; do
        read -p "Number of gates? " mmu_num_gates
        if ! [ "${mmu_num_gates}" -ge 1 ] 2> /dev/null ;then
            echo -e "${INFO}Positive integer value only"
      else
           break
       fi
    done
    mmu_num_leds=$(expr $mmu_num_gates + 1)

    brd_type="unknown"
    echo
    echo -e "${PROMPT}${SECTION}Select mcu board type used to control MMU${INPUT}"
    echo -e " 1) BTT MMB"
    echo -e " 2) Fysetc Burrows ERB"
    echo -e " 3) Standard EASY-BRD (with SAMD21)"
    echo -e " 4) EASY-BRD with RP2040"
    echo -e " 5) Mellow EASY-BRD with CANbus"
    echo -e " 6) Not in list / Unknown"
    num=$(prompt_123 "MCU type?" 6)
    echo
    case $num in
        1)
            brd_type="MMB"
            pattern="Klipper_stm32"
            ;;
        2)
            brd_type="ERB"
            pattern="Klipper_rp2040"
            ;;
        3)
            brd_type="EASY-BRD"
            pattern="Klipper_samd21"
            ;;
        4)
            brd_type="EASY-BRD-RP2040"
            pattern="Klipper_rp2040"
            ;;
        5)
            brd_type="MELLOW-EASY-BRD-CAN"
            pattern="Klipper_rp2040"
            ;;
        6)
            brd_type="unknown"
            pattern="Klipper_"
            ;;
    esac

    serial=""
    echo
    for line in `ls /dev/serial/by-id 2>/dev/null | egrep "Klipper_"`; do
        if echo ${line} | grep --quiet "${pattern}"; then
            echo -e "${PROMPT}${SECTION}This looks like your ${EMPHASIZE}${brd_type}${PROMPT} controller serial port. Is that correct?${INPUT}"
            yn=$(prompt_yn "/dev/serial/by-id/${line}")
            echo
            case $yn in
                y)
                    serial="/dev/serial/by-id/${line}"
                    break
                    ;;
                n)
                    ;;
            esac
        fi
    done
    if [ "${serial}" == "" ]; then
        echo
        echo -e "${WARNING}    Couldn't find your serial port, but no worries - I'll configure the default and you can manually change later"
        serial='/dev/ttyACM1 # Config guess. Run ls -l /dev/serial/by-id and set manually'
    fi

    echo
    echo -e "${PROMPT}${SECTION}Touch selector operation using TMC Stallguard? This allows for additional selector recovery steps but is difficult to tune"
    echo -e "Not recommend if you are new to MMU/Happy Hare & MCU must have DIAG output for steppers. Can configure later${INPUT}"
    yn=$(prompt_yn "Enable selector touch operation")
    echo
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

    echo
    echo -e "${PROMPT}${SECTION}Would you like to have neopixel LEDs setup now for your MMU?${INPUT}"
    yn=$(prompt_yn "Enable LED support?")
    echo
    case $yn in
        y)
            SETUP_LED=1
            ;;
        n)
            SETUP_LED=0
            ;;
    esac

    if [ "${mmu_vendor}" == "ERCF" ]; then
        echo
        echo -e "${PROMPT}${SECTION}Which servo are you using?"
        echo -e "1) MG-90S (ERCF)"
        echo -e "2) Savox SH0255MG (ERCF)"
        echo -e "3) Not listed / Other${INPUT}"
        num=$(prompt_123 "Servo?" 3)
        echo
        case $num in
            1)
                servo_up_angle=30
                if [ "${mmu_version}" == "2.0" ]; then
                    servo_move_angle=61
                else
                    servo_move_angle=${servo_up_angle}
                fi
                servo_down_angle=140
                ;;
            2)
                servo_up_angle=140
                if [ "${mmu_version}" == "2.0" ]; then
                    servo_move_angle=109
                else
                    servo_move_angle=${servo_up_angle}
                fi
                servo_down_angle=30
                ;;
        esac
    elif [ "${mmu_vendor}" == "Tradrack" ]; then
        echo
        echo -e "${PROMPT}${SECTION}Which servo are you using?"
        echo -e "1) PS-1171MG or FT1117M (Tradrack)"
        echo -e "2) Not listed / Other${INPUT}"
        num=$(prompt_123 "Servo?" 2)
        echo
        case $num in
            1)
                servo_up_angle=145
                servo_move_angle=${servo_up_angle}
                servo_down_angle=1
                ;;
        esac
    fi

    if [ "${mmu_vendor}" == "ERCF" -o "${HAS_ENCODER}" == "yes" ]; then
        echo
        echo -e "${PROMPT}${SECTION}Clog detection? This uses the MMU encoder movement to detect clogs and can call your filament runout logic${INPUT}"
        yn=$(prompt_yn "Enable clog detection")
        echo
        case $yn in
            y)
                enable_clog_detection=1
                echo -e "${PROMPT}    Would you like MMU to automatically adjust clog detection length (recommended)?${INPUT}"
                yn=$(prompt_yn "    Automatic")
                echo
                if [ "${yn}" == "y" ]; then
                    enable_clog_detection=2
                fi
                ;;
            n)
                enable_clog_detection=0
                ;;
        esac
    fi

    echo
    echo -e "${PROMPT}${SECTION}EndlessSpool? This uses filament runout detection to automate switching to new spool without interruption${INPUT}"
    yn=$(prompt_yn "Enable EndlessSpool")
    echo
    case $yn in
        y)
            enable_endless_spool=1
            ;;
        n)
            enable_endless_spool=0
            ;;
    esac

    echo
    MENU_12864=0
    ERCF_COMPAT=0
    echo -e "${PROMPT}${SECTION}Finally, would you like me to include all the MMU config files into your printer.cfg file${INPUT}"
    yn=$(prompt_yn "Add include?")
    echo
    case $yn in
        y)
            INSTALL_PRINTER_INCLUDES=1
            echo -e "${PROMPT}    Would you like to include Mini 12864 menu configuration extension for MMU${INPUT}"
            yn=$(prompt_yn "    Include menu")
            echo
            case $yn in
                y)
                    MENU_12864=1
                    ;;
                n)
                    MENU_12864=0
                    ;;
            esac

            echo -e "${PROMPT}    Would you like to include subset of the legacy ERCF_ command set compatibility module${INPUT}"
            yn=$(prompt_yn "    Include legacy ERCF command set (not recommended)")
            echo
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
            echo
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

    echo -e "${WARNING}"
    echo -e "mmu_vendor: ${mmu_vendor}"
    echo -e "mmu_version: ${mmu_version}"
    echo -e "mmu_num_gates: ${mmu_num_gates}"
    echo -e "servo_up_angle: ${servo_up_angle}"
    echo -e "servo_move_angle: ${servo_move_angle}"
    echo -e "servo_down_angle: ${servo_down_angle}"
    echo -e "enable_clog_detection: ${enable_clog_detection}"
    echo -e "enable_endless_spool: ${enable_endless_spool}"

    echo
    echo -e "${INFO}"
    echo "    vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv"
    echo
    echo "    NOTES:"
    echo "     What still needs to be done:"
    if [ "${brd_type}" == "unknown" ]; then
        echo "         * Edit *.cfg files and substitute all missing pins"
    else
        echo "         * Review all pin configuration and change to match your mcu"
    fi
    echo "         * Verify motor current, especially if using non BOM motors"
    echo "         * Adjust motor direction with '!' on pin if necessary. No way to know here"
    echo "         * Adjust your config for loading and unloading preferences"
    echo 
    echo "    Later:"
    echo "         * Tweak configurations like speed and distance in mmu/base/mmu_parameter.cfg"
    echo 
    echo "    Good luck! MMU is complex to setup. Remember Discord is your friend.."
    echo
    echo "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^"
    echo
}

usage() {
    echo -e "${EMPHASIZE}"
    echo "Usage: $0 [-k <klipper_home_dir>] [-c <klipper_config_dir>] [-m <moonraker_home_dir>] [-i] [-u]"
    echo
    echo "-i for interactive install"
    echo "-d for uninstall"
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
SETUP_LED=0

INSTALL=0
UNINSTALL=0
NOSERVICE=0
INSTALL_KLIPPER_SCREEN_ONLY=0
while getopts "k:c:m:ids" arg; do
    case $arg in
        k) KLIPPER_HOME=${OPTARG};;
        m) MOONRAKER_HOME=${OPTARG};;
        c) KLIPPER_CONFIG_HOME=${OPTARG};;
        i) INSTALL=1;;
        d) UNINSTALL=1;;
        s) NOSERVICE=1;;
        *) usage;;
    esac
done

if [ "${INSTALL}" -eq 1 -a "${UNINSTALL}" -eq 1 ]; then
    echo -e "${ERROR}Can't install and uninstall at the same time!"
    usage
fi

if [ "${INSTALL}" -eq 0 -a "${UNINSTALL}" -eq 0 ]; then
    echo -e "${TITLE}Upgrading previous version of Happy Hare..."
fi

verify_not_root
verify_home_dirs
check_klipper
cleanup_old_ercf
if [ "$UNINSTALL" -eq 0 ]; then

    # Set in memory parameters from default file
    set_default_tokens

    if [ "${INSTALL}" -eq 1 ]; then
        # Update in memory parameters from questionaire
        questionaire
        read_default_config

        if [ "${INSTALL_PRINTER_INCLUDES}" -eq 1 ]; then
            install_printer_includes
        fi
    else
        read_default_config
        read_previous_config # Update in memory parameters from previous install
    fi

    # Important to overwrite this
    _param_happy_hare_version=${VERSION}

    copy_config_files

    # Temp upgrades
    cleanup_manual_stepper_version
    upgrade_mmu_sensors
    upgrade_led_effects

    link_mmu_plugins
    install_update_manager

else
    echo
    echo -e "${WARNING}You have asked me to remove Happy Hare and cleanup"
    echo
    yn=$(prompt_yn "Are you sure you want to proceed with deleting Happy Hare?")
    echo
    case $yn in
        y)
            unlink_mmu_plugins
            uninstall_update_manager
            uninstall_printer_includes
            uninstall_config_files
            echo -e "${INFO}Uninstall complete except for the Happy-Hare directory - you can now safely delete that as well"
            ;;
        n)
            echo -e "${INFO}Well that was a close call!  Everything still intact"
            echo
	    exit 0
            ;;
    esac
fi

if [ "$INSTALL" -eq 0 ]; then
    restart_klipper
else
    echo -e "${WARNING}Klipper not restarted automatically because you need to validate and complete config"
fi

if [ "$UNINSTALL" -eq 0 ]; then
    echo -e "${TITLE}Done."
    echo -e "${INFO}"
    echo '(\_/)'
    echo '( *,*)'
    echo '(")_(") Happy Hare Ready'
    echo
else
    echo -e "${EMPHASIZE}"
    echo "Done.  Sad to see you go (but maybe you'll be back)..."
    echo -e "${INFO}"
    echo '(\_/)'
    echo '( v,v)'
    echo '(")^(") Very Unappy Hare'
    echo
fi
