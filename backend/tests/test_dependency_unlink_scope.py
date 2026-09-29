"""Unlink waits for the two executions and their helpers, not the entire bot."""
from types import SimpleNamespace
import unittest
from uuid import uuid4

from coconut.service import ServiceError
import test_dependency_unlink as fixtures


class DependencyUnlinkScopeTests(unittest.TestCase):
    setUp = fixtures.DependencyUnlinkTests.setUp
    tearDown = fixtures.DependencyUnlinkTests.tearDown
    write = fixtures.DependencyUnlinkTests.write
    prepare = fixtures.DependencyUnlinkTests.prepare
    setup_edge = fixtures.DependencyUnlinkTests.setup_edge
    unlink = fixtures.DependencyUnlinkTests.unlink

    def other(self, root, **changes):
        identity = str(uuid4())
        task = dict(root, id=identity, _execution_session_id=identity,
                    _native_outcome_id='t_44444444', status='completed')
        for key in ('_native_relay', '_assignment_task_id', '_is_return', '_relay_callback_owner'):
            task.pop(key, None)
        task.update(changes)
        self.service._put('tasks', task)
        return task

    def test_unrelated_same_bot_work_and_unknown_history_do_not_block(self):
        child, parent, source = self.setup_edge()
        other = self.other(child, status='working')
        self.service.running[other['id']] = SimpleNamespace(is_alive=lambda: True, join=lambda **kwargs: None)
        self.snapshot_rows.append(dict(external_id=other['id'], id='t_44444444',
                                       status='running', current_run_id=123, worker_pid=456))
        self.service._put('native_receipts', dict(id='unrelated-unknown', task_id=other['id'],
                         state='executing', action={'name': 'terminal'}))
        self.service._put('contexts', dict(id='unrelated-context', task_id=other['id'], status='working'))
        self.assertEqual(self.unlink(source)['status'], 'unlinked')
        self.assertEqual(self.service._get('native_receipts', 'unrelated-unknown')['state'], 'executing')
        requested = self.service.kanban.call.call_args_list[0].args[0]['external_ids']
        self.assertNotIn(other['id'], requested)

    def test_unrelated_historical_pending_result_is_preserved(self):
        child, parent, source = self.setup_edge()
        other = self.other(child, status='waiting')
        receipt = dict(id='old-wait', task_id=other['id'], state='completed',
                       _wait_task_id='old-helper', action={'name': 'coconut_wait'})
        self.service._put('native_receipts', receipt)
        self.assertTrue(self.service._background_pending(other['id']))
        self.assertEqual(self.unlink(source)['status'], 'unlinked')
        self.assertEqual(self.service._get('native_receipts', 'old-wait'), receipt)

    def test_each_execution_continuation_still_blocks(self):
        child, parent, source = self.setup_edge()
        for root in (child, parent):
            with self.subTest(root=root['id']):
                peer = self.other(root, status='working', _execution_session_id=root['_execution_session_id'])
                with self.assertRaisesRegex(ServiceError, 'owner work'):
                    self.unlink(source)
                self.service._put('tasks', peer | {'status': 'completed'})
        self.assertEqual(self.requests, [])

    def test_cross_bot_helper_and_its_continuation_are_guarded(self):
        child, parent, source = self.setup_edge()
        foreign = self.write('/v1/bots', {'name': 'Helper'})
        helper = self.other(child, bot_id=foreign['id'], session_id=foreign['session_id'],
                            _assignment_task_id=child['id'])
        peer = self.other(helper, status='working', _execution_session_id=helper['_execution_session_id'])
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.service._put('tasks', peer | {'status': 'completed'})
        self.assertEqual(self.unlink(source)['status'], 'unlinked')

    def test_related_unknown_command_and_live_native_run_remain_guarded(self):
        child, parent, source = self.setup_edge()
        helper = self.other(parent, _assignment_task_id=parent['id'])
        self.service._put('native_receipts', dict(id='unknown', task_id=helper['id'],
                         state='executing', action={'name': 'terminal'}))
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.service._put('native_receipts', dict(id='unknown', task_id=helper['id'],
                         state='completed', action={'name': 'terminal'}))
        self.snapshot_rows.append(dict(external_id=helper['id'], id='t_44444444',
                                       status='running', current_run_id=123))
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_related_background_and_both_undelivered_return_shapes_block(self):
        child, parent, source = self.setup_edge()
        helper = self.other(child, _assignment_task_id=child['id'])
        self.service._put('native_receipts', dict(id='wait', task_id=helper['id'], state='completed',
                         _wait_task_id='nested-helper', action={'name': 'coconut_wait'}))
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.service._put('native_receipts', dict(id='wait', task_id=helper['id'], state='completed',
                         _process_delivery='consumed', action={'name': 'coconut_wait'}))
        for key in ('_return_pending', '_native_reply_pending'):
            self.service._put('tasks', helper | {key: True})
            with self.subTest(key=key), self.assertRaisesRegex(ServiceError, 'owner work'):
                self.unlink(source)
        self.service._put('tasks', helper)
        self.assertEqual(self.unlink(source)['status'], 'unlinked')

    def test_recorded_helper_links_do_not_walk_up_to_requester_siblings(self):
        child, parent, source = self.setup_edge()
        sibling = self.other(child, status='working', _native_relay={'id': 'other-request'})
        helper = self.other(parent, source_task_id=child['id'], status='working',
                            _native_relay={'id': 'helper-request'})
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.service._put('tasks', helper | {'status': 'completed'})
        self.assertEqual(self.unlink(source)['status'], 'unlinked')
        self.assertEqual(self.service._get('tasks', sibling['id'])['status'], 'working')

    def test_waited_helper_with_old_session_is_still_guarded(self):
        child, parent, source = self.setup_edge()
        helper = self.other(parent, status='working', session_id='previous-session')
        self.service._put('tasks', child | {'_waiting_on': [helper['id']]})
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_consumed_legacy_helper_still_guards_a_new_native_claim(self):
        from test_dependency_unlink_epoch import MissingEpochUnlinkTests
        fixture = MissingEpochUnlinkTests()
        fixture.setUp()
        try:
            child, parent, source = fixture.setup_edge()
            helper = fixture.producer
            self.assertNotIn('_native_relay', helper)
            self.assertNotIn('_assignment_task_id', helper)
            fixture.snapshot_rows.append(dict(external_id=helper['id'], id='t_44444444',
                                             status='running', current_run_id=123))
            with self.assertRaisesRegex(ServiceError, 'owner work'):
                fixture.unlink(source)
            self.assertEqual(fixture.requests, [])
        finally:
            fixture.tearDown()

    def test_legacy_helpers_do_not_expand_scope_to_every_unbound_task(self):
        child, parent, source = self.setup_edge()
        helper = self.other(child, _execution_session_id=None, _assignment_task_id=child['id'])
        self.other(child, _execution_session_id=None, status='working')
        self.assertEqual(self.unlink(source)['status'], 'unlinked')
        self.service._put('tasks', helper | {'status': 'working'})
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)

    def test_new_relation_after_snapshot_requires_a_fresh_read(self):
        child, parent, source = self.setup_edge()
        original = self.service.kanban.call.side_effect

        def changed(request):
            result = original(request)
            if request['op'] == 'unlink_receipt':
                self.other(parent, _assignment_task_id=parent['id'])
            return result

        self.service.kanban.call.side_effect = changed
        with self.assertRaisesRegex(ServiceError, 'related work changed'):
            self.unlink(source)
        self.assertEqual(self.requests, [])

    def test_return_callback_and_cycles_keep_their_writer_guard(self):
        child, parent, source = self.setup_edge()
        helper = self.other(parent, _assignment_task_id=child['id'])
        callback = self.other(parent, status='working', source_task_id=helper['id'],
                              _is_return=True, _assignment_task_id=child['id'])
        self.service._put('tasks', helper | {'_waiting_on': [child['id']]})
        with self.assertRaisesRegex(ServiceError, 'owner work'):
            self.unlink(source)
        self.service._put('tasks', callback | {'status': 'completed'})
        self.assertEqual(self.unlink(source)['status'], 'unlinked')

    def test_related_context_blocks(self):
        child, parent, source = self.setup_edge()
        helper = self.other(parent, _assignment_task_id=parent['id'])
        self.service._put('contexts', dict(id='context', task_id=helper['id'], status='queued'))
        with self.assertRaisesRegex(ServiceError, 'owner context'):
            self.unlink(source)
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
