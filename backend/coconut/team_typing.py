"""Who types in the Team chat (docs/team-log.md, "Typing").

A `typing` entry is a small entry of the log, not kept. A Mac keeps one such entry for its person in each room, and one
for each of its bots in each room, and replaces it (`rev` grows). So the relay holds one small entry for each subject,
not one for each key press. TeamStore.accept reads the entries of the other members into memory (`TeamStore.typing`),
and `GET /v1/team/rooms` gives the list in `typing`.

- A person's Mac writes the entry of its person with `on` true through `POST /v1/team/rooms/<room>/typing`. The app
  calls the route while the person changes the draft of the Team chat. The Mac writes at most one such entry in
  FLOOR_SECONDS for a room.
- A Mac for a team only writes the entry of a bot while a turn of the bot runs (BotTyping): `on` true when the turn
  starts in the gateway, again every AGAIN_SECONDS, and `on` false at the end of the turn.

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
# A Mac for a team only writes the `typing` entry of a bot again this often while the turn runs. A reader shows a bot
# for TYPING_BOT_SECONDS (team_store.py) after each read, so the row does not go between two writes.
AGAIN_SECONDS = 20
# How often BotTyping looks whether the adapter of the gateway took the turn.
SHOWN_SECONDS = 0.2
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


class BotTyping:
    """The `typing` entry of one bot of a Mac for a team only while one turn runs (team_turns.py), on a thread of its
    own, so a write never fails or delays the turn.

    The thread waits until the adapter of the gateway shows the turn (`started`). Then it writes `on` true, again every
    AGAIN_SECONDS, and `on` false after `stop`. A turn that ends before the adapter shows it writes nothing. A turn that
    gets a notice at once (no adapter) never starts this thread. A failed write gives one log line."""

    def __init__(self, service, room: str, bot: str, started):
        self.service, self.room, self.bot, self.started = service, room, bot, started
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.run, name='coconut-team-typing', daemon=True)

    def start(self) -> BotTyping:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.done.set()

    def run(self) -> None:
        while not self.started():
            if self.done.wait(SHOWN_SECONDS):
                return
        wrote = False
        while True:
            wrote = self.write(True) or wrote
            if self.done.wait(AGAIN_SECONDS):
                break
        if wrote:
            self.write(False)

    def write(self, on: bool) -> bool:
        """Write the entry while the bot still answers in the room. True when the relay took it."""
        service = self.service
        try:
            with service._team_room_lock(self.room):
                with service.lock:
                    room = service.team_store.room(self.room)
                    share = service._team_share(self.room, self.bot, None)
                if room is None or share is None:
                    return False
                service._team_typing_write(room, share, on)
            return True
        except Exception as error:
            log.warning('coconut.team_turns room %s: the typing entry of the bot %s did not reach the relay (%s).',
                        self.room[:8], self.bot, type(error).__name__)
            return False
