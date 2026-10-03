# Extruder homing endstop defaults with a toolhead sensor.
#
# A toolhead sensor makes extruder homing optional, but the endstop is still needed
# (e.g. bowden calibration). Hiding the choice would leave no member selected and
# PARAM_EXTRUDER_HOMING_ENDSTOP would fall through to "none", so it must stay
# visible and default exactly as it does when homing is forced.

import os
import tempfile
import unittest

from test.hh import cfg, profiles

CHOICE = "CHOICE_EXTRUDER_HOMING_ENDSTOP"
PARAM = "PARAM_EXTRUDER_HOMING_ENDSTOP"


def _all_mmu_types():
    """Each selectable MMU type as the syms needed to pick it (family + member)."""
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        kc = cfg._kconfig("mmu_type_enumeration", {})
    import kconfiglib       # importable once the harness has parsed a tree
    types = []
    for top in kc.syms["MMU_CUSTOM"].choice.syms:
        if not top.name.startswith("MMU_FAMILY_"):
            types.append({top.name: True})
            continue
        for choice in kc.choices:
            if top in kconfiglib.expr_items(choice.direct_dep):
                types.extend({top.name: True, sym.name: True}
                             for sym in choice.syms if sym.name.startswith("MMU_TYPE_"))
    return types


class TestExtruderHomingWithToolheadSensor(unittest.TestCase):

    def _kconfig(self, label, syms):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            return cfg._kconfig(label, syms)

    def test_toolhead_choice_matches_forced_choice_for_every_type(self):
        for type_syms in _all_mmu_types():
            label = "_".join(type_syms)
            with self.subTest(mmu_type=label):
                normal = self._kconfig(label + "_toolhead",
                                       dict(type_syms, MMU_HAS_SENSOR_TOOLHEAD=True))
                forced = self._kconfig(label + "_toolhead_forced",
                                       dict(type_syms, MMU_HAS_SENSOR_TOOLHEAD=True,
                                            PARAM_EXTRUDER_FORCE_HOMING=True))
                self.assertGreater(normal.named_choices[CHOICE].visibility, 0)
                self.assertEqual(normal.get(PARAM), forced.get(PARAM))

    def test_ercf_with_toolhead_sensor_uses_encoder_collision(self):
        kc = self._kconfig("ercf_toolhead", {
            "MMU_FAMILY_ERCF": True,
            "MMU_TYPE_ERCF_1_1": True,
            "MMU_HAS_SENSOR_TOOLHEAD": True,
        })
        self.assertEqual(kc.get(PARAM), "encoder")

        # olddefconfig saves this computed value and build.py renders from it
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".mmu_config")
            kc.write_config(path)
            with open(path) as f:
                saved = [l for l in f if l.startswith("CONFIG_%s=" % PARAM)]
        self.assertEqual(saved, ['CONFIG_%s="encoder" #~DEFAULT~#\n' % PARAM])

    def test_collision_homing_controls_shown_with_toolhead_sensor(self):
        collision = self._kconfig("ercf_toolhead_collision", {
            "MMU_FAMILY_ERCF": True,
            "MMU_TYPE_ERCF_1_1": True,
            "MMU_HAS_SENSOR_TOOLHEAD": True,
        })
        compression = self._kconfig("box_turtle_toolhead", {
            "MMU_TYPE_BOX_TURTLE_1_0": True,
            "MMU_HAS_SENSOR_TOOLHEAD": True,
        })
        self.assertEqual(compression.get(PARAM), "filament_compression")
        for sym in ("PARAM_EXTRUDER_HOMING_MAX", "PARAM_EXTRUDER_COLLISION_HOMING_CURRENT"):
            with self.subTest(sym=sym):
                self.assertGreater(collision.syms[sym].visibility, 0)
                self.assertEqual(compression.syms[sym].visibility, 0)

    def test_extruder_gate_homing_with_toolhead_sensor_advances_from_entry(self):
        kc = self._kconfig("low_rider_toolhead", {
            "MMU_TYPE_LOW_RIDER_1_0": True,
            "MMU_HAS_SENSOR_TOOLHEAD": True,
        })
        self.assertEqual(kc.get("PARAM_GATE_HOMING_ENDSTOP"), "extruder")
        self.assertEqual(kc.get(PARAM), "extruder")

    def test_rendered_parameters(self):
        profile = profiles.Profile("ercf_toolhead_render", syms={
            "MMU_FAMILY_ERCF": True,
            "MMU_TYPE_ERCF_1_1": True,
            "MMU_HAS_SENSOR_TOOLHEAD": True,
        })
        params = dict(cfg.assemble(cfg.render(profile)).items("mmu_unit_parameters unit0"))
        self.assertEqual(params["extruder_homing_endstop"], "encoder")


if __name__ == "__main__":
    unittest.main()
