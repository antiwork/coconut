import copy
import unittest

import test_automatic_work as fixtures
from turn_text import turn_input
from coconut.service import Service
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class WorkSelfRecoveryTests(unittest.TestCase):
    setUp = fixtures.AutomaticWorkTests.setUp
    tearDown = fixtures.AutomaticWorkTests.tearDown
    start = fixtures.AutomaticWorkTests.start
    sync = fixtures.AutomaticWorkTests.sync
    write = fixtures.AutomaticWorkTests.write

    def stranded(self):
        self.start()
        item = self.sync()[0]
        self.start(status='failed', summary='Tool result unknown')
        self.service.running.clear()
        return item

    def recover(self):
        self.service._queue_work_recoveries()
        return self.service._all('tasks')[-1]

    def test_unknown_write_gets_verification_turn_without_replaying_or_faking_approval(self):
        self.start()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            {'name': 'terminal', 'arguments': {'command': 'git push'}})
        item = self.stranded()
        messages = self.service._all('messages')
        old = copy.deepcopy(self.service._get('tasks', self.task['id']))
        task = self.recover()
        self.assertNotEqual(task['id'], old['id'])
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(self.service._get('tasks', old['id']), old)
        self.assertEqual(self.service._all('messages'), messages)
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'executing')
        self.assertEqual(task['_self_recovery']['unknown_tools'][0]['action']['arguments']['command'], 'git push')
        self.assertEqual(self.sync()[0]['id'], item['id'])
        self.assertEqual(self.sync()[0]['work_state'], 'queued')
        self.assertEqual(self.sync()[0]['work_queue_position'], 1)
        count = len(self.service._all('tasks'))
        self.recover()
        self.assertEqual(len(self.service._all('tasks')), count)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.tracker)
        self.recover()
        self.assertEqual(len(self.service._all('tasks')), count)

    def test_recovery_rejects_circular_wait_between_exchanges(self):
        from coconut.service import ServiceError
        self.stranded()
        first = self.recover()
        first['status'] = 'failed'
        self.service._put('tasks', first)
        second = self.recover()
        for task in (first, second):
            task.update(status='working', _native_turn_id=task['id'])
            self.service._put('tasks', task)
        self.service.wait_for_task(first['id'], dict(task_id=second['id'], reason='Wait for recovery'))
        with self.assertRaises(ServiceError) as error:
            self.service.wait_for_task(second['id'], dict(task_id=first['id'], reason='Wait for recovery'))
        self.assertEqual(error.exception.status, 409)

    def test_completed_turn_without_outcome_gets_a_check_not_false_completion(self):
        item = self.stranded()
        self.start(status='completed')
        self.service.running.clear()
        task = self.recover()
        self.assertEqual(task['_self_recovery']['previous_status'], 'completed')
        self.assertEqual(self.sync()[0]['work_state'], 'queued')
        self.assertEqual(task['_self_recovery']['work'][0]['key'], 'task-' + self.task['id'])

    def test_cancelled_work_never_recovers(self):
        item = self.stranded()
        self.write('POST', '/v1/work/items/' + item['id'] + '/cancel', {})
        count = len(self.service._all('tasks'))
        self.recover()
        self.assertEqual(len(self.service._all('tasks')), count)

    def test_verified_completed_work_never_recovers(self):
        self.stranded()
        task = self.service._get('tasks', self.task['id'])
        task.update(status='completed', _work_todos={'todos': [dict(id='coconut-assignment', status='completed', content='Verified result')]})
        self.service._put('tasks', task)
        count = len(self.service._all('tasks'))
        self.recover()
        self.assertEqual(len(self.service._all('tasks')), count)

    def test_old_session_work_never_recovers(self):
        self.stranded()
        task = self.service._get('tasks', self.task['id'])
        task['session_id'] = 'old'
        self.service._put('tasks', task)
        count = len(self.service._all('tasks'))
        self.recover()
        self.assertEqual(len(self.service._all('tasks')), count)

    def test_bot_can_make_decision_during_recovery(self):
        self.stranded()
        task = self.recover()
        def run(task, bot, history, emit):
            self.assertIn('_self_recovery', task)
            self.service.native_event(task['id'], 'native_question', dict(title='Which account owns this work?',
                context='The existing records refer to two accounts.',
                options=[dict(id='first', label='First account'), dict(id='second', label='Second account')]))
        self.provider.start_task = run
        self.service._run_task(task['id'])
        self.assertEqual(self.sync()[0]['work_state'], 'needs_decision')
        count = len(self.service._all('tasks'))
        self.recover()
        self.assertEqual(len(self.service._all('tasks')), count)

    def test_service_restarts_do_not_exhaust_recovery_attempts(self):
        self.stranded()
        receipts = []
        for _ in range(4):
            task = self.recover()
            self.assertEqual(task['status'], 'queued')
            self.assertEqual(task['_self_recovery']['attempt'], 1)
            task.update(status='working', _native_turn_id='restart-' + task['id'])
            self.service._put('tasks', task)
            receipt = self.service.native_event(task['id'], 'native_call',
                dict(name='terminal', arguments={'command': 'git push'}))
            receipts.append(receipt['id'])
            self.service.close()
            self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.tracker)
            self.assertEqual(self.service._get('tasks', task['id'])['status'], 'failed')
            self.assertEqual(self.service._all('decisions'), [])
        task = self.recover()
        self.assertTrue(set(receipts).issubset({r['id'] for r in task['_self_recovery']['unknown_tools']}))
        for identity in receipts:
            self.assertEqual(self.service._get('native_receipts', identity)['state'], 'executing')
        # Actual unsuccessful turns still reach the limit after service interruptions.
        for attempt in (1, 2):
            self.assertEqual(task['_self_recovery']['attempt'], attempt)
            task.update(status='failed', summary='Provider unavailable')
            self.service._put('tasks', task)
            task = self.recover()
        self.assertEqual(self.service._all('decisions'), [])
        self.assertEqual(task['status'], 'scheduled')
        self.assertEqual(self.sync()[0]['activity'], 'Will retry automatically')

    def test_scheduled_retry_survives_restart_without_duplicates_or_cards(self):
        self.stranded()
        for _ in range(2):
            task = self.recover()
            task.update(status='failed', summary='Provider unavailable')
            self.service._put('tasks', task)
        task = self.recover()
        self.assertEqual(task['status'], 'scheduled')
        self.assertEqual(task['_self_recovery']['attempt'], 3)
        self.assertEqual(self.service._all('decisions'), [])
        count = len(self.service._all('tasks'))
        for _ in range(3):
            self.recover()
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.tracker)
        self.recover()
        self.assertEqual(len(self.service._all('tasks')), count)
        self.assertEqual(self.service._get('tasks', task['id'])['scheduled_at'], task['scheduled_at'])
        self.assertEqual(self.sync()[0]['activity'], 'Will retry automatically')

    def test_retries_back_off_and_do_not_reserve_worker_capacity(self):
        from datetime import datetime, timezone
        from legacy_work_sync import retry_delay
        self.assertEqual([retry_delay(n) for n in range(8)], [0, 0, 300, 600, 1200, 2400, 3600, 3600])
        self.stranded()
        for attempt in range(2):
            task = self.recover()
            task.update(status='failed', summary='Provider unavailable')
            self.service._put('tasks', task)
        task = self.recover()
        delay = (datetime.fromisoformat(task['scheduled_at']) - datetime.now(timezone.utc)).total_seconds()
        self.assertTrue(290 < delay <= 300)
        row = self.sync()[0]
        self.assertEqual(row['work_state'], 'waiting')
        self.assertIsNone(row['work_queue_position'])
        self.assertEqual(self.service._all('decisions'), [])

    def test_human_reply_resumes_scheduled_check_immediately(self):
        self.stranded()
        for _ in range(2):
            task = self.recover()
            task.update(status='failed')
            self.service._put('tasks', task)
        task = self.recover()
        before = len(self.service._all('tasks'))
        self.write('POST', '/v1/tasks/' + task['id'] + '/replies', dict(text='Continue now using the saved result'))
        saved = self.service._get('tasks', task['id'])
        self.assertEqual(saved['status'], 'queued')
        self.assertIsNone(saved['scheduled_at'])
        self.assertEqual(saved['_self_recovery']['attempt'], 0)
        self.assertEqual(len(self.service._all('tasks')), before)

    def test_recovery_prompt_keeps_verification_and_decision_instructions(self):
        self.stranded()
        task = self.recover()
        prompt, _ = turn_input(dict(operation='start', task=task, history=[]))
        self.assertIn('Never repeat a write while its effect remains unknown', prompt)
        self.assertIn('coconut_ask', prompt)
        self.assertIn('not a new human instruction or approval', prompt)
        self.assertNotIn('A question does not restart', prompt)

    def test_pending_context_recovers_once_with_original_question_and_receipts(self):
        self.start()
        from coconut.service import YieldForDecision
        try:
            self.service.native_event(self.task['id'], 'native_question', dict(title='Choose an account',
                options=[dict(id='first', label='First account'), dict(id='second', label='Second account')]))
        except YieldForDecision:
            pass
        decision = self.service._all('decisions')[-1]
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Check which account owns the record', revision=1))
        old = self.service._all('contexts')[-1]
        old.update(status='working')
        self.service._put('contexts', old)
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'read receipt'}), context_id=old['id'])
        old.update(status='failed', _background_completion=['Research result already delivered'])
        self.service._put('contexts', old)
        before = len(self.service._all('messages'))
        self.service._queue_context_recoveries()
        new = self.service._all('contexts')[-1]
        self.assertNotEqual(old['id'], new['id'])
        self.assertEqual(new['decision_id'], old['decision_id'])
        self.assertEqual(new['_background_completion'], old['_background_completion'])
        self.assertEqual(new['question'], old['question'])
        self.assertIn('read receipt', new['_context_recovery'])
        self.assertEqual(len(self.service._all('messages')), before)
        self.service._queue_context_recoveries()
        self.assertEqual(len(self.service._all('contexts')), 2)
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'executing')
        from coconut.service import ServiceError
        new.update(status='working')
        new.pop('_background_completion', None)
        self.service._put('contexts', new)
        with self.assertRaises(ServiceError):
            self.service.resolve_decision_reply(new['id'], dict(choice='approve', option_id='first', quote='read receipt'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_new_message_and_process_result_survive_recovery_prompt(self):
        self.stranded()
        task = self.recover()
        message = dict(id='new', task_id=task['id'], role='user', kind='text', text='Only inspect; do not push')
        task['_background_completion'] = ['Existing command completed with receipt 123']
        task['_work_context'] = {'title': 'Current work title'}
        prompt, _ = turn_input(dict(operation='start', task=task, history=[message]))
        for text in ('Only inspect; do not push', 'receipt 123', 'Current work title', 'Never repeat a write'):
            self.assertIn(text, prompt)

    def test_context_recovery_receipts_reach_hermes_with_original_message(self):
        request = dict(operation='context', question='Ignored fallback', history=[
            dict(id='original', role='user', kind='text', text='Check the result')],
            task=dict(id='task', title='Check record', _context_message_id='original',
                _context_recovery='Verify unknown receipt 456 before repeating its write.', pending_decision={}))
        prompt, _ = turn_input(request)
        self.assertIn('Check the result', prompt)
        self.assertIn('Verify unknown receipt 456', prompt)

    def test_automatic_retry_keeps_receipt_from_failed_recovery_attempt(self):
        self.stranded()
        for _ in range(2):
            task = self.recover()
            task.update(status='working', _native_turn_id='recovery-turn')
            self.service._put('tasks', task)
            receipt = self.service.native_event(task['id'], 'native_call',
                dict(name='terminal', arguments={'command': 'git push'}))
            task.update(status='failed')
            self.service._put('tasks', task)
        task = self.recover()
        self.assertEqual(task['status'], 'scheduled')
        self.assertEqual(self.service._all('decisions'), [])
        self.assertIn(receipt['id'], [r['id'] for r in task['_self_recovery']['unknown_tools']])
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'executing')
        self.assertNotEqual(task.get('_native_turn_id'), 'recovery-turn')

    def test_unknown_result_does_not_leave_an_undeliverable_wait_blocking_recovery(self):
        self.start()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'git push'}))
        item = self.stranded()
        helper = dict(self.service._get('tasks', self.task['id']), id='finished-helper', status='completed')
        self.service._put('tasks', helper)
        record = copy.deepcopy(self.service._get('native_receipts', receipt['id']))
        record.update(id='saved-wait', state='completed', _wait_task_id='finished-helper', result='{}')
        self.service._put('native_receipts', record)
        self.assertTrue(self.service._background_pending(self.task['id']))
        self.assertEqual(self.sync()[0]['work_state'], 'needs_recovery')
        recovered = self.recover()
        self.assertEqual(self.sync()[0]['task_id'], recovered['id'])
        self.assertNotEqual(recovered['id'], self.task['id'])
        self.assertEqual(recovered['_self_recovery']['unknown_tools'][0]['id'], receipt['id'])

    def test_newer_merge_report_prevents_recovery_of_an_old_open_report(self):
        from test_service_pr import URL
        self.start()
        first = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        other_bot = self.write('POST', '/v1/bots', dict(name='Reviewer', role='Review'))
        other = self.write('POST', '/v1/bots/' + other_bot['id'] + '/messages', dict(text='Read the merge result', mode='queue'))
        owner = self.service._get('tasks', other['id'])
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        merged = dict(self.service._get('artifacts', first['id']), id='merge-report',
            bot_id=other_bot['id'], task_id=owner['id'], _review_task_id=owner['id'], status='merged',
            pr_state='merged', observed_at='2099-01-01T00:00:00Z', checked_at='2099-01-01T00:00:00Z')
        self.service._put('artifacts', merged)
        self.start(status='failed')
        self.service.running.clear()
        before = len(self.service._all('tasks'))
        self.recover()
        self.assertEqual(len(self.service._all('tasks')), before)

    def test_duplicate_pr_reports_get_one_recovery_owner(self):
        from test_service_pr import URL
        self.start()
        first = self.service._register_pr_artifact(self.task['id'], dict(url=URL))
        other_bot = self.write('POST', '/v1/bots', dict(name='Reviewer', role='Review'))
        other = self.write('POST', '/v1/bots/' + other_bot['id'] + '/messages', dict(text='Review this PR', mode='queue'))
        second_task = self.service._get('tasks', other['id'])
        second_task.update(status='completed', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', second_task)
        second = dict(self.service._get('artifacts', first['id']), id='second-report',
                      bot_id=other_bot['id'], task_id=other['id'], _review_task_id=other['id'])
        self.service._put('artifacts', second)
        self.start(status='failed')
        self.service.running.clear()
        count = len(self.service._all('tasks'))
        recovery = self.recover()
        self.assertEqual(len(self.service._all('tasks')), count + 1)
        peers = [a for a in self.service._all('artifacts') if a['kind'] == 'pr']
        self.assertEqual([a['_review_task_id'] for a in peers if a.get('_review_task_id')], [recovery['id']])
        self.assertEqual(len(recovery['_self_recovery']['work']), 1)
        self.recover()
        self.assertEqual(len(self.service._all('tasks')), count + 1)
