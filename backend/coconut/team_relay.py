"""The relay routes of a team room (docs/team-log.md, "Relay routes"), with the Python standard library.

The relay sees only sealed bytes, IDs, times and sizes. A member token or a ticket goes into the Authorization header,
never into a path, and no error of this module carries one. The client follows no redirect, so a token never goes
to another host.

The socket of the contract is not here: the Python standard library has no WebSocket client, so the watch mode of the
helper holds it (team_socket.py).
"""
from __future__ import annotations

import http.client
import json
import re
import time
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 15
# One answer of GET .../entries holds at most 256 entries and 1 MiB of bodies, as base64 in JSON.
MAX_ANSWER = 4 * 1024 * 1024
HEX64 = re.compile(r'[a-f0-9]{64}')
HEX32 = re.compile(r'[a-f0-9]{32}')
BASE64 = re.compile(r'(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?')
HOST = re.compile(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')
# The largest whole number of the contract. JavaScript keeps each number up to this one exact, and SQLite stores it.
# A larger number from the relay or in a payload makes the binding of SQLite fail.
MAX_NUMBER = 2 ** 53 - 1


def number(value, low: int = 0) -> bool:
    """True when a value of JSON is a whole number from `low` to MAX_NUMBER. A bool is no number here."""
    return type(value) is int and low <= value <= MAX_NUMBER


class TeamRelayError(Exception):
    """The relay did not give the answer that the caller needs. `status` is the HTTP status, or 0 without one."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status

    @property
    def temporary(self) -> bool:
        """True when the same request can work later: no answer, too many requests, or a failure of the relay."""
        return self.status in (0, 429) or self.status >= 500


def relay_origin(value) -> str:
    """Check the address of a relay: an HTTPS origin with a host name, without a path or account details.

    This is the rule of an invite link. The loopback relay of a test is the one exception, as in a debug build of
    the app: a release helper refuses to put it into a link. The result has the host in lowercase, because the rule
    of the link (ios/Tunnel/TeamLog.swift) accepts a host with capital letters, and one relay has one address.
    """
    try:
        url = urlsplit(value)
        port = url.port
    except (ValueError, AttributeError, TypeError):
        url = port = None
    if url is not None and not (url.path or url.query or url.fragment or url.username or url.password) and url.hostname:
        loopback = url.scheme == 'http' and url.hostname == '127.0.0.1' and port
        named = url.scheme == 'https' and HOST.fullmatch(url.hostname) and re.search(r'[a-z]', url.hostname.rsplit('.', 1)[1])
        origin = f'{url.scheme}://{url.hostname}' + (f':{port}' if port else '')
        # urlsplit makes the scheme and the host lowercase. The link rule takes only a lowercase scheme.
        if (loopback or named) and value.startswith(url.scheme + '://') and value.lower() == origin:
            return origin
    raise ValueError('Use the HTTPS address of a relay, without a path.')


class TeamRelay:
    def __init__(self, timeout: float = TIMEOUT_SECONDS):
        self.timeout = timeout

    def request(self, relay: str, method: str, path: str, *, authorization: str | None = None, body: dict | None = None,
                tries: int = 1) -> tuple[int, dict]:
        """Send one request. Return the status and the JSON object of the answer, or {} without one.

        Each route of a room gives the same result for a repeat, so a request that got no answer can go again.
        """
        url = urlsplit(relay_origin(relay))
        headers = {'Accept': 'application/json'}
        if authorization:
            headers['Authorization'] = authorization
        data = None
        if body is not None:
            data = json.dumps(body, separators=(',', ':')).encode()
            headers['Content-Type'] = 'application/json'
        for attempt in range(tries):
            if attempt:
                time.sleep(.5)
            factory = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
            connection = factory(url.hostname, url.port, timeout=self.timeout)
            try:
                connection.request(method, '/v1/rooms/' + path, data, headers)
                response = connection.getresponse()
                content = response.read(MAX_ANSWER + 1)
                if len(content) > MAX_ANSWER:
                    raise TeamRelayError(0, 'The answer of the relay is too large.')
                try:
                    value = json.loads(content) if content else {}
                except ValueError:
                    value = None
                return response.status, value if isinstance(value, dict) else {}
            except (OSError, http.client.HTTPException):
                # The text of the error could name the address. The address is no secret, but the caller needs none of it.
                pass
            finally:
                connection.close()
        raise TeamRelayError(0, 'The relay did not answer.')

    def _expect(self, answer: tuple[int, dict], *statuses: int) -> dict:
        status, value = answer
        if status not in statuses:
            raise TeamRelayError(status, f'The relay answered {status}.')
        return value

    def create(self, relay: str, room: str, claim: dict, tries: int = 1) -> None:
        """Make the room with this Mac as its admin. A repeat with the same member and token is no error."""
        self._expect(self.request(relay, 'PUT', room, body=claim, tries=tries), 200, 201)

    def add_ticket(self, relay: str, room: str, token: str, ticket_hash: str, ttl: int, uses: int, welcome: str,
                   tries: int = 1) -> None:
        self._expect(self.request(relay, 'POST', f'{room}/tickets', authorization='Bearer ' + token, tries=tries,
                                  body={'hash': ticket_hash, 'ttl': ttl, 'uses': uses, 'welcome': welcome}), 204)

    def remove_ticket(self, relay: str, room: str, token: str, ticket_hash: str, tries: int = 1) -> None:
        """Remove a ticket and its welcome. Only the admin can do this. A ticket that the relay no longer has is no
        error."""
        self._expect(self.request(relay, 'DELETE', f'{room}/tickets/{ticket_hash}', authorization='Bearer ' + token,
                                  tries=tries), 204)

    def join(self, relay: str, room: str, ticket: str, claim: dict, tries: int = 1) -> str:
        """Add this Mac as a member with a ticket. Return the sealed welcome as base64."""
        value = self._expect(self.request(relay, 'POST', f'{room}/members', authorization='Ticket ' + ticket,
                                          body=claim, tries=tries), 200)
        welcome = value.get('welcome')
        if not isinstance(welcome, str) or not welcome or not BASE64.fullmatch(welcome):
            raise TeamRelayError(0, 'The relay gave no welcome.')
        return welcome

    def members(self, relay: str, room: str, token: str) -> list[dict]:
        """The members that the relay knows: member, admin, joined."""
        value = self._expect(self.request(relay, 'GET', f'{room}/members', authorization='Bearer ' + token), 200)
        members = value.get('members')
        if not isinstance(members, list) or not all(
                isinstance(row, dict) and isinstance(row.get('member'), str) and HEX64.fullmatch(row['member'])
                for row in members):
            raise TeamRelayError(0, 'The relay gave no list of members.')
        return members

    def remove_member(self, relay: str, room: str, token: str, member: str, tries: int = 1) -> None:
        """Remove a member, or leave as that member. When the admin leaves a room without other members, the room goes."""
        self._expect(self.request(relay, 'DELETE', f'{room}/members/{member}', authorization='Bearer ' + token,
                                  tries=tries), 204)

    def write(self, relay: str, room: str, token: str, entry: str, kept: bool, body: str, tries: int = 1) -> dict:
        """Write an entry, or replace the entry of this ID. Return `seq` and `first`."""
        value = self._expect(self.request(relay, 'POST', f'{room}/entries', authorization='Bearer ' + token, tries=tries,
                                          body={'id': entry, 'kept': kept, 'body': body}), 200)
        seq, first = value.get('seq'), value.get('first')
        if not number(first, 1) or not number(seq, first):
            raise TeamRelayError(0, 'The relay gave no position for the entry.')
        return {'seq': seq, 'first': first}

    def read(self, relay: str, room: str, token: str, after: int) -> dict:
        """The entries with a higher `seq` than `after`, in order, and `last`: the highest `seq` of the room."""
        value = self._expect(self.request(relay, 'GET', f'{room}/entries?after={int(after)}',
                                          authorization='Bearer ' + token), 200)
        entries, last = value.get('entries'), value.get('last')
        position = after
        if not isinstance(entries, list) or not number(last):
            raise TeamRelayError(0, 'The relay gave no entries.')
        for entry in entries:
            if not valid_entry(entry) or entry['seq'] <= position:
                raise TeamRelayError(0, 'The relay gave an entry without the fields of the contract.')
            position = entry['seq']
        return {'entries': entries, 'last': last}

    def destroy(self, relay: str, room: str, token: str) -> None:
        """Delete the room with all its data. Only the admin can do this."""
        self._expect(self.request(relay, 'DELETE', room, authorization='Bearer ' + token), 204)


def valid_entry(entry) -> bool:
    """True when an entry of an answer has the fields of the entry table of the contract."""
    return (isinstance(entry, dict) and number(entry.get('first'), 1) and number(entry.get('seq'), entry['first'])
            and isinstance(entry.get('id'), str) and bool(HEX32.fullmatch(entry['id']))
            and isinstance(entry.get('member'), str) and bool(HEX64.fullmatch(entry['member']))
            and number(entry.get('at')) and type(entry.get('kept')) is bool
            and isinstance(entry.get('body'), str) and bool(entry['body']) and bool(BASE64.fullmatch(entry['body'])))
