########################################################################################################################
# Happy Hare MMU Software
#
# Template file for MMU's with Selector Stepper but no servo (Type-A designs like 3DChameleon)
# This file omits servo parts of the configuration
#
# EDIT THIS FILE BASED ON YOUR SETUP
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Goal: Main configuration parameters for the klipper module
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# Notes:
#   Macro configuration is specified separately in 'mmu_macro_vars.cfg'.
#   Full details in https://github.com/moggieuk/Happy-Hare/tree/main/doc/configuration.md
#
[mmu]
happy_hare_version: {happy_hare_version}			# Don't mess, used for upgrade detection

# MMU Hardware Limits --------------------------------------------------------------------------------------------------
# ██╗     ██╗███╗   ███╗██╗████████╗███████╗
# ██║     ██║████╗ ████║██║╚══██╔══╝██╔════╝
# ██║     ██║██╔████╔██║██║   ██║   ███████╗
# ██║     ██║██║╚██╔╝██║██║   ██║   ╚════██║
# ███████╗██║██║ ╚═╝ ██║██║   ██║   ███████║
# ╚══════╝╚═╝╚═╝     ╚═╝╚═╝   ╚═╝   ╚══════╝
#
# Define the physical limits of your MMU. These settings will be respected regardless of individual speed settings.
#
gear_max_velocity: 300			# Never to be exceeded gear velocity regardless of specific parameters
gear_max_accel: 1500			# Never to be exceeded gear acceleration regardless of specific parameters
selector_max_velocity: 250		# Never to be exceeded selector velocity regardless of specific parameters
selector_max_accel: 1200		# Never to be exceeded selector acceleration regardless of specific parameters


# Logging --------------------------------------------------------------------------------------------------------------
# ██╗      ██████╗  ██████╗  ██████╗ ██╗███╗   ██╗ ██████╗ 
# ██║     ██╔═══██╗██╔════╝ ██╔════╝ ██║████╗  ██║██╔════╝ 
# ██║     ██║   ██║██║  ███╗██║  ███╗██║██╔██╗ ██║██║  ███╗
# ██║     ██║   ██║██║   ██║██║   ██║██║██║╚██╗██║██║   ██║
# ███████╗╚██████╔╝╚██████╔╝╚██████╔╝██║██║ ╚████║╚██████╔╝
# ╚══════╝ ╚═════╝  ╚═════╝  ╚═════╝ ╚═╝╚═╝  ╚═══╝ ╚═════╝ 
#
# log_level & logfile_level can be set to one of (0 = essential, 1 = info, 2 = debug, 3 = trace, 4 = stepper moves)
# Generally you can keep console logging to a minimal whilst still sending debug output to the mmu.log file
# Increasing the console log level is only really useful during initial setup to save having to constantly open the log file
# Note: that it is not recommended to keep logging at level greater that 2 (debug) if not debugging an issue because
# of the additional overhead
#
log_level: 1
log_file_level: 2			# Can also be set to -1 to disable log file completely
log_statistics: 1 			# 1 to log statistics on every toolchange (default), 0 to disable (but still recorded)
log_visual: 1				# 1 log visual representation of filament, 0 = disable
log_startup_status: 1			# Whether to log tool to gate status on startup, 1 = summary (default), 0 = disable
log_m117_messages: 1			# Whether send toolchange message via M117 to screen


# Movement speeds ------------------------------------------------------------------------------------------------------
# ███████╗██████╗ ███████╗███████╗██████╗ ███████╗
# ██╔════╝██╔══██╗██╔════╝██╔════╝██╔══██╗██╔════╝
# ███████╗██████╔╝█████╗  █████╗  ██║  ██║███████╗
# ╚════██║██╔═══╝ ██╔══╝  ██╔══╝  ██║  ██║╚════██║
# ███████║██║     ███████╗███████╗██████╔╝███████║
# ╚══════╝╚═╝     ╚══════╝╚══════╝╚═════╝ ╚══════╝
#
# Long moves are faster than the small ones and used for the bulk of the bowden movement. You can set two fast load speeds
# depending on whether pulling from the spool or filament buffer (if fitted and not the first time load). This can be helpful
# in allowing faster loading from buffer and slower when pulling from the spool because of the additional friction (prevents
# loosing steps). Unloading speed can be tuning if you have a rewinder system that imposes additional limits.
# NOTE: Encoder cannot keep up much above 450mm/s so make sure 'bowden_apply_correction' is off at very high speeds!
#
gear_from_spool_speed: 80               # mm/s Speed when loading from the spool (for the first time if has_filament_buffer: 1)
gear_from_spool_accel: 100              # Acceleration when loading from spool
gear_from_buffer_speed: 150             # mm/s Speed when loading filament from buffer. Conservative is 100mm/s, Max around 400mm/s
gear_from_buffer_accel: 400             # Normal acceleration when loading filament
gear_unload_speed: 80                   # mm/s Use (lower) speed when unloading filament (defaults to "from spool" speed)
gear_unload_accel: 100                  # Acceleration when unloading filament (defaults to "from spool" accel)
#
gear_short_move_speed: 80		# mm/s Speed when making short moves (like incremental retracts with encoder)
gear_short_move_accel: 600		# Usually the same as gear_from_buffer_accel (for short movements)
gear_short_move_threshold: 70		# Move distance that controls application of 'short_move' speed/accel
gear_homing_speed: 50			# mm/s Speed of gear stepper only homing moves (e.g. homing to gate or extruder)

# Speeds of extruder movement. The 'sync' speeds will be used when gear and extruder steppers are moving in sync
#
extruder_load_speed: 16			# mm/s speed of load move inside extruder from homing position to meltzone
extruder_unload_speed: 16		# mm/s speed of unload moves inside of extruder (very initial move from meltzone is 50% of this)
extruder_sync_load_speed: 18		# mm/s speed of synchronized extruder load moves
extruder_sync_unload_speed: 18		# mm/s speed of synchronized extruder unload moves
extruder_homing_speed: 18		# mm/s speed of extruder only homing moves (e.g. to toolhead sensor)

# Selector movement speeds. (Acceleration is defined by physical MMU limits set above and passed to selector stepper driver)
#
selector_move_speed: 200		# mm/s speed of selector movement (not touch)
selector_homing_speed: 60		# mm/s speed of initial selector homing move (not touch)
selector_touch_speed: 80		# mm/s speed of all touch selector moves (if stallguard configured)

# Selector touch (stallguard) operation. If stallguard is configured, then this can be used to switch on touch movement which
# can detect blocked filament path and try to recover automatically but it is more difficult to set up
#
selector_touch_enabled: 0		# If selector touch operation configured this can be used to disable it 1=enabled, 0=disabled

# When Happy Hare calls out to a macro for user customization and for parking moves these settings are applied and the previous
# values automatically restored afterwards. This allows for deterministic movement speed regardless of the starting state.
#
macro_toolhead_max_accel: 0		# Default printer toolhead acceleration applied when macros are run. 0 = use printer max
macro_toolhead_min_cruise_ratio: 0.5	# Default printer cruise ratio applied when macros are run


