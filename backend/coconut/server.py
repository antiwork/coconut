"""Versioned HTTP API with bearer authentication, on loopback only.

The main listener serves the tools on this Mac. The tunnel listener is for the tunnel sidecar
(scripts/tunnel-sidecar.swift), which carries the phone's requests from the relay. The sidecar sets X-Coconut-Peer to
the static key of the phone at the other end of each Noise session. Only that listener reads the header.
"""
from __future__ import annotations

import argparse
import faulthandler
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from uuid import UUID
from pathlib import Path
import re
import signal
import socket
import socketserver
import sqlite3
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

# The modules of the full service load in main, so a team-only service (team_only.py) never imports them.
from .service_base import ServiceError
from .team_files import FileContent

PEER_KEY = re.compile(r'[0-9a-f]{64}')
# The page route of the tooling of the Mac. POST saves a page, and DELETE deletes it.
PAGE_ROUTE = '/v1/gateway/document'
# Only the main listener serves these, each with POST. The tunnel listener carries the phone's requests.
GATEWAY_ROUTES = ('/v1/gateway/poll', '/v1/gateway/events', '/v1/gateway/tools', '/v1/gateway/cron',
                  '/v1/gateway/wake', '/v1/gateway/messages', '/v1/gateway/files', '/v1/gateway/steering',
                  PAGE_ROUTE)
# The routes of the adapter that a team-only service serves: the turns of the team chat (team_turns.py) and the sends
# of Hermes outside a turn. No tool, no cron and no file of a phone.
TEAM_GATEWAY_ROUTES = ('/v1/gateway/poll', '/v1/gateway/events', '/v1/gateway/messages', '/v1/gateway/files')
# The routes of the team rooms (team.py). A paired device calls them on each listener. A tool on this Mac calls them
# on the main listener with the token of the bridge file.
TEAM_ROUTES = '/v1/team/'


# A path segment with a digit and at least 8 characters is an ID. The log keeps the route, not private IDs.
PATH_ID = re.compile(r'/(?=[^/]*[0-9])[^/]{8,}')


def request_timing(method, target, status, seconds, stages=None):
    """Log how long the service took to answer one request from the phone, as `coconut.request_timing`.

    A long poll (`wait` in the query) waits for new events on purpose, so its line also gives the wait.
    The line names the route without IDs or query values.
    """
    url = urlsplit(target)
    record = dict(method=method, route=PATH_ID.sub('/:id', url.path), status=status, ms=round(seconds * 1000, 1))
    wait = parse_qs(url.query).get('wait')
    if wait:
        try:
            record['wait_s'] = float(wait[0])
        except ValueError:
            pass
    if stages:
        record['stages_ms'] = {name: round(value, 1) for name, value in stages.items()}
    logging.getLogger(__name__).warning('coconut.request_timing %s', json.dumps(record))


class APIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    # Only TunnelServer reads X-Coconut-Peer.
    tunnel = False

    def __init__(self, address, service: Service):
        if ':' in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)
        self.service = service

    def server_bind(self):
        # HTTPServer.server_bind sets server_name with socket.getfqdn, a reverse name lookup. On a CI Mac that lookup
        # of 127.0.0.1 blocked the start for more than 30 seconds. Each listener is on loopback, and nothing reads
        # server_name, so the bound host is enough.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def service_actions(self):
        if self.service is not None and self.service.take_service_restart():
            raise KeyboardInterrupt

    def process_request_thread(self, request, client_address):
        request.settimeout(35)
        super().process_request_thread(request, client_address)


class TunnelServer(APIServer):
    """The plain HTTP listener on loopback for the tunnel sidecar."""
    tunnel = True

    def __init__(self, port: int, service: Service):
        super().__init__(('127.0.0.1', port), service)

    def service_actions(self):
        # Only the main listener takes a restart request, so the tunnel listener never consumes it.
        pass


