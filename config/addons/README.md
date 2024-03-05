# Addons
This directory contains possible addons for your MMU setup. 
## mmu_erec_cutter
...
## Blobifier
An addon used to create purge blobs instead of using a wipe tower
![Blobifier Render](https://raw.githubusercontent.com/Dendrowen/Blobifier/main/Pictures/Render_Full.png)
### Github
https://github.com/Dendrowen/Blobifier
### Config
1. Add `[include mmu/addons/blobifier.cfg]` to your `printer.cfg`
1. Set `variable_user_post_load_extension` to `"BLOBIFIER"` in `mmu_macro_vars.cfg`
1. Optionally set `variable_user_pre_unload_extension` to `"BLOBIFIER_PARK"` in `mmu_macro_vars.cfg` to park the nozzle on the tray during a swap
