# A unit sharing a sync-feedback buffer it doesn't define, named by
# PARAM_SYNC_FEEDBACK_BUFFER_NAME: another unit's own buffer, whose sensors it takes, or one in
# the user's own config, whose sensors it declares. Value files are written and refreshed the
# way install.sh's unit passes do; the reader underneath (shared_components.py) is pinned by
# test_shared_components.py.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from test.hh import cfg, profiles


def _sharing(syms, name='unit0'):
    shared = dict(syms, MMU_HAS_SYNC_FEEDBACK_BUFFER=True, MMU_SHARED_SYNC_FEEDBACK_BUFFER=True)
    if name is not None:
        shared['PARAM_SYNC_FEEDBACK_BUFFER_NAME'] = name
    return shared


def _menu_nodes(kc, title, comments=False):
    """The nodes menuconfig shows directly in menu 'title', in menu order."""
    import kconfiglib
    import menuconfig
    menu = next(n for n in kc.node_iter() if n.item == kconfiglib.MENU and n.prompt[0] == title)
    nodes, node = [], menu.list
    while node:
        if menuconfig._visible(node) and (comments or node.item != kconfiglib.COMMENT):
            nodes.append(node)
        node = node.next
    return nodes


def _menu_prompts(kc, title):
    """The prompts menuconfig shows directly in menu 'title', in menu order (no comments)."""
    return [n.prompt[0].strip() for n in _menu_nodes(kc, title)]


def _menu_comments(kc, title):
    import kconfiglib
    return [n.prompt[0] for n in _menu_nodes(kc, title, comments=True)
            if n.item == kconfiglib.COMMENT]


class _Install:
    """
    A multi-unit install's value files, saved and refreshed the way install.sh's unit parses
    do: each with KCONFIG_PARENT pointing at the top-level file, so a sharer reads its owner.
    """

    def __init__(self, tmp, names, syms=None):
        self.parent = os.path.join(tmp, '.mmu_config')
        self.names = list(names)
        self.entry_env = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                              UNIT_NAME=','.join(names), MCU_NAME=','.join(names),
                              KCONFIG_CONFIG=self.parent)
        with cfg._env(self.entry_env):
            self.entry = cfg._kconfig('install', syms or {})
        self.entry.write_config(self.parent)

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


def _visible(kc, sym):
    return kc.syms[sym].visibility == 2


