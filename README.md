<p align="center">
  <img src="https://github.com/moggieuk/Happy-Hare/wiki/resources/happy_hare_logo.jpg" alt='Happy Hare' width='30%'>
  <h1 align="center">Happy Hare</h1>
</p>

<p align="center">
Universal Automated Filament Changer / MMU driver for Klipper
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

Happy Hare is the original open source filament changer for multi-colored printing. It is implemented as a klipper extension for controlling MMU's / AFC's. The philosophy behind it is to have a universal control system that can adapt to whatever MMU to choose - you change MMU, the software will go with you. Currently it fully supports **ERCF**, **Tradrack**, **Box Turtle**, **Angry Beaver**, **Night Owl**, **3MS**, **3D Chameleon**, other custom designs. It has extensive configuration options for customization but also has an tinstaller to help simplify the initial setup for common MMU types. The different conceptual types of MMUs and the function and operation of their various sensors can be [found here](https://github.com/moggieuk/Happy-Hare/wiki/Conceptual-MMU) and should be consulted for any customized setup.  It is best partnered with [KlipperScreen for Happy Hare](https://github.com/moggieuk/KlipperScreen-Happy-Hare-Edition) project at least until the Mainsail integration is complete :-)

Happy Hare is actively being developed and strives to be maticulous about the quality of multi-colored printing learning from many thousands of users over the past 2+ years.  Whilst the experience if very complete today work is continuing in three important areas:
- Addtional MMU support:  Prusa MMU, KMS, Open AMS, Pico MMU and others are in the works
- Multi-MMU support: Type-A MMU designs are easy to scale through additional gates. Type-B designs are generally limited to a max of 4 gates. Therefore you might want to set up multiple (perhaps different MMU's) on the same printer.
- Mainsail/Fluidd plugin: The klipperscreen extension provides a rich user interface but many have requested a similar treatment for Mainsail. The wait will be over soon.

Some folks have asked about making a donation to cover the cost of the all the coffee I'm drinking (actually it's been G&T lately!). Although I'm not doing this for any financial reward this is a BIG undertaking (13000 lines of python, 8500 lines of doc, 5500 lines of macros/config). I have put hundreds of hours into this project and if you find value and feel inclined a donation to PayPal https://www.paypal.me/moggieuk will certainly be spent making your life with your favorate MMU more enjoyable. Thank you!
<p align="center"><a href="https://www.paypal.me/moggieuk"><img src="https://github.com/moggieuk/Happy-Hare/wiki/resources/donate.svg" width="30%"></a></p>

<br>

**Don't forget to join the dedicated Happy Hare forum here: https://discord.gg/aABQUjkZPk**

## ![#f03c15](https://github.com/moggieuk/Happy-Hare/wiki/resources/f03c15.png) ![#c5f015](https://github.com/moggieuk/Happy-Hare/wiki/resources/c5f015.png) ![#1589F0](https://github.com/moggieuk/Happy-Hare/wiki/resources/1589F0.png) A few of the features:

- Support almost any brand of MMU or user defined monsters:
  - ERCF
  - Tradrack
  - Box Turtle
  - Angry Beaver
  - Night Owl
  - 3MS
  - 3D Chameleon
  - Custom...
- Synchronized movement of extruder and gear motors (with sync feedback control) to overcome friction and even work with FLEX materials!
- Support for all type of sensor: pre-gate, post-gear, combiner gate sensors, extruder entry sensors, toolhead sensors
- Full Spoolman integration
- Support for motorized filament buffer systems for rewinding
- Implements a Tool-to-Gate mapping so that the physical spool can be mapped to any tool
- EndlessSpool allowing a spool to automatically be mapped and take over from a spool that runs out
- Sophisticated logging options (console and separate mmu.log file)
- Can define material type and color in each gate for visualization and customized settings (like Pressure Advance)
- Automated calibration for easy setup
- Supports MMU "bypass" gate functionality
- Moonraker update-manager support
- Moonraker gcode pre-parsing to extract important print infomation
- Complete persistence of state and statistics across restarts
- Highly configurable speed control
- Optional integrated encoder driver that validates filament movement, runout, clog detection and flow rate verification!
- Vast customization options most of which can be changed and tested at runtime
- Integrated help, testing and soak-testing procedures
- Gcode pre-processor check that all the required tools are avaialble!
- Drives LEDs for functional feed and some bling!
- Built in tip forming and filament cutter support
- Lots more... Detail change log can be found in the [Wiki](https://github.com/moggieuk/Happy-Hare/wiki/Change-Log)

Controlling my oldest ERCF MMU with companion [customized KlipperScreen](https://github.com/moggieuk/Happy-Hare/wiki/Basic-Operation#---klipperscreen-happy-hare) for easy touchscreen MMU control!

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

Most of the development of Happy Hare was done on my trusty old ERCF v1.1 setup but as it's grown, so has my collection of MMU's and MCU controllers. Multi-color printing is addictive but can be frustrating during setup and learning. Be patient and use the forums for help!  **But first read the [Wiki](https://github.com/moggieuk/Happy-Hare/wiki/Home)!**

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
