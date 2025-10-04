SHELL := /usr/bin/env sh
PY    := python
Q  ?= @                           # For quiet make builds, override with make Q= for verbose output
V  ?=                             # For verbose output (mostly from python builder), set to -v to enable
UT ?= *                           # For unittests, e.g. make UT=test_build.py test

MAKEFLAGS += --jobs 8             # Parallel build

# By default KCONFIG_CONFIG is '.config', but it can be overridden by the user
export KCONFIG_CONFIG ?= .config

# Enable output-sync if menuconfig will not be trigger. menuconfig.py will crash if output-sync is enabled on certain systems
ifeq ($(CHECK_OUTPUT_SYNC),)
  # Never enable output-sync for menuconfig
  ifeq ($(findstring menuconfig,$(MAKECMDGOALS)),)
    # Check whether $KCONFIG_CONFIG is outdated. if so menuconfig will be triggered and output-sync should stay disabled
    ifeq ($(shell $(MAKE) CHECK_OUTPUT_SYNC=y -q $(KCONFIG_CONFIG) > /dev/null && echo y),y)
      MAKEFLAGS += --output-sync
    endif
  endif
  -include $(KCONFIG_CONFIG) # Won't exist on first invocation
endif

# Prevent the user from running with sudo. This isn't perfect if something else than sudo is used.
# Just checking for root isn't enough, as users on Creality K1 printers usually run as root (ugh)
ifneq ($(SUDO_COMMAND),) 
  $(error $(C_ERROR)Please do not run with sudo$(C_OFF))
endif

# Print Colors (exported for use in py installer)
ifneq ($(shell command -v tput 2>/dev/null),)
  export C_OFF     := $(shell tput -Txterm-256color sgr0)
  export C_DEBUG   := $(shell tput -Txterm-256color setaf 5)
  export C_INFO    := $(shell tput -Txterm-256color setaf 6)
  export C_NOTICE  := $(shell tput -Txterm-256color bold; tput -Txterm-256color setaf 2)
  export C_WARNING := $(shell tput -Txterm-256color setaf 3)
  export C_ERROR   := $(shell tput -Txterm-256color bold; tput -Txterm-256color setaf 1)
endif

# Couple verbose debug output to python debugging flag
debug = $(if $(findstring -v,$(V)),$(info $(1)))


export SRC ?= $(CURDIR)
# export $srctree for menuconfig and kconfiglib
export srctree := $(SRC)/installer
export PYTHONPATH:=$(SRC)/installer/lib/kconfiglib:$(PYTHONPATH)

ifneq ($(TESTDIR),)
  OUTDIR := $(TESTDIR)
else
  OUTDIR := $(CURDIR)
endif

export OUT ?= $(OUTDIR)/out
export IN  := $(OUT)/in

# Default unit and mcu naming
export UNIT_NAME ?= mmu0
export MMU_MCU ?= mmu0

# Helper functions/constants
comma := ,
empty :=
space := $(empty) $(empty)

# Replace ~ with $(HOME) and remove quotes
unwrap                   = $(subst ~,$(HOME),$(patsubst "%",%,$(1)))
space_to_underscore      = $(subst $(space),_,$(1))
comma_to_space           = $(subst $(comma),$(space),$(1))
# Convert a comma separated list to a space separated list with spaces replaced with underscores
convert_list             = $(call comma_to_space,$(call space_to_underscore,$(1)))

# We strip these from surrounding quotes, and replace ~ with $(HOME)
KLIPPER_HOME            := $(call unwrap,$(CONFIG_KLIPPER_HOME))
KLIPPER_CONFIG_HOME     := $(call unwrap,$(CONFIG_KLIPPER_CONFIG_HOME))
MOONRAKER_HOME          := $(call unwrap,$(CONFIG_MOONRAKER_HOME))
PRINTER_CONFIG_FILE     := $(call unwrap,$(CONFIG_PRINTER_CONFIG_FILE))
MOONRAKER_CONFIG_FILE   := $(call unwrap,$(CONFIG_MOONRAKER_CONFIG_FILE))

