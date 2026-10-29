# Happy Hare test harness - a Moonraker-compatible API so a real Mainsail/Fluidd can talk to
# the simulated printer.
#
# WHAT IT IS FOR. Everything the web UIs show about Happy Hare (the MMU panel, the pause
# dialog, the console) is driven by Moonraker's JSON-RPC websocket. This serves just enough of
# that API from the harness that an unmodified Mainsail connects, renders the live MMU state,
# pops the real pause dialog Happy Hare sends, and sends its button
# presses back as gcode. It is a development and documentation aid, not a Moonraker clone:
# unknown methods get a JSON-RPC error and are logged so the gap is visible.
#
# THREADING. Sockets are served from threads, but the simulated printer is single threaded
# and must only be touched from the console's thread. Threads therefore never execute
# anything: they parse a request and put it on `inbox`. The console calls service() between
# keystrokes (from its idle tick), which runs the queued requests and writes the replies.
# Pushes the other way (gcode output, status changes) are plain socket writes, so they are
# safe from any thread.
#
# Standard library only: the harness has no websocket dependency and `make test` must not
# gain one.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import base64
import collections
import hashlib
import http.server
import json
import queue
import pathlib
import tempfile
import re

from test.hh.live_viewer import LiveViewer
import socket
import struct
import sys
import threading
import time
import urllib.parse

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
RPC_METHOD_NOT_FOUND = -32601
RPC_SERVER_ERROR = -32000
STATUS_PERIOD = 0.5                 # seconds between notify_status_update pushes
REQUEST_TIMEOUT = 20.0              # an HTTP caller waits this long for the console thread
GCODE_STORE_LINES = 1000


class RpcError(Exception):
    def __init__(self, message, code=RPC_SERVER_ERROR):
        super().__init__(message)
        self.code = code


