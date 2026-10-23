# TD-1 filament measurement, end to end.
#
# Runs the real Happy Hare controller against the fake Klipper, with a mocked Moonraker
# transport standing in for the [td1] component. The scanner itself is imaginary; the
# gate homing, filament movement and gate-map persistence are all real.
#
# There is no scan geometry: a measurement is produced by filament traveling its normal
# path past the scanner, so Happy Hare never needs to know where the scanner sits.
# MMU_CHECK_GATE TD1=1 measures a gate by loading to the extruder (never into it) and
# unloading again; everything else is passive.
#
# Single file run:
#   make test UT='test_mmu_td1.py' JOBS=1
import logging
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from test.hh import session
from test.hh import profiles, cfg
from test.hh.moonraker import harness

logging.getLogger().setLevel(logging.CRITICAL)

# One scanner in a shared part of the bowden: every gate names the same serial, which
# is how an in-path scanner serving several gates is expressed (and the topology the
# attribution debt exists for). An off-path scanner is 'td1_device' - see TestTd1Pending
PROFILE = profiles.get("boxturtle").derive("td1", syms={
    "MMU_HAS_TD1": True, "MMU_HAS_PER_GATE_TD1": True,
    "PARAM_TD1_DEVICE_0": "SERIAL_A", "PARAM_TD1_DEVICE_1": "SERIAL_A",
    "PARAM_TD1_DEVICE_2": "SERIAL_A", "PARAM_TD1_DEVICE_3": "SERIAL_A",
    "PARAM_TD1_CAPTURE_TIMEOUT": "1",
})

# Per-gate assignment: gate 2 deliberately has no scanner
PER_GATE_SYMS = {
    "MMU_HAS_PER_GATE_TD1": True,
    "PARAM_TD1_DEVICE_0": "A", "PARAM_TD1_DEVICE_1": "B",
    "PARAM_TD1_DEVICE_GATE_2": False, "PARAM_TD1_DEVICE_2": "",
    "PARAM_TD1_DEVICE_3": "A",
}


def record(second=0, td=4., color="123456"):
    return {"td": td, "color": color, "scan_time": f"2026-09-19T00:00:{second:02d}Z"}


class Request:
    """Stand-in for a Klipper WebRequest delivered to the mmu/td1 endpoint."""

    def __init__(self, **values):
        self.values = values
        self.response = None

    def get(self, key, default=None):
        return self.values.get(key, default)

    def get_int(self, key):
        return int(self.values[key])

    def send(self, value):
        self.response = value


class Td1Case(unittest.TestCase):
    def setUp(self):
        self.hh = session(PROFILE)
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [])
        self.mmu = self.hh.mmu
        self.bridge = self.mmu.td1                        # Moonraker transport
        self.manager = self.mmu.mmu_unit(0).td1_manager   # this profile has one unit
        self.data = {"SERIAL_A": record()}
        self.hh.webhooks.sink = self.respond
        self.bridge.pending.clear()

    def tearDown(self):
        self.hh.close()

    def respond(self, method, values):
        if method == "mmu_td1_request":
            self.bridge._callback(Request(
                request_id=values["request_id"], devices=self.data, error=None))

    def param(self, gate, name, value):
        """Set one TD-1 tunable on the unit owning 'gate'."""
        setattr(self.mmu.mmu_unit(gate).p, name, value)

    def owner(self, gate, serial="SERIAL_A", capture=False):
        """Arm attribution the way begin_load() would, without a load sequence."""
        token = {'gate': gate, 'revision': self.manager.revision(gate),
                 'baseline': None, 'capture': capture, 'satisfied': False}
        self.device(serial).arm(token)
        return token

    def owners(self):
        """Armed attribution across every device, in the old {serial: token} shape."""
        return {s: d.owner for s, d in self.bridge.devices.items() if d.owner is not None}

    def clear_owners(self):
        """Drop every armed token WITHOUT recording a debt (test setup, not a release)."""
        for d in self.bridge.devices.values():
            d.owner = None

    def device(self, serial="SERIAL_A"):
        """The MmuTd1Device object for a serial, adopting an unconfigured one."""
        return self.bridge.devices.get(serial) or self.bridge._adopt(serial)


class TestMeasurement(Td1Case):
    def test_formats_and_invalid_records(self):
        from extras.mmu.mmu_td1 import measurement
        for stamp in ("2026-09-19T00:00:00Z", "2026-09-19T00:00:00+00:00Z",
                      "2026-09-19T02:00:00+02:00"):
            result = measurement(dict(record(color="ABCDEF"), scan_time=stamp))
            self.assertEqual(result, {"td": 4., "color": "abcdef",
                                     "scan_time": "2026-09-19T00:00:00+00:00"})
        for data in (None, {}, dict(record(), error="optical error"),
                     record(td=True), record(td="4"), record(td=-1),
                     record(td=float("nan")), record(td=float("inf")),
                     record(color="#ffffff"), dict(record(), scan_time=None),
                     dict(record(), scan_time="invalid"),
                     dict(record(), scan_time="2026-09-19T00:00:00")):
            with self.subTest(data=data), self.assertRaises(ValueError):
                measurement(data)

    def test_has_reading_separates_unmeasured_from_malformed(self):
        from extras.mmu.mmu_td1 import has_reading
        self.assertTrue(has_reading(record()))
        self.assertTrue(has_reading({"td": -1}))
        self.assertFalse(has_reading({}))
        self.assertFalse(has_reading({"color": "123456"}))
        self.assertFalse(has_reading(None))


class TestTd1Setup(Td1Case):
    def test_shared_config_and_status(self):
        self.assertEqual([self.manager.serial_for(g) for g in range(4)], ["SERIAL_A"] * 4)
        self.bridge.refresh()
        self.assertEqual(self.bridge.gates_for("SERIAL_A"), [0, 1, 2, 3])
        self.assertTrue(self.bridge.devices["SERIAL_A"].connected)

    def test_measurements_are_published_in_printer_status(self):
        self.bridge.update_devices({"SERIAL_A": record()})
        status = self.mmu.get_status(0)
        self.assertEqual(status["gate_td"], [None] * 4)
        self.assertIn("gate_td1_color", status)

    def test_policy_is_one_flat_list_indexed_by_gate(self):
        # Same shape as 'espooler' and 'drying_state': one entry per gate, aggregated
        # across units, empty string where the feature doesn't reach
        status = self.mmu.get_status(0)
        self.assertEqual(status["td1"], ["enabled"] * 4)
        self.assertEqual(len(status["td1"]), self.mmu.num_gates)
        self.bridge.set_device_state("SERIAL_A", auto=True)
        self.assertEqual(self.mmu.get_status(0)["td1"], ["auto"] * 4)
        self.bridge.set_device_state("SERIAL_A", enabled=False)
        self.assertEqual(self.mmu.get_status(0)["td1"], ["disabled"] * 4)

    def test_gates_without_a_scanner_report_empty(self):
        self.manager.gate_devices[2] = None
        self.assertEqual(self.mmu.get_status(0)["td1"],
                         ["enabled", "enabled", "", "enabled"])

    def test_disabled_outranks_auto(self):
        # A disabled scanner isn't auto-updating anything, whatever the flag says
        self.bridge.set_device_state("SERIAL_A", auto=True, enabled=False)
        self.assertEqual(self.mmu.get_status(0)["td1"], ["disabled"] * 4)

    def test_assignment_is_discoverable_from_the_machine_object(self):
        machine = self.mmu.mmu_machine.get_status(0)
        self.assertEqual(machine["unit_0"]["td1_devices"], ["SERIAL_A"] * 4)
        self.assertNotIn("td1_device", machine["unit_0"])

    def test_both_topologies_are_published_independently(self):
        # They are not alternatives: a unit may have per-gate scanners AND one you
        # present filament to, exactly as with the NFC readers
        profile = PROFILE.derive("td1_both", syms={
            "MMU_HAS_OFFPATH_TD1": True, "PARAM_TD1_DEVICE": "BENCH"})
        with session(profile) as hh:
            hh.boot(calibrate=True)
            self.assertEqual(hh.errors, [])
            unit = hh.mmu.mmu_machine.get_status(0)["unit_0"]
            self.assertEqual(unit["td1_device"], "BENCH")
            self.assertEqual(unit["td1_devices"], ["SERIAL_A"] * 4)
            mgr = hh.mmu.mmu_unit(0).td1_manager
            self.assertEqual(mgr.shared_device.serial, "BENCH")
            self.assertEqual([mgr.serial_for(g) for g in range(4)], ["SERIAL_A"] * 4)

    def test_per_gate_template(self):
        profile = PROFILE.derive("td1_per_gate", syms=PER_GATE_SYMS)
        rendered = cfg.assemble(cfg.render(profile))
        unit = rendered["mmu_unit unit0"]
        self.assertEqual(unit.get("td1_devices"), "A, B, , A")
        self.assertNotIn("td1_device", unit)

    def test_tunables_live_on_unit_parameters(self):
        # Reachable through the ordinary parameter machinery, so MMU_TEST_CONFIG and
        # MMU_STATUS work on them exactly as they do for every other unit tunable
        rendered = cfg.assemble(cfg.render(PROFILE))
        params = rendered["mmu_unit_parameters unit0"]
        self.assertEqual(params.get("td1_capture_timeout"), "1")
        self.assertEqual(params.get("td1_auto_update"), "0")
        self.hh.run_gcode("MMU_TEST_CONFIG TD1_CAPTURE_TIMEOUT=4")
        self.assertEqual(self.mmu.mmu_unit(0).p.td1_capture_timeout, 4.)
        self.assertEqual(self.hh.errors, [])


class TestTd1Apply(Td1Case):
    def test_register_preserves_filament_color_and_persists(self):
        self.mmu.gate_maps.gate_color[2] = "ff0000"
        self.hh.run_gcode("MMU_TD1 GATE=2 REGISTER=1")
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.gate_td, [None, None, 4., None])
        self.assertEqual(self.mmu.gate_td1_color[2], "123456")
        self.assertEqual(self.mmu.gate_color[2], "ff0000")
        self.assertEqual(self.mmu.var_manager.get("mmu_state_gate_td", None),
                         [None, None, 4., None])

    def test_an_identical_reading_changes_nothing(self):
        # Nothing new to record: no persistence, no LED repaint, no lane-data push
        self.manager.apply(0, record())
        with patch.object(self.mmu.gate_maps, "persist_gate_map") as persist:
            self.manager.apply(0, record(second=1))
        persist.assert_not_called()
        # The time recorded is when the value was established, not last re-confirmed


class TestTd1Invalidate(Td1Case):
    def test_spool_replacement_and_empty_clear_measurements(self):
        revision = self.manager.revisions[0]
        self.manager.apply(0, record())
        self.owner(0)
        self.mmu.gate_maps.assign_spool_id(0, 99)
        self.assertIsNone(self.mmu.gate_td[0])
        self.assertEqual(self.mmu.gate_td1_color[0], "")
        self.assertEqual(self.owners(), {})
        self.assertEqual(self.manager.revisions[0], revision + 1)

    def test_the_event_clears_measurement_and_attribution(self):
        self.manager.apply(2, record())
        self.owner(2)
        revision = self.manager.revisions[2]
        self.mmu.gate_maps.gate_filament_changed(2)
        # The gate map owns and clears the measurement...
        self.assertIsNone(self.mmu.gate_td[2])
        self.assertEqual(self.mmu.gate_td1_color[2], "")
        # ...and TD-1 drops only what it derived from that identity
        self.assertEqual(self.manager.revisions[2], revision + 1)
        self.assertEqual(self.owners(), {})


class TestTd1Color(Td1Case):
    """
    A measured color fills in filament_color, it does not compete with it.

    Adopted into gate_color when nothing else has claimed that field, so every existing
    color consumer sees it through the ordinary route.
    """

    def test_it_fills_an_empty_filament_color(self):
        self.assertEqual(self.mmu.gate_color[0], "")
        self.manager.apply(0, record(color="00ff00"))
        self.assertEqual(self.mmu.gate_color[0], rgba("00ff00"))
        self.assertEqual(self.mmu.gate_color_rgb[0], (0., 1., 0.))
        # ...and the raw measurement is still recorded, so provenance is not lost
        self.assertEqual(self.mmu.gate_td1_color[0], "00ff00")

    def test_it_never_overwrites_a_color_that_is_already_set(self):
        # Spoolman's or the user's color both beat a scanner's guess
        self.mmu.gate_maps.gate_color[1] = "ff0000"
        self.manager.apply(1, record(color="00ff00"))
        self.assertEqual(self.mmu.gate_color[1], "ff0000")
        self.assertEqual(self.mmu.gate_td1_color[1], "00ff00")

    def test_black_is_a_measurement_not_a_missing_one(self):
        self.manager.apply(0, record(color="000000"))
        self.assertEqual(self.mmu.gate_td1_color[0], "000000")
        self.assertEqual(self.mmu.gate_color[0], rgba("000000"))
        self.assertEqual(self.mmu.gate_color_rgb[0], (0., 0., 0.))


