# Spoolman Support
- [Configuration](#---setup)<br>
- [Gate Map and Spool ID](#---gate-map-and-spool-id)<br>

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

> [!NOTE]  
> If you see a command similar to this appear on the console during boot, don't worry. It is the startup sync of gate map with spoolman at work. It doesn't always appears because the sync can occur before the console is ready but seeing it occassionaly is confirmation that everything is connected
> ```yml
> MMU_GATE_MAP MAP="{0: {'spool_id': 3, 'material': 'TPU', 'color': 'DC6834'}, 1: {'spool_id': 2, 'material': 'PTEG', 'color': 'DCDA34'}, 2: {'spool_id': 1, 'material': 'PLA', 'color': '8CDFAC'}, 3: {'spool_id': 4, 'material': 'ASA', 'color': '95DC34'}, 4: {'spool_id': 5, 'material': 'ABS', 'color': ''}, 5: {'spool_id': 6, 'material': 'ABS', 'color': ''}, 6: {'spool_id': 7, 'material': 'ABS+', 'color': '34DCAD'}}" QUIET=1
> ```


### TODO
