SHELL=/usr/bin/env bash

export KCONFIG_CONFIG ?= .config
include $(KCONFIG_CONFIG)

# For quiet builds, override with make Q= for verbose output
Q ?= @
export SRC ?= $(CURDIR)

# Use the name of the 'name.config' file as the out_name directory, or 'out' if just '.config' is used
ifeq ($(basename $(KCONFIG_CONFIG)),)
  export OUT ?= $(CURDIR)/out
else
  export OUT ?= $(CURDIR)/out_$(basename $(KCONFIG_CONFIG))
endif

export IN=$(OUT)/in

# Print Colors (exported for use in scripts)
export C_OFF=\033[0m
# Cyan
export C_INFO=\033[1;36m
# Bold Green
export C_NOTICE=\033[1;32m
# Bold Yellow
export C_WARNING=\033[1;33m
# Bold Red
export C_ERROR=\033[1;31m


MAKEFLAGS += --jobs 16 # Parallel build
# kconfiglib/menuconfig doesn't like --output-sync, so we don't add it if it's the target or if .config is outdated
ifeq ($(findstring menuconfig,$(MAKECMDGOALS)),)
  ifeq ($(shell [ "$(KCONFIG_CONFIG)" -ot "$(SRC)/scripts/Kconfig" ] || echo y),y)
    MAKEFLAGS += --output-sync
  endif
endif

# If CONFIG_IS_MIPS is not yet set by .config, set it here
ifeq ($(shell uname -m),mips)
  CONFIG_IS_MIPS ?= y
endif

# If CONFIG_KLIPPER_HOME is not yet set by .config, set it to the default value.
# This is required to make menuconfig work the first time.
# If the klipper directory is not at one of the standard locations,
# it can be overridden with 'make CONFIG_KLIPPER_HOME=/path/to/klipper <target>'
ifeq ($(CONFIG_IS_MIPS),y)
  CONFIG_KLIPPER_HOME ?= /usr/share/klipper
else
  CONFIG_KLIPPER_HOME ?= ~/klipper
endif

CONFIG_KLIPPER_HOME:=$(patsubst "%",%,$(CONFIG_KLIPPER_HOME))
CONFIG_KLIPPER_CONFIG_HOME:=$(patsubst "%",%,$(CONFIG_KLIPPER_CONFIG_HOME))
CONFIG_MOONRAKER_HOME:=$(patsubst "%",%,$(CONFIG_MOONRAKER_HOME))
CONFIG_PRINTER_CONFIG_FILE:=$(patsubst "%",%,$(CONFIG_PRINTER_CONFIG_FILE))
CONFIG_MOONRAKER_CONFIG_FILE:=$(patsubst "%",%,$(CONFIG_MOONRAKER_CONFIG_FILE))

hh_klipper_extras_files = $(patsubst extras/%,%,$(wildcard extras/*.py extras/*/*.py))
hh_old_klipper_modules = mmu.py mmu_toolhead.py # These will get removed upon install
hh_config_files = $(patsubst config/%,%,$(wildcard config/*.cfg config/**/*.cfg))
hh_moonraker_components = $(patsubst components/%,%,$(wildcard components/*.py))

export CONFIGS_TO_PARSE = $(patsubst config/%,$(IN)/mmu/%,$(wildcard $(addprefix config/, \
	base/mmu.cfg \
	base/mmu_parameters.cfg \
	base/mmu_hardware.cfg \
	base/mmu_macro_vars.cfg \
	addons/*.cfg)))

# Files/targets that need to be build
build_targets = \
	$(OUT)/$(CONFIG_MOONRAKER_CONFIG_FILE) \
	$(OUT)/$(CONFIG_PRINTER_CONFIG_FILE) \
	$(addprefix $(OUT)/mmu/, $(hh_config_files)) \
	$(addprefix $(OUT)/klippy/extras/,$(hh_klipper_extras_files)) \
	$(addprefix $(OUT)/moonraker/components/,$(hh_moonraker_components)) 

# Files/targets that need to be installed
install_targets = \
	$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE) \
	$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_PRINTER_CONFIG_FILE) \
	$(addprefix $(CONFIG_KLIPPER_CONFIG_HOME)/mmu/, $(hh_config_files)) \
	$(addprefix $(CONFIG_KLIPPER_HOME)/klippy/extras/, $(hh_klipper_extras_files)) \
	$(addprefix $(CONFIG_MOONRAKER_HOME)/moonraker/components/, $(hh_moonraker_components))


# Recipe functions
install = \
	$(info Installing $(subst ",,$(2))...) \
	mkdir -p $(dir $(2)); \
	cp -af $(3) "$(1)" "$(2)";

link = \
	mkdir -p $(dir $(2)); \
	ln -sf "$(1)" "$(2)";

backup_ext :::= .old-$(shell date '+%Y%m%d-%H%M%S')
backup_ext ::= .old-$(shell date '+%Y%m%d-%H%M%S')
backup_name = $(addsuffix $(backup_ext),$(1))
backup = \
	if [ -e "$(1)" ] && [ ! -e "$(call backup_name,$(1))" ]; then \
		echo -e "$(C_NOTICE)Making a backup of '$(1)' to '$(call backup_name,$(1))'$(C_OFF)"; \
		cp -a "$(1)" "$(call backup_name,$(1))"; \
	fi

# Bool to check if moonraker/klipper needs to be restarted
restart_moonraker = 0
restart_klipper = 0

.SECONDEXPANSION:
.DEFAULT_GOAL := build
.PRECIOUS: $(KCONFIG_CONFIG)
.PHONY: update menuconfig install uninstall check_root check_paths check_version diff test build clean
.SECONDARY: $(call backup_name,$(CONFIG_KLIPPER_CONFIG_HOME)/mmu) \
	$(call backup_name,$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE)) \
	$(call backup_name,$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_PRINTER_CONFIG_FILE)) \
	$(OUT)/params.tmp

FORCE:

### Build targets

# Copy existing config files to the out/in directory to break circular dependency
$(IN)/%: FORCE 
	$(Q)mkdir -p "${@D}"
	$(Q)if [ -f "$(CONFIG_KLIPPER_CONFIG_HOME)/$*" ]; then \
		cp -af "$(CONFIG_KLIPPER_CONFIG_HOME)/$*" "$@"; \
	else \
		[ -f "$@" ] || touch "$@"; \
		[ ! -s "$@" ] || truncate -s 0 "$@"; \
	fi

ifneq ($(wildcard $(subst ",,$(KCONFIG_CONFIG))),) # To prevent make errors when .config is not yet created

# Copy existing moonraker.conf and printer.cfg to the out directory
$(OUT)/$(CONFIG_MOONRAKER_CONFIG_FILE): $(IN)/$$(@F)
	$(info Copying $(CONFIG_MOONRAKER_CONFIG_FILE) to '$(notdir $(OUT))' directory)
	$(Q)cp -a "$<" "$@" # Copy the current version to the out directory
	$(Q)$(SRC)/scripts/build.sh install-moonraker "$@"

$(OUT)/$(CONFIG_PRINTER_CONFIG_FILE): $(IN)/$$(@F)
	$(info Copying $(CONFIG_PRINTER_CONFIG_FILE) to '$(notdir $(OUT))' directory)
	$(Q)cp -a "$<" "$@" # Copy the current version to the out directory
	$(Q)$(SRC)/scripts/build.sh install-includes "$@"

endif

$(OUT)/params.tmp: $(CONFIGS_TO_PARSE)
	$(Q)$(SRC)/scripts/build.sh parse-params

# We link all config files, those that need to be updated will be written over in the install script
$(OUT)/mmu/%.cfg: $(SRC)/config/%.cfg $(OUT)/params.tmp  
	$(Q)$(call link,$<,$@)
	$(Q)$(SRC)/scripts/build.sh build "$<" "$@"

# Python files are linked to the out directory
$(OUT)/klippy/extras/%.py: $(SRC)/extras/%.py
	$(Q)$(call link,$<,$@)

$(OUT)/moonraker/components/%.py: $(SRC)/components/%.py
	$(Q)$(call link,$<,$@)

$(OUT):
	$(Q)mkdir -p "$@"

$(build_targets): $(KCONFIG_CONFIG) | $(OUT) update check_paths check_version 

build: $(build_targets)


### Install targets
$(CONFIG_KLIPPER_HOME)/%: $(OUT)/%
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_klipper = 1)

$(CONFIG_MOONRAKER_HOME)/%: $(OUT)/%
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_moonraker = 1)

$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_PRINTER_CONFIG_FILE): $(OUT)/$$(@F) | $(call backup_name,$$@)
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_klipper = 1)

$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE): $(OUT)/$$(@F) | $(call backup_name,$$@)
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_moonraker = 1)

$(CONFIG_KLIPPER_CONFIG_HOME)/mmu/%.cfg: $(OUT)/mmu/%.cfg | $(call backup_name,$(CONFIG_KLIPPER_CONFIG_HOME)/mmu) 
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_klipper = 1)

$(CONFIG_KLIPPER_CONFIG_HOME)/mmu/mmu_vars.cfg: | $(OUT)/mmu/mmu_vars.cfg $(call backup_name,$(CONFIG_KLIPPER_CONFIG_HOME)/mmu) 
	$(Q)$(call install,$(OUT)/mmu/mmu_vars.cfg,$@,--update=none)
	$(Q)$(eval restart_klipper = 1)

$(call backup_name,$(CONFIG_KLIPPER_CONFIG_HOME)/%): $(OUT)/% | build
	$(Q)$(call backup,$(basename $@))

$(call backup_name,$(CONFIG_KLIPPER_CONFIG_HOME)/mmu): $(addprefix $(OUT)/mmu/, $(hh_config_files)) | build
	$(Q)$(call backup,$(basename $@))

$(install_targets): | build update check_root check_paths check_version

install: $(install_targets)
	$(Q)rm -rf $(addprefix $(CONFIG_KLIPPER_HOME)/klippy/extras,$(hh_old_klipper_modules))
	$(Q)[ "$(restart_moonraker)" -eq 0 ] || $(SRC)/scripts/build.sh restart-service "Moonraker" $(CONFIG_SERVICE_MOONRAKER)
	$(Q)[ "$(restart_klipper)" -eq 0 ] || $(SRC)/scripts/build.sh restart-service "Klipper" $(CONFIG_SERVICE_KLIPPER)
	$(Q)$(SRC)/scripts/build.sh print-happy-hare

uninstall:
	$(Q)$(call backup,$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE))
	$(Q)$(call backup,$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_PRINTER_CONFIG_FILE))
	$(Q)$(call backup,$(CONFIG_KLIPPER_CONFIG_HOME)/mmu)
	$(Q)rm -rf $(addprefix $(CONFIG_KLIPPER_HOME)/klippy/extras/,$(hh_klipper_extras_files) $(filter-out ./,$(dir $(hh_klipper_extras_files))))
	$(Q)rm -rf $(addprefix $(CONFIG_MOONRAKER_HOME)/moonraker/components/,$(hh_moonraker_components) $(filter-out ./,$(dir $(hh_moonraker_components))))
	$(Q)rm -rf $(CONFIG_KLIPPER_CONFIG_HOME)/mmu
	$(Q)$(SRC)/scripts/build.sh uninstall-moonraker $(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE)
	$(Q)$(SRC)/scripts/build.sh uninstall-includes $(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_PRINTER_CONFIG_FILE)
	$(Q)$(SRC)/scripts/build.sh restart-service "Moonraker" $(CONFIG_SERVICE_MOONRAKER)
	$(Q)$(SRC)/scripts/build.sh restart-service "Klipper" $(CONFIG_SERVICE_KLIPPER)
	$(Q)$(SRC)/scripts/build.sh print-unhappy-hare


### Misc targets
update: check_root
	$(Q)$(SRC)/scripts/build.sh self-update

clean:
	$(Q)rm -rf $(OUT)

diff: check_paths $(build_targets)
	$(Q)$(SRC)/scripts/build.sh diff "$(CONFIG_KLIPPER_CONFIG_HOME)/mmu" "$(patsubst $(SRC)/%,%,$(OUT)/mmu)"
	$(Q)$(SRC)/scripts/build.sh diff "$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_PRINTER_CONFIG_FILE)" "$(patsubst $(SRC)/%,%,$(OUT)/$(CONFIG_PRINTER_CONFIG_FILE))"
	$(Q)$(SRC)/scripts/build.sh diff "$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE)" "$(patsubst $(SRC)/%,%,$(OUT)/$(CONFIG_MOONRAKER_CONFIG_FILE))"

test: 
	$(Q)$(SRC)/scripts/build.sh tests

check_root:
ifneq ($(shell id -u),0)
  ifeq ($(CONFIG_IS_MIPS),y)
	$(error Please run as root)
  endif
else
  ifneq ($(CONFIG_IS_MIPS),y)
	$(error Please run as non-root)
  endif
endif

check_version:
	$(Q)$(SRC)/scripts/build.sh check-version

# Check whther the required paths exist
check_paths:
ifeq ($(wildcard $(subst ",,$(CONFIG_KLIPPER_HOME)/klippy/extras)),)
	$(error The directory '$(subst ",,$(CONFIG_KLIPPER_HOME)/klippy/extras)' does not exist. Please check your config for the correct paths)
endif
ifeq ($(wildcard $(subst ",,$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_PRINTER_CONFIG_FILE))),)
	$(error The file '$(subst ",,$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_KLIPPER_PRINTER_CONFIG_FILE))' does not exist. Please check your config for the correct paths)
endif
ifneq ($(CONFIG_OCTOPRINT),y)
  ifeq ($(wildcard $(subst ",,$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE))),)
	$(error The file '$(subst ",,$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE))' does not exist. Please check your config for the correct paths)
  endif
  ifeq ($(wildcard $(subst ",,$(CONFIG_MOONRAKER_HOME)/moonraker/components)),)
	$(error The directory '$(subst ",,$(CONFIG_MOONRAKER_HOME)/moonraker/components)' does not exist. Please check your config for the correct paths)
  endif
endif

$(KCONFIG_CONFIG): $(SRC)/scripts/Kconfig
# if KCONFIG_CONFIG is outdated or doesn't exist run menuconfig first. If the user doesn't save the config, we will update it with olddefconfig
# touch in case .config does not get updated by olddefconfig.py
ifneq ($(findstring menuconfig,$(MAKECMDGOALS)),menuconfig)
	$(Q)$(MAKE) -s MAKEFLAGS= menuconfig
	$(Q)python $(CONFIG_KLIPPER_HOME)/lib/kconfiglib/olddefconfig.py $(SRC)/scripts/Kconfig >/dev/null # Always update the .config file in case user doesn't save it
	$(Q)touch $(KCONFIG_CONFIG)
	$(Q)[ -f "$(KCONFIG_CONFIG)" ] || { echo "No config file found. run 'make menuconfig' to create one"; exit 1; }
endif

menuconfig: $(SRC)/scripts/Kconfig
	$(Q)MENUCONFIG_STYLE="aquatic" python $(CONFIG_KLIPPER_HOME)/lib/kconfiglib/menuconfig.py $(SRC)/scripts/Kconfig

