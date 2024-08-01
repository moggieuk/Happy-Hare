<p align="center">
  <img src="https://github.com/moggieuk/Happy-Hare/wiki/resources/happy_hare_logo.jpg" alt='Happy Hare' width='30%'>
  <h1 align="center">Happy Hare</h1>
</p>

<p align="center">
Universal MMU driver for Klipper
</p>

<p align="center">
  <!--
  <a aria-label="Downloads" href="https://github.com/moggieuk/Happy-Hare/releases">
    <img src="https://img.shields.io/github/release/moggieuk/Happy-Hare?display_name=tag&style=flat-square">
  </a>
-->
  <a aria-label="Stars" href="https://github.com/moggieuk/Happy-Hare/stargazers">
    <img src="https://img.shields.io/github/stars/moggieuk/Happy-Hare?style=flat-square"></a> &nbsp;
  <a aria-label="Forks" href="https://github.com/moggieuk/Happy-Hare/network/members">
    <img src="https://img.shields.io/github/forks/moggieuk/Happy-Hare?style=flat-square"></a> &nbsp;
  <a aria-label="License" href="https://github.com/moggieuk/Happy-Hare/blob/master/LICENSE">
    <img src="https://img.shields.io/github/license/moggieuk/Happy-Hare?style=flat-square"></a> &nbsp;
  <a aria-label="Commits" href="">
    <img src="https://img.shields.io/github/commit-activity/y/moggieuk/Happy-Hare"></a> &nbsp;
</p>

Happy Hare is the second edition of what started life and as alternative software control for the ERCF v1.1 ecosystem - the original open source filament changer for multi-colored printing. However it has now been rearchitected to support most types of MMU's connected to the Klipper ecosystem. That includes **ERCF**, **Tradrack**, AMS-style and other custom designs. It has extensive configuration to allow for customization and using the installer simplifies initial setup for common MMU types. The three conceptual types of MMUs and the function and operation of their various sensors can be [found here](https://github.com/moggieuk/Happy-Hare/wiki/Conceptual-MMU) and should be consulted for any customized setup.  It is best partnered with [KlipperScreen for Happy Hare](https://github.com/moggieuk/KlipperScreen-Happy-Hare-Edition) projet at least until the Mainsail integration is complete :-)

Some folks have asked about making a donation to cover the cost of the all the coffee I'm drinking (actually it's been G&T lately!). Although I'm not doing this for any financial reward this is a BIG undertaking (9000 lines of python, 5000 lines of doc, 4000 lines of macros/config). I have put hundreds of hours into this project and if you find value and feel inclined a donation to PayPal https://www.paypal.me/moggieuk will certainly be spent making your life with your favorate MMU more enjoyable. Thank you!
<p align="center"><a href="https://www.paypal.me/moggieuk"><img src="https://github.com/moggieuk/Happy-Hare/wiki/resources/donate.svg" width="35%"></a></p>

<br>

## ![#f03c15](https://github.com/moggieuk/Happy-Hare/wiki/resources/f03c15.png) ![#c5f015](https://github.com/moggieuk/Happy-Hare/wiki/resources/c5f015.png) ![#1589F0](https://github.com/moggieuk/Happy-Hare/wiki/resources/1589F0.png) A few of the features:

