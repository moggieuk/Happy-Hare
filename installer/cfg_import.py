# Happy Hare MMU Software
#
# Import values the installed .cfg files hold into menuconfig at the start of an interactive
# Refresh, so menuconfig opens showing what is actually running: hand edits, and defaults a
# release has since changed. The build then applies only what menuconfig changed this run
# (build.menuconfig_changes), against the baseline saved here once any import is done.
#
# Only a template line whose whole value is one symbol can be traced back to it, and only a
# symbol menuconfig shows can hold a user value. Anything else stays in the .cfg files.
#
# Each Kconfig file needs its own parse environment, so install.sh runs this once per file
# to --report (also saving the --baseline), shows the combined report with --show and asks
# once, then on yes runs it once per file again to --apply what was shown
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import argparse
import ast
import glob
import json
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
EXIT_SHOWN = 10

# One symbol as the whole value, any trailing comment: "option: [[SYMBOL]]",
# "option: [[SYMBOL|klipper_string_literal]]" or a per-gate "option_[[i]]: [[ (SYMBOL_|d)[i] ]]"
_SYMBOL = r"(?:PARAM|VAR|PIN)_[A-Z0-9_]+"
_PLAIN_REF = re.compile(
    r"^(?P<head>[A-Za-z_][A-Za-z0-9_]*(?:\[\[[^\]]*\]\])?\s*[:=]\s*)"
    r"(?P<value>\[\[\s*(?:"
    r"(?P<sym>%s)\s*(?P<literal>\|\s*klipper_string_literal\s*)?"
    r"|\(\s*(?P<array>%s_)\s*\|\s*d\s*\)\s*\[(?P<index>[^\]]+)\]\s*"
    r")\]\])(?P<tail>.*)$" % (_SYMBOL, _SYMBOL))
_MARKER = re.compile(r"@@HHSYM (\S+) ([01])@@")
_SECTION = re.compile(r"^\[([^\]]+)\]")
_OPTION = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:=]")
_NUM_GATES = re.compile(r"^CONFIG_PARAM_NUM_GATES=(\d+)")
_PREVIOUS_MEMBER = re.compile(r"_PREVIOUS(_\d+)?$")


