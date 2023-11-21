# Happy Hare MMU Software
# Temporary stub to replace previous class to remind user to re-run ./install.sh
#
import chelper, logging

class MmuConfigSetup():

    def __init__(self, config):
        raise config.error("Happy Hare upgrade notice:\nRerun `./install.sh` and move your extruder definition back to normal (printer.cfg)!\nRead comments in new mmu_hardware.cfg")

def load_config(config):
    return MmuConfigSetup(config)
