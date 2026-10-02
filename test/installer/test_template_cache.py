# Installer templates may share compiled code, but never render-time values or roots.
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from installer import build
from test.hh import cfg


class TestTemplateCache(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.values = {'UNIT_NAME': 'unit0'}
        self.kconfig = SimpleNamespace(
            config_file='<template-cache-test>', as_dict=lambda: dict(self.values))

    def render(self, root=None, **extra):
        with cfg._chdir(root or self.root):
            return build.render_template('template.cfg', self.kconfig, extra)

    def rewrite(self, path, text):
        # Explicitly change mtime; no sleeps or filesystem timestamp-resolution races.
        previous = path.stat().st_mtime
        path.write_text(text, encoding='utf-8')
        os.utime(path, (previous + 2, previous + 2))

    def test_repeated_render_compiles_once_and_uses_current_values(self):
        (self.root / 'template.cfg').write_text(
            '[[ UNIT_NAME ]]:[[ LABEL|klipper_string_literal ]]', encoding='utf-8')
        original = build.Environment.compile
        with patch.object(build.Environment, 'compile', autospec=True,
                          side_effect=original) as compile_template:
            first = self.render(LABEL='first')
            self.values['UNIT_NAME'] = 'unit1'
            label = 'fresh "quoted" value'
            second = self.render(LABEL=label)
        self.assertEqual(first, 'unit0:"first"')
        self.assertEqual(second, 'unit1:' + json.dumps(label))
        self.assertEqual(compile_template.call_count, 1)

    def test_same_template_names_in_different_directories_stay_independent(self):
        for name in ('left', 'right'):
            root = self.root / name
            root.mkdir()
            (root / 'template.cfg').write_text(
                '[% include "part.cfg" %]', encoding='utf-8')
            (root / 'part.cfg').write_text(name + ':[[ UNIT_NAME ]]', encoding='utf-8')
        for name in ('left', 'right', 'left'):
            self.assertEqual(self.render(self.root / name), name + ':unit0')

    def test_changed_template_and_include_are_reloaded(self):
        template = self.root / 'template.cfg'
        include = self.root / 'part.cfg'
        template.write_text('first:[% include "part.cfg" %]', encoding='utf-8')
        include.write_text('old', encoding='utf-8')
        self.assertEqual(self.render(), 'first:old')
        self.rewrite(include, 'new')
        self.assertEqual(self.render(), 'first:new')
        self.rewrite(template, 'second:[% include "part.cfg" %]')
        self.assertEqual(self.render(), 'second:new')

    def test_failed_render_can_be_retried_with_corrected_values(self):
        (self.root / 'template.cfg').write_text('[[ MISSING.field ]]', encoding='utf-8')
        with self.assertLogs(level='ERROR'), self.assertRaises(SystemExit):
            self.render()
        self.assertEqual(self.render(MISSING={'field': 'recovered'}), 'recovered')
