#!/bin/bash
# Happy Hare MMU Software
# Installer
#
# Copyright (C) 2022  moggieuk#6538 (discord) moggieuk@hotmail.com
#
KLIPPER_HOME="${HOME}/klipper"
KLIPPER_CONFIG_HOME="${HOME}/printer_data/config"
OLD_KLIPPER_CONFIG_HOME="${HOME}/klipper_config"

declare -A PIN 2>/dev/null || {
    echo "Please run this script with ./bash $0"
    exit 1
}

# Pins for Fysetc Burrows ERB board, EASY-BRD and EASY-BRD with Seed Studio XIAO RP2040
PIN[ERB,gear_uart_pin]="ercf:gpio20";         PIN[EASY-BRD,gear_uart_pin]="ercf:PA8";          PIN[EASY-BRD-RP2040,gear_uart_pin]="ercf:gpio6"
PIN[ERB,gear_step_pin]="ercf:gpio10";         PIN[EASY-BRD,gear_step_pin]="ercf:PA4";          PIN[EASY-BRD-RP2040,gear_step_pin]="ercf:gpio27"
PIN[ERB,gear_dir_pin]="!ercf:gpio9";          PIN[EASY-BRD,gear_dir_pin]="!ercf:PA10";         PIN[EASY-BRD-RP2040,gear_dir_pin]="!ercf:gpio28"
PIN[ERB,gear_enable_pin]="!ercf:gpio8";       PIN[EASY-BRD,gear_enable_pin]="!ercf:PA2";       PIN[EASY-BRD-RP2040,gear_enable_pin]="!ercf:gpio26"
PIN[ERB,gear_diag_pin]="ercf:gpio13";         PIN[EASY-BRD,gear_diag_pin]="";                  PIN[EASY-BRD-RP2040,gear_diag_pin]=""
PIN[ERB,gear_endstop_pin]="ercf:gpio24";      PIN[EASY-BRD,gear_endstop_pin]="ercf:PB9";       PIN[EASY-BRD-RP2040,gear_endstop_pin]="ercf:gpio1"
PIN[ERB,selector_uart_pin]="ercf:gpio17";     PIN[EASY-BRD,selector_uart_pin]="ercf:PA8";      PIN[EASY-BRD-RP2040,selector_uart_pin]="ercf:gpio6"
PIN[ERB,selector_step_pin]="ercf:gpio16";     PIN[EASY-BRD,selector_step_pin]="ercf:PA9";      PIN[EASY-BRD-RP2040,selector_step_pin]="ercf:gpio7"
PIN[ERB,selector_dir_pin]="!ercf:gpio15";     PIN[EASY-BRD,selector_dir_pin]="!ercf:PB8";      PIN[EASY-BRD-RP2040,selector_dir_pin]="!ercf:gpio0"
PIN[ERB,selector_enable_pin]="!ercf:gpio14";  PIN[EASY-BRD,selector_enable_pin]="!ercf:PA11";  PIN[EASY-BRD-RP2040,selector_enable_pin]="!ercf:gpio29"
PIN[ERB,selector_diag_pin]="ercf:gpio19";     PIN[EASY-BRD,selector_diag_pin]="ercf:PA7";      PIN[EASY-BRD-RP2040,selector_diag_pin]="ercf:gpio2"
PIN[ERB,selector_endstop_pin]="ercf:gpio24";  PIN[EASY-BRD,selector_endstop_pin]="ercf:PB9";   PIN[EASY-BRD-RP2040,selector_endstop_pin]="ercf:gpio1"
PIN[ERB,servo_pin]="ercf:gpio23";             PIN[EASY-BRD,servo_pin]="ercf:PA5";              PIN[EASY-BRD-RP2040,servo_pin]="ercf:gpio4"
PIN[ERB,encoder_pin]="ercf:gpio22";           PIN[EASY-BRD,encoder_pin]="ercf:PA6";            PIN[EASY-BRD-RP2040,encoder_pin]="ercf:gpio3"

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