# unit_names: from CONFIG_PARAM_MMU_UNITS in multi-unit, else default to mmu0
unit_names := $(if $(filter y,$(CONFIG_MULTI_UNIT)),$(strip $(subst ",,$(call convert_list,$(CONFIG_PARAM_MMU_UNITS)))),mmu0)

# Use sudo if the klipper home is at a system location (not owned by user)
SUDO := $(shell \
	  [ -n "$(KLIPPER_HOME)" ] && \
	  [ -d "$(KLIPPER_HOME)" ] && \
	  [ "$$(ls -nd -- $(KLIPPER_HOME) | awk '{print $$3}')" != "$$(id -u)" ] && \
	  echo "sudo " || echo "" \
	)

# Bool to check if moonraker/klipper needs to be restarted
restart_moonraker = 0
restart_klipper = 0

.SECONDEXPANSION:
.DEFAULT_GOAL := build
.PRECIOUS: $(KCONFIG_CONFIG)
.PHONY: menuconfig install uninstall check_version diff test build clean variables
.SECONDARY: \
	$(call backup_name,$(KLIPPER_CONFIG_HOME)/mmu) \
	$(call backup_name,$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE)) \
	$(call backup_name,$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE))
.NOTPARALLEL: clean



#####################
##### File sets #####
#####################

hh_klipper_extras_files := $(wildcard extras/*.py extras/mmu/*.py)
hh_old_klipper_modules  := mmu.py mmu_toolhead.py # These will get removed upon install
hh_moonraker_components := $(wildcard components/*.py)

# All repo configs files less mmu_vars.cfg
repo_cfgs := \
	$(patsubst config/%,%, $(wildcard config/*.cfg config/**/*.cfg))

# Per-unit files: <unit>_{hardware,parameters}.cfg
hh_unit_config_files := \
	$(addprefix base/,$(addsuffix _hardware.cfg,$(unit_names))) \
	$(addprefix base/,$(addsuffix _parameters.cfg,$(unit_names)))

# Final config set: all repo cfgs (minus the single-unit defaults) + per-unit files
hh_config_files := \
	$(filter-out base/mmu_hardware.cfg base/mmu_parameters.cfg,$(repo_cfgs)) \
	$(hh_unit_config_files)

