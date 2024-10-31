#!/usr/bin/env bash
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

shopt -s extglob

source "${KCONFIG_CONFIG}"

VERSION=${CONFIG_VERSION} # Important: Keep synced with mmy.py

set -e # Exit immediately on error

# scoped paramaters, will have the following layout:
# [SECTION],PARAM = VALUE
declare -A PARAMS

# Screen Colors
OFF='\033[0m'       # Text Reset
BLACK='\033[0;30m'  # Black
RED='\033[0;31m'    # Red
GREEN='\033[0;32m'  # Green
YELLOW='\033[0;33m' # Yellow
BLUE='\033[0;34m'   # Blue
PURPLE='\033[0;35m' # Purple
CYAN='\033[0;36m'   # Cyan
WHITE='\033[0;37m'  # White

B_RED='\033[1;31m'    # Bold Red
B_GREEN='\033[1;32m'  # Bold Green
B_YELLOW='\033[1;33m' # Bold Yellow
B_CYAN='\033[1;36m'   # Bold Cyan
B_WHITE='\033[1;37m'  # Bold White

TITLE="${B_WHITE}"
DETAIL="${BLUE}"
INFO="${CYAN}"
EMPHASIZE="${B_CYAN}"
ERROR="${B_RED}"
WARNING="${B_YELLOW}"
DIM="${PURPLE}"

log_color() {
    for line in "${@:2}"; do
        echo -e "${1}${line}${OFF}"
    done
}

log_warning() {
    log_color "${WARNING}" "$@"
}

log_error() {
    log_color "${ERROR}" "$@"
    exit 1
}

log_info() {
    log_color "${INFO}" "$@"
}

get_logo() {
    caption=$1
    cat <<EOF
 
(\_/)
( *,*)
(")_(") ${caption}

EOF
}

sad_logo=$(
    cat <<EOF

(\_/)
( v,v)
(")^(") Very Unhappy Hare

EOF
)

