"""A relay and a helper in memory for the team tests.

FakeRelay follows relay/src/room.js for the routes that a Mac uses, with the methods of coconut.team_relay.TeamRelay.
FakeHelper has the methods of coconut.team_helper.TeamHelper. It seals nothing: a body is JSON that names its room,
its entry, its epoch, its room key and its signer, so `open` can give each failure of the real helper. The tests of
the real helper and of the real relay are in scripts/test_team_log.py.
"""
import base64
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import time

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
from coconut.team_helper import TeamFormatError, TeamHelperMissing
from coconut.team_relay import TeamRelayError

RELAY = 'https://relay.example'


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def encode(value) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


def strict_base64(text) -> bytes:
    """The bytes of a base64 text that `base64.b64encode` makes, and of no other text. A ValueError else.

    `base64.b64decode` without `validate` drops characters outside the alphabet, and what it does with data after
    the padding differs between Python versions. So the fakes take only the one canonical text of each value, as the
    helper and the relay do, and a test gives the same result on each version.
    """
    if not isinstance(text, str) or not text.isascii():
        raise ValueError('This is not base64 text.')
    data = base64.b64decode(text, validate=True)
    if base64.b64encode(data).decode() != text:
        raise ValueError('This is not the canonical base64 text of its bytes.')
    return data


def decode(text: str):
    """The JSON value of a text that `encode` made. A ValueError for each other text."""
    value = json.loads(strict_base64(text))
    if encode(value) != text:
        raise ValueError('This is not a text that encode made.')
    return value


def public_agreement(agreement_key: str) -> str:
    """The public agreement key of a private one in the fakes: unpadded base64url of 32 bytes, as in a `member` entry."""
    return base64.urlsafe_b64encode(hashlib.sha256(agreement_key.encode()).digest()).decode().rstrip('=')


