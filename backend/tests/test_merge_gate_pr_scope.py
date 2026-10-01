import copy
import json
import tempfile
import unittest
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError, YieldForDecision

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
        self.payload = dict(title='Merge the target change?', merge_pr_url=TARGET,
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


if __name__ == '__main__':
    unittest.main()
