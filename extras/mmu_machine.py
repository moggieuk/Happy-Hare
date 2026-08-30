# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Definition of logical MMU
#   - checks for upgrade need
#   - allows for specification and aggregation of multiple mmu_units
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging

# Happy Hare imports. Guarded because a Happy Hare update pulled in without re-running
# install.sh (e.g. via Moonraker's update_manager) leaves these new/renamed modules
# unlinked in klippy/extras, so the import itself is the first thing to fail. Report
# that failure as a clean config.error from load_config() instead of letting Klipper
# surface a raw ImportError.
_IMPORT_ERROR = None
try:
    from .mmu                        import mmu_unit
    from .mmu.mmu_unit               import MmuUnit
    from .mmu.mmu_constants          import *
    from .mmu.mmu_utils              import SaveVariableManager
    from .mmu.mmu_sensor_utils       import MmuSensorFactory
    from .mmu.mmu_machine_parameters import MmuMachineParameters
    from .mmu.mmu_controller         import MmuController
except Exception as e:
    _IMPORT_ERROR = e

# VERSION/UPGRADE_REMINDER come from the guarded import above, so the failure messages
# below can't reference them - keep these literal and self-contained.
_NOT_INSTALLED_MSG = (
    "Happy Hare's Klipper modules failed to load (%s).\n"
    "This looks like it is because of a update to Happy Hare v4 while you are still configured for v3\n"
    "To see options please run:\n"
    "  cd ~/Happy-Hare && ./install.sh\n"
    "If you know now you just want to stay on v3, run:\n"
    "  cd ~/Happy-Hare && ./install.sh -b v3\n"
    "More details: https://moggieuk.github.io/Happy-Hare-Doc/Upgrade-v3-v4/"
)

# Same failure, different cause: an already-v4 install whose klippy/extras symlinks
# were wiped out from under it (a "hard" Klipper update is the usual culprit - see
# install.sh's own -f flag). No config migration needed here, just re-linking.
_BROKEN_SYMLINKS_MSG = (
    "Happy Hare's Klipper modules failed to load (%s).\n"
    "This usually means a Klipper update wiped the extras/ symlinks Happy Hare needs.\n"
    "Please run:\n"
    "  cd ~/Happy-Hare && ./install.sh -f\n"
    "to restore them. If that doesn't help, run ./install.sh to reinstall properly."
)


def _looks_like_v3(config):
    """
    True if the parsed config still carries a [mmu] section - v3's home for
    happy_hare_version. A v4 config never has one (replaced by [mmu_machine] /
    [mmu_parameters]), so its presence means this printer.cfg (and the mmu/*.cfg it
    includes) predates the v4 rework, whatever the reason our own imports just failed.
    has_section() reads off the whole parsed config regardless of this wrapper's own
    section, exactly like the has_section('mmu_parameters') check further down.
    """
    return config.has_section('mmu')


class MmuMachine:

    def __init__(self, config):
        self.printer = config.get_printer()
        self.config = config

        def major_minor(version_str):
            """
            Convert "<major>.<minor>.<point>" to tuple (<major>, <minor>)
            """
            major, minor, *_ = version_str.strip('"').split(".")
            return (int(major), int(minor))

        # Instruct users to re-run ./install.sh if version number changes
        self.happy_hare_version = config.get('happy_hare_version', None)
        if self.happy_hare_version is None:
            raise self.config.error("Looks like Happy Hare is not installed correctly - cannot find `happy_hare_version` in klipper config")
        elif major_minor(self.happy_hare_version) < major_minor(VERSION):
            raise self.config.error("Looks like you upgraded (v%s -> v%s)?\n%s" % (self.happy_hare_version, VERSION, UPGRADE_REMINDER))

        self.unit_names = list(config.getlist('units'))
        self.num_units = len(self.unit_names)

        self.num_gates = 0           # Total number gates on system
        self.units = []              # Unit by index
        self.unit_by_name = {}       # Unit lookup by name
        self.unit_by_gate = []       # Quick unit lookup by gate
        self.machine_status = {}     # Aggregated static status
        self.unit_with_bypass = None # Unit with selectable bypass (only one allowed)

        logging.info("MMU: Loaded [%s]" % config.get_name())

        self.machine_status["happy_hare_version"] = self.happy_hare_version

        for i, name in enumerate(self.unit_names):
            section = "mmu_unit %s" % name
            logging.info("MMU: Building mmu_unit #%d [%s] ---------------------------" % (i, section))

            if not config.has_section(section):
                raise config.error("Expected [%s] section not found" % section)
            c = config.getsection(section)
            unit = MmuUnit(c, self, i, self.num_gates)
            logging.info("MMU: Created: [%s]" % c.get_name())

            self.units.append(unit)
            self.unit_by_name[name] = unit
            self.unit_by_gate[self.num_gates:self.num_gates + unit.num_gates] = [unit] * unit.num_gates
            self.machine_status["unit_%d" % i] = unit.get_status(0)
            if unit.show_bypass:
                logging.info(f"MMU: Unit with bypass: {unit.name}")
                if self.unit_with_bypass is not None:
                    raise config.error("Only one mmu_unit can show bypass or have bypass gate. Configured on %s and %s" % (self.unit_with_bypass.name, unit.name))
                self.unit_with_bypass = unit

            self.num_gates += unit.num_gates

        self.machine_status['num_units'] = self.num_units
        self.machine_status['num_gates'] = self.num_gates

        # Load parameters config for mmu machine
        if not config.has_section('mmu_parameters'):
            raise config.error("Expected [mmu_parameters] section not found")
        c = config.getsection('mmu_parameters')
        self.params = MmuMachineParameters(c, self)
        logging.info("MMU: Read: [%s]" % c.get_name())

        # Create master mmu operations
        self.mmu_controller = self.mmu = MmuController(c, self)
        self.printer.add_object('mmu', self.mmu_controller) # Register with klipper for get_status() under legacy name
        logging.info("MMU: Created MmuController")

        # Create efficient and namespaced save variable management
        self.var_manager = SaveVariableManager(c, self)
        logging.info("MMU: Created SaveVariableManager")


    def reinit(self):
        for unit in self.units:
            unit.reinit()


    def get_mmu_unit_by_index(self, index):
        if index is not None and 0 <= index < self.num_units:
            return self.units[index]
        return None


    def get_mmu_unit_by_gate(self, gate):
        if gate >= 0 and gate < self.num_gates:
            return self.unit_by_gate[gate]
        if gate == TOOL_GATE_BYPASS:
            return self.unit_with_bypass
        return None


    def get_mmu_unit_by_name(self, name):
        return self.unit_by_name.get(name, None)


    def get_status(self, eventtime):
        return self.machine_status


def load_config(config):
    if _IMPORT_ERROR is not None:
        # v3 takes precedence: an old config always means "you upgraded without
        # reinstalling", even if the extras/ symlinks also happen to be stale.
        if _looks_like_v3(config):
            raise config.error(_NOT_INSTALLED_MSG % str(_IMPORT_ERROR))
        raise config.error(_BROKEN_SYMLINKS_MSG % str(_IMPORT_ERROR))
    return MmuMachine(config)
