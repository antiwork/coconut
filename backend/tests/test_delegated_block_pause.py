"""A block check can record an answered card of a delegate that its outcome waits for.

The saved graph: one requester execution delegates through an audited native relay. A later requester execution owns
the blocked outcome and links its native outcome to the delegate's outcome. The person answered the delegate's card.
Only that verified wait makes the delegate's answer eligible. An ancestor, sibling, comment, or copied relay does not.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.kanban_scheduler import KanbanScheduler
from coconut.providers import FixtureProvider, HermesProvider
from coconut.service import Service, ServiceError, now
from coconut.work_recovery import block_check_delay
import test_provider as fixtures

ANSWER = 'delegate-answer'
# The public answer fields a check may show of another bot's card.
PUBLIC_ANSWER = {'id', 'bot_id', 'task_id', 'title', 'action_type', 'status', 'revision', 'decided_at',
                 'selected_option_id', 'selected_option_label'}


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
        self.delegate = self.relay(self.sender, self.dev, 't_delegate')
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

    def relay(self, sender, target, native_id):
        """The sender's message_agent call delivered through the audited native relay route."""
        saved = self.working(sender['id'], 'sender-turn-' + native_id)
        text = 'Check the payout record ' + native_id
        call = self.service.emit(sender['id'], 'native_call', dict(name='message_agent', arguments=dict(target=target['id'], message=text)))
        receipt = self.service.native_relay_delivery(sender['id'], dict(
            native_call_id=call['id'], native_envelope_id=uuid4().hex, target_bot_id=target['id'], text=text))
        self.service._put('tasks', dict(self.service._get('tasks', sender['id']), status='completed'))
        delegate = self.service._get('tasks', receipt['task_id'])
        self.assertEqual(delegate['_native_relay']['sender_bot_id'], saved['bot_id'])
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
                                        parent_task_id=self.sender['id'], _native_relay=self.delegate['_native_relay']))
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
        for owner in ('checked', 'delegate', 'requester'):
            with self.subTest(owner=owner):
                self.setUp()
                self.queued_check()
                self.human_message({'checked': self.task_id, 'delegate': self.delegate['id'],
                                    'requester': self.sender['id']}[owner])
                with self.assertRaisesRegex(ServiceError, 'new direction'):
                    self.record()

    def test_later_human_direction_from_either_owner_ends_the_pause(self):
        for owner in ('checked', 'delegate', 'delegate approval'):
            with self.subTest(owner=owner):
                self.setUp()
                armed = self.paused_check()
                if owner == 'delegate approval':
                    self.answer(self.delegate['id'], 'new-direction', status='approved', decided_at='9999-01-01T00:00:00Z')
                else:
                    self.human_message(self.task_id if owner == 'checked' else self.delegate['id'])
                queued = self.check(armed['next_at'])
                self.assertEqual(queued['status'], 'queued')
                self.assertNotIn(ANSWER, self.offered(queued))

    def test_record_needs_the_check_turn(self):
        self.queued_check()
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved['_native_turn_id'] = 'later-turn'
            self.service._put('tasks', saved)
        with self.assertRaisesRegex(ServiceError, 'Only the check'):
            self.record()



