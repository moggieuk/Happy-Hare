# A unit sharing an encoder it doesn't define: another unit's, or one in the user's own
# config, named by PARAM_ENCODER_NAME. Unlike the buffer there is nothing to take from an
# owner but its name. Value files are written and refreshed the way install.sh's unit passes
# do (see test_shared_buffer.py for the helper).
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import tempfile
import unittest

from test.hh import cfg, profiles
from test.installer.test_shared_buffer import _Install, _menu_prompts

ENCODER_OWNER = profiles.get('encoder').syms                   # BoxTurtle + encoder
TRADRACK_OWNER = dict(profiles.get('tradrack').syms, MMU_HAS_ENCODER=True)
NO_ENCODER = profiles.get('boxturtle').syms


def _sharing(syms, name=None):
    shared = dict(syms, MMU_HAS_ENCODER=True, MMU_SHARED_ENCODER=True)
    if name is not None:
        shared['PARAM_ENCODER_NAME'] = name
    return shared


def _single(syms):
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        return cfg._kconfig('shared_encoder', syms)


class TestSharedEncoderMenu(unittest.TestCase):

    def test_the_shared_question_comes_first_then_the_name(self):
        self.assertEqual(_menu_prompts(_single(ENCODER_OWNER), 'Encoder config')[:2],
                         ['Use shared encoder?', 'Encoder name'])
        self.assertEqual(_menu_prompts(_single(_sharing(ENCODER_OWNER)), 'Encoder config')[:2],
                         ['Use shared encoder?', 'Shared encoder object name'])

    def test_an_owners_name_defaults_to_its_unit_and_a_sharers_to_blank(self):
        self.assertEqual(_single(ENCODER_OWNER).syms['PARAM_ENCODER_NAME'].str_value, 'unit0')
        kc = _single(_sharing(ENCODER_OWNER))
        self.assertEqual(kc.syms['PARAM_ENCODER_NAME'].str_value, '')
        self.assertTrue(kc.is_enabled('W30'))

    def test_a_sharer_hides_the_encoders_own_settings(self):
        kc = _single(_sharing(ENCODER_OWNER, 'xyz'))
        prompts = _menu_prompts(kc, 'Encoder config')
        self.assertNotIn('Type', prompts)
        self.assertIn('Gate endstop to encoder distance', prompts)
        self.assertFalse(kc.is_enabled('W30'))

    def test_the_name_validator_takes_an_object_name_or_blank(self):
        validator = _single(ENCODER_OWNER).syms['PARAM_ENCODER_NAME'].validator
        for good in ('xyz', 'box_enc', 'a-1', ''):
            self.assertTrue(validator.fullmatch(good), good)
        for bad in ('XYZ', '1abc', 'a b', 'a:b'):
            self.assertFalse(validator.fullmatch(bad), bad)


class TestSharedEncoderRender(unittest.TestCase):

    def test_a_single_unit_can_share_an_encoder_of_its_own_config(self):
        parser = cfg.assemble(cfg.render(profiles.ENCODER.derive(
            'encoder_private_share', syms=_sharing({}, 'xyz'))))
        self.assertEqual(dict(parser.items('mmu_unit unit0'))['encoder'], 'xyz')
        self.assertFalse([s for s in parser.sections() if s.startswith('mmu_encoder')])

    def test_an_owners_section_is_named_from_its_encoder_name(self):
        parser = cfg.assemble(cfg.render(profiles.ENCODER.derive(
            'encoder_named', syms=dict(PARAM_ENCODER_NAME='box_enc'))))
        self.assertEqual(dict(parser.items('mmu_unit unit0'))['encoder'], 'box_enc')
        self.assertTrue(parser.has_section('mmu_encoder box_enc'))
        self.assertFalse(parser.has_section('mmu_encoder unit0'))

    def test_a_sharer_names_an_owners_custom_name(self):
        profile = profiles.Profile('encoder_custom_owner', units=[
            profiles.UnitProfile('unit0', index=0, syms=dict(
                ENCODER_OWNER, PARAM_ENCODER_NAME='box_enc')),
            profiles.UnitProfile('unit1', index=1, syms=_sharing(NO_ENCODER, 'box_enc')),
        ])
        parser = cfg.assemble(cfg.render(profile))
        self.assertEqual(dict(parser.items('mmu_unit unit1'))['encoder'], 'box_enc')
        self.assertEqual([s for s in parser.sections() if s.startswith('mmu_encoder')],
                         ['mmu_encoder box_enc'])