- Support any brand of MMU and user defined monsters (ERCF 1.1, 2.0, Tradrack, Custom.  Prusa & KMS coming very soon)
- Synchronized movement of extruder and gear motors (with feedback control) to overcome friction and even work with FLEX materials!
- Sophisticated multi-homing options including extruder!
- Implements a Tool-to-Gate mapping so that the physical spool can be mapped to any tool
- EndlessSpool allowing a spool to automatically be mapped and take over from a spool that runs out
- Sophisticated logging options (console and mmu.log file)
- Can define material type and color in each gate for visualization and customized settings (like Pressure Advance)
- Deep Spoolman integration
- Automated calibration for easy setup
- Supports MMU "bypass" gate functionality
- Moonraker update-manager support
- Complete persistence of state and statistics across restarts. That's right you don't even need to home!
- Highly configurable speed control that intelligently takes into account the realities of friction and tugs on the spool
- Optional integrated encoder driver that validates filament movement, runout, clog detection and flow rate verification!
- Vast customization options most of which can be changed and tested at runtime
- Integrated help, testing and soak-testing procedures
- Gcode pre-processor check that all the required tools are avaialble!
- Drives LEDs for functional feed and some bling!
- Built in tip forming and filament cutter support
- Lots more... Detail change log can be found in the [Wiki](https://github.com/moggieuk/Happy-Hare/wiki/Change-Log)

Controlling an ERCF with companion [customized KlipperScreen](https://github.com/moggieuk/Happy-Hare/wiki/Basic-Operation#---klipperscreen-happy-hare) for easy touchscreen MMU control!

<p align="center"><img src="https://github.com/moggieuk/Happy-Hare/wiki/resources/my_klipperscreen.png" width="600" alt="KlipperScreen-Happy Hare edition"></p>

<br>
 
## ![#f03c15](https://github.com/moggieuk/Happy-Hare/wiki/resources/f03c15.png) ![#c5f015](https://github.com/moggieuk/Happy-Hare/wiki/resources/c5f015.png) ![#1589F0](https://github.com/moggieuk/Happy-Hare/wiki/resources/1589F0.png) Installation

The module can be installed into an existing Klipper setup with the supplied install script. Once installed it will be added to Moonraker update-manager to easy updates like other Klipper plugins. Full installation documentation is in the [Wiki](https://github.com/moggieuk/Happy-Hare/wiki/Home) but start with cloning the repo onto your rpi:

```
cd ~
git clone https://github.com/moggieuk/Happy-Hare.git
```

<br>
 
## ![#f03c15](https://github.com/moggieuk/Happy-Hare/wiki/resources/f03c15.png) ![#c5f015](https://github.com/moggieuk/Happy-Hare/wiki/resources/c5f015.png) ![#1589F0](https://github.com/moggieuk/Happy-Hare/wiki/resources/1589F0.png) Documentation
<table>
<tr>
<td width=30%><a href="https://github.com/moggieuk/Happy-Hare/wiki/Home"><img src="https://github.com/moggieuk/Happy-Hare/wiki/resources/wiki.png" alt="wiki"></a></td>
<td>
MMU's are complexd! Fortunately Happy Hare has elaborate documentation logically organized in the <a href="https://github.com/moggieuk/Happy-Hare/wiki/Home">Wiki</a>

<p><br><p>

**Other Resources:**
<div align="left">
<a href="https://www.youtube.com/watch?v=uaPLuWJBdQU"><img src="https://github.com/moggieuk/Happy-Hare/wiki/resources/youtube1.png" width="30%"></a>
German instructional video created by Crydteam
<!--
    <img src="https://i9.ytimg.com/vi_webp/uaPLuWJBdQU/maxresdefault.webp?v=6522d1a6&sqp=CKycn6kG&rs=AOn4CLBCiHQsjGJ0c8ywvkxy9uWEk_yUXw" 
         alt="Everything Is AWESOME" 
         style="width:50%;">
-->

<p><br>

<a href="https://www.youtube.com/watch?v=FCl5NfQnulg"><img src="https://github.com/moggieuk/Happy-Hare/wiki/resources/youtube2.png" width="30%"></a>
English Happy Hare introduction by Silverback
</div>

</td>
</tr>
</table>

<br>

## ![#f03c15](https://github.com/moggieuk/Happy-Hare/wiki/resources/f03c15.png) ![#c5f015](https://github.com/moggieuk/Happy-Hare/wiki/resources/c5f015.png) ![#1589F0](https://github.com/moggieuk/Happy-Hare/wiki/resources/1589F0.png) Just how good a MMU multi-color prints?

Although the journey to calibrating and setup can be a frustrating one, I wanted to share @igiannakas (ERCFv2 + Orca Slicer + Happy Hare) example prints here.  Click on the image to zoom it. Incredible! :cool: :clap:

<p align="center"><a href="https://github.com/moggieuk/Happy-Hare/wiki/resources/example_mmu_print.png"><img src="https://github.com/moggieuk/Happy-Hare/wiki/resources/example_mmu_print.png" width="800" alt="Example Prints"></a></p>
<br>

## ![#f03c15](https://github.com/moggieuk/Happy-Hare/wiki/resources/f03c15.png) ![#c5f015](https://github.com/moggieuk/Happy-Hare/wiki/resources/c5f015.png) ![#1589F0](https://github.com/moggieuk/Happy-Hare/wiki/resources/1589F0.png) My Testing and Setup:

Most of the development of Happy Hare was done on my trusty ERCF v1.1 setup but as it's grown, so has my collection of MMU's and MCU controllers. Multi-color printing is addictive but can be frustrating during setup and learning. Be patient and use the forums for help!  **But first read the [Wiki](https://github.com/moggieuk/Happy-Hare/wiki/Home)!**

<p align="center"><img src="https://github.com/moggieuk/Happy-Hare/wiki/resources/my_voron_and_ercf.jpg" width="300" alt="My Setup"></p>
<p align="center"><i>
There once was a printer so keen,<br>
To print in red, yellow, and green.<br>
It whirred and it spun,<br>
Mixing colors for fun,<br>
The most vibrant prints ever seen!</br>
</i></p>

---

```yml
  (\_/)
  ( *,*)
  (")_(") Happy Hare Ready
```
