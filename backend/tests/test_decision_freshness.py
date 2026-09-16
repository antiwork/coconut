import unittest
import json
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import test_automatic_work as fixtures
from coconut.hermes_worker import conversation_input
from coconut.service import ServiceError, YieldForDecision


class DecisionFreshnessTests(unittest.TestCase):
    setUp = fixtures.AutomaticWorkTests.setUp
    tearDown = fixtures.AutomaticWorkTests.tearDown
    start = fixtures.AutomaticWorkTests.start
    write = fixtures.AutomaticWorkTests.write

    def question(self, old=True):
        self.start()
        try:
            self.service.native_event(self.task['id'], 'native_question', dict(
                title='Install the test dependencies?', context='The requested fix needs local tests.',
                options=[dict(id='install', label='Install'), dict(id='leave', label='Leave')]))
        except YieldForDecision:
            pass
        self.service.running.clear()
        decision = self.service._all('decisions')[-1]
        if old:
            decision['created_at'] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
            decision['_freshness']['checked_at'] = decision['created_at']
            self.service._put('decisions', decision)
        return decision

    def refresh(self):
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        context['status'] = 'working'
        self.service._put('contexts', context)
        return context

    def finish(self, context):
        context = self.service._get('contexts', context['id'])
        context['status'] = 'completed'
        self.service._put('contexts', context)

    def test_scoped_receipts_keep_journal_order_when_delivery_is_acknowledged(self):
        decision = self.question()
        records = [dict(id='earlier', task_id=self.task['id'], state='completed', action={'name': 'terminal'}, result='{}'),
                   dict(id='later', task_id=self.task['id'], state='completed', action={'name': 'delegate_task'},
                        result=json.dumps({'status': 'dispatched', 'delegation_id': 'native-worker'}),
                        _process_delivery='consumed', _delegation_ack_pending=True)]
        for record in records:
            self.service._put('native_receipts', record)
        tasks = {t['id']: t for t in self.service._all('tasks')}
        artifacts = self.service._all('artifacts')
        before = self.service._decision_records(decision, tasks, artifacts)
        fingerprint, _ = self.service._decision_evidence(decision, tasks, before, artifacts)
        records[1]['_delegation_ack_pending'] = False
        self.service._put('native_receipts', records[1])
        after = self.service._decision_records(decision, tasks, artifacts)
        self.assertEqual([r['id'] for r in before], [r['id'] for r in after])
        self.assertEqual(fingerprint, self.service._decision_evidence(decision, tasks, after, artifacts)[0])

    def test_card_checks_read_only_related_tool_history(self):
        original = self.service._all
        def scoped(table):
            self.assertNotEqual(table, 'native_receipts', 'A card check must not read the full tool journal')
            return original(table)
        with patch.object(self.service, '_all', side_effect=scoped):
            self.question()
            context = self.refresh()
            self.service.refresh_decision(context['id'], dict(status='current', evidence='The original choice still needs an answer.'))
        self.assertTrue(self.service._get('contexts', context['id'])['_refresh_recorded'])

    def test_old_card_queues_one_silent_check_without_a_human_message(self):
        self.question()
        messages = self.service._all('messages')
        self.refresh()
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.assertEqual(self.service._all('messages'), messages)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')

    def test_fresh_card_waits_but_changed_work_queues_a_check(self):
        self.question(old=False)
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts'), [])
        task = self.service._get('tasks', self.task['id'])
        task['summary'] = 'New source evidence arrived'
        self.service._put('tasks', task)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_current_card_does_not_repeat_a_model_check_on_each_scan(self):
        decision = self.question()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='current', evidence='The verified account choice still requires the owner.'))
        self.finish(context)
        for _ in range(3):
            self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.assertEqual(self.service._get('decisions', decision['id'])['revision'], 1)

    def test_refresh_revises_same_card_and_rejects_an_old_choice(self):
        decision = self.question()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='revise', evidence='The source no longer needs installation.',
            revision=dict(title='Use the new account?', context='The old account closed.', risk='This changes the destination.',
                          options=[dict(id='new', label='New account'), dict(id='leave', label='Leave')], recommended_option_id='new')))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['revision'], saved['title'], saved['status']), (2, 'Use the new account?', 'pending'))
        self.assertIsNone(saved['decided_by'])
        with self.assertRaises(ServiceError):
            self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))

    def test_copy_revision_keeps_the_pr_link_and_records_its_head(self):
        decision = self.question()
        url = 'https://github.com/example/project/pull/42'
        self.service._put('artifacts', dict(id='linked-pr', kind='pr', task_id=self.task['id'], bot_id=self.bot['id'],
            title='Fix the export', url=url, head_sha='reviewed-head', status='ready_for_review', created_at=decision['created_at']))
        decision['merge_pr_url'] = url
        self.service._put('decisions', decision)
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='revise', evidence='The current head is ready.',
            revision=dict(title='Merge the export fix?', context='The tests pass.', options=decision['options'])))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['merge_pr_url'], saved['_merge_pr_head']), (url, 'reviewed-head'))
        self.service._revise_question(saved, self.service._get('tasks', self.task['id']),
            dict(title='Deploy this change?', options=decision['options'], merge_pr_url=None), 'New action.')
        self.assertIsNone(saved['merge_pr_url'])
        self.assertIsNone(saved['_merge_pr_head'])

    def test_recovery_revision_preserves_fixed_actions(self):
        decision = self.question()
        decision.update(_recovery_work_ids=['work'], options=[dict(id='retry', label='Retry'), dict(id='stop', label='Stop')])
        self.service._put('decisions', decision)
        context = self.refresh()
        revision = dict(title='Continue the interrupted test?', context='The tool result is available.',
                        options=[dict(id='retry', label='Retry'), dict(id='stop-work', label='Stop this work')])
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='revise', evidence='Read the tool result.', revision=revision))
        self.assertEqual(self.service._get('decisions', decision['id'])['options'], decision['options'])
        revision['options'] = decision['options']
        self.service.refresh_decision(context['id'], dict(status='revise', evidence='Read the tool result.', revision=revision))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['title'], revision['title'])
        self.assertEqual([(o['id'], o['label']) for o in saved['options']], [('retry', 'Retry'), ('stop', 'Stop')])

    def test_callback_card_includes_changes_from_a_sibling_delegate(self):
        decision = self.question()
        owner = self.service._get('tasks', self.task['id'])
        bot = self.write('POST', '/v1/bots', dict(name='Helper', role='Verify'))
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        child = self.service._store_bot_message(owner['id'], dict(target_bot_id=bot['id'], text='Verify result', require_response=True))
        owner = self.service._get('tasks', owner['id'])
        owner['status'] = 'needs_you'
        self.service._put('tasks', owner)
        callback = dict(owner, id='parent-callback', _assignment_task_id=owner['id'],
                        source_task_id='another-helper', _is_return=True,
                        created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        decision['task_id'] = callback['id']
        self.service._put('decisions', decision)
        context = self.refresh()
        helper = self.service._get('tasks', child['task_id'])
        helper['summary'] = 'New evidence contradicts the pending question.'
        self.service._put('tasks', helper)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old evidence.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_withdrawal_resumes_existing_task_without_fabricating_approval(self):
        decision = self.question()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The requested local fix already covers isolated test setup.'))
        saved = self.service._get('decisions', decision['id'])
        task = self.service._get('tasks', self.task['id'])
        self.assertEqual(saved['status'], 'resolved')
        self.assertIsNone(saved['decided_by'])
        self.assertEqual((task['status'], task['_resume_decision']), ('queued', decision['id']))
        prompt, _ = conversation_input(dict(operation='resume', task=task, decision=saved, history=[]))
        self.assertIn('No option was selected', prompt)

    def test_source_change_during_check_rejects_stale_withdrawal(self):
        decision = self.question()
        context = self.refresh()
        task = self.service._get('tasks', self.task['id'])
        task['summary'] = 'New evidence contradicts the old result'
        self.service._put('tasks', task)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old source result'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_refresh_reads_do_not_invalidate_their_own_evidence(self):
        self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='read_file', arguments={'path': 'saved-test-result'}), context_id=context['id'])
        self.service.native_event(self.task['id'], 'native_result',
            dict(id=receipt['id'], result='Verified test result'), context_id=context['id'])
        result = self.service.refresh_decision(context['id'], dict(status='current', evidence='The saved result confirms the current question.'))
        self.assertEqual(result['status'], 'recorded')

    def process_receipt(self):
        task = self.service._get('tasks', self.task['id'])
        receipt = dict(id='original-process', task_id=task['id'], turn_id=task['_native_turn_id'],
            action=dict(name='terminal', arguments={'command': 'run tests'}), state='completed',
            completed_at=task['created_at'], created_at=task['created_at'],
            result=json.dumps(dict(session_id='test-process', notify_on_complete=True)))
        self.service._put('native_receipts', receipt)
        return receipt

    def test_new_process_output_invalidates_an_older_check(self):
        decision = self.question()
        receipt = self.process_receipt()
        context = self.refresh()
        receipt['_completion_text'] = 'The tests failed.'
        self.service._put('native_receipts', receipt)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Tests still running.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_cancelling_a_refresh_clears_bot_activity(self):
        decision = self.question()
        owner = self.service._get('tasks', self.task['id'])
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        decision['inbox_kind'] = 'proposal'
        self.service._put('decisions', decision)
        context = self.refresh()
        self.service._refresh_bot(self.bot['id'])
        self.assertEqual(self.service._get('bots', self.bot['id'])['status'], 'working')
        decision = self.service._get('decisions', decision['id'])
        decision['status'] = 'rejected'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')
        self.assertEqual(self.service._get('bots', self.bot['id'])['status'], 'idle')

    def test_maintenance_does_not_replace_failed_human_reply_recovery(self):
        decision = self.question()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Use the local setup.', revision=1))
        human = self.service._all('contexts')[-1]
        human['status'] = 'failed'
        self.service._put('contexts', human)
        automatic = self.refresh()
        self.finish(automatic)
        self.service._queue_context_recoveries()
        saved = self.service._get('contexts', human['id'])
        retry = self.service._get('contexts', saved['_recovery_context_id'])
        self.assertEqual(retry['_reply_device'], human['_reply_device'])
        self.assertFalse(retry.get('_decision_refresh'))

    def test_maintenance_read_preserves_delivery_to_the_process_owner(self):
        decision = self.question()
        original = self.process_receipt()
        context = self.refresh()
        read = self.service.native_event(self.task['id'], 'native_call',
            dict(name='process', arguments={'session_id': 'test-process'}), context_id=context['id'])
        self.service.native_event(self.task['id'], 'native_result', dict(id=read['id'],
            result=json.dumps(dict(status='exited', session_id='test-process', output='Tests passed'))), context_id=context['id'])
        self.assertFalse(self.service._get('native_receipts', original['id']).get('_process_delivery'))
        self.assertTrue(self.service._background_pending(self.task['id']))
        self.assertIn('Tests passed', self.service._get('native_receipts', original['id'])['_completion_text'])
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'waiting'
        self.service._put('tasks', task)
        self.service._migrate_process_deliveries()
        self.assertFalse(self.service._get('native_receipts', original['id']).get('_process_delivery'))
        self.service.provider.process_completions = lambda bot, processes: self.fail('The saved result must not be fetched again.')
        self.service._drain_process_completions()
        resumed = self.service._get('tasks', self.task['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn('Tests passed', str(resumed['_background_completion']))

    def test_poller_preserves_an_owner_result_before_the_refresh_read_returns(self):
        self.question()
        original = self.process_receipt()
        context = self.refresh()
        self.service.native_event(self.task['id'], 'native_call',
            dict(name='process', arguments={'session_id': 'test-process'}), context_id=context['id'])
        self.provider.process_completions = lambda bot, processes: [dict(id=original['id'], consumed=True, text='Tests passed')]
        self.service._drain_process_completions()
        saved = self.service._get('native_receipts', original['id'])
        self.assertFalse(saved.get('_process_delivery'))
        self.assertEqual(saved['_completion_text'], 'Tests passed')

    def test_outstanding_refresh_process_prevents_duplicate_research(self):
        decision = self.question()
        context = self.refresh()
        record = self.process_receipt()
        record.update(id='refresh-process', turn_id=context['id'])
        self.service._put('native_receipts', record)
        context['status'] = 'failed'
        self.service._put('contexts', context)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.provider.process_completions = lambda bot, processes: [dict(id=record['id'], text='Research is complete')]
        self.service._drain_process_completions()
        self.assertEqual(len(self.service._all('contexts')), 2)
        self.assertEqual(self.service._all('contexts')[-1]['_background_completion'], ['Research is complete'])

    def test_interrupted_refresh_passes_unknown_effects_to_its_retry(self):
        decision = self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'inspect result'}), context_id=context['id'])
        context['status'] = 'failed'
        self.service._put('contexts', context)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        retry = self.service._all('contexts')[-1]
        self.assertNotEqual(retry['id'], context['id'])
        self.assertIn(receipt['id'], retry['_context_recovery'])
        self.assertEqual(retry['_recovery_unknown_tools'][0]['id'], receipt['id'])

    def test_cancelled_check_preserves_a_late_synchronous_result(self):
        decision = self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'inspect result'}), context_id=context['id'])
        context['status'] = 'cancelled'
        self.service._put('contexts', context)
        self.service.native_event(self.task['id'], 'native_result',
            dict(id=receipt['id'], result='The local dependency is installed and verified.'), context_id=context['id'])
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        retry = self.service._all('contexts')[-1]
        self.assertNotEqual(retry['id'], context['id'])
        self.assertIn('The local dependency is installed and verified.', retry['_context_recovery'])
        self.assertEqual(retry['_recovery_unknown_tools'][0]['state'], 'completed')

    def test_recovery_card_checks_receipts_from_previous_work_attempts(self):
        decision = self.question()
        record = self.process_receipt()
        previous = self.service._get('tasks', self.task['id'])
        current = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Recover this outcome', mode='queue'))
        current = self.service._get('tasks', current['id'])
        current.update(status='needs_you', _conversation_epoch=previous['_conversation_epoch'])
        self.service._put('tasks', current)
        self.service._put('artifacts', dict(id='work-history', kind='work', bot_id=self.bot['id'], task_id=current['id'],
            _work_previous_task_ids=[previous['id']], title='Verify the export', status='blocked', created_at=previous['created_at']))
        decision.update(task_id=current['id'], _recovery_work_ids=['work-history'])
        self.service._put('decisions', decision)
        context = self.refresh()
        record['_completion_text'] = 'Export verification failed.'
        self.service._put('native_receipts', record)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old result.'))

    def test_answer_interrupts_only_its_automatic_check(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        with patch.object(self.provider, 'cancel_context', create=True) as cancel:
            self.service._cancel_stale_decision_refreshes()
        cancel.assert_called_once_with(self.task['id'], context['id'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')

    def test_peer_card_reply_invalidates_the_coordinators_check(self):
        decision = self.question()
        owner = self.service._get('tasks', self.task['id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        bot = self.write('POST', '/v1/bots', dict(name='Helper', role='Verify'))
        message = self.service._store_bot_message(owner['id'], dict(target_bot_id=bot['id'], text='Verify result', require_response=True))
        owner = self.service._get('tasks', owner['id'])
        owner['status'] = 'needs_you'
        self.service._put('tasks', owner)
        peer = dict(decision, id='peer-card', task_id=message['task_id'], bot_id=bot['id'], title='Use this narrower scope?')
        self.service._put('decisions', peer)
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + peer['id'] + '/reply', dict(text='Keep the original scope.', revision=1))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The peer card replaces this question.'))

    def test_unknown_tool_does_not_strand_completed_background_research(self):
        decision = self.question()
        context = self.refresh()
        record = self.process_receipt()
        record.update(turn_id=context['id'], _completion_text='The research result is available.')
        self.service._put('native_receipts', record)
        self.service.native_event(self.task['id'], 'native_call', dict(name='read_file', arguments={'path': 'result'}), context_id=context['id'])
        context['status'] = 'failed'
        self.service._put('contexts', context)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        retry = self.service._all('contexts')[-1]
        self.assertNotEqual(retry['id'], context['id'])
        self.assertIn('The research result is available.', retry['_context_recovery'])
        self.assertIn('Reuse their processes', retry['_context_recovery'])

    def test_maintenance_process_is_not_a_task_dependency(self):
        decision = self.question()
        context = self.refresh()
        record = self.process_receipt()
        record['turn_id'] = context['id']
        self.service._put('native_receipts', record)
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        owner = self.service._get('tasks', self.task['id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        self.assertEqual(self.service.wait_for_task(owner['id'], {})['processes'], [])
        with self.assertRaises(ServiceError):
            self.service.wait_for_task(owner['id'], dict(process_id='test-process', reason='Wait for research'))

    def test_partial_completion_does_not_forget_a_second_research_process(self):
        decision = self.question()
        context = self.refresh()
        first = self.process_receipt()
        first['turn_id'] = context['id']
        self.service._put('native_receipts', first)
        second = dict(first, id='second-research', result=json.dumps(dict(session_id='second-process', notify_on_complete=True)))
        self.service._put('native_receipts', second)
        context['status'] = 'failed'
        self.service._put('contexts', context)
        self.service._resume_process_context(context['id'], [(first, 'The first result is ready.')])
        continuation = self.service._all('contexts')[-1]
        continuation['status'] = 'failed'
        self.service._put('contexts', continuation)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_human_reply_interrupts_the_check_before_changing_the_card(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Change the scope.', revision=1))
        with patch.object(self.provider, 'cancel_context', create=True) as cancel:
            self.service._cancel_stale_decision_refreshes()
        cancel.assert_called_once_with(self.task['id'], context['id'])
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')
        self.assertEqual(self.service._get('decisions', decision['id'])['revision'], 1)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_result_for_a_cancelled_callback_reaches_the_replacement_check(self):
        decision = self.question()
        context = self.refresh()
        record = self.process_receipt()
        record['turn_id'] = context['id']
        self.service._put('native_receipts', record)
        context['status'] = 'failed'
        self.service._put('contexts', context)
        self.service._resume_process_context(context['id'], [(record, 'The dependency is already installed.')])
        callback = self.service._all('contexts')[-1]
        callback['status'] = 'cancelled'
        self.service._put('contexts', callback)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        retry = self.service._all('contexts')[-1]
        self.assertNotEqual(retry['id'], callback['id'])
        self.assertIn('The dependency is already installed.', retry['_context_recovery'])

    def test_normal_chat_cancels_and_defers_automatic_checks(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Use a different scope.', mode='queue'))
        with patch.object(self.provider, 'cancel_context', create=True) as cancel:
            self.service._cancel_stale_decision_refreshes()
        cancel.assert_called_once_with(self.task['id'], context['id'])
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_cancelled_check_keeps_a_result_for_an_existing_tool_call(self):
        self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'inspect source'}), context_id=context['id'])
        context['status'] = 'cancelled'
        self.service._put('contexts', context)
        result = json.dumps(dict(session_id='still-running', notify_on_complete=True))
        self.service.native_event(self.task['id'], 'native_result',
            dict(id=receipt['id'], result=result), context_id=context['id'])
        saved = self.service._get('native_receipts', receipt['id'])
        self.assertEqual((saved['state'], saved['result']), ('completed', result))
        with self.assertRaises(ServiceError):
            self.service.native_event(self.task['id'], 'native_call',
                dict(name='terminal', arguments={'command': 'new action'}), context_id=context['id'])
        other = self.process_receipt()
        with self.assertRaises(ServiceError):
            self.service.native_event(self.task['id'], 'native_result',
                dict(id=other['id'], result=other['result']), context_id=context['id'])

    def test_second_research_result_invalidates_a_running_callback(self):
        decision = self.question()
        context = self.refresh()
        first = self.process_receipt()
        first['turn_id'] = context['id']
        self.service._put('native_receipts', first)
        second = dict(first, id='second-research', result=json.dumps(dict(session_id='second-process', notify_on_complete=True)))
        self.service._put('native_receipts', second)
        self.finish(context)
        self.service._resume_process_context(context['id'], [(first, 'The first result is ready.')])
        callback = self.service._all('contexts')[-1]
        callback['status'] = 'working'
        self.service._put('contexts', callback)
        # The callback starts with the delivered result in its current evidence.
        self.service.refresh_decision(callback['id'], dict(status='current', evidence='The second process still runs.'))
        second['_completion_text'] = 'The second result changes the required decision.'
        self.service._put('native_receipts', second)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(callback['id'], dict(status='withdraw', evidence='Old first result.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_working_human_chat_keeps_priority_until_its_turn_finishes(self):
        decision = self.question()
        context = self.refresh()
        reply = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Change this scope.', mode='queue'))
        task = self.service._get('tasks', reply['id'])
        task.update(status='working', _turn_preference_source_ids=task['_preference_source_ids'])
        self.service._put('tasks', task)
        # Update the input snapshot to isolate priority from the separate new-message guard.
        owner = self.service._get('tasks', self.task['id'])
        fingerprint, _ = self.service._decision_evidence(decision, {t['id']: t for t in self.service._all('tasks')},
            self.service._all('native_receipts'), self.service._all('artifacts'))
        context.update(_evidence_fingerprint=fingerprint, _human_input_ids=[m['id'] for m in self.service._decision_human_messages(owner)])
        self.service._put('contexts', context)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Premature result.'))
        self.finish(context)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        task['status'] = 'completed'
        self.service._put('tasks', task)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_human_reply_research_keeps_priority_after_its_turn_ends(self):
        decision = self.question()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Check the source before changing this.', revision=1))
        human = self.service._all('contexts')[-1]
        human['status'] = 'working'
        self.service._put('contexts', human)
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'inspect source'}), context_id=human['id'])
        self.service.native_event(self.task['id'], 'native_result', dict(id=receipt['id'],
            result=json.dumps(dict(session_id='human-research', notify_on_complete=True))), context_id=human['id'])
        self.finish(human)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.provider.process_completions = lambda bot, processes: [dict(id=receipt['id'], text='The source confirms a change.')]
        self.service._drain_process_completions()
        self.assertEqual(len(self.service._all('contexts')), 2)
        self.assertFalse(self.service._all('contexts')[-1].get('_decision_refresh'))

    def test_busy_owner_does_not_block_another_cards_refresh(self):
        import threading
        decision = self.question()
        task = self.service._get('tasks', self.task['id'])
        task.update(status='working', _turn_preference_source_ids=[])
        self.service._put('tasks', task)
        decision['inbox_kind'] = 'proposal'
        self.service._put('decisions', decision)
        bot = self.write('POST', '/v1/bots', dict(name='Other bot', role='Independent work'))
        other = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Other work', mode='queue'))
        other = self.service._get('tasks', other['id'])
        other['status'] = 'needs_you'
        self.service._put('tasks', other)
        peer = dict(decision, id='other-card', bot_id=bot['id'], task_id=other['id'])
        self.service._put('decisions', peer)
        self.service.running[task['id']] = threading.current_thread()
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts')[-1]['decision_id'], peer['id'])
        self.service.running[other['id']] = threading.current_thread()
        self.service._cancel_stale_decision_refreshes()
        self.assertEqual(self.service._all('contexts')[-1]['status'], 'cancelled')

    def test_answered_required_card_preempts_an_optional_card_check(self):
        decision = self.question()
        proposal = dict(decision, id='optional-card', inbox_kind='proposal')
        self.service._put('decisions', proposal)
        decision['_freshness']['queued_at'] = datetime.now(timezone.utc).isoformat()
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        self.assertEqual(context['decision_id'], proposal['id'])
        context['status'] = 'working'
        self.service._put('contexts', context)
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        self.service._cancel_stale_decision_refreshes()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')

    def test_waiting_human_chat_research_defers_card_checks(self):
        self.question()
        reply = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Inspect the source first.', mode='queue'))
        task = self.service._get('tasks', reply['id'])
        task.update(status='working', _turn_preference_source_ids=task['_preference_source_ids'])
        self.service._put('tasks', task)
        receipt = self.service.native_event(task['id'], 'native_call', dict(name='terminal', arguments={'command': 'inspect source'}))
        self.service.native_event(task['id'], 'native_result', dict(id=receipt['id'],
            result=json.dumps(dict(session_id='human-research', notify_on_complete=True))))
        task.update(status='waiting', _preference_source_ids=[], _turn_preference_source_ids=[])
        self.service._put('tasks', task)
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts'), [])

    def test_retry_keeps_original_research_after_recording_a_result(self):
        decision = self.question()
        context = self.refresh()
        process = self.process_receipt()
        process['turn_id'] = context['id']
        self.service._put('native_receipts', process)
        self.service.native_event(self.task['id'], 'native_call', dict(name='read_file', arguments={'path': 'source'}), context_id=context['id'])
        context['status'] = 'failed'
        self.service._put('contexts', context)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        retry = self.refresh()
        self.service.refresh_decision(retry['id'], dict(status='current', evidence='The required decision remains unchanged.'))
        self.finish(retry)
        process['_completion_text'] = 'The original research found a changed source.'
        self.service._put('native_receipts', process)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        replacement = self.service._all('contexts')[-1]
        self.assertNotEqual(replacement['id'], retry['id'])
        self.assertIn('The original research found a changed source.', replacement['_context_recovery'])

    def test_approved_continuation_in_another_session_preempts_the_check(self):
        decision = self.question()
        context = self.refresh()
        owner = self.service._get('tasks', self.task['id'])
        continuation = dict(owner, id='approved-proposal', status='queued', _resume_decision=decision['id'],
                            _execution_session_id='independent-session', _preference_source_ids=[], _turn_preference_source_ids=[])
        self.service._put('tasks', continuation)
        self.service._cancel_stale_decision_refreshes()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')

    def test_second_completion_carries_an_unknown_effect_from_its_sibling(self):
        decision = self.question()
        context = self.refresh()
        first = self.process_receipt()
        first['turn_id'] = context['id']
        self.service._put('native_receipts', first)
        second = dict(first, id='second-research', result=json.dumps(dict(session_id='second-process', notify_on_complete=True)))
        self.service._put('native_receipts', second)
        self.finish(context)
        self.service._resume_process_context(context['id'], [(first, 'First research result.')])
        sibling = self.service._all('contexts')[-1]
        sibling['status'] = 'working'
        self.service._put('contexts', sibling)
        unknown = self.service.native_event(self.task['id'], 'native_call', dict(name='terminal', arguments={'command': 'inspect result'}), context_id=sibling['id'])
        sibling['status'] = 'failed'
        self.service._put('contexts', sibling)
        self.service._resume_process_context(context['id'], [(second, 'Second research result.')])
        callback = self.service._all('contexts')[-1]
        self.assertIn(unknown['id'], callback['_context_recovery'])
        callback['status'] = 'working'
        self.service._put('contexts', callback)
        self.service.refresh_decision(callback['id'], dict(status='current', evidence='The question still applies.'))
        self.finish(callback)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness'].update(checked_at='2000-01-01T00:00:00Z', queued_at='2000-01-01T00:00:00Z')
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertIn(unknown['id'], self.service._all('contexts')[-1]['_context_recovery'])

    def test_dispatch_runs_only_one_automatic_callback_at_a_time(self):
        import threading
        decision = self.question()
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        bot = self.write('POST', '/v1/bots', dict(name='Other bot', role='Independent work'))
        task = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Other work', mode='queue'))
        task = self.service._get('tasks', task['id'])
        task['status'] = 'needs_you'
        self.service._put('tasks', task)
        peer = dict(decision, id='peer-card', bot_id=bot['id'], task_id=task['id'])
        self.service._put('decisions', peer)
        callback = dict(context, id='peer-callback', bot_id=bot['id'], task_id=task['id'], decision_id=peer['id'],
                        _conversation_epoch=task.get('_conversation_epoch'),
                        _human_input_ids=[m['id'] for m in self.service._decision_human_messages(task)])
        self.service._put('contexts', callback)
        started = []
        def start(identity):
            started.append(identity)
            self.service.context_threads[identity] = threading.current_thread()
        try:
            with patch.object(self.service, '_start_context', start):
                self.service._dispatch_pending()
            self.assertEqual(len(started), 1)
        finally:
            self.service.context_threads.clear()

    def test_archived_reply_does_not_block_another_bots_card_check(self):
        decision = self.question()
        bot = self.write('POST', '/v1/bots', dict(name='Archived bot', role='Old work'))
        task = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Old request', mode='queue'))
        task = self.service._get('tasks', task['id'])
        task['status'] = 'needs_you'
        self.service._put('tasks', task)
        peer = dict(decision, id='old-card', bot_id=bot['id'], task_id=task['id'])
        self.service._put('decisions', peer)
        self.write('POST', '/v1/decisions/' + peer['id'] + '/reply', dict(text='Check this.', revision=1))
        bot = self.service._get('bots', bot['id'])
        bot['archived'] = True
        self.service._put('bots', bot)
        context = self.refresh()
        self.assertTrue(context.get('_decision_refresh'))
        self.assertEqual(context['decision_id'], decision['id'])

    def test_evidence_change_before_the_first_scan_triggers_a_check(self):
        self.question(old=False)
        owner = self.service._get('tasks', self.task['id'])
        owner['summary'] = 'The source changed after the card was created.'
        self.service._put('tasks', owner)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_same_source_id_does_not_expose_an_unrelated_bots_work(self):
        self.question()
        owner = self.service._get('tasks', self.task['id'])
        owner['_source_assignment'] = {'id': 'issue-2'}
        self.service._put('tasks', owner)
        bot = self.write('POST', '/v1/bots', dict(name='Private', role='Private work'))
        other = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Private task', mode='queue'))
        other = self.service._get('tasks', other['id'])
        other.update(status='working', _source_assignment={'id': 'issue-2'})
        self.service._put('tasks', other)
        self.service._register_work(other['id'], dict(key='private-work', title='Private record for another source',
            activity='Private evidence', status='working'))
        context = self.refresh()
        self.assertNotIn('Private record for another source', context['question'])
        self.assertNotIn('Private evidence', context['question'])

    def test_changed_peer_question_invalidates_duplicate_withdrawal(self):
        decision = self.question()
        peer = dict(decision, id='peer', title='Use the local database?')
        self.service._put('decisions', peer)
        context = self.refresh()
        peer.update(revision=2, context='Only inspect the schema. Do not change it.')
        self.service._put('decisions', peer)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The other card covers this.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_maintenance_process_does_not_hold_an_answered_assignment(self):
        decision = self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'read evidence'}), context_id=context['id'])
        self.service.native_event(self.task['id'], 'native_result', dict(id=receipt['id'],
            result=json.dumps(dict(session_id='maintenance-process', notify_on_complete=True))), context_id=context['id'])
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        self.assertFalse(self.service._background_pending(self.task['id']))
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'completed')

    def test_interrupted_maintenance_stays_silent_and_keeps_its_receipt(self):
        self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='read_file', arguments={'path': 'evidence'}), context_id=context['id'])
        before = self.service._all('messages')
        self.assertTrue(self.service._block_uncertain_context(context))
        self.assertEqual(self.service._all('messages'), before)
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'executing')

    def test_automatic_work_maintenance_does_not_invalidate_its_card_check(self):
        self.question()
        self.service._sync_automatic_work()
        context = self.refresh()
        self.service.native_event(self.task['id'], 'native_call',
            dict(name='read_file', arguments={'path': 'evidence'}), context_id=context['id'])
        self.service._sync_automatic_work()
        rows = [a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']
        self.assertEqual(rows[0]['work_state'], 'needs_decision')
        result = self.service.refresh_decision(context['id'], dict(status='current', evidence='The question still applies.'))
        self.assertEqual(result['status'], 'recorded')

    def test_instruction_addressed_from_another_chat_invalidates_the_check(self):
        decision = self.question()
        context = self.refresh()
        other = self.write('POST', '/v1/bots', dict(name='Coordinator', role='Coordinate'))
        self.write('POST', '/v1/bots/' + other['id'] + '/messages',
            dict(text='Do not install dependencies.', recipient_bot_ids=[self.bot['id']], mode='queue'))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old result'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_next_check_includes_new_human_instructions(self):
        self.question()
        reply = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Do not install dependencies.', mode='auto'))
        task = self.service._get('tasks', reply['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        context = self.refresh()
        self.assertIn('Do not install dependencies.', context['question'])

    def test_withdrawn_recovery_preserves_unknown_effects_in_a_new_turn(self):
        decision = self.question()
        task = self.service._get('tasks', self.task['id'])
        task['_self_recovery'] = dict(attempt=2, unknown_tools=[])
        previous_turn = task['_native_turn_id']
        self.service._put('tasks', task)
        self.service._put('native_receipts', dict(id='unknown', task_id=task['id'], turn_id=previous_turn,
            action={'name': 'terminal'}, state='executing', created_at=task['created_at']))
        decision['_recovery_work_ids'] = ['work']
        self.service._put('decisions', decision)
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The result can be verified within the existing request.'))
        saved = self.service._get('tasks', task['id'])
        self.assertNotEqual(saved['_native_turn_id'], previous_turn)
        self.assertFalse(self.service._has_uncertain_receipt(task['id'], saved['_native_turn_id']))
        self.assertEqual(saved['_self_recovery']['unknown_tools'][0]['id'], 'unknown')
        self.assertEqual(self.service._get('native_receipts', 'unknown')['state'], 'executing')

    def test_new_chat_instruction_invalidates_an_automatic_check(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Do not install the dependencies.', mode='auto'))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old result'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_automatic_check_does_not_disable_human_replies(self):
        decision = self.question()
        self.refresh()
        shown = next(d for d in self.service.snapshot()['decisions'] if d['id'] == decision['id'])
        self.assertFalse(shown['reply_pending'])
        self.assertFalse(self.service.card(decision['id'])['decision']['reply_pending'])
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Keep the old setup.', revision=1))
        self.assertTrue(self.service.card(decision['id'])['decision']['reply_pending'])

    def test_human_reply_invalidates_an_automatic_check(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Use the existing local services.', revision=1))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old result'))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['status'], saved['revision']), ('pending', 1))

    def test_human_answer_wins_over_late_check(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='No longer required'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'approved')

    def test_check_cannot_approve_and_cannot_refresh_native_tool_approval(self):
        decision = self.question()
        context = self.refresh()
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='approve', evidence='This looks safe'))
        decision['action_type'] = 'native_approval'
        self.service._put('decisions', decision)
        self.finish(context)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_automatic_failure_obeys_backoff_without_chat_spam(self):
        self.question()
        context = self.refresh()
        context['status'] = 'failed'
        self.service._put('contexts', context)
        self.service._queue_context_recoveries()
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_refresh_prompt_excludes_unrelated_private_cards(self):
        self.question()
        bot = self.write('POST', '/v1/bots', dict(name='Private bot', role='Private'))
        task = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Private request', mode='queue'))
        stored = self.service._get('tasks', task['id'])
        stored['status'] = 'working'
        self.service._put('tasks', stored)
        try:
            self.service.native_event(task['id'], 'native_question', dict(title='Private account secret',
                options=[dict(id='first', label='First'), dict(id='second', label='Second')]))
        except YieldForDecision:
            pass
        context = self.refresh()
        self.assertNotIn('Private account secret', context['question'])

    def test_optional_proposal_does_not_block_required_question_withdrawal(self):
        decision = self.question()
        proposal = dict(decision, id='optional-proposal', inbox_kind='proposal', title='Optional next step')
        self.service._put('decisions', proposal)
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Local setup is already covered.'))
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')
        self.assertEqual(self.service._get('decisions', proposal['id'])['status'], 'pending')

    def test_completed_routine_card_uses_an_independent_context(self):
        decision = self.question()
        task = self.service._get('tasks', self.task['id'])
        job = dict(id='finished-routine', fire_claim={'by': 'old-owner'})
        task.update(status='completed', _native_job=job, _native_job_finished=True)
        self.service._put('tasks', task)
        decision['inbox_kind'] = 'proposal'
        self.service._put('decisions', decision)
        def answer(task, bot, history, question, emit):
            if task.get('_native_job'):
                emit('failed', {'summary': 'This routine no longer owns its native execution claim.'})
                return
            emit('decision_response', dict(status='current', evidence='The pending question still applies.'))
        self.provider.answer_context = answer
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'completed')
        self.assertTrue(self.service._get('contexts', context['id'])['_refresh_recorded'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['_native_job'], job)

    def test_human_reply_to_a_completed_routine_uses_its_own_context(self):
        decision = self.question()
        task = self.service._get('tasks', self.task['id'])
        job = dict(id='finished-routine', fire_claim={'by': 'old-owner'})
        task.update(status='completed', _native_job=job, _native_job_finished=True)
        self.service._put('tasks', task)
        decision['inbox_kind'] = 'proposal'
        self.service._put('decisions', decision)
        def answer(task, bot, history, question, emit):
            if task.get('_native_job'):
                emit('failed', {'summary': 'This routine no longer owns its native execution claim.'})
                return
            receipt = emit('native_call', dict(name='read_file', arguments={'path': 'saved-result'}))
            emit('native_result', dict(id=receipt['id'], result='Verified result'))
            emit('completed', {'summary': 'The saved result confirms the stated risk.'})
        self.provider.answer_context = answer
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Explain the risk.', revision=1))
        context = self.service._all('contexts')[-1]
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'completed')
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.assertEqual(self.service._get('tasks', self.task['id'])['_native_job'], job)

    def test_real_context_worker_records_refresh_and_stays_silent(self):
        decision = self.question()
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        messages = self.service._all('messages')
        def answer(task, bot, history, question, emit):
            self.assertTrue(task['_decision_refresh'])
            self.assertNotIn('Do not change the pending proposal', conversation_input(dict(
                operation='context', task=task, history=history, question=question))[0])
            emit('decision_response', dict(status='current', evidence='Verified source; the account choice remains open.'))
            emit('completed', dict(summary='[SILENT]'))
        self.provider.answer_context = answer
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'completed')
        self.assertIn('checked_at', self.service._get('decisions', decision['id'])['_freshness'])
        self.assertEqual(self.service._all('messages'), messages)


