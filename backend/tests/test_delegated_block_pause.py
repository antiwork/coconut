"""A block check can record an answered card of a delegate that its outcome waits for.

The saved graph: one requester execution delegates through an audited native relay. A later requester execution owns
the blocked outcome and links its native outcome to the delegate's outcome. The person answered the delegate's card.
Only that verified wait makes the delegate's answer eligible. An ancestor, sibling, comment, or copied relay does not.
The delegate's outcome has its own owner when the relay intake created it with kanban_create, or the intake owns it.
"""
import json
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.kanban_scheduler import KanbanScheduler
from coconut.gateway_chat import kanban_arguments
from coconut.native_outcomes import outcome_owner_id
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError, YieldForDecision, digest, encoded, now
from coconut.work_recovery import block_check_delay

ANSWER = 'delegate-answer'
# The public answer fields a check may show of another bot's card.
PUBLIC_ANSWER = {'id', 'bot_id', 'task_id', 'title', 'action_type', 'status', 'revision', 'decided_at',
                 'selected_option_id', 'selected_option_label', 'correction'}


class DelegatedBlockPauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(self.tmp.name, FixtureProvider(self.tmp.name), start_workers=False)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)
        # The board snapshot only. These tests start no Kanban adapter process.
        self.service.kanban = KanbanScheduler(self.service.provider, self.tmp.name)
        self.desk = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        self.dev = self.service.mutate('POST', '/v1/bots', {'name': 'Dev', 'role': ''}, str(uuid4()))
        self.sender = self.message(self.desk, 'Ask Dev to check the payout record')
        self.delegate = self.delegated('t_delegate')
        self.task_id = self.message(self.desk, 'Settle the payout case')['id']
        self.bind(self.task_id, 't_checked')
        self.link('t_delegate')
        self.service.kanban.tasks[self.task_id] = dict(external_id=self.task_id, id='t_checked', status='blocked',
                                                      block_kind='needs_input', parent_ids=['t_delegate'])
        self.answer(self.delegate['id'])

    # Fixture steps. Each one uses the service route that saves this state in production.

    def message(self, bot, text):
        return self.service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': text, 'mode': 'queue'}, str(uuid4()))

    def working(self, identity, turn):
        saved = self.service._get('tasks', identity)
        self.service._put('tasks', dict(saved, status='working', _native_turn_id=turn))
        return saved

    def delegated(self, native_id):
        """The relay intake owns the delegate's outcome."""
        self.intake = self.relay(self.sender, self.dev, native_id)
        return self.intake

    def relay(self, sender, target, native_id, *, bind=True):
        """The saved state of the sender's message_agent call that an earlier Coconut delivered through its audited
        native relay: the call's receipt with the delivery, the delivery under its envelope ID, and the relay of the
        delegate. Coconut no longer makes this state, but it still reads it (_block_check_relay_lineage)."""
        saved = self.working(sender['id'], 'sender-turn-' + native_id)
        text = 'Check the payout record ' + native_id
        envelope = uuid4().hex
        call = self.service.emit(sender['id'], 'native_call', dict(name='message_agent', arguments=dict(target=target['id'], message=text)))
        with self.service.transaction():
            receipt = self.service._store_bot_message(sender['id'], dict(target_bot_id=target['id'], text=text))
            delivery = next(d for d in reversed(self.service._all('deliveries')) if d['receipt']['task_id'] == receipt['task_id'])
            self.service.db.execute('DELETE FROM deliveries WHERE id=?', (delivery['id'],))
            self.service._put('deliveries', dict(delivery, id=digest(encoded([sender['id'], envelope]))))
            delegate = self.service._get('tasks', receipt['task_id'])
            self.service._put('tasks', dict(delegate, _native_relay=dict(sender_bot_id=saved['bot_id'], envelope_id=envelope)))
        self.service.emit(sender['id'], 'native_result', dict(id=call['id'], result=json.dumps(dict(receipt, delivery_id=envelope))))
        self.service._put('tasks', dict(self.service._get('tasks', sender['id']), status='completed'))
        delegate = self.service._get('tasks', receipt['task_id'])
        self.assertEqual(delegate['_native_relay']['sender_bot_id'], saved['bot_id'])
        if bind:
            self.bind(delegate['id'], native_id)
        return self.service._get('tasks', delegate['id'])

    def bind(self, identity, native_id):
        saved = self.service._get('tasks', identity)
        self.service._put('tasks', dict(saved, status='completed', _native_outcome_id=native_id,
                                        _execution_session_id=saved.get('_execution_session_id') or identity))

    def tool(self, identity, name, arguments, result, *, finish=True):
        saved = self.working(identity, 'tool-turn')
        call = self.service.emit(identity, 'native_call', dict(name=name, arguments=arguments))
        if finish:
            self.service.emit(identity, 'native_result', dict(id=call['id'], result=json.dumps(result)))
        self.service._put('tasks', dict(self.service._get('tasks', identity), status=saved['status'],
                                        _native_turn_id=saved.get('_native_turn_id')))

    def link(self, parent, *, owner=None, result=None):
        arguments = dict(parent_id=parent, child_id='t_checked')
        self.tool(owner or self.task_id, 'kanban_link', arguments,
                  dict(arguments, ok=True, gated=False) if result is None else result)

    def answer(self, task_id, identity=ANSWER, bot=None, **fields):
        decision = dict(id=identity, task_id=task_id, bot_id=(bot or self.dev)['id'], status='executed',
                        decided_by='person', decided_at=now(), created_at=now(), title='Leave the case open?',
                        action_type='question', options=[], revision=1, selected_option_id='leave-open',
                        selected_option_label='Leave it open', context='Private delegate notes')
        decision.update(fields)
        with self.service.transaction():
            self.service._put('decisions', decision)
        return decision

    def intake_peer(self):
        """Another turn of the intake's execution, such as its reply to a later message."""
        peer = self.message(self.dev, 'Any update on the record?')
        intake = self.service._get('tasks', self.intake['id'])
        self.service._put('tasks', dict(self.service._get('tasks', peer['id']), status='completed',
                                        _execution_session_id=intake.get('_execution_session_id') or intake['id']))
        return peer['id']

    def human_message(self, task_id, text='Use the other account.'):
        with self.service.transaction():
            task = self.service._get('tasks', task_id)
            message = self.service._message(task['bot_id'], text, role='user', task_id=task_id)
            self.service._put('messages', dict(message, _human_device_id='phone'))

    def blocked(self):
        return dict(self.service.kanban.tasks[self.task_id])

    def check(self, clock, native=None):
        with patch('coconut.native_outcomes.time.time', return_value=clock), self.service.transaction():
            self.service._native_block_check(self.service._get('tasks', self.task_id), native or self.blocked())
        return self.service._get('tasks', self.task_id)

    def state(self, status):
        with self.service.transaction():
            task = self.service._get('tasks', self.task_id)
            task['status'] = status
            self.service._put('tasks', task)

    def queued_check(self):
        self.check(0)
        queued = self.check(10 ** 9)
        self.assertEqual(queued['status'], 'queued')
        self.state('working')
        return queued

    def offered(self, task=None):
        return {d['id']: d for d in (task or self.service._get('tasks', self.task_id))['_self_recovery']['decisions']}

    def record(self, identity=ANSWER):
        return self.service.emit(self.task_id, 'block_pause', {'decision_id': identity})

    def finish_blocked(self, clock):
        # The check turn ran as the native claim, then blocked the task again.
        self.check(clock, dict(self.blocked(), status='running', current_run_id=7))
        self.state('completed')
        return self.check(clock + 1)['_block_check']

    def paused_check(self):
        self.queued_check()
        self.assertEqual(self.record(), dict(status='recorded', decision_id=ANSWER))
        armed = self.finish_blocked(2 * 10 ** 9)
        self.assertEqual(armed['paused_by_decision_id'], ANSWER)
        return armed

    def counts(self):
        return {table: len(self.service._all(table)) for table in ('decisions', 'messages', 'deliveries', 'contexts')}

    # Genuine delegate answer.

    def test_check_offers_the_delegate_answer_and_records_its_pause(self):
        before = self.counts()
        decision = self.service._get('decisions', ANSWER)
        queued = self.queued_check()
        offered = self.offered(queued)[ANSWER]
        # Only the public answer fields and the fingerprint join the check, never the delegate's other card data.
        self.assertEqual(set(offered), PUBLIC_ANSWER | {'answer_fingerprint'})
        self.assertNotIn('Private delegate notes', json.dumps(queued['_self_recovery']))
        self.assertEqual(self.record(), dict(status='recorded', decision_id=ANSWER))
        armed = self.finish_blocked(2 * 10 ** 9)
        self.assertEqual((armed['paused_by_decision_id'], armed['pause_fingerprint']), (ANSWER, offered['answer_fingerprint']))
        deferred = self.check(armed['next_at'])
        self.assertEqual(deferred['status'], 'completed')
        self.assertEqual(deferred['_block_check']['next_at'], armed['next_at'] + block_check_delay(armed['attempt']))
        # The pause is bookkeeping only. The answer, cards, messages, and deliveries stay as they were.
        self.assertEqual(self.service._get('decisions', ANSWER), decision)
        self.assertEqual(self.counts(), before)

    def test_own_answer_still_pauses_without_a_fingerprint(self):
        self.answer(self.task_id, 'own-answer', bot=self.desk, status='rejected')
        queued = self.queued_check()
        self.assertNotIn('answer_fingerprint', self.offered(queued)['own-answer'])
        self.assertEqual(self.record('own-answer'), dict(status='recorded', decision_id='own-answer'))
        armed = self.finish_blocked(2 * 10 ** 9)
        self.assertEqual(armed['paused_by_decision_id'], 'own-answer')
        self.assertNotIn('pause_fingerprint', armed)
        self.assertEqual(self.check(armed['next_at'])['status'], 'completed')

    def test_switching_to_the_own_answer_drops_the_delegate_fingerprint(self):
        self.answer(self.task_id, 'own-answer', bot=self.desk, status='rejected')
        self.queued_check()
        self.record()
        self.record('own-answer')
        check = self.service._get('tasks', self.task_id)['_block_check']
        self.assertEqual(check['paused_by_decision_id'], 'own-answer')
        self.assertNotIn('pause_fingerprint', check)

    # Links that do not prove this outcome waits for the delegate.

    def assert_not_eligible(self, identity):
        queued = self.queued_check()
        self.assertNotIn(identity, self.offered(queued))
        with self.assertRaisesRegex(ServiceError, 'answered human decision'):
            self.record(identity)
        self.assertNotIn('paused_by_decision_id', self.service._get('tasks', self.task_id)['_block_check'])

    def test_sibling_delegate_without_a_link_is_not_eligible(self):
        # Desk delegated the sibling through the same audited route, and the board shows an edge that another task added.
        sibling = self.relay(self.sender, self.dev, 't_sibling')
        self.answer(sibling['id'], 'sibling-answer')
        other = self.message(self.desk, 'Track the sibling')
        self.bind(other['id'], 't_other')
        self.link('t_sibling', owner=other['id'])
        self.service.kanban.tasks[self.task_id]['parent_ids'].append('t_sibling')
        self.assert_not_eligible('sibling-answer')

    def test_ancestor_of_the_delegate_is_not_eligible(self):
        ancestor = self.relay(self.sender, self.dev, 't_ancestor')
        self.answer(ancestor['id'], 'ancestor-answer')
        self.service.kanban.tasks[self.delegate['id']] = dict(external_id=self.delegate['id'], id='t_delegate',
                                                             status='done', parent_ids=['t_ancestor'])
        self.assert_not_eligible('ancestor-answer')

    def test_comment_or_board_edge_alone_is_not_eligible(self):
        commented = self.relay(self.sender, self.dev, 't_commented')
        self.answer(commented['id'], 'commented-answer')
        self.tool(self.task_id, 'kanban_comment', dict(task_id='t_commented', body='Waiting for Dev.'),
                  dict(ok=True, task_id='t_commented', comment_id=4))
        self.assertIn('t_commented', self.service._get('tasks', self.task_id)['_source_native_ids'])
        self.service.kanban.tasks[self.task_id]['parent_ids'].append('t_commented')
        self.assert_not_eligible('commented-answer')

    def test_failed_or_unfinished_link_is_not_eligible(self):
        for parent, result, finish in (('t_failed', dict(ok=False, error='No such task'), True), ('t_unfinished', None, False)):
            delegate = self.relay(self.sender, self.dev, parent)
            self.answer(delegate['id'], parent + '-answer')
            arguments = dict(parent_id=parent, child_id='t_checked')
            self.tool(self.task_id, 'kanban_link', arguments, result, finish=finish)
            self.service.kanban.tasks[self.task_id]['parent_ids'].append(parent)
        queued = self.queued_check()
        self.assertFalse({'t_failed-answer', 't_unfinished-answer'} & set(self.offered(queued)))
        self.assertIn(ANSWER, self.offered(queued))

    def test_copied_relay_without_its_audited_delivery_is_not_eligible(self):
        # A Dev task that copies the requester fields and relay of the genuine delegate.
        forged = self.message(self.dev, 'Pretend Desk asked for this')
        saved = self.service._get('tasks', forged['id'])
        self.service._put('tasks', dict(saved, source_bot_id=self.desk['id'], source_task_id=self.sender['id'],
                                        parent_task_id=self.sender['id'], _native_relay=self.intake['_native_relay']))
        self.bind(forged['id'], 't_forged')
        self.answer(forged['id'], 'forged-answer')
        self.link('t_forged')
        self.service.kanban.tasks[self.task_id]['parent_ids'].append('t_forged')
        # A fresh envelope has no delivery or sender receipt at all.
        forged_fresh = self.message(self.dev, 'Another copy')
        saved = self.service._get('tasks', forged_fresh['id'])
        self.service._put('tasks', dict(saved, source_bot_id=self.desk['id'], source_task_id=self.sender['id'],
                                        _native_relay=dict(sender_bot_id=self.desk['id'], envelope_id=uuid4().hex)))
        self.bind(forged_fresh['id'], 't_fresh')
        self.answer(forged_fresh['id'], 'fresh-answer')
        self.link('t_fresh')
        self.service.kanban.tasks[self.task_id]['parent_ids'].append('t_fresh')
        queued = self.queued_check()
        self.assertFalse({'forged-answer', 'fresh-answer'} & set(self.offered(queued)))
        self.assertIn(ANSWER, self.offered(queued))

    def test_link_removed_after_the_offer_blocks_the_record(self):
        queued = self.queued_check()
        self.assertIn(ANSWER, self.offered(queued))
        self.service.kanban.tasks[self.task_id]['parent_ids'] = ['t_unrelated']
        with self.assertRaisesRegex(ServiceError, 'no longer belongs'):
            self.record()
        self.assertNotIn('paused_by_decision_id', self.service._get('tasks', self.task_id)['_block_check'])

    def test_context_entry_of_an_unrelated_delegate_is_rechecked(self):
        # An entry in the saved context does not make a decision eligible. The record checks the relation itself.
        sibling = self.relay(self.sender, self.dev, 't_sibling')
        decision = self.answer(sibling['id'], 'sibling-answer')
        queued = self.queued_check()
        task = self.service._get('tasks', self.task_id)
        entry = dict({key: decision.get(key) for key in PUBLIC_ANSWER},
                     answer_fingerprint=self.offered(queued)[ANSWER]['answer_fingerprint'])
        task['_self_recovery']['decisions'].append(entry)
        self.service._put('tasks', task)
        with self.assertRaisesRegex(ServiceError, 'no longer belongs'):
            self.record('sibling-answer')
        self.assertNotIn('paused_by_decision_id', self.service._get('tasks', self.task_id)['_block_check'])

    def test_unlinked_edge_ends_the_pause(self):
        armed = self.paused_check()
        # Hermes keeps the outcome's other edges.
        self.service.kanban.tasks[self.task_id]['parent_ids'] = ['t_unrelated']
        queued = self.check(armed['next_at'])
        self.assertEqual(queued['status'], 'queued')
        self.assertNotIn(ANSWER, self.offered(queued))
        self.assertNotIn('pause_fingerprint', queued['_block_check'])

    def test_requester_from_another_bot_session_is_not_eligible(self):
        with self.service.transaction():
            sender = self.service._get('tasks', self.sender['id'])
            self.service._put('tasks', dict(sender, session_id=str(uuid4())))
        self.assert_not_eligible(ANSWER)

    # Pending, unanswered, and changed answers.

    def test_pending_or_unanswered_delegate_cards_are_not_offered_and_do_not_defer(self):
        self.answer(self.delegate['id'], 'pending', status='pending', decided_by=None, decided_at=None)
        self.answer(self.delegate['id'], 'closed-by-bot', status='rejected', decided_by=None)
        self.answer(self.delegate['id'], 'hidden', _hidden=True)
        queued = self.queued_check()
        self.assertEqual(set(self.offered(queued)), {ANSWER})
        for identity in ('pending', 'closed-by-bot', 'hidden'):
            with self.subTest(identity=identity), self.assertRaisesRegex(ServiceError, 'answered human decision'):
                self.record(identity)

    def test_answer_changed_after_the_check_offered_it_cannot_be_recorded(self):
        self.queued_check()
        for change in (dict(selected_option_id='close-now', selected_option_label='Close it now'),
                       dict(revision=2), dict(decided_at='9999-01-01T00:00:00Z'), dict(task_id=self.sender['id'])):
            with self.subTest(change=change):
                original = self.service._get('decisions', ANSWER)
                with self.service.transaction():
                    self.service._put('decisions', dict(original, **change))
                with self.assertRaises(ServiceError):
                    self.record()
                with self.service.transaction():
                    self.service._put('decisions', original)
        self.assertNotIn('paused_by_decision_id', self.service._get('tasks', self.task_id)['_block_check'])
        self.assertEqual(self.record(), dict(status='recorded', decision_id=ANSWER))

    def test_answer_changed_after_the_record_ends_the_pause(self):
        armed = self.paused_check()
        with self.service.transaction():
            self.service._put('decisions', dict(self.service._get('decisions', ANSWER), selected_option_id='close-now'))
        queued = self.check(armed['next_at'])
        self.assertEqual(queued['status'], 'queued')
        # The refreshed check offers the current answer with its new fingerprint.
        self.assertNotEqual(self.offered(queued)[ANSWER]['answer_fingerprint'], armed['pause_fingerprint'])

    # Later human direction from either owner.

    def test_human_message_during_the_check_blocks_the_record(self):
        for owner in ('checked', 'delegate', 'intake', 'intake execution', 'requester'):
            with self.subTest(owner=owner):
                self.setUp()
                self.queued_check()
                target = self.intake_peer() if owner == 'intake execution' else {
                    'checked': self.task_id, 'delegate': self.delegate['id'], 'intake': self.intake['id'],
                    'requester': self.sender['id']}[owner]
                self.human_message(target)
                with self.assertRaisesRegex(ServiceError, 'new direction'):
                    self.record()

    def test_later_human_direction_from_either_owner_ends_the_pause(self):
        for owner in ('checked', 'delegate', 'intake', 'intake execution', 'delegate approval'):
            with self.subTest(owner=owner):
                self.setUp()
                armed = self.paused_check()
                if owner == 'delegate approval':
                    self.answer(self.delegate['id'], 'new-direction', status='approved', decided_at='9999-01-01T00:00:00Z')
                elif owner == 'intake execution':
                    self.human_message(self.intake_peer())
                else:
                    self.human_message({'checked': self.task_id, 'delegate': self.delegate['id'],
                                        'intake': self.intake['id']}[owner])
                queued = self.check(armed['next_at'])
                self.assertEqual(queued['status'], 'queued')
                self.assertNotIn(ANSWER, self.offered(queued))

    def typed_reply(self, text):
        """The delegate asks a card, and the person answers it with a typed reply through the phone route."""
        saved = self.working(self.delegate['id'], 'question-turn')
        call = self.service.emit(self.delegate['id'], 'native_call', dict(name='coconut_ask', arguments={'title': 'Close the case?'}))
        with self.assertRaises(YieldForDecision):
            self.service.native_event(self.delegate['id'], 'native_question', dict(
                title='Close the case?', context='Private delegate notes', receipt_id=call['id'],
                options=[dict(id='close', label='Close it now')]))
        decision = next(d for d in self.service._all('decisions') if d['task_id'] == self.delegate['id'] and d['status'] == 'pending')
        self.service.mutate('POST', f"/v1/decisions/{decision['id']}/answer",
                            dict(choice='reply', correction=text, revision=decision['revision']), str(uuid4()))
        self.service._put('tasks', dict(self.service._get('tasks', self.delegate['id']), status=saved['status']))
        return self.service._get('decisions', decision['id'])

    def test_typed_reply_is_offered_with_its_public_text_only(self):
        text = 'Keep it open. The customer asked us to wait until Friday.'
        reply = self.typed_reply(text)
        self.assertEqual((reply['status'], reply['correction'], reply['selected_option_id']), ('replied', text, None))
        queued = self.queued_check()
        offered = self.offered(queued)[reply['id']]
        self.assertEqual(set(offered), PUBLIC_ANSWER | {'answer_fingerprint'})
        self.assertEqual((offered['status'], offered['correction']), ('replied', text))
        self.assertNotIn('Private delegate notes', json.dumps(queued['_self_recovery']))
        # A changed typed answer is a changed answer. The record refuses it, then the exact answer records.
        changed = dict(reply, correction='Close it now.')
        with self.service.transaction():
            self.service._put('decisions', changed)
        with self.assertRaisesRegex(ServiceError, 'changed'):
            self.record(reply['id'])
        with self.service.transaction():
            self.service._put('decisions', reply)
        self.assertEqual(self.record(reply['id']), dict(status='recorded', decision_id=reply['id']))
        self.assertEqual(self.service._get('decisions', reply['id']), reply)

    def test_record_needs_the_check_turn(self):
        self.queued_check()
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved['_native_turn_id'] = 'later-turn'
            self.service._put('tasks', saved)
        with self.assertRaisesRegex(ServiceError, 'Only the check'):
            self.record()



