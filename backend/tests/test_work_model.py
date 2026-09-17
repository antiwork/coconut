import json
import threading
import unittest

import test_tracked_work as fixtures
from coconut.service import ServiceError, YieldForDecision


class WorkModelTests(unittest.TestCase):
    setUp = fixtures.TrackedWorkTests.setUp
    tearDown = fixtures.TrackedWorkTests.tearDown
    track = fixtures.TrackedWorkTests.track
    row = fixtures.TrackedWorkTests.row
    write = fixtures.TrackedWorkTests.write

    def state(self, **fields):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task.update(fields)
            self.service._put('tasks', task)

    def test_completed_pr_check_waits_for_author_without_recovery_loop(self):
        from dataclasses import replace
        from test_service_pr import observation
        self.tracker.observations = [replace(observation(), pr_state='open', draft=True, checks_state='failed')]
        self.provider.payload.update(activity='Waiting for the author', activity_state='blocked')
        self.service._run_task(self.task['id'])
        self.state(_work_todos={'todos': [dict(id='coconut-assignment', status='completed',
            content='Verified the requested PR status; the author still owns implementation.')]})
        row = self.row()
        self.assertEqual(row['work_state'], 'waiting')
        self.assertEqual(row['activity'], 'Waiting for the PR author')
        self.assertIsNone(row['completion_evidence'])
        count = len(self.service._all('tasks'))
        for _ in range(3):
            self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), count)
        self.assertEqual(self.service._all('decisions'), [])
        self.tracker.observations = [replace(observation(), pr_state='open', draft=False, checks_state='passed')]
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')
        self.assertEqual(self.row()['work_state'], 'queued')

    def test_turn_completion_does_not_complete_the_outcome(self):
        self.track(completion_condition='The exported file contains every requested record.')
        self.state(status='completed')
        self.assertEqual(self.row()['work_state'], 'needs_recovery')
        self.assertEqual(self.row()['completion_condition'], 'The exported file contains every requested record.')
        self.assertEqual(self.row()['work_next_step'], 'Ask for progress')

    def test_explicit_pr_wait_uses_github_updates_without_recovery(self):
        from dataclasses import replace
        from test_service_pr import observation
        self.tracker.observations = [replace(observation(), pr_state='open', draft=True, checks_state='failed')]
        self.provider.payload.update(activity='Waiting for author validation', activity_state='waiting')
        self.service._run_task(self.task['id'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')
        self.assertEqual(self.row()['work_state'], 'waiting')
        count = len(self.service._all('tasks'))
        for _ in range(3): self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), count)
        self.assertEqual(self.service._all('decisions'), [])
        self.tracker.observations = [replace(observation(), pr_state='open', draft=False, checks_state='passed')]
        self.service.refresh_pull_requests()
        self.assertEqual(self.row()['work_state'], 'queued')

    def test_pr_wait_cannot_hide_a_failed_turn_or_changed_head(self):
        self.provider.payload.update(activity='Waiting for author validation', activity_state='waiting')
        self.service._run_task(self.task['id'])
        self.state(status='failed')
        self.assertEqual(self.row()['work_state'], 'needs_recovery')
        self.state(status='completed')
        self.assertEqual(self.row()['work_state'], 'waiting')
        item = self.service._all('artifacts')[0]
        item['head_sha'] = 'b' * 40
        self.service._put('artifacts', item)
        self.assertEqual(self.row()['work_state'], 'needs_recovery')

    def test_pr_wait_verifies_an_unknown_tool_result_before_waiting(self):
        self.provider.payload.update(activity='Waiting for author validation', activity_state='waiting')
        self.service._run_task(self.task['id'])
        self.state(status='working')
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments=dict(command='git push')))
        self.state(status='completed')
        self.assertEqual(self.row()['work_state'], 'needs_recovery')
        self.service._queue_work_recoveries()
        recovery = self.service._all('tasks')[-1]
        self.assertNotEqual(recovery['id'], self.task['id'])
        self.assertIn(receipt['id'], {r['id'] for r in recovery['_self_recovery']['unknown_tools']})

    def test_completion_requires_evidence_and_keeps_the_outcome_id(self):
        item = self.track()
        with self.assertRaises(ServiceError):
            self.track(status='completed')
        self.track(status='completed', completion_evidence='Read the exported file and verified all 12 records.')
        self.state(status='completed')
        row = self.row()
        self.assertEqual(row['id'], item['id'])
        self.assertEqual(row['work_state'], 'completed')
        self.assertIn('12 records', row['completion_evidence'])
        self.assertIsNone(row['work_next_step'])

    def pending_card(self):
        self.track()
        self.state(status='needs_you')
        self.assertEqual(self.row()['work_state'], 'needs_recovery')
        self.state(status='working')
        with self.assertRaises(YieldForDecision):
            self.service.emit(self.task['id'], 'native_question', dict(title='Export?', options=[dict(id='yes', label='Export')]))
        decision = self.service._all('decisions')[-1]
        self.assertEqual(self.row()['work_state'], 'needs_decision')
        self.assertEqual(self.row()['work_decision_id'], decision['id'])

    def test_decision_requires_a_real_pending_card(self):
        self.pending_card()

    def test_research_result_can_progress_while_the_decision_stays_pending(self):
        self.pending_card()
        decision = self.service._all('decisions')[-1]
        self.write('POST', f"/v1/decisions/{decision['id']}/reply", dict(text='Check the source first.', revision=1))
        self.assertEqual(self.row()['work_state'], 'queued')
        context = self.service._all('contexts')[-1]
        with self.service.transaction():
            context['status'] = 'working'
            self.service._put('contexts', context)
        self.service.context_threads[context['id']] = threading.current_thread()
        try:
            self.assertEqual(self.row()['work_state'], 'working')
            self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        finally:
            self.service.context_threads.clear()

    def test_completed_process_is_queued_for_delivery_not_still_waiting(self):
        self.track()
        call = self.service.native_event(self.task['id'], 'native_call', dict(name='terminal', arguments={}))
        self.service.native_event(self.task['id'], 'native_result', dict(id=call['id'], result=json.dumps(
            dict(session_id='proc-one', status='running', notify_on_complete=True))))
        self.state(status='waiting')
        self.assertEqual(self.row()['work_state'], 'waiting')
        with self.service.transaction():
            receipt = self.service._get('native_receipts', call['id'])
            receipt['_completion_text'] = 'The check passed.'
            self.service._put('native_receipts', receipt)
        row = self.row()
        self.assertEqual(row['work_state'], 'queued')
        self.assertEqual(row['work_dependencies'][0]['state'], 'ready')
        self.assertEqual(len(self.service._all('artifacts')), 1)

    def test_saved_source_result_readies_a_process_wait(self):
        self.track()
        self.state(status='waiting')
        task = self.service._get('tasks', self.task['id'])
        self.service._put('tasks', dict(task, id='other'))
        self.service._put('native_receipts', dict(id='source', task_id='other', state='completed',
            action={'name': 'terminal'}, _completion_text='The check passed.'))
        self.service._put('native_receipts', dict(id='wait', task_id=task['id'], state='completed',
            action={'name': 'coconut_wait', 'arguments': {'reason': 'Waiting for the check'}},
            turn_id=task.get('_native_turn_id'), _wait_process_receipt='source', _wait_session_id=task['session_id']))
        self.assertEqual(self.row()['work_state'], 'queued')
        self.assertEqual(self.row()['work_dependencies'][0]['state'], 'ready')

    def test_completed_turn_keeps_undelivered_process_dependency(self):
        self.test_completed_process_is_queued_for_delivery_not_still_waiting()
        self.state(status='completed')
        self.assertEqual(self.row()['work_state'], 'queued')

    def test_decision_wait_uses_the_dispatcher_rules(self):
        self.track()
        self.state(status='waiting')
        task = self.service._get('tasks', self.task['id'])
        self.service._put('tasks', dict(task, id='other'))
        self.service._put('native_receipts', dict(id='wait', task_id=self.task['id'], state='completed',
            action={'name': 'coconut_wait', 'arguments': {}}, _wait_decision_id='external-decision', _wait_session_id=task['session_id']))
        decision = dict(id='external-decision', task_id='other', bot_id=self.bot['id'], status='approved')
        self.service._put('decisions', decision)
        self.assertEqual(self.row()['work_state'], 'queued')
        decision.update(status='dismissed', inbox_undo_until='2099-01-01T00:00:00Z')
        self.service._put('decisions', decision)
        self.assertEqual(self.row()['work_state'], 'waiting')

    def test_waiting_on_another_tasks_card_exposes_that_decision(self):
        self.test_decision_wait_uses_the_dispatcher_rules()
        decision = self.service._get('decisions', 'external-decision')
        decision.update(status='pending')
        self.service._put('decisions', decision)
        row = self.row()
        self.assertEqual(row['work_state'], 'needs_decision')
        self.assertEqual(row['work_decision_id'], decision['id'])
        decision.update(status='approved')
        self.service._put('decisions', decision)
        row = self.row()
        self.assertEqual(row['work_state'], 'queued')
        self.assertIsNone(row['work_decision_id'])

    def test_continuation_keeps_the_other_process_dependency(self):
        self.test_completed_process_is_queued_for_delivery_not_still_waiting()
        receipt = self.service._all('native_receipts')[-1]
        receipt.pop('_completion_text')
        self.service._put('native_receipts', receipt)
        self.state(status='waiting', _native_turn_id='continuation')
        self.assertEqual(self.row()['work_state'], 'waiting')

    def test_completed_task_with_unfinished_work_is_not_a_ready_result(self):
        self.track()
        self.state(status='waiting')
        task = self.service._get('tasks', self.task['id'])
        self.service._put('tasks', dict(task, id='target', status='completed', _waiting_on=['another']))
        receipt = dict(id='wait', task_id=task['id'], state='completed', action={'name': 'coconut_wait'},
                       _wait_task_id='target', _wait_session_id=task['session_id'])
        self.service._put('native_receipts', receipt)
        self.assertIsNone(self.service._wait_completion(receipt, task))
        self.assertEqual(self.row()['work_state'], 'waiting')