import test_provider as provider_fixtures


class RefreshCancellationTests(unittest.TestCase):
    setUp = provider_fixtures.ProviderTests.setUp
    tearDown = provider_fixtures.ProviderTests.tearDown
    fake_hermes = provider_fixtures.ProviderTests.fake_hermes
    wait_for_path = provider_fixtures.ProviderTests.wait_for_path

    def test_native_context_cancellation_preserves_the_task_and_runtime(self):
        import os
        import threading
        started = self.root / 'started'
        provider = self.fake_hermes(
            "from pathlib import Path\nimport time\n"
            "if 'pendingcheck' in prompt:\n"
            f"    Path({str(started)!r}).touch()\n"
            "    while not getattr(self, 'interrupted', False): time.sleep(0.01)\n"
            "return {'completed': True, 'final_response': 'Done.'}\n")
        source = provider.hermes_root / 'run_agent.py'
        source.write_text(source.read_text().replace('class AIAgent:', 'class AIAgent:\n    def hard_interrupt(self, reason): self.interrupted = True'))
        context = dict(self.task, _context_id='freshness-check')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            thread = threading.Thread(target=lambda: provider.answer_context(context, self.bot, [], 'pendingcheck', lambda *_: None))
            thread.start()
            try:
                self.wait_for_path(started)
                runtime = provider._runtimes[self.bot['id']]
                provider.cancel_context(self.task['id'], context['_context_id'])
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(runtime.poll())
                events = []
                provider.start_task(self.task, self.bot, [], lambda kind, data: events.append(kind))
                self.assertIn('completed', events)
            finally:
                if thread.is_alive():
                    provider.cancel_task(self.task['id'])
                    thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
