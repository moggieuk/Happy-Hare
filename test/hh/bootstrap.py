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
                 strict_gcode=False, printer_stub=PRINTER_STUB, virtual_nfc=False):
        self.klippy = install()
        self.profile = (profile if isinstance(profile, profiles_mod.Profile)
                        else profiles_mod.get(profile))
        self.adc_api = adc_api
        self.adc_payload = adc_payload
        self.strict_gcode = strict_gcode
        self.printer_stub = printer_stub
        # Swap reader chips for model-driven virtual ones instead of scripting the real
        # RC522 init. Needed for anything that asks a reader for a UID (MMU_NFC READ,
        # MMU_NFC_SCAN, the preload NFC compound).
        self.virtual_nfc = virtual_nfc
        self.nfc_chips = {}
        self.tmpdir = tempfile.mkdtemp(prefix='hh-session-')
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
        start_args = {
            'log_file': os.path.join(self.tmpdir, 'klippy.log'),
            'config_file': os.path.join(self.tmpdir, 'printer.cfg'),
            'software_version': 'v0.13.0-harness',
            'apiserver': None,
            'debuginput': None,
        }
        self.printer = printer = klippy.Printer(start_args, self.reactor)
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

        # Both of these MUST happen before klippy:connect: readers are constructed
        # during the section loop above but initialised at connect ("rc522 did not
        # respond at connect time"), and a failed init is never retried - so doing
        # either later leaves every reader dead for the whole session.
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
        for unit in self.mmu.mmu_machine.units:
            buffer = getattr(unit, 'buffer', None)
            if buffer is None:
                continue
            sensor_name = resting.get(getattr(buffer, 'buffer_spring_state', 'none'))
            if sensor_name is not None:
                at_rest.add(sensor_name)

        for name, sensor in self.sensors().items():
            bare = name.split(':')[-1]
            try:
                _SensorHandle(self, name, sensor).set(bare in at_rest)
            except AssertionError:
                # A sensor with no button registration (e.g. an ADC-backed one) is
                # driven through its own pin object instead - not an error here.
                logging.debug('no button for sensor %s', name)
        self._spring_at_rest = at_rest
        return self

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
        model.bind(owned, self._set_sensor_state)
        self.printer.harness_filament = model
        mq = self.printer.lookup_object('motion_queuing', None)
        if mq is not None:
            mq.move_observer = self._on_manual_move
        return model

    def _on_manual_move(self, trapq, distance):
        """
        Advance the filament model for a plain (non-homing) gear move.

        Filtered to the SELECTED gate's gear stepper trapq on purpose: a
        motor="gear+extruder" move appends to both the gear and the extruder trapq for
        one physical filament movement, and counting both would double the distance.
        """
        model = getattr(self.printer, 'harness_filament', None)
        if model is None:
            return
        gate = self.mmu.gate_selected
        if gate is None or gate < 0:
            return
        stepper = self._gear_stepper(gate)
        if stepper is None or getattr(stepper, 'manual_trapq', None) is not trapq:
            return
        model.advance(gate, distance, 'move')

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
        """Step 7 (klippy:ready)."""
        self.printer.send_event('klippy:ready')
        return self

    def boot(self, extra=0.01):
        """
        Full sequence to a live MMU: connect -> ready -> pump the reactor past
        BOOT_DELAY so the scheduled bootup callback runs __MMU_BOOTUP.
        """
        if not self._booted:
            if self.config is None:
                self.build()
            self.connect()
            self.ready()
            self.reactor.advance(BOOT_DELAY + extra)
            self._booted = True
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

    def set(self, state=True, settle=True):
        buttons = self._session.printer.lookup_object('buttons')
        buttons.press(self.sensor.switch_pin, state)
        # Only pump when we are NOT already inside a reactor callback. The filament
        # model syncs sensors from within homing moves, which themselves run inside a
        # callback when an operation was started by a sensor event - pumping there
        # trips advance()'s non-reentrancy assertion and kills the operation
        # mid-flight. Nothing needs pumping in that case: we are already being
        # dispatched.
        if settle and not self._session.reactor.in_dispatch():
            self._session.reactor.advance(0.)
        return self

    def clear(self, settle=True):
        return self.set(False, settle=settle)

    def __repr__(self):
        return '<sensor %s present=%s>' % (self.name, self.present)


def session(profile='boxturtle', **kwargs):
    return Session(profile, **kwargs)
