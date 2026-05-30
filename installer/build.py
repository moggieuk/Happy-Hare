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
import copy
import logging
import subprocess
import dill

from jinja2  import Environment, FileSystemLoader, UndefinedError
from pathlib import Path

import kconfiglib
from .parser   import ConfigBuilder, WhitespaceNode
from .upgrades import Upgrades

# Documented params that are not in templates or are commented out.
# This list prevents removal on upgrade/reinstall.
supplemental_params = [
    "cad_gate0_pos",
    "cad_gate_width",
    "cad_bypass_offset",
    "cad_last_gate_offset",
    "cad_block_width",
    "cad_bypass_block_width",
    "cad_bypass_block_delta",
    "cad_selector_tolerance",
    "cad_max_rotations",
    "default_gate_material",
    "default_gate_color",
    "default_gate_spool_id",
    "default_gate_status",
    "default_gate_filament_name",
    "default_gate_temperature",
    "default_gate_speed_override",
    "default_endless_spool_groups",
    "default_ttg_map",
]

# Other legal params that aren't exposed.
# This list prevents removal on upgrade/reinstall if user chooses to use them
hidden_params = [
    "serious",
    "test_random_failures",
    "test_force_in_print",
    "error_dialog_macro",
    "error_macro",
    "toolhead_homing_macro",
    "park_macro",
    "save_position_macro",
    "restore_position_macro",
    "clear_position_macro",
    "encoder_dwell",
    "encoder_move_step_size",
    "gear_buzz_accel",

    # For proportional sync-feedback sensor
    "sync_feedback_analog_gamma",
    "sync_feedback_analog_sample_time",
    "sync_feedback_analog_sample_count"
    "sync_feedback_analog_report_time",

    # From PR to convert Qidi hall-effect filament width sensor into extruder/toolhead sensor
    "hall_sensor_endstop",
    "hall_adc1",
    "hall_adc2",
    "hall_cal_dia1",
    "hall_cal_dia2",
    "hall_raw_dia1",
    "hall_raw_dia2",
    "hall_min_diameter",
    "hall_max_diameter",
]

happy_hare = '\n(\\_/)\n( *,*)\n(")_(") {caption}\n'
unhappy_hare = '\n(\\_/)\n( V,V)\n(")^(") {caption}\n'

LEVEL_NOTICE = 25


class KConfig(kconfiglib.Kconfig):
    """
    Enhanced representation of Kconfig file
    that provides a few convenience methods
    """

    def __init__(self, config_file):
        super(KConfig, self).__init__("Kconfig")
        self.load_config(config_file, filter_defaults=False)
        self.config_file = config_file

    def load_unit(self, unit_config_file):
        self.load_config(unit_config_file, filter_defaults=False)

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
            raise KeyError("Symbol '{}' not found in Kconfig".format(sym))
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


# ---------------------------------------


