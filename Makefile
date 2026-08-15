SHELL := /usr/bin/env sh
PY    := python

# Keep these comments on their own line: an inline one pads the value with the spaces
# leading up to the '#', which silently breaks anything matching on the value

# For quiet make builds, override with make Q= for verbose output
Q  ?= @

# For verbose output (mostly from python builder), set to -v to enable
V  ?=

# For unittests, e.g. make UT=test_build.py test
# A UT pattern skips the interactive picker, as do ALL=1 (run everything) and LAST=1
# (re-run the last selection). See test/README.md section 1
UT ?= *

# Extra flags for the pip that installs installer/requirements.txt. Exported rather than
# passed on the command line when it has to survive install.sh, which forwards no make
# vars: export PIP_ARGS='--user --break-system-packages' on a PEP 668 python with no venv
PIP_ARGS ?=

# Parallel build
MAKEFLAGS += --jobs 16

# By default KCONFIG_CONFIG is '.mmu_config', but it can be overridden by the user
export KCONFIG_CONFIG ?= .mmu_config

# Enable output-sync if menuconfig will not trigger. menuconfig.py will crash if output-sync is enabled on certain systems
ifeq ($(CHECK_OUTPUT_SYNC),)
  # Never probe for menuconfig or uninstall and only if KCONFIG exists
  # 'console' and 'test' must stay in this list: --output-sync buffers a recipe's output
  # until it finishes, which for an interactive prompt means no prompt at all. 'test' opens
  # the file picker in test/select.py
  ifeq ($(strip $(filter menuconfig uninstall variables gen_kconfig fix_links console test shots,$(MAKECMDGOALS))),)
    ifneq ($(wildcard $(KCONFIG_CONFIG)),)
      # Check whether $KCONFIG_CONFIG is outdated. if so menuconfig will be triggered and output-sync should stay disabled
      ifeq ($(shell $(MAKE) CHECK_OUTPUT_SYNC=y -q $(KCONFIG_CONFIG) >/dev/null 2>&1 && echo y),y)
        MAKEFLAGS += --output-sync=line
      endif
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

# Virtualenv, created on demand by any goal that needs an interpreter the machine cannot
# otherwise provide: `make test` for the test deps, and anything running the builder where
# $(PY) resolved to it below. See test/README.md
VENV       ?= $(SRC)/venv
VENV_PY    := $(VENV)/bin/python
VENV_STAMP := $(VENV)/.hh-test-requirements
# Separate stamp: test/requirements.txt and installer/requirements.txt are installed
# independently into the same venv, and neither should trigger the other
INSTALLER_STAMP := $(VENV)/.hh-installer-requirements

# Interpreter used to create the venv, falling back where plain `python` isn't a name
BOOTSTRAP_PY := $(if $(shell command -v $(PY) 2>/dev/null),$(PY),python3)

# Klipper's own virtualenv carries greenlet and Jinja2 - klippy-requirements.txt needs both -
# which is the whole of test/requirements.txt. So on a printer there is nothing to build:
# use it and skip the venv
KLIPPY_ENV ?= $(HOME)/klippy-env
ifneq ($(filter test console,$(MAKECMDGOALS)),)
  klippy_env_py := $(wildcard $(KLIPPY_ENV)/bin/python)
  ifneq ($(klippy_env_py),)
    klippy_env_py := $(shell $(klippy_env_py) -c 'import greenlet, jinja2' 2>/dev/null && echo $(klippy_env_py))
  endif
endif

# The builder needs jinja2, which a PEP 668 python outside a venv will never accept, so keep
# the first interpreter that already has it and fall back to the venv. Skipped when someone
# has said which to feed (PY=, PIP_ARGS, a live venv). Must stay below BOOTSTRAP_PY
ifeq ($(VIRTUAL_ENV)$(PIP_ARGS),)
  ifneq ($(origin PY),command line)
    PY := $(shell for p in $(PY) $(KLIPPY_ENV)/bin/python $(VENV_PY); do \
              command -v "$$p" >/dev/null 2>&1 && \
              "$$p" -c 'import jinja2' >/dev/null 2>&1 && { echo "$$p"; exit 0; }; \
            done; \
            $(PY) -c 'import os, sys, sysconfig; \
                sys.exit(0 if os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 1)' \
              >/dev/null 2>&1 && echo $(VENV_PY) || echo $(PY))
  endif
endif