class TestSharedSyncFeedbackBuffer(unittest.TestCase):
    """
    A unit sharing another unit's own buffer takes that OWNER's sensors (read from the owner's
    saved config, matched by name) for its own endstop and calibration defaults. Its own buffer
    sensor symbols are then fixed, so no machine type may select them for a sharer (kconfiglib
    warns about a select overriding unmet dependencies).
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
                    self.assertEqual(kc.syms['SHARED_BUFFER_FOUND'].str_value, 'y')
                    for sym in BUFFER_SENSORS:
                        self.assertFalse(_visible(kc, sym), sym)

    def test_an_owner_is_found_by_its_buffer_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1', 'unit2'))
            install.save('unit0', BUFFER_OWNERS['qidi'])
            install.save('unit1', dict(BUFFER_OWNERS['emu'],
                                       PARAM_SYNC_FEEDBACK_BUFFER_NAME='box_buf'))
            kc = install.parse('unit2', _sharing(BUFFER_SHARERS['tradrack'], 'box_buf'))
            self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_PROPORTIONAL'], 'y')
            self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_TENSION'], 'n')
            # unit1's buffer isn't called unit1 any more
            kc = install.parse('unit2', _sharing(BUFFER_SHARERS['tradrack'], 'unit1'))
            self.assertEqual(kc.syms['SHARED_BUFFER_FOUND'].str_value, 'n')

    def test_a_name_changed_in_menuconfig_is_followed_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1', 'unit2'))
            install.save('unit0', BUFFER_OWNERS['qidi'])
            install.save('unit1', BUFFER_OWNERS['emu'])
            kc = install.parse('unit2', _sharing(BUFFER_SHARERS['tradrack'], 'unit0'))
            self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_TENSION'], 'y')
            kc.syms['PARAM_SYNC_FEEDBACK_BUFFER_NAME'].set_value('unit1')
            self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_TENSION'], 'n')
            self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_PROPORTIONAL'], 'y')
            kc.syms['PARAM_SYNC_FEEDBACK_BUFFER_NAME'].set_value('private_buf')
            self.assertEqual(kc.syms['SHARED_BUFFER_FOUND'].str_value, 'n')
            self.assertTrue(_visible(kc, 'MMU_HAS_SENSOR_BUFFER_TENSION'))

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

    def test_an_owners_section_is_named_from_its_buffer_name(self):
        parser = cfg.assemble(cfg.render(profiles.get('boxturtle').derive(
            'boxturtle_named_buffer', syms=dict(PARAM_SYNC_FEEDBACK_BUFFER_NAME='box_buf'))))
        self.assertEqual(dict(parser.items('mmu_unit unit0'))['buffer'], 'box_buf')
        self.assertTrue(parser.has_section('mmu_buffer box_buf'))

    def test_an_owner_configured_after_its_sharer_is_picked_up_by_a_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', _sharing(BUFFER_SHARERS['tradrack'], 'unit1'))
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

    def test_the_spring_state_is_the_owners_and_steers_extruder_homing(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])            # Turtle Neck v2: tension
            kc = install.parse('unit1', _sharing(BUFFER_SHARERS['tradrack']))
            self.assertEqual(kc.syms['PARAM_BUFFER_SPRING_STATE'].str_value, 'tension')
            self.assertEqual(kc.named_choices['CHOICE_BUFFER_SPRING_STATE'].visibility, 0)
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
        """A pick list saved the owner's name as a #~DEFAULT~# value; before it, typed."""
        for token in (' #~DEFAULT~#', ''):
            with self.subTest(token=token), tempfile.TemporaryDirectory() as tmp:
                install = _Install(tmp, ('unit0', 'unit1', 'unit2'))
                install.save('unit0', BUFFER_OWNERS['boxturtle'])
                install.save('unit1', BUFFER_OWNERS['qidi'])
                with open(install.path('unit2'), 'w') as f:
                    f.write('CONFIG_MMU_TYPE_BOX_TURTLE_1_0=y\n'
                            'CONFIG_MMU_SHARED_SYNC_FEEDBACK_BUFFER=y\n'
                            'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="unit1"%s\n' % token)
                kc = install.refresh('unit2')
                self.assertEqual(kc.syms['PARAM_SYNC_FEEDBACK_BUFFER_NAME'].str_value, 'unit1')
                self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_COMPRESSION'], 'n')
                self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_TENSION'], 'y')


class TestKnownBufferSummary(unittest.TestCase):
    """
    Another unit's buffer is summarized in dim comment lines, as its owner configured it, in
    place of the sensor and spring state rows a unit declaring its buffer gets.
    """

    SENSOR_ROWS = ('Sync-Feedback Compression sensor (aka buffer out)?',
                   'Sync-Feedback Tension sensor (aka buffer in)?',
                   'Sync-Feedback Analog proportional position sensor?',
                   'Buffer resting spring state')

    def parse(self, owner, syms):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', owner)
            return install.parse('unit1', syms)

    def summary_nodes(self, kc):
        import kconfiglib
        return [n for n in _menu_nodes(kc, 'Buffer config', comments=True)
                if n.item == kconfiglib.COMMENT and getattr(n, 'dim', False)
                and ':' in n.prompt[0] and not n.prompt[0].startswith(('Known shared', 'No other'))]

    def summary(self, kc):
        return [n.prompt[0].strip() for n in self.summary_nodes(kc)]

    def test_the_summary_is_one_aligned_block_without_the_sensors_heading(self):
        kc = self.parse(BUFFER_OWNERS['boxturtle'], _sharing(BUFFER_SHARERS['tradrack']))
        rows = _menu_nodes(kc, 'Buffer config', comments=True)
        block = self.summary_nodes(kc)
        first = rows.index(block[0])
        self.assertEqual(rows[first:first + 4], block)
        self.assertEqual(len({len(n.prompt[0]) for n in block}), 1)     # closing *** line up
        self.assertNotIn('_Fitted Sensors', [n.prompt[0] for n in rows])
        unknown = self.parse(BUFFER_OWNERS['boxturtle'],
                             _sharing(BUFFER_SHARERS['tradrack'], 'private_buf'))
        self.assertIn('_Fitted Sensors', [n.prompt[0] for n in _menu_nodes(unknown, 'Buffer config', comments=True)])

    def test_a_known_buffer_is_summarized_as_its_owner_configured_it(self):
        for owner, expected in (
                ('boxturtle', ['Resting spring state: Tension / squeezed buffer',
                               'Compression sensor: yes', 'Tension sensor: yes',
                               'Proportional sensor: no']),
                ('emu', ['Resting spring state: Tension / squeezed buffer',
                         'Compression sensor: no', 'Tension sensor: no',
                         'Proportional sensor: yes'])):
            with self.subTest(owner=owner):
                kc = self.parse(BUFFER_OWNERS[owner], _sharing(BUFFER_SHARERS['tradrack']))
                self.assertEqual(self.summary(kc), expected)
                prompts = _menu_prompts(kc, 'Buffer config')
                for row in self.SENSOR_ROWS:
                    self.assertNotIn(row, prompts)

        neutral = self.parse(dict(BUFFER_OWNERS['boxturtle'], CHOICE_BUFFER_SPRING_STATE_NEUTRAL=True),
                             _sharing(BUFFER_SHARERS['tradrack']))
        self.assertEqual(self.summary(neutral)[0], 'Resting spring state: Neutral')

    def test_an_owner_or_an_unknown_buffer_has_rows_not_a_summary(self):
        for label, syms in (('owner', BUFFER_OWNERS['qidi']),
                            ('unknown', _sharing(BUFFER_SHARERS['tradrack'], 'private_buf'))):
            with self.subTest(label):
                kc = self.parse(BUFFER_OWNERS['boxturtle'], syms)
                self.assertEqual(self.summary(kc), [])
                prompts = _menu_prompts(kc, 'Buffer config')
                for row in self.SENSOR_ROWS:
                    self.assertIn(row, prompts)


