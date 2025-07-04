import sys
import argparse
import re
import os
import logging
import subprocess
from pprint import pprint
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


class KConfig(kconfiglib.Kconfig):
    def __init__(self, confg_file):
        super(KConfig, self).__init__("installer/Kconfig")
        self.load_config(confg_file)

    def is_selected(self, choice, value):
        if isinstance(value, list):
            return any(self.is_selected(choice, v) for v in value)
        return self.named_choices[choice].selection.name == value

    def is_enabled(self, sym):
        return self.syms[sym].user_value

    def getint(self, sym):
        return int(self.syms[sym].user_value)

    def get(self, sym):
        """Get the value of the symbol"""
        if sym not in self.syms:
            return None
        return self.syms[sym].user_value

    def as_dict(self):
        """Return the Kconfig as a dictionary"""
        result = {}
        for sym in self.syms:
            if self.syms[sym].user_value is None:
                continue

            # Convert
            if self.syms[sym].type in [kconfiglib.BOOL, kconfiglib.TRISTATE]:
                value = 1 if self.syms[sym].user_value else 0
            else:
                value = self.syms[sym].user_value

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

        # return {sym: self.syms[sym].user_value for sym in self.syms if self.syms[sym].user_value is not None}


class HHConfig(ConfigBuilder):
    def __init__(self, cfg_files):
        super(HHConfig, self).__init__()
        self.origins = {}
        self.used_options = set()
        # self.document = super(HHConfig, self).document  # because Python's inheritance apparently completly broken
        prefix = os.path.commonprefix(cfg_files)
        for cfg_file in cfg_files:
            logging.info("Reading config file: " + cfg_file)
            basename = cfg_file.replace(prefix, "")
            super(HHConfig, self).read(cfg_file)
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
        """Update the builder config with the existing config file and replace the placeholders with the Kconfig values"""
        for section in builder.sections():
            for option in builder.options(section):
                if self.has_option(section, option):
                    if not option.startswith("gcode"):  # Don't ever resuse the gcode
                        builder.set(section, option, self.get(section, option))
                    self.used_options.add((section, option))
        #
        # if self.kcfg is not None:
        #     for key, sym in self.kcfg.syms.items():
        #         if key.startswith("PARAM_") or key.startswith("PIN_"):
        #             builder.replace_placeholder(key.lower(), sym.user_value or "")

        # for param, value in self.extra_params().items():
        #     builder.replace_placeholder(param, str(value))

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
    env = Environment(
        loader=FileSystemLoader("."),
        block_start_string="[%",
        block_end_string="%]",
        variable_start_string="[[",
        variable_end_string="]]",
        comment_start_string="[#",
        comment_end_string="#]",
        trim_blocks=True,
    )
    return env


def render_template(template_file, kcfg):
    env = jinja_env()
    template = env.get_template(os.path.relpath(template_file))
    return template.render(kcfg.as_dict())


def build(cfg_file, dest_file, kconfig_file, input_files):
    match = re.search(r"config/(.*?)$", cfg_file)
    if match:
        basename = match.group(1)
    else:
        logging.error("Invalid config file: " + cfg_file)
        exit(1)

    if (not basename.startswith("addons/") or not basename.endswith("_hw.cfg")) and basename not in [
        "base/mmu.cfg",
        "base/mmu_hardware.cfg",
        "base/mmu_parameters.cfg",
        "base/mmu_macro_vars.cfg",
        "mmu_vars.cfg",
    ]:
        return
    logging.info("Building config file: " + cfg_file)

    hhcfg = HHConfig(input_files)
    kcfg = KConfig(kconfig_file)

    buffer = render_template("config/" + basename, kcfg)
    builder = ConfigBuilder()
    builder.read_buf(buffer)

    to_version = kcfg.get("PARAM_HAPPY_HARE_VERSION")
    if hhcfg.has_option("mmu", "happy_hare_version"):
        from_version = hhcfg.get("mmu", "happy_hare_version")
    else:
        from_version = to_version

    logging.info("Upgrading from {} to {}".format(from_version, to_version))
    upgrades = Upgrades()
    upgrades.upgrade(hhcfg, from_version, to_version)

    if basename == "base/mmu_parameters.cfg":
        build_mmu_parameters_cfg(builder, hhcfg)

    hhcfg.update_builder(builder)

    first = True
    for section, option in hhcfg.unused_options_for(basename):
        if first:
            first = False
            logging.warning("The following parameters in {} have been dropped:".format(basename))
        logging.warning("[{}] {}: {}".format(section, option, hhcfg.get(section, option)))

    for ph in builder.placeholders():
        logging.warning("Placeholder {{{}}} not replaced".format(ph))

    if os.path.islink(dest_file):
        os.remove(dest_file)

    if sys.version_info[0] < 3: # Python 2
        with open(dest_file, "w") as f:
            f.write(builder.write().encode("utf-8"))
    else: # Python 3
        with open(dest_file, "w", encoding="utf-8") as f:
            f.write(builder.write())