# If $(PY) landed on the venv it has to exist before any recipe runs, so python_deps - which
# everything running the builder waits on - builds it. Empty otherwise, so a python whose pip
# works still just installs the deps rather than getting a venv it never asked for
builder_prereq := $(if $(filter $(VENV_PY),$(PY)),$(INSTALLER_STAMP))

# NO_VENV=1 or an explicit PY= runs the tests against the system interpreter instead.
# test_prereqs keys off TEST_PY so opting out never builds a venv it then ignores
ifdef NO_VENV
  TEST_PY ?= $(BOOTSTRAP_PY)
endif
TEST_PY      ?= $(if $(findstring command line,$(origin PY)),$(PY),$(if $(klippy_env_py),$(klippy_env_py),$(VENV_PY)))
test_prereqs := $(if $(filter $(VENV_PY),$(TEST_PY)),$(VENV_STAMP))

# Which python ran the tests should never be a mystery, and this is the surprising one.
# No commas in the message: $(if) splits its arguments on them
using_klippy_env = $(if $(filter $(klippy_env_py),$(TEST_PY)), \
	echo "$(C_INFO)Using klipper's virtualenv '$(KLIPPY_ENV)' - it already has greenlet and jinja2$(C_OFF)",:)

ifneq ($(TESTDIR),)
  OUTDIR := $(TESTDIR)
else
  OUTDIR := $(CURDIR)
endif

export OUT ?= $(OUTDIR)/out
export IN  := $(OUT)/in

# Default unit and mcu naming
export UNIT_NAME ?= unit0
export MCU_NAME ?= unit0

# Helper functions/constants
comma := ,
empty :=
space := $(empty) $(empty)

# Replace ~ with $(HOME) and remove quotes
unwrap                   = $(subst ~,$(HOME),$(patsubst "%",%,$(1)))

define convert_list
$(strip $(foreach t,$(subst $(comma), ,$(1)),$(subst $(space),_,$(strip $(t)))))
endef

# We strip these from surrounding quotes, and replace ~ with $(HOME)
KLIPPER_HOME            := $(call unwrap,$(CONFIG_KLIPPER_HOME))
KLIPPER_CONFIG_HOME     := $(call unwrap,$(CONFIG_KLIPPER_CONFIG_HOME))
MOONRAKER_HOME          := $(call unwrap,$(CONFIG_MOONRAKER_HOME))
PRINTER_CONFIG_FILE     := $(call unwrap,$(CONFIG_PRINTER_CONFIG_FILE))
MOONRAKER_CONFIG_FILE   := $(call unwrap,$(CONFIG_MOONRAKER_CONFIG_FILE))

