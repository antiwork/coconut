"""Integration checks against the installed Hermes kernel and Coconut service."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service
import legacy_work_sync
from hermes_env import needs_hermes, PYTHON, SOURCE

setUpModule, tearDownModule = legacy_work_sync.scope()


class Provider(FixtureProvider):
    def __init__(self, root):
        super().__init__(root)
        self.hermes_root, self.python = Path(SOURCE), PYTHON
        self.started = []
        self.release = threading.Event()
        self.release.set()

    def start_task(self, task, bot, history, emit):
        self.started.append(task['id'])
        self.release.wait(15)
        super().start_task(task, bot, history, emit)

    def close(self):
        self.release.set()


@needs_hermes
class NativeKanbanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = Provider(self.tmp.name)
        self.service = Service(self.tmp.name, self.provider, start_workers=False)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.service.close())
        self.bot = self.write('/v1/bots', {'name': 'Desk', 'role': ''})

    def write(self, path, data, key=None):
        return self.service.mutate('POST', path, data, key or str(uuid4()))

    def task(self, text):
        return self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': text, 'mode': 'queue'})

    def tick(self):
        self.service._dispatch_pending()

    def settle(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.tick()
            if predicate():
                return
        self.fail('The native queue did not settle.')

    def count_claims(self):
        """Record the task or context of each placeholder process that a claim starts (kanban_worker_pid)."""
        claims = []
        start = self.service.start_gateway_holder

        def counted(identity, outcome=None):
            claims.append(identity)
            return start(identity, outcome)
        self.service.start_gateway_holder = counted
        return claims

    def native_program(self, program, *args):
        result = subprocess.run([PYTHON, '-c', "import sys\nfrom coconut.kanban_runtime import Board\nboard = Board(sys.argv[1])\n" + program,
            str(self.service.root / 'kanban'), *args],
            env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[1]), SOURCE))),
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_a_gateway_audit_writes_to_the_board_of_coconut_and_its_task_becomes_an_outcome(self):
        # An audit of a work source in the gateway calls coconut_kanban_create and coconut_kanban_comment. Coconut runs
        # kanban_create and kanban_comment of Hermes on its own board, as a worker of the audit runs them.
        with self.service.transaction():
            audit = self.service._send(self.service._get('bots', self.bot['id']), {'text': 'Review open assignment',
                                       'mode': 'queue'}, record_message=False, human_authored=False)
            audit = self.service._get('tasks', audit['id'])
            self.service._ensure_execution_session(audit)
            audit.update(status='working', _source_audit_prompt='Reconcile.', _native_turn_id='turn-1')
            self.service._put('tasks', audit)
        with patch.object(self.service, 'gateway_enabled', return_value=True):
            created = self.service.gateway_kanban_tool(audit['id'], 'kanban_create',
                                                       {'title': 'Fix the export', 'body': 'Scope and criteria.'})
            self.assertTrue(created.get('ok'), created)
            # The same call of the same turn creates no second task.
            again = self.service.gateway_kanban_tool(audit['id'], 'kanban_create',
                                                     {'title': 'Fix the export', 'body': 'Scope and criteria.'})
            self.assertEqual(again['task_id'], created['task_id'])
            commented = self.service.gateway_kanban_tool(audit['id'], 'kanban_comment',
                                                         {'task_id': created['task_id'], 'body': 'Source revision 3.'})
            # A later audit finds the task of the outcome and its comments before it records new facts.
            listed = self.service.gateway_kanban_tool(audit['id'], 'kanban_list', {})
            shown = self.service.gateway_kanban_tool(audit['id'], 'kanban_show', {'task_id': created['task_id']})
        self.assertTrue(commented.get('ok'), commented)
        self.assertIn(created['task_id'], [task['id'] for task in listed['tasks']], listed)
        self.assertEqual([comment['body'] for comment in shown['comments']], ['Source revision 3.'], shown)
        # A read has no receipt. The two writes and the repeated create do.
        receipts = [r['action']['name'] for r in self.service._all('native_receipts') if r['task_id'] == audit['id']]
        self.assertEqual(sorted(receipts), ['kanban_comment', 'kanban_create', 'kanban_create'])
        # The task is on Coconut's board for this bot, and Coconut adopts it as an outcome of the audit.
        self.assertEqual(self.native_program(
            "task = board.kb.get_task(board.conn, sys.argv[2]); print(task.tenant, task.status)", created['task_id']),
            self.bot['id'] + ' blocked')
        self.assertEqual(self.native_program(
            "print([c.body for c in board.kb.list_comments(board.conn, sys.argv[2])])", created['task_id']),
            "['Source revision 3.']")
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owners = [t for t in self.service._all('tasks') if t.get('_native_outcome_id') == created['task_id']]
        self.assertEqual(len(owners), 1)
        self.assertEqual(self.service._get('tasks', audit['id'])['_source_native_ids'], [created['task_id']])

        # The source changes while its outcome exists. The next audit finds the task of the outcome, records the new
        # revision on it, and creates no second task.
        with self.service.transaction():
            later = self.service._send(self.service._get('bots', self.bot['id']), {'text': 'Review open assignment',
                                       'mode': 'queue'}, record_message=False, human_authored=False)
            later = self.service._get('tasks', later['id'])
            self.service._ensure_execution_session(later)
            later.update(status='working', _source_audit_prompt='Reconcile revision 4.', _native_turn_id='turn-2')
            self.service._put('tasks', later)
        with patch.object(self.service, 'gateway_enabled', return_value=True):
            found = [task['id'] for task in self.service.gateway_kanban_tool(later['id'], 'kanban_list', {})['tasks']
                     if task['title'] == 'Fix the export']
            self.assertEqual(found, [created['task_id']])
            self.service.gateway_kanban_tool(later['id'], 'kanban_comment',
                                             {'task_id': found[0], 'body': 'Source revision 4.'})
        self.assertEqual(self.native_program(
            "print(len([t for t in board.kb.list_tasks(board.conn, tenant=sys.argv[2])]))", self.bot['id']), '1')
        self.assertEqual(self.native_program(
            "print([c.body for c in board.kb.list_comments(board.conn, sys.argv[2])])", created['task_id']),
            "['Source revision 3.', 'Source revision 4.']")
        self.assertEqual(self.service._get('tasks', later['id'])['_source_native_ids'], [created['task_id']])
        # A read shows only the tasks of the bot.
        other = self.native_program("""
print(board.kb.create_task(board.conn, title='Other bot work', body='x', assignee='default', tenant='other-bot',
    initial_status='blocked'))