def accepts_gzip(header):
    """Return True when Accept-Encoding names gzip with a quality above zero (RFC 9110, section 12.5.3)."""
    for item in header.split(','):
        name, *parameters = (part.strip() for part in item.split(';'))
        if name.lower() != 'gzip':
            continue
        quality = next((value[2:] for value in parameters if value.lower().startswith('q=')), '1')
        try:
            return float(quality) > 0
        except ValueError:
            return False
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = 'Coconut/1'
    # A client can keep one connection open for many requests.
    # Every response sets Content-Length, so the client can find the end of each response.
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
        compressed = len(content) > 1024 and accepts_gzip(self.headers.get('Accept-Encoding', ''))
        if compressed:
            content = gzip.compress(content, compresslevel=5)
        self.status = status
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

    def _bytes(self, content, filename, content_type, fallback):
        """Send the bytes of a file as an attachment. The name in the header keeps only safe ASCII characters."""
        self.status = 200
        safe_filename = ''.join(c for c in filename if c.isascii() and c.isalnum() or c in '._- ')[:160] or fallback
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Disposition', f'attachment; filename="{safe_filename}"')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
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

    @property
    def recent_work(self):
        """A phone that shows no finished Work asks the recent state to leave it out."""
        return self.headers.get('X-Coconut-Work') == 'recent'

    def _gateway(self, bridge, path, method):
        """The routes of the `coconut` adapter of the Hermes gateway on this Mac, and the page route of the tooling.
        See gateway_bridge."""
        if not bridge.authorized(self.headers.get('Authorization', '')):
            raise ServiceError(401, 'Use the token in <data>/gateway/bridge.json.')
        data = self._body()
        try:
            if path == '/v1/gateway/tools':
                self._json(200, self.server.service.gateway_tool(data))
            elif path == '/v1/gateway/cron':
                self._json(200, self.server.service.gateway_cron(data))
            elif path == '/v1/gateway/wake':
                self._json(200, self.server.service.gateway_hermes_run(data))
            elif path == '/v1/gateway/messages':
                self._json(200, self.server.service.gateway_message(data))
            elif path == '/v1/gateway/files':
                self._json(200, self.server.service.gateway_file(data))
            elif path == '/v1/gateway/steering':
                self._json(200, self.server.service.gateway_steering(data))
            elif path == PAGE_ROUTE:
                # Only this route takes DELETE (_serve).
                service = self.server.service
                self._json(200, service.gateway_document_delete(data) if method == 'DELETE'
                           else service.gateway_document(data))
            else:
                self._json(200, bridge.poll(data) if path == '/v1/gateway/poll' else bridge.event(data))
        except (TypeError, ValueError) as error:
            raise ServiceError(400, str(error)) from None

    def _team(self, service, method, url, device=''):
        """The routes of the team rooms. The caller checked the token of a device or of the bridge file. See team.py.

        Only a paired device gives its `Idempotency-Key`, because the app repeats a write after a lost answer. A tool
        of this Mac sends no key, and its request runs each time. A GET has no body, so its data is the query, with
        the last value of each name."""
        key = self.headers.get('Idempotency-Key', '') if device else ''
        data = ({name: values[-1] for name, values in parse_qs(url.query).items()} if method == 'GET'
                else self._body())
        # An app that knows direct messages says so on each read of the rooms (docs/team-log.md, "Direct messages").
        direct = bool(device) and self.headers.get('X-Coconut-Team') == 'direct'
        answer = service.team_request(method, url.path, data, key, device, direct)
        if isinstance(answer, FileContent):
            self._bytes(answer.content, answer.name, answer.type, 'file')
        else:
            self._json(200, answer)

    def _team_only(self, service, method, url):
        """The routes of a team-only service (team_only.py): the team routes and the page route (POST saves, DELETE
        deletes), for a tool of this Mac, and the routes of the adapter of the team mode (TEAM_GATEWAY_ROUTES), each
        with the token of the bridge file. Each other route says that this Mac runs Coconut for a team only."""
        page = method in ('POST', 'DELETE') and url.path == PAGE_ROUTE
        gateway = method == 'POST' and url.path in TEAM_GATEWAY_ROUTES
        if not page and not gateway and not url.path.startswith(TEAM_ROUTES):
            service.refuse()
        if not service.gateway_bridge.authorized(self.headers.get('Authorization', '')):
            raise ServiceError(401, 'Use the token in <data>/gateway/bridge.json.')
        if page:
            data = self._body()
            self._json(200, service.gateway_document_delete(data) if method == 'DELETE'
                       else service.gateway_document(data))
        elif gateway:
            self._gateway(service.gateway_bridge, url.path, method)
        else:
            self._team(service, method, url)

    def _handle(self, method):
        """Serve one request. Log the time of each request from the phone (see request_timing)."""
        started = time.perf_counter()
        # One connection carries many requests. Each request starts without the status and stages of the one before.
        self.status = self.timings = None
        try:
            self._serve(method)
        finally:
            if self.server.tunnel:
                request_timing(method, self.path, self.status, time.perf_counter() - started, getattr(self, 'timings', None))

    def _serve(self, method):
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
            if getattr(service, 'team_only', False):
                self._team_only(service, method, url)
                return
            gateway = ((method == 'POST' and url.path in GATEWAY_ROUTES)
                       or (method == 'DELETE' and url.path == PAGE_ROUTE))
            if gateway and not self.server.tunnel and hasattr(service, 'gateway_bridge'):
                self._gateway(service.gateway_bridge, url.path, method)
                return
            team = url.path.startswith(TEAM_ROUTES) and hasattr(service, 'team_request')
            if (team and not self.server.tunnel
                    and service.gateway_bridge.authorized(self.headers.get('Authorization', ''))):
                self._team(service, method, url)
                return
            peer = None
            if self.server.tunnel:
                # Every tunnel request names its phone. A request without the key never reaches a device.
                peer = self.headers.get('X-Coconut-Peer', '')
                if not PEER_KEY.fullmatch(peer):
                    raise ServiceError(401, 'Pair this phone with the Mac to continue.')
            if method == 'POST' and url.path == '/v1/pair':
                self._json(200, service.pair(self._body(), peer))
                return
            authorization = self.headers.get('Authorization', '')
            if not authorization.startswith('Bearer '):
                raise ServiceError(401, 'Pair this phone with the Mac to continue.')
            started = time.perf_counter()
            device = service.authenticate(authorization[7:], peer)
            self.timings = {'auth': (time.perf_counter() - started) * 1000}
            if getattr(service, 'setup_only', False) and not (method == 'GET' and url.path == '/v1/setup'):
                # Without Hermes, only the setup status answers. See hermes_unavailable.
                service.refuse()
            if team:
                # The request log of the tunnel listener keeps the route without its IDs, and never a body, so an
                # invite link in a join stays out of it.
                self._team(service, method, url, device)
                return
            if method == 'POST' and url.path == '/v1/maintenance/workspace-recovery':
                self._json(200, service.request_workspace_recovery(
                    self._body(), self.headers.get('Idempotency-Key', ''), device))
                return
            if method == 'GET':
                if url.path == '/v1/state':
                    self._json(200, service.snapshot(timings=self.timings, content_only=self.headers.get('X-Coconut-State') in ('content', 'recent'),
                        compact_history=self.headers.get('X-Coconut-State') == 'recent', recent_work=self.recent_work))
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
                        compact_history=self.headers.get('X-Coconut-Updates') == '4', recent_work=self.recent_work))
                elif url.path == '/v1/todos':
                    self._json(200, service.todos.list(fresh=parse_qs(url.query).get('fresh') == ['1']))
                elif url.path == '/v1/business':
                    self._json(200, service.business.catalog(fresh=parse_qs(url.query).get('fresh') == ['1']))
                elif url.path == '/v1/business/work':
                    query = parse_qs(url.query)
                    self._json(200, service.business.work(query.get('function', [None])[0], query.get('project', [None])[0], query.get('state', ['open'])[0]))
                elif url.path.startswith('/v1/business/reports/'):
                    from .business_reports import report
                    self._json(200, report(service.business, url.path.rsplit('/', 1)[1]))
                elif url.path == '/v1/business/ledger':
                    from .business_reports import ledger
                    self._json(200, ledger(service.business, parse_qs(url.query).get('path', [None])[0]))
                elif url.path == '/v1/github/preview':
                    self._json(200, service.github_previews.preview(parse_qs(url.query).get('url', [''])[0]))
                elif url.path in ('/v1/github/item', '/v1/github/item/files'):
                    query = parse_qs(url.query)
                    read = service.github_items.files if url.path.endswith('/files') else service.github_items.item
                    self._json(200, read(query.get('repository', [''])[0], query.get('number', [''])[0]))
                elif url.path == '/v1/settings/github':
                    self._json(200, service.github_connection('status'))
                elif url.path == '/v1/settings/google':
                    self._json(200, service.google_connection('status', {'mailbox': parse_qs(url.query).get('mailbox', [''])[0]}, device))
                elif url.path.startswith('/v1/support/conversations/'):
                    from .support_threads import conversation, full_message
                    parts = url.path.split('/')[4:]
                    if len(parts) == 3 and parts[1] == 'messages':
                        self._json(200, full_message(service.root, parts[0], parts[2]))
                    elif len(parts) == 1:
                        self._json(200, conversation(service.root, parts[0], everything=parse_qs(url.query).get('all') == ['1']))
                    else:
                        raise ServiceError(404, 'This route was not found.')
                elif url.path == '/v1/device/push':
                    self._json(200, service.push_status(device))
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
                elif url.path == '/v1/usage':
                    try:
                        days = int(parse_qs(url.query).get('days', ['30'])[0])
                    except ValueError:
                        raise ServiceError(400, 'days must contain a number.')
                    self._json(200, service.usage(days))
                elif url.path == '/v1/setup':
                    self._json(200, service.hermes_setup())
                elif url.path == '/v1/documents':
                    self._json(200, service.documents())
                elif url.path.startswith('/v1/documents/') and url.path.count('/') == 3:
                    self._json(200, service.document(url.path.rsplit('/', 1)[1]))
                elif url.path.startswith('/v1/tasks/') and '/activity' in url.path:
                    # The live activity of a turn (turn_activity.py): its feed, one image of it, or the start of a
                    # file that the turn touched.
                    parts = url.path.strip('/').split('/')
                    if len(parts) == 4 and parts[3] == 'activity':
                        self._json(200, service.task_activity(parts[2]))
                    elif len(parts) == 6 and parts[3:5] == ['activity', 'images']:
                        self._bytes(service.activity_image(parts[2], parts[5]), parts[5] + '.jpg', 'image/jpeg', 'image')
                    elif len(parts) == 5 and parts[3:] == ['activity', 'file']:
                        self._json(200, service.activity_file(parts[2], parse_qs(url.query).get('path', [''])[0]))
                    else:
                        raise ServiceError(404, 'This route was not found.')
                elif url.path.startswith('/v1/bots/') and url.path.endswith('/usage') and url.path.count('/') == 4:
                    # The tokens and the cost of one chat (usage.py, chat_usage).
                    try:
                        days = int(parse_qs(url.query).get('days', ['30'])[0])
                        bot_id = str(UUID(url.path.split('/')[3]))
                    except ValueError:
                        raise ServiceError(400, 'Name a chat and a number of days.')
                    self._json(200, service.chat_usage(bot_id, days))
                elif url.path.startswith('/v1/artifacts/') and url.path.endswith('/content'):
                    parts = url.path.strip('/').split('/')
                    if len(parts) != 4:
                        raise ServiceError(404, 'This artifact was not found.')
                    content, filename = service.artifact_content(parts[2])
                    self._bytes(content, filename, 'application/octet-stream', 'artifact')
                else:
                    raise ServiceError(404, 'This route was not found.')
                return
            response = service.mutate(method, url.path, self._body(), self.headers.get('Idempotency-Key', ''), device)
            self._json(200, response)
        except ServiceError as error:
            # An error can come before the request body was read. Unread body bytes would corrupt the next request.
            self._json(error.status, {'error': str(error), **getattr(error, 'details', {})}, close=True)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self._json(500, {'error': 'The Mac could not finish this request. Try again with the same request key.'}, close=True)