# unit_names: from CONFIG_MMU_UNITS in multi-unit, else default to unit0
#unit_names := $(if $(filter y,$(CONFIG_MULTI_UNIT)),$(strip $(subst ",,$(call convert_list,$(call strip_ws_around_commas,$(CONFIG_MMU_UNITS))))),unit0)
unit_names := $(if $(filter y,$(CONFIG_MULTI_UNIT)),$(call convert_list,$(subst ",,$(CONFIG_MMU_UNITS))),unit0)

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
.PRECIOUS: $(KCONFIG_CONFIG) $(KCONFIG_CONFIG)_%
.PHONY: menuconfig install uninstall check_version diff test console shots venv installer_venv clean_venv build clean variables python_deps fix_links gen_kconfig kconfig_needs_update olddefconfig verify_pickle
.SECONDARY: \
	$(call backup_name,$(KLIPPER_CONFIG_HOME)/mmu) \
	$(call backup_name,$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE)) \
	$(call backup_name,$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE))



#####################
##### File sets #####
#####################

hh_klipper_extras_files := $(wildcard extras/*.py extras/mmu/*.py extras/mmu/unit/*.py extras/mmu/unit/nfc/*.py extras/mmu/unit/selectors/*.py extras/mmu/commands/*.py)
hh_old_klipper_modules  := mmu_toolhead.py mmu_encoder.py mmu_espooler.py mmu_leds.py mmu_sensors.py mmu/* # These will get removed upon install/uninstall
hh_moonraker_components := $(wildcard components/*.py)

# All repo configs files less mmu_vars.cfg. Zero-length files (e.g. config/base/*.cfg
# upgrade-path placeholders, see config/base/README.md) are deliberately excluded here:
# $(wildcard) can't test size, so use find -size +0c instead, or every fresh install
# would build and install those empty stubs as real (if empty) config files.
repo_cfgs := \
	$(patsubst config/%,%, $(shell find config -mindepth 1 -maxdepth 2 -name '*.cfg' -size +0c))

# Per-unit files: <unit>_{hardware,parameters}.cfg
hh_unit_config_files := \
	$(addprefix base/mmu_hardware_,$(addsuffix .cfg,$(unit_names))) \
	$(addprefix base/mmu_parameters_,$(addsuffix .cfg,$(unit_names)))

# Final config set: all repo cfgs (minus the single-unit defaults) + per-unit files
hh_config_files := \
	$(filter-out base/mmu_hardware.cfg base/mmu_parameters.cfg,$(repo_cfgs)) \
	$(hh_unit_config_files)

# Look for installed configs that would need be parsed by the build script
# This allows for easy upgrades and option movement across files
hh_configs_to_parse := \
	$(subst $(KLIPPER_CONFIG_HOME),$(IN),$(wildcard $(KLIPPER_CONFIG_HOME)/mmu/base/*.cfg))

# Set of config files (one if single unit, else n + 1)
kconfig_files := $(KCONFIG_CONFIG) \
	$(if $(filter y,$(CONFIG_MULTI_UNIT)), \
		$(addprefix $(KCONFIG_CONFIG)_,$(unit_names)))

# Files/targets that need to be built
build_targets := \
	$(addprefix $(OUT)/mmu/, $(hh_config_files))

# Subset of files/targets which require token processing and/or are user editable
processed_targets := \
	$(filter-out $(OUT)/mmu/optional/%.cfg $(OUT)/mmu/macros/%.cfg $(OUT)/mmu/mmu_vars.cfg, \
		$(filter $(OUT)/mmu/%.cfg, $(addprefix $(OUT)/mmu/,$(hh_config_files))))

# Files/targets that need to be installed
install_targets := \
	$(addprefix $(KLIPPER_CONFIG_HOME)/mmu/, $(hh_config_files)) \
	$(addprefix $(KLIPPER_HOME)/klippy/, $(hh_klipper_extras_files)) \
	$(addprefix $(MOONRAKER_HOME)/moonraker/, $(hh_moonraker_components)) \
	$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE) \
	$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE)

kconfig_sources := \
	$(wildcard $(SRC)/installer/Kconfig* $(SRC)/installer/**/Kconfig*) \
	$(SRC)/installer/lib/kconfiglib/kconfigfunctions.py


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
        elif [ -z "$(KCONFIG_CONFIG)" -o -z "$(2)" -o -z "$(3)" ]; then \
          echo "$(C_WARNING)Skipping restart of $(2): KCONFIG_CONFIG not set$(C_OFF)"; \
        else \
          [ "$(1)" -eq 0 ] || $(PY) -m installer.build $(V) --restart-service "$(2)" $(3) "$(KCONFIG_CONFIG)"; \
        fi



#########################
##### Build targets #####
#########################

KCONFIG_PREREQS := \
	$(KCONFIG_CONFIG) \
	$(OUT)/$(notdir $(KCONFIG_CONFIG)).pickle

## Conditional per-unit prerequisite ".mmu_config_<unit>" when multi-unit, else to ".mmu_config" (and pickles)
KCONF_REQS = $(if $(filter y,$(CONFIG_MULTI_UNIT)), \
	$(KCONFIG_CONFIG)_% $(OUT)/$(notdir $(KCONFIG_CONFIG))_%.pickle, \
	$(KCONFIG_PREREQS))

# Link existing config files to the out/in directory to break circular dependency
$(IN)/%:
	$(Q)[ -f "$(KLIPPER_CONFIG_HOME)/$*" ] || { echo "$(C_ERROR)The file '$(KLIPPER_CONFIG_HOME)/$*' does not exist. Please check your config for the correct paths$(C_OFF)"; exit 1; }
	$(call debug,$(C_DEBUG)Linking $(KLIPPER_CONFIG_HOME)/$* to '$(notdir $(IN))' directory$(C_OFF))
	$(Q)$(call link,$(KLIPPER_CONFIG_HOME)/$*,$@)

ifneq ($(strip $(MOONRAKER_CONFIG_FILE)),)
# Copy existing moonraker.conf to the out directory and update with moonraker_update.txt
$(OUT)/$(MOONRAKER_CONFIG_FILE): $(IN)/$$(@F) $(KCONFIG_PREREQS)
	$(call debug,$(C_DEBUG)Copying $< to '$(notdir $(OUT))' directory$(C_OFF))
	$(Q)$(call copy,$<,$@)
	$(Q)$(PY) -m installer.build $(V) --install-moonraker "$(SRC)/installer/moonraker_update.txt" "$@" "$(KCONFIG_CONFIG)"
endif

ifneq ($(strip $(PRINTER_CONFIG_FILE)),)
# Copy existing printer.cfg to the out directory and update with includes
$(OUT)/$(PRINTER_CONFIG_FILE): $(IN)/$$(@F) $(KCONFIG_PREREQS)
	$(call debug,$(C_DEBUG)Copying $< to '$(notdir $(OUT))' directory$(C_OFF))
	$(Q)$(call copy,$<,$@)
	$(Q)$(PY) -m installer.build $(V) --install-includes "$@" "$(KCONFIG_CONFIG)"
endif

# We link all config files, those that need to be updated will be written over in the install script,
# in case of a multi unit setup, the per-unit config targets are overridden below
$(OUT)/mmu/%.cfg: $(SRC)/config/%.cfg $(hh_configs_to_parse) $(KCONFIG_PREREQS)
	$(Q)$(call link,$<,$@)
	$(Q)$(if $(filter $@,$(processed_targets)), \
		$(PY) -m installer.build $(V) --build "$<" "$@" "$(KCONFIG_CONFIG)" $(hh_configs_to_parse), \
	        $(info $(C_INFO)Skipping build of mmu/$*$(C_OFF)))

# Shared target rules don't work on old make so separate for portability
$(OUT)/mmu/base/mmu_hardware_%.cfg: \
  $(SRC)/config/base/mmu_hardware.cfg $(hh_configs_to_parse) $(KCONF_REQS)
	$(Q)$(call link,$<,$@)
	$(Q)$(PY) -m installer.build $(V) --build "$<" "$@" \
		"$(if $(filter y,$(CONFIG_MULTI_UNIT)),$(KCONFIG_CONFIG)_$*,$(KCONFIG_CONFIG))" $(hh_configs_to_parse)

$(OUT)/mmu/base/mmu_parameters_%.cfg: \
  $(SRC)/config/base/mmu_parameters.cfg $(hh_configs_to_parse) $(KCONF_REQS)
	$(Q)$(call link,$<,$@)
	$(Q)$(PY) -m installer.build $(V) --build "$<" "$@" \
		"$(if $(filter y,$(CONFIG_MULTI_UNIT)),$(KCONFIG_CONFIG)_$*,$(KCONFIG_CONFIG))" $(hh_configs_to_parse)

# Python files are linked to the out directory
$(OUT)/klippy/extras/%.py: $(SRC)/extras/%.py
	$(Q)$(call link,$<,$@)

$(OUT)/moonraker/components/%.py: $(SRC)/components/%.py
	$(Q)$(call link,$<,$@)

$(OUT)/%: %
	$(Q)cp -p "$<" "$@"

$(OUT):
	$(Q)mkdir -p "$@"

$(build_targets): $(KCONFIG_PREREQS) | $(OUT) check_version python_deps

build: $(build_targets)



###########################
##### Install targets #####
###########################

# Check whether the required paths exist
$(KLIPPER_HOME)/klippy/extras $(MOONRAKER_HOME)/moonraker/components:
	$(error The directory '$@' does not exist. Please check your config for the correct paths)

# Install python files for klipper
$(KLIPPER_HOME)/%: $(OUT)/% | $(KLIPPER_HOME)/klippy/extras
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

$(install_targets): build | python_deps

install: $(install_targets)
	@# Backup current kconfig files in the klipper config directory
	@echo "$(C_INFO)Copying kconfig files '$(notdir $(kconfig_files))' to $(KLIPPER_CONFIG_HOME)/mmu$(C_OFF)"
	$(Q)for f in $(kconfig_files); do \
		[ -f "$$f" ] && $(SUDO)cp -p "$$f" "$(KLIPPER_CONFIG_HOME)/mmu/$$(basename "$$f")"; \
	done
	@# We are done. Restart everything
	$(Q)$(call restart_service,$(restart_moonraker),Moonraker,$(CONFIG_SERVICE_MOONRAKER))
	$(Q)$(call restart_service,$(restart_klipper),Klipper,$(CONFIG_SERVICE_KLIPPER))
	$(Q)$(PY) -m installer.build $(V) --print-happy-hare "Done! Happy Hare $(CONFIG_F_VERSION)is ready!"

uninstall: clean | python_deps
	$(Q)$(if $(MOONRAKER_CONFIG_FILE), \
		$(call backup,$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE)))
	$(Q)$(if $(PRINTER_CONFIG_FILE), \
		$(call backup,$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE)))
	$(Q)$(call backup,$(KLIPPER_CONFIG_HOME)/mmu)
	@# Be sure older v3 files are also removed
	$(Q)rm -rf $(addprefix $(KLIPPER_HOME)/klippy/extras/,$(hh_old_klipper_modules))
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
	$(Q)rm -f $(KCONFIG_CONFIG) $(KCONFIG_CONFIG).old $(KCONFIG_CONFIG)_*
	$(Q)$(PY) -m installer.build $(V) --print-unhappy-hare "Done. Very unHappy Hare."

