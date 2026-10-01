import json
import unittest
from unittest.mock import patch

import test_tracked_work as fixtures
from coconut.service import now
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class WorkReadCostTests(unittest.TestCase):
    setUp = fixtures.TrackedWorkTests.setUp
    tearDown = fixtures.TrackedWorkTests.tearDown
    track = fixtures.TrackedWorkTests.track
    write = fixtures.TrackedWorkTests.write

    def test_work_snapshot_reads_settings_once_and_reloads_on_next_snapshot(self):
        for index in range(8):
            self.track(key='work-' + str(index), title='Work ' + str(index))
        self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('probe', json.dumps({'value': 1})))
        with self.service._read_view() as view, view._read_transaction():
            queries = []
            view.db.set_trace_callback(queries.append)
            items = view._work_projection(view._all('artifacts'))
            self.assertEqual(len(items), 8)
            value = view._setting('probe')
            value['value'] = 99
            self.assertEqual(view._setting('probe'), {'value': 1})
            self.assertEqual(view._setting('missing', 'default'), 'default')
            settings_reads = [q for q in queries if 'FROM settings' in q]
            self.assertEqual(len(settings_reads), 1)
        self.service.db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps({'value': 2}), 'probe'))
        with self.service._read_view() as view, view._read_transaction():
            self.assertEqual(view._setting('probe'), {'value': 2})
        with self.service.transaction():
            self.service.db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps({'value': 3}), 'probe'))
            self.assertEqual(self.service._setting('probe'), {'value': 3})

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

    def test_work_wait_and_cancel_do_not_scan_completed_tool_history(self):
        item = self.track()
        self.track(key='second-outcome', title='Keep the second outcome')
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            self.service._put('native_receipts', dict(id='completed-history', task_id='unrelated',
                turn_id='old', state='completed', created_at=now(), action={'name': 'terminal', 'arguments': {}},
                result=json.dumps({'output': 'A retained tool result'})))
            self.service._put('native_receipts', dict(id='current-process', task_id=task['id'],
                turn_id=task.get('_native_turn_id'), state='completed', created_at=now(),
                action={'name': 'terminal', 'arguments': {}},
                result=json.dumps({'session_id': 'current-test', 'notify_on_complete': True})))
        original = self.service._all
        def read(table):
            self.assertNotEqual(table, 'native_receipts', 'A work action must not decode unrelated tool history.')
            return original(table)
        with patch.object(self.service, '_all', side_effect=read):
            result = self.service.wait_for_task(task['id'], dict(process_id='current-test', reason='Waiting for tests'))
            self.assertEqual(result['status'], 'waiting')
            self.write('POST', '/v1/work/items/' + item['id'] + '/cancel', {})
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'working')
        self.assertEqual(self.service._get('native_receipts', 'completed-history')['state'], 'completed')

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

    def test_context_reuses_graphs_only_within_the_current_read(self):
        self.track()
        task = self.service._get('tasks', self.task['id'])
        with self.service._read_view() as view:
            original = view._work_links
            with patch.object(view, '_work_links', wraps=original) as links:
                with view._read_transaction():
                    tasks = {t['id']: t for t in view._all('tasks')}
                    records = view._work_receipts()
                    first = view._work_chain(task, tasks, records, include_continuations=True)
                    second = view._work_chain(task, tasks, records, include_continuations=True)
                    self.assertEqual(first, second)
                    self.assertEqual(links.call_count, 1)
                with self.service.transaction():
                    task['_waiting_on'] = ['new-dependency']
                    self.service._put('tasks', task)
                with view._read_transaction():
                    tasks = {t['id']: t for t in view._all('tasks')}
                    view._work_chain(tasks[task['id']], tasks, view._work_receipts(), include_continuations=True)
                    self.assertEqual(tasks[task['id']]['_waiting_on'], ['new-dependency'])
                    self.assertEqual(links.call_count, 2)

    def test_pause_direction_reads_only_ancestors_and_their_messages(self):
        item = self.track()
        parent = self.service._get('tasks', self.task['id'])
        child = dict(parent, id='child', parent_task_id=parent['id'])
        parent['source_task_id'] = child['id']
        self.service._put('tasks', parent)
        self.service._put('tasks', child)
        pause = dict(id='pause', created_at='2100-01-01T00:00:00Z')
        original = self.service._all
        def bounded(table):
            self.assertNotIn(table, ('tasks', 'messages'))
            return original(table)
        with patch.object(self.service, '_all', side_effect=bounded):
            self.assertFalse(self.service._work_has_new_direction(child, pause, item))
            message = self.service._message(self.bot['id'], 'Resume', task_id=parent['id'], role='user')
            message.update(_human_device_id='person', created_at='2101-01-01T00:00:00Z')
            self.service._put('messages', message)
            self.assertTrue(self.service._work_has_new_direction(child, pause, item))
            parent['_work_status_only'] = True
            self.service._put('tasks', parent)
            self.assertFalse(self.service._work_has_new_direction(child, pause, item))

    def test_queue_selection_uses_indexes_and_retains_terminal_native_rows_once(self):
        from types import SimpleNamespace
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        retained = dict(task, id='retained')
        self.service._put('tasks', retained)
        queued = dict(task, id='queued', status='queued')
        self.service._put('tasks', queued)
        with self.service.transaction():
            for index in range(1000):
                self.service._put('tasks', dict(task, id=f'history-{index}'))
        self.service.db.execute('ANALYZE')
        self.service.kanban = SimpleNamespace(close=lambda: None, tasks={
            retained['id']: dict(id='native-retained', status='scheduled'),
            queued['id']: dict(id='native-queued', status='ready')})
        self.addCleanup(setattr, self.service, 'kanban', None)
        queries = []
        self.service.db.set_trace_callback(queries.append)
        rows = self.service._kanban_rows()
        self.service.db.set_trace_callback(None)
        self.assertEqual([row['id'] for row in rows], ['retained', 'queued'])
        for table, index in [('tasks', 'tasks_status'), ('contexts', 'contexts_status')]:
            query = next(q for q in queries if q.startswith(f'SELECT data FROM {table} WHERE ') and 'json_each' in q)
            plan = [r[3] for r in self.service.db.execute('EXPLAIN QUERY PLAN ' + query)]
            self.assertTrue(any(index in step for step in plan), plan)
            self.assertFalse(any(step.startswith('SCAN ' + table) for step in plan), plan)
