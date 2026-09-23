# Gate count against the count the machine was designed for.
#
# Six machine types have a gate count fixed by the design. They used to
# `select DISABLE_NUM_GATES_OPTION` to hide the prompt, but that symbol was
# never declared anywhere, so the select silently did nothing and the prompt
# always showed.
#
# Declaring it would have worked, and would also have lost data: hiding a
# prompt makes the symbol invisible, kconfiglib discards a user value for an
# invisible symbol, and the next olddefconfig would have written the design
# count over whatever the user had set. So the count stays editable and W26
# says when it disagrees with the design instead.

import os
import re
import tempfile
import unittest

from test.hh import cfg

# Types whose design fixes the gate count, and what it is. Deliberately
# written out rather than scraped: the point of the test below is to catch
# these drifting apart from the Kconfig files, which a scrape could not do.
FIXED_COUNT_TYPES = {
    'MMU_TYPE_BOX_TURTLE_1_0': 4,
    'MMU_TYPE_KMS_1_0': 4,
    'MMU_TYPE_MMX6_1_0': 6,
    'MMU_TYPE_VVD_1_0': 4,
    'MMU_TYPE_QIDI_BOX_1_0': 4,
    'MMU_TYPE_HTLF_1_0': 4,
}


class TestDesignGateCount(unittest.TestCase):
    """One parsed tree, reset per case - a parse each would cost ~80s."""

    @classmethod
    def setUpClass(cls):
        cls._env_ctx = cfg._env(cfg._SINGLE_UNIT_ENV)
        cls._env_ctx.__enter__()
        cls.addClassCleanup(cls._env_ctx.__exit__, None, None, None)
        cls.tree = cfg._new_kconfig('num_gates')
        cls.machine_types = sorted(
            name for name in cls.tree.syms
            if re.match(r'^MMU_TYPE_[A-Z0-9_]+$', name) and cls.tree.syms[name].nodes)

    def _configure(self, machine_type, gates=None):
        syms = {machine_type: True}
        if gates is not None:
            syms['PARAM_NUM_GATES'] = gates
        return cfg._apply_syms(self.tree, 'num_gates', syms, reset=True)

    def test_no_machine_type_warns_about_its_own_defaults(self):
        """The drift guard.

        PARAM_DESIGN_NUM_GATES sits beside PARAM_NUM_GATES's default in each
        machine-type file, so the number appears twice and could diverge.
        Rather than compare the two literals, assert the consequence: a
        freshly selected machine must never warn about itself. That catches a
        divergence whichever of the two moved.
        """
        for machine_type in self.machine_types:
            with self.subTest(machine=machine_type):
                kconfig = self._configure(machine_type)
                self.assertFalse(
                    kconfig.is_enabled('W26'),
                    '%s warns on its own defaults: gate count %s but the design '
                    'says %s' % (machine_type, kconfig.get('PARAM_NUM_GATES'),
                                 kconfig.get('PARAM_DESIGN_NUM_GATES')))

    def test_a_fixed_count_machine_warns_when_the_count_is_changed(self):
        for machine_type, design in sorted(FIXED_COUNT_TYPES.items()):
            with self.subTest(machine=machine_type):
                kconfig = self._configure(machine_type)
                self.assertEqual(kconfig.get('PARAM_DESIGN_NUM_GATES'), str(design))
                self.assertTrue(
                    self._configure(machine_type, design + 2).is_enabled('W26'))

    def test_a_variable_count_machine_never_warns(self):
        """Design count 0 means the design permits any count."""
        for machine_type in self.machine_types:
            if machine_type in FIXED_COUNT_TYPES:
                continue
            with self.subTest(machine=machine_type):
                kconfig = self._configure(machine_type)
                self.assertEqual(kconfig.get('PARAM_DESIGN_NUM_GATES'), '0')
                self.assertFalse(self._configure(machine_type, 7).is_enabled('W26'))

    def test_the_gate_count_stays_editable_everywhere(self):
        """The whole reason this is a warning and not a hidden prompt.

        An invisible symbol loses its user value on the next write_config, so
        hiding the prompt would silently reset anyone running a non-standard
        lane count on their next upgrade.
        """
        for machine_type in self.machine_types:
            with self.subTest(machine=machine_type):
                kconfig = self._configure(machine_type)
                self.assertTrue(kconfig.syms['PARAM_NUM_GATES'].visibility)

    def test_a_changed_count_survives_a_save(self):
        """The property the hidden-prompt approach would have broken."""
        kconfig = cfg._new_kconfig('gates_roundtrip')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '.mmu_config')
            with open(path, 'w') as handle:
                handle.write('CONFIG_MMU_TYPE_BOX_TURTLE_1_0=y\n'
                             'CONFIG_PARAM_NUM_GATES=8\n')
            kconfig.load_config(path, filter_defaults=True)
            self.assertEqual(kconfig.get('PARAM_NUM_GATES'), '8')
            self.assertTrue(kconfig.is_enabled('W26'))
            kconfig.write_config(path, save_old=False)
            with open(path) as handle:
                saved = handle.read()
        self.assertIn('CONFIG_PARAM_NUM_GATES=8', saved,
                      'the count was rewritten on save - the symbol has become '
                      'invisible somewhere')


if __name__ == '__main__':
    unittest.main()
