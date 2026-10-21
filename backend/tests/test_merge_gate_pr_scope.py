import copy
import json
import tempfile
import unittest
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError, YieldForDecision

import legacy_work_sync
import test_targeted_bot_continuation as continuation_fixtures
from saved_pr_scope_topology import ARTIFACTS, TASKS

TARGET = 'https://github.com/example/target/pull/3170'
SIBLING = 'https://github.com/example/sibling/pull/383'
URLS = {'target': TARGET, 'sibling': SIBLING}
ROOT, CALLER, SETUP, EXPERT = 'b8057ab9', '6a524539', 'bad1ea8a', 'da8d0428'
PENDING = 'pending worker or delegated result'


class MergeGatePullRequestScopeTests(unittest.TestCase):
    """The saved graph of one Desk intake root that received two pull requests.

    The root returned PR 3170 reviews and PR 383 work in one exchange. After the PR 383 setup execution asked an
    expert to read advice, it waited on that finished task and stopped for a person's product decision. Its wait
    result was never delivered, because a task that needs a person cannot consume it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        self.bots = {name: self.write('/v1/bots', {'name': name, 'role': ''})
                     for name in ('Desk', 'Gumroad Dev', 'Coconut Care')}
        template = self.write(f"/v1/bots/{self.bots['Desk']['id']}/messages", {'text': 'Fast work intake'})
        template = self.service._get('tasks', template['id'])
        with self.service.transaction():
            for number, row in enumerate(TASKS):
                bot = self.service._get('bots', self.bots[row['bot']]['id'])
                task = copy.deepcopy(template)
                task.update(id=row['id'], bot_id=bot['id'], session_id=bot['session_id'], status=row['status'],
                            summary='Complete.', created_at=f'2026-10-01T14:{number:02d}:00.000000Z',
                            _assignment_task_id=row.get('assignment'), _is_return=row.get('is_return', False),
                            source_task_id=row.get('source'), _resolved_dependency_id=row.get('resolved'),
                            _requester_reply_task_id=row.get('requester'), _relay_callback_owner=row.get('callback'),
                            _waiting_on=list(row.get('waiting', [])), _execution_session_id=row.get('execution'),
                            _continued_task_id=row.get('continued'), _native_relay=row.get('relay', False),
                            _reported_pr_urls=sorted(URLS[name] for name in row.get('reported', [])))
                if row.get('native'):
                    task['_native_outcome_id'] = 't_' + row['id']
                for key in [k for k, v in task.items() if v in (None, False) and k.startswith('_')]:
                    del task[key]
                self.service._put('tasks', task)
            for bot, pr, task_id, review_id in ARTIFACTS:
                self.service._put('artifacts', dict(
                    id=f'{bot}-{pr}', bot_id=self.bots[bot]['id'], task_id=task_id, _review_task_id=review_id,
                    kind='pr', url=URLS[pr], title='Change', status='open', pr_state='open', head_sha='a' * 40,
                    created_at='2026-10-01T14:00:00.000000Z', observed_at='2026-10-01T15:00:00.000000Z',
                    checked_at='2026-10-01T15:00:00.000000Z', refresh_error=None))
        # The saved graph also holds the later diagnosis and its worker. They did not exist when the gate failed.
        self.patch('3910baa3', status='completed', _waiting_on=[])
        self.patch('51b30b3d', status='completed')
        # The setup execution is the historical offender: it needs a person and holds an undelivered finished wait.
        self.patch(SETUP, status='needs_you', summary='Waiting for the Xcode decision.')
        self.sibling_decision = dict(id='xcode-decision', task_id=SETUP, bot_id=self.bots['Gumroad Dev']['id'],
                                     status='pending', title='Provide Xcode 27 for the sibling tests?',
                                     action_type='question', inbox_kind='decision', revision=1,
                                     created_at='2026-10-01T15:21:10.273999Z')
        self.service._put('decisions', self.sibling_decision)
        self.wait_receipt = self.receipt('sibling-wait', SETUP, _wait_task_id=EXPERT)
        self.card = dict(id='merge-3170', merge_pr_url=TARGET)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def write(self, path, data):
        return self.service.mutate('POST', path, data, str(uuid4()))

    def patch(self, task_id, **fields):
        with self.service.transaction():
            task = self.service._get('tasks', task_id)
            task.update(fields)
            self.service._put('tasks', task)

    def receipt(self, identity, task_id, **fields):
        record = dict(id=identity, task_id=task_id, turn_id='turn-' + identity, state='completed',
                      action=dict(name='coconut_wait', arguments={}), action_hash=identity, result='{}',
                      created_at='2026-10-01T15:19:35.736944Z', completed_at='2026-10-01T15:19:35.736954Z')
        if '_wait_task_id' in fields:
            care = self.service._get('tasks', EXPERT)
            fields = dict(_wait_session_id=care['session_id'], _completion_text='Task completed: Complete.', **fields)
        record.update(fields)
        self.service._put('native_receipts', record)
        return record

    def validate(self, url=TARGET):
        self.patch(CALLER, status='working')
        return self.service._validate_merge_decision(dict(self.card, merge_pr_url=url), self.service._get('tasks', CALLER))

    def saved(self):
        return {table: self.service._all(table) for table in ('tasks', 'decisions', 'native_receipts', 'artifacts')}

    def assertOpen(self, url=TARGET):
        try:
            self.assertIsNotNone(self.validate(url))
        except ServiceError as error:
            self.fail(f'The gate for {url} stayed closed: {error}')

    def deliver_sibling_wait(self):
        self.service._put('native_receipts', dict(self.wait_receipt, _process_delivery='delivered'))

    def assertBlocked(self, url=TARGET):
        with self.assertRaisesRegex(ServiceError, PENDING) as error:
            self.validate(url)
        self.assertEqual(error.exception.status, 400)

    def test_fixture_is_the_saved_shape(self):
        tasks = {t['id']: t for t in self.service._all('tasks')}
        records = self.service._work_receipts()
        chain = {}
        for artifact in self.service._all('artifacts'):
            if artifact['url'] == TARGET:
                chain.update(self.service._work_chain(tasks[artifact['_review_task_id']], tasks, records,
                                                      include_continuations=True))
        self.assertEqual(len(chain), len(TASKS))
        self.assertIn(SETUP, chain)
        self.assertTrue(self.service._background_pending(SETUP))
        self.assertFalse(self.service._background_pending(CALLER))

    def test_sibling_setup_wait_does_not_gate_the_target_merge_decision(self):
        before = self.saved()
        self.assertOpen()
        after = self.saved()
        # The target gate neither answers, delivers, nor wakes the sibling's product work.
        self.assertEqual({k: v for k, v in after.items() if k != 'tasks'}, {k: v for k, v in before.items() if k != 'tasks'})
        self.assertEqual(self.service._get('tasks', SETUP)['status'], 'needs_you')
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        self.assertNotIn('_process_delivery', self.service._get('native_receipts', 'sibling-wait'))

    def test_the_sibling_pr_is_still_gated_by_its_own_setup_wait(self):
        self.assertBlocked(SIBLING)

    def test_a_replied_sibling_decision_and_delivered_wait_leave_the_gate_open(self):
        self.service._put('decisions', dict(self.sibling_decision, status='replied', decided_by='person',
                                            decided_at='2026-10-01T16:02:02.593923Z', revision=2))
        self.receipt('sibling-wait', SETUP, _wait_task_id=EXPERT, _process_delivery='delivered')
        self.patch(SETUP, status='completed')
        self.assertOpen()
        self.assertOpen(SIBLING)
        self.assertEqual(self.service._get('decisions', 'xcode-decision')['status'], 'replied')

    def test_a_pending_wait_of_a_target_only_task_still_gates(self):
        self.receipt('target-wait', 'f21ea9d7', _wait_task_id=EXPERT)
        self.assertBlocked()

    def test_unread_process_result_of_the_target_review_still_gates(self):
        self.deliver_sibling_wait()
        record = self.receipt('target-review', 'f21ea9d7', action=dict(name='terminal', arguments={}),
                              result=json.dumps(dict(session_id='review', notify_on_complete=True)))
        self.assertBlocked()
        self.service._put('native_receipts', dict(record, _process_delivery='consumed'))
        self.assertOpen()

    def test_undelivered_answer_queued_for_a_target_task_still_gates(self):
        self.patch('393ab0b2', status='queued', _background_completion=['The review finished.'])
        self.assertBlocked()

    def test_a_target_task_waiting_on_a_sibling_task_still_gates(self):
        self.patch('f21ea9d7', status='waiting', _waiting_on=[SETUP])
        self.assertBlocked()

    def test_a_task_that_also_reported_the_target_is_not_exempt(self):
        self.patch(SETUP, _reported_pr_urls=[SIBLING, TARGET])
        self.assertBlocked()

    def test_a_current_review_of_the_target_keeps_the_task_in_the_gate(self):
        self.patch(SETUP, _pr_review=dict(url=TARGET, pr_state='open', head_sha='a' * 40))
        self.assertBlocked()

    def test_a_merge_card_of_the_target_keeps_its_task_in_the_gate(self):
        self.service._put('decisions', dict(self.sibling_decision, id='done-card', status='executed',
                                            merge_pr_url=TARGET))
        self.assertBlocked()

    def test_a_later_diagnosis_without_a_pull_request_record_still_gates(self):
        self.patch('3910baa3', status='waiting', _waiting_on=['51b30b3d'])
        self.patch('51b30b3d', status='working')
        self.assertBlocked()

    def test_a_task_without_a_pull_request_record_is_unknown_and_gates(self):
        self.receipt('unknown-wait', EXPERT, _wait_task_id=SETUP)
        self.assertBlocked()

    def test_unknown_work_below_the_waiting_owner_still_gates(self):
        self.deliver_sibling_wait()
        self.assertOpen()
        self.receipt('expert-wait', EXPERT, _wait_task_id='f21ea9d7')
        self.assertBlocked()

    def sibling_artifacts(self, **fields):
        for artifact in self.service._all('artifacts'):
            if artifact['url'] == SIBLING and artifact['bot_id'] == self.bots['Gumroad Dev']['id']:
                self.service._put('artifacts', dict(artifact, **fields))

    def test_a_sibling_record_without_an_observation_does_not_certify_the_wait(self):
        self.sibling_artifacts(observed_at=None)
        self.assertBlocked()

    def test_a_sibling_record_of_another_bot_does_not_certify_the_wait(self):
        self.sibling_artifacts(bot_id=self.bots['Coconut Care']['id'])
        self.assertBlocked()

    def test_one_unrecorded_pull_request_makes_the_owner_ambiguous(self):
        self.patch(SETUP, _reported_pr_urls=[SIBLING, 'https://github.com/example/other/pull/9'])
        self.assertBlocked()

    def test_a_report_and_the_bots_tracking_alone_do_not_certify_the_wait(self):
        # The bot tracks the sibling PR and the owner reported it, but the record was not created in the owner's lineage.
        self.sibling_artifacts(task_id='29a01294', _review_task_id='29a01294')
        self.assertBlocked()
        self.sibling_artifacts(task_id='a43e8fe6', _review_task_id='a43e8fe6')
        self.assertOpen()

    def test_an_owner_without_any_pull_request_claim_is_ambiguous(self):
        self.patch(SETUP, _reported_pr_urls=[])
        self.assertBlocked()

    def test_a_stale_sibling_claim_loses_to_a_current_target_record(self):
        artifact = next(a for a in self.service._all('artifacts') if a['url'] == TARGET and a['bot_id'] == self.bots['Gumroad Dev']['id'])
        self.service._put('artifacts', dict(artifact, _activity_task_id=SETUP))
        self.assertBlocked()

    def test_a_wait_for_a_task_with_target_records_still_gates(self):
        self.receipt('sibling-wait', SETUP, _wait_task_id='f21ea9d7')
        self.assertBlocked()

    def test_an_unfinished_wait_target_still_gates(self):
        self.patch(EXPERT, status='working')
        self.assertBlocked()

    def test_a_wait_target_that_is_not_the_owners_delegation_and_has_no_record_is_ambiguous(self):
        self.patch(EXPERT, source_task_id=None, _requester_reply_task_id=None)
        self.assertBlocked()

    def test_only_a_held_owner_can_have_its_wait_ignored(self):
        # A task that a drain can resume reads its result first, so the gate waits for that delivery.
        self.patch(SETUP, status='completed')
        self.assertBlocked()
        self.patch(SETUP, status='waiting')
        self.assertBlocked()
        self.patch(SETUP, status='needs_you')
        self.assertOpen()
        self.service._put('decisions', dict(self.sibling_decision, status='dismissed'))
        self.assertBlocked()

    def test_a_pending_decision_of_another_task_does_not_hold_the_owner(self):
        self.service._put('decisions', dict(self.sibling_decision, task_id=EXPERT))
        self.assertBlocked()

    def test_a_sibling_turn_of_the_shared_root_with_an_unread_wait_still_gates(self):
        self.deliver_sibling_wait()
        self.receipt('desk-wait', '7141aa74', _wait_task_id=EXPERT)
        self.assertBlocked()

    def test_a_sibling_task_keeps_every_other_unread_effect_in_the_gate(self):
        self.assertOpen()
        effects = {
            'terminal for unknown work': dict(action=dict(name='terminal', arguments={}),
                                              result=json.dumps(dict(session_id='unknown', notify_on_complete=True))),
            'terminal for the target review': dict(action=dict(name='terminal', arguments={}),
                                                   result=json.dumps(dict(session_id='review', notify_on_complete=True))),
            'dispatched delegation': dict(action=dict(name='delegate_task', arguments={}),
                                          result=json.dumps(dict(status='dispatched', delegation_id='delegated'))),
            'relay for unknown work': dict(action=dict(name='message_agent', arguments={}), _relay_task_id='unknown'),
            'relay for the target review': dict(action=dict(name='message_agent', arguments={}), _relay_task_id='f21ea9d7'),
            'process result wait': dict(_wait_process_receipt='some-process'),
            'decision wait': dict(_wait_decision_id='some-decision'),
            'wait fields on another tool': dict(_wait_task_id=EXPERT, action=dict(name='terminal', arguments={})),
        }
        for name, fields in effects.items():
            with self.subTest(name):
                record = self.receipt('effect', SETUP, **fields)
                self.assertBlocked()
                # Once consumed, the same receipt no longer gates.
                self.service._put('native_receipts', dict(record, _process_delivery='consumed'))
                self.assertOpen()

    def test_a_wait_receipt_that_is_also_a_relay_process_or_decision_wait_is_never_exempt(self):
        self.patch(EXPERT, _native_relay_result=dict(status='completed'))
        self.receipt('process-source', EXPERT, _completion_text='The process finished.')
        self.service._put('decisions', dict(self.sibling_decision, id='replied-decision', task_id=EXPERT, status='replied'))
        for name, fields in {'relay': dict(_relay_task_id=EXPERT), 'process': dict(_wait_process_receipt='process-source'),
                             'decision': dict(_wait_decision_id='replied-decision')}.items():
            with self.subTest(name):
                self.receipt('sibling-wait', SETUP, _wait_task_id=EXPERT, **fields)
                self.assertBlocked()
        self.receipt('sibling-wait', SETUP, _wait_task_id=EXPERT)
        self.assertOpen()

    def test_a_failed_wait_target_still_gates(self):
        self.patch(EXPERT, status='failed')
        self.assertBlocked()

    def test_a_wait_target_with_an_unrecorded_pull_request_is_ambiguous(self):
        self.patch(EXPERT, _reported_pr_urls=['https://github.com/example/other/pull/9'])
        self.assertBlocked()

    def test_a_wait_target_with_its_own_observed_record_does_not_need_to_be_a_delegation(self):
        self.patch(EXPERT, source_task_id=None, _requester_reply_task_id=None)
        self.assertBlocked()
        self.service._put('artifacts', dict(
            id='care-sibling', bot_id=self.bots['Coconut Care']['id'], task_id=EXPERT, _review_task_id=EXPERT, kind='pr',
            url=SIBLING, title='Change', status='open', pr_state='open', head_sha='a' * 40,
            created_at='2026-10-01T14:00:00.000000Z', observed_at='2026-10-01T15:00:00.000000Z'))
        self.assertOpen()

    def test_an_owner_sharing_the_execution_of_a_target_task_is_not_exempt(self):
        self.patch(SETUP, _execution_session_id='f21ea9d7')
        self.assertBlocked()

    @unittest.skipUnless(hasattr(Service, '_pr_inert_waits'), 'The pinned source has no proof helper to test.')
    def test_the_proof_covers_only_the_wait_receipt_and_never_the_requester(self):
        def inert(**fields):
            tasks = {t['id']: t for t in self.service._all('tasks')}
            return self.service._pr_inert_waits(TARGET, tasks, self.service._work_receipts(),
                                                fields.get('keep', {CALLER, ROOT}))
        self.assertEqual(inert(), {'sibling-wait'})
        self.assertEqual(inert(keep={SETUP}), set())
        # A target that still has its own unread process result has not delivered its result.
        self.receipt('expert-process', EXPERT, action=dict(name='terminal', arguments={}),
                     result=json.dumps(dict(session_id='expert', notify_on_complete=True)))
        self.assertEqual(inert(), set())

    def test_active_background_completion_of_a_sibling_task_still_gates(self):
        for status in ('queued', 'working', 'needs_you', 'paused'):
            with self.subTest(status):
                self.patch(SETUP, status=status, _background_completion=['The review finished.'])
                self.assertBlocked()
        self.patch(SETUP, status='needs_you')
        self.patch(SETUP, _background_completion=None)
        self.assertOpen()

    def test_the_wait_list_of_a_sibling_task_still_gates(self):
        self.patch(SETUP, _waiting_on=[EXPERT])
        self.assertBlocked()

    def test_a_queued_answer_for_a_sibling_turn_of_the_root_still_gates(self):
        self.patch('7141aa74', status='queued', _background_completion=['The setup finished.'])
        self.assertBlocked()

    def test_a_second_unread_wait_of_the_owner_keeps_it_in_the_gate(self):
        self.assertOpen()
        self.receipt('second-wait', SETUP, _wait_task_id='f21ea9d7')
        self.assertBlocked()

    def test_the_task_that_asks_for_the_card_is_never_exempt(self):
        self.patch(CALLER, _reported_pr_urls=[SIBLING])
        self.receipt('caller-wait', CALLER, _wait_task_id=EXPERT)
        self.assertBlocked()

SOURCE_REVISION = '2026-10-01T16:00:00Z'


class MergeGateNativeReceiptBoundaryTests(unittest.TestCase):
    """The same saved graph, asked through the native receipt route that a Hermes worker uses.

    The caller records a coconut_ask receipt with Service.emit('native_call') and then sends the question with
    Service.emit('native_question') and that receipt. The service binds the question to the calling task, its
    working status, and the executing receipt, then validates the merge card in the same transaction.
    """

    write = MergeGatePullRequestScopeTests.write
    patch = MergeGatePullRequestScopeTests.patch
    receipt = MergeGatePullRequestScopeTests.receipt
    saved = MergeGatePullRequestScopeTests.saved
    tearDown = MergeGatePullRequestScopeTests.tearDown

    def setUp(self):
        MergeGatePullRequestScopeTests.setUp(self)
        # The card's GitHub source check reads one issue revision and never leaves the isolated database.
        self.service.pr_tracker = type('Tracker', (), {
            'observe': lambda _, url: None,
            'observe_discussion': lambda _, url: None,
            'observe_issue': lambda _, url: dict(updated_at=SOURCE_REVISION)})()
        self.patch(CALLER, status='working')
        self.payload = dict(title='Merge the target change?', merge_pr_url=TARGET, risk_level='low', risk='A bad merge reaches main.',
                            options=[dict(id='merge', label='Merge', explanation='Merge the reviewed change.')])

    def raw(self, table, identity):
        row = self.service.db.execute(f'SELECT data FROM {table} WHERE id=?', (identity,)).fetchone()
        return row[0] if row else None

    def bytes_of(self, table):
        return dict(self.service.db.execute(f'SELECT id, data FROM {table} ORDER BY id').fetchall())

    def call(self, task_id=CALLER):
        return self.service.emit(task_id, 'native_call', {'name': 'coconut_ask', 'arguments': self.payload})

    def ask(self, receipt_id, task_id=CALLER, **fields):
        payload = dict(self.payload, source_revisions={TARGET: SOURCE_REVISION}, **fields)
        if receipt_id is not None:
            payload['receipt_id'] = receipt_id
        return self.service.emit(task_id, 'native_question', payload)

    def assertCreated(self, receipt_id):
        try:
            self.ask(receipt_id)
        except YieldForDecision as paused:
            return self.service._get('decisions', paused.decision_id)
        except ServiceError as error:
            self.fail(f'The native question for {TARGET} was refused at the provider boundary: {error}')
        self.fail('The native question did not pause for a decision.')

    def assertRefused(self, receipt_id, status, pattern, caller=CALLER):
        with self.assertRaisesRegex(ServiceError, pattern) as error:
            self.ask(receipt_id, task_id=caller)
        self.assertEqual(error.exception.status, status)

    def test_the_saved_topology_creates_the_target_card_through_the_native_receipt_route(self):
        call = self.call()
        self.assertEqual(call['status'], 'recorded')
        sibling = {name: self.bytes_of(name) for name in ('native_receipts', 'decisions')}
        sibling_tasks = {t['id']: self.raw('tasks', t['id']) for t in self.service._all('tasks') if t['id'] != CALLER}
        before_receipt = self.raw('native_receipts', call['id'])
        decision = self.assertCreated(call['id'])
        self.assertEqual((decision['status'], decision['task_id'], decision['merge_pr_url']), ('pending', CALLER, TARGET))
        self.assertEqual(decision['_receipt_id'], call['id'])
        # The caller now waits for the person, and its receipt waits for the answer.
        self.assertEqual(self.service._get('tasks', CALLER)['status'], 'needs_you')
        receipt = self.service._get('native_receipts', call['id'])
        self.assertEqual(receipt['state'], 'awaiting_answer')
        self.assertEqual({k: v for k, v in receipt.items() if k != 'state'},
                         {k: v for k, v in json.loads(before_receipt).items() if k != 'state'})
        # The sibling work is untouched, byte for byte: its decision, its undelivered wait, and every other task.
        self.assertEqual(self.service._get('tasks', SETUP)['status'], 'needs_you')
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        self.assertNotIn('_process_delivery', self.service._get('native_receipts', 'sibling-wait'))
        self.assertEqual(self.bytes_of('decisions'), dict(sibling['decisions'], **{decision['id']: self.raw('decisions', decision['id'])}))
        self.assertEqual({k: v for k, v in self.bytes_of('native_receipts').items() if k != call['id']},
                         {k: v for k, v in sibling['native_receipts'].items() if k != call['id']})
        self.assertEqual({t['id']: self.raw('tasks', t['id']) for t in self.service._all('tasks') if t['id'] != CALLER}, sibling_tasks)

    def test_an_unread_result_of_the_same_pr_refuses_the_card_and_changes_nothing_until_consumed(self):
        record = self.receipt('target-review', 'f21ea9d7', action=dict(name='terminal', arguments={}),
                              result=json.dumps(dict(session_id='review', notify_on_complete=True)))
        call = self.call()
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions', 'artifacts')}
        self.assertRefused(call['id'], 400, PENDING)
        # The refusal rolls back the whole receipt route: no card, no receipt state, no caller status change.
        self.assertEqual({name: self.bytes_of(name) for name in before}, before)
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        self.assertEqual(self.service._get('tasks', CALLER)['status'], 'working')
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        # Reading the result opens the same receipt for the same question.
        self.service._put('native_receipts', dict(record, _process_delivery='consumed'))
        decision = self.assertCreated(call['id'])
        self.assertEqual(decision['merge_pr_url'], TARGET)

    def test_a_pending_wait_of_the_same_pr_refuses_the_card_without_a_receipt_link_too(self):
        self.receipt('target-wait', 'f21ea9d7', _wait_task_id=EXPERT)
        # A question without receipt_id is also validated: leaving the receipt out is no way around the gate.
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions')}
        self.assertRefused(None, 400, PENDING)
        self.assertEqual({name: self.bytes_of(name) for name in before}, before)
        call = self.call()
        self.assertRefused(call['id'], 400, PENDING)
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        self.assertEqual(self.service._all('decisions'), [self.sibling_decision])

    def test_a_sibling_effect_still_refuses_the_card_through_the_native_receipt_route(self):
        self.receipt('sibling-effect', SETUP, action=dict(name='terminal', arguments={}),
                     result=json.dumps(dict(session_id='unknown', notify_on_complete=True)))
        call = self.call()
        self.assertRefused(call['id'], 400, PENDING)
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)

    def test_a_caller_cannot_use_the_receipt_of_another_task(self):
        call = self.call()
        self.patch('f21ea9d7', status='working')
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions')}
        self.assertRefused(call['id'], 409, 'does not match this tool call', caller='f21ea9d7')
        self.assertEqual({name: self.bytes_of(name) for name in before}, before)
        self.assertEqual(self.service._get('native_receipts', call['id'])['task_id'], CALLER)

    def test_a_missing_or_unknown_receipt_or_caller_is_refused(self):
        call = self.call()
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions')}
        for name, args, status in (('unknown receipt', dict(receipt_id='no-such-receipt'), 404),
                                   ('unknown caller', dict(receipt_id=call['id'], task_id='no-such-task'), 404)):
            with self.subTest(name):
                with self.assertRaises(ServiceError) as error:
                    self.ask(args['receipt_id'], task_id=args.get('task_id', CALLER))
                self.assertEqual(error.exception.status, status)
        self.assertEqual({name: self.bytes_of(name) for name in before}, before)

    def test_a_caller_that_is_not_working_cannot_execute_the_call_or_the_question(self):
        call = self.call()
        for status in ('needs_you', 'waiting', 'completed'):
            with self.subTest(status):
                self.patch(CALLER, status=status)
                before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions')}
                self.assertRefused(call['id'], 409, 'cannot execute a tool now')
                with self.assertRaisesRegex(ServiceError, 'cannot execute a tool now') as error:
                    self.call()
                self.assertEqual(error.exception.status, 409)
                self.assertEqual({name: self.bytes_of(name) for name in before}, before)
        # The held sibling task cannot ask either, and its saved state stays as it was.
        before = self.bytes_of('tasks')[SETUP]
        with self.assertRaisesRegex(ServiceError, 'cannot execute a tool now'):
            self.call(SETUP)
        self.assertEqual(self.bytes_of('tasks')[SETUP], before)

    def test_a_receipt_that_was_already_used_cannot_create_a_second_card(self):
        call = self.call()
        decision = self.assertCreated(call['id'])
        self.patch(CALLER, status='working')
        self.assertRefused(call['id'], 409, 'does not match this tool call')
        self.assertEqual([d['id'] for d in self.service._all('decisions') if d.get('merge_pr_url') == TARGET], [decision['id']])


class MergeGateTargetDelegationTests(unittest.TestCase):
    """A worker that the target review delegated to has its own execution and may claim a sibling PR.

    It still works for the target review. Neither its own claim nor a lineage that also reaches the sibling work
    through the mixed intake can make its unread wait inert. Only the shared human-origin intake is not target work.
    """

    write = MergeGatePullRequestScopeTests.write
    patch = MergeGatePullRequestScopeTests.patch
    receipt = MergeGatePullRequestScopeTests.receipt
    saved = MergeGatePullRequestScopeTests.saved
    validate = MergeGatePullRequestScopeTests.validate
    assertOpen = MergeGatePullRequestScopeTests.assertOpen
    assertBlocked = MergeGatePullRequestScopeTests.assertBlocked
    tearDown = MergeGatePullRequestScopeTests.tearDown
    raw = MergeGateNativeReceiptBoundaryTests.raw
    bytes_of = MergeGateNativeReceiptBoundaryTests.bytes_of
    call = MergeGateNativeReceiptBoundaryTests.call
    ask = MergeGateNativeReceiptBoundaryTests.ask
    assertCreated = MergeGateNativeReceiptBoundaryTests.assertCreated

    def setUp(self):
        MergeGateNativeReceiptBoundaryTests.setUp(self)

    def assertRefusedCard(self, receipt_id):
        try:
            self.ask(receipt_id)
        except YieldForDecision:
            self.fail('The provider boundary created the merge card while a target delegate holds an unread wait.')
        except ServiceError as error:
            self.assertEqual(error.status, 400)
            self.assertRegex(str(error), PENDING)
        else:
            self.fail('The native question did not pause for a decision.')

    def delegate(self, identity, source, status='completed', **fields):
        """A task with its own execution that its delegator created, as a native relay does."""
        task = copy.deepcopy(self.service._get('tasks', 'f21ea9d7'))
        task.update(id=identity, source_task_id=source, parent_task_id=source, _requester_reply_task_id=source,
                    _execution_session_id=identity, _native_relay=True, _is_return=False, status=status,
                    _reported_pr_urls=[], _waiting_on=[], created_at='2026-10-01T15:30:00.000000Z')
        for key in ('_assignment_task_id', '_relay_callback_owner', '_resolved_dependency_id', '_pr_review'):
            task.pop(key, None)
        task.update(fields)
        self.service._put('tasks', task)
        return task

    def hold(self, identity, helper='target-helper'):
        """The worker needs a person and holds a finished, unread wait on its own helper."""
        self.delegate(helper, identity, bot_id=self.service._get('tasks', identity)['bot_id'])
        self.service._put('decisions', dict(self.sibling_decision, id='hold-' + identity, task_id=identity))
        return self.receipt('wait-' + identity, identity, _wait_task_id=helper)

    def worker(self, identity='target-worker', source='f21ea9d7', claims=(SIBLING,), **fields):
        return self.delegate(identity, source, status='needs_you', _reported_pr_urls=list(claims), **fields)

    def test_the_saved_shared_intake_keeps_the_historical_positive_beside_a_delegated_worker(self):
        self.assertOpen()
        self.worker()
        self.hold('target-worker')
        self.assertBlocked()
        # Reading the target worker's result opens the gate again, and the unrelated sibling wait stays exempt.
        self.service._put('native_receipts', dict(self.service._get('native_receipts', 'wait-target-worker'),
                                                  _process_delivery='consumed'))
        self.assertOpen()
        self.assertNotIn('_process_delivery', self.service._get('native_receipts', 'sibling-wait'))

    def test_the_lineage_of_a_target_delegate_reaches_the_sibling_creator_but_is_not_enough(self):
        worker = self.worker()
        tasks = {t['id']: t for t in self.service._all('tasks')}
        lineage, pending = set(), [worker['id']]
        while pending:
            identity = pending.pop()
            if identity not in lineage and identity in tasks:
                lineage.add(identity)
                pending.extend(tasks[identity].get(f) for f in ('source_task_id', 'parent_task_id', '_execution_session_id'))
        self.assertIn('a43e8fe6', lineage)
        self.hold('target-worker')
        self.assertBlocked()

    def test_an_intermediate_worker_that_claims_only_the_sibling_does_not_waive_the_target_delegation(self):
        self.worker('intermediate')
        self.worker('target-worker', source='intermediate')
        self.hold('target-worker')
        self.assertBlocked()

    def test_a_delegated_worker_that_claims_both_prs_makes_its_own_delegates_target_work(self):
        # The mixed worker came from the sibling side, so only its own target claim ties its delegates to this PR.
        self.worker('mixed-worker', source='a43e8fe6', claims=(SIBLING, TARGET))
        self.worker('target-worker', source='mixed-worker')
        self.hold('target-worker')
        self.assertBlocked()

    def test_a_shared_intake_with_an_unobserved_claim_is_ambiguous_and_keeps_gating(self):
        self.assertOpen()
        self.patch(ROOT, _reported_pr_urls=[SIBLING, TARGET, 'https://github.com/example/other/pull/9'])
        self.assertBlocked()

    def test_a_wait_on_a_target_delegate_with_a_sibling_claim_still_gates(self):
        self.assertOpen()
        self.delegate('target-helper', 'f21ea9d7', bot_id=self.bots['Gumroad Dev']['id'], _reported_pr_urls=[SIBLING])
        self.receipt('sibling-wait', SETUP, _wait_task_id='target-helper')
        self.assertBlocked()

    def test_a_task_that_a_target_delegate_waits_on_is_target_work_whatever_its_own_claims(self):
        self.assertOpen()
        self.worker()
        # The helper came from the sibling side and has its own observed sibling record. The wait still names it.
        self.delegate('sibling-helper', SETUP, bot_id=self.bots['Gumroad Dev']['id'], _reported_pr_urls=[SIBLING])
        self.service._put('decisions', dict(self.sibling_decision, id='hold-target-worker', task_id='target-worker'))
        self.receipt('wait-target-worker', 'target-worker', _wait_task_id='sibling-helper')
        self.assertBlocked()

    def test_a_delegate_of_a_human_origin_task_that_only_serves_this_pr_still_gates(self):
        self.assertOpen()
        # A person started the target review itself. Having no other PR record, it is not a shared intake.
        self.patch('f21ea9d7', source_task_id=None, parent_task_id=None)
        self.worker()
        self.hold('target-worker')
        self.assertBlocked()

    def test_a_delegate_of_an_unlinked_human_origin_target_task_still_gates(self):
        self.assertOpen()
        # The intake hangs from sibling work, so no other task tied to this PR leads to its delegate.
        self.delegate('target-intake', 'a43e8fe6', source_task_id=None, parent_task_id=None,
                      _reported_pr_urls=[TARGET], _requester_reply_task_id='a43e8fe6')
        self.worker(source='target-intake', parent_task_id='a43e8fe6')
        self.hold('target-worker')
        self.assertBlocked()

    def test_the_native_receipt_route_refuses_a_card_while_a_target_delegate_holds_an_unread_wait(self):
        self.worker()
        self.hold('target-worker')
        call = self.call()
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions', 'artifacts')}
        self.assertRefusedCard(call['id'])
        self.assertEqual({name: self.bytes_of(name) for name in before}, before)
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        self.assertEqual(self.service._get('tasks', CALLER)['status'], 'working')
        # The sibling decision, its undelivered wait, and its status are the same as before the request.
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        self.assertNotIn('_process_delivery', self.service._get('native_receipts', 'sibling-wait'))
        self.assertEqual(self.service._get('tasks', SETUP)['status'], 'needs_you')
        # The same receipt creates the card once the target worker reads its result.
        self.service._put('native_receipts', dict(self.service._get('native_receipts', 'wait-target-worker'),
                                                  _process_delivery='consumed'))
        decision = self.assertCreated(call['id'])
        self.assertEqual((decision['merge_pr_url'], decision['status']), (TARGET, 'pending'))
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)


class MergeGateTargetReviewIdentityTests(unittest.TestCase):
    """A review that a person started for this PR, and that also holds a record of a sibling PR.

    Holding the sibling record makes it look like the shared intake of the saved graph. Its recorded review of this
    PR is what tells them apart: the PR monitor's current review (`_pr_review`) or the reviewed heads that the
    monitor saved for the review owner's PR record. The saved shared Desk root has neither.
    """

    write = MergeGatePullRequestScopeTests.write
    patch = MergeGatePullRequestScopeTests.patch
    receipt = MergeGatePullRequestScopeTests.receipt
    saved = MergeGatePullRequestScopeTests.saved
    validate = MergeGatePullRequestScopeTests.validate
    assertOpen = MergeGatePullRequestScopeTests.assertOpen
    assertBlocked = MergeGatePullRequestScopeTests.assertBlocked
    tearDown = MergeGatePullRequestScopeTests.tearDown
    raw = MergeGateNativeReceiptBoundaryTests.raw
    bytes_of = MergeGateNativeReceiptBoundaryTests.bytes_of
    call = MergeGateNativeReceiptBoundaryTests.call
    assertCreated = MergeGateNativeReceiptBoundaryTests.assertCreated
    delegate = MergeGateTargetDelegationTests.delegate
    hold = MergeGateTargetDelegationTests.hold
    worker = MergeGateTargetDelegationTests.worker
    assertRefusedCard = MergeGateTargetDelegationTests.assertRefusedCard
    ask = MergeGateNativeReceiptBoundaryTests.ask
    setUp = MergeGateTargetDelegationTests.setUp

    def person_started_review(self, **fields):
        """The target review is started by a person and also reports the sibling PR."""
        self.patch('f21ea9d7', source_task_id=None, parent_task_id=None, _reported_pr_urls=[SIBLING, TARGET], **fields)
        # It also created an observed record of the sibling PR, so its delegates have that creator in their lineage.
        self.service._put('artifacts', dict(
            id='review-sibling', bot_id=self.bots['Gumroad Dev']['id'], task_id='f21ea9d7', _review_task_id='f21ea9d7',
            kind='pr', url=SIBLING, title='Change', status='open', pr_state='open', head_sha='a' * 40,
            created_at='2026-10-01T14:00:00.000000Z', observed_at='2026-10-01T15:00:00.000000Z'))

    def target_record(self, **fields):
        for artifact in self.service._all('artifacts'):
            if artifact['url'] == TARGET and artifact['bot_id'] == self.bots['Gumroad Dev']['id']:
                self.service._put('artifacts', dict(artifact, **fields))

    def held_worker(self):
        self.worker()
        self.hold('target-worker')

    def test_a_current_review_of_the_pr_keeps_a_person_started_review_in_the_target_work(self):
        self.assertOpen()
        self.person_started_review(_pr_review=dict(url=TARGET, pr_state='open', head_sha='a' * 40))
        self.held_worker()
        self.assertBlocked()

    def test_the_review_of_an_older_head_is_still_a_recorded_review_of_the_pr(self):
        self.person_started_review(_pr_review=dict(url=TARGET, pr_state='open', head_sha='b' * 40))
        self.held_worker()
        self.assertBlocked()

    def test_the_reviewed_head_saved_on_its_pr_record_identifies_the_review(self):
        self.person_started_review()
        self.target_record(_reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.held_worker()
        self.assertBlocked()

    def test_an_earlier_reviewed_head_saved_on_its_pr_record_identifies_the_review(self):
        self.person_started_review()
        self.target_record(_reviewed_head=None, _reviewed_heads=['b' * 40])
        self.held_worker()
        self.assertBlocked()

    def test_a_sibling_record_that_names_the_review_does_not_make_it_a_shared_intake(self):
        self.person_started_review(_pr_review=dict(url=TARGET, pr_state='open', head_sha='a' * 40))
        self.patch('f21ea9d7', _reported_pr_urls=[TARGET])
        self.held_worker()
        self.assertBlocked()

    def test_helper_work_that_follows_the_target_reviews_delegate_stays_in_the_target_work(self):
        self.person_started_review(_pr_review=dict(url=TARGET, pr_state='open', head_sha='a' * 40))
        self.worker()
        # The helper holds a person's decision and a finished wait on the next helper, and both are outside the claims.
        self.delegate('first-helper', 'target-worker', status='needs_you', bot_id=self.bots['Gumroad Dev']['id'],
                      _reported_pr_urls=[SIBLING])
        self.delegate('next-helper', 'first-helper', bot_id=self.bots['Gumroad Dev']['id'])
        self.service._put('decisions', dict(self.sibling_decision, id='hold-first-helper', task_id='first-helper'))
        self.receipt('wait-first-helper', 'first-helper', _wait_task_id='next-helper')
        self.assertBlocked()

    def test_the_saved_shared_intake_stays_exempt_when_the_target_review_is_recorded_on_its_own_review(self):
        # The saved live state: the real target review f21ea9d7 has its current review and reviewed head.
        self.patch('f21ea9d7', _pr_review=dict(url=TARGET, pr_state='open', head_sha='a' * 40))
        self.target_record(_reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.assertOpen()
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)

    def test_a_recorded_review_of_another_bots_record_does_not_make_the_intake_a_target_review(self):
        self.target_record(_review_task_id=ROOT, _reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.assertOpen()

    def test_reviewed_heads_saved_for_another_review_owner_do_not_name_this_task(self):
        self.person_started_review()
        self.target_record(_review_task_id='65e24636', _reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.held_worker()
        self.assertOpen()

    def test_a_person_started_task_with_no_review_record_cannot_be_told_from_the_saved_intake(self):
        # Without a recorded review, only its claims remain, and they match the saved mixed Desk root exactly.
        self.person_started_review()
        self.worker()
        self.hold('target-worker')
        self.assertOpen()

    def test_the_native_receipt_route_refuses_a_card_for_a_person_started_review_delegate(self):
        self.person_started_review(_pr_review=dict(url=TARGET, pr_state='open', head_sha='a' * 40))
        self.held_worker()
        call = self.call()
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions', 'artifacts')}
        self.assertRefusedCard(call['id'])
        self.assertEqual({name: self.bytes_of(name) for name in before}, before)
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        self.assertEqual(self.service._get('tasks', CALLER)['status'], 'working')
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        self.assertNotIn('_process_delivery', self.service._get('native_receipts', 'sibling-wait'))
        self.assertEqual(self.service._get('tasks', SETUP)['status'], 'needs_you')
        # Once the target worker reads its result, the same receipt creates the card.
        self.service._put('native_receipts', dict(self.service._get('native_receipts', 'wait-target-worker'),
                                                  _process_delivery='consumed'))
        decision = self.assertCreated(call['id'])
        self.assertEqual((decision['merge_pr_url'], decision['status']), (TARGET, 'pending'))
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)


class MergeGateReviewTransferTests(unittest.TestCase):
    """The monitor's records of a person-started target review can move to another task.

    The review's own `_pr_review` is cleared when the task starts its next turn, and the PR record's
    `_review_task_id` moves when the review is transferred or a new execution continues it. The saved reviewed
    heads stay on the record, and the review stays the creator of that record.
    """

    write = MergeGatePullRequestScopeTests.write
    patch = MergeGatePullRequestScopeTests.patch
    receipt = MergeGatePullRequestScopeTests.receipt
    saved = MergeGatePullRequestScopeTests.saved
    validate = MergeGatePullRequestScopeTests.validate
    assertOpen = MergeGatePullRequestScopeTests.assertOpen
    assertBlocked = MergeGatePullRequestScopeTests.assertBlocked
    tearDown = MergeGatePullRequestScopeTests.tearDown
    raw = MergeGateNativeReceiptBoundaryTests.raw
    bytes_of = MergeGateNativeReceiptBoundaryTests.bytes_of
    call = MergeGateNativeReceiptBoundaryTests.call
    ask = MergeGateNativeReceiptBoundaryTests.ask
    assertCreated = MergeGateNativeReceiptBoundaryTests.assertCreated
    delegate = MergeGateTargetDelegationTests.delegate
    hold = MergeGateTargetDelegationTests.hold
    worker = MergeGateTargetDelegationTests.worker
    assertRefusedCard = MergeGateTargetDelegationTests.assertRefusedCard
    person_started_review = MergeGateTargetReviewIdentityTests.person_started_review
    target_record = MergeGateTargetReviewIdentityTests.target_record
    held_worker = MergeGateTargetReviewIdentityTests.held_worker
    setUp = MergeGateTargetDelegationTests.setUp

    def record(self):
        return next(a for a in self.service._all('artifacts')
                    if a['url'] == TARGET and a['bot_id'] == self.bots['Gumroad Dev']['id'])

    def transfer_review(self):
        """Ownership of the target PR record moves to a new review task through the service's own transfer path."""
        self.delegate('review-owner', 'a43e8fe6', status='working', source_task_id=None, parent_task_id=None,
                      bot_id=self.bots['Gumroad Dev']['id'])
        self.service._register_pr_artifact(None, dict(url=TARGET, review_task_id='review-owner'),
                                           bot_id=self.bots['Gumroad Dev']['id'])
        record = self.record()
        # The records now sit on the new owner, and the person-started review has no current request.
        self.assertEqual(record['_review_task_id'], 'review-owner')
        self.assertTrue(record.get('_review_ownership_at'))
        self.assertEqual(record['task_id'], 'f21ea9d7')
        self.assertTrue(record.get('_reviewed_heads'))
        self.assertNotIn('_pr_review', self.service._get('tasks', 'f21ea9d7'))

    def test_a_transferred_review_keeps_its_delegates_unread_wait_in_the_gate(self):
        self.assertOpen()
        self.person_started_review()
        self.target_record(_reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.transfer_review()
        self.worker()
        self.hold('target-worker')
        self.assertBlocked()

    def test_the_review_stays_in_the_target_work_after_its_current_request_is_cleared_and_moved(self):
        self.person_started_review(_pr_review=dict(url=TARGET, pr_state='open', head_sha='a' * 40))
        self.target_record(_reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.held_worker()
        self.assertBlocked()
        # The review's next turn clears its request, and the record moves to a continuing task.
        task = self.service._get('tasks', 'f21ea9d7')
        task.pop('_pr_review')
        with self.service.transaction():
            self.service._put('tasks', task)
        self.transfer_review()
        self.assertBlocked()

    def test_a_continuation_that_takes_over_the_review_keeps_its_delegates_unread_wait_in_the_gate(self):
        self.person_started_review()
        self.target_record(_reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        # The service moves the record only while all linked work is quiet, so release the held sibling work first.
        held = {table: self.service._get(table, identity) for table, identity in
                (('decisions', 'xcode-decision'), ('native_receipts', 'sibling-wait'), ('tasks', SETUP))}
        self.service._put('decisions', dict(held['decisions'], status='dismissed'))
        self.service._put('native_receipts', dict(held['native_receipts'], _process_delivery='delivered'))
        self.patch(SETUP, status='completed')
        self.patch(CALLER, status='completed')
        continuation = self.delegate('review-continuation', 'a43e8fe6', status='working', source_task_id=None,
                                     parent_task_id=None, _requester_reply_task_id=None, _native_relay=False,
                                     bot_id=self.bots['Gumroad Dev']['id'])
        record = self.record()
        self.service._bind_pr_progress(record, continuation)
        self.service._put('artifacts', record)
        self.patch(CALLER, status='working')
        self.assertEqual(self.record()['_review_task_id'], 'review-continuation')
        self.assertTrue(self.record().get('_review_ownership_at'))
        # The sibling work holds again, and the review's worker holds a finished wait of its own.
        for table, record in held.items():
            self.service._put(table, record)
        self.held_worker()
        self.assertBlocked()

    def test_a_record_without_saved_heads_or_a_transfer_does_not_make_the_shared_intake_a_review(self):
        # The saved shared Desk root created the Desk record. Its review owner moved to a Desk turn, with no heads saved.
        desk = next(a for a in self.service._all('artifacts') if a['url'] == TARGET and a['bot_id'] == self.bots['Desk']['id'])
        self.assertEqual(desk['task_id'], ROOT)
        self.service._put('artifacts', dict(desk, _review_task_id='6a524539', _review_ownership_at='2026-10-01T15:00:00.000000Z'))
        self.assertOpen()

    def test_the_native_receipt_route_refuses_a_card_for_the_delegate_of_a_transferred_review(self):
        self.person_started_review()
        self.target_record(_reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.transfer_review()
        self.held_worker()
        call = self.call()
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions', 'artifacts')}
        self.assertRefusedCard(call['id'])
        self.assertEqual({name: self.bytes_of(name) for name in before}, before)
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        self.assertEqual(self.service._get('tasks', CALLER)['status'], 'working')
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        self.assertNotIn('_process_delivery', self.service._get('native_receipts', 'sibling-wait'))
        self.assertEqual(self.service._get('tasks', SETUP)['status'], 'needs_you')
        self.service._put('native_receipts', dict(self.service._get('native_receipts', 'wait-target-worker'),
                                                  _process_delivery='consumed'))
        decision = self.assertCreated(call['id'])
        self.assertEqual((decision['merge_pr_url'], decision['status']), (TARGET, 'pending'))
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)


    def second_owner(self):
        """Another person-started review of this PR, with its own sibling record, that the record's ownership passes through."""
        gumroad = self.bots['Gumroad Dev']['id']
        self.delegate('review-b', 'a43e8fe6', source_task_id=None, parent_task_id=None, _native_relay=False,
                      _requester_reply_task_id='a43e8fe6', bot_id=gumroad, _reported_pr_urls=[SIBLING, TARGET])
        self.service._put('artifacts', dict(
            id='b-sibling', bot_id=gumroad, task_id='review-b', _review_task_id='review-b', kind='pr', url=SIBLING,
            title='Change', status='open', pr_state='open', head_sha='a' * 40,
            created_at='2026-10-01T14:00:00.000000Z', observed_at='2026-10-01T15:00:00.000000Z'))
        self.delegate('review-c', 'a43e8fe6', status='working', source_task_id=None, parent_task_id=None,
                      _requester_reply_task_id=None, _native_relay=False, bot_id=gumroad)
        self.service._register_pr_artifact(None, dict(url=TARGET, review_task_id='review-b'), bot_id=gumroad)
        self.worker('b-worker', source='review-b')
        self.hold('b-worker', helper='b-helper')

    def pass_to_the_third_owner(self):
        self.service._register_pr_artifact(None, dict(url=TARGET, review_task_id='review-c'),
                                           bot_id=self.bots['Gumroad Dev']['id'])
        record = self.record()
        # The record now names only its creator and its newest owner. The middle owner is not recorded anywhere.
        self.assertEqual((record['task_id'], record['_review_task_id']), ('f21ea9d7', 'review-c'))
        self.assertTrue(record.get('_review_ownership_at'))
        self.assertTrue(record.get('_reviewed_heads'))
        self.assertNotIn('_pr_review', self.service._get('tasks', 'review-b'))

    def test_a_second_transfer_keeps_the_middle_owners_delegate_wait_in_the_gate(self):
        self.person_started_review()
        self.target_record(_reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.second_owner()
        # While the middle owner owns the record, its delegate's finished wait gates.
        self.assertEqual(self.record()['_review_task_id'], 'review-b')
        self.assertBlocked()
        self.pass_to_the_third_owner()
        self.assertBlocked()

    def test_the_native_receipt_route_refuses_a_card_after_the_second_transfer(self):
        self.person_started_review()
        self.target_record(_reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.second_owner()
        self.pass_to_the_third_owner()
        call = self.call()
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions', 'artifacts')}
        self.assertRefusedCard(call['id'])
        self.assertEqual({name: self.bytes_of(name) for name in before}, before)
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        self.assertNotIn('_process_delivery', self.service._get('native_receipts', 'sibling-wait'))
        self.service._put('native_receipts', dict(self.service._get('native_receipts', 'wait-b-worker'),
                                                  _process_delivery='consumed'))
        decision = self.assertCreated(call['id'])
        self.assertEqual((decision['merge_pr_url'], decision['status']), (TARGET, 'pending'))
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)

    def test_a_transferred_record_of_another_bot_does_not_name_the_shared_desk_intake(self):
        self.person_started_review()
        self.target_record(_reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.transfer_review()
        # The Gumroad record moved and holds heads, but the saved Desk root belongs to the Desk bot's record.
        self.assertOpen()
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)

    def test_resuming_the_work_replaces_the_review_owner_so_the_old_chain_leaves_the_gate(self):
        self.person_started_review(_pr_review=dict(url=TARGET, pr_state='open', head_sha='a' * 40))
        self.target_record(_reviewed_head='a' * 40, _reviewed_heads=['a' * 40])
        self.held_worker()
        self.assertBlocked()
        before = self.record()
        # The person cancels the work and resumes it. The service gives the review to the new task and drops the others.
        self.write(f"/v1/work/items/{before['id']}/cancel", {})
        self.write(f"/v1/work/items/{before['id']}/resume", {})
        after = self.record()
        self.assertNotEqual(after['_review_task_id'], 'f21ea9d7')
        self.assertEqual(after.get('_reviewed_heads'), [])
        self.assertTrue(all(a.get('_review_task_id') is None for a in self.service._all('artifacts')
                            if a['url'] == TARGET and a['bot_id'] != after['bot_id']))
        tasks = {t['id']: t for t in self.service._all('tasks')}
        chain = self.service._work_chain(tasks[after['_review_task_id']], tasks, self.service._work_receipts(),
                                         include_continuations=True)
        self.assertNotIn('target-worker', chain)
        self.assertOpen()
        # The resumed owner's own unread work still gates the card.
        self.receipt('resumed-wait', after['_review_task_id'], _wait_task_id=EXPERT)
        self.assertBlocked()


class MergeGateReviewHistoryTests(unittest.TestCase):
    """The PR record lists every owner it has had, from its first one, so a transfer names who reviewed.

    The records here are created by the service's own registration, not written by the test. A record that lists
    its owners names exactly them. A record without that list, or with one that is not consistent, has lost its
    earlier owners and keeps gating every task of its bot that tracks the PR.
    """

    write = MergeGatePullRequestScopeTests.write
    patch = MergeGatePullRequestScopeTests.patch
    receipt = MergeGatePullRequestScopeTests.receipt
    saved = MergeGatePullRequestScopeTests.saved
    validate = MergeGatePullRequestScopeTests.validate
    assertOpen = MergeGatePullRequestScopeTests.assertOpen
    assertBlocked = MergeGatePullRequestScopeTests.assertBlocked
    tearDown = MergeGatePullRequestScopeTests.tearDown
    raw = MergeGateNativeReceiptBoundaryTests.raw
    bytes_of = MergeGateNativeReceiptBoundaryTests.bytes_of
    call = MergeGateNativeReceiptBoundaryTests.call
    ask = MergeGateNativeReceiptBoundaryTests.ask
    assertCreated = MergeGateNativeReceiptBoundaryTests.assertCreated
    assertRefused = MergeGateNativeReceiptBoundaryTests.assertRefused
    delegate = MergeGateTargetDelegationTests.delegate
    hold = MergeGateTargetDelegationTests.hold
    worker = MergeGateTargetDelegationTests.worker
    assertRefusedCard = MergeGateTargetDelegationTests.assertRefusedCard
    record = MergeGateReviewTransferTests.record
    transfer_review = MergeGateReviewTransferTests.transfer_review
    second_owner = MergeGateReviewTransferTests.second_owner
    pass_to_the_third_owner = MergeGateReviewTransferTests.pass_to_the_third_owner
    person_started_review = MergeGateTargetReviewIdentityTests.person_started_review

    def setUp(self):
        from coconut.pr_tracking import PRObservation
        MergeGateNativeReceiptBoundaryTests.setUp(self)
        observation = PRObservation('pending', 'a' * 40, 'Change', TARGET, 'open', '2026-10-01T15:00:00Z', True, 'open',
                                    False, 'pending')
        self.service.pr_tracker = type('Tracker', (), {
            'observe': lambda _, url: observation, 'observe_discussion': lambda _, url: None,
            'observe_issue': lambda _, url: dict(updated_at=SOURCE_REVISION)})()
        # Replace the saved Gumroad record of the target with the one that registration creates for the same review.
        with self.service.transaction():
            self.service.db.execute('DELETE FROM artifacts WHERE id=?', (self.record()['id'],))
        self.patch('f21ea9d7', status='working')
        self.service._register_pr_artifact('f21ea9d7', dict(url=TARGET))
        self.patch('f21ea9d7', status='completed')
        for artifact in self.service._all('artifacts'):
            if artifact['url'] == TARGET and artifact['bot_id'] == self.bots['Gumroad Dev']['id']:
                self.service._put('artifacts', dict(artifact, _reviewed_head='a' * 40, _reviewed_heads=['a' * 40]))

    def set_record(self, **fields):
        self.service._put('artifacts', dict(self.record(), **fields))

    def shared_intake(self):
        """A person-started task of the review's own bot that tracks both PRs and never owned the review."""
        gumroad = self.bots['Gumroad Dev']['id']
        self.delegate('shared-intake', 'a43e8fe6', source_task_id=None, parent_task_id=None, _native_relay=False,
                      _requester_reply_task_id='a43e8fe6', bot_id=gumroad, _reported_pr_urls=[SIBLING, TARGET])
        self.service._put('artifacts', dict(
            id='intake-sibling', bot_id=gumroad, task_id='shared-intake', _review_task_id='shared-intake', kind='pr',
            url=SIBLING, title='Change', status='open', pr_state='open', head_sha='a' * 40,
            created_at='2026-10-01T14:00:00.000000Z', observed_at='2026-10-01T15:00:00.000000Z'))
        self.worker('intake-worker', source='shared-intake')
        self.hold('intake-worker', helper='intake-helper')

    def test_registration_starts_the_history_with_the_first_owner(self):
        self.assertEqual(self.record().get('_review_owners'), ['f21ea9d7'])
        self.assertEqual(self.record()['_review_task_id'], 'f21ea9d7')

    def test_the_history_starts_with_the_review_owner_and_not_the_task_that_reported(self):
        gumroad = self.bots['Gumroad Dev']['id']
        self.delegate('exec-owner', 'a43e8fe6', bot_id=gumroad, _native_outcome_id='t_exec-owner')
        self.delegate('exec-turn', 'a43e8fe6', status='working', bot_id=gumroad, _execution_session_id='exec-owner')
        other = 'https://github.com/example/target/pull/3171'
        self.service._register_pr_artifact('exec-turn', dict(url=other))
        record = next(a for a in self.service._all('artifacts') if a['url'] == other)
        self.assertEqual((record['task_id'], record['_review_task_id']), ('exec-turn', 'exec-owner'))
        self.assertEqual(record.get('_review_owners'), ['exec-owner'])

    def test_every_move_of_the_owner_is_appended_in_order(self):
        self.transfer_review()
        self.assertEqual(self.record().get('_review_owners'), ['f21ea9d7', 'review-owner'])
        self.service._register_pr_artifact(None, dict(url=TARGET, review_task_id='f21ea9d7'), bot_id=self.bots['Gumroad Dev']['id'])
        self.assertEqual(self.record().get('_review_owners'), ['f21ea9d7', 'review-owner', 'f21ea9d7'])
        # A repeated report by the same owner adds nothing.
        self.patch('f21ea9d7', status='working')
        self.service._register_pr_artifact('f21ea9d7', dict(url=TARGET))
        self.assertEqual(self.record().get('_review_owners'), ['f21ea9d7', 'review-owner', 'f21ea9d7'])

    def test_a_continuation_through_the_progress_path_is_appended(self):
        held = {table: self.service._get(table, identity) for table, identity in
                (('decisions', 'xcode-decision'), ('native_receipts', 'sibling-wait'), ('tasks', SETUP))}
        self.service._put('decisions', dict(held['decisions'], status='dismissed'))
        self.service._put('native_receipts', dict(held['native_receipts'], _process_delivery='delivered'))
        self.patch(SETUP, status='completed')
        self.patch(CALLER, status='completed')
        continuation = self.delegate('review-continuation', 'a43e8fe6', status='working', source_task_id=None,
                                     parent_task_id=None, _requester_reply_task_id=None, _native_relay=False,
                                     bot_id=self.bots['Gumroad Dev']['id'])
        record = self.record()
        self.service._bind_pr_progress(record, continuation)
        self.service._put('artifacts', record)
        self.assertEqual(self.record().get('_review_owners'), ['f21ea9d7', 'review-continuation'])

    def test_a_resume_appends_the_resumed_task_and_a_new_bot_record_starts_its_own_history(self):
        record = self.record()
        self.write(f"/v1/work/items/{record['id']}/cancel", {})
        self.write(f"/v1/work/items/{record['id']}/resume", {})
        record = self.record()
        owners = record.get('_review_owners') or []
        self.assertEqual((owners[:1], owners[-1:], len(owners)), (['f21ea9d7'], [record['_review_task_id']], 2))

    def test_a_same_bot_shared_intake_that_never_owned_the_review_stays_exempt_after_a_transfer(self):
        self.shared_intake()
        self.transfer_review()
        self.assertOpen()

    def test_the_card_is_created_through_the_native_receipt_route_while_that_intake_waits_unread(self):
        self.shared_intake()
        self.transfer_review()
        call = self.call()
        before = {table: self.bytes_of(table) for table in ('native_receipts', 'decisions')}
        decision = self.assertCreated(call['id'])
        self.assertEqual((decision['merge_pr_url'], decision['status'], decision['task_id']), (TARGET, 'pending', CALLER))
        # Neither the intake's unread wait, its worker, nor the sibling decision and wait changed.
        for identity in ('wait-intake-worker', 'sibling-wait'):
            self.assertEqual(self.raw('native_receipts', identity), before['native_receipts'][identity])
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        self.assertEqual(self.service._get('tasks', 'intake-worker')['status'], 'needs_you')
        self.assertEqual(self.service._get('tasks', SETUP)['status'], 'needs_you')

    def test_a_former_reviewer_stays_gated_after_repeated_transfers_through_the_native_receipt_route(self):
        self.shared_intake()
        self.second_owner()
        self.pass_to_the_third_owner()
        self.assertBlocked()
        call = self.call()
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions', 'artifacts')}
        self.assertRefusedCard(call['id'])
        self.assertEqual({name: self.bytes_of(name) for name in before}, before)
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        self.assertEqual(self.service._get('tasks', CALLER)['status'], 'working')
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        self.assertNotIn('_process_delivery', self.service._get('native_receipts', 'sibling-wait'))
        # Once the former reviewer's worker reads its result, only the intake's unread wait is left, and it is exempt.
        self.service._put('native_receipts', dict(self.service._get('native_receipts', 'wait-b-worker'),
                                                  _process_delivery='consumed'))
        self.assertEqual(self.assertCreated(call['id'])['merge_pr_url'], TARGET)
        self.assertEqual(self.record().get('_review_owners'), ['f21ea9d7', 'review-b', 'review-c'])

    def test_the_first_owner_stays_gated_after_a_transfer(self):
        self.shared_intake()
        self.transfer_review()
        self.assertOpen()
        self.worker()
        self.hold('target-worker')
        self.assertBlocked()

    def test_the_current_owner_stays_gated(self):
        self.shared_intake()
        self.transfer_review()
        self.worker('owner-worker', source='review-owner')
        self.hold('owner-worker', helper='owner-helper')
        self.assertBlocked()

    def test_unknown_or_inconsistent_history_fails_closed(self):
        self.shared_intake()
        self.transfer_review()
        self.assertOpen()
        cases = {
            'a record that predates the list': {'_review_owners': None},
            'a list that is not a list': {'_review_owners': 'f21ea9d7,review-owner'},
            'an empty list with an owner': {'_review_owners': []},
            'a list whose last owner is not the current one': {'_review_owners': ['f21ea9d7', 'review-c']},
            'a list with an entry that is not a task ID': {'_review_owners': ['f21ea9d7', None, 'review-owner']},
        }
        for name, fields in cases.items():
            with self.subTest(name):
                original = self.record()
                record = {k: v for k, v in original.items() if k != '_review_owners'}
                if fields['_review_owners'] is not None:
                    record.update(fields)
                self.service._put('artifacts', record)
                self.assertBlocked()
                self.service._put('artifacts', original)
                self.assertOpen()

    def test_an_owner_written_without_the_list_makes_the_history_unknown(self):
        self.shared_intake()
        self.transfer_review()
        self.assertOpen()
        # A move that did not append to the list leaves its last entry behind the current owner.
        self.set_record(_review_task_id='someone-else')
        self.assertBlocked()

    def test_a_record_that_moved_without_a_list_gates_every_task_of_its_bot(self):
        self.shared_intake()
        self.service._put('artifacts', {k: v for k, v in self.record().items() if k != '_review_owners'})
        self.transfer_review()
        self.assertNotIn('_review_owners', self.record())
        self.assertBlocked()

    def test_the_history_does_not_reach_another_bot(self):
        self.shared_intake()
        self.transfer_review()
        self.assertOpen()
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)

    def test_caller_binding_and_receipt_rollback_hold_beside_the_history(self):
        self.shared_intake()
        self.transfer_review()
        call = self.call()
        self.patch('f21ea9d7', status='working')
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions')}
        self.assertRefused(call['id'], 409, 'does not match this tool call', caller='f21ea9d7')
        self.assertRefused('no-such-receipt', 404, '.')
        self.patch('f21ea9d7', status='completed')
        self.patch(CALLER, status='needs_you')
        self.assertRefused(call['id'], 409, 'cannot execute a tool now')
        self.patch(CALLER, status='working')
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        self.assertCreated(call['id'])

    def person_started_owner(self, identity):
        """A person-started task of the review's bot that tracks both PRs, with its own sibling record."""
        gumroad = self.bots['Gumroad Dev']['id']
        self.delegate(identity, 'a43e8fe6', source_task_id=None, parent_task_id=None, _native_relay=False,
                      _requester_reply_task_id='a43e8fe6', bot_id=gumroad, _reported_pr_urls=[SIBLING, TARGET])
        self.service._put('artifacts', dict(
            id=identity + '-sibling', bot_id=gumroad, task_id=identity, _review_task_id=identity, kind='pr',
            url=SIBLING, title='Change', status='open', pr_state='open', head_sha='a' * 40,
            created_at='2026-10-01T14:00:00.000000Z', observed_at='2026-10-01T15:00:00.000000Z'))

    def move_to(self, identity):
        """A real supported transfer to a task that did not review before."""
        gumroad = self.bots['Gumroad Dev']['id']
        self.delegate(identity, 'a43e8fe6', status='working', source_task_id=None, parent_task_id=None,
                      _requester_reply_task_id=None, _native_relay=False, bot_id=gumroad)
        self.service._register_pr_artifact(None, dict(url=TARGET, review_task_id=identity), bot_id=gumroad)

    def assert_history_unknown(self):
        record = self.record()
        self.assertNotIn('_review_owners', record)
        self.assertTrue(record.get('_review_history_unknown'))

    def test_a_stale_list_is_not_repaired_by_the_next_move(self):
        self.shared_intake()
        self.second_owner()
        # An owner is written without the list, which no supported writer does: C owns, and the list stops at B.
        self.person_started_owner('review-c')
        self.set_record(_review_task_id='review-c')
        self.worker('c-worker', source='review-c')
        self.hold('c-worker', helper='c-helper')
        self.service._put('native_receipts', dict(self.service._get('native_receipts', 'wait-b-worker'), _process_delivery='consumed'))
        self.assertBlocked()
        # Two real moves later, C is still not in a list that looks complete, so the gate must still hold.
        for later in ('review-d', 'review-e'):
            self.move_to(later)
            self.assertBlocked()
        self.assert_history_unknown()
        self.assertEqual(self.record()['_review_task_id'], 'review-e')

    def test_an_empty_list_is_not_repaired_by_the_next_move(self):
        self.shared_intake()
        # The first owner is a person-started task, so only a list that names it can exempt its delegate.
        self.person_started_review()
        self.set_record(_review_owners=[])
        self.worker()
        self.hold('target-worker')
        self.assertBlocked()
        for later in ('review-d', 'review-e'):
            self.move_to(later)
            self.assertBlocked()
        self.assert_history_unknown()

    def test_a_list_that_is_not_trusted_is_dropped_by_the_next_move(self):
        for name, owners in {'not a list': 'f21ea9d7', 'entry that is not a task ID': ['f21ea9d7', None]}.items():
            with self.subTest(name):
                self.set_record(_review_owners=owners, _review_task_id='f21ea9d7')
                self.move_to('review-' + name.split()[0])
                self.assert_history_unknown()
                self.service._put('artifacts', {k: v for k, v in self.record().items()
                                                if k not in ('_review_history_unknown',)} | dict(_review_owners=['f21ea9d7'],
                                                                                              _review_task_id='f21ea9d7'))

    def test_a_progress_move_does_not_repair_an_inconsistent_list(self):
        self.person_started_review()
        self.set_record(_review_owners=[])
        held = {table: self.service._get(table, identity) for table, identity in
                (('decisions', 'xcode-decision'), ('native_receipts', 'sibling-wait'), ('tasks', SETUP))}
        self.service._put('decisions', dict(held['decisions'], status='dismissed'))
        self.service._put('native_receipts', dict(held['native_receipts'], _process_delivery='delivered'))
        self.patch(SETUP, status='completed')
        self.patch(CALLER, status='completed')
        continuation = self.delegate('review-continuation', 'a43e8fe6', status='working', source_task_id=None,
                                     parent_task_id=None, _requester_reply_task_id=None, _native_relay=False,
                                     bot_id=self.bots['Gumroad Dev']['id'])
        record = self.record()
        self.service._bind_pr_progress(record, continuation)
        self.service._put('artifacts', record)
        self.assertEqual(self.record()['_review_task_id'], 'review-continuation')
        # The first owner lost its place in the list, and its delegate still gates once the sibling work holds again.
        for table, record in held.items():
            self.service._put(table, record)
        self.patch(CALLER, status='working')
        self.worker()
        self.hold('target-worker')
        self.assertBlocked()
        self.assert_history_unknown()

    def test_the_native_receipt_route_refuses_a_card_for_a_former_reviewer_of_an_unknown_history(self):
        self.shared_intake()
        self.second_owner()
        self.person_started_owner('review-c')
        self.set_record(_review_task_id='review-c')
        self.worker('c-worker', source='review-c')
        self.hold('c-worker', helper='c-helper')
        self.move_to('review-d')
        self.move_to('review-e')
        for identity in ('wait-b-worker', 'wait-intake-worker'):
            self.service._put('native_receipts', dict(self.service._get('native_receipts', identity), _process_delivery='consumed'))
        call = self.call()
        before = {name: self.bytes_of(name) for name in ('tasks', 'native_receipts', 'decisions', 'artifacts')}
        self.assertRefusedCard(call['id'])
        self.assertEqual({name: self.bytes_of(name) for name in before}, before)
        self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        self.assertEqual(self.service._get('tasks', CALLER)['status'], 'working')
        self.assertEqual(self.service._get('decisions', 'xcode-decision'), self.sibling_decision)
        self.assertNotIn('_process_delivery', self.service._get('native_receipts', 'sibling-wait'))
        # Unknown history gates every task of the bot, so the card waits until the former reviewer's result is read.
        self.service._put('native_receipts', dict(self.service._get('native_receipts', 'wait-c-worker'), _process_delivery='consumed'))
        self.assertEqual(self.assertCreated(call['id'])['merge_pr_url'], TARGET)

    def test_a_complete_history_still_exempts_the_intake_after_repeated_moves(self):
        self.shared_intake()
        self.move_to('review-d')
        self.move_to('review-e')
        self.assertEqual(self.record().get('_review_owners'), ['f21ea9d7', 'review-d', 'review-e'])
        self.assertNotIn('_review_history_unknown', self.record())
        self.assertOpen()

    def test_a_record_marked_unknown_gates_every_task_of_its_bot_without_a_recorded_transfer(self):
        self.shared_intake()
        self.assertOpen()
        # A move that was not a transfer, such as a replacement or a resume, marks the record and leaves no transfer time.
        record = {k: v for k, v in self.record().items() if k not in ('_review_owners', '_review_ownership_at')}
        self.service._put('artifacts', dict(record, _review_history_unknown=True))
        self.assertNotIn('_review_ownership_at', self.record())
        self.assertBlocked()

    def test_a_detached_record_keeps_its_history_and_a_rebind_continues_it(self):
        desk = next(a for a in self.service._all('artifacts') if a['url'] == TARGET and a['bot_id'] == self.bots['Desk']['id'])
        self.write(f"/v1/work/items/{desk['id']}/cancel", {})
        self.write(f"/v1/work/items/{desk['id']}/resume", {})
        record = self.record()
        # The resumed task belongs to the Desk bot, so the Gumroad record is detached and keeps its complete list.
        self.assertIsNone(record['_review_task_id'])
        self.assertEqual(record.get('_review_owners'), ['f21ea9d7'])
        self.assertNotIn('_review_history_unknown', record)
        self.move_to('review-d')
        self.assertEqual(self.record().get('_review_owners'), ['f21ea9d7', 'review-d'])
        self.assertNotIn('_review_history_unknown', self.record())

    def test_a_record_without_a_list_is_marked_when_its_owner_leaves(self):
        self.service._put('artifacts', {k: v for k, v in self.record().items() if k != '_review_owners'})
        self.move_to('review-d')
        record = self.record()
        self.assertNotIn('_review_owners', record)
        self.assertTrue(record.get('_review_history_unknown'))
        self.assertEqual(record['_review_task_id'], 'review-d')


class MergeGateNativeContinuationHistoryTests(unittest.TestCase):
    """A native continuation of a finished review moves the PR record's owner, and the history records the move."""

    setUpClass = classmethod(lambda cls: legacy_work_sync.scope()[0]())
    tearDownClass = classmethod(lambda cls: legacy_work_sync.scope()[1]())
    setUp = continuation_fixtures.TargetedBotContinuationTests.setUp
    tearDown = continuation_fixtures.TargetedBotContinuationTests.tearDown
    write = continuation_fixtures.TargetedBotContinuationTests.write
    prepare = continuation_fixtures.TargetedBotContinuationTests.prepare
    send = continuation_fixtures.TargetedBotContinuationTests.send

    def test_the_continuation_is_appended_after_the_first_owner(self):
        from dataclasses import replace
        from test_service_pr import Tracker, observation
        previous, source = self.prepare()
        self.service.pr_tracker = Tracker(replace(observation(), pr_state='open', draft=False, checks_state='running'))
        self.service._put('tasks', dict(previous, status='working'))
        artifact = self.service.emit(previous['id'], 'artifact', dict(kind='pr', url=observation().url))
        self.service._put('tasks', previous)
        self.assertEqual(self.service._get('artifacts', artifact['id']).get('_review_owners'), [previous['id']])
        _, receipt = self.send(source, previous)
        continued = self.service._get('tasks', receipt['task_id'])
        record = self.service._get('artifacts', artifact['id'])
        self.assertEqual((record['_review_task_id'], record.get('_review_owners')), (continued['id'], [previous['id'], continued['id']]))
        self.assertTrue(record.get('_review_ownership_at'))

    def register(self):
        from dataclasses import replace
        from test_service_pr import Tracker, observation
        previous, source = self.prepare()
        self.service.pr_tracker = Tracker(replace(observation(), pr_state='open', draft=False, checks_state='running'))
        self.service._put('tasks', dict(previous, status='working'))
        artifact = self.service.emit(previous['id'], 'artifact', dict(kind='pr', url=observation().url))
        self.service._put('tasks', previous)
        return previous, source, artifact['id']

    def test_a_continuation_does_not_repair_an_inconsistent_list(self):
        previous, source, identity = self.register()
        # A list that lost an owner: it is empty while the record has an owner.
        self.service._put('artifacts', dict(self.service._get('artifacts', identity), _review_owners=[]))
        _, receipt = self.send(source, previous)
        continued = self.service._get('tasks', receipt['task_id'])
        record = self.service._get('artifacts', identity)
        self.assertEqual(record['_review_task_id'], continued['id'])
        self.assertNotIn('_review_owners', record)
        self.assertTrue(record.get('_review_history_unknown'))

    def test_a_continuation_of_a_record_without_a_list_marks_the_history_unknown(self):
        previous, source, identity = self.register()
        self.service._put('artifacts', {k: v for k, v in self.service._get('artifacts', identity).items() if k != '_review_owners'})
        _, receipt = self.send(source, previous)
        record = self.service._get('artifacts', identity)
        self.assertEqual(record['_review_task_id'], receipt['task_id'])
        self.assertNotIn('_review_owners', record)
        self.assertTrue(record.get('_review_history_unknown'))


if __name__ == '__main__':
    unittest.main()
