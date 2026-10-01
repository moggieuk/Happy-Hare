import re
import logging

class Upgrades:

    def version_str(self, version):
        """(4, 9) -> '4.9'"""
        return f"{version[0]}.{version[1]}"

    def version_fn(self, version):
        """(4, 9) -> '4_9'"""
        return f"{version[0]}_{version[1]}"

    def parse_upgrade_fn(self, name):
        """
        'upgrade_4_9_to_4_10'
        -> [(4, 9), (4, 10)]
        """
        parts = name[len("upgrade_") :].split("_to_")
        versions = []
        for p in parts:
            major, minor = p.split("_")
            versions.append((int(major), int(minor)))
        return versions


    def upgrade(self, cfg, from_version, to_version):
        """Upgrade cfg as far as possible up to to_version."""

        if from_version >= to_version:
            return

        all_upgrades = sorted(
            [
                self.parse_upgrade_fn(f)
                for f in dir(self)
                if re.search(r"upgrade_\d+_\d+_to_\d+_\d+$", f)
            ],
            key=lambda u: u[1],
        )

        if not all_upgrades:
            return

        try:
            upgrade_path = next(
                upgrade
                for upgrade in all_upgrades
                if upgrade[0] == from_version and upgrade[1] <= to_version
            )

        except StopIteration:
            logging.info(
                "No further upgrade path available from %s toward %s",
                self.version_str(from_version),
                self.version_str(to_version),
            )
            return

        logging.info(
            "Upgrading from %s to %s",
            self.version_str(from_version),
            self.version_str(upgrade_path[1]),
        )

        upgrade_fn = "upgrade_{}_to_{}".format(
            self.version_fn(upgrade_path[0]),
            self.version_fn(upgrade_path[1]),
        )

        getattr(self, upgrade_fn)(cfg)

        self.upgrade(cfg, upgrade_path[1], to_version)


    HTLF_SELECTOR_ROTATION_DISTANCE = 360.0 # Selector distances are cam degrees

    def upgrade_4_0_to_4_1(self, cfg):
        self._upgrade_htlf_selector_to_degrees(cfg)

    def _upgrade_htlf_selector_to_degrees(self, cfg):
        """
        HTLF moved its selector to rotation_distance 360 with CAD geometry in cam degrees. A
        refresh keeps the old rotation_distance but renders the new degree-based CAD lines, so
        convert the existing mm-based selector settings to match.
        """
        for section in cfg.sections():
            if not section.startswith("mmu_unit "):
                continue
            if (cfg.get(section, "vendor") or "").strip().lower() != "htlf":
                continue

            unit = section[len("mmu_unit "):]
            stepper = "mmu_stepper %s" % (cfg.get(section, "selector_stepper") or "%s_selector" % unit)
            if not cfg.has_option(stepper, "rotation_distance"):
                continue

            old_rd = float(cfg.get(stepper, "rotation_distance"))
            if old_rd == self.HTLF_SELECTOR_ROTATION_DISTANCE:
                continue

            scale = self.HTLF_SELECTOR_ROTATION_DISTANCE / old_rd
            logging.warning(
                "HTLF unit '%s': selector rotation_distance changed from %s to %g (cam degrees). "
                "Re-run MMU_CALIBRATE_ROTARY_SELECTOR",
                unit, cfg.get(stepper, "rotation_distance"), self.HTLF_SELECTOR_ROTATION_DISTANCE,
            )
            cfg.set(stepper, "rotation_distance", "%g" % self.HTLF_SELECTOR_ROTATION_DISTANCE)

            params = "mmu_unit_parameters %s" % unit
            for option in ("selector_move_speed", "selector_homing_speed", "selector_accel"):
                if cfg.has_option(params, option):
                    cfg.set(params, option, "%g" % round(float(cfg.get(params, option)) * scale, 1))

            # Hand-set mm geometry predates descending cam support, so let the new defaults through
            for option in ("cad_gate0_pos", "cad_gate_width", "cad_selector_tolerance"):
                if cfg.has_option(params, option):
                    logging.warning("HTLF unit '%s': removing %s (now set by the cam angle)", unit, option)
                    cfg.remove_option(params, option)

