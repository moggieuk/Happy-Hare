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
# NOTE: Encoder cannot keep up much above 250mm/s so make sure `apply_bowden_correction` is off at very high speeds!
#
gear_from_buffer_speed: 160		# mm/s Conservative value is 100mm/s, Max around 350mm/s
gear_from_spool_speed: 60		# mm/s Use (lower) speed when loading from a gate for the first time (i.e. pulling from spool)
gear_short_move_speed: 60		# mm/s Conservative value is 35mm/s. Max around 100mm/s
gear_homing_speed: 50			# mm/s Speed of gear stepper only homing moves (e.g. extruder homing)

# Speeds of extruder movement. The 'sync' speeds will be used when gear and extruder steppers are moving in sync
extruder_load_speed: 15			# mm/s speed of load move inside extruder from homing position to meltzone
extruder_unload_speed: 20		# mm/s speed of unload moves inside of extruder (very initial move from meltzone is 50% of this)
extruder_sync_load_speed: 20		# mm/s speed of synchronized extruder load moves
extruder_sync_unload_speed: 25		# mm/s speed of synchronized extruder unload moves
extruder_homing_speed: 20		# mm/s speed of extruder only homing moves (e.g. to toolhead sensor)

# Selector movement speeds
selector_move_speed: 200        	# mm/s speed of selector movement (not touch)
selector_homing_speed: 60       	# mm/s speed of initial selector homing move (not touch)
selector_touch_speed: 80		# mm/s speed of all touch selector moves (if stallguard configured)
enable_selector_touch: 0		# If selector touch operation is possible this can be used to disable it 1=enabled, 0=disabled
```

This section controls the module that controls filament loading and unload at the gate when an encoder is present. The `encoder_unload_buffer` represents how close to the gate the filament ends up after fast bowden move. You want it close (for speed) but not too close that it can overshoot.  `encoder_parking_distance` is how fast away from the gate exit the filament should be parked when unloaded.  It rarely needs to be changed from the default.

```yml
# Encoder loading/unloading ------------------------------------------------------------------------------------------------
#
# These setttings control the optional encoder to load and unload filament at the gate
#
encoder_unload_buffer: 40		# Amount to reduce the fast unload so that accurate encoder unload has room to operate
encoder_load_retries: 2			# Number of times MMU will attempt to grab the filament on initial load (max 5)
encoder_parking_distance: 23.0		# Advanced: Controls parking postion in the gate (distance from encoder, range=12-30)
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
bowden_apply_correction: 0		# 1 to enable, 0 disabled (default)
bowden_load_tolerance: 15.0		# How close in mm the correction moves will attempt to get to target
bowden_num_moves: 1			# Number of separate fast moves to make when loading or unloading bowden (>1 if you have TTC errors)
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
# Whether the detection of successful extruder load is considered an error or warning. Some designs of extruder have a short
# final move distance that may not be picked up by encoder and cause false errors. This allows masking of those errors.
# However the error often indicates that your extruder load speed is too high for the friction on the filament and in
# that case masking the error is not a good idea. Try reducing friction, syncing motors and lowering speed first!
toolhead_ignore_load_error: 0
#
# Synchronized loading: It is generally recommended to load the toolhead with synchronized gear and extruder motors.
toolhead_sync_load: 1			# Extruder loading leverages motor synchronization
#
# However, if synchronized loading is disabled, there are two more settings can aid successful transition of the filament
# from the bowden tube through the extruder entrance into the toolhead
toolhead_transition_length: 10		# mm of special handling for entry and exit of extruder when not synced. 0 to disable
toolhead_delay_servo_release: 2.0	# Delay release on servo by (mm) when not using synchronous load during transition into toolhead
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
force_form_tip_standalone: 0		# 0 = Default smart behavor, 1 = Always do standalone tip forming (TURN SLICER OFF!)
```

Clog detection and EndlessSpool feature is well documented [here](https://github.com/moggieuk/Happy-Hare#5-clogrunout-detection-endlessspool-and-flowrate-monitoring).

```yml
# Clog detection and Endless Spool ---------------------------------------------------------------------------------------
# Selector (stallguard) operation. If configured for sensorless homing MMU can detect blocked filament path and try to recover
# automatically but it is slower and more difficult to set up (sensorless still requires the physical endstop switch)
# This is setup by defining stallguard homing on the selector_stepper and setting the physical endstop pin in mmu_hardware.cfg
#
enable_clog_detection: 2	# 0 = disable, 1 = static length clog detection, 2 = automatic length clog detection
enable_endless_spool: 1		# 0 = disable endless spool,  1 = enable endless spool (requires clog detection)
```

State persisence is a powerful feature of Happy Hare and is documented [here](https://github.com/moggieuk/Happy-Hare#2-state-and-persistence). I highly recommend level 4 as soon as you understand how it works.

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

This section contains an eclectic set of remianing options. Ask on discord if any aren't clear.

```yml
# Misc configurable, but fairly fixed values -----------------------------------------------------------------------------
#
extruder: extruder		# Name of the toolhead extruder that MMU is using
timeout_pause: 72000		# Time out in seconds used by the MMU_PAUSE
disable_heater: 600		# Delay in seconds after which the hotend heater is disabled in the MMU_PAUSE state
min_temp_extruder: 200		# Used to ensure we can move the extruder and form tips
z_hop_height: 5			# Height in mm of z_hop move on pause or runout to avoid blob on print
z_hop_speed: 15			# mm/s Speed of z_hop move
slicer_tip_park_pos: 0		# This specifies the position of filament in extruder after slicer tip forming move
gcode_load_sequence: 0		# Advanced: Gcode loading sequence 1=enabled, 0=internal logic (default)
gcode_unload_sequence: 0	# Advanced: Gcode unloading sequence, 1=enabled, 0=internal logic (default)
auto_calibrate_gates: 0		# Automated gate (not gate#0) calibration. 1=calibrated on first load, 0=disabled
strict_filament_recovery: 0	# If '1' with toolhead sensor, will look for filament trapped after extruder but before sensor
```

This final section is commented out because it is not generally needed. It retains abilities that existed in earlier versions of Happy Hare which may still be useful in some specific cases.  Normally when reset Happy Hare will default to empty or simple values for these settings. However, you can define the default here so that after a MMU reset has been performed they will be the starting values perhaps saving some additional configuration. E.g. if you always have specific filament spools loaded on a particular gate (I always have ABS black on gate #8 for example) you can define that here by setting the starting `gate_material` and `gate_color` arrays. Read [here](https://github.com/moggieuk/Happy-Hare#3-tool-to-gate-ttg-mapping) and [here](https://github.com/moggieuk/Happy-Hare#12-gate-map-describing-filament-type-color-and-status) for more details.

> [!Note]  
> Happy Hare will report error if these arrays are not the same length as the configured number of gates.

```yml
# Advanced: re-initialize behavior --- ONLY SET IF YOU REALLY WANT NON DEFAULT INITIALIZATION ----------------------------
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
# This group of settings collectively form the default gate map which can be updated with the `MMU_SET_GATE_MAP` command
# or similar commands that determine gate status. They must all be the same length at the number of gates (0 .. n)
# Note that these are the defaults and will be overriden by saved values in mmu_vars.cfg
#
# 1. The default mapping for tool to gate.  If not specified or commented out the mapping will default to Tx = Gate #x
#    'MMU_RESET_TTG_MAP' will revert to these default values. 'MMU_REMAP_TTG' will modify and persist during use.
#tool_to_gate_map: 0, 1, 2, 3, 4, 5, 6, 7, 8
#
# 2. Whether gate has filament available (2=available from buffer, 1=available from spool, 0=empty). If not specified or commentet
#    out the system default of all gates in an unknown state will be assumed
#    'MMU_SET_GATE_MAP' is used to adjust and persist during use
#gate_status: 1, 1, 1, 1, 1, 1, 1, 1, 1
#
# 3. Similarly this specifies the material type present in the gate. If not specified or commented out the name will be empty
#    'MMU_SET_GATE_MAP' is used to adjust and persist during use
#gate_material: PLA, ABS+, ABS, ABS, PLA, PLA, PETG, ABS, ABS
#
# 4. Similarly this specifies the color of the filament in each gate. If not specified or commented out the color will be default
#    Color can be w3c color name or RRGGBB (no leading #)
#    'MMU_SET_GATE_MAP' is used to adjust and persist during use
#gate_color: red, orange, yellow, green, blue, indigo, violet, ffffff, black
#
# 5. If endless spool is turned on, you should define a list of EndlessSpool groups here, one entry for each gate in your MMU
#    when filament runs out on a gate, it will switch to the next gate with the same group number
#    for example, if set to 1, 2, 3, 1, 2, 3, 1, 2, 3 on a 9 cart MMU, and a runout occurs on gate #0
#    the MMU will switch to using gate #3 and then gate #6 automatically remapping the tool as it goes.
#    Note that this will be overriden by a saved value in mmu_vars.cfg if modified with 'MMU_ENDLESS_SPOOL_GROUPS' command
#endless_spool_groups: 1, 2, 3, 1, 2, 3, 1, 2, 3
#
# For completeness and primarily for historical reasons rather than usefulness, the default position of each gate on the selector
# and the optional bypass position can be specified. These would only ever be used if 'mmu_vars.cfg' was deleted
#selector_offsets: 3.2, 24.2, 45.2, 71.3, 92.3, 113.3, 141.6, 162.6, 183.6
#selector_bypass: 123.4			# Set to your measured position, 0 to disable
```