class TestTd1SetColor(Td1Case):
    def test_it_overrides_a_color_that_is_already_set(self):
        self.mmu.gate_maps.gate_color[1] = "ff0000"
        self.manager.apply(1, record(color="00ff00"))
        self.assertEqual(self.mmu.gate_color[1], "ff0000")
        self.hh.run_gcode("MMU_TD1 GATE=1 SET_COLOR=1 QUIET=1")
        self.assertEqual(self.mmu.gate_color[1], rgba("00ff00"))

    def test_it_takes_a_list(self):
        for gate in (0, 1, 2):
            self.mmu.gate_maps.gate_color[gate] = "ff0000"
            self.manager.apply(gate, record(color="00ff00"))
        self.hh.run_gcode("MMU_TD1 GATES=0,2 SET_COLOR=1 QUIET=1")
        self.assertEqual([self.mmu.gate_color[g] for g in (0, 1, 2)],
                         [rgba("00ff00"), "ff0000", rgba("00ff00")])

    def test_it_says_so_when_there_is_nothing_measured(self):
        with patch.object(self.mmu, "log_info") as info:
            self.hh.run_gcode("MMU_TD1 GATE=3 SET_COLOR=1 QUIET=1")
        self.assertTrue(any("No measured color" in c.args[0] for c in info.call_args_list))

    def test_it_warns_that_spoolman_will_win_again(self):
        self.mmu.gate_maps.assign_spool_id(0, 42)
        self.manager.apply(0, record(color="00ff00"))
        self.mmu.gate_maps.gate_color[0] = "ff0000"
        with patch.object(self.mmu, "log_warning") as warning:
            self.hh.run_gcode("MMU_TD1 GATE=0 SET_COLOR=1 QUIET=1")
        self.assertTrue(any("Spoolman refresh" in c.args[0] for c in warning.call_args_list))

    def test_it_needs_a_gate(self):
        with self.assertRaisesRegex(Exception, "SET_COLOR=1 needs GATE"):
            self.hh.run_gcode("MMU_TD1 SET_COLOR=1")


class TestTd1Staleness(Td1Case):
    def test_unmeasured_is_the_whole_staleness_rule(self):
        # No timestamps, no thresholds: a gate either has a measurement for the filament
        # currently in it, or it doesn't
        self.assertTrue(self.manager.needs_measurement(0))
        self.manager.apply(0, record())
        self.assertFalse(self.manager.needs_measurement(0))

    def test_identity_change_makes_a_gate_stale_again(self):
        self.manager.apply(0, record())
        self.mmu.gate_maps.assign_spool_id(0, 99)
        self.assertTrue(self.manager.needs_measurement(0))

    def test_a_gate_without_a_scanner_is_never_stale(self):
        self.manager.gate_devices[1] = None
        self.assertFalse(self.manager.needs_measurement(1))


class TestTd1UpdateDevices(Td1Case):
    def test_auto_only_updates_known_owner_and_new_record(self):
        self.bridge.refresh()
        device = self.bridge.devices["SERIAL_A"]
        device.auto_override = True
        self.mmu.select_gate(0)
        self.mmu.set_filament_pos_state(10)
        self.owner(0)
        self.bridge.update_devices({"SERIAL_A": record(second=1)})
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.bridge.update_devices({"SERIAL_A": record(second=1, td=9.)})
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.clear_owners()
        self.bridge.update_devices({"SERIAL_A": record(second=2, td=9.)})
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.assertEqual(device.td, 9.)
        self.bridge.update_devices({})
        self.assertFalse(device.connected)

    def test_invalid_data_and_unknown_device_are_status_only(self):
        self.bridge.update_devices({"B": record(), "SERIAL_A": {"error": "bad optics"}})
        self.assertEqual(self.bridge.devices["SERIAL_A"].error, "bad optics")
        self.assertEqual(self.bridge.devices["SERIAL_A"].error_kind, "device")
        self.assertEqual(self.bridge.devices["B"].td, 4.)
        self.assertEqual(self.mmu.gate_td, [None] * 4)

    def test_unmeasured_is_classified_not_matched_on_message(self):
        # device() must tell "nothing measured yet" from "measured badly" structurally
        from extras.mmu.mmu_td1 import TD1_ERR_NO_READING, TD1_ERR_INVALID
        self.bridge.update_devices({"SERIAL_A": {}})
        device = self.bridge.devices["SERIAL_A"]
        self.assertEqual(device.error_kind, TD1_ERR_NO_READING)
        self.manager.device(0) # Healthy: simply hasn't seen filament yet
        self.bridge.update_devices({"SERIAL_A": {"td": "nonsense"}})
        self.assertEqual(device.error_kind, TD1_ERR_INVALID)
        with self.assertRaises(Exception):
            self.manager.device(0)


class TestTd1Callback(Td1Case):
    def test_late_callback_is_ignored(self):
        request = Request(request_id=999, devices=self.data)
        self.bridge._callback(request)
        self.assertEqual(request.response, {})
        self.assertIsNone(self.bridge.devices["SERIAL_A"].td)


class TestTd1Command(Td1Case):
    def test_status_has_no_gate_side_effects(self):
        self.hh.run_gcode("MMU_TD1 READ=1")
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.gate_td, [None] * 4)

    def test_enable_and_auto_control(self):
        self.hh.run_gcode("MMU_TD1 GATE=0 ENABLE=0")
        self.assertFalse(self.bridge.devices["SERIAL_A"].enabled)
        self.hh.run_gcode("MMU_TD1 SERIAL=SERIAL_A ENABLE=1")
        self.hh.run_gcode("MMU_TD1 GATES=0,1 AUTO=1")
        self.assertTrue(self.bridge.devices["SERIAL_A"].auto_override)
        self.assertEqual(self.hh.errors, [])

    def test_the_command_never_moves_filament(self):
        # Every motion operation now lives in MMU_CHECK_GATE TD1=1
        for args in ("SCAN=1", "GATE=0 SCAN=1", "GATE=0 CALIBRATE=1"):
            with self.subTest(args=args):
                self.hh.run_gcode("MMU_TD1 " + args) # Unknown params are simply ignored
        self.assertEqual(self.hh.filament().history, [])

    def test_status_report_is_formatted_not_json(self):
        """One line per reader, grouped by unit - the shape MMU_NFC reports in."""
        self.bridge.update_devices({"SERIAL_A": record()})
        self.manager.apply(0, record())
        with patch.object(self.mmu, "log_always") as report:
            self.hh.run_gcode("MMU_TD1 DETAILS=1")
        message = "\n".join(c.args[0] for c in report.call_args_list)
        self.assertIn("MMU TD-1 readers:", message)
        # DETAILS appends to the row, it does not reorder it
        self.assertIn("gate 0:   enabled=1, connected=1, mode=manual, td=4.00, "
                      "td-color=123456, serial=SERIAL_A, last_measured=", message)
        rows = [l for l in message.splitlines() if l.startswith("gate ")]
        self.assertTrue(rows and all(", serial=SERIAL_A, last_measured=" in l for l in rows),
                        message)
        self.assertIn("scanned at 2026-09-19T00:00:00+00:00", message)
        self.assertNotIn("{", message)

    def test_a_row_quotes_its_own_gate_not_the_device(self):
        """
        Four gates behind one bowden scanner must not all print the scanner's latest
        reading - the question "what has gate 2 measured" is the one being asked.
        """
        self.bridge.update_devices({"SERIAL_A": record()})
        self.manager.apply(1, record(td=7.5, color="abcdef"))
        with patch.object(self.mmu, "log_always") as report:
            self.hh.run_gcode("MMU_TD1")
        message = "\n".join(c.args[0] for c in report.call_args_list)
        self.assertIn("gate 1:   enabled=1, connected=1, mode=manual, "
                      "td=7.50, td-color=abcdef", message)
        self.assertIn("gate 2:   enabled=1, connected=1, mode=manual, "
                      "td=None, td-color=None", message)
        self.assertIn("serial=SERIAL_A", message, "the serial is always shown")
        self.assertNotIn("last_measured=", message, "the scan time belongs to DETAILS")


class TestTd1Moonraker(unittest.TestCase):
    def test_read_and_failure_return_over_webhook(self):
        with harness() as hh:
            transport = SimpleNamespace(call_method=AsyncMock(
                return_value={"devices": {"A": record()}}))
            hh.server.components["internal_transport"] = transport
            send = AsyncMock()
            hh.server.klippy_apis._send_klippy_request = send
            hh.call_remote("mmu_td1_request", request_id=1)
            send.assert_awaited_once_with(
                "mmu/td1", {"request_id": 1, "devices": {"A": record()}, "error": None})
            self.assertEqual(hh.gcode(), [])
            transport.call_method.side_effect = ValueError("offline")
            hh.call_remote("mmu_td1_request", request_id=2)
            self.assertEqual(send.await_args.args[1]["error"], "offline")


class TestTd1Restore(Td1Case):
    def test_reassigning_a_scanner_does_not_discard_measurements(self):
        # TD is a property of the filament, not of the device that read it. Rewiring
        # scanners must not silently wipe what the machine already measured
        self.manager.apply(0, record())
        self.manager.gate_devices[0] = self.bridge._adopt("A_DIFFERENT_SCANNER")
        errors = self.mmu.gate_maps.load_persisted_state()
        self.assertEqual(errors, [])
        self.assertEqual(self.mmu.gate_td[0], 4.)


class TestTd1Poll(Td1Case):
    def test_poll_then_disconnect_and_expiry(self):
        now = self.hh.reactor.monotonic()
        self.bridge._poll(now)
        self.assertEqual(self.bridge.pending, {})
        self.assertEqual(self.bridge.devices["SERIAL_A"].td, 4.)
        self.bridge.pending[500] = {"deadline": now - 1, "done": False}
        self.bridge._poll(now)
        self.assertFalse(self.bridge.devices["SERIAL_A"].connected)
        self.owner(0)
        self.bridge._disconnect()
        self.assertFalse(self.bridge.connected)
        self.assertEqual(self.owners(), {})
        self.assertEqual(self.bridge._poll(now), self.hh.reactor.NEVER)

    def test_poll_backs_off_when_nothing_consumes_readings(self):
        from extras.mmu.mmu_td1 import TD1_POLL_ACTIVE, TD1_POLL_IDLE
        now = self.hh.reactor.monotonic()
        # Nothing is auto-updating and nothing is capturing: don't hammer Moonraker
        self.assertEqual(self.bridge._poll(now), now + TD1_POLL_IDLE)
        self.bridge.devices["SERIAL_A"].auto_override = True
        self.bridge.pending.clear()
        self.assertEqual(self.bridge._poll(now), now + TD1_POLL_ACTIVE)
        self.bridge.devices["SERIAL_A"].auto_override = False
        self.bridge.pending.clear()
        self.owner(0)
        self.assertEqual(self.bridge._poll(now), now + TD1_POLL_ACTIVE)

    def test_missing_remote_method_is_nonfatal(self):
        with patch.object(self.hh.webhooks, "call_remote_method",
                          side_effect=self.mmu.printer.command_error("missing")):
            self.bridge._poll(self.hh.reactor.monotonic())
        self.assertEqual(self.bridge.pending, {})


class TestTd1Refresh(Td1Case):
    def test_timeout_cleans_pending(self):
        from extras.mmu.mmu_td1 import MmuTd1BridgeError
        self.hh.webhooks.sink = lambda *args: None
        with self.assertRaisesRegex(MmuTd1BridgeError, "timed out"):
            self.bridge.refresh()
        self.assertEqual(self.bridge.pending, {})

    def test_failure_and_reset(self):
        from extras.mmu.mmu_td1 import MmuTd1BridgeError
        def failed(method, values):
            if method == "mmu_td1_request":
                self.bridge._callback(Request(request_id=values["request_id"],
                                               error="offline", devices={}))
        self.hh.webhooks.sink = failed
        with self.assertRaisesRegex(MmuTd1BridgeError, "offline"):
            self.bridge.refresh()
        self.assertFalse(self.bridge.devices["SERIAL_A"].connected)
        self.hh.webhooks.sink = self.respond
        self.bridge.pending.clear()
        self.bridge.refresh("SERIAL_A", reset=True)
        self.assertTrue(self.bridge.devices["SERIAL_A"].connected)
        self.assertTrue(self.hh.webhooks.calls_to("mmu_td1_request")[-1]["reset"])


class TestTd1Device(Td1Case):
    def test_missing_disabled_disconnected_and_error(self):
        from extras.mmu.mmu_utils import MmuError
        device = self.bridge.devices["SERIAL_A"]
        with self.assertRaisesRegex(MmuError, "disconnected"):
            self.manager.device(0)
        device.enabled = False
        with self.assertRaisesRegex(MmuError, "disabled"):
            self.manager.device(0)
        device.enabled, device.connected = True, True
        device.error, device.error_kind = "optics", "device"
        with self.assertRaisesRegex(MmuError, "optics"):
            self.manager.device(0)
        self.manager.gate_devices[0] = None
        with self.assertRaisesRegex(MmuError, "no scanner"):
            self.manager.device(0)


