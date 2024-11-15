SHELL=/usr/bin/env bash

export KCONFIG_CONFIG ?= .config
# For parallel builds
MAKEFLAGS += --jobs 16
# kconfiglib/menuconfig doesn't like --output-sync, so we don't add it if it's the target or if .config doesn't exist yet
MAKEFLAGS += $(if $(or $(findstring menuconfig,$(MAKECMDGOALS)),$(shell [ ! -e "$(KCONFIG_CONFIG)" ] && echo y)),, --output-sync)
# For quiet builds, override with make Q= for verbose output
Q ?= @


include $(KCONFIG_CONFIG)

# If CONFIG_IS_MIPS is not yet set by .config, set it here
ifeq ($(shell uname -m),mips)
  CONFIG_IS_MIPS ?= y
endif

# If CONFIG_KLIPPER_HOME is not yet set by .config, set it to the default value
# this is required to make menuconfig work the first time
# can be overridden with 'make CONFIG_KLIPPER_HOME=/path/to/klipper <target>'
ifeq ($(CONFIG_IS_MIPS),y)
  CONFIG_KLIPPER_HOME ?= /usr/share/klipper
else
  CONFIG_KLIPPER_HOME ?= ${HOME}/klipper
endif

export SRC ?= $(CURDIR)

# Use the name of the 'name.config' file as the output directory, or 'out' if just '.config' is used
ifeq ($(basename $(KCONFIG_CONFIG)),)
  export OUT ?= $(CURDIR)/out
else
  export OUT ?= $(CURDIR)/out_$(basename $(KCONFIG_CONFIG))
endif

# Strings in .config are quoted, this removes the quotes so paths are handled properly
# I feel like there should be a better way but I haven't found it yet..
unquote = $(patsubst "%",%,$(1))

# Klipper python extras
klipper_home = $(call unquote,$(CONFIG_KLIPPER_HOME))
hh_klipper_extras_files = $(wildcard extras/*.py extras/*/*.py )
hh_klipper_extras_dirs = $(sort $(dir $(hh_klipper_extras_files)))
hh_old_klipper_modules = mmu.py mmu_toolhead.py

# Klipper config files
klipper_config_home = $(call unquote,$(CONFIG_KLIPPER_CONFIG_HOME))
klipper_printer_file = $(call unquote,$(CONFIG_PRINTER_CONFIG))
hh_config_files = $(patsubst config/%, %, $(wildcard config/*.cfg config/*/*.cfg))
hh_config_dirs = $(sort $(dir $(hh_config_files)))

