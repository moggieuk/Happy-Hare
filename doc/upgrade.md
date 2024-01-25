# Happy Hare - Upgrade Notice
If you have found this page you are probably experiencing a startup message similar to:

```
Looks like you upgraded (v2.2 -> v2.3)?
Happy Hare minor version has changed which requires you to re-run
'./install.sh' to update configuration files and klipper modules.
More details: https://github.com/moggieuk/Happy-Hare/doc/upgrade.md
```

Happy Hare version as seen in Mainsail & Fluidd UI's is in the form: `Major`.`Minor`.`Point`-`Patch`. The meaning of each number is as follows:
- `Major` - Major change of functionality that requires a complete re-install
- `Minor` - A significant change has been made that requires update to config file or installation of a new Klipper module. './install.sh' must be run and configuration should be checked afterwards
- `Point` - Enhancement that may make a minor change to configuration files.  You will be instructed if you need to rerun './install.sh'
- `Patch` - Routine update to address a bug that doesn require any special treatment other than Klipper restart

The most common is `Minor` change and to fix that you simply need to log into your rpi and run the following:

```
cd ~/Happy-Hare
./install.sh
```

Once run klipper should startup without the upgrade warning.

> [!NOTE]  
> Note that there are no options passed to install.sh unless you need to specify `-c` or `-k` to point to a none standard Klipper install location. Once run klipper should startup without the upgrade warning.<br>
> HH v2.3 requires Klipper 0.12.0 or greater -- Klipper made a breaking change and v2.3 addresses that but is not backward compatable with older versions of klipper

### Detailed Change Log
Can be found [here](/doc/change_log.md)

