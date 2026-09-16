import json
import unittest
from unittest.mock import patch

import test_tracked_work as fixtures
from coconut.service import now


class WorkReadCostTests(unittest.TestCase):
    setUp = fixtures.TrackedWorkTests.setUp
    tearDown = fixtures.TrackedWorkTests.tearDown
    track = fixtures.TrackedWorkTests.track
    write = fixtures.TrackedWorkTests.write

    def test_work_reads_keep_completed_tool_history_in_storage(self):
        item = self.track()
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task.update(status='waiting', _native_turn_id='test-turn',
                        _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', task)
            for index in range(5):
                self.service._put('native_receipts', dict(id=f'old-{index}', task_id=task['id'],
                    turn_id=task['_native_turn_id'], state='completed', created_at=now(),
                    action={'name': 'terminal', 'arguments': {}}, result=json.dumps({'output': str(index)})))
            self.service._put('native_receipts', dict(id='waiting-process', task_id=task['id'],
                turn_id=task['_native_turn_id'], state='completed', created_at=now(),
                action={'name': 'terminal', 'arguments': {}},
                result=json.dumps({'session_id': 'process-1', 'notify_on_complete': True})))
        original = self.service._all
        def read(table):
            self.assertNotEqual(table, 'native_receipts', 'Work must not decode every saved tool result.')
            return original(table)
        with patch.object(self.service, '_all', side_effect=read):
            self.service._tracked_work_input(task)
            self.service._tracked_items_input(task)
            self.service.work_dependencies(task)
            context = self.service.work_context_input(item['id'])
            self.service._sync_automatic_work()
        self.assertEqual(context['work_state'], 'waiting')
        self.assertEqual(context['work_dependencies'][0]['target_id'], 'process-1')
        self.assertEqual([json.loads(r['result']) for r in context['recent_results']],
                         [{'output': '3'}, {'output': '4'}, {'session_id': 'process-1', 'notify_on_complete': True}])
        self.assertEqual(len(original('native_receipts')), 6)

    def test_work_receipts_preserve_unknown_results_and_pending_deliveries(self):
        with self.service.transaction():
            for identity, state, fields in [
                ('unknown', 'executing', {}),
                ('waiting', 'completed', {'_wait_task_id': self.task['id']}),
                ('delivered', 'completed', {'_wait_task_id': self.task['id'], '_process_delivery': 'done'}),
                ('finished', 'completed', {}),
            ]:
                self.service._put('native_receipts', dict(id=identity, task_id=self.task['id'], state=state,
                    action={'name': 'terminal', 'arguments': {}}, result='{}', **fields))
        self.assertEqual({r['id'] for r in self.service._work_receipts()}, {'unknown', 'waiting'})

    def test_completed_output_does_not_slow_pending_result_checks(self):
        with self.service.transaction():
            self.service._put('native_receipts', dict(id='historical-output', task_id=self.task['id'],
                turn_id='previous-turn', state='completed', action={'name': 'terminal', 'arguments': {}},
                result=json.dumps({'output': 'saved output'})))
        original = json.loads
        def decode(value, *args, **kwargs):
            self.assertNotIn('historical-output', value)
            return original(value, *args, **kwargs)
        with patch('coconut.background_processes.json.loads', side_effect=decode):
            self.assertFalse(self.service._background_pending(self.task['id']))

    def test_streaming_reads_only_the_current_task_messages(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
            unrelated = self.service._message(self.bot['id'], 'Other task output', streaming=True)
        original = self.service._all
        def read(table):
            self.assertNotEqual(table, 'messages', 'A text chunk must not read the full transcript.')
            return original(table)
        with patch.object(self.service, '_all', side_effect=read):
            self.service.emit(task['id'], 'text_delta', {'text': 'Hello '})
            self.service.emit(task['id'], 'text_delta', {'text': 'again.'})
        messages = [m for m in self.service._task_messages(task['id']) if m['streaming']]
        self.assertEqual([m['text'] for m in messages], ['Hello again.'])
        self.assertEqual(self.service._get('messages', unrelated['id'])['text'], 'Other task output')
        plan = self.service.db.execute("EXPLAIN QUERY PLAN SELECT data FROM messages WHERE json_extract(data, '$.task_id')=?", (task['id'],)).fetchall()
        self.assertIn('messages_task', str(plan))
