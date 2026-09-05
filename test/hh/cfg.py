# Happy Hare test harness - render the REAL shipped config templates.
#
# We reuse exactly two functions from installer/build.py - render_template (:459)
# and KConfig.as_dict (:178) - and bypass all the heavy machinery in
# build_config_file (:508). No .mmu_config, no kconfig pickle, no OUT/SRC build dir,
# no version-upgrade migrations, no filesystem writes.
#
# Four gotchas are encoded here; each one silently produces wrong output rather
# than an error if you get it wrong:
#
#  1. TWO different working directories. kconfiglib.Kconfig('Kconfig') needs cwd =
#     repo/installer (with srctree set); render_template uses
#     FileSystemLoader(".") + os.path.relpath(template) (build.py:446-462) so it
#     needs cwd = repo root. Both are wrapped so cwd is always restored - leaving
#     it modified breaks unrelated tests in the same process.
#  2. Env vars must be set BEFORE Kconfig() is constructed: kconfiglib expands
#     $(VAR) at parse time. Notably, omitting MCU_NAME renders pins as ':PD5'
#     instead of 'unit0:PD5' - silently wrong config, not an error. assert_sane()
#     checks for exactly that. _env() owns this, per parse, with restore - see the
#     multi-unit note below for why it cannot be set once and left.
#  3. extra_params must supply PARAM_TOTAL_NUM_GATES, mirroring
#     installer/build.py:481-491 - the SUM across units on a multi-unit machine.
#     UNIT_NAME/MCU_NAME are deliberately NOT injected: they are Kconfig symbols
#     resolved from the env, and injecting them would mask an env that never
#     reached the parse.
#  4. render_template calls exit(1) on a Jinja UndefinedError (build.py:468-470),
#     so a template bug would otherwise make the test process vanish. We catch
#     SystemExit and re-raise as a normal failure.
#
# MULTI-UNIT. A one-unit profile is one parse; a multi-unit profile is one entry-point parse
# for the shared files plus one parse PER UNIT for mmu_hardware/mmu_parameters, mirroring
# install.sh:385-432. The env does not merely carry different values between those parses, it
# changes the Kconfig's SHAPE: MULTI_UNIT / MULTI_UNIT_ENTRY_POINT / UNIT_NAME / MCU_NAME /
# UNIT_INDEX are all env-driven (Kconfig:146-186) and whole symbol sets appear and disappear
# behind `if MULTI_UNIT_ENTRY_POINT`. That is why a multi-unit profile cannot be expressed as
# one larger syms dict, and why the env is per-parse rather than module state.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os, re, sys, glob, contextlib, configparser

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HARNESS_DIR))
INSTALLER = os.path.join(REPO_ROOT, 'installer')
KCONFIGLIB = os.path.join(INSTALLER, 'lib', 'kconfiglib')

# Klipper expands `[include mmu/base/*.cfg]` with a SORTED glob, and the ordering is
# load-bearing: [mmu_machine] (mmu.cfg) must be processed before
# [mmu_stepper unit0_gear] (mmu_hardware.cfg), because MmuUnit force-loads each gear
# stepper itself with force_rail=True (extras/mmu/mmu_unit.py:305-312). If the generic
# section loop reached the stepper first it would be built WITHOUT a rail and HH's
# later add_object would collide. Production only works because of this sort order,
# so test_mmu_config.py asserts it explicitly.
# Rendered ONCE, from the shared (entry-point) Kconfig. On a multi-unit machine these stay
# single files: mmu.cfg is just `units: [[MMU_UNITS]]`, and the Makefile builds both through
# the generic $(OUT)/mmu/%.cfg rule off the base .mmu_config (Makefile:266-271).
SHARED_TEMPLATES = (
    'config/base/mmu.cfg',
    'config/base/mmu_macro_vars.cfg',
)

# Rendered ONCE PER UNIT, each from that unit's own Kconfig. The installer replaces the two
# single-unit files with per-unit ones (Makefile:151-158) built from $(KCONFIG_CONFIG)_<unit>
# (Makefile:273-284), so the installed names are mmu_hardware_<unit>.cfg /
# mmu_parameters_<unit>.cfg. mmu_hardware.cfg alone has 36 UNIT_NAME references.
PER_UNIT_TEMPLATES = (
    'config/base/mmu_hardware.cfg',
    'config/base/mmu_parameters.cfg',
)

