SHELL=/usr/bin/env bash

export KCONFIG_CONFIG ?= .config
include $(KCONFIG_CONFIG)

# kconfiglib/menuconfig doesn't like --output-sync, so we don't add it if it's the target or if .config doesn't exist yet
MAKEFLAGS += --jobs 16 $(if $(or $(findstring menuconfig,$(MAKECMDGOALS)),$(shell [ ! -e "$(KCONFIG_CONFIG)" ] && echo y)),,--output-sync)
# For quiet builds, override with make Q= for verbose output
Q ?= @

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
  CONFIG_KLIPPER_HOME ?= ~/klipper
endif

export SRC ?= $(CURDIR)

# Use the name of the 'name.config' file as the output directory, or 'out' if just '.config' is used
ifeq ($(basename $(KCONFIG_CONFIG)),)
  export OUT ?= $(CURDIR)/out
else
  export OUT ?= $(CURDIR)/out_$(basename $(KCONFIG_CONFIG))
endif

# Screen Colors
export OFF=\033[0m
export CYAN=\033[0;36m

export B_RED=\033[1;31m
export B_GREEN=\033[1;32m
export B_YELLOW=\033[1;33m

export C_INFO=$(CYAN)
export C_NOTICE=$(B_GREEN)
export C_WARNING=$(B_YELLOW)
export C_ERROR=$(B_RED)

hh_klipper_extras_files = $(patsubst extras/%,%,$(wildcard extras/*.py extras/*/*.py))
hh_old_klipper_modules = mmu.py mmu_toolhead.py # These will get removed upon install
hh_config_files = $(patsubst config/%,%,$(wildcard config/*.cfg config/**/*.cfg))
hh_moonraker_components = $(patsubst components/%,%,$(wildcard components/*.py))

# Files/targets that need to be build
build_targets = \
	$(OUT)/$(CONFIG_MOONRAKER_CONFIG_FILE) \
	$(OUT)/$(CONFIG_PRINTER_CONFIG_FILE) \
	$(addprefix $(OUT)/mmu/, $(hh_config_files)) \
	$(addprefix $(OUT)/klippy/extras/,$(hh_klipper_extras_files)) \
	$(addprefix $(OUT)/moonraker/components/,$(hh_moonraker_components)) \

# Files/targets that need to be installed
install_targets = \
	$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE) \
	$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_PRINTER_CONFIG_FILE) \
	$(addprefix $(CONFIG_KLIPPER_CONFIG_HOME)/mmu/, $(hh_config_files)) \
	$(addprefix $(CONFIG_MOONRAKER_HOME)/moonraker/component/, $(hh_moonraker_components)) \
	$(addprefix $(CONFIG_KLIPPER_HOME)/klippy/extras/, $(hh_klipper_extras_files))

# Recipe functions
install = \
	$(info Installing $(subst ",,$(2))...) \
	mkdir -p $(dir $(2)); \
	cp -rdf $(3) "$(1)" "$(2)";

link = \
	mkdir -p $(dir $(2)); \
	ln -sf "$(1)" "$(2)";

backup_ext :::= .old-$(shell date '+%Y%m%d-%H%M%S')
backup = \
	if [ -e "$(1)" ] && [ ! -e "$(addsuffix $(backup_ext),$(1))" ]; then \
		echo -e "$(C_NOTICE)Making a backup of '$(1)' to '$(addsuffix $(backup_ext),$(1))'$(OFF)"; \
		cp -a "$(1)" "$(addsuffix $(backup_ext),$(1))"; \
	fi

# Bool to check if moonraker needs to be restarted
restart_moonraker = 0

.DEFAULT_GOAL := build
.NOPARALLEL: install
.PHONY: update menuconfig install uninstall remove_old_klippy_modules check_root check_paths check_version diff build clean clean-all bnackup_mmu


### Build targets
ifneq ($(wildcard $(subst ",,$(KCONFIG_CONFIG))),) # To prevent make errors when .config is not yet created

# Copy existing moonraker.conf and printer.cfg to the out directory
$(OUT)/$(CONFIG_MOONRAKER_CONFIG_FILE):
	$(info Copying $(CONFIG_MOONRAKER_CONFIG_FILE) to '$(notdir $(OUT))' directory)
	$(Q)cp -a "$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE)" "$@" # Copy the current version to the out directory
	$(Q)$(SRC)/scripts/build.sh install-moonraker "$@"

$(OUT)/$(CONFIG_PRINTER_CONFIG_FILE):
	$(info Copying $(CONFIG_PRINTER_CONFIG_FILE) to '$(notdir $(OUT))' directory)
	$(Q)cp -a "$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_PRINTER_CONFIG_FILE)" "$@" # Copy the current version to the out directory
	$(Q)$(SRC)/scripts/build.sh install-includes "$@"

endif

# We link all config files, those that need to be updated will be written over in the install script
$(OUT)/mmu/%.cfg: $(SRC)/config/%.cfg
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

$(CONFIG_MOONRAKER_HOME)/%: $(OUT)/%
	$(Q)$(call install,$<,$@)

$(CONFIG_KLIPPER_CONFIG_HOME)/%: $(OUT)/%
	$(Q)$(call backup,$@)
	$(Q)$(call install,$<,$@)

$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE): $(OUT)/$(CONFIG_MOONRAKER_CONFIG_FILE)
	$(Q)$(call backup,$@)
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_moonraker = 1)

$(CONFIG_KLIPPER_CONFIG_HOME)/mmu/%: $(OUT)/mmu/% | backup_mmu
	$(Q)$(call install,$<,$@)

# Special case for mmu_vars.cfg, we don't want to overwrite it
$(CONFIG_KLIPPER_CONFIG_HOME)/mmu/mmu_vars.cfg: $(OUT)/mmu/mmu_vars.cfg | backup_mmu
	$(Q)$(call install,$<,$@,--update=none)

$(install_targets): build | update check_root

install: $(install_targets)
	$(Q)rm -rf $(addprefix $(CONFIG_KLIPPER_HOME)/klippy/extras,$(hh_old_klipper_modules))
	$(Q)[ "$(restart_moonraker)" -eq 0 ] || $(SRC)/scripts/build.sh restart-service "Moonraker" $(CONFIG_SERVICE_MOONRAKER)
	$(Q)$(SRC)/scripts/build.sh restart-service "Klipper" $(CONFIG_SERVICE_KLIPPER)
	$(Q)$(SRC)/scripts/build.sh print-happy-hare

backup_mmu: | build
	$(Q)$(call backup,$(CONFIG_KLIPPER_CONFIG_HOME)/mmu)


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

# Remove old klippy modules that are no longer needed
remove_old_modules:

clean:
	$(Q)rm -rf $(OUT)

distclean: clean
	$(Q)rm -f $(KCONFIG_CONFIG) $(KCONFIG_CONFIG).old

diff_cmd = git diff -U2 --color --src-prefix="current: " --dst-prefix="built: " --minimal --word-diff=color --stat --no-index -- "$(1)" "$(2)" | \
	grep -v "diff --git " | grep -Ev "index [[:xdigit:]]+\.\.[[:xdigit:]]+" || true # Filter out command and index lines from the diff, they only muck up the information

diff: | check_paths
ifeq ($(wildcard $(OUT)/mmu),)
	$(error No build directory found, exiting. Run 'make build' first)
endif
	$(Q)$(call diff_cmd,$(CONFIG_KLIPPER_CONFIG_HOME)/mmu,$(patsubst $(CURDIR)/%,%,$(OUT)/mmu))
	$(Q)$(call diff_cmd,$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_PRINTER_CONFIG_FILE),$(patsubst $(CURDIR)/%,%,$(OUT)/$(CONFIG_PRINTER_CONFIG_FILE)))
	$(Q)$(call diff_cmd,$(CONFIG_KLIPPER_CONFIG_HOME)/$(CONFIG_MOONRAKER_CONFIG_FILE),$(patsubst $(CURDIR)/%,%,$(OUT)/$(CONFIG_MOONRAKER_CONFIG_FILE)))

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
# If .config/$KCONFIG_CONFIG does not exist yet run menuconfig, else just update it
# touch in case .config does not get updated by olddefconfig.py
ifneq ($(wildcard $(KCONFIG_CONFIG)),)
	$(Q)python $(CONFIG_KLIPPER_HOME)/lib/kconfiglib/olddefconfig.py $(SRC)/scripts/Kconfig;
	$(Q)touch $(KCONFIG_CONFIG);
else ifneq ($(findstring menuconfig,$(MAKECMDGOALS)),menuconfig)
	$(Q)$(MAKE) -s menuconfig;
	$(Q)[ -e $(KCONFIG_CONFIG) ] || echo "No config file found. run 'make menuconfig' to create one" && false
endif

menuconfig: $(SRC)/scripts/Kconfig
	$(Q)MENUCONFIG_STYLE="aquatic" python $(CONFIG_KLIPPER_HOME)/lib/kconfiglib/menuconfig.py $(SRC)/scripts/Kconfig

