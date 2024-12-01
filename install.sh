#!/bin/bash
# Happy Hare MMU Software
#
# Installer / Updater script
#
# Copyright (C) 2022  moggieuk#6538 (discord) moggieuk@hotmail.com
#
# Creality K1 Support
#               2024  hamyy <oudy_1999@hotmail.com>
#               2024  Unsweeticetea <iamzevle@gmail.com>
#               2024  Dmitry Kychanov <k1-801@mail.ru>
#
VERSION=3.01 # Important: Keep synced with mmy.py

F_VERSION=$(echo "$VERSION" | sed 's/\([0-9]\+\)\.\([0-9]\)\([0-9]\)/\1.\2.\3/')
SCRIPT="$(readlink -f "$0")"
SCRIPTFILE="$(basename "$SCRIPT")"
SCRIPTPATH="$(dirname "$SCRIPT")"
SCRIPTNAME="$0"
ARGS=( "$@" )

# Creality K1 series printers run on MIPS, with a limited instruction set and different default klipper directories
# Checking for machine type is the easiest way so far to spot them (will be set to 1 if on MIPS):
IS_MIPS=0
if [ $(uname -m) = "mips" ]; then
    IS_MIPS=1
fi

KLIPPER_HOME="${HOME}/klipper"
MOONRAKER_HOME="${HOME}/moonraker"
KLIPPER_CONFIG_HOME="${HOME}/printer_data/config"
OCTOPRINT_KLIPPER_CONFIG_HOME="${HOME}"
KLIPPER_LOGS_HOME="${HOME}/printer_data/logs"
OLD_KLIPPER_CONFIG_HOME="${HOME}/klipper_config"

if [ "$IS_MIPS" -eq 1 ]; then
    KLIPPER_HOME="/usr/share/klipper"
    MOONRAKER_HOME="/usr/data/moonraker/moonraker"
    KLIPPER_CONFIG_HOME="/usr/data/printer_data/config"
    unset OCTOPRINT_KLIPPER_CONFIG_HOME
    unset OLD_KLIPPER_CONFIG_HOME
fi

clear
set -e # Exit immediately on error

declare -A PIN 2>/dev/null || {
    echo "Please run this script with bash $0"
    exit 1
}

# Source pin defs for common MCU's
source ${SCRIPTPATH}/pin_defs

# These pins will usually be on main mcu for wiring simplification
#
_hw_toolhead_sensor_pin=""
_hw_extruder_sensor_pin=""
_hw_gantry_servo_pin=""
_hw_sync_feedback_tension_pin=""
_hw_sync_feedback_compression_pin=""

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
DIM="${PURPLE}"
INPUT="${OFF}"
SECTION="----------------\n"

get_logo() {
    caption=$1
    logo=$(cat <<EOF
${INFO}
(\_/)
( *,*)
(")_(") ${caption}
${OFF}
EOF
    )
    echo -e "$logo"
}

sad_logo=$(cat <<EOF
${INFO}
(\_/)
( v,v)
(")^(") Very Unhappy Hare
${OFF}
EOF
)

self_update() {
    [ "$UPDATE_GUARD" ] && return
    export UPDATE_GUARD=YES
    clear

    cd "$SCRIPTPATH"

    set +e
    # timeout is unavailable on MIPS
    if [ "$IS_MIPS" -ne 1 ]; then
        BRANCH=$(timeout 3s git branch --show-current)
    else
        BRANCH=$(git branch --show-current)
    fi

    if [ $? -ne 0 ]; then
        echo -e "${ERROR}Error updating from github"
        echo -e "${ERROR}You might have an old version of git"
        echo -e "${ERROR}Skipping automatic update..." 
        set -e
        return
    fi
    set -e

    [ -z "${BRANCH}" ] && {
        echo -e "${ERROR}Timeout talking to github. Skipping upgrade check"
        return
    }
    echo -e "${B_GREEN}Running on '${BRANCH}' branch"

    # Both check for updates but also help me not loose changes accidently
    echo -e "${B_GREEN}Checking for updates..."
    git fetch --quiet

    set +e
    git diff --quiet --exit-code "origin/$BRANCH"
    if [ $? -eq 1 ]; then
        echo -e "${B_GREEN}Found a new version of Happy Hare on github, updating..."
        [ -n "$(git status --porcelain)" ] && {
            git stash push -m 'local changes stashed before self update' --quiet
        }
        RESTART=1
    fi
    set -e

    if [ -n "${N_BRANCH}" -a "${BRANCH}" != "${N_BRANCH}" ]; then
        BRANCH=${N_BRANCH}
        echo -e "${B_GREEN}Switching to '${BRANCH}' branch"
        RESTART=1
    fi

    if [ -n "${RESTART}" ]; then
        git checkout $BRANCH --quiet
        git pull --quiet --force
        GIT_VER=$(git describe --tags)
        echo -e "${B_GREEN}Now on git version ${GIT_VER}"
        echo -e "${B_GREEN}Running the new install script..."
        cd - >/dev/null
        exec "$SCRIPTNAME" "${ARGS[@]}"
        exit 0 # Exit this old instance
    fi
    GIT_VER=$(git describe --tags)
    echo -e "${B_GREEN}Already the latest version: ${GIT_VER}"
}

function nextfilename {
    local name="$1"
    if [ -d "${name}" ]; then
        printf "%s-%s" ${name} $(date '+%Y%m%d_%H%M%S')
    else
        printf "%s-%s.%s-old" ${name%.*} $(date '+%Y%m%d_%H%M%S') ${name##*.}
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
    if [ "$IS_MIPS" -ne 1 ]; then
        if [ "$EUID" -eq 0 ]; then
            echo -e "${ERROR}This script must not run as root"
            exit -1
        fi
    else
        echo -e "${WARNING}This script is running on a MIPS system, so we expect it to be run as root"
    fi
}

check_klipper() {
    if [ "$NOSERVICE" -ne 1 ]; then
        if [ "$IS_MIPS" -ne 1 ]; then
            if [ "$(systemctl list-units --full -all -t service --no-legend | grep -F "${KLIPPER_SERVICE}")" ]; then
                echo -e "${DIM}Klipper ${KLIPPER_SERVICE} systemd service found"
            else
                echo -e "${ERROR}Klipper ${KLIPPER_SERVICE} systemd service not found! Please install Klipper first"
                exit -1
            fi
        else
            # There is no systemd on MIPS, we can only check the running processes
            running_klipper_pid=$(ps -o pid,comm,args | grep [^]]/usr/share/klipper/klippy/klippy.py | awk '{print $1}')
            KLIPPER_PID_FILE=/var/run/klippy.pid

            if [ $(cat $KLIPPER_PID_FILE) = $running_klipper_pid ]; then
                echo -e "${DIM}Klipper service found"
            else
                echo -e "${ERROR}Klipper service not found! Please install Klipper first"
                exit -1
            fi
        fi
    fi
}

check_octoprint() {
    if [ "$IS_MIPS" -eq 1 ]; then
        OCTOPRINT=0 # Octoprint can not be set up on MIPS
    elif [ "$NOSERVICE" -ne 1 ]; then
        if [ "$(sudo systemctl list-units --full -all -t service --no-legend | grep -F "octoprint.service")" ]; then
            echo -e "${DIM}OctoPrint service found"
            OCTOPRINT=1
        else
            OCTOPRINT=0
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
            if [ ! -f "${OCTOPRINT_KLIPPER_CONFIG_HOME}/${PRINTER_CONFIG}" ]; then
                echo -e "${ERROR}Klipper config directory (${KLIPPER_CONFIG_HOME} or ${OLD_KLIPPER_CONFIG_HOME}) not found. Use '-c <dir>' option to override"
                exit -1
            fi
            KLIPPER_CONFIG_HOME="${OCTOPRINT_KLIPPER_CONFIG_HOME}"
        else
            KLIPPER_CONFIG_HOME="${OLD_KLIPPER_CONFIG_HOME}"
        fi
    fi
    echo -e "${DIM}Klipper config directory (${KLIPPER_CONFIG_HOME}) found"

    if [ ! -d "${MOONRAKER_HOME}" ]; then
        if [ "${OCTOPRINT}" -eq 0 ]; then
            echo -e "${ERROR}Moonraker home directory (${MOONRAKER_HOME}) not found. Use '-m <dir>' option to override"
            exit -1
        fi
        echo -e "${WARNING}Moonraker home directory (${MOONRAKER_HOME}) not found. OctoPrint detected, skipping."
    fi
}

# Silently cleanup any potentially old klippy modules
cleanup_old_klippy_modules() {
    if [ -d "${KLIPPER_HOME}/klippy/extras" ]; then
        for file in mmu.py mmu_toolhead.py mmu_config_setup.py; do
            rm -f "${KLIPPER_HOME}/klippy/extras/${file}"
        done
    fi
}

