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

    def test_text_replay_does_not_build_history_work_or_decisions(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        cursor = self.service._cursor()
        self.service.emit(task['id'], 'text_delta', {'text': 'First words'})
        timestamp = self.service.db.execute('SELECT created_at FROM events WHERE seq=?', (cursor,)).fetchone()[0]
        with patch('coconut.service.now', return_value=timestamp), \
             patch.object(Service, '_snapshot', side_effect=AssertionError('Replay must not build a snapshot')), \
             patch.object(Service, '_recent_messages', side_effect=AssertionError('Replay must not read history')), \
             patch.object(Service, '_work_projection', side_effect=AssertionError('Text must not rebuild Work')):
            page = self.service.updates(cursor)
        self.assertEqual([m['text'] for m in page['messages']], ['First words'])
        self.assertEqual([t['id'] for t in page['tasks']], [task['id']])
        self.assertNotIn('artifacts', page)
        self.assertNotIn('decisions', page)

    def test_continuous_text_refreshes_time_dependent_work(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        cursor = self.service._cursor()
        with self.service.transaction():
            self.service.db.execute('UPDATE events SET created_at=? WHERE seq=?', ('2026-09-16T12:00:00Z', cursor))
        self.service.emit(task['id'], 'text_delta', {'text': 'More words'})
        observation = {'id': 'work', 'activity_observed_at': 'current'}
        with patch('coconut.service.now', return_value='2026-09-16T12:00:16Z'), \
             patch.object(Service, '_work_projection', return_value=[observation]):
            page = self.service.updates(cursor)
        self.assertEqual(page['artifacts'], [observation])
        self.assertEqual(page['messages'][0]['text'], 'More words')

    def test_structural_replay_keeps_complete_replacement_collections(self):
        initial = self.service.snapshot(content_only=True)
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        with self.service.transaction():
            for i in range(2):
                decision = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=task['id'], status='pending', revision=1)
                self.service._put('decisions', decision)
                self.service._event('decision.created', decision, self.bot['id'], task['id'])
        page = self.service.updates(initial['cursor'])
        current = self.service.snapshot(content_only=True)
        self.assertEqual(page['decisions'], current['decisions'])
        self.assertEqual(page['confirmed_request_ids'], current['confirmed_request_ids'])
        self.assertEqual(page['bots'], current['bots'])

    def test_stream_reconciliation_updates_one_message_and_withdraws_failed_text(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        cursor = self.service._cursor()
        self.service.emit(task['id'], 'text_delta', {'text': 'Draft'})
        first = self.service.updates(cursor)
        identity = first['messages'][0]['id']
        self.service.emit(task['id'], 'text_delta', {'text': 'Verified result.', 'replace': True})
        final = self.service.updates(first['cursor'])
        self.assertEqual([(m['id'], m['text']) for m in final['messages']], [(identity, 'Verified result.')])
        self.service.emit(task['id'], 'text_delta', {'text': '', 'replace': True})
        removed = self.service.updates(final['cursor'])
        self.assertEqual(removed['removed_messages'], [identity])
        self.assertNotIn(identity, [m['id'] for m in self.service.message_page(self.bot['id'])['messages']])

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

    def test_card_origins_share_one_history_read_and_refresh_after_changes(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Original request'})
        with self.service.transaction():
            for _ in range(5):
                self.service._put('decisions', dict(id=str(uuid4()), bot_id=self.bot['id'],
                    task_id=task['id'], status='pending', revision=1))
        original = Service._history
        calls = []
        def read(view, bot_id):
            calls.append(bot_id)
            return original(view, bot_id)
        with patch.object(Service, '_history', read):
            state = self.service.snapshot(content_only=True)
        self.assertEqual(calls, [self.bot['id']])
        self.assertEqual({d['request_text'] for d in state['decisions']}, {'Original request'})
        with self.service.transaction():
            source = self.service._get('messages', state['decisions'][0]['request_message_id'])
            source['text'] = 'Corrected request'
            self.service._put('messages', source)
        self.assertEqual({d['request_text'] for d in self.service.snapshot(content_only=True)['decisions']},
                         {'Corrected request'})

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