class TestTd1WaitMeasurement(Td1Case):
    def test_every_round_trip_is_capped_to_the_time_remaining(self):
        # The default bridge deadline is longer than the default capture timeout, so an
        # uncapped round trip would silently overrun the caller's own deadline
        from extras.mmu.mmu_td1 import MmuTd1NoReading, TD1_REQUEST_TIMEOUT
        self.bridge.update_devices({"SERIAL_A": record()})
        timeouts = []
        original = self.bridge.refresh
        def capped(*args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return original(*args, **kwargs)
        with patch.object(self.bridge, "refresh", side_effect=capped):
            with self.assertRaises(MmuTd1NoReading):
                self.manager.wait_measurement(0, "2026-09-19T00:00:00+00:00", 1.)
        self.assertTrue(timeouts)
        self.assertLess(max(timeouts), TD1_REQUEST_TIMEOUT)
        self.assertLessEqual(max(timeouts), 1.)

    def test_a_transport_stall_is_not_a_hard_failure(self):
        # One slow round trip inside a longer wait is just "no reading yet"
        from extras.mmu.mmu_td1 import MmuTd1BridgeTimeout
        self.bridge.update_devices({"SERIAL_A": record()})
        attempts = []
        def flaky(*args, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise MmuTd1BridgeTimeout("TD-1: Moonraker response timed out")
            self.bridge.update_devices({"SERIAL_A": record(second=5)})
        with patch.object(self.bridge, "refresh", side_effect=flaky):
            result = self.manager.wait_measurement(0, "2026-09-19T00:00:00+00:00", 5.)
        self.assertEqual(result["scan_time"], "2026-09-19T00:00:05+00:00")
        self.assertEqual(len(attempts), 3)

    def test_an_unusable_bridge_propagates_instead_of_spinning(self):
        from extras.mmu.mmu_td1 import MmuTd1BridgeError
        self.bridge.update_devices({"SERIAL_A": record()})
        with patch.object(self.bridge, "refresh",
                          side_effect=MmuTd1BridgeError("TD-1: bridge unavailable")):
            with self.assertRaisesRegex(MmuTd1BridgeError, "bridge unavailable"):
                self.manager.wait_measurement(0, "2026-09-19T00:00:00+00:00", 5.)

    def test_returns_immediately_when_a_newer_reading_is_already_cached(self):
        self.bridge.update_devices({"SERIAL_A": record(second=5)})
        with patch.object(self.bridge, "refresh", side_effect=AssertionError("refreshed")):
            result = self.manager.wait_measurement(0, "2026-09-19T00:00:00+00:00", 5.)
        self.assertEqual(result["scan_time"], "2026-09-19T00:00:05+00:00")


class TestTd1BeginLoad(Td1Case):
    def test_disabled_and_unconfigured_skip(self):
        self.assertIsNone(self.manager.begin_load(-1))
        self.assertIsNone(self.manager.begin_load(0))
        self.bridge.devices["SERIAL_A"].auto_override = True
        self.bridge.devices["SERIAL_A"].enabled = False
        self.assertIsNone(self.manager.begin_load(0))
        self.manager.gate_devices[1] = None
        self.assertIsNone(self.manager.begin_load(1))
        self.assertEqual(self.owners(), {})

    def test_arms_without_a_blocking_bridge_call(self):
        # begin_load runs inside every tool change - it must never wait on Moonraker
        self.param(0, "td1_capture_on_load", 1)
        self.bridge.update_devices({"SERIAL_A": record()})
        with patch.object(self.bridge, "refresh", side_effect=AssertionError("blocked")):
            token = self.manager.begin_load(0)
        self.assertEqual(token["gate"], 0)
        self.assertTrue(token["capture"])
        self.assertIs(self.owners()["SERIAL_A"], token)

    def test_unhealthy_device_disarms_quietly(self):
        self.param(0, "td1_capture_on_load", 1)
        self.bridge.update_devices({}) # Disconnected
        with patch.object(self.mmu, "log_debug") as debug:
            self.assertIsNone(self.manager.begin_load(0))
        self.assertEqual(self.owners(), {})
        self.assertTrue(any("disconnected" in c.args[0] for c in debug.call_args_list))


class TestTd1EndLoad(Td1Case):
    def test_success_and_failure(self):
        self.param(0, "td1_capture_on_load", 1)
        self.mmu.select_gate(0)
        self.bridge.update_devices({"SERIAL_A": record()})
        token = self.manager.begin_load(0)
        self.bridge.update_devices({"SERIAL_A": record(second=1)})
        self.manager.end_load(token, True)
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.manager.end_load(token, False)
        self.assertEqual(self.owners(), {})
        self.manager.end_load(None, True)

    def test_identity_change_and_timeout_do_not_apply(self):
        from extras.mmu.mmu_td1 import MmuTd1NoReading
        self.mmu.select_gate(0)
        self.param(0, "td1_capture_on_load", 1)
        self.bridge.update_devices({"SERIAL_A": record()})
        token = self.manager.begin_load(0)
        self.manager.filament_changed(0)
        self.manager.end_load(token, True)
        self.assertIsNone(self.mmu.gate_td[0])
        token = self.manager.begin_load(0)
        with patch.object(self.manager, "wait_measurement",
                          side_effect=MmuTd1NoReading("no scan")), \
                patch.object(self.mmu, "log_warning") as warning:
            self.manager.end_load(token, True)
        warning.assert_called_once_with("no scan")
        self.assertIsNone(self.mmu.gate_td[0])

    def test_printing_never_waits_and_leaves_attribution_armed(self):
        # Tool changes must not pay for TD-1: stay armed and let the poller deliver
        self.mmu.select_gate(0)
        self.param(0, "td1_capture_on_load", 1)
        self.bridge.update_devices({"SERIAL_A": record()})
        token = self.manager.begin_load(0)
        with patch.object(self.mmu, "is_printing", return_value=True), \
                patch.object(self.manager, "wait_measurement") as wait:
            self.manager.end_load(token, True)
        wait.assert_not_called()
        self.assertIs(self.owners()["SERIAL_A"], token)
        self.mmu.set_filament_pos_state(10)
        self.bridge.update_devices({"SERIAL_A": record(second=1)})
        self.assertEqual(self.mmu.gate_td[0], 4.)


class TestTd1CommandSelection(Td1Case):
    def test_invalid_combinations_and_lists_are_reported(self):
        # Argument validation uses gcmd.error (the house convention), which surfaces as
        # an ordinary Klipper command error rather than an MMU pause
        for args, message in (
                ("GATE=0 GATES=1", "only one of GATE"),
                ("GATES=no", "Invalid GATES"),
                ("GATE=0,1", "Use GATES for a list"),
                ("GATES=99", r"Invalid gate\(s\)"),
                ("REGISTER=1", "exactly one explicit GATE"),
                ("GATES=0,1 REGISTER=1", "exactly one explicit GATE"),
                ("GATE=0 SERIAL=A", "only one of SHARED"),
                ("SHARED=1 GATE=0", "only one of SHARED"),
                ("REGISTER=1 INIT=1", "only one operation"),
                ("SERIAL=UNKNOWN", "Unknown TD-1 device"),
                ("ENABLE=0", "Select a gate or device")):
            with self.subTest(args=args):
                with self.assertRaisesRegex(Exception, message):
                    self.hh.run_gcode("MMU_TD1 " + args)
        self.assertEqual(self.hh.filament().history, [])

    def test_register_warns_when_the_gate_has_no_identity_yet(self):
        # Assigning a spool or tag later clears the measurement, so registering first
        # silently loses it. The order isn't guessable - say so
        self.bridge.update_devices({"SERIAL_A": record()})
        with patch.object(self.mmu, "log_warning") as warning:
            self.hh.run_gcode("MMU_TD1 GATE=0 REGISTER=1")
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.assertTrue(any("no spool or tag assigned yet" in c.args[0]
                            for c in warning.call_args_list))
        # ...and the warning is right: assigning one does clear it
        self.hh.run_gcode("MMU_GATE_MAP GATE=0 SPOOLID=42 QUIET=1")
        self.assertIsNone(self.mmu.gate_td[0])

    def test_register_is_quiet_once_the_gate_has_a_spool(self):
        self.bridge.update_devices({"SERIAL_A": record()})
        self.mmu.gate_maps.assign_spool_id(0, 42)
        with patch.object(self.mmu, "log_warning") as warning:
            self.hh.run_gcode("MMU_TD1 GATE=0 REGISTER=1")
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.assertFalse(any("no spool or tag" in c.args[0]
                             for c in warning.call_args_list))

    def test_register_is_quiet_when_only_a_tag_is_known(self):
        # Spoolman off, NFC only: a tag is identity enough
        self.bridge.update_devices({"SERIAL_A": record()})
        self.mmu.gate_maps.set_gate_rfid(1, "04a1b2c3")
        with patch.object(self.mmu, "log_warning") as warning:
            self.hh.run_gcode("MMU_TD1 GATE=1 REGISTER=1")
        self.assertEqual(self.mmu.gate_td[1], 4.)
        self.assertFalse(any("no spool or tag" in c.args[0]
                             for c in warning.call_args_list))

    def test_register_on_a_gate_without_a_scanner_says_so(self):
        self.manager.gate_devices[1] = None
        with self.assertRaisesRegex(Exception, r"No TD-1 scanner configured for gate\(s\) 1"):
            self.hh.run_gcode("MMU_TD1 GATE=1 REGISTER=1")

    def test_bare_status_on_a_gate_without_a_scanner_just_says_so(self):
        self.manager.gate_devices[1] = None
        with patch.object(self.mmu, "log_info") as info:
            self.hh.run_gcode("MMU_TD1 GATE=1")
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(any("No TD-1 scanner configured for gate(s) 1" in c.args[0]
                            for c in info.call_args_list))


class TestTd1ManualMap(Td1Case):
    def test_manual_td_and_clear(self):
        self.hh.run_gcode("MMU_GATE_MAP GATE=0 TD=3.5")
        self.assertEqual(self.mmu.gate_td[0], 3.5)
        self.hh.run_gcode("MMU_GATE_MAP GATE=0 TD=")
        self.assertIsNone(self.mmu.gate_td[0])
        self.assertEqual(self.hh.errors, [])

    def test_manual_td_supersedes_a_measurement_and_is_a_no_op_when_unchanged(self):
        self.manager.apply(0, record())
        self.hh.run_gcode("MMU_GATE_MAP GATE=0 TD=3.5")
        self.assertEqual(self.mmu.gate_td[0], 3.5)
        self.assertEqual(self.mmu.gate_td1_color[0], "")
        revision = self.manager.revisions[0]
        self.hh.run_gcode("MMU_GATE_MAP GATE=0 TD=3.5")
        self.assertEqual(self.manager.revisions[0], revision)

    def test_td_is_editable_in_spoolman_pull_mode(self):
        self.mmu.p.spoolman_support = "pull"
        self.hh.run_gcode("MMU_GATE_MAP GATE=0 TD=2.5")
        self.assertEqual(self.mmu.gate_td[0], 2.5)
        self.assertEqual(self.hh.errors, [])

    def test_details_shows_measurement(self):
        self.manager.apply(0, record())
        text = self.mmu.gate_maps.gate_map_to_string(details=True)
        self.assertIn("TD: 4.00", text)
        self.assertIn("MEASURED COLOR: 123456", text)


class TestTd1Led(Td1Case):
    def test_a_measurement_reaches_the_leds_through_filament_color(self):
        # No separate effect - the existing one shows it with no extra wiring
        self.hh.reactor.advance(12)
        self.mmu.gate_maps.gate_status[0] = 1
        self.hh.run_gcode("MMU_LED EXIT_EFFECT=filament_color")
        unit = self.mmu.mmu_unit(0)
        before = unit.leds.virtual_chains["exit"].get_status()["color_data"][:]
        self.manager.apply(0, record(color="00ff00"))
        after = unit.leds.virtual_chains["exit"].get_status()["color_data"][:]
        self.assertNotEqual(before, after)
        self.assertGreater(after[0][1], after[0][0])
        self.assertEqual(self.mmu.gate_color[0], rgba("00ff00"))


class TestTd1MultiUnit(unittest.TestCase):
    def test_shared_device_is_unique_and_distances_are_per_gate(self):
        profile = profiles.clone_across_units("td1_multi", PROFILE, ["unit0", "unit1"])
        with session(profile) as hh:
            hh.boot(calibrate=True)
            self.assertEqual(hh.errors, [])
            bridge = hh.mmu.td1
            self.assertEqual(list(bridge.devices), ["SERIAL_A"])
            self.assertEqual(bridge.gates_for("SERIAL_A"), list(range(8)))
            # One serial named by two units is ONE device object, so the debt and the
            # armed token are shared rather than duplicated per unit
            devices = {id(hh.mmu.mmu_unit(g).td1_manager.device_for(g)) for g in range(8)}
            self.assertEqual(len(devices), 1)


class TestTd1CheckGate(Td1Case):
    def test_combined_check_traverses_the_path_and_parks(self):
        # No scan geometry: the filament is run down to the extruder (never into it, so
        # nothing has to be heated) and back, passing the scanner wherever it sits
        self.hh.place_filament(0)
        self.mmu.gate_maps.set_gate_status(0, 1)
        with patch.object(self.mmu, "load_sequence", wraps=self.mmu.load_sequence) as load, \
                patch.object(self.manager, "wait_measurement", return_value=record(second=1)):
            self.hh.run_gcode("MMU_CHECK_GATE GATE=0 TD1=1")
        self.assertEqual(self.hh.errors, [])
        load.assert_called_once_with(skip_extruder=True)
        self.assertEqual(self.mmu.gate_td[0], 4.)
        # The traverse genuinely leaves filament in the buffer, so the gate says so
        self.assertEqual(self.mmu.gate_status[0], 2)
        self.assertEqual(self.mmu.filament_pos, 0)

    def test_already_measured_gates_are_skipped_unless_forced(self):
        self.hh.place_filament(0)
        self.mmu.gate_maps.set_gate_status(0, 1)
        self.manager.apply(0, record())
        with patch.object(self.mmu, "load_sequence") as load:
            self.hh.run_gcode("MMU_CHECK_GATE GATE=0 TD1=1")
        load.assert_not_called() # Already has a reading for this filament
        self.assertEqual(self.mmu.gate_status[0], 1)
        with patch.object(self.mmu, "load_sequence", wraps=self.mmu.load_sequence) as load, \
                patch.object(self.manager, "wait_measurement",
                             return_value=record(second=9, td=7.)):
            self.hh.run_gcode("MMU_CHECK_GATE GATE=0 TD1_UPDATE=1")
        load.assert_called_once_with(skip_extruder=True)
        self.assertEqual(self.mmu.gate_td[0], 7.)

    def test_a_changed_spool_makes_the_gate_stale_again(self):
        self.hh.place_filament(0)
        self.mmu.gate_maps.set_gate_status(0, 1)
        self.manager.apply(0, record())
        self.mmu.gate_maps.assign_spool_id(0, 42)
        with patch.object(self.mmu, "load_sequence", wraps=self.mmu.load_sequence) as load, \
                patch.object(self.manager, "wait_measurement", return_value=record(second=1)):
            self.hh.run_gcode("MMU_CHECK_GATE GATE=0 TD1=1")
        load.assert_called_once_with(skip_extruder=True)

    def test_failed_measurement_still_marks_the_gate_available(self):
        # Homing proved the filament is there. A scanner that didn't produce a reading
        # is not evidence of an empty gate, and must not cost the gate its availability
        from extras.mmu.mmu_td1 import MmuTd1NoReading
        self.hh.place_filament(0)
        self.mmu.gate_maps.set_gate_status(0, -1)
        with patch.object(self.manager, "wait_measurement",
                          side_effect=MmuTd1NoReading("TD-1: no fresh measurement")):
            self.hh.run_gcode("MMU_CHECK_GATE GATE=0 TD1=1")
        self.assertEqual(self.hh.errors, [])
        self.assertGreaterEqual(self.mmu.gate_status[0], 1)
        self.assertIsNone(self.mmu.gate_td[0])
        self.assertEqual(self.mmu.filament_pos, 0)

    def test_stale_empty_gate_is_promoted_to_available(self):
        # The classic workflow: gate marked EMPTY after a runout, user reloads it,
        # runs MMU_CHECK_GATE. TD1=1 must not change that outcome
        self.hh.place_filament(0)
        self.mmu.gate_maps.set_gate_status(0, 0)
        with patch.object(self.manager, "wait_measurement", return_value=record(second=1)):
            self.hh.run_gcode("MMU_CHECK_GATE GATE=0 TD1=1")
        self.assertEqual(self.hh.errors, [])
        self.assertGreaterEqual(self.mmu.gate_status[0], 1)
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.assertEqual(self.mmu.filament_pos, 0)

    def test_measuring_works_during_a_print(self):
        # The pre-print check in _MMU_PRINT_START runs with print_state 'started', which
        # counts as printing. Measuring there is the main reason to use TD1= at all, and
        # the traverse never enters the extruder so it needs no heat
        self.hh.place_filament(0)
        self.mmu.gate_maps.set_gate_status(0, -1)
        extruder = self.mmu.printer.lookup_object('extruder').get_status(0)
        self.assertEqual(extruder.get('target'), 0.)
        with patch.object(self.mmu, "is_printing", return_value=True), \
                patch.object(self.manager, "wait_measurement", return_value=record(second=1)), \
                patch.object(self.mmu, "load_sequence", wraps=self.mmu.load_sequence) as load:
            self.hh.run_gcode("MMU_CHECK_GATE GATE=0 TD1=1")
        self.assertEqual(self.hh.errors, [])
        load.assert_called_once_with(skip_extruder=True)
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.assertEqual(self.mmu.filament_pos, 0)
        # Still cold - nothing was heated to do this
        self.assertEqual(
            self.mmu.printer.lookup_object('extruder').get_status(0).get('target'), 0.)

    def test_unusable_bridge_degrades_the_whole_batch_once(self):
        from extras.mmu.mmu_td1 import MmuTd1BridgeError
        self.hh.place_filament(0)
        self.mmu.gate_maps.set_gate_status(0, -1)
        with patch.object(self.bridge, "refresh",
                          side_effect=MmuTd1BridgeError("TD-1: bridge unavailable")), \
                patch.object(self.mmu, "load_sequence") as load:
            self.hh.run_gcode("MMU_CHECK_GATE GATE=0 TD1=1")
        load.assert_not_called()
        self.assertEqual(self.mmu.gate_status[0], 1)
        self.assertEqual(self.hh.errors, [])

    def test_batch_selects_each_gate_once(self):
        # scan() must not restore the previous gate mid-batch: MMU_CHECK_GATE owns
        # selection, and on a physical selector the extra moves are real wear
        self.hh.place_filament(0)
        self.hh.place_filament(1)
        self.mmu.gate_maps.gate_status = [1, 1, -1, -1]
        with patch.object(self.manager, "wait_measurement", return_value=record(second=1)), \
                patch.object(self.mmu, "select_gate", wraps=self.mmu.select_gate) as select:
            self.hh.run_gcode("MMU_CHECK_GATE GATES=0,1 TD1=1")
        self.assertEqual(self.hh.errors, [])
        selected = [c.args[0] for c in select.call_args_list]
        self.assertEqual(selected[:2], [0, 1])
        self.assertTrue(all(a != b for a, b in zip(selected, selected[1:])),
                        "gate re-selected without moving on: %s" % selected)
        self.assertEqual(self.mmu.gate_td[:2], [4., 4.])

    def test_unconfigured_gate_uses_normal_availability_check(self):
        self.hh.place_filament(0)
        self.manager.gate_devices[0] = None
        self.hh.run_gcode("MMU_CHECK_GATE GATE=0 TD1=1")
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.gate_status[0], 1)
        self.assertEqual(self.mmu.filament_pos, 0)


class TestTd1AutoGuards(Td1Case):
    def test_each_owner_condition_prevents_automatic_application(self):
        from extras.mmu.mmu_constants import FILAMENT_POS_LOADED
        scenarios = ("unknown", "disabled", "manual", "reconnect", "same", "busy",
                     "revision", "selected", "unloaded")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.manager.filament_changed(0)
                self.bridge.update_devices({"SERIAL_A": record()})
                self.mmu.gate_selected = 0
                self.mmu.filament_pos = FILAMENT_POS_LOADED
                device = self.bridge.devices["SERIAL_A"]
                device.enabled = device.connected = True
                device.auto_override = True
                self.bridge.busy = False
                self.owner(0)
                data = record(second=1)
                if scenario == "unknown":
                    self.clear_owners()
                elif scenario == "disabled":
                    device.enabled = False
                elif scenario == "manual":
                    device.auto_override = False
                elif scenario == "reconnect":
                    device.connected = False
                elif scenario == "same":
                    data = record()
                elif scenario == "busy":
                    self.bridge.busy = True
                elif scenario == "revision":
                    self.manager.revisions[0] += 1
                elif scenario == "selected":
                    self.mmu.gate_selected = 1
                elif scenario == "unloaded":
                    self.mmu.filament_pos = 0
                self.bridge.update_devices({"SERIAL_A": data})
                self.assertIsNone(self.mmu.gate_td[0])
                self.assertEqual(device.td, 4.)
        self.bridge.busy = False

    def test_capture_on_load_arms_attribution_without_auto(self):
        from extras.mmu.mmu_constants import FILAMENT_POS_LOADED
        self.bridge.update_devices({"SERIAL_A": record()})
        self.mmu.gate_selected = 0
        self.mmu.filament_pos = FILAMENT_POS_LOADED
        self.assertFalse(self.bridge.devices["SERIAL_A"].auto_override)
        self.owner(0, capture=True)
        self.bridge.update_devices({"SERIAL_A": record(second=1)})
        self.assertEqual(self.mmu.gate_td[0], 4.)

    def test_first_reading_with_known_owner_applies_and_disconnect_clears_owner(self):
        from extras.mmu.mmu_constants import FILAMENT_POS_LOADED
        self.bridge.update_devices({"SERIAL_A": {}})
        self.mmu.gate_selected = 0
        self.mmu.filament_pos = FILAMENT_POS_LOADED
        self.bridge.devices["SERIAL_A"].auto_override = True
        self.owner(0)
        self.bridge.update_devices({"SERIAL_A": record()})
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.assertEqual(self.bridge.devices["SERIAL_A"].last_outcome, "applied to gate 0")
        self.bridge.update_devices({})
        self.assertEqual(self.owners(), {})
        self.assertFalse(self.bridge.devices["SERIAL_A"].connected)
        self.assertEqual(self.mmu.gate_td[0], 4.)

    def test_malformed_payload_and_unassigned_discovery(self):
        self.bridge.update_devices([])
        self.assertEqual(self.bridge.devices["SERIAL_A"].error, "Invalid TD-1 device list")
        self.bridge.update_devices({"UNASSIGNED": record()})
        self.assertEqual(self.bridge.gates_for("UNASSIGNED"), [])
        self.assertEqual(self.mmu.gate_td, [None] * 4)


class TestTd1ResponseOrdering(Td1Case):
    def test_older_pending_reply_cannot_overwrite_newer_device_state(self):
        self.bridge.pending = {10: {"wait": True}, 11: {"wait": True}}
        self.bridge._callback(Request(request_id=11, devices={"SERIAL_A": record(td=7)}))
        self.bridge._callback(Request(request_id=10, devices={"SERIAL_A": record(td=3)}))
        self.assertTrue(self.bridge.pending[10]["done"])
        self.assertEqual(self.bridge.devices["SERIAL_A"].td, 7.)
        self.assertEqual(self.bridge.last_response, 11)

    def test_expired_poll_clears_ownership(self):
        self.owner(0)
        self.bridge.pending[90] = {"deadline": 0, "done": False}
        self.bridge._poll(self.hh.reactor.monotonic())
        self.assertEqual(self.owners(), {})
        self.assertEqual(self.bridge.devices["SERIAL_A"].last_outcome, "disconnected")


class TestTd1ConfigRender(unittest.TestCase):
    def test_disabled_shared_and_per_gate_with_capture_policy(self):
        disabled = cfg.render(profiles.get("boxturtle"))
        self.assertNotIn("td1_device", disabled["config/base/mmu_hardware.cfg"])
        self.assertNotIn("td1_scan_distance", disabled["config/base/mmu_parameters.cfg"])
        advanced = PROFILE.derive("td1_advanced", syms=dict(
            PER_GATE_SYMS,
            PARAM_TD1_CAPTURE_TIMEOUT="8",
            PARAM_TD1_AUTO_UPDATE=True, PARAM_TD1_CAPTURE_ON_LOAD=True))
        rendered = cfg.render(advanced)
        cfg.assert_sane(rendered)
        with session(advanced) as hh:
            hh.boot(calibrate=True)
            self.assertEqual(hh.errors, [])
            bridge = hh.mmu.td1
            manager = hh.mmu.mmu_unit(0).td1_manager
            p = hh.mmu.mmu_unit(0).p
            self.assertEqual([manager.serial_for(g) for g in range(4)], ["A", "B", "", "A"])
            self.assertEqual(p.td1_capture_timeout, 8)
            self.assertTrue(p.td1_capture_on_load)
            self.assertTrue(manager.auto_for(bridge.devices["A"]))


class TestTd1BridgeReboot(unittest.TestCase):
    def test_reboot_and_rediscovery(self):
        with harness() as hh:
            transport = SimpleNamespace(call_method=AsyncMock(side_effect=[
                {"status": "ok"}, {"devices": {}}, {"devices": {"A": record()}}]))
            hh.server.components["internal_transport"] = transport
            send = AsyncMock()
            hh.server.klippy_apis._send_klippy_request = send
            with patch("asyncio.sleep", new_callable=AsyncMock):
                hh.call_remote("mmu_td1_request", request_id=1, serial="A", reset=True)
            self.assertEqual(transport.call_method.await_args_list[0].args,
                             ("machine.td1.reboot", {"serial": "A"}))
            self.assertEqual(send.await_args.args[1],
                             {"request_id": 1, "devices": {"A": record()}, "error": None})

    def test_rejected_reboot_reports_error(self):
        with harness() as hh:
            hh.server.components["internal_transport"] = SimpleNamespace(
                call_method=AsyncMock(return_value={"status": "error"}))
            hh.server.klippy_apis._send_klippy_request = AsyncMock()
            hh.call_remote("mmu_td1_request", request_id=1, serial="A", reset=True)
            payload = hh.server.klippy_apis._send_klippy_request.await_args.args[1]
            self.assertEqual(payload["error"], "TD-1 reboot rejected")

    def test_missing_td1_component_is_reported_not_raised(self):
        with harness() as hh:
            hh.server.components.pop("internal_transport", None)
            hh.server.klippy_apis._send_klippy_request = AsyncMock()
            hh.call_remote("mmu_td1_request", request_id=1)
            payload = hh.server.klippy_apis._send_klippy_request.await_args.args[1]
            self.assertTrue(payload["error"])
            self.assertEqual(payload["devices"], {})


class TestTd1IdentityPersistence(Td1Case):
    def test_clearing_unassigned_spool_does_not_erase_other_gate_measurement(self):
        self.manager.apply(1, record())
        self.mmu.gate_maps.assign_spool_id(0, -1)
        self.assertEqual(self.mmu.gate_td[1], 4.)

    def test_same_spool_refresh_preserves_measurement_and_replacement_clears(self):
        self.mmu.p.spoolman_support = "pull"
        self.mmu.gate_maps.assign_spool_id(0, 5)
        self.manager.apply(0, record())
        self.hh.run_gcode(
            'MMU_GATE_MAP MAP="{0: {\'spool_id\': 5, \'color\': \'ff0000\'}}" '
            'REPLACE=1 FROM_SPOOLMAN=1 QUIET=1')
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.assertEqual(self.mmu.gate_color[0], "ff0000")
        self.hh.run_gcode(
            'MMU_GATE_MAP MAP="{0: {\'spool_id\': 6, \'color\': \'ffffff\'}}" '
            'REPLACE=1 FROM_SPOOLMAN=1 QUIET=1')
        self.assertIsNone(self.mmu.gate_td[0])
        self.assertEqual(self.mmu.gate_td1_color[0], "")
        self.assertEqual(self.hh.errors, [])


class TestTd1ExtraCommands(Td1Case):
    def test_init_details_quiet_and_unit_selector(self):
        self.hh.run_gcode("MMU_TD1 SERIAL=SERIAL_A INIT=1 QUIET=1")
        self.assertTrue(self.hh.webhooks.calls_to("mmu_td1_request")[-1]["reset"])
        self.hh.run_gcode("MMU_TD1 GATE=0 UNIT=0 DETAILS=1")
        self.assertEqual(self.hh.errors, [])
        # A disabled scanner is reported as such rather than raising
        self.bridge.set_device_state("SERIAL_A", enabled=False)
        with patch.object(self.mmu, "log_always") as report:
            self.hh.run_gcode("MMU_TD1 GATE=0")
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(any("enabled=0" in c.args[0] for c in report.call_args_list))

    def test_bare_status_works_without_the_bridge(self):
        # This is the command a user runs to find out why the bridge isn't working
        with patch.object(self.hh.webhooks, "call_remote_method",
                          side_effect=self.mmu.printer.command_error("not installed")), \
                patch.object(self.mmu, "log_always") as report:
            self.hh.run_gcode("MMU_TD1")
        self.assertEqual(self.hh.errors, [])
        message = "\n".join(c.args[0] for c in report.call_args_list)
        # Every row reports the scanner as unreachable rather than the command failing,
        # and names the USB device to go and check
        self.assertIn("connected=0", message)
        self.assertNotIn("connected=1", message)
        self.assertIn("serial=SERIAL_A", message)
        self.assertEqual(self.hh.filament().history, [])

    def test_operations_that_need_a_value_still_report_a_missing_bridge(self):
        with patch.object(self.hh.webhooks, "call_remote_method",
                          side_effect=self.mmu.printer.command_error("not installed")):
            self.hh.run_gcode("MMU_TD1 READ=1")
        self.assertTrue(any("bridge unavailable" in str(e) for e in self.hh.errors))
        self.assertEqual(self.hh.filament().history, [])


class TestTd1LoadHooks(Td1Case):
    def test_normal_load_establishes_owner_and_unload_clears_it(self):
        self.bridge.devices["SERIAL_A"].auto_override = True
        self.bridge.update_devices({"SERIAL_A": record()})
        self.hh.place_filament(0)
        self.mmu.gate_maps.set_gate_status(0, 1)
        self.hh.heat_extruder()
        self.hh.run_gcode("MMU_LOAD")
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.filament_pos, 10)
        self.assertEqual(self.owners()["SERIAL_A"]["gate"], 0)
        self.bridge.update_devices({"SERIAL_A": record(second=1)})
        self.assertEqual(self.mmu.gate_td[0], 4.)
        self.hh.run_gcode("MMU_UNLOAD")
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.owners(), {})
        self.assertEqual(self.mmu.filament_pos, 0)

    def test_loading_never_blocks_on_moonraker(self):
        self.bridge.devices["SERIAL_A"].auto_override = True
        self.bridge.update_devices({"SERIAL_A": record()})
        self.hh.place_filament(0)
        self.mmu.gate_maps.set_gate_status(0, 1)
        self.hh.heat_extruder()
        with patch.object(self.bridge, "refresh", side_effect=AssertionError("blocked")):
            self.hh.run_gcode("MMU_LOAD")
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.filament_pos, 10)