def install_moonraker(moonraker_cfg, existing_cfg, kconfig):
    logging.info("Adding moonraker components")

    kcfg = KConfig(kconfig)
    buffer = render_template(moonraker_cfg, kcfg)
    update = ConfigBuilder()
    update.read_buf(buffer)
    builder = ConfigBuilder(existing_cfg)

    def update_section(section):
        if not builder.has_section(section):
            logging.debug("Adding [{}]".format(section))
            builder.add_section(section)

        for option, value in update.items(section):
            if not builder.has_option(section, option):
                logging.debug("Adding [{}] {} = {}".format(section, option, value))
                builder.set(section, option, value)

    update_section("update_manager happy-hare")
    update_section("mmu_server")

    with open(existing_cfg, "w") as f:
        f.write(builder.write())


def uninstall_moonraker(moonraker_cfg):
    logging.info("Cleaning up moonraker components")
    builder = ConfigBuilder(moonraker_cfg)

    if builder.has_section("update_manager happy-hare"):
        logging.debug("Removing [update_manager happy-hare]")
        builder.remove_section("update_manager happy-hare")

    if builder.has_section("mmu_server"):
        logging.debug("Removing [mmu_server]")
        builder.remove_section("mmu_server")

    with open(moonraker_cfg, "w") as f:
        f.write(builder.write())


def install_includes(dest_file, kconfig):
    logging.info("Configuring includes")
    kcfg = KConfig(kconfig)
    builder = ConfigBuilder(dest_file)

    def check_include(builder, param, include):
        include = "include " + include
        if kcfg.is_enabled(param):
            if not builder.has_section(include):
                logging.debug("Adding include [{}]".format(include))
                builder.add_section(include, at_top=True, extra_newline=False)
        else:
            if builder.has_section(include):
                logging.debug("Removing include [{}]".format(include))
                builder.remove_section(include)

    check_include(builder, "INSTALL_12864_MENU", "mmu/optional/mmu_menu.cfg")
    check_include(builder, "INSTALL_CLIENT_MACROS", "mmu/optional/client_macros.cfg")
    check_include(builder, "ADDON_EREC_CUTTER", "mmu/addons/mmu_erec_cutter.cfg")
    check_include(builder, "ADDON_BLOBIFIER", "mmu/addons/blobifier.cfg")
    check_include(builder, "ADDON_EJECT_BUTTONS", "mmu/addons/mmu_eject_buttons.cfg")

    if not builder.has_section("include mmu/base/*.cfg"):
        logging.debug("Adding include [include mmu/base/*.cfg]")
        builder.add_section("include mmu/base/*.cfg", at_top=True, extra_newline=False)

    with open(dest_file, "w") as f:
        f.write(builder.write())


def uninstall_includes(dest_file):
    logging.info("Cleaning up includes")
    builder = ConfigBuilder(dest_file)
    for include in [
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
        "mmu_filametrix.cfg",
        "mmu.cfg",
        "mmu/base/*.cfg",
        "mmu/addons/*.cfg",
    ]:
        if builder.has_section("include " + include):
            logging.debug("Removing include [{}]".format(include))
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

    if args.build:
        build(args.build[0], args.build[1], args.build[2], args.build[3:])

    if args.print_happy_hare:
        logging.info(happy_hare.format(caption=args.print_happy_hare))
    if args.print_unhappy_hare:
        logging.warning(unhappy_hare.format(caption=args.print_unhappy_hare))

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
