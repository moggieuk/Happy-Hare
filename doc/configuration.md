# Detailed Configuration Guide

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) mmu_parameters.cfg

The first section specifies the type of MMU and is used by Happy Hare to adjust options. It is documented in the main [README.md](https://github.com/moggieuk/Happy-Hare#1-important-mmu-vendor--version-specification).

```yml
[mmu]
#
# The vendor and version config is important to define the capabiliies of the MMU
#
# ERCF
# 1.1 original design, add "s" suffix for Sprigy, "b" for Binky, "t" for Triple-Decky
#     e.g. "1.1sb" for v1.1 with Spriny mod and Binky encoder
# 2.0 new community edition ERCF
#
# Tradrack
#  - Comming soon
#
# Prusa
#  - Comming soon
#
mmu_vendor: ERCF			# MMU family
mmu_version: 1.1			# MMU hardware version number (add mod suffix documented above)
mmu_num_gates: 9			# Number of selector gates
```

This important sections is where you define the hardware limitations of your build. These can be consisted the never to be exceeded settings but one important one if you are using `selector touch` operation is `selector_max_accel`. Since stallguard doesn't behave well at slow speed it is important that the accelation isn't set too low - below 600 causes problems, over 1000 ensures reliable operation.

```yml
# MMU Limits ---------------------------------------------------------------------------------------------------------------
#
# Define the physical limits of your MMU. These setings will be respected regardless of individual speed settings.
#
gear_max_velocity: 300			# Never to be exceeeded gear velocity regardless of specific parameters
gear_max_accel: 1500			# Never to be exceeeded gear accelaration regardless of specific parameters
selector_max_velocity: 250		# Never to be exceeeded selector velocity regardless of specific parameters
selector_max_accel: 1200		# Never to be exceeeded selector accelaration regardless of specific parameters
```

The servo configuration allos for up to three positions but some designs (e.g. ERCF v1.1) only require `up`/`down`.  If `move` is not used then comment it out or set it to the same value as `up`.  The servo duraction is the lemght of PWM burst.  Most digital servos only require a short 0.1 second or so but slower analog servos may require longer (0.4 - 0.5s).  Be very careful if you use the `servo_active_down` option because it will can strain your electronics.

```yml
# Servo configuration  -----------------------------------------------------------------------------------------------------
#
# Angle of the servo in three named positions:
#   up   = tool is selected and filament is allowed to freely move through gate
#   down = to grip filament
#   move = ready the servo for selector move (optional - defaults to up)
#
# Note that leaving the servo active when down can stress the electronics and is not recommended with EASY-BRD or ERB board
# unless the 5v power supply has been improved and it is not necessary with standard ERCF build.
# Make sure your hardware is suitable for the job!
#
servo_up_angle: 125			# Default: MG90S servo: Up~30    ; SAVOX SH0255MG: Up~140
servo_down_angle: 45			# Default: MG90S servo: Down~140 ; SAVOX SH0255MG: Down~30
servo_move_angle: 110			# Optional angle used when selector is moved (defaults to up position)
servo_duration: 0.2			# Duration of PWM burst sent to servo (automatically turns off)
servo_dwell: 0.5			# Minimum time given to servo to complete movement prior to next move
servo_active_down: 0			# CAUTION: 1=Force servo to stay active when down, 0=Release after movement
```

Logging controls control the verbosity level of logging to console and separate `mmu.log` file as well and fun visual filament position and various status messages - it really is unessessary to have verbose logging to the console so defaults are recommended.

```yml
# Logging ------------------------------------------------------------------------------------------------------------------
#
# log_level & logfile_level can be set to one of (0 = essential, 1 = info, 2 = debug, 3 = trace, 4 = developer)
# Generally you can keep console logging to a minimal whilst still sending debug output to the mmu.log file
# Increasing the console log level is only really useful during initial setup to save having to constantly open the log file
#
log_level: 1
log_file_level: 3			# Can also be set to -1 to disable log file completely
log_statistics: 1 			# 1 to log statistics on every toolchange (default), 0 to disable (but still recorded)
log_visual: 2				# 1 log visual representation of filament, 2 compact form (default) , 0 disable
log_startup_status: 1			# Whether to log tool to gate status on startup, 1 = summary (default), 2 = full, 0 = disable
```

All Happy Hare speeds can be configured in this section.  Most are self-explanatory and are separated into gear stepper speeds, speeds inside of the extruder (either just extruder motor or when synced with gear stepper) and selector movement.

```yml
# Movement speeds ----------------------------------------------------------------------------------------------------------
#
# Long moves are faster than the small ones and used for the bulk of the bowden movement. Note that you can set two fast load
# speeds depending on whether MMU thinks it is pulling from the buffer or from the spool. It is often helpful to use a lower
# speed when pulling from the spool because more force is required to overcome friction and this prevents loosing steps.
# 100mm/s should be "quiet" with the NEMA14 motor or a NEMA17 pancake, but you can go lower for really low noise
# NOTE: Encoder cannot keep up much above 250mm/s so make sure `bowden_apply_correction` is off at very high speeds!
#
gear_from_buffer_speed: 160		# mm/s Normal speed when loading filament. Conservative is 100mm/s, Max around 300mm/s
gear_from_buffer_accel: 400		# Normal accelaration when loading filament
gear_from_spool_speed: 60		# mm/s Use (lower) speed when loading from a gate for the first time (i.e. pulling from spool)
gear_from_spool_accel: 100		# Accelaration when loading from spool
gear_short_move_speed: 60		# mm/s Conservative value is 35mm/s. Max around 100mm/s
gear_short_move_accel: 400		# Usually the same as gear_from_buffer_accel (for short movements)
gear_short_move_threshold: 60		# Move distance that controls application of 'short_move' speed/accel
gear_homing_speed: 50			# mm/s Speed of gear stepper only homing moves (e.g. extruder homing)

# Speeds of extruder movement. The 'sync' speeds will be used when gear and extruder steppers are moving in sync
extruder_load_speed: 15			# mm/s speed of load move inside extruder from homing position to meltzone
extruder_unload_speed: 20		# mm/s speed of unload moves inside of extruder (very initial move from meltzone is 50% of this)
extruder_sync_load_speed: 20		# mm/s speed of synchronized extruder load moves
extruder_sync_unload_speed: 25		# mm/s speed of synchronized extruder unload moves
extruder_homing_speed: 20		# mm/s speed of extruder only homing moves (e.g. to toolhead sensor)

# Selector movement speeds. (Accelaration is defined by physical MMU limits set above and passed to selector stepper driver)
selector_move_speed: 200        	# mm/s speed of selector movement (not touch)
selector_homing_speed: 60       	# mm/s speed of initial selector homing move (not touch)
selector_touch_speed: 80		# mm/s speed of all touch selector moves (if stallguard configured)
selector_touch_enable: 0		# If selector touch operation is possible this can be used to disable it 1=enabled, 0=disabled
```

This section controls the module that controls filament loading and unload at the gate when an encoder is present. The `encoder_unload_buffer` represents how close to the gate the filament ends up after fast bowden move. You want it close (for speed) but not too close that it can overshoot.  `encoder_parking_distance` is how fast away from the gate exit the filament should be parked when unloaded.  It rarely needs to be changed from the default.

```yml
# Gate/Encoder loading/unloading -------------------------------------------------------------------------------------------
#
# These setttings control the method of loading and unloading the gate and optional encoder configuration
#
gate_homing_endstop: encoder		# Name of gate endstop, "encoder" forces use of encoder for parking (ERCF default)
gate_homing_max: 70			# Maximum move distance to home to the gate (actual move distance for encoder parking)
gate_unload_buffer: 50			# Amount to reduce the fast unload so that filament doesn't overshoot
gate_load_retries: 2			# Number of times MMU will attempt to grab the filament on initial load (max 5)
gate_parking_distance: 23		# Advanced: Specifies parking postion in the gate (distance from gate endstop/encoder)
```

For more information on the bowden correct move, read about the loading sequence [here](https://github.com/moggieuk/Happy-Hare#---filament-loading-and-unloading-sequences).  The `bowden_num_moves` allows a long move to be broken into separate moves.  Only increase this if Klipper throws errors with very long moves - setting it higher than `1` will long down the loading process.

```yml
# Bowden tube loading/unloading --------------------------------------------------------------------------------------------
#
# In addition to different bowden loading speeds for buffer and non-buffered filament it is possible to detect missed steps
# caused by "jerking" on a heavy spool. If bowden correction is enabled the driver with "believe" the encoder reading and
# make correction moves to bring the filament to within the 'load_bowden_tolerance' of the end of bowden position
# (this does require a reliable encoder and is not recommended for very high speed loading >200mm/s)
#
bowden_num_moves: 1			# Number of separate fast moves when loading or unloading bowden (>1 only if you have TTC errors)
bowden_apply_correction: 0		# 1 to enable, 0 disabled (default) [Requires Encoder]
bowden_load_tolerance: 20.0		# How close in mm the correction moves will attempt to get to target [Requires Encoder]
#
# This test verifies the filament is free of extruder before the fast bowden movement to reduce possibility of grinding filament
bowden_pre_unload_test: 1		# 1 to check for bowden movement before full pull (slower), 0 don't check (faster) [Requires Encoder]
#
# Advanced: If pre-unload test is enabled, this controls the detection of successful bowden pre-unload test and represents the
# fraction of allowable mismatch between actual movement and that seen by encoder. Setting to 50% tolerance usually works well.
# Increasing will make test more tolerent. Value of 100% essentially disabled detection
bowden_pre_error_percent: 50
```

This section controls the optional extruder homing step. The `extruder_homing_endstop` is either a real endstop name or the string "collision" which causes Happy Hare to "feel" for the extruder entrance.  If other options dictate this homing step it will automatically be performed, however it is possible to force it even when not strickly needed by setting the `extruder_force_homing: 1`.

```yml
# Extruder entrance detection/homing ---------------------------------------------------------------------------------------
#
# If not using a toolhead sensor (homing endpoint) the driver can "feel" for the extruder gear entry by colliding with it
# and thus needs to know how far to attempt homing. Because this method is not completely deterministic you might find
# have to find the sweetspot for your setup by adjusting the TMC current reduction. Also, touch (stallguard) sensing is
# possible to configure but unfortunately doesn't work well with external EASY-BRD or ERB mcu's.
# Reduced current during collision detection can also prevent filament griding
#
extruder_homing_max: 50			# Maximum distance to advance in order to attempt to home the extruder
extruder_homing_endstop: collision	# Filament homing method/endstop name ("mmu_gear_touch" for stallguard) or "collision"
extruder_homing_current: 40		# % gear_stepper current (10%-100%) to use when homing to extruder homing (100 to disable)
#
# In the absence of a toolhead sensor Happy Hare will automatically default to extruder entrance detection regardless of
# this setting, however if you have a toolhead sensor you can still force the additional (unecessary and not recommended)
# step of homing to extruder entrance before then homing to the toolhead sensor
extruder_force_homing: 0
```

This section controls the module responsible for loading filament into and unloading from the extruder/toolhead. There are many options and the notes below and in the file explain the options already.  Note that the default of synchronized loading and non-synchronized unloading is recommended. Read about the loading and unloading sequences [here](https://github.com/moggieuk/Happy-Hare#---filament-loading-and-unloading-sequences).

```yml
# Built in default toolhead loading and unloading -------------------------------------------------------------------------
#
# It is possible to define highly customized loading and unloading sequences, however, unless you have a specialized setup
# it is probably easier to opt for the built-in toolhead loading and unloading sequence which already offers a high degree
# of customization. If you need even more control then edit the _MMU_LOAD_SEQUENCE and __MMU_UNLOAD_SEQUENCE macros in
# mmu_software.cfg - but be careful!
#
# An MMU must have a known point at the end of the bowden from which it can precisely load the extruder. Generally this will
# either be the extruder extrance (which is controlled with settings above) or by homing to toolhead sensor. If you have
# toolhead sensor it is past the extruder gear and the driver needs to know the max distance (from end of bowden move) to
# attempt homing
#
toolhead_homing_max: 40			# Maximum distance to advance in order to attempt to home to toolhead sensor
#
# Once a homing position is determined, Happy Hare needs to know the final move distance to the nozzle. If homing to
# toolhead sensor this will be the distance from the toolhead sensor to the nozzle. If extruder homing it will be the
# distance from the extruder gears to the nozzle. Set the appropriate parameter for your setup
#
# This value can be determined by manually inserting filament to your homing point (extruder gears or toolhead sensor)
# and advancing it 1-2mm at a time until it starts to extrude from the nozzle.  Subtract 1-2mm from that distance distance
# to get this value.  If you have large gaps in your purge tower, increase this value.  If you have blobs, reduce this value.
# This value will depend on your extruder, hotend and nozzle setup.
# (Note that the difference between these two represents the extruder to sensor distance and is used as the final
# unload distance from extruder. An accurate setting can reduce tip noise/grinding on exit from extruder)
toolhead_extruder_to_nozzle: 72		# E.g. Revo Voron with CW2 extruder using extruder homing
toolhead_sensor_to_nozzle: 62		# E.g. Revo Voron with CW2 extruder using toolhead sensor homing
#
# Distance added to the extruder unload movement to ensure filament is free of extruder. Must be less than 'gate_unload_buffer`
toolhead_unload_safety_margin: 10
#
# Controls the detection of successful extruder load/unload movement and represents the fraction of allowable mismatch between
# actual movement and that seen by encoder. Setting to 100% tolerance effectively turns off checking. Some designs of extruder
# have a short move distance that may not be picked up by encoder and cause false errors. This allows masking of those errors.
# However the error often indicates that your extruder load speed is too high for the friction on the filament and in
# that case masking the error is not a good idea. Try reducing friction and lowering speed first!
toolhead_move_error_percent: 60
#
# Synchronized unloading: It is recommended not to enable synced motors during unloading because (i) it makes it harder to
# detect stuck filament, (ii) it can lead to additional noise, (iii) it is possible to "over unload". Nevertheless, it can
# be employed if you extruder struggles to unload
toolhead_sync_unload: 0			# Extruder unloading (except stand alone tip forming) leverages motor synchronization
```
Happy Hare has the ability to synchronize various motors during printing operation and this section controls those options. Make sure you have [understand the caution](https://github.com/moggieuk/Happy-Hare#4-synchronized-gearextruder-motors) needed when `sync_to_extruder: 1` is enabled.

> [!NOTE]  
> Setting `force_form_tip_standalone: 1` will cause Happy Hare to always run the supplied tip shaping macro.  If you set this then make sure your slicer is not adding tip shaping logic of its own else tips will attempt to be created twice and knowledge of the filament position in the extruder may become inaccurate

```yml
# Synchronized gear/extruder movement and tip forming ----------------------------------------------------------------------
#
# This controls whether the extruder and gear steppers are synchronized during printing operations
# If you normally run with maxed out gear stepper current consider reducing it with 'sync_gear_current'
# If equipped with TMC drivers the current of the gear and extruder motors can be controlled to optimize performance.
# This can be useful to control gear stepper temperature when printing with synchronized motor, to ensure no skipping during
# fast tip-forming moves
#
sync_to_extruder: 0			# Gear motor is synchronized to extruder during print
sync_gear_current: 50			# % of gear_stepper current (10%-100%) to use when syncing with extruder during print
sync_form_tip: 0			# Synchronize during standalone tip formation (initial part of unload)
#
# Tip forming responsibity is typically split between slicer (in-print) and standalone macro (not in-print). Whilst there is
# an option to choose for every toolchange, setting 'force_form_tip_standalone: 1' will always do the standalone sequence
# Often it is useful to increase the current for this generally rapid movement
#
extruder_form_tip_current: 100		# % of extruder current (100%-150%) to use when forming tip (100 to disable)
force_form_tip_standalone: 0		# 0 = Default smart behavior, 1 = Always do standalone tip forming (TURN SLICER OFF!)
```

Clog detection and EndlessSpool feature is well documented [here](https://github.com/moggieuk/Happy-Hare#5-clogrunout-detection-endlessspool-and-flowrate-monitoring).

```yml
# Clog detection, Endless Spool, SpoolMan ----------------------------------------------------------------------------------
#
# Selector (stallguard) operation. If configured for sensorless homing MMU can detect blocked filament path and try to recover
# automatically but it is slower and more difficult to set up (sensorless still requires the physical endstop switch)
# This is setup by defining stallguard homing on the selector_stepper and setting the physical endstop pin in mmu_hardware.cfg
#
enable_clog_detection: 2	# 0 = disable, 1 = static length clog detection, 2 = automatic length clog detection
enable_endless_spool: 1		# 0 = disable endless spool,  1 = enable endless spool (requires clog detection)
enable_spoolman: 0		# 0 = disable spoolman support,  1 = enable spoolman (requires spoolman setup)
```

State persisence is a powerful feature of Happy Hare and is documented [here](https://github.com/moggieuk/Happy-Hare#2-state-and-persistence). I highly recommend level `4` as soon as you understand how it works.

```yml
# Turn on behavior -------------------------------------------------------------------------------------------------------
# MMU can auto-initialize based on previous persisted state. There are 5 levels with each level bringing in
# additional state information requiring progressively less inital setup. The higher level assume that you don't touch
# MMU while it is offline and it can come back to life exactly where it left off!  If you do touch it or get confused
# then issue an appropriate reset command (E.g. MMU_RESET) to get state back to the defaults.
# Enabling `startup_status` is recommended if you use persisted state at level 2 and above
# Levels: 0 = start fresh every time except calibration data (the former default behavior)
#         1 = restore persisted endless spool groups
#         2 = additionally restore persisted tool-to-gate mapping
#         3 = additionally restore persisted gate status (filament availability, material and color) (default)
#         4 = additionally restore persisted tool, gate and filament position! (Recommended when MMU is working well)
#
persistence_level: 3
```

This section contains an eclectic set of remaining options. Ask on discord if any aren't clear, however a couple warrant further explantion:<br>
`default_extruder_temp` - This is the default temperature for performing swaps and tip forming when outside of a print. It's also a fallback in the event that your printer tries to print with an unsafe temperature after a pause. When printing, the slicer will be responsible for setting the temperature. You may want to set this to a middleground temperature that works "well enough" with the full range of filaments you regularly print.<br>
`slicer_tip_park_pos` - If you use the default slicer tip shaping logic then it will leave the filament at a particular place in the extruder. Unfortunately Happy Hare has no way to detect this like it can when it takes care of tip shaping. This parameter usually exists in the slicer and setting it will pass on to Happy Hare for more efficient subsequent unloading.<br>
`auto_calibrate_gates` - discussed in main readme but avoids having to calibrate since that are automatically calibrated on first use.<br>
`strict_filament_recovery` - Occassionaly Happy Hare will be forced to try to figure our where the filament is. It employs various mechanisms to achive this depending on the capability of the MMU. Some of this steps are invasive (e.g. warming the extruder when it is cold) and are therefore skipped by default. Enabling this option will force extra detection steps.
`retry_tool_change_on_error` - This setting defaults to off (0) because it can hide problems with your MMU, however, if enabled (1) it will cause Happy Hare to automatically retry a failed tool change but performing the equivalent commands as `MMU_RECOVER` + `Tx`.  It is useful for long prints to minimize "baby-sitting" false failures.
`print_start_detection` - Default is `1` which will cause Happy Hare to correctly initialize the MMU on print start and finalize on print end. Set to `0` if you wish to include `_MMU_PRINT_START` and `_MMU_PRINT_END` directly in your own print start/end macros.


```yml
# Misc configurable, but fairly fixed values -----------------------------------------------------------------------------
#
extruder: extruder		# Name of the toolhead extruder that MMU is using
timeout_pause: 72000		# Idle time out in seconds used when in MMU pause state
disable_heater: 600		# Delay in seconds after which the hotend heater is disabled in the MMU_PAUSE state
default_extruder_temp: 200	# The baseline temperature for performing swaps and forming tips outside of a print
z_hop_height_error: 5		# Height in mm of z_hop move on pause to avoid blob on print
z_hop_height_toolchange: 0	# Height in mm of z_hop move on toolchange or runout to avoid blob on print
z_hop_speed: 15			# Speed of z_hop move (mm/s)
slicer_tip_park_pos: 0		# This specifies the position of filament in extruder after slicer tip forming move
gcode_load_sequence: 0		# Advanced: Gcode loading sequence 1=enabled, 0=internal logic (default)
gcode_unload_sequence: 0	# Advanced: Gcode unloading sequence, 1=enabled, 0=internal logic (default)
auto_calibrate_gates: 0		# Automated gate (not gate#0) calibration. 1=calibrated automatically on first load, 0=disabled
strict_filament_recovery: 0	# If enabled with MMU with toolhead sensor, this will cause filament position recovery to
				# perform extra moves to look for filament trapped in the space after extruder but before sensor
retry_tool_change_on_error: 0	# Whether to automatically retry a failed tool change. If enabled Happy Hare will perform
				# the equivalent of 'MMU_RECOVER' + 'Tx' commands which usually is all that is necessary
				# to recover. Note that enabling this can mask problems with your MMU
print_start_detection: 1	# Enabled for Happy Hare to automatically detect start and end of print and call
				# _MMU_START_PRINT and _MMU_END_PRINT. Disable if you want to include in your own macros
encoder_move_validation: 1	# 1 = Normally Encoder validates move distances are within given tolerence (slower but more safe)
				# 0 = Validation is disabled for many moves (eliminates slight pause between moves but less safe)
```

This section contains a list of overrides for macros that Happy Hare calls internally. Currently, there's the option to override the `PAUSE` macro and the `_MMU_FORM_TIP_STANDALONE` macro but other macros or arguments may be added in the future.

```yml
# Advanced: MMU macro overrides --- ONLY SET IF YOU'RE COMFORTABLE WITH KLIPPER MACROS -----------------------------------

# When a print or the MMU should be paused, Happy Hare will call the `PAUSE` macro by default. If you want additional
# behavior, you can override the macro that is called by Happy Hare. Some examples as to why you may want this:
# 1. You are using a sparse purge tower and you want Happy Hare errors to park above your purge tower as to not hit
#    any models that are between your tower and normal pause location
# 2. You want to additionally call a macro that sends a push notification on filament swap error
# 3. You want to set additional static arguments to either the default pause macro or your own macro
#
# IMPORTANT: Whatever macro you call _must_ ultimately leave the printer in a paused state. Failure to do so will result
#            in failed prints, jams, and physical hardware crashes
#
pause_macro: PAUSE

# When Happy Hare is asked to form a tip it will run this macro. It can be convenient to change it if you want to replace
# with a macro that performs filament cutting.
#
# IMPORTANT: Because Happy Hare watches the filament movement during the tool tip procedure to establish the park_position after
#            the move, you must override this behavior by setting the `output_park_pos` variable if you cut the filament.
#            The value can be set dynamically in gcode with this construct:
#                SET_GCODE_VARIABLE MACRO=_MMU_FORM_TIP_STANDALONE VARIABLE=output_park_pos VALUE=-1
#
form_tip_macro: _MMU_FORM_TIP_STANDALONE
```

This final section is commented out because it is not generally needed. It retains abilities that existed in earlier versions of Happy Hare which may still be useful in some specific cases.  Normally when reset Happy Hare will default to empty or simple values for these settings. However, you can define the default here so that after a MMU reset has been performed they will be the starting values perhaps saving some additional configuration. E.g. if you always have specific filament spools loaded on a particular gate (I always have ABS black on gate #8 for example) you can define that here by setting the starting `gate_material` and `gate_color` arrays. Read [here](https://github.com/moggieuk/Happy-Hare#3-tool-to-gate-ttg-mapping) and [here](https://github.com/moggieuk/Happy-Hare#12-gate-map-describing-filament-type-color-and-status) for more details.

> [!Note]  
> Happy Hare will report error if these arrays are not the same length as the configured number of gates.

```yml
# ADVANCED: Re-initialize behavior --- ONLY SET IF YOU REALLY WANT NON DEFAULT INITIALIZATION ----------------------------
#
# Happy Hare has advanced features like:
# 1. Managing a tool to gate mapping so you can remap incorrectly spools or map all tools to one gate for mono color prints!
# 2. Remembering the state of (presence) of filament in each gate
# 3. The filament material loaded in each gate
# 4. The filament color in each gate
# 5. Grouping gates (spools) into Endless Spool groups
#
# Typically these will be set dynamically over time and automatically saved to 'mmu_vars.cfg'.  When you power up your MMU
# these values are loaded. However, if you explicity reset your MMU state through one of the many reset commands, these values
# will be restored to a default. The system default values are typically empty or in the case of TTG map, 1:1 mapping of
# Tx to Gate #x, or no Endless Spool groups.  However you have the option to define starting values here.
# IMPORTANT: the arrays of values must be the same length as the number of gates on your MMU otherwise they will be rejected.
#
# This group of settings collectively form the default gate map which can be updated with the `MMU_GATE_MAP` command
# or similar commands that determine gate status. They must all be the same length at the number of gates (0 .. n)
# Note that these are the defaults and will be overriden by saved values in mmu_vars.cfg
#
# 1. The default mapping for tool to gate.  If not specified or commented out the mapping will default to Tx = Gate #x
#    'MMU_RESET_TTG_MAP' will revert to these default values. 'MMU_REMAP_TTG' will modify and persist during use.
#tool_to_gate_map: 0, 1, 2, 3, 4, 5, 6, 7, 8
#
# 2. Whether gate has filament available (2=available from buffer, 1=available from spool, 0=empty). If not specified or commentet
#    out the system default of all gates in an unknown state will be assumed
#    'MMU_GATE_MAP' is used to adjust and persist during use
#gate_status: 1, 1, 1, 1, 1, 1, 1, 1, 1
#
# 3. Similarly this specifies the material type present in the gate. If not specified or commented out the name will be empty
#    'MMU_GATE_MAP' is used to adjust and persist during use
#gate_material: PLA, ABS+, ABS, ABS, PLA, PLA, PETG, ABS, ABS
#
# 4. Similarly this specifies the color of the filament in each gate. If not specified or commented out the color will be default
#    Color can be w3c color name or RRGGBB (no leading #)
#    'MMU_GATE_MAP' is used to adjust and persist during use
#gate_color: red, orange, yellow, green, blue, indigo, violet, ffffff, black
#
# 5. If endless spool is turned on, you should define a list of EndlessSpool groups here, one entry for each gate in your MMU
#    when filament runs out on a gate, it will switch to the next gate with the same group number
#    for example, if set to 1, 2, 3, 1, 2, 3, 1, 2, 3 on a 9 cart MMU, and a runout occurs on gate #0
#    the MMU will switch to using gate #3 and then gate #6 automatically remapping the tool as it goes.
#    Note that this will be overriden by a saved value in mmu_vars.cfg if modified with 'MMU_ENDLESS_SPOOL_GROUPS' command
#endless_spool_groups: 1, 2, 3, 1, 2, 3, 1, 2, 3
#
# 6. If spoolman is active, you can here define the gate to spoolId relation
#gate_spool_id: 3,2,1,4,5,6,7,8,9

# For completeness and primarily for historical reasons rather than usefulness, the default position of each gate on the selector
# and the optional bypass position can be specified. These would only ever be used if 'mmu_vars.cfg' was deleted
#selector_offsets: 3.2, 24.2, 45.2, 71.3, 92.3, 113.3, 141.6, 162.6, 183.6
#selector_bypass: 123.4			# Set to your measured position, 0 to disable
```
