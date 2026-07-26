# Fake Klipper `klippy/extras/save_variables.py` for the Happy Hare test harness.
#
# HH touches exactly one attribute - `allVariables` (extras/mmu/mmu_utils.py:72-117)
# - but it also needs a real SAVE_VARIABLE command, because
# SaveVariableManager.write() issues run_script_from_command("SAVE_VARIABLE ...")
# (extras/mmu/mmu_utils.py:137).
#
# The file must contain `mmu__revision`: SaveVariableManager treats its absence as
# "mmu_vars.cfg not found" and raises a config error (extras/mmu/mmu_utils.py:71-84).
# The harness points `filename` at a per-session temp copy of config/mmu_vars.cfg.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os, ast, configparser


class SaveVariables:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.filename = os.path.expanduser(config.get('filename'))
        self.allVariables = {}
        self.writes = []            # test assertion surface
        try:
            self.loadVariables()
        except Exception:
            raise config.error("Unable to load variables from %s" % (self.filename,))
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('SAVE_VARIABLE', self.cmd_SAVE_VARIABLE,
                               desc='Save arbitrary variable to disk')

    def loadVariables(self):
        allvars = {}
        varfile = configparser.ConfigParser()
        if not os.path.exists(self.filename):
            # A missing file is legitimate on a fresh install; HH seeds mmu__revision
            self.allVariables = {}
            return
        varfile.read(self.filename)
        if varfile.has_section('Variables'):
            for name, val in varfile.items('Variables'):
                try:
                    allvars[name] = ast.literal_eval(val)
                except (SyntaxError, ValueError):
                    allvars[name] = val
        self.allVariables = allvars

    def cmd_SAVE_VARIABLE(self, gcmd):
        varname = gcmd.get('VARIABLE')
        value = gcmd.get('VALUE')
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            pass
        self.allVariables[varname] = value
        self.writes.append((varname, value))
        self._write_file()

    def _write_file(self):
        varfile = configparser.ConfigParser()
        varfile.add_section('Variables')
        for name, val in sorted(self.allVariables.items()):
            varfile.set('Variables', name, repr(val))
        try:
            with open(self.filename, 'w') as f:
                varfile.write(f)
        except OSError:
            pass    # a read-only fixture path is not a test failure

    def get_status(self, eventtime=None):
        return {'variables': self.allVariables}


def load_config(config):
    return SaveVariables(config)