class Candidate(object):
    def __init__(self, sym_name, literal, locations, menuconfig_value, cfg_value):
        self.sym_name = sym_name
        self.literal = literal
        self.locations = locations              # [(section, option), ...]
        self.menuconfig_value = menuconfig_value
        self.cfg_value = cfg_value
        self.set_name = None                    # The symbol menuconfig sets: this one, its toggle or choice member
        self.set_key = None                     # What can only take one value: set_name, or the choice
        self.kconfig_value = None

    def note(self):
        """Verbose detail: the value it replaces and the symbol that holds it"""
        sym = self.sym_name if self.set_name in (None, self.sym_name) else \
            "%s, set by %s" % (self.sym_name, self.set_name)
        return "was %s; %s" % (self.menuconfig_value, sym)


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
            # An array element's marker renders its index into the symbol name
            sym = m.group("sym") or "%s[[ %s ]]" % (m.group("array"), m.group("index").strip())
            line = "%s%s@@HHSYM %s %d@@%s" % (
                m.group("head"), m.group("value"), sym, 1 if m.group("literal") else 0, m.group("tail"))
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
    """.cfg values menuconfig doesn't show, split into importable candidates and the rest"""

    def __init__(self, kconfig_path, installed_files):
        self.kconfig_path = kconfig_path
        self.candidates = []
        self.kept = []                         # Can't be imported: [((section, option), value, reason)]

        # Recomputed defaults are what menuconfig shows, so a .cfg still holding a default
        # a release has since changed is imported too. Trial values are judged here, so
        # kconfiglib's warnings about rejecting them are only noise
        self.kconf = build.KConfig(kconfig_path)
        self.kconf.load_config(kconfig_path, filter_defaults=True)
        self.kconf.warn = False
        values = self.kconf.as_dict()

        # The installed file each option is in, for the summary
        self.origin = {}
        for path in installed_files:
            installed = ConfigBuilder(path)
            for section in installed.sections():
                for option in installed.options(section):
                    self.origin.setdefault((section, option), os.path.basename(path))

        hhcfg = build.HHConfig(installed_files)
        build.upgrade_hhconfig(hhcfg, "existing config")

        self.templates = templates_for(values)
        self.total_gates = total_gates(kconfig_path, values)
        self.env = build._jinja_env(REPO_ROOT)

        rendered = self._render(values)
        self.live = self._options(rendered)
        symbols = {}
        for template in self.templates:
            with open(os.path.join(REPO_ROOT, template), encoding="utf-8") as f:
                marked = mark_template(f.read())
            symbols.update(symbol_lines(self.env.from_string(marked).render(self._params(values))))

        # Every (section, option) each symbol renders into, so an edit to one copy isn't
        # imported when the other copies say something else
        uses = {}
        for key, ref in symbols.items():
            if ref and rendered.has_option(*key) and hhcfg.has_option(*key):
                uses.setdefault(ref, []).append(key)

        edited = {}
        self.order = {}
        for section in rendered.sections(scope="included"):
            for option in rendered.options(section):
                key = (section, option)
                self.order[key] = len(self.order)
                ref = symbols.get(key)
                if ref is None or option == "gcode" or not hhcfg.has_option(*key) or \
                        (section.split(" ", 1)[0], option) in build.KCONFIG_OWNED_OPTIONS:
                    continue
                ours, theirs = rendered.get(*key) or "", hhcfg.get(*key) or ""
                if not same_value(ours, theirs, ref[1]):
                    edited.setdefault(ref, []).append((key, ours, theirs))

        for (sym_name, literal), edits in sorted(edited.items()):
            locations = uses[(sym_name, literal)]
            cfg_values = {(hhcfg.get(*key) or "").strip() for key in locations}
            _, ours, theirs = edits[0]
            if len(cfg_values) > 1:
                for key, _, value in edits:
                    self.kept.append((key, value, "%s is set differently elsewhere" % sym_name))
                continue
            self._consider(Candidate(sym_name, literal, locations, ours.strip(), theirs.strip()))

        self._drop_conflicts()

    def _params(self, values):
        params = dict(values)
        params["PARAM_TOTAL_NUM_GATES"] = self.total_gates
        return params

    def _render(self, values):
        rendered = ConfigBuilder()
        for template in self.templates:
            rendered.read_buf(self.env.get_template(template).render(self._params(values)))
        return rendered

    @staticmethod
    def _options(rendered):
        """{(section, option): value}, with None for each section, for comparing renders"""
        found = {}
        for section in rendered.sections(scope="included"):
            found[(section, None)] = None
            for option in rendered.options(section):
                found[(section, option)] = (rendered.get(section, option) or "").strip()
        return found

    def _drop_conflicts(self):
        """A toggle or choice can't take two values, so neither edit that needs it can be imported"""
        wanted = {}
        for candidate in self.candidates:
            wanted.setdefault(candidate.set_key, set()).add((candidate.set_name, candidate.kconfig_value))
        for candidate in [c for c in self.candidates if len(wanted[c.set_key]) > 1]:
            self.candidates.remove(candidate)
            for key in candidate.locations:
                self.kept.append((key, candidate.cfg_value, "%s is needed two ways" % candidate.set_key))

    def _consider(self, candidate):
        sym = self.kconf.syms[candidate.sym_name]
        value = kconfig_value(sym, candidate.cfg_value, candidate.literal)
        reason = why_not_settable(sym, value)
        setting = None
        if reason is None:
            ok = self._reproduces(sym, value, candidate)
            setting = (sym, value) if ok is True else None
            reason = None if ok is True else {
                False: "outside the range menuconfig allows",
                None: "not a valid %s value" % kconfiglib.TYPE_TO_STR[sym.orig_type],
            }.get(ok, ok)
        elif reason == HIDDEN:
            for through in (self._through_toggle, self._through_choice):
                setting, why = through(sym, candidate)
                if setting or why:
                    reason = why
                    break

        if reason is None:
            candidate.set_name, candidate.kconfig_value = setting[0].name, setting[1]
            candidate.set_key = setting[0].choice.name if setting[0].choice else setting[0].name
            self.candidates.append(candidate)
        else:
            for key in candidate.locations:
                self.kept.append((key, candidate.cfg_value, reason))

    def _reproduces(self, set_sym, value, candidate):
        """
        True if setting set_sym to value renders the .cfg value and changes nothing else.
        None if the value can't be set, False if it doesn't render, or the reason when it
        would also change another option or section
        """
        if not set_sym.set_value(value):
            return None
        # kconfiglib keeps a value outside the symbol's range but saves the clamped one
        if set_sym.orig_type in (kconfiglib.BOOL, kconfiglib.BOOLINT, kconfiglib.TRISTATE):
            honoured = set_sym.tri_value == {"y": 2, "1": 2, "m": 1, "n": 0, "0": 0}.get(value, value)
        else:
            honoured = set_sym.str_value == value
        after = self._options(self._render(self.kconf.as_dict()))
        self.kconf.load_config(self.kconfig_path, filter_defaults=True)
        if not honoured:
            return False

        ok = all(same_value(after.get(key), candidate.cfg_value, candidate.literal) for key in candidate.locations)
        changed = [key for key in sorted(set(after) | set(self.live), key=str)
                   if key not in candidate.locations and after.get(key) != self.live.get(key)]
        if ok and changed:
            return "would also change %s" % ", ".join(
                "[%s]" % s if o is None else "[%s] %s" % (s, o) for s, o in changed[:2])
        return ok

    def _conditions(self, sym):
        return {s for _, cond in sym.defaults for s in kconfiglib.expr_items(cond)
                if isinstance(s, kconfiglib.Symbol)}

    def _through(self, candidate, trials, what):
        """((symbol, value), None) for the first trial that gives the .cfg value, else (None, reason)"""
        side_effect = None
        for set_sym, value in trials:
            ok = self._reproduces(set_sym, value, candidate)
            if ok is True:
                return (set_sym, value), None
            if isinstance(ok, str):
                side_effect = side_effect or ok
        return None, side_effect or "no %s gives this value" % what

    def _through_toggle(self, sym, candidate):
        """For a hidden symbol whose defaults follow one menuconfig toggle; (None, None) if none do"""
        # Only a BOOL_ partner: a capability flag in the same condition switches hardware
        toggles = [s for s in self._conditions(sym)
                   if s.name.startswith("BOOL_") and s.orig_type in (kconfiglib.BOOL, kconfiglib.BOOLINT)
                   and why_not_settable(s, "y") is None]
        if len(toggles) != 1:
            return None, None
        return self._through(candidate, [(toggles[0], v) for v in ("y", "n")],
                             "setting of %s" % toggles[0].name)

    def _through_choice(self, sym, candidate):
        """For a hidden symbol whose defaults follow one menuconfig list; (None, None) if none do"""
        choices = {s.choice for s in self._conditions(sym)
                   if s.choice is not None and (s.choice.name or "").startswith("CHOICE_")}
        if len(choices) != 1:
            return None, None
        choice = choices.pop()
        if not choice.visibility:
            return None, None
        # A *_PREVIOUS member only re-offers the saved value
        members = [m for m in choice.syms if m.visibility and not _PREVIOUS_MEMBER.search(m.name)]
        return self._through(candidate, [(m, "y") for m in members], "%s option" % choice.name)

    def apply(self, accepted):
        apply_settings(self.kconfig_path, [[c.set_name, c.kconfig_value] for c in accepted], self.kconf)

    def report(self):
        """The JSON-ready summary --show prints and the settings --apply makes"""
        def rows(entries):
            return [[self.origin.get(key, "?"), key[0], key[1], value.strip(), note]
                    for key, value, note in sorted(entries, key=lambda e: self.order.get(e[0], 0))]
        return {
            "kconfig": os.path.abspath(self.kconfig_path),
            "imported": rows([(key, c.cfg_value, c.note()) for c in self.candidates for key in c.locations]),
            "kept": rows(self.kept),
            "settings": [[c.set_name, c.kconfig_value] for c in self.candidates],
        }