class FakeHelper:
    """The helper without its cryptography. `version` is the version of the bodies that it seals and can open."""

    def __init__(self, version: int = 1):
        self.available = True
        self.version = version
        self.calls = []

    def _call(self, name: str):
        if not self.available:
            raise TeamHelperMissing('This Mac has no team helper. Run python3 scripts/team.py build.')
        self.calls.append(name)

    def member(self) -> dict:
        self._call('member')
        member = secrets.token_hex(32)
        agreement_key = base64.b64encode(secrets.token_bytes(32)).decode()
        return {'member': member, 'member_key': 'key of ' + member, 'token': secrets.token_hex(32),
                'agreement': public_agreement(agreement_key), 'agreement_key': agreement_key}

    def seal_direct(self, room: str, entry: str, agreement_key: str, member: str, other: str, agreement: str,
                    plaintext: bytes) -> str:
        """A box that names its room, its entry, its writer and its pair: the two public agreement keys and the two
        member IDs. Only the same pair opens it, as with the pair key of the real helper."""
        self._call('seal_direct')
        return base64.urlsafe_b64encode(json.dumps({
            'room': room, 'entry': entry, 'writer': member, 'keys': sorted([public_agreement(agreement_key), agreement]),
            'members': sorted([member, other]), 'plaintext': base64.b64encode(plaintext).decode()}).encode()).decode().rstrip('=')

    def open_direct(self, room: str, agreement_key: str, member: str, boxes: list) -> list:
        self._call('open_direct')
        results = []
        for box in boxes:
            try:
                sealed = json.loads(base64.urlsafe_b64decode(box['box'] + '=' * (-len(box['box']) % 4)))
                plaintext = strict_base64(sealed['plaintext'])
            except (ValueError, TypeError, KeyError):
                results.append({'error': 'malformed'})
                continue
            if (sealed['room'], sealed['entry'], sealed['writer'], sealed['keys'], sealed['members']) != (
                    room, box['entry'], box['writer'], sorted([public_agreement(agreement_key), box['agreement']]),
                    sorted([member, box['other']])):
                results.append({'error': 'seal'})
            else:
                results.append({'plaintext': plaintext})
        return results

    def proof(self, room: str, token: str, member_key: str) -> dict:
        self._call('proof')
        return {'proof': encode({'room': room, 'hash': digest(token), 'signer': member_key.removeprefix('key of ')}),
                'hash': digest(token)}

    def seal(self, room: str, entry: str, epoch: int, key: str, member_key: str, payload: bytes, *, version=None) -> str:
        self._call('seal')
        return encode({'version': version or self.version, 'room': room, 'entry': entry, 'epoch': epoch, 'key': key,
                       'signer': member_key.removeprefix('key of '), 'payload': base64.b64encode(payload).decode()})

    def open(self, room: str, keys: dict, entries: list) -> list:
        self._call('open')
        results = []
        for entry in entries:
            try:
                body = decode(entry['body'])
                payload = strict_base64(body['payload'])
            except (ValueError, TypeError, KeyError):
                results.append({'error': 'malformed'})
                continue
            if body['version'] > self.version:
                results.append({'error': 'unknown_version'})
            elif str(body['epoch']) not in keys:
                results.append({'error': 'unknown_epoch', 'epoch': body['epoch']})
            elif body['key'] != keys[str(body['epoch'])] or body['room'] != room or body['entry'] != entry['id']:
                results.append({'error': 'seal'})
            elif body['signer'] != entry['member']:
                results.append({'error': 'signature'})
            else:
                results.append({'epoch': body['epoch'], 'payload': payload})
        return results

    def ticket(self, secret: str) -> dict:
        self._call('ticket')
        ticket = digest('ticket of ' + secret)
        return {'ticket': ticket, 'hash': digest(ticket)}

    def seal_welcome(self, room: str, secret: str, epoch: int, key: str, admin: str, name: str) -> str:
        self._call('seal_welcome')
        return encode({'room': room, 'secret': secret, 'epoch': epoch, 'key': key, 'admin': admin, 'name': name})

    def open_welcome(self, room: str, secret: str, welcome: str) -> dict:
        self._call('open_welcome')
        try:
            fields = decode(welcome)
        except ValueError:
            raise TeamFormatError('malformed') from None
        if fields.get('version', 1) > 1:
            raise TeamFormatError('unknown_version')
        if fields['room'] != room or fields['secret'] != secret:
            raise TeamFormatError('seal')
        return {name: fields[name] for name in ('epoch', 'key', 'admin', 'name')}

    def link(self, room: str, secret: str, expires: int, relay: str, name: str) -> str:
        self._call('link')
        return 'coconut://team/' + encode({'version': 1, 'room': room, 'secret': secret, 'expires': expires, 'relay': relay,
                                           'name': name})

    def invite(self, link: str) -> dict:
        self._call('invite')
        try:
            fields = decode(link.removeprefix('coconut://team/'))
        except ValueError:
            raise TeamFormatError('malformed') from None
        if not link.startswith('coconut://team/') or not isinstance(fields, dict):
            raise TeamFormatError('malformed')
        if fields.pop('version') > 1:
            raise TeamFormatError('unknown_version')
        return fields


