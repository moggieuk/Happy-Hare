# Happy Hare MMU Software
#
# Install utilities called from Makefile
#   - allows for Config file parsing and jinja templating
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import sys
import argparse
import re
import os
import logging
import subprocess
from jinja2 import Environment, FileSystemLoader

import kconfiglib
from .parser import ConfigBuilder
from .upgrades import Upgrades

# Documented params that are not in templates
supplemental_params = "cad_gate0_pos cad_gate_width cad_bypass_offset cad_last_gate_offset cad_block_width cad_bypass_block_width cad_bypass_block_delta cad_selector_tolerance gate_material gate_color gate_spool_id gate_status gate_filament_name gate_temperature gate_speed_override endless_spool_groups tool_to_gate_map"

# Other legal params that aren't exposed
hidden_params = "serious suppress_kalico_warning test_random_failures test_force_in_print error_dialog_macro error_macro toolhead_homing_macro park_macro save_position_macro restore_position_macro clear_position_macro encoder_dwell encoder_move_step_size gear_buzz_accel"

happy_hare = '\n(\\_/)\n( *,*)\n(")_(") {caption}\n'
unhappy_hare = '\n(\\_/)\n( V,V)\n(")^(") {caption}\n'

LEVEL_NOTICE = 25

HH_CONFIG_FILES_TO_BUILD = [
    "config/base/mmu.cfg",
    "config/base/mmu_hardware.cfg",
    "config/base/mmu_parameters.cfg",
    "config/base/mmu_macro_vars.cfg",
    "config/base/mmu_hardware_unit0.cfg",
    "config/mmu_vars.cfg"
]

HH_CONFIG_INCLUDES_TO_CLEAN = [
    "mmu/optional/mmu_menu.cfg",
    "mmu/optional/mmu_ercf_compat.cfg",
    "mmu/optional/client_macros.cfg",
    "mmu/addons/mmu_erec_cutter.cfg",
    "mmu/addons/blobifier.cfg",
    "mmu/addons/dc_espooler.cfg",
    "mmu_software.cfg",
    "mmu_sequence.cfg",
    "mmu_cut_tip.cfg",
    "mmu_form_tip.cfg",
    "mmu_parameters.cfg",
    "mmu_hardware.cfg",
    "mmu_hardware_unit0.cfg",
    "mmu_filametrix.cfg",
    "mmu.cfg",
    "mmu/base/*.cfg",
    "mmu/addons/*.cfg"
]

# Enhanced representation of Kconfig file
class KConfig(kconfiglib.Kconfig):
    def __init__(self, config_file):
        super(KConfig, self).__init__("installer/Kconfig")
        self.load_config(config_file)

    def load_unit(self, unit_config_file):
        self.load_config(unit_config_file)

    def is_selected(self, choice, value):
        if isinstance(value, list):
            return any(self.is_selected(choice, v) for v in value)
        return self.named_choices[choice].selection.name == value

    def is_enabled(self, sym):
        return self.syms[sym].user_value

    def getint(self, sym):
        return int(self.syms[sym].user_value)

    def get(self, sym):
        """ Get the value of the symbol """
        if sym not in self.syms:
            raise KeyError("Symbol '{}' not found in Kconfig".format(sym))
        return self.syms[sym].user_value

    def as_dict(self):
        """ Return the Kconfig as a dictionary """
        result = {}
        for sym in self.syms:
            if self.syms[sym].user_value is None:
                continue

            # Convert
            if self.syms[sym].type in [kconfiglib.BOOL, kconfiglib.TRISTATE]:
                value = 1 if self.syms[sym].user_value else 0
            else:
                value = self.syms[sym].user_value
                value = value.replace("\\n", "\n")

            if re.match(r".+\d+$", sym):
                split = re.split(r"(\d+$)", sym)
                key = split[0]
                nr = int(split[1])
                if key in result:
                    result[key][nr] = value
                else:
                    result[key] = {nr: value}
            else:
                result[sym] = value
        return result

    def update(self, hhcfg): # PAUL new
        """ Where posssible update the KConfig with the existing config HH Config data """
        # PAUL may need to create mutation methods
        #    sym.set_value(value)