def apply_settings(kconfig_path, settings, kconf=None, baseline_dir=None):
    """
    Save menuconfig values as explicit settings, keeping every other value as it was saved.
    With baseline_dir, also save the result there, with defaults recomputed, as the
    baseline the build compares the post-menuconfig config with
    """
    kconf = kconf or build.KConfig(kconfig_path)
    kconf.load_config(kconfig_path, filter_defaults=True)
    for name, value in settings:
        kconf.syms[name].set_value(value)
    if settings:
        kconf.write_config(kconfig_path)
    if baseline_dir:
        os.makedirs(baseline_dir, exist_ok=True)
        kconf.write_config(os.path.join(baseline_dir, os.path.basename(kconfig_path)), save_old=False)


def installed_files(config_home):
    return sorted(glob.glob(os.path.join(config_home, "mmu", "base", "*.cfg")))


def show_edits(heading, rows, verbose, out):
    """Print report rows grouped by .cfg file, then section"""
    print(heading, file=out)
    grouped = {}
    for name, section, option, value, note in rows:
        line = "%s: %s" % (option, value) + (" (%s)" % note if verbose else "")
        grouped.setdefault(name, {}).setdefault(section, []).append(line)
    for i, (name, sections) in enumerate(grouped.items()):
        if i:
            print(file=out)
        print("From %s:" % name, file=out)
        for section, lines in sections.items():
            print("[%s]" % section, file=out)
            for line in lines:
                print(line, file=out)
    print(file=out)


