# Happy Hare - Command Reference

Firstly you can get a quick reminder of commands using the `MMU_HELP` command from the console:

  > MMU_HELP

```yml
Happy Hare MMU commands: (use MMU_HELP MACROS=1 TESTING=1 STEPS=1 for full command set)
    MMU : Enable/Disable functionality and reset state
    MMU_CHANGE_TOOL : Perform a tool swap (called from Tx command)
    MMU_CHECK_GATE : Automatically inspects gate(s), parks filament and marks availability
    MMU_EJECT : aka MMU_UNLOAD Eject filament and park it in the MMU or optionally unloads just the extruder (EXTRUDER_ONLY=1)
    MMU_ENCODER : Display encoder position and stats or enable/disable runout detection logic in encoder
    MMU_ENDLESS_SPOOL : Diplay or Manage EndlessSpool functionality and groups
    MMU_GATE_MAP : Display or define the type and color of filaments on each gate
    MMU_HELP : Display the complete set of MMU commands and function
    MMU_HOME : Home the MMU selector
    MMU_LED : Manage mode of operation of optional MMU LED's
    MMU_LOAD : Loads filament on current tool/gate or optionally loads just the extruder for bypass or recovery usage (EXTRUDER_ONLY=1)
    MMU_MOTORS_OFF : Turn off both MMU motors
    MMU_PAUSE : Pause the current print and lock the MMU operations
    MMU_PRELOAD : Preloads filament at specified or current gate
    MMU_RECOVER : Recover the filament location and set MMU state after manual intervention/movement
    MMU_RESET : Forget persisted state and re-initialize defaults
    MMU_SELECT : Select the specified logical tool (following TTG map) or physical gate
    MMU_SELECT_BYPASS : Select the filament bypass
    MMU_SENSORS : Query state of sensors fitted to mmu
    MMU_SERVO : Move MMU servo to position specified position or angle
    MMU_SLICER_TOOL_MAP : Display or define the tools used in print as specified by slicer
    MMU_STATS : Dump and optionally reset the MMU statistics
    MMU_STATUS : Complete dump of current MMU state and important configuration
    MMU_SYNC_GEAR_MOTOR : Sync the MMU gear motor to the extruder stepper
    MMU_TOOL_OVERRIDES : Displays, sets or clears tool speed and extrusion factors (M220 & M221)
    MMU_TTG_MAP : aka MMU_REMAP_TTG Display or remap a tool to a specific gate and set gate availability
    MMU_UNLOCK : Wakeup the MMU prior to resume to restore temperatures and timeouts
```


  ## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Basic MMU functionality

  | Command | Description | &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Parameters&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; |
  | ------- | ----------- | ---------- |
  | `MMU` | Enable and reset state or disable the MMU. Useful to completely turn off the MMU functionality rather then uninstalling it. Note that persisted state will be reset when re-enabling | `ENABLE=[0\|1]` |
  | `MMU_HOME` | Home the MMU selector and optionally selects gate associated with the specified tool | `TOOL=[0..n]` After homing, select gate associated with this tool <br>`FORCE_UNLOAD=[0\|1]` Optional. If specified will override default intelligent filament unload behavior prior to homing |
  | `MMU_SELECT` | Selects the logical tool or physical gate. If tool is sepficed the gate associated with the specified tool (TTG map) will be selected | `TOOL=[0..n]` The tool to be selected (will actually select the gate currently mapped to the tool with TTG) <br>`GATE=[0..n]` The gate to be selected (ignores TTG map) <br>`BYPASS=1` Selects the bypass selector position if configured (same as MMU_SELECT_BYPASS) |
  | `MMU_SELECT_BYPASS` | Select the bypass selector position if configured | None |
  | `MMU_CHANGE_TOOL` | Perform a tool swap (generally called from 'Tx' macros). Use `STANDALONE=1` option in your print_start macro to saftely load the initial tool | `TOOL=[0..n]` <br>`STANDALONE=[0\|1]` Optional to force standalone logic (tip forming)<br> `QUIET=[0\|1]` Optional to always suppress swap statistics |
  | `MMU_LOAD` | Loads filament in currently selected tool/gate to extruder. Optionally performs just the extruder load part of the sequence - designed for bypass loading or non MMU use | `EXTRUDER_ONLY=[0\|1]` To force just the extruder loading (automatic if bypass selected) |
  | `MMU_LED` | Quick way to try/test modes of operation of optional MMU LEDs  | `ENABLE=[0\|1]` Whether LED's are operational or not <br> `EFFECT=[off\|gate_status\|filament_color\|slicer_color]` Selects the default effect for gate LEDs when no action is taking place <br> `EXIT_EFFECT=[off\|filament_color\|slicer_color]` Selects the default effect for exit LED when no action is taking place |
  | `MMU_EJECT` | `MMU_UNLOAD` | Eject filament and park it in the MMU gate or does the extruder unloading part of the unload sequence if in bypass | `EXTRUDER_ONLY=[0\|1]` To force just the extruder unloading (automatic if bypass selected) <br>`SKIP_TIP=[0\|1]` if set the tip forming/cutting macro will be skipped |
  | `MMU_PRELOAD` | Helper for filament loading. Feed filament into gate, MMU will catch it and correctly position at the specified gate | `GATE=[0..n]` The specific gate to preload. If omitted the currently selected gate can be loaded |
  | `MMU_PAUSE` | Pause the current print and lock the MMU operations. (`MMU_UNLOCK + RESUME` or just `RESUME` to continue print) | `FORCE_IN_PRINT=[0\|1]` This option forces the handling of pause as if it occurred in print and is useful for testing. Calls `PAUSE` by default or your `pause_macro` if set <br>`MSG=<message>` Supply message to be displayed. Useful when used in macros |
  | `MMU_RECOVER` | Recover filament position and optionally reset MMU state. Useful to call prior to RESUME if you intervene/manipulate filament by hand | `TOOL=[0..n]\|-2` Optionally force set the currently selected tool (-2 = bypass). Use caution! <br>`GATE=[0..n]` Optionally force set the currently selected gate if TTG mapping is being leveraged otherwise it will get the gate associated with current tool. Use caution! <br>`LOADED=[0\|1]` Optionally specify if the filamanet is fully loaded or fully unloaded. Use caution! If not specified, MMU will try to discover filament position <br>`STRICT=[0\|1]` If automatically detecting impose stricter testing for filament position (temporarily sets 'strict_filament_recovery' parameter) |
  | `MMU_ENCODER` | Displays the current value of the MMU encoder or explicitly enable or disable the encoder. Note that the encoder state is set automatically so this will only be sticky until next tool change | `ENABLE=[0\|1]` 0=Disable, 1=Enable <br>`VALUE=..` Set the current distance |
  | `MMU_FORM_TIP` | see `MMU_TEST_FORM_TIP` | |
  | `MMU_TOOL_OVERRIDES` | Displays, sets or clears tool speed and extrusion factors (M220 & M221) | `TOOL=[0..n]` Specify tool to set <br> `M220=[0-200]` Speed (feedrate) multiplier percentage <br> `M221=[0-200]` Extrusion multiplier percentage <br> `RESET=1` Reset specified override for specified tool to default 100%. Note that omitting `TOOL=` will reset all tools |
  | `MMU_UNLOCK` | Wakeup the MMU prior to RESUME to restore temperatures and timeouts | None |
  | `MMU_HELP` | Generate reminder list of command set | `TESTING=[0\|1]` Also list the testing commands <br>`MACROS=[0\|1]` Also list the callback backros |
  <br>

  ### Filament specification, Tool to Gate map and Endless spool commands
  | Command | Description | &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Parameters&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp |
  | ------- | ----------- | ---------- |
  | `MMU_CHECK_GATE` | Inspect the gate(s) and mark availability | `GATE=[0..n]` The specific gate to check <br>`TOOL=[0..n]` The specific too to check (same as gate if no TTG mapping in place) <br>`TOOLS={csv}` The list of tools to check. Typically used in print start macro to validate all necessary tools <br>`GATES={csv}` The lis of gates to check. <br>If all parameters are omitted all gates will be checked (the default) <br>`QUIET=[0\|1]` Optional. Supresses dump of gate status at end of checking procedure |
  | `MMU_GATE_MAP` | Without parameters this will display the current gate map. Optionally configure the filament type, color and availabilty. Used in colored UI's and available via printer variables in your print_start macro | `RESET=1` If specified the 'gate_materials, 'gate_colors' and 'gate_status' will be reset to that defined in mmu_parameters.cfg <br>The following must be specified together to create a complete entry in the gate map: <br>`GATE=[0..n]` Gate number <br>`GATES={csv}` The list of gates to set. Can be used as an alternative to a single `GATE=..` <br>`MATERIAL=..` The material type. Short, no spaces. e.g. "PLA+" <br>`COLOR=..` The color of the filament. Can be a string representing one of the [w3c color names](https://www.w3.org/TR/css-color-4/#named-colors) e.g. "violet" or a color string in the hexadeciaml format RRGGBB e.g. "ff0000" for red. NO space or # symbols. Empty string for no color <br>`AVAILABLE=[0\|1\|2]` Optionally marks gate as empty (0) or available from spool (1) or available from buffer (2) <br>`SPEED=[10..150]` Optional and controls the speed and accelaration factor (percent) used in loading and unloading moves. This is particularly useful when special handling is required for TPU material <br>`SPOOLID=..` The SpoolMan SpoolID (integer) if SpoolMan support is enabled <br>`QUIET=[0\|1]` Optional. Supresses dump of current gate map to log file <br> `REFRESH=1` Will refresh data from Spoolman if configured for all filaments <br> `NEXT_SPOOLID=..` Will auto assign specified spool_id to the next gate preloaded either specifically or registered by pre-gate sensor. Valid for `pending_spool_id_timeout` seconds (useful for RFID reader) |
  | `MMU_TTG_MAP` | Reconfiguration of the Tool - to - Gate (TTG) map.  Can also set gates as empty! | `RESET=1` If specified the Tool -> Gate mapping will be reset to that defined in mmu_parameters.cfg <br>`TOOL=[0..n]` Tool to set in TTG map <br>`GATE=[0..n]` Maps specified tool to this gate (multiple tools can point to same gate) <br>`AVAILABLE=[0\|1]`  Marks gate as available or empty <br>`QUIET=[0\|1]` Optional. Supresses dump of current TTG map to log file <br>`MAP={csv}` List of gates, one for each tool to specify the entire TTG map for bulk updates |
  | `MMU_ENDLESS_SPOOL` | With parameters this will display the EndlessSpool groups. It can also modify the defined EndlessSpool groups at runtime | `RESET=1` If specified the EndlessSpool groups will be reset to that defined in mmu_parameters.cfg <br>`GROUPS={csv of groups}` The same format as the default groups defined in mmu_parameters.cfg. Must be the same length as the number of MMU gates | `QUIET=[0\|1]` Optional. Supresses dump of current TTG and endless spool map to log file <br>`ENABLE=[0\|1]` Optional. Force the enabling or disabling of endless spool at runtime (not persisted) |
  | `MMU_SLICER_TOOL_MAP` | Used to set or display (no parameters) tools used in print. Typically this will be set in the print_start macro based on placeholders from the slicer including filament color, material type and temperature setting. This detail can be used in print by reading the printer variable. For example, `printer.mmu.slicer_tool_map.tools.5.material` will contain the material the slicer is expecting in tool T5 or `printer.mmu.slicer_tool_map.purge_volumes[0][2]` the volume in mm^3 to purge when changing from tool T0 to T2 | `TOOL` the tool to set <br> `MATERIAL` the material type e.g. PLA or ABS <br> `COLOR` the color in form RRBBGG <br> `TEMP` the filament print temperature <br> `PURGE_VOLUMES` a comma separated list of purge volumes when changing tools. Map is always NxN but the volumes can be specified as a single volume (same unload/load on every tool), N volumes (same unload/load volume separately for each tool), 2xN volumes (specific unload/load volumes for each tool), or NxN volumes (every possible unload tool and load tool) <br> `QUIET=[0\|1]` whether to suppress output after set <br> `RESET=[0\|1]` used to reset/clear the map <br> `DETAIL=1` will dump the purge volumes map |
  <br>

  ### Status, Logging and Persisted state
  | Command | Description | Parameters |
  | ------- | ----------- | ---------- |
  | `MMU_RESET` | Reset the MMU persisted state back to defaults | `CONFIRM=[0\|1]` Must be sepcifed for affirmative action of this dangerous command |
  | `MMU_STATS` | Dump (and optionally reset) the MMU statistics for current print job or total | `RESET=1` If specified the persisted statistics will be reset (will only apply to counts if COUNTER argument is supplied) <br> `TOTAL=[0\|1]` whether to also show the total swap stats in addition to the current/last print job <br> `DETAIL=[0\|1]` Whether to display additional details about the per-gate statistics <br> `COUNTER=<name>` Consumption counter name <br> `LIMIT=<int>` The maximum count for consumption counter before warning <br> `WARNING="<message>"` The warning message to issue when the counter exceeds limit <br> `PAUSE=1` Whether to pause the print when the limit is reach verses just warning <br> `INCR=1` Increment the consumption counter by one (can be any positive number) <br> `DELETE=1` deletes the specified consumption counter completely (use `RESET=1` to reset count to 0) <br> `SHOWCOUNTS=1` will also display the comsuption counters |
  | `MMU_STATUS` | Report on MMU state, capabilities and Tool-to-Gate map | `DETAIL=[0\|1]` Whether to show a more detailed view including EndlessSpool groups and full Tool-To-Gate mapping <br>`SHOWCONFIG=[0\|1]` (default 0) Whether or not to describe the machine configuration in status message |
  | `MMU_SENSORS` | Report on the state of all sensors connected to the MMU | |
  <br>
  
  ### Servo and motor control
  | Command | Description | Parameters |
  | ------- | ----------- | ---------- |
  | `MMU_SERVO` | Set the servo to specified postion or a sepcific angle for testing. Will also report position when run without parameters | `POS=[up\|down\|move]` Move servo to predetermined position <br>`ANGLE=..` Move servo to specified angle <br>`SAVE=1` Specifed with the POS= parameter will cause Happy Hare to store the current servo angle for the specified position. This is written to `mmu_vars.cfg` |
  | `MMU_MOTORS_OFF` | Turn off both MMU motors | None |
  | `MMU_SYNC_GEAR_MOTOR` | Explicitly override the synchronization of extruder and gear motors. Note that synchronization is set automatically so this will only be sticky until the next tool change | `SYNC=[0\|1]` Turn gear/extruder synchronization on/off (default 1) <br>`SERVO=[0\|1]` If 1 (the default) servo will engage if SYNC=1 or disengage if SYNC=0 otherwise servo position will not change <br>`FORCE_IN_PRINT=[0\|1]` If 1, gear stepper current will be set according to `sync_gear_current`. If 0, gear stepper current is set to 100%. The default is automatically determined based on print state but can be overridden with this argument. Only meaningful if `SYNC=1` |
  
  <br>

  ## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Calibration

```yml
    MMU_CALIBRATE_BOWDEN : Calibration of reference bowden length for gate 0
    MMU_CALIBRATE_ENCODER : Calibration routine for the MMU encoder
    MMU_CALIBRATE_GATES : Optional calibration of individual MMU gate
    MMU_CALIBRATE_GEAR : Calibration routine for gear stepper rotational distance
    MMU_CALIBRATE_SELECTOR : Calibration of the selector positions or postion of specified gate
```
  
  | Command | Description | &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Parameters&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; |
  | ------- | ----------- | ---------- |
  | `MMU_CALIBRATE_GEAR` | Calibration rourine for the the gear stepper rotational distance | `LENGTH=..` length to test over (default 100mm) <br>`MEASURED=..` User measured distance <br>`SAVE=[0\|1]` (default 1) Whether to save the result |
  | `MMU_CALIBRATE_ENCODER` | Calibration routine for MMU encoder | `LENGTH=..` Distance (mm) to measure over. Longer is better, defaults to 400mm <br>`REPEATS=..` Number of times to average over <br>`SPEED=..` Speed of gear motor move. Defaults to long move speed <br>`ACCEL=..` Accel of gear motor move. Defaults to motor setting in ercf_hardware.cfg <br>`MINSPEED=..` & `MAXSPEED=..` If specified the speed is increased over each iteration between these speeds (only for experimentation) <br>`SAVE=[0\|1]` (default 1)  Whether to save the result |
  | `MMU_CALIBRATE_SELECTOR` | Calibration of the selector gate positions. By default will automatically calibrate every gate.  ERCF v1.1 users must specify the bypass block position if fitted.  If GATE to BYPASS option is sepcifed this will update the calibrate for a single gate | `GATE=[0..n]` The individual gate position to calibrate <br>`BYPASS=[0\|1]` Calibrate the bypass position <br>`BYPASS_BLOCK=..` Optional (v1.1 only). Which bearing block contains the bypass where the first one is numbered 1 <br>`SAVE=[0\|1]` (default 1) Whether to save the result |
  | `MMU_CALIBRATE_BOWDEN` | Measure the calibration length of the bowden tube used for fast load movement. This will be performed on gate #0 | `BOWDEN_LENGTH=..` The approximate length of the bowden tube but NOT longer than the real measurement. 50mm less that real is a good starting point <br>`HOMING_MAX=..` (default 100) The distance after the sepcified BOWDEN_LENGTH to search of the extruder entrance <br>`REPEATS=..` (default 3) Number of times to average measurement over <br>`SAVE=[0\|1]` (default 1)  Whether to save the result <br>`MANUAL=1` This allows for calibration without an encoder |
  | `MMU_CALIBRATE_GATES` | Optional calibration for loading of a sepcifed gate or all gates. This is calculated as a ratio of gate #0 and thus this is usually the last calibration step | `GATE=[0..n]` The individual gate position to calibrate <br>`ALL[0\|1]` Calibrate all gates 1..n sequentially (filament must be available in each gate) <br>`LENGTH=..` Distance (mm) to measure over. Longer is better, defaults to 400mm <br>`REPEATS=..` Number of times to average over <br>`SAVE=[0\|1]` (default 1)  Whether to save the result |

<br>

  ## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Testing

```yml
    MMU_SOAKTEST_LOAD_SEQUENCE : Soak test tool load/unload sequence
    MMU_SOAKTEST_SELECTOR : Soak test of selector movement
    MMU_TEST_BUZZ_MOTOR : Simple buzz the selected motor (default gear) for setup testing
    MMU_TEST_CONFIG : Runtime adjustment of MMU configuration for testing or in-print tweaking purposes
    MMU_TEST_FORM_TIP : Convenience macro for calling the standalone tip forming functionality (or cutter logic)
    MMU_TEST_GRIP : Test the MMU grip for a Tool
    MMU_TEST_HOMING_MOVE : Test filament homing move to help debug setup / options
    MMU_TEST_LOAD : For quick testing filament loading from gate to the extruder
    MMU_TEST_MOVE : Test filament move to help debug setup / options
    MMU_TEST_RUNOUT : Manually invoke the clog/runout detection logic for testing
    MMU_TEST_TRACKING : Test the tracking of gear feed and encoder sensing
```
    
  | Command | &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Description&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | Parameters |
  | ------- | ----------- | ---------- |
  | `MMU_SOAKTEST_SELECTOR` | Reliability testing to put the selector movement under stress to test for failures. Randomly selects gates and occasionally re-homes | `LOOP=..[100]` Number of times to repeat the test <br>`SERVO=[0\|1]` Whether to include the servo down movement in the test <br> `HOME=[0\|1]` Whether to include randomized homing operations |
  | `MMU_SOAKTEST_LOAD_SEQUENCE` | Soak testing of load sequence. Great for testing reliability and repeatability| `LOOP=..[10]` Number of times to loop while testing <br>`RANDOM=[0\|1]` Whether to randomize tool selection <br>`FULL=[0\|1]` Whether to perform full load to nozzle or short load just past encoder |
  | `MMU_TEST_BUZZ_MOTOR` | Buzz the sepcified MMU motor. If the gear motor is buzzed it will also report if filament is detected | `MOTOR=[gear\|selector\|servo]` |
  | `MMU_TEST_CONFIG` | Dump / Change essential load/unload config options at runtime | Many. Best to run MMU_TEST_CONFIG without options to report all parameters than can be specified |
  | `MMU_TEST_FORM_TIP` : Convenience macro to call to test the standalone tip forming functionality | Any valid `_MMU_FORM_TIP` gcode variable can be supplied as a parameter and will override the defaults in the `mmu_software.cfg` file. overrides will remain active (sticky) until called with `RESET=1` which will cause Happy Hare to revert to starting values (in `mmu_software.cfg`) <br> `SHOW=1` will just list the current macro variable values and not run macro <br> `RUN=0` will set the variable but not run the macro <br> `FORCE_IN_PRINT=1` behave like in print with gear/extruder syncing and current <br> `EJECT=[0\|1]` Force ejection of filament after tip forming, akin to setting `variable_final_eject=1` |
  | `MMU_TEST_GRIP` | Test the MMU grip of the currently selected tool by gripping filament but relaxing the gear motor so you can check for good contact | None |
  | `MMU_TEST_LOAD` | Test loading filament from park position in the gate. (MMU_EJECT will unload) | `LENGTH=..[100]` Test load the specified length of filament into selected tool <br>`FULL=[0\|1]` If set to one a full bowden move will occur and filament will home to extruder |
  | `MMU_TEST_MOVE` | Simple test move the MMU gear stepper | `MOVE=..[100]` Length of gear move in mm <br>`SPEED=..` (defaults to speed defined to type of motor/homing combination) Stepper move speed <br>`ACCEL=..` (defaults to min accel defined on steppers employed in move) Motor acceleration <br>`MOTOR=[gear\|extruder\|gear+extruder\|extruder+gear]` (default: gear) The motor or motor combination to employ. gear+extruder commands the gear stepper and links extruder to movement, extruder+gear commands the extruder stepper and links gear to movement |
  | `MMU_TEST_HOMING_MOVE` | Testing homing move of filament using multiple stepper combinations specifying endstop and driection of homing move | `MOVE=..[100]` Length of gear move in mm <br>`SPEED=..` (defaults to speed defined to type of motor/homing combination) Stepper move speed <br>`ACCEL=..` Motor accelaration (defaults to min accel defined on steppers employed in homing move) <br>`MOTOR=[gear\|extruder\|gear+extruder\|extruder+gear]` (default: gear) The motor or motor combination to employ. gear+extruder commands the gear stepper and links extruder to movement, extruder+gear commands the extruder stepper and links gear to movement. This is important for homing because the endstop must be on the commanded stepper <br>`ENDSTOP=..` Symbolic name of endstop to home to as defined in mmu_hardware.cfg. Must be defined on the primary stepper <br>`STOP_ON_ENDSTOP=[1\|-1]` (default 1) The direction of homing move. 1 is in the normal direction with endstop firing, -1 is in the reverse direction waiting for endstop to release. Note that virtual (touch) endstops can only be homed in a forward direction |
  | `MMU_TEST_RUNOUT` | Invoke filament runout handler that will also trigger EndlessSpool if enabled and thus useful to validate your load/unload sequence macros (define in `mmu_sequence.cfg`) | `FORCE_RUNOUT=0` optional parameter (defaults to `1`) that if set to `0` will cause HH to try to determine if a clog vs runout by also running a filament movement test |
  | `MMU_TEST_TRACKING | Simple visual test to see how encoder tracks with gear motor | `DIRECTION=[-1\|1]` Direction to perform the test (default load direction) <br>`STEP=[0.5 .. 20]` Size of individual steps (default 1mm) <br>`SENSITIVITY=..` (defaults to expected encoder resolution) Sets the scaling for the +/- mismatch visualization |

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) User defined/configurable macros (defined in mmu_software.cfg)

  | Macro | Description | Supplied Parameters |
  | ----- | ----------- | ------------------- |
  | `_MMU_PRE_UNLOAD` | Called prior to unloading on toolchange | |
  | `_MMU_POST_FORM_TIP` | Called immediately after forming tip | |
  | `_MMU_POST_UNLOAD` | Called after unload is complete and filament is parked at the gate | |
  | `_MMU_PRE_LOAD` | Called prior to the loading of a new filament | |
  | `_MMU_POST_LOAD` | Called subsequent to loading new filament | |

  | `_MMU_FORM_TIP` | Called to create tip on filament (when not under the control of the slicer). You tune this macro by modifying the defaults to the parameters | |
  | `_MMU_CUT_TIP` | Called to create tip by cutting the filament. You tune this macro by modifying the defaults to the parameters | |

  | `_MMU_ACTION_CHANGED` | Callback that is called everytime the `printer.ercf.action` is updated. Great for contolling LED lights, etc | |
  | `_MMU_PRINT_STATE_CHANGED` | Callback when the print job state changes and `printer.ercf.print_state` is updated. Great for contolling LED lights, etc | |
  | `_MMU_GATE_MAP_CHANGED` | Called when gate map is updated. Useful for updating LED lights, etc | |

  | `_MMU_LOAD_SEQUENCE` | Advanced: Called when MMU is asked to load filament | `FILAMENT_POS` `LENGTH` `FULL` `HOME_EXTRUDER` `SKIP_EXTRUDER` `EXTRUDER_ONLY` |
  | `_MMU_UNLOAD_SEQUENCE` | Advanced: Called when MMU is asked to unload filament | `FILAMENT_POS` `LENGTH` `EXTRUDER_ONLY` `PARK_POS` |

  | `_MMU_INITIALIZE` | Call when starting print to setup MMU | `INITIAL_TOOL`, `REFERENCED_TOOLS`, `TOOL_COLORS`, `TOOL_TEMPS`, `TOOL_MATERIALS` (see slicer setup guide) |
  | `_MMU_LOAD_INITIAL_TOOL` | Helper to load initial tool if not paused | |
  | `_MMU_FINALIZE` | Call when ending print to finalize MMU | `EJECT=[0\|1]` Override the macro setting for final unloading of filament (see slicer setup guide) |
  | `_MMU_PRINT_START` | Initialize MMU state and ready for print (optionally include in print start macro) | None |
  | `_MMU_PRINT_END` | Restore MMU idle state after print (optionally include in print end macro) | None |

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Internal macros for custom composition of load/unload sequences

  | Macro | &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Description&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | Parameters |
  | ----- | ----------- | ---------- |
  | `_MMU_STEP_LOAD_GATE` | User composable loading step: Move filament from gate to start of bowden using encoder or gate sensor | |
  | `_MMU_STEP_LOAD_BOWDEN` | User composable loading step: Smart loading of bowden | `LENGTH=..` |
  | `_MMU_STEP_HOME_EXTRUDER` | User composable loading step: Extruder collision detection | |
  | `_MMU_STEP_LOAD_TOOLHEAD` | User composable loading step: Toolhead loading | `EXTRUDER_ONLY=[0\|1]` |
  | `_MMU_STEP_UNLOAD_TOOLHEAD` | User composable unloading step: Toolhead unloading | `EXTRUDER_ONLY=[0\|1]` `PARK_POS=..` |
  | `_MMU_STEP_UNLOAD_BOWDEN` | User composable unloading step: Smart unloading of bowden | `FULL=[0\|1]` `LENGTH=..` |
  | `_MMU_STEP_UNLOAD_GATE` | User composable unloading step: Move filament from start of bowden and park in the gate using encoder or gate sensor | `FULL=[0\|1]` |
  | `_MMU_STEP_SET_FILAMENT` | User composable loading step: Set filament position state | `STATE=[0..8]` `SILENT=[0\|1]` |
  | `_MMU_STEP_MOVE` | User composable loading step: Generic move | `MOVE=..[100]` Length of gear move in mm <br>`SPEED=..` (defaults to speed defined to type of motor/homing combination) Stepper move speed <br>`ACCEL=..` (defaults to min accel defined on steppers employed in move) Motor acceleration <br>`MOTOR=[gear\|extruder\|gear+extruder\|extruder+gear]` (default: gear) The motor or motor combination to employ. gear+extruder commands the gear stepper and links extruder to movement, extruder+gear commands the extruder stepper and links gear to movement |
  | `_MMU_STEP_HOMING_MOVE` | User composable loading step: Generic homing move | `MOVE=..[100]` Length of gear move in mm <br>`SPEED=..` (defaults to speed defined to type of motor/homing combination) Stepper move speed <br>`ACCEL=..` Motor accelaration (defaults to min accel defined on steppers employed in homing move) <br>`MOTOR=[gear\|extruder\|gear+extruder\|extruder+gear]` (default: gear) The motor or motor combination to employ. gear+extruder commands the gear stepper and links extruder to movement, extruder+gear commands the extruder stepper and links gear to movement. This is important for homing because the endstop must be on the commanded stepper <br>`ENDSTOP=..` Symbolic name of endstop to home to as defined in mmu_hardware.cfg. Must be defined on the primary stepper <br>`STOP_ON_ENDSTOP=[1\|-1]` (default 1) The direction of homing move. 1 is in the normal direction with endstop firing, -1 is in the reverse direction waiting for endstop to release. Note that virtual (touch) endstops can only be homed in a forward direction |

> [!NOTE]  
> *Working reference PAUSE / RESUME / CANCEL_PRINT macros are defined in `client_macros.cfg` and can be used/modified if you don't already have your own*

<br>
  
    (\_/)
    ( *,*)
    (")_(") MMU Ready
  
