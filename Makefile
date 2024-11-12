SHELL=/usr/bin/env bash

# For parallel builds
MAKEFLAGS += --jobs 16
# kconfiglib/menuconfig doesn't like --output-sync, so we don't add it if it's the target
MAKEFLAGS += $(if $(findstring menuconfig,$(MAKECMDGOALS)),, --output-sync)
# For quiet builds, override with make Q= or verbose output
Q ?= @

export KCONFIG_CONFIG ?= .config

include $(KCONFIG_CONFIG)

# If CONFIG_IS_MIPS is not yet set by .config, set it here
ifeq ($(shell uname -m),mips)
  CONFIG_IS_MIPS ?= y
endif

# If CONFIG_KLIPPER_HOME is not yet set by .config, set it to the default value
# this is required to make menuconfig work the first time
# can be overridden with 'CONFIG_KLIPPER_HOME=/path/to/klipper make ...'
ifeq ($(CONFIG_IS_MIPS),y)
  CONFIG_KLIPPER_HOME ?= /usr/share/klipper
else
  CONFIG_KLIPPER_HOME ?= ${HOME}/klipper
endif

export SRC ?= $(CURDIR)
export OUT ?= $(CURDIR)/out

# Strings in .config are quoted, this removes the quotes so paths are handled properly
unquote = $(patsubst "%",%,$(1))

klipper_home = $(call unquote,$(CONFIG_KLIPPER_HOME))
hh_klipper_extras = $(wildcard extras/*.py extras/*/*.py )
hh_klipper_extras_dirs = $(sort $(dir $(hh_klipper_extras)))
hh_old_klipper_modules = mmu.py mmu_toolhead.py

klipper_config_home = $(call unquote,$(CONFIG_KLIPPER_CONFIG_HOME))
klipper_printer_file = $(call unquote, $(CONFIG_PRINTER_CONFIG))

hh_all_config_files = $(patsubst config/%, $(OUT)/config/mmu/%, $(wildcard config/*.cfg config/*/*.cfg))
hh_config_files = $(patsubst config/%, %, $(wildcard config/*.cfg config/*/*.cfg))
hh_config_dirs = $(sort $(dir $(hh_config_files)))

