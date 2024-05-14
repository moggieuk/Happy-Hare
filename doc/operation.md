# Understanding operation of Happy Hare

## ![#f03c15](/doc/resources/f03c15.png) ![#c5f015](/doc/resources/c5f015.png) ![#1589F0](/doc/resources/1589F0.png) MMU_STATUS

The `MMU_STATUS` command can give you a host of information about the state of your MMU or aid your in understanding on how it is currently configured, how it will operate and thus aid to debugging most problems.

### Parameters

> [!NOTE]  
> Tip: It is often useful to run this command after making a configuration change with `MMU_TEST_CONFIG` (or other command). It will help provide an explanation of the restult of the change

Let's disect the full output of the following command:

```
> MMU_STATUS SHOWCONFIG=1 DETAIL=1

Happy Hare v2.4 running ERCF v1.1sb with 9 gates (OPERATIONAL)
Servo in MOVE position, Encoder reads 0.0mm
Print state is INITIALIZED. Selector is HOMED. Tool T0 selected on gate #0
Gear stepper is at 100% and is not synced to extruder

Load Sequence
- Filament loads into gate by homing a maximum of 70.0mm ('gate_homing_max') to ENCODER
- Bowden is loaded with a fast 698.0mm ('calibration_bowden_length') move
- Extruder loads (synced) by homing a maximum of 50.0mm ('toolhead_homing_max') to TOOLHEAD SENSOR before moving the last 62.0mm ('toolhead_sensor_to_nozzle') to the nozzle

Unload Sequence
- Tip is always formed by '_MMU_FORM_TIP' macro and tip forming extruder current is 120%
- Extruder unloads (synced) by reverse homing a maximum 72.0mm ('toolhead_sensor_to_nozzle' + 'toolhead_unload_safety_margin') less reported park position to TOOLHEAD SENSOR, then (unsynced) the remaining 10.0mm ('toolhead_extruder_to_nozzle' - 'toolhead_sensor_to_nozzle') to exist extruder
- Bowden is unloaded with a short 15.0mm ('encoder_move_step_size') validation move before 643.0mm ('calibration_bowden_length' - 'gate_unload_buffer' - 'encoder_move_step_size') fast move
- Filament is stored by homing a maximum of 70.0mm ('gate_homing_max') to ENCODER and parking 23.0mm ('gate_parking_distance') in the gate

Selector touch (stallguard) is DISABLED - blocked gate recovery is not possible
Persistence: All state is persisted across restarts
MMU has an encoder. Non essential move validation is ENABLED
Runout/Clog detection is AUTOMATIC (13.6mm runout), EndlessSpool is ENABLED
SpoolMan is ENABLED. Sensors: TOOLHEAD (Empty), MMU_GATE (Disabled), EXTRUDER (Disabled),
Logging: Console 1(INFO), Logfile 4(STEPPER), Visual 2(SHORT), Statistics 1(ON)

Gates: |#0 |#1 |#2 |#3 |#4 |#5 |#6 |#7 |#8 |
Tools: |T0 | . |T2 |T1+|T4 |T5 |T6 |T7 |T8 |
Avail: | B | . | S | ? | B | . | ? | . | S |
Selct: | * |-------------------------------- T0

MMU [T0] >.. [En] ....... [Ex] .. [Ts] .. [Nz] UNLOADED 0.0mm (e:0.0mm)

Tool/gate mapping and EndlessSpool groups:
T0 -> Gate #0(*) Group_0: 0(*) > 4(*) > 5( ) [SELECTED]
T1 -> Gate #3(?) Group_1: 3(?) > 8(*) > 1( )
T2 -> Gate #2(*) Group_2: 2(*)
T3 -> Gate #3(?) Group_1: 3(?) > 8(*) > 1( )
T4 -> Gate #4(*) Group_0: 4(*) > 5( ) > 0(*)
T5 -> Gate #5( ) Group_0: 5( ) > 0(*) > 4(*)
T6 -> Gate #6(?) Group_3: 6(?)
T7 -> Gate #7( ) Group_4: 7( )
T8 -> Gate #8(*) Group_1: 8(*) > 1( ) > 3(?)

MMU Gates / Filaments:
Gate #0(*) -> T0, Status: Buffered, Material: ASA, Color: 95DC34, SpoolID: 3 [SELECTED]
Gate #1( ) -> ?, Status: Empty, Material: PTEG, Color: DCDA34, SpoolID: 2
Gate #2(*) -> T2, Status: Available, Material: PLA, Color: 8CDFAC, SpoolID: 1
Gate #3(?) -> T1,T3, Status: Unknown, Material: TPU, Color: dc6834, SpoolID: 22
Gate #4(*) -> T4, Status: Buffered, Material: PLA, Color: blue, SpoolID: 5
Gate #5( ) -> T5, Status: Empty, Material: PLA, Color: indigo, SpoolID: 6
Gate #6(?) -> T6, Status: Unknown, Material: PETG, Color: ffffff, SpoolID: 7
Gate #7( ) -> T7, Status: Empty, Material: ABS, Color: back, SpoolID: 8
Gate #8(*) -> T8, Status: Available, Material: ABS, Color: black, SpoolID: 9
```

