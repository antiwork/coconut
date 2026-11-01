"""A successful routine's continuation continues after Coconut's settle pass archives its scheduled card.

The saved shape is the one of a real incident. The routine occurrence ended its own card, and Hermes confirmed its
success. The owner then continued the same execution several times. Board.sync made one successor card for the first
continuation, and later continuations reused that card while it waited. The last turn parked the card with
kanban_schedule to finish a read-only check later, so Hermes ended its run as scheduled and the task completed.
A day later the settle pass archived the card, and no continuation could reach the unfinished work.

Only that exact card continues: the successor of an authenticated continuation, holding the current authenticated
input, whose last run is the scheduled run of that input's claim. The original success and every receipt stay.
Native CI sets COCONUT_REQUIRE_NATIVE, and then a missing Hermes install fails the native tests instead of skipping them.
"""
import legacy_routine
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from uuid import UUID, uuid4, uuid5

from coconut.kanban_scheduler import SETTLE_SECONDS
from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
import legacy_work_sync
import test_native_kanban as native_fixtures
import hermes_env

setUpModule, tearDownModule = legacy_work_sync.scope()

JOB = '2a465624c253'
OWNER = 'coconut-host:occurrence-owner'
REASON = 'SCHEDULED_UNTIL=2026-10-16T21:00:00Z. The read-only bank validation is unfinished. Recheck it in this workspace.'
SUCCESS = dict(job_id=JOB, owner=OWNER, success=True)


def saved_job(workdir):
    # The saved shape: an interval job whose occurrence this owner claimed and finished successfully.
    return dict(id=JOB, name='Bank validation', prompt='Validate the bank details read-only.', enabled=True,
                deliver='local', workdir=str(workdir), schedule=dict(kind='interval', minutes=10, display='every 10m'),
                repeat=dict(completed=1, times=None), state='scheduled', last_status='ok',
                fire_claim={'by': OWNER, 'at': '2026-10-16T10:27:26-04:00'})


