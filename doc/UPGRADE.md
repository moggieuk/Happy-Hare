# Important: Happy Hare upgrade notes
In v1.2.0, v1.2.1, v1.2.2 and v1.3.0 changes were made that altered the contents of the `ercf_hardware.cfg` and `ercf_parameters.cfg` as well as adding new klipper extra modules.  When upgrading to these versions it is necessary to also run:

 > cd /home/pi/ERCF-Software-V3<br>
 > ./install.sh

Optionally if you have multiple instances of Klipper running on a single rpi or have Klipper installed in a non-standard location you will also need to add `-c` and `-k` flags like so:

 > ./install.sh -k <klipper_home_dir> -c <klipper_config_dir>

This will attempt to update your `.cfg` files for you and install the additional klipper modules.  If successful nothing else is required. However if you are curious or run into problems, keep reading...

## For a full explanation of the changes read below:
Note you are advised to refer to the reference `ercf_hardware.cfg` and `ercf_parameters.cfg` files and compare to your own and see the full set of updates.

### v1.2.0
This release added persistence of ERCF state between restarts. The following was added to `ercf_parameters.cfg`

    # Advanced: ERCF can auto-initialize based on previous persisted state. There are 5 levels with each level bringing in
    # additional state information requiring progressively less inital setup. The higher level assume that you don't touch
    # ERCF while it is offline and it can come back to life exactly where it left off!  If you do touch it or get confused
    # then issue an appropriate reset command (E.g. ERCF_RESET) to get state back to the defaults.
    # Enabling `startup_status` is recommended if you use persisted state at level 2 and above
    # Levels: 0 = start fresh every time (the former default behavior)
    #         1 = restore persisted endless spool groups
    #         2 = additionally restore persisted tool-to-gate mapping
    #         3 = additionally restore persisted gate status (filament availability)
    #         4 = additionally restore persisted tool, gate and filament position!
    persistence_level: 2

    startup_status: 0  # Whether to log tool to gate status on startup, 1 = summary, 2 = full, 0 = disable


### v1.2.1
Introduced a custom servo driver that fixes the kickback issues that have plagued ERCF useage.  This driver synchronizes PWM transitions and is defined by a new [ercf_servo] definition in klipper.

Therefore it is necessary to change `[servo ercf_servo]` to `[ercf_servo ercf_servo]` in `ercf_hardware.cfg`

The following MUST BE REMOVED from `ercf_parameters.cfg`:

    extra_servo_dwell_up: 0    # Additional dwell time (ms) prior to turning off the servo (can help servo settle)
    extra_servo_dwell_down: 0  # Additional dwell time (ms) prior to turning off the servo (can help servo settle)

Also the following option was added to `ercf_parameters.cfg`:

    servo_duration: 0.2        # Duration of PWM burst sent to servo (automatically turns off)


### v1.2.2
Introduced a custom encoder driver that removes the need to `[duplicate_pin_override]` and the `[filament_motion_sensor]` in `ercf_hardware.cfg`. Which this change comes that ability to dynamically change clog detection during print and to choose a new automatic selection which will tune detection length automatically! 

This release requires a few changes:

Firstly completely remove the `[duplicate_pin_override]` and the `[filament_motion_sensor encoder_sensor]` sections. They are no longer used

Then add the follow to `ercf_hardware.cfg`

    ## ENCODER -----------------------------------------------------------------------------------------------------------------
    # The encoder_resolution is determined by running the ERCF_CALIBRATE_ENCODER. Be sure to read the manual
    [ercf_encoder ercf_encoder]
    encoder_pin: ^ercf:PA6           # EASY-BRD: ^ercf:PA6, Flytech ERB: ^ercf:gpio22
    encoder_resolution: 1.339226     # Set AFTER 'rotation_distance' is tuned for gear stepper (see manual)
    extruder: extruder               # The extruder to track with for runout/clog detection
    
    # These are advanced but settings for Automatic clog detection mode. Make sure you understand or ask questions on Discord
    desired_headroom: 5.0            # The runout headroom that ERCF will attempt to maintain (closest ERCF comes to triggering runout)
    average_samples: 4               # The "damping" effect of last measurement. Higher value means clog_length will be reduced more slowly

Note that the encoder_pin and the encoder_resolution should be copied from you `ercf_parameters.cfg` file

Finally remove the section that looks like this from `ercf_parameters.cfg`

    # Encoder setup. The encoder_pin must match the pin defined in the ercf_hardware.cfg
    # The encoder_resolution is determined by running the ERCF_CALIBRATE_ENCODER. Be sure to read the manual
    encoder_pin: ^ercf:PA6           # EASY-BRD: ^ercf:PA6, Flytech ERB: ^ercf:gpio22
    encoder_resolution: 1.339226     # Set AFTER 'rotation_distance' is tuned for gear stepper (see manual)

And note that `enable_clog_detection` now has 3 possible values (2 is recommended):

    enable_clog_detection: 2         # 0 = disable, 1 = static length clog detection, 2 = automatic length clog detection


### v1.3.0
Introduced synchronized gear/extruder movement during print!  This is implemented through a stepper controller that can both act as a MANUAL_STEPPER and as an extruder.  The `[manual_stepper gear_stepper]` section of `ercf_hardware.cfg` needs to be changed to `[manual_extruder_stepper gear_stepper]`. Also, the `extruder: extruder` line under `[ercf_encoder]` can be deleted.  It is harmless but the extruder name is now centrally set in `ercf_parameters.cfg`

In addition several new parameters are added to control gear/extruder syncing as well as the ability to any extruder for use with ERCF.

    extruder: extruder		# Name of the toolhead extruder that ERCF is using

    # EXPERIMENTAL: New synchronized gear/extruder movement!
    # If enabled for loading or unloading extruder this will override 'sync_load_length' and 'sync_unload_length'
    # If you normally run with maxed out gear stepper current consider reducing it with 'sync_gear_current'
    #
    sync_to_extruder: 0		# Gear motor is synchronized to extruder during print
    sync_load_extruder: 0	# Full extruder load leveraging motor synchronization
    sync_unload_extruder: 0	# Full extruder unload (except stand alone tip forming) leveraging motor synchronization
    sync_form_tip: 0		# Standalone tip formation (initial part of unload) also leveraging motor synchronization
    #
    sync_gear_current: 100	# Percentage of gear_stepper current (10%-100%) to use when syncing with extruder during print

