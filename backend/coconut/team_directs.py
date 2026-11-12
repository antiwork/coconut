"""Direct messages: a private chat between two people of a team (docs/team-log.md, "Direct messages").

A direct message is a `direct` entry of the log. Its text is in a box that only the writer and the member that the
message is for can open: the pair key of the two Macs seals it a second time. The other members store the entry and
show nothing. Each Mac keeps its direct chats in `team_directs` (team_store.py), apart from the Team chat. The routes
are for the app of the person, and only a paired device reaches them on a person's Mac (team.tool_route):

- `GET /v1/team/rooms/<room>/directs/<member>/messages?before=<first>&limit=<n>`: one page of the chat, newest first.
- `POST /v1/team/rooms/<room>/directs/<member>/messages` with `{text, reply_to?}`: send a direct message.
- `PATCH /v1/team/rooms/<room>/directs/<member>/messages/<id>` with `{text}`: change a message of this Mac's person.
- `DELETE /v1/team/rooms/<room>/directs/<member>/messages/<id>`: remove a message of this Mac's person.
- `POST /v1/team/rooms/<room>/directs/<member>/read` with `{first}`: move the read position of the chat.

A Mac for a team only has no person, so it answers 409. The text of a direct message is private, so no log line of
this module has it.
"""
from __future__ import annotations

import hashlib
import json
from secrets import token_hex

from . import team_keys
from .team_messages import GONE_MESSAGE, MAX_PAGE, PAGE, PREVIEW, message_text, query_number
from .team_relay import HEX64, MAX_NUMBER, number
from .team_store import ACTIVE, DIRECTS_EVENT, entry_id

# The answers of a person's Mac when the member of a chat cannot get a direct message now (rule 2 of the contract).
NOT_HERE = 'This person is not in this team.'
SELF = 'You cannot send a direct message to yourself.'
LEFT = 'This person is no longer in the team.'
BOT_ONLY = 'This Mac runs only bots for the team. Write to its bots in the team chat.'
NOT_YET = 'This person cannot get direct messages yet. Ask them to update the Coconut app.'
NOT_ON = 'Direct messages are not on for this Mac yet. Open the Team screen of the Coconut app, then try again.'
# The answer of a Mac for a team only to each route of this module.
TEAM_ONLY = 'This Mac runs Coconut for a team only, so it has no direct messages.'


def member_id(value) -> bool:
    """True when a value is a member ID: 64 lowercase hex characters."""
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


