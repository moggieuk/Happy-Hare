import argparse
import re
import os
import logging
import subprocess
from pprint import pprint


import kconfiglib
from .parser import ConfigBuilder
from .upgrades import Upgrades

supplemental_params = "cad_gate0_pos cad_gate_width cad_bypass_offset cad_last_gate_offset cad_block_width cad_bypass_block_width cad_bypass_block_delta cad_selector_tolerance gate_material gate_color gate_spool_id gate_status gate_filament_name gate_temperature gate_speed_override endless_spool_groups tool_to_gate_map"
hidden_params = "test_random_failures test_random_failures test_disable_encoder test_force_in_print serious"

happy_hare = '\n(\\_/)\n( *,*)\n(")_(") {caption}\n'
unhappy_hare = '\n(\\_/)\n( V,V)\n(")^(") {caption}\n'

LEVEL_NOTICE = 25


class KConfig(kconfiglib.Kconfig):
    def __init__(self, confg_file):
        super(KConfig, self).__init__("installer/Kconfig")
        self.load_config(confg_file)

    def is_selected(self, choice, value):
        return self.named_choices[choice].selection.name == value

    def is_enabled(self, sym):
        return self.syms[sym].user_value

    def getint(self, sym):
        return int(self.syms[sym].user_value)


class HHConfig(ConfigBuilder):
    def __init__(self, cfg_files):
        super(HHConfig, self).__init__()
        self.origins = {}
        prefix = os.path.commonprefix(cfg_files)
        for cfg_file in cfg_files:
            basename = cfg_file.replace(prefix, "")
            self.read(cfg_file)
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


class ConfigInput:
    def __init__(self, hhcfg, kcfg):
        self.hhcfg = hhcfg
        self.kcfg = kcfg
        self.used_options = set()

    def extra_params(self):
        """Return extra parameters based on the Kconfig, that are impossible to define in the config file"""
        params = {}
        num_gates = self.getint("mmu_machine", "num_gates", "PARAM_NUM_GATES")
        if num_gates is not None:
            params["param_num_leds"] = num_gates * 2 + 1
            params["param_num_leds_minus1"] = num_gates * 2
            params["param_num_gates_plus1"] = num_gates + 1

        config_home = self.get_param("KLIPPER_CONFIG_HOME")
        if config_home is not None:
            params["param_mmu_vars_cfg"] = "{}/mmu/mmu_vars.cfg".format(config_home)

        return params

    def unused_options_for(self, origin):
        return [
            (section, key)
            for (section, key), file in self.hhcfg.origins.items()
            if file == origin and (section, key) not in self.used_options
        ]

    def is_selected(self, choice, value):
        """Check if the choice is selected in the Kconfig file"""
        if self.kcfg is None:
            return False
        return self.kcfg.is_selected(choice, value)

    def is_enabled(self, sym):
        """Check if the symbol is enabled in the Kconfig file"""
        if self.kcfg is None:
            return False
        return self.kcfg.is_enabled(sym)

    def has_option(self, section, key, kcfg_param=None):
        """Check if the option is defined in the existing config file or else check if kcfg_param is defined from the Kconfig file"""
        if self.hhcfg.has_option(section, key):
            return True
        if self.kcfg is None or self.kcfg.syms is None or kcfg_param is None:
            return False
        if kcfg_param not in self.kcfg.syms:
            return False

        return self.kcfg.syms[kcfg_param].user_value is not None

    def get(self, section, key, kcfg_param=None, default=None):
        """Get the value of the option from the existing config file or else get the value of kcfg_param from the Kconfig file
        If neither is defined, return the default value"""
        if self.hhcfg is not None and self.hhcfg.has_option(section, key):
            return self.hhcfg.get(section, key)

        if self.kcfg is None or kcfg_param is None:
            return default

        return self.kcfg.syms[kcfg_param].user_value

    def getint(self, section, key, kcfg_param=None, default=None):
        """Get the value of the option from the existing config file or else get the value of kcfg_param from the Kconfig file
        If neither is defined, return the default value"""
        value = self.get(section, key, kcfg_param, default)
        if value is not None:
            return int(value)
        return default

    def get_param(self, key, default=None):
        """Get the value of the parameter from the Kconfig file"""
        if self.kcfg is None or self.kcfg.syms is None:
            return default
        return self.kcfg.syms[key].user_value

    def getint_param(self, key, default=None):
        """Get the value of the parameter from the Kconfig file"""
        if self.kcfg is None or self.kcfg.syms is None:
            return default
        return int(self.kcfg.syms[key].user_value)

    def getbool_param(self, key, default=None):
        """Get the value of the parameter from the Kconfig file"""
        if self.kcfg is None or self.kcfg.syms is None:
            return default
        return self.kcfg.syms[key].user_value not in [0, None]

    def update_builder(self, builder):
        """Update the builder config with the existing config file and replace the placeholders with the Kconfig values"""
        for section in builder.sections():
            for option in builder.options(section):
                if self.has_option(section, option):
                    if not option.startswith("gcode"):  # Don't ever resuse the gcode
                        builder.set(section, option, self.get(section, option))
                    self.used_options.add((section, option))

        if self.kcfg is not None:
            for key, sym in self.kcfg.syms.items():
                if key.startswith("PARAM_") or key.startswith("PIN_"):
                    builder.replace_placeholder(key.lower(), sym.user_value or "")

        for param, value in self.extra_params().items():
            builder.replace_placeholder(param, str(value))


