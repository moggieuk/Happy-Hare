# Happy Hare - LED ("bling") Support

TODO intro


  ## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) LED Effects


  ## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) LED Effects


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

