# A CHOICE_* selection saved with #~DEFAULT~# must stay a modifiable default
# on reload, so its conditional defaults keep tracking the symbols they
# reference. CHOICE_PURGE_MACRO is the worked example: it defaults to
# Blobifier only when MMU_HAS_BLOBIFIER is enabled.

import os
import tempfile
import unittest

from test.hh import cfg


class TestDefaultChoiceStaysDynamic(unittest.TestCase):

    def _roundtrip(self, body):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._new_kconfig('default_choice')
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, '.mmu_config')
                with open(path, 'w') as handle:
                    handle.write(body)
                kc.load_config(path, filter_defaults=True)
                kc.syms['MMU_HAS_BLOBIFIER'].set_value('y')
                kc.write_config(path)
                with open(path) as handle:
                    written = handle.read()
        choice = kc.syms['CHOICE_PURGE_MACRO_PURGE'].choice
        return choice.selection.name, kc.get('PARAM_PURGE_MACRO'), written

    def test_saved_default_follows_blobifier(self):
        selection, macro, written = self._roundtrip(
            '# CONFIG_MMU_HAS_BLOBIFIER is not set #~DEFAULT~#\n'
            'CONFIG_CHOICE_PURGE_MACRO_PURGE=y #~DEFAULT~#\n'
            'CONFIG_PARAM_PURGE_MACRO="_MMU_PURGE" #~DEFAULT~#\n')
        self.assertEqual(selection, 'CHOICE_PURGE_MACRO_BLOBIFIER')
        self.assertEqual(macro, 'BLOBIFIER')
        self.assertIn('CONFIG_CHOICE_PURGE_MACRO_BLOBIFIER=y #~DEFAULT~#', written)

    def test_explicit_selection_is_kept(self):
        selection, macro, written = self._roundtrip(
            '# CONFIG_MMU_HAS_BLOBIFIER is not set #~DEFAULT~#\n'
            'CONFIG_CHOICE_PURGE_MACRO_PURGE=y\n'
            '# CONFIG_CHOICE_PURGE_MACRO_SLICER is not set #~DEFAULT~#\n')
        self.assertEqual(selection, 'CHOICE_PURGE_MACRO_PURGE')
        self.assertEqual(macro, '_MMU_PURGE')
        self.assertIn('CONFIG_CHOICE_PURGE_MACRO_PURGE=y\n', written)
