"""The copy of the team log on this Mac: rooms, members and entries in coconut.sqlite3 (docs/team-log.md).

Each table has the room ID in its key, because a Mac can be in more than one room.

- `team_rooms`: one row for each room of this Mac: its relay, its name, this Mac's member ID, the admin, and how far
  the sync read (`seq`). `profile` is what this Mac writes about itself in its `member` entry. `notifications` is the
  choice of the person of this Mac: a new message of the room notifies the phone. It never goes into the log.
- `team_entries`: the newest accepted write of each entry ID, with the payload as its member wrote it and the epoch of
  its body.
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
- `team_messages`: a view of `team_entries` for the chats: one row for each valid `message` entry, in the order of
  `first`. `chat` is the entry ID of the named chat of the message, or null for the Team chat.
- `team_chats`: a view of `team_entries`: the valid `chat` entries of the admin, the named chats of the room.
- `team_names`: the last name that the store showed for each member and each bot. It stays after a `delete` entry,
  so a message of a member that left keeps a name, and a message of a bot can still name a valid `bot` entry.
- `team_reads`: the read position of this Mac in the Team chat of each room: the `first` of the newest message that
  the person read.
- `team_chat_reads`: the read position of this Mac in each named chat that the person read.
- `team_triggers`: on a team-only Mac only, the messages and the direct messages that start a turn of a bot of this
  Mac, in the order in which this Mac accepted them (team_turns.py). `started` marks a trigger whose turn began.
- `team_directs`: the direct messages that this Mac shows: one row for each `direct` entry whose box opens for this
  Mac, with `member`, the other member of the chat. `bot` is the `bot` entry of a chat with a bot, or null for a
  chat of two people, and `page` the page that a message is about. On a team-only Mac each row is a message of a chat
  of a person with a bot of this Mac: `member` is the person. No query of the Team chat reads this table.
- `team_direct_reads`: the read position of this Mac in each direct chat of two people: one row for each room and
  other member.
- `team_bot_direct_reads`: the read position of this Mac in each direct chat with a bot: one row for each room and
  `bot` entry.
- `team_marks`: the lasting marks of this Mac. `direct` means that an app of the person knows direct messages.
  `corrected:<room>` means that the open of the store corrected a row of the room, and its event did not go out yet.
- `team_direct_waits`: the `direct` entries that the sync could not try with the key that they need yet, because the
  `member` entry of that key came later in the read. The end of the read tries each one again, then removes it.
- `team_removed`: the members that a `remove` entry of the admin took out of the room, each with the entry ID of that
  entry. A member here shows in no list, also when the store still has its `member` entry.
- `team_invites`: on the admin's Mac, the invites that this Mac made: the hash of the ticket and its end. A removal
  removes the open ones on the relay first.
- `team_erased`: the messages that an `erase` entry of the admin took out of the chat. A later write of such a message
  shows nothing.
- `team_reactions`: the reactions on the messages of the chats: one row for each entry that says a reaction, from its
  newest write: a `reaction` entry, or a `direct` entry whose box is a reaction. An empty emoji (taken back) keeps its
  row. `peer` and `bot` name the direct chat of such a box, as `member` and `bot` of `team_directs` do, and both are
  null for a reaction in a team chat. A view takes the row with the highest `seq` for each message, member and chat.

Who types in a chat is in memory only (`TeamStore.typing` and `TeamStore.direct_typing`), not in a table: a restart of
the service forgets it.

`TeamStore.accept` holds the "Rules for a Mac that reads an entry" of the contract. The secrets of a room are not
here: team_keys.py keeps them in a private file. The caller holds the transaction of each write.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import re
import time
import unicodedata

from .team_relay import HEX32, HEX64, number

log = logging.getLogger(__name__)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS team_rooms (room TEXT PRIMARY KEY, relay TEXT NOT NULL, name TEXT NOT NULL,
    member TEXT NOT NULL, admin TEXT NOT NULL, state TEXT NOT NULL, epoch INTEGER NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0, profile TEXT NOT NULL, created_at TEXT NOT NULL,
    notifications INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS team_entries (room TEXT NOT NULL, id TEXT NOT NULL, seq INTEGER NOT NULL,
    first INTEGER NOT NULL, member TEXT NOT NULL, kind TEXT NOT NULL, rev INTEGER NOT NULL, at INTEGER NOT NULL,
    kept INTEGER NOT NULL, payload TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (room, id));
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
    direct INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (room, entry));
CREATE TABLE IF NOT EXISTS team_pages (room TEXT NOT NULL, entry TEXT NOT NULL, member TEXT NOT NULL,
    valid INTEGER NOT NULL, bot TEXT, version INTEGER, key TEXT, title TEXT, format TEXT, summary TEXT, size INTEGER, rev INTEGER,
    expires_at TEXT, created_at TEXT, updated_at TEXT, pinned INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (room, entry));
CREATE TABLE IF NOT EXISTS team_messages (room TEXT NOT NULL, entry TEXT NOT NULL, first INTEGER NOT NULL,
    member TEXT NOT NULL, bot TEXT, text TEXT NOT NULL, reply_to TEXT, mentions TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, edited INTEGER NOT NULL, chat TEXT, PRIMARY KEY (room, entry));
CREATE TABLE IF NOT EXISTS team_names (room TEXT NOT NULL, entry TEXT NOT NULL, member TEXT NOT NULL,
    kind TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (room, entry));
CREATE INDEX IF NOT EXISTS team_names_member ON team_names (room, member, kind);
CREATE TABLE IF NOT EXISTS team_reads (room TEXT PRIMARY KEY, first INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS team_triggers (number INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
    bot TEXT NOT NULL, entry TEXT NOT NULL, started INTEGER NOT NULL DEFAULT 0, UNIQUE (room, bot, entry));
CREATE TABLE IF NOT EXISTS team_directs (room TEXT NOT NULL, entry TEXT NOT NULL, first INTEGER NOT NULL,
    writer TEXT NOT NULL, member TEXT NOT NULL, text TEXT NOT NULL, reply_to TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, edited INTEGER NOT NULL, bot TEXT, page TEXT, PRIMARY KEY (room, entry));
CREATE TABLE IF NOT EXISTS team_direct_reads (room TEXT NOT NULL, member TEXT NOT NULL, first INTEGER NOT NULL,
    PRIMARY KEY (room, member));
CREATE TABLE IF NOT EXISTS team_bot_direct_reads (room TEXT NOT NULL, bot TEXT NOT NULL, first INTEGER NOT NULL,
    PRIMARY KEY (room, bot));
CREATE TABLE IF NOT EXISTS team_marks (mark TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS team_direct_waits (room TEXT NOT NULL, entry TEXT NOT NULL, PRIMARY KEY (room, entry));
CREATE TABLE IF NOT EXISTS team_removed (room TEXT NOT NULL, member TEXT NOT NULL, entry TEXT NOT NULL,
    PRIMARY KEY (room, member));
CREATE TABLE IF NOT EXISTS team_invites (room TEXT NOT NULL, hash TEXT NOT NULL, expires INTEGER NOT NULL,
    PRIMARY KEY (room, hash));
CREATE TABLE IF NOT EXISTS team_erased (room TEXT NOT NULL, entry TEXT NOT NULL, PRIMARY KEY (room, entry));
CREATE TABLE IF NOT EXISTS team_chats (room TEXT NOT NULL, entry TEXT NOT NULL, first INTEGER NOT NULL,
    name TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (room, entry));
CREATE TABLE IF NOT EXISTS team_chat_reads (room TEXT NOT NULL, chat TEXT NOT NULL, first INTEGER NOT NULL,
    PRIMARY KEY (room, chat));
-- A store from before team_names gets the names that it shows now. A later accept keeps them current.
INSERT OR IGNORE INTO team_names (room, entry, member, kind, name)
    SELECT room, entry, member, 'member', name FROM team_members;
INSERT OR IGNORE INTO team_names (room, entry, member, kind, name)
    SELECT room, entry, member, 'bot', name FROM team_bots;
'''
TABLES = ('team_rooms', 'team_entries', 'team_members', 'team_pending', 'team_shares', 'team_page_shares',
          'team_page_entries', 'team_bots', 'team_pages', 'team_messages', 'team_names', 'team_reads', 'team_triggers',
          'team_directs', 'team_direct_reads', 'team_bot_direct_reads', 'team_direct_waits', 'team_removed',
          'team_invites',
          'team_erased', 'team_chats', 'team_chat_reads', 'team_reactions')
# The table of the reactions and its index for the views. The store makes both in `migrate`, so a store of an older
# version reads its stored `reaction` entries one time when it gets the table (_read_reactions_again). The state is a
# function of the newest write of each entry only, so each Mac shows the same reactions for the same log, in whatever
# order it applies the entries.
REACTIONS_TABLE = '''CREATE TABLE team_reactions (room TEXT NOT NULL, entry TEXT NOT NULL, message TEXT NOT NULL,
    member TEXT NOT NULL, emoji TEXT NOT NULL, seq INTEGER NOT NULL, peer TEXT, bot TEXT, PRIMARY KEY (room, entry))'''
# The index has `member` and `seq`, so the inner MAX(seq) of SHOWN_REACTIONS finds the newest row of a member with one
# seek. A member that writes many entries for one message then keeps the view of a page linear.
REACTIONS_INDEX = 'CREATE INDEX team_reactions_message ON team_reactions (room, message, member, seq)'
# The reactions that show on the messages of one chat: for each message and member, the row with the highest `seq`
# in that chat (the same `peer` and `bot`), when its emoji is not empty. The values are the room, the entry IDs of
# the messages as a JSON list, `peer` and `bot`.
SHOWN_REACTIONS = '''SELECT r.message, r.emoji, r.member FROM team_reactions r
    WHERE r.room=? AND r.message IN (SELECT value FROM json_each(?)) AND r.peer IS ? AND r.bot IS ? AND r.emoji!=''
    AND r.seq=(SELECT MAX(x.seq) FROM team_reactions x WHERE x.room=r.room AND x.message=r.message
        AND x.member=r.member AND x.peer IS r.peer AND x.bot IS r.bot)
    ORDER BY r.seq'''
# The emoji of a reaction, in the order of the view of a message (docs/team-log.md, "Reactions"): thumbs up, red heart
# (with the variation selector 16), face with tears of joy, face with open mouth, crying face, folded hands.
REACTIONS = ('\U0001F44D', '\u2764\ufe0f', '\U0001F602', '\U0001F62E', '\U0001F622', '\U0001F64F')
ROOM_FIELDS = ('room', 'relay', 'name', 'member', 'admin', 'state', 'epoch', 'seq', 'profile', 'created_at',
               'notifications')
ENTRY_FIELDS = ('id', 'seq', 'first', 'member', 'kind', 'rev', 'at', 'kept', 'payload')
MEMBER_FIELDS = ('member', 'name', 'avatar', 'color', 'agreement', 'entry', 'bot_only', 'direct')
PENDING_FIELDS = ('id', 'seq', 'first', 'member', 'at', 'kept', 'body', 'reason')
PAGE_FIELDS = ('key', 'title', 'format', 'summary', 'size', 'rev', 'expires_at', 'created_at', 'updated_at', 'pinned')
# What the Library of the phone gets for a team page and a team bot. TeamStore.library reads these rows.
LIBRARY_PAGE = ('room', 'entry', 'bot', 'team', *PAGE_FIELDS)
LIBRARY_BOT = ('room', 'entry', 'name', 'avatar', 'color', 'team', 'member', 'member_name')
TEAM_BOT = ('entry', 'member', 'name', 'avatar', 'color', 'open', 'direct')
# What the chat of the phone gets for a message. TeamStore.messages reads these rows.
MESSAGE_FIELDS = ('entry', 'first', 'member', 'member_name', 'bot', 'bot_name', 'text', 'reply_to', 'mentions',
                  'created_at', 'updated_at', 'edited', 'chat')
# A message shows only when its `bot` is null or names a `bot` entry of the same member that the store showed once.
# The bot entry can come after the message, because the relay keeps only its newest write, so the check runs at read
# time. team_names keeps the bot after a `delete`, so the history of a bot that the team no longer has stays. The same
# holds for `chat`: a message of a named chat shows only while the store shows that chat (team_chats).
SHOWN_MESSAGE = '''(m.bot IS NULL OR EXISTS (SELECT 1 FROM team_names b WHERE b.room=m.room AND b.entry=m.bot
    AND b.kind='bot' AND b.member=m.member)) AND (m.chat IS NULL OR EXISTS (SELECT 1 FROM team_chats c
    WHERE c.room=m.room AND c.entry=m.chat))'''
# A message names a member when its `mentions` have a `member` entry of that member. The value is the member ID. The
# primary key of team_names finds each mention, so the check reads at most 16 rows.
NAMES_MEMBER = '''EXISTS (SELECT 1 FROM json_each(m.mentions) j JOIN team_names n ON n.room=m.room AND n.entry=j.value
    WHERE n.kind='member' AND n.member=?)'''
# A message counts as unread for this Mac's person when another member wrote it, or a bot wrote it. The value is the
# member ID of this Mac.
UNREAD_MESSAGE = '(m.member!=? OR m.bot IS NOT NULL)'
# The event of each transaction that changes the chat of a room: a message, an edit, a removal, or the read position.
# GET /v1/updates then has `team_messages_changed`.
MESSAGES_EVENT = 'team_messages.updated'
# What a direct chat of the phone gets for a message. TeamStore.directs reads these rows. `member` is the other
# member of the chat, and `writer` is the member that wrote the message. `bot` is the entry ID of the `bot` entry of a
# chat with a bot, or null for a chat of two people. `page` is the entry ID of the page that a message is about.
DIRECT_FIELDS = ('entry', 'first', 'writer', 'member', 'member_name', 'text', 'reply_to', 'created_at', 'updated_at',
                 'edited', 'bot', 'bot_name', 'page')
# A direct message with `bot` shows only while `bot` names a `bot` entry of the member of the chat that the store showed
# once (team_names), as a message of a bot in the Team chat. The `bot` entry can come after the message, so the check
# runs at read time.
SHOWN_DIRECT = '''(d.bot IS NULL OR EXISTS (SELECT 1 FROM team_names b WHERE b.room=d.room AND b.entry=d.bot
    AND b.kind='bot' AND b.member=d.member))'''
# On a Mac for a team only, a message of a direct chat with a bot of this Mac shows only while `bot` names a `bot`
# entry of this Mac that the store showed once (team_names).
OWN_BOT_DIRECT = '''EXISTS (SELECT 1 FROM team_names b JOIN team_rooms r ON r.room=b.room WHERE b.room=d.room
    AND b.entry=d.bot AND b.kind='bot' AND b.member=r.member)'''
# The event of each transaction that changes a direct chat of two people: `{"team": <room>, "member": <other member>}`.
# GET /v1/updates then has `team_directs_changed`.
DIRECTS_EVENT = 'team_directs.updated'
# The event of each transaction that changes a direct chat with a bot: `{"team": <room>, "bot": <team bot ID>}`.
# GET /v1/updates then has `team_bot_directs_changed`.
BOT_DIRECTS_EVENT = 'team_bot_directs.updated'
# The event of each change of who types in the Team chat of a room: a person or a bot starts, types on, or ends.
# GET /v1/updates then has `team_typing_changed`.
TYPING_EVENT = 'team_typing.updated'
# A `typing` entry shows only when its `at` of the relay is under this many seconds from the clock of this Mac. So a
# first read of the log, or a read after a long stop, shows nothing.
TYPING_FRESH_SECONDS = 60
# How long a person and a bot show as typing after the read of their `typing` entry, unless a new entry ends it first.
TYPING_PERSON_SECONDS = 10
TYPING_BOT_SECONDS = 30
# The mark in `team_marks` after an app of the person asked for direct messages.
DIRECT_MARK = 'direct'
# The prefix of a mark in `team_marks` for a room whose rows the open of the store corrected, until its event goes out.
CORRECTED_MARK = 'corrected:'
# The repair of the marks at a start (TeamStore._read_marks_again) reads a kept `member` or `bot` payload only up to this
# size. An honest one has at most some kilobytes: four short text fields of at most 80 code points and a key.
MAX_MARK_PAYLOAD = 1 << 20
# The longest wait of that repair, and of the event after it, for the write lock of another connection. A write of the
# service lasts some milliseconds, and the next start tries again, so a start never waits long for it.
MARK_WAIT_MS = 1000
# The limits of a `message` entry, in Unicode code points and in entry IDs.
MAX_TEXT = 16000
MAX_MENTIONS = 16
# The unread count of a room stops here, so the count reads at most this many rows.
MAX_UNREAD = 99
# The check of a mention (`mention_first`) reads at most this many rows of the index after the read position, the
# newest first, also own messages and messages that do not show. GET /v1/team/rooms runs it for each room on each load
# under the service lock, so it needs a bound. It is larger than MAX_UNREAD: the badge stops at 99, but a mention of
# the person in the 100th unread message must still show the mark.
MAX_MENTION_ROWS = 1000
# The statement of TeamStore.mention_first. The values are the room, the chat, the read position, and the member ID of
# this Mac two times.
MENTION_SQL = f'''SELECT MAX(m.first) FROM (SELECT room, first, member, bot, mentions, chat FROM team_messages
    WHERE room=? AND chat IS ? AND first>? ORDER BY first DESC LIMIT {MAX_MENTION_ROWS}) m
    WHERE {UNREAD_MESSAGE} AND {SHOWN_MESSAGE} AND {NAMES_MEMBER}'''
# A message starts a turn of a bot, or notifies the phone of a person, only while it is younger than this. An older
# message is history, for example the log that a Mac reads after it joins.
TRIGGER_SECONDS = 3600
# At most this many triggers wait for one bot in one room. A member can mention the bot again later.
MAX_WAITING = 8
TRIGGER_FIELDS = ('number', 'room', 'bot', 'entry', 'started')
CHAT_FIELDS = ('entry', 'first', 'name', 'created_at')
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

# The longest life of a ticket on the relay (docs/team-log.md, "Limits"). The Mac forgets the record of an invite only
# this long after its saved end, because the relay starts its own count later than this Mac, and its clock can differ.
MAX_TICKET_SECONDS = 7 * 86400

# Only the admin writes these kinds (rule 4).
ADMIN_KINDS = ('remove', 'key', 'erase', 'chat')
MAX_KIND = 64
# The fields of a `member` entry, with the most Unicode code points of each.
MEMBER_LIMITS = {'name': 80, 'avatar': 80, 'color': 20}
# A public agreement key: 32 bytes as unpadded base64url.
AGREEMENT = re.compile(r'[A-Za-z0-9_-]{43}')
# A `key` entry has a box for at most this many members: each member of a full room but the admin.
MAX_BOXES = 64
# The longest text of a box that a reader takes. A box of the contract has 80 characters.
MAX_BOX = 1024
# The epoch of a body is a 32-bit number.
MAX_EPOCH = 2 ** 32 - 1
# The kinds of the kept entries that a Mac writes again with the room key of a new epoch (TeamEpochs).
WRITTEN_AGAIN = ('member', 'bot', 'page', 'chat')
# The most Unicode code points of the name of a chat, and the most chats of a room (docs/team-log.md, "Chats of a team").
MAX_CHAT_NAME = 50
MAX_CHATS = 20


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
    """The profile of a `bot` payload and its marks `open` and `direct`, or None when a field of the profile does not
    fit.

    `open` is true only for the JSON value true: a team-only Mac writes it for a bot that answers each member of the
    team. A missing field and each other value are false, so a value that does not fit hides no bot. A reader trusts
    the mark only for the list of the bots that a member can name in the chat. The mark `direct` (the bot answers
    direct messages) follows the same rule.
    """
    result = profile_fields(fields)
    return dict(result, open=fields.get('open') is True, direct=fields.get('direct') is True) if result else None


def member_fields(fields: dict) -> dict | None:
    """The name, avatar, color and agreement key of a `member` payload, or None when a field does not fit. Also the
    mark `bot_only`: true only for the JSON value true, which a Mac without a person writes. A missing field and each
    other value are false, so a value that does not fit hides no member. The mark `direct` (this member reads direct
    messages) follows the same rule."""
    result = profile_fields(fields)
    agreement = fields.get('agreement')
    if not result or not agreement_key(agreement):
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

    `text` has 1 to MAX_TEXT code points of valid Unicode text. `reply_to`, `bot` and `chat` are entry IDs, and
    `mentions` is a list of at most MAX_MENTIONS entry IDs. A reader takes null as an absent field, so a later writer
    can write either. Fields that this version does not know stay in the payload, and nothing reads them.
    """
    text, reply_to, mentions, bot, chat = (fields.get(name) for name in ('text', 'reply_to', 'mentions', 'bot', 'chat'))
    if not unicode_text(text) or not 0 < len(text) <= MAX_TEXT:
        return None
    if any(value is not None and not entry_id(value) for value in (reply_to, bot, chat)):
        return None
    mentions = [] if mentions is None else mentions
    if not isinstance(mentions, list) or len(mentions) > MAX_MENTIONS or not all(map(entry_id, mentions)):
        return None
    return dict(text=text, reply_to=reply_to, mentions=list(dict.fromkeys(mentions)), bot=bot, chat=chat)


