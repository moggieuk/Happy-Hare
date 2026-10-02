# A unit of a multi-unit machine sharing another unit's sync-feedback buffer: the pick list
# of owners, the owner's sensors as the sharer's own, and how the saved choice survives a
# refresh. Value files are written and refreshed the way install.sh's unit passes do; the
# reader underneath (shared_components.py) is pinned by test_shared_components.py.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import contextlib
import io
import os
import tempfile
import unittest

from test.hh import cfg, profiles


def _sharing(syms, owner=None):
    shared = dict(syms, MMU_HAS_SYNC_FEEDBACK_BUFFER=True, MMU_SHARED_SYNC_FEEDBACK_BUFFER=True)
    if owner:
        shared['CHOICE_SHARED_BUFFER_' + owner.upper()] = True
    return shared


class _Install:
    """
    A multi-unit install's value files, saved and refreshed the way install.sh's unit parses
    do: each with KCONFIG_PARENT pointing at the top-level file, so a sharer reads its owner.
    """

    def __init__(self, tmp, names):
        self.parent = os.path.join(tmp, '.mmu_config')
        self.names = list(names)
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                           UNIT_NAME=','.join(names), MCU_NAME=','.join(names))):
            cfg._kconfig('install', {}).write_config(self.parent)

    def path(self, name):
        return '%s_%s' % (self.parent, name)

    def env(self, name):
        return dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', UNIT_NAME=name, MCU_NAME=name,
                    UNIT_INDEX=str(self.names.index(name)), KCONFIG_PARENT=self.parent,
                    KCONFIG_CONFIG=self.path(name))

    def parse(self, name, syms):
        with cfg._env(self.env(name)):
            return cfg._kconfig('%s:%s' % (self.parent, name), syms)

    def save(self, name, syms):
        kc = self.parse(name, syms)
        kc.write_config(self.path(name))
        return kc

    def refresh(self, name):
        """olddefconfig: reload the saved file, recomputing its #~DEFAULT~# values."""
        with cfg._env(self.env(name)):
            kc = cfg._new_kconfig('%s:%s' % (self.parent, name))
        kc.warn = False
        kc.load_config(self.path(name), filter_defaults=True)
        kc.write_config(self.path(name))
        return kc

    def stale(self, name):
        import shared_components
        return shared_components.stale(self.path(name), self.parent)


BUFFER_SENSORS = ('MMU_HAS_SENSOR_BUFFER_COMPRESSION', 'MMU_HAS_SENSOR_BUFFER_TENSION',
                  'MMU_HAS_SENSOR_BUFFER_PROPORTIONAL')

# One owner per distinct set of buffer sensors
BUFFER_OWNERS = {
    'boxturtle': profiles.get('boxturtle').syms,           # compression + tension
    'qidi': profiles.get('qidi').syms,                     # tension
    'emu': profiles.get('emu').syms,                       # proportional
}

# Sharers whose own machine type used to imply its sensors: one per set, plus types with none
BUFFER_SHARERS = dict(BUFFER_OWNERS, tradrack=profiles.get('tradrack').syms,
                      emu_without_psf=dict(profiles.get('emu').syms, OPTION_PSF_BUFFER=False))


def _flags(kc):
    for sym in kc.unique_defined_syms:
        sym.str_value
    return {s: kc.syms[s].str_value for s in BUFFER_SENSORS}


def _select_warnings(kc):
    _flags(kc)
    return [w.splitlines()[0] for w in kc.warnings if 'being y-selected' in w]


