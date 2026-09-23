"""A new Hermes claim must have a process the prior run cannot reap."""
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coconut.providers import HermesProvider


class Process:
    next_pid = 100

    def __init__(self, *args, **kwargs):
        self.pid = Process.next_pid
        Process.next_pid += 1
        self.ended = False

    def poll(self):
        return 0 if self.ended else None


class KanbanClaimProcessTests(unittest.TestCase):
    def setUp(self):
        self.provider = HermesProvider.__new__(HermesProvider)
        provider = self.provider
        provider._lock = threading.RLock()
        provider._closed = False
        provider._stopped_bots = set()
        provider._kanban_runtimes = {}
        provider._prepared = {}
        provider._transports = {}
        provider._processes = {}
        provider._paused_processes = {}
        provider._runtime_envs = {}
        provider._runtime_bots = {}
        provider.python = '/test/python'
        provider.catalog = SimpleNamespace(root='/test/home')
        provider.data_dir = Path('/test/data')
        provider.environment = lambda bot_id: ({}, '/test/workspace')
        provider._terminate = lambda process: setattr(process, 'ended', True)

    def test_new_claim_gets_new_process_after_prior_run(self):
        provider = self.provider
        bot = {'id': 'bot'}
        with patch('coconut.providers.subprocess.Popen', Process):
            first = provider.prepare_kanban_worker('claim-1', bot, '/test/workspace', 'outcome')
            self.assertEqual(provider.prepare_kanban_worker('claim-1', bot, '/test/workspace', 'outcome'), first)
            provider.release_kanban_preparation('claim-1')
            second = provider.prepare_kanban_worker('claim-2', bot, '/test/workspace', 'outcome')
        self.assertNotEqual(first, second)
        self.assertEqual(provider._kanban_runtimes['outcome'].pid, second)

    def test_new_claim_replaces_paused_worker(self):
        provider = self.provider
        bot = {'id': 'bot'}
        with patch('coconut.providers.subprocess.Popen', Process):
            first = provider.prepare_kanban_worker('claim-1', bot, '/test/workspace', 'outcome')
            provider.release_kanban_preparation('claim-1')
            provider._paused_processes['task'] = provider._kanban_runtimes['outcome']
            second = provider.prepare_kanban_worker('claim-2', bot, '/test/workspace', 'outcome')
        self.assertNotEqual(first, second)
        self.assertFalse(provider._paused_processes)


if __name__ == '__main__':
    unittest.main()
