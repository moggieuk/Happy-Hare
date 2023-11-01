    def _setup_mmu_hardware(self, config):
        self._log_debug("MMU Hardware Initialization -------------------------------")
        self.mmu_hardware = self.printer.lookup_object('mmu_hardware', None)

        # Selector h/w setup ------
        for manual_stepper in self.printer.lookup_objects('manual_mh_stepper'):
            stepper_name = manual_stepper[1].get_steppers()[0].get_name()
            if stepper_name == 'manual_mh_stepper selector_stepper':
                self.selector_stepper = manual_stepper[1]
        if self.selector_stepper is None:
            raise self.config.error("Missing [manual_mh_stepper selector_stepper] section in mmu_hardware.cfg")

        # Find the pyhysical (homing) selector endstop
        self.selector_endstop = self.selector_stepper.get_endstop(self.SELECTOR_HOME_ENDSTOP)
        if self.selector_endstop is None:
            for name in self.selector_stepper.get_endstop_names():
                if not self.selector_stepper.is_endstop_virtual(name):
                    self.selector_endstop = self.selector_stepper.get_endstop(name)
                    break
        if self.selector_endstop is None:
            raise self.config.error("Physical homing endstop not found for selector_stepper")

        # If user didn't configure a default endstop do it here. Mimimizes config differences
        if 'default' not in self.selector_stepper.get_endstop_names():
            self.selector_stepper.activate_endstop(self.SELECTOR_HOME_ENDSTOP)
        self.selector_touch = self.SELECTOR_TOUCH_ENDSTOP in self.selector_stepper.get_endstop_names() and self.selector_touch_enable

        # Gear h/w setup ------
        for manual_stepper in self.printer.lookup_objects('manual_extruder_stepper'):
            stepper_name = manual_stepper[1].get_steppers()[0].get_name()
            if stepper_name == 'manual_extruder_stepper gear_stepper':
                self.gear_stepper = manual_stepper[1]
            if stepper_name == "manual_extruder_stepper extruder":
                self.mmu_extruder_stepper = manual_stepper[1]
        if self.gear_stepper is None:
            raise self.config.error("Missing [manual_extruder_stepper gear_stepper] definition in mmu_hardware.cfg\n%s" % self.UPGRADE_REMINDER)
        if self.mmu_extruder_stepper is None:
            raise self.config.error("Missing [manual_extruder_stepper extruder] definition in mmu_hardware.cfg\n%s" % self.UPGRADE_REMINDER)

        # Configure sensor options:
        # "toolhead" - assumed to be after extruder gears inside of extruder
        #              Must be done first and gear/extruder endstops synced afterwards
        # "gate" - shared homing point for parking at the gate
        # "extruder" - assumed to be just prior to extruder gears
        for name in ["toolhead", "gate", "extruder"]:
            sensor = self.printer.lookup_object("filament_switch_sensor %s_sensor" % name, None)
            if sensor is not None:
                self.sensors[name] = sensor
                # With MMU this must not pause nor call user defined macros
                self.sensors[name].runout_helper.runout_pause = False
                self.sensors[name].runout_helper.runout_gcode = None
                self.sensors[name].runout_helper.insert_gcode = None
                sensor_pin = self.config.getsection("filament_switch_sensor %s_sensor" % name).get("switch_pin")
    
                # Add sensor pin as an extra endstop for gear_stepper
                ppins = self.printer.lookup_object('pins')
                pin_params = ppins.parse_pin(sensor_pin, True, True)
                share_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])
                ppins.allow_multi_use_pin(share_name)
                mcu_endstop = self.gear_stepper._add_endstop(sensor_pin, "mmu_gear_%s" % name)

                if name == "toolhead":
                    # Finally we might want to home the extruder to toolhead sensor in isolation
                    self.mmu_extruder_stepper._add_endstop(sensor_pin, "mmu_toolhead", register=False)

            if name == "toolhead":
                # After adding just toolhead, to allow for homing moves on extruder synced with gear
                # and gear synced with extruder we need each to share each others endstops
                for en in self.mmu_extruder_stepper.get_endstop_names():
                    mcu_es = self.mmu_extruder_stepper.get_endstop(en)
                    for s in self.gear_stepper.steppers:
                        mcu_es.add_stepper(s)
                for en in self.gear_stepper.get_endstop_names():
                    mcu_es = self.gear_stepper.get_endstop(en)
                    for s in self.mmu_extruder_stepper.steppers:
                        mcu_es.add_stepper(s)

        # Get servo and (optional) encoder setup -----
        self.servo = self.printer.lookup_object('mmu_servo mmu_servo', None)
        if not self.servo:
            raise self.config.error("Missing [mmu_servo] definition in mmu_hardware.cfg\n%s" % self.UPGRADE_REMINDER)
        self.encoder_sensor = self.printer.lookup_object('mmu_encoder mmu_encoder', None)
        if not self.encoder_sensor:
            # MMU logging not set up so use main klippy logger
            logging.warn("No [mmu_encoder] definition found in mmu_hardware.cfg. Assuming encoder is not available")

    def handle_connect(self):
        self._setup_logging()
        self.toolhead = self.printer.lookup_object('toolhead')

        # See if we have a TMC controller capable of current control for filament collision detection and syncing
        # on gear_stepper and tip forming on extruder
        self.selector_tmc = self.gear_tmc = self.extruder_tmc = None
        tmc_chips = ["tmc2209", "tmc2130", "tmc2208", "tmc2660", "tmc5160", "tmc2240"]
        for chip in tmc_chips:
            if self.selector_tmc is None:
                self.selector_tmc = self.printer.lookup_object('%s manual_mh_stepper selector_stepper' % chip, None)
                if self.selector_tmc is not None:
                    self._log_debug("Found %s on selector_stepper. Stallguard 'touch' homing possible." % chip)
            if self.gear_tmc is None:
                self.gear_tmc = self.printer.lookup_object('%s manual_extruder_stepper gear_stepper' % chip, None)
                if self.gear_tmc is not None:
                    self._log_debug("Found %s on gear_stepper. Current control enabled. Stallguard 'touch' homing possible." % chip)
            if self.extruder_tmc is None:
                self.extruder_tmc = self.printer.lookup_object("%s manual_extruder_stepper %s" % (chip, self.extruder_name), None)
                if self.extruder_tmc is not None:
                    self._log_debug("Found %s on extruder. Current control enabled" % chip)

        if self.selector_tmc is None:
            self._log_debug("TMC driver not found for selector_stepper, cannot use sensorless homing and recovery")
        if self.extruder_tmc is None:
            self._log_debug("TMC driver not found for extruder, cannot use current increase for tip forming move")
        if self.gear_tmc is None:
            self._log_debug("TMC driver not found for gear_stepper, cannot use current reduction for collision detection or while synchronized printing")
