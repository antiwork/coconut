"""A local Anthropic Messages endpoint that forwards mote's model calls to Claude on Amazon Bedrock (mote_runtime.py).

mote reaches Claude through the Anthropic API or Vertex. On a Mac whose Claude access is Amazon Bedrock, the agent's
`mote.env` holds a Bedrock API key (AWS_BEARER_TOKEN_BEDROCK). The service then runs this relay on the loopback, and
mote gets the relay's address as ANTHROPIC_BASE_URL and a token of this process as ANTHROPIC_API_KEY, which mote keeps
out of its shell commands. The Bedrock key stays in the service, so neither mote nor its commands get it.

Bedrock takes the body of the Messages API without `model` and `stream`, with `anthropic_version`, at the model in
the URL: an inference profile such as global.anthropic.claude-opus-5-5. A model name without a dot, as mote's config
has it, gets the global profile. Bedrock streams an AWS event stream whose chunks carry the events of the Messages
API, base64-encoded, and the relay writes them out as server-sent events again. An error of Bedrock becomes an error
of the Messages API with the status that mote retries (429, 5xx) or does not.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import secrets
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger(__name__)

KEY = 'AWS_BEARER_TOKEN_BEDROCK'
REGION = 'BEDROCK_REGION'
VERSION = 'bedrock-2023-05-31'
BASE = 'https://bedrock-runtime.{region}.amazonaws.com'
PROFILE = 'global.anthropic.'
TIMEOUT = 600
MAX_BODY = 64 * 1024 * 1024
# Bedrock's exceptions as the status and the type of an error of the Messages API.
ERRORS = {
    'validationexception': (400, 'invalid_request_error'), 'accessdeniedexception': (403, 'permission_error'),
    'unrecognizedclientexception': (401, 'authentication_error'), 'resourcenotfoundexception': (404, 'not_found_error'),
    'throttlingexception': (429, 'rate_limit_error'), 'servicequotaexceededexception': (429, 'rate_limit_error'),
    'modelnotreadyexception': (529, 'overloaded_error'), 'serviceunavailableexception': (529, 'overloaded_error'),
    'modeltimeoutexception': (500, 'api_error'), 'internalserverexception': (500, 'api_error'),
    'modelerrorexception': (500, 'api_error'), 'modelstreamerrorexception': (500, 'api_error'),
}
TYPES = {400: 'invalid_request_error', 401: 'authentication_error', 403: 'permission_error', 404: 'not_found_error',
         413: 'request_too_large', 429: 'rate_limit_error', 529: 'overloaded_error'}
# The sizes of the values of the header types of an event stream, after the type byte. 6 and 7 carry a length.
SIZES = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}


def model_id(name: str) -> str:
    """The Bedrock model of a model name: a name with a dot as it is, else the global inference profile."""
    return name if '.' in name else PROFILE + name


def error_body(status: int, kind: str | None, message: str) -> dict:
    return {'type': 'error', 'error': {'type': kind or TYPES.get(status, 'api_error'), 'message': message}}


def exception(name: str, status: int = 500) -> tuple[int, str]:
    """The status and the type of a Bedrock exception, by its name (`ValidationException:http://...` too)."""
    return ERRORS.get(name.split(':')[0].strip().lower(), (status, TYPES.get(status, 'api_error')))


def _exact(stream, size: int) -> bytes:
    data = b''
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            break
        data += part
    return data


def frames(stream):
    """The messages of an AWS event stream: (headers, payload). The checksums go unchecked: TLS protects the bytes."""
    while True:
        prelude = _exact(stream, 12)
        if not prelude:
            return
        if len(prelude) < 12:
            raise OSError('The event stream of Bedrock broke off.')
        total, length, _ = struct.unpack('>III', prelude)
        rest = _exact(stream, total - 12)
        if len(rest) < total - 12 or length > total - 16:
            raise OSError('The event stream of Bedrock broke off.')
        raw, payload, headers, index = rest[:length], rest[length:total - 16], {}, 0
        while index < length:
            size = raw[index]
            name = raw[index + 1:index + 1 + size].decode()
            kind = raw[index + 1 + size]
            index += 2 + size
            if kind in (6, 7):
                count = struct.unpack('>H', raw[index:index + 2])[0]
                value = raw[index + 2:index + 2 + count]
                headers[name] = value.decode(errors='replace') if kind == 7 else value
                index += 2 + count
            elif kind in SIZES:
                index += SIZES[kind]
            else:
                raise OSError(f'The event stream of Bedrock has a header of the unknown type {kind}.')
        yield headers, payload


class Relay:
    """The relay on 127.0.0.1 for one Bedrock key and region. `url` and `token` are for mote."""

    def __init__(self, key: str, region: str = 'us-east-1', base: str | None = None):
        self.key, self.region = key, region
        self.base = (base or BASE.format(region=region)).rstrip('/')
        self.token = secrets.token_urlsafe(32)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True, name='bedrock relay').start()

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'coconut-bedrock-relay'

            def log_message(self, *_):
                pass

            def do_POST(self):
                relay.handle(self)

        return Handler

    def authorized(self, request) -> bool:
        given = request.headers.get('x-api-key') or (request.headers.get('authorization') or '').removeprefix('Bearer ')
        return hmac.compare_digest(given.strip().encode(), self.token.encode())

    def handle(self, request) -> None:
        if not self.authorized(request):
            return self._json(request, 401, error_body(401, None, 'This relay takes only the token of its service.'))
        path = urllib.parse.urlsplit(request.path).path
        if path.rstrip('/') != '/v1/messages':
            return self._json(request, 404, error_body(404, None, f'The relay to Bedrock has no route {path}.'))
        size = int(request.headers.get('content-length') or 0)
        if size <= 0 or size > MAX_BODY:
            return self._json(request, 413, error_body(413, None, 'Send one Messages request of at most 64 MB.'))
        try:
            body = json.loads(request.rfile.read(size))
            name = body.pop('model')
            stream = bool(body.pop('stream', False))
        except (ValueError, KeyError, AttributeError, TypeError):
            return self._json(request, 400, error_body(400, None, 'Send a Messages request with its model.'))
        body['anthropic_version'] = VERSION
        betas = [beta.strip() for beta in (request.headers.get('anthropic-beta') or '').split(',') if beta.strip()]
        if betas:
            body['anthropic_beta'] = betas
        action = 'invoke-with-response-stream' if stream else 'invoke'
        url = f'{self.base}/model/{urllib.parse.quote(model_id(str(name)), safe="")}/{action}'
        upstream = urllib.request.Request(url, json.dumps(body).encode(), method='POST', headers={
            'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json', 'User-Agent': 'coconut-mote',
            'Accept': 'application/vnd.amazon.eventstream' if stream else 'application/json'})
        try:
            response = urllib.request.urlopen(upstream, timeout=TIMEOUT)
        except urllib.error.HTTPError as error:
            with error:
                raw = error.read()
            try:
                found = json.loads(raw or b'{}')
                message = found.get('message') or found.get('Message') or raw.decode(errors='replace')
            except ValueError:
                message = raw.decode(errors='replace').strip() or f'HTTP {error.code}'
            status, kind = exception(error.headers.get('x-amzn-ErrorType') or '', error.code)
            LOG.warning('Bedrock refused a call of mote (%s %s): %s', status, kind, message[:300])
            return self._json(request, status, error_body(status, kind, f'Bedrock: {message}'))
        except (OSError, ValueError) as error:
            LOG.warning('Bedrock did not answer a call of mote (%s).', type(error).__name__)
            return self._json(request, 502, error_body(500, 'api_error', f'Bedrock did not answer ({error}).'))
        with response:
            if not stream:
                return self._json(request, 200, json.loads(response.read() or b'{}'))
            request.send_response(200)
            request.send_header('Content-Type', 'text/event-stream')
            request.send_header('Cache-Control', 'no-cache')
            request.end_headers()
            try:
                for headers, payload in frames(response):
                    if headers.get(':message-type') == 'event':
                        event = json.loads(base64.b64decode(json.loads(payload)['bytes']))
                        name = event.get('type', 'message')
                        request.wfile.write(f'event: {name}\ndata: {json.dumps(event)}\n\n'.encode())
                    else:
                        try:
                            message = json.loads(payload).get('message') or payload.decode(errors='replace')
                        except ValueError:
                            message = payload.decode(errors='replace')
                        status, kind = exception(str(headers.get(':exception-type') or headers.get(':error-code') or ''))
                        LOG.warning('Bedrock ended a stream of mote (%s): %s', kind, str(message)[:300])
                        data = json.dumps(error_body(status, kind, f'Bedrock: {message}'))
                        request.wfile.write(f'event: error\ndata: {data}\n\n'.encode())
                        break
                    request.wfile.flush()
            except (OSError, ValueError, KeyError) as error:
                # The stream breaks off here, and mote reports the cut-off reply.
                LOG.warning('A stream of Bedrock to mote broke off (%s).', type(error).__name__)

    @staticmethod
    def _json(request, status: int, value: dict) -> None:
        data = json.dumps(value).encode()
        request.send_response(status)
        request.send_header('Content-Type', 'application/json')
        request.send_header('Content-Length', str(len(data)))
        request.end_headers()
        request.wfile.write(data)