class HHConfig(ConfigBuilder):
    """
    Enhanced ConfigBuilder for Happy Hare configuration management.

    Responsibilities:
      1. Load and merge multiple Happy Hare .cfg files
      2. Track where every option originally came from
      3. Preserve user edits during upgrades/reinstalls
      4. Detect deprecated/unused settings
      5. Copy hidden/excluded config blocks safely

    This allows configuration options to move between files
    without breaking upgrades, while preserving user changes
    and maintaining backward compatibility.
    """

    def __init__(self, cfg_files):
        super(HHConfig, self).__init__()
        self.origins = {}
        self.used_options = set()
        prefix = os.path.commonprefix(cfg_files)
        for cfg_file in cfg_files:
            logging.debug(" > Reading config file: " + cfg_file)
            basename = cfg_file.replace(prefix, "")
            super(HHConfig, self).read(cfg_file, origin=os.path.basename(cfg_file))
            for section in self.sections(scope="included"):
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

    def update_builder(self, builder, ignore_params, origin=None):
        """
        Update the builder config selectively from the existing config HH data
        ignore_params is a list of params that should NOT be updated. It is empty
        for the non-interactive upgrade case where all previous params are retained
        """

        # Copy over included options
        for section in builder.sections(scope="included"):
            for option in builder.options(section):
                if self.has_option(section, option):
                    if (
                        not option.startswith("gcode")     # Don't ever resuse the gcode
                        and option not in ignore_params
                    ):
                        builder.set(section, option, self.get(section, option))
                        logging.debug("Using previous [%s] %s: %s" % (section, option, self.get(section, option)))
                    elif not option.startswith("gcode"):
                        logging.debug("Ignoring previous [%s] %s: %s" % (section, option, self.get(section, option)))
                    self.used_options.add((section, option))

        # If existing config has excluded options use them in lieu of builder excluded options
        if self.excluded_nodes(origin=origin) and builder.excluded_nodes():
            logging.info("Preserving existing excluded config sections")
            builder.delete_excluded()                      # Ensure template is clean
            self._copy_excluded_to(builder, origin=origin) # Copy in previous excluded nodes
            self.delete_excluded(origin=origin)            # Prevent them being reported in unsed option set

    def _copy_excluded_to(self, builder, origin=None, insert_blank_line=True):
        """
        Copy all MagicExclusionNode(s) into builder config appending each as a separate top-level MagicExclusionNode.
        Returns the number of nodes copied.
        """

        # Append deep copies to the destination, one by one
        dest_doc = builder.document
        copied = 0
        for excluded in self.excluded_nodes(origin=origin):
            if insert_blank_line and dest_doc.body and not isinstance(dest_doc.body[-1], WhitespaceNode):
                dest_doc.body.append(WhitespaceNode("\n"))
            dest_doc.body.append(copy.deepcopy(excluded))
            copied += 1

        return copied

    def unused_options_for(self, origin):
        return [
            (section, key)
            for (section, key), file in self.origins.items()
            if file == origin and (section, key) not in self.used_options
        ]


# ---------------------------------------


def add_supplemental_params(builder, hhcfg, section):
    for param in supplemental_params + hidden_params:
        if hhcfg.has_option(section, param):
            logging.debug(" > Reinserting hidden / supplemental option: %s" % param)
            builder.copy_option(hhcfg, section, param)


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
    """Render the template config file after expanding KConfig params (and extra params) dictionary"""
    try:
        env = jinja_env()
        template = env.get_template(os.path.relpath(template_file))
        params = kcfg.as_dict()
        params.update(extra_params)
        return template.render(params)
    except UndefinedError as ue:
        logging.error("%s while rendering '%s' with KConfig '%s'" % (str(ue), template_file, kcfg.config_file))
        exit(1)


# TODO Really input_files should exclude first directory config/mmu to make origin consistent everywhere
def build(cfg_file, dest_file, kconfig, input_files):
    logging.debug("Building {} -> {} with kconfig {}".format(cfg_file, dest_file, kconfig))

    cfg_file_basename = cfg_file[len(os.getenv("SRC")) + 1 :]
    kcfg = load_parsed_kconfig(kconfig)
    extra_params = dict()

    # PARAM_TOTAL_NUM_GATES is required to create the Tx macro wrappers
    if kcfg.is_enabled("MULTI_UNIT_ENTRY_POINT"):
        unit_kcfgs = dict()
        total_num_gates = 0
        for unit in kcfg.get("MMU_UNITS").split(","):
            unit = unit.strip()
            unit_kcfgs[unit] = load_parsed_kconfig(kconfig + "_" + unit)
            total_num_gates += unit_kcfgs[unit].getint("PARAM_NUM_GATES")

        # Total sum of gates for all units
        extra_params["PARAM_TOTAL_NUM_GATES"] = total_num_gates
    else:
        extra_params["PARAM_TOTAL_NUM_GATES"] = kcfg.getint("PARAM_NUM_GATES")

    build_config_file(cfg_file_basename, dest_file, kcfg, input_files, extra_params)


