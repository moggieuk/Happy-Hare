# Happy Hare - Command Reference

Firstly you can get a quick reminder of commands using the `MMU_HELP` command from the console:

  > MMU_HELP

```
    Happy Hare MMU commands: (use MMU_HELP MACROS=1 TESTING=1 for full command set)
    MMU - Enable/Disable functionality and reset state
    MMU_CHANGE_TOOL - Perform a tool swap
    MMU_CHECK_GATES - Automatically inspects gate(s), parks filament and marks availability
    MMU_STATS - Dump or reset the MMU statistics
    MMU_EJECT - Eject filament and park it in the MMU or optionally unloads just the extruder (EXTRUDER_ONLY=1)
    MMU_ENCODER - Display encoder position or temporarily enable/disable detection logic in encoder
    MMU_ENDLESS_SPOOL - Redefine the EndlessSpool groups
    MMU_FORM_TIP_STANDALONE - Convenience macro for calling standalone tip forming (defined in mmu_software.cfg)
    MMU_HELP - Display the complete set of MMU commands and function
    MMU_HOME - Home the MMU selector
    MMU_LOAD - Loads filament on current tool/gate or optionally loads just the extruder for bypass or recovery usage (EXTUDER_ONLY=1)
    MMU_MOTORS_OFF - Turn off both MMU motors
    MMU_PAUSE - Pause the current print and lock the MMU operations
    MMU_PRELOAD - Preloads filament at specified or current gate
    MMU_RECOVER - Recover the filament location and set MMU state after manual intervention/movement
    MMU_REMAP_TTG - Remap a tool to a specific gate and set gate availability
    MMU_RESET - Forget persisted state and re-initialize defaults
    MMU_SELECT - Select the specified logical tool (following TTG map) or physical gate
    MMU_SELECT_BYPASS - Select the filament bypass
    MMU_SERVO - Move MMU servo to position specified position or angle
    MMU_SET_GATE_MAP - Define the type and color of filaments on each gate
    MMU_STATUS - Complete dump of current MMU state and important configuration
    MMU_SYNC_GEAR_MOTOR - Sync the MMU gear motor to the extruder motor
    MMU_UNLOCK - Unlock MMU operations after an error condition
```


  ## ![#f03c15](https://placehold.co/15x15/f03c15/f03c15.png) ![#c5f015](https://placehold.co/15x15/c5f015/c5f015.png) ![#1589F0](https://placehold.co/15x15/1589F0/1589F0.png) Basic MMU functionality

  | Command | Description | Parameters |
  | ------- | ----------- | ---------- |
  | `MMU` | Enable and reset state or disable the MMU. Useful to completely turn off the MMU functionality rather then uninstalling it. Note that persisted state will be reset when re-enabling | `ENABLE=[0\|1]` |
  | `MMU_HOME` | Home the MMU selector and optionally selects gate associated with the specified tool | `TOOL=[0..n]` After homing, select gate associated with this tool <br>`FORCE_UNLOAD=[0\|1]` Optional. If specified will override default intelligent filament unload behavior prior to homing |
  | `MMU_SELECT` | Selects the logical tool or physical gate. If tool is sepficed the gate associated with the specified tool (TTG map) will be selected | `TOOL=[0..n]` The tool to be selected (will actually select the gate currently mapped to the tool with TTG) <br>`GATE=[0..n]` The gate to be selected (ignores TTG map) |
  | `MMU_SELECT_BYPASS` | Select the bypass selector position if configured | None |
  | `MMU_CHANGE_TOOL` | Perform a tool swap (generally called from 'Tx' macros). Use `STANDALONE=1` option in your print_start macro to saftely load the initial tool | `TOOL=[0..n]` <br>`STANDALONE=[0\|1]` Optional to force standalone logic (tip forming)<br> `QUIET=[0\|1]` Optional to always suppress swap statistics |
  | `MMU_LOAD` | Loads filament in currently selected tool/gate to extruder. Optionally performs just the extruder load part of the sequence - designed for bypass loading or non MMU use | `EXTRUDER_ONLY=[0\|1]` To force just the extruder loading (automatic if bypass selected) |
  | `MMU_EJECT` | Eject filament and park it in the MMU gate or does the extruder unloading part of the unload sequence if in bypass | `EXTRUDER_ONLY=[0\|1]` To force just the extruder unloading (automatic if bypass selected) |
  | `MMU_PRELOAD` | Helper for filament loading. Feed filament into gate, MMU will catch it and correctly position at the specified gate | `GATE=[0..n]` The specific gate to preload. If omitted the currently selected gate can be loaded |
  | `MMU_UNLOCK` | Unlock MMU operations after a pause caused by error condition | None |
  | `MMU_PAUSE` | Pause the current print and lock the MMU operations | `FORCE_IN_PRINT=[0\|1]` This option forces the handling of pause as if it occurred in print and is useful for testing |
  | `MMU_RECOVER` | Recover filament position and optionally reset MMU state. Useful to call prior to RESUME if you intervene/manipulate filament by hand | `TOOL=[0..n]\|-2` Optionally force set the currently selected tool (-2 = bypass). Use caution! <br>`GATE=[0..n]` Optionally force set the currently selected gate if TTG mapping is being leveraged otherwise it will get the gate associated with current tool. Use caution! <br>`LOADED=[0\|1]` Optionally specify if the filamanet is fully loaded or fully unloaded. Use caution! If not specified, MMU will try to discover filament position <br>`STRICT=[0\|1]` If automatically detecting impose stricter testing for filament position (temporarily sets 'strict_filament_recovery' parameter) |
  | `MMU_ENCODER` | Displays the current value of the MMU encoder or explicitly enable or disable the encoder. Note that the encoder state is set automatically so this will only be sticky until next tool change | `ENABLE=[0\|1]` |
  | `MMU_HELP` | Generate reminder list of command set | `TESTING=[0\|1]` Also list the testing commands <br>`MACROS=[0\|1]` Also list the callback backros |
  <br>

  ### Filament specification, Tool to Gate map and Endless spool commands
  | Command | Description | Parameters |
  | ------- | ----------- | ---------- |
  | `MMU_CHECK_GATES` | Inspect the gate(s) and mark availability | `GATE=[0..n]` The specific gate to check <br>`TOOL=[0..n]` The specific too to check (same as gate if no TTG mapping in place) <br>`TOOLS={csv}` The list of tools to check. Typically used in print start macro to validate all necessary tools <br>If all parameters are omitted all gates will be checked (the default) <br>`QUIET=[0\|1]` Optional. Supresses dump of gate status at end of checking procedure |
  | `MMU_SET_GATE_MAP` | Optionally configure the filament type, color and availabilty. Used in colored UI's and available via printer variables in your print_start macro | `RESET=[0\|1]` If specified the 'gate_materials, 'gate_colors' and 'gate_status' will be reset to that defined in mmu_parameters.cfg <br>`DISPLAY=[0\|1]` To simply display the current gate map <br>The following must be specified together to create a complete entry in the gate map: <br>`GATE=[0..n]` Gate numer <br>`MATERIAL=..` The material type. Short, no spaces. e.g. "PLA+" <br>`COLOR=..` The color of the filament. Can be a string representing one of the [w3c color names](https://www.w3.org/TR/css-color-4/#named-colors) e.g. "violet" or a color string in the hexadeciaml format RRGGBB e.g. "ff0000" for red. NO space or # symbols. Empty string for no color <br>`AVAILABLE=[0\|1]` Optionally marks gate as empty or available <br>`QUIET=[0\|1]` Optional. Supresses dump of current gate map to log file |
  | `MMU_REMAP_TTG` | Reconfiguration of the Tool - to - Gate (TTG) map.  Can also set gates as empty! | `RESET=[0\|1]` If specified the Tool -> Gate mapping will be reset to that defined in mmu_parameters.cfg <br>`TOOL=[0..n]` Tool to set in TTG map <br>`GATE=[0..n]` Maps specified tool to this gate (multiple tools can point to same gate) <br>`AVAILABLE=[0\|1]`  Marks gate as available or empty <br>`QUIET=[0\|1]` Optional. Supresses dump of current TTG map to log file <br>`MAP={csv}` List of gates, one for each tool to specify the entire TTG map for bulk updates |
  | `MMU_ENDLESS_SPOOL` | Modify the defined EndlessSpool groups at runtime | `RESET=[0\|1]` If specified the EndlessSpool groups will be reset to that defined in mmu_parameters.cfg <br>`GROUPS={csv of groups}` The same format as the default groups defined in mmu_parameters.cfg. Must be the same length as the number of MMU gates | `QUIET=[0\|1]` Optional. Supresses dump of current TTG and endless spool map to log file <br>`ENABLE=[0\|1]` Optional. Force the enabling or disabling of endless spool at runtime (not persisted) |
  <br>

  ### Status, Logging and Persisted state
  | Command | Description | Parameters |
  | ------- | ----------- | ---------- |
  | `MMU_RESET` | Reset the MMU persisted state back to defaults | `CONFIRM=[0\|1]` Must be sepcifed for affirmative action of this dangerous command |
  | `MMU_STATS` | Dump (and optionally reset) the MMU statistics. Note that gate statistics are sent to debug level - usually the logfile) | `RESET=[0\|1]` If 1 the stored statistics will be reset |
  | `MMU_STATUS` | Report on MMU state, capabilities and Tool-to-Gate map | `DETAIL=[0\|1]` Whether to show a more detailed view including EndlessSpool groups and full Tool-To-Gate mapping <br>`SHOWCONFIG=[0\|1]` (default 0) Whether or not to describe the machine configuration in status message |
  <br>
  
  ### Servo and motor control
  | Command | Description | Parameters |
  | ------- | ----------- | ---------- |
  | `MMU_SERVO` | Set the servo to specified postion or a sepcific angle for testing.  | `POS=[up\|down\|move]` Move servo to predetermined position <br>`ANGLE=..` Move servo to specified angle |
  | `MMU_MOTORS_OFF` | Turn off both MMU motors | None |
  | `MMU_SYNC_GEAR_MOTOR` | Explicitly override the synchronization of extruder and gear motors. Note that synchronization is set automatically so this will only be sticky until the next tool change | `SYNC=[0\|1]` Turn gear/extruder synchronization on/off (default 1) <br>`SERVO=[0\|1]` If 1 (the default) servo will engage if SYNC=1 or disengage if SYNC=0 otherwise servo position will not change |
  
  <br>

  ## ![#f03c15](https://placehold.co/15x15/f03c15/f03c15.png) ![#c5f015](https://placehold.co/15x15/c5f015/c5f015.png) ![#1589F0](https://placehold.co/15x15/1589F0/1589F0.png) Calibration

