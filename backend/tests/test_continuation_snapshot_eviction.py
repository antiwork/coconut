"""Continuation admission must survive replacement of the scheduler's cache."""
import copy
import json
import os
import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coconut.kanban_scheduler import KanbanScheduler
from coconut.service import ServiceError
import test_native_work_continuation as fixtures
import test_provider


class ContinuationSnapshotEvictionTests(unittest.TestCase):
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    completed_pr_peer = fixtures.NativeWorkContinuationTests.completed_pr_peer
    resume = fixtures.NativeWorkContinuationTests.resume

    def admission(self, *, provider=False, change=None, context=False, cached=False, periodic=None):
        owner, source, peer, items = self.completed_pr_peer()
        peer['_kanban_claim'] = dict(native_id='t_peer', input='peer-input')
        self.service._put('tasks', peer)
        snapshots = {owner['id']: dict(self.service.kanban.tasks[owner['id']]),
                     peer['id']: dict(id='t_peer', status='done')}
        target = 't_existing'
        if context:
            target = 't_context'
            self.service._put('contexts', dict(id='context', task_id=owner['id'],
                bot_id=owner['bot_id'], status='completed', _kanban_claim={'native_id': target}))
            snapshots['context'] = dict(id=target, status='done')
        for identity, row in snapshots.items():
            row['external_id'] = identity
        adapter = KanbanScheduler(self.provider, self.service.root)
        adapter.process = Mock()
        adapter.process.poll.return_value = None
        adapter.frames = queue.Queue()
        adapter.tasks = copy.deepcopy(snapshots) if cached else {}
        self.service.kanban = adapter
        calls, errors = [], []
        ticks = queue.Queue()
        original_call = adapter.call

        # Only the wire is synthetic. Exercise the real scheduler cache replacement,
        # adapter lock, spawn callback and service transaction on separate threads.
        def wire(line):
            request = json.loads(line)
            if request.get('op') == 'snapshot':
                calls.append(request)
                rows = [dict(snapshots[i]) for i in request['external_ids'] if i in snapshots]
                adapter.frames.put({'result': {'tasks': rows}})
            elif request.get('op') == 'tick':
                adapter.frames.put({'spawn': {}})
            elif 'pid' in request or 'error' in request:
                if 'error' in request:
                    errors.append(request['error'])
                adapter.frames.put({'result': {'tasks': periodic(owner, peer) if periodic else []}})
            else:
                raise AssertionError(request)
        adapter.process.stdin.write.side_effect = wire

        def spawn(_):
            if not self.service.lock.acquire(timeout=2):
                raise AssertionError('Adapter -> service lock deadlocked')
            try:
                if change:
                    change(owner, source, peer, snapshots)
            finally:
                self.service.lock.release()
            return 123

        def tick():
            while (done := ticks.get()) is not None:
                try:
                    original_call({'op': 'tick'}, spawn=spawn)
                except BaseException as error:
                    errors.append(error)
                finally:
                    done.set()
        worker = threading.Thread(target=tick, daemon=True)
        worker.start()

        def evict():
            done = threading.Event()
            ticks.put(done)
            self.assertTrue(done.wait(5), 'Periodic snapshot did not finish')

        def targeted(request, *args, **kwargs):
            self.assertFalse(self.service.lock._is_owned(), 'Targeted snapshot under service lock')
            result = original_call(request, *args, **kwargs)
            evict()
            return result

        all_rows = self.service._all
        def evict_during_lookup(table):
            if table == 'tasks' and not calls:
                # No spawn here: periodic cache replacement itself needs no service
                # lock, including while admission is already in a transaction.
                done = threading.Event()
                def replace():
                    original_call({'op': 'snapshot', 'external_ids': []})
                    done.set()
                replacement = threading.Thread(target=replace, daemon=True)
                replacement.start()
                self.assertTrue(done.wait(5))
                replacement.join(5)
            return all_rows(table)

        payload = dict(task_id=target, message='Keep the verified correction local.')
        try:
            with patch.object(adapter, 'call', side_effect=targeted), patch.object(
                    self.service, '_all', side_effect=evict_during_lookup if cached else all_rows):
                if provider:
                    self.root = Path(self.temp.name) / 'provider-fixture'
                    transport = test_provider.ProviderTests.fake_hermes(self,
                        "import json\n"
                        "result = json.loads(registry.dispatch('coconut_continue_work', " + repr(payload) + "))\n"
                        "return {'completed': True, 'final_response': json.dumps(result)}\n")
                    events = []
                    def emit(kind, data):
                        if kind == 'work_continue':
                            self.assertEqual(data, payload)
                            return self.service.emit(source['id'], kind, data)
                        events.append((kind, data))
                    task = {key: source[key] for key in ('id', 'bot_id', 'title')}
                    with patch.dict(os.environ, ANTHROPIC_API_KEY='test-only'):
                        transport.start_task(task, self.service._get('bots', source['bot_id']), [], emit)
                    text = ''.join(data['text'] for kind, data in events if kind == 'text_delta')
                    self.assertTrue(text.startswith('{'), events)
                    result = json.loads(text)
                else:
                    try:
                        result = self.service.emit(source['id'], 'work_continue', payload)
                    except ServiceError as error:
                        result = error
        finally:
            ticks.put(None)
            worker.join(10)
            adapter.process.poll.return_value = 0
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(adapter.tasks, {r['external_id']: r for r in periodic(owner, peer)} if periodic else {})
        return owner, source, peer, items, result, calls

    def assert_admitted(self, values):
        owner, source, peer, items, receipt, calls = values
        self.assertIsInstance(receipt, dict, str(receipt))
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        queued = self.service._get('tasks', owner['id'])
        self.assertEqual(queued['_execution_session_id'], owner['_execution_session_id'])
        self.assertEqual(queued['_kanban_claim'], owner['_kanban_claim'])
        self.assertEqual(queued['source_task_id'], owner['source_task_id'])
        self.assertEqual(queued['_work_context_id'], items[0]['id'])
        self.assertEqual(self.service._get('tasks', peer['id']), peer)
        self.assertEqual(self.service._get('artifacts', items[1]['id']), items[1])
        self.assertEqual(self.service.emit(source['id'], 'work_continue', dict(
            task_id='t_context' if 'context' in calls[-1]['external_ids'] else 't_existing',
            message='Keep the verified correction local.')), receipt)
        self.assertEqual(self.service._get('tasks', owner['id']), queued)

    def test_targeted_owner_and_peer_survive_periodic_eviction(self):
        values = self.admission()
        self.assert_admitted(values)
        self.assertEqual(len(values[-1]), 2)
        self.assertEqual(set(values[-1][-1]['external_ids']), {values[0]['id'], values[2]['id']})

    def test_provider_tool_admits_after_periodic_eviction(self):
        self.assert_admitted(self.admission(provider=True))

    def test_context_and_owner_survive_periodic_eviction(self):
        self.assert_admitted(self.admission(context=True))

    def test_cached_owner_and_peer_use_one_admission_snapshot(self):
        values = self.admission(cached=True)
        self.assert_admitted(values)
        self.assertEqual(values[-1], [{'op': 'snapshot', 'external_ids': []}])

    def test_eviction_still_revalidates_authority(self):
        def change(owner, source, peer, snapshots):
            self.service._put('tasks', dict(owner, source_bot_id='unrelated'))
        owner, _, _, _, result, _ = self.admission(change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertEqual(result.status, 403)
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'completed')

    def test_provider_receives_pending_decision_rejection(self):
        def change(owner, source, peer, snapshots):
            self.service._put('decisions', dict(id='pause', task_id=owner['id'], status='pending'))
        owner, _, _, _, result, _ = self.admission(provider=True, change=change)
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('existing decision', result['error'])
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('decisions', 'pause')['status'], 'pending')

    def test_newer_cached_peer_claim_wins_over_targeted_done(self):
        def periodic(owner, peer):
            return [dict(external_id=peer['id'], id='t_peer', status='running', current_run_id=47)]
        owner, _, _, _, result, _ = self.admission(periodic=periodic)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('another execution', str(result))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_second_targeted_snapshot_does_not_reuse_missing_owner(self):
        def change(owner, source, peer, snapshots):
            snapshots.pop(owner['id'], None)
        owner, _, _, _, result, calls = self.admission(change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('Choose one existing', str(result))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_second_targeted_snapshot_revalidates_rebound_owner(self):
        def change(owner, source, peer, snapshots):
            snapshots[owner['id']]['id'] = 't_rebound'
        owner, _, _, _, result, calls = self.admission(change=change)
        self.assertIsInstance(result, ServiceError)
        self.assertIn('mapping does not match', str(result))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)


if __name__ == '__main__':
    unittest.main()
