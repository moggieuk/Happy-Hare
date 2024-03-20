# Tool Changing
- [Turning off slicer tip forming](#turning-off-slicer-tip-forming)<br>
- [Turning off slicer wipetower](#turning-off-slicer-wipetower)<br>
  - [Tip cutting options](#---tip-cutting-options)<br>
  - [Tip forming options](#---tip-forming-options)<br>
  - [Printing without wipetower](#printing-without-wipetower)<br>
- [Returning to print movement](#---return-to-print-movement)<br>
- [Z-hop moves](#---z-hop-moves)<br>
  - [Sequential printing](#sequential-printing)

<br>

The tool change movement options in the guide assume you have configured key toolhead locations (if applicable to your setup) by editing the `mmu_macro_vars.cfg` file:

<img src="/doc/toolchange/toolhead_locations.png" width="900" alt="Toolhead Locations">

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Role of the Slicer

The most important part of MMU printing is understanding how to configure your slicer for "single extruder multi material".  Because each slicer is different this is beyond the scope this documentation.  That said, the common slicers: Prusaslicer, Superslicer and Orcaslicer all have similar interfaces and there are a couple of settings you need to be aware of in deciding between what is performed by Happy Hare (the MMU "firmware") and the slicer.

### Turning off slicer tip forming
The first thing that has to happen on a tool change is to prepare the tip of the filament being removed from the extruder. The simplist and recommend approach is to disable the tip creation process in the slicer and allow Happy Hare to do this. The reason is Happy Hare will have to do this while not actively printing anyway - so why configure what will likely be the most furstrating part of your journey in two places.

Slicers have some quirks and don't make it very straighforward to turn off as you would expect. Instead you need to configure in a number of different areas.  This screenshots shown here are for Prusaslicer but Superslicer and Orcaslicer are very similar.

The first place is a setting like this on the `printer settings` tab.  This disables the primary retract/extrude oscillation that is the bulk of the tip forming and cooling movement.

> [!NOTE]  
> Whilst it is logical to zero all these settings out, Prusaslicer (v2.5) at least has bug that will insert illegal `G1 F0` commands if all the fields are exactly 0.  Instead use a tiny value for the cooling tube length.

<img src="/doc/toolchange/printer_settings.png" width="500" alt="Slicer printer settings">

Working in conjunction with the above and found on the `filament settings` tab is this area where you should zero out all all movement speeds and distances.  Leave only the timing inputs that you can tune once you know the average loading and unloading time for your particular MMU.

<img src="/doc/toolchange/filament_settings.png" width="680" alt="Slicer filament settings">

The next setting must be configure on each of your extruders.  This turns off an initial retraction and subsequent extrude that will leave blobs on your wipetower.  The reason to turn this off is that Happy Hare will correctly load the filament exatly to the nozzle and additonal extrusion will cause a blob.

<img src="/doc/toolchange/printer_settings_extruder.png" width="500" alt="Slicer printer settings per extruder">

Unless you have a sepcialized purge system (documented later) you will want the slicer to manage a wipe tower used to purge out the remains of the previous filament.  To do this, make sure this option is enabled (it usually is by default):

<img src="/doc/toolchange/print_settings.png" width="500" alt="Slicer print settings">

> [!NOTE]  
> If you use SuperSlicer, be sure to turn off Skinnydip:
> <br><img src="/doc/toolchange/skinny_dip.png" width="500" alt="Skinnydip disabling"><br>
> It's probably also a good idea to zero out the distances below.
> Doing this prevents Superslicer from pushing out a blob of filament before cutting the tip.


### Turning off slicer wipetower
To switch to a custom purge system you need only to untoggle the `enable wipetower` option.  All tip forming settings remain the same.

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Role of Happy Hare

Happy Hare controls all of the setup, customization and control of your MMU. It allows your to change tools outside of a print as well as controlling the toolchange and movement inside of a print when the `Tx` toolchange command is issued.  The tip forming logic is the only duplicative component with the slicer and thus you need to decided on always allow the firmware to do it (recommended) or split duties: firmware out of print, slicer while printing. The `force_form_tip_standalone` is an important setting that switches between these options (together with correct slicer configuration).

The rest of this guide describes the toolhead movement possibilities that occurs during a tool change.

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Tip Cutting Options
Firstly, although the default way to form tips is through calculated filament movement, there is an easier way -- just cut it off! There are supported ways to do this at the MMU (through piggybacking on the `_MMU_POST_UNLOAD` callback) the more typical way is with a filament cutter at the toolhead.  This it usually some form of blade that is operated via a dedicated servo mechanism or simply the movement of the toolhead itself and pressing against a pin (optionally itself activated by a servo).

To set this up you need to edit three modular configuraton files: `mmu_parameters.cfg` (the primary setup), `mmu_cut_tip.cfg` (contains the tip cutting macro) and `mmu_sequence.cfg` (contains the default toolhead movement options)

#### Option 1: Cutting tip and parking at the cutter while making the tool change
- Pro: Minimizes movement
- Con: Possibility of oozing in a undesirable part of the build plate
<img src="/doc/toolchange/cutter_cutter.png" width="900" alt="Cutting and Parking at Cutter">

#### Option 2: Cutting tip and parking in a designated park area (often over purge bucket) while making the tool change.
- Pro: Allows of addition of brush cleaning move after the new filament is loaded before returning to wipe tower
<img src="/doc/toolchange/cutter_park_area.png" width="900" alt="Cutting and Parking at Purge">

#### Option 3: Cutting tip and parking at the wipetower
- Pro: Minimizes movement
- Neutral: Possibility of oozing (blobs) on the wipertower. Not really a big problem unless they are large and this is where the slicer designers assume the toolhead will be positioned
<img src="/doc/toolchange/cutter_wipe_tower.png" width="900" alt="Cutting and Parking on Wipetower">

#### Option 4: Cutting tip and, custom purge with no wipe tower <img src="/doc/cool.png" width="40">
- Pro: You get your full buildplate to work with because wipe tower is disabled
- Con: You will need to implement your own purging routine.  This could just be a purge into a large bin followed by nozzle cleaning or, more likely, some form of pellet forming and cleaning.
- Neutral: This is perhaps the coolest option!
<img src="/doc/toolchange/cutter_custom_purge_hh.png" width="900" alt="Cutting and No Wipetower">

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Tip Forming Options

#### Option 5: Forming tip by Happy Hare and parking in a designated park area (often over purge bucket) while making the tool change.
- Pro: Allows of addition of brush cleaning move after the new filament is loaded before returning to wipe tower
<img src="/doc/toolchange/forming_park_area_hh.png" width="900" alt="Tip Forming by HH at Park Area">

#### Option 6: Forming tip by Happy Hare and parking at the wipetower
- Pro: Minimizes movement
- Neutral: Possibility of oozing (blobs) on the wipertower. Not really a big problem unless they are large and this is where the slicer designers assume the toolhead will be positioned
<img src="/doc/toolchange/forming_wipe_tower_hh.png" width="900" alt="Tip Forming by HH at Wipetower">

#### Option 7: Forming tip by slicer and parking at the wipetower
- Neutral: Possibility of oozing (blobs) on the wipertower. Not really a big problem unless they are large and this is where the slicer designers assume the toolhead will be positioned
- Con: You will also need to tune tip forming in the slicer and manage all the settings that were zeroed out above.
- Con: Movement during a toolchange will also be different in a print verses out of a print
<img src="/doc/toolchange/forming_wipe_tower_slicer.png" width="900" alt="Tip Forming by Slicer at Wipetower">

### Printing without wipetower

#### Option 8: Forming tip by Happy Hare, custom purge with no wipe tower <img src="/doc/cool.png" width="40">
- Pro: You get your full buildplate to work with because wipe tower is disabled
- Con: You will need to implement your own purging routine.  This could just be a purge into a large bin followed by nozzle cleaning or, more likely, some form of pellet forming and cleaning.
- Neutral: This is perhaps the coolest option!
<img src="/doc/toolchange/forming_custom_purge_hh.png" width="900" alt="Tip Forming by HH No Wipetower">

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Return To Print Movement
How the toolhead returns to the print has three options contolled by the `variable_restore_xy_pos` variable in `mmu_macro_vars.cfg`:

### "last"
This is the default option and will cause the toolhead to be returned to the **last postion** it was at when the toolchange was issued  before giving control back to the slicer generated code. The exact movement is as follows:
- As a safety step the toolhead Z-height is restored to tool change plane if necessary (this allows for some fault tolerence of user supplied extensions)
- Toolhead travels to the last X,Y position at `variable_travel_speed` speed
- Finally Toolhead Z-height is restored onto the print internally by Happy Hare (undoing `z_hop_height_toolchange` if set) or by slicer g-code

### "none"
In this option the Z-height is restored first to the print height defined at the start of toolchange but no X,Y movement occurs. The specific movements are:
- Toolhead Z-height is restored to the print height defined by the slicer
- When printing resumes the slicer's next travel command (G0 or G1) will move the toolhead back to the print

Caution: If the slicer or Happy Hare isn't configured to do a toolchange z-hop then might result in the toolhead grazing the top of the print

### "next"
This advanced option will cause the toolhead to return to the **next print position**. This is benficial because the travel height can easily be controlled. The specific movements are:
- As a safety step the toolhead Z-height is restored to tool change plane if necessary (this allows for some fault tolerence of user supplied extensions)
- Toolhead travels to the next X,Y position at `variable_travel_speed` speed
- Finally Toolhead Z-height is restored onto the print internally by Happy Hare (undoing `z_hop_height_toolchange` if set) or by slicer g-code
You can see this is a variation on "last" but will prevent print marking. Of course, out of print this will act the same as "last" because there is no concept of "next"

> [!NOTE]  
> To use the "next" functionality you must have this option enabled in your `moonraker.conf`.  This causes Happy Hare to pre-process uploaded g-code file to extract the "next pos" information.
> ```
> [mmu_server]
> enable_toolchange_next_pos: True
> ```

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Z-Hop Moves
It's worth noting and to aid debugging that there are three possible origins for z-hop moves during a toolchange:
- The first is input by the slicer which often have a "z-hop on toolchange option". With the settings described above that should be disabled though.
- The second is by Happy Hare: during a print, HH will immediately lift the toolhead away from the print on toolchange and on error if `z_hop_height_toolchange` is non-zero in `mmu_parameters.cfg`. This move only occurs when in a print and is designed to prevent any chance of a blob forming on your part.  The z-hop move is the first move and occurs before any movement in the horizontal plane.
- Finally, the movement macro defined in `mmu_sequence.cfg` can optionally define a Z-hop move. This lifting move, if configured (`variable_enable_park` and `variable_park_z_hop`) will always happen both in and out of a print. Out of a print and for convenience it will be automatically skipped if the z-axis has not been homed.

This illustrations visualize and explain the toolhead movements in the vertical plane:
<img src="/doc/toolchange/toolchange_z_hop.png" width="100%" alt="Toolchange Z-hop">

The primary configuration options that effect z-height are described here:
<img src="/doc/toolchange/toolchange_z_hop_config.png" width="100%" alt="Toolchange Z-hop Config">

Personally I find it useful to set z-hop to 0.8mm in Happy Hare, disable in the slicer and 0mm in the parking (`mmu_macro_vars.cfg`) macro since out of a print I'm not worried about hitting objects or possible blobs.

<br>

## Sequential Printing
Sequential printing is possible but some additional setup is important because the likely movement path during a toolchange may not be able to avoid completed objects. Therefore it is important to ensure that the z-lifted plane is at least as high as the tallest object. Happy Hare provides a mechanism to make this easy. In you slicer add the following to your POST layer change custom gcode:
```
_MMU_UPDATE_HEIGHT
```
That's it.  This is harmless with normal printing but when printing sequentially it will cause the minimum "z-lifted" plane to be at the height of the highest object.  The z-hop defined in the sequence macros will work relative to this position (rather than current toolhead position) and so will always be the desired height above any printed object thus preventing collisions.

> [!NOTE]  
> If you really want to or have to include in your PRE layer change custom gcode then you must pass the height in like this: `_MMU_UPDATE_HEIGHT HEIGHT=[layer_z]` (or whatever your slicers placeholder variable is that holds the next layer height). The post-layer change is easier because it doesn't need any parameters.

<br>

### More slicer setup help:
[Slicer Setup](/doc/slicer_setup.md)<br>
[Tip Forming and Purging](/doc/tip_forming_and_purging.md)<br>

