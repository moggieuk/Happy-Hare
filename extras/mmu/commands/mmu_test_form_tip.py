# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_FORM_TIP command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuTestFormTipCommand(BaseCommand):
    """
    Convenience macro for calling the standalone tip forming functionality (or cutter logic).
    """

    CMD = "MMU_TEST_FORM_TIP"

    HELP_BRIEF = "Convenience macro for calling the standalone tip forming functionality (or cutter logic)"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "RESET          = [0|1]\n"
        + "SHOW           = [0|1]\n"
        + "RUN            = [0|1]\n"
        + "FORCE_IN_PRINT = [0|1]\n"
        + "(also accepts macro variable overrides; can use 'variable_' prefix or omit it)\n"
    )
    HELP_SUPPLEMENT = (
        ""  # add examples here if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING
        )

    def _run(self, gcmd):
        # BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if mmu.check_if_disabled(): return

        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        show = bool(gcmd.get_int('SHOW', 0, minval=0, maxval=1))
        run = bool(gcmd.get_int('RUN', 1, minval=0, maxval=1))
        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print syncing and current

        gcode_macro = mmu.printer.lookup_object("gcode_macro %s" % mmu.p.form_tip_macro, None)
        if gcode_macro is None:
            raise gcmd.error("Filament tip forming macro '%s' not found" % mmu.p.form_tip_macro)
        gcode_vars = mmu.printer.lookup_object("gcode_macro %s_VARS" % mmu.p.form_tip_macro, gcode_macro)

        if reset:
            if mmu.form_tip_vars is not None:
                gcode_vars.variables = dict(mmu.form_tip_vars)
                mmu.form_tip_vars = None
                mmu.log_always("Reset '%s' macro variables to defaults" % mmu.p.form_tip_macro)
            show = True

        if show:
            msg = "Variable settings for macro '%s':" % mmu.p.form_tip_macro
            for k, v in gcode_vars.variables.items():
                msg += "\nvariable_%s: %s" % (k, v)
            mmu.log_always(msg)
            return

        # Save restore point on first call
        if mmu.form_tip_vars is None:
            mmu.form_tip_vars = dict(gcode_vars.variables)

        for param in gcmd.get_command_parameters():
            value = gcmd.get(param)
            param = param.lower()
            if param.startswith("variable_"):
                mmu.log_always("Removing 'variable_' prefix from '%s' - not necessary" % param)
                param = param[9:]
            if param in gcode_vars.variables:
                gcode_vars.variables[param] = mmu._fix_type(value)
            elif param not in ["reset", "show", "run", "force_in_print"]:
                mmu.log_error("Variable '%s' is not defined for '%s' macro" % (param, mmu.p.form_tip_macro))

        # Run the macro in test mode (final_eject is set)
        msg = "Running macro '%s' with the following variable settings:" % mmu.p.form_tip_macro
        for k, v in gcode_vars.variables.items():
            msg += "\nvariable_%s: %s" % (k, v)
        mmu.log_always(msg)

        try:
            with mmu.wrap_sync_gear_to_extruder():
                if run:
                    mmu._ensure_safe_extruder_temperature(wait=True)

                    # Ensure sync state and mimick in print if requested
                    mmu.reset_sync_gear_to_extruder(mmu.mmu_unit().p.sync_form_tip, force_in_print=force_in_print)

                    _, _, _ = mmu._do_form_tip(test=not mmu.is_in_print(force_in_print))
                    mmu._set_filament_pos_state(FILAMENT_POS_UNLOADED)

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
