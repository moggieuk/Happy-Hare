# The shared i2c bus definition, components/Kconfig.i2c_bus.
#
# Every i2c device (environment sensor, NFC reader; shared and per gate) takes
# its bus type, bus name and software pins from one fragment. Pinned here:
#
# - the bus choice offers the board's buses, then "Custom bus name", and only
#   a board's own default preselects one of its buses
# - a custom name renders as i2c_bus, a blank one leaves it out, and software
#   pins render exactly as typed
# - every value prompt on a page lines up, including gates 10 and 11
# - a v4.0 config migrates on load (kconfiglib HH_RENAMED_SYMBOLS and friends)
#   and renders the same: renamed pins, the environment sensor's MCU prefix,
#   its old fixed i2c2/i2c3 buses and the NFC reader's free-text bus name

import os
import re
import tempfile
import unittest

import kconfiglib

from test.hh import cfg, profiles

ENV = {'MMU_HAS_ENVIRONMENT_SENSOR': True}
NFC = {'MMU_HAS_NFC_READER': True, 'MMU_HAS_COMMON_NFC_READER': True,
       'CHOICE_NFC_READER_TYPE_PN532': True}
NFC_GATES = {'MMU_HAS_NFC_READER': True, 'MMU_HAS_PER_GATE_NFC_READERS': True,
             'CHOICE_NFC_READER_TYPE_PN532_1': True}
X5 = {'BOARD_TYPE_CHAMELEON_X5_1_0': True}
EBB = {'BOARD_TYPE_EBB_GEN1': True}

# (prefix, suffix, base profile, syms, the device's section in mmu_hardware.cfg)
DEVICES = (
    ('ENVIRONMENT_SENSOR', '', 'boxturtle', ENV, 'temperature_sensor unit0_Env'),
    ('ENVIRONMENT_SENSOR', '_1', 'emu', {}, 'temperature_sensor unit0_Env1'),
    ('NFC_READER', '', 'boxturtle', NFC, 'mmu_nfc_reader unit0_nfc'),
    ('NFC_READER', '_1', 'boxturtle', NFC_GATES, 'mmu_nfc_reader unit0_nfc1'),
)


def _kconfig(label, base, syms):
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        return cfg._kconfig(label, dict(profiles.get(base).syms, **syms))


def _fresh(label):
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        return cfg._new_kconfig(label)


def _section(base, syms, label, section):
    profile = profiles.get(base).derive(label, syms=syms)
    return cfg.assemble(cfg.render(profile))[section]


def _offered(kc, prefix, suffix=''):
    choice = kc.named_choices['CHOICE_%s_I2C_BUS%s' % (prefix, suffix)]
    return [sym.name for sym in choice.syms if sym.visibility], choice.selection.name


def _v40(kc, tmp, replace):
    """Save `kc`, then rewrite the file the way v4.0 would have written it.

    `replace` maps a regexp over whole lines to its replacement; a None
    replacement drops the line.
    """
    path = os.path.join(tmp, '.mmu_config')
    kc.write_config(path, header='')
    with open(path) as handle:
        lines = handle.read().splitlines()
    out = []
    for line in lines:
        for pattern, new in replace:
            if re.match(pattern, line):
                line = None if new is None else re.sub(pattern, new, line)
                break
        if line is not None:
            out.append(line)
    with open(path, 'w') as handle:
        handle.write('\n'.join(out) + '\n')
    return path


def _upgraded(path, label):
    """Load as olddefconfig does, save, and load that the way the build does."""
    kc = _fresh(label)
    kc.load_config(path, filter_defaults=True)
    saved = path + '.upgraded'
    kc.write_config(saved, header='')
    built = _fresh(label + '_built')
    built.load_config(saved, filter_defaults=False)
    with open(saved) as handle:
        return built, handle.read()