fix_links:
	$(Q)rm -rf $(addprefix $(KLIPPER_HOME)/klippy/extras/,$(hh_old_klipper_modules))
	$(Q)$(foreach f,$(hh_klipper_extras_files),$(call link,$(SRC)/$(f),$(KLIPPER_HOME)/klippy/$(f)))
	$(Q)$(foreach f,$(hh_moonraker_components),$(call link,$(SRC)/$(f),$(MOONRAKER_HOME)/moonraker/$(f)))
	$(Q)$(call restart_service,1,Moonraker,$(CONFIG_SERVICE_MOONRAKER))
	$(Q)$(call restart_service,1,Klipper,$(CONFIG_SERVICE_KLIPPER))



########################
##### Misc targets #####
########################

# Look for version number in current config files and report
check_version: $(hh_configs_to_parse) $(KCONFIG_PREREQS) | python_deps
	$(Q)$(PY) -m installer.build $(V) --check-version "$(KCONFIG_CONFIG)" $(hh_configs_to_parse)

gen_kconfig: | python_deps
	@echo "$(C_NOTICE)kconfig=$(KCONFIG_CONFIG)$(C_OFF)"
	$(Q)$(PY) -m installer.build $(V) --gen-kconfig-options "$(KCONFIG_CONFIG)"