# Gate loading/unloading -----------------------------------------------------------------------------------------------
#  ██████╗  █████╗ ████████╗███████╗    ██╗      ██████╗  █████╗ ██████╗ 
# ██╔════╝ ██╔══██╗╚══██╔══╝██╔════╝    ██║     ██╔═══██╗██╔══██╗██╔══██╗
# ██║  ███╗███████║   ██║   █████╗      ██║     ██║   ██║███████║██║  ██║
# ██║   ██║██╔══██║   ██║   ██╔══╝      ██║     ██║   ██║██╔══██║██║  ██║
# ╚██████╔╝██║  ██║   ██║   ███████╗    ███████╗╚██████╔╝██║  ██║██████╔╝
#  ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝    ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═════╝ 
#
# These settings control the loading and unloading filament at the gate which is the parking position inside the MMU.
# Typically this would be switch sensor but you can also use an encoder. Even with encoder the endstop can be a switch
# and the encoder used for move verifcation (see advanced 'gate_endstop_to_encoder' option). Note that the `encoder`
# method, due to the nature of its operation will overshoot a little. This is not a problem in practice because the
# overshoot will simply be compensated for in the subsequent move. A +ve parking distance moves towards the MMU, -ve
# moves back through the endstop towards the toolhead. If the MMU has multiple bowden tubes then it is possible to home
# at the extruder sensor and avoid long bowden moves!
#
# Possible gate_homing_endstop names:
#   encoder       - Detect filament position using movement of the encoder
#   mmu_gate      - Use gate endstop
#   mmu_gear      - Use individual per-gate endstop (type-B MMU's)
#   extruder      - Use extruder entry sensor (Only for some type-B designs, see [mmu_machine] require_bowden_move setting)
#
gate_homing_endstop: encoder		# Name of gate endstop, "encoder" forces use of encoder for parking
gate_homing_max: 70			# Maximum move distance to home to the gate (or actual move distance for encoder parking)
gate_preload_homing_max: 70		# Maximum homing distance to the mmu_gear endstop (if MMU is fitted with one)
gate_preload_parking_distance: 0	# Parking position relative to mmu_gear endstop (-ve value means move forward) 
gate_unload_buffer: 50			# Amount to reduce the fast unload so that filament doesn't overshoot when parking
gate_load_retries: 2			# Number of times MMU will attempt to grab the filament on initial load (type-A designs)
gate_parking_distance: 23 		# Parking position in the gate (distance back from homing point, -ve value means move forward)
gate_endstop_to_encoder: 10		# Distance between gate endstop and encoder (IF both fitted. +ve if encoder after endstop)
gate_autoload: 1			# If pre-gate sensor fitted this controls the automatic loading of the gate
gate_final_eject_distance: 0		# Distance to eject filament on MMU_EJECT (Ignored by MMU_UNLOAD)


# Bowden tube loading/unloading ----------------------------------------------------------------------------------------
# ██████╗  ██████╗ ██╗    ██╗██████╗ ███████╗███╗   ██╗    ██╗      ██████╗  █████╗ ██████╗ 
# ██╔══██╗██╔═══██╗██║    ██║██╔══██╗██╔════╝████╗  ██║    ██║     ██╔═══██╗██╔══██╗██╔══██╗
# ██████╔╝██║   ██║██║ █╗ ██║██║  ██║█████╗  ██╔██╗ ██║    ██║     ██║   ██║███████║██║  ██║
# ██╔══██╗██║   ██║██║███╗██║██║  ██║██╔══╝  ██║╚██╗██║    ██║     ██║   ██║██╔══██║██║  ██║
# ██████╔╝╚██████╔╝╚███╔███╔╝██████╔╝███████╗██║ ╚████║    ███████╗╚██████╔╝██║  ██║██████╔╝
# ╚═════╝  ╚═════╝  ╚══╝╚══╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝    ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═════╝ 
#
bowden_homing_max: 2000			# Maximum attempted bowden move (for calibration). Should be larger than your actual bowden!

# If you MMU is equiped with an encoder the following options are available:
# 
# In addition to different bowden loading speeds for buffer and non-buffered filament it is possible to detect missed
# steps caused by "jerking" on a heavy spool. If bowden correction is enabled Happy Hare will "believe" the encoder
# reading and make correction moves to bring the filament to within the 'bowden_allowable_load_delta' of the end of
# bowden position (this does require a reliable encoder and is not recommended for very high speed loading >350mm/s)
#
bowden_apply_correction: 0		# 1 to enable, 0 disabled
bowden_allowable_load_delta: 20.0	# How close in mm the correction moves will attempt to get to target
#
# This saftey check uses the encoder to verify the filament is free of extruder before the fast bowden movement to
# reduce possibility of grinding filament. If enabled the trigger can be tuned by setting the "error tolerance" which
# represents the fraction of allowable mismatch between actual movement and that seen by encoder. Setting to 50% tolerance
# usually works well. Increasing will make test more tolerant. Value of 100% essentially disables error detection
# 
bowden_pre_unload_test: 1		# 1 to check for bowden movement before full pull (slower), 0 don't check (faster)
bowden_pre_unload_error_tolerance: 50	# ADVANCED: tune pre_unload_test


# Extruder homing -----------------------------------------------------------------------------------------------------
# ███████╗██╗  ██╗████████╗    ██╗  ██╗ ██████╗ ███╗   ███╗██╗███╗   ██╗ ██████╗ 
# ██╔════╝╚██╗██╔╝╚══██╔══╝    ██║  ██║██╔═══██╗████╗ ████║██║████╗  ██║██╔════╝ 
# █████╗   ╚███╔╝    ██║       ███████║██║   ██║██╔████╔██║██║██╔██╗ ██║██║  ███╗
# ██╔══╝   ██╔██╗    ██║       ██╔══██║██║   ██║██║╚██╔╝██║██║██║╚██╗██║██║   ██║
# ███████╗██╔╝ ██╗   ██║██╗    ██║  ██║╚██████╔╝██║ ╚═╝ ██║██║██║ ╚████║╚██████╔╝
# ╚══════╝╚═╝  ╚═╝   ╚═╝╚═╝    ╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝ ╚═════╝ 
#
# Happy Hare needs a reference "homing point" close to the extruder from which to accurately complete the loading of
# the toolhead. This homing operation takes place after the fast bowden load and it is anticipated that that load
# operation will leave the filament just shy of the homing point. If using a toolhead sensor this initial extruder
# homing is unnecessary (but can be forced) because the homing will occur inside the extruder for the optimum in accuracy.
# You still should set this homing method because it is also used for the determination and calibration of bowden length.
#
# In addition to an entry sensor "extruder" it is possible for Happy Hare to "feel" for the extruder gear entry
# by colliding with it. This can be done with encoder based collision detection, the compression of the sync-feedback
# (aka buffer) sensor or using "touch" (stallguard) on the gear stepper. Note that encoder collision detection is not
# completely deterministic and you will have to find the sweetspot for your setup by adjusting the TMC current reduction.
# Note that reduced current during collision detection can also prevent unecessary filament griding.
#
# Possible extruder_homing_endtop names:
#   collision            - Detect the collision with the extruder gear by monitoring encoder movement (Requires encoder)
#                          Fast bowden load will move to the extruder gears
#   mmu_gear_touch       - Use touch detection when the gear stepper hits the extruder (Requires stallguard)
#                          Fast bowden load will move to extruder_homing_buffer distance before extruder gear, then home
#   extruder             - If you have a "filament entry" endstop configured (Requires 'extruder' endstop)
#                          Fast bowden load will move to extruder_homing_buffer distance before sensor, then home
#   filament_compression - If you have a "sync-feedback" sensor with compression switch configured
#                          Fast bowden load will move to extruder_homing_buffer distance before extruder gear, then home
#   none                 - Don't attempt to home. Only possibiliy if lacking all sensor options
#                          Fast bowden load will move to the extruder gears. Option is fine if using toolhead sensor
# Note: The homing_endstop will be ignored ("none") if a toolhead sensor is available unless "extruder_force_homing: 1"
#
extruder_homing_max: 80			# Maximum distance to advance in order to attempt to home the extruder
extruder_homing_endstop: collision	# Filament homing method/endstop name (fallback if toolhead sensor not available)
extruder_homing_buffer: 25		# Amount to reduce the fast bowden load so filament doesn't overshoot the extruder homing point
extruder_collision_homing_current: 30	# % gear_stepper current (10%-100%) to use when homing to extruder homing (100 to disable)