class DelegatedBlockPauseProviderTests(unittest.TestCase):
    """The check turn records the delegate's answer through the exposed coconut_record_pause tool."""
    setUp = fixtures.ProviderTests.setUp
    tearDown = fixtures.ProviderTests.tearDown
    fake_hermes = fixtures.ProviderTests.fake_hermes
    for name in ('message', 'working', 'relay', 'bind', 'tool', 'link', 'answer', 'blocked', 'check', 'state'):
        locals()[name] = getattr(DelegatedBlockPauseTests, name)
    del name

    def test_check_turn_records_the_delegate_answer_through_the_tool(self):
        fixture = self.fake_hermes('''
            import json
            names = set(registry.handlers)
            assert 'coconut_record_pause' in names, names
            assert 'Coconut queued this check because the native task stayed blocked' in prompt
            assert 'an answered card of a delegate' in prompt
            assert ANSWER in prompt and SIBLING not in prompt
            assert 'Private delegate notes' not in prompt
            denied = json.loads(registry.dispatch('coconut_record_pause', {'decision_id': SIBLING}))
            assert denied['status'] == 'rejected' and 'answered human decision' in denied['error'], denied
            receipt = json.loads(registry.dispatch('coconut_record_pause', {'decision_id': ANSWER}))
            assert receipt == {'status': 'recorded', 'decision_id': ANSWER}, receipt
            return {'completed': True, 'final_response': 'The delegate answer still controls this block.'}
        '''.replace('SIBLING', repr('sibling-answer')).replace('ANSWER', repr(ANSWER)))
        # A native claim runs in its workspace. The shared stub has no workspace override.
        with (self.root / 'source' / 'tools' / 'terminal_tool.py').open('a') as stub:
            stub.write('def register_task_env_overrides(task_id, overrides): pass\n')
        self.service = Service(str(self.root / 'service'), FixtureProvider(str(self.root / 'service')), start_workers=False)
        self.addCleanup(self.service.close)
        self.service.provider = HermesProvider(fixture.data_dir, python=fixture.python,
                                               hermes_root=fixture.hermes_root, catalog=fixture.catalog)
        self.addCleanup(self.service.provider.close)
        # The board snapshot only. This test starts no Kanban adapter process.
        self.service.kanban = KanbanScheduler(self.service.provider, str(self.root / 'service'))
        self.desk = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        self.dev = self.service.mutate('POST', '/v1/bots', {'name': 'Dev', 'role': ''}, str(uuid4()))
        sender = self.message(self.desk, 'Ask Dev to check the payout record')
        delegate = self.relay(sender, self.dev, 't_delegate')
        sibling = self.relay(sender, self.dev, 't_sibling')
        self.task_id = self.message(self.desk, 'Settle the payout case')['id']
        self.bind(self.task_id, 't_checked')
        self.link('t_delegate')
        self.answer(delegate['id'])
        self.answer(sibling['id'], 'sibling-answer')
        with self.service.transaction():
            saved = self.service._get('tasks', self.task_id)
            saved['_kanban_claim'] = dict(native_id='t_checked', run_id=1, claim_lock='claim-checked', external_id=self.task_id,
                                          context='# Kanban task t_checked: Settle the payout case')
            self.service._put('tasks', saved)
        # The sibling's edge on the board came from elsewhere, so it does not make the sibling eligible.
        self.service.kanban.tasks[self.task_id] = dict(external_id=self.task_id, id='t_checked', status='blocked',
                                                      block_kind='needs_input', parent_ids=['t_delegate', 't_sibling'])
        before = {table: len(self.service._all(table)) for table in ('decisions', 'messages', 'deliveries')}
        answer = self.service._get('decisions', ANSWER)
        self.check(0)
        self.assertEqual(self.check(10 ** 9)['status'], 'queued')
        self.check(10 ** 9 + 1, dict(self.blocked(), status='running', current_run_id=1))
        workspace = self.root / 'workspace'
        workspace.mkdir()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            self.service.provider.prepare_kanban_worker(self.task_id, self.service._public(self.desk), str(workspace), self.task_id)
            self.service._run_task(self.task_id)
        finished = self.service._get('tasks', self.task_id)
        self.assertNotIn(finished['status'], ('failed', 'queued', 'working'), (finished.get('summary'), finished.get('_error_detail')))
        check = finished['_block_check']
        self.assertEqual(check['paused_by_decision_id'], ANSWER)
        self.assertTrue(check['pause_fingerprint'])
        # The worker blocked the task again. The next check waits for the person.
        armed = self.check(2 * 10 ** 9)['_block_check']
        self.assertEqual(armed['paused_by_decision_id'], ANSWER)
        self.assertEqual(self.check(armed['next_at'])['status'], finished['status'])
        self.assertEqual(self.service._get('decisions', ANSWER), answer)
        # Replies of the worker turn are its own. No card, delivery, or relay was added.
        after = {table: len(self.service._all(table)) for table in ('decisions', 'messages', 'deliveries')}
        self.assertEqual((after['decisions'], after['deliveries']), (before['decisions'], before['deliveries']))


if __name__ == '__main__':
    unittest.main()
