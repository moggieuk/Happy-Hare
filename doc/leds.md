# Happy Hare - LED ("bling") Support

TODO intro


  ## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) H/W Setup
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

The default effects which are both functional as well as adding a little color are sumerized here:

  | State | At each Gate | Filament Exit<br>(E.g. Bowden tube) |
  | ----- | ------------ | ----------------------------------- |
  | MMU Disabled | OFF | OFF |
  | Printer State "initialization" | Bling - shooting stars (for 3 seconds) | OFF |
  | Printer State "standby" | OFF | OFF |
  | Printer State "completed" | Sparkle (for 20 seconds) | default_exit_effect:<br>- filament_color<br>- white<br>- off |
  | Printer State "cancelled" | default_gate_effect:<br>- gate_status<br>- filament_color<br>- off |
  | Printer State "error" | Strobe (for 20 seconds) | Strobe (for 20 seconds) |
  | Printer State "pause_locked" (mmu pause) | Strobe | Strobe |
  | Printer State "paused" (after unlock) | OFF<br>except current gate Strobe | Strobe |
  | Printer State "printing" | default_gate_effect:<br>- gate_status<br>- filament_color<br>- off | default_exit_effect:<br>- filament_color<br>- white<br>- off |
  | Action State "Loading"<br>(whole sequence) | OFF except current gate Slow Pulsing White | Slow Pulsing White |
  | Action State ""Unloading"<br>(whole sequence) | OFF except current gate Slow Pulsing White | Slow Pulsing White |
  | Action State "Heating" | OFF | Pulsing Red |
  | Action State "Selecting" | Fast Pulsing White | Fast Pulsing White |
  | Action State "Idle" | default_gate_effect:<br>- gate_status<br>- filament_color<br>- off | default_exit_effect:<br>- filament_color<br>- white<br>- off |

