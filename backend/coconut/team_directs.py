"""Direct messages: a private chat between two people of a team, or between a person and a bot of a Mac for a team only
(docs/team-log.md, "Direct messages" and "Direct chat with an agent").

A direct message is a `direct` entry of the log. Its text is in a box that only the writer and the member that the
message is for can open: the pair key of the two Macs seals it a second time. The other members store the entry and
show nothing. Each Mac keeps its direct chats in `team_directs` (team_store.py), apart from the Team chat. A message of
a chat with a bot names the `bot` entry of the bot in its box, and the member is the Mac that runs the bot. The routes
are for the app of the person, and only a paired device reaches them on a person's Mac (team.tool_route). `<chat>` is
the member ID of the other person, or the team bot ID of a bot (`t-<room ID>-<entry ID of the bot entry>`):

- `GET /v1/team/rooms/<room>/directs/<chat>/messages?before=<first>&limit=<n>`: one page of the chat, newest first.
- `POST /v1/team/rooms/<room>/directs/<chat>/messages` with `{text, reply_to?}`: send a direct message. A message to
  a bot can also have `about_document_id`: the Library ID of a page of that bot.
- `PATCH /v1/team/rooms/<room>/directs/<chat>/messages/<id>` with `{text}`: change a message of this Mac's person.
- `DELETE /v1/team/rooms/<room>/directs/<chat>/messages/<id>`: remove a message of this Mac's person.
- `POST /v1/team/rooms/<room>/directs/<chat>/read` with `{first}`: move the read position of the chat.

An answer names a chat of two people with `member`, and a chat with a bot with `bot` (the team bot ID). A Mac for a
team only has no person, so it answers 409. The text of a direct message is private, so no log line of this module
has it.
"""
from __future__ import annotations

import hashlib
import json
import math
from secrets import token_hex

from . import team_keys
from .team_messages import GONE_MESSAGE, MAX_PAGE, PAGE, PREVIEW, message_text, query_number
from .team_relay import HEX64, MAX_NUMBER, number
from .team_store import ACTIVE, BOT_DIRECTS_EVENT, DIRECTS_EVENT, entry_id

# The answers of a person's Mac when the member of a chat cannot get a direct message now (rule 2 of the contract).
NOT_HERE = 'This person is not in this team.'
SELF = 'You cannot send a direct message to yourself.'
LEFT = 'This person is no longer in the team.'
BOT_ONLY = 'This Mac runs only bots for the team. Write to its bots in the team chat.'
NOT_YET = 'This person cannot get direct messages yet. Ask them to update the Coconut app.'
NOT_ON = 'Direct messages are not on for this Mac yet. Open the Team screen of the Coconut app, then try again.'
# The answers of a person's Mac when a bot cannot get a direct message now (docs/team-log.md, "Who can write to an
# agent"), and when `about_document_id` does not name a page of the bot of the chat.
BOT_GONE = 'This agent is no longer in the team.'
BOT_SILENT = 'This agent does not answer direct messages now.'
CHOOSE_PAGE = 'Choose a page of this agent in the Library.'
# The answer of a Mac for a team only to each route of this module.
TEAM_ONLY = 'This Mac runs Coconut for a team only, so it has no direct messages.'


def member_id(value) -> bool:
    """True when a value is a member ID: 64 lowercase hex characters."""
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


