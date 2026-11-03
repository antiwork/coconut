"""Bot continuations keep the saved execution without changing worktree ownership."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service, ServiceError, YieldForDecision
import test_work_progress as fixtures
from test_work_progress import progress
import legacy_work_sync
from turn_text import turn_input
from hermes_env import needs_hermes, SOURCE

setUpModule, tearDownModule = legacy_work_sync.scope()


class TargetedBotContinuationTests(unittest.TestCase):
    setUp = fixtures.WorkProgressTests.setUp
    tearDown = fixtures.WorkProgressTests.tearDown
    write = fixtures.WorkProgressTests.write

    def prepare(self):
        previous = self.service._get('tasks', self.child)
        previous.update(status='failed', _execution_session_id=self.child,
                        _conversation_epoch=self.service._setting('conversation_epoch'),
                        _work_todos=progress(), _preference_source_ids=['saved-human-source'])
        self.service._put('tasks', previous)
        source = self.service._get('tasks', self.parent['id'])
        source.update(status='working', _execution_session_id=source['id'])
        self.service._put('tasks', source)
        return previous, source

    def send(self, source, previous, message='Continue the approved correction.', **changes):
        """A message of the bot that continues the failed task `previous` (continuation_task_id)."""
        payload = dict(target_bot_id=self.dev['id'], text=message, continuation_task_id=previous['id']) | changes
        return payload, self.service.emit(source['id'], 'bot_message', payload)

    def assert_rejected(self, source, previous, reason):
        before = len(self.service._all('tasks'))
        # A message with new text, so the check does not find the delivery of an earlier message.
        with self.assertRaisesRegex(ServiceError, reason):
            self.send(source, previous, message='Continue the approved correction: ' + str(uuid4()))
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(self.service._get('tasks', source['id'])['status'], 'working')

    def test_target_survives_an_unrelated_helper_callback_and_restart(self):
        previous, source = self.prepare()
        helper = self.write('/v1/bots', {'name': 'Care'})
        with self.service.transaction():
            callback = self.service._store_bot_message(source['id'], {'target_bot_id': helper['id'], 'text': 'Check local setup.'})
            callback = self.service._get('tasks', callback['task_id'])
            source = dict(source, id=str(uuid4()), source_task_id=callback['id'], _is_return=True)
            self.service._put('tasks', source)
        before_users = len([m for m in self.service._all('messages') if m['role'] == 'user'])
        payload, receipt = self.send(source, previous)
        continued = self.service._get('tasks', receipt['task_id'])
        self.assertNotEqual(continued['id'], previous['id'])
        self.assertEqual(continued['_execution_session_id'], previous['_execution_session_id'])
        self.assertEqual(continued['parent_task_id'], previous['id'])
        self.assertEqual(continued['_work_todos'], previous['_work_todos'])
        self.assertEqual(continued['_preference_source_ids'], ['saved-human-source'])
        self.assertEqual(continued['_bot_messages'][-1]['sender_bot_id'], self.desk['id'])
        self.assertEqual(continued['source_task_id'], source['id'])
        self.assertEqual(len([m for m in self.service._all('messages') if m['role'] == 'user']), before_users)
        # A repeated delivery of the same message finds the same task, also after a restart.
        self.assertEqual(self.service.emit(source['id'], 'bot_message', payload)['task_id'], receipt['task_id'])
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertEqual(self.service.emit(source['id'], 'bot_message', payload)['task_id'], receipt['task_id'])
        self.assertEqual(self.service._get('tasks', continued['id'])['_execution_session_id'], self.child)
        self.service._put('tasks', dict(self.service._get('tasks', source['id']), status='working'))
        self.assert_rejected(source, previous, 'already has active work')

    def test_wrong_owner_scope_and_terminal_state_do_not_change_execution(self):
        previous, source = self.prepare()
        cases = [dict(status=s) for s in ('working', 'queued', 'waiting', 'paused', 'cancelled', 'completed')]
        cases += [dict(bot_id=self.desk['id']), dict(session_id='old'), dict(thread_id='another'),
                  dict(conversation_bot_id=self.dev['id']), dict(_conversation_epoch='old'),
                  dict(_native_outcome_id='native-outcome'), dict(_chat_session=True),
                  dict(_native_job={'id': 'scheduled-job'}), dict(routine_id='scheduled-job')]
        for changes in cases:
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(previous, **changes))
                before = len(self.service._all('tasks'))
                self.assert_rejected(source, previous, 'Choose a failed task')
                self.assertEqual(len(self.service._all('tasks')), before)
                self.assertEqual(self.service._get('tasks', previous['id'])['_execution_session_id'], self.child)
        self.service._put('tasks', previous)

    def test_pending_decision_and_cancelled_exchange_remain_blocked(self):
        previous, source = self.prepare()
        self.service._put('decisions', dict(id='pending', task_id=previous['id'], status='pending'))
        self.assert_rejected(source, previous, 'pending decision')
        self.service._put('decisions', dict(id='pending', task_id=previous['id'], status='executed'))
        exchange = self.service._get('exchanges', previous['exchange_id'])
        self.service._put('exchanges', dict(exchange, cancelled=True))
        self.assert_rejected(source, previous, 'paused or cancelled')

    def test_explicit_work_pause_and_live_process_remain_blocked(self):
        previous, source = self.prepare()
        with patch.object(self.service, '_automatic_work_is_paused', return_value=True):
            self.assert_rejected(source, previous, 'paused or cancelled')
        with patch.object(self.service, '_background_pending', return_value=True):
            self.assert_rejected(source, previous, 'already has active work')

    def test_optional_proposal_does_not_block_authorized_continuation(self):
        previous, source = self.prepare()
        self.service._put('decisions', dict(id='optional', task_id=previous['id'],
                                           status='pending', inbox_kind='proposal'))
        _, receipt = self.send(source, previous)
        self.assertEqual(self.service._get('tasks', receipt['task_id'])['_execution_session_id'], self.child)
        self.assertEqual(self.service._get('decisions', 'optional')['status'], 'pending')

    def test_malformed_continuation_id_is_rejected(self):
        previous, source = self.prepare()
        for value in (' ', '', None, 42, 'x' * 101):
            with self.subTest(value=value):
                before = len(self.service._all('tasks'))
                with self.assertRaisesRegex(ServiceError, 'continuation_task_id'):
                    self.send(source, previous, continuation_task_id=value)
                self.assertEqual(len(self.service._all('tasks')), before)
                self.assertEqual(self.service._get('tasks', source['id'])['status'], 'working')

    def test_unknown_effects_use_existing_recovery_context_before_retry(self):
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working'))
        unknown = self.service.emit(previous['id'], 'native_call', {
            'name': 'terminal', 'arguments': {'command': 'git push'}})
        previous['_self_recovery'] = dict(unknown_tools=[dict(id='older', action={'name': 'publish'})])
        self.service._put('tasks', previous)
        _, receipt = self.send(source, previous)
        captured = []
        def verify(task, bot, history, emit):
            captured.append(task)
            emit('completed', {'summary': 'Verified the saved result.'})
        self.provider.start_task = verify
        self.service._run_task(receipt['task_id'])
        recovery = captured[0]['_self_recovery']
        self.assertEqual({r['id'] for r in recovery['unknown_tools']}, {'older', unknown['id']})
        prompt = turn_input(dict(operation='start', task=captured[0], history=[]))[0]
        self.assertIn('Never repeat a write while its effect remains unknown', prompt)
        self.assertIn('git push', prompt)
        self.assertEqual(self.service._get('native_receipts', unknown['id'])['state'], 'executing')

    def test_helper_can_target_failed_requester_without_becoming_a_return(self):
        previous, source = self.prepare()
        source.update(source_bot_id=self.dev['id'], source_task_id=previous['id'],
                      _reply_to_task_id=previous['id'])
        self.service._put('tasks', source)
        _, receipt = self.send(source, previous)
        continued = self.service._get('tasks', receipt['task_id'])
        self.assertFalse(continued['_is_return'])
        self.assertNotIn('_assignment_task_id', continued)
        self.assertEqual(continued['_reply_to_task_id'], source['id'])
        self.provider.mode = 'done'
        self.service._run_task(continued['id'])
        self.assertEqual(self.service._get('tasks', continued['id'])['status'], 'completed')

    def test_rejected_decision_survives_failure_before_provider_receives_it(self):
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working'))
        with self.assertRaises(YieldForDecision):
            self.service.native_event(previous['id'], 'native_question', {
                'title': 'Publish the correction?',
                'options': [dict(id='yes', label='Publish'), dict(id='no', label='Keep local')]})
        decision = self.service._all('decisions')[-1]
        self.write('/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, choice='reject'))
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.resume_task = fail_before_input
        self.service._run_task(previous['id'])
        previous = self.service._get('tasks', previous['id'])
        self.assertEqual(previous['status'], 'failed')
        self.assertEqual(previous['_resume_decision'], decision['id'])
        _, receipt = self.send(source, previous)
        captured = []
        def check_restriction(task, bot, history, emit):
            captured.append(task)
            emit('completed', {'summary': 'The correction stays local.'})
        self.provider.start_task = check_restriction
        self.service._run_task(receipt['task_id'])
        saved = captured[0]['_self_recovery']['decisions']
        self.assertEqual(saved[0]['id'], decision['id'])
        self.assertEqual(saved[0]['status'], 'rejected')
        prompt = turn_input(dict(operation='start', task=captured[0], history=[]))[0]
        self.assertIn('Publish the correction?', prompt)
        self.assertIn('rejected', prompt)
        self.assertIn('This is not a new human instruction or approval', prompt)
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'rejected')

    def test_undelivered_human_followup_reaches_the_continued_session(self):
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working', _turn_message_ids=[]))
        self.write('/v1/tasks/' + previous['id'] + '/replies', {'text': 'Do not publish. Keep the correction local.'})
        self.service.emit(previous['id'], 'failed', {'summary': 'Provider interrupted.'})
        previous = self.service._get('tasks', previous['id'])
        pending = previous['_blocked_followup']
        _, receipt = self.send(source, previous)
        captured = []
        def check_followup(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The correction stays local.', '_work_todos': progress('completed')})
        self.provider.start_task = check_followup
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        self.assertTrue(set(pending['message_ids']).issubset(task['_turn_message_ids']))
        prompt = turn_input(dict(operation='start', task=task, history=history))[0]
        self.assertIn('Do not publish. Keep the correction local.', prompt)
        self.assertNotIn('_blocked_followup', self.service._get('tasks', previous['id']))

    def test_selected_human_followup_survives_provider_failure(self):
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='queued'))
        restriction = 'Do not publish. Keep the correction local.'
        self.write('/v1/tasks/' + previous['id'] + '/replies', {'text': restriction})
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(previous['id'])
        previous = self.service._get('tasks', previous['id'])
        self.assertEqual(previous['status'], 'failed')
        self.assertTrue(previous['_turn_message_ids'])
        self.assertFalse(previous.get('_steering'))
        self.assertNotIn('_blocked_followup', previous)
        _, receipt = self.send(source, previous)
        captured = []
        def check_followup(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The correction stays local.', '_work_todos': progress('completed')})
        self.provider.start_task = check_followup
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        self.assertTrue(set(previous['_turn_message_ids']).issubset(task['_turn_message_ids']))
        self.assertIn(restriction, turn_input(dict(operation='start', task=task, history=history))[0])

    def test_bot_correction_survives_failure_before_provider_input(self):
        previous, source = self.prepare()
        _, receipt = self.send(source, previous, message='Keep this change local. Do not publish.')
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(receipt['task_id'])
        previous = self.service._get('tasks', receipt['task_id'])
        self.assertEqual(previous['status'], 'failed')
        _, receipt = self.send(source, previous, message='Continue from the saved results.')
        captured = []
        def check_context(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The change stays local.', '_work_todos': progress('completed')})
        self.provider.start_task = check_context
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        prompt = turn_input(dict(operation='start', task=task, history=history))[0]
        self.assertIn('Keep this change local. Do not publish.', prompt)
        self.assertIn('Continue from the saved results.', prompt)
        self.assertEqual(task['_bot_messages'][-2]['sender_bot_id'], self.desk['id'])

    def test_background_result_survives_failure_before_provider_input(self):
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working', _native_turn_id='process-start'))
        record = self.service.emit(previous['id'], 'native_call', dict(name='terminal', arguments={'background': True}))
        self.service.emit(previous['id'], 'native_result', dict(id=record['id'], result=json.dumps({'session_id': 'proc_review', 'notify_on_complete': True, 'status': 'running'})))
        record = self.service._get('native_receipts', record['id'])
        with self.service.transaction():
            self.service._resume_process_task(previous['id'], [(record, 'Review completed. Fix the currency label.')])
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(previous['id'])
        previous = self.service._get('tasks', previous['id'])
        self.assertEqual(previous['status'], 'failed')
        self.assertTrue(self.service._get('native_receipts', record['id'])['_process_delivery'])
        _, receipt = self.send(source, previous)
        captured = []
        def check_result(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The label is corrected.', '_work_todos': progress('completed')})
        self.provider.start_task = check_result
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        self.assertIn('Review completed. Fix the currency label.', turn_input(dict(operation='start', task=task, history=history))[0])

    def test_pending_pr_review_survives_provider_failure(self):
        previous, source = self.prepare()
        review = dict(url='https://github.com/example/repo/pull/1', head_sha='current-head',
                      checks_state='failed', check_failures='The currency regression failed')
        self.service._put('tasks', dict(previous, status='queued', _pr_review=review))
        def fail_before_input(*args, **kwargs):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(previous['id'])
        previous = self.service._get('tasks', previous['id'])
        self.assertEqual(previous['status'], 'failed')
        _, receipt = self.send(source, previous)
        captured = []
        def check_review(task, bot, history, emit):
            captured.append((task, history))
            emit('completed', {'summary': 'The regression is fixed.', '_work_todos': progress('completed')})
        self.provider.start_task = check_review
        self.service._run_task(receipt['task_id'])
        task, history = captured[0]
        self.assertEqual(task['_pr_review'], review)
        prompt = turn_input(dict(operation='start', task=task, history=history))[0]
        self.assertIn('The currency regression failed', prompt)
        self.assertIn('current-head', prompt)

    def test_pr_checks_resume_the_continuation_instead_of_the_failed_owner(self):
        from dataclasses import replace
        from test_service_pr import Tracker, observation, URL
        previous, source = self.prepare()
        self.service.pr_tracker = Tracker(replace(observation(), pr_state='open', draft=False, checks_state='running'))
        self.service._put('tasks', dict(previous, status='working'))
        artifact = self.service.emit(previous['id'], 'artifact', dict(kind='pr', url=URL))
        artifact = self.service._get('artifacts', artifact['id'])
        artifact['_reviewed_states'] = {'older-head': 'saved-review-signal'}
        self.service._put('artifacts', artifact)
        self.service._put('tasks', previous)
        _, receipt = self.send(source, previous)
        artifact = self.service._get('artifacts', artifact['id'])
        self.assertEqual(artifact['_review_task_id'], receipt['task_id'])
        self.assertEqual(artifact['_reviewed_states'], {'older-head': 'saved-review-signal'})
        artifact.update(checks_state='passed', review_checks='new-review-signal')
        self.service._put('artifacts', artifact)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', previous['id'])['status'], 'failed')
        continued = self.service._get('tasks', receipt['task_id'])
        self.assertEqual(continued['status'], 'queued')
        self.service._put('tasks', dict(continued, status='waiting'))
        self.service._queue_pr_reviews()
        resumed = self.service._get('tasks', continued['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertEqual(resumed['_pr_review']['review_checks'], 'new-review-signal')
        self.assertEqual(self.service._get('tasks', previous['id'])['status'], 'failed')

    def test_native_routine_owner_cannot_resume_through_a_failed_callback(self):
        previous, source = self.prepare()
        routine = dict(previous, id='routine-owner', _execution_session_id='routine-owner',
                       _native_job={'id': 'scheduled-job'}, created_at='2000-01-01T00:00:00Z')
        self.service._put('tasks', routine)
        for changes in ({'_assignment_task_id': routine['id']}, {'_execution_session_id': routine['id']}):
            with self.subTest(changes=changes):
                callback = dict(previous, **changes)
                self.service._put('tasks', callback)
                self.assert_rejected(source, callback, 'Choose a failed task')

    def test_continuation_preserves_newer_pr_ownership_on_another_bot(self):
        from dataclasses import replace
        from test_service_pr import Tracker, observation, URL
        previous, source = self.prepare()
        self.service.pr_tracker = Tracker(replace(observation(), pr_state='open', draft=False, checks_state='running'))
        self.service._put('tasks', dict(previous, status='working'))
        artifact = self.service.emit(previous['id'], 'artifact', dict(kind='pr', url=URL))
        self.service._put('tasks', previous)
        other = dict(self.service._get('artifacts', artifact['id']), id='newer-owner', bot_id=self.desk['id'],
                     task_id=source['id'], _review_task_id=source['id'], _review_ownership_at=source['updated_at'])
        self.service._put('artifacts', other)
        _, receipt = self.send(source, previous)
        peers = [self.service._get('artifacts', identity) for identity in (artifact['id'], other['id'])]
        self.assertEqual(self.service._pr_review_owner(peers)['id'], other['id'])
        self.assertEqual(peers[0]['_review_task_id'], previous['id'])
        self.assertEqual(self.service._get('tasks', receipt['task_id'])['_execution_session_id'], previous['_execution_session_id'])

    def test_stale_target_cannot_restart_a_completed_continuation(self):
        previous, source = self.prepare()
        _, receipt = self.send(source, previous)
        continued = self.service._get('tasks', receipt['task_id'])
        self.service._put('tasks', dict(continued, status='completed'))
        self.assert_rejected(source, previous, 'already continued')

    def test_live_delegation_keeps_its_original_callback(self):
        previous, source = self.prepare()
        helper = dict(source, id=str(uuid4()), _execution_session_id='helper-execution',
                      source_task_id=previous['id'], _reply_to_task_id=previous['id'])
        self.service._put('tasks', helper)
        previous['_waiting_on'] = [helper['id']]
        self.service._put('tasks', previous)
        self.assert_rejected(source, previous, 'Wait for the result')
        self.assertEqual(self.service._get('tasks', helper['id']), helper)
        helper.update(status='completed', _return_pending={'text': 'Saved helper result.'})
        self.service._put('tasks', helper)
        self.assert_rejected(source, previous, 'Wait for the result')
        self.assertEqual(self.service._get('tasks', helper['id']), helper)
        self.assertEqual(self.service._get('tasks', previous['id'])['_waiting_on'], [helper['id']])

    def test_stale_target_cannot_restart_an_implicit_continuation(self):
        previous, source = self.prepare()
        source.update(_is_return=True, source_task_id=previous['id'])
        self.service._put('tasks', source)
        with self.service.transaction():
            receipt = self.service._store_bot_message(source['id'], dict(
                target_bot_id=self.dev['id'], text='Continue the approved correction.'))
        continued = self.service._get('tasks', receipt['task_id'])
        self.assertEqual(continued['_execution_session_id'], previous['_execution_session_id'])
        self.assertNotEqual(continued['parent_task_id'], previous['id'])
        self.provider.mode = 'done'
        self.service._run_task(continued['id'])
        self.assertEqual(self.service._get('tasks', continued['id'])['status'], 'completed')
        self.assert_rejected(source, previous, 'already continued')

    def test_work_tracks_targeted_continuation_and_does_not_queue_recovery(self):
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working'))
        work = self.service._register_work(previous['id'], dict(
            key='approved-correction', title='Finish the approved correction',
            status='working', activity='Checking the correction'))
        self.service._put('tasks', previous)
        source = dict(source, id=str(uuid4()), _waiting_on=[])
        self.service._put('tasks', source)
        _, receipt = self.send(source, previous)
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertEqual(row['work_state'], 'queued')
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(self.service._get('tasks', receipt['task_id'])['status'], 'queued')

    def test_completed_continuation_finishes_existing_automatic_work(self):
        previous, source = self.prepare()
        self.service._put('tasks', dict(previous, status='working'))
        work = self.service._register_work(previous['id'], dict(
            key='approved-correction', title='Finish the approved correction',
            status='working', activity='Checking the correction'))
        self.service._put('artifacts', dict(self.service._get('artifacts', work['id']), _automatic_work=True))
        self.service._put('tasks', previous)
        _, receipt = self.send(source, previous)
        self.service._sync_automatic_work()
        item = self.service._get('artifacts', work['id'])
        self.assertEqual(item['task_id'], receipt['task_id'])
        self.assertEqual(item['_work_origin_task_id'], previous['id'])
        self.provider.mode = 'done'
        self.service._run_task(receipt['task_id'])
        self.service._sync_automatic_work()
        row = self.service._work_projection([self.service._get('artifacts', work['id'])])[0]
        self.assertEqual(row['work_state'], 'completed')
        before = len(self.service._all('tasks'))
        self.service._queue_work_recoveries()
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(self.service._get('artifacts', work['id'])['task_id'], receipt['task_id'])

    @needs_hermes
    def test_continuation_reopens_real_dirty_worktree_without_rebinding(self):
        from coconut.work_workspace import acquire, git
        previous, source = self.prepare()
        root = Path(self.temp.name)
        repo = root / 'repo'
        repo.mkdir()
        git(repo, 'init')
        git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'Initial')
        with patch.object(sys, 'path', [str(Path(__file__).parents[1] / 'coconut'), SOURCE, *sys.path]):
            binding = acquire(root / 'workspaces', previous['_execution_session_id'], str(repo))
            note = Path(binding['path']) / 'pending.txt'
            note.write_text('Uncommitted correction')
            _, receipt = self.send(source, previous)
            continued = self.service._get('tasks', receipt['task_id'])
            resumed = acquire(root / 'workspaces', continued['_execution_session_id'], str(repo))
            self.assertEqual(resumed, binding)
            self.assertEqual(note.read_text(), 'Uncommitted correction')
            with self.assertRaisesRegex(ValueError, 'Another outcome'):
                acquire(root / 'workspaces', continued['id'], str(repo), binding['path'])


if __name__ == '__main__':
    unittest.main()
