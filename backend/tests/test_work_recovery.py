import copy
import unittest

import test_automatic_work as fixtures
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class WorkRecoveryTests(unittest.TestCase):
    setUp = fixtures.AutomaticWorkTests.setUp
    tearDown = fixtures.AutomaticWorkTests.tearDown
    write = fixtures.AutomaticWorkTests.write
    start = fixtures.AutomaticWorkTests.start
    sync = fixtures.AutomaticWorkTests.sync

    def retry(self):
        self.start(status='failed')
        self.service.running.clear()
        self.sync()
        return self.write('POST', f'/v1/tasks/{self.task["id"]}/replies', {'text': 'Continue the investigation'})

    def test_retry_retains_one_row_and_original_context(self):
        original = self.start()
        row = self.sync()[0]
        retry = self.retry()
        self.assertNotEqual(retry['exchange_id'], original['exchange_id'])
        rows = self.sync()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], row['id'])
        self.assertEqual(rows[0]['task_id'], retry['id'])
        self.assertEqual(rows[0]['activity'], 'Queued')
        context = self.service.work_context_input(row['id'])
        self.assertEqual(context['source_message']['text'], original['title'])
        self.assertEqual({t['id'] for t in context['tasks']}, {original['id'], retry['id']})
        self.task = retry
        self.start(status='needs_you')
        self.service.running.clear()
        self.assertEqual(self.sync()[0]['work_state'], 'needs_recovery')
        self.start(status='completed', _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Verified the requested result.')]})
        self.service.running.clear()
        self.assertEqual(self.sync()[0]['status'], 'completed')

    def test_repairs_an_existing_duplicate_and_preserves_history(self):
        original = self.start()
        row = self.sync()[0]
        retry = self.retry()
        duplicate = copy.deepcopy(self.service._get('artifacts', row['id']))
        duplicate.update(id='old-duplicate', task_id=retry['id'], title=retry['title'],
                         status='working', activity='In progress', _work_key='task-' + retry['id'])
        self.service._put('artifacts', duplicate)
        self.task = retry
        self.start()
        rows = self.sync()
        self.assertEqual([a['id'] for a in rows], [row['id']])
        self.assertEqual(rows[0]['activity'], 'In progress')
        self.assertEqual(self.service._get('tasks', original['id'])['status'], 'failed')
        self.assertTrue(self.service._get('artifacts', duplicate['id'])['_hidden'])
        self.start(status='completed', _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Verified the requested result.')]})
        self.service.running.clear()
        self.sync()
        self.write('POST', f'/v1/work/items/{row["id"]}/dismiss', {'dismissal_id': 'done'})
        self.assertEqual(self.sync()[0]['work_dismissal_id'], 'done')

    def test_repair_preserves_dismissal_of_a_completed_retry(self):
        self.start()
        row = self.sync()[0]
        retry = self.retry()
        duplicate = copy.deepcopy(self.service._get('artifacts', row['id']))
        duplicate.update(id='dismissed-duplicate', task_id=retry['id'], status='completed', activity='Completed')
        self.service._put('artifacts', duplicate)
        self.task = retry
        self.start(status='completed', _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Verified the requested result.')]})
        self.service.running.clear()
        self.write('POST', '/v1/work/items/dismissed-duplicate/dismiss', {'dismissal_id': 'dismissed'})
        rows = self.sync()
        self.assertEqual([a['id'] for a in rows], ['dismissed-duplicate'])
        self.assertEqual(rows[0]['work_dismissal_id'], 'dismissed')
        self.write('POST', '/v1/work/items/dismissed-duplicate/dismiss', {'dismissal_id': 'dismissed', 'undo': True})
        self.assertIsNone(self.sync()[0]['work_dismissal_id'])
        self.assertTrue(self.service._get('artifacts', row['id'])['_hidden'])

    def test_second_retry_from_original_message_keeps_the_row(self):
        original = self.start()
        row = self.sync()[0]
        first = self.retry()
        self.sync()
        self.task = first
        self.start(status='failed')
        self.service.running.clear()
        second = self.write('POST', f'/v1/tasks/{original["id"]}/replies', {'text': 'Try again'})
        rows = self.sync()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], row['id'])
        self.assertEqual(rows[0]['task_id'], second['id'])
        self.assertEqual(self.sync()[0]['task_id'], second['id'])

    def test_retry_from_intermediate_request_keeps_all_evidence(self):
        original = self.start()
        row = self.sync()[0]
        first = self.retry()
        self.sync()
        self.task = first
        self.start()
        self.service._message(self.bot['id'], 'The first retry found a missing file.', task_id=first['id'], role='assistant')
        second = self.retry()
        self.sync()
        self.task = second
        self.start(status='failed')
        self.service.running.clear()
        third = self.write('POST', f'/v1/tasks/{first["id"]}/replies', {'text': 'Continue from that evidence'})
        rows = self.sync()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], row['id'])
        self.assertEqual(rows[0]['task_id'], third['id'])
        context = self.service.work_context_input(row['id'])
        self.assertEqual({t['id'] for t in context['tasks']}, {original['id'], first['id'], second['id'], third['id']})
        self.assertIn('The first retry found a missing file.', [m['text'] for m in context['updates']])

    def test_long_history_keeps_the_current_task_in_work_context(self):
        original = self.start()
        row = self.sync()[0]
        for _ in range(17):
            self.task = self.retry()
            self.sync()
            self.start()
        context = self.service.work_context_input(row['id'])
        self.assertEqual(len(context['tasks']), 16)
        self.assertIn(self.task['id'], {t['id'] for t in context['tasks']})
        self.assertEqual(context['source_message']['text'], original['title'])

    def test_does_not_steal_row_from_an_active_retry(self):
        original = self.start()
        row = self.sync()[0]
        first = self.retry()
        self.sync()
        self.write('POST', f'/v1/tasks/{original["id"]}/replies', {'text': 'Another reply'})
        rows = self.sync()
        self.assertEqual(next(a for a in rows if a['id'] == row['id'])['task_id'], first['id'])

    def test_existing_bot_report_replaces_the_failed_automatic_row(self):
        self.start()
        row = self.sync()[0]
        retry = self.retry()
        self.task = retry
        self.start()
        report = self.service._register_work(retry['id'], dict(key='export', title='Export account data',
            activity='Checking the saved export', status='ready'))
        rows = self.sync()
        self.assertEqual([a['id'] for a in rows], [report['id']])
        self.assertEqual(rows[0]['activity'], 'Checking the saved export')
        self.assertTrue(self.service._get('artifacts', row['id'])['_hidden'])
        context = self.service.work_context_input(report['id'])
        self.assertEqual(context['source_message']['text'], 'Track an existing PR')

    def test_retry_clears_an_explicit_failure_report(self):
        self.start()
        row = self.service._register_work(self.task['id'], dict(key='export', title='Export account data',
            activity='Export failed', status='blocked'))
        retry = self.retry()
        projected = self.sync()[0]
        self.assertEqual(projected['id'], row['id'])
        self.assertEqual(projected['activity'], 'Queued')
        self.task = retry
        self.start()
        self.assertEqual(self.sync()[0]['activity'], 'In progress')
        updated = self.service._register_work(retry['id'], dict(key='export', title='Export account data',
            activity='Export saved', status='completed', completion_evidence='Read the saved export; all records are present.'))
        self.assertEqual(updated['id'], row['id'])
        self.assertEqual(self.sync()[0]['status'], 'completed')

    def test_completed_requests_and_separate_tasks_do_not_combine(self):
        self.start()
        original = self.sync()[0]
        self.start(status='completed', _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Verified the requested result.')]})
        self.service.running.clear()
        self.sync()
        self.task = self.write('POST', f'/v1/tasks/{self.task["id"]}/replies', {'text': 'Investigate another issue'})
        self.start()
        rows = self.sync()
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(a for a in rows if a['id'] == original['id'])['status'], 'completed')

    def test_old_session_retry_does_not_replace_current_work(self):
        self.start()
        row = self.sync()[0]
        retry = self.retry()
        task = self.service._get('tasks', retry['id'])
        task.update(status='working', _conversation_epoch='old', _native_turn_id='old')
        self.service._put('tasks', task)
        self.assertEqual(self.sync()[0]['task_id'], row['task_id'])


if __name__ == '__main__':
    unittest.main()
