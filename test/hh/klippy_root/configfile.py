# Fake Klipper `klippy/configfile.py` for the Happy Hare test harness.
#
# Klipper's ConfigWrapper semantics are ported LITERALLY, including minval/maxval/
# above/below/count validation. This matters more than it looks: Happy Hare
# validates ~200 tunables through this API (MmuUnitParameters, MmuMachineParameters,
# via the reflective _SourceAdapter in extras/mmu/mmu_base_parameters.py), so a fake
# that quietly ignored a limit would turn every config-validation test into a no-op.
# For the same reason the getters do NOT absorb **kwargs - an unrecognised keyword
# raises, so a Klipper API change surfaces instead of being swallowed.
#
# `fileconfig` is a genuine mutable RawConfigParser because four HH sites mutate it
# in place and must keep working:
#   extras/mmu/unit/mmu_leds.py:32-35        add_section/getsection/remove_section to
#                                            build a real led.LEDHelper
#   extras/mmu_led_effect.py:107-113         synthesises [_led_effect ...] sections
#   extras/mmu/unit/mmu_extruder_wrapper.py:63-66,89-91
#                                            removes then restores [extruder] options
#   extras/mmu/mmu_unit.py:277-292           fileconfig.set to share gear/TMC params
#
# getsection() of a missing section must return a wrapper rather than raise -
# bare-section lookups depend on it.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class sentinel:
    pass


class error(Exception):
    pass


class ConfigWrapper:
    error = error

    def __init__(self, printer, fileconfig, access_tracking, section):
        self.printer = printer
        self.fileconfig = fileconfig
        self.access_tracking = access_tracking
        self.section = section

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.section

    def has_section(self, section):
        return self.fileconfig.has_section(section)

    def _get_wrapper(self, parser, option, default, minval=None, maxval=None,
                     above=None, below=None, note_valid=True):
        if not self.fileconfig.has_option(self.section, option):
            if default is not sentinel:
                if note_valid and default is not None:
                    self.access_tracking[(self.section.lower(), option.lower())] = default
                return default
            raise error("Option '%s' in section '%s' must be specified"
                        % (option, self.section))
        try:
            v = parser(self.section, option)
        except error:
            raise
        except Exception:
            raise error("Unable to parse option '%s' in section '%s'"
                        % (option, self.section))
        if note_valid:
            self.access_tracking[(self.section.lower(), option.lower())] = v
        if minval is not None and v < minval:
            raise error("Option '%s' in section '%s' must have minimum of %s"
                        % (option, self.section, minval))
        if maxval is not None and v > maxval:
            raise error("Option '%s' in section '%s' must have maximum of %s"
                        % (option, self.section, maxval))
        if above is not None and v <= above:
            raise error("Option '%s' in section '%s' must be above %s"
                        % (option, self.section, above))
        if below is not None and v >= below:
            raise error("Option '%s' in section '%s' must be below %s"
                        % (option, self.section, below))
        return v

    def get(self, option, default=sentinel, note_valid=True):
        return self._get_wrapper(self.fileconfig.get, option, default,
                                 note_valid=note_valid)

    def getint(self, option, default=sentinel, minval=None, maxval=None,
               note_valid=True):
        return self._get_wrapper(self.fileconfig.getint, option, default,
                                 minval, maxval, note_valid=note_valid)

    def getfloat(self, option, default=sentinel, minval=None, maxval=None,
                 above=None, below=None, note_valid=True):
        return self._get_wrapper(self.fileconfig.getfloat, option, default,
                                 minval, maxval, above, below,
                                 note_valid=note_valid)

    def getboolean(self, option, default=sentinel, note_valid=True):
        return self._get_wrapper(self.fileconfig.getboolean, option, default,
                                 note_valid=note_valid)

    def getchoice(self, option, choices, default=sentinel, note_valid=True):
        if choices and not isinstance(choices, dict):
            choices = {i: i for i in choices}
        if default is not sentinel and default not in choices:
            # Klipper allows a default outside the choice map (e.g. None)
            pass
        c = self.get(option, default, note_valid=note_valid)
        if c not in choices:
            raise error("Choice '%s' for option '%s' in section '%s'"
                        " is not a valid choice" % (c, option, self.section))
        return choices[c]

    def getlists(self, option, default=sentinel, seps=(',',), count=None,
                 parser=str, note_valid=True):
        def lparser(value, pos):
            if pos:
                # nested separators
                sub = [lparser(p, pos - 1) for p in value.split(seps[-pos])]
                return tuple(sub)
            parts = [parser(p.strip()) for p in value.split(seps[0]) if p.strip() != '']
            if count is not None and len(parts) != count:
                raise error("Option '%s' in section '%s' must have %d elements"
                            % (option, self.section, count))
            return tuple(parts)

        def fcparser(section, option):
            return lparser(self.fileconfig.get(section, option), len(seps) - 1)

        return self._get_wrapper(fcparser, option, default, note_valid=note_valid)

    def getlist(self, option, default=sentinel, seps=(',',), count=None,
                note_valid=True):
        return self.getlists(option, default, seps, count, parser=str,
                             note_valid=note_valid)

    def getintlist(self, option, default=sentinel, seps=(',',), count=None,
                   note_valid=True):
        return self.getlists(option, default, seps, count, parser=int,
                             note_valid=note_valid)

    def getfloatlist(self, option, default=sentinel, seps=(',',), count=None,
                     note_valid=True):
        return self.getlists(option, default, seps, count, parser=float,
                             note_valid=note_valid)

    def getsection(self, section):
        # Must NOT raise for a missing section - see module docstring
        return ConfigWrapper(self.printer, self.fileconfig, self.access_tracking,
                             section)

    def get_prefix_sections(self, prefix):
        return [self.getsection(s) for s in self.fileconfig.sections()
                if s.startswith(prefix)]

    def get_prefix_options(self, prefix):
        return [o for o in self.fileconfig.options(self.section)
                if o.startswith(prefix)]

    def deprecate(self, option, value=None):
        pass


class PrinterConfig:
    """Only deprecate_gcode is used by HH (extras/mmu_stepper.py:1084)."""

    def __init__(self, printer):
        self.printer = printer
        self.status_raw_config = {}
        self.status_save_pending = {}
        self.status_settings = {}
        self.status_warnings = []
        self.save_config_pending = False
        self.deprecated = {}
        self.runtime_warnings = []
        self.set_values = {}       # test assertion surface for configfile.set()

    def deprecate_gcode(self, cmd, msg=None):
        self.deprecated[cmd] = msg

    def deprecate(self, section, option, value=None, msg=None):
        self.deprecated[(section, option)] = msg

    def set(self, section, option, value):
        self.set_values[(section, option)] = value

    def remove_section(self, section):
        self.set_values[(section, None)] = 'REMOVED'

    def get_status(self, eventtime):
        return {'config': self.status_raw_config,
                'settings': self.status_settings,
                'warnings': self.status_warnings,
                'save_config_pending': self.save_config_pending,
                'save_config_pending_items': self.status_save_pending}

    def runtime_warning(self, msg):
        self.runtime_warnings.append(msg)
