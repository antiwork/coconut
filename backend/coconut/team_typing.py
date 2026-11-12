"""Who types in the Team chat (docs/team-log.md, "Typing").

A `typing` entry is a small entry of the log, not kept. A Mac keeps one such entry for its person in each room, and one
for each of its bots in each room, and replaces it (`rev` grows). So the relay holds one small entry for each subject,
not one for each key press. TeamStore.accept reads the entries of the other members into memory (`TeamStore.typing`),
and `GET /v1/team/rooms` gives the list in `typing`.

- A person's Mac writes the entry of its person with `on` true through `POST /v1/team/rooms/<room>/typing`. The app
  calls the route while the person changes the draft of the Team chat. The Mac writes at most one such entry in
  FLOOR_SECONDS for a room.

A direct chat shows no typing: a `typing` entry would tell each member that two people talk. No log line of this module
has a name.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import threading
import time

from . import team_keys
from .team_pages import team_id
from .team_store import ACTIVE

log = logging.getLogger(__name__)

# A person's Mac writes the `typing` entry of its person at most this often in a room. A call inside that time writes
# nothing.
FLOOR_SECONDS = 4
# The label of the entry ID of a `typing` entry (typing_entry).
TYPING_LABEL = b'coconut room typing'
# The answer of a Mac for a team only: no person types there.
TEAM_ONLY = 'This Mac runs Coconut for a team only, so nobody types here.'
# The clock of FLOOR_SECONDS. A test replaces it, so it needs no wait.
clock = time.monotonic


def typing_entry(token: str, room: str, bot: str | None = None) -> str:
    """The entry ID of the `typing` entry of the person of this Mac, or of one of its bots (the entry ID of its `bot`
    entry), in a room. The same subject keeps one entry ID, also after a restart, so the relay holds one entry.

    The member token is the key of the HMAC, as for a `remove` entry. Another member cannot compute the ID and take it
    first, because the relay gives an entry ID to one member only."""
    message = TYPING_LABEL + bytes.fromhex(room) + (bytes.fromhex(bot) if bot else b'')
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()[:32]


class TeamTyping:
    """The route of the person, the list of `GET /v1/team/rooms`, and the writes. Team mixes this class in."""

    def _init_team_typing(self):
        # The monotonic time of the last `typing` write of the person in each room, for FLOOR_SECONDS.
        self._team_typing_sent: dict[str, float] = {}
        self._team_typing_lock = threading.Lock()

    def team_typing(self, room_id: str, device: str) -> dict:
        """The person of this Mac types in the Team chat of the room: write the `typing` entry of the person with `on`
        true, at most once in FLOOR_SECONDS. A call inside that time answers at once and writes nothing. A failed write
        is an error, as a failed send of a message is, and the app ignores it."""
        from .service_base import ServiceError
        from .team import PERSON_ONLY, team_errors
        if self._team_bot_only:
            raise ServiceError(409, TEAM_ONLY)
        if not device:
            raise ServiceError(403, PERSON_ONLY)
        self._team_room(room_id)
        now = clock()
        with self._team_typing_lock:
            last = self._team_typing_sent.get(room_id)
            if last is not None and now - last < FLOOR_SECONDS:
                return {'room': room_id}
            self._team_typing_sent[room_id] = now
        try:
            with team_errors(), self._team_room_lock(room_id):
                self._team_typing_write(self._team_writable(room_id), None, True)
        except BaseException:
            # A write that failed does not hold back the next call.
            with self._team_typing_lock:
                if self._team_typing_sent.get(room_id) == now:
                    del self._team_typing_sent[room_id]
            raise
        return {'room': room_id}

    def _team_typing_write(self, room: dict, bot: str | None, on: bool) -> None:
        """Write the `typing` entry of the person of this Mac, or of the bot with the `bot` entry `bot`, with the next
        `rev`. The caller holds the lock of the room."""
        token = team_keys.read(self.root, room['room'])['token']
        fields = {'kind': 'typing', 'on': on}
        if bot is not None:
            fields['bot'] = bot
        self._team_write(room, typing_entry(token, room['room'], bot), False, fields)

    def _team_typing_view(self, room: dict, members: list[dict]) -> list[dict]:
        """`typing` of a room view: each other subject that types now, the earliest start first. `members` are the
        members of the view. A subject shows only while its member is in the list, and a bot only while its `bot` entry
        of the same member is valid. `seconds` is the time that is left, so the app hides the subject without a request.
        The caller holds the service lock."""
        if room['state'] != ACTIVE:
            return []
        names = {member['member']: member['name'] for member in members if not member['this_mac']}
        bots = {(bot['member'], bot['entry']): bot['name'] for bot in self.team_store.bots(room['room'])}
        items = []
        for member, bot, seconds in self.team_store.typing_now(room['room']):
            if member not in names or (bot is not None and (member, bot) not in bots):
                continue
            items.append({'member': member, 'name': names[member],
                          'bot': team_id(room['room'], bot) if bot else None,
                          'bot_name': bots[(member, bot)] if bot else None, 'seconds': math.ceil(seconds)})
        return items