class WorkContextModelTests(unittest.TestCase):
    from test_context_completions import ContextCompletionTests as Fixtures
    setUp = Fixtures.setUp
    tearDown = Fixtures.tearDown
    write = Fixtures.write
    event = Fixtures.event
    question = Fixtures.question
    restart = Fixtures.restart
    run_context = Fixtures.run_context
    start_research = Fixtures.start_research
    drain = Fixtures.drain

    def row(self):
        return next(a for a in self.service.snapshot()['artifacts'] if a['id'] == 'research-work')

    def test_background_research_and_cached_result_survive_restart(self):
        from coconut.service import now
        self.start_research()
        self.service._put('artifacts', dict(id='research-work', task_id=self.task['id'], bot_id=self.task['bot_id'],
            kind='work', title='Check the local workflow', status='waiting', activity='Checking the workflow',
            created_at=now(), observed_at=now(), url=None))
        receipt = self.service._all('native_receipts')[-1]
        receipt.update(_process_status='running', _process_observed_at=now())
        self.service._put('native_receipts', receipt)
        self.assertEqual(self.row()['work_state'], 'working')
        receipt['_completion_text'] = 'The local workflow is available.'
        receipt['_process_status'] = 'completed'
        self.service._put('native_receipts', receipt)
        self.assertEqual(self.row()['work_state'], 'queued')
        self.restart()
        self.assertEqual(self.row()['work_state'], 'queued')
        self.drain()
        self.drain()
        self.assertEqual(len(self.service._all('contexts')), 2)
        self.assertEqual(self.row()['work_state'], 'queued')
        self.assertEqual(self.service._get('decisions', self.decision['id'])['status'], 'pending')


