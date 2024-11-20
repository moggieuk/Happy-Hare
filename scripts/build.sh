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

# shellcheck source=.config
source "./${KCONFIG_CONFIG:-.config}"
source "scripts/upgrades.sh"

SRC=${SRC:-.}
OUT=${OUT:-out}

set -e # Exit immediately on error

# scoped paramaters, will have the following layout:
# [SECTION],PARAM = VALUE
declare -A PARAMS
# These will be the same as above but not stripped of comments and whitespace
declare -A PARAMS_UNSTRIPPED
# Keep track of where the parameters came from to detect deprecated parameters
declare -A SECTION_ORIGIN

log_color() {
    local line
    for line in "${@:2}"; do
        echo -e "${1}${line}${C_OFF}"
    done
}

log_info() {
    log_color "${C_INFO}" "$@"
}

log_notice() {
    log_color "${C_NOTICE}" "$@"
}

log_warning() {
    log_color "${C_WARNING}" "$@"
}

log_error() {
    log_color "${C_ERROR}" "$@"
    exit 1
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

# Get the value of a [section] parameter
param() {
    local section=$1
    local param=$2
    echo "${PARAMS["${section},${param}"]}"
}

# Get all keys for a section
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

# Set a {section] parameter value
set_param() {
    local section=$1
    local param=$2
    local value=$3
    PARAMS["${section},${param}"]="${value}"
}

# Check if a [section] parameter is defined, an empty string returns true
has_param() {
    local section=$1
    local param=$2
    [ -n "${PARAMS["${section},${param}"]+x}" ]
}

# Check if a section exists
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

# Delete a single line
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

replace_cfg_placeholder() {
    local placeholder=$1
    local dest=$2
    local src=${SRC}/config/${dest#"${OUT}/mmu/"}.${placeholder}

    sed -i -e "/{cfg_${placeholder}}/ { r ${src}" -e "; d }" "${dest}"
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

        current_value="${current_value%$'\n'}"
        current_value_lines="${current_value_lines%$'\n'}"
        current_value_lines="${current_value_lines#*:}"               # Select value after :
        current_value_lines="${current_value_lines/#*([[:blank:]])/}" # Strip leading spaces
        set_param "${current_section}" "${current_parameter}" "${current_value}"
        PARAMS_UNSTRIPPED["${current_section},${current_parameter}"]="${current_value_lines}"
        SECTION_ORIGIN["${current_section}"]="${file#*/config/mmu/}"
    }

    local current_section
    local current_parameter
    local current_value       # Stripped current value
    local current_value_lines # Unstripped current value
    local remaining_lines     # Unstripped remaining lines that don't belong to the current parameter
    local line

    # Read old config files
    while IFS="" read -r line; do
        if [[ "${line}" =~ ^\[.*\] ]]; then
            # section
            set_parameter
            current_section="${line%%[#;]*}"
            current_section="${current_section/%*([[:blank:]])/}"
            current_parameter=
            current_value=
            current_value_lines=
            remaining_lines=
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
            current_value_lines=
            remaining_lines=

            # echo "current_section: '${current_section}'"
            if [[ "${current_section}" =~ ^\[(gcode_macro|delayed_gcode) ]] &&
                [[ ! "${current_parameter}" =~ ^variable_ ]]; then
                # skip everything that is not a variable in gcode macros
                # log_warning "Skipping gcode"
                current_parameter=
            fi

            if [[ ! "${current_parameter}" =~ ^(${prefix_filter}) ]]; then
                # We skip these parameters
                current_parameter=
            fi
            # echo "current_parameter: '${current_parameter}'"
        fi

        if [ -z "${current_parameter}" ]; then
            # no parameter yet, so not relevant
            continue
        fi

        # part of a possibly multiline value
        local value="${line%%[#;]*}"      # Strip comment
        value="${value/%*([[:blank:]])/}" # Strip trailing spaces
        value="${value#*:}"               # Select value after :
        value="${value/#*([[:blank:]])/}" # Strip leading spaces
        remaining_lines+=${line}$'\n'
        if [ -n "${value}" ]; then
            if [ -n "${current_value}" ]; then
                # We are in a multiline value, add space infront
                current_value+=${line%%[^[:blank:]]*}${value}$'\n'
            else
                current_value+=${value}$'\n'
            fi
            current_value_lines+=${remaining_lines} # Line is still part of a value, add it to the value lines

            remaining_lines=
        fi

    done <"${file}"

    # after reaching end of file, set the last known parameter
    set_parameter
}

update_file() {
    local dest=$1
    local prefix_filter=$2

    check_parameter() {
        current_value="${current_value%$'\n'}"
        current_value_lines="${current_value_lines%$'\n'}"

        if [ -z "${current_parameter}" ]; then
            if [ -n "${remaining_lines}" ]; then
                echo "${remaining_lines%$'\n'}" >>"${dest}.tmp"
            fi
            return
        fi

        local existing_value="$(param "${current_section}" "${current_parameter}")"
        remove_param "${current_section}" "${current_parameter}"
        local unstripped_value="${PARAMS_UNSTRIPPED["${current_section},${current_parameter}"]}"
        unset "PARAMS_UNSTRIPPED[${current_section},${current_parameter}]"

        # Compare stripped parameter values
        if [ "${current_value}" == "${existing_value}" ]; then
            # No change, just print as is. This will remove any comments the user might have added
            echo "${current_value_lines}" >>"${dest}.tmp"
            if [ -n "${remaining_lines}" ]; then
                echo "${remaining_lines%$'\n'}" >>"${dest}.tmp"
            fi
            return
        fi

        if [ "${#existing_value}" -gt 50 ]; then
            local print_value="'${existing_value:0:50}'..."
        else
            local print_value="'${existing_value}'"
        fi

        log_info "Reusing parameter value from existing install: ${current_section} ${current_parameter} = ${print_value}"

        if [[ ! "${current_value_lines}" =~ $'\n' ]]; then
            # Single line value. We retain the comments in the base files
            echo "${current_value_lines/${current_value}/${existing_value}}" >>"${dest}.tmp"
        else
            # Multiline value. We can't retain the comments from the base files, so we will use the one in the user's config
            unstripped_current_value="${current_value_lines#*:}"                    # Select value after :
            unstripped_current_value="${unstripped_current_value/#*([[:blank:]])/}" # Strip leading spaces
            echo "${current_value_lines/${unstripped_current_value}/${unstripped_value}}" >>"${dest}.tmp"
        fi

        if [ -n "${remaining_lines}" ]; then
            echo "${remaining_lines%$'\n'}" >>"${dest}.tmp"
        fi
    }

    local current_section
    local current_parameter
    local current_value       # Stripped current value
    local current_value_lines # Unstripped current value
    local remaining_lines     # Unstripped remaining lines that don't belong to the current parameter
    local line

    # Read the file line by line
    while IFS="" read -r line; do
        if [[ "${line}" =~ ^\[.*\] ]]; then
            # section
            check_parameter
            current_section="${line%%[#;]*}"
            current_section="${current_section/%*([[:blank:]])/}"
            current_parameter=
            current_value=
            current_value_lines=
            remaining_lines=
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
            current_value_lines=
            remaining_lines=

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
        local value="${line%%[#;]*}"      # Strip comment
        value="${value/%*([[:blank:]])/}" # Strip trailing spaces
        value="${value#*:}"               # Select value after :
        value="${value/#*([[:blank:]])/}" # Strip leading spaces

        remaining_lines+=${line}$'\n'
        if [ -n "${value}" ]; then
            if [ -n "${current_value}" ]; then
                # We are in a multiline value, add space infront
                current_value+=${line%%[^[:blank:]]*}${value}$'\n'
            else
                current_value+=${value}$'\n'
            fi
            current_value_lines+=${remaining_lines}
            remaining_lines=
        fi
    done <"${dest}"

    # after reaching end of file, check the last known parameter
    check_parameter
    mv "${dest}.tmp" "${dest}"
}

# Pull parameters from previous installation
read_previous_config() {
    for cfg in ${CONFIGS_TO_PARSE}; do
        if [ -f "${cfg}" ]; then
            parse_file "${cfg}"
        fi
    done
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

# Now substitute tokens using Kconfig starting values
copy_config_files() {
    local filename=${1#"${SRC}/config/"} # Filename relevant to config directory
    local src=$1
    local dest=$2

    local files_to_copy=(
        config/base/mmu.cfg
        config/base/mmu_hardware.cfg
        config/base/mmu_parameters.cfg
        config/base/mmu_macro_vars.cfg
        config/mmu_vars.cfg
        config/addons/*.cfg
    )
    if [[ ! "${files_to_copy[*]#"config/"}" =~ (^| )${filename}( |$) ]]; then
        # File doesn't need to be changed/copie
        return
    fi

    if [[ "${filename}" =~ ^my_ ]]; then
        # Skip files that start with 'my_' used in development/testing
        return
    fi

    log_info "Building file ${src#"${SRC}/config/"}..."

    cp -a --remove-destination "${src}" "${dest}"

    local sed_expr=
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

    sed_expr+="/{cfg_.*}/d; "   # Remove any remaining unprocessed cfg placeholders
    sed_expr+="s/{pin_.*}//g; " # Clear any remaining unprocessed pin placeholders

    if [ "${filename}" == "base/mmu.cfg" ]; then
        duplicate_section "[[:space:]]*MMU_PRE_GATE_0=" "" "0" "${NUM_GATES} - 1" "${dest}"
        duplicate_section "[[:space:]]*MMU_POST_GEAR_0=" "" "0" "${NUM_GATES} - 1" "${dest}"

        if [ "${CONFIG_PARAM_SELECTOR_TYPE}" == "LinearSelector" ]; then
            replace_cfg_placeholder "selector" "${dest}"
        elif [ "${CONFIG_PARAM_SELECTOR_TYPE}" == "VirtualSelector" ]; then
            replace_cfg_placeholder "gears" "${dest}"
            duplicate_section "[[:space:]]*MMU_GEAR_UART_1" "$" "1" "${NUM_GATES} - 1" "${dest}"
        fi

        if [ "${CONFIG_MMU_HAS_ENCODER}" == "y" ]; then
            replace_cfg_placeholder "encoder" "${dest}"
        fi

        if [ "${CONFIG_INSTALL_DC_ESPOOLER}" == "y" ]; then
            replace_cfg_placeholder "dc_espooler" "${dest}"
            duplicate_section "[[:space:]]*MMU_DC_MOT_0_EN=" "$" "0" "${NUM_GATES} - 1" "${dest}"
        fi
    fi

    if [ "${filename}" == "base/mmu_hardware.cfg" ]; then
        duplicate_section "pre_gate_switch_pin_0:" "" "0" "${NUM_GATES} - 1" "${dest}"
        duplicate_section "post_gear_switch_pin_0:" "" "0" "${NUM_GATES} - 1" "${dest}"

        # Correct shared uart_address for EASY-BRD
        if [ "${CONFIG_MMU_BOARD_TYPE}" == "EASY-BRD" ]; then
            # Share uart_pin to avoid duplicate alias problem
            sed_expr+="s/^uart_pin: mmu:MMU_SEL_UART/uart_pin: mmu:MMU_GEAR_UART/; "
        else
            # Remove uart_address lines
            delete_line "uart_address:" "${dest}"
        fi

        # Handle LED option - Comment out if disabled (section is last, go comment to end of file)
        if [ "${CONFIG_ENABLE_LED}" == "y" ]; then
            replace_cfg_placeholder "leds" "${dest}"
        fi

        # Handle Encoder option - Delete if not required (section is 25 lines long)
        if [ "${CONFIG_MMU_HAS_ENCODER}" == "y" ]; then
            replace_cfg_placeholder "encoder" "${dest}"
        fi
        # Handle Selector options - Delete if not required (sections are 8 and 36 lines respectively)
        if [ "${CONFIG_PARAM_SELECTOR_TYPE}" == "LinearSelector" ]; then
            replace_cfg_placeholder "selector_stepper" "${dest}"
            replace_cfg_placeholder "selector_servo" "${dest}"
        elif [ "${CONFIG_PARAM_SELECTOR_TYPE}" == "VirtualSelector" ]; then
            replace_cfg_placeholder "gear_steppers" "${dest}"
            duplicate_section "\[tmc2209 stepper_mmu_gear_1\]" "$" "1" "${NUM_GATES} - 1" "${dest}"
            duplicate_section "\[stepper_mmu_gear_1\]" "$" "1" "${NUM_GATES} - 1" "${dest}"
        fi

        if [ "${CONFIG_ENABLE_SELECTOR_TOUCH}" == "y" ]; then
            uncomment_section "#diag_pin: ^mmu:MMU_GEAR_DIAG" "$" "${dest}"
            uncomment_section "#extra_endstop_pins" "$" "${dest}"
            comment_section "uart_address" "" "${dest}"
        fi
    fi

    # Conifguration parameters -----------------------------------------------------------
    if [ "${filename}" == "base/mmu_parameters.cfg" ]; then
        # Ensure that supplemental user added params are retained. These are those that are
        # by default set internally in Happy Hare based on vendor and version settings but
        # can be overridden.  This set also includes a couple of hidden test parameters.

        if [ "${CONFIG_PARAM_SELECTOR_TYPE}" == "LinearSelector" ]; then
            replace_cfg_placeholder "selector_servo" "${dest}"
            replace_cfg_placeholder "selector_speeds" "${dest}"
            replace_cfg_placeholder "custom_mmu" "${dest}"
        elif [ "${CONFIG_PARAM_SELECTOR_TYPE}" == "VirtualSelector" ]; then
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
    if [ "${filename}" == "base/mmu_macro_vars.cfg" ]; then
        duplicate_section "\[gcode_macro T0\]" "$" "0" "${NUM_GATES} - 1" "${dest}"
    fi

    sed -i "${sed_expr}" "${dest}"
    update_file "${dest}"

    # If any params are still left warn the user because they will be lost (should have been upgraded)
    for key in "${!PARAMS[@]}"; do
        local section=${key%,*}
        if [ "${SECTION_ORIGIN["${section}"]}" == "${filename}" ]; then
            log_warning "Following parameter in '${filename}' has been been removed or been deprecated: ${section} ${key#*,} = '${PARAMS[$key]}'"
        fi
    done
}

install_include() {
    local include=${1//./\\.} # Escape for regex
    include=${include//\*/\\*}
    local dest=$2

    if ! grep -q "\[include ${include}\]" "${dest}"; then
        log_info "Adding include ${1} to ${dest#"${SRC}/"}"
        sed -i "1i [include ${include}]" "${dest}"
    fi
}

uninstall_include() {
    local include=${1//./\\.} # Escape for regex
    include=${include//\*/\\*}
    local dest=$2

    if grep -q "\[include ${include}\]" "${dest}"; then
        log_info "Removing include ${1} from ${dest#"${SRC}/"}"
        sed -i "\%\[include ${include}\]% d" "${dest}"
    fi
}

# Link in all includes if not already present
install_printer_includes() {
    local dest=$1

    log_info "Installing MMU references in ${dest#"${SRC}/"}"

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

    if [ "${CONFIG_INSTALL_DC_ESPOOLER}" == "y" ]; then
        install_include "mmu/addons/dc_espooler.cfg" "${dest}"
    else
        uninstall_include "mmu/addons/dc_espooler.cfg" "${dest}"
    fi

    install_include "mmu/base/*.cfg" "${dest}"
}

uninstall_printer_includes() {
    local dest=$1
    log_info "Cleaning MMU references from ${dest}"

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

install_moonraker() {
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

uninstall_moonraker() {
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
    if [ -n "${SKIP_UPDATE+x}" ]; then
        log_notice "Skipping self update"
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

    log_notice "Running on '${current_branch}' branch" \
        "Checking for updates..."
    # Both check for updates but also help me not loose changes accidently
    git fetch --quiet

    local switch=0
    if ! git diff --quiet --exit-code "origin/${current_branch}"; then
        log_notice "Found a new version of Happy Hare on github, updating..."
        if [ -n "$(git status --porcelain)" ]; then
            git stash push -m 'local changes stashed before self update' --quiet
        fi
        switch=1
    fi

    if [ -n "${BRANCH}" ] && [ "${BRANCH}" != "${current_branch}" ]; then
        log_notice "Switching to '${current_branch}' branch"
        current_branch=${BRANCH}
        switch=1
    fi

    if [ "${switch}" -eq 1 ]; then
        git checkout "${current_branch}" --quiet
        git pull --quiet --force
        git_version=$(git describe --tags)
        log_notice "Now on git version: ${git_version}"
    else
        git_version=$(git describe --tags)
        log_notice "Already on the latest version: ${git_version}"
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

    CONFIG_PARAM_MMU_VERSION=${CONFIG_MMU_BASE_VERSION}
    if [ "${CONFIG_MMU_TYPE_ERCF_1_1}" == "y" ]; then
        [ "${CONFIG_EXT_SPRINGY}" == "y" ] && CONFIG_PARAM_MMU_VERSION+="s"
        [ "${CONFIG_EXT_BINKY}" == "y" ] && CONFIG_PARAM_MMU_VERSION+="b"
        [ "${CONFIG_EXT_TRIPLE_DECK}" == "y" ] && CONFIG_PARAM_MMU_VERSION+="t"
    elif [ "${CONFIG_MMU_TYPE_TRADRACK}" == "y" ]; then
        [ "${CONFIG_EXT_BINKY}" == "y" ] && CONFIG_PARAM_MMU_VERSION+="e"
    fi

    if has_param "[mmu_machine]" "num_gates"; then
        NUM_GATES=$(param "[mmu_machine]" "num_gates")
    else
        NUM_GATES=${CONFIG_PARAM_NUM_GATES}
    fi

    CONFIG_PARAM_NUM_LEDS=$(calc "${NUM_GATES} * 2 + 1")
    CONFIG_PARAM_NUM_LEDS_MINUS1=$(calc "${CONFIG_PARAM_NUM_LEDS} - 1")
    CONFIG_PARAM_NUM_GATES_PLUS1=$(calc "${NUM_GATES} + 1")
}

build() {
    local src=$1
    local out=$2

    # if params.tmp doesn't exist, we are doing a fresh install
    if [ -f "${OUT}/params.tmp" ]; then
        source "${OUT}/params.tmp"
    fi

    set_extra_parameters
    copy_config_files "$src" "$out"
}

cmp_version() {
    awk "BEGIN { exit !($1 < $2) }"
}

check_version() {
    if [ ! -f "${CONFIG_KLIPPER_CONFIG_HOME}/mmu/base/mmu_parameters.cfg" ]; then
        log_notice "Fresh install detected"
        return
    fi

    parse_file "${CONFIG_KLIPPER_CONFIG_HOME}/mmu/base/mmu_parameters.cfg" "happy_hare_version"
    # Important to update version
    local from_version=$(param "[mmu]" "happy_hare_version")
    log_notice "Current version: ${from_version}"
    if [ -n "${from_version}" ]; then
        if cmp_version "${CONFIG_PARAM_HAPPY_HARE_VERSION}" "${from_version}"; then
            log_warning "Trying to update from version ${from_version} to ${CONFIG_PARAM_HAPPY_HARE_VERSION}" \
                "Automatic 'downgrade' to earlier version is not guaranteed. If you encounter startup problems you may" \
                "need to manually compare the backed-up 'mmu_parameters.cfg' with current one to restore differences"
        fi
    fi
}

run_test() {
    local test_dir=$1
    parse_file "${test_dir}/in.cfg"

    local test_name=${test_dir#test/build/}

    if [[ "${test_name:0:4}" =~ ^[0-9]_[0-9][0-9]$ ]]; then
        local to_version=${test_name:0:4}
        local from_version=$(param "[mmu]" "happy_hare_version")
        process_upgrades "${from_version}" "${to_version//_/.}"
    fi

    cp -f "${test_dir}/config.cfg" "${test_dir}/test_out.cfg"
    update_file "${test_dir}/test_out.cfg"
    if [ ! "$(diff -q "${test_dir}/out.cfg" "${test_dir}/test_out.cfg")" ]; then
        log_notice "Test passed: ${test_name}"
    else
        diff -U1 --color=always "${test_dir}/out.cfg" "${test_dir}/test_out.cfg" || true
        log_error "Test failed: ${test_name}"
    fi
}

run_tests() {
    for test_dir in test/build/* test/build/*/*; do
        if [ -f "${test_dir}/config.cfg" ]; then
            run_test "${test_dir}"
        fi
    done
}

case $1 in
build)
    build "$2" "$3"
    ;;
install-includes)
    install_printer_includes "$2"
    ;;
install-moonraker)
    install_moonraker "$2"
    ;;
self-update)
    self_update
    ;;
uninstall-includes)
    uninstall_printer_includes "$2"
    ;;
uninstall-moonraker)
    uninstall_moonraker "$2"
    ;;
restart-service)
    restart_service "$2" "$3"
    ;;
print-happy-hare)
    log_info "$(get_logo "Done! Happy Hare ${CONFIG_F_VERSION} ready...")"
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
parse-params)
    log_info "Parsing existing parameters..."
    read_previous_config
    process_upgrades "$(param "[mmu]" "happy_hare_version")" "${CONFIG_PARAM_HAPPY_HARE_VERSION}"
    echo -n "" >"${OUT}/params.tmp"
    for key in "${!PARAMS[@]}"; do
        echo "PARAMS[${key}]=${PARAMS[${key}]@Q}" >>"${OUT}/params.tmp"
    done
    for key in "${!PARAMS_UNSTRIPPED[@]}"; do
        echo "PARAMS_UNSTRIPPED[${key}]=${PARAMS_UNSTRIPPED[${key}]@Q}" >>"${OUT}/params.tmp"
    done
    for key in "${!SECTION_ORIGIN[@]}"; do
        echo "SECTION_ORIGIN[${key}]=${SECTION_ORIGIN[${key}]@Q}" >>"${OUT}/params.tmp"
    done
    ;;
tests)
    run_tests
    ;;
diff)
    git diff -U2 --color --src-prefix="current: " --dst-prefix="built: " --minimal --word-diff=color --stat --no-index -- "$2" "$3" |
        # Filter out command and index lines from the diff, they only muck up the information
        grep -v "diff --git " | grep -Ev "index [[:xdigit:]]+\.\.[[:xdigit:]]+" || true
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
