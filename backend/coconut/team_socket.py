"""Learn of a new entry at once through the socket of each room (docs/team-log.md, "Socket" and "Mac").

The Python standard library has no WebSocket client, so the watch mode of the helper holds the sockets
(scripts/team-helper.swift). One process of the helper watches each active room of this Mac. The backend writes the
rooms to its standard input, one JSON line for each change of the set, and reads one JSON line for each event:

- `seq` with a higher number than the store has wakes the sync for that room. A number that the store has wakes
  nothing, for example the number that the relay sends when a socket opens.
- `open` and `closed` say whether a room has a socket. The timer reads a room with a socket only each
  SOCKET_POLL_SECONDS, because the socket tells of each write of another member. The timer reads a room without one
  every POLL_SECONDS, as before.
- `closed` with `retry` false wakes the sync for the room: the relay answered 401 or 404, or it closed with 1000 or
  1008. The read then finds that the room is `removed` or `gone`, and the next line leaves the room out.

An older helper has no watch mode. It reads to the end of its input before it answers, so it writes no `ready` line
while the input is open. After READY_SECONDS the watcher closes the input, and the older helper answers
`{"error": "malformed"}`. The watcher then keeps the identity of that file and does not start it again until the file
changes, for example after `scripts/team.py build`. The timer reads each room every POLL_SECONDS meanwhile, so the
sync works as before and fails no pass.

The line of rooms holds the member tokens. It goes only to the standard input of the helper. No log line and no error
of this module has a token, a key or the address of a relay.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from . import team_keys
from .team_helper import helper_path
from .team_keys import TeamKeysError
from .team_relay import number
from .team_store import ACTIVE

# The timer reads a room with an open socket this often. The socket tells of each write of another member, so this
# read only covers a socket that died before its next ping showed it. A quiet room then costs the relay one small read
# in 5 minutes instead of one in 30 seconds.
SOCKET_POLL_SECONDS = 300
# A helper with the watch mode answers its first line at once. An older helper does not answer while its input is open.
READY_SECONDS = 10
# The time that a helper gets to end after its input closes, before the watcher stops it.
STOP_SECONDS = 5
# After a helper process ends, the watcher waits 2, 4, 8 seconds and longer, up to this limit, before the next start.
MAX_RESTART_SECONDS = 300
# A process that ran this long resets the wait.
STEADY_SECONDS = 60
# A Mac without a usable helper looks for one again this often.
CHECK_SECONDS = 60
# The longest line that the helper writes is a `closed` event, far below this limit.
MAX_LINE = 4096

log = logging.getLogger(__name__)


class TeamWatcher:
    """One process of the helper in watch mode, and the thread that keeps it running.

    The thread owns the process: it starts it, writes each new line of rooms, starts it again after a wait when it
    ends, and stops it when the service closes. A second thread reads the lines of each process. The callbacks run
    on that reader thread and take the service lock themselves. No lock of the watcher is held while a callback runs,
    and `_lock` guards only the set of rooms with an open socket, so a caller can hold the service lock and ask
    `connected`.
    """

    def __init__(self, root, rooms, seq, refused, stopping: threading.Event):
        self.root = Path(root)
        # rooms() is the list of {room, relay, token} to watch. seq(room, n) and refused(room) get the events.
        self._rooms, self._seq, self._refused = rooms, seq, refused
        self._stopping = stopping
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._connected: set[str] = set()
        self._thread = None
        self._process = None
        self._reader = None
        # The last line of rooms that the process got. It holds the tokens, so it never goes into a log.
        self._line = None
        self._watched = frozenset()
        self._started = 0.0
        self._failures = 0
        self._next_start = 0.0
        # The identity of a helper file without the watch mode: path, inode, size and time of change.
        self._unsupported = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name='coconut-team-socket', daemon=True)
        self._thread.start()

    def refresh(self) -> None:
        """Read the rooms again soon, and send them to the helper when they changed. It takes no lock."""
        self._wake.set()

    def connected(self, room: str) -> bool:
        with self._lock:
            return room in self._connected

    def close(self) -> None:
        """Stop the thread and the process. The caller set `stopping` first."""
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3 * STOP_SECONDS)
        # The thread stops the process itself. This is for a thread that did not end in time.
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    @property
    def running(self) -> bool:
        return self._process is not None and self._reader is not None and self._reader.is_alive()

    def _loop(self) -> None:
        wait = 0
        try:
            while not self._stopping.is_set():
                self._wake.wait(wait)
                self._wake.clear()
                if self._stopping.is_set():
                    break
                try:
                    wait = self._step()
                except Exception:
                    log.exception('coconut.team_log: the watch of the team sockets failed. It tries again later.')
                    self._stop_process()
                    wait = CHECK_SECONDS
        finally:
            self._stop_process()

    def _step(self) -> float | None:
        """Bring the process to the rooms of now. Return the wait before the next step, or None to wait for a call
        of `refresh` (the reader also calls it when the process ends)."""
        if self._process is not None and not self.running:
            self._ended()
        rooms = self._rooms()
        if not rooms:
            self._stop_process()
            return None
        line = json.dumps({'operation': 'watch', 'rooms': rooms}, separators=(',', ':'))
        if self.running:
            if line != self._line:
                self._send(line, rooms)
            return None
        wait = self._next_start - time.monotonic()
        if wait > 0:
            return wait
        path = helper_path(self.root)
        if path is None:
            return CHECK_SECONDS
        identity = file_identity(path)
        if identity is None or identity == self._unsupported:
            return CHECK_SECONDS
        outcome = self._launch(path, line, rooms)
        if outcome == 'old':
            self._unsupported = identity
            log.warning('coconut.team_log: the team helper has no watch mode, so this Mac reads each room on its '
                        'timer. Update Coconut, or run python3 scripts/team.py build.')
            return CHECK_SECONDS
        if outcome == 'failed':
            return self._ended()
        return None

    def _ended(self) -> float:
        """The process ended or did not start. Wait longer after each end in a row, then start it again."""
        lived = time.monotonic() - self._started
        self._stop_process()
        if lived >= STEADY_SECONDS:
            self._failures = 0
        self._failures += 1
        delay = min(2 ** self._failures, MAX_RESTART_SECONDS)
        self._next_start = time.monotonic() + delay
        log.warning('coconut.team_log: the team helper of the sockets stopped. It starts again in %d seconds, and the '
                    'timer reads each room until then.', delay)
        return delay

    def _launch(self, path: Path, line: str, rooms: list[dict]) -> str:
        """Start the helper and send the first line. Return 'ready', 'old' (no watch mode) or 'failed'."""
        try:
            process = subprocess.Popen([str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
        except OSError:
            self._started = time.monotonic()
            return 'failed'
        first = queue.Queue(maxsize=1)
        reader = threading.Thread(target=self._read, args=(process, first), name='coconut-team-socket-read',
                                  daemon=True)
        self._process, self._reader, self._started = process, reader, time.monotonic()
        reader.start()
        answer = None
        if self._send(line, rooms):
            try:
                answer = first.get(timeout=READY_SECONDS)
            except queue.Empty:
                # An older helper reads to the end of its input, then answers that it does not know the operation.
                try:
                    process.stdin.close()
                except OSError:
                    pass
                try:
                    answer = first.get(timeout=STOP_SECONDS)
                except queue.Empty:
                    answer = None
        if isinstance(answer, dict) and answer.get('event') == 'ready':
            return 'ready'
        self._stop_process()
        if isinstance(answer, dict) and 'error' in answer and 'event' not in answer:
            return 'old'
        return 'failed'

    def _send(self, line: str, rooms: list[dict]) -> bool:
        try:
            self._process.stdin.write(line.encode() + b'\n')
            self._process.stdin.flush()
        except (OSError, ValueError):
            # The process ended. The reader sees the end of its output and wakes the thread.
            return False
        self._line = line
        self._watched = frozenset(room['room'] for room in rooms)
        return True

    def _stop_process(self) -> None:
        process, reader = self._process, self._reader
        self._process = self._reader = self._line = None
        self._watched = frozenset()
        with self._lock:
            self._connected.clear()
        if process is None:
            return
        try:
            # The helper ends when its input closes.
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(STOP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if reader is not None:
            reader.join(STOP_SECONDS)
        process.stdout.close()

    def _read(self, process: subprocess.Popen, first: queue.Queue) -> None:
        """Read the lines of one process until it ends. The first line goes to `_launch`, each later line is an event."""
        handed = False
        try:
            for raw in process.stdout:
                try:
                    event = json.loads(raw) if len(raw) <= MAX_LINE else None
                except ValueError:
                    event = None
                if not isinstance(event, dict):
                    continue
                if not handed:
                    handed = True
                    first.put(event)
                elif process is self._process:
                    self._event(event)
        except (OSError, ValueError):
            pass
        finally:
            if not handed:
                first.put(None)
            if process is self._process:
                with self._lock:
                    self._connected.clear()
            self._wake.set()

    def _event(self, event: dict) -> None:
        kind, room = event.get('event'), event.get('room')
        if kind == 'error':
            log.warning('coconut.team_log: the team helper refused the list of rooms. The timer reads them.')
            return
        if not isinstance(room, str) or room not in self._watched:
            return
        try:
            if kind == 'open':
                with self._lock:
                    self._connected.add(room)
                log.info('coconut.team_log room %s: the socket is open.', room[:8])
            elif kind == 'seq' and number(event.get('seq')):
                self._seq(room, event['seq'])
            elif kind == 'closed':
                with self._lock:
                    self._connected.discard(room)
                code, status = event.get('code'), event.get('status')
                log.info('coconut.team_log room %s: the socket closed (code %s, status %s).', room[:8],
                         code if number(code) else '-', status if number(status) else '-')
                if event.get('retry') is False:
                    self._refused(room)
        except Exception:
            log.exception('coconut.team_log room %s: an event of the socket failed.', room[:8])


def file_identity(path: Path) -> tuple | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns


class TeamSocket:
    """The part of Team that uses the sockets: which room the timer reads, and what an event of a socket wakes."""

    def _init_team_socket(self):
        self._team_watcher = TeamWatcher(self.root, self._team_socket_rooms, self._team_socket_seq,
                                         self._team_socket_refused, self.stopping)
        # The rooms that a socket woke, and whether a pass must read each room. A dedicated lock guards them, because
        # team_wake runs in a transaction of the caller. Nothing else is taken while it is held.
        self._team_wakes = threading.Lock()
        self._team_woken: set[str] = set()
        # The first pass after a start reads each room.
        self._team_wake_all = True
        # The monotonic time of the last read of each room that ended without a failure.
        self._team_read_at: dict[str, float] = {}
        self._team_watched: frozenset = frozenset()

    def _start_team_socket(self):
        self._team_watcher.start()

    def _close_team_socket(self):
        self._team_watcher.close()

    def team_socket_open(self, room: str) -> bool:
        """True while the socket of the room is open. The caller can hold the service lock."""
        return self._team_watcher.connected(room)

    def _team_note_wake(self, room: str | None) -> None:
        """Record what a call of team_wake asks for: one room after an event of its socket, else each room."""
        with self._team_wakes:
            if room is None:
                self._team_wake_all = True
            else:
                self._team_woken.add(room)

    def _team_take_wakes(self) -> tuple[bool, set[str]]:
        """What the pass that starts now must read beyond the rooms that are due on the timer."""
        with self._team_wakes:
            wakes = self._team_wake_all, self._team_woken
            self._team_wake_all, self._team_woken = False, set()
        return wakes

    def _team_due(self, room: str, every: bool, woken: set[str]) -> bool:
        """True when this pass reads the room: a room operation, a save or a tool asked for each room, the socket
        woke this room, the last read failed, the room has no open socket, or its last good read is
        SOCKET_POLL_SECONDS old.

        A room waits after a failure (TeamSync._team_failures), and each pass in that wait drops the wakes of its
        socket. So a room with a failure is due as soon as its wait ends, whatever its socket says. A good read
        removes the failure."""
        if every or room in woken or room in self._team_failures or not self.team_socket_open(room):
            return True
        return time.monotonic() - self._team_read_at.get(room, float('-inf')) >= SOCKET_POLL_SECONDS

    def _team_socket_read(self, room: str) -> None:
        self._team_read_at[room] = time.monotonic()

    def _team_socket_watch(self, rooms: list[str]) -> None:
        """Give the watcher the active rooms of a pass when they changed: a join, or a room that is now `removed` or
        `gone`."""
        watched = frozenset(rooms)
        if watched != self._team_watched:
            self._team_watched = watched
            self._team_watcher.refresh()

    def _team_socket_forget(self, room: str) -> None:
        """The person left the room, so its socket goes."""
        self._team_read_at.pop(room, None)
        self._team_watcher.refresh()

    def _team_socket_rooms(self) -> list[dict]:
        """The rooms for the helper: each active room with its relay and its member token. The key files are read
        without the service lock."""
        with self.lock:
            rooms = [(room['room'], room['relay']) for room in self.team_store.rooms() if room['state'] == ACTIVE]
        result = []
        for room, relay in rooms:
            try:
                token = team_keys.read(self.root, room)['token']
            except TeamKeysError:
                # The sync shows this failure for the room. Without its token, the room has no socket.
                continue
            result.append({'room': room, 'relay': relay, 'token': token})
        return result

    def _team_socket_seq(self, room_id: str, seq: int) -> None:
        """The relay has entries up to `seq`. Wake the sync for the room unless the store has them."""
        with self.lock:
            room = self.team_store.room(room_id)
        if room is not None and room['state'] == ACTIVE and room['seq'] < seq:
            self.team_wake(room=room_id)

    def _team_socket_refused(self, room_id: str) -> None:
        """The relay refused the socket for good. A read now finds out whether the room is `removed` or `gone`."""
        self.team_wake(room=room_id)
