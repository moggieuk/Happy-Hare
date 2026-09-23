# Macro expansion in unquoted lexemes.
#
# A `config` name may contain a $(macro) reference - installer/Kconfig.pins:90
# relies on it for the per-gate PIN_GEAR_UART_$(i) symbols. A `choice` name may
# not, because the tokenizer routes it through the "missing quotes" branch,
# which never called _expand_name. That asymmetry is the one thing stopping a
# Kconfig fragment from being sourced once per `prefix :=` and declaring its
# own named choices.
#
# The fix mirrors the symbol branch, but only for a choice name. What makes it
# safe is not that it was tested broadly, but that the construct it enables is
# a hard parse error today: _id_keyword_match stops at the '(' of a '$(' and
# the leftover paren always reaches _trailing_tokens_error. So no Kconfig that
# parses now can change meaning.
#
# Every other lexeme reaching that branch is a title or prompt whose quoted
# form already expands macros; only a choice name cannot be quoted. Leaving
# them alone is what keeps the safety claim caveat-free, and the tests below
# hold the line: other unquoted positions must still raise, and a bare '$'
# with no parens must still be literal.

import glob
import os
import re
import tempfile
import unittest

import kconfiglib

from test.hh import cfg

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keywords after which the tokenizer accepts an unquoted lexeme as a string
# (kconfiglib._STRING_LEX). 'choice' is the one we deliberately change; every
# other one must keep rejecting a macro in that position, or the patch is
# broader than advertised.
OTHER_STRING_LEX_USES = {
    'menu': 'menu MENU_$(prefix)_X\nendmenu\n',
    'mainmenu': 'mainmenu MAIN_$(prefix)_X\n',
    'comment': 'comment COMMENT_$(prefix)_X\n',
    'prompt': 'config A\n  bool\n  prompt PROMPT_$(prefix)_X\n',
    'bool': 'config A\n  bool BOOL_$(prefix)_X\n',
    'int': 'config A\n  int INT_$(prefix)_X\n',
    'string': 'config A\n  string STR_$(prefix)_X\n',
    'tristate': 'config A\n  tristate TRI_$(prefix)_X\n',
}