# Look for installed configs that would need be parsed by the build script
# This allows for easy upgrades and option movement across files
hh_configs_to_parse := \
	$(subst $(KLIPPER_CONFIG_HOME),$(IN), \
	$(wildcard $(KLIPPER_CONFIG_HOME)/mmu/base/*.cfg \
		$(KLIPPER_CONFIG_HOME)/mmu/addons/*.cfg))

# Files/targets that need to be build
build_targets := \
	$(addprefix $(OUT)/mmu/, $(hh_config_files)) \
	$(addprefix $(OUT)/klipper/, $(hh_klipper_extras_files)) \
	$(addprefix $(OUT)/moonraker/, $(hh_moonraker_components)) \
        $(OUT)/$(MOONRAKER_CONFIG_FILE) \
        $(OUT)/$(PRINTER_CONFIG_FILE)

# Subset of files/targets which require token processing and/or are user editable
processed_targets := \
	$(filter-out $(OUT)/mmu/optional/%.cfg $(OUT)/mmu/macros/%.cfg $(OUT)/mmu/mmu_vars.cfg, \
		$(filter $(OUT)/mmu/%.cfg, $(addprefix $(OUT)/mmu/,$(hh_config_files))))

# Files/targets that need to be installed
install_targets := \
	$(addprefix $(KLIPPER_CONFIG_HOME)/mmu/, $(hh_config_files)) \
	$(addprefix $(KLIPPER_HOME)/, $(hh_klipper_extras_files)) \
	$(addprefix $(MOONRAKER_HOME)/moonraker/, $(hh_moonraker_components)) \
	$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE) \
	$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE)



############################
##### Recipe functions #####
############################

install = \
	$(info $(C_INFO)Installing $(2)$(C_OFF)) \
	$(SUDO)mkdir -p $(dir $(2)); \
	$(SUDO)cp -rPf $(3) "$(1)" "$(2)";

link = \
	mkdir -p $(dir $(2)); \
	ln -sf "$(abspath $(1))" "$(2)";

copy = \
	mkdir -p $(dir $(2)); \
	cp -aL "$(1)" "$(2)" && chmod +w "$(2)"

strip_prefix = $(patsubst $(1)%,%,$(2))

backup_ext  := .old-$(shell date '+%Y%m%d-%H%M%S')
backup_name = $(addsuffix $(backup_ext),$(1))
backup = \
	if [ -e "$(1)" ] && [ ! -e "$(call backup_name,$(1))" ]; then \
	  echo "$(C_INFO)Making a backup of '$(1)' to '$(notdir $(call backup_name,$(1)))'$(C_OFF)"; \
	  $(SUDO)cp -a "$(1)" "$(call backup_name,$(1))"; \
	fi

restart_service = \
	if [ "$(F_NO_SERVICE)" ]; then \
	  echo "$(C_WARNING)Skipping restart of $(2) service$(C_OFF)"; \
	else \
	  [ "$(1)" -eq 0 ] || $(PY) -m installer.build $(V) --restart-service "$(2)" $(3) "$(KCONFIG_CONFIG)"; \
	fi



#########################
##### Build targets #####
#########################

# Link existing config files to the out/in directory to break circular dependency
$(IN)/%:
	$(Q)[ -f "$(KLIPPER_CONFIG_HOME)/$*" ] || { echo "$(C_ERROR)The file '$(KLIPPER_CONFIG_HOME)/$*' does not exist. Please check your config for the correct paths$(C_OFF)"; exit 1; }
	$(call debug,$(C_DEBUG)Linking $(KLIPPER_CONFIG_HOME)/$* to '$(notdir $(IN))' directory$(C_OFF))
	$(Q)$(call link,$(KLIPPER_CONFIG_HOME)/$*,$@)

ifneq ($(strip $(MOONRAKER_CONFIG_FILE)),)
# Copy existing moonraker.conf to the out directory and update with moonraker_update.txt
$(OUT)/$(MOONRAKER_CONFIG_FILE): $(IN)/$$(@F)
	$(call debug,$(C_DEBUG)Copying $< to '$(notdir $(OUT))' directory$(C_OFF))
	$(Q)$(call copy,$<,$@)
	$(Q)$(PY) -m installer.build $(V) --install-moonraker "$(SRC)/installer/moonraker_update.txt" "$@" "$(KCONFIG_CONFIG)"
endif

ifneq ($(strip $(PRINTER_CONFIG_FILE)),)
# Copy existing printer.cfg to the out directory and update with includes
$(OUT)/$(PRINTER_CONFIG_FILE): $(IN)/$$(@F)
	$(call debug,$(C_DEBUG)Copying $< to '$(notdir $(OUT))' directory$(C_OFF))
	$(Q)$(call copy,$<,$@)
	$(Q)$(PY) -m installer.build $(V) --install-includes "$@" "$(KCONFIG_CONFIG)"
endif

# We link all config files, those that need to be updated will be written over in the install script,
# in case of a multi unit setup, the per-unit config targets are overridden below
$(OUT)/mmu/%.cfg: $(SRC)/config/%.cfg $(hh_configs_to_parse)
	$(Q)$(call link,$<,$@)
	$(Q)$(if $(filter $@,$(processed_targets)), \
		$(PY) -m installer.build $(V) --build "$<" "$@" "$(KCONFIG_CONFIG)" $(hh_configs_to_parse), \
	        $(info $(C_INFO)Skipping build of mmu/$*$(C_OFF)))

# Conditional per-unit prereqiusit ".config_<unit>" when multi-unit, else to ".config"
KCONF_REQ = $$(if $$(filter y,$$(CONFIG_MULTI_UNIT)),$$(KCONFIG_CONFIG)_%,)

# Shared target rules don't work on old make so separate for portability
$(OUT)/mmu/base/%_hardware.cfg: \
  $$(SRC)/config/base/mmu_hardware.cfg $$(hh_configs_to_parse) $(KCONF_REQ)
	$(Q)$(call link,$<,$@)
	$(Q)$(PY) -m installer.build $(V) --build "$<" "$@" \
		"$(if $(filter y,$(CONFIG_MULTI_UNIT)),$(KCONFIG_CONFIG)_$*,$(KCONFIG_CONFIG))" $(hh_configs_to_parse)

$(OUT)/mmu/base/%_parameters.cfg: \
  $$(SRC)/config/base/mmu_parameters.cfg $$(hh_configs_to_parse) $(KCONF_REQ)
	$(Q)$(call link,$<,$@)
	$(Q)$(PY) -m installer.build $(V) --build "$<" "$@" \
		"$(if $(filter y,$(CONFIG_MULTI_UNIT)),$(KCONFIG_CONFIG)_$*,$(KCONFIG_CONFIG))" $(hh_configs_to_parse)

# Python files are linked to the out directory
$(OUT)/klipper/extras/%.py: $(SRC)/extras/%.py
	$(Q)$(call link,$<,$@)

$(OUT)/moonraker/components/%.py: $(SRC)/components/%.py
	$(Q)$(call link,$<,$@)

$(OUT):
	$(Q)mkdir -p "$@"

$(build_targets): $(KCONFIG_CONFIG) | $(OUT) check_version 

build: $(build_targets)



###########################
##### Install targets #####
###########################

# Check whether the required paths exist
$(KLIPPER_HOME) $(MOONRAKER_HOME)/moonraker/components:
	$(error The directory '$@' does not exist. Please check your config for the correct paths)

# Install python files for klipper
$(KLIPPER_HOME)/%: $(OUT)/klipper/% | $(KLIPPER_HOME)
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_klipper = 1)

# Install python files for moonraker
$(MOONRAKER_HOME)/%: $(OUT)/% | $(MOONRAKER_HOME)/moonraker/components
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_moonraker = 1)

ifneq ($(strip $(MOONRAKER_CONFIG_FILE)),)
# Install moonraker.conf
$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE): $(OUT)/$$(@F) | $(call backup_name,$$@)
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_moonraker = 1)
endif

ifneq ($(strip $(PRINTER_CONFIG_FILE)),)
# Install printer.cfg
$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE): $(OUT)/$$(@F) | $(call backup_name,$$@)
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_klipper = 1)
endif

# Install Happy-Hare *.cfg files
$(KLIPPER_CONFIG_HOME)/mmu/%.cfg: $(OUT)/mmu/%.cfg | $(call backup_name,$(KLIPPER_CONFIG_HOME)/mmu) 
	$(Q)$(call install,$<,$@)
	$(Q)$(eval restart_klipper = 1)

# Special recipe for mmu_vars.cfg, so it doesn't overwrite an existing mmu_vars.cfg
# Avoiding use of non-POSIX $(Q)$(call install,$(firstword $|),$@,--no-clobber)
$(KLIPPER_CONFIG_HOME)/mmu/mmu_vars.cfg: | $(OUT)/mmu/mmu_vars.cfg $(call backup_name,$(KLIPPER_CONFIG_HOME)/mmu)
	$(Q)$(SUDO)mkdir -p "$(dir $@)"
	$(Q)[ -f "$@" ] || $(SUDO)cp -p "$(firstword $|)" "$@"
	$(Q)$(eval restart_klipper = 1)

# Recipe to backup printer.cfg and moonraker.conf before installing
$(call backup_name,$(KLIPPER_CONFIG_HOME)/%): $(OUT)/% | build
	$(Q)$(call backup,$(basename $@))

# Recipe to backup Happy-Hare configs before installing
$(call backup_name,$(KLIPPER_CONFIG_HOME)/mmu): $(addprefix $(OUT)/mmu/, $(hh_config_files)) | build
	$(Q)$(call backup,$(basename $@))


$(install_targets): build

install: $(install_targets)
	$(Q)rm -rf $(addprefix $(KLIPPER_HOME)/klippy/extras/,$(hh_old_klipper_modules))
	$(Q)$(call restart_service,$(restart_moonraker),Moonraker,$(CONFIG_SERVICE_MOONRAKER))
	$(Q)$(call restart_service,$(restart_klipper),Klipper,$(CONFIG_SERVICE_KLIPPER))
	$(Q)$(PY) -m installer.build $(V) --print-happy-hare "Done! Happy Hare $(CONFIG_F_VERSION) is ready!"

uninstall:
	$(Q)$(if $(MOONRAKER_CONFIG_FILE), \
		$(call backup,$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE)))
	$(Q)$(if $(PRINTER_CONFIG_FILE), \
		$(call backup,$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE)))
	$(Q)$(call backup,$(KLIPPER_CONFIG_HOME)/mmu)
	@# Remove the installed files
	$(Q)rm -f $(addprefix $(KLIPPER_HOME)/klippy/,$(hh_klipper_extras_files))
	$(Q)rmdir --ignore-fail-on-non-empty $(addprefix $(KLIPPER_HOME)/klippy/, \
		$(filter-out extras/,$(dir $(hh_klipper_extras_files)))) 2>/dev/null || true
	$(Q)rm -f $(addprefix $(MOONRAKER_HOME)/moonraker/,$(hh_moonraker_components))
	$(Q)rmdir --ignore-fail-on-non-empty $(addprefix $(MOONRAKER_HOME)/moonraker/, \
		$(filter-out components/,$(dir $(hh_moonraker_components)))) 2>/dev/null || true
	$(Q)rm -rf $(KLIPPER_CONFIG_HOME)/mmu
	@# Remove HH from config files
	$(Q)$(PY) -m installer.build $(V) --uninstall-moonraker $(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE)
	$(Q)$(PY) -m installer.build $(V) --uninstall-includes $(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE)
	@# Restart services if needed
	$(Q)$(call restart_service,1,Moonraker,$(CONFIG_SERVICE_MOONRAKER))
	$(Q)$(call restart_service,1,Klipper,$(CONFIG_SERVICE_KLIPPER))
	$(Q)$(PY) -m installer.build $(V) --print-unhappy-hare "Done. Very unHappy Hare."



########################
##### Misc targets #####
########################

# Look for version number in current config files and report
check_version: $(hh_configs_to_parse)
	$(Q)$(PY) -m installer.build $(V) --check-version "$(KCONFIG_CONFIG)" $(hh_configs_to_parse)

clean:
	$(Q)rm -rf $(OUT)

diff= \
	git diff -U2 --color --src-prefix="current: " --dst-prefix="built: " --minimal --word-diff=color --stat --no-index -- "$(1)" "$(2)" | \
	grep -v "diff --git " | \
	grep -Ev "index [[:xdigit:]]+\.\.[[:xdigit:]]+" || true;

diff: | build
	$(Q)$(call diff,$(KLIPPER_CONFIG_HOME)/mmu,$(patsubst $(SRC)/%,%,$(OUT)/mmu))
	$(Q)$(call diff,$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE),$(patsubst $(SRC)/%,%,$(OUT)/$(PRINTER_CONFIG_FILE)))
	$(Q)$(call diff,$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE),$(patsubst $(SRC)/%,%,$(OUT)/$(MOONRAKER_CONFIG_FILE)))

test: 
	$(Q)$(PY) -m unittest discover $(V) -p '$(UT)'

variables:
	@echo "========================="
	@echo "$(C_NOTICE)hh_klipper_extras_files  =$(C_INFO) $(hh_klipper_extras_files)$(C_OFF)"
	@echo "$(C_NOTICE)hh_old_klipper_modules   =$(C_INFO) $(hh_old_klipper_modules)$(C_OFF)"
	@echo "$(C_NOTICE)hh_moonraker_components  =$(C_INFO) $(hh_moonraker_components)$(C_OFF)"
	@echo "$(C_NOTICE)repo_cfgs                =$(C_INFO) $(repo_cfgs)$(C_OFF)"
	@echo "$(C_NOTICE)unit_names               =$(C_INFO) $(unit_names)$(C_OFF)"
	@echo "$(C_NOTICE)hh_unit_config_files     =$(C_INFO) $(hh_unit_config_files)$(C_OFF)"
	@echo "$(C_NOTICE)hh_config_files          =$(C_INFO) $(hh_config_files)$(C_OFF)"
	@echo "$(C_NOTICE)hh_configs_to_parse      =$(C_INFO) $(hh_configs_to_parse)$(C_OFF)"
	@echo "$(C_NOTICE)build_targets     ..out/ =$(C_INFO) $(call strip_prefix,$(OUT)/,$(build_targets))$(C_OFF)"
	@echo "$(C_NOTICE)processed_targets ..out/ =$(C_INFO) $(call strip_prefix,$(OUT)/,$(processed_targets))$(C_OFF)"
	@echo "$(C_NOTICE)install_targets          =$(C_INFO) $(install_targets)$(C_OFF)"
	@echo "$(C_NOTICE)OUT                      =$(C_INFO) $(OUT)$(C_OFF)"
	@echo "$(C_NOTICE)IN                       =$(C_INFO) $(IN)$(C_OFF)"
	@echo "$(C_NOTICE)KCONFIG_CONFIG           =$(C_INFO) $(KCONFIG_CONFIG)$(C_OFF)"
	@echo "========================="



##############################
##### Menuconfig targets #####
##############################

MENUCONFIG_STYLE ?= aquatic
ifeq ($(F_MULTI_UNIT_ENTRY_POINT),y)
  MENUCONFIG_STYLE := default
endif

$(KCONFIG_CONFIG): $(SRC)/installer/Kconfig* $(SRC)/installer/**/Kconfig* 
# If KCONFIG_CONFIG is outdated or doesn't exist run menuconfig first. If the user doesn't save the config,
# we will update it with olddefconfig. touch in case .config does not get updated by olddefconfig.py
# Only if install or menuconfig is not the target (else it will run twice)
ifeq ($(filter menuconfig uninstall,$(MAKECMDGOALS)),)
	$(Q)$(MAKE) MAKEFLAGS= menuconfig
	$(Q)$(PY) -m olddefconfig $(SRC)/installer/Kconfig >/dev/null # Always update the .config file in case the user doesn't save it
	$(Q)touch $(KCONFIG_CONFIG)
endif

menuconfig: $(SRC)/installer/Kconfig
	$(Q)MENUCONFIG_STYLE="$(MENUCONFIG_STYLE)" $(PY) -m menuconfig Kconfig

