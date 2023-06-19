# Summary of configuration differences between documented ERCF and Happy Hare

Happy Hare is a rewrite of the controller software for ERCF.  An attempt has been made where possible to retain backward compatability with existing commands.  About 95% of the setup is the same, but the number of new features mandate some changes.  You much completely read the README in addition to the ERCF manual, however is a quick list of things to watch for:

<ol>
 <li>The installer is recommended for first time setup's.  It will take most of the pain away
 <li>Command to calibrate ERCF are the same (although augmented so the output is different). However the `encoder_resolution` & `encoder_pin` now sit in the `[ercf_encoder] section in ercf_hardware.cfg and not ercf_parameters.cfg
 <li>`end_of_bowden_to_nozzle` is replaced with `home_position_to_nozzle` and dependent on use of toolhead sensor or not
 <li>The `[servo]` section in ercf_hardward.cfg is now `[ercf_servo]`
 <li>There is no `[filament_motion_sensor]` or `[duplicate_pin_override]` configuration anymore
 <li>Don't put any `encoder` control logic in your print_start, pause, resume or cancel macros. This is now automatic
 <li>The `ERCF_CHANGE_TOOL_STANDALONE TOOL={initial_tool}` that you may want in your print_start macro should now be called `ERCF_CHANGE_TOOL STANDALONE=1 TOOL={initial_tool}`
</ol>

Good luck!

    (\_/)
    ( *,*)
    (")_(") ERCF Ready
  
