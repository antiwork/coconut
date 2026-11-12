"""The key change after a member removal (docs/team-log.md, "Key change").

After the admin removes a member, the room gets a new room key that the removed member never gets. Three parts:

- The admin's Mac makes the change (_team_change_key). It saves the new key in the key file of the room, seals it in a
  box for each other member, writes one kept `key` entry in the old epoch, and then uses the new epoch.
- Each Mac that reads a `key` entry opens its own box and takes the new key (_team_take_keys). The bodies that waited
  for that epoch then open.
- Each Mac writes its own kept entries again with the new key (_team_write_again), so a Mac that joins later can open
  them.

Each step is safe to repeat, and each pass of a room checks each step again, so a stop in the middle loses nothing. A
Mac saves a key in its key file before its store says that the Mac has the epoch. No call to the helper or the relay
and no write of a key file runs in a transaction. No log line has a key or a name.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
from secrets import token_bytes
import time

from . import team_keys
from .team_helper import TeamFormatError
from .team_store import ACTIVE

# The label of the entry ID of a `key` entry (key_entry).
KEY_LABEL = b'coconut room key'
# A Mac writes at most this many kept entries again in one minute in one room. The relay takes 300 writes of one member
# in one minute, so the person can still write in the chat while a Mac with many pages writes them again.
WRITE_AGAIN_LIMIT = 100
WRITE_AGAIN_SECONDS = 60

log = logging.getLogger(__name__)


def key_entry(token: str, room: str, epoch: int) -> str:
    """The entry ID of the `key` entry of an epoch: from the room and the new epoch, so a repeat of a key change writes
    the same entry. The member token of the admin is the key of the HMAC, as for a `remove` entry (team.removal_entry),
    so no other member can write an entry with this ID first."""
    message = KEY_LABEL + bytes.fromhex(room) + epoch.to_bytes(4, 'big')
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()[:32]


def room_key(value) -> bool:
    """True when a value is a room key as the key file keeps it: 32 bytes as standard base64."""
    try:
        return isinstance(value, str) and len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


class TeamEpochs:
    """The steps of a key change. Team mixes this class in, and TeamSync.team_sync_room calls each step."""

    def _init_team_epochs(self):
        # For each room: the write of a `key` entry (entry ID and `seq`) that gave this Mac no key. The Mac tries the
        # same write once only, and a new write of the entry gets its own try.
        self._team_key_tried: dict[str, tuple[str, int]] = {}
        # For each room: the start of the minute of _team_write_again and the writes in it.
        self._team_written_again: dict[str, tuple[float, int]] = {}
        # The rooms with kept entries that this Mac still writes again. TeamSync reads such a room at each pass.
        self._team_busy: set[str] = set()
        # On the admin's Mac, for each room: the members that the helper could not seal a box for in the newest `key`
        # entry. A new write would fail for them again, so they cause none (_team_fill_boxes).
        self._team_unboxed: dict[str, set[str]] = {}

    def _team_forget_epochs(self, room_id: str) -> None:
        for state in (self._team_key_tried, self._team_written_again, self._team_unboxed):
            state.pop(room_id, None)
        self._team_busy.discard(room_id)

    # Each Mac

    def _team_take_keys(self, room_id: str) -> None:
        """Take the room key of each new epoch that a `key` entry of the store gives this Mac, and move the room to it.
        The caller holds the lock of the room.

        A Mac that has the key of an epoch in its key file already moves the room only: the admin made the key, or a
        stop came between the save of the key and the move. Else the Mac opens its own box with the pair key of itself
        and the admin, outside a transaction, and saves the key in its key file before the store moves the room. The
        epoch of a room only moves forward, and one epoch at a time, because each `key` entry is sealed with the key of
        the epoch before its own.

        After a move, the bodies that waited for a key get a new try. One of them can be the next `key` entry, so the
        loop then looks again.
        """
        while not self.stopping.is_set():
            with self.lock:
                room = self.team_store.room(room_id)
                if room is None or room['state'] != ACTIVE:
                    return
                changes = self.team_store.key_changes(room_id, room['epoch'])
            if not changes:
                return
            secrets = team_keys.read(self.root, room_id)
            epoch = room['epoch']
            for change in changes:
                if change['epoch'] != epoch + 1:
                    break
                if str(change['epoch']) not in secrets['keys']:
                    key = self._team_open_key(room, secrets, change)
                    if key is None:
                        break
                    # The key file comes first. A stop before the move leaves a key that the next pass uses.
                    secrets = team_keys.add_key(self.root, room_id, change['epoch'], key)
                epoch = change['epoch']
            if epoch == room['epoch']:
                return
            with self.transaction():
                current = self.team_store.room(room_id)
                if current is None:
                    return
                # The phone shows no epoch, so the move adds no event.
                if current['epoch'] < epoch:
                    self.team_store.update_room(room_id, epoch=epoch)
            log.info('coconut.team_log room %s: this Mac writes with the room key of epoch %d now.', room_id[:8], epoch)
            self._team_retry_waiting(room_id)

    def _team_open_key(self, room: dict, secrets: dict, change: dict) -> str | None:
        """The room key that a `key` entry gives this Mac, or None. Without a box of its own, or with a box that does
        not open, the Mac keeps its epoch and writes one log line, and the entries of the new epoch wait. A new write of
        the entry, for example with a box for a member that the admin missed, gets a new try."""
        room_id = room['room']
        write = (change['entry'], change['seq'])
        if self._team_key_tried.get(room_id) == write:
            return None
        box = change['boxes'].get(room['member'])
        key = None
        if box is not None:
            try:
                key = self.team_helper.open_key(room_id, change['entry'], change['epoch'], secrets['agreement_key'],
                                                room['member'], room['admin'], change['agreement'], box)
            except TeamFormatError as error:
                reason = error.code
            else:
                reason = None if room_key(key) else 'malformed'
        else:
            reason = 'no box'
        if reason is None:
            self._team_key_tried.pop(room_id, None)
            return key
        self._team_key_tried[room_id] = write
        log.warning('coconut.team_log room %s: the key entry %s at seq %d gives this Mac no room key for epoch %d '
                    '(%s). This Mac keeps epoch %d.', room_id[:8], change['entry'], change['seq'], change['epoch'],
                    reason, room['epoch'])
        return None

    def _team_write_again(self, room_id: str) -> None:
        """Write each kept `member`, `bot` and `page` entry of this Mac again with the room key of the epoch of the
        room: the same entry ID, the next `rev` and the same fields. A Mac that joins after a key change gets only the
        new key, so it can open only such a write. The caller holds the lock of the room.

        The store keeps the epoch of the body of each entry, so the Mac finds what is left after a stop, and a repeat
        writes nothing. The Mac writes at most WRITE_AGAIN_LIMIT entries in one minute, and the rest at the next passes.
        """
        with self.lock:
            room = self.team_store.room(room_id)
            if room is None or room['state'] != ACTIVE:
                return
            start, count = self._team_written_again.get(room_id, (0.0, 0))
            if time.monotonic() - start >= WRITE_AGAIN_SECONDS:
                start, count = time.monotonic(), 0
            allowed = WRITE_AGAIN_LIMIT - count
            older = self.team_store.older_entries(room_id, room['member'], room['epoch'], allowed + 1)
        # One entry more than the minute allows tells that the next pass has work.
        if len(older) > allowed:
            self._team_busy.add(room_id)
        else:
            self._team_busy.discard(room_id)
        for row in older[:allowed]:
            fields = {name: value for name, value in json.loads(row['payload']).items() if name not in ('rev', 'at')}
            self._team_write(room, row['id'], True, fields)
            count += 1
            self._team_written_again[room_id] = start, count

    # The admin's Mac

    def _team_change_key(self, room_id: str) -> None:
        """On the admin's Mac: make the key change when a removal is newer than the newest `key` entry, and write the
        newest `key` entry again for a member that it missed. The caller holds the lock of the room.

        The steps of a change, each safe to repeat:

        1. Make the room key of the next epoch, and save it in the key file before anything goes to the relay. A key of
           that epoch in the key file without a `key` entry in the store is from a try that stopped: the Mac uses it.
        2. Seal the key for each other member of the list now.
        3. Write one kept `key` entry in the old epoch. A write without an answer is found by the read before the next
           write (TeamSync._team_settle).
        4. Move the room to the new epoch (_team_take_keys).
        """
        self._team_take_keys(room_id)
        with self.lock:
            room = self.team_store.room(room_id)
            if room is None or room['state'] != ACTIVE or room['admin'] != room['member']:
                return
            due = self.team_store.key_change_due(room_id)
        if not due:
            self._team_fill_boxes(room)
            return
        # A `key` entry that the relay took without an answer must be in the store before the check below.
        self._team_settle(room_id)
        with self.lock:
            room = self.team_store.room(room_id)
            if room is None or room['state'] != ACTIVE:
                return
        secrets = team_keys.read(self.root, room_id)
        epoch = room['epoch'] + 1
        entry_id = key_entry(secrets['token'], room_id, epoch)
        with self.lock:
            written = self.team_store.entry(room_id, entry_id) is not None
        if not written:
            key = secrets['keys'].get(str(epoch))
            if key is None:
                key = base64.b64encode(token_bytes(32)).decode()
                secrets = team_keys.add_key(self.root, room_id, epoch, key)
            self._team_write_key(room, secrets, entry_id, epoch, key)
            log.info('coconut.team_log room %s: this Mac wrote the key entry %s of epoch %d.', room_id[:8], entry_id,
                     epoch)
        self._team_take_keys(room_id)

    def _team_fill_boxes(self, room: dict) -> None:
        """Write the newest `key` entry again when a member of the list has no box in it and has only the key before
        it: a Mac that took its invite before the removal closed the invites, and whose `member` entry came after the
        `key` entry. The new write has a box for each member of the list now. The caller holds the lock of the room."""
        room_id = room['room']
        with self.lock:
            changes = self.team_store.key_changes(room_id, room['epoch'] - 1)
            if not changes or changes[-1]['epoch'] != room['epoch']:
                return
            change = changes[-1]
            unboxed = self._team_unboxed.get(room_id, set())
            missed = [member for member, epoch in self.team_store.member_epochs(room_id).items()
                      if member != room['member'] and member not in change['boxes'] and member not in unboxed
                      and epoch < change['epoch']]
        if not missed:
            return
        secrets = team_keys.read(self.root, room_id)
        self._team_write_key(room, secrets, change['entry'], change['epoch'], secrets['keys'][str(change['epoch'])])
        log.info('coconut.team_log room %s: this Mac wrote the key entry %s again for %d members.', room_id[:8],
                 change['entry'], len(missed))

    def _team_write_key(self, room: dict, secrets: dict, entry_id: str, epoch: int, key: str) -> None:
        """Seal the room key of `epoch` for each other member of the list now, and write the `key` entry in the epoch
        before it. A member whose box the helper cannot seal gets none, and the Mac writes a log line."""
        room_id = room['room']
        with self.lock:
            members = [member for member in self.team_store.members(room_id) if member['member'] != room['member']]
        sealed = self.team_helper.seal_key(room_id, entry_id, epoch, key, secrets['agreement_key'], room['member'],
                                           [{'member': member['member'], 'agreement': member['agreement']}
                                            for member in members])
        boxes = {member['member']: result['box'] for member, result in zip(members, sealed) if 'box' in result}
        self._team_unboxed[room_id] = {member['member'] for member in members} - set(boxes)
        if len(boxes) < len(members):
            log.warning('coconut.team_log room %s: %d members get no box in the key entry %s.', room_id[:8],
                        len(members) - len(boxes), entry_id)
        fields = {'kind': 'key', 'epoch': epoch, 'agreement': json.loads(room['profile'])['agreement'], 'boxes': boxes}
        self._team_write(room, entry_id, True, fields, tries=2, epoch=epoch - 1)
