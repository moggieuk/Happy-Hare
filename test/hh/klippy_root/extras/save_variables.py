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
# SAVE_VARIABLE IS NOT SYNCHRONOUS ANY MORE, and modelling that is the point of
# _submit(). Klipper commit 332fbf236 (2026-03-21) routes both the write and the
# reload through aio_executor, whose submit() blocks on completion.wait() ->
# reactor.pause(). So one SAVE_VARIABLE yields the calling greenlet TWICE. Combined
# with the assert_no_pause around the klippy:ready loop (klippy.py:161), that turns
# any ready-time SAVE_VARIABLE into ReactorError - the bug this stub has to be able
# to reproduce. printer.harness_klipper_aio picks the generation; False restores the
# old synchronous module so the same tests can assert the fix is a no-op there.
#
# THE PRE-PAUSE SNAPSHOT IS LOAD-BEARING. Klipper builds `newvars = dict(allVariables)`
# and renders every repr() BEFORE it submits (save_variables.py:48-54), then throws the
# live dict away and reloads from the file it wrote (:64). So a mutation landing during
# either pause is absent from the file AND erased from memory. Writing straight out of
# the live dict, as this stub used to, silently includes such a mutation and makes the
# race unreproducible. `on_pause` is the injection point tests use to create one.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os, ast, configparser


class SaveVariables:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.aio = getattr(self.printer, 'harness_klipper_aio', True)
        self.filename = os.path.expanduser(config.get('filename'))
        self.allVariables = {}
        self.writes = []            # test assertion surface
        # One-shot hook fired inside the pause window, so a test can mutate state at
        # exactly the moment real klipper is blocked on its worker thread.
        self.on_pause = None
        try:
            self.loadVariables()
        except Exception:
            raise config.error("Unable to load variables from %s" % (self.filename,))
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('SAVE_VARIABLE', self.cmd_SAVE_VARIABLE,
                               desc='Save arbitrary variable to disk')

    def _submit(self, fn, *args):
        """
        Stand-in for aio_executor.Executor.submit() (klipper aio_executor.py:50-58).

        The real one hands `fn` to a worker thread and waits on a completion, which
        pauses this greenlet. There are no threads here, so we take a zero-length
        pause instead - going through reactor.pause() rather than verify_can_pause()
        directly so the pause guard AND the greenlet switch both behave as they would
        in production.
        """
        if self.aio:
            if self.on_pause is not None:
                hook, self.on_pause = self.on_pause, None   # fire once
                hook()
            self.reactor.pause(self.reactor.monotonic())
        return fn(*args)

    def loadVariables(self):
        allvars = {}
        varfile = configparser.ConfigParser()
        if not os.path.exists(self.filename):
            # A missing file is legitimate on a fresh install; HH seeds mmu__revision
            self.allVariables = {}
            return
        self._submit(varfile.read, self.filename)   # klipper save_variables.py:29
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
        # Snapshot BEFORE any pause, exactly as klipper does (save_variables.py:48-54).
        # allVariables is deliberately NOT updated here - the reload below is what
        # republishes it, and that asymmetry is the whole bug being modelled.
        newvars = dict(self.allVariables)
        newvars[varname] = value
        self.writes.append((varname, value))
        if self._submit(self._write_file, newvars):     # klipper save_variables.py:59
            # Klipper re-reads the file it just wrote (save_variables.py:64), which
            # REPLACES allVariables wholesale after a second pause. Skipped when the
            # write failed, or the reload would discard the in-memory values.
            self.loadVariables()

    def _write_file(self, newvars):
        varfile = configparser.ConfigParser()
        varfile.add_section('Variables')
        for name, val in sorted(newvars.items()):
            varfile.set('Variables', name, repr(val))
        try:
            with open(self.filename, 'w') as f:
                varfile.write(f)
        except OSError:
            return False    # a read-only fixture path is not a test failure
        return True

    def get_status(self, eventtime=None):
        return {'variables': self.allVariables}


def load_config(config):
    return SaveVariables(config)
