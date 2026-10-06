# The harness renders a multi-unit machine from its in-memory Kconfig parses; `make install`
# renders from the saved .mmu_config files, re-parsed in the build's env. They must agree.
# The case that needs watching: kconfiglib doesn't write an invisible bool that is n, so the
# build recomputes a sharing unit's buffer sensor flags in the wrong env - harmless only
# while the templates never read a sharer's flags.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import cfg, profiles


def _pair(name, owner, sharer):
    return profiles.Profile(name, units=[profiles.UnitProfile('unit0', syms=owner, index=0),
                                         profiles.UnitProfile('unit1', syms=sharer, index=1)])


BOXTURTLE = profiles.get('boxturtle').syms
TRADRACK = profiles.get('tradrack').syms
ENCODER = profiles.get('encoder').syms


def _buffer_sharer(syms, name):
    return dict(syms, MMU_HAS_SYNC_FEEDBACK_BUFFER=True, MMU_SHARED_SYNC_FEEDBACK_BUFFER=True,
                PARAM_SYNC_FEEDBACK_BUFFER_NAME=name)


BUFFER_SHARER = _buffer_sharer(TRADRACK, 'unit0')

CASES = [p for p in profiles.PROFILES.values() if p.units] + [
    profiles.SHARED_ENCODER,
    _pair('via_files_buffer_sharer', BOXTURTLE, BUFFER_SHARER),
    _pair('via_files_qidi_owner', profiles.get('qidi').syms, _buffer_sharer(BOXTURTLE, 'unit0')),
    profiles.Profile('via_files_sharer_first', units=[
        profiles.UnitProfile('unit0', syms=_buffer_sharer(TRADRACK, 'unit1'), index=0),
        profiles.UnitProfile('unit1', syms=BOXTURTLE, index=1)]),
    _pair('via_files_custom_owner_names',
          dict(ENCODER, PARAM_SYNC_FEEDBACK_BUFFER_NAME='box_buf', PARAM_ENCODER_NAME='box_enc'),
          dict(_buffer_sharer(TRADRACK, 'box_buf'), MMU_HAS_ENCODER=True,
               MMU_SHARED_ENCODER=True, PARAM_ENCODER_NAME='box_enc')),
    _pair('via_files_private_buffer', BOXTURTLE,
          dict(_buffer_sharer(TRADRACK, 'private_buf'), MMU_HAS_SENSOR_BUFFER_TENSION=True,
               CHOICE_BUFFER_SPRING_STATE_TENSION=True)),
]


class TestRenderViaFiles(unittest.TestCase):

    def test_the_build_renders_what_the_harness_renders(self):
        for profile in CASES:
            with self.subTest(profile=profile.name):
                harness = cfg.render(profile)
                built = cfg.render_via_files(profile)
                self.assertEqual(sorted(built), sorted(harness))
                for name in harness:
                    self.assertEqual(built[name], harness[name], name)


if __name__ == '__main__':
    unittest.main()