# Where this fits into the Makefile flow:
# Live installed configs
#     v
# linked into OUT/in
#     v
# installer.build reads them (THIS STEP)
#     v
# new generated configs written to OUT/
#     v
# OUT files installed back to live locations
#
def build_config_file(cfg_file_basename, dest_file, kcfg, input_files, extra_params):
    dest_file_basename = dest_file[len(os.getenv("OUT")) + 1 :]
    logging.info("Building config file: %s" % dest_file_basename)

    # 1.Generate an aggregated master HHConfig for all HH input_files
    hhcfg = HHConfig(input_files)

    # 2.Run upgrade transform on aggregated master HH Config
    to_version = get_target_version()
    from_version = get_current_version(hhcfg)

    if major_minor(from_version) != major_minor(to_version):
        logging.debug("Upgrading {} from v{} to v{}".format(cfg_file_basename, from_version, to_version))
        upgrades = Upgrades()
        upgrades.upgrade(hhcfg, major_minor(from_version), major_minor(to_version))

        # Important to update version in case .mmu_config is not changed
        hhcfg.set("mmu_machine", "happy_hare_version", to_version)

    # 3.Render cfg template expanding KConfig parameters from read .mmu_config
    buffer = render_template(cfg_file_basename, kcfg, extra_params)
    logging.debug(
        "Rendered template '%s' using Kconfig '%s' with extra_params: %s"
        % (cfg_file_basename, kcfg.config_file, extra_params)
    )

    # 4.Generate builder Config from rendered cfg template
    builder = ConfigBuilder()
    builder.read_buf(buffer)

    # 5.Special case cfg files that contains parameters so we can add back any optional,
    #   hidden or supplemental params because they are not present in cfg template
    if cfg_file_basename == "config/base/mmu_parameters.cfg":
        name = Path(dest_file).name
        prefix = "mmu_parameters_"
        suffix = ".cfg"
        if name.startswith(prefix) and name.endswith(suffix):
            unit_name = name[len(prefix):-len(suffix)]
        else:
            unit_name = "mmu"
        section = "mmu_unit_parameters %s" % unit_name
        add_supplemental_params(builder, hhcfg, section)

    elif cfg_file_basename == "config/base/mmu.cfg":
        add_supplemental_params(builder, hhcfg, "mmu_parameters")

    # 6. Determine how much of the HHConfig (existing .cfg's) do we re-apply
    refresh_mode = os.getenv("F_CFG_UPGRADE_MODE", 'refresh').lower()

    if refresh_mode == 'refresh':
        # Default choice (always used when menuconfig UI is not run)
        # Here we use the refreshed cfg templates as a starting point but
        # then replace every matching parameter with existing value.
        # Unused options will be reported.
        filtered_params = [] # Don't filter out any existing params in HHConfig

    elif refresh_mode == 'replace':
        # Here we (re)create prestine cfg files based on kconfig settings
        pass

    elif refresh_mode == 'merge':
        # Experimental. Here we selectively ignore simple PARAM_ parameter settings
        # so that the Kconfig value "wins" in these cases
        filtered_params = [
            k.lower()[6:]
            for k in kcfg.as_dict()
            if k.lower().startswith("param_")
        ]
        logging.debug("The following parameters are being filtered: %s" % ", ".join(filtered_params))

    else:
        logging.error("Invalid F_CFG_UPGRADE_MODE '%s'" % refresh_mode)
        exit(1)

    if refresh_mode != 'replace':
        # 7.Update the builder Config from the existing master HHConfig to ensure user edits are preserved
        hhcfg.update_builder(builder, filtered_params, origin=os.path.basename(dest_file))

        # 8.Report on deprecated/unused options
        first = True
        origin = re.sub(r"^mmu[/\\]", "", dest_file_basename)
        for section, option in hhcfg.unused_options_for(origin):
            if first:
                first = False
                logging.warning("The following parameters in {} have been dropped:".format(dest_file_basename))
            logging.warning("[{}] {}: {}".format(section, option, hhcfg.get(section, option)))

    # 9.Write builder Config to destination cfg file
    if os.path.islink(dest_file):
        os.remove(dest_file)

    data = builder.write()
    with open(dest_file, "wb") as f:
        f.write(data.encode("utf-8"))


def install_moonraker(moonraker_cfg, existing_cfg, kconfig):
    logging.info("Checking for moonraker.conf additions")

    kcfg = load_parsed_kconfig(kconfig)
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

    kcfg = load_parsed_kconfig(kconfig)
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

    if not builder.has_section("include mmu/macros/*.cfg"):
        logging.debug(" > Adding include [include mmu/macros/*.cfg]")
        builder.add_section("include mmu/macros/*.cfg", at_top=True, extra_newline=False)

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
    for section in builder.sections():
        if section.startswith("include mmu/"):
            logging.debug(" > Removing include [{}]".format(section))
            builder.remove_section(section)

    with open(dest_file, "w") as f:
        f.write(builder.write())