def read_report(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def show(path, verbose=False, out=None):
    """Print the combined report. EXIT_SHOWN if there is anything to confirm"""
    out = out or sys.stdout
    reports = read_report(path)
    failed = [r for r in reports if r.get("failed")]
    if failed:
        for r in failed:
            print("WARNING: couldn't check '%s' for .cfg values to import (%s), continuing without"
                  % (os.path.basename(r["kconfig"]), r["failed"]), file=out)
        return EXIT_NOTHING

    # The import list comes last, straight above the prompt
    sections = (
        ("kept", "These .cfg values stay in the .cfg files but can't be imported into menuconfig:", verbose),
        ("imported", "These .cfg values will be imported into menuconfig:", True),
    )
    for name, heading, wanted in sections:
        rows = [row for r in reports for row in r[name]]
        if rows and wanted:
            show_edits(heading, rows, verbose, out)
    return EXIT_SHOWN if any(r["imported"] for r in reports) else EXIT_NOTHING


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import hand-edited .cfg values into menuconfig")
    parser.add_argument("--kconfig")
    parser.add_argument("--config-home")
    parser.add_argument("--report", metavar="FILE", help="append this Kconfig file's edits to FILE")
    parser.add_argument("--apply", metavar="FILE", help="import this Kconfig file's edits listed in FILE")
    parser.add_argument("--show", metavar="FILE", help="print the edits every Kconfig file reported")
    parser.add_argument("--baseline", metavar="DIR", help="also save the Kconfig file to DIR for the build")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.show:
        return show(args.show, args.verbose)

    kconfig = os.path.abspath(args.kconfig)
    if args.apply:
        settings = [s for r in read_report(args.apply) if r["kconfig"] == kconfig for s in r.get("settings", [])]
        if settings:
            try:
                apply_settings(kconfig, settings, baseline_dir=args.baseline)
            except (Exception, SystemExit) as e:
                print("WARNING: couldn't import the .cfg edits into '%s' (%s)" % (os.path.basename(kconfig), e),
                      file=sys.stderr)
                return EXIT_FAILED
            print("Imported %d value(s) into '%s'" % (len(settings), os.path.basename(kconfig)))
        return EXIT_NOTHING

    files = installed_files(os.path.expanduser(args.config_home or ""))
    if not files or not os.path.exists(kconfig):
        return EXIT_NOTHING
    status = EXIT_NOTHING
    try:
        analysis = Analysis(kconfig, files)
        report = analysis.report()
        if args.baseline:
            apply_settings(kconfig, [], analysis.kconf, args.baseline)
    except (Exception, SystemExit) as e:
        report, status = {"kconfig": kconfig, "failed": str(e)}, EXIT_FAILED
    with open(args.report, "a", encoding="utf-8") as f:
        f.write(json.dumps(report) + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
