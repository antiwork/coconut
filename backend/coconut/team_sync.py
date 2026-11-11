"""Copy the log of each team room from the relay into the store (docs/team-log.md, "Mac").

One thread reads the new entries of each active room after the `seq` that the store saved. The helper opens the
bodies, and TeamStore.accept applies the rules of the contract. The thread also writes the `member` entry of this
Mac when the log has none.

The thread reads on a timer for now. `team_wake` starts a pass at once: a room operation calls it, and the socket of
the contract can call it later, because that socket only says that the log has new entries. A room whose relay does
not answer waits longer after each failure, up to 15 minutes.

No call to the relay or to the helper holds the service lock. The lock of a room keeps two threads from reading or
writing the same room at one time, and it is held for one request, not for a whole pass.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import logging
import sqlite3
import threading
import time

from . import team_keys
from .team_helper import TeamFormatError, TeamHelperError, TeamHelperMissing
from .team_keys import TeamKeysError
from .team_relay import TeamRelayError
from .team_store import ACTIVE, DROPPED, GONE, JOINING, MESSAGES_EVENT, PENDING, REFUSED, REMOVED, SKIPPED, read_entry

POLL_SECONDS = 30
MAX_WAIT_SECONDS = 900
# The thread writes the `member` entry of this Mac again at most this often, when the log does not show it.
ANNOUNCE_SECONDS = 300
# What can fail for one room without a fault of this code: the relay, the helper, or the key file of the room.
ROOM_ERRORS = (TeamRelayError, TeamHelperError, TeamFormatError, TeamKeysError)
# What TeamStore.accept can raise for the data of one entry, when a check of the store misses a value. The Mac drops
# that entry. A database failure, for example a full disk, is not here: it stops the answer, so no entry gets lost.
DATA_ERRORS = (ValueError, TypeError, OverflowError, sqlite3.DataError, sqlite3.InterfaceError)
# The error of a room that failed for a defect of this code. The log has the reason, and the app needs no more.
DEFECT = 'Coconut could not update this team. It tries again later.'
# The event of each transaction that changes what the Team screen of the app shows. GET /v1/updates then has
# `team_changed`.
TEAM_EVENT = 'team.updated'
# What a person reads when a room is not active on this Mac.
INACTIVE = {JOINING: 'This Mac did not finish its join. Join again with the same link.',
            REMOVED: 'This Mac is no longer in the team.',
            GONE: 'This team no longer exists.'}

log = logging.getLogger(__name__)


def utc_now() -> str:
    """The time of this Mac in ISO 8601, as the `at` of a payload."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def encode_payload(fields: dict, rev: int, at: str) -> bytes:
    """The payload of an entry of this Mac. JSON without ASCII escapes, so text outside ASCII does not grow."""
    return json.dumps(dict(fields, rev=rev, at=at), ensure_ascii=False, separators=(',', ':')).encode()


def room_error(error: Exception) -> str:
    """What a person reads about a failure of the helper, the key file or the relay: what happened and what to do,
    without a word of the code. The log of the service keeps the technical reason."""
    if isinstance(error, TeamHelperMissing):
        return 'Update Coconut on this Mac, then try again.'
    if isinstance(error, (TeamHelperError, TeamFormatError)):
        return 'Coconut on this Mac could not finish this step. Try again later.'
    if isinstance(error, TeamKeysError):
        return 'This Mac can no longer read the team. Leave the team on this Mac, then join again.'
    if isinstance(error, TeamRelayError):
        if error.temporary:
            return 'Coconut could not reach its server. Try again later.'
        if error.status == 401:
            return 'This Mac is no longer in the team.'
        if error.status == 404:
            return 'This team no longer exists.'
        return 'The Coconut server refused this step. Try again later.'
    return DEFECT


def member_payload(profile: dict) -> dict:
    """The fields of the `member` entry of this Mac, from the profile of its room."""
    return {'kind': 'member', 'name': profile['name'], 'avatar': profile['avatar'], 'color': profile['color'],
            'agreement': profile['agreement']}


def retry_delay(failures: int) -> int:
    """The seconds that a room waits after this many failures in a row: 60, 120, 240, 480, then 900."""
    return min(POLL_SECONDS * 2 ** min(failures, 10), MAX_WAIT_SECONDS)


def first_key(secrets: dict) -> int:
    """The first epoch for which the key file of a room has a room key."""
    return min(map(int, secrets['keys']), default=0)


