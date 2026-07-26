# Happy Hare test harness - render the REAL shipped config templates.
#
# We reuse exactly two functions from installer/build.py - render_template (:459)
# and KConfig.as_dict (:178) - and bypass all the heavy machinery in
# build_config_file (:508). No .mmu_config, no dill pickle, no OUT/SRC build dir,
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
#     checks for exactly that.
#  3. extra_params must supply PARAM_TOTAL_NUM_GATES / UNIT_NAME / MCU_NAME,
#     mirroring installer/build.py:481-491.
#  4. render_template calls exit(1) on a Jinja UndefinedError (build.py:468-470),
#     so a template bug would otherwise make the test process vanish. We catch
#     SystemExit and re-raise as a normal failure.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os, re, sys, contextlib, configparser

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
BASE_TEMPLATES = (
    'config/base/mmu.cfg',
    'config/base/mmu_hardware.cfg',
    'config/base/mmu_macro_vars.cfg',
    'config/base/mmu_parameters.cfg',
)

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


_ENV = {
    'srctree': INSTALLER,
    'SRC': REPO_ROOT,
    'HH_VERSION': hh_version(),
    'KLIPPER_HOME': '/nonexistent/klipper',
    'CONFIG_KLIPPER_HOME': '/nonexistent/klipper',
    'CONFIG_KLIPPER_CONFIG_HOME': '/nonexistent/printer_data/config',
    'CONFIG_MOONRAKER_HOME': '/nonexistent/moonraker',
    'CONFIG_SERVICE_KLIPPER': 'klipper.service',
    'UNIT_NAME': 'unit0',
    'MCU_NAME': 'unit0',
    'F_MULTI_UNIT': '',
    'F_MULTI_UNIT_ENTRY_POINT': '',
    'F_PER_GATE_MCU': '',
}


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
    # Gotcha 2: env must be in place before Kconfig() parses.
    for k, v in _ENV.items():
        os.environ.setdefault(k, v)


def _kconfig(profile):
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
            self.config_file = '<harness:%s>' % (profile.name,)

    with _chdir(INSTALLER):
        kc = _HarnessKConfig()
        for name, value in profile.syms.items():
            if name not in kc.syms:
                raise KeyError("profile %r sets unknown Kconfig symbol %r"
                               % (profile.name, name))
            sym = kc.syms[name]
            if isinstance(value, bool):
                # BOOL/TRISTATE user values are int tri-states: 2 == y, 0 == n
                sym.set_value(2 if value else 0)
            else:
                sym.set_value(str(value))
    return kc


_render_cache = {}


def render(profile):
    """
    Render every base template for `profile`. Returns an ordered dict of
    {template_path: rendered_text} in Klipper's include order.

    Memoised per profile: parsing the ~1500-symbol Kconfig takes several seconds
    and dominates harness runtime otherwise. Callers must not mutate the result;
    assemble() builds a fresh parser from it each time.
    """
    key = (profile.name, tuple(sorted(profile.syms.items())),
           tuple(sorted(profile.extra_params.items())))
    if key in _render_cache:
        return _render_cache[key]

    _prepare_imports()          # must precede `import installer.build` (needs kconfiglib)
    import installer.build as build

    kc = _kconfig(profile)
    num_gates = kc.getint('PARAM_NUM_GATES')
    extra = {
        'PARAM_TOTAL_NUM_GATES': num_gates,
        'UNIT_NAME': _ENV['UNIT_NAME'],
        'MCU_NAME': _ENV['MCU_NAME'],
    }
    extra.update(profile.extra_params)

    out = {}
    with _chdir(REPO_ROOT):
        for tmpl in BASE_TEMPLATES:
            try:
                out[tmpl] = build.render_template(tmpl, kc, extra)
            except SystemExit as e:
                # Gotcha 4: render_template exits the process on a Jinja
                # UndefinedError. Turn it into a normal failure.
                raise AssertionError(
                    "render_template(%r) called exit(%s) - almost certainly a Jinja "
                    "UndefinedError from a template referencing a param this profile "
                    "does not define. Re-run with logging at ERROR to see which."
                    % (tmpl, e.code))
    _render_cache[key] = out
    return out


# A pin value that renders as ':PD5' means an env var (usually MCU_NAME) was missing
# when Kconfig parsed - see gotcha 2. Matches `key: :something` / `key: !:something`.
_MALFORMED_PIN = re.compile(r'^\s*[A-Za-z_][A-Za-z_0-9]*\s*:\s*\^?~?!?:')


def assert_sane(rendered):
    """Catch the silent-misrender failure modes. Raises AssertionError."""
    problems = []
    for tmpl, text in rendered.items():
        for lineno, line in enumerate(text.splitlines(), 1):
            if _MALFORMED_PIN.match(line):
                problems.append('%s:%d has a chip-less pin (missing env var?): %r'
                                % (tmpl, lineno, line.strip()))
        if '[[' in text or '[%' in text:
            for lineno, line in enumerate(text.splitlines(), 1):
                if '[[' in line or '[%' in line:
                    problems.append('%s:%d left an unrendered template token: %r'
                                    % (tmpl, lineno, line.strip()))
    if problems:
        raise AssertionError('\n'.join([''] + problems))


def sections(text):
    """Section names in file order, as configparser would see them."""
    return re.findall(r'^\[([^\]]+)\]', text, re.M)


def assemble(rendered, printer_stub=''):
    """
    Build the single RawConfigParser Klipper would see, reading the parts in
    include order.

    strict=False is required: [extruder] legitimately appears in more than one
    input (the stub supplies its stepper options, mmu_macro_vars.cfg supplies its
    extrude limits). Interpolation is off because macro bodies are full of '%'.
    """
    fileconfig = configparser.RawConfigParser(
        strict=False,
        comment_prefixes=('#', ';'),
        inline_comment_prefixes=('#', ';'))
    # Klipper preserves case in option names
    fileconfig.optionxform = str
    if printer_stub:
        fileconfig.read_string(printer_stub, source='printer_stub.cfg')
    for tmpl in BASE_TEMPLATES:
        if tmpl in rendered:
            fileconfig.read_string(rendered[tmpl], source=tmpl)
    return fileconfig
