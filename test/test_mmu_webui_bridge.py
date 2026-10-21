# Happy Hare test harness - the Moonraker-compatible API that lets a real Mainsail/Fluidd talk to
# the simulator (test/hh/webui_bridge.py, `make console ARGS=--serve`).
#
# A raw socket stands in for the browser: it does the websocket handshake, sends JSON-RPC and
# reads what the bridge pushes. The bridge only ever touches the session from service(), so the
# tests call that from this thread exactly as the console's idle tick does.
#
#   ./venv/bin/python -m unittest test.test_mmu_webui_bridge
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import base64
import json
import logging
import os
import select
import socket
import struct
import time
import unittest

from test.hh import session
from test.hh.webui_bridge import WebUiBridge

logging.getLogger().setLevel(logging.CRITICAL)

TIP_AT_GATE = -40.0


class WebSocketClient:
    """The least a browser does: handshake, masked text frames out, unmasked text frames in."""

    def __init__(self, port):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET /websocket HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n'
                           'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
                           'Sec-WebSocket-Version: 13\r\n\r\n' % key).encode())
        head = b''
        while b'\r\n\r\n' not in head:
            head += self.sock.recv(1)
        self.handshake = head.decode()
        self.next_id = 0

    def send(self, method, **params):
        self.next_id += 1
        payload = json.dumps({'jsonrpc': '2.0', 'id': self.next_id, 'method': method,
                              'params': params}).encode()
        mask = os.urandom(4)
        header = bytes([0x81]) + (bytes([0x80 | len(payload)]) if len(payload) < 126
                                  else bytes([0x80 | 126]) + struct.pack('>H', len(payload)))
        self.sock.sendall(header + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))
        return self.next_id

    def _read(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('closed')
            buf += chunk
        return buf

    def receive(self):
        n = self._read(2)[1] & 0x7F
        if n == 126:
            n = struct.unpack('>H', self._read(2))[0]
        elif n == 127:
            n = struct.unpack('>Q', self._read(8))[0]
        return json.loads(self._read(n))

    def close(self):
        self.sock.close()


class WebUiBridgeTestCase(unittest.TestCase):
    PROFILE = 'boxturtle'

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.boot(calibrate=True)
        self.bridge = WebUiBridge(self.hh, self.hh.run_gcode, port=0).start()
        self.client = WebSocketClient(self.bridge.port)

    def tearDown(self):
        self.client.close()
        self.bridge.close()
        self.hh.close()

    def serve(self, until, timeout=5.0):
        """Service the inbox from this thread until `until()` is true."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.bridge.service()
            if until():
                return
            time.sleep(0.01)
        self.fail('bridge never produced the expected result')

    def call(self, method, **params):
        request_id = self.client.send(method, **params)
        self.serve(lambda: select.select([self.client.sock], [], [], 0)[0])
        while True:
            reply = self.client.receive()
            if reply.get('id') == request_id:
                return reply


class TestWebUiBridge(WebUiBridgeTestCase):
    def test_handshake_completes_and_the_server_reports_klippy_ready(self):
        self.assertIn('101 Switching Protocols', self.client.handshake)
        info = self.call('server.info')['result']
        self.assertEqual(info['klippy_state'], 'ready')
        self.assertTrue(info['klippy_connected'])

    def test_objects_include_the_mmu_with_its_status(self):
        self.assertIn('mmu', self.call('printer.objects.list')['result']['objects'])
        status = self.call('printer.objects.query', objects={'mmu': ['print_state', 'pause_hint']})['result']['status']
        self.assertEqual(set(status['mmu']), {'print_state', 'pause_hint'})
        self.assertEqual(status['mmu']['pause_hint'], '')

    def test_unknown_method_is_a_json_rpc_error_and_is_counted(self):
        reply = self.call('machine.made_up')
        self.assertEqual(reply['error']['code'], -32601)
        self.assertEqual(self.bridge.unsupported['machine.made_up'], 1)

    def test_gcode_script_runs_in_the_session(self):
        mark = len(self.hh.gcode.executed)
        self.assertEqual(self.call('printer.gcode.script', script='MMU_STATUS')['result'], 'ok')
        self.assertIn('MMU_STATUS', self.hh.gcode.executed[mark:])

    def test_subscription_pushes_only_what_changed(self):
        self.call('printer.objects.subscribe', objects={'mmu': ['print_state']})
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        self.bridge.push_status()
        update = self.client.receive()
        self.assertEqual(update['method'], 'notify_status_update')
        self.assertEqual(update['params'][0]['mmu']['print_state'], self.hh.mmu.psm.print_state)

    def test_http_database_item_is_served_like_moonraker(self):
        import http.client, threading
        box = {}

        def post():
            conn = http.client.HTTPConnection('127.0.0.1', self.bridge.port, timeout=5)
            conn.request('POST', '/server/database/item',
                         json.dumps({'namespace': 'mainsail', 'key': 'a.b', 'value': 3}))
            box['status'] = conn.getresponse().status
            conn.close()

        thread = threading.Thread(target=post)
        thread.start()
        self.serve(lambda: not thread.is_alive())
        self.assertEqual(box['status'], 200)
        self.assertEqual(self.call('server.database.get_item', namespace='mainsail', key='a.b')['result']['value'], 3)


class TestPauseDialogReachesTheUi(WebUiBridgeTestCase):
    def setUp(self):
        super().setUp()
        for gate in (0, 1):
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.gcode.register_output_handler(self.bridge.host_output) # All output, as Moonraker relays it

    def pushed(self):
        lines = []
        self.client.sock.settimeout(0.2)
        try:
            while True:
                message = self.client.receive()
                if message.get('method') == 'notify_gcode_response':
                    lines.append(message['params'][0])
        except (socket.timeout, ConnectionError):
            pass
        return lines

    def test_error_dialog_arrives_as_action_prompt_lines_ending_in_show(self):
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        self.hh.run_gcode('MMU_PAUSE FORCE_IN_PRINT=1 MSG="Gate 1 jammed"')
        self.bridge.service()
        prompts = [l for l in self.pushed() if l.startswith('// action:prompt_')]
        self.assertEqual(prompts[0], '// action:prompt_begin Happy Hare Pause Notice')
        self.assertIn('// action:prompt_text Gate 1 jammed', prompts)
        self.assertNotIn('// action:prompt_text Print paused', prompts)
        self.assertTrue([p for p in prompts if p.startswith('// action:prompt_footer_button RESUME|RESUME|')], prompts)
        self.assertEqual(prompts[-1], '// action:prompt_show')

    def test_dialog_text_is_not_duplicated_into_the_terminal_console(self):
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        mark = len(self.hh.gcode.console)
        self.hh.run_gcode('MMU_PAUSE FORCE_IN_PRINT=1 MSG="Gate 1 jammed"')
        self.assertFalse([m for m in self.hh.gcode.console[mark:] if m.startswith('action:prompt_')])


if __name__ == '__main__':
    unittest.main()