# Moonraker files
moonraker_home = $(call unquote,$(CONFIG_MOONRAKER_HOME))
hh_moonraker_components = $(wildcard components/*.py )
moonraker_config_file = $(call unquote,$(CONFIG_MOONRAKER_CONFIG_FILE))

# Bool to check if moonraker needs to be restarted
restart_moonraker = 0

# Files/targets that will be built
build_targets = $(OUT)/$(moonraker_config_file) \
	$(OUT)/$(klipper_printer_file) \
	$(addprefix $(OUT)/mmu/, $(hh_config_files)) \
	$(addprefix $(OUT)/klippy/,$(hh_klipper_extras_files)) \
	$(addprefix $(OUT)/moonraker/,$(hh_moonraker_components)) \

# Files/targets that will be installed
install_targets = $(klipper_config_home)/$(moonraker_config_file) \
	$(klipper_config_home)/$(klipper_printer_file) \
	$(addprefix $(klipper_config_home)/mmu/, $(hh_config_files)) \
	$(addprefix $(moonraker_home)/moonraker/, $(hh_moonraker_components)) \
	$(addprefix $(klipper_home)/klippy/, $(hh_klipper_extras_files))

# Install function
install = echo "Installing $(2)"; \
	mkdir -p $(dir $(2)); \
	cp -rdf $(3) "$(1)" "$(2)";

# Backup function
backup_ext ?= .old-$(shell date '+%Y%m%d-%H%M%S')
backup = if [ -e "$(1)" ] && [ ! -e "$(addsuffix $(backup_ext),$(1))" ]; then \
		echo "Making a backup of '$(1)' to '$(addsuffix $(backup_ext),$(1))'"; \
		cp -a "$(1)" "$(addsuffix $(backup_ext),$(1))"; \
	fi

.DEFAULT_GOAL := install
.PHONY: update menuconfig install uninstall remove_old_klippy_modules check_root check_paths check_version diff build clean clean-all

# Copy existing $(moonraker_config_file) and printer.cfg to the out directory
$(OUT)/$(moonraker_config_file):
	echo "Copying moonraker_config_file.cfg"
	$(Q)cp -a "$(klipper_config_home)/$(moonraker_config_file)" "$@" # Copy the current version to the out directory
	$(Q)$(SRC)/scripts/build.sh install-update-manager "$@"

$(OUT)/$(klipper_printer_file):
	echo "Copying printer.cfg"
	$(Q)cp -a "$(klipper_config_home)/$(klipper_printer_file)" "$@" # Copy the current version to the out directory
	$(Q)$(SRC)/scripts/build.sh install-includes "$@"


# We link all config files, those that need to be updated will be written over in the install script
$(OUT)/mmu/%.cfg: $(SRC)/config/%.cfg | $(addprefix $(OUT)/mmu/,$(hh_config_dirs))
	$(Q)ln -sf "$(abspath $<)" "$@"
	$(Q)$(SRC)/scripts/build.sh build "$<" "$@"

# Python files are linked to the out directory
$(OUT)/klippy/extras/%.py: $(SRC)/extras/%.py | $(addprefix $(OUT)/klippy/,$(hh_klipper_extras_dirs))
	$(Q)ln -sf "$(abspath $<)" "$@"

$(OUT)/moonraker/components/%.py: $(SRC)/components/%.py | $(OUT)/moonraker/components/
	$(Q)ln -sf "$(abspath $<)" "$@"


# Create the output directories
$(OUT):
	$(Q)mkdir -p "$@"

$(OUT)/%/:
	$(Q)mkdir -p "$@"


$(build_targets): $(KCONFIG_CONFIG) | update $(OUT)

build: $(build_targets) | check_paths check_version 

# Different install targets
$(klipper_home)/%: $(OUT)/%
	$(Q)$(call install,$<,$@)

$(moonraker_home)/%: $(OUT)/%
	$(Q)$(call install,$<,$@)

$(klipper_config_home)/%: $(OUT)/%
	$(Q)$(call backup,$@)
	$(Q)$(call install,$<,$@)

$(klipper_config_home)/${moonraker_config_file}: $(OUT)/$(moonraker_config_file)
	$(Q)$(call backup,$@)
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_moonraker = 1)

$(klipper_config_home)/mmu/%: $(OUT)/mmu/%
	$(Q)$(call backup,$(klipper_config_home)/mmu)
	$(Q)$(call install,$<,$@)

# Special case for mmu_vars.cfg, we don't want to overwrite it
$(klipper_config_home)/mmu/mmu_vars.cfg: $(OUT)/mmu/mmu_vars.cfg
	$(Q)$(call backup,$(klipper_config_home)/mmu)
	$(Q)$(call install,$<,$@,--update=none)

$(install_targets): | build

install: $(install_targets) | update check_root remove_old_modules
	$(Q)[ "$(restart_moonraker)" -eq 0 ] || $(SRC)/scripts/build.sh restart-service "Moonraker" $(CONFIG_SERVICE_MOONRAKER)
	$(Q)$(SRC)/scripts/build.sh restart-service "Klipper" $(CONFIG_SERVICE_KLIPPER)
	$(Q)$(SRC)/scripts/build.sh print-happy-hare

# Remove old klippy modules that are no longer needed
remove_old_modules:
	$(Q)cd $(klipper_home)/klippy/extras && rm -f $(hh_old_klipper_modules)

update: check_root
	$(Q)$(SRC)/scripts/build.sh self-update

uninstall:
	$(Q)$(call backup,$(klipper_config_home)/$(moonraker_config_file))
	$(Q)$(call backup,$(klipper_config_home)/$(klipper_printer_file))
	$(Q)$(call backup,$(klipper_config_home)/mmu)
	$(Q)rm -rf $(addprefix $(klipper_home)/klippy/,$(hh_klipper_extras_files) $(hh_klipper_extras_dirs))
	$(Q)rm -f $(addprefix $(moonraker_home)/moonraker/, $(hh_moonraker_components))
	$(Q)rm -rf $(klipper_config_home)/mmu
	$(Q)$(SRC)/scripts/build.sh uninstall
	$(Q)$(SRC)/scripts/build.sh restart-service "Moonraker" $(CONFIG_SERVICE_MOONRAKER)
	$(Q)$(SRC)/scripts/build.sh restart-service "Klipper" $(CONFIG_SERVICE_KLIPPER)
	$(Q)$(SRC)/scripts/build.sh print-unhappy-hare

clean:
	$(Q)rm -rf $(OUT)

clean-all: clean
	$(Q)rm -rf $(KCONFIG_CONFIG)

# Target to see the difference between an existing config and the one build
diff_args = -U2 --color --src-prefix="current: " --dst-prefix="built: " --minimal --word-diff=color --stat --no-index
# Filter out command and index lines from the diff, they only muck up the information
diff_filter = | grep -v "diff --git " | grep -Ev "index [[:xdigit:]]+\.\.[[:xdigit:]]+"
diff: | check_paths
	$(Q)[ -d "$(OUT)/mmu" ] || { echo "No build directory found, exiting. Run 'make build' first"; exit 1; }
	$(Q)git diff $(diff_args) -- "$(klipper_config_home)/mmu" "$(patsubst $(CURDIR)/%,%,$(OUT)/mmu)" $(diff_filter) || true
	$(Q)git diff $(diff_args) -- "$(klipper_config_home)/$(klipper_printer_file)" "$(patsubst $(CURDIR)/%,%,$(OUT)/$(klipper_printer_file))" $(diff_filter) || true
	$(Q)git diff $(diff_args) -- "$(klipper_config_home)/$(moonraker_config_file)" "$(patsubst $(CURDIR)/%,%,$(OUT)/$(moonraker_config_file))" $(diff_filter) || true

check_root:
ifneq ($(shell id -u),0)
  ifeq ($(CONFIG_IS_MIPS),y)
	$(error "Please run as root")
  endif
else 
  ifneq ($(CONFIG_IS_MIPS),y) 
	$(error "Please run as non-root")
  endif
endif

check_version:
	$(Q)$(SRC)/scripts/build.sh check-version

# Check whther the required paths exist
check_paths:
	$(Q)[ -f "$(klipper_config_home)/$(moonraker_config_file)" ] || { \
		echo -e "The file '$(klipper_config_home)/$(moonraker_config_file)' does not exist. Please check your config for the correct paths"; \
		exit 1; }
	$(Q)[ -f "$(klipper_config_home)/$(klipper_printer_file)" ] || { \
		echo -e "The file '$(klipper_config_home)/$(klipper_printer_file)' does not exist. Please check your config for the correct paths"; \
		exit 1; }
	$(Q)[ -d "$(klipper_home)/klippy/extras" ] || { \
		echo -e "The directory '$(klipper_home)/klippy/extras' does not exist. Please check your config for the correct paths"; \
		exit 1; }
ifneq ($(CONFIG_OCTOPRINT),y)
	$(Q)[ -d "$(moonraker_home)/moonraker/components" ] || { \
		echo -e "The directory '$(moonraker_home)/moonraker/components' does not exist. Please check your config for the correct paths"; \
		exit 1; }
endif

$(KCONFIG_CONFIG): $(SRC)/scripts/Kconfig
# 	If .config does not exist yet run menuconfig, else just update it
#	touch in case .config does not get updated by olddefconfig.py
	$(Q)if [ -f $(KCONFIG_CONFIG) ]; then \
		python $(klipper_home)/lib/kconfiglib/olddefconfig.py $(SRC)/scripts/Kconfig; \
	elif [[ ! "$(MAKECMDGOALS)" =~ menuconfig ]]; then \
		$(MAKE) menuconfig; \
		[ -f $(KCONFIG_CONFIG) ] || { echo "No $(KCONFIG_CONFIG) file found, exiting. Run 'make menuconfig' to create a config file"; exit 1; }; \
	fi

menuconfig: $(SRC)/scripts/Kconfig
	$(Q)MENUCONFIG_STYLE="aquatic" python ${klipper_home}/lib/kconfiglib/menuconfig.py $(SRC)/scripts/Kconfig

