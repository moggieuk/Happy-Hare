# Tip Forming and Purging
- [Tuning Filament Tips](#---tuning-filament-tips)<br>
- [Purge Volumes](#---purge-volumes)<br>
- [No Wipe Tower Option](#---no-wipe-tower-option)<br>
- [More Slicer Setup Help](#---more-slicer-setup-help)<br>

There are two parts to an MMU toolchange that are critical to get set up correctly: tip forming and purging. Tip forming is optional if you go the extra step and configure a filament cutting option, but even if you do, it is advisible to have the ability to create decent tips for times when you are not using the cutter or want to fallback to the "traditional" method. Purging is the process of removing reminants of the old filament (and color) so that your print has clean color changes with sharp edges. Let's look at setting up those two cababilities.

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Tuning Filament Tips

The shape of filament tips is of crucial importance for a reliable system. The filament tips need to look like tiny spears, free of any blobs or long hairs. Here are some proper tips that won’t cause any issue:

<img src="/doc/tip_forming_and_purging/good_tips.png" width="100%" alt="Good Tips"><br>

A very solid base for filament profile multimaterial section is to use the default filament Prusa MMU profiles. To do that, add an Prusa printer + MMU from the system presets printers, and then select the MMU printer (NOT the Single one). From there, you’ll be able to access the list of filament system presets built for MultiMaterial (they have the @MMU tag in their name, only use those). Use the filament type of your choice (ABS, PETG, PLA etc.) and use their Multimaterial section settings for your own filaments profiles.

<img src="/doc/tip_forming_and_purging/prusa_starting_point.png" width="100%" alt="Good Starting Point"><br>

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Purge Volumes

In the slicer there are 3 different options when it comes to defining the purging volumes for multi-filament prints:
- Manual definition
- Manual matrix volume
- Advanced purging volume using filament pigmentation

The manual volume definition is simple to setup but lacks depth and may result in wasted filament, the matrix one becomes complicated if you have a high number of tools and finally the advanced purging volume algorithm requires a filament profile for each different pigmentation value. The latter offers the most control and elinination of waste.

Although we are initially talking about how the slicer can create the purge volume matrix who will see later how that information can be passed into Happy Hare for use with custom purge systems that don't require a wipe tower!

### Manual Purging Volume Definition
This option allows you to define the total purge volume for each tool by defining the unloaded and loaded values. For instance, swapping from Tool 0 to Tool 1, the purge volume used will be the sum of the Tool 0 unloaded and the Tool 1 loaded.

<img src="/doc/tip_forming_and_purging/TODO.png" width="100%" alt="TODO"><br>

### Matrix Purging Volume Definition
Clicking on the Show advanced settings in the manual purging volume panel will pop the purging matrix. With this, you can define every single transition precisely, from whatever tool to whatever tool you have. As you can see, when you have a lot of tools you’ll have to track a lot of transitions, which can be painful.

<img src="/doc/tip_forming_and_purging/TODO.png" width="100%" alt="TODO"><br>

### Advanced Purge Volume Algorithm
If you enable the Advanced wiping volume option in the Printer settings, Single Extruder MM setup section, the slicer will use the Pigment percentage, ranging from 0 to 1, to define the purge volume for each swap. You can adjust the different values of this option to finely tune the final purging volume. Note that if you have the same profile for filaments of different colors, you’ll need to duplicate those filament profiles and adjust, for each, the pigment percentage value. Don’t forget to select the proper filament profile for each tool.

### Purging on the Wipe Tower
Normally the purging logic is performed by the slicer and written into the g-code. The purged filament will be deposited onto the wipe tower (that is why `enable wipe tower` must be checked to access the purge matrix and then disabled if you want to use the volumes but not the wipe tower. 

Even with purge volumes setup correctly the configuration of your toolhead parameters also come into play.  Let's assume that you have correctly defined your toolhead geometry [here](/doc/configuration.md#---toolhead-loading--unloading) noting that these settings are not tunables. There will be a correct value for your setup based on the CAD of your toolhead. Ok, with that said it is still necessary to fine tune the purging process and altough the toolhead dimensions will effect this, the correct parameter to tune is `toolhead_ooze_reduction` defined in `mmu_parameters.cfg`. This controls a "delta" in the theoretical loading distance. Typically this would be 0 or a small positive value to reduce the load distance so that the extruder doesn't prematurely extrude plastic. 

#### Tuning `toolhead_ooze_reduction`
Once you are printing your first multi filament print, check the purge tower to verify that the `toolhead_ooze_reduction` value is well tuned (at that your dimensional settings are correct).  If you notice over extrusion during loads (i.e., plastic blobs on the purge tower after a load) you need to increase the `toolhead_ooze_reduction` value.
If you notice big gaps on the purge tower after a load, you need to decrease the `toolhead_ooze_reduction` value. Note that although small negative values are allowed, going negative almost certainly means that the `toolhead_extruder_to_nozzle` or `toolhead_sensor_to_nozzle` are too long. Here is an example purge tower here, with values for the `toolhead_ooze_reduction` from -5 to +10 mm. In this example, the proper value seems to be around 1 mm. Note that because there is some uncertainty in this process (because of the filament tip shape), there will be some slight differences even when the value is the same, as shown in the green to grey and white to orange transitions.

Tweaking this is something that can be done in print with:
```
MMU_TEST_CONFIG toolhead_ooze_reduction=N
```
Just don't forget to persist the final result in `mmu_parameters.cfg` when the print is done.

<img src="/doc/tip_forming_and_purging/toolhead_ooze_reduction.png" width="100%" alt="TODO"><br>

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) No Wipe Tower Option

<br>

### More slicer setup help:
[Slicer Setup](/doc/slicer_setup.md)<br>
[Toolchange Movement](/doc/toolchange_movement.md)<br>

