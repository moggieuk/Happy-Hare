# Happy Hare - Detailed Revision History

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Imperfect Log Changes since V2 launch

### v2.0.1
- Initial Release (forked from my ERCF-Software-V3 project which is now deprecated).
- HHv2 is a rewrite to structure the software so it can support all types of MMU (only ERCF at release) and sanitize command set
- Adds total control of motor synchronization, multiple endstops (even for the extruder!!)
- Although HHv1 (aka ERCF-Software-V3) will remain available, HHv2 will be where all future enhancements will be made
- The latest KlipperScreen-Happy_Hare edition requires HHv2 (for my sanity)
- Much better doc and LOTS of new features to discover

### v2.1.0
- Speed and extrusion overrides (M220/M221) support .. records overrides across tool changes (MMU_TOOL_OVERRIDES command to see/reset)
- SpoolMan support (new options to MMU_GATE_MAP for SpoolD.. see  doc)
- Separate "per-print" and total swap stats!  No need to clear in your print_start anymore.
- "Auto restoring" gate quality indication (the "excellent/good/../terrible" one).  Slowly averages out bad results.
- New "state machine" that closes a lot of annoying corner cases that I knew about but most users hadn't found yet [doc](https://github.com/moggieuk/Happy-Hare#13-job-state-transistions-and-print-startend-handling)
- New filament cutter option (Alternative `_MMU_CUT_TIP` macro) instead of tip forming and `mmu_form_tip_macro` setting [doc](https://github.com/moggieuk/Happy-Hare/blob/main/doc/gcode_customization.md#---_mmu_form_tip_standalone)
- MMU_UNLOCK is back (but as an optional step to resume temps).  Can just call `RESUME` as well.
- Better support for Octoprint users where the [print_stats] module is not available. Read up on new state machine and `_MMU_START_PRINT` and `_MMU_END_PRINT` conventions (must read doc)
- New  moonraker gcode pre-processor module! Adds !referenced_tools!  placeholder so you can automatically check all used tools before printing [doc](https://github.com/moggieuk/Happy-Hare/blob/main/doc/gcode_preprocessing.md)
- 'MMU_FORM_TIP' command updated to allow for runtime "tuning"  Any variable to the macro can be adjust (and persisted) for testing or tweaking in print (handles tip cutting macro as well)
- Config now also automatically adjusts references to "extruder"  when referring to stepper (e.g. in rare [controller_fan], [homing_heaters] and [angle])
- Lots of little things/bug fixes but I lost track 🫣

### v2.2.0
- Replacement of manual steppers with new MMU toolhead - faster homing and movements in general, new optional `gate` and `extruder` sensors, optional encoder, intial support for Tradrack and other customized designs.
- Ever wanted to use Happy Hare on a non-ERCF MMU?
- Ever wanted to use a pre-extruder entry sensor instead of collision?
- Wanted to fit a gate sensor and not rely on encoder for loading and parking at the gate
- Want to run without an encoder? (why? 🤷 )
- Want fast (no wait) homing?
- DON'T WANT TO RECONFIGURE YOUR EXISTING EXTRUDER? 👍
- Want the latest and greatest features?

### v2.3.0
**NOTE: Requires Klipper 0.12.0 or greater**
- LED support for bling, gate_status, filament color and action status, pre-gate sensor support for automated loading and gate_status setting, BTT MMB board support, integrated filametrix cutter support, new [mmu_sensors] config section of easy sensor setup. Doc improvements.
New Features:
- LED (bling) support! See new page in the [doc](https://github.com/moggieuk/Happy-Hare/tree/main?tab=readme-ov-file#14-leds)
- Pre-gate sensor support:  Automatically set gate_status, LED status and activate pre-load. Oh, and new earlier run-out detection of reliable EndlessSpool
- Installer updates and support for BTT MMB board
- Integrated Filament Cutter support (Filametrix)
- Improved doc. E.g. [Conceptual MMU](https://github.com/moggieuk/Happy-Hare/blob/main/doc/conceptual_mmu.md)
- New [mmu_sensors] section for simple setup of filament_sensors and endstops
- Enhancements for gate_sensor as alternative or in addition to encoder
- Lots of bug fixes and minor enhancements requested.
- Version tracking and better feedback on what to do
- Enhancements to existing commands. E.g try: 'MMU_STATUS SHOWCONFIG=1'

### v2.3.1
- Full Spoolman integration: will now pull material and colors from spoolman in addition to activating the spool
- Allow the LED effects to be configure anywhere on a chain (as well as gate 0->N or N->0 ordering)
- EndlessSpool got some love because I think it will be much more valuable with pre-gate switches and early runout detection:
 - a) endless_spool_on_load parameter that will activate ES on loading a tool with empty gate
 - b) endless_spool_final_eject distance specification for push beyond park position in an attempt to prevent filament from being accidentally re-loaded
 - c) Cleanup of the display on klipper console and log messages
 - d) Will ensure that the gate_status is at least "unknown" when MMU_REMAP_TTG is run, so attempt will always be made to load from the gate