clean:
	$(Q)rm -rf $(OUT)

# Deliberately not part of `clean`, which runs far too often to pay for a venv rebuild.
# STAMP_DIR is cleared unconditionally, even with no venv - it can hold a stamp for a
# klippy-env or system PY too, and either way it must stop claiming deps are installed
clean_venv:
	$(Q)rm -rf "$(STAMP_DIR)"
	$(Q)[ -f "$(VENV_PY)" ] || { echo "$(C_WARNING)No virtualenv at '$(VENV)'$(C_OFF)"; exit 0; }; \
		echo "$(C_INFO)Removing virtualenv '$(patsubst $(SRC)/%,%,$(VENV))'$(C_OFF)"; \
		rm -rf "$(VENV)"

# Shared tail for every pip failure below, whichever of the three it is: the ways out are
# the same, and the install path and the test path can each reach any of them
no_venv_hint = \
	echo "$(C_ERROR)Or install into your system python anyway (PEP 668 override):$(C_OFF)"; \
	echo "$(C_ERROR)  export PIP_ARGS='--user --break-system-packages'$(C_OFF)"; \
	echo "$(C_ERROR)For tests only, the system interpreter also works: make NO_VENV=1 test$(C_OFF)";

# Stamp so a `make` invocation with nothing changed just stats it, instead of every one of
# install.sh's several `make` calls per run re-installing from scratch. Reuses $(INSTALLER_STAMP)
# when $(PY) is $(VENV_PY) - installer_venv already keeps that fresh - otherwise keys off
# $(PY)'s resolved path, so switching interpreters without a `make clean` still reinstalls.
# Not under $(OUT): `uninstall: clean | python_deps` races `rm -rf $(OUT)` against this touch under -j
STAMP_DIR := $(SRC)/.hh-stamps
python_deps_pyid    := $(subst /,_,$(shell command -v $(PY) 2>/dev/null))
python_deps_stamp   := $(if $(builder_prereq),$(builder_prereq),$(STAMP_DIR)/.python-deps-installed-$(python_deps_pyid))

