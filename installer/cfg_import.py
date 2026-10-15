# Happy Hare MMU Software
#
# Offer values hand edited in the installed .cfg files back to menuconfig before a
# Merge install. Merge applies every PARAM_/VAR_ value saved in .mmu_config, so without
# this a value tuned only in a .cfg file reverts to the older value menuconfig holds.
#
# Only a template line whose whole value is one symbol can be traced back to it, and
# only a symbol menuconfig shows can hold a user value. Everything else Merge would
# overwrite is listed so the user knows
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import argparse
import ast
import glob
import os
import re
import sys

import kconfiglib
from . import build
from .parser import ConfigBuilder, MAGIC_EXCLUSION_COMMENT

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SHARED_TEMPLATES = ["config/base/mmu.cfg", "config/base/mmu_macro_vars.cfg"]
UNIT_TEMPLATES = ["config/base/mmu_hardware.cfg", "config/base/mmu_parameters.cfg"]

EXIT_NOTHING = 0
EXIT_FAILED = 2

# "option: [[SYMBOL]]" or "option: [[SYMBOL|klipper_string_literal]]", any trailing comment
_PLAIN_REF = re.compile(
    r"^(?P<head>[A-Za-z_][A-Za-z0-9_]*\s*[:=]\s*)"
    r"\[\[\s*(?P<sym>(?:PARAM|VAR)_[A-Z0-9_]+)\s*(?P<literal>\|\s*klipper_string_literal\s*)?\]\]"
    r"(?P<tail>.*)$")
_MARKER = re.compile(r"@@HHSYM (\S+) ([01])@@")
_SECTION = re.compile(r"^\[([^\]]+)\]")
_OPTION = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:=]")
_NUM_GATES = re.compile(r"^CONFIG_PARAM_NUM_GATES=(\d+)")


class Candidate(object):
    def __init__(self, sym_name, literal, locations, menuconfig_value, cfg_value):
        self.sym_name = sym_name
        self.literal = literal
        self.locations = locations              # [(section, option), ...]
        self.menuconfig_value = menuconfig_value
        self.cfg_value = cfg_value
        self.set_name = None                    # The symbol menuconfig sets: this one, or its toggle
        self.kconfig_value = None

    def describe(self):
        where = ", ".join("[%s] %s" % loc for loc in self.locations)
        sym = self.sym_name if self.set_name in (None, self.sym_name) else \
            "%s, set by %s" % (self.sym_name, self.set_name)
        return "%s: menuconfig %s / .cfg %s (%s)" % (where, self.menuconfig_value, self.cfg_value, sym)


def templates_for(values):
    if build.kconfig_truthy(values.get("MULTI_UNIT_ENTRY_POINT")):
        return SHARED_TEMPLATES
    if build.kconfig_truthy(values.get("MULTI_UNIT")):
        return UNIT_TEMPLATES
    return SHARED_TEMPLATES + UNIT_TEMPLATES


def total_gates(kconfig_path, values):
    """PARAM_TOTAL_NUM_GATES as the build computes it, read from the saved unit files"""
    if not build.kconfig_truthy(values.get("MULTI_UNIT_ENTRY_POINT")):
        return int(values["PARAM_NUM_GATES"])
    total = 0
    for unit in build.split_csv(values.get("MMU_UNITS", "")):
        with open("%s_%s" % (kconfig_path, unit), encoding="utf-8") as f:
            for line in f:
                m = _NUM_GATES.match(line)
                if m:
                    total += int(m.group(1))
                    break
    return total


def mark_template(text):
    """Tag each line whose whole value is one symbol, so the render says which symbol it came from"""
    out = []
    for line in text.split("\n"):
        m = _PLAIN_REF.match(line)
        if m:
            line = "%s[[%s%s]]@@HHSYM %s %d@@%s" % (
                m.group("head"), m.group("sym"), m.group("literal") or "",
                m.group("sym"), 1 if m.group("literal") else 0, m.group("tail"))
        out.append(line)
    return "\n".join(out)


def symbol_lines(rendered):
    """{(section, option): (symbol, literal)} from a marked render; None where it's ambiguous"""
    found = {}
    section = None
    for line in rendered.split("\n"):
        if MAGIC_EXCLUSION_COMMENT.match(line):
            break
        m = _SECTION.match(line)
        if m:
            section = m.group(1).strip()
            continue
        marker = _MARKER.search(line)
        option = _OPTION.match(line)
        if marker and option and section:
            key = (section, option.group(1))
            value = (marker.group(1), marker.group(2) == "1")
            found[key] = value if found.get(key, value) == value else None
    return found


def same_value(a, b, literal=False):
    a, b = (a or "").strip(), (b or "").strip()
    if a == b:
        return True
    if literal:
        try:
            return ast.literal_eval(a) == ast.literal_eval(b)
        except (ValueError, SyntaxError):
            return False
    try:
        return float(a) == float(b)
    except ValueError:
        pass
    truthy = {"true": True, "1": True, "false": False, "0": False}
    return a.lower() in truthy and truthy[a.lower()] == truthy.get(b.lower())