class AdoptedDelegateBlockPauseTests(DelegatedBlockPauseTests):
    """Every check above, where the relay intake created the delegate's outcome and adoption gave it its own owner."""

    def delegated(self, native_id):
        self.intake = self.relay(self.sender, self.dev, native_id, bind=False)
        return self.adopt(self.intake, native_id)

    def adopt(self, intake, native_id):
        """The intake's kanban_create with Coconut's creation arguments, then _adopt_native_outcomes."""
        saved = self.working(intake['id'], 'intake-turn-' + native_id)
        arguments = kanban_arguments(dict(title='Check the payout record', body='Check it read-only.', workspace_kind='dir',
                                          workspace_path=str(self.service.root / 'workspaces' / native_id)),
                                     intake['id'], saved.get('_native_turn_id') or 'intake-turn-' + native_id, intake['bot_id'],
                                     self.service.root / 'kanban' / 'kanban' / 'workspaces')
        call = self.service.emit(intake['id'], 'native_call', dict(name='kanban_create', arguments=arguments))
        self.service.emit(intake['id'], 'native_result', dict(id=call['id'], result=json.dumps(
            dict(ok=True, task_id=native_id, status='blocked'))))
        self.service._put('tasks', dict(self.service._get('tasks', intake['id']), status='completed'))
        self.service.kanban.tasks['native:' + native_id] = dict(
            external_id='native:' + native_id, unbound=True, id=native_id, title='Check the payout record',
            status='blocked', tenant=intake['bot_id'], session_id=intake['id'], idempotency_key=arguments['idempotency_key'])
        with patch.object(self.service.kanban, 'call', return_value={}) as adapter:
            self.service._adopt_native_outcomes()
        identity = outcome_owner_id(intake['id'], native_id)
        adapter.assert_any_call(dict(op='bind', id=native_id, external_id=identity))
        self.service.kanban.tasks.pop('native:' + native_id)
        owner = self.service._get('tasks', identity)
        self.assertEqual((owner['parent_task_id'], owner['_relay_callback_owner'], owner['_native_outcome_id']),
                         (intake['id'], intake['id'], native_id))
        self.assertNotIn('_native_relay', owner)
        return owner

    def creation(self):
        return next(r for r in self.service._all('native_receipts')
                    if r['task_id'] == self.intake['id'] and r['action']['name'] == 'kanban_create')

    def change(self, table, row, **fields):
        with self.service.transaction():
            self.service._put(table, dict(row, **fields))

    def test_only_the_adopted_execution_answers_are_offered(self):
        # The intake's own card and a sibling outcome that the same intake created stay with their owners.
        self.answer(self.intake['id'], 'intake-answer')
        sibling = self.adopt(self.intake, 't_sibling')
        self.answer(sibling['id'], 'sibling-outcome-answer')
        queued = self.queued_check()
        self.assertIn(ANSWER, self.offered(queued))
        for identity in ('intake-answer', 'sibling-outcome-answer'):
            self.assertNotIn(identity, self.offered(queued))
            with self.subTest(identity=identity), self.assertRaisesRegex(ServiceError, 'answered human decision'):
                self.record(identity)

    def test_adoption_without_its_proof_is_not_eligible(self):
        def other_intake():
            return self.relay(self.sender, self.dev, 't_other', bind=False)

        def copied_owner():
            # The same fields and the answer under an ID that adoption did not derive.
            copy = dict(self.delegate, id=str(uuid4()))
            self.change('tasks', copy, _execution_session_id=copy['id'])
            self.change('tasks', {k: v for k, v in self.delegate.items() if k != '_native_outcome_id'})
            self.answer(copy['id'])

        def without_receipt():
            self.service.db.execute('DELETE FROM native_receipts WHERE id=?', (self.creation()['id'],))

        def receipt_arguments(**fields):
            receipt = self.creation()
            receipt['action']['arguments'].update(fields)
            self.change('native_receipts', receipt)

        cases = {
            'callback to another intake': lambda: self.change('tasks', self.delegate, _relay_callback_owner=other_intake()['id']),
            'copied owner': copied_owner,
            'missing creation receipt': without_receipt,
            'creation result for another outcome': lambda: self.change('native_receipts', self.creation(),
                result=json.dumps(dict(ok=True, task_id='t_other', status='blocked'))),
            'unfinished creation': lambda: self.change('native_receipts', self.creation(), state='executing'),
            'creation for another tenant': lambda: receipt_arguments(tenant=self.desk['id']),
            'creation key of another task': lambda: receipt_arguments(idempotency_key='coconut:' + self.sender['id'] + ':key'),
            'creation receipt of another task': lambda: self.change('native_receipts', self.creation(), task_id=self.sender['id']),
            'owner in another session': lambda: self.change('tasks', self.delegate, session_id=str(uuid4())),
            'intake in another session': lambda: self.change('tasks', self.service._get('tasks', self.intake['id']), session_id=str(uuid4())),
            'intake relay without delivery': lambda: self.change('tasks', self.service._get('tasks', self.intake['id']),
                _native_relay=dict(self.intake['_native_relay'], envelope_id=uuid4().hex)),
        }
        for name, forge in cases.items():
            with self.subTest(case=name):
                self.setUp()
                forge()
                queued = self.queued_check()
                self.assertNotIn(ANSWER, self.offered(queued))
                with self.assertRaisesRegex(ServiceError, 'answered human decision'):
                    self.record()


if __name__ == '__main__':
    unittest.main()