class TestI2cBusChoice(unittest.TestCase):

    def test_board_buses_come_first_then_the_custom_name(self):
        env = 'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_'
        nfc = 'CHOICE_NFC_READER_I2C_BUS_'
        hardware = dict(ENV, CHOICE_ENVIRONMENT_SENSOR_I2C_HARDWARE=True)
        # (label, base, syms, prefix, suffix, offered tags, selected tag); OTHER is "Custom bus name"
        cases = (
            ('env shared', 'boxturtle', ENV, env, '', [], 'OTHER'),
            ('env per gate', 'emu', {}, env, '_1', ['SLB_I2C2_PB10_PB11'], 'SLB_I2C2_PB10_PB11'),
            ('env EBB', 'boxturtle', dict(ENV, **EBB), env, '', ['EBB_I2C3_PB3_PB4'], 'EBB_I2C3_PB3_PB4'),
            # The I2C1 header is the sensor's own, preselected when hardware i2c is chosen
            ('env X5', 'boxturtle', dict(hardware, **X5), env, '', ['X5_I2C1', 'X5_I2C3'], 'X5_I2C3'),
            ('env AFC Pro', 'boxturtle', dict(ENV, BOARD_TYPE_AFC_PRO_1_0=True), env, '',
             ['AFC_PRO_I2C3'], 'AFC_PRO_I2C3'),
            ('env WGB', 'boxturtle', dict(ENV, BOARD_TYPE_WGB_3_0=True), env, '', ['WGB_I2C1'], 'WGB_I2C1'),
            ('env TZB', 'boxturtle', dict(ENV, BOARD_TYPE_TZB_1_0=True), env, '', ['TZB_I2C2'], 'TZB_I2C2'),
            ('env MMB 1.1', 'boxturtle', dict(ENV, BOARD_TYPE_MMB_1_1=True), env, '', ['MMB_1_1_I2C3'], 'MMB_1_1_I2C3'),
            ('env MMB 2.0', 'boxturtle', dict(ENV, BOARD_TYPE_MMB_2_0=True), env, '', ['MMB_2_0_I2C3'], 'MMB_2_0_I2C3'),
            # A sensor soldered to the board: offered to it, still software i2c by default
            ('env KMS', 'kms', {'CHOICE_ENVIRONMENT_SENSOR_I2C_HARDWARE': True}, env, '', ['KMS_I2C2'], 'KMS_I2C2'),
            ('env QIDI', 'qidi', {'CHOICE_ENVIRONMENT_SENSOR_I2C_HARDWARE': True}, env, '',
             ['QIDI_I2C3'], 'QIDI_I2C3'),
            # gpio6/gpio7 are entry sensors 6 and 7 too, so only up to six gates
            ('env ERB 6 gates', 'boxturtle', dict(ENV, BOARD_TYPE_ERB_2=True, PARAM_NUM_GATES='6'), env, '',
             ['ERB_2_I2C1'], 'OTHER'),
            ('env ERB 8 gates', 'boxturtle', dict(ENV, BOARD_TYPE_ERB_2=True, PARAM_NUM_GATES='8'), env, '',
             [], 'OTHER'),
            ('nfc shared', 'boxturtle', NFC, nfc, '', [], 'OTHER'),
            ('nfc X5', 'boxturtle', dict(NFC, **X5), nfc, '', ['X5_I2C1', 'X5_I2C3'], 'X5_I2C1'),
            ('nfc X5 per gate', 'boxturtle', dict(NFC_GATES, **X5), nfc, '_1', ['X5_I2C1', 'X5_I2C3'], 'X5_I2C1'),
            # Offered, but an NFC reader has always defaulted to the MCU's default bus
            ('nfc EBB', 'boxturtle', dict(NFC, **EBB), nfc, '', ['EBB_I2C3_PB3_PB4'], 'OTHER'),
            ('nfc MMB 2.0 per gate', 'boxturtle', dict(NFC_GATES, BOARD_TYPE_MMB_2_0=True), nfc, '_1',
             ['MMB_2_0_I2C3'], 'OTHER'),
            # The KMS and QIDI sensors' buses have no connector for a reader
            ('nfc KMS', 'kms', NFC, nfc, '', [], 'OTHER'),
        )
        for label, base, syms, member, suffix, tags, selected in cases:
            with self.subTest(label):
                kc = _kconfig('i2c_offer_' + label, base, syms)
                prefix = member[len('CHOICE_'):-len('_I2C_BUS_')]
                offered = [member + tag + suffix for tag in tags + ['OTHER']]
                self.assertEqual(_offered(kc, prefix, suffix), (offered, member + selected + suffix))

    def test_every_board_bus_has_its_own_tag_and_bus_name(self):
        """A tag two boards share merges their members, and the first-parsed bus name wins."""
        kc = _kconfig('i2c_tags', 'boxturtle', {})
        prefix = 'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_'
        choice = kc.named_choices['CHOICE_ENVIRONMENT_SENSOR_I2C_BUS']
        names = [sym.name for sym in choice.syms]
        self.assertEqual(len(names), len(set(names)), names)
        bus = kc.syms['PARAM_ENVIRONMENT_SENSOR_I2C_BUS']
        for member in names:
            conditions = [cond for _, cond in bus.defaults if member in kconfiglib.expr_str(cond).split()]
            self.assertLessEqual(len(conditions), 1, member)
        self.assertIn(prefix + 'MMB_2_0_I2C3', names)

    def test_the_ebb42_exit_sensor_leaves_pb4_to_a_device_on_the_i2c_header(self):
        """PB4 is the I2C header's SDA and the exit sensor's default pin."""
        exit_sensor = 'PIN_SHARED_EXIT_SENSOR'
        base = dict(EBB, MMU_HAS_ENCODER=False)
        for label, syms, expected in (
                ('no i2c device', {}, '^unit0:PB4'),
                ('sensor on software i2c', dict(ENV, CHOICE_ENVIRONMENT_SENSOR_I2C_SOFTWARE=True), '^unit0:PB4'),
                ('sensor on the I2C header', dict(ENV, CHOICE_ENVIRONMENT_SENSOR_I2C_HARDWARE=True), ''),
                ('reader on the I2C header', dict(NFC, CHOICE_NFC_READER_I2C_BUS_EBB_I2C3_PB3_PB4=True), '')):
            with self.subTest(label):
                kc = _kconfig('i2c_ebb_exit_' + label, 'boxturtle', dict(base, **syms))
                self.assertEqual(kc.syms[exit_sensor].str_value, expected)

    def test_a_custom_name_renders_and_a_blank_one_is_left_out(self):
        for prefix, suffix, base, syms, section in DEVICES:
            for name in ('i2c1', ''):
                with self.subTest(device=section, name=name):
                    custom = dict(syms, **{'CHOICE_%s_I2C_BUS_OTHER%s' % (prefix, suffix): True,
                                           'PARAM_%s_I2C_BUS%s' % (prefix, suffix): name})
                    label = 'i2c_custom_%s%s_%s' % (prefix, suffix, name or 'blank')
                    self.assertEqual(_section(base, custom, label, section).get('i2c_bus'),
                                     name or None)

    def test_software_pins_render_exactly_as_typed(self):
        for prefix, suffix, base, syms, section in DEVICES:
            with self.subTest(device=section):
                pins = dict(syms, **{'CHOICE_%s_I2C_SOFTWARE%s' % (prefix, suffix): True,
                                     'PIN_%s_I2C_SCL%s' % (prefix, suffix): 'mmu:PB8',
                                     'PIN_%s_I2C_SDA%s' % (prefix, suffix): 'mmu:PB9'})
                rendered = _section(base, pins, 'i2c_pins_%s%s' % (prefix, suffix), section)
                self.assertEqual((rendered['i2c_software_scl_pin'], rendered['i2c_software_sda_pin']),
                                 ('mmu:PB8', 'mmu:PB9'))
                self.assertNotIn('i2c_bus', rendered)


