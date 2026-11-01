"""The native proof of a waiting routine occurrence, read from a real Hermes Kanban board through the real adapter.

The fake board of test_waiting_occurrence_release.py cannot tell whether Hermes names the columns and attributes that the
completion operation reads. Here the original card is ended by a real run of the installed kernel, Coconut's own
promotion binds a successor card to the task, and the release reads both cards through the adapter process.
Native CI sets COCONUT_REQUIRE_NATIVE, and then a missing Hermes install fails these tests instead of skipping them.
"""
import copy
import json
import unittest
from uuid import UUID, uuid5

import test_native_kanban as fixtures
import test_waiting_occurrence_release as waiting
import hermes_env

SUMMARY = waiting.SUMMARY


class NativeOccurrenceCompletionTests(unittest.TestCase):
    write = fixtures.NativeKanbanTests.write
    native_program = fixtures.NativeKanbanTests.native_program
    native_settings = waiting.WaitingOccurrenceTests.native_settings
    event_count = waiting.WaitingOccurrenceTests.event_count
    operations = waiting.WaitingOccurrenceTests.operations
    tick = waiting.WaitingOccurrenceTests.tick
    claim = waiting.WaitingOccurrenceTests.claim
    update = waiting.WaitingOccurrenceTests.update
    receipt = waiting.WaitingOccurrenceTests.receipt
    stored = waiting.WaitingOccurrenceTests.stored
    assert_released = waiting.WaitingOccurrenceTests.assert_released

    def setUp(self):
        hermes_env.require(self)
        fixtures.NativeKanbanTests.setUp(self)
        self.native = waiting.NativeJob()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.calls, self.routine, self.view = [], None, None
        self.service.provider.native_settings = self.native_settings
        self.routine_id = str(uuid5(UUID(self.bot['id']), 'cron:job'))

    def binding(self):
        return self.native_program("print(board.conn.execute('SELECT native_id FROM coconut_bindings WHERE external_id=?',"
                                   "(sys.argv[2],)).fetchone()[0])", self.task_id)

    def card(self, identity):
        return self.service.kanban.call({'op': 'completion', 'id': identity})['card']

    def build(self, ended=True, capture=True):
        """The incident on a real board. A run of the kernel ended the original card, then Work moved the binding to a successor."""
        service = self.service
        task = self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Fast intake', 'mode': 'queue'})
        self.task_id = task['id']
        task = self.update(status='working', routine_id=self.routine_id, _native_job=copy.deepcopy(self.native.job),
                           _native_turn_id='turn-original')
        row = service._kanban_row(task, task, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        service.kanban.call({'op': 'snapshot'})
        self.original_id = service.kanban.tasks[self.task_id]['id']
        self.run = int(self.native_program("print(board.kb.claim_task(board.conn, sys.argv[2], claimer='default').current_run_id)",
                                           self.original_id))
        self.update(_kanban_claim=self.claim(self.original_id, self.run, 'turn-original'))
        service._event('routine.triggered', dict(routine_id=self.routine_id, task_id=self.task_id), self.bot['id'], self.task_id)
        if ended:
            self.native_program('assert board.kb.complete_task(board.conn, sys.argv[2], summary=sys.argv[3], expected_run_id=int(sys.argv[4]))',
                                self.original_id, SUMMARY, str(self.run))
        self.receipt('kanban_complete', dict(task_id=self.original_id, summary=SUMMARY),
                     dict(ok=True, task_id=self.original_id, run_id=self.run))
        # Retained Work binds a successor card to the task, and the delegated task is awaited.
        self.update(_work_context_id='work', _waiting_on=['dev-task'], _native_turn_id='turn-successor')
        row = dict(service._kanban_row(self.stored(), self.stored(), 'scheduled', 10), title='Fast intake', work_id='work',
                   body=json.dumps(dict(outcome='Fast intake')))
        self.successor_id = service.kanban.call({'op': 'promote', 'row': row})['id']
        self.assertNotEqual(self.successor_id, self.original_id)
        self.update(status='working', _native_outcome_id=self.successor_id,
                    _kanban_claim=self.claim(self.successor_id, 5380, 'turn-successor'))
        self.receipt('kanban_complete', dict(task_id=self.successor_id, summary='Verified the dependency.'),
                     dict(ok=True, task_id=self.successor_id, run_id=5380))
        task = self.update(status='waiting', summary='Waiting for a result.')
        if not capture:
            task.pop('_native_occurrence', None)
            with service.transaction():
                service._put('tasks', task)
        service.kanban.call({'op': 'snapshot'})
        return task

    def test_the_adapter_reads_the_ended_card_after_the_binding_moved_to_a_successor(self):
        self.build()
        self.assertEqual(self.binding(), self.successor_id)
        card = self.card(self.original_id)
        self.assertEqual((card['id'], card['tenant'], card['idempotency_key'], card['status']),
                         (self.original_id, self.bot['id'], 'coconut:' + self.task_id, 'done'))
        self.assertIsNone(card['current_run_id'])
        self.assertIsNone(card['worker_pid'])
        self.assertIsNone(card['input_id'])
        self.assertEqual([(r['id'], r['outcome'], r['ended']) for r in card['runs']], [(self.run, 'completed', True)])
        successor = self.card(self.successor_id)
        self.assertEqual(successor['idempotency_key'], 'coconut-outcome:work')
        self.assertNotEqual(successor['status'], 'done')
        self.assertIsNone(self.card('t_missing'))

    def test_the_real_card_proves_the_release_and_the_successor_stays_intact(self):
        task = self.build(capture=False)
        self.assertEqual(task['_kanban_claim']['native_id'], self.successor_id)
        before = {key: self.service.kanban.tasks[key] for key in self.service.kanban.tasks}
        self.tick()
        self.assert_released()
        after = self.stored()
        self.assertEqual(after['_native_release'], dict(native_id=self.original_id, run_id=self.run, turn_id='turn-original',
                         receipt_id=after['_native_release']['receipt_id'], job_id='job', owner='owner'))
        for field in ('status', 'summary', '_waiting_on', '_native_outcome_id', '_work_context_id', '_kanban_claim'):
            self.assertEqual(after.get(field), task.get(field), field)
        # Both cards were read by native id, and only the finished original proved the occurrence.
        self.assertEqual(self.binding(), self.successor_id)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[self.task_id], before[self.task_id])
        self.assertEqual(self.card(self.original_id)['status'], 'done')
        self.assertNotEqual(self.card(self.successor_id)['status'], 'done')

    def test_a_card_that_has_not_ended_natively_keeps_the_claim(self):
        self.build(ended=False)
        card = self.card(self.original_id)
        self.assertEqual(card['status'], 'running')
        self.assertEqual(card['current_run_id'], self.run)
        self.tick()
        self.assertEqual(self.operations(), ['routine_heartbeat'])
        self.assertEqual(self.native.job['fire_claim'], {'by': 'owner'})
        self.assertFalse(self.stored().get('_native_job_finished'))
        self.assertNotIn('_native_release', self.stored())


if __name__ == '__main__':
    unittest.main()
