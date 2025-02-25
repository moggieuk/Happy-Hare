import re
import logging


class Upgrades:
    def upgrade(self, cfg_input, from_version, to_version):
        """Will recursively upgrade cfg_input from from_version to to_version"""
        if from_version == to_version:
            return

        all_upgrades = [
            [float(v.replace("_", ".")) for v in f[len("upgrade_") :].split("_to_")]
            for f in dir(self)
            if re.search(r"upgrade_\d+_\d+_to_\d+_\d+", f)
        ]

        if len(all_upgrades) == 0:
            return

        try:
            upgrade_path = next(upgrade for upgrade in all_upgrades if upgrade[1] > float(from_version))
        except ValueError:
            lowest_from_version = min([upgrade[0] for upgrade in all_upgrades if upgrade[0] > float(from_version)])
            lowest_from_version = f"{lowest_from_version:.2f}"
            logging.error(
                f"Upgrade path from {from_version} to {to_version} is not supported, try upgrading to {lowest_from_version} first"
            )
            exit(1)

        upgrade_path = [f"{v:.2f}" for v in upgrade_path]
        logging.debug(f"Upgrading from {from_version} to {upgrade_path[1]}")
        getattr(self, f"upgrade_{upgrade_path[0].replace('.', '_')}_to_{upgrade_path[1].replace('.', '_')}")(cfg_input)
        cfg_input.hhcfg.set("mmu", "happy_hare_version", upgrade_path[1])
        self.upgrade(cfg_input, upgrade_path[1], to_version)

    def upgrade_2_70_to_2_71(self, cfg_input):
        cfg = cfg_input.hhcfg

        section = "gcode_macro _MMU_CUT_TIP_VARS"
        cfg.rename_option(section, "variable_pin_park_x_dist", "variable_pin_park_dist")
        cfg.rename_option(section, "variable_pin_loc_x_compressed", "variable_pin_loc_compressed")

        section = "gcode_macro _MMU_SEQUENCE_VARS"
        cfg.rename_option(section, "variable_lift_speed", "variable_park_lift_speed")

        if cfg.has_option(section, "variable_park_xy"):
            xy = cfg.get(section, "variable_park_xy")
            z_hop_toolchange = cfg.get("mmu", "z_hop_height_toolchange", default=1)
            z_hop_error = cfg.get("mmu", "z_hop_height_error", default=5)

            cfg.set(section, "variable_park_toolchange", f"{xy}, {z_hop_toolchange}, 0, 2")
            cfg.set(section, "variable_park_pause", f"{xy}, {z_hop_error}, 0, 2")
            cfg.remove_option(section, "variable_park_xy")
            cfg.remove_option("mmu", "z_hop_height_toolchange")
            cfg.remove_option("mmu", "z_hop_height_error")

        if cfg.has_option(section, "variable_enable_park"):
            cfg.set(
                section,
                "variable_enable_park_printing",
                "'toolchange,load,unload,pause,cancel'"
                if cfg.getboolean(section, "variable_enable_park")
                else "'toolchange,load,unload,runout,pause,cancel'"
                if cfg.getboolean(section, "variable_enable_park_runout")
                else "'pause,cancel'",
            )
            cfg.remove_option(section, "variable_enable_park")
            cfg.remove_option(section, "variable_enable_park_runout")

        if cfg.has_option(section, "variable_enable_park_standalone"):
            cfg.set(
                section,
                "variable_enable_park_standalone",
                "'toolchange,load,unload,pause,cancel'"
                if cfg.getboolean(section, "variable_enable_park_standalone")
                else "'pause,cancel'",
            )

    def upgrade_2_71_to_2_72(self, cfg_input):
        cfg = cfg_input.hhcfg

        if cfg.get("mmu", "toolhead_residual_filament") == "0" and cfg.get("mmu", "toolhead_ooze_reduction") != "0":
            cfg.set("mmu", "toolhead_residual_filament", cfg.get("mmu", "toolhead_ooze_reduction"))
            cfg.set("mmu", "toolhead_ooze_reduction", "0")

    def upgrade_2_72_to_2_73(self, cfg_input):
        cfg = cfg_input.hhcfg

        section = "gcode_macro BLOBIFIER"
        if cfg.has_option(section, "variable_iteration_z_raise"):
            max_i_per_blob = cfg.getint(section, "variable_max_iterations_per_blob")
            i_z_raise = cfg.getfloat(section, "variable_iteration_z_raise")
            i_z_change = cfg.getfloat(section, "variable_iteration_z_change")
            max_i_length = cfg.getfloat(section, "variable_max_iteration_length")

            cfg.set(
                section,
                "variable_z_raise",
                i_z_raise * max_i_per_blob - (max_i_per_blob * (max_i_per_blob - 1) / 2.0) * i_z_change,
            )
            cfg.set(section, "variable_purge_length_maximum", max_i_length * max_i_per_blob)
            cfg.remove_option(section, "variable_max_iterations_per_blob")
            cfg.remove_option(section, "variable_iteration_z_raise")
            cfg.remove_option(section, "variable_iteration_z_change")
            cfg.remove_option(section, "variable_max_iteration_length")

    def upgrade_2_73_to_3_00(self, cfg_input):
        cfg = cfg_input.hhcfg

        if cfg.has_section("mmu_servo mmu_servo"):
            cfg.rename_section("mmu_servo mmu_servo", "mmu_servo selector_servo")
            if cfg.get("mmu_servo selector_servo", "pin") == "mmu:MMU_SERVO":
                cfg.remove_option("mmu_servo selector_servo", "pin")  # Pin name has been changed, reset

        if not cfg.has_section("mmu_machine"):
            cfg.add_section("mmu_machine")
        cfg.move_option("mmu", "mmu_num_gates", "mmu_machine", "num_gates")
        cfg.move_option("mmu", "mmu_vendor", "mmu_machine")
        cfg.move_option("mmu", "mmu_version", "mmu_machine")

        cfg.rename_option("mmu", "auto_calibrate_gates", "autotune_rotation_distance")
        cfg.rename_option("mmu", "auto_calibrate_bowden", "autotune_bowden_length")
        cfg.rename_option("mmu", "endless_spool_final_eject", "gate_final_eject_distance")
        cfg.rename_option("gcode_macro _MMU_SOFTWARE_VARS", "variable_eject_tool", "variable_unload_tool")
        cfg.rename_option(
            "gcode_macro _MMU_CLIENT_VARS", "variable_eject_tool_on_cancel", "variable_unload_tool_on_cancel"
        )

    def upgrade_3_00_to_3_10(self, cfg_input):
        cfg_input.hhcfg.move_option("mmu", "homing_extruder", "mmu_machine")