class TeamDirects:
    """The routes of the direct messages. Team mixes this class in."""

    def _team_direct_route(self, method: str, parts: list[str], data: dict, repeat: str) -> dict:
        """Serve a route under `/v1/team/rooms/<room>/directs/<member>`. `parts` are the parts after `/v1/team`."""
        from .service_base import ServiceError
        room, member, rest = parts[1], parts[3], parts[4:]
        routes = {('GET', 'messages'): lambda: self.team_direct_messages(room, member, data),
                  ('POST', 'messages'): lambda: self.team_direct_send(room, member, data, repeat),
                  ('PATCH', 'message'): lambda: self.team_direct_edit(room, member, rest[1], data),
                  ('DELETE', 'message'): lambda: self.team_direct_unsend(room, member, rest[1]),
                  ('POST', 'read'): lambda: self.team_direct_mark_read(room, member, data)}
        shape = 'message' if len(rest) == 2 and rest[0] == 'messages' else rest[0] if len(rest) == 1 else ''
        route = routes.get((method, shape))
        if route is None:
            raise ServiceError(404, 'This route was not found.')
        if self._team_bot_only:
            raise ServiceError(409, TEAM_ONLY)
        return route()

    def team_direct_messages(self, room_id: str, member: str, data: dict) -> dict:
        """One page of the direct chat with `member`, newest first, as a page of the Team chat. A chat with a member
        that left keeps its history."""
        self._team_direct_room(room_id, member)
        before = query_number(data, 'before', None, 1, MAX_NUMBER, 'Use a whole number for before.')
        limit = query_number(data, 'limit', PAGE, 1, MAX_PAGE, f'Ask for 1 to {MAX_PAGE} messages.')
        with self.lock:
            room = self.team_store.room(room_id)
            rows = self.team_store.directs(room_id, member, before, limit + 1)
            messages = self._team_direct_views(room, member, rows[:limit])
        older = rows[limit - 1]['first'] if len(rows) > limit else None
        return {'room': room_id, 'member': member, 'messages': messages, 'before': older}

    def team_direct_send(self, room_id: str, member: str, data: dict, repeat: str = '') -> dict:
        """Write a direct message of this Mac's person to `member`, and return it as the chat shows it.

        With an `Idempotency-Key`, the entry ID comes from the room, the device and the key, as for a message of the
        Team chat (TeamMessages.team_send). So a repeat after a write without an answer finds that write and sends no
        second message.
        """
        from .service_base import ServiceError
        from .team import team_errors
        text = message_text(data.get('text'))
        reply_to = data.get('reply_to')
        if reply_to is not None and not entry_id(reply_to):
            raise ServiceError(400, GONE_MESSAGE)
        self._team_direct_room(room_id, member)
        entry = hashlib.sha256(f'{room_id}:direct:{repeat}'.encode()).hexdigest()[:32] if repeat else token_hex(16)
        with team_errors(), self._team_room_lock(room_id):
            room = self._team_writable(room_id)
            with self.lock:
                if self.team_store.entry(room_id, entry) is not None:
                    return self._team_direct_answer(room_id, member, entry)
                agreement = self._team_direct_reachable(room, member)
                if reply_to is not None and not self.team_store.directs_of(room_id, member, [reply_to]):
                    raise ServiceError(409, 'The message that you answer is no longer in the chat.')
            self._team_write_direct(room, entry, member, agreement, {'kind': 'direct'}, text, reply_to)
        return self._team_direct_answer(room_id, member, entry)

    def team_direct_edit(self, room_id: str, member: str, entry: str, data: dict) -> dict:
        """Change the text of a direct message of this Mac's person. The reply stays. The same text writes nothing.

        As for an edit in the Team chat, the Mac first reads the room to its end, so the store has the time of the
        relay before the edit replaces the write. The new box needs the agreement key of the other member, so a member
        that can no longer get a direct message answers 409.
        """
        from .team import team_errors
        text = message_text(data.get('text'))
        self._team_direct_room(room_id, member)
        with team_errors(), self._team_room_lock(room_id):
            while self._team_read(room_id):
                pass
            room, existing, row = self._team_own_direct(room_id, member, entry)
            if row['text'] != text:
                with self.lock:
                    agreement = self._team_direct_reachable(room, member)
                fields = {name: value for name, value in json.loads(existing['payload']).items()
                          if name not in ('rev', 'at', 'box')}
                self._team_write_direct(room, entry, member, agreement, fields, text, row['reply_to'])
        return self._team_direct_answer(room_id, member, entry)

    def team_direct_unsend(self, room_id: str, member: str, entry: str) -> dict:
        """Remove a direct message of this Mac's person with a `delete` entry. A repeat after a delete without an
        answer finds the `delete` entry of this Mac, and gets the same answer without a write."""
        from .team import team_errors
        self._team_direct_room(room_id, member)
        with team_errors(), self._team_room_lock(room_id):
            room, existing, _ = self._team_own_direct(room_id, member, entry, deleted=True)
            if existing['kind'] != 'delete':
                self._team_write(room, entry, False, {'kind': 'delete'}, tries=2)
        return {'room': room_id, 'member': member, 'id': entry, 'deleted': True}

    def team_direct_mark_read(self, room_id: str, member: str, data: dict) -> dict:
        """Move the read position of the direct chat with `member` to `first`. It only moves forward, and never past
        the newest message of the chat. A move tells the other devices of the person."""
        from .service_base import ServiceError
        first = data.get('first')
        if not number(first):
            raise ServiceError(400, 'Give the first of the newest message that the person read.')
        self._team_direct_room(room_id, member)
        with self.transaction():
            if self.team_store.room(room_id) is None:
                raise ServiceError(404, 'This Mac is not in this team.')
            if self.team_store.mark_direct_read(room_id, member, first) is not None:
                self._event(DIRECTS_EVENT, {'team': room_id, 'member': member})
            return {'room': room_id, 'member': member, 'read': self.team_store.direct_read_position(room_id, member),
                    'unread': self.team_store.direct_unread(room_id, member)}

    # Steps

    def _team_direct_room(self, room_id: str, member) -> dict:
        """The room of a direct chat. A member ID that is not one answers 404."""
        from .service_base import ServiceError
        room = self._team_room(room_id)
        if not member_id(member):
            raise ServiceError(404, NOT_HERE)
        return room

    @staticmethod
    def _team_can_direct(room: dict, member: dict) -> bool:
        """True when this Mac, which reads direct messages, can send one to the member now (rule 2 of the contract):
        the room is active, and the member is another member of the list now, with `direct`, and not a Mac for a team
        only."""
        return (room['state'] == ACTIVE and member['member'] != room['member'] and bool(member['direct'])
                and not member['bot_only'])

    def _team_direct_reachable(self, room: dict, member: str) -> str:
        """The public agreement key of `member` when this Mac can send it a direct message now, else a 409 with the
        reason. The caller holds the service lock."""
        from .service_base import ServiceError
        if member == room['member']:
            raise ServiceError(409, SELF)
        if not self._team_reads_directs:
            raise ServiceError(409, NOT_ON)
        row = self.team_store.member(room['room'], member)
        if row is None:
            raise ServiceError(409, LEFT)
        if row['bot_only']:
            raise ServiceError(409, BOT_ONLY)
        if not row['direct']:
            raise ServiceError(409, NOT_YET)
        return row['agreement']

    def _team_write_direct(self, room: dict, entry: str, member: str, agreement: str, fields: dict, text: str,
                           reply_to: str | None) -> None:
        """Seal the box of a direct message to `member` and write its entry. `fields` are the other fields of the
        payload. The caller holds the lock of the room, and no lock of the service."""
        plaintext = {'to': member, 'text': text}
        if reply_to is not None:
            plaintext['reply_to'] = reply_to
        secrets = team_keys.read(self.root, room['room'])
        box = self.team_helper.seal_direct(room['room'], entry, secrets['agreement_key'], room['member'], member,
                                           agreement, json.dumps(plaintext, ensure_ascii=False,
                                                                 separators=(',', ':')).encode())
        self._team_write(room, entry, False, dict(fields, box=box), tries=2,
                         direct={'member': member, 'text': text, 'reply_to': reply_to})

    def _team_own_direct(self, room_id: str, member: str, entry: str,
                         deleted: bool = False) -> tuple[dict, dict, dict | None]:
        """The room, the entry and the row of a direct message that this Mac's person wrote in the chat with `member`.
        With `deleted`, also a `delete` entry of this Mac, without a row. The caller holds the lock of the room."""
        from .service_base import ServiceError
        if not entry_id(entry):
            raise ServiceError(404, GONE_MESSAGE)
        room = self._team_writable(room_id)
        with self.lock:
            existing = self.team_store.entry(room_id, entry)
            row = self.team_store.direct(room_id, entry)
        if deleted and existing and existing['kind'] == 'delete' and existing['member'] == room['member']:
            return room, existing, None
        if existing is None or existing['kind'] != 'direct' or row is None or row['member'] != member:
            raise ServiceError(404, GONE_MESSAGE)
        if row['writer'] != room['member']:
            raise ServiceError(403, 'You can change only your own messages.')
        return room, existing, row

    def _team_direct_answer(self, room_id: str, member: str, entry: str) -> dict:
        from .service_base import ServiceError
        with self.lock:
            room = self.team_store.room(room_id)
            row = self.team_store.direct(room_id, entry) if room else None
            if row is None or row['member'] != member:
                raise ServiceError(404, GONE_MESSAGE)
            return {'room': room_id, 'member': member, 'message': self._team_direct_views(room, member, [row])[0]}

    def _team_direct_views(self, room: dict, member: str, rows: list[dict]) -> list[dict]:
        """The messages of a direct chat as the app gets them. `reply` quotes the message of the same chat that
        `reply_to` names, or is null when the chat does not show it. One query reads the answered messages of the
        page. The caller holds the service lock."""
        answered = self.team_store.directs_of(room['room'], member, [row['reply_to'] for row in rows if row['reply_to']])

        def quote(entry):
            row = answered.get(entry)
            return row and {'id': row['entry'], 'this_mac': row['writer'] == room['member'], 'text': row['text'][:PREVIEW]}
        return [{'id': row['entry'], 'first': row['first'], 'this_mac': row['writer'] == room['member'],
                 'text': row['text'], 'reply_to': row['reply_to'], 'reply': quote(row['reply_to']),
                 'created_at': row['created_at'], 'edited_at': row['updated_at'] if row['edited'] else None}
                for row in rows]

    def _team_direct_summary(self, room: dict) -> list[dict]:
        """The direct chats of a room for GET /v1/team/rooms: one item for each chat with a message that shows, newest
        chat first. A chat with a member that left stays. The caller holds the service lock."""
        return [{'member': row['member'], 'member_name': row['member_name'],
                 'unread': self.team_store.direct_unread(room['room'], row['member']),
                 'last_message': {'first': row['first'], 'this_mac': row['writer'] == room['member'],
                                  'text': row['text'][:PREVIEW], 'created_at': row['created_at']}}
                for row in self.team_store.direct_chats(room['room'])]
