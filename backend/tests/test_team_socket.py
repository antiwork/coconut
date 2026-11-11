"""The socket of each team room on a Mac: the watcher of the helper process and what its events do
(coconut/team_socket.py).

A stub helper stands in for the watch mode of scripts/team-helper.swift. It writes each line that it gets into
`lines.jsonl` next to itself, and it writes to its output each line that the test adds to `out.jsonl`. So the test
plays the relay. scripts/test_team_log.py runs the real helper with the relay code in Miniflare.
"""
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
import unittest
from unittest import mock

from coconut import team_keys, team_socket
from coconut.team_helper import HELPER_ENVIRONMENT
from team_fakes import RELAY, TeamMacs

STUB = '''#!{python}
import json, os, pathlib, sys, threading, time
folder = pathlib.Path(__file__).parent
mode = (folder / 'mode').read_text().strip()
with (folder / 'starts').open('a') as starts:
    starts.write(f'{{os.getpid()}}\\n')
if mode == 'old':
    # A helper of before the watch mode: it reads to the end of its input, and it does not know the operation.
    sys.stdin.read()
    sys.stdout.write(json.dumps({{'error': 'malformed'}}))
    sys.exit(0)

def play():
    sent = 0
    while True:
        path = folder / 'out.jsonl'
        lines = path.read_text().splitlines() if path.exists() else []
        for line in lines[sent:]:
            sys.stdout.write(line + '\\n')
            sys.stdout.flush()
        sent = len(lines)
        time.sleep(.02)

ready = False
for line in sys.stdin:
    with (folder / 'lines.jsonl').open('a') as lines:
        lines.write(line)
    if not ready:
        ready = True
        sys.stdout.write(json.dumps({{'event': 'ready', 'version': 1}}) + '\\n')
        sys.stdout.flush()
        threading.Thread(target=play, daemon=True).start()
(folder / 'ended').write_text(str(os.getpid()))
'''


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A child that ended stays until its parent waits for it. The watcher waits for each process that it stops.
    return True