def kconfig_value(sym, text, literal):
    """The value to assign, or None if the .cfg text can't be one"""
    text = text.strip()
    if literal:
        try:
            text = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return None
        if not isinstance(text, str) or "\n" in text:
            return None
    if sym.orig_type in (kconfiglib.BOOL, kconfiglib.BOOLINT):
        return {"true": "y", "1": "y", "false": "n", "0": "n"}.get(text.lower())
    return text


def rendered_value(sym, literal):
    """What the template renders for the symbol's current value"""
    value = sym.str_value
    if sym.orig_type == kconfiglib.BOOLINT:
        value = "1" if value in ("1", "y") else "0"
    elif sym.orig_type == kconfiglib.BOOL:
        value = str(value == "y")
    return build.klipper_string_literal(value) if literal else value


HIDDEN = "not shown in menuconfig"


def why_not_settable(sym, value):
    """None if the value can be set in menuconfig, otherwise the reason"""
    if sym.choice is not None:
        return "chosen from a list in menuconfig"
    if not sym.visibility or not any(node.prompt for node in sym.nodes):
        return HIDDEN
    if value is None:
        return "not a valid %s value" % kconfiglib.TYPE_TO_STR[sym.orig_type]
    validator = getattr(sym, "validator", None)
    if validator is not None and sym.orig_type == kconfiglib.STRING:
        sep = getattr(sym, "array_editor", None)
        elements = (value.split(sep) if value else []) if sep else [value]
        if not all(validator.fullmatch(e.strip()) for e in elements):
            return "rejected by menuconfig's validation"
    return None


class Analysis(object):
    """Hand edits a Merge install would overwrite, split into importable candidates and the rest"""

    def __init__(self, kconfig_path, installed_files):
        self.kconfig_path = kconfig_path
        self.candidates = []
        self.blocked = []                      # [((section, option), cfg value, reason)]

        self.kconf = build.KConfig(kconfig_path)
        saved = self.kconf.as_dict()

        hhcfg = build.HHConfig(installed_files)
        build.upgrade_hhconfig(hhcfg, "existing config")

        params = dict(saved)
        params["PARAM_TOTAL_NUM_GATES"] = total_gates(kconfig_path, saved)
        env = build._jinja_env(REPO_ROOT)

        rendered = ConfigBuilder()
        symbols = {}
        for template in templates_for(saved):
            rendered.read_buf(env.get_template(template).render(params))
            with open(os.path.join(REPO_ROOT, template), encoding="utf-8") as f:
                symbols.update(symbol_lines(env.from_string(mark_template(f.read())).render(params)))

        excluded_params, excluded_vars = build.merge_exclusions(saved)
        excluded_vars = set(excluded_vars)

        # Every (section, option) each symbol renders into, so an edit to one copy isn't
        # imported when the other copies say something else
        uses = {}
        for key, ref in symbols.items():
            if ref and rendered.has_option(*key) and hhcfg.has_option(*key):
                uses.setdefault(ref, []).append(key)
        self.mapped = sorted(uses)

        edited = {}
        for section in rendered.sections(scope="included"):
            for option in rendered.options(section):
                key = (section, option)
                if option == "gcode" or not hhcfg.has_option(*key) or \
                        (section.split(" ", 1)[0], option) in build.KCONFIG_OWNED_OPTIONS or \
                        not build.kconfig_wins(section, option, excluded_params, excluded_vars):
                    continue
                ref = symbols.get(key)
                ours, theirs = rendered.get(*key), hhcfg.get(*key)
                if same_value(ours, theirs, ref[1] if ref else False):
                    continue
                if ref is None:
                    self.blocked.append((key, theirs, "not a plain menuconfig value"))
                else:
                    edited.setdefault(ref, []).append((key, ours, theirs))

        # Recomputed defaults are what menuconfig shows and saves. Trial values are judged
        # here, so kconfiglib's warnings about rejecting them are only noise
        self.kconf.load_config(kconfig_path, filter_defaults=True)
        self.kconf.warn = False

        for (sym_name, literal), edits in sorted(edited.items()):
            locations = uses[(sym_name, literal)]
            cfg_values = {hhcfg.get(*key).strip() for key in locations}
            _, ours, theirs = edits[0]
            if len(cfg_values) > 1:
                for key, _, value in edits:
                    self.blocked.append((key, value, "%s is set differently elsewhere" % sym_name))
                continue
            self._consider(Candidate(sym_name, literal, locations, ours.strip(), theirs.strip()))

        # One toggle can't take two values
        wanted = {}
        for candidate in self.candidates:
            wanted.setdefault(candidate.set_name, set()).add(candidate.kconfig_value)
        for candidate in [c for c in self.candidates if len(wanted[c.set_name]) > 1]:
            self.candidates.remove(candidate)
            for key in candidate.locations:
                self.blocked.append((key, candidate.cfg_value, "%s is needed both ways" % candidate.set_name))

    def _consider(self, candidate):
        sym = self.kconf.syms[candidate.sym_name]
        value = kconfig_value(sym, candidate.cfg_value, candidate.literal)
        reason = why_not_settable(sym, value)
        setting = None
        if reason is None:
            ok = self._reproduces(sym, value, sym, candidate)
            setting = (sym, value) if ok is True else None
            reason = None if ok is True else {
                False: "outside the range menuconfig allows",
                None: "not a valid %s value" % kconfiglib.TYPE_TO_STR[sym.orig_type],
            }.get(ok, ok)
        elif reason == HIDDEN:
            setting = self._through_toggle(sym, candidate)
            reason = None if setting else reason

        if reason is None:
            candidate.set_name, candidate.kconfig_value = setting[0].name, setting[1]
            self.candidates.append(candidate)
        else:
            for key in candidate.locations:
                self.blocked.append((key, candidate.cfg_value, reason))

    def _reproduces(self, set_sym, value, sym, candidate):
        """
        True if setting set_sym to value makes sym render the .cfg value. None if the value
        can't be set, or the reason when it would also change another value Merge renders
        """
        others = [ref for ref in self.mapped if ref[0] != sym.name]
        before = [self._value(ref) for ref in others]
        if not set_sym.set_value(value):
            return None
        ok = same_value(rendered_value(sym, candidate.literal), candidate.cfg_value, candidate.literal)
        changed = [ref[0] for ref, old in zip(others, before) if self._value(ref) != old]
        self.kconf.load_config(self.kconfig_path, filter_defaults=True)
        if ok and changed:
            return "would also change %s" % ", ".join(changed)
        return ok

    def _value(self, ref):
        return rendered_value(self.kconf.syms[ref[0]], ref[1])

    def _through_toggle(self, sym, candidate):
        """(toggle, value) for a hidden symbol whose defaults follow one menuconfig toggle"""
        # Only a BOOL_ partner: a capability flag in the same condition switches hardware
        toggles = {
            s for _, cond in sym.defaults for s in kconfiglib.expr_items(cond)
            if isinstance(s, kconfiglib.Symbol) and s.name.startswith("BOOL_")
            and s.orig_type in (kconfiglib.BOOL, kconfiglib.BOOLINT) and why_not_settable(s, "y") is None
        }
        if len(toggles) != 1:
            return None
        toggle = toggles.pop()
        for value in ("y", "n"):
            if self._reproduces(toggle, value, sym, candidate) is True:
                return toggle, value
        return None

    def apply(self, accepted):
        """Save the accepted candidates as explicit menuconfig values"""
        for candidate in accepted:
            self.kconf.syms[candidate.set_name].set_value(candidate.kconfig_value)
        self.kconf.write_config(self.kconfig_path)