def build_mmu_cfg(builder, cfg):
    num_gates = cfg.getint("mmu_machine", "num_gates", "PARAM_NUM_GATES")
    if num_gates is None:
        logging.error("num_gates is not defined")
        exit(1)

    builder.expand_value_line("board_pins mmu", "aliases", "MMU_PRE_GATE_%", num_gates)
    builder.expand_value_line("board_pins mmu", "aliases", "MMU_POST_GEAR_%", num_gates)

    if cfg.is_selected("CHOICE_SELECTOR_TYPE", "SELECTOR_TYPE_LINEAR"):
        builder.use_config("selector")
        builder.remove_placeholder("cfg_gears")
    elif cfg.is_selected("CHOICE_SELECTOR_TYPE", "SELECTOR_TYPE_VIRTUAL"):
        builder.use_config("gears")
        builder.remove_placeholder("cfg_selector")
        builder.expand_value_line(
            "board_pins mmu",
            "aliases",
            r"MMU_GEAR_\w+_%",
            num_gates - 1,
            start_idx=1,
        )

    if cfg.is_enabled("MMU_HAS_ENCODER"):
        builder.use_config("encoder")
    else:
        builder.remove_placeholder("cfg_encoder")

    if cfg.is_enabled("INSTALL_DC_ESPOOLER"):
        builder.use_config("dc_espooler")
        builder.expand_value_line("board_pins mmu", "aliases", r"MMU_DC_MOT_%_\w+", num_gates)
    else:
        builder.remove_placeholder("cfg_dc_espooler")

    return builder


def build_mmu_hardware_cfg(builder, cfg):
    num_gates = cfg.getint("mmu_machine", "num_gates", "PARAM_NUM_GATES")
    if num_gates is None:
        logging.error("num_gates is not defined")
        exit(1)

    builder.expand_option("mmu_sensors", "switch_pin_%", num_gates)

    if cfg.is_selected("CHOICE_SELECTOR_TYPE", "SELECTOR_TYPE_LINEAR"):
        builder.use_config("selector_stepper")
        builder.use_config("selector_servo")
        builder.remove_placeholder("cfg_gear_steppers")
    elif cfg.is_selected("CHOICE_SELECTOR_TYPE", "SELECTOR_TYPE_VIRTUAL"):
        builder.remove_placeholder("cfg_selector_stepper")
        builder.remove_placeholder("cfg_selector_servo")
        builder.use_config("gear_steppers")
        builder.expand_section(
            "tmc2209 stepper_mmu_gear_%",
            num_gates - 1,
            start_idx=1,
            newline=True,
        )
        builder.expand_section(
            "stepper_mmu_gear_%",
            num_gates - 1,
            start_idx=1,
            newline=True,
        )

    if cfg.is_selected("CHOICE_BOARD_TYPE", "BOARD_TYPE_EASY_BRD"):
        # Share uart_pin to avoid duplicate alias problem
        builder.set("tmc2209 stepper_mmu_selector", "uart_pin", " mmu:MMU_GEAR_UART")
    else:
        builder.remove_option("tmc2209 stepper_mmu_gear", "uart_address")

    if cfg.is_enabled("ENABLE_LEDS"):
        builder.use_config("leds")
    else:
        builder.remove_placeholder("cfg_leds")

    if cfg.is_enabled("MMU_HAS_ENCODER"):
        builder.use_config("encoder")
    else:
        builder.remove_placeholder("cfg_encoder")

    if cfg.is_enabled("ENABLE_SELECTOR_TOUCH"):
        builder.remove_option("tmc2209 stepper_mmu_gear", "uart_address")
    else:
        builder.comment_out_option("tmc2209 stepper_mmu_gear", "diag_pin")
        builder.comment_out_option("tmc2209 stepper_mmu_gear", "driver_SGTHRS")
        builder.comment_out_option("stepper_mmu_gear", "extra_endstop_pins")
        builder.comment_out_option("stepper_mmu_gear", "extra_endstop_names")


