# Happy Hare MMU Software
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

# Import the adapter first: it installs the fake Klipper tree before loading
# the real controller and its extras.* dependencies.
from test.filament_display import (
    FilamentDisplayState,
    get_filament_position_string,
    strip_color_markup,
)
from extras.mmu.mmu_constants import (
    FILAMENT_POS_HOMED_GATE,
    FILAMENT_POS_START_BOWDEN,
    FILAMENT_POS_UNLOADED,
    FILAMENT_POS_UNKNOWN,
    GATE_AVAILABLE,
    SENSOR_ENCODER,
    SENSOR_EXIT_PREFIX,
)


class TestFilamentDisplayEncoderParking(unittest.TestCase):

    def _render_prefix(self, *, pos, bold=False, has_encoder=True, endstop=SENSOR_ENCODER):
        state = FilamentDisplayState(
            pos=pos,
            bold=bold,
            has_encoder=has_encoder,
            gate_homing_endstop=endstop,
            gate_status=GATE_AVAILABLE,
        )
        return strip_color_markup(get_filament_position_string(state))[:16]

    def test_unloaded_filament_is_parked_one_character_before_encoder(self):
        self.assertEqual(
            self._render_prefix(pos=FILAMENT_POS_UNLOADED),
            "[T0] ━━━━━━━━▶┈e",
        )
        self.assertEqual(
            self._render_prefix(pos=FILAMENT_POS_UNLOADED, bold=True),
            "[T0] ■■■■■■■■■┈e",
        )

    def test_other_filament_positions_are_unchanged(self):
        expected = {
            FILAMENT_POS_UNKNOWN: "[T0] ━▶┈┈┈┈┈┈┈┈e",
            FILAMENT_POS_HOMED_GATE: "[T0] ━━━━━━━━━┫ê",
            FILAMENT_POS_START_BOWDEN: "[T0] ━━━━━━━━━━ê",
        }
        for pos, prefix in expected.items():
            with self.subTest(pos=pos):
                self.assertEqual(self._render_prefix(pos=pos), prefix)

    def test_unloaded_display_without_encoder_is_unchanged(self):
        self.assertEqual(
            self._render_prefix(
                pos=FILAMENT_POS_UNLOADED,
                has_encoder=False,
                endstop=SENSOR_EXIT_PREFIX,
            ),
            "[T0] ━━━━━━━━━▶┈",
        )


if __name__ == '__main__':
    unittest.main()
