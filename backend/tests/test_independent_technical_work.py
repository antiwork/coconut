"""Independent technical work continues one execution while its exact decision stays pending."""
import copy
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut.native_outcomes import CONTINUE_SCHEMA
from coconut.service import ServiceError
from coconut.technical_lane import held_action, instructions
import test_native_claim_continuation as native_claims
import test_native_work_continuation as continuation
import test_provider as provider_fixtures
from test_work_progress import progress

SCOPE = ['diagnosis', 'tests', 'local_fixes']
RESUMED_RESULT = 'Marked ready after the checks.'
URL = 'https://github.com/antiwork/gumroad/pull/8537'


class IndependentTechnicalWorkTests(unittest.TestCase):
    # Borrow the fixture methods only. A module-level TestCase alias would run its tests here too.
    setUp = continuation.NativeWorkContinuationTests.setUp
    tearDown = continuation.NativeWorkContinuationTests.tearDown
    write = continuation.NativeWorkContinuationTests.write
    prepare = continuation.NativeWorkContinuationTests.prepare
    snapshot_result = continuation.NativeWorkContinuationTests.snapshot_result
    scheduled_native = continuation.NativeWorkContinuationTests.scheduled_native
    review_native = continuation.NativeWorkContinuationTests.review_native

    def lane_native(self):
        owner, source = self.review_native()
        self.service.kanban.tasks[owner['id']]['title'] = owner['title']
        self.service.kanban.observed_at = None  # Work projection reads the adapter's last snapshot time.
        owner.update(status='needs_you', summary='Accept the views and mark the PR ready after checks?',
                     _resume_decision='scope')
        self.service._put('tasks', owner)
        self.approved = dict(id='scope', task_id=owner['id'], bot_id=owner['bot_id'], status='approved',
            action_type='question', inbox_kind='decision', revision=1, title='Make #8537 a complete subscription list?',
            context='The list misses gifted memberships.', sources=[dict(kind='issue', title='#8537', url=URL)],
            options=[dict(id='all', label='Build the complete list', answer='approve', explanation='Includes gifts.'),
                     dict(id='library', label='Keep the Library-only filter', answer='approve', explanation='')],
            selected_option_id='all', selected_option_label='Build the complete list',
            decided_by='person-device', decided_at='2026-10-20T05:32:58Z', _native=True,
            _answers=[dict(choice='approve', option_id='all', decided_by='person-device')])
        self.gate = dict(id='gate', task_id=owner['id'], bot_id=owner['bot_id'], status='pending',
            action_type='question', inbox_kind='decision', revision=2, title=owner['summary'],
            options=[dict(id='accept', label='Accept the views', answer='approve', explanation=''),
                     dict(id='hold', label='Keep the PR in draft', answer='reject', explanation='')],
            decided_by=None, decided_at=None, selected_option_id=None, merge_pr_url=None, _native=True,
            _answers=[dict(choice='revise', correction='Findings changed.', decided_by=None)])
        # Another execution keeps its own human refusal and interrupted tool. The lane must not touch either.
        unrelated = dict(copy.deepcopy(source), id='unrelated', status='failed', _execution_session_id='unrelated')
        self.unrelated_rejected = dict(id='unrelated-rejected', task_id='unrelated', bot_id=source['bot_id'],
            status='rejected', action_type='question', revision=2, decided_by='person-device', _answers=[])
        self.interrupted = dict(id='interrupted', task_id='unrelated', turn_id='old-turn', state='executing',
            action={'name': 'terminal', 'arguments': {'command': 'bin/rspec'}}, action_hash='old', created_at='2026-10-19')
        for table, record in (('decisions', self.approved), ('decisions', self.gate), ('tasks', unrelated),
                              ('decisions', self.unrelated_rejected), ('native_receipts', self.interrupted)):
            self.service._put(table, record)
        return self.service._get('tasks', owner['id']), source

    def lane(self, source, work=None, **changes):
        lane = dict(dict(decision_id='gate', decision_revision=2, scope=SCOPE, authorization_decision_id='scope'), **(work or {}))
        if 'local_fixes' not in lane['scope'] and 'authorization_decision_id' not in (work or {}):
            lane.pop('authorization_decision_id')
        payload = dict(task_id='t_existing', message='Fix the CI findings locally. Keep the visual decision pending.',
                       independent_technical_work=lane)
        return self.service.emit(source['id'], 'work_continue', dict(payload, **changes))

    def assert_untouched(self, owner):
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        self.assertEqual(self.service._get('decisions', 'scope'), self.approved)
        self.assert_unrelated_untouched()

    def assert_unrelated_untouched(self):
        self.assertEqual(self.service._get('decisions', 'unrelated-rejected'), self.unrelated_rejected)
        self.assertEqual(self.service._get('native_receipts', 'interrupted'), self.interrupted)

    def refused(self, owner, source, pattern, work=None):
        with self.assertRaisesRegex(ServiceError, pattern):
            self.lane(source, work)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def admitted_turn(self):
        owner, source = self.lane_native()
        self.lane(source)
        task = self.service._get('tasks', owner['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        return task, source

    def test_lane_continues_the_exact_owner_and_leaves_the_decision_pending(self):
        owner, source = self.lane_native()
        snapshot = copy.deepcopy(self.service.kanban.tasks)
        receipt = self.lane(source)
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        self.assertEqual(self.lane(source), receipt)
        self.assertEqual(self.service._get('tasks', owner['id']), queued)
        self.assertEqual(queued['status'], 'queued')
        for key in ('id', 'bot_id', 'session_id', '_execution_session_id', 'source_bot_id', 'source_task_id',
                    '_reply_to_task_id', 'thread_id', 'conversation_bot_id', 'exchange_id', '_kanban_claim'):
            self.assertEqual(queued.get(key), owner.get(key), key)
        self.assertEqual(queued['_native_outcome_id'], 't_existing')
        self.assertEqual({k: queued['_technical_lane'][k] for k in ('decision_id', 'decision_revision', 'scope', 'native_task_id')},
                         dict(decision_id='gate', decision_revision=2, scope=SCOPE, native_task_id='t_existing'))
        self.assertEqual(queued['_technical_lane']['requester_task_id'], source['id'])
        # The fix scope is the person's recorded choice, read from the decision, not the requester's words.
        self.assertEqual(queued['_technical_lane']['authorization'], dict(decision_id='scope',
            title=self.approved['title'], context=self.approved['context'], selected_option_id='all',
            selected_option_label='Build the complete list', selected_option_explanation='Includes gifts.',
            sources=self.approved['sources'], decided_at=self.approved['decided_at']))
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        self.assertEqual(self.service._get('decisions', 'scope'), self.approved)
        self.assert_unrelated_untouched()
        self.assertEqual(self.service.kanban.tasks, snapshot)
        self.assertNotIn('_return_pending', queued)
        # The queued owner returns to native ready through the normal row; no second owner appears.
        rows = [row for row in self.service._kanban_rows() if row['execution_id'] == owner['_execution_session_id']]
        self.assertEqual([(row['id'], row['state']) for row in rows], [(owner['id'], 'ready')])

    def test_schema_documents_an_optional_structured_opt_in(self):
        parameters = CONTINUE_SCHEMA['parameters']
        self.assertEqual(parameters['required'], ['task_id', 'message'])
        lane = parameters['properties']['independent_technical_work']
        self.assertFalse(lane['additionalProperties'])
        self.assertEqual(lane['required'], ['decision_id', 'decision_revision', 'scope'])
        self.assertEqual(lane['properties']['scope']['items']['enum'], SCOPE)

    def test_ordinary_continuation_still_waits_for_the_decision(self):
        owner, source = self.lane_native()
        with self.assertRaisesRegex(ServiceError, 'Wait for this task'):
            self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing', message='Continue.'))
        self.assert_untouched(owner)
        completed = dict(owner, status='completed')
        self.service._put('tasks', completed)
        with self.assertRaisesRegex(ServiceError, 'decision must be answered'):
            self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing', message='Continue.'))
        # The lane does not relax a completed owner's pending decision either.
        self.refused(completed, source, 'waits on its decision')

    def test_lane_refuses_malformed_or_broader_input(self):
        owner, source = self.lane_native()
        for work in (dict(scope=[]), dict(scope=['publish']), dict(scope=['tests', 'tests']), dict(decision_revision='2'),
                     dict(decision_revision=True), dict(decision_id=''), dict(permissions=['merge'])):
            with self.subTest(work=work):
                self.refused(owner, source, 'exact pending decision ID', work)
        with self.assertRaisesRegex(ServiceError, 'exact pending decision ID'):
            self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing', message='Continue.',
                                                                  independent_technical_work='gate'))
        self.assert_untouched(owner)

    def test_lane_refuses_a_stale_or_different_decision(self):
        owner, source = self.lane_native()
        for work in (dict(decision_id='scope'), dict(decision_id='unrelated-rejected'), dict(decision_revision=1),
                     dict(decision_revision=3)):
            with self.subTest(work=work):
                self.refused(owner, source, 'pending decision changed', work)
        for extra in (dict(id='second', status='pending', action_type='question', inbox_kind='decision', revision=1),
                      dict(id='proposal', status='pending', action_type='question', inbox_kind='proposal', revision=1)):
            with self.subTest(extra=extra['id']):
                self.service._put('decisions', dict(extra, task_id=owner['id'], bot_id=owner['bot_id']))
                self.refused(owner, source, 'pending decision changed')
                self.service.db.execute('DELETE FROM decisions WHERE id=?', (extra['id'],))
        answered = dict(self.gate, status='approved', decided_by='person-device', selected_option_id='accept')
        self.service._put('decisions', answered)
        self.refused(owner, source, 'pending decision changed')

    def test_lane_refuses_tool_merge_draft_and_answered_cards(self):
        owner, source = self.lane_native()
        for change in (dict(action_type='native_approval'), dict(action_type='draft_message', draft={'body': 'Hi'}),
                       dict(merge_pr_url='https://github.com/antiwork/gumroad/pull/8541'), dict(_receipt_id='call'),
                       dict(inbox_kind='proposal'), dict(_source_recheck=True),
                       dict(_answers=[dict(choice='reply', decided_by='person-device')])):
            with self.subTest(change=sorted(change)):
                self.service._put('decisions', dict(self.gate, **change))
                with self.assertRaises(ServiceError):
                    self.lane(source)
                self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.service._put('decisions', self.gate)
        self.assert_untouched(owner)

    def test_lane_refuses_human_refusals_and_explicit_holds(self):
        owner, source = self.lane_native()
        refusal = dict(id='refusal', task_id=owner['id'], bot_id=owner['bot_id'], status='rejected',
                       action_type='native_approval', decided_by='person-device', revision=1)
        self.service._put('decisions', refusal)
        self.refused(owner, source, 'declined part of this execution')
        self.assertEqual(self.service._get('decisions', 'refusal'), refusal)
        self.service.db.execute("DELETE FROM decisions WHERE id='refusal'")
        # An approved pause card is not a refusal. Its Work pause alone holds the execution.
        pause = dict(id='human-pause', task_id=owner['id'], bot_id=owner['bot_id'], status='approved',
                     action_type='question', decided_by='person-device', revision=1)
        item = dict(id='paused-work', task_id=owner['id'], bot_id=owner['bot_id'], kind='work', title=owner['title'],
                    status='waiting', paused_by_decision_id='human-pause', created_at='2026-10-20')
        self.service._put('decisions', pause)
        self.service._put('artifacts', item)
        self.refused(owner, source, 'paused')
        self.assertEqual(self.service._get('artifacts', 'paused-work'), item)
        self.assert_untouched(owner)

    def test_lane_refuses_paused_cancelled_and_earlier_conversations(self):
        owner, source = self.lane_native()
        for change in (dict(_conversation_epoch='earlier'), dict(session_id='earlier-session')):
            with self.subTest(change=sorted(change)):
                changed = dict(owner, **change)
                self.service._put('tasks', changed)
                self.refused(changed, source, 'paused, cancelled, or belongs to an earlier conversation')
        self.service._put('tasks', owner)
        exchange = self.service._get('exchanges', owner['exchange_id'])
        self.service._put('exchanges', dict(exchange, cancelled=True))
        self.refused(owner, source, 'paused, cancelled')
        self.service._put('exchanges', exchange)
        self.assert_untouched(owner)

    def test_lane_refuses_concurrent_or_uncertain_native_dispatch(self):
        owner, source = self.lane_native()
        for flag in ('_kanban_claiming', '_kanban_dispatch_uncertain'):
            with self.subTest(flag=flag), patch.object(self.service, flag, True, create=True):
                self.refused(owner, source, 'Native dispatch is in progress')
        self.assert_untouched(owner)

    def test_lane_refuses_an_active_reviewer_worker_or_card_request(self):
        owner, source = self.lane_native()
        native = self.service.kanban.tasks[owner['id']]
        for field in ('current_run_id', 'worker_pid'):
            native[field] = 123
            with self.subTest(field=field):
                self.refused(owner, source, 'active work')
            native[field] = None
        self.service.running[owner['id']] = Mock(is_alive=lambda: True)
        self.refused(owner, source, 'active work')
        self.service.running.clear()
        for status in ('queued', 'working'):
            self.service._put('contexts', dict(id='reply', task_id=owner['id'], bot_id=owner['bot_id'],
                                               decision_id='gate', status=status))
            with self.subTest(context=status):
                self.refused(owner, source, 'active request')
        self.service.db.execute("DELETE FROM contexts WHERE id='reply'")
        for state in ('running', 'done', 'blocked', 'scheduled'):
            native['status'] = state
            with self.subTest(native=state), self.assertRaises(ServiceError):
                self.lane(source)
        native['status'] = 'review'
        self.assert_untouched(owner)

    def test_lane_refuses_unknown_or_executing_tool_effects(self):
        owner, source = self.lane_native()
        for state in ('executing', 'awaiting_approval', 'awaiting_answer'):
            receipt = dict(id='unknown', task_id=owner['id'], turn_id=owner['_native_turn_id'], state=state,
                           action={'name': 'terminal', 'arguments': {'command': 'git push'}}, action_hash='x', created_at='2026-10-20')
            self.service._put('native_receipts', receipt)
            with self.subTest(state=state):
                self.refused(owner, source, 'no confirmed result')
            self.assertEqual(self.service._get('native_receipts', 'unknown'), receipt)
        self.service.db.execute("DELETE FROM native_receipts WHERE id='unknown'")
        recovering = dict(owner, _self_recovery=dict(unknown_tools=[dict(id='old-call')]))
        self.service._put('tasks', recovering)
        self.refused(recovering, source, 'no confirmed result')

    def test_lane_refuses_a_foreign_bot(self):
        owner, source = self.lane_native()
        ops = self.write('/v1/bots', {'name': 'Ops'})
        self.service._put('tasks', dict(source, bot_id=ops['id']))
        self.refused(owner, source, 'Only the owner or its requesting bot')
        self.assert_untouched(owner)

    def test_lane_turn_keeps_local_tools_and_holds_native_actions(self):
        task, source = self.admitted_turn()
        # The lane is no sandbox. Normal local and coding tools keep their full access and only record receipts.
        local = [('terminal', {'command': 'bin/rspec spec/requests/subscriptions_spec.rb'}),
                 ('terminal', {'command': 'claude -p "Review the local diff for regressions"'}),
                 ('execute_code', {'code': 'print(1)'}), ('delegate_task', {'goal': 'Review the local diff'}),
                 ('patch', {'path': 'app/models/subscription.rb', 'old_string': 'a', 'new_string': 'b'}),
                 ('kanban_comment', {'body': 'Fixed the failing spec locally.'}),
                 ('kanban_heartbeat', {'task_id': 't_existing', 'note': 'Running tests'}), ('kanban_show', {'task_id': 't_other'})]
        for name, arguments in local:
            with self.subTest(local=name):
                record = self.service.emit(task['id'], 'native_call', dict(name=name, arguments=arguments))
                self.assertEqual(record['status'], 'recorded')
                self.service.emit(task['id'], 'native_result', dict(id=record['id'], result='{"ok": true}'))
        receipts = len(self.service._all('native_receipts'))
        held = [('kanban_complete', {'summary': 'Done'}), ('kanban_complete', {'task_id': 't_existing', 'summary': 'Done'}),
                ('kanban_block', {'reason': 'Waiting'}), ('kanban_unblock', {'task_id': 't_desk'}),
                ('kanban_create', {'title': 'Second owner'}), ('kanban_link', {'parent_id': 't_existing', 'child_id': 't_x'}),
                ('kanban_request_review', {}), ('kanban_schedule', {'reason': 'Later'}),
                ('kanban_comment', {'task_id': 't_desk', 'body': 'Ready to merge'}),
                ('kanban_comment', {'board': 'other', 'body': 'x'}), ('message_agent', {'target': 'desk', 'message': 'Merge it'}),
                ('cronjob_manage', {'action': 'create'}), ('clarify', {'question': 'Accept the views?'})]
        for name, arguments in held:
            with self.subTest(held=name, arguments=arguments), self.assertRaises(ServiceError) as raised:
                self.service.emit(task['id'], 'native_call', dict(name=name, arguments=arguments))
            self.assertEqual(raised.exception.status, 403)
        self.assertEqual(len(self.service._all('native_receipts')), receipts)
        for kind, payload in (('decision_response', dict(choice='approve', decision_id='gate', decision_revision=2, quote='Yes')),
                              ('native_question', dict(title='Mark the PR ready?', options=[dict(id='y', label='Yes', explanation='')])),
                              ('native_proposal', dict(key='ready', title='Mark ready?', options=[])),
                              ('native_approval', dict(receipt_id='x')), ('decision', dict(title='Write a file')),
                              ('work_continue', dict(task_id='t_desk', message='Merge it.')), ('work_wait', dict(task_id='t_desk')),
                              ('dependency_unlink', dict(parent_id='t_existing', child_id='t_desk', reason='x')),
                              ('withdrawal_correction', {}), ('artifact', dict(kind='pr', url='https://github.com/antiwork/gumroad/pull/8541')),
                              ('bot_message', dict(target_bot_id=source['bot_id'], text='Merge it.'))):
            with self.subTest(event=kind), self.assertRaises(ServiceError) as raised:
                self.service.emit(task['id'], kind, payload)
            self.assertIn(raised.exception.status, (400, 403))
        # No answer, selection, revision, or acceptance reached a decision.
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        self.assertEqual(self.service._get('decisions', 'scope'), self.approved)
        self.assertEqual(len(self.service._all('decisions')), 3)
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'working')

    def test_local_fixes_need_the_exact_approved_question_of_this_execution(self):
        owner, source = self.lane_native()
        for work in (dict(authorization_decision_id=None), dict(scope=['diagnosis'], authorization_decision_id='scope')):
            work = {k: v for k, v in work.items() if v is not None}
            with self.subTest(work=work):
                if 'scope' not in work:
                    with self.assertRaisesRegex(ServiceError, 'needs authorization_decision_id'):
                        self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing', message='Fix.',
                            independent_technical_work=dict(decision_id='gate', decision_revision=2, scope=SCOPE)))
                else:
                    self.refused(owner, source, 'only local_fixes, needs authorization_decision_id', work)
        unrelated_approval = dict(self.approved, id='unrelated-approval', task_id='unrelated')
        candidates = [('gate', None), ('missing', None), ('unrelated-approval', unrelated_approval),
                      ('tool', dict(self.approved, id='tool', action_type='native_approval')),
                      ('file', dict(self.approved, id='file', action_type='create_file', _native=False)),
                      ('legacy', dict(self.approved, id='legacy', _native=False)),
                      ('proposal', dict(self.approved, id='proposal', inbox_kind='proposal')),
                      ('merge', dict(self.approved, id='merge', merge_pr_url=URL)),
                      ('draft', dict(self.approved, id='draft', draft={'body': 'x'})),
                      ('pending', None), ('unsigned', dict(self.approved, id='unsigned', decided_by=None)),
                      ('agent', dict(self.approved, id='agent', _answers=[dict(choice='approve', option_id='all', decided_by=None)])),
                      ('other-person', dict(self.approved, id='other-person', _answers=[dict(choice='approve', option_id='all', decided_by='someone')])),
                      ('other-option', dict(self.approved, id='other-option', selected_option_id='library')),
                      ('no-option', dict(self.approved, id='no-option', selected_option_id='gone')),
                      ('reply', dict(self.approved, id='reply', status='replied'))]
        for identity, record in candidates:
            if record:
                self.service._put('decisions', record)
            with self.subTest(authorization=identity):
                self.refused(owner, source, 'needs authorization_decision_id', dict(authorization_decision_id=identity))
            if record:
                self.service.db.execute('DELETE FROM decisions WHERE id=?', (identity,))
        self.assert_untouched(owner)
        # Diagnosis and tests need no approval and keep exactly that scope, with no fix permission.
        self.lane(source, dict(scope=['tests', 'diagnosis']))
        lane = self.service._get('tasks', owner['id'])['_technical_lane']
        self.assertEqual(lane['scope'], ['diagnosis', 'tests'])
        self.assertNotIn('authorization', lane)
        self.assertIn('Do not change product code.', instructions(lane))

    def test_an_executed_approval_still_authorizes_its_own_recorded_choice(self):
        owner, source = self.lane_native()
        self.approved['status'] = 'executed'
        self.service._put('decisions', self.approved)
        self.lane(source)
        lane = self.service._get('tasks', owner['id'])['_technical_lane']
        text = instructions(lane)
        self.assertIn('Build the complete list', text)
        self.assertIn(URL, text)
        self.assertNotIn('Keep the Library-only filter', text)

    def test_scope_instructions_follow_the_exact_scope(self):
        diagnosis = instructions(dict(scope=['diagnosis']))
        fixes = instructions(dict(scope=SCOPE, authorization=dict(decision_id='scope', selected_option_label='Build it')))
        self.assertIn('Do not change product code.', diagnosis)
        self.assertNotIn('Fix only', diagnosis)
        self.assertIn('Fix only the technical defects inside the choice', fixes)
        self.assertIn('"selected_option_label": "Build it"', fixes)
        self.assertNotIn('Do not change product code.', fixes)
        for text in (diagnosis, fixes):
            self.assertIn('Do not push, publish', text)
            self.assertIn('grants no new permission', text)

    def test_finished_lane_turn_returns_to_the_pending_decision(self):
        task, source = self.admitted_turn()
        requester = self.service._get('tasks', source['id'])
        self.service.emit(task['id'], 'text_delta', {'text': 'The failing spec passes locally.'})
        self.service.emit(task['id'], 'completed', {'summary': 'Fixed the CI findings locally.'})
        finished = self.service._get('tasks', task['id'])
        self.assertEqual((finished['status'], finished['summary']), ('needs_you', self.gate['title']))
        self.assertNotIn('_return_pending', finished)
        self.assertEqual(finished['_reply_to_task_id'], task['_reply_to_task_id'])
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        # The approved scope stays approved until the outcome really finishes.
        self.assertEqual(self.service._get('decisions', 'scope'), self.approved)
        self.assertEqual(self.service._get('tasks', source['id']), requester)
        self.assert_unrelated_untouched()
        rows = [row for row in self.service._kanban_rows() if row['id'] == task['id']]
        self.assertEqual([row['state'] for row in rows], ['review'])
        self.assertIn('The decision remains pending.', self.service._task_messages(task['id'])[-1]['text'])
        # A failed lane turn also leaves the decision in charge instead of failing the outcome.
        self.service._put('tasks', dict(finished, status='working'))
        self.service.emit(task['id'], 'failed', {'summary': 'Work stopped: the model failed.'})
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'needs_you')
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)

    def test_a_failed_lane_turn_with_a_follow_up_keeps_the_decision_route_and_follow_up(self):
        task, source = self.admitted_turn()
        requester = self.service._get('tasks', source['id'])
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            self.service._steer_task(owner, 'Any progress on the CI findings?', [], owner['conversation_bot_id'])
            self.service._put('tasks', owner)
        follow_up = self.service._get('tasks', task['id'])
        self.assertEqual(follow_up['_steering'], ['Any progress on the CI findings?'])
        self.service.emit(task['id'], 'failed', {'summary': 'Work stopped: the model failed.'})
        failed = self.service._get('tasks', task['id'])
        self.assertEqual(failed['status'], 'needs_you')
        self.assertTrue(failed['summary'].startswith(self.gate['title']))
        self.assertNotIn('_return_pending', failed)
        self.assertEqual((failed['_reply_to_task_id'], failed['source_task_id'], failed['source_bot_id']),
                         (task['_reply_to_task_id'], task['source_task_id'], task['source_bot_id']))
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        self.assertEqual(self.service._get('decisions', 'scope'), self.approved, 'The authorization stays approved')
        self.assertEqual(self.service._get('tasks', source['id']), requester)
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == task['id']], [])
        message = next(m for m in self.service._task_messages(task['id']) if m.get('text') == 'Any progress on the CI findings?')
        self.assertEqual(failed['_blocked_followup']['message_ids'], [message['id']])
        # The next continuation of the same owner receives the saved follow-up, under the same pending decision.
        self.service._put('tasks', dict(source, status='working'))
        self.lane(source, message='Retry the CI fix locally.')
        resumed = self.service._get('tasks', task['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertNotIn('_blocked_followup', resumed)
        self.assertEqual(resumed['_steering'], [])
        seen = {}
        def start_task(provider_task, bot, history, emit):
            seen.update(messages=provider_task['_turn_message_ids'], lane=provider_task.get('_technical_lane'))
            emit('completed', {'summary': 'Answered the follow-up.'})
        self.service.provider.start_task = start_task
        self.service._run_task(task['id'])
        self.assertIn(message['id'], seen['messages'])
        self.assertEqual(seen['lane']['decision_id'], 'gate')
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'needs_you')
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        self.assertEqual(self.service._get('decisions', 'scope'), self.approved)

    def test_a_restart_during_a_lane_tool_returns_to_the_decision_and_keeps_the_unknown_tool(self):
        from coconut.service import Service
        task, source = self.admitted_turn()
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            self.service._steer_task(owner, 'Is the spec run finished?', [], owner['conversation_bot_id'])
            self.service._put('tasks', owner)
        call = self.service.emit(task['id'], 'native_call', dict(name='terminal', arguments={'command': 'bin/rspec'}))
        unknown = self.service._get('native_receipts', call['id'])
        self.assertEqual(unknown['state'], 'executing')
        # Like the live coordinator, the requester waits for this owner's result while the lane runs.
        requester = dict(self.service._get('tasks', source['id']), status='waiting', _waiting_on=[task['id']])
        self.service._put('tasks', requester)
        native = copy.deepcopy(self.service.kanban.tasks)
        # The Mac service stops while the tool runs, and a new Service reopens the same store.
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service.kanban = Mock(tasks=native)
        self.service.kanban.call.side_effect = lambda request: self.snapshot_result()
        recovered = self.service._get('tasks', task['id'])
        self.assertEqual(recovered['status'], 'needs_you')
        self.assertTrue(recovered['summary'].startswith(self.gate['title']))
        self.assertNotIn('_return_pending', recovered)
        for key in ('_reply_to_task_id', 'source_task_id', 'source_bot_id', '_execution_session_id',
                    '_native_outcome_id', '_native_turn_id', '_kanban_claim', '_technical_lane'):
            self.assertEqual(recovered.get(key), task.get(key), key)
        message = next(m for m in self.service._task_messages(task['id']) if m.get('text') == 'Is the spec run finished?')
        self.assertEqual(recovered['_blocked_followup']['message_ids'], [message['id']])
        self.assertIn('A tool stopped without a receipt', self.service._task_messages(task['id'])[-1]['text'])
        # The unknown tool keeps its receipt. Nothing completes, clears, or repeats it.
        self.assertEqual(self.service._get('native_receipts', call['id']), unknown)
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        self.assertEqual(self.service._get('decisions', 'scope'), self.approved)
        self.assertEqual(self.service._get('tasks', source['id']), requester)
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == task['id']], [])
        self.assert_unrelated_untouched()
        self.assertEqual([r['state'] for r in self.service._kanban_rows() if r['id'] == task['id']], ['review'])
        # Every continuation fails closed until someone checks that tool.
        self.service._put('tasks', dict(source, status='working'))
        with self.assertRaisesRegex(ServiceError, 'no confirmed result'):
            self.lane(source, message='Retry after the restart.')
        with self.assertRaisesRegex(ServiceError, 'Wait for this task'):
            self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing', message='Continue.'))
        self.assertEqual(self.service._get('tasks', task['id']), recovered)
        self.assertEqual(self.service._get('native_receipts', call['id']), unknown)

    def gate_turn_receipt(self, owner):
        """A completed effect of the turn that asked the gate, before the lane."""
        from coconut.service import digest, encoded
        action = {'name': 'terminal', 'arguments': {'command': 'bin/rails db:seed'}}
        receipt = dict(id='pre-lane', task_id=owner['id'], turn_id=owner['_native_turn_id'], state='completed',
                       action=action, action_hash=digest(encoded(action)), result=json.dumps({'exit_code': 0}),
                       created_at='2026-10-20T05:30:00Z', completed_at='2026-10-20T05:30:01Z')
        self.service._put('native_receipts', receipt)
        return receipt

    def answer_gate(self):
        revision = self.service._get('decisions', 'gate')['revision']
        return self.service.mutate('POST', '/v1/decisions/gate/answer', {'option_id': 'accept', 'revision': revision},
                                   str(uuid4()), 'test-phone')

    def run_lane_turn(self, owner, answer=None):
        """The lane turn through _run_task: two runs of the same local command, as in a fix-and-retest cycle."""
        seen = {'refused': []}
        def start_task(task, bot, history, emit):
            seen['task'] = copy.deepcopy(task)
            for index, output in enumerate(('1 example, 1 failure', '1 example, 0 failures')):
                try:
                    record = emit('native_call', {'name': 'terminal', 'arguments': {'command': 'bin/rspec'}})
                except ServiceError as error:
                    seen['refused'].append(error.status)
                    continue
                if answer == 'during_tool' and index == 0:
                    self.answer_gate()
                try:
                    emit('native_result', {'id': record['id'], 'result': json.dumps({'output': output})})
                except ServiceError as error:
                    seen['refused'].append(error.status)
                if answer == 'after_tool' and index == 0:
                    self.answer_gate()
            emit('completed', {'summary': 'The specs pass locally.'})
        self.service.provider.start_task = start_task
        self.service._run_task(owner['id'])
        return seen

    def resume_after_gate(self, owner):
        """The dispatcher builds rows, then the resumed provider turn runs. Returns its exact provider input."""
        from coconut.native_bridge import NativeBridge
        self.service._kanban_rows()
        captured = {}
        def resume_task(task, bot, history, decision, emit):
            captured.update(task=copy.deepcopy(task), decision=copy.deepcopy(decision),
                            resume_decision=self.service._get('tasks', owner['id'])['_resume_decision'])
            record = emit('native_call', {'name': 'kanban_heartbeat', 'arguments': {'note': 'Resumed.'}})
            emit('native_result', {'id': record['id'], 'result': '{"ok": true}'})
            captured['resumed_complete'] = emit('native_call', {'name': 'kanban_complete', 'arguments': {'summary': 'Done.'}})['status']
            emit('text_delta', {'text': RESUMED_RESULT})
            emit('completed', {'summary': RESUMED_RESULT, '_work_todos': progress('completed')})
        def start_task(task, bot, history, emit):
            raise AssertionError('An answered decision resumes; it does not start a new request.')
        self.service.provider.resume_task, self.service.provider.start_task = resume_task, start_task
        self.service._run_task(owner['id'])
        self.finish_resumed_outcome(owner, captured)
        bridge = NativeBridge(dict(operation='resume', task=captured['task'], decision=captured['decision'],
                                   bot={'id': owner['bot_id']}), lambda *a: None, lambda *a: None)
        return captured, bridge

    def finish_resumed_outcome(self, owner, captured):
        """The real result route after the resumed turn. Hermes records its kanban_complete on the same card."""
        native = self.service.kanban.tasks[owner['id']]
        native.update(status='done', input_id=captured['task']['_native_turn_id'], outcome_summary=RESUMED_RESULT,
                      current_run_id=None, worker_pid=None)
        lane_texts = {m['text'] for m in self.service._task_messages(owner['id'])
                      if m.get('role') == 'assistant' and m.get('created_at', '') < self.service._get('tasks', owner['id']).get('_turn_started_at', '')}
        for _ in range(2):
            # Repeated passes must not deliver the result twice.
            self.service._reconcile_kanban()
            self.service._drain_returns()
        returned = [d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']]
        captured['returned'], captured['lane_texts'] = returned, lane_texts

    def assert_returned_once(self, owner, captured):
        returned = captured['returned']
        self.assertEqual(len(returned), 1, 'Exactly one final result reaches the original requester')
        self.assertEqual((returned[0]['target_bot_id'], returned[0]['text']), (owner['source_bot_id'], RESUMED_RESULT))
        self.assertNotIn(returned[0]['text'], captured['lane_texts'])
        callback = self.service._get('tasks', returned[0]['receipt']['task_id'])
        self.assertEqual((callback['bot_id'], callback['source_task_id'], callback['_is_return']),
                         (owner['source_bot_id'], owner['id'], True))
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        self.assertNotIn('_return_pending', self.service._get('tasks', owner['id']))

    def lane_receipts(self, owner, lane_turn):
        return [r for r in self.service._native_task_receipts(owner['id']) if r['turn_id'] == lane_turn]

    def assert_resumed_from_the_gate(self, owner, lane_turns, captured, bridge, pre_lane, expect=('approved', 'accept')):
        lane_turns = [lane_turns] if isinstance(lane_turns, str) else list(lane_turns)
        task = captured['task']
        self.assertNotIn(task['_native_turn_id'], [pre_lane['turn_id'], *lane_turns])
        # Only the decision turn's completed effects are carried. The lane's results are history, never this turn's.
        self.assertEqual([r['id'] for r in task['_native_receipts']], ['pre-lane'])
        self.assertEqual(set(bridge.completed), {pre_lane['action_hash']})
        from coconut.native_receipt_context import receipt_prompt
        self.assertIn('"total": 1', receipt_prompt(task['_native_receipts']))
        lane = [r for turn in lane_turns for r in self.lane_receipts(owner, turn)]
        self.assertTrue(all(self.lane_receipts(owner, turn) for turn in lane_turns), 'Every lane turn ran a tool')
        # No output of any lane turn is an input or a cached result of the resumed decision turn.
        self.assertFalse({r['id'] for r in lane} & {r['id'] for r in task['_native_receipts']})
        self.assertFalse({r['action_hash'] for r in lane} & set(bridge.completed))
        # The provider receives the person's actual answer as the resumed decision.
        self.assertEqual((captured['resume_decision'], captured['decision']['id'], captured['decision']['status'],
                          captured['decision']['selected_option_id']), ('gate', 'gate', *expect))
        self.assertEqual((task['id'], task['_execution_session_id'], task['_native_outcome_id']),
                         (owner['id'], owner['_execution_session_id'], 't_existing'))
        stored = self.service._get('tasks', owner['id'])
        # A human follow-up starts a new exchange in _steer_task, as in any task, so the exchange is not compared.
        for key in ('_execution_session_id', '_reply_to_task_id', 'source_task_id', 'source_bot_id', '_kanban_claim',
                    'thread_id', 'session_id', 'conversation_bot_id'):
            self.assertEqual(stored.get(key), owner.get(key), key)
        self.assertNotIn('_technical_lane', task)
        # The resumed turn completed normally, so the approved question is executed. Its human answer is unchanged.
        scope = self.service._get('decisions', 'scope')
        self.assertEqual(scope['status'], 'executed')
        self.assertEqual(captured['resumed_complete'], 'recorded', 'The resumed turn has normal native actions')
        self.assert_returned_once(owner, captured)
        self.assertEqual({k: v for k, v in scope.items() if k not in ('status', 'outcome')},
                         {k: v for k, v in self.approved.items() if k not in ('status', 'outcome')})
        self.assertEqual(self.service._get('native_receipts', 'pre-lane'), pre_lane)
        self.assert_unrelated_untouched()

    def test_an_answer_after_a_finished_lane_resumes_the_decision_turn_without_lane_replays(self):
        owner, source = self.lane_native()
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        seen = self.run_lane_turn(owner)
        # The lane turn itself does not replay the decision turn's effects: its test runs stay fresh.
        self.assertEqual(seen['task']['_native_receipts'], [])
        finished = self.service._get('tasks', owner['id'])
        self.assertEqual((finished['status'], finished['_native_turn_id']), ('needs_you', lane_turn))
        self.assertEqual(len(self.lane_receipts(owner, lane_turn)), 2)
        lane_messages = finished.get('_turn_message_ids', [])
        with self.service.transaction():
            later = self.service._message(owner['bot_id'], 'Also check the mobile layout.', task_id=owner['id'],
                                          role='user', human_authored=True)
        self.answer_gate()
        captured, bridge = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, lane_turn, captured, bridge, pre_lane)
        self.assertEqual(captured['task']['_turn_message_ids'], [later['id']])
        self.assertFalse(set(lane_messages) & set(captured['task']['_turn_message_ids']))

    def test_an_answer_during_an_active_lane_resumes_the_decision_turn_without_lane_replays(self):
        owner, source = self.lane_native()
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        seen = self.run_lane_turn(owner, answer='after_tool')
        self.assertEqual(seen['refused'], [409], 'The lane worker loses tool access when the person answers')
        answered = self.service._get('tasks', owner['id'])
        self.assertEqual((answered['status'], answered['_resume_decision'], answered['_native_turn_id']),
                         ('queued', 'gate', lane_turn))
        captured, bridge = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, lane_turn, captured, bridge, pre_lane)

    def test_an_automatic_gate_end_also_resumes_without_lane_replays(self):
        owner, source = self.lane_native()
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        self.run_lane_turn(owner)
        withdrawn = dict(self.gate, status='resolved', resolution='work_scope_changed', resolution_context='Withdrawn.',
                         outcome='Obsolete.', resolved_at='now', revision=3)
        self.service._put('decisions', withdrawn)
        self.service._put('tasks', dict(self.service._get('tasks', owner['id']), status='queued', _resume_decision='gate'))
        self.service._kanban_rows()
        resumed = self.service._get('tasks', owner['id'])
        self.assertNotEqual(resumed['_native_turn_id'], lane_turn)
        self.assertEqual(resumed['_continuation_receipt_ids'], ['pre-lane'])
        self.assertEqual(resumed['_resume_decision'], 'gate')
        self.assertNotIn('_technical_lane', resumed)
        self.assertEqual(self.service._get('decisions', 'gate'), withdrawn)

    def test_an_unknown_lane_effect_still_fails_the_answered_resume_closed(self):
        owner, source = self.lane_native()
        self.gate_turn_receipt(owner)
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        seen = self.run_lane_turn(owner, answer='during_tool')
        unknown = [r for r in self.lane_receipts(owner, lane_turn) if r['state'] == 'executing']
        self.assertEqual(len(unknown), 1, seen)
        self.service._kanban_rows()
        held = self.service._get('tasks', owner['id'])
        # No fresh turn hides the unknown effect behind the person's answer.
        self.assertEqual((held['status'], held['_native_turn_id']), ('queued', lane_turn))
        self.assertIn('_technical_lane', held)
        def resume_task(*args):
            raise AssertionError('An unknown effect must stop the resume before the provider')
        self.service.provider.resume_task = resume_task
        self.service._run_task(owner['id'])
        failed = self.service._get('tasks', owner['id'])
        self.assertEqual((failed['status'], failed['summary']),
                         ('failed', 'A previous tool has no receipt. Check its result before starting new work.'))
        self.assertEqual(self.service._get('native_receipts', unknown[0]['id']), unknown[0])
        self.assertEqual(self.service._get('decisions', 'gate')['selected_option_id'], 'accept')

    def test_the_lane_turn_and_ordinary_resumes_keep_their_turn(self):
        owner, source = self.lane_native()
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        # A requeued lane turn while the decision is pending, as after a restart without unknown tools, keeps its turn.
        self.service._kanban_rows()
        self.assertEqual(self.service._get('tasks', owner['id'])['_native_turn_id'], lane_turn)
        # An ordinary answered owner without a lane is unchanged by the dispatcher.
        ordinary = dict(self.service._get('tasks', owner['id']), status='queued', _resume_decision='gate',
                        _continuation_receipt_ids=['kept'])
        ordinary.pop('_technical_lane')
        self.service._put('tasks', ordinary)
        self.service._put('decisions', dict(self.gate, status='approved', decided_by='person-device'))
        self.service._kanban_rows()
        self.assertEqual(self.service._get('tasks', owner['id']), ordinary)

    def drive(self, owner, tools=1, steer=None, answer=None, unknown=False, end='completed', during=None):
        """One real lane turn through _run_task: local tools, an optional live follow-up, answer, or unknown result."""
        seen = {'refused': []}
        def start_task(task, bot, history, emit):
            seen['task'] = copy.deepcopy(task)
            for index in range(tools):
                try:
                    record = emit('native_call', {'name': 'terminal', 'arguments': {'command': 'bin/rspec'}})
                except ServiceError as error:
                    seen['refused'].append(error.status)
                    continue
                if unknown and index == tools - 1:
                    seen['unknown'] = record['id']
                    if during:
                        try:
                            during(task, emit)
                        except Exception as error:
                            seen['during_error'] = repr(error)
                            raise
                    elif answer == 'during_tool':
                        self.answer_gate()
                    break
                emit('native_result', {'id': record['id'], 'result': json.dumps({'output': 'run %d' % index})})
                if answer == 'after_tool' and index == 0:
                    self.answer_gate()
                if during and index == 0:
                    try:
                        during(task, emit)
                    except Exception as error:
                        seen['during_error'] = repr(error)
                        raise
                    try:
                        emit('native_call', {'name': 'kanban_complete', 'arguments': {'summary': 'Done.'}})
                        seen['held_after'] = 'recorded'
                    except ServiceError as error:
                        seen['held_after'] = error.status
            if steer:
                with self.service.transaction():
                    current = self.service._get('tasks', owner['id'])
                    self.service._steer_task(current, steer, [], current['conversation_bot_id'])
                    self.service._put('tasks', current)
                seen['steer'] = next(m['id'] for m in self.service._task_messages(owner['id']) if m.get('text') == steer)
            emit(end, {'summary': 'Lane turn ' + end + '.'})
        self.service.provider.start_task = start_task
        self.service._run_task(owner['id'])
        return seen

    def lane_turns(self, owner):
        return self.service._get('tasks', owner['id'])['_technical_lane']['turn_ids']

    def assert_lane_held(self, owner, status):
        held = self.service._get('tasks', owner['id'])
        self.assertEqual(held['status'], status)
        self.assertEqual(self.service._get('decisions', 'scope'), self.approved, 'The approved scope stays approved')
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        self.assertNotIn('_return_pending', held)
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']], [])
        self.assertEqual((held['_reply_to_task_id'], held['source_task_id'], held['source_bot_id']),
                         (owner['_reply_to_task_id'], owner['source_task_id'], owner['source_bot_id']))
        return held

    def test_readmission_keeps_the_original_gate_checkpoint_across_failures_and_revisions(self):
        owner, source = self.lane_native()
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        first = self.service._get('tasks', owner['id'])['_technical_lane']
        self.assertEqual((first['gate_turn_id'], first['gate_receipt_ids']), ('completed-input', ['pre-lane']))
        seen = self.drive(owner, tools=2, steer='Please rerun the flaky spec.', end='failed')
        self.assertIn('_blocked_followup', self.assert_lane_held(owner, 'needs_you'))
        # An automated revision of the same question, then a retry of the lane with the saved follow-up.
        self.gate = dict(self.gate, revision=3, _answers=self.gate['_answers'] + [dict(choice='revise', decided_by=None)])
        self.service._put('decisions', self.gate)
        self.service._put('tasks', dict(source, status='working'))
        self.lane(source, dict(decision_revision=3), message='Retry the CI fix locally.')
        second = self.service._get('tasks', owner['id'])['_technical_lane']
        self.assertEqual((second['gate_turn_id'], second['gate_receipt_ids']), ('completed-input', ['pre-lane']))
        self.assertEqual(second['turn_ids'][0], first['turn_id'])
        retry = self.drive(owner)
        self.assertIn(seen['steer'], retry['task']['_turn_message_ids'], 'The saved follow-up reaches the retry')
        self.assertEqual(retry['task']['_native_receipts'], [], 'The retry does not replay the failed lane turn')
        self.assert_lane_held(owner, 'needs_you')
        turns = self.lane_turns(owner)
        self.assertEqual(len(turns), 2)
        self.answer_gate()
        captured, bridge = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, turns, captured, bridge, pre_lane)

    def test_completed_follow_ups_run_as_lane_turns_and_the_answer_resumes_the_gate(self):
        owner, source = self.lane_native()
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        first = self.drive(owner, steer='Also run the request specs.')
        queued = self.assert_lane_held(owner, 'queued')
        self.assertEqual(queued['_technical_lane']['turn_id'], queued['_native_turn_id'])
        second = self.drive(owner, steer='And the system specs.')
        self.assertIn(first['steer'], second['task']['_turn_message_ids'])
        self.assertEqual(second['task']['_native_receipts'], [])
        self.assertEqual(second['task']['_technical_lane']['decision_id'], 'gate')
        self.assert_lane_held(owner, 'queued')
        third = self.drive(owner)
        self.assertIn(second['steer'], third['task']['_turn_message_ids'])
        self.assertNotIn(first['steer'], third['task']['_turn_message_ids'])
        self.assert_lane_held(owner, 'needs_you')
        turns = self.lane_turns(owner)
        self.assertEqual(len(set(turns)), 3)
        self.answer_gate()
        captured, bridge = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, turns, captured, bridge, pre_lane)

    def test_an_answer_during_a_follow_up_lane_turn_resumes_the_gate(self):
        owner, source = self.lane_native()
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        self.drive(owner, steer='Also run the request specs.')
        turns = self.lane_turns(owner)
        running = self.drive(owner, tools=2, answer='after_tool')
        self.assertEqual(running['refused'], [409])
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'queued')
        captured, bridge = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, turns, captured, bridge, pre_lane)

    def test_an_automatic_gate_end_after_follow_ups_resumes_the_gate(self):
        owner, source = self.lane_native()
        self.gate_turn_receipt(owner)
        self.lane(source)
        self.drive(owner, steer='Also run the request specs.')
        self.drive(owner)
        self.service._put('decisions', dict(self.gate, status='resolved', resolution='work_scope_changed', resolved_at='now', revision=3))
        self.service._put('tasks', dict(self.service._get('tasks', owner['id']), status='queued', _resume_decision='gate'))
        turns = self.lane_turns(owner)
        self.service._kanban_rows()
        resumed = self.service._get('tasks', owner['id'])
        self.assertNotIn(resumed['_native_turn_id'], turns)
        self.assertEqual(resumed['_continuation_receipt_ids'], ['pre-lane'])
        self.assertNotIn('_technical_lane', resumed)

    def test_an_unknown_effect_on_a_follow_up_lane_turn_fails_the_answered_resume_closed(self):
        owner, source = self.lane_native()
        self.gate_turn_receipt(owner)
        self.lane(source)
        self.drive(owner, steer='Also run the request specs.')
        follow_up = self.service._get('tasks', owner['id'])['_native_turn_id']
        seen = self.drive(owner, tools=1, unknown=True, answer='during_tool')
        self.assert_unknown_fails_the_resume_closed(owner, follow_up, seen['unknown'])

    def test_a_completed_lane_turn_with_an_unknown_effect_keeps_its_turn_and_follow_up(self):
        owner, source = self.lane_native()
        self.gate_turn_receipt(owner)
        self.lane(source)
        turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        seen = self.drive(owner, unknown=True, steer='Is the spec run finished?')
        held = self.assert_lane_held(owner, 'needs_you')
        self.assertEqual(held['_native_turn_id'], turn, 'The lane never leaves a turn with an unknown result')
        self.assertEqual(held['_blocked_followup']['message_ids'], [seen['steer']])
        with self.assertRaisesRegex(ServiceError, 'no confirmed result'):
            self.service._put('tasks', dict(source, status='working'))
            self.lane(source, message='Retry.')
        self.answer_gate()
        self.assert_unknown_fails_the_resume_closed(owner, turn, seen['unknown'])

    def assert_unknown_fails_the_resume_closed(self, owner, turn, receipt_id):
        unknown = self.service._get('native_receipts', receipt_id)
        self.assertEqual(unknown['state'], 'executing')
        self.service._kanban_rows()
        self.assertEqual(self.service._get('tasks', owner['id'])['_native_turn_id'], turn)
        def resume_task(*args):
            raise AssertionError('An unknown effect must stop the resume before the provider')
        self.service.provider.resume_task = resume_task
        self.service._run_task(owner['id'])
        failed = self.service._get('tasks', owner['id'])
        self.assertEqual((failed['status'], failed['summary']),
                         ('failed', 'A previous tool has no receipt. Check its result before starting new work.'))
        self.assertEqual(self.service._get('native_receipts', receipt_id), unknown)

    ENDINGS = {
        'answered': dict(status='approved', selected_option_id='accept', decided_by='person-device', decided_at='now'),
        'withdrawn': dict(status='resolved', resolution='work_scope_changed', resolution_context='Withdrawn.',
                          outcome='Obsolete.', resolved_at='now', revision=3),
        'merged': dict(status='resolved', outcome='Already merged.', resolved_at='now', revision=3),
        'cancelled': dict(status='resolved', resolution='cancelled', resolution_context='Cancelled.',
                          outcome='Work cancelled.', resolved_at='now', revision=3)}

    def test_a_lane_turn_keeps_its_holds_until_the_dispatcher_resumes_the_ended_decision(self):
        for name in [*self.ENDINGS, 'deleted']:
            with self.subTest(ending=name):
                task, source = self.admitted_turn()
                lane_turn = task['_native_turn_id']
                if name == 'deleted':
                    self.service.db.execute("DELETE FROM decisions WHERE id='gate'")
                else:
                    self.service._put('decisions', dict(self.gate, **self.ENDINGS[name]))
                complete = dict(name='kanban_complete', arguments={'summary': 'Done.'})
                # The in-flight lane turn keeps its holds after the question ends.
                with self.assertRaises(ServiceError) as held:
                    self.service.emit(task['id'], 'native_call', complete)
                self.assertEqual(held.exception.status, 403)
                self.service.emit(task['id'], 'completed', {'summary': 'Local checks pass.'})
                queued = self.service._get('tasks', task['id'])
                self.assertEqual((queued['status'], queued['_native_turn_id']), ('queued', lane_turn))
                self.assertEqual(queued.get('_resume_decision'), None if name == 'deleted' else 'gate')
                self.assertNotIn('_return_pending', queued)
                self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == task['id']], [])
                self.assertEqual(self.service._get('decisions', 'scope'), self.approved)
                # The dispatcher moves the owner to a normal resumed turn. The hold ends there.
                self.service._kanban_rows()
                resumed = self.service._get('tasks', task['id'])
                self.assertNotEqual(resumed['_native_turn_id'], lane_turn)
                self.assertNotIn('_technical_lane', resumed)
                self.service._put('tasks', dict(resumed, status='working'))
                self.assertEqual(self.service.emit(task['id'], 'native_call', complete)['status'], 'recorded')
                self.tearDown()
                self.setUp()

    def test_a_restart_after_the_decision_ended_keeps_an_unknown_tool_and_resumes_closed(self):
        from coconut.service import Service
        task, source = self.admitted_turn()
        lane_turn = task['_native_turn_id']
        self.service._put('decisions', dict(self.gate, **self.ENDINGS['answered']))
        call = self.service.emit(task['id'], 'native_call', dict(name='terminal', arguments={'command': 'bin/rspec'}))
        native = copy.deepcopy(self.service.kanban.tasks)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service.kanban = Mock(tasks=native)
        self.service.kanban.call.side_effect = lambda request: self.snapshot_result()
        recovered = self.service._get('tasks', task['id'])
        self.assertEqual((recovered['status'], recovered['_resume_decision'], recovered['_native_turn_id']),
                         ('queued', 'gate', lane_turn))
        self.assertNotIn('_return_pending', recovered)
        self.assert_unknown_fails_the_resume_closed(task, lane_turn, call['id'])

    def withdraw_gate(self, owner):
        """The real decision_freshness withdrawal, from a working refresh check of the gate."""
        decision = self.service._get('decisions', 'gate')
        task = self.service._get('tasks', owner['id'])
        fingerprint, _ = self.service._decision_evidence(decision, {t['id']: t for t in self.service._all('tasks')},
            self.service._all('native_receipts'), self.service._all('artifacts'))
        context = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], decision_id='gate', status='working',
                       question='Check the pending question.', created_at=task['created_at'], _decision_refresh=True,
                       _decision_revision=decision['revision'], _evidence_fingerprint=fingerprint,
                       _conversation_epoch=task.get('_conversation_epoch'),
                       _human_input_ids=[m['id'] for m in self.service._decision_human_messages(task, decision)])
        self.service._put('contexts', context)
        self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The findings were superseded.'))
        self.service._put('contexts', dict(self.service._get('contexts', context['id']), status='completed'))
        return self.service._get('decisions', 'gate')

    def cancel_work(self, work_id):
        return self.service.mutate('POST', '/v1/work/items/' + work_id + '/cancel', {}, str(uuid4()), 'test-phone')

    def assert_ended_lane_turn_held(self, owner, lane_turn, seen):
        self.assertEqual(seen['held_after'], 403, 'Native actions stay held until the old lane turn exits')
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual((queued['status'], queued['_resume_decision'], queued['_native_turn_id']), ('queued', 'gate', lane_turn))
        self.assertNotIn('_return_pending', queued)
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']], [])
        self.assertEqual(self.service._get('decisions', 'scope'), self.approved, 'No approval executes from the old lane')
        self.assertEqual([r['state'] for r in self.service._kanban_rows() if r['id'] == owner['id']], ['ready'])
        return queued

    def test_a_real_withdrawal_during_a_lane_turn_holds_it_and_resumes_the_withdrawn_decision(self):
        owner, source = self.lane_native()
        # A requester-continued owner carries no saved human input. A working human turn would keep priority,
        # and decision_freshness then refuses the withdrawal by design.
        owner = dict(owner, _preference_source_ids=[])
        self.service._put('tasks', owner)
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        withdrawn = {}
        seen = self.drive(owner, tools=2, during=lambda task, emit: withdrawn.update(self.withdraw_gate(owner)))
        self.assertNotIn('during_error', seen, seen.get('during_error'))
        self.assertEqual((withdrawn['status'], withdrawn['resolution']), ('resolved', 'work_scope_changed'))
        self.assertEqual(seen['refused'], [], 'Local tools keep running in the old lane turn')
        self.assert_ended_lane_turn_held(owner, lane_turn, seen)
        captured, bridge = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, [lane_turn], captured, bridge, pre_lane, expect=('resolved', None))
        self.assertEqual(captured['decision']['resolution_context'], withdrawn['resolution_context'])

    def test_a_real_shared_work_cancel_during_a_follow_up_lane_turn_resumes_with_its_stop_message(self):
        owner, source = self.lane_native()
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        work_id = self.service._get('tasks', owner['id'])['_work_context_id']
        # The owner also serves another Work outcome, so cancelling one is a scope change, not a stop.
        self.service._put('artifacts', dict(id='other-work', task_id=owner['id'], bot_id=owner['bot_id'], kind='work',
            title='Another outcome', status='queued', created_at=owner['created_at'], _native_outcome_id='t_other'))
        first = self.drive(owner, steer='Also run the request specs.')
        follow_up_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        consumed = {}
        def during(task, emit):
            with self.service.transaction():
                current = self.service._get('tasks', owner['id'])
                self.service._steer_task(current, 'Use the staging seed data.', [], current['conversation_bot_id'])
                self.service._put('tasks', current)
            consumed['id'] = next(m['id'] for m in self.service._task_messages(owner['id']) if m.get('text') == 'Use the staging seed data.')
            emit('steering_consumed', {'turn_id': task['_native_turn_id'], 'message_ids': [consumed['id']]})
            self.cancel_work(work_id)
        seen = self.drive(owner, tools=2, during=during)
        self.assertNotIn('during_error', seen, seen.get('during_error'))
        gate = self.service._get('decisions', 'gate')
        self.assertEqual((gate['status'], gate['resolution']), ('resolved', 'work_scope_changed'))
        stop = next(m for m in self.service._task_messages(owner['id']) if m.get('text', '').startswith('Stop work on'))
        self.assert_ended_lane_turn_held(owner, follow_up_turn, seen)
        captured, bridge = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, [first['task']['_native_turn_id'], follow_up_turn], captured, bridge,
                                          pre_lane, expect=('resolved', None))
        self.assertIn(stop['id'], captured['task']['_turn_message_ids'], 'The unconsumed stop message reaches the resume')
        self.assertNotIn(consumed['id'], captured['task']['_turn_message_ids'], 'A live message the lane turn read is not repeated')
        self.assertNotIn(first['steer'], captured['task']['_turn_message_ids'])
        self.assertIn('Ask a new question only if', captured['decision']['resolution_context'])

    def test_cancelling_the_owners_only_work_during_a_lane_turn_stops_it(self):
        owner, source = self.lane_native()
        self.lane(source)
        work_id = self.service._get('tasks', owner['id'])['_work_context_id']
        seen = self.drive(owner, tools=2, during=lambda task, emit: self.cancel_work(work_id))
        self.assertNotIn('during_error', seen, seen.get('during_error'))
        stopped = self.service._get('tasks', owner['id'])
        self.assertEqual(stopped['status'], 'cancelled')
        self.assertNotIn('_resume_decision', stopped)
        self.assertNotIn('_return_pending', stopped)
        # The normal task stop closes its decisions as cancelled. Nothing executes and nothing resumes.
        for identity in ('scope', 'gate'):
            closed = self.service._get('decisions', identity)
            self.assertEqual((closed['status'], closed['decided_by']), ('rejected', 'cancellation'))
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']], [])
        self.service._kanban_rows()
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'cancelled', 'A Work stop grants no resume')

    def test_a_real_withdrawal_during_an_unknown_lane_tool_keeps_the_turn_and_resumes_closed(self):
        owner, source = self.lane_native()
        # A requester-continued owner carries no saved human input. A working human turn would keep priority,
        # and decision_freshness then refuses the withdrawal by design.
        owner = dict(owner, _preference_source_ids=[])
        self.service._put('tasks', owner)
        self.gate_turn_receipt(owner)
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        seen = self.drive(owner, tools=1, unknown=True, during=lambda task, emit: self.withdraw_gate(owner))
        held = self.service._get('tasks', owner['id'])
        self.assertEqual((held['status'], held['_resume_decision'], held['_native_turn_id']), ('queued', 'gate', lane_turn))
        self.assert_unknown_fails_the_resume_closed(owner, lane_turn, seen['unknown'])

    def test_a_restart_after_a_mid_turn_withdrawal_resumes_the_withdrawn_decision(self):
        from coconut.service import Service
        owner, source = self.lane_native()
        owner = dict(owner, _preference_source_ids=[])
        self.service._put('tasks', owner)
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        task = self.service._get('tasks', owner['id'])
        lane_turn = task['_native_turn_id']
        self.service._put('tasks', dict(task, status='working'))
        record = self.service.emit(owner['id'], 'native_call', dict(name='terminal', arguments={'command': 'bin/rspec'}))
        self.service.emit(owner['id'], 'native_result', dict(id=record['id'], result='{"exit_code": 0}'))
        withdrawn = self.withdraw_gate(owner)
        native = copy.deepcopy(self.service.kanban.tasks)
        # The Mac service stops before the lane turn exits; no tool result is unknown.
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service.kanban = Mock(tasks=native, observed_at=None)
        self.service.kanban.call.side_effect = lambda request: self.snapshot_result()
        requeued = self.service._get('tasks', owner['id'])
        self.assertEqual((requeued['status'], requeued['_native_turn_id']), ('queued', lane_turn))
        self.assertNotIn('_resume_decision', requeued)
        captured, bridge = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, [lane_turn], captured, bridge, pre_lane, expect=('resolved', None))
        self.assertEqual(captured['decision']['resolution_context'], withdrawn['resolution_context'])

    def test_lane_turn_through_the_service_run_route(self):
        owner, source = self.lane_native()
        self.lane(source)
        seen = {}
        def start_task(task, bot, history, emit):
            seen['lane'] = task.get('_technical_lane')
            record = emit('native_call', {'name': 'terminal', 'arguments': {'command': 'bin/rspec'}})
            emit('native_result', {'id': record['id'], 'result': json.dumps({'output': '1 example, 0 failures', 'exit_code': 0})})
            try:
                emit('native_call', {'name': 'kanban_complete', 'arguments': {'summary': 'Fixed.'}})
            except ServiceError as error:
                seen['complete'] = error.status
            emit('text_delta', {'text': 'The spec passes locally.'})
            emit('completed', {'summary': 'Fixed locally.'})
        self.service.provider.start_task = start_task
        self.service._run_task(owner['id'])
        self.assertEqual(seen['lane']['decision_id'], 'gate')
        self.assertEqual(seen['complete'], 403)
        finished = self.service._get('tasks', owner['id'])
        self.assertEqual(finished['status'], 'needs_you')
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        names = [r['action']['name'] for r in self.service._native_task_receipts(owner['id']) if r['turn_id'] == finished['_native_turn_id']]
        self.assertEqual(names, ['terminal'])

    def test_an_automated_pending_revision_keeps_the_lane(self):
        task, source = self.admitted_turn()
        revised = dict(self.gate, revision=3, _answers=self.gate['_answers'] + [dict(choice='revise', decided_by=None)])
        self.service._put('decisions', revised)
        with self.assertRaises(ServiceError):
            self.service.emit(task['id'], 'native_call', dict(name='kanban_complete', arguments={}))
        self.assertEqual(self.service._get('decisions', 'gate'), revised)

    def test_a_callback_of_the_owner_keeps_its_normal_route(self):
        task, source = self.admitted_turn()
        callback = dict(copy.deepcopy(task), id=str(uuid4()), status='working', _assignment_task_id=task['id'],
                        _is_return=True, _native_turn_id=str(uuid4()))
        callback.pop('_technical_lane')
        self.service._put('tasks', callback)
        self.assertIsNone(self.service._technical_lane(callback))
        record = self.service.emit(callback['id'], 'native_call', dict(name='kanban_comment',
            arguments={'task_id': 't_desk', 'body': 'Callback note'}))
        self.assertEqual(record['status'], 'recorded')
        self.assertFalse(self.service._finish_technical_lane(callback, 'completed', 'Done.'))
        self.service.emit(callback['id'], 'completed', {'summary': 'Callback finished.'})
        self.assertNotEqual(self.service._get('tasks', callback['id'])['status'], 'needs_you')
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)

    def retained_pr_owner(self):
        """The live topology: an owner without _native_outcome_id whose merged and open PRs serve the requester's Work."""
        owner, source = self.lane_native()
        requester = self.service._get('tasks', source['id'])
        work = dict(id='requester-work', task_id=requester['id'], bot_id=requester['bot_id'], kind='work',
            title='Buyer-facing membership list', status='queued', created_at=requester['created_at'],
            _native_outcome_id='t_requester', _work_key='native-t_earlier')
        requester.update(_native_outcome_id='t_requester', _work_context_id=work['id'], _waiting_on=[])
        self.service._put('tasks', requester)
        requester_pr = dict(id='requester-pr', task_id=requester['id'], bot_id=requester['bot_id'], kind='pr',
            title='#8537', status='merged', pr_state='merged', url=URL, created_at=requester['created_at'],
            work_outcome_id=work['id'], _review_task_id=requester['id'])
        merged = dict(id='merged-pr', task_id=owner['id'], bot_id=owner['bot_id'], kind='pr', title='#8537',
            status='merged', pr_state='merged', url=URL, created_at=owner['created_at'], work_outcome_id=work['id'],
            _review_task_id=owner['id'], _activity_task_id=owner['id'])
        follow_up = dict(merged, id='open-pr', title='#8541', status='failed', pr_state='open', url=URL[:-1] + '1')
        for item in (work, requester_pr, merged, follow_up):
            self.service._put('artifacts', item)
        self.assertNotIn('_native_outcome_id', owner)
        return owner, source, dict(work=work, requester_pr=requester_pr, merged=merged, open=follow_up)

    def test_the_live_retained_pr_topology_continues_its_open_pr_without_new_work(self):
        owner, source, items = self.retained_pr_owner()
        before = {a['id']: a for a in self.service._all('artifacts')}
        receipt = self.lane(source)
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertEqual(queued['_work_context_id'], 'open-pr')
        self.assertEqual(queued['_native_outcome_id'], 't_existing')
        after = {a['id']: a for a in self.service._all('artifacts')}
        self.assertEqual(set(after), set(before), 'No duplicate Work item')
        self.assertEqual(after['open-pr'], dict(before['open-pr'], _native_outcome_id='t_existing'))
        for identity in ('requester-work', 'requester-pr', 'merged-pr'):
            self.assertEqual(after[identity], before[identity])
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)

    def test_retained_pr_ambiguity_and_live_requester_writers_still_refuse(self):
        cases = {
            'two open delegated PRs': ('Choose one retained Work item', dict(merged=dict(pr_state='open', status='open'))),
            'two finished delegated PRs': ('Choose one retained Work item', dict(open=dict(pr_state='merged', status='merged'))),
            'an open requester PR of the Work': ('another execution', dict(requester_pr=dict(pr_state='open', status='open'))),
            'a finished PR of another Work': ('Choose one retained Work item', dict(merged=dict(work_outcome_id='other-work')))}
        for name, (pattern, changes) in cases.items():
            with self.subTest(case=name):
                owner, source, items = self.retained_pr_owner()
                self.service._put('artifacts', dict(items['work'], id='other-work', _native_outcome_id='t_other'))
                for key, change in changes.items():
                    self.service._put('artifacts', dict(items[key], **change))
                before = self.service._all('artifacts')
                self.refused(owner, source, pattern)
                self.assertEqual(self.service._all('artifacts'), before)
                self.tearDown()
                self.setUp()