def chat_fields(fields: dict) -> dict | None:
    """The name of a `chat` payload, or None when it does not fit (docs/team-log.md, "Chats of a team"). A reader takes
    each name of 1 to MAX_CHAT_NAME code points of valid Unicode text. The admin's Mac writes a name by a stricter rule
    (team_chats.py)."""
    name = fields.get('name')
    return dict(name=name) if unicode_text(name) and 0 < len(name) <= MAX_CHAT_NAME else None


def direct_fields(plaintext, to: str, page: bool = False) -> dict | None:
    """What a direct chat shows of the plaintext of a box, or None when it does not fit.

    The plaintext is one JSON object in UTF-8. `to` must name the member that the caller expects: this Mac for a
    message of another member, or the other member for an own message. `text` has 1 to MAX_TEXT code points of valid
    Unicode text, and `reply_to` is an entry ID or absent. A reader takes null as an absent field and ignores a field
    that it does not know.

    `bot` is the entry ID of the `bot` entry of a chat with a bot, or absent for a chat of two people. With `page`, the
    reader also takes the field `page`: the entry ID of the page that a message of the person is about. A reader
    ignores `page` in a box without `bot` and in a message of the Mac of the bot, so a person's Mac reads it only in
    an own message.
    """
    fields = box_fields(plaintext, to)
    if fields is None:
        return None
    text, reply_to = fields.get('text'), fields.get('reply_to')
    if not unicode_text(text) or not 0 < len(text) <= MAX_TEXT:
        return None
    bot = fields.get('bot')
    about = fields.get('page') if page and bot is not None else None
    if any(value is not None and not entry_id(value) for value in (reply_to, bot, about)):
        return None
    return dict(text=text, reply_to=reply_to, bot=bot, page=about)