#        """
#        Sets the user value of the symbol.
#
#        Equal in effect to assigning the value to the symbol within a .config
#        file. For bool and tristate symbols, use the 'assignable' attribute to
#        check which values can currently be assigned. Setting values outside
#        'assignable' will cause Symbol.user_value to differ from
#        Symbol.str/tri_value (be truncated down or up).
#
#        Setting a choice symbol to 2 (y) sets Choice.user_selection to the
#        choice symbol in addition to setting Symbol.user_value.
#        Choice.user_selection is considered when the choice is in y mode (the
#        "normal" mode).
#
#        Other symbols that depend (possibly indirectly) on this symbol are
#        automatically recalculated to reflect the assigned value.
#
#        value:
#          The user value to give to the symbol. For bool and tristate symbols,
#          n/m/y can be specified either as 0/1/2 (the usual format for tristate
#          values in Kconfiglib) or as one of the strings "n", "m", or "y". For
#          other symbol types, pass a string.
#        
#          Note that the value for an int/hex symbol is passed as a string, e.g.
#          "123" or "0x0123". The format of this string is preserved in the
#          output.
#
#          Values that are invalid for the type (such as "foo" or 1 (m) for a
#          BOOL or "0x123" for an INT) are ignored and won't be stored in
#          Symbol.user_value. Kconfiglib will print a warning by default for
#          invalid assignments, and set_value() will return False.
#
#        Returns True if the value is valid for the type of the symbol, and
#        False otherwise. This only looks at the form of the value. For BOOL and
#        TRISTATE symbols, check the Symbol.assignable attribute to see what
#        values are currently in range and would actually be reflected in the
#        value of the symbol. For other symbol types, check whether the
#        visibility is non-n.
#        """
        pass # PAUL TODO

# PAUL write_config exists in super class
#    def write_config(self, filename=None, header=None, save_old=True,
#                     verbose=None):
#    def write(self): # PAUL new
#        def print_node(node, buffer, _):
#            buffer += node.serialize()
#            return True, buffer
#
#        return self.document.walk(print_node, "")
#        pass # PAUL TODO



class HHConfig(ConfigBuilder):
    def __init__(self, cfg_files):
        super(HHConfig, self).__init__()
        self.origins = {}
        self.used_options = set()
        prefix = os.path.commonprefix(cfg_files)
        for cfg_file in cfg_files:
            logging.debug(" > Reading config file: " + cfg_file)
            basename = cfg_file.replace(prefix, "")
            super(HHConfig, self).read(cfg_file)
            logging.debug(" > PAUL: sections: %s" % self.sections())
            for section in self.sections():
                for option in self.options(section):
                    if (section, option) not in self.origins:
                        self.origins[(section, option)] = basename

    def remove_option(self, section_name, option_name):
        if (section_name, option_name) in self.origins:
            self.origins.pop((section_name, option_name))
        return super(HHConfig, self).remove_option(section_name, option_name)

    def remove_section(self, section_name):
        for sec, option in self.origins.items():
            if sec == section_name:
                self.origins.pop((sec, option))
        return super(HHConfig, self).remove_section(section_name)

    def move_option(self, old_section_name, old_option_name, new_section_name, new_option_name=None):
        if new_option_name is None:
            new_option_name = old_option_name
        if self.has_option(old_section_name, old_option_name):
            self.set(new_section_name, new_option_name, self.get(old_section_name, old_option_name))
            self.origins.pop((old_section_name, old_option_name))
            self.remove_option(old_section_name, old_option_name)

    def rename_option(self, section_name, option_name, new_option_name):
        self.move_option(section_name, option_name, section_name, new_option_name)

    def rename_section(self, section_name, new_section_name):
        if self.has_section(section_name):
            self.add_section(new_section_name)
            for option, value in self.items(section_name):
                self.set(new_section_name, option, value)
                self.origins.pop((section_name, option))
            self.remove_section(section_name)

    def update_builder(self, builder):
        """ Update the builder config with the existing config data """
        for section in builder.sections():
            for option in builder.options(section):
                if self.has_option(section, option):
                    if not option.startswith("gcode"):  # Don't ever resuse the gcode
                        builder.set(section, option, self.get(section, option))
                    self.used_options.add((section, option))

    def unused_options_for(self, origin):
        return [
            (section, key)
            for (section, key), file in self.origins.items()
            if file == origin and (section, key) not in self.used_options
        ]