Let's run through these section-by-section

### Basic State Information
The first section reports the current operational health of the MMU. Here we have a ERCF v1.1 MMU with springy and binky modifications running Happy Hare v2.4 and operating 9 gates. The MMU is operational meaning that it is not disabled (`MMU ENABLE=0`). The current servo position and encoder reading (if fitted) is shown. The print state refects what the MMU is doing relative to a print. If not printing it is most likey to be 'idle' but here the unit has just been 'initialized' either by booting it up or though one of the reset commands (e.g. `MMU_RESET`). As indicated in the text graphic later and the subsequently explained tool-to-gate map, T0 is selected on the expected gate #0.  Finally the gear or filament drive stepper is not currently synchronized to the extruder and is at 100% of its configured current.

```
Happy Hare v2.4 running ERCF v1.1sb with 9 gates (OPERATIONAL)
Servo in MOVE position, Encoder reads 0.0mm
Print state is INITIALIZED. Selector is HOMED. Tool T0 selected on gate #0
Gear stepper is at 100% and is not synced to extruder
```

### Load Sequence
The filament loading sequence is modified entirely through settings in `mmu_parameters.cfg`.  Those settings are transcribed here in english. Your description will likely be different - they are many ways Happy Hare can be configured.  Basically there are three stages to loading: (1) filament is loaded into the gate to the start of the bowden using some reference sensor; (2) the filament is moved to the end of the bowden, often as quickly as possible; (3) the filament is loaded into the extruder and to the nozzle as accurately as possible. It is this last stage that is the most complex and typically envolves establishing a homing point as close as possible to the nozzle.  The goal is to get the filament to align exactly at the nozzle without oozing, ready to continue printing.

```
Load Sequence
- Filament loads into gate by homing a maximum of 70.0mm ('gate_homing_max') to ENCODER
- Bowden is loaded with a fast 698.0mm ('calibration_bowden_length') move
- Extruder loads (synced) by homing a maximum of 50.0mm ('toolhead_homing_max') to TOOLHEAD SENSOR before moving the last 62.0mm ('toolhead_sensor_to_nozzle') to the nozzle
```

### Unload Sequence
Similar to loading the sequence is also modified through parameters in `mmu_parameters.cfg`. Unloading is also more complex and error prone. There are four stages to unloading: (1) a tip is formed at the end of the filament either through a rapid sequence of movements manipulating the molten plastic into a spear like tip or through filament cutting. The procedure can optionally be controlled by the slicer when in print but also (and has to be) controlled by Happy Hare when not printing. The procedure is so nuanced that it is externalized through a gcode-macro. Separate macros are provided for tip forming and cutting; (2) extruder unloads the filament using the best avilable sensors and gear/extruder motor synchronization to ensure that the filament exits the extruder grip, but no more than necessary; (3) the bowden is unloaded (shown here with an optional encoder validate safety move) in a fast movement to a point just prior to the gate homing point; (4) where it homes and then is stored at a precise predetermined location.

```
Unload Sequence
- Tip is always formed by '_MMU_FORM_TIP' macro and tip forming extruder current is 120%
- Extruder unloads (synced) by reverse homing a maximum 72.0mm ('toolhead_sensor_to_nozzle' + 'toolhead_unload_safety_margin') less reported park position to TOOLHEAD SENSOR, then (unsynced) the remaining 10.0mm ('toolhead_extruder_to_nozzle' - 'toolhead_sensor_to_nozzle') to exist extruder
- Bowden is unloaded with a short 15.0mm ('encoder_move_step_size') validation move before 643.0mm ('calibration_bowden_length' - 'gate_unload_buffer' - 'encoder_move_step_size') fast move
- Filament is stored by homing a maximum of 70.0mm ('gate_homing_max') to ENCODER and parking 23.0mm ('gate_parking_distance') in the gate
```

### Feature and Sensor status
This section lists the configuration of the majority of Happy Hare features and the status and availability of various sensors

