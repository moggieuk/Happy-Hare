# Happy Hare test harness - a trimmed replica of Klipper's Printer._read_config /
# _connect, plus the event driver that gets from a parsed config to a live MMU.
#
# The ORDER BELOW IS NOT NEGOTIABLE - it mirrors klippy.py, and several HH designs
# depend on the exact sequencing (notably MmuExtruderWrapper, which strips
# [extruder]'s stepper options during the section loop and restores them at
# klippy:connect).
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os, shutil, logging, tempfile, contextlib

from . import cfg as cfg_mod
from . import profiles as profiles_mod
from . import selector as selector_mod
from .root import install

# extras/mmu/mmu_constants.py:69 - bootup is scheduled this far out
BOOT_DELAY = 2.5

# What a user's real printer.cfg provides and the HH templates deliberately do not.
#
# [extruder] needs its STEPPER options here: the shipped mmu_macro_vars.cfg [extruder]
# carries only the extrude limits, but MmuExtruderWrapper builds an MmuExtruderStepper
# straight off config.getsection('extruder') (mmu_extruder_wrapper.py:58-59).
#
# [tmc2209 extruder] is MANDATORY - MmuExtruderWrapper.__init__ raises
# "Extruder 'extruder' TMC configuration not found" without a <chip> extruder section
# (mmu_extruder_wrapper.py:44-55).
PRINTER_STUB = """
[mcu]
serial: /dev/null

[printer]
kinematics: none
max_velocity: 300
max_accel: 3000

[idle_timeout]
timeout: 600

[extruder]
step_pin: mcu:PA1
dir_pin: mcu:PA2
enable_pin: !mcu:PA3
microsteps: 16
full_steps_per_rotation: 200
rotation_distance: 22.0
nozzle_diameter: 0.400
filament_diameter: 1.750
heater_pin: mcu:PA4
sensor_type: EPCOS 100K B57560G104F
sensor_pin: mcu:PA5
control: pid
pid_Kp: 22.2
pid_Ki: 1.08
pid_Kd: 114
min_temp: 0
max_temp: 300

[tmc2209 extruder]
uart_pin: mcu:PA6
run_current: 0.5

# A cabinet-wide LED chain living in the USER's printer.cfg rather than in any Happy Hare
# template. cfg.assemble() only ever sees HH's own files plus this stub, so a profile whose
# exit_leds/status_leds point at an external chain (ERCF on ERB, for instance:
# 'neopixel:cabinet_leds (1-9)') has nothing to resolve against.
#
# It fails LATE and confusingly without this: configfile.py:148-151 hands back a wrapper
# for a missing section and neopixel.py:20 defaults chain_count to 1, so load_object
# SUCCEEDS with a 1-LED chain and the error surfaces later as
# "MMU LED (with index 2) on segment exit isn't available" (mmu/unit/mmu_leds.py:107-110).
#
# 12 is the smallest count that covers the shipped (1-9) exit range plus a (11) status LED.
# Note mmu_leds.py:102-103 additionally requires num_leds % num_gates == 0, so a profile
# using this chain must have a gate count that divides its exit range.
[neopixel cabinet_leds]
pin: mcu:PA10
chain_count: 12
"""


@contextlib.contextmanager
def _nullcontext():
    yield