def build_mmu_parameters_cfg(builder, hhcfg):
    for param in supplemental_params.split() + hidden_params.split():
        if hhcfg.has_option("mmu", param):
            builder.buf += param + ": " + hhcfg.get("mmu", param) + "\n"
            hhcfg.remove_option("mmu", param)


def jinja_env():
    return Environment(
        loader=FileSystemLoader("."),
        block_start_string="[%",
        block_end_string="%]",
        variable_start_string="[[",
        variable_end_string="]]",
        comment_start_string="[#",
        comment_end_string="#]",
        trim_blocks=True,
        line_comment_prefix=";;",
    )


def render_template(template_file, kcfg, extra_params):
    """ Render the template config file after expanding KConfig params (and extra params) dictionary """
    env = jinja_env()
    template = env.get_template(os.path.relpath(template_file))
    params = kcfg.as_dict()
    params.update(extra_params)
    return template.render(params)


def build(cfg_file, dest_file, kconfig_file, input_files):
    cfg_file_basename = cfg_file[len(os.getenv("SRC")) + 1 :]

    if (
        not cfg_file_basename.startswith("config/addons/") or not cfg_file_basename.endswith("_hw.cfg")
    ) and cfg_file_basename not in HH_CONFIG_FILES_TO_BUILD: # PAUL: CONFIG_FILES_TO_BUILD could be the same as input_files!
        logging.debug("Skipping build of %s" % cfg_file)
        return

    kcfg = KConfig(kconfig_file)
    extra_params = dict()
    unit_kcfgs = dict()

# PAUL .... fix me vvv
    if kcfg.is_enabled("MULTI_UNIT_ENTRY_POINT"):
        total_num_gates = 0
        for unit in kcfg.get("PARAM_MMU_UNITS").split(","):
            unit = unit.strip()
            unit_kcfgs[unit] = KConfig(kconfig_file + "." + unit)
            total_num_gates += unit_kcfgs[unit].getint("PARAM_NUM_GATES")

        # Total sum of gates for all units
        extra_params["PARAM_TOTAL_NUM_GATES"] = total_num_gates
    else:
        extra_params["PARAM_TOTAL_NUM_GATES"] = kcfg.getint("PARAM_NUM_GATES")
# PAUL .... ^^^

    build_config_file(cfg_file_basename, dest_file, kcfg, input_files, extra_params)


def build_config_file(cfg_file_basename, dest_file, kcfg, input_files, extra_params):
    dest_file_basename = dest_file[len(os.getenv("OUT")) + 1 :]
    logging.info("Building config file: " + dest_file_basename)
    logging.info(dest_file)

    # 1.Generate an aggregated master HH Config for all HH input_files
    hhcfg = HHConfig(input_files)

    # 2.Run upgrade transform on aggregated master HH Config
    to_version = kcfg.get("PARAM_HAPPY_HARE_VERSION")
    if hhcfg.has_option("mmu", "happy_hare_version"):
        from_version = hhcfg.get("mmu", "happy_hare_version")
    else:
        from_version = to_version

    if from_version != to_version:
        logging.debug("Upgrading {} from v{} to v{}".format(cfg_file, from_version, to_version))
        upgrades = Upgrades()
        upgrades.upgrade(hhcfg, from_version, to_version)

# PAUL what if I updated the kcfg parameters here.. if exists in hhcfg then update value (or separate task called at end of make)

    # 3.Render cfg template expanding KConfig parameters
    buffer = render_template(cfg_file_basename, kcfg, extra_params)

    # 4.Generate builder Config from rendered template
    builder = ConfigBuilder()
    builder.read_buf(buffer)

    # 5.Special case mmu_parameters.cfg because it map contain hidden and supplemental options not present in cfg template
    if cfg_file_basename == "config/base/mmu_parameters.cfg":
        build_mmu_parameters_cfg(builder, hhcfg)

    # 6.Update the builder Config from the existing master HH Config to ensure user edits are preserved
    #   This will ignore any section after the EXCLUDE magic marker
    hhcfg.update_builder(builder)