# If you have a toolhead sensor it will always be used as a homing point making the homing outside of the extruder
# potentially unnecessary. However you can still force this initial homing step by setting this option in which case
# the filament will home to the extruder and then home to the toolhead sensor in two steps
#
extruder_force_homing: 0


# Toolhead loading and unloading --------------------------------------------------------------------------------------
# ████████╗ ██████╗  ██████╗ ██╗     ██╗  ██╗███████╗ █████╗ ██████╗     ██╗      ██████╗  █████╗ ██████╗ 
# ╚══██╔══╝██╔═══██╗██╔═══██╗██║     ██║  ██║██╔════╝██╔══██╗██╔══██╗    ██║     ██╔═══██╗██╔══██╗██╔══██╗
#    ██║   ██║   ██║██║   ██║██║     ███████║█████╗  ███████║██║  ██║    ██║     ██║   ██║███████║██║  ██║
#    ██║   ██║   ██║██║   ██║██║     ██╔══██║██╔══╝  ██╔══██║██║  ██║    ██║     ██║   ██║██╔══██║██║  ██║
#    ██║   ╚██████╔╝╚██████╔╝███████╗██║  ██║███████╗██║  ██║██████╔╝    ███████╗╚██████╔╝██║  ██║██████╔╝
#    ╚═╝    ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═════╝     ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═════╝ 
#
# It is possible to define highly customized loading and unloading sequences, however, unless you have a specialized
# setup it is probably easier to opt for the built-in toolhead loading and unloading sequence which already offers a
# high degree of customization. If you need even more control then edit the _MMU_LOAD_SEQUENCE and _MMU_UNLOAD_SEQUENCE
# macros in mmu_sequence.cfg - but be careful!
#
# An MMU must have a known point at the end of the bowden from which it can precisely load the extruder. Generally this
# will either be the extruder entrance (which is controlled with settings above) or by homing to toolhead sensor. If
# you have toolhead sensor it is past the extruder gear and the driver needs to know the max distance (from end of
# bowden move) to attempt homing
#
toolhead_homing_max: 40			# Maximum distance to advance in order to attempt to home to defined homing endstop

# IMPORTANT: These next three settings are based on the physical dimensions of your toolhead
# Once a homing position is determined, Happy Hare needs to know the final move distance to the nozzle. There is only
# one correct value for your setup - use 'toolhead_ooze_reduction' (which corresponds to the residual filament left in
# your nozzle) to control excessive oozing on load. See doc for table of proposed values for common configurations.
#
# NOTE: If you have a toolhead sensor you can automate the calculation of these parameters! Read about the
# `MMU_CALIBRATE_TOOLHEAD` command (https://github.com/moggieuk/Happy-Hare/wiki/Blobbing-and-Stringing#---calibrating-toolhead)
#
toolhead_extruder_to_nozzle: 72		# Distance from extruder gears (entrance) to nozzle
toolhead_sensor_to_nozzle: 62		# Distance from toolhead sensor to nozzle (ignored if not fitted)
toolhead_entry_to_extruder: 8		# Distance from extruder "entry" sensor to extruder gears (ignored if not fitted)

# This setting represents how much residual filament is left behind in the nozzle when filament is removed, it is thus
# used to reduce the extruder loading length and prevent excessive blobbing but also in the calculation of purge volume.
# Note that this value can also be measured with the `MMU_CALIBRATE_TOOLHEAD` procedure
#
toolhead_residual_filament: 0		# Reduction in extruder loading length because of residual filament left behind

# TUNING: Finally, this is the last resort tuning value to fix blobbing. It is expected that this value is NEAR ZERO as
# it represents a further reduction in extruder load length to fix blobbing. If using a wipetower and you experience blobs
# on it, increase this value (reduce the quantity of filament loaded). If you experience gaps, decrease this value. If gaps
# and already at 0 then perhaps the 'toolhead_extruder_to_nozzle' or 'toolhead_residual_filament' settings are incorrect.
# Similarly a value >+5mm also suggests the four settings above are not correct. Also see 'retract' setting in
# 'mmu_macro_vars.cfg' for final in-print ooze tuning.
#
toolhead_ooze_reduction: 0		# Reduction in extruder loading length to prevent ooze (represents filament remaining)

# Distance added to the extruder unload movement to ensure filament is free of extruder. This adds some degree of tolerance
# to slightly incorrect configuration or extruder slippage. However don't use as an excuse for incorrect toolhead settings
#
toolhead_unload_safety_margin: 10	# Extra movement safety margin (default: 10mm)

# If not synchronizing gear and extruder and you experience a "false" clog detection immediately after the tool change
# it might be because of a long bowden and/or large internal diameter that causes slack in the filament. This optional
# move will tighten the filament after a load by % of current clog detection length. Gear stepper will run at 50% current
#
toolhead_post_load_tighten: 60		# % of clog detection length, 0 to disable. Ignored if 'sync_to_extruder: 1'

# If synchronizing gear and extruder and you have a sync-feedback "buffer" this setting determines whether to use it
# to create neutral tension after loading
toolhead_post_load_tension_adjust: 1	# 1 to enable (recommended), 0 to disable

# If sync-feedback compression sensor is available this test will ensure the filament passes the extruder entry by checking
# for neutral tension when moving filament with just the extruder. Recommended with sprung loaded sync-feedback buffers.
# This is ignored if toolhead sensor is available.
toolhead_entry_tension_test: 1		# 1 to enable (recommended), 0 to disable

# ADVANCED: Controls the detection of successful extruder load/unload movement and represents the fraction of allowable
# mismatch between actual movement and that seen by encoder. Setting to 100% tolerance effectively turns off checking.
# Some designs of extruder have a short move distance that may not be picked up by encoder and cause false errors. This
# allows masking of those errors. However the error often indicates that your extruder load speed is too high or the
# friction is too high on the filament and in that case masking the error is not a good idea. Try reducing friction
# and lowering speed first!
#
toolhead_move_error_tolerance: 60