def create_pairing_code(data_dir: str | Path, lifetime: int = 300) -> str:
    """Admin-only operation. It does not recover tasks in another service process."""
    return store_pairing_code(data_dir, lifetime, False)[0]


def create_tunnel_pairing(data_dir: str | Path) -> tuple[str, int]:
    """Return the secret of a version 2 pairing link and its expiry in Unix seconds."""
    from .service import TUNNEL_PAIRING_LIFETIME
    return store_pairing_code(data_dir, TUNNEL_PAIRING_LIFETIME, True)


def store_pairing_code(data_dir: str | Path, lifetime: int, tunnel: bool) -> tuple[str, int]:
    """The store keeps only the hash of the code. See issue_pairing_code."""
    from .service import issue_pairing_code, migrate_device_peers
    path = Path(data_dir).expanduser() / 'coconut.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with sqlite3.connect(path, timeout=10) as connection:
        os.chmod(path, 0o600)
        connection.execute('CREATE TABLE IF NOT EXISTS pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)')
        migrate_device_peers(connection)
        return issue_pairing_code(connection, lifetime, tunnel)


def write_process_record(service):
    """Write the record that identifies this process and says that it drains before an update stops it.

    A drain request belongs to the process that the updater asked. An old one is removed first, so that a request written
    after the record is never removed (see Service.poll_service_drain).
    """
    service.clear_service_drain()
    (service.root / 'process.json').write_text(json.dumps({
        'pid': os.getpid(), 'data_dir': str(service.root),
        'project': str(Path(__file__).resolve().parents[2]),
        'drain': getattr(service, 'drains', True),
        # The Mac app and the tool tell a team-only service from a full one (team_only.py).
        **({'mode': 'team'} if getattr(service, 'team_only', False) else {}),
    }))


