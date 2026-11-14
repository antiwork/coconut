"""The team-only service (coconut/team_only.py): a Mac that keeps its team rooms and the pages of its declared bots, and
touches nothing of Hermes.

Most tests run the service in this process with the FakeRelay and the FakeHelper of team_fakes.py. The first Mac of
TeamMacs is a full Mac (Ada), and `only` is the team-only Mac. ProcessTests starts the service as the login item does,
in a fresh process. scripts/test_team_log.py runs it with the real helper and the relay code.
"""
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from uuid import uuid4

from coconut import team_only
from coconut.service_base import FULL_MODE, TEAM_MODE, WRONG_FOLDER, ServiceError
from coconut.team import PERSON_ONLY
from coconut.team_only import TEAM_PAGE, TeamOnlyService
from team_fakes import RELAY, FakeHelper, TeamMacs

PAGE = dict(key='weekly-report', title='Weekly report', format='markdown', summary='3 open tasks',
            content='3 tasks are open.')
BACKEND = Path(__file__).resolve().parents[1]
# The modules of this package that a team-only service may load. Each other module belongs to the full runtime.
TEAM_ONLY_MODULES = {'coconut', 'coconut.server', 'coconut.service_base', 'coconut.team_only', 'coconut.documents',
                     'coconut.gateway_bridge', 'coconut.silent_responses', 'coconut.service_restart', 'coconut.team',
                     'coconut.team_helper', 'coconut.team_keys', 'coconut.team_messages', 'coconut.team_pages',
                     'coconut.team_relay', 'coconut.team_socket', 'coconut.team_store', 'coconut.team_sync',
                     'coconut.push_relay', 'coconut.team_turns', 'coconut.team_directs', 'coconut.team_epochs',
                     'coconut.team_typing', 'coconut.team_chats', 'coconut.hermes_notices'}


def make_hermes_home(home: Path) -> None:
    """A made-up Hermes home with the files that a production agent has: config, keys, profiles, plugins, cron and a
    state database."""
    files = {'config.yaml': 'model:\n  default: example-model\n', '.env': 'EXAMPLE_API_KEY=not-a-real-key\n',
             'SOUL.md': 'An example agent.\n', 'profiles/reporter/config.yaml': 'model:\n  default: example-model\n',
             'profiles/reporter/.env': 'EXAMPLE_API_KEY=not-a-real-key\n',
             'plugins/example/plugin.yaml': 'name: example\n', 'plugins/example/__init__.py': '',
             'cron/jobs.json': '{"jobs": []}\n', 'skills/example/SKILL.md': '# Example\n'}
    for name, text in files.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text)
    with sqlite3.connect(home / 'state.db') as database:
        database.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY)')
    database.close()


def snapshot(home: Path) -> dict:
    """Each path under the home with its kind, size, mode, inode, times of change and bytes. The read of the bytes
    changes only the access time, which the snapshot leaves out."""
    found = {}
    for folder, folders, files in os.walk(home):
        for name in folders + files:
            path = Path(folder) / name
            stat = path.lstat()
            found[str(path.relative_to(home))] = (stat.st_mode, stat.st_ino, stat.st_size, stat.st_mtime_ns,
                                                 stat.st_ctime_ns, path.read_bytes() if path.is_file() else None)
    stat = home.lstat()
    found['.'] = (stat.st_mode, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
    return found


class TeamOnlyMacs(TeamMacs):
    def setUp(self):
        super().setUp()
        self.home = Path(self.temp.name) / 'hermes'
        make_hermes_home(self.home)
        self.only = self.team_only_mac('only')

    def team_only_mac(self, name: str) -> TeamOnlyService:
        service = TeamOnlyService(Path(self.temp.name) / name)
        self.addCleanup(lambda: service.stopping.is_set() or service.close())
        service.team_relay, service.team_helper = self.relay, FakeHelper()
        return service

    def call(self, method: str, path: str, data: dict | None = None, service=None) -> dict:
        return (service or self.only).team_request(method, path, data or {})

    def declare(self, name: str = 'Reporter', home=None, **fields) -> dict:
        return self.call('POST', '/v1/team/bots', dict(fields, name=name, home=str(home or self.home)))['bot']

    def team(self) -> str:
        """Ada makes a room, and the team-only Mac joins it. Return the room ID."""
        room = self.create()
        self.join(self.only, self.invite(room)['link'], 'Reporter Mac')
        return room['room']

    def share(self, room: str, bot: dict) -> dict:
        return self.call('POST', f'/v1/team/rooms/{room}/bots', {'bot_id': bot['id']})['room']

    def save(self, **fields) -> dict:
        return self.only.gateway_document({**PAGE, 'home': str(self.home), 'audience': 'team', **fields})

    def sync(self):
        """A pass on the team-only Mac, which writes, then one on Ada's Mac, which reads."""
        self.only.team_sync_pass()
        self.first.team_sync_pass()

    def library(self) -> dict:
        """Ada's Library: the team pages and the team bots."""
        answer = self.first.documents()
        return {'pages': [(page['title'], page['version']) for page in answer['documents'] if 'team' in page],
                'bots': [bot['name'] for bot in answer['team_bots']]}

    def stored(self) -> int:
        return self.only.db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]


