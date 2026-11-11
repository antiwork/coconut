"""A repeated team write, and the leave of a join that did not finish.

The app sends a write again with the same Idempotency-Key after a lost or slow answer (APIClient.swift). The repeat
must get the answer of the first request and do nothing else, also while the first request still runs. A room that
stays `joining` because its relay never answers must still go from the Team screen.
"""
import contextlib
import http.client
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from coconut import team
from coconut.server import APIServer
from team_fakes import RELAY, TeamMacs

PHONE = 'phone-device'


class Hold:
    """Hold the first call of `name` on `owner` until `release`, so a second request comes while the first runs."""

    def __init__(self, test, owner, name):
        self.entered, self.released = threading.Event(), threading.Event()
        original = getattr(owner, name)

        def held(*arguments, **options):
            if not self.entered.is_set():
                self.entered.set()
                self.released.wait(5)
            return original(*arguments, **options)

        # The method comes from the class, so the cleanup removes the attribute of the instance.
        setattr(owner, name, held)
        test.addCleanup(owner.__dict__.pop, name, None)

    def release(self):
        self.released.set()


class TeamRepeatTests(TeamMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.calls = {}
        for service in (self.first, self.second):
            for name in ('team_create', 'team_join', 'team_leave', 'team_share_page', 'team_profile', 'team_invite'):
                self.count(service, name)

    def count(self, service, name):
        original = getattr(service, name)

        def counted(*arguments, **options):
            self.calls[(service.root.name, name)] = self.calls.get((service.root.name, name), 0) + 1
            return original(*arguments, **options)

        setattr(service, name, counted)

    def ran(self, service, name) -> int:
        return self.calls.get((service.root.name, name), 0)

    def send(self, service, method, path, data, key, device=PHONE):
        return service.team_request(method, path, data, key, device)

    def twice_at_once(self, service, method, path, data, hold) -> list:
        """Send a request, and the same request with the same key while `hold` keeps the first one running."""
        key, answers = str(uuid4()), [None, None]

        def run(index):
            answers[index] = self.send(service, method, path, data, key)

        first = threading.Thread(target=run, args=(0,))
        first.start()
        self.assertTrue(hold.entered.wait(5))
        second = threading.Thread(target=run, args=(1,))
        second.start()
        # The second request waits for the first one.
        second.join(.2)
        self.assertTrue(second.is_alive())
        hold.release()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        return answers

    def body(self, **fields):
        return {'name': 'Example Team', 'member_name': 'Ada', 'relay': RELAY, **fields}

    def joined(self):
        room = self.create()
        link = self.invite(room)['link']
        self.join(self.second, link)
        return room, link

    # Make a team

    def test_a_repeated_create_makes_one_room_and_gives_the_same_answer(self):
        key = str(uuid4())
        answers = [self.send(self.first, 'POST', '/v1/team/rooms', self.body(), key) for _ in range(2)]
        self.assertEqual(answers[0], answers[1])
        self.assertEqual((len(self.relay.rooms), self.ran(self.first, 'team_create')), (1, 1))
        self.assertEqual(list(self.relay.rooms), [answers[0]['room']['room']])

    def test_a_create_that_comes_while_the_first_runs_gets_its_answer(self):
        hold = Hold(self, self.relay, 'create')
        answers = self.twice_at_once(self.first, 'POST', '/v1/team/rooms', self.body(), hold)
        self.assertEqual(answers[0], answers[1])
        self.assertEqual((len(self.relay.rooms), self.ran(self.first, 'team_create')), (1, 1))

    def test_another_key_runs_again(self):
        for _ in range(2):
            self.send(self.first, 'POST', '/v1/team/rooms', self.body(), str(uuid4()))
        self.assertEqual(len(self.relay.rooms), 2)

    def test_another_device_with_the_same_key_does_not_get_the_answer_of_the_first(self):
        key = str(uuid4())
        one = self.send(self.first, 'POST', '/v1/team/rooms', self.body(), key, 'device-one')
        two = self.send(self.first, 'POST', '/v1/team/rooms', self.body(), key, 'device-two')
        self.assertNotEqual(one['room']['room'], two['room']['room'])
        self.assertEqual(len(self.relay.rooms), 2)

    def test_a_request_without_a_key_runs_as_before(self):
        for _ in range(2):
            self.first.team_request('POST', '/v1/team/rooms', self.body())
        self.assertEqual(len(self.relay.rooms), 2)
        room = self.create()
        link = self.invite(room)['link']
        self.join(self.second, link)
        self.assertEqual(self.refusal(self.join, self.second, link), (409, 'This Mac is already in this team.'))

    def test_the_same_key_with_another_request_is_refused(self):
        key = str(uuid4())
        self.send(self.first, 'POST', '/v1/team/rooms', self.body(), key)
        status, text = self.refusal(self.send, self.first, 'POST', '/v1/team/rooms', self.body(name='Other Team'), key)
        self.assertEqual((status, text), (409, 'This idempotency key belongs to a different request.'))
        self.assertEqual(self.refusal(self.send, self.first, 'POST', '/v1/team/rooms', self.body(), 'not-a-uuid')[0], 400)
        self.assertEqual(len(self.relay.rooms), 1)

    def test_a_failure_keeps_no_answer(self):
        key = str(uuid4())
        self.relay.down = True
        self.assertEqual(self.refusal(self.send, self.first, 'POST', '/v1/team/rooms', self.body(), key)[0], 503)
        self.relay.down = False
        self.send(self.first, 'POST', '/v1/team/rooms', self.body(), key)
        self.assertEqual(len(self.relay.rooms), 1)

    # Join, leave, add a bot, change the name

    def test_a_repeated_join_gives_the_answer_of_the_join_that_worked(self):
        room = self.create()
        link = self.invite(room)['link']
        body, key = {'link': link, 'member_name': 'Grace'}, str(uuid4())
        answers = [self.send(self.second, 'POST', '/v1/team/join', body, key) for _ in range(2)]
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(answers[0]['room']['state'], 'active')
        self.assertEqual((len(self.relay.rooms[room['room']]['members']), self.ran(self.second, 'team_join')), (2, 1))

    def test_a_join_that_comes_while_the_first_runs_gets_its_answer(self):
        room = self.create()
        link = self.invite(room)['link']
        hold = Hold(self, self.relay, 'join')
        answers = self.twice_at_once(self.second, 'POST', '/v1/team/join', {'link': link, 'member_name': 'Grace'}, hold)
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(answers[0]['room']['state'], 'active')
        self.assertEqual(self.ran(self.second, 'team_join'), 1)

    def test_a_repeated_leave_gives_the_answer_of_the_leave_that_worked(self):
        room, _ = self.joined()
        key, path = str(uuid4()), f'/v1/team/rooms/{room["room"]}'
        answers = [self.send(self.second, 'DELETE', path, {}, key) for _ in range(2)]
        self.assertEqual(answers, [{'room': room['room'], 'left': True}] * 2)
        self.assertEqual((len(self.relay.rooms[room['room']]['members']), self.ran(self.second, 'team_leave')), (1, 1))

    def test_a_leave_that_comes_while_the_first_runs_gets_its_answer(self):
        room, _ = self.joined()
        hold = Hold(self, self.relay, 'remove_member')
        answers = self.twice_at_once(self.second, 'DELETE', f'/v1/team/rooms/{room["room"]}', {}, hold)
        self.assertEqual(answers, [{'room': room['room'], 'left': True}] * 2)
        self.assertEqual(self.ran(self.second, 'team_leave'), 1)

    def test_a_repeated_page_share_gives_the_same_answer(self):
        room = self.create()
        bot = self.first.mutate('POST', '/v1/bots', {'name': 'Reporter'}, str(uuid4()), 'phone')
        self.first.publish_page(bot['id'], {'key': 'weekly-report', 'title': 'Weekly report', 'format': 'markdown',
                                            'content': '3 tasks are open.'})
        document = self.first._document_of_key(bot['id'], 'weekly-report')['id']
        path, body = f'/v1/team/rooms/{room["room"]}/pages/{document}', {}
        key = str(uuid4())
        answers = [self.send(self.first, 'PUT', path, body, key) for _ in range(2)]
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(self.ran(self.first, 'team_share_page'), 1)
        hold = Hold(self, self.first, 'team_wake')
        answers = self.twice_at_once(self.first, 'PUT', path, body, hold)
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(answers[0]['document']['teams'], [{'room': room['room'], 'name': 'Example Team'}])
        self.assertEqual(self.ran(self.first, 'team_share_page'), 2)

    def test_a_repeated_name_change_writes_one_member_entry(self):
        room = self.create()
        path, body = f'/v1/team/rooms/{room["room"]}', {'member_name': 'Ada L.'}
        writes = lambda: [request[0] for request in self.relay.requests].count('write')
        before, key = writes(), str(uuid4())
        answers = [self.send(self.first, 'PATCH', path, body, key) for _ in range(2)]
        self.assertEqual(answers[0], answers[1])
        self.assertEqual((writes() - before, self.ran(self.first, 'team_profile')), (1, 1))
        hold = Hold(self, self.relay, 'write')
        before = writes()
        answers = self.twice_at_once(self.first, 'PATCH', path, {'member_name': 'Ada K.'}, hold)
        self.assertEqual(answers[0], answers[1])
        self.assertEqual((writes() - before, self.ran(self.first, 'team_profile')), (1, 2))

    def test_old_answers_go(self):
        self.addCleanup(setattr, team, 'REPLY_LIMIT', team.REPLY_LIMIT)
        self.addCleanup(setattr, team, 'REPLY_SECONDS', team.REPLY_SECONDS)
        team.REPLY_LIMIT = 2
        keys = [str(uuid4()) for _ in range(4)]
        for key in keys:
            self.send(self.first, 'POST', '/v1/team/rooms', self.body(), key)
        self.assertLessEqual(len(self.first._team_replies), 2)
        # The first key has no answer now, so its repeat runs again.
        self.send(self.first, 'POST', '/v1/team/rooms', self.body(), keys[0])
        self.assertEqual(len(self.relay.rooms), 5)
        # A request drops the answers past their time.
        team.REPLY_SECONDS = 0
        time.sleep(.01)
        self.relay.down = True
        self.refusal(self.send, self.first, 'POST', '/v1/team/rooms', self.body(), str(uuid4()))
        self.assertEqual(self.first._team_replies, {})

    def test_the_sync_thread_drops_an_old_answer_without_another_request(self):
        now = [1000.0]
        patch = mock.patch.object(team, 'clock', lambda: now[0], create=True)
        patch.start()
        self.addCleanup(patch.stop)
        # The answer of a leave stays, and the Mac then has no room.
        room = self.create()
        self.send(self.first, 'DELETE', f'/v1/team/rooms/{room["room"]}', {}, str(uuid4()))
        self.assertEqual((len(self.first._team_replies), self.first.team_store.rooms()), (1, []))
        self.first._start_team()
        self.addCleanup(self.first.close)
        now[0] += team.REPLY_SECONDS - 1
        self.assertTrue(self.first.team_sync_now()['complete'])
        self.assertEqual(len(self.first._team_replies), 1)
        now[0] += 2
        self.assertTrue(self.first.team_sync_now()['complete'])
        self.assertEqual(self.first._team_replies, {})

    def test_a_finished_write_leaves_no_more_answers_than_the_limit(self):
        self.addCleanup(setattr, team, 'REPLY_LIMIT', team.REPLY_LIMIT)
        team.REPLY_LIMIT = 2
        keys = []
        for _ in range(4):
            keys.append(str(uuid4()))
            self.send(self.first, 'POST', '/v1/team/rooms', self.body(), keys[-1])
            self.assertLessEqual(len(self.first._team_replies), 2)
        # The newest answers stay.
        self.assertEqual({slot[1] for slot in self.first._team_replies}, set(keys[2:]))

    # The invite link

    def test_an_invite_link_is_in_no_log_line_and_no_database_row(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, handler)
        server = APIServer(('127.0.0.1', 0), self.first)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        device = self.first.pair({'code': self.first.pairing_code()})['token']

        def call(method, path, data, key):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            self.addCleanup(connection.close)
            connection.request(method, path, body=json.dumps(data), headers={
                'Authorization': f'Bearer {device}', 'Content-Type': 'application/json', 'Idempotency-Key': key})
            response = connection.getresponse()
            return response.status, json.load(response)

        status, answer = call('POST', '/v1/team/rooms', self.body(), str(uuid4()))
        self.assertEqual(status, 200, answer)
        room = answer['room']['room']
        key = str(uuid4())
        status, invite = call('POST', f'/v1/team/rooms/{room}/invites', {}, key)
        self.assertEqual(status, 200, invite)
        # The repeat gives the same link and adds no second ticket.
        self.assertEqual(call('POST', f'/v1/team/rooms/{room}/invites', {}, key), (200, invite))
        self.assertEqual(len(self.relay.rooms[room]['tickets']), 1)
        status, joined = call('POST', '/v1/team/join', {'link': invite['link']}, str(uuid4()))
        self.assertEqual(status, 409, joined)
        self.first.close()

        secret = invite['link'].removeprefix('coconut://team/')
        output = '\n'.join(logging.Formatter().format(record) for record in records)
        self.assertNotIn(secret, output)
        rows = []
        for path in Path(self.first.root).rglob('*.sqlite3*'):
            rows.append(path.read_bytes())
            if path.suffix == '.sqlite3':
                with contextlib.closing(sqlite3.connect(path)) as db:
                    rows.extend(db.iterdump())
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn(secret.encode() if isinstance(row, bytes) else secret, row)

    # The leave of a join that did not finish

    def test_the_leave_of_a_joining_room_without_an_answer_of_the_relay_removes_the_room(self):
        room = self.create()
        link = self.invite(room)['link']
        self.relay.down = True
        self.assertEqual(self.refusal(self.join, self.second, link)[0], 503)
        self.assertEqual(self.rooms(self.second, sync=False)[0]['state'], 'joining')
        self.assertTrue(self.key_file(self.second, room['room']).exists())
        asked = len(self.relay.requests)
        self.assertEqual(self.leave(self.second, room), {'room': room['room'], 'left': True})
        # The Mac asked the relay one time, then removed its copy and its keys.
        self.assertEqual([request[0] for request in self.relay.requests[asked:]], ['remove_member'])
        self.assertEqual(self.rooms(self.second, sync=False), [])
        self.assertFalse(self.key_file(self.second, room['room']).exists())

    def test_the_leave_of_an_active_room_still_needs_the_relay(self):
        room, _ = self.joined()
        self.relay.down = True
        self.assertEqual(self.refusal(self.leave, self.second, room)[0], 503)
        self.assertEqual(self.rooms(self.second, sync=False)[0]['state'], 'active')
        self.assertTrue(self.key_file(self.second, room['room']).exists())


if __name__ == '__main__':
    unittest.main()