link_ercf_plugin() {
    echo -e "${INFO}Linking ercf extension to Klipper..."
    if [ -d "${KLIPPER_HOME}/klippy/extras" ]; then
        for file in `cd ${SRCDIR}/extras ; ls *.py`; do
            ln -sf "${SRCDIR}/extras/${file}" "${KLIPPER_HOME}/klippy/extras/${file}"
        done
    else
        echo -e "${WARNING}MMU modules not installed because Klipper 'extras' directory not found!"
    fi
}

unlink_ercf_plugin() {
    echo -e "${INFO}Unlinking ercf extension to Klipper..."
    if [ -d "${KLIPPER_HOME}/klippy/extras" ]; then
        for file in `cd ${SRCDIR}/extras ; ls *.py`; do
            rm -f "${KLIPPER_HOME}/klippy/extras/${file}"
        done
    else
        echo -e "${WARNING}MMU modules not uninstalled because Klipper 'extras' directory not found!"
    fi
}

copy_template_files() {
    if [ "${INSTALL_TEMPLATES}" -eq 0 ]; then
        # Then check to see if we need to upgrade the current config files
        upgrade_config_files
        return
    fi

    # Generate sample colorselector, gate_status, endless_spool_groups based on number of gates
    colorselector="colorselector: "
    gate_status="gate_status: "
    gate_material="gate_material: "
    gate_color="gate_color: "
    tool_to_gate_map="tool_to_gate_map: "
    endless_spool_groups="endless_spool_groups: "
    offset_x10=23
    available=1
    materials=(PLA PETG ABS PLA+ ABS ABS PLA ABS ASA)
    colors=(red orange yellow green blue indigo violet ffffff black)
    for (( i=0; i<=$(expr $num_gates - 1); i++ ))
    do
       mod=$(echo `expr $i % 9`)
       if [ "${i}" -ne 0 ]; then
           colorselector="${colorselector}, "
           gate_status="${gate_status}, "
           gate_material="${gate_material}, "
           gate_color="${gate_color}, "
           tool_to_gate_map="${tool_to_gate_map}, "
           endless_spool_groups="${endless_spool_groups}, "
       fi
       colorselector="${colorselector}$(echo $offset_x10 | sed -e 's/.$/.&/;t' -e 's/.$/.0&/')"
       gate_status="${gate_status}${available}"
       gate_material="${gate_material}${materials[$mod]}"
       gate_color="${gate_color}${colors[$mod]}"
       tool_to_gate_map="${tool_to_gate_map}${i}"
       endless_spool_groups="${endless_spool_groups}$(expr $i % 3 + 1)"
       offset_x10=$(expr $offset_x10 + 210)
       if [ "$(expr $i % 3)" -eq 2 ]; then
           offset_x10=$(expr $offset_x10 + 51)
       fi
       available=0
    done
    if [ "${has_bypass}" -eq 1 ]; then
        bypass_comment=""
    else
        bypass_comment="#"
    fi

    echo -e "${INFO}Copying configuration files to ${KLIPPER_CONFIG_HOME}"
    for file in `cd ${SRCDIR} ; ls *.cfg`; do
        dest=${KLIPPER_CONFIG_HOME}/${file}

        if test -f $dest; then
            next_dest="$(nextfilename "$dest")"
            echo -e "${INFO}Config file ${file} already exists - moving old one to ${next_dest}"
            mv ${dest} ${next_dest}
        fi

        if [ "${file}" == "ercf_hardware.cfg" ]; then
            if [ "${toolhead_sensor}" -eq 1 ]; then
                magic_str1="## MMU Toolhead sensor"
            else
                magic_str1="NO TOOLHEAD"
            fi
            uart_comment=""
            if [ "${brd_type}" == "ERB" -o "${brd_type}" == "EASY-BRD-RP2040" ]; then
                uart_comment="#"
            fi

            if [ "${sensorless_selector}" -eq 1 ]; then
                cat ${SRCDIR}/${file} | sed -e "\
                    s/^#diag_pin: \^{selector_diag_pin}/diag_pin: \^{selector_diag_pin}/; \
                    s/^#driver_SGTHRS: 65/driver_SGTHRS: 65/; \
                    s/^endstop_pin: \^{selector_endstop_pin}/#endstop_pin: \^{selector_endstop_pin}/; \
                    s/^#endstop_pin: tmc2209_selector_stepper/endstop_pin: tmc2209_selector_stepper/; \
                    s/^uart_address:/${uart_comment}uart_address:/; \
                    s/{brd_type}/${brd_type}/; \
                        " > ${dest}.tmp
            else
                # This is the default template config without sensorless selector homing enabled
                cat ${SRCDIR}/${file} | sed -e "\
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
                    s/{gear_endstop_pin}/${PIN[$brd_type,gear_endstop_pin]}/; \
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

        elif [ "${file}" == "ercf_software.cfg" ]; then
            cat ${SRCDIR}/${file} | sed -e "\
                s%{klipper_config_home}%${KLIPPER_CONFIG_HOME}%g; \
                    " > ${dest}
        else
            # Other config files (not ercf_hardware.cfg, ercf_software.cfg, ercf_display_menu.cfg)
            cp ${SRCDIR}/${file} ${dest}.tmp
            cat ${dest}.tmp | sed -e "\
                s/{clog_detection}/${clog_detection}/g; \
                s/{endless_spool}/${endless_spool}/g; \
                s/{servo_up_angle}/${servo_up_angle}/g; \
                s/{servo_down_angle}/${servo_down_angle}/g; \
                s/{calibration_bowden_length}/${calibration_bowden_length}/g; \
		s/colorselector:.*/${colorselector}/; \
		s/gate_status:.*/${gate_status}/; \
		s/gate_material:.*/${gate_material}/; \
		s/gate_color:.*/${gate_color}/; \
		s/tool_to_gate_map:.*/${tool_to_gate_map}/; \
		s/endless_spool_groups:.*/${endless_spool_groups}/; \
		s/#bypass_selector:/${bypass_comment}bypass_selector:/; \
                    " > ${dest} && rm ${dest}.tmp
        fi
    done

    if [ "${INSTALL_TEMPLATES}" -eq 1 ]; then
        if [ "${add_includes}" -eq 1 ]; then
            # Link in all includes if not already present
            dest=${KLIPPER_CONFIG_HOME}/printer.cfg
            if test -f $dest; then
                next_dest="$(nextfilename "$dest")"
                echo -e "${INFO}Copying original printer.cfg file to ${next_dest}"
                cp ${dest} ${next_dest}
                if [ ${menu_12864} -eq 1 ]; then
                    i='\[include ercf_menu.cfg\]'
                    already_included=$(grep -c "${i}" ${dest} || true)
                    if [ "${already_included}" -eq 0 ]; then
                        sed -i "1i ${i}" ${dest}
                    fi
                fi
                for i in \
                        '\[include client_macros.cfg\]' \
                        '\[include ercf_software.cfg\]' \
                        '\[include ercf_parameters.cfg\]' \
                        '\[include ercf_hardware.cfg\]' ; do
                    already_included=$(grep -c "${i}" ${dest} || true)
                    if [ "${already_included}" -eq 0 ]; then
                        sed -i "1i ${i}" ${dest}
                    fi
                done
            else
                echo -e "${WARNING}File printer.cfg file not found! Cannot include MMU configuration files"
            fi
        fi
    fi
}

