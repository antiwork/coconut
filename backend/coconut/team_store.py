"""The copy of the team log on this Mac: rooms, members and entries in coconut.sqlite3 (docs/team-log.md).

Each table has the room ID in its key, because a Mac can be in more than one room.

- `team_rooms`: one row for each room of this Mac: its relay, its name, this Mac's member ID, the admin, and how far
  the sync read (`seq`). `profile` is what this Mac writes about itself in its `member` entry. `notifications` is the
  choice of the person of this Mac: a new message of the room notifies the phone. It never goes into the log.
- `team_entries`: the newest accepted write of each entry ID, with the payload as its member wrote it.
- `team_members`: the members that the app shows. It is a view of `team_entries`: the newest valid `member` entry
  of each member.
- `team_pending`: bodies that this Mac cannot open yet. It keeps the bytes and tries again later.
- `team_shares`: the bots of this Mac that author a page in the room, each with the entry ID of its `bot` entry. On a
  person's Mac a row comes with the first shared page of the bot and goes with the last one. On a Mac that runs
  Coconut for a team only, a tool adds and removes the row.
- `team_page_shares`: the documents that the person of this Mac shares with the room. A Mac that runs Coconut for a
  team only has no rows here: the audience of a page names the team.
- `team_page_entries`: the entry ID of each document of this Mac that the room has or had as a `page` entry.
- `team_bots` and `team_pages`: views of `team_entries` for the Library: the valid `bot` entries and the `page`
  entries of the other members. The Library list reads them and parses no payload. A page row also keeps the pin of
  this Mac and the time of the first write that this Mac saw.
- `team_messages`: a view of `team_entries` for the Team chat: one row for each valid `message` entry, in the order
  of `first`.
- `team_names`: the last name that the store showed for each member and each bot. It stays after a `delete` entry,
  so a message of a member that left keeps a name, and a message of a bot can still name a valid `bot` entry.
- `team_reads`: the read position of this Mac in the chat of each room: the `first` of the newest message that the
  person read.
- `team_triggers`: on a team-only Mac only, the messages that start a turn of a bot of this Mac, in the order in which
  this Mac accepted them (team_turns.py). `started` marks a trigger whose turn began.
- `team_directs`: the direct messages that this Mac shows: one row for each `direct` entry whose box opens for this
  Mac, with `member`, the other member of the chat. No query of the Team chat reads this table.
- `team_direct_reads`: the read position of this Mac in each direct chat: one row for each room and other member.
- `team_marks`: the lasting marks of this Mac. `direct` means that an app of the person knows direct messages.
- `team_direct_waits`: the `direct` entries that the sync could not try with the key that they need yet, because the
  `member` entry of that key came later in the read. The end of the read tries each one again, then removes it.

`TeamStore.accept` holds the "Rules for a Mac that reads an entry" of the contract. The secrets of a room are not
here: team_keys.py keeps them in a private file. The caller holds the transaction of each write.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import logging
import re
import time

from .team_relay import HEX32, number

log = logging.getLogger(__name__)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS team_rooms (room TEXT PRIMARY KEY, relay TEXT NOT NULL, name TEXT NOT NULL,
    member TEXT NOT NULL, admin TEXT NOT NULL, state TEXT NOT NULL, epoch INTEGER NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0, profile TEXT NOT NULL, created_at TEXT NOT NULL,
    notifications INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS team_entries (room TEXT NOT NULL, id TEXT NOT NULL, seq INTEGER NOT NULL,
    first INTEGER NOT NULL, member TEXT NOT NULL, kind TEXT NOT NULL, rev INTEGER NOT NULL, at INTEGER NOT NULL,
    kept INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (room, id));
CREATE TABLE IF NOT EXISTS team_members (room TEXT NOT NULL, member TEXT NOT NULL, name TEXT NOT NULL,
    avatar TEXT NOT NULL, color TEXT NOT NULL, agreement TEXT NOT NULL, entry TEXT NOT NULL,
    bot_only INTEGER NOT NULL DEFAULT 0, direct INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (room, member));
CREATE TABLE IF NOT EXISTS team_pending (room TEXT NOT NULL, id TEXT NOT NULL, seq INTEGER NOT NULL,
    first INTEGER NOT NULL, member TEXT NOT NULL, at INTEGER NOT NULL, kept INTEGER NOT NULL, body BLOB NOT NULL,
    reason TEXT NOT NULL, PRIMARY KEY (room, id));
CREATE TABLE IF NOT EXISTS team_shares (room TEXT NOT NULL, bot TEXT NOT NULL, entry TEXT NOT NULL,
    PRIMARY KEY (room, bot));
CREATE TABLE IF NOT EXISTS team_page_shares (room TEXT NOT NULL, document TEXT NOT NULL,
    PRIMARY KEY (room, document));
CREATE INDEX IF NOT EXISTS team_page_shares_document ON team_page_shares (document);
CREATE TABLE IF NOT EXISTS team_page_entries (room TEXT NOT NULL, document TEXT NOT NULL, entry TEXT NOT NULL,
    PRIMARY KEY (room, document));
CREATE TABLE IF NOT EXISTS team_bots (room TEXT NOT NULL, entry TEXT NOT NULL, member TEXT NOT NULL,
    name TEXT NOT NULL, avatar TEXT NOT NULL, color TEXT NOT NULL, open INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (room, entry));
CREATE TABLE IF NOT EXISTS team_pages (room TEXT NOT NULL, entry TEXT NOT NULL, member TEXT NOT NULL,
    valid INTEGER NOT NULL, bot TEXT, version INTEGER, key TEXT, title TEXT, format TEXT, summary TEXT, size INTEGER, rev INTEGER,
    expires_at TEXT, created_at TEXT, updated_at TEXT, pinned INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (room, entry));
CREATE TABLE IF NOT EXISTS team_messages (room TEXT NOT NULL, entry TEXT NOT NULL, first INTEGER NOT NULL,
    member TEXT NOT NULL, bot TEXT, text TEXT NOT NULL, reply_to TEXT, mentions TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, edited INTEGER NOT NULL, PRIMARY KEY (room, entry));
CREATE INDEX IF NOT EXISTS team_messages_order ON team_messages (room, first);
CREATE TABLE IF NOT EXISTS team_names (room TEXT NOT NULL, entry TEXT NOT NULL, member TEXT NOT NULL,
    kind TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (room, entry));
CREATE INDEX IF NOT EXISTS team_names_member ON team_names (room, member, kind);
CREATE TABLE IF NOT EXISTS team_reads (room TEXT PRIMARY KEY, first INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS team_triggers (number INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
    bot TEXT NOT NULL, entry TEXT NOT NULL, started INTEGER NOT NULL DEFAULT 0, UNIQUE (room, bot, entry));
CREATE TABLE IF NOT EXISTS team_directs (room TEXT NOT NULL, entry TEXT NOT NULL, first INTEGER NOT NULL,
    writer TEXT NOT NULL, member TEXT NOT NULL, text TEXT NOT NULL, reply_to TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, edited INTEGER NOT NULL, PRIMARY KEY (room, entry));
CREATE INDEX IF NOT EXISTS team_directs_order ON team_directs (room, member, first);
CREATE TABLE IF NOT EXISTS team_direct_reads (room TEXT NOT NULL, member TEXT NOT NULL, first INTEGER NOT NULL,
    PRIMARY KEY (room, member));
CREATE TABLE IF NOT EXISTS team_marks (mark TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS team_direct_waits (room TEXT NOT NULL, entry TEXT NOT NULL, PRIMARY KEY (room, entry));
-- A store from before team_names gets the names that it shows now. A later accept keeps them current.
INSERT OR IGNORE INTO team_names (room, entry, member, kind, name)
    SELECT room, entry, member, 'member', name FROM team_members;
INSERT OR IGNORE INTO team_names (room, entry, member, kind, name)
    SELECT room, entry, member, 'bot', name FROM team_bots;
'''
TABLES = ('team_rooms', 'team_entries', 'team_members', 'team_pending', 'team_shares', 'team_page_shares',
          'team_page_entries', 'team_bots', 'team_pages', 'team_messages', 'team_names', 'team_reads', 'team_triggers',
          'team_directs', 'team_direct_reads', 'team_direct_waits')
ROOM_FIELDS = ('room', 'relay', 'name', 'member', 'admin', 'state', 'epoch', 'seq', 'profile', 'created_at',
               'notifications')
ENTRY_FIELDS = ('id', 'seq', 'first', 'member', 'kind', 'rev', 'at', 'kept', 'payload')
MEMBER_FIELDS = ('member', 'name', 'avatar', 'color', 'agreement', 'entry', 'bot_only', 'direct')
PENDING_FIELDS = ('id', 'seq', 'first', 'member', 'at', 'kept', 'body', 'reason')
PAGE_FIELDS = ('key', 'title', 'format', 'summary', 'size', 'rev', 'expires_at', 'created_at', 'updated_at', 'pinned')
# What the Library of the phone gets for a team page and a team bot. TeamStore.library reads these rows.
LIBRARY_PAGE = ('room', 'entry', 'bot', 'team', *PAGE_FIELDS)
LIBRARY_BOT = ('room', 'entry', 'name', 'avatar', 'color', 'team', 'member', 'member_name')
TEAM_BOT = ('entry', 'member', 'name', 'avatar', 'color', 'open')
# What the chat of the phone gets for a message. TeamStore.messages reads these rows.
MESSAGE_FIELDS = ('entry', 'first', 'member', 'member_name', 'bot', 'bot_name', 'text', 'reply_to', 'mentions',
                  'created_at', 'updated_at', 'edited')
# A message shows only when its `bot` is null or names a `bot` entry of the same member that the store showed once.
# The bot entry can come after the message, because the relay keeps only its newest write, so the check runs at read
# time. team_names keeps the bot after a `delete`, so the history of a bot that the team no longer has stays.
SHOWN_MESSAGE = '''(m.bot IS NULL OR EXISTS (SELECT 1 FROM team_names b WHERE b.room=m.room AND b.entry=m.bot
    AND b.kind='bot' AND b.member=m.member))'''
# The event of each transaction that changes the chat of a room: a message, an edit, a removal, or the read position.
# GET /v1/updates then has `team_messages_changed`.
MESSAGES_EVENT = 'team_messages.updated'
# What a direct chat of the phone gets for a message. TeamStore.directs reads these rows. `member` is the other
# member of the chat, and `writer` is the member that wrote the message.
DIRECT_FIELDS = ('entry', 'first', 'writer', 'member', 'member_name', 'text', 'reply_to', 'created_at', 'updated_at',
                 'edited')
# The event of each transaction that changes a direct chat: `{"team": <room>, "member": <other member>}`. GET /v1/updates
# then has `team_directs_changed`.
DIRECTS_EVENT = 'team_directs.updated'
# The mark in `team_marks` after an app of the person asked for direct messages.
DIRECT_MARK = 'direct'
# The limits of a `message` entry, in Unicode code points and in entry IDs.
MAX_TEXT = 16000
MAX_MENTIONS = 16
# The unread count of a room stops here, so the count reads at most this many rows.
MAX_UNREAD = 99
# A message starts a turn of a bot, or notifies the phone of a person, only while it is younger than this. An older
# message is history, for example the log that a Mac reads after it joins.
TRIGGER_SECONDS = 3600
# At most this many triggers wait for one bot in one room. A member can mention the bot again later.
MAX_WAITING = 8
TRIGGER_FIELDS = ('number', 'room', 'bot', 'entry', 'started')
# The end date of a page as documents.end_time writes it.
PAGE_END = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ')

# The states of a room on this Mac.
JOINING = 'joining'  # The Mac has its keys, and the relay did not answer the join yet.
ACTIVE = 'active'    # The Mac is a member and copies the log.
REMOVED = 'removed'  # The relay no longer knows the token of this Mac. The copy of the log stays.
GONE = 'gone'        # The relay no longer has the room. The copy of the log stays.

# What `accept` did with an entry.
STORED = 'stored'    # The entry is in the store now.
PENDING = 'pending'  # The Mac keeps the bytes and tries again later.
SAME = 'same'        # The store already has this write.
SKIPPED = 'skipped'  # The entry is from before this Mac joined, and it cannot get the room key of that epoch.
DROPPED = 'dropped'  # The body does not open, or its payload is not the payload of an entry.
REFUSED = 'refused'  # The body opens, and a rule of the contract refuses the entry.

# Only the admin writes these kinds (rule 4).
ADMIN_KINDS = ('remove', 'key')
MAX_KIND = 64
# The fields of a `member` entry, with the most Unicode code points of each.
MEMBER_LIMITS = {'name': 80, 'avatar': 80, 'color': 20}
# A public agreement key: 32 bytes as unpadded base64url.
AGREEMENT = re.compile(r'[A-Za-z0-9_-]{43}')


def unicode_text(value) -> bool:
    """True when a value is a str that UTF-8 can encode.

    JSON can carry a lone surrogate such as "\\ud800", and json.loads gives it. The binding of SQLite cannot store
    such a str, so a field with one does not fit.
    """
    if not isinstance(value, str):
        return False
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def read_payload(data) -> tuple[str, dict] | None:
    """The text and the fields of a payload: one JSON object in UTF-8 with a `kind` and a `rev` from 1 to
    MAX_NUMBER. Else None."""
    try:
        text = bytes(data).decode('utf-8')
        fields = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(fields, dict):
        return None
    kind, rev = fields.get('kind'), fields.get('rev')
    if not unicode_text(kind) or not 0 < len(kind) <= MAX_KIND or not number(rev, 1):
        return None
    # The text is the bytes of the payload, which are valid UTF-8, so SQLite can store it.
    return text, fields


def profile_fields(fields: dict) -> dict | None:
    """The name, avatar and color of a `member` or a `bot` payload, or None when a field does not fit."""
    result = {}
    for name, limit in MEMBER_LIMITS.items():
        value = fields.get(name, '')
        if not unicode_text(value) or len(value) > limit:
            return None
        result[name] = value
    return result if result['name'] else None


def bot_fields(fields: dict) -> dict | None:
    """The profile of a `bot` payload and its mark `open`, or None when a field of the profile does not fit.

    `open` is true only for the JSON value true: a team-only Mac writes it for a bot that answers each member of the
    team. A missing field and each other value are false, so a value that does not fit hides no bot. A reader trusts
    the mark only for the list of the bots that a member can name in the chat.
    """
    result = profile_fields(fields)
    return dict(result, open=fields.get('open') is True) if result else None


def member_fields(fields: dict) -> dict | None:
    """The name, avatar, color and agreement key of a `member` payload, or None when a field does not fit. Also the
    mark `bot_only`: true only for the JSON value true, which a Mac without a person writes. A missing field and each
    other value are false, so a value that does not fit hides no member. The mark `direct` (this member reads direct
    messages) follows the same rule."""
    result = profile_fields(fields)
    agreement = fields.get('agreement')
    if not result or not isinstance(agreement, str) or not AGREEMENT.fullmatch(agreement):
        return None
    # One key has one text: the unused bits of the last character are zero.
    if base64.urlsafe_b64encode(base64.urlsafe_b64decode(agreement + '=')).decode().rstrip('=') != agreement:
        return None
    return dict(result, agreement=agreement, bot_only=fields.get('bot_only') is True,
                direct=fields.get('direct') is True)


def page_fields(fields: dict) -> dict | None:
    """What the Library shows of a `page` payload, or None when the page breaks a rule of a local save.

    The writer checked the page too, but this Mac does not trust that check. The rules are the ones of
    documents.page_to_save, and the text must be valid Unicode text before a rule or SQLite reads it.
    """
    from .documents import page_to_save
    from .service_base import ServiceError
    bot, ends, summary = fields.get('bot'), fields.get('expires_at'), fields.get('summary')
    if not isinstance(bot, str) or not HEX32.fullmatch(bot):
        return None
    if ends is not None and not page_end(ends):
        return None
    if not all(unicode_text(fields.get(name)) for name in ('key', 'title', 'format', 'content')):
        return None
    if summary is not None and not unicode_text(summary):
        return None
    try:
        key, title, document_format, content, summary = page_to_save(fields)
    except ServiceError:
        return None
    return dict(bot=bot, key=key, title=title, format=document_format, summary=(summary or '').strip() or None,
                size=len(content.encode()), expires_at=ends)


def page_end(value) -> bool:
    """True when an end date of a page is a real UTC time as documents.end_time writes it. The pattern alone takes,
    for example, a 13th month, and strptime alone takes a month of one digit."""
    if not isinstance(value, str) or not PAGE_END.fullmatch(value):
        return False
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        return False
    return True


def relay_time(at: int) -> str | None:
    """The `at` of the relay (milliseconds since 1970) as the UTC text of a local document, or None when the time
    is past the year 9999. The relay sends a whole number up to 2^53 - 1, and Python has no date that late."""
    try:
        moment = datetime.fromtimestamp(at / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return moment.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def entry_id(value) -> bool:
    """True when a value of JSON is an entry ID: 32 lowercase hex characters."""
    return isinstance(value, str) and HEX32.fullmatch(value) is not None


def message_fields(fields: dict) -> dict | None:
    """What the chat shows of a `message` payload, or None when a field does not fit.

    `text` has 1 to MAX_TEXT code points of valid Unicode text. `reply_to` and `bot` are entry IDs, and `mentions` is
    a list of at most MAX_MENTIONS entry IDs. A reader takes null as an absent field, so a later writer can write
    either. Fields that this version does not know stay in the payload, and nothing reads them.
    """
    text, reply_to, mentions, bot = (fields.get(name) for name in ('text', 'reply_to', 'mentions', 'bot'))
    if not unicode_text(text) or not 0 < len(text) <= MAX_TEXT:
        return None
    if any(value is not None and not entry_id(value) for value in (reply_to, bot)):
        return None
    mentions = [] if mentions is None else mentions
    if not isinstance(mentions, list) or len(mentions) > MAX_MENTIONS or not all(map(entry_id, mentions)):
        return None
    return dict(text=text, reply_to=reply_to, mentions=list(dict.fromkeys(mentions)), bot=bot)


def direct_fields(plaintext, to: str) -> dict | None:
    """What a direct chat shows of the plaintext of a box, or None when it does not fit.

    The plaintext is one JSON object in UTF-8. `to` must name the member that the caller expects: this Mac for a
    message of another member, or the other member for an own message. `text` has 1 to MAX_TEXT code points of valid
    Unicode text, and `reply_to` is an entry ID or absent. A reader takes null as an absent field and ignores a field
    that it does not know.
    """
    try:
        fields = json.loads(bytes(plaintext).decode('utf-8'))
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(fields, dict) or not isinstance(fields.get('to'), str) or fields['to'] != to:
        return None
    text, reply_to = fields.get('text'), fields.get('reply_to')
    if not unicode_text(text) or not 0 < len(text) <= MAX_TEXT:
        return None
    if reply_to is not None and not entry_id(reply_to):
        return None
    return dict(text=text, reply_to=reply_to)


SHOWN = {'member': member_fields, 'bot': bot_fields, 'page': page_fields, 'message': message_fields}


def read_entry(opened: dict) -> dict:
    """The first part of the reading rules for one entry: the work that needs no database.

    It reads the payload (rule 2), and for a `member`, `bot`, `page` or `message` entry it checks the fields that the
    store shows. The page rules run an HTML parser on up to 512 KiB that another member wrote, so a caller runs this
    part before its transaction and without the lock of the service. A page that is slow to check then slows only the
    sync thread. The result is `opened` with `read` (the text and the fields of the payload, or None) and `shown`
    (the checked fields, or None). TeamStore.accept is the second part.
    """
    if opened.get('error'):
        return opened
    payload = read_payload(opened.get('payload'))
    check = SHOWN.get(payload[1]['kind']) if payload else None
    shown = None
    if check:
        try:
            shown = check(payload[1])
        except Exception:
            # A check that fails is the rule of an entry that does not fit: the entry stays in the store and shows
            # nothing, and its bytes stay for a later version of Coconut.
            log.exception('coconut.team_log: the check of a %s entry failed, so the entry shows nothing.',
                          payload[1]['kind'])
    return dict(opened, read=payload, shown=shown)


class TeamStore:
    def __init__(self, db):
        self.db = db
        # The rooms whose Library rows `accept` changed, the rooms whose members or team bots it changed, and the rooms
        # whose chat it changed. TeamSync tells the phone and empties each set.
        self.touched: set[str] = set()
        self.changed: set[str] = set()
        self.chatted: set[str] = set()
        # Only a team-only Mac sets `answers` (team_turns.py): its bots answer in the team chat. `triggered` has the
        # rooms with a new trigger, and TeamSync wakes the turns. The clock is the wall clock, as the `at` of the relay.
        self.answers = False
        self.triggered: set[str] = set()
        # The fresh messages of the other members, by room (_note_fresh). TeamSync gives them to the push code of a
        # person's Mac and empties the list. A team-only Mac has no push, and the list goes nowhere.
        self.fresh: dict[str, list[str]] = {}
        # The direct chats that `accept` changed, as (room, other member), and the fresh direct messages of the other
        # members by room (_note_direct_fresh). TeamSync tells the phone and empties both.
        self.directed: set[tuple[str, str]] = set()
        self.fresh_directs: dict[str, list[str]] = {}
        self.clock = time.time

    def migrate(self) -> None:
        """Add the columns that a store of an older version lacks. A `bot` row of that version reads as not open
        until its next write, a `member` row as a person, and a room has its notifications on."""
        if 'open' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_bots)')}:
            self.db.execute('ALTER TABLE team_bots ADD COLUMN open INTEGER NOT NULL DEFAULT 0')
        if 'bot_only' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_members)')}:
            self.db.execute('ALTER TABLE team_members ADD COLUMN bot_only INTEGER NOT NULL DEFAULT 0')
        if 'notifications' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_rooms)')}:
            self.db.execute('ALTER TABLE team_rooms ADD COLUMN notifications INTEGER NOT NULL DEFAULT 1')
        # A `member` row of an older version reads as a member without direct messages until its next write.
        if 'direct' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_members)')}:
            self.db.execute('ALTER TABLE team_members ADD COLUMN direct INTEGER NOT NULL DEFAULT 0')

    def _rows(self, fields, query, values=()) -> list[dict]:
        return [dict(zip(fields, row)) for row in self.db.execute(query, values).fetchall()]

    # Rooms

    def rooms(self) -> list[dict]:
        return self._rows(ROOM_FIELDS, f'SELECT {", ".join(ROOM_FIELDS)} FROM team_rooms ORDER BY created_at, room')

    def room(self, room: str) -> dict | None:
        rows = self._rows(ROOM_FIELDS, f'SELECT {", ".join(ROOM_FIELDS)} FROM team_rooms WHERE room=?', (room,))
        return rows[0] if rows else None

    def add_room(self, **fields) -> None:
        self.db.execute(f'INSERT INTO team_rooms ({", ".join(fields)}) VALUES ({", ".join("?" * len(fields))})',
                        tuple(fields.values()))

    def update_room(self, room: str, **fields) -> None:
        self.db.execute(f'UPDATE team_rooms SET {", ".join(name + "=?" for name in fields)} WHERE room=?',
                        (*fields.values(), room))

    def remove_room(self, room: str) -> None:
        """Delete the room with its members, its entries and its pending bodies."""
        for table in TABLES:
            self.db.execute(f'DELETE FROM {table} WHERE room=?', (room,))

    # Entries and members

    def entry(self, room: str, entry: str) -> dict | None:
        rows = self._rows(ENTRY_FIELDS, f'SELECT {", ".join(ENTRY_FIELDS)} FROM team_entries WHERE room=? AND id=?',
                          (room, entry))
        return rows[0] if rows else None

    def members(self, room: str) -> list[dict]:
        """The members of the room in the order of their first `member` entry."""
        return self._rows(MEMBER_FIELDS, f'''SELECT {", ".join("m." + name for name in MEMBER_FIELDS)}
            FROM team_members m JOIN team_entries e ON e.room=m.room AND e.id=m.entry
            WHERE m.room=? ORDER BY e.first, m.member''', (room,))

    def member(self, room: str, member: str) -> dict | None:
        rows = self._rows(MEMBER_FIELDS, f'SELECT {", ".join(MEMBER_FIELDS)} FROM team_members WHERE room=? AND member=?',
                          (room, member))
        return rows[0] if rows else None

    def counts(self, room: str) -> tuple[int, int]:
        """How many entries the store has for the room, and how many bodies wait."""
        entries = self.db.execute('SELECT COUNT(*) FROM team_entries WHERE room=?', (room,)).fetchone()[0]
        pending = self.db.execute('SELECT COUNT(*) FROM team_pending WHERE room=?', (room,)).fetchone()[0]
        return entries, pending

    def pending(self, room: str, after: int = 0, limit: int = 32) -> list[dict]:
        """The next bodies that wait, in the order of the log. A body can have 1 MiB, so a caller reads a few."""
        return self._rows(PENDING_FIELDS, f'''SELECT {", ".join(PENDING_FIELDS)} FROM team_pending
            WHERE room=? AND seq>? ORDER BY seq LIMIT ?''', (room, after, limit))

    def drop_pending(self, room: str, entry: str) -> None:
        self.db.execute('DELETE FROM team_pending WHERE room=? AND id=?', (room, entry))

    def accept(self, room: dict, entry: dict, opened: dict, first_epoch: int = 0) -> tuple[str, str | None]:
        """Apply the "Rules for a Mac that reads an entry" of docs/team-log.md to one entry.

        `room` is the row of the room. `entry` has the fields that the relay keeps: seq, first, id, member, at, kept,
        and body as bytes. `opened` is the answer of the helper for the body after read_entry: `epoch`, `read` and
        `shown`, or `error`. read_entry does each check of the text, so this part only applies the rules that need
        the store, and a caller holds its transaction for a short time.
        `first_epoch` is the first epoch for which this Mac has a room key. The result is what happened to the
        entry, and the reason when the entry did not go into the store.
        """
        error = opened.get('error')
        # Rule 1. A later version can have another form, so the Mac keeps the bytes until an update of Coconut.
        if error == 'unknown_version':
            return self._keep(room, entry, 'version')
        if error == 'unknown_epoch':
            # The Mac has no room key for the epoch. A key of an epoch before its first one never comes: a member
            # that joins later cannot read earlier epochs. The key of a later epoch comes in a `key` entry.
            epoch = opened.get('epoch')
            if type(epoch) is int and epoch < first_epoch:
                return SKIPPED, 'epoch'
            return self._keep(room, entry, 'epoch')
        # Rule 2. The seal or the signature failed, or the bytes are not a body.
        if error:
            return DROPPED, str(error)
        if opened['read'] is None:
            return DROPPED, 'payload'
        text, fields = opened['read']
        shown = opened['shown']
        # Rule 3. Only the member of an entry replaces it, and only with a higher `rev`.
        existing = self.entry(room['room'], entry['id'])
        if existing:
            if existing['seq'] == entry['seq']:
                if existing['at'] != entry['at']:
                    self._relay_time(room['room'], existing, entry['at'])
                return SAME, None
            if existing['member'] != entry['member']:
                return REFUSED, 'member'
            if fields['rev'] <= existing['rev']:
                return REFUSED, 'rev'
        # Rule 4. The admin is the member that the welcome names.
        if fields['kind'] in ADMIN_KINDS and entry['member'] != room['admin']:
            return REFUSED, 'admin'
        # Rule 5. An entry of an unknown kind goes into the store as each other entry, and nothing reads it.
        self.db.execute('''INSERT INTO team_entries (room, id, seq, first, member, kind, rev, at, kept, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, id) DO UPDATE SET seq=excluded.seq,
            first=excluded.first, kind=excluded.kind, rev=excluded.rev, at=excluded.at, kept=excluded.kept,
            payload=excluded.payload''',
                        (room['room'], entry['id'], entry['seq'], entry['first'], entry['member'], fields['kind'],
                         fields['rev'], entry['at'], int(bool(entry['kept'])), text))
        # The relay keeps only the newest write of an entry ID, so an earlier body that waits is gone there.
        self.db.execute('DELETE FROM team_pending WHERE room=? AND id=? AND seq<=?',
                        (room['room'], entry['id'], entry['seq']))
        kinds = {fields['kind'], existing['kind'] if existing else None}
        other = entry['member'] != room['member']
        if 'message' in kinds and self._show_message(room['room'], entry, fields['rev'],
                                                     shown if fields['kind'] == 'message' else None):
            self.chatted.add(room['room'])
        # A direct message shows only with `direct`: what TeamSync found in its box before the transaction. A write
        # without it, for example a box that no longer opens, or a `delete` over it, removes the message.
        if 'direct' in kinds:
            for member in self._show_direct(room['room'], entry, fields['rev'],
                                            opened.get('direct') if fields['kind'] == 'direct' else None):
                self.directed.add((room['room'], member))
            # A new write or a `delete` ends the wait of an earlier write. A new write that must wait waits again.
            self.db.execute('DELETE FROM team_direct_waits WHERE room=? AND entry=?', (room['room'], entry['id']))
            if fields['kind'] == 'direct' and opened.get('wait'):
                self.db.execute('INSERT INTO team_direct_waits (room, entry) VALUES (?, ?)', (room['room'], entry['id']))
        # A trigger is the first write of a message of another member that this Mac sees. A later write is an edit.
        if self.answers and existing is None and other and fields['kind'] == 'message' and fields['rev'] == 1 and shown:
            self._queue_triggers(room, entry, shown)
        if 'member' in kinds:
            self.show_member(room['room'], entry['member'], (entry['id'], shown) if fields['kind'] == 'member' else None)
            if not other and fields['kind'] == 'member':
                if shown:
                    self._follow_profile(room['room'], shown)
                # The chat of a Mac that joins starts at its own `member` entry, so the history before the join does
                # not count as unread. A later write of the entry keeps its `first`, and the position only moves on.
                self.db.execute('INSERT OR IGNORE INTO team_reads (room, first) VALUES (?, ?)',
                                (room['room'], entry['first']))
            # The Library shows a bot only while its member is in the list, and it names that member.
            if other and self.db.execute('SELECT 1 FROM team_bots WHERE room=? AND member=? LIMIT 1',
                                         (room['room'], entry['member'])).fetchone():
                self.touched.add(room['room'])
        # The Team screen shows the bots of the other members. It takes the own bots from the shares of this Mac.
        if other and 'bot' in kinds:
            self.changed.add(room['room'])
        # The rows of the own entries keep the form that the publish step compares (see `written`). The Library
        # leaves them out: the Mac that wrote a page has the document already.
        if kinds & {'bot', 'page'}:
            if fields['kind'] == 'bot':
                self._show_bot(room['room'], entry, fields if not other else shown)
            elif fields['kind'] == 'page':
                self._show_page(room['room'], entry, fields, shown, other)
            else:
                for table in ('team_bots', 'team_pages'):
                    self.db.execute(f'DELETE FROM {table} WHERE room=? AND entry=?', (room['room'], entry['id']))
            if other:
                self.touched.add(room['room'])
        # The last step, so a failure of an earlier step leaves no note of an entry that the savepoint takes back.
        if existing is None and fields['kind'] == 'message' and fields['rev'] == 1 and shown:
            self._note_fresh(room, entry, shown)
        if existing is None and fields['kind'] == 'direct' and fields['rev'] == 1 and other and opened.get('direct'):
            self._note_direct_fresh(room, entry)
        return STORED, None

    def _follow_profile(self, room: str, shown: dict) -> None:
        """Make the saved profile of this Mac equal to its newest accepted `member` entry.

        The log is the one source of truth for what the team sees. A write of a new name can reach the relay without
        an answer, and the sync then reads it back. Without this step, the saved profile would keep the old name, and
        a later change of one field or a new `member` entry after a loss (TeamSync._team_announce) would write the
        old name again. The entry ID and the agreement key stay: this Mac made them.
        """
        row = self.db.execute('SELECT profile FROM team_rooms WHERE room=?', (room,)).fetchone()
        profile = json.loads(row[0])
        followed = dict(profile, **{name: shown[name] for name in MEMBER_LIMITS})
        if followed != profile:
            self.update_room(room, profile=json.dumps(followed))

    def _keep(self, room: dict, entry: dict, reason: str) -> tuple[str, str | None]:
        """Keep the bytes of a body that the Mac cannot open yet. One entry ID keeps its newest write."""
        existing = self.entry(room['room'], entry['id'])
        if existing and existing['seq'] >= entry['seq']:
            return SAME, None
        self.db.execute('''INSERT INTO team_pending (room, id, seq, first, member, at, kept, body, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, id) DO UPDATE SET seq=excluded.seq,
            first=excluded.first, member=excluded.member, at=excluded.at, kept=excluded.kept, body=excluded.body,
            reason=excluded.reason WHERE excluded.seq >= team_pending.seq''',
                        (room['room'], entry['id'], entry['seq'], entry['first'], entry['member'], entry['at'],
                         int(bool(entry['kept'])), entry['body'], reason))
        return PENDING, reason

    def show_member(self, room: str, member: str, known: tuple[str, dict | None] | None = None) -> None:
        """Set the row of a member from its `member` entries: the newest one with fields that fit, or no row.

        A member has one `member` entry. When it wrote more, the one with the highest `seq` counts, so each Mac
        shows the same member. A `delete` over that entry takes the member out of the list. `known` is the entry ID
        and the fields that read_entry found for the entry of the caller, so the store does not parse it again.
        A row that changes puts the room into `changed`, so a new write of the same profile adds no event.
        """
        before = self.member(room, member)
        self._show_member(room, member, known)
        if self.member(room, member) != before:
            self.changed.add(room)

    def _show_member(self, room: str, member: str, known: tuple[str, dict | None] | None) -> None:
        rows = self.db.execute("SELECT id FROM team_entries WHERE room=? AND member=? AND kind='member' "
                               'ORDER BY seq DESC', (room, member)).fetchall()
        for (entry,) in rows:
            if known and known[0] == entry:
                fields = known[1]
            else:
                payload = self.db.execute('SELECT payload FROM team_entries WHERE room=? AND id=?', (room, entry))
                fields = member_fields(json.loads(payload.fetchone()[0]))
            if fields:
                self.db.execute('''INSERT INTO team_members (room, member, name, avatar, color, agreement, entry,
                    bot_only, direct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, member) DO UPDATE SET
                    name=excluded.name, avatar=excluded.avatar, color=excluded.color, agreement=excluded.agreement,
                    entry=excluded.entry, bot_only=excluded.bot_only, direct=excluded.direct''',
                                (room, member, fields['name'], fields['avatar'], fields['color'], fields['agreement'], entry,
                                 int(fields.get('bot_only') is True), int(fields.get('direct') is True)))
                # A member has one name: the one of the entry that the list shows.
                if self.db.execute("DELETE FROM team_names WHERE room=? AND member=? AND kind='member' AND entry!=?",
                                   (room, member, entry)).rowcount:
                    self._names_changed(room, member)
                self._name(room, entry, member, 'member', fields['name'])
                return
        self.db.execute('DELETE FROM team_members WHERE room=? AND member=?', (room, member))

    def _show_bot(self, room: str, entry: dict, profile: dict | None) -> None:
        """Set the row of a `bot` entry: the profile that read_entry found valid for another member, or the profile
        of an own entry as this Mac wrote it. No row when a field does not fit."""
        self.db.execute('DELETE FROM team_pages WHERE room=? AND entry=?', (room, entry['id']))
        if profile is None:
            self.db.execute('DELETE FROM team_bots WHERE room=? AND entry=?', (room, entry['id']))
            return
        self.db.execute('''INSERT INTO team_bots (room, entry, member, name, avatar, color, open)
            VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, entry) DO UPDATE SET member=excluded.member,
            name=excluded.name, avatar=excluded.avatar, color=excluded.color, open=excluded.open''',
                        (room, entry['id'], entry['member'], profile['name'], profile['avatar'], profile['color'],
                         int(profile.get('open') is True)))
        self._name(room, entry['id'], entry['member'], 'bot', profile['name'])

    def _name(self, room: str, entry: str, member: str, kind: str, name: str) -> None:
        """Keep the name that the store shows for a member or a bot. No `delete` entry removes it (see team_names).

        The upsert changes a row only when a value is new, so its row count tells whether the row is new or changed.
        A same name changes nothing and adds no event.
        """
        if self.db.execute('''INSERT INTO team_names (room, entry, member, kind, name) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (room, entry) DO UPDATE SET member=excluded.member, kind=excluded.kind, name=excluded.name
            WHERE team_names.member!=excluded.member OR team_names.kind!=excluded.kind
                OR team_names.name!=excluded.name''', (room, entry, member, kind, name)).rowcount:
            self._names_changed(room, member if kind == 'member' else None)

    def _names_changed(self, room: str, member: str | None = None) -> None:
        """A new or changed name can change the chat: a message of a bot shows once its `bot` entry comes, and the
        names of the messages, of the quotes and of `last_message` follow team_names. So the chat of a room with
        messages gets an event. A direct chat names its other member, so a new name of `member` changes that chat."""
        if self.db.execute('SELECT 1 FROM team_messages WHERE room=? LIMIT 1', (room,)).fetchone():
            self.chatted.add(room)
        if member and self.db.execute('SELECT 1 FROM team_directs WHERE room=? AND member=? LIMIT 1',
                                      (room, member)).fetchone():
            self.directed.add((room, member))

    def _relay_time(self, room: str, existing: dict, at: int) -> None:
        """Take the time of the relay for a write that the store has with another time.

        This Mac stores its own write at once, with the clock of this Mac, because the answer of a write has no time.
        The read of the same write gives the time of the relay. Each Mac shows the time of the relay in the chat, so
        the clock of one Mac does not change what the others see.
        """
        self.db.execute('UPDATE team_entries SET at=? WHERE room=? AND id=?', (at, room, existing['id']))
        old, new = relay_time(existing['at']), relay_time(at)
        if existing['kind'] not in ('message', 'direct') or new is None:
            return
        table = 'team_messages' if existing['kind'] == 'message' else 'team_directs'
        if not self.db.execute(f'''UPDATE {table} SET created_at=CASE WHEN created_at=? THEN ? ELSE created_at END,
                updated_at=? WHERE room=? AND entry=? AND updated_at=?''',
                               (old, new, new, room, existing['id'], old)).rowcount:
            return
        if existing['kind'] == 'message':
            self.chatted.add(room)
        else:
            (member,) = self.db.execute('SELECT member FROM team_directs WHERE room=? AND entry=?',
                                        (room, existing['id'])).fetchone()
            self.directed.add((room, member))

    def _show_message(self, room: str, entry: dict, rev: int, message: dict | None) -> bool:
        """Set the row of a `message` entry, or remove it. `message` is what read_entry found valid, or None for a
        message that does not fit and for a `delete` entry over a message. True when the chat changed.

        The times come from the relay: `created_at` is the `at` of the first write that this Mac saw, and `updated_at`
        the `at` of this write. A message is edited when its `rev` is over 1, so each Mac gives the same answer, also a
        Mac that only saw the newest write.
        """
        at = relay_time(entry['at'])
        if message is None or at is None:
            removed = self.db.execute('DELETE FROM team_messages WHERE room=? AND entry=?', (room, entry['id']))
            return removed.rowcount > 0
        self.db.execute('''INSERT INTO team_messages (room, entry, first, member, bot, text, reply_to, mentions,
            created_at, updated_at, edited) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, entry) DO UPDATE
            SET first=excluded.first, bot=excluded.bot, text=excluded.text, reply_to=excluded.reply_to,
            mentions=excluded.mentions, updated_at=excluded.updated_at, edited=excluded.edited''',
                        (room, entry['id'], entry['first'], entry['member'], message['bot'], message['text'],
                         message['reply_to'], json.dumps(message['mentions']), at, at, int(rev > 1)))
        return True

    def _show_direct(self, room: str, entry: dict, rev: int, direct: dict | None) -> set[str]:
        """Set the row of a `direct` entry, or remove it. `direct` is what the box gave this Mac: the other member of
        the chat, the text and the reply. It is None for a box that does not open for this Mac or does not fit, and for
        a `delete` entry over a direct message. Return the other members whose chat changed.

        The times follow the rule of a message: `created_at` is the `at` of the first write that this Mac saw, and
        `updated_at` the `at` of this write. A message is edited when its `rev` is over 1.
        """
        before = self.db.execute('SELECT member FROM team_directs WHERE room=? AND entry=?', (room, entry['id'])).fetchone()
        changed = {before[0]} if before else set()
        at = relay_time(entry['at'])
        if direct is None or at is None:
            self.db.execute('DELETE FROM team_directs WHERE room=? AND entry=?', (room, entry['id']))
            return changed
        self.db.execute('''INSERT INTO team_directs (room, entry, first, writer, member, text, reply_to, created_at,
            updated_at, edited) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, entry) DO UPDATE SET
            first=excluded.first, writer=excluded.writer, member=excluded.member, text=excluded.text,
            reply_to=excluded.reply_to, updated_at=excluded.updated_at, edited=excluded.edited''',
                        (room, entry['id'], entry['first'], entry['member'], direct['member'], direct['text'],
                         direct['reply_to'], at, at, int(rev > 1)))
        return changed | {direct['member']}

    def _show_page(self, room: str, entry: dict, fields: dict, page: dict | None, other: bool) -> None:
        """Set the row of a `page` entry. `page` is what read_entry found valid, or None.

        A page that breaks a rule keeps a row that is not valid, so the pin of this Mac and the time of the first
        write stay. The relay gives the times: `updated_at` is the `at` of this write. The row of an own page keeps
        the bot and the version that this Mac wrote, also when the page is not valid, so that the publish step does
        not write it again in each pass.
        """
        at = relay_time(entry['at'])
        if page is None or at is None:
            page = dict.fromkeys(('bot', 'key', 'title', 'format', 'summary', 'size', 'expires_at'))
        valid = page['bot'] is not None
        bot, version = (page['bot'], None) if other else (fields.get('bot'), fields.get('version'))
        # This Mac wrote the own payload, but its row keeps only values that SQLite stores as they are.
        bot = bot if isinstance(bot, str) and HEX32.fullmatch(bot) else None
        version = version if number(version) else None
        self.db.execute('DELETE FROM team_bots WHERE room=? AND entry=?', (room, entry['id']))
        self.db.execute('''INSERT INTO team_pages (room, entry, member, valid, bot, version, key, title, format,
            summary, size, rev, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (room, entry) DO UPDATE SET member=excluded.member, valid=excluded.valid, bot=excluded.bot,
            version=excluded.version, key=excluded.key, title=excluded.title, format=excluded.format,
            summary=excluded.summary, size=excluded.size, rev=excluded.rev, expires_at=excluded.expires_at,
            updated_at=excluded.updated_at, created_at=COALESCE(team_pages.created_at, excluded.created_at)''',
                        (room, entry['id'], entry['member'], int(valid), bot, version, page['key'], page['title'],
                         page['format'], page['summary'], page['size'], fields['rev'], page['expires_at'], at, at))

    def _note_fresh(self, room: dict, entry: dict, message: dict) -> None:
        """Note a message that can notify the phone of the person (docs/team-log.md, "Notifications").

        The caller checked that this is the first write of a valid message that this Mac sees. The message also shows
        now, counts as unread by the rule of `unread`, is under TRIGGER_SECONDS old, and is after the read position of
        the room. A room has a read position only after the own `member` entry of this Mac, so the history that a Mac
        reads after a join notifies nothing.
        """
        room_id = room['room']
        if entry['member'] == room['member'] and message['bot'] is None:
            return
        if self.clock() * 1000 - entry['at'] >= TRIGGER_SECONDS * 1000:
            return
        position = self.db.execute('SELECT first FROM team_reads WHERE room=?', (room_id,)).fetchone()
        if position is None or entry['first'] <= position[0] or self.message(room_id, entry['id']) is None:
            return
        self.fresh.setdefault(room_id, []).append(entry['id'])

    def _note_direct_fresh(self, room: dict, entry: dict) -> None:
        """Note a direct message of another member that can notify the phone of the person (docs/team-log.md,
        "Notifications of a direct message").

        The caller checked that this is the first write of a direct message of another member that this Mac sees, and
        that its box opened for this Mac. The message also shows now, is under TRIGGER_SECONDS old, and is after the
        read position of its chat.
        """
        if self.clock() * 1000 - entry['at'] >= TRIGGER_SECONDS * 1000:
            return
        row = self.direct(room['room'], entry['id'])
        if row is None or entry['first'] <= self.direct_read_position(room['room'], row['member']):
            return
        self.fresh_directs.setdefault(room['room'], []).append(entry['id'])

    # The triggers of the bots of a team-only Mac

    def _queue_triggers(self, room: dict, entry: dict, message: dict) -> None:
        """Queue a turn for each bot of this Mac that the message names or answers (docs/team-log.md, "A bot answers").

        The message has no `bot`, is under TRIGGER_SECONDS old, and its member is in the member list. It names the
        `bot` entry of a bot in `mentions`, or its `reply_to` is a message of that bot. The bot is shared with the room,
        is not archived, and has its chat connected (`chat` of its record). The signature of the entry proves its
        member, and no other field of a payload decides here. A bot with MAX_WAITING triggers that wait skips the
        message, with a log line and no text of the message.
        """
        room_id = room['room']
        if message['bot'] is not None or self.clock() * 1000 - entry['at'] >= TRIGGER_SECONDS * 1000:
            return
        if self.member(room_id, entry['member']) is None:
            return
        answered = None
        if message['reply_to']:
            row = self.db.execute('SELECT bot FROM team_messages WHERE room=? AND entry=? AND member=?',
                                  (room_id, message['reply_to'], room['member'])).fetchone()
            answered = row[0] if row else None
        for bot, share in self.answering(room_id):
            if share not in message['mentions'] and share != answered:
                continue
            if self.db.execute('SELECT COUNT(*) FROM team_triggers WHERE room=? AND bot=? AND started=0',
                               (room_id, bot)).fetchone()[0] >= MAX_WAITING:
                log.warning('coconut.team_log room %s: the bot %s has %d messages that wait, so it skips the entry %s.',
                            room_id[:8], bot, MAX_WAITING, entry['id'])
                continue
            if self.db.execute('INSERT OR IGNORE INTO team_triggers (room, bot, entry) VALUES (?, ?, ?)',
                               (room_id, bot, entry['id'])).rowcount:
                self.triggered.add(room_id)

    def answering(self, room: str) -> list[tuple[str, str]]:
        """The bots of this Mac that answer in the chat of the room, each with the entry ID of its `bot` entry: shared
        with the room, not archived, and with their chat connected. The `bots` table holds the records."""
        return self.db.execute('''SELECT s.bot, s.entry FROM team_shares s JOIN bots b ON b.id=s.bot
            WHERE s.room=? AND json_extract(b.data, '$.chat')=1 AND COALESCE(json_extract(b.data, '$.archived'), 0)=0
            ORDER BY s.bot''', (room,)).fetchall()

    def next_trigger(self, room: str, bot: str) -> dict | None:
        """The oldest trigger of the bot in the room whose turn did not begin."""
        rows = self._rows(TRIGGER_FIELDS, f'''SELECT {", ".join(TRIGGER_FIELDS)} FROM team_triggers
            WHERE room=? AND bot=? AND started=0 ORDER BY number LIMIT 1''', (room, bot))
        return rows[0] if rows else None

    def waiting(self) -> list[tuple[str, str]]:
        """Each room and bot with a trigger whose turn did not begin, in the order of their oldest trigger."""
        return [tuple(row) for row in self.db.execute('''SELECT room, bot FROM team_triggers WHERE started=0
            GROUP BY room, bot ORDER BY MIN(number)''')]

    def started_triggers(self) -> list[dict]:
        """The triggers whose turn began and did not end: the service stopped in their turn."""
        return self._rows(TRIGGER_FIELDS, f'SELECT {", ".join(TRIGGER_FIELDS)} FROM team_triggers WHERE started=1 '
                                          'ORDER BY number')

    def start_trigger(self, number: int) -> None:
        self.db.execute('UPDATE team_triggers SET started=1 WHERE number=?', (number,))

    def drop_trigger(self, number: int) -> None:
        self.db.execute('DELETE FROM team_triggers WHERE number=?', (number,))

    def context(self, room: str, bot: str, first: int, limit: int) -> list[dict]:
        """The shown messages before `first` and after the newest message of the bot (its `bot` entry ID), at most
        `limit`, oldest first."""
        last = self.db.execute('SELECT MAX(first) FROM team_messages WHERE room=? AND bot=?', (room, bot)).fetchone()[0]
        rows = self._messages('m.first<? AND m.first>? ORDER BY m.first DESC LIMIT ?', (room, first, last or 0, limit))
        return rows[::-1]

    # What this Mac shares

    def shares(self, room: str) -> dict[str, str]:
        """The bots of this Mac that author a page in the room, each with the entry ID of its `bot` entry."""
        return dict(self.db.execute('SELECT bot, entry FROM team_shares WHERE room=? ORDER BY bot', (room,)).fetchall())

    def shared(self, bot: str) -> bool:
        return self.db.execute('SELECT 1 FROM team_shares WHERE bot=? LIMIT 1', (bot,)).fetchone() is not None

    def rooms_of(self, bot: str) -> list[str]:
        """The rooms that this Mac shares the bot with."""
        return [row[0] for row in self.db.execute('SELECT room FROM team_shares WHERE bot=? ORDER BY room', (bot,))]

    def share(self, room: str, bot: str, entry: str) -> bool:
        """Share a bot with the room. A bot that the room has keeps its entry ID. True when the bot is new there."""
        return self.db.execute('INSERT OR IGNORE INTO team_shares (room, bot, entry) VALUES (?, ?, ?)',
                               (room, bot, entry)).rowcount > 0

    def unshare(self, room: str, bot: str) -> bool:
        """Stop sharing a bot with the room. True when the room had the bot."""
        return self.db.execute('DELETE FROM team_shares WHERE room=? AND bot=?', (room, bot)).rowcount > 0

    def page_shares(self, room: str) -> set[str]:
        """The documents that the person of this Mac shares with the room."""
        return {row[0] for row in self.db.execute('SELECT document FROM team_page_shares WHERE room=?', (room,))}

    def shared_pages(self, document: str | None = None) -> dict[str, list[dict]]:
        """The teams of each shared document, or of one document: its active rooms, each as `room` and `name`, in the
        order of the names.

        This is the one rule of "the page is shared now": the teams and the audience that the app shows, the audience
        that a bot reads, and the size limit of a save. A share with a room that is `removed` or `gone` does not count,
        because that room can never get the page again. Its row stays until the person leaves the room on this Mac.
        """
        teams = {}
        for row, room, name in self.db.execute('''SELECT s.document, s.room, r.name FROM team_page_shares s
                JOIN team_rooms r ON r.room=s.room WHERE r.state=? AND (? IS NULL OR s.document=?)
                ORDER BY r.name, s.room''', (ACTIVE, document, document)):
            teams.setdefault(row, []).append({'room': room, 'name': name})
        return teams

    def page_teams(self, document: str) -> list[dict]:
        """The teams of one document now (shared_pages)."""
        return self.shared_pages(document).get(document, [])

    def rooms_of_page(self, document: str) -> list[str]:
        """Each room that the person of this Mac shares the document with, in any state: for the steps that remove
        the shares of a document or wake the sync. Whether the page is shared now is shared_pages."""
        return [row[0] for row in self.db.execute('SELECT room FROM team_page_shares WHERE document=? ORDER BY room',
                                                  (document,))]

    def share_page(self, room: str, document: str, bot: str, entry: str) -> tuple[bool, bool]:
        """Share a document of `bot` with the room. The bot gets a share row with the entry ID `entry` when it has none
        there. Return whether the page is new in the room, and whether the bot is new there."""
        page = self.db.execute('INSERT OR IGNORE INTO team_page_shares (room, document) VALUES (?, ?)',
                               (room, document)).rowcount > 0
        return page, self.share(room, bot, entry)

    def stop_page(self, room: str, document: str, bot: str) -> tuple[bool, bool]:
        """Stop sharing a document of `bot` with the room. The share row of the bot goes with its last shared page
        there. Return whether the room had the page, and whether the bot left the room."""
        page = self.db.execute('DELETE FROM team_page_shares WHERE room=? AND document=?', (room, document)).rowcount > 0
        return page, page and self.drop_idle_bot(room, bot)

    def drop_idle_bot(self, room: str, bot: str) -> bool:
        """Remove the share row of a bot that has no shared page in the room. True when a row went."""
        return self.db.execute('''DELETE FROM team_shares WHERE room=? AND bot=? AND NOT EXISTS (
            SELECT 1 FROM team_page_shares s JOIN documents d ON d.id=s.document
            WHERE s.room=team_shares.room AND json_extract(d.data, '$.bot_id')=team_shares.bot)''',
                               (room, bot)).rowcount > 0

    def page_entries(self, room: str) -> dict[str, str]:
        """The entry ID of each document of this Mac that the room has or had as a `page` entry."""
        return dict(self.db.execute('SELECT document, entry FROM team_page_entries WHERE room=?', (room,)).fetchall())

    def add_page_entry(self, room: str, document: str, entry: str) -> None:
        self.db.execute('INSERT INTO team_page_entries (room, document, entry) VALUES (?, ?, ?)', (room, document, entry))

    def drop_page_entry(self, room: str, document: str) -> None:
        self.db.execute('DELETE FROM team_page_entries WHERE room=? AND document=?', (room, document))

    def written(self, room: str, member: str) -> dict[str, tuple]:
        """The `bot` and `page` entries that this Mac wrote and did not delete, each with its form: the profile and
        the mark `open` of a bot, or the bot and the version of a page. `accept` keeps the form in columns, so no
        payload is parsed."""
        bots = self.db.execute('SELECT entry, name, avatar, color, open FROM team_bots WHERE room=? AND member=?',
                               (room, member)).fetchall()
        pages = self.db.execute('SELECT entry, bot, version FROM team_pages WHERE room=? AND member=?',
                                (room, member)).fetchall()
        return {entry: ('bot', *form) for entry, *form in bots} | {entry: ('page', *form) for entry, *form in pages}

    def shows(self, room: str) -> bool:
        """True when the Library shows something of the room now: a bot of another member, or the room in the teams
        of a page that this Mac shares. A room that stops being active or goes changes both."""
        return self.db.execute('''SELECT 1 FROM team_bots b JOIN team_rooms r ON r.room=b.room
            WHERE b.room=? AND b.member!=r.member LIMIT 1''', (room,)).fetchone() is not None or bool(
            self.page_shares(room))

    def bots(self, room: str) -> list[dict]:
        """The valid `bot` entries of the other members of the room, for the Team screen."""
        return self._rows(TEAM_BOT, '''SELECT b.entry, b.member, b.name, b.avatar, b.color, b.open FROM team_bots b
            JOIN team_rooms r ON r.room=b.room WHERE b.room=? AND b.member!=r.member ORDER BY b.name, b.entry''',
                          (room,))

    # The Team chat

    def messages(self, room: str, before: int | None = None, limit: int = 50) -> list[dict]:
        """The shown messages of a room before the `first` of `before`, newest first, at most `limit`."""
        return self._rows(MESSAGE_FIELDS, *self._page(room, before, limit))

    def _page(self, room: str, before: int | None, limit: int) -> tuple[str, tuple]:
        """The statement and the values of one page. A page with `before` has `m.first<?` as a plain term, so the
        index team_messages_order starts its search at `before`, and a page reads about `limit` rows in any room. A
        term such as `(? IS NULL OR m.first<?)` would make SQLite walk the index from the newest row."""
        if before is None:
            return self._message_sql('1 ORDER BY m.first DESC LIMIT ?'), (room, limit)
        return self._message_sql('m.first<? ORDER BY m.first DESC LIMIT ?'), (room, before, limit)

    def message(self, room: str, entry: str) -> dict | None:
        """One shown message, or None."""
        rows = self._messages('m.entry=?', (room, entry))
        return rows[0] if rows else None

    def messages_of(self, room: str, entries) -> dict[str, dict]:
        """The shown messages of these entry IDs, by entry ID, in one query. An ID that the chat does not show is not
        in the result."""
        rows = self._messages('m.entry IN (SELECT value FROM json_each(?))', (room, json.dumps(sorted(set(entries)))))
        return {row['entry']: row for row in rows}

    def _messages(self, where: str, values: tuple) -> list[dict]:
        return self._rows(MESSAGE_FIELDS, self._message_sql(where), values)

    @staticmethod
    def _message_sql(where: str) -> str:
        # The name of a member or a bot is the last one that the store showed, also after a `delete` (team_names).
        return f'''SELECT m.entry, m.first, m.member, n.name, m.bot, k.name, m.text,
            m.reply_to, m.mentions, m.created_at, m.updated_at, m.edited FROM team_messages m
            LEFT JOIN team_names n ON n.room=m.room AND n.member=m.member AND n.kind='member'
            LEFT JOIN team_names k ON k.room=m.room AND k.entry=m.bot AND k.kind='bot'
            WHERE m.room=? AND {SHOWN_MESSAGE} AND {where}'''

    def names(self, room: str, entries) -> dict[str, tuple[str, str]]:
        """The kind and the member of each entry ID that is a member or a bot of the room in team_names."""
        rows = self.db.execute('''SELECT entry, kind, member FROM team_names WHERE room=?
            AND entry IN (SELECT value FROM json_each(?))''', (room, json.dumps(sorted(set(entries))))).fetchall()
        return {entry: (kind, member) for entry, kind, member in rows}

    def read_position(self, room: str) -> int:
        row = self.db.execute('SELECT first FROM team_reads WHERE room=?', (room,)).fetchone()
        return row[0] if row else 0

    def mark_read(self, room: str, first: int) -> int | None:
        """Move the read position of the room to `first`, but never back and never past the newest message. The new
        position, or None when it stays."""
        newest = self.db.execute('SELECT MAX(first) FROM team_messages WHERE room=?', (room,)).fetchone()[0] or 0
        position = min(first, newest)
        if position <= self.read_position(room):
            return None
        self.db.execute('''INSERT INTO team_reads (room, first) VALUES (?, ?)
            ON CONFLICT (room) DO UPDATE SET first=excluded.first''', (room, position))
        return position

    def unread(self, room: str, member: str) -> int:
        """How many shown messages are after the read position, up to MAX_UNREAD. Only a message of this Mac's
        person counts as read: the person did not write the reply of a bot of this Mac. The LIMIT keeps the count to
        at most MAX_UNREAD rows of the index."""
        return self.db.execute(f'''SELECT COUNT(*) FROM (SELECT 1 FROM team_messages m WHERE m.room=? AND m.first>?
            AND (m.member!=? OR m.bot IS NOT NULL) AND {SHOWN_MESSAGE} LIMIT {MAX_UNREAD})''',
                               (room, self.read_position(room), member)).fetchone()[0]

    # Direct messages

    def direct_on(self) -> bool:
        """True after an app of the person asked for direct messages on this Mac (team_marks)."""
        return self.db.execute('SELECT 1 FROM team_marks WHERE mark=?', (DIRECT_MARK,)).fetchone() is not None

    def set_direct(self) -> bool:
        """Set the mark of direct messages. True when the mark is new."""
        return self.db.execute('INSERT OR IGNORE INTO team_marks (mark) VALUES (?)', (DIRECT_MARK,)).rowcount > 0

    def direct_waits(self, room: str) -> list[dict]:
        """The waiting `direct` entries of the room, as rows of `team_entries`, in the order of the log."""
        return self._rows(ENTRY_FIELDS, f'''SELECT {", ".join("e." + name for name in ENTRY_FIELDS)} FROM team_entries e
            JOIN team_direct_waits w ON w.room=e.room AND w.entry=e.id WHERE e.room=? ORDER BY e.seq''', (room,))

    def end_wait(self, room: str, entry: dict, direct: dict | None) -> None:
        """End the wait of a `direct` entry after its last try. `direct` is what its box gave this Mac, or None. A
        message that shows now is history: it changes its chat and notifies nothing. A newer write of the entry since
        the try has its own wait, so this one changes nothing then."""
        current = self.entry(room, entry['id'])
        if current is None or current['seq'] != entry['seq']:
            return
        if self.db.execute('DELETE FROM team_direct_waits WHERE room=? AND entry=?', (room, entry['id'])).rowcount:
            for member in self._show_direct(room, entry, entry['rev'], direct):
                self.directed.add((room, member))

    def direct(self, room: str, entry: str) -> dict | None:
        """One direct message that this Mac shows, or None."""
        rows = self._rows(DIRECT_FIELDS, self._direct_sql('d.entry=?'), (room, entry))
        return rows[0] if rows else None

    def directs(self, room: str, member: str, before: int | None = None, limit: int = 50) -> list[dict]:
        """The messages of the direct chat with `member` before the `first` of `before`, newest first, at most
        `limit`. A page with `before` has `d.first<?` as a plain term, so the index team_directs_order starts there."""
        if before is None:
            return self._rows(DIRECT_FIELDS, self._direct_sql('d.member=? ORDER BY d.first DESC LIMIT ?'),
                              (room, member, limit))
        return self._rows(DIRECT_FIELDS, self._direct_sql('d.member=? AND d.first<? ORDER BY d.first DESC LIMIT ?'),
                          (room, member, before, limit))

    def directs_of(self, room: str, member: str, entries) -> dict[str, dict]:
        """The messages of these entry IDs in the direct chat with `member`, by entry ID, in one query."""
        rows = self._rows(DIRECT_FIELDS, self._direct_sql('d.member=? AND d.entry IN (SELECT value FROM json_each(?))'),
                          (room, member, json.dumps(sorted(set(entries)))))
        return {row['entry']: row for row in rows}

    def direct_chats(self, room: str) -> list[dict]:
        """The newest message of each direct chat of the room, newest chat first."""
        return self._rows(DIRECT_FIELDS, self._direct_sql('''d.first=(SELECT MAX(x.first) FROM team_directs x
            WHERE x.room=d.room AND x.member=d.member) ORDER BY d.first DESC'''), (room,))

    @staticmethod
    def _direct_sql(where: str) -> str:
        # The name of the other member is the last one that the store showed, also after it left (team_names).
        return f'''SELECT d.entry, d.first, d.writer, d.member, n.name, d.text, d.reply_to, d.created_at, d.updated_at,
            d.edited FROM team_directs d
            LEFT JOIN team_names n ON n.room=d.room AND n.member=d.member AND n.kind='member'
            WHERE d.room=? AND {where}'''

    def direct_read_position(self, room: str, member: str) -> int:
        """The `first` of the newest message that the person read in the direct chat with `member`. It starts at 0,
        so each message that arrives counts as unread."""
        row = self.db.execute('SELECT first FROM team_direct_reads WHERE room=? AND member=?', (room, member)).fetchone()
        return row[0] if row else 0

    def mark_direct_read(self, room: str, member: str, first: int) -> int | None:
        """Move the read position of a direct chat to `first`, but never back and never past its newest message. The
        new position, or None when it stays."""
        newest = self.db.execute('SELECT MAX(first) FROM team_directs WHERE room=? AND member=?',
                                 (room, member)).fetchone()[0] or 0
        position = min(first, newest)
        if position <= self.direct_read_position(room, member):
            return None
        self.db.execute('''INSERT INTO team_direct_reads (room, member, first) VALUES (?, ?, ?)
            ON CONFLICT (room, member) DO UPDATE SET first=excluded.first''', (room, member, position))
        return position

    def direct_unread(self, room: str, member: str) -> int:
        """How many messages of the other member are after the read position of the direct chat, up to MAX_UNREAD."""
        return self.db.execute(f'''SELECT COUNT(*) FROM (SELECT 1 FROM team_directs WHERE room=? AND member=?
            AND writer=? AND first>? LIMIT {MAX_UNREAD})''',
                               (room, member, member, self.direct_read_position(room, member))).fetchone()[0]

    # The Library of the phone

    def library(self, current: str) -> tuple[list[dict], list[dict]]:
        """The team pages that this Mac shows, without the pages past their end, and the team bots.

        A page shows only while its `bot` entry is valid and belongs to the member of the page, and while that
        member is in the member list. So a `delete` over the bot entry or over the `member` entry hides the page.
        The Library shows a room only while it is active on this Mac, and never the own entries of this Mac.
        """
        pages = self._pages('(p.expires_at IS NULL OR p.expires_at > ?)', (current,))
        bots = self._rows(LIBRARY_BOT, f'''SELECT b.room, b.entry, b.name, b.avatar, b.color, r.name, b.member, m.name
            FROM team_bots b JOIN team_members m ON m.room=b.room AND m.member=b.member
            JOIN team_rooms r ON r.room=b.room WHERE b.member!=r.member AND r.state='{ACTIVE}'
            ORDER BY r.created_at, b.room, b.name, b.entry''')
        return pages, bots

    def page(self, room: str, entry: str) -> dict | None:
        """A team page that this Mac shows, also one past its end. Else None."""
        rows = self._pages('p.room=? AND p.entry=?', (room, entry))
        return rows[0] if rows else None

    def content(self, room: str, entry: str) -> str:
        """The content of a valid team page, from the payload of its entry."""
        payload = self.db.execute('SELECT payload FROM team_entries WHERE room=? AND id=?', (room, entry)).fetchone()[0]
        # Python reads the payload as page_fields did. SQLite can take another value of a key that comes twice.
        return json.loads(payload)['content']

    def pin(self, room: str, entry: str, pinned: bool) -> None:
        self.db.execute('UPDATE team_pages SET pinned=? WHERE room=? AND entry=?', (int(pinned), room, entry))

    def _pages(self, where: str, values: tuple) -> list[dict]:
        columns = ', '.join('p.' + name for name in PAGE_FIELDS)
        return self._rows(LIBRARY_PAGE, f'''SELECT p.room, p.entry, p.bot, r.name, {columns} FROM team_pages p
            JOIN team_bots b ON b.room=p.room AND b.entry=p.bot AND b.member=p.member
            JOIN team_members m ON m.room=p.room AND m.member=p.member JOIN team_rooms r ON r.room=p.room
            WHERE p.valid=1 AND p.member!=r.member AND r.state='{ACTIVE}' AND {where}''', values)
