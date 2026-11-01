"""A failed routine occurrence continues after Coconut's own settle pass archives its native task.

Coconut archives the waiting native task of every finished request (see _settle_native_orphans). A failed routine
occurrence still continues through the authenticated continuation route. Hermes keeps the archived task and the
continuation gets a successor task in the same execution and workspace. Every other archived task stays final.
"""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from uuid import UUID, uuid4, uuid5

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
import legacy_work_sync
import test_native_kanban as native_fixtures
from hermes_env import needs_hermes

setUpModule, tearDownModule = legacy_work_sync.scope()

JOB = 'job'
OWNER = 'coconut-host:current-owner'
PREVIOUS = 'coconut-host:previous-owner'


def saved_job(retired):
    # The saved shape: a one-shot job whose earlier occurrence succeeded under another owner.
    return dict(id=JOB, name='Capacity recheck', prompt='Recheck the retained capacity.', enabled=True,
                deliver='local', workdir=str(retired), schedule=dict(kind='once', run_at='2026-10-16T16:54:46+00:00'),
                repeat=dict(completed=0, times=1), state='scheduled', last_status='ok',
                fire_claim={'by': OWNER, 'at': '2026-10-16T12:54:52-04:00'},
                _coconut_completion=dict(job_id=JOB, owner=PREVIOUS, success=True),
                _coconut_completion_owner=PREVIOUS)


class ArchivedOccurrence:
    """A real Service with a failed routine occurrence whose native task the settle pass archived."""

    def setUp(self):
        self.store_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.store_tmp.cleanup)
        self.retired = Path(self.store_tmp.name) / 'retired-worktree'
        self.store = Service(self.store_tmp.name, FixtureProvider(self.store_tmp.name), start_workers=False)
        self.addCleanup(self.store.close)
        self.dev = self.store.mutate('POST', '/v1/bots', {'name': 'Dev'}, str(uuid4()), 'test-phone')

    def claim(self, job):
        routine = dict(id=str(uuid5(UUID(self.dev['id']), 'cron:' + job['id'])), bot_id=self.dev['id'],
                       name=job['name'], _native_id=job['id'])
        self.store.provider.native_settings = lambda *a, **kw: dict(job=copy.deepcopy(job))
        with self.store.transaction():
            task = self.store.claim_native_routine(routine)
        return self.store._get('tasks', task['id'])

    def archived(self, job=None):
        owner = self.claim(job or saved_job(self.retired))
        # The saved claim names the request as its input. The turn has its own ID.
        owner.update(status='working', _execution_session_id=owner['id'], _native_turn_id=str(uuid4()),
                     _conversation_epoch=self.store._setting('conversation_epoch'))
        self.store._put('tasks', owner)
        self.store.emit(owner['id'], 'failed', dict(summary='Coconut could not prepare this conversation.',
            error_code='worker_preparation_failed',
            error_detail='ValueError at native_routine_execution.py:101 in job_context'))
        self.store._drain_returns()
        owner = self.store._get('tasks', owner['id'])
        workspace = self.store.root / 'workspaces' / owner['id']
        workspace.mkdir(parents=True)
        owner.update(_native_job_finished=True, _native_finish_unconfirmed=False,
                     _native_job_completion_receipt=dict(job_id=JOB, owner=OWNER, success=False),
                     _kanban_claim=dict(native_id='t_archived', external_id=owner['id'], input=owner['id'],
                                        run_id=41, claim_lock='host:1', workspace=str(workspace)),
                     _kanban_started=41)
        self.store._put('tasks', owner)
        self.card = dict(external_id=owner['id'], id='t_archived', status='archived', tenant=owner['bot_id'],
                         idempotency_key='coconut:' + owner['id'], created_by='coconut', current_run_id=None,
                         worker_pid=None, claim_lock=None, input_id=None, workspace_path=str(workspace),
                         last_activity_at=time.time() - 900, dependencies=[], dispatch_blocker=None)
        self.runs = [dict(id=41, status='blocked', outcome='blocked', ended=True)]
        self.calls = []
        self.store.kanban = Mock(tasks={owner['id']: self.card})
        self.store.kanban.call.side_effect = self.adapter
        source = self.store.mutate('POST', '/v1/bots/' + self.dev['id'] + '/messages',
                                   {'text': 'Check the routine.'}, str(uuid4()), 'test-phone')
        source = self.store._get('tasks', source['id'])
        source['status'] = 'working'
        self.store._put('tasks', source)
        return owner, source

    def adapter(self, request):
        self.calls.append(request)
        if request['op'] == 'snapshot':
            return {'tasks': [dict(row, external_id=identity) for identity, row in self.store.kanban.tasks.items()]}
        if request['op'] == 'completion':
            card = next((dict(c) for c in self.store.kanban.tasks.values() if c['id'] == request['id']), None)
            return {'card': card and dict(id=card['id'], tenant=card['tenant'], idempotency_key=card['idempotency_key'],
                status=card['status'], current_run_id=card['current_run_id'], worker_pid=card['worker_pid'],
                input_id=card['input_id'], runs=copy.deepcopy(self.runs))}
        raise AssertionError(request)

    def resume(self, source, message='The scheduled checkout retired. Recheck in this execution workspace.'):
        return self.store.emit(source['id'], 'work_continue', dict(task_id='t_archived', message=message))

    def successor(self, owner):
        """What Board.sync and the next dispatch do for the new input: a successor card and its claim."""
        continued = self.store._get('tasks', owner['id'])
        workspace = owner['_kanban_claim']['workspace']
        self.store.kanban.tasks[owner['id']] = dict(self.card, id='t_successor', status='running',
            idempotency_key='coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'],
            current_run_id=42, worker_pid=4242, claim_lock='host:2', input_id=continued['_native_turn_id'])
        continued.update(_native_outcome_id='t_successor', status='working', _kanban_started=42,
                         _kanban_claim=dict(native_id='t_successor', external_id=owner['id'], run_id=42,
                                            input=continued['_native_turn_id'], claim_lock='host:2', workspace=workspace))
        self.store._put('tasks', continued)
        return continued

    def settled_successor(self, owner, source):
        """The continuation fails, the settle pass archives its successor, and reconciliation reads that snapshot."""
        self.resume(source)
        continued = self.successor(owner)
        self.store.emit(owner['id'], 'failed', dict(summary='The recheck tool failed.'))
        # Board.sync recorded the accepted input of the successor. The settle pass archived it after the failure.
        self.store.kanban.tasks[owner['id']].update(status='archived', current_run_id=None, worker_pid=None,
                                                    claim_lock=None, last_activity_at=time.time() - 60)
        self.assertEqual(self.store.kanban.tasks[owner['id']]['input_id'], continued['_native_turn_id'])
        self.runs = [dict(id=42, status='blocked', outcome='worker_failed', ended=True)]
        self.store._reconcile_kanban()
        return continued

    def cancellations(self, owner):
        return [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT data FROM events WHERE task_id=? AND type='task.updated' "
            "AND json_extract(data,'$.status')='cancelled'", (owner['id'],))]


