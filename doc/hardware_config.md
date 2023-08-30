# Hardware configuration, Movement and Homing

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Hardware configuration (mmu_hardware.cfg explained)

This will vary slightly depending on your particular brand of MMU but the steps are essentially the same with some being dependent on hardware configuration.

## Step 1. Validate your hardware configuration

### Location of configuration files
The Klipper configuration files for Happy Hare are modular and can be found in this layout in the Klipper config directory:

```yml
mmu/
  base/
    mmu.cfg
    mmu_hardware.cfg
    mmu_software.cfg
    mmu_parameters.cfg

  optional/
    mmu_menu.cfg
    mmu_ercf_compat.cfg
    client_macros.cfg

  mmu_vars.cfg
```

This makes the minimal include into your printer.cfg easy: `[include mmu/base/*.cfg]'

### a) MCU and Pin Validation (mmu.cfg)
The `mmu.cfg` file is part of the hardware configuration but defines aliases for all of the pins used in `mmu_hardware.cfg`. The benefit of this is that configuration frameworks like [Klippain](https://github.com/Frix-x/klippain) can more easily incorporate. It is also in keeping with an organized modular layout.

<br>

### b) Hardware Configuration (mmu_hardware.cfg):
This can be daunting but the interactive installer will make the process easy for common mcu's designed for a MMU (e.g. ERCF EASY-BRD, Burrows ERB, etc)

Assuming you are familiar with all that there is one new IMPORTANT step that must be performed by hand.  You must move most of your `[extruder]` definition into `mmu_hardware.cfg`. This is best illustrated with my actual configuration (pulled from the top of `mmu_hardware.cfg`):
  
```yml
# HOMING CAPABLE EXTRUDER --------------------------------------------------------------------------------------------------
# With Happy Hare, it is important that the extruder stepper definition is moved here to allow for sophisticated homing and syncing
# options.  This definition replaces the stepper definition part of you existing [extruder] definition.
#
# IMPORTANT: Move the complete stepper driver configuration associated with regular extruder here
[tmc2209 manual_extruder_stepper extruder]
uart_pin: EXT_UART
interpolate: true
run_current: 0.55			# LDO 36STH20-1004AHG.  Match to macro below
hold_current: 0.4
sense_resistor: 0.110
stealthchop_threshold: 0		# Spreadcycle (better for extruder)
#
# Uncomment two lines below if you have TMC and want the ability to use filament "touch" homing to nozzle
diag_pin: EXT_DIAG			# Set to MCU pin connected to TMC DIAG pin for extruder
driver_SGTHRS: 100			# 255 is most sensitive value, 0 is least sensitive