# Tip forming ---------------------------------------------------------------------------------------------------------
# ████████╗██╗██████╗     ███████╗ ██████╗ ██████╗ ███╗   ███╗██╗███╗   ██╗ ██████╗ 
# ╚══██╔══╝██║██╔══██╗    ██╔════╝██╔═══██╗██╔══██╗████╗ ████║██║████╗  ██║██╔════╝ 
#    ██║   ██║██████╔╝    █████╗  ██║   ██║██████╔╝██╔████╔██║██║██╔██╗ ██║██║  ███╗
#    ██║   ██║██╔═══╝     ██╔══╝  ██║   ██║██╔══██╗██║╚██╔╝██║██║██║╚██╗██║██║   ██║
#    ██║   ██║██║         ██║     ╚██████╔╝██║  ██║██║ ╚═╝ ██║██║██║ ╚████║╚██████╔╝
#    ╚═╝   ╚═╝╚═╝         ╚═╝      ╚═════╝ ╚═╝  ╚═╝╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝ ╚═════╝ 
#
# Tip forming responsibility can be split between slicer (in-print) and standalone macro (not in-print) or forced to always
# be done by Happy Hare's standalone macro. Since you always need the option to form tips without the slicer so it is
# generally easier to completely turn off the slicer, force "standalone" tip forming and tune only in Happy Hare.
#
# When Happy Hare is asked to form a tip it will run the referenced macro. Two are reference examples are provided but
# you can implement your own:
#   _MMU_FORM_TIP .. default tip forming similar to popular slicers like Superslicer and Prusaslicer
#   _MMU_CUT_TIP  .. for Filametrix (originally ERCFv2) or similar style toolhead filament cutting system
#
# NOTE: For MMU located cutting like the optional EREC cutter you should set still this to _MMU_FORM_TIP to build a decent
# tip prior to extraction and cutting after the unload.
#
# Often it is useful to increase the extruder current for the rapid movement to ensure high torque and no skipped steps
#
# If opting for slicer tip forming you MUST configure where the slicer leaves the filament in the extruder since
# there is no way to determine this. This can be ignored if all tip forming is performed by Happy Hare
#
force_form_tip_standalone: 1            # 0 = Slicer in print else standalone, 1 = Always standalone tip forming (TURN SLICER OFF!)
form_tip_macro: _MMU_FORM_TIP           # Name of macro to call to perform the tip forming (or cutting) operation
extruder_form_tip_current: 100          # % of extruder current (100%-150%) to use when forming tip (100 to disable)
slicer_tip_park_pos: 0                  # This specifies the position of filament in extruder after slicer completes tip forming


# Purging -------------------------------------------------------------------------------------------------------------
# ██████╗ ██╗   ██╗██████╗  ██████╗ ██╗███╗   ██╗ ██████╗ 
# ██╔══██╗██║   ██║██╔══██╗██╔════╝ ██║████╗  ██║██╔════╝ 
# ██████╔╝██║   ██║██████╔╝██║  ███╗██║██╔██╗ ██║██║  ███╗
# ██╔═══╝ ██║   ██║██╔══██╗██║   ██║██║██║╚██╗██║██║   ██║
# ██║     ╚██████╔╝██║  ██║╚██████╔╝██║██║ ╚████║╚██████╔╝
# ╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝╚═╝  ╚═══╝ ╚═════╝ 
#
# After a toolchange it is necessary to purge the old filament. Similar to tip forming this can be done by the slicer and/or
# by Happy Hare using an extension like Blobifer. If a purge_macro is defined it will be called when not printing or whenever
# the slicer isn't going to purge (like initial tool load). You can force it to always be called in a print by setting
# force_purge_standalone, but remember to turn off the slicer wipetower
#
# The default is for no (empty) macro so purging will not be done out of a print and thus wipetower. Two options are shipped with
# Happy Hare but you can also build your own custom one:
#   _MMU_PURGE .. default purging that just dumps the desired amount of filament (setup correct parking before enabling this!)
#   BLOBIFER   .. for excellent Blobifer addon (https://github.com/Dendrowen/Blobifier)
#
# Often it is useful to increase the extruder current for the often rapid puring movement to ensure high torque and no skipped steps
#
force_purge_standalone: 0               # 0 = Slicer wipetower in print else standalone, 1 = Always standalone purging (TURN WIPETOWER OFF!)
purge_macro: _MMU_PURGE			# Name of macro to call to perform the standalone purging operation. E.g. BLOBIFIER, _MMU_PURGE
extruder_purge_current: 100             # % of extruder current (100%-150%) to use when purging (100 to disable)


# Synchronized gear/extruder movement ----------------------------------------------------------------------------------
# ███╗   ███╗ ██████╗ ████████╗ ██████╗ ██████╗     ███████╗██╗   ██╗███╗   ██╗ ██████╗
# ████╗ ████║██╔═══██╗╚══██╔══╝██╔═══██╗██╔══██╗    ██╔════╝╚██╗ ██╔╝████╗  ██║██╔════╝
# ██╔████╔██║██║   ██║   ██║   ██║   ██║██████╔╝    ███████╗ ╚████╔╝ ██╔██╗ ██║██║     
# ██║╚██╔╝██║██║   ██║   ██║   ██║   ██║██╔══██╗    ╚════██║  ╚██╔╝  ██║╚██╗██║██║     
# ██║ ╚═╝ ██║╚██████╔╝   ██║   ╚██████╔╝██║  ██║    ███████║   ██║   ██║ ╚████║╚██████╗
# ╚═╝     ╚═╝ ╚═════╝    ╚═╝    ╚═════╝ ╚═╝  ╚═╝    ╚══════╝   ╚═╝   ╚═╝  ╚═══╝ ╚═════╝
#
# This controls whether the extruder and gear steppers are synchronized during printing operations
# If you normally run with maxed out gear stepper current consider reducing it with 'sync_gear_current'
# If equipped with TMC drivers the current of the gear and extruder motors can be controlled to optimize performance.
# This can be useful to control gear stepper temperature when printing with synchronized motor
#
sync_to_extruder: 0			# Gear motor is synchronized to extruder during print
sync_gear_current: 70			# % of gear_stepper current (10%-100%) to use when syncing with extruder during print
sync_form_tip: 0			# Synchronize during standalone tip formation (initial part of unload)
sync_purge: 0				# Synchronize during standalone purging (last part of load)

# Optionally it is possible to leverage feedback from a "compression/expansion" sensor (aka "buffer") in the bowden
# path from MMU to extruder to ensure that the two motors are kept in sync as viewed by the filament (the signal feedback
# state can be binary supplied by one or two switches: -1 (expanded) and 1 (compressed) of proportional value between
# -1.0 and 1.0.
#
# If only "one half" of the sync-feedback is available (either compression-only or tension-only) then the rotation
# distance is always shifted based on the high/low multipliers, however if both tension and compression are available
# then the rotation distance will autotune to correct setting (recommend you also enable 'autotune_rotation_distance: 1'
# Note that proportional feedback sensors are continuously dynamic
#
# Possible buffer setups, forth option for type where neutral is when both sensors are active:
#
#   <------maxrange------>       <------maxrange------>       <------maxrange------>       <------maxrange------>
#        <--range--->                  <----range----->       <----range----->                       <> range=0
#   |====================|       |====================|       |====================|       |====================|
#        ^          ^                  ^                                     ^                       ^^
#   compression   tension        compression-only                      tension-only
#
sync_feedback_enabled: 0		# Turn off even if sensor is installed and active
sync_feedback_buffer_range: 6		# Travel in "buffer" between compression/tension or one sensor and end (see above)
sync_feedback_buffer_maxrange: 12	# Absolute maximum end-to-end travel (mm) provided by buffer (see above)
sync_feedback_speed_multiplier: 5	# % "twolevel" gear speed delta to keep filament neutral in buffer (recommend 5%)
sync_feedback_boost_multiplier: 3	# % "twolevel" extra gear speed boost for finding initial neutral position (recommend 3%)
sync_feedback_extrude_threshold: 5	# Extruder movement (mm) for updates (keep small but set > retract distance)