#    hhcfg.pretty_print_document() # PAUL TEMP

    # 7.Report on deprecated/unused options
    first = True
    for section, option in hhcfg.unused_options_for(cfg_file_basename):
        if first:
            first = False
            logging.warning("The following parameters in {} have been dropped:".format(dest_file_basename))
        logging.warning("[{}] {}: {}".format(section, option, hhcfg.get(section, option)))

    # 8.Write builder Config to destination cfg file
    if os.path.islink(dest_file):
        os.remove(dest_file)

    if sys.version_info[0] < 3: # Python 2
        with open(dest_file, "w") as f:
            f.write(builder.write().encode("utf-8"))
    else: # Python 3
        with open(dest_file, "w", encoding="utf-8") as f:
            f.write(builder.write())

# PAUL the below should replace the above and work on py2 and py3. TEST IT
#    data = builder.write()
#    with open(dest_file, "wb") as f:
#        f.write(data.encode("utf-8"))



def install_moonraker(moonraker_cfg, existing_cfg, kconfig):
    logging.info("Checking for moonraker.conf additions")

    kcfg = KConfig(kconfig)
    buffer = render_template(moonraker_cfg, kcfg, {})
    update = ConfigBuilder()
    update.read_buf(buffer)
    builder = ConfigBuilder(existing_cfg)

    def update_section(section):
        if not builder.has_section(section):
            logging.debug(" > Adding [{}]".format(section))
            builder.add_section(section)

        for option, value in update.items(section):
            if not builder.has_option(section, option):
                logging.debug(" > Adding [{}] {} = {}".format(section, option, value))
                builder.set(section, option, value)

    update_section("update_manager happy-hare")
    update_section("mmu_server")

    with open(existing_cfg, "w") as f:
        f.write(builder.write())


def uninstall_moonraker(moonraker_cfg):
    # May not be complete path if config already deleted so ignore
    if not os.path.isfile(moonraker_cfg):
        return

    logging.info("Cleaning up moonraker.conf additions")
    builder = ConfigBuilder(moonraker_cfg)

    if builder.has_section("update_manager happy-hare"):
        logging.debug(" > Removing [update_manager happy-hare]")
        builder.remove_section("update_manager happy-hare")

    if builder.has_section("mmu_server"):
        logging.debug(" > Removing [mmu_server]")
        builder.remove_section("mmu_server")

    with open(moonraker_cfg, "w") as f:
        f.write(builder.write())


def install_includes(dest_file, kconfig):
    logging.info("Checking for printer.cfg includes")

    kcfg = KConfig(kconfig)
    builder = ConfigBuilder(dest_file)

    def check_include(builder, param, include):
        include = "include " + include
        if kcfg.is_enabled(param):
            if not builder.has_section(include):
                logging.debug(" > Adding include [{}]".format(include))
                builder.add_section(include, at_top=True, extra_newline=False)
        else:
            if builder.has_section(include):
                logging.debug(" > Removing include [{}]".format(include))
                builder.remove_section(include)

    check_include(builder, "INSTALL_12864_MENU", "mmu/optional/mmu_menu.cfg")
    check_include(builder, "INSTALL_CLIENT_MACROS", "mmu/optional/client_macros.cfg")
    check_include(builder, "ADDON_EREC_CUTTER", "mmu/addons/mmu_erec_cutter.cfg")
    check_include(builder, "ADDON_BLOBIFIER", "mmu/addons/blobifier.cfg")
    check_include(builder, "ADDON_EJECT_BUTTONS", "mmu/addons/mmu_eject_buttons.cfg")

    if not builder.has_section("include mmu/base/*.cfg"):
        logging.debug(" > Adding include [include mmu/base/*.cfg]")
        builder.add_section("include mmu/base/*.cfg", at_top=True, extra_newline=False)

    with open(dest_file, "w") as f:
        f.write(builder.write())


def uninstall_includes(dest_file):
    # May not be complete path if config already deleted so ignore
    if not os.path.isfile(dest_file):
        return

    logging.info("Cleaning up includes")
    builder = ConfigBuilder(dest_file)
    for include in HH_CONFIG_INCLUDES_TO_CLEAN:
        if builder.has_section("include " + include):
            logging.debug(" > Removing include [{}]".format(include))
            builder.remove_section("include " + include)

    with open(dest_file, "w") as f:
        f.write(builder.write())


