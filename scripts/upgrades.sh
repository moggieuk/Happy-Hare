upgrade_2_70_to_2_71() {
	local sec="[gcode_macro _MMU_CUT_TIP_VARS]"
	param_change_var "${sec}" "variable_pin_park_x_dist" "variable_pin_park_dist"
	param_change_var "${sec}" "variable_pin_loc_x_compressed" "variable_pin_loc_compressed"

	local sec="[gcode_macro _MMU_SEQUENCE_VARS]"
	if has_param "${sec}" "variable_park_xy"; then
		local xy=$(param "${sec}" "variable_park_xy")
		local z_hop_toolchange=$(param "[mmu]" "z_hop_height_toolchange")
		# local z_hop_error=$(param "${sec}" "z_hop_height_error")

		set_param "${sec}" "variable_park_toolchange" "${xy}, ${z_hop_toolchange:-0}, 0, 2"
		# TODO: This is not relevant anymore?
		# set_param "${sec}" "variable_park_error" "${xy}, ${z_hop_error:-0}, 0, 2"
		remove_param "${sec}" "variable_park_xy"
		remove_param "${sec}" "z_hop_height_toolchange"
		# remove "${sec}" "z_hop_height_error"
	fi

	param_change_var "${sec}" "variable_lift_speed" "variable_park_lift_speed"

	if has_param "${sec}" "variable_enable_park"; then
		if [ "$(param "${sec}" "variable_enable_park")" == "False" ]; then
			if [ "$(param "${sec}" "variable_enable_park_runout")" == "True" ]; then
				set_param "${sec}" "variable_enable_park_printing" "'toolchange,load,unload,runout,pause,cancel'"
			else
				set_param "${sec}" "variable_enable_park_printing" "'pause,cancel'"
			fi
		else
			set_param "${sec}" "variable_enable_park_printing" "'toolchange,load,unload,pause,cancel'"
		fi
		remove_param "${sec}" "variable_enable_park"
	fi

	if has_param "${sec}" "variable_enable_park_standalone"; then
		if [ "$(param "${sec}" "variable_enable_park_standalone")" == "False" ]; then
			set_param "${sec}" "variable_enable_park_standalone" "'pause,cancel'"
		else
			set_param "${sec}" "variable_enable_park_standalone" "'toolchange,load,unload,pause,cancel'"
		fi
	fi
}

upgrade_2_71_to_2_72() {
	if [ "$(param "[mmu]" "toolhead_residual_filament")" == "0" ] &&
		[ "$(param "[mmu]" "toolhead_ooze_reduction")" != "0" ]; then
		copy_param "[mmu]" "toolhead_ooze_reduction" "[mmu]" "toolhead_residual_filament"
		set_param "[mmu]" "toolhead_ooze_reduction" "0"
	fi
}

upgrade_2_72_to_2_73() {
	# Blobifer update - Oct 13th 2024
	local sec="[gcode_macro BLOBIFIER]"
	if has_param "${sec}" "variable_iteration_z_raise"; then
		log_info "Setting Blobifier variable_z_raise and variable_purge_length_maximum from previous settings"
		local max_i_per_blob=$(param "${sec}" "variable_max_iterations_per_blob")
		local i_z_raise=$(param "${sec}" "variable_iteration_z_raise")
		local i_z_change=$(param "${sec}" "variable_iteration_z_change")
		local max_i_length=$(param "${sec}" "variable_max_iteration_length")
		local triangulated=$(calc "${max_i_per_blob} * (${max_i_per_blob} - 1) / 2")

		set_param "${sec}" "variable_z_raise" "$(calc "${i_z_raise} * ${max_i_per_blob} - ${triangulated} * ${i_z_change}")"
		set_param "${sec}" "variable_purge_length_maximum" "$(calc "${max_i_length} * ${max_i_per_blob}")"

		remove_param "${sec}" "variable_max_iterations_per_blob"
		remove_param "${sec}" "variable_iteration_z_raise"
		remove_param "${sec}" "variable_iteration_z_change"
		remove_param "${sec}" "variable_max_iteration_length"
	fi
}

upgrade_2_73_to_3_00() {
	if has_param_section "[mmu_servo mmu_servo]"; then
		param_change_section "[mmu_servo mmu_servo]" "[mmu_servo selector_servo]"
		if [ "$(param "[mmu_servo selector_servo]" "pin")" == "mmu:MMU_SERVO" ]; then
			remove_param "[mmu_servo selector_servo]" "pin" # Pin name has been changed, reset
		fi
	fi

	param_change_key "[mmu],mmu_num_gates" "[mmu_machine],num_gates"
	param_change_key "[mmu],mmu_vendor" "[mmu_machine],mmu_vendor"
	param_change_key "[mmu],mmu_version" "[mmu_machine],mmu_version"

	param_change_var "[mmu]" "auto_calibrate_gates" "autotune_rotation_distance"
	param_change_var "[mmu]" "auto_calibrate_bowden" "autotune_bowden_length"
	param_change_var "[mmu]" "endless_spool_final_eject" "gate_final_eject_distance"
	param_change_var "[gcode_macro _MMU_SOFTWARE_VARS]" "variable_eject_tool" "variable_unload_tool"
	param_change_var "[gcode_macro _MMU_CLIENT_VARS]" "variable_eject_tool_on_cancel" "variable_unload_tool_on_cancel"
}