# If defined this forces debugging to a telemetry log file "sync_<gate>.jsonl". This is great if trying to tune clog/tangle
# detection or for getting help on the Happy Hare forum. To plot graph of sync-feedback operation, run:
#  ~/Happy-Hare/utils/plot_sync_feedback.sh
#
sync_feedback_debug_log: 0		# 0 = disable (normal opertion), 1 = enable telemetry log (for debugging)


# ESpooler control -----------------------------------------------------------------------------------------------------
# ███████╗███████╗██████╗  ██████╗  ██████╗ ██╗     ███████╗██████╗ 
# ██╔════╝██╔════╝██╔══██╗██╔═══██╗██╔═══██╗██║     ██╔════╝██╔══██╗
# █████╗  ███████╗██████╔╝██║   ██║██║   ██║██║     █████╗  ██████╔╝
# ██╔══╝  ╚════██║██╔═══╝ ██║   ██║██║   ██║██║     ██╔══╝  ██╔══██╗
# ███████╗███████║██║     ╚██████╔╝╚██████╔╝███████╗███████╗██║  ██║
# ╚══════╝╚══════╝╚═╝      ╚═════╝  ╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═╝
#                                                                  
# If your MMU has a dc motor (often N20) controlled respooler/assist then how it operates can be controlled with these
# settings. Typically the espooler will be controlled with PWM signal. This will be at the maximum at speeds equal or
# above 'espooler.max_stepper_speed'. The PWM signal will scale downwards towards 0 for slower speeds. The falloff being
# controlled by the 'espooler_speed_exponent' setting according to this formula and allows for non-linear characteristics
# the DC motor (0.5 is a good starting value).
# 
#     espooler_pwm = (stepper_speed / espooler_max_stepper_speed) ^ {espooler_speed_exponent}
#
# Regardless of h/w configuration you can enable/disable actions with the 'espooler_operations' list. E.g. remove 'play' to
# turn off operation while printing. Options are:
#
#    rewind - when filament is being unloaded under MMU control (aka respool)
#    assist - when filament is being loaded under MMU control (% of "rewind" speed but with minimum of "print" power)
#    print  - while printing. Generally set 'espooler_printing_power' to a low percentage just to allow motor to be turned
#             freely or set to 0 to enable/allow "burst" assist movements
#
# If using a digitally controlled espooler motor (not PWM) then you should turn off the "print" mode and set
# 'espooler_min_stepper_speed' to prevent "over movement"
#
espooler_min_distance: 30			# Individual stepper movements less than this distance will not active espooler
espooler_max_stepper_speed: 300			# Gear stepper speed at which espooler will be at maximum power
espooler_min_stepper_speed: 0			# Gear stepper speed at which espooler will become inactive (useful for non PWM control)
espooler_speed_exponent: 0.5			# Controls non-linear espooler power relative to stepper speed (see notes)
espooler_assist_reduced_speed: 50		# Control the % of the rewind speed that is applied to assisting load (want rewind to be faster)
espooler_printing_power: 0			# If >0, fixes the % of PWM power while printing. 0=allows burst movement
espooler_operations: rewind, assist, print	# List of operational modes (allows disabling even if h/w is configured)
#
# The following burst configuration is used to control the small rotation in the ASSIST direction optionally used
# when in 'print' operation is enabled, 'espooler_printing_power: 0' and is triggered (tension switch or extruder movement).
# It can also be used to loosen filament with 'MMU_ESPOOLER COMMAND=assist BURST=1'
#
espooler_assist_extruder_move_length: 100	# Distance (mm) extruder needs to move between each assist burst
espooler_assist_burst_power: 100		# The % power of the burst move
espooler_assist_burst_duration: 0.4		# The duration of the burst move is seconds
espooler_assist_burst_trigger: 0		# If trigger assist switch is fitted 0=disable, 1=enable
espooler_assist_burst_trigger_max: 3		# If trigger assist switch is fitted this limits the max number of back-to-back advances
#
# The following burst configuration is used to control the small rotation in the REWIND direction optionally used
# when running running the filament drying cycle. The goal is to rotate the spool 60-90 degrees. It can also be
# used to tighten the filament with 'MMU_ESPOOLER COMMAND=rewind BURST=1'
#
espooler_rewind_burst_power: 100		# The % power of the rewind burst move
espooler_rewind_burst_duration: 0.4		# The duration of the rewind burst move is seconds


# Heater / Environment Management ------------------------------------------------------------------------------------
# ██╗  ██╗███████╗ █████╗ ████████╗███████╗██████╗
# ██║  ██║██╔════╝██╔══██╗╚══██╔══╝██╔════╝██╔══██╗
# ███████║█████╗  ███████║   ██║   █████╗  ██████╔╝
# ██╔══██║██╔══╝  ██╔══██║   ██║   ██╔══╝  ██╔══██╗
# ██║  ██║███████╗██║  ██║   ██║   ███████╗██║  ██║
# ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝
#
heater_max_temp: 70			# Absolute max heater setting to protect the MMU enclosure construction (adjust to match material)
heater_default_dry_temp: 45		# Default drying temperature if filament type is not matched in drying_data
heater_default_dry_time: 300		# Default drying cycle time in minutes
heater_default_humidity: 25		# Default humidity % goal. Drying will terminate if this value is reached
heater_vent_macro: _MMU_VENT		# Name of macro to periodicaly call during drying cycle
heater_vent_interval: 0			# Interval in minutes to call heater_vent_macro during drying cycle, 0=disable venting
heater_rotate_interval: 5		# Interval in minutes to rotate filament (requires eSpooler and filament end attached to spool)

# Drying data for MMU_HEATER DRY=1 command in form (material type is case insensitive):
#   'filament_type': (temp, drying_time_mins)
#
#   (Careful with formatting of this line - reformatting will break upgrade logic)
#
drying_data: { 'pla': (45, 300), 'pla+': (55, 300), 'petg': (60, 300), 'tpu': (55, 300), 'abs': (70, 300), 'abs+': (75, 300), 'asa': (65, 300), 'nylon': (75, 600), 'pc': (75, 600), 'pva': (75, 600), 'hips': (75, 600) }


# FlowGuard Clog and Tangle Detection --------------------------------------------------------------------------------
# ███████╗██╗      ██████╗ ██╗    ██╗ ██████╗ ██╗   ██╗ █████╗ ██████╗ ██████╗
# ██╔════╝██║     ██╔═══██╗██║    ██║██╔════╝ ██║   ██║██╔══██╗██╔══██╗██╔══██╗
# █████╗  ██║     ██║   ██║██║ █╗ ██║██║  ███╗██║   ██║███████║██████╔╝██║  ██║
# ██╔══╝  ██║     ██║   ██║██║███╗██║██║   ██║██║   ██║██╔══██║██╔══██╗██║  ██║
# ██║     ███████╗╚██████╔╝╚███╔███╔╝╚██████╔╝╚██████╔╝██║  ██║██║  ██║██████╔╝
# ╚═╝     ╚══════╝ ╚═════╝  ╚══╝╚══╝  ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝
#
# Options are available to automatically detects extruder clogs and MMU tangles. Each option works independently and
# can be combined. Flowguard can even discern the difference between an extruder clog and a spool tangle!
#
# Flowguard:  This intelligently measures filament tension (only available if sync-feedback buffer is fitted)
#
# Encoder detection: This monitors encoder movement and compares to extruder (only available if encoder is fitted)
#
flowguard_enabled: 1			# 0 = Flowguard protection disabled, 1 = Enabled

