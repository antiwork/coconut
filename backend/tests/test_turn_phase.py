import unittest

from coconut.gateway_bridge import TurnEvents
from coconut.turn_phase import PHASES, tool_phase


class ToolPhaseTests(unittest.TestCase):
    def test_each_tool_names_its_phase_and_what_it_does(self):
        self.assertEqual(tool_phase('terminal', {'command': 'git status --short'}), ('command', 'git status --short'))
        self.assertEqual(tool_phase('terminal', {'command': 'python -m pytest backend/tests'})[0], 'testing')
        self.assertEqual(tool_phase('terminal', {'command': 'gh pr view 12'})[0], 'github')
        self.assertEqual(tool_phase('read_file', {'path': '/Users/x/project/app/Models.swift'}), ('reading', 'Models.swift'))
        self.assertEqual(tool_phase('patch', {'path': 'a/b/c.py'}), ('editing', 'c.py'))
        self.assertEqual(tool_phase('search_files', {'pattern': 'unread'}), ('searching', 'unread'))
        self.assertEqual(tool_phase('web_search', {'query': 'swift timeline'}), ('browsing', 'swift timeline'))
        self.assertEqual(tool_phase('web_extract', {'urls': ['https://example.com/a/b']}), ('browsing', 'example.com'))
        self.assertEqual(tool_phase('delegate_task', {}), ('helpers', ''))
        self.assertEqual(tool_phase('coconut_save_document', {}), ('working', 'coconut save document'))

    def test_the_detail_is_one_short_line(self):
        phase, detail = tool_phase('terminal', {'command': 'echo ' + 'x' * 400 + '\nsecond line'})
        self.assertEqual(phase, 'command')
        self.assertLessEqual(len(detail), 120)
        self.assertNotIn('second line', detail)

    def test_every_phase_is_one_the_app_knows(self):
        for tool in ('terminal', 'read_file', 'patch', 'search_files', 'web_search', 'memory', 'delegate_task', 'x'):
            self.assertIn(tool_phase(tool, {})[0], PHASES)

    def test_a_tool_event_reports_its_phase_with_the_detail(self):
        emitted = []
        events = TurnEvents(lambda kind, payload: emitted.append((kind, payload)))
        events.apply({'kind': 'typing'})
        events.apply({'kind': 'tool', 'tool': 'terminal', 'args': {'command': 'ls'}})
        events.apply({'kind': 'tool', 'tool': 'terminal', 'args': {'command': 'ls'}})
        events.apply({'kind': 'tool', 'tool': 'read_file', 'args': {'path': 'a/b.txt'}})
        self.assertEqual(emitted, [('phase', {'phase': 'thinking'}),
                                   ('phase', {'phase': 'command', 'detail': 'ls'}),
                                   ('phase', {'phase': 'reading', 'detail': 'b.txt'})])


if __name__ == '__main__':
    unittest.main()