class TestTd1LaneData(unittest.TestCase):
    def test_lane_data_keeps_filament_color_and_adds_measured_td(self):
        with harness() as hh:
            hh.klippy.extra_status = {
                "gate_status": [1], "gate_color": ["ff0000"],
                "gate_td": [4.2], "gate_td1_color": ["0000ff"]}
            hh.call_remote("moonraker_push_lane_data", gate_ids=[(0, -1)])
            lane = hh.server.database.store["lane_data"]["lane0"]
            self.assertEqual(lane["td"], 4.2)
            self.assertEqual(lane["color"], "ff0000")
            # Happy Hare doesn't record when a gate was measured, so this stays null
            self.assertIsNone(lane["scan_time"])


class TestTd1ConfigValidation(unittest.TestCase):
    def test_conflicting_and_malformed_assignments_are_rejected(self):
        cases = [
            (dict(PARAM_TD1_CAPTURE_TIMEOUT="0"), "td1_capture_timeout"),
        ]
        for syms, message in cases:
            with self.subTest(syms=syms):
                profile = PROFILE.derive("td1_invalid", syms=dict(syms))
                with self.assertRaisesRegex(Exception, message):
                    with session(profile):
                        pass


class TestTd1LoadCaptureIdentity(Td1Case):
    def test_identity_change_while_waiting_is_not_applied(self):
        self.mmu.select_gate(0)
        self.param(0, "td1_capture_on_load", 1)
        self.bridge.update_devices({"SERIAL_A": record()})
        token = self.manager.begin_load(0)
        def replace(*args):
            self.manager.filament_changed(0)
            return record(second=1)
        with patch.object(self.manager, "wait_measurement", side_effect=replace):
            self.manager.end_load(token, True)
        self.assertEqual(self.owners(), {})
        self.assertIsNone(self.mmu.gate_td[0])

    def test_auto_without_explicit_capture_keeps_owner(self):
        self.mmu.select_gate(0)
        self.bridge.devices["SERIAL_A"].auto_override = True
        self.bridge.update_devices({"SERIAL_A": record()})
        token = self.manager.begin_load(0)
        with patch.object(self.manager, "wait_measurement") as wait:
            self.manager.end_load(token, True)
        wait.assert_not_called()
        self.assertIs(self.owners()["SERIAL_A"], token)


