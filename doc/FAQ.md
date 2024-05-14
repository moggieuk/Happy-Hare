# Happy Hare - FAQ
This is a new section that is under construction.

## ![#f03c15](/doc/resources/f03c15.png) ![#c5f015](/doc/resources/c5f015.png) ![#1589F0](/doc/resources/1589F0.png) Hardware

## ![#f03c15](/doc/resources/f03c15.png) ![#c5f015](/doc/resources/c5f015.png) ![#1589F0](/doc/resources/1589F0.png) Calibration

## ![#f03c15](/doc/resources/f03c15.png) ![#c5f015](/doc/resources/c5f015.png) ![#1589F0](/doc/resources/1589F0.png) Slicer

### Invalid speed in 'G1 F-2147483648' (or similar)
This is a divide by zero bug in popular slicers like superslicer, meaning somewhere you have two inverse settings that together equal zero and superslicer. It occurs when you try to completely turn of slicer tip forming. The workaround is to set the offending value to a near-zero number. E.g.

<img src="error_2147483648.jpg" width=50%>


## ![#f03c15](/doc/resources/f03c15.png) ![#c5f015](/doc/resources/c5f015.png) ![#1589F0](/doc/resources/1589F0.png) Loading Problems

## ![#f03c15](/doc/resources/f03c15.png) ![#c5f015](/doc/resources/c5f015.png) ![#1589F0](/doc/resources/1589F0.png) Unloading Problems

### "Filament seems to be stuck in extruder error" (but it's not really stuck)
This is a surprisingly common question if you have an encoder but no toolhead sensor and is a result of a config error... Although it might seem confusing, think about what Happy Hare is doing to unload the extruder... if moves the filament and looks at the encoder for movement.  If it doesn't see any then it has to conclude the filament is stuck.<br>
It almost always happens because the preceeding "tip forming" move is erroneously ejecting the filament clear out of the extruder.  If tip forming is configured to be done by the slicer, review what you have asked it to do - it should only form tip and NOT eject filament.  If using standalone tip forming or tip cutting - make sure slicer is not doing anything and make sure the forming/cutting macro is not ejecting filament.
