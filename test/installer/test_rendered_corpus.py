# Digests of everything the shipped templates render.
#
# The structural snapshot in test_symbol_inventory says what the tree
# DECLARES. This says what it PRODUCES. A refactor that is meant to be
# output-neutral - factoring a fragment out, re-sourcing it elsewhere - is
# only provably so if the bytes are unchanged, and that is not something a
# handful of targeted assertions can establish.
#
# Two corpora, because the profiles alone are not enough: no registered
# profile enables the Blobifier, so its whole driver block - every TMC chip,
# both buses, three SPI wiring modes - renders in no profile test at all. The
# matrix below is the only coverage it has.
#
# Regenerate after an intended change:
#     HH_REGEN_GOLDEN=1 make test UT='test_rendered_corpus.py'
# and read the diff before committing it.

import difflib
import hashlib
import os
import unittest

from test.hh import cfg, profiles

MMU = 'config/base/mmu.cfg'

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'inventory')
PROFILE_GOLDEN = os.path.join(GOLDEN_DIR, 'render_digests.txt')
BLOBIFIER_GOLDEN = os.path.join(GOLDEN_DIR, 'blobifier_digests.txt')
BLOBIFIER_TEXT = os.path.join(GOLDEN_DIR, 'blobifier_reference.txt')

# A stepper tray on a Box Turtle, with the pins a real install would set.
_BLOBIFIER_BASE = {
    'MMU_HAS_BLOBIFIER': True,
    'CHOICE_BLOBIFIER_TYPE_STEPPER': True,
    'PIN_BLOBIFIER_STEPPER_STEP': 'unit0:PD4',
    'PIN_BLOBIFIER_STEPPER_DIR': '!unit0:PD3',
    'PIN_BLOBIFIER_STEPPER_ENABLE': '!unit0:PD6',
    'PIN_BLOBIFIER_STEPPER_ENDSTOP': '^!unit0:PC15',
}
_UART = {'PIN_BLOBIFIER_STEPPER_UART': 'unit0:PC14'}
_CS = {'PIN_BLOBIFIER_STEPPER_CS': 'unit0:PC14'}
_SW_SPI = dict(_CS, **{
    'PIN_BLOBIFIER_STEPPER_SPI_SCLK': 'unit0:PG8',
    'PIN_BLOBIFIER_STEPPER_SPI_MOSI': 'unit0:PG6',
    'PIN_BLOBIFIER_STEPPER_SPI_MISO': 'unit0:PG7',
})
_NAMED_BUS = dict(_CS, **{'PARAM_BLOBIFIER_STEPPER_SPI_BUS': 'spi2'})

# Every chip, both buses for the one that has a choice, and all three SPI
# wiring modes. The names are the golden's keys, so keep them stable.
BLOBIFIER_MATRIX = {
    'tmc2209_uart': dict(_UART, **{'CHOICE_BLOBIFIER_TMC2209': True}),
    'tmc2226_uart': dict(_UART, **{'CHOICE_BLOBIFIER_TMC2226': True}),
    'tmc2208_uart': dict(_UART, **{'CHOICE_BLOBIFIER_TMC2208': True}),
    'tmc2130_hwspi': dict(_CS, **{'CHOICE_BLOBIFIER_TMC2130': True}),
    'tmc2660_hwspi': dict(_CS, **{'CHOICE_BLOBIFIER_TMC2660': True}),
    'tmc5160_hwspi': dict(_CS, **{'CHOICE_BLOBIFIER_TMC5160': True}),
    'tmc5160_named_bus': dict(_NAMED_BUS, **{'CHOICE_BLOBIFIER_TMC5160': True}),
    'tmc5160_swspi': dict(_SW_SPI, **{'CHOICE_BLOBIFIER_TMC5160': True}),
    'tmc2240_spi': dict(_CS, **{'CHOICE_BLOBIFIER_TMC2240': True}),
    'tmc2240_swspi': dict(_SW_SPI, **{'CHOICE_BLOBIFIER_TMC2240': True}),
    'tmc2240_uart': dict(_UART, **{'CHOICE_BLOBIFIER_TMC2240': True,
                                   'CHOICE_BLOBIFIER_TMC_BUS_UART': True}),
    'tmc_none': {'CHOICE_BLOBIFIER_TMC_NONE': True},
    'servo_tray': {'MMU_HAS_BLOBIFIER': True, 'CHOICE_BLOBIFIER_TYPE_SERVO': True,
                   'PIN_BLOBIFIER_SERVO': 'unit0:PA1'},
}

# Kept as full text rather than a digest so a failure shows what moved.
REFERENCE_CASES = ('tmc2209_uart', 'tmc2240_spi', 'tmc5160_swspi')


def _blobifier_sections(name, syms):
    base = dict(_BLOBIFIER_BASE) if name != 'servo_tray' else {}
    profile = profiles.get('boxturtle').derive(
        'corpus_blobifier_' + name, syms=dict(base, **syms))
    mmu = cfg.render(profile)[MMU]
    parsed = cfg.assemble({MMU: mmu})
    out = []
    for section in cfg.sections(mmu):
        if 'blobifier' not in section.lower():
            continue
        out.append('[%s]' % section)
        out.extend('%s: %s' % item for item in parsed.items(section))
    return out


def _compare(testcase, actual, path, what):
    if os.environ.get('HH_REGEN_GOLDEN'):
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(actual) + '\n')
        testcase.skipTest('regenerated %s' % os.path.basename(path))
    with open(path, encoding='utf-8') as handle:
        expected = handle.read().splitlines()
    if actual == expected:
        return
    diff = '\n'.join(list(difflib.unified_diff(
        expected, actual, 'golden', 'current', lineterm=''))[:60])
    testcase.fail(
        'the %s changed:\n%s\n\nIf this is intended, regenerate with\n'
        "    HH_REGEN_GOLDEN=1 make test UT='test_rendered_corpus.py'"
        % (what, diff))


class TestRenderedCorpus(unittest.TestCase):

    def test_every_profile_renders_to_the_recorded_digest(self):
        lines = []
        for name in sorted(profiles.PROFILES):
            rendered = cfg.render(profiles.get(name))
            for path in sorted(rendered):
                digest = hashlib.sha256(rendered[path].encode('utf-8')).hexdigest()
                lines.append('%s\t%s\t%s' % (name, path, digest))
        _compare(self, lines, PROFILE_GOLDEN, 'profile render digests')

    def test_every_blobifier_wiring_renders_to_the_recorded_digest(self):
        lines = []
        for name in sorted(BLOBIFIER_MATRIX):
            text = '\n'.join(_blobifier_sections(name, BLOBIFIER_MATRIX[name]))
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            lines.append('%s\t%s' % (name, digest))
        _compare(self, lines, BLOBIFIER_GOLDEN, 'blobifier render digests')

    def test_reference_blobifier_renders_are_unchanged(self):
        lines = []
        for name in REFERENCE_CASES:
            lines.append('### %s' % name)
            lines.extend(_blobifier_sections(name, BLOBIFIER_MATRIX[name]))
        _compare(self, lines, BLOBIFIER_TEXT, 'reference blobifier renders')


if __name__ == '__main__':
    unittest.main()
