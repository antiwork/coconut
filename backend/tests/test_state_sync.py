import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError


class StateSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.write('POST', '/v1/bots', {'name': 'Desk'})

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), 'phone')

    def test_content_and_history_do_not_wait_for_native_settings(self):
        with self.service.transaction():
            expected = [self.service._message(self.bot['id'], str(i), role='user')['id'] for i in range(135)]
        with patch.object(self.service, '_model_catalog', side_effect=AssertionError('Native discovery blocks content')):
            state = self.service.snapshot(content_only=True)
        self.assertEqual([m['id'] for m in state['messages']], expected[-60:])
        cursor = state['history_before'][self.bot['id']]
        second = self.service.message_page(self.bot['id'], cursor)
        third = self.service.message_page(self.bot['id'], second['before'])
        self.assertEqual([m['id'] for m in third['messages'] + second['messages'] + state['messages']], expected)
        self.assertIsNone(third['before'])
        self.assertEqual(state['sync_version'], 1)

    def test_updates_include_new_and_hidden_messages_and_changed_tasks(self):
        initial = self.service.snapshot(content_only=True)
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        page = self.service.updates(initial['cursor'])
        self.assertNotIn('reset', page)
        self.assertIn(task['id'], [t['id'] for t in page['tasks']])
        message = next(m for m in page['messages'] if m['role'] == 'user')
        with self.service.transaction():
            stored = self.service._get('messages', message['id'])
            stored['_hidden'] = True
            self.service._put('messages', stored)
            self.service._event('message.updated', stored, self.bot['id'], task['id'])
        changed = self.service.updates(page['cursor'])
        self.assertIn(message['id'], changed['removed_messages'])
        self.assertEqual(changed['messages'], [])
        observation = {'id': 'current-work', 'activity_observed_at': 'new-observation'}
        with patch.object(Service, '_work_projection', return_value=[observation]):
            idle = self.service.updates(changed['cursor'])
        self.assertEqual(idle['artifacts'], [observation])
        self.assertEqual(idle['after'], idle['cursor'])
        self.assertNotIn('messages', idle)

    def test_unknown_events_and_missing_replay_reset_content(self):
        initial = self.service.snapshot(content_only=True)
        with self.service.transaction():
            self.service._event('future.event', {})
        reset = self.service.updates(initial['cursor'])
        self.assertEqual(reset['reset']['cursor'], reset['cursor'])
        with self.service.transaction():
            self.service.db.execute('DELETE FROM events WHERE seq=?', (reset['cursor'],))
            self.service._event('task.updated', {})
        self.assertIn('reset', self.service.updates(initial['cursor']))

    def test_missing_event_inside_a_batch_resets_content(self):
        initial = self.service.snapshot(content_only=True)
        with self.service.transaction():
            for _ in range(3):
                self.service._event('task.updated', {})
            self.service.db.execute('DELETE FROM events WHERE seq=?', (initial['cursor'] + 2,))
        page = self.service.updates(initial['cursor'])
        self.assertIn('reset', page)
        self.assertEqual(page['reset']['cursor'], page['cursor'])

    def test_card_reads_current_revision_without_full_snapshot(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        decision = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=task['id'], status='pending', revision=2)
        with self.service.transaction():
            self.service._put('decisions', decision)
            reply = self.service._message(self.bot['id'], 'Earlier card discussion', decision_id=decision['id'])
            for index in range(65):
                self.service._message(self.bot['id'], str(index))
        self.assertIn(reply['id'], [m['id'] for m in self.service.snapshot(content_only=True)['messages']])
        with patch.object(self.service, '_snapshot', side_effect=AssertionError('Full sync is unnecessary')):
            card = self.service.card(decision['id'])
        self.assertEqual(card['messages'], [self.service._public(reply)])
        self.assertEqual(card['decision']['revision'], 2)
        self.assertEqual(card['decision']['request_text'], 'Check this.')
        with self.service.transaction():
            decision['_hidden'] = True
            self.service._put('decisions', decision)
        with self.assertRaises(ServiceError):
            self.service.card(decision['id'])

    def test_delegated_history_stays_in_its_conversation(self):
        delegate = self.write('POST', '/v1/bots', {'name': 'Dev'})
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored.update(bot_id=delegate['id'], conversation_bot_id=self.bot['id'], source_bot_id=self.bot['id'])
            self.service._put('tasks', stored)
            message = self.service._message(delegate['id'], '', task_id=task['id'], kind='decision', decision_id='answered-card')
        page = self.service.message_page(self.bot['id'])
        self.assertNotIn(message['id'], [m['id'] for m in page['messages']])
        delegated = self.service.message_page(delegate['id'])
        self.assertIn(message['id'], [m['id'] for m in delegated['messages']])
        self.assertIn(task['id'], [t['id'] for t in delegated['tasks']])

    def test_old_task_history_and_search_remain_available(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'ÉVIDENCE hidden by pagination'})
        receipt = next(m['client_request_id'] for m in self.service._all('messages') if m.get('task_id') == task['id'] and m['role'] == 'user')
        with self.service.transaction():
            task = self.service._get('tasks', task['id'])
            task['status'] = 'completed'
            self.service._put('tasks', task)
            for i in range(65):
                self.service._message(self.bot['id'], str(i), role='assistant')
        state = self.service.snapshot(content_only=True)
        self.assertNotIn(task['id'], [t['id'] for t in state['tasks']])
        self.assertIn(receipt, state['confirmed_request_ids'])
        page = self.service.message_page(self.bot['id'], state['history_before'][self.bot['id']])
        self.assertIn(task['id'], [t['id'] for t in page['tasks']])
        self.assertEqual(self.service.search_conversations('évidence')['bot_ids'], [self.bot['id']])

    def test_relationship_index_is_reused_only_during_one_projection(self):
        tasks = {}
        for i in range(4):
            task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': str(i), 'mode': 'queue'})
            tasks[task['id']] = self.service._get('tasks', task['id'])
        artifacts = [dict(id=str(uuid4()), kind='work', task_id=t['id'], bot_id=self.bot['id'],
                          created_at=t['created_at'], title=t['title'], status='working') for t in tasks.values()]
        from coconut.work_status import WorkStatus
        with patch.object(WorkStatus, '_work_links', wraps=WorkStatus._work_links) as build:
            self.service._work_projection(artifacts)
            self.assertLessEqual(build.call_count, 2)
            count = build.call_count
            self.service._work_projection(artifacts)
            self.assertGreater(build.call_count, count)
        self.assertIsNone(self.service._work_graphs)

    def test_native_routines_keep_the_recorded_task(self):
        routine = dict(id=str(uuid4()), bot_id=self.bot['id'], last_task_id='recorded-task')
        with self.service.transaction():
            self.service._put('routines', routine)
        native = {'routines': [dict(routine, last_task_id=None)]}
        with patch.object(self.service.provider, 'observe_state', return_value=({}, native), create=True):
            state = self.service.native_snapshot()
        self.assertEqual(state['routines'][0]['last_task_id'], 'recorded-task')
        fallback = self.service.native_snapshot()
        self.assertEqual(fallback['routines'][0]['last_task_id'], 'recorded-task')


if __name__ == '__main__':
    unittest.main()
