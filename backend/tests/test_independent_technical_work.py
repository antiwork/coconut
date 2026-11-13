"""Independent technical work continues one execution while its exact decision stays pending."""
import copy
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut.technical_lane import CONTINUE_SCHEMA
from coconut.service import ServiceError, YieldForDecision
from coconut.technical_lane import SYSTEM_DECIDERS, held_action, instructions
import test_native_claim_continuation as native_claims
import test_native_work_continuation as continuation
from test_work_progress import progress
from hermes_env import needs_hermes

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
        # This gate already has an assessment for the current native profile.
        self.gate = dict(id='gate', task_id=owner['id'], bot_id=owner['bot_id'], status='pending',
            action_type='question', inbox_kind='decision', revision=2, title=owner['summary'],
            _profile_revision=self.service.profiles.prompt_revision(self.service._get('bots', owner['bot_id'])),
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

    def refused(self, owner, source, pattern, work=None, **changes):
        with self.assertRaisesRegex(ServiceError, pattern):
            self.lane(source, work, **changes)
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
        declined = lane['properties']['declined_merge_pr_url']
        self.assertEqual((declined['type'], declined['maxLength']), ('string', 500))
        self.assertIn('declined_merge_pr_url', lane['description'])

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
                       dict(inbox_kind='proposal'), dict(status='resolved', resolution='source_changed'),
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
        self.refused(owner, source, 'Only the owner or its requesting agent')
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
        return captured

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

    def assert_resumed_from_the_gate(self, owner, lane_turns, captured, pre_lane, expect=('approved', 'accept')):
        lane_turns = [lane_turns] if isinstance(lane_turns, str) else list(lane_turns)
        task = captured['task']
        self.assertNotIn(task['_native_turn_id'], [pre_lane['turn_id'], *lane_turns])
        # Only the decision turn's completed effects are carried. The lane's results are history, never this turn's.
        self.assertEqual([r['id'] for r in task['_native_receipts']], ['pre-lane'])
        from coconut.native_receipt_context import receipt_prompt
        self.assertIn('"total": 1', receipt_prompt(task['_native_receipts']))
        lane = [r for turn in lane_turns for r in self.lane_receipts(owner, turn)]
        self.assertTrue(all(self.lane_receipts(owner, turn) for turn in lane_turns), 'Every lane turn ran a tool')
        # No output of any lane turn is an input or a cached result of the resumed decision turn.
        self.assertFalse({r['id'] for r in lane} & {r['id'] for r in task['_native_receipts']})
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
        captured = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, lane_turn, captured, pre_lane)
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
        # The same held-in-flight state that the real-kernel case asserts in CI.
        self.assertEqual(self.service._technical_lane(answered)['turn_id'], lane_turn)
        self.assertIsNone(self.service._technical_gate(answered)[0])
        captured = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, lane_turn, captured, pre_lane)

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
        captured = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, turns, captured, pre_lane)

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
        captured = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, turns, captured, pre_lane)

    def test_an_answer_during_a_follow_up_lane_turn_resumes_the_gate(self):
        owner, source = self.lane_native()
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        self.drive(owner, steer='Also run the request specs.')
        turns = self.lane_turns(owner)
        running = self.drive(owner, tools=2, answer='after_tool')
        self.assertEqual(running['refused'], [409])
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'queued')
        captured = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, turns, captured, pre_lane)

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
            self.service._all('native_receipts'), self.service._all('artifacts'), profile_revision=self.service.profiles.prompt_revision(self.service._get('bots', decision['bot_id'])))
        context = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], decision_id='gate', status='working',
                       question='Check the pending question.', created_at=task['created_at'], _decision_refresh=True,
                       _decision_revision=decision['revision'], _evidence_fingerprint=fingerprint,
                       _conversation_epoch=task.get('_conversation_epoch'),
                       _human_input_ids=[m['id'] for m in self.service._decision_human_messages(task, decision)])
        self.service._put('contexts', context)
        self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The findings were superseded.'))
        self.service._put('contexts', dict(self.service._get('contexts', context['id']), status='completed'))
        return self.service._get('decisions', 'gate')

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
        captured = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, [lane_turn], captured, pre_lane, expect=('resolved', None))
        self.assertEqual(captured['decision']['resolution_context'], withdrawn['resolution_context'])

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
        captured = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, [lane_turn], captured, pre_lane, expect=('resolved', None))
        self.assertEqual(captured['decision']['resolution_context'], withdrawn['resolution_context'])

    def test_an_old_lane_worker_failing_after_an_answer_keeps_the_answer_for_the_next_turn(self):
        owner, source = self.lane_native()
        pre_lane = self.gate_turn_receipt(owner)
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        def start_task(task, bot, history, emit):
            record = emit('native_call', {'name': 'terminal', 'arguments': {'command': 'bin/rspec'}})
            emit('native_result', {'id': record['id'], 'result': json.dumps({'output': 'passes'})})
            self.answer_gate()
            raise RuntimeError('The old lane worker stopped after the answer.')
        self.service.provider.start_task = start_task
        self.service._run_task(owner['id'])
        gate = self.service._get('decisions', 'gate')
        self.assertEqual((gate['status'], gate['selected_option_id'], gate['decided_by']), ('approved', 'accept', 'test-phone'))
        self.assertNotIn('Work stopped', gate.get('outcome') or '')
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual((queued['status'], queued['_resume_decision'], queued['_native_turn_id']), ('queued', 'gate', lane_turn))
        self.assertNotIn('_return_pending', queued)
        captured = self.resume_after_gate(owner)
        self.assert_resumed_from_the_gate(owner, [lane_turn], captured, pre_lane)

    def test_a_resumed_turn_that_fails_still_fails_its_own_decision(self):
        owner, source = self.lane_native()
        self.answer_gate()
        resumed = self.service._get('tasks', owner['id'])
        self.assertEqual((resumed['status'], resumed['_resume_decision']), ('queued', 'gate'))
        self.assertNotIn('_technical_lane', resumed)
        def resume_task(task, bot, history, decision, emit):
            raise RuntimeError('The resumed worker stopped.')
        self.service.provider.resume_task = resume_task
        self.service._run_task(owner['id'])
        gate = self.service._get('decisions', 'gate')
        self.assertEqual((gate['status'], gate['outcome']), ('failed', 'Work stopped: The resumed worker stopped.'))
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'failed')

    def requester_branch(self, owner, source, **changes):
        """Another branch of the requester, delegated through the real bot-message route."""
        docs = self.write('/v1/bots', {'name': 'Docs'})
        with self.service.transaction():
            receipt = self.service._store_bot_message(source['id'], {'target_bot_id': docs['id'], 'text': 'Check the docs.'})
        sibling = dict(self.service._get('tasks', receipt['task_id']), **changes)
        self.service._put('tasks', sibling)
        return sibling

    def test_direct_delegation_keeps_the_requesters_other_work_in_the_guard(self):
        cases = {
            'an active sibling branch': lambda owner, source: self.requester_branch(owner, source),
            'a pending requester decision': lambda owner, source: self.service._put('decisions', dict(
                id='requester-question', task_id=source['id'], bot_id=source['bot_id'], status='pending',
                action_type='question', inbox_kind='decision', revision=1, title='Which list?')),
            'an unknown requester tool result': lambda owner, source: self.service._put('native_receipts', dict(
                id='requester-unknown', task_id=source['id'], turn_id='requester-turn', state='executing',
                action={'name': 'terminal', 'arguments': {'command': 'gh pr view'}}, action_hash='x', created_at=source['created_at'])),
            'an undelivered requester return': lambda owner, source: self.service._put('tasks', dict(
                self.service._get('tasks', source['id']), _return_pending={'target_bot_id': 'someone', 'text': 'Result'})),
            'a requester wait on other work': lambda owner, source: self.service._put('tasks', dict(
                self.service._get('tasks', source['id']), _waiting_on=['other-task'])),
            'an unfinished sibling return': lambda owner, source: self.requester_branch(
                owner, source, status='completed', _return_pending={'target_bot_id': source['bot_id'], 'text': 'Result'}),
            # Only the requester chain finds these: a return callback of the requester's assignment, not in its waits.
            'a queued requester callback': lambda owner, source: self.requester_callback(source),
            'a requester callback with an unknown tool result': lambda owner, source: self.service._put('native_receipts', dict(
                id='callback-unknown', task_id=self.requester_callback(source, status='completed')['id'], turn_id='callback-turn',
                state='executing', action={'name': 'terminal', 'arguments': {'command': 'make'}}, action_hash='x',
                created_at=source['created_at'])),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                try:
                    owner, source, items = self.retained_pr_owner()
                    change(owner, source)
                    before = self.service._all('artifacts')
                    self.refused(owner, source, 'another execution')
                    self.assertEqual(self.service._all('artifacts'), before)
                finally:
                    # Each case needs a fresh store: an admitted case would replay its saved receipt otherwise.
                    self.tearDown()
                    self.setUp()

    def requester_callback(self, requester, status='queued'):
        """A return callback of the requester's assignment. The work chain links it through _assignment_task_id."""
        requester = self.service._get('tasks', requester['id'])
        callback = dict(copy.deepcopy(requester), id=str(uuid4()), status=status, _is_return=True,
                        _assignment_task_id=requester['id'], _waiting_on=[])
        callback['_execution_session_id'] = callback['id']
        for key in ('_native_outcome_id', '_kanban_claim', '_work_context_id'):
            callback.pop(key, None)
        self.service._put('tasks', callback)
        return callback

    def test_direct_delegation_still_admits_a_requester_that_waits_for_this_owner(self):
        owner, source, items = self.retained_pr_owner()
        # A finished sibling branch and a requester that waits only for this owner do not hold the continuation.
        self.requester_branch(owner, source, status='completed')
        self.service._put('tasks', dict(self.service._get('tasks', source['id']), _waiting_on=[owner['id']]))
        self.assertEqual(self.lane(source)['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['_work_context_id'], 'open-pr')

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

    def test_lane_turn_through_the_gateway_keeps_the_holds_of_the_worker(self):
        # The lane runs in the gateway, in the chat of its work session, with the lane rules. Its Coconut tools keep
        # the holds of a worker lane: no card, no wait, no lifecycle tool. A note on its own card works. An approval
        # request of Hermes gets a denial at once, and no card.
        from coconut.gateway_chat import LANE_QUESTION
        owner, source = self.lane_native()
        self.lane(source)
        stored = self.service._get('tasks', owner['id'])
        stored['_kanban_claim'] = dict(stored.get('_kanban_claim') or {}, native_id='t_existing', run_id=4,
                                       claim_lock='host:9', context='Card body: fix the subscription list.')
        self.service._put('tasks', stored)
        snapshot = self.service.kanban.call.side_effect
        tools = []
        def call(request):
            if request.get('op') == 'tool':
                tools.append((request['name'], request.get('claim')))
                return {'result': json.dumps({'ok': True})}
            return snapshot(request)
        self.service.kanban.call.side_effect = call
        seen, refused = {}, {}
        bridge = self.service.gateway_bridge
        home = self.service.profiles.home(self.service._get('bots', owner['bot_id']))
        def run_turn(task_id, home_, chat_id, text, emit, **options):
            seen.update(text=text, chat=chat_id, mode=options.get('mode'))
            turn = bridge.open_turn(task_id, home_, chat_id)
            def tool(name, **arguments):
                try:
                    return self.service.gateway_tool({'turn_id': turn, 'tool': name, 'arguments': arguments})
                except ServiceError as error:
                    refused[name] = error.status
            for name, arguments in (('ask', dict(title='Mark the PR ready?', options=[])), ('wait', dict(task_id='t_desk')),
                                    ('suggest', dict(key='ready', title='Mark ready?')),
                                    ('resolve_reply', dict(choice='approve', decision_id='gate')),
                                    ('kanban_complete', dict(summary='Fixed.')),
                                    ('kanban_comment', dict(task_id='t_desk', body='Ready to merge')),
                                    ('message_bot', dict(bot=source['bot_id'], text='Merge it.')),
                                    ('track_pr', dict(url=URL))):
                tool(name, **arguments)
            seen['note'] = tool('kanban_comment', body='Fixed the failing spec locally.')
            # As the gateway reports a command approval of Hermes (TurnEvents).
            emit('gateway_question', dict(turn_id=turn, question_id='q1', question_kind='approval',
                                          text='Allow the deploy?', detail='bin/deploy', choices=[]))
            seen['answers'] = list(bridge.answers.get(os.path.realpath(home_), []))
            emit('text_delta', {'text': 'The spec passes locally.'})
            emit('completed', {'summary': 'Fixed locally.'})
        with patch.object(self.service, 'gateway_enabled', return_value=True), \
                patch.object(bridge, 'connected', return_value=True), patch.object(bridge, 'run_turn', run_turn):
            self.service._run_task(owner['id'])
        self.assertEqual(seen['mode'], 'technical')
        self.assertTrue(seen['chat'].endswith(':work:' + owner['_execution_session_id']))
        lane = self.service._technical_lane(self.service._get('tasks', owner['id'])) or stored['_technical_lane']
        self.assertIn(instructions(lane).strip()[:120], seen['text'])
        self.assertEqual(refused, {'ask': 400, 'wait': 403, 'suggest': 400, 'resolve_reply': 403,
                                   'kanban_complete': 403, 'kanban_comment': 403, 'message_bot': 403, 'track_pr': 403})
        self.assertTrue(seen['note']['ok'])
        self.assertEqual(tools, [('kanban_comment', dict(native_id='t_existing', run_id=4, claim_lock='host:9'))])
        self.assertEqual(seen['answers'], [dict(question_id='q1', text=LANE_QUESTION, choice='deny')])
        # The lane turn returns the owner to its pending decision, as a worker lane turn does. No card changed.
        finished = self.service._get('tasks', owner['id'])
        self.assertEqual(finished['status'], 'needs_you')
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        self.assertEqual(len(self.service._all('decisions')), 3)
        self.assertEqual(self.service.provider.starts if hasattr(self.service.provider, 'starts') else [], [])

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

    def test_a_named_finished_pr_continues_the_lane_and_the_plain_call_still_refuses(self):
        # Both delegated PRs finished. The plain lane call refuses (see the next test). Naming one keeps that PR.
        owner, source, items = self.retained_pr_owner()
        self.service._put('artifacts', dict(items['open'], pr_state='merged', status='merged'))
        before = {a['id']: a for a in self.service._all('artifacts')}
        self.refused(owner, source, 'Name one of them with retained_pr_url')
        self.refused(owner, source, 'retained_pr_url names no finished delegated PR', retained_pr_url=URL[:-1] + '9')
        receipt = self.lane(source, retained_pr_url=URL[:-1] + '1')
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        self.assertEqual((queued['_work_context_id'], queued['_technical_lane']['decision_id']), ('open-pr', 'gate'))
        after = {a['id']: a for a in self.service._all('artifacts')}
        self.assertEqual(set(after), set(before), 'No new Work item')
        self.assertEqual(after['open-pr'], dict(before['open-pr'], _native_outcome_id='t_existing'))
        for identity in ('requester-work', 'requester-pr', 'merged-pr'):
            self.assertEqual(after[identity], before[identity])
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)

    def test_retained_pr_ambiguity_and_live_requester_writers_still_refuse(self):
        cases = {
            # Two open delegated PRs continue together (see the next test). A finished one among them stays ambiguous.
            'two open delegated PRs and a finished one': ('Choose one retained Work item', dict(
                merged=dict(pr_state='open', status='open'),
                open=dict(id='finished-pr', url=URL[:-1] + '2', pr_state='merged', status='merged'))),
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

    def test_two_open_delegated_prs_continue_together_under_the_lane(self):
        owner, source, items = self.retained_pr_owner()
        self.service._put('artifacts', dict(items['merged'], pr_state='open', status='open'))
        before = {a['id']: a for a in self.service._all('artifacts')}
        self.assertEqual(self.lane(source)['task_id'], owner['id'])
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual((queued.get('_work_context_id'), queued['_native_outcome_id']), (None, 't_existing'))
        after = {a['id']: a for a in self.service._all('artifacts')}
        self.assertEqual(set(after), set(before), 'No duplicate Work item')
        for identity in ('merged-pr', 'open-pr'):
            self.assertEqual(after[identity], dict(before[identity], _native_outcome_id='t_existing'))
        for identity in ('requester-work', 'requester-pr'):
            self.assertEqual(after[identity], before[identity])
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)


DECLINED_PR = 'https://github.com/antiwork/gumroad/pull/8661'
DECLINED_SCOPE = ['diagnosis', 'tests']


class DeclinedMergeLaneTests(unittest.TestCase):
    """A person declined one merge card, and its answer pauses the whole outcome. Only the explicit lane continues it."""
    setUp = continuation.NativeWorkContinuationTests.setUp
    tearDown = continuation.NativeWorkContinuationTests.tearDown
    write = continuation.NativeWorkContinuationTests.write
    prepare = continuation.NativeWorkContinuationTests.prepare
    snapshot_result = continuation.NativeWorkContinuationTests.snapshot_result
    scheduled_native = continuation.NativeWorkContinuationTests.scheduled_native

    def declined_native(self):
        """The saved state of the stopped outcome: a finished owner, a triage card, and a declined merge as its pause."""
        from coconut.service import now
        owner, source = self.scheduled_native()
        # The pause follows the messages that started the work, as on the Mac.
        self.paused_at = now()
        self.service.kanban.tasks[owner['id']].update(status='triage', block_kind='needs_input', title=owner['title'])
        self.service.kanban.observed_at = None
        owner.update(summary='Complete.', _native_outcome_id='t_existing', _work_context_id='paused-work',
                     _self_recovery=dict(attempt=0, decisions=[], previous_task_id=owner['id'], unknown_tools=[]),
                     _pr_review=dict(url=DECLINED_PR, pr_state='open', draft=False),
                     _block_check=dict(attempt=1, since=1.0, next_at=2.0))
        self.service._put('tasks', owner)
        # The fields of the live card: a native merge question, declined with no option, no text, and one answer.
        self.declined = dict(id='declined', task_id=owner['id'], bot_id=owner['bot_id'], status='rejected',
            action_type='question', inbox_kind='decision', revision=1, title='Approve the migration and merge #8661?',
            context='', explanation='', merge_pr_url=DECLINED_PR, _merge_pr_head='2d07701', _merge_request_at='',
            options=[dict(id='merge', label='Approve migration and merge #8661', answer='approve', explanation=''),
                     dict(id='leave_open', label='Leave #8661 open', answer='approve', explanation='')],
            selected_option_id=None, selected_option_label=None, correction='', decided_by='person-device',
            decided_at='2026-10-26T19:30:02Z', created_at='2026-10-26T17:28:08Z', _native=True, _receipt_id=None,
            outcome='Answer recorded. Hermes will continue.',
            _answers=[dict(choice='reject', option_id=None, correction='', predicted_option_id=None,
                           decided_by='person-device')])
        self.work = dict(id='paused-work', task_id=owner['id'], bot_id=owner['bot_id'], kind='work', title=owner['title'],
                         status='waiting', paused_by_decision_id='declined', _work_pause_at=self.paused_at,
                         created_at='2026-10-26T17:00:00Z', _native_outcome_id='t_existing')
        self.pr = dict(id='paused-pr', task_id=owner['id'], bot_id=owner['bot_id'], kind='pr', url=DECLINED_PR,
                       title='#8661', status='failed', pr_state='open', _review_task_id=owner['id'],
                       _activity_task_id=owner['id'], _activity_state='waiting', work_outcome_id='paused-work',
                       paused_by_decision_id='declined', _work_pause_at=self.paused_at, created_at='2026-10-26T17:00:00Z')
        # Another execution keeps its own human refusal and interrupted tool. The lane must not touch either.
        unrelated = dict(copy.deepcopy(source), id='unrelated', status='failed', _execution_session_id='unrelated')
        self.unrelated_rejected = dict(id='unrelated-rejected', task_id='unrelated', bot_id=source['bot_id'],
            status='rejected', action_type='question', inbox_kind='decision', revision=1, decided_by='person-device',
            merge_pr_url=DECLINED_PR, _native=True, selected_option_id=None,
            _answers=[dict(choice='reject', option_id=None, decided_by='person-device')])
        self.interrupted = dict(id='interrupted', task_id='unrelated', turn_id='old-turn', state='executing',
            action={'name': 'terminal', 'arguments': {'command': 'bin/rspec'}}, action_hash='old', created_at='2026-10-19')
        for table, record in (('decisions', self.declined), ('artifacts', self.work), ('artifacts', self.pr),
                              ('tasks', unrelated), ('decisions', self.unrelated_rejected),
                              ('native_receipts', self.interrupted)):
            self.service._put(table, record)
        owner = self.service._get('tasks', owner['id'])
        self.assertTrue(self.service._task_work_is_paused(owner))
        self.assertTrue(self.service._automatic_work_is_paused(owner))
        return owner, source

    def lane(self, source, work=None, **changes):
        lane = dict(decision_id='declined', decision_revision=1, scope=DECLINED_SCOPE, declined_merge_pr_url=DECLINED_PR)
        lane.update(work or {})
        payload = dict(task_id='t_existing', message='Diagnose the CI failure and run the specs locally. #8661 stays unmerged.',
                       independent_technical_work={k: v for k, v in lane.items() if v is not None})
        return self.service.emit(source['id'], 'work_continue', dict(payload, **changes))

    def assert_saved_refusal_kept(self):
        self.assertEqual(self.service._get('decisions', 'declined'), self.declined)
        self.assertEqual(self.service._get('artifacts', 'paused-work'), self.work)
        self.assertEqual(self.service._get('artifacts', 'paused-pr'), self.pr)
        self.assertEqual(self.service._get('decisions', 'unrelated-rejected'), self.unrelated_rejected)
        self.assertEqual(self.service._get('native_receipts', 'interrupted'), self.interrupted)

    def refused(self, owner, source, pattern, work=None, **changes):
        with self.assertRaisesRegex(ServiceError, pattern):
            self.lane(source, work, **changes)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def person_says(self, owner, text):
        """A person's own message in the owner's chat, as a paired device sends it."""
        self.service._request_device_id = 'test-phone'
        try:
            return self.service._message(owner['bot_id'], text, task_id=owner['id'], role='user', human_authored=True)
        finally:
            self.service._request_device_id = None

    def put_owner(self, owner, **changes):
        changed = dict(owner, **changes)
        self.service._put('tasks', changed)
        return changed

    def test_plain_continuation_and_the_pending_lane_still_refuse_the_paused_outcome(self):
        owner, source = self.declined_native()
        snapshot = copy.deepcopy(self.service.kanban.tasks)
        with self.assertRaisesRegex(ServiceError, 'This work is paused'):
            self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing', message='Continue.'))
        # The pending lane does not read past a pause, and it cannot name an answered card.
        self.refused(owner, source, 'This work is paused', dict(declined_merge_pr_url=None))
        self.assertEqual(self.service.kanban.tasks, snapshot)
        self.assert_saved_refusal_kept()

    def test_the_exact_declined_merge_admits_local_diagnosis_and_tests_without_changing_the_refusal(self):
        owner, source = self.declined_native()
        snapshot = copy.deepcopy(self.service.kanban.tasks)
        receipt = self.lane(source)
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        self.assertEqual(self.lane(source), receipt, 'The same request is idempotent')
        self.assertEqual(self.service._get('tasks', owner['id']), queued)
        for key in ('id', 'bot_id', 'session_id', '_execution_session_id', 'source_bot_id', 'source_task_id',
                    '_reply_to_task_id', 'thread_id', 'conversation_bot_id', 'exchange_id', '_kanban_claim',
                    '_native_outcome_id', '_work_context_id', '_block_check'):
            self.assertEqual(queued.get(key), owner.get(key), key)
        self.assertNotEqual(queued['_native_turn_id'], owner['_native_turn_id'])
        lane = queued['_technical_lane']
        self.assertEqual({k: lane[k] for k in ('decision_id', 'decision_revision', 'scope', 'declined_merge_pr_url',
                                               'native_task_id', 'requester_task_id', 'turn_id', 'hold', 'declined')},
                         dict(decision_id='declined', decision_revision=1, scope=DECLINED_SCOPE,
                              declined_merge_pr_url=DECLINED_PR, native_task_id='t_existing',
                              requester_task_id=source['id'], turn_id=queued['_native_turn_id'],
                              hold=dict(status='completed', summary='Complete.'),
                              declined=dict(decision_id='declined', title=self.declined['title'],
                                            merge_pr_url=DECLINED_PR, decided_at=self.declined['decided_at'])))
        self.assertNotIn('authorization', lane, 'A decline authorizes nothing')
        self.assertNotIn('_resume_decision', queued)
        self.assertNotIn('_return_pending', queued)
        self.assert_saved_refusal_kept()
        self.assertEqual(self.service.kanban.tasks, snapshot)
        # The pause still holds the outcome. Only this lane turn runs past it.
        self.assertTrue(self.service._task_work_is_paused(queued))
        self.assertFalse(self.service._automatic_work_is_paused(queued))
        self.assertTrue(self.service._automatic_work_is_paused(dict(queued, _technical_lane=None)))
        rows = [row for row in self.service._kanban_rows() if row['execution_id'] == owner['_execution_session_id']]
        self.assertEqual([(row['id'], row['state']) for row in rows], [(owner['id'], 'ready')])
        text = instructions(lane)
        for phrase in ('declined this recorded decision', self.declined['title'], DECLINED_PR, 'Do not change product code.',
                       'Do not push, publish', 'until the person gives new direction', 'returns this outcome to its pause'):
            self.assertIn(phrase, text)
        self.assertNotIn('stays pending', text)

    def test_the_lane_refuses_a_wrong_identity_or_malformed_request(self):
        owner, source = self.declined_native()
        for work in (dict(decision_id='unrelated-rejected'), dict(decision_id='missing'), dict(decision_revision=0),
                     dict(decision_revision=2), dict(declined_merge_pr_url='https://github.com/antiwork/gumroad/pull/8662'),
                     dict(declined_merge_pr_url=DECLINED_PR + '/files')):
            with self.subTest(work=work):
                self.refused(owner, source, 'declined merge changed', work)
        for work in (dict(declined_merge_pr_url=''), dict(declined_merge_pr_url=8661),
                     dict(declined_merge_pr_url='https://x/' + 'a' * 500), dict(scope=['merge']), dict(permissions=['merge']),
                     dict(scope=['tests'], authorization_decision_id='declined')):
            with self.subTest(work=work):
                self.refused(owner, source, 'exact pending decision ID', work)
        self.assert_saved_refusal_kept()

    def test_the_lane_refuses_a_record_that_is_not_a_persons_plain_merge_decline(self):
        owner, source = self.declined_native()
        answer = self.declined['_answers'][0]
        variants = {
            'product choice': dict(merge_pr_url=None),
            'tool refusal': dict(action_type='native_approval'),
            'draft': dict(action_type='draft_message', draft={'body': 'Merge it'}),
            'proposal': dict(inbox_kind='proposal'),
            'tool question': dict(_receipt_id='call'),
            'closed for its source': dict(status='resolved', resolution='source_changed'),
            'legacy': dict(_native=False),
            'hidden': dict(_hidden=True),
            'approved': dict(status='approved', selected_option_id='merge',
                             _answers=[dict(answer, choice='approve', option_id='merge')]),
            'pending': dict(status='pending', decided_by=None, _answers=[]),
            'dismissed in undo': dict(status='dismissed'),
            'modified': dict(status='modified', correction='Split the migration.'),
            'chosen option': dict(selected_option_id='leave_open', _answers=[dict(answer, option_id='leave_open')]),
            'correction': dict(correction='Do not touch the migration.'),
            'answer text': dict(_answers=[dict(answer, correction='Stop all work on this.')]),
            'explicit stop': dict(decided_by='cancellation', decided_by_name='Task cancellation'),
            'archived': dict(decided_by='archive'),
            'unsigned': dict(decided_by=None),
            'no human answer': dict(_answers=[dict(answer, decided_by=None)]),
            'two answers': dict(_answers=[dict(answer, choice='approve', option_id='merge'), answer]),
            'other person': dict(_answers=[dict(answer, decided_by='someone-else')]),
            'unknown choice': dict(_answers=[dict(answer, choice='stop')]),
        }
        for name, change in variants.items():
            with self.subTest(record=name):
                self.service._put('decisions', dict(self.declined, **change))
                self.refused(owner, source, 'declined merge changed|This work is paused')
        self.service._put('decisions', self.declined)
        self.assert_saved_refusal_kept()
        self.lane(source)  # The unchanged record is admitted, so each refusal above came from its one change.

    def test_the_lane_refuses_other_refusals_pauses_peers_and_unknown_effects(self):
        owner, source = self.declined_native()
        cases = [
            ('rejected fix', 'declined another part', 'decisions', dict(id='fix', task_id=owner['id'], status='rejected',
                action_type='question', revision=1, decided_by='person-device', _native=True)),
            ('rejected tool', 'declined another part', 'decisions', dict(id='tool', task_id=owner['id'], status='rejected',
                action_type='native_approval', revision=1, decided_by='person-device')),
            ('pending peer', 'decision must be answered', 'decisions', dict(id='peer', task_id=owner['id'],
                status='pending', action_type='question', inbox_kind='decision', revision=1)),
            ('active request', 'active request', 'contexts', dict(id='reply', task_id=owner['id'], bot_id=owner['bot_id'],
                decision_id='declined', status='queued')),
            ('unknown effect', 'no confirmed result', 'native_receipts', dict(id='unknown', task_id=owner['id'],
                turn_id=owner['_native_turn_id'], state='executing', action_hash='x', created_at='2026-10-26',
                action={'name': 'terminal', 'arguments': {'command': 'git push'}})),
        ]
        for name, pattern, table, record in cases:
            with self.subTest(case=name):
                self.service._put(table, dict(record, bot_id=owner['bot_id']))
                self.refused(owner, source, pattern)
                self.service.db.execute('DELETE FROM ' + table + ' WHERE id=?', (record['id'],))
        recovering = self.put_owner(owner, _self_recovery=dict(owner['_self_recovery'], unknown_tools=[dict(id='old-call')]))
        self.refused(recovering, source, 'no confirmed result')
        self.service._put('tasks', owner)
        # An unrelated answered pause on one peer, or no pause at all, refuses. So does a person's new direction.
        other = dict(id='other-pause', task_id=owner['id'], bot_id=owner['bot_id'], status='approved', action_type='question',
                     revision=1, decided_by='person-device', decided_at=self.paused_at, _native=True)
        self.service._put('decisions', other)
        self.service._put('artifacts', dict(self.pr, paused_by_decision_id='other-pause'))
        self.refused(owner, source, 'declined another part|paused by another decision')
        self.service.db.execute("DELETE FROM decisions WHERE id='other-pause'")
        for change in (dict(paused_by_decision_id=None), dict(paused_by_decision_id='missing')):
            self.service._put('artifacts', dict(self.pr, **change))
            self.service._put('artifacts', dict(self.work, **change))
            with self.subTest(pause=change):
                self.refused(owner, source, 'paused by another decision')
        self.service._put('artifacts', self.pr)
        self.service._put('artifacts', self.work)
        self.person_says(owner, 'Merge it after all.')
        self.assertFalse(self.service._automatic_work_is_paused(owner), 'New direction ends the automatic pause')
        self.refused(owner, source, 'has new direction')
        self.assert_saved_refusal_kept()

    def test_each_refusal_above_starts_from_an_admissible_state(self):
        owner, source = self.declined_native()
        self.assertEqual(self.lane(source)['status'], 'queued')

    def test_the_lane_refuses_a_stopped_foreign_active_or_open_owner(self):
        owner, source = self.declined_native()
        for change in (dict(status='needs_you'), dict(status='cancelled'), dict(status='waiting'),
                       dict(_waiting_on=['other']), dict(_return_pending=dict(text='x'))):
            with self.subTest(owner=change):
                self.refused(self.put_owner(owner, **change), source, 'paused|Wait|finished owner|previous result')
        for change in (dict(_conversation_epoch='earlier'), dict(session_id='earlier-session')):
            with self.subTest(owner=change):
                self.refused(self.put_owner(owner, **change), source, 'earlier conversation')
        self.service._put('tasks', owner)
        native = self.service.kanban.tasks[owner['id']]
        for change in (dict(status='running', current_run_id=7), dict(status='blocked', worker_pid=123),
                       dict(status='review'), dict(status='done'), dict(status='archived'), dict(status='ready')):
            saved = dict(native)
            native.update(change)
            with self.subTest(native=change), self.assertRaises(ServiceError):
                self.lane(source)
            self.assertEqual(self.service._get('tasks', owner['id']), owner)
            native.clear()
            native.update(saved)
        self.service.running[owner['id']] = Mock(is_alive=lambda: True)
        self.refused(owner, source, 'finished owner')
        self.service.running.clear()
        exchange = self.service._get('exchanges', owner['exchange_id'])
        self.service._put('exchanges', dict(exchange, cancelled=True))
        self.refused(owner, source, 'cancelled')
        self.service._put('exchanges', exchange)
        ops = self.write('/v1/bots', {'name': 'Ops'})
        self.service._put('tasks', dict(source, bot_id=ops['id']))
        self.refused(owner, source, 'Only the owner or its requesting agent')
        self.service._put('tasks', source)

    def test_local_fixes_still_need_a_separate_approval_and_the_decline_never_authorizes(self):
        owner, source = self.declined_native()
        for authorization in ('declined', 'unrelated-rejected', 'missing'):
            with self.subTest(authorization=authorization):
                self.refused(owner, source, 'needs authorization_decision_id',
                             dict(scope=SCOPE, authorization_decision_id=authorization))
        with self.assertRaisesRegex(ServiceError, 'needs authorization_decision_id'):
            self.lane(source, dict(scope=SCOPE))
        approved = dict(id='fix-scope', task_id=owner['id'], bot_id=owner['bot_id'], status='approved', action_type='question',
            inbox_kind='decision', revision=1, title='Fix the failing subscription spec?', context='',
            options=[dict(id='fix', label='Fix the spec only', answer='approve', explanation='No migration changes.')],
            selected_option_id='fix', selected_option_label='Fix the spec only', decided_by='person-device',
            decided_at='2026-10-26T00:00:00Z', _native=True, _answers=[dict(choice='approve', option_id='fix', decided_by='person-device')])
        self.assertLess(approved['decided_at'], self.paused_at, 'The fix scope was approved before the decline paused the work')
        # An approval after the pause is the person's new direction. It ends the declined gate instead of extending it.
        self.service._put('decisions', dict(approved, decided_at='2999-01-01T00:00:00Z'))
        self.refused(owner, source, 'has new direction', dict(scope=SCOPE, authorization_decision_id='fix-scope'))
        self.service._put('decisions', approved)
        self.lane(source, dict(scope=SCOPE, authorization_decision_id='fix-scope'))
        lane = self.service._get('tasks', owner['id'])['_technical_lane']
        self.assertEqual((lane['scope'], lane['authorization']['decision_id'], lane['authorization']['selected_option_id']),
                         (SCOPE, 'fix-scope', 'fix'))
        text = instructions(lane)
        self.assertIn('Fix the spec only', text)
        self.assertIn('Do not do anything that it asked the person to approve', text)
        self.assertEqual(self.service._get('decisions', 'fix-scope'), approved)
        self.assert_saved_refusal_kept()

    def drive(self, owner, end='completed', steer=None, during=None, unknown=False):
        """One real lane turn through _run_task: a local tool, held native actions, and the end of the turn."""
        seen = {}
        def start_task(task, bot, history, emit):
            seen['task'] = copy.deepcopy(task)
            record = emit('native_call', {'name': 'terminal', 'arguments': {'command': 'bin/rspec spec/models'}})
            if unknown:
                seen['unknown'] = record['id']
                return
            emit('native_result', {'id': record['id'], 'result': json.dumps({'exit_code': 0})})
            comment = emit('native_call', {'name': 'kanban_comment', 'arguments': {'body': 'Specs pass locally.'}})
            seen['comment'] = comment['status']
            emit('native_result', {'id': comment['id'], 'result': '{"ok": true}'})
            seen['held'] = {}
            for name, arguments in (('kanban_complete', {'summary': 'Done.'}), ('kanban_block', {'reason': 'Waiting'}),
                                    ('message_agent', {'target': 'desk', 'message': 'Merge #8661'})):
                try:
                    emit('native_call', {'name': name, 'arguments': arguments})
                except ServiceError as error:
                    seen['held'][name] = error.status
            seen['events'] = {}
            for kind, payload in (('decision_response', dict(choice='approve', decision_id='declined', decision_revision=1, quote='Yes')),
                                  ('native_question', dict(title='Merge #8661 now?', options=[dict(id='y', label='Yes', explanation='')])),
                                  ('artifact', dict(kind='pr', url=DECLINED_PR)), ('work_continue', dict(task_id='t_x', message='Merge.'))):
                try:
                    emit(kind, payload)
                except ServiceError as error:
                    seen['events'][kind] = error.status
            if steer:
                with self.service.transaction():
                    current = self.service._get('tasks', owner['id'])
                    self.service._steer_task(current, steer, [], current['conversation_bot_id'])
                    self.service._put('tasks', current)
            if during:
                during()
            emit('text_delta', {'text': 'The model specs pass locally.'})
            emit(end, {'summary': 'Diagnosed the CI failure locally.'})
        def resume_task(*args):
            raise AssertionError('A declined merge never resumes its decision.')
        self.service.provider.start_task, self.service.provider.resume_task = start_task, resume_task
        self.service._run_task(owner['id'])
        return seen

    def test_a_lane_turn_runs_with_its_holds_and_returns_to_the_pause_exactly_once(self):
        owner, source = self.declined_native()
        requester = self.service._get('tasks', source['id'])
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        seen = self.drive(owner)
        self.assertEqual(seen['task']['_technical_lane']['declined_merge_pr_url'], DECLINED_PR)
        self.assertEqual(seen['task']['_native_turn_id'], lane_turn)
        self.assertEqual(seen['comment'], 'recorded')
        self.assertEqual(seen['held'], dict(kanban_complete=403, kanban_block=403, message_agent=403))
        self.assertEqual(seen['events'], dict(decision_response=403, native_question=400, artifact=403, work_continue=403))
        finished = self.service._get('tasks', owner['id'])
        self.assertEqual((finished['status'], finished['summary'], finished['_native_turn_id']), ('completed', 'Complete.', lane_turn))
        for key in ('_resume_decision', '_return_pending', '_blocked_followup'):
            self.assertNotIn(key, finished)
        self.assertIn('The decline and its pause stay.', self.service._task_messages(owner['id'])[-1]['text'])
        self.assertEqual(self.service._get('tasks', source['id']), requester)
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']], [])
        self.assert_saved_refusal_kept()
        self.assertEqual(len(self.service._all('decisions')), 2, 'No decision was created or answered')
        # The finished lane turn parks its card. Nothing completes, retries, resumes, or settles the outcome.
        self.assertEqual(self.service._technical_native_state(finished), 'scheduled')
        self.assertTrue(self.service._automatic_work_is_paused(finished))
        for native_status in ('scheduled', 'ready'):
            self.service.kanban.tasks[owner['id']].update(status=native_status, current_run_id=None, worker_pid=None)
            for _ in range(2):
                rows = [r for r in self.service._kanban_rows() if r['id'] == owner['id']]
                self.service._native_retry(self.service._get('tasks', owner['id']), self.service.kanban.tasks[owner['id']])
            self.assertEqual([r['state'] for r in rows], ['done'])
            self.assertEqual(self.service._get('tasks', owner['id']), finished, native_status)
        # The settle pass keeps the card for the pause alone, without a block record or a held block.
        self.service.kanban.tasks[owner['id']].update(status='scheduled', block_kind=None)
        self.service._put('tasks', {k: v for k, v in finished.items() if k != '_block_check'})
        self.assertEqual(self.service._native_orphans(10 ** 9), [])
        self.service._put('tasks', finished)
        # The plain call still refuses. The person keeps the decision, and a new lane turn needs a new request.
        self.service._put('tasks', dict(requester, status='working'))
        with self.assertRaisesRegex(ServiceError, 'This work is paused'):
            self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing', message='Continue.'))
        self.service.kanban.tasks[owner['id']]['status'] = 'scheduled'
        self.lane(source, message='Run the request specs too.')
        again = self.service._get('tasks', owner['id'])
        self.assertEqual((again['status'], again['_technical_lane']['hold']['status']), ('queued', 'completed'))
        self.assertIn(lane_turn, again['_technical_lane']['turn_ids'])

    def test_a_failed_lane_turn_also_returns_to_the_pause_and_keeps_its_follow_up(self):
        owner, source = self.declined_native()
        self.lane(source)
        seen = self.drive(owner, end='failed', steer='Also check the migration spec locally.')
        failed = self.service._get('tasks', owner['id'])
        self.assertEqual(failed['status'], 'completed')
        self.assertTrue(failed['summary'].startswith('Complete.'))
        self.assertEqual(len(failed['_blocked_followup']['message_ids']), 1)
        self.assertNotIn('_resume_decision', failed)
        self.assertIn('Technical work stopped:', next(m['text'] for m in reversed(self.service._task_messages(owner['id']))
                                                      if m.get('kind') == 'activity'))
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']], [])
        self.assert_saved_refusal_kept()

    def test_a_completed_follow_up_runs_as_the_next_lane_turn_under_the_same_refusal(self):
        owner, source = self.declined_native()
        self.lane(source)
        first_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        self.drive(owner, steer='Also run the request specs.')
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(queued['status'], 'queued')
        self.assertNotEqual(queued['_native_turn_id'], first_turn)
        self.assertEqual(queued['_technical_lane']['turn_ids'], [first_turn, queued['_native_turn_id']])
        self.assertFalse(self.service._automatic_work_is_paused(queued))
        seen = self.drive(owner)
        self.assertEqual(seen['held']['kanban_complete'], 403)
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        self.assert_saved_refusal_kept()

    def test_new_direction_during_a_lane_turn_moves_the_owner_to_a_normal_turn_without_replaying_the_decline(self):
        owner, source = self.declined_native()
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        def direction():
            self.person_says(owner, 'Merge it after all.')
        seen = self.drive(owner, during=direction)
        self.assertEqual(seen['held']['kanban_complete'], 403, 'The lane turn keeps its holds')
        held = self.service._get('tasks', owner['id'])
        self.assertEqual((held['status'], held['_native_turn_id']), ('queued', lane_turn))
        self.assertNotIn('_resume_decision', held)
        self.service._kanban_rows()
        moved = self.service._get('tasks', owner['id'])
        self.assertNotEqual(moved['_native_turn_id'], lane_turn)
        self.assertNotIn('_technical_lane', moved)
        self.assertNotIn('_resume_decision', moved, 'Nothing replays the decline')
        self.assertIsNone(self.service._technical_native_state(moved))
        self.assert_saved_refusal_kept()

    def test_a_pause_that_ends_before_the_lane_turn_runs_leaves_the_lane_under_normal_rules(self):
        owner, source = self.declined_native()
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        # Another answered decision takes over the pause before dispatch. The lane ends; the new pause holds the owner.
        other = dict(id='other-pause', task_id=owner['id'], bot_id=owner['bot_id'], status='approved', action_type='question',
                     revision=1, decided_by='person-device', decided_at=self.paused_at, _native=True)
        self.service._put('decisions', other)
        for item in (self.work, self.pr):
            self.service._put('artifacts', dict(item, paused_by_decision_id='other-pause'))
        rows = [r for r in self.service._kanban_rows() if r['id'] == owner['id']]
        moved = self.service._get('tasks', owner['id'])
        self.assertNotEqual(moved['_native_turn_id'], lane_turn)
        self.assertNotIn('_technical_lane', moved)
        self.assertNotIn('_resume_decision', moved)
        self.assertEqual((moved['status'], [r['state'] for r in rows]), ('queued', ['scheduled']))
        self.assertTrue(self.service._automatic_work_is_paused(moved))
        self.assertFalse(self.service._dispatch_eligible(moved, 0))
        self.assertEqual(self.service._get('decisions', 'declined'), self.declined)

    def test_a_restart_during_a_lane_tool_returns_to_the_pause_and_keeps_the_unknown_tool(self):
        from coconut.service import Service
        owner, source = self.declined_native()
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        task = self.service._get('tasks', owner['id'])
        self.service._put('tasks', dict(task, status='working'))
        call = self.service.emit(owner['id'], 'native_call', dict(name='terminal', arguments={'command': 'bin/rspec'}))
        unknown = self.service._get('native_receipts', call['id'])
        native = copy.deepcopy(self.service.kanban.tasks)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service.kanban = Mock(tasks=native)
        self.service.kanban.call.side_effect = lambda request: self.snapshot_result()
        recovered = self.service._get('tasks', owner['id'])
        self.assertEqual((recovered['status'], recovered['_native_turn_id']), ('completed', lane_turn))
        self.assertNotIn('_resume_decision', recovered)
        self.assertEqual(self.service._get('native_receipts', call['id']), unknown)
        self.assertEqual(self.service._technical_native_state(recovered), 'scheduled')
        self.service._put('tasks', dict(self.service._get('tasks', source['id']), status='working'))
        with self.assertRaisesRegex(ServiceError, 'no confirmed result'):
            self.lane(source, message='Retry after the restart.')
        self.assert_saved_refusal_kept()

    def test_the_scheduler_parks_a_finished_lane_card_instead_of_completing_or_failing_it(self):
        owner, source = self.declined_native()
        self.lane(source)
        self.drive(owner)
        finished = self.service._get('tasks', owner['id'])
        claim = dict(finished['_kanban_claim'], run_id=41, generation='earlier', pid=None)
        self.service._put('tasks', dict(finished, _kanban_claim=claim, _kanban_started=41))
        self.service.kanban.tasks[owner['id']].update(status='running', current_run_id=41, worker_pid=999)
        requests = []
        self.service.kanban.call.side_effect = lambda request: requests.append(request) or self.snapshot_result()
        self.service._reconcile_kanban()
        finishes = [r for r in requests if r.get('op') == 'finish']
        self.assertEqual([(r['id'], r['run_id'], r['state']) for r in finishes], [('t_existing', 41, 'scheduled')])
        # Without the lane, the same finished owner asks for done, which Hermes books as a worker failure.
        self.assertEqual(self.service._technical_native_state(dict(finished, _technical_lane=None)), None)


HELD_PR = 'https://github.com/antiwork/gumroad/pull/8669'
HELD_LANE = dict(decision_id='held', decision_revision=1, scope=['diagnosis'], held_merge_pr_url=HELD_PR,
                 held_merge_option_id='leave_open')


class HeldMergeLaneTests(unittest.TestCase):
    """A person held a merge card open, and that answer pauses a completed outcome. Only read-only diagnosis continues."""
    setUp = continuation.NativeWorkContinuationTests.setUp
    tearDown = continuation.NativeWorkContinuationTests.tearDown
    write = continuation.NativeWorkContinuationTests.write
    prepare = continuation.NativeWorkContinuationTests.prepare
    snapshot_result = continuation.NativeWorkContinuationTests.snapshot_result
    scheduled_native = continuation.NativeWorkContinuationTests.scheduled_native
    drive = DeclinedMergeLaneTests.drive
    person_says = DeclinedMergeLaneTests.person_says
    put_owner = DeclinedMergeLaneTests.put_owner

    def held_native(self):
        """The saved state of the live outcome: a completed owner on a done card, its PR held open by the requester."""
        from coconut.service import now
        owner, source = self.scheduled_native()
        self.service.kanban.tasks[owner['id']].update(status='done', title=owner['title'], outcome_summary='Verified draft.')
        self.service.kanban.observed_at = None
        owner.update(summary='Verified the draft locally.', _pr_review=dict(url=HELD_PR, pr_state='open', draft=True))
        self.service._put('tasks', owner)
        # The requester's return callback of this owner asked the person, in its own execution, and finished.
        self.asker = dict(copy.deepcopy(source), id='asker', status='completed', source_task_id=owner['id'],
                          parent_task_id=owner['id'], _execution_session_id='asker-execution', _is_return=True,
                          _waiting_on=[])
        self.asker.pop('_return_pending', None)
        self.held = dict(id='held', task_id='asker', bot_id=source['bot_id'], status='executed', action_type='question',
            inbox_kind='decision', revision=1, title='Mark Gumroad #8669 ready and merge it?',
            context='Approval marks the draft ready and merges it into main.', explanation='', merge_pr_url=HELD_PR,
            options=[dict(id='merge', label='Mark #8669 ready and merge', answer='approve', explanation='', risk='high'),
                     dict(id='leave_open', label='Leave #8669 open', answer='approve',
                          explanation='Keep the verified draft available for further review.', risk='low')],
            selected_option_id='leave_open', selected_option_label='Leave #8669 open', correction='',
            decided_by='person-device', decided_at='2026-10-27T00:19:28Z', created_at='2026-10-27T00:10:00Z',
            _native=True, _receipt_id=None, _action={}, outcome='Hermes continued with your answer.',
            _answers=[dict(choice='approve', option_id='leave_open', correction='', predicted_option_id=None,
                           decided_by='person-device')])
        self.paused_at = now()
        self.pr = dict(id='held-pr', task_id=owner['id'], bot_id=owner['bot_id'], kind='pr', url=HELD_PR, title='#8669',
                       status='failed', pr_state='open', _review_task_id=owner['id'], _activity_state='ready',
                       paused_by_decision_id='held', _work_pause_at=self.paused_at, created_at='2026-10-27T00:00:00Z')
        unrelated = dict(copy.deepcopy(source), id='unrelated', status='failed', _execution_session_id='unrelated')
        self.unrelated_held = dict(self.held, id='unrelated-held', task_id='unrelated')
        for table, record in (('tasks', self.asker), ('decisions', self.held), ('artifacts', self.pr),
                              ('tasks', unrelated), ('decisions', self.unrelated_held)):
            self.service._put(table, record)
        owner = self.service._get('tasks', owner['id'])
        self.assertTrue(self.service._task_work_is_paused(owner))
        self.assertTrue(self.service._automatic_work_is_paused(owner))
        return owner, source

    def lane(self, source, work=None, **changes):
        lane = dict(HELD_LANE, **(work or {}))
        payload = dict(task_id='t_existing', message='Diagnose the new author delta read-only. #8669 stays open.',
                       independent_technical_work={k: v for k, v in lane.items() if v is not None})
        return self.service.emit(source['id'], 'work_continue', dict(payload, **changes))

    def refused(self, owner, source, pattern, work=None, **changes):
        with self.assertRaisesRegex(ServiceError, pattern):
            self.lane(source, work, **changes)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def assert_hold_kept(self):
        self.assertEqual(self.service._get('decisions', 'held'), self.held)
        self.assertEqual(self.service._get('decisions', 'unrelated-held'), self.unrelated_held)
        pr = self.service._get('artifacts', 'held-pr')
        self.assertEqual({k: v for k, v in pr.items() if k != '_native_outcome_id'}, self.pr)

    def test_plain_continuation_and_the_other_lanes_still_refuse_the_held_outcome(self):
        owner, source = self.held_native()
        with self.assertRaisesRegex(ServiceError, 'This work is paused'):
            self.service.emit(source['id'], 'work_continue', dict(task_id='t_existing', message='Continue.'))
        # The pending lane does not read past a pause, and the declined lane admits only a plain decline.
        plain = dict(held_merge_pr_url=None, held_merge_option_id=None)
        self.refused(owner, source, 'This work is paused', plain)
        self.refused(owner, source, 'declined merge changed|finished owner',
                     dict(plain, declined_merge_pr_url=HELD_PR, scope=['diagnosis', 'tests']))
        self.assert_hold_kept()
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'done')

    def test_the_exact_hold_admits_read_only_diagnosis_and_keeps_the_completed_card_hold_and_route(self):
        owner, source = self.held_native()
        snapshot = copy.deepcopy(self.service.kanban.tasks)
        receipt = self.lane(source)
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        self.assertEqual(self.lane(source), receipt, 'The same request is idempotent')
        self.assertEqual(self.service._get('tasks', owner['id']), queued)
        for key in ('id', 'bot_id', 'session_id', '_execution_session_id', 'source_bot_id', 'source_task_id',
                    '_reply_to_task_id', 'thread_id', 'conversation_bot_id', 'exchange_id', '_kanban_claim'):
            self.assertEqual(queued.get(key), owner.get(key), key)
        # The completed card stays as it is. New input on it owns the linked successor that the board creates.
        self.assertEqual(self.service.kanban.tasks, snapshot)
        self.assertEqual((queued['_native_outcome_id'], queued['_work_context_id']), ('t_existing', 'held-pr'))
        lane = queued['_technical_lane']
        self.assertEqual({k: lane[k] for k in ('decision_id', 'decision_revision', 'scope', 'held_merge_pr_url',
                                               'held_merge_option_id', 'native_task_id', 'hold')},
                         dict(HELD_LANE, native_task_id='t_existing', hold=dict(status='completed',
                                                                               summary='Verified the draft locally.')))
        self.assertEqual(lane['held'], dict(decision_id='held', revision=1, title=self.held['title'],
            context=self.held['context'], merge_pr_url=HELD_PR, decided_at=self.held['decided_at'],
            selected_option=dict(id='leave_open', label='Leave #8669 open', answer='approve',
                                 explanation='Keep the verified draft available for further review.'),
            options=[dict(id='merge', label='Mark #8669 ready and merge', answer='approve'),
                     dict(id='leave_open', label='Leave #8669 open', answer='approve')]))
        self.assertNotIn('authorization', lane, 'A hold authorizes nothing')
        self.assertNotIn('declined_merge_pr_url', lane)
        for key in ('_resume_decision', '_return_pending'):
            self.assertNotIn(key, queued)
        self.assert_hold_kept()
        self.assertEqual(self.service._get('artifacts', 'held-pr')['_native_outcome_id'], 't_existing')
        self.assertTrue(self.service._task_work_is_paused(queued))
        self.assertFalse(self.service._automatic_work_is_paused(queued))
        rows = [row for row in self.service._kanban_rows() if row['execution_id'] == owner['_execution_session_id']]
        self.assertEqual([(row['id'], row['state'], row['native_id']) for row in rows], [(owner['id'], 'ready', 't_existing')])
        text = instructions(lane)
        for phrase in ('answered this recorded merge decision', 'Leave #8669 open', 'Keep the verified draft available',
                       'Mark #8669 ready and merge', 'approves no merge, no ready, and no fix', 'read-only diagnosis',
                       'Do not edit, create, or delete any file', 'do not run tests, builds, migrations, or services',
                       'Do not push, publish', 'until the person gives new direction', 'returns this outcome to its hold'):
            self.assertIn(phrase, text)
        for phrase in ('stays pending', 'Fix only', 'Run the local tests'):
            self.assertNotIn(phrase, text)

    def test_the_lane_refuses_malformed_or_broader_requests(self):
        owner, source = self.held_native()
        for work in (dict(scope=['tests']), dict(scope=['diagnosis', 'tests']), dict(scope=['local_fixes']),
                     dict(scope=['diagnosis', 'local_fixes'], authorization_decision_id='held'),
                     dict(authorization_decision_id='held'), dict(held_merge_option_id=None), dict(held_merge_pr_url=None),
                     dict(declined_merge_pr_url=HELD_PR), dict(held_merge_pr_url=''), dict(held_merge_option_id=''),
                     dict(held_merge_pr_url=8669), dict(held_merge_pr_url='https://x/' + 'a' * 500),
                     dict(held_merge_option_id='x' * 101), dict(permissions=['merge'])):
            with self.subTest(work=work):
                self.refused(owner, source, 'exact pending decision ID', work)
        for work in (dict(decision_id='unrelated-held'), dict(decision_id='missing'), dict(decision_revision=0),
                     dict(decision_revision=2), dict(held_merge_pr_url='https://github.com/antiwork/gumroad/pull/8670'),
                     dict(held_merge_pr_url=HELD_PR + '/files'), dict(held_merge_option_id='merge'),
                     dict(held_merge_option_id='Leave #8669 open')):
            with self.subTest(work=work):
                self.refused(owner, source, 'held merge changed', work)
        self.assert_hold_kept()
        self.assertEqual(self.lane(source)['status'], 'queued', 'The unchanged request is admitted')

    def test_only_the_canonical_human_leave_open_answer_qualifies(self):
        owner, source = self.held_native()
        answer, options = self.held['_answers'][0], self.held['options']
        merge = dict(selected_option_id='merge', selected_option_label='Mark #8669 ready and merge',
                     _answers=[dict(answer, option_id='merge')])
        variants = {
            'merge chosen': merge, 'merge chosen and requested': dict(merge, _lane=dict(held_merge_option_id='merge')),
            'stop labelled leave_open': dict(options=[options[0], dict(options[1], label='Stop all work on #8669')],
                                             selected_option_label='Stop all work on #8669'),
            'other pr label': dict(options=[options[0], dict(options[1], label='Leave #8670 open')],
                                   selected_option_label='Leave #8670 open'),
            'saved label differs': dict(selected_option_label='Leave it open'),
            'product choice id': dict(options=[options[0], dict(options[1], id='keep_library_filter')],
                                      selected_option_id='keep_library_filter',
                                      _answers=[dict(answer, option_id='keep_library_filter')]),
            'duplicate option ids': dict(options=[options[1], options[1]]),
            'missing option': dict(options=[options[0]]),
            'rejecting option': dict(options=[options[0], dict(options[1], answer='reject')]),
            'option action': dict(options=[options[0], dict(options[1], action={'type': 'create_file'})]),
            'card action': dict(_action={'type': 'merge'}), 'proposal action': dict(_proposal_action={'type': 'x'}),
            'approved not executed': dict(status='approved'), 'pending': dict(status='pending', decided_by=None, _answers=[]),
            'rejected': dict(status='rejected', selected_option_id=None, selected_option_label=None,
                             _answers=[dict(answer, choice='reject', option_id=None)]),
            'replied': dict(status='replied', correction='Keep it open, and also stop.'),
            'modified': dict(status='modified', correction='Split it.'),
            'correction': dict(correction='Leave it open but stop engineering.'),
            'answer text': dict(_answers=[dict(answer, correction='Stop.')]),
            'reply answer': dict(_answers=[dict(answer, choice='reply')]),
            'draft': dict(draft={'body': 'Merge it'}), 'tool': dict(action_type='native_approval'),
            'proposal': dict(inbox_kind='proposal'), 'tool question': dict(_receipt_id='call'),
            'closed for its source': dict(status='resolved', resolution='source_changed'), 'legacy': dict(_native=False),
            'hidden': dict(_hidden=True), 'deleted': dict(_deleted=True),
            'explicit stop': dict(decided_by='cancellation'), 'archived': dict(decided_by='archive'),
            'unsigned': dict(decided_by=None), 'agent answer': dict(_answers=[dict(answer, decided_by=None)]),
            'two answers': dict(_answers=[answer, answer]),
            'agent revision too': dict(_answers=[dict(choice='revise', correction='Changed.', decided_by=None), answer]),
            'other person': dict(_answers=[dict(answer, decided_by='someone-else')]),
            'no merge url': dict(merge_pr_url=None), 'not a pr url': dict(merge_pr_url='https://github.com/antiwork/gumroad/issues/8669'),
            'another execution': dict(task_id='unrelated'),
        }
        for name, change in variants.items():
            with self.subTest(record=name):
                change = dict(change)
                work = change.pop('_lane', None)
                self.service._put('decisions', dict(self.held, **change))
                self.refused(owner, source, 'held merge changed|This work is paused', work)
        self.service._put('decisions', self.held)
        self.assert_hold_kept()
        self.assertEqual(self.lane(source)['status'], 'queued', 'The unchanged record is admitted')

    def test_the_lane_refuses_other_decisions_pauses_active_work_and_unknown_effects(self):
        owner, source = self.held_native()
        cases = [
            ('pending peer', 'pending', 'decisions', dict(id='peer', task_id=owner['id'], status='pending',
                action_type='question', inbox_kind='decision', revision=1)),
            ('pending asker question', 'pending', 'decisions', dict(id='peer', task_id='asker', status='pending',
                action_type='question', inbox_kind='decision', revision=1)),
            ('pending card on the same pr', 'pending', 'decisions', dict(id='peer', task_id='unrelated', status='pending',
                action_type='question', inbox_kind='decision', revision=1, merge_pr_url=HELD_PR.upper())),
            ('rejected fix', 'declined part', 'decisions', dict(id='peer', task_id=owner['id'], status='rejected',
                action_type='question', revision=1, decided_by='person-device', _native=True)),
            ('unknown owner tool', 'no confirmed result', 'native_receipts', dict(id='unknown', task_id=owner['id'],
                turn_id=owner['_native_turn_id'], state='executing', action_hash='x', created_at='2026-10-27',
                action={'name': 'terminal', 'arguments': {'command': 'gh pr ready 8669'}})),
            ('unknown asker tool', 'no confirmed result', 'native_receipts', dict(id='unknown', task_id='asker',
                turn_id='asker-turn', state='awaiting_answer', action_hash='x', created_at='2026-10-27',
                action={'name': 'clarify', 'arguments': {}})),
            ('active request', 'active request', 'contexts', dict(id='reply', task_id='asker', decision_id='held',
                status='working')),
        ]
        for name, pattern, table, record in cases:
            with self.subTest(case=name):
                self.service._put(table, dict(record, bot_id=owner['bot_id']))
                self.refused(owner, source, pattern)
                self.service.db.execute('DELETE FROM ' + table + ' WHERE id=?', (record['id'],))
        for change in (dict(status='working'), dict(status='queued'), dict(_waiting_on=['x']), dict(_return_pending={'text': 'x'})):
            with self.subTest(asker=change):
                self.service._put('tasks', dict(self.asker, **change))
                self.refused(owner, source, 'still active')
        self.service._put('tasks', self.asker)
        # The asker must be this owner's own return. A card of a stranger's callback does not pause this work by name.
        self.service._put('tasks', dict(self.asker, source_task_id='unrelated', parent_task_id='unrelated'))
        self.refused(owner, source, 'held merge changed')
        self.service._put('tasks', self.asker)
        recovering = self.put_owner(owner, _self_recovery=dict(unknown_tools=[dict(id='old-call')]))
        self.refused(recovering, source, 'no confirmed result')
        self.service._put('tasks', owner)
        other = dict(id='other-pause', task_id=owner['id'], bot_id=owner['bot_id'], status='approved',
                     action_type='question', revision=1, decided_by='person-device', decided_at='2026-10-27T00:00:00Z',
                     _native=True)
        self.service._put('decisions', other)
        self.service._put('artifacts', dict(self.pr, id='peer-pr', task_id='unrelated', paused_by_decision_id='other-pause'))
        self.refused(owner, source, 'paused by another decision')
        self.service.db.execute("DELETE FROM artifacts WHERE id='peer-pr'")
        self.service.db.execute("DELETE FROM decisions WHERE id='other-pause'")
        self.service._put('artifacts', dict(self.pr, paused_by_decision_id=None))
        self.refused(owner, source, 'paused by another decision')
        self.service._put('artifacts', dict(self.pr, url='https://github.com/antiwork/gumroad/pull/8670'))
        self.refused(owner, source, 'held merge changed')
        self.service._put('artifacts', self.pr)
        self.person_says(owner, 'Mark it ready after all.')
        self.refused(owner, source, 'has new direction')
        self.assert_hold_kept()

    def test_a_stop_or_refusal_in_the_asking_thread_ends_the_hold_for_the_lane(self):
        owner, source = self.held_native()
        asker = dict(owner, id='asker')
        # The person's own message in the thread that asked the card, after the hold, is new direction there.
        stop = self.person_says(asker, 'Stop all work on #8669.')
        self.refused(owner, source, 'has new direction')
        self.service.db.execute('DELETE FROM messages WHERE id=?', (stop['id'],))
        for status in ('rejected', 'approved'):
            later = dict(id='later', task_id='asker', bot_id=source['bot_id'], status=status, action_type='question',
                         inbox_kind='decision', revision=1, decided_by='person-device', decided_at='2999-01-01T00:00:00Z',
                         _native=True)
            with self.subTest(later=status):
                self.service._put('decisions', later)
                self.refused(owner, source, 'declined part of this work|has new direction')
                self.service.db.execute("DELETE FROM decisions WHERE id='later'")
        self.assertEqual(self.lane(source)['status'], 'queued', 'The unchanged records are admitted')
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        # The same stop during the lane turn ends the hold: the turn keeps its holds and leaves through a normal turn.
        seen = self.drive(owner, during=lambda: self.person_says(asker, 'Stop all work on #8669.'))
        self.assertEqual(seen['held']['kanban_complete'], 403)
        held = self.service._get('tasks', owner['id'])
        self.assertEqual((held['status'], held['_native_turn_id'], held['summary']),
                         ('queued', lane_turn, 'Continuing after the held decision or its pause changed.'))
        self.assertNotIn('_resume_decision', held)
        self.service._kanban_rows()
        moved = self.service._get('tasks', owner['id'])
        self.assertNotIn('_technical_lane', moved)
        self.assertNotIn('_resume_decision', moved, 'Nothing replays the hold')
        self.assertTrue(self.service._task_work_is_paused(moved))
        self.assert_hold_kept()

    def answer_later_question(self, task_id, option='continue'):
        """A later native question of this task, answered by the person and finished through its real resumed turn."""
        task = self.service._get('tasks', task_id)
        self.service._put('tasks', dict(task, status='working', _native_turn_id=str(uuid4())))
        with self.assertRaises(YieldForDecision):
            self.service.emit(task_id, 'native_question', dict(title='Keep reviewing #8669 after the new delta?',
                options=[dict(id='continue', label='Keep reviewing', explanation=''),
                         dict(id='stop', label='Stop reviewing', explanation='')]))
        question = next(d for d in self.service._all('decisions') if d['task_id'] == task_id and d['status'] == 'pending')
        self.service._put('decisions', dict(question, _profile_revision=self.service.profiles.prompt_revision(
            self.service._get('bots', question['bot_id']))))
        self.service.mutate('POST', '/v1/decisions/%s/answer' % question['id'],
                            {'option_id': option, 'revision': question['revision']}, str(uuid4()), 'test-phone')
        self.assertEqual(self.service._get('decisions', question['id'])['status'], 'approved')
        seen = {}
        def resume(task, bot, history, decision, emit):
            seen['decision'] = decision['id']
            emit('completed', {'summary': 'Kept reviewing.'})
        self.service.provider.resume_task = resume
        self.service._run_task(task_id)
        self.assertEqual(seen, {'decision': question['id']}, 'The answer resumed its own turn')
        return self.service._get('decisions', question['id'])

    def assert_answer_is_the_only_direction(self, owner, question, tasks):
        # The answer finished: the card is executed, its execution is idle, and no human message stands in for it.
        self.assertEqual((question['status'], question['decided_by']), ('executed', 'test-phone'))
        self.assertEqual([a['decided_by'] for a in question['_answers']], ['test-phone'])
        self.assertGreater(question['decided_at'], self.paused_at)
        # No turn runs and no card waits. A coordinating requester keeps waiting for its delegate, as before the card.
        self.assertTrue(all(self.service._get('tasks', t)['status'] in ('completed', 'waiting') for t in tasks))
        self.assertFalse(any(d['status'] in ('pending', 'approved') for d in self.service._all('decisions')
                             if d['task_id'] in tasks))
        self.assertFalse(any(t in self.service.running for t in tasks))
        self.assertFalse(any(m.get('_human_device_id') and m['created_at'] > self.paused_at
                             for m in self.service._all('messages')))
        self.assertFalse(self.service._human_direction_after(set(tasks), self.paused_at, self.held),
                         'The shared check reads only approved or modified cards, so it misses the executed answer')

    def test_a_later_answer_that_finished_in_the_asking_thread_ends_the_hold(self):
        owner, source = self.held_native()
        question = self.answer_later_question('asker')
        self.assert_answer_is_the_only_direction(owner, question, ['asker'])
        self.refused(owner, source, 'has new direction')
        self.assert_hold_kept()
        self.assertEqual(self.service._get('decisions', question['id'])['status'], 'executed')

    def test_a_later_answer_that_finished_in_the_owner_lineage_ends_the_hold(self):
        owner, source = self.held_native()
        self.assertIn(source['id'], (owner.get('parent_task_id'), owner.get('source_task_id')),
                      'The calling requester task is in the owner lineage')
        question = self.answer_later_question(source['id'])
        self.assert_answer_is_the_only_direction(owner, question, [source['id']])
        self.service._put('tasks', dict(self.service._get('tasks', source['id']), status='working'))
        self.refused(self.service._get('tasks', owner['id']), source, 'has new direction')
        self.assert_hold_kept()

    def test_earlier_system_and_pausing_card_answers_do_not_end_the_hold(self):
        owner, source = self.held_native()
        answered = dict(task_id='asker', bot_id=source['bot_id'], status='executed', action_type='question',
                        inbox_kind='decision', revision=1, _native=True, selected_option_id='continue')
        cards = [dict(answered, id='before', decided_by='person-device', decided_at='2026-10-27T00:00:00Z',
                      _answers=[dict(choice='approve', option_id='continue', decided_by='person-device')])]
        for actor in sorted(SYSTEM_DECIDERS):
            cards.append(dict(answered, id='system-' + actor, decided_by=actor, decided_at='2999-01-01T00:00:00Z',
                              status='failed', _answers=[dict(choice='approve', option_id='continue', decided_by=actor)]))
        cards.append(dict(answered, id='agent-revision', decided_by=None, decided_at=None, status='executed',
                          _answers=[dict(choice='revise', correction='Findings changed.', decided_by=None)]))
        for card in cards:
            self.service._put('decisions', card)
        # The pausing card's own later revision of its answer time is not new direction either.
        self.service._put('decisions', dict(self.held, decided_at='2999-01-01T00:00:00Z'))
        self.assertEqual(self.lane(source)['status'], 'queued')

    def test_a_later_answer_that_finishes_during_the_turn_ends_it_without_replay(self):
        owner, source = self.held_native()
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        finished = {}
        def later_answer():
            asker = self.service._get('tasks', 'asker')
            self.service._put('tasks', dict(asker, status='working', _native_turn_id=str(uuid4())))
            try:
                self.service.emit('asker', 'native_question', dict(title='Keep reviewing #8669?',
                    options=[dict(id='continue', label='Keep reviewing', explanation='')]))
            except YieldForDecision:
                pass
            question = next(d for d in self.service._all('decisions') if d['task_id'] == 'asker' and d['status'] == 'pending')
            self.service._put('decisions', dict(question, _profile_revision=self.service.profiles.prompt_revision(
                self.service._get('bots', question['bot_id']))))
            self.service.mutate('POST', '/v1/decisions/%s/answer' % question['id'],
                                {'option_id': 'continue', 'revision': question['revision']}, str(uuid4()), 'test-phone')
            # The resumed turn of the asking task finishes through the real completion route, which executes the answer.
            self.service._put('tasks', dict(self.service._get('tasks', 'asker'), status='working'))
            self.service.emit('asker', 'completed', {'summary': 'Kept reviewing.'})
            finished.update(self.service._get('decisions', question['id']))
        seen = self.drive(owner, during=later_answer)
        self.assertEqual(finished['status'], 'executed')
        self.assertEqual(seen['held']['kanban_complete'], 403, 'The lane turn keeps its holds')
        held = self.service._get('tasks', owner['id'])
        self.assertEqual((held['status'], held['_native_turn_id'], held['summary']),
                         ('queued', lane_turn, 'Continuing after the held decision or its pause changed.'))
        self.assertNotIn('_resume_decision', held)
        self.service._kanban_rows()
        moved = self.service._get('tasks', owner['id'])
        self.assertNotIn('_technical_lane', moved)
        self.assertNotIn('_resume_decision', moved, 'Nothing replays the hold')
        self.assert_hold_kept()

    def test_the_lane_refuses_a_stopped_foreign_active_or_open_owner(self):
        owner, source = self.held_native()
        for change in (dict(status='failed'), dict(status='queued'), dict(status='needs_you'), dict(status='cancelled'),
                       dict(status='waiting'), dict(_return_pending=dict(text='x'))):
            with self.subTest(owner=change):
                self.refused(self.put_owner(owner, **change), source, 'paused|Wait|completed owner|previous result')
        for change in (dict(_conversation_epoch='earlier'), dict(session_id='earlier-session')):
            with self.subTest(owner=change):
                self.refused(self.put_owner(owner, **change), source, 'earlier conversation')
        self.service._put('tasks', owner)
        native = self.service.kanban.tasks[owner['id']]
        for change in (dict(status='running', current_run_id=7), dict(status='done', worker_pid=123), dict(status='ready'),
                       dict(status='review'), dict(status='triage'), dict(status='blocked'), dict(status='archived')):
            saved = dict(native)
            native.update(change)
            with self.subTest(native=change), self.assertRaises(ServiceError):
                self.lane(source)
            self.assertEqual(self.service._get('tasks', owner['id']), owner)
            native.clear()
            native.update(saved)
        self.service.running[owner['id']] = Mock(is_alive=lambda: True)
        self.refused(owner, source, 'completed owner')
        self.service.running.clear()
        exchange = self.service._get('exchanges', owner['exchange_id'])
        self.service._put('exchanges', dict(exchange, cancelled=True))
        self.refused(owner, source, 'cancelled')
        self.service._put('exchanges', exchange)
        ops = self.write('/v1/bots', {'name': 'Ops'})
        self.service._put('tasks', dict(source, bot_id=ops['id']))
        self.refused(owner, source, 'Only the owner or its requesting agent')
        self.service._put('tasks', source)

    def test_a_read_only_turn_runs_with_its_holds_follows_the_successor_and_returns_to_the_hold_once(self):
        owner, source = self.held_native()
        requester = self.service._get('tasks', source['id'])
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        # Hermes gives the new input of the done card its linked successor. The lane follows it before its turn starts.
        self.service.kanban.tasks[owner['id']].update(id='t_successor', status='ready', parent_ids=['t_existing'])
        self.service._reconcile_kanban()
        bound = self.service._get('tasks', owner['id'])
        self.assertEqual((bound['_native_outcome_id'], bound['_technical_lane']['native_task_id']), ('t_successor', 't_successor'))
        self.assertEqual(self.service._get('artifacts', 'held-pr')['_native_outcome_id'], 't_successor')
        self.assertEqual(bound['_native_turn_id'], lane_turn)
        self.assertIsNone(held_action(dict(name='kanban_comment', arguments={'task_id': 't_successor'}), 't_successor'))
        self.assertIsNotNone(held_action(dict(name='kanban_comment', arguments={'task_id': 't_existing'}), 't_successor'))
        seen = self.drive(owner)
        self.assertEqual(seen['task']['_technical_lane']['held_merge_option_id'], 'leave_open')
        self.assertEqual(seen['task']['_native_turn_id'], lane_turn)
        self.assertEqual(seen['comment'], 'recorded')
        self.assertEqual(seen['held'], dict(kanban_complete=403, kanban_block=403, message_agent=403))
        self.assertEqual(seen['events'], dict(decision_response=403, native_question=400, artifact=403, work_continue=403))
        finished = self.service._get('tasks', owner['id'])
        self.assertEqual((finished['status'], finished['summary'], finished['_native_turn_id']),
                         ('completed', 'Verified the draft locally.', lane_turn))
        for key in ('_resume_decision', '_return_pending', '_blocked_followup'):
            self.assertNotIn(key, finished)
        self.assertIn('The hold and its pause stay.', self.service._task_messages(owner['id'])[-1]['text'])
        self.assertEqual(self.service._get('tasks', source['id']), requester)
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']], [])
        self.assert_hold_kept()
        self.assertEqual(len(self.service._all('decisions')), 2, 'No decision was created or answered')
        self.assertEqual(self.service._technical_native_state(finished), 'scheduled')
        self.assertTrue(self.service._automatic_work_is_paused(finished))
        for native_status in ('scheduled', 'ready'):
            self.service.kanban.tasks[owner['id']].update(status=native_status, current_run_id=None, worker_pid=None)
            for _ in range(2):
                rows = [r for r in self.service._kanban_rows() if r['id'] == owner['id']]
                self.service._native_retry(self.service._get('tasks', owner['id']), self.service.kanban.tasks[owner['id']])
            self.assertEqual([r['state'] for r in rows], ['done'])
            self.assertEqual(self.service._get('tasks', owner['id']), finished, native_status)
        # The plain call still refuses. A parked card admits another read-only turn only by the same exact request.
        self.service._put('tasks', dict(requester, status='working'))
        with self.assertRaisesRegex(ServiceError, 'This work is paused'):
            self.service.emit(source['id'], 'work_continue', dict(task_id='t_successor', message='Continue.'))
        self.service.kanban.tasks[owner['id']]['status'] = 'scheduled'
        self.lane(source, message='Read the new CI log too.', task_id='t_successor')
        again = self.service._get('tasks', owner['id'])
        self.assertEqual((again['status'], again['_technical_lane']['native_task_id']), ('queued', 't_successor'))

    def test_new_direction_during_the_turn_leaves_the_lane_without_replaying_the_hold(self):
        owner, source = self.held_native()
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        seen = self.drive(owner, during=lambda: self.person_says(owner, 'Mark it ready after all.'))
        self.assertEqual(seen['held']['kanban_complete'], 403, 'The lane turn keeps its holds')
        held = self.service._get('tasks', owner['id'])
        self.assertEqual((held['status'], held['_native_turn_id']), ('queued', lane_turn))
        self.assertNotIn('_resume_decision', held)
        self.service._kanban_rows()
        moved = self.service._get('tasks', owner['id'])
        self.assertNotEqual(moved['_native_turn_id'], lane_turn)
        self.assertNotIn('_technical_lane', moved)
        self.assertNotIn('_resume_decision', moved, 'Nothing replays the hold')
        self.assert_hold_kept()

    def test_a_restart_during_a_read_only_tool_returns_to_the_hold_and_keeps_the_unknown_tool(self):
        from coconut.service import Service
        owner, source = self.held_native()
        self.lane(source)
        lane_turn = self.service._get('tasks', owner['id'])['_native_turn_id']
        self.service._put('tasks', dict(self.service._get('tasks', owner['id']), status='working'))
        call = self.service.emit(owner['id'], 'native_call', dict(name='terminal', arguments={'command': 'git log -5'}))
        unknown = self.service._get('native_receipts', call['id'])
        native = copy.deepcopy(self.service.kanban.tasks)
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.service.kanban = Mock(tasks=native)
        self.service.kanban.call.side_effect = lambda request: self.snapshot_result()
        recovered = self.service._get('tasks', owner['id'])
        self.assertEqual((recovered['status'], recovered['_native_turn_id']), ('completed', lane_turn))
        self.assertNotIn('_resume_decision', recovered)
        self.assertEqual(self.service._get('native_receipts', call['id']), unknown)
        self.service._put('tasks', dict(self.service._get('tasks', source['id']), status='working'))
        self.service.kanban.tasks[owner['id']]['status'] = 'scheduled'
        with self.assertRaisesRegex(ServiceError, 'no confirmed result'):
            self.lane(source, message='Retry after the restart.')
        self.assert_hold_kept()


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


@needs_hermes
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
        # This gate already has an assessment for the current native profile.
        gate = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], status='pending',
            action_type='question', inbox_kind='decision', revision=2, title=owner['summary'], explanation='',
            _profile_revision=self.service.profiles.prompt_revision(self.service._get('bots', owner['bot_id'])),
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
        # The answer ends the gate, but the in-flight lane turn keeps its holds until the dispatcher moves the owner.
        self.assertEqual(resumed['_native_turn_id'], seen['turn'])
        self.assertEqual(self.service._technical_lane(resumed)['turn_id'], seen['turn'])
        self.assertIsNone(self.service._technical_gate(resumed)[0])
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
        completed = self.service._get('tasks', owner['id'])
        self.assertEqual(completed['status'], 'completed')
        # The dispatcher moved the owner to a normal resumed turn, which ended the lane.
        self.assertNotIn('_technical_lane', completed)
        self.assertEqual(completed['_native_turn_id'], captured['task']['_native_turn_id'])
        self.assertNotEqual(completed['_native_turn_id'], seen['turn'])
        lane_receipts = {r['id'] for r in self.service._native_task_receipts(owner['id']) if r['turn_id'] == seen['turn']}
        self.assertTrue(lane_receipts)
        self.assertNotEqual(captured['task']['_native_turn_id'], seen['turn'])
        self.assertFalse(lane_receipts & {r['id'] for r in captured['task']['_native_receipts']})
        self.assertEqual((captured['decision']['id'], captured['decision']['selected_option_id']), (gate['id'], 'accept'))


    def test_a_declined_merge_lane_runs_on_the_real_triage_card_and_parks_it_without_more_runs(self):
        from coconut.service import now
        owner, source, identity, workspace = self.prepare('blocked')
        # Hermes sends the same input block to triage on its second recurrence, as on the live card.
        self.native_program("""
for _ in range(2):
    board.kb.unblock_task(board.conn, sys.argv[2])
    board.kb.block_task(board.conn, sys.argv[2], reason='Merge declined by the person.', kind='needs_input')
""", identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.native_status(identity), 'triage')
        paused_at = now()
        work = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], kind='work', title=owner['title'],
                    status='waiting', paused_by_decision_id='declined', _work_pause_at=paused_at,
                    created_at=owner['created_at'], _native_outcome_id=identity, _work_key='native-' + identity)
        pr = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], kind='pr', url=DECLINED_PR, title='#8661',
                  status='failed', pr_state='open', _review_task_id=owner['id'], _activity_task_id=owner['id'],
                  work_outcome_id=work['id'], paused_by_decision_id='declined', _work_pause_at=paused_at,
                  created_at=owner['created_at'])
        declined = dict(id='declined', task_id=owner['id'], bot_id=owner['bot_id'], status='rejected', action_type='question',
            inbox_kind='decision', revision=1, title='Approve the migration and merge #8661?', context='',
            merge_pr_url=DECLINED_PR, options=[dict(id='merge', label='Merge #8661', answer='approve', explanation='')],
            selected_option_id=None, selected_option_label=None, correction='', decided_by='test-phone',
            decided_at=paused_at, created_at=owner['created_at'], _native=True,
            _answers=[dict(choice='reject', option_id=None, correction='', decided_by='test-phone')])
        owner.update(_native_outcome_id=identity, _work_context_id=work['id'], summary='Original input complete')
        for table, record in (('artifacts', work), ('artifacts', pr), ('decisions', declined), ('tasks', owner)):
            self.service._put(table, record)
        with self.assertRaisesRegex(ServiceError, 'This work is paused'):
            self.service.emit(source['id'], 'work_continue', dict(task_id=identity, message='Continue.'))
        runs = int(self.native_program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", '
                                       '(sys.argv[2],)).fetchone()[0])', identity))
        receipt = self.service.emit(source['id'], 'work_continue', dict(task_id=identity,
            message='Diagnose the CI failure locally. #8661 stays unmerged.', independent_technical_work=dict(
                decision_id='declined', decision_revision=1, scope=['diagnosis', 'tests'], declined_merge_pr_url=DECLINED_PR)))
        self.assertEqual(receipt['task_id'], owner['id'])
        seen = {}
        def run(task, bot, history, emit):
            seen.update(lane=task.get('_technical_lane'), native=self.native_status(task['_native_outcome_id']))
            record = emit('native_call', {'name': 'terminal', 'arguments': {'command': 'bin/rspec'}})
            emit('native_result', {'id': record['id'], 'result': json.dumps({'exit_code': 0})})
            for name in ('kanban_complete', 'kanban_block'):
                try:
                    emit('native_call', {'name': name, 'arguments': {'summary': 'Done.', 'reason': 'Waiting'}})
                except ServiceError as error:
                    seen[name] = error.status
            emit('completed', {'summary': 'Diagnosed the CI failure locally.'})
        def resume(*args):
            raise AssertionError('A declined merge never resumes its decision.')
        self.provider.start_task, self.provider.resume_task = run, resume
        self.service._dispatch_pending()
        worker = self.service.running.get(owner['id'])
        self.assertIsNotNone(worker, 'The lane must dispatch the real native claim of the same owner past the pause')
        worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual((seen['native'], seen['lane']['declined_merge_pr_url']), ('running', DECLINED_PR))
        self.assertEqual((seen['kanban_complete'], seen['kanban_block']), (403, 403))
        for _ in range(3):
            # Repeated passes: the card parks once, nothing completes it, retries it, or claims it again.
            self.service.kanban.call({'op': 'snapshot'})
            self.service._reconcile_kanban()
            self.service._drain_returns()
            self.service._dispatch_pending()
            again = self.service.running.get(owner['id'])
            self.assertTrue(again is worker or again is None, 'No second worker claims the parked card')
        self.assertEqual(self.native_status(identity), 'scheduled')
        card = self.service.kanban.tasks[owner['id']]
        self.assertEqual((card['id'], card['workspace_path'], card.get('consecutive_failures', 0)),
                         (identity, str(workspace), 0))
        self.assertEqual(self.native_program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", '
                                             '(sys.argv[2],)).fetchone()[0])', identity), str(runs + 1))
        finished = self.service._get('tasks', owner['id'])
        self.assertEqual((finished['status'], finished['summary']), ('completed', 'Original input complete'))
        self.assertNotIn('_resume_decision', finished)
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']], [])
        self.assertEqual(self.service._get('decisions', 'declined'), declined)
        self.assertEqual((self.service._get('artifacts', work['id']), self.service._get('artifacts', pr['id'])), (work, pr))
        self.assertEqual((workspace / 'pending.txt').read_text(), 'Uncommitted correction')
        self.assertEqual(len(self.service._all('decisions')), 1)

    def test_a_held_merge_lane_diagnoses_on_the_linked_successor_of_a_done_card_and_parks_it(self):
        from coconut.service import now
        owner, source, identity, workspace = self.prepare('done')
        self.assertEqual(self.native_status(identity), 'done')
        # The requester's return callback of this owner asked the person about the owner's PR, and finished.
        asker = dict(copy.deepcopy(source), id=str(uuid4()), status='completed', source_task_id=owner['id'],
                     parent_task_id=owner['id'], _execution_session_id=str(uuid4()), _is_return=True, _waiting_on=[])
        asker.pop('_return_pending', None)
        held = dict(id='held', task_id=asker['id'], bot_id=source['bot_id'], status='executed', action_type='question',
            inbox_kind='decision', revision=1, title='Mark Gumroad #8669 ready and merge it?', context='', merge_pr_url=HELD_PR,
            options=[dict(id='merge', label='Mark #8669 ready and merge', answer='approve', explanation=''),
                     dict(id='leave_open', label='Leave #8669 open', answer='approve', explanation='Keep the draft.')],
            selected_option_id='leave_open', selected_option_label='Leave #8669 open', correction='', decided_by='test-phone',
            decided_at=owner['created_at'], created_at=owner['created_at'], _native=True, _action={},
            _answers=[dict(choice='approve', option_id='leave_open', correction='', decided_by='test-phone')])
        pr = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], kind='pr', url=HELD_PR, title='#8669',
                  status='failed', pr_state='open', _review_task_id=owner['id'], paused_by_decision_id='held',
                  _work_pause_at=now(), created_at=owner['created_at'])
        for table, record in (('tasks', asker), ('decisions', held), ('artifacts', pr)):
            self.service._put(table, record)
        with self.assertRaisesRegex(ServiceError, 'This work is paused'):
            self.service.emit(source['id'], 'work_continue', dict(task_id=identity, message='Continue.'))
        count = 'print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (sys.argv[2],)).fetchone()[0])'
        runs = self.native_program(count, identity)
        receipt = self.service.emit(source['id'], 'work_continue', dict(task_id=identity,
            message='Diagnose the new author delta read-only. #8669 stays open.',
            independent_technical_work=dict(HELD_LANE)))
        self.assertEqual(receipt['task_id'], owner['id'])
        seen = {}
        def run(task, bot, history, emit):
            seen.update(lane=copy.deepcopy(task.get('_technical_lane')), outcome=task['_native_outcome_id'],
                        native=self.native_status(task['_native_outcome_id']), old=self.native_status(identity),
                        parents=self.native_program('print(",".join(board.kb.parent_ids(board.conn, sys.argv[2])))',
                                                    task['_native_outcome_id']))
            record = emit('native_call', {'name': 'terminal', 'arguments': {'command': 'git log --oneline -3'}})
            emit('native_result', {'id': record['id'], 'result': json.dumps({'exit_code': 0})})
            note = emit('native_call', {'name': 'kanban_comment', 'arguments': {'body': 'Read-only findings saved.'}})
            emit('native_result', {'id': note['id'], 'result': '{"ok": true}'})
            for name, arguments in (('kanban_complete', {'summary': 'Done.'}), ('kanban_comment', {'task_id': identity, 'body': 'x'})):
                try:
                    emit('native_call', {'name': name, 'arguments': arguments})
                except ServiceError as error:
                    seen[name + (':old' if arguments.get('task_id') else '')] = error.status
            emit('completed', {'summary': 'Diagnosed the author delta read-only.'})
        def resume(*args):
            raise AssertionError('A held merge never resumes its decision.')
        self.provider.start_task, self.provider.resume_task = run, resume
        self.service._dispatch_pending()
        worker = self.service.running.get(owner['id'])
        self.assertIsNotNone(worker, 'The lane must dispatch the real native claim of the successor past the pause')
        worker.join(15)
        self.assertFalse(worker.is_alive())
        successor = seen['outcome']
        self.assertNotEqual(successor, identity, 'New input on a done card owns a successor')
        self.assertEqual((seen['native'], seen['old'], seen['parents']), ('running', 'done', identity),
                         'The successor is the linked follow-up of the completed card')
        self.assertEqual(seen['lane']['native_task_id'], successor, 'The lane follows the successor before its turn')
        self.assertEqual((seen['kanban_complete'], seen['kanban_comment:old']), (403, 403))
        for _ in range(3):
            # Repeated passes: the successor parks once, the old card keeps its result, and nothing claims either again.
            self.service.kanban.call({'op': 'snapshot'})
            self.service._reconcile_kanban()
            self.service._drain_returns()
            self.service._dispatch_pending()
            again = self.service.running.get(owner['id'])
            self.assertTrue(again is worker or again is None, 'No second worker claims the parked card')
        self.assertEqual((self.native_status(identity), self.native_status(successor)), ('done', 'scheduled'))
        self.assertEqual(self.native_program(count, identity), runs, 'The completed card keeps its runs')
        self.assertEqual(self.native_program(count, successor), '1')
        card = self.service.kanban.tasks[owner['id']]
        self.assertEqual((card['id'], card['workspace_path'], card.get('consecutive_failures', 0)), (successor, str(workspace), 0))
        finished = self.service._get('tasks', owner['id'])
        self.assertEqual((finished['status'], finished['summary']), ('completed', 'Original input complete'))
        self.assertNotIn('_resume_decision', finished)
        self.assertEqual([d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']], [])
        self.assertEqual(self.service._get('decisions', 'held'), held)
        stored = self.service._get('artifacts', pr['id'])
        self.assertEqual(({k: v for k, v in stored.items() if k != '_native_outcome_id'}, stored['_native_outcome_id']),
                         (pr, successor))
        self.assertEqual((workspace / 'pending.txt').read_text(), 'Uncommitted correction')
        self.assertEqual(len(self.service._all('decisions')), 1)

if __name__ == '__main__':
    unittest.main()
