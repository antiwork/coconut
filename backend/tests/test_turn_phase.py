import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service
from coconut.turn_phase import tool_phase
from test_service import ControlledProvider


class ToolPhaseTests(unittest.TestCase):
    def test_native_tools_map_to_short_phases(self):
        self.assertEqual(tool_phase('read_file', {'path': 'a.py'}), 'reading')
        self.assertEqual(tool_phase('patch', {}), 'editing')
        self.assertEqual(tool_phase('search_files', {}), 'searching')
        self.assertEqual(tool_phase('web_search', {}), 'browsing')
        self.assertEqual(tool_phase('browser_navigate', {}), 'browsing')
        self.assertEqual(tool_phase('session_search', {}), 'memory')
        self.assertEqual(tool_phase('delegate_task', {}), 'helpers')
        self.assertEqual(tool_phase('mcp_github_list_pulls', {}), 'github')
        self.assertEqual(tool_phase('skill_view', {}), 'working')

    def test_commands_use_their_executable(self):
        self.assertEqual(tool_phase('terminal', {'command': 'gh pr view 12'}), 'github')
        self.assertEqual(tool_phase('terminal', {'command': 'python3 -m pytest tests'}), 'testing')
        self.assertEqual(tool_phase('terminal', {'command': 'ls -la'}), 'command')
        self.assertEqual(tool_phase('terminal', {'command': 'echo "unclosed'}), 'command')
        self.assertEqual(tool_phase('terminal', {}), 'command')

    def test_bookkeeping_keeps_the_previous_phase(self):
        self.assertIsNone(tool_phase('todo', {}))
        self.assertIsNone(tool_phase('coconut_send_message', {}))
        self.assertIsNone(tool_phase(None, {}))


class RecordedPhaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.device = self.service.pair({'code': self.service.pairing_code()})['device_id']
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Ada'}, str(uuid4()), self.device)
        self.task = self.service.mutate('POST', f'/v1/bots/{self.bot["id"]}/messages',
                                        dict(text='Check the test', mode='queue'), str(uuid4()), self.device)
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def stored(self):
        with self.service.transaction():
            return self.service._get('tasks', self.task['id'])

    def public(self):
        return next(task for task in self.service.snapshot()['tasks'] if task['id'] == self.task['id'])

    def test_working_task_publishes_its_phase_and_keeps_the_turn_start(self):
        self.service.emit(self.task['id'], 'phase', {'phase': 'thinking'})
        started = self.stored()['turn_started_at']
        self.service.emit(self.task['id'], 'phase', {'phase': 'testing'})
        self.assertEqual(self.public()['phase'], 'testing')
        self.assertEqual(self.public()['turn_started_at'], started)

    def test_unknown_phase_is_ignored(self):
        self.service.emit(self.task['id'], 'phase', {'phase': 'rm -rf /'})
        self.assertNotIn('phase', self.stored())

    def test_phase_ends_with_the_turn(self):
        self.service.emit(self.task['id'], 'phase', {'phase': 'reading'})
        self.service.emit(self.task['id'], 'text_delta', {'text': 'Done.'})
        self.service.emit(self.task['id'], 'completed', {'summary': 'Complete.'})
        self.assertNotIn('phase', self.stored())
        self.assertNotIn('turn_started_at', self.stored())

    def test_finished_task_does_not_take_a_late_phase(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'completed'
            self.service._put('tasks', task)
        self.service.emit(self.task['id'], 'phase', {'phase': 'writing'})
        self.assertNotIn('phase', self.stored())


class BridgePhaseTests(unittest.TestCase):
    def bridge(self):
        from coconut.native_bridge import NativeBridge
        sent = []
        bridge = NativeBridge({'operation': 'start', 'task': {'id': str(uuid4())}},
                              lambda kind, data: sent.append(data['phase']), None)
        bridge.PHASE_INTERVAL = 0.05
        return bridge, sent

    def settle(self, bridge):
        timer = bridge.phase_timer
        if timer is not None:
            timer.join()

    def test_parallel_tools_report_thinking_after_the_last_one_returns(self):
        bridge, sent = self.bridge()
        bridge.PHASE_INTERVAL = 0
        bridge.tool_phase_started('reading')
        bridge.tool_phase_started('testing')
        bridge.tool_phase_finished()
        self.assertEqual(sent, ['reading', 'testing'])
        bridge.tool_phase_finished()
        self.assertEqual(sent, ['reading', 'testing', 'thinking'])

    def test_fast_changes_send_only_the_latest_phase(self):
        bridge, sent = self.bridge()
        bridge.report_phase('thinking')
        bridge.report_phase('reading')
        bridge.report_phase('testing')
        self.assertEqual(sent, ['thinking'])
        self.settle(bridge)
        self.assertEqual(sent, ['thinking', 'testing'])

    def test_a_return_to_the_sent_phase_sends_nothing(self):
        bridge, sent = self.bridge()
        bridge.report_phase('thinking')
        bridge.report_phase('reading')
        bridge.report_phase('thinking')
        self.settle(bridge)
        self.assertEqual(sent, ['thinking'])


class WorkerPhaseTests(unittest.TestCase):
    def test_worker_reports_each_phase_change_once(self):
        from test_provider import ProviderTests
        fixture = ProviderTests()
        fixture.setUp()
        try:
            provider = fixture.fake_hermes(
                "import json\n"
                "for _ in range(2):\n"
                "    registry.dispatch('coconut_track_pr', {'url': 'https://github.com/example/project/pull/42'})\n"
                "return {'completed': True, 'final_response': 'Tracked.'}\n")
            phases = []
            def emit(kind, data):
                if kind == 'phase':
                    phases.append(data['phase'])
                return {'id': 'saved-pr', 'status': 'checks_running'} if kind == 'artifact' else None
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
                provider.start_task(fixture.task, fixture.bot, [], emit)
            # Calls inside one second share an update, so the fast fixture shows only the first phase.
            self.assertEqual(phases, ['thinking'])
        finally:
            fixture.tearDown()
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