def build_mmu_parameters_cfg(builder, cfg):
    if cfg.is_selected("CHOICE_SELECTOR_TYPE", "SELECTOR_TYPE_LINEAR"):
        builder.use_config("selector_speeds")
        builder.use_config("selector_servo")
        builder.use_config("custom_mmu")
    elif cfg.is_selected("CHOICE_SELECTOR_TYPE", "SELECTOR_TYPE_VIRTUAL"):
        builder.delete_line("sync_to_extruder:")
        builder.delete_line("sync_form_tip:")
        builder.delete_line("preload_attempts:")
        builder.delete_line("gate_load_retries:")
    for param in supplemental_params.split() + hidden_params.split():
        if cfg.hhcfg.has_option("mmu", param):
            builder.buf += param + ": " + cfg.hhcfg.get("mmu", param) + "\n"
            cfg.hhcfg.remove_option("mmu", param)


def build_mmu_macro_vars_cfg(builder, cfg):
    num_gates = cfg.getint("mmu_machine", "num_gates", "PARAM_NUM_GATES")
    if num_gates is None:
        logging.error("num_gates is not defined")
        exit(1)
    builder.expand_section("gcode_macro T%", num_gates)


def build_addon_dc_espooler_cfg(builder, cfg):
    num_gates = cfg.getint("mmu_machine", "num_gates", "PARAM_NUM_GATES")
    if num_gates is None:
        logging.error("num_gates is not defined")
        exit(1)
    builder.expand_section("output_pin _mmu_dc_espooler_rwd_%", num_gates)
    builder.expand_section("output_pin _mmu_dc_espooler_en_%", num_gates)


def build_addon_eject_buttons_cfg(builder, cfg):
    num_gates = cfg.getint("mmu_machine", "num_gates", "PARAM_NUM_GATES")
    if num_gates is None:
        logging.error("num_gates is not defined")
        exit(1)
    builder.expand_section("gcode_button mmu_eject_button_%", num_gates)


def build(cfg_file, dest_file, kconfig_file, input_files):
    match = re.search(r"config/(.*?)$", cfg_file)
    if match:
        basename = match.group(1)
    else:
        logging.error("Invalid config file: " + cfg_file)
        exit(1)

    if not basename.startswith("addons/") and basename not in [
        "base/mmu.cfg",
        "base/mmu_hardware.cfg",
        "base/mmu_parameters.cfg",
        "base/mmu_macro_vars.cfg",
        "mmu_vars.cfg",
    ]:
        return

    logging.info("Building " + dest_file)
    cfg_input = ConfigInput(HHConfig(input_files), KConfig(kconfig_file))
    builder = ConfigBuilder(cfg_file)

    to_version = cfg_input.get_param("PARAM_HAPPY_HARE_VERSION")
    if cfg_input.has_option("mmu", "happy_hare_version"):
        from_version = cfg_input.get("mmu", "happy_hare_version")
    else:
        from_version = to_version

    upgrades = Upgrades()
    upgrades.upgrade(cfg_input, from_version, to_version)

    if basename == "base/mmu.cfg":
        build_mmu_cfg(builder, cfg_input)
    elif basename == "base/mmu_hardware.cfg":
        build_mmu_hardware_cfg(builder, cfg_input)
    elif basename == "base/mmu_parameters.cfg":
        build_mmu_parameters_cfg(builder, cfg_input)
    elif basename == "base/mmu_macro_vars.cfg":
        build_mmu_macro_vars_cfg(builder, cfg_input)
    elif basename == "addons/dc_espooler_hw.cfg":
        build_addon_dc_espooler_cfg(builder, cfg_input)
    elif basename == "addons/mmu_eject_buttons_hw.cfg":
        build_addon_eject_buttons_cfg(builder, cfg_input)

    cfg_input.update_builder(builder)

    first = True
    for section, option in cfg_input.unused_options_for(basename):
        if first:
            first = False
            logging.warning("The following parameters in {} have been dropped:".format(basename))
        logging.warning("[{}] {}: {}".format(section, option, cfg_input.get(section, option)))

    for ph in builder.placeholders():
        logging.warning("Placeholder {{{}}} not replaced".format(ph))

    if os.path.islink(dest_file):
        os.remove(dest_file)

    with open(dest_file, "w") as f:
        f.write(builder.write())


