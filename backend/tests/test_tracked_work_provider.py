import json
import os
import unittest
from unittest.mock import patch
import test_provider as fixtures


class TrackedWorkProviderTests(unittest.TestCase):
    setUp = fixtures.ProviderTests.setUp
    tearDown = fixtures.ProviderTests.tearDown
    fake_hermes = fixtures.ProviderTests.fake_hermes

    def test_native_tool_tracks_without_starting_another_task(self):
        provider = self.fake_hermes('''
            import json
            receipt = json.loads(registry.dispatch('coconut_track_work', {
                'key': 'account-export', 'title': 'Export account data',
                'activity': 'Export saved', 'status': 'completed'}))
            assert receipt['id'] == 'tracked-export'
            return {'completed': True, 'final_response': 'Export saved.'}
        ''')
        events = []
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'artifact': return {'id': 'tracked-export'}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.start_task(self.task, self.bot, [], emit)
        artifacts = [data for kind, data in events if kind == 'artifact']
        self.assertEqual(artifacts, [dict(kind='work', key='account-export', title='Export account data',
                                        activity='Export saved', status='completed')])
        self.assertEqual(events[-1][0], 'completed')