link_mmu_plugins() {
    echo -e "${INFO}Linking mmu extensions to Klipper..."
    if [ -d "${KLIPPER_HOME}/klippy/extras" ]; then
        mkdir -p "${KLIPPER_HOME}/klippy/extras/mmu"
        for dir in extras extras/mmu; do
            for file in ${SRCDIR}/${dir}/*.py; do
                ln -sf "$file" "${KLIPPER_HOME}/klippy/${dir}/$(basename "$file")"
            done
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
        for dir in extras extras/mmu; do
            for file in ${SRCDIR}/${dir}/*.py; do
                rm -f "${KLIPPER_HOME}/klippy/${dir}/$(basename "$file")"
            done
        done
        rm -rf "${KLIPPER_HOME}/klippy/extras/mmu"
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

# Parse file config settings into memory
parse_file() {
    file="$1"
    prefix_filter="$2"
    namespace="$3"
    checkdup="$4"
    checkdup=""

    if [ ! -f "${file}" ]; then
        return
    fi

    # Read old config files
    while IFS= read -r line
    do
        # Remove leading spaces, comments and config sections
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%%#*}"
        line="${line%%[*}"
        line="${line%%;*}"

        # Check if line is not empty and contains variable or parameter
         if [ ! -z "$line" ] && { [ -z "$prefix_filter" ] || [[ "$line" =~ ^($prefix_filter) ]]; }; then
            # Split the line into parameter and value
            IFS=":=" read -r parameter value <<< "$line"

            # Remove leading and trailing whitespace
            parameter=$(echo "$parameter" | xargs)
            # Need to be more careful with value because it can be quoted
            value=$(echo "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

            # If parameter is one of interest and it has a value remember it
            if echo "$parameter" | grep -E -q "${prefix_filter}"; then
                if [ "${value}" != "" ]; then
                    combined="${namespace}${parameter}"
                    if [ -n "${checkdup}" ] && [ ! -z "${!combined+x}" ]; then
                        echo -e "${ERROR}${parameter} defined multiple times!"
                    fi
                    if echo "$value" | grep -q '^{.*}$'; then
                        eval "${combined}=\$${value}"
                    elif [ "${value%"${value#?}"}" = "'" ]; then
                        eval "${combined}=\'${value}\'"
                    else
                        eval "${combined}='${value}'"
                    fi
                fi
            fi
        fi
    done < "${file}"
}

# Copy config file substituting in memory values from past config or initial interview
update_copy_file() {
    src="$1"
    dest="$2"
    prefix_filter="$3"
    namespace="$4"

    # Read the file line by line
    while IFS="" read -r line || [ -n "$line" ]
    do
        if echo "$line" | grep -E -q '^[[:space:]]*#'; then
            # Just copy simple comments
            echo "$line"
        elif [ ! -z "$line" ] && { [ -z "$prefix_filter" ] || [[ "$line" =~ ^($prefix_filter) ]]; }; then
            # Line of interest
            # Split the line into the part before # and the part after #
            parameterAndValueAndSpace=$(echo "$line" | sed 's/^[[:space:]]*//' | sed 's/;/# /' | cut -d'#' -f1)

            comment=""
            if echo "$line" | grep -q "#"; then
                commentChar="#"
                comment=$(echo "$line" | sed 's/[^#]*#//')
            elif echo "$line" | grep -q ";"; then
                commentChar=";"
                comment=$(echo "$line" | sed 's/[^;]*;//')
            fi
            space=`printf "%s" "$parameterAndValueAndSpace" | sed 's/.*[^[:space:]]\(.*\)$/\1/'`

            if echo "$parameterAndValueAndSpace" | grep -E -q "${prefix_filter}"; then
                # If parameter and value exist, substitute the value with the in memory variable of the same name
                if echo "$parameterAndValueAndSpace" | grep -E -q '^\['; then
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
                    if [ -z "$new_value" ]; then
                        new_value="''"
                    fi
                    if [ -n "$comment" ]; then
                        echo "${parameter}: ${new_value}${space}${commentChar}${comment}"
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

# Get MMU type info first
read_previous_mmu_type() {
    HAS_SELECTOR="yes"
    dest_cfg="${KLIPPER_CONFIG_HOME}/mmu/base/mmu_hardware.cfg"
    if [ -f "${dest_cfg}" ]; then
        if ! grep -q "^\[stepper_mmu_selector\]" "${dest_cfg}"; then
            HAS_SELECTOR="no"
        fi
    fi
}

# Set default parameters from the distribution (reference) config files
read_default_config() {
    echo -e "${INFO}Reading default configuration parameters..."
    if [ "$HAS_SELECTOR" == "no" ]; then
        parse_file "${SRCDIR}/config/base/mmu_parameters.cfg.vs" ""            "_param_" "checkdup"
    else
        parse_file "${SRCDIR}/config/base/mmu_parameters.cfg" ""               "_param_" "checkdup"
    fi
    parse_file "${SRCDIR}/config/base/mmu_macro_vars.cfg" "variable_|filename" ""        "checkdup"
    for file in `cd ${SRCDIR}/config/addons ; ls *.cfg | grep -v "_hw" | grep -v "my_"`; do
        parse_file "${SRCDIR}/config/addons/${file}"      "variable_"          ""        "checkdup"
    done

}

# Pull parameters from previous installation
read_previous_config() {

    # Get a few vital bits of information stored in mmu_hardware.cfg if available
    cfg="mmu_hardware.cfg"
    dest_cfg=${KLIPPER_CONFIG_HOME}/mmu/base/${cfg}
    if [ -f "${dest_cfg}" ]; then
        _hw_num_gates=$(sed -n 's/^[[:space:]]*num_gates:[[:space:]]*\([0-9]\{1,\}\)[[:space:]]*.*$/\1/p' "${dest_cfg}")
    fi

    cfg="mmu_parameters.cfg"
    dest_cfg=${KLIPPER_CONFIG_HOME}/mmu/base/${cfg}
    if [ -f "${dest_cfg}" -a "$_hw_num_gates" == "" ]; then
        _hw_num_gates=$(sed -n 's/^[[:space:]]*mmu_num_gates[:=][[:space:]]*\([0-9]\{1,\}\)[[:space:]]*.*$/\1/p' "${dest_cfg}")
    fi

    if [ ! -f "${dest_cfg}" ]; then
        echo -e "${WARNING}No previous ${cfg} found. Will install default"
    else
        echo -e "${INFO}Reading ${cfg} configuration from previous installation..."
        parse_file "${dest_cfg}" "" "_param_"
    fi

    for cfg in mmu_macro_vars.cfg; do
        dest_cfg=${KLIPPER_CONFIG_HOME}/mmu/base/${cfg}
        if [ ! -f "${dest_cfg}" ]; then
            echo -e "${WARNING}No previous ${cfg} found. Will install default"
        else
            echo -e "${INFO}Reading ${cfg} configuration from previous installation..."
            if [ "${cfg}" == "mmu_macro_vars.cfg" ]; then
                parse_file "${dest_cfg}" "variable_|filename"
            else
                parse_file "${dest_cfg}" "variable_"
            fi
        fi
    done

    # TODO namespace config in third-party addons separately
    if [ -d "${KLIPPER_CONFIG_HOME}/mmu/addons" ]; then
        for cfg in `cd ${KLIPPER_CONFIG_HOME}/mmu/addons ; ls *.cfg | grep -v "_hw"`; do
            dest_cfg=${KLIPPER_CONFIG_HOME}/mmu/addons/${cfg}
            if [ ! -f "${dest_cfg}" ]; then
                echo -e "${WARNING}No previous ${cfg} found. Will install default"
            else
                echo -e "${INFO}Reading ${cfg} configuration from previous installation..."
                parse_file "${dest_cfg}" "variable_"
            fi
        done
    fi

    # Upgrade / map / force old parameters...
    # v2.7.1
    if [ ! "${variable_pin_park_x_dist}" == "" ]; then
        variable_pin_park_dist="${variable_pin_park_x_dist}"
    fi
    if [ ! "${variable_pin_loc_x_compressed}" == "" ]; then
        variable_pin_loc_compressed="${variable_pin_loc_x_compressed}"
    fi
    if [ ! "${variable_park_xy}" == "" ]; then
        variable_park_toolchange="${variable_park_xy}, ${_param_z_hop_height_toolchange:-0}, 0, 2"
        variable_park_error="${variable_park_xy}, ${_param_z_hop_height_error:-0}, 0, 2"
    fi
    if [ ! "${variable_lift_speed}" == "" ]; then
        variable_park_lift_speed="${variable_lift_speed}"
    fi
    if [ "${variable_enable_park}" == "False" ]; then
        variable_enable_park_printing="'pause,cancel'"
        if [ "${variable_enable_park_runout}" == "True" ]; then
            variable_enable_park_printing="'toolchange,load,unload,runout,pause,cancel'"
        fi
    else
        variable_enable_park_printing="'toolchange,load,unload,pause,cancel'"
    fi
    if [ "${variable_enable_park_standalone}" == "False" ]; then
        variable_enable_park_standalone="'pause,cancel'"
    else
        variable_enable_park_standalone="'toolchange,load,unload,pause,cancel'"
    fi

    # v2.7.2
    if [ "${_param_toolhead_residual_filament}" == "0" -a ! "${_param_toolhead_ooze_reduction}" == "0" ]; then
        _param_toolhead_residual_filament=$_param_toolhead_ooze_reduction
        _param_toolhead_ooze_reduction=0
    fi

    # v2.7.3 - Blobifer update - Oct 13th 20204
    if [ ! "${variable_iteration_z_raise}" == "" ]; then
        echo -e "${INFO}Setting Blobifier variable_z_raise and variable_purge_length_maximum from previous settings"
        variable_z_raise=$(awk -v iter_z_raise="$variable_iteration_z_raise" -v max_iter="$variable_max_iterations_per_blob" -v z_change="$variable_iteration_z_change" 'BEGIN {
            triangular_value = (max_iter - 1) * max_iter / 2;
            print iter_z_raise * max_iter - triangular_value * z_change;
        }')
        variable_purge_length_maximum=$(awk -v max_len="$variable_max_iteration_length" -v max_iter="$variable_max_iterations_per_blob" 'BEGIN { print max_len * max_iter }')
    fi

    # v3.0.0
    if [ "${_param_auto_calibrate_gates}" != "" ]; then
        _param_autotune_rotation_distance=${_param_auto_calibrate_gates}
    fi
    if [ "${_param_auto_calibrate_bowden}" != "" ]; then
        _param_autotune_bowden_length=${_param_auto_calibrate_bowden}
    fi
    if [ "${_param_endless_spool_final_eject}" != "" ]; then
        _param_gate_final_eject_distance=${_param_endless_spool_final_eject}
    fi
    if [ "${_variable_eject_tool}" != "" ]; then
        _variable_unload_tool=${_variable_eject_tool}
    fi
    if [ "${_variable_eject_tool_on_cancel}" != "" ]; then
        _variable_unload_tool_on_cancel=${_variable_eject_tool_on_cancel}
    fi
    # Temp for alpha testers..
    if [ "${_param_respooler_start_macro}" != "" ]; then
        _param_espooler_start_macro=${_param_respooler_start_macro}
    fi
    if [ "${_param_respooler_stop_macro}" != "" ]; then
        _param_espooler_stop_macro=${_param_respooler_stop_macro}
    fi
}

# Helper for upgrade logic
convert_to_boolean_string() {
    if [ "$1" -eq 1 ] 2>/dev/null; then
        echo "True"
    elif [ "$1" -eq 0 ] 2>/dev/null; then
        echo "False"
    else
        echo "$1"
    fi
}

# I'd prefer not to attempt to upgrade mmu_hardware.cfg but these will ease pain
# and are relatively safe
upgrade_mmu_hardware() {
    hardware_cfg="${KLIPPER_CONFIG_HOME}/mmu/base/mmu_hardware.cfg"

    # v3.0.0: Upgrade mmu_servo to mmu_selector_servo
    found_mmu_servo=$(grep -E -c "^\[mmu_servo mmu_servo\]" ${hardware_cfg} || true)
    if [ "${found_mmu_servo}" -eq 1 ]; then
        sed "s/\[mmu_servo mmu_servo\]/\[mmu_servo selector_servo\]/g" "${hardware_cfg}" > "${hardware_cfg}.tmp" && mv "${hardware_cfg}.tmp" ${hardware_cfg}
        echo -e "${INFO}Updated [mmu_servo mmu_servo] in mmu_hardware.cfg..."
    fi

    found_mmu_machine=$(grep -E -c "^\[mmu_machine\]" ${hardware_cfg} || true)

    # v3.0.0: Remove num_gates in led section
    found_num_gates=$(grep -E -c "^(#?num_gates)" ${hardware_cfg} || true)
    if [ "${found_num_gates}" -gt 0 -a "${found_mmu_machine}" -eq 0 ]; then
        sed "/^\(#\?num_gates\)/d" "${hardware_cfg}" > "${hardware_cfg}.tmp" && mv "${hardware_cfg}.tmp" ${hardware_cfg}
        echo -e "${INFO}Removed 'num_gates' from [mmu_leds] section in mmu_hardware.cfg..."
    fi

    # v3.0.0: Add minimal [mmu_machine] section as first section
    if [ "${found_mmu_machine}" -eq 0 ]; then

        # Note params will be comming from mmu_parameters
        new_section=$(cat <<EOF
# MMU MACHINE / TYPE ---------------------------------------------------------------------------------------------------
# ███╗   ███╗███╗   ███╗██╗   ██╗    ███╗   ███╗ █████╗  ██████╗██╗  ██╗██╗███╗   ██╗███████╗
# ████╗ ████║████╗ ████║██║   ██║    ████╗ ████║██╔══██╗██╔════╝██║  ██║██║████╗  ██║██╔════╝
# ██╔████╔██║██╔████╔██║██║   ██║    ██╔████╔██║███████║██║     ███████║██║██╔██╗ ██║█████╗  
# ██║╚██╔╝██║██║╚██╔╝██║██║   ██║    ██║╚██╔╝██║██╔══██║██║     ██╔══██║██║██║╚██╗██║██╔══╝  
# ██║ ╚═╝ ██║██║ ╚═╝ ██║╚██████╔╝    ██║ ╚═╝ ██║██║  ██║╚██████╗██║  ██║██║██║ ╚████║███████╗
# ╚═╝     ╚═╝╚═╝     ╚═╝ ╚═════╝     ╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝
[mmu_machine]
num_gates: ${_param_mmu_num_gates}				# Number of selectable gates on MMU
mmu_vendor: ${_param_mmu_vendor}			# MMU family
mmu_version: ${_param_mmu_version}			# MMU hardware version number (add mod suffix documented above)

EOF
)
        temp_file=$(mktemp)
        echo "$new_section" > "$temp_file"
        awk '
            BEGIN { found = 0 }
            /^[[:space:]]*$/ && !found {
                print
                while ((getline line < "'"$temp_file"'") > 0) print line
                close("'"$temp_file"'")
                found = 1
                next
            }
            { print }
        ' "${hardware_cfg}" > "${hardware_cfg}.tmp" && mv "${hardware_cfg}.tmp" "${hardware_cfg}"
        rm "$temp_file"

        echo -e "${INFO}Added new [mmu_machine] section to mmu_hardware.cfg..."
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
        mkdir ${mmu_dir}/addons
    else
        echo -e "${DETAIL}Config directory ${mmu_dir} already exists"
        echo -e "${DETAIL}Backing up old config files to ${next_mmu_dir}"
        mkdir ${next_mmu_dir}
        (cd "${mmu_dir}"; cp -r * "${next_mmu_dir}")

        # Ensure all new directories exist
        mkdir -p ${mmu_dir}/base
        mkdir -p ${mmu_dir}/optional
        mkdir -p ${mmu_dir}/addons
    fi

    _hw_additional_pins=
    if [ ${ADDONS_DC_ESPOOLER} -eq 1 ]; then
        for i in $(seq 0 $(expr $_hw_num_gates - 1)); do
            espooler_pins="    MMU_DC_MOT_$(expr $i + 1)_EN={spooler_en_${i}_pin},\n"
            espooler_pins="${espooler_pins}    MMU_DC_MOT_$(expr $i + 1)_A={spooler_rwd_${i}_pin},\n"
            espooler_pins="${espooler_pins}    MMU_DC_MOT_$(expr $i + 1)_B={spooler_fwd_${i}_pin},\n"
            _hw_additional_pins="${_hw_additional_pins}\n${espooler_pins}"
        done
    fi

    # Now substitute tokens using given brd_type and "questionaire" starting values
    _hw_num_leds=$(expr $_hw_num_gates \* 2 + 1)
    _hw_num_leds_minus1=$(expr $_hw_num_leds - 1)
    _hw_num_gates_plus1=$(expr $_hw_num_gates + 1)

    # Find all variables that start with _hw_
    for var in $(compgen -v | grep '^_hw_'); do
        value=${!var}
        pattern="{${var#_hw_}}"
        sed_expr="${sed_expr}s|${pattern}|${value}|g; "
    done

    # Find all variables in the form of PIN[$_hw_brd_type,*]
    if [ "$HAS_SELECTOR" == "yes" ]; then
        key_match="$_hw_brd_type"
    else
        # Type-B MMU has alternative pin allocation
        key_match="B,$_hw_brd_type"
    fi
    for key in "${!PIN[@]}"; do
        if [[ $key == "$key_match"* ]]; then
            value="${PIN[$key]}"
            pin_var=$(echo "$key" | sed "s/^$key_match,//")
            pattern="{${pin_var}}"
            sed_expr="${sed_expr}s|${pattern}|${value}|g; "
        fi
    done

    for file in `cd ${SRCDIR}/config/base ; ls *.cfg`; do
        src=${SRCDIR}/config/base/${file}
        dest=${mmu_dir}/base/${file}
        next_dest=${next_mmu_dir}/base/${file}

        if [ -f "${dest}" ]; then
            if [ "${file}" == "mmu_hardware.cfg" -a "${INSTALL}" -eq 0 ] || [ "${file}" == "mmu.cfg"  -a "${INSTALL}" -eq 0 ]; then
                echo -e "${WARNING}Skipping copy of hardware config file ${file} because already exists"
                continue
            else
                if [ "${file}" == "mmu_parameters.cfg" ] || [ "${file}" == "mmu_macro_vars.cfg" ]; then
                    echo -e "${INFO}Upgrading configuration file ${file}"
                else
                    echo -e "${INFO}Installing configuration file ${file}"
                fi
                mv ${dest} ${next_dest} # Backup old config file
            fi
        fi

        # Hardware files: Special token substitution -----------------------------------------
        if [ "${file}" == "mmu.cfg" -o "${file}" == "mmu_hardware.cfg" ]; then
            cp ${src} ${dest}

            # Correct shared uart_address for EASY-BRD
            if [ "${_hw_brd_type}" == "EASY-BRD" ]; then
                # Share uart_pin to avoid duplicate alias problem
                cat ${dest} | sed -e "\
                    s/^uart_pin: mmu:MMU_SEL_UART/uart_pin: mmu:MMU_GEAR_UART/; \
                        " > ${dest}.tmp && mv ${dest}.tmp ${dest}
            else
                # Remove uart_address lines
                cat ${dest} | sed -e "\
                    /^uart_address:/ d; \
                        " > ${dest}.tmp && mv ${dest}.tmp ${dest}
            fi

            if [ "${SETUP_SELECTOR_TOUCH}" == "yes" ]; then
                cat ${dest} | sed -e "\
                    s/^#\(diag_pin: \^mmu:MMU_SEL_DIAG\)/\1/; \
                    s/^#\(driver_SGTHRS: 75\)/\1/; \
                    s/^#\(extra_endstop_pins: tmc2209_stepper_mmu_selector:virtual_endstop\)/\1/; \
                    s/^#\(extra_endstop_names: mmu_sel_touch\)/\1/; \
                    s/^uart_address:/${uart_comment}uart_address:/; \
                        " > ${dest}.tmp && mv ${dest}.tmp ${dest}
            fi

            # Do all the token substitution
            cat ${dest} | sed -e "$sed_expr" "${dest}" > "${dest}.tmp" > ${dest}.tmp && mv ${dest}.tmp ${dest}

            # Handle LED option - Comment out if disabled (section is last, go comment to end of file)
            if [ "${file}" == "mmu_hardware.cfg" -a "$SETUP_LED" == "no" ]; then
                sed "/^# MMU OPTIONAL NEOPIXEL/,$ {/^[^#]/ s/^/#/}" ${dest} > ${dest}.tmp && mv ${dest}.tmp ${dest}
            fi

            # Handle Encoder option - Delete if not required (section is 25 lines long)
            if [ "${file}" == "mmu_hardware.cfg" -a "$HAS_ENCODER" == "no" ]; then
                sed "/^# ENCODER/,+24 d" ${dest} > ${dest}.tmp && mv ${dest}.tmp ${dest}
            fi

            # Handle Selector options - Delete if not required (sections are 8 and 38 lines respectively)
            if [ "${file}" == "mmu_hardware.cfg" -a "$HAS_SELECTOR" == "no" ]; then
                sed "/^# SELECTOR SERVO/,+7 d" ${dest} > ${dest}.tmp && mv ${dest}.tmp ${dest}
                sed "/^# SELECTOR STEPPER/,+37 d" ${dest} > ${dest}.tmp && mv ${dest}.tmp ${dest}

                # Expand out the additional filament drive for each gate
                additional_gear_section=$(sed -n "/^# ADDITIONAL FILAMENT DRIVE/,+10 p" ${dest} | sed "1,3d")
                awk '{ print } /^# ADDITIONAL FILAMENT DRIVE/ { for (i=1; i<=11; i++) { getline; print }; exit }' ${dest} > ${dest}.tmp
                for (( i=2; i<=$(expr $_hw_num_gates - 1); i++ ))
                do
                    echo "$(echo "${additional_gear_section}" | sed "s/_1/_$i/g")" >> ${dest}.tmp
                    echo >> ${dest}.tmp
                done
                awk '/^# ADDITIONAL FILAMENT DRIVE/ {flag=1; count=0} flag && count++ >= 12 {print}' ${dest} >> ${dest}.tmp && mv ${dest}.tmp ${dest}

            else
                # Delete additional gear drivers template section
                sed "/^# ADDITIONAL FILAMENT DRIVE/,+10 d" ${dest} > ${dest}.tmp && mv ${dest}.tmp ${dest}
            fi

        # Configuration parameters -----------------------------------------------------------
        elif [ "${file}" == "mmu_parameters.cfg" ]; then
            if [ "${HAS_SELECTOR}" == "no" ]; then
                # Use truncated VirtualSelector parameter file
                update_copy_file "${src}.vs" "$dest" "" "_param_"
            else
                update_copy_file "$src" "$dest" "" "_param_"
            fi

            # Ensure that supplemental user added params are retained. These are those that are
            # by default set internally in Happy Hare based on vendor and version settings but
            # can be overridden.  This set also includes a couple of hidden test parameters.
            echo "" >> $dest
            echo "# SUPPLEMENTAL USER CONFIG retained after upgrade --------------------------------------------------------------------" >> $dest
            echo "#" >> $dest
            supplemental_params="cad_gate0_pos cad_gate_width cad_bypass_offset cad_last_gate_offset cad_block_width cad_bypass_block_width cad_bypass_block_delta cad_selector_tolerance gate_material gate_color gate_spool_id gate_status gate_filament_name gate_temperature gate_speed_override endless_spool_groups tool_to_gate_map"
            hidden_params="test_random_failures test_random_failures test_disable_encoder test_force_in_print serious"
            for var in $(set | grep '^_param_' | cut -d'=' -f1 | sort); do
                param=${var#_param_}
                for item in ${supplemental_params} ${hidden_params}; do
                    if [ "$item" = "$param" ]; then
                        value=$(eval echo "\$${var}")
                        echo "${param}: ${value}"
                        eval unset ${var}
                    fi
                done
            done >> $dest

            # If any params are still left warn the user because they will be lost (should have been upgraded)
            for var in $(set | grep '^_param_' | cut -d= -f1); do
                param=${var#_param_}
                value=$(eval echo \$$var)
                echo "Parameter: '$param: $value' is deprecated and has been removed"
            done

        # Variables macro ---------------------------------------------------------------------
        elif [ "${file}" == "mmu_macro_vars.cfg" ]; then
            tx_macros=""
            if [ "$_hw_num_gates" == "" -o "$_hw_num_gates" == "{num_gates}" ]; then
                _hw_num_gates=12
            fi
            for (( i=0; i<=$(expr $_hw_num_gates - 1); i++ ))
            do
                tx_macros+="[gcode_macro T${i}]\n"
                tx_macros+="gcode: MMU_CHANGE_TOOL TOOL=${i}\n"
            done

            if [ "${INSTALL}" -eq 1 ]; then
                cat ${src} | sed -e "\
                    s%{tx_macros}%${tx_macros}%g; \
                        " > ${dest}
            else
                cat ${src} | sed -e "\
                    s%{tx_macros}%${tx_macros}%g; \
                        " > ${dest}.tmp
                update_copy_file "${dest}.tmp" "${dest}" "variable_|filename" && rm ${dest}.tmp
            fi

        # Everything else is read-only symlink ------------------------------------------------
        else
            ln -sf ${src} ${dest}
        fi
    done

    # Optional config are read-only symlinks --------------------------------------------------
    for file in `cd ${SRCDIR}/config/optional ; ls *.cfg`; do
        src=${SRCDIR}/config/optional/${file}
        dest=${mmu_dir}/optional/${file}
        ln -sf ${src} ${dest}
    done

    # Don't stomp on existing persisted state ------------------------------------------------
    src=${SRCDIR}/config/mmu_vars.cfg
    dest=${mmu_dir}/mmu_vars.cfg
    if [ -f "${dest}" ]; then
        echo -e "${WARNING}Skipping copy of mmu_vars.cfg file because already exists"
    else
        cp ${src} ${dest}
    fi

    # Addon config files are always copied (and updated) so they can be edited ----------------
    # Skipping files with 'my_' prefix for development
    for file in `cd ${SRCDIR}/config/addons ; ls *.cfg | grep -v "my_"`; do
        src=${SRCDIR}/config/addons/${file}
        dest=${mmu_dir}/addons/${file}
        if [ -f "${dest}" ]; then
            if ! echo "$file" | grep -E -q ".*_hw\.cfg.*"; then
                echo -e "${INFO}Upgrading configuration file ${file}"
                update_copy_file ${src} ${dest} "variable_"
            else
                echo -e "${WARNING}Skipping copy of ${file} file because already exists"
            fi
        else
            echo -e "${INFO}Installing configuration file ${file}"
            cp ${src} ${dest}
        fi
    done
}

uninstall_config_files() {
    if [ -d "${KLIPPER_CONFIG_HOME}/mmu" ]; then
        echo -e "${INFO}Removing MMU configuration files from ${KLIPPER_CONFIG_HOME}"
        mv "${KLIPPER_CONFIG_HOME}/mmu" /tmp/mmu.uninstalled
    fi
}

install_printer_includes() {
    # Link in all includes if not already present
    dest=${KLIPPER_CONFIG_HOME}/${PRINTER_CONFIG}
    if test -f $dest; then

        klippain_included=$(grep -c "\[include config/hardware/mmu.cfg\]" ${dest} || true)
        if [ "${klippain_included}" -eq 1 ]; then
            echo -e "${WARNING}This looks like a Klippain config installation - skipping automatic config install. Please add config includes by hand"
        else
            next_dest="$(nextfilename "$dest")"
            echo -e "${INFO}Copying original ${PRINTER_CONFIG} file to ${next_dest}"
            cp ${dest} ${next_dest}
            if [ ${ADDONS_EREC} -eq 1 ]; then
                i='\[include mmu/addons/mmu_erec_cutter.cfg\]'
                already_included=$(grep -c "${i}" ${dest} || true)
                if [ "${already_included}" -eq 0 ]; then
                    sed -i "1i ${i}" ${dest}
                fi
            fi
            if [ ${ADDONS_BLOBIFIER} -eq 1 ]; then
                i='\[include mmu/addons/blobifier.cfg\]'
                already_included=$(grep -c "${i}" ${dest} || true)
                if [ "${already_included}" -eq 0 ]; then
                    sed -i "1i ${i}" ${dest}
                fi
            fi
            if [ ${ADDONS_DC_ESPOOLER} -eq 1 ]; then
                i='\[include mmu/addons/dc_espooler.cfg\]'
                already_included=$(grep -c "${i}" ${dest} || true)
                if [ "${already_included}" -eq 0 ]; then
                    sed -i "1i ${i}" ${dest}
                fi
            fi
            if [ ${MENU_12864} -eq 1 ]; then
                i='\[include mmu/optional/mmu_menu.cfg\]'
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
            for i in '\[include mmu/base/\*.cfg\]' ; do
                already_included=$(grep -c "${i}" ${dest} || true)
                if [ "${already_included}" -eq 0 ]; then
                    sed -i "1i ${i}" ${dest}
                fi
            done
        fi
    else
        echo -e "${WARNING}File ${PRINTER_CONFIG} file not found! Cannot include MMU configuration files"
    fi
}

uninstall_printer_includes() {
    echo -e "${INFO}Cleaning MMU references from ${PRINTER_CONFIG}"
    dest=${KLIPPER_CONFIG_HOME}/${PRINTER_CONFIG}
    if test -f $dest; then
        next_dest="$(nextfilename "$dest")"
        echo -e "${INFO}Copying original ${PRINTER_CONFIG} file to ${next_dest} before cleaning"
        cp ${dest} ${next_dest}
        cat "${dest}" | sed -e " \
            /\[include mmu\/optional\/client_macros.cfg\]/ d; \
            /\[include mmu\/optional\/mmu_menu.cfg\]/ d; \
            /\[include mmu\/base\/\*.cfg\]/ d; \
            /\[include mmu\/addon\/\*.cfg\]/ d; \
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
            # The path for Happy-Hare on MIPS is /usr/data/Happy-Hare
            if [ "$IS_MIPS" -eq 1 ]; then
                sed -i 's|path: ~/Happy-Hare|path: /usr/data/Happy-Hare|' "${file}"
                echo -e "${INFO}Update Happy-Hare path for MIPS architecture."
            fi
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

        # Quick "catch-up" update for new toolchange_next_pos pre-processing
        update_section=$(grep -c 'enable_toolchange_next_pos' ${file} || true)
        if [ "${update_section}" -eq 0 ]; then
            awk '/^enable_file_preprocessor/ {print $0 "\nenable_toolchange_next_pos: True\n"; next} {print}' ${file} > ${file}.tmp && mv ${file}.tmp ${file}
            restart=1
            echo -e "${WARNING}Added new 'enable_toolchange_next_pos' to moonraker.conf"
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

        if [ "$IS_MIPS" -ne 1 ]; then
            sudo systemctl restart ${KLIPPER_SERVICE}
        else
            set +e
            /etc/init.d/*klipper_service restart
            set -e
        fi
    else
        echo -e "${WARNING}Klipper restart suppressed - Please restart ${KLIPPER_SERVICE} by hand"
    fi
}

restart_moonraker() {
    if [ "$NOSERVICE" -ne 1 ]; then
        echo -e "${INFO}Restarting Moonraker..."

        if [ "$IS_MIPS" -ne 1 ]; then
            sudo systemctl restart moonraker
        else
            set +e
            /etc/init.d/*moonraker_service restart
            set -e
        fi
    else
        echo -e "${WARNING}Moonraker restart suppressed - Please restart by hand"
    fi
}

prompt_yn() {
    while true; do
        read -n1 -p "$@ (y/n)? " yn
        case "${yn}" in
            Y|y)
                echo -n "y" 
                break
                ;;
            N|n)
                echo -n "n" 
                break
                ;;
            *)
                ;;
        esac
    done
}

prompt_123() {
    prompt=$1
    max=$2
    while true; do
        if [ -z "${max}" ]; then
            read -ep "${prompt}? " number
        elif [[ "${max}" -lt 10 ]]; then
            read -ep "${prompt} (1-${max})? " -n1 number
        else
            read -ep "${prompt} (1-${max})? " number
        fi
        if ! [[ "$number" =~ ^-?[0-9]+$ ]] ; then
            echo -e "Invalid value." >&2
            continue
        fi
        if [ "$number" -lt 1 ]; then
            echo -e "Value must be greater than 0." >&2
            continue
        fi
        if [ -n "$max" ] && [ "$number" -gt "$max" ]; then
            echo -e "Value must be less than $((max+1))." >&2
            continue
        fi
        echo ${number}
        break
    done
}

prompt_option() {
    local var_name="$1"
    local query="$2"
    shift 2
    local i=0
    for val in "$@"; do
        i=$((i+1))
        echo "$i) $val"
    done
    REPLY=$(prompt_123 "$query" "$#")
    declare -g $var_name="${!REPLY}"
}

option() {
    local var_name="$1"
    local desc="$2"
    declare -g $var_name="${desc}"
    OPTIONS+=("$desc")
}

questionaire() {
    echo
    echo -e "${INFO}Let me see if I can get you started with initial configuration"
    echo -e "You will still have some manual editing to perform but I will explain that later"
    echo -e "(Note that all this script does is set a lot of the time consuming parameters in the config"
    echo
    echo -e "${PROMPT}${SECTION}What type of MMU are you running?${INPUT}"
    OPTIONS=()
    option ERCF11         'ERCF v1.1 (inc TripleDecky, Springy, Binky mods)'
    option ERCF20         'ERCF v2.0'
    option TRADRACK       'Tradrack v1.0'
    option ANGRY_BEAVER   'Angry Beaver v1.0'
    option BOX_TURTLE     'Box Turtle v1.0'
    option NIGHT_OWL      'Night Owl v1.0'
    option _3MS           '3MS (Modular Multi Material System) v1.0'
    option _3D_CHAMELEON  '3D Chameleon'
    option OTHER          'Other / Custom (or just want starter config files)'
    prompt_option opt 'MMU Type' "${OPTIONS[@]}"
    case $opt in
        "$ERCF11")
            HAS_ENCODER=yes
            HAS_SELECTOR=yes
            _hw_mmu_vendor="ERCF"
            _hw_mmu_version="1.1"
            _hw_selector_type=LinearSelector
            _hw_variable_bowden_lengths=0
            _hw_variable_rotation_distances=1
            _hw_require_bowden_move=1
            _hw_filament_always_gripped=0
            _hw_gear_gear_ratio="80:20"
            _hw_gear_run_current=0.5
            _hw_gear_hold_current=0.1
            _hw_sel_run_current=0.4
            _hw_sel_hold_current=0.2
            _hw_encoder_resolution=0.7059
            _param_extruder_homing_endstop="collision"
            _param_gate_homing_endstop="encoder"
            _param_gate_parking_distance=23
            _param_servo_buzz_gear_on_down=3
            _param_servo_duration=0.4
            _param_servo_always_active=0
            _param_servo_buzz_gear_on_down=1

            echo
            echo -e "${PROMPT}Some popular upgrade options for ERCF v1.1 can automatically be setup. Let me ask you about them...${INPUT}"
            yn=$(prompt_yn "Are you using the 'Springy' sprung servo selector cart")
            echo
            case $yn in
            y)
                _hw_mmu_version+="s"
                ;;
            esac
            yn=$(prompt_yn "Are you using the improved 'Binky' encoder")
            echo
            case $yn in
            y)
                _hw_mmu_version+="b"
                ;;
            esac
            yn=$(prompt_yn "Are you using the wider 'Triple-Decky' filament blocks")
            echo
            case $yn in
            y)
                _hw_mmu_version+="t"
                ;;
            esac
            ;;

        "$ERCF20")
            HAS_ENCODER=yes
            HAS_SELECTOR=yes
            _hw_mmu_vendor="ERCF"
            _hw_mmu_version="2.0"
            _hw_selector_type=LinearSelector
            _hw_variable_bowden_lengths=0
            _hw_variable_rotation_distances=1
            _hw_require_bowden_move=1
            _hw_filament_always_gripped=0
            _hw_gear_gear_ratio="80:20"
            _hw_gear_run_current=0.5
            _hw_gear_hold_current=0.1
            _hw_sel_run_current=0.4
            _hw_sel_hold_current=0.2
            _hw_encoder_resolution=1.0
            _param_extruder_homing_endstop="collision"
            _param_gate_homing_endstop="encoder"
            _param_gate_parking_distance=13 # ThumperBlocks is 11
            _param_servo_buzz_gear_on_down=3
            _param_servo_duration=0.4
            _param_servo_always_active=0
            _param_servo_buzz_gear_on_down=1
            ;;

        "$TRADRACK")
            HAS_ENCODER=no
            HAS_SELECTOR=yes
            _hw_mmu_vendor="Tradrack"
            _hw_mmu_version="1.0"
            _hw_selector_type=LinearSelector
            _hw_variable_bowden_lengths=0
            _hw_variable_rotation_distances=0
            _hw_require_bowden_move=1
            _hw_filament_always_gripped=0
            _hw_gear_gear_ratio="50:17"
            _hw_gear_run_current=1.27
            _hw_gear_hold_current=0.2
            _hw_sel_run_current=0.63
            _hw_sel_hold_current=0.2
            _param_extruder_homing_endstop="none"
            _param_gate_homing_endstop="mmu_gate"
            _param_gate_parking_distance=17.5
            _param_servo_buzz_gear_on_down=0
            _param_servo_always_active=1

            echo -e "${PROMPT}Some popular upgrade options for Tradrack v1.0 can automatically be setup. Let me ask you about them...${INPUT}"
            yn=$(prompt_yn "Are you using the 'Binky' encoder modification")
            echo
            case $yn in
            y)
                HAS_ENCODER=yes
                _hw_mmu_version+="e"
                _param_extruder_homing_endstop="collision"
                _param_gate_homing_endstop="encoder"
                _param_gate_parking_distance=48.0
                _param_gate_endstop_to_encoder=31.0
                ;;
            esac
            ;;

        "$ANGRY_BEAVER")
            HAS_ENCODER=no
            HAS_SELECTOR=no
            _hw_mmu_vendor="AngryBeaver"
            _hw_mmu_version="1.0"
            _hw_selector_type=VirtualSelector
            _hw_variable_bowden_lengths=0
            _hw_variable_rotation_distances=1
            _hw_require_bowden_move=0
            _hw_filament_always_gripped=1
            _hw_gear_gear_ratio="1:1"
            _hw_gear_run_current=0.7
            _hw_gear_hold_current=0.1
            _param_extruder_homing_endstop="extruder"
            _param_gate_homing_endstop="extruder"
            _param_gate_homing_max=500
            _param_gate_parking_distance=50
            _param_gear_homing_speed=80
            ;;

        "$BOX_TURTLE")
            HAS_ENCODER=no
            HAS_SELECTOR=no
            ADDONS_DC_ESPOOLER=1
            _hw_mmu_vendor="BoxTurtle"
            _hw_mmu_version="1.0"
            _hw_selector_type=VirtualSelector
            _hw_variable_bowden_lengths=0
            _hw_variable_rotation_distances=1
            _hw_require_bowden_move=1
            _hw_filament_always_gripped=1
            _hw_gear_gear_ratio="1:1"
            _hw_gear_run_current=0.7
            _hw_gear_hold_current=0.1
            _param_extruder_homing_endstop="none"
            _param_gate_homing_endstop="mmu_gate"
            _param_gate_homing_max=300
            _param_gate_parking_distance=100
            _param_gate_final_eject_distance=100

            # Macro variable config
            _param_espooler_start_macro="MMU_ESPOOLER_START"
            _param_espooler_stop_macro="MMU_ESPOOLER_STOP"
            ;;

        "$NIGHT_OWL")
            HAS_ENCODER=no
            HAS_SELECTOR=no
            _hw_mmu_vendor="NightOwl"
            _hw_mmu_version="1.0"
            _hw_selector_type=VirtualSelector
            _hw_variable_bowden_lengths=0
            _hw_variable_rotation_distances=1
            _hw_require_bowden_move=1
            _hw_filament_always_gripped=1
            _hw_gear_gear_ratio="1:1"
            _hw_gear_run_current=0.7
            _hw_gear_hold_current=0.1
            _param_extruder_homing_endstop="none"
            _param_gate_homing_endstop="mmu_gate"
            _param_gate_parking_distance=100
            _param_gate_final_eject_distance=100

            # Macro variable config
            _param_espooler_start_macro="MMU_ESPOOLER_START"
            _param_espooler_stop_macro="MMU_ESPOOLER_STOP"
            ;;

        "$_3MS")
            HAS_ENCODER=no
            HAS_SELECTOR=no
            _hw_mmu_vendor="3MS"
            _hw_mmu_version="1.0"
            _hw_selector_type=VirtualSelector
            _hw_variable_bowden_lengths=0
            _hw_variable_rotation_distances=1
            _hw_require_bowden_move=0
            _hw_filament_always_gripped=1
            _hw_gear_gear_ratio="1:1"
            _hw_gear_run_current=0.7
            _hw_gear_hold_current=0.1
            _param_extruder_homing_endstop="extruder"
            _param_gate_homing_endstop="extruder"
            _param_gate_homing_max=500
            _param_gate_parking_distance=250
            _param_gear_homing_speed=80
            ;;

        "$_3D_CHAMELEON")
            HAS_ENCODER=no
            HAS_SELECTOR=yes
            _hw_mmu_vendor="3DChameleon"
            _hw_mmu_version="1.0"
            _hw_selector_type=RotarySelector
            _hw_variable_bowden_lengths=0
            _hw_variable_rotation_distances=0
            _hw_require_bowden_move=1
            _hw_filament_always_gripped=0
            _hw_gear_gear_ratio="1:1"
            _hw_gear_run_current=0.7
            _hw_gear_hold_current=0.1
            _param_extruder_homing_endstop="none"
            _param_gate_homing_endstop="mmu_gate"
            _param_gate_homing_max=500
            _param_gate_parking_distance=250
            _param_gear_homing_speed=80
            ;;

        *)
            HAS_ENCODER=yes
            HAS_SELECTOR=yes
            SETUP_LED=yes
            SETUP_SELECTOR_TOUCH=no
            _hw_mmu_vendor="Other"
            _hw_mmu_version="1.0"
            _hw_selector_type=LinearSelector
            _hw_variable_bowden_lengths=0
            _hw_variable_rotation_distances=0
            _hw_require_bowden_move=1
            _hw_filament_always_gripped=0
            _hw_gear_gear_ratio="1:1"
            _hw_gear_run_current=0.7
            _hw_gear_hold_current=0.1
            _hw_sel_run_current=0.5
            _hw_sel_hold_current=0.1

            # This isn't meant to be all-inclusive of options. It is just to provide a config starting point that is close
            echo -e "${PROMPT}${SECTION}Which of these most closely resembles your MMU design (this allows for some tuning of config files)?{$INPUT}"
            OPTIONS=() # reset option array
            option TYPE_A_WITH_ENCODER                          'Type-A (selector) with Encoder'
            option TYPE_A_NO_ENCODER                            'Type-A (selector), No Encoder'
            option TYPE_B_WITH_ENCODER                          'Type-B (mutliple filament drive steppers) with Encoder'
            option TYPE_B_WITH_SHARED_GATE_AND_ENCODER          'Type-B (multiple filament drive steppers) with shared Gate sensor and Encoder'
            option TYPE_B_WITH_SHARED_GATE_NO_ENCODER           'Type-B (multiple filament drive steppers) with shared Gate sensor, No Encoder'
            option TYPE_B_WITH_INDIVIDUAL_GEAR_SENSOR_AND_ENCODER 'Type-B (multiple filament drive steppers) with individual post-gear sensors and Encoder'
            option TYPE_B_WITH_INDIVIDUAL_GEAR_SENSOR_NO_ENCODER  'Type-B (multiple filament drive steppers) with individual post-gear sensors, No Encoder'
            option OTHER                                        'Just turn on all options and let me configure'
            prompt_option opt 'Type' "${OPTIONS[@]}"
            case "$opt" in
                "$TYPE_A_WITH_ENCODER")
                    _param_gate_homing_endstop="encoder"
                    _param_extruder_homing_endstop="collision"
                    echo
                    echo -e "${WARNING}    IMPORTANT: Since you have a custom MMU with selector you will need to setup some CAD dimensions in mmu_parameters.cfg... See doc"
                    ;;
                "$TYPE_A_NO_ENCODER")
                    HAS_ENCODER=no
                    _param_gate_homing_endstop="mmu_gate"
                    _param_extruder_homing_endstop="none"
                    echo
                    echo -e "${WARNING}    IMPORTANT: Since you have a custom MMU with selector you will need to setup some CAD dimensions in mmu_parameters.cfg... See doc"
                    ;;
                "$TYPE_B_WITH_ENCODER")
                    HAS_SELECTOR=no
                    _hw_selector_type=VirtualSelector
                    _hw_variable_bowden_lengths=1
                    _hw_variable_rotation_distances=1
                    _hw_filament_always_gripped=1
                    _param_gate_homing_endstop="mmu_gate"
                    _param_extruder_homing_endstop="none"
                    ;;
                "$TYPE_B_WITH_SHARED_GATE_AND_ENCODER")
                    HAS_SELECTOR=no
                    _hw_selector_type=VirtualSelector
                    _hw_variable_bowden_lengths=1
                    _hw_variable_rotation_distances=1
                    _hw_filament_always_gripped=1
                    _param_gate_homing_endstop="mmu_gate"
                    _param_extruder_homing_endstop="none"
                    ;;
                "$TYPE_B_WITH_SHARED_GATE_NO_ENCODER")
                    HAS_SELECTOR=no
                    HAS_ENCODER=no
                    _hw_selector_type=VirtualSelector
                    _hw_variable_bowden_lengths=1
                    _hw_variable_rotation_distances=1
                    _hw_filament_always_gripped=1
                    _param_gate_homing_endstop="mmu_gate"
                    _param_extruder_homing_endstop="none"
                    ;;
                "$TYPE_B_WITH_INDIVIDUAL_GEAR_SENSOR_AND_ENCODER")
                    HAS_SELECTOR=no
                    _hw_selector_type=VirtualSelector
                    _hw_variable_bowden_lengths=1
                    _hw_variable_rotation_distances=1
                    _hw_filament_always_gripped=1
                    _param_gate_homing_endstop="mmu_gear"
                    _param_extruder_homing_endstop="none"
                    ;;
                "$TYPE_B_WITH_INDIVIDUAL_GEAR_SENSOR_NO_ENCODER")
                    HAS_SELECTOR=no
                    HAS_ENCODER=no
                    _hw_selector_type=VirtualSelector
                    _hw_variable_bowden_lengths=1
                    _hw_variable_rotation_distances=1
                    _hw_filament_always_gripped=1
                    _param_gate_homing_endstop="mmu_gear"
                    _param_extruder_homing_endstop="none"
                    ;;
                *)
                    _param_gate_homing_endstop="mmu_gate"
                    _param_extruder_homing_endstop="none"
                    ;;
            esac
            ;;
        esac

    echo -e "${PROMPT}${SECTION}How many gates (selectors) do you have?${INPUT}"
    _hw_num_gates=$(prompt_123 "Number of gates")

    _hw_brd_type="unknown"
    echo -e "${PROMPT}${SECTION}Select mcu board type used to control MMU${INPUT}"
    # Perhaps consider just supporting the BTT MMB (and eventually AFC) when mmu_vendor is BoxTurtle
    # as many of these other boards may not work (due lack of exposed gpio)
    OPTIONS=()
    option MMB10                'BTT MMB v1.0 (with CANbus)'
    option MMB11                'BTT MMB v1.1 (with CANbus)'
    option FYSETC_BURROWS_ERB_1 'Fysetc Burrows ERB v1'
    option FYSETC_BURROWS_ERB_2 'Fysetc Burrows ERB v2'
    option EASY_BRD_SAMD21      'Standard EASY-BRD (with SAMD21)'
    option EASY_BRD_RP2040      'EASY-BRD with RP2040'
    option MELLOW_BRD_1         'Mellow EASY-BRD v1.x (with CANbus)'
    option MELLOW_BRD_2         'Mellow EASY-BRD v2.x (with CANbus)'
    option AFC_LITE_1           'AFC Lite v1.0'
    option OTHER                'Not in list / Unknown'
    prompt_option opt 'MCU Type' "${OPTIONS[@]}"
    case $opt in
        "$MMB10")
            _hw_brd_type="MMB10"
            pattern="Klipper_stm32"
            ;;
        "$MMB11")
            _hw_brd_type="MMB11"
            pattern="Klipper_stm32"
            ;;
        "$FYSETC_BURROWS_ERB_1")
            _hw_brd_type="ERB"
            pattern="Klipper_rp2040"
            ;;
        "$FYSETC_BURROWS_ERB_2")
            _hw_brd_type="ERBv2"
            pattern="Klipper_rp2040"
            ;;
        "$EASY_BRD_SAMD21")
            _hw_brd_type="EASY-BRD"
            pattern="Klipper_samd21"
            ;;
        "$EASY_BRD_RP2040")
            _hw_brd_type="EASY-BRD-RP2040"
            pattern="Klipper_rp2040"
            ;;
        "$MELLOW_BRD_1")
            _hw_brd_type="MELLOW-EASY-BRD-CAN"
            pattern="Klipper_rp2040"
            ;;
        "$MELLOW_BRD_2")
            _hw_brd_type="MELLOW-EASY-BRD-CANv2"
            pattern="Klipper_rp2040"
            ;;
        "$AFC_LITE_1")
            _hw_brd_type="AFC_LITE_1"
            pattern="Klipper_stm32"
            ;;
        *)
            _hw_brd_type="unknown"
            pattern="Klipper_"
            ;;
    esac

    for line in `ls /dev/serial/by-id 2>/dev/null | grep -E "Klipper_"`; do
        if echo ${line} | grep --quiet "${pattern}"; then
            echo -e "${PROMPT}${SECTION}This looks like your ${EMPHASIZE}${_hw_brd_type}${PROMPT} controller serial port. Is that correct?${INPUT}"
            yn=$(prompt_yn "/dev/serial/by-id/${line}")
            echo
            case $yn in
                y)
                    _hw_serial="/dev/serial/by-id/${line}"
                    break
                    ;;
                n)
                    ;;
            esac
        fi
    done
    if [ "${_hw_serial}" == "" ]; then
        echo
        echo -e "${WARNING}    Couldn't find your serial port, but no worries - I'll configure the default and you can manually change later"
        _hw_serial='/dev/ttyACM1 # Config guess. Run ls -l /dev/serial/by-id and set manually'
    fi

    # Avoid pin duplication. Most type-A MMU's have either encoder or gate. If both, user will have to fix
    if [ "${HAS_ENCODER}" == "yes" ]; then
        eval PIN[${_hw_brd_type},gate_sensor_pin]=""
    else
        eval PIN[${_hw_brd_type},encoder_pin]=""
    fi

    echo -e "${PROMPT}${SECTION}Would you like to have neopixel LEDs setup now for your MMU?${INPUT}"
    yn=$(prompt_yn "Enable LED support?")
    echo
    case $yn in
        y)
            SETUP_LED=yes
            ;;
        n)
            SETUP_LED=no
            ;;
    esac

    if [ "${HAS_SELECTOR}" == "yes" ]; then
        echo -e "${PROMPT}${SECTION}Touch selector operation using TMC Stallguard? This allows for additional selector recovery steps but is difficult to tune"
        echo -e "Not recommend if you are new to MMU/Happy Hare & MCU must have DIAG output for steppers. Can configure later${INPUT}"
        yn=$(prompt_yn "Enable selector touch operation")
        echo
        case $yn in
            y)
                if [ "${_hw_brd_type}" == "EASY-BRD" ]; then
                    echo
                    echo -e "${WARNING}    IMPORTANT: Set the J6 jumper pins to 2-3 and 4-5, i.e. .[..][..]  MAKE A NOTE NOW!!"
                fi
                SETUP_SELECTOR_TOUCH=yes
                ;;
            n)
                if [ "${_hw_brd_type}" == "EASY-BRD" ]; then
                    echo
                    echo -e "${WARNING}    IMPORTANT: Set the J6 jumper pins to 1-2 and 4-5, i.e. [..].[..]  MAKE A NOTE NOW!!"
                fi
                SETUP_SELECTOR_TOUCH=no
                ;;
        esac

        if [ "${_hw_mmu_vendor}" == "ERCF" ]; then
            _hw_maximum_servo_angle=180
            _hw_minimum_pulse_width=0.00085
            _hw_maximum_pulse_width=0.00215
            _param_servo_always_active=0

            echo -e "${PROMPT}${SECTION}Which servo are you using?${INPUT}"
            OPTIONS=()
            option MG90S    'MG-90S'
            option SH0255MG 'Savox SH0255MG'
            option DS041MG  'GDW DS041MG'
            option OTHER    'Not listed / Other'
            prompt_option opt 'Servo' "${OPTIONS[@]}"
            case $opt in
                "$MG90S")
                    _param_servo_up_angle=30
                    if [ "${_hw_mmu_version}" == "2.0" ]; then
                        _param_servo_move_angle=61
                    else
                        _param_servo_move_angle=${_param_servo_up_angle}
                    fi
                    _param_servo_down_angle=140
                    ;;
                "$SH0255MG")
                    _param_servo_up_angle=140
                    if [ "${_hw_mmu_version}" == "2.0" ]; then
                        _param_servo_move_angle=109
                    else
                        _param_servo_move_angle=${_param_servo_up_angle}
                    fi
                    _param_servo_down_angle=30
                    ;;
                "$DS041MG")
                    _hw_maximum_servo_angle=180
                    _hw_minimum_pulse_width=0.00050
                    _hw_maximum_pulse_width=0.00250
                    _param_servo_always_active=1
                    _param_servo_up_angle=30
                    if [ "${_hw_mmu_version}" == "2.0" ]; then
                        _param_servo_move_angle=50
                    else
                        _param_servo_move_angle=${servo_up_angle}
                    fi
                    _param_servo_down_angle=100
            esac

        elif [ "${_hw_mmu_vendor}" == "Tradrack" ]; then
            _hw_maximum_servo_angle=131
            _hw_minimum_pulse_width=0.00070
            _hw_maximum_pulse_width=0.00220
            _param_servo_always_active=1

            echo -e "${PROMPT}${SECTION}Which servo are you using?${INPUT}"
            OPTIONS=()
            option TRADRACK_BOM 'PS-1171MG or FT1117M (Tradrack)'
            option OTHER 'Not listed / Other'
            prompt_option opt 'Servo' "${OPTIONS[@]}"
            case $opt in
                "$TRADRACK_BOM")
                    _param_servo_up_angle=145
                    _param_servo_move_angle=${servo_up_angle}
                    _param_servo_down_angle=1
                    ;;
                *)
                    _param_servo_up_angle=145
                    _param_servo_move_angle=${servo_up_angle}
                    _param_servo_down_angle=1
                    ;;
            esac

        else
            _hw_maximum_servo_angle=180
            _hw_minimum_pulse_width=0.001
            _hw_maximum_pulse_width=0.002
            _param_servo_always_active=0
            _param_servo_up_angle=0
            _param_servo_move_angle=0
            _param_servo_down_angle=0
        fi
    fi

    if [ "${HAS_ENCODER}" == "yes" ]; then
        echo -e "${PROMPT}${SECTION}Clog detection? This uses the MMU encoder movement to detect clogs and can call your filament runout logic${INPUT}"
        yn=$(prompt_yn "Enable clog detection")
        echo
        case $yn in
            y)
                _param_enable_clog_detection=1
                echo -e "${PROMPT}    Would you like MMU to automatically adjust clog detection length (recommended)?${INPUT}"
                yn=$(prompt_yn "    Automatic")
                echo
                if [ "${yn}" == "y" ]; then
                    _param_enable_clog_detection=2
                fi
                ;;
            n)
                _param_enable_clog_detection=0
                ;;
        esac
    else
        _param_enable_clog_detection=0
    fi

    echo -e "${PROMPT}${SECTION}EndlessSpool? This uses filament runout detection to automate switching to new spool without interruption${INPUT}"
    yn=$(prompt_yn "Enable EndlessSpool")
    echo
    case $yn in
        y)
            _param_enable_endless_spool=1
            ;;
        n)
            _param_enable_endless_spool=0
            ;;
    esac

    echo -e "${PROMPT}${SECTION}Finally, would you like me to include all the MMU config files into your ${PRINTER_CONFIG} file${INPUT}"
    yn=$(prompt_yn "Add include?")
    echo
    case $yn in
        y)
            INSTALL_PRINTER_INCLUDES=yes
            echo -e "${PROMPT}    Would you like to include Mini 12864 screen menu configuration extension for MMU (only if you have one!)${INPUT}"
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

            echo -e "${PROMPT}    Recommended: Would you like to include the default pause/resume macros supplied with Happy Hare${INPUT}"
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

            echo -e "${PROMPT}    Addons: Would you like to include the EREC filament cutter macro (requires EREC servo installation)${INPUT}"
            yn=$(prompt_yn "    Include mmu_erec_cutter.cfg")
            echo
            case $yn in
                y)
                    ADDONS_EREC=1
                    ;;
                n)
                    ADDONS_EREC=0
                    ;;
            esac

            echo -e "${PROMPT}    Addons: Would you like to include the Blobifier purge system (requires Blobifier servo installation)${INPUT}"
            yn=$(prompt_yn "    Include blobifier.cfg")
            echo
            case $yn in
                y)
                    ADDONS_BLOBIFIER=1
                    ;;
                n)
                    ADDONS_BLOBIFIER=0
                    ;;
            esac
            ;;
        n)
            INSTALL_PRINTER_INCLUDES=no
            ;;
    esac

# Too verbose..
#    echo -e "${EMPHASIZE}"
#    echo -e "Summary of hardware config set by questionaire:${INFO}"
#    for var in $(set | grep '^_hw_' | cut -d '=' -f 1); do
#        short_name=$(echo "$var" | sed 's/^_hw_//')
#        eval "echo -e \"$short_name: \${$var}\""
#    done

    echo -e "${INFO}"
    echo "    vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv"
    echo
    echo "    NOTES:"
    echo "    What still needs to be done:"
    echo "    Edit mmu.cfg and mmu_hardware.cfg to get hardware correctly setup"
    if [ "${_hw_brd_type}" == "unknown" ]; then
        echo "        * Edit *.cfg files and substitute all missing pins"
    else
        echo "        * Review all pin configuration and change to match your mcu"
    fi
    echo "        * Verify motor current, especially if using non BOM motors"
    echo "        * Adjust motor direction with '!' on pin if necessary. No way to know here"
    echo "        * Adjust your config for loading and unloading preferences"
    echo -e "${WARNING}"
    echo "    Make sure you that you have these near the top of your printer.cfg:"
    echo "        # Happy Hare"
    echo "        [include mmu/base/*.cfg]"
    echo "        [include mmu/optional/client_macros.cfg]"
    echo "        [include mmu/addons/blobifier.cfg]"
    echo "        [include mmu/addons/mmu_erec_cutter.cfg]"
    if [ "${ADDONS_DC_ESPOOLER:-0}" -eq 1 ]; then
        echo "        [include mmu/addons/dc_espooler.cfg]"
    fi
    echo -e "${INFO}"
    echo "    Later:"
    echo "        * Tweak configurations like speed and distance in mmu_parameters.cfg"
    echo "        * Configure your operational preferences in mmu_macro_vars.cfg"
    echo 
    echo "    Good luck! MMU is complex to setup. Remember Discord is your friend.."
    echo
    echo "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^"
    echo
}

usage() {
    echo -e "${EMPHASIZE}"
    echo "Usage: $0 [-i] [-e] [-d] [-z] [-s]"
    echo "                    [-b <branch>] [-k <klipper_home_dir>] [-c <klipper_config_dir>] [-m <moonraker_home_dir>]"
    echo "                    [-a <kiauh_alternate_klipper>] [-r <repetier_server stub>]"
    echo
    echo "-i for interactive install"
    echo "-e for install of default starter config files for manual configuration"
    echo "-d for uninstall"
    echo "-z skip github update check (nullifies -b <branch>)"
    echo "-s to skip restart of services"
    echo "-b to switch to specified feature branch (sticky)"
    echo "-k <dir> to specify location of non-default klipper home directory"
    echo "-c <dir> to specify location of non-default klipper config directory"
    echo "-m <dir> to specify location of non-default moonraker home directory"
    echo "-r specify Repetier-Server <stub> to override printer.cfg and klipper.service names"
    echo "-a <name> to specify alternative klipper-service-name when installed with Kiauh"
    echo "(no flags for safe re-install / upgrade)"
    echo
    exit 1
}

# Find SRCDIR from the pathname of this script
SRCDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )"/ && pwd )"

# Defaults for first time run without running interview/questionaire
_hw_brd_type="unknown"
_hw_serial="/dev/serial/by-id/XXX"
_hw_num_gates=12
_hw_mmu_vendor=Custom
_hw_mmu_version=1.0
_hw_selector_type=LinearSelector
_hw_variable_bowden_lengths=1
_hw_variable_rotation_distances=1
_hw_encoder_resolution=1.0
SETUP_SELECTOR_TOUCH=no
SETUP_LED=yes
HAS_ENCODER=yes
HAS_SELECTOR=yes
ADDONS_EREC=0
ADDONS_BLOBIFIER=0
ADDONS_DC_ESPOOLER=0

INSTALL=0
UNINSTALL=0
NOSERVICE=0
STARTER=0
PRINTER_CONFIG=printer.cfg
KLIPPER_SERVICE=klipper.service

while getopts "a:b:k:c:m:r:idsze" arg; do
    case $arg in
        a) KLIPPER_SERVICE=${OPTARG}.service;;
        b) N_BRANCH=${OPTARG};;
        k) KLIPPER_HOME=${OPTARG};;
        m) MOONRAKER_HOME=${OPTARG};;
        c) KLIPPER_CONFIG_HOME=${OPTARG};;
        r) PRINTER_CONFIG=${OPTARG}.cfg
           KLIPPER_SERVICE=klipper_${OPTARG}.service
           echo "Repetier-Server <stub> specified. Over-riding printer.cfg to [${PRINTER_CONFIG}] & klipper.service to [${KLIPPER_SERVICE}]"
           ;;
        i) INSTALL=1;;
        d) UNINSTALL=1;;
        e) STARTER=1;;
        s) NOSERVICE=1;;
        z) SKIP_UPDATE=1;;
        *) usage;;
    esac
done

if [ "${INSTALL}" -eq 1 -a "${UNINSTALL}" -eq 1 ]; then
    echo -e "${ERROR}Can't install and uninstall at the same time!"
    usage
fi

verify_not_root
[ -z "${SKIP_UPDATE}" ] && {
    self_update # Make sure the repo is up-to-date on correct branch
}
check_octoprint
verify_home_dirs
check_klipper
cleanup_old_klippy_modules

if [ "$UNINSTALL" -eq 0 ]; then
    if [ "${INSTALL}" -eq 1 ]; then
        echo -e "${TITLE}$(get_logo "Happy Hare interactive installer...")"
        read_default_config  # Parses template file parameters into memory
        questionaire         # Update in memory parameters from questionaire

        if [ "${INSTALL_PRINTER_INCLUDES}" == "yes" ]; then
            install_printer_includes
        fi
    else
        hardware_config="${KLIPPER_CONFIG_HOME}/mmu/base/mmu_hardware.cfg"
        if [ -f "${hardware_config}" ]; then
            echo -e "${TITLE}$(get_logo "Happy Hare upgrading previous install...")"
            read_previous_mmu_type # Get MMU type info first
            read_default_config  # Parses template file parameters into memory
            read_previous_config # Update in memory parameters from previous install
        elif [ "${STARTER}" -eq 0 ]; then
            echo -e "${ERROR}Nothing to upgrade. If you want a new install run with '-i' (interactive) or '-e' (empty config)"
            usage
        else
            # Starter blank install
            echo -e "${TITLE}$(get_logo "Happy Hare generating skeletal config...")"
            read_default_config  # Parses template file parameters into memory
        fi
    fi

    # Important to update version
    FROM_VERSION=${_param_happy_hare_version}
    if [ ! "${FROM_VERSION}" == "" ]; then
        downgrade=$(awk -v to="$VERSION" -v from="$FROM_VERSION" 'BEGIN {print (to < from) ? "1" : "0"}')
        bad_v2v3=$(awk -v to="$VERSION" -v from="$FROM_VERSION" 'BEGIN {print (from < 2.70 && to >= 3.0) ? "1" : "0"}')
        if [ "$downgrade" -eq 1 ]; then
            echo -e "${WARNING}Trying to update from version ${FROM_VERSION} to ${VERSION}"
            echo -e "${ERROR}Automatic 'downgrade' to earlier version is not garanteed. If you encounter startup problems you may"
            echo -e "${ERROR}need to manually compare the backed-up 'mmu_parameters.cfg' with current one to restore differences"
        elif [ "$bad_v2v3" -eq 1 ]; then
            echo -e "${ERROR}Cannot automatically 'upgrade' from version ${FROM_VERSION} to ${VERSION}..."
            echo -e "${ERROR}Please upgrade to v2.7.0 or later before attempting v3.0 upgrade"
            exit 1
        elif [ ! "${FROM_VERSION}" == "${VERSION}" ]; then
            echo -e "${WARNING}Upgrading from version ${FROM_VERSION} to ${VERSION}..."
        fi
    fi
    _param_happy_hare_version=${VERSION}

    # Copy config files updating from in memory parmameters or h/w settings
    copy_config_files

    # Special upgrades of mmu_hardware.cfg
    upgrade_mmu_hardware

    # Link in new components
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
    if [ "$VERSION" == "2.70" -a "$FROM_VERSION" != "2.70" ]; then
        restart_moonraker
    fi
    restart_klipper
else
    echo -e "${WARNING}Klipper not restarted automatically because you need to validate and complete config first"
fi

if [ "$UNINSTALL" -eq 1 ]; then
    echo -e "${EMPHASIZE}"
    echo "Done.  Sad to see you go (but maybe you'll be back)..."
    echo -e "${sad_logo}"
else
    echo -e "${TITLE}Done."
    echo -e "$(get_logo "Happy Hare ${F_VERSION} Ready...")"
fi
