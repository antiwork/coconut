import copy
import unittest

import test_tracked_work as fixtures
from coconut.service import ServiceError
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class WorkQuestionTests(unittest.TestCase):
    setUp = fixtures.TrackedWorkTests.setUp
    tearDown = fixtures.TrackedWorkTests.tearDown
    write = fixtures.TrackedWorkTests.write
    track = fixtures.TrackedWorkTests.track

    def test_question_keeps_context_and_does_not_resume_original_work(self):
        item = self.track()
        with self.service.transaction():
            owner = self.service._get('tasks', self.task['id'])
            owner.update(status='needs_you', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', owner)
        before = copy.deepcopy(self.service._get('tasks', owner['id']))
        question = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
            dict(text='How is this going?', mode='auto', work_artifact_id=item['id'], work_status_only=True))
        self.assertNotEqual(question['id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id']), before)
        message = next(m for m in self.service._all('messages') if m['task_id'] == question['id'])
        self.assertEqual(message['work_context'], dict(id=item['id'], title=item['title']))
        # Refresh at execution, rather than trust the state attached by the phone.
        current = self.service._get('artifacts', item['id'])
        current.update(status='completed', activity='Export saved'); self.service._put('artifacts', current)
        seen = []
        def start(task, bot, history, emit):
            seen.append(task)
            emit('text_delta', {'text': 'The export is saved.'})
            emit('completed', {'summary': 'Update delivered.'})
        self.provider.start_task = start
        self.service._run_task(question['id'])
        self.assertEqual(seen[0]['_work_context']['status'], 'completed')
        self.assertTrue(seen[0]['_work_status_only'])
        self.assertEqual(self.service._get('tasks', owner['id']), before)
        self.service._sync_automatic_work()
        self.assertEqual(len([a for a in self.service._all('artifacts') if a['kind'] == 'work']), 1)

    def test_wrong_bot_or_hidden_work_cannot_be_attached(self):
        item = self.track()
        other = self.write('POST', '/v1/bots', dict(name='Other', role='Other work'))
        payload = dict(text='How is this going?', mode='queue', work_artifact_id=item['id'])
        with self.assertRaises(ServiceError):
            self.write('POST', f'/v1/bots/{other["id"]}/messages', payload)
        saved = self.service._get('artifacts', item['id']); saved['_hidden'] = True
        self.service._put('artifacts', saved)
        with self.assertRaises(ServiceError):
            self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', payload)

    def test_status_request_runs_while_the_bot_works(self):
        import threading
        item = self.track()
        question = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
            dict(text='How is this going?', work_artifact_id=item['id'], work_status_only=True))
        delivered = threading.Event()
        self.provider.start_task = lambda task, bot, history, emit: (emit('completed', {'summary': 'Checked.'}), delivered.set())
        lock = self.service.bot_locks.setdefault(self.bot['id'], threading.Lock())
        lock.acquire()
        worker = threading.Thread(target=self.service._run_task, args=(question['id'],))
        try:
            worker.start()
            self.assertTrue(delivered.wait(2), 'The update waited behind the active bot.')
        finally:
            lock.release(); worker.join(2)

    def test_pr_context_includes_the_other_bots_review(self):
        from test_service_pr import URL, observation
        self.track()
        first = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        other = self.write('POST', '/v1/bots', dict(name='Reviewer', role='Review'))
        review = self.write('POST', f'/v1/bots/{other["id"]}/messages', dict(text='Review the PR'))
        with self.service.transaction():
            task = self.service._get('tasks', review['id'])
            task.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', task)
        self.tracker.observations.append(observation())
        self.service._register_pr_artifact(review['id'], dict(url=URL, activity='Reviewing with Fable'))
        context = self.service.work_context_input(first['id'])
        self.assertIn(review['id'], {t['id'] for t in context['tasks']})


    def test_update_keeps_pr_state_separate_from_bot_activity(self):
        from test_service_pr import URL
        self.track()
        item = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        for state, checks in [('open', 'failed'), ('merged', 'passed')]:
            with self.service.transaction():
                saved = self.service._get('artifacts', item['id'])
                saved.update(status='merged' if state == 'merged' else 'checks_failed',
                             pr_state=state, checks_state=checks, refresh_error=None)
                self.service._put('artifacts', saved)
            context = self.service.work_context_input(item['id'])
            self.assertEqual(context['status'], saved['status'])
            self.assertEqual(context['pull_request']['pr_state'], state)
            self.assertEqual(context['pull_request']['checks_state'], checks)

    def test_normal_followup_does_not_reuse_a_status_only_turn(self):
        item = self.track()
        question = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
            dict(text='How is this going?', work_artifact_id=item['id'], work_status_only=True))
        followup = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Save the export', mode='auto'))
        self.assertNotEqual(followup['id'], question['id'])
        self.assertFalse(self.service._get('tasks', followup['id']).get('_work_status_only'))
        explicit = self.write('POST', f'/v1/tasks/{question["id"]}/replies', dict(text='Explain the last check'))
        saved = self.service._get('tasks', explicit['id'])
        self.assertNotEqual(saved['id'], question['id'])
        self.assertEqual(saved['_work_context_id'], item['id'])
        self.assertFalse(saved['_work_status_only'])

    def test_next_chat_turn_receives_the_visible_update(self):
        from turn_text import turn_input
        item = self.track()
        question = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
            dict(text='How is this going?', work_artifact_id=item['id'], work_status_only=True))
        self.provider.start_task = lambda task, bot, history, emit: (
            emit('text_delta', {'text': 'The export needs its final check.'}), emit('completed', {'summary': 'Complete.'}))
        self.service._run_task(question['id'])
        followup = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Why?', mode='queue'))
        seen = []
        def start(task, bot, history, emit):
            prompt, _ = turn_input(dict(operation='start', task=task, bot=bot, history=history))
            seen.append(prompt)
            emit('completed', {'summary': 'Complete.'})
        self.provider.start_task = start
        self.service._run_task(followup['id'])
        self.assertEqual(len(seen), 1)
        self.assertIn('The export needs its final check.', seen[0])
        self.assertIn('Why?', seen[0])

    def test_status_includes_saved_process_output(self):
        from coconut.service import now
        item = self.track()
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['_conversation_epoch'] = self.service._setting('conversation_epoch')
            self.service._put('tasks', task)
            self.service._put('native_receipts', dict(id='process-result', task_id=task['id'], state='completed',
                action={'name': 'terminal'}, result='Process started', completed_at=now(),
                _completion_text='All export checks passed.', _process_delivery='delivered'))
        result = self.service.work_context_input(item['id'])['recent_results'][0]
        self.assertEqual(result['result'], 'All export checks passed.')
        self.assertTrue(result['delivered'])


import test_work_waits as waits


class FailureTitleTests(unittest.TestCase):
    setUp = waits.WorkWaitTests.setUp
    tearDown = waits.WorkWaitTests.tearDown
    write = waits.WorkWaitTests.write

    def test_internal_path_does_not_become_the_failure_title(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.child)
            task['title'] = 'Snapshot saved at /Users/example/private/review.json'
            self.service._put('tasks', task)
        for _ in range(3):
            self.service._run_task(self.child)
        summary = self.service._get('tasks', self.child)['summary']
        self.assertNotIn('/Users/', summary)
        self.assertIn('Ask for an update', summary)


if __name__ == '__main__':
    unittest.main()
