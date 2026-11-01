"""Failed/blocked peer history remains intact after a proved recovery.

The retained shape is the bbdbc176 -> bfd19838 recovery: a Work-root Dev
owner without _work_context_id, Desk's linked PR-only report, legacy native
claim on a failed predecessor, and an unknown search_files copied to recovery.
The live recovery was still working. Only the disposable fixture completes it.
"""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from coconut.service import ServiceError
import test_native_work_continuation as fixtures


class HistoricalPeerContinuationTests(unittest.TestCase):
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    linked_report_message = fixtures.NativeWorkContinuationTests.linked_report_message
    linked_owner_report = fixtures.NativeWorkContinuationTests.linked_owner_report
    resume = fixtures.NativeWorkContinuationTests.resume

    def saved_shape(self, name='search_files'):
        owner, source, work, pr, peer, reporter, coordinator, callback = self.linked_owner_report()
        # The incident's current Dev report replaced the historical same-bot report.
        pr['_activity_task_id'] = owner['id']
        pr.update(status='closed', pr_state='closed')
        peer.update(status='closed', pr_state='closed')
        self.service._put('artifacts', pr)
        self.service._put('artifacts', peer)
        care = self.write('/v1/bots', {'name': 'Care'})
        coordinator['status'] = 'working'
        self.service._put('tasks', coordinator)
        sent = self.linked_report_message(coordinator, care['id'], 'Diagnose the retained owner')
        previous = self.service._get('tasks', sent['task_id'])
        previous.update(status='working', _execution_session_id=previous['id'],
                        _conversation_epoch=self.service._setting('conversation_epoch'),
                        _native_turn_id='failed-turn',
                        _kanban_claim=dict(native_id='t_failed', external_id=previous['id'],
                                           input=previous['id'], run_id=17, pid=1234))
        self.service._put('tasks', previous)
        intent = self.service.emit(previous['id'], 'native_call', dict(name=name,
            arguments={'pattern': 'coconut.sqlite3', 'target': 'files', 'path': '/fixture/runtime',
                       'limit': 10, 'offset': 15} if name == 'search_files' else {'path': '/fixture/evidence'}))
        unknown = self.service._get('native_receipts', intent['id'])
        self.service.emit(previous['id'], 'failed', {'summary': 'A tool stopped without a receipt.'})
        self.service._drain_returns()
        previous = self.service._get('tasks', previous['id'])
        self.assertEqual(previous['status'], 'failed')
        self.service.kanban.tasks[previous['id']] = dict(id='t_failed', status='blocked',
            current_run_id=None, worker_pid=None, block_kind=None)
        coordinator = self.service._get('tasks', coordinator['id'])
        coordinator['status'] = 'working'
        self.service._put('tasks', coordinator)
        sent = self.linked_report_message(coordinator, care['id'], 'Continue the same recovery',
                                          continuation_task_id=previous['id'])
        self.assertEqual(sent['status'], 'queued')
        successor = self.service._get('tasks', sent['task_id'])
        successor.update(status='working', _native_outcome_id='t_recovered', _native_turn_id='recovered-turn',
            _kanban_claim=dict(native_id='t_recovered', external_id=successor['id'], input='recovered-turn',
                               run_id=23, pid=5678))
        # A later native input can replace this recovery pointer with itself;
        # the retained parent/_continued_task_id pair remains the provenance.
        successor['_self_recovery'].update(previous_task_id=successor['id'], unknown_tools=[copy.deepcopy(unknown)])
        self.service._put('tasks', successor)
        completion = self.service.emit(successor['id'], 'native_call', dict(name='kanban_complete',
            arguments={'summary': 'Recovery verified'}))
        self.service.emit(successor['id'], 'native_result', dict(id=completion['id'],
            result=json.dumps(dict(ok=True, task_id='t_recovered', run_id=23, attachments=[]))))
        self.service.kanban.tasks[successor['id']] = dict(id='t_recovered', status='done',
            input_id='recovered-turn', worker_pid=None, current_run_id=None)
        self.service.emit(successor['id'], 'completed', {'summary': 'Recovery verified'})
        self.service._drain_returns()
        # Actually consume the queued callback inputs in the fake provider.
        for task in self.service._all('tasks'):
            if task.get('_is_return') and task['status'] == 'queued':
                self.service._run_task(task['id'])
        coordinator = self.service._get('tasks', coordinator['id'])
        coordinator.update(status='completed', _waiting_on=[])
        self.service._put('tasks', coordinator)
        previous = self.service._get('tasks', previous['id'])
        successor = self.service._get('tasks', successor['id'])
        self.assertEqual(successor['status'], 'completed')
        self.assertEqual(previous['_continued_task_id'], successor['id'])
        self.assertEqual(successor['parent_task_id'], previous['id'])
        self.assertNotIn('_work_context_id', owner)
        self.assertNotIn('_native_outcome_id', previous)
        self.assertEqual(self.service._get('native_receipts', unknown['id']), unknown)
        return owner, source, previous, successor, unknown, completion, coordinator

    def assert_blocked(self, source):
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(source)

    def test_saved_failed_blocked_peer_and_unknown_read_are_preserved(self):
        owner, source, previous, successor, unknown, _, _ = self.saved_shape()
        before = {name: copy.deepcopy(self.service._all(name)) for name in ('native_receipts', 'deliveries')}
        cards = copy.deepcopy(self.service.kanban.tasks)
        result = self.resume(source)
        self.assertEqual(result['task_id'], owner['id'])
        self.assertEqual(result['native_task_id'], 't_existing')
        self.assertEqual(self.service._get('tasks', owner['id'])['_execution_session_id'], owner['_execution_session_id'])
        self.assertEqual(self.service._get('tasks', previous['id']), previous)
        self.assertEqual(self.service._get('tasks', successor['id']), successor)
        self.assertEqual(self.service._get('native_receipts', unknown['id']), unknown)
        self.assertEqual(self.service.kanban.tasks, cards)
        for name, rows in before.items():
            for row in rows:
                self.assertEqual(self.service._get(name, row['id']), row)
        self.assertEqual(self.service._all('native_receipts'), before['native_receipts'])
        new_deliveries = [d for d in self.service._all('deliveries')
                          if d['id'] not in {row['id'] for row in before['deliveries']}]
        self.assertEqual(len(new_deliveries), 1)
        self.assertEqual(new_deliveries[0]['receipt']['task_id'], owner['id'])

    def test_read_file_is_also_a_read_but_not_a_completed_result(self):
        _, source, _, _, unknown, _, _ = self.saved_shape('read_file')
        self.resume(source)
        self.assertEqual(self.service._get('native_receipts', unknown['id']), unknown)
        self.assertNotIn('result', unknown)

    def test_unknown_terminal_write_blocks_even_with_ended_process_marker(self):
        _, source, _, successor, unknown, _, _ = self.saved_shape()
        unknown['action'] = dict(name='terminal', arguments={'command': 'git push'})
        for ended in (None, '2026-09-28T14:00:00Z'):
            with self.subTest(ended=ended):
                unknown['_process_ended_at'] = ended
                self.service._put('native_receipts', unknown)
                successor['_self_recovery']['unknown_tools'] = [copy.deepcopy(unknown)]
                self.service._put('tasks', successor)
                self.assert_blocked(source)

    def test_wrong_successor_provenance_and_unconsumed_inputs_block(self):
        _, source, previous, successor, _, _, _ = self.saved_shape()
        changes = [dict(parent_task_id='unrelated'), dict(_execution_session_id='unrelated'),
                   dict(_conversation_epoch='old'), dict(thread_id='unrelated'), dict(bot_id=source['bot_id']),
                   dict(_continued_task_id='newer'), dict(status='working'), dict(status='queued'),
                   dict(status='failed'), dict(status='cancelled'), dict(_is_return=True),
                   dict(_native_relay={'envelope_id': 'wrong', 'sender_bot_id': source['bot_id']}),
                   dict(_native_return_prepared=['old-native', 'old-turn']),
                   dict(_native_turn_id='next-input'), dict(_self_recovery={})]
        for changeset in changes:
            with self.subTest(changes=changeset):
                self.service._put('tasks', dict(successor, **changeset))
                self.assert_blocked(source)
        self.service._put('tasks', successor)
        self.service._put('tasks', dict(previous, _continued_task_id=None))
        self.assert_blocked(source)

    def test_reciprocal_links_cannot_replace_exact_delivery_provenance(self):
        _, source, _, successor, _, _, _ = self.saved_shape()
        delivery, = [d for d in self.service._all('deliveries')
                     if d['receipt'].get('task_id') == successor['id']]
        cases = [dict(_progress_only=True), dict(source_task_id='other'), dict(target_bot_id='other')]
        cases += [dict(receipt=dict(delivery['receipt'], **{field: 'other'}))
                  for field in ('task_id', 'target_bot_id', 'thread_id', 'status')]
        for change in cases:
            with self.subTest(change=change):
                self.service._put('deliveries', dict(delivery, **change))
                self.assert_blocked(source)
        self.service._put('deliveries', delivery)
        relay = dict(successor['_native_relay'], envelope_id='0' * 32)
        self.service._put('tasks', dict(successor, _native_relay=relay))
        self.assert_blocked(source)

    def test_failed_or_closed_alone_never_qualifies_a_peer(self):
        _, source, previous, successor, unknown, _, _ = self.saved_shape()
        self.service._put('tasks', dict(previous, _continued_task_id=None))
        self.assert_blocked(source)
        self.service._put('native_receipts', dict(unknown, state='completed', result='Read recovered'))
        self.assert_blocked(source)
        self.service._put('tasks', dict(previous, status='cancelled'))
        self.assert_blocked(source)

    def test_completion_requires_current_successful_native_receipt(self):
        _, source, _, _, _, completion, _ = self.saved_shape()
        saved = self.service._get('native_receipts', completion['id'])
        for change in (dict(state='executing'), dict(turn_id='old-turn'), dict(result='not json'),
                       dict(result=json.dumps(dict(ok=False, task_id='t_recovered', run_id=23))),
                       dict(result=json.dumps(dict(ok=True, task_id='other', run_id=23))),
                       dict(result=json.dumps(dict(ok=True, task_id='t_recovered', run_id=22)))):
            with self.subTest(change=change):
                self.service._put('native_receipts', dict(saved, **change))
                self.assert_blocked(source)

    def test_active_native_predecessor_or_successor_and_missing_snapshot_block(self):
        _, source, previous, successor, _, _, _ = self.saved_shape()
        for task in (previous, successor):
            card = self.service.kanban.tasks[task['id']]
            for change in (dict(current_run_id=99), dict(worker_pid=123), dict(status='ready'),
                           dict(status='scheduled'), dict(status='review'), dict(id='other'),
                           dict(status='blocked', block_kind='dependency')):
                with self.subTest(task=task['id'], change=change):
                    self.service.kanban.tasks[task['id']] = dict(card, **change)
                    self.assert_blocked(source)
            self.service.kanban.tasks.pop(task['id'])
            self.assert_blocked(source)
            self.service.kanban.tasks[task['id']] = card
        for change in (dict(input_id='old-input'), dict(status='archived')):
            self.service.kanban.tasks[successor['id']] = dict(card, **change)
            self.assert_blocked(source)

    def test_wait_decision_context_and_live_worker_remain_guarded(self):
        _, source, previous, successor, _, _, coordinator = self.saved_shape()
        for task in (previous, successor, coordinator):
            for field in ('_waiting_on', '_return_pending', '_steering',
                          '_live_messages', '_blocked_followup'):
                with self.subTest(task=task['id'], field=field):
                    self.service._put('tasks', dict(task, **{field: ['pending']}))
                    self.assert_blocked(source)
            self.service._put('tasks', task)
            self.service.running[task['id']] = Mock(is_alive=Mock(return_value=True))
            self.assert_blocked(source)
            self.service.running.clear()
            with patch.object(self.service, '_background_pending', side_effect=lambda identity: identity == task['id']):
                self.assert_blocked(source)
            self.service._put('decisions', dict(id='pending', task_id=task['id'], status='pending'))
            self.assert_blocked(source)
            self.service._put('decisions', dict(id='pending', task_id=task['id'], status='executed'))
            self.service._put('contexts', dict(id='context', task_id=task['id'], status='queued'))
            self.assert_blocked(source)
            self.service._put('contexts', dict(id='context', task_id=task['id'], status='completed'))

    def test_active_sibling_and_undelivered_callback_are_not_exempted(self):
        _, source, previous, successor, _, _, _ = self.saved_shape()
        for status in ('working', 'queued', 'failed'):
            sibling = dict(successor, id='sibling', _assignment_task_id=successor['id'], status=status,
                           _kanban_claim={}, _native_outcome_id=None)
            self.service._put('tasks', sibling)
            self.assert_blocked(source)
        sibling.update(status='completed', _return_pending={'text': 'undelivered'})
        self.service._put('tasks', sibling)
        self.assert_blocked(source)
        sibling.pop('_return_pending')
        sibling.update(status='queued', _is_return=True, source_task_id=previous['id'])
        self.service._put('tasks', sibling)
        self.assert_blocked(source)

    def test_unlinked_same_execution_sibling_cannot_hide_behind_failed_root(self):
        _, source, previous, successor, _, _, _ = self.saved_shape()
        sibling = dict(successor, id='unlinked-sibling', status='working', _kanban_claim={},
                       _native_outcome_id=None, parent_task_id=previous['id'])
        sibling.pop('_assignment_task_id', None)
        sibling.pop('_native_relay', None)
        # _work_execution_owner deliberately refuses the failed root. This sibling
        # has no callback/wait edge, but still shares the recovered execution.
        self.service._put('tasks', sibling)
        self.assert_blocked(source)

    def test_unknown_read_without_exact_recovery_copy_stays_guarded(self):
        _, source, _, successor, unknown, _, _ = self.saved_shape()
        for field, value in (('turn_id', 'wrong-turn'), ('task_id', 'other-task'),
                             ('action', dict(name='search_files', arguments={})), ('created_at', 'other-time')):
            saved = copy.deepcopy(unknown)
            saved[field] = value
            successor['_self_recovery']['unknown_tools'] = [saved]
            self.service._put('tasks', successor)
            self.assert_blocked(source)


if __name__ == '__main__':
    unittest.main()