### Helper functions
calc() {
    awk "BEGIN { print $* }"
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

param_change_var() {
    local section=$1
    local from_var=$2
    local to_var=$3

    for key in "${!PARAMS[@]}"; do
        if [[ "${key}" =~ ^"${section}","${from_var}" ]]; then
            param_change_key "${key}" "${section},${to_var}"
        fi
    done
}
param_change_section() {
    local from_section=$1
    local to_section=$2

    for key in "${!PARAMS[@]}"; do
        if [[ "${key}" =~ ^"${from_section}", ]]; then
            param_change_key "${key}" "${to_section},${key##*,}"
        fi
    done
}

param_change_key() {
    local from=$1
    local to=$2

    PARAMS["${to}"]="${PARAMS[${from}]}"
    unset "PARAMS[${from}]"
}

param() {
    local section=$1
    local param=$2
    echo "${PARAMS["${section},${param}"]}"
}

param_keys_for() {
    local section=$1
    for key in "${!PARAMS[@]}"; do
        if [[ "${key}" =~ ^"${section}", ]]; then
            echo "${key}"
        fi
    done
}

copy_param() {
    local from_section=$1
    local from_param=$2
    local to_section=$3
    local to_param=$4

    set_param "${to_section}" "${to_param}" "$(param "${from_section}" "${from_param}")"
}

set_param() {
    local section=$1
    local param=$2
    local value=$3
    PARAMS["${section},${param}"]="${value}"
}

has_param() {
    local section=$1
    local param=$2
    [ -n "${PARAMS["${section},${param}"]}" ]
}

remove_param() {
    local section=$1
    local param=$2
    unset "PARAMS[${section},${param}]"
}

per_section() {
    local start_section=$1
    local end_section=$2
    local sed_cmd=$3
    local in_situ=$4
    local file=$5

    if ! grep -q "${start_section}" "${file}"; then
        log_warning "$start_section not found in ${file} - skipping"
    else
        local params=""
        if [ "${in_situ}" == "y" ]; then
            params+="-i"
        else
            params+="-n"
        fi
        sed ${params} "
			/^${start_section}/,/^${end_section}/ { // ! ${sed_cmd}; /${start_section}/ ${sed_cmd}; }
		" "${file}"
    fi
}

# Delete every line from start_section up to end_section (exclusive),
delete_section() {
    local start_section=$1
    local end_section=$2
    local file=$3

    per_section "${start_section}" "${end_section}" "d" "y" "${file}"
}

insert_after_section() {
    local start_section=$1
    local end_section=$2
    local insert="${3//$'\n'/\\ \\n}" # Add command split at every newline
    local file=$4

    sed -i "/^${start_section}/,/^${end_section}/!b; /^${end_section}/ a ${insert}" "${file}"
}

comment_section() {
    local start_section=$1
    local end_section=$2
    local file=$3

    per_section "${start_section}" "${end_section}" "s/^\([^#]\)/#\1/" "y" "${file}"
}

uncomment_section() {
    local start_section=$1
    local end_section=$2
    local file=$3

    per_section "${start_section}" "${end_section}" "s/^#//" "y" "${file}"
}

select_section() {
    local start_section=$1
    local end_section=$2
    local file=$3

    per_section "${start_section}" "${end_section}" "p" "n" "${file}"
}

replace_placeholder() {
    local placeholder=$1
    local value=$2
    local file=$3

    sed -i "s|{${placeholder}}|${value}|g" "${file}"
}

### CFG Processing fucntions

parse_file() {
    local file=$1
    local prefix_filter=$2

    if [ ! -f "${file}" ]; then
        log_error "Trying to parse '${file}' but it doesn't exist"
    fi

    # Read old config files
    local current_section=""
    while IFS="" read -r line; do
        # Remove comments and config sections
        local line="${line%%[#\;]*}"
        line="${line##*([[:space:]])}" # Remove leading whitespace
        line="${line%%*([[:space:]])}" # Remove leading whitespace

        if [[ "${line}" =~ ^\[ ]]; then
            # section
            current_section="${line}"
            continue
        fi

        if [[ ! "${line}" =~ : ]]; then
            # Line doesn't contain a parameter
            continue
        fi

        local parameter="${line%%:*}"            # Select parameter before :
        parameter="${parameter%%*([[:space:]])}" # Remove trailing whitespace

        if [ -z "${parameter}" ] || ! [[ "${parameter}" =~ ^(${prefix_filter}) ]]; then
            # Parameter is empty or doesn't match filter
            continue
        fi

        local value="${line#*:}" # Select value after :
        value="${value##*([[:space:]])}"

        if [ -n "${value}" ]; then
            # If parameter is one of interest and it has a value remember it
            set_param "${current_section}" "${parameter}" "${value}"
        fi

    done <"${file}"
}

update_file() {
    local dest=$1
    local prefix_filter=$2

    local current_section=""
    # Read the file line by line
    while IFS="" read -r line; do
        if [[ "${line}" =~ ^[[:space:]]*([#\;]|$) ]]; then
            # Empty line or comment, just copy
            echo "${line}" >>"${dest}.tmp"
            continue
        fi

        if [[ "${line}" =~ ^[[:space:]]*\[ ]]; then
            # section
            current_section="${line##*([[:space:]])}"
            current_section="${current_section%%*([[:space:]])?([#;]*)}"
            echo "${line}" >>"${dest}.tmp"
            continue
        fi

        local parameter="${line%%:*}"            # select parameter
        parameter="${parameter##*([[:space:]])}" # remove leading spaces
        parameter="${parameter%%*([[:space:]])}" # remove trailing spaces

        if [ -z "${parameter}" ] || [[ ! "${parameter}" =~ ^(${prefix_filter}) ]]; then
            # Parameter doesn't match filter
            echo "${line}" >>"${dest}.tmp"
            continue
        fi

        if ! has_param "${current_section}" "${parameter}"; then
            echo "${line}" >>"${dest}.tmp"
        fi

        local value="${line#*:}" # select value
        value="${value##*([[:space:]])}"
        value="${value%%*([[:space:]])?([#;]*)}" # remove trailing spaces and comments

        local new_value="$(param "${current_section}" "${parameter}")"
        remove_param "${current_section}" "${parameter}"

        if [ "${value}" == "${new_value}" ]; then
            # No change
            echo "${line}" >>"${dest}.tmp"
            continue
        fi

        log_info "Reusing parameter value from existing install: ${current_section} ${parameter} = ${new_value}"
        local comment="${line#"${line%%*([[:space:]])?([#;]*)}"}" # extract trailing space + comment
        echo "${parameter}: ${new_value}${comment}" >>"${dest}.tmp"

    done <"${dest}"
    mv "${dest}.tmp" "${dest}"
}

# Pull parameters from previous installation
read_previous_config() {
    for cfg in mmu_parameters.cfg mmu_hardware.cfg mmu_macro_vars.cfg; do
        local dest_cfg=${CONFIG_KLIPPER_CONFIG_HOME}/mmu/base/${cfg}
        if [ -f "${dest_cfg}" ]; then
            parse_file "${dest_cfg}"
        fi
    done

    # TODO namespace config in third-party addons separately
    if [ -d "${CONFIG_KLIPPER_CONFIG_HOME}/mmu/addons" ]; then
        for cfg in "${CONFIG_KLIPPER_CONFIG_HOME}"/mmu/addons/*.cfg; do
            if [[ ! "${cfg##*/}" =~ _hw ]]; then
                parse_file "${cfg}"
            fi
        done
    fi
}

process_upgrades() {
    # Upgrade / map / force old parameters...
    # v2.7.1
    local sec="[gcode_macro _MMU_CUT_TIP_VARS]"
    if has_param "${sec}" "variable_pin_park_x_dist"; then
        change_param_var "${sec}" "variable_pin_park_x_dist" "variable_pin_park_dist"
    fi

    if has_param "${sec}" "variable_pin_pin_loc_x_compressed"; then
        change_param_var "${sec}" "variable_pin_pin_loc_x_compressed" "variable_pin_loc_compressed"
    fi

    # TODO: this seems broken?
    # if [ -n "${variable_park_xy}" ]; then
    #     variable_park_toolchange="${variable_park_xy}, ${_param_z_hop_height_toolchange:-0}, 0, 2"
    #     variable_park_error="${variable_park_xy}, ${_param_z_hop_height_error:-0}, 0, 2"
    # fi
    #
    local sec="[gcode_macro _MMU_SEQUENCE_VARS]"
    if has_param "${sec}" "variable_lift_speed"; then
        change_param_var "${sec}" "variable_lift_speed" "variable_park_lift_speed"
    fi

    if has_param "${sec}" "variable_enable_park"; then
        if param "${sec}" "variable_enable_park" == "False"; then
            if param "${sec}" "variable_enable_park_runout" == "True"; then
                set_param "${sec}" "varriable_enable_park_printing" "'toolchange,load,unload,runout,pause,cancel'"
            else
                set_param "${sec}" "varriable_enable_park_printing" "'pause,cancel'"
            fi
        else
            set_param "${sec}" "varriable_enable_park_printing" "'toolchange,load,unload,pause,cancel'"
        fi
        remove_param "${sec}" "variable_enable_park"
    fi

    if has_param "${sec}" "variable_enable_park_standalone"; then
        if param "${sec}" "variable_enable_park_standalone" == "False"; then
            set_param "${sec}" "varriable_enable_park_standalone" "'pause,cancel'"
        else
            set_param "${sec}" "varriable_enable_park_standalone" "'toolchange,load,unload,pause,cancel'"
        fi
        remove_param "${sec}" "variable_enable_park_standalone"
    fi

    # v2.7.2
    if [ "$(param "[mmu]" "toolhead_residual_filament")" == "0" ] &&
        [ "$(param "[mmu]" "toolhead_ooze_reduction")" != "0" ]; then
        copy_param "[mmu]" "toolhead_residual_filament" "[mmu]" "toolhead_ooze_reduction"
        set_param "[mmu]" "toolhead_ooze_reduction" "0"
    fi

    # Blobifer update - Oct 13th 20204
    local sec="[gcode_macro BLOBIFIER]"
    if has_param "${sec}" "variable_iteration_z_raise"; then
        log_info "Setting Blobifier variable_z_raise and variable_purge_length_maximum from previous settings"
        local max_i_per_blob=$(param "${sec}" "variable_max_iterations_per_blob")
        local i_z_raise=$(param "${sec}" "variable_iterations_z_raise")
        local i_z_change=$(param "${sec}" "variable_iteration_z_change")
        local max_i_length=$(param "${sec}" "variable_max_iteration_length")
        local triangulated=$(calc "${max_i_per_blob} (${max_i_per_blob} - 1) / 2")

        set_param "${sec}" "variable_z_raise" "$(calc "${i_z_raise} * ${max_i_per_blob} - ${triangulated} * ${i_z_change}")"
        set_param "${sec}" "variable_purge_length_maximum" "$(calc "${max_i_length} * ${max_i_per_blob}")"
    fi

    if has_param "[mmu]" "mmu_num_gates"; then
        param_change_key "[mmu],mmu_num_gates" "[mmu_machine],num_gates"
    fi
    if has_param "[mmu]" "mmu_vendor"; then
        param_change_key "[mmu],mmu_vendor" "[mmu_machine],mmu_vendor"
    fi
    if has_param "[mmu]" "mmu_version"; then
        param_change_key "[mmu],mmu_version" "[mmu_machine],mmu_version"
    fi

    param_change_section "[mmu_servo mmu_servo]" "[mmu_servo selector_servo]"
}

copy_config_files() {
    # Now substitute tokens using given brd_type and Kconfig starting values

    local filename=${1##*/}
    local src=${1}
    local dest=${2}

    # Hardware files: Special token substitution -----------------------------------------
    if [ "${filename}" == "mmu.cfg" ] || [ "${filename}" == "mmu_hardware.cfg" ]; then
        local sed_expr=""
        for var in $(declare | grep '^CONFIG_HW_'); do
            local var=${var%%=*}
            local pattern="{${var#CONFIG_HW_}}"
            sed_expr+="s|${pattern,,}|${!var}|g; "
        done

        for var in $(declare | grep "^CONFIG_PIN_"); do
            local var=${var%%=*}
            local pattern="{${var#CONFIG_}}"
            sed_expr+="s|${pattern,,}|${!var}|g; "
        done

        sed_expr+="s|{pin_.*}||g; " # Remove any remaining unprocessed pin tokens

        # Correct shared uart_address for EASY-BRD
        if [ "${CONFIG_HW_MMU_BOARD_TYPE}" == "EASY-BRD" ]; then
            # Share uart_pin to avoid duplicate alias problem
            sed_expr+="s/^uart_pin: mmu:MMU_SEL_UART/uart_pin: mmu:MMU_GEAR_UART/; "
        else
            # Remove uart_address lines
            sed_expr+="/^uart_address:/ d; "
        fi

        cp --remove-destination "${src}" "${dest}"
        update_file "${dest}"
        sed -i "${sed_expr}" "${dest}"
    fi

    if [ "${filename}" == "mmu_hardware.cfg" ]; then
        if [ "${CONFIG_ENABLE_SELECTOR_TOUCH}" == "y" ]; then
            uncomment_section "#diag_pin: ^mmu:MMU_GEAR_DIAG" "$" "${dest}"
            uncomment_section "#extra_endstop_pins" "$" "${dest}"
            comment_section "uart_address" "" "${dest}"
        fi

        # Handle LED option - Comment out if disabled (section is last, go comment to end of file)
        if [ "${filename}" == "mmu_hardware.cfg" ] && [ "${CONFIG_ENABLE_LED}" != "y" ]; then
            comment_section "\[neopixel mmu_leds\]" "$" "${dest}"
            comment_section "\[mmu_leds\]" "$" "${dest}"
        fi

        # Handle Encoder option - Delete if not required (section is 25 lines long)
        if [ "${filename}" == "mmu_hardware.cfg" ] && [ "${CONFIG_MMU_HAS_ENCODER}" != "y" ]; then
            delete_section "# ENCODER" "# FILAMENT SENSORS" "${dest}"
        fi
        # Handle Selector options - Delete if not required (sections are 8 and 36 lines respectively)
        if [ "${filename}" == "mmu_hardware.cfg" ] && [ "${CONFIG_MMU_HAS_SELECTOR}" != "y" ]; then
            delete_section "# SELECTOR SERVO" "# OPTIONAL GANTRY" "${dest}"
            delete_section "# SELECTOR STEPPER" "# SERVOS" "${dest}"

            additional_gear_section_tmc="$(select_section "\[tmc2209 stepper_mmu_gear_1\]" "$" "${dest}")\n"
            additional_gear_section_stepper="$(select_section "\[stepper_mmu_gear_1\]" "$" "${dest}")\n"

            gear_sections=""
            for ((i = 2; i <= $(calc "${CONFIG_HW_NUM_GATES} - 1"); i++)); do
                gear_sections+="${additional_gear_section_tmc//_1/_${i}}\n"
                gear_sections+="${additional_gear_section_stepper//_1/_${i}}\n"
            done

            insert_after_section "\[stepper_mmu_gear_1\]" "$" "${gear_sections}" "${dest}"
        else
            # Delete additional gear drivers template section
            delete_section "# ADDITIONAL FILAMENT DRIVE" "# SELECTOR STEPPER" "${dest}"
        fi

    fi

    # Conifguration parameters -----------------------------------------------------------
    if [ "${filename}" == "mmu_parameters.cfg" ]; then
        cp --remove-destination "${src}" "${dest}"
        update_file "${dest}"
        # Ensure that supplemental user added params are retained. These are those that are
        # by default set internally in Happy Hare based on vendor and version settings but
        # can be overridden.  This set also includes a couple of hidden test parameters.
        {
            echo ""
            echo "# SUPPLEMENTAL USER CONFIG retained after upgrade --------------------------------------------------------------------"
            echo "#"
            local supplemental_params="cad_gate0_pos cad_gate_width cad_bypass_offset cad_last_gate_offset cad_block_width cad_bypass_block_width cad_bypass_block_delta cad_selector_tolerance gate_material gate_color gate_spool_id gate_status gate_filament_name gate_temperature gate_speed_override endless_spool_groups tool_to_gate_map"
            local hidden_params="test_random_failures test_random_failures test_disable_encoder test_force_in_print serious"

            for key in $(param_keys_for "[mmu]"); do
                local param=${key#*,}
                for item in ${supplemental_params} ${hidden_params}; do
                    if [ "${item}" == "${param}" ]; then
                        echo "${param}: ${PARAMS[$key]}"
                        unset "PARAMS[$key]"
                    fi
                done
            done
        } >>"${dest}"

        # If any params are still left warn the user because they will be lost (should have been upgraded)
        for key in $(param_keys_for "[mmu]"); do
            log_warning "Following parameter is deprecated and has been removed: [mmu] ${key#*,} = ${PARAMS[$key]}"
        done
    fi

    # Variables macro ---------------------------------------------------------------------
    if [ "${filename}" == "mmu_macro_vars.cfg" ]; then
        cp --remove-destination "${src}" "${dest}" # Overwrite the symbolic link first
        local tx_macros=""
        if has_param "[mmu_machine]" "num_gates"; then
            num_gates=$(param "[mmu_machine]" "num_gates")
        else
            num_gates=${CONFIG_HW_NUM_GATES}
        fi

        for ((i = 0; i <= $(calc "${num_gates} - 1"); i++)); do
            tx_macros+="[gcode_macro T${i}]\n"
            tx_macros+="gcode: MMU_CHANGE_TOOL TOOL=${i}\n"
        done

        replace_placeholder "tx_macros" "${tx_macros}" "${dest}"
        update_file "${dest}"
    fi
    # done

    if [ "${filename}" == "mmu_vars.cfg" ]; then
        cp --remove-destination "${src}" "${dest}"
    fi

    # Addon config files are always copied (and updated) so they can be edited ----------------
    # Skipping files with 'my_' prefix for development
    if [[ ${src#"${OUT}"/config/} =~ ^addons ]]; then
        # for file in config/addons/*.cfg; do
        if [[ ! "${filename}" =~ (^my_|_hw\.cfg.*$) ]]; then
            cp --remove-destination "${src}" "${dest}" # Overwrite the symbolic link first
            log_info "Installing configuration file ${file}"
            update_file "${dest}"
        fi

    fi
}

install_include() {
    local include=$1
    local dest=$2

    if ! grep -q "\[include ${include}\]" "${dest}"; then
        sed -i "1i \[include ${include}\]" "${dest}"
    fi
}

uninstall_include() {
    local include=$1
    local dest=$2

    if grep -q "\[include ${include}\]" "${dest}"; then
        sed -i "/\[include ${include}\]/ d" "${dest}"
    fi
}

# Link in all includes if not already present
install_printer_includes() {
    if [ "${CONFIG_INSTALL_INCLUDES}" != "y" ]; then
        uninstall_printer_includes "$1"
        return
    fi

    log_info "Installing MMU references in ${CONFIG_PRINTER_CONFIG}"
    local dest=$1

    if grep -q "\[include config/hardware/mmu.cfg\]" "${dest}"; then
        log_warning "This looks like a Klippain config installation - skipping automatic config install. Please add config includes by hand"
        return
    fi

    if [ "${CONFIG_INSTALL_12864_MENU}" == "y" ]; then
        install_include "mmu/optional/mmu_menu.cfg" "${dest}"
    else
        uninstall_include "mmu/optional/mmu_menu.cfg" "${dest}"
    fi

    if [ "${CONFIG_INSTALL_CLIENT_MACROS}" == "y" ]; then
        install_include "mmu/optional/client_macros.cfg" "${dest}"
    else
        uninstall_include "mmu/optional/client_macros.cfg" "${dest}"
    fi

    if [ "${CONFIG_INSTALL_EREC_CUTTER}" == "y" ]; then
        install_include "mmu/addons/mmu_erec_cutter.cfg" "${dest}"
    else
        uninstall_include "mmu/addons/mmu_erec_cutter.cfg" "${dest}"
    fi

    if [ "${CONFIG_INSTALL_BLOBIFIER}" == "y" ]; then
        install_include "mmu/addons/blobifier.cfg" "${dest}"
    else
        uninstall_include "mmu/addons/blobifier.cfg" "${dest}"
    fi

    if [ "${CONFIG_INSTALL_RESPOOLER}" == "y" ]; then
        install_include "mmu/addons/dc_respooler.cfg" "${dest}"
    else
        uninstall_include "mmu/addons/dc_respooler.cfg" "${dest}"
    fi

    install_include "mmu/base/\*.cfg" "${dest}"
}

uninstall_printer_includes() {
    log_info "Cleaning MMU references from ${CONFIG_PRINTER_CONFIG}"
    local dest=$1 #${CONFIG_KLIPPER_CONFIG_HOME}/${CONFIG_PRINTER_CONFIG}

    for include in \
        'mmu/optional/mmu_menu.cfg' \
        'mmu/optional/mmu_ercf_compat.cfg' \
        'mmu/optional/client_macros.cfg' \
        'mmu/addons/mmu_erec_cutter.cfg' \
        'mmu/addons/blobifier.cfg' \
        'mmu/addons/respooler.cfg' \
        'mmu_software.cfg' \
        'mmu_sequence.cfg' \
        'mmu_cut_tip.cfg' \
        'mmu_form_tip.cfg' \
        'mmu_parameters.cfg' \
        'mmu_hardware.cfg' \
        'mmu_filametrix.cfg' \
        'mmu.cfg' \
        'mmu/base/\*.cfg' \
        'mmu/addons/\*.cfg'; do
        uninstall_include "${include}" "${dest}"
    done
}

install_update_manager() {
    log_info "Adding update manager to moonraker.conf"

    local dest=$1

    if ! grep -q "\[update_manager happy-hare\]" "${dest}"; then
        echo "" >>"${dest}"
        cat "${SRC}/moonraker_update.txt" >>"${dest}"
        replace_placeholder "happy_hare_home" "${CONFIG_HAPPY_HARE_HOME}" "${dest}"
    fi

    # Quick "catch-up" update for ne mmu_service
    if ! grep -q "\[mmu_server\]" "${dest}"; then
        echo -e "$(select_section "\[mmu_server\]" "$" "${SRC}/moonraker_update.txt")\n" >>"${dest}"
    fi

    # Quick "catch-up" update for new toolchange_next_pos pre-processing
    if ! grep -q "enable_toolchange_next_pos" "${dest}"; then
        log_warning "Added new 'enable_toolchange_next_pos' to moonraker.conf"
        insert_after_section "\[mmu_server\]" "" "enable_toolchange_next_pos: True" "${dest}"
    fi

    if ! grep -q "update_spoolman_location" "${dest}"; then
        log_warning "Added new 'update_spoolman_location' to moonraker.conf"
        insert_after_section "\[mmu_server\]" "" "update_spoolman_location: True" "${dest}"
    fi
}

uninstall_update_manager() {
    log_info "Removing update manager from moonraker.conf"
    local dest=$1
    for section in "update_manager happy-hare" "mmu_server"; do
        if ! grep -q "${section}" "${file}"; then
            log_info "[$section] not found in moonraker.conf - skipping removal"
        else
            delete_section "\[${section}\]" "$" "${dest}"
        fi
    done
}

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

    if [ -n "${N_BRANCH}" ] && [ "${BRANCH}" != "${N_BRANCH}" ]; then
        BRANCH=${N_BRANCH}
        echo -e "${B_GREEN}Switching to '${BRANCH}' branch"
        RESTART=1
    fi

    if [ -n "${RESTART}" ]; then
        git checkout "$BRANCH" --quiet
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

restart_service() {
    name=$1
    service=$2

    log_info "Restarting ${name}..."

    if [ -z "${service}" ]; then
        log_warning "No ${name} service specified - Please restart by hand"
        return
    fi

    if [ "${CONFIG_IS_MIPS}" == "y" ]; then
        if [ -e "/etc/init.d/${service}" ]; then
            set +e
            /etc/init.d/"${service}" restart
            set -e
        else
            log_warning "Service '${service}' not found! Restart manually or check your config"
        fi
    else
        if systemctl list-unit-files "${service}" >/dev/null; then
            systemctl restart "${service}" 2>/dev/null
        else
            log_warning "Service '${service}' not found! Restart manually or check your config"
        fi
    fi
}

restart_klipper() {
    restart_service "Klipper" "${CONFIG_SERVICE_KLIPPER}"
}

restart_moonraker() {
    restart_service "Moonraker" "${CONFIG_SERVICE_MOONRAKER}"
}

# These parameters are too complex to encode with Kconfig.
set_extra_parameters() {
    set_param "[mmu]" "happy_hare_version" "${CONFIG_VERSION}"

    CONFIG_HW_MMU_VERSION=${CONFIG_HW_MMU_BASE_VERSION}
    if [ "${CONFIG_HW_MMU_TYPE_ERCF_V1_1}" == "y" ]; then
        [ "${CONFIG_HW_EXT_SPRINGY}" == "y" ] && CONFIG_HW_MMU_VERSION+="s"
        [ "${CONFIG_HW_EXT_BINKY}" == "y" ] && CONFIG_HW_MMU_VERSION+="b"
        [ "${CONFIG_HW_EXT_TRIPLE_DECK}" == "y" ] && CONFIG_HW_MMU_VERSION+="t"
    elif [ "${CONFIG_HW_MMU_TYPE_TRADRACK}" == "y" ]; then
        [ "${CONFIG_HW_EXT_BINKY}" == "y" ] && CONFIG_HW_MMU_VERSION+="e"
    fi

    CONFIG_HW_NUM_LEDS=$(calc "${CONFIG_HW_NUM_GATES} * 2 + 1")
    CONFIG_HW_NUM_LEDS_MINUS1=$(calc "${CONFIG_HW_NUM_LEDS} - 1")
    CONFIG_HW_NUM_GATES_PLUS1=$(calc "${CONFIG_HW_NUM_GATES} + 1")
}

build() {
    local src=$1
    local out=$2

    log_info "Building file ${out}..."

    set_extra_parameters
    read_previous_config
    process_upgrades
    copy_config_files "$src" "$out"
}

uninstall() {
    uninstall_update_manager "${CONFIG_KLIPPER_CONFIG_HOME}/moonraker.conf"
    uninstall_printer_includes "${CONFIG_KLIPPER_CONFIG_HOME}/${CONFIG_PRINTER_CONFIG}"
}

check_version() {
    parse_file "${CONFIG_KLIPPER_CONFIG_HOME}/mmu/base/mmu_parameters.cfg" "happy_hare_version"
    # Important to update version
    FROM_VERSION=$(param "[mmu]" "happy_hare_version")
    if [ ! "${FROM_VERSION}" == "" ]; then
        downgrade=$(awk -v to="$CONFIG_VERSION" -v from="$FROM_VERSION" 'BEGIN {print (to < from) ? "1" : "0"}')
        bad_v2v3=$(awk -v to="$CONFIG_VERSION" -v from="$FROM_VERSION" 'BEGIN {print (from < 2.70 && to >= 3.0) ? "1" : "0"}')
        if [ "$downgrade" -eq 1 ]; then
            log_warning "Trying to update from version ${FROM_VERSION} to ${CONFIG_VERSION}" \
                "Automatic 'downgrade' to earlier version is not garanteed. If you encounter startup problems you may" \
                "need to manually compare the backed-up 'mmu_parameters.cfg' with current one to restore differences"
        elif [ "$bad_v2v3" -eq 1 ]; then
            log_error "Cannot automatically 'upgrade' from version ${FROM_VERSION} to ${CONFIG_VERSION}..." \
                "${ERROR}Please upgrade to v2.7.0 or later before attempting v3.0 upgrade"
        elif [ ! "${FROM_VERSION}" == "${CONFIG_VERSION}" ]; then
            log_warning "Upgrading from version ${FROM_VERSION} to ${CONFIG_VERSION}..."
        fi
    fi
    set_param "[mmu]" "happy_hare_version" "${CONFIG_VERSION}"
}

print_happy_hare() {
    log_info "$(get_logo "Happy Hare ${CONFIG_F_VERSION} Ready...")"
}

print_unhappy_hare() {
    log_info "${sad_logo}"
}

print_params() {
    read_previous_config
    for key in "${!PARAMS[@]}"; do
        echo "${key%,*} ${key#*,}: ${PARAMS[${key}]}"
    done
}

case $1 in
build)
    build "$2" "$3"
    ;;
install-includes)
    install_printer_includes "$2"
    ;;
install-update-manager)
    install_update_manager "$2"
    ;;
update)
    self_update
    ;;
uninstall)
    uninstall
    ;;
restart-klipper)
    restart_klipper
    ;;
restart-moonraker)
    restart_moonraker
    ;;
print-happy-hare)
    print_happy_hare
    ;;
print-unhappy-hare)
    print_unhappy_hare
    ;;
check-version)
    check_version
    ;;
print-params)
    print_params
    ;;
*)
    log_error "
The install and update method for Happy Hare has changed, please use make and the following commands to install, update or uninstall Happy Hare.

For a new install:
    make menuconfig
    make install

To update Happy Hare:
    make menuconfig # Optional if you don't want to change any settings you can skip this step
    make update

To uninstall Happy Hare:
    make uninstall
"
    ;;
esac
