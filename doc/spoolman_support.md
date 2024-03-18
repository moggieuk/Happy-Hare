# Spoolman Support
- [Configuration](#---configuration)<br>
- [Gate Map and Spool ID](#---gate-map-and-spool-id)<br>
  - [Auto-setting with RFID reader](#auto-setting-with-rfid-reader)<br>

Spoolman has become a popular way to manage a large collection of print spools. It is a database that you host somewhere (commonly on same rpi as your printer) that can be accessed through a web UI and web based remote procedure calls. Other than providing spool management it does two additional things:
- Tracks filament usage
- Stores attributes about the filament, like print temperature, color and material

Happy Hare fully integrates with spoolman and leverages these two capabilities. Specifically, when Happy Hare selects a filament/spool, spoolman is notified of the selection so that the printer can update usage against the correct spool.  Secondly, spoolman is asked again for filament attributes to ensure Happy Hare's knowledge is up to date. Note that the load of filament attributes is also done in bulk during MMU initialization.

To use spoolman with Happy Hare you need to configure and understand the roll of the "gate map" and the "spool_id" attribute.

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Configuration

Firstly, Happy Hare's moonraker extension should be installed. It will be by default but check that you have the following in your `moonraker.conf`:
```yml
[mmu_server]
enable_file_preprocessor: True
```
> [!IMPORTANT]  
> The 'enable_file_preprocessor' may be True or False depending on whether slicer placeholder pre-processing is enabled. Having the section is important

Secondly, in `mmu_parameters.cfg` enable support:
```yml
enable_spoolman: 1
```
If you made changes, restart klipper and moonraker services and you are done.

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Gate Map and Spool ID

Each gate can have configured information about what is loaded (technically it can have information even if the gate is currently empty). This information used in various features and UI visualization but also is available to you via `printer.mmu.*` printer variables for use in your custom gocde.

The gate map currently consists of: (1) availability of filament, (2) filament material type, (3) filament color in W3C color name or in RGB format, (4) the spoolman spool ID, (5) load/unload speed override. If spoolman is enabled the material and color is automatically retrieved from the spoolman database. Note a direct way to manipulate the gate map is via the `MMU_GATE_MAP` command. For example:
```
Gates / Filaments:
Gate 0: Status: Empty, Material: TPU, Color: DC6834, SpoolID: 3
Gate 1: Status: Spool, Material: PTEG, Color: DCDA34, SpoolID: 2
Gate 2: Status: Empty, Material: PLA, Color: 8CDFAC, SpoolID: 1
Gate 3: Status: Empty, Material: ASA, Color: 95DC34, SpoolID: 4
Gate 4: Status: Empty, Material: ABS, Color: n/a, SpoolID: 5
Gate 5: Status: Empty, Material: ABS, Color: n/a, SpoolID: 6
Gate 6: Status: Empty, Material: ABS+, Color: 34DCAD, SpoolID: 7
Gate 7: Status: Empty, Material: TPU, Color: grey, SpoolID: n/a, Load Speed: 50%
Gate 8: Status: Empty, Material: TPU, Color: black, SpoolID: 9, Load Speed: 60%
```
If spoolman is enabled and a `SpoolID` is available, Happy Hare will use this to pull attributes from spoolman and set the other elements of the gate map. I.e. it will replace whatever was statically created with dynamic data from spoolman. If you exploit that you can simple keep the `SpoolID` up to date when you replace or reload a filament spool and not worry about anything else. If you have LEDs installed they can display the dynamically set filament color.

To set the `SpoolID` use the command like this to set the ID to 5 on gate #0:
```yml
MMU_GATE_MAP GATE=0 SPOOLID=5
```
A `SpoolID` of `-1` can be used to unset the spool ID and other attributes can be set manually as in `MMU_GATE_MAP GATE=0 SPOOLID=-1 COLOR=red MATERIAL=PLA`

See the [command reference](/doc/command_reference.md) for a complete list of command arguments.

> [!NOTE]  
> If you see a command similar to this appear on the console during boot, don't worry. It is the startup sync of gate map with spoolman at work. It doesn't always appears because the sync can occur before the console is ready but seeing it occassionaly is confirmation that everything is connected
> 
> <img src="/doc/spoolman_support/spoolman_update.png" width="70%">

<br>

### Changing SpoolID on Toolchange
Once configured Happy Hare will, on a change of tool, let spoolman know (via moonraker) to deactivate the previous spool and activate the new one. You will see this occur in a UI like mainsail:

<img src="/doc/spoolman_support/spoolman_mainsail.png" width="50%">

If you use my enhanced [KlipperScreen-Happy Hare Edition](https://github.com/moggieuk/KlipperScreen-Happy-Hare-Edition) there are also screens to visualize the gate map with spoolman setup as well as to edit the spoolID:

<img src="/doc/spoolman_support/spoolman_ks.png" width="50%">

<br>

### Auto-setting with RFID reader
So you have fitted all you spools with a fancy RIFD tags and built a nifty RFID onto your printer or MMU, perhaps are already using [nfc2klipper](https://github.com/bofh69/nfc2klipper) to be able to read that info into klipper. How do you automatically use that with Happy Hare?  Here is how...

Because it isn't practical to build a RFID into every gate, the workflow supported by Happy Hare is this:
- Offer up spool to reader
- Read the spool_id you have programmed onto the RFID tag
- In this reader macro, call: `MMU_GATE_MAP NEXT_SPOOLID=..` with the read spool_id
  - Insert filament into gate and run MMU_PRELOAD` to load and park the filament, or
  - If you have pre-gate sensors then simply insert the filament into the back of the gate (this can even be done in print although obviously the filament cannot be preloaded in that case

The gate map entry for this gate will automatically be updated with the spool_id and the rest of the gate map parameters (material/color/temp) will be retrieved from spoolman and, if configured, LEDs updated. The length of time that "NEXT_SPOOLID" remains valid is configured in `mmu_parameters.cfg`:
```yml
pending_spool_id_timeout: 20            # Seconds after which this pending spool_id (set with rfid) is voided
```

> [!TIP]  
> If the `pending_spool_id_timeout` is exceed the spool_id will be forgotten and the RFID would need to be read again. Set this to a little longer than the time it takes you to load a spool into your MMU but not too long that it never expires

<br>

> [!NOTE]  
> In the future Happy Hare may include direct RFID reader support but at present you need to program the calling of `MMU_GATE_MAP NEXT_SPOOLID=..`

