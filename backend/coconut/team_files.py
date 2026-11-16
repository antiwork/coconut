"""Files in the messages of the team chats (docs/team-log.md, "Files").

A person adds a file to a message of the Team chat or of a named chat. The routes:

- `POST /v1/team/rooms/<room>/files` with `{name, content, type?, width?, height?}`: keep the bytes as a draft on this
  Mac. Nothing goes to the relay.
- `DELETE /v1/team/rooms/<room>/files/<id>`: remove an own draft.
- `POST /v1/team/rooms/<room>/messages` with `files`: the IDs of the drafts. TeamMessages.team_send writes each piece
  of each file as a `file` entry, then the `message` entry with the items of the files (_team_write_pieces).
- `GET /v1/team/rooms/<room>/files/<id>`: the bytes of a complete file of a message that shows on this Mac.

A piece is a `file` entry, not kept, with at most CHUNK bytes. The writer derives its entry ID from the file and the
index, so a repeat writes the same entry. The relay refuses each write of a member over 512 MiB of entries that are
not kept, also a plain message. So the Mac sends files only up to FILE_BUDGET in 90 days, and keeps the rest of the
space for messages. A Mac for a team only has no person, so it answers 409. A direct chat takes no file in this version.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
from secrets import token_hex
from typing import NamedTuple

from . import team_keys
from .team_relay import TeamRelayError
from .team_store import (ACTIVE, CHUNK, MAX_FILE, MAX_FILE_NAME, MAX_FILES, MAX_SIDE, entry_id, file_name, media_type,
                         whole_number)

# The label of the entry ID of a piece (file_entry).
FILE_LABEL = b'coconut room file'
# The most bytes of files that one member sends in one room in SPACE_SECONDS: the relay keeps an entry that is not kept
# for 90 days, and it holds 512 MiB of such entries of one member. A piece of CHUNK bytes needs a body of about 4/3 of
# that, so 320 MiB of files use about 430 MiB, and the rest stays free for the messages.
FILE_BUDGET = 320 * 1024 * 1024
SPACE_SECONDS = 90 * 86400
# A draft that no message used goes this long after its upload.
DRAFT_SECONDS = 86400
TEAM_ONLY = 'This Mac runs Coconut for a team only, so nobody sends a file here.'
FILE_SIZE = 'A file must have between 1 byte and 4 MB.'
SPACE_FULL = ('Your space for files in this team is full. The space of a file is free again 90 days after you sent '
              'it.')
GONE_DRAFT = 'This file is no longer on this Mac. Add it again.'
GONE_FILE = 'This file is no longer in the chat.'
NOT_COMPLETE = 'This file is not on this Mac yet. Try again later.'


class FileContent(NamedTuple):
    """The answer of the download route: server.py sends the bytes, not JSON."""
    content: bytes
    name: str
    type: str


def file_entry(token: str, room: str, file: str, index: int) -> str:
    """The entry ID of a piece: from the room, the file and the index, so a repeat writes the same entry. The member
    token is the key of the HMAC, as for a reaction (team_reactions.reaction_entry), so another member cannot take the
    ID first."""
    message = FILE_LABEL + bytes.fromhex(room) + bytes.fromhex(file) + index.to_bytes(4, 'big')
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()[:32]


def file_view(item: dict) -> dict:
    """What the app gets for a draft: the item without its hash."""
    return {name: item[name] for name in ('id', 'name', 'type', 'size', 'width', 'height')}


class TeamFiles:
    """The routes of the files. Team mixes this class in."""

    def team_file_upload(self, room_id: str, data: dict) -> dict:
        """Keep a file as a draft of this Mac's person, and answer `{room, file}`. The draft goes when a message uses
        it, or DRAFT_SECONDS after its upload: each upload removes the old drafts of each room. It also removes the
        old pieces that no message names (TeamStore.sweep_pieces)."""
        from .service_base import ServiceError
        from .team_sync import INACTIVE
        if self._team_bot_only:
            raise ServiceError(409, TEAM_ONLY)
        room = self._team_room(room_id)
        name, kind, width, height = (data.get(field) for field in ('name', 'type', 'width', 'height'))
        if not file_name(name):
            raise ServiceError(400, f'Give the file a name of 1 to {MAX_FILE_NAME} characters, without / or \\.')
        if kind is not None and not media_type(kind):
            raise ServiceError(400, 'Give a media type of at most 100 characters, for example image/png, or none.')
        if (width, height) != (None, None) and not (whole_number(width, 1, MAX_SIDE)
                                                     and whole_number(height, 1, MAX_SIDE)):
            raise ServiceError(400, f'Give the width and the height of an image from 1 to {MAX_SIDE:,}, or neither.')
        content = data.get('content')
        try:
            content = base64.b64decode(content, validate=True) if isinstance(content, str) else None
        except ValueError:
            content = None
        if content is None:
            raise ServiceError(400, 'Send the file as base64.')
        if not 0 < len(content) <= MAX_FILE:
            raise ServiceError(413, FILE_SIZE)
        if room['state'] != ACTIVE:
            raise ServiceError(409, INACTIVE[room['state']])
        draft = {'id': token_hex(16), 'name': name, 'type': kind, 'size': len(content),
                 'hash': hashlib.sha256(content).hexdigest(), 'width': width, 'height': height}
        with self.transaction():
            self.team_store.drop_old_drafts(DRAFT_SECONDS)
            self.team_store.sweep_pieces()
            self.team_store.add_draft(room_id, draft, content)
        return {'room': room_id, 'file': file_view(draft)}

    def team_file_drop(self, room_id: str, file: str) -> dict:
        """Remove a draft of this Mac, and answer `{room, file, deleted: true}`. A draft that this Mac does not have
        answers 404."""
        from .service_base import ServiceError
        self._team_room(room_id)
        with self.transaction():
            if not entry_id(file) or not self.team_store.drop_draft(room_id, file):
                raise ServiceError(404, GONE_DRAFT)
        return {'room': room_id, 'file': file, 'deleted': True}

    def team_file_content(self, room_id: str, file: str) -> FileContent:
        """The bytes of a complete file of a message that shows on this Mac. A file that this Mac does not know, or
        whose message no longer shows, answers 404. A file whose pieces did not all come answers 409."""
        from .service_base import ServiceError
        self._team_room(room_id)
        if not entry_id(file):
            raise ServiceError(404, GONE_FILE)
        with self.lock:
            found = self.team_store.shown_file(room_id, file)
            if found is None:
                raise ServiceError(404, GONE_FILE)
            message, item = found
            content = self.team_store.file_bytes(room_id, message['member'], item)
        if content is None:
            raise ServiceError(409, NOT_COMPLETE)
        return FileContent(content, item['name'], item['type'] or 'application/octet-stream')

    # Steps of TeamMessages.team_send

    @staticmethod
    def _team_draft_ids(value) -> list[str]:
        """The `files` of a send: a list of 1 to MAX_FILES draft IDs, each one time. An empty list without them. The
        app sends only the IDs of its own drafts, so a person never sees these texts."""
        from .service_base import ServiceError
        if value is None:
            return []
        if not isinstance(value, list) or not 0 < len(value) <= MAX_FILES:
            raise ServiceError(400, f'Add 1 to {MAX_FILES} files to a message.')
        if not all(map(entry_id, value)) or len(set(value)) != len(value):
            raise ServiceError(400, GONE_DRAFT)
        return value

    def _team_send_drafts(self, room: dict, ids: list[str]) -> list[dict]:
        """The drafts of a send, in the order of `ids`, with their bytes. A draft that this Mac does not have answers
        400. A send over FILE_BUDGET answers 413: the files that this member wrote in the room in the last
        SPACE_SECONDS, and the new ones. The pieces of the new files that a send without an answer wrote already do
        not count two times. The caller holds the service lock."""
        from .service_base import ServiceError
        drafts = self.team_store.drafts(room['room'], ids)
        if len(drafts) != len(ids):
            raise ServiceError(400, GONE_DRAFT)
        since = int((self.team_store.clock() - SPACE_SECONDS) * 1000)
        used = self.team_store.sent_bytes(room['room'], room['member'], since, ids)
        if used + sum(draft['size'] for draft in drafts.values()) > FILE_BUDGET:
            raise ServiceError(413, SPACE_FULL)
        return [drafts[draft] for draft in ids]

    def _team_write_pieces(self, room: dict, drafts: list[dict]) -> list[dict]:
        """Write each piece of each draft as a `file` entry, in order, and return the items of the message. A piece
        that the store has as an own `file` entry, from a send without an answer, is not written again. The caller
        holds the lock of the room."""
        token = team_keys.read(self.root, room['room'])['token']
        items = []
        for draft in drafts:
            for index in range(-(-draft['size'] // CHUNK)):
                entry = file_entry(token, room['room'], draft['id'], index)
                with self.lock:
                    existing = self.team_store.entry(room['room'], entry)
                if existing is None or existing['kind'] != 'file':
                    piece = draft['data'][index * CHUNK:(index + 1) * CHUNK]
                    self._team_write(room, entry, False, {'kind': 'file', 'file': draft['id'], 'index': index,
                                                          'data': base64.b64encode(piece).decode()}, tries=2)
            items.append({name: draft[name] for name in ('id', 'name', 'type', 'size', 'hash', 'width', 'height')
                          if draft[name] is not None})
        return items

    def _team_drop_drafts(self, room_id: str, drafts: list[dict]) -> None:
        with self.transaction():
            for draft in drafts:
                self.team_store.drop_draft(room_id, draft['id'])

    @contextmanager
    def _team_file_space(self, files: bool):
        """Give a person the text of a full space when the relay answers 507 to a send with files."""
        from .service_base import ServiceError
        try:
            yield
        except TeamRelayError as error:
            if files and error.status == 507:
                raise ServiceError(413, SPACE_FULL) from None
            raise