class FakeRelay:
    """The rooms of a relay in memory. `down` makes each request fail without an answer."""

    def __init__(self, page: int = 256):
        self.rooms = {}
        self.page = page
        self.down = False
        self.requests = []
        # A test can check something at the start of each request, for example that no lock is held.
        self.before = None

    def _request(self, name: str, relay: str, room: str):
        self.requests.append((name, relay, room))
        if self.before:
            self.before()
        if self.down:
            raise TeamRelayError(0, 'The relay did not answer.')

    def _member(self, room: str, token: str) -> tuple[dict, str]:
        """The room and the member ID of a token, as the relay finds them."""
        if room not in self.rooms:
            raise TeamRelayError(404, 'The relay answered 404.')
        state = self.rooms[room]
        for member, row in state['members'].items():
            if row['hash'] == digest(token):
                return state, member
        raise TeamRelayError(401, 'The relay answered 401.')

    def _check(self, room: str, claim: dict):
        try:
            proof = decode(claim['proof'])
        except ValueError:
            raise TeamRelayError(401, 'The relay answered 401.') from None
        if proof != {'room': room, 'hash': claim['hash'], 'signer': claim['member']}:
            raise TeamRelayError(401, 'The relay answered 401.')

    def create(self, relay: str, room: str, claim: dict, tries: int = 1) -> None:
        self._request('create', relay, room)
        self._check(room, claim)
        if room in self.rooms:
            admin = next(member for member, row in self.rooms[room]['members'].items() if row['admin'])
            if (admin, self.rooms[room]['members'][admin]['hash']) != (claim['member'], claim['hash']):
                raise TeamRelayError(409, 'The relay answered 409.')
            return
        self.rooms[room] = {'members': {claim['member']: {'hash': claim['hash'], 'admin': True}}, 'tickets': {},
                            'entries': {}, 'seq': 0}

    def add_ticket(self, relay: str, room: str, token: str, ticket_hash: str, ttl: int, uses: int, welcome: str,
                   tries: int = 1) -> None:
        self._request('add_ticket', relay, room)
        state, member = self._member(room, token)
        if not state['members'][member]['admin']:
            raise TeamRelayError(403, 'The relay answered 403.')
        if sum(ticket['uses'] > 0 for ticket in state['tickets'].values()) >= 8:
            raise TeamRelayError(409, 'The relay answered 409.')
        state['tickets'][ticket_hash] = {'uses': uses, 'welcome': welcome, 'ttl': ttl}

    def remove_ticket(self, relay: str, room: str, token: str, ticket_hash: str, tries: int = 1) -> None:
        self._request('remove_ticket', relay, room)
        state, member = self._member(room, token)
        if not state['members'][member]['admin']:
            raise TeamRelayError(403, 'The relay answered 403.')
        state['tickets'].pop(ticket_hash, None)

    def join(self, relay: str, room: str, ticket: str, claim: dict, tries: int = 1) -> str:
        self._request('join', relay, room)
        if room not in self.rooms:
            raise TeamRelayError(404, 'The relay answered 404.')
        self._check(room, claim)
        state = self.rooms[room]
        found = state['tickets'].get(digest(ticket))
        if found is None:
            raise TeamRelayError(401, 'The relay answered 401.')
        existing = state['members'].get(claim['member'])
        if existing:
            if existing['hash'] != claim['hash']:
                raise TeamRelayError(409, 'The relay answered 409.')
            return found['welcome']
        if found['uses'] < 1:
            raise TeamRelayError(401, 'The relay answered 401.')
        state['members'][claim['member']] = {'hash': claim['hash'], 'admin': False}
        found['uses'] -= 1
        return found['welcome']

    def members(self, relay: str, room: str, token: str) -> list:
        self._request('members', relay, room)
        state, _ = self._member(room, token)
        return [{'member': member, 'admin': row['admin'], 'joined': 0} for member, row in state['members'].items()]

    def remove_member(self, relay: str, room: str, token: str, member: str, tries: int = 1) -> None:
        self._request('remove_member', relay, room)
        state, caller = self._member(room, token)
        if not state['members'][caller]['admin'] and caller != member:
            raise TeamRelayError(403, 'The relay answered 403.')
        if member not in state['members']:
            return
        if state['members'][member]['admin']:
            if len(state['members']) > 1:
                raise TeamRelayError(409, 'The relay answered 409.')
            del self.rooms[room]
            return
        del state['members'][member]
        # The relay deletes the kept entries of a member that leaves.
        state['entries'] = {entry: row for entry, row in state['entries'].items() if row['member'] != member or not row['kept']}

    def write(self, relay: str, room: str, token: str, entry: str, kept: bool, body: str, tries: int = 1) -> dict:
        self._request('write', relay, room)
        state, member = self._member(room, token)
        old = state['entries'].get(entry)
        if old and old['member'] != member:
            raise TeamRelayError(409, 'The relay answered 409.')
        state['seq'] += 1
        # The relay keeps only the newest write of an entry ID.
        state['entries'][entry] = {'seq': state['seq'], 'first': old['first'] if old else state['seq'], 'id': entry,
                                   'member': member, 'at': int(time.time() * 1000), 'kept': kept, 'body': body}
        return {'seq': state['seq'], 'first': state['entries'][entry]['first']}

    def read(self, relay: str, room: str, token: str, after: int) -> dict:
        self._request('read', relay, room)
        state, _ = self._member(room, token)
        entries = sorted((dict(row) for row in state['entries'].values() if row['seq'] > after), key=lambda row: row['seq'])
        return {'entries': entries[:self.page], 'last': max((row['seq'] for row in state['entries'].values()), default=0)}