class TeamSocketTests(TeamMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stub = Path(self.temp.name) / 'stub' / 'team-helper'
        self.stub.parent.mkdir()
        self.stub.write_text(STUB.format(python=sys.executable))
        self.stub.chmod(0o700)
        self.mode('watch')
        for patcher in (mock.patch.dict(os.environ, {HELPER_ENVIRONMENT: str(self.stub)}),
                        mock.patch.multiple(team_socket, READY_SECONDS=1, STOP_SECONDS=2, CHECK_SECONDS=.2)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.room = self.create()
        self.room_id = self.room['room']
        self.join(self.second, self.invite(self.room)['link'])
        self.token = team_keys.read(self.second.root, self.room_id)['token']
        self.wakes = []
        wake = self.second.team_wake

        def record(retry=False, room=None):
            self.wakes.append(room)
            wake(retry=retry, room=room)
        self.second.team_wake = record

    def mode(self, value: str) -> None:
        (self.stub.parent / 'mode').write_text(value)

    def emit(self, **event) -> None:
        with (self.stub.parent / 'out.jsonl').open('a') as out:
            out.write(json.dumps(event) + '\n')

    def lines(self) -> list[dict]:
        path = self.stub.parent / 'lines.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def starts(self) -> list[int]:
        path = self.stub.parent / 'starts'
        return [int(line) for line in path.read_text().split()] if path.exists() else []

    def wait(self, check, seconds: float = 10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(.02)
        self.fail(f'{check} stayed false for {seconds} seconds.')

    def watched(self) -> list[str]:
        lines = self.lines()
        return sorted(room['room'] for room in lines[-1]['rooms']) if lines else None

    def start(self):
        self.second._start_team_socket()
        self.wait(lambda: self.watched() == [self.room_id])

    def open(self):
        self.emit(event='open', room=self.room_id)
        self.wait(lambda: self.second.team_socket_open(self.room_id))

    def reads(self) -> int:
        return sum(request[0] == 'read' for request in self.relay.requests)

    def test_the_helper_gets_each_active_room_with_its_relay_and_token(self):
        self.start()
        self.assertEqual(self.lines(), [{'operation': 'watch', 'rooms': [
            {'room': self.room_id, 'relay': RELAY, 'token': self.token}]}])
        self.assertEqual(len(self.starts()), 1)
        self.assertFalse(self.rooms(self.second, sync=False)[0]['sync']['socket'])
        self.open()
        self.assertTrue(self.rooms(self.second, sync=False)[0]['sync']['socket'])

    def test_a_new_seq_wakes_its_room_and_a_seq_that_the_store_has_wakes_nothing(self):
        self.start()
        self.open()
        seq = self.rooms(self.second)[0]['sync']['seq']
        # The relay sends the number of the room when the socket opens. The store has it.
        self.emit(event='seq', room=self.room_id, seq=seq)
        # A room that the helper does not watch, and a number that is not one, change nothing.
        self.emit(event='seq', room='f' * 64, seq=seq + 5)
        self.emit(event='seq', room=self.room_id, seq='9')
        self.emit(event='seq', room=self.room_id, seq=seq + 1)
        self.wait(lambda: self.wakes)
        time.sleep(.2)
        self.assertEqual(self.wakes, [self.room_id])
        self.assertIn(self.room_id, self.second._team_woken)

    def test_the_timer_reads_a_room_with_a_socket_only_at_the_long_interval(self):
        self.start()
        # The first pass after the start reads each room.
        before = self.reads()
        self.second.team_sync_pass()
        self.assertEqual(self.reads() - before, 1)
        self.open()
        before = self.reads()
        self.second.team_sync_pass()
        self.assertEqual(self.reads(), before)
        # A wake of the socket reads the room, and so does a wake for each room, for example after a save.
        for room in (self.room_id, None):
            self.second.team_wake(room=room)
            self.second.team_sync_pass()
            self.assertEqual(self.reads() - before, 1)
            before = self.reads()
        # The long interval reads the room once more.
        self.second._team_read_at[self.room_id] -= team_socket.SOCKET_POLL_SECONDS
        self.second.team_sync_pass()
        self.assertEqual(self.reads() - before, 1)
        # Without a socket the timer reads the room on each pass, as before.
        self.emit(event='closed', room=self.room_id, code=1006, retry=True)
        self.wait(lambda: not self.second.team_socket_open(self.room_id))
        before = self.reads()
        self.second.team_sync_pass()
        self.assertEqual(self.reads() - before, 1)
        self.assertEqual(self.wakes, [self.room_id, None])

    def test_a_room_whose_woken_read_failed_reads_as_soon_as_its_wait_ends(self):
        self.start()
        self.second.team_sync_pass()
        self.open()
        # The socket wakes the room after a good read, and the read fails.
        self.second.team_wake(room=self.room_id)
        self.relay.down = True
        with self.assertLogs('coconut.team_sync', 'WARNING'):
            self.second.team_sync_pass()
        self.assertIn(self.room_id, self.second._team_failures)
        # More events of the socket come during the wait. Each pass skips the room and drops the wake.
        self.relay.down = False
        before = self.reads()
        for _ in range(2):
            self.second.team_wake(room=self.room_id)
            self.second.team_sync_pass()
        self.assertEqual(self.reads(), before)
        # The wait ends. The next pass of the timer reads the room at once, not after SOCKET_POLL_SECONDS.
        failures, _ = self.second._team_failures[self.room_id]
        self.second._team_failures[self.room_id] = (failures, time.monotonic() - 1)
        self.assertTrue(self.second.team_socket_open(self.room_id))
        self.second.team_sync_pass()
        self.assertEqual(self.reads() - before, 1)
        self.assertNotIn(self.room_id, self.second._team_failures)
        self.assertIsNone(self.rooms(self.second, sync=False)[0]['sync']['error'])
        # After the good read, the timer waits for the socket again.
        before = self.reads()
        self.second.team_sync_pass()
        self.assertEqual(self.reads(), before)

    def test_a_change_of_the_rooms_sends_a_new_line(self):
        self.start()
        other = self.create(self.third, name='Other Team', member_name='Edsger')
        self.join(self.second, self.invite(other, self.third)['link'])
        # The pass sees the new active room and gives it to the helper.
        self.second.team_sync_pass()
        self.wait(lambda: self.watched() == sorted([self.room_id, other['room']]))
        # The admin removes the second Mac. The next read finds the room `removed`, and the helper drops it.
        keys = team_keys.read(self.first.root, self.room_id)
        self.relay.remove_member(RELAY, self.room_id, keys['token'], self.joined_member())
        with self.assertLogs('coconut.team_sync', 'WARNING'):
            self.second.team_sync_pass()
        self.second.team_sync_pass()
        self.wait(lambda: self.watched() == [other['room']])
        # A leave drops the room too, and a Mac without an active room stops the helper.
        self.leave(self.second, other)
        self.wait(lambda: (self.stub.parent / 'ended').exists())
        self.wait(lambda: not self.second._team_watcher.running)
        self.assertEqual(len(self.starts()), 1)

    def joined_member(self) -> str:
        with self.second.lock:
            return self.second.team_store.room(self.room_id)['member']

    def test_a_refused_socket_wakes_the_room_so_that_the_read_finds_out_why(self):
        self.start()
        self.open()
        self.emit(event='closed', room=self.room_id, status=401, retry=False)
        self.wait(lambda: self.wakes)
        self.assertEqual(self.wakes, [self.room_id])
        self.assertFalse(self.second.team_socket_open(self.room_id))

    def test_a_process_that_dies_starts_again_after_a_wait(self):
        self.start()
        self.open()
        (pid,) = self.starts()
        with self.assertLogs('coconut.team_socket', 'WARNING') as logs:
            os.kill(pid, signal.SIGKILL)
            self.wait(lambda: not self.second.team_socket_open(self.room_id))
            self.wait(lambda: len(self.starts()) == 2)
        self.assertIn('It starts again in 2 seconds', '\n'.join(logs.output))
        self.wait(lambda: len(self.lines()) == 2)
        self.assertEqual(self.lines()[0], self.lines()[1])

    def test_a_close_of_the_service_stops_the_process(self):
        self.start()
        (pid,) = self.starts()
        self.second.close()
        # The helper ends when its input closes, and the watcher waited for it.
        self.assertEqual((self.stub.parent / 'ended').read_text(), str(pid))
        self.assertFalse(alive(pid))
        self.assertFalse(self.second._team_watcher._thread.is_alive())

    def test_an_older_helper_falls_back_to_the_timer(self):
        self.mode('old')
        with self.assertLogs('coconut.team_socket', 'WARNING') as logs:
            self.second._start_team_socket()
            self.wait(lambda: self.second._team_watcher._unsupported)
            # The watcher does not start the same file again.
            time.sleep(.6)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('has no watch mode', logs.output[0])
        self.assertFalse(self.second._team_watcher.running)
        # Each pass reads the room without an error, as before.
        for _ in range(2):
            before = self.reads()
            view = self.rooms(self.second)[0]['sync']
            self.assertEqual((self.reads() - before, view['error'], view['socket']), (1, None, False))
        # An update of the helper brings the watch mode.
        self.mode('watch')
        self.stub.write_text(self.stub.read_text() + '\n')
        self.wait(lambda: self.watched() == [self.room_id])

    def test_no_log_line_and_no_error_has_a_secret(self):
        keys = team_keys.read(self.second.root, self.room_id)
        secrets = [keys['token'], keys['member_key'], keys['agreement_key'], *keys['keys'].values()]
        logger = logging.getLogger('coconut')
        with self.assertLogs('coconut', 'DEBUG') as logs:
            logger.debug('start')
            self.start()
            self.open()
            self.emit(event='error', error='malformed')
            self.emit(event='closed', room=self.room_id, code=1012, retry=True)
            self.emit(event='closed', room=self.room_id, status=401, retry=False)
            self.wait(lambda: self.wakes)
            os.kill(self.starts()[0], signal.SIGKILL)
            self.wait(lambda: len(self.starts()) == 2)
            self.mode('old')
            self.second._team_watcher._failures = 0
            os.kill(self.starts()[1], signal.SIGKILL)
            self.wait(lambda: self.second._team_watcher._unsupported, seconds=15)
        text = '\n'.join(logs.output)
        self.assertIn('refused the list of rooms', text)
        self.assertIn('has no watch mode', text)
        self.assertIn('status 401', text)
        for secret in secrets:
            self.assertNotIn(secret, text)
        self.assertNotIn(RELAY, text)


if __name__ == '__main__':
    unittest.main()
