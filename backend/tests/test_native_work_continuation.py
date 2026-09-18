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

    def scheduled_native(self):
        owner, source = self.prepare()
        owner.update(status='completed', _native_turn_id='completed-input',
                     _kanban_claim={'native_id': 't_existing', 'input': 'completed-input',
                                    'run_id': 'completed-run'})
        owner.pop('_native_outcome_id', None)
        self.service._put('tasks', owner)
        self.service.kanban = Mock(tasks={owner['id']: dict(
            id='t_existing', status='scheduled', current_run_id=None, worker_pid=None)})
        return owner, source

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

    def test_completed_successor_can_keep_its_previous_claim(self):
        owner, source = self.native()
        owner['_kanban_claim'] = {'native_id': 't_previous', 'input': 'previous-input'}
        self.service._put('tasks', owner)
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        self.assertEqual(self.resume(source)['task_id'], owner['id'])

    def test_requester_continues_quiescent_scheduled_outcome_from_durable_claim(self):
        owner, source = self.scheduled_native()
        task_count = len(self.service._all('tasks'))
        original_execution = owner['_execution_session_id']
        original_workspace = '/tmp/retained-native-workspace'
        self.service.kanban.tasks[owner['id']]['workspace_path'] = original_workspace
        result = self.resume(source)
        continued = self.service._get('tasks', result['task_id'])
        row = next(row for row in self.service._kanban_rows() if row['id'] == owner['id'])
        self.assertEqual(continued['id'], owner['id'])
        self.assertEqual(continued['_native_outcome_id'], 't_existing')
        self.assertEqual(continued['_execution_session_id'], original_execution)
        self.assertEqual(continued['status'], 'queued')
        self.assertEqual(row['state'], 'ready')
        self.assertEqual(row['native_id'], 't_existing')
        self.assertEqual(row['previous_input'], 'completed-input')
        self.assertNotEqual(row['input'], row['previous_input'])
        self.assertEqual(row['workspace'], original_workspace)
        self.assertEqual(self.resume(source), result)
        self.assertEqual(len(self.service._all('tasks')), task_count)
        self.assertEqual(len(continued['_bot_messages']),
                         len(self.service._get('tasks', owner['id'])['_bot_messages']))

    def test_owner_can_continue_quiescent_scheduled_outcome(self):
        owner, source = self.scheduled_native()
        self.service._put('tasks', dict(source, bot_id=owner['bot_id']))
        self.assertEqual(self.resume(source)['task_id'], owner['id'])

    def test_promoted_claim_retains_work_for_a_human_reply_after_completion(self):
        owner, source = self.scheduled_native()
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        continued['status'] = 'completed'
        self.service._put('tasks', continued)
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        result = self.write('/v1/tasks/' + owner['id'] + '/replies',
                            {'text': 'Check the additional review finding in this same work.'})
        next_task = self.service._get('tasks', result['id'])
        self.assertEqual(next_task['_execution_session_id'], owner['_execution_session_id'])
        item = self.service._get('artifacts', next_task['_work_context_id'])
        self.assertEqual(item['_native_outcome_id'], 't_existing')
        self.assertEqual(len([a for a in self.service._all('artifacts') if a['kind'] == 'work']), 1)

    def test_promoted_claim_reuses_its_existing_work_item(self):
        owner, source = self.scheduled_native()
        self.service._put('artifacts', dict(id='retained-work', task_id=owner['id'],
            bot_id=owner['bot_id'], kind='work', title='The original outcome', status='waiting'))
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['_work_context_id'], 'retained-work')
        item = self.service._get('artifacts', 'retained-work')
        self.assertEqual(item['title'], 'The original outcome')
        self.assertEqual(item['_native_outcome_id'], 't_existing')
        self.assertEqual(len(self.service._all('artifacts')), 1)

    def test_promoted_claim_binds_an_existing_context_before_a_human_reply(self):
        owner, source = self.scheduled_native()
        owner['_work_context_id'] = 'retained-work'
        self.service._put('tasks', owner)
        self.service._put('artifacts', dict(id='retained-work', task_id=owner['id'],
            bot_id=owner['bot_id'], kind='work', title='The original outcome', status='waiting'))
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        continued['status'] = 'completed'
        self.service._put('tasks', continued)
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        result = self.write('/v1/tasks/' + owner['id'] + '/replies',
                            {'text': 'Continue this work with the new review finding.'})
        followup = self.service._get('tasks', result['id'])
        self.assertEqual(followup['_native_outcome_id'], 't_existing')
        self.assertEqual(followup['_work_context_id'], 'retained-work')
        self.assertEqual(followup['_execution_session_id'], owner['_execution_session_id'])

    def test_promoted_claim_retains_a_standalone_pr(self):
        owner, source = self.scheduled_native()
        self.service._put('artifacts', dict(id='retained-pr', task_id=owner['id'],
            bot_id=owner['bot_id'], kind='pr', title='The original PR', status='open',
            url='https://github.com/example/project/pull/1', pr_state='open'))
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['_work_context_id'], 'retained-pr')
        self.assertEqual(self.service._get('artifacts', 'retained-pr')['_native_outcome_id'], 't_existing')
        self.assertEqual(len(self.service._all('artifacts')), 1)

    def test_claim_promotion_cannot_take_a_pr_from_another_execution(self):
        owner, source = self.scheduled_native()
        from uuid import uuid4
        other = dict(owner, id=str(uuid4()), _execution_session_id=str(uuid4()), status='working')
        self.service._put('tasks', other)
        item = dict(id='retained-pr', task_id=owner['id'], bot_id=owner['bot_id'],
                    kind='pr', title='The original PR', status='open',
                    url='https://github.com/example/project/pull/1', pr_state='open')
        for field in ('_review_task_id', '_activity_task_id'):
            self.service._put('artifacts', dict(item, **{field: other['id']}))
            with self.subTest(field=field), self.assertRaisesRegex(ServiceError, 'another execution'):
                self.resume(source)
            self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_claim_promotion_preserves_cancellation_of_its_retained_pr(self):
        owner, source = self.scheduled_native()
        item = dict(id='retained-pr', task_id=owner['id'], bot_id=owner['bot_id'],
                    kind='pr', title='The original PR', status='open',
                    url='https://github.com/example/project/pull/1', pr_state='open')
        self.service._put('artifacts', item)
        self.service.db.execute('INSERT INTO settings VALUES (?,?)',
                                ('work.cancelled.' + item['url'], '"2026-09-18T00:00:00Z"'))
        with self.assertRaisesRegex(ServiceError, 'cancelled'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('artifacts', item['id']), item)

    def test_claim_promotion_checks_linked_pr_owners_behind_a_work_context(self):
        owner, source = self.scheduled_native()
        from uuid import uuid4
        other = dict(owner, id=str(uuid4()), _execution_session_id=str(uuid4()), status='working')
        self.service._put('tasks', other)
        owner['_work_context_id'] = 'retained-work'
        self.service._put('tasks', owner)
        self.service._put('artifacts', dict(id='retained-work', task_id=owner['id'],
            bot_id=owner['bot_id'], kind='work', title='The original outcome', status='waiting'))
        pr = dict(id='linked-pr', task_id=other['id'], bot_id=owner['bot_id'],
                  kind='pr', title='The linked PR', status='open', pr_state='open',
                  url='https://github.com/example/project/pull/1', work_outcome_id='retained-work')
        for field in ('_review_task_id', '_activity_task_id'):
            self.service._put('artifacts', dict(pr, **{field: other['id']}))
            with self.subTest(field=field), self.assertRaisesRegex(ServiceError, 'another execution'):
                self.resume(source)
            self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_promoted_claim_resolves_and_deduplicates_retained_aliases(self):
        owner, source = self.scheduled_native()
        root = dict(id='retained-work', task_id=owner['id'], bot_id=owner['bot_id'],
                    kind='work', title='The original outcome', status='waiting')
        for item in (root, dict(root, id='hidden', _hidden=True, _work_parent_id='retained-work'),
                     dict(root, id='replaced', _work_replaced_by='hidden'),
                     dict(root, id='pr-link', kind='pr', work_outcome_id='replaced')):
            self.service._put('artifacts', item)
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['_work_context_id'], 'retained-work')
        self.assertEqual(self.service._get('artifacts', 'retained-work')['_native_outcome_id'], 't_existing')
        self.assertEqual(len(self.service._all('artifacts')), 4)

    def test_promoted_claim_resolves_a_retained_context_alias(self):
        owner, source = self.scheduled_native()
        owner['_work_context_id'] = 'alias'
        self.service._put('tasks', owner)
        root = dict(id='retained-work', task_id=owner['id'], bot_id=owner['bot_id'],
                    kind='work', title='The original outcome', status='waiting')
        self.service._put('artifacts', root)
        self.service._put('artifacts', dict(root, id='alias', _work_replaced_by=root['id']))
        self.resume(source)
        continued = self.service._get('tasks', owner['id'])
        self.assertEqual(continued['_work_context_id'], root['id'])
        self.assertEqual(self.service._get('artifacts', root['id'])['_native_outcome_id'], 't_existing')

    def test_scheduled_continuation_rejects_foreign_caller(self):
        owner, source = self.scheduled_native()
        other = self.write('/v1/bots', {'name': 'Unrelated'})
        self.service._put('tasks', dict(source, bot_id=other['id']))
        with self.assertRaisesRegex(ServiceError, 'owner or its requesting bot'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_scheduled_continuation_preserves_human_work_pause(self):
        owner, source = self.scheduled_native()
        with patch.object(self.service, '_task_work_is_paused', return_value=True), \
                self.assertRaisesRegex(ServiceError, 'paused, cancelled'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_scheduled_native_mapping_must_be_unambiguous_and_consistent(self):
        owner, source = self.scheduled_native()
        cases = (
            {'_native_outcome_id': 't_conflicting'},
            {'_kanban_claim': dict(owner['_kanban_claim'], native_id='t_conflicting')},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(owner, **changes))
                with self.assertRaisesRegex(ServiceError, 'mapping does not match'):
                    self.resume(source)
                self.assertEqual(self.service._get('tasks', owner['id']), dict(owner, **changes))

    def test_scheduled_continuation_rejects_active_native_work(self):
        owner, source = self.scheduled_native()
        native = self.service.kanban.tasks[owner['id']]
        for changes in ({'current_run_id': 'active-run'}, {'worker_pid': 1234}):
            with self.subTest(changes=changes):
                native.update(current_run_id=None, worker_pid=None)
                native.update(changes)
                with self.assertRaisesRegex(ServiceError, 'active work'):
                    self.resume(source)
        native.update(current_run_id=None, worker_pid=None)
        worker = Mock()
        worker.is_alive.return_value = True
        self.service.running[owner['id']] = worker
        with self.assertRaisesRegex(ServiceError, 'active work'):
            self.resume(source)
        self.service.running.clear()
        with patch.object(self.service, '_background_pending', return_value=True), \
                self.assertRaisesRegex(ServiceError, 'active work'):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_scheduled_continuation_fails_closed_for_cancelled_deleted_or_missing_work(self):
        owner, source = self.scheduled_native()
        exchange = self.service._get('exchanges', owner['exchange_id'])
        self.service._put('exchanges', dict(exchange, cancelled=True))
        with self.assertRaisesRegex(ServiceError, 'paused, cancelled'):
            self.resume(source)
        self.service._put('exchanges', exchange)
        bot = self.service._get('bots', owner['bot_id'])
        self.service._put('bots', dict(bot, _deleted=True))
        with self.assertRaisesRegex(ServiceError, 'paused, cancelled'):
            self.resume(source)
        self.service._put('bots', bot)
        self.service.kanban.tasks.clear()
        with self.assertRaisesRegex(ServiceError, 'existing native outcome'):
            self.resume(source)

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

    def test_reporter_wait_does_not_block_restoring_the_native_owner(self):
        owner, source, reporter, item = self.reported_work()
        self.service._put('tasks', dict(reporter, status='working'))
        self.service.emit(reporter['id'], 'work_wait', dict(task_id=source['id'], reason='Wait for the coordinator'))
        self.service._put('tasks', reporter)
        receipts = copy.deepcopy(self.service._native_task_receipts(reporter['id']))
        self.assertTrue(self.service._background_pending(reporter['id']))
        self.resume(source)
        self.assertEqual(self.service._get('artifacts', item['id'])['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', reporter['id']), reporter)
        self.assertEqual(self.service._native_task_receipts(reporter['id']), receipts)

    def test_reporter_process_or_unknown_action_still_blocks_restoration(self):
        import json
        owner, source, reporter, item = self.reported_work()
        self.service._put('tasks', dict(reporter, status='working'))
        record = self.service.emit(reporter['id'], 'native_call', dict(name='terminal', arguments={'command': 'run tests'}))
        self.service._put('tasks', reporter)
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(source)
        self.service._put('tasks', dict(reporter, status='working'))
        self.service.emit(reporter['id'], 'native_result', dict(id=record['id'],
            result=json.dumps(dict(notify_on_complete=True, session_id='running-process'))))
        self.service._put('tasks', reporter)
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(source)
        self.assertEqual(self.service._get('artifacts', item['id']), item)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_reporter_wait_for_another_tasks_decision_blocks_restoration(self):
        owner, source, reporter, item = self.reported_work()
        decision = dict(id='other-decision', task_id=source['id'], status='pending')
        self.service._put('decisions', decision)
        self.service._put('tasks', dict(reporter, status='working'))
        self.service.emit(reporter['id'], 'work_wait', dict(decision_id=decision['id'], reason='Wait for the human'))
        self.service._put('tasks', reporter)
        for changes in ({}, {'status': 'dismissed', 'inbox_undo_until': '2099-01-01T00:00:00Z'}):
            self.service._put('decisions', dict(decision, **changes))
            with self.subTest(changes=changes), self.assertRaisesRegex(ServiceError, 'another execution'):
                self.resume(source)
            self.assertEqual(self.service._get('artifacts', item['id']), item)
        self.service._put('decisions', dict(decision, status='approved'))
        self.resume(source)
        self.assertEqual(self.service._get('artifacts', item['id'])['task_id'], owner['id'])

    def test_old_chat_execution_does_not_block_current_reporter(self):
        from uuid import uuid4
        owner, source, reporter, item = self.reported_work()
        reporter.pop('_execution_session_id')
        reporter['_chat_session'] = True
        self.service._put('tasks', reporter)
        old = dict(reporter, id=str(uuid4()), status='working', _conversation_epoch='old-conversation')
        self.service._put('tasks', old)
        self.service.emit(old['id'], 'native_call', dict(name='terminal', arguments={'command': 'old process'}))
        self.resume(source)
        self.assertEqual(self.service._get('artifacts', item['id'])['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', old['id'])['status'], 'working')

    def test_queued_reporter_callback_blocks_before_its_epoch_is_assigned(self):
        from uuid import uuid4
        owner, source, reporter, item = self.reported_work()
        callback = dict(reporter, id=str(uuid4()), status='queued', _assignment_task_id=reporter['id'])
        callback.pop('_conversation_epoch', None)
        self.service._put('tasks', callback)
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(source)
        self.assertEqual(self.service._get('artifacts', item['id']), item)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

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
        self.service.kanban.tasks[owner['id']].update(status='done', input_id=owner['id'])
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
        self.service.kanban.tasks[owner['id']].update(status='done', input_id=continued['_native_turn_id'])
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
