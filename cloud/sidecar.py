#!/usr/bin/env python3
"""The Linux end of the relay tunnel: a Python port of scripts/tunnel-sidecar.swift (docs/cloud.md).

It keeps the files of the Swift sidecar in <data>/tunnel, speaks the same relay protocol (docs/relay-transport.md),
and runs the same Noise IK and IKpsk2 (ios/Tunnel/Noise.swift) and frames (ios/Tunnel/TunnelFrames.swift), so the
iPhone app pairs with a Linux host exactly as with a Mac. Each stream of a Noise session is one HTTP request, sent to
the backend's tunnel listener with X-Coconut-Peer set to the phone's static key.

Usage:
  sidecar.py setup --data-dir D [--relay URL]      register a route with the relay, once
  sidecar.py run --data-dir D [--backend 127.0.0.1:8961]
  sidecar.py link --data-dir D --backend-dir B --name NAME   print a one-time pairing link (coconut://pair/...)
  sidecar.py selftest <cacophony vectors file>     check Noise against the official test vectors
Logs hold no tokens, keys, routes or payloads.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

RELAY_URL = 'https://coconut-push.gumroad.workers.dev'
# Cloudflare refuses the default User-Agent of Python's HTTP clients (error 1010), so the sidecar names itself.
USER_AGENT = 'CoconutSidecar/1 (Linux)'
MAX_REQUEST_BODY = 7 * 1024 * 1024
MAX_BUFFERED_BODIES = 32 * 1024 * 1024
MAX_RESPONSE_HEAD = 64 * 1024
MAX_STREAMS = 64
MAX_CHANNELS = 8
MAX_TICKETS = 8
MAX_TICKET_SECONDS = 600
PING_INTERVAL = 20
SILENCE_LIMIT = 45
MAX_BACKOFF = 60
MAX_PAYLOAD = 60 * 1024
INITIAL_WINDOW = 1024 * 1024
DROPPED = {'host', 'connection', 'content-length', 'transfer-encoding', 'keep-alive', 'upgrade', 'x-coconut-peer',
           'proxy-connection', 'te', 'trailer'}


def log(message):
    print(f'{datetime.now(timezone.utc).isoformat(timespec="seconds")} {message}', flush=True)


# MARK: Noise (https://noiseprotocol.org/noise.html), as ios/Tunnel/Noise.swift


class NoiseError(Exception):
    pass


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hmac256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def noise_hkdf(chaining_key: bytes, material: bytes, outputs: int) -> list[bytes]:
    temporary = hmac256(chaining_key, material)
    results, previous = [], b''
    for index in range(1, outputs + 1):
        previous = hmac256(temporary, previous + bytes([index]))
        results.append(previous)
    return results


def derive(material: bytes, label: str) -> bytes:
    """HKDF-SHA256 (RFC 5869) with an empty salt and one 32-byte block. The iPhone and pairing.py use the same labels."""
    key = hmac256(bytes(32), material)
    return hmac256(key, label.encode() + b'\x01')


def public(private: X25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def dh(private: X25519PrivateKey, public_key: bytes) -> bytes:
    try:
        output = private.exchange(X25519PublicKey.from_public_bytes(public_key))
    except ValueError as error:  # cryptography refuses an all-zero result of a low-order key
        raise NoiseError('invalid key') from error
    if not any(output):
        raise NoiseError('invalid key')
    return output


class Cipher:
    MAX_NONCE = 2 ** 64 - 1

    def __init__(self, key: bytes | None = None):
        self.key, self.nonce = key, 0

    @staticmethod
    def _nonce(value: int) -> bytes:
        return bytes(4) + value.to_bytes(8, 'little')

    def encrypt(self, plaintext: bytes, ad: bytes = b'') -> bytes:
        if self.key is None:
            return plaintext
        if self.nonce >= self.MAX_NONCE:
            raise NoiseError('nonce exhausted')
        output = ChaCha20Poly1305(self.key).encrypt(self._nonce(self.nonce), plaintext, ad)
        self.nonce += 1
        return output

    def decrypt(self, ciphertext: bytes, ad: bytes = b'') -> bytes:
        if self.key is None:
            return ciphertext
        if self.nonce >= self.MAX_NONCE or len(ciphertext) < 16:
            raise NoiseError('decryption failed')
        try:
            output = ChaCha20Poly1305(self.key).decrypt(self._nonce(self.nonce), ciphertext, ad)
        except Exception as error:
            raise NoiseError('decryption failed') from error
        self.nonce += 1
        return output

    def rekey(self):
        # Noise section 11.3. The nonce is unchanged.
        if self.key is not None:
            self.key = ChaCha20Poly1305(self.key).encrypt(self._nonce(self.MAX_NONCE), bytes(32), b'')[:32]


class Symmetric:
    def __init__(self, name: bytes):
        self.hash = name.ljust(32, b'\0') if len(name) <= 32 else sha256(name)
        self.chaining_key = self.hash
        self.cipher = Cipher()

    def mix_hash(self, data: bytes):
        self.hash = sha256(self.hash + data)

    def mix_key(self, material: bytes):
        self.chaining_key, key = noise_hkdf(self.chaining_key, material, 2)
        self.cipher = Cipher(key)

    def mix_key_and_hash(self, material: bytes):
        self.chaining_key, temporary, key = noise_hkdf(self.chaining_key, material, 3)
        self.mix_hash(temporary)
        self.cipher = Cipher(key)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.cipher.encrypt(plaintext, self.hash)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.cipher.decrypt(ciphertext, self.hash)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self):
        first, second = noise_hkdf(self.chaining_key, b'', 2)
        return Cipher(first), Cipher(second)


class Handshake:
    """IK, or IKpsk2 with a pre-shared key. The iPhone is the initiator; the host answers."""

    def __init__(self, initiator: bool, local_static: X25519PrivateKey, remote_static: bytes | None = None,
                 psk: bytes | None = None, prologue: bytes = b'', ephemeral: X25519PrivateKey | None = None):
        if psk is not None and len(psk) != 32:
            raise NoiseError('invalid key')
        if initiator and (remote_static is None or len(remote_static) != 32):
            raise NoiseError('invalid key')
        self.initiator, self.local_static, self.remote_static, self.psk = initiator, local_static, remote_static, psk
        self.ephemeral, self.remote_ephemeral, self.step = ephemeral, None, 0
        name = b'Noise_IKpsk2_25519_ChaChaPoly_SHA256' if psk else b'Noise_IK_25519_ChaChaPoly_SHA256'
        self.state = Symmetric(name)
        self.state.mix_hash(prologue)
        # IK pre-message: <- s
        self.state.mix_hash(remote_static if initiator else public(local_static))

    def _write_ephemeral(self) -> bytes:
        self.ephemeral = self.ephemeral or X25519PrivateKey.generate()
        key = public(self.ephemeral)
        self.state.mix_hash(key)
        if self.psk is not None:
            self.state.mix_key(key)
        return key

    def _read_ephemeral(self, message: bytes):
        if len(message) < 32:
            raise NoiseError('invalid message')
        self.remote_ephemeral = message[:32]
        self.state.mix_hash(self.remote_ephemeral)
        if self.psk is not None:
            self.state.mix_key(self.remote_ephemeral)

    def write(self, payload: bytes = b'') -> bytes:
        if self.initiator and self.step == 0:
            message = self._write_ephemeral()
            self.state.mix_key(dh(self.ephemeral, self.remote_static))
            message += self.state.encrypt_and_hash(public(self.local_static))
            self.state.mix_key(dh(self.local_static, self.remote_static))
        elif not self.initiator and self.step == 1:
            message = self._write_ephemeral()
            self.state.mix_key(dh(self.ephemeral, self.remote_ephemeral))
            self.state.mix_key(dh(self.ephemeral, self.remote_static))
            if self.psk is not None:
                self.state.mix_key_and_hash(self.psk)
        else:
            raise NoiseError('wrong state')
        message += self.state.encrypt_and_hash(payload)
        if len(message) > 65535:
            raise NoiseError('invalid message')
        self.step += 1
        return message

    def read(self, message: bytes) -> bytes:
        if len(message) > 65535:
            raise NoiseError('invalid message')
        if not self.initiator and self.step == 0:
            self._read_ephemeral(message)
            self.state.mix_key(dh(self.local_static, self.remote_ephemeral))
            if len(message) < 80:
                raise NoiseError('invalid message')
            self.remote_static = self.state.decrypt_and_hash(message[32:80])
            self.state.mix_key(dh(self.local_static, self.remote_static))
            rest = message[80:]
        elif self.initiator and self.step == 1:
            self._read_ephemeral(message)
            self.state.mix_key(dh(self.ephemeral, self.remote_ephemeral))
            self.state.mix_key(dh(self.local_static, self.remote_ephemeral))
            if self.psk is not None:
                self.state.mix_key_and_hash(self.psk)
            rest = message[32:]
        else:
            raise NoiseError('wrong state')
        payload = self.state.decrypt_and_hash(rest)
        self.step += 1
        return payload

    def transport(self):
        if self.step != 2:
            raise NoiseError('wrong state')
        first, second = self.state.split()
        send, receive = (first, second) if self.initiator else (second, first)
        return Session(send, receive, self.state.hash, self.remote_static)


# MARK: Frames, as ios/Tunnel/TunnelFrames.swift

REQ_HEAD, BODY, END, RESP_HEAD, CANCEL, PING, PONG, REKEY, WINDOW, ERROR = range(1, 11)


def frame(kind: int, stream: int, payload: bytes = b'', flags: int = 0) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise NoiseError('frame too large')
    return bytes([kind]) + stream.to_bytes(4, 'big') + bytes([flags]) + payload


def decode(data: bytes):
    if len(data) < 6 or len(data) - 6 > MAX_PAYLOAD or not 1 <= data[0] <= 10:
        raise NoiseError('invalid frame')
    return data[0], int.from_bytes(data[1:5], 'big'), data[5], data[6:]


class Session:
    REKEY_MESSAGES = 1 << 16
    REKEY_SECONDS = 30 * 60

    def __init__(self, send: Cipher, receive: Cipher, handshake_hash: bytes, remote_static: bytes):
        self.send, self.receive = send, receive
        self.handshake_hash, self.remote_static = handshake_hash, remote_static
        self.sent, self.last_rekey = 0, time.monotonic()

    def seal(self, data: bytes) -> list[bytes]:
        output = []
        if self.sent >= self.REKEY_MESSAGES or time.monotonic() - self.last_rekey >= self.REKEY_SECONDS:
            output.append(self._rekey())
        output.append(self._one(data))
        return output

    def _rekey(self) -> bytes:
        ciphertext = self._one(frame(REKEY, 0))
        self.send.rekey()
        self.sent, self.last_rekey = 0, time.monotonic()
        return ciphertext

    def _one(self, data: bytes) -> bytes:
        ciphertext = self.send.encrypt(data)
        self.sent += 1
        return ciphertext

    def open(self, ciphertext: bytes):
        kind, stream, flags, payload = decode(self.receive.decrypt(ciphertext))
        if kind == REKEY:
            if stream or flags or payload:
                raise NoiseError('invalid rekey')
            self.receive.rekey()
            return None
        return kind, stream, flags, payload


# MARK: Files in <data>/tunnel


def write_private(path: Path, data: bytes):
    temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}')
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as handle:
        handle.write(data)
    os.replace(temporary, path)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


class Store:
    def __init__(self, data_dir: Path):
        self.directory = Path(data_dir) / 'tunnel'
        self.tickets = self.directory / 'tickets'
        for folder in (self.directory, self.tickets):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(folder, 0o700)

    def identity(self) -> X25519PrivateKey:
        path = self.directory / 'identity.key'
        if path.exists():
            return X25519PrivateKey.from_private_bytes(path.read_bytes())
        key = X25519PrivateKey.generate()
        write_private(path, key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                              serialization.NoEncryption()))
        return key

    def route(self):
        try:
            return json.loads((self.directory / 'route.json').read_text())
        except (OSError, ValueError):
            return None

    def peers(self) -> list:
        try:
            return json.loads((self.directory / 'peers.json').read_text())
        except (OSError, ValueError):
            return []

    def save_peers(self, peers):
        write_private(self.directory / 'peers.json', json.dumps(peers).encode())

    def valid_tickets(self) -> dict:
        result, now = {}, time.time()
        for path in self.tickets.glob('*.json'):
            name = path.stem
            try:
                ticket = json.loads(path.read_text())
                valid = (len(name) == 64 and all(c in '0123456789abcdef' for c in name)
                         and len(bytes.fromhex(ticket['psk'])) == 32 and ticket['expires'] > now)
            except (OSError, ValueError, KeyError, TypeError):
                valid = False
            if valid:
                result[name] = ticket
            else:
                path.unlink(missing_ok=True)
        return result


def register(store: Store, relay: str) -> dict:
    """Register a new route with the relay. The relay answers 409 for a route that exists, so pick another."""
    key = store.identity()
    for _ in range(3):
        route = secrets.token_hex(32)
        request = urllib.request.Request(relay.rstrip('/') + '/v1/tunnels', data=json.dumps({'route': route}).encode(),
                                         headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                token = json.load(response)['host_token']
        except urllib.error.HTTPError as error:
            if error.code == 409:
                continue
            raise SystemExit(f'The relay did not register the tunnel (HTTP {error.code}).')
        value = dict(relay=relay, route=route, host_token=token, mac_key=b64url(public(key)))
        write_private(store.directory / 'route.json', json.dumps(value).encode())
        return value
    raise SystemExit('The relay refused every new route.')


# MARK: The sidecar


class Stream:
    def __init__(self, identity: int, method: str, path: str, headers: dict):
        self.id, self.method, self.path, self.headers = identity, method, path, headers
        self.body = bytearray()
        self.unacknowledged = 0
        self.window = INITIAL_WINDOW
        self.window_open = asyncio.Event()
        self.window_open.set()
        self.refused = False
        self.task: asyncio.Task | None = None


class Channel:
    def __init__(self, identity: int, ticket: bool, token_hash: str):
        self.id, self.ticket, self.token_hash = identity, ticket, token_hash
        self.session: Session | None = None
        self.paired = not ticket
        self.psk: bytes | None = None
        self.streams: dict[int, Stream] = {}
        self.closed = False


def is_token(text: str) -> bool:
    return bool(text) and all(c.isascii() and (c.isalnum() or c in "!#$%&'*+-.^_`|~") for c in text)


def is_field_value(text: str) -> bool:
    return all(c in ' \t' or 0x21 <= ord(c) <= 0x7E for c in text)


class Sidecar:
    def __init__(self, store: Store, route: dict, backend: str):
        self.store, self.route = store, route
        self.identity = store.identity()
        self.peers = store.peers()
        host, port = backend.rsplit(':', 1)
        self.backend = (host, int(port))
        self.channels: dict[int, Channel] = {}
        self.sent_tickets: set[str] = set()
        self.outgoing: asyncio.Queue | None = None
        self.state = 'starting'
        self.last_received = time.monotonic()
        self.connections = 0

    # The relay socket

    def send(self, data):
        if self.outgoing is not None:
            self.outgoing.put_nowait(data)

    def control(self, value: dict):
        self.send(bytes(4) + json.dumps(value, separators=(',', ':')).encode())

    def seal(self, channel: Channel, data: bytes):
        """Encrypt and queue in one synchronous step, so the ciphertexts leave in nonce order."""
        if channel.closed or channel.session is None:
            return
        try:
            for ciphertext in channel.session.seal(data):
                self.send(channel.id.to_bytes(4, 'big') + ciphertext)
        except NoiseError as error:
            self.close(channel, 'seal_failed', error)

    async def run(self):
        from websockets.asyncio.client import connect
        relay = self.route['relay']
        scheme = 'ws://' if relay.startswith('http://') else 'wss://'
        base = relay.split('://', 1)[1].strip('/')
        url = f'{scheme}{base}/v1/tunnels/{self.route["route"]}/host'
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                async with connect(url, additional_headers=[('Authorization', f'Bearer {self.route["host_token"]}')],
                                   max_size=128 * 1024, ping_interval=None, open_timeout=20,
                                   user_agent_header=USER_AGENT) as socket:
                    self.connections += 1
                    log('Connected to the relay.')
                    self.set_state('connected')
                    await self.serve(socket)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # network, TLS, a refused upgrade or a closed socket: connect again
                log(f'The relay socket ended ({type(error).__name__}).')
            self.set_state('offline')
            for channel in list(self.channels.values()):
                self.drop(channel)
            self.channels.clear()
            self.outgoing = None
            # The end of a healthy socket, for example a reset by Cloudflare, gets a new attempt at once.
            if time.monotonic() - started > 60:
                backoff = 1.0
                delay = 0.2
            else:
                delay = backoff * random.uniform(0.8, 1.2)
                backoff = min(backoff * 2, MAX_BACKOFF)
            log(f'Retrying in {delay:.1f} seconds.')
            await asyncio.sleep(delay)

    async def serve(self, socket):
        self.outgoing = asyncio.Queue()
        self.sent_tickets = set()
        self.last_received = time.monotonic()
        outgoing = self.outgoing

        async def writer():
            while True:
                await socket.send(await outgoing.get())

        async def ticker():
            while True:
                await asyncio.sleep(2)
                silent = time.monotonic() - self.last_received
                if silent > SILENCE_LIMIT:
                    log('The relay sent nothing for 45 seconds.')
                    await socket.close()
                    return
                if silent > PING_INTERVAL:
                    # The relay answers `ping` with `pong` without waking its Durable Object.
                    self.send('ping')
                self.send_tickets()
                self.write_status()

        tasks = [asyncio.create_task(writer()), asyncio.create_task(ticker())]
        self.send_tickets()
        try:
            async for message in socket:
                self.last_received = time.monotonic()
                if isinstance(message, bytes):
                    self.handle_relay(message)
        finally:
            for task in tasks:
                task.cancel()

    def set_state(self, state):
        self.state = state
        self.write_status()

    def write_status(self):
        status = dict(state=self.state, relay=self.route['relay'], channels=len(self.channels),
                      reconnects=max(0, self.connections - 1), updated=datetime.now(timezone.utc).isoformat(),
                      pid=os.getpid())
        try:
            write_private(self.store.directory / 'status.json', json.dumps(status).encode())
        except OSError:
            pass

    # Pairing tickets

    def send_tickets(self):
        if self.state != 'connected':
            return
        now = time.time()
        tickets = self.store.valid_tickets()
        self.sent_tickets &= set(tickets)
        for name, ticket in sorted(tickets.items(), key=lambda item: item[1]['expires']):
            if name in self.sent_tickets or len(self.sent_tickets) >= MAX_TICKETS:
                continue
            ttl = int(ticket['expires'] - now)
            if ttl >= 1:
                self.control({'type': 'add_ticket', 'hash': name, 'ttl': min(ttl, MAX_TICKET_SECONDS)})
                self.sent_tickets.add(name)
                log('Sent a pairing ticket to the relay.')

    # Channels and Noise

    def handle_relay(self, data: bytes):
        if len(data) < 4:
            return
        identity, payload = int.from_bytes(data[:4], 'big'), data[4:]
        if identity == 0:
            return self.handle_control(payload)
        channel = self.channels.get(identity)
        if channel is None:
            return self.control({'type': 'close_channel', 'id': identity})
        try:
            if channel.session is None:
                self.handshake(channel, payload)
            else:
                opened = channel.session.open(payload)
                if opened is not None:
                    self.handle_frame(channel, *opened)
        except (NoiseError, ValueError, KeyError, TypeError) as error:
            self.close(channel, 'handshake_failed' if channel.session is None else 'session_error', error)

    def handle_control(self, payload: bytes):
        try:
            value = json.loads(payload)
        except ValueError:
            return
        kind = value.get('type')
        if kind == 'open':
            identity, auth, token_hash = value.get('id'), value.get('auth'), value.get('hash')
            if not isinstance(identity, int) or identity == 0 or not isinstance(token_hash, str):
                return
            channel = Channel(identity, auth == 'ticket', token_hash)
            if channel.ticket:
                # The relay removed the ticket at this connection, so the link is spent and its slot is free.
                path = self.store.tickets / f'{token_hash}.json'
                try:
                    ticket = json.loads(path.read_text())
                    if ticket['expires'] > time.time():
                        channel.psk = bytes.fromhex(ticket['psk'])
                except (OSError, ValueError, KeyError):
                    pass
                path.unlink(missing_ok=True)
                self.sent_tickets.discard(token_hash)
                self.send_tickets()
            if len(self.channels) >= MAX_CHANNELS:
                return self.control({'type': 'close_channel', 'id': identity})
            self.channels[identity] = channel
            log(f'Channel {identity} opened ({"ticket" if channel.ticket else "token"}).')
        elif kind == 'closed':
            channel = self.channels.pop(value.get('id'), None)
            if channel:
                self.drop(channel)
                log(f'Channel {channel.id} closed by the relay.')
        elif kind == 'error':
            log(f'The relay refused a control message: {value.get("code", "unknown")}.')

    def handshake(self, channel: Channel, message: bytes):
        """Read message 1 from the phone and answer with message 2. The phone key must belong to the channel's token.
        On a ticket channel, the ticket's pre-shared key proves that the phone has the pairing link."""
        if channel.ticket and channel.psk is None:
            raise NoiseError('no ticket')
        handshake = Handshake(False, self.identity, psk=channel.psk if channel.ticket else None)
        if handshake.read(message):
            raise NoiseError('invalid message')
        peer = handshake.remote_static.hex()
        if not channel.ticket and not any(p['key'] == peer and p['token_hash'] == channel.token_hash for p in self.peers):
            raise NoiseError('unknown phone')
        reply = handshake.write()
        channel.session = handshake.transport()
        self.send(channel.id.to_bytes(4, 'big') + reply)

    def close(self, channel: Channel, reason: str, error: Exception):
        if self.channels.pop(channel.id, None) is None:
            return
        self.drop(channel)
        log(f'Channel {channel.id} closed by this host: {reason} ({error}).')
        self.control({'type': 'close_channel', 'id': channel.id})

    def drop(self, channel: Channel):
        channel.closed = True
        for stream in channel.streams.values():
            if stream.task:
                stream.task.cancel()
        channel.streams.clear()
        channel.session = None

    def handle_frame(self, channel: Channel, kind: int, identity: int, flags: int, payload: bytes):
        if kind == PING:
            self.seal(channel, frame(PONG, identity))
        elif kind in (PONG, ERROR):
            pass
        elif kind == REQ_HEAD:
            head = json.loads(payload)
            method, path, headers = head.get('method'), head.get('path'), head.get('headers') or {}
            # The phone opens odd streams. A control character in a header could add headers or a second request.
            if (identity % 2 != 1 or identity in channel.streams or len(channel.streams) >= MAX_STREAMS
                    or not isinstance(method, str) or not is_token(method) or not isinstance(path, str)
                    or not path.startswith('/') or not all(0x21 <= ord(c) <= 0x7E for c in path)
                    or not isinstance(headers, dict)
                    or not all(isinstance(k, str) and isinstance(v, str) and is_token(k) and is_field_value(v)
                               for k, v in headers.items())):
                raise NoiseError('invalid request head')
            channel.streams[identity] = Stream(identity, method, path, headers)
        elif kind == BODY:
            stream = channel.streams.get(identity)
            if stream is None or stream.task is not None:
                raise NoiseError('body without a stream')
            if stream.refused:
                return
            buffered = sum(len(s.body) for c in self.channels.values() for s in c.streams.values())
            if len(stream.body) + len(payload) > MAX_REQUEST_BODY or buffered + len(payload) > MAX_BUFFERED_BODIES:
                stream.refused = True
                stream.body = bytearray()
                return self.seal(channel, frame(ERROR, identity, json.dumps({'code': 'too_large'}).encode()))
            stream.body += payload
            # The sidecar holds the body in memory, so it returns the window at once in steps of 256 KB.
            stream.unacknowledged += len(payload)
            if stream.unacknowledged >= 256 * 1024:
                self.seal(channel, frame(WINDOW, identity, stream.unacknowledged.to_bytes(4, 'big')))
                stream.unacknowledged = 0
        elif kind == END:
            stream = channel.streams.get(identity)
            if stream is None or stream.task is not None:
                raise NoiseError('end without a stream')
            if stream.refused:
                del channel.streams[identity]
            else:
                stream.task = asyncio.get_running_loop().create_task(self.forward(channel, stream))
        elif kind == CANCEL:
            stream = channel.streams.pop(identity, None)
            if stream and stream.task:
                stream.task.cancel()
        elif kind == WINDOW:
            if len(payload) != 4:
                raise NoiseError('invalid window')
            stream = channel.streams.get(identity)
            if stream:
                stream.window += int.from_bytes(payload, 'big')
                stream.window_open.set()
        else:
            raise NoiseError('unexpected frame')

    # The backend

    def respond(self, channel: Channel, stream: Stream, status: int, value: dict):
        body = json.dumps(value).encode()
        head = {'status': status, 'headers': {'Content-Type': 'application/json; charset=utf-8',
                                              'Content-Length': str(len(body))}}
        self.seal(channel, frame(RESP_HEAD, stream.id, json.dumps(head).encode()))
        self.seal(channel, frame(BODY, stream.id, body))
        self.seal(channel, frame(END, stream.id))
        channel.streams.pop(stream.id, None)

    def fail(self, channel: Channel, stream: Stream):
        self.seal(channel, frame(ERROR, stream.id, json.dumps({'code': 'backend_down'}).encode()))
        channel.streams.pop(stream.id, None)

    def add_client(self, channel: Channel) -> bool:
        """Allow the new phone's client token on the relay, and record its key. The relay allows 32 tokens."""
        session = channel.session
        peer = session.remote_static.hex()
        token = derive(session.handshake_hash, 'coconut relay client').hex()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        kept = sorted((p for p in self.peers if p['key'] != peer), key=lambda p: p['created'])
        removed = [p for p in self.peers if p['key'] == peer]
        while len(kept) >= 31:
            removed.append(kept.pop(0))
        kept.append({'key': peer, 'token_hash': token_hash,
                     'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
        try:
            self.store.save_peers(kept)
        except OSError:
            log('The sidecar could not save the paired phone keys. The pairing failed.')
            return False
        self.peers = kept
        for old in removed:
            self.control({'type': 'remove_client', 'hash': old['token_hash']})
        self.control({'type': 'add_client', 'hash': token_hash})
        log('A phone paired.')
        return True

    async def forward(self, channel: Channel, stream: Stream):
        writer = None
        try:
            pairing = stream.method == 'POST' and stream.path == '/v1/pair'
            if not channel.paired and not pairing and stream.path != '/health':
                # A ticket session exists to pair. Other requests wait for a session with the client token.
                return self.respond(channel, stream, 401, {'error': 'Pair this phone with the Mac to continue.'})
            peer = channel.session.remote_static.hex()
            try:
                reader, writer = await asyncio.open_connection(*self.backend)
            except OSError:
                return self.fail(channel, stream)
            head = f'{stream.method} {stream.path} HTTP/1.1\r\nHost: 127.0.0.1\r\n'
            for name, value in stream.headers.items():
                if name.lower() not in DROPPED:
                    head += f'{name}: {value}\r\n'
            head += f'X-Coconut-Peer: {peer}\r\nContent-Length: {len(stream.body)}\r\nConnection: close\r\n\r\n'
            writer.write(head.encode('latin-1') + bytes(stream.body))
            await writer.drain()
            stream.body = bytearray()
            buffer = b''
            while b'\r\n\r\n' not in buffer:
                chunk = await reader.read(65536)
                if not chunk or len(buffer) + len(chunk) > MAX_RESPONSE_HEAD + 65536:
                    return self.fail(channel, stream)
                buffer += chunk
            raw, rest = buffer.split(b'\r\n\r\n', 1)
            lines = raw.decode('latin-1').split('\r\n')
            try:
                status = int(lines[0].split(' ')[1])
            except (IndexError, ValueError):
                status = 502
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ':' not in line:
                    continue
                name, value = line.split(':', 1)
                name, value = name.strip(), value.strip()
                if name.lower() in ('connection', 'keep-alive', 'transfer-encoding'):
                    continue
                headers[name] = f'{headers[name]}, {value}' if name in headers else value
            length = next((int(v) for k, v in headers.items() if k.lower() == 'content-length' and v.isdigit()), None)
            pending = rest[:length] if length is not None else rest
            remaining = None if length is None else length - len(pending)
            # The phone gets its relay client token from this session. The relay must know the token before the
            # phone reads the answer and reconnects with it, so the control message goes first on the same socket.
            if not channel.paired and pairing and status == 200:
                if not self.add_client(channel):
                    return self.fail(channel, stream)
                channel.paired = True
            self.seal(channel, frame(RESP_HEAD, stream.id, json.dumps({'status': status, 'headers': headers}).encode()))
            while True:
                while pending:
                    while stream.window <= 0:
                        stream.window_open.clear()
                        await stream.window_open.wait()
                    count = min(len(pending), stream.window, MAX_PAYLOAD)
                    self.seal(channel, frame(BODY, stream.id, pending[:count]))
                    pending, stream.window = pending[count:], stream.window - count
                if remaining == 0:
                    break
                chunk = await reader.read(256 * 1024)
                if not chunk:
                    if remaining is None:
                        break
                    # The backend ended before the whole body. The phone must not take a partial body as complete.
                    return self.fail(channel, stream)
                if remaining is not None:
                    chunk = chunk[:remaining]
                    remaining -= len(chunk)
                pending = chunk
            self.seal(channel, frame(END, stream.id))
            channel.streams.pop(stream.id, None)
        except asyncio.CancelledError:
            pass
        except (OSError, NoiseError) as error:
            log(f'A stream failed ({type(error).__name__}).')
            self.fail(channel, stream)
        finally:
            if writer is not None:
                writer.close()


# MARK: Pairing links, as scripts/pairing.py


def pairing_link(data_dir: Path, backend_dir: Path, name: str) -> str:
    store = Store(data_dir)
    route = store.route()
    if route is None:
        raise SystemExit('Run setup first.')
    result = subprocess.run([sys.executable, '-m', 'coconut', '--data-dir', str(data_dir), '--pair', '--tunnel'],
                            cwd=backend_dir, check=True, capture_output=True, text=True)
    secret, expires = result.stdout.split()
    expires = int(expires)
    raw = base64.urlsafe_b64decode(secret + '=' * (-len(secret) % 4))
    ticket_hash = hashlib.sha256(derive(raw, 'coconut pairing ticket').hex().encode()).hexdigest()
    psk = derive(raw, 'coconut pairing psk').hex()
    write_private(store.tickets / f'{ticket_hash}.json', json.dumps({'psk': psk, 'expires': expires}).encode())
    relay, label = route['relay'].encode(), name.strip()[:80].encode()
    mac_key = base64.urlsafe_b64decode(route['mac_key'] + '=' * (-len(route['mac_key']) % 4))
    packed = (bytes([3]) + bytes.fromhex(route['route']) + mac_key + raw + expires.to_bytes(4, 'big')
              + len(relay).to_bytes(2, 'big') + relay + label)
    return 'coconut://pair/' + base64.b32encode(packed).decode().rstrip('=').lower()


# MARK: Test vectors


def selftest(path: str):
    """Run the IK and IKpsk2 vectors of cacophony for 25519, ChaChaPoly and SHA-256 in both roles."""
    vectors = json.loads(Path(path).read_text())['vectors']
    names = ('Noise_IK_25519_ChaChaPoly_SHA256', 'Noise_IKpsk2_25519_ChaChaPoly_SHA256')
    checked = 0
    for vector in vectors:
        if vector.get('protocol_name') not in names:
            continue
        key = lambda field: X25519PrivateKey.from_private_bytes(bytes.fromhex(vector[field]))
        psks = vector.get('init_psks') or []
        psk = bytes.fromhex(psks[0]) if psks else None
        initiator = Handshake(True, key('init_static'), bytes.fromhex(vector['init_remote_static']), psk,
                              bytes.fromhex(vector['init_prologue']), key('init_ephemeral'))
        responder = Handshake(False, key('resp_static'), None, psk, bytes.fromhex(vector['resp_prologue']),
                              key('resp_ephemeral'))
        messages = vector['messages']
        first, second = messages[0], messages[1]
        assert initiator.write(bytes.fromhex(first['payload'])).hex() == first['ciphertext'], 'message 1'
        assert responder.read(bytes.fromhex(first['ciphertext'])).hex() == first['payload'], 'read 1'
        assert responder.write(bytes.fromhex(second['payload'])).hex() == second['ciphertext'], 'message 2'
        assert initiator.read(bytes.fromhex(second['ciphertext'])).hex() == second['payload'], 'read 2'
        sessions = initiator.transport(), responder.transport()
        if vector.get('handshake_hash'):
            assert sessions[0].handshake_hash.hex() == vector['handshake_hash'] == sessions[1].handshake_hash.hex()
        for index, message in enumerate(messages[2:]):
            sender, receiver = (sessions[0], sessions[1]) if index % 2 == 0 else (sessions[1], sessions[0])
            assert sender.send.encrypt(bytes.fromhex(message['payload'])).hex() == message['ciphertext'], 'transport'
            assert receiver.receive.decrypt(bytes.fromhex(message['ciphertext'])).hex() == message['payload']
        checked += 1
    # Rekey keeps both sides in step.
    a, b = Cipher(bytes(range(32))), Cipher(bytes(range(32)))
    a.rekey(), b.rekey()
    assert b.decrypt(a.encrypt(b'rekeyed')) == b'rekeyed'
    print(f'{checked} Noise vectors passed in both roles, and rekey agrees.')
    if not checked:
        raise SystemExit('No vector matched.')


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    command, options = argv[0], dict(zip(argv[1::2], argv[2::2]))
    if command == 'selftest':
        return selftest(argv[1])
    os.umask(0o077)
    data_dir = Path(options['--data-dir'])
    store = Store(data_dir)
    if command == 'setup':
        relay = options.get('--relay', RELAY_URL)
        existing = store.route()
        route = existing if existing and existing['relay'] == relay else register(store, relay)
        print(json.dumps({'route': route['route'][:8] + '…', 'mac_key': route['mac_key'], 'relay': route['relay']}))
    elif command == 'run':
        route = store.route()
        if route is None:
            raise SystemExit('Run setup first.')
        sidecar = Sidecar(store, route, options.get('--backend', '127.0.0.1:8961'))
        asyncio.run(sidecar.run())
    elif command == 'link':
        print(pairing_link(data_dir, Path(options['--backend-dir']), options.get('--name', 'Coconut Cloud')))
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
