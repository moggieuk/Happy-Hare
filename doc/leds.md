# Happy Hare - LED ("bling") Support
- [Wiring](#---wiring)<br>
- [Hardware Config](#---hardware-config)<br>
- [Controlling LED Effects](#---controlling-led-effects)<br>
- [Summary of Default Effects](#---summary-of-default-effects)<br>

Happy Hare now can drive LEDs (NeoPixel/WS2812) on your MMU to provide both functional feedback as well as to add a little bling to your machine.  Typically you would connect a string of neopixels (either descrete components or an LED strip, or combination of both if compatible contollers) to the neopixel output on the MCU that drives your MMU although this can be changed.

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Wiring

<p align=center><img src="/doc/leds/led_connection.jpg" alt='LED Connection' width='80%'></p>

LED strips can be formed but soldering together individual neopixels or using pre-made strips.  You can also mix the two but if the "RGBW" order is different you must specify `color_order` as a list with the correct spec for each LED.  There is a lot of flexibility in how the LEDs are connected - segments can even be joined in parallel to drive two LED's for the same index number.  The only important concept is that each segment in the strip that represents the MMU gates must be contiguous (ascending or decending), but the order of segments is unimportant (see config examples below)

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Hardware Config
  If you have run the Happy Hare installer it should have added a section to the end of your `mmu_hardware.cfg` that starts like this:

```yml
# MMU OPTIONAL NEOPIXEL LED SUPPORT ------------------------------------------------------------------------------------
# Define the led connection, type and length
#
# (comment out this section if you don't have leds)
[neopixel mmu_leds]
pin: mmu:MMU_NEOPIXEL
chain_count: 17            # Number gates x1 or x2 + 1 (if you want status)
color_order: GRBW          # Set based on your particular neopixel specification
```
This section may be all commented out, if so and you wish to configure LEDS, uncomment the entire section and ensure that the `MMU_NEOPIXEL` pin is correctly set in the aliases in `mmu.py` and that the `color_order` matches your particular LED (don't mix type or if you do, set to a comma separated list of the type of each led in the chain. e.g. "GRB, GRB, GRB, RGBW, RGBW, RGBW").  Note that you must also install "Klipper LED Effects" plugin.

The wiring of LED's is very flexible but must be controlled by the same pin.  Happy Hare defines three led segments: "entry", "exit" and "status" as described in the config file:
```yml
# MMU LED EFFECT SEGMENTS ----------------------------------------------------------------------------------------------
# Define neopixel LEDs for your MMU. The chain_count must be large enough for your desired ranges:
#   exit   .. this set of LEDs, one for every gate, usually would be mounted at the exit point of the gate
#   entry  .. this set of LEDs, one for every gate, could be mounted at the entry point of filament into the MMU/buffer
#   status .. this single LED represents the status of the currently selected filament
#
# Note that all sets are optional. You can opt simple to have just the 'exit' set for example. The advantage to having
# both entry and exit LEDs is, for example, so that 'entry' can display gate status while 'exit' displays the color
# 
# The effects requires the installation of Julian Schill's awesome LED effect module:
#   https://github.com/julianschill/klipper-led_effect
# LED's are indexed in the chain from 1..N. Thus to set up LED's on 'exit' and a single 'status' LED on a 4 gate MMU:
#   exit_range:   1-4
#   status_index: 5
# In this example no 'entry' set is configured.
#
# Note the range order is important and depends on your wiring. Thus 1-4 and 4-1 both represent the same LED range
# but mapped to increasing or decreasing gates respectively
#
# Note that Happy Hare provides a convenience wrapper [mmu_led_effect] that not only creates an effect on each of the
# [mmu_leds] specified segments but also each individual LED for atomic control. See mmu_leds.cfg for examples
#
# (comment out this section if you don't have leds)
[mmu_leds]
num_gates:                      # Number of gates on your MMU
led_strip: neopixel:mmu_leds
exit_range: 
entry_range: 
status_index: 
frame_rate: 24
```
Some examples of how to set these values can be seen in this illustration (ERCFv2 MMU example):
<p align=center><img src="/doc/leds/led_configuration.png" alt='LED Configuration' width='100%'></p>

> [!NOTE]  
> All the default LED effects are defined in the read-only `mmu_leds.cfg`.  You can create your own but be careful and note the `[mmu_led_effect]` definition - this is a wrapper around `[led_effect]` that will create the effect on the specified LED range (segment) but also duplicate the effect on each LED individually.  This is especially useful on the MMU where you want per-gate effects.

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Controlling LED Effects
Happy Hare LED effects are 100% implemented in `mmu_leds.cfg` as macros so you can review if you want to tweak and create your own modifications.  I would caution that you make sure you understand the logic before doing so, and in most cases you may just want to tweak the effects defined in `mmu_macro_vars.cfg` to customize.  The macros work by intercepting changes of the Happy Hare's print state machine, changes in actions it is performing and changes to the "gate_map" containing gate status and filament color.

You can change default effect or enable/disable in `mmu_macro_vars.cfg` under the `_MMU_LED_VARS` macro:
```yml
# LED CONTROL -------------------------------------------------------------
# Only configure if you have LEDs installed
#   (base/mmu_led.cfg)
#
[gcode_macro _MMU_LED_VARS]
description: Happy Hare led macro configuration variables
gcode: # Leave empty

# Default effects for LED segments when not providing action status
# This can be any effect name, 'r,g,b' color, or built-in functional effects:
#   'off'             - LED's off
#   'on'              - LED's white
#   'gate_status'     - indicate gate availability
#   'filament_color'  - indicate filament color
#   'slicer_color'    - display slicer defined color for each gate (printer.mmu.slicer_color_rgb)
variable_led_enable             : True                  ; Whether LEDs are enabled at startup (MMU_LED can control)
variable_default_exit_effect    : "gate_status"         ;    off|gate_status|filament_color|slicer_color
variable_default_entry_effect   : "filament_color"      ;    off|gate_status|filament_color|slicer_color
variable_default_status_effect  : "filament_color"      ; on|off|gate_status|filament_color|slicer_color
```
To change the default effect for a segment change the appropriate line. These effects can be any named effect you define, the built-in functional defaults, "on", "off", or even an RGB color specification in the form `red,green,blue` e.g. `0.5,0,0.5` would be 50% intensity red and blue with no green.

Happy Hare also has an empirical command to control LEDs:

```yml
> MMU_LED
  LEDs are enabled
  Default exit effect: 'filament_color'
  Default entry effect: 'gate_status'
  Default status effect: 'filament_color'
  ENABLE=[0|1] EXIT_EFFECT=[off|gate_status|filament_color|slicer_color] ENTRY_EFFECT=[off|gate_status|filament_color|slicer_color] STATUS_EFFECT=[off|on|filament_color|slicer_color]
```
You can change the effect at runtime, e.g. `MMU_LED ENTRY_EFFECT=gate_status` or `MMU_LED ENABLE=0` to turn off and disable the LED operation.  Please note that similar to `MMU_TEST_CONFIG` changes made like this don't persist on a restart.  Update the macro variables in `mmu_macro_vars.cfg` to make changes persistent.

The `slicer_color` is not persisted and can be set with the command `MMU_SLICER_TOOL_MAP GATE=.. COLOR=..` as is the case in the recommended `MMU_START_SETUP` macro. The color can be a w3c color name or `RRGGBB` value.

> [!TIP]
> The strongly recommended Happy Hare version of Klipperscreen has buttons to quickly "toggle" between `gate_status` and `filament_color` for the default gate effect...

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Summary of Default Effects
The default effects, which are both functional as well as adding a little color, are summerized here:

  | State | LED at each Gate &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | Filament Exit (E.g. Bowden tube) |
  | ----- | ------------ | ----------------------------------- |
  | MMU Disabled | OFF | OFF |
  | MMU Print State "initialization" | Bling - shooting stars <br>(for 3 seconds) | OFF |
  | MMU Print State "ready" | **default_gate_effect**:<br>- gate_status<br>- filament_color<br>- off | **default_exit_effect**:<br>- filament_color<br>- on (white)<br>- off |
  | MMU Print State "printing" | **default_gate_effect**:<br>- gate_status<br>- filament_color<br>- off | **default_exit_effect**:<br>- filament_color<br>- on (white)<br>- off |
  | MMU Print State "pause_locked"<br>(mmu pause) | Strobe | Strobe |
  | MMU Print State "paused"<br>(after unlock) | OFF except current gate<br>Strobe | Strobe |
  | MMU Print State "completed" | Sparkle <br>(for 20 seconds) | **default_exit_effect**:<br>- filament_color<br>- on (white)<br>- off |
  | MMU Print State "cancelled" | **default_gate_effect**:<br>- gate_status<br>- filament_color<br>- off | **default_exit_effect**:<br>- filament_color<br>- on (white)<br>- off |
  | MMU Print State "error" | Strobe <br>(for 20 seconds) | Strobe <br>(for 20 seconds) |
  | MMU Print State "standby" | OFF | OFF |
  | Action State "Loading"<br>(whole sequence) | OFF except current gate:<br>Slow Pulsing White | Slow Pulsing White |
  | Action State "Unloading"<br>(whole sequence) | OFF except current gate:<br>Slow Pulsing White | Slow Pulsing White |
  | Action State "Heating" | OFF except current gate:<br>Pulsing Red | Pulsing Red |
  | Action State "Selecting" | Fast Pulsing White | OFF |
  | Action State "Checking" | **default_gate_effect**:<br>- gate_status<br>- filament_color<br>- off | Fast Pulsing White |
  | Action State "Idle" | **default_gate_effect**:<br>- gate_status<br>- filament_color<br>- off | **default_exit_effect**:<br>- filament_color<br>- on (white)<br>- off |

> [!NOTE]  
> - MMU Print State is the same as the printer variable `printer.mmu.print_state`
> - Action State is the same as the printer variable `printer.mmu.action`
> - These are built-in functional "effects":
>   - **filament_color** - displays the static color of the filament defined for the gate from MMU_GATE_MAP (specifically `printer.mmu.gate_color_rgb`). Requires you to setup color either directly or via Spoolman.
>   - **gate_status** - dispays the status for the gate (printer.mmu.get_status): **red** if empty, **green** if loaded, **orange** if unknown


