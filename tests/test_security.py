import base64
from hashlib import sha256
from http.client import HTTPConnection
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard as app
from security import Credentials, BoundedHTTPServer

class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = patch.object(app, 'DATA', Path(self.tmp.name))
        self.data.start()
        auth = Path(self.tmp.name)/'auth.json'
        auth.write_text(json.dumps({'devices': {'one': sha256(b'one').hexdigest(), 'two': sha256(b'two').hexdigest()},
                                   'viewers': {'reader': sha256(b'password').hexdigest()}}))
        self.credentials = Credentials(auth)
        self.app = app.Dashboard()
        self.server = BoundedHTTPServer(('127.0.0.1', 0), app.handler(self.app, self.credentials))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.data.stop()
        self.tmp.cleanup()

    def request(self, path='/', auth=None, telemetry=None, method='GET'):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {}
        if auth is not None: headers['Authorization'] = auth
        if telemetry is not None: headers['X-Badge-Supply-Mv'] = telemetry
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_auth_all_public_routes_and_no_unauthorized_side_effects(self):
        for path in ['/', '/dashboard.png', '/status.json', '/v1/manifest.json', '/v1/pages/home.bin']:
            self.assertEqual(self.request(path)[0], 401)
            self.assertEqual(self.request(path, 'Bearer wrong')[0], 401)
        with patch.object(self.app, 'record_battery') as record:
            self.assertEqual(self.request('/v1/manifest.json', telemetry='3730')[0], 401)
            record.assert_not_called()
        viewer = 'Basic ' + base64.b64encode(b'reader:password').decode()
        self.assertEqual(self.request('/dashboard.png', viewer)[0], 200)
        self.assertEqual(self.request('/v1/manifest.json', viewer, '3730')[0], 403)
        self.assertIsNone(self.app.battery_sample)
        for value in ['Basic !!!', 'Basic '+base64.b64encode(b'no-colon').decode(), 'Bearer '+ 'x'*600]:
            self.assertEqual(self.request(auth=value)[0], 401)

    def test_telemetry_rate_limit_and_read_only_retry(self):
        with patch.object(app.time, 'monotonic', return_value=1000):
            self.assertEqual(self.request('/v1/manifest.json', 'Bearer one', '3730')[0], 200)
            first = (Path(self.tmp.name)/'battery.json').read_bytes()
            self.assertEqual(self.request('/v1/manifest.json', 'Bearer one', '4200')[0], 429)
            self.assertEqual(self.request('/v1/manifest.json', 'Bearer two', '4200')[0], 429)
            self.assertEqual((Path(self.tmp.name)/'battery.json').read_bytes(), first)
            self.assertEqual(self.request('/v1/manifest.json', 'Bearer one')[0], 200)
        with patch.object(app.time, 'monotonic', return_value=1061):
            self.assertEqual(self.request('/v1/manifest.json', 'Bearer one', '4200')[0], 200)
        self.assertEqual(self.request('/v1/manifest.json', 'Bearer one', 'bad')[0], 400)
        with self.app.render_lock:
            self.assertEqual(self.request('/v1/manifest.json', 'Bearer two', '3800')[0], 429)

    def test_paths_methods_and_independent_revocation(self):
        for path in ['/../../etc/passwd', '/run/secrets/auth.json', '/v1/frames/unknown.bin']:
            self.assertEqual(self.request(path, 'Bearer one')[0], 404)
        self.assertEqual(self.request(auth='Bearer one', method='POST')[0], 501)
        self.assertEqual(self.request('/'+'x'*300, 'Bearer one')[0], 400)
        del self.credentials.devices['one']
        self.assertEqual(self.request(auth='Bearer one')[0], 401)
        self.assertEqual(self.request(auth='Bearer two')[0], 200)

    def test_worker_bound_and_socket_timeout(self):
        sockets = []
        try:
            for _ in range(8):
                sock = socket.create_connection(self.server.server_address, timeout=2)
                sock.sendall(b'GET / HTTP/1.1\r\n')
                sockets.append(sock)
            deadline = time.monotonic() + 2
            while self.server.slots._value and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.server.slots._value, 0)
            extra = socket.create_connection(self.server.server_address, timeout=2)
            self.assertEqual(extra.recv(1), b'')
            extra.close()
        finally:
            for sock in sockets: sock.close()

    def test_missing_credentials_fails_closed(self):
        with self.assertRaises(FileNotFoundError): Credentials('/nonexistent-auth-file')

if __name__ == '__main__': unittest.main()
