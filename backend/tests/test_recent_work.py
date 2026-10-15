"""A phone's recent state leaves out Work that finished before the Task details window. Task details loads it on request."""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.server import APIServer
from coconut.service import Service


def stamp(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace('+00:00', 'Z')


def ids(rows):
    return [row['id'] for row in rows]


class RecentWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.service.provider.supports_native_outcomes = True
        self.desk = self.write('POST', '/v1/bots', {'name': 'Desk'})
        self.dev = self.write('POST', '/v1/bots', {'name': 'Dev'})
        self.native = {}
        kanban = patch.object(self.service, 'kanban', SimpleNamespace(tasks=self.native, observed_at=stamp(0)))
        kanban.start()
        self.addCleanup(kanban.stop)

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), 'phone')

    def work(self, identity, bot, status, days, conversation=None):
        """Store a native Work card whose Hermes task last changed some days ago, and the Coconut task that owns it."""
        task = dict(id='task-' + identity, bot_id=bot['id'], conversation_bot_id=(conversation or bot)['id'],
                    status='completed', title=identity, created_at=stamp(days))
        self.native['external-' + identity] = dict(id='native-' + identity, external_id='external-' + identity, title=identity,
            status=status, last_activity_at=time.time() - days * 86_400, dependencies=[], diagnostics=[])
        with self.service.transaction():
            self.service._put('tasks', task)
            self.service._put('artifacts', dict(id=identity, kind='work', bot_id=bot['id'], task_id=task['id'], status='working',
                title=identity, created_at=stamp(days), _native_outcome_id='native-' + identity))

    def pull_request(self, identity, days):
        with self.service.transaction():
            self.service._put('artifacts', dict(id=identity, kind='pr', bot_id=self.desk['id'], status='merged', pr_state='merged',
                title='Fix', url='https://github.com/acme/app/pull/7', created_at=stamp(30), observed_at=stamp(days)))

    def test_recent_state_leaves_out_work_that_finished_before_the_window(self):
        self.work('old-done', self.desk, 'done', 10)
        self.work('old-stopped', self.desk, 'archived', 10)
        self.work('recent-done', self.desk, 'done', 2)
        self.work('old-waiting', self.desk, 'todo', 10)
        # Two copies of one pull request form one item. The newer copy keeps both.
        self.pull_request('old-copy', 10)
        self.pull_request('new-copy', 1)
        full = self.service.snapshot(content_only=True, compact_history=True)
        recent = self.service.snapshot(content_only=True, compact_history=True, recent_work=True)
        self.assertEqual(set(ids(full['artifacts'])), {'old-done', 'old-stopped', 'recent-done', 'old-waiting', 'old-copy', 'new-copy'})
        self.assertNotIn('older_work_bot_ids', full)
        self.assertEqual(set(ids(recent['artifacts'])), {'recent-done', 'old-waiting', 'old-copy', 'new-copy'})
        self.assertEqual(recent['older_work_bot_ids'], [self.desk['id']])
        self.assertIn('task-old-done', ids(full['tasks']))
        self.assertNotIn('task-old-done', ids(recent['tasks']))
        self.assertIn('task-recent-done', ids(recent['tasks']))

    def test_task_details_loads_the_left_out_work_of_its_bot(self):
        self.work('desk-old', self.desk, 'done', 10)
        self.work('delegated-old', self.dev, 'done', 10, conversation=self.desk)
        self.work('dev-old', self.dev, 'archived', 10)
        self.work('dev-recent', self.dev, 'done', 1)
        # Task details lists native Work only, so no bot loads an old legacy card.
        with self.service.transaction():
            self.service._put('artifacts', dict(id='legacy-old', kind='work', bot_id=self.desk['id'], status='cancelled',
                                                title='Legacy', created_at=stamp(10)))
        desk = self.service.older_work(self.desk['id'])
        self.assertEqual(ids(desk['artifacts']), ['desk-old', 'delegated-old'])
        self.assertEqual(ids(desk['tasks']), ['task-desk-old', 'task-delegated-old'])
        self.assertEqual({a['work_state'] for a in desk['artifacts']}, {'completed'})
        self.assertEqual(desk['cursor'], self.service._cursor())
        self.assertEqual(ids(self.service.older_work(self.dev['id'])['artifacts']), ['delegated-old', 'dev-old'])
        recent = self.service.snapshot(content_only=True, compact_history=True, recent_work=True)
        self.assertEqual(recent['older_work_bot_ids'], sorted([self.desk['id'], self.dev['id']]))
        self.assertEqual(ids(recent['artifacts']), ['dev-recent'])

    def test_a_message_keeps_its_card_and_history_brings_older_cards(self):
        self.pull_request('shown-pr', 10)
        self.pull_request('history-pr', 10)
        with self.service.transaction():
            self.service._message(self.desk['id'], 'Fix', kind='artifact', artifact_id='history-pr')
            for number in range(60):
                self.service._message(self.desk['id'], str(number))
            self.service._message(self.desk['id'], 'Fix', kind='artifact', artifact_id='shown-pr')
        # A second pull request makes the history card an item of its own.
        with self.service.transaction():
            row = self.service._get('artifacts', 'history-pr')
            row['url'] = 'https://github.com/acme/app/pull/8'
            self.service._put('artifacts', row)
        recent = self.service.snapshot(content_only=True, compact_history=True, recent_work=True)
        self.assertEqual(ids(recent['artifacts']), ['shown-pr'])
        page = self.service.message_page(self.desk['id'], recent['history_before'][self.desk['id']])
        self.assertEqual(ids(page['artifacts']), ['history-pr'])
        self.assertEqual(page['artifacts'][0]['work_state'], 'completed')
        # A full state keeps every card, even when a phone sends the header.
        self.assertEqual(set(ids(self.service.snapshot(recent_work=True)['artifacts'])), {'shown-pr', 'history-pr'})

    def test_update_reset_leaves_routines_to_native_settings(self):
        with self.service.transaction():
            self.service._put('routines', dict(id='routine', bot_id=self.desk['id'], name='Daily check'))
            self.service._event('future.event', {})
        after = self.service._cursor() - 1
        self.assertNotIn('routines', self.service.updates(after, collection_deltas=True, native_events=True, compact_history=True)['reset'])
        self.assertEqual(ids(self.service.updates(after, collection_deltas=True, native_events=True)['reset']['routines']), ['routine'])
        # The first state after pairing has no native settings yet, so it keeps the routines.
        self.assertEqual(ids(self.service.snapshot(content_only=True, compact_history=True, recent_work=True)['routines']), ['routine'])

    def test_phone_asks_for_recent_work_with_a_header(self):
        self.work('desk-old', self.desk, 'done', 10)
        self.work('desk-recent', self.desk, 'done', 1)
        server = APIServer(('127.0.0.1', 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        token = self.service.pair({'code': self.service.pairing_code()})['token']
        def read(path, **headers):
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}',
                                             headers={'Authorization': f'Bearer {token}', **headers})
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        self.assertEqual(set(ids(read('/v1/state', **{'X-Coconut-State': 'recent'})['artifacts'])), {'desk-old', 'desk-recent'})
        self.assertEqual(ids(read('/v1/state', **{'X-Coconut-State': 'recent', 'X-Coconut-Work': 'recent'})['artifacts']), ['desk-recent'])
        with self.service.transaction():
            self.service._event('future.event', {})
        reset = read(f'/v1/updates?after={self.service._cursor() - 1}', **{'X-Coconut-Updates': '4', 'X-Coconut-Work': 'recent'})['reset']
        self.assertEqual(ids(reset['artifacts']), ['desk-recent'])
        self.assertEqual(ids(read(f"/v1/bots/{self.desk['id']}/work/older")['artifacts']), ['desk-old'])


if __name__ == '__main__':
    unittest.main()
