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
        self.primed = {}                # gate -> filament attributes, from prime_gate_map()
        self.moonraker = None           # set by attach_moonraker()
        self.moonraker_link = None
        self._booted = False

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self):
        self.build()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        if self.moonraker is not None:
            # Owns an asyncio loop; leaks a ResourceWarning per session otherwise
            self.moonraker.close()
            self.moonraker = self.moonraker_link = None
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
        """
        A virtual chip by reader name, or by gate index for a per-gate reader. A chip
        shared between neighboring gates matches on any gate in its _gates list.
        """
        if name_or_gate in self.nfc_chips:
            return self.nfc_chips[name_or_gate]
        for chip in self.nfc_chips.values():
            if chip._gate == name_or_gate or name_or_gate in chip._gates:
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
        # Keyed by the buffer's OWN namespace prefix (buffer.name, which is what
        # MmuBuffer actually qualifies its sensor keys with - not necessarily the
        # requesting unit's name, for a shared buffer). Bare sensor names collide
        # across units (every buffer's switches are called filament_tension /
        # filament_compression regardless of which unit owns them), so both `at_rest`
        # and `derived` below must stay qualified or one unit's buffer state bleeds
        # into another's - which is what silently left a switch-based buffer's
        # sensors untouched whenever another unit's buffer was proportional.
        at_rest = set()
        spring_by_prefix = {}
        for unit in self.mmu.mmu_machine.units:
            buffer = getattr(unit, 'buffer', None)
            if buffer is None:
                continue
            spring = getattr(buffer, 'buffer_spring_state', 'none')
            prefix = getattr(buffer, 'name', unit.name)
            spring_by_prefix[prefix] = spring
            sensor_name = resting.get(spring)
            if sensor_name is not None:
                at_rest.add('%s:%s' % (prefix, sensor_name))

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
            prefix = name.split(':')[0]
            spring = spring_by_prefix.get(prefix, 'none')
            handle.feed(self._resting_raw(handle, spring), settle=False)
            # Exclude the analog sensor ITSELF as well as the two it derives, scoped to
            # THIS buffer's own namespace: the loop below would otherwise call set(False)
            # on it, which for a proportional sensor means feeding neutral - overwriting
            # the resting value just fed.
            derived |= {'%s:%s' % (prefix, SENSOR_TENSION), '%s:%s' % (prefix, SENSOR_COMPRESSION), name}

        for name, sensor in self.sensors().items():
            if name in derived:
                continue        # derived from the analog reading above
            try:
                _SensorHandle(self, name, sensor).set(name in at_rest)
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

        Sensors it does not own are left alone. Unsupported buffer types remain at the
        state established by apply_initial_sensor_states; a tension-sprung two-switch
        buffer is explicitly claimed by the filament model so its state can change.
        """
        existing = getattr(self.printer, 'harness_filament', None)
        if existing is not None and layout is None:
            return existing

        from .filament import FilamentPath
        from extras.mmu.mmu_constants import (
            DRIVE_UNSYNCED, DRIVE_EXTRUDER_ONLY,
        )

        def drive_mode(gate):
            mode = self.mmu.drive(gate).get_sync_mode()
            if mode == DRIVE_UNSYNCED:
                return 'gear'
            if mode == DRIVE_EXTRUDER_ONLY:
                return 'extruder'
            return 'synced'

        model = FilamentPath(self.mmu.num_gates, layout=layout)
        model.configure_buffers(
            self.mmu.mmu_machine.units,
            selected_gate=lambda: self.mmu.gate_selected,
            drive_mode=drive_mode,
        )
        owned = [name for name in self.sensors()
                 if (name not in getattr(self, '_spring_at_rest', set())
                     or model.models_sensor(name))
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
        # Purge moves a lot of filament through the nozzle, none of which this harness models -
        # but it TAKES TIME on a real machine, and a paced session that skips it in an instant
        # reads as if the operation were over when it is not. Time only; no movement.
        effects.setdefault('_MMU_PURGE', self._effect_spend_time)
        return effects

    # What a macro whose body never runs would nonetheless COST, at pace 1. Tip forming and
    # purging are both a sequence of ramming, cooling and wiping moves that the harness does not
    # reproduce (see _effect_form_tip); a few seconds each is the honest stand-in, and it is a
    # round number on purpose - deriving one from the macro's own speeds implies a fidelity the
    # net-movement model does not have.
    MACRO_DURATION = 4.0

    def _spend_macro_time(self, seconds=None, movement=0.):
        """
        Let a macro cost time, walking `movement` mm of filament across it if there is any.

        Yields nothing itself - the caller gets the per-slice movement so it can apply it - and
        with pacing off it is a single slice and no time at all, exactly as before.
        """
        mq = self.printer.lookup_object('motion_queuing', None)
        if mq is None:
            return iter((movement,))
        return mq.pace_move(self.MACRO_DURATION if seconds is None else seconds, movement)

    def _effect_spend_time(self, macro, gcmd):
        for _amount in self._spend_macro_time():
            pass

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

        Both halves are done explicitly because the harness macro body does not emit either a
        manual trapq move or a toolhead extrusion for the motion observers to see.
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
        model = getattr(self.printer, 'harness_filament', None)
        gate = self.mmu.gate_selected
        if gate is None or gate < 0:
            model = None

        # HOW LONG it takes, so a paced session does not do the whole retract in one instant -
        # it is the first thing an unload does, and it was the last step still finishing
        # immediately.
        #
        # MACRO_DURATION rather than distance/speed. The retract modelled here is the macro's
        # NET movement, but the real macro spends its time ramming, cooling and dipping over
        # that same span - so dividing the net distance by any one of its speeds understates it
        # (unloading_speed_start put the whole thing at 0.5s). A flat few seconds is the honest
        # answer for a body that does not run.
        for amount in self._spend_macro_time(movement=-distance):
            if stepper is not None:
                # Retract: HH reads (initial_mcu_pos - final_mcu_pos) * step_dist
                # (mmu_filament_movement.py:2541-2559), so BOTH have to move. set_position
                # alone is not enough - it is mcu-preserving, by design (klippy_root/stepper.py)
                stepper.set_position([stepper.get_commanded_position() + amount, 0., 0., 0.])
                stepper.harness_note_motion(amount)
            if model is not None:
                model.advance(gate, amount, 'tip forming')

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
        notion (MmuDrive.driving_stepper).  The three manual move modes use these trapqs:

            gear / gear+extruder  -> the gear stepper drives
            extruder              -> the extruder stepper drives

        The fourth mode, gear synced to extruder, is a toolhead move and is handled separately
        by _on_toolhead_move because it never reaches an MmuStepper manual trapq.

        A gear+extruder move appends to BOTH trapqs for ONE physical movement, so something has
        to pick just one; keying off the DRIVER counts it exactly once without having to reason
        about append order, and without dropping extruder-only movement.

        This used to watch the gear stepper unconditionally, which silently discarded every
        motor="extruder" move.  Synced moves were also dropped because no toolhead observer
        existed.  Invisible on a machine with no encoder; on one with an encoder either gap
        means no pulses are generated and HH concludes the filament is stuck. test_mmu_motion's
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
        Watch every plain (non-homing) manual move and toolhead extrusion.  Needed by the
        filament model and selector axes, so it is installed independently of either - a
        selector can move before anything has asked for the filament model, and filament()
        is lazy.
        """
        mq = self.printer.lookup_object('motion_queuing', None)
        if mq is not None:
            mq.move_observer = self._on_manual_move
            mq.toolhead_move_observer = self._on_toolhead_move
        return self

    def _on_toolhead_move(self, distance):
        """Advance filament when the gear is following a toolhead extrusion move."""
        model = getattr(self.printer, 'harness_filament', None)
        gate = self.mmu.gate_selected
        if model is None or gate is None or gate < 0:
            return

        from extras.mmu.mmu_constants import DRIVE_GEAR_SYNCED_TO_EXTRUDER
        if self.mmu.drive(gate).get_sync_mode() != DRIVE_GEAR_SYNCED_TO_EXTRUDER:
            return
        model.advance(gate, distance, 'toolhead extrusion')

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

    # Plausible spool metadata for prime_gate_map(). Vendors and materials are real names a
    # user would recognise, so the gate table, the LED filament_color render and the Spoolman
    # paths all have something to show that is not "Unknown".
    FILAMENT_VENDORS = ('eSun', 'KVS', 'Bambu Labs', 'Prusa')
    FILAMENT_MATERIALS = ('ABS', 'ABS+', 'PLA', 'TPU', 'PLA+')
    FILAMENT_TEMP_RANGE = (210, 250)
    # The filament NAME is a product name, and HH renders it next to the vendor
    # ("Prusa | PLA Matte"), so it must not repeat the vendor or the line reads "Prusa Prusa".
    # Matches the shape of the material_detail field a real deep-read tag carries.
    FILAMENT_GRADES = ('Basic', 'Matte', 'Silk', 'Tough', 'HF')

    def prime_gate_map(self, seed=0):
        """
        Give every gate a vendor, material, colour and temperature. Returns
        {gate: {what was set}}.

        A fresh machine has none of this - `MMU_STATUS` shows "Unknown | 200C | Unknown" on
        every gate, and the LED filament_color effect has nothing to render - which makes
        anything that presents filament attributes impossible to eyeball.

        SEEDED, so a session is reproducible: the point is varied data, not different data
        every run. Pass a different seed for a different spread.

        Goes through HH's own set_gate_filament_from_tag (mmu_gate_maps.py:352) rather than
        assigning the lists directly, so the colour is validated, gate_color_rgb is refreshed
        and the map is persisted exactly as a real tag read would leave it. That setter does
        NOT touch spool_id - a resolved Spoolman spool stays authoritative. The same values
        are installed as the harness's configured defaults so MMU_GATE_MAP RESET=1 can restore
        the reproducible dummy filament map just as it could restore default_gate_XXX values
        from a real mmu.cfg.
        """
        import random as _random

        rng = _random.Random(seed)
        applied = {}
        for gate in range(self.mmu.num_gates):
            material = rng.choice(self.FILAMENT_MATERIALS)
            attrs = {
                'vendor': rng.choice(self.FILAMENT_VENDORS),
                'material': material,
                'color': '%06X' % rng.randrange(0x1000000),
                'temperature': rng.randint(*self.FILAMENT_TEMP_RANGE),
                'name': '%s %s' % (material, rng.choice(self.FILAMENT_GRADES)),
            }
            self.mmu.gate_maps.set_gate_filament_from_tag(gate, **attrs)
            self.mmu.p.default_gate_vendor[gate] = attrs['vendor']
            self.mmu.p.default_gate_material[gate] = attrs['material']
            self.mmu.p.default_gate_color[gate] = attrs['color'].lower()
            self.mmu.p.default_gate_temperature[gate] = attrs['temperature']
            self.mmu.p.default_gate_filament_name[gate] = attrs['name']
            applied[gate] = attrs
        return applied

    def attach_moonraker(self, spools=None, hostname='mmu-sim', **kwargs):
        """
        Give this session a live fake Moonraker + Spoolman, and return the MoonrakerLink.

        Without one, every call Happy Hare makes to Moonraker goes into the void: a UID
        lookup is dispatched and nothing ever answers, so an NFC read ends in "Automatic
        assignment of id timed out" ~20s later. That is faithful to a printer with Moonraker
        down, and useless for exercising the Spoolman paths.

        The MmuServer is REAL - only the server around it is faked - so the round trip
        exercises the actual contract in both directions. The caller must pump it: the link's
        settle() alternately delivers queued Klipper->Moonraker calls and Moonraker->Klipper
        gcode until both sides are quiet.

        Must be called AFTER boot(): component_init() replays the state bootup published.
        """
        from .moonraker import MoonrakerHarness
        from .roundtrip import MoonrakerLink

        self.moonraker = MoonrakerHarness(
            spools=list(spools or ()), num_gates=self.mmu.num_gates,
            hostname=hostname, **kwargs)
        self.moonraker.component_init()
        self.moonraker_link = MoonrakerLink(self, self.moonraker)
        self.moonraker_link.settle()
        return self.moonraker_link

    def spools_for_gate_map(self, uid_for=None):
        """
        One Spoolman spool per gate, matching what prime_gate_map() put in the gate map, each
        registered against a UID. Returns the list of add_spool() kwargs.

        Matching matters: a resolved spool OVERRIDES the local gate map, so seeding Spoolman
        with unrelated filament would make every lookup visibly rewrite the gate it resolved
        for - which looks like a bug rather than like a printer.

        uid_for(gate) supplies the tag UID; the default is stable and greppable, so
        `_MMU_TEST NFC_READ=1 UID=<it>` resolves without having to look anything up.
        """
        uid_for = uid_for or (lambda gate: 'BADCAFE%03X' % gate)
        spools = []
        for gate, attrs in sorted((self.primed or {}).items()):
            spools.append({
                'uid': uid_for(gate),
                'gate': gate,
                'name': attrs['name'],
                'material': attrs['material'],
                'vendor': attrs['vendor'],
                'color_hex': attrs['color'],
                'extruder_temp': attrs['temperature'],
            })
        return spools

    def set_pacing(self, factor, wall=None):
        """
        How much of each move's real duration to spend in virtual time. Returns the factor.

        0 (the default) is instant: an MMU_LOAD finishes without the clock moving, which is
        fast but leaves nothing time-driven observable - LED effects never reach a second
        frame, and every action transition happens in the same instant. 1.0 gives each move
        roughly the time the real machine would need; 0.5 is twice as fast as real.

        `wall` is a SEPARATE multiplier for sleeping in real time. Virtual time is free -
        advancing the clock 11 seconds costs milliseconds - so pacing alone makes an operation
        report the right timings while still flashing past in an instant. wall=1 holds each
        move for as long as it claims to take, which is what makes it watchable; 0 (the
        default) never sleeps, so tests can pace freely. Left as None it is unchanged.

        Only meaningful for commands dispatched at TOP LEVEL (the console, and the tests):
        the pacer advances the reactor, which is illegal inside a reactor callback and is
        skipped there. See PrinterMotionQueuing._pace.
        """
        factor = max(0., float(factor))
        self.printer.harness_pacing = factor
        if wall is not None:
            self.printer.harness_pace_wall = max(0., float(wall))
        return factor

    @property
    def pacing(self):
        return getattr(self.printer, 'harness_pacing', 0.) or 0.

    @property
    def pacing_wall(self):
        return getattr(self.printer, 'harness_pace_wall', 0.) or 0.

    def settle_leds(self, limit=20.):
        """
        Advance the virtual clock until no unit is held by a timed state effect.

        effect_initialized is a unit-wide 8s flash from bootup (mmu_led_manager.py:254), and
        while it holds a unit EVERY transient flash is silently dropped
        (mmu_led_manager.py:473) - so an NFC read acknowledgment, say, does nothing. On a
        printer that window passes on its own; here the clock stops where boot() left it,
        2.5s in, so an interactive session sits inside it for good.

        Returns the seconds advanced. Deliberately NOT called from boot(): tests assert the
        held state (test_mmu_leds.TestTransientFlashWhileHeld), and warming up costs every
        other test 10 virtual seconds it does not need.
        """
        manager = getattr(self.mmu, 'led_manager', None)
        if manager is None:
            return 0.
        advanced = 0.
        while any(manager.pending_update) and advanced < limit:
            self.reactor.advance(1.)
            advanced += 1.
        return advanced

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

    def seed_loaded_gates(self, tip_position, status=None):
        """
        Put filament at every gate and persist an 'available' map, BEFORE klippy:ready.

        Same trick and the same reason as calibrate(): __MMU_BOOTUP prints the gate table,
        and anything the caller does afterwards is invisible in it. The console preloads
        every gate right after boot() returns, so its bootup banner reported the whole
        machine unknown (or empty) about a machine that is fully loaded by the time the
        prompt appears - and that banner is the last thing on screen.

        This is what a real printer looks like, not a fiction: gate_status is persisted in
        mmu_vars.cfg and MmuGateMaps.load_persisted_state reads it back at handle_ready
        (mmu_controller.py:228), so a printer that was loaded yesterday boots up loaded. A
        harness session starts with no vars file at all, which is the only reason it came up
        GATE_UNKNOWN.

        BOTH HALVES ARE NEEDED, and each covers what the other cannot:

          - the persisted map is the only source for a unit with no per-gate sensors (ERCF),
          - and it is not enough for one that has them (ViViD's mmu_entry_9..12). __MMU_BOOTUP
            re-derives those gates from their switches, so a seeded 'available' with no
            filament in front of the switch is overwritten with GATE_EMPTY.

        Placing pre-ready is also why it is safe to place at all: filament arriving at a gate
        is a real insert event that Happy Hare responds to by preloading that gate (see
        quiet_sensors), and nothing is listening yet.

        Must be called between connect() and ready(): mmu.var_manager is bound in
        handle_connect (mmu_controller.py:192). Do NOT call apply_initial_sensor_states()
        afterwards - it resets every switch to its configured resting state and would undo
        the placement.
        """
        # NOT the selected gate/tool: that is seed_selection()'s job, and it has its own rules
        # about pairing with seed_selector_last_pos().
        from extras.mmu.mmu_constants import GATE_AVAILABLE, VARS_MMU_GATE_STATUS
        model = self.filament()
        for gate in range(self.mmu.num_gates):
            model.place(gate, tip_position)
        seeded_status = GATE_AVAILABLE if status is None else status
        self.mmu.var_manager.set(VARS_MMU_GATE_STATUS,
                                 [seeded_status] * self.mmu.num_gates)
        self.mmu.p.default_gate_status[:] = [seeded_status] * self.mmu.num_gates
        return self

    def seed_selection(self, gate, tool=None):
        """
        Persist a selected gate/tool BEFORE klippy:ready, as a printer that ran yesterday does.

        Same window and the same reason as seed_loaded_gates(): must be between connect() and
        ready(), because mmu.var_manager is bound in handle_connect (mmu_controller.py:196), and
        MmuGateMaps.load_persisted_state() reads it during MmuController.handle_ready.

        Both vars are GLOBAL, not unit-namespaced (mmu_constants.py:228-229) - which is exactly
        why they can outlive a per-unit mmu_<unit>_selector_last_pos across a rename or an
        upgrade, and so why the selector falls back to them. tool defaults to gate, i.e. the
        identity TTG map every profile ships with.

        On its own this leaves the selector claiming a gate with NO position on record, which is
        the fallback path (PhysicalSelector._persisted_gate_position). Pair it with
        seed_selector_last_pos() to model an ordinary power-off instead.
        """
        from extras.mmu.mmu_constants import VARS_MMU_GATE_SELECTED, VARS_MMU_TOOL_SELECTED
        self.mmu.var_manager.set(VARS_MMU_GATE_SELECTED, gate)
        self.mmu.var_manager.set(VARS_MMU_TOOL_SELECTED, gate if tool is None else tool)
        return self

    def selector_offset(self, gate, unit=0):
        """
        The calibrated carriage offset for a gate, read straight from the persisted vars.

        Works BEFORE ready(), unlike selector.selector_offsets, which the selector only loads in
        its own handle_ready - so this is how a test seeds a position that AGREES with a gate.
        None on a selector that keeps no offsets (IndexedSelector) or before calibrate().
        """
        from extras.mmu.mmu_constants import VARS_MMU_SELECTOR_OFFSETS
        mmu_unit = self.mmu.mmu_machine.units[unit]
        offsets = self.mmu.var_manager.get(VARS_MMU_SELECTOR_OFFSETS, None, namespace=mmu_unit.name)
        if not mmu_unit.owns_gate(gate):
            return None
        lgate = mmu_unit.local_gate(gate)
        if not offsets or lgate >= len(offsets):
            return None
        return offsets[lgate]

    def seed_selector_last_pos(self, pos, unit=0):
        """
        Persist a unit's raw selector carriage position BEFORE klippy:ready.

        This is the PRIMARY position record, restored by the selector's own handle_ready, which
        runs before MmuController.handle_ready - so it is what decides whether the selector
        claims homed in time for load_persisted_state to keep the persisted gate.

        Namespaced per unit (mmu_<unit>_selector_last_pos), so a value seeded for unit0 says
        nothing about unit1. Pass pos=None to model a position that was deliberately invalidated.
        """
        from extras.mmu.mmu_constants import VARS_MMU_SELECTOR_LAST_POS
        mmu_unit = self.mmu.mmu_machine.units[unit]
        self.mmu.var_manager.set(VARS_MMU_SELECTOR_LAST_POS, pos, namespace=mmu_unit.name)
        return self

    def seed_sensor_disabled(self, names):
        """
        Persist a sparse per-sensor disabled map BEFORE klippy:ready, as a printer that had
        MMU_SENSORS SENSOR=... ENABLE=0 run against it yesterday does.

        Must be called between connect() and ready(): mmu.var_manager is bound in
        handle_connect, and MmuSensorManager.load_persisted_state() reads it back at
        klippy:ready.
        """
        from extras.mmu.mmu_constants import VARS_MMU_SENSOR_ENABLED
        self.mmu.var_manager.set(VARS_MMU_SENSOR_ENABLED, {name: False for name in names})
        return self

    def home_selectors(self):
        """
        MMU_HOME every unit that has a physical selector. Returns the units homed.

        Named per unit because MMU_HOME insists on it once more than one is configured
        (mmu_base_command.py:198). No-op on a VirtualSelector machine.
        """
        # Unlike production code, the harness owns the complete physical model and knows the
        # active path is empty. Publish that fact explicitly: MMU_HOME deliberately no longer
        # accepts an override that homes through an unresolved filament state.
        from extras.mmu.mmu_constants import FILAMENT_POS_UNLOADED
        self.mmu.set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=True)

        homed = []
        for index, unit in enumerate(self.mmu.mmu_machine.units):
            if getattr(unit.selector, 'selector_stepper', None) is not None:
                self.gcode.run_script('MMU_HOME UNIT=%d' % index)
                homed.append(unit.name)
        return homed

    def boot(self, extra=0.01, calibrate=False, gates_loaded_at=None, prime=False, seed=0,
             pre_bootup=None, selected_gate=None, selected_tool=None, selector_last_pos=None,
             sensors_disabled=None):
        """
        Full sequence to a live MMU: connect -> ready -> pump the reactor past
        BOOT_DELAY so the scheduled bootup callback runs __MMU_BOOTUP, then past the
        NFC reader init delay that bootup schedules.

        calibrate=True seeds calibration BEFORE klippy:ready, so Happy Hare's own handle_ready
        loads the variables and neither the per-subsystem "... not found in mmu_vars.cfg"
        warnings nor __MMU_BOOTUP's "Calibration steps are not complete" ever fire. Seeding
        afterwards (as the console used to) left the banner warning about a machine that was
        calibrated a millisecond later.

        gates_loaded_at=<tip position> is the same idea for the gate map - see
        seed_loaded_gates(). Only the console passes it, and only when it is about to preload
        every gate anyway.

        prime=True is the same idea again, for the filament ATTRIBUTES - see prime_gate_map().
        Also before ready, and for the same reason: __MMU_BOOTUP prints the gate/filament table,
        so priming afterwards left the bootup banner showing "Unknown" on every gate while a
        later MMU_STATUS showed the real thing.

        pre_bootup is a callable run after klippy:ready but before the advance that fires
        __MMU_BOOTUP - the seam for anything needing a LIVE machine that bootup nonetheless has
        to see. The console passes homing (see home_selectors): bootup renders the selector and
        filament rows, and homing afterwards left it showing an unhomed 'Selct: XXXX' about a
        machine a later MMU_STATUS reported as homed.

        What homing here trades away: bootup takes a different recovery branch on an already
        homed machine, so its "Attempting to recover filament position" line does not appear
        there (MMU_HOME emits it instead). A test asserting on that line wants pre_bootup=None.

        selected_gate / selected_tool / selector_last_pos are the same idea for the persisted
        SELECTION - see seed_selection() and seed_selector_last_pos(). Seed both to model an
        ordinary power-off; seed only the gate to model a machine whose per-unit position var
        never existed (a rename or an upgrade), which is the gate-offset fallback path. Needs
        calibrate=True to do anything on a physical selector: the fallback refuses to turn a gate
        into a position while the offsets are -1 placeholders.

        selector_last_pos=True is the shorthand for "the calibrated offset of selected_gate",
        resolved after calibrate() on the unit that owns the gate. Pass a number for a position
        that deliberately disagrees with the gate.

        sensors_disabled=[names] is the same idea for per-sensor enable state - see
        seed_sensor_disabled(). Models a printer that had MMU_SENSORS SENSOR=... ENABLE=0 run
        against it before this boot.

        All of them default to False: an uncalibrated, unhomed machine with an unknown gate map
        is a real state HH has to cope with, and the tests assert it.
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
            if prime:
                self.primed = self.prime_gate_map(seed=seed)
            if gates_loaded_at is not None:
                self.seed_loaded_gates(gates_loaded_at)
            if selected_gate is not None:
                self.seed_selection(selected_gate, selected_tool)
            if selector_last_pos is not None:
                # True means "the offset that agrees with selected_gate", resolved on the unit
                # that owns it - so a test never has to restate a CAD number
                unit_index = self.mmu.mmu_unit(selected_gate).unit_index
                if selector_last_pos is True:
                    selector_last_pos = self.selector_offset(selected_gate, unit=unit_index)
                self.seed_selector_last_pos(selector_last_pos, unit=unit_index)
            if sensors_disabled is not None:
                self.seed_sensor_disabled(sensors_disabled)
            self.ready()
            # The seam for anything that needs a LIVE machine but has to happen before bootup
            # renders - homing, in the console's case. See the docstring.
            if pre_bootup is not None:
                pre_bootup()
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
