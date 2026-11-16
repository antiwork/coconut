"""Files in the messages of the team chats (docs/team-log.md, "Files").

A person adds a file to a message of the Team chat or of a named chat. The routes:

- `POST /v1/team/rooms/<room>/files` with `{name, content, type?, width?, height?}`: keep the bytes as a draft on this
  Mac. Nothing goes to the relay.
- `DELETE /v1/team/rooms/<room>/files/<id>`: remove an own draft.
- `POST /v1/team/rooms/<room>/messages` with `files`: the IDs of the drafts. TeamMessages.team_send writes each piece
  of each file as a `file` entry, then the `message` entry with the items of the files (_team_write_pieces).
- `GET /v1/team/rooms/<room>/messages/<message>/files/<id>`: the bytes of a complete file of a message that shows on
  this Mac. The route names the message, because two members can name the same file ID, each with own pieces.

A piece is a `file` entry, not kept, with at most CHUNK bytes. The writer derives its entry ID from the file and the
index, so a repeat writes the same entry. The relay refuses each write of a member over 512 MiB of entries that are
not kept, also a plain message. So the Mac sends files only up to FILE_BUDGET in 90 days, and keeps the rest of the
space for messages. A Mac for a team only has no person, so it answers 409. A direct chat takes no file in this version.

Only the writer of a piece can replace it, so each Mac takes its own pieces off the relay with a `delete` entry over
each one. The relay then frees their space, each other Mac removes their bytes, and the count of the space no longer
counts them. Two steps do this: the delete of an own message with files (_team_drop_pieces), and a pass of the sync of
a room (_team_clean_pieces) for each other own piece that no message names.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
from secrets import token_hex
import time
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
# The pass of the old own pieces (_team_clean_pieces) runs at most one time in CLEAN_SECONDS for each room, and writes at
# most CLEAN_LIMIT `delete` entries in one run. The relay takes 300 writes a minute of one member.
CLEAN_SECONDS = 3600
CLEAN_LIMIT = 48

log = logging.getLogger(__name__)


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

    def team_file_content(self, room_id: str, message: str, file: str) -> FileContent:
        """The bytes of a complete file of one message that shows on this Mac. A message that does not show, or that
        does not name the file, answers 404. A file whose pieces did not all come answers 409. Only the pieces of the
        member of that message count, so the bytes are always the file of that message."""
        from .service_base import ServiceError
        self._team_room(room_id)
        if not entry_id(message) or not entry_id(file):
            raise ServiceError(404, GONE_FILE)
        with self.lock:
            found = self.team_store.message_file(room_id, message, file)
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
        drafts = self.team_store.drafts(room['room'], ids, DRAFT_SECONDS)
        if len(drafts) != len(ids):
            raise ServiceError(400, GONE_DRAFT)
        since = int((self.team_store.clock() - SPACE_SECONDS) * 1000)
        used = self.team_store.sent_bytes(room['room'], room['member'], since, ids)
        if used + sum(draft['size'] for draft in drafts.values()) > FILE_BUDGET:
            raise ServiceError(413, SPACE_FULL)
        return [drafts[draft] for draft in ids]

    def _team_write_pieces(self, room: dict, drafts: list[dict]) -> list[dict]:
        """Write each piece of each draft as a `file` entry, in order, and return the items of the message. A piece
        whose bytes this Mac has with the size of the draft, from a send without an answer, is not written again. The
        `file` entry alone is not enough: the sweep or the delete of a message can have removed its bytes. Then the
        Mac writes the piece again with the next `rev`, so each Mac gets the bytes again. The caller holds the lock of
        the room."""
        token = team_keys.read(self.root, room['room'])['token']
        items = []
        for draft in drafts:
            for index in range(-(-draft['size'] // CHUNK)):
                entry = file_entry(token, room['room'], draft['id'], index)
                piece = draft['data'][index * CHUNK:(index + 1) * CHUNK]
                with self.lock:
                    existing = self.team_store.entry(room['room'], entry)
                    stored = self.team_store.piece_size(room['room'], entry)
                if existing is None or existing['kind'] != 'file' or stored != len(piece):
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

    # The pieces of this Mac that no message names

    def _team_drop_pieces(self, room: dict, message: dict) -> None:
        """Write a `delete` entry over each own piece of each file of a message that this Mac's person deleted. The
        caller wrote the `delete` over the message first, so the chat changed already. A file that another message of
        this Mac still names keeps its pieces. A piece that is no `file` entry any more, for example after a repeat,
        needs no write.

        A failure here does not fail the delete of the message: the person sees the chat change, and the pass of the
        sync (_team_clean_pieces) writes the rest later. The caller holds the lock of the room."""
        from .team_store import files_fields
        from .team_sync import ROOM_ERRORS
        files = files_fields(json.loads(message['payload']).get('files')) or []
        if not files:
            return
        try:
            token = team_keys.read(self.root, room['room'])['token']
            for item in files:
                with self.lock:
                    if self.team_store.file_named(room['room'], room['member'], item['id']):
                        continue
                for index in range(-(-item['size'] // CHUNK)):
                    entry = file_entry(token, room['room'], item['id'], index)
                    with self.lock:
                        piece = self.team_store.entry(room['room'], entry)
                    if piece is not None and piece['kind'] == 'file' and piece['member'] == room['member']:
                        self._team_write(room, entry, False, {'kind': 'delete'}, tries=2)
        except ROOM_ERRORS as error:
            log.warning('coconut.team_log room %s: the pieces of a deleted message wait for the next pass. %s',
                        room['room'][:8], error)

    def _team_clean_pieces(self, room_id: str) -> None:
        """Write a `delete` entry over each own piece that no message of this Mac names and that is older than
        DRAFT_SECONDS, at most CLEAN_LIMIT in one run, and at most one run in CLEAN_SECONDS for each room. A send takes
        no older draft, so no later message can name such a piece. A piece older than SPACE_SECONDS is gone from the
        relay already, so the pass leaves it.

        The pass finishes a delete of a message that stopped half way, and it takes off the pieces of a send that
        failed, of a message that the admin erased, and of a chat that the admin removed. The time of the last run is
        in memory only, so a start of the service runs the pass again. A failure ends the run, and the next run, one hour
        later, tries again. A run that does not start sets no time.

        The store must have each own message that the relay has, else a piece of a shown message looks like an orphan,
        and its `delete` cannot be taken back. So the sync calls this only after it read the room to its end, never
        while the service stops, and with the lock of the room. A pending body of this member (a body that waits for
        its room key) can be such a message, so a room with one has no run."""
        from .team_sync import ROOM_ERRORS
        now = time.monotonic()
        last = self._team_cleaned.get(room_id)
        if last is not None and now - last < CLEAN_SECONDS:
            return
        with self.lock:
            room = self.team_store.room(room_id)
            if room is None or room['state'] != ACTIVE or self.team_store.pending_of(room_id, room['member']):
                return
            clock = self.team_store.clock()
            orphans = self.team_store.orphan_pieces(room_id, room['member'], int((clock - SPACE_SECONDS) * 1000),
                                                    int((clock - DRAFT_SECONDS) * 1000), CLEAN_LIMIT)
        self._team_cleaned[room_id] = now
        try:
            for entry in orphans:
                if self.stopping.is_set():
                    return
                self._team_write(room, entry, False, {'kind': 'delete'})
        except ROOM_ERRORS as error:
            log.warning('coconut.team_log room %s: the old pieces wait for the next pass. %s', room_id[:8], error)