# Define just your printer's extruder stepper here. Valid config options are:
# step_pin, dir_pin, enable_pin, rotation_distance, gear_ratio, microsteps, full_steps_per_rotation
# pressure_advance, pressure_advance_smooth_time
# IMPORTANT: REMOVE these settings from your existing [extruder] configuration BUT LEAVE ALL OTHER parameters!
#
[manual_extruder_stepper extruder]
step_pin: EXT_STEP
dir_pin: EXT_DIR
enable_pin: EXT_ENABLE
microsteps: 64
rotation_distance: 22.4522		# Calibrated by hand
gear_ratio: 50:10
full_steps_per_rotation: 200
pressure_advance: 0.035			# Fairly arbitary default
pressure_advance_smooth_time: 0.040	# Recommended default
#
# Uncomment the two lines below to enable the option for filament "touch" homing option to nozzle!
extra_endstop_pins: tmc2209_extruder:virtual_endstop
extra_endstop_names: mmu_ext_touch
```

The first TMC definition was previously `[tmc2209 extruder]` and is moved here as `[tmc2209 manual_extruder_stepper extruder]`. The original `[tmc2209 extruder]` in your `printer.cfg` should be deleted or commented out.
The second definion is the elements that define the extruder stepper motor taken from my original `[extruder]` definition. These parameters include only: `step_pin`, `dir_pin`, `enable_pin`, `rotation_distance`, `gear_ratio`, `microsteps`, `full_steps_per_rotation`, `pressure_advance` and `pressure_advance_smooth_time`.  Leave all the other parameters (things like pid controls, sensor type, etc) in the original `[extruder]` definition in your `printer.cfg` file. Make sense? The stepper definition moved here, the rest of the toolhead extruder definition left where it was originally.

> [!WARNING]  
> If you see a Klipper error message like `mux command SET_PRESSURE_ADVANCE EXTRUDER None already registered` it almost certainly means that you have not commented out or disabled your extruder stepper in the original `[extruder]` section of `printer.cfg`

**Still not clear?**  Here is a diagram that shows the change to my config (note that I was already using aliases for pin names so you might also be moving your direct pin names into the aliases file `mmu.py`).  Don't worry about adding DIAG and endstops now because you can do that later only if you want to experiment with extruder homing:
<img src="/doc/extruder_config_move.png" width="980" alt="extruder config move">

Endstop setup and options can be [found here](#---endstops-and-mmu-movement)

If all other pin's and setup look correct *RESTART KLIPPER* and proceed to step 2.

<br>

### c) Variables file (mmu_vars.cfg):
This is the file where Happy Hare stores all calibration settings and state. It is pointed to by this section at the top of `mmu_software.cfg`:
```yml
[save_variables]
filename: /home/pi/printer_data/config/mmu/mmu_vars.cfg
```

Klipper can only have one `save_variables` file and so if you are already using one you can simply comment out the lines above and Happy Hare will append into your existing "variables" file.

<br>

## Step 2. Check motor movement and direction
TODO .. help on basic motor movement and direction / changes

<br>

## Step 3. Check endstops & optional sensors
TODO .. help on how to validate endtops and reverse polarity

<br>

## Step 4. Check Encoder (if fitted)
TODO .. help on validating that it is registering movement

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Endstops and MMU Movement
Happy Hare offers some sophisticated stepper synching and homing options which add additional parameters to the klipper stepper definition.

### Multiple Endstops
In a nutshell, all steppers (MMU and extruder) defined in Happy Hare can have muliple endstops defined. Firstly the default endstop can be defined in the normal way by setting `endstop_pin`.  This would then become the default endstop and can be referenced in gcode as "default".  However it is better to give the endstop a vanity name by adding a new `endstop_name` parameter. This is the name that will appear when listing endstops (e.g. in the Mainsail interface or with `QUERY_ENDSTOPS`). Happy Hare uses a naming convention of `mmu_` so these are anticipated names: `mmu_gear_touch`, `mmu_ext_touch`, `mmu_sel_home`, `mmu_sel_touch`, `mmu_toolhead`.

These would represent touch endstop on gear, touch on extruder, physical selector home, selector touch endstop and toolhead sensor. In Happy Hare, "touch" refers to stallguard based sensing feedback from the TMC driver (if avaialable).

In addition the default endstop which is only set on the selector in the out-of-the-box configuration you can specify a list of "extra" endstops each with a customized name.  These extra endstops can be switched in and out as needed. E.g.

```yml
extra_endstop_pins: tmc2209_extruder:virtual_endstop, TEST_PIN
extra_endstop_names: mmu_ext_touch, my_test_endstop
```

Defines two (non-default) endstops, the first is a virtual "touch" one leveraging stallguard and the second an example of a test switch.

> [!IMPORTANT]  
> If equipped with a toolhead sensor, endstops for gear stepper and extruder stepper will automatically be created with the name `mmu_toolhead`

Ok, so you can define lots of endstops. Why? and what next... Let's discuss syncing and homing moves first and then bring it all together with an example.

### Stepper syncing
Any stepper defined with `[manual_extruder_stepper]` not only inherits multiple endstops but also can act as both an extruder stepper or a manual stepper. This dual personality allows its motion queue to be synced with other extruder steppers or, but manipulated manually in the same way as a Klipper manual\_stepper can be.

Happy have provides two test moves commands `MMU_TEST_MOVE`, `MMU_TEST_HOMING_MOVE` (and two similar commands designed for embedded gcode use). For example:

> MMU_TEST_MOVE MOVE=100 SPEED=10 MOTOR="gear+extruder"

This will advance both the MMU gear and extruder steppers in sync but +100mm at 10mm/s. If only only `MOTOR` was specified the move would obviously not be synchronized. Note that the difference between "gear+extruder" and "extruder+gear" is which motors position is driving the movement and in the case of a homing move, which endstop.

### Homing moves
Similarly it is possible to specify a homing move:

> MMU_TEST_HOMING_MOVE MOVE=100 SPEED=10 MOTOR="extruder+gear" ENDSTOP=mmu_ext_touch STOP_ON_ENDSTOP=1

This would home the filament using synchronized motors to the nozzle using stallguard! Cool hey?!?

> [!NOTE]  
> Homing moves can also be done in the reverse direction (and by therefore reversing the endstop switch) by specifying `STOP_ON_ENDSTOP=-1`. This should be familiar if you have ever used the Klipper `MANUAL_STEPPER` command.<br>If you are at all curious (and I know you will be after reading this) you can "dump" out the Happy Hare stepper configuration with the command `DUMP_MANUAL_STEPPER STEPPER=gear_stepper | extruder | selector_stepper`. I'll leave it to you to figure out the results.<br>Final note is that the generic `MANUAL_STEPPER` command has additional parameters `ENDSTOP=` and `EXTRUDER=` for specifying endstop or extruder to sync too when managing steppers defined with Happy Hare.

<br>

For quick reference here are the two test MMU move commands:

  | Command | &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Description&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | Parameters |
  | ------- | ----------- | ---------- |
  | `MMU_TEST_MOVE` | Simple test move the MMU gear stepper | `MOVE=..[100]` Length of gear move in mm <br>`SPEED=..` (defaults to speed defined to type of motor/homing combination) Stepper move speed <br>`ACCEL=..` (defaults to min accel defined on steppers employed in move) Motor acceleration <br>`MOTOR=[gear\|extruder\|gear+extruder\|extruder+gear]` (default: gear) The motor or motor combination to employ. gear+extruder commands the gear stepper and links extruder to movement, extruder+gear commands the extruder stepper and links gear to movement |
  | `MMU_TEST_HOMING_MOVE` | Testing homing move of filament using multiple stepper combinations specifying endstop and driection of homing move | `MOVE=..[100]` Length of gear move in mm <br>`SPEED=..` (defaults to speed defined to type of motor/homing combination) Stepper move speed <br>`ACCEL=..` Motor accelaration (defaults to min accel defined on steppers employed in homing move) <br>`MOTOR=[gear\|extruder\|gear+extruder\|extruder+gear]` (default: gear) The motor or motor combination to employ. gear+extruder commands the gear stepper and links extruder to movement, extruder+gear commands the extruder stepper and links gear to movement. This is important for homing because the endstop must be on the commanded stepper <br>`ENDSTOP=..` Symbolic name of endstop to home to as defined in mmu_hardware.cfg. Must be defined on the primary stepper <br>`STOP_ON_ENDSTOP=[1\|-1]` (default 1) The direction of homing move. 1 is in the normal direction with endstop firing, -1 is in the reverse direction waiting for endstop to release. Note that virtual (touch) endstops can only be homed in a forward direction |

### What's the point?
Hopefully you can see some of the coordinated movements that are possible that are highly useful for an MMU setup.  For example, I'm current loading filament with an incredibly fast bowden load using the gear stepper followed by a synchronized homing move of the extruder and gear, homing to the nozzle using `mmu_ext_touch` (stallguard) endstop. It requires zero knowledge of extruder dimensions and no physical switches! It also has lots of uses for custom setups with filmament cutters or other purging mechanisms.

Altough this advanced functionality is already being used internally in Happy Hare, you will need to use the manual gcode commands or customize the loading and unloading gcode sequences to do highly imaginatively things - let me know how you get on.
