"""The named chats of a team (docs/team-log.md, "Chats of a team").

A team has the Team chat, and the admin makes more chats with a name. Each member reads each chat. A chat is one kept
`chat` entry of the admin, and the ID of the chat is its entry ID. A rename is a new write of the entry, and a removal
is a `delete` entry over it. A message and a `typing` entry name their chat in the field `chat`. TeamStore keeps the
chats in `team_chats`.

- `POST /v1/team/rooms/<room>/chats` with `{name}`: make a chat.
- `PATCH /v1/team/rooms/<room>/chats/<id>` with `{name}`: rename it.
- `DELETE /v1/team/rooms/<room>/chats/<id>`: remove it.

Only a paired device on the admin's Mac calls these routes. The routes of the Team chat take a chat in `chat`
(team_messages.py, team_typing.py), and `GET /v1/team/rooms` gives `chats` and `can_manage_chats` (Team._team_view).
No log line of this module has the name of a chat.
"""
from __future__ import annotations

import hashlib
from secrets import token_hex

from .team_store import ACTIVE, MAX_CHAT_NAME, MAX_CHATS, entry_id

GONE_CHAT = 'This chat is no longer in the team.'
ADMIN_ONLY = 'Only the admin of the team can change its chats.'
SAME_NAME = 'This team already has a chat with this name.'
TOO_MANY = f'A team can have at most {MAX_CHATS} chats.'
NOUN = 'the name of the chat'


class TeamChats:
    """The routes of the named chats, and the chat of a request of the Team chat routes. Team mixes this class in."""

    def team_chat_create(self, room_id: str, data: dict, device: str, repeat: str = '') -> dict:
        """Make a named chat: a kept `chat` entry of the admin. With an `Idempotency-Key`, the entry ID comes from the
        room, the device and the key, as for a message. So a repeat after a write without an answer finds that write
        and makes no second chat."""
        from .team import team_errors
        name = self._team_chat_name(data)
        self._team_chat_admin(room_id, device)
        entry = hashlib.sha256(f'{room_id}:chat:{repeat}'.encode()).hexdigest()[:32] if repeat else token_hex(16)
        with team_errors(), self._team_room_lock(room_id):
            room = self._team_writable(room_id)
            with self.lock:
                if self.team_store.entry(room_id, entry) is None:
                    self._team_chat_fits(room_id, name, None)
                    write = True
                else:
                    write = False
            if write:
                self._team_write(room, entry, True, {'kind': 'chat', 'name': name}, tries=2)
        return dict(self._team_answer(room_id), chat=entry)

    def team_chat_rename(self, room_id: str, chat: str, data: dict, device: str) -> dict:
        """Rename a named chat: a new write of its entry with the next `rev`. The same name writes nothing."""
        from .team import team_errors
        name = self._team_chat_name(data)
        self._team_chat_admin(room_id, device)
        with team_errors(), self._team_room_lock(room_id):
            room = self._team_writable(room_id)
            with self.lock:
                self._team_chat_named(room_id, chat)
                write = self.team_store.chat(room_id, chat)['name'] != name
                if write:
                    self._team_chat_fits(room_id, name, chat)
            if write:
                self._team_write(room, chat, True, {'kind': 'chat', 'name': name}, tries=2)
        return dict(self._team_answer(room_id), chat=chat)

    def team_chat_delete(self, room_id: str, chat: str, device: str) -> dict:
        """Remove a named chat for each member: a `delete` entry over its entry. A chat that is already removed, for
        example after a delete without an answer, gets the same answer and writes nothing."""
        from .service_base import ServiceError
        from .team import team_errors
        self._team_chat_admin(room_id, device)
        with team_errors(), self._team_room_lock(room_id):
            room = self._team_writable(room_id)
            with self.lock:
                existing = self.team_store.entry(room_id, chat) if entry_id(chat) else None
            if existing is None or existing['member'] != room['member'] or existing['kind'] not in ('chat', 'delete'):
                raise ServiceError(404, GONE_CHAT)
            if existing['kind'] == 'chat':
                self._team_write(room, chat, False, {'kind': 'delete'}, tries=2)
        return dict(self._team_answer(room_id), chat=chat)

    # Steps

    @staticmethod
    def _team_chat_name(data: dict) -> str:
        """The name of a chat by the strict rule of a member name, with at most MAX_CHAT_NAME code points."""
        from .team import checked_name
        return checked_name(data.get('name'), NOUN, person=True, limit=MAX_CHAT_NAME)

    def _team_chat_admin(self, room_id: str, device: str) -> None:
        """Only a paired device on the admin's Mac changes the chats of an active room."""
        from .service_base import ServiceError
        from .team import PERSON_ONLY
        from .team_sync import INACTIVE
        if not device:
            raise ServiceError(403, PERSON_ONLY)
        room = self._team_room(room_id)
        if room['admin'] != room['member']:
            raise ServiceError(403, ADMIN_ONLY)
        if room['state'] != ACTIVE:
            raise ServiceError(409, INACTIVE[room['state']])

    def _team_chat_fits(self, room_id: str, name: str, chat: str | None) -> None:
        """Refuse a name that another chat of the room has, compared with casefold, and a chat over MAX_CHATS. `chat`
        is the chat of a rename, or None for a new chat. The caller holds the service lock."""
        from .service_base import ServiceError
        others = [row for row in self.team_store.chats(room_id) if row['entry'] != chat]
        if any(row['name'].casefold() == name.casefold() for row in others):
            raise ServiceError(409, SAME_NAME)
        if chat is None and len(others) >= MAX_CHATS:
            raise ServiceError(409, TOO_MANY)

    def _team_chat_named(self, room_id: str, value) -> str | None:
        """The chat of a request of the Team chat routes: None for the Team chat, or the ID of a named chat that the
        store shows. Else 404. The caller holds the service lock."""
        from .service_base import ServiceError
        if value is None:
            return None
        if not entry_id(value) or self.team_store.chat(room_id, value) is None:
            raise ServiceError(404, GONE_CHAT)
        return value

    def _team_chats_view(self, room: dict, members: list[dict]) -> list[dict]:
        """`chats` of a room view: each named chat with the fields that the room has for the Team chat. The caller holds
        the service lock."""
        return [dict(id=chat['entry'], name=chat['name'], created_at=chat['created_at'],
                     **self._team_chat_summary(room, chat['entry']),
                     typing=self._team_typing_view(room, members, chat['entry']))
                for chat in self.team_store.chats(room['room'])]
