#!/bin/bash

# Specify the filename
filename="/tmp/test.cfg"

# Read the file line by line
while IFS= read -r line
do
  # Remove comments
  line="${line%%#*}"
  
  # Check if line is not empty
  if [ ! -z "$line" ]; then
    # Split the line into parameter and value
    IFS=":" read -r parameter value <<< "$line"
    # Borne shell equivalent:
    #    parameter=$(echo "$line" | cut -d: -f1 | tr -d '[:space:]')
    #    value=$(echo "$line" | cut -d: -f2 | tr -d '[:space:]')
    
    # Remove leading and trailing whitespace
    parameter=$(echo "$parameter" | xargs)
    value=$(echo "$value" | xargs)
    
    # Print the parameter and value
    echo "$parameter: $value"
  fi
done < "$filename"


#extruder
#long_moves_speed > long_moves_speed_from_buffer
#long_moves_speed_from_spool > long_moves_speed_from_spool
#short_moves_speed
#z_hop_height
#z_hop_speed
#gear_homing_accel
#gear_sync_accel
#gear_buzz_accel
#servo_down_angle
#servo_up_angle
#servo_move_angle
#servo_duration
#num_moves
#apply_bowden_correction
#load_bowden_tolerance
#unload_bowden_tolerance
#parking_distance
#encoder_move_step_size
#load_encoder_retries
#colorselector > selector_offsets CHANGE
#bypass_selector
#timeout_pause
#timeout_unlock
#disable_heater
#min_temp_extruder
#calibration_bowden_length
#unload_buffer
#home_to_extruder
#ignore_extruder_load_error
#extruder_homing_max
#extruder_homing_step
#extruder_homing_current
#extruder_form_tip_current
#toolhead_homing_max
#toolhead_homing_step
#sync_load_length # CHANGE TO sync_load_extruder BOOLEAN
#sync_load_speed > extruder_sync_load_speed
#sync_unload_length # CHANGE TO sync_unload_extruder BOOLEAN
#sync_unload_speed > extruder_sync_unload_speed
#delay_servo_release
#home_position_to_nozzle # DEPRECATE? copy to two below unless they are already set.
#extruder_to_nozzle
#sensor_to_nozzle
#nozzle_load_speed > extruder_load_speed
#nozzle_unload_speed > extruder_unload_speed
#selector_move_speed
#selector_homing_speed
#selector_sensorless_speed
#sync_to_extruder
#sync_load_extruder
#sync_unload_extruder
#sync_form_tip
#sync_gear_current
#homing_method
#enable_clog_detection
#enable_endless_spool
#endless_spool_groups
#tool_to_gate_map
#gate_status
#gate_material
#gate_color
#persistence_level (force to 3 or higher)
#log_level
#logfile_level
#log_statistics
#log_visual
#startup_status (force to 1 or higher)
