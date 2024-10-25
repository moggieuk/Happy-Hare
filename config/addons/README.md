# Addons
This directory contains recommended optional addons for your MMU setup. 

## ![#f03c15](https://github.com/moggieuk/Happy-Hare/wiki/resources/f03c15.png) ![#c5f015](https://github.com/moggieuk/Happy-Hare/wiki/resources/c5f015.png) ![#1589F0](https://github.com/moggieuk/Happy-Hare/wiki/resources/1589F0.png) EREC Filament Cutter

An addon used to control filament cutting at the MMU rather than the toolhead

<img src="https://github.com/moggieuk/Happy-Hare/wiki/Addon-Feature-Setup/erec_logo.png" width=60%>

### Compatibility
**MMU:** ERCFv2<br>
**Printer:** _Any_
### Github
https://github.com/kevinakasam/ERCF_Filament_Cutter
### Config
1. Add `[include mmu/addons/mmu_erec_cutter.cfg]` to your `printer.cfg`
1. Edit `mmu_erec_cutter.cfg` and `mmu_erec_cutter_hw.cfg` to work with your setup
1. In `mmu_macro_vars.cfg` set `variable_user_post_unload_extension : "EREC_CUTTER_ACTION"`
1. Optionally in `mmu_macro_vars.cfg` set `variable_user_pre_unload_extension : "BLOBIFIER_PARK"` to park the nozzle on the tray during a swap

<hr>

## ![#f03c15](https://github.com/moggieuk/Happy-Hare/wiki/resources/f03c15.png) ![#c5f015](https://github.com/moggieuk/Happy-Hare/wiki/resources/c5f015.png) ![#1589F0](https://github.com/moggieuk/Happy-Hare/wiki/resources/1589F0.png) Blobifier

An addon used to create purge blobs instead of using a wipe tower

<img src="https://github.com/moggieuk/Happy-Hare/wiki/Addon-Feature-Setup/blobifier.png" width=60%>

### Compatibility
**MMU:** _Any_<br>
**Printer:** Voron v2, others in the works
### Github
https://github.com/Dendrowen/Blobifier
### Config
1. Add `[include mmu/addons/blobifier.cfg]` to your `printer.cfg`
1. Edit `blobifier.cfg` and `blobifier_hw.cfg` to work with your setup
1. Set `variable_user_post_load_extension : "BLOBIFIER"` in `mmu_macro_vars.cfg`
1. Optionally set `variable_user_pre_unload_extension : "BLOBIFIER_PARK"` in `mmu_macro_vars.cfg` to park the nozzle on the tray during a swap

<hr>

## ![#f03c15](https://github.com/moggieuk/Happy-Hare/wiki/resources/f03c15.png) ![#c5f015](https://github.com/moggieuk/Happy-Hare/wiki/resources/c5f015.png) ![#1589F0](https://github.com/moggieuk/Happy-Hare/wiki/resources/1589F0.png) Blobifier

An addon used to control a DC motor based respooler that is active when the MMU is unloaded

<img src="https://github.com/moggieuk/Happy-Hare/wiki/Addon-Feature-Setup/TODO.png" width=60%>

### Compatibility
**MMU:** _Any_<br>
**Printer:** _Any_
### Github

### Config
1. Add `[include mmu/addons/respooler.cfg]` to your `printer.cfg`
1. Set `variable_user_pre_unload_extension : "_MMU_RESPOOL_START"` in `mmu_macro_vars.cfg` to start respooler movement
1. Set `variable_user_post_unload_extension : "_MMU_RESPOOL_STOP"` in `mmu_macro_vars.cfg` to stop respooler movement

<hr>
