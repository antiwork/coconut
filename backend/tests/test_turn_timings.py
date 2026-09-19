import json
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from coconut.turn_timings import TurnTrace, native_request_timings


class RequestTimingTests(unittest.TestCase):
    def setUp(self):
        self.hooks = {}
        hooks = self.hooks
        class Handle:
            def __init__(self, name, callback):
                self.name, self.callback = name, callback
            def dispose(self):
                hooks[self.name].remove(self.callback)
        class Context:
            def __init__(self, *args):
                pass
            def register_hook(self, name, callback):
                hooks.setdefault(name, []).append(callback)
                return Handle(name, callback)
        module = ModuleType('hermes_cli.plugins')
        module.PluginContext = Context
        module.PluginManifest = lambda **kwargs: kwargs
        module.get_plugin_manager = lambda: None
        mocked = patch.dict('sys.modules', {'hermes_cli.plugins': module})
        mocked.start()
        self.addCleanup(mocked.stop)
        self.events = []
        self.trace = TurnTrace({'id': 'own', '_native_turn_id': 'turn'}, 'start',
                               lambda kind, data: self.events.append(data))

    def invoke(self, hook, **payload):
        for callback in list(self.hooks.get(hook, [])):
            callback(**payload)

    def test_counts_only_and_repeated_requests_remain_correlated(self):
        with native_request_timings(self.trace, SimpleNamespace(session_id='session')):
            for count in (1, 2):
                self.invoke('pre_api_request', task_id='own', session_id='session', api_call_count=count,
                            tool_count=3, user_message='PRIVATE', api_key='PRIVATE', request={'input': 'PRIVATE'})
                self.invoke('post_api_request', task_id='own', session_id='session', api_call_count=count,
                            api_duration=1.2, started_at=100, first_chunk_at=100.4,
                            assistant_message='PRIVATE', usage={'input_tokens': 10, 'cache_read_tokens': 20,
                            'raw_usage': {'token': 'PRIVATE'}, 'output_tokens': 'PRIVATE'})
        self.assertEqual(len(self.events), 4)
        self.assertNotIn('PRIVATE', json.dumps(self.events))
        self.assertEqual([e['api_call_count'] for e in self.events], [1, 1, 2, 2])
        self.assertTrue(all(e['task_id'] == 'own' and e['turn_id'] == 'turn' for e in self.events))
        end = self.events[-1]
        self.assertEqual((end['duration_ms'], end['first_chunk_ms']), (1200, 400))
        self.assertEqual((end['input_tokens'], end['cache_read_tokens']), (10, 20))
        self.assertTrue(all(not callbacks for callbacks in self.hooks.values()))

    def test_other_sessions_and_tasks_do_not_leak_into_turn(self):
        with native_request_timings(self.trace, SimpleNamespace(session_id='session')):
            self.invoke('post_api_request', task_id='other', session_id='session', api_duration=5)
            self.invoke('post_api_request', task_id='own', session_id='other', api_duration=5)
        self.assertEqual(self.events, [])

    def test_failed_turn_removes_observers(self):
        with self.assertRaises(ValueError):
            with native_request_timings(self.trace, SimpleNamespace(session_id='session')):
                self.invoke('api_request_error', task_id='own', session_id='session', retry_count=2,
                            error_message='PRIVATE', request={'authorization': 'PRIVATE'})
                raise ValueError('Turn failed')
        self.assertEqual(self.events[0]['retry_count'], 2)
        self.assertNotIn('PRIVATE', json.dumps(self.events))
        self.invoke('api_request_error', task_id='own', session_id='session')
        self.assertEqual(len(self.events), 1)
        self.assertTrue(all(not callbacks for callbacks in self.hooks.values()))