def restart_service(name, service, kconfig):
    if not service:
        logging.warning("No {name} service specified - Please restart manually")
    else:
        logging.info("Restarting {}...".format(name))

    kcfg = KConfig(kconfig)
    if kcfg.is_enabled("INIT_SYSTEMD"):
        if not service.endswith(".service"):
            service = service + ".service"
        if subprocess.call("systemctl list-unit-files '{}'".format(service), stdout=open(os.devnull, "w"), shell=True):
            logging.warning("Service '{}' not found! Restart manually or check your config".format(service))
        else:
            subprocess.call("sudo systemctl restart '{}'".format(service), shell=True)
    else:
        if os.path.exists("/etc/init.d/" + service):
            subprocess.call("/etc/init.d/{} restart".format(service), shell=True)
        else:
            logging.warning("Service '/etc/init.d/{}' not found! Restart manually or check your config".format(service))


def check_version(kconfig_file, input_files):
    hhcfg = HHConfig(input_files)
    kcfg = KConfig(kconfig_file)

    current_version = hhcfg.get("mmu", "happy_hare_version")
    if current_version is None:
        logging.log(LEVEL_NOTICE, "Fresh install detected")
        return

    logging.log(LEVEL_NOTICE, "Current version: " + current_version)
    target_version = kcfg.get("PARAM_HAPPY_HARE_VERSION")
    if target_version is None:
        logging.error("Target version is not defined")
        exit(1)

    if current_version == target_version:
        logging.log(LEVEL_NOTICE, "Up to date, no upgrades required")
        return

    if float(current_version) > float(target_version):
        logging.warning(
            "Automatic 'downgrade' to earlier version is not guaranteed!\n"
            "If you encounter startup problems you may need to manually compare "
            "the backed-up 'mmu_parameters.cfg' with current one to restore differences"
        )
        return

    logging.log(LEVEL_NOTICE, "Trying to upgrade to " + target_version)


def main():
    logging.addLevelName(logging.DEBUG, os.getenv("C_DEBUG", ""))
    logging.addLevelName(logging.INFO, os.getenv("C_INFO", ""))
    logging.addLevelName(LEVEL_NOTICE, os.getenv("C_NOTICE", ""))
    logging.addLevelName(logging.WARNING, os.getenv("C_WARNING", ""))
    logging.addLevelName(logging.ERROR, os.getenv("C_ERROR", ""))
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s%(message)s" + os.getenv("C_OFF", ""))

    parser = argparse.ArgumentParser(description="Build script")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-b", "--build", nargs="*")
    parser.add_argument("--check-version", nargs="*")
    parser.add_argument("--print-happy-hare", nargs="?")
    parser.add_argument("--print-unhappy-hare", nargs="?")
    parser.add_argument("--install-moonraker", nargs=3)
    parser.add_argument("--uninstall-moonraker", nargs=1)
    parser.add_argument("--install-includes", nargs=2)
    parser.add_argument("--uninstall-includes", nargs=1)
    parser.add_argument("--restart-service", nargs=3)
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    if args.build:
        build(args.build[0], args.build[1], args.build[2], args.build[3:])

    if args.print_happy_hare:
        logging.log(LEVEL_NOTICE, happy_hare.format(caption=args.print_happy_hare))
    if args.print_unhappy_hare:
        logging.log(LEVEL_NOTICE, unhappy_hare.format(caption=args.print_unhappy_hare))

    if args.install_moonraker:
        install_moonraker(args.install_moonraker[0], args.install_moonraker[1], args.install_moonraker[2])
    if args.uninstall_moonraker:
        uninstall_moonraker(args.uninstall_moonraker[0])

    if args.install_includes:
        install_includes(args.install_includes[0], args.install_includes[1])
    if args.uninstall_includes:
        uninstall_includes(args.uninstall_includes[0])

    if args.restart_service:
        restart_service(args.restart_service[0], args.restart_service[1], args.restart_service[2])

    if args.check_version:
        check_version(args.check_version[0], args.check_version[1:])


if __name__ == "__main__":
    main()