remove_template_files() {
    echo -e "${INFO}Removing MMU configuration files from ${KLIPPER_CONFIG_HOME}"
    for file in `cd ${SRCDIR} ; ls *.cfg`; do
        dest=${KLIPPER_CONFIG_HOME}/${file}

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
            /\[include client_macros.cfg\]/ d; \
            /\[include ercf_software.cfg\]/ d; \
            /\[include ercf_parameters.cfg\]/ d; \
            /\[include ercf_hardware.cfg\]/ d; \
            /\[include ercf_menu.cfg\]/ d" > "${dest}.tmp" && mv "${dest}.tmp" "${dest}"
    fi
}

# Save the user some time and attempt the upgrade of config files for them...
upgrade_config_files() {
    echo -e "${INFO}Upgrading configuration files..."
    hardware="ercf_hardware.cfg"
    dest_hardware=${KLIPPER_CONFIG_HOME}/${hardware}
    parameters="ercf_parameters.cfg"
    dest_parameters=${KLIPPER_CONFIG_HOME}/${parameters}

    encoder_pin="<FIXME>"
    encoder_resolution="<FIXME>"
    if test -f $dest_parameters; then
        next_dest="$(nextfilename "$dest_parameters")"
        echo -e "${INFO}Pre upgrade config file ${dest_parameters} moved to ${next_dest} for reference"
        cp ${dest_parameters} ${next_dest}

        update_encoder=$(grep -c '\encoder_pin:' ${dest_parameters} || true)
        if [ "${update_encoder}" -eq 1 ]; then
            # These two settings have moved to ercf_hardware.cfg
            encoder_pin=$(egrep 'encoder_pin:' ${dest_parameters} | awk '{ sub("\^", "") ; print $2 }')
            encoder_resolution=$(egrep 'encoder_resolution:' ${dest_parameters} | awk '{ print $2 }')

            # Comment out all old settings in ercf_parameters.cfg
            echo -e "${WARNING}Upgrading ${dest_parameters} to remove legacy servo and encoder settings..."
            cat ${dest_parameters} | sed -e " \
                s/^encoder_pin:/#encoder_pin:/ ; \
                s/^encoder_resolution:/#encoder_resolution:/ ; \
                s/^extra_servo_dwell_up:/#extra_servo_dwell_up:/ ; \
                s/^extra_servo_dwell_down:/#extra_servo_dwell_down:/ ; \
                s/^sensorless_selector:/#sensorless_selector:/ ; \
                    " > ${dest_parameters}.encoder_upgrade && mv ${dest_parameters}.encoder_upgrade ${dest_parameters}
        fi

        update_sensorless=$(grep -c 'sensorless_selector:' ${dest_parameters} || true)
        if [ "${update_sensorless}" -eq 1 ]; then
            # Delete deprecated parameter
            echo -e "${WARNING}Upgrading ${dest_parameters} to remove legacy parameters..."
            cat ${dest_parameters} | sed -e " \
                s/^sensorless_selector:.*/#/ ; \
                    " > ${dest_parameters}.param_upgrade && mv ${dest_parameters}.param_upgrade ${dest_parameters}
        fi

        update_sync=$(grep -c 'sync_to_extruder:' ${dest_parameters} || true)
        if [ "${update_sync}" -ne 1 ]; then

            # Add on the new synchronization params and extruder name
            echo -e "${WARNING}Upgrading ${dest_parameters} to add new sync and extruder parameters..."
            cat << EOF >> ${dest_parameters}

## Added in v1.3.0 ---------------------------------------------------------------------------------------------------------
extruder: extruder		# Name of the toolhead extruder that MMU is using

# EXPERIMENTAL: New synchronized gear/extruder movement!
# If enabled for loading or unloading extruder this will override 'sync_load_length' and 'sync_unload_length'
# If you normally run with maxed out gear stepper current consider reducing it with 'sync_gear_current'
#
sync_to_extruder: 0		# Gear motor is synchronized to extruder during print
sync_load_extruder: 0		# Full extruder load leveraging motor synchronization
sync_unload_extruder: 0		# Full extruder unload (except stand alone tip forming) leveraging motor synchronization
sync_form_tip: 0		# Standalone tip formation (initial part of unload) also leveraging motor synchronization
#
sync_gear_current: 100		# Percentage of gear_stepper current (10%-100%) to use when syncing with extruder during print

EOF
        fi
    fi

    if test -f $dest_hardware; then
        next_dest="$(nextfilename "$dest_hardware")"
        echo -e "${INFO}Pre upgrade config file ${dest_hardware} moved to ${next_dest} for reference"
        cp ${dest_hardware} ${next_dest}

        update_gear_stepper=$(grep -c 'manual_stepper gear_stepper' ${dest_hardware} || true)
        if [ "${update_gear_stepper}" -eq 2 ]; then
            echo -e "${WARNING}Upgrading ${dest_hardware} for new manual_extruder_stepper..."
            cat ${dest_hardware} | sed -e " \
                s/manual_stepper gear_stepper\]/manual_extruder_stepper gear_stepper\]/ ; \
                    " > ${dest_hardware}.gear_stepper_upgrade
        else
            # Gear stepper already upgraded or can't upgrade
            cp ${dest_hardware} ${dest_hardware}.gear_stepper_upgrade
        fi

        update_gear_endstop=$(egrep -c '^endstop_pin:' ${dest_hardware} || true)
        if [ "${update_gear_endstop}" -eq 2 ]; then
            echo -e "${WARNING}Upgrading ${dest_hardware} to remove endstop on gear_selector..."
            cat ${dest_hardware} | sed -e " \
                s/^endstop_pin:.*# Comment if using physical endstop switch/#/
                    " > ${dest_hardware}.gear_endstop_upgrade
        else
            # Gear stepper endstop already upgraded or can't upgrade
            cp ${dest_hardware} ${dest_hardware}.gear_endstop_upgrade
        fi

        update_servo=$(grep -c '\[servo ercf_servo\]' ${dest_hardware}.gear_endstop_upgrade || true)
        if [ "${update_servo}" -eq 1 ]; then
            echo -e "${WARNING}Upgrading ${dest_hardware} for new ercf_servo..."
            cat ${dest_hardware} | sed -e " \
                s/^\[servo ercf_servo\]/\[ercf_servo ercf_servo\]/ ;
                    " > ${dest_hardware}.servo_upgrade
        else
            # Servo already upgraded or can't upgrade
            cp ${dest_hardware}.gear_endstop_upgrade ${dest_hardware}.servo_upgrade
        fi

        # This is a little difficult to cleanly update, so try a couple of strategies
        update_encoder=$(grep -c '\[filament_motion_sensor encoder_sensor\]' ${dest_hardware}.servo_upgrade || true)
        if [ "${update_encoder}" -eq 1 ]; then
            magic_str2="# MMU Clog detection"
            echo -e "${WARNING}Upgrading ${dest_hardware} for new ercf_encoder..."
            cat ${dest_hardware}.servo_upgrade | sed -e " \
                /${magic_str2} START/,/${magic_str2} END/d; \
                /\[duplicate_pin_override\]/d; \
                /pins:/d; \
                /\[filament_motion_sensor encoder_sensor\]/,+6 d; \
                    " > ${dest_hardware} && rm ${dest_hardware}.servo_upgrade

            # Finally add on the new encoder section
            cat << EOF >> ${dest_hardware}

## ENCODER -----------------------------------------------------------------------------------------------------------------
## The encoder_resolution is determined by running the ERCF_CALIBRATE_ENCODER. Be sure to read the manual
[ercf_encoder ercf_encoder]
encoder_pin: ^${encoder_pin}			# EASY-BRD: ^ercf:PA6, Flytech ERB: ^ercf:gpio22
encoder_resolution: ${encoder_resolution}		# Set AFTER 'rotation_distance' is tuned for gear stepper (see manual)
extruder: extruder			# The extruder to track with for runout/clog detection

# These are advanced but settings for Automatic clog/runout detection mode. Make sure you understand or ask questions on Discord
desired_headroom: 5.0				# The runout headroom that MMU will attempt to maintain (closest MMU comes to triggering runout)
average_samples: 4			# The "damping" effect of last measurement. Higher value means clog_length will be reduced more slowly

EOF
        else
            # Encoder already upgraded or can't upgrade
            mv ${dest_hardware}.servo_upgrade ${dest_hardware}
        fi
    fi
}