# The flowguard_max_relief is the amount of relief movement (effective mm change in filament length between MMU and extruder)
# that Happy Hare will wait until triggering a clog or runout. A smaller value is more sensitive to triggering. Since the
# relief movement is hightly dependent on filament "spring" in the bowden tube, filament friction, and
# 'sync_feedback_buffer_range', it is generally good to start high and then decrease if a more sensitive trigger is desired.
# Analog proportional (type P) sensors can generally have a much lower value. Increase if you have false triggers.
flowguard_max_relief: 40

# Encoder runout/clog/tangle detection watches for movement over either a static or automatically adjusted distance - if
# no encoder movement is seen when the extruder moves this distance runout/ clog/tangle event will be generated. Allowing
# the distance to be adjusted automatically (mode=2) will generally allow for a quicker trigger but use a static length
# (mode=1, set encoder_max_motion) if you get false triggers (see flowguard guide on wiki for more details)
# Note that this feature cannot disinguish between clog or tangle.
flowguard_encoder_mode: 2		# 0 = Disable, 1 = Static length clog detection, 2 = Automatic length clog detection

# The encoder_max_motion is the absolute max permitted extruder movement without the encoder seeing movement when using
# status mode (mode=1). Smaller values are more sensitive but beware of going too small - slack and friction in the
# bowden may cause gaps in encoder movement. Increase if you have false triggers
# Note that this value is overriden by any calibrated value stored in 'mmu_vars.cfg' if in automatic mode (mode=2).
flowguard_encoder_max_motion: 20


# Filament Management Options ----------------------------------------------------------------------------------------
# ███████╗██╗██╗            ███╗   ███╗ ██████╗ ███╗   ███╗████████╗
# ██╔════╝██║██║            ████╗ ████║██╔════╝ ████╗ ████║╚══██╔══╝
# █████╗  ██║██║            ██╔████╔██║██║  ███╗██╔████╔██║   ██║   
# ██╔══╝  ██║██║            ██║╚██╔╝██║██║   ██║██║╚██╔╝██║   ██║   
# ██║     ██║███████╗██╗    ██║ ╚═╝ ██║╚██████╔╝██║ ╚═╝ ██║   ██║   
# ╚═╝     ╚═╝╚══════╝╚═╝    ╚═╝     ╚═╝ ╚═════╝ ╚═╝     ╚═╝   ╚═╝   
#
# - EndlessSpool feature allows detection of runout on one spool and the automatic mapping of tool to an alternative
#   gate (spool). Set to '1', this feature requires clog detection or gate sensor or pre-gate sensors. EndlessSpool
#   functionality can optionally be extended to attempt to load an empty gate with 'endless_spool_on_load'. On some MMU
#   designs (with linear selector) it can also be configured to eject filament remains to a designated gate rather than
#   defaulting to current gate. A custom gate will disable pre-gate runout detection for EndlessSpool because filament
#   end must completely pass through the gate for selector to move
#
endless_spool_enabled: 1		# 0 = disable, 1 = enable endless spool
endless_spool_on_load: 0		# 0 = don't apply endless spool on load, 1 = run endless spool if gate is empty
endless_spool_eject_gate: -1		# Which gate to eject the filament remains. -1 = current gate
#endless_spool_groups:			# Default EndlessSpool groups (see later in file)
#
# Spoolman support requires you to correctly enable spoolman with moonraker first. If enabled, the gate SpoolId will
# be used to load filament details and color from the spoolman database and Happy Hare will activate/deactivate
# spools as they are used. The enabled variation allows for either the local map or the spoolman map to be the
# source of truth as well as just fetching filament attributes. See this table for explanation:
#
#                    | Activate/  | Fetch filament attributes | Filament gate    | Filament gate     |
#   spoolman_support | Deactivate | attributes from spoolman  | assignment shown | assignment pulled |
#                    | spool?     | based on spool_id?        | in spoolman db?  | from spoolman db? |
#   -----------------+------------+---------------------------+------------------+-------------------+
#        off         |     no     |           no              |        no        |        no         |
#        readonly    |     yes    |           yes             |        no        |        no         |
#        push        |     yes    |           yes             |        yes       |        no         |
#        pull        |     yes    |           yes             |        yes       |        yes        |
#
spoolman_support: off			# off = disabled, readonly = enabled, push = local gate map, pull = remote gate map
pending_spool_id_timeout: 20            # Seconds after which this pending spool_id (set with rfid) is voided
#
# Mainsail/Fluid UI can visualize the color of filaments next to the extruder/tool chooser. The color is dynamic and
# can be customized to your choice:
#
#    slicer   - Color from slicer tool map (what the slicer expects)
#    allgates - Color from all the tools in the gate map after running through the TTG map
#    gatemap  - As per gatemap but hide empty tools
#    off      - Turns off support
#
# Note: Happy Hare will also add the 'spool_id' variable to the Tx macro if spoolman is enabled
#
t_macro_color: slicer			# 'slicer' = default | 'allgates' = mmu | 'gatemap' = mmu without empty gates | 'off'


# Print Statistics ---------------------------------------------------------------------------------------------------
# ███████╗████████╗ █████╗ ████████╗███████╗
# ██╔════╝╚══██╔══╝██╔══██╗╚══██╔══╝██╔════╝
# ███████╗   ██║   ███████║   ██║   ███████╗
# ╚════██║   ██║   ██╔══██║   ██║   ╚════██║
# ███████║   ██║   ██║  ██║   ██║   ███████║
# ╚══════╝   ╚═╝   ╚═╝  ╚═╝   ╚═╝   ╚══════╝
#
# These parameters determine how print statistic data is shown in the console. This table can show a lot of data,
# probably more than you'd want to see. Below you can enable/disable options to your needs.
#
# +-----------+---------------------+----------------------+----------+
# |  114(46)  |      unloading      |       loading        | complete |
# |   swaps   | pre  |   -   | post | pre  |   -   | post  |   swap   |
# +-----------+------+-------+------+------+-------+-------+----------+
# | all time  | 0:07 | 47:19 | 0:00 | 0:01 | 37:11 | 33:39 |  2:00:38 |
# |     - avg | 0:00 |  0:24 | 0:00 | 0:00 |  0:19 |  0:17 |     1:03 |
# | this job  | 0:00 | 10:27 | 0:00 | 0:00 |  8:29 |  8:30 |    28:02 |
# |     - avg | 0:00 |  0:13 | 0:00 | 0:00 |  0:11 |  0:11 |     0:36 |
# |      last | 0:00 |  0:12 | 0:00 | 0:00 |  0:10 |  0:14 |     0:39 |
# +-----------+------+-------+------+------+-------+-------+----------+
#             Note: Only formats correctly on Python3
#
# Comma separated list of desired columns
# Options: pre_unload, form_tip, unload, post_unload, pre_load, load, purge, post_load, total
console_stat_columns: unload, load, post_load, total

# Comma separated list of rows. The order determines the order in which they're shown.
# Options: total, total_average, job, job_average, last
console_stat_rows: total, total_average, job, job_average, last

# How you'd want to see the state of the gates and how they're performing
#   string     - poor, good, perfect, etc..
#   percentage - rate of success
#   emoticon   - fun sad to happy faces (python3 only)
console_gate_stat: emoticon

# Always display the full statistics table
console_always_output_full: 1	# 1 = Show full table, 0 = Only show totals out of print


