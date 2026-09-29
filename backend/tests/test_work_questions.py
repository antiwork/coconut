import copy
import unittest

import test_tracked_work as fixtures
from coconut.service import ServiceError
import legacy_work_sync  # noqa: F401  (adds Service._sync_automatic_work)


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
        from coconut.hermes_worker import conversation_input
        item = self.track()
        question = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
            dict(text='How is this going?', work_artifact_id=item['id'], work_status_only=True))
        self.provider.start_task = lambda task, bot, history, emit: (
            emit('text_delta', {'text': 'The export needs its final check.'}), emit('completed', {'summary': 'Complete.'}))
        self.service._run_task(question['id'])
        followup = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text='Why?', mode='queue'))
        seen = []
        def start(task, bot, history, emit):
            prompt, _ = conversation_input(dict(operation='start', task=task, bot=bot, history=history))
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
from test_work_progress import progress


class ProcessWaitTests(unittest.TestCase):
    setUp = waits.WorkWaitTests.setUp
    tearDown = waits.WorkWaitTests.tearDown
    write = waits.WorkWaitTests.write

    def test_waits_on_review_process_without_creating_a_task_cycle(self):
        import json
        self.provider.mode = 'process'
        self.service._run_task(self.child)
        with self.service.transaction():
            followup = self.service._send(self.dev, {'text': 'Use the saved review snapshot', 'mode': 'queue'},
                exchange_id=self.parent['exchange_id'], thread_id=self.parent['thread_id'],
                conversation_bot_id=self.desk['id'])
            owner = self.service._get('tasks', self.child)
            owner['_waiting_on'] = [followup['id']]
            self.service._put('tasks', owner)
        def start(task, bot, history, emit):
            with self.assertRaises(ServiceError):
                emit('work_wait', {'task_id': self.child, 'reason': 'Await review'})
            listing = emit('work_wait', {})
            self.assertEqual(listing['processes'][0]['process_id'], 'proc_test')
            emit('work_wait', {'process_id': 'proc_test', 'reason': 'Await review'})
            emit('completed', {'summary': 'Waiting for the review.', '_work_todos': progress()})
        self.provider.start_task = start
        self.service._run_task(followup['id'])
        waiting = self.service._get('tasks', followup['id'])
        self.assertEqual(waiting['status'], 'waiting')
        self.assertNotIn('_work_retry', waiting)
        source = next(r for r in self.service._all('native_receipts') if r['action']['name'] == 'terminal')
        with self.service.transaction():
            self.service._resume_process_task(self.child, [(source, 'Review finished: no findings.')])
        self.service._drain_task_completions()
        resumed = self.service._get('tasks', followup['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn('Review finished: no findings.', resumed['_background_completion'])
        turn = resumed['_native_turn_id']
        self.service._drain_task_completions()
        self.assertEqual(self.service._get('tasks', followup['id'])['_native_turn_id'], turn)
        with self.service.transaction():
            task = self.service._get('tasks', followup['id'])
            task['status'] = 'working'; self.service._put('tasks', task)
            count = len(self.service._all('native_receipts'))
        result = self.service.wait_for_task(task['id'], {'process_id': 'proc_test', 'reason': 'Check again'})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['result'], 'Review finished: no findings.')
        self.assertEqual(len(self.service._all('native_receipts')), count)

    def test_process_result_arrives_while_its_owner_waits_for_a_decision(self):
        self.provider.mode = 'process'; self.service._run_task(self.child)
        with self.service.transaction():
            followup = self.service._send(self.dev, {'text': 'Use the review result', 'mode': 'queue'},
                exchange_id=self.parent['exchange_id'], thread_id=self.parent['thread_id'],
                conversation_bot_id=self.desk['id'])
            owner = self.service._get('tasks', self.child)
            owner['status'] = 'needs_you'; self.service._put('tasks', owner)
        def start(task, bot, history, emit):
            emit('work_wait', {'process_id': 'proc_test', 'reason': 'Await review'})
            emit('completed', {'summary': 'Waiting.', '_work_todos': progress()})
        self.provider.start_task = start
        self.service._run_task(followup['id'])
        self.provider.results['proc_test'] = 'Review complete. No findings.'
        self.service._drain_process_completions()
        self.service._drain_task_completions()
        self.assertEqual(self.service._get('tasks', self.child)['status'], 'needs_you')
        resumed = self.service._get('tasks', followup['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn('Review complete. No findings.', resumed['_background_completion'])
        # The owner still receives its result when its decision is resolved.
        self.provider.results.clear()
        with self.service.transaction():
            owner = self.service._get('tasks', self.child)
            owner['status'] = 'completed'; self.service._put('tasks', owner)
        self.service._drain_process_completions()
        self.assertIn('Review complete. No findings.', self.service._get('tasks', self.child)['_background_completion'])

    def test_old_consumed_process_returns_an_explicit_missing_result(self):
        self.provider.mode = 'process'; self.service._run_task(self.child)
        with self.service.transaction():
            followup = self.service._send(self.dev, {'text': 'Check the review', 'mode': 'queue'},
                exchange_id=self.parent['exchange_id'], thread_id=self.parent['thread_id'],
                conversation_bot_id=self.desk['id'])
            source = next(r for r in self.service._all('native_receipts') if r['action']['name'] == 'terminal')
            source['_process_delivery'] = 'consumed'; self.service._put('native_receipts', source)
        def start(task, bot, history, emit):
            emit('work_wait', {'process_id': 'proc_test', 'reason': 'Await review'})
            emit('completed', {'summary': 'Waiting.', '_work_todos': progress()})
        self.provider.start_task = start
        self.service._run_task(followup['id'])
        self.service._drain_task_completions()
        resumed = self.service._get('tasks', followup['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn('output is unavailable', resumed['_background_completion'][0])

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


import test_provider as provider_fixtures


class IsolatedUpdateTests(unittest.TestCase):
    setUp = provider_fixtures.ProviderTests.setUp
    tearDown = provider_fixtures.ProviderTests.tearDown
    fake_hermes = provider_fixtures.ProviderTests.fake_hermes

    def test_update_uses_a_separate_worker_and_keeps_the_bot_runtime(self):
        import os
        import threading
        from unittest.mock import patch
        from uuid import uuid4
        provider = self.fake_hermes("""
            if 'Answer a work status question' in self.system_prompt:
                assert self.skip_memory
                assert kwargs['conversation_history'] == []
                assert json.loads(prompt)['title'] == 'Export account data'
                return {'completed': True, 'final_response': 'The export needs its final check.'}
            return {'completed': True, 'final_response': 'Original work.'}
        """)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], lambda *_: None)
            original = provider._runtimes[self.bot['id']]
            question = dict(self.task, id=str(uuid4()), _work_status_only=True,
                            _work_context={'title': 'Export account data'})
            events, errors = [], []
            def run():
                try:
                    provider.start_task(question, self.bot, [], lambda kind, data: events.append((kind, data)))
                except Exception as error:
                    errors.append(error)
            lock = provider._bot_locks[self.bot['id']]
            lock.acquire()
            worker = threading.Thread(target=run)
            try:
                worker.start(); worker.join(5)
                self.assertFalse(worker.is_alive(), 'The update waited for the active runtime.')
            finally:
                lock.release(); worker.join(5)
            self.assertEqual(errors, [])
            self.assertIs(provider._runtimes[self.bot['id']], original)
            self.assertIsNone(original.poll())
            self.assertIn(('text_delta', {'text': 'The export needs its final check.'}), events)
            self.assertEqual(events[-1], ('completed', {'summary': 'Complete.'}))
