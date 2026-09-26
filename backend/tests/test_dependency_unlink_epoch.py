"""Missing input epochs require durable, current-conversation return proof."""
import unittest
from unittest.mock import patch

from coconut.service import ServiceError
import test_dependency_unlink as fixtures


class MissingEpochUnlinkTests(fixtures.DependencyUnlinkTests):
    def setup_edge(self, caller='Desk'):
        # Create the original owner after an actual conversation rotation.
        self.write('/v1/bots/restart', {})
        old = self.service._get('tasks', self.child)
        self.service._put('tasks', old | {'status': 'completed'})
        source = self.service._get('tasks', self.parent['id'])
        self.service._put('tasks', source | {'status': 'working'})
        with self.service.transaction():
            receipt = self.service._store_bot_message(source['id'],
                {'target_bot_id': self.dev['id'], 'text': 'Retain this exact implementation owner.'})
        self.child = receipt['task_id']
        child, parent, source = super().setup_edge(caller)
        helper = self.write('/v1/bots', {'name': 'Evidence helper'})
        with self.service.transaction():
            child.update(status='working', _native_turn_id='consumed-turn', _turn_message_ids=['consumed-message'])
            self.service._put('tasks', child)
            receipt = self.service._store_bot_message(child['id'],
                {'target_bot_id': helper['id'], 'text': 'Check the resource release.'})
            producer = self.service._get('tasks', receipt['task_id'])
            producer.update(status='completed', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', producer)
            if not getattr(self, 'summary_only', False):
                self.service._message(helper['id'], 'The resource was released.', task_id=producer['id'])
            child = self.service._get('tasks', child['id'])
            child['status'] = 'waiting'
            self.service._put('tasks', child)
            self.service._store_bot_message(producer['id'],
                {'target_bot_id': child['bot_id'], 'text': 'The resource was released.', 'result_status': 'completed'},
                returning=True)
        self.producer = producer
        child = self.service._get('tasks', child['id'])
        self.assertEqual(child['status'], 'queued')
        self.assertNotIn('_conversation_epoch', child)
        self.assertNotEqual(child['_native_turn_id'], 'consumed-turn')
        self.assertEqual(child['_input_after_message_id'], 'consumed-message')
        return child, parent, source

    def test_exact_legacy_input_and_all_service_records_are_preserved(self):
        child, parent, source = self.setup_edge()
        tables = ('tasks', 'artifacts', 'messages', 'decisions', 'contexts', 'deliveries', 'exchanges')
        before = {table: self.service._all(table) for table in tables}
        events = self.service.db.execute('SELECT * FROM events').fetchall()
        self.assertEqual(self.unlink(source)['status'], 'unlinked')
        self.assertEqual(before, {table: self.service._all(table) for table in tables})
        self.assertEqual(events, self.service.db.execute('SELECT * FROM events').fetchall())
        self.assertEqual(self.service._get('tasks', child['id']), child)

    def test_initial_unrotated_missing_epoch_keeps_original_admission(self):
        child, parent, source = fixtures.DependencyUnlinkTests.setup_edge(self)
        self.assertIsNone(self.service._setting('conversation_epoch'))
        self.assertEqual(self.service.db.execute("SELECT COUNT(*) FROM events WHERE type='bots.restarted'").fetchone()[0], 0)
        child.pop('_conversation_epoch', None)
        self.service._put('tasks', child)
        self.assertEqual(self.unlink(source)['status'], 'unlinked')
        self.assertEqual(self.service._get('tasks', child['id']), child)

    def test_summary_only_return_uses_persisted_exchange_message(self):
        self.summary_only = True
        child, parent, source = self.setup_edge()
        messages = self.service._all('messages')
        self.assertFalse(any(m.get('task_id') == self.producer['id'] and m.get('kind') == 'text' for m in messages))
        returned = next(m for m in messages if m.get('task_id') == child['id']
                        and m.get('kind') == 'bot_exchange' and m.get('sender_bot_id') == self.producer['bot_id'])
        for change in ({'recipient_bot_ids': [source['bot_id']]}, {'role': 'user'}, {'_hidden': True}):
            self.service._put('messages', returned | change)
            with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
                self.unlink(source)
        self.service._put('messages', returned)
        self.assertEqual(self.unlink(source)['status'], 'unlinked')
        self.assertEqual(self.service._get('tasks', child['id']), child)

    def test_native_owner_creation_event_preserves_exact_identity(self):
        child, parent, source = self.setup_edge()
        self.assertNotEqual(child['bot_id'], child['conversation_bot_id'])
        # Native outcome creation records the owner as event audience.
        self.service.db.execute("UPDATE events SET bot_id=? WHERE type='task.created' AND task_id=?",
                                ('foreign-bot', child['id']))
        with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
            self.unlink(source)
        self.service.db.execute("UPDATE events SET bot_id=? WHERE type='task.created' AND task_id=?",
                                (child['bot_id'], child['id']))
        self.assertEqual(self.unlink(source)['status'], 'unlinked')
        self.assertEqual(self.service._get('tasks', child['id']), child)

    def test_earlier_return_cannot_prove_a_later_queued_input(self):
        child, parent, source = self.setup_edge()
        first_turn = child['_native_turn_id']
        other = self.write('/v1/bots', {'name': 'Later helper'})
        with self.service.transaction():
            child.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
            self.service._put('tasks', child)
            self.service._event('task.updated', self.service._public(child), child['bot_id'], child['id'])
            receipt = self.service._store_bot_message(child['id'],
                {'target_bot_id': other['id'], 'text': 'Check another resource.'})
            producer = self.service._get('tasks', receipt['task_id'])
            producer.update(status='completed', _conversation_epoch=self.service._setting('conversation_epoch'),
                            _result_task_id='continuation-result')
            self.service._put('tasks', producer)
            child = self.service._get('tasks', child['id'])
            child['status'] = 'waiting'
            self.service._put('tasks', child)
            self.service._event('task.updated', self.service._public(child), child['bot_id'], child['id'])
            self.service._store_bot_message(producer['id'],
                {'target_bot_id': child['bot_id'], 'text': 'Later continuation result.', 'result_status': 'completed'},
                returning=True)
        current = self.service._get('tasks', child['id'])
        self.assertNotEqual(current['_native_turn_id'], first_turn)
        self.assertNotIn('_conversation_epoch', current)
        with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_later_input_event_cannot_reuse_an_earlier_return(self):
        child, parent, source = self.setup_edge()
        self.service._advance_input(child)
        self.service._put('tasks', child)
        self.service._event('task.updated', self.service._public(child), child['bot_id'], child['id'])
        with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_missing_return_message_is_read_once_across_delivery_candidates(self):
        child, parent, source = self.setup_edge()
        delivery = next(d for d in self.service._all('deliveries') if d['source_task_id'] == self.producer['id'])
        for index in range(3):
            self.service._put('deliveries', delivery | {'id': f'repeated-return-{index}'})
        message = next(m for m in self.service._all('messages') if m['task_id'] == self.producer['id'] and m['kind'] == 'text')
        self.service._put('messages', message | {'_hidden': True})
        with patch.object(self.service, '_all', wraps=self.service._all) as read:
            self.assertFalse(self.service._unlink_conversation_current(child))
        self.assertEqual(sum(call.args == ('messages',) for call in read.call_args_list), 1)

    def test_explicit_null_stale_epoch_and_nonoriginal_execution_reject(self):
        child, parent, source = self.setup_edge()
        for change in ({'_conversation_epoch': None}, {'_conversation_epoch': 'old'},
                       {'_execution_session_id': source['id']}, {'_native_turn_id': None}, {'status': 'waiting'}):
            with self.subTest(change=change):
                self.service._put('tasks', child | change)
                with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
                    self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_restart_rejects_even_a_new_current_epoch_helper(self):
        child, parent, source = self.setup_edge()
        self.write('/v1/bots/restart', {})
        self.service._put('tasks', self.producer | {'_conversation_epoch': self.service._setting('conversation_epoch')})
        with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_missing_conflicting_and_reordered_history_rejects(self):
        child, parent, source = self.setup_edge()
        for mode in ('creation', 'rotation', 'delivery', 'duplicate', 'identity', 'order'):
            with self.subTest(mode=mode):
                history = self.service.db.execute('SELECT * FROM events').fetchall()
                try:
                    if mode in ('creation', 'rotation', 'delivery'):
                        kind = {'creation': 'task.created', 'rotation': 'bots.restarted', 'delivery': 'bot_message.delivered'}[mode]
                        self.service.db.execute('DELETE FROM events WHERE type=?', (kind,))
                    elif mode == 'duplicate':
                        self.service._event('task.created', self.service._public(child), child['conversation_bot_id'], child['id'])
                    elif mode == 'identity':
                        self.service.db.execute("UPDATE events SET data=json_set(data,'$.session_id','foreign') WHERE type='task.created' AND task_id=?", (child['id'],))
                    else:
                        # Equal timestamps cannot turn a post-creation rotation into earlier authority.
                        self.service._event('bots.restarted', {'ok': True})
                        self.service.db.execute("UPDATE events SET created_at='2000-01-01T00:00:00Z'")
                    with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
                        self.unlink(source)
                finally:
                    self.service.db.execute('DELETE FROM events')
                    self.service.db.executemany('INSERT INTO events VALUES (?,?,?,?,?,?)', history)
        self.assertEqual(self.requests, [])

    def test_foreign_or_unproven_return_rejects(self):
        child, parent, source = self.setup_edge()
        producer = self.producer
        for change in ({'source_task_id': source['id']}, {'_reply_to_task_id': source['id']},
                       {'source_bot_id': 'foreign'}, {'thread_id': 'foreign'}, {'exchange_id': 'foreign'},
                       {'_conversation_epoch': 'old'}, {'status': 'failed'}, {'_assignment_task_id': source['id']},
                       {'_result_task_id': source['id']}):
            with self.subTest(change=change):
                self.service._put('tasks', producer | change)
                with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
                    self.unlink(source)
        self.service._put('tasks', producer)
        delivery = next(d for d in self.service._all('deliveries') if d['source_task_id'] == producer['id'])
        for change in ({'_progress_only': True}, {'text': 'Different result'},
                       {'receipt': delivery['receipt'] | {'task_id': source['id']}},
                       {'receipt': delivery['receipt'] | {'status': 'paused'}}):
            with self.subTest(change=change):
                self.service._put('deliveries', delivery | change)
                with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
                    self.unlink(source)
        self.service._put('deliveries', delivery)
        message = next(m for m in self.service._all('messages') if m['task_id'] == producer['id'] and m['kind'] == 'text')
        self.service._put('messages', message | {'_hidden': True})
        with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_unknown_receipt_context_and_work_pause_still_reject(self):
        child, parent, source = self.setup_edge()
        with patch.object(self.service, '_task_work_is_paused', return_value=True):
            with self.assertRaisesRegex(ServiceError, 'paused'):
                self.unlink(source)
        self.service._put('native_receipts', dict(id='unknown', task_id=child['id'], state='executing', action={'name': 'terminal'}))
        with self.assertRaisesRegex(ServiceError, 'tool results'):
            self.unlink(source)
        self.service._put('native_receipts', dict(id='unknown', task_id=child['id'], state='completed', action={'name': 'terminal'}))
        self.service._put('contexts', dict(id='context', task_id=child['id'], status='queued'))
        with self.assertRaisesRegex(ServiceError, 'owner context'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_input_epoch_and_owner_changes_during_receipt_lookup_reject(self):
        child, parent, source = self.setup_edge()
        call = self.service.kanban.call.side_effect
        for change in ({'_native_turn_id': 'new-input'}, {'_execution_session_id': source['id']},
                       {'_conversation_epoch': self.service._setting('conversation_epoch')}):
            with self.subTest(change=change):
                self.service._put('tasks', child)
                def changed(request):
                    if request['op'] == 'unlink_receipt':
                        self.service._put('tasks', child | change)
                    return call(request)
                self.service.kanban.call.side_effect = changed
                with self.assertRaisesRegex(ServiceError, 'input changed'):
                    self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_compatibility_does_not_admit_todo_continuation(self):
        child, parent, source = self.setup_edge()
        with self.assertRaisesRegex(ServiceError, 'earlier conversation'):
            self.service.emit(source['id'], 'work_continue',
                {'task_id': child['_native_outcome_id'], 'message': 'Continue this same outcome.'})
        self.service._put('tasks', child | {'_conversation_epoch': self.service._setting('conversation_epoch')})
        with self.assertRaisesRegex(ServiceError, 'not blocked or completed'):
            self.service.emit(source['id'], 'work_continue',
                {'task_id': child['_native_outcome_id'], 'message': 'Continue this same outcome.'})
        self.assertEqual(self.requests, [])

    def test_rotation_during_receipt_lookup_rejects_without_stamping_input(self):
        child, parent, source = self.setup_edge()
        call = self.service.kanban.call.side_effect
        def rotated(request):
            if request['op'] == 'unlink_receipt':
                self.write('/v1/bots/restart', {})
            return call(request)
        self.service.kanban.call.side_effect = rotated
        with self.assertRaisesRegex(ServiceError, 'input changed'):
            self.unlink(source)
        self.assertEqual(self.service._get('tasks', child['id']), child)
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