class TestKnownNames(unittest.TestCase):
    """A sharer is shown the names of the other units' own buffers and encoders."""

    def names(self, kc, menu):
        return [c for c in _menu_comments(kc, menu) if c.startswith('Known shared')]

    def test_the_other_units_names_are_listed_under_the_shared_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1', 'unit2', 'unit3'))
            install.save('unit0', dict(profiles.get('encoder').syms,
                                       PARAM_SYNC_FEEDBACK_BUFFER_NAME='box_buf'))
            install.save('unit1', _sharing(BUFFER_SHARERS['tradrack'], 'box_buf'))  # a sharer
            install.save('unit2', BUFFER_OWNERS['qidi'])
            sharer = dict(_sharing(profiles.get('encoder').syms), MMU_SHARED_ENCODER=True)
            kc = install.parse('unit3', sharer)
            self.assertEqual(self.names(kc, 'Buffer config'), ["Known shared buffers: box_buf, unit2"])
            self.assertEqual(self.names(kc, 'Encoder config'), ["Known shared encoders: unit0"])
            nodes = _menu_nodes(kc, 'Buffer config', comments=True)
            labels = [n.prompt[0].strip() for n in nodes]
            self.assertEqual(labels.index("Known shared buffers: box_buf, unit2"),
                             labels.index('Shared buffer object name') + 1)

            owner = install.parse('unit3', BUFFER_OWNERS['boxturtle'])
            self.assertEqual(self.names(owner, 'Buffer config'), [])

    def test_none_is_listed_without_another_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', profiles.get('tradrack').syms)
            sharer = dict(_sharing(BUFFER_SHARERS['tradrack'], 'private_buf'),
                          MMU_HAS_ENCODER=True, MMU_SHARED_ENCODER=True)
            kc = install.parse('unit1', sharer)
            self.assertEqual(self.names(kc, 'Buffer config'), ["Known shared buffers: none"])
            self.assertEqual(self.names(kc, 'Encoder config'), ["Known shared encoders: none"])