# Always '$(PY) -m pip', never a bare 'pip': the first pip on PATH need not belong to $(PY),
# and on macOS is often a leftover with a dead shebang. Only a PEP 668 python with no venv to
# fall back on reaches the hints
python_deps: $(python_deps_stamp)

$(STAMP_DIR):
	$(Q)mkdir -p "$@"

$(STAMP_DIR)/.python-deps-installed-$(python_deps_pyid): $(SRC)/installer/requirements.txt | $(STAMP_DIR)
	$(Q)echo "$(C_INFO)Checking and resolving python dependencies$(C_OFF)"
	$(Q)$(PY) -m pip install --quiet --disable-pip-version-check $(PIP_ARGS) \
		-r $(SRC)/installer/requirements.txt || { \
		echo "$(C_ERROR)'$(PY) -m pip' could not install installer/requirements.txt$(C_OFF)"; \
		echo "$(C_ERROR)If pip refused with 'externally-managed-environment' (PEP 668), work in$(C_OFF)"; \
		echo "$(C_ERROR)a virtualenv as install.sh does, then re-run:$(C_OFF)"; \
		echo "$(C_ERROR)  make installer_venv && . $(patsubst $(SRC)/%,%,$(VENV))/bin/activate$(C_OFF)"; \
		$(no_venv_hint) \
		exit 1; \
	}
	$(Q)touch $@

$(OUT)/$(notdir $(KCONFIG_CONFIG)).pickle: $(KCONFIG_CONFIG) | python_deps $(OUT)
	$(Q)echo "$(C_INFO)Pre-parsing Kconfig $(notdir $(KCONFIG_CONFIG))$(C_OFF)"
	$(Q)$(PY) -m installer.build $(V) --pre-parse-kconfig "$(KCONFIG_CONFIG)"

$(OUT)/$(notdir $(KCONFIG_CONFIG))_%.pickle: $(KCONFIG_CONFIG)_% | python_deps $(OUT)
	$(Q)echo "$(C_INFO)Pre-parsing Kconfig $(notdir $(KCONFIG_CONFIG)_$*)$(C_OFF)"
	$(Q)$(PY) -m installer.build $(V) --pre-parse-kconfig "$(KCONFIG_CONFIG)_$*"


diff= \
	git diff -U2 --color --src-prefix="current: " --dst-prefix="built: " --minimal --word-diff=color --stat --no-index -- "$(1)" "$(2)" | \
	grep -v "diff --git " | \
	grep -Ev "index [[:xdigit:]]+\.\.[[:xdigit:]]+" || true;

diff: | build
	$(Q)$(call diff,$(KLIPPER_CONFIG_HOME)/mmu,$(patsubst $(SRC)/%,%,$(OUT)/mmu))
	$(Q)$(call diff,$(KLIPPER_CONFIG_HOME)/$(PRINTER_CONFIG_FILE),$(patsubst $(SRC)/%,%,$(OUT)/$(PRINTER_CONFIG_FILE)))
	$(Q)$(call diff,$(KLIPPER_CONFIG_HOME)/$(MOONRAKER_CONFIG_FILE),$(patsubst $(SRC)/%,%,$(OUT)/$(MOONRAKER_CONFIG_FILE)))

# Debian/RPi OS split ensurepip into python3-venv, so `python3 -m venv` there leaves a
# bin/python with no pip - and since that satisfies $(VENV_PY), creation never re-runs. Hence
# the check sits with the rule that uses pip, and ensurepip is the repair, not just a probe
venv_pip_check = \
	$(VENV_PY) -m pip --version >/dev/null 2>&1 || \
	$(VENV_PY) -m ensurepip --default-pip >/dev/null 2>&1 || { \
		echo "$(C_ERROR)The virtualenv at '$(patsubst $(SRC)/%,%,$(VENV))' has no pip$(C_OFF)"; \
		echo "$(C_ERROR)On Debian/Ubuntu/Raspberry Pi OS: sudo apt install python3-venv$(C_OFF)"; \
		echo "$(C_ERROR)and just run this again$(C_OFF)"; \
		[ -x "$(KLIPPY_ENV)/bin/python" ] && { \
			echo "$(C_ERROR)Klipper's own virtualenv already has what the tests need$(C_OFF)"; \
			echo "$(C_ERROR)and installs nothing: make PY=$(KLIPPY_ENV)/bin/python test$(C_OFF)"; \
		}; \
		$(no_venv_hint) \
		exit 1; \
	}