# Calibration and autotune -------------------------------------------------------------------------------------------
#  ██████╗ █████╗ ██╗     ██╗██████╗ ██████╗  █████╗ ████████╗██╗ ██████╗ ███╗   ██╗
# ██╔════╝██╔══██╗██║     ██║██╔══██╗██╔══██╗██╔══██╗╚══██╔══╝██║██╔═══██╗████╗  ██║
# ██║     ███████║██║     ██║██████╔╝██████╔╝███████║   ██║   ██║██║   ██║██╔██╗ ██║
# ██║     ██╔══██║██║     ██║██╔══██╗██╔══██╗██╔══██║   ██║   ██║██║   ██║██║╚██╗██║
# ╚██████╗██║  ██║███████╗██║██████╔╝██║  ██║██║  ██║   ██║   ██║╚██████╔╝██║ ╚████║
#  ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   ╚═╝ ╚═════╝ ╚═╝  ╚═══╝
#
# These are auto calibration/tuning settings that can be used to ease initial setup and/or to tune calibration over
# time based on measured telemetry. Whether these auto-tuning features are available depends on MMU design and
# configured sensors (explained below). The setting will be ignored if the required sensors are not available but if
# they can operate they will suppress the normal calibration warnings (MMU_STATUS can still be used to view them).
# Note that these are initially set by the installer to recommended values
#
#  autocal_bowden_length      - the calibrated bowden length will be established on first load. It can also be set
#                               manually or reset with MMU_CALIBRATE_BOWDEN. Best results require the use of
#                               sync-feedback-compression or extruder sensor but gear-touch or encoder will also work.
#                               'extruder_homing_endstop' cannot be 'none'
#  autotune_bowden_length     - Once calibrated this setting will tune the bowden distance over time. Works best with
#                               toolhead sensor
#  skip_cal_rotation_distance - This will rely on installed default value (although it can still be calibrated). Usually
#                               a good choice if autotune is enabled
#  autotune_rotation_distance - Requires sync-feedback sensor (aka "buffer") or calibrated encoder. If set then either the
#                               "autotuner" (sync-feedback buffer) or encoder telemetry will be used to adjust the
#                               persisted gear rotation distance.
#  skip_cal_encoder           - Will rely on installed default value (although it can still be calibrates).
#                               Not recommended but allows for easier initial setup especially when 'autotune_encoder'
#                               is enabled.
#  autotune_encoder           - NOT IMPLEMENTED YET. Soon!
#
autocal_bowden_length: 1	# Automated bowden length calibration. 1=automatic, 0=manual/off
autotune_bowden_length: 1	# Automated bowden length tuning. 1=on, 0=off
skip_cal_rotation_distance: 0	# Skip rotation distance calibration (MMU_CALIBRATE_GEAR), 1=skip, 0=require
autotune_rotation_distance: 0	# Automated gate calibration/tuning. 1=automatic, 0=manual/off
skip_cal_encoder: 0		# Skip encoder calibration (MMU_CALIBRATE_ENCODER), 1=skip, 0=require
autotune_encoder: 0		# Automated encoder tuning. 1=automatic, 0=manual/off


# Miscellaneous, but you should review -------------------------------------------------------------------------------
# ███╗   ███╗██╗███████╗ ██████╗
# ████╗ ████║██║██╔════╝██╔════╝
# ██╔████╔██║██║███████╗██║     
# ██║╚██╔╝██║██║╚════██║██║     
# ██║ ╚═╝ ██║██║███████║╚██████╗
# ╚═╝     ╚═╝╚═╝╚══════╝ ╚═════╝
#
# Important you verify these work for you setup/workflow. Temperature and timeouts
#
timeout_pause: 72000		# Idle time out (printer shuts down) in seconds used when in MMU pause state
disable_heater: 600		# Delay in seconds after which the hotend heater is disabled in the MMU_PAUSE state
default_extruder_temp: 200	# Default temperature for performing swaps and forming tips when not in print (overridden by gate map)
extruder_temp_variance: 2	# When waiting for extruder temperature this is the +/- permissible variance in degrees (>= 1)
#
# Other workflow options
#
startup_home_if_unloaded: 0	# 1 = force mmu homing on startup if unloaded, 0 = do nothing
startup_reset_ttg_map: 0	# 1 = reset TTG map on startup, 0 = do nothing
show_error_dialog: 1		# 1 = show pop-up dialog in addition to console message, 0 = show error in console
preload_attempts: 5		# How many "grabbing" attempts are made to pick up the filament with preload feature
strict_filament_recovery: 0	# If enabled with MMU with toolhead sensor, this will cause filament position recovery to
				# perform extra moves to look for filament trapped in the space after extruder but before sensor
filament_recovery_on_pause: 1	# 1 = Run a quick check to determine current filament position on pause/error, 0 = disable
retry_tool_change_on_error: 0	# Whether to automatically retry a failed tool change. If enabled Happy Hare will perform
				# the equivalent of 'MMU_RECOVER' + 'Tx' commands which usually is all that is necessary
				# to recover. Note that enabling this can mask problems with your MMU
bypass_autoload: 1		# If extruder sensor fitted this controls the automatic loading of extruder for bypass operation
has_filament_buffer: 1          # Whether the MMU has a filament buffer. Set to 0 if using Filamentalist or DC eSpooler, etc
#
# Advanced options. Don't mess unless you fully understand. Read documentation.
#
encoder_move_validation: 1	# ADVANCED: 1 = Normally Encoder validates move distances are within given tolerance
				#           0 = Validation is disabled (eliminates slight pause between moves but less safe)
print_start_detection: 1	# ADVANCED: Enabled for Happy Hare to automatically detect start and end of print and call
				# ADVANCED: MMU_PRINT_START and MMU_PRINT_END automatically. Harmless to leave enabled but can disable
                                #           if you think it is causing problems and known START/END is covered in your macros
extruder: extruder		# ADVANCED: Name of the toolhead extruder that MMU is using
gcode_load_sequence: 0		# VERY ADVANCED: Gcode loading sequence 1=enabled, 0=internal logic (default)
gcode_unload_sequence: 0	# VERY ADVANCED: Gcode unloading sequence, 1=enabled, 0=internal logic (default)


# ADVANCED: Klipper tuning -------------------------------------------------------------------------------------------
# ██╗  ██╗██╗     ██╗██████╗ ██████╗ ███████╗██████╗ 
# ██║ ██╔╝██║     ██║██╔══██╗██╔══██╗██╔════╝██╔══██╗
# █████╔╝ ██║     ██║██████╔╝██████╔╝█████╗  ██████╔╝
# ██╔═██╗ ██║     ██║██╔═══╝ ██╔═══╝ ██╔══╝  ██╔══██╗
# ██║  ██╗███████╗██║██║     ██║     ███████╗██║  ██║
# ╚═╝  ╚═╝╚══════╝╚═╝╚═╝     ╚═╝     ╚══════╝╚═╝  ╚═╝
#
# Timer too close is a catch all error, however it has been found to occur on some systems during homing and probing
# operations especially so with CANbus connected MCUs. Happy Hare uses many homing moves for reliable extruder loading
# and unloading and enabling this option affords klipper more tolerance and avoids this dreaded error
#
update_trsync: 0		# 1 = Increase TRSYNC_TIMEOUT, 0 = Leave the klipper default
#
# Some CANbus boards are prone to this but it have been seen on regular USB boards where a comms timeout will kill
# the print. Since it seems to occur only on homing moves they can be safely retried to workaround. This has been
# working well in practice
canbus_comms_retries: 3		# Number of retries. Recommend the default of 3.
#
# Older neopixels have very finicky timing and can generate lots of "Unable to obtain 'neopixel_result' response"
# errors in klippy.log. An often cited workaround is to increase BIT_MAX_TIME in neopixel.py. This option does that
# automatically for you to save dirtying klipper
update_bit_max_time: 1		# 1 = Increase BIT_MAX_TIME, 0 = Leave the klipper default
#
# BTT ViViD used a AHT30 sensor. If you are using an older klipper you may not have this sensor available. If so, use
# AHT10 and set this to 1 to convert AHT30 commands to AHT10
update_aht10_commands: 0	# 1 = Config AHT10 for BTT ViViD heater sensor on older klipper, 0 = Leave the klipper default