class TestBufferOfTheUsersOwnConfig(unittest.TestCase):
    """A shared name no sibling owns: the sensors are declared here, and no pins."""

    def parse(self, syms):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])
            return install.parse('unit1', _sharing(BUFFER_SHARERS['tradrack'], **syms))

    def test_the_sensors_and_spring_state_are_prompts_without_pins(self):
        kc = self.parse(dict(name='private_buf'))
        self.assertEqual(kc.syms['SHARED_BUFFER_FOUND'].str_value, 'n')
        for sym in BUFFER_SENSORS:
            self.assertTrue(_visible(kc, sym), sym)
        self.assertEqual(kc.named_choices['CHOICE_BUFFER_SPRING_STATE'].visibility, 2)
        kc.syms['MMU_HAS_SENSOR_BUFFER_TENSION'].set_value(2)
        kc.syms['MMU_HAS_SENSOR_BUFFER_PROPORTIONAL'].set_value(2)
        for sym in ('PIN_BUFFER_TENSION', 'PIN_BUFFER_ANALOG', 'PARAM_BUFFER_RANGE',
                    'PARAM_REGISTER_BUFFER_SENSORS', 'PARAM_ANALOG_GAMMA'):
            self.assertFalse(_visible(kc, sym), sym)

    def test_the_declared_sensors_steer_this_units_defaults(self):
        kc = self.parse(dict(name='private_buf'))
        self.assertTrue(kc.is_enabled('W15'))                    # nothing declared yet
        self.assertFalse(kc.is_enabled('W29'))
        kc.syms['MMU_HAS_SENSOR_BUFFER_COMPRESSION'].set_value(2)
        kc.named_choices['CHOICE_BUFFER_SPRING_STATE'].syms[0].set_value(2)   # tension
        self.assertFalse(kc.is_enabled('W15'))
        self.assertEqual(kc.syms['PARAM_BUFFER_SPRING_STATE'].str_value, 'tension')
        self.assertEqual(kc.syms['PARAM_AUTOCAL_BOWDEN_LENGTH'].str_value, '1')
        self.assertEqual(kc.named_choices['CHOICE_EXTRUDER_HOMING_ENDSTOP'].selection.name,
                         'CHOICE_EXTRUDER_HOMING_ENDSTOP_COMPRESSION')

    def test_a_blank_shared_name_is_warned(self):
        self.assertTrue(self.parse(dict(name='')).is_enabled('W29'))

    def test_a_dim_hint_says_to_declare_it(self):
        import kconfiglib
        hint = lambda kc: [c for c in _menu_comments(kc, 'Buffer config') if 'enter its' in c]
        kc = self.parse(dict(name='private_buf'))
        found = hint(kc)
        self.assertEqual(len(found), 1, _menu_comments(kc, 'Buffer config'))
        node = next(n for n in kc.node_iter() if n.item == kconfiglib.COMMENT and n.prompt[0] == found[0])
        self.assertTrue(getattr(node, 'dim', False))
        self.assertEqual(hint(self.parse(dict(name='unit0'))), [])       # unit0's own buffer
        self.assertEqual(hint(self.parse(dict(name=''))), [])            # W29 says it instead

    def test_the_sensors_last_saved_are_kept_when_the_owner_goes(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])
            install.save('unit1', _sharing(BUFFER_SHARERS['tradrack']))
            install.save('unit0', profiles.get('tradrack').syms)    # unit0 loses its buffer
            kc = install.refresh('unit1')
            self.assertEqual(kc.syms['SHARED_BUFFER_FOUND'].str_value, 'n')
            self.assertEqual(kc.syms['PARAM_SYNC_FEEDBACK_BUFFER_NAME'].str_value, 'unit0')
            self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_COMPRESSION'], 'y')
            self.assertEqual(_flags(kc)['MMU_HAS_SENSOR_BUFFER_TENSION'], 'y')
            self.assertEqual(kc.syms['PARAM_BUFFER_SPRING_STATE'].str_value, 'tension')

    def test_a_single_unit_can_share_a_buffer(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig('single_sharer', _sharing(profiles.get('tradrack').syms, 'xyz'))
        self.assertTrue(_visible(kc, 'MMU_SHARED_SYNC_FEEDBACK_BUFFER'))
        self.assertTrue(_visible(kc, 'MMU_HAS_SENSOR_BUFFER_TENSION'))
        self.assertEqual(_menu_prompts(kc, 'Buffer config')[:2],
                         ['Use shared buffer?', 'Shared buffer object name'])


class TestOwnerBufferNameWarning(unittest.TestCase):

    def test_two_owners_of_one_name_are_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = _Install(tmp, ('unit0', 'unit1'))
            install.save('unit0', BUFFER_OWNERS['boxturtle'])
            self.assertTrue(install.parse('unit1', dict(
                BUFFER_OWNERS['qidi'], PARAM_SYNC_FEEDBACK_BUFFER_NAME='unit0')).is_enabled('W33'))
            self.assertFalse(install.parse('unit1', BUFFER_OWNERS['qidi']).is_enabled('W33'))
            self.assertFalse(install.parse('unit1', _sharing(BUFFER_OWNERS['qidi'])).is_enabled('W33'))


if __name__ == "__main__":
    unittest.main()
