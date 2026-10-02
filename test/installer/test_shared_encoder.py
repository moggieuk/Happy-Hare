# A unit of a multi-unit machine sharing another unit's encoder: the pick list of owners and
# how the saved choice survives a refresh. Unlike the buffer there is nothing to take from
# the owner but its name. Value files are written and refreshed the way install.sh's unit
# passes do (see test_shared_buffer.py for the helper).
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import tempfile
import unittest

from test.hh import profiles
from test.installer.test_shared_buffer import _Install

ENCODER_OWNER = profiles.get('encoder').syms                   # BoxTurtle + encoder
TRADRACK_OWNER = dict(profiles.get('tradrack').syms, MMU_HAS_ENCODER=True)
NO_ENCODER = profiles.get('boxturtle').syms


def _sharing(syms, owner=None):
    shared = dict(syms, MMU_HAS_ENCODER=True, MMU_SHARED_ENCODER=True)
    if owner:
        shared['CHOICE_SHARED_ENCODER_' + owner.upper()] = True
    return shared


def _offered(kc):
    choice = kc.named_choices['CHOICE_SHARED_ENCODER']
    return [(s.name, s.nodes[0].prompt[0]) for s in choice.syms if s.visibility]


class TestSharedEncoder(unittest.TestCase):

    def test_the_pick_list_offers_only_units_owning_an_encoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1', 'unit2', 'unit3'))
            install.save('unit0', TRADRACK_OWNER)
            install.save('unit1', NO_ENCODER)
            install.save('unit2', _sharing(NO_ENCODER, owner='unit0'))
            kc = install.parse('unit3', _sharing(ENCODER_OWNER))
            self.assertEqual(_offered(kc), [('CHOICE_SHARED_ENCODER_UNIT0', 'unit0')])
            self.assertEqual(kc.syms['PARAM_ENCODER_NAME'].str_value, 'unit0')
            self.assertFalse(kc.is_enabled('W30'))

    def test_a_unit_not_configured_yet_can_be_picked(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            kc = install.save('unit0', _sharing(NO_ENCODER))
            self.assertEqual(_offered(kc), [('CHOICE_SHARED_ENCODER_UNIT1',
                                             'unit1 (not configured yet)')])
            self.assertEqual(kc.syms['PARAM_ENCODER_NAME'].str_value, 'unit1')

            install.save('unit1', ENCODER_OWNER)
            # Nothing to take from an encoder's owner but its name, so nothing goes stale
            self.assertFalse(install.stale('unit0'))
            self.assertEqual(install.refresh('unit0').syms['PARAM_ENCODER_NAME'].str_value,
                             'unit1')

    def test_a_saved_owner_that_no_longer_has_an_encoder_is_kept_and_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', ENCODER_OWNER)
            install.save('unit1', _sharing(NO_ENCODER, owner='unit0'))
            install.save('unit0', NO_ENCODER)

            kc = install.refresh('unit1')
            self.assertEqual(kc.named_choices['CHOICE_SHARED_ENCODER'].selection.name,
                             'CHOICE_SHARED_ENCODER_UNRESOLVED')
            self.assertEqual(kc.syms['PARAM_ENCODER_NAME'].str_value, 'unit0')
            self.assertEqual(kc.syms['MMU_SHARED_ENCODER'].str_value, 'y')
            self.assertTrue(kc.is_enabled('W30'))

    def test_a_saved_share_with_no_owner_at_all_is_kept_and_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', NO_ENCODER)
            with open(install.path('unit1'), 'w') as f:
                f.write('CONFIG_MMU_TYPE_TRADRACK_1_0=y\n'
                        'CONFIG_MMU_HAS_ENCODER=y\n'
                        'CONFIG_MMU_SHARED_ENCODER=y\n')
            kc = install.refresh('unit1')
            self.assertEqual(kc.syms['MMU_SHARED_ENCODER'].str_value, 'y')
            self.assertTrue(kc.is_enabled('W30'))

    def test_sharing_is_only_offered_when_there_is_a_unit_to_share_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', NO_ENCODER)
            kc = install.parse('unit1', ENCODER_OWNER)
            self.assertEqual(kc.syms['MMU_SHARED_ENCODER'].visibility, 0)
            install.save('unit0', TRADRACK_OWNER)
            kc = install.parse('unit1', ENCODER_OWNER)
            self.assertEqual(kc.syms['MMU_SHARED_ENCODER'].visibility, 2)

    def test_an_owner_is_not_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            self.assertFalse(install.save('unit0', ENCODER_OWNER).is_enabled('W30'))

    def test_a_sharer_saved_by_an_older_install_keeps_its_owner(self):
        """Before the pick list the name was typed, so it was saved as an explicit value."""
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1', 'unit2'))
            install.save('unit0', ENCODER_OWNER)
            install.save('unit1', TRADRACK_OWNER)
            with open(install.path('unit2'), 'w') as f:
                f.write('CONFIG_MMU_TYPE_TRADRACK_1_0=y\n'
                        'CONFIG_MMU_HAS_ENCODER=y\n'
                        'CONFIG_MMU_SHARED_ENCODER=y\n'
                        'CONFIG_PARAM_ENCODER_NAME="unit1"\n')
            kc = install.refresh('unit2')
            self.assertEqual(kc.named_choices['CHOICE_SHARED_ENCODER'].selection.name,
                             'CHOICE_SHARED_ENCODER_UNIT1')
            self.assertEqual(kc.syms['PARAM_ENCODER_NAME'].str_value, 'unit1')


if __name__ == "__main__":
    unittest.main()