def hermes_status(data_dir, argv) -> dict:
    """Find and check the person's Hermes, and run this service under its Python.

    The Hermes Python has the Hermes modules that the service imports, for example for Kanban. When this process
    runs under another Python, it starts again under the Hermes Python, with the checked status in its environment.
    Without a usable Hermes, the service stays under this Python in refuse mode.
    """
    from . import hermes_runtime
    saved = os.environ.pop('COCONUT_HERMES_STATUS', None)
    # A launcher can name the Hermes Python differently in sys.executable, so this process starts again only once.
    restarted = os.environ.pop('COCONUT_HERMES_RESTARTED', None) == '1'
    try:
        status = json.loads(saved) if saved else None
    except ValueError:
        status = None
    if not isinstance(status, dict):
        status = hermes_runtime.status(data_dir)
    if status.get('status') == 'ok' and not restarted and os.path.abspath(sys.executable) != os.path.abspath(status['python']):
        os.environ.update(COCONUT_HERMES_STATUS=json.dumps(status), COCONUT_HERMES_RESTARTED='1')
        try:
            os.execv(status['python'], [status['python'], '-m', 'coconut', *argv])
        except OSError as error:
            os.environ.pop('COCONUT_HERMES_STATUS', None)
            os.environ.pop('COCONUT_HERMES_RESTARTED', None)
            return {'status': 'incompatible', 'version': status.get('version'),
                    'reason': f'The Hermes Python cannot start: {error.strerror}.'}
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run Coconut on this Mac.')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8960, help='The port of the main listener on 127.0.0.1.')
    parser.add_argument('--provider', choices=('fixture', 'hermes', 'agent-sdk'), default='fixture')
    parser.add_argument('--tunnel-port', type=int, default=0,
                        help='Also listen with plain HTTP on 127.0.0.1 at this port for the tunnel sidecar. 0 turns it off.')
    parser.add_argument('--pair', action='store_true')
    parser.add_argument('--tunnel', action='store_true',
                        help='With --pair: print the secret for a version 2 pairing link and its expiry in Unix seconds.')
    parser.add_argument('--team-only', action='store_true',
                        help='Keep only the team rooms and the pages of the agents that this Mac declares (team_only.py). '
                             'COCONUT_MODE=team in the environment does the same.')
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    mode = os.environ.get('COCONUT_MODE', '')
    if mode not in ('', 'team'):
        parser.error('COCONUT_MODE must be team, or empty for the full service.')
    team_only = args.team_only or mode == 'team'
    if team_only and args.pair:
        parser.error('A team-only service pairs no phone.')
    if args.pair:
        try:
            if args.tunnel:
                print(*create_tunnel_pairing(args.data_dir))
            else:
                print(create_pairing_code(args.data_dir))
        except ServiceError as error:
            parser.error(str(error))
        return
    hermes = None
    try:
        if team_only:
            from .team_only import TeamOnlyService
            service = TeamOnlyService(args.data_dir)
        elif args.provider == 'fixture':
            from .providers import FixtureProvider
            from .service import Service
            service = Service(args.data_dir, FixtureProvider(args.data_dir), start_workers=False)
        elif args.provider == 'agent-sdk':
            # Opt-in, off by default: Claude's Agent SDK runs each turn, without Hermes (docs/cloud.md).
            from .agent_sdk_provider import AgentSDKProvider
            from .service import Service
            service = Service(args.data_dir, AgentSDKProvider(args.data_dir), start_workers=False)
        else:
            from . import hermes_runtime
            from .hermes_unavailable import UnavailableService
            from .providers import HermesProvider
            from .service import Service
            hermes = hermes_status(args.data_dir, sys.argv[1:] if argv is None else argv)
            if hermes['status'] == 'ok':
                # The service imports some Hermes modules itself. Like the workers, it loads them from the checked source
                # folder, not from a build copy in the installer's dependency environment.
                if hermes['root'] not in sys.path:
                    sys.path.insert(1, hermes['root'])
                os.environ.update(hermes_runtime.NO_LAZY_INSTALLS)
                provider = HermesProvider(args.data_dir, python=hermes['python'], hermes_root=hermes['root'],
                                          home=hermes_runtime.DEFAULT_HOME)
                # Each Hermes profile of the person is a bot (bot_profiles).
                service = Service(args.data_dir, provider, start_workers=False, hermes_home=hermes.get('home'))
                service.hermes_status = hermes_runtime.public(hermes)
            else:
                service = UnavailableService(args.data_dir, hermes)
    except ServiceError as error:
        parser.error(str(error))
    server = tunnel = None
    try:
        # The phone reaches this Mac only through the tunnel sidecar, so no listener accepts connections from the network.
        server = APIServer(('127.0.0.1', args.port), service)
        if args.tunnel_port and not team_only:
            tunnel = TunnelServer(args.tunnel_port, service)
    except OSError as error:
        if server:
            server.server_close()
        service.close()
        parser.error(f'The service could not listen: {error}')
    if hasattr(service, 'gateway_bridge'):
        # The `coconut` adapter of the Hermes gateway reads the address and the token of this process here.
        service.gateway_bridge.publish(f'http://127.0.0.1:{server.server_address[1]}')
    write_process_record(service)
    service.restart_enabled = os.environ.get("COCONUT_MANAGED_SERVICE") == "1"
    if service.restart_enabled and hermes is not None:
        # launchd starts the service again after it exits, so it can restart for a new Hermes.
        service.watch_hermes(hermes)
    service.start()
    if team_only:
        print(f'Coconut is listening on 127.0.0.1:{args.port} for a team only. It runs no agent and changes no Hermes.',
              flush=True)
        if args.tunnel_port:
            print('A team-only service has no phone, so it opens no tunnel listener.', flush=True)
    else:
        print(f'Coconut is listening on 127.0.0.1:{args.port}. Provider: {args.provider}.', flush=True)
        if tunnel:
            threading.Thread(target=tunnel.serve_forever, kwargs={'poll_interval': .2}, name='tunnel-listener',
                             daemon=True).start()
            print(f'The tunnel sidecar connects to 127.0.0.1:{args.tunnel_port}.', flush=True)
        if args.provider == 'fixture':
            print('Fixture mode uses scripted responses. It does not execute Hermes.', flush=True)
        elif args.provider == 'agent-sdk':
            print('Claude\'s Agent SDK runs each turn. Hermes is not used.', flush=True)
        elif isinstance(service, UnavailableService):
            print(f'Coconut runs no agents, because Hermes is {hermes["status"]}: {hermes["reason"]}', flush=True)
        else:
            print(f'Coconut uses Hermes {hermes["version"] or "of unknown version"} in {hermes["root"]}.', flush=True)
            warning = hermes_runtime.untested_warning(hermes['version'])
            if warning:
                print(warning, flush=True)
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
        if team_only:
            # The order of a clean stop: the listener answers again while the adapters collect the stops of the
            # running turns of the team chat (team_turns.TeamTurns.team_close_turns), then it stops, then the service.
            threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .2}, daemon=True).start()
            service.team_close_turns()
            server.shutdown()
        if tunnel:
            tunnel.shutdown()
            tunnel.server_close()
        server.server_close()
        service.close()


if __name__ == '__main__':
    main()