class TestI2cBusPrompts(unittest.TestCase):

    def _widths(self, kc, parent):
        """Widths of the value prompts shown under `parent` (not toggles or choice members)."""
        widths = {}
        node = parent.list
        while node:
            item = node.item
            if node.prompt and kconfiglib.expr_value(node.prompt[1]) and \
                    not (isinstance(item, kconfiglib.Symbol) and item.choice) and \
                    not (isinstance(item, kconfiglib.Symbol) and item.orig_type is kconfiglib.BOOL):
                widths[item.name] = len(node.prompt[0])
            node = node.next
        return widths

    def test_every_value_prompt_on_a_page_lines_up(self):
        twelve = dict(NFC_GATES, PARAM_NUM_GATES='12', CHOICE_NFC_READER_TYPE_PN532_11=True,
                      CHOICE_NFC_READER_I2C_BUS_OTHER_1=True, CHOICE_NFC_READER_I2C_BUS_OTHER_11=True)
        kc = _kconfig('i2c_widths', 'boxturtle', twelve)
        self.assertEqual(kc.syms['PARAM_NUM_GATES'].str_value, '12')
        for gate in (1, 11):
            with self.subTest(gate=gate):
                toggle = next(n for n in kc.syms['PARAM_NFC_READER_GATE_%d' % gate].nodes if n.prompt)
                widths = self._widths(kc, toggle)
                self.assertIn('PARAM_NFC_READER_I2C_BUS_%d' % gate, widths)
                self.assertEqual(set(widths.values()), {28}, widths)

    def test_help_fits_the_menuconfig_help_window(self):
        for prefix, suffix, base, syms, _ in DEVICES:
            kc = _kconfig('i2c_help_%s%s' % (prefix, suffix), base, syms)
            items = [kc.named_choices['CHOICE_%s_I2C_BUS_TYPE%s' % (prefix, suffix)],
                     kc.named_choices['CHOICE_%s_I2C_BUS%s' % (prefix, suffix)]] + \
                [kc.syms[name % (prefix, suffix)] for name in
                 ('PARAM_%s_I2C_BUS%s', 'PIN_%s_I2C_SCL%s', 'PIN_%s_I2C_SDA%s')]
            for item in items:
                with self.subTest(item=item.name):
                    help_text = next(node.help for node in item.nodes if node.help)
                    self.assertLessEqual(len(help_text.split('\n')), 7)
                    self.assertEqual(help_text, help_text.strip())