def installed_files(config_home):
    return sorted(glob.glob(os.path.join(config_home, "mmu", "base", "*.cfg")))


def ask(question, choices="yn"):
    """First letter of the answer, from 'choices'; end of input answers n"""
    while True:
        try:
            answer = input("%s (%s)? " % (question, "/".join(choices))).strip().lower()
        except EOFError:
            print()
            return "n"
        if answer[:1] in choices:
            return answer[:1]


def review(analysis, out=None):
    """Show the hand edits and ask which to import. Returns the accepted candidates"""
    out = out or sys.stdout
    name = os.path.basename(analysis.kconfig_path)
    if analysis.blocked:
        print("Merge will overwrite these .cfg edits, which can't be imported into '%s':" % name, file=out)
        for (section, option), value, reason in analysis.blocked:
            print("  [%s] %s: %s  (%s)" % (section, option, value, reason), file=out)
    if not analysis.candidates:
        return []

    print("These .cfg edits differ from '%s' and Merge would revert them:" % name, file=out)
    for candidate in analysis.candidates:
        print("  " + candidate.describe(), file=out)
    if ask("Review these .cfg values for import into menuconfig") != "y":
        return []

    accepted = []
    for i, candidate in enumerate(analysis.candidates):
        answer = ask("%s. Use the .cfg value" % candidate.describe(), "ynaq")
        if answer == "a":
            accepted.extend(analysis.candidates[i:])
            break
        if answer == "q":
            break
        if answer == "y":
            accepted.append(candidate)
    return accepted


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import hand-edited .cfg values into menuconfig")
    parser.add_argument("--kconfig", required=True)
    parser.add_argument("--config-home", required=True)
    args = parser.parse_args(argv)

    files = installed_files(os.path.expanduser(args.config_home))
    if not files or not os.path.exists(args.kconfig):
        return EXIT_NOTHING

    try:
        analysis = Analysis(args.kconfig, files)
        accepted = review(analysis)
        if accepted:
            analysis.apply(accepted)
            print("Imported %d value(s) into '%s'" % (len(accepted), os.path.basename(args.kconfig)))
    except (Exception, SystemExit) as e:
        print("WARNING: couldn't check the .cfg files for values to import (%s), continuing with Merge" % e,
              file=sys.stderr)
        return EXIT_FAILED
    return EXIT_NOTHING


if __name__ == "__main__":
    sys.exit(main())
