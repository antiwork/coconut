"""The Team chat: the messages of the people of a team (docs/team-log.md, "Team chat").

A message is a `message` entry of the log, not kept: the relay holds it for its retention time, and each Mac keeps the
history in `team_messages` (team_store.py). An edit is a new write of the same entry ID with the next `rev`, and a
removal is a `delete` entry over it. The routes are for the chat of the app:

- `GET /v1/team/rooms/<room>/messages?before=<first>&limit=<n>`: one page of messages, newest first.
- `POST /v1/team/rooms/<room>/messages` with `{text, reply_to?, mentions?}`: send a message.
- `PATCH /v1/team/rooms/<room>/messages/<id>` with `{text}`: change a message of this Mac's person.
- `DELETE /v1/team/rooms/<room>/messages/<id>`: remove a message of this Mac's person.
- `POST /v1/team/rooms/<room>/read` with `{first}`: move the read position of this Mac.

A write goes to the relay inside the request, as the other team writes do, and a failure is an error for the person.
The text of a message is private, so no log line of this module has it.
"""
from __future__ import annotations

import hashlib
import json
from secrets import token_hex

from .team_pages import team_id, team_ids
from .team_relay import MAX_NUMBER, number
from .team_store import ACTIVE, MAX_MENTIONS, MAX_TEXT, MESSAGES_EVENT, entry_id, unicode_text

# The size of a page of messages: the default and the most.
PAGE = 50
MAX_PAGE = 100
# The start of the text that the chat list shows for the newest message of a room.
PREVIEW = 200
GONE_MESSAGE = 'This message is no longer in the chat.'


def message_text(value) -> str:
    """The text of a message that this Mac writes, with one text for a person for each problem."""
    from .service import ServiceError
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(400, 'Write a message.')
    if len(value) > MAX_TEXT:
        raise ServiceError(400, f'Use at most {MAX_TEXT:,} characters for a message.')
    if not unicode_text(value):
        raise ServiceError(400, 'Use only valid text in the message.')
    return value


def query_number(data: dict, name: str, default: int | None, low: int, high: int, text: str) -> int | None:
    """A whole number of the query of a GET, or the default when the query does not have it."""
    from .service import ServiceError
    value = data.get(name)
    if value is None:
        return default
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or not low <= int(value) <= high:
        raise ServiceError(400, text)
    return int(value)


