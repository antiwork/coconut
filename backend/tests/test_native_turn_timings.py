"""Verify timing hooks against the pinned Hermes plugin implementation."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from coconut.turn_timings import TurnTrace, native_request_timings
from hermes_env import needs_hermes, SOURCE


@needs_hermes
class NativeTimingTests(unittest.TestCase):
    def test_native_hooks_deliver_scoped_events_and_dispose_after_error(self):
        sys.path.insert(0, str(Path(SOURCE)))
        from hermes_cli.plugins import get_plugin_manager
        manager = get_plugin_manager()
        hooks = ('pre_api_request', 'post_api_request', 'api_request_error')
        before = {name: list(manager.iter_hook_callbacks(name)) for name in hooks}
        events = []
        trace = TurnTrace({'id': 'timing-test', '_native_turn_id': 'turn'}, 'start',
                          lambda kind, data: events.append(data))
        with self.assertRaisesRegex(ValueError, 'test failure'):
            with native_request_timings(trace, SimpleNamespace(session_id='timing-session')):
                for hook in hooks:
                    manager.invoke_hook(hook, task_id='other', session_id='timing-session')
                    manager.invoke_hook(hook, task_id='timing-test', session_id='timing-session',
                                        api_call_count=1, api_duration=0.25, retry_count=2)
                raise ValueError('test failure')
        self.assertEqual([row['stage'] for row in events],
                         ['api_request_start', 'api_request_end', 'api_request_error'])
        self.assertTrue(all(row['turn_id'] == 'turn' for row in events))
        self.assertEqual(events[1]['duration_ms'], 250)
        self.assertEqual(events[2]['retry_count'], 2)
        for hook in hooks:
            self.assertEqual(list(manager.iter_hook_callbacks(hook)), before[hook])
            manager.invoke_hook(hook, task_id='timing-test', session_id='timing-session')
        self.assertEqual(len(events), 3)