class Session:
    """
    One harness session: a fake klippy tree, a rendered config, a Printer, and the
    machinery to walk it to mmu:bootup.

    Use as a context manager so teardown always runs - MmuLogger installs an atexit
    handler and a background QueueListener thread (extras/mmu/mmu_logger.py:76-78)
    that would otherwise leak per test.
    """

    def __init__(self, profile='boxturtle', adc_api='new', adc_payload='samples',
                 strict_gcode=False, printer_stub=PRINTER_STUB, virtual_nfc=False,
                 log_dir=None, klipper_aio=True):
        self.klippy = install()
        self.profile = (profile if isinstance(profile, profiles_mod.Profile)
                        else profiles_mod.get(profile))
        self.adc_api = adc_api
        self.adc_payload = adc_payload
        # Which klipper generation to emulate for save_variables, the same kind of
        # version switch as adc_api. True models klipper >= 332fbf236 (2026-03-21),
        # where SAVE_VARIABLE goes through aio_executor and PAUSES the calling
        # greenlet, and where the klippy:ready dispatch loop runs inside
        # reactor.assert_no_pause(). False models everything before that: a plain
        # synchronous write and no pause guard. Defaults to the modern behaviour.
        self.klipper_aio = klipper_aio
        self.strict_gcode = strict_gcode
        self.printer_stub = printer_stub
        # Swap reader chips for model-driven virtual ones instead of scripting the real
        # RC522 init. Needed for anything that asks a reader for a UID (MMU_NFC READ,
        # MMU_NFC_SCAN, the preload NFC compound).
        self.virtual_nfc = virtual_nfc
        self.nfc_chips = {}
        self.tmpdir = tempfile.mkdtemp(prefix='hh-session-')
        # Where MmuLogger's mmu.log lands. Defaults to the session tmpdir, which is deleted
        # by close() - right for tests, useless for anything that wants to read the log
        # afterwards. test/console.py passes a real directory to keep it.
        self.log_dir = log_dir
        self.printer = None
        self.reactor = None
        self.config = None
        self.fileconfig = None
        self._booted = False

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self):
        self.build()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        try:
            mmu = self.printer.lookup_object('mmu', None) if self.printer else None
            logger = getattr(mmu, 'logger', None) if mmu else None
            if logger is not None and hasattr(logger, 'shutdown'):
                logger.shutdown()
        except Exception:
            logging.debug('logger shutdown failed', exc_info=True)
        if self.reactor is not None:
            self.reactor.finalize()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- step 1: session objects -------------------------------------------
    def build(self):
        import klippy
        import reactor as reactor_mod
        import configfile
        import gcode as gcode_mod
        import webhooks as webhooks_mod

        self.reactor = reactor_mod.VirtualReactor(start_time=1000.)
        # start_args['log_file'] MUST point into the session temp dir: MmuLogger opens
        # dirname(log_file)/mmu.log with a TimedRotatingFileHandler
        # (extras/mmu/mmu_logger.py:39-53) and MmuSyncFeedback writes
        # sync_<gate>.jsonl beside it (mmu_sync_feedback.py:366). Left empty, both
        # fall back to /tmp and collide across parallel runs.
        log_root = self.log_dir or self.tmpdir
        if self.log_dir:
            os.makedirs(self.log_dir, exist_ok=True)
        start_args = {
            'log_file': os.path.join(log_root, 'klippy.log'),
            'config_file': os.path.join(self.tmpdir, 'printer.cfg'),
            'software_version': 'v0.13.0-harness',
            'apiserver': None,
            'debuginput': None,
        }
        self.printer = printer = klippy.Printer(start_args, self.reactor)
        # Read by the save_variables stub, which is built by the section loop below and
        # so cannot be handed the mode directly the way ppins.adc_api is.
        printer.harness_klipper_aio = self.klipper_aio
        gcode_mod.add_early_printer_objects(printer)
        webhooks_mod.add_early_printer_objects(printer)

        # -- step 2: config -------------------------------------------------
        rendered = cfg_mod.render(self.profile)
        cfg_mod.assert_sane(rendered)
        self.fileconfig = cfg_mod.assemble(rendered, printer_stub=self.printer_stub)
        # Must be applied AFTER assembly, not via the stub: the shipped
        # mmu_macro_vars.cfg carries its own [save_variables] and is read later, so a
        # stub value would be silently overridden by the real
        # ~/printer_data/config/mmu/mmu_vars.cfg path.
        self.fileconfig.set('save_variables', 'filename', self._mmu_vars_copy())
        pconfig = configfile.PrinterConfig(printer)
        printer.add_object('configfile', pconfig)
        self.config = configfile.ConfigWrapper(printer, self.fileconfig, {}, '')

        # -- step 3: pins + mcus --------------------------------------------
        import pins as pins_mod
        import mcu as mcu_mod
        pins_mod.add_printer_objects(self.config)
        ppins = printer.lookup_object('pins')
        ppins.adc_api = self.adc_api
        ppins.adc_payload = self.adc_payload
        # One MCU + pin chip per [mcu] / [mcu name], unnamed one as 'mcu'
        reactor = self.reactor
        printer.add_object('mcu', mcu_mod.MCU('mcu', reactor))
        ppins.register_mcu_chip('mcu', printer.lookup_object('mcu'))
        for section in self.fileconfig.sections():
            if section.startswith('mcu '):
                name = section.split()[-1]
                obj = mcu_mod.MCU(name, reactor)
                printer.add_object(section, obj)
                ppins.register_mcu_chip(name, obj)

        if self.strict_gcode:
            printer.lookup_object('gcode').strict = True

        # -- step 4: the generic section loop -------------------------------
        # This is where everything happens: [mmu_machine] builds the entire MMU tree.
        # Default of None matters - HH's own manually-parsed sections
        # ([mmu_unit], [mmu_sensors], [mmu_parameters], ...) have no load_config
        # module and must be skipped, exactly as in Klipper.
        for section_config in self.config.get_prefix_sections(''):
            section = section_config.get_name()
            if section not in printer.objects:
                printer.load_object(self.config, section, None)

        # Both of these MUST happen before the readers are initialised: they are
        # constructed during the section loop above, but the chip is only talked to at
        # init, and a failed init is never retried - so doing either later leaves every
        # reader dead for the whole session. Init used to be at klippy:connect; it is now
        # MmuNfcManager's delayed post-bootup pass (Session._settle_nfc_init), so this is
        # comfortably early rather than only-just early.
        if self.virtual_nfc:
            self.virtualise_nfc_readers()
        else:
            self.prime_nfc_readers()
        return self

    def virtualise_nfc_readers(self):
        """Replace each reader's chip driver with a model-driven VirtualNfcChip."""
        from . import nfc_fixtures
        self.nfc_chips = nfc_fixtures.virtualise(self.printer, self.filament())
        return self.nfc_chips

    def chip(self, name_or_gate):
        """A virtual chip by reader name, or by gate index for a per-gate reader."""
        if name_or_gate in self.nfc_chips:
            return self.nfc_chips[name_or_gate]
        for chip in self.nfc_chips.values():
            if chip._gate == name_or_gate:
                return chip
        raise KeyError('no virtual NFC chip for %r; have: %s'
                       % (name_or_gate, ', '.join(sorted(self.nfc_chips))))

    def _mmu_vars_copy(self):
        """
        A per-session writable copy of config/mmu_vars.cfg. It must genuinely contain
        `mmu__revision`: SaveVariableManager reads it BEFORE seeding a default and
        raises "mmu_vars.cfg not found" if the original lookup came back None
        (extras/mmu/mmu_utils.py:71-84), so an empty or missing file is not enough.
        """
        src = os.path.join(cfg_mod.REPO_ROOT, 'config', 'mmu_vars.cfg')
        dest = os.path.join(self.tmpdir, 'mmu_vars.cfg')
        shutil.copyfile(src, dest)
        return dest

    # -- steps 5-8 ---------------------------------------------------------
    def connect(self):
        """Step 5 (toolhead phase) + step 6 (klippy:connect)."""
        import toolhead as toolhead_mod
        from kinematics import extruder as extruder_mod
        toolhead_mod.add_printer_objects(self.config)
        extruder_mod.add_printer_objects(self.config)
        self.printer.send_event('klippy:connect')
        self.apply_initial_sensor_states()
        return self

    # -- sensors -----------------------------------------------------------
    def sensors(self):
        """{name: MmuSwitchSensor} keyed by HH's qualified name (e.g. 'unit0:mmu_gate')."""
        return dict(self.mmu.sensor_manager.all_sensors_map)

    def sensor(self, name):
        """
        Handle for driving one sensor. Accepts a qualified name ('unit0:mmu_gate') or
        a bare one ('mmu_gate', 'filament_tension') when unambiguous.
        """
        all_sensors = self.sensors()
        if name in all_sensors:
            return _SensorHandle(self, name, all_sensors[name])
        matches = [k for k in all_sensors if k.split(':')[-1] == name]
        if len(matches) == 1:
            return _SensorHandle(self, matches[0], all_sensors[matches[0]])
        if not matches:
            raise KeyError("no sensor %r; known: %s"
                           % (name, ', '.join(sorted(all_sensors))))
        raise KeyError("sensor %r is ambiguous across units: %s"
                       % (name, ', '.join(sorted(matches))))

    def apply_initial_sensor_states(self):
        """
        Establish a physically coherent "machine powered on, no filament loaded"
        state. Real Klipper reports every button's initial state when the MCU
        connects; our fake buttons only fire when driven, so without this every
        switch sits at its constructed default and HH infers nonsense.

        Concretely: BoxTurtle's buffer declares `buffer_spring_state: tension`, so at
        rest the spring pulls the TENSION switch closed. Leaving both tension and
        compression open reads as NEUTRAL, which differs from the configured resting
        state, and check_filament_in_mmu concludes "buffer is not in resting position
        thus filament must be present" (extras/mmu/mmu_filament_movement.py:2987-2992).
        recover_filament_pos then infers FILAMENT_POS_IN_BOWDEN and bootup reports
        "Filament not detected as either unloaded or fully loaded". That is HH
        behaving correctly on an incoherent machine - the harness has to supply the
        coherent one.
        """
        from extras.mmu.mmu_constants import SENSOR_TENSION, SENSOR_COMPRESSION
        resting = {'tension': SENSOR_TENSION, 'compression': SENSOR_COMPRESSION}
        at_rest = set()
        spring_states = []
        for unit in self.mmu.mmu_machine.units:
            buffer = getattr(unit, 'buffer', None)
            if buffer is None:
                continue
            spring = getattr(buffer, 'buffer_spring_state', 'none')
            spring_states.append(spring)
            sensor_name = resting.get(spring)
            if sensor_name is not None:
                at_rest.add(sensor_name)

        # A PROPORTIONAL (analog) buffer derives its compression/tension sensors from the
        # ADC reading, so the resting state must be expressed as a RAW VALUE and left to
        # derive. Forcing the virtual sensors directly instead leaves them stuck: the
        # proportional sensor only re-evaluates them on a threshold crossing, so a
        # subsequently-fed neutral reading does not clear a hand-set tension flag - which
        # showed up as EMU reading "tension" at a normalised value of 0.0.
        derived = set()
        for name, sensor in self.sensors().items():
            handle = _SensorHandle(self, name, sensor)
            if handle.kind != 'proportional':
                continue
            spring = spring_states[0] if spring_states else 'none'
            handle.feed(self._resting_raw(handle, spring), settle=False)
            # Exclude the analog sensor ITSELF as well as the two it derives: the loop
            # below would otherwise call set(False) on it, which for a proportional sensor
            # means feeding neutral - overwriting the resting value just fed.
            derived |= {SENSOR_TENSION, SENSOR_COMPRESSION, name.split(':')[-1]}

        for name, sensor in self.sensors().items():
            bare = name.split(':')[-1]
            if bare in derived:
                continue        # derived from the analog reading above
            try:
                _SensorHandle(self, name, sensor).set(bare in at_rest)
            except AssertionError:
                logging.debug('cannot drive sensor %s directly', name)
        self.reactor.advance(0.)
        self._spring_at_rest = at_rest
        return self

    @staticmethod
    def _resting_raw(handle, spring_state):
        """Raw ADC reading for a proportional buffer's configured resting spring state."""
        sensor = handle.sensor
        neutral = handle.neutral_value()
        if spring_state == 'tension':
            return neutral - getattr(sensor, '_d_neg', 0.5)
        if spring_state == 'compression':
            return neutral + getattr(sensor, '_d_pos', 0.5)
        return neutral

    # -- filament path model -----------------------------------------------
    def filament(self, layout=None):
        """
        The 1-D filament path model for this machine, created on first use and
        published as printer.harness_filament so the fake HomingMove can find it.

        Sensors it does not own are left alone: the buffer spring sensors are held at
        their configured resting state by apply_initial_sensor_states, and a filament
        model that also drove them would fight it.
        """
        existing = getattr(self.printer, 'harness_filament', None)
        if existing is not None and layout is None:
            return existing

        from .filament import FilamentPath
        model = FilamentPath(self.mmu.num_gates, layout=layout)
        owned = [name for name in self.sensors()
                 if name.split(':')[-1] not in getattr(self, '_spring_at_rest', set())
                 and model.position(name) is not None]
        # Which gates each unit owns, so a unit-qualified sensor is not answered from another
        # unit's filament - see FilamentPath.gates_visible_to.
        model.units = {u.name: (u.first_gate, u.num_gates)
                       for u in self.mmu.mmu_machine.units}
        model.bind(owned, self._set_sensor_state)
        self.printer.harness_filament = model
        self._install_move_observer()
        if self._encoders():
            model.observers.append(self._on_encoder_travel)
        return model

    def install_macro_effects(self):
        """
        Give the shipped macros whose MOTION Happy Hare measures a real effect.

        Macro bodies do not run in the harness (see klippy_root/extras/gcode_macro.py), which
        is fine for choreography - a recorded call is all a park or a purge needs to be
        asserted. Tip forming is different: HH brackets the macro with encoder and extruder-step
        readings and refuses the unload if nothing moved
        (mmu_filament_movement.py:2477-2497, :2559-2568):

            "No encoder movement: Concluding filament is stuck in extruder"

        So on a machine WITH AN ENCODER a no-op tip form reads as a jam and MMU_UNLOAD always
        fails. BoxTurtle never showed this because can_use_encoder() is False there.
        """
        effects = getattr(self.printer, 'harness_macro_effects', None)
        if effects is None:
            effects = self.printer.harness_macro_effects = {}
        effects.setdefault('_MMU_FORM_TIP', self._effect_form_tip)
        return effects

    def _effect_form_tip(self, macro, gcmd):
        """
        Retract the extruder as the shipped _MMU_FORM_TIP would, and move the filament with it.

        DISTANCE is cooling_tube_position + cooling_tube_length, which the shipped config itself
        describes as "the top of the heater" (config/base/mmu_macro_vars.cfg:476) - i.e. how far
        back the tip ends up. Both are real values read from the machine's own config, so this
        tracks the machine rather than being a number chosen here. It approximates the shipped
        macro's NET retraction; it does not reimplement its ramming/cooling/skinnydip sequence.

        The variables live on _MMU_FORM_TIP_VARS, not on _MMU_FORM_TIP itself, with a fallback
        to the calling macro so a machine that keeps them elsewhere still works.

        WHAT MATTERS IS CONSISTENCY, not matching any particular printer. HH derives park_pos
        from the extruder movement it observes (park_pos = stepper_movement +
        residual_filament + toolchange_retract), exactly as it would on real hardware - so as
        long as the extruder and the filament model move by the SAME amount, HH's conclusion is
        self-consistent with the machine the harness is presenting.

        Both halves are done explicitly rather than through a trapq append, because the model's
        move observer is filtered to the gear stepper and tip forming is an extruder move; see
        the CANDIDATE IMPROVEMENT note on _on_manual_move.
        """
        vars_macro = self.printer.lookup_object('gcode_macro %s_VARS' % macro.alias, None)
        variables = dict(getattr(macro, 'variables', {}) or {})
        variables.update(getattr(vars_macro, 'variables', {}) or {})
        distance = (float(variables.get('cooling_tube_position', 0.0) or 0.0)
                    + float(variables.get('cooling_tube_length', 0.0) or 0.0))
        if distance <= 0:
            logging.debug('harness: no tip-forming distance for %s; leaving it a no-op',
                          macro.alias)
            return

        toolhead = self.printer.lookup_object('toolhead', None)
        extruder = toolhead.get_extruder() if toolhead is not None else None
        stepper = getattr(getattr(extruder, 'extruder_stepper', None), 'stepper', None)
        if stepper is not None:
            # Retract: HH reads (initial_mcu_pos - final_mcu_pos) * step_dist
            # (mmu_filament_movement.py:2541-2559), so BOTH have to move. set_position alone
            # is not enough - it is mcu-preserving, by design (klippy_root/stepper.py).
            stepper.set_position([stepper.get_commanded_position() - distance, 0., 0., 0.])
            stepper.harness_note_motion(-distance)

        model = getattr(self.printer, 'harness_filament', None)
        gate = self.mmu.gate_selected
        if model is not None and gate is not None and gate >= 0:
            model.advance(gate, -distance, 'tip forming')

    def calibrate(self):
        """
        Seed the calibration a PHYSICAL-selector machine needs before it can select a gate and
        load. Returns {unit name: {what was seeded}}.

        Not called from a bare boot(), deliberately: uncalibrated is a real state HH has to
        cope with, tradrack's existing tests assert exactly that, and quietly calibrating
        everything would erase the distinction. Pass boot(calibrate=True), or call this
        explicitly, to get a calibrated machine.

        WORKS IN EITHER PHASE. Called BEFORE klippy:ready (which is what boot(calibrate=True)
        does) it only writes the variables, and Happy Hare's own handle_ready then loads them
        and marks itself calibrated - so the "not found in mmu_vars.cfg" warnings never fire.
        Called after ready, as tests do, it additionally applies the values in memory and
        marks calibrated itself, because handle_ready has already been and gone.

        Two steps, both only where the machine actually requires them:

        SELECTOR OFFSETS, from HH's own published quick-method formula (see
        SelectorAxis.nominal_gate_offsets), so no geometry is invented here. Skipped for a
        self-calibrating selector - IndexedSelector marks itself at handle_ready.

        BOWDEN LENGTH, but only for a unit with require_bowden_move (Type-A designs with a
        shared gear); a BoxTurtle-style unit is marked calibrated automatically
        (mmu_calibrator.py:91-93), which is why no existing test needed this. The length comes
        from the HARNESS'S OWN filament geometry rather than from config: the model places the
        gate at 0 and the extruder entry at layout['extruder_entry'], so that distance IS the
        bowden length here, and seeding anything else would leave HH's idea of the machine
        disagreeing with the machine.

        Why seeding rather than running HH's MMU_CALIBRATE_SELECTOR AUTO=1: speed, and because
        every test that wants a working machine would otherwise pay for a full calibration run.
        Auto-calibration DOES work now (test_mmu_selector.py exercises it) - drive it directly,
        or use the console's --no-calibrate, when the calibration flow is what you are testing.
        """
        from extras.mmu.mmu_constants import (CALIBRATED_ENCODER, CALIBRATED_SELECTOR,
                                              VARS_MMU_BOWDEN_LENGTHS,
                                              VARS_MMU_ENCODER_RESOLUTION,
                                              VARS_MMU_GEAR_ROTATION_DISTANCES,
                                              VARS_MMU_SELECTOR_OFFSETS)

        model = self.filament()
        # Every unit, not just the ones with a physical selector: bowden, gear and encoder
        # seeding apply to a VirtualSelector machine too, and skipping them left BoxTurtle
        # booting with "Calibration steps are not complete" for no reason.
        axes = {axis.unit.name: axis
                for axis in (getattr(self.printer, 'harness_selectors', None) or ())}
        applied = {}
        for unit in self.mmu.mmu_machine.units:
            axis = axes.get(unit.name)
            done = {}

            offsets = axis.nominal_gate_offsets() if axis is not None else []
            if offsets:
                axis.selector.var_manager.set(VARS_MMU_SELECTOR_OFFSETS, list(offsets),
                                              namespace=unit.name)
                # Pre-ready, the variable is all that is needed - LinearSelector.handle_ready
                # reads it and marks itself calibrated (mmu_linear_selector.py:203-216).
                # selector_offsets does not exist until then, and is the phase marker.
                if hasattr(axis.selector, 'selector_offsets'):
                    axis.selector.selector_offsets = list(offsets)
                    axis.selector.calibrator.mark_calibrated(CALIBRATED_SELECTOR)
                done['selector_offsets'] = list(offsets)

            if getattr(unit, 'require_bowden_move', False):
                length = model.layout['extruder_entry'] - model.layout.get('mmu_exit', 0.0)
                # Pre-ready there is no _bowden_lengths to update yet, so write the variable
                # and let MmuCalibrator.handle_ready load it (mmu_calibrator.py:70-95).
                if getattr(unit.calibrator, '_bowden_lengths', None) is None:
                    unit.calibrator.var_manager.set(VARS_MMU_BOWDEN_LENGTHS,
                                                    [length] * unit.num_gates,
                                                    namespace=unit.name)
                else:
                    unit.calibrator.update_bowden_length(
                        length, gate=unit.first_gate, reason='seeded by the test harness')
                done['bowden_length'] = length

            # GEAR ROTATION DISTANCE: seeded with the value the harness is ALREADY using.
            # MmuCalibrator caches the config-derived distances at handle_ready
            # (mmu_calibrator.py:110-111, straight off stepper.get_rotation_distance()), and
            # every gear move in the harness is generated from them - so calibrating would just
            # re-measure the number we started from. Writing it back through HH's own setter
            # keeps the persisted vars honest and silences "gate N not calibrated! Using default
            # rotation distance" on every load. Setting rd to its current value leaves the
            # bowden adjustment inside update_gear_rd a no-op, so ordering after the bowden
            # seeding above is safe.
            #
            # Pre-ready the cache does not exist yet, so read the same source it will be
            # built from and write the variable directly; handle_ready then loads it instead
            # of warning. The two branches agree because a unit's per-gate rotation distances
            # all come from the same config value - verified uniform on every shipped profile.
            defaults = getattr(unit.calibrator, '_default_rotation_distances', None)
            if defaults:
                unit.calibrator.update_gear_rd(defaults[0], gate=unit.first_gate)
                done['gear_rotation_distance'] = defaults[0]
            else:
                rds = self._config_rotation_distances(unit)
                if rds:
                    unit.calibrator.var_manager.set(VARS_MMU_GEAR_ROTATION_DISTANCES, rds,
                                                    namespace=unit.name)
                    done['gear_rotation_distance'] = rds[0]

            # ENCODER: the configured resolution written back as if it had been measured.
            # MMU_CALIBRATE_ENCODER exists to discover the real resolution of real hardware;
            # here the encoder pulses are GENERATED from mmu_encoder.resolution
            # (bootstrap._on_encoder_travel divides travel by it), so the configured value is
            # true by construction and measuring it would only confirm arithmetic.
            if unit.has_encoder():
                for encoder, _counter in self._encoders():
                    unit.calibrator.var_manager.set(VARS_MMU_ENCODER_RESOLUTION,
                                                    round(encoder.resolution, 4),
                                                    namespace=encoder.name)
                unit.calibrator.mark_calibrated(CALIBRATED_ENCODER)
                done['encoder'] = 'resolution is config-true in the harness'

            if done:
                applied[unit.name] = done
        return applied

    def _encoders(self):
        """(MmuEncoder, MCU_counter) for every encoder on this machine."""
        counters = getattr(self.printer, 'harness_counters', {})
        out = []
        for name, obj in self.printer.objects.items():
            if not name.startswith('mmu_encoder '):
                continue
            counter = counters.get(getattr(obj, 'encoder_pin', None))
            if counter is None and len(counters) == 1:
                counter, = counters.values()
            if counter is not None:
                out.append((obj, counter))
        return out

    def _on_encoder_travel(self, gate, delta, start_tip, start_tail):
        """
        Turn filament travel past the encoder into real pulses.

        Delivered through MCU_counter's callback rather than by setting _counts, so
        Happy Hare's own _counter_callback runs: it is what accumulates the distance,
        maintains the no-movement window and drives the derived encoder sensor's
        trigger_handler. Poking _counts would leave that sensor permanently clear.
        """
        model = getattr(self.printer, 'harness_filament', None)
        position = model.layout.get('mmu_encoder') if model else None
        if position is None:
            return
        # A shared encoder sits downstream of the selector, so it only ever sees the
        # gate currently selected. Happy Hare has no per-gate encoders.
        if gate != self.mmu.gate_selected:
            return
        travel = model.travel_over(position, start_tip, start_tail, delta)
        for encoder, counter in self._encoders():
            counter.pulse(int(round(travel / encoder.resolution)))

    def _on_manual_move(self, trapq, distance):
        """
        Advance the filament model for a plain (non-homing) move.

        Filtered to the trapq of whichever stepper is currently DRIVING the filament - HH's own
        notion (MmuDrive.driving_stepper), and exactly one stepper drives in each of the four
        sync modes (mmu_constants.py:169-172):

            gear / gear+extruder  -> the gear stepper drives
            extruder / synced     -> the extruder stepper drives

        A gear+extruder move appends to BOTH trapqs for ONE physical movement, so something has
        to pick just one; keying off the DRIVER counts it exactly once without having to reason
        about append order, and without dropping the two modes where the gear is not the driver.

        This used to watch the gear stepper unconditionally, which silently discarded every
        motor="extruder" and motor="synced" move - the model just did not follow the filament.
        Invisible on a machine with no encoder; on one with an encoder it means no pulses are
        generated and HH concludes the filament is stuck. test_mmu_motion's
        TestEveryDriveModeMovesFilament pins all four modes to the exact distance, so a
        regression to either the dropped-move or the double-counted kind fails loudly.

        Plain SELECTOR moves are handled first and separately. They carry no filament - the
        carriage is a different axis - but they DO have to move the carriage, because the
        harness now tracks it (see the note at the top of test/hh/selector.py). Two things
        break without this: the retract inside MmuGenericRail.home() never backs the carriage
        off the switch, so the second homing move measures zero; and MMU_CALIBRATE_SELECTOR
        AUTO=1 never reaches the end of travel it is trying to find.
        """
        axis = self._selector_axis_for(trapq)
        if axis is not None:
            moved = axis.advance(distance)
            for stepper in axis.stepper.get_steppers():
                stepper.harness_note_motion(moved)
            return

        model = getattr(self.printer, 'harness_filament', None)
        if model is None:
            return
        gate = self.mmu.gate_selected
        if gate is None or gate < 0:
            return
        stepper = self._driving_stepper(gate)
        if stepper is None or getattr(stepper, 'manual_trapq', None) is not trapq:
            return
        model.advance(gate, distance, 'move')

    def _config_rotation_distances(self, unit):
        """
        The per-gate gear rotation distances straight off the steppers - the same expression
        MmuCalibrator.handle_ready uses to build _default_rotation_distances
        (mmu_calibrator.py:108-111). Needed only when seeding BEFORE ready, when that cache
        does not exist yet.
        """
        try:
            steppers = [d.mmu_gear_stepper for d in unit.drives][:unit.num_gates]
            return [s.stepper.get_rotation_distance()[0] for s in steppers]
        except Exception:                           # pragma: no cover - defensive
            logging.debug('harness: no gear steppers to read rotation distance from')
            return []

    def _selector_axis_for(self, trapq):
        """The SelectorAxis whose stepper owns `trapq`, or None for any other move."""
        for axis in (getattr(self.printer, 'harness_selectors', None) or ()):
            if getattr(axis.stepper, 'manual_trapq', None) is trapq:
                return axis
        return None

    def _install_move_observer(self):
        """
        Watch every plain (non-homing) move. Needed by BOTH the filament model and the
        selector axes, so it is installed independently of either - a selector can move
        before anything has asked for the filament model, and filament() is lazy.
        """
        mq = self.printer.lookup_object('motion_queuing', None)
        if mq is not None:
            mq.move_observer = self._on_manual_move
        return self

    def _driving_stepper(self, gate):
        """
        The MmuStepper currently moving this gate's filament, through HH's own public accessors
        (mmu.drive(gate) -> MmuDrive.driving_stepper()).

        Falls back to the gear stepper if either is missing, so the harness degrades to its
        older behaviour against a checkout that predates them rather than advancing nothing.
        """
        drive = self.mmu.drive(gate) if hasattr(self.mmu, 'drive') else None
        driving = getattr(drive, 'driving_stepper', None)
        if callable(driving):
            stepper = driving()
            if stepper is not None:
                return stepper
        return self._gear_stepper(gate)

    def _gear_stepper(self, gate):
        """
        The MmuStepper driving this gate. A multigear machine has one per gate
        (mmu_gear_names == ['unit0_gear', 'unit0_gear_1', ...]); a single-gear machine
        shares index 0 across all gates.
        """
        unit = self.mmu.mmu_unit(gate)
        names = getattr(unit, 'mmu_gear_names', None)
        if not names:
            return None
        index = gate - unit.first_gate if unit.multigear else 0
        if not 0 <= index < len(names):
            index = 0
        return self.printer.lookup_object('mmu_stepper %s' % names[index], None)

    def _set_sensor_state(self, name, state):
        handle = self.sensor(name)
        if handle.present != state:
            handle.set(state)

    @contextlib.contextmanager
    def quiet_sensors(self):
        """
        Apply sensor state WITHOUT HH acting on it, for scenario setup.

        Needed because a filament state change is a real event: putting filament at a
        gate trips the entry switch, which HH treats as an insert and responds to by
        preloading that gate. Correct behaviour, but not what you want while arranging
        a starting position.

        Suppression uses min_event_systime = reactor.NEVER, which is exactly what
        MmuRunoutHelper itself does while a callback is in flight
        (extras/mmu/mmu_sensor_utils.py:218,224,229) - so this is HH's own mechanism,
        not a backdoor.

        To TEST insert handling, place filament outside this block.
        """
        helpers = [sensor.runout_helper for sensor in self.sensors().values()]
        saved = [h.min_event_systime for h in helpers]
        for helper in helpers:
            helper.min_event_systime = self.reactor.NEVER
        try:
            yield self
        finally:
            for helper, previous in zip(helpers, saved):
                helper.min_event_systime = previous

    def heat_extruder(self, temp=None):
        """
        Bring the extruder to temperature so a load does not have to auto-heat.

        Without this, HH emits "Alert: Automatically heating extruder to ..." - and it
        does so through log_error (extras/mmu/mmu_controller.py:2456), so it lands in the
        error sentinel and makes `errors == []` fail. Rather than reclassify HH's own
        severity (which would risk hiding real errors), do what a real print does and heat
        first. Defaults to the selected gate's configured temperature.
        """
        extruder = self.printer.lookup_object('extruder', None)
        if extruder is None:
            return self
        if temp is None:
            gate = self.mmu.gate_selected
            temps = getattr(self.mmu, 'gate_temperature', None)
            temp = 0
            if temps and gate is not None and 0 <= gate < len(temps):
                temp = temps[gate] or 0
            temp = temp or 220
        extruder.heater.set_temp(temp)
        extruder.heater.smoothed_temp = temp
        extruder.heater.can_extrude = True
        return self

    def place_filament(self, gate, position=None, quiet=True):
        """
        Put a gate's filament somewhere. Defaults to the gate park position and to
        quiet placement; pass quiet=False to let HH react as if a user inserted it.
        """
        model = self.filament()
        with (self.quiet_sensors() if quiet else _nullcontext()):
            if position is None:
                model.park(gate)
            else:
                model.place(gate, position)
        if not self.reactor.in_dispatch():
            self.reactor.advance(0.)
        return model

    def ready(self):
        """
        Step 7 (klippy:ready).

        Wrapped in assert_no_pause to match klipper's own dispatch (klippy.py:159-165),
        which has run the whole ready handler loop that way since 302df255d. Anything
        that pauses in here - most easily a SAVE_VARIABLE on modern klipper - raises
        ReactorError, exactly as it does on a real printer.
        """
        with (self.reactor.assert_no_pause() if self.klipper_aio else _nullcontext()):
            self.printer.send_event('klippy:ready')
        return self

    def boot(self, extra=0.01, calibrate=False):
        """
        Full sequence to a live MMU: connect -> ready -> pump the reactor past
        BOOT_DELAY so the scheduled bootup callback runs __MMU_BOOTUP, then past the
        NFC reader init delay that bootup schedules.

        calibrate=True seeds calibration BEFORE klippy:ready, so Happy Hare's own handle_ready
        loads the variables and neither the per-subsystem "... not found in mmu_vars.cfg"
        warnings nor __MMU_BOOTUP's "Calibration steps are not complete" ever fire. Seeding
        afterwards (as the console used to) left the banner warning about a machine that was
        calibrated a millisecond later.

        It deliberately does NOT home. Measured on tradrack, seeding alone leaves bootup's
        output byte-identical minus the warnings; homing here as well makes bootup take a
        different recovery branch (the "Attempting to recover filament position" line goes
        away and the selector row changes). Homing stays where it was - after boot() returns.

        Defaults to False: an uncalibrated machine is a real state HH has to cope with, and
        the tests assert it.
        """
        if not self._booted:
            if self.config is None:
                self.build()
            self.connect()
            # Selector endstop geometry, and the plain-move observer that keeps each
            # carriage up to date. Published here rather than from filament(), which is
            # LAZY - nothing builds the filament model until a test asks for it, and a
            # selector homing move can happen before that (MMU_HOME needs no filament). It is
            # also a genuinely separate axis, so hanging it off the filament model would be
            # the wrong dependency even if the timing worked. Empty list on a VirtualSelector
            # machine, so nothing changes for profiles that have no selector.
            #
            # Before ready() because calibrate() needs the axes to read each unit's CAD
            # geometry; everything they touch is settled at config load.
            self.printer.harness_selectors = selector_mod.axes_for(self.printer)
            self._install_move_observer()
            if calibrate:
                self.calibrate()
            self.ready()
            self.install_macro_effects()
            self.reactor.advance(BOOT_DELAY + extra)
            self._settle_nfc_init(extra)
            self._booted = True
        return self

    def _settle_nfc_init(self, extra=0.01):
        """
        Pump the reactor past MmuNfcManager's post-bootup reader init.

        NFC readers used to initialise on klippy:connect. That handler is now disabled
        (mmu_nfc_reader.py) and MmuNfcManager._handle_mmu_bootup instead schedules
        _delayed_bootup_init NFC_INIT_DELAY seconds later, to let other I2C devices
        settle. Simulated time does not pass on its own, so without this every reader
        stays uninitialised (alive False, chip.init never called) and _start_polling
        never arms - which silently changes what the NFC tests are testing.

        NFC_INIT_DELAY is read from the production module rather than copied, so the
        harness cannot drift out of step with it again. Advance only just past it: the
        same callback arms shared-reader polling (NFC_CHECK_INTERVAL), and overshooting
        would fire poll cycles tests do not expect.

        THEN DRAIN, because firing the callback is not the same as finishing it.
        _init_all_readers runs every reader's init INLINE, and a chip init sleeps
        mid-sequence - RC522's is a soft reset plus _sleep(0.050) (rc522_driver.py:215).
        A driver sleep is reactor.pause(), which parks the callback on a resume timer;
        if that timer lands past advance()'s target the callback is abandoned half-run.
        The advance above leaves only ~`extra` of headroom, so the very first RC522
        would get one register write and then silently stop - taking every LATER reader
        with it, since they had not been reached yet. That is why SPI readers used to
        boot alive=False while a UART reader (no init sleep) came up fine, and why
        _start_polling never armed on shared-reader profiles.

        Draining in small steps keeps the overshoot to what the sleeps actually needed
        (~50ms per SPI reader) rather than a blind jump. It stays under
        NFC_CHECK_INTERVAL (1.0s) up to ~16 readers on one unit; past that a spurious
        shared poll cycle would fire and this needs revisiting.

        The drain is scoped to _delayed_bootup_init because the tail of that same
        callback arms the shared-reader poll, and a poll pauses too - draining on "is
        anything parked" would then chase a 1 Hz timer forever.
        """
        try:
            from extras.mmu.unit.mmu_nfc_manager import (NFC_INIT_DELAY,
                                                         MmuNfcManager)
        except Exception:
            return self
        machine = self.printer.lookup_object('mmu_machine', None)
        if machine is None:
            return self
        if any(getattr(u, 'nfc_manager', None) is not None for u in machine.units):
            self.reactor.advance(NFC_INIT_DELAY + extra)
            # Name taken from the method itself, for the same anti-drift reason as
            # NFC_INIT_DELAY: renaming it upstream then fails loudly here rather than
            # turning the drain into a no-op that looks like a priming bug. budget
            # holds the overshoot under NFC_CHECK_INTERVAL - see above.
            self.reactor.drain_suspended(
                inside=MmuNfcManager._delayed_bootup_init.__name__, budget=1.)
        return self

    def prime_nfc_readers(self, cycles=None):
        """
        Give every configured NFC reader enough scripted bus responses to initialise.
        See test/hh/nfc_fixtures.py for what is scripted and why.
        """
        from . import nfc_fixtures
        kwargs = {} if cycles is None else {'cycles': cycles}
        return nfc_fixtures.prime_all(self.printer, **kwargs)

    def settle(self, dt=0.):
        self.reactor.advance(dt)
        return self

    @property
    def mmu_log(self):
        """
        Where MmuLogger is writing. Derived the same way MmuLogger derives it - dirname of
        start_args['log_file'] + '/mmu.log' (extras/mmu/mmu_logger.py:39-44) - so this
        follows log_dir automatically. Deleted with the tmpdir unless log_dir was given.
        """
        return os.path.join(self.log_dir or self.tmpdir, 'mmu.log')

    # -- assertion surfaces -------------------------------------------------
    @property
    def mmu(self):
        return self.printer.lookup_object('mmu')

    @property
    def gcode(self):
        return self.printer.lookup_object('gcode')

    @property
    def pins(self):
        return self.printer.lookup_object('pins')

    @property
    def webhooks(self):
        return self.printer.lookup_object('webhooks')

    @property
    def save_variables(self):
        """The stub carries a `writes` list of every (name, value) SAVE_VARIABLE saw."""
        return self.printer.lookup_object('save_variables')

    @property
    def errors(self):
        """
        Everything HH reported via respond_raw('!! ...') - log_error and
        log_assertion both funnel there (extras/mmu/mmu_logger.py:137,142).

        ASSERT THIS IS EMPTY IN EVERY BOOTUP TEST. cmd_MMU_BOOTUP catches all
        exceptions and fires mmu:bootup unconditionally
        (extras/mmu/mmu_controller.py:307-456), so checking the event alone passes
        even when bootup failed outright.
        """
        return list(self.gcode.errors)

    @property
    def console(self):
        return list(self.gcode.console)

    def fired(self, event):
        return self.printer.fired(event)

    def run_gcode(self, script):
        self.gcode.run_script(script)
        return self

    def object_names(self, prefix=''):
        return sorted(n for n in self.printer.objects if n.startswith(prefix))


