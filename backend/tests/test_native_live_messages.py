"""Use the installed Hermes control API without a model request or external action."""
import os
from pathlib import Path
import sys
import threading
import unittest

from coconut.live_messages import LiveMessages


@unittest.skipUnless(os.environ.get('COCONUT_HERMES_ROOT'), 'Requires the native Hermes runtime')
class NativeLiveMessagesTests(unittest.TestCase):
    def test_input_before_agent_start_uses_native_steering(self):
        sys.path.insert(0, str(Path(os.environ['COCONUT_HERMES_ROOT'])))
        from agent.interrupt_control import InterruptControlMixin
        agent = InterruptControlMixin()
        consumed = []
        live = LiveMessages(None, consumed.append)
        live.deliver([dict(id='early', text='Answer this first.')])
        live.bind(agent)
        text = agent._drain_pending_steer()
        self.assertIn('Answer this first.', text)
        live.finish(dict(completed=True, messages=[dict(role='user', content=text)]))
        self.assertEqual(consumed, [['early']])

    def test_tool_yield_and_model_redirect_consume_input_without_hard_cancellation(self):
        sys.path.insert(0, str(Path(os.environ['COCONUT_HERMES_ROOT'])))
        from agent.interrupt_control import InterruptControlMixin
        agent = InterruptControlMixin()
        agent._executing_tools = True
        consumed = []
        live = LiveMessages(agent, consumed.append)
        live.deliver([dict(id='ack', text='Thanks, stop the investigation.')])
        self.assertEqual(consumed, [])
        first = agent._drain_pending_steer()
        self.assertIn('stop the investigation', first)
        self.assertEqual(consumed, [])
        self.assertFalse(getattr(agent, '_interrupt_requested', False))
        agent._executing_tools = False
        agent._interrupt_requested = False
        agent._model_request_active = threading.Event()
        agent._model_request_active.set()
        aborted = []
        agent._active_request_abort = aborted.append
        live.deliver([dict(id='topic', text='Unrelated: what is two plus two?')])
        self.assertEqual(aborted, ['redirect_abort'])
        second = agent._drain_pending_redirect()
        self.assertIn('Unrelated:', second)
        live.finish(dict(completed=True, pending_steer=second, messages=[dict(role='user', content=first)]))
        self.assertEqual(consumed, [['ack']])
