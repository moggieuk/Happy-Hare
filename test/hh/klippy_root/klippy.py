# Fake Klipper `klippy/klippy.py` (the Printer object) for the Happy Hare harness.
#
# load_object is a SEMANTIC COPY of Klipper's, deliberately including the
# `section.split()` and the exists-on-disk check. Both matter:
#
#   extras/mmu/unit/nfc/mmu_nfc_reader.py:132 calls
#       printer.load_object('mmu_nfc_reader', None)
#   but the signature is load_object(config, section, default=sentinel). So the
#   arguments are shifted: config='mmu_nfc_reader', section=None, no default. Real
#   Klipper then does None.split() -> AttributeError, crashing config load for
#   anyone with an [mmu_nfc_reader NAME] section. A permissive fake would paper over
#   a genuine crash-on-load bug, so we keep the exact behaviour.
#
#   Keeping the on-disk check is the second half: even with the arguments fixed the
#   call returns None, because HH's reader lives at extras/mmu/unit/nfc/ and there is
#   no klippy/extras/mmu_nfc_reader.py to import. So the documented bare
#   [mmu_nfc_reader] defaults inheritance is inert regardless.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os, logging, importlib

import configfile
import reactor as reactor_mod


class error(Exception):
    pass


class command_error(Exception):
    pass


class Printer:
    config_error = configfile.error
    command_error = command_error

    def __init__(self, start_args=None, reactor=None):
        self.start_args = dict(start_args or {})
        self.reactor = reactor if reactor is not None else reactor_mod.VirtualReactor()
        self.objects = {}
        self.event_handlers = {}
        self.state_message = 'startup'
        self.in_shutdown_state = False
        self.run_result = None
        # test assertion surfaces
        self.events_fired = []
        self.rollover_info = {}
        self._extras_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       'extras')

    # -- objects -----------------------------------------------------------
    def get_reactor(self):
        return self.reactor

    def get_start_args(self):
        return self.start_args

    def get_state_message(self):
        return self.state_message, 'ready'

    def is_shutdown(self):
        return self.in_shutdown_state

    def add_object(self, name, obj):
        if name in self.objects:
            raise self.config_error(
                "Printer object '%s' already created" % (name,))
        self.objects[name] = obj

    def lookup_object(self, name, default=configfile.sentinel):
        if name in self.objects:
            return self.objects[name]
        if default is configfile.sentinel:
            raise self.config_error("Unknown config object '%s'" % (name,))
        return default

    def lookup_objects(self, module=None):
        if module is None:
            return list(self.objects.items())
        prefix = module + ' '
        objs = [(n, self.objects[n])
                for n in self.objects if n.startswith(prefix)]
        if module in self.objects:
            return [(module, self.objects[module])] + objs
        return objs

    def load_object(self, config, section, default=configfile.sentinel):
        # NOTE: byte-for-byte semantics with Klipper - see module docstring.
        if section in self.objects:
            return self.objects[section]
        module_parts = section.split()
        module_name = module_parts[0]
        py_name = os.path.join(self._extras_dir, module_name + '.py')
        py_dirname = os.path.join(self._extras_dir, module_name, '__init__.py')
        if not os.path.exists(py_name) and not os.path.exists(py_dirname):
            if default is not configfile.sentinel:
                return default
            raise self.config_error("Unable to load module '%s'" % (section,))
        mod = importlib.import_module('extras.' + module_name)
        init_func = 'load_config'
        if len(module_parts) > 1:
            init_func = 'load_config_prefix'
        init_func = getattr(mod, init_func, None)
        if init_func is None:
            if default is not configfile.sentinel:
                return default
            raise self.config_error("Unable to load module '%s'" % (section,))
        self.objects[section] = init_func(config.getsection(section))
        return self.objects[section]

    # -- events ------------------------------------------------------------
    def register_event_handler(self, event, callback):
        self.event_handlers.setdefault(event, []).append(callback)

    def send_event(self, event, *params):
        self.events_fired.append(event)
        return [cb(*params) for cb in self.event_handlers.get(event, [])]

    def fired(self, event):
        """Test-facing: has this event been sent?"""
        return event in self.events_fired

    def set_rollover_info(self, name, info, log=True):
        self.rollover_info[name] = info
        if log and info:
            logging.info(info)

    def invoke_shutdown(self, msg):
        self.in_shutdown_state = True
        self.state_message = msg
        self.send_event('klippy:shutdown')

    def invoke_async_shutdown(self, msg):
        self.invoke_shutdown(msg)

    def request_exit(self, result):
        self.run_result = result