# Guarded by the interpreter it produces, so this runs once
$(VENV_PY):
	$(Q)echo "$(C_INFO)Creating virtualenv in '$(patsubst $(SRC)/%,%,$(VENV))'$(C_OFF)"
	$(Q)$(BOOTSTRAP_PY) -m venv "$(VENV)" || { \
		echo "$(C_ERROR)Could not create a virtualenv with '$(BOOTSTRAP_PY) -m venv'$(C_OFF)"; \
		echo "$(C_ERROR)On Debian/Ubuntu install it with: sudo apt install python3-venv$(C_OFF)"; \
		$(no_venv_hint) \
		exit 1; \
	}

# One rule for both tenants: '.hh-<dir>-requirements' stamps <dir>/requirements.txt, so test/
# and installer/ deps install independently. Stamps live inside the venv, so a deleted venv
# or an edited requirements.txt reinstalls
$(VENV)/.hh-%-requirements: $(SRC)/%/requirements.txt | $(VENV_PY)
	$(Q)$(venv_pip_check)
	$(Q)echo "$(C_INFO)Installing $* dependencies from $(patsubst $(SRC)/%,%,$<)$(C_OFF)"
	$(Q)$(VENV_PY) -m pip install --quiet --disable-pip-version-check -r "$<"
	$(Q)touch "$@"

# Explicit target for anyone who wants the venv without running the tests
venv: $(VENV_STAMP)
	$(Q)echo "$(C_NOTICE)Test virtualenv ready: $(patsubst $(SRC)/%,%,$(VENV_PY))$(C_OFF)"

# Installer deps only - the tests' greenlet is a compile on a Pi and the installer never
# needs it. install.sh runs this then activates the venv on a PEP 668 python. The extras it
# installs still run under klipper's python. The no-op recipe silences 'Nothing to be done'
installer_venv: $(INSTALLER_STAMP)
	@:

# Opens the interactive file picker, everything pre-ticked, so Enter runs the whole suite as
# before. Skipped for UT/ALL/LAST or when stdin isn't a tty - test/select.py decides. Extra
# unittest flags go through ARGS, e.g. make test ARGS='-k homing'
test: $(test_prereqs)
	$(Q)$(using_klippy_env)
	$(Q)PYTHONPATH="$(SRC)/installer/lib/kconfiglib:$(PYTHONPATH)" \
		$(TEST_PY) -m test.select $(V) \
			$(if $(filter-out *,$(UT)),--pattern '$(strip $(UT))') \
			$(if $(ALL),--all) $(if $(LAST),--last) $(ARGS)

# Interactive MMU console on the test harness. Pass flags through ARGS, e.g.
#   make console ARGS='--profile /tmp/mmu_test/printer_data/config'
#   make console ARGS='--profile encoder --header machine,sensors,filament,leds'
console: $(test_prereqs)
	$(Q)$(using_klippy_env)
	$(Q)PYTHONPATH="$(SRC)/installer/lib/kconfiglib:$(PYTHONPATH)" \
		$(TEST_PY) -m test.console $(ARGS)