# ADVANCED: MMU macro overrides --- ONLY SET IF YOU'RE COMFORTABLE WITH KLIPPER MACROS -------------------------------
# ███╗   ███╗ █████╗  ██████╗██████╗  ██████╗ ███████╗
# ████╗ ████║██╔══██╗██╔════╝██╔══██╗██╔═══██╗██╔════╝
# ██╔████╔██║███████║██║     ██████╔╝██║   ██║███████╗
# ██║╚██╔╝██║██╔══██║██║     ██╔══██╗██║   ██║╚════██║
# ██║ ╚═╝ ██║██║  ██║╚██████╗██║  ██║╚██████╔╝███████║
# ╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝
#
# 'pause_macro' defines what macro to call on MMU error (must put printer in paused state)
# Other macros are detailed in 'mmu_sequence.cfg'
# Also see form_tip_macro in Tip Forming section and purge_macro in Purging section
#
pause_macro: PAUSE 					# What macro to call to pause the print
action_changed_macro: _MMU_ACTION_CHANGED		# Called when action (printer.mmu.action) changes
print_state_changed_macro: _MMU_PRINT_STATE_CHANGED	# Called when print state (printer.mmu.print_state) changes
mmu_event_macro: _MMU_EVENT				# Called on useful MMU events
pre_unload_macro: _MMU_PRE_UNLOAD			# Called before starting the unload
post_form_tip_macro: _MMU_POST_FORM_TIP			# Called immediately after tip forming
post_unload_macro: _MMU_POST_UNLOAD			# Called after unload completes
pre_load_macro: _MMU_PRE_LOAD				# Called before starting the load
post_load_macro: _MMU_POST_LOAD				# Called after the load is complete
unload_sequence_macro: _MMU_UNLOAD_SEQUENCE		# VERY ADVANCED: Optionally called based on 'gcode_unload_sequence'
load_sequence_macro: _MMU_LOAD_SEQUENCE			# VERY ADVANCED: Optionally called based on 'gcode_load_sequence'


# ADVANCED: See documentation for use of these -----------------------------------------------------------------------
# ██████╗ ███████╗███████╗███████╗████████╗    ██████╗ ███████╗███████╗███████╗
# ██╔══██╗██╔════╝██╔════╝██╔════╝╚══██╔══╝    ██╔══██╗██╔════╝██╔════╝██╔════╝
# ██████╔╝█████╗  ███████╗█████╗     ██║       ██║  ██║█████╗  █████╗  ███████╗
# ██╔══██╗██╔══╝  ╚════██║██╔══╝     ██║       ██║  ██║██╔══╝  ██╔══╝  ╚════██║
# ██║  ██║███████╗███████║███████╗   ██║       ██████╔╝███████╗██║     ███████║
# ╚═╝  ╚═╝╚══════╝╚══════╝╚══════╝   ╚═╝       ╚═════╝ ╚══════╝╚═╝     ╚══════╝
#
# These are the values that the various "RESET" commands will reset too rather than the built-in defaults. The lenght
# of the lists must match the number of gates on your MMU
#
# e.g. MMU_GATE_MAP RESET=1              - will use all the 'gate_XXX' values
#      MMU_TTG_MAP RESET=1               - will use the 'tool_to_gate_map'
#      MMU_ENDLESS_SPOOL_GROUPS RESET=1  - will use the 'endless_spool_groups'
#
# Gate:                #0      #1      #2      #3      #4      #5      #6      #7      #8
#gate_status:          1,      0,      1,      2,      2,     -1,     -1,      0,      1
#gate_filament_name:   one,    two,    three,  four,   five,   six,    seven,  eight,  nine
#gate_material:        PLA,    ABS,    ABS,    ABS+,   PLA,    PLA,    PETG,   TPU,    ABS
#gate_color:           red,    black,  yellow, green,  blue,   indigo, ffffff, grey,   black
#gate_temperature:     210,    240,    235,    245,    210,    200,    215,    240,    240
#gate_spool_id:        3,      2,      1,      4,      5,      6,      7,      -1,     9
#gate_speed_override:  100,    100,    100,    100,    100,    100,    100,    50,     100
#endless_spool_groups: 0,      1,      2,      1,      0,      0,      3,      4,      1
#
# Tool:                T0      T1      T2      T3      T4      T5      T6      T7      T8
#tool_to_gate_map:     0,      1,      2,      3,      4,      5,      6,      7,      8


# ADVANCED/CUSTOM MMU: See documentation for use of these ------------------------------------------------------------
#  ██████╗██╗   ██╗███████╗████████╗ ██████╗ ███╗   ███╗    ███╗   ███╗███╗   ███╗██╗   ██╗
# ██╔════╝██║   ██║██╔════╝╚══██╔══╝██╔═══██╗████╗ ████║    ████╗ ████║████╗ ████║██║   ██║
# ██║     ██║   ██║███████╗   ██║   ██║   ██║██╔████╔██║    ██╔████╔██║██╔████╔██║██║   ██║
# ██║     ██║   ██║╚════██║   ██║   ██║   ██║██║╚██╔╝██║    ██║╚██╔╝██║██║╚██╔╝██║██║   ██║
# ╚██████╗╚██████╔╝███████║   ██║   ╚██████╔╝██║ ╚═╝ ██║    ██║ ╚═╝ ██║██║ ╚═╝ ██║╚██████╔╝
#  ╚═════╝ ╚═════╝ ╚══════╝   ╚═╝    ╚═════╝ ╚═╝     ╚═╝    ╚═╝     ╚═╝╚═╝     ╚═╝ ╚═════╝ 
#
# Normally all these settings are set based on your choice of 'mmu_vendor' and 'mmu_version' in mmu_hardware.cfg, but they
# can be overridden. If you have selected a vendor of "Other" and your MMU has a selector you must set these CAD based
# dimensions else you will get arbitrary defaults. You may also need to set additional attributes in '[mmu_machine]'
# section of mmu_hardware.cfg.
#
#cad_gate0_pos: 4.2					# Approximate distance from endstop to first gate. Used for rough calibration only
#cad_gate_width: 21.0					# Width of each gate
#cad_bypass_offset: 0					# Distance from limit of travel back to the bypass (e.g. ERCF v2.0)
#cad_last_gate_offset: 2.0				# Distance from limit of travel back to last gate
#cad_selector_tolerance: 10.0				# How much extra selector movement to allow for calibration
#cad_gate_directions = [1, 1, 0, 0]			# Directions of gear depending on gate (3DChameleon)
#cad_release_gates = [2, 3, 0, 1]			# Gate to move to when releasing filament (3DChameleon)
