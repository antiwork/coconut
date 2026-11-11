"""Team rooms on this Mac: make a room, invite, join, leave and list (docs/team-log.md, "Mac").

A team is a set of Macs that share one sealed log on the relay. This module holds the steps of the contract for one
Mac. team_sync.py copies the log, team_socket.py learns of new entries through the socket of each room, team_store.py
keeps the log, team_keys.py keeps the secrets of each room, team_relay.py talks to the relay, and team_helper.py does
the cryptography.

The routes are for the Team screen of the app and for the tools on this Mac (server.py). A paired device calls them
on the tunnel listener or on the main listener, with the authorization of its other routes. A tool calls them on the
main listener with the token of <data>/gateway/bridge.json, as scripts/team.py does. One handler serves both. On a
person's Mac the token reaches only the routes of tool_route, and each other route answers 403. The error texts are
for a person, because the app shows them.

- `GET /v1/team/rooms`: the rooms with their members, their bots and their sync state, and the bots of this Mac.
- `POST /v1/team/rooms` with `{name, member_name, avatar, color, relay}`: make a room. This Mac is its admin.
- `PATCH /v1/team/rooms/<room>` with `{member_name, avatar, color}`: change what the team sees for this Mac.
- `POST /v1/team/rooms/<room>/invites` with `{ttl, uses}`: make an invite link. Only the admin can do this.
- `POST /v1/team/join` with `{link, member_name, avatar, color}`: join a room.
- `DELETE /v1/team/rooms/<room>`: leave the room, and delete its copy and its keys on this Mac.
- `POST /v1/team/sync`: read each room from the relay now, and return the rooms.
- `PUT /v1/team/rooms/<room>/pages/<document>`: the person shares a page of this Mac with the room (team_pages.py).
- `DELETE /v1/team/rooms/<room>/pages/<document>`: the person stops sharing the page with the room.
- `POST /v1/team/rooms/<room>/bots` with `{bot_id}`: share a bot with the room, only on a Mac that runs Coconut for a
  team only. A person's Mac answers 409, because its bots stay the person's.
- `DELETE /v1/team/rooms/<room>/bots/<bot_id>`: stop sharing the bot with the room, with the same rule.
- The routes of the Team chat under `/v1/team/rooms/<room>/messages` and `/v1/team/rooms/<room>/read`
  (team_messages.py).

No answer has a key, a token or an invite secret. Only the invite link, which the person shares, carries a secret.

A write of a paired device with an `Idempotency-Key` is safe to repeat (team_request). A leave of a `joining` room
always removes the room from this Mac (team_leave).
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import hmac
import json
import logging
import re
from secrets import token_bytes, token_hex
import threading
import time
import unicodedata
from uuid import UUID

from . import team_keys
from .push_relay import RELAY_URL
from .team_helper import TeamFormatError, TeamHelper, TeamHelperError
from .team_keys import ROOM_ID, TeamKeysError
from .team_messages import TeamMessages
from .team_pages import TeamPages
from .team_relay import TeamRelay, TeamRelayError, relay_origin
from .team_socket import TeamSocket
from .team_store import ACTIVE, JOINING, MEMBER_LIMITS, SCHEMA, TeamStore, unicode_text
from .team_sync import INACTIVE, ROOM_ERRORS, TEAM_EVENT, TeamSync, member_payload, room_error, utc_now

__all__ = ['SCHEMA', 'Team']

# The name of this Mac's member when the caller gives none.
MEMBER_NAME = 'Member'
MAX_NAME = 80
# The format characters that the name of a person can have: the zero width joiner of an emoji sequence, and
# the tag characters of a flag such as the flag of Scotland. Other format characters, for example the marks that
# change the direction of text, can hide what a name says.
EMOJI_FORMAT = re.compile('[\u200d\U000e0020-\U000e007f]')
# The limits of the relay for a ticket (docs/team-log.md, "Limits").
INVITE_SECONDS = (60, 7 * 86400)
INVITE_USES = (1, 64)
DEFAULT_INVITE_SECONDS = 86400
# `POST /v1/team/sync` waits this long for the pass that it started.
SYNC_WAIT_SECONDS = 60
# The app repeats a write one time, at once, after a lost or slow answer (APIClient.swift). A repeat later than this
# runs again. The limit keeps the memory small when a device sends many keys.
REPLY_SECONDS = 600
REPLY_LIMIT = 256
# The answer on a person's Mac to a team route that only a paired device can call (tool_route).
PERSON_ONLY = 'Only the person can do this in the team. Use the Coconut app.'
# The clock of the saved answers. A test replaces it, so it needs no wait of 10 minutes.
clock = time.monotonic

log = logging.getLogger(__name__)


def checked_name(value, noun: str, *, person: bool) -> str:
    """Check a name, with one text for each problem. `noun` names the name in the text, for example "your name".

    The name of a team follows the rule of a link (docs/team-log.md, "Invite and join"). The helper counts the
    characters that a person sees, trims white space and line breaks, and refuses each control and format character
    (`validName` in ios/Tunnel/TeamLog.swift). This rule counts code points, which are never fewer, and refuses each
    category that starts with "C" and each line break, so it is never wider than the rule of the helper.

    The name of a person (`person`) goes only into a sealed `member` entry, so no rule of the helper applies. It can
    have an emoji with a joiner. It has no control character, no line break, no lone surrogate, no format character
    outside EMOJI_FORMAT, and no space at its start or end. A name with a character that this Python does not know
    yet, for example a new emoji, is valid.
    """
    from .service_base import ServiceError
    if not isinstance(value, str) or not value:
        raise ServiceError(400, f'Give {noun}.')
    if len(value) > MAX_NAME:
        raise ServiceError(400, f'Use at most {MAX_NAME} characters for {noun}.')
    if not unicode_text(value):
        raise ServiceError(400, f'Use only valid text for {noun}.')
    for character in value:
        category = unicodedata.category(character)
        if category in ('Zl', 'Zp') or character in '\n\r\x0b\x0c\x1c\x1d\x1e\x85':
            raise ServiceError(400, f'Put {noun} on one line.')
        if category == 'Cc' or (category.startswith('C') and not person) or (
                category == 'Cf' and not EMOJI_FORMAT.fullmatch(character)):
            raise ServiceError(400, f'Remove the hidden characters from {noun}.')
    if value != value.strip():
        raise ServiceError(400, f'Remove the space at the start or end of {noun}.')
    return value


def member_profile(data: dict, current: dict | None = None, *, field: str = 'member_name',
                   noun: str = 'your name') -> dict:
    """What this Mac writes about itself in its `member` entry: name, avatar and color. `current` is the profile of
    the room for a change: a field that the caller leaves out keeps its value.

    The bot of a team-only Mac (team_only.py) has the same three fields with the same rules: `field` names the name
    in the data, and `noun` names it in the text of an error."""
    from .service_base import ServiceError
    current = current or {'name': MEMBER_NAME, 'avatar': '', 'color': ''}
    profile = {'name': checked_name(data.get(field, current['name']), noun, person=True)}
    for field in ('avatar', 'color'):
        value = data.get(field, current[field])
        if not unicode_text(value) or len(value) > MEMBER_LIMITS[field]:
            raise ServiceError(400, f'Use text of at most {MEMBER_LIMITS[field]} characters for the {field}.')
        profile[field] = value
    return profile


def tool_route(method: str, parts: list[str]) -> bool:
    """True for a team route that a caller without a paired device can call on a person's Mac: what the `coconut`
    tool needs there. `parts` are the parts of the route after `/v1/team`, as _team_route reads them.

    Only the person acts for a person's Mac in a team, through the app. The token of the bridge file is for the gateway
    adapter and the tools of this Mac, and a bot can read that file. So the token gets the list of the teams, a join, a
    leave and a sync, and no other route. A route that a later change adds stays closed until it is in this list. The
    rule closes the plain path only: a process of the same macOS account can still read other files of this Mac."""
    return (method, parts) in (('GET', ['rooms']), ('POST', ['join']), ('POST', ['sync'])) or (
        method == 'DELETE' and len(parts) == 2 and parts[0] == 'rooms')


def whole(data: dict, field: str, default: int, limits: tuple[int, int], text: str) -> int:
    from .service_base import ServiceError
    value = data.get(field, default)
    if type(value) is not int or not limits[0] <= value <= limits[1]:
        raise ServiceError(400, text)
    return value


@contextlib.contextmanager
def team_errors():
    """Give the caller of a route a failure of the helper, the key file or the relay, as a text for a person."""
    from .service_base import ServiceError
    try:
        yield
    except ROOM_ERRORS as error:
        if isinstance(error, TeamHelperError):
            status = 503
        elif isinstance(error, TeamFormatError):
            status = 500
        elif isinstance(error, TeamKeysError):
            status = 409
        elif error.temporary:
            status = 503
        else:
            status = 409 if error.status in (401, 404) else 502
        raise ServiceError(status, room_error(error)) from None


class Team(TeamSync, TeamSocket, TeamPages, TeamMessages):
    """The team rooms of this Mac: the routes of the tools, and the thread that copies each log."""

    def _init_team(self):
        self.team_store = TeamStore(self.db)
        self.team_relay = TeamRelay()
        self.team_helper = TeamHelper(self.root)
        # One room operation runs at a time. It calls the relay and the helper, so it does not hold the service lock.
        self._team_operation = threading.Lock()
        # The answers of the writes of the devices, for a repeat with the same key (team_request). They stay in memory
        # only, because the answer of the invite route has the invite link. The Mac holds this lock only for a change
        # of the dict, and takes no other lock while it holds it.
        self._team_replies = {}
        self._team_replies_lock = threading.Lock()
        self._init_team_sync()
        self._init_team_socket()
        self._init_team_pages()

    def _start_team(self):
        self._start_team_sync()
        self._start_team_socket()

    def _close_team(self):
        self._close_team_socket()
        self._close_team_sync()

    def team_request(self, method: str, path: str, data: dict, key: str = '', device: str = '') -> dict:
        """One of the routes at the top of this module. server.py checked the token of the device or of the bridge.

        A write calls the relay and can take seconds. When the answer does not reach the phone, the app sends the same
        request again with the same `Idempotency-Key`. That repeat must not make a second room, or answer "already in
        this team" after a join that worked. So the first successful answer for a key of a device stays in memory for
        REPLY_SECONDS, and a repeat gets it and does nothing else. A repeat that comes while the first request runs
        waits for it. A failure keeps no answer, because the person tries again. A request without a key, for example
        of a tool on this Mac, runs as before. `POST /v1/team/sync` and the reads need no key.
        """
        from .service_base import ServiceError
        if not key or method == 'GET' or path.rstrip('/') == '/v1/team/sync':
            return self._team_route(method, path, data, device=device)
        try:
            UUID(key)
        except (ValueError, TypeError, AttributeError):
            raise ServiceError(400, 'Supply a UUID in Idempotency-Key.') from None
        # The digest keeps no part of a link in a join, only its hash.
        fingerprint = hashlib.sha256(json.dumps([method, path, data], sort_keys=True).encode()).hexdigest()
        slot = (device, key)
        while True:
            with self._team_replies_lock:
                self._team_prune_replies()
                reply = self._team_replies.get(slot)
                if reply is None:
                    reply = {'fingerprint': fingerprint, 'at': clock(), 'answer': None, 'done': threading.Event()}
                    self._team_replies[slot] = reply
                    break
                if not hmac.compare_digest(reply['fingerprint'], fingerprint):
                    raise ServiceError(409, 'This idempotency key belongs to a different request.')
                if reply['done'].is_set():
                    return copy.deepcopy(reply['answer'])
            # The first request runs. When it fails, it removes its slot, and this request runs itself.
            reply['done'].wait()
        try:
            answer = self._team_route(method, path, data, f'{device}:{key}', device)
        except BaseException:
            with self._team_replies_lock:
                self._team_replies.pop(slot, None)
            reply['done'].set()
            raise
        with self._team_replies_lock:
            reply['answer'], reply['at'], reply['stored'] = copy.deepcopy(answer), clock(), True
            # The limit holds after each write, not only at the next one.
            self._team_prune_replies()
        reply['done'].set()
        return answer

    def team_drop_old_replies(self) -> None:
        """Drop the saved answers past their time. The sync thread calls this on each pass, also on a Mac without a
        room, so an answer with an invite link goes when no other write comes."""
        with self._team_replies_lock:
            self._team_prune_replies()

    def _team_prune_replies(self) -> None:
        """Drop the answers older than REPLY_SECONDS, then the oldest answers over REPLY_LIMIT. A request that runs
        keeps its slot. The caller holds the lock of the replies."""
        done = sorted((reply['at'], slot) for slot, reply in self._team_replies.items() if reply.get('stored'))
        old = clock() - REPLY_SECONDS
        over = len(done) - REPLY_LIMIT
        for number, (at, slot) in enumerate(done):
            if at < old or number < over:
                del self._team_replies[slot]

    def _team_route(self, method: str, path: str, data: dict, repeat: str = '', device: str = '') -> dict:
        """Serve one route. `repeat` names the device and the `Idempotency-Key` of a write, or is empty. `device` is
        the paired device of the request, or empty for a tool of this Mac."""
        from .service_base import ServiceError
        parts = path.strip('/').split('/')[2:]
        # The check uses the parts that choose the route, so no form of the path passes around it. A Mac for a team
        # only has no person, so there the token keeps each route.
        if not device and self.library_reader and not tool_route(method, parts):
            raise ServiceError(403, PERSON_ONLY)
        if parts == ['rooms'] and method == 'GET':
            return self.team_rooms()
        if parts == ['rooms'] and method == 'POST':
            return self.team_create(data)
        if parts == ['join'] and method == 'POST':
            return self.team_join(data)
        if parts == ['sync'] and method == 'POST':
            return self.team_sync_now()
        if len(parts) == 2 and parts[0] == 'rooms' and method == 'DELETE':
            return self.team_leave(parts[1])
        if len(parts) == 2 and parts[0] == 'rooms' and method == 'PATCH':
            return self.team_profile(parts[1], data)
        if len(parts) == 3 and parts[0] == 'rooms' and parts[2] == 'invites' and method == 'POST':
            return self.team_invite(parts[1], data)
        if len(parts) == 4 and parts[0] == 'rooms' and parts[2] == 'pages' and method in ('PUT', 'DELETE'):
            if method == 'PUT':
                return self.team_share_page(parts[1], parts[3])
            return self.team_stop_page(parts[1], parts[3])
        if len(parts) == 3 and parts[0] == 'rooms' and parts[2] == 'bots' and method == 'POST':
            return self.team_share(parts[1], data)
        if len(parts) == 4 and parts[0] == 'rooms' and parts[2] == 'bots' and method == 'DELETE':
            return self.team_unshare(parts[1], parts[3])
        if len(parts) == 3 and parts[0] == 'rooms' and parts[2] == 'messages' and method == 'GET':
            return self.team_messages(parts[1], data)
        if len(parts) == 3 and parts[0] == 'rooms' and parts[2] == 'messages' and method == 'POST':
            return self.team_send(parts[1], data, repeat)
        if len(parts) == 4 and parts[0] == 'rooms' and parts[2] == 'messages' and method == 'PATCH':
            return self.team_edit(parts[1], parts[3], data)
        if len(parts) == 4 and parts[0] == 'rooms' and parts[2] == 'messages' and method == 'DELETE':
            return self.team_unsend(parts[1], parts[3])
        if len(parts) == 3 and parts[0] == 'rooms' and parts[2] == 'read' and method == 'POST':
            return self.team_mark_read(parts[1], data)
        raise ServiceError(404, 'This route was not found.')

    # Reads

    def team_rooms(self) -> dict:
        """The rooms, `helper`, and `mac_bots`: the bots of this Mac that a room can take, so a tool can find a bot by
        its name (`coconut team add-bot`). An archived or deleted bot is not in that list."""
        with self.lock:
            rooms = [self._team_view(room) for room in self.team_store.rooms()]
            bots = sorted(({'id': bot['id'], 'name': bot['name']} for bot in self._all('bots')
                           if not bot.get('archived') and not bot.get('_deleted')),
                          key=lambda bot: (bot['name'].casefold(), bot['id']))
        return {'rooms': rooms, 'helper': self.team_helper.available, 'mac_bots': bots}

    def _team_view(self, room: dict) -> dict:
        """What the Team screen and a tool read about a room, with no key. The caller holds the service lock."""
        entries, waiting = self.team_store.counts(room['room'])
        status = self._team_status.get(room['room'], {})
        refused = [{name: value for name, value in refusal.items() if name != 'form'}
                   for refusal in self._team_refusals.get(room['room'], {}).values()]
        members = [dict(member=member['member'], name=member['name'], avatar=member['avatar'], color=member['color'],
                        admin=member['member'] == room['admin'], this_mac=member['member'] == room['member'])
                   for member in self.team_store.members(room['room'])]
        is_admin = room['member'] == room['admin']
        return {'room': room['room'], 'name': room['name'], 'relay': room['relay'], 'state': room['state'],
                'member': room['member'], 'admin': room['admin'], 'is_admin': is_admin,
                'can_invite': is_admin and room['state'] == ACTIVE, 'epoch': room['epoch'],
                'created_at': room['created_at'], 'members': members, 'bots': self._team_bots(room, members),
                **self._team_chat_summary(room),
                'sync': {'seq': room['seq'], 'entries': entries, 'waiting': waiting,
                         'synced_at': status.get('synced_at'), 'error': status.get('error'), 'refused': refused,
                         'socket': self.team_socket_open(room['room'])}}

    def _team_room(self, room_id) -> dict:
        from .service_base import ServiceError
        if not isinstance(room_id, str) or not ROOM_ID.fullmatch(room_id):
            raise ServiceError(404, 'This Mac is not in this team.')
        with self.lock:
            room = self.team_store.room(room_id)
        if room is None:
            raise ServiceError(404, 'This Mac is not in this team.')
        return room

    def _team_answer(self, room_id: str) -> dict:
        with self.lock:
            return {'room': self._team_view(self.team_store.room(room_id))}

    # Operations

    def team_create(self, data: dict) -> dict:
        """Make a room on the relay. This Mac is its admin, and it writes its `member` entry."""
        from .service_base import ServiceError
        name = checked_name(data.get('name'), 'the name of the team', person=False)
        profile = member_profile(data)
        try:
            relay = relay_origin(data.get('relay') or RELAY_URL)
        except ValueError:
            raise ServiceError(400, 'Use the HTTPS address of a Coconut server, without a path.') from None
        with self._team_operation, team_errors():
            made = self.team_helper.member()
            room_id = token_hex(32)
            try:
                self.team_relay.create(relay, room_id, self._team_claim(room_id, made), tries=2)
            except TeamRelayError as error:
                if error.status == 429:
                    raise ServiceError(429, 'This network made too many teams. Try again in an hour.') from None
                raise
            # The room exists now. A stop before the next lines leaves a room that no Mac can use: nobody else has
            # its ID or the key of its admin, and the relay deletes it after its idle time.
            team_keys.write(self.root, room_id, member_key=made['member_key'], agreement_key=made['agreement_key'],
                            token=made['token'], keys={0: base64.b64encode(token_bytes(32)).decode()})
            with self.transaction():
                self.team_store.add_room(
                    room=room_id, relay=relay, name=name, member=made['member'], admin=made['member'], state=ACTIVE,
                    epoch=0, profile=json.dumps(dict(profile, entry=token_hex(16), agreement=made['agreement'])),
                    created_at=utc_now())
                self._event(TEAM_EVENT, {'team': room_id})
        self._team_first_write(room_id)
        return self._team_answer(room_id)

    def team_invite(self, room_id: str, data: dict) -> dict:
        """Make an invite: a ticket and a sealed welcome on the relay, and the link that the person shares."""
        from .service_base import ServiceError
        room = self._team_room(room_id)
        if room['state'] != ACTIVE:
            raise ServiceError(409, INACTIVE[room['state']])
        if room['admin'] != room['member']:
            raise ServiceError(403, 'Only the admin of the team can invite a person.')
        ttl = whole(data, 'ttl', DEFAULT_INVITE_SECONDS, INVITE_SECONDS,
                    'An invite link lasts from 1 minute to 7 days. Choose a time in that range.')
        uses = whole(data, 'uses', 1, INVITE_USES, 'An invite link admits 1 to 64 Macs. Choose a number in that range.')
        with self._team_operation, team_errors():
            secrets = team_keys.read(self.root, room_id)
            key = secrets['keys'].get(str(room['epoch']))
            if key is None:
                raise TeamKeysError('This Mac has no room key for its epoch.')
            secret = base64.b64encode(token_bytes(32)).decode()
            expires = int(time.time()) + ttl
            ticket = self.team_helper.ticket(secret)
            welcome = self.team_helper.seal_welcome(room_id, secret, room['epoch'], key, room['admin'], room['name'])
            try:
                # The link comes before the ticket, so a link that the helper refuses leaves no ticket on the relay.
                link = self.team_helper.link(room_id, secret, expires, room['relay'], room['name'])
            except TeamFormatError:
                raise ServiceError(409, 'This Mac cannot make an invite link for this team. Update Coconut, then try '
                                        'again.') from None
            try:
                self.team_relay.add_ticket(room['relay'], room_id, secrets['token'], ticket['hash'], ttl, uses, welcome, tries=2)
            except TeamRelayError as error:
                if error.status == 409:
                    raise ServiceError(409, 'The team has 8 open invite links. Wait until one ends, then try again.') from None
                raise
        return {'link': link, 'expires': expires, 'uses': uses}

    def team_join(self, data: dict) -> dict:
        """Join a room with an invite link: show the ticket and the proof, open the welcome, and write the `member`
        entry of this Mac. The sync then reads the log from its start.

        The Mac saves its keys before it asks the relay. When the relay does not answer, the room stays `joining`,
        and a join with a link of the same room and relay asks again with the same member and token, as the
        contract allows.
        """
        from .service_base import ServiceError
        link = data.get('link')
        if not isinstance(link, str) or not 0 < len(link) <= 4096:
            raise ServiceError(400, 'Paste the invite link of the team.')
        profile = member_profile(data)
        with self._team_operation, team_errors():
            try:
                invite = self.team_helper.invite(link.strip())
                relay = relay_origin(invite['relay'])
            except (TeamFormatError, ValueError) as error:
                if getattr(error, 'code', '') == 'unknown_version':
                    raise ServiceError(400, 'A newer Coconut made this invite link. Update Coconut, then join again.') from None
                raise ServiceError(400, 'This is not the invite link of a team. Check the link, then try again.') from None
            if invite['expires'] <= time.time():
                raise ServiceError(410, 'The invite link is past its end. Ask for a new link.')
            room_id = invite['room']
            with self.lock:
                room = self.team_store.room(room_id)
            if room is not None and room['state'] != JOINING:
                raise ServiceError(409, 'This Mac is already in this team.')
            if room is None:
                made = self.team_helper.member()
                team_keys.write(self.root, room_id, member_key=made['member_key'], agreement_key=made['agreement_key'],
                                token=made['token'], keys={})
                with self.transaction():
                    self.team_store.add_room(
                        room=room_id, relay=relay, name=invite['name'], member=made['member'], admin='', state=JOINING,
                        epoch=0, profile=json.dumps(dict(profile, entry=token_hex(16), agreement=made['agreement'])),
                        created_at=utc_now())
                    self._event(TEAM_EVENT, {'team': room_id})
            elif room['relay'] != relay:
                # The relay of the earlier join can have taken the join, so the member token of this Mac is valid
                # there. The token goes to no other relay.
                raise ServiceError(409, 'This Mac already tries to join this team with a link of another server. '
                                        'Leave the team on this Mac, then join again.')
            else:
                # An earlier join got no answer. This one asks the same relay again, with the keys of that join.
                with self.transaction():
                    self.team_store.update_room(room_id, name=invite['name'])
            with self.lock:
                room = self.team_store.room(room_id)
            secrets = team_keys.read(self.root, room_id)
            claim = self._team_claim(room_id, dict(secrets, member=room['member']))
            try:
                welcome = self.team_relay.join(relay, room_id, self.team_helper.ticket(invite['secret'])['ticket'], claim, tries=2)
            except TeamRelayError as error:
                if error.temporary:
                    raise ServiceError(503, 'Coconut could not reach its server. Join again with the same link.') from None
                # The relay refused this Mac, so its keys are of no use.
                self._team_forget(room_id)
                reasons = {401: 'The invite link is used, past its end or removed. Ask for a new link.',
                           404: 'This team no longer exists.', 409: 'The team is full. It has 64 members.'}
                raise ServiceError(409, reasons.get(error.status, 'The Coconut server refused the join. Ask for a new '
                                                                  'link.')) from None
            try:
                opened = self.team_helper.open_welcome(room_id, invite['secret'], welcome)
            except TeamFormatError as error:
                # A Mac without the room key cannot read or write the log, so it leaves the room (the contract).
                self._team_drop_membership(room, secrets)
                if error.code == 'unknown_version':
                    raise ServiceError(409, 'A newer Coconut made this team. Update Coconut, then join again.') from None
                raise ServiceError(409, 'This Mac could not open the invite, so it did not join the team. Ask for a '
                                        'new link.') from None
            team_keys.write(self.root, room_id, member_key=secrets['member_key'], agreement_key=secrets['agreement_key'],
                            token=secrets['token'], keys={opened['epoch']: opened['key']})
            with self.transaction():
                # The admin sealed this name. The name of the link is only what the app shows before the join.
                self.team_store.update_room(
                    room_id, state=ACTIVE, admin=opened['admin'], name=opened['name'], epoch=opened['epoch'],
                    profile=json.dumps(dict(json.loads(room['profile']), **profile)))
                self._event(TEAM_EVENT, {'team': room_id})
        self._team_first_write(room_id)
        return self._team_answer(room_id)

    def team_leave(self, room_id: str) -> dict:
        """Leave a room. The Mac tells the other members, leaves at the relay, and deletes its copy and its keys.

        A failure of the relay stops the leave of an `active` room, because the Mac is still a member there. A
        `joining` room always goes: the Mac asks the relay, as for an `active` room, then deletes its copy and its keys.

        The relay deletes the kept entries of a member that leaves, but the other Macs already have a copy of its
        `member` entry. So the Mac first replaces that entry with a `delete` entry, when its helper works (see
        _team_retire_member). An admin leaves only a room without other members, and the relay then deletes the room.
        """
        from .service_base import ServiceError
        self._team_room(room_id)
        with self._team_operation, team_errors():
            # An operation before this one can have changed the room, for example a join that made it `active`.
            room = self._team_room(room_id)
            try:
                secrets = team_keys.read(self.root, room_id)
            except TeamKeysError:
                # Without its token, the Mac cannot tell the relay. Only its own copy remains to delete.
                secrets = None
            # The lock of the room holds from the `delete` entry to the end of the leave. Without it, the sync could
            # write the `member` entry again in the gap, the relay would delete that kept entry with the member, and
            # no `delete` entry would stay for the other Macs. When the relay does not answer the leave, the lock
            # goes, the room stays `active` and the Mac is still a member, so a later `member` entry is correct.
            with self._team_room_lock(room_id):
                if secrets is not None and room['state'] in (ACTIVE, JOINING):
                    try:
                        if room['state'] == ACTIVE and room['admin'] != room['member']:
                            self._team_retire_member(room)
                        self.team_relay.remove_member(room['relay'], room_id, secrets['token'], room['member'], tries=2)
                    except TeamRelayError as error:
                        if room['state'] == JOINING:
                            # The relay never answered the join, and it can never answer, for example when the link
                            # named a server that does not exist. The room must not stay on the Team screen for ever.
                            # A relay that took the join keeps this Mac as a member without a Mac, until the member
                            # removal of the contract exists.
                            log.warning('coconut.team_log room %s: the relay did not take the leave of a join that did '
                                        'not finish, so this Mac removes only its copy. %s', room_id[:8], error)
                        elif error.status == 409:
                            raise ServiceError(409, 'The admin can leave the team only when no other member is in it.') from None
                        elif error.status not in (401, 404):
                            raise
                        # Else the relay no longer knows this Mac or the room, so the Mac is out already.
                self._team_forget(room_id)
        return {'room': room_id, 'left': True}

    def team_profile(self, room_id: str, data: dict) -> dict:
        """Change what the team sees for this Mac: its name, avatar and color. The Mac writes its `member` entry
        again with the next `rev`, as its first write does, and a failure of the relay is an error for the caller.

        The saved profile follows each accepted `member` entry of this Mac (TeamStore._follow_profile), so it is what
        the log has for this Mac. An earlier write without an answer can be on the relay with other values, so the
        Mac first reads the room back (TeamSync._team_settle). Only then does it take the fields that the caller
        leaves out from the profile. The lock of the room holds from that read to the end of the write, so the sync
        cannot write between them. A failure changes nothing on this Mac, and the person tries again.
        """
        from .service_base import ServiceError
        self._team_room(room_id)
        with self._team_operation, team_errors(), self._team_room_lock(room_id):
            self._team_settle(room_id)
            with self.lock:
                room = self.team_store.room(room_id)
            if room is None:
                raise ServiceError(404, 'This Mac is not in this team.')
            if room['state'] != ACTIVE:
                raise ServiceError(409, INACTIVE[room['state']])
            current = json.loads(room['profile'])
            profile = dict(current, **member_profile(data, current))
            # The accepted write of this Mac saves the profile in the same transaction.
            self._team_write(room, profile['entry'], True, member_payload(profile), tries=2)
        return self._team_answer(room_id)

    def team_sync_now(self) -> dict:
        """Read each room from the relay now, also a room that waits after a failure. Return the rooms."""
        with self._team_passes:
            # A pass that runs now can be past a room already, so the next pass is the one that counts.
            number = self._team_pass_started + 1
            self.team_wake(retry=True)
            if self._team_thread is not None:
                self._team_passes.wait_for(lambda: self._team_pass_ended >= number or self.stopping.is_set(),
                                           timeout=SYNC_WAIT_SECONDS)
            complete = self._team_pass_ended >= number
        return dict(self.team_rooms(), complete=complete)

    # Steps

    def _team_claim(self, room_id: str, keys: dict) -> dict:
        """What a Mac sends to make or join a room: its member ID, the hash of its token, and its proof."""
        proof = self.team_helper.proof(room_id, keys['token'], keys['member_key'])
        return {'member': keys['member'], 'hash': proof['hash'], 'proof': proof['proof']}

    def _team_first_write(self, room_id: str) -> None:
        """Write the `member` entry of this Mac now, and start a pass. When the write fails, the sync writes the
        entry later (TeamSync._team_announce)."""
        try:
            with self._team_room_lock(room_id):
                self._team_announce(room_id, force=True)
        except ROOM_ERRORS as error:
            self._team_status.setdefault(room_id, {})['error'] = room_error(error)
            log.warning('coconut.team_log room %s: the member entry waits for the next pass. %s', room_id[:8], error)
            self._team_tell_sync(room_id)
        self.team_wake()

    def _team_retire_member(self, room: dict) -> None:
        """Replace the `member` entry of this Mac with a `delete` entry, so each other Mac takes it out of its list.

        A member must always be able to leave. Without a working helper or room key, the Mac leaves without the
        `delete` entry, and the other Macs show it until the member removal of the contract exists. The caller holds
        the lock of the room.
        """
        entry_id = json.loads(room['profile'])['entry']
        try:
            # The first `member` entry can be on the relay without an answer, so the Mac reads the room first. A
            # failure of the helper or of the key file in this read also lets the leave go on. A failure of the relay
            # stops the leave, and the person tries again.
            self._team_settle(room['room'])
            with self.lock:
                entry = self.team_store.entry(room['room'], entry_id)
            if entry is None or entry['kind'] != 'member':
                return
            self._team_write(room, entry_id, False, {'kind': 'delete'}, tries=2)
        except (TeamHelperError, TeamFormatError, TeamKeysError) as error:
            log.warning('coconut.team_log room %s: this Mac leaves without a delete entry, so the other Macs can '
                        'still show it. %s', room['room'][:8], error)

    def _team_drop_membership(self, room: dict, secrets: dict) -> None:
        """Leave a room that this Mac joined and cannot read. The keys stay only when the relay does not answer, so
        a later leave can ask again."""
        from .service_base import ServiceError
        try:
            self.team_relay.remove_member(room['relay'], room['room'], secrets['token'], room['member'], tries=2)
        except TeamRelayError as error:
            if error.temporary:
                raise ServiceError(503, 'This Mac could not open the invite, and it could not reach its server to '
                                        'leave the team. Leave the team on this Mac, then try again.') from None
        self._team_forget(room['room'])

    def _team_forget(self, room_id: str) -> None:
        """Delete what this Mac has of a room: the rows of the store first, then the key file."""
        with self.transaction():
            # The bots and the pages of the room go from the Library, and the shared pages of this Mac lose the room,
            # so the phone reads the list again.
            if self.team_store.shows(room_id):
                self._event('document.updated', {'team': room_id})
            self._event(TEAM_EVENT, {'team': room_id})
            self.team_store.remove_room(room_id)
        team_keys.remove(self.root, room_id)
        for state in (self._team_failures, self._team_status, self._team_announced, self._team_locks,
                      self._team_refusals, self._team_told):
            state.pop(room_id, None)
        self._team_retried.discard(room_id)
        self._team_unsure.discard(room_id)
        self._team_socket_forget(room_id)