class ArchivedFailedRoutineAdmissionTests(ArchivedOccurrence, unittest.TestCase):
    def assertRejected(self, owner, source, pattern='.'):
        before = self.store._get('tasks', owner['id'])
        with self.assertRaisesRegex(ServiceError, pattern):
            self.resume(source)
        self.assertEqual(self.store._get('tasks', owner['id']), before)
        self.assertEqual(self.store.native_routine_completion_input(before), {})

    def test_saved_shape_continues_its_own_failed_occurrence(self):
        owner, source = self.archived()
        before = copy.deepcopy(owner)
        receipt = self.resume(source)
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_archived', status='queued'))
        continued = self.store._get('tasks', owner['id'])
        for key in ('id', 'bot_id', 'session_id', 'thread_id', 'exchange_id', 'routine_id', '_execution_session_id',
                    '_native_job', '_native_job_finished', '_native_job_completion_receipt', '_kanban_claim',
                    'source_bot_id', 'source_task_id'):
            self.assertEqual(continued.get(key), before.get(key), key)
        self.assertEqual(continued['_native_outcome_id'], 't_archived')
        self.assertNotEqual(continued['_native_turn_id'], before['_native_turn_id'])
        self.assertEqual(continued['_self_recovery']['previous_task_id'], owner['id'])
        self.assertEqual(self.resume(source), receipt)
        self.assertEqual(self.store._get('tasks', owner['id']), continued)
        proof = self.store.native_routine_completion_input(continued)
        # Only the failure of this occurrence and owner. The earlier owner's success authorizes nothing.
        self.assertEqual(proof['completion'], dict(job_id=JOB, owner=OWNER, success=False, native_confirmed=True))
        self.assertEqual(proof['continuation']['native_task_id'], 't_archived')
        self.assertEqual(proof['continuation']['turn_id'], continued['_native_turn_id'])
        self.assertIn({'op': 'completion', 'id': 't_archived'}, self.calls)

    def test_successor_of_the_archived_task_keeps_the_continuation(self):
        owner, source = self.archived()
        self.resume(source)
        continued = self.successor(owner)
        proof = self.store.native_routine_completion_input(continued)
        self.assertEqual(proof['completion'], dict(job_id=JOB, owner=OWNER, success=False, native_confirmed=True))
        self.assertEqual(proof['continuation']['task_id'], owner['id'])
        self.assertEqual(proof['continuation']['native_task_id'], 't_successor')
        self.assertEqual(self.store._active_routine_task(owner['routine_id'])['id'], owner['id'])

    def test_successor_waiting_for_dispatch_keeps_the_routine_busy(self):
        owner, source = self.archived()
        self.resume(source)
        # Board.sync made the successor and the reconciler bound it. Hermes has not claimed it yet.
        continued = self.store._get('tasks', owner['id'])
        self.store.kanban.tasks[owner['id']] = dict(self.card, id='t_successor', status='ready',
            idempotency_key='coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        continued['_native_outcome_id'] = 't_successor'
        self.store._put('tasks', continued)
        self.assertEqual(continued['_kanban_claim'], owner['_kanban_claim'])
        proof = self.store.native_routine_completion_input(continued)
        self.assertEqual(proof['continuation']['native_task_id'], 't_successor')
        self.assertEqual(proof['completion'], dict(job_id=JOB, owner=OWNER, success=False, native_confirmed=True))
        self.assertEqual(self.store._active_routine_task(owner['routine_id'])['id'], owner['id'])

    def test_successor_must_be_this_turn_and_tenant(self):
        owner, source = self.archived()
        self.resume(source)
        continued = self.successor(owner)
        card = dict(self.store.kanban.tasks[owner['id']])
        for change in (dict(idempotency_key='coconut-followup:' + owner['id'] + ':another-turn'),
                       dict(idempotency_key='coconut-followup:another-task:' + continued['_native_turn_id']),
                       dict(idempotency_key='coconut:' + owner['id']), dict(tenant='another-bot'),
                       dict(created_by='someone'), dict(id='t_other')):
            with self.subTest(change=change):
                self.store.kanban.tasks[owner['id']] = card | change
                self.assertEqual(self.store.native_routine_completion_input(continued), {})
        self.store.kanban.tasks[owner['id']] = card
        # A claim of this input must name the successor.
        self.assertEqual(self.store.native_routine_completion_input(
            dict(continued, _kanban_claim=dict(continued['_kanban_claim'], native_id='t_other'))), {})
        self.store.kanban.tasks.pop(owner['id'])
        self.assertEqual(self.store.native_routine_completion_input(continued), {})

    def test_later_genuine_failure_stays_failure(self):
        owner, source = self.archived()
        self.resume(source)
        continued = self.successor(owner)
        self.store.emit(owner['id'], 'failed', dict(summary='The recheck tool failed.'))
        failed = self.store._get('tasks', owner['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['_native_job_completion_receipt'], dict(job_id=JOB, owner=OWNER, success=False))
        self.assertEqual(failed['_native_job'], continued['_native_job'])
        self.assertEqual(self.store.native_routine_completion_input(failed), {})

    def test_unrelated_archived_task_stays_final(self):
        owner, source = self.archived()
        for field in ('_native_job', 'routine_id'):
            owner.pop(field)
        self.store._put('tasks', owner)
        self.assertRejected(owner, source, 'not blocked or completed')

    def test_stop_and_cancellation_stay_final(self):
        owner, source = self.archived()
        for changes in (dict(status='cancelled'), dict(status='completed')):
            with self.subTest(changes=changes):
                self.store._put('tasks', dict(owner, **changes))
                self.assertRejected(self.store._get('tasks', owner['id']), source)
        self.store._put('tasks', owner)
        self.store._event('task.updated', dict(owner, status='cancelled'), owner['bot_id'], owner['id'])
        self.assertRejected(owner, source, 'not blocked or completed')

    def test_wrong_requester_is_rejected(self):
        owner, source = self.archived()
        other = self.store.mutate('POST', '/v1/bots', {'name': 'Other'}, str(uuid4()), 'test-phone')
        self.store._put('tasks', dict(source, bot_id=other['id']))
        self.assertRejected(owner, source, 'Only the owner')

    def test_occurrence_must_be_confirmed_by_hermes(self):
        owner, source = self.archived()
        for changes in (dict(_native_job_finished=False), dict(_native_finish_unconfirmed=True),
                        dict(_native_job_completion_receipt=None),
                        dict(_native_job_completion_receipt=dict(job_id=JOB, owner=PREVIOUS, success=False)),
                        dict(_native_job_completion_receipt=dict(job_id=JOB, owner=PREVIOUS, success=True)),
                        dict(_native_job_completion_receipt=dict(job_id=JOB, owner=OWNER, success=True)),
                        dict(_native_job_completion_receipt=dict(job_id='other', owner=OWNER, success=False)),
                        dict(_native_job_completion_receipt=dict(job_id=JOB, owner=OWNER, success=False, extra=1)),
                        dict(routine_id=str(uuid4())),
                        dict(_native_job=dict(owner['_native_job'], id='forged')),
                        dict(_native_job=dict(owner['_native_job'], fire_claim={'by': PREVIOUS}))):
            with self.subTest(changes=changes):
                self.store._put('tasks', dict(owner, **changes))
                self.assertRejected(self.store._get('tasks', owner['id']), source)

    def test_archive_must_be_this_occurrence_settled_by_coconut(self):
        owner, source = self.archived()
        card = dict(self.card)
        for change in (dict(tenant='another-bot'), dict(idempotency_key='coconut:another-task'),
                       dict(created_by='human'), dict(current_run_id=43), dict(worker_pid=4343),
                       # The phone archives only tasks that waited for seven days. This archive is not Coconut's.
                       dict(last_activity_at=time.time() - 8 * 86400), dict(last_activity_at=None)):
            with self.subTest(card=change):
                self.store.kanban.tasks[owner['id']] = card | change
                self.assertRejected(owner, source)
        self.store.kanban.tasks[owner['id']] = card
        runs = copy.deepcopy(self.runs)
        for change in ([dict(id=41, status='running', outcome=None, ended=False)],
                       [dict(id=41, status='reclaimed', outcome='reclaimed', ended=True)],
                       [dict(id=41, status='done', outcome='completed', ended=True)],
                       [dict(id=40, status='blocked', outcome='blocked', ended=True)],
                       runs + [dict(id=44, status='blocked', outcome='blocked', ended=True)], []):
            with self.subTest(runs=change):
                self.runs = change
                self.assertRejected(owner, source)
        self.runs = runs
        self.store._put('tasks', dict(owner, _kanban_claim=dict(owner['_kanban_claim'], external_id='another-task')))
        self.assertRejected(self.store._get('tasks', owner['id']), source)

    def test_restart_reclaimed_run_before_the_claim_run_is_admitted(self):
        owner, source = self.archived()
        self.runs = [dict(id=40, status='reclaimed', outcome='reclaimed', ended=True)] + self.runs
        self.assertEqual(self.resume(source)['task_id'], owner['id'])

    def test_failed_continuation_whose_successor_was_archived_continues_again(self):
        owner, source = self.archived()
        self.resume(source)
        continued = self.successor(owner)
        self.store.emit(owner['id'], 'failed', dict(summary='The recheck tool failed.'))
        failed = self.store._get('tasks', owner['id'])
        # The settle pass archives the successor of the failed continuation too.
        self.store.kanban.tasks[owner['id']].update(status='archived', current_run_id=None, worker_pid=None, claim_lock=None)
        self.runs = [dict(id=42, status='blocked', outcome='blocked', ended=True)]
        receipt = self.store.emit(source['id'], 'work_continue', dict(task_id='t_successor', message='Recheck again.'))
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_successor', status='queued'))
        again = self.store._get('tasks', owner['id'])
        self.assertNotEqual(again['_native_turn_id'], continued['_native_turn_id'])
        proof = self.store.native_routine_completion_input(again)
        self.assertEqual(proof['completion'], dict(job_id=JOB, owner=OWNER, success=False, native_confirmed=True))
        self.assertEqual(proof['continuation']['native_task_id'], 't_successor')
        # A successor key of another input is not this claim's card.
        self.store._put('tasks', failed)
        self.store.kanban.tasks[owner['id']]['idempotency_key'] = 'coconut-followup:' + owner['id'] + ':another-input'
        with self.assertRaisesRegex(ServiceError, 'not blocked or completed'):
            self.store.emit(source['id'], 'work_continue', dict(task_id='t_successor', message='Another recheck.'))
        self.assertEqual(self.store._get('tasks', owner['id']), failed)

    def test_reconciliation_keeps_the_failure_of_a_settled_successor(self):
        owner, source = self.archived()
        continued = self.settled_successor(owner, source)
        failed = self.store._get('tasks', owner['id'])
        self.assertEqual(failed['status'], 'failed', 'Reconciliation cancelled a failure that no person stopped.')
        self.assertEqual(self.cancellations(owner), [])
        self.assertEqual(failed['_native_outcome_id'], 't_successor')
        self.assertEqual(failed['_native_job_completion_receipt'], dict(job_id=JOB, owner=OWNER, success=False))
        # Later passes keep it, also after the seven days in which it can continue.
        card = self.store.kanban.tasks[owner['id']]
        card['last_activity_at'] = time.time() - 8 * 86400
        self.store._reconcile_kanban()
        self.assertEqual(self.store._get('tasks', owner['id']), failed)
        self.assertEqual(self.cancellations(owner), [])
        card['last_activity_at'] = time.time() - 60
        receipt = self.store.emit(source['id'], 'work_continue', dict(task_id='t_successor', message='Recheck again.'))
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_successor', status='queued'))
        again = self.store._get('tasks', owner['id'])
        self.assertNotEqual(again['_native_turn_id'], continued['_native_turn_id'])
        # Board.sync makes a successor for this third input. The reconciler binds it before Hermes claims it.
        self.store.kanban.tasks[owner['id']] = dict(self.card, id='t_successor_2', status='ready', input_id=None,
            idempotency_key='coconut-followup:' + owner['id'] + ':' + again['_native_turn_id'])
        self.store._reconcile_kanban()
        queued = self.store._get('tasks', owner['id'])
        self.assertEqual((queued['status'], queued['_native_outcome_id']), ('queued', 't_successor_2'))
        proof = self.store.native_routine_completion_input(queued)
        self.assertEqual(proof['completion'], dict(job_id=JOB, owner=OWNER, success=False, native_confirmed=True))
        self.assertEqual(proof['continuation']['native_task_id'], 't_successor_2')
        self.assertEqual(self.store._active_routine_task(owner['routine_id'])['id'], owner['id'])
        # Hermes claims it. The worker receives the same proof for this turn.
        queued.update(status='working', _kanban_claim=dict(queued['_kanban_claim'], native_id='t_successor_2',
                                                           run_id=43, input=queued['_native_turn_id']))
        self.store.kanban.tasks[owner['id']].update(status='running', current_run_id=43, worker_pid=4343)
        proof = self.store.native_routine_completion_input(queued)
        self.assertEqual(proof['continuation']['native_task_id'], 't_successor_2')

    def test_reconciliation_still_cancels_other_archived_results(self):
        cases = dict(
            # The phone archived it after a week without activity.
            idle=lambda task, card: card.update(last_activity_at=time.time() - 8 * 86400),
            foreign=lambda task, card: card.update(tenant='another-bot'),
            another_input=lambda task, card: card.update(
                idempotency_key='coconut-followup:' + task['id'] + ':another-input'),
            unconfirmed=lambda task, card: task.update(_native_finish_unconfirmed=True),
            unknown_tool=lambda task, card: self.store._put('native_receipts', dict(id='unknown-' + task['id'],
                task_id=task['id'], turn_id=task['_native_turn_id'], state='executing', action_hash='hash',
                action={'name': 'write_file', 'arguments': {}}, created_at='2026-10-16T17:00:00Z')),
            plain=lambda task, card: [task.pop('_native_job'), task.pop('routine_id')])
        for name, change in cases.items():
            with self.subTest(name):
                ArchivedOccurrence.setUp(self)  # A fresh store: one routine claim per store.
                owner, source = self.archived()
                self.resume(source)
                self.successor(owner)
                self.store.emit(owner['id'], 'failed', dict(summary='The recheck tool failed.'))
                task = self.store._get('tasks', owner['id'])
                card = self.store.kanban.tasks[owner['id']]
                card.update(status='archived', current_run_id=None, worker_pid=None, claim_lock=None,
                            last_activity_at=time.time() - 60)
                change(task, card)
                self.store._put('tasks', task)
                self.store._reconcile_kanban()
                self.assertEqual(self.store._get('tasks', owner['id'])['status'], 'cancelled')
                self.assertEqual(len(self.cancellations(owner)), 1)

    def test_stop_after_the_failure_stays_cancelled(self):
        owner, source = self.archived()
        self.settled_successor(owner, source)
        stopped = dict(self.store._get('tasks', owner['id']), status='cancelled')
        self.store._put('tasks', stopped)
        self.store._event('task.updated', stopped, owner['bot_id'], owner['id'])
        self.store._reconcile_kanban()
        self.assertEqual(self.store._get('tasks', owner['id']), stopped)
        with self.assertRaises(ServiceError):
            self.store.emit(source['id'], 'work_continue', dict(task_id='t_successor', message='Continue anyway.'))
        self.assertEqual(self.store._get('tasks', owner['id']), stopped)

    def test_adapter_without_an_answer_admits_nothing(self):
        owner, source = self.archived()
        def silent(request):
            if request['op'] == 'completion':
                raise RuntimeError('The Hermes Kanban adapter stopped.')
            return self.adapter(request)
        self.store.kanban.call.side_effect = silent
        self.assertRejected(owner, source)
        self.store.kanban.call.side_effect = lambda request: {'card': None} if request['op'] == 'completion' else self.adapter(request)
        self.assertRejected(owner, source)

    def test_idle_archive_by_the_person_stays_final(self):
        owner, source = self.archived()
        self.store._put('decisions', dict(id='archived-question', task_id=owner['id'], bot_id=owner['bot_id'],
                                          status='rejected', decided_by='archive', outcome='Task archived.'))
        self.assertRejected(owner, source)
        self.store.db.execute("DELETE FROM decisions WHERE id='archived-question'")
        self.store._put('decisions', dict(id='archived-proposal', task_id=str(uuid4()), _execution_task_id=owner['id'],
                                          bot_id=owner['bot_id'], status='rejected', decided_by='archive'))
        self.assertRejected(owner, source)

    def test_open_input_decision_wait_and_unknown_effects_hold(self):
        owner, source = self.archived()
        self.store._put('decisions', dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'], status='pending'))
        self.assertRejected(owner, source, 'decision must be answered')
        self.store.db.execute("DELETE FROM decisions WHERE id='pending'")
        for changes in (dict(_steering=['Use the other checkout.']), dict(_blocked_followup=dict(
                message_ids=['m'], source_ids=['m'], attachment_ids=[])), dict(_waiting_on=['dependency']),
                dict(_return_pending=dict(text='Saved result'))):
            with self.subTest(changes=changes):
                self.store._put('tasks', dict(owner, **changes))
                self.assertRejected(self.store._get('tasks', owner['id']), source)
        self.store._put('tasks', owner)
        base = dict(task_id=owner['id'], turn_id=owner['_native_turn_id'], action_hash='hash',
                    created_at='2026-10-16T16:54:55Z')
        # A wait for another task that has no result yet.
        self.store._put('native_receipts', dict(base, id='wait', state='completed', result='{}',
            action={'name': 'coconut_wait', 'arguments': {}}, _wait_task_id='dependency', _wait_session_id='session'))
        self.assertRejected(owner, source)
        self.store.db.execute("DELETE FROM native_receipts WHERE id='wait'")
        self.store._put('native_receipts', dict(base, id='unknown', state='executing',
                                                action={'name': 'write_file', 'arguments': {}}))
        self.assertRejected(owner, source)
        self.store.db.execute("DELETE FROM native_receipts WHERE id='unknown'")
        self.store.running[owner['id']] = Mock(is_alive=Mock(return_value=True))
        self.assertRejected(owner, source)

    def test_other_execution_task_holds(self):
        owner, source = self.archived()
        peer = self.store.mutate('POST', '/v1/bots/' + self.dev['id'] + '/messages',
                                 {'text': 'Peer turn'}, str(uuid4()), 'test-phone')
        peer = self.store._get('tasks', peer['id'])
        peer.update(status='working', _execution_session_id=owner['id'])
        self.store._put('tasks', peer)
        self.assertRejected(owner, source, 'active work')


@needs_hermes
class ArchivedFailedRoutineNativeTests(unittest.TestCase):
    """The installed Hermes kernel: Coconut's settle pass archives the task, continuation dispatches its successor."""

    write = native_fixtures.NativeKanbanTests.write
    native_program = native_fixtures.NativeKanbanTests.native_program
    tick = native_fixtures.NativeKanbanTests.tick

    def setUp(self):
        native_fixtures.NativeKanbanTests.setUp(self)
        self.provider.tasks = []
        self.fail_turns = False
        start = self.provider.start_task
        def record(task, bot, history, emit):
            self.provider.tasks.append(copy.deepcopy(task))
            if self.fail_turns:
                emit('failed', dict(summary='The recheck tool failed.'))
                return
            start(task, bot, history, emit)
        self.provider.start_task = record

    def dispatched(self, count):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(self.provider.tasks) < count:
            self.tick()
        self.assertEqual(len(self.provider.tasks), count, 'The continuation did not dispatch.')
        return self.provider.tasks[-1]

    def native(self, identity):
        program = ('import json\nt = board.kb.get_task(board.conn, sys.argv[2])\n'
                   'links = [r[0] for r in board.conn.execute("SELECT parent_id FROM task_links WHERE child_id=?", (t.id,))]\n'
                   'runs = [list(r) for r in board.conn.execute("SELECT id,status,outcome,ended_at IS NOT NULL FROM task_runs WHERE task_id=? ORDER BY id", (t.id,))]\n'
                   'print(json.dumps(dict(status=t.status, tenant=t.tenant, key=t.idempotency_key, path=t.workspace_path, parents=links, runs=runs)))')
        return json.loads(self.native_program(program, identity))

    def failed_occurrence(self):
        retired = Path(self.tmp.name) / 'retired-worktree'
        job = dict(saved_job(retired))
        routine = dict(id=str(uuid5(UUID(self.bot['id']), 'cron:' + JOB)), bot_id=self.bot['id'],
                       name=job['name'], _native_id=JOB)
        self.service.provider.native_settings = lambda *a, **kw: dict(job=copy.deepcopy(job))
        with self.service.transaction():
            owner = self.service._get('tasks', self.service.claim_native_routine(routine)['id'])
            owner.update(_execution_session_id=owner['id'], _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', owner)
        row = self.service._kanban_row(owner, owner, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        card = self.service.kanban.tasks[owner['id']]
        identity = card['id']
        # The original turn failed in preparation. Coconut finished its run, then the settle pass archived the task.
        run = self.native_program('c = board.kb.claim_task(board.conn, sys.argv[2])\nprint(c.current_run_id)', identity)
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner.update(status='working', _kanban_started=int(run), _kanban_claim=dict(native_id=identity,
                external_id=owner['id'], input=row['input'], run_id=int(run), workspace=card['workspace_path']))
            self.service._put('tasks', owner)
        self.service.emit(owner['id'], 'failed', dict(summary='Coconut could not prepare this conversation.',
                                                      error_code='worker_preparation_failed'))
        self.native_program("assert board.finish(sys.argv[2], int(sys.argv[3]), 'blocked', 'Coconut could not prepare this conversation.')",
                            identity, run)
        self.native_program("assert board.archive_settled(sys.argv[2], None)", identity)
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner.update(_native_job_finished=True, _native_finish_unconfirmed=False,
                         _native_job_completion_receipt=dict(job_id=JOB, owner=OWNER, success=False))
            self.service._put('tasks', owner)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.native(identity)['status'], 'archived')
        Path(card['workspace_path']).mkdir(parents=True, exist_ok=True)
        source = self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Check the routine.'})
        source = self.service._get('tasks', source['id'])
        source['status'] = 'working'
        self.service._put('tasks', source)
        return owner, source, identity, card['workspace_path']

    def test_continuation_dispatches_a_successor_in_the_same_execution(self):
        owner, source, archived, workspace = self.failed_occurrence()
        history = self.native(archived)
        receipt = self.service.emit(source['id'], 'work_continue', dict(task_id=archived,
            message='The scheduled checkout retired. Recheck in this execution workspace.'))
        self.assertEqual(receipt['native_task_id'], archived)
        continued = self.service._get('tasks', owner['id'])
        task = self.dispatched(1)
        successor = task['_native_outcome_id']
        self.assertNotEqual(successor, archived)
        native = self.native(successor)
        self.assertEqual(native['parents'], [archived])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], workspace))
        # The archived task keeps its own history. Hermes runs only the successor.
        self.assertEqual(self.native(archived), history)
        proof = task['_native_job_completion']
        self.assertEqual(proof['completion'], dict(job_id=JOB, owner=OWNER, success=False, native_confirmed=True))
        self.assertEqual(proof['continuation']['native_task_id'], successor)
        self.assertEqual(proof['continuation']['turn_id'], continued['_native_turn_id'])
        self.assertEqual(task['_native_job'], owner['_native_job'])
        self.assertEqual(task['_kanban_workspace'], workspace)

    def test_settled_successor_of_a_failed_continuation_stays_failed_and_continues(self):
        owner, source, archived, workspace = self.failed_occurrence()
        self.fail_turns = True
        self.service.emit(source['id'], 'work_continue', dict(task_id=archived, message='Recheck in this workspace.'))
        first = self.dispatched(1)
        successor = first['_native_outcome_id']
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.service._get('tasks', owner['id'])['status'] != 'failed':
            time.sleep(.05)
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'failed')
        # Coconut finishes the failed run, and the settle pass archives the successor.
        self.native_program("assert board.finish(sys.argv[2], int(sys.argv[3]), 'blocked', 'The recheck tool failed.')",
                            successor, str(first['_kanban_claim']['run_id']))
        self.native_program("assert board.archive_settled(sys.argv[2], None)", successor)
        # A real dispatcher pass reconciles the archived successor of the accepted input before the next continuation.
        self.tick()
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'archived')
        self.assertEqual(self.service.kanban.tasks[owner['id']]['input_id'], first['_native_turn_id'])
        failed = self.service._get('tasks', owner['id'])
        self.assertEqual(failed['status'], 'failed', 'Reconciliation cancelled a failure that no person stopped.')
        self.assertFalse(any(json.loads(row[0]).get('status') == 'cancelled' for row in self.service.db.execute(
            "SELECT data FROM events WHERE task_id=? AND type='task.updated'", (owner['id'],))))
        receipt = self.service.emit(source['id'], 'work_continue', dict(task_id=successor, message='Recheck again.'))
        self.assertEqual(receipt['native_task_id'], successor)
        continued = self.service._get('tasks', owner['id'])
        second = self.dispatched(2)
        latest = second['_native_outcome_id']
        native = self.native(latest)
        self.assertEqual(native['parents'], [successor])
        self.assertEqual(native['key'], 'coconut-followup:' + owner['id'] + ':' + continued['_native_turn_id'])
        self.assertEqual((native['tenant'], native['path']), (owner['bot_id'], workspace))
        proof = second['_native_job_completion']
        self.assertEqual(proof['completion'], dict(job_id=JOB, owner=OWNER, success=False, native_confirmed=True))
        self.assertEqual(proof['continuation']['native_task_id'], latest)
        self.assertEqual(proof['continuation']['turn_id'], continued['_native_turn_id'])
        self.assertEqual(second['_kanban_workspace'], workspace)

    def test_cancelled_occurrence_stays_archived(self):
        owner, source, archived, _ = self.failed_occurrence()
        with self.service.transaction():
            owner = self.service._get('tasks', owner['id'])
            owner['status'] = 'cancelled'
            self.service._put('tasks', owner)
        with self.assertRaises(ServiceError):
            self.service.emit(source['id'], 'work_continue', dict(task_id=archived, message='Continue anyway.'))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)


if __name__ == '__main__':
    unittest.main()
