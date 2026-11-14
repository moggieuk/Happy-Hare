# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: The optional stream component is installed and removed with Happy Hare.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from installer import build

ROOT = Path(__file__).resolve().parents[2]

class TestTelemetryComponentInstall(unittest.TestCase):
    def test_install_preserves_existing_config_and_is_idempotent(self):
        template = (ROOT / 'installer/moonraker_update.txt').read_text().replace(
            '[[PARAM_HAPPY_HARE_HOME]]', '/home/pi/happy-hare')
        for case in ('1', '2'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'moonraker.conf'
                fixture = ROOT / 'test/installer/moonraker' / case
                path.write_text((fixture / 'in.cfg').read_text())
                with patch.object(build, 'load_parsed_kconfig', return_value=None), patch.object(
                        build, 'render_template', return_value=template):
                    for repeat in range(2):
                        build.install_moonraker('unused', str(path), 'unused')
                        self.assertEqual(path.read_text().strip(), (fixture / 'expected.cfg').read_text().strip())
                build.uninstall_moonraker(str(path))
                self.assertNotIn('[mmu_telemetry]', path.read_text())
                self.assertNotIn('[mmu_server]', path.read_text())
                self.assertIn('[server]', path.read_text())