class TeamMessages:
    """The routes of the Team chat. Team mixes this class in."""

    def team_messages(self, room_id: str, data: dict) -> dict:
        """One page of the chat, newest first. `before` is the `first` of the oldest message that the app has, and
        the answer has the `before` of the next page, or null when no older message exists."""
        room = self._team_room(room_id)
        before = query_number(data, 'before', None, 1, MAX_NUMBER, 'Use a whole number for before.')
        limit = query_number(data, 'limit', PAGE, 1, MAX_PAGE, f'Ask for 1 to {MAX_PAGE} messages.')
        with self.lock:
            rows = self.team_store.messages(room_id, before, limit + 1)
            messages = self._team_message_views(room, rows[:limit])
        older = rows[limit - 1]['first'] if len(rows) > limit else None
        return {'room': room_id, 'messages': messages, 'before': older}

    def team_send(self, room_id: str, data: dict, repeat: str = '') -> dict:
        """Write a new message of this Mac's person, and return it as the chat shows it.

        A write can reach the relay without an answer, and the app then sends the request again with the same
        `Idempotency-Key`. team_request keeps no answer of a request that failed, so the repeat runs again. The entry
        ID of a message with a key comes from the room, the device and the key, so the repeat finds the first write
        after the read of _team_writable and writes no second message. `repeat` names the device and the key.
        """
        from .service import ServiceError
        from .team import team_errors
        text = message_text(data.get('text'))
        reply_to, mentions = data.get('reply_to'), data.get('mentions')
        if reply_to is not None and not entry_id(reply_to):
            raise ServiceError(400, GONE_MESSAGE)
        if mentions is not None and (not isinstance(mentions, list) or len(mentions) > MAX_MENTIONS):
            raise ServiceError(400, f'Mention at most {MAX_MENTIONS} people and bots in one message.')
        self._team_room(room_id)
        entry = hashlib.sha256(f'{room_id}:{repeat}'.encode()).hexdigest()[:32] if repeat else token_hex(16)
        with team_errors(), self._team_room_lock(room_id):
            room = self._team_writable(room_id)
            fields = {'kind': 'message', 'text': text}
            with self.lock:
                if self.team_store.entry(room_id, entry) is not None:
                    return self._team_message_answer(room_id, entry)
                if reply_to is not None:
                    if self.team_store.message(room_id, reply_to) is None:
                        raise ServiceError(409, 'The message that you answer is no longer in the chat.')
                    fields['reply_to'] = reply_to
                if mentions:
                    fields['mentions'] = self._team_mention_entries(room, mentions)
            self._team_write(room, entry, False, fields, tries=2)
        return self._team_message_answer(room_id, entry)

    def team_edit(self, room_id: str, entry: str, data: dict) -> dict:
        """Change the text of a message of this Mac's person. The other fields of the message stay."""
        from .team import team_errors
        text = message_text(data.get('text'))
        self._team_room(room_id)
        with team_errors(), self._team_room_lock(room_id):
            room, existing = self._team_own_message(room_id, entry)
            fields = {name: value for name, value in json.loads(existing['payload']).items()
                      if name not in ('rev', 'at')}
            # The same text needs no write, so a repeat of an edit without a key changes nothing.
            if fields.get('text') != text:
                self._team_write(room, entry, False, dict(fields, text=text), tries=2)
        return self._team_message_answer(room_id, entry)

    def team_unsend(self, room_id: str, entry: str) -> dict:
        """Remove a message of this Mac's person: a `delete` entry replaces it on each Mac."""
        from .team import team_errors
        self._team_room(room_id)
        with team_errors(), self._team_room_lock(room_id):
            room, _ = self._team_own_message(room_id, entry)
            self._team_write(room, entry, False, {'kind': 'delete'}, tries=2)
        return {'room': room_id, 'id': entry, 'deleted': True}

    def team_mark_read(self, room_id: str, data: dict) -> dict:
        """Move the read position of this Mac to `first`, the `first` of the newest message that the person read.
        The position only moves forward, and never past the newest message. A move tells the other devices."""
        from .service import ServiceError
        first = data.get('first')
        if not number(first):
            raise ServiceError(400, 'Give the first of the newest message that the person read.')
        self._team_room(room_id)
        with self.transaction():
            room = self.team_store.room(room_id)
            if room is None:
                raise ServiceError(404, 'This Mac is not in this team.')
            if self.team_store.mark_read(room_id, first) is not None:
                self._event(MESSAGES_EVENT, {'team': room_id})
            return {'room': room_id, 'read': self.team_store.read_position(room_id),
                    'unread': self.team_store.unread(room_id, room['member'])}

    # Steps

    def _team_writable(self, room_id: str) -> dict:
        """The room for a write of the chat, read again after the caller took the lock of the room: a leave can have
        removed it in between. The Mac first reads back an earlier write without an answer (TeamSync._team_settle),
        so the store has the newest `rev` of each own entry."""
        from .service import ServiceError
        from .team_sync import INACTIVE
        self._team_settle(room_id)
        with self.lock:
            room = self.team_store.room(room_id)
        if room is None:
            raise ServiceError(404, 'This Mac is not in this team.')
        if room['state'] != ACTIVE:
            raise ServiceError(409, INACTIVE[room['state']])
        return room

    def _team_own_message(self, room_id: str, entry: str) -> tuple[dict, dict]:
        """The room and the entry of a message that this Mac's person wrote. The caller holds the lock of the room."""
        from .service import ServiceError
        if not entry_id(entry):
            raise ServiceError(404, GONE_MESSAGE)
        room = self._team_writable(room_id)
        with self.lock:
            existing = self.team_store.entry(room_id, entry)
        if existing is None or existing['kind'] != 'message':
            raise ServiceError(404, GONE_MESSAGE)
        # A message of a bot of this Mac belongs to the bot, not to the person.
        if existing['member'] != room['member'] or json.loads(existing['payload']).get('bot') is not None:
            raise ServiceError(403, 'You can change only your own messages.')
        return room, existing

    def _team_mention_entries(self, room: dict, mentions: list) -> list[str]:
        """The entry IDs of the mentions that the app gives: the member ID of a member in the list, or the ID of a bot
        that GET /v1/team/rooms shows. The caller holds the service lock."""
        from .service import ServiceError
        members = {member['member']: member['entry'] for member in self.team_store.members(room['room'])}
        bots = {entry for (entry,) in self.db.execute('SELECT entry FROM team_bots WHERE room=?', (room['room'],))}
        entries = []
        for mention in mentions:
            ids = team_ids(mention)
            if isinstance(mention, str) and mention in members:
                entries.append(members[mention])
            elif ids and ids[0] == room['room'] and ids[1] in bots:
                entries.append(ids[1])
            else:
                raise ServiceError(400, 'Mention only people and bots of this team.')
        return list(dict.fromkeys(entries))

    def _team_message_answer(self, room_id: str, entry: str) -> dict:
        from .service import ServiceError
        with self.lock:
            room = self.team_store.room(room_id)
            row = self.team_store.message(room_id, entry) if room else None
            if row is None:
                raise ServiceError(404, GONE_MESSAGE)
            return {'room': room_id, 'message': self._team_message_views(room, [row])[0]}

    def _team_message_views(self, room: dict, rows: list[dict]) -> list[dict]:
        """The messages as the chat of the app gets them. A mention is the member ID of a member, or the ID of a team
        bot as in GET /v1/team/rooms. A mention that the store cannot name stays out. The caller holds the service
        lock."""
        mentions = [json.loads(row['mentions']) for row in rows]
        names = self.team_store.names(room['room'], [entry for entries in mentions for entry in entries])

        def phone_id(entry):
            kind, member = names[entry]
            return member if kind == 'member' else team_id(room['room'], entry)
        return [{'id': row['entry'], 'first': row['first'], 'member': row['member'], 'member_name': row['member_name'],
                 'this_mac': row['member'] == room['member'],
                 'bot': team_id(room['room'], row['bot']) if row['bot'] else None, 'text': row['text'],
                 'reply_to': row['reply_to'], 'mentions': [phone_id(entry) for entry in entries if entry in names],
                 'created_at': row['created_at'], 'edited_at': row['updated_at'] if row['edited'] else None}
                for row, entries in zip(rows, mentions)]

    def _team_chat_summary(self, room: dict) -> dict:
        """The unread count and the newest message of a room, for GET /v1/team/rooms. The caller holds the service
        lock."""
        newest = self.team_store.messages(room['room'], None, 1)
        last = None
        if newest:
            row = newest[0]
            last = {'first': row['first'], 'member_name': row['member_name'], 'text': row['text'][:PREVIEW],
                    'created_at': row['created_at']}
        return {'unread': self.team_store.unread(room['room'], room['member']), 'last_message': last}