class TestTd1InFlightAuto(Td1Case):
    def test_reading_received_during_load_is_applied_after_success(self):
        self.mmu.select_gate(0)
        self.bridge.devices["SERIAL_A"].auto_override = True
        self.bridge.update_devices({"SERIAL_A": record()})
        token = self.manager.begin_load(0)
        self.mmu.filament_pos = 2
        self.bridge.update_devices({"SERIAL_A": record(second=1)})
        self.assertIsNone(self.mmu.gate_td[0])
        self.mmu.filament_pos = 10
        self.bridge.update_devices({"SERIAL_A": record(second=2)})
        self.assertEqual(self.mmu.gate_td[0], 4.)
        del token


class TestTd1MultiUnitValidation(unittest.TestCase):
    def test_units_sharing_a_scanner_may_differ_on_auto_update(self):
        # td1_auto_update is resolved at the point of use, like the other two
        # tunables, so it describes a unit's policy rather than the device - two
        # units sharing one physical scanner need not agree
        profile = profiles.clone_across_units("td1_differ", PROFILE, ["unit0", "unit1"])
        profile.units[1] = profile.units[1].derive(syms={"PARAM_TD1_AUTO_UPDATE": True})
        with session(profile) as hh:
            hh.boot(calibrate=True)
            self.assertEqual(hh.errors, [])
            device = hh.mmu.td1.devices["SERIAL_A"]
            self.assertFalse(hh.mmu.mmu_unit(0).td1_manager.auto_for(device))
            self.assertTrue(hh.mmu.mmu_unit(4).td1_manager.auto_for(device))

    def test_gate_unit_conflicts_are_rejected(self):
        profile = profiles.clone_across_units("td1_targets", PROFILE, ["unit0", "unit1"])
        with session(profile) as hh:
            hh.boot(calibrate=True)
            with self.assertRaisesRegex(Exception, "conflicts"):
                hh.run_gcode("MMU_TD1 GATE=0 UNIT=1")

    def test_per_gate_scanners_are_addressed_by_gate(self):
        profile = PROFILE.derive("td1_no_shared", syms=PER_GATE_SYMS)
        with session(profile) as hh:
            hh.boot(calibrate=True)
            hh.run_gcode("MMU_TD1 GATE=1")
            self.assertEqual(hh.errors, [])
            self.assertEqual(hh.mmu.td1.gates_for("B"), [1])


class TestTd1CaptureStatus(Td1Case):
    def test_only_the_active_reader_reports_as_capturing(self):
        self.bridge.update_devices({"SERIAL_A": record(), "OTHER": record()})
        self.bridge.active_serial = "SERIAL_A"
        with patch.object(self.mmu, "log_always") as report:
            self.hh.run_gcode("MMU_TD1")
        message = "\n".join(c.args[0] for c in report.call_args_list)
        gate_rows = [l for l in message.splitlines() if l.startswith("gate ")]
        self.assertTrue(gate_rows and all("(capturing)" in l for l in gate_rows), message)
        # OTHER serves no gate, so it lands in the unassigned section labelled by its
        # own serial - the only name it has - and is not capturing
        unused = [l for l in message.splitlines() if l.startswith("OTHER:")]
        self.assertTrue(unused, message)
        self.assertNotIn("(capturing)", unused[0])

    def test_owner_loss_during_load_prevents_application(self):
        self.mmu.select_gate(0)
        self.param(0, "td1_capture_on_load", 1)
        self.bridge.update_devices({"SERIAL_A": record()})
        token = self.manager.begin_load(0)
        self.clear_owners()
        with patch.object(self.manager, "wait_measurement") as wait:
            self.manager.end_load(token, True)
        wait.assert_not_called()
        self.assertIsNone(self.mmu.gate_td[0])


class TestTd1DeviceState(Td1Case):
    def test_runtime_state_does_not_persist(self):
        # Follows the NFC readers, not the filament sensors: turning a scanner off is a
        # "not right now" action, and the configuration is the record of intent
        self.hh.run_gcode("MMU_TD1 SERIAL=SERIAL_A ENABLE=0")
        self.hh.run_gcode("MMU_TD1 SERIAL=SERIAL_A AUTO=1")
        self.assertEqual(self.mmu.var_manager.get("mmu_state_td1_devices", None), None)
        # A restart rebuilds from configuration alone
        from extras.mmu.unit.td1.mmu_td1_device import MmuTd1Device
        self.bridge.devices["SERIAL_A"] = MmuTd1Device("SERIAL_A")
        self.assertTrue(self.bridge.devices["SERIAL_A"].enabled)
        self.assertFalse(self.bridge.devices["SERIAL_A"].auto_override)

    def test_command_reports_the_runtime_scope(self):
        with patch.object(self.mmu, "log_always") as report:
            self.hh.run_gcode("MMU_TD1 SERIAL=SERIAL_A ENABLE=0 QUIET=1")
        self.assertTrue(any("until restart" in c.args[0] for c in report.call_args_list))

    def test_changing_policy_drops_stale_attribution(self):
        self.owner(0)
        self.bridge.set_device_state("SERIAL_A", auto=True)
        self.assertEqual(self.owners(), {})


class TestTd1Ready(Td1Case):
    def test_no_configured_reader_does_not_start_polling(self):
        self.bridge.devices.clear()
        self.bridge.connected = False
        with patch.object(self.hh.reactor, "register_timer") as timer:
            self.bridge._ready()
        self.assertTrue(self.bridge.connected)
        timer.assert_not_called()


class TestTd1InvalidateOtherOwner(Td1Case):
    def test_other_gate_owner_survives_a_filament_change(self):
        token = self.owner(1, serial="OTHER")
        self.manager.apply(0, record())
        self.mmu.gate_maps.gate_filament_changed(0)
        self.assertEqual(self.owners(), {"OTHER": token})
        self.assertIsNone(self.mmu.gate_td[0])


class TestTd1Attribution(Td1Case):
    """
    A shared scanner must never hand one gate's measurement to the next.

    Attribution rests on "a reading newer than the baseline sampled before this gate
    moved", and Moonraker timestamps a reading when it RECEIVES it rather than when
    filament entered the scanner. So a reading produced by gate A but delivered late
    carries a late timestamp and is indistinguishable by time from gate B's own. What
    separates them is the debt: a gate that stops waiting leaves the scanner owing it
    a reading, and the next claimant discards whatever settles that debt.

    These drive the real wait_measurement()/update_devices() path on purpose. Stubbing
    wait_measurement is what let this through the first time.
    """

    def feed(self, plan):
        """Answer every bridge poll from 'plan', which returns a Moonraker payload."""
        def sink(method, values):
            if method == "mmu_td1_request":
                self.bridge._callback(Request(
                    request_id=values["request_id"], devices=plan(), error=None))
        self.hh.webhooks.sink = sink

    def test_a_late_reading_is_not_adopted_by_the_next_gate(self):
        # Gate 0's reading misses its capture timeout, then lands while gate 1 is
        # traversing. Before the debt existed this wrote 99.0 onto gate 1
        state = {"late": False}
        self.feed(lambda: {"SERIAL_A": rec_or_blank(state["late"])})
        for gate in (0, 1):
            self.hh.place_filament(gate)
        self.mmu.gate_maps.gate_status = [1, 1, -1, -1]
        original = self.manager.capture
        def capture(gate, baseline):
            if gate == 1:
                state["late"] = True
            return original(gate, baseline)
        self.manager.capture = capture
        self.hh.run_gcode("MMU_CHECK_GATE GATES=0,1 TD1=1")
        self.assertEqual(self.hh.errors, [])
        self.assertIsNone(self.mmu.gate_td[0], "gate 0 gave up, so it stays unmeasured")
        self.assertIsNone(self.mmu.gate_td[1], "gate 0's reading must not become gate 1's")

    def test_a_gate_still_measures_after_settling_someone_else_s_debt(self):
        # Settling a debt costs one reading, not the whole capture: the gate keeps
        # waiting and takes the next one
        blank = {"SERIAL_A": {"td": None, "color": None, "scan_time": None}}
        self.bridge.update_devices(blank) # Connected, nothing measured yet
        self.device().owe(0)
        readings = iter([record(second=1, td=7.), record(second=2, td=8.)])
        current = {"data": blank}
        def plan():
            nxt = next(readings, None)
            if nxt is not None:
                current["data"] = {"SERIAL_A": nxt}
            return current["data"]
        self.feed(plan)
        self.manager.capture(1, None)
        self.assertIsNone(self.mmu.gate_td[0])
        self.assertEqual(self.mmu.gate_td[1], 8., "first reading settled gate 0's debt")

    def test_an_owner_dropped_without_a_reading_leaves_a_debt(self):
        # The printing path never waits, so nothing times out - the token is simply
        # discarded at the next tool change, and that is equally a debt
        self.bridge.update_devices({"SERIAL_A": record()})
        self.clear_owners()
        self.owner(0, capture=False)
        self.manager.release(gate=0)
        self.assertEqual(self.bridge.devices["SERIAL_A"].unclaimed, 0)
        self.assertFalse(self.device().claimable(1))
        self.assertTrue(self.device().claimable(0),
                        "the gate that is owed can still claim its own reading")

    def test_a_satisfied_owner_leaves_no_debt(self):
        self.bridge.update_devices({"SERIAL_A": record()})
        token = self.owner(0, capture=False)
        token["satisfied"] = True
        self.manager.release(gate=0)
        self.assertIsNone(self.bridge.devices["SERIAL_A"].unclaimed)

    def test_the_passive_path_refuses_a_reading_owed_elsewhere(self):
        from extras.mmu.mmu_constants import FILAMENT_POS_LOADED
        self.bridge.update_devices({"SERIAL_A": record()})
        self.mmu.select_gate(1)
        self.mmu.filament_pos = FILAMENT_POS_LOADED
        self.bridge.devices["SERIAL_A"].auto_override = True
        self.owner(1, capture=False)
        self.device().owe(0)
        self.bridge.update_devices({"SERIAL_A": record(second=5, td=9.)})
        self.assertIsNone(self.mmu.gate_td[1], "owed to gate 0, so attributed to nobody")
        self.assertIsNone(self.bridge.devices["SERIAL_A"].unclaimed, "debt settled")
        # ...and with the debt gone the next reading is genuinely gate 1's
        self.bridge.update_devices({"SERIAL_A": record(second=6, td=9.5)})
        self.assertEqual(self.mmu.gate_td[1], 9.5)

    def test_busy_is_set_while_waiting_and_always_cleared(self):
        # A leaked 'busy' would silence the passive path and pin polling for the rest
        # of the session, so the flag has to survive the failure case too
        blank = {"SERIAL_A": {"td": None, "color": None, "scan_time": None}}
        self.bridge.update_devices(blank) # Connected, nothing measured yet
        seen = []
        self.feed(lambda: (seen.append(self.bridge.busy), blank)[1])
        self.param(0, "td1_capture_timeout", 0.2)
        with self.assertRaises(Exception):
            self.manager.capture(0, None)
        self.assertTrue(any(seen), "busy must be set while a capture is waiting")
        self.assertFalse(self.bridge.busy)
        self.assertEqual(self.bridge.active_serial, "")