class TestI2cBusUpgrade(unittest.TestCase):

    def test_environment_pins_are_renamed_and_gain_their_mcu(self):
        cases = (
            ('boxturtle', dict(ENV, CHOICE_ENVIRONMENT_SENSOR_I2C_SOFTWARE=True), '',
             ('PB6', '^!PB7'), ('unit0:PB6', '^!unit0:PB7')),
            # Per-gate MCU: the template used each gate's own MCU
            ('emu', {'CHOICE_ENVIRONMENT_SENSOR_I2C_SOFTWARE_3': True}, '_3',
             ('PB6', 'other:PB7'), ('unit0_gate3:PB6', 'other:PB7')),
        )
        for base, syms, suffix, old, new in cases:
            with self.subTest(profile=base):
                kc = _kconfig('i2c_env_pins_' + base, base, syms)
                with tempfile.TemporaryDirectory() as tmp:
                    path = _v40(kc, tmp, [
                        (r'CONFIG_PIN_ENVIRONMENT_SENSOR_I2C_SCL%s=.*' % suffix,
                         'CONFIG_PIN_ENVIRONMENT_SENSOR_SCL%s="%s"' % (suffix, old[0])),
                        (r'CONFIG_PIN_ENVIRONMENT_SENSOR_I2C_SDA%s=.*' % suffix,
                         'CONFIG_PIN_ENVIRONMENT_SENSOR_SDA%s="%s"' % (suffix, old[1]))])
                    built, saved = _upgraded(path, 'i2c_env_pins_up_' + base)
                self.assertEqual((built.syms['PIN_ENVIRONMENT_SENSOR_I2C_SCL' + suffix].str_value,
                                  built.syms['PIN_ENVIRONMENT_SENSOR_I2C_SDA' + suffix].str_value), new)
                self.assertNotIn('PIN_ENVIRONMENT_SENSOR_SCL', saved)

    def test_migrated_pins_follow_a_unit_rename(self):
        """The template used to follow a rename by itself; the saved prefix must too."""
        from installer import unit_migration
        kc = _kconfig('i2c_env_rename', 'emu', {'CHOICE_ENVIRONMENT_SENSOR_I2C_SOFTWARE_3': True})
        with tempfile.TemporaryDirectory() as tmp:
            path = _v40(kc, tmp, [
                (r'CONFIG_PIN_ENVIRONMENT_SENSOR_I2C_SCL_3=.*', 'CONFIG_PIN_ENVIRONMENT_SENSOR_SCL_3="PB6"'),
                (r'CONFIG_PIN_ENVIRONMENT_SENSOR_I2C_SDA_3=.*', 'CONFIG_PIN_ENVIRONMENT_SENSOR_SDA_3="^PB7"')])
            _upgraded(path, 'i2c_env_rename_up')
            unit_migration.rewrite_kconfig(path + '.upgraded', {'unit0': 'box'}, {'unit0', 'box'})
            values = unit_migration.read_kconfig(path + '.upgraded')
        self.assertEqual((values['PIN_ENVIRONMENT_SENSOR_I2C_SCL_3'], values['PIN_ENVIRONMENT_SENSOR_I2C_SDA_3']),
                         ('box_gate3:PB6', '^box_gate3:PB7'))

    def test_a_shared_mcu_prefix_uses_the_unit_mcu(self):
        """Per-gate pins without per-gate MCUs fall back to the MMU MCU, as the template did."""
        kc = _kconfig('i2c_shared_mcu', 'boxturtle', {})
        self.assertEqual(kc._mcu_prefixed_pin('PIN_ENVIRONMENT_SENSOR_I2C_SCL_3', 'PB6'), 'unit0:PB6')
        self.assertEqual(kc._mcu_prefixed_pin('PIN_ENVIRONMENT_SENSOR_I2C_SCL_3', ''), '')

    def test_nfc_pins_are_renamed_unchanged(self):
        kc = _kconfig('i2c_nfc_pins', 'nfc_pn532_sw_i2c', {})
        with tempfile.TemporaryDirectory() as tmp:
            path = _v40(kc, tmp, [(r'CONFIG_PIN_NFC_READER_I2C_(SCL|SDA)_(\d+)=',
                                   r'CONFIG_PARAM_NFC_READER_\1_PIN_\2=')])
            built, saved = _upgraded(path, 'i2c_nfc_pins_up')
        self.assertEqual([built.syms['PIN_NFC_READER_I2C_%s_%d' % (line, gate)].str_value
                          for gate in (0, 1) for line in ('SCL', 'SDA')],
                         ['unit0:PB8', 'unit0:PB9', 'unit0:PC4', 'unit0:PC5'])
        self.assertNotIn('PARAM_NFC_READER_SCL_PIN', saved)

    def test_the_environment_sensors_old_buses_keep_their_name(self):
        old = r'CONFIG_CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_OTHER=y.*'
        drop = (r'.*CONFIG_(CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_|PARAM_ENVIRONMENT_SENSOR_I2C_BUS=)', None)
        cases = (
            ('i2c2 default', ENV, 'CONFIG_CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_I2C2=y #~DEFAULT~#',
             'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_OTHER', 'i2c2_PB10_PB11'),
            ('i2c3 chosen', ENV, 'CONFIG_CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_I2C3=y',
             'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_OTHER', 'i2c3_PB3_PB4'),
            # The EBB offers i2c3 itself now, and it was the EBB's old default
            ('EBB i2c3 default', dict(ENV, **EBB), 'CONFIG_CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_I2C3=y #~DEFAULT~#',
             'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_EBB_I2C3_PB3_PB4', 'i2c3_PB3_PB4'),
        )
        for label, syms, line, selected, bus in cases:
            with self.subTest(label):
                kc = _kconfig('i2c_env_bus', 'boxturtle', syms)
                with tempfile.TemporaryDirectory() as tmp:
                    path = _v40(kc, tmp, [(old, line), drop])
                    built, saved = _upgraded(path, 'i2c_env_bus_up')
                    again, resaved = _upgraded(path + '.upgraded', 'i2c_env_bus_again')
                choice = built.named_choices['CHOICE_ENVIRONMENT_SENSOR_I2C_BUS']
                self.assertEqual((choice.selection.name, built.syms['PARAM_ENVIRONMENT_SENSOR_I2C_BUS'].str_value),
                                 (selected, bus))
                self.assertEqual(resaved, saved)

    def test_a_typed_nfc_bus_name_becomes_the_custom_name(self):
        drop = (r'.*CONFIG_CHOICE_NFC_READER_I2C_BUS_', None)
        cases = (
            ('X5 typed', 'CONFIG_PARAM_NFC_READER_I2C_BUS="i2c3"', 'CHOICE_NFC_READER_I2C_BUS_OTHER', 'i2c3'),
            ('X5 blank typed', 'CONFIG_PARAM_NFC_READER_I2C_BUS=""', 'CHOICE_NFC_READER_I2C_BUS_OTHER', ''),
            ('X5 default', 'CONFIG_PARAM_NFC_READER_I2C_BUS="i2c1" #~DEFAULT~#',
             'CHOICE_NFC_READER_I2C_BUS_X5_I2C1', 'i2c1'),
        )
        for label, line, selected, bus in cases:
            with self.subTest(label):
                kc = _kconfig('i2c_nfc_bus', 'boxturtle', dict(NFC, **X5))
                with tempfile.TemporaryDirectory() as tmp:
                    path = _v40(kc, tmp, [(r'CONFIG_PARAM_NFC_READER_I2C_BUS=.*', line), drop])
                    built, saved = _upgraded(path, 'i2c_nfc_bus_up')
                    again, resaved = _upgraded(path + '.upgraded', 'i2c_nfc_bus_again')
                choice = built.named_choices['CHOICE_NFC_READER_I2C_BUS']
                self.assertEqual((choice.selection.name, built.syms['PARAM_NFC_READER_I2C_BUS'].str_value),
                                 (selected, bus))
                self.assertEqual(resaved, saved)


if __name__ == '__main__':
    unittest.main()
