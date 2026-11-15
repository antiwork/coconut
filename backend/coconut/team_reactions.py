"""Reactions on the messages of the team chats (docs/team-log.md, "Reactions").

A person puts one of six emoji (team_store.REACTIONS) on a message of the Team chat, of a named chat, or of a direct
chat. Each person has one reaction on a message. The routes set it:

- `PUT /v1/team/rooms/<room>/messages/<id>/reaction` with `{emoji}`: the reaction of this Mac's person on a message of
  a team chat. An empty `emoji` takes the reaction back.
- `PUT /v1/team/rooms/<room>/directs/<chat>/messages/<id>/reaction` with `{emoji}`: the same in a direct chat
  (team_directs.py routes it).

The route sets a value, so a repeat is safe: the same emoji again writes nothing. The app takes the reaction back when
the person picks the same emoji again: it sends an empty `emoji`.

A reaction in a team chat is a `reaction` entry, not kept. In a direct chat it is a `direct` entry with a box, as a
direct message, so the other members do not learn who talks with whom. A Mac derives the entry ID from the message,
so a change of the reaction is the next `rev` of the same entry. A reaction changes no unread count and no read
position, notifies nothing, and starts no turn of a bot. A Mac for a team only has no person, so it answers 409.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from . import team_keys
from .team_messages import GONE_MESSAGE
from .team_store import REACTIONS, entry_id

# The labels of the entry IDs of a reaction (reaction_entry).
REACTION_LABEL = b'coconut room reaction'
DIRECT_REACTION_LABEL = b'coconut room direct reaction'
CHOOSE = 'Choose one of these reactions: ' + ' '.join(REACTIONS) + '.'
# The answer of a Mac for a team only.
TEAM_ONLY = 'This Mac runs Coconut for a team only, so nobody reacts here.'


def reaction_entry(token: str, room: str, message: str, direct: bool = False) -> str:
    """The entry ID of the reaction of this Mac's person on a message: from the room and the entry ID of the message,
    so a change and a repeat write the same entry. The member token is the key of the HMAC, as for a `typing` entry, so
    another member cannot take the ID first. A reaction in a direct chat has its own label."""
    label = DIRECT_REACTION_LABEL if direct else REACTION_LABEL
    return hmac.new(token.encode(), label + bytes.fromhex(room) + bytes.fromhex(message), hashlib.sha256).hexdigest()[:32]


def reaction_emoji(value) -> str:
    """The emoji of a request: one of REACTIONS, or an empty text that takes the reaction back."""
    from .service_base import ServiceError
    if value != '' and value not in REACTIONS:
        raise ServiceError(400, CHOOSE)
    return value


class TeamReactions:
    """The routes of the reactions. Team mixes this class in."""

    def team_react(self, room_id: str, entry: str, data: dict) -> dict:
        """Set the reaction of this Mac's person on a message of a team chat, and answer `{room, message}` with the new
        `reactions` of the message. The message must show on this Mac: a message that the store does not have, that
        its author deleted, that the admin erased, or whose chat the admin removed answers 404."""
        from .service_base import ServiceError
        from .team import team_errors
        if self._team_bot_only:
            raise ServiceError(409, TEAM_ONLY)
        emoji = reaction_emoji(data.get('emoji'))
        if not entry_id(entry):
            raise ServiceError(404, GONE_MESSAGE)
        self._team_room(room_id)
        with team_errors(), self._team_room_lock(room_id):
            room = self._team_writable(room_id)
            with self.lock:
                if self.team_store.message(room_id, entry) is None:
                    raise ServiceError(404, GONE_MESSAGE)
                same = self.team_store.own_reaction(room_id, entry, room['member']) == emoji
            if not same:
                token = team_keys.read(self.root, room_id)['token']
                self._team_write(room, reaction_entry(token, room_id, entry), False,
                                 {'kind': 'reaction', 'message': entry, 'emoji': emoji}, tries=2)
        return self._team_message_answer(room_id, entry)

    def team_direct_react(self, room_id: str, chat: str, entry: str, data: dict) -> dict:
        """Set the reaction of this Mac's person on a message of a direct chat, and answer `{room, member, message}`,
        or `{room, bot, message}` for a chat with a bot. The box goes to the other member of the chat, so a write needs
        the rules of a direct message: a member or a bot that can no longer get one answers 409. The same emoji again
        writes nothing, also then."""
        from .service_base import ServiceError
        from .team import team_errors
        emoji = reaction_emoji(data.get('emoji'))
        member, bot = self._team_direct_chat(room_id, chat)
        if not entry_id(entry):
            raise ServiceError(404, GONE_MESSAGE)
        with team_errors(), self._team_room_lock(room_id):
            room = self._team_writable(room_id)
            with self.lock:
                if bot is not None:
                    member = self.team_store.bot_member(room_id, bot)
                row = self.team_store.direct(room_id, entry)
                if row is None or row['member'] != member or row['bot'] != bot:
                    raise ServiceError(404, GONE_MESSAGE)
                same = self.team_store.own_reaction(room_id, entry, room['member'], member, bot) == emoji
                agreement = None if same else self._team_direct_reachable(room, member, bot)
            if not same:
                plaintext = {'to': member, 'message': entry, 'emoji': emoji}
                if bot is not None:
                    plaintext['bot'] = bot
                secrets = team_keys.read(self.root, room_id)
                reaction_id = reaction_entry(secrets['token'], room_id, entry, direct=True)
                box = self.team_helper.seal_direct(room_id, reaction_id, secrets['agreement_key'], room['member'], member,
                                                   agreement, json.dumps(plaintext, ensure_ascii=False,
                                                                         separators=(',', ':')).encode())
                self._team_write(room, reaction_id, False, {'kind': 'direct', 'box': box}, tries=2,
                                 reaction={'message': entry, 'emoji': emoji, 'peer': member, 'bot': bot})
        return self._team_direct_answer(room_id, member, bot, entry)
