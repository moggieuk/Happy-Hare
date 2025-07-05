SHELL:=/usr/bin/env sh
PY:=python
MAKEFLAGS += --jobs 16 # Parallel build

# Print Colors (exported for use in py installer)
ifneq ($(shell which tput 2>/dev/null),)
  export C_OFF:=$(shell tput -Txterm-256color sgr0)
  export C_DEBUG:=$(shell tput -Txterm-256color setaf 5)
  export C_INFO:=$(shell tput -Txterm-256color setaf 6)
  export C_NOTICE:=$(shell tput -Txterm-256color setaf 2)
  export C_WARNING:=$(shell tput -Txterm-256color setaf 3)
  export C_ERROR:=$(shell tput -Txterm-256color setaf 1)
endif

# Prevent the user from running with sudo. This isn't perfect if something else than sudo is used.
# Just checking for root isn't enough, as users on Creality K1 printers usually run as root (ugh)
ifneq ($(SUDO_COMMAND),) 
  $(error $(C_ERROR)Please do not run with sudo$(C_OFF))
endif

export KCONFIG_CONFIG ?= .config
include $(KCONFIG_CONFIG)

Q ?= @ # For quiet builds, override with make Q= for verbose output
UT ?= * # For unittests, e.g. make UT=test_build.py test
ifneq ($(strip $(Q)),@)
  V ?= -v # For verbose output of build.py
endif

SRC ?= $(CURDIR)
# Use the name of the 'name.config' file as the out_name directory, or 'out' if just '.config' is used
ifeq ($(basename $(notdir $(KCONFIG_CONFIG))),)
  OUT ?= $(CURDIR)/out
else
  OUT ?= $(CURDIR)/out_$(subst .config.,,$(notdir $(KCONFIG_CONFIG)))
endif

export IN=$(OUT)/in
export PYTHONPATH:=$(SRC)/installer/lib/kconfiglib:$(PYTHONPATH)

# replace ~ with $(HOME) and remove quotes
unwrap = $(subst ~,$(HOME),$(patsubst "%",%,$(1)))
KLIPPER_HOME = $(call unwrap,$(CONFIG_KLIPPER_HOME))
KLIPPER_CONFIG_HOME := $(call unwrap,$(CONFIG_KLIPPER_CONFIG_HOME))
MOONRAKER_HOME := $(call unwrap,$(CONFIG_MOONRAKER_HOME))
PRINTER_CONFIG_FILE := $(call unwrap,$(CONFIG_PRINTER_CONFIG_FILE))
MOONRAKER_CONFIG_FILE := $(call unwrap,$(CONFIG_MOONRAKER_CONFIG_FILE))
BUILD_MODULE:=$(PY) -m installer.build $(V)