def stamp(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat().replace('+00:00', 'Z')


class ScheduledOccurrence:
    """A real Service whose successful routine occurrence was continued until the settle pass archived its card."""

    def setUp(self):
        self.store_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.store_tmp.cleanup)
        self.workdir = Path(self.store_tmp.name) / 'bot-workspace'
        self.workdir.mkdir()
        self.store = Service(self.store_tmp.name, FixtureProvider(self.store_tmp.name), start_workers=False)
        self.addCleanup(self.store.close)
        self.dev = self.store.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'test-phone')
        self.runs, self.calls = {}, []
        self.store.kanban = Mock(tasks={})
        self.store.kanban.call.side_effect = self.adapter

    def adapter(self, request):
        """The adapter operations that these passes use, with the semantics of kanban_runtime.Board."""
        self.calls.append(request)
        cards = {card['id']: card for card in self.store.kanban.tasks.values()}
        if request['op'] == 'snapshot':
            return {'tasks': [dict(card, external_id=identity) for identity, card in self.store.kanban.tasks.items()]}
        if request['op'] == 'completion':
            card = cards.get(request['id'])
            return {'card': card and dict(id=card['id'], tenant=card['tenant'], idempotency_key=card['idempotency_key'],
                status=card['status'], current_run_id=card['current_run_id'], worker_pid=card['worker_pid'],
                input_id=card['input_id'], runs=copy.deepcopy(self.runs.get(card['id'], [])))}
        if request['op'] == 'archive_settled':
            card = cards[request['id']]
            if card['status'] not in ('ready', 'todo', 'triage', 'blocked', 'scheduled'):
                return {'ok': False}
            # Board.archive_settled completes only a ready or blocked card. A scheduled card is archived.
            done = card['status'] in ('ready', 'blocked') and request.get('summary')
            card.update(status='done' if done else 'archived', claim_lock=None, current_run_id=None, worker_pid=None)
            return {'ok': True}
        raise AssertionError(request)

    def claim(self, job):
        routine = dict(id=str(uuid5(UUID(self.dev['id']), 'cron:' + job['id'])), bot_id=self.dev['id'],
                       name=job['name'], _native_id=job['id'])
        # An earlier Coconut claimed the run in Hermes (legacy_routine).
        with self.store.transaction():
            task = legacy_routine.claimed(self.store, routine, copy.deepcopy(job))
        return self.store._get('tasks', task['id'])

    def update(self, identity, **fields):
        with self.store.transaction():
            task = self.store._get('tasks', identity)
            task.update(fields)
            self.store._put('tasks', task)
        return task

    def receipt(self, identity, name, arguments, result):
        """Save a tool receipt through the service's own call and result path, as the worker does."""
        call = self.store.emit(identity, 'native_call', dict(name=name, arguments=arguments))
        self.store.emit(identity, 'native_result', dict(id=call['id'], result=json.dumps(result)))
        return call['id']

    def card(self, owner, identity, key, **changes):
        return dict(external_id=owner['id'], id=identity, status='done', tenant=owner['bot_id'], idempotency_key=key,
                    created_by='coconut', current_run_id=None, worker_pid=None, claim_lock=None, input_id=owner['id'],
                    workspace_path=str(self.execution_dir), last_activity_at=time.time() - 120, dependencies=[],
                    dispatch_blocker=None, block_kind=None) | changes

    def occurrence(self, job=None):
        """The original occurrence ends card t_original by run 26393, and Hermes confirms its success."""
        owner = self.claim(job or saved_job(self.workdir))
        self.execution_dir = self.store.root / 'workspaces' / owner['id']
        self.execution_dir.mkdir(parents=True)
        owner = self.update(owner['id'], status='working', _execution_session_id=owner['id'],
                            _native_turn_id='turn-original', _conversation_epoch=self.store._setting('conversation_epoch'),
                            _kanban_claim=dict(native_id='t_original', external_id=owner['id'], input=owner['id'],
                                               run_id=26393, claim_lock='host:1', workspace=str(self.execution_dir)),
                            _kanban_started=26393)
        completion = self.receipt(owner['id'], 'kanban_complete', dict(task_id='t_original', summary='Validated the intake.'),
                                  dict(ok=True, task_id='t_original', run_id=26393))
        self.store.emit(owner['id'], 'completed', dict(summary='Validated the intake.'))
        self.store._drain_returns()
        # The earlier Coconut kept the card and run that ended the occurrence, and the receipt that Hermes wrote when
        # the routine finished.
        owner = self.update(owner['id'], _native_job_finished=True, _native_finish_unconfirmed=False,
                            _native_job_completion_receipt=dict(SUCCESS),
                            _native_occurrence=dict(native_id='t_original', run_id=26393, turn_id='turn-original',
                                                    receipt_id=completion, input=owner['id']))
        self.assertEqual(owner['_native_occurrence']['native_id'], 't_original')
        self.store.kanban.tasks[owner['id']] = self.card(owner, 't_original', 'coconut:' + owner['id'])
        self.runs['t_original'] = [dict(id=26393, status='done', outcome='completed', ended=True)]
        source = self.store.mutate('POST', '/v1/bots/' + self.dev['id'] + '/messages',
                                   {'text': 'Check the routine.'}, str(uuid4()), 'test-phone')
        self.requester = self.update(source['id'], status='working')
        return owner

    def resume(self, identity, message):
        return self.store.emit(self.requester['id'], 'work_continue', dict(task_id=identity, message=message))

    def dispatch(self, owner, identity, run, key=None):
        """Board.sync gives the queued input a card (a new successor when key is given), and Hermes claims it."""
        continued = self.store._get('tasks', owner['id'])
        turn = continued['_native_turn_id']
        card = self.store.kanban.tasks[owner['id']]
        if key:
            card = self.card(continued, identity, key)
        card.update(status='running', current_run_id=run, worker_pid=4000 + run % 1000, claim_lock='host:' + str(run),
                    input_id=turn, last_activity_at=time.time() - 60)
        self.store.kanban.tasks[owner['id']] = card
        self.runs.setdefault(identity, []).append(dict(id=run, status='running', outcome=None, ended=False))
        return self.update(owner['id'], status='working', _native_outcome_id=identity, _kanban_started=run,
                           _kanban_claim=dict(native_id=identity, external_id=owner['id'], run_id=run, input=turn,
                                              claim_lock='host:' + str(run), workspace=str(self.execution_dir)))

    def schedule(self, owner):
        """The worker parks its card with kanban_schedule, and the turn completes."""
        task = self.store._get('tasks', owner['id'])
        identity, run = task['_kanban_claim']['native_id'], task['_kanban_claim']['run_id']
        self.receipt(owner['id'], 'kanban_schedule', dict(task_id=identity, reason=REASON),
                     dict(ok=True, task_id=identity, status='scheduled', reason=REASON))
        self.store.kanban.tasks[owner['id']].update(status='scheduled', current_run_id=None, worker_pid=None,
                                                    claim_lock=None, last_activity_at=time.time() - 60)
        self.runs[identity][-1].update(status='scheduled', outcome='scheduled', ended=True)
        self.store.emit(owner['id'], 'completed', dict(summary='Complete.'))
        return self.store._get('tasks', owner['id'])

    def settle(self, owner):
        """A day later the settle pass archives the scheduled card of the completed task, and reconciliation reads it."""
        self.update(owner['id'], updated_at=stamp(SETTLE_SECONDS + 1))
        card = self.store.kanban.tasks[owner['id']]
        card['last_activity_at'] = time.time() - SETTLE_SECONDS - 1
        self.store._settle_native_orphans()
        self.assertIn({'op': 'archive_settled', 'id': card['id'], 'summary': 'Complete.'}, self.calls)
        # Archiving is not activity. A comment of a later turn can still add some.
        self.assertEqual(card['status'], 'archived', 'The settle pass completed unfinished scheduled work.')
        self.store._reconcile_kanban()
        return self.store._get('tasks', owner['id'])

    def saved(self):
        """The incident: the first continuation made t_scheduled, a later one reused it, and both turns scheduled it."""
        owner = self.occurrence()
        self.resume('t_original', 'Validate the bank details again.')
        first = self.store._get('tasks', owner['id'])['_native_turn_id']
        self.dispatch(owner, 't_scheduled', 26559, key='coconut-followup:' + owner['id'] + ':' + first)
        self.schedule(owner)
        # The scheduled card waits. The next continuation reuses it.
        self.resume('t_scheduled', 'Continue the bank validation.')
        self.dispatch(owner, 't_scheduled', 26630)
        scheduled = self.schedule(owner)
        self.assertEqual(scheduled['status'], 'completed')
        archived = self.settle(owner)
        self.assertEqual(archived, dict(scheduled, updated_at=archived['updated_at']))
        self.assertNotEqual(scheduled['_native_turn_id'], first)
        self.assertEqual(self.store.kanban.tasks[owner['id']]['idempotency_key'],
                         'coconut-followup:' + owner['id'] + ':' + first)
        return archived

    def cancellations(self, owner):
        return [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT data FROM events WHERE task_id=? AND type='task.updated' "
            "AND json_extract(data,'$.status')='cancelled'", (owner['id'],))]


