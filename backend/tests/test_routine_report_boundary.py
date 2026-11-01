"""Diagnostic only: original-owner routine provenance is not wait completion.

Run with PYTHONPATH=backend:backend/tests python3 routing_boundary_test.py -v.
No production changes or live database writes. Counterfactual patches below
measure existing gates; they are deliberately not proposed implementations.
"""
import copy
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import ServiceError
import test_native_work_continuation as fixtures


class RoutingBoundaryTests(unittest.TestCase):
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    resume = fixtures.NativeWorkContinuationTests.resume

    def incident(self):
        owner, original_source = self.scheduled_native()
        self.service.kanban.tasks[owner['id']].update(status='done', result='Retained result',
                                                     workspace_path='/retained/original')
        # Durable create receipt belongs to the original owner, not the occurrence.
        self.service._put('tasks', dict(owner, status='working'))
        created = self.service.emit(owner['id'], 'native_call', dict(name='cronjob_manage',
            arguments=dict(action='create', prompt='A separate capacity checkpoint', schedule='1h')))
        self.service.emit(owner['id'], 'native_result', dict(id=created['id'],
            result=json.dumps(dict(success=True, job_id='checkpoint-job'))))
        self.service._put('tasks', owner)
        checkpoint = dict(owner, id=str(uuid4()), status='working', source_task_id=None,
            source_bot_id=None, parent_task_id=None, _reply_to_task_id=None,
            routine_id='checkpoint-routine', _native_job={'id': 'checkpoint-job'},
            _kanban_claim={'native_id': 't_checkpoint', 'input': 'checkpoint-input'})
        checkpoint['_execution_session_id'] = checkpoint['id']
        checkpoint.pop('_work_todos', None)
        checkpoint.pop('_waiting_on', None)
        self.service._put('tasks', checkpoint)
        # Use real persisted relay deliveries to create the coordinator and helper.
        with self.service.transaction():
            delivered = self.service._store_bot_message(checkpoint['id'],
                {'target_bot_id': original_source['bot_id'], 'text': 'Restore the original owner routing.'})
        coordinator = self.service._get('tasks', delivered['task_id'])
        coordinator.update(status='working', _execution_session_id=coordinator['id'],
            _conversation_epoch=self.service._setting('conversation_epoch'),
            _kanban_claim={'native_id': 't_coordinator', 'input': 'coordinator-input'})
        self.service._put('tasks', coordinator)
        care_bot = self.write('/v1/bots', {'name': 'Care'})
        with self.service.transaction():
            delivered = self.service._store_bot_message(coordinator['id'],
                {'target_bot_id': care_bot['id'], 'text': 'Diagnose the routing rejection only.'})
        care = self.service._get('tasks', delivered['task_id'])
        care.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
        checkpoint.update(status='waiting', _waiting_on=[coordinator['id']])
        coordinator.update(status='waiting', _waiting_on=[care['id']])
        for task in (checkpoint, coordinator, care):
            self.service._put('tasks', task)
        self.service.kanban.tasks[checkpoint['id']] = dict(id='t_checkpoint', status='done')
        self.service.kanban.tasks[coordinator['id']] = dict(id='t_coordinator', status='todo')
        for identity, task, reporter in (('owner-pr', owner, checkpoint),
                                         ('requester-pr', original_source, coordinator)):
            self.service._put('artifacts', dict(id=identity, kind='pr', task_id=task['id'],
                bot_id=task['bot_id'], title='Existing PR', status='open', pr_state='open',
                url='https://github.com/example/project/pull/1', _review_task_id=task['id'],
                _activity_task_id=reporter['id']))
        return owner, original_source, checkpoint, coordinator, care, created['id']

    def reject(self, caller, owner):
        before = self.service._get('tasks', owner['id'])
        snapshots = copy.deepcopy(self.service.kanban.tasks)
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(caller)
        self.assertEqual(self.service._get('tasks', owner['id']), before)
        self.assertEqual(self.service.kanban.tasks, snapshots)

    def test_actual_wait_chain_rejects_without_mutation(self):
        owner, source, checkpoint, coordinator, care, _ = self.incident()
        self.reject(source, owner)
        self.assertEqual(self.service._get('tasks', checkpoint['id'])['_waiting_on'], [coordinator['id']])

    def test_after_care_returns_current_coordinator_still_blocks(self):
        owner, source, checkpoint, coordinator, care, _ = self.incident()
        self.service._put('tasks', dict(care, status='completed'))
        coordinator.update(status='working', _waiting_on=[])
        self.service._put('tasks', coordinator)
        self.service.kanban.tasks[coordinator['id']].update(status='running', current_run_id=17)
        self.reject(coordinator, owner)
        # Even granting only the completed-report classification cannot dismiss
        # a real waiting ancestor, or this unrelated-execution foreground.
        with patch.object(self.service, '_linked_owner_report_coordinator', return_value=self.service._get('tasks', checkpoint['id'])):
            self.reject(coordinator, owner)

    def test_creation_alone_is_not_existing_requester_foreground_proof(self):
        owner, source, checkpoint, coordinator, care, receipt_id = self.incident()
        coordinator.update(status='working', _waiting_on=[], _native_turn_id='coordinator-input')
        coordinator['_kanban_claim'].update(external_id=coordinator['id'], run_id=17)
        self.service.kanban.tasks[coordinator['id']].update(status='running', current_run_id=17)
        self.service._put('tasks', coordinator)
        tasks = {t['id']: t for t in self.service._all('tasks')}
        receipt = self.service._get('native_receipts', receipt_id)
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertTrue(json.loads(receipt['result'])['success'])
        self.assertFalse(self.service._requester_callback_foreground(coordinator, source, tasks,
                                                                    self.service.kanban.tasks))

    def test_terminal_waits_do_not_fix_unconditional_same_bot_gate(self):
        owner, source, checkpoint, coordinator, care, _ = self.incident()
        for task in (checkpoint, coordinator, care):
            self.service._put('tasks', dict(task, status='completed', _waiting_on=[]))
        self.service.kanban.tasks[coordinator['id']].update(status='done')
        self.reject(source, owner)
        # Only in the fully quiet synthetic control does the existing report
        # classification remove the last gate. This is not live completion proof.
        with patch.object(self.service, '_linked_owner_report_coordinator', return_value=self.service._get('tasks', checkpoint['id'])):
            result = self.resume(source)
        self.assertEqual(result['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['_execution_session_id'], owner['_execution_session_id'])

    def test_continuation_returns_actionable_rejection_for_incident(self):
        owner, source, checkpoint, coordinator, care, _ = self.incident()
        self.service._put('tasks', dict(coordinator, status='working'))
        payload = dict(task_id='t_existing', message='Restore the original owner.')
        before = self.service._get('tasks', owner['id'])
        # The turn's coconut_continue_work reaches the service as the work_continue event.
        with self.assertRaisesRegex(ServiceError, 'another execution') as raised:
            self.service.emit(coordinator['id'], 'work_continue', payload)
        self.assertTrue(400 <= raised.exception.status < 500, raised.exception)
        self.assertEqual(self.service._get('tasks', owner['id']), before)
        # A rejected continuation creates no continuation delivery.
        self.assertFalse(any(d.get('source_task_id') == coordinator['id']
                             and d.get('receipt', {}).get('task_id') == owner['id']
                             for d in self.service._all('deliveries')))


if __name__ == '__main__':
    unittest.main()
