"""The bots and the pages that a Mac shares with its teams (docs/team-log.md, "Bots and pages").

The person shares a bot of this Mac with a room (`POST /v1/team/rooms/<room>/bots`). The Mac then keeps one kept
`bot` entry for the bot in the log of the room, and one kept `page` entry for each document of the bot with the
audience "team". A save changes only the document and calls team_wake, because a save holds a transaction and no call
to the relay or the helper holds the lock of the service. The sync pass then makes the log equal to what this Mac
wants to share (TeamPages._team_publish). It derives both sides each time, from the bots, the documents and the own
entries of the store, so it keeps no second copy of that state.

The Library of the phone also shows the team pages of the other Macs (team_library and team_page). TeamStore keeps
the rows that it reads.
"""
from __future__ import annotations

import json
import logging
import re
from secrets import token_hex

from .team_relay import MAX_NUMBER, TeamRelayError
from .team_store import ACTIVE, MEMBER_LIMITS, TeamStore, unicode_text
from .team_sync import encode_payload

# The relay takes a body of at most 1 MiB. The body is the payload and 97 bytes: the version, the epoch, the nonce,
# the tag and the signature.
MAX_PAYLOAD = 1024 * 1024 - 97
# The longest values that a page entry can get after its save. The check of a save uses them.
WIDEST = {'version': MAX_NUMBER, 'expires_at': '0000-00-00T00:00:00Z'}
WIDEST_BOT = '0' * 32
WIDEST_AT = '0000-00-00T00:00:00.000000Z'
# The ID that the phone gets for a team page or a team bot: "t-", the room ID, "-", the entry ID. It is unique for
# each entry of each room and safe in a URL path. The ID of a local document is a UUID, which never starts with "t".
TEAM_ID = re.compile(r't-([a-f0-9]{64})-([a-f0-9]{32})')
# The longest bot ID that a share takes, so a long value does not reach SQLite. A bot ID of this Mac is a UUID.
MAX_BOT_ID = 200

log = logging.getLogger(__name__)


def team_id(room: str, entry: str) -> str:
    return f't-{room}-{entry}'


def team_ids(value) -> tuple[str, str] | None:
    """The room ID and the entry ID of a team page ID, or None for another ID."""
    match = TEAM_ID.fullmatch(value) if isinstance(value, str) else None
    return match.groups() if match else None


def bot_entry(bot: dict) -> dict:
    """The fields of the `bot` entry of a bot of this Mac.

    A bot can have a longer name, avatar or color than a `member` entry takes, so the entry gets the start of each.
    A lone surrogate becomes U+FFFD, because UTF-8 cannot encode it and the other Macs refuse it.
    """
    fields = {'kind': 'bot'}
    for name, limit in MEMBER_LIMITS.items():
        value = bot.get(name)
        value = value if isinstance(value, str) else ''
        fields[name] = ''.join('�' if '\ud800' <= character <= '\udfff' else character
                               for character in value[:limit])
    return fields


def page_entry(document: dict, content: str, bot: str) -> dict:
    """The fields of the `page` entry of a document. `bot` is the entry ID of the `bot` entry of its bot."""
    return {'kind': 'page', 'bot': bot, 'key': document['key'], 'title': document['title'],
            'format': document['format'], 'summary': document.get('summary'), 'content': content,
            'version': document['version'], 'expires_at': document.get('expires_at')}


def payload_problem(fields: dict) -> str | None:
    """Why the payload of an entry cannot go to the relay, or None. It takes the longest `rev` and `at`."""
    try:
        size = len(encode_payload(fields, MAX_NUMBER, WIDEST_AT))
    except UnicodeEncodeError:
        return 'The page has text that is not valid Unicode.'
    if size > MAX_PAYLOAD:
        return (f'The page is too large for the team: it needs {size} bytes, and the limit is {MAX_PAYLOAD}. '
                'Keep less data in the page.')
    return None


def team_document(row: dict) -> dict:
    """A team page as the Library of the phone gets it: the fields of a local document, an ID for its room and
    entry, and its team. `version` is the `rev` of the entry, and the times come from the relay."""
    return {'id': team_id(row['room'], row['entry']), 'bot_id': team_id(row['room'], row['bot']), 'key': row['key'],
            'title': row['title'], 'format': row['format'], 'summary': row['summary'], 'version': row['rev'],
            'created_at': row['created_at'], 'updated_at': row['updated_at'], 'expires_at': row['expires_at'],
            'size': row['size'], 'pinned': bool(row['pinned']), 'team': {'room': row['room'], 'name': row['team']}}