install_update_manager() {
    echo -e "${INFO}Adding update manager to moonraker.conf"
    file="${KLIPPER_CONFIG_HOME}/moonraker.conf"
    if [ -f "${file}" ]; then
        update_section=$(grep -c '\[update_manager happy-hare\]' \
        ${file} || true)
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
        update_section=$(grep -c '\[update_manager ercf-happy_hare\]' \
        ${file} || true)
        if [ "${update_section}" -eq 0 ]; then
            echo -e "${INFO}[update_manager ercf-happy_hare] not found in moonraker.conf - skipping removal"
        else
            cat "${file}" | sed -e " \
                /\[update_manager ercf-happy_hare\]/,+6 d; \
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
    if [ "${INSTALL_TEMPLATES}" -eq 1 ]; then
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
		echo -e "${PROMPT}Sensorless selector operation? This can be difficult to setup but allows for additional selector recovery steps (disables the 'extra' input on the EASY-BRD)${INPUT}"
                yn=$(prompt_yn "Enable sensorless selector operation")
                case $yn in
                    y)
                        if [ "${brd_type}" == "EASY-BRD" ]; then
                            echo
                            echo -e "${WARNING}    IMPORTANT: Set the J6 jumper pins to 2-3 and 4-5, i.e. .[..][..]  MAKE A NOTE NOW!!"
                        fi
                        sensorless_selector=1
                        ;;
                    n)
                        if [ "${brd_type}" == "EASY-BRD" ]; then
                            echo
                            echo -e "${WARNING}    IMPORTANT: Set the J6 jumper pins to 1-2 and 4-5, i.e. [..].[..]  MAKE A NOTE NOW!!"
                        fi
                        sensorless_selector=0
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
                echo -e "${PROMPT}Sensorless selector operation? This allows for additional selector recovery steps but is difficult to tune${INPUT}"
                yn=$(prompt_yn "Enable sensorless selector operation")
                case $yn in
                    y)
                        sensorless_selector=1
                        ;;
                    n)
                        sensorless_selector=0
                        ;;
                esac
                ;;
        esac

        num_gates=9
        echo
        echo -e "${PROMPT}How many gates (selectors) do you have (3, 6, 9, 12)?${INPUT}"
       while true; do
            read -p "Number of gates? " num_gates
            if ! [ "${num_gates}" -ge 1 ] 2> /dev/null ;then
                echo -e "${INFO}Positive integer value only"
          else
               break
           fi
        done

        echo
        echo -e "${PROMPT}Have you built with a selector bypass? (in one of the selector separators)${INPUT}"
        yn=$(prompt_yn "Selector bypass")
        case $yn in
            y)
                has_bypass=1
                echo -e "${INFO}    NOTE: You will need to set the bypass selector offset manually"
                ;;
            n)
                has_bypass=0
                ;;
        esac

        echo
        echo -e "${PROMPT}Do you have a toolhead sensor you would like to use? If reliable this provides the smoothest and most reliable loading and unloading operation${INPUT}"
        yn=$(prompt_yn "Enable toolhead sensor")
        case $yn in
            y)
                toolhead_sensor=1
                echo -e "${PROMPT}    What is the mcu pin name that your toolhead sensor is connected too?"
                echo -e "${PROMPT}    If you don't know just hit return, I can enter a default and you can change later${INPUT}"
                read -p "    Toolhead sensor pin name? " toolhead_sensor_pin
                if [ "${toolhead_sensor_pin}" = "" ]; then
                    toolhead_sensor_pin="{dummy_pin_must_set_me}"
                fi
                ;;
            n)
                toolhead_sensor=0
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
                clog_detection=1
                echo -e "${PROMPT}    Would you like MMU to automatically adjust clog detection length (recommended)?${INPUT}"
                yn=$(prompt_yn "    Automatic")
                if [ "${yn}" == "y" ]; then
                    clog_detection=2
                fi
                ;;
            n)
                clog_detection=0
                ;;
        esac

        echo
        echo -e "${PROMPT}EndlessSpool? This uses filament runout detection to automate switching to new spool without interruption${INPUT}"
        yn=$(prompt_yn "Enable EndlessSpool")
        case $yn in
            y)
                endless_spool=1
                if [ "${clog_detection}" -eq 0 ]; then
                    echo
                    echo -e "${WARNING}    NOTE: I've re-enabled clog detection which is necessary for EndlessSpool to function"
                    clog_detection=2
                fi
                ;;
            n)
               endless_spool=0
               ;;
        esac

        echo
        echo -e "${PROMPT}What is the length of your reverse bowden tube in mm?"
        echo -e "${PROMPT}(This is just to speed up calibration and needs to be approximately right but not longer than the real length)${INPUT}"
        while true; do
            read -p "Reverse bowden length in mm? " calibration_bowden_length
            if ! [ "${calibration_bowden_length}" -ge 1 ] 2> /dev/null ;then
                echo -e "${INFO}Positive integer value only"
           else
               break
           fi
        done

        echo
        menu_12864=0
        echo -e "${PROMPT}Finally, would you like me to include all the MMU config files into your printer.cfg file${INPUT}"
        yn=$(prompt_yn "Add include?")
        case $yn in
            y)
                add_includes=1
                echo -e "${PROMPT}    Would you like to include Mini 12864 menu configuration extension for MMU${INPUT}"
                yn=$(prompt_yn "    Include menu")
                case $yn in
                    y)
                        menu_12864=1
                        ;;
                    n)
                       menu_12864=0
                       ;;
                esac
                ;;
            n)
               add_includes=0
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
        echo "     * Adjust your config for loading and unloading preferences"
        echo "     * Adjust toolhead distances 'home_to_extruder' for your particular setup"
        echo 
        echo "    Advanced:"
        echo "         * Tweak configurations like speed and distance in ercf_parameter.cfg"
        echo 
        echo "    Good luck! MMU is complex to setup. Remember Discord is your friend.."
        echo
        echo "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^"
        echo
    
    fi
}