class TestTd1RebootedScanner(Td1Case):
    def test_a_rebooted_scanner_drops_its_cached_reading(self):
        # Moonraker reports every field null after a power cycle. Keeping the old
        # values made status quote a measurement the device no longer stands behind
        self.bridge.update_devices({"SERIAL_A": record()})
        self.assertIsNotNone(self.bridge.devices["SERIAL_A"].scan_time)
        self.bridge.update_devices({"SERIAL_A": {"td": None, "color": None, "scan_time": None}})
        device = self.bridge.devices["SERIAL_A"]
        self.assertEqual((device.td, device.color, device.scan_time),
                         (None, None, None))

    def test_register_on_a_rebooted_scanner_reports_rather_than_shutting_down(self):
        # MMU_TD1 INIT=1 then REGISTER=1 is the documented recovery flow. measurement()
        # raises ValueError, which is not an MmuError - escaping a g-code handler makes
        # klipper call invoke_shutdown() rather than print an error
        self.bridge.update_devices({"SERIAL_A": record()})
        self.data = {"SERIAL_A": {"td": None, "color": None, "scan_time": None}}
        with patch.object(self.mmu, "handle_mmu_error") as handled:
            self.hh.run_gcode("MMU_TD1 GATE=0 REGISTER=1")
        self.assertIsNone(self.mmu.gate_td[0])
        self.assertTrue(handled.called, "reported as an MMU error, not raised")

    def test_apply_never_raises_a_bare_value_error(self):
        from extras.mmu.mmu_utils import MmuError
        for bad in ({}, {"td": None}, dict(record(), error="optical error")):
            with self.subTest(bad=bad), self.assertRaises(MmuError):
                self.manager.apply(0, bad)


class TestTd1ManualEditEvent(Td1Case):
    def test_a_hand_entered_td_is_not_an_identity_change(self):
        # The gate still holds the same filament; only the measurement was overridden.
        # Announcing an identity change would make every future subscriber drop state
        # it had no reason to
        events = []
        self.hh.printer.register_event_handler(
            "mmu:gate_filament_changed", lambda gate: events.append(gate))
        self.manager.apply(0, record())
        self.hh.run_gcode("MMU_GATE_MAP GATE=0 TD=3.5 QUIET=1")
        self.assertEqual(self.mmu.gate_td[0], 3.5)
        self.assertEqual(self.mmu.gate_td1_color[0], "", "measured color no longer applies")
        self.assertEqual(events, [])


def rgba(color, td=4.):
    """The color a measurement adopts into filament_color: RGB plus TD-derived alpha."""
    return "%s%02x" % (color, int(round(255 * max(0., 1. - td / 100.))))


def rec_or_blank(ready):
    return record(second=10, td=99.) if ready else {"td": None, "color": None, "scan_time": None}


# An off-path scanner: filament never passes through it. Readings are staged as pending
# and applied to the gate preloaded next, exactly as a shared NFC tag read is
OFFPATH = profiles.get("boxturtle").derive("td1_offpath", syms={
    "MMU_HAS_TD1": True, "MMU_HAS_OFFPATH_TD1": True, "PARAM_TD1_DEVICE": "BENCH",
    "PARAM_TD1_CAPTURE_TIMEOUT": "1",
})


class Td1OffPathCase(unittest.TestCase):
    def setUp(self):
        self.hh = session(OFFPATH)
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [])
        self.mmu = self.hh.mmu
        self.bridge = self.mmu.td1
        self.manager = self.mmu.mmu_unit(0).td1_manager
        self.bridge.pending.clear()
        self.data = {"BENCH": {"td": None, "color": None, "scan_time": None}}
        self.hh.webhooks.sink = self.respond
        # Connected, nothing measured yet - the state a scanner is in before anyone
        # presents filament to it. Staging requires the device to have been connected
        # already, so a reading cached across a reconnect is never mistaken for a new one
        self.bridge.update_devices(
            {"BENCH": {"td": None, "color": None, "scan_time": None}})

    def tearDown(self):
        self.hh.close()

    def respond(self, method, values):
        if method == "mmu_td1_request":
            self.bridge._callback(Request(
                request_id=values["request_id"], devices=self.data, error=None))

    def present(self, second=0, td=4., color="123456"):
        """Present filament to the off-path scanner by hand."""
        self.data = {"BENCH": record(second=second, td=td, color=color)}
        self.bridge.update_devices(self.data)

    def present_none(self):
        """Take it away again - connected, but with nothing to report."""
        self.data = {"BENCH": {"td": None, "color": None, "scan_time": None}}
        self.bridge.update_devices(self.data)


class TestTd1OffPathStaging(Td1OffPathCase):
    def test_it_serves_no_gate(self):
        self.assertEqual(self.manager.shared_device.serial, "BENCH")
        self.assertEqual([self.manager.serial_for(g) for g in range(4)], [""] * 4)
        # No gate has a scanner, so MMU_CHECK_GATE TD1=1 must not pay a traverse
        self.assertFalse(any(self.manager.needs_measurement(g) for g in range(4)))

    def test_a_reading_is_staged_rather_than_attributed(self):
        # An off-path reading has no gate, and guessing at the loaded one is the
        # misattribution the in-path rules exist to prevent
        self.mmu.select_gate(1)
        self.present()
        self.assertEqual(self.mmu.gate_td, [None] * 4)
        self.assertEqual(self.mmu.pending_measurement["td"], 4.)
        self.assertEqual(self.mmu.get_status(0)["pending_td"], 4.)

    def test_preload_applies_it_together_with_the_spool(self):
        # Assigning identity clears measurements, so both must land in one call
        self.present()
        self.mmu.set_pending_spool_id(42)
        pending = self.mmu._grab_pending()
        self.mmu._check_pending_filament(2, pending=pending)
        self.assertEqual(self.mmu.gate_spool_id[2], 42)
        self.assertEqual(self.mmu.gate_td[2], 4.)
        self.assertEqual(self.mmu.gate_td1_color[2], "123456")

    def test_it_is_applied_even_with_no_spool_or_tag(self):
        self.present()
        self.mmu._check_pending_filament(3)
        self.assertEqual(self.mmu.gate_td[3], 4.)
        self.assertIsNone(self.mmu.pending_measurement)

    def test_grab_and_clear_keep_all_three_in_lockstep(self):
        self.present()
        self.mmu.set_pending_spool_id(42)
        spool_id, tag, measured = self.mmu._grab_pending()
        self.assertEqual((spool_id, measured["td"]), (42, 4.))
        self.assertIsNone(self.mmu.pending_measurement)
        self.assertEqual(self.mmu.pending_spool_id, -1)

    def test_consuming_a_staging_does_not_restage_the_same_reading(self):
        # Filament left in the reader keeps being reported with the scan_time it was
        # first read at. That is not a new measurement, so it must not be staged onto
        # a second gate - presenting it again produces a new scan_time and does
        self.present(second=1)
        self.mmu._check_pending_filament(0)
        self.assertIsNone(self.mmu.pending_measurement)
        self.present(second=1)
        self.assertIsNone(self.mmu.pending_measurement)
        self.present(second=2, td=7.)
        self.assertEqual(self.mmu.pending_measurement["td"], 7.)

    def test_register_takes_the_off_path_reading_without_a_gate_assignment(self):
        # The workflow that previously needed the scanner assigned to gates
        self.present()
        self.hh.run_gcode("MMU_GATE_MAP GATE=2 SPOOLID=42 QUIET=1")
        self.hh.run_gcode("MMU_TD1 GATE=2 REGISTER=1 QUIET=1")
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.gate_td[2], 4.)


class TestTd1BothTopologies(Td1Case):
    """A unit may have in-path scanners AND an off-path one, as NFC always could."""

    def profile(self):
        return PROFILE.derive("td1_mixed", syms={
            "MMU_HAS_OFFPATH_TD1": True, "PARAM_TD1_DEVICE": "BENCH"})

    def test_pending_beats_a_gate_s_own_in_path_reading_at_preload(self):
        # The user just presented this filament by hand; an in-path reader corrects it
        # on the next load if td1_auto_update is on
        with session(self.profile()) as hh:
            hh.boot(calibrate=True)
            bridge, mmu = hh.mmu.td1, hh.mmu
            bridge.pending.clear()
            blank = {"td": None, "color": None, "scan_time": None}
            bridge.update_devices({"SERIAL_A": blank, "BENCH": blank})
            bridge.update_devices({"SERIAL_A": record(second=1, td=1.),
                                   "BENCH": record(second=2, td=9.)})
            hh.mmu.mmu_unit(0).td1_manager.apply(0, record(second=1, td=1.))
            self.assertEqual(mmu.gate_td[0], 1.)
            mmu._check_pending_filament(0)
            self.assertEqual(mmu.gate_td[0], 9.)

    def test_only_the_off_path_device_stages(self):
        with session(self.profile()) as hh:
            hh.boot(calibrate=True)
            bridge, mmu = hh.mmu.td1, hh.mmu
            bridge.pending.clear()
            blank = {"td": None, "color": None, "scan_time": None}
            bridge.update_devices({"SERIAL_A": blank, "BENCH": blank})
            bridge.update_devices({"SERIAL_A": record(second=1, td=1.), "BENCH": blank})
            self.assertIsNone(mmu.pending_measurement)
            bridge.update_devices({"SERIAL_A": record(second=1, td=1.),
                                   "BENCH": record(second=2, td=9.)})
            self.assertEqual(mmu.pending_measurement["td"], 9.)


class TestTd1Bypass(Td1OffPathCase):
    """
    The bypass has no gate-map row, so a measurement rides on 'active_filament'.

    Only the TD. Measured color is a per-gate LED source (gate_td1_color) with no
    bypass equivalent, so there would be nothing to read it.
    """

    def select_bypass(self):
        from extras.mmu.mmu_constants import TOOL_GATE_BYPASS
        self.mmu.select_bypass()
        self.assertEqual(self.mmu.gate_selected, TOOL_GATE_BYPASS)

    def test_a_staged_measurement_lands_on_the_bypass(self):
        self.present()
        self.select_bypass()
        self.mmu._check_pending_bypass()
        self.assertEqual(self.mmu.active_filament["td"], 4.)

    def test_no_measured_color_is_carried(self):
        self.present()
        self.select_bypass()
        self.mmu._check_pending_bypass()
        self.assertNotIn("td1_color", self.mmu.active_filament)

    def test_a_spoolman_refresh_does_not_wipe_it(self):
        # Spoolman does not model TD, so its async BYPASS=1 callback sends none and
        # rebuilds the whole dict. Arriving moments after a bypass load, it would
        # otherwise erase what was just measured
        self.present()
        self.select_bypass()
        self.mmu._check_pending_bypass()
        self.hh.run_gcode('MMU_GATE_MAP BYPASS=1 SPOOLID=7 NAME="PLA Black" '
                          'MATERIAL="PLA" VENDOR="Acme" COLOR="000000" QUIET=1')
        self.assertEqual(self.mmu.active_filament["spool_id"], 7)
        self.assertEqual(self.mmu.active_filament["td"], 4.)

    def test_it_can_be_set_and_cleared_by_hand(self):
        self.select_bypass()
        self.hh.run_gcode("MMU_GATE_MAP BYPASS=1 TD=3.5 QUIET=1")
        self.assertEqual(self.mmu.active_filament["td"], 3.5)
        # A blank parameter has to come last or klipper's parser swallows the next one
        self.hh.run_gcode("MMU_GATE_MAP BYPASS=1 QUIET=1 TD=")
        self.assertIsNone(self.mmu.active_filament["td"])

    def test_a_bad_value_is_refused_on_the_bypass_too(self):
        self.select_bypass()
        for bad in ("0", "-1", "abc"):
            with self.subTest(td=bad), self.assertRaises(Exception):
                self.hh.run_gcode("MMU_GATE_MAP BYPASS=1 TD=%s QUIET=1" % bad)


class TestTd1ActiveFilament(Td1Case):
    def test_selecting_a_gate_publishes_its_measured_td(self):
        self.manager.apply(2, record(td=6.5))
        self.mmu.select_gate(2)
        self.assertEqual(self.mmu.active_filament["td"], 6.5)

    def test_an_unmeasured_gate_publishes_none(self):
        self.mmu.select_gate(1)
        self.assertIsNone(self.mmu.active_filament["td"])


