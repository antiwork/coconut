import base64
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service, ServiceError, YieldForDecision, digest
from coconut.hermes_worker import conversation_input
from test_push import pair_phone
from test_service import ControlledProvider


class TeamServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.a = self.write('POST', '/v1/bots', {'name': 'Ada', 'instructions': 'A private instruction', 'memory': 'A private memory'})
        self.b = self.write('POST', '/v1/bots', {'name': 'Lin', 'instructions': 'B private instruction', 'memory': 'B private memory'})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None, key=None):
        return self.service.mutate(method, path, data or {}, key or str(uuid4()), 'test-phone')

    def send(self, bot, text='Read the report', **extra):
        return self.write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': text, 'mode': 'queue', **extra})

    def working(self, task):
        with self.service.transaction():
            current = self.service._get('tasks', task['id'])
            current['status'] = 'working'
            self.service._put('tasks', current)
        return current


    def restart(self):
        self.service.close()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)

    def deliver(self, source, target, text='Read the source'):
        return self.service.emit(source['id'], 'bot_message', {'target_bot_id': target['id'], 'text': text})

    def routine(self, **fields):
        return self.write('POST', f'/v1/bots/{self.a["id"]}/routines', {'name': 'Daily check', 'prompt': 'Read the check', 'workflow': 'general', 'cron': '* * * * *', 'timezone': 'UTC', **fields})

    def due(self, routine):
        current = datetime.now(timezone.utc).replace(microsecond=0)
        with self.service.transaction():
            row = self.service._get('routines', routine['id'])
            row['next_run_at'] = (current - timedelta(minutes=20)).isoformat().replace('+00:00', 'Z')
            self.service._put('routines', row)
        return current

    def finish(self, task, text='Finished the assigned work.'):
        self.service.emit(task['id'], 'text_delta', {'text': text})
        self.service.emit(task['id'], 'completed', {'summary': 'Complete.'})

    def test_return_reads_later_human_scope_from_its_source_task(self):
        parent = self.working(self.send(self.a, 'Prepare the patch locally'))
        child = self.working(self.service._get('tasks', self.deliver(parent, self.b)['task_id']))
        self.finish(parent, 'I delegated local preparation')
        self.write('POST', f'/v1/tasks/{child["id"]}/replies', {'text': 'Publish the approved draft PR'})
        self.send(self.b, 'Unrelated private request')
        receipt = self.deliver(child, self.a, 'Earlier report: local only; publication needs approval')
        returned = self.service._get('tasks', receipt['task_id'])
        captured = []
        def receive(task, bot, history, emit):
            captured.append(task)
            emit('completed', {'summary': 'Complete.'})
        self.provider.start_task = receive
        self.service._run_task(returned['id'])
        prompt = conversation_input(dict(operation='start', task=captured[0], history=[]))[0]
        self.assertIn('Publish the approved draft PR', prompt)
        self.assertNotIn('Unrelated private request', prompt)
        self.assertNotIn(self.b['instructions'], prompt)

    def test_nested_handoff_resumes_original_scope_and_returns_once_after_restart(self):
        third = self.write('POST', '/v1/bots', {'name': 'Third'})
        root = self.working(self.send(self.a, 'Complete the maintenance.'))
        assignment_text = ('Check dependencies, then finish the maintenance. ' + 'Keep the existing files. ' * 10).strip()
        middle = self.working(self.service._get('tasks', self.deliver(root, self.b, assignment_text)['task_id']))
        leaf = self.working(self.service._get('tasks', self.deliver(middle, third, 'Check the dependency.')['task_id']))
        self.finish(root, 'I asked Lin to handle this.')
        # The reply arrives while Lin still works on an independent step.
        self.finish(leaf, 'The dependency is unused.')
        self.service._drain_returns()
        self.finish(middle, 'The independent step is done.')
        for task in (root, middle):
            stored = self.service._get('tasks', task['id'])
            self.assertEqual(stored['status'], 'waiting')
            self.assertNotIn('_return_pending', stored)
        self.restart()
        continuation = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == middle['id'])
        self.assertEqual(continuation['_resolved_dependency_id'], leaf['id'])
        captured = []
        def continue_work(task, bot, history, emit):
            captured.append(task)
            emit('text_delta', {'text': 'The maintenance is complete.'})
            emit('completed', {'summary': 'Complete.'})
        self.provider.start_task = continue_work
        self.service._run_task(continuation['id'])
        context = captured[0]['_handoff_assignment']
        self.assertEqual(context['request'], assignment_text)
        self.assertEqual(context['return_to_bot_id'], self.a['id'])
        self.assertEqual(context['waiting_on_task_ids'], [])
        self.assertEqual(captured[0]['_outgoing_bot_messages'][0]['status'], 'completed')
        prompt = conversation_input(dict(operation='start', task=captured[0], history=[]))[0]
        self.assertIn(assignment_text, prompt)
        self.assertIn('finish the remaining authorized work', prompt)
        self.restart()
        self.service._drain_returns()
        self.service._drain_returns()
        final = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'])
        self.assertEqual(final['bot_id'], self.a['id'])
        self.assertEqual(final['_bot_messages'][0]['text'], 'The maintenance is complete.')
        self.service._run_task(final['id'])
        self.service._drain_returns()
        self.assertEqual(len(self.service._all('tasks')), 5)
        self.assertTrue(all(t['status'] == 'completed' for t in self.service._all('tasks')))
        before = len(self.service._all('messages'))
        self.restart()
        self.assertEqual(len(self.service._all('messages')), before)

    def test_multiple_dependencies_keep_assignment_waiting_until_all_replies_are_processed(self):
        third = self.write('POST', '/v1/bots', {'name': 'Third'})
        root = self.working(self.send(self.a))
        children = [self.working(self.service._get('tasks', self.deliver(root, bot)['task_id'])) for bot in (self.b, third)]
        self.finish(root)
        for index, child in enumerate(children):
            self.finish(child)
            self.service._drain_returns()
            reply = next(t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == child['id'])
            self.service._run_task(reply['id'])
            stored = self.service._get('tasks', root['id'])
            self.assertEqual(stored['status'], 'waiting' if index == 0 else 'completed')
            self.assertEqual(len(stored['_waiting_on']), 1 if index == 0 else 0)

    def test_continuation_can_delegate_again_without_losing_its_owner(self):
        root = self.working(self.send(self.a))
        child = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
        self.finish(root)
        self.finish(child)
        self.service._drain_returns()
        reply = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'])
        self.working(reply)
        next_child = self.working(self.service._get('tasks', self.deliver(reply, self.b, 'Check another dependency.')['task_id']))
        self.assertFalse(next_child['_is_return'])
        self.finish(reply)
        self.assertEqual(self.service._get('tasks', root['id'])['status'], 'waiting')
        self.finish(next_child)
        self.service._drain_returns()
        next_reply = next(t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == next_child['id'])
        self.assertEqual(next_reply['_assignment_task_id'], root['id'])
        self.service._run_task(next_reply['id'])
        self.assertEqual(self.service._get('tasks', root['id'])['status'], 'completed')

    def test_automatic_results_return_after_many_deliveries(self):
        root = self.working(self.send(self.a))
        child = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
        self.finish(root)
        with self.service.transaction():
            exchange = self.service._get('exchanges', root['exchange_id'])
            exchange['deliveries'] = 100
            self.service._put('exchanges', exchange)
        self.finish(child)
        self.restart()
        self.service._drain_returns()
        reply = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'])
        self.working(reply)
        self.assertEqual(self.service._get('exchanges', root['exchange_id'])['deliveries'], 100)
        self.finish(reply)
        self.assertEqual(self.service._get('tasks', root['id'])['status'], 'completed')

    def test_cancel_waiting_assignment_cancels_its_queued_continuation(self):
        root = self.working(self.send(self.a))
        child = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
        self.finish(root)
        self.finish(child)
        self.service._drain_returns()
        self.write('POST', f'/v1/tasks/{root["id"]}/cancel')
        self.restart()
        self.service._drain_returns()
        self.assertEqual(self.service._get('tasks', root['id'])['status'], 'cancelled')
        reply = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'])
        self.assertEqual(reply['status'], 'cancelled')

    def test_failed_continuation_returns_failure_to_original_requester(self):
        third = self.write('POST', '/v1/bots', {'name': 'Third'})
        root = self.working(self.send(self.a))
        middle = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
        leaf = self.working(self.service._get('tasks', self.deliver(middle, third)['task_id']))
        self.finish(root)
        self.finish(middle)
        self.finish(leaf)
        self.service._drain_returns()
        continuation = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == middle['id'])
        self.working(continuation)
        self.service.emit(continuation['id'], 'failed', {'summary': 'The tool failed.'})
        self.assertEqual(self.service._get('tasks', middle['id'])['status'], 'failed')
        self.restart()
        self.service._drain_returns()
        result = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'])
        self.assertEqual(result['_bot_messages'][0]['result_status'], 'failed')
        self.assertIn('The tool failed.', result['_bot_messages'][0]['text'])

    def test_completion_notification_uses_final_continuation_and_waits_for_it(self):
        from coconut.push_relay import notification_alert
        device = pair_phone(self.service)
        self.service.push.registration(device, dict(token='a' * 64, environment='development', enabled=True))
        root = self.working(self.send(self.a))
        child = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
        self.finish(root, 'I am waiting for Lin.')
        self.finish(child)
        self.service._drain_returns()
        self.assertEqual(self.service.db.execute('SELECT COUNT(*) FROM push_items').fetchone()[0], 0)
        reply = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'])
        self.working(reply)
        self.finish(reply, 'The complete result is ready.')
        jobs = self.service.db.execute('SELECT task_id,kind,turn_id FROM push_items').fetchall()
        stored = self.service._get('tasks', root['id'])
        self.assertEqual(jobs, [(root['id'], 'completed', stored.get('_native_turn_id') or stored['updated_at'])])
        alert = notification_alert(self.service, {'kind': 'completed'}, self.a, stored, None)
        self.assertEqual(alert['body'], 'The complete result is ready.')

    def test_explicit_native_reply_records_its_own_receipt_after_restart(self):
        root = self.working(self.send(self.a))
        child = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
        self.finish(root)
        envelope = uuid4().hex
        receipt = self.service._deliver_bot_message(child['id'], dict(target_bot_id=self.a['id'],
                                                    text='The dependency is ready.', native_envelope_id=envelope))
        self.finish(child)
        self.service._run_task(receipt['task_id'])
        self.assertEqual(self.service._get('tasks', receipt['task_id'])['_native_reply_pending']['envelope_id'], envelope)
        self.restart()
        native = []
        self.provider.native_settings = lambda operation, bots, **payload: native.append((operation, payload)) or {'ok': True}
        self.service._drain_returns()
        self.service._drain_returns()
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0][0], 'relay_reply')
        self.assertEqual(native[0][1]['envelope_id'], envelope)
        self.assertEqual(native[0][1]['bot_id'], self.b['id'])
        self.assertFalse(native[0][1]['failed'])
        self.assertEqual(len(self.service._all('tasks')), 3)

    def test_dependency_reply_waits_for_original_decision_before_running(self):
        root = self.working(self.send(self.a))
        child = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
        with self.assertRaises(YieldForDecision):
            self.service.emit(root['id'], 'decision', {'title': 'Save the report?', 'explanation': 'Save this file.',
                'action_type': 'create_file', 'action': {'type': 'create_file', 'filename': 'report.md', 'content': 'Report'},
                'predicted_choice': 'approve'})
        self.finish(child)
        self.service._drain_returns()
        reply = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'])
        self.restart()
        self.assertTrue(self.service._continuation_blocked(reply))
        self.service._run_task(reply['id'])
        self.assertEqual(self.provider.starts, [])
        self.assertEqual(self.service._get('tasks', reply['id'])['status'], 'queued')
        self.assertEqual(self.service._get('tasks', root['id'])['_waiting_on'], [child['id']])
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', f'/v1/decisions/{decision["id"]}/answer', {'choice': 'approve', 'revision': decision['revision']})
        self.service._run_task(root['id'])
        self.assertEqual(self.service._get('tasks', root['id'])['status'], 'waiting')
        self.assertFalse(self.service._continuation_blocked(reply))
        self.service._run_task(reply['id'])
        self.assertEqual(self.service._get('tasks', root['id'])['status'], 'completed')

    def test_completed_dependency_returns_once_after_failed_owner_and_allows_recovery(self):
        desk = self.working(self.send(self.a, 'Coordinate the fix.'))
        dev = self.working(self.service._get('tasks', self.deliver(desk, self.b, 'Fix the bug.')['task_id']))
        care = self.write('POST', '/v1/bots', {'name': 'Care'})
        dependency = self.working(self.service._get('tasks', self.deliver(dev, care, 'Check setup.')['task_id']))
        self.service.emit(dev['id'], 'failed', {'summary': 'A tool stopped without a receipt.'})
        self.finish(dependency, 'The setup check was queued; no capacity was reclaimed.')
        self.service._drain_returns()
        callback = next(t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == dependency['id'])
        self.assertEqual(callback['_assignment_task_id'], dev['id'])
        self.assertEqual(self.service._get('tasks', dev['id'])['_waiting_on'], [dependency['id']])
        self.service._run_task(callback['id'])
        owner = self.service._get('tasks', dev['id'])
        self.assertEqual(owner['status'], 'failed')
        self.assertEqual(owner['_waiting_on'], [])
        self.assertEqual(owner['_bot_messages'][-1]['text'], 'The setup check was queued; no capacity was reclaimed.')
        self.assertEqual(self.service._get('tasks', callback['id'])['status'], 'completed')
        self.restart()
        self.service._drain_returns()
        self.service._run_task(callback['id'])
        self.assertEqual(len(self.service._get('tasks', dev['id'])['_bot_messages']), 2)
        desk = self.working(desk)
        continued = self.service.emit(desk['id'], 'bot_message', {'target_bot_id': self.b['id'],
            'continuation_task_id': dev['id'], 'text': 'Continue only the unfinished test in the saved workspace.'})
        recovered = self.service._get('tasks', continued['task_id'])
        self.assertEqual(recovered['_execution_session_id'],
                         self.service._get('tasks', dev['id'])['_execution_session_id'])
        self.assertEqual(recovered['_bot_messages'][-2]['text'], 'The setup check was queued; no capacity was reclaimed.')
        self.assertEqual(len([t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == dependency['id']]), 1)

    def test_saved_failed_callback_is_consumed_after_restart_without_replaying_dependency(self):
        desk = self.working(self.send(self.a, 'Coordinate the fix.'))
        dev = self.working(self.service._get('tasks', self.deliver(desk, self.b, 'Fix the bug.')['task_id']))
        care = self.write('POST', '/v1/bots', {'name': 'Care'})
        dependency = self.working(self.service._get('tasks', self.deliver(dev, care, 'Check setup.')['task_id']))
        self.service.emit(dev['id'], 'failed', {'summary': 'A tool stopped without a receipt.'})
        self.finish(dependency, 'The setup check was queued.')
        self.service._drain_returns()
        callback = next(t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == dependency['id'])
        with self.service.transaction():
            callback = self.service._get('tasks', callback['id'])
            callback.update(status='failed',
                summary='The original assignment stopped. Reply to continue. Your follow-up is saved. Reply to continue.',
                _blocked_followup={'message_ids': [], 'source_ids': [], 'attachment_ids': []},
                steering_status='blocked')
            self.service._put('tasks', callback)
        self.restart()
        self.service._drain_returns()
        stored = self.service._get('tasks', dev['id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertEqual(stored['_waiting_on'], [])
        self.assertEqual(stored['_bot_messages'][-1]['text'], 'The setup check was queued.')
        self.assertEqual(self.service._get('tasks', callback['id'])['status'], 'completed')
        self.assertIn('_blocked_followup', self.service._get('tasks', callback['id']))
        self.service._drain_returns()
        self.assertEqual(len(self.service._get('tasks', dev['id'])['_bot_messages']), 2)
        desk = self.working(desk)
        receipt = self.service.emit(desk['id'], 'bot_message', {'target_bot_id': self.b['id'],
            'continuation_task_id': dev['id'], 'text': 'Continue the unfinished test.'})
        self.assertEqual(self.service._get('tasks', receipt['task_id'])['_bot_messages'][-2]['text'],
                         'The setup check was queued.')

    def test_stopped_callback_recovery_rejects_a_cancelled_owner_and_a_mismatched_receipt(self):
        for invalid in ('cancelled', 'wrong-result'):
            with self.subTest(invalid=invalid):
                desk = self.working(self.send(self.a, 'Coordinate.'))
                dev_receipt = self.deliver(desk, self.b, 'Fix.')
                assert dev_receipt is not None
                dev = self.working(self.service._get('tasks', dev_receipt['task_id']))
                care = self.write('POST', '/v1/bots', {'name': 'Care' + invalid})
                setup_receipt = self.deliver(dev, care, 'Check setup.')
                assert setup_receipt is not None
                dependency = self.working(self.service._get('tasks', setup_receipt['task_id']))
                self.service.emit(dev['id'], 'failed', {'summary': 'Tool stopped.'})
                self.finish(dependency, 'Saved setup result.')
                self.service._drain_returns()
                callback = next(t for t in self.service._all('tasks')
                                if t.get('_resolved_dependency_id') == dependency['id'])
                with self.service.transaction():
                    callback = self.service._get('tasks', callback['id'])
                    callback.update(status='failed', summary='The original assignment stopped. Reply to continue.')
                    if invalid == 'wrong-result':
                        callback['_bot_messages'][0]['text'] = 'Altered result.'
                    else:
                        owner = self.service._get('tasks', dev['id'])
                        owner['status'] = 'cancelled'
                        self.service._put('tasks', owner)
                    self.service._put('tasks', callback)
                self.service._drain_returns()
                self.assertEqual(self.service._get('tasks', callback['id'])['status'], 'failed')
                self.assertEqual(self.service._get('tasks', dev['id'])['_waiting_on'], [dependency['id']])

    def test_failed_continuation_preserves_an_independent_result(self):
        device = pair_phone(self.service)
        self.service.push.registration(device, dict(token='a' * 64, environment='development', enabled=True))
        third = self.write('POST', '/v1/bots', {'name': 'Third'})
        root = self.working(self.send(self.a))
        children = [self.working(self.service._get('tasks', self.deliver(root, bot)['task_id'])) for bot in (self.b, third)]
        self.finish(root)
        for child in children:
            self.finish(child)
        self.service._drain_returns()
        replies = [t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id']]
        self.working(replies[0])
        self.service.emit(replies[0]['id'], 'failed', {'summary': 'The tool failed.'})
        stored = self.service._get('tasks', root['id'])
        self.assertEqual(stored['status'], 'waiting')
        self.assertEqual(stored['_waiting_on'], [replies[1]['_resolved_dependency_id']])
        self.assertFalse(self.service._continuation_blocked(replies[1]))
        self.service._run_task(replies[1]['id'])
        self.assertEqual([task['id'] for task, _, _ in self.provider.starts], [replies[1]['id']])
        self.assertEqual(self.service._get('tasks', replies[1]['id'])['status'], 'completed')
        failed = self.service._get('tasks', replies[0]['id'])
        self.assertEqual((failed['status'], failed['summary']), ('failed', 'The tool failed.'))
        self.assertTrue(any(m['task_id'] == failed['id'] and m['text'] == 'The tool failed.'
                            for m in self.service._all('messages')))
        jobs = self.service.db.execute('SELECT task_id,kind FROM push_items').fetchall()
        self.assertEqual(jobs, [(root['id'], 'completed')])

    def test_rejecting_a_continuation_proposal_resolves_the_original_assignment(self):
        root = self.working(self.send(self.a))
        child = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
        self.finish(root)
        self.finish(child)
        self.service._drain_returns()
        reply = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'])
        self.working(reply)
        with self.assertRaises(YieldForDecision):
            self.service.emit(reply['id'], 'decision', {'title': 'Save the report?', 'explanation': 'Save this file.',
                'action_type': 'create_file', 'action': {'type': 'create_file', 'filename': 'report.md', 'content': 'Report'},
                'predicted_choice': 'approve'})
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', f'/v1/decisions/{decision["id"]}/answer', {'choice': 'reject', 'revision': decision['revision']})
        stored = self.service._get('tasks', root['id'])
        self.assertEqual(stored['status'], 'completed')
        self.assertEqual(stored['_waiting_on'], [])
        self.assertEqual(self.service.snapshot()['artifacts'], [])

    def test_sibling_replies_wait_for_a_decision_but_separate_assignments_can_run(self):
        third = self.write('POST', '/v1/bots', {'name': 'Third'})
        root = self.working(self.send(self.a))
        children = [self.working(self.service._get('tasks', self.deliver(root, bot)['task_id'])) for bot in (self.b, third)]
        self.finish(root)
        for child in children:
            self.finish(child)
        self.service._drain_returns()
        replies = [t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id']]
        self.working(replies[0])
        with self.assertRaises(YieldForDecision):
            self.service.emit(replies[0]['id'], 'decision', {'title': 'Save the report?', 'explanation': 'Save this file.',
                'action_type': 'create_file', 'action': {'type': 'create_file', 'filename': 'report.md', 'content': 'Report'},
                'predicted_choice': 'approve'})
        self.restart()
        self.assertTrue(self.service._continuation_blocked(replies[1]))
        self.service._run_task(replies[1]['id'])
        self.assertEqual(self.provider.starts, [])
        self.assertEqual(self.service._get('tasks', replies[1]['id'])['status'], 'queued')
        independent = self.send(self.a, 'An independent request.')
        self.service._run_task(independent['id'])
        self.assertEqual(self.service._get('tasks', independent['id'])['status'], 'completed')
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', f'/v1/decisions/{decision["id"]}/answer', {'choice': 'approve', 'revision': decision['revision']})
        self.assertFalse(self.service._continuation_blocked(replies[0]))
        self.service._run_task(replies[0]['id'])
        self.assertEqual(self.service._get('tasks', root['id'])['status'], 'waiting')
        self.service._run_task(replies[1]['id'])
        self.assertEqual(self.service._get('tasks', root['id'])['status'], 'completed')

    def test_deleting_dependency_bot_returns_failure_and_releases_waiting_owner(self):
        for reply_already_queued in (False, True):
            with self.subTest(reply_already_queued=reply_already_queued):
                peer = self.write('POST', '/v1/bots', {'name': 'Dependency'})
                root = self.working(self.send(self.a))
                child = self.working(self.service._get('tasks', self.deliver(root, peer)['task_id']))
                self.finish(root)
                if reply_already_queued:
                    self.finish(child)
                    self.service._drain_returns()
                self.write('DELETE', f'/v1/bots/{peer["id"]}')
                self.restart()
                self.service._drain_returns()
                replies = [t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'] and t['status'] == 'queued']
                self.assertEqual(len(replies), 1)
                self.assertEqual(replies[0]['_bot_messages'][0]['result_status'], 'failed')
                self.assertIn('was deleted', replies[0]['_bot_messages'][0]['text'])
                self.service._run_task(replies[0]['id'])
                stored = self.service._get('tasks', root['id'])
                self.assertEqual(stored['status'], 'completed')
                self.assertEqual(stored['_waiting_on'], [])
                self.service._drain_returns()
                self.assertEqual(len([t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'] and t['status'] == 'queued']), 0)

    def test_human_reply_to_waiting_work_keeps_assignment_and_blocks_dependency_reply(self):
        root = self.working(self.send(self.a, 'Finish the report.'))
        child = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
        self.finish(root)
        reply = self.write('POST', f'/v1/tasks/{root["id"]}/replies', {'text': 'Also save a copy.'})
        self.assertEqual(reply['id'], root['id'])
        self.assertEqual(reply['status'], 'queued')
        self.assertEqual(self.service._get('tasks', root['id'])['_waiting_on'], [child['id']])
        self.working(reply)
        with self.assertRaises(YieldForDecision):
            self.service.emit(reply['id'], 'decision', {'title': 'Save the report?', 'explanation': 'Save this file.',
                'action_type': 'create_file', 'action': {'type': 'create_file', 'filename': 'report.md', 'content': 'Report'},
                'predicted_choice': 'approve'})
        self.finish(child)
        self.service._drain_returns()
        continuation = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'])
        self.service._run_task(continuation['id'])
        self.assertEqual(self.provider.starts, [])
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', f'/v1/decisions/{decision["id"]}/answer', {'choice': 'approve', 'revision': decision['revision']})
        self.service._run_task(root['id'])
        self.service._run_task(continuation['id'])
        self.assertEqual(self.service._get('tasks', root['id'])['status'], 'completed')
        self.assertIn('Also save a copy.', self.provider.starts[-1][0]['_handoff_assignment']['request'])

    def test_failed_continuation_blocks_human_replies_queued_during_its_turn(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                root = self.working(self.send(self.a))
                child = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
                self.finish(root)
                self.finish(child)
                self.service._drain_returns()
                continuation = next(t for t in self.service._all('tasks') if t.get('_assignment_task_id') == root['id'])
                for text in ('Also save a copy.', 'Keep both versions.'):
                    self.write('POST', f'/v1/tasks/{root["id"]}/replies', {'text': text})
                if uncertain:
                    with patch.object(self.service, '_has_uncertain_receipt', return_value=True):
                        self.service._run_task(continuation['id'])
                else:
                    self.working(continuation)
                    self.service.emit(continuation['id'], 'failed', {'summary': 'The tool failed.'})
                stored = self.service._get('tasks', root['id'])
                self.assertEqual(stored['status'], 'failed')
                held = stored['_blocked_followup']['message_ids']
                self.assertEqual([self.service._get('messages', identity)['text'] for identity in held],
                                 ['Also save a copy.', 'Keep both versions.'])
                self.restart()
                self.service._run_task(root['id'])
                self.assertEqual(self.provider.starts, [])

    def test_mentions_address_only_selected_bots_once(self):
        c = self.write('POST', '/v1/bots', {'name': 'Ken'})
        first = self.send(self.a, 'Read the shared request', recipient_bot_ids=[self.b['id'], c['id'], self.b['id']])
        state = self.service.snapshot()
        self.assertEqual({t['bot_id'] for t in state['tasks']}, {self.b['id'], c['id']})
        self.assertEqual(len(first['dispatched_task_ids']), 2)
        self.assertEqual(len({t['thread_id'] for t in state['tasks']}), 1)
        self.assertTrue(all(t['conversation_bot_id'] == self.a['id'] for t in state['tasks']))
        self.assertEqual(len(state['messages']), 1)
        self.assertEqual(state['messages'][0]['recipient_bot_ids'], [self.b['id'], c['id']])
        for task in state['tasks']:
            self.service._run_task(task['id'])
        replies = [m for m in self.service.snapshot()['messages'] if m['role'] == 'assistant' and m['kind'] == 'text']
        self.assertEqual({m['sender_bot_id'] for m in replies}, {self.b['id'], c['id']})
        self.assertTrue(all(m['bot_id'] == self.a['id'] for m in replies))
        self.assertEqual({call[1]['id'] for call in self.provider.starts}, {self.b['id'], c['id']})

    def test_mentions_validate_all_ids_before_dispatch(self):
        self.write('PATCH', f'/v1/bots/{self.b["id"]}', {'archived': True})
        for recipients in ([self.a['id'], self.b['id']], [str(uuid4())], 'Lin'):
            with self.assertRaises(ServiceError):
                self.send(self.a, recipient_bot_ids=recipients)
        self.assertEqual(self.service.snapshot()['tasks'], [])


    def test_recipient_gets_only_explicit_content_and_its_own_memory(self):
        private = self.send(self.a, 'Private conversation text')
        self.service._run_task(private['id'])
        upload = self.write('POST', '/v1/attachments', {'bot_id': self.a['id'], 'filename': 'shared.txt', 'content_base64': base64.b64encode(b'Explicit shared attachment').decode()})
        task = self.send(self.a, 'Read the explicitly shared note', recipient_bot_ids=[self.b['id']], attachment_ids=[upload['id']])
        self.service._run_task(task['id'])
        provider_task, bot, history = self.provider.starts[-1]
        self.assertEqual(bot['id'], self.b['id'])
        self.assertNotIn('memory', bot)
        self.assertNotIn('Private conversation text', str(history))
        self.assertNotIn('A private instruction', str(provider_task))
        self.assertNotIn('A private memory', str(provider_task))
        self.assertTrue(all(set(peer) == {'id', 'name', 'role'} for peer in provider_task['_available_bots']))
        self.assertEqual(base64.b64decode(provider_task['attachments'][0]['content_base64']), b'Explicit shared attachment')

    def test_reply_mentions_keep_existing_thread(self):
        task = self.send(self.a)
        self.service._run_task(task['id'])
        reply = self.write('POST', f'/v1/tasks/{task["id"]}/replies', {'text': 'Ask Lin about this', 'recipient_bot_ids': [self.b['id']]})
        self.assertEqual(reply['thread_id'], task['thread_id'])
        self.assertEqual(reply['bot_id'], self.b['id'])
        self.assertEqual(reply['conversation_bot_id'], self.a['id'])
        self.assertEqual(reply['reply_status'], 'queued_recipients')

    def test_automatic_messages_do_not_join_peer_exchanges_or_mentioned_tasks(self):
        mentioned = self.write('POST', f'/v1/bots/{self.a["id"]}/messages',
                               {'text': 'Ask Lin', 'mode': 'auto', 'recipient_bot_ids': [self.b['id']]})
        with self.service.transaction():
            task = self.service._get('tasks', mentioned['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        own = self.write('POST', f'/v1/bots/{self.a["id"]}/messages', {'text': 'Ask Ada', 'mode': 'auto'})
        self.assertEqual(own['bot_id'], self.a['id'])
        self.assertNotEqual(own['id'], mentioned['id'])
        with self.service.transaction():
            task['source_bot_id'] = self.a['id']
            self.service._put('tasks', task)
        direct = self.write('POST', f'/v1/bots/{self.b["id"]}/messages', {'text': 'Direct request to Lin', 'mode': 'auto'})
        self.assertEqual(direct['bot_id'], self.b['id'])
        self.assertNotEqual(direct['id'], mentioned['id'])
        self.assertFalse(self.service._get('tasks', mentioned['id']).get('_steering'))

    def test_steering_requires_exact_task_in_origin_conversation(self):
        task = self.send(self.a, recipient_bot_ids=[self.b['id']])
        self.working(task)
        with self.assertRaises(ServiceError):
            self.write('POST', f'/v1/bots/{self.b["id"]}/messages', {'text': 'Wrong conversation', 'mode': 'steer', 'task_id': task['id']})
        response = self.write('POST', f'/v1/bots/{self.a["id"]}/messages', {'text': 'Use this scope', 'mode': 'steer', 'task_id': task['id']})
        self.assertEqual(response['id'], task['id'])
        self.assertEqual(response['bot_id'], self.b['id'])

    def test_a_to_b_to_a_routing_and_restart_dedupe(self):
        root = self.working(self.send(self.a))
        first = self.deliver(root, self.b, 'Read this request')
        child = self.working(self.service._get('tasks', first['task_id']))
        second = self.deliver(child, self.a, 'The answer is 323.')
        returned = self.service._get('tasks', second['task_id'])
        self.assertTrue(returned['_is_return'])
        self.assertEqual(returned['bot_id'], self.a['id'])
        self.assertEqual(returned['source_bot_id'], self.b['id'])
        self.assertEqual(returned['conversation_bot_id'], self.a['id'])
        self.assertEqual(returned['thread_id'], root['thread_id'])
        messages = self.service.snapshot()['messages']
        outbound = next(m for m in messages if m['text'] == 'Read this request')
        self.assertEqual(outbound['sender_bot_id'], self.a['id'])
        self.assertEqual(outbound['recipient_bot_ids'], [self.b['id']])
        self.assertEqual(outbound['bot_id'], self.a['id'])
        self.restart()
        retry = self.deliver(root, self.b, 'Read this request')
        self.assertEqual(retry['status'], 'duplicate')
        self.assertEqual(retry['task_id'], first['task_id'])
        self.assertEqual(len(self.service.snapshot()['tasks']), 3)
        self.assertEqual(len([m for m in self.service.snapshot()['messages'] if m['text'] == 'Read this request']), 1)
        self.service._run_task(root['id'])
        self.assertEqual(self.provider.starts[-1][0]['_outgoing_bot_messages'][0]['task_id'], first['task_id'])

    def test_completion_returns_once_after_restart_without_duplicate_transcript(self):
        root = self.working(self.send(self.a))
        first = self.deliver(root, self.b)
        self.service._run_task(first['task_id'])
        before = len(self.service.snapshot()['messages'])
        self.restart()
        self.service._drain_returns()
        self.service._drain_returns()
        state = self.service.snapshot()
        self.assertEqual(len(state['tasks']), 3)
        self.assertEqual(len(state['messages']), before)
        returned = next(t for t in self.service._all('tasks') if t.get('_is_return'))
        self.assertIn('independent task is done', returned['_bot_messages'][0]['text'])
        self.service._run_task(returned['id'])
        self.service._drain_returns()
        self.assertEqual(len(self.service.snapshot()['tasks']), 3)

    def test_reply_to_a_working_child_keeps_the_origin_and_attachment_binding(self):
        root = self.working(self.send(self.a))
        child = self.deliver(root, self.b)
        attachment = self.write('POST', '/v1/attachments', {'bot_id': self.a['id'], 'filename': 'note.txt',
                                'content_base64': base64.b64encode(b'Use the blue label.').decode()})
        turns = []

        def start(task, bot, history, emit):
            turns.append(task)
            if len(turns) == 1:
                self.write('POST', f'/v1/tasks/{task["id"]}/replies',
                           {'text': 'Read the attached note.', 'attachment_ids': [attachment['id']]})
            emit('completed', {'summary': 'Done.'})

        self.provider.start_task = start
        self.service._run_task(child['task_id'])
        self.assertEqual(self.service._get('tasks', child['task_id'])['status'], 'queued')
        self.service._run_task(child['task_id'])
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[1]['conversation_bot_id'], self.a['id'])
        self.assertEqual(turns[1]['bot_id'], self.b['id'])
        self.assertEqual([a['id'] for a in turns[1]['attachments']], [attachment['id']])
        self.assertEqual([s['text'] for s in turns[1]['_bot_creation_sources']], ['Read the attached note.'])
        message = next(m for m in self.service.snapshot()['messages'] if m['text'] == 'Read the attached note.')
        self.assertEqual(message['recipient_bot_ids'], [self.b['id']])

    def check_archived_return_waits_for_restoration(self, archive_origin):
        third = self.write('POST', '/v1/bots', {'name': 'Third'})
        root = self.working(self.send(self.a))
        middle = self.working(self.service._get('tasks', self.deliver(root, self.b)['task_id']))
        child = self.deliver(middle, third)
        self.service._run_task(child['task_id'])
        archived = self.a if archive_origin else self.b
        self.write('PATCH', f'/v1/bots/{archived["id"]}', {'archived': True})
        self.service._drain_returns()
        self.restart()
        self.service._drain_returns()
        self.assertIsNotNone(self.service._get('tasks', child['task_id']).get('_return_pending'))
        self.assertEqual(len(self.service._all('tasks')), 3)
        self.write('PATCH', f'/v1/bots/{archived["id"]}', {'archived': False})
        self.service._drain_returns()
        self.service._drain_returns()
        returned = [t for t in self.service._all('tasks') if t.get('_is_return')]
        self.assertEqual(len(returned), 1)
        self.assertEqual(returned[0]['bot_id'], self.b['id'])
        self.assertIsNone(self.service._get('tasks', child['task_id']).get('_return_pending'))

    def test_archived_recipient_keeps_a_pending_result(self):
        self.check_archived_return_waits_for_restoration(archive_origin=False)

    def test_archived_origin_keeps_a_pending_result(self):
        self.check_archived_return_waits_for_restoration(archive_origin=True)

    def test_long_delegated_results_return_completely_through_both_transports(self):
        root = self.working(self.send(self.a))
        child = self.deliver(root, self.b)
        with self.service.transaction():
            task = self.service._get('tasks', child['task_id'])
            task['_native_relay'] = {'sender_bot_id': self.a['id'], 'envelope_id': uuid4().hex}
            self.service._put('tasks', task)
        result = 'Details. ' * 15000 + 'The required final instruction is: keep the blue label.'

        def finish(task, bot, history, emit):
            emit('text_delta', {'text': result})
            emit('completed', {'summary': 'Done.'})

        self.provider.start_task = finish
        self.service._run_task(child['task_id'])
        self.restart()
        native = []
        self.provider.native_settings = lambda operation, bots, **payload: native.append((operation, payload)) or {'ok': True}
        self.service._drain_returns()
        self.service._drain_returns()
        returned = [task for task in self.service._all('tasks') if task.get('_is_return')]
        self.assertEqual(len(returned), 1)
        self.assertEqual(returned[0]['_bot_messages'][0]['text'], result)
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0][0], 'relay_reply')
        self.assertEqual(native[0][1]['text'], result)
        prompts = []
        self.provider.start_task = lambda task, bot, history, emit: prompts.append(
            conversation_input(dict(operation='start', task=task, history=history))[0])
        self.service._run_task(returned[0]['id'])
        self.assertIn(result, prompts[0])

    def test_task_artifact_return_contains_result_not_title(self):
        root = self.working(self.send(self.a))
        child = self.deliver(root, self.b)
        def report(task, bot, history, emit):
            emit('artifact', {'kind': 'task', 'title': 'Calculate 17 times 19', 'preview': '17 times 19 is 323.'})
            emit('completed', {'summary': 'Ready to review.'})
        self.provider.start_task = report
        self.service._run_task(child['task_id'])
        self.service._drain_returns()
        returned = next(t for t in self.service._all('tasks') if t.get('_is_return'))
        self.assertEqual(returned['_bot_messages'][0]['text'], '17 times 19 is 323.')

    def test_failed_child_returns_a_failure(self):
        root = self.working(self.send(self.a))
        child = self.deliver(root, self.b)
        self.provider.start_task = lambda task, bot, history, emit: emit('failed', {'summary': 'The source is unavailable.'})
        self.service._run_task(child['task_id'])
        self.service._drain_returns()
        returned = next(t for t in self.service._all('tasks') if t.get('_is_return'))
        self.assertEqual(returned['_bot_messages'][0]['result_status'], 'failed')
        self.assertTrue(returned['_bot_messages'][0]['text'].startswith('Task failed:'))
        self.assertEqual(self.service._get('tasks', child['task_id'])['status'], 'failed')

    def assert_uncertain_child_returns(self, failure):
        root = self.working(self.send(self.a))
        child = self.deliver(root, self.b)
        task = self.working(self.service._get('tasks', child['task_id']))
        with self.service.transaction():
            task['_native_relay'] = {'sender_bot_id': self.a['id'], 'envelope_id': uuid4().hex}
            self.service._put('tasks', task)
        if failure == 'file':
            with self.assertRaises(YieldForDecision):
                self.service.emit(task['id'], 'decision', {'title': 'Save?', 'action_type': 'create_file',
                    'action': {'type': 'create_file', 'filename': 'report.md', 'content': 'Expected'}, 'predicted_choice': 'approve'})
            decision = next(d for d in self.service._all('decisions') if d['task_id'] == task['id'])
            artifact_id = str(uuid4())
            (self.service.files / artifact_id).write_text('Different content')
            with self.service.transaction():
                self.service.db.execute('INSERT INTO receipts VALUES (?,?,?,?,?)',
                    (decision['id'], 'executing', artifact_id, digest('Expected'), 'crash'))
        else:
            call = self.service.emit(task['id'], 'native_call', {'name': 'terminal', 'arguments': {'command': 'fixture'}})
            if failure == 'dispatch':
                with self.service.transaction():
                    task = self.service._get('tasks', task['id'])
                    task['status'] = 'queued'
                    self.service._put('tasks', task)
                self.service._run_task(task['id'])
        self.restart()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'failed')
        self.service._run_task(task['id'])
        self.assertEqual(self.provider.starts, [])
        if failure != 'file':
            self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
        replies = []
        self.provider.native_settings = lambda operation, bots, **payload: replies.append((operation, payload)) or {'ok': True}
        self.service._drain_returns()
        self.service._drain_returns()
        returns = [t for t in self.service._all('tasks') if t.get('_is_return')]
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]['_bot_messages'][0]['result_status'], 'failed')
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0][1]['failed'])
        self.assertEqual(replies[0][1]['text'], returns[0]['_bot_messages'][0]['text'])
        self.restart()
        self.provider.native_settings = lambda *args, **kwargs: self.fail('The failure must return only once.')
        self.service._drain_returns()
        self.assertEqual(len([t for t in self.service._all('tasks') if t.get('_is_return')]), 1)

    def test_uncertain_child_returns_a_failure_after_restart(self):
        self.assert_uncertain_child_returns('restart')

    def test_uncertain_child_returns_a_failure_before_dispatch(self):
        self.assert_uncertain_child_returns('dispatch')

    def test_uncertain_file_returns_a_failure_after_restart(self):
        self.assert_uncertain_child_returns('file')

    def test_many_deliveries_survive_restart_without_a_user_reply(self):
        current = self.working(self.send(self.a))
        exchange_id = current['exchange_id']
        for index in range(20):
            if index == 10:
                self.restart()
                current = self.working(self.service._get('tasks', current['id']))
            target = self.b if current['bot_id'] == self.a['id'] else self.a
            receipt = self.deliver(current, target, f'Request {index}')
            self.assertEqual(receipt['status'], 'queued')
            self.assertEqual(self.deliver(current, target, f'Request {index}')['status'], 'duplicate')
            current = self.working(self.service._get('tasks', receipt['task_id']))
            self.assertEqual(current['exchange_id'], exchange_id)
        exchange = self.service._get('exchanges', exchange_id)
        self.assertEqual(exchange['deliveries'], 20)
        self.assertFalse(exchange.get('paused'))
        self.assertFalse(any('paused after eight' in m['text'] for m in self.service.snapshot()['messages']))
        self.write('POST', f'/v1/tasks/{current["id"]}/cancel')
        self.assertEqual(self.deliver(current, target, 'Do not deliver after cancellation')['status'], 'cancelled')

    def test_cancel_cascades_and_suppresses_pending_return(self):
        root = self.working(self.send(self.a))
        child = self.deliver(root, self.b)
        self.service._run_task(child['task_id'])
        self.write('POST', f'/v1/tasks/{root["id"]}/cancel')
        self.service._drain_returns()
        self.assertEqual(len(self.service.snapshot()['tasks']), 2)
        self.assertEqual(self.service._get('tasks', root['id'])['status'], 'cancelled')
        self.assertEqual(self.service._get('tasks', child['task_id'])['status'], 'completed')
        self.assertEqual(self.deliver(root, self.b, 'Too late')['status'], 'cancelled')

    def test_cancel_also_stops_an_already_queued_return(self):
        root = self.working(self.send(self.a))
        child = self.deliver(root, self.b)
        self.service._run_task(child['task_id'])
        self.service._drain_returns()
        returned = next(t for t in self.service._all('tasks') if t.get('_is_return'))
        self.write('POST', f'/v1/tasks/{root["id"]}/cancel')
        self.assertEqual(self.service._get('tasks', returned['id'])['status'], 'cancelled')
        self.assertIn(returned['id'], self.provider.cancelled)



    def test_deleted_origin_hides_shared_decisions_and_artifacts(self):
        root = self.working(self.send(self.a))
        child = self.deliver(root, self.b, 'Write a report')
        self.service._run_task(child['task_id'])
        self.write('DELETE', f'/v1/bots/{self.a["id"]}')
        state = self.service.snapshot()
        self.assertEqual(state['tasks'], [])
        self.assertEqual(state['decisions'], [])
        self.assertEqual(state['artifacts'], [])

    def test_routine_coalesces_missed_runs_and_dedupes_restart(self):
        routine = self.routine()
        current = self.due(routine)
        self.service.tick_routines(current)
        state = self.service.snapshot()
        self.assertEqual(len(state['tasks']), 1)
        self.assertGreater(datetime.fromisoformat(state['routines'][0]['next_run_at'].replace('Z', '+00:00')), current)
        occurrence = self.service._all('routine_occurrences')[0]
        self.restart()
        self.service.tick_routines(current)
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)
        self.assertEqual(self.service._all('routine_occurrences')[0]['id'], occurrence['id'])

    def test_routine_occurrence_and_task_commit_atomically(self):
        routine = self.routine()
        current = self.due(routine)
        original = self.service._event
        def fail(event_type, *args):
            if event_type == 'routine.triggered':
                raise RuntimeError('Simulated failure before commit')
            return original(event_type, *args)
        with patch.object(self.service, '_event', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.service.tick_routines(current)
        self.assertEqual(self.service.snapshot()['tasks'], [])
        self.assertEqual(self.service._all('routine_occurrences'), [])
        self.service.tick_routines(current)
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)

    def test_routine_waits_for_active_descendant(self):
        routine = self.routine()
        current = self.due(routine)
        self.service.tick_routines(current)
        root = self.working(self.service.snapshot()['tasks'][0])
        child = self.deliver(root, self.b, 'Write a report')
        self.service.emit(root['id'], 'completed', {'summary': 'Sent the report request.'})
        self.service._run_task(child['task_id'])
        self.service.tick_routines(current + timedelta(minutes=5))
        self.assertEqual(len(self.service._all('routine_occurrences')), 1)
        existing = self.write('POST', f'/v1/routines/{routine["id"]}/run')
        self.assertEqual(existing['id'], child['task_id'])

    def test_routine_waits_for_durable_return_before_next_occurrence(self):
        routine = self.routine()
        current = self.due(routine)
        self.service.tick_routines(current)
        root = self.working(self.service.snapshot()['tasks'][0])
        child = self.deliver(root, self.b)
        self.service.emit(root['id'], 'completed', {'summary': 'Sent the check.'})
        self.service._run_task(child['task_id'])
        self.assertTrue(self.service._get('tasks', child['task_id']).get('_return_pending'))
        self.service.tick_routines(current + timedelta(minutes=5))
        self.assertEqual(len(self.service._all('routine_occurrences')), 1)
        self.service._drain_returns()
        self.service.tick_routines(current + timedelta(minutes=5))
        self.assertEqual(len(self.service._all('routine_occurrences')), 1)

    def test_routines_honor_pause_archive_disable_and_explicit_run(self):
        routine = self.routine()
        current = self.due(routine)
        self.write('PATCH', f'/v1/bots/{self.a["id"]}', {'archived': True})
        self.service.tick_routines(current)
        self.assertEqual(self.service.snapshot()['tasks'], [])
        self.write('PATCH', f'/v1/bots/{self.a["id"]}', {'archived': False})
        disabled = self.write('PATCH', f'/v1/routines/{routine["id"]}', {'enabled': False})
        self.assertIsNone(disabled['next_run_at'])
        self.service.tick_routines(current)
        self.assertEqual(self.service.snapshot()['tasks'], [])
        manual = self.write('POST', f'/v1/routines/{routine["id"]}/run')
        repeated = self.write('POST', f'/v1/routines/{routine["id"]}/run')
        self.assertEqual(manual['id'], repeated['id'])
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)
        self.write('DELETE', f'/v1/routines/{routine["id"]}')
        self.assertEqual(self.service.snapshot()['routines'], [])

    def test_routine_validation_and_schedule_edit(self):
        with self.assertRaises(ServiceError):
            self.routine(cron='not a cron')
        with self.assertRaises(ServiceError):
            self.routine(timezone='Unknown/Zone')
        routine = self.routine(enabled=False)
        updated = self.write('PATCH', f'/v1/routines/{routine["id"]}', {'cron': '0 9 * * 1-5', 'timezone': 'America/New_York', 'enabled': True, 'name': 'Weekday check'})
        self.assertEqual(updated['id'], routine['id'])
        self.assertTrue(updated['next_run_at'])
        self.assertEqual(updated['name'], 'Weekday check')


if __name__ == '__main__':
    unittest.main()