def restart_service(name, service, kconfig):
    if not service:
        logging.warning("No {name} service specified - Please restart manually")
    else:
        logging.info("Restarting {}...".format(name))

    kcfg = load_parsed_kconfig(kconfig)
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


def major_minor(version_str):
    """
    Convert "<major>.<minor>.<point>" to (<major>, <minor>)
    """
    major, minor, *_ = version_str.strip('"').split(".")
    return (int(major), int(minor))


def get_current_version(hhcfg):
    current_version = None
    if hhcfg.has_section("mmu_machine"):
        current_version = hhcfg.get("mmu_machine", "happy_hare_version")
    elif hhcfg.has_section("mmu"):
        current_version = hhcfg.get("mmu", "happy_hare_version") # old v3 config location

    if current_version is None:
        current_version = "4.0.0"
    return current_version


def get_target_version():
    target_version = os.environ.get("HH_VERSION")
    return target_version


def get_config_version(kcfg):
    version = kcfg.get("HAPPY_HARE_VERSION")
    return version


def check_version(kconfig, input_files):
    hhcfg = HHConfig(input_files)
    kcfg = load_parsed_kconfig(kconfig)

    # Current version is pulled from current cfg files...
    current_version = get_current_version(hhcfg)
    logging.log(LEVEL_NOTICE, f"Current version: v{current_version}")

    # Target version is pulled from the environment (from mmu_constants.py)
    target_version = get_target_version()
    if target_version is None:
        logging.error("Target version HH_VERSION was not set")
        exit(1)

    if major_minor(current_version) == major_minor(target_version):
        logging.log(LEVEL_NOTICE, "Up to date, no config upgrades required")
        return

    if major_minor(current_version) > major_minor(target_version):
        logging.warning(
            "Automatic 'downgrade' to earlier version is not guaranteed!\n"
            "If you encounter startup problems you may need to manually compare "
            "the backed-up 'mmu_hardware.cfg and 'mmu_parameters.cfg' with current one to restore differences"
        )
        return

    logging.log(LEVEL_NOTICE, "Will try to upgrade to " + target_version)


def pre_parse_kconfig(kconfig):
    out = os.getenv("OUT")
    base = os.path.basename(kconfig)
    pickle_file = "%s/%s.pickle" % (out, base)
    logging.debug(" > Pickling kconfig file: %s -> %s" % (kconfig, pickle_file))
    kcfg = KConfig(kconfig)
    sys.setrecursionlimit(20000)  # Increase recursion limit for pickling kconfiglib structures
    with open(pickle_file, "wb") as f:
        dill.dump(kcfg, f, recurse=True)


def load_parsed_kconfig(kconfig):
    out = os.getenv("OUT")
    base = os.path.basename(kconfig)
    pickle_file = "%s/%s.pickle" % (out, base)
    logging.debug(" > Loading pickled kconfig file: %s" % pickle_file)
    try:
        with open(pickle_file, "rb") as f:
            return dill.load(f)
    except FileNotFoundError:
        # This shouldn't happen because we want make to decide on staleness
        logging.warning("Pre-parsed kconfig for '%s' not available... Parsing original" % kconfig)
        return KConfig(kconfig)
    except Exception as e:
        logging.error("Error unpickling '%s' (%s)" % (pickle_file, e))
        exit(1)


# ----------------------------------------------------------------------------------------------
# Dynamically generate optional Kconfig rules to augment options choices for:
# mmu_toolhead, mmu_encoder, mmu_sync_feedback
# The idea is that these can be shared and this would allow selection from choice list
# TODO: This is currently incomplete and unused (never called)
# ----------------------------------------------------------------------------------------------

# Param tokens
TOOLHEAD                  = "TOOLHEAD"
ENCODER_NAME              = "ENCODER_NAME"
SYNC_FEEDBACK_BUFFER_NAME = "SYNC_FEEDBACK_BUFFER_NAME"