class ArchivedScheduledRoutineAdmissionTests(ScheduledOccurrence, unittest.TestCase):
    def assertRejected(self, owner, pattern='.', identity='t_scheduled'):
        before = self.store._get('tasks', owner['id'])
        with self.assertRaisesRegex(ServiceError, pattern):
            self.resume(identity, 'Finish the bank validation.')
        self.assertEqual(self.store._get('tasks', owner['id']), before)

    def test_saved_shape_continues_its_scheduled_work_in_the_same_execution(self):
        owner = self.saved()
        before = copy.deepcopy(owner)
        self.assertEqual(self.cancellations(owner), [])
        receipt = self.resume('t_scheduled', 'Finish the bank validation.')
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_scheduled', status='queued'))
        self.assertIn({'op': 'completion', 'id': 't_scheduled'}, self.calls)
        continued = self.store._get('tasks', owner['id'])
        for key in ('id', 'bot_id', 'session_id', 'thread_id', 'exchange_id', 'routine_id', '_execution_session_id',
                    '_native_job', '_native_job_finished', '_native_job_completion_receipt', '_native_occurrence',
                    '_kanban_claim', '_native_outcome_id', '_work_context_id'):
            self.assertEqual(continued.get(key), before.get(key), key)
        self.assertEqual(continued['status'], 'queued')
        self.assertNotEqual(continued['_native_turn_id'], before['_native_turn_id'])
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.'), receipt)
        self.assertEqual(self.store._get('tasks', owner['id']), continued)
        # Every saved receipt stays. The original success authorizes the continuation; nothing failed.
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM native_receipts WHERE json_extract(data,'$.task_id')=? "
                                               "AND json_extract(data,'$.state')='completed'", (owner['id'],)).fetchone()[0], 3)
        proof = self.store.native_routine_completion_input(continued)
        self.assertEqual(proof['completion'], dict(SUCCESS, native_confirmed=True))
        self.assertEqual(proof['continuation']['native_task_id'], 't_scheduled')
        self.assertEqual(proof['continuation']['turn_id'], continued['_native_turn_id'])
        self.assertEqual(self.store._active_routine_task(owner['routine_id'])['id'], owner['id'])
        # Reconciliation before dispatch reads the archived card of the earlier input. It neither cancels nor completes.
        settled = [c for c in self.calls if c['op'] == 'archive_settled']
        self.store._reconcile_kanban()
        self.assertEqual(self.store._get('tasks', owner['id']), continued)
        self.assertEqual(self.cancellations(owner), [])
        self.assertEqual([c for c in self.calls if c['op'] == 'archive_settled'], settled)
        self.assertEqual(self.store.kanban.tasks[owner['id']]['status'], 'archived')

    def test_successor_of_the_archived_card_carries_the_continuation(self):
        owner = self.saved()
        self.resume('t_scheduled', 'Finish the bank validation.')
        turn = self.store._get('tasks', owner['id'])['_native_turn_id']
        key = 'coconut-followup:' + owner['id'] + ':' + turn
        # Board.sync makes the successor, and the reconciler binds it before Hermes claims it.
        self.store.kanban.tasks[owner['id']] = self.card(owner, 't_next', key, status='ready', input_id=None)
        self.store._reconcile_kanban()
        queued = self.store._get('tasks', owner['id'])
        self.assertEqual((queued['status'], queued['_native_outcome_id']), ('queued', 't_next'))
        proof = self.store.native_routine_completion_input(queued)
        self.assertEqual(proof['completion'], dict(SUCCESS, native_confirmed=True))
        self.assertEqual(proof['continuation']['native_task_id'], 't_next')
        self.assertEqual(self.store._active_routine_task(owner['routine_id'])['id'], owner['id'])
        working = self.dispatch(owner, 't_next', 26700)
        proof = self.store.native_routine_completion_input(working)
        self.assertEqual(proof['continuation']['native_task_id'], 't_next')
        self.assertEqual(self.cancellations(owner), [])

    def test_repeated_scheduled_continuations_continue_in_order(self):
        owner = self.saved()
        identity = 't_scheduled'
        turns = [self.store._get('tasks', owner['id'])['_native_turn_id']]
        for index, run in enumerate((26700, 26800, 26900)):
            with self.subTest(round=index):
                receipt = self.resume(identity, 'Finish the bank validation, round %d.' % index)
                self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id=identity, status='queued'))
                turn = self.store._get('tasks', owner['id'])['_native_turn_id']
                self.assertNotIn(turn, turns)
                turns.append(turn)
                successor = 't_next_%d' % index
                self.dispatch(owner, successor, run, key='coconut-followup:' + owner['id'] + ':' + turn)
                working = self.store._get('tasks', owner['id'])
                proof = self.store.native_routine_completion_input(working)
                self.assertEqual(proof['completion'], dict(SUCCESS, native_confirmed=True))
                self.assertEqual(proof['continuation']['native_task_id'], successor)
                self.schedule(owner)
                settled = self.settle(owner)
                self.assertEqual(settled['status'], 'completed')
                self.assertEqual(settled['_native_job_completion_receipt'], SUCCESS)
                self.assertEqual(self.cancellations(owner), [])
                # The earlier card stays archived with its own runs.
                self.assertEqual(self.runs[identity][-1]['outcome'], 'scheduled')
                identity = successor
        self.assertEqual(self.store._get('tasks', owner['id'])['_native_occurrence']['native_id'], 't_original')
        self.assertEqual(self.runs['t_original'], [dict(id=26393, status='done', outcome='completed', ended=True)])

    def test_continuation_that_completes_releases_the_routine(self):
        owner = self.saved()
        self.resume('t_scheduled', 'Finish the bank validation.')
        turn = self.store._get('tasks', owner['id'])['_native_turn_id']
        self.dispatch(owner, 't_next', 26700, key='coconut-followup:' + owner['id'] + ':' + turn)
        self.receipt(owner['id'], 'kanban_complete', dict(task_id='t_next', summary='Validated.'),
                     dict(ok=True, task_id='t_next', run_id=26700))
        self.store.emit(owner['id'], 'completed', dict(summary='Validated the bank details.'))
        self.store.kanban.tasks[owner['id']].update(status='done', current_run_id=None, worker_pid=None)
        self.runs['t_next'][-1].update(status='done', outcome='completed', ended=True)
        self.store._reconcile_kanban()
        done = self.store._get('tasks', owner['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertIsNone(self.store._active_routine_task(owner['routine_id']))
        self.assertEqual(done['_native_occurrence']['native_id'], 't_original')
        self.assertEqual(done['_native_job_completion_receipt'], SUCCESS)
        # A done card is no settled scheduled card. Its own continuation path applies.
        self.store.kanban.tasks[owner['id']].update(status='archived')
        self.assertRejected(done, identity='t_next')

    def test_archive_must_be_the_scheduled_run_of_the_current_input(self):
        owner = self.saved()
        runs = copy.deepcopy(self.runs['t_scheduled'])
        last = runs[-1]
        for change in ([dict(last, outcome='review_requested', status='review')], [dict(last, outcome='blocked', status='blocked')],
                       [dict(last, outcome='reclaimed', status='reclaimed')], [dict(last, outcome='completed', status='done')],
                       [dict(last, ended=False, outcome=None)], [dict(last, id=26631)],
                       [dict(runs[0], outcome='completed', status='done'), last], runs[:1], []):
            with self.subTest(runs=change):
                self.runs['t_scheduled'] = change
                self.assertRejected(owner, 'not blocked or completed')
        self.runs['t_scheduled'] = runs
        card = dict(self.store.kanban.tasks[owner['id']])
        for change in (dict(tenant='another-bot'), dict(created_by='human'), dict(current_run_id=26631),
                       dict(worker_pid=4343), dict(input_id='an-earlier-input'), dict(input_id=None),
                       dict(idempotency_key='coconut:' + owner['id']),
                       dict(idempotency_key='coconut-followup:another-task:' + card['idempotency_key'].rsplit(':', 1)[1]),
                       dict(idempotency_key='coconut-followup:' + owner['id'] + ':an-unauthenticated-input'),
                       dict(idempotency_key='coconut-outcome:work'),
                       # A newer card owns the execution now. The archived card is history.
                       dict(id='t_newer', status='ready'),
                       # The phone archives only cards that waited for seven days. This archive is not Coconut's.
                       dict(last_activity_at=time.time() - 8 * 86400), dict(last_activity_at=None)):
            with self.subTest(card=change):
                self.store.kanban.tasks[owner['id']] = card | change
                self.assertRejected(owner)
        self.store.kanban.tasks[owner['id']] = card
        claim = owner['_kanban_claim']
        for changes in (dict(_kanban_claim=dict(claim, run_id=26623)), dict(_kanban_claim=dict(claim, input='an-earlier-input')),
                        dict(_kanban_claim=dict(claim, run_id=str(claim['run_id']))),
                        dict(_kanban_claim=dict(claim, external_id='another-task')), dict(_kanban_started=26623),
                        dict(_native_turn_id='an-unclaimed-turn')):
            with self.subTest(task=changes):
                self.store._put('tasks', dict(owner, **changes))
                self.assertRejected(self.store._get('tasks', owner['id']))
        self.store._put('tasks', owner)
        self.assertEqual(self.resume('t_scheduled', 'Finish the bank validation.')['task_id'], owner['id'])

    def test_completion_read_must_match_the_card(self):
        owner = self.saved()
        adapter = self.adapter
        for change in (dict(id='t_other'), dict(tenant='another-bot'), dict(idempotency_key='coconut:' + owner['id']),
                       dict(status='scheduled'), dict(current_run_id=26631), dict(worker_pid=4343),
                       dict(input_id='an-earlier-input')):
            with self.subTest(change=change):
                self.store.kanban.call.side_effect = lambda request, change=change: (
                    {'card': adapter(request)['card'] | change} if request['op'] == 'completion' else adapter(request))
                self.assertRejected(owner)
        def silent(request):
            if request['op'] == 'completion':
                raise RuntimeError('The Hermes Kanban adapter stopped.')
            return adapter(request)
        self.store.kanban.call.side_effect = silent
        self.assertRejected(owner)
        self.store.kanban.call.side_effect = lambda request: {'card': None} if request['op'] == 'completion' else adapter(request)
        self.assertRejected(owner)

    def test_inputs_must_be_authenticated_continuations_of_this_task(self):
        owner = self.saved()
        rows = [(row[0], json.loads(row[1])) for row in self.store.db.execute(
            "SELECT id,data FROM deliveries WHERE json_extract(data,'$.receipt.task_id')=? ORDER BY rowid", (owner['id'],))]
        self.assertEqual([delivery['turn_id'] for _, delivery in rows][1:], [owner['_native_turn_id']])
        for index, (identity, delivery) in enumerate(rows):
            for change in (dict(target_bot_id='another-bot'), dict(receipt=dict(delivery['receipt'], status='completed')),
                           dict(receipt=dict(delivery['receipt'], extra=1)), dict(source_task_id='a-missing-task'),
                           dict(text='A message that the task never received.')):
                with self.subTest(delivery=index, change=change):
                    self.store.db.execute('UPDATE deliveries SET data=? WHERE id=?', (json.dumps(delivery | change), identity))
                    self.assertRejected(owner)
            self.store.db.execute('UPDATE deliveries SET data=? WHERE id=?', (json.dumps(delivery), identity))
        other = self.store.mutate('POST', '/v1/bots', {'name': 'Other'}, str(uuid4()), 'test-phone')
        self.store._put('tasks', dict(self.requester, bot_id=other['id']))
        self.assertRejected(owner, 'Only the owner')

    def test_original_success_must_be_confirmed_and_kept(self):
        owner = self.saved()
        for changes in (dict(_native_job_finished=False), dict(_native_finish_unconfirmed=True),
                        dict(_native_job_completion_receipt=None),
                        dict(_native_job_completion_receipt=dict(SUCCESS, success=False)),
                        dict(_native_job_completion_receipt=dict(SUCCESS, owner='coconut-host:previous-owner')),
                        dict(_native_job_completion_receipt=dict(SUCCESS, job_id='other')),
                        dict(_native_job_completion_receipt=dict(SUCCESS, extra=1)),
                        dict(routine_id=str(uuid4())), dict(_native_job=dict(owner['_native_job'], id='forged')),
                        dict(_native_job=dict(owner['_native_job'], fire_claim={'by': 'coconut-host:previous-owner'})),
                        dict(_native_occurrence=None), dict(status='failed'), dict(status='cancelled')):
            with self.subTest(changes=changes):
                self.store._put('tasks', dict(owner, **changes))
                self.assertRejected(self.store._get('tasks', owner['id']))
        self.store._put('tasks', owner)
        # The original card itself is no continuation of the occurrence.
        self.store._put('tasks', dict(owner, _native_occurrence=dict(owner['_native_occurrence'], native_id='t_scheduled')))
        self.assertRejected(self.store._get('tasks', owner['id']))

    def test_unrelated_archived_task_stays_final(self):
        owner = self.saved()
        for field in ('_native_job', 'routine_id'):
            owner.pop(field)
        self.store._put('tasks', owner)
        self.assertRejected(owner, 'not blocked or completed')

    def test_manual_stop_stays_final(self):
        owner = self.saved()
        self.store._event('task.updated', dict(owner, status='cancelled'), owner['bot_id'], owner['id'])
        self.assertRejected(owner, 'not blocked or completed')

    def test_idle_archive_by_the_person_stays_final(self):
        owner = self.saved()
        self.store._put('decisions', dict(id='archived-question', task_id=owner['id'], bot_id=owner['bot_id'],
                                          status='rejected', decided_by='archive', outcome='Task archived.'))
        self.assertRejected(owner)

    def test_open_input_decision_waits_and_unknown_effects_hold(self):
        owner = self.saved()
        self.store._put('decisions', dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'], status='pending'))
        self.assertRejected(owner)
        self.store.db.execute("DELETE FROM decisions WHERE id='pending'")
        for changes in (dict(_steering=['Use the other checkout.']), dict(_blocked_followup=dict(
                message_ids=['m'], source_ids=['m'], attachment_ids=[])), dict(_waiting_on=['dependency']),
                dict(_return_pending=dict(text='Saved result')), dict(_routine_pending_bot_result=True),
                dict(_self_recovery=dict(owner['_self_recovery'], unknown_tools=[dict(id='unknown')]))):
            with self.subTest(changes=changes):
                self.store._put('tasks', dict(owner, **changes))
                self.assertRejected(self.store._get('tasks', owner['id']))
        self.store._put('tasks', owner)
        base = dict(task_id=owner['id'], turn_id=owner['_native_turn_id'], action_hash='hash',
                    created_at='2026-10-16T20:16:00Z')
        # A wait for another task that has no result yet.
        self.store._put('native_receipts', dict(base, id='wait', state='completed', result='{}',
            action={'name': 'coconut_wait', 'arguments': {}}, _wait_task_id='dependency', _wait_session_id='session'))
        self.assertRejected(owner)
        self.store.db.execute("DELETE FROM native_receipts WHERE id='wait'")
        self.store._put('native_receipts', dict(base, id='unknown', state='executing',
                                                action={'name': 'write_file', 'arguments': {}}))
        self.assertRejected(owner)
        self.store.db.execute("DELETE FROM native_receipts WHERE id='unknown'")
        self.store.running[owner['id']] = Mock(is_alive=Mock(return_value=True))
        self.assertRejected(owner)
        self.store.running.pop(owner['id'])
        peer = self.store.mutate('POST', '/v1/bots/' + self.dev['id'] + '/messages', {'text': 'Peer turn'},
                                 str(uuid4()), 'test-phone')
        self.update(peer['id'], status='working', _execution_session_id=owner['id'])
        self.assertRejected(owner, 'active work')

    def test_failed_routine_continuation_keeps_its_own_rule(self):
        owner = self.saved()
        # A failed occurrence with the same scheduled card is no settled failure: its last run did not fail.
        self.store._put('tasks', dict(owner, status='failed', _native_job_completion_receipt=dict(SUCCESS, success=False)))
        self.assertRejected(self.store._get('tasks', owner['id']), 'not blocked or completed')
        self.assertFalse(self.store.keeps_settled_failure(self.store._get('tasks', owner['id']),
                                                          self.store.kanban.tasks[owner['id']]))


class ArchivedScheduledRoutineNativeTests(unittest.TestCase):
    """The installed Hermes kernel: the settle pass archives the scheduled card, continuation dispatches its successor."""

    write = native_fixtures.NativeKanbanTests.write
    native_program = native_fixtures.NativeKanbanTests.native_program
    tick = native_fixtures.NativeKanbanTests.tick

    def setUp(self):
        hermes_env.require(self)
        native_fixtures.NativeKanbanTests.setUp(self)
        self.provider.tasks = []
        start = self.provider.start_task
        def record(task, bot, history, emit):
            self.provider.tasks.append(copy.deepcopy(task))
            claim = task['_kanban_claim']
            # The worker parks its card with kanban_schedule, then its turn completes.
            self.native_program("assert board.kb.schedule_task(board.conn, sys.argv[2], reason=sys.argv[4], "
                                "expected_run_id=int(sys.argv[3]))", claim['native_id'], str(claim['run_id']), REASON)
            start(task, bot, history, emit)
        self.provider.start_task = record

    def dispatched(self, count):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(self.provider.tasks) < count:
            self.tick()
        self.assertEqual(len(self.provider.tasks), count, 'The continuation did not dispatch.')
        return self.provider.tasks[-1]

    def completed(self, owner):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.service._get('tasks', owner['id'])['status'] != 'completed':
            time.sleep(.05)
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        # The turn's thread ends after it reports. Until then the task counts as running, for continuation and settle alike.
        worker = self.service.running.get(owner['id'])
        if worker:
            worker.join(20)
            self.assertFalse(worker.is_alive(), 'The completed turn did not end.')

    def native(self, identity):
        program = ('import json\nt = board.kb.get_task(board.conn, sys.argv[2])\n'
                   'links = [r[0] for r in board.conn.execute("SELECT parent_id FROM task_links WHERE child_id=?", (t.id,))]\n'
                   'runs = [list(r) for r in board.conn.execute("SELECT id,status,outcome,ended_at IS NOT NULL FROM task_runs WHERE task_id=? ORDER BY id", (t.id,))]\n'
                   'print(json.dumps(dict(status=t.status, tenant=t.tenant, key=t.idempotency_key, path=t.workspace_path, parents=links, runs=runs)))')
        return json.loads(self.native_program(program, identity))

    def successful_occurrence(self):
        job = saved_job(Path(self.tmp.name))
        routine = dict(id=str(uuid5(UUID(self.bot['id']), 'cron:' + JOB)), bot_id=self.bot['id'],
                       name=job['name'], _native_id=JOB)
        # An earlier Coconut claimed the run in Hermes (legacy_routine).
        with self.service.transaction():
            owner = self.service._get('tasks', legacy_routine.claimed(self.service, routine, copy.deepcopy(job))['id'])
            owner.update(_execution_session_id=owner['id'], _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', owner)
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        card = self.service.kanban.tasks[owner['id']]
        identity = card['id']
        # The original turn ends its own card with kanban_complete, and Hermes confirms the routine's success.
        run = self.native_program('c = board.kb.claim_task(board.conn, sys.argv[2])\nprint(c.current_run_id)', identity)
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner.update(status='working', _native_turn_id='turn-original', _kanban_started=int(run), _kanban_claim=dict(
                native_id=identity, external_id=owner['id'], input=row['input'], run_id=int(run), workspace=card['workspace_path']))
            self.service._put('tasks', owner)
        self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Validated the intake.', "
                            "expected_run_id=int(sys.argv[3]))", identity, run)
        call = self.service.emit(owner['id'], 'native_call', dict(name='kanban_complete', arguments=dict(task_id=identity)))
        self.service.emit(owner['id'], 'native_result', dict(id=call['id'], result=json.dumps(
            dict(ok=True, task_id=identity, run_id=int(run)))))
        self.service.emit(owner['id'], 'completed', dict(summary='Validated the intake.'))
        # The earlier Coconut kept the card and run that ended the occurrence, and the receipt that Hermes wrote when
        # the routine finished.
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner.update(_native_job_finished=True, _native_finish_unconfirmed=False, _native_job_completion_receipt=dict(SUCCESS),
                         _native_occurrence=dict(native_id=identity, run_id=int(run), turn_id='turn-original',
                                                 receipt_id=call['id'], input=row['input']))
            self.service._put('tasks', owner)
        self.assertEqual(owner['_native_occurrence']['native_id'], identity)
        # The next dispatcher pass reads the committed card before any continuation, as the scheduler does.
        # The cached snapshot above still shows the card ready, from before the claim.
        self.service.kanban.call({'op': 'snapshot'})
        self.service._reconcile_kanban()
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'done')
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        Path(card['workspace_path']).mkdir(parents=True, exist_ok=True)
        source = self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Check the routine.'})
        source = self.service._get('tasks', source['id'])
        source['status'] = 'working'
        self.service._put('tasks', source)
        return owner, source, identity, card['workspace_path']

    def scheduled(self):
        """Two continuations schedule one successor card. The settle pass archives it a day after the task completed."""
        owner, source, original, workspace = self.successful_occurrence()
        self.service.emit(source['id'], 'work_continue', dict(task_id=original, message='Validate the bank details again.'))
        first = self.dispatched(1)
        scheduled = first['_native_outcome_id']
        self.completed(owner)
        self.tick()
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'scheduled')
        self.service.emit(source['id'], 'work_continue', dict(task_id=scheduled, message='Continue the bank validation.'))
        second = self.dispatched(2)
        self.assertEqual(second['_native_outcome_id'], scheduled)
        self.completed(owner)
        self.tick()
        with self.service.transaction():
            task = self.service._get('tasks', owner['id'])
            task['updated_at'] = stamp(SETTLE_SECONDS + 1)
            self.service._put('tasks', task)
        self.service._settle_native_orphans()
        self.service.kanban.call({'op': 'snapshot'})
        self.tick()
        native = self.native(scheduled)
        self.assertEqual(native['status'], 'archived')
        self.assertEqual([run[2] for run in native['runs']], ['scheduled', 'scheduled'])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + first['_native_turn_id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')
        return owner, source, original, scheduled, workspace

    def test_continuation_dispatches_a_successor_of_the_scheduled_card(self):
        owner, source, original, scheduled, workspace = self.scheduled()
        history = self.native(scheduled), self.native(original)
        receipt = self.service.emit(source['id'], 'work_continue', dict(task_id=scheduled, message='Finish the bank validation.'))
        self.assertEqual(receipt['native_task_id'], scheduled)
        continued = self.service._get('tasks', owner['id'])
        task = self.dispatched(3)
        successor = task['_native_outcome_id']
        self.assertNotIn(successor, (original, scheduled))
        native = self.native(successor)
        self.assertEqual(native['parents'], [scheduled])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], workspace))
        # The archived card and the original card keep their history. Hermes runs only the successor.
        self.assertEqual((self.native(scheduled), self.native(original)), history)
        # The continuation guard of the routine still proves the saved occurrence and its continuation.
        with self.service.lock:
            proof = self.service.native_routine_completion_input(self.service._get('tasks', owner['id']))
        self.assertEqual(proof['completion'], dict(SUCCESS, native_confirmed=True))
        self.assertEqual(proof['continuation']['native_task_id'], successor)
        self.assertEqual(proof['continuation']['turn_id'], continued['_native_turn_id'])
        self.assertEqual(task['_native_job'], owner['_native_job'])
        self.assertEqual(task['_kanban_workspace'], workspace)
        self.assertFalse(any(json.loads(row[0]).get('status') == 'cancelled' for row in self.service.db.execute(
            "SELECT data FROM events WHERE task_id=? AND type='task.updated'", (owner['id'],))))

    def test_cancelled_occurrence_stays_archived(self):
        owner, source, _, scheduled, _ = self.scheduled()
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner['status'] = 'cancelled'
            self.service._put('tasks', owner)
        with self.assertRaises(ServiceError):
            self.service.emit(source['id'], 'work_continue', dict(task_id=scheduled, message='Continue anyway.'))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)


if __name__ == '__main__':
    unittest.main()