```
    MMU_CALIBRATE_BOWDEN - Calibration of reference bowden length for gate #0
    MMU_CALIBRATE_ENCODER - Calibration routine for the MMU encoder
    MMU_CALIBRATE_GATES - Optional calibration of individual MMU gate
    MMU_CALIBRATE_GEAR - Calibration routine for gear stepper rotational distance
    MMU_CALIBRATE_SELECTOR - Calibration of the selector positions or postion of specified gate
```
  
  | Command | Description | Parameters |
  | ------- | ----------- | ---------- |
  | `MMU_CALIBRATE_GEAR` | Calibration rourine for the the gear stepper rotational distance | `LENGTH=..` length to test over (default 100mm) <br>`MEASURED=..` User measured distance <br>`SAVE=[0\|1]` (default 1) Whether to save the result |
  | `MMU_CALIBRATE_ENCODER` | Calibration routine for MMU encoder | `LENGTH=..` Distance (mm) to measure over. Longer is better, defaults to 400mm <br>`REPEATS=..` Number of times to average over <br>`SPEED=..` Speed of gear motor move. Defaults to long move speed <br>`ACCEL=..` Accel of gear motor move. Defaults to motor setting in ercf_hardware.cfg <br>`MINSPEED=..` & `MAXSPEED=..` If specified the speed is increased over each iteration between these speeds (only for experimentation) <br>`SAVE=[0\|1]` (default 1)  Whether to save the result |
  | `MMU_CALIBRATE_SELECTOR` | Calibration of the selector gate positions. By default will automatically calibrate every gate.  ERCF v1.1 users must specify the bypass block position if fitted.  If GATE to BYPASS option is sepcifed this will update the calibrate for a single gate | `GATE=[0..n]` The individual gate position to calibrate <br>`BYPASS=[0\|1]` Calibrate the bypass position <br>`BYPASS_BLOCK=..` Optional (v1.1 only). Which bearing block contains the bypass where the first one is numbered 1 <br>`SAVE=[0\|1]` (default 1) Whether to save the result |
  | `MMU_CALIBRATE_BOWDEN` | Measure the calibration length of the bowden tube used for fast load movement. This will be performed on gate #0 | `BOWDEN_LENGTH=..` The approximate length of the bowden tube but NOT longer than the real measurement. 50mm less that real is a good starting point <br>`HOMING_MAX=..` (default 100) The distance after the sepcified BOWDEN_LENGTH to search of the extruder entrance <br>`REPEATS=..` (default 3) Number of times to average measurement over <br>`SAVE=[0\|1]` (default 1)  Whether to save the result |
  | `MMU_CALIBRATE_GATES` | Optional calibration for loading of a sepcifed gate or all gates. This is calculated as a ratio of gate #0 and thus this is usually the last calibration step | `GATE=[0..n]` The individual gate position to calibrate <br>`ALL[0\|1]` Calibrate all gates 1..n sequentially (filament must be available in each gate) <br>`LENGTH=..` Distance (mm) to measure over. Longer is better, defaults to 400mm <br>`REPEATS=..` Number of times to average over <br>`SAVE=[0\|1]` (default 1)  Whether to save the result |

