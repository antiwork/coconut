import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service


class RecordingProvider(FixtureProvider):
    def __init__(self, root):
        super().__init__(root)
        self.started = []

    def start_task(self, task, bot, history, emit):
        self.started.append(task['id'])
        emit('completed', {'summary': 'Finished.'})


class DispatchRecoveryTests(unittest.TestCase):
    def test_failed_maintenance_or_return_delivery_does_not_stop_work(self):
        for operation in ('tick_routines', '_drain_returns'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as root:
                provider = RecordingProvider(root)
                service = Service(root, provider, start_workers=False)
                try:
                    bot = service.mutate('POST', '/v1/bots', {'name': 'Dev', 'role': 'Development'}, str(uuid4()))
                    task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Finish the approved work'}, str(uuid4()))
                    original = getattr(service, operation)
                    calls = []

                    def fail_once():
                        calls.append(True)
                        if len(calls) == 1:
                            raise sqlite3.DatabaseError('another row available')
                        return original()

                    with patch.object(service, operation, fail_once), self.assertLogs('coconut.service', level='ERROR'):
                        service.start()
                        deadline = time.monotonic() + 5
                        with service.changed:
                            while service._get('tasks', task['id'])['status'] != 'completed' and time.monotonic() < deadline:
                                service.changed.wait(.1)
                        self.assertEqual(service._get('tasks', task['id'])['status'], 'completed')
                        self.assertTrue(service.dispatcher.is_alive())
                        self.assertEqual(provider.started, [task['id']])
                finally:
                    service.close()

    def test_readers_wait_for_transaction_rollback(self):
        readers = {
            'list': lambda s, bot: s._all('bots')[0]['name'],
            'get': lambda s, bot: s._get('bots', bot['id'])['name'],
            'setting': lambda s, bot: s._setting('probe'),
            'events': lambda s, bot: s._cursor(),
            'receipt': lambda s, bot: s._has_uncertain_receipt('task', 'turn'),
        }
        for name, read in readers.items():
            with self.subTest(reader=name), tempfile.TemporaryDirectory() as root:
                service = Service(root, RecordingProvider(root), start_workers=False)
                thread = None
                try:
                    bot = service.mutate('POST', '/v1/bots', {'name': 'Dev', 'role': 'Development'}, str(uuid4()))
                    before = read(service, bot)
                    entered, finished = threading.Event(), threading.Event()
                    results = []

                    def observe():
                        entered.set()
                        results.append(read(service, bot))
                        finished.set()

                    class Rollback(Exception):
                        pass

                    with self.assertRaises(Rollback):
                        with service.transaction():
                            changed = service._get('bots', bot['id'])
                            changed['name'] = 'Uncommitted'
                            service._put('bots', changed)
                            service.db.execute("INSERT INTO settings VALUES ('probe','true')")
                            service._event('probe', {})
                            service._put('native_receipts', {'id': 'probe', 'state': 'executing', 'task_id': 'task', 'turn_id': 'turn'})
                            thread = threading.Thread(target=observe)
                            thread.start()
                            self.assertTrue(entered.wait(1))
                            self.assertFalse(finished.wait(.05), 'A reader observed another thread\'s open transaction.')
                            raise Rollback()
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(results, [before])
                finally:
                    if thread:
                        thread.join(1)
                    service.close()


if __name__ == '__main__':
    unittest.main()