# Upgrade from v3 is possible but difficult and requires a lot of testing. Therefore for
# now I'm going to assume at least a v4.0.0 starting point for sanity. In case I ever
# add upgrade from previous versions, the logic is mostly here:
#    def upgrade_2_70_to_2_71(self, cfg):
#        cfg = cfg.hhcfg
#
#        section = "gcode_macro _MMU_CUT_TIP_VARS"
#        cfg.rename_option(section, "variable_pin_park_x_dist", "variable_pin_park_dist")
#        cfg.rename_option(section, "variable_pin_loc_x_compressed", "variable_pin_loc_compressed")
#
#        section = "gcode_macro _MMU_SEQUENCE_VARS"
#        cfg.rename_option(section, "variable_lift_speed", "variable_park_lift_speed")
#
#        if cfg.has_option(section, "variable_park_xy"):
#            xy = cfg.get(section, "variable_park_xy")
#            z_hop_toolchange = cfg.get("mmu", "z_hop_height_toolchange", default=1)
#            z_hop_error = cfg.get("mmu", "z_hop_height_error", default=5)
#
#            cfg.set(section, "variable_park_toolchange", "{}, {}, 0, 2".format(xy, z_hop_toolchange))
#            cfg.set(section, "variable_park_pause", "{}, {}, 0, 2".format(xy, z_hop_error))
#            cfg.remove_option(section, "variable_park_xy")
#            cfg.remove_option("mmu", "z_hop_height_toolchange")
#            cfg.remove_option("mmu", "z_hop_height_error")
#
#        if cfg.has_option(section, "variable_enable_park"):
#            cfg.set(
#                section,
#                "variable_enable_park_printing",
#                "'toolchange,load,unload,pause,cancel'"
#                if cfg.getboolean(section, "variable_enable_park")
#                else "'toolchange,load,unload,runout,pause,cancel'"
#                if cfg.getboolean(section, "variable_enable_park_runout")
#                else "'pause,cancel'",
#            )
#            cfg.remove_option(section, "variable_enable_park")
#            cfg.remove_option(section, "variable_enable_park_runout")
#
#        if cfg.has_option(section, "variable_enable_park_standalone"):
#            cfg.set(
#                section,
#                "variable_enable_park_standalone",
#                "'toolchange,load,unload,pause,cancel'"
#                if cfg.getboolean(section, "variable_enable_park_standalone")
#                else "'pause,cancel'",
#            )
#
#    def upgrade_2_71_to_2_72(self, cfg):
#        if cfg.get("mmu", "toolhead_residual_filament") == "0" and cfg.get("mmu", "toolhead_ooze_reduction") != "0":
#            cfg.set("mmu", "toolhead_residual_filament", cfg.get("mmu", "toolhead_ooze_reduction"))
#            cfg.set("mmu", "toolhead_ooze_reduction", "0")
#
#    def upgrade_2_72_to_2_73(self, cfg):
#        section = "gcode_macro BLOBIFIER"
#        if cfg.has_option(section, "variable_iteration_z_raise"):
#            max_i_per_blob = cfg.getint(section, "variable_max_iterations_per_blob")
#            i_z_raise = cfg.getfloat(section, "variable_iteration_z_raise")
#            i_z_change = cfg.getfloat(section, "variable_iteration_z_change")
#            max_i_length = cfg.getfloat(section, "variable_max_iteration_length")
#
#            cfg.set(
#                section,
#                "variable_z_raise",
#                i_z_raise * max_i_per_blob - (max_i_per_blob * (max_i_per_blob - 1) / 2.0) * i_z_change,
#            )
#            cfg.set(section, "variable_purge_length_maximum", max_i_length * max_i_per_blob)
#            cfg.remove_option(section, "variable_max_iterations_per_blob")
#            cfg.remove_option(section, "variable_iteration_z_raise")
#            cfg.remove_option(section, "variable_iteration_z_change")
#            cfg.remove_option(section, "variable_max_iteration_length")
#
#    def upgrade_2_73_to_3_00(self, cfg):
#        if cfg.has_section("mmu_servo mmu_servo"):
#            cfg.rename_section("mmu_servo mmu_servo", "mmu_servo selector_servo")
#            if cfg.get("mmu_servo selector_servo", "pin") == "mmu:MMU_SERVO":
#                cfg.remove_option("mmu_servo selector_servo", "pin")  # Pin name has been changed, reset
#
#        if not cfg.has_section("mmu_machine"):
#            cfg.add_section("mmu_machine")
#        cfg.move_option("mmu", "mmu_num_gates", "mmu_machine", "num_gates")
#        cfg.move_option("mmu", "mmu_vendor", "mmu_machine")
#        cfg.move_option("mmu", "mmu_version", "mmu_machine")
#
#        cfg.rename_option("mmu", "auto_calibrate_gates", "autotune_rotation_distance")
#        cfg.rename_option("mmu", "auto_calibrate_bowden", "autotune_bowden_length")
#        cfg.rename_option("mmu", "endless_spool_final_eject", "gate_final_eject_distance")
#        cfg.rename_option("gcode_macro _MMU_SOFTWARE_VARS", "variable_eject_tool", "variable_unload_tool")
#        cfg.rename_option(
#            "gcode_macro _MMU_CLIENT_VARS", "variable_eject_tool_on_cancel", "variable_unload_tool_on_cancel"
#        )
#
#    def upgrade_3_0_to_3_1(self, cfg):
#        cfg.move_option("mmu", "homing_extruder", "mmu_machine")
#
#    def upgrade_3_1_to_3_2(self, cfg):
#        # change the dc espooler pins so they are easier to expand with the new script.
#        # e.g [output_pin _mmu_dc_espooler_rwd_0] pin = mmu:MMU_DC_MOT_1_A -> mmu:MMU_DC_MOT_0_A
#        for i in range(0, 12):
#            section = "output_pin _mmu_dc_espooler_rwd_" + str(i)
#            if cfg.has_section(section):
#                if cfg.get(section, "pin") == "mmu:MMU_DC_MOT_{}_A".format(i + 1):
#                    cfg.remove_option(section, "pin")  # Pin name has been changed, reset
#
#            section = "output_pin _mmu_dc_espooler_en_" + str(i)
#            if cfg.has_section(section):
#                if cfg.get(section, "pin") == "mmu:MMU_DC_MOT_{}_EN".format(i + 1):
#                    cfg.remove_option(section, "pin")  # Pin name has been changed, reset
#
#            aliases = cfg.get("board_pins mmu", "aliases")
#            if aliases:
#                aliases = aliases.replace("MMU_DC_MOT_{}_A".format(i + 1), "MMU_DC_MOT_{}_A".format(i))
#                aliases = aliases.replace("MMU_DC_MOT_{}_B".format(i + 1), "MMU_DC_MOT_{}_B".format(i))
#                aliases = aliases.replace("MMU_DC_MOT_{}_EN".format(i + 1), "MMU_DC_MOT_{}_EN".format(i))
#                cfg.set("board_pins mmu", "aliases", aliases)
#
#    def upgrade_3_2_to_3_4(self, cfg):
#        cfg.rename_option("mmu", "sync_feedback_enable", "sync_feedback_enabled")
#        cfg.rename_option("mmu", "selector_touch_enable", "selector_touch_enabled")
#        cfg.rename_option("mmu", "endless_spool_enable", "endless_spool_enabled")
#        # This upgrade step may be incomplete