class TestTd1CommandAlignment(Td1OffPathCase):
    """
    MMU_TD1 addresses scanners the way MMU_NFC addresses readers.

    Same verbs for the same jobs, so what you learn on one command carries to the
    other: SHARED/GATE/GATES/UNIT to address, ENABLE to switch off, READ to go and
    look now, INIT / INIT_ALL to reset, DETAILS to say more.
    """

    def test_shared_addresses_the_off_path_scanner(self):
        with patch.object(self.mmu, "log_always") as report:
            self.hh.run_gcode("MMU_TD1 SHARED=1")
        text = "\n".join(c.args[0] for c in report.call_args_list)
        self.assertIn("serial=BENCH", text)
        # It serves no gate on purpose, so it must read as the bench scanner it is
        # rather than as a gate reader that lost its gate
        self.assertIn("shared:", text)
        self.assertNotIn("gate ", text)
        self.assertNotIn("Not assigned to any gate", text)

    def test_shared_enable_needs_no_serial(self):
        self.hh.run_gcode("MMU_TD1 SHARED=1 ENABLE=0 QUIET=1")
        self.assertEqual(self.hh.errors, [])
        self.assertFalse(self.bridge.devices["BENCH"].enabled)

    def test_shared_on_a_unit_without_one_says_so(self):
        with session(PROFILE) as hh:   # in-path scanners only
            hh.boot(calibrate=True)
            with self.assertRaisesRegex(Exception, "No off-path TD-1 scanner"):
                hh.run_gcode("MMU_TD1 SHARED=1")

    def test_read_polls_instead_of_using_the_cache(self):
        with patch.object(self.bridge, "refresh") as refresh:
            self.hh.run_gcode("MMU_TD1 READ=1 QUIET=1")
        self.assertTrue(refresh.called)

    def test_init_all_reboots_every_scanner(self):
        with patch.object(self.bridge, "refresh") as refresh:
            self.hh.run_gcode("MMU_TD1 INIT_ALL=1")
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(all(c.kwargs.get("reset") for c in refresh.call_args_list))
        self.assertEqual({c.args[0] for c in refresh.call_args_list}, {"BENCH"})

    def test_a_staged_measurement_is_reported(self):
        self.present()
        with patch.object(self.mmu, "log_always") as report:
            self.hh.run_gcode("MMU_TD1")
        text = "\n".join(c.args[0] for c in report.call_args_list)
        self.assertIn("Staged for the next gate loaded", text)
        self.assertIn("td=4.00", text)


class TestTd1AdoptedColorLifetime(Td1Case):
    """An adopted filament color belongs to the measurement, and goes with it."""

    def test_it_is_dropped_when_the_measurement_is(self):
        self.manager.apply(0, record(color="00ff00"))
        self.assertEqual(self.mmu.gate_color[0], rgba("00ff00"))
        self.mmu.gate_maps.gate_filament_changed(0)
        self.assertEqual(self.mmu.gate_color[0], "", "adopted color outlived its reading")
        self.assertEqual(self.mmu.gate_color_rgb[0], (0., 0., 0.))

    def test_a_color_we_did_not_set_is_left_alone(self):
        self.mmu.gate_maps.gate_color[1] = "ff0000"
        self.manager.apply(1, record(color="00ff00"))
        self.mmu.gate_maps.gate_filament_changed(1)
        self.assertEqual(self.mmu.gate_color[1], "ff0000")

    def test_a_forced_color_is_ours_afterwards(self):
        # SET_COLOR makes the measured color the gate's, so it is adopted from then on
        self.mmu.gate_maps.gate_color[2] = "ff0000"
        self.manager.apply(2, record(color="00ff00"))
        self.hh.run_gcode("MMU_TD1 GATE=2 SET_COLOR=1 QUIET=1")
        self.mmu.gate_maps.gate_filament_changed(2)
        self.assertEqual(self.mmu.gate_color[2], "")

    def test_a_hand_entered_td_drops_it_too(self):
        self.manager.apply(3, record(color="00ff00"))
        self.hh.run_gcode("MMU_GATE_MAP GATE=3 TD=2.5 QUIET=1")
        self.assertEqual(self.mmu.gate_td[3], 2.5)
        self.assertEqual(self.mmu.gate_color[3], "")


class TestTd1Alpha(Td1Case):
    """
    The adopted color carries an alpha channel derived from the TD.

    TD is a transmission distance - how far light gets into the filament - so a low
    value is opaque and a high one is clear. AJAX's own examples anchor the scale:
    black ~0.1, white ~4.6, transparent natural ~100.
    """

    def test_opaque_translucent_and_clear(self):
        for gate, (td, expect) in enumerate(((0.1, "ff"),    # black, opaque
                                             (4.6, "f3"),    # white, near opaque
                                             (50., "80"),    # half way
                                             (100., "00"))): # transparent natural
            with self.subTest(td=td):
                self.mmu.gate_maps.gate_color[gate] = ""
                self.manager.apply(gate, record(second=gate, td=td, color="112233"))
                self.assertEqual(self.mmu.gate_color[gate], "112233" + expect)

    def test_beyond_the_clear_point_stays_fully_transparent(self):
        self.manager.apply(0, record(td=250., color="112233"))
        self.assertEqual(self.mmu.gate_color[0], "11223300")

    def test_the_alpha_does_not_disturb_the_rgb_leds_read(self):
        self.manager.apply(0, record(td=50., color="00ff00"))
        self.assertEqual(self.mmu.gate_color_rgb[0], (0., 1., 0.))
        self.assertEqual(self.mmu.gate_td1_color[0], "00ff00") # Recorded measurement stays plain rgb

    def test_an_adopted_color_with_alpha_is_still_recognized_as_ours(self):
        self.manager.apply(0, record(td=50., color="00ff00"))
        self.mmu.gate_maps.gate_filament_changed(0)
        self.assertEqual(self.mmu.gate_color[0], "")


class TestTd1OffPathResponsiveness(Td1OffPathCase):
    """
    An off-path scanner's whole job is producing readings to stage.

    Nothing arms for it and it has no gate to auto-update, so without being counted
    explicitly it falls to the idle poll interval - the user presents filament and
    waits, with nothing to say the pending clock has not started yet.
    """

    def test_a_configured_off_path_scanner_keeps_polling_active(self):
        self.assertTrue(self.bridge._wants_readings())

    def test_disabling_it_backs_the_polling_off_again(self):
        self.bridge.set_device_state("BENCH", enabled=False)
        self.assertFalse(self.bridge._wants_readings())

    def test_in_path_scanners_alone_still_idle(self):
        # Unchanged: they are only read when something is actually capturing
        with session(PROFILE) as hh:
            hh.boot(calibrate=True)
            self.assertFalse(hh.mmu.td1._wants_readings())

    def test_the_first_reading_after_a_bridge_blip_is_not_lost(self):
        # consider() must not attribute a reading cached across a disconnect, but
        # staging is judged on the timestamp alone - so a hiccup must not cost the
        # user a presentation
        self.present(second=1)
        self.mmu._clear_pending()
        self.bridge.update_devices({}, "moonraker hiccup", "bridge")
        self.assertFalse(self.bridge.devices["BENCH"].connected)
        self.present(second=5, td=7.)
        self.assertEqual(self.mmu.pending_measurement["td"], 7.)

    def test_a_stale_re_report_after_a_blip_is_still_refused(self):
        # The property the was_connected guard was protecting, kept by the timestamp
        # test: the cached reading survives a disconnect, so re-reporting it stages
        # nothing
        self.present(second=1)
        self.mmu._clear_pending()
        self.bridge.update_devices({}, "moonraker hiccup", "bridge")
        self.present(second=1)
        self.assertIsNone(self.mmu.pending_measurement)


class TestTd1LeftInTheReader(Td1OffPathCase):
    """
    Filament left sitting in the off-path scanner must stage exactly once.

    A re-measuring device advances scan_time every poll, so the timestamp alone is not
    a dedupe - what decides is whether the measurement changed.
    """

    def stagings(self, readings):
        with patch.object(self.mmu, "stage_pending_measurement",
                          wraps=self.mmu.stage_pending_measurement) as staged:
            for second, td in readings:
                self.present(second=second, td=td)
            return staged.call_count

    def test_a_device_that_re_measures_still_stages_once(self):
        # scan_time advances on every poll; the measurement does not
        self.assertEqual(self.stagings([(s, 4.) for s in range(10, 16)]), 1)
        self.assertEqual(self.mmu.pending_measurement["td"], 4.)

    def test_a_device_that_reports_one_reading_also_stages_once(self):
        self.assertEqual(self.stagings([(1, 4.)] * 5), 1)

    def test_a_genuinely_different_measurement_restages(self):
        # Swap the filament for another: the measurement changes, so it is new
        self.assertEqual(self.stagings([(1, 4.), (2, 4.), (3, 9.)]), 2)
        self.assertEqual(self.mmu.pending_measurement["td"], 9.)

    def test_consuming_a_pending_does_not_restage_what_is_still_sitting_there(self):
        # A spool just loaded must not immediately stage itself for the next gate -
        # the same rule NFC applies to a tag left on its shared reader
        self.present(second=1)
        self.mmu._check_pending_filament(0)
        self.assertIsNone(self.mmu.pending_measurement)
        self.assertEqual(self.stagings([(s, 4.) for s in range(10, 14)]), 0)

    def test_a_timed_out_pending_does_not_re_establish_itself(self):
        """
        Unlike the NFC reader, which DOES re-arm on timeout (allow_reread). A tag is
        read at a distance and is normally lifted away between presentations, so one
        still in the field means somebody is holding it there. Filament sits in a TD-1
        until it is pulled out, so re-staging every timeout would re-arm for ever.
        """
        self.present(second=1)
        self.assertIsNotNone(self.mmu.pending_measurement)
        self.hh.reactor.advance(self.mmu.p.spoolman_pending_id_timeout + 1)
        self.assertIsNone(self.mmu.pending_measurement, "should have timed out")
        self.assertEqual(self.stagings([(s, 4.) for s in range(10, 16)]), 0,
                         "left in the reader, so nothing new has happened")

    def test_a_different_filament_re_stages_after_a_timeout(self):
        """The first of the two ways back: swap what is in front of the lens."""
        self.present(second=1)
        self.hh.reactor.advance(self.mmu.p.spoolman_pending_id_timeout + 1)
        self.assertEqual(self.stagings([(10, 9.)]), 1, "a materially different reading")
        self.assertEqual(self.mmu.pending_measurement["td"], 9.)

    def test_removing_and_re_inserting_the_same_filament_re_stages(self):
        """
        The second: an empty lens holds nothing, so the dedupe has nothing left to
        suppress and the same spool presented again is a fresh presentation.
        """
        self.present(second=1)
        self.hh.reactor.advance(self.mmu.p.spoolman_pending_id_timeout + 1)
        self.present_none()
        self.assertEqual(self.stagings([(10, 4.)]), 1, "re-inserted after a removal")
        self.assertEqual(self.mmu.pending_measurement["td"], 4.)

    def test_a_cancelled_pending_is_not_re_established(self):
        # The opposite of the timeout above: MMU_GATE_MAP NEXT_SPOOLID=0 cancels whatever
        # is staged, a measurement included, and must not call allow_restage(). Filament
        # left at the bench scanner would otherwise re-stage on the next poll and there
        # would be no way to cancel at all
        self.present(second=1)
        self.assertEqual(self.mmu.pending_measurement["td"], 4.)
        self.assertEqual(self.mmu.pending_phase, "pending")

        self.hh.run_gcode("MMU_GATE_MAP NEXT_SPOOLID=0")
        self.assertIsNone(self.mmu.pending_measurement)
        self.assertIsNone(self.mmu.pending_phase, "the countdown goes out with it")

        self.assertEqual(self.stagings([(s, 4.) for s in range(10, 16)]), 0,
                         "still in the reader, but cancelled means cancelled")
        self.hh.reactor.advance(self.mmu.p.spoolman_pending_id_timeout + 1)
        self.assertIsNone(self.mmu.pending_measurement)


class TestTd1AutoPolicy(Td1Case):
    """
    td1_auto_update is resolved where it is used, like the other two tunables.

    It describes a unit's policy, not the scanner, so a runtime edit takes effect at
    once and two units sharing one physical scanner need not agree. The device holds
    only the MMU_TD1 AUTO= override.
    """

    def test_a_runtime_config_edit_takes_effect_immediately(self):
        # It used to be read once at boot and cached on the device, so MMU_TEST_CONFIG
        # changed the parameter while the machine went on using its boot-time value
        device = self.bridge.devices["SERIAL_A"]
        manager = self.mmu.mmu_unit(0).td1_manager
        self.assertFalse(manager.auto_for(device))
        self.hh.run_gcode("MMU_TEST_CONFIG td1_auto_update=1")
        self.assertTrue(manager.auto_for(device))
        self.assertTrue(self.bridge._wants_readings(), "polling should follow it too")

    def test_the_command_override_beats_the_configured_value(self):
        device = self.bridge.devices["SERIAL_A"]
        manager = self.mmu.mmu_unit(0).td1_manager
        self.hh.run_gcode("MMU_TEST_CONFIG td1_auto_update=1")
        self.hh.run_gcode("MMU_TD1 SERIAL=SERIAL_A AUTO=0 QUIET=1")
        self.assertFalse(manager.auto_for(device), "override should win until restart")

    def test_clearing_nothing_falls_back_to_the_configured_value(self):
        device = self.bridge.devices["SERIAL_A"]
        manager = self.mmu.mmu_unit(0).td1_manager
        self.assertIsNone(device.auto_override, "no override until asked for one")
        self.hh.run_gcode("MMU_TEST_CONFIG td1_auto_update=1")
        self.assertTrue(manager.auto_for(device))


