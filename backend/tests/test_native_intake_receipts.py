"""Native creation receipts retain Coconut ownership across session normalization."""
import unittest
from unittest.mock import Mock

import test_service_pr as fixtures


class NativeIntakeReceiptTests(unittest.TestCase):
    setUp = fixtures.PRServiceTests.setUp
    tearDown = fixtures.PRServiceTests.tearDown
    write = fixtures.PRServiceTests.write

    def prepare(self, session_id='persisted-native-conversation', **changes):
        origin = self.service._get('tasks', self.task['id'])
        origin['status'] = 'working'
        self.service._put('tasks', origin)
        key = 'coconut:' + origin['id'] + ':intake'
        arguments = dict(session_id=origin['id'], tenant=self.bot['id'], idempotency_key=key)
        arguments.update(changes)
        receipt = self.service.native_event(origin['id'], 'native_call',
            dict(name='kanban_create', arguments=arguments))
        saved = self.service._get('native_receipts', receipt['id'])
        saved['_attachment_ids'] = ['accepted-attachment']
        self.service._put('native_receipts', saved)
        native = dict(id='t_123abc', unbound=True, session_id=session_id,
                      tenant=self.bot['id'], idempotency_key=key, title='Accepted outcome', status='blocked')
        previous = self.service.kanban
        self.service.kanban = Mock(tasks={'native:t_123abc': native})
        self.addCleanup(setattr, self.service, 'kanban', previous)
        return origin, native

    def owners(self):
        return [t for t in self.service._all('tasks') if t.get('_native_outcome_id') == 't_123abc']

    def test_adoption_does_not_scan_unrelated_tool_receipts(self):
        import json
        self.prepare()
        self.service.db.executemany('INSERT INTO native_receipts (id,data) VALUES (?,?)',
            [(f'old-{i}', json.dumps(dict(id=f'old-{i}', task_id='old-task',
              action=dict(name='terminal', arguments=dict(command='true'))))) for i in range(5000)])
        self.service.kanban.tasks['native:missing'] = dict(id='t_missing', unbound=True,
            session_id='unknown', tenant=self.bot['id'], idempotency_key='missing')
        steps = []
        self.service.db.set_progress_handler(lambda: steps.append(1000) or 0, 1000)
        try:
            self.service._adopt_native_outcomes()
        finally:
            self.service.db.set_progress_handler(None, 0)
        self.assertEqual(len(self.owners()), 1)
        self.assertLess(sum(steps), 10000, 'Adoption scans the tool receipt history under the service lock')

    def test_normalized_session_uses_durable_intent_before_result(self):
        origin, native = self.prepare()
        self.service._adopt_native_outcomes()
        self.service._adopt_native_outcomes()
        owners = self.owners()
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0]['parent_task_id'], origin['id'])
        self.assertEqual(owners[0]['bot_id'], origin['bot_id'])
        self.assertEqual(owners[0]['_attachment_ids'], ['accepted-attachment'])
        self.assertEqual(owners[0]['session_id'], origin['session_id'])
        self.assertEqual(native['session_id'], 'persisted-native-conversation')
        self.assertEqual(len([a for a in self.service._all('artifacts') if a['kind'] == 'work']), 1)
        self.service.kanban.call.assert_any_call(dict(op='bind', id=native['id'], external_id=owners[0]['id']))

    def test_receipt_wins_over_another_coconut_task_session(self):
        other = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Other input'))
        origin, _ = self.prepare(session_id=other['id'])
        self.service._adopt_native_outcomes()
        self.assertEqual(self.owners()[0]['parent_task_id'], origin['id'])

    def test_unknown_normalized_session_without_receipt_is_not_adopted(self):
        self.prepare()
        self.service.db.execute('DELETE FROM native_receipts')
        self.service._adopt_native_outcomes()
        self.assertEqual(self.owners(), [])
        self.service.kanban.call.assert_not_called()

    def test_legacy_task_session_without_receipt_still_adopts(self):
        self.prepare(session_id=self.task['id'])
        self.service.db.execute('DELETE FROM native_receipts')
        self.service._adopt_native_outcomes()
        self.assertEqual(self.owners()[0]['parent_task_id'], self.task['id'])

    def test_mismatched_receipt_origin_is_not_adopted(self):
        self.prepare(session_id=self.task['id'], tenant='another-bot')
        self.service._adopt_native_outcomes()
        self.assertEqual(self.owners(), [])

    def test_mismatched_receipt_session_is_not_adopted(self):
        self.prepare(session_id='another-task')
        record = self.service._all('native_receipts')[0]
        record['action']['arguments']['session_id'] = 'another-task'
        self.service._put('native_receipts', record)
        self.service._adopt_native_outcomes()
        self.assertEqual(self.owners(), [])

    def test_native_tenant_must_match_coconut_owner(self):
        _, native = self.prepare(tenant='another-bot')
        native['tenant'] = 'another-bot'
        self.service._adopt_native_outcomes()
        self.assertEqual(self.owners(), [])


if __name__ == '__main__':
    unittest.main()