class TeamDirects:
    """The routes of the direct messages. Team mixes this class in.

    A chat is (member, bot): the other member and None for a chat of two people, or the member of the `bot` entry and
    the entry ID of that entry for a chat with a bot. Each route finds the chat at its start (_team_direct_chat), and
    then runs one path for both kinds."""

    def _team_direct_route(self, method: str, parts: list[str], data: dict, repeat: str) -> dict:
        """Serve a route under `/v1/team/rooms/<room>/directs/<chat>`. `parts` are the parts after `/v1/team`."""
        from .service_base import ServiceError
        room, chat, rest = parts[1], parts[3], parts[4:]
        routes = {('GET', 'messages'): lambda: self.team_direct_messages(room, chat, data),
                  ('POST', 'messages'): lambda: self.team_direct_send(room, chat, data, repeat),
                  ('PATCH', 'message'): lambda: self.team_direct_edit(room, chat, rest[1], data),
                  ('DELETE', 'message'): lambda: self.team_direct_unsend(room, chat, rest[1]),
                  ('POST', 'read'): lambda: self.team_direct_mark_read(room, chat, data)}
        shape = 'message' if len(rest) == 2 and rest[0] == 'messages' else rest[0] if len(rest) == 1 else ''
        route = routes.get((method, shape))
        if route is None:
            raise ServiceError(404, 'This route was not found.')
        if self._team_bot_only:
            raise ServiceError(409, TEAM_ONLY)
        return route()

    def team_direct_messages(self, room_id: str, chat: str, data: dict) -> dict:
        """One page of a direct chat, newest first, as a page of the Team chat. A chat with a member or a bot that left
        keeps its history."""
        member, bot = self._team_direct_chat(room_id, chat)
        before = query_number(data, 'before', None, 1, MAX_NUMBER, 'Use a whole number for before.')
        limit = query_number(data, 'limit', PAGE, 1, MAX_PAGE, f'Ask for 1 to {MAX_PAGE} messages.')
        with self.lock:
            room = self.team_store.room(room_id)
            rows = self.team_store.directs(room_id, member, before, limit + 1, bot)
            messages = self._team_direct_views(room, member, bot, rows[:limit])
        older = rows[limit - 1]['first'] if len(rows) > limit else None
        return {'room': room_id, **self._team_direct_key(room_id, member, bot), 'messages': messages, 'before': older}

    def team_direct_send(self, room_id: str, chat: str, data: dict, repeat: str = '') -> dict:
        """Write a direct message of this Mac's person in a chat, and return it as the chat shows it.

        With an `Idempotency-Key`, the entry ID comes from the room, the device and the key, as for a message of the
        Team chat (TeamMessages.team_send). So a repeat after a write without an answer finds that write and sends no
        second message. In a chat with a bot, `about_document_id` names the page that the message is about.
        """
        from .service_base import ServiceError
        from .team import team_errors
        text = message_text(data.get('text'))
        reply_to = data.get('reply_to')
        if reply_to is not None and not entry_id(reply_to):
            raise ServiceError(400, GONE_MESSAGE)
        member, bot = self._team_direct_chat(room_id, chat)
        entry = hashlib.sha256(f'{room_id}:direct:{repeat}'.encode()).hexdigest()[:32] if repeat else token_hex(16)
        with team_errors(), self._team_room_lock(room_id):
            room = self._team_writable(room_id)
            with self.lock:
                if self.team_store.entry(room_id, entry) is not None:
                    return self._team_direct_answer(room_id, member, bot, entry)
                agreement = self._team_direct_reachable(room, member, bot)
                if reply_to is not None and not self.team_store.directs_of(room_id, member, [reply_to], bot):
                    raise ServiceError(409, 'The message that you answer is no longer in the chat.')
                page = self._team_direct_page(room, bot, data) if bot is not None else None
            self._team_write_direct(room, entry, member, agreement, {'kind': 'direct'}, text, reply_to, bot, page)
        return self._team_direct_answer(room_id, member, bot, entry)

    def team_direct_edit(self, room_id: str, chat: str, entry: str, data: dict) -> dict:
        """Change the text of a direct message of this Mac's person. The reply, the bot and the page stay. The same text
        writes nothing.

        As for an edit in the Team chat, the Mac first reads the room to its end, so the store has the time of the
        relay before the edit replaces the write. The new box needs the agreement key of the other member, so a member
        or a bot that can no longer get a direct message answers 409.
        """
        from .team import team_errors
        text = message_text(data.get('text'))
        member, bot = self._team_direct_chat(room_id, chat)
        with team_errors(), self._team_room_lock(room_id):
            while self._team_read(room_id):
                pass
            room, existing, row = self._team_own_direct(room_id, member, bot, entry)
            if row['text'] != text:
                with self.lock:
                    agreement = self._team_direct_reachable(room, member, bot)
                fields = {name: value for name, value in json.loads(existing['payload']).items()
                          if name not in ('rev', 'at', 'box')}
                self._team_write_direct(room, entry, member, agreement, fields, text, row['reply_to'], bot, row['page'])
        return self._team_direct_answer(room_id, member, bot, entry)

    def team_direct_unsend(self, room_id: str, chat: str, entry: str) -> dict:
        """Remove a direct message of this Mac's person with a `delete` entry. A repeat after a delete without an
        answer finds the `delete` entry of this Mac, and gets the same answer without a write."""
        from .team import team_errors
        member, bot = self._team_direct_chat(room_id, chat)
        with team_errors(), self._team_room_lock(room_id):
            room, existing, _ = self._team_own_direct(room_id, member, bot, entry, deleted=True)
            if existing['kind'] != 'delete':
                self._team_write(room, entry, False, {'kind': 'delete'}, tries=2)
        return {'room': room_id, **self._team_direct_key(room_id, member, bot), 'id': entry, 'deleted': True}

    def team_direct_mark_read(self, room_id: str, chat: str, data: dict) -> dict:
        """Move the read position of a direct chat to `first`. It only moves forward, and never past the newest message
        of the chat. A move tells the other devices of the person."""
        from .service_base import ServiceError
        first = data.get('first')
        if not number(first):
            raise ServiceError(400, 'Give the first of the newest message that the person read.')
        member, bot = self._team_direct_chat(room_id, chat)
        key = self._team_direct_key(room_id, member, bot)
        with self.transaction():
            if self.team_store.room(room_id) is None:
                raise ServiceError(404, 'This Mac is not in this team.')
            if self.team_store.mark_direct_read(room_id, member, first, bot) is not None:
                self._event(DIRECTS_EVENT if bot is None else BOT_DIRECTS_EVENT, {'team': room_id, **key})
            return {'room': room_id, **key, 'read': self.team_store.direct_read_position(room_id, member, bot),
                    'unread': self.team_store.direct_unread(room_id, member, bot)}

    # Steps

    def _team_direct_chat(self, room_id: str, chat) -> tuple[str | None, str | None]:
        """The chat that a route names, as (member, bot). A member ID is a chat of two people. A team bot ID of this
        room is a chat with that bot, and its member is the member that team_names has for the `bot` entry: None when
        the store never showed the entry, so the chat has no message. Another value answers 404."""
        from .service_base import ServiceError
        from .team_pages import team_ids
        self._team_room(room_id)
        if member_id(chat):
            return chat, None
        ids = team_ids(chat)
        if ids is None or ids[0] != room_id:
            raise ServiceError(404, NOT_HERE)
        with self.lock:
            return self.team_store.bot_member(room_id, ids[1]), ids[1]

    @staticmethod
    def _team_direct_key(room_id: str, member: str | None, bot: str | None) -> dict:
        """The field that names a chat in an answer: `member` for a chat of two people, `bot` (the team bot ID) for a
        chat with a bot."""
        from .team_pages import team_id
        return {'member': member} if bot is None else {'bot': team_id(room_id, bot)}

    @staticmethod
    def _team_can_direct(room: dict, member: dict) -> bool:
        """True when this Mac, which reads direct messages, can send one to the member now (rule 2 of the contract):
        the room is active, and the member is another member of the list now, with `direct`, and not a Mac for a team
        only."""
        return (room['state'] == ACTIVE and member['member'] != room['member'] and bool(member['direct'])
                and not member['bot_only'])

    def _team_direct_reachable(self, room: dict, member: str | None, bot: str | None = None) -> str:
        """The public agreement key of the member of a chat when this Mac can send it a direct message now, else a 409
        with the reason. For a chat with a bot, the store has a valid `bot` entry with `direct`, and its member is
        another member of the list now. The caller holds the service lock."""
        from .service_base import ServiceError
        if bot is not None:
            if not self._team_reads_directs:
                raise ServiceError(409, NOT_ON)
            row = next((row for row in self.team_store.bots(room['room']) if row['entry'] == bot), None)
            if row is None and self.team_store.bot_member(room['room'], bot) == room['member']:
                # A bot of this Mac: a person's Mac never marks its own bot.
                raise ServiceError(409, BOT_SILENT)
            peer = self.team_store.member(room['room'], row['member']) if row else None
            if peer is None:
                raise ServiceError(409, BOT_GONE)
            if not row['direct']:
                raise ServiceError(409, BOT_SILENT)
            return peer['agreement']
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

    def _team_direct_page(self, room: dict, bot: str, data: dict) -> str | None:
        """The entry ID of the page that a message to a bot is about, from `about_document_id`, or None without the
        field. The value is the Library ID of a team page of this room that shows now and whose bot is the bot of the
        chat. Each other value answers 409. The caller holds the service lock."""
        from .service_base import ServiceError
        from .team_pages import team_ids
        value = data.get('about_document_id')
        if value is None:
            return None
        ids = team_ids(value)
        page = self._team_shown_page(room['room'], ids[1]) if ids and ids[0] == room['room'] else None
        if page is None or page['bot'] != bot:
            raise ServiceError(409, CHOOSE_PAGE)
        return ids[1]

    def _team_shown_page(self, room_id: str, entry: str) -> dict | None:
        """A team page that the Library of this Mac shows now, by the rules of the Library, or None. The caller holds
        the service lock."""
        from .documents import expired
        page = self.team_store.page(room_id, entry)
        return page if page is not None and not expired(page, self._now()) else None

    def _team_write_direct(self, room: dict, entry: str, member: str, agreement: str, fields: dict, text: str,
                           reply_to: str | None, bot: str | None = None, page: str | None = None) -> None:
        """Seal the box of a direct message to `member` and write its entry. `fields` are the other fields of the
        payload. A message to a bot names its `bot` entry, and the page that it is about. The caller holds the lock of
        the room, and no lock of the service."""
        plaintext = {'to': member, 'text': text}
        for name, value in (('bot', bot), ('reply_to', reply_to), ('page', page)):
            if value is not None:
                plaintext[name] = value
        secrets = team_keys.read(self.root, room['room'])
        box = self.team_helper.seal_direct(room['room'], entry, secrets['agreement_key'], room['member'], member,
                                           agreement, json.dumps(plaintext, ensure_ascii=False,
                                                                 separators=(',', ':')).encode())
        self._team_write(room, entry, False, dict(fields, box=box), tries=2,
                         direct={'member': member, 'text': text, 'reply_to': reply_to, 'bot': bot, 'page': page})

    def _team_own_direct(self, room_id: str, member: str | None, bot: str | None, entry: str,
                         deleted: bool = False) -> tuple[dict, dict, dict | None]:
        """The room, the entry and the row of a direct message that this Mac's person wrote in a chat. With `deleted`,
        also a `delete` entry of this Mac, without a row. The caller holds the lock of the room."""
        from .service_base import ServiceError
        if not entry_id(entry):
            raise ServiceError(404, GONE_MESSAGE)
        room = self._team_writable(room_id)
        with self.lock:
            existing = self.team_store.entry(room_id, entry)
            row = self.team_store.direct(room_id, entry)
        if deleted and existing and existing['kind'] == 'delete' and existing['member'] == room['member']:
            return room, existing, None
        if (existing is None or existing['kind'] != 'direct' or row is None or row['member'] != member
                or row['bot'] != bot):
            raise ServiceError(404, GONE_MESSAGE)
        if row['writer'] != room['member']:
            raise ServiceError(403, 'You can change only your own messages.')
        return room, existing, row

    def _team_direct_answer(self, room_id: str, member: str | None, bot: str | None, entry: str) -> dict:
        from .service_base import ServiceError
        with self.lock:
            room = self.team_store.room(room_id)
            row = self.team_store.direct(room_id, entry) if room else None
            if row is None or row['member'] != member or row['bot'] != bot:
                raise ServiceError(404, GONE_MESSAGE)
            return {'room': room_id, **self._team_direct_key(room_id, member, bot),
                    'message': self._team_direct_views(room, member, bot, [row])[0]}

    def _team_direct_views(self, room: dict, member: str | None, bot: str | None, rows: list[dict]) -> list[dict]:
        """The messages of a direct chat as the app gets them. `reply` quotes the message of the same chat that
        `reply_to` names, or is null when the chat does not show it. One query reads the answered messages of the
        page. A message of a chat with a bot also has `page`: null, the ID and the title of a page that the Library
        shows now, or `{"id": null, "title": null}` for a page that no longer shows. The caller holds the service
        lock."""
        from .team_pages import team_id
        answered = self.team_store.directs_of(room['room'], member,
                                              [row['reply_to'] for row in rows if row['reply_to']], bot)

        def quote(entry):
            row = answered.get(entry)
            return row and {'id': row['entry'], 'this_mac': row['writer'] == room['member'], 'text': row['text'][:PREVIEW]}

        def about(entry):
            if entry is None:
                return None
            page = self._team_shown_page(room['room'], entry)
            return {'id': team_id(room['room'], entry), 'title': page['title']} if page else {'id': None, 'title': None}
        views = [{'id': row['entry'], 'first': row['first'], 'this_mac': row['writer'] == room['member'],
                  'text': row['text'], 'reply_to': row['reply_to'], 'reply': quote(row['reply_to']),
                  'created_at': row['created_at'], 'edited_at': row['updated_at'] if row['edited'] else None}
                 for row in rows]
        if bot is not None:
            for view, row in zip(views, rows):
                view['page'] = about(row['page'])
        return views

    def _team_direct_summary(self, room: dict) -> list[dict]:
        """The direct chats of two people of a room for GET /v1/team/rooms: one item for each chat with a message that
        shows, newest chat first. A chat with a member that left stays. The caller holds the service lock."""
        return [{'member': row['member'], 'member_name': row['member_name'],
                 'unread': self.team_store.direct_unread(room['room'], row['member']),
                 'last_message': {'first': row['first'], 'this_mac': row['writer'] == room['member'],
                                  'text': row['text'][:PREVIEW], 'created_at': row['created_at']}}
                for row in self.team_store.direct_chats(room['room'])]

    def _team_bot_direct_summary(self, room: dict) -> list[dict]:
        """The direct chats with a bot of a room for GET /v1/team/rooms: one item for each chat with a message that
        shows, newest chat first. A chat with a bot that left stays. `typing` is the whole number of seconds that are
        left of the typing sign of the bot, or null. A sign shows only while the room is active and the member of the
        bot is in the member list. The caller holds the service lock."""
        from .team_pages import team_id

        def typing(row) -> int | None:
            if room['state'] != ACTIVE or self.team_store.member(room['room'], row['member']) is None:
                return None
            seconds = self.team_store.direct_typing_now(room['room'], row['member'], row['bot'])
            return None if seconds is None else math.ceil(seconds)
        return [{'bot': team_id(room['room'], row['bot']), 'bot_name': row['bot_name'],
                 'unread': self.team_store.direct_unread(room['room'], row['member'], row['bot']),
                 'last_message': {'first': row['first'], 'this_mac': row['writer'] == room['member'],
                                  'text': row['text'][:PREVIEW], 'created_at': row['created_at']},
                 'typing': typing(row)}
                for row in self.team_store.bot_direct_chats(room['room'])]