# A paired device of the person. On a person's Mac only a device calls the team routes outside team.tool_route.
DEVICE = 'test-device'


class TeamMacs:
    """The Macs of a team test, for a TestCase. Each Mac is a Service with its own data folder, and the Macs share
    one FakeRelay."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.relay = FakeRelay()
        self.first, self.second, self.third = (self.mac(name) for name in ('first', 'second', 'third'))

    def mac(self, name: str) -> Service:
        root = Path(self.temp.name) / name
        service = Service(root, FixtureProvider(root), start_workers=False)
        # A test can close a Mac itself. Service.close sets `stopping` first.
        self.addCleanup(lambda: service.stopping.is_set() or service.close())
        service.team_relay, service.team_helper = self.relay, FakeHelper()
        return service

    def create(self, service=None, **fields) -> dict:
        body = {'name': 'Example Team', 'member_name': 'Ada', 'relay': RELAY, **fields}
        return (service or self.first).team_request('POST', '/v1/team/rooms', body, '', DEVICE)['room']

    def invite(self, room: dict, service=None, **fields) -> dict:
        return (service or self.first).team_request('POST', f'/v1/team/rooms/{room["room"]}/invites', fields, '', DEVICE)

    def join(self, service, link: str, name: str = 'Grace') -> dict:
        return service.team_request('POST', '/v1/team/join', {'link': link, 'member_name': name})['room']

    def phone(self, service):
        """Call the team routes of a Mac as the app of the person does: as the paired device DEVICE, unless the call
        names another device."""
        return lambda method, path, data, key='', device=DEVICE: service.team_request(method, path, data, key, device)

    def leave(self, service, room: dict) -> dict:
        return service.team_request('DELETE', f'/v1/team/rooms/{room["room"]}', {})

    def rooms(self, service, *, sync: bool = True) -> list[dict]:
        if sync:
            service.team_sync_pass()
        return service.team_request('GET', '/v1/team/rooms', {})['rooms']

    def names(self, service, **options) -> list[str]:
        return [member['name'] for member in self.rooms(service, **options)[0]['members']]

    def share_page(self, service, room: str, bot: dict, key: str = 'weekly-report', **fields) -> dict:
        """Save a page of a bot of a person's Mac and share it with the room, as the person does in the Library. The
        bot is then the author of a page in the room. Return the document of the answer."""
        service.publish_page(bot['id'], dict(key=key, title='Weekly report', format='markdown',
                                             content='3 tasks are open.', **fields))
        document = service._document_of_key(bot['id'], key)
        return service.team_request('PUT', f'/v1/team/rooms/{room}/pages/{document["id"]}', {}, '', DEVICE)['document']

    def stop_page(self, service, room: str, bot: dict, key: str = 'weekly-report') -> dict:
        """Stop sharing the page of the key with the room. Return the document of the answer."""
        document = service._document_of_key(bot['id'], key)
        return service.team_request('DELETE', f'/v1/team/rooms/{room}/pages/{document["id"]}', {}, '', DEVICE)['document']

    def refusal(self, call, *arguments) -> tuple[int, str]:
        with self.assertRaises(ServiceError) as raised:
            call(*arguments)
        return raised.exception.status, str(raised.exception)

    def key_file(self, service, room: str) -> Path:
        return service.root / 'team' / f'{room}.json'