def team_bot(row: dict) -> dict:
    """A team bot as the Library of the phone gets it, with its team and the member of the Mac that runs it."""
    return {'id': team_id(row['room'], row['entry']), 'name': row['name'], 'avatar': row['avatar'],
            'color': row['color'], 'team': {'room': row['room'], 'name': row['team']},
            'member': {'id': row['member'], 'name': row['member_name']}}


def team_library(db, current: str) -> tuple[list[dict], list[dict]]:
    """The team pages and the team bots for GET /v1/documents."""
    pages, bots = TeamStore(db).library(current)
    return [team_document(row) for row in pages], [team_bot(row) for row in bots]


def team_page(db, document_id: str, *, content: bool = False) -> dict:
    """One team page that the Library shows, or the reason for the phone. A page past its end answers as an ended
    local document does."""
    from .documents import expired
    from .service import ServiceError, now
    store = TeamStore(db)
    row = store.page(*team_ids(document_id))
    if row is None:
        raise ServiceError(404, 'Document was not found.')
    if expired(row, now()):
        raise ServiceError(404, 'This document ended.')
    page = team_document(row)
    if content:
        page['content'] = store.content(row['room'], row['entry'])
    return page


class TeamPages:
    """The bots that this Mac shares, and the sync step that writes their entries. Team mixes this class in."""

    def _init_team_pages(self):
        # For each room: the writes that the relay refused, by entry ID, each with the form that it refused. The Mac
        # does not send that form again. A new form, for example a new version of the document, tries again.
        self._team_refusals: dict[str, dict[str, dict]] = {}

    def team_share(self, room_id: str, data: dict) -> dict:
        """Share a bot of this Mac with a room. The next pass writes its `bot` entry and its team pages."""
        from .service import ServiceError
        from .team_sync import INACTIVE, TEAM_EVENT
        room = self._team_room(room_id)
        if room['state'] != ACTIVE:
            raise ServiceError(409, INACTIVE[room['state']])
        bot_id = data.get('bot_id')
        if not unicode_text(bot_id) or not 0 < len(bot_id) <= MAX_BOT_ID:
            raise ServiceError(400, 'Choose a bot of this Mac.')
        with self.transaction():
            bot = self._team_bot(bot_id)
            if bot is None or bot.get('_deleted'):
                raise ServiceError(404, 'This bot is no longer on this Mac.')
            if bot.get('archived'):
                raise ServiceError(409, 'The bot is archived. Restore it, then add it to the team.')
            if self.team_store.room(room_id) is None:
                raise ServiceError(404, 'This Mac is not in this team.')
            if self.team_store.share(room_id, bot_id, token_hex(16)):
                self._event(TEAM_EVENT, {'team': room_id})
        self.team_wake()
        return self._team_answer(room_id)

    def team_unshare(self, room_id: str, bot_id: str) -> dict:
        """Stop sharing a bot with a room. The next pass replaces its entries with `delete` entries."""
        from .team_sync import TEAM_EVENT
        self._team_room(room_id)
        with self.transaction():
            if self.team_store.unshare(room_id, bot_id):
                self._event(TEAM_EVENT, {'team': room_id})
        self.team_wake()
        return self._team_answer(room_id)

    def _team_bot(self, bot_id: str) -> dict | None:
        row = self.db.execute('SELECT data FROM bots WHERE id=?', (bot_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _team_live_bots(self, room_id: str) -> dict[str, tuple[str, dict]]:
        """The bots that this Mac shares with a room and that exist and are not archived, each with the entry ID of
        its `bot` entry. The caller holds the service lock."""
        live = {}
        for bot_id, entry in self.team_store.shares(room_id).items():
            bot = self._team_bot(bot_id)
            if bot and not bot.get('archived') and not bot.get('_deleted'):
                live[bot_id] = entry, bot
        return live

    def _team_bots(self, room: dict, members: list[dict]) -> list[dict]:
        """The bots of the team as a person sees them, for GET /v1/team/rooms. The caller holds the service lock.

        As in the Library, a bot shows only while the room is active and its member is in the member list. A bot of
        another Mac has the ID of its team bot in GET /v1/documents. A bot of this Mac also has its `bot_id`, and
        its profile comes from the bot of this Mac, so a change shows before the next pass writes it.
        """
        if room['state'] != ACTIVE:
            return []
        names = {member['member']: member['name'] for member in members}
        bots = [dict(id=team_id(room['room'], row['entry']), name=row['name'], avatar=row['avatar'], color=row['color'],
                     member=row['member'], member_name=names[row['member']], this_mac=False)
                for row in self.team_store.bots(room['room']) if row['member'] in names]
        if room['member'] in names:
            for bot_id, (entry, bot) in self._team_live_bots(room['room']).items():
                fields = bot_entry(bot)
                bots.append(dict(id=team_id(room['room'], entry), name=fields['name'], avatar=fields['avatar'],
                                 color=fields['color'], member=room['member'], member_name=names[room['member']],
                                 this_mac=True, bot_id=bot_id))
        # The bots follow the order of the members, then their names.
        order = {member: index for index, member in enumerate(names)}
        return sorted(bots, key=lambda bot: (order[bot['member']], bot['name'], bot['id']))

    def _team_publish(self, room_id: str) -> None:
        """Make the log of the room equal to what this Mac shares. The caller holds the lock of the room.

        A write that the relay refuses for the entry itself, for example a page over the limit of a body or the
        limit of kept entries, does not stop the other writes. The Mac keeps the refusal in memory until the form of
        the entry changes, and the sync state of the room shows it. A failure of the relay or of the helper stops the
        pass of the room, as a read does.
        """
        with self.lock:
            room = self.team_store.room(room_id)
            if room is None or room['state'] != ACTIVE:
                return
            steps, added, gone = self._team_steps(room)
            # Most passes change no entry ID, so they open no write transaction.
            if added or gone:
                with self.transaction():
                    for document_id, entry in added.items():
                        self.team_store.add_page_entry(room_id, document_id, entry)
                    for document_id in gone:
                        self.team_store.drop_page_entry(room_id, document_id)
        refusals = self._team_refusals.setdefault(room_id, {})
        forms = {entry: form for entry, form, _, _ in steps}
        for entry in [entry for entry, refusal in refusals.items() if forms.get(entry) != refusal['form']]:
            del refusals[entry]
        for entry, form, label, source in steps:
            if entry in refusals:
                continue
            fields = self._team_fields(form, source)
            if fields is None:
                continue
            problem = payload_problem(fields)
            try:
                if problem is None:
                    self._team_write(room, entry, form[0] != 'delete', fields)
            except TeamRelayError as error:
                # 507 says that the kept entries are full. That holds until this Mac deletes some, so it is no
                # temporary failure here.
                if error.status in (401, 404) or (error.temporary and error.status != 507):
                    raise
                problem = f'The relay refused the entry with status {error.status}.'
            if problem:
                refusals[entry] = dict(label, entry=entry, kind=form[0], form=form, reason=problem)
                log.warning('coconut.team_log room %s: this Mac did not write the %s entry %s. %s', room_id[:8],
                            form[0], entry, problem)

    def _team_steps(self, room: dict) -> tuple[list[tuple], dict[str, str], list[str]]:
        """The writes that make the log equal to what this Mac shares, each as (entry ID, form, label, source). Also
        the new entry IDs of documents, and the documents whose entry ID has no more use.

        The form is what tells whether the entry of the log is current: the profile of a bot, or the bot and the
        version of a page. A save of a document always gives a new version. The caller holds the service lock.
        """
        from .documents import expired
        from .service import now
        room_id = room['room']
        live = self._team_live_bots(room_id)
        shares = {bot_id: entry for bot_id, (entry, _) in live.items()}
        bots = {bot_id: bot for bot_id, (_, bot) in live.items()}
        current = now()
        documents = [document for document in (json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM documents WHERE json_extract(data, '$.audience')='team'"))
            if document['bot_id'] in bots and not expired(document, current)]
        entries = self.team_store.page_entries(room_id)
        added = {document['id']: token_hex(16) for document in documents if document['id'] not in entries}
        written = self.team_store.written(room_id, room['member'])
        # A document that is gone, and whose entry the log no longer has, needs its entry ID no more.
        gone = [document_id for document_id, entry in entries.items() if entry not in written
                and not self.db.execute('SELECT 1 FROM documents WHERE id=?', (document_id,)).fetchone()]
        entries.update(added)
        wanted = {}
        for bot_id, bot in bots.items():
            fields = bot_entry(bot)
            wanted[shares[bot_id]] = (('bot', fields['name'], fields['avatar'], fields['color']), {'bot_id': bot_id},
                                      fields)
        for document in documents:
            wanted[entries[document['id']]] = (('page', shares[document['bot_id']], document['version']),
                                               {'document': document['id'], 'key': document['key']}, document)
        steps = [(entry, ('delete',), {}, {'kind': 'delete'}) for entry in written if entry not in wanted]
        steps += [(entry, form, label, source) for entry, (form, label, source) in wanted.items()
                  if written.get(entry) != form]
        return steps, added, gone

    def _team_fields(self, form: tuple, source: dict) -> dict | None:
        """The fields of one write. A page reads the content of its version now, and gets None when that version
        is gone: the document changed after the steps, and the next pass writes the new version."""
        if form[0] != 'page':
            return source
        with self.lock:
            row = self.db.execute('SELECT content FROM document_versions WHERE document_id=? AND version=?',
                                  (source['id'], source['version'])).fetchone()
        return page_entry(source, row[0], form[1]) if row else None
