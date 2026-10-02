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
BUFFER_SHARER = dict(TRADRACK, MMU_HAS_SYNC_FEEDBACK_BUFFER=True,
                     MMU_SHARED_SYNC_FEEDBACK_BUFFER=True)

CASES = [p for p in profiles.PROFILES.values() if p.units] + [
    profiles.SHARED_ENCODER,
    _pair('via_files_buffer_sharer', BOXTURTLE, BUFFER_SHARER),
    _pair('via_files_qidi_owner', profiles.get('qidi').syms, dict(BOXTURTLE,
                                                                  MMU_SHARED_SYNC_FEEDBACK_BUFFER=True)),
    profiles.Profile('via_files_sharer_first', units=[
        profiles.UnitProfile('unit0', syms=BUFFER_SHARER, index=0),
        profiles.UnitProfile('unit1', syms=BOXTURTLE, index=1)]),
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
