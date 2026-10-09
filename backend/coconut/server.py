"""Versioned HTTP API with bearer authentication and optional pinned TLS."""
from __future__ import annotations

import argparse
import faulthandler
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import ssl
import sys
import time
from urllib.parse import parse_qs, unquote, urlsplit

from .service import Service, ServiceError, digest


class APIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    tls_handshake_timeout = 5

    def __init__(self, address, service: Service, *, tls_context: ssl.SSLContext | None = None):
        if ':' in address[0]:
            self.address_family = socket.AF_INET6
        self.tls_context = tls_context
        super().__init__(address, Handler)
        self.service = service

    def service_actions(self):
        if self.service is not None and self.service.take_service_restart():
            raise KeyboardInterrupt

    def process_request_thread(self, request, client_address):
        if self.tls_context is not None:
            try:
                # Each worker owns its handshake. A silent peer cannot block acceptance.
                request = self.tls_context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
                request.settimeout(self.tls_handshake_timeout)
                request.do_handshake()
            except OSError:
                self.shutdown_request(request)
                return
        request.settimeout(35)
        super().process_request_thread(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = 'Coconut/1'
    # Keep the connection open between requests. Each new connection costs a TCP and TLS handshake over Tailscale.
    # Every response sets Content-Length, so the phone can find the end of each response.
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        # Paths can contain private IDs. Keep the default access log quiet.
        pass

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PATCH(self):
        self._handle('PATCH')

    def do_PUT(self):
        self._handle('PUT')

    def do_DELETE(self):
        self._handle('DELETE')

    def _json(self, status, value, close=False):
        content = json.dumps(value, separators=(',', ':')).encode()
        # A state response from the Mac mini data is about 3 MB and compresses about 5 times. URLSession asks for gzip.
        compressed = len(content) > 1024 and 'gzip' in self.headers.get('Accept-Encoding', '')
        if compressed:
            content = gzip.compress(content, compresslevel=5)
        self.send_response(status)
        if close:
            # send_header also marks this connection to close after the response.
            self.send_header('Connection', 'close')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        if compressed:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Vary', 'Accept-Encoding')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        if getattr(self, 'timings', None):
            self.send_header('Server-Timing', ', '.join(f'{name};dur={duration:.1f}' for name, duration in self.timings.items()))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(content)

    def _body(self):
        if self.headers.get('Transfer-Encoding'):
            raise ServiceError(400, 'Use Content-Length for request bodies.')
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ServiceError(400, 'The request size is invalid.')
        if size < 0 or size > 6 * 1024 * 1024:
            raise ServiceError(413, 'The request is too large.')
        if size and self.headers.get_content_type() != 'application/json':
            raise ServiceError(415, 'Send the request as application/json.')
        try:
            value = json.loads(self.rfile.read(size)) if size else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ServiceError(400, 'The request does not contain valid JSON.')
        if not isinstance(value, dict):
            raise ServiceError(400, 'Send a JSON object.')
        return value

    def _handle(self, method):
        self.connection.settimeout(35)
        try:
            url = urlsplit(self.path)
            service = self.server.service
            if method == 'GET' and (self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0'):
                # GET routes never read a body. Unread bytes would corrupt the next request on this connection.
                raise ServiceError(400, 'Send GET requests without a body.')
            if method == 'GET' and url.path == '/health':
                self._json(200, {'ok': True, 'api_version': 1})
                return
            if method == 'POST' and url.path == '/v1/pair':
                self._json(200, service.pair(self._body()))
                return
            authorization = self.headers.get('Authorization', '')
            if not authorization.startswith('Bearer '):
                raise ServiceError(401, 'Pair this phone with the Mac to continue.')
            started = time.perf_counter()
            device = service.authenticate(authorization[7:])
            self.timings = {'auth': (time.perf_counter() - started) * 1000}
            if method == 'POST' and url.path == '/v1/maintenance/workspace-recovery':
                self._json(200, service.request_workspace_recovery(
                    self._body(), self.headers.get('Idempotency-Key', ''), device))
                return
            if method == 'POST' and url.path == '/v1/maintenance/checkout-recovery':
                self._json(200, service.request_checkout_recovery(
                    self._body(), self.headers.get('Idempotency-Key', ''), authorization[7:]))
                return
            if method == 'GET':
                if url.path.startswith('/v1/tasks/') and url.path.endswith('/checkout-recovery'):
                    self._json(200, service.checkout_recovery_preview(
                        url.path.removeprefix('/v1/tasks/').removesuffix('/checkout-recovery'), device))
                elif url.path == '/v1/state':
                    self._json(200, service.snapshot(timings=self.timings, content_only=self.headers.get('X-Coconut-State') in ('content', 'recent'),
                        compact_history=self.headers.get('X-Coconut-State') == 'recent'))
                elif url.path == '/v1/search':
                    self._json(200, service.search_conversations(parse_qs(url.query).get('q', [''])[0]))
                elif url.path == '/v1/state/settings':
                    self._json(200, service.native_snapshot())
                elif url.path.startswith('/v1/cards/'):
                    self._json(200, service.card(url.path.removeprefix('/v1/cards/')))
                elif url.path.startswith('/v1/history/'):
                    query = parse_qs(url.query)
                    try:
                        before = int(query['before'][0]) if 'before' in query else None
                    except ValueError:
                        raise ServiceError(400, 'before must contain a number.')
                    self._json(200, service.message_page(url.path.removeprefix('/v1/history/'), before))
                elif url.path == '/v1/updates':
                    query = parse_qs(url.query)
                    try:
                        after = int(query.get('after', ['0'])[0])
                        wait = float(query.get('wait', ['0'])[0])
                    except ValueError:
                        raise ServiceError(400, 'after and wait must contain numbers.')
                    self._json(200, service.updates(after, wait, collection_deltas=self.headers.get('X-Coconut-Updates') in ('2', '3', '4'),
                        native_events=self.headers.get('X-Coconut-Updates') in ('3', '4'),
                        compact_history=self.headers.get('X-Coconut-Updates') == '4'))
                elif url.path == '/v1/settings/google':
                    self._json(200, service.google_connection('status', {}, device))
                elif url.path == '/v1/push/config':
                    self._json(200, service.push.relay.configuration())
                elif url.path == '/v1/events':
                    query = parse_qs(url.query)
                    try:
                        after = int(query.get('after', ['0'])[0])
                        wait = float(query.get('wait', ['0'])[0])
                    except ValueError:
                        raise ServiceError(400, 'after and wait must contain numbers.')
                    self._json(200, service.events(after, wait))
                elif url.path == '/v1/learning':
                    self._json(200, service.learning())
                elif url.path.startswith('/v1/artifacts/') and url.path.endswith('/content'):
                    parts = url.path.strip('/').split('/')
                    if len(parts) != 4:
                        raise ServiceError(404, 'This artifact was not found.')
                    content, filename = service.artifact_content(parts[2])
                    safe_filename = ''.join(c for c in filename if c.isascii() and c.isalnum() or c in '._- ')[:160] or 'artifact'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Disposition', f'attachment; filename="{safe_filename}"')
                    self.send_header('Content-Length', str(len(content)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    self.wfile.write(content)
                else:
                    raise ServiceError(404, 'This route was not found.')
                return
            response = service.mutate(method, url.path, self._body(), self.headers.get('Idempotency-Key', ''), device)
            self._json(200, response)
        except ServiceError as error:
            # An error can come before the request body was read. Unread body bytes would corrupt the next request.
            self._json(error.status, {'error': str(error)}, close=True)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self._json(500, {'error': 'The Mac could not finish this request. Try again with the same request key.'}, close=True)


def create_pairing_code(data_dir: str | Path, lifetime: int = 300) -> str:
    """Admin-only operation. It does not recover tasks in another service process."""
    path = Path(data_dir).expanduser() / 'coconut.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    code = ''.join(secrets.choice('0123456789') for _ in range(10))
    with sqlite3.connect(path, timeout=10) as connection:
        os.chmod(path, 0o600)
        connection.execute('CREATE TABLE IF NOT EXISTS pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)')
        connection.execute('DELETE FROM pairing WHERE expires < ? OR used=1', (time.time(),))
        connection.execute('INSERT INTO pairing(hash,expires) VALUES (?,?)', (digest(code), time.time() + lifetime))
    return code


def write_process_record(service):
    """Write the record that identifies this process and says that it drains before an update stops it.

    A drain request belongs to the process that the updater asked. An old one is removed first, so that a request written
    after the record is never removed (see Service.poll_service_drain).
    """
    service.clear_service_drain()
    (service.root / 'process.json').write_text(json.dumps({
        'pid': os.getpid(), 'data_dir': str(service.root),
        'project': str(Path(__file__).resolve().parents[2]),
        'drain': True,
    }))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run Coconut on this Mac.')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8960)
    parser.add_argument('--provider', choices=('fixture', 'hermes'), default='fixture')
    parser.add_argument('--cert', type=Path)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--pair', action='store_true')
    parser.add_argument('--hermes-python', type=Path)
    args = parser.parse_args(argv)
    if args.pair:
        try:
            print(create_pairing_code(args.data_dir))
        except ServiceError as error:
            parser.error(str(error))
        return
    if bool(args.cert) != bool(args.key):
        parser.error('Supply both --cert and --key.')
    if args.host not in ('127.0.0.1', '::1', 'localhost') and not args.cert:
        parser.error('TLS is required for access outside this Mac. Supply --cert and --key.')
    from .providers import FixtureProvider, HermesProvider
    if args.provider == 'fixture':
        provider = FixtureProvider(args.data_dir)
    else:
        provider = HermesProvider(args.data_dir, **({'python': str(args.hermes_python)} if args.hermes_python else {}))
    try:
        service = Service(args.data_dir, provider, start_workers=False)
    except ServiceError as error:
        parser.error(str(error))
    server = None
    try:
        context = None
        if args.cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(args.cert, args.key)
        server = APIServer((args.host, args.port), service, tls_context=context)
    except OSError as error:
        if server:
            server.server_close()
        service.close()
        parser.error(f'The service could not listen: {error}')
    write_process_record(service)
    service.restart_enabled = os.environ.get("COCONUT_MANAGED_SERVICE") == "1"
    service.start()
    print(f'Coconut is listening on {args.host}:{args.port}. Provider: {args.provider}.', flush=True)
    if args.provider == 'fixture':
        print('Fixture mode uses scripted responses. It does not execute Hermes.', flush=True)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, 'SIGUSR1'):
        # Local diagnostics show thread stacks without stopping active work.
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == '__main__':
    main()
