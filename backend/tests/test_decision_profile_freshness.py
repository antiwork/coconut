import json
import unittest
from unittest.mock import patch

from coconut.profiles import ProfileError
from coconut.service import ServiceError, YieldForDecision, now
import test_decision_freshness as fixtures


setUpModule, tearDownModule = fixtures.legacy_work_sync.scope()


class DecisionProfileFreshnessTests(unittest.TestCase):
    setUp = fixtures.DecisionFreshnessTests.setUp
    tearDown = fixtures.DecisionFreshnessTests.tearDown
    start = fixtures.DecisionFreshnessTests.start
    write = fixtures.DecisionFreshnessTests.write
    question = fixtures.DecisionFreshnessTests.question
    finish = fixtures.DecisionFreshnessTests.finish

    def policy(self, name, text):
        path = self.service.profiles.home(self.bot) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def allow_refresh(self, decision):
        saved = self.service._get('decisions', decision['id'])
        saved['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', saved)

    def refresh(self):
        before = len(self.service._all('contexts'))
        self.service._queue_decision_refreshes()
        contexts = self.service._all('contexts')
        self.assertEqual(len(contexts), before + 1, 'Changed native instructions must queue a card check.')
        context = contexts[-1]
        context['status'] = 'working'
        self.service._put('contexts', context)
        return context

    def other_question(self):
        other = self.write('POST', '/v1/bots', dict(name='Other', role='Review'))
        task = self.write('POST', '/v1/bots/' + other['id'] + '/messages', dict(text='Review this separate work.', mode='queue'))
        original = self.task
        try:
            self.task = task
            return other, self.question(old=False)
        finally:
            self.task = original

    def refresh_with_profile_failure(self):
        with self.assertLogs('coconut.decision_freshness', level='ERROR'):
            try:
                return self.refresh()
            except (OSError, ProfileError) as error:
                self.fail('A profile read failure must not stop another bot: ' + str(error))

    def test_each_changed_native_prompt_file_queues_one_existing_card_check(self):
        decision = self.question(old=False)
        decision.update(risk_level='low', risk='Only disposable test files change.')
        self.service._put('decisions', decision)
        for index, name in enumerate(('SOUL.md', 'memories/USER.md', 'memories/MEMORY.md')):
            with self.subTest(name=name):
                self.policy(name, 'Current scoped merge instructions: ' + name)
                self.allow_refresh(decision)
                context = self.refresh()
                self.assertEqual(context['decision_id'], decision['id'])
                self.assertTrue(self.service._get('decisions', decision['id'])['assessment_stale'])
                self.service.refresh_decision(context['id'], dict(status='current', evidence='Checked the current native instructions.'))
                self.finish(context)
                self.allow_refresh(decision)
                self.service._queue_decision_refreshes()
                self.assertEqual(len(self.service._all('contexts')), index + 1)
                self.assertEqual(len(self.service._all('decisions')), 1)
                saved = self.service._get('decisions', decision['id'])
                self.assertEqual((saved['revision'], saved['status'], saved['decided_by']), (1, 'pending', None))

    def test_native_policy_change_during_a_check_rejects_its_stale_result(self):
        self.question()
        context = self.refresh()
        self.policy('memories/USER.md', 'Only issue #3664 requires manual merges.')
        with self.assertRaisesRegex(ServiceError, 'work changed'):
            self.service.refresh_decision(context['id'], dict(status='current', evidence='Checked the old instructions.'))

    def test_card_keeps_the_worker_revision_when_the_profile_changes_during_its_turn(self):
        self.start()
        captured = self.service.profiles.prompt_revision(self.bot)
        self.policy('SOUL.md', 'A new instruction arrives during the worker turn.')
        with self.assertRaises(YieldForDecision):
            self.service.native_event(self.task['id'], 'native_question', dict(title='Mark the draft ready?',
                options=[dict(id='ready', label='Mark ready')], _worker_profile_revision=captured))
        self.service.running.clear()
        decision = self.service._all('decisions')[-1]
        self.assertEqual(decision['_profile_revision'], captured)
        self.assertTrue(decision['assessment_stale'])
        with self.assertRaises(ServiceError) as error:
            self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='ready'))
        self.assertEqual(error.exception.status, 409)
        context = self.refresh()
        self.assertEqual(context['decision_id'], decision['id'])

    def test_a_later_worker_cannot_relabel_an_earlier_tool_intent(self):
        self.start()
        captured = self.service.profiles.prompt_revision(self.bot)
        call = self.service.native_event(self.task['id'], 'native_call', dict(name='tool_call',
            arguments={'calls': []}, _worker_profile_revision=captured))
        self.policy('SOUL.md', 'The later worker receives this different instruction.')
        current = self.service.profiles.prompt_revision(self.bot)
        with self.assertRaises(YieldForDecision):
            self.service.native_event(self.task['id'], 'native_question', dict(title='Mark the draft ready?',
                options=[dict(id='ready', label='Mark ready')], receipt_id=call['id'], _worker_profile_revision=current))
        decision = self.service._all('decisions')[-1]
        self.assertEqual(decision['_profile_revision'], captured)
        self.assertEqual(self.service._get('native_receipts', call['id'])['_worker_profile_revision'], captured)
        with self.assertRaises(ServiceError):
            self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='ready'))

    def test_an_older_intent_without_provenance_needs_a_new_check(self):
        self.start()
        call = self.service.native_event(self.task['id'], 'native_call', dict(name='tool_call', arguments={'calls': []}))
        record = self.service._get('native_receipts', call['id'])
        record.pop('_worker_profile_revision')
        self.service._put('native_receipts', record)
        current = self.service.profiles.prompt_revision(self.bot)
        with self.assertRaises(YieldForDecision):
            self.service.native_event(self.task['id'], 'native_question', dict(title='Mark the draft ready?',
                options=[dict(id='ready', label='Mark ready')], receipt_id=call['id'], _worker_profile_revision=current))
        self.service.running.clear()
        decision = self.service._all('decisions')[-1]
        self.assertIsNone(decision['_profile_revision'])
        self.assertTrue(decision['assessment_stale'])
        with self.assertRaises(ServiceError):
            self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='ready'))
        self.assertEqual(self.refresh()['decision_id'], decision['id'])

    def test_refresh_requires_its_worker_revision_even_when_the_files_change_back(self):
        self.question()
        context = self.refresh()
        home = self.service.profiles.home(self.bot)
        original = (home / 'SOUL.md').read_text()
        self.policy('SOUL.md', 'A temporary instruction used by the worker.')
        captured = self.service.profiles.prompt_revision(self.bot)
        self.policy('SOUL.md', original)
        with self.assertRaisesRegex(ServiceError, 'worker used different'):
            self.service.refresh_decision(context['id'], dict(status='current', evidence='The worker saw the temporary instruction.',
                _worker_profile_revision=captured))
        self.assertFalse(self.service._get('contexts', context['id']).get('_refresh_recorded'))

    def test_new_profile_revision_bypasses_the_recent_check_interval(self):
        decision = self.question()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='current', evidence='The previous profile is current.'))
        self.finish(context)
        self.policy('memories/USER.md', 'The native profile has a new scoped instruction.')
        followup = self.refresh()
        self.assertEqual(followup['decision_id'], decision['id'])
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_legacy_profile_marker_bypasses_the_recent_check_interval(self):
        decision = self.question()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='current', evidence='The profile is current.'))
        self.finish(context)
        saved = self.service._get('decisions', decision['id'])
        saved.pop('_profile_revision')
        self.service._put('decisions', saved)
        followup = self.refresh()
        self.assertEqual(followup['decision_id'], decision['id'])
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_failed_checks_wait_for_the_same_profile_revision(self):
        self.question(old=False)
        self.policy('memories/USER.md', 'The first new instruction.')
        context = self.refresh()
        context.update(status='failed', _failed_at=now())
        self.service._put('contexts', context)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.policy('memories/USER.md', 'A different new instruction.')
        self.refresh()
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_changed_policy_revises_the_same_card_and_rejects_the_old_choice(self):
        self.policy('memories/USER.md', 'All PRs require manual merges.')
        decision = self.question(old=False)
        decision.update(title='Mark PR #42 ready for a manual merge?',
            context='Approval only marks this draft ready.',
            options=[dict(id='ready', label='Mark ready', answer='approve', explanation=''),
                     dict(id='leave', label='Leave open', answer='approve', explanation='')])
        url, head = 'https://github.com/example/project/pull/42', 'a' * 40
        self.service._put('artifacts', dict(id='reviewed-pr', kind='pr', task_id=self.task['id'], bot_id=self.bot['id'],
            title='The reviewed change', url=url, head_sha=head, status='ready_for_review', created_at=decision['created_at']))
        decision['merge_pr_url'] = url
        self.service._put('decisions', decision)
        self.service._initialize_decision_freshness(decision)
        self.policy('memories/USER.md', 'Other PRs ask for approval to mark ready and merge the reviewed head.')
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='revise', evidence='The current native instructions scope the manual-merge hold.',
            revision=dict(title='Mark PR #42 ready and merge it?', context='Approval marks the draft ready and merges the reviewed head.',
                options=[dict(id='merge', label='Mark ready and merge'), dict(id='leave', label='Leave open')],
                recommended_option_id='merge')))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['id'], saved['revision'], saved['status']), (decision['id'], 2, 'pending'))
        self.assertEqual((saved['merge_pr_url'], saved['_merge_pr_head']), (url, head))
        self.assertIsNone(saved['decided_by'])
        self.assertEqual(len(self.service._all('decisions')), 1)
        with self.assertRaisesRegex(ServiceError, 'changed') as error:
            self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='ready'))
        self.assertEqual(error.exception.status, 409)

    def test_policy_change_does_not_expand_an_approved_ready_only_answer(self):
        decision = self.question(old=False)
        decision.update(title='Mark PR #42 ready for a manual merge?',
            context='Approval only marks the draft ready.',
            options=[dict(id='ready', label='Mark ready', answer='approve', explanation=''),
                     dict(id='leave', label='Leave open', answer='approve', explanation='')])
        self.service._put('decisions', decision)
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='ready'))
        before = json.dumps(self.service._get('decisions', decision['id']), sort_keys=True)
        self.policy('memories/USER.md', 'Other PRs use an exact approval to mark ready and merge.')
        self.service._queue_decision_refreshes()
        self.assertEqual(json.dumps(self.service._get('decisions', decision['id']), sort_keys=True), before)
        self.assertEqual(self.service._all('contexts'), [])
        self.assertEqual(self.service._get('tasks', self.task['id'])['_resume_decision'], decision['id'])

    def test_profile_change_blocks_an_old_choice_before_the_background_check(self):
        for name in ('SOUL.md', 'memories/USER.md', 'memories/MEMORY.md'):
            with self.subTest(name=name):
                decision = self.question(old=False)
                self.policy(name, 'Current scoped instructions: ' + name)
                before = json.dumps({table: self.service._all(table) for table in ('decisions', 'tasks', 'native_receipts')}, sort_keys=True)
                with self.assertRaisesRegex(ServiceError, 'instructions changed') as error:
                    self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
                self.assertEqual(error.exception.status, 409)
                self.assertEqual(json.dumps({table: self.service._all(table) for table in ('decisions', 'tasks', 'native_receipts')}, sort_keys=True), before)
                self.assertEqual(self.service._all('contexts'), [])

    def test_legacy_card_waits_for_one_fenced_check_then_accepts_its_current_choice(self):
        decision = self.question(old=False)
        decision.pop('_profile_revision')
        self.service._put('decisions', decision)
        with self.assertRaises(ServiceError) as error:
            self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        self.assertEqual(error.exception.status, 409)
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='current', evidence='The current profile still supports this choice.'))
        self.finish(context)
        self.allow_refresh(decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['revision'], saved['status'], saved['selected_option_id']), (1, 'approved', 'install'))

    def test_current_profile_accepts_the_same_ready_only_option_after_a_check(self):
        decision = self.question(old=False)
        decision.update(title='Mark PR #42 ready for a manual merge?',
            context='Approval only marks the draft ready.',
            options=[dict(id='ready', label='Mark ready', answer='approve', explanation=''),
                     dict(id='leave', label='Leave open', answer='approve', explanation='')])
        self.service._put('decisions', decision)
        self.policy('memories/USER.md', 'Only this PR retains its existing ready-only choice.')
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='current', evidence='The ready-only choice remains current.'))
        self.finish(context)
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='ready'))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['status'], saved['revision'], saved['selected_option_id']), ('approved', 1, 'ready'))
        self.assertEqual(saved['context'], 'Approval only marks the draft ready.')

    def test_a_profile_change_does_not_block_an_explicit_rejection(self):
        decision = self.question(old=False)
        self.policy('memories/USER.md', 'The profile has changed.')
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, choice='reject'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'rejected')

    def test_unreadable_profile_blocks_approval_without_mutating_the_answer(self):
        decision = self.question(old=False)
        before = json.dumps(self.service._get('decisions', decision['id']), sort_keys=True)
        with patch.object(self.service.profiles, 'prompt_revision', side_effect=PermissionError('The native instructions are unreadable.')):
            with self.assertRaisesRegex(ServiceError, 'cannot read') as error:
                self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(json.dumps(self.service._get('decisions', decision['id']), sort_keys=True), before)

    def test_profile_change_blocks_a_send_before_any_draft_continuation(self):
        self.start()
        self.service.native_event(self.task['id'], 'native_proposal', dict(key='draft', title='Send the reply?',
            draft=dict(channel='Gmail', sender='me@example.com', recipient='other@example.com',
                       subject='The review', body='The review is ready.')))
        decision = self.service._all('decisions')[-1]
        self.policy('memories/USER.md', 'The profile has changed.')
        before = json.dumps({table: self.service._all(table) for table in ('decisions', 'tasks', 'native_receipts')}, sort_keys=True)
        with self.assertRaises(ServiceError) as error:
            self.write('POST', '/v1/decisions/' + decision['id'] + '/answer',
                dict(revision=1, option_id='send', draft_hash=decision['draft_hash']))
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(json.dumps({table: self.service._all(table) for table in ('decisions', 'tasks', 'native_receipts')}, sort_keys=True), before)

    def test_native_allow_once_keeps_its_exact_action_after_a_profile_change(self):
        self.start()
        action = dict(name='terminal', arguments={'command': 'printf approved'})
        call = self.service.native_event(self.task['id'], 'native_call', action)
        with self.assertRaises(YieldForDecision):
            self.service.native_event(self.task['id'], 'native_approval', dict(receipt_id=call['id'], title='Run this exact command?'))
        decision = self.service._all('decisions')[-1]
        self.assertNotIn('_profile_revision', decision)
        self.policy('memories/USER.md', 'The model profile has changed.')
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='once'))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['status'], saved['selected_option_id']), ('approved', 'once'))
        self.assertEqual((saved['_action'], saved['_action_hash']), (action, decision['_action_hash']))

    def test_another_profiles_policy_does_not_refresh_this_card(self):
        decision = self.question(old=False)
        other = self.write('POST', '/v1/bots', dict(name='Other', role='Review'))
        path = self.service.profiles.home(other) / 'memories/USER.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Other profile instructions.')
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts'), [])
        self.assertFalse(self.service._get('decisions', decision['id']).get('assessment_stale'))

    def test_unreadable_native_instructions_cannot_confirm_the_card(self):
        self.question()
        context = self.refresh()
        with patch.object(self.service.profiles, 'prompt_revision', side_effect=PermissionError('The native instructions are unreadable.')):
            with self.assertRaises(PermissionError):
                self.service.refresh_decision(context['id'], dict(status='current', evidence='The policy cannot be verified.'))
        self.assertFalse(self.service._get('contexts', context['id']).get('_refresh_recorded'))

    def test_a_symlinked_profile_does_not_block_another_bots_card_check(self):
        decision = self.question(old=False)
        decision.update(risk_level='low', risk='Only disposable test files change.')
        self.service._put('decisions', decision)
        other, current = self.other_question()
        home = self.service.profiles.home(self.bot)
        (home / 'memories').mkdir(exist_ok=True)
        (home / 'memories/USER.md').symlink_to(home / 'SOUL.md')
        (self.service.profiles.home(other) / 'SOUL.md').write_text('Updated instructions for the other bot.')
        context = self.refresh_with_profile_failure()
        self.assertEqual(context['decision_id'], current['id'])
        blocked = self.service._get('decisions', decision['id'])
        self.assertEqual(blocked['_profile_revision'], decision['_profile_revision'])
        self.assertTrue(blocked['assessment_stale'])
        self.assertEqual(blocked['status'], 'pending')

    def test_an_unreadable_profile_does_not_block_another_bots_card_check(self):
        decision = self.question(old=False)
        other, current = self.other_question()
        (self.service.profiles.home(other) / 'SOUL.md').write_text('Updated instructions for the other bot.')
        original = self.service.profiles.prompt_revision

        def read(bot):
            if bot['id'] == self.bot['id']:
                raise PermissionError('The first profile is unreadable.')
            return original(bot)

        with patch.object(self.service.profiles, 'prompt_revision', side_effect=read):
            context = self.refresh_with_profile_failure()
            with self.assertRaisesRegex(ServiceError, 'cannot read'):
                self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        self.assertEqual(context['decision_id'], current['id'])
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def profile_failure_during_process_delivery(self, *, symlink=False):
        first = self.question()
        other, second = self.other_question()
        second['created_at'] = second['_freshness']['checked_at'] = first['created_at']
        second['assessment_stale'] = True
        self.service._put('decisions', second)
        self.service._queue_decision_refreshes()
        self.finish(self.service._all('contexts')[0])
        self.service._queue_decision_refreshes()
        contexts = self.service._all('contexts')
        self.assertEqual(len(contexts), 2)
        records = {}
        for context in contexts:
            context['status'] = 'completed'
            self.service._put('contexts', context)
            record = dict(id='process-' + context['bot_id'], task_id=context['task_id'], turn_id=context['id'],
                action=dict(name='terminal', arguments={'command': 'inspect the evidence'}), state='completed',
                created_at=now(), completed_at=now(),
                result=json.dumps(dict(session_id='session-' + context['bot_id'], notify_on_complete=True)))
            self.service._put('native_receipts', record)
            records[context['bot_id']] = record
        self.provider.process_completions = lambda bot, processes: [dict(id=process['id'], text='The evidence is ready.') for process in processes]
        # Fail the first bot in the same set order that the completion sweep uses.
        blocked_id, current_id = list({self.bot['id'], other['id']})
        original = self.service.profiles.prompt_revision
        reads = []
        if symlink:
            home = self.service.profiles.home(self.service._get('bots', blocked_id))
            (home / 'memories').mkdir(exist_ok=True)
            (home / 'memories/USER.md').symlink_to(home / 'SOUL.md')

        def read(bot):
            reads.append(bot['id'])
            if bot['id'] == blocked_id and not symlink:
                raise PermissionError('This bot profile is unreadable.')
            return original(bot)

        before = self.service._get('decisions', next(c['decision_id'] for c in contexts if c['bot_id'] == blocked_id))
        with patch.object(self.service.profiles, 'prompt_revision', side_effect=read), \
             self.assertLogs('coconut.background_processes', level='ERROR'):
            try:
                self.service._drain_process_completions()
            except (OSError, ProfileError) as error:
                self.fail('One unreadable profile must not stop process delivery: ' + str(error))
        blocked = self.service._get('native_receipts', records[blocked_id]['id'])
        current = self.service._get('native_receipts', records[current_id]['id'])
        self.assertEqual(blocked['_completion_text'], 'The evidence is ready.')
        self.assertFalse(blocked.get('_process_delivery'))
        self.assertEqual(self.service._get('decisions', before['id']), before)
        callback = self.service._get('contexts', current['_process_delivery'])
        self.assertEqual((callback['bot_id'], callback['status']), (current_id, 'queued'))
        self.assertEqual(callback['_background_completion'], ['The evidence is ready.'])
        self.assertEqual(len(self.service._all('contexts')), 3)
        self.assertEqual(reads.count(current_id), 1)
        if symlink:
            (home / 'memories/USER.md').unlink()
        self.service._drain_process_completions()
        restored = self.service._get('native_receipts', blocked['id'])
        retry = self.service._get('contexts', restored['_process_delivery'])
        self.assertEqual((retry['bot_id'], retry['status']), (blocked_id, 'queued'))
        self.assertEqual(retry['_background_completion'], ['The evidence is ready.'])
        self.assertEqual(len(self.service._all('contexts')), 4)
        self.service._drain_process_completions()
        self.assertEqual(len(self.service._all('contexts')), 4)

    def test_unreadable_profile_retains_its_process_result_and_delivers_the_next_bot(self):
        self.profile_failure_during_process_delivery()

    def test_symlinked_profile_retains_its_process_result_and_delivers_the_next_bot(self):
        self.profile_failure_during_process_delivery(symlink=True)


if __name__ == '__main__':
    unittest.main()