class TeamSync:
    def _init_team_sync(self):
        self._team_wake = threading.Event()
        self._team_thread = None
        self._team_locks: dict[str, threading.Lock] = {}
        # For each room with a failure: how many in a row, and the monotonic time of the next try.
        self._team_failures: dict[str, tuple[int, float]] = {}
        # What the last pass did for each room: `synced_at` and `error`. It is the state of this process only.
        self._team_status: dict[str, dict] = {}
        # The rooms whose waiting bodies this process tried again.
        self._team_retried: set[str] = set()
        self._team_announced: dict[str, float] = {}
        # The rooms with a write that got no answer. The relay can have that write, so the next write reads first.
        self._team_unsure: set[str] = set()
        # For each room: the sync error and the refused writes that the app last got an event for (_team_tell_sync).
        self._team_told: dict[str, tuple] = {}
        # The number of the pass that started last and of the pass that ended last. See team_sync_now.
        self._team_passes = threading.Condition()
        self._team_pass_started = self._team_pass_ended = 0

    def _start_team_sync(self):
        self._team_thread = threading.Thread(target=self._team_loop, name='coconut-team-sync', daemon=True)
        self._team_thread.start()

    def _close_team_sync(self):
        """Stop the thread. The caller set `stopping` first. A request to the relay ends after its timeout."""
        self._team_wake.set()
        with self._team_passes:
            self._team_passes.notify_all()
        if self._team_thread:
            self._team_thread.join(timeout=20)

    def team_wake(self, retry: bool = False) -> None:
        """Start a pass now. With `retry`, also the rooms that wait after a failure try again."""
        if retry:
            self._team_failures.clear()
        self._team_wake.set()

    def _team_room_lock(self, room: str) -> threading.Lock:
        return self._team_locks.setdefault(room, threading.Lock())

    def _team_loop(self):
        while not self.stopping.is_set():
            with self._team_passes:
                self._team_pass_started += 1
                number = self._team_pass_started
            try:
                # Each pass, also one without a room, drops the saved answers of the team writes past their time.
                self.team_drop_old_replies()
                self.team_sync_pass()
            except Exception:
                if self.stopping.is_set():
                    return
                log.exception('The sync of the team log failed. The next pass tries again.')
            with self._team_passes:
                self._team_pass_ended = number
                self._team_passes.notify_all()
            self._team_wake.wait(POLL_SECONDS)
            self._team_wake.clear()

    def team_sync_pass(self) -> None:
        """Read the new entries of each active room once. A room that failed waits for its next try."""
        with self.lock:
            rooms = [room['room'] for room in self.team_store.rooms() if room['state'] == ACTIVE]
        for room in rooms:
            if self.stopping.is_set():
                return
            failures, due = self._team_failures.get(room, (0, 0))
            if time.monotonic() < due:
                continue
            try:
                self.team_sync_room(room)
            except Exception as error:
                # Also a defect of this code fails only this room until its next try, so the other rooms go on.
                with self.lock:
                    left = self.team_store.room(room) is None
                if left:
                    # The person left the room while the pass read it.
                    continue
                delay = retry_delay(failures + 1)
                self._team_failures[room] = (failures + 1, time.monotonic() + delay)
                if isinstance(error, ROOM_ERRORS):
                    self._team_status.setdefault(room, {})['error'] = room_error(error)
                    log.warning('coconut.team_log room %s: %s The next try is in %d seconds.', room[:8], error, delay)
                else:
                    self._team_status.setdefault(room, {})['error'] = DEFECT
                    log.exception('coconut.team_log room %s: the sync failed. The next try is in %d seconds.',
                                  room[:8], delay)
            else:
                self._team_failures.pop(room, None)
                self._team_status[room] = {'synced_at': utc_now(), 'error': None}
            self._team_tell_sync(room)

    def _team_tell_sync(self, room_id: str) -> None:
        """Add TEAM_EVENT when the sync error or the refused writes of a room changed: they appear, change or clear.

        The Team screen shows both, and a pass can change them without a change of a member, a bot or the state of
        the room. A pass with the same error or the same refusals as the last event adds none. The state is of this
        process only, as `_team_status` is.

        `_team_told` changes only after the event commits. When the database write fails, the next pass tries the
        event again, so the app never keeps an old error state. A failure fails no other room. A room that the
        person left gets no event and keeps no state here.
        """
        seen = (self._team_status.get(room_id, {}).get('error'),
                tuple(sorted((entry, refusal['reason']) for entry, refusal in self._team_refusals.get(room_id, {}).items())))
        if self._team_told.get(room_id, (None, ())) == seen:
            return
        try:
            with self.transaction():
                # The person can have left the room while the pass read it.
                present = self.team_store.room(room_id) is not None
                if present:
                    self._event(TEAM_EVENT, {'team': room_id})
        except sqlite3.Error:
            log.exception('coconut.team_log room %s: the event of the sync state did not save. The next pass tries '
                          'again.', room_id[:8])
            return
        if present:
            self._team_told[room_id] = seen
        else:
            self._team_told.pop(room_id, None)

    def team_sync_room(self, room: str) -> None:
        """Read the room until the store has each entry of the relay. Then make sure that the log shows this Mac, and
        that it has the bots and the pages that this Mac shares (TeamPages._team_publish)."""
        lock = self._team_room_lock(room)
        if room not in self._team_retried:
            with lock:
                self._team_retry_waiting(room)
            self._team_retried.add(room)
        more = True
        while more and not self.stopping.is_set():
            with lock:
                more = self._team_read(room)
        with lock:
            self._team_announce(room)
            self._team_publish(room)

    def _team_read(self, room_id: str) -> bool:
        """Read one answer of the relay after the saved `seq`, and store it. Return True when the relay has more."""
        with self.lock:
            room = self.team_store.room(room_id)
        if room is None or room['state'] != ACTIVE:
            return False
        secrets = team_keys.read(self.root, room_id)
        try:
            answer = self.team_relay.read(room['relay'], room_id, secrets['token'], room['seq'])
        except TeamRelayError as error:
            if error.status not in (401, 404):
                raise
            # 401: the token of this Mac is no longer a member's. 404: the relay has no such room. The copy stays.
            state = REMOVED if error.status == 401 else GONE
            with self.transaction():
                # The Library shows a room only while it is active, so the phone reads the list again.
                if self.team_store.shows(room_id):
                    self._event('document.updated', {'team': room_id})
                self._event(TEAM_EVENT, {'team': room_id})
                self.team_store.update_room(room_id, state=state)
            log.warning('coconut.team_log room %s: the relay answered %d. The room is now %s on this Mac.',
                        room_id[:8], error.status, state)
            return False
        entries = answer['entries']
        if not entries:
            return False
        opened = self.team_helper.open(room_id, secrets['keys'], entries)
        entries = [dict(entry, body=base64.b64decode(entry['body'])) for entry in entries]
        # The checks of the text run here, before the transaction and without the lock of the service.
        opened = [self._team_prepare(room_id, entry, result) for entry, result in zip(entries, opened)]
        first_epoch = first_key(secrets)
        results = []
        with self.transaction():
            current = self.team_store.room(room_id)
            # Another thread left the room, or already stored this answer.
            if current is None or current['state'] != ACTIVE or current['seq'] != room['seq']:
                return False
            for entry, result in zip(entries, opened):
                results.append((entry, *self._team_accept(current, entry, result, first_epoch)))
            self.team_store.update_room(room_id, seq=entries[-1]['seq'])
            self._team_tell(room_id)
        self._team_report(room_id, results)
        return answer['last'] > entries[-1]['seq']

    def _team_retry_waiting(self, room_id: str) -> None:
        """Try again the bodies that this Mac could not open. An update of Coconut starts the service again, so this
        runs once for each room after each start."""
        after = 0
        while not self.stopping.is_set():
            with self.lock:
                room = self.team_store.room(room_id)
                waiting = self.team_store.pending(room_id, after) if room else []
            if not waiting:
                return
            after = waiting[-1]['seq']
            secrets = team_keys.read(self.root, room_id)
            opened = [self._team_prepare(room_id, entry, result)
                      for entry, result in zip(waiting, self.team_helper.open(room_id, secrets['keys'], [
                          dict(entry, body=base64.b64encode(entry['body']).decode()) for entry in waiting]))]
            first_epoch = first_key(secrets)
            results = []
            with self.transaction():
                current = self.team_store.room(room_id)
                if current is None:
                    return
                for entry, result in zip(waiting, opened):
                    outcome, reason = self._team_accept(current, entry, result, first_epoch)
                    if outcome != PENDING:
                        self.team_store.drop_pending(room_id, entry['id'])
                    results.append((entry, outcome, reason))
                self._team_tell(room_id)
            self._team_report(room_id, results)

    def _team_prepare(self, room_id: str, entry: dict, opened: dict) -> dict:
        """read_entry for one entry, the part of the reading rules without the database. The caller holds no lock of
        the service.

        This part touches no database, so an error can come only from the data of the entry or from a defect of the
        code. Each error drops only this entry, so one entry never stops a room. _team_accept keeps DATA_ERRORS,
        because a database failure there must drop no entry.
        """
        try:
            return read_entry(opened)
        except Exception as error:
            log.exception('coconut.team_log room %s: the store cannot take the entry %s at seq %d.', room_id[:8],
                          entry['id'], entry['seq'])
            return {'error': type(error).__name__}

    def _team_accept(self, room: dict, entry: dict, opened: dict, first_epoch: int) -> tuple[str, str | None]:
        """TeamStore.accept for one entry, in a savepoint of the transaction of the caller.

        A data error of one entry drops that entry, and the savepoint takes back what `accept` wrote of it. Without
        this, the answer would roll back, `seq` would stay, and each later pass would fail on the same entry.
        """
        self.db.execute('SAVEPOINT team_entry')
        try:
            result = self.team_store.accept(room, entry, opened, first_epoch)
        except DATA_ERRORS as error:
            self.db.execute('ROLLBACK TO team_entry')
            log.exception('coconut.team_log room %s: the store cannot take the entry %s at seq %d.', room['room'][:8],
                          entry['id'], entry['seq'])
            result = DROPPED, type(error).__name__
        self.db.execute('RELEASE team_entry')
        return result

    def _team_tell(self, room_id: str) -> None:
        """Add one event when the entries of this transaction changed what the phone shows of the room.

        The phone learns of a local document from its `document.*` event in GET /v1/updates, so a change of the team
        pages or bots of the Library uses the same type. A change of the members or of the bots of the team adds
        TEAM_EVENT for the Team screen, and a change of the chat adds MESSAGES_EVENT. Each type is in the `known` set
        of GET /v1/updates, so an event does not make a phone of today read the whole state again. The caller holds
        the transaction.
        """
        if room_id in self.team_store.touched:
            self.team_store.touched.discard(room_id)
            self._event('document.updated', {'team': room_id})
        if room_id in self.team_store.changed:
            self.team_store.changed.discard(room_id)
            self._event(TEAM_EVENT, {'team': room_id})
        if room_id in self.team_store.chatted:
            self.team_store.chatted.discard(room_id)
            self._event(MESSAGES_EVENT, {'team': room_id})

    def _team_report(self, room_id: str, results: list) -> None:
        """Write a log line for each entry that the Mac dropped or refused, and one for those that it skipped or
        keeps. An ID is no secret: the relay sees each one."""
        for entry, outcome, reason in results:
            if outcome in (DROPPED, REFUSED):
                log.warning('coconut.team_log room %s: %s the entry %s at seq %d of member %s (%s).', room_id[:8],
                            outcome, entry['id'], entry['seq'], entry['member'][:8], reason)
        for outcome, text in ((PENDING, 'keeps %d bodies that it cannot open yet'),
                              (SKIPPED, 'skipped %d entries from before it joined')):
            count = sum(result[1] == outcome for result in results)
            if count:
                log.warning('coconut.team_log room %s: this Mac ' + text + '.', room_id[:8], count)

    def _team_announce(self, room_id: str, force: bool = False) -> None:
        """Write the `member` entry of this Mac when the store shows no member for it.

        A Mac writes this entry after it makes or joins a room. This check also writes it after a stop between the
        join and the write, and after a leave that the relay did not answer.
        """
        with self.lock:
            room = self.team_store.room(room_id)
            shown = room is not None and self.team_store.member(room_id, room['member']) is not None
        if room is None or room['state'] != ACTIVE or shown:
            return
        last = self._team_announced.get(room_id)
        if not force and last is not None and time.monotonic() - last < ANNOUNCE_SECONDS:
            return
        self._team_announced[room_id] = time.monotonic()
        profile = json.loads(room['profile'])
        self._team_write(room, profile['entry'], True, member_payload(profile))

    def _team_write(self, room: dict, entry_id: str, kept: bool, fields: dict, tries: int = 1) -> None:
        """Seal an entry of this Mac, write it to the relay, and put it into the store.

        A write with an entry ID that the store has replaces that entry, with the next `rev`. The caller holds the
        lock of the room.
        """
        secrets = team_keys.read(self.root, room['room'])
        key = secrets['keys'].get(str(room['epoch']))
        if key is None:
            raise TeamKeysError('This Mac has no room key for its epoch.')
        self._team_settle(room['room'])
        with self.lock:
            existing = self.team_store.entry(room['room'], entry_id)
        payload = encode_payload(fields, (existing['rev'] if existing else 0) + 1, utc_now())
        body = self.team_helper.seal(room['room'], entry_id, room['epoch'], key, secrets['member_key'], payload)
        try:
            position = self.team_relay.write(room['relay'], room['room'], secrets['token'], entry_id, kept, body, tries=tries)
        except TeamRelayError as error:
            if error.status == 0:
                self._team_unsure.add(room['room'])
            raise
        entry = dict(position, id=entry_id, member=room['member'], at=int(time.time() * 1000), kept=kept, body=b'')
        opened = read_entry({'epoch': room['epoch'], 'payload': payload})
        with self.transaction():
            current = self.team_store.room(room['room'])
            if current is not None:
                # The sync reads this write again later. The store then has it already.
                self.team_store.accept(current, entry, opened)
                self._team_tell(room['room'])

    def _team_settle(self, room_id: str) -> None:
        """Read the room to its end when a write of this Mac got no answer. The relay can have that write. Without
        this read, the next write could repeat its `rev`, or a leave could miss its `member` entry. The caller holds
        the lock of the room."""
        if room_id in self._team_unsure:
            while self._team_read(room_id):
                pass
            self._team_unsure.discard(room_id)
