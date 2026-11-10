"""The copy of the team log on this Mac: rooms, members and entries in coconut.sqlite3 (docs/team-log.md).

Each table has the room ID in its key, because a Mac can be in more than one room.

- `team_rooms`: one row for each room of this Mac: its relay, its name, this Mac's member ID, the admin, and how far
  the sync read (`seq`). `profile` is what this Mac writes about itself in its `member` entry.
- `team_entries`: the newest accepted write of each entry ID, with the payload as its member wrote it.
- `team_members`: the members that the app shows. It is a view of `team_entries`: the newest valid `member` entry
  of each member.
- `team_pending`: bodies that this Mac cannot open yet. It keeps the bytes and tries again later.

`TeamStore.accept` holds the "Rules for a Mac that reads an entry" of the contract. The secrets of a room are not
here: team_keys.py keeps them in a private file. The caller holds the transaction of each write.
"""
from __future__ import annotations

import base64
import json
import re

SCHEMA = '''
CREATE TABLE IF NOT EXISTS team_rooms (room TEXT PRIMARY KEY, relay TEXT NOT NULL, name TEXT NOT NULL,
    member TEXT NOT NULL, admin TEXT NOT NULL, state TEXT NOT NULL, epoch INTEGER NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0, profile TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS team_entries (room TEXT NOT NULL, id TEXT NOT NULL, seq INTEGER NOT NULL,
    first INTEGER NOT NULL, member TEXT NOT NULL, kind TEXT NOT NULL, rev INTEGER NOT NULL, at INTEGER NOT NULL,
    kept INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (room, id));
CREATE TABLE IF NOT EXISTS team_members (room TEXT NOT NULL, member TEXT NOT NULL, name TEXT NOT NULL,
    avatar TEXT NOT NULL, color TEXT NOT NULL, agreement TEXT NOT NULL, entry TEXT NOT NULL,
    PRIMARY KEY (room, member));
CREATE TABLE IF NOT EXISTS team_pending (room TEXT NOT NULL, id TEXT NOT NULL, seq INTEGER NOT NULL,
    first INTEGER NOT NULL, member TEXT NOT NULL, at INTEGER NOT NULL, kept INTEGER NOT NULL, body BLOB NOT NULL,
    reason TEXT NOT NULL, PRIMARY KEY (room, id));
'''
TABLES = ('team_rooms', 'team_entries', 'team_members', 'team_pending')
ROOM_FIELDS = ('room', 'relay', 'name', 'member', 'admin', 'state', 'epoch', 'seq', 'profile', 'created_at')
ENTRY_FIELDS = ('id', 'seq', 'first', 'member', 'kind', 'rev', 'at', 'kept', 'payload')
MEMBER_FIELDS = ('member', 'name', 'avatar', 'color', 'agreement', 'entry')
PENDING_FIELDS = ('id', 'seq', 'first', 'member', 'at', 'kept', 'body', 'reason')

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


def read_payload(data) -> tuple[str, dict] | None:
    """The text and the fields of a payload: one JSON object in UTF-8 with a `kind` and a `rev` from 1. Else None."""
    try:
        text = bytes(data).decode('utf-8')
        fields = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(fields, dict):
        return None
    kind, rev = fields.get('kind'), fields.get('rev')
    if not isinstance(kind, str) or not 0 < len(kind) <= MAX_KIND or type(rev) is not int or rev < 1:
        return None
    return text, fields


def member_fields(fields: dict) -> dict | None:
    """The name, avatar, color and agreement key of a `member` payload, or None when a field does not fit."""
    result = {}
    for name, limit in MEMBER_LIMITS.items():
        value = fields.get(name, '')
        if not isinstance(value, str) or len(value) > limit:
            return None
        result[name] = value
    agreement = fields.get('agreement')
    if not result['name'] or not isinstance(agreement, str) or not AGREEMENT.fullmatch(agreement):
        return None
    # One key has one text: the unused bits of the last character are zero.
    if base64.urlsafe_b64encode(base64.urlsafe_b64decode(agreement + '=')).decode().rstrip('=') != agreement:
        return None
    return dict(result, agreement=agreement)


class TeamStore:
    def __init__(self, db):
        self.db = db

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
        and body as bytes. `opened` is the answer of the helper for the body: `epoch` and `payload`, or `error`.
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
        payload = read_payload(opened.get('payload'))
        if payload is None:
            return DROPPED, 'payload'
        text, fields = payload
        # Rule 3. Only the member of an entry replaces it, and only with a higher `rev`.
        existing = self.entry(room['room'], entry['id'])
        if existing:
            if existing['seq'] == entry['seq']:
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
        if fields['kind'] == 'member' or (existing and existing['kind'] == 'member'):
            self.show_member(room['room'], entry['member'])
        return STORED, None

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

    def show_member(self, room: str, member: str) -> None:
        """Set the row of a member from its `member` entries: the newest one with fields that fit, or no row.

        A member has one `member` entry. When it wrote more, the one with the highest `seq` counts, so each Mac
        shows the same member. A `delete` over that entry takes the member out of the list.
        """
        rows = self.db.execute("SELECT id, payload FROM team_entries WHERE room=? AND member=? AND kind='member' "
                               'ORDER BY seq DESC', (room, member)).fetchall()
        for entry, payload in rows:
            fields = member_fields(json.loads(payload))
            if fields:
                self.db.execute('''INSERT INTO team_members (room, member, name, avatar, color, agreement, entry)
                    VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, member) DO UPDATE SET name=excluded.name,
                    avatar=excluded.avatar, color=excluded.color, agreement=excluded.agreement, entry=excluded.entry''',
                                (room, member, fields['name'], fields['avatar'], fields['color'], fields['agreement'], entry))
                return
        self.db.execute('DELETE FROM team_members WHERE room=? AND member=?', (room, member))