variables:
	@echo "========================="
	@echo "$(C_NOTICE)hh_klipper_extras_files        =$(C_INFO) $(hh_klipper_extras_files)$(C_OFF)"
	@echo "$(C_NOTICE)hh_old_klipper_modules         =$(C_INFO) $(hh_old_klipper_modules)$(C_OFF)"
	@echo "$(C_NOTICE)hh_moonraker_components        =$(C_INFO) $(hh_moonraker_components)$(C_OFF)"
	@echo "$(C_NOTICE)repo_cfgs                      =$(C_INFO) $(repo_cfgs)$(C_OFF)"
	@echo "$(C_NOTICE)unit_names                     =$(C_INFO) $(unit_names)$(C_OFF)"
	@echo "$(C_NOTICE)hh_unit_config_files           =$(C_INFO) $(hh_unit_config_files)$(C_OFF)"
	@echo "$(C_NOTICE)hh_config_files                =$(C_INFO) $(hh_config_files)$(C_OFF)"
	@echo "$(C_NOTICE)hh_configs_to_parse            =$(C_INFO) $(hh_configs_to_parse)$(C_OFF)"
	@echo "$(C_NOTICE)kconfig_files                  =$(C_INFO) $(kconfig_files)$(C_OFF)"
	@echo "$(C_NOTICE)build_targets     ..out/       =$(C_INFO) $(call strip_prefix,$(OUT)/,$(build_targets))$(C_OFF)"
	@echo "$(C_NOTICE)processed_targets ..out/       =$(C_INFO) $(call strip_prefix,$(OUT)/,$(processed_targets))$(C_OFF)"
	@echo "$(C_NOTICE)kconfig_sources   ..installer/ =$(C_INFO) $(call strip_prefix,$(SRC)/installer/,$(kconfig_sources))$(C_OFF)"
	@echo "$(C_NOTICE)install_targets                =$(C_INFO) $(install_targets)$(C_OFF)"
	@echo "$(C_NOTICE)OUT                            =$(C_INFO) $(OUT)$(C_OFF)"
	@echo "$(C_NOTICE)IN                             =$(C_INFO) $(IN)$(C_OFF)"
	@echo "$(C_NOTICE)KCONFIG_CONFIG                 =$(C_INFO) $(KCONFIG_CONFIG)$(C_OFF)"
	@echo "$(C_NOTICE)PY (builder)                   =$(C_INFO) $(PY)$(C_OFF)"
	@echo "$(C_NOTICE)TEST_PY (test/console)         =$(C_INFO) $(TEST_PY)$(C_OFF)"
	@echo "========================="


# Verify that every explicit CONFIG_* assignment in each raw Kconfig value file survived
# correctly into its pickle. Catches silent value-dropping or mis-typing bugs in KConfig.as_dict()
# (see installer/lib/kconfiglib/test_kconfig_pickle_consistency.py for why this check exists and how it works).
kconfig_pickles := $(addprefix $(OUT)/,$(addsuffix .pickle,$(notdir $(kconfig_files))))
verify_pickle: $(kconfig_pickles) | python_deps
	$(Q)status=0; \
	for f in $(kconfig_files); do \
		echo "$(C_INFO)Verifying pickle consistency for $$f$(C_OFF)"; \
		$(PY) "$(SRC)/installer/lib/kconfiglib/test_kconfig_pickle_consistency.py" \
			"$$f" "$(OUT)/$$(basename "$$f").pickle" || status=1; \
	done; \
	rm -rf $(OUT); \
	exit $$status



##############################
##### Menuconfig targets #####
##############################

MENUCONFIG_STYLE ?= default
ifeq ($(F_MULTI_UNIT_ENTRY_POINT),y)
  MENUCONFIG_STYLE := aquatic
endif

menuconfig: $(SRC)/installer/Kconfig | python_deps
	$(Q)MENUCONFIG_STYLE="$(MENUCONFIG_STYLE)" KLIPPER_HOME=$(KLIPPER_HOME) $(PY) -m menuconfig Kconfig

# Documentation screenshots: runs menuconfig headlessly and renders its screens to
# doc/images (and per-page image folders under doc/ - see doc_tools/README.md).
# The tool itself lives in doc_tools/; doc/ holds only what it generates. Pass flags
# through ARGS, e.g.
#   make shots ARGS='--list'
#   make shots ARGS='--only mmu-type -v'
# Or drive it by hand to find a screen worth capturing:
#   make shots CAPTURE=1 ARGS='--keys "select:Purging,enter" --dump'
#
# Always the venv python: doc_tools/requirements.txt (pyte, Pillow) is doc tooling
# that no other target needs, and the '.hh-<dir>-requirements' rule above installs it
# on demand. The tool sets up its own Kconfig environment - see doc_tools/capture.py -
# so unlike 'menuconfig' this target deliberately passes nothing from here.
shots: $(VENV)/.hh-doc_tools-requirements
	$(Q)$(VENV_PY) -m doc_tools.$(if $(CAPTURE),capture,shots) $(ARGS)



##################################
##### Upgrade helper targets #####
##################################

kconfig_needs_update:
	$(Q)if [ ! -f "$(KCONFIG_CONFIG)" ]; then \
		echo y; \
		exit 0; \
	fi; \
	for f in $(kconfig_sources); do \
		[ "$$f" -nt "$(KCONFIG_CONFIG)" ] && { echo y; exit 0; }; \
	done; \
	echo n

olddefconfig: | python_deps
	$(Q)$(PY) -m olddefconfig $(SRC)/installer/Kconfig >/dev/null
	$(Q)touch "$(KCONFIG_CONFIG)"