moonraker_home = $(call unquote,$(CONFIG_MOONRAKER_HOME))
hh_moonraker_components = $(wildcard components/*.py )

required_paths = $(klipper_home)/klippy/extras $(klipper_config_home) $(klipper_config_home)/$(klipper_printer_file) 
ifneq ($(CONFIG_OCTOPRINT),y)
  required_paths += $(moonraker_home)/moonraker/components
endif

backup_ext ?= .old-$(shell date '+%Y%m%d-%H%M%S')
backup = $(addsuffix $(backup_ext),$(1))

restart_moonraker = 0

.PHONY: update menuconfig install uninstall remove_old_klippy_modules check_root link_plugins backups build clean clean-all 


# Check whther the required paths exist
check_paths:
	@[ -f "$(klipper_config_home)/moonraker.conf" ] || { \
		echo -e "The file '$(klipper_config_home)/moonraker.conf' does not exist. Please check your config for the correct paths"; \
		exit 1; }
	@[ -f "$(klipper_config_home)/$(klipper_printer_file)" ] || { \
		echo -e "The file '$(klipper_config_home)/$(klipper_printer_file)' does not exist. Please check your config for the correct paths"; \
		exit 1; }
	@[ -d "$(klipper_home)/klippy/extras" ] || { \
		echo -e "The directory '$(klipper_home)/klippy/extras' does not exist. Please check your config for the correct paths"; \
		exit 1; }
ifneq ($(CONFIG_OCTOPRINT),y)
	@[ -d "$(moonraker_home)/moonraker/components" ] || { \
		echo -e "The directory '$(moonraker_home)/moonraker/components' does not exist. Please check your config for the correct paths"; \
		exit 1; }
endif

# $(OUT)/config/$(klipper_printer_file): | $(OUT)/config/#$(klipper_config_home)/$(klipper_printer_file) | $(OUT)/config/
# 	$(Q)cp -a "$<" "$@"

$(OUT)/config/moonraker.conf: | $(OUT)/config/
	$(Q)cp -a "$(klipper_config_home)/moonraker.conf" "$@" # Copy the current version to the out directory
	$(Q)$(SRC)/scripts/build.sh install-update-manager "$@"

$(OUT)/config/$(klipper_printer_file): | $(OUT)/config/
	$(Q)cp -a "$(klipper_config_home)/$(klipper_printer_file)" "$@" # Copy the current version to the out directory
	$(Q)$(SRC)/scripts/build.sh install-includes "$@"

# We link all config files, those that need to be copied will be written over in the install script
$(OUT)/config/mmu/%.cfg: $(SRC)/config/%.cfg | $(addprefix $(OUT)/config/mmu/,$(hh_config_dirs))
	$(Q)ln -sf "$(abspath $<)" "$@"
	$(Q)$(SRC)/scripts/build.sh build "$<" "$@"

$(OUT)/klippy/extras/%.py: $(SRC)/extras/%.py | $(addprefix $(OUT)/klippy/,$(hh_klipper_extras_dirs))
	$(Q)ln -sf "$(abspath $<)" "$@"

$(OUT)/moonraker/components/%.py: $(SRC)/components/%.py | $(OUT)/moonraker/components/
	$(Q)ln -sf "$(abspath $<)" "$@"

remove_old_modules:
	$(Q)cd $(klipper_home)/klippy/extras && rm -f $(hh_old_klipper_modules)

$(OUT):
	$(Q)mkdir -p "$@"

$(OUT)/%/: | $(OUT)
	$(Q)mkdir -p "$@"

$(call backup,%):
	@echo "Making a backup of '$(basename $@)' to '$@'"
	$(Q)cp -a "$(basename $@)" "$@"

build: $(KCONFIG_CONFIG) check_root check_paths check_version .config \
	$(OUT)/config/$(klipper_printer_file) \
	$(OUT)/config/moonraker.conf \
	$(addprefix $(OUT)/config/mmu/, $(hh_config_files)) \
	$(addprefix $(OUT)/klippy/,$(hh_klipper_extras)) \
	$(addprefix $(OUT)/moonraker/, $(hh_moonraker_components))

$(moonraker_home)/%: $(OUT)/% | build 
	@echo "Installing $@"
	$(Q)cp -rdf "$<" "$@"

$(klipper_home)/%: $(OUT)/% | build
	@echo "Installing $@"
	$(Q)mkdir -p $(dir $@)
	$(Q)cp -rdf "$<" "$@"

$(klipper_config_home)/mmu/%: $(OUT)/config/mmu/% | build $(call backup, $(wildcard $(klipper_config_home)/mmu))
	@echo "Installing $@"
	$(Q)mkdir -p $(dir $@)
	$(Q)cp -rdf "$<" "$@"

$(klipper_config_home)/mmu/mmu_vars.cfg: $(OUT)/config/mmu/mmu_vars.cfg | build
	@echo "Installing $@"
	$(Q)mkdir -p $(dir $@)
	$(Q)cp --update=none "$<" "$@"

$(klipper_config_home)/$(klipper_printer_file): $(OUT)/config/$(klipper_printer_file) | build 
	@$(MAKE) -s $(call backup, $(klipper_config_home)/$(klipper_printer_file))
	@echo "Installing $@"
	$(Q)cp -f "$<" "$@"

$(klipper_config_home)/moonraker.conf: $(OUT)/config/moonraker.conf | build 
	@$(MAKE) -s $(call backup, $(klipper_config_home)/moonraker.conf) 
	@echo "Installing $@"
	$(Q)cp -f "$<" "$@"
	$(eval restart_moonraker = 1)

install: check_root check_paths \
	$(klipper_config_home)/moonraker.conf \
	$(klipper_config_home)/$(klipper_printer_file) \
	$(addprefix $(klipper_config_home)/mmu/, $(hh_config_files)) \
	$(addprefix $(moonraker_home)/moonraker/, $(hh_moonraker_components)) \
	$(addprefix $(klipper_home)/klippy/, $(hh_klipper_extras)) \
	| remove_old_modules
	$(Q)[ "$(restart_moonraker)" -eq 0 ] || $(SRC)/scripts/build.sh restart-service "Moonraker" $(CONFIG_SERVICE_MOONRAKER)
	$(Q)$(SRC)/scripts/build.sh restart-service "Klipper" $(CONFIG_SERVICE_KLIPPER)
	$(Q)$(SRC)/scripts/build.sh print-happy-hare

uninstall: | $(call backup, $(wildcard $(klipper_config_home)/mmu)) $(call backup, $(klipper_config_home)/$(klipper_printer_file)) $(call backup, $(klipper_config_home)/moonraker.conf)
	$(Q)rm -f $(klipper_target_extras)
	$(Q)rm -f $(addprefix, $(moonraker_home)/, $(moonraker_components))
	$(Q)rm -rf $(klipper_config_home)/mmu
	$(Q)$(SRC)/scripts/build.sh uninstall
	$(Q)$(SRC)/scripts/build.sh restart-service "Moonraker" $(CONFIG_SERVICE_MOONRAKER)
	$(Q)$(SRC)/scripts/build.sh restart-service "Klipper" $(CONFIG_SERVICE_KLIPPER)
	$(Q)$(SRC)/scripts/build.sh print-unhappy-hare

clean: 
	$(Q)rm -rf $(OUT)

clean-all: clean
	$(Q)rm -rf $(KCONFIG_CONFIG)

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

$(KCONFIG_CONFIG): $(SRC)/scripts/Kconfig 
# 	If .config does not exist yet run menuconfig, else just update it
#	touch in case .config does not get updated by olddefconfig.py
	@if [ -f $(KCONFIG_CONFIG) ]; then \
		python $(klipper_home)/lib/kconfiglib/olddefconfig.py $(SRC)/scripts/Kconfig; \
		touch $(KCONFIG_CONFIG); \
	elif [[ ! "$(MAKECMDGOALS)" =~ menuconfig ]]; then \
		$(MAKE) MAKEFLAGS= menuconfig; \
		[ -f $(KCONFIG_CONFIG) ] || { echo "No $(KCONFIG_CONFIG) file found, exiting. Run 'make menuconfig' to create a config file"; exit 1; }; \
	fi

menuconfig: $(SRC)/scripts/Kconfig 
	@MENUCONFIG_STYLE="aquatic" python ${klipper_home}/lib/kconfiglib/menuconfig.py $(SRC)/scripts/Kconfig


genconfig: $(SRC)/scripts/Kconfig 
	@python ${klipper_home}/lib/kconfiglib/genconfig.py $(SRC)/scripts/Kconfig --config-out test.config