<br>

  ## ![#f03c15](https://placehold.co/15x15/f03c15/f03c15.png) ![#c5f015](https://placehold.co/15x15/c5f015/c5f015.png) ![#1589F0](https://placehold.co/15x15/1589F0/1589F0.png) Testing

```
    MMU_SOAKTEST_LOAD_SEQUENCE - Soak test tool load/unload sequence
    MMU_SOAKTEST_SELECTOR - Soak test of selector movement
    MMU_TEST_BUZZ_MOTOR - Simple buzz the selected motor (default gear) for setup testing
    MMU_TEST_CONFIG - Runtime adjustment of MMU configuration for testing or in-print tweaking purposes
    MMU_TEST_ENCODER_RUNOUT - Convenience macro to spoof a filament runout condition (defined in mmu_software.cfg)
    MMU_TEST_GRIP - Test the MMU grip for a Tool
    MMU_TEST_HOMING_MOVE - Test filament homing move to help debug setup / options
    MMU_TEST_LOAD - For quick testing filament loading from gate to the extruder
    MMU_TEST_MOVE - Test filament move to help debug setup / options
    MMU_TEST_TRACKING - Test the tracking of gear feed and encoder sensing
```
    
  | Command | Description | Parameters |
  | ------- | ----------- | ---------- |
  | `MMU_SOAKTEST_SELECTOR` | Reliability testing to put the selector movement under stress to test for failures. Randomly selects gates and occasionally re-homes | `LOOP=..[100]` Number of times to repeat the test <br>`SERVO=[0\|1]` Whether to include the servo down movement in the test |
  | `MMU_SOAKTEST_LOAD_SEQUENCE` | Soak testing of load sequence. Great for testing reliability and repeatability| `LOOP=..[10]` Number of times to loop while testing <br>`RANDOM=[0\|1]` Whether to randomize tool selection <br>`FULL=[0\|1]` Whether to perform full load to nozzle or short load just past encoder |
  | `MMU_TEST_BUZZ_MOTOR` | Buzz the sepcified MMU motor. If the gear motor is buzzed it will also report if filament is detected | `MOTOR=[gear\|selector\|servo]` |
  | `MMU_TEST_GRIP` | Test the MMU grip of the currently selected tool by gripping filament but relaxing the gear motor so you can check for good contact | None |
  | `MMU_TEST_LOAD` | Test loading filament from park position in the gate. (MMU_EJECT will unload) | `LENGTH=..[100]` Test load the specified length of filament into selected tool <br>`FULL=[0\|1]` If set to one a full bowden move will occur and filament will home to extruder |
  | `MMU_TEST_TRACKING | Simple visual test to see how encoder tracks with gear motor | `DIRECTION=[-1\|1]` Direction to perform the test (default load direction) <br>`STEP=[0.5 .. 20]` Size of individual steps (default 1mm) <br>`SENSITIVITY=..` (defaults to expected encoder resolution) Sets the scaling for the +/- mismatch visualization |
  | `MMU_TEST_MOVE` | Simple test move the MMU gear stepper | `MOVE=..[100]` Length of gear move in mm <br>`SPEED=..` (defaults to speed defined to type of motor/homing combination) Stepper move speed <br>`ACCEL=..` (defaults to min accel defined on steppers employed in move) Motor acceleration <br>`MOTOR=[gear\|extruder\|gear+extruder\|extruder+gear]` (default: gear) The motor or motor combination to employ. gear+extruder commands the gear stepper and links extruder to movement, extruder+gear commands the extruder stepper and links gear to movement |
  | `MMU_TEST_HOMING_MOVE` | Testing homing move of filament using multiple stepper combinations specifying endstop and driection of homing move | `MOVE=..[100]` Length of gear move in mm <br>`SPEED=..` (defaults to speed defined to type of motor/homing combination) Stepper move speed <br>`ACCEL=..` Motor accelaration (defaults to min accel defined on steppers employed in homing move) <br>`MOTOR=[gear\|extruder\|gear+extruder\|extruder+gear]` (default: gear) The motor or motor combination to employ. gear+extruder commands the gear stepper and links extruder to movement, extruder+gear commands the extruder stepper and links gear to movement. This is important for homing because the endstop must be on the commanded stepper <br>`ENDSTOP=..` Symbolic name of endstop to home to as defined in mmu_hardware.cfg. Must be defined on the primary stepper <br>`STOP_ON_ENDSTOP=[1\|-1]` (default 1) The direction of homing move. 1 is in the normal direction with endstop firing, -1 is in the reverse direction waiting for endstop to release. Note that virtual (touch) endstops can only be homed in a forward direction |
  | `MMU_TEST_CONFIG` | Dump / Change essential load/unload config options at runtime | Many. Best to run MMU_TEST_CONFIG without options to report all parameters than can be specified |
  | `MMU_TEST_ENCODER_RUNOUT` | Filament runout handler that will also implement EndlessSpool if enabled | `FORCE_RUNOUT=1` is useful for testing to validate your _MMU_ENDLESS_SPOOL\*\* macros |

<br>

## ![#f03c15](https://placehold.co/15x15/f03c15/f03c15.png) ![#c5f015](https://placehold.co/15x15/c5f015/c5f015.png) ![#1589F0](https://placehold.co/15x15/1589F0/1589F0.png) User defined/configurable macros (defined in mmm_software.cfg) 
  | Command | Description |
  | ------- | ----------- |
  | `_MMU_ENDLESS_SPOOL_PRE_UNLOAD` | Called prior to unloading the remains of the current filament |
  | `_MMU_ENDLESS_SPOOL_POST_LOAD` | Called subsequent to loading filament in the new gate in the sequence |
  | `_MMU_FORM_TIP_STANDALONE` | Called to create tip on filament when not in print (and under the control of the slicer). You tune this macro by modifying the defaults to the parameters |
  | `_MMU_ACTION_CHANGED` | Callback that is called everytime the `printer.ercf.action` is updated. Great for contolling LED lights, etc |

*Working reference PAUSE / RESUME / CANCEL_PRINT macros are defined in `client_macros.cfg` and can be used/modified if you don't already have your own*

<br>
  
    (\_/)
    ( *,*)
    (")_(") MMU Ready
  