class _SensorHandle:
    """
    Drives a switch sensor THROUGH ITS BUTTON CALLBACK rather than by poking
    runout_helper.filament_present. That is deliberate: the callback path is what
    exercises MmuRunoutHelper.note_filament_present, and with it event_delay,
    min_event_systime gating and the insert/remove/runout/clog/tangle dispatch
    (extras/mmu/mmu_sensor_utils.py:98-273).
    """

    def __init__(self, session, name, sensor):
        self._session = session
        self.name = name
        self.sensor = sensor

    @property
    def present(self):
        return self.sensor.runout_helper.filament_present

    @property
    def kind(self):
        """
        'switch'       - a real switch pin, driven through the buttons callback
        'proportional' - ADC backed (MmuProportionalSensor); driven by feeding a value
        'virtual'      - derived/virtual endstop sensor; driven via trigger_handler

        Not every sensor in all_sensors_map is a switch. A proportional buffer sensor and
        the virtual compression/tension sensors derived from it have no switch_pin at all,
        which is why assuming one broke the EMU profile outright.
        """
        if hasattr(self.sensor, 'switch_pin'):
            return 'switch'
        if hasattr(self.sensor, 'mcu_adc'):
            return 'proportional'
        if hasattr(self.sensor, 'trigger_handler'):
            return 'virtual'
        return 'unknown'

    def set(self, state=True, settle=True):
        kind = self.kind
        if kind == 'switch':
            # Through the real button callback, so MmuRunoutHelper's event_delay /
            # min_event_systime gating and insert/runout dispatch all run.
            buttons = self._session.printer.lookup_object('buttons')
            buttons.press(self.sensor.switch_pin, state)
        elif kind == 'proportional':
            # No switch to press: drive the ADC to a value past the trigger threshold.
            self.feed(self._extreme_value() if state else self.neutral_value(),
                      settle=False)
        elif kind == 'virtual':
            eventtime = self._session.reactor.monotonic()
            self.sensor.trigger_handler(eventtime, bool(state))
        else:
            raise AssertionError(
                'do not know how to drive sensor %r (%s): no switch_pin, mcu_adc or '
                'trigger_handler' % (self.name, type(self.sensor).__name__))
        # Only pump when we are NOT already inside a reactor callback. The filament
        # model syncs sensors from within homing moves, which themselves run inside a
        # callback when an operation was started by a sensor event - pumping there
        # trips advance()'s non-reentrancy assertion and kills the operation
        # mid-flight. Nothing needs pumping in that case: we are already being
        # dispatched.
        if settle and not self._session.reactor.in_dispatch():
            self._session.reactor.advance(0.)
        return self

    # -- proportional (ADC) sensors ----------------------------------------
    def neutral_value(self):
        """The raw ADC reading meaning "no force" - normalises to 0.0."""
        return getattr(self.sensor, '_neutral_point', 0.5)

    def _extreme_value(self):
        """A raw reading comfortably past the virtual-sensor trigger threshold."""
        sensor = self.sensor
        neutral = self.neutral_value()
        threshold = getattr(sensor, 'analog_sensor_threshold', 0.9)
        span = getattr(sensor, '_d_pos', 0.5)
        return neutral + span * min(1.0, threshold + 0.05)

    def feed(self, raw_value, settle=True):
        """Deliver a raw ADC reading to a proportional sensor."""
        if self.kind != 'proportional':
            raise AssertionError('%r is not an ADC-backed sensor' % (self.name,))
        self.sensor.mcu_adc.feed(raw_value)
        if settle and not self._session.reactor.in_dispatch():
            self._session.reactor.advance(0.)
        return self

    @property
    def value(self):
        """Normalised [-1.0, 1.0] reading, for proportional sensors."""
        return getattr(self.sensor, 'value', None)

    def clear(self, settle=True):
        return self.set(False, settle=settle)

    def __repr__(self):
        return '<sensor %s present=%s>' % (self.name, self.present)


def session(profile='boxturtle', **kwargs):
    return Session(profile, **kwargs)
