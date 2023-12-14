# Conceptional MMU Designs and how Happy Hare manages them

Basic MMU types supported by Happy Hare and the function of sensors.

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Type-A

<img src="/doc/conceptual/typeA_mmu.png" width="700" alt="Type A MMU">

This is the most common type of MMU used today. The advantage is that it allows for a large number of gates (available filaments) at a low cost because it leverages only two steppers and a servo to complete the selection process. Examples of this design include Voron ERCF and Annex Tradrack.

Examples:
<img src="/doc/conceptual/default_ercf.png" width="350" alt="Default ERCF Design">
<img src="/doc/conceptual/default_tradrack.png" width="350" alt="Default Tradrack Design">

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Type-B

<img src="/doc/conceptual/typeB_mmu.png" width="700" alt="Type B MMU">

The type has been popularized by Bambu Labs and their AMS system. Each gate has a dedicated stepper for loading and unloading and it leverages a filament "combiner" rather than a selector in the Type-A design.  The advantage is in effeciency. The disadvantage is that it is generally limited to a small number of gates <sup>[Technically these units can be cascaded to provide a greater number of gates but the control logic both firmware and electronics quickly become too complex and costly]</sup>.

<!--<img src="/doc/conceptual/default_kms.png" width="300" alt="Default KMS Design">-->

<br>

## ![#f03c15](/doc/f03c15.png) ![#c5f015](/doc/c5f015.png) ![#1589F0](/doc/1589F0.png) Type-C

<img src="/doc/conceptual/typeC_mmu.png" width="700" alt="Type C MMU">