class _AdHocKconfig(unittest.TestCase):
    """Parse a throwaway Kconfig tree, isolated from installer/."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write(self, name, text):
        path = os.path.join(self.tmpdir.name, name)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)
        return path

    def _parse(self, text, **extra_files):
        for name, body in extra_files.items():
            self._write(name, body)
        path = self._write('Kconfig', text)
        # srctree must point at the temp dir or `source` resolves into installer/
        env = {'srctree': self.tmpdir.name,
               'KCONFIG_CONFIG': os.path.join(self.tmpdir.name, '.config')}
        with cfg._env(env):
            return kconfiglib.Kconfig(path, warn=False)


class TestUnquotedLexemeMacroExpansion(_AdHocKconfig):

    def test_a_named_choice_can_take_a_macro_expanded_name(self):
        kc = self._parse(
            'mainmenu "t"\n'
            'prefix := GEAR\n'
            'choice CHOICE_$(prefix)_TMC\n'
            '  prompt "chip"\n'
            '  config CHOICE_$(prefix)_A\n'
            '    bool "A"\n'
            'endchoice\n')
        self.assertIn('CHOICE_GEAR_TMC', kc.named_choices)
        self.assertIn('CHOICE_GEAR_A', kc.syms)

    def test_one_fragment_sourced_twice_yields_independent_choices(self):
        """The whole point: one definition, one instance per consumer."""
        fragment = (
            'choice CHOICE_$(prefix)_TMC\n'
            '  prompt "$(label) chip"\n'
            '  default CHOICE_$(prefix)_B\n'
            '  config CHOICE_$(prefix)_A\n'
            '    bool "A"\n'
            '  config CHOICE_$(prefix)_B\n'
            '    bool "B"\n'
            'endchoice\n')
        kc = self._parse(
            'mainmenu "t"\n'
            'prefix := GEAR\nlabel := Gear\nsource "frag"\n'
            'prefix := BLOBIFIER\nlabel := Blobifier\nsource "frag"\n',
            frag=fragment)

        gear = kc.named_choices['CHOICE_GEAR_TMC']
        blob = kc.named_choices['CHOICE_BLOBIFIER_TMC']
        self.assertIsNot(gear, blob)
        self.assertEqual([s.name for s in gear.syms],
                         ['CHOICE_GEAR_A', 'CHOICE_GEAR_B'])
        self.assertEqual(gear.nodes[0].prompt[0], 'Gear chip')
        self.assertEqual(blob.nodes[0].prompt[0], 'Blobifier chip')

        # Selecting in one must not disturb the other
        self.assertEqual(gear.selection.name, 'CHOICE_GEAR_B')
        kc.syms['CHOICE_GEAR_A'].set_value(2)
        self.assertEqual(gear.selection.name, 'CHOICE_GEAR_A')
        self.assertEqual(blob.selection.name, 'CHOICE_BLOBIFIER_B')


class TestPatchIsConservative(_AdHocKconfig):
    """The safety argument, made executable."""

    def test_a_bare_dollar_in_a_choice_name_is_still_literal(self):
        """No '(' means no expansion - the name must survive verbatim."""
        kc = self._parse(
            'mainmenu "t"\n'
            'choice FOO$BAR\n'
            '  prompt "p"\n'
            '  config A\n    bool "A"\n'
            'endchoice\n')
        self.assertIn('FOO$BAR', kc.named_choices)

    def test_a_bare_dollar_in_an_unquoted_prompt_is_still_literal(self):
        kc = self._parse(
            'mainmenu "t"\n'
            'config A\n  bool\n  prompt unquoted$THING\n')
        self.assertEqual(kc.syms['A'].nodes[0].prompt[0], 'unquoted$THING')

    def test_a_macro_in_every_other_unquoted_position_is_still_an_error(self):
        """Only `choice` changes. Everything else in _STRING_LEX still rejects it.

        This is what bounds the patch: the newly reachable code path is the one
        we wanted, and no other construct silently started expanding.
        """
        for keyword, body in sorted(OTHER_STRING_LEX_USES.items()):
            with self.subTest(keyword=keyword):
                text = 'prefix := GEAR\n' + body
                if not body.startswith('mainmenu'):
                    text = 'mainmenu "t"\n' + text
                with self.assertRaises(
                        kconfiglib.KconfigError,
                        msg='%s now accepts a macro in an unquoted lexeme' % keyword):
                    self._parse(text)

    def test_expand_name_is_the_identity_without_a_paren_macro(self):
        """_id_keyword_match's character class is exactly the negation of
        _name_special_search's, so for any lexeme with no '$(' the expander
        stops where the tokenizer already stopped. That is the reason the
        patch cannot perturb an existing parse, so it is worth asserting."""
        kc = self._parse('mainmenu "t"\nconfig A\n  bool\n')
        for name in ('PLAIN', 'WITH$DOLLAR', 'trailing$', 'a/b.c-d', '$'):
            with self.subTest(name=name):
                self.assertEqual(kc._expand_name(name, 0),
                                 (name, name, len(name)))


class TestShippedTreeIsUnaffected(unittest.TestCase):

    def test_no_shipped_kconfig_uses_a_macro_in_an_unquoted_lexeme(self):
        """The static half: nothing in the tree reaches the patched branch.

        @repeat is a line-level preprocessor that substitutes its variable
        textually BEFORE tokenizing, so `choice CHOICE_FOO_$(i)` inside a
        `@repeat var=i` block is already `choice CHOICE_FOO_3` by the time the
        tokenizer sees it. Those references never reach the branch and are not
        offenders - the tree has four of them today.
        """
        # `choice` is excluded: it is the construct the patch deliberately
        # enables, and installer/components/ now uses it. Every OTHER keyword
        # must still be macro-free here, which is what bounds the change.
        keywords = '|'.join(sorted(OTHER_STRING_LEX_USES) + ['source',
                                                             'rsource', 'osource'])
        suspect = re.compile(r'^\s*(?:%s)\s+[A-Za-z0-9_/.-]*\$\((\w+)\)' % keywords)
        repeat_start = re.compile(r'^\s*@repeat\s+var=(\w+)')
        offenders = []
        for pattern in ('installer/Kconfig*', 'installer/*/Kconfig*',
                        'installer/*/*/Kconfig*'):
            for path in glob.glob(os.path.join(REPO_ROOT, pattern)):
                active = []
                for number, line in enumerate(open(path, encoding='utf-8'), 1):
                    started = repeat_start.match(line)
                    if started:
                        active.append(started.group(1))
                        continue
                    if line.strip() == '@endrepeat@':
                        active.pop()
                        continue
                    found = suspect.match(line)
                    if found and found.group(1) not in active:
                        offenders.append('%s:%d %s' % (
                            os.path.relpath(path, REPO_ROOT), number, line.strip()))
        self.assertEqual(
            offenders, [],
            'these lines would change meaning under the expansion patch:\n%s'
            % '\n'.join(offenders))

    def test_the_only_user_of_the_feature_is_the_shared_component(self):
        """Keeps the blast radius visible.

        If a macro-named choice ever appears outside installer/components/,
        that is worth a deliberate look rather than a silent spread: the
        construct only pays for itself where a fragment is sourced more than
        once, and anywhere else it just obscures the symbol's real name.
        """
        pattern = re.compile(r'^\s*choice\s+[A-Za-z0-9_]*\$\(')
        users = set()
        for glob_pattern in ('installer/Kconfig*', 'installer/*/Kconfig*',
                             'installer/*/*/Kconfig*'):
            for path in glob.glob(os.path.join(REPO_ROOT, glob_pattern)):
                active = []
                for line in open(path, encoding='utf-8'):
                    started = re.match(r'^\s*@repeat\s+var=(\w+)', line)
                    if started:
                        active.append(started.group(1))
                        continue
                    if line.strip() == '@endrepeat@':
                        active.pop()
                        continue
                    found = pattern.match(line)
                    if found and not active:
                        users.add(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(
            sorted(users), ['installer/components/Kconfig.tmc_driver_menu'])

    def test_no_symbol_or_choice_name_survives_unexpanded(self):
        """Tripwire for a fragment sourced without re-assigning its variables.

        Preprocessor variables are global for the whole parse, so a missing
        `prefix :=` before a `source` produces plausible-looking symbols under
        the previous consumer's name - or, if never assigned, a name with a
        literal '$' or a doubled underscore in it.
        """
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._new_kconfig('macro_names_tripwire')
        names = ([sym.name for sym in kc.unique_defined_syms] +
                 [choice.name for choice in kc.unique_choices if choice.name])
        self.assertEqual([n for n in names if '$' in n or '__' in n], [])


if __name__ == '__main__':
    unittest.main()
