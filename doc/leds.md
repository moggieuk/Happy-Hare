# Happy Hare - LED ("bling") Support
Happy Hare now can drive LEDs on your MMU to provide both functional feedback as well as to add a little bling to your machine.  Typically you would connect a string of neopixels (either descrete components or an LED strip, or combination of both if compatible contollers) to the neopixel output on the MCU that drives your MMU although this can be changed.  What is important is that the first N LEDs of the chain must relate to the N gates of you MMU.  Typically the first LED would be for gate 0 but the order can be reversed by setting `reverse_gate_order:1` in `mmu_software.cfg`.  The optional N+1 LED is design to drive an "exit" light.  I.e. an indicator on or near the bowden output from your MMU. You can also add additional LED's after N+1 because they will be ignored by Happy Hare, but if you do, make sure you restrict effects to that segment of the chain - don't try to control the first N+1 LEDs.

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Hardware Setup
  If you have run the Happy Hare installer it should have added a section to the end of your `mmu_hardware.cfg` that starts like this:

```yaml
# MMU OPTIONAL NEOPIXEL LED SUPPORT ----------------------------------------------------------------------------------------
#
# Define neopixel LEDs for your MMU. The chain_count should match or be greater than your number of gates.
# Requires the installation of Julian Schill's awesome LED effect module: https://github.com/julianschill/klipper-led_effect
# LED index 1..N should be connected from gate #0 to gate #N
# LED index N+1 can optionally be connected to exit from MMU (like encoder in ERCF design)
#
[neopixel mmu_leds]        # Cabinet neopixels (2x18)
pin: mmu:MMU_NEOPIXEL
chain_count: 10            # Number of gates + 1
color_order: GRBW          # Set based on your particular neopixel specification

#
# Define LED effects for MMU gate. 'mmu_led_effect' is a wrapper for led_effect
...
```

This section may be all commented out, if so and you wish to configure LEDS, uncomment and ensure that the `MMU_NEOPIXEL` pin is correctly set in the aliases in `mmu.py`.  Note that you must also install "Klipper LED Effects" plugin.

> [!NOTE]  
> Careful to note the `[mmu_led_effect]` definition.  This is a simple wrapper around `[led_effect]` that will duplicate the effect on all the specified LEDs and each one individually.  This is especially useful on the MMU where you want per-gate feedback.

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) LED Effects
Happy Hare LED effects are 100% implemented in `mmu_software.cfg` as macros so you can tweak if you desire.  I would caution that you make sure you understand the logic before doing so, and in most cases you make just want to tweak the effects defined in `mmu_hardware.cfg` to customize.  The macros work by intercepting changes of the Happy Hare's print state, changes in actions it is performing and changes to the gate_map.

The default effects which are both functional as well as adding a little color are summerized here:

  | State | LED at each Gate | Filament Exit<br>(E.g. Bowden tube) |
  | ----- | ------------ | ----------------------------------- |
  | MMU Disabled | OFF | OFF |
  | Printer State "initialization" | Bling - shooting stars (for 3 seconds) | OFF |
  | Printer State "ready" | **default_gate_effect**:<br>- gate_status<br>- filament_color<br>- off | **default_exit_effect**:<br>- filament_color<br>- white<br>- off |
  | Printer State "printing" | **default_gate_effect**:<br>- gate_status<br>- filament_color<br>- off | **default_exit_effect**:<br>- filament_color<br>- white<br>- off |
  | Printer State "pause_locked"<br>(mmu pause) | Strobe | Strobe |
  | Printer State "paused"<br>(after unlock) | OFF except current gate<br>Strobe | Strobe |
  | Printer State "completed" | Sparkle (for 20 seconds) | **default_exit_effect**:<br>- filament_color<br>- white<br>- off |
  | Printer State "cancelled" | **default_gate_effect**:<br>- gate_status<br>- filament_color<br>- off |
  | Printer State "error" | Strobe (for 20 seconds) | Strobe (for 20 seconds) |
  | Printer State "standby" | OFF | OFF |
  | Action State "Loading"<br>(whole sequence) | OFF except current gate<br>Slow Pulsing White | Slow Pulsing White |
  | Action State "Unloading"<br>(whole sequence) | OFF except current gate<br>Slow Pulsing White | Slow Pulsing White |
  | Action State "Heating" | OFF | Pulsing Red |
  | Action State "Selecting" | Fast Pulsing White | Fast Pulsing White |
  | Action State "Checking" | **default_gate_effect**:<br>- gate_status<br>- filament_color<br>- off | Fast Pulsing White |
  | Action State "Idle" | **default_gate_effect**:<br>- gate_status<br>- filament_color<br>- off | **default_exit_effect**:<br>- filament_color<br>- white<br>- off |

> [!NOTE]
> These are built-in functional "effects"
> **filament_color** - displays the static color of the filament defined for the gate from MMU_GATE_MAP (printer.mmu.gate_color_rgb). Requires you to setup color either directly or via Spoolman.
> **gate_status** - dispays the status for the gate (printer.mmu.get_status): **red** if empty, **green** if loaded, **orange** if unknown

To change the default effect for the per-gate LEDs you edit `default_gate_effect` in the macro variables in `mmu_software.cfg` and for the default exit LED, you can edit `default_exit_effect`.  These effects can be any named effect you define, the built-in functional defaults, on, off, or even an RGB color specification in the form `red,green,blue` e.g. `0.5,0,0.5` would be 50% intensity red and blue with no green.

Happy Hare has an empirical command to control LEDs:

```yaml
> MMU_LED
  LEDs are enabled
  Default gate effect: 'gate_status'
  Default exit effect: `filament_color`
  ENABLE=[0|1] EFFECT=[off|gate_status|filament_color] EXIT_EFFECT=[off|on|filament_color]
```

You can change default effect or enable/disable. E.g. `MMU_LED ENABLE=0` will turn off and disable the LED operation.  Please note that similar to `MMU_TEST_CONFIG` changes made like this don't persist on a restart.  Update the macro variables in `mmu_software.cfg` to make changes persistent.