class TestTd1ClearPending(Td1OffPathCase):
    """
    MMU_TD1 CLEAR_PENDING=1 - the targeted counterpart of MMU_GATE_MAP NEXT_SPOOLID=0.
    Discards what this scanner staged and nothing else.
    """

    def stagings(self, readings):
        with patch.object(self.mmu, "stage_pending_measurement",
                          wraps=self.mmu.stage_pending_measurement) as staged:
            for second, td in readings:
                self.present(second=second, td=td)
            return staged.call_count

    def test_it_discards_the_measurement(self):
        self.present(second=1)
        self.assertEqual(self.mmu.pending_measurement["td"], 4.)
        self.hh.run_gcode("MMU_TD1 CLEAR_PENDING=1")
        self.assertIsNone(self.mmu.pending_measurement)
        # Nothing else was staged, so the countdown goes with it
        self.assertIsNone(self.mmu.pending_phase)

    def test_it_says_so_when_there_is_nothing_staged(self):
        self.hh.run_gcode("MMU_TD1 CLEAR_PENDING=1")
        self.assertIsNone(self.mmu.pending_measurement)

    def test_it_leaves_a_hand_set_spool_id_pending(self):
        # The point of a targeted clear: a wrong measurement should not cost the
        # spool assignment staged beside it
        self.present(second=1)
        self.mmu.set_pending_spool_id(77)
        self.hh.run_gcode("MMU_TD1 CLEAR_PENDING=1")
        self.assertIsNone(self.mmu.pending_measurement)
        self.assertEqual(self.mmu.pending_spool_id, 77)
        self.assertEqual(self.mmu.pending_phase, "pending", "still something to apply")

    def test_the_survivor_keeps_its_original_deadline(self):
        # A clear is not a staging, so it must not renew the window - otherwise
        # discarding one source would extend the life of the other
        timeout = self.mmu.p.spoolman_pending_id_timeout
        self.present(second=1)
        self.mmu.set_pending_spool_id(77)
        self.hh.reactor.advance(timeout - 5)
        self.hh.run_gcode("MMU_TD1 CLEAR_PENDING=1")
        self.assertEqual(self.mmu.pending_spool_id, 77)
        self.hh.reactor.advance(6)   # past the ORIGINAL deadline, not a fresh one
        self.assertEqual(self.mmu.pending_spool_id, -1, "the window was not renewed")

    def test_a_cleared_measurement_does_not_re_stage_itself(self):
        # Same rule as the cancel: the dedupe is left alone, or the next poll would
        # bring back what was just discarded and the command would do nothing
        self.present(second=1)
        self.hh.run_gcode("MMU_TD1 CLEAR_PENDING=1")
        self.assertEqual(self.stagings([(s, 4.) for s in range(10, 16)]), 0,
                         "still in the reader, but cleared means cleared")
        self.assertIsNone(self.mmu.pending_measurement)


class TestTd1MeasurementJitter(Td1OffPathCase):
    """
    A TD-1 is an analogue instrument, so equality is the wrong dedupe.

    AJAX quote +/-7.5%: the same filament read twice never gives the same numbers, so
    an exact comparison would call every re-measurement a new filament.
    """

    def stagings(self, readings):
        with patch.object(self.mmu, "stage_pending_measurement",
                          wraps=self.mmu.stage_pending_measurement) as staged:
            for second, td, color in readings:
                self.present(second=second, td=td, color=color)
            return staged.call_count

    def test_jitter_within_the_instrument_error_is_not_a_new_filament(self):
        # +/-7.5% around 4.0, with the low color bits wobbling too
        jittery = [(10, 4.00, "123456"), (11, 4.28, "123457"), (12, 3.72, "123455"),
                   (13, 4.11, "123458"), (14, 3.85, "123454")]
        self.assertEqual(self.stagings(jittery), 1)

    def test_a_real_filament_swap_still_restages(self):
        self.assertEqual(self.stagings([(10, 4.0, "123456"), (11, 0.1, "000000")]), 2)
        self.assertEqual(self.mmu.pending_measurement["td"], 0.1)

    def test_a_color_change_alone_restages(self):
        self.assertEqual(self.stagings([(10, 4.0, "112233"), (11, 4.0, "ff8800")]), 2)


class TestTd1Removal(Td1OffPathCase):
    """
    Taking filament out restarts the pending window - where the device says so.

    Optional: Moonraker's API has no presence field and does not document whether
    the values clear on removal. Where they do, this is the better moment to start
    the clock; where they do not, staging on insertion still carries the feature.
    """

    def remove(self):
        """The device reports nothing again - if that is what it does on removal."""
        self.present_none()

    def test_removal_moves_the_deadline(self):
        self.present(second=1)
        self.hh.reactor.advance(15)        # most of the window spent at the bench
        with patch.object(self.mmu, "stage_pending_measurement",
                          wraps=self.mmu.stage_pending_measurement) as staged:
            self.remove()
        self.assertTrue(staged.called, "removal should re-stage to move the deadline")
        self.assertEqual(staged.call_args.kwargs.get("removed"), True)
        self.hh.reactor.advance(10)        # past the ORIGINAL deadline
        self.assertIsNotNone(self.mmu.pending_measurement, "window should have moved")

    def test_a_consumed_measurement_is_never_resurrected(self):
        # Otherwise the spool just loaded would stage itself for the next gate too
        self.present(second=1)
        self.mmu._check_pending_filament(0)
        self.assertIsNone(self.mmu.pending_measurement)
        self.remove()
        self.assertIsNone(self.mmu.pending_measurement)

    def test_a_device_that_never_reports_removal_still_works(self):
        # The whole feature must not depend on undocumented device behavior
        self.present(second=1)
        self.assertEqual(self.mmu.pending_measurement["td"], 4.)
        self.hh.reactor.advance(self.mmu.p.spoolman_pending_id_timeout + 1)
        self.assertIsNone(self.mmu.pending_measurement)

    def test_removal_of_something_else_does_not_extend(self):
        self.present(second=1, td=4.)
        self.mmu.pending_measurement = dict(self.mmu.pending_measurement, td=99.)
        with patch.object(self.mmu, "stage_pending_measurement") as staged:
            self.remove()
        self.assertFalse(staged.called)


class TestTd1PendingLed(Td1OffPathCase):
    """
    A staged measurement drives the same countdown overlay a pending spool_id does.

    The overlay is base functionality keyed on the pending lifecycle, not on any one
    source of it, so TD-1 lights it without a TD-1-specific effect.
    """

    def test_a_staged_measurement_starts_the_countdown(self):
        self.assertIsNone(self.mmu.pending_phase)
        self.present()
        self.assertEqual(self.mmu.pending_phase, 'pending')

    def test_it_reaches_expiring_before_the_deadline(self):
        self.present()
        self.hh.reactor.advance(self.mmu.p.spoolman_pending_id_timeout - 1)
        self.assertEqual(self.mmu.pending_phase, 'expiring')

    def test_it_clears_when_the_pending_times_out(self):
        self.present()
        self.hh.reactor.advance(self.mmu.p.spoolman_pending_id_timeout + 1)
        self.assertIsNone(self.mmu.pending_measurement)
        self.assertIsNone(self.mmu.pending_phase)

    def test_it_clears_when_the_pending_is_consumed(self):
        self.present()
        self.mmu._check_pending_filament(0)
        self.assertIsNone(self.mmu.pending_phase)

    def test_a_restage_restarts_the_countdown(self):
        # Every staging restarts the timeout, so the overlay has to restart with it or
        # it would sit in 'expiring' against a deadline that has already moved
        self.present(second=1, td=4.)
        self.hh.reactor.advance(self.mmu.p.spoolman_pending_id_timeout - 1)
        self.assertEqual(self.mmu.pending_phase, 'expiring')
        self.present(second=2, td=9.)
        self.assertEqual(self.mmu.pending_phase, 'pending')


class TestTd1PendingLedScope(Td1Case):
    """What counts as a pending worth showing - the NFC side must not change."""

    def test_a_bare_uid_left_by_a_cancel_does_not_light_it(self):
        # NEXT_SPOOLID=0 is a deliberate cancel. The uid survives so the gate's RFID is
        # still recorded, but it is bookkeeping, not a result anyone is waiting on
        self.mmu.pending_tag = ("0451ABCD", None)
        self.assertFalse(self.mmu.pending_active)

    def test_a_tag_carrying_filament_data_does_light_it(self):
        self.mmu.pending_tag = ("0451ABCD", {"material": "PLA"})
        self.assertTrue(self.mmu.pending_active)

    def test_a_resolved_spool_still_lights_it(self):
        self.mmu.pending_spool_id = 7
        self.assertTrue(self.mmu.pending_active)

    def test_a_measurement_alone_lights_it(self):
        self.mmu.pending_measurement = record()
        self.assertTrue(self.mmu.pending_active)


class TestTd1Flash(Td1Case):
    """The transient 'measured' / 'no measurement' flashes."""

    def flashes(self):
        return patch.object(self.mmu.led_manager, "set_transient_effect",
                            wraps=self.mmu.led_manager.set_transient_effect)

    def test_a_measurement_applied_flashes_that_gates_leds(self):
        with self.flashes() as flash:
            self.manager.apply(2, record())
        self.assertEqual(flash.call_count, 1)
        self.assertEqual(flash.call_args.kwargs["gate"], 2)
        self.assertEqual(flash.call_args.kwargs["segment"], "exit")

    def test_a_repeat_reading_does_not_flash(self):
        # The whole point of the dedupe: a scanner re-measuring filament left in front
        # of it must not strobe on every poll
        self.manager.apply(0, record())
        with self.flashes() as flash:
            self.manager.apply(0, record())
        self.assertEqual(flash.call_count, 0)

    def test_a_timed_out_capture_flashes_fail(self):
        from extras.mmu.mmu_utils import MmuError
        self.bridge.update_devices(self.data) # Connected, with one cached reading
        baseline = self.manager.baseline(0)   # Nothing newer will arrive
        with self.flashes() as flash:
            with self.assertRaises(MmuError):
                self.manager.wait_measurement(0, baseline, 0.)
        self.assertEqual(flash.call_count, 1)
        self.assertEqual(flash.call_args.args[1],
                         self.mmu.led_manager.effect_name(0, 'td1_fail'))
        self.assertTrue(flash.call_args.kwargs["defer"], "must queue behind a read flash")

    def test_an_unmapped_effect_is_a_no_op(self):
        # Opt-in: a user who maps no td1 effect sees exactly the old behavior
        self.mmu.mmu_unit(0).leds.effects['td1_read'] = ''
        with self.flashes() as flash:
            self.manager.apply(1, record())
        self.assertEqual(flash.call_count, 0)

    def test_the_segment_can_be_configured(self):
        self.hh.run_gcode("MMU_TEST_CONFIG TD1_LED_SEGMENT=entry")
        self.assertEqual(self.hh.errors, [])
        with self.flashes() as flash:
            self.manager.apply(3, record())
        self.assertEqual(flash.call_args.kwargs["segment"], "entry")


class TestTd1OffPathFlash(Td1OffPathCase):
    def test_a_staged_reading_flashes_the_units_segment(self):
        # No gate to attribute to, so the whole segment flashes rather than one gate's
        with patch.object(self.mmu.led_manager, "set_transient_effect",
                          wraps=self.mmu.led_manager.set_transient_effect) as flash:
            self.present()
        self.assertEqual(flash.call_count, 1)
        self.assertIsNone(flash.call_args.kwargs["gate"])

    def test_filament_left_in_the_scanner_flashes_once(self):
        with patch.object(self.mmu.led_manager, "set_transient_effect",
                          wraps=self.mmu.led_manager.set_transient_effect) as flash:
            self.present(second=1)
            self.present(second=2)
            self.present(second=3)
        self.assertEqual(flash.call_count, 1)


class TestTd1AutoUnitQualification(unittest.TestCase):
    """
    An AUTO= override says which unit's policy it stands in for.

    td1_auto_update is resolved per unit, so an override that lands on a device has
    to be attributed to someone. Anything that already implies a unit is enough -
    UNIT= is only demanded when nothing does. ENABLE= needs none of this: switching
    a scanner off is about the device, not about a unit's policy.
    """

    MULTI = profiles.Profile("td1_auto_units", units=[
        profiles.UnitProfile("unit0", syms=dict(
            profiles.get("boxturtle").syms, MMU_HAS_TD1=True,
            PARAM_TD1_BOWDEN_DEVICE="ONE"), index=0),
        profiles.UnitProfile("unit1", syms=dict(
            profiles.get("boxturtle").syms, MMU_HAS_TD1=True,
            PARAM_TD1_BOWDEN_DEVICE="ONE"), index=1)])

    def test_one_unit_never_needs_it(self):
        single = profiles.get("boxturtle").derive("td1_auto_one", syms={
            "MMU_HAS_TD1": True, "PARAM_TD1_BOWDEN_DEVICE": "ONE"})
        with session(single) as hh:
            hh.boot(calibrate=True)
            hh.run_gcode("MMU_TD1 SERIAL=ONE AUTO=1 QUIET=1")
            self.assertEqual(hh.errors, [])
            self.assertTrue(hh.mmu.td1.devices["ONE"].auto_override)

    def test_a_gate_selection_implies_the_unit(self):
        with session(self.MULTI) as hh:
            hh.boot(calibrate=True)
            for cmd in ("MMU_TD1 GATE=0 AUTO=1", "MMU_TD1 GATES=4,5 AUTO=1"):
                with self.subTest(cmd=cmd):
                    hh.run_gcode(cmd + " QUIET=1")
                    self.assertEqual(hh.errors, [])

    def test_an_explicit_unit_is_accepted(self):
        with session(self.MULTI) as hh:
            hh.boot(calibrate=True)
            hh.run_gcode("MMU_TD1 UNIT=1 SERIAL=ONE AUTO=1 QUIET=1")
            self.assertEqual(hh.errors, [])

    def test_a_bare_serial_on_a_multi_unit_machine_is_refused(self):
        # Nothing here says whose policy is being overridden
        with session(self.MULTI) as hh:
            hh.boot(calibrate=True)
            with self.assertRaisesRegex(Exception, "UNIT parameter is required"):
                hh.run_gcode("MMU_TD1 SERIAL=ONE AUTO=1 QUIET=1")

    def test_enable_needs_no_unit(self):
        with session(self.MULTI) as hh:
            hh.boot(calibrate=True)
            hh.run_gcode("MMU_TD1 SERIAL=ONE ENABLE=0 QUIET=1")
            self.assertEqual(hh.errors, [])
            self.assertFalse(hh.mmu.td1.devices["ONE"].enabled)
