# Fake Klipper `klippy/extras/heater_generic.py` for the Happy Hare test harness.
#
# WHY THIS EXISTS AT ALL. Most unfaked sections are harmless: bootstrap.py:187 passes None
# as load_object's default, so a section with no fake module is silently skipped. This one
# is not skippable. VVD and KMS both `select MMU_HAS_HEATER`
# (installer/mmu_types/Kconfig.vvd:8, Kconfig.kms:11) and set PARAM_FILAMENT_HEATER, so
# config/base/mmu_hardware.cfg renders `filament_heater: <unit>_heater` on [mmu_machine] -
# and that is resolved through extras/mmu/mmu_unit.py:145-162 resolve_object_name(), which
# calls load_object with the SENTINEL default and therefore HARD ERRORS:
#
#   Object 'unit1_heater' could not be loaded as a valid heater in [mmu_machine]
#
# So without this module those two machines cannot load at all.
#
# Real Klipper's heater_generic is exactly this two-line hand-off to PrinterHeaters, and
# everything downstream is already faked: MmuEnvironmentManager reads the heater through
# printer.lookup_object(name).get_status() (mmu_environment_manager.py:824-829) and drives
# it with SET_HEATER_TEMPERATURE (:800), which extras/heaters.py registers per heater.
#
# Note the shipped VVD heater carries `min_temp: -100`; Heater.__init__ reads min_temp
# without a bounds check, which is deliberate - the harness is not validating hardware.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


def load_config_prefix(config):
    pheaters = config.get_printer().load_object(config, 'heaters')
    return pheaters.setup_heater(config)