# LED themes: per-unit templates like the set above, installed to mmu/led_theme/ (NOT
# glob-included from printer.cfg - the unit's hardware file includes the one named by
# PARAM_LED_THEME). Rendered like the other per-unit files so the include line can be
# inlined at assemble() time (see _expand_includes).
THEME_TEMPLATES = tuple(sorted(os.path.relpath(p, REPO_ROOT)
                               for p in glob.glob(os.path.join(REPO_ROOT, 'config/led_theme/*.cfg'))))

# Sorted, because that IS the include order (see above) - and because on a multi-unit render
# the per-unit names have to interleave correctly: mmu.cfg, mmu_hardware_unit0.cfg,
# mmu_hardware_unit1.cfg, mmu_macro_vars.cfg, mmu_parameters_unit*.cfg. Every path shares
# the config/base/ prefix, so sorting full paths and sorting basenames agree.
# Theme templates render per unit like the set above but install to mmu/led_theme/; they
# come after config/base/ in this sort, which matches reality (the hardware file includes
# its theme, so the theme never precedes it).
BASE_TEMPLATES = tuple(sorted(SHARED_TEMPLATES + PER_UNIT_TEMPLATES + THEME_TEMPLATES))

# config/macros/*.cfg are COPIED VERBATIM by the installer, not rendered - Makefile:148
# filters mmu/macros/%.cfg out of the rendered set. They must therefore be read raw:
# pushing them through render_template fails, because they are Klipper Jinja ({% %} / { })
# and a nested list literal like [[a, b]|min, c]|max collides with the installer's own
# [[ ]] variable delimiter (config/macros/mmu_sequence.cfg:155).
#
# They matter because Happy Hare refuses to run sequences whose macros are missing - an
# unload fails with "Filament tip forming macro '_MMU_FORM_TIP' not found" without them.
MACRO_GLOB = 'config/macros/*.cfg'

# Values that only matter for path interpolation in the templates. Pointed at
# harmless placeholders; the harness overrides the ones that matter (e.g.
# [save_variables] filename) after rendering.
def hh_version():
    """
    The canonical Happy Hare version, extracted the same way install.sh:32 does
    (sed over mmu_constants.py). Must not be hardcoded: [mmu_machine] renders
    happy_hare_version from $HH_VERSION and extras/mmu_machine.py:46 rejects a
    config whose major.minor is older than the running code, so a stale literal
    here would fail config load in a thoroughly confusing way.
    """
    path = os.path.join(REPO_ROOT, 'extras', 'mmu', 'mmu_constants.py')
    with open(path, encoding='utf-8') as f:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    assert m, 'could not find VERSION in %s' % (path,)
    return m.group(1)


# Invariant across every parse: paths and the version. Nothing unit-shaped belongs here.
_BASE_ENV = {
    'srctree': INSTALLER,
    'SRC': REPO_ROOT,
    'HH_VERSION': hh_version(),
    'KLIPPER_HOME': '/nonexistent/klipper',
    'CONFIG_KLIPPER_HOME': '/nonexistent/klipper',
    'CONFIG_KLIPPER_CONFIG_HOME': '/nonexistent/printer_data/config',
    'CONFIG_MOONRAKER_HOME': '/nonexistent/moonraker',
    'CONFIG_SERVICE_KLIPPER': 'klipper.service',
    'F_PER_GATE_MCU': '',
}

# What install.sh passes when there is no MULTI_UNIT (install.sh:397-398 takes the else
# branch and adds nothing, so these are the ambient values a one-unit machine sees).
#
# UNIT_INDEX is given as a real '0' rather than left unset: Kconfig:175 reads it as
# $(shell, echo "${UNIT_INDEX-0}"), and ${VAR-default} substitutes only when the variable is
# UNSET - an empty string would survive and render an empty int.
_SINGLE_UNIT_ENV = {
    'UNIT_NAME': 'unit0',
    'MCU_NAME': 'unit0',
    'UNIT_INDEX': '0',
    'F_MULTI_UNIT': '',
    'F_MULTI_UNIT_ENTRY_POINT': '',
}