""")
        from coconut.service import ServiceError
        with patch.object(self.service, 'gateway_enabled', return_value=True):
            self.assertNotIn(other, [task['id'] for task in
                                     self.service.gateway_kanban_tool(later['id'], 'kanban_list', {})['tasks']])
            with self.assertRaisesRegex(ServiceError, 'another bot'):
                self.service.gateway_kanban_tool(later['id'], 'kanban_show', {'task_id': other})

    PR = 'https://github.com/example/mobile/pull/393'

    def outcome(self, title):
        """A native outcome of the bot and its Coconut task, adopted from the board."""
        origin = self.task(title)
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title=sys.argv[2], body='Acceptance', assignee='default',
    tenant=sys.argv[3], session_id=sys.argv[4], initial_status='blocked'))
""", title, self.bot['id'], origin['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        # The message that asked for the outcome has its answer. Only the outcome waits for a run.
        with self.service.transaction():
            stored = self.service._get('tasks', origin['id'])
            stored['status'] = 'completed'
            self.service._put('tasks', stored)
        return identity, owner

    def comment(self, identity, body):
        self.native_program("board.kb.add_comment(board.conn, sys.argv[2], author='default', body=sys.argv[3])",
                            identity, body)

    def runs(self, identity):
        return int(self.native_program(
            "print(board.conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (sys.argv[2],)).fetchone()[0])",
            identity))

    def test_a_restart_that_leaves_a_dead_run_on_an_outcome_with_a_pr_respawns_it_once(self):
        identity, owner = self.outcome('Ship the mobile fix')
        entered, release = threading.Event(), threading.Event()
        def hold(task, bot, history, emit):
            if task['id'] != owner['id']:
                emit('completed', {'summary': 'Done.'})
                return
            # The worker runs the outcome until the service stops.
            entered.set()
            release.wait(30)
            raise RuntimeError('The service stopped.')
        self.provider.start_task = hold
        self.settle(entered.is_set)
        stale = self.service._get('tasks', owner['id'])['_kanban_claim']
        # The worker opened the PR and said so on the board. Then the service restarts, and the worker is gone.
        self.comment(identity, 'Opened ' + self.PR + ' for review.')
        self.service.stopping.set()
        release.set()
        self.service.running[owner['id']].join(10)
        self.service.close()
        self.provider.close()
        self.provider = Provider(self.tmp.name)
        self.service = Service(self.tmp.name, self.provider, start_workers=False)
        recovered = self.service._get('tasks', owner['id'])
        self.assertEqual((recovered['status'], recovered['summary']), ('queued', 'Resuming after the Mac service restarted.'))
        self.assertEqual(recovered['_kanban_claim']['run_id'], stale['run_id'])
        starts = []
        def run(task, bot, history, emit):
            if task['id'] != owner['id']:
                emit('completed', {'summary': 'Done.'})
                return
            starts.append(task.get('_native_outcome'))
            self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Merged', "
                                "expected_run_id=int(sys.argv[3]))", identity, str(task['_native_outcome']['run_id']))
            emit('completed', {'summary': 'Done.'})
        self.provider.start_task = run
        # Hermes reaps the dead run. Coconut sees the ended run of its claim and respawns the outcome once, past the
        # PR guard, with a new claim.
        self.settle(lambda: starts and self.service.kanban.tasks[owner['id']]['status'] == 'done')
        for _ in range(3):
            self.tick()
        self.assertEqual(len(starts), 1)
        self.assertNotEqual(starts[0]['run_id'], stale['run_id'])
        self.assertEqual(self.runs(identity), 2)
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')

    def test_a_run_that_is_alive_keeps_the_pr_guard(self):
        from coconut.kanban_scheduler import interrupted_claim
        process = subprocess.Popen(['sleep', '30'])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        claim = dict(run_id=7, pid=process.pid, native_id='t_one')
        ready = dict(id='t_one', status='ready', current_run_id=None)
        # No duplicate run while the worker of the claim still runs, or while the board runs the claim's run.
        self.assertFalse(interrupted_claim(claim, ready))
        self.assertFalse(interrupted_claim(dict(claim, pid=None), dict(ready, status='running', current_run_id=7)))
        self.assertFalse(interrupted_claim({}, ready))
        # A claim without a process proves no cut-off run.
        self.assertFalse(interrupted_claim(dict(claim, pid=None), ready))
        process.kill()
        process.wait()
        self.assertTrue(interrupted_claim(claim, ready))
        # The claim of another board task, for example before a successor card, does not recover this one.
        self.assertFalse(interrupted_claim(claim, dict(ready, id='t_two')))

    def test_a_restart_that_leaves_a_dead_run_on_a_board_task_with_a_pr_respawns_it_once(self):
        # The live state of Dev's task 3b3718b1: a task that Coconut put on the board (a binding, not an adopted
        # outcome, so no _native_outcome_id), claimed, with a PR on the board and a dead run after a restart.
        with self.service.transaction():
            task = self.service._send(self.service._get('bots', self.bot['id']), {'text': 'Fix the mobile export.',
                                      'mode': 'queue'}, human_authored=False)
            # An earlier turn gave the task its turn ID, so its claim and its next run have the same input.
            stored = self.service._get('tasks', task['id'])
            stored['_native_turn_id'] = str(uuid4())
            self.service._put('tasks', stored)
        entered, release = threading.Event(), threading.Event()
        def hold(started, bot, history, emit):
            if started['id'] != task['id']:
                emit('completed', {'summary': 'Done.'})
                return
            entered.set()
            release.wait(30)
            raise RuntimeError('The service stopped.')
        self.provider.start_task = hold
        self.settle(entered.is_set)
        stale = self.service._get('tasks', task['id'])['_kanban_claim']
        identity = stale['native_id']
        self.assertNotIn('_native_outcome_id', self.service._get('tasks', task['id']))
        self.assertEqual(stale['input'], stored['_native_turn_id'])
        self.comment(identity, 'Opened ' + self.PR + ' for review.')
        self.service.stopping.set()
        release.set()
        self.service.running[task['id']].join(10)
        self.service.close()
        self.provider.close()
        self.provider = Provider(self.tmp.name)
        self.service = Service(self.tmp.name, self.provider, start_workers=False)
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        starts = []
        def run(started, bot, history, emit):
            if started['id'] == task['id']:
                starts.append(started.get('_kanban_claim'))
            emit('completed', {'summary': 'Done.'})
        self.provider.start_task = run
        self.settle(lambda: starts and self.service._get('tasks', task['id'])['status'] == 'completed')
        for _ in range(3):
            self.tick()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]['native_id'], identity)
        self.assertNotEqual(starts[0]['run_id'], stale['run_id'])
        self.assertEqual(self.runs(identity), 2)

    def test_a_guarded_outcome_with_comments_keeps_one_guard_event_and_shows_its_guard(self):
        # A task that never ran waits for new input while a comment names its PR. The guard holds. Audits comment on
        # the task again and again, and the board keeps one guard event. After an hour Coconut shows the guard.
        identity, owner = self.outcome('Desk fast work intake')
        self.comment(identity, 'Source revision 3: see ' + self.PR + '.')
        starts = []
        self.provider.start_task = lambda task, *args: starts.append(task['id'])
        for revision in range(4, 8):
            self.tick()
            self.comment(identity, f'Source revision {revision}: see {self.PR}.')
        self.tick()
        self.assertNotIn(owner['id'], starts)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['dispatch_blocker'], 'active_pr')
        self.assertEqual(self.native_program(
            "print(board.conn.execute(\"SELECT count(*) FROM task_events WHERE task_id=? AND kind='respawn_guarded'\", "
            "(sys.argv[2],)).fetchone()[0])", identity), '1')
        from coconut.kanban_scheduler import RESPAWN_GUARD_NOTICE_SECONDS
        saved = self.service._get('tasks', owner['id'])['_respawn_guard']
        self.assertEqual(saved['reason'], 'active_pr')
        with self.assertLogs('coconut.kanban_scheduler', 'WARNING'):
            self.service._note_respawn_guards(saved['since'] + RESPAWN_GUARD_NOTICE_SECONDS + 1)
        self.assertIn('its respawn guard is active_pr', self.service._get('tasks', owner['id'])['summary'])

    CARD = dict(title='Refund the buyer?', context='The seller is suspended.',
                options=[dict(id='refund', label='Verify and refund', explanation=''),
                         dict(id='hold', label='Hold', explanation='')])

    def gateway(self):
        """Serve the gateway turns of the bot's profile, as the coconut adapter does. Return the turns that it took."""
        from coconut.service import ServiceError
        self.service.gateway_enabled = lambda: True
        bridge, home = self.service.gateway_bridge, str(self.service.profiles.home(self.service._get('bots', self.bot['id'])))
        turns, done = [], threading.Event()
        self.addCleanup(done.set)
        def serve():
            while not done.is_set():
                for turn in bridge.poll({'home': home, 'wait': .1, 'active': [t['id'] for t in turns]})['turns']:
                    turns.append(turn)
                    if 'Reconcile' in turn['text']:
                        # The audit records the source revision on Coconut's board and asks the person.
                        native = self.service.kanban.tasks[turn['text'] and self.audit_id]['id']
                        self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'kanban_comment',
                                                   'arguments': {'task_id': native, 'body': 'Source revision 2.'}})
                        try:
                            self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'ask', 'arguments': self.CARD})
                        except ServiceError:
                            pass
                    else:
                        bridge.event({'turn_id': turn['id'], 'kind': 'send', 'message_id': 'm:' + turn['id'], 'text': 'Refunded.'})
                    bridge.event({'turn_id': turn['id'], 'kind': 'done', 'outcome': 'success'})
        threading.Thread(target=serve, daemon=True).start()
        bridge.poll({'home': home, 'wait': 0})
        return turns

    def test_a_native_outcome_runs_in_the_gateway_and_ends_its_run_with_the_tools_of_its_claim(self):
        # A native outcome runs in the gateway. Its claim holds the gateway's placeholder, and Coconut runs the board
        # tools of the turn on its board for that claim. A turn that ends without a run-ending tool is a failed run,
        # as for a worker, and Hermes runs the card again.
        identity, owner = self.outcome('Ship the export')
        self.service.gateway_enabled = lambda: True
        bridge, home = self.service.gateway_bridge, str(self.service.profiles.home(self.service._get('bots', self.bot['id'])))
        turns, results, holders, done = [], [], [], threading.Event()
        self.addCleanup(done.set)
        def serve():
            while not done.is_set():
                for turn in bridge.poll({'home': home, 'wait': .1, 'active': [t['id'] for t in turns]})['turns']:
                    turns.append(turn)
                    def tool(name, **arguments):
                        results.append((name, self.service.gateway_tool({'turn_id': turn['id'], 'tool': name,
                                                                           'arguments': arguments})))
                    if turn['chat_id'] != self.bot['id']:
                        holders.append(self.service.gateway_holders.get(owner['id']))
                        tool('kanban_heartbeat', note='Exporting.')
                        tool('kanban_show')
                        if len(holders) > 1:
                            tool('kanban_complete', summary='The export ships.')
                    bridge.event({'turn_id': turn['id'], 'kind': 'send', 'message_id': 'm:' + turn['id'], 'text': 'Done.'})
                    bridge.event({'turn_id': turn['id'], 'kind': 'done', 'outcome': 'success'})
        threading.Thread(target=serve, daemon=True).start()
        bridge.poll({'home': home, 'wait': 0})
        self.settle(lambda: len(holders) > 1 or self.service._get('tasks', owner['id'])['status'] == 'failed')
        self.settle(lambda: self.service.kanban.tasks[owner['id']]['status'] == 'done'
                    and self.service._get('tasks', owner['id'])['status'] == 'completed')
        work = [turn for turn in turns if turn['chat_id'] != self.bot['id']]
        self.assertEqual([turn['mode'] for turn in work], ['outcome', 'outcome'])
        # No worker ran the outcome.
        self.assertEqual(self.provider.started, [])
        # The tools acted for each claim on Coconut's board: the heartbeat and the show of the claimed card.
        self.assertTrue(all(value.get('ok') for name, value in results if name == 'kanban_heartbeat'), results)
        self.assertTrue(all((value.get('task') or {}).get('id') == identity
                            for name, value in results if name == 'kanban_show'), results)
        self.assertEqual(self.native_program(
            "print(sorted({e.kind for e in board.kb.list_events(board.conn, sys.argv[2])} & {'heartbeat', 'completed'}))",
            identity), "['completed', 'heartbeat']")
        # The first turn ended without a run-ending tool, so its run failed and Hermes ran the card again. The second
        # turn completed it, and its placeholder stopped.
        self.assertEqual(self.runs(identity), 2)
        self.assertEqual(self.native_program(
            "print([r.outcome for r in board.kb.list_runs(board.conn, sys.argv[2])])", identity),
            "['worker_protocol_violation', 'completed']")
        self.assertTrue(all(holder is not None and holder.poll() is not None for holder in holders))

    def test_a_card_answer_of_a_gateway_audit_continues_in_the_gateway_from_its_claim(self):
        # From build 569 the continuation went to the worker, whose claim had only the gateway's placeholder, and the
        # task failed with "The native claim has no prepared worker." The receipt of the audit's board comment made the
        # turn look like work of the worker.
        with self.service.transaction():
            audit = self.service._send(self.service._get('bots', self.bot['id']), {'text': 'Review open assignment',
                                       'mode': 'queue'}, record_message=False, human_authored=False)
            stored = self.service._get('tasks', audit['id'])
            stored['_source_audit_prompt'] = 'Reconcile these open assignments against the original sources.'
            self.service._put('tasks', stored)
        self.audit_id = audit['id']
        turns = self.gateway()
        self.settle(lambda: any(d['task_id'] == audit['id'] for d in self.service._all('decisions')))
        card = next(d for d in self.service._all('decisions') if d['task_id'] == audit['id'])
        self.settle(lambda: self.service._get('tasks', audit['id'])['status'] == 'needs_you')
        # The board comment is a receipt of the turn.
        self.assertTrue(any(r['action']['name'] == 'kanban_comment' for r in self.service._all('native_receipts')
                            if r['task_id'] == audit['id']))
        self.service.mutate('POST', f'/v1/decisions/{card["id"]}/answer', {'option_id': 'refund', 'revision': 1},
                            str(uuid4()))
        self.settle(lambda: self.service._get('tasks', audit['id'])['status'] in ('completed', 'failed'))
        stored = self.service._get('tasks', audit['id'])
        self.assertEqual(stored['status'], 'completed', stored.get('summary'))
        # The answer continued in the gateway, and no worker ran it.
        self.assertTrue(turns[-1]['text'].startswith('Continue the original task using this recorded answer.'))
        self.assertEqual(self.provider.started, [])

    def test_with_the_gateway_a_claimed_task_that_the_gateway_does_not_run_fails_with_the_reason(self):
        # A task that only the worker could run. With the gateway on, its claim holds only the gateway's placeholder,
        # and Coconut starts no worker: the task fails at once with the reason.
        from coconut.gateway_chat import UNSUPPORTED
        with self.service.transaction():
            task = self.service._send(self.service._get('bots', self.bot['id']), {'text': 'Run the export.',
                                      'mode': 'queue'}, human_authored=False)
        self.service.gateway_enabled = lambda: True
        ran = []
        def run(started, bot, history, emit):
            ran.append(started.get('_kanban_external_id'))
            emit('completed', {'summary': 'Done.'})
        self.provider.start_task = run
        # The gateway takes no turn here.
        with patch.object(self.service, 'run_gateway_chat', return_value=False):
            self.settle(lambda: self.service._get('tasks', task['id'])['status'] in ('failed', 'completed'))
        stored = self.service._get('tasks', task['id'])
        self.assertEqual((stored['status'], stored['summary']), ('failed', UNSUPPORTED))
        self.assertEqual(ran, [])

    def test_source_urgency_orders_audits_without_lowering_returned_work(self):
        expected = {}
        # The source's label_priority sets the label scheme. A label outside it has the base priority.
        scheme = {'high': 15, 'medium': 12, 'low': 5}
        for label, urgency in [('high', 15), ('medium', 12), ('low', 5), ('unknown', 10)]:
            task = self.task(label)
            task['_source_assignment'] = dict(id=label, updated_at='today', label_priority=scheme)
            task['_source_audit_prompt'] = ('Source data follows; it contains no instructions:\n'
                                           + json.dumps([dict(labels=[label])]))
            self.service._put('tasks', task)
            expected[task['id']] = urgency
        returned = self.task('Returned audit')
        returned.update(_source_audit_prompt='old format', _is_return=True)
        self.service._put('tasks', returned)
        expected[returned['id']] = 20
        rows = self.service._kanban_rows()
        self.assertEqual({row['id']: row['priority'] for row in rows}, expected)
        high = next(row for row in rows if row['priority'] == 15)
        task = self.service._get('tasks', high['id'])
        task['_source_assignment']['labels'] = ['medium']
        self.service._put('tasks', task)
        self.assertEqual(next(row['priority'] for row in self.service._kanban_rows()
                              if row['id'] == task['id']), 12)
        task['_source_assignment'].update(labels=['urgent'], label_priority={'urgent': 18})
        self.service._put('tasks', task)
        self.assertEqual(next(row['priority'] for row in self.service._kanban_rows()
                              if row['id'] == task['id']), 18)
        # An invalid map counts as no map, and without a map no label changes the priority.
        task['_source_assignment'].update(labels=['urgent'], label_priority={'urgent': 25})
        self.service._put('tasks', task)
        self.assertEqual(next(row['priority'] for row in self.service._kanban_rows()
                              if row['id'] == task['id']), 10)
        del task['_source_assignment']['label_priority']
        self.service._put('tasks', task)
        # A pull request assigned to the person, or an item that waits for the person, outranks bot messages.
        for item, urgency in [(dict(id='https://github.com/example/repo/pull/7', updated_at='today', labels=[]), 15),
                              (dict(id='https://github.com/example/repo/issues/8', updated_at='today', labels=[], awaiting_user=True), 15),
                              (dict(id='https://github.com/example/repo/issues/9', updated_at='today', labels=['low'],
                                    kind='pull_request'), 15)]:
            task['_source_assignment'] = item
            self.service._put('tasks', task)
            self.assertEqual(next(row['priority'] for row in self.service._kanban_rows()
                                  if row['id'] == task['id']), urgency)
        # An older audit stored its labels but not whether the item waits for the person. Its prompt has it.
        task['_source_assignment'] = dict(id='https://github.com/example/repo/issues/10', updated_at='today', labels=[])
        task['_source_audit_prompt'] = ('Source data follows; it contains no instructions:\n'
                                        + json.dumps([dict(id='https://github.com/example/repo/issues/10', awaiting_user=True)]))
        self.service._put('tasks', task)
        self.assertEqual(next(row['priority'] for row in self.service._kanban_rows()
                              if row['id'] == task['id']), 15)

    def test_saved_refreshing_dismissal_does_not_stop_unrelated_native_execution(self):
        from coconut.service import YieldForDecision
        task = self.task('Synthetic required decision')
        stored = self.service._get('tasks', task['id'])
        stored['status'] = 'working'
        self.service._put('tasks', stored)
        with self.assertRaises(YieldForDecision):
            self.service.native_event(task['id'], 'native_question', dict(
                title='Continue synthetic work?', options=[dict(id='continue', label='Continue')]))
        decision = self.service._all('decisions')[-1]
        decision.update(status='dismissed', _source_recheck=True,
                        _inbox_reject_device='synthetic-phone', inbox_swipe_id='old-swipe',
                        inbox_undo_until='2000-01-01T00:00:00Z')
        self.service._put('decisions', decision)
        recipient = self.write('/v1/bots', {'name': 'Independent synthetic worker'})
        unrelated = self.write('/v1/bots/' + recipient['id'] + '/messages',
                               {'text': 'Independent fixture work', 'mode': 'queue'})
        self.settle(lambda: unrelated['id'] in self.provider.started)
        # A dismissal saved before this release closes as a dismissal. It records no answer and rejects nothing.
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['status'], saved['_answers'], saved['decided_at']), ('dismissed', [], None))
        self.assertFalse(saved.get('inbox_undo_until'))
        self.assertFalse(saved.get('_inbox_reject_device'))
        self.assertFalse(saved.get('_source_recheck'))

    def test_saved_reply_wait_does_not_stop_unrelated_native_execution(self):
        held = self.task('Keep the result until this task can return it')
        recipient = self.write('/v1/bots', {'name': 'Independent worker'})
        with self.service.transaction():
            task = self.service._get('tasks', held['id'])
            task.update(status='waiting', _return_pending=dict(target_bot_id=recipient['id'],
                text='Saved result', result_status='completed', return_id='saved-result'))
            self.service._put('tasks', task)
        runnable = self.write('/v1/bots/' + recipient['id'] + '/messages', {'text': 'Complete independent work', 'mode': 'queue'})
        self.settle(lambda: self.service._get('tasks', runnable['id'])['status'] == 'completed')
        self.assertIn(runnable['id'], self.provider.started)
        self.assertIn('_return_pending', self.service._get('tasks', held['id']))
        self.assertFalse(self.service._all('deliveries'))
        with self.service.transaction():
            task = self.service._get('tasks', held['id'])
            task['status'] = 'completed'
            self.service._put('tasks', task)
        self.service._drain_returns()
        self.service._drain_returns()
        self.assertNotIn('_return_pending', self.service._get('tasks', held['id']))
        self.assertEqual(len(self.service._all('deliveries')), 1)

    def test_restart_drain_retries_adapter_failures_then_dispatches_after_deadline(self):
        from contextlib import ExitStack
        active = self.task('Finish the current turn')
        with self.service.transaction():
            task = self.service._get('tasks', active['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        release = threading.Event()
        worker = threading.Thread(target=release.wait)
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(5)))
        self.service.running[active['id']] = worker
        bot = self.write('/v1/bots', {'name': 'Queued work'})
        queued = self.write('/v1/bots/' + bot['id'] + '/messages',
                            {'text': 'Run after the restart pause', 'mode': 'queue'})
        self.service.restart_enabled = True
        self.assertEqual(self.service.request_service_restart(active['id'], {})['status'], 'queued')

        call = self.service.kanban.call
        failures = [BrokenPipeError('adapter pipe closed'), RuntimeError('The Hermes Kanban adapter did not respond.')]
        retried = threading.Event()
        def flaky_adapter(request, *args):
            if self.service.restart_pending:
                self.assertEqual(request['op'], 'snapshot')
                if failures:
                    raise failures.pop(0)
                result = call(request, *args)
                retried.set()
                return result
            return call(request, *args)

        with ExitStack() as stack:
            maintenance = [stack.enter_context(patch.object(self.service, name)) for name in (
                'tick_routines', '_queue_context_recoveries', '_queue_decision_refreshes')]
            stack.enter_context(patch.object(self.service.kanban, 'call', side_effect=flaky_adapter))
            log = stack.enter_context(patch('coconut.service.logging.getLogger'))
            self.service.dispatcher = threading.Thread(target=self.service._dispatch)
            self.service.dispatcher.start()
            try:
                self.assertTrue(retried.wait(5), 'Drain adapter failures killed the dispatcher instead of retrying.')
                self.assertTrue(self.service.dispatcher.is_alive())
                self.assertEqual(log.return_value.exception.call_count, 2)
                for update in maintenance:
                    update.assert_not_called()
                self.assertEqual(self.provider.started, [])
                self.assertTrue(self.service.restart_pending)
                self.assertEqual(self.service._get('tasks', queued['id'])['status'], 'queued')

                self.service.restart_deadline = time.monotonic() - 1
                deadline = time.monotonic() + 5
                while self.service.restart_pending and time.monotonic() < deadline:
                    self.assertFalse(self.service.take_service_restart())
                    time.sleep(.01)
                self.assertFalse(self.service.restart_pending)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with self.service.lock:
                        if self.service._get('tasks', queued['id'])['status'] == 'completed':
                            break
                    time.sleep(.01)
                self.assertEqual(self.service._get('tasks', queued['id'])['status'], 'completed')
                self.assertEqual(self.provider.started, [queued['id']])
                self.assertTrue(self.service.dispatcher.is_alive())
                self.assertTrue(worker.is_alive())
                self.assertFalse(self.service.stopping.is_set())
                self.assertEqual(self.service._get('tasks', active['id'])['status'], 'working')
                for update in maintenance:
                    update.assert_called()
            finally:
                self.service.stopping.set()
                with self.service.changed:
                    self.service.changed.notify_all()
                self.service.dispatcher.join(5)
                self.assertFalse(self.service.dispatcher.is_alive())

    def legacy_work(self, title='Verify the saved export'):
        task = self.task(title)
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'working'
            self.service._put('tasks', owner)
        item = self.service._register_work(task['id'], dict(key=task['id'], title=title,
            status='working', activity='Checking results', completion_condition='The export passes its tests.'))
        return task, item

    def test_native_wait_and_completion_do_not_depend_on_child_receipts(self):
        import json
        from coconut.service import now
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        owner = self.service._get('tasks', task['id'])
        owner['status'] = 'waiting'
        self.service._put('tasks', owner)
        self.service._migrate_native_work()
        self.service.kanban.call({'op': 'snapshot'})
        native = self.service.kanban.tasks[task['id']]
        self.native_program("board.kb.schedule_task(board.conn, sys.argv[2], reason='Waiting for the existing worker')", native['id'])
        self.service.kanban.call({'op': 'snapshot'})
        bot = self.write('/v1/bots', dict(name='Developer', role='Run checks'))
        owner = self.service._get('tasks', owner['id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        child = self.service._store_bot_message(owner['id'], dict(target_bot_id=bot['id'],
            text='Run the acceptance check', require_response=True))
        worker = self.service._get('tasks', child['task_id'])
        worker.update(status='waiting')
        self.service._put('tasks', worker)
        owner = self.service._get('tasks', owner['id'])
        owner.update(status='waiting', _waiting_on=[worker['id']])
        self.service._put('tasks', owner)
        receipt = dict(id=str(uuid4()), task_id=worker['id'], turn_id='earlier-turn', state='completed',
            action={'name': 'terminal'}, result=json.dumps(dict(session_id='child-process', notify_on_complete=True)),
            _process_status='running', _process_observed_at=now())
        self.service._put('native_receipts', receipt)
        def state():
            return next(a for a in self.service.snapshot()['artifacts'] if a['id'] == item['id'])['work_state']
        self.assertEqual(state(), 'waiting')
        receipt['_process_delivery'] = 'result-received'
        self.service._put('native_receipts', receipt)
        self.assertEqual(state(), 'waiting')
        self.native_program("board.kb.unblock_task(board.conn, sys.argv[2]); board.kb.complete_task(board.conn, sys.argv[2], summary='Acceptance check passed')", native['id'])
        self.service.kanban.call({'op': 'snapshot'})
        receipt.pop('_process_delivery')
        self.service._put('native_receipts', receipt)
        self.assertEqual(state(), 'completed')

    def test_linked_pr_message_resumes_its_completed_native_outcome(self):
        from test_service_pr import Tracker, observation, URL
        self.service.pr_tracker = Tracker(observation())
        task, item = self.legacy_work()
        pr = self.service._register_pr_artifact(task['id'], dict(url=URL))
        self.provider.supports_native_outcomes = True
        owner = self.service._get('tasks', task['id'])
        owner['status'] = 'waiting'
        self.service._put('tasks', owner)
        self.service._migrate_native_work()
        self.service.kanban.call({'op': 'snapshot'})
        native = self.service.kanban.tasks[task['id']]
        other = self.write('/v1/bots', dict(name='Reviewer', role='Review'))
        copy = dict(self.service._get('artifacts', pr['id']), id=str(uuid4()), bot_id=other['id'])
        self.service._put('artifacts', copy)
        from coconut.service import ServiceError
        before = len(self.service._all('messages'))
        with self.assertRaises(ServiceError):
            self.write('/v1/bots/' + other['id'] + '/messages',
                dict(text='Add another field', work_artifact_id=copy['id'], mode='queue'))
        self.assertEqual(len(self.service._all('messages')), before)
        self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='Export verified')", native['id'])
        self.service.kanban.call({'op': 'snapshot'})
        sent = self.write('/v1/bots/' + self.bot['id'] + '/messages',
            dict(text='Add another export field', work_artifact_id=pr['id'], mode='queue'))
        continuation = self.service._get('tasks', sent['id'])
        self.assertEqual(continuation['_native_outcome_id'], native['id'])
        self.assertEqual(continuation['_work_context_id'], item['id'])

    def test_existing_work_migrates_once_with_same_workspace_and_card(self):
        from coconut.service import YieldForDecision
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        workspace = self.service.root / 'workspaces' / task['id']
        workspace.mkdir(parents=True)
        (workspace / 'saved.txt').write_text('Keep this result')
        with self.assertRaises(YieldForDecision):
            self.service.emit(task['id'], 'native_question', dict(title='Publish the export?',
                options=[dict(id='yes', label='Publish')]))
        card = self.service._all('decisions')[0]
        with self.service.transaction():
            self.service._migrate_native_work()
            self.service._migrate_native_work()
        self.service.kanban.call({'op': 'snapshot'})
        owner = self.service._get('tasks', task['id'])
        native = self.service.kanban.tasks[task['id']]
        self.assertEqual(owner['_native_outcome_id'], native['id'])
        self.assertEqual(self.service._get('artifacts', item['id'])['_native_outcome_id'], native['id'])
        self.assertEqual(native['workspace_path'], str(workspace))
        self.assertEqual((workspace / 'saved.txt').read_text(), 'Keep this result')
        self.assertEqual(self.service._all('decisions'), [card])
        self.assertNotEqual(native['status'], 'ready')
        self.assertEqual(len(self.service.kanban.tasks), 1)

    def test_native_title_and_progress_changes_reach_incremental_work_updates(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'waiting'
            self.service._put('tasks', owner)
            self.service._migrate_native_work()
        self.service.kanban.call({'op': 'snapshot'})
        native = self.service.kanban.tasks[task['id']]
        self.native_program("""
board.kb.claim_task(board.conn, sys.argv[2])
board.dispatch.heartbeat_worker(board.conn, sys.argv[2], note='Checking the file')
""", native['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._publish_kanban_changes()
        before = self.service.snapshot()['cursor']
        self.native_program("""
with board.kb.write_txn(board.conn):
    board.conn.execute('UPDATE tasks SET title=? WHERE id=?', ('Verify the corrected export', sys.argv[2]))
board.dispatch.heartbeat_worker(board.conn, sys.argv[2], note='Checking the exported rows')
""", native['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._publish_kanban_changes()
        updates = self.service.updates(before, collection_deltas=True)
        self.assertGreater(updates['cursor'], before)
        result = next(a for a in updates['artifacts'] if a['id'] == item['id'])
        self.assertEqual(result['title'], 'Verify the corrected export')
        self.assertEqual(result['activity'], 'Checking the exported rows')
        self.assertEqual(result['work_state'], 'working')
        self.service._publish_kanban_changes()
        self.assertEqual(self.service.snapshot()['cursor'], updates['cursor'])

    def test_completed_legacy_turn_becomes_unfinished_native_outcome(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'completed'
            self.service._put('tasks', owner)
            self.service._migrate_native_work()
        owner = self.service._get('tasks', task['id'])
        self.assertEqual(owner['status'], 'queued')
        self.service.kanban.call({'op': 'snapshot'})
        native = self.service.kanban.tasks[task['id']]
        self.assertEqual(native['status'], 'ready')
        self.assertIn('The export passes its tests.', native['body'])
        self.assertEqual(len(self.service._all('tasks')), 1)

    def test_migration_shares_history_reads_and_refreshes_after_writes(self):
        owners = []
        for title in ('First export', 'Second export'):
            task, item = self.legacy_work(title)
            alias = self.task('Read ' + title)
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'completed'
            self.service._put('tasks', owner)
            self.service._put('artifacts', dict(item, id=str(uuid4()),
                task_id=alias['id'], _same_outcome_as=item['id']))
            owners.append((owner['id'], alias['id']))
        self.provider.supports_native_outcomes = True
        with patch.object(self.service, '_all', wraps=self.service._all) as reads, \
                patch.object(self.service, '_read_all_tasks', wraps=self.service._read_all_tasks) as full, \
                patch.object(self.service, '_work_task_link_pairs', wraps=self.service._work_task_link_pairs) as graphs:
            self.service._migrate_native_work()
        self.assertEqual(graphs.call_count, 1)
        # The service keeps the parsed tasks and reads only the changed rows (TaskCache).
        self.assertEqual(sum(call.args == ('tasks',) for call in reads.call_args_list), 0)
        self.assertLessEqual(full.call_count, 1)
        for identity, alias in owners:
            self.assertNotIn('_native_outcome_id', self.service._get('tasks', identity))
            task = self.service._get('tasks', alias)
            task['status'] = 'completed'
            self.service._put('tasks', task)
        self.service._migrate_native_work()
        for identity, alias in owners:
            self.assertIn('_native_outcome_id', self.service._get('tasks', identity))

    def test_migration_preserves_cancelled_work_and_pending_alias_result(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'cancelled'
            self.service._put('tasks', owner)
            self.service._migrate_native_work()
        self.assertNotIn('_native_outcome_id', self.service._get('tasks', task['id']))
        task, item = self.legacy_work('Verify another export')
        alias = self.task('Read the returned result')
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'completed'
            self.service._put('tasks', owner)
            peer = dict(item, id=str(uuid4()), task_id=alias['id'], _same_outcome_as=item['id'])
            self.service._put('artifacts', peer)
            self.service._migrate_native_work()
        self.assertNotIn('_native_outcome_id', self.service._get('tasks', task['id']))
        self.assertEqual(self.service._get('tasks', alias['id'])['status'], 'queued')

    def test_migration_keeps_answered_pause_and_distinct_outcomes(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'completed'
            self.service._put('tasks', owner)
            decision = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=task['id'],
                status='rejected', decided_by='person')
            self.service._put('decisions', decision)
            item['paused_by_decision_id'] = decision['id']
            self.service._put('artifacts', item)
            self.service._migrate_native_work()
            self.assertNotIn('_native_outcome_id', self.service._get('tasks', task['id']))
            item.pop('paused_by_decision_id')
            self.service._put('artifacts', item)
            other = dict(item, id=str(uuid4()), title='Verify a second export')
            self.service._put('artifacts', other)
            self.service._migrate_native_work()
            self.assertNotIn('_native_outcome_id', self.service._get('tasks', task['id']))

    def test_migration_waits_for_the_wait_of_a_completed_alias(self):
        for link in ('_same_outcome_as', '_work_parent_id'):
            task, item = self.legacy_work()
            alias = self.task('Read the returned result')
            self.provider.supports_native_outcomes = True
            with self.service.transaction():
                for identity in (task['id'], alias['id']):
                    owner = self.service._get('tasks', identity)
                    owner['status'] = 'completed'
                    self.service._put('tasks', owner)
                self.service._put('artifacts', dict(item, id=str(uuid4()), task_id=alias['id'], **{link: item['id']}))
                # The alias waits for another task that has no result yet.
                record = dict(id=str(uuid4()), task_id=alias['id'], state='completed', result='{}',
                    action=dict(name='coconut_wait', arguments={}), _wait_task_id=str(uuid4()), _wait_session_id='session')
                self.service._put('native_receipts', record)
                self.service._migrate_native_work()
                self.assertNotIn('_native_outcome_id', self.service._get('tasks', task['id']))
                self.assertNotIn('_process_delivery', self.service._get('native_receipts', record['id']))

    def test_migration_reopens_answered_native_review_and_preserves_routine_wait(self):
        import json
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            self.service._ensure_execution_session(owner)
            row = self.service._kanban_row(owner, owner, 'review', 10)
            owner['status'] = 'queued'
            self.service._put('tasks', owner)
        self.native_program("board.sync([__import__('json').loads(sys.argv[2])])", json.dumps(row))
        with self.service.transaction():
            self.service._migrate_native_work()
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[task['id']]['status'], 'ready')
        other, other_item = self.legacy_work('Wait for external confirmation')
        with self.service.transaction(), patch.object(self.service, '_external_work_routine', return_value={'id': 'native-check'}):
            owner = self.service._get('tasks', other['id'])
            owner['status'] = 'completed'
            self.service._put('tasks', owner)
            self.service._migrate_native_work()
            self.assertNotIn('_native_outcome_id', self.service._get('tasks', other['id']))

    def test_migration_retains_uncertain_action_before_new_input(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner.update(status='failed', _native_turn_id='interrupted-input')
            self.service._put('tasks', owner)
            receipt = dict(id=str(uuid4()), task_id=owner['id'], turn_id='interrupted-input', state='executing',
                action=dict(name='terminal', arguments={'command': 'publish the prepared artifact'}))
            self.service._put('native_receipts', receipt)
            self.service._migrate_native_work()
            migrated = self.service._get('tasks', owner['id'])
            self.assertNotEqual(migrated['_native_turn_id'], 'interrupted-input')
            self.assertEqual(migrated['_self_recovery']['unknown_tools'], [receipt])
            self.assertEqual(self.service._get('native_receipts', receipt['id']), receipt)

    def test_migration_keeps_unknown_actions_from_alias_and_assignment(self):
        task, item = self.legacy_work()
        alias = self.task('Read the returned result')
        child = self.task('Finish the assigned check')
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            records = []
            for identity in (task['id'], alias['id'], child['id']):
                owner = self.service._get('tasks', identity)
                owner.update(status='failed', _conversation_epoch=self.service._setting('conversation_epoch'))
                if identity == child['id']:
                    owner.update(_assignment_task_id=task['id'], exchange_id=task['exchange_id'])
                self.service._put('tasks', owner)
                record = dict(id=str(uuid4()), task_id=identity, state='executing',
                    action=dict(name='terminal', arguments={'command': 'publish the prepared artifact'}))
                self.service._put('native_receipts', record)
                records.append(record)
            saved = dict(id=str(uuid4()), task_id='earlier-attempt', action={'name': 'terminal'})
            owner = self.service._get('tasks', task['id'])
            owner['_self_recovery'] = {'unknown_tools': [saved, records[0]]}
            self.service._put('tasks', owner)
            self.service._put('artifacts', dict(item, id=str(uuid4()), task_id=alias['id'], _same_outcome_as=item['id']))
            self.service._migrate_native_work()
            migrated = self.service._get('tasks', task['id'])
            self.assertCountEqual(migrated['_self_recovery']['unknown_tools'], [saved, *records])
            self.assertEqual(migrated['status'], 'queued')

    def test_migration_defers_a_wait_with_an_unknown_tool_result(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner.update(status='failed', _native_turn_id='interrupted-turn')
            self.service._put('tasks', owner)
            self.service._put('native_receipts', dict(id=str(uuid4()), task_id=owner['id'],
                turn_id='interrupted-turn', state='executing', result=None, action=dict(name='terminal', arguments={})))
            self.service._put('native_receipts', dict(id=str(uuid4()), task_id=owner['id'],
                turn_id='interrupted-turn', state='completed', result='{}', action=dict(name='coconut_wait', arguments={}),
                _wait_task_id=str(uuid4()), _wait_session_id='session'))
            self.service._migrate_native_work()
            saved = self.service._get('tasks', owner['id'])
            self.assertNotIn('_native_outcome_id', saved)
            self.assertEqual(saved['_native_turn_id'], 'interrupted-turn')
            self.assertEqual(saved['status'], 'failed')

    def test_saved_wait_defers_uncertain_results_from_linked_attempts(self):
        task, item = self.legacy_work()
        alias = self.task('Read the saved result')
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'completed'
            self.service._put('tasks', owner)
            child = self.service._get('tasks', alias['id'])
            child['status'] = 'failed'
            self.service._put('tasks', child)
            self.service._put('artifacts', dict(item, id=str(uuid4()), task_id=child['id'], _same_outcome_as=item['id']))
            self.service._put('native_receipts', dict(id=str(uuid4()), task_id=owner['id'], state='completed',
                result='{}', action=dict(name='coconut_wait', arguments={}), _wait_task_id=str(uuid4()),
                _wait_session_id='session'))
            self.service._put('native_receipts', dict(id=str(uuid4()), task_id=child['id'], state='executing',
                action=dict(name='terminal', arguments={'command': 'publish the prepared artifact'})))
            self.service._migrate_native_work()
            self.assertNotIn('_native_outcome_id', self.service._get('tasks', owner['id']))

    def test_migration_preserves_waits_from_a_previous_owner(self):
        for wait in ('wait', 'question', 'working'):
            task, item = self.legacy_work()
            previous = self.task('Previous attempt')
            self.provider.supports_native_outcomes = True
            with self.service.transaction():
                owner = self.service._get('tasks', task['id'])
                owner['status'] = 'completed'
                self.service._put('tasks', owner)
                old = self.service._get('tasks', previous['id'])
                old['status'] = 'working' if wait == 'working' else 'completed'
                self.service._put('tasks', old)
                item['_work_previous_task_ids'] = [old['id']]
                self.service._put('artifacts', item)
                if wait == 'wait':
                    self.service._put('native_receipts', dict(id=str(uuid4()), task_id=old['id'], state='completed',
                        action=dict(name='coconut_wait', arguments={}), result='{}', _wait_task_id=str(uuid4()),
                        _wait_session_id='session'))
                elif wait == 'question':
                    self.service._put('decisions', dict(id=str(uuid4()), task_id=old['id'], bot_id=old['bot_id'], status='pending'))
                self.service._migrate_native_work()
                self.assertNotIn('_native_outcome_id', self.service._get('tasks', owner['id']))

    def test_queued_migration_keeps_unknown_results_from_previous_owner(self):
        task, item = self.legacy_work()
        previous = self.task('Interrupted attempt')
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner.update(status='queued', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', owner)
            old = self.service._get('tasks', previous['id'])
            old['status'] = 'failed'
            self.service._put('tasks', old)
            item['_work_previous_task_ids'] = [old['id']]
            self.service._put('artifacts', item)
            receipt = dict(id=str(uuid4()), task_id=old['id'], state='executing', action={'name': 'terminal'})
            self.service._put('native_receipts', receipt)
            self.service._migrate_native_work()
            saved = self.service._get('tasks', owner['id'])
            self.assertEqual(saved['_self_recovery']['unknown_tools'], [receipt])
            self.assertEqual(saved['status'], 'queued')
            saved['status'] = 'failed'
            saved['_self_recovery']['saved_tasks'] = [{'id': old['id'], 'summary': 'Action result unknown'}]
            self.service._put('tasks', saved)
            for attempt in (1, 2):
                self.service._native_retry(saved, dict(status='ready', consecutive_failures=attempt))
                retried = self.service._get('tasks', saved['id'])
                self.assertEqual(retried['_self_recovery']['unknown_tools'], [receipt])
                self.assertEqual(retried['_self_recovery']['saved_tasks'], saved['_self_recovery']['saved_tasks'])
                saved = dict(retried, status='failed')

    def test_migration_preserves_alias_external_wait(self):
        task, item = self.legacy_work()
        alias = self.task('Wait for external confirmation')
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            for identity in (task['id'], alias['id']):
                owner = self.service._get('tasks', identity)
                owner['status'] = 'completed'
                self.service._put('tasks', owner)
            peer = dict(item, id=str(uuid4()), task_id=alias['id'], _work_parent_id=item['id'])
            self.service._put('artifacts', peer)
            with patch.object(self.service, '_external_work_routine', side_effect=lambda a: {'id': 'check'} if a['id'] == peer['id'] else None):
                self.service._migrate_native_work()
            self.assertNotIn('_native_outcome_id', self.service._get('tasks', task['id']))

    def test_service_forwards_each_accepted_input_to_native_intake(self):
        self.provider.supports_native_outcomes = True
        task = self.task('Start an outcome')
        captured = []
        def receive(request, bot, history, emit):
            captured.append(request['_native_turn_id'])
            self.assertEqual(request['_native_turn_id'], self.service._get('tasks', task['id'])['_native_turn_id'])
            emit('completed', {'summary': 'Complete.'})
        with patch.object(self.provider, 'start_task', side_effect=receive):
            self.service._run_task(task['id'])
            with self.service.transaction():
                current = self.service._get('tasks', task['id'])
                self.service._advance_input(current)
                current['status'] = 'queued'
                self.service._put('tasks', current)
            self.service._run_task(task['id'])
        self.assertEqual(len(captured), 2)
        self.assertNotEqual(captured[0], captured[1])

    def test_native_intake_retains_the_requesting_bot(self):
        from coconut.service import ServiceError
        self.provider.supports_native_outcomes = True
        source = self.task('Delegate an accepted outcome')
        developer = self.write('/v1/bots', {'name': 'Developer', 'role': 'Implement requested fixes'})
        with self.service.transaction():
            source = self.service._get('tasks', source['id'])
            source['status'] = 'working'
            self.service._put('tasks', source)
            origin = self.service._store_bot_message(source['id'],
                dict(target_bot_id=developer['id'], text='Implement the accepted result', require_response=True))
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Accepted result', body='Preserve the approved scope', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", developer['id'], origin['task_id'])
        from coconut.work_progress import NativeWork
        intake = self.service._get('tasks', origin['task_id'])
        tracking = NativeWork(dict(operation='start', task=intake))
        self.assertFalse(tracking.enabled)
        self.assertFalse(tracking.capture)
        with self.service.transaction():
            intake['status'] = 'working'
            self.service._put('tasks', intake)
        record = self.service.emit(intake['id'], 'native_call', dict(name='kanban_create', arguments={}))
        import json
        self.service.emit(intake['id'], 'native_result',
            dict(id=record['id'], result=json.dumps(dict(ok=True, task_id=identity))))
        with self.service.transaction():
            intake = self.service._get('tasks', intake['id'])
            intake['status'] = 'completed'
            self.service._finish_assignment_turn(intake, 'I started the work', 'completed')
            self.service._put('tasks', intake)
        self.assertNotIn('_return_pending', intake)
        self.service._drain_returns()
        self.assertFalse(any(d['source_task_id'] == intake['id'] for d in self.service._all('deliveries')))
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        self.assertEqual(owner['source_bot_id'], self.bot['id'])
        self.assertEqual(owner['source_task_id'], source['id'])
        self.assertEqual(owner['_relay_callback_owner'], origin['task_id'])
        with self.service.transaction():
            intake = self.service._get('tasks', origin['task_id'])
            intake['status'] = 'completed'
            self.service._finish_assignment_turn(intake, 'I started the work', 'completed')
            self.service._put('tasks', intake)
        self.assertNotIn('_return_pending', intake)
        self.service._drain_returns()
        self.assertFalse(any(d['source_task_id'] == intake['id'] for d in self.service._all('deliveries')))
        self.assertIn(intake['id'], self.service._get('tasks', source['id'])['_waiting_on'])
        with self.service.transaction():
            owner.update(status='failed', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', owner)
        self.native_program("board.kb.block_task(board.conn, sys.argv[2], reason='Awaiting corrected input')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        result = self.service.continue_native_work(source['id'],
            dict(task_id=identity, message='Continue within the accepted scope'))
        self.assertEqual(result['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'queued')
        unrelated = self.write('/v1/bots', {'name': 'Unrelated'})
        other = self.write('/v1/bots/' + unrelated['id'] + '/messages', {'text': 'Check something else'})
        with self.service.transaction():
            other = self.service._get('tasks', other['id'])
            other['status'] = 'working'
            self.service._put('tasks', other)
        with self.assertRaises(ServiceError) as rejected:
            self.service.continue_native_work(other['id'], dict(task_id=identity, message='Continue'))
        self.assertEqual(rejected.exception.status, 403)
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner.update(status='failed', summary='The worker failed')
            self.service._prepare_return(owner, owner['summary'], 'failed')
            self.service._put('tasks', owner)
        self.service._drain_returns()
        failed_callback = next(t for t in self.service._all('tasks')
                               if t.get('source_task_id') == owner['id'])
        self.assertEqual(failed_callback['_resolved_dependency_id'], intake['id'])
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            self.service._native_retry(owner, {'status': 'ready'})
        import json
        owner = self.service._get('tasks', owner['id'])
        row = self.service._kanban_row(owner, owner, 'ready', priority=10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner.update(status='completed', summary='The requested result is ready')
            self.service._prepare_return(owner, owner['summary'], 'completed')
            self.service._put('tasks', owner)
        self.assertNotIn('_return_pending', owner)
        self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='The requested result is ready')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        self.service._drain_returns()
        returned = [d for d in self.service._all('deliveries')
                    if d['source_task_id'] == owner['id'] and d['target_bot_id'] == self.bot['id']]
        self.assertEqual(len(returned), 2)
        self.assertEqual(returned[-1]['text'], 'The requested result is ready')
        completed_callback = self.service._get('tasks', returned[-1]['receipt']['task_id'])
        self.assertEqual(completed_callback['_resolved_dependency_id'], intake['id'])
        with self.service.transaction():
            self.service._finish_assignment_turn(completed_callback, 'Result received', 'completed')
        self.assertNotIn(intake['id'], self.service._get('tasks', source['id'])['_waiting_on'])
        self.service._reconcile_kanban()
        self.service._drain_returns()
        self.assertEqual(len([d for d in self.service._all('deliveries')
                              if d['source_task_id'] == owner['id'] and d['target_bot_id'] == self.bot['id']]), 2)
        self.service.continue_native_work(source['id'], dict(task_id=identity, message='Verify the later accepted input'))
        continued = self.service._get('tasks', owner['id'])
        row = self.service._kanban_row(continued, continued, 'ready', priority=10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        successor = self.service._get('tasks', owner['id'])['_native_outcome_id']
        self.assertNotEqual(successor, identity)
        self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='Later input verified')", successor)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        self.service._drain_returns()
        self.assertEqual([d['text'] for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']],
                         ['The worker failed', 'The requested result is ready', 'Later input verified'])

    def test_each_native_result_returns_after_shared_callback_work_finishes(self):
        import json
        self.provider.supports_native_outcomes = True
        source = self.task('Delegate two results')
        developer = self.write('/v1/bots', {'name': 'Developer'})
        with self.service.transaction():
            source = self.service._get('tasks', source['id'])
            source['status'] = 'working'
            self.service._put('tasks', source)
            request = self.service._store_bot_message(source['id'], dict(target_bot_id=developer['id'],
                text='Implement both results', require_response=True))
        identities = self.native_program("""
for title in ('First result', 'Second result'):
    print(board.kb.create_task(board.conn, title=title, assignee='default', tenant=sys.argv[2],
        session_id=sys.argv[3], initial_status='blocked'))
""", developer['id'], request['task_id']).splitlines()
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owners = [t for t in self.service._all('tasks') if t.get('_native_outcome_id') in identities]
        with self.service.transaction():
            intake = self.service._get('tasks', request['task_id'])
            intake.update(status='completed', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._finish_assignment_turn(intake, 'I started both results', 'completed')
            self.service._put('tasks', intake)
            parent = self.service._get('tasks', source['id'])
            parent.update(status='waiting', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', parent)
        for owner in owners:
            row = self.service._kanban_row(owner, owner, 'ready', priority=10)
            self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        with self.service.transaction():
            second = next(t for t in owners if t['_native_outcome_id'] == identities[1])
            second['status'] = 'completed'
            self.service._put('tasks', second)
        self.native_program("board.kb.block_task(board.conn, sys.argv[2], reason='Waiting for the external result')", identities[1])
        self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='First result verified')", identities[0])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        first = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identities[0])
        self.assertNotIn('_native_return_prepared', first)
        self.assertNotIn('_return_pending', first)
        self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='Second result verified')", identities[1])
        self.service.kanban.call({'op': 'snapshot'})
        for _ in range(3):
            self.service._reconcile_kanban()
            self.service._drain_returns()
        delivered = [d for d in self.service._all('deliveries') if d['source_task_id'] in {t['id'] for t in owners}]
        self.assertEqual({d['text'] for d in delivered}, {'First result verified', 'Second result verified'})
        self.assertEqual(len(delivered), 2)
        self.assertFalse(any(d['source_task_id'] == intake['id'] for d in self.service._all('deliveries')))

    def test_native_intake_is_idempotent_and_followup_keeps_owner(self):
        origin = self.task('Fix two separate defects')
        with self.service.transaction():
            stored = self.service._get('tasks', origin['id'])
            stored['_turn_attachment_ids'] = ['accepted-file']
            stored['_attachment_ids'] = ['accepted-file', 'later-file']
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        for title in ('First defect', 'Second defect'):
            self.service.native_event(origin['id'], 'native_call',
                {'name': 'kanban_create', 'arguments': {'idempotency_key': title,
                 'session_id': origin['id'], 'tenant': self.bot['id']}})
        with self.service.transaction():
            stored = self.service._get('tasks', origin['id'])
            self.service._advance_input(stored)
            stored['status'] = 'completed'
            self.service._put('tasks', stored)
        ids = self.native_program("""
for title in ('First defect', 'Second defect'):
    print(board.kb.create_task(board.conn, title=title, body='Accepted scope', assignee='default',
        tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked', idempotency_key=title))
""", self.bot['id'], origin['id']).splitlines()
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        self.service._adopt_native_outcomes()
        owned = [t for t in self.service._all('tasks') if t.get('_native_outcome_id')]
        self.assertEqual(len(owned), 2)
        self.assertTrue(all(t['_attachment_ids'] == ['accepted-file'] for t in owned))
        self.assertEqual({t['_native_outcome_id'] for t in owned}, set(ids))
        self.assertNotEqual(owned[0]['exchange_id'], owned[1]['exchange_id'])
        self.assertNotEqual(owned[0]['thread_id'], owned[1]['thread_id'])
        task = owned[0]
        item = self.service._get('artifacts', task['_work_context_id'])
        with self.service.transaction():
            task['status'] = 'failed'
            self.service._put('tasks', task)
            reply = self.service._send(self.service._get('bots', self.bot['id']),
                {'text': 'Continue using the saved result'}, work_item=item)
        self.assertEqual(reply['id'], task['id'])
        self.assertEqual(len(self.service._all('tasks')), 3)
        self.assertEqual(self.service._get('tasks', task['id'])['_native_outcome_id'], task['_native_outcome_id'])
        context_row = self.service._kanban_row({'id': str(uuid4())}, task, 'ready', 30)
        self.assertIsNone(context_row['native_id'])
        with self.service.transaction():
            self.service._cancel(self.service._get('tasks', task['id']), [])
        self.assertEqual(self.service._get('tasks', owned[1]['id'])['status'], 'queued')

    def test_native_view_and_maintenance_skip_the_duplicate_tracker(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'waiting'
            self.service._put('tasks', owner)
        self.service._migrate_native_work()
        self.service.kanban.call({'op': 'snapshot'})
        item = self.service._get('artifacts', item['id'])
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'queued'
            self.service._put('tasks', owner)
        with patch.object(self.service, '_all', side_effect=AssertionError('Do not rebuild history')), \
             patch.object(self.service, '_pending_completion_receipts', side_effect=AssertionError('Do not infer work from receipts')):
            self.service._sync_automatic_work()
            projected = self.service._work_projection([dict(item)])
        self.assertEqual(projected[0]['work_state'], 'queued')

    def test_native_alias_uses_its_original_outcome_without_history_reads(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        owner = self.service._get('tasks', task['id'])
        owner['status'] = 'waiting'
        self.service._put('tasks', owner)
        self.service._migrate_native_work()
        self.service.kanban.call({'op': 'snapshot'})
        item = self.service._get('artifacts', item['id'])
        other = self.write('/v1/bots', {'name': 'Coordinator', 'role': ''})
        alias = dict(item, id=str(uuid4()), bot_id=other['id'], _same_outcome_as=item['id'])
        alias.pop('_native_outcome_id')
        self.service._put('artifacts', alias)
        with patch.object(self.service, '_all', side_effect=AssertionError('Do not infer the alias from history')):
            original, projected = self.service._work_projection([dict(item), dict(alias)])
        self.assertEqual(projected['native_task_id'], original['native_task_id'])
        self.assertEqual(projected['work_outcome_id'], original['id'])
        self.assertEqual(projected['work_state'], original['work_state'])
        self.assertEqual(self.service._work_identity(alias), self.service._work_identity(item))

    def test_a_bot_on_hold_for_a_vacuum_gets_no_ready_row_and_no_claim(self):
        queued = self.task('Continue accepted work')
        self.assertEqual(next(r['state'] for r in self.service._kanban_rows() if r['id'] == queued['id']), 'ready')
        self.service._store_hold.add(queued['bot_id'])
        # The request keeps its place in the queue, but no turn can start while the store is rewritten.
        self.assertEqual(next(r['state'] for r in self.service._kanban_rows() if r['id'] == queued['id']), 'scheduled')
        self.service._store_hold.discard(queued['bot_id'])
        self.assertEqual(next(r['state'] for r in self.service._kanban_rows() if r['id'] == queued['id']), 'ready')

    def test_a_turn_that_was_claimed_before_the_hold_starts_after_it(self):
        task = self.task('Continue accepted work')
        # Claim the request, but leave the start of its turn to the next reconciliation.
        with patch.object(self.service, '_reconcile_kanban'):
            self.tick()
        self.assertNotIn(task['id'], self.service.running)
        self.service._store_hold.add(self.bot['id'])
        self.service._reconcile_kanban()
        # Reconciliation registers a turn as soon as it starts one.
        self.assertNotIn(task['id'], self.service.running)
        self.service._store_hold.discard(self.bot['id'])
        self.settle(lambda: task['id'] in self.provider.started)

    def test_scheduler_reads_active_rows_and_known_terminal_transitions(self):
        queued = self.task('Continue accepted work')
        finished = self.task('Completed input')
        unrelated = self.task('Old completed input')
        for value in (finished, unrelated):
            owner = self.service._get('tasks', value['id'])
            owner['status'] = 'completed'
            self.service._put('tasks', owner)
        self.service.kanban.tasks = {finished['id']: dict(id='t_finished', status='running')}
        original = self.service._all
        def bounded(table):
            if table in ('tasks', 'contexts'):
                raise AssertionError('Do not decode complete task history')
            return original(table)
        with patch.object(self.service, '_all', side_effect=bounded):
            rows = self.service._kanban_rows()
        self.assertEqual({r['id'] for r in rows}, {queued['id'], finished['id']})
        self.assertEqual(next(r['state'] for r in rows if r['id'] == finished['id']), 'done')

    def retained_pr(self):
        from coconut.service import now
        task = self.task('Finish the existing PR')
        owner = self.service._get('tasks', task['id'])
        owner.update(status='completed', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', owner)
        report = dict(id=str(uuid4()), kind='pr', task_id=owner['id'], _review_task_id=owner['id'],
            bot_id=owner['bot_id'], title='Correct the exported totals', url='https://github.com/example/project/pull/42',
            status='ready_for_review', pr_state='open', observed_at=now(), created_at=now())
        self.service._put('artifacts', report)
        self.provider.supports_native_outcomes = True
        return owner, report

    def test_retained_pr_migrates_with_its_owner_and_waiting_coordinator(self):
        owner, report = self.retained_pr()
        coordinator = self.task('Read the implementation result')
        source = self.service._get('tasks', coordinator['id'])
        source.update(status='waiting', _waiting_on=[owner['id']], _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', source)
        owner.update(source_task_id=source['id'], source_bot_id=source['bot_id'], exchange_depth=1)
        self.service._put('tasks', owner)
        peer = dict(report, id=str(uuid4()), task_id=source['id'], _review_task_id=source['id'])
        self.service._put('artifacts', peer)
        # The coordinator waits for another task that has no result yet.
        receipt = dict(id=str(uuid4()), task_id=source['id'], state='completed', result='{}',
            action=dict(name='coconut_wait', arguments={}), _wait_task_id=str(uuid4()), _wait_session_id='session')
        self.service._put('native_receipts', receipt)
        self.service._migrate_native_work()
        self.assertNotIn('_native_outcome_id', self.service._get('tasks', owner['id']))
        receipt['_process_delivery'] = 'delivered'
        self.service._put('native_receipts', receipt)
        self.service._migrate_native_work()
        migrated = self.service._get('tasks', owner['id'])
        self.assertTrue(migrated['_native_outcome_id'])
        self.assertEqual(migrated['source_task_id'], source['id'])
        self.assertEqual(self.service._get('tasks', source['id'])['_waiting_on'], [owner['id']])
        root = self.service._get('artifacts', report['id'])['work_outcome_id']
        self.assertEqual(self.service._get('artifacts', peer['id'])['work_outcome_id'], root)
        count = len(self.service._all('artifacts'))
        self.service._migrate_native_work()
        self.assertEqual(len(self.service._all('artifacts')), count)
        self.assertEqual(len(self.service._all('tasks')), 2)

    def test_retained_pr_ignores_unrelated_questions_without_a_merge_url(self):
        owner, report = self.retained_pr()
        other = self.task('A separate decision')
        self.service._put('decisions', dict(id=str(uuid4()), task_id=other['id'], bot_id=self.bot['id'],
            status='pending', merge_pr_url=None))
        self.service._migrate_native_work()
        self.assertTrue(self.service._get('tasks', owner['id'])['_native_outcome_id'])

    def test_retained_pr_migration_waits_for_a_separate_activity_execution(self):
        owner, report = self.retained_pr()
        active = self.task('Apply the current fix')
        report['_activity_task_id'] = active['id']
        self.service._put('artifacts', report)
        self.service._migrate_native_work()
        self.assertNotIn('_native_outcome_id', self.service._get('tasks', owner['id']))
        self.assertEqual(self.service._get('tasks', active['id'])['status'], 'queued')

    def test_retained_pr_migration_keeps_an_answered_merge_stop(self):
        owner, report = self.retained_pr()
        self.service._put('decisions', dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'],
            status='rejected', merge_pr_url=report['url'], created_at=report['created_at'], decided_by='phone'))
        self.service._migrate_native_work()
        self.assertNotIn('_native_outcome_id', self.service._get('tasks', owner['id']))
        self.assertNotIn('work_outcome_id', self.service._get('artifacts', report['id']))

    def test_retained_pr_migration_preserves_a_pending_merge_choice(self):
        owner, report = self.retained_pr()
        self.service._put('decisions', dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'],
            status='pending', merge_pr_url=report['url']))
        self.service._migrate_native_work()
        self.service.kanban.call({'op': 'snapshot'})
        migrated = self.service._get('tasks', owner['id'])
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'scheduled')
        item = self.service._get('artifacts', report['id'])
        self.assertEqual(self.service._work_projection([item])[0]['work_state'], 'needs_decision')
        self.assertEqual(migrated['status'], 'completed')

    def test_retained_pr_migration_waits_for_writers_and_preserves_stops(self):
        owner, report = self.retained_pr()
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        self.service._migrate_native_work()
        self.assertNotIn('work_outcome_id', self.service._get('artifacts', report['id']))
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        self.service.db.execute('INSERT INTO settings VALUES (?,?)', ('work.cancelled.' + report['url'], 'true'))
        self.service._migrate_native_work()
        self.assertNotIn('work_outcome_id', self.service._get('artifacts', report['id']))

    def exchange_review(self):
        """A review that Desk asks of Dev, as a turn of Dev's conversation. A new exchange now runs in a work session
        of Dev, but earlier exchanges joined the conversation (#308), and such tasks remain in stores. Dev reports the
        open PR and ends its turn, and the retained PR migration binds the PR's native outcome to that chat turn."""
        from coconut.service import now
        dev = self.write('/v1/bots', {'name': 'Dev', 'role': 'Review'})
        requester = self.task('Own the review of the PR')
        with self.service.transaction():
            stored = self.service._get('tasks', requester['id'])
            stored.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', stored)
        self.provider.supports_native_outcomes = True
        with patch.object(self.service, 'gateway_enabled', return_value=True):
            sent = self.service._deliver_bot_message(requester['id'], dict(
                target_bot_id=dev['id'], text='Review the PR privately.', require_response=True))
        with self.service.transaction():
            stored = self.service._get('tasks', requester['id'])
            stored['status'] = 'waiting'
            self.service._put('tasks', stored)
            review = self.service._get('tasks', sent['task_id'])
            # The earlier state: the exchange joined Dev's conversation.
            review.update(status='completed', _conversation_epoch=self.service._setting('conversation_epoch'),
                          _chat_session=True)
            self.service._put('tasks', review)
        self.assertTrue(review['_chat_session'])
        self.assertNotIn('_execution_session_id', review)
        self.assertEqual(self.service._get('tasks', requester['id'])['_waiting_on'], [review['id']])
        self.service._put('artifacts', dict(id=str(uuid4()), kind='pr', task_id=review['id'], _review_task_id=review['id'],
            bot_id=dev['id'], title='Correct the exported totals', url='https://github.com/example/project/pull/42',
            status='ready_for_review', pr_state='open', observed_at=now(), created_at=now()))
        self.service._migrate_native_work()
        self.service.kanban.call({'op': 'snapshot'})
        review = self.service._get('tasks', review['id'])
        self.assertTrue(review['_chat_session'])
        self.assertEqual(review['status'], 'queued')
        native = self.service.kanban.tasks[review['id']]
        self.assertEqual((native['id'], native['status']), (review['_native_outcome_id'], 'ready'))
        return self.service._get('tasks', requester['id']), review

    def claimed_starts(self, release=None):
        """Run each provider turn as a native worker does. It records the claim that it receives, and with that claim it
        completes the outcome's card with its result."""
        starts = []
        def run(task, bot, history, emit):
            claim = task.get('_native_outcome')
            starts.append((task['id'], claim, task.get('_kanban_claim')))
            self.provider.started.append(task['id'])
            if release is not None and not release.wait(15):
                raise AssertionError('The test did not release the claimed turn.')
            if claim:
                self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Review result saved', "
                                    "expected_run_id=int(sys.argv[3]))", claim['native_id'], str(claim['run_id']))
            emit('text_delta', {'text': 'The review passed.'})
            emit('completed', {'summary': 'Review result saved'})
        self.provider.start_task = run
        return starts

    def returns(self, source_id, target_bot_id):
        return [d for d in self.service._all('deliveries')
                if d['source_task_id'] == source_id and d['target_bot_id'] == target_bot_id]

    def stopped(self, task_id):
        return [m['text'] for m in self.service._task_messages(task_id) if m['text'].startswith('Work stopped')]

    def test_chat_turn_with_a_native_outcome_runs_once_from_its_claim(self):
        requester, review = self.exchange_review()
        starts = self.claimed_starts()
        self.settle(lambda: starts or self.stopped(review['id']))
        # No turn ran without the claim of the card, so none stopped with the missing claim and none returned a failure.
        self.assertEqual(self.stopped(review['id']), [])
        self.settle(lambda: self.service.kanban.tasks[review['id']]['status'] == 'done'
                    and self.service._get('tasks', review['id'])['status'] == 'completed'
                    and self.returns(review['id'], requester['bot_id']))
        reviews = [s for s in starts if s[0] == review['id']]
        self.assertEqual(len(reviews), 1)
        _, outcome, claim = reviews[0]
        native = self.service.kanban.tasks[review['id']]
        self.assertEqual(outcome, claim)
        self.assertEqual((claim['native_id'], claim['external_id']), (native['id'], review['id']))
        self.assertEqual(self.service._get('tasks', review['id'])['_native_outcome_id'], native['id'])
        # The requester receives the completed result once. More passes neither run the turn again nor send it again.
        for _ in range(5):
            self.tick()
        sent = self.returns(review['id'], requester['bot_id'])
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]['text'].startswith('Task failed'))
        self.assertEqual([s[0] for s in starts].count(review['id']), 1)
        self.assertEqual(self.native_program(
            "print(board.conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (sys.argv[2],)).fetchone()[0])",
            native['id']), '1')
        callback = next(t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == review['id'])
        self.assertEqual(callback['_assignment_task_id'], requester['id'])
        self.assertNotIn(review['id'], self.service._get('tasks', requester['id']).get('_waiting_on', []))

    def test_saved_claimless_failure_retries_once_from_the_claim_and_returns_one_result(self):
        # The saved state of the incident: the conversation turn that owns the outcome ran without its claim, stopped,
        # and returned that failure to the requester. Its card is ready and has no run.
        requester, review = self.exchange_review()
        with self.service.transaction():
            stored = self.service._get('tasks', review['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        self.service.emit(review['id'], 'failed', {'summary': "Work stopped: '_kanban_claim'"})
        self.service._drain_returns()
        failures = self.returns(review['id'], requester['bot_id'])
        self.assertEqual([d['text'] for d in failures], ["Task failed: Work stopped: '_kanban_claim'"])
        earlier = [t['id'] for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == review['id']]
        self.assertEqual(len(earlier), 1)
        self.service.kanban.call({'op': 'snapshot'})
        native = self.service.kanban.tasks[review['id']]
        self.assertEqual((native['status'], native.get('current_run_id')), ('ready', None))
        starts = self.claimed_starts()
        self.settle(lambda: self.service.kanban.tasks[review['id']]['status'] == 'done'
                    and len(self.returns(review['id'], requester['bot_id'])) == 2)
        for _ in range(5):
            self.tick()
        # The retry runs once, from the claim of the same card. No pass adds another failure for the requester.
        reviews = [s for s in starts if s[0] == review['id']]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0][1]['native_id'], native['id'])
        self.assertEqual(self.service._get('tasks', review['id'])['_native_outcome_id'], native['id'])
        self.assertEqual(self.stopped(review['id']), ["Work stopped: '_kanban_claim'"])
        sent = self.returns(review['id'], requester['bot_id'])
        self.assertEqual([d['text'].startswith('Task failed') for d in sent], [True, False])
        # The earlier failure callback is not rewritten and runs once as its own turn. The completed result reaches the
        # requester once, in one more callback.
        callbacks = [t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == review['id']]
        self.assertEqual(callbacks[0]['id'], earlier[0])
        self.assertEqual([t['status'] for t in callbacks], ['completed', 'completed'])
        self.assertEqual([s[0] for s in starts].count(earlier[0]), 1)
        self.assertEqual(self.native_program(
            "print(board.conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (sys.argv[2],)).fetchone()[0])",
            native['id']), '1')

    def test_requester_continues_a_conversation_outcome_on_its_successor_card(self):
        requester, review = self.exchange_review()
        starts = self.claimed_starts()
        self.settle(lambda: self.service.kanban.tasks[review['id']]['status'] == 'done'
                    and self.returns(review['id'], requester['bot_id']))
        first = self.service.kanban.tasks[review['id']]['id']
        callback = next(t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == review['id'])
        self.settle(lambda: self.service._get('tasks', callback['id'])['status'] == 'completed')
        care = self.write('/v1/bots', {'name': 'Care', 'role': ''})
        foreign = self.write('/v1/bots/' + care['id'] + '/messages', {'text': 'Unrelated work'})
        for source in (foreign, callback):
            with self.service.transaction():
                stored = self.service._get('tasks', source['id'])
                stored['status'] = 'working'
                self.service._put('tasks', stored)
        before = (self.service._get('tasks', review['id']), len(self.service._all('deliveries')))
        from coconut.service import ServiceError
        with self.assertRaisesRegex(ServiceError, 'Only the owner or its requesting bot can continue this work.') as error:
            self.service.emit(foreign['id'], 'work_continue', dict(task_id=first, message='Check another head.'))
        self.assertEqual(error.exception.status, 403)
        self.assertEqual((self.service._get('tasks', review['id']), len(self.service._all('deliveries'))), before)
        receipt = self.service.emit(callback['id'], 'work_continue', dict(task_id=first, message='Check the new head.'))
        self.assertEqual((receipt['task_id'], receipt['status']), (review['id'], 'queued'))
        self.settle(lambda: len([s for s in starts if s[0] == review['id']]) == 2
                    and self.service._get('tasks', review['id'])['status'] == 'completed'
                    and len(self.returns(review['id'], requester['bot_id'])) == 2)
        # The new input has a successor card. Its turn starts from that card's claim, never as a conversation turn.
        successor = self.service.kanban.tasks[review['id']]
        self.assertNotEqual(successor['id'], first)
        self.assertEqual(self.service._get('tasks', review['id'])['_native_outcome_id'], successor['id'])
        self.assertEqual([s[1]['native_id'] for s in starts if s[0] == review['id']], [first, successor['id']])
        self.assertTrue(self.service._get('tasks', review['id'])['_chat_session'])
        self.assertEqual(self.stopped(review['id']), [])
        for _ in range(5):
            self.tick()
        self.assertEqual(len([s for s in starts if s[0] == review['id']]), 2)
        self.assertEqual(len(self.returns(review['id'], requester['bot_id'])), 2)
        self.assertEqual(len(self.returns(review['id'], care['id'])), 0)

    def test_chat_turn_of_the_same_bot_waits_while_its_outcome_turn_is_claimed(self):
        requester, review = self.exchange_review()
        dev = self.service._get('bots', review['bot_id'])
        starts = self.claimed_starts()
        prepare = self.service.kanban_worker_pid
        chats, during_claim = [], []
        def claimed(identity, *args):
            pid = prepare(identity, *args)
            if identity == review['id'] and not chats:
                # A message to Dev arrives while the native pass claims the review, and the conversation dispatcher
                # runs on its own thread meanwhile. The review's execution is the bot's, so the chat turn shares it.
                chats.append(self.write('/v1/bots/' + dev['id'] + '/messages', {'text': 'A direct question for Dev'}))
                with self.service.lock:
                    self.service._dispatch_chat(4)
                    during_claim.append(set(self.service.running))
            return pid
        self.service.kanban_worker_pid = claimed
        self.settle(lambda: chats and self.service.kanban.tasks[review['id']]['status'] == 'done'
                    and self.service._get('tasks', chats[0]['id'])['status'] == 'completed')
        chat = self.service._get('tasks', chats[0]['id'])
        self.assertTrue(chat['_chat_session'])
        # The chat turn waited for the claimed turn, and each ran once.
        self.assertEqual(len(during_claim), 1)
        self.assertNotIn(chat['id'], during_claim[0])
        ids = [s[0] for s in starts]
        self.assertEqual((ids.count(review['id']), ids.count(chat['id'])), (1, 1))
        self.assertLess(ids.index(review['id']), ids.index(chat['id']))
        self.assertEqual(self.stopped(review['id']), [])

    def test_merge_card_waits_for_the_conversation_review_result_until_it_is_consumed(self):
        from coconut.decision_sources import github_links
        from coconut.pr_tracking import PRObservation
        from coconut.service import ServiceError, YieldForDecision
        URL, revision = 'https://github.com/example/project/pull/42', '2026-09-23T11:46:31Z'
        state = PRObservation('pending', 'a' * 40, 'Change', URL, 'open', '2026-09-13T10:00:00Z', True, 'open', False, 'pending')
        self.service.pr_tracker = type('Tracker', (), {'observe': lambda _, url: state, 'observe_discussion': lambda _, url: None,
                                                       'observe_issue': lambda _, url: dict(updated_at=revision)})()
        requester, review = self.exchange_review()
        def working(task_id):
            with self.service.transaction():
                stored = self.service._get('tasks', task_id)
                stored['status'] = 'working'
                self.service._put('tasks', stored)
        def ask(task_id):
            payload = dict(title='Merge this PR?', merge_pr_url=URL, risk_level='low',
                           risk='A bad merge reaches main until a revert.',
                           options=[dict(id='merge', label='Merge', explanation='Merge the reviewed change.')])
            payload['source_revisions'] = {url: revision for url in github_links(payload)}
            receipt = self.service.emit(task_id, 'native_call', {'name': 'coconut_ask', 'arguments': payload})
            self.service.emit(task_id, 'native_question', dict(payload, receipt_id=receipt['id']))
        working(requester['id'])
        self.service._register_pr_artifact(requester['id'], dict(url=URL))
        release = threading.Event()
        self.addCleanup(release.set)
        starts = self.claimed_starts(release)
        self.settle(lambda: starts)
        self.assertEqual(self.service._get('tasks', review['id'])['status'], 'working')
        # The reviewer's turn runs from its claim, so the requester cannot offer the merge yet.
        with self.assertRaisesRegex(ServiceError, 'pending worker or delegated result'):
            ask(requester['id'])
        # Its result is saved but not delivered yet. The card still waits.
        with patch.object(self.service, '_drain_returns'):
            release.set()
            self.settle(lambda: self.service.kanban.tasks[review['id']]['status'] == 'done'
                        and self.service._get('tasks', review['id'])['status'] == 'completed')
            self.assertEqual(self.returns(review['id'], requester['bot_id']), [])
            with self.assertRaisesRegex(ServiceError, 'pending worker or delegated result'):
                ask(requester['id'])
        self.assertEqual([d for d in self.service._all('decisions') if d.get('merge_pr_url')], [])
        # The requester's callback consumes the completed result once, and then it can offer the merge.
        self.settle(lambda: any(t.get('_resolved_dependency_id') == review['id'] for t in self.service._all('tasks')))
        sent = self.returns(review['id'], requester['bot_id'])
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]['text'].startswith('Task failed'))
        callback = next(t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == review['id'])
        working(callback['id'])
        with self.assertRaises(YieldForDecision):
            ask(callback['id'])
        cards = [d for d in self.service._all('decisions') if d.get('merge_pr_url')]
        self.assertEqual([(d['status'], d['task_id']) for d in cards], [('pending', callback['id'])])
        self.assertEqual(len([s for s in starts if s[0] == review['id']]), 1)

    def test_completed_history_import_removes_repeated_chain_reads(self):
        task, item = self.legacy_work('Verified export')
        self.provider.supports_native_outcomes = True
        owner = self.service._get('tasks', task['id'])
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        item.update(status='completed', completion_evidence='The exported rows match the source.')
        self.service._put('artifacts', item)
        self.service._migrate_native_work()
        item = self.service._get('artifacts', item['id'])
        self.assertIn('_completed_history', item)
        self.assertNotIn('_native_outcome_id', item)
        with patch.object(self.service, '_all', side_effect=AssertionError('Do not rebuild history')), \
             patch.object(self.service, '_pending_completion_receipts', side_effect=AssertionError('Do not read old receipts')):
            projected = self.service._work_projection([dict(item)])[0]
            self.service._migrate_native_work()
        self.assertEqual(projected['work_state'], 'completed')
        self.assertEqual(projected['completion_evidence'], item['completion_evidence'])
        item.update(status='working', activity='Apply the new request')
        self.service._put('artifacts', item)
        self.assertIsNone(self.service._retained_terminal_work(item))
        self.service._migrate_native_work()
        self.assertTrue(self.service._get('tasks', owner['id'])['_native_outcome_id'])

    def test_completed_report_does_not_block_a_different_outcome_for_its_owner(self):
        task, item = self.legacy_work('The completed outcome')
        self.provider.supports_native_outcomes = True
        owner = self.service._get('tasks', task['id'])
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        item.update(status='completed', completion_evidence='Verified the result.')
        self.service._put('artifacts', item)
        other = dict(item, id=str(uuid4()), title='The unfinished outcome', status='working', completion_evidence=None)
        self.service._put('artifacts', other)
        self.service._migrate_native_work()
        self.assertIn('_completed_history', self.service._get('artifacts', item['id']))
        self.assertTrue(self.service._get('artifacts', other['id'])['_native_outcome_id'])

    def test_completed_history_import_keeps_the_owner_summary_when_the_row_has_no_evidence(self):
        task, item = self.legacy_work('Verified export')
        self.provider.supports_native_outcomes = True
        owner = self.service._get('tasks', task['id'])
        owner.update(status='completed', summary='The owner verified the export.')
        self.service._put('tasks', owner)
        item.update(status='completed', completion_evidence=None, activity='', _automatic_work=True)
        self.service._put('artifacts', item)
        self.service._migrate_native_work()
        self.assertEqual(self.service._get('artifacts', item['id'])['_completed_history']['evidence'], 'The owner verified the export.')

    def test_completed_report_with_a_pending_decision_migrates_without_completion(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        owner = self.service._get('tasks', task['id'])
        owner['status'] = 'needs_you'
        self.service._put('tasks', owner)
        item.update(status='completed', completion_evidence='Local tests pass')
        self.service._put('artifacts', item)
        self.service._put('decisions', dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=owner['id'], status='pending'))
        self.service._migrate_native_work()
        current = self.service._get('artifacts', item['id'])
        self.assertNotIn('_completed_history', current)
        self.assertTrue(current['_native_outcome_id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'scheduled')
        self.assertEqual(self.service._work_projection([current])[0]['work_state'], 'needs_decision')

    def test_native_work_only_view_keeps_the_linked_pr_decision(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'waiting'
            self.service._put('tasks', owner)
        self.service._migrate_native_work()
        self.service.kanban.call({'op': 'snapshot'})
        item = self.service._get('artifacts', item['id'])
        reviewer = self.task('Review the existing PR')
        with self.service.transaction():
            pr = dict(item, id=str(uuid4()), kind='pr', work_outcome_id=item['id'],
                url='https://github.com/example/repo/pull/1')
            self.service._put('artifacts', pr)
            decision = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=reviewer['id'],
                status='pending', merge_pr_url=pr['url'])
            self.service._put('decisions', decision)
        full = self.service._work_projection([dict(item), dict(pr)])[0]
        partial = self.service._work_projection([dict(item)])[0]
        self.assertEqual(partial['work_state'], 'needs_decision')
        self.assertEqual(partial['work_decision_id'], decision['id'])
        self.assertEqual(full['work_decision_id'], partial['work_decision_id'])

    def test_native_view_preserves_legacy_pause_and_canonical_pr_links(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'completed'
            self.service._put('tasks', owner)
            decision = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=task['id'],
                status='rejected', decided_by='person')
            self.service._put('decisions', decision)
            item['paused_by_decision_id'] = decision['id']
            self.service._put('artifacts', item)
        paused = self.service._work_projection([dict(item)])[0]
        self.assertEqual(paused['work_state'], 'waiting')
        self.assertEqual(paused['activity'], 'Paused by your decision')
        with self.service.transaction():
            item.update(status='cancelled', completion_evidence='Stopped by the user')
            self.service._put('artifacts', item)
            alias = dict(item, id=str(uuid4()), _hidden=True, _work_parent_id=item['id'])
            self.service._put('artifacts', alias)
            pr = dict(item, id=str(uuid4()), kind='pr', url='https://github.com/example/repo/pull/1',
                work_outcome_id=alias['id'])
        projected = self.service._work_projection([pr])[0]
        self.assertEqual(projected['work_outcome_id'], item['id'])
        self.assertEqual(projected['work_state'], 'cancelled')

    def test_native_view_does_not_complete_legacy_work_with_a_pending_decision(self):
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'completed'
            self.service._put('tasks', owner)
            decision = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=task['id'], status='pending')
            self.service._put('decisions', decision)
            item.update(status='completed', completion_evidence='Local check passed')
            self.service._put('artifacts', item)
        projected = self.service._work_projection([dict(item)])[0]
        self.assertEqual(projected['work_state'], 'needs_decision')
        self.assertEqual(projected['work_decision_id'], decision['id'])

    def test_blocked_native_outcome_queues_one_owner_check(self):
        origin = self.task('Build the customer record')
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Build the customer record', body='Retain the approved scope', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], origin['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        import json
        # A live board has already synchronized this binding before the block.
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])',
                            json.dumps(self.service._kanban_row(owner, owner, 'ready', priority=10)))
        self.native_program("board.kb.unblock_task(board.conn, sys.argv[2]); "
                            "board.kb.block_task(board.conn, sys.argv[2], reason='Build space is below the limit', kind='capability')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[owner['id']]['block_kind'], 'capability')
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner.update(status='completed', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', owner)
        self.service._reconcile_kanban()
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        with patch('coconut.native_outcomes.time.time', return_value=time.time() + 3600):
            self.service._reconcile_kanban()
        owner = self.service._get('tasks', owner['id'])
        self.assertEqual(owner['status'], 'queued')
        self.assertEqual(owner['_self_recovery']['blocked']['kind'], 'capability')
        captured = []
        original = self.provider.start_task
        # The origin task can also dispatch. Capture only the owner's check turn.
        self.provider.start_task = lambda task, bot, history, emit: (
            task['id'] == owner['id'] and captured.append(task), original(task, bot, history, emit))
        self.settle(lambda: captured and not (self.service.running.get(owner['id']) and self.service.running[owner['id']].is_alive()))
        from turn_text import turn_input
        from coconut.work_recovery import BLOCKED_WORK_INSTRUCTIONS
        self.assertIn(BLOCKED_WORK_INSTRUCTIONS, turn_input(dict(operation='start', task=captured[0], history=[]))[0])
        # Starting the check turn unblocks the task. The backoff survives until the turn ends.
        self.assertEqual(self.service._get('tasks', owner['id'])['_block_check']['attempt'], 1)

    def test_check_turn_that_hermes_sends_to_triage_gets_no_new_check(self):
        origin = self.task('Fix the payout destinations')
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Fix the payout destinations', body='Retain the approved scope', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], origin['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])',
                            json.dumps(self.service._kanban_row(owner, owner, 'ready', priority=10)))
        # Hermes moves a task to triage when it blocks again with the same kind after an unblock.
        self.native_program("""
for _ in range(2):
    board.kb.unblock_task(board.conn, sys.argv[2])
    board.kb.block_task(board.conn, sys.argv[2], reason='paused_by_decision_id=other: resume only after a human reversal', kind='needs_input')
""", identity)
        self.service.kanban.call({'op': 'snapshot'})
        native = self.service.kanban.tasks[owner['id']]
        self.assertEqual((native['status'], native['block_kind'], native['block_recurrences']), ('triage', 'needs_input', 2))
        # The check turn ended with this block.
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner.update(status='completed', _conversation_epoch=self.service._setting('conversation_epoch'),
                         _native_turn_id='check-turn', _self_recovery=dict(blocked=dict(kind='needs_input', turn='check-turn')))
            self.service._put('tasks', owner)
        self.service._reconcile_kanban()
        # Each pass is past the due time. A deferred check is due again 30 minutes later.
        for hours in (1, 2, 3):
            with patch('coconut.native_outcomes.time.time', return_value=time.time() + 3600 * hours):
                self.service._reconcile_kanban()
            self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        # A later turn is not the check, so its block in the same triage state gets a check.
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner['_native_turn_id'] = 'message-turn'
            self.service._put('tasks', owner)
        with patch('coconut.native_outcomes.time.time', return_value=time.time() + 3600 * 4):
            self.service._reconcile_kanban()
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'queued')

    def test_native_work_status_does_not_follow_conversation_activity(self):
        origin = self.task('Discuss a pending outcome')
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Verify accepted outcome', body='Retain the result', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], origin['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        item = self.service._get('artifacts', owner['_work_context_id'])
        self.native_program("board.kb.block_task(board.conn, sys.argv[2], reason='Pending external result')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        with self.service.transaction():
            owner['status'] = 'working'
            self.service._put('tasks', owner)
        release = threading.Event()
        worker = threading.Thread(target=release.wait)
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(5)))
        self.service.running[owner['id']] = worker
        with patch.object(self.service, '_work_chain', side_effect=AssertionError('Native work needs no conversation graph')):
            projected = self.service._work_projection([dict(item)])[0]
        self.assertEqual(projected['work_state'], 'waiting')
        self.service.kanban.tasks = {}
        projected = self.service._work_projection([dict(item)])[0]
        self.assertEqual(projected['activity'], 'Status unavailable')
        self.assertEqual(projected['work_state'], 'waiting')

    def test_native_bot_result_resumes_same_owner_and_native_task(self):
        import json
        origin = self.task('Delegate one check')
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Check result', body='Verify the result', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], origin['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        helper = self.write('/v1/bots', {'name': 'Helper', 'role': ''})
        with self.service.transaction():
            owner['status'] = 'working'
            self.service._put('tasks', owner)
            result = self.service._store_bot_message(owner['id'], {'target_bot_id': helper['id'], 'text': 'Verify it'})
            owner = self.service._get('tasks', owner['id'])
            owner['status'] = 'waiting'
            self.service._put('tasks', owner)
            child = self.service._get('tasks', result['task_id'])
            child['status'] = 'completed'
            self.service._put('tasks', child)
            row = next(r for r in self.service._kanban_rows() if r['id'] == owner['id'])
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'scheduled')
        count = len(self.service._all('tasks'))
        payload = {'target_bot_id': self.bot['id'], 'text': 'Verified the result.', 'return_id': 'result-one'}
        receipt = self.service._deliver_bot_message(child['id'], payload, returning=True)
        duplicate = self.service._deliver_bot_message(child['id'], payload, returning=True)
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertEqual(duplicate['status'], 'duplicate')
        self.assertEqual(len(self.service._all('tasks')), count)
        resumed = self.service._get('tasks', owner['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertFalse(resumed['_waiting_on'])
        self.assertEqual(len(resumed['_bot_messages']), 1)
        with self.service.transaction():
            resumed['_background_completion'] = ['Saved process output']
            resumed['_resume_decision'] = 'answered-card'
            self.service._put('tasks', resumed)
        self.service._deliver_bot_message(child['id'], dict(payload, return_id='result-two', text='Second result'), returning=True)
        again = self.service._get('tasks', owner['id'])
        self.assertEqual(again['_native_turn_id'], resumed['_native_turn_id'])
        self.assertEqual(again['_background_completion'], ['Saved process output'])
        self.assertEqual(again['_resume_decision'], 'answered-card')
        row = self.service._kanban_row(resumed, resumed, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'ready')
        self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='Verified')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')

    def test_native_completion_survives_restart_and_keeps_saved_followup(self):
        import json
        for followup in (False, True):
            origin = self.task('Finish one outcome')
            identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Verified outcome', body='Acceptance', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], origin['id'])
            self.service.kanban.call({'op': 'snapshot'})
            self.service._adopt_native_outcomes()
            owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
            with self.service.transaction():
                row = next(r for r in self.service._kanban_rows() if r['id'] == owner['id'])
                owner = self.service._get('tasks', owner['id'])
                owner['_kanban_claim'] = {}
                self.service._put('tasks', owner)
            self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
            def complete_then_stop(task, bot, history, emit):
                if followup:
                    with self.service.transaction():
                        self.service._reply(self.service._get('tasks', task['id']), {'text': 'Also verify the second case.'})
                self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='Verified once')", identity)
                self.service.stopping.set()
                raise RuntimeError('Service stops after native completion')
            with patch.object(self.provider, 'start_task', side_effect=complete_then_stop):
                self.service._run_task(owner['id'])
            self.assertTrue(self.service.stopping.is_set())
            self.service.stopping.clear()
            with self.service.transaction():
                self.service._recover()
            self.service.kanban.call({'op': 'snapshot'})
            cursor = self.service._cursor()
            self.service._reconcile_kanban()
            page = self.service.updates(cursor)
            self.assertIn(owner['id'], [t['id'] for t in page['tasks']])
            recovered = self.service._get('tasks', owner['id'])
            self.assertEqual(recovered['status'], 'queued' if followup else 'completed')
            self.assertEqual(bool(recovered.get('_steering')), followup)
            row = self.service._kanban_row(recovered, recovered, 'ready' if followup else 'done', 10)
            self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
            self.service.kanban.call({'op': 'snapshot'})
            self.assertEqual(self.service.kanban.tasks[owner['id']]['id'] == identity, not followup)

    def test_native_context_preserves_workspace_and_blocked_reply_advances_input(self):
        origin = self.task('Keep the outcome workspace')
        workspace = Path(self.tmp.name) / 'native-scratch'
        workspace.mkdir()
        (workspace / 'receipt.txt').write_text('Saved result')
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Workspace outcome', body='Acceptance', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked', workspace_kind='dir', workspace_path=sys.argv[4]))
""", self.bot['id'], origin['id'], str(workspace))
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        context = self.service._kanban_row({'id': str(uuid4())}, owner, 'ready', 30)
        self.assertEqual(context['workspace'], str(workspace))
        successor = dict(owner, id=str(uuid4()))
        next_row = self.service._kanban_row(successor, successor, 'ready', 10)
        self.assertEqual(next_row['workspace'], str(workspace))
        import json
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program("""
import json
board.sync([json.loads(sys.argv[2]), json.loads(sys.argv[3])])
board.kb.block_task(board.conn, sys.argv[4], reason='Worker setup failed')
""", json.dumps(row), json.dumps(context), identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[owner['id']]['workspace_path'], str(workspace))
        with self.service.transaction():
            self.service._reply(owner, {'text': 'Setup is fixed. Continue.'})
        owner = self.service._get('tasks', owner['id'])
        self.assertNotEqual(owner['_native_turn_id'], row['input'])
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program("import json; board.sync([json.loads(sys.argv[2])])", json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'ready')
        self.assertEqual((workspace / 'receipt.txt').read_text(), 'Saved result')
        # Use the real native triage state to check an explicit continuation.
        self.native_program("with board.kb.write_txn(board.conn): board.conn.execute(\"UPDATE tasks SET status='triage' WHERE id=?\", (sys.argv[2],))", identity)
        self.service.kanban.call({'op': 'snapshot'})
        with self.service.transaction():
            self.service._reply(self.service._get('tasks', owner['id']), {'text': 'Continue with the corrected setup.'})
        owner = self.service._get('tasks', owner['id'])
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'ready')

    def test_bot_continuation_returns_triaged_native_outcome_to_ready(self):
        import json
        task, item = self.legacy_work()
        self.provider.supports_native_outcomes = True
        with self.service.transaction():
            owner = self.service._get('tasks', task['id'])
            owner['status'] = 'completed'
            self.service._put('tasks', owner)
            self.service._migrate_native_work()
        owner = self.service._get('tasks', task['id'])
        identity = owner['_native_outcome_id']
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.native_program("with board.kb.write_txn(board.conn): board.conn.execute(\"UPDATE tasks SET status='triage' WHERE id=?\", (sys.argv[2],))", identity)
        self.service.kanban.call({'op': 'snapshot'})
        workspace = self.service.kanban.tasks[owner['id']]['workspace_path']
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        source = self.task('The review result is available')
        source = self.service._get('tasks', source['id'])
        source['status'] = 'working'
        self.service._put('tasks', source)
        result = self.service.emit(source['id'], 'work_continue',
            dict(task_id=identity, message='Read the completed review and finish the accepted outcome.'))
        resumed = self.service._get('tasks', result['task_id'])
        self.assertEqual(resumed['id'], owner['id'])
        self.assertNotEqual(resumed['_native_turn_id'], owner['_native_turn_id'])
        row = self.service._kanban_row(resumed, resumed, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        current = self.service.kanban.tasks[owner['id']]
        self.assertEqual(current['id'], identity)
        self.assertEqual(current['status'], 'ready')
        self.assertEqual(current['workspace_path'], workspace)

    def test_bot_continuation_supplies_fresh_input_to_same_scheduled_native_outcome(self):
        import json
        task = self.task('Finish the saved native outcome')
        owner = self.service._get('tasks', task['id'])
        original_execution = owner['_execution_session_id'] = owner['id']
        original = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(original))
        self.service.kanban.call({'op': 'snapshot'})
        identity = self.service.kanban.tasks[owner['id']]['id']
        workspace = self.service.kanban.tasks[owner['id']]['workspace_path']
        self.native_program(
            "board.kb.schedule_task(board.conn, sys.argv[2], reason='Waiting for Coconut input readiness.')",
            identity)
        self.service.kanban.call({'op': 'snapshot'})
        owner.update(status='completed', _kanban_claim={
            'native_id': identity, 'input': original['input'], 'run_id': 'completed-run'})
        owner.pop('_native_outcome_id', None)
        self.service._put('tasks', owner)
        source = self.service._get('tasks', self.task('Continue with the completed review')['id'])
        source['status'] = 'working'
        self.service._put('tasks', source)

        receipt = self.service.emit(source['id'], 'work_continue', dict(
            task_id=identity, message='The review passed. Finish the original outcome.'))
        continued = self.service._get('tasks', receipt['task_id'])
        fresh = self.service._kanban_row(continued, continued, 'ready', 10)
        self.assertEqual(fresh['previous_input'], original['input'])
        self.assertNotEqual(fresh['input'], original['input'])
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(fresh))
        self.service.kanban.call({'op': 'snapshot'})
        current = self.service.kanban.tasks[owner['id']]
        self.assertEqual(current['id'], identity)
        self.assertEqual(current['status'], 'ready')
        self.assertEqual(current['workspace_path'], workspace)
        self.assertEqual(continued['_execution_session_id'], original_execution)

    def test_linked_delegated_pr_dispatches_in_original_native_workspace(self):
        import json
        source = self.task('Coordinate the correction')
        dev = self.write('/v1/bots', {'name': 'Dev'})
        owner = self.write('/v1/bots/' + dev['id'] + '/messages',
            {'text': 'Review the correction', 'mode': 'queue'})
        source = self.service._get('tasks', source['id'])
        owner = self.service._get('tasks', owner['id'])
        workspace = Path(self.tmp.name) / 'original-dev-workspace'
        workspace.mkdir()
        (workspace / 'receipt.txt').write_text('Original Dev execution')
        owner.update(status='completed', source_bot_id=source['bot_id'],
            source_task_id=source['id'], parent_task_id=source['id'],
            _execution_session_id=owner['id'])
        original = self.service._kanban_row(owner, owner, 'ready', 10)
        original['workspace'] = str(workspace)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(original))
        self.service.kanban.call({'op': 'snapshot'})
        identity = self.service.kanban.tasks[owner['id']]['id']
        self.native_program(
            "board.kb.complete_task(board.conn, sys.argv[2], summary='Review finished')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        owner.update(_kanban_claim={'native_id': identity, 'input': original['input'],
                                    'workspace': str(workspace), 'run_id': 'completed-run'})
        owner.pop('_native_outcome_id', None)
        self.service._put('tasks', owner)
        source['status'] = 'working'
        self.service._put('tasks', source)
        work = dict(id='requester-work', task_id=source['id'], bot_id=source['bot_id'],
            kind='work', title='Existing request', status='waiting', _native_outcome_id='t_requester')
        pr = dict(id='dev-pr', task_id=owner['id'], bot_id=owner['bot_id'],
            kind='pr', title='Existing draft', status='open', pr_state='open',
            url='https://github.com/example/project/pull/2', work_outcome_id=work['id'],
            _review_task_id=owner['id'], _activity_task_id=owner['id'])
        self.service._put('artifacts', work)
        self.service._put('artifacts', pr)
        receipt = self.service.emit(source['id'], 'work_continue', dict(
            task_id=identity, message='Continue the approved correction in the original workspace.'))
        continued = self.service._get('tasks', receipt['task_id'])
        row = self.service._kanban_row(continued, continued, 'ready', 10)
        self.assertEqual(row['workspace'], str(workspace))
        self.assertEqual(row['execution_id'], owner['id'])
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        current = self.service.kanban.tasks[owner['id']]
        self.assertEqual(current['status'], 'ready')
        self.assertEqual(current['workspace_path'], str(workspace))
        self.settle(lambda: owner['id'] in self.provider.started)
        # The claim of the continued owner is the current card in the original workspace.
        claim = self.service._get('tasks', owner['id'])['_kanban_claim']
        self.assertEqual((claim['native_id'], claim['workspace']), (current['id'], str(workspace)))
        self.assertEqual((workspace / 'receipt.txt').read_text(), 'Original Dev execution')
        self.assertEqual(self.service._get('artifacts', work['id']), work)
        self.assertEqual(self.service._get('artifacts', pr['id'])['work_outcome_id'], work['id'])

    def test_claim_only_terminal_continuation_requires_saved_input(self):
        import copy
        import json
        from coconut.service import ServiceError
        task = self.task('Do not falsely continue a terminal claim')
        owner = self.service._get('tasks', task['id'])
        owner['_execution_session_id'] = owner['id']
        original = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(original))
        self.service.kanban.call({'op': 'snapshot'})
        identity = self.service.kanban.tasks[owner['id']]['id']
        self.native_program(
            "board.kb.complete_task(board.conn, sys.argv[2], summary='Original input complete')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        owner.update(status='completed', _kanban_claim={
            'native_id': identity, 'run_id': 'completed-run'})
        owner.pop('_native_outcome_id', None)
        self.service._put('tasks', owner)
        source = self.service._get('tasks', self.task('Try to continue the terminal claim')['id'])
        source['status'] = 'working'
        self.service._put('tasks', source)
        saved = copy.deepcopy(owner)
        deliveries = copy.deepcopy(self.service._all('deliveries'))

        with patch.object(self.service, '_background_pending', return_value=False), \
                self.assertRaisesRegex(ServiceError, 'existing native outcome'):
            self.service.emit(source['id'], 'work_continue', dict(
                task_id=identity, message='This must not report a queued continuation.'))

        self.assertEqual(self.service._get('tasks', owner['id']), saved)
        self.assertEqual(self.service._all('deliveries'), deliveries)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[owner['id']]['id'], identity)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'done')

    def test_native_reply_cannot_complete_outcome_and_retry_keeps_identity(self):
        origin = self.task('Complete an outcome')
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Real outcome', body='Verify the result', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], origin['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        task = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        self.native_program("""
board.kb.claim_task(board.conn, sys.argv[2], claimer='default')
task = board.kb.get_task(board.conn, sys.argv[2])
board.finish(task.id, task.current_run_id, 'done', 'I will do this next')
assert board.kb.get_task(board.conn, task.id).status != 'done'
""", identity)
        self.service.kanban.call({'op': 'snapshot'})
        native = self.service.kanban.tasks[task['id']]
        self.assertEqual(native['consecutive_failures'], 1)
        with self.service.transaction():
            task['status'] = 'completed'
            self.service._put('tasks', task)
            self.service._native_retry(task, native)
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['_native_outcome_id'], identity)
        self.assertEqual(task['_self_recovery']['attempt'], 1)

    def test_completed_reply_cannot_park_ready_native_outcome(self):
        self.native_program("""
workspace = board.kb.kanban_db_path().parent / 'saved-work'
workspace.mkdir()
row = dict(id='original-owner', bot_id='owner', title='Finish accepted work', state='ready',
           input='same-input', workspace=str(workspace), priority=10, summary='Complete.')
board.sync([row])
identity = board.snapshot()[0]['id']
board.bind(identity, row['id'])
row['native_id'] = identity
row['state'] = 'done'
started = []
board.tick([row], 0, lambda *args: started.append(args))
assert board.kb.get_task(board.conn, identity).status == 'ready'
assert not started
assert len(board.snapshot()) == 1
row['state'] = 'scheduled'
row['summary'] = 'Waiting for an existing result.'
board.tick([row], 0, lambda *args: started.append(args))
assert board.kb.get_task(board.conn, identity).status == 'scheduled'
assert not started
""")

    def test_dispatcher_retries_native_failure_without_scheduling_it(self):
        origin = self.task('Deliver a verified outcome')
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Verify the result', body='Check evidence', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], origin['id'])
        with self.service.transaction():
            task = self.service._get('tasks', origin['id'])
            task['status'] = 'completed'
            self.service._put('tasks', task)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        self.settle(lambda: self.provider.started.count(owner['id']) >= 2)
        self.assertEqual(self.service._get('tasks', owner['id'])['_native_outcome_id'], identity)
        self.assertNotEqual(self.service.kanban.tasks[owner['id']]['status'], 'scheduled')

    def test_independent_foreground_conversations_share_host_capacity(self):
        self.provider.supports_native_outcomes = True
        second = self.write('/v1/bots', {'name': 'Second bot', 'role': ''})
        tasks = [self.task('Answer first'), self.write('/v1/bots/' + second['id'] + '/messages', {'text': 'Answer second'})]
        self.provider.release.clear()
        with self.service.lock:
            self.service._dispatch_chat(2)
            self.service._dispatch_chat(2)
            self.assertEqual(set(self.service.running), {task['id'] for task in tasks})
        self.provider.release.set()

    def test_foreground_capacity_and_session_exclusion(self):
        self.provider.supports_native_outcomes = True
        tasks = [self.task('First direct reply'), self.task('Same session waits')]
        for name in ('Second', 'Third', 'Fourth'):
            bot = self.write('/v1/bots', {'name': name})
            tasks.append(self.write('/v1/bots/' + bot['id'] + '/messages', {'text': 'Direct reply'}))
        self.provider.release.clear()
        with self.service.lock:
            self.service._dispatch_chat(2)
            self.service._dispatch_chat(2)
            self.assertEqual(set(self.service.running), {tasks[i]['id'] for i in (0, 2, 3)})
        self.provider.release.set()

    def test_chat_reserves_native_capacity_before_dispatch(self):
        self.provider.supports_native_outcomes = True
        for name in ('First', 'Second', 'Third'):
            bot = self.write('/v1/bots', {'name': name})
            self.write('/v1/bots/' + bot['id'] + '/messages', {'text': 'Direct reply'})
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 2}, str(uuid4()))
        self.provider.release.clear()
        call = self.service.kanban.call
        budgets = []
        def observed(request, *args):
            if request['op'] == 'tick':
                budgets.append(request['capacity'])
            return call(request, *args)
        with patch.object(self.service.kanban, 'call', side_effect=observed):
            self.tick()
        self.assertEqual(budgets, [0])
        self.provider.release.set()

    def test_chat_failure_and_completion_keep_persistent_conversation(self):
        self.provider.supports_native_outcomes = True
        for status in ('failed', 'completed'):
            task = self.service._get('tasks', self.task('Direct conversation')['id'])
            self.assertTrue(task['_chat_session'])
            with self.service.transaction():
                task['status'] = status
                self.service._put('tasks', task)
                reply = self.service._reply(task, {'text': 'Continue the conversation'})
            stored = self.service._get('tasks', reply['id'])
            self.assertTrue(stored['_chat_session'])
            self.assertNotIn('_execution_session_id', stored)

    def test_bot_continues_completed_outcome_in_its_dirty_worktree(self):
        import json
        source = self.task('Continue the accepted outcome with new facts')
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Saved correction', body='Keep publication paused', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], source['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        repo = Path(self.tmp.name) / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '--allow-empty', '-qm', 'Initial'], check=True)
        binding = json.loads(self.native_program("""
import json
from coconut.work_workspace import acquire
print(json.dumps(acquire(sys.argv[2], sys.argv[3], sys.argv[4])))
""", str(self.service.root / 'workspaces'), owner['_execution_session_id'], str(repo)))
        saved = Path(binding['path']) / 'pending.txt'
        saved.write_text('Uncommitted correction')
        row = next(r for r in self.service._kanban_rows() if r['id'] == owner['id'])
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='First accepted scope complete')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        original = self.service._get('tasks', owner['id'])
        self.assertEqual(original['status'], 'completed')
        source = self.service._get('tasks', source['id'])
        source['status'] = 'working'
        self.service._put('tasks', source)
        receipt = self.service.emit(source['id'], 'work_continue', dict(task_id=identity,
            message='Apply the revised local scope. Keep publication paused.'))
        continued = self.service._get('tasks', receipt['task_id'])
        row = self.service._kanban_row(continued, continued, 'ready', 10)
        row['title'] = 'Read-only assessment from the original conversation'
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        continued = self.service._get('tasks', receipt['task_id'])
        native = self.service.kanban.tasks[continued['id']]
        self.assertEqual(continued['id'], original['id'])
        self.assertEqual(continued['_execution_session_id'], original['_execution_session_id'])
        self.assertNotEqual(native['id'], identity)
        self.assertEqual(native['title'], 'Saved correction')
        self.assertEqual(native['status'], 'ready')
        self.assertEqual(native['workspace_path'], binding['path'])
        self.assertEqual(self.native_program('print(board.kb.get_task(board.conn, sys.argv[2]).status)', identity), 'done')
        historical = next(n for n in self.service.kanban.tasks.values() if n['id'] == identity)
        self.assertEqual(historical['status'], 'done')
        self.assertEqual(historical['title'], 'Saved correction')
        self.assertEqual(saved.read_text(), 'Uncommitted correction')
        item = self.service._get('artifacts', owner['_work_context_id'])
        self.assertEqual(item['task_id'], original['id'])
        self.assertEqual(item['_native_outcome_id'], native['id'])

    def test_native_completion_and_cancel_resume_rebind_successors(self):
        origin = self.task('An outcome with a follow-up')
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Follow-up outcome', body='Keep the accepted scope', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], origin['id'])
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        task = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        row = self.service._kanban_row(task, task, 'ready', 10)
        import json
        self.native_program('import json; board.sync(json.loads(sys.argv[2]))', json.dumps([row]))
        self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='Verified original result')", identity)
        with self.service.transaction():
            task.update(status='working')
            self.service._put('tasks', task)
            self.service._reply(task, {'text': 'Also verify the follow-up'})
            task = self.service._get('tasks', task['id'])
            self.service._advance_input(task)
            task['status'] = 'queued'
            self.service._put('tasks', task)
        row = self.service._kanban_row(task, task, 'ready', 10)
        self.native_program('import json; board.sync(json.loads(sys.argv[2]))', json.dumps([row]))
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        rebound = self.service._get('tasks', task['id'])
        successor = rebound['_native_outcome_id']
        self.assertNotEqual(successor, identity)
        item = self.service._get('artifacts', task['_work_context_id'])
        self.assertEqual(item['_native_outcome_id'], successor)
        # Coconut cancellation commits before the native cache observes it.
        with self.service.transaction():
            self.service._cancel_one(rebound, [])
            reply = self.service._send(self.service._get('bots', self.bot['id']), {'text': 'Resume this outcome'}, work_item=item)
        resumed = self.service._get('tasks', reply['id'])
        self.assertNotEqual(resumed['id'], task['id'])
        cancelled = self.service._get('tasks', task['id'])
        rows = [self.service._kanban_row(cancelled, cancelled, 'archived', 10),
                self.service._kanban_row(resumed, resumed, 'ready', 10)]
        self.native_program('import json; board.sync(json.loads(sys.argv[2]))', json.dumps(rows))
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        resumed = self.service._get('tasks', resumed['id'])
        item = self.service._get('artifacts', item['id'])
        self.assertNotEqual(resumed['_native_outcome_id'], successor)
        self.assertEqual(item['_native_outcome_id'], resumed['_native_outcome_id'])
        self.assertEqual(self.service._work_projection([dict(item)])[0]['work_state'], 'queued')
        self.native_program("board.kb.complete_task(board.conn, sys.argv[2], summary='Verified successor')", resumed['_native_outcome_id'])
        self.service.kanban.call({'op': 'snapshot'})
        with self.service.transaction():
            resumed['status'] = 'completed'
            self.service._put('tasks', resumed)
            reply = self.service._reply(resumed, {'text': 'Make one further change'})
        further = self.service._get('tasks', reply['id'])
        self.assertEqual(further['_work_context_id'], item['id'])
        self.assertEqual(further['_native_outcome_id'], resumed['_native_outcome_id'])


    def test_review_reply_resumes_same_claimed_task_once(self):
        claims = self.count_claims()
        task = self.task('Draft a note.md')
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'review')
        native_id = self.service.kanban.tasks[task['id']]['id']
        for _ in range(2):
            self.tick()
        self.assertEqual(self.provider.started, [task['id']])
        card = self.service._all('decisions')[0]
        path = '/v1/decisions/' + card['id'] + '/answer'
        data = {'option_id': 'full', 'revision': card['revision']}
        key = str(uuid4())
        result = self.write(path, data, key)
        self.assertEqual(result, self.write(path, data, key))
        self.settle(lambda: self.service.kanban.tasks[task['id']]['status'] == 'done')
        self.assertEqual(self.service.kanban.tasks[task['id']]['id'], native_id)
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM receipts').fetchone()[0], 1)
        self.assertEqual(claims, [task['id'], task['id']])

    def test_pr_card_answer_resumes_original_review_once(self):
        claims = self.count_claims()
        task = self.task('Draft a note.md')
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'review')
        native_id = self.service.kanban.tasks[task['id']]['id']
        self.native_program("board.kb.add_comment(board.conn, sys.argv[2], 'default', 'https://github.com/example/app/pull/6')", native_id)
        self.tick()
        self.assertEqual(self.provider.started, [task['id']])
        card = self.service._all('decisions')[0]
        self.write('/v1/decisions/' + card['id'] + '/answer',
            {'option_id': 'full', 'revision': card['revision']})
        self.settle(lambda: self.service.kanban.tasks[task['id']]['status'] == 'done')
        self.tick()
        self.assertEqual(self.service.kanban.tasks[task['id']]['id'], native_id)
        self.assertEqual(claims, [task['id'], task['id']])

    def test_native_pr_followup_preserves_capacity_owner_and_saved_work(self):
        self.native_program("""
import os
workspace = board.kb.kanban_db_path().parent / 'saved-work'
workspace.mkdir()
(workspace / 'patch.txt').write_text('Unpublished fix')
rows = [dict(id=name, bot_id='owner', title=name, state=state, input='new', previous_input=previous,
    workspace=str(workspace), priority=10, summary='') for name, state, previous in [
        ('new-task', 'ready', None), ('unchanged', 'ready', 'new'),
        ('human', 'review', 'old'), ('paused', 'scheduled', 'old'),
        ('followup', 'ready', 'old'), ('next-followup', 'ready', 'old')]]
rows[-2]['priority'] = 20
board.sync(rows)
identities = {t['external_id']: t['id'] for t in board.snapshot()}
for identity in identities.values():
    board.kb.add_comment(board.conn, identity, 'default', 'https://github.com/example/app/pull/6')
board.bind(identities['followup'], 'followup')
rows[-2]['native_id'] = identities['followup']
started = []
def spawn(task, path):
    started.append(task.id)
    assert task.tenant == 'owner'
    assert path == str(workspace)
    return os.getpid()
board.tick(rows, 1, spawn)
board.tick(rows, 1, spawn)
assert started == [identities['followup']], started
assert board.kb.get_task(board.conn, identities['human']).status == 'review'
assert board.kb.get_task(board.conn, identities['paused']).status == 'scheduled'
assert (workspace / 'patch.txt').read_text() == 'Unpublished fix'
assert len(board.snapshot()) == len(rows)
""")

    def test_pr_followup_retries_after_guard_expiry_but_keeps_auth_and_human_pause(self):
        self.native_program("""
import os
workspace = board.kb.kanban_db_path().parent / 'saved-work'
workspace.mkdir()
rows = [dict(id=name, bot_id='owner', title=name, state='ready', input='new', previous_input='old',
    workspace=str(workspace), priority=10, summary='') for name in ('handoff', 'auth')]
board.sync(rows)
identities = {t['external_id']: t['id'] for t in board.snapshot()}
for identity in identities.values():
    board.kb.add_comment(board.conn, identity, 'default', 'https://github.com/example/app/pull/6')
with board.kb.write_txn(board.conn):
    board.conn.execute('UPDATE tasks SET last_failure_error=? WHERE id=?', ('invalid API key', identities['auth']))
started = []
def spawn(task, path):
    started.append(task.id)
    return os.getpid()
board.tick(rows, 2, spawn)
assert started == [identities['handoff']], started
assert board.dispatch.check_respawn_guard(board.conn, identities['auth']) == 'blocker_auth'
board.dispatch._record_task_failure(board.conn, identities['handoff'], 'Worker exited',
    outcome='worker_failed', release_claim=True, end_run=True)
assert board.kb.get_task(board.conn, identities['handoff']).status == 'ready'
rows[0]['previous_input'] = rows[0]['input']
with board.kb.write_txn(board.conn):
    board.conn.execute('UPDATE task_comments SET created_at=created_at-172800 WHERE task_id=?', (identities['handoff'],))
board = Board(sys.argv[1])
board.tick(rows, 2, spawn)
assert started == [identities['handoff']] * 2, started
current = board.kb.get_task(board.conn, identities['handoff'])
assert board.kb.request_review(board.conn, current.id, expected_run_id=current.current_run_id)
rows[0]['state'] = 'review'
board.tick(rows, 2, spawn)
assert len(started) == 2
assert board.kb.get_task(board.conn, current.id).status == 'review'
""")

    def test_native_pr_recovery_resumes_same_input_after_restart(self):
        self.native_program("""
import os
workspace = board.kb.kanban_db_path().parent / 'saved-work'
workspace.mkdir()
(workspace / 'patch.txt').write_text('Retained correction')
rows = [dict(id=name, bot_id='owner', title=name, state='ready', input='same', previous_input=None,
    workspace=str(workspace), priority=10, summary='') for name in ('recover', 'unmarked', 'auth')]
board.sync(rows)
identities = {t['external_id']: t['id'] for t in board.snapshot()}
for row in rows:
    identity = identities[row['id']]
    board.bind(identity, row['id'])
    board.kb.claim_task(board.conn, identity)
    board.kb.add_comment(board.conn, identity, 'default', 'https://github.com/example/app/pull/6')
    board.dispatch._record_task_failure(board.conn, identity, 'Worker exited',
        outcome='worker_failed', release_claim=True, end_run=True)
    row.update(native_id=identity, previous_input='same', recovering=row['id'] != 'unmarked')
with board.kb.write_txn(board.conn):
    board.conn.execute('UPDATE tasks SET last_failure_error=? WHERE id=?', ('invalid API key', identities['auth']))
board = Board(sys.argv[1])
started = []
def spawn(task, path):
    started.append(task.id)
    assert path == str(workspace)
    return os.getpid()
board.tick(rows, 3, spawn)
board.tick(rows, 3, spawn)
assert started == [identities['recover']], started
assert board.kb.get_task(board.conn, identities['unmarked']).status == 'ready'
assert board.dispatch.check_respawn_guard(board.conn, identities['auth']) == 'blocker_auth'
assert (workspace / 'patch.txt').read_text() == 'Retained correction'
assert len(board.snapshot()) == 3
""")

    def test_pr_followup_shares_native_memory_pressure_limit_with_normal_work(self):
        self.native_program("""
import os
from unittest.mock import patch
workspace = board.kb.kanban_db_path().parent / 'saved-work'
workspace.mkdir()
rows = [dict(id=name, bot_id='owner', title=name, state='ready', input='new', previous_input=previous,
    workspace=str(workspace), priority=10, summary='') for name, previous in [('followup', 'old'), ('normal', None)]]
board.sync(rows)
identities = {t['external_id']: t['id'] for t in board.snapshot()}
board.kb.add_comment(board.conn, identities['followup'], 'default', 'https://github.com/example/app/pull/6')
started = []
def spawn(task, path):
    started.append(task.id)
    return os.getpid()
with patch.object(board.dispatch, '_memory_pressure_level', return_value='elevated'):
    board.tick(rows, 4, spawn)
assert started == [identities['followup']], started
assert board.kb.get_task(board.conn, identities['normal']).status == 'ready'
""")

    def test_pr_followup_tick_also_reclaims_crashed_native_workers(self):
        self.native_program("""
import os, subprocess
workspace = board.kb.kanban_db_path().parent / 'saved-work'
workspace.mkdir()
rows = [dict(id=name, bot_id='owner', title=name, state='ready', input='new', previous_input='old',
    workspace=str(workspace), priority=10, summary='') for name in ('crashed', 'followup')]
board.sync(rows)
identities = {t['external_id']: t['id'] for t in board.snapshot()}
process = subprocess.Popen(['sleep', '1'])
process.terminate()
process.wait()
board.kb.claim_task(board.conn, identities['crashed'])
board.dispatch._set_worker_pid(board.conn, identities['crashed'], process.pid)
with board.kb.write_txn(board.conn):
    board.conn.execute('UPDATE tasks SET started_at=started_at-? WHERE id=?',
        (board.kb._resolve_crash_grace_seconds() + 1, identities['crashed']))
rows[0]['state'] = 'scheduled'
board.kb.add_comment(board.conn, identities['followup'], 'default', 'https://github.com/example/app/pull/6')
started = []
def spawn(task, path):
    started.append(task.id)
    return os.getpid()
result = board.tick(rows, 3, spawn)
assert identities['crashed'] in result['dispatch']['crashed'], result
assert started == [identities['followup']], started
""")

    def test_native_capacity_and_fifo_then_cancellation(self):
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 2}, str(uuid4()))
        tasks = [self.task('Calculate item ' + str(i)) for i in range(4)]
        self.provider.release.clear()
        self.settle(lambda: len(self.provider.started) == 2)
        self.assertEqual(set(self.provider.started), {t['id'] for t in tasks[:2]})
        with self.service.lock:
            positions = self.service._queue_positions({t['id']: self.service._get('tasks', t['id']) for t in tasks}, time.time())
        self.assertEqual(positions[tasks[2]['id']], 1)
        self.assertEqual(positions[tasks[3]['id']], 2)
        cancelled = self.service._get('tasks', tasks[2]['id'])
        with self.service.transaction():
            cancelled['status'] = 'cancelled'
            self.service._put('tasks', cancelled)
        self.tick()
        self.provider.release.set()
        self.settle(lambda: self.service.kanban.tasks[tasks[3]['id']]['status'] == 'done')
        self.assertNotIn(tasks[2]['id'], self.provider.started)
        self.assertEqual(self.service.kanban.tasks[tasks[2]['id']]['status'], 'archived')

    def test_restart_deferral_reopens_native_dispatch_and_preserves_active_claim(self):
        from coconut.service_restart import RESTART_DRAIN_SECONDS
        self.service.restart_enabled = True
        self.provider.release.clear()
        active = self.task('Check the existing patch')
        self.settle(lambda: active['id'] in self.provider.started)
        original_claim = self.service._get('tasks', active['id'])['_kanban_claim'].copy()
        queued = self.task('Check another patch')
        with patch('coconut.service_restart.time.monotonic', return_value=100) as clock:
            self.service.request_service_restart(active['id'], {})
            self.tick()
            self.assertNotIn(queued['id'], self.provider.started)
            clock.return_value += RESTART_DRAIN_SECONDS
            self.assertFalse(self.service.take_service_restart())
        self.settle(lambda: queued['id'] in self.provider.started)
        self.assertEqual(self.service._get('tasks', active['id'])['_kanban_claim'], original_claim)
        self.assertFalse(self.service.take_service_restart())
        self.provider.release.set()
        self.settle(lambda: all(self.service.kanban.tasks[t['id']]['status'] == 'done' for t in (active, queued)))
        self.assertEqual(self.provider.started.count(active['id']), 1)
        self.assertEqual(self.provider.started.count(queued['id']), 1)
        self.assertTrue(self.service.take_service_restart())

    def test_lost_dispatch_reply_uses_saved_claim(self):
        claims = self.count_claims()
        task = self.task('Calculate one item')
        original = self.service.kanban.call
        lost = []
        def call(request, spawn=None):
            result = original(request, spawn)
            if request['op'] == 'tick' and not lost:
                lost.append(True)
                raise ConnectionError('Lost acknowledgement after native claim commit')
            return result
        self.service.kanban.call = call
        with self.assertRaises(ConnectionError):
            self.tick()
        self.assertFalse(self.provider.started)
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'done')
        self.assertEqual(self.provider.started, [task['id']])
        self.assertEqual(claims, [task['id']])

    def test_lost_finish_reply_does_not_repeat_work(self):
        task = self.task('Calculate one item')
        original = self.service.kanban.call
        lost = []
        def call(request, spawn=None):
            result = original(request, spawn)
            if request['op'] == 'finish' and not lost:
                lost.append(True)
                raise ConnectionError('Lost finish acknowledgement')
            return result
        self.service.kanban.call = call
        with self.assertRaises(ConnectionError):
            for _ in range(4):
                self.tick()
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'done')
        self.assertEqual(self.provider.started, [task['id']])

    def interrupt_before_pid_commit(self, save_claim, cancel=False):
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 1}, str(uuid4()))
        task = self.task('Calculate one item')
        original = self.service.kanban.call
        interrupted = []
        def call(request, spawn=None):
            if request['op'] != 'tick' or interrupted:
                return original(request, spawn)
            def stop(claim):
                interrupted.append(True)
                if save_claim:
                    spawn(claim)
                self.service.kanban.process.kill()
                self.service.kanban.process.wait(timeout=5)
                raise ConnectionError('Adapter stopped before the native PID commit')
            return original(request, stop)
        self.service.kanban.call = call
        with self.assertRaises((OSError, RuntimeError)):
            self.tick()
        self.service.kanban.call = original
        if cancel:
            self.write('/v1/tasks/' + task['id'] + '/cancel', {})
        original(dict(op='snapshot'))
        native = self.service.kanban.tasks[task['id']]
        self.assertEqual(native['status'], 'running')
        self.assertIsNone(native['worker_pid'])
        self.service._reconcile_kanban()
        self.assertFalse(self.provider.started)
        original(dict(op='snapshot'))
        self.assertEqual(self.service.kanban.tasks[task['id']]['status'], 'archived' if cancel else 'ready')
        if cancel:
            task = self.task('Start the next item')
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'done')
        self.assertEqual(self.provider.started, [task['id']])

    def test_interrupted_preparation_before_coconut_claim_recovers(self):
        self.interrupt_before_pid_commit(save_claim=False)

    def test_reused_worker_pid_moves_to_new_native_run(self):
        program = '''
import subprocess, sys, tempfile, time
from coconut.kanban_runtime import Board
with tempfile.TemporaryDirectory() as root:
    board = Board(root)
    task_id = board.kb.create_task(board.conn, title='Lease transfer probe', body='probe',
        assignee='default', created_by='probe', tenant='probe', workspace_kind='dir',
        workspace_path=root, priority=0, initial_status='blocked', idempotency_key='probe')
    board.kb.unblock_task(board.conn, task_id)
    first = board.kb.claim_task(board.conn, task_id)
    process = subprocess.Popen(['sleep', '120'])
    try:
        board.dispatch._set_worker_pid(board.conn, task_id, process.pid)
        board.kb.block_task(board.conn, task_id, reason='Wait for next run', kind='capability')
        board.kb.unblock_task(board.conn, task_id)
        second = board.kb.claim_task(board.conn, task_id)
        original_fingerprint = board.dispatch._process_fingerprint
        try:
            board.dispatch._process_fingerprint = lambda pid: None
            with board.kb.write_txn(board.conn):
                board.record_worker_pid(second, process.pid)
        finally:
            board.dispatch._process_fingerprint = original_fingerprint
        rows = board.conn.execute('SELECT id,worker_pid FROM task_runs WHERE task_id=? ORDER BY id',
                                  (task_id,)).fetchall()
        assert first.current_run_id != second.current_run_id
        assert rows[0]['worker_pid'] is None and rows[1]['worker_pid'] == process.pid
        with board.kb.write_txn(board.conn):
            board.conn.execute('UPDATE task_runs SET ended_at=? WHERE id=?',
                               (int(time.time()) - 600, rows[0]['id']))
        signals = []
        board.dispatch.reap_terminal_workers(board.conn,
            signal_fn=lambda pid, sig: signals.append((pid, sig)))
        assert not signals and process.poll() is None
    finally:
        process.terminate()
        process.wait()
'''
        result = subprocess.run([PYTHON, '-c', program],
            env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[1]), SOURCE))),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_normal_dispatch_keeps_each_active_task_pid(self):
        self.native_program('''
import os, subprocess, time
workspace = board.kb.kanban_db_path().parent / 'reuse'
workspace.mkdir()
rows = [dict(id=name, bot_id='owner', title=name, state='ready', input='one',
    previous_input=None, workspace=str(workspace), priority=10, summary='')
    for name in ('first', 'second')]
rows[1]['state'] = 'scheduled'
board.sync(rows)
ids = {item['external_id']: item['id'] for item in board.snapshot()}
processes = [subprocess.Popen(['sleep', '120']) for _ in rows]
try:
    started = []
    def spawn(task, path):
        assert not board.conn.in_transaction
        started.append(task.id)
        return processes[len(started) - 1].pid
    board.tick(rows, 2, spawn)
    assert started == [ids['first']], started
    rows[0]['state'] = 'scheduled'
    rows[1]['state'] = 'ready'
    board.tick(rows, 2, spawn)
    assert started == [ids['first'], ids['second']], started
    first_run = board.conn.execute('SELECT id,worker_pid FROM task_runs WHERE task_id=? ORDER BY id DESC',
                                   (ids['first'],)).fetchone()
    second_run = board.conn.execute('SELECT worker_pid FROM task_runs WHERE task_id=? ORDER BY id DESC',
                                    (ids['second'],)).fetchone()
    assert first_run['worker_pid'] == processes[0].pid
    assert second_run['worker_pid'] == processes[1].pid
finally:
    for process in processes:
        process.terminate()
        process.wait()
''')

    def test_interrupted_preparation_after_coconut_claim_recovers(self):
        self.interrupt_before_pid_commit(save_claim=True)

    def test_cancel_before_coconut_claim_save_releases_capacity(self):
        self.interrupt_before_pid_commit(save_claim=False, cancel=True)

    def test_cancel_after_coconut_claim_save_releases_capacity(self):
        self.interrupt_before_pid_commit(save_claim=True, cancel=True)

    def test_interrupted_schedule_unblock_replays_unchanged_readiness(self):
        task = self.task('Calculate one item')
        row = self.service._kanban_rows()[0]
        self.service.kanban.call(dict(op='tick', rows=[row], capacity=1),
            lambda claim: self.service.kanban_worker_pid(task['id'], task['id']))
        native = self.service.kanban.tasks[task['id']]
        program = '''
import sys
from coconut.kanban_runtime import Board
board = Board(sys.argv[1])
task = board.kb.get_task(board.conn, sys.argv[2])
def interrupted(*args, **kwargs):
    raise SystemExit(0)
board.kb.unblock_task = interrupted
board.finish(task.id, task.current_run_id, 'ready', 'Interrupted preparation')
raise RuntimeError('The interruption did not run')
'''
        result = subprocess.run([PYTHON, '-c', program, str(self.service.root / 'kanban'), native['id']],
            env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[1]), SOURCE))),
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.service.kanban.call(dict(op='snapshot'))
        self.assertEqual(self.service.kanban.tasks[task['id']]['status'], 'scheduled')
        self.tick()
        self.assertIn(self.service.kanban.tasks[task['id']]['status'], ('running', 'done'))
        self.settle(lambda: self.service.kanban.tasks[task['id']]['status'] == 'done')
        self.assertEqual(self.provider.started, [task['id']])

    def test_restart_preserves_review_without_dispatch(self):
        task = self.task('Draft a note.md')
        self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'review')
        native_id = self.service.kanban.tasks[task['id']]['id']
        self.service.close()
        self.provider = Provider(self.tmp.name)
        self.service = Service(self.tmp.name, self.provider, start_workers=False)
        self.tick()
        self.assertEqual(self.service.kanban.tasks[task['id']]['id'], native_id)
        self.assertEqual(self.service.kanban.tasks[task['id']]['status'], 'review')
        self.assertFalse(self.provider.started)

    def test_inherited_board_paths_cannot_redirect_coconut_dispatch(self):
        foreign = self.service.root / 'unrelated-board'
        foreign.mkdir()
        with patch.dict(os.environ, dict(HERMES_KANBAN_DB=str(foreign / 'kanban.db'),
                HERMES_KANBAN_BOARD='unrelated', HERMES_KANBAN_WORKSPACES_ROOT=str(foreign / 'workspaces'),
                HERMES_KANBAN_ATTACHMENTS_ROOT=str(foreign / 'attachments'))):
            task = self.task('Calculate one item')
            self.settle(lambda: self.service.kanban.tasks.get(task['id'], {}).get('status') == 'done')
        self.assertTrue((self.service.root / 'kanban/kanban.db').is_file())
        self.assertEqual(list(foreign.iterdir()), [])
        self.assertEqual(self.provider.started, [task['id']])

    def test_repeated_dependency_waits_do_not_trigger_native_triage(self):
        task = self.task('Continue after each dependency result')
        row = self.service._kanban_rows()[0]
        for index in range(4):
            row.update(state='scheduled', input=str(index))
            self.service.kanban.call(dict(op='tick', rows=[row], capacity=1))
            native = self.service.kanban.tasks[task['id']]
            self.assertEqual(native['status'], 'scheduled')
            row['state'] = 'ready'
            self.service.kanban.call(dict(op='tick', rows=[row], capacity=1),
                lambda claim: self.service.kanban_worker_pid(task['id'], task['id']))
            native = self.service.kanban.tasks[task['id']]
            self.assertEqual(native['status'], 'running')
            self.assertEqual(native['block_recurrences'], 0)
            self.service.kanban.call(dict(op='finish', id=native['id'], run_id=native['current_run_id'],
                                          state='scheduled', summary='Waiting for a dependency'))
        row['state'] = 'review'
        self.service.kanban.call(dict(op='tick', rows=[row], capacity=1))
        self.assertEqual(self.service.kanban.tasks[task['id']]['status'], 'review')

    def test_new_input_resumes_a_triaged_legacy_turn_after_an_old_sync(self):
        import json
        task = self.task('Check the pull request')
        owner = self.service._get('tasks', task['id'])
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        identity = self.service.kanban.tasks[owner['id']]['id']
        self.native_program("""
board.kb.block_task(board.conn, sys.argv[2], reason='Previous attempt stopped')
board.kb.unblock_task(board.conn, sys.argv[2])
board.kb.block_task(board.conn, sys.argv[2], reason='Previous attempt stopped')
assert board.kb.get_task(board.conn, sys.argv[2]).status == 'triage'
""", identity)
        owner['_kanban_claim'] = {'input': row['input']}
        same = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])]); assert board.kb.get_task(board.conn, sys.argv[3]).status == "triage"',
            json.dumps(same), identity)
        self.service._advance_input(owner)
        self.service._put('tasks', owner)
        fresh = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program("""
import json
from coconut.kanban_runtime import fingerprint
row = json.loads(sys.argv[2])
board.conn.execute('UPDATE coconut_bindings SET intent=? WHERE external_id=?',
    (fingerprint({k: row[k] for k in ('state', 'input')}), row['id']))
board.sync([row])
assert board.kb.get_task(board.conn, sys.argv[3]).status == 'ready'
board.sync([row])
assert board.kb.get_task(board.conn, sys.argv[3]).status == 'ready'
""", json.dumps(fresh), identity)

    def test_source_check_passes_its_claim_to_the_provider(self):
        task = self.task('Reconcile the assigned issue')
        received = []
        def start(value, bot, history, emit):
            import json
            received.append(value)
            result = self.native_program('''
import json, os
claim = json.loads(sys.argv[2])
os.environ.update(HERMES_KANBAN_TASK=claim['native_id'], HERMES_KANBAN_RUN_ID=str(claim['run_id']),
                  HERMES_KANBAN_CLAIM_LOCK=claim['claim_lock'])
from tools.kanban_tools import _handle_complete
print(_handle_complete(dict(task_id=claim['native_id'], summary='Source checked')))
''', json.dumps(value['_kanban_claim']))
            self.assertNotIn('error', json.loads(result), result)
            emit('completed', {'summary': 'Source checked'})
        self.provider.start_task = start
        self.settle(lambda: self.service._get('tasks', task['id'])['status'] == 'completed')
        self.assertEqual(len(received), 1)
        self.assertNotIn('_native_outcome_id', received[0])
        self.assertEqual(received[0]['_kanban_claim'], self.service._get('tasks', task['id'])['_kanban_claim'])

    def test_queued_dependency_remains_available_to_wait(self):
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 1}, str(uuid4()))
        parent = self.task('Inspect the change')
        child = self.task('Check its dependencies')
        with self.service.transaction():
            saved = self.service._get('tasks', child['id'])
            saved['exchange_id'] = parent['exchange_id']
            self.service._put('tasks', saved)
        self.provider.release.clear()
        self.settle(lambda: self.provider.started == [parent['id']])
        self.assertEqual(self.service.kanban.tasks[child['id']]['status'], 'ready')
        result = self.service.wait_for_task(parent['id'], dict(task_id=child['id'], reason='Waiting for dependencies'))
        self.assertEqual(result['status'], 'waiting')

    def test_coconut_wait_rejects_cycle_through_native_dependency(self):
        from coconut.service import ServiceError
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 1}, str(uuid4()))
        parent = self.task('Deliver the prepared result')
        child = self.task('Act on the prepared result')
        with self.service.transaction():
            saved = self.service._get('tasks', child['id'])
            saved['exchange_id'] = parent['exchange_id']
            self.service._put('tasks', saved)
        self.provider.release.clear()
        self.settle(lambda: self.provider.started == [parent['id']])
        parent_native = self.service.kanban.tasks[parent['id']]['id']
        child_native = self.service.kanban.tasks[child['id']]['id']
        self.native_program('board.kb.link_tasks(board.conn, sys.argv[2], sys.argv[3])',
                            parent_native, child_native)
        with self.assertRaisesRegex(ServiceError, 'circular wait'):
            self.service.wait_for_task(parent['id'], dict(task_id=child['id'], reason='Waiting for its action'))
        self.assertFalse(any(r.get('_wait_task_id') for r in self.service._all('native_receipts')))

    def test_resumed_turn_reads_a_completed_dependency_without_another_wait(self):
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 1}, str(uuid4()))
        parent = self.task('Inspect the change')
        child = self.task('Check its dependencies')
        with self.service.transaction():
            saved = self.service._get('tasks', child['id'])
            saved['exchange_id'] = parent['exchange_id']
            self.service._put('tasks', saved)
        replies = []
        def start(value, bot, history, emit):
            if value['id'] == child['id']:
                result = self.native_program('''
import json, os
claim = json.loads(sys.argv[2])
os.environ.update(HERMES_KANBAN_TASK=claim['native_id'], HERMES_KANBAN_RUN_ID=str(claim['run_id']),
                  HERMES_KANBAN_CLAIM_LOCK=claim['claim_lock'])
from tools.kanban_tools import _handle_complete
print(_handle_complete(dict(task_id=claim['native_id'], summary='Dependencies checked')))
''', json.dumps(value['_kanban_claim']))
                self.assertNotIn('error', json.loads(result), result)
                emit('completed', {'summary': 'Dependencies checked'})
                return
            # Each native input of the waiter repeats the same wait.
            replies.append(emit('work_wait', dict(task_id=child['id'], reason='Waiting for dependencies')))
            emit('completed', {'summary': 'Inspection finished' if len(replies) > 1 else 'Waiting for dependencies.'})
        self.provider.start_task = start
        waits = lambda: [r for r in self.service._all('native_receipts') if r.get('_wait_task_id') == child['id']]
        self.settle(lambda: self.service._get('tasks', child['id'])['status'] == 'completed'
                    and self.service._get('tasks', parent['id'])['status'] == 'waiting')
        self.assertEqual([reply['status'] for reply in replies], ['waiting'])
        self.service._drain_task_completions()
        resumed = self.service._get('tasks', parent['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn('Dependencies checked', resumed['_background_completion'][0])
        self.settle(lambda: self.service._get('tasks', parent['id'])['status'] == 'completed')
        self.assertEqual(replies[1], {'status': 'completed', 'task_id': child['id'],
                                      'result': f"Task {child['id']} completed: Dependencies checked"})
        for _ in range(3):
            self.service._drain_task_completions()
            self.tick()
        finished = self.service._get('tasks', parent['id'])
        self.assertEqual((finished['status'], finished['_native_turn_id']), ('completed', resumed['_native_turn_id']))
        self.assertEqual(len(replies), 2)
        self.assertEqual([r['_process_delivery'] for r in waits()], [resumed['_native_turn_id']])

    def test_status_question_runs_when_native_capacity_is_full(self):
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 1}, str(uuid4()))
        owner = self.task('Inspect the change')
        original = self.provider.start_task
        def start(task, bot, history, emit):
            if task.get('_work_status_only'):
                emit('completed', {'summary': 'The worker is still checking the change.'})
            else:
                original(task, bot, history, emit)
        self.provider.start_task = start
        self.provider.release.clear()
        self.settle(lambda: owner['id'] in self.provider.started)
        item = self.service._register_work(owner['id'], dict(key='change', title='Inspect the change',
            activity='Checking the change', status='working'))
        question = self.write('/v1/bots/' + self.bot['id'] + '/messages', dict(text='How is this going?',
            work_artifact_id=item['id'], work_status_only=True, mode='queue'))
        self.settle(lambda: self.service._get('tasks', question['id'])['status'] == 'completed'
                    and owner['id'] in self.provider.started)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'running')
        self.assertNotIn(question['id'], self.service.kanban.tasks)

    def test_native_permission_guard_enters_recovery_instead_of_staying_queued(self):
        task = self.task('Inspect this repository')
        def refused(*_args):
            raise PermissionError('Permission denied: placeholder executable')
        self.service.start_gateway_holder = refused
        self.settle(lambda: self.service._get('tasks', task['id'])['status'] == 'failed')
        saved = self.service._get('tasks', task['id'])
        self.assertEqual(saved['error_code'], 'native_dispatch_failed')
        self.assertFalse(self.provider.started)

    def test_failed_dispatch_records_the_delegation_return(self):
        dev = self.write('/v1/bots', {'name': 'Dev', 'role': ''})
        parent = self.task('Check this change with Dev')
        with self.service.transaction():
            saved = self.service._get('tasks', parent['id'])
            saved['status'] = 'working'
            self.service._put('tasks', saved)
            receipt = self.service._store_bot_message(parent['id'], dict(target_bot_id=dev['id'],
                text='Check this change'))
            saved = self.service._get('tasks', parent['id'])
            saved['status'] = 'waiting'
            self.service._put('tasks', saved)
        def refused(*_args):
            raise PermissionError('Permission denied: placeholder executable')
        self.service.start_gateway_holder = refused
        self.settle(lambda: self.service._get('tasks', receipt['task_id'])['status'] == 'failed')
        child = self.service._get('tasks', receipt['task_id'])
        self.assertEqual(child['_return_pending']['target_bot_id'], self.bot['id'])
        self.assertEqual(child['_return_pending']['result_status'], 'failed')
        self.service._reconcile_kanban()
        self.assertEqual(self.service._get('tasks', child['id'])['_return_pending'], child['_return_pending'])


if __name__ == '__main__':
    unittest.main()