class TechnicalLanePolicyTests(unittest.TestCase):
    def test_held_actions_name_native_actions_and_never_read_commands(self):
        for name, arguments in (('terminal', {'command': 'anything'}), ('execute_code', {}), ('delegate_task', {}),
                                ('write_file', {}), ('kanban_show', {'task_id': 't_other'}), ('kanban_list', {}),
                                ('kanban_comment', {'body': 'x'}), ('kanban_comment', {'task_id': 't_own', 'body': 'x'})):
            with self.subTest(local=name):
                self.assertIsNone(held_action(dict(name=name, arguments=arguments), 't_own'))
        for name, arguments in (('kanban_complete', {}), ('kanban_comment', {'task_id': 't_other'}),
                                ('kanban_list', {'board': 'other'}), ('kanban_new_write', {}), ('message_agent', {}),
                                ('cronjob_manage', {}), ('clarify', {})):
            with self.subTest(held=name):
                self.assertIsNotNone(held_action(dict(name=name, arguments=arguments), 't_own'))


class TechnicalLaneProviderTests(unittest.TestCase):
    """The real Hermes worker, bridge, and provider receipt route with fake Hermes tools."""
    setUp = provider_fixtures.ProviderTests.setUp
    tearDown = provider_fixtures.ProviderTests.tearDown
    fake_hermes = provider_fixtures.ProviderTests.fake_hermes

    BEHAVIOR = """
        import json, tools.approval as approval
        ran = []
        def terminal(args):
            ran.append(args['command'])
            for callback in list(approval.callbacks.values()):
                callback({'request_id': 'r1', 'command': args['command'], 'description': 'Delete build output'})
            return json.dumps({'output': 'ok', 'exit_code': 0})
        registry.handlers['terminal'] = terminal
        registry.handlers['kanban_complete'] = lambda args: ran.append('kanban_complete') or json.dumps({'ok': True})
        assert ('Independent technical work' in prompt) == LANE
        assert ('Do not change product code.' in prompt) == LANE
        if LANE:
            held = json.loads(registry.dispatch('kanban_complete', {'summary': 'Fixed.'}))
            assert held['error'].startswith('Independent technical work holds this native action.'), held
            assert ran == []
        tested = json.loads(registry.dispatch('terminal', {'command': 'rm -rf tmp/build && claude -p "Run the tests"'}))
        assert tested['exit_code'] == 0 and ran == ['rm -rf tmp/build && claude -p "Run the tests"']
        assert approval.resolutions == [('deny', 'The original decision remains pending.')], approval.resolutions
        return {'completed': True, 'final_response': 'Tests pass locally.'}
    """

    def run_worker(self, lane):
        from coconut.providers import HermesProvider
        provider = self.fake_hermes(self.BEHAVIOR.replace('LANE', repr(bool(lane))))
        (self.root / 'source' / 'tools' / 'approval.py').write_text(
            'callbacks = {}\nresolutions = []\n'
            'def register_gateway_notify(key, callback): callbacks[key] = callback\n'
            'def unregister_gateway_notify(key): callbacks.pop(key, None)\n'
            'def resolve_gateway_approval(key, choice, **kwargs):\n'
            '    resolutions.append((choice, kwargs.get("reason")))\n    return 1\n')
        provider.__class__ = HermesProvider  # Send receipts to the service route instead of acknowledging them.
        if lane:
            self.task['_technical_lane'] = dict(decision_id='gate', decision_revision=2, scope=['diagnosis', 'tests'],
                                                native_task_id='t_own')
        events = []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'native_call':
                # The same refusal that NativeExecution.native_event raises for this lane.
                reason = held_action(dict(name=data['name'], arguments=data['arguments']), 't_own') if lane else None
                if reason:
                    raise ServiceError(403, 'Independent technical work holds this native action. ' + reason)
                return {'status': 'recorded', 'id': str(uuid4())}
            if kind == 'native_result':
                return {'status': 'recorded'}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        return events

    def test_worker_holds_native_actions_and_new_permissions_but_runs_local_tools(self):
        events = self.run_worker(lane=True)
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[-1], 'completed', events)
        self.assertNotIn('native_approval', kinds)
        self.assertNotIn('failed', kinds)

    def test_without_the_lane_the_same_dangerous_command_requests_approval(self):
        # Negative control: the denial above comes from the lane, not from the fake approval module.
        events = self.run_worker(lane=False)
        self.assertIn('native_approval', [kind for kind, _ in events], events)