@contextlib.contextmanager
def _env(overrides):
    """
    Install the env for ONE Kconfig parse, then put it back.

    Two halves, both load-bearing (this is gotcha 2 with teeth):

    ASSIGNMENT, not setdefault. kconfiglib expands $(VAR) at PARSE time, into every board
    pin default - so a multi-unit render needs a genuinely different UNIT_NAME/MCU_NAME for
    each of its parses. `setdefault` silently kept the first parse's values, which is why
    two units could not previously coexist in one session.

    RESTORE, not leak. The suite renders many profiles in one process. A leaked
    MCU_NAME=unit1 would re-render later single-unit profiles against unit1's MCU, or drop
    the chip entirely and give ':PD5' pins - wrong output, not an error. assert_sane()
    catches the chip-less form; nothing catches the wrong-chip form, so the restore is the
    only guard. test_mmu_config asserts a boxturtle render is unchanged by an intervening
    multi-unit one.

    Only kconfiglib reads these, and only while constructing Kconfig - render_template works
    off the already-resolved as_dict(), so the env need not be live during rendering.
    """
    env = dict(_BASE_ENV)
    env.update(overrides)
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update({key: str(value) for key, value in env.items()})
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def _chdir(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def _prepare_imports():
    """kconfiglib is vendored (Makefile:49 puts it on PYTHONPATH); mirror that."""
    for p in (KCONFIGLIB, REPO_ROOT):
        if p not in sys.path:
            sys.path.insert(0, p)


def _new_kconfig(label):
    """Parse a fresh Kconfig tree in the caller's currently installed environment."""
    _prepare_imports()
    import kconfiglib
    import installer.build as build

    class _HarnessKConfig(build.KConfig):
        """
        build.KConfig.__init__ insists on loading a .config file; we set symbols
        programmatically instead, so bypass it and keep the helper methods
        (as_dict / is_enabled / getint / get / is_selected).
        """

        def __init__(self):
            kconfiglib.Kconfig.__init__(self, 'Kconfig')
            self.config_file = '<harness:%s>' % (label,)

    with _chdir(INSTALLER):
        return _HarnessKConfig()


def _apply_syms(kc, label, syms, reset=False):
    """Apply one profile's values to a fresh or resettable parsed tree."""
    if reset:
        kc.unset_values()
    kc.config_file = '<harness:%s>' % (label,)
    for name, value in syms.items():
        if name not in kc.syms:
            raise KeyError("profile %r sets unknown Kconfig symbol %r"
                           % (label, name))
        sym = kc.syms[name]
        if isinstance(value, bool):
            # BOOL/TRISTATE user values are int tri-states: 2 == y, 0 == n
            sym.set_value(2 if value else 0)
        else:
            sym.set_value(str(value))
    return kc


def _kconfig(label, syms):
    """
    One independent Kconfig parse with `syms` applied. The CALLER owns the env - wrap this
    in _env(), because kconfiglib expands $(VAR) while Kconfig() is being constructed here.

    Direct callers often retain and mutate the returned object, so this deliberately does
    not use the render-only parsed-tree cache below.
    """
    return _apply_syms(_new_kconfig(label), label, syms)


def _flag(kc, name):
    """
    A Kconfig bool as the 'y'/'' shape install.sh:424-426 hands down to each unit. Guarded
    on existence because the symbol lives behind an `if` in some configurations.
    """
    return 'y' if (name in kc.syms and kc.is_enabled(name)) else ''


def _render_templates(templates, kc, extra, name_of=None):
    """
    Render `templates` with `kc`, keyed by the INSTALLED name. `name_of` maps a template
    path to that name (per-unit files gain a _<unit> suffix); identity by default.
    """
    import installer.build as build

    out = {}
    with _chdir(REPO_ROOT):
        for tmpl in templates:
            try:
                out[name_of(tmpl) if name_of else tmpl] = build.render_template(
                    tmpl, kc, extra)
            except SystemExit as e:
                # Gotcha 4: render_template exits the process on a Jinja
                # UndefinedError. Turn it into a normal failure.
                raise AssertionError(
                    "render_template(%r) called exit(%s) - almost certainly a Jinja "
                    "UndefinedError from a template referencing a param this profile "
                    "does not define. Re-run with logging at ERROR to see which."
                    % (tmpl, e.code))
    return out


def _per_unit_name(tmpl, unit_name):
    """config/base/mmu_hardware.cfg -> config/base/mmu_hardware_unit1.cfg (Makefile:151-158)"""
    root, ext = os.path.splitext(tmpl)
    return '%s_%s%s' % (root, unit_name, ext)


_render_cache = {}
_render_kconfig_cache = {}


def _render_kconfig_key():
    """Environment identity for a parsed tree used only during rendering.

    Kconfig expands environment variables while parsing, including variables referenced by
    recursive preprocessor functions. Keying on the complete environment is intentionally
    conservative: an unrelated change merely costs another parse, while omitting a relevant
    value could render valid-looking pins for the wrong unit. Dynamic discovery is already
    assumed stable for a process by `_render_cache`, which memoises final output.
    """
    return tuple(sorted(os.environ.items()))


def _kconfig_for_render(label, syms):
    """Apply values to a parsed tree leased synchronously by the render path.

    The returned object must not escape rendering: the next render in the same environment
    resets all symbol and choice user values with Kconfiglib's public `unset_values()` API.
    Tests which need an independent, persistent Kconfig object continue to use `_kconfig()`.
    """
    key = _render_kconfig_key()
    kc = _render_kconfig_cache.get(key)
    if kc is None:
        kc = _new_kconfig(label)
        _render_kconfig_cache[key] = kc
        return _apply_syms(kc, label, syms)
    return _apply_syms(kc, label, syms, reset=True)


def render(profile):
    """
    Render every base template for `profile`. Returns an ordered dict of
    {template_path: rendered_text} in Klipper's include order.

    Memoised per profile: parsing the ~1500-symbol Kconfig takes several seconds
    and dominates harness runtime otherwise. Callers must not mutate the result;
    assemble() builds a fresh parser from it each time.

    An InstallDirProfile has nothing to render - the installer already did it - so the
    files are read verbatim instead. That is the point: it exercises the real installer
    output, including any hand edits made afterwards.
    """
    if getattr(profile, 'install_dir', False):
        return load_install_dir(profile)

    units = tuple(getattr(profile, 'units', None) or ())
    # Per-unit syms and env are load-bearing, so they belong in the key. Without them two
    # multi-unit profiles differing only in a unit's config would collide, and - worse - the
    # unchanged-boxturtle leak test would pass by returning a cached render.
    key = (profile.name,
           tuple(sorted(profile.syms.items())),
           tuple(sorted(profile.extra_params.items())),
           tuple((u.name, u.mcu_name, u.index, tuple(sorted(u.syms.items())))
                 for u in units))
    if key in _render_cache:
        return _render_cache[key]

    _prepare_imports()          # must precede `import installer.build` (needs kconfiglib)

    out = _render_multi_unit(profile, units) if units else _render_single_unit(profile)
    _render_cache[key] = out
    return out


def _render_single_unit(profile):
    with _env(_SINGLE_UNIT_ENV):
        kc = _kconfig_for_render(profile.name, profile.syms)

    # Note we do NOT inject UNIT_NAME/MCU_NAME as jinja params. Production does not
    # (build.py:478-494 sets only PARAM_TOTAL_NUM_GATES) and they are already Kconfig
    # symbols here, resolved from the env above. Injecting them would paper over an env that
    # never reached the parse - exactly the bug _env() exists to prevent.
    extra = {'PARAM_TOTAL_NUM_GATES': kc.getint('PARAM_NUM_GATES')}
    extra.update(profile.extra_params)
    return _render_templates(BASE_TEMPLATES, kc, extra)


def _render_multi_unit(profile, units):
    """
    Three parses, mirroring install.sh run_kconfig_top (:385-399) + run_kconfig_units
    (:401-432): one entry-point parse for the shared files, then one per unit.

    The env is not merely different per parse, it changes the Kconfig's SHAPE - whole symbol
    sets appear and disappear behind `if MULTI_UNIT_ENTRY_POINT` (Kconfig:158-186), which is
    why this cannot be one flatter syms dict.
    """
    names = [u.name for u in units]

    # Entry point. UNIT_NAME is set to the joined list because Kconfig:159-162 defaults
    # MMU_UNITS from it; a profile that sets MMU_UNITS explicitly (as it should) just
    # overrides that with the same value.
    with _env(dict(_SINGLE_UNIT_ENV,
                   F_MULTI_UNIT='y',
                   F_MULTI_UNIT_ENTRY_POINT='y',
                   UNIT_NAME=','.join(names),
                   MCU_NAME=','.join(names))):
        entry_kc = _kconfig_for_render(profile.name, profile.syms)

    # Printer-level capabilities the units need to know about, read back off the entry parse
    # exactly as install.sh:418-427 reads them back out of the top-level config file.
    handed_down = {
        'HAS_SENSOR_TOOLHEAD': _flag(entry_kc, 'MMU_HAS_SENSOR_TOOLHEAD'),
        'HAS_SENSOR_EXTRUDER': _flag(entry_kc, 'MMU_HAS_SENSOR_EXTRUDER'),
        'HAS_SENSOR_TOOLHEAD_CUTTER': _flag(entry_kc, 'MMU_HAS_TOOLHEAD_CUTTER'),
    }

    unit_kcs = []
    for unit in units:
        with _env(dict(_SINGLE_UNIT_ENV, **dict(
                handed_down,
                F_MULTI_UNIT='y',
                F_MULTI_UNIT_ENTRY_POINT='',
                UNIT_NAME=unit.name,
                MCU_NAME=unit.mcu_name,
                UNIT_INDEX=str(unit.index)))):
            unit_kcs.append(
                (unit, _kconfig_for_render(
                    '%s:%s' % (profile.name, unit.name), unit.syms)))

    # The SUM across units, not this unit's count - build.py:481-492. It drives the Tx macro
    # wrappers, which are printer-wide.
    extra = {'PARAM_TOTAL_NUM_GATES': sum(kc.getint('PARAM_NUM_GATES')
                                          for _u, kc in unit_kcs)}
    extra.update(profile.extra_params)

    rendered = _render_templates(SHARED_TEMPLATES, entry_kc, extra)
    for unit, kc in unit_kcs:
        rendered.update(_render_templates(
            PER_UNIT_TEMPLATES + THEME_TEMPLATES, kc, extra,
            name_of=lambda tmpl, n=unit.name: _per_unit_name(tmpl, n)))

    # Rebuild in installed-name order: assemble() treats insertion order AS include order,
    # and Klipper's glob is sorted. Do not rely on the order the loops above happen to give.
    return {name: rendered[name] for name in sorted(rendered)}


# A pin value that renders as ':PD5' means an env var (usually MCU_NAME) was missing
# when Kconfig parsed - see gotcha 2. Matches `key: :something` / `key: !:something`.
_MALFORMED_PIN = re.compile(r'^\s*[A-Za-z_][A-Za-z_0-9]*\s*:\s*\^?~?!?:')


def _is_rendered(name):
    """
    macros/ and optional/ are COPIED VERBATIM by the installer, never rendered (see the
    MACRO_GLOB comment above), and they legitimately contain the same [[ ]] token the
    installer uses as its own delimiter - a nested Klipper list literal like
    [[a, b]|min, c]|max (config/macros/mmu_sequence.cfg:155). Only rendered files can be
    asserted token-free.
    """
    return '/macros/' not in name and '/optional/' not in name


def assert_sane(rendered):
    """Catch the silent-misrender failure modes. Raises AssertionError."""
    problems = []
    for tmpl, text in rendered.items():
        for lineno, line in enumerate(text.splitlines(), 1):
            if _MALFORMED_PIN.match(line):
                problems.append('%s:%d has a chip-less pin (missing env var?): %r'
                                % (tmpl, lineno, line.strip()))
        if _is_rendered(tmpl) and ('[[' in text or '[%' in text):
            for lineno, line in enumerate(text.splitlines(), 1):
                if '[[' in line or '[%' in line:
                    problems.append('%s:%d left an unrendered template token: %r'
                                    % (tmpl, lineno, line.strip()))
    if problems:
        raise AssertionError('\n'.join([''] + problems))


def sections(text):
    """Section names in file order, as configparser would see them."""
    return re.findall(r'^\[([^\]]+)\]', text, re.M)


def macro_files():
    """The shipped macro files, verbatim. Sorted, as Klipper's include glob would be."""
    import glob
    out = {}
    for path in sorted(glob.glob(os.path.join(REPO_ROOT, MACRO_GLOB))):
        with open(path, encoding='utf-8') as f:
            out[os.path.relpath(path, REPO_ROOT)] = f.read()
    return out


class InstallDirProfile:
    """
    Stands in for a Profile when the config comes from a real install rather than from
    Kconfig symbols. Same duck type: .name, .syms, .extra_params - but render() short
    circuits on .install_dir and reads files instead.
    """

    install_dir = True

    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.name = self.path
        self.syms = {}
        self.extra_params = {}
        self.description = 'installed config at %s' % self.path

    def __repr__(self):
        return 'InstallDirProfile(%r)' % (self.path,)


def _includes_from_printer_cfg(path):
    """
    The `[include mmu/...]` globs from a real printer.cfg, in file order.

    Authoritative rather than guessed: installer/build.py:698-709 writes
    `include mmu/base/*.cfg` above `include mmu/macros/*.cfg`, and reading them back also
    picks up anything else the user added (e.g. mmu/optional/*.cfg).
    """
    out = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            m = re.match(r'^\s*\[include\s+(mmu/[^\]]+?)\s*\]', line)
            if m:
                out.append(m.group(1))
    return out


def load_install_dir(profile):
    """
    Read an installed Happy Hare config into the same ordered {name: text} shape render()
    returns, so everything downstream is unchanged.

    `profile.path` may be either the printer_data/config directory (which has printer.cfg
    and an mmu/ subdirectory) or the mmu/ directory itself.
    """
    import glob

    root = profile.path
    if not os.path.isdir(root):
        raise AssertionError('no such install directory: %s' % root)

    printer_cfg = os.path.join(root, 'printer.cfg')
    patterns = None
    if os.path.isfile(printer_cfg):
        patterns = _includes_from_printer_cfg(printer_cfg)
    if not patterns:
        # Pointed straight at mmu/, or a printer.cfg with no HH includes (an uninstalled
        # or partially installed tree). Mirror what the installer would have written.
        base = root if os.path.basename(root) == 'mmu' else os.path.join(root, 'mmu')
        if not os.path.isdir(base):
            raise AssertionError(
                "%s has neither an [include mmu/...] in printer.cfg nor an mmu/ "
                "directory - is this a Happy Hare install? Run './install.sh -z -t' to "
                "make one in /tmp/mmu_test." % root)
        root = os.path.dirname(base)    # so 'mmu/...' resolves relative to root
        patterns = ['mmu/base/*.cfg', 'mmu/macros/*.cfg']

    # LED theme files are never listed in printer.cfg (the unit's hardware file includes
    # them), so pick them up explicitly; an empty glob is harmless on older installs.
    if patterns is not None and 'mmu/led_theme/*.cfg' not in patterns:
        patterns = list(patterns) + ['mmu/led_theme/*.cfg']

    out = {}
    for pattern in patterns:
        # Sorted, because Klipper's include glob is sorted and the order is load-bearing:
        # [mmu_machine] in mmu.cfg must be parsed before the steppers in mmu_hardware*.cfg
        # (see the BASE_TEMPLATES comment above).
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            name = os.path.relpath(path, root)
            if os.path.basename(path) == 'mmu_vars.cfg':
                # Not included by printer.cfg (it is reached via [save_variables]) and the
                # harness substitutes its own writable copy after assembly.
                continue
            with open(path, encoding='utf-8') as f:
                out[name] = f.read()

    if not out:
        raise AssertionError('no config files found under %s for %s'
                             % (root, ', '.join(patterns)))
    first = next(iter(out))
    if not first.endswith('mmu.cfg'):
        raise AssertionError(
            'expected mmu.cfg to load first (it carries [mmu_machine], which must be '
            'parsed before the steppers) but got %r. Include order is wrong.' % first)
    return out


# Klipper 'include' directive line; the path resolves relative to the including file.
INCLUDE_RE = re.compile(r'^\s*\[include[ \t]+([^\]]*)\]')


def _resolve_include_ref(ref, rendered, including_key):
    """
    Map a Klipper include path (relative to the including file's directory) to the key
    of the rendered dict it names, or None. The including file's rendered key decides
    the config-root-relative directory the reference resolves against. Multi-unit
    renders key per-unit templates as config/led_theme/<t>_<unit>.cfg (matching the
    installed name); single-unit renders omit the suffix, so fall back to a prefix
    match on the basename.
    """
    if ref.startswith(('/', 'mmu/', 'config/')):
        normalized = ref
    else:
        install_key = 'mmu/' + including_key[len('config/'):] \
            if including_key.startswith('config/') else including_key
        normalized = os.path.normpath(os.path.join(os.path.dirname(install_key), ref))
    if normalized in rendered:
        return normalized
    if normalized.startswith('mmu/'):
        key = 'config/' + normalized[len('mmu/'):]
        if key in rendered:
            return key
    root, ext = os.path.splitext(os.path.basename(normalized))
    for name in rendered:
        if not name.startswith(('config/led_theme/', 'mmu/led_theme/')):
            continue
        kbase, kext = os.path.splitext(os.path.basename(name))
        if ext == kext and root.startswith(kbase + '_'):
            return name
    return None


def _expand_includes(name, text, rendered):
    """
    Replace '[include <path>]' lines with the referenced file's text, the way Klipper's
    configfile would. The harness parses a flat dict instead of a directory tree, so
    without this the included file's content (e.g. a LED theme's effect_* options)
    would be missing from the assembled config.
    """
    out = []
    for line in text.split('\n'):
        match = INCLUDE_RE.match(line)
        if match:
            key = _resolve_include_ref(match.group(1).strip(), rendered, name)
            if key is not None and key in rendered:
                out.append(rendered[key].rstrip('\n'))
                continue
        out.append(line)
    return '\n'.join(out)


def assemble(rendered, printer_stub='', macros=True):
    """
    Build the single RawConfigParser Klipper would see, reading the parts in
    include order.

    strict=False is required: [extruder] legitimately appears in more than one
    input (the stub supplies its stepper options, mmu_macro_vars.cfg supplies its
    extrude limits). Interpolation is off because macro bodies are full of '%'.

    'include <path>' lines are expanded inline (see _expand_includes); a file
    that is only ever reached through such an include is not read twice.
    """
    fileconfig = configparser.RawConfigParser(
        strict=False,
        comment_prefixes=('#', ';'),
        inline_comment_prefixes=('#', ';'))
    # Klipper preserves case in option names
    fileconfig.optionxform = str
    if printer_stub:
        fileconfig.read_string(printer_stub, source='printer_stub.cfg')
    # Insertion order IS include order. render() builds its dict by iterating
    # BASE_TEMPLATES so this is identical for a profile, and it generalises to the
    # arbitrary file set load_install_dir() produces. Files referenced only through
    # an '[include]' line in an EARLIER file are spliced in there and skipped here;
    # the including files (e.g. mmu/base/*) always sort before their include
    # targets (mmu/led_theme/*), so a single pass in dict order is sufficient.
    # LED theme files are additionally never read on their own even when not
    # included: printer.cfg does not glob mmu/led_theme/, so an unselected theme
    # (e.g. the EMU one on a stock machine) sits installed but is unreachable -
    # reading it would wrongly merge its [mmu_leds <unit>] options over the
    # selected theme's.
    inlined = set()
    for name, text in rendered.items():
        if name in inlined or name.startswith(('config/led_theme/', 'mmu/led_theme/')):
            continue
        for line in text.split('\n'):
            match = INCLUDE_RE.match(line)
            if match:
                key = _resolve_include_ref(match.group(1).strip(), rendered, name)
                if key is not None:
                    inlined.add(key)
        fileconfig.read_string(_expand_includes(name, text, rendered), source=name)
    if macros and not any(n.startswith('config/macros/') or '/macros/' in n
                          for n in rendered):
        # An install directory carries its own macros (possibly hand-edited); only fall
        # back to the repo's when the caller did not supply any.
        for name, text in macro_files().items():
            fileconfig.read_string(text, source=name)
    return fileconfig