def direct_typing_fields(plaintext, to: str) -> dict | None:
    """The typing sign of a bot in the plaintext of a box (docs/team-log.md, "The Mac for a team only", rule 12), or
    None when the box is not one. `to` must name this Mac, `bot` is an entry ID, `on` is true or false, and the box
    has no `text`, no `message` and no `emoji`, so it is never a reaction (reaction_box_fields)."""
    fields = box_fields(plaintext, to)
    if fields is None or any(fields.get(name) is not None for name in ('text', 'message', 'emoji')):
        return None
    if type(fields.get('on')) is not bool or not entry_id(fields.get('bot')):
        return None
    return dict(bot=fields['bot'], on=fields['on'])


def box_fields(plaintext, to: str) -> dict | None:
    """The JSON object of the plaintext of a box when its `to` names `to`, else None."""
    try:
        fields = json.loads(bytes(plaintext).decode('utf-8'))
    except (TypeError, ValueError, RecursionError):
        return None
    return fields if isinstance(fields, dict) and isinstance(fields.get('to'), str) and fields['to'] == to else None


def typing_fields(fields: dict) -> dict | None:
    """What a `typing` payload says, or None when a field does not fit (docs/team-log.md, "Typing").

    `on` is true or false. `bot` is the entry ID of a `bot` entry of the same member, or absent for the person of the
    Mac. `chat` is the entry ID of the named chat of the subject, or absent for the Team chat. A reader takes null as
    an absent field.
    """
    on, bot, chat = fields.get('on'), fields.get('bot'), fields.get('chat')
    if type(on) is not bool or any(value is not None and not entry_id(value) for value in (bot, chat)):
        return None
    return dict(on=on, bot=bot, chat=chat)


def reaction_fields(fields: dict) -> dict | None:
    """What a `reaction` payload says, or None when a field does not fit (docs/team-log.md, "Reactions").

    `message` is the entry ID of the message. `emoji` is one of REACTIONS, or empty when the member took its reaction
    back. A later version can know more emoji. An emoji that this version does not know does not fit, so the entry
    shows nothing, as each entry that does not fit.
    """
    message, emoji = fields.get('message'), fields.get('emoji')
    if not entry_id(message) or not (emoji == '' or emoji in REACTIONS):
        return None
    return dict(message=message, emoji=emoji)


def reaction_box_fields(plaintext, to: str) -> dict | None:
    """What the box of a `direct` entry says when it is a reaction, or None (docs/team-log.md, "Reactions").

    The box has the fields of a `reaction` payload, `to` (the member that the caller expects, as in direct_fields), and
    `bot` for a chat with a bot. It has no `text`, so a Mac of an older version finds no direct message in it. It has
    no `on`, so it is never the typing sign of a bot (direct_typing_fields), and that sign is never a reaction.
    """
    fields = box_fields(plaintext, to)
    if fields is None or fields.get('text') is not None or fields.get('on') is not None:
        return None
    bot = fields.get('bot')
    reaction = reaction_fields(fields)
    if reaction is None or (bot is not None and not entry_id(bot)):
        return None
    return dict(reaction, bot=bot)


def reaction_views(reactions: list[tuple[str, str]], own: str) -> list[dict]:
    """The `reactions` of a message for the app: one item for each emoji with a member, in the order of REACTIONS.
    `reactions` are (emoji, member ID) in the order of the log. `mine` is true when `own`, the member of this Mac, is
    one of the members."""
    members: dict[str, list[str]] = {}
    for emoji, member in reactions:
        members.setdefault(emoji, []).append(member)
    return [{'emoji': emoji, 'members': members[emoji], 'mine': own in members[emoji]}
            for emoji in REACTIONS if emoji in members]


def remove_fields(fields: dict) -> dict | None:
    """The member that a `remove` payload takes out of the room, and its name, or None when a field does not fit.

    `member` is a member ID: 64 lowercase hex characters. `name` is the last name that the admin's Mac showed for that
    member, for the history. A reader does not need it, so a name that does not fit hides no removal.
    """
    member, name = fields.get('member'), fields.get('name')
    if not isinstance(member, str) or not HEX64.fullmatch(member):
        return None
    return dict(member=member, name=name if unicode_text(name) and 0 < len(name) <= MEMBER_LIMITS['name'] else None)


def erase_fields(fields: dict) -> dict | None:
    """The message that an `erase` payload takes out of the chat, or None when the field does not fit.

    `entry` is the entry ID of a `message` entry. A reader cannot know the kind of an entry that it does not have yet,
    so this check reads only the form of the ID.
    """
    entry = fields.get('entry')
    return dict(entry=entry) if entry_id(entry) else None


def agreement_key(value) -> bool:
    """True when a value is a public agreement key: 32 bytes as unpadded base64url, in the one text of these bytes (the
    unused bits of the last character are zero)."""
    if not isinstance(value, str) or not AGREEMENT.fullmatch(value):
        return False
    return base64.urlsafe_b64encode(base64.urlsafe_b64decode(value + '=')).decode().rstrip('=') == value


def same_fields(payload: str, fields: dict) -> bool:
    """True when the payload of the store has the same fields as `fields`, each field but `rev` and `at`."""
    stored = json.loads(payload)
    return {name: value for name, value in stored.items() if name not in ('rev', 'at')} == {
        name: value for name, value in fields.items() if name not in ('rev', 'at')}


def key_fields(fields: dict, epoch) -> dict | None:
    """The new epoch, the agreement key of the admin and the boxes of a `key` payload, or None when a field does not
    fit (docs/team-log.md, "Key change").

    `epoch` is the epoch of the body. The new epoch is the next one. `boxes` maps at most MAX_BOXES member IDs to the
    box of each, as text. A box that does not open is a matter for its own member only, so this check does not read a
    box.
    """
    new, agreement, boxes = fields.get('epoch'), fields.get('agreement'), fields.get('boxes')
    if type(epoch) is not int or type(new) is not int or new != epoch + 1 or new > MAX_EPOCH:
        return None
    if not agreement_key(agreement) or not isinstance(boxes, dict) or len(boxes) > MAX_BOXES:
        return None
    if not all(HEX64.fullmatch(member) and isinstance(box, str) and 0 < len(box) <= MAX_BOX
               for member, box in boxes.items()):
        return None
    return dict(epoch=new, agreement=agreement, boxes=boxes)


SHOWN = {'member': member_fields, 'bot': bot_fields, 'page': page_fields, 'message': message_fields,
         'remove': remove_fields, 'erase': erase_fields, 'typing': typing_fields, 'chat': chat_fields,
         'reaction': reaction_fields}


def same_name(first: str, second: str) -> bool:
    """True when two names are the same for a person: equal after Unicode normalization to NFC and casefold().

    The limit: casefold() does not join each letter with its other case. About 11 letters, for example some letters
    with a title case of their own, stay different from their other case here."""
    return unicodedata.normalize('NFC', first).casefold() == unicodedata.normalize('NFC', second).casefold()


def stored_fields(kind: str, payload: str | bytes) -> dict | None:
    """The checked fields of a kept entry of this kind, as read_entry finds them for `accept`. None for a payload that
    does not read, has another kind, or has fields that do not fit. No payload makes it raise."""
    try:
        opened = read_entry({'epoch': 0, 'payload': payload.encode() if isinstance(payload, str) else payload})
    except Exception:
        return None
    return opened['shown'] if opened['read'] and opened['read'][1]['kind'] == kind else None