class TestSharedSyncFeedbackBuffer(unittest.TestCase):
    """
    A unit sharing another unit's buffer takes the OWNER's sensors (read from the owner's saved
    config) for its own endstop and calibration defaults, and picks the owner from a list of
    the units that have a buffer. Its own buffer sensor symbols are hidden, so no machine type
    may select them for a sharer (kconfiglib warns about a select overriding unmet
    dependencies).
    """

    def test_a_sharer_takes_its_owners_sensors_whatever_its_own_type(self):
        for owner, owner_syms in BUFFER_OWNERS.items():
            for sharer, sharer_syms in BUFFER_SHARERS.items():
                with self.subTest(owner=owner, sharer=sharer), \
                        tempfile.TemporaryDirectory() as tmp:
                    install = _Install(tmp, ('unit0', 'unit1'))
                    owner_kc = install.save('unit0', owner_syms)
                    kc = install.parse('unit1', _sharing(sharer_syms))
                    self.assertEqual(_flags(kc), _flags(owner_kc))
                    self.assertEqual(_select_warnings(kc), [])
                    self.assertEqual(kc.syms['PARAM_SYNC_FEEDBACK_BUFFER_NAME'].str_value, 'unit0')

    def test_autocal_and_compression_homing_follow_the_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])
            kc = install.parse('unit1', _sharing(BUFFER_SHARERS['tradrack']))
            self.assertEqual(kc.syms['PARAM_AUTOCAL_BOWDEN_LENGTH'].str_value, '1')
            self.assertEqual(kc.syms['CHOICE_EXTRUDER_HOMING_ENDSTOP_COMPRESSION'].visibility, 2)

            install.save('unit0', BUFFER_OWNERS['qidi'])          # tension only
            kc = install.parse('unit1', _sharing(BUFFER_SHARERS['boxturtle']))
            self.assertEqual(kc.syms['PARAM_AUTOCAL_BOWDEN_LENGTH'].str_value, '0')
            self.assertEqual(kc.syms['CHOICE_EXTRUDER_HOMING_ENDSTOP_COMPRESSION'].visibility, 0)

    def test_rendering_a_sharing_boxturtle_emits_no_select_warnings(self):
        encoder = profiles.get('encoder')
        profile = profiles.Profile('boxturtle_buffer_sharer', units=[
            profiles.UnitProfile('unit0', syms=encoder.syms, index=0),
            profiles.UnitProfile('unit1', syms=_sharing(encoder.syms), index=1),
        ])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rendered = cfg.render(profile)
        self.assertNotIn('being y-selected', stderr.getvalue())

        parser = cfg.assemble(rendered)
        self.assertEqual(dict(parser.items('mmu_unit unit1'))['buffer'], 'unit0')
        self.assertFalse(parser.has_section('mmu_buffer unit1'))
        self.assertEqual(
            dict(parser.items('mmu_unit_parameters unit1'))['autocal_bowden_length'], '1')

    def test_the_pick_list_offers_only_owners_and_units_not_configured_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1', 'unit2', 'unit3'))
            install.save('unit0', BUFFER_OWNERS['qidi'])
            install.save('unit1', profiles.get('tradrack').syms)              # no buffer
            install.save('unit2', _sharing(BUFFER_SHARERS['qidi'], owner='unit0'))
            kc = install.parse('unit3', _sharing(BUFFER_SHARERS['boxturtle']))
            choice = kc.named_choices['CHOICE_SHARED_BUFFER']
            offered = [(s.name, s.nodes[0].prompt[0]) for s in choice.syms if s.visibility]
            # unit3 is the unit being configured; unit2 shares rather than owns
            self.assertEqual(offered, [('CHOICE_SHARED_BUFFER_UNIT0',
                                        'unit0 (tension, spring: tension)')])

        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            kc = install.parse('unit0', _sharing(BUFFER_SHARERS['boxturtle']))
            choice = kc.named_choices['CHOICE_SHARED_BUFFER']
            self.assertEqual([s.nodes[0].prompt[0] for s in choice.syms if s.visibility],
                             ['unit1 (not configured yet)'])

    def test_an_owner_configured_after_its_sharer_is_picked_up_by_a_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', _sharing(BUFFER_SHARERS['tradrack']))
            install.save('unit1', BUFFER_OWNERS['boxturtle'])
            self.assertTrue(install.stale('unit0'))
            self.assertFalse(install.stale('unit1'))

            kc = install.refresh('unit0')
            self.assertEqual(kc.syms['PARAM_SYNC_FEEDBACK_BUFFER_NAME'].str_value, 'unit1')
            self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_COMPRESSION'], 'y')
            self.assertEqual(kc.syms['PARAM_AUTOCAL_BOWDEN_LENGTH'].str_value, '1')
            self.assertFalse(install.stale('unit0'))

    def test_units_that_share_nothing_are_never_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])
            install.save('unit1', BUFFER_OWNERS['qidi'])
            install.save('unit0', BUFFER_OWNERS['emu'])             # owners change freely
            self.assertFalse(install.stale('unit0'))
            self.assertFalse(install.stale('unit1'))

    def test_a_saved_owner_that_no_longer_owns_a_buffer_is_kept_and_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])
            install.save('unit1', _sharing(BUFFER_SHARERS['tradrack'], owner='unit0'))
            install.save('unit0', profiles.get('tradrack').syms)    # unit0 loses its buffer

            kc = install.refresh('unit1')
            self.assertEqual(kc.named_choices['CHOICE_SHARED_BUFFER'].selection.name,
                             'CHOICE_SHARED_BUFFER_UNRESOLVED')
            self.assertEqual(kc.syms['PARAM_SYNC_FEEDBACK_BUFFER_NAME'].str_value, 'unit0')
            self.assertEqual(kc.syms['MMU_SHARED_SYNC_FEEDBACK_BUFFER'].str_value, 'y')
            self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_COMPRESSION'], 'y')  # last known
            self.assertTrue(kc.is_enabled('W29'))

    def test_sharing_with_no_owner_at_all_is_kept_and_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', profiles.get('tradrack').syms)
            install.save('unit1', _sharing(BUFFER_SHARERS['tradrack']))
            kc = install.refresh('unit1')
            self.assertEqual(kc.syms['MMU_SHARED_SYNC_FEEDBACK_BUFFER'].str_value, 'y')
            self.assertTrue(kc.is_enabled('W29'))

    def test_the_spring_state_is_the_owners_and_steers_extruder_homing(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])            # Turtle Neck v2: tension
            kc = install.parse('unit1', _sharing(BUFFER_SHARERS['tradrack']))
            self.assertEqual(kc.syms['PARAM_BUFFER_SPRING_STATE'].str_value, 'tension')
            self.assertEqual(kc.syms['PARAM_BUFFER_SPRING_STATE'].visibility, 0)  # not settable
            self.assertEqual(kc.named_choices['CHOICE_EXTRUDER_HOMING_ENDSTOP'].selection.name,
                             'CHOICE_EXTRUDER_HOMING_ENDSTOP_COMPRESSION')

            install.save('unit0', dict(BUFFER_OWNERS['boxturtle'],
                                       CHOICE_BUFFER_SPRING_STATE_NONE=True))
            kc = install.parse('unit1', _sharing(BUFFER_SHARERS['tradrack']))
            self.assertEqual(kc.syms['PARAM_BUFFER_SPRING_STATE'].str_value, 'none')
            self.assertNotEqual(kc.named_choices['CHOICE_EXTRUDER_HOMING_ENDSTOP'].selection.name,
                                'CHOICE_EXTRUDER_HOMING_ENDSTOP_COMPRESSION')

    def test_a_sharer_is_stale_when_its_owners_spring_state_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])
            install.save('unit1', _sharing(BUFFER_SHARERS['tradrack']))
            self.assertFalse(install.stale('unit1'))
            install.save('unit0', dict(BUFFER_OWNERS['boxturtle'],
                                       CHOICE_BUFFER_SPRING_STATE_NEUTRAL=True))
            self.assertTrue(install.stale('unit1'))
            self.assertEqual(install.refresh('unit1').syms['PARAM_BUFFER_SPRING_STATE'].str_value,
                             'neutral')
            self.assertFalse(install.stale('unit1'))

    def test_owners_and_resolved_sharers_are_not_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            self.assertFalse(install.save('unit0', BUFFER_OWNERS['boxturtle']).is_enabled('W29'))
            self.assertFalse(install.save('unit1', _sharing(BUFFER_SHARERS['qidi'])).is_enabled('W29'))

    def test_sharer_keeps_an_explicit_compression_extruder_endstop(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])
            install.save('unit1', dict(_sharing(BUFFER_SHARERS['boxturtle']),
                                       CHOICE_EXTRUDER_HOMING_ENDSTOP_COMPRESSION=True))
            kc = install.refresh('unit1')
        choice = kc.named_choices['CHOICE_EXTRUDER_HOMING_ENDSTOP']
        self.assertEqual(choice.selection.name, 'CHOICE_EXTRUDER_HOMING_ENDSTOP_COMPRESSION')

    def test_a_sharer_saved_by_an_older_install_keeps_its_owner(self):
        """Before the pick list the name was typed, so it was saved as an explicit value."""
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1', 'unit2'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])
            install.save('unit1', BUFFER_OWNERS['qidi'])
            with open(install.path('unit2'), 'w') as f:
                f.write('CONFIG_MMU_TYPE_BOX_TURTLE_1_0=y\n'
                        'CONFIG_MMU_SHARED_SYNC_FEEDBACK_BUFFER=y\n'
                        'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="unit1"\n')
            kc = install.refresh('unit2')
            self.assertEqual(kc.named_choices['CHOICE_SHARED_BUFFER'].selection.name,
                             'CHOICE_SHARED_BUFFER_UNIT1')
            self.assertEqual(kc.syms['PARAM_SYNC_FEEDBACK_BUFFER_NAME'].str_value, 'unit1')
            self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_COMPRESSION'], 'n')


if __name__ == "__main__":
    unittest.main()
