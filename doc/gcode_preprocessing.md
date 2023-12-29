# G-Code Preprocessing
Happy Hare now provides a moonraker gcode preprocesser that parses uploaded gcode files prior to storage and can insert useful metadata that can then be passed into your `START_PRINT` macro to provide useful functionality.

This support is added to Moonraker configuration during installation but can be added manually by inserting the following lines into your `moonraker.conf` file and restarting Moonraker. Setting `enable_file_preprocessor: False` will disable this functionality.

```yml
[mmu_server]
enable_file_preprocessor: True
```
The functionality is similar to the "placeholder" substitution that many slicers do.  For example if you use `{total_toolchanges}` in PrusaSlicer, it will substitute this placeholder with the actual number of tool changes.  When used in conjunction with a call to your `START_PRINT` macro you can pass parameters to use at print time. E.g. by defining your "start" G-Code as:

```
START_PRINT TOOL_CHANGES={total_toolchanges} ....
```

Your `START_PRINT` macro thus gets the number of tool changes passed as an integer parameter.

<br>

The Happy Hare pre-processor implements similar functionality but runs when the file is uploaded to Moonraker. To distinguish from Slicer added tokens, this pre-processor delimits placeholders with `!` marks. Note that this does not duplicate all the Slicer placeholders but provides an extensible mechanism to implement anything missing in the slicer. At this time a single placeholder `!referenced_tools!` is implemented but this might grow over time.

> [!NOTE]  
> The `{referenced_tools}` placeholder has been submitted as a PR for PrusaSlicer but it has not yet been incorporated, so you can use `!referenced_tools!` instead!

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Supported Placeholders

### !referenced_tools!
This placeholder is substituted with a comma separated list of tools used in a print.  If there are no toolchanges (non MMU print) it will be an empty string. E.g. `0,2,5,6` means that T0, T2, T5 and T6 are used in the print.

__Why is this useful?__
<br>
When combined with the `MMU_CHECK_GATES TOOLS=` functionality and placed in your `START_PRINT` macro it allows for up-front verification that all required tools are present before commencing the print!

To implement incorporate into your start g-code on your Slicer:

```yml
START_PRINT REFERENCED_TOOLS=!referenced_tools! INITIAL_TOOL={initial_tool} ...the other parameters...
```

Then in you print start macro add logic similar to:

```yml
[gcode_macro START_PRINT]
description: Called when starting print
gcode:
    {% set REFERENCED_TOOLS = params.REFERENCED_TOOLS|default("")|string %}
    {% set INITIAL_TOOL = params.INITIAL_TOOL|default(0)|int %}

    {% if REFERENCED_TOOLS == "!referenced_tools!" %}
        RESPOND MSG="Happy Hare gcode pre-processor is diabled"
        {% set REFERENCED_TOOLS = INITIAL_TOOL %}
    {% elif REFERENCED_TOOLS == "" %}
        RESPOND MSG="Happy Hare single color print"
        {% set REFERENCED_TOOLS = INITIAL_TOOL %}
    {% endif %}

    :

    MMU_CHECK_GATE TOOLS={REFERENCED_TOOLS}

    MMU_CHANGE_TOOL STANDALONE=1 TOOL={INITIAL_TOOL} # Optional: load initial tool

    :
```

> [!NOTE]  
> * `MMU_CHECK_GATE TOOLS=` with empty string will be ignored by Happy Hare.<br>
> * Any tool that was loaded prior to calling `MMU_CHECK_GATES` will be automatically restored at the end of the checking procedure.<br>
> * In the gcode snippet above we also pass in the slicer placeholder {initial_tool} because single color prints have no tool changes and thus `REFERENCED_TOOLS` (which counts `Tx` commands) will be empty. This code will ensure that `REFERENCED_TOOLS` will always contain the initial tool.

