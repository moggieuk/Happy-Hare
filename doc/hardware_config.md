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

This makes the minimal include into your printer.cfg easy: `[include mmu/base/*.cfg]`

### a) MCU and Pin Validation (mmu.cfg)
The `mmu.cfg` file is part of the hardware configuration but defines aliases for all of the pins used in `mmu_hardware.cfg`. The benefit of this is that configuration frameworks like [Klippain](https://github.com/Frix-x/klippain) can more easily incorporate. It is also in keeping with an organized modular layout.

<br>

### b) Hardware Configuration (mmu_hardware.cfg):
This can be daunting but the interactive installer will make the process easier for common mcu's designed for a MMU (e.g. ERCF EASY-BRD, Burrows ERB, etc) and perform most of the setup for you.

Assuming you have first run the installer (and perhaps familiar with the early incarnation of Happy Hare) there is one NEW IMPORTANT step that must be performed by hand:  You must move some of your `[extruder]` definition into `mmu_hardware.cfg`. This is best illustrated with my actual configuration (pulled from the top of `mmu_hardware.cfg`):
  
```yml
[mmu_config_setup]

# HOMING CAPABLE EXTRUDER --------------------------------------------------------------------------------------------------
# With Happy Hare, it is important that the extruder stepper definition is moved here to allow for sophisticated homing and syncing
# options.  This definition replaces the stepper definition part of you existing [extruder] definition.

# IMPORTANT: Move the complete stepper driver configuration associated with regular extruder here and comment out the
# original driver config
#
[tmc2209 manual_extruder_stepper extruder]
uart_pin: E_TMCUART
interpolate: true
run_current: 0.55			# LDO 36STH20-1004AHG.  Match to macro below
hold_current: 0.4
sense_resistor: 0.110
stealthchop_threshold: 0		# Spreadcycle (better for extruder)
#
# Uncomment two lines below if you have TMC and want the ability to use filament "touch" homing to nozzle
#diag_pin: E_DIAG			# Set to MCU pin connected to TMC DIAG pin for extruder
#driver_SGTHRS: 100			# 255 is most sensitive value, 0 is least sensitive


# NOTE: The [mmu_config_setup] line earlier in this file will now automatically pull the required [extruder] stepper config
# options here so you now only need to add supplementary ones like endstops!
#
#  If you do decide to define your printer's extruder stepper here instead of in the [extruder] then valid config options are ONLY:
#    step_pin, dir_pin, enable_pin, rotation_distance, gear_ratio, microsteps, full_steps_per_rotation
#    pressure_advance, pressure_advance_smooth_time
#  Leave all other options in your [extruder] config
#
[manual_extruder_stepper extruder]
#
# Uncomment the two lines below to enable the option for filament "touch" homing option to nozzle!
#extra_endstop_pins: tmc2209_extruder:virtual_endstop
#extra_endstop_names: mmu_ext_touch
```

The first TMC definition was previously `[tmc2209 extruder]` and is moved here as `[tmc2209 manual_extruder_stepper extruder]`. The original `[tmc2209 extruder]` in `printer.cfg` most be deleted or commented out. Note that tmc2209 is most common but obviously adjust the driver to match your particular driver chip.

The second definion is the elements that define the extruder stepper. The standard list of `step_pin`, `dir_pin`, `enable_pin`, `rotation_distance`, `gear_ratio`, `microsteps`, `full_steps_per_rotation`, `pressure_advance` and `pressure_advance_smooth_time` will all be AUTOMATICALLY added during bootup and nullified on the original extruder.

**Still not clear?**  Here is a diagram that shows the change to my config (note that I was already using aliases for pin names so you might also be moving your direct pin names into the aliases file `mmu.py`).  Don't worry about adding DIAG and endstops now because you can do that later only if you want to experiment with extruder homing:
<img src="/doc/extruder_config_move.png" width="980" alt="extruder config move">

Endstop setup and options can be [found here](#---endstops-and-mmu-movement)

<br>

### c) Variables file (mmu_vars.cfg):
This is the file where Happy Hare stores all calibration settings and state. It is pointed to by this section at the top of `mmu_software.cfg`:
```
[save_variables]
filename: /home/pi/printer_data/config/mmu/mmu_vars.cfg
```

Klipper can only have one `save_variables` file and so if you are already using one you can simply comment out the lines above and Happy Hare will append into your existing "variables" file.

If all other pin's and setup look correct *RESTART KLIPPER* and proceed to step 2.

<br>

## Step 2. Check motor movement and direction
Once pins are correct it is important to verify direction.  It is not possible for the installer to ensure this because it depends on the actual stepper wiring.  The recommended procedure is:
```yml
MMU_MOTORS_OFF
  # move selector to the center of travel
MANUAL_STEPPER STEPPER=selector_stepper SET_POSITION=0 MOVE=-10
  # verify that the selector moves to the left towards the home position
MANUAL_STEPPER STEPPER=selector_stepper SET_POSITION=0 MOVE=10
  # verify that the selector moves to the right away from the home position
```
If the selector doesn't move or moves the wrong way open up `mmu_hardware.cfg`, find the section `[manual_mh_stepper selector_stepper]`:
If selector doesn't move it is likley that the pin configuration for `step_pin` and/or `enable_pin` are incorrect. Verify the pin names and prefix the pin with `!` to invert the signal. E.g.
```yml
enable_pin: !mmu:MMU_SEL_ENABLE
  # or
enable_pin: mmu:MMU_SEL_ENABLE
```
If the selector moves the wrong way the `dir_pin` is inverted. Either add or remove the `!` prefix:
```yml
dir_pin: !mmu:MMU_SEL_DIR
  # or
dir_pin: mmu:MMU_SEL_DIR
```

Now repeat the exercise with the gear stepper:
```yml
MMU_MOTORS_OFF
  # remove any filament from your MMU
MANUAL_STEPPER STEPPER=gear_stepper SET_POSITION=0 MOVE=-10
  # verify that the gear stepper would pull filament away from the extruder
MANUAL_STEPPER STEPPER=gear_stepper SET_POSITION=0 MOVE=10
  # verify that the gear stepper is push filament towards the extruder
```
If the gear stepper doesn't move or moves the wrong way open up `mmu_hardware.cfg`, find the section `[manual_extruder_stepper gear_stepper]`:
If gear doesn't move it is likley that the pin configuration for `step_pin` and/or `enable_pin` are incorrect. Verify the pin names and prefix the pin with `!` to invert the signal. E.g.
```yml
enable_pin: !mmu:MMU_GEAR_ENABLE
  # or
enable_pin: mmu:MMU_GEAR_ENABLE
```
If the gear moves the wrong way the `dir_pin` is inverted. Either add or remove the `!` prefix:
```yml
dir_pin: !mmu:MMU_GEAR_DIR
  # or
dir_pin: mmu:MMU_GEAR_DIR
```

<br>

## Step 3. Check endstops & optional sensors
Next verify that the necessary endstops are working and the polarity is correct. The recommended procedure is:
```yml
MMU_MOTORS_OFF
  # remove filament from ERCF and extruder, move selector to center of travel
QUERY_ENDSTOPS
  # or use the visual query in Mainsail or Fluuid
```
Validate that you can see:
```yml
mmu_sel_home:open (Essential)
mmu_toolhead:open (Optional if you have a toolhead sensor)
```
Then manually press and hold the selector microswitch and rerun `QUERY_ENDSTOPS`
Validate that you can see `mmu_sel_home:TRIGGERED` in the list
If you have toolhead sensor, feed filament into the extruder past the switch and rerun `QUERY_ENDSTOPS`
Validate that you can see `mmu_toolhead:TRIGGERED` in the list

If either of these don't change state then the pin assigned to the endstop is incorrect.  If the state is inverted (i.e. enstop transitions to `open` when pressed) the add/remove the `!` on the respective endstop pin either in the `[manual_mh_stepper selector_stepper]` block for selector endstop or in `[filament_switch_sensor toolhead_sensor]` block for toolhead sensor.

Other endstops like "touch" operation are advanced and not cover by this inital setup.

<br>

## Step 4. Check Encoder (if fitted)
Ok, last sanity check.  If you have an encoder based design like the ERCF, need to check that it is wired correctly. Run the command `MMU_ENCODER` and note the position displayed.
```yml
MMU_ENCODER
Encoder position: 23.4
```
Insert some filament (from either side) and pull backwards and forwards.  You should see the LED flashing.  Rerun `MMU_ENCODER` and validate the position displayed has increased (note that the encoder is not direction aware so it will always increase in reading)

If the encoder postion does not change, validate the `encoder_pin` is correct. It shouldn't matter if it has a `!` (inverted) or not, but it might require a `^` (pull up resister) to function.
```yml
[mmu_encoder mmu_encoder]
encoder_pin: ^mmu:MMU_ENCODER	
```

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