class WebSocketConnection:
    """One connected client. Frames out are unmasked, frames in are masked (RFC 6455)."""

    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()
        self.open = True

    def send_text(self, text):
        self._send(0x1, text.encode('utf-8'))

    def close(self):
        if self.open:
            try:
                self._send(0x8, b'')
            except OSError:
                pass
            self.open = False

    def _send(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 1 << 16:
            header += bytes([126]) + struct.pack('>H', n)
        else:
            header += bytes([127]) + struct.pack('>Q', n)
        with self.lock:
            try:
                self.sock.sendall(bytes(header) + payload)
            except OSError:
                self.open = False

    def _read(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('closed')
            buf += chunk
        return buf

    def messages(self):
        """Yield each complete text message, answering pings, until the peer closes."""
        message = b''
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack('>H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if opcode == 0x8:
                return
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload                    # text, binary or continuation
            if fin:
                yield message.decode('utf-8', 'replace')
                message = b''


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A browser tab closing mid-request is routine, not worth a traceback
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


class WebUiBridge:
    """
    A Moonraker-compatible server over a harness session.

    `session` is the harness Session. `run_gcode(script)` is how a script from the UI is
    executed - the console passes its own so output streams to the terminal as usual.
    """

    def __init__(self, session, run_gcode, port=7125, host='127.0.0.1', log=None):
        self.session = session
        self.run_gcode = run_gcode
        self.port = port
        self.host = host
        self.log = log or (lambda text: None)
        self.inbox = queue.Queue()
        self.clients = set()
        self.subscriptions = {}                   # connection -> {object: fields or None}
        self.last_sent = {}                       # connection -> {object: status}
        self.database = {}                        # namespace -> {key: value}
        self.gcode_store = collections.deque(maxlen=GCODE_STORE_LINES)
        self.unsupported = collections.Counter()  # method -> times asked for
        self.started = time.time()
        self._next_status = 0.0
        self._server = None
        self.viewer = LiveViewer(session)
        self.restart_profile = None
        self.profile_name = ""
        self.uploads = tempfile.TemporaryDirectory(prefix="hh-viewer-")

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_GET(self):
                if self.headers.get('Upgrade', '').lower() == 'websocket':
                    bridge._serve_websocket(self)
                else:
                    bridge._serve_http(self, None)

            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                bridge._serve_http(self, self.rfile.read(n) if n else b'')

            def _cors(self):
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Headers', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

        try:
            self._server = _Server((self.host, self.port), Handler)
        except OSError:
            self.viewer.close()
            self.uploads.cleanup()
            raise
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def close(self):
        self.viewer.close()
        self.uploads.cleanup()
        for conn in list(self.clients):
            conn.close()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- socket side (any thread) ----------------------------------------------
    def _serve_websocket(self, handler):
        key = handler.headers.get('Sec-WebSocket-Key', '')
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        handler.send_response(101, 'Switching Protocols')
        handler.send_header('Upgrade', 'websocket')
        handler.send_header('Connection', 'Upgrade')
        handler.send_header('Sec-WebSocket-Accept', accept)
        handler.end_headers()
        handler.wfile.flush()
        conn = WebSocketConnection(handler.connection)
        self.clients.add(conn)
        self.log('web UI connected')
        try:
            for text in conn.messages():
                self.inbox.put((conn, text, None))
        except (OSError, ConnectionError):
            pass
        finally:
            self.clients.discard(conn)
            self.subscriptions.pop(conn, None)
            self.last_sent.pop(conn, None)
            conn.open = False
            handler.close_connection = True
            self.log('web UI disconnected')

    def _serve_http(self, handler, body):
        parsed = urllib.parse.urlparse(handler.path)
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if body:
            try:
                params.update(json.loads(body))
            except ValueError:
                pass
        if parsed.path in ('/sim/viewer', '/sim/viewer/', '/sim/viewer/app.js', '/sim/viewer/style.css', '/sim/viewer/playback.js'):
            filename = {'/sim/viewer/app.js': 'app.js', '/sim/viewer/playback.js': 'playback.js', '/sim/viewer/style.css': 'style.css'}.get(parsed.path, 'index.html')
            data = (pathlib.Path(__file__).parent / 'viewer' / filename).read_bytes()
            handler.send_response(200)
            handler.send_header('Content-Type', {'playback.js':'text/javascript', 'app.js':'text/javascript', 'style.css':'text/css'}.get(filename, 'text/html'))
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return
        if parsed.path in ('/sim/viewer/events', '/sim/viewer/telemetry'):
            try:
                after = int(params.get('after', 0))
            except (TypeError, ValueError):
                after = 0
            reader = self.viewer.read_telemetry if parsed.path.endswith('/telemetry') else self.viewer.read
            data = json.dumps({'result': reader(after)}, allow_nan=False).encode()
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return
        method = parsed.path.strip('/').replace('/', '.')
        if method == 'server.database.item':
            method = 'server.database.get_item' if body is None else 'server.database.post_item'
        done = threading.Event()
        box = {}
        request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        self.inbox.put((None, request, (done, box)))
        if done.wait(REQUEST_TIMEOUT) and 'result' in box:
            payload, status = json.dumps({'result': box['result']}, default=str), 200
        else:
            payload, status = json.dumps({'error': {'code': 404, 'message': box.get(
                'error', 'Not found: %s' % parsed.path)}}), 404
        data = payload.encode()
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(data)))
        handler._cors()
        handler.end_headers()
        handler.wfile.write(data)

    def notify(self, method, params):
        text = json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params}, default=str)
        for conn in list(self.clients):
            conn.send_text(text)

    # -- console side (single thread) ------------------------------------------
    def host_output(self, msg):
        """A line of gcode output, as Moonraker would relay it to the UI's console."""
        self.gcode_store.append({'message': msg, 'time': time.time(), 'type': 'response'})
        self.notify('notify_gcode_response', [msg])

    def service(self):
        """Run every queued request. Call from the thread that owns the session."""
        while True:
            try:
                conn, text, waiter = self.inbox.get_nowait()
            except queue.Empty:
                break
            self._handle(conn, text, waiter)
        self.viewer.capture('state', 'State changed')
        now = time.monotonic()
        if self.clients and now >= self._next_status:
            self._next_status = now + STATUS_PERIOD
            self.push_status()

    def _handle(self, conn, text, waiter):
        try:
            request = json.loads(text)
        except ValueError:
            return
        reply = {'jsonrpc': '2.0', 'id': request.get('id')}
        method = request.get('method', '')
        try:
            fn = getattr(self, 'rpc_' + method.replace('.', '_'), None)
            if fn is None:
                self.unsupported[method] += 1
                raise RpcError('Method not found: %s' % method, RPC_METHOD_NOT_FOUND)
            reply['result'] = fn(conn, request.get('params') or {})
        except RpcError as exc:
            reply['error'] = {'code': exc.code, 'message': str(exc)}
        except Exception as exc:                  # noqa: BLE001 - one bad request must not kill the console
            reply['error'] = {'code': RPC_SERVER_ERROR, 'message': '%s: %s' % (type(exc).__name__, exc)}
        if waiter is not None:
            done, box = waiter
            box.update({'result': reply['result']} if 'result' in reply
                       else {'error': reply['error']['message']})
            done.set()
        elif conn is not None:
            conn.send_text(json.dumps(reply, default=str))
        if method == 'printer.gcode.script':
            self.push_status()                    # show what the script changed immediately

    # -- status ----------------------------------------------------------------
    def _objects(self):
        return self.session.printer.objects

    def _status_of(self, name, fields=None):
        obj = self._objects().get(name)
        getter = getattr(obj, 'get_status', None)
        if getter is None:
            return None
        try:
            status = getter(self.session.reactor.monotonic())
        except Exception:                         # noqa: BLE001
            return None
        status = json.loads(json.dumps(status, default=str))
        if fields:
            status = {k: v for k, v in status.items() if k in fields}
        return status

    def _query(self, wanted):
        status = {}
        for name, fields in wanted.items():
            one = self._status_of(name, fields)
            if one is not None:
                status[name] = one
        return status

    def push_status(self):
        for conn, wanted in list(self.subscriptions.items()):
            now = self._query(wanted)
            before = self.last_sent.setdefault(conn, {})
            changed = {}
            for name, status in now.items():
                diff = {k: v for k, v in status.items() if before.get(name, {}).get(k, object()) != v}
                if diff:
                    changed[name] = diff
            if changed:
                self.last_sent[conn] = now
                conn.send_text(json.dumps({'jsonrpc': '2.0', 'method': 'notify_status_update',
                                           'params': [changed, self.session.reactor.monotonic()]},
                                          default=str))

    # -- Moonraker API ---------------------------------------------------------
    def rpc_server_connection_identify(self, conn, params):
        return {'connection_id': id(conn) & 0xFFFFFFFF}

    def rpc_server_info(self, conn, params):
        return {
            'klippy_connected': True, 'klippy_state': 'ready',
            'components': ['database', 'file_manager', 'klippy_apis', 'machine', 'history',
                           'mmu_server'],
            'failed_components': [], 'registered_directories': ['config', 'gcodes'],
            'warnings': [], 'websocket_count': len(self.clients),
            'moonraker_version': 'v0.9.3-sim', 'missing_klippy_requirements': [],
            'api_version': [1, 5, 0], 'api_version_string': '1.5.0',
        }

    def rpc_server_config(self, conn, params):
        return {'config': {'server': {'host': self.host, 'port': self.port}}, 'orig': {},
                'files': []}

    def rpc_server_temperature_store(self, conn, params):
        return {}

    def rpc_server_gcode_store(self, conn, params):
        count = int(params.get('count', GCODE_STORE_LINES))
        return {'gcode_store': list(self.gcode_store)[-count:]}

    def rpc_server_database_list(self, conn, params):
        return {'namespaces': sorted(self.database), 'backups': []}

    def rpc_server_database_get_item(self, conn, params):
        value = self.database.get(params.get('namespace'), {})
        key = params.get('key')
        for part in filter(None, (key or '').split('.')):
            if not isinstance(value, dict) or part not in value:
                raise RpcError("Key '%s' in namespace '%s' not found" % (key, params.get('namespace')))
            value = value[part]
        return {'namespace': params.get('namespace'), 'key': key, 'value': value}

    def rpc_server_database_post_item(self, conn, params):
        space = self.database.setdefault(params.get('namespace'), {})
        parts = [p for p in (params.get('key') or '').split('.') if p]
        for part in parts[:-1]:
            space = space.setdefault(part, {})
        if parts:
            space[parts[-1]] = params.get('value')
        return {'namespace': params.get('namespace'), 'key': params.get('key'),
                'value': params.get('value')}

    rpc_server_database_delete_item = rpc_server_database_post_item

    def rpc_server_webcams_list(self, conn, params):
        return {'webcams': []}

    def rpc_server_announcements_list(self, conn, params):
        return {'entries': [], 'feeds': []}

    def rpc_server_sensors_list(self, conn, params):
        return {'sensors': {}}

    def rpc_machine_device_power_devices(self, conn, params):
        return {'devices': []}

    def rpc_server_spoolman_status(self, conn, params):
        return {'spoolman_connected': False, 'pending_reports': [], 'spool_id': None}

    def rpc_sim_unsupported(self, conn, params):
        """Not Moonraker: which methods a UI asked for that this bridge does not serve."""
        return dict(self.unsupported)

    def rpc_server_files_roots(self, conn, params):
        return [{'name': 'config', 'path': '/sim/config', 'permissions': 'rw'},
                {'name': 'gcodes', 'path': '/sim/gcodes', 'permissions': 'rw'}]

    def rpc_server_files_list(self, conn, params):
        return []

    def rpc_server_files_get_directory(self, conn, params):
        return {'dirs': [], 'files': [], 'disk_usage': {'total': 0, 'used': 0, 'free': 0},
                'root_info': {'name': params.get('path', 'gcodes').split('/')[0], 'permissions': 'rw'}}

    def rpc_server_job_queue_status(self, conn, params):
        return {'queued_jobs': [], 'queue_state': 'ready'}

    def rpc_server_history_totals(self, conn, params):
        return {'job_totals': {'total_jobs': 0, 'total_time': 0, 'total_print_time': 0,
                               'total_filament_used': 0, 'longest_job': 0, 'longest_print': 0}}

    def rpc_server_history_list(self, conn, params):
        return {'count': 0, 'jobs': []}

    def rpc_machine_system_info(self, conn, params):
        return {'system_info': {'cpu_info': {'cpu_count': 4, 'total_memory': 4194304,
                                             'cpu_desc': 'Happy Hare simulator'},
                                'distribution': {'name': 'Simulator'}, 'network': {}}}

    def rpc_machine_proc_stats(self, conn, params):
        return {'moonraker_stats': [], 'throttled_state': None, 'cpu_temp': None,
                'network': {}, 'system_cpu_usage': {'cpu': 0.0}, 'system_uptime': time.time() - self.started,
                'system_memory': {'total': 0, 'available': 0, 'used': 0}, 'websocket_connections': len(self.clients)}

    def rpc_machine_update_status(self, conn, params):
        return {'version_info': {}, 'github_rate_limit': 60, 'github_requests_remaining': 60,
                'github_limit_reset_time': 0, 'busy': False}

    def rpc_printer_info(self, conn, params):
        return {'state': 'ready', 'state_message': 'Printer is ready', 'hostname': 'mmu-sim',
                'software_version': 'sim', 'cpu_info': 'Happy Hare simulator',
                'klipper_path': '/sim/klipper', 'python_path': '/sim/python',
                'log_file': '/sim/klippy.log', 'config_file': '/sim/printer.cfg'}

    def rpc_printer_objects_list(self, conn, params):
        return {'objects': [n for n in self._objects() if self._status_of(n) is not None]}

    def rpc_printer_objects_query(self, conn, params):
        wanted = params.get('objects') or {}
        return {'eventtime': self.session.reactor.monotonic(), 'status': self._query(wanted)}

    def rpc_printer_objects_subscribe(self, conn, params):
        wanted = params.get('objects') or {}
        self.subscriptions[conn] = wanted
        status = self._query(wanted)
        self.last_sent[conn] = status
        return {'eventtime': self.session.reactor.monotonic(), 'status': status}

    def rpc_printer_gcode_help(self, conn, params):
        return self.session.gcode.get_command_help()

    def rpc_printer_gcode_script(self, conn, params):
        script = params.get('script', '')
        self.gcode_store.append({'message': script, 'time': time.time(), 'type': 'command'})
        self.run_gcode(script)
        return 'ok'

    def rpc_printer_print_pause(self, conn, params):
        return self.rpc_printer_gcode_script(conn, {'script': 'PAUSE'})

    def rpc_printer_print_resume(self, conn, params):
        return self.rpc_printer_gcode_script(conn, {'script': 'RESUME'})

    def rpc_printer_print_cancel(self, conn, params):
        return self.rpc_printer_gcode_script(conn, {'script': 'CANCEL_PRINT'})

    def rpc_printer_firmware_restart(self, conn, params):
        raise RpcError('Not available in the simulator')

    rpc_printer_restart = rpc_printer_emergency_stop = rpc_printer_firmware_restart

    def rpc_sim_viewer_setup(self, conn, params):
        from test.hh.profiles import CONSOLE_PROFILES
        units = self.session.mmu.mmu_machine.units
        index = int(params.get('unit', 0))
        if not 0 <= index < len(units):
            raise RpcError('Unknown MMU unit')
        return dict(profile=self.profile_name,
                    profiles=[dict(name=p.name, description=p.description) for p in CONSOLE_PROFILES],
                    units=[u.name for u in units], parameters=self.viewer.parameters(index))

    def rpc_sim_viewer_configure(self, conn, params):
        index = int(params.get('unit', 0))
        available = {p['name'] for p in self.rpc_sim_viewer_setup(conn, {'unit':index})['parameters']}
        name, value = params.get('name'), str(params.get('value', ''))
        if name not in available:
            raise RpcError('Parameter is not available through MMU_TEST_CONFIG')
        if any(c in value for c in '\r\n;'):
            raise RpcError('Enter one parameter value')
        # JSON string quoting prevents a value from becoming another command or option.
        command = 'MMU_TEST_CONFIG UNIT=%d %s=%s' % (index, name.upper(), json.dumps(value))
        errors_before = len(self.session.errors)
        self.run_gcode(command)
        if len(self.session.errors) > errors_before:
            raise RpcError('; '.join(self.session.errors[errors_before:]))
        self.viewer.capture('config', command)
        return self.rpc_sim_viewer_setup(conn, {'unit':index})

    def rpc_sim_viewer_restart(self, conn, params):
        from test.hh.profiles import CONSOLE_PROFILES
        if self.restart_profile is None:
            raise RpcError('Profile restart requires the interactive console')
        profile = params.get('profile')
        if 'files' in params:
            files = params['files']
            if not isinstance(files, list) or not files or len(files) > 500:
                raise RpcError('Supply a configuration directory with at most 500 files')
            root = pathlib.Path(tempfile.mkdtemp(dir=self.uploads.name))
            total = 0
            for item in files:
                name, text = item['name'], item['text']
                path = pathlib.PurePosixPath(name)
                if path.is_absolute() or '..' in path.parts or not name.endswith('.cfg'):
                    raise RpcError('Supply relative .cfg filenames')
                total += len(text.encode())
                if total > 5_000_000:
                    raise RpcError('Configuration upload exceeds 5 MB')
                for inc in re.findall(r'(?im)^\s*\[include\s+([^]]+)\]', text):
                    if pathlib.PurePosixPath(inc).is_absolute() or '..' in pathlib.PurePosixPath(inc).parts:
                        raise RpcError('Includes must stay within the uploaded configuration')
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            roots = list(root.rglob('printer.cfg'))
            if len(roots) != 1:
                raise RpcError('Upload a directory containing one printer.cfg and its mmu directory')
            profile = str(roots[0].parent)
        elif profile not in {p.name for p in CONSOLE_PROFILES}:
            raise RpcError('Choose an available simulator profile')
        self.restart_profile(profile)
        return {'profile': self.profile_name}
