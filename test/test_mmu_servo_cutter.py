"""Render the shipped cutter macro: the harness does not execute macro bodies."""
import configparser
from pathlib import Path
import unittest

import jinja2


class TestServoCutterSequence(unittest.TestCase):
    def test_rip_once_immediately_after_final_cut(self):
        config = configparser.RawConfigParser()
        config.read(Path(__file__).resolve().parents[1]
                    / 'config/macros/mmu_servo_cutter.cfg')
        env = jinja2.Environment(variable_start_string='{', variable_end_string='}',
                                 undefined=jinja2.StrictUndefined)
        template = env.from_string(config['gcode_macro SERVO_CUTTER_ACTION']['gcode'])
        for attempts in (1, 2, 5):
            with self.subTest(cut_attempts=attempts):
                rendered = template.render(printer={
                    'gcode_macro _MMU_SERVO_CUTTER_VARS': {
                        'feed_length': 30, 'cut_length': 10,
                        'cut_attempts': attempts,
                    },
                })
                commands = [line.split('#', 1)[0].strip()
                            for line in rendered.splitlines()
                            if line.split('#', 1)[0].strip()]
                cuts = [i for i, cmd in enumerate(commands) if cmd == '_CUTTER_CLOSE']
                self.assertEqual(len(cuts), attempts)
                self.assertEqual(commands.count('_CUTTER_OPEN'), attempts)
                self.assertEqual(commands.count('_MMU_STEP_MOVE MOVE=-1'), 1)
                rip = commands.index('_MMU_STEP_MOVE MOVE=-1')
                self.assertEqual(rip, cuts[-1] + 1)
                self.assertLess(rip, commands.index('_MMU_STEP_UNLOAD_GATE'))
                self.assertEqual(commands.count('_MMU_EVENT EVENT="filament_cut"'), 1)