class TeamOnlyFlowTests(TeamOnlyMacs, unittest.TestCase):
    def test_a_full_flow_leaves_the_hermes_home_as_it_was(self):
        before = snapshot(self.home)
        room = self.team()
        bot = self.declare()
        self.assertEqual(self.share(room, bot)['bots'][0]['name'], 'Reporter')
        self.assertEqual(self.save()['status'], 'saved')
        self.sync()
        self.assertEqual(self.library(), {'pages': [('Weekly report', 1)], 'bots': ['Reporter']})
        # Ada's Library lists the page under the team bot.
        listed = self.first.documents()
        (page,) = (row for row in listed['documents'] if 'team' in row)
        self.assertEqual(page['bot_id'], listed['team_bots'][0]['id'])
        self.assertEqual(self.first.document(page['id'])['content'], '3 tasks are open.')
        self.only.close()
        self.assertEqual(snapshot(self.home), before)

    def test_a_page_of_a_bot_without_a_team_waits_and_goes_out_when_the_bot_is_shared(self):
        room = self.team()
        bot = self.declare()
        self.assertEqual(self.save()['status'], 'saved')
        self.sync()
        self.assertEqual(self.library(), {'pages': [], 'bots': []})
        self.share(room, bot)
        self.sync()
        self.assertEqual(self.library(), {'pages': [('Weekly report', 1)], 'bots': ['Reporter']})

    def test_a_save_that_changes_nothing_writes_nothing(self):
        self.declare()
        self.assertEqual(self.save()['version'], 1)
        self.assertEqual(self.save(), {'status': 'unchanged', 'key': 'weekly-report', 'version': 1, 'expires_at': None})
        self.assertEqual(self.save(content='4 tasks are open.')['version'], 2)
        # The page limits and checks are those of a full Mac.
        self.assertEqual(self.refusal(lambda: self.save(key='Not A Key'))[0], 400)
        self.assertEqual(self.refusal(lambda: self.save(format='html', content='<p style="color:red">x</p>'))[0], 400)

    def test_a_page_must_name_the_team_audience(self):
        self.declare()
        for fields in ({}, {'audience': 'private'}, {'audience': None}, {'audience': 'Team'}):
            with self.subTest(fields=fields):
                page = dict(PAGE, home=str(self.home), **fields)
                self.assertEqual(self.refusal(self.only.gateway_document, page), (400, TEAM_PAGE))
        self.assertEqual(self.stored(), 0)
        # A home that is not the home of a declared bot, and a home that is not a full path.
        status, text = self.refusal(lambda: self.save(home=str(self.home / 'profiles' / 'reporter')))
        self.assertEqual(status, 404)
        self.assertIn('coconut team new-agent', text)
        for home in ('hermes', '~/.hermes', 'a\0b', '/a\ud800'):
            with self.subTest(home=home):
                self.assertEqual(self.refusal(lambda: self.save(home=home))[0], 400)
        self.assertEqual(self.stored(), 0)

    def test_a_save_names_the_home_as_the_declaration_does(self):
        # A folder name that ends with a space is a home of its own. A strip would name another folder.
        spaced = Path(self.temp.name) / 'agent '
        spaced.mkdir()
        bot = self.declare('Spaced', home=spaced)
        self.assertEqual(bot['home'], os.path.realpath(spaced))
        self.assertEqual(self.save(home=str(spaced))['status'], 'saved')
        self.assertEqual(self.refusal(lambda: self.save(home=str(spaced).rstrip()))[0], 404)
        # A trailing slash and a link name the same home as the declaration.
        self.declare()
        self.assertEqual(self.save()['status'], 'saved')
        link = Path(self.temp.name) / 'link'
        link.symlink_to(self.home)
        for home in (f'{self.home}/', str(link), f'{link}/'):
            with self.subTest(home=home):
                self.assertEqual(self.save(home=home)['status'], 'unchanged')
        # A missing home, and a home that is not a full path, answer the same clear error.
        page = {**PAGE, 'audience': 'team'}
        for home in (None, '', '   ', 7):
            with self.subTest(home=home):
                fields = page if home is None else dict(page, home=home)
                self.assertEqual(self.refusal(self.only.gateway_document, fields), (400, team_only.HOME))

    def remove(self, **fields) -> dict:
        """DELETE /v1/gateway/document of the tooling: the home and the key, without an audience."""
        return self.only.gateway_document_delete({'home': str(self.home), 'key': 'weekly-report', **fields})

    def versions(self) -> int:
        return self.only.db.execute('SELECT COUNT(*) FROM document_versions').fetchone()[0]

    def team_entries(self, room: str, kind: str) -> list[str]:
        """The entry IDs of the team-only Mac on the relay of this kind."""
        import base64
        from team_fakes import decode
        member = self.only.team_store.room(room)['member']
        found = []
        for row in self.relay.rooms[room]['entries'].values():
            payload = json.loads(base64.b64decode(decode(row['body'])['payload']))
            if payload['kind'] == kind and row['member'] == member:
                found.append(row['id'])
        return found

    def test_the_tooling_deletes_a_team_page_and_the_bot_stays(self):
        room = self.team()
        bot = self.declare()
        self.share(room, bot)
        self.save()
        self.sync()
        self.save(content='4 tasks are open.')
        self.sync()
        self.assertEqual((self.library(), self.versions()), ({'pages': [('Weekly report', 2)], 'bots': ['Reporter']}, 2))
        (page_entry,) = self.team_entries(room, 'page')
        self.assertEqual(self.remove(), {'status': 'deleted', 'key': 'weekly-report'})
        self.assertEqual((self.stored(), self.versions()), (0, 0))
        self.sync()
        # The bot stays in the team: the team shares the bot, not only its pages.
        self.assertEqual(self.library(), {'pages': [], 'bots': ['Reporter']})
        self.assertEqual(self.team_entries(room, 'page'), [])
        self.assertIn(page_entry, self.team_entries(room, 'delete'))
        # A repeat finds no page.
        self.assertEqual(self.remove(), {'status': 'absent', 'key': 'weekly-report'})

    def test_the_tooling_deletes_a_page_that_waits_for_a_team(self):
        self.declare()
        self.save()
        with mock.patch.object(self.only, 'team_wake') as wake:
            # A delete takes no audience. A page of a bot without a team goes too.
            self.assertEqual(self.remove(audience='private'), {'status': 'deleted', 'key': 'weekly-report'})
        wake.assert_called()
        self.assertEqual((self.stored(), self.versions()), (0, 0))
        with mock.patch.object(self.only, 'team_wake') as wake:
            self.assertEqual(self.remove(), {'status': 'absent', 'key': 'weekly-report'})
            self.assertEqual(self.remove(key='never-saved'), {'status': 'absent', 'key': 'never-saved'})
        wake.assert_not_called()

    def test_a_delete_refuses_a_bad_home_or_key_and_keeps_each_page(self):
        from coconut.documents import KEY_RULE
        self.declare()
        other_home = Path(self.temp.name) / 'other-hermes'
        other_home.mkdir()
        self.declare('Helper', home=other_home)
        self.save()
        self.save(home=str(other_home))
        for home in (None, '', 'hermes', '~/.hermes', 7, 'a\0b'):
            with self.subTest(home=home):
                fields = {'key': 'weekly-report'} if home is None else {'home': home, 'key': 'weekly-report'}
                self.assertEqual(self.refusal(self.only.gateway_document_delete, fields), (400, team_only.HOME))
        undeclared = str(self.home / 'profiles' / 'reporter')
        self.assertEqual(self.refusal(lambda: self.remove(home=undeclared)), (404, team_only.UNKNOWN_HOME))
        for key in (None, 7, 'Weekly Report', ''):
            with self.subTest(key=key):
                self.assertEqual(self.refusal(lambda: self.remove(key=key)), (400, KEY_RULE))
        self.assertEqual(self.refusal(self.only.gateway_document_delete, {'home': str(self.home)}), (400, KEY_RULE))
        self.assertEqual(self.stored(), 2)
        # The delete of one bot's key keeps the page of the other bot with the same key.
        self.assertEqual(self.remove()['status'], 'deleted')
        self.assertEqual(self.stored(), 1)
        self.assertEqual(self.save(home=str(other_home))['status'], 'unchanged')

    def test_the_tool_reads_the_mode_and_the_bots(self):
        self.team()
        bot = self.declare()
        self.sync()
        answer = self.call('GET', '/v1/team/rooms')
        self.assertEqual((answer['mode'], answer['mac_bots']), ('team', [{'id': bot['id'], 'name': 'Reporter'}]))
        # A full Mac has no mode, so the tool tells the two apart.
        self.assertNotIn('mode', self.call('GET', '/v1/team/rooms', service=self.first))
        status = self.call('GET', '/v1/team/status')
        self.assertEqual((status['mode'], status['helper'], status['bots']), ('team', True, 1))
        self.assertTrue(status['version'] is None or len(status['version']) in (40, 64), status['version'])
        (room,) = status['rooms']
        self.assertEqual((room['name'], room['state'], room['members'], room['bots']), ('Example Team', 'active', 2, 0))

    def test_the_token_keeps_each_team_route_that_a_persons_mac_refuses(self):
        # No person is at this Mac, so its tools call each team route with the token of the bridge file.
        room = self.call('POST', '/v1/team/rooms', {'name': 'Example Team', 'member_name': 'Reporter Mac',
                                                    'relay': RELAY})['room']['room']
        link = self.call('POST', f'/v1/team/rooms/{room}/invites', {})['link']
        self.join(self.second, link)
        changed = self.call('PATCH', f'/v1/team/rooms/{room}', {'member_name': 'Reporter Mac 2'})['room']
        self.assertEqual([member['name'] for member in changed['members'] if member['this_mac']], ['Reporter Mac 2'])
        messages = f'/v1/team/rooms/{room}/messages'
        sent = self.call('POST', messages, {'text': 'Hello'})['message']
        self.assertEqual(self.call('PATCH', f'{messages}/{sent["id"]}', {'text': 'Hi'})['message']['text'], 'Hi')
        self.assertEqual([message['text'] for message in self.call('GET', messages)['messages']], ['Hi'])
        self.assertEqual(self.call('POST', f'/v1/team/rooms/{room}/read', {'first': sent['first']})['unread'], 0)
        self.assertEqual(self.call('DELETE', f'{messages}/{sent["id"]}')['deleted'], True)
        # The same token on a person's Mac gets the refusal.
        self.assertEqual(self.refusal(self.call, 'POST', '/v1/team/rooms', {'name': 'Example Team'}, self.first),
                         (403, PERSON_ONLY))

    def test_the_status_names_the_commit_of_a_checkout(self):
        folder = Path(self.temp.name) / 'checkout'
        (folder / '.git/refs/heads').mkdir(parents=True)
        commit = 'a' * 40
        (folder / '.git/HEAD').write_text('ref: refs/heads/main\n')
        (folder / '.git/packed-refs').write_text(f'# pack-refs\n{commit} refs/heads/main\n')
        self.assertEqual(team_only.code_version(folder), commit)
        (folder / '.git/refs/heads/main').write_text('b' * 40 + '\n')
        self.assertEqual(team_only.code_version(folder), 'b' * 40)
        (folder / '.git/HEAD').write_text('c' * 40 + '\n')
        self.assertEqual(team_only.code_version(folder), 'c' * 40)
        # A worktree names its git folder in a file, and its refs are in the shared folder.
        worktree = Path(self.temp.name) / 'worktree'
        worktree.mkdir()
        (folder / '.git/worktrees/one').mkdir(parents=True)
        (worktree / '.git').write_text(f'gitdir: {folder}/.git/worktrees/one\n')
        (folder / '.git/worktrees/one/HEAD').write_text('ref: refs/heads/main\n')
        (folder / '.git/worktrees/one/commondir').write_text('../..\n')
        self.assertEqual(team_only.code_version(worktree), 'b' * 40)
        self.assertIsNone(team_only.code_version(Path(self.temp.name) / 'nothing'))


