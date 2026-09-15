"""Exercise queue admission and concurrent native turn routing."""
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service


class BlockingProvider(FixtureProvider):
    def __init__(self, root):
        super().__init__(root)
        self.started = []
        self.release = threading.Event()

    def start_task(self, task, bot, history, emit):
        self.started.append(task)
        self.release.wait(5)
        emit('completed', {'summary': 'Complete.'})


class ConcurrentWorkTests(unittest.TestCase):
    def test_same_bot_uses_two_slots_and_preserves_capacity_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            provider = BlockingProvider(root)
            service = Service(root, provider, start_workers=False)
            def write(method, path, data, key=None):
                return service.mutate(method, path, data, key or str(uuid4()))
            try:
                key = str(uuid4())
                saved = write('PUT', '/v1/settings/work-capacity', {'limit': 2}, key)
                self.assertEqual(write('PUT', '/v1/settings/work-capacity', {'limit': 2}, key), saved)
                bot = write('POST', '/v1/bots', {'name': 'Dev'})
                tasks = [write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': f'Independent task {i}', 'mode': 'queue'}) for i in range(3)]
                service._dispatch_pending()
                deadline = time.monotonic() + 2
                while len(provider.started) < 2 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual({t['id'] for t in provider.started}, {t['id'] for t in tasks[:2]})
                self.assertEqual(len({t['_execution_session_id'] for t in provider.started}), 2)
                write('PUT', '/v1/settings/work-capacity', {'limit': 1})
                service._dispatch_pending()
                self.assertEqual(len(provider.started), 2)
                self.assertEqual(service._get('tasks', tasks[2]['id'])['status'], 'queued')
                provider.release.set()
                for thread in list(service.running.values()):
                    thread.join(2)
                service._dispatch_pending()
                for thread in list(service.running.values()):
                    thread.join(2)
                self.assertEqual(len(provider.started), 3)
            finally:
                provider.release.set()
                service.close()
            service = Service(root, BlockingProvider(root), start_workers=False)
            try:
                self.assertEqual(service.snapshot()['work_capacity']['effective_limit'], 1)
                self.assertTrue(all(t.get('_execution_session_id') for t in service._all('tasks')))
            finally:
                service.close()

    def test_same_execution_waits_while_another_bot_uses_the_free_slot(self):
        with tempfile.TemporaryDirectory() as root:
            provider = BlockingProvider(root)
            service = Service(root, provider, start_workers=False)
            def write(method, path, data):
                return service.mutate(method, path, data, str(uuid4()))
            try:
                write('PUT', '/v1/settings/work-capacity', {'limit': 2})
                bots = [write('POST', '/v1/bots', {'name': name}) for name in ('Desk', 'Dev')]
                tasks = [write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Check work', 'mode': 'queue'})
                         for bot in (bots[0], bots[0], bots[1])]
                for task in tasks[:2]:
                    saved = service._get('tasks', task['id'])
                    saved['_execution_session_id'] = tasks[0]['id']
                    service._put('tasks', saved)
                positions = service._queue_positions({t['id']: t for t in service._all('tasks')}, time.time())
                self.assertEqual(positions, {task['id']: i + 1 for i, task in enumerate(tasks)})
                service._dispatch_pending()
                deadline = time.monotonic() + 2
                while len(provider.started) < 2 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual({t['id'] for t in provider.started}, {tasks[0]['id'], tasks[2]['id']})
                self.assertEqual(service._get('tasks', tasks[1]['id'])['status'], 'queued')
                provider.release.set()
                for thread in list(service.running.values()): thread.join(2)
                service._dispatch_pending()
                for thread in list(service.running.values()): thread.join(2)
                self.assertEqual(len(provider.started), 3)
            finally:
                provider.release.set()
                service.close()


class TurnCleanupTests(unittest.TestCase):
    import test_provider as fixtures
    setUp = fixtures.ProviderTests.setUp
    tearDown = fixtures.ProviderTests.tearDown
    fake_hermes = fixtures.ProviderTests.fake_hermes

    def test_cleanup_failure_releases_the_channel_without_repeating_the_result(self):
        import os
        from unittest.mock import patch
        provider = self.fake_hermes("def broken_cleanup(): raise RuntimeError('PRIVATE_CLEANUP_ERROR')\nself.release_clients = broken_cleanup\nreturn {'completed': True, 'final_response': 'Finished'}\n")
        events = []
        errors = []
        def run():
            try:
                with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
                    provider.start_task(self.task, self.bot, [], lambda kind, data: events.append((kind, data)))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(3)
        try:
            self.assertFalse(thread.is_alive(), 'The queue slot stayed occupied after cleanup failed.')
            self.assertFalse(errors)
            self.assertEqual([kind for kind, _ in events if kind in ('completed', 'failed')], ['completed'])
            self.assertNotIn('PRIVATE_CLEANUP_ERROR', str(events))
        finally:
            provider.close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
