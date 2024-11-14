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
source "${SRC}/scripts/upgrades.sh"

set -e # Exit immediately on error

# scoped paramaters, will have the following layout:
# [SECTION],PARAM = VALUE
declare -A PARAMS
# These will be the same as above but not stripped of comments and whitespace
declare -A PARAMS_UNSTRIPPED
# Keep track of where the parameters came from to detect deprecated parameters
declare -A SECTION_ORIGIN

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
    local line
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

log_git() {
    log_color "${B_GREEN}" "$@"
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

# Change parameter name of section $1 from $2 to $3
# e.g. param_change_var "[section]" "from_param" "to_param"
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

# Move all parameters in section $1 to section $2
# e.g. param_change_section "[from_section]" "[to_section]"
param_change_section() {
    local from_section=$1
    local to_section=$2

    for key in "${!PARAMS[@]}"; do
        if [[ "${key}" =~ ^"${from_section}", ]]; then
            param_change_key "${key}" "${to_section},${key##*,}"
        fi
    done
}

# Change key $1 to $2
# e.g. param_change_key "[from_section],from_param" "[to_section],to_param"
param_change_key() {
    local from=$1
    local to=$2

    # only change if the key exists (even if empty)
    if [ -n "${PARAMS["${from}"]+x}" ]; then
        PARAMS["${to}"]="${PARAMS[${from}]}"
        unset "PARAMS[${from}]"
    fi
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

# check if a parameter is defined, an empty string returns true
has_param() {
    local section=$1
    local param=$2
    [ -n "${PARAMS["${section},${param}"]+x}" ]
}

has_param_section() {
    local section=$1
    for key in "${!PARAMS[@]}"; do
        if [[ "${key}" =~ ^"${section}", ]]; then
            return 0
        fi
    done
    return 1
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
			\%^${start_section}%,\%^${end_section}% { // ! ${sed_cmd}; \%${start_section}% ${sed_cmd}; }
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
delete_line() {
    local line_select=$1
    local file=$2

    delete_section "${line_select}" "" "${file}"
}

insert_after_section() {
    local start_section=$1
    local end_section=$2
    local insert="${3//$'\n'/\\ \\n}" # Add command split at every newline for sed
    local file=$4

    sed -i -e "/^${start_section}/,/^${end_section}/ !b; /^${start_section}/ b; $ b a; /^${end_section}/ b i; b" \
        -e ":a a\\${insert}" \
        -e "b" \
        -e ":i i\\${insert}" \
        "${file}"
}

# Selects a section from start_section to end_section (exclusive) and duplicates it end_range - start_range times replacing any start_range number with the current range number
duplicate_section() {
    local start_section=$1
    local end_section=$2
    local start_range=$3
    local end_range=$4
    local file=$5

    local section_to_duplicate="$(select_section "${start_section}" "${end_section}" "${file}")"
    local duplicated_sections=""
    local suffix="\\n"
    if [ -n "${end_section}" ]; then
        # A multiline section, instead of a single line. So add an extra newline
        suffix+="\\n"
        duplicated_sections+="\\n"
    fi

    for ((i = start_range + 1; i <= end_range; i++)); do
        duplicated_sections+="${section_to_duplicate//${start_range}/${i}}${suffix}"
    done

    duplicated_sections="${duplicated_sections%%*(\\n)}" # Remove last newlines

    insert_after_section "${start_section}" "${end_section}" "${duplicated_sections}" "${file}"
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

### .cfg Processing fucntions

parse_file() {
    local file=$1
    local prefix_filter=$2

    if [ ! -f "${file}" ]; then
        log_error "Trying to parse '${file}' but it doesn't exist"
    fi

    set_parameter() {
        if [ -z "${current_parameter}" ]; then
            return
        fi

        current_value="${current_value%$'\n\t'}"
        set_param "${current_section}" "${current_parameter}" "${current_value}"
        PARAMS_UNSTRIPPED["${current_section},${current_parameter}"]="${current_lines%$'\n'}"
        SECTION_ORIGIN["${current_section}"]="${file##*/}"
    }

    local current_section
    local current_parameter
    local current_value
    local current_lines
    local line

    # Read old config files
    while IFS="" read -r line; do

        if [ -z "${line%%*([[:space:]])?([#\;]*)}" ]; then
            current_lines+=${line}$'\n'
            # Empty line
            continue
        fi

        if [[ "${line}" =~ ^\[.*\] ]]; then
            # section
            set_parameter
            current_section="${line%%*([[:space:]])?([#;]*)}"
            current_parameter=
            current_value=
            current_lines=
            continue
        fi

        if [ -z "${current_section}" ]; then
            # No section yet, everything is irrelevant until we do
            continue
        fi

        if [[ "${line}" =~ ^[[:alnum:]_]+[[:space:]]*: ]]; then
            # parameter
            set_parameter
            current_parameter="${line%%*([[:space:]]):*}" # Select parameter before :
            current_value=
            current_lines=

            if [[ "${current_section}" =~ ^\[gcode_macro ]] &&
                [[ ! "${current_parameter}" =~ ^variable_ ]]; then
                # skip everything that is not a variable in gcode macros
                current_parameter=
            fi

            if [[ ! "${current_parameter}" =~ ^(${prefix_filter}) ]]; then
                # We skip these parameters
                current_parameter=
            fi
        fi

        if [ -z "${current_parameter}" ]; then
            # no parameter yet, so not relevant
            continue
        fi

        # part of a possibly multiline value
        local value="${line%%*([[:space:]])?([#\;]*)}" # Strip spaces and comments
        value="${value#*:}"                            # Select value after :
        value="${value##*([[:space:]])}"               # Strip leading spaces
        if [ -n "${value}" ]; then
            current_value+=${value}$'\n\t'
        fi
        current_lines+=${line}$'\n'

    done <"${file}"

    # after reaching end of file, set the last known parameter
    set_parameter
}

update_file() {
    local dest=$1
    local prefix_filter=$2

    check_parameter() {
        if [ -z "${current_parameter}" ]; then
            return
        fi

        current_value="${current_value%$'\n\t'}"
        local existing_value="$(param "${current_section}" "${current_parameter}")"
        remove_param "${current_section}" "${current_parameter}"
        local unstripped_value="${PARAMS_UNSTRIPPED["${current_section},${current_parameter}"]}"
        unset "PARAMS_UNSTRIPPED[${current_section},${current_parameter}]"

        # Compare stripped parameter values
        if [ "${current_value}" == "${existing_value}" ]; then
            # No change, just print as is. This will remove any comments the user might have added
            echo "${current_lines%$'\n'}" >>"${dest}.tmp"
            return
        fi

        if [ "${#existing_value}" -gt 50 ]; then
            local print_value="'${existing_value:0:50}'..."
        else
            local print_value="'${existing_value}'"
        fi

        log_info "Reusing parameter value from existing install: ${current_section} ${current_parameter} = ${print_value}"
        echo "${unstripped_value}" >>"${dest}.tmp"
    }

    local current_section
    local current_parameter
    local current_value
    local current_lines
    local line

    # Read the file line by line
    while IFS="" read -r line; do
        if [[ "${line}" =~ ^\[.*\] ]]; then
            # section
            check_parameter
            current_section="${line%%*([[:space:]])?([#;]*)}"
            current_parameter=
            current_value=
            current_lines=
            echo "${line}" >>"${dest}.tmp"
            continue
        fi

        if [ -z "${current_section}" ]; then
            # No section yet, everything is irrelevant until we do
            echo "${line}" >>"${dest}.tmp"
            continue
        fi

        if [[ "${line}" =~ ^[[:alnum:]_]+[[:space:]]*: ]]; then
            check_parameter
            current_parameter="${line%%*([[:space:]]):*}" # Select parameter before
            current_value=
            current_lines=

            if [[ "${current_section}" = ^\[gcode_macro ]] &&
                [[ ! "${current_parameter}" = ^variable_ ]]; then
                # skip everything that is not a variable in gcode macros
                current_parameter=
            fi

            if [[ ! "${current_parameter}" =~ ^(${prefix_filter}) ]] ||
                ! has_param "${current_section}" "${current_parameter}"; then
                # We skip these parameters as they should never be overwritten
                current_parameter=
            fi
        fi

        if [ -z "${current_parameter}" ]; then
            # no parameter yet, so just print the line
            echo "${line}" >>"${dest}.tmp"
            continue
        fi

        # part of a possibly multiline value
        local value="${line%%*([[:space:]])?([#\;]*)}" # Strip spaces and comments
        value="${value#*:}"                            # Select value after :
        value="${value##*([[:space:]])}"               # Strip leading spaces

        if [ -n "${value}" ]; then
            current_value+=${value}$'\n\t'
        fi
        current_lines+=${line}$'\n'
    done <"${dest}"

    # after reaching end of file, check the last known parameter
    check_parameter
    mv "${dest}.tmp" "${dest}"
}

# Pull parameters from previous installation
read_previous_config() {
    for cfg in mmu.cfg mmu_parameters.cfg mmu_hardware.cfg mmu_macro_vars.cfg; do
        local dest_cfg=${CONFIG_KLIPPER_CONFIG_HOME}/mmu/base/${cfg}
        if [ -f "${dest_cfg}" ]; then
            parse_file "${dest_cfg}"
        fi
    done

    # TODO namespace config in third-party addons separately
    if [ -d "${CONFIG_KLIPPER_CONFIG_HOME}/mmu/addons" ]; then
        for cfg in "${CONFIG_KLIPPER_CONFIG_HOME}"/mmu/addons/*.cfg; do
            if [[ ! "${cfg##*/}" =~ ^my_ ]]; then
                parse_file "${cfg}"
            fi
        done
    fi
}

# Runs the upgrades in upgrades.sh from the current version to the final version
# Tries to find the quickest path to the final version
process_upgrades() {
    local from_version=$1
    local final_version=$2

    if [ -z "${from_version}" ] || [ "${from_version}" == "${final_version}" ]; then
        return
    fi

    local highest_to_version="0.00"
    local upgrade_function
    for upgrade in $(declare -F | grep "^declare -f upgrade_${from_version//\./_}_to_[0-9]\+_[0-9]\+" | sed "s/declare -f //"); do
        local to_version=${upgrade#*_to_}
        if cmp_version "${highest_to_version}" "${to_version}"; then
            highest_to_version=${to_version}
            upgrade_function=${upgrade}
        fi
    done

    if [ -z "${upgrade_function}" ]; then
        local lowest_from_version="999.99"
        # find lowest available upgrade path
        for upgrade in $(declare -F | grep "^declare -f upgrade_[0-9]\+_[0-9]\+_to_[0-9]\+_[0-9]\+" | sed "s/declare -f //"); do
            local upgrade_from_version=${upgrade#upgrade_}
            upgrade_from_version=${upgrade_from_version%_to_*}
            if cmp_version "${upgrade_from_version}" "${lowest_from_version}"; then
                lowest_from_version=${upgrade_from_version}
            fi
        done
        log_error "No upgrade path found for version ${from_version}" \
            "Please reinstall Happy Hare from scratch or manually upgrade to ${lowest_from_version//_/.} first"
    fi

    highest_to_version=${highest_to_version//_/.}
    log_info "Upgrading from ${from_version} to ${highest_to_version}"
    eval "${upgrade_function}"
    process_upgrades "${highest_to_version}" "${final_version}"
}

copy_config_files() {
    # Now substitute tokens using given brd_type and Kconfig starting values
    local filename=${1##*/}
    local src=$1
    local dest=$2

    local sed_expr=""

    local files_to_copy="mmu.cfg mmu_hardware.cfg mmu_parameters.cfg mmu_macro_vars.cfg mmu_vars.cfg"
    if [[ ! "${files_to_copy}" =~ (^| )${filename}( |$) ]] && [[ ! ${src#"${SRC}"/config/} =~ ^addons/ ]]; then
        # File doesn't need to be changed/copied
        return
    fi

    if [[ "${filename}" =~ ^my_ ]]; then
        # Skip files that start with 'my_' used in development/testing
        return
    fi

    cp --remove-destination "${src}" "${dest}"

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

    for var in $(declare | grep "^CONFIG_PARAM_"); do
        local var=${var%%=*}
        local pattern="{${var#CONFIG_PARAM_}}"
        sed_expr+="s|${pattern,,}|${!var}|g; "
    done

    local num_gates=${CONFIG_HW_NUM_GATES}

    if [ "${filename}" == "mmu.cfg" ]; then
        sed_expr+="s|{pin_.*}||g; " # Clear any remaining unprocessed pin placeholders

        duplicate_section "[[:space:]]*MMU_PRE_GATE_0=" "" "0" "${num_gates} - 1" "${dest}"
        duplicate_section "[[:space:]]*MMU_POST_GEAR_0=" "" "0" "${num_gates} - 1" "${dest}"

        if [ "${CONFIG_MMU_HAS_SELECTOR}" != "y" ]; then
            duplicate_section "[[:space:]]*MMU_GEAR_UART_1" "$" "1" "${num_gates} - 1" "${dest}"
        else
            delete_section "[[:space:]]*MMU_GEAR_UART_1" "$" "${dest}"
        fi

        if [ "${CONFIG_INSTALL_ESPOOLER}" == "y" ]; then
            duplicate_section "[[:space:]]*MMU_DC_MOT_0_EN=" "$" "0" "${num_gates} - 1" "${dest}"
        else
            delete_section "[[:space:]]*MMU_DC_MOT_0_EN=" "$" "${dest}"
        fi
    fi

    if [ "${filename}" == "mmu_hardware.cfg" ]; then
        duplicate_section "pre_gate_switch_pin_0:" "" "0" "${num_gates} - 1" "${dest}"
        duplicate_section "post_gear_switch_pin_0:" "" "0" "${num_gates} - 1" "${dest}"

        # Correct shared uart_address for EASY-BRD
        if [ "${CONFIG_HW_MMU_BOARD_TYPE}" == "EASY-BRD" ]; then
            # Share uart_pin to avoid duplicate alias problem
            sed_expr+="s/^uart_pin: mmu:MMU_SEL_UART/uart_pin: mmu:MMU_GEAR_UART/; "
        else
            # Remove uart_address lines
            delete_line "uart_address:" "${dest}"
        fi

        if [ "${CONFIG_ENABLE_SELECTOR_TOUCH}" == "y" ]; then
            uncomment_section "#diag_pin: ^mmu:MMU_GEAR_DIAG" "$" "${dest}"
            uncomment_section "#extra_endstop_pins" "$" "${dest}"
            comment_section "uart_address" "" "${dest}"
        fi

        # Handle LED option - Comment out if disabled (section is last, go comment to end of file)
        if [ "${CONFIG_ENABLE_LED}" != "y" ]; then
            comment_section "\[neopixel mmu_leds\]" "$" "${dest}"
            comment_section "\[mmu_leds\]" "$" "${dest}"
        fi

        # Handle Encoder option - Delete if not required (section is 25 lines long)
        if [ "${CONFIG_MMU_HAS_ENCODER}" != "y" ]; then
            delete_section "# ENCODER" "# FILAMENT SENSORS" "${dest}"
        fi
        # Handle Selector options - Delete if not required (sections are 8 and 36 lines respectively)
        if [ "${CONFIG_MMU_HAS_SELECTOR}" != "y" ]; then
            delete_section "# SELECTOR SERVO" "# OPTIONAL GANTRY" "${dest}"
            delete_section "# SELECTOR STEPPER" "# SERVOS" "${dest}"

            duplicate_section "\[tmc2209 stepper_mmu_gear_1\]" "$" "1" "${num_gates} - 1" "${dest}"
            duplicate_section "\[stepper_mmu_gear_1\]" "$" "1" "${num_gates} - 1" "${dest}"
        else
            # Delete additional gear drivers template section
            delete_section "# ADDITIONAL FILAMENT DRIVE" "# SELECTOR STEPPER" "${dest}"
        fi
    fi

    # Conifguration parameters -----------------------------------------------------------
    if [ "${filename}" == "mmu_parameters.cfg" ]; then
        # Ensure that supplemental user added params are retained. These are those that are
        # by default set internally in Happy Hare based on vendor and version settings but
        # can be overridden.  This set also includes a couple of hidden test parameters.

        if [ "${CONFIG_HW_SELECTOR_TYPE}" == "VirtualSelector" ]; then
            delete_section "# Servo configuration" "# Logging" "${dest}"
            delete_section "# Selector movement speeds" "$" "${dest}"
            delete_section "# Selector touch" "$" "${dest}"
            delete_section "# ADVANCED/CUSTOM MMU" "$" "${dest}"
            delete_line "sync_to_extruder:" "${dest}"
            delete_line "sync_form_tip:" "${dest}"
            delete_line "preload_attempts:" "${dest}"
            delete_line "gate_load_retries:" "${dest}"
        fi

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
    fi

    # Variables macro ---------------------------------------------------------------------
    if [ "${filename}" == "mmu_macro_vars.cfg" ]; then
        duplicate_section "\[gcode_macro T0\]" "$" "0" "${num_gates} - 1" "${dest}"
    fi

    sed -i "${sed_expr}" "${dest}"
    update_file "${dest}"

    # If any params are still left warn the user because they will be lost (should have been upgraded)
    for key in "${!PARAMS[@]}"; do
        local section=${key%,*}
        if [ "${SECTION_ORIGIN["${section}"]}" == "${filename}" ]; then
            log_warning "Following parameter in '${filename}' is deprecated and has been removed: ${section} ${key#*,} = ${PARAMS[$key]}"
        fi
    done
}

install_include() {
    local include=${1//./\\.} # Escape for regex
    include=${include//\*/\\*}
    local dest=$2

    if ! grep -q "\[include ${include}\]" "${dest}"; then
        log_info "Adding include ${include} from ${dest}"
        sed -i "1i [include ${include}]" "${dest}"
    fi
}

uninstall_include() {
    local include=${1//./\\.} # Escape for regex
    include=${include//\*/\\*}
    local dest=$2

    if grep -q "\[include ${include}\]" "${dest}"; then
        log_info "Removing include ${1} from ${dest}"
        sed -i "\%\[include ${include}\]% d" "${dest}"
    fi
}

# Link in all includes if not already present
install_printer_includes() {
    local dest=$1

    log_info "Installing MMU references in ${CONFIG_PRINTER_CONFIG}"

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

    if [ "${CONFIG_INSTALL_ESPOOLER}" == "y" ]; then
        install_include "mmu/addons/dc_espooler.cfg" "${dest}"
    else
        uninstall_include "mmu/addons/dc_espooler.cfg" "${dest}"
    fi

    install_include "mmu/base/*.cfg" "${dest}"
}

uninstall_printer_includes() {
    log_info "Cleaning MMU references from ${CONFIG_PRINTER_CONFIG}"
    local dest=$1

    for include in \
        'mmu/optional/mmu_menu.cfg' \
        'mmu/optional/mmu_ercf_compat.cfg' \
        'mmu/optional/client_macros.cfg' \
        'mmu/addons/mmu_erec_cutter.cfg' \
        'mmu/addons/blobifier.cfg' \
        'mmu/addons/dc_espooler.cfg' \
        'mmu_software.cfg' \
        'mmu_sequence.cfg' \
        'mmu_cut_tip.cfg' \
        'mmu_form_tip.cfg' \
        'mmu_parameters.cfg' \
        'mmu_hardware.cfg' \
        'mmu_filametrix.cfg' \
        'mmu.cfg' \
        'mmu/base/*.cfg' \
        'mmu/addons/*.cfg'; do
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
    log_info "Cleaning Happy Hare sections from moonraker.conf"
    local dest=$1
    for section in "update_manager happy-hare" "mmu_server"; do
        if ! grep -q "\[${section}\]" "${dest}"; then
            log_info "[$section] not found in ${dest} - skipping removal"
        else
            log_info "Removing [$section] from ${dest}"
            delete_section "\[${section}\]" "\[" "${dest}"
        fi
    done
}

self_update() {
    if [ -n "${NO_UPDATE+x}" ]; then
        log_info "Skipping self update"
        return
    fi

    local git_cmd="git branch --show-current"
    if [ "${CONFIG_IS_MIPS}" != "y" ]; then
        # timeout is unavailable on MIPS
        git_cmd="timeout 3s ${git_cmd}"
    fi

    local current_branch
    if ! current_branch=$(${git_cmd}); then
        log_warning "Error updating from github" \
            "You might have an old version of git" \
            "Skipping automatic update..."
        return
    fi

    if [ -z "${current_branch}" ]; then
        log_warning "Timeout talking to github. Skipping upgrade check"
        return
    fi

    log_git "Running on '${current_branch}' branch" \
        "Checking for updates..."
    # Both check for updates but also help me not loose changes accidently
    git fetch --quiet

    local switch=0
    if ! git diff --quiet --exit-code "origin/${current_branch}"; then
        log_git "Found a new version of Happy Hare on github, updating..."
        if [ -n "$(git status --porcelain)" ]; then
            git stash push -m 'local changes stashed before self update' --quiet
        fi
        switch=1
    fi

    if [ -n "${BRANCH}" ] && [ "${BRANCH}" != "${current_branch}" ]; then
        log_git "Switching to '${current_branch}' branch"
        current_branch=${BRANCH}
        switch=1
    fi

    if [ "${switch}" -eq 1 ]; then
        git checkout "${current_branch}" --quiet
        git pull --quiet --force
        git_version=$(git describe --tags)
        log_git "Now on git version: ${git_version}"
    else
        git_version=$(git describe --tags)
        log_git "Already on the latest version: ${git_version}"
    fi
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

# These parameters are too complex to encode with Kconfig.
set_extra_parameters() {
    # never use the version from the existing installation files
    remove_param "[mmu]" "happy_hare_version"
    CONFIG_PARAM_HAPPY_HARE_VERSION="${CONFIG_VERSION}"

    CONFIG_HW_MMU_VERSION=${CONFIG_HW_MMU_BASE_VERSION}
    if [ "${CONFIG_HW_MMU_TYPE_ERCF_1_1}" == "y" ]; then
        [ "${CONFIG_HW_EXT_SPRINGY}" == "y" ] && CONFIG_HW_MMU_VERSION+="s"
        [ "${CONFIG_HW_EXT_BINKY}" == "y" ] && CONFIG_HW_MMU_VERSION+="b"
        [ "${CONFIG_HW_EXT_TRIPLE_DECK}" == "y" ] && CONFIG_HW_MMU_VERSION+="t"
    elif [ "${CONFIG_HW_MMU_TYPE_TRADRACK}" == "y" ]; then
        [ "${CONFIG_HW_EXT_BINKY}" == "y" ] && CONFIG_HW_MMU_VERSION+="e"
    fi

    if has_param "[mmu_machine]" "num_gates"; then
        CONFIG_HW_NUM_GATES=$(param "[mmu_machine]" "num_gates")
    fi

    CONFIG_HW_NUM_LEDS=$(calc "${CONFIG_HW_NUM_GATES} * 2 + 1")
    CONFIG_HW_NUM_LEDS_MINUS1=$(calc "${CONFIG_HW_NUM_LEDS} - 1")
    CONFIG_HW_NUM_GATES_PLUS1=$(calc "${CONFIG_HW_NUM_GATES} + 1")
}

build() {
    local src=$1
    local out=$2
    log_info "Building file ${out}..."

    read_previous_config
    process_upgrades "$(param "[mmu]" "happy_hare_version")" "${CONFIG_VERSION}"
    set_extra_parameters
    copy_config_files "$src" "$out"
}

uninstall() {
    uninstall_update_manager "${CONFIG_KLIPPER_CONFIG_HOME}/moonraker.conf"
    uninstall_printer_includes "${CONFIG_KLIPPER_CONFIG_HOME}/${CONFIG_PRINTER_CONFIG}"
}

cmp_version() {
    awk "BEGIN { exit !($1 < $2) }"
}

check_version() {
    if [ ! -f "${CONFIG_KLIPPER_CONFIG_HOME}/mmu/base/mmu_parameters.cfg" ]; then
        log_info "Fresh install detected"
        return
    fi

    parse_file "${CONFIG_KLIPPER_CONFIG_HOME}/mmu/base/mmu_parameters.cfg" "happy_hare_version"
    # Important to update version
    FROM_VERSION=$(param "[mmu]" "happy_hare_version")
    if [ -n "${FROM_VERSION}" ]; then
        if cmp_version "${CONFIG_VERSION}" "${FROM_VERSION}"; then
            log_warning "Trying to update from version ${FROM_VERSION} to ${CONFIG_VERSION}" \
                "Automatic 'downgrade' to earlier version is not guaranteed. If you encounter startup problems you may" \
                "need to manually compare the backed-up 'mmu_parameters.cfg' with current one to restore differences"
        fi
    fi
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
self-update)
    self_update
    ;;
uninstall)
    uninstall
    ;;
restart-service)
    restart_service "$2" "$3"
    ;;
print-happy-hare)
    log_info "$(get_logo "Happy Hare ${CONFIG_F_VERSION} Ready...")"
    ;;
print-unhappy-hare)
    log_info "${sad_logo}"
    ;;
check-version)
    check_version
    ;;
print-params)
    read_previous_config
    for key in "${!PARAMS[@]}"; do
        echo -e "${key%,*} ${key#*,}: ${PARAMS[${key}]}"
    done
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