# Regex patterns grouped by token
PARAM_REGEX = {
    TOOLHEAD: {
        "value": re.compile(r'^CONFIG_PARAM_TOOLHEAD="((?:[^"\\]|\\.)*)"(?:\s+#.*)?$'),
        "shared": re.compile(r'^CONFIG_MMU_SHARED_TOOLHEAD=y(?:\s+#.*)?$'),
    },
    ENCODER_NAME: {
        "value": re.compile(r'^CONFIG_PARAM_ENCODER_NAME="((?:[^"\\]|\\.)*)"(?:\s+#.*)?$'),
        "shared": re.compile(r'^CONFIG_MMU_SHARED_ENCODER=y(?:\s+#.*)?$'),
    },
    SYNC_FEEDBACK_BUFFER_NAME: {
        "value": re.compile(r'^CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="((?:[^"\\]|\\.)*)"(?:\s+#.*)?$'),
        "shared": re.compile(r'^CONFIG_MMU_SHARED_SYNC_FEEDBACK_BUFFER=y(?:\s+#.*)?$'),
    },
}
GENERATED_KCONFIG_PATH = "/tmp/.Kconfig.generated"

def gen_kconfig_options(configs):
    parts = []

    for token in [TOOLHEAD, ENCODER_NAME, SYNC_FEEDBACK_BUFFER_NAME]:
        text = generate_choices(token)
        if text:
            parts.append(text)

    # Join with a newline between sections (and ensure trailing newline)
    output = "\n".join(parts)
    if output and not output.endswith("\n"):
        output += "\n"

    with open(GENERATED_KCONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(output)

def split_csv(raw):
    return [part.strip() for part in raw.split(",") if part.strip()]

def unescape_kconfig_string(s):
    # Matches the escaping style used in .mmu_config string values
    return re.sub(r'\\(.)', r'\1', s)

def to_symbol(name):
    sym = re.sub(r"[^A-Za-z0-9_]", "_", name.strip().upper())
    sym = re.sub(r"_+", "_", sym).strip("_")
    if not sym:
        raise ValueError(f"Invalid param name: {name!r}")
    if sym[0].isdigit():
        sym = f"_{sym}"
    return sym

def parse_param_from_config(path, token):
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return [] # If exclude pattern exists anywhere in the file, ignore everything

    value_regexp = PARAM_REGEX[token]["value"]
    exclude_regexp = PARAM_REGEX[token]["shared"]

    if exclude_regexp:
        for line in lines:
            if exclude_regexp.search(line):
                return [] # Otherwise find the (single) match

    for line in lines:
        m = value_regexp.match(line.rstrip("\n"))
        if not m:
            continue

        name = unescape_kconfig_string(m.group(1)).strip()
        if name:
            return [name]

    return []

def discover_names_from_configs(token):
    """
    Reads:
      - base config (KCONFIG_CONFIG or .mmu_config)
      - per-unit configs: KCONFIG_CONFIG_<unit>

    using CONFIG_MMU_UNITS to know which unit config files to inspect.
    """
    base_config = os.environ.get("KCONFIG_CONFIG", ".mmu_config")
    mmu_units = split_csv(os.environ.get("CONFIG_MMU_UNITS", ""))

    seen = set()
    result = []

    def add_from_file(path):
        for name in parse_param_from_config(path, token):
            if name not in seen:
                seen.add(name)
                result.append(name)

    add_from_file(base_config)

    for unit_name in mmu_units:
        cfg_path = f"{base_config}_{unit_name}"
        add_from_file(cfg_path)

    return result

def generate_choices(token):
    names = discover_names_from_configs(token)
    if not names:
        return ""

    lines = []
    lines.append(f'choice {token}_TYPE')

    for name in names:
        sym = to_symbol(name)

        lines.append(f'  config CHOICE_{token}_TYPE_{sym}')
        lines.append(f'    bool "{name}"')
        lines.append('')
        lines.append(f'  if CHOICE_{token}_TYPE_{sym}')
        lines.append(f'    config PARAM_{token}_TYPE')
        lines.append('      default "Shared"')
        lines.append(f'    config MMU_SHARED_{token}')
        lines.append('      default y')
        lines.append(f'    config PARAM_{token}')
        lines.append(f'      default "{name}"')
        lines.append('  endif')

    lines.append('endchoice')
    lines.append('')

    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------

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
    parser.add_argument("--pre-parse-kconfig", nargs=1)
    parser.add_argument("--gen-kconfig-options", nargs=1)
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

    if args.pre_parse_kconfig:
        pre_parse_kconfig(args.pre_parse_kconfig[0])

    if args.gen_kconfig_options:
        gen_kconfig_options(args.gen_kconfig_options[0:])


if __name__ == "__main__":
    main()
