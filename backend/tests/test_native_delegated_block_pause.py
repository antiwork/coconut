"""A block check records a delegate's answer from the installed Hermes board.

The fixture board is the test's own Hermes Kanban board. The delegate's relay intake (saved relay history) creates the delegate's outcome with
Hermes' own kanban_create, and _adopt_native_outcomes gives that outcome its own owner, as in normal native delegation.
The blocked outcome's claimed run links its card to that outcome's card and blocks it through Hermes' own kanban_link
and kanban_block tools. Coconut reads the edge from its adapter snapshot. The check offers the delegate's answer, and
coconut_record_pause reaches the service on the route of the gateway plugin.
Native CI sets COCONUT_REQUIRE_NATIVE, and then a missing Hermes install fails these tests instead of skipping them.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.gateway_chat import kanban_arguments
from coconut.native_outcomes import outcome_owner_id
from coconut.service import ServiceError, digest, encoded, now
from coconut.work_recovery import BLOCK_PAUSE_SCHEMA, block_check_delay
import legacy_work_sync
import test_native_kanban as native_fixtures
import hermes_env

setUpModule, tearDownModule = legacy_work_sync.scope()

ANSWER = 'delegate-answer'
SIBLING = 'sibling-answer'
PUBLIC_ANSWER = {'id', 'bot_id', 'task_id', 'title', 'action_type', 'status', 'revision', 'decided_at',
                 'selected_option_id', 'selected_option_label', 'correction'}

# A tool call of the chat turn on the fixture board, without a claimed card. Board pins the fixture's own board first.
CHAT_TOOL = '''
import json
import tools.kanban_tools
from tools.registry import registry
print(json.dumps(dict(result=registry.dispatch(sys.argv[2], json.loads(sys.argv[3])))))
'''

# A tool call of the claimed worker on the fixture board. Board pins the Hermes board to the fixture's own home first.
WORKER_TOOL = '''
import json, os
os.environ.update(HERMES_KANBAN_TASK=sys.argv[2], HERMES_KANBAN_RUN_ID=sys.argv[3], HERMES_KANBAN_CLAIM_LOCK=sys.argv[4])
import tools.kanban_tools
from tools.registry import registry
print(json.dumps(dict(result=registry.dispatch(sys.argv[5], json.loads(sys.argv[6])))))
'''


class NativeDelegatedBlockPauseTests(unittest.TestCase):
    write = native_fixtures.NativeKanbanTests.write
    task = native_fixtures.NativeKanbanTests.task
    native_program = native_fixtures.NativeKanbanTests.native_program

    def setUp(self):
        hermes_env.require(self)
        try:
            native_fixtures.NativeKanbanTests.setUp(self)
            self.service.kanban.call({'op': 'snapshot'})
        except RuntimeError as error:
            # A Hermes descendant fence makes the fixture's own board read-only, so the adapter cannot start.
            # Report Hermes' own reason instead of working around the fence.
            log = Path(self.tmp.name) / 'kanban' / 'adapter.log'
            reason = log.read_text().strip().splitlines()[-1:] if log.exists() else []
            self.fail('The native fixture board did not start: %s %s' % (error, reason))
        # The board is the fixture's own, never a board that the environment pins.
        self.assertTrue((Path(self.tmp.name) / 'kanban' / 'kanban.db').is_file())
        self.dev = self.write('/v1/bots', {'name': 'Dev', 'role': ''})

    def update(self, identity, **fields):
        self.assertFalse(self.service.db.in_transaction, 'A fixture write must not nest in another transaction.')
        with self.service.transaction():
            task = self.service._get('tasks', identity)
            task.update(fields)
            self.service._put('tasks', task)
        return task

    def last_json(self, output):
        return json.loads(output.splitlines()[-1])

    def snapshot(self, identity):
        self.service.kanban.call({'op': 'snapshot'})
        return dict(self.service.kanban.tasks[identity])

    def card(self, identity):
        """Board.sync gives the task its own card on the fixture board."""
        task = self.update(identity, _execution_session_id=identity, _conversation_epoch=self.service._setting('conversation_epoch'))
        row = self.service._kanban_row(task, task, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        return self.snapshot(identity)['id'], row

    def claim(self, native):
        claimed = self.last_json(self.native_program(
            'import json\nc = board.kb.claim_task(board.conn, sys.argv[2])\nprint(json.dumps([c.current_run_id, c.claim_lock]))', native))
        self.assertTrue(claimed[0], 'Hermes did not claim the card.')
        return claimed

    def worker_tool(self, native, run, lock, name, arguments):
        return self.last_json(self.native_program(WORKER_TOOL, native, str(run), lock or '', name, json.dumps(arguments)))['result']

    def record(self, identity, name, arguments, result):
        """The worker saves each tool call and its result through the provider's receipt events."""
        call = self.service.emit(identity, 'native_call', dict(name=name, arguments=arguments))
        self.service.emit(identity, 'native_result', dict(id=call['id'], result=result))

    def relay(self, sender, target, text):
        """The saved state of the sender's message_agent call that an earlier Coconut delivered through its audited
        native relay: the call's receipt with the delivery, the delivery under its envelope ID, and the relay of the
        intake. Coconut no longer makes this state, but it still reads it (_block_check_relay_lineage)."""
        self.update(sender['id'], status='working', _native_turn_id='sender-turn-' + str(uuid4()))
        envelope = uuid4().hex
        call = self.service.emit(sender['id'], 'native_call', dict(name='message_agent', arguments=dict(target=target['id'], message=text)))
        with self.service.transaction():
            receipt = self.service._store_bot_message(sender['id'], dict(target_bot_id=target['id'], text=text))
            delivery = next(d for d in reversed(self.service._all('deliveries')) if d['receipt']['task_id'] == receipt['task_id'])
            self.service.db.execute('DELETE FROM deliveries WHERE id=?', (delivery['id'],))
            self.service._put('deliveries', dict(delivery, id=digest(encoded([sender['id'], envelope]))))
            intake = self.service._get('tasks', receipt['task_id'])
            self.service._put('tasks', dict(intake, _native_relay=dict(sender_bot_id=sender['bot_id'], envelope_id=envelope)))
        self.service.emit(sender['id'], 'native_result', dict(id=call['id'], result=json.dumps(dict(receipt, delivery_id=envelope))))
        self.update(sender['id'], status='completed')
        return self.service._get('tasks', receipt['task_id'])

    def adopted_delegate(self, intake, title):
        """The intake's chat turn creates the delegate's outcome with kanban_create, Coconut adopts it, and its run finishes."""
        turn = 'intake-turn-' + str(uuid4())
        self.update(intake['id'], status='working', _native_turn_id=turn)
        arguments = kanban_arguments(dict(title=title, body='Check it read-only.', workspace_kind='dir',
                                          workspace_path=str(Path(self.tmp.name) / 'workspaces' / str(uuid4()))),
                                     intake['id'], turn, intake['bot_id'], self.service.root / 'kanban' / 'kanban' / 'workspaces')
        created = self.last_json(self.native_program(CHAT_TOOL, 'kanban_create', json.dumps(arguments)))['result']
        native = json.loads(created)
        self.assertIs(native.get('ok'), True, created)
        self.record(intake['id'], 'kanban_create', arguments, created)
        self.update(intake['id'], status='completed')
        self.service.kanban.call({'op': 'snapshot'})
        card = self.service.kanban.tasks['native:' + native['task_id']]
        self.assertTrue(card.get('unbound'))
        self.assertEqual((card['idempotency_key'], card['tenant']), (arguments['idempotency_key'], intake['bot_id']))
        self.service._adopt_native_outcomes()
        identity = outcome_owner_id(intake['id'], native['task_id'])
        owner = self.service._get('tasks', identity)
        self.assertEqual((owner['parent_task_id'], owner['_relay_callback_owner'], owner['_native_outcome_id']),
                         (intake['id'], intake['id'], native['task_id']))
        self.assertNotIn('_native_relay', owner)
        self.assertEqual(self.snapshot(identity)['id'], native['task_id'])
        # The outcome's own run finishes its card.
        self.native_program('board.kb.unblock_task(board.conn, sys.argv[2])', native['task_id'])
        run, _ = self.claim(native['task_id'])
        self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Checked the record.', "
                            "expected_run_id=int(sys.argv[3]))", native['task_id'], str(run))
        self.update(identity, status='completed')
        self.assertEqual(self.snapshot(identity)['status'], 'done')
        return owner, native['task_id']

    def answer(self, task_id, identity):
        decision = dict(id=identity, task_id=task_id, bot_id=self.dev['id'], status='executed', decided_by='person',
                        decided_at=now(), created_at=now(), title='Leave the case open?', action_type='question',
                        options=[], revision=1, selected_option_id='leave-open', selected_option_label='Leave it open',
                        context='Private delegate notes')
        with self.service.transaction():
            self.service._put('decisions', decision)
        return decision

    def check(self, identity, clock):
        # The scheduler's automatic check, given Coconut's current adapter snapshot of the card.
        with patch('coconut.native_outcomes.time.time', return_value=clock), self.service.transaction():
            self.service._native_block_check(self.service._get('tasks', identity), dict(self.service.kanban.tasks[identity]))
        return self.service._get('tasks', identity)

    def pause_turn(self, identity, payloads):
        """Call coconut_record_pause of the check turn as the gateway plugin calls it (gateway_chat.gateway_record_pause).
        The plugin returns a refusal of the service as an error result."""
        results = []
        for arguments in payloads:
            try:
                results.append(self.service.gateway_record_pause(identity, arguments))
            except ServiceError as error:
                self.assertTrue(400 <= error.status < 500, error)
                results.append({'error': str(error)})
        return results

    def test_check_records_the_delegate_answer_from_the_hermes_board(self):
        sender = self.task('Ask Dev to check the payout record')
        intake = self.relay(sender, self.dev, 'Check the payout record')
        # The intake creates two outcomes. The blocked outcome waits for one of them only.
        delegate, delegate_card = self.adopted_delegate(intake, 'Check the payout record')
        sibling, sibling_card = self.adopted_delegate(intake, 'Check the refund record')
        # The person's request comes first. A message from the person after an answer is new direction, so the
        # answers must follow the request that the blocked outcome serves.
        checked = self.task('Settle the payout case')
        answer = self.answer(delegate['id'], ANSWER)
        self.answer(sibling['id'], SIBLING)
        self.answer(intake['id'], 'intake-answer')

        # The blocked outcome's own claimed run links its card to the delegate's card, then blocks it for input.
        checked_card, row = self.card(checked['id'])
        run, lock = self.claim(checked_card)
        self.update(checked['id'], status='working', _native_turn_id='link-turn', _native_outcome_id=checked_card,
                    _kanban_started=run, _kanban_claim=dict(native_id=checked_card, external_id=checked['id'],
                                                            input=row['input'], run_id=run, claim_lock=lock))
        link = dict(parent_id=delegate_card, child_id=checked_card)
        linked = self.worker_tool(checked_card, run, lock, 'kanban_link', link)
        result = json.loads(linked)
        self.assertEqual((result.get('ok'), result.get('parent_id'), result.get('child_id'), result.get('gated')),
                         (True, delegate_card, checked_card, False), linked)
        self.record(checked['id'], 'kanban_link', link, linked)
        block = dict(task_id=checked_card, kind='needs_input', reason='The person declined the action this work needs.')
        blocked = self.worker_tool(checked_card, run, lock, 'kanban_block', block)
        self.assertEqual((json.loads(blocked).get('ok'), json.loads(blocked).get('block_kind')), (True, 'needs_input'), blocked)
        self.record(checked['id'], 'kanban_block', block, blocked)
        self.update(checked['id'], status='completed')
        card = self.snapshot(checked['id'])
        self.assertEqual((card['id'], card['status'], card['block_kind']), (checked_card, 'blocked', 'needs_input'))
        # Coconut's adapter snapshot lists the edge that the run added. The sibling has a card but no edge.
        self.assertIn(delegate_card, card['parent_ids'])
        self.assertNotIn(sibling_card, card['parent_ids'])

        # The automatic check offers the delegate's public answer only.
        self.check(checked['id'], 0)
        queued = self.check(checked['id'], 10 ** 9)
        self.assertEqual(queued['status'], 'queued')
        offered = {d['id']: d for d in queued['_self_recovery']['decisions']}
        self.assertIn(ANSWER, offered)
        self.assertNotIn(SIBLING, offered)
        self.assertNotIn('intake-answer', offered)
        self.assertEqual(set(offered[ANSWER]), PUBLIC_ANSWER | {'answer_fingerprint'})
        self.assertNotIn('Private delegate notes', json.dumps(queued['_self_recovery']))

        # The check turn claims the card again on the board.
        self.assertTrue(self.last_json(self.native_program(
            'import json; print(json.dumps(board.kb.unblock_task(board.conn, sys.argv[2])))', checked_card)))
        run, lock = self.claim(checked_card)
        self.update(checked['id'], status='working', _kanban_started=run, _kanban_claim=dict(
            native_id=checked_card, external_id=checked['id'], input=queued['_native_turn_id'], run_id=run, claim_lock=lock))
        card = self.snapshot(checked['id'])
        self.assertEqual(card['status'], 'running')
        self.assertIn(delegate_card, card['parent_ids'])
        self.check(checked['id'], 10 ** 9 + 1)
        counts = {table: len(self.service._all(table)) for table in ('decisions', 'deliveries', 'messages')}

        # The plugin offers coconut_record_pause with this schema.
        self.assertEqual(BLOCK_PAUSE_SCHEMA['parameters']['required'], ['decision_id'])
        self.assertIs(BLOCK_PAUSE_SCHEMA['parameters']['additionalProperties'], False)
        # Hermes does not apply the schema to the arguments, so an empty call reaches the service, which refuses it.
        refused, missing, recorded = self.pause_turn(checked['id'], [dict(decision_id=SIBLING), {}, dict(decision_id=ANSWER)])
        self.assertEqual(refused, dict(error='Choose an answered human decision from this check context.'))
        self.assertEqual(missing, dict(error='decision_id must contain between 1 and 100 characters.'))
        self.assertEqual(recorded, dict(status='recorded', decision_id=ANSWER))
        # The pause is saved in the committed task row.
        saved = json.loads(self.service.db.execute('SELECT data FROM tasks WHERE id=?', (checked['id'],)).fetchone()[0])
        self.assertEqual((saved['_block_check']['paused_by_decision_id'], saved['_block_check']['pause_turn']),
                         (ANSWER, queued['_native_turn_id']))
        self.assertEqual(saved['_block_check']['pause_fingerprint'], offered[ANSWER]['answer_fingerprint'])

        # The check turn blocks the card again, and the recorded pause holds the next check. Hermes sends a second
        # block of the same kind to triage, where the check waits for the person anyway. Another kind stays blocked,
        # so the next check reads the pause against the current board.
        again = dict(task_id=checked_card, kind='capability', reason='The declined action is still needed.')
        blocked = self.worker_tool(checked_card, run, lock, 'kanban_block', again)
        self.assertEqual((json.loads(blocked).get('ok'), json.loads(blocked).get('block_kind')), (True, 'capability'), blocked)
        self.record(checked['id'], 'kanban_block', again, blocked)
        self.update(checked['id'], status='completed')
        card = self.snapshot(checked['id'])
        self.assertEqual((card['status'], card['block_kind']), ('blocked', 'capability'))
        self.assertIn(delegate_card, card['parent_ids'])
        armed = self.check(checked['id'], 2 * 10 ** 9)['_block_check']
        self.assertEqual(armed['paused_by_decision_id'], ANSWER)
        due = max(armed['next_at'], 2 * 10 ** 9 + block_check_delay(armed['attempt']))
        self.assertEqual(self.check(checked['id'], due)['status'], 'completed')
        # Bookkeeping only: the answer and every card, delivery, and message stay as they were.
        self.assertEqual(self.service._get('decisions', ANSWER), answer)
        self.assertEqual({table: len(self.service._all(table)) for table in counts}, counts)

        # A later message from the person in the delegate's execution is new direction. The pause ends.
        with self.service.transaction():
            message = self.service._message(self.dev['id'], 'Close the case after all.', role='user', task_id=delegate['id'])
            self.service._put('messages', dict(message, _human_device_id='phone'))
        resumed = self.check(checked['id'], due + 10 ** 6)
        self.assertEqual(resumed['status'], 'queued')
        self.assertNotIn(ANSWER, {d['id'] for d in resumed['_self_recovery']['decisions']})


if __name__ == '__main__':
    unittest.main()