def install_moonraker(moonraker_cfg, existing_cfg, kconfig):
    logging.info("Adding moonraker components")
    cfg_input = ConfigInput(HHConfig([moonraker_cfg]), KConfig(kconfig))
    builder = ConfigBuilder(existing_cfg)

    if not builder.has_section("update_manager happy-hare"):
        logging.debug("Adding [update_manager happy-hare]")
        builder.add_section("update_manager happy-hare")
        for option, value in cfg_input.hhcfg.items("update_manager happy-hare"):
            logging.debug("Adding [update_manager happy-hare] {} = {}".format(option, value))
            builder.set("update_manager happy-hare", option, value)

    if not builder.has_section("mmu_server"):
        builder.add_section("mmu_server")
        for option, value in cfg_input.hhcfg.items("mmu_server"):
            builder.set("mmu_server", option, value)
    else:
        if not builder.has_option("mmu_server", "enable_toolchange_next_pos"):
            builder.set(
                "mmu_server", "enable_toolchange_next_pos", cfg_input.get("mmu_server", "enable_toolchange_next_pos")
            )
        if not builder.has_option("mmu_server", "update_spoolman_location"):
            builder.set(
                "mmu_server", "update_spoolman_location", cfg_input.get("mmu_server", "update_spoolman_location")
            )

    cfg_input.update_builder(builder)
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
    cfg_input = ConfigInput(None, KConfig(kconfig))
    builder = ConfigBuilder(dest_file)

    def check_include(builder, param, include):
        include = "include " + include
        if cfg_input.getbool_param(param, False):
            if not builder.has_section(include):
                logging.debug("Adding include [{}]".format(include))
                builder.add_section(include, at_top=True, extra_newline=False)
        else:
            if builder.has_section(include):
                logging.debug("Removing include [{}]".format(include))
                builder.remove_section(include)

    check_include(builder, "INSTALL_12864_MENU", "mmu/optional/mmu_menu.cfg")
    check_include(builder, "INSTALL_CLIENT_MACROS", "mmu/optional/client_macros.cfg")
    check_include(builder, "INSTALL_EREC_CUTTER", "mmu/addons/mmu_erec_cutter.cfg")
    check_include(builder, "INSTALL_BLOBIFIER", "mmu/addons/blobifier.cfg")
    check_include(builder, "INSTALL_DC_ESPOOLER", "mmu/addons/dc_espooler.cfg")
    check_include(builder, "INSTALL_EJECT_BUTTONS", "mmu/addons/mmu_eject_buttons.cfg")

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

    cfg_input = ConfigInput(None, KConfig(kconfig))
    if cfg_input.is_enabled("INIT_SYSTEMD"):
        if not service.endswith(".service"):
            service = service + ".service"
        if subprocess.call("systemctl list-unit-files '{}'".format(service), stdout=subprocess.DEVNULL, shell=True):
            logging.warning("Service '{}' not found! Restart manually or check your config".format(service))
        else:
            subprocess.call("sudo systemctl restart '{}'".format(service), shell=True)
    else:
        if os.path.exists("/etc/init.d/" + service):
            subprocess.call("/etc/init.d/{} restart".format(service), shell=True)
        else:
            logging.warning("Service '/etc/init.d/{}' not found! Restart manually or check your config".format(service))


def check_version(kconfig_file, input_files):
    cfg_input = ConfigInput(HHConfig(input_files), KConfig(kconfig_file))

    current_version = cfg_input.get("mmu", "happy_hare_version")
    if current_version is None:
        logging.log(LEVEL_NOTICE, "Fresh install detected")
        return

    logging.log(LEVEL_NOTICE, "Current version: " + current_version)
    target_version = cfg_input.get_param("PARAM_HAPPY_HARE_VERSION")
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s%(message)s" + os.getenv("C_OFF", ""))

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