class BotRecordTests(TeamOnlyMacs, unittest.TestCase):
    def test_a_bot_record_has_the_fields_of_a_bot_entry(self):
        bot = self.declare('Reporter 📊', avatar='📊', color='blue')
        self.assertEqual({name: bot[name] for name in ('name', 'avatar', 'color', 'home', 'chat', 'teams')},
                         {'name': 'Reporter 📊', 'avatar': '📊', 'color': 'blue', 'home': os.path.realpath(self.home),
                          'chat': False, 'teams': []})
        self.assertEqual(self.call('GET', '/v1/team/bots')['bots'], [bot])

    def test_a_bot_record_refuses_hostile_input(self):
        cases = [({'name': 'R' * 81}, 'Use at most 80 characters for the name of the agent.'),
                 ({'name': 'Re\ud800porter'}, 'Use only valid text for the name of the agent.'),
                 ({'name': 'Two\nlines'}, 'Put the name of the agent on one line.'),
                 ({'name': '‮Reporter'}, 'Remove the hidden characters from the name of the agent.'),
                 ({'name': ' Reporter'}, 'Remove the space at the start or end of the name of the agent.'),
                 ({'name': ''}, 'Give the name of the agent.'), ({'name': 7}, 'Give the name of the agent.'), ({}, 'Give the name of the agent.'),
                 ({'name': 'Reporter', 'avatar': 'a' * 81}, 'Use text of at most 80 characters for the avatar.'),
                 ({'name': 'Reporter', 'color': 'c' * 21}, 'Use text of at most 20 characters for the color.'),
                 ({'name': 'Reporter', 'color': '\udfff'}, 'Use text of at most 20 characters for the color.'),
                 ({'name': 'Reporter', 'home': 42}, team_only.HOME),
                 ({'name': 'Reporter', 'home': None}, team_only.HOME),
                 ({'name': 'Reporter', 'home': ['/tmp']}, team_only.HOME),
                 ({'name': 'Reporter', 'home': ''}, team_only.HOME),
                 ({'name': 'Reporter', 'home': 'relative/hermes'}, team_only.HOME),
                 ({'name': 'Reporter', 'home': '/tmp/a\0b'}, team_only.HOME),
                 ({'name': 'Reporter', 'home': '/tmp/\ud800'}, team_only.HOME),
                 ({'name': 'Reporter', 'home': '/' + 'h' * 4096}, team_only.HOME)]
        for data, text in cases:
            with self.subTest(data=data):
                self.assertEqual(self.refusal(self.call, 'POST', '/v1/team/bots', data), (400, text))
        self.assertEqual(self.call('GET', '/v1/team/bots')['bots'], [])

    def test_two_bots_cannot_stand_for_one_home_or_have_one_name(self):
        self.declare()
        # The same home through a link is the same home.
        link = Path(self.temp.name) / 'link'
        link.symlink_to(self.home)
        status, text = self.refusal(lambda: self.declare('Helper', home=link))
        self.assertEqual((status, text), (409, 'The agent Reporter already stands for this Hermes home. A Hermes home '
                                               'has one agent.'))
        other = Path(self.temp.name) / 'other'
        self.assertEqual(self.refusal(lambda: self.declare('reporter', home=other)),
                         (409, 'This Mac already has an agent with the name Reporter. Choose another name.'))
        self.assertEqual(self.declare('Helper', home=other)['home'], os.path.realpath(other))

    def test_the_default_home_is_the_home_of_the_process(self):
        with mock.patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
            bot = self.call('POST', '/v1/team/bots', {'name': 'Reporter'})['bot']
        self.assertEqual(bot['home'], os.path.realpath(self.home))
        user = Path(self.temp.name) / 'user'
        with mock.patch.dict(os.environ, {'HOME': str(user), 'HERMES_HOME': ''}):
            bot = self.call('POST', '/v1/team/bots', {'name': 'Helper'})['bot']
        self.assertEqual(bot['home'], os.path.realpath(user / '.hermes'))

    def test_a_bot_is_open_only_while_it_answers_in_the_chat(self):
        room = self.team()
        bot = self.declare()
        self.assertIs(bot['chat'], False, 'A declared bot does not answer in the chat yet.')
        (own,) = self.share(room, bot)['bots']
        self.assertIs(own['open'], False)
        self.sync()
        member = self.only.team_store.room(room)['member']

        def seen() -> tuple:
            (row,) = self.first.team_request('GET', '/v1/team/rooms', {})['rooms'][0]['bots']
            return row['name'], row['this_mac'], row['open']

        self.assertEqual(list(self.only.team_store.written(room, member).values()), [('bot', 'Reporter', '', '', 0)])
        self.assertEqual(seen(), ('Reporter', False, False), 'No mark while the bot does not answer.')
        # The step that connects the bot to its agent sets `chat`. The next pass writes the mark.
        changed = self.call('PATCH', f'/v1/team/bots/{bot["id"]}', {'chat': True})['bot']
        self.assertIs(changed['chat'], True)
        self.assertEqual(self.call('GET', '/v1/team/status')['chat_bots'], 1)
        self.sync()
        self.assertEqual(list(self.only.team_store.written(room, member).values()), [('bot', 'Reporter', '', '', 1)])
        self.assertEqual(seen(), ('Reporter', False, True))
        # A change of the name keeps the mark, and a pass with nothing new writes nothing.
        self.call('PATCH', f'/v1/team/bots/{bot["id"]}', {'name': 'Night Reporter'})
        self.sync()
        self.assertEqual(seen(), ('Night Reporter', False, True))
        self.assertEqual(self.only._team_steps(self.only.team_store.room(room))[0], [])
        # Back to false takes the mark away on the other Mac.
        self.call('PATCH', f'/v1/team/bots/{bot["id"]}', {'chat': False})
        self.sync()
        self.assertEqual(seen(), ('Night Reporter', False, False))
        # Only a boolean is a value of `chat`.
        for value in ('true', 1, None, {}):
            with self.subTest(value=value):
                self.assertEqual(self.refusal(self.call, 'PATCH', f'/v1/team/bots/{bot["id"]}', {'chat': value}),
                                 (400, 'Give chat as true or false.'))
        self.assertIs(self.call('GET', '/v1/team/bots')['bots'][0]['chat'], False)

    def test_a_team_only_mac_is_a_member_without_a_person(self):
        room = self.team()
        self.sync()
        own = self.only.team_store.room(room)['member']
        view = self.first.team_request('GET', '/v1/team/rooms', {})['rooms'][0]
        self.assertEqual([(member['name'], member['bot_only']) for member in view['members']],
                         [('Ada', False), ('Reporter Mac', True)])
        self.assertTrue(self.only.team_store.member(room, own)['bot_only'])
        # A pass with nothing new writes no `member` entry again.
        before = self.only.team_store.entry(room, self.only.team_store.member(room, own)['entry'])['rev']
        self.sync()
        self.sync()
        self.assertEqual(self.only.team_store.entry(room, self.only.team_store.member(room, own)['entry'])['rev'], before)
        # A full Mac writes no mark.
        self.assertIs(view['members'][0]['bot_only'], False)

    def test_a_team_only_mac_of_an_older_version_writes_its_member_entry_once_with_the_mark(self):
        room = self.team()
        self.sync()
        own = self.only.team_store.room(room)['member']
        with self.only.lock:
            self.only.db.execute('UPDATE team_members SET bot_only=0 WHERE room=? AND member=?', (room, own))
        # The update starts a new process, which has written no `member` entry yet.
        self.only._team_announced.clear()
        rev = self.only.team_store.entry(room, self.only.team_store.member(room, own)['entry'])['rev']
        self.sync()
        self.assertEqual(self.only.team_store.entry(room, self.only.team_store.member(room, own)['entry'])['rev'], rev + 1)
        self.sync()
        self.assertEqual(self.only.team_store.entry(room, self.only.team_store.member(room, own)['entry'])['rev'], rev + 1)
        view = self.first.team_request('GET', '/v1/team/rooms', {})['rooms'][0]
        self.assertEqual([member['bot_only'] for member in view['members']], [False, True])

    def test_a_change_keeps_the_home_and_reaches_the_team(self):
        room = self.team()
        bot = self.declare()
        self.share(room, bot)
        self.sync()
        changed = self.call('PATCH', f'/v1/team/bots/{bot["id"]}', {'name': 'Night Reporter', 'color': 'green'})['bot']
        self.assertEqual((changed['name'], changed['avatar'], changed['color'], changed['teams']),
                         ('Night Reporter', '', 'green', [room]))
        self.sync()
        self.assertEqual(self.library()['bots'], ['Night Reporter'])
        self.assertEqual(self.refusal(self.call, 'PATCH', f'/v1/team/bots/{bot["id"]}', {'home': '/tmp'})[0], 400)
        self.assertEqual(self.refusal(self.call, 'PATCH', f'/v1/team/bots/{bot["id"]}', {'name': 'x' * 81})[0], 400)
        self.assertEqual(self.refusal(self.call, 'PATCH', '/v1/team/bots/missing', {'name': 'Other'}),
                         (404, 'This Mac has no agent with this ID.'))
        self.declare('Helper', home=Path(self.temp.name) / 'other')
        self.assertEqual(self.refusal(self.call, 'PATCH', f'/v1/team/bots/{bot["id"]}', {'name': 'HELPER'})[0], 409)
        (stored,) = [row for row in self.call('GET', '/v1/team/bots')['bots'] if row['id'] == bot['id']]
        self.assertEqual(stored['name'], 'Night Reporter')

    def test_a_removed_bot_leaves_each_team_with_its_pages(self):
        room = self.team()
        bot = self.declare()
        self.share(room, bot)
        self.save()
        self.sync()
        self.assertEqual(self.library(), {'pages': [('Weekly report', 1)], 'bots': ['Reporter']})
        self.assertEqual(self.call('DELETE', f'/v1/team/bots/{bot["id"]}'), {'bot': bot['id'], 'removed': True})
        self.assertEqual(self.call('GET', '/v1/team/bots')['bots'], [])
        self.assertEqual(self.call('GET', '/v1/team/rooms')['rooms'][0]['bots'], [])
        self.assertEqual(self.stored(), 0)
        self.sync()
        self.assertEqual(self.library(), {'pages': [], 'bots': []})
        self.assertEqual(self.refusal(self.call, 'DELETE', f'/v1/team/bots/{bot["id"]}')[0], 404)
        # The home is free again.
        self.assertEqual(self.declare()['home'], os.path.realpath(self.home))


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'data'

    def test_a_full_install_is_refused(self):
        from coconut.providers import FixtureProvider
        from coconut.service import Service
        service = Service(self.data, FixtureProvider(self.data), start_workers=False)
        # The folder of a running service has its lock.
        with self.assertRaises(ServiceError) as held:
            TeamOnlyService(self.data)
        self.assertEqual(held.exception.status, 409)
        service.mutate('POST', '/v1/bots', {'name': 'Ada'}, str(uuid4()), 'phone')
        service.close()
        with self.assertRaises(ServiceError) as refused:
            TeamOnlyService(self.data)
        self.assertEqual((refused.exception.status, str(refused.exception)), (409, WRONG_FOLDER[TEAM_MODE]))
        # The refusal releases the lock, so the full service starts again.
        Service(self.data, FixtureProvider(self.data), start_workers=False).close()

    def test_a_folder_with_a_paired_phone_is_refused(self):
        self.data.mkdir()
        with sqlite3.connect(self.data / 'coconut.sqlite3') as database:
            database.execute('CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT '
                             'NULL, created_at TEXT NOT NULL, peer_key TEXT)')
            database.execute("INSERT INTO devices VALUES ('phone', 'iPhone', 'hash', 'now', NULL)")
        database.close()
        with self.assertRaises(ServiceError) as refused:
            TeamOnlyService(self.data)
        self.assertEqual(str(refused.exception), WRONG_FOLDER[TEAM_MODE])

    def full_service(self):
        from coconut.providers import FixtureProvider
        from coconut.service import Service
        return Service(self.data, FixtureProvider(self.data), start_workers=False)

    def refused(self, start) -> ServiceError:
        with self.assertRaises(ServiceError) as refused:
            start()
        self.assertEqual((refused.exception.status, str(refused.exception)), (409, WRONG_FOLDER[FULL_MODE]))
        return refused.exception

    def test_the_full_service_and_the_refuse_mode_refuse_a_team_only_folder(self):
        from coconut.hermes_unavailable import UnavailableService
        service = TeamOnlyService(self.data)
        service.team_request('POST', '/v1/team/bots', {'name': 'Reporter', 'home': '/tmp/example-hermes'})
        service.close()
        before = (self.data / 'coconut.sqlite3').read_bytes()
        self.refused(self.full_service)
        self.refused(lambda: UnavailableService(self.data, {'status': 'missing', 'version': None, 'reason': 'None.'}))
        # A refusal writes nothing and releases the lock, so the team-only service starts again.
        self.assertEqual((self.data / 'coconut.sqlite3').read_bytes(), before)
        self.assertFalse((self.data / 'artifacts').exists())
        TeamOnlyService(self.data).close()

    def test_a_team_only_folder_without_a_bot_or_a_room_is_still_a_team_only_folder(self):
        # The service wrote its mark at its first start, and it keeps its bridge token and its team folder there. One
        # rule for each folder keeps the two refusals simple: the person uses another data folder.
        TeamOnlyService(self.data).close()
        self.refused(self.full_service)

    def test_a_new_folder_takes_either_mode(self):
        TeamOnlyService(self.data).close()
        other = Path(self.temp.name) / 'other'
        self.data = other
        self.full_service().close()

    def test_a_team_only_folder_starts_again(self):
        service = TeamOnlyService(self.data)
        service.team_request('POST', '/v1/team/bots', {'name': 'Reporter', 'home': '/tmp/example-hermes'})
        service.close()
        service = TeamOnlyService(self.data)
        self.addCleanup(service.close)
        self.assertEqual([bot['name'] for bot in service.team_request('GET', '/v1/team/bots', {})['bots']],
                         ['Reporter'])
        # No event is kept: nothing reads one on this Mac.
        self.assertNotIn('events', {row[0] for row in service.db.execute("SELECT name FROM sqlite_master")})