class TestSharedEncoderSaved(unittest.TestCase):

    def test_a_sharers_name_saved_as_a_default_survives_a_refresh(self):
        """A pick list saved the owner's name as a #~DEFAULT~# value."""
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', ENCODER_OWNER)
            with open(install.path('unit1'), 'w') as f:
                f.write('CONFIG_MMU_TYPE_TRADRACK_1_0=y\n'
                        'CONFIG_MMU_HAS_ENCODER=y\n'
                        'CONFIG_MMU_SHARED_ENCODER=y\n'
                        'CONFIG_PARAM_ENCODER_NAME="unit0" #~DEFAULT~#\n')
            kc = install.refresh('unit1')
            self.assertEqual(kc.syms['PARAM_ENCODER_NAME'].str_value, 'unit0')
            self.assertFalse(kc.is_enabled('W30'))

    def test_an_older_placeholder_name_is_blanked_and_warned(self):
        """Older installs started a sharer's name from '-specify name-'."""
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            with open(install.path('unit1'), 'w') as f:
                f.write('CONFIG_MMU_TYPE_TRADRACK_1_0=y\n'
                        'CONFIG_MMU_HAS_ENCODER=y\n'
                        'CONFIG_MMU_SHARED_ENCODER=y\n'
                        'CONFIG_PARAM_ENCODER_NAME="-specify name-" #~DEFAULT~#\n')
            kc = install.refresh('unit1')
            self.assertEqual(kc.syms['PARAM_ENCODER_NAME'].str_value, '')
            self.assertTrue(kc.is_enabled('W30'))

    def test_turning_sharing_on_starts_from_a_blank_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit1', ENCODER_OWNER)
            with cfg._env(install.env('unit1')):
                kc = cfg._new_kconfig('toggle')
                kc.warn = False
                kc.load_config(install.path('unit1'), filter_defaults=True)
                kc.syms['MMU_SHARED_ENCODER'].set_value(2)
            self.assertEqual(kc.syms['PARAM_ENCODER_NAME'].str_value, '')

    def test_an_older_sharers_typed_name_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            with open(install.path('unit1'), 'w') as f:
                f.write('CONFIG_MMU_TYPE_TRADRACK_1_0=y\n'
                        'CONFIG_MMU_HAS_ENCODER=y\n'
                        'CONFIG_MMU_SHARED_ENCODER=y\n'
                        'CONFIG_PARAM_ENCODER_NAME="unit0"\n')
            self.assertEqual(install.refresh('unit1').syms['PARAM_ENCODER_NAME'].str_value,
                             'unit0')

    def test_nothing_goes_stale_for_an_encoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', _sharing(NO_ENCODER, 'unit1'))
            install.save('unit1', ENCODER_OWNER)
            self.assertFalse(install.stale('unit0'))


class TestOwnerNameWarning(unittest.TestCase):

    def test_two_owners_of_one_name_are_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', dict(ENCODER_OWNER, PARAM_ENCODER_NAME='box_enc'))
            self.assertTrue(install.parse('unit1', dict(
                TRADRACK_OWNER, PARAM_ENCODER_NAME='box_enc')).is_enabled('W34'))
            self.assertFalse(install.parse('unit1', TRADRACK_OWNER).is_enabled('W34'))
            # A sharer naming it is the point
            self.assertFalse(install.parse('unit1', _sharing(
                TRADRACK_OWNER, 'box_enc')).is_enabled('W34'))

    def test_a_blank_owner_name_is_warned(self):
        self.assertTrue(_single(dict(ENCODER_OWNER, PARAM_ENCODER_NAME='')).is_enabled('W34'))
        self.assertFalse(_single(ENCODER_OWNER).is_enabled('W34'))


if __name__ == "__main__":
    unittest.main()
