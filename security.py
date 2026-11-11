"""Fail-closed credentials and bounded HTTP workers. No credentials in logs."""
import base64
import binascii
import hashlib
import hmac
import json
from pathlib import Path
import socket
import threading
from http.server import ThreadingHTTPServer


class Credentials:
    def __init__(self, path):
        data = json.loads(Path(path).read_text())
        self.devices = data['devices']
        self.viewers = data['viewers']
        if not self.devices or not self.viewers:
            raise ValueError('device and viewer credentials required')
        for digest in [*self.devices.values(), *self.viewers.values()]:
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('expected SHA256 of high-entropy generated credential')

    def authenticate(self, header):
        if not header or len(header) > 512:
            return None
        if header.startswith('Bearer '):
            digest = hashlib.sha256(header[7:].encode()).hexdigest()
            for name, expected in self.devices.items():
                if hmac.compare_digest(digest, expected):
                    return ('device', name)
        elif header.startswith('Basic '):
            try:
                raw = base64.b64decode(header[6:], validate=True).decode('ascii')
                name, password = raw.split(':', 1)
            except (ValueError, UnicodeError, binascii.Error):
                return None
            digest = hashlib.sha256(password.encode()).hexdigest()
            expected = self.viewers.get(name, '0' * 64)
            if hmac.compare_digest(digest, expected) and name in self.viewers:
                return ('viewer', name)
        return None


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Avoid dumping arbitrary request strings/credentials into tracebacks.
        pass
