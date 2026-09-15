"""The MCU opt-out must suppress setup without suppressing MMU hardware."""

import tempfile
import unittest

from test.hh import cfg, profiles


class TestManualMcu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = profiles.TRADRACK.derive(
            "manual_mcu", syms={
                "BOARD_TYPE_MANUAL": True,
                "PIN_GEAR_STEP": "mcu:PA1",
                "PARAM_GEAR_UART_ADDRESS": 2,
            })
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kconfig = cfg._kconfig(cls.profile.name, cls.profile.syms)

    def test_connection_and_cpu_sensor_options_are_disabled(self):
        kc = self.kconfig
        self.assertTrue(kc.is_enabled("BOARD_TYPE_MANUAL"))
        self.assertEqual(kc.get("PARAM_BOARD_TYPE"), "Shared / externally configured")
        for name in ("CHOICE_MMU_CONNECTION_TYPE_SERIAL",
                     "CHOICE_MMU_CONNECTION_TYPE_CANBUS",
                     "MMU_SERIAL_DEVICE_OTHER", "MMU_CANBUS_UUID_OTHER",
                     "CHOICE_BUFFER_CONNECTION_TYPE_SERIAL",
                     "CHOICE_BUFFER_CONNECTION_TYPE_CANBUS",
                     "BOOL_CREATE_MCU_ENVIRONMENT_SENSORS"):
            with self.subTest(symbol=name):
                self.assertEqual(kc.syms[name].visibility, 0)
                self.assertIn(kc.get(name), ("n", ""))

    def test_custom_pins_and_uart_address_still_render(self):
        hardware = cfg.render(self.profile)["config/base/mmu_hardware.cfg"]
        self.assertIn("[mmu_unit unit0]", hardware)
        self.assertIn("[mmu_stepper unit0_gear]", hardware)
        self.assertIn("step_pin                 : mcu:PA1", hardware)
        self.assertIn("uart_address             : 2", hardware)

    def test_supported_mcu_topologies_can_opt_out(self):
        for machine in ("MMU_TYPE_TRADRACK_1_0", "MMU_TYPE_EMU_1_0"):
            with self.subTest(machine=machine):
                profile = profiles.Profile(
                    "manual_" + machine,
                    syms={machine: True, "BOARD_TYPE_MANUAL": True})
                hardware = cfg.render(profile)["config/base/mmu_hardware.cfg"]
                self.assertFalse(any(s.startswith("mcu ")
                                     for s in cfg.sections(hardware)))
                self.assertNotIn("temperature_mcu", hardware)
                self.assertNotIn("serial", hardware.split("[mmu_unit")[0])
                self.assertNotIn("canbus", hardware.split("[mmu_unit")[0])
                self.assertIn("MCU setup is managed outside Happy Hare", hardware)

    def test_fixed_custom_boards_reject_manual_selection(self):
        for machine, board in (("MMU_TYPE_KMS_1_0", "BOARD_TYPE_KMS_1_0"),
                               ("MMU_TYPE_VVD_1_0", "BOARD_TYPE_VVD_1_0"),
                               ("MMU_TYPE_QIDI_BOX_1_0", "BOARD_TYPE_QIDI_BOX_2_0")):
            with self.subTest(machine=machine):
                # Also exercise switching from a previously selected manual board.
                with cfg._env(cfg._SINGLE_UNIT_ENV):
                    kc = cfg._kconfig("fixed_board", self.profile.syms)
                    kc.syms[machine].set_value(2)
                    kc.syms["BOARD_TYPE_MANUAL"].set_value(2)
                    self.assertEqual(kc.syms["BOARD_TYPE_MANUAL"].visibility, 0)
                    self.assertEqual(kc.syms["BOARD_TYPE_MANUAL"].str_value, "n")
                    self.assertTrue(kc.is_enabled(board))
                    self.assertTrue(kc.is_enabled("BOOL_CREATE_MCU_ENVIRONMENT_SENSORS"))
                    with tempfile.NamedTemporaryFile() as saved:
                        kc.write_config(saved.name)
                        reloaded = cfg._kconfig("fixed_board_reload", {})
                        reloaded.load_config(saved.name)
                    self.assertFalse(reloaded.is_enabled("BOARD_TYPE_MANUAL"))
                    self.assertTrue(reloaded.is_enabled(board))
                hardware = cfg._render_templates(
                    ["config/base/mmu_hardware.cfg"], reloaded,
                    {"PARAM_TOTAL_NUM_GATES": reloaded.getint("PARAM_NUM_GATES")}
                )["config/base/mmu_hardware.cfg"]
                self.assertIn("[mcu unit0]", hardware)
                self.assertIn("temperature_mcu", hardware)
                self.assertNotIn("MCU setup is managed outside Happy Hare", hardware)

    def test_selection_survives_save_reload_and_can_be_reversed(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig("manual_mcu_reload", self.profile.syms)
            with tempfile.NamedTemporaryFile() as saved:
                kc.write_config(saved.name)
                reloaded = cfg._kconfig("manual_mcu_reloaded", {})
                reloaded.load_config(saved.name)
        self.assertTrue(reloaded.is_enabled("BOARD_TYPE_MANUAL"))
        self.assertFalse(reloaded.is_enabled("CHOICE_MMU_CONNECTION_TYPE_SERIAL"))
        reloaded.syms["BOARD_TYPE_OTHER"].set_value(2)
        self.assertTrue(reloaded.is_enabled("CHOICE_MMU_CONNECTION_TYPE_SERIAL"))
        self.assertTrue(reloaded.is_enabled("BOOL_CREATE_MCU_ENVIRONMENT_SENSORS"))

    def test_opt_out_is_scoped_to_one_unit(self):
        profile = profiles.ERCF_VVD.derive(
            "mixed_manual_mcu",
            units=[profiles.ERCF_VVD.units[0].derive(
                syms={"BOARD_TYPE_MANUAL": True}),
                profiles.ERCF_VVD.units[1]])
        rendered = "\n".join(cfg.render(profile).values())
        mcus = [s for s in cfg.sections(rendered) if s.startswith("mcu ")]
        self.assertEqual(mcus, ["mcu unit1", "mcu unit1_buffer"])

    def test_per_gate_connection_fields_are_hidden(self):
        # The installer enables these expensive fields on its second parse.
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV, F_PER_GATE_MCU="y")):
            kc = cfg._kconfig("manual_gate_mcus", {
                "MMU_TYPE_EMU_1_0": True,
                "BOARD_TYPE_MANUAL": True,
            })
        self.assertTrue(kc.is_enabled("MMU_HAS_PER_GATE_MCU"))
        for gate in range(kc.getint("PARAM_NUM_GATES")):
            for prefix in ("MMU_SERIAL_DEVICE_OTHER_", "MMU_CANBUS_UUID_OTHER_",
                           "MMU_CANBUS_INTERFACE_OTHER_"):
                with self.subTest(gate=gate, prefix=prefix):
                    self.assertEqual(kc.syms[prefix + str(gate)].visibility, 0)


if __name__ == "__main__":
    unittest.main()