class WorkReviewOwnershipTests(unittest.TestCase):
    import test_work_status as fixtures
    setUp = fixtures.WorkStatusTests.setUp
    tearDown = fixtures.WorkStatusTests.tearDown
    write = fixtures.WorkStatusTests.write
    run_task = fixtures.WorkStatusTests.run_task
    artifact = fixtures.WorkStatusTests.artifact
    row = fixtures.WorkStatusTests.row
    status = fixtures.WorkStatusTests.status

    def test_replaced_review_ignores_failed_or_cancelled_original(self):
        artifact = self.artifact()
        previous = self.task
        next_task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Review the updated change'})
        task = self.service._get('tasks', next_task['id'])
        task.update(status='working', _native_turn_id='replacement', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', task)
        self.service._bind_pr_review(artifact, task, transfer=True)
        self.service.running[task['id']] = threading.current_thread()
        try:
            for status in ('failed', 'cancelled'):
                self.task = previous
                self.status(status)
                self.assertEqual(self.row()['work_state'], 'working')
        finally:
            self.service.running.clear()

    def test_new_reported_attempt_replaces_failed_review_without_transferring_ownership(self):
        artifact = self.artifact()
        self.status('failed')
        next_task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Fix the remaining findings'})
        task = self.service._get('tasks', next_task['id'])
        task.update(status='working', _native_turn_id='fix-turn', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', task)
        self.service._set_work_activity(artifact, task, {'activity': 'Fixing the review findings'})
        self.service.running[task['id']] = threading.current_thread()
        try:
            self.assertEqual(self.row()['work_state'], 'working')
            self.assertEqual(self.row()['activity'], 'Fixing the review findings')
            self.assertEqual(self.service._get('artifacts', artifact['id'])['_review_task_id'], self.task['id'])
        finally:
            self.service.running.clear()


class AutomaticOutcomeCompletionTests(unittest.TestCase):
    import test_automatic_work as fixtures
    setUp = fixtures.AutomaticWorkTests.setUp
    tearDown = fixtures.AutomaticWorkTests.tearDown
    write = fixtures.AutomaticWorkTests.write
    start = fixtures.AutomaticWorkTests.start
    sync = fixtures.AutomaticWorkTests.sync

    def test_native_checklist_completes_the_tracked_outcome(self):
        from coconut.work_progress import NativeWork, ROOT_ID
        task = self.start()
        item = self.sync()[0]
        class Store:
            def __init__(self):
                self.todos, self.revision = [], 0
            def restore(self, todos, revision):
                self.todos, self.revision = todos, revision
            def snapshot(self):
                return dict(todos=self.todos, revision=self.revision)
        class Agent:
            _todo_store = Store()
        agent = Agent()
        work = NativeWork(dict(operation='start', task=task))
        self.assertFalse(work.enabled)
        work.restore(agent)
        self.assertEqual(agent._todo_store.todos, [])
        agent._todo_store.restore([dict(id='export', status='completed',
            content='Verified that all requested records appear in the saved file.')], revision=1)
        self.service.emit(task['id'], 'completed', dict(summary='Verified the export.', **work.payload(agent)))
        self.service.running.clear()
        row = self.sync()[0]
        self.assertEqual(row['id'], item['id'])
        self.assertEqual(row['work_state'], 'completed')
        self.assertEqual(row['completion_evidence'], 'Verified the export.')
