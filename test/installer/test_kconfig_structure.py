# Structural invariants of the Kconfig tree.
#
# These are properties that hold no matter what symbols exist, so unlike a
# snapshot they cost nothing to maintain: adding a symbol is routine and must
# not make anyone regenerate a file.
#
# They exist because a Kconfig fragment sourced once per consumer can go wrong
# in ways no value-level test notices - a symbol picking up an extra
# definition site, or a generated name falling outside the prefix contract -
# and the resulting config still renders and still boots.

import unittest

from test.hh import cfg

# The prefixes that drive the #~DEFAULT~# marker, the (NOT DEFAULT) display
# and the `r` reset key (kconfiglib write path / menuconfig display path).
CONTRACT_PREFIXES = ('PARAM_', 'PIN_', 'BOOL_', 'CHOICE_', 'MMU_HAS_',
                     'UNSELECT_', 'VAR_')

# The three shapes install.sh parses the tree in.
_ENTRY_ENV = {'UNIT_NAME': 'unit0,unit1', 'MCU_NAME': 'unit0,unit1',
              'UNIT_INDEX': '0', 'F_MULTI_UNIT': 'y', 'F_MULTI_UNIT_ENTRY_POINT': 'y'}
_PER_UNIT_ENV = {'UNIT_NAME': 'unit0', 'MCU_NAME': 'unit0', 'UNIT_INDEX': '0',
                 'F_MULTI_UNIT': 'y', 'F_MULTI_UNIT_ENTRY_POINT': ''}


