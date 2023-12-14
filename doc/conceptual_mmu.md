# Conceptional MMU Designs and how Happy Hare manages them

Basic MMU types supported by Happy Hare and the function of sensors.

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Type-A

<img src="/doc/conceptual/typeA_mmu.png" width="700" alt="Type A MMU">

This is the most common type of MMU used today. The advantage is that it allows for a large number of gates (available filaments) at a low cost because it leverages only two steppers and a servo to complete the selection process. Examples of this design include Voron ERCF and Annex Tradrack.

### Examples:
<img src="/doc/conceptual/default_ercf.png" width="400" alt="Default ERCF Design">  <img src="/doc/conceptual/default_tradrack.png" width="400" alt="Default Tradrack Design">

Many of the sensors in this design are optional, each providing additional capabilities and benefits, but generally any design needs a way to establish a "homing point" near to the gate (for parking filament) and another near or in the extruder (for verification and acurate loading to the nozzle).

### Sensors explained:

**Gate Sensor**
This is a filament switch fitted on the exit of the MMU. It is "shared" in that it is used to provide a homing point for all filaments close to the MMU after they have been selected and are being driven by the filament drive or gear stepper.  The gate sensor can trigger filament runout logic and thus initiate the "EndlessSpool" feature which allows continous printing form an alternative set of spools which are automatically mapped to the original tool number.  The gate filament runout sensor is named `mmu_gate`

**Encoder**
The encoder measures filament movement and provides feedback to Happy Hare primarily for validation purposes.  However for homing it is an alternative to, but can also be combined with, the Gate Sensor and used to establish a reference point at the MMU.  This reference point is used in the subsequent bowden move when loading or as a point from which to measure the parking position in the gate when unloading. Similar to the Gate Sensor, the encoder can trigger filament runout logic and thus initiate the "EndlessSpool" feature which allows continous printing form an alternative set of spools which are automatically mapped to the original tool number.  However it has additional functionality in that it can detect a "clog" condition and automatically pause the print and notify you.  The encoder is visable through the printer object `printer['mmu_encoder mmu_encoder']`

The ERCF design exclusively uses an encoder for both homing and validation.  For the Tradrack design it is optional because a Gate Sensor provides the reference homing point and if added the encoder can provide more reliability and error recovery.

**Extruder (Entry) Sensor**
This optional filament sensor sits right before the extruder entrance and can be used in several ways: (i) it can provide a homing point at the end of the long bowden move prior to loading the extruder, (ii) if provides extra feedback that the filament is right at the extruder entrance. Named `mmu_extruder`

**Toolhead Sensor**
This sensor sits after the extruder entrance but before the start of the hotend.  It is probably the most useful of all sensors because it provides an accurate reference homing point after the problematic extruder entrace a short way from the nozzle. It also provides extremely useful feedback on the presence of a filament inside the extruder and thus detections of error conditions like stuck filament. This is a highly recommended sensor. Named `mmu_toolhead`

**"Virtual Sensors"**
Note shown in the diagram there are several "virtual sensors" that are implemented by Happy Hare:
- If TMC stallguard is available and configured Happy Hare can sense the filament hitting the extruder entrance.  This therefore acts as an extruder homing point. Endstop is named `mmu_gear_touch`
- If an encoder is available, Happy Hare can sense the lack of movement as another way to sense hitting the extruder entrance, again acting as a reference homing point. Endstop is named `collision`
- If TMC stallguard is configured on the extruder stepper (yes, that's possible with Happy Hare), then an Endstop named `mmu_ext_touch` is available.


**Pre-gate Sensor**
Pre-gate sensors sit just prior to the entry of the filament into the MMU.  They could physically be part of the MMU or mounted to the buffer system.  They have a few functions:
- If the MMU is idle and a filament is inserted and triggers a pre-gate sensor, the selector will move to that gate and preload the filament and correctly park in the gate
- Regardless of whether the MMU is busy or not the insertion or removal of the filament will update the gate_status and adjust status LEDs if fitted thus retaining knowledge of the availability of a particular tool/gate
- If "EndlessSpool" is enabled, this sensor can also act as a early runout sensor and automatically unload, map tool to an alternative gate, re-load and continue printing. This is a highly reliable from of continuous printing because the potentially kinked end of the filament is kept out of the MMU mechanisms.

<br>

For completeness, endstops are created on the selector. Typically there are two that are relevant (although you can add your own):
- The physical selector homing endstop is called `mmu_sel_home`
- If TMC stallguard is configured an additional `mmu_sel_touch` is available. One internal use is detecting the limit of selector travel during setup and calibration

<br>

Complete set of default Happy Hare endstops and filament sensors:<br>
<img src="/doc/filament_sensors.jpg" width="300" alt="Filament Sensors"> <img src="/doc/endstops.jpg" width="300" alt="Endstops">

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Type-B

<img src="/doc/conceptual/typeB_mmu.png" width="700" alt="Type B MMU">

The type has been popularized by Bambu Labs and their AMS system. Each gate has a dedicated stepper for loading and unloading and it leverages a filament "combiner" rather than a selector in the Type-A design.  The advantage is in effeciency. The disadvantage is that it is generally limited to a small number of gates. _[Technically these units can be cascaded to provide a greater number of gates but the control logic both firmware and electronics quickly become too complex and costly]_

<!--<img src="/doc/conceptual/default_kms.png" width="300" alt="Default KMS Design">-->

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Type-C

<img src="/doc/conceptual/typeC_mmu.png" width="700" alt="Type C MMU">

The type is more theoretical at this point - I'm not aware of any designs that take this approach.  It would eliminate the gate limitations of a filament "combiner" to allow for large gate arrays and thus simplify the controlling logic. It still suffers from the need for a large number of stepper motors.
