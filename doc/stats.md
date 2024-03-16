# MMU Print Statistics and Consumption Counting
- [Swap Timings](#---swap-timings)<br>
- [Gate Statistics](#---gate-statistics)<br>
- [Consumption Counters](#---consumption-counters)<br>

Happy Hare records tool change statistics including things like the number of swaps, the number of errors/pauses as well as detailed timing for each step of the process. These statistics are independently recorded for the current print as well as all time totals (since last reset). In addition there is a simple "consumption counting" framework that can be used to remind you to change consumables like filament cutting blade or servo arm. It can even pause the print if critical counts are hit.

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Swap Timings

### Viewing Statistics
Statics can be viewed at anytime with the `MMU_STATS` command:
```
+------------+-----------------------+--------------------------+----------+
| 1895(1230) |       unloading       |         loading          | complete |
|   swaps    | pre  |    -    | post | pre  |    -    |   post  |   swap   |
+------------+------+---------+------+------+---------+---------+----------+
|     total  | 0:47 | 6:54:24 | 0:02 | 0:02 | 5:35:31 | 6:40:30 | 20:05:52 |
|      └ avg | 0:00 |    0:13 | 0:00 | 0:00 |    0:10 |    0:12 |     0:35 |
|  this job  | 0:36 | 4:26:51 | 0:01 | 0:01 | 3:34:34 | 4:34:54 | 13:22:01 |
|      └ avg | 0:00 |    0:12 | 0:00 | 0:00 |    0:10 |    0:13 |     0:38 |
|       last | 0:00 |    0:12 | 0:00 | 0:00 |    0:10 |    0:17 |     0:42 |
+------------+------+---------+------+------+---------+---------+----------+

11:43:27 spent paused over 10 pauses (All time)
8:15:38 spent paused over 3 pauses (This job)
Number of swaps since last incident: 105 (Record: 1111)
```

This example is taken during a print and you can see the timing of each phase of the tool change process. These phases match the "sequence macros" described [here](/doc/macro_customization.md) and can be useful for debugging and optimization. The total time the MMU is in a paused (error) state is also tracked together will a "fun" metric of how many tool changes since the last incident including your record!

You can customize which rows and columns are displayed via configuration in `mmu_parameters.cfg`.  This allows you to shrink the size of the table and can be useful to ensure it fits on say a KlipperScreen popup:

```yml
# Comma separated list of desired columns
# Options: pre_unload, unload, post_unload, pre_load, load, post_load, total
console_stat_columns: pre_unload, unload, post_unload, pre_load, load, post_load, total

# Comma seperated list of rows. The order determines the order in which they're shown.
# Options: total, total_average, job, job_average, last
console_stat_rows: total, total_average, job, job_average, last

# Always display the full statistics table
console_always_output_full: 1   # 1 = Show full table, 0 = Only show totals out of print
```
`MMU_STATS TOTAL=1` will display the totals but you can also force the totals to always be displayed by this setting in `mmu_parameters.cfg`
```yml
console_always_output_full: 1
```
As well as on demand display there is an option which, if enabled, will dump a table of statistics after each tool change. This defaults to enabled but can be disabled by setting to `0`:
```yml
log_statistics: 1      # 1 to log statistics on every toolchange (default), 0 to disable (but still recorded)
```

### Statistics Storage
Happy Hare stores all statics in the defined `[save_variables]` file, typically `mmu_vars.cfg` so that they are persisted on roboot/restart.

> [!CAUTION]  
> Although `mmu_vars.cfg` can be edited directly be careful you don't corrupt it because then Happy Hare may fail to start

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Gate Statistics
Happy Hare also records information about the quality of operation of each gate. If an encoder is fitted this includes a measurement of filament slippage, or times the servo failed to catch/grip the filament. To see the raw details run:

> MMU_STATS DETAIL=1

Because that is a lot of information, it is heuristically summarised as an assement with fun ways to visualize:
<img src="/doc/stats/gate_statistics.png" width="70%"><br>
Visualization is controlled by this setting in `mmu_parameters.cfg`:
```yml
# How you'd want to see the state of the gates and how they're performing
#   string     - poor, good, perfect, etc..
#   percentage - rate of success
#   emoticon   - fun sad to happy faces (python3 only)
console_gate_stat: emoticon
```

> [!NOTE]  
> - The purpose of this this is to draw your attention to a gate that might be misbehaving. This might be due to poor calibration, slippage caused but too much friction to moving too fast. You will have to deep dive to find the source but the comparison with other gates is useful
> - Don't be obsessed with always wanting a perfect score. The summary will slowly trend back to good after a problem is addressed - you don't need to reset

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Consumption Counters

The `MMU_STATS` command has a dual purpose of providing a mechanism for simple counting and warning. The purpose is to track consumables on your particular MMU and warning (optionally pause) when the count is reached. This is best illustrated with an example:

Suppose you have fitted a filament cutter that has a blade that dulls (and ideally needs replacing) after 4000 cuts.

### To Setup:

> MMU_STATS COUNTER=cutter_blade LIMIT=4000 WARNING="You may need to replace your cutting blade" PAUSE=0

It is harmless for this to be specified multiple times because it doesn't reset so you could include in a macro if you wanted. You can also optionally specify `PAUSE=1` to the setup command. This will turn the warning into an MMU pause/error. Whilst this is not the default it could be useful if you need to take proactive action such as emptying a purge tray for example.

### Counting:

> MMU_STATS COUNTER=cutter_blade INCR=1

Typically you would add this into a macro that is called when the consumable is used. Can be any increment not just 1. When the LIMIT is exceeded this would generate a message:
```yml
Warning: Count cutter_blade (4001) above limit 4000 : You may need to replace your cutting blade
```

### Resetting:

> MMU_STATS COUNTER=cutter_blade RESET=1

Will only reset just the "cutter_blade" count to 0.

### Viewing Stats:

> MMU_STATS SHOWCOUNTS=1

```yml
Consumption counters:
Count cutter_blade: 568 (limit 4000)
```

### Deleting Counters:
Counters are persisted until explicitly deleted. To delete:

> MMU_STATS COUNTER=cutter_blade DELETE=1

> [!NOTE]  
> Happy Hare will likely be adding preset counters (depending MMU type and options) in the future