class TestComponentContract(unittest.TestCase):
    """What a fragment under installer/components/ must keep true."""

    @classmethod
    def setUpClass(cls):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kconfig = cfg._new_kconfig('kconfig_structure')

    def test_component_symbols_follow_the_naming_contract(self):
        """Generated names still have to earn their modifiable-default handling.

        The `#~DEFAULT~#` marker, the `(NOT DEFAULT)` display and the `r`
        reset key are all driven off a symbol's PREFIX. A fragment that
        generated, say, TMC_BLOBIFIER_UART would parse and render fine and
        silently lose all three.
        """
        generated = [
            sym.name for sym in self.kconfig.unique_defined_syms
            if any('components/' in node.filename for node in sym.nodes)]
        self.assertTrue(generated, 'no component-generated symbols found at all')
        self.assertEqual(
            [name for name in generated
             if not name.startswith(CONTRACT_PREFIXES)], [])

    def test_no_name_survives_unexpanded(self):
        """Tripwire for a fragment sourced without re-assigning its variables.

        Preprocessor variables are global for the whole parse, so a missing
        `prefix :=` before a `source` yields symbols under the previous
        consumer's name - or, if never assigned, a literal '$' or a doubled
        underscore in the name.
        """
        names = ([sym.name for sym in self.kconfig.unique_defined_syms] +
                 [choice.name for choice in self.kconfig.unique_choices
                  if choice.name])
        self.assertEqual([n for n in names if '$' in n or '__' in n], [])

    def test_the_tmc_component_is_sourced_once_per_consumer(self):
        """Node counts here are a product, and both factors matter.

        The component is split in two - declarations and prompts - and its
        only consumer, Kconfig.purging, is itself sourced twice (root
        Kconfig:251 and :294). So a symbol appears twice per fragment that
        mentions it: 2 for declaration-only, 2 for prompt-only, 4 for both.
        Anything else means a stray or missing source, which is exactly what
        goes wrong when a second stepper starts consuming the same fragment.
        """
        expected = {
            'PARAM_BLOBIFIER_TMC': 2,          # declared, never prompted
            'CHOICE_BLOBIFIER_TMC2209': 2,     # a choice member, menu only
            'BOOL_BLOBIFIER_TMC_SPI': 2,       # derived, never prompted
            'PIN_BLOBIFIER_UART': 4,           # declared and prompted
            'PARAM_BLOBIFIER_RREF': 4,         # declared and prompted
        }
        for name, nodes in sorted(expected.items()):
            with self.subTest(symbol=name):
                sym = self.kconfig.syms[name]
                files = {node.filename for node in sym.nodes}
                self.assertEqual(len(sym.nodes), nodes)
                self.assertTrue(
                    all('components/Kconfig.tmc_driver' in f for f in files),
                    '%s is declared outside the component: %s' % (name, files))

    # A prompt for the bus the chip is not on. It renders nothing - every
    # template gates uart_address behind the UART branch - so it costs the
    # user a meaningless question and nothing ever goes wrong loudly. Two of
    # the three steppers had it.
    BUS_PROMPTS = {
        'GEAR': ('PIN_GEAR_UART', 'PARAM_GEAR_UART_ADDRESS',
                 'PIN_GEAR_UART_1', 'PARAM_GEAR_UART_ADDRESS_1'),
        'SELECTOR': ('PIN_SELECTOR_UART', 'PARAM_SELECTOR_UART_ADDRESS'),
    }
    SPI_PROMPTS = {
        'GEAR': ('PIN_GEAR_CS', 'PIN_GEAR_CS_1'),
        'SELECTOR': ('PIN_SELECTOR_CS',),
    }
    # A machine type that supplies that stepper, plus a board that asks for
    # a UART address at all.
    BASE = {'GEAR': {'MMU_TYPE_BOX_TURTLE_1_0': True,
                     'BOARD_TYPE_MANUAL': True},
            'SELECTOR': {'MMU_TYPE_TRADRACK_1_0': True,
                         'BOARD_TYPE_MANUAL': True}}

    NO_TMC_PROMPTS = {
        'GEAR': ('PIN_GEAR_UART', 'PIN_GEAR_CS', 'PIN_GEAR_DIAG',
                 'PARAM_GEAR_UART_ADDRESS', 'PARAM_GEAR_STALLGUARD_THRESHOLD'),
        'SELECTOR': ('PIN_SELECTOR_UART', 'PIN_SELECTOR_CS',
                     'PIN_SELECTOR_DIAG', 'PARAM_SELECTOR_UART_ADDRESS',
                     'PARAM_SELECTOR_STALLGUARD_THRESHOLD'),
    }

    def test_a_stepper_with_no_tmc_is_asked_nothing_about_one(self):
        for prefix in sorted(self.NO_TMC_PROMPTS):
            with self.subTest(stepper=prefix):
                syms = dict(self.BASE[prefix])
                syms['CHOICE_%s_TMC_NONE' % prefix] = True
                with cfg._env(cfg._SINGLE_UNIT_ENV):
                    kc = cfg._kconfig('no_tmc_' + prefix, syms)
                shown = [n for n in self.NO_TMC_PROMPTS[prefix]
                         if kc.syms[n].visibility]
                self.assertEqual(
                    shown, [],
                    'no driver is written for this stepper, so these prompts '
                    'cannot affect anything: %s' % ', '.join(shown))

    def test_no_uart_prompt_is_offered_for_an_spi_chip(self):
        for prefix in sorted(self.BUS_PROMPTS):
            for chip, spi in (('TMC2209', False), ('TMC2130', True),
                              ('TMC2660', True), ('TMC5160', True),
                              ('TMC2240_UART', False), ('TMC2240_SPI', True)):
                with self.subTest(stepper=prefix, chip=chip):
                    syms = dict(self.BASE[prefix])
                    syms['CHOICE_%s_%s' % (prefix, chip)] = True
                    with cfg._env(cfg._SINGLE_UNIT_ENV):
                        kc = cfg._kconfig('bus_prompts_%s_%s' % (prefix, chip),
                                          syms)
                    for name in self.BUS_PROMPTS[prefix]:
                        self.assertEqual(
                            bool(kc.syms[name].visibility), not spi,
                            '%s is a UART prompt and this chip is on %s'
                            % (name, 'SPI' if spi else 'UART'))
                    for name in self.SPI_PROMPTS[prefix]:
                        self.assertEqual(
                            bool(kc.syms[name].visibility), spi,
                            '%s is an SPI prompt and this chip is on %s'
                            % (name, 'SPI' if spi else 'UART'))

    def test_all_three_parse_shapes_declare_the_same_tree(self):
        """`if MULTI_UNIT_ENTRY_POINT` attaches a dependency, it does not skip
        a source - so every shape declares the same symbols and only which
        defaults are satisfiable differs. Easy to break by moving a source
        under an `if` that changes structure rather than dependency.
        """
        def shape(kc):
            return (sorted(sym.name for sym in kc.unique_defined_syms),
                    sorted(c.name for c in kc.unique_choices if c.name))

        baseline = shape(self.kconfig)
        for label, env in (('multi-unit entry point', _ENTRY_ENV),
                           ('multi-unit per unit', _PER_UNIT_ENV)):
            with self.subTest(shape=label):
                with cfg._env(env):
                    other = cfg._new_kconfig('kconfig_structure_' + label)
                self.assertEqual(
                    baseline, shape(other),
                    'the %s parse declares a different tree than the '
                    'single-unit parse' % label)


if __name__ == '__main__':
    unittest.main()
