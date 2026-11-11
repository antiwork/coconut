"""The bots and the pages that a Mac shares with its teams (docs/team-log.md, "Bots and pages").

A Mac has two ways to share, one for each kind of Mac:

- A person's Mac (Service): the bots are the person's. The person shares a page with a team by one act on the page
  (`PUT /v1/team/rooms/<room>/pages/<document>`), and stops with `DELETE` on the same path. The bot of the page then
  needs an author entry in the room: its share row comes with the first shared page of the bot there and goes with the
  last one (TeamStore.share_page and stop_page). A bot cannot share: a save that names an audience gets an error
  (Documents._page_audience).
- A Mac that runs Coconut for a team only (team_only.py): its bots are the team's. A tool shares a bot with a room
  (`POST /v1/team/rooms/<room>/bots`), and each page of the bot with the audience "team" goes to each room of the bot.

The Mac keeps one kept `bot` entry for each bot in the log of the room, and one kept `page` entry for each shared
document. A save or a share changes only the store and calls team_wake, because it holds a transaction and no call to
the relay or the helper holds the lock of the service. The sync pass then makes the log equal to what this Mac wants
to share (TeamPages._team_publish). It derives both sides each time, from the bots, the documents, the shares and the
own entries of the store, so it keeps no second copy of that state.

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
# The longest bot ID or document ID that a share takes, so a long value does not reach SQLite. Each is a UUID.
MAX_BOT_ID = 200
# The answer of the routes of a bot share on a person's Mac. An app of today still shows "Add one of your bots", and
# `coconut team add-bot` prints the same text.
PERSONAL_BOTS = ('The bots of this Mac stay yours, so a team does not get them. To share a page with the team, open it '
                 'in the Library of the Coconut app.')
NO_PAGE_SHARE = ('This Mac runs Coconut for a team only, so each page of a bot goes to the teams of the bot. Save the '
                 'page with audience "team".')

log = logging.getLogger(__name__)


def team_id(room: str, entry: str) -> str:
    return f't-{room}-{entry}'


def team_ids(value) -> tuple[str, str] | None:
    """The room ID and the entry ID of a team page ID, or None for another ID."""
    match = TEAM_ID.fullmatch(value) if isinstance(value, str) else None
    return match.groups() if match else None


def bot_entry(bot: dict, open: bool = False) -> dict:
    """The fields of the `bot` entry of a bot of this Mac.

    A bot can have a longer name, avatar or color than a `member` entry takes, so the entry gets the start of each.
    A lone surrogate becomes U+FFFD, because UTF-8 cannot encode it and the other Macs refuse it.

    `open` marks a bot that answers each member in the team chat: only a team-only Mac writes it, for a bot whose
    record has `chat` true (team_only.py). A bot of a person answers only that person, so the entry of a full Mac has
    no `open`.
    """
    fields = {'kind': 'bot'}
    for name, limit in MEMBER_LIMITS.items():
        value = bot.get(name)
        value = value if isinstance(value, str) else ''
        fields[name] = ''.join('�' if '\ud800' <= character <= '\udfff' else character
                               for character in value[:limit])
    if open:
        fields['open'] = True
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


def page_audience(teams: list[dict]) -> dict:
    """The fields of a document of this Mac about its teams: `teams` (each active room of the page, as `room` and
    `name`), and `audience` for an app of today, which reads only that field."""
    return {'audience': 'team' if teams else 'private', 'teams': teams}


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


def team_page(db, document_id: str, current: str, *, content: bool = False) -> dict:
    """One team page that the Library shows, or the reason for the phone. A page past its end answers as an ended
    local document does. `current` is the time of the store."""
    from .documents import expired
    from .service_base import ServiceError
    store = TeamStore(db)
    row = store.page(*team_ids(document_id))
    if row is None:
        raise ServiceError(404, 'Document was not found.')
    if expired(row, current):
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
        """Share a bot of a Mac that runs Coconut for a team only with a room. The next pass writes its `bot` entry and
        its team pages. On a person's Mac, the person shares a page and never a bot (team_share_page)."""
        from .service_base import ServiceError
        from .team_sync import INACTIVE, TEAM_EVENT
        if self.library_reader:
            raise ServiceError(409, PERSONAL_BOTS)
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
        """Stop sharing a bot of a Mac that runs Coconut for a team only with a room. The next pass replaces its entries
        with `delete` entries. On a person's Mac the author entry of a bot goes with its last shared page."""
        from .service_base import ServiceError
        from .team_sync import TEAM_EVENT
        if self.library_reader:
            raise ServiceError(409, PERSONAL_BOTS)
        self._team_room(room_id)
        with self.transaction():
            if self.team_store.unshare(room_id, bot_id):
                self._event(TEAM_EVENT, {'team': room_id})
        self.team_wake()
        return self._team_answer(room_id)

    def team_share_page(self, room_id: str, document_id: str) -> dict:
        """PUT /v1/team/rooms/<room>/pages/<document>: the person shares a page of a bot of this Mac with a room. The
        page goes to the room with its bot as the author, and so does each later version. A second share changes
        nothing. The next pass writes the entries."""
        from .documents import expired
        from .service_base import ServiceError
        from .team_sync import INACTIVE
        if not self.library_reader:
            raise ServiceError(409, NO_PAGE_SHARE)
        self._team_room(room_id)
        with self.transaction():
            # The room and the page are checked in the transaction of the share, so neither can go in between.
            room = self.team_store.room(room_id)
            if room is None:
                raise ServiceError(404, 'This Mac is not in this team.')
            if room['state'] != ACTIVE:
                raise ServiceError(409, INACTIVE[room['state']])
            document = self._team_document(document_id)
            bot = self._team_bot(document['bot_id'])
            if bot is None or bot.get('_deleted') or expired(document, self._now()):
                raise ServiceError(404, 'Document was not found.')
            if bot.get('archived'):
                raise ServiceError(409, 'The bot of this page is archived. Restore the bot, then share the page.')
            content = self.db.execute('SELECT content FROM document_versions WHERE document_id=? AND version=?',
                                      (document['id'], document['version'])).fetchone()
            problem = payload_problem(page_entry(dict(WIDEST, key=document['key'], title=document['title'],
                                                      format=document['format'], summary=document.get('summary')),
                                                 content[0] if content else '', WIDEST_BOT))
            if problem:
                raise ServiceError(413, f'Coconut did not share the page. {problem}')
            self._team_page_events(room_id, document, *self.team_store.share_page(
                room_id, document['id'], bot['id'], token_hex(16)))
            answer = self._team_shared_document(document)
        self.team_wake()
        return {'document': answer}

    def team_stop_page(self, room_id: str, document_id: str) -> dict:
        """DELETE /v1/team/rooms/<room>/pages/<document>: the person stops sharing a page with a room. The next pass
        removes the page from the room, and the author entry of its bot when the bot has no other shared page there.
        A stop of a page that the room does not have changes nothing and answers as a stop that worked."""
        from .service_base import ServiceError
        if not self.library_reader:
            raise ServiceError(409, NO_PAGE_SHARE)
        self._team_room(room_id)
        with self.transaction():
            document = self._team_document(document_id)
            self._team_page_events(room_id, document, *self.team_store.stop_page(room_id, document['id'],
                                                                                document['bot_id']))
            answer = self._team_shared_document(document)
        self.team_wake()
        return {'document': answer}

    def _team_document(self, document_id) -> dict:
        """A document of this Mac for a share or a stop. A team page of another Mac is not one. The caller holds the
        transaction."""
        from .service_base import ServiceError
        row = None
        if unicode_text(document_id) and 0 < len(document_id) <= MAX_BOT_ID:
            row = self.db.execute('SELECT data FROM documents WHERE id=?', (document_id,)).fetchone()
        if row is None:
            raise ServiceError(404, 'Document was not found.')
        return json.loads(row[0])

    def _team_page_events(self, room_id: str, document: dict, page: bool, bot: bool) -> None:
        """The events of a share or a stop: the Library shows the teams of a page, and the Team screen shows the bots
        of a team. A share or a stop that changes nothing adds no event. The caller holds the transaction."""
        from .team_sync import TEAM_EVENT
        if page:
            self._event('document.updated', {'id': document['id'], 'team': room_id})
        if bot:
            self._event(TEAM_EVENT, {'team': room_id})

    def _team_shared_document(self, document: dict) -> dict:
        """A document as GET /v1/documents lists it, with its teams. The caller holds the service lock, or it is a read
        view with its own connection, so the store of the connection reads the shares."""
        return dict(document, **page_audience(TeamStore(self.db).shared_pages().get(document['id'], [])))

    def _team_forget_page(self, document: dict) -> None:
        """Remove the shares of a document that goes. The author entry of its bot goes with its last shared page. The
        caller holds the transaction."""
        rooms = self.team_store.rooms_of_page(document['id'])
        for room in rooms:
            self._team_page_events(room, document, *self.team_store.stop_page(room, document['id'],
                                                                             document['bot_id']))
        if rooms:
            self.team_wake()

    def _team_migrate_page_shares(self) -> None:
        """The one step from the bots that a person shared to the pages that a person shares, on a person's Mac.

        Before, a bot that the person added to a team sent each page with the audience "team" to that team. Each such
        page stays shared with each of these teams, with the same entry IDs, so the log needs no new write. Then the
        audience leaves the document, because only a share makes a page a team page now. A bot without a shared page
        loses its share row, and the next pass replaces its `bot` entry with a `delete` entry. After its first run the
        step finds nothing to do. The caller holds the transaction.
        """
        for (data,) in self.db.execute("SELECT data FROM documents WHERE json_extract(data, '$.audience') IS NOT NULL"
                                       ).fetchall():
            document = json.loads(data)
            if document.pop('audience') == 'team':
                for room in self.team_store.rooms_of(document['bot_id']):
                    self.db.execute('INSERT OR IGNORE INTO team_page_shares (room, document) VALUES (?, ?)',
                                    (room, document['id']))
            self._put('documents', document)
        for room, bot in self.db.execute('SELECT room, bot FROM team_shares').fetchall():
            self.team_store.drop_idle_bot(room, bot)

    def _team_opens(self, bot: dict) -> bool:
        """True for a bot that answers each member in the team chat, so its entry has `open`: a bot of a team-only
        Mac whose record has `chat` true. The step that connects the bot to its agent sets `chat`. A bot of a person
        answers only that person, so a full Mac never writes the mark."""
        return getattr(self, 'team_only', False) is True and bot.get('chat') is True

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
                     member=row['member'], member_name=names[row['member']], this_mac=False, open=bool(row['open']))
                for row in self.team_store.bots(room['room']) if row['member'] in names]
        if room['member'] in names:
            for bot_id, (entry, bot) in self._team_live_bots(room['room']).items():
                fields = bot_entry(bot)
                bots.append(dict(id=team_id(room['room'], entry), name=fields['name'], avatar=fields['avatar'],
                                 color=fields['color'], member=room['member'], member_name=names[room['member']],
                                 this_mac=True, bot_id=bot_id, open=self._team_opens(bot)))
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
        room_id = room['room']
        live = self._team_live_bots(room_id)
        shares = {bot_id: entry for bot_id, (entry, _) in live.items()}
        bots = {bot_id: bot for bot_id, (_, bot) in live.items()}
        current = self._now()
        if self.library_reader:
            rows = self.db.execute('''SELECT d.data FROM documents d JOIN team_page_shares s ON s.document=d.id
                WHERE s.room=?''', (room_id,))
        else:
            rows = self.db.execute("SELECT data FROM documents WHERE json_extract(data, '$.audience')='team'")
        documents = [document for document in (json.loads(row[0]) for row in rows)
                     if document['bot_id'] in bots and not expired(document, current)]
        if self.library_reader:
            # On a person's Mac a bot is the author of its shared pages, so its entry stays only while one shows.
            authors = {document['bot_id'] for document in documents}
            bots = {bot_id: bot for bot_id, bot in bots.items() if bot_id in authors}
        entries = self.team_store.page_entries(room_id)
        added = {document['id']: token_hex(16) for document in documents if document['id'] not in entries}
        written = self.team_store.written(room_id, room['member'])
        # A document that is gone, and whose entry the log no longer has, needs its entry ID no more.
        gone = [document_id for document_id, entry in entries.items() if entry not in written
                and not self.db.execute('SELECT 1 FROM documents WHERE id=?', (document_id,)).fetchone()]
        entries.update(added)
        wanted = {}
        for bot_id, bot in bots.items():
            fields = bot_entry(bot, open=self._team_opens(bot))
            wanted[shares[bot_id]] = (('bot', fields['name'], fields['avatar'], fields['color'],
                                       self._team_opens(bot)), {'bot_id': bot_id}, fields)
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