def read_entry(opened: dict) -> dict:
    """The first part of the reading rules for one entry: the work that needs no database.

    It reads the payload (rule 2), and for a `member`, `bot`, `page`, `message`, `remove`, `erase`, `typing`, `chat`,
    `reaction` or `key` entry it checks the fields that the store uses. The page rules run an HTML parser on up to 512 KiB that another member wrote, so a
    caller runs this part before its transaction and without the lock of the service. A page that is slow to check then slows only the
    sync thread. The result is `opened` with `read` (the text and the fields of the payload, or None) and `shown`
    (the checked fields, or None). TeamStore.accept is the second part.
    """
    if opened.get('error'):
        return opened
    payload = read_payload(opened.get('payload'))
    check = SHOWN.get(payload[1]['kind']) if payload else None
    if payload and payload[1]['kind'] == 'key':
        # The new epoch of a `key` entry follows the epoch of its body.
        def check(fields):
            return key_fields(fields, opened.get('epoch'))
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
        # The direct chats with a bot that `accept` changed, as (room, entry ID of the `bot` entry).
        self.bot_directed: set[tuple[str, str]] = set()
        self.fresh_directs: dict[str, list[str]] = {}
        # Who types in the chats of each room: for each (room, member, bot entry or None) the time of the start, the
        # time of the end on the clock below, and the chat (None for the Team chat). A subject types in one chat at a
        # time. `typed` has the rooms whose lists changed, and TeamSync tells the phone. Nothing of this goes to the
        # database.
        self.typing: dict[tuple[str, str, str | None], tuple[float, float, str | None]] = {}
        # The typing sign of a bot in a direct chat with the person of this Mac: for each (room, member of the bot,
        # bot entry) the time of the end on the clock below. It adds the room to `typed` too.
        self.direct_typing: dict[tuple[str, str, str], float] = {}
        self.typed: set[str] = set()
        self.clock = time.time

    def migrate(self) -> None:
        """Add the columns that a store of an older version lacks. A room of that version has its notifications on.
        The marks of a `bot` row and a `member` row come from their kept entries (_read_marks_again)."""
        if 'open' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_bots)')}:
            self.db.execute('ALTER TABLE team_bots ADD COLUMN open INTEGER NOT NULL DEFAULT 0')
        if 'bot_only' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_members)')}:
            self.db.execute('ALTER TABLE team_members ADD COLUMN bot_only INTEGER NOT NULL DEFAULT 0')
        if 'notifications' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_rooms)')}:
            self.db.execute('ALTER TABLE team_rooms ADD COLUMN notifications INTEGER NOT NULL DEFAULT 1')
        if 'direct' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_members)')}:
            self.db.execute('ALTER TABLE team_members ADD COLUMN direct INTEGER NOT NULL DEFAULT 0')
        # Each body of an older version has epoch 0, because no Mac changed a room key before this column.
        if 'epoch' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_entries)')}:
            self.db.execute('ALTER TABLE team_entries ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0')
        if 'chat' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_messages)')}:
            # The new column and the pass are one step: a stop or an error in the pass takes the column back, so the
            # next start does the pass again. A savepoint works also inside a transaction of a caller.
            self.db.execute('SAVEPOINT team_chats_column')
            try:
                self.db.execute('ALTER TABLE team_messages ADD COLUMN chat TEXT')
                self._read_chats_again()
            except BaseException:
                self.db.execute('ROLLBACK TO team_chats_column')
                self.db.execute('RELEASE team_chats_column')
                raise
            self.db.execute('RELEASE team_chats_column')
        # A `bot` row of an older version gets its mark `direct` from its kept entry (_read_marks_again).
        if 'direct' not in {row[1] for row in self.db.execute('PRAGMA table_info(team_bots)')}:
            self.db.execute('ALTER TABLE team_bots ADD COLUMN direct INTEGER NOT NULL DEFAULT 0')
        # Each direct message of an older version is a message of a chat of two people.
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(team_directs)')}
        for column in ('bot', 'page'):
            if column not in columns:
                self.db.execute(f'ALTER TABLE team_directs ADD COLUMN {column} TEXT')
        # One index on the room, the member, the bot and `first` gives a page, the unread count and the newest message
        # of each direct chat. It takes the place of the index of an older version without the bot.
        self.db.execute('DROP INDEX IF EXISTS team_directs_order')
        self.db.execute('CREATE INDEX IF NOT EXISTS team_directs_chat ON team_directs (room, member, bot, first)')
        # One index on the room, the chat and `first` gives a page, the unread count and the newest message of each chat.
        # It takes the place of the index on the room and `first` of an older version, which SQLite would choose for a
        # named chat and then walk the messages of each other chat.
        self.db.execute('DROP INDEX IF EXISTS team_messages_order')
        self.db.execute('CREATE INDEX IF NOT EXISTS team_messages_chat ON team_messages (room, chat, first)')
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_reactions'").fetchone():
            # The new table and the pass are one step, as for the column `chat` above.
            self.db.execute('SAVEPOINT team_reactions_table')
            try:
                self.db.execute(REACTIONS_TABLE)
                # The index comes before the pass, so each step of the pass finds its rows by an index.
                self.db.execute(REACTIONS_INDEX)
                self._read_reactions_again()
            except BaseException:
                self.db.execute('ROLLBACK TO team_reactions_table')
                self.db.execute('RELEASE team_reactions_table')
                raise
            self.db.execute('RELEASE team_reactions_table')
        self._read_marks_again()

    def _read_marks_again(self) -> None:
        """Set `bot_only` and `direct` of each member row, and `open` and `direct` of each bot row, from the kept entry
        of the row. The mark `direct` of a member and the mark `direct` of a bot have the same name, so each kind of row
        reads its own fields: member_fields for a member row, bot_fields for a bot row.

        A service of an older version kept the entries with their marks in rows without a column for a mark, and each
        column came with the value 0. The other Mac does not write its entry again, so without this step a team-only
        Mac shows as a person, and its bots as bots that answer nobody.

        The step is a best effort: it must never stop or slow a start. Each error ends it with one log line, and the
        next start tries again. It reads one row at a time, each payload as bytes, so one damaged row does not stop the
        read. It reads each payload with `stored_fields`, the reader of `accept`, and no JSON function of SQLite: such
        a function refuses some JSON that Python reads. A row whose entry does not read, or is over MAX_MARK_PAYLOAD
        bytes, keeps its marks with a log line. So does a row without its kept entry. Only when a row is wrong does it
        write, and it waits at most MARK_WAIT_MS for the write lock.

        The room of a corrected row goes into `changed`, and into `team_marks` in the same savepoint, so the event for
        the phone (`corrected_rooms`) is not lost when the service stops before it can send it."""
        try:
            changes = self._wrong_marks()
            if changes:
                with self.short_wait():
                    self._write_marks(changes)
                self.changed.update(room for _, room, _, _ in changes)
        except Exception as error:
            log.warning('coconut.team_log: the start did not read the marks of the members and bots again (%s). The '
                        'next start tries again.', type(error).__name__)

    def _wrong_marks(self) -> list[tuple[str, str, str, tuple[int, int]]]:
        """Each member row and bot row whose marks differ from its kept entry: (kind, room, key, marks). The key is the
        member ID of a member row and the entry ID of a bot row: the primary key of the row in its room.

        CROSS JOIN keeps the order of the tables: SQLite walks the few rows of the members and bots and finds each entry
        by its key. It does not walk each entry of the store."""
        blob = 'CAST(e.payload AS BLOB)'
        read = f'length({blob}), CASE WHEN length({blob}) <= {MAX_MARK_PAYLOAD} THEN {blob} END'
        cursor = self.db.execute(f"""SELECT 'member', m.room, m.entry, m.member, m.bot_only, m.direct, {read}
                FROM team_members m CROSS JOIN team_entries e ON e.room=m.room AND e.id=m.entry AND e.kind='member'
            UNION ALL SELECT 'bot', b.room, b.entry, b.entry, b.open, b.direct, {read}
                FROM team_bots b CROSS JOIN team_entries e ON e.room=b.room AND e.id=b.entry AND e.kind='bot'""")
        changes = []
        for kind, room, entry, key, first, second, size, payload in cursor:
            fields = stored_fields(kind, payload) if payload is not None else None
            if fields is None:
                log.warning('coconut.team_log room %s: the kept %s entry %s (%d bytes) does not read, so its row keeps '
                            'its marks.', room[:8], kind, entry[:8], size or 0)
                continue
            marks = (int(fields['bot_only']), int(fields['direct'])) if kind == 'member' else (int(fields['open']), int(fields['direct']))
            if marks != (first, second):
                changes.append((kind, room, key, marks))
        return changes

    def _write_marks(self, changes: list[tuple[str, str, str, tuple[int, int]]]) -> None:
        self.db.execute('SAVEPOINT team_marks_again')
        try:
            for kind, room, key, (first, second) in changes:
                if kind == 'member':
                    self.db.execute('UPDATE team_members SET bot_only=?, direct=? WHERE room=? AND member=?',
                                    (first, second, room, key))
                else:
                    self.db.execute('UPDATE team_bots SET open=?, direct=? WHERE room=? AND entry=?',
                                    (first, second, room, key))
            self.db.executemany('INSERT OR IGNORE INTO team_marks (mark) VALUES (?)',
                                [(CORRECTED_MARK + room,) for room in {room for _, room, _, _ in changes}])
        except BaseException:
            self.db.execute('ROLLBACK TO team_marks_again')
            self.db.execute('RELEASE team_marks_again')
            raise
        self.db.execute('RELEASE team_marks_again')

    @contextmanager
    def short_wait(self):
        """Wait at most MARK_WAIT_MS for a lock of another connection, then give the old bound back."""
        before = self.db.execute('PRAGMA busy_timeout').fetchone()[0]
        self.db.execute(f'PRAGMA busy_timeout={MARK_WAIT_MS}')
        try:
            yield
        finally:
            self.db.execute(f'PRAGMA busy_timeout={int(before)}')

    def corrected_waiting(self) -> bool:
        """True when a corrected room waits for its event. A read only: it takes no lock."""
        return self.db.execute('SELECT 1 FROM team_marks WHERE substr(mark, 1, ?)=? LIMIT 1',
                               (len(CORRECTED_MARK), CORRECTED_MARK)).fetchone() is not None

    def corrected_rooms(self) -> list[str]:
        """The rooms whose rows _read_marks_again corrected and whose event did not go out yet. The marks go, and each
        room that this Mac still has goes into `changed`. The caller holds the transaction and tells the phone."""
        marks = [row[0] for row in self.db.execute('SELECT mark FROM team_marks WHERE substr(mark, 1, ?)=?',
                                                   (len(CORRECTED_MARK), CORRECTED_MARK))]
        self.db.executemany('DELETE FROM team_marks WHERE mark=?', [(mark,) for mark in marks])
        rooms = sorted(room for room in (mark[len(CORRECTED_MARK):] for mark in marks) if self.room(room))
        self.changed.update(rooms)
        return rooms

    def _read_chats_again(self) -> None:
        """Read the chats of a store of an older version, one time. That version kept each `chat` entry as an entry of
        an unknown kind, and each message of a named chat in the Team chat, because it ignored the field `chat`. So the
        chats show now, and each message moves to its chat. The pass reads each payload with the reader of an entry
        (read_payload, chat_fields, message_fields), and a message whose fields do not fit goes, as `accept` would not
        show it.

        The person read the Team chat of that version, which had the messages of each chat. So each chat starts at the
        read position of the Team chat, but never past its own newest message."""
        rooms = {room['room']: room for room in self.rooms()}
        rows = self.db.execute("SELECT room, id, first, member, at, payload FROM team_entries WHERE kind='chat'")
        for room, entry, first, member, at, payload in rows.fetchall():
            if room in rooms and member == rooms[room]['admin']:
                read = read_payload(payload.encode())
                self._show_chat(room, dict(id=entry, first=first, at=at), chat_fields(read[1]) if read else None)
        rows = self.db.execute("""SELECT m.room, m.entry, e.payload FROM team_messages m
            JOIN team_entries e ON e.room=m.room AND e.id=m.entry""")
        for room, entry, payload in rows.fetchall():
            read = read_payload(payload.encode())
            message = message_fields(read[1]) if read else None
            if message is None:
                self.db.execute('DELETE FROM team_messages WHERE room=? AND entry=?', (room, entry))
            elif message['chat'] is not None:
                self.db.execute('UPDATE team_messages SET chat=? WHERE room=? AND entry=?',
                                (message['chat'], room, entry))
        self.db.execute("""INSERT OR REPLACE INTO team_chat_reads (room, chat, first)
            SELECT r.room, c.entry, MIN(r.first, (SELECT MAX(m.first) FROM team_messages m
                WHERE m.room=c.room AND m.chat=c.entry))
            FROM team_reads r JOIN team_chats c ON c.room=r.room
            WHERE EXISTS (SELECT 1 FROM team_messages m WHERE m.room=c.room AND m.chat=c.entry)""")

    def _read_reactions_again(self) -> None:
        """Read the `reaction` entries of a store of an older version, one time. That version kept each one as an entry
        of an unknown kind. The pass reads each payload with the reader of an entry.

        A reaction of a direct chat is in a box, and only the helper opens a box. So this pass does not find such a
        reaction, and it shows when its member writes it again."""
        rows = self.db.execute("SELECT room, id, seq, member, payload FROM team_entries WHERE kind='reaction' "
                               'ORDER BY room, seq')
        for room, entry, seq, member, payload in rows.fetchall():
            read = read_payload(payload.encode())
            reaction = reaction_fields(read[1]) if read else None
            if reaction is not None:
                self._show_reaction(room, dict(id=entry, seq=seq, member=member), dict(reaction, peer=None, bot=None))

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
        for typing in (self.typing, self.direct_typing):
            for key in [key for key in typing if key[0] == room]:
                del typing[key]

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
        # A `key` entry that does not fit never gives a key, so the store does not keep it (TeamEpochs reads the rest).
        if fields['kind'] == 'key' and shown is None:
            return REFUSED, 'key'
        # Rule 5. An entry of an unknown kind goes into the store as each other entry, and nothing reads it.
        self.db.execute('''INSERT INTO team_entries (room, id, seq, first, member, kind, rev, at, kept, payload, epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, id) DO UPDATE SET seq=excluded.seq,
            first=excluded.first, kind=excluded.kind, rev=excluded.rev, at=excluded.at, kept=excluded.kept,
            payload=excluded.payload, epoch=excluded.epoch''',
                        (room['room'], entry['id'], entry['seq'], entry['first'], entry['member'], fields['kind'],
                         fields['rev'], entry['at'], int(bool(entry['kept'])), text, opened['epoch']))
        # The relay keeps only the newest write of an entry ID, so an earlier body that waits is gone there.
        self.db.execute('DELETE FROM team_pending WHERE room=? AND id=? AND seq<=?',
                        (room['room'], entry['id'], entry['seq']))
        # A Mac writes its kept entries again with the room key of a new epoch (TeamEpochs). Such a write has the
        # fields of the write that the store has, and it changes nothing that a person sees: the rows, the times and
        # the names stay, and no event goes to the phone. Only the version of a page, the `rev`, moves.
        if existing and existing['kind'] == fields['kind'] and fields['kind'] in WRITTEN_AGAIN and same_fields(
                existing['payload'], fields):
            if fields['kind'] == 'page':
                self.db.execute('UPDATE team_pages SET rev=? WHERE room=? AND entry=?',
                                (fields['rev'], room['room'], entry['id']))
            return STORED, None
        kinds = {fields['kind'], existing['kind'] if existing else None}
        other = entry['member'] != room['member']
        if 'message' in kinds and self._show_message(room['room'], entry, fields['rev'],
                                                     shown if fields['kind'] == 'message' else None):
            self.chatted.add(room['room'])
        # A direct message shows only with `direct`: what TeamSync found in its box before the transaction. A write
        # without it, for example a box that no longer opens, or a `delete` over it, removes the message.
        if 'direct' in kinds:
            self._direct_changed(room['room'], self._show_direct(
                room['room'], entry, fields['rev'], opened.get('direct') if fields['kind'] == 'direct' else None))
            # A new write or a `delete` ends the wait of an earlier write. A new write that must wait waits again.
            self.db.execute('DELETE FROM team_direct_waits WHERE room=? AND entry=?', (room['room'], entry['id']))
            if fields['kind'] == 'direct' and opened.get('wait'):
                self.db.execute('INSERT INTO team_direct_waits (room, entry) VALUES (?, ?)', (room['room'], entry['id']))
        # A reaction: a `reaction` entry in a team chat, or a `direct` entry whose box TeamSync found to be a reaction
        # (`reaction` of `opened`). Each other write over such an entry takes the reaction back. A reaction changes no
        # unread count and no read position, notifies nothing and starts no turn.
        if kinds & {'reaction', 'direct'}:
            if fields['kind'] == 'reaction':
                reaction = dict(shown, peer=None, bot=None) if shown else None
            else:
                reaction = opened.get('reaction') if fields['kind'] == 'direct' else None
            self._show_reaction(room['room'], entry, reaction)
        # A trigger is the first write of a message of another member that this Mac sees. A later write is an edit.
        if self.answers and existing is None and other and fields['kind'] == 'message' and fields['rev'] == 1 and shown:
            self._queue_triggers(room, entry, shown)
        # The same holds for a direct message to a bot of this Mac. A message that waited for its key is history.
        if (self.answers and existing is None and other and fields['kind'] == 'direct' and fields['rev'] == 1
                and opened.get('direct')):
            self._queue_direct_trigger(room, entry, opened['direct'])
        # The typing sign of a bot in a direct chat with the person of this Mac.
        if other and fields['kind'] == 'direct' and opened.get('sign'):
            self._note_direct_typing(room['room'], entry, opened['sign'])
        # An `erase` entry of the admin takes a message of another member out of the chat. Rule 4 checked the writer
        # above. A later write over the `erase` entry does not bring the message back.
        if fields['kind'] == 'erase' and shown:
            self._erase(room['room'], shown['entry'])
        # A `chat` entry of the admin shows a named chat. Rule 4 checked the writer above. A `delete` over it removes the
        # chat with its messages.
        if 'chat' in kinds and self._show_chat(room['room'], entry, shown if fields['kind'] == 'chat' else None,
                                               fresh=True):
            self.changed.add(room['room'])
        # A `remove` entry of the admin takes a member out. Rule 4 checked the writer above.
        if 'remove' in kinds:
            self._show_removal(room, entry, shown if fields['kind'] == 'remove' else None)
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
        # Who types: a `typing` entry of another member starts, renews or ends the typing of its subject. A message of
        # a person ends the typing of that person. A message of a bot does not end the typing of the bot: a bot sends
        # more than one message in a turn, and its Mac writes `on` false at the end of the turn.
        if other and fields['kind'] == 'typing' and shown:
            self._note_typing(room['room'], entry, shown)
        if other and fields['kind'] == 'message' and shown and shown['bot'] is None:
            self._end_typing(room['room'], entry['member'], None)
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
        if self.removed(room, member):
            # A `remove` entry of the admin took the member out. A `member` entry of the store, or a later one, does
            # not bring it back.
            self.db.execute('DELETE FROM team_members WHERE room=? AND member=?', (room, member))
            return
        rows = self.db.execute("SELECT id FROM team_entries WHERE room=? AND member=? AND kind='member' "
                               'ORDER BY seq DESC', (room, member)).fetchall()
        for (entry,) in rows:
            if known and known[0] == entry:
                fields = known[1]
            else:
                payload = self.db.execute('SELECT payload FROM team_entries WHERE room=? AND id=?', (room, entry))
                fields = stored_fields('member', payload.fetchone()[0])
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

    def _show_chat(self, room: str, entry: dict, chat: dict | None, fresh: bool = False) -> bool:
        """Set the row of a `chat` entry, or remove the chat (docs/team-log.md, "Chats of a team"). `chat` is what
        read_entry found valid, or None for a name that does not fit and for a `delete` entry over a chat. True when the
        list of the chats changed.

        A new chat shows the messages of the chat that the store has already, because the entries can come in either
        order. A removed chat takes its messages and its read position with it. The triggers of these messages that did
        not start go, and the turn thread stops a running turn, because its trigger no longer shows. The waiting
        notifications check that the message still shows, so they send nothing. With `fresh`, a chat that starts to
        show notes its fresh messages for a notification (_note_fresh_chat).
        """
        at = relay_time(entry['at'])
        if chat is None or at is None:
            if not self.db.execute('DELETE FROM team_chats WHERE room=? AND entry=?', (room, entry['id'])).rowcount:
                return False
            if self.db.execute('''DELETE FROM team_triggers WHERE room=? AND started=0 AND entry IN (
                    SELECT entry FROM team_messages WHERE room=? AND chat=?)''', (room, room, entry['id'])).rowcount:
                self.triggered.add(room)
            if self.db.execute('DELETE FROM team_messages WHERE room=? AND chat=?', (room, entry['id'])).rowcount:
                self.chatted.add(room)
            self.db.execute('DELETE FROM team_chat_reads WHERE room=? AND chat=?', (room, entry['id']))
            return True
        new = self.chat(room, entry['id']) is None
        changed = self.db.execute('''INSERT INTO team_chats (room, entry, first, name, created_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (room, entry) DO UPDATE SET first=excluded.first, name=excluded.name
            WHERE team_chats.first!=excluded.first OR team_chats.name!=excluded.name''',
                                  (room, entry['id'], entry['first'], chat['name'], at)).rowcount > 0
        if changed and self.db.execute('SELECT 1 FROM team_messages WHERE room=? AND chat=? LIMIT 1',
                                       (room, entry['id'])).fetchone():
            self.chatted.add(room)
        if new and fresh:
            self._note_fresh_chat(room, entry['id'])
        return changed

    def _note_fresh_chat(self, room: str, chat: str) -> None:
        """Note the messages of a chat that starts to show, by the rules of a notification (_note_fresh). The relay
        keeps only the newest write of a `chat` entry, so after a rename the entry comes after the messages of the
        chat, and these messages did not show when this Mac took them. Only a first write (`rev` 1) notifies. Such a
        message did not show before, so _note_fresh did not note it then, and the item of a phone has the entry ID in
        its key, so no message notifies two times."""
        room_row = self.room(room)
        rows = self.db.execute('''SELECT e.id, e.first, e.member, e.at, m.bot FROM team_messages m
            JOIN team_entries e ON e.room=m.room AND e.id=m.entry WHERE m.room=? AND m.chat=? AND e.rev=1
            ORDER BY m.first''', (room, chat)).fetchall()
        for entry, first, member, at, bot in rows:
            self._note_fresh(room_row, dict(id=entry, first=first, member=member, at=at), dict(bot=bot, chat=chat))

    def chats(self, room: str) -> list[dict]:
        """The named chats of the room that this Mac shows, in the order of `first`: `entry`, `first`, `name` and
        `created_at`."""
        return self._rows(CHAT_FIELDS, f'SELECT {", ".join(CHAT_FIELDS)} FROM team_chats WHERE room=? ORDER BY first',
                          (room,))

    def chat(self, room: str, entry) -> dict | None:
        """One named chat that this Mac shows, or None."""
        rows = self._rows(CHAT_FIELDS, f'SELECT {", ".join(CHAT_FIELDS)} FROM team_chats WHERE room=? AND entry=?',
                          (room, entry))
        return rows[0] if rows else None

    def _show_removal(self, room: dict, entry: dict, removal: dict | None) -> None:
        """Apply a `remove` entry of the admin, or a write over one (docs/team-log.md, "Member removal").

        `removal` is what read_entry found valid, or None for an entry that does not fit and for a write of another kind
        over a `remove` entry. The relay deleted the kept entries of the removed member, and no `delete` entry comes for
        them, so this entry is the only sign. The member leaves the member list, so its bots, its pages and the Library
        rows of these hide by the rules that exist. Its messages and its names in `team_names` stay for the history,
        and a direct chat with it becomes read-only. On a Mac for a team only, its messages start no waiting turn.

        A removal of the admin or of this Mac changes nothing: the admin cannot be removed, and the relay tells this
        Mac by a refusal of its token.
        """
        room_id = room['room']
        members = {row[0] for row in self.db.execute('SELECT member FROM team_removed WHERE room=? AND entry=?',
                                                      (room_id, entry['id']))}
        self.db.execute('DELETE FROM team_removed WHERE room=? AND entry=?', (room_id, entry['id']))
        if removal is not None and removal['member'] not in (room['admin'], room['member']):
            self.db.execute('INSERT OR REPLACE INTO team_removed (room, member, entry) VALUES (?, ?, ?)',
                            (room_id, removal['member'], entry['id']))
            members.add(removal['member'])
            if self.db.execute('''DELETE FROM team_triggers WHERE room=? AND started=0 AND entry IN (
                    SELECT entry FROM team_messages WHERE room=? AND member=?)''',
                               (room_id, room_id, removal['member'])).rowcount:
                # The turn thread also checks a running turn, and stops the turn of a member that is out.
                self.triggered.add(room_id)
            # A Mac that reads the room after the removal gets no `member` entry of the member, because the relay
            # deleted it, but it gets the messages. The name of the entry names them. A name that this Mac showed wins.
            name = removal['name']
            if name is not None and self.last_name(room_id, removal['member']) is None:
                self._name(room_id, entry['id'], removal['member'], 'member', name)
        for member in sorted(members):
            self.show_member(room_id, member)
            # The Library shows a bot only while its member is in the list.
            if self.db.execute('SELECT 1 FROM team_bots WHERE room=? AND member=? LIMIT 1', (room_id, member)).fetchone():
                self.touched.add(room_id)

    def _erase(self, room: str, entry: str) -> None:
        """Apply an `erase` entry of the admin (docs/team-log.md, "The admin deletes a message").

        The named message leaves the chat as after a `delete` over it (_show_message), and so it leaves the unread
        count, the notifications that wait, and the triggers that did not start. A running turn of such a trigger
        stops, because the turn thread checks that its trigger still shows. `team_erased` keeps the entry ID, so a
        later write of the message shows nothing, and an `erase` that comes before its message counts too. An `erase`
        of an entry of another kind, for example a `direct` entry, does nothing.
        """
        existing = self.entry(room, entry)
        if existing and existing['kind'] not in ('message', 'delete'):
            return
        self.db.execute('INSERT OR IGNORE INTO team_erased (room, entry) VALUES (?, ?)', (room, entry))
        if existing and self._show_message(room, existing, existing['rev'], None):
            self.chatted.add(room)
        if self.db.execute('DELETE FROM team_triggers WHERE room=? AND entry=? AND started=0', (room, entry)).rowcount:
            self.triggered.add(room)

    def erased(self, room: str, entry: str) -> bool:
        """True when an `erase` entry of the admin took the message out of the chat."""
        return self.db.execute('SELECT 1 FROM team_erased WHERE room=? AND entry=?', (room, entry)).fetchone() is not None

    def removed(self, room: str, member: str) -> bool:
        """True when a `remove` entry of the admin took the member out of the room."""
        return self.db.execute('SELECT 1 FROM team_removed WHERE room=? AND member=?', (room, member)).fetchone() is not None

    def last_name(self, room: str, member: str) -> str | None:
        """The last name that the store showed for a member, also after it left (team_names)."""
        row = self.db.execute("SELECT name FROM team_names WHERE room=? AND member=? AND kind='member' LIMIT 1",
                              (room, member)).fetchone()
        return row[0] if row else None

    # The key change

    def key_change_due(self, room: str) -> bool:
        """True when the room has a `remove` entry that is newer than its newest `key` entry, or a `remove` entry and no
        `key` entry. Only the admin writes both kinds (rule 4). `first` gives the order, because a `key` entry that the
        admin writes again for a member that it missed (TeamEpochs) gets a new `seq` and keeps its `first`."""
        return bool(self.db.execute('''SELECT (SELECT MAX(first) FROM team_entries WHERE room=? AND kind='remove')
            > COALESCE((SELECT MAX(first) FROM team_entries WHERE room=? AND kind='key'), 0)''',
                                    (room, room)).fetchone()[0])

    def key_changes(self, room: str, after: int) -> list[dict]:
        """The `key` entries of the room for an epoch after `after`, in the order of the epochs: `entry`, `seq`,
        `epoch`, `agreement` (the public agreement key of the admin) and `boxes`. For one epoch, only the newest write
        counts. `accept` keeps only a `key` entry whose fields fit."""
        changes = {}
        rows = self.db.execute("SELECT id, seq, payload FROM team_entries WHERE room=? AND kind='key' ORDER BY seq",
                               (room,))
        for entry, seq, payload in rows:
            fields = json.loads(payload)
            if fields['epoch'] > after:
                changes[fields['epoch']] = dict(entry=entry, seq=seq, epoch=fields['epoch'],
                                                agreement=fields['agreement'], boxes=fields['boxes'])
        return [changes[epoch] for epoch in sorted(changes)]

    def member_epochs(self, room: str) -> dict[str, int]:
        """The epoch of the body of the `member` entry of each member in the list."""
        return dict(self.db.execute('''SELECT m.member, e.epoch FROM team_members m
            JOIN team_entries e ON e.room=m.room AND e.id=m.entry WHERE m.room=?''', (room,)).fetchall())

    def older_entries(self, room: str, member: str, epoch: int, limit: int) -> list[dict]:
        """The kept `member`, `bot` and `page` entries of `member` with a body of an epoch before `epoch`, in the
        order of the log, at most `limit`. This Mac writes them again with the room key of `epoch`."""
        return self._rows(('id', 'payload'), f'''SELECT id, payload FROM team_entries WHERE room=? AND member=?
            AND kept=1 AND kind IN ({", ".join("?" * len(WRITTEN_AGAIN))}) AND epoch<? ORDER BY seq LIMIT ?''',
                          (room, member, *WRITTEN_AGAIN, epoch, limit))

    # The invites of the admin

    def add_invite(self, room: str, ticket_hash: str, expires: int) -> None:
        """Keep the hash of the ticket of an invite and its end, so a removal can remove the ticket first."""
        self.db.execute('INSERT OR REPLACE INTO team_invites (room, hash, expires) VALUES (?, ?, ?)',
                        (room, ticket_hash, expires))

    def invites(self, room: str) -> list[tuple[str, int]]:
        """The kept invites of the room: the hash of each ticket and its end, oldest end first."""
        return [tuple(row) for row in self.db.execute(
            'SELECT hash, expires FROM team_invites WHERE room=? ORDER BY expires, hash', (room,))]

    def drop_invite(self, room: str, ticket_hash: str) -> None:
        self.db.execute('DELETE FROM team_invites WHERE room=? AND hash=?', (room, ticket_hash))

    def drop_old_invites(self, now: float) -> None:
        """Forget the invites whose saved end is more than MAX_TICKET_SECONDS in the past. The relay deleted their
        tickets by then, also when its count started later than the count of this Mac."""
        self.db.execute('DELETE FROM team_invites WHERE expires<=?', (int(now) - MAX_TICKET_SECONDS,))

    def _show_bot(self, room: str, entry: dict, profile: dict | None) -> None:
        """Set the row of a `bot` entry: the profile that read_entry found valid for another member, or the profile
        of an own entry as this Mac wrote it. No row when a field does not fit."""
        self.db.execute('DELETE FROM team_pages WHERE room=? AND entry=?', (room, entry['id']))
        if profile is None:
            self.db.execute('DELETE FROM team_bots WHERE room=? AND entry=?', (room, entry['id']))
            return
        self.db.execute('''INSERT INTO team_bots (room, entry, member, name, avatar, color, open, direct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, entry) DO UPDATE SET member=excluded.member,
            name=excluded.name, avatar=excluded.avatar, color=excluded.color, open=excluded.open,
            direct=excluded.direct''',
                        (room, entry['id'], entry['member'], profile['name'], profile['avatar'], profile['color'],
                         int(profile.get('open') is True), int(profile.get('direct') is True)))
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
            # A chat with a bot shows its messages once the `bot` entry comes, and it names the bot.
            if kind == 'bot' and self.db.execute('SELECT 1 FROM team_directs WHERE room=? AND bot=? LIMIT 1',
                                                 (room, entry)).fetchone():
                self.bot_directed.add((room, entry))

    def _names_changed(self, room: str, member: str | None = None) -> None:
        """A new or changed name can change the chat: a message of a bot shows once its `bot` entry comes, and the
        names of the messages, of the quotes and of `last_message` follow team_names. So the chat of a room with
        messages gets an event. A direct chat names its other member, so a new name of `member` changes that chat."""
        if self.db.execute('SELECT 1 FROM team_messages WHERE room=? LIMIT 1', (room,)).fetchone():
            self.chatted.add(room)
        if member and self.db.execute('SELECT 1 FROM team_directs WHERE room=? AND member=? AND bot IS NULL LIMIT 1',
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
            chat = self.db.execute('SELECT member, bot FROM team_directs WHERE room=? AND entry=?',
                                   (room, existing['id'])).fetchone()
            self._direct_changed(room, {tuple(chat)})

    def _show_message(self, room: str, entry: dict, rev: int, message: dict | None) -> bool:
        """Set the row of a `message` entry, or remove it. `message` is what read_entry found valid, or None for a
        message that does not fit and for a `delete` entry over a message. True when the chat changed.

        The times come from the relay: `created_at` is the `at` of the first write that this Mac saw, and `updated_at`
        the `at` of this write. A message is edited when its `rev` is over 1, so each Mac gives the same answer, also a
        Mac that only saw the newest write. A message that the admin erased shows nothing.
        """
        at = relay_time(entry['at'])
        if message is None or at is None or self.erased(room, entry['id']):
            removed = self.db.execute('DELETE FROM team_messages WHERE room=? AND entry=?', (room, entry['id']))
            return removed.rowcount > 0
        self.db.execute('''INSERT INTO team_messages (room, entry, first, member, bot, text, reply_to, mentions,
            created_at, updated_at, edited, chat) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, entry)
            DO UPDATE SET first=excluded.first, bot=excluded.bot, text=excluded.text, reply_to=excluded.reply_to,
            mentions=excluded.mentions, updated_at=excluded.updated_at, edited=excluded.edited, chat=excluded.chat''',
                        (room, entry['id'], entry['first'], entry['member'], message['bot'], message['text'],
                         message['reply_to'], json.dumps(message['mentions']), at, at, int(rev > 1), message['chat']))
        return True

    def _show_direct(self, room: str, entry: dict, rev: int, direct: dict | None) -> set[tuple[str, str | None]]:
        """Set the row of a `direct` entry, or remove it. `direct` is what the box gave this Mac: the other member of
        the chat, the text, the reply, the bot and the page. It is None for a box that does not open for this Mac or
        does not fit, and for a `delete` entry over a direct message. Return the chats that changed, each as the other
        member and the bot (None for a chat of two people).

        The times follow the rule of a message: `created_at` is the `at` of the first write that this Mac saw, and
        `updated_at` the `at` of this write. A message is edited when its `rev` is over 1.
        """
        before = self.db.execute('SELECT member, bot FROM team_directs WHERE room=? AND entry=?',
                                 (room, entry['id'])).fetchone()
        changed = {tuple(before)} if before else set()
        at = relay_time(entry['at'])
        if direct is None or at is None:
            self.db.execute('DELETE FROM team_directs WHERE room=? AND entry=?', (room, entry['id']))
            return changed
        bot, page = direct.get('bot'), direct.get('page')
        self.db.execute('''INSERT INTO team_directs (room, entry, first, writer, member, text, reply_to, created_at,
            updated_at, edited, bot, page) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (room, entry) DO UPDATE SET first=excluded.first, writer=excluded.writer, member=excluded.member, text=excluded.text,
            reply_to=excluded.reply_to, updated_at=excluded.updated_at, edited=excluded.edited, bot=excluded.bot,
            page=excluded.page''',
                        (room, entry['id'], entry['first'], entry['member'], direct['member'], direct['text'],
                         direct['reply_to'], at, at, int(rev > 1), bot, page))
        return changed | {(direct['member'], bot)}

    def _direct_changed(self, room: str, chats) -> None:
        """Note the direct chats that changed, each as the other member and the bot (None for a chat of two people),
        for the event of each kind of chat (TeamSync._team_tell)."""
        for member, bot in chats:
            if bot is None:
                self.directed.add((room, member))
            else:
                self.bot_directed.add((room, bot))

    def _show_reaction(self, room: str, entry: dict, reaction: dict | None) -> None:
        """Set the row of an entry that says a reaction, or delete it (docs/team-log.md, "Reactions"). `reaction` is
        what the newest write of the entry says: the message, the emoji, and `peer` and `bot` of a reaction in a direct
        chat. It is None for a write that does not fit, a box that does not open for this Mac, and a write of another
        kind over the entry: then the entry says no reaction, and its row goes.

        Each row holds the newest write of one entry, and an empty emoji keeps its row with its `seq`. A view takes the
        row with the highest `seq` for each message, member and chat (SHOWN_REACTIONS). So the state depends only on the
        newest write of each entry, and each Mac shows the same reactions in whatever order it applies the entries. The
        chat of a message gets its event only when the reactions that it shows change.
        """
        old = self.db.execute('SELECT message, peer, bot FROM team_reactions WHERE room=? AND entry=?',
                              (room, entry['id'])).fetchone()
        chats = {tuple(old)} if old else set()
        if reaction is not None:
            chats.add((reaction['message'], reaction['peer'], reaction['bot']))
        before = {chat: self._shown_reactions(room, *chat) for chat in chats}
        if reaction is None:
            self.db.execute('DELETE FROM team_reactions WHERE room=? AND entry=?', (room, entry['id']))
        else:
            self.db.execute('''INSERT INTO team_reactions (room, entry, message, member, emoji, seq, peer, bot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, entry) DO UPDATE SET message=excluded.message,
                member=excluded.member, emoji=excluded.emoji, seq=excluded.seq, peer=excluded.peer, bot=excluded.bot
                WHERE excluded.seq >= team_reactions.seq''',
                            (room, entry['id'], reaction['message'], entry['member'], reaction['emoji'], entry['seq'],
                             reaction['peer'], reaction['bot']))
        for chat, shown in before.items():
            if self._shown_reactions(room, *chat) != shown:
                self._reaction_changed(room, *chat)

    def _shown_reactions(self, room: str, message: str, peer: str | None, bot: str | None) -> list[tuple[str, str]]:
        return self.reactions_of(room, [message], peer, bot).get(message, [])

    def _reaction_changed(self, room: str, message: str, peer: str | None, bot: str | None) -> None:
        """Note the chat whose message shows other reactions now, for the event of a change of its messages. A reaction
        on a message that the store does not have changes no chat."""
        if peer is None:
            if self.db.execute('SELECT 1 FROM team_messages WHERE room=? AND entry=?', (room, message)).fetchone():
                self.chatted.add(room)
        elif self.db.execute('SELECT 1 FROM team_directs WHERE room=? AND entry=? AND member=? AND bot IS ?',
                             (room, message, peer, bot)).fetchone():
            self._direct_changed(room, {(peer, bot)})

    def own_reaction(self, room: str, message: str, member: str, peer: str | None = None,
                     bot: str | None = None) -> str:
        """The emoji that the reaction of `member` on a message of a chat shows (`peer` and `bot` as in reactions_of),
        or an empty text."""
        return next((emoji for emoji, who in self._shown_reactions(room, message, peer, bot) if who == member), '')

    def reactions_of(self, room: str, entries, peer: str | None = None,
                     bot: str | None = None) -> dict[str, list[tuple[str, str]]]:
        """The reactions that show on these messages, by entry ID: (emoji, member) in the order of the log. A message of
        a team chat takes only the reactions of `reaction` entries (`peer` None). A message of a direct chat takes only
        the reactions in the boxes of that chat: the other member `peer`, and `bot` for a chat with a bot. So a
        reaction never shows in another chat than the one that it names. One query reads the reactions of a whole page
        (SHOWN_REACTIONS)."""
        result: dict[str, list[tuple[str, str]]] = {}
        for message, emoji, member in self.db.execute(SHOWN_REACTIONS, (room, json.dumps(sorted(set(entries))), peer,
                                                                         bot)):
            result.setdefault(message, []).append((emoji, member))
        return result

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
        # A room has a row in team_reads from the own `member` entry of this Mac on, also for its named chats.
        if self.db.execute('SELECT 1 FROM team_reads WHERE room=?', (room_id,)).fetchone() is None:
            return
        if (entry['first'] <= self.read_position(room_id, message['chat'])
                or self.message(room_id, entry['id']) is None):
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
        if row is None or entry['first'] <= self.direct_read_position(room['room'], row['member'], row['bot']):
            return
        self.fresh_directs.setdefault(room['room'], []).append(entry['id'])

    # Who types in the Team chat

    def _note_typing(self, room: str, entry: dict, typing: dict) -> None:
        """Apply one `typing` entry of another member (docs/team-log.md, "Typing"). `on` true with an `at` of the
        relay under TYPING_FRESH_SECONDS from this clock shows the subject for TYPING_PERSON_SECONDS, or for
        TYPING_BOT_SECONDS for a bot, from now, in the chat of the entry. An older entry changes nothing. `on` false
        ends it."""
        if not typing['on']:
            self._end_typing(room, entry['member'], typing['bot'])
            return
        now = self.clock()
        if abs(now * 1000 - entry['at']) >= TYPING_FRESH_SECONDS * 1000:
            return
        key = (room, entry['member'], typing['bot'])
        current = self.typing.get(key)
        start = current[0] if current and current[1] > now and current[2] == typing['chat'] else now
        self.typing[key] = (start, now + (TYPING_BOT_SECONDS if typing['bot'] else TYPING_PERSON_SECONDS),
                            typing['chat'])
        self.typed.add(room)

    def _note_direct_typing(self, room: str, entry: dict, sign: dict) -> None:
        """Apply the typing sign of a bot in a direct chat (docs/team-log.md, "The Mac for a team only", rule 12). `on`
        true with an `at` of the relay under TYPING_FRESH_SECONDS from this clock shows the bot for TYPING_BOT_SECONDS
        from now. An older sign changes nothing. `on` false ends it. A message of the bot does not end it."""
        key = (room, entry['member'], sign['bot'])
        now = self.clock()
        self._drop_direct_typing(now)
        if not sign['on']:
            if self.direct_typing.pop(key, 0) > now:
                self.typed.add(room)
            return
        if abs(now * 1000 - entry['at']) >= TYPING_FRESH_SECONDS * 1000:
            return
        self.direct_typing[key] = now + TYPING_BOT_SECONDS
        self.typed.add(room)

    def direct_typing_now(self, room: str, member: str, bot: str) -> float | None:
        """The seconds that are left of the typing sign of a bot in its direct chat, or None. An end by time needs no
        event, so this read drops each sign past its end."""
        now = self.clock()
        self._drop_direct_typing(now)
        end = self.direct_typing.get((room, member, bot))
        return None if end is None else end - now

    def _drop_direct_typing(self, now: float) -> None:
        """Drop each typing sign past its end, also a sign whose `bot` names no chat, so the memory stays small."""
        for key in [key for key, end in self.direct_typing.items() if end <= now]:
            del self.direct_typing[key]

    def _end_typing(self, room: str, member: str, bot: str | None) -> None:
        current = self.typing.pop((room, member, bot), None)
        if current and current[1] > self.clock():
            self.typed.add(room)

    def typing_now(self, room: str, chat: str | None = None) -> list[tuple[str, str | None, float]]:
        """Who types in one chat of the room now (None for the Team chat), the earliest start first: the member, the
        bot entry or None, and the seconds that are left. An end by time needs no event, so this read drops the subjects
        past their end."""
        now = self.clock()
        for key in [key for key, (_, end, _) in self.typing.items() if end <= now]:
            del self.typing[key]
        return [(member, bot, end - now) for (_, member, bot), (start, end, _) in
                sorted(((key, value) for key, value in self.typing.items() if key[0] == room and value[2] == chat),
                       key=lambda item: (item[1][0], item[0][1], item[0][2] or ''))]

    # The triggers of the bots of a team-only Mac

    def _queue_triggers(self, room: dict, entry: dict, message: dict) -> None:
        """Queue a turn for each bot of this Mac that the message names or answers (docs/team-log.md, "An agent answers").

        The message has no `bot`, is under TRIGGER_SECONDS old, and its member is in the member list. It names the
        `bot` entry of a bot in `mentions`, or its `reply_to` is a message of that bot. A team-only Mac is no person, so
        a mention of its own `member` entry names a bot too: the one that `_meant_bot` finds. The bot is shared with the
        room, is not archived, and has its chat connected (`chat` of its record). The signature of the entry proves its
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
            # A reply starts a turn only in the chat of the answered message.
            row = self.db.execute('SELECT bot FROM team_messages WHERE room=? AND entry=? AND member=? AND chat IS ?',
                                  (room_id, message['reply_to'], room['member'], message['chat'])).fetchone()
            answered = row[0] if row else None
        answering = self.answering(room_id)
        own = self.member(room_id, room['member'])
        meant = self._meant_bot(room_id, own['name'], answering) if own and own['entry'] in message['mentions'] else None
        for bot, share in answering:
            if share in message['mentions'] or share == answered or bot == meant:
                self._queue(room_id, bot, entry['id'])

    def _queue_direct_trigger(self, room: dict, entry: dict, direct: dict) -> None:
        """Queue a turn for the bot of this Mac that a direct message names (docs/team-log.md, "The Mac for a team
        only", rule 3). The box opened for this Mac, so `to` names it. The message is under TRIGGER_SECONDS old, its
        member is in the member list, and its `bot` is the `bot` entry of a bot of this Mac that answers in the room.
        A direct message names a bot by its `bot` entry only: the mention of the `member` entry of this Mac is a rule
        of the team chat."""
        room_id = room['room']
        if direct['bot'] is None or self.clock() * 1000 - entry['at'] >= TRIGGER_SECONDS * 1000:
            return
        if self.member(room_id, entry['member']) is None:
            return
        for bot, share in self.answering(room_id):
            if share == direct['bot']:
                self._queue(room_id, bot, entry['id'])

    def _queue(self, room_id: str, bot: str, entry: str) -> None:
        """Add a trigger of a bot. The Team chat, the named chats and the direct chats share the bound of MAX_WAITING
        triggers that wait for one bot in one room. Over it, the bot skips the message, with a log line and no text."""
        if self.db.execute('SELECT COUNT(*) FROM team_triggers WHERE room=? AND bot=? AND started=0',
                           (room_id, bot)).fetchone()[0] >= MAX_WAITING:
            log.warning('coconut.team_log room %s: the bot %s has %d messages that wait, so it skips the entry %s.',
                        room_id[:8], bot, MAX_WAITING, entry)
            return
        if self.db.execute('INSERT OR IGNORE INTO team_triggers (room, bot, entry) VALUES (?, ?, ?)',
                           (room_id, bot, entry)).rowcount:
            self.triggered.add(room_id)

    def answering(self, room: str) -> list[tuple[str, str]]:
        """The bots of this Mac that answer in the chat of the room, each with the entry ID of its `bot` entry: shared
        with the room, not archived, and with their chat connected. The `bots` table holds the records."""
        return self.db.execute('''SELECT s.bot, s.entry FROM team_shares s JOIN bots b ON b.id=s.bot
            WHERE s.room=? AND json_extract(b.data, '$.chat')=1 AND COALESCE(json_extract(b.data, '$.archived'), 0)=0
            ORDER BY s.bot''', (room,)).fetchall()

    def _meant_bot(self, room: str, name: str, answering: list[tuple[str, str]]) -> str | None:
        """The bot that a mention of this Mac's `member` entry names: of the bots in `answering`, the one with the name
        of the member (same_name), else the only one. None when the choice is not clear."""
        names = dict(self.db.execute('SELECT entry, name FROM team_bots WHERE room=?', (room,)).fetchall())
        same = [bot for bot, share in answering if same_name(names.get(share, ''), name)]
        if len(same) == 1:
            return same[0]
        return answering[0][0] if len(answering) == 1 else None

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

    def context(self, room: str, bot: str, first: int, limit: int, chat: str | None = None) -> list[dict]:
        """The shown messages of one chat before `first` and after the newest message of the bot (its `bot` entry ID)
        in that chat, at most `limit`, oldest first."""
        last = self.db.execute('SELECT MAX(first) FROM team_messages WHERE room=? AND chat IS ? AND bot=?',
                               (room, chat, bot)).fetchone()[0]
        rows = self._messages('m.chat IS ? AND m.first<? AND m.first>? ORDER BY m.first DESC LIMIT ?',
                              (room, chat, first, last or 0, limit))
        return rows[::-1]

    # The direct chats of the bots of a Mac for a team only. The member of such a chat is the person, and its `bot` is a
    # `bot` entry of this Mac, so SHOWN_DIRECT does not hold here: OWN_BOT_DIRECT does.

    def bot_direct(self, room: str, entry: str) -> dict | None:
        """One message of a direct chat of a person with a bot of this Mac, or None."""
        rows = self._rows(DIRECT_FIELDS, self._direct_sql('d.entry=?', OWN_BOT_DIRECT), (room, entry))
        return rows[0] if rows else None

    def bot_context(self, room: str, member: str, bot: str, first: int, limit: int) -> list[dict]:
        """The messages of the direct chat of `member` with a bot of this Mac (its `bot` entry ID) before `first` and
        after the newest message of the bot there, at most `limit`, oldest first."""
        last = self.db.execute('''SELECT MAX(d.first) FROM team_directs d JOIN team_rooms r ON r.room=d.room
            WHERE d.room=? AND d.member=? AND d.bot=? AND d.writer=r.member''', (room, member, bot)).fetchone()[0]
        rows = self._rows(DIRECT_FIELDS, self._direct_sql(
            'd.member=? AND d.bot=? AND d.first<? AND d.first>? ORDER BY d.first DESC LIMIT ?', OWN_BOT_DIRECT),
                          (room, member, bot, first, last or 0, limit))
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
        the marks `open` and `direct` of a bot, or the bot and the version of a page. `accept` keeps the form in
        columns, so no payload is parsed."""
        bots = self.db.execute('SELECT entry, name, avatar, color, open, direct FROM team_bots WHERE room=? AND member=?',
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
        return self._rows(TEAM_BOT, '''SELECT b.entry, b.member, b.name, b.avatar, b.color, b.open, b.direct
            FROM team_bots b
            JOIN team_rooms r ON r.room=b.room WHERE b.room=? AND b.member!=r.member ORDER BY b.name, b.entry''',
                          (room,))

    # The Team chat

    def messages(self, room: str, before: int | None = None, limit: int = 50, chat: str | None = None) -> list[dict]:
        """The shown messages of one chat of a room (None for the Team chat) before the `first` of `before`, newest
        first, at most `limit`."""
        return self._rows(MESSAGE_FIELDS, *self._page(room, chat, before, limit))

    def _page(self, room: str, chat: str | None, before: int | None, limit: int) -> tuple[str, tuple]:
        """The statement and the values of one page. A page with `before` has `m.first<?` as a plain term, so the
        index team_messages_chat starts its search at `before`, and a page reads about `limit` rows in any chat. A
        term such as `(? IS NULL OR m.first<?)` would make SQLite walk the index from the newest row."""
        if before is None:
            return self._message_sql('m.chat IS ? ORDER BY m.first DESC LIMIT ?'), (room, chat, limit)
        return self._message_sql('m.chat IS ? AND m.first<? ORDER BY m.first DESC LIMIT ?'), (room, chat, before, limit)

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
            m.reply_to, m.mentions, m.created_at, m.updated_at, m.edited, m.chat FROM team_messages m
            LEFT JOIN team_names n ON n.room=m.room AND n.member=m.member AND n.kind='member'
            LEFT JOIN team_names k ON k.room=m.room AND k.entry=m.bot AND k.kind='bot'
            WHERE m.room=? AND {SHOWN_MESSAGE} AND {where}'''

    def names(self, room: str, entries) -> dict[str, tuple[str, str]]:
        """The kind and the member of each entry ID that is a member or a bot of the room in team_names."""
        rows = self.db.execute('''SELECT entry, kind, member FROM team_names WHERE room=?
            AND entry IN (SELECT value FROM json_each(?))''', (room, json.dumps(sorted(set(entries))))).fetchall()
        return {entry: (kind, member) for entry, kind, member in rows}

    def read_position(self, room: str, chat: str | None = None) -> int:
        """The `first` of the newest message that the person read in one chat of the room (None for the Team chat).

        The position of the Team chat starts at the `first` of the own `member` entry of this Mac (team_reads). The
        position of a named chat starts at the later of that `first` and the `first` of the `chat` entry, so the
        history from before a join or before the chat showed does not count as unread. It is 0 for a chat that the
        store does not show."""
        if chat is None:
            row = self.db.execute('SELECT first FROM team_reads WHERE room=?', (room,)).fetchone()
            return row[0] if row else 0
        row = self.db.execute('''SELECT MAX(c.first,
                COALESCE((SELECT first FROM team_chat_reads WHERE room=c.room AND chat=c.entry), 0),
                COALESCE((SELECT e.first FROM team_rooms r JOIN team_entries e ON e.room=r.room
                          AND e.id=json_extract(r.profile, '$.entry') WHERE r.room=c.room), 0))
            FROM team_chats c WHERE c.room=? AND c.entry=?''', (room, chat)).fetchone()
        return row[0] if row else 0

    def mark_read(self, room: str, first: int, chat: str | None = None) -> int | None:
        """Move the read position of one chat of the room to `first`, but never back and never past the newest message
        of that chat. The new position, or None when it stays."""
        newest = self.db.execute('SELECT MAX(first) FROM team_messages WHERE room=? AND chat IS ?',
                                 (room, chat)).fetchone()[0] or 0
        position = min(first, newest)
        if position <= self.read_position(room, chat):
            return None
        if chat is None:
            self.db.execute('''INSERT INTO team_reads (room, first) VALUES (?, ?)
                ON CONFLICT (room) DO UPDATE SET first=excluded.first''', (room, position))
        else:
            self.db.execute('''INSERT INTO team_chat_reads (room, chat, first) VALUES (?, ?, ?)
                ON CONFLICT (room, chat) DO UPDATE SET first=excluded.first''', (room, chat, position))
        return position

    def unread(self, room: str, member: str, chat: str | None = None) -> int:
        """How many shown messages of one chat are after its read position, up to MAX_UNREAD. Only a message of this
        Mac's person counts as read: the person did not write the reply of a bot of this Mac. The LIMIT keeps the count
        to at most MAX_UNREAD rows of the index."""
        return self.db.execute(f'''SELECT COUNT(*) FROM (SELECT 1 FROM team_messages m WHERE m.room=? AND m.chat IS ?
            AND m.first>? AND {UNREAD_MESSAGE} AND {SHOWN_MESSAGE} LIMIT {MAX_UNREAD})''',
                               (room, chat, self.read_position(room, chat), member)).fetchone()[0]

    def names_member(self, room: str, entry: str, member: str) -> bool:
        """True when the shown message `entry` names `member` in its `mentions`."""
        return self.db.execute(f'''SELECT 1 FROM team_messages m WHERE m.room=? AND m.entry=? AND {SHOWN_MESSAGE}
            AND {NAMES_MEMBER}''', (room, entry, member)).fetchone() is not None

    def mention_first(self, room: str, member: str, chat: str | None = None) -> int | None:
        """The `first` of the newest unread message (the rule of `unread`) of one chat that names `member`, or None. The
        inner query walks the index team_messages_chat back from the newest message and stops after MAX_MENTION_ROWS
        rows after the read position. The outer query applies the rules to those rows only."""
        return self.db.execute(MENTION_SQL, (room, chat, self.read_position(room, chat), member, member)).fetchone()[0]

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
        """End the wait of a `direct` entry after its last try. `direct` is what its box gave this Mac: a message, a
        reaction (`{"reaction": ...}`), or None. A message that shows now is history: it changes its chat and notifies
        nothing. A newer write of the entry since the try has its own wait, so this one changes nothing then."""
        current = self.entry(room, entry['id'])
        if current is None or current['seq'] != entry['seq']:
            return
        if not self.db.execute('DELETE FROM team_direct_waits WHERE room=? AND entry=?', (room, entry['id'])).rowcount:
            return
        if direct is not None and 'reaction' in direct:
            self._show_reaction(room, entry, direct['reaction'])
        else:
            self._direct_changed(room, self._show_direct(room, entry, entry['rev'], direct))

    def direct(self, room: str, entry: str) -> dict | None:
        """One direct message that this Mac shows, or None."""
        rows = self._rows(DIRECT_FIELDS, self._direct_sql('d.entry=?'), (room, entry))
        return rows[0] if rows else None

    # A direct chat is the other member and the bot: None for a chat of two people, or the entry ID of the `bot` entry
    # of a chat with a bot. Each query reads only the messages of its own chat (`d.bot IS ?`), and a message with `bot`
    # shows only by the rule of SHOWN_DIRECT.

    def directs(self, room: str, member: str, before: int | None = None, limit: int = 50,
                bot: str | None = None) -> list[dict]:
        """The messages of one direct chat before the `first` of `before`, newest first, at most `limit`. A page with
        `before` has `d.first<?` as a plain term, so the index team_directs_chat starts there."""
        if before is None:
            return self._rows(DIRECT_FIELDS, self._direct_sql(
                'd.member=? AND d.bot IS ? ORDER BY d.first DESC LIMIT ?'), (room, member, bot, limit))
        return self._rows(DIRECT_FIELDS, self._direct_sql(
            'd.member=? AND d.bot IS ? AND d.first<? ORDER BY d.first DESC LIMIT ?'),
                          (room, member, bot, before, limit))

    def directs_of(self, room: str, member: str, entries, bot: str | None = None) -> dict[str, dict]:
        """The messages of these entry IDs in one direct chat, by entry ID, in one query."""
        rows = self._rows(DIRECT_FIELDS, self._direct_sql(
            'd.member=? AND d.bot IS ? AND d.entry IN (SELECT value FROM json_each(?))'),
                          (room, member, bot, json.dumps(sorted(set(entries)))))
        return {row['entry']: row for row in rows}

    def direct_chats(self, room: str) -> list[dict]:
        """The newest message of each direct chat of two people of the room, newest chat first."""
        return self._rows(DIRECT_FIELDS, self._direct_sql('''d.bot IS NULL AND d.first=(SELECT MAX(x.first)
            FROM team_directs x WHERE x.room=d.room AND x.member=d.member AND x.bot IS NULL) ORDER BY d.first DESC'''),
                          (room,))

    def bot_direct_chats(self, room: str) -> list[dict]:
        """The newest message of each direct chat with a bot of the room that shows, newest chat first. A message of
        another member with the same `bot` does not show (SHOWN_DIRECT), so it never takes the place of the newest
        message of the chat."""
        return self._rows(DIRECT_FIELDS, self._direct_sql('''d.bot IS NOT NULL AND d.first=(SELECT MAX(x.first)
            FROM team_directs x WHERE x.room=d.room AND x.bot=d.bot AND x.member=d.member) ORDER BY d.first DESC'''),
                          (room,))

    def bot_member(self, room: str, bot: str) -> str | None:
        """The member of a `bot` entry that the store showed once (team_names), also after a `delete`, or None."""
        row = self.db.execute("SELECT member FROM team_names WHERE room=? AND entry=? AND kind='bot'",
                              (room, bot)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _direct_sql(where: str, shown: str = SHOWN_DIRECT) -> str:
        # The name of the other member and of the bot is the last one that the store showed, also after a `delete`
        # (team_names).
        return f'''SELECT d.entry, d.first, d.writer, d.member, n.name, d.text, d.reply_to, d.created_at, d.updated_at,
            d.edited, d.bot, k.name, d.page FROM team_directs d
            LEFT JOIN team_names n ON n.room=d.room AND n.member=d.member AND n.kind='member'
            LEFT JOIN team_names k ON k.room=d.room AND k.entry=d.bot AND k.kind='bot'
            WHERE d.room=? AND {shown} AND {where}'''

    def direct_read_position(self, room: str, member: str, bot: str | None = None) -> int:
        """The `first` of the newest message that the person read in one direct chat. It starts at 0, so each message
        that arrives counts as unread. A chat with a bot keeps its position in team_bot_direct_reads."""
        if bot is not None:
            row = self.db.execute('SELECT first FROM team_bot_direct_reads WHERE room=? AND bot=?',
                                  (room, bot)).fetchone()
        else:
            row = self.db.execute('SELECT first FROM team_direct_reads WHERE room=? AND member=?',
                                  (room, member)).fetchone()
        return row[0] if row else 0

    def mark_direct_read(self, room: str, member: str, first: int, bot: str | None = None) -> int | None:
        """Move the read position of a direct chat to `first`, but never back and never past its newest message. The
        new position, or None when it stays."""
        newest = self.db.execute(f'''SELECT MAX(d.first) FROM team_directs d WHERE d.room=? AND d.member=?
            AND d.bot IS ? AND {SHOWN_DIRECT}''', (room, member, bot)).fetchone()[0] or 0
        position = min(first, newest)
        if position <= self.direct_read_position(room, member, bot):
            return None
        if bot is not None:
            self.db.execute('''INSERT INTO team_bot_direct_reads (room, bot, first) VALUES (?, ?, ?)
                ON CONFLICT (room, bot) DO UPDATE SET first=excluded.first''', (room, bot, position))
        else:
            self.db.execute('''INSERT INTO team_direct_reads (room, member, first) VALUES (?, ?, ?)
                ON CONFLICT (room, member) DO UPDATE SET first=excluded.first''', (room, member, position))
        return position

    def direct_unread(self, room: str, member: str, bot: str | None = None) -> int:
        """How many messages of the other side are after the read position of the direct chat, up to MAX_UNREAD."""
        return self.db.execute(f'''SELECT COUNT(*) FROM (SELECT 1 FROM team_directs d WHERE d.room=? AND d.member=?
            AND d.bot IS ? AND d.writer=? AND d.first>? AND {SHOWN_DIRECT} LIMIT {MAX_UNREAD})''',
                               (room, member, bot, member, self.direct_read_position(room, member, bot))).fetchone()[0]

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