# Start the service as `python3 -m coconut` does, and write the names of the loaded modules when it exits.
RUNNER = ('import atexit, json, os, runpy, sys; '
          'atexit.register(lambda: open(os.environ["MODULES_FILE"], "w").write(json.dumps(sorted(sys.modules)))); '
          'sys.argv[0] = "coconut"; runpy.run_module("coconut", run_name="__main__", alter_sys=True)')


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class ProcessTests(unittest.TestCase):
    """The team-only service in a fresh process, as a login item starts it with COCONUT_MODE=team."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.data, self.home, self.modules = folder / 'data', folder / 'hermes', folder / 'modules.json'
        make_hermes_home(self.home)
        self.port, self.tunnel_port = free_port(), free_port()
        environment = {name: value for name, value in os.environ.items() if name not in ('COCONUT_TEAM_HELPER',)}
        environment.update(COCONUT_MODE='team', HERMES_HOME=str(self.home), MODULES_FILE=str(self.modules),
                           PYTHONPATH=str(BACKEND))
        self.log = folder / 'service.log'
        self.before = snapshot(self.home)
        with self.log.open('w') as output:
            self.process = subprocess.Popen(
                [sys.executable, '-c', RUNNER, '--data-dir', str(self.data), '--port', str(self.port),
                 '--tunnel-port', str(self.tunnel_port), '--provider', 'hermes'],
                cwd=folder, env=environment, stdout=output, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 30
        while not self.answers('/health'):
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.fail(f'The team-only service did not start:\n{self.log.read_text()}')
            time.sleep(.1)
        self.token = json.loads((self.data / 'gateway/bridge.json').read_text())['token']

    def answers(self, path: str) -> bool:
        try:
            return self.request('GET', path)[0] == 200
        except OSError:
            return False

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(30)

    def request(self, method: str, path: str, body: dict | None = None, token: str | None = None,
                port: int | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=10)
        try:
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = f'Bearer {token}'
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def children(self) -> list[str]:
        rows = subprocess.run(['ps', '-A', '-o', 'ppid=,command='], capture_output=True, text=True, check=True).stdout
        return [command for ppid, command in (row.strip().split(None, 1) for row in rows.splitlines() if row.strip())
                if ppid == str(self.process.pid)]

    def test_the_service_touches_nothing_of_hermes(self):
        # A bot with the home of the process, and a page for the team.
        status, answer = self.request('POST', '/v1/team/bots', {'name': 'Reporter'}, self.token)
        self.assertEqual((status, answer['bot']['home']), (200, os.path.realpath(self.home)), answer)
        page = dict(PAGE, home=str(self.home), audience='team')
        self.assertEqual(self.request('POST', '/v1/gateway/document', page, self.token)[1]['status'], 'saved')
        self.assertEqual(self.request('POST', '/v1/gateway/document', dict(page, audience='private'), self.token),
                         (400, {'error': TEAM_PAGE}))
        status, answer = self.request('GET', '/v1/team/status', token=self.token)
        self.assertEqual((status, answer['mode'], answer['bots'], answer['rooms']), (200, 'team', 1, []), answer)
        # The tooling deletes the page with the token of the bridge file. Without it the page stays.
        delete = {'home': str(self.home), 'key': 'weekly-report'}
        self.assertEqual(self.request('DELETE', '/v1/gateway/document', delete)[0], 401)
        self.assertEqual(self.request('DELETE', '/v1/gateway/document', delete, self.token),
                         (200, {'status': 'deleted', 'key': 'weekly-report'}))
        self.assertEqual(self.request('DELETE', '/v1/gateway/document', delete, self.token),
                         (200, {'status': 'absent', 'key': 'weekly-report'}))

        # The team routes need the token of the bridge file.
        self.assertEqual(self.request('GET', '/v1/team/rooms')[0], 401)
        self.assertEqual(self.request('GET', '/v1/team/rooms', token='wrong')[0], 401)
        # A pair request and each route of the full service answer the refusal of this mode.
        create = {'code': '0123456789', 'device_name': 'iPhone'}
        refusal = {'error': team_only.REFUSAL, 'code': 'team_only'}
        for method, path, body, token in (('POST', '/v1/pair', create, None), ('GET', '/v1/state', None, self.token),
                                          ('POST', '/v1/gateway/tools', {}, self.token),
                                          ('POST', '/v1/gateway/cron', {}, self.token),
                                          ('POST', '/v1/gateway/files', {}, self.token),
                                          ('DELETE', '/v1/documents/x', {}, self.token),
                                          # Only the page route takes DELETE, and it takes only POST and DELETE.
                                          ('DELETE', '/v1/gateway/poll', {}, self.token),
                                          ('DELETE', '/v1/gateway/tools', {}, self.token),
                                          ('PUT', '/v1/gateway/document', {}, self.token),
                                          ('GET', '/v1/documents', None, None)):
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path, body, token), (503, refusal))
        # The adapter of the team mode polls with the token of the bridge file. No bot has its chat, so no turn comes.
        self.assertEqual(self.request('POST', '/v1/gateway/poll', {'home': str(self.home), 'wait': 0}, self.token),
                         (200, {'turns': [], 'stop': []}))
        self.assertEqual(self.request('POST', '/v1/gateway/poll', {'home': str(self.home), 'wait': 0})[0], 401)
        # No tunnel listener, and no child process without a room.
        with self.assertRaises(ConnectionRefusedError):
            self.request('GET', '/health', port=self.tunnel_port)
        self.assertEqual(self.children(), [])
        self.assertEqual(json.loads((self.data / 'process.json').read_text())['mode'], 'team')

        self.stop()
        loaded = {name for name in json.loads(self.modules.read_text())}
        self.assertEqual({name for name in loaded if name.split('.')[0].startswith('hermes')}, set())
        self.assertLessEqual({name for name in loaded if name.split('.')[0] == 'coconut'}, TEAM_ONLY_MODULES)
        self.assertEqual(snapshot(self.home), self.before)
        self.assertIn('for a team only', self.log.read_text())
        self.assertNotIn(self.token, self.log.read_text())
        self.assertNotIn('3 tasks are open.', self.log.read_text())


class ArgumentTests(unittest.TestCase):
    def run_service(self, *arguments, mode='') -> subprocess.CompletedProcess:
        environment = dict(os.environ, COCONUT_MODE=mode, PYTHONPATH=str(BACKEND))
        with tempfile.TemporaryDirectory() as folder:
            return subprocess.run([sys.executable, '-m', 'coconut', '--data-dir', folder, *arguments], env=environment,
                                  capture_output=True, text=True, timeout=30)

    def test_a_team_only_service_pairs_no_phone(self):
        result = self.run_service('--team-only', '--pair')
        self.assertEqual(result.returncode, 2)
        self.assertIn('A team-only service pairs no phone.', result.stderr)
        self.assertIn('A team-only service pairs no phone.', self.run_service('--pair', mode='team').stderr)

    def test_an_unknown_mode_stops_the_start(self):
        result = self.run_service(mode='other')
        self.assertEqual(result.returncode, 2)
        self.assertIn('COCONUT_MODE must be team, or empty for the full service.', result.stderr)


if __name__ == '__main__':
    unittest.main()