```
Selector touch (stallguard) is DISABLED - blocked gate recovery is not possible
Persistence: All state is persisted across restarts
MMU has an encoder. Non essential move validation is ENABLED
Runout/Clog detection is AUTOMATIC (13.6mm runout), EndlessSpool is ENABLED
SpoolMan is ENABLED. Sensors: TOOLHEAD (Empty), MMU_GATE (Disabled), EXTRUDER (Disabled),
Logging: Console 1(INFO), Logfile 4(STEPPER), Visual 2(SHORT), Statistics 1(ON)
```

### Representation of MMU gates and filament position
The text graphic representation is probably already familiar.  It depicts each of the physical gates of your MMU, the (logical) tools that is supported by that gate as well as the availability of filament and currently selected tool/gate.  The availablity symbols are: `B` filament is available from buffer, `S` filament is available by tugging on the spool, `.` gate is empty and `?` which means Happy Hare isn't sure about the status. Note that the availability of pre-gate sensors as well as use will allow Happy Hare to fill in the gaps over time.

The filament position graphic shows various sensors along the filament path, the current direction and an approximation of the filament location.  The distance measurement is generally the distance from the gate "endstop" for loading and from the nozzle for unloading.

```
Gates: |#0 |#1 |#2 |#3 |#4 |#5 |#6 |#7 |#8 |
Tools: |T0 | . |T2 |T1+|T4 |T5 |T6 |T7 |T8 |
Avail: | B | . | S | ? | B | . | ? | . | S |
Selct: | * |-------------------------------- T0

MMU [T0] >.. [En] ....... [Ex] .. [Ts] .. [Nz] UNLOADED 0.0mm (e:0.0mm)
```

### Tool/Gate Mapping
This is described in detail under the `MMU_REMAP_TTG` command but depicts from which gate filament is loaded when a particular tool is loaded with the `Tx` command. The `*`/`?`/` ` repeats the availability of filament in that gate.  The groups are supported by the EndlessSpool feature: if the filament runs out on the currently mapped gate, the tool will automatically be mapped to the next gate in the group that contains filament (all features and M220/M221 overrides will be carried forward because this is made to appear and the same tool).  The Tool to Gate map or TTG and EndlessSpool are powerful features that allow you to run print jobs without wondering if you have enough filament to finish or allow you to reprint using gcode that was created when spools were loaded in a different order.

```
Tool/gate mapping and EndlessSpool groups:
T0 -> Gate #0(*) Group_0: 0(*) > 4(*) > 5( ) [SELECTED]
T1 -> Gate #3(?) Group_1: 3(?) > 8(*) > 1( )
T2 -> Gate #2(*) Group_2: 2(*)
T3 -> Gate #3(?) Group_1: 3(?) > 8(*) > 1( )
T4 -> Gate #4(*) Group_0: 4(*) > 5( ) > 0(*)
T5 -> Gate #5( ) Group_0: 5( ) > 0(*) > 4(*)
T6 -> Gate #6(?) Group_3: 6(?)
T7 -> Gate #7( ) Group_4: 7( )
T8 -> Gate #8(*) Group_1: 8(*) > 1( ) > 3(?)
```

### Gate Map
Finally this section depicts what Happy Hare calls the gate map. Each gate can have configured information about what is loaded (technically it can have information even if the gate is currently empty). This information used in various features and UI visualization but also is available to you via `printer.mmu.*` printer variables for use in your custom gocde.

The gate map currently consists of: (1) availability of filament, (2) filament material type, (3) filament color in W3C color name or in RGB format, (4) the spoolman spool ID.  If spoolman is enabled the material and color is automatically retrieved from the spoolman database. Note a direct way to manipulate the gate map is via the `MMU_GATE_MAP` command.

```
MMU Gates / Filaments:
Gate #0(*) -> T0, Status: Buffered, Material: ASA, Color: 95DC34, SpoolID: 3 [SELECTED]
Gate #1( ) -> ?, Status: Empty, Material: PTEG, Color: DCDA34, SpoolID: 2
Gate #2(*) -> T2, Status: Available, Material: PLA, Color: 8CDFAC, SpoolID: 1
Gate #3(?) -> T1,T3, Status: Unknown, Material: TPU, Color: dc6834, SpoolID: 22
Gate #4(*) -> T4, Status: Buffered, Material: PLA, Color: blue, SpoolID: 5
Gate #5( ) -> T5, Status: Empty, Material: PLA, Color: indigo, SpoolID: 6
Gate #6(?) -> T6, Status: Unknown, Material: PETG, Color: ffffff, SpoolID: 7
Gate #7( ) -> T7, Status: Empty, Material: ABS, Color: back, SpoolID: 8
Gate #8(*) -> T8, Status: Available, Material: ABS, Color: black, SpoolID: 9
```