@unittest.skipUnless(native_claims.fixtures.SOURCE and native_claims.fixtures.PYTHON,
                     'Set Coconut Hermes paths for native integration checks.')
class TechnicalLaneNativeBoardTests(unittest.TestCase):
    """The pinned Hermes kernel: a real review card, native claim, run, finish, and service tool route."""
    setUp = native_claims.NativeClaimContinuationTests.setUp
    write = native_claims.NativeClaimContinuationTests.write
    native_program = native_claims.NativeClaimContinuationTests.native_program
    prepare = native_claims.NativeClaimContinuationTests.prepare

    def native_status(self, identity):
        return self.native_program('print(board.kb.get_task(board.conn, sys.argv[2]).status)', identity)

    def run_lane(self, answer_while_running=False):
        owner, source, identity, workspace = self.prepare('review')
        owner.update(status='needs_you', summary='Accept the views and mark the PR ready after checks?')
        self.service._put('tasks', owner)
        approved = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], status='approved',
            action_type='question', inbox_kind='decision', revision=1, title='Build the complete list?', context='',
            options=[dict(id='all', label='Build the complete list', answer='approve', explanation='')],
            selected_option_id='all', selected_option_label='Build the complete list', decided_by='test-phone',
            decided_at=owner['created_at'], created_at=owner['created_at'], _native=True,
            _answers=[dict(choice='approve', option_id='all', decided_by='test-phone')])
        gate = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], status='pending',
            action_type='question', inbox_kind='decision', revision=2, title=owner['summary'], explanation='',
            context='', resource='', options=[dict(id='accept', label='Accept the views', answer='approve', explanation=''),
                                             dict(id='draft', label='Keep the PR in draft', answer='approve', explanation='')],
            recommended_option_id=None, selected_option_id=None, selected_option_label=None, decided_at=None,
            decided_by=None, decided_by_name=None, correction=None, outcome=None, created_at=owner['created_at'],
            merge_pr_url=None, _native=True, _answers=[dict(choice='revise', correction='Findings changed.', decided_by=None)])
        for decision in (approved, gate):
            self.service._put('decisions', decision)
        runs = self.native_program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", '
                                   '(sys.argv[2],)).fetchone()[0])', identity)
        receipt = self.service.emit(source['id'], 'work_continue', dict(task_id=identity,
            message='Fix the CI findings locally. Keep the views pending.', independent_technical_work=dict(
                decision_id=gate['id'], decision_revision=2, scope=['diagnosis', 'tests', 'local_fixes'],
                authorization_decision_id=approved['id'])))
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertEqual(self.service._get('decisions', gate['id']), gate)
        seen = {}
        def run(task, bot, history, emit):
            seen['lane'] = task.get('_technical_lane')
            seen['turn'] = task['_native_turn_id']
            seen['native'] = self.native_status(task['_native_outcome_id'])
            record = emit('native_call', {'name': 'terminal', 'arguments': {'command': 'bin/rspec'}})
            emit('native_result', {'id': record['id'], 'result': json.dumps({'exit_code': 0})})
            try:
                emit('native_call', {'name': 'kanban_complete', 'arguments': {'summary': 'Done.'}})
            except ServiceError as error:
                seen['complete'] = error.status
            if answer_while_running:
                self.service.mutate('POST', '/v1/decisions/' + gate['id'] + '/answer',
                                    {'option_id': 'accept', 'revision': 2}, str(uuid4()), 'test-phone')
                seen['answered'] = self.service._get('decisions', gate['id'])
                try:
                    emit('native_call', {'name': 'terminal', 'arguments': {'command': 'bin/rspec'}})
                except ServiceError as error:
                    seen['after_answer'] = error.status
            emit('completed', {'summary': 'Fixed the CI findings locally.'})
        self.provider.start_task = run
        self.service._dispatch_pending()
        worker = self.service.running.get(owner['id'])
        self.assertIsNotNone(worker, 'The lane must dispatch the real native claim of the same owner')
        worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(seen['native'], 'running')
        self.assertEqual(seen['lane']['decision_id'], gate['id'])
        self.assertEqual(seen['complete'], 403)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        self.service._drain_returns()
        current = self.service.kanban.tasks[owner['id']]
        self.assertEqual(current['id'], identity, 'The review card keeps its identity')
        self.assertEqual(current['workspace_path'], str(workspace))
        self.assertEqual(self.native_program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", '
                                             '(sys.argv[2],)).fetchone()[0])', identity), str(int(runs) + 1))
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']], [])
        self.assertEqual(self.service._get('decisions', approved['id']), approved)
        self.assertEqual(len(self.service._all('decisions')), 2)
        self.assertEqual((workspace / 'pending.txt').read_text(), 'Uncommitted correction')
        return owner, identity, gate, seen

    def test_a_lane_run_returns_the_real_card_to_review_with_its_decision_pending(self):
        owner, identity, gate, seen = self.run_lane()
        self.assertEqual(self.native_status(identity), 'review')
        finished = self.service._get('tasks', owner['id'])
        self.assertEqual((finished['status'], finished['summary']), ('needs_you', gate['title']))
        self.assertEqual(self.service._get('decisions', gate['id']), gate)

    def test_an_answer_during_the_lane_run_ends_the_lane_without_worker_changes(self):
        owner, identity, gate, seen = self.run_lane(answer_while_running=True)
        self.assertEqual(seen['after_answer'], 409)
        answered = self.service._get('decisions', gate['id'])
        self.assertEqual(answered, seen['answered'])
        self.assertEqual((answered['status'], answered['selected_option_id'], answered['decided_by']),
                         ('approved', 'accept', 'test-phone'))
        resumed = self.service._get('tasks', owner['id'])
        self.assertEqual((resumed['status'], resumed['_resume_decision']), ('queued', gate['id']))
        self.assertIsNone(self.service._technical_lane(resumed))
        self.assertNotIn(self.native_status(identity), ('done', 'archived'))
        # The next real claim resumes the answered decision in a new turn without the lane's results.
        captured = {}
        runs = int(self.native_program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", '
                                       '(sys.argv[2],)).fetchone()[0])', identity))
        def resume(task, bot, history, decision, emit):
            captured.update(task=copy.deepcopy(task), decision=copy.deepcopy(decision))
            self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Resumed result verified', expected_run_id=int(sys.argv[3]))",
                                task['_native_outcome_id'], str(self.service._get('tasks', task['id'])['_kanban_claim']['run_id']))
            emit('completed', {'summary': 'Resumed result verified'})
        self.provider.resume_task = resume
        self.service._dispatch_pending()
        worker = self.service.running.get(owner['id'])
        self.assertIsNotNone(worker, 'The answered decision must dispatch through the native claim')
        worker.join(15)
        self.assertFalse(worker.is_alive())
        for _ in range(2):
            # The real result route, repeated: one completed card, one run, and one return to the requester.
            self.service.kanban.call({'op': 'snapshot'})
            self.service._reconcile_kanban()
            self.service._drain_returns()
        finished = self.service.kanban.tasks[owner['id']]
        self.assertEqual((finished['id'], finished['status']), (identity, 'done'))
        self.assertEqual(self.native_program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", '
                                             '(sys.argv[2],)).fetchone()[0])', identity), str(runs + 1))
        returned = [d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']]
        self.assertEqual([(d['target_bot_id'], d['text']) for d in returned], [(owner['source_bot_id'], 'Resumed result verified')])
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        lane_receipts = {r['id'] for r in self.service._native_task_receipts(owner['id']) if r['turn_id'] == seen['turn']}
        self.assertTrue(lane_receipts)
        self.assertNotEqual(captured['task']['_native_turn_id'], seen['turn'])
        self.assertFalse(lane_receipts & {r['id'] for r in captured['task']['_native_receipts']})
        self.assertEqual((captured['decision']['id'], captured['decision']['selected_option_id']), (gate['id'], 'accept'))


if __name__ == '__main__':
    unittest.main()
