"""The Team chat: the messages of the people of a team (docs/team-log.md, "Team chat").

A message is a `message` entry of the log, not kept: the relay holds it for its retention time, and each Mac keeps the
history in `team_messages` (team_store.py). An edit is a new write of the same entry ID with the next `rev`, and a
removal is a `delete` entry over it. The routes are for the chat of the app:

- `GET /v1/team/rooms/<room>/messages?before=<first>&limit=<n>&chat=<id>`: one page of messages, newest first.
- `POST /v1/team/rooms/<room>/messages` with `{text, reply_to?, mentions?, chat?}`: send a message.
- `PATCH /v1/team/rooms/<room>/messages/<id>` with `{text}`: change a message of this Mac's person.
- `DELETE /v1/team/rooms/<room>/messages/<id>`: remove a message of this Mac's person. On the admin's Mac, also a
  message of another member or of a bot: an `erase` entry takes it out of the chat on each Mac.
- `POST /v1/team/rooms/<room>/read` with `{first, chat?}`: move the read position of this Mac.

`chat` names a named chat of the team (team_chats.py). Without it, a route means the Team chat. A message has its chat,
so an edit and a removal need none.

A write goes to the relay inside the request, as the other team writes do, and a failure is an error for the person.
The text of a message is private, so no log line of this module has it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from secrets import token_hex

from . import team_keys
from .team_pages import team_id, team_ids
from .team_relay import MAX_NUMBER, number
from .team_store import ACTIVE, MAX_MENTIONS, MAX_TEXT, MESSAGES_EVENT, entry_id, unicode_text

log = logging.getLogger(__name__)

# The size of a page of messages: the default and the most.
PAGE = 50
MAX_PAGE = 100
# The start of the text that the chat list shows for the newest message of a room.
PREVIEW = 200
GONE_MESSAGE = 'This message is no longer in the chat.'
OWN_MESSAGE = 'You can change only your own messages.'
# The label of the entry ID of an `erase` entry (erase_entry).
ERASE_LABEL = b'coconut room erase'


def erase_entry(token: str, room: str, message: str) -> str:
    """The entry ID of the `erase` entry of a message: from the room and the entry ID of the message, so a repeat
    writes the same entry. The member token of the admin is the key of the HMAC, as for a `remove` entry
    (team.removal_entry), so another member cannot take the ID first."""
    return hmac.new(token.encode(), ERASE_LABEL + bytes.fromhex(room) + bytes.fromhex(message),
                    hashlib.sha256).hexdigest()[:32]


def message_text(value) -> str:
    """The text of a message that this Mac writes, with one text for a person for each problem."""
    from .service_base import ServiceError
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(400, 'Write a message.')
    if len(value) > MAX_TEXT:
        raise ServiceError(400, f'Use at most {MAX_TEXT:,} characters for a message.')
    if not unicode_text(value):
        raise ServiceError(400, 'Use only valid text in the message.')
    return value


def query_number(data: dict, name: str, default: int | None, low: int, high: int, text: str) -> int | None:
    """A whole number of the query of a GET, or the default when the query does not have it."""
    from .service_base import ServiceError
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
            chat = self._team_chat_named(room_id, data.get('chat'))
            rows = self.team_store.messages(room_id, before, limit + 1, chat)
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
        from .service_base import ServiceError
        from .team import team_errors
        text = message_text(data.get('text'))
        reply_to, mentions = data.get('reply_to'), data.get('mentions')
        if reply_to is not None and not entry_id(reply_to):
            raise ServiceError(400, GONE_MESSAGE)
        if mentions is not None and (not isinstance(mentions, list) or len(mentions) > MAX_MENTIONS):
            raise ServiceError(400, f'Mention at most {MAX_MENTIONS} people and agents in one message.')
        self._team_room(room_id)
        entry = hashlib.sha256(f'{room_id}:{repeat}'.encode()).hexdigest()[:32] if repeat else token_hex(16)
        with team_errors(), self._team_room_lock(room_id):
            room = self._team_writable(room_id)
            fields = {'kind': 'message', 'text': text}
            with self.lock:
                if self.team_store.entry(room_id, entry) is not None:
                    return self._team_message_answer(room_id, entry)
                chat = self._team_chat_named(room_id, data.get('chat'))
                if chat is not None:
                    fields['chat'] = chat
                if reply_to is not None:
                    answered = self.team_store.message(room_id, reply_to)
                    if answered is None or answered['chat'] != chat:
                        raise ServiceError(409, 'The message that you answer is no longer in the chat.')
                    fields['reply_to'] = reply_to
                if mentions:
                    fields['mentions'] = self._team_mention_entries(room, mentions)
            self._team_write(room, entry, False, fields, tries=2)
        return self._team_message_answer(room_id, entry)

    def team_edit(self, room_id: str, entry: str, data: dict) -> dict:
        """Change the text of a message of this Mac's person. The other fields of the message stay.

        The edit replaces the write of the message on the relay. The store can have that write with the clock of this
        Mac, when the sync did not read it back yet (TeamStore._relay_time). So the Mac first reads the room to its
        end, and the store gets the time of the relay for `created_at` before the relay drops that write. An edit
        costs one more read of the relay. The read holds the lock of the room and no lock of the service.
        """
        from .team import team_errors
        text = message_text(data.get('text'))
        self._team_room(room_id)
        with team_errors(), self._team_room_lock(room_id):
            while self._team_read(room_id):
                pass
            room, existing = self._team_own_message(room_id, entry)
            fields = {name: value for name, value in json.loads(existing['payload']).items()
                      if name not in ('rev', 'at')}
            # The same text needs no write, so a repeat of an edit without a key changes nothing.
            if fields.get('text') != text:
                self._team_write(room, entry, False, dict(fields, text=text), tries=2)
        return self._team_message_answer(room_id, entry)

    def team_unsend(self, room_id: str, entry: str) -> dict:
        """Remove a message: a `delete` entry replaces a message of this Mac's person on each Mac. On the admin's
        Mac, an `erase` entry takes a message of another member or of a bot out of the chat on each Mac
        (docs/team-log.md, "The admin deletes a message").

        A write can reach the relay without an answer, and team_request keeps no answer of a request that failed. So
        a repeat finds the `delete` or the `erase` entry of this Mac after the read of _team_writable, and gets the
        same answer without a write.
        """
        from .team import team_errors
        self._team_room(room_id)
        with team_errors(), self._team_room_lock(room_id):
            room, existing, own = self._team_deletable(room_id, entry)
            if own and existing['kind'] != 'delete':
                self._team_write(room, entry, False, {'kind': 'delete'}, tries=2)
            elif not own and existing['kind'] == 'message':
                with self.lock:
                    done = self.team_store.erased(room_id, entry)
                if not done:
                    token = team_keys.read(self.root, room_id)['token']
                    self._team_write(room, erase_entry(token, room_id, entry), False,
                                     {'kind': 'erase', 'entry': entry}, tries=2)
                    log.info('coconut.team_log room %s: the admin erased the message %s.', room_id[:8], entry)
        return {'room': room_id, 'id': entry, 'deleted': True}

    def team_mark_read(self, room_id: str, data: dict) -> dict:
        """Move the read position of this Mac in one chat to `first`, the `first` of the newest message that the person
        read. The position only moves forward, and never past the newest message. A move tells the other devices."""
        from .service_base import ServiceError
        first = data.get('first')
        if not number(first):
            raise ServiceError(400, 'Give the first of the newest message that the person read.')
        self._team_room(room_id)
        with self.transaction():
            room = self.team_store.room(room_id)
            if room is None:
                raise ServiceError(404, 'This Mac is not in this team.')
            chat = self._team_chat_named(room_id, data.get('chat'))
            if self.team_store.mark_read(room_id, first, chat) is not None:
                self._event(MESSAGES_EVENT, {'team': room_id})
            return {'room': room_id, 'read': self.team_store.read_position(room_id, chat),
                    'unread': self.team_store.unread(room_id, room['member'], chat)}

    # Steps

    def _team_writable(self, room_id: str) -> dict:
        """The room for a write of the chat, read again after the caller took the lock of the room: a leave can have
        removed it in between. The Mac first reads back an earlier write without an answer (TeamSync._team_settle),
        so the store has the newest `rev` of each own entry."""
        from .service_base import ServiceError
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
        from .service_base import ServiceError
        room, existing = self._team_message_entry(room_id, entry, ('message',))
        if not self._team_person_wrote(room, existing):
            raise ServiceError(403, OWN_MESSAGE)
        # The admin erased the message, or removed its chat, so a new write of it shows nothing on any Mac.
        with self.lock:
            if self.team_store.erased(room_id, entry) or self.team_store.message(room_id, entry) is None:
                raise ServiceError(404, GONE_MESSAGE)
        return room, existing

    def _team_deletable(self, room_id: str, entry: str) -> tuple[dict, dict, bool]:
        """The room and the entry of a message that the person can delete, and True when the person wrote it. This
        Mac's person deletes an own message, and the admin also a message of another member or of a bot. The entry
        can be a `delete` entry already, after a delete without an answer. The caller holds the lock of the room."""
        from .service_base import ServiceError
        room, existing = self._team_message_entry(room_id, entry, ('message', 'delete'))
        own = self._team_person_wrote(room, existing)
        if not own and room['admin'] != room['member']:
            raise ServiceError(403, OWN_MESSAGE)
        return room, existing, own

    def _team_message_entry(self, room_id: str, entry: str, kinds: tuple) -> tuple[dict, dict]:
        from .service_base import ServiceError
        if not entry_id(entry):
            raise ServiceError(404, GONE_MESSAGE)
        room = self._team_writable(room_id)
        with self.lock:
            existing = self.team_store.entry(room_id, entry)
        if existing is None or existing['kind'] not in kinds:
            raise ServiceError(404, GONE_MESSAGE)
        return room, existing

    @staticmethod
    def _team_person_wrote(room: dict, existing: dict) -> bool:
        """True when this Mac's person wrote the entry. A message of a bot of this Mac belongs to the bot."""
        return existing['member'] == room['member'] and json.loads(existing['payload']).get('bot') is None

    def _team_mention_entries(self, room: dict, mentions: list) -> list[str]:
        """The entry IDs of the mentions that the app gives: the member ID of a member in the list, or the ID of a bot
        that GET /v1/team/rooms shows. The caller holds the service lock."""
        from .service_base import ServiceError
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
                raise ServiceError(400, 'Mention only people and agents of this team.')
        return list(dict.fromkeys(entries))

    def _team_message_answer(self, room_id: str, entry: str) -> dict:
        from .service_base import ServiceError
        with self.lock:
            room = self.team_store.room(room_id)
            row = self.team_store.message(room_id, entry) if room else None
            if row is None:
                raise ServiceError(404, GONE_MESSAGE)
            return {'room': room_id, 'message': self._team_message_views(room, [row])[0]}

    def _team_message_views(self, room: dict, rows: list[dict]) -> list[dict]:
        """The messages as the chat of the app gets them. A mention is the member ID of a member, or the ID of a team
        bot as in GET /v1/team/rooms. A mention that the store cannot name stays out.

        `reply` quotes the message that `reply_to` names, because that message is often not in the page of the app.
        It is null when the chat does not show that message: removed, not valid, never seen, or in another chat. One
        query reads the answered messages of the whole page. The caller holds the service lock.
        """
        mentions = [json.loads(row['mentions']) for row in rows]
        names = self.team_store.names(room['room'], [entry for entries in mentions for entry in entries])
        answered = self.team_store.messages_of(room['room'], [row['reply_to'] for row in rows if row['reply_to']])

        def phone_id(entry):
            kind, member = names[entry]
            return member if kind == 'member' else team_id(room['room'], entry)

        def quote(entry, chat):
            row = answered.get(entry)
            return None if row is None or row['chat'] != chat else {'id': row['entry'], 'member': row['member'], 'member_name': row['member_name'],
                            'bot': team_id(room['room'], row['bot']) if row['bot'] else None,
                            'bot_name': row['bot_name'], 'text': row['text'][:PREVIEW]}
        return [{'id': row['entry'], 'first': row['first'], 'member': row['member'], 'member_name': row['member_name'],
                 'this_mac': row['member'] == room['member'],
                 'bot': team_id(room['room'], row['bot']) if row['bot'] else None, 'bot_name': row['bot_name'],
                 'text': row['text'], 'reply_to': row['reply_to'], 'reply': quote(row['reply_to'], row['chat']),
                 'chat': row['chat'],
                 'mentions': [phone_id(entry) for entry in entries if entry in names],
                 'created_at': row['created_at'], 'edited_at': row['updated_at'] if row['edited'] else None}
                for row, entries in zip(rows, mentions)]

    def _team_chat_summary(self, room: dict, chat: str | None = None) -> dict:
        """The unread count and the newest message of one chat of a room (None for the Team chat), for GET
        /v1/team/rooms. `mentioned` is true while an unread message names this Mac's person, and `mention_first` is the
        `first` of the newest such message. The caller holds the service lock."""
        store = self.team_store
        newest = store.messages(room['room'], None, 1, chat)
        last = None
        if newest:
            row = newest[0]
            # The writer, as a message of a page has it, so the list can say "You" without a compare of names.
            last = {'first': row['first'], 'member': row['member'], 'member_name': row['member_name'],
                    'this_mac': row['member'] == room['member'],
                    'bot': team_id(room['room'], row['bot']) if row['bot'] else None, 'bot_name': row['bot_name'],
                    'text': row['text'][:PREVIEW],
                    'created_at': row['created_at']}
        mention = store.mention_first(room['room'], room['member'], chat)
        return {'unread': store.unread(room['room'], room['member'], chat), 'last_message': last,
                'mentioned': mention is not None, **({'mention_first': mention} if mention is not None else {})}
