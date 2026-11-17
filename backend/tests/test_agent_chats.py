"""A new chat of an agent is a fresh conversation with the same Hermes profile (agent_chats.py)."""
import sqlite3
import unittest

from coconut import profile_registry
from coconut.agent_chats import chat_title
from coconut.service import ServiceError
from test_bot_profiles import ProfileTests


class AgentChatTests(ProfileTests):
    def agent(self, name='work'):
        return next(bot for bot in self.service.snapshot()['bots'] if bot['profile'] == name and not bot.get('chat_of'))

    def chats(self, agent):
        return [bot for bot in self.service.snapshot()['bots'] if bot.get('chat_of') == agent['id']]

    def new_chat(self, bot):
        return self.write('POST', f'/v1/bots/{bot["id"]}/chats')

    def test_a_new_chat_shares_the_profile_and_has_its_own_session(self):
        agent = self.agent()
        before = sorted(str(path) for path in self.hermes.rglob('*'))
        chat = self.new_chat(agent)
        self.assertEqual(chat['chat_of'], agent['id'])
        self.assertEqual((chat['profile'], chat['profile_state'], chat['name']), ('work', 'active', agent['name']))
        self.assertNotEqual(chat['id'], agent['id'])
        self.assertNotEqual(chat['session_id'], agent['session_id'])
        self.assertEqual((chat['chat_title'], chat['unread_count'], chat['archived'], chat['pinned']), ('', 0, False, False))
        # Hermes creates no profile, and Coconut writes no file: no copy of .env or of any other file.
        self.assertEqual(self.commands, [])
        self.assertEqual(sorted(str(path) for path in self.hermes.rglob('*')), before)
        rows = self.service.profiles.rows
        self.assertEqual(rows[chat['id']]['home'], rows[agent['id']]['home'])
        self.assertEqual(self.service.profiles.home(chat), self.work)
        # A scan adds no bot for the shared folder, and a restart keeps the chat.
        self.assertEqual(self.service.rescan_profiles(), [])
        self.open()
        self.assertEqual([bot['id'] for bot in self.chats(agent)], [chat['id']])

    def test_a_new_chat_from_a_chat_belongs_to_the_agent(self):
        agent = self.agent()
        first = self.new_chat(agent)
        second = self.new_chat(first)
        self.assertEqual(second['chat_of'], agent['id'])
        self.assertEqual(len(self.chats(agent)), 2)

    def test_the_request_key_makes_one_chat(self):
        agent = self.agent()
        key = '5f0c0a2e-9d7c-4a55-8a65-0d6f6c1d2b11'
        first = self.write('POST', f'/v1/bots/{agent["id"]}/chats', key=key)
        again = self.write('POST', f'/v1/bots/{agent["id"]}/chats', key=key)
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(len(self.chats(agent)), 1)

    def test_the_first_message_names_the_chat_and_stays_in_it(self):
        agent = self.agent()
        chat = self.new_chat(agent)
        self.write('POST', f'/v1/bots/{chat["id"]}/messages', {'text': '\nPlan the payout reminder email\nfor Friday'})
        self.write('POST', f'/v1/bots/{chat["id"]}/messages', {'text': 'A second message'})
        shown = self.chats(agent)[0]
        self.assertEqual(shown['chat_title'], 'Plan the payout reminder email')
        state = self.service.snapshot()
        self.assertEqual({message['bot_id'] for message in state['messages'] if message['role'] == 'user'}, {chat['id']})
        self.assertEqual({task['session_id'] for task in state['tasks']}, {chat['session_id']})
        self.assertEqual(self.agent().get('chat_title'), None)
        self.assertEqual(chat_title('x' * 100), 'x' * 71 + '…')

    def test_a_chat_takes_its_own_fields_and_the_agent_owns_the_settings(self):
        agent = self.agent()
        chat = self.new_chat(agent)
        with self.assertRaisesRegex(ServiceError, 'Change them on the agent'):
            self.write('PATCH', f'/v1/bots/{chat["id"]}', {'instructions': 'Other.'})
        with self.assertRaisesRegex(ServiceError, 'Only a chat'):
            self.write('PATCH', f'/v1/bots/{agent["id"]}', {'chat_title': 'Title'})
        self.assertEqual(self.write('PATCH', f'/v1/bots/{chat["id"]}', {'chat_title': ' Payouts '})['chat_title'], 'Payouts')
        self.assertTrue(self.write('PATCH', f'/v1/bots/{chat["id"]}', {'archived': True})['archived'])
        self.assertFalse(self.agent()['archived'])
        # The look of the agent reaches its chats.
        self.write('PATCH', f'/v1/bots/{agent["id"]}', {'color': '448A89', 'avatar': 'coconut.bean'})
        shown = self.chats(agent)[0]
        self.assertEqual((shown['color'], shown['avatar']), ('448A89', 'coconut.bean'))

    def test_removing_a_chat_keeps_the_agent_and_removing_the_agent_takes_its_chats(self):
        agent = self.agent()
        first, second = self.new_chat(agent), self.new_chat(agent)
        self.write('DELETE', f'/v1/bots/{first["id"]}')
        self.assertEqual([bot['id'] for bot in self.chats(agent)], [second['id']])
        self.assertEqual(self.agent()['id'], agent['id'])
        self.assertTrue(self.work.is_dir())
        self.write('DELETE', f'/v1/bots/{agent["id"]}')
        self.service.rescan_profiles()
        self.assertEqual({bot['profile'] for bot in self.service.snapshot()['bots']}, {'default'})
        self.assertTrue(self.work.is_dir())

    def test_only_the_agent_deletes_the_profile_and_the_name_is_free_again(self):
        agent = self.agent()
        chat = self.new_chat(agent)
        with self.assertRaisesRegex(ServiceError, 'from the agent'):
            self.write('POST', f'/v1/bots/{chat["id"]}/profile/delete', {'confirm': 'work'})
        self.write('POST', f'/v1/bots/{agent["id"]}/profile/delete', {'confirm': 'work'})
        self.assertFalse(self.work.exists())
        self.assertEqual([row for row in self.service.profiles.rows.values() if row['name'] == 'work'], [])
        self.profile('work')
        self.service.rescan_profiles()
        self.assertNotIn(self.agent()['id'], (agent['id'], chat['id']))

    def test_a_missing_folder_marks_the_agent_and_its_chats(self):
        agent = self.agent()
        chat = self.new_chat(agent)
        self.work.rename(self.base / 'kept')
        self.service.rescan_profiles()
        states = {bot['id']: bot['profile_state'] for bot in self.service.snapshot()['bots']}
        self.assertEqual((states[agent['id']], states[chat['id']]), ('missing', 'missing'))
        with self.assertRaisesRegex(ServiceError, 'no Hermes profile'):
            self.new_chat(agent)
        (self.base / 'kept').rename(self.work)
        self.service.rescan_profiles()
        states = {bot['id']: bot['profile_state'] for bot in self.service.snapshot()['bots']}
        self.assertEqual((states[agent['id']], states[chat['id']]), ('active', 'active'))

    def test_a_chat_of_the_coconut_agent_can_be_archived_and_is_not_the_coconut_chat(self):
        coconut = self.agent('default')
        chat = self.new_chat(coconut)
        self.assertTrue(coconut.get('coconut'))
        self.assertFalse(chat.get('coconut'))
        self.assertTrue(self.write('PATCH', f'/v1/bots/{chat["id"]}', {'archived': True})['archived'])
        with self.assertRaisesRegex(ServiceError, 'The Coconut chat stays'):
            self.write('PATCH', f'/v1/bots/{coconut["id"]}', {'archived': True})

    def test_the_gateway_serves_the_chats_of_its_profile_and_names_the_agent_for_the_home(self):
        agent = self.agent()
        chat = self.new_chat(agent)
        home = str(self.work.resolve())
        self.assertTrue(self.service._gateway_serves(home, chat['id']))
        self.assertFalse(self.service._gateway_serves(home, self.agent('default')['id']))
        self.assertEqual(self.service._gateway_profile(home)['bot_id'], agent['id'])
        self.assertEqual(profile_registry.bot_for_home(self.root, self.work), agent['id'])
        self.assertEqual(profile_registry.homes(self.root).count(str(self.work)), 1)

    def test_other_agents_and_the_usage_see_the_agent_once(self):
        agent = self.agent()
        chat = self.new_chat(agent)
        self.assertNotIn(chat['id'], [bot['id'] for bot in self.service.team_rooms()['mac_bots']])
        self.assertNotIn(chat['id'], self.service.usage()['agents'])
        from coconut import coconut_chat
        self.assertNotIn(chat['id'], [row['id'] for row in coconut_chat.read(self.service)['agents']])

    def test_a_chat_reads_and_saves_the_library_of_its_agent(self):
        agent = self.agent()
        chat = self.new_chat(agent)
        self.service.publish_page(agent['id'], {'key': 'plan', 'title': 'Plan', 'format': 'markdown', 'content': 'The plan.'})
        task = self.write('POST', f'/v1/bots/{chat["id"]}/messages', {'text': 'Read the plan'})
        self.assertEqual([page['key'] for page in self.service.read_document(task['id'], {})['documents']], ['plan'])


class TableTests(unittest.TestCase):
    def test_an_earlier_table_with_unique_names_loses_the_constraint_and_keeps_its_rows(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE profiles (bot_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, home TEXT NOT NULL, '
                   'state TEXT NOT NULL, seen_at TEXT NOT NULL)')
        db.execute("INSERT INTO profiles VALUES ('b','work','/h/work','active','t'), ('a','default','/h','active','t')")
        profile_registry.ensure_table(db)
        profile_registry.ensure_table(db)
        self.assertEqual(list(profile_registry.rows(db)), ['b', 'a'])
        profile_registry.write(db, dict(bot_id='c', name='work', home='/h/work', state='active'))
        self.assertEqual([row['name'] for row in profile_registry.rows(db).values()], ['work', 'default', 'work'])


if __name__ == '__main__':
    unittest.main()
