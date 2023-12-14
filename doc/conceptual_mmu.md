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
This is a filament switch fitted on the exit of the MMU. It is "shared" in that it is used to provide a homing point for all filaments close to the MMU after they have been selected and are being driven by the filament drive or gear stepper.

**Encoder**
The encoder measures filament movement and provides feedback to Happy Hare primarily for validation purposes.  However for homing it is an alternative to, but can also be combined with, the Gate Sensor and used to establish a reference point at the MMU.  This reference point is used in the subsequent bowden move when loading or as a point from which to measure the parking position in the gate when unloading.

The ERCF design exclusively uses an encoder for both homing and validation.  For the Tradrack design it is optional because a Gate Sensor provides the reference homing point and if added the encoder can provide more reliability and error recovery.

**Extruder (Entry) Sensor**
This optional filament sensor sits right before the extruder entrance and can be used in several ways: (i) it can provide a homing point at the end of the long bowden move prior to loading the extruder, (ii) if provides extra feedback that the filament is right at the extruder entrance.

**Toolhead Sensor**
This sensor sits after the extruder entrance but before the start of the hotend.  It is probably the most useful of all sensors because it provides an accurate reference homing point after the problematic extruder entrace a short way from the nozzle. It also provides extremely useful feedback on the presence of a filament inside the extruder and thus detections of error conditions like stuck filament. This is a highly recommended sensor.

**Virtual Sensors**


**Pre-gate Sensor**
...

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Type-B

<img src="/doc/conceptual/typeB_mmu.png" width="700" alt="Type B MMU">

The type has been popularized by Bambu Labs and their AMS system. Each gate has a dedicated stepper for loading and unloading and it leverages a filament "combiner" rather than a selector in the Type-A design.  The advantage is in effeciency. The disadvantage is that it is generally limited to a small number of gates. _[Technically these units can be cascaded to provide a greater number of gates but the control logic both firmware and electronics quickly become too complex and costly]_

<!--<img src="/doc/conceptual/default_kms.png" width="300" alt="Default KMS Design">-->

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Type-C

<img src="/doc/conceptual/typeC_mmu.png" width="700" alt="Type C MMU">

The type is more theoretical at this point - I'm not aware of any designs that take this approach.  It would eliminate the gate limitations of a filament "combiner" to allow for large gate arrays and thus simplify the controlling logic. It still suffers from the need for a large number of stepper motors.
