"""The Coconut chat: the agent of the default profile changes the settings of Coconut (coconut/coconut_chat.py)."""
import tempfile
import unittest
from uuid import uuid4

from coconut import coconut_chat
from coconut.service import Service, ServiceError
from coconut.providers import FixtureProvider


class CoconutChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, FixtureProvider(__import__('pathlib').Path(self.temp.name)), start_workers=False)
        self.addCleanup(self.service.close)
        self.coconut = self.bot('Hermes')
        self.scout = self.bot('Scout')
        # The default Hermes profile is the Coconut chat.
        self.service.profiles.rows = {self.coconut['id']: {'name': 'default', 'state': 'active', 'home': '/x'},
                                      self.scout['id']: {'name': 'scout', 'state': 'active', 'home': '/y'}}
        self.calls = []
        self.service.mutate = lambda method, path, data, key, device='local': self.calls.append((method, path, data))

    def bot(self, name):
        return self.service.mutate('POST', '/v1/bots', {'name': name}, str(uuid4()), 'phone')

    def task(self, bot):
        task = dict(id=str(uuid4()), bot_id=bot['id'], conversation_bot_id=bot['id'])
        self.service._get = lambda table, id, _get=self.service._get: task if table == 'tasks' and id == task['id'] else _get(table, id)
        return task['id']

    def test_the_default_profile_shows_as_the_coconut_chat(self):
        shown = coconut_chat.view(self.service, self.coconut, dict(self.coconut))
        self.assertEqual((shown['name'], shown['coconut'], shown['mode']), ('Coconut', True, 'local'))
        self.assertNotIn('coconut', coconut_chat.view(self.service, self.scout, dict(self.scout)))

    def test_only_the_coconut_chat_changes_settings(self):
        with self.assertRaises(ServiceError) as raised:
            coconut_chat.settings_tool(self.service, self.task(self.scout), {'action': 'read'})
        self.assertEqual(raised.exception.status, 403)
        state = coconut_chat.settings_tool(self.service, self.task(self.coconut), {'action': 'read'})
        self.assertEqual(state['modes'], {'local': 'available', 'cloud': 'coming soon'})
        self.assertEqual({agent['name'] for agent in state['agents']}, {'Coconut', 'Scout'})

    def test_a_change_goes_through_the_route_of_the_app(self):
        task = self.task(self.coconut)
        answer = coconut_chat.settings_tool(self.service, task, {'action': 'change', 'setting': 'agent_access',
                                                                 'agent': 'scout', 'value': 'off'})
        self.assertEqual(self.calls, [('PATCH', f'/v1/bots/{self.scout["id"]}', {'approval_override': 'off'})])
        self.assertEqual((answer['agent'], answer['before'], answer['after']), ('Scout', 'default', 'off'))
        coconut_chat.settings_tool(self.service, task, {'action': 'change', 'setting': 'agent_notifications',
                                                        'agent': 'Scout', 'value': False})
        coconut_chat.settings_tool(self.service, task, {'action': 'change', 'setting': 'default_access', 'value': 'smart'})
        self.assertEqual(self.calls[1:], [('PATCH', f'/v1/bots/{self.scout["id"]}', {'notifications': False}),
                                          ('PUT', '/v1/settings/approvals', {'mode': 'smart'})])

    def test_cloud_is_coming_soon_and_an_unknown_agent_lists_the_agents(self):
        task = self.task(self.coconut)
        with self.assertRaises(ServiceError) as raised:
            coconut_chat.settings_tool(self.service, task, {'action': 'change', 'setting': 'mode', 'value': 'cloud'})
        self.assertIn('coming soon', str(raised.exception))
        with self.assertRaises(ServiceError) as raised:
            coconut_chat.settings_tool(self.service, task, {'action': 'change', 'setting': 'agent_access',
                                                            'agent': 'Nobody', 'value': 'off'})
        self.assertIn('Scout', str(raised.exception))

    def test_the_coconut_chat_is_never_archived_or_deleted(self):
        with self.assertRaises(ServiceError):
            coconut_chat.refuse_removal(self.service, self.coconut['id'], {'archived': True})
        with self.assertRaises(ServiceError):
            coconut_chat.refuse_removal(self.service, self.coconut['id'])
        coconut_chat.refuse_removal(self.service, self.coconut['id'], {'pinned': True})
        coconut_chat.refuse_removal(self.service, self.scout['id'])


if __name__ == '__main__':
    unittest.main()