### v2.4.0
- Updated LED support with lots more "multi-segment" flexibility
- New servo calibration - to fine tune and save without klipper restart!
- New full set of default toolhead positioning macros (defined in `mmu_sequence.cfg`)
- Full support for pre-extruder sensor option (prior to extruder entry)
- Exposed vendor-specific params (including the "cad_" set -- see doc at bottom of `mmu_parameters.cfg`)
- Full support for Tradrack including installer
- New manual bowden calibration for setups without encoder
- Workaround for CANbus comms timeout that is plaguing klipper
- Much improved `MMU_STATUS SHOWCONFIG=1`.  It will tell you in english what loading and unload sequence you have based on dynamic changes with `MMU_TEST_CONFIG` or sensor disable/enable.
- EndlessSpool is now available on tool load
- Sync feedback sensor support .. I.e support for Annex Belay or another other sensor including proportional feedback. [doc](https://github.com/moggieuk/Happy-Hare/tree/main?tab=readme-ov-file#4-synchronized-gearextruder-motors)
- Improved "tip forming" test procedure and `MMU_FORM_TIP` command
- Fixed silly bug in spoolman integration where spool_id was being used to search as filament_id
- New `toolhead_ooze_reduction` parameter for tuning without messing with what should be fixed extruder measurements. Doc page to follow
- Refined toolhead unloading with better detection of incorrect config
- Cleanup and separation of config files based on function
- Lots of new/updated doc

### v2.4.1
- Fixes / update to the way toolhead movement occurs through the "sequence macros" like `_MMU_PRE_UNLOAD` and `_MMU_POST_LOAD` etc. 
  - Also if enabled these will now work while not actively printing (that was an oversight)
  - These macros also play nicely with Klippain  pause/resume macros now
  - The z_hop_height_error has been deprecated. Additional z_hop height can be configured in the macro variables at the start of mmu_sequence.cfg
- LED update
  - Better error feedback on LED misconfiguration
  - Fix for led index when order of reversed.

### v2.4.2 (Klipperscreen-Happy Hare edition will also need to be updated)
- New placeholder preprocessing for colors and filament temps pulled from you slicer ( !colors! and !temperatures! ). See [here](https://github.com/moggieuk/Happy-Hare/blob/main/doc/gcode_preprocessing.md)
- LED update: New effect `custom_color`.  This will display colors stored for each gate based on user setting. One example use is to render the colors used in the slicer so you can visually compare with what is loaded.  Documentation is in the gcode pre-processing section.
- Improved movement "sequence" macros.  These now work better when not completely homed (e.g. z-hop is optional.
- CUT_TIP macro now has option to control whether movement goes back to wipetower or not after cut
- Faster pausing on runout
- Fix for not automatically engaging the sync/servo after fixing error and resuming.
  - New [doc](https://github.com/moggieuk/Happy-Hare/blob/main/doc/slicer_setup.md) on how to setup your slicer to disable tip forming
  - New [doc](https://github.com/moggieuk/Happy-Hare/blob/main/doc/toolchange_movement.md) on how to setup toolhead movement during toolchange or error
- Couple of new states to filament movement.  These are to enable and display of various other sensors such as a gate sensor (option to encoder) and pre-entry extruder sensor.
- New rendering of filament position in console (and KlipperScreen-HH) showing all sensor options if fitted
- Imporved use of miscellaneous sensors to detect errors or non-errors
- Cleanup of the status displays of various commands `MMU_GATE_MAP`, `MMU_TTG_MAP`, `MMU_ENDLESS_SPOOL`
- New encoder calibration routine that allows calibration that "remembers" gate homing point and compensates for space between gate sensor and encoder if both are fitted
- Other bug fixes report in github "Issues"

### v2.4.3
- Bug fixes reported via github "Issues"
- Added capability to install to auto-check github to ensure the latest version and to switch branches with `-b <branch name>` option

### v2.5.0 (Recommend Klipperscreen-Happy Hare edition should be updated to get dialog popup fixes)
This release centralizes macro configuration and extends will a lot more pre-packaged options
- Macro config moved into a unified `mmu_macro_vars.cfg`.
- Default macros have become read-only with a formal way to add custom extensions
- New recommended "print_start" and end integration
  - See https://github.com/moggieuk/Happy-Hare/blob/main/doc/slicer_setup.md
- New `MMU_SLICER_TOOLS_MAP` command that is used by the "print_start" and for easy integration of non-wipetower purge options like the excellent "Blobifier"
E.g.
```
> MMU_SLICER_TOOL_MAP DETAIL=1
--------- Slicer MMU Tool Summary ---------
2 color print (Purge volume map loaded)
T0 (Gate 0, ASA, ff0000, 245°C)
T1 (Gate 1, ABS+, 00fe02, 240°C)
T6 (Gate 6, ABS, 0310fe, 240°C)
Initial Tool: T0
-------------------------------------------
Purge Volume Map:
To -> T0   T1   T2   T3   T4   T5   T6   T7   T8
T0    -   200  210  210  200  200  200  210  210
T1   200   -   210  210  200  200  200  210  210
T2   210  210   -   220  210  210  210  220  220
T3   210  210  220   -   210  210  210  220  220
T4   200  200  210  210   -   200  200  210  210
T5   200  200  210  210  200   -   200  210  210
T6   200  200  210  210  200  200   -   210  210
T7   210  210  220  220  210  210  210   -   220
T8   210  210  220  220  210  210  210  220   -
```
- New [doc](https://github.com/moggieuk/Happy-Hare/blob/main/doc/tip_forming_and_purging.md) on tip forming and purging
- New printer variables:
   - `printer.mmu.slicer_tool_map.initial_tool`
   - `printer.mmu.slicer_tool_map.tools.<tool_num>.material|color|temp`
   - `printer.mmu.slicer_tool_map.purge_volumes`
   - `printer.mmu.runout` which is true during runout toolchange
   - `printer.mmu.active_gate` map of a attributes of current filament (like color, material, temp,..)
- Z-hop modfications:
   - By default HH will not return to pre-toolchange position (will only restore z-height).
   - New `variable_restore_xy_pos: True|False` to control sequence macros return to starting pos or let the slicer do it. This has benefit when printing without a wipe tower so the print is not contaminated at the point of tool-change
- New "addons" folder for recommended third-party extensions with ready-to-use configs
   - Includes @kevinakasam's "EREC" filament cutter logic for cutting at the MMU (ERCF specific)
   - Includes @dendrowen's excellent "Blobifier" - intelligent purging that doesn't require a wipe tower!! (Any MMU)
- Enhanced `MMU_SENSORS` command for quick review of all mmu sensors
- New (optional) popup dialog option in Mainsail/KlipperScreen/Fluidd when MMU pauses on error
- Two new pre-processing placeholders: !materials! and !purge_volumes!
- Also, thanks to the Blobifer author, @dendrowen, the "MMU Statistics" has been given some love with new layout and some new customization. For both total stats and current job status. See `console_stat_*` options in `mmu_parameters.cfg`). Note advanced formatting on Python3 only.
```
MMU Statistics:
+------------+-----------------------+--------------------------+----------+
| 1895(1230) |       unloading       |         loading          | complete |
|   swaps    | pre  |    -    | post | pre  |    -    |   post  |   swap   |
+------------+------+---------+------+------+---------+---------+----------+
|     total  | 0:47 | 6:54:24 | 0:02 | 0:02 | 5:35:31 | 6:40:30 | 20:05:52 |
|      └ avg | 0:00 |    0:13 | 0:00 | 0:00 |    0:10 |    0:12 |     0:35 |
|  this job  | 0:36 | 4:26:51 | 0:01 | 0:01 | 3:34:34 | 4:34:54 | 13:22:01 |
|      └ avg | 0:00 |    0:12 | 0:00 | 0:00 |    0:10 |    0:13 |     0:38 |
|       last | 0:00 |    0:12 | 0:00 | 0:00 |    0:10 |    0:17 |     0:42 |
+------------+------+---------+------+------+---------+---------+----------+

11:43:27 spent paused over 10 pauses (All time)
8:15:38 spent paused over 3 pauses (This job)
Number of swaps since last incident: 105 (Record: 1111)

Gate Statistics:
#0: 😎, #1: 😎, #2: —, #3: —, #4: —, #5: —, #6: 😎, #7: —, #8: —
```

### v2.5.1
The release provides more flexibilty in tool change movement, introduces consumption counters, optimizes statistics output
- New (moonraker) pre-processing option to lookahead for next print location to allow for option to move to the NEXT print position on completion of tool change. Requires addition to `[mmu_server]` section of `moonraker.conf`:
  - `enable_toolchange_next_pos: True`
- `variable_restore_xy_pos` can now be "none", "last" or "next" (next being new functionality)
- Augmented `MMU_STATS` functionality to provide "consumption counters" that can warn or even pause your print when threshold is exceeded
- New doc page to explain statistics and consumption counters: [/doc/stats.md](/doc/stats.md)
- New doc page to explain spoolman config and use: [/doc/spoolman_support.md](/doc/spoolman_support.md)
- Elimination of "h" option to ERCFv2 MMU - ThumperBlock are not a compatible 23mm wide so no longer required
- More accurate timing of tool change phases
- Defaults for "white" and "black" filament can not be configured in `mmu_macro_vars.cfg`
- Fixes to `printer.mmu.runout` and `printer.mmu.last_tool` for better accuracy at all possible times
- Filament remaining in toolhead is now tracked accross reboots / restarts to prevent over extruding initial load
- `custom_color` is now a more intuitive `slicer_color` and set with the `MMU_SLICER_TOOL_MAP` command (MMU_START_SETUP does this for you)
- New config parameter `extruder_temp_variance` to specify the +/- delta allowable when waiting for extruder temperature
- Updates and to bloblifier macro (needs latest klipper)
- Allow specifying spool_id in advance for supporting RFID readers. You can read more about it [here](/doc/spoolman_support.md)

