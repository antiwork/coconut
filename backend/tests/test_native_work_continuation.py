import copy
import os
import unittest
from unittest.mock import Mock, patch

from coconut.service import ServiceError
import test_targeted_bot_continuation as fixtures


class NativeWorkContinuationTests(unittest.TestCase):
    setUp = fixtures.TargetedBotContinuationTests.setUp
    tearDown = fixtures.TargetedBotContinuationTests.tearDown
    write = fixtures.TargetedBotContinuationTests.write
    prepare = fixtures.TargetedBotContinuationTests.prepare

    def native(self):
        owner, source = self.prepare()
        owner.update(status='completed', _native_outcome_id='t_existing')
        self.service._put('tasks', owner)
        self.service.kanban = Mock(tasks={owner['id']: dict(id='t_existing', status='triage')})
        return owner, source

    def resume(self, source):
        return self.service.emit(source['id'], 'work_continue',
            dict(task_id='t_existing', message='The corrected commit passed review. Finish its acceptance check.'))

    def test_requester_resumes_same_outcome_and_preserves_its_result_route(self):
        owner, source = self.native()
        before = len(self.service._all('tasks'))
        result = self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(result['task_id'], owner['id'])
        self.assertEqual(continued['status'], 'queued')
        self.assertNotEqual(continued.get('_native_turn_id'), owner.get('_native_turn_id'))
        for key in ('_native_outcome_id', '_execution_session_id', '_work_todos',
                    'source_task_id', 'source_bot_id', 'thread_id', '_preference_source_ids'):
            self.assertEqual(continued.get(key), owner.get(key))
        self.assertEqual(self.resume(source), result)
        self.assertEqual(self.service._get('tasks', owner['id']), continued)

    def test_new_chat_can_continue_work_without_replacing_the_original_requester(self):
        owner, original = self.native()
        new = self.write('/v1/bots/' + original['bot_id'] + '/messages', {'text': 'Continue the original correction.'})
        source = self.service._get('tasks', new['id'])
        source['status'] = 'working'
        self.service._put('tasks', source)
        self.assertNotEqual(source['exchange_id'], owner['exchange_id'])
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['source_task_id'], original['id'])
        self.assertEqual(continued['exchange_id'], owner['exchange_id'])
        self.assertEqual(continued['source_bot_id'], original['bot_id'])

    def test_another_bot_cannot_resume_the_outcome(self):
        owner, source = self.native()
        other = self.write('/v1/bots', {'name': 'Unrelated'})
        self.service._put('tasks', dict(source, bot_id=other['id']))
        with self.assertRaisesRegex(ServiceError, 'owner or its requesting bot'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_cannot_resume_active_or_archived_native_work(self):
        owner, source = self.native()
        for state in ('running', 'ready', 'archived'):
            self.service.kanban.tasks[owner['id']]['status'] = state
            with self.subTest(state=state), self.assertRaisesRegex(ServiceError, 'not blocked'):
                self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_completed_outcome_accepts_new_facts_without_changing_execution(self):
        owner, source = self.native()
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        result = self.resume(source)
        continued = self.service._get('tasks', result['task_id'])
        self.assertEqual(continued['id'], owner['id'])
        self.assertEqual(continued['_execution_session_id'], owner['_execution_session_id'])
        self.assertEqual(continued['source_task_id'], owner['source_task_id'])
        self.assertEqual(continued['status'], 'queued')
        self.assertNotEqual(continued['_native_turn_id'], owner.get('_native_turn_id'))

    def reported_work(self):
        from uuid import uuid4
        owner, source = self.native()
        reporter = dict(owner, id=str(uuid4()), status='waiting', _native_outcome_id=None,
                        _work_context_id=None, created_at='2099-01-01T00:00:00Z')
        reporter['_execution_session_id'] = reporter['id']
        self.service._put('tasks', reporter)
        item = dict(id=str(uuid4()), kind='work', bot_id=owner['bot_id'], task_id=reporter['id'],
            _work_key='retained-work', title='Continue the saved correction', status='waiting',
            activity='Waiting for the original worker', created_at=owner['created_at'], observed_at=owner['created_at'],
            _work_origin_task_id=owner['id'], _work_previous_task_ids=[owner['id']], _native_outcome_id='old-report')
        self.service._put('artifacts', item)
        owner['_work_context_id'] = item['id']
        self.service._put('tasks', owner)
        return owner, source, reporter, item

    def test_continuation_restores_an_owner_displaced_by_a_progress_report(self):
        owner, source, reporter, item = self.reported_work()
        self.resume(source)
        restored = self.service._get('artifacts', item['id'])
        self.assertEqual(restored['task_id'], owner['id'])
        self.assertEqual(restored['_native_outcome_id'], owner['_native_outcome_id'])
        self.assertIn(reporter['id'], restored['_work_previous_task_ids'])
        self.assertEqual(self.service._get('tasks', reporter['id']), reporter)

    def test_continuation_does_not_take_work_from_another_execution(self):
        owner, source, reporter, item = self.reported_work()
        cases = ({'_native_outcome_id': 'another'}, {'_work_context_id': item['id']},
                 {'status': 'working'}, {'status': 'paused'}, {'status': 'cancelled'}, {'session_id': 'old'})
        for changes in cases:
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(reporter, **changes))
                with self.assertRaisesRegex(ServiceError, 'another execution'):
                    self.resume(source)
                self.assertEqual(self.service._get('artifacts', item['id']), item)
                self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.service._put('tasks', reporter)
        self.service._put('decisions', dict(id='pending-report', task_id=reporter['id'], status='pending'))
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(source)

    def test_progress_report_keeps_native_work_owner_and_binding(self):
        owner, source, reporter, item = self.reported_work()
        item.update(task_id=owner['id'], _native_outcome_id=owner['_native_outcome_id'])
        self.service._put('artifacts', item)
        reporter['status'] = 'working'
        self.service._put('tasks', reporter)
        self.service.emit(reporter['id'], 'artifact', dict(kind='work', key=item['_work_key'],
            title=item['title'], activity='The human revised the local scope', status='waiting',
            completion_condition='Verify the revised scope in the original worktree'))
        saved = self.service._get('artifacts', item['id'])
        self.assertEqual(saved['task_id'], owner['id'])
        self.assertEqual(saved['_native_outcome_id'], owner['_native_outcome_id'])
        self.assertEqual(saved['_work_report_task_id'], reporter['id'])
        self.assertEqual(saved['completion_condition'], 'Verify the revised scope in the original worktree')

    def test_cannot_resume_paused_work_or_pending_dependencies(self):
        owner, source = self.native()
        for changes in ({'status': 'paused'}, {'_waiting_on': [source['id']]}):
            self.service._put('tasks', dict(owner, **changes))
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                self.resume(source)
        self.service._put('tasks', owner)
        with patch.object(self.service, '_background_pending', return_value=True), self.assertRaisesRegex(ServiceError, 'active work'):
            self.resume(source)

    def test_pending_human_decision_remains_pending(self):
        owner, source = self.native()
        self.service._put('decisions', dict(id='decision', task_id=owner['id'], status='pending'))
        before = copy.deepcopy(self.service._all('decisions'))
        with self.assertRaisesRegex(ServiceError, 'existing decision'):
            self.resume(source)
        self.assertEqual(self.service._all('decisions'), before)

    def test_unconfirmed_actions_and_saved_results_reach_the_continued_worker(self):
        from coconut.hermes_worker import conversation_input
        owner, source = self.native()
        self.service._put('tasks', dict(owner, status='working', _native_turn_id='previous-turn'))
        unknown = self.service.emit(owner['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'git push'}))
        finished = self.service.emit(owner['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'git status'}))
        self.service.emit(owner['id'], 'native_result', dict(id=finished['id'], result='The commit is saved.'))
        owner.update(status='failed', _native_turn_id='previous-turn',
            _self_recovery={'unknown_tools': [dict(id='older', action={'name': 'publish'})]})
        self.service._put('tasks', owner)
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual({r['id'] for r in continued['_self_recovery']['unknown_tools']}, {'older', unknown['id']})
        self.assertEqual(continued['_continuation_receipt_ids'], [finished['id']])
        prompt = conversation_input(dict(operation='start', task=continued, history=[]))[0]
        self.assertIn('Never repeat a write while its effect remains unknown', prompt)
        self.assertIn('git push', prompt)
        self.assertEqual(self.service._get('native_receipts', unknown['id'])['state'], 'executing')

    def test_completed_turn_does_not_replay_old_observations_for_new_facts(self):
        owner, source = self.native()
        self.service._put('tasks', dict(owner, status='working', _native_turn_id='completed-turn'))
        record = self.service.emit(owner['id'], 'native_call', dict(name='terminal', arguments={'command': 'gh pr view'}))
        self.service.emit(owner['id'], 'native_result', dict(id=record['id'], result='Checks are pending.'))
        self.service._put('tasks', dict(owner, _native_turn_id='completed-turn'))
        self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id'])['_continuation_receipt_ids'], [])

    def test_continued_result_reaches_requester_after_an_earlier_return(self):
        owner, source = self.native()
        self.service._prepare_return(owner, 'Acceptance is blocked.', 'completed')
        self.service._put('tasks', owner)
        with self.assertRaisesRegex(ServiceError, 'previous result'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        self.service._drain_returns()
        returned = [t for t in self.service._all('tasks') if t.get('_is_return')]
        self.assertEqual(len(returned), 1)
        self.service._run_task(returned[0]['id'])
        self.service._put('tasks', dict(source, status='working'))
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.service._prepare_return(continued, 'Acceptance passed.', 'completed')
        continued['status'] = 'completed'
        self.service._put('tasks', continued)
        self.service._drain_returns()
        self.service._drain_returns()
        deliveries = [d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']]
        self.assertEqual([d['text'] for d in deliveries], ['Acceptance is blocked.', 'Acceptance passed.'])
        self.assertEqual({d['target_bot_id'] for d in deliveries}, {source['bot_id']})

    def test_explicit_work_pause_blocks_bot_continuation(self):
        owner, source = self.native()
        with patch.object(self.service, '_task_work_is_paused', return_value=True), self.assertRaisesRegex(ServiceError, 'paused'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_saved_human_followup_keeps_the_continuation_receipt_idempotent(self):
        owner, source = self.native()
        self.service._put('tasks', dict(owner, status='working', _turn_message_ids=[]))
        self.write('/v1/tasks/' + owner['id'] + '/replies', {'text': 'Keep the correction local.'})
        self.service.emit(owner['id'], 'failed', {'summary': 'Provider stopped before receiving the follow-up.'})
        self.assertTrue(self.service._get('tasks', owner['id'])['_blocked_followup'])
        self.service._drain_returns()
        self.service._put('tasks', dict(source, status='working'))
        result = self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertNotIn('_blocked_followup', continued)
        self.assertEqual(self.resume(source), result)
        self.assertEqual(self.service._get('tasks', owner['id']), continued)

    def test_human_restriction_survives_failure_before_provider_input(self):
        from coconut.hermes_worker import conversation_input
        owner, source = self.native()
        owner.update(status='queued', _kanban_claim={})
        self.service._put('tasks', owner)
        self.write('/v1/tasks/' + owner['id'] + '/replies', {'text': 'Do not publish. Keep the change local.'})
        start = self.provider.start_task
        def fail_before_input(*args):
            raise RuntimeError('Provider unavailable before input')
        self.provider.start_task = fail_before_input
        self.service._run_task(owner['id'])
        failed = self.service._get('tasks', owner['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue(failed['_turn_message_ids'])
        self.provider.start_task = start
        self.service._drain_returns()
        self.service._put('tasks', dict(source, status='working'))
        self.resume(source)
        captured = []
        def verify(task, bot, history, emit):
            captured.append(conversation_input(dict(operation='start', task=task, history=history))[0])
            emit('completed', {'summary': 'The change remains local.'})
        self.provider.start_task = verify
        self.service._run_task(owner['id'])
        self.assertEqual(len(captured), 1)
        self.assertIn('Do not publish. Keep the change local.', captured[0])


class NativeContinuationProviderTests(unittest.TestCase):
    import test_provider as provider_fixtures
    setUp = provider_fixtures.ProviderTests.setUp
    tearDown = provider_fixtures.ProviderTests.tearDown
    fake_hermes = provider_fixtures.ProviderTests.fake_hermes

    def test_tool_receives_rejection_and_can_continue_another_native_task(self):
        provider = self.fake_hermes(
            "import json\n"
            "rejected = json.loads(registry.dispatch('coconut_continue_work', {'task_id': 't_active', 'message': 'Continue.'}))\n"
            "assert rejected == {'status': 'rejected', 'error': 'The worker is still active.'}\n"
            "receipt = json.loads(registry.dispatch('coconut_continue_work', {'task_id': 't_blocked', 'message': 'The checks passed.'}))\n"
            "assert receipt == {'status': 'queued', 'task_id': 'existing', 'native_task_id': 't_blocked'}\n"
            "return {'completed': True, 'final_response': 'The existing work will continue.'}\n")
        events = []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'work_continue':
                if data['task_id'] == 't_active':
                    raise ServiceError(409, 'The worker is still active.')
                return dict(status='queued', task_id='existing', native_task_id='t_blocked')
        with patch.dict(os.environ, ANTHROPIC_API_KEY='test-only'):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(len([e for e in events if e[0] == 'work_continue']), 2)
        self.assertEqual(events[-1][0], 'completed', events)

    def test_premature_merge_validation_reaches_the_worker_without_failing(self):
        provider = self.fake_hermes(
            "import json\n"
            "result = json.loads(registry.dispatch('coconut_ask', {'title': 'Merge?', 'options': [{'id': 'merge', 'label': 'Merge'}]}))\n"
            "assert result['status'] == 'validation_failed'\n"
            "assert 'pending worker' in result['error']\n"
            "return {'completed': True, 'final_response': 'I will read the review result first.'}\n")
        events = []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'native_question':
                raise ServiceError(400, 'The PR review still has a pending worker or delegated result.')
        with patch.dict(os.environ, ANTHROPIC_API_KEY='test-only'):
            provider.start_task(self.task, self.bot, [], emit)
        self.assertEqual(events[-1][0], 'completed', events)
        self.assertNotIn('failed', [kind for kind, _ in events])


if __name__ == '__main__':
    unittest.main()