usage() {
    echo -e "${EMPHASIZE}"
    echo "Usage: $0 [-k <klipper_home_dir>] [-c <klipper_config_dir>] [-i] [-u]"
    echo
    echo "-i for interactive"
    echo "-u for uninstall"
    echo
    exit 1
}

# Force script to exit if an error occurs
set -e
clear

# Find SRCDIR from the pathname of this script
SRCDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )"/ && pwd )"

UNINSTALL=0
INSTALL_TEMPLATES=0
INSTALL_KLIPPER_SCREEN_ONLY=0
while getopts "k:c:iu" arg; do
    case $arg in
        k) KLIPPER_HOME=${OPTARG};;
        c) KLIPPER_CONFIG_HOME=${OPTARG};;
        i) INSTALL_TEMPLATES=1;;
        u) UNINSTALL=1;;
        *) usage;;
    esac
done

verify_not_root
verify_home_dirs
check_klipper
if [ "$UNINSTALL" -eq 0 ]; then
    questionaire
    link_ercf_plugin
    copy_template_files
    install_update_manager
else
    echo
    echo -e "${WARNING}You have asked me to remove Happy Hare and cleanup"
    echo
    yn=$(prompt_yn "Are you sure you want to proceed with deleting Happy Hare?")
    case $yn in
        y)
            unlink_ercf_plugin
            remove_template_files
            uninstall_update_manager
            echo -e "${INFO}Uninstall complete except for the ERCF-Software-V3 directory - you can now safely delete that as well"
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
    echo '(")^(") ERCF Unready'
    echo
fi