hh_klipper_extras_files = $(patsubst extras/%,%,$(wildcard extras/*.py extras/*/*.py))
hh_old_klipper_modules = mmu.py mmu_toolhead.py # These will get removed upon install
hh_config_files = $(patsubst config/%,%,$(wildcard config/*.cfg config/**/*.cfg))
hh_moonraker_components = $(patsubst components/%,%,$(wildcard components/*.py))

# use sudo if the klipper home is at a system location
SUDO:=$(shell [ -d $(KLIPPER_HOME) ] && [ "$$(stat -c %u $(KLIPPER_HOME))" != "$$(id -u)" ] && echo "sudo " || echo "")

# Look for installed configs that would need be parsed by the build script
cfg_addons = $(wildcard $(KLIPPER_CONFIG_HOME)/mmu/addons/*_hw.cfg)
cfg_base = $(wildcard $(addprefix $(KLIPPER_CONFIG_HOME)/mmu/, \
				base/mmu.cfg \
				base/mmu_parameters.cfg \
				base/mmu_hardware.cfg \
				base/mmu_macro_vars.cfg))
hh_configs_to_parse = $(subst $(KLIPPER_CONFIG_HOME),$(IN),$(cfg_base) $(cfg_addons))

# Files/targets that need to be build
build_targets = \
	$(OUT)/$(MOONRAKER_CONFIG_FILE) \
	$(OUT)/$(PRINTER_CONFIG_FILE) \
	$(addprefix $(OUT)/mmu/, $(hh_config_files)) \
	$(addprefix $(OUT)/klippy/extras/,$(hh_klipper_extras_files)) \
	$(addprefix $(OUT)/moonraker/components/,$(hh_moonraker_components)) 

# Files/targets that need to be installed
install_targets = \
	$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE) \
	$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE) \
	$(addprefix $(KLIPPER_CONFIG_HOME)/mmu/, $(hh_config_files)) \
	$(addprefix $(KLIPPER_HOME)/klippy/extras/, $(hh_klipper_extras_files)) \
	$(addprefix $(MOONRAKER_HOME)/moonraker/components/, $(hh_moonraker_components))


# Recipe functions
install = \
	$(info $(C_INFO)Installing $(2)...$(C_OFF)) \
	$(SUDO)mkdir -p $(dir $(2)); \
	$(SUDO)cp -af $(3) "$(1)" "$(2)";

link = \
	mkdir -p $(dir $(2)); \
	ln -sf "$(abspath $(1))" "$(2)";

backup_ext ::= .old-$(shell date '+%Y%m%d-%H%M%S')
backup_name = $(addsuffix $(backup_ext),$(1))
backup = \
	if [ -e "$(1)" ] && [ ! -e "$(call backup_name,$(1))" ]; then \
		echo "$(C_NOTICE)Making a backup of '$(1)' to '$(call backup_name,$(1))'$(C_OFF)"; \
		$(SUDO)cp -a "$(1)" "$(call backup_name,$(1))"; \
	fi

restart_service = \
	if [ "${F_NOSERVICE:-0}" -eq 1 ]; then \
		echo "$(C_INFO)Skipping restart of $(2) service$(C_OFF)"; \
	else \
		[ "$(1)" -eq 0 ] || $(BUILD_MODULE) --restart-service "$(2)" $(3) "$(KCONFIG_CONFIG)"; \
	fi

# Bool to check if moonraker/klipper needs to be restarted
restart_moonraker = 0
restart_klipper = 0

.SECONDEXPANSION:
.DEFAULT_GOAL := build
.PRECIOUS: $(KCONFIG_CONFIG)
.PHONY: update menuconfig install uninstall check_root check_version diff test build clean
.SECONDARY: $(call backup_name,$(KLIPPER_CONFIG_HOME)/mmu) \
	$(call backup_name,$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE)) \
	$(call backup_name,$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE))

##### Build targets #####

# Link existing config files to the out/in directory to break circular dependency
$(IN)/%:
	$(Q)[ -f "$(KLIPPER_CONFIG_HOME)/$*" ] || { echo "The file '$(KLIPPER_CONFIG_HOME)/$*' does not exist. Please check your config for the correct paths"; exit 1; }
	$(Q)$(call link,$(KLIPPER_CONFIG_HOME)/$*,$@)

ifneq ($(wildcard $(KCONFIG_CONFIG)),) # To prevent make errors when .config is not yet created

# Copy existing moonraker.conf and printer.cfg to the out directory
$(OUT)/$(MOONRAKER_CONFIG_FILE): $(IN)/$$(@F) 
	$(info $(C_INFO)Copying $(MOONRAKER_CONFIG_FILE) to '$(notdir $(OUT))' directory$(C_OFF))
	$(Q)cp -aL "$<" "$@" # Copy the current version to the out directory
	$(Q)chmod +w "$@" # Make sure the file is writable
	$(Q)$(BUILD_MODULE) --install-moonraker "$(SRC)/moonraker_update.txt" "$@" "$(KCONFIG_CONFIG)"

$(OUT)/$(PRINTER_CONFIG_FILE): $(IN)/$$(@F) 
	$(info $(C_INFO)Copying $(PRINTER_CONFIG_FILE) to '$(notdir $(OUT))' directory$(C_OFF))
	$(Q)cp -aL "$<" "$@" # Copy the current version to the out directory
	$(Q)chmod +w "$@" # Make sure the file is writable
	$(Q)$(BUILD_MODULE) --install-includes "$@" "$(KCONFIG_CONFIG)"

# We link all config files, those that need to be updated will be written over in the install script
$(OUT)/mmu/%.cfg: $(SRC)/config/%.cfg $(hh_configs_to_parse)
	$(Q)$(call link,$<,$@)
	$(Q)$(BUILD_MODULE) --build "$<" "$@" "$(KCONFIG_CONFIG)" $(hh_configs_to_parse)

# Python files are linked to the out directory
$(OUT)/klippy/extras/%.py: $(SRC)/extras/%.py
	$(Q)$(call link,$<,$@)

$(OUT)/moonraker/components/%.py: $(SRC)/components/%.py
	$(Q)$(call link,$<,$@)

$(OUT):
	$(Q)mkdir -p "$@"

$(build_targets): $(KCONFIG_CONFIG) | $(OUT) update check_version 

build: $(build_targets)


##### Install targets #####

# Check whether the required paths exist
$(KLIPPER_HOME)/klippy/extras $(MOONRAKER_HOME)/moonraker/components:
	$(error The directory '$@' does not exist. Please check your config for the correct paths)

$(KLIPPER_HOME)/%: $(OUT)/% | $(KLIPPER_HOME)/klippy/extras
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_klipper = 1)

$(MOONRAKER_HOME)/%: $(OUT)/% | $(MOONRAKER_HOME)/moonraker/components
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_moonraker = 1)

$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE): $(OUT)/$$(@F) | $(call backup_name,$$@)
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_klipper = 1)

$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE): $(OUT)/$$(@F) | $(call backup_name,$$@)
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_moonraker = 1)

$(KLIPPER_CONFIG_HOME)/mmu/%.cfg: $(OUT)/mmu/%.cfg | $(call backup_name,$(KLIPPER_CONFIG_HOME)/mmu) 
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_klipper = 1)

$(KLIPPER_CONFIG_HOME)/mmu/mmu_vars.cfg: | $(OUT)/mmu/mmu_vars.cfg $(call backup_name,$(KLIPPER_CONFIG_HOME)/mmu) 
	$(Q)$(call install,$(OUT)/mmu/mmu_vars.cfg,$@,--no-clobber)
	$(Q)$(eval restart_klipper = 1)

$(call backup_name,$(KLIPPER_CONFIG_HOME)/%): $(OUT)/% | build
	$(Q)$(call backup,$(basename $@))

$(call backup_name,$(KLIPPER_CONFIG_HOME)/mmu): $(addprefix $(OUT)/mmu/, $(hh_config_files)) | build
	$(Q)$(call backup,$(basename $@))

endif

$(install_targets): build

install: $(install_targets)
	$(Q)rm -rf $(addprefix $(KLIPPER_HOME)/klippy/extras,$(hh_old_klipper_modules))
	$(Q)$(call restart_service,$(restart_moonraker),Moonraker,$(CONFIG_SERVICE_MOONRAKER))
	$(Q)$(call restart_service,$(restart_klipper),Klipper,$(CONFIG_SERVICE_KLIPPER))
	$(Q)$(BUILD_MODULE) --print-happy-hare "Done! Happy Hare $(CONFIG_F_VERSION) is ready!"

uninstall:
	$(Q)$(call backup,$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE))
	$(Q)$(call backup,$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE))
	$(Q)$(call backup,$(KLIPPER_CONFIG_HOME)/mmu)
	$(Q)rm -rf $(addprefix $(KLIPPER_HOME)/klippy/extras/,$(hh_klipper_extras_files) $(filter-out ./,$(dir $(hh_klipper_extras_files))))
	$(Q)rm -rf $(addprefix $(MOONRAKER_HOME)/moonraker/components/,$(hh_moonraker_components) $(filter-out ./,$(dir $(hh_moonraker_components))))
	$(Q)rm -rf $(KLIPPER_CONFIG_HOME)/mmu
	$(Q)$(BUILD_MODULE) --uninstall-moonraker $(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE)
	$(Q)$(BUILD_MODULE) --uninstall-includes $(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE)
	$(Q)$(call restart_service,1,Moonraker,$(CONFIG_SERVICE_MOONRAKER))
	$(Q)$(call restart_service,1,Klipper,$(CONFIG_SERVICE_KLIPPER))
	$(Q)$(BUILD_MODULE) --print-unhappy-hare "Done... Very unHappy Hare."


##### Misc targets #####

update: 
	$(Q)$(SRC)/installer/self_update.sh

clean:
	$(Q)rm -rf $(OUT)

diff=\
	 git diff -U2 --color --src-prefix="current: " --dst-prefix="built: " --minimal --word-diff=color --stat --no-index -- "$(1)" "$(2)" | \
        grep -v "diff --git " | \
		grep -Ev "index [[:xdigit:]]+\.\.[[:xdigit:]]+" || true;

diff: | build
	$(Q)$(call diff,$(KLIPPER_CONFIG_HOME)/mmu,$(patsubst $(SRC)/%,%,$(OUT)/mmu))
	$(Q)$(call diff,$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE),$(patsubst $(SRC)/%,%,$(OUT)/$(PRINTER_CONFIG_FILE)))
	$(Q)$(call diff,$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE),$(patsubst $(SRC)/%,%,$(OUT)/$(MOONRAKER_CONFIG_FILE)))

test: 
	$(Q)$(PY) -m unittest discover $(V) -p '$(UT)'

check_version:
	$(Q)$(BUILD_MODULE) --check-version "$(KCONFIG_CONFIG)" $(hh_configs_to_parse)  

$(KCONFIG_CONFIG): $(SRC)/installer/Kconfig* $(SRC)/installer/**/Kconfig* 
# if KCONFIG_CONFIG is outdated or doesn't exist run menuconfig first. If the user doesn't save the config, we will update it with olddefconfig
# touch in case .config does not get updated by olddefconfig.py
ifneq ($(findstring menuconfig,$(MAKECMDGOALS)),menuconfig) # only if menuconfig is not the target, else it will run twice
	$(Q)$(MAKE) MAKEFLAGS= menuconfig
	$(Q)$(PY) -m olddefconfig $(SRC)/installer/Kconfig >/dev/null # Always update the .config file in case the user doesn't save it
	$(Q)touch $(KCONFIG_CONFIG)
endif

menuconfig: $(SRC)/installer/Kconfig
	$(Q)MENUCONFIG_STYLE="aquatic" $(PY) -m menuconfig $(SRC)/installer/Kconfig

