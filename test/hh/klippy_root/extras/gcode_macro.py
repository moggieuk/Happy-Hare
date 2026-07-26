# Fake Klipper `klippy/extras/gcode_macro.py` for the Happy Hare test harness.
#
# Two distinct jobs:
#
# 1. load_template() must return a REAL jinja2-backed template. _ledEffect
#    requires it for its `layers` option (extras/mmu_led_effect.py:527), and the
#    rendered mmu.cfg ships 25 [mmu_led_effect] sections, so this is on the bootup
#    path for any profile with LEDs.
#
# 2. load_config_prefix() must give macros a real mutable `.variables` dict. HH reads
#    variables straight off them: _MMU_SEQUENCE_VARS['park_toolchange'] and
#    user_post_load_extension (extras/mmu/mmu_controller.py:205-207, 219-221) and
#    _BLOBIFIER_VARS (:461).
#
# Macro bodies are registered as recorded no-ops at this tier - running ~2000 lines
# of shipped Jinja macro is a later milestone, and HH reaches bootup without it.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import jinja2


class TemplateWrapper:
    def __init__(self, printer, env, name, script):
        self.printer = printer
        self.name = name
        self.script = script
        try:
            self.template = env.from_string(script)
        except Exception as e:
            raise printer.config_error("Error building template '%s': %s" % (name, e))

    def render(self, context=None):
        try:
            return str(self.template.render(context or {}))
        except Exception as e:
            raise self.printer.command_error(
                "Error evaluating template '%s': %s" % (self.name, e))

    def run_gcode_from_command(self, context=None):
        gcode = self.printer.lookup_object('gcode')
        gcode.run_script_from_command(self.render(context))


class PrinterGCodeMacro:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.env = jinja2.Environment('{%', '%}', '{', '}',
                                      extensions=['jinja2.ext.do'])

    def load_template(self, config, option, default=None):
        name = "%s:%s" % (config.get_name(), option)
        if default is None:
            script = config.get(option)
        else:
            script = config.get(option, default)
        return TemplateWrapper(self.printer, self.env, name, script)

    def create_template_context(self, eventtime=None):
        return {
            'printer': _StatusWrapper(self.printer, eventtime),
            'action_emergency_stop': lambda msg='': None,
            'action_respond_info': lambda msg: None,
            'action_raise_error': lambda msg: None,
            'action_call_remote_method': lambda method, **kw: None,
        }


class _StatusWrapper:
    """Minimal printer.<obj>.<field> access for template rendering."""

    def __init__(self, printer, eventtime=None):
        self.printer = printer
        self.eventtime = eventtime

    def __getitem__(self, key):
        obj = self.printer.lookup_object(key, None)
        if obj is None or not hasattr(obj, 'get_status'):
            return {}
        et = self.eventtime
        if et is None:
            et = self.printer.get_reactor().monotonic()
        return obj.get_status(et)

    def __getattr__(self, key):
        return self[key]

    def __contains__(self, key):
        return self.printer.lookup_object(key, None) is not None


class GCodeMacro:
    def __init__(self, config):
        name = config.get_name().split()[-1]
        self.alias = name.upper()
        self.printer = printer = config.get_printer()
        gcode_macro = printer.load_object(config, 'gcode_macro')
        self.template = gcode_macro.load_template(config, 'gcode', '')
        self.gcode = printer.lookup_object('gcode')
        self.rename_existing = config.get('rename_existing', None)
        self.description = config.get('description', 'G-Code macro')
        self.variables = {}
        # Klipper parses `variable_<name>: <literal>` into .variables
        import ast
        for option in config.get_prefix_options('variable_'):
            try:
                self.variables[option[len('variable_'):]] = ast.literal_eval(
                    config.get(option).strip())
            except (SyntaxError, ValueError):
                self.variables[option[len('variable_'):]] = config.get(option).strip()
        self.calls = []         # test assertion surface
        self.gcode.register_command(self.alias, self.cmd, desc=self.description)

    def cmd(self, gcmd):
        # Recorded no-op: see module docstring.
        self.calls.append(gcmd.get_commandline())

    def get_status(self, eventtime=None):
        return dict(self.variables)


def load_config(config):
    return PrinterGCodeMacro(config)


def load_config_prefix(config):
    return GCodeMacro(config)
