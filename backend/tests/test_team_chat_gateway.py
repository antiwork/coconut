"""The mode `team` of the `coconut` plugin (hermes_plugin/coconut/team.py) against a team-only service.

The stand-ins of the Hermes modules are those of test_gateway_adapter.py, with the fields of a message event that a
group chat needs. Ada's Mac is a full Mac, and the team-only Mac serves its routes on a real loopback listener. The
adapter of the team mode connects to it as in a gateway, and the message handler plays the gateway runner. No model
runs. test_native_team_chat.py runs a real Hermes gateway.
"""
import asyncio
import dataclasses
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import test_gateway_adapter
from coconut.server import APIServer
from coconut.team_turns import ASKED, DENIED, chat_id
from test_team_direct_turns import DirectTurnMacs
from test_team_turns import TeamTurnMacs

EVENT = '''
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
class MessageType(Enum):
    TEXT = "text"
    COMMAND = "command"
class ProcessingOutcome(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
@dataclass
class SessionSource:
    platform: Any
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
@dataclass
class MessageEvent:
    text: str
    message_type: MessageType = MessageType.TEXT
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    source: Any = None
    message_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None
    reply_to_is_own_message: bool = False
    channel_prompt: Optional[str] = None
    channel_context: Optional[str] = None
    internal: bool = False
    metadata: dict = field(default_factory=dict)
    allow_gateway_control: bool = True
    reply_expected: Optional[bool] = None
'''
# The registration of a Hermes that has each field that the team chat needs (team.REGISTRATION_FIELDS).
REGISTRY = '''
from dataclasses import dataclass, field
from typing import Any
@dataclass
class PlatformEntry:
    name: str
    label: str
    adapter_factory: Any
    check_fn: Any
    trusted_inbound: bool = False
    display_tier: str = ""
    display_overrides: dict = field(default_factory=dict)
    platform_hint: str = ""
    max_message_length: int = 0
    emoji: str = ""
    standalone_sender_fn: Any = None
'''
# The middleware kinds of a Hermes that has the middleware of the guard of the direct chats.
MIDDLEWARE = '''
VALID_MIDDLEWARE = {"tool_request", "tool_execution", "llm_request", "llm_execution"}
'''


class Context:
    """The plugin context of Hermes: it records each registration."""

    def __init__(self, fail=None):
        self.platforms, self.tools, self.hooks, self.middleware, self.fail = [], [], [], {}, fail

    def register_platform(self, **options):
        if self.fail:
            raise self.fail
        self.platforms.append(options)

    def register_tool(self, **options):
        self.tools.append(options['name'])

    def register_hook(self, name, callback):
        self.hooks.append(name)

    def register_middleware(self, name, callback):
        self.hooks.append(name)
        self.middleware[name] = callback


def setUpModule():
    global STUBS, plugin, team, privacy
    STUBS = tempfile.TemporaryDirectory()
    with mock.patch.dict(test_gateway_adapter.STUBS, {'gateway/platforms/event.py': EVENT,
                                                       'gateway/platform_registry.py': REGISTRY,
                                                       'hermes_cli/middleware.py': MIDDLEWARE}):
        plugin, _ = test_gateway_adapter.load_plugin(Path(STUBS.name))
    import hermes_plugins.coconut.privacy as privacy
    import hermes_plugins.coconut.team as team


def tearDownModule():
    sys.path.remove(STUBS.name)
    STUBS.cleanup()


def team_mode(config=None):
    import hermes_cli.config
    value = {'gateway': {'platforms': {'coconut': {'enabled': True, 'extra': {'mode': 'team'}}}}}
    return mock.patch.object(hermes_cli.config, 'CONFIG', value if config is None else config)


class RegistrationTests(unittest.TestCase):
    def test_the_team_mode_registers_the_guard_and_the_platform_and_nothing_else(self):
        context = Context()
        with team_mode():
            plugin.register(context)
        self.assertEqual((context.tools, context.hooks), ([], ['tool_execution']))
        self.assertIs(context.middleware['tool_execution'], privacy.guard)
        (options,) = context.platforms
        self.assertEqual((options['name'], options['trusted_inbound'], options['platform_hint']),
                         ('coconut', True, plugin.TEAM_HINT))
        # The hint holds for each chat of a team, a direct chat too. The rules of each chat say who reads.
        from coconut import team_turns
        self.assertEqual(plugin.TEAM_HINT, 'You are in a chat of a team in Coconut. The rules of the chat say who can '
                                           'write to you and who reads your reply.')
        self.assertIn('Each member of the team can write to you, and the whole team reads your reply.',
                      team_turns.RULES)
        self.assertNotIn('whole team', team_turns.RULES_DIRECT)
        self.assertIs(options['adapter_factory'], team.TeamAdapter)
        self.assertEqual(options['display_overrides'], plugin.DISPLAY)
        self.assertIs(options['standalone_sender_fn'], team.send_standalone)
        self.assertIsNone(team.TeamAdapter.lacking)
        # Without the mode, the plugin registers what it registers for a full install, and no sender for a cron run
        # without a gateway: the service of a person's Mac needs the job ID of a delivery.
        full = Context()
        plugin.register(full)
        self.assertEqual((len(full.platforms), len(full.tools)), (1, 29))
        self.assertNotIn('standalone_sender_fn', full.platforms[0])
        self.assertIn('pre_tool_call', full.hooks)

    def test_a_hermes_without_a_field_of_the_adapter_gets_one_log_line_and_no_channel(self):
        @dataclasses.dataclass
        class OlderEvent:
            text: str
            source: object = None
        context = Context()
        with team_mode(), mock.patch.object(team, 'MessageEvent', OlderEvent), \
                self.assertLogs('hermes_plugins.coconut', logging.WARNING) as logs:
            plugin.register(context)
        # The guard stays for the other platforms of the profile, which can read the sessions of the direct chats.
        self.assertEqual((context.platforms, context.tools, context.hooks), ([], [], ['tool_execution']))
        self.assertEqual(len(logs.records), 1)
        self.assertIn('channel_context', logs.output[0])

    def test_a_hermes_without_a_field_of_the_registration_gets_a_channel_that_takes_no_message(self):
        import gateway.platform_registry

        @dataclasses.dataclass
        class OlderEntry:
            name: str
            label: str
            adapter_factory: object
            check_fn: object
            display_tier: str = ''
            platform_hint: str = ''
        self.addCleanup(setattr, team.TeamAdapter, 'lacking', None)
        context = Context()
        with team_mode(), mock.patch.object(gateway.platform_registry, 'PlatformEntry', OlderEntry), \
                self.assertLogs('hermes_plugins.coconut', logging.WARNING) as logs:
            plugin.register(context)
        (options,) = context.platforms
        self.assertIs(options['adapter_factory'], team.TeamAdapter)
        self.assertNotIn('trusted_inbound', options)
        # A Hermes without the field of the sender registers the channel without it.
        self.assertNotIn('standalone_sender_fn', options)
        self.assertEqual(team.TeamAdapter.lacking, 'trusted_inbound, display_overrides')
        self.assertEqual(logs.output, ['WARNING:hermes_plugins.coconut:Coconut: the team chat takes no message, '
                                       'because this Hermes lacks trusted_inbound, display_overrides. Update Hermes.'])

    def test_a_hermes_that_raises_gets_one_log_line_and_no_channel(self):
        for fault in ('register', 'import'):
            with self.subTest(fault=fault):
                context = Context(fail=RuntimeError('boom') if fault == 'register' else None)
                modules = {'gateway.platform_registry': None} if fault == 'import' else {}
                with team_mode(), mock.patch.dict(sys.modules, modules), \
                        self.assertLogs('hermes_plugins.coconut', logging.WARNING) as logs:
                    plugin.register(context)
                self.assertEqual((context.platforms, context.tools, context.hooks),
                                 ([], [], ['tool_execution'] if fault == 'register' else []))
                self.assertEqual(len(logs.records), 1)
                self.assertIn('the team chat stays off', logs.output[0])

    def test_a_hermes_without_the_middleware_of_the_guard_gets_a_channel_that_takes_no_message(self):
        # Without the guard, a turn of each chat can read each direct chat: also the Team chat must not run.
        import hermes_cli.middleware
        for case in ('kind', 'method', 'module'):
            with self.subTest(case=case):
                self.addCleanup(setattr, team.TeamAdapter, 'lacking', None)
                context = Context()
                if case == 'method':
                    context.register_middleware = None
                patches = {'kind': mock.patch.object(hermes_cli.middleware, 'VALID_MIDDLEWARE', {'llm_request'}),
                           'method': mock.patch.dict(sys.modules, {}),
                           'module': mock.patch.dict(sys.modules, {'hermes_cli.middleware': None})}
                with team_mode(), patches[case], self.assertLogs('hermes_plugins.coconut', logging.WARNING) as logs:
                    plugin.register(context)
                (options,) = context.platforms
                self.assertIs(options['adapter_factory'], team.TeamAdapter)
                self.assertEqual(context.hooks, [])
                self.assertEqual(team.TeamAdapter.lacking, 'tool_execution')
                self.assertEqual(logs.output, ['WARNING:hermes_plugins.coconut:Coconut: the team chat takes no '
                                               'message, because this Hermes lacks tool_execution. Update Hermes.'])


# The chats of the agent Reporter in the North team, as team_turns.chat_id makes them.
TEAM_CHAT = chat_id('room-north', 'reporter')
NAMED_CHAT = chat_id('room-north', 'reporter', 'entry-design')
ADA_DIRECT = chat_id('room-north', 'reporter', direct='member-ada')
GRACE_DIRECT = chat_id('room-north', 'reporter', direct='member-grace')
# The rows of `sessions`: (id, source, chat_id, parent_session_id).
SESSIONS = [('team-grace', 'coconut', TEAM_CHAT, None), ('named-grace', 'coconut', NAMED_CHAT, None),
            ('ada', 'coconut', ADA_DIRECT, None), ('ada-later', 'coconut', ADA_DIRECT, 'ada'),
            ('ada-child', 'subagent', None, 'ada'), ('grace', 'coconut', GRACE_DIRECT, None),
            ('cli', 'cli', None, None), ('cron', 'cron', None, None), ('telegram', 'telegram', '42', None),
            ('nobody', 'coconut', None, None), ('loop-a', 'subagent', None, 'loop-b'),
            ('loop-b', 'subagent', None, 'loop-a')]


class DirectChatGuardTests(unittest.TestCase):
    """The guard of the direct chats (privacy.guard) against a `sessions` table of its own. No Hermes runs."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patch = mock.patch.dict(os.environ, {'HERMES_HOME': self.home.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.table('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, chat_id TEXT, '
                   'parent_session_id TEXT)', SESSIONS)
        self.calls = []

    def table(self, create, rows=()):
        connection = sqlite3.connect(Path(self.home.name) / 'state.db')
        with connection:
            connection.execute(create)
            connection.executemany('INSERT INTO sessions VALUES (?, ?, ?, ?)', rows)
        connection.close()

    def call(self, session, tool='session_search', result=None, **args):
        """Call the guard as Hermes does, with a tool that records its call and returns `result`."""
        def tool_call(next_args):
            self.calls.append(next_args)
            return result if result is not None else json.dumps({'success': True, 'mode': 'read',
                                                                  'session_id': next_args['session_id'].strip()})
        return privacy.guard(tool_name=tool, args=args, next_call=tool_call, session_id=session,
                             original_args=dict(args), task_id='', tool_call_id='call-1', turn_id='')

    def refused(self, answer, text=None):
        self.assertEqual(json.loads(answer), {'success': False, 'error': text or privacy.REFUSED})

    @staticmethod
    def found(mode, *sessions, **extra):
        results = [dict(session_id=session, title=f'Title {session}', **({'parent_session_id': parent}
                                                                         if parent else {}))
                   for session, parent in sessions]
        return json.dumps({'success': True, 'mode': mode, 'results': results, 'count': len(results), **extra})

    def test_the_chat_ids_of_the_service_are_the_direct_chats_of_the_guard(self):
        self.assertTrue(privacy.is_direct(ADA_DIRECT))
        self.assertFalse(privacy.is_direct(TEAM_CHAT))
        self.assertFalse(privacy.is_direct(NAMED_CHAT))
        self.assertFalse(privacy.is_direct(None))
        self.assertFalse(privacy.is_direct('42'))

    def test_a_read_of_a_direct_chat_works_only_in_that_chat(self):
        for session in ('team-grace', 'named-grace', 'grace', 'cli', 'cron', 'telegram', 'nobody', 'unknown', ''):
            for target in ('ada', 'ada-later', 'ada-child', 'nobody', 'unknown'):
                with self.subTest(session=session, target=target):
                    self.refused(self.call(session, session_id=target))
                    self.refused(self.call(session, session_id=target, around_message_id=3))
        self.assertEqual(self.calls, [], 'A refused call does not reach the tool.')
        # Ada's own chat, its later session and a subagent of it reach each session of her chat.
        for session in ('ada', 'ada-later', 'ada-child'):
            for target in ('ada', 'ada-later', 'ada-child'):
                with self.subTest(session=session, target=target):
                    self.assertEqual(json.loads(self.call(session, session_id=target))['session_id'], target)
        # The other chats stay readable from each chat.
        for session in ('ada', 'grace', 'team-grace', 'cli'):
            for target in ('team-grace', 'named-grace', 'cli', 'cron', 'telegram'):
                with self.subTest(session=session, target=target):
                    self.assertEqual(json.loads(self.call(session, session_id=f' {target} '))['session_id'], target)

    def test_a_read_whose_result_names_a_direct_chat_of_another_chat_is_refused(self):
        # Scroll can move to a child session of the same lineage. The guard checks the session of the result again.
        moved = json.dumps({'success': True, 'mode': 'scroll', 'session_id': 'ada', 'messages': []})
        with self.assertLogs('hermes_plugins.coconut.privacy', logging.WARNING):
            self.refused(self.call('team-grace', session_id='team-grace', around_message_id=3, result=moved))
        self.assertEqual(len(self.calls), 1)

    def test_discover_and_browse_return_no_direct_chat_of_another_chat(self):
        discover = self.found('discover', ('team-grace', None), ('ada', None), ('ada-later', 'ada'),
                              ('ada-child', 'ada'), ('telegram', None), ('nobody', None), ('grace', None),
                              query='door', detail='adaptive', sessions_searched=7, link_hint='Write the link.',
                              index_rebuild={'percent': 50})
        answer = json.loads(self.call('team-grace', query='door', result=discover))
        self.assertEqual([item['session_id'] for item in answer['results']], ['team-grace', 'telegram'])
        self.assertEqual((answer['count'], answer['sessions_searched'], answer['query'], answer['link_hint'],
                          answer['index_rebuild']), (2, 2, 'door', 'Write the link.', {'percent': 50}))
        self.assertNotIn('Title ada', json.dumps(answer))
        # Ada's own chat finds her sessions, and no session of Grace's direct chat.
        answer = json.loads(self.call('ada', query='door', result=discover))
        self.assertEqual([item['session_id'] for item in answer['results']],
                         ['team-grace', 'ada', 'ada-later', 'ada-child', 'telegram'])
        browse = self.found('browse', ('ada', None), ('cli', None), message='Showing 2 most recent sessions.')
        answer = json.loads(self.call('cli', result=browse))
        self.assertEqual(answer, {'success': True, 'mode': 'browse', 'results': [answer['results'][0]], 'count': 1,
                                  'message': 'Showing 1 most recent sessions. Pass a query= to search, or '
                                             'session_id+around_message_id to scroll.'})
        self.assertEqual(answer['results'][0]['session_id'], 'cli')
        # A result without a direct chat of another chat keeps each key that the guard knows, and loses the others.
        self.assertEqual(json.loads(self.call('ada', result=browse)), json.loads(browse))
        found = self.found('discover', ('cli', None), query='door', sessions_searched=1)
        later = json.dumps(dict(json.loads(found), related=[{'session_id': 'ada', 'preview': 'My door code'}]))
        self.assertEqual(json.loads(self.call('team-grace', query='door', result=later)), json.loads(found))

    def test_profile_and_unknown_arguments_are_refused(self):
        for args in ({'profile': 'default'}, {'profile': 'default', 'session_id': 'team-grace'},
                     {'session_id': 'default/team-grace'}, {'session_id': 7}, {'session_id': ['ada']},
                     {'query': 'door', 'scope': 'all'}, {'source': 'coconut'}):
            with self.subTest(args=args):
                self.refused(self.call('team-grace', **args))
        self.assertEqual(self.calls, [])
        # A blank profile is no profile for Hermes.
        self.assertEqual(json.loads(self.call('team-grace', session_id='cli', profile=' '))['session_id'], 'cli')

    def test_a_result_that_the_guard_cannot_read_is_refused(self):
        for result in ('not json', json.dumps([]), json.dumps({'success': True}), json.dumps({'mode': 'browse'}),
                       json.dumps({'success': True, 'mode': 'later', 'results': []}),
                       json.dumps({'success': True, 'mode': 'browse', 'results': {}}),
                       json.dumps({'success': True, 'mode': 'browse', 'results': ['cli']}),
                       json.dumps({'success': True, 'mode': 'read'}), json.dumps({'success': False})):
            with self.subTest(result=result), self.assertLogs('hermes_plugins.coconut.privacy', logging.WARNING):
                self.refused(self.call('team-grace', query='door', result=result))
        # A list of sessions for a read.
        with self.assertLogs('hermes_plugins.coconut.privacy', logging.WARNING):
            self.refused(self.call('team-grace', session_id='cli', result=self.found('browse', ('cli', None))))

    def test_a_fault_of_the_guard_refuses_the_call(self):
        # A store that does not read: no state.db, and a `sessions` table without the chat of a session.
        for case in ('missing', 'columns'):
            with self.subTest(store=case):
                (Path(self.home.name) / 'state.db').unlink(missing_ok=True)
                if case == 'columns':
                    self.table('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, title TEXT)',
                               [('team-grace', 'coconut', 'stub', 'Plans')])
                with self.assertLogs('hermes_plugins.coconut.privacy', logging.WARNING):
                    self.refused(self.call('team-grace', query='door', result=self.found('browse', ('cli', None))))
                    self.refused(self.call('team-grace', session_id='cli'))
                self.assertEqual(self.calls, [], 'A refused call does not reach the tool.')
        # Another tool does not need the store.
        self.assertEqual(self.call('team-grace', tool='terminal', result='done', command='ls'), 'done')

    def test_an_error_of_the_tool_reaches_the_model_as_an_error(self):
        error = json.dumps({'error': 'around_message_id 9 not in session_id cli', 'success': False})
        self.refused(self.call('team-grace', session_id='cli', around_message_id=9, result=error),
                     'around_message_id 9 not in session_id cli')
        self.refused(self.call('team-grace', query='door', result=json.dumps({'error': 'Search failed'})),
                     'Search failed')

    def turn(self, platform, chat):
        """The context of a gateway turn of Hermes, as gateway.session_context keeps it."""
        from gateway.session_context import SESSION
        token = SESSION.set({'HERMES_SESSION_PLATFORM': platform, 'HERMES_SESSION_CHAT_ID': chat})
        self.addCleanup(SESSION.reset, token)

    def write(self, session, tool='memory'):
        return self.call(session, tool=tool, result='saved', action='add', content='A fact.')

    def test_a_direct_chat_writes_no_memory_and_no_skill(self):
        for tool in ('memory', 'skill_manage'):
            # A turn of a direct chat, a subagent of it, the review after it (it has the session ID of the turn),
            # and a `coconut` session without a chat ID.
            for session in ('ada', 'ada-later', 'ada-child', 'grace', 'nobody'):
                with self.subTest(tool=tool, session=session):
                    self.refused(self.call(session, tool=tool, action='add', content='The door code is 7731.'),
                                 privacy.WRITES[tool])
            self.assertEqual(self.calls, [])
            # The Team chat, a named chat, the other platforms, and each session that the guard does not know as a
            # chat keep their writes: a session without a row (the curator), no session ID, and a loop of parents.
            for session in ('team-grace', 'named-grace', 'cli', 'cron', 'telegram', 'unknown', '', 'loop-a'):
                with self.subTest(tool=tool, session=session):
                    self.assertEqual(self.call(session, tool=tool, result='saved', action='add', content='A fact.'),
                                     'saved')
                    self.assertEqual(self.calls.pop(), {'action': 'add', 'content': 'A fact.'})

    def test_the_chat_of_the_gateway_turn_also_marks_a_direct_chat(self):
        # Hermes keeps the chat of the turn for the turn, its review and its subagents. The guard refuses a write when
        # it names a direct chat, also when the row does not.
        self.turn('coconut', ADA_DIRECT)
        for session in ('unknown', 'team-grace', ''):
            with self.subTest(session=session):
                self.refused(self.write(session), privacy.WRITES['memory'])
        self.assertEqual(self.calls, [])
        self.turn('coconut', TEAM_CHAT)
        self.assertEqual(self.write('unknown'), 'saved')
        self.refused(self.write('ada'), privacy.WRITES['memory'])

    def test_a_fault_of_the_store_on_a_write(self):
        patch = mock.patch.object(privacy, 'RETRY_SECONDS', 0)
        patch.start()
        self.addCleanup(patch.stop)
        real = privacy.store
        # One fault: the second read decides.
        for session, refused in (('ada', True), ('cli', False)):
            with self.subTest(session=session), mock.patch.object(
                    privacy, 'store', side_effect=[sqlite3.OperationalError('database is locked'), real()]) as store:
                answer = self.write(session)
                self.assertEqual(store.call_count, 2)
                if refused:
                    self.refused(answer, privacy.WRITES['memory'])
                else:
                    self.assertEqual(answer, 'saved')
        # Two faults: a write outside a Coconut turn (cron, the command line, the curator) and a write of a Coconut
        # turn of a known other chat run. A Coconut turn without its chat is refused with a true message.
        failing = mock.patch.object(privacy, 'store', side_effect=sqlite3.OperationalError('database is locked'))
        failing.start()
        self.addCleanup(failing.stop)
        for platform, chat, refused in (('', '', False), ('cron', '', False), ('coconut', TEAM_CHAT, False),
                                        ('coconut', '', True)):
            with self.subTest(platform=platform, chat=chat):
                self.turn(platform, chat)
                with self.assertLogs('hermes_plugins.coconut.privacy', logging.WARNING) as logs:
                    answer = self.write('cron', tool='skill_manage')
                if refused:
                    self.refused(answer, 'Coconut could not check the chat of this turn, so it refused this skill '
                                         'change. Try it again later.')
                    self.assertIn('so it refused the call', logs.output[0])
                else:
                    self.assertEqual(answer, 'saved')
                    self.assertIn('so it let the call run', logs.output[0])
        # A direct chat of the gateway turn needs no store.
        self.turn('coconut', ADA_DIRECT)
        self.refused(self.write('cron'), privacy.WRITES['memory'])

    def test_another_tool_passes_untouched(self):
        self.assertEqual(self.call('ada', tool='terminal', result='done', command='ls'), 'done')
        self.assertEqual(self.calls, [{'command': 'ls'}])


def listen(test):
    """Serve the routes of the team-only Mac of `test` on a loopback listener, and write its bridge file."""
    test.server = APIServer(('127.0.0.1', 0), test.only)
    threading.Thread(target=test.server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
    test.addCleanup(test.server.server_close)
    test.addCleanup(test.server.shutdown)
    test.only.gateway_bridge.publish(f'http://127.0.0.1:{test.server.server_address[1]}')


class TeamAdapterTests(TeamTurnMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        listen(self)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.addCleanup(lambda: self.loop.call_soon_threadsafe(self.loop.stop))
        self.adapter = None

    def complete(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(10)

    def connect(self, runner=None, **options):
        if runner is None:
            return super().connect(**options)
        from gateway.config import PlatformConfig
        self.adapter = team.TeamAdapter(PlatformConfig({'bridge': str(self.only.gateway_bridge.address_file),
                                                        'mode': 'team'}))
        self.adapter.set_message_handler(runner)
        with mock.patch.dict(os.environ, HERMES_HOME=str(self.home)):
            self.assertTrue(self.complete(self.adapter.connect()))
        self.addCleanup(lambda: self.complete(self.adapter.disconnect()))
        deadline = time.monotonic() + 5
        while not self.only.gateway_bridge.connected(self.home) and time.monotonic() < deadline:
            time.sleep(.02)

    def record_polls(self):
        """Record the body of each poll that reaches the bridge."""
        polls, poll = [], self.only.gateway_bridge.poll
        self.only.gateway_bridge.poll = lambda data: (polls.append(dict(data)), poll(data))[1]
        return polls

    def test_an_adapter_of_a_hermes_that_lacks_a_field_takes_no_turn_and_names_it_in_each_poll(self):
        from coconut.team_turns import OUTDATED

        async def runner(event):
            return 'Three tasks are open.'
        polls = self.record_polls()
        with mock.patch.object(team.TeamAdapter, 'lacking', 'trusted_inbound'):
            self.connect(runner)
            named = self.send('@Reporter what is open?', mentions=[self.mention])
            self.sync()
            self.run_turns()
            self.assertEqual(self.adapter.events, [])
            self.assertEqual(self.chat()[-1], ('Reporter', OUTDATED, named['id']))
            # A turn that reaches the adapter anyway does not reach Hermes: the poll skips it.
            with self.assertRaises(ValueError):
                self.adapter._event({'id': 'turn', 'chat_id': 'b:team:r', 'text': 'Hello',
                                     'team': {'member': 'm', 'member_name': 'Ada'}})
        self.assertTrue(polls)
        self.assertEqual({poll.get('lacking') for poll in polls}, {'trusted_inbound'})

    def test_a_turn_is_a_group_message_and_its_reply_reaches_the_chat(self):
        async def runner(event):
            return 'Three tasks are open.'
        polls = self.record_polls()
        self.connect(runner)
        self.send('Yesterday we shipped the export.')
        named = self.send('@Reporter what is open?', mentions=[self.mention])
        self.sync()
        self.run_turns()
        (event,) = self.adapter.events
        ada = self.first.team_store.room(self.room)['member']
        source = event.source
        self.assertEqual((source.chat_id, source.chat_name, source.chat_type, source.user_id, source.user_name),
                         (f'{self.bot["id"]}:team:{self.room}', 'Example Team', 'group', ada, 'Ada'))
        self.assertEqual((event.text, event.user_id, event.allow_gateway_control, event.reply_expected),
                         ('@Reporter what is open?', ada, False, True))
        self.assertIn('> Yesterday we shipped the export.', event.channel_context)
        self.assertIn('Example Team', event.channel_prompt)
        self.assertIsNone(event.reply_to_message_id)
        self.assertEqual(self.chat()[-1], ('Reporter', 'Three tasks are open.', named['id']))
        # A reply to the bot names the message of the bot as its own.
        bot_message = self.call('GET', f'/v1/team/rooms/{self.room}/messages', service=self.first)['messages'][0]
        self.send('Which one first?', reply_to=bot_message['id'])
        self.sync()
        self.run_turns()
        event = self.adapter.events[-1]
        self.assertEqual((event.reply_to_message_id, event.reply_to_text, event.reply_to_is_own_message),
                         (bot_message['id'], 'Three tasks are open.', True))
        self.assertEqual(self.adapter._turns, {})
        self.assertEqual(self.complete(self.adapter.get_chat_info(source.chat_id))['type'], 'group')
        # A Hermes with each field of the registration: no poll names a lack.
        self.assertTrue(polls)
        self.assertFalse(any('lacking' in poll for poll in polls))

    def test_a_turn_of_a_direct_chat_is_a_group_chat_of_its_own(self):
        async def runner(event):
            return None
        self.connect(runner)
        chat = f'{self.bot["id"]}:team:{self.room}:direct:{"a" * 64}'
        # A `dm` chat gets a first-contact note of Hermes, so a direct chat is a group chat with its own chat ID.
        event = self.adapter._event({'id': 'turn-1', 'chat_id': chat, 'chat_name': 'Ada · Example Team',
                                     'text': 'Hello', 'team': {'member': 'a' * 64, 'member_name': 'Ada',
                                                               'direct': True}})
        self.assertEqual((event.source.chat_id, event.source.chat_type), (chat, 'group'))
        self.assertEqual(self.complete(self.adapter.get_chat_info(chat)),
                         {'name': 'Ada · Example Team', 'type': 'group', 'chat_id': chat})

    def test_an_approval_is_denied_and_a_clarify_question_goes_into_the_chat(self):
        from types import SimpleNamespace
        import tools.approval
        import tools.clarify_gateway

        async def runner(event):
            chat = event.source.chat_id
            prompt = SimpleNamespace(chat_id=chat, session_key='session-1', description='Run rm -rf build?',
                                     command='rm -rf build', actions=[('Once', 'once', None), ('Deny', 'deny', None)])
            await self.adapter._send_exec_approval_prompt(prompt)
            await self.adapter.send_clarify(chat, 'Which build?', ['Debug', 'Release'], 'clarify-1', 'session-1')
            deadline = time.monotonic() + 5
            while len(tools.approval.calls) + len(tools.clarify_gateway.calls) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(.05)
            return 'I asked in the chat.'
        tools.approval.calls.clear()
        tools.clarify_gateway.calls.clear()
        self.connect(runner)
        named = self.send('@Reporter clean the build', mentions=[self.mention])
        self.sync()
        self.run_turns()
        self.assertEqual(tools.approval.calls, [('session-1', 'deny', DENIED)])
        self.assertEqual(tools.clarify_gateway.calls, [('clarify-1', ASKED)])
        replies = [(text, to) for writer, text, to in self.chat() if writer == 'Reporter']
        self.assertEqual(replies, [('Which build?\n- Debug\n- Release', named['id']), ('I asked in the chat.', None)])

    def start_slow_turn(self):
        """A turn whose run waits in Hermes until the test releases it. Return the release event and the event."""
        release = threading.Event()

        async def runner(event):
            if event.message_type.value == 'text':
                await asyncio.to_thread(release.wait, 60)
            return None
        self.connect(runner)
        self.addCleanup(release.set)
        self.send('@Reporter slow', mentions=[self.mention])
        self.sync()
        runner_thread = threading.Thread(target=self.only._turns_run, args=(self.room, self.bot['id']), daemon=True)
        runner_thread.start()
        deadline = time.monotonic() + 5
        while not self.adapter._turns and time.monotonic() < deadline:
            time.sleep(.05)
        (chat, turn), = self.adapter._turns.items()
        return release, chat, turn

    def stops(self):
        return [event for event in self.adapter.events if event.text == '/stop']

    def test_a_turn_that_a_new_service_does_not_know_stops_with_the_source_of_its_member(self):
        release, chat, turn = self.start_slow_turn()
        # The service restarts: the new process has no turn, and the adapter lists it in its next poll.
        with self.only.gateway_bridge.lock:
            self.only.gateway_bridge.turns.pop(turn)
        deadline = time.monotonic() + 30
        while not self.stops() and time.monotonic() < deadline:
            time.sleep(.05)
        (stop,) = self.stops()
        ada = self.first.team_store.room(self.room)['member']
        self.assertEqual((stop.message_id, stop.source.user_id, stop.source.chat_id), (turn + ':stop', ada, chat))
        # A late send of the stopped turn, and the answer of Hermes to its /stop, do not reach the chat.
        with mock.patch.object(self.only, 'gateway_message', side_effect=AssertionError('A late send reached Coconut.')):
            self.assertTrue(self.complete(self.adapter.send(chat, 'Late reply.', reply_to=turn)).success)
            self.assertTrue(self.complete(self.adapter.send(chat, 'Stopped.', reply_to=turn + ':stop')).success)
        release.set()

    def test_a_turn_stops_when_the_service_cannot_be_reached_for_long(self):
        release, chat, turn = self.start_slow_turn()
        # The service is gone: the listener closes, and nothing answers.
        self.server.shutdown()
        self.server.server_close()
        with mock.patch.object(team, 'LOST_SECONDS', 1.5):
            deadline = time.monotonic() + 30
            while not self.stops() and time.monotonic() < deadline:
                time.sleep(.1)
        self.assertEqual([stop.message_id for stop in self.stops()], [turn + ':stop'])
        release.set()

    def test_a_turn_that_comes_again_runs_once(self):
        runs = []

        async def runner(event):
            runs.append(event.message_id)
            await asyncio.sleep(.3)
            return None
        self.connect(runner)
        turn = {'id': 'turn-1', 'chat_id': 'b:team:r', 'chat_name': 'Example Team', 'text': 'Hello',
                'team': {'member': 'm', 'member_name': 'Ada'}}
        event = self.adapter._event(turn)
        with self.assertLogs('hermes_plugins.coconut', 'WARNING'):
            self.complete(self.adapter.handle_message(event))
            self.complete(self.adapter.handle_message(self.adapter._event(turn)))
            time.sleep(.6)
        self.assertEqual(runs, ['turn-1'])

    def test_a_fault_in_an_entry_point_stays_in_the_adapter(self):
        async def runner(event):
            return None
        self.connect(runner)

        async def broken(*arguments, **options):
            raise RuntimeError('boom')
        with mock.patch.object(self.adapter, '_report', broken), \
                self.assertLogs('hermes_plugins.coconut', logging.ERROR):
            result = self.complete(self.adapter.send('chat', 'text'))
            self.assertFalse(result.success)
            self.assertFalse(self.complete(self.adapter.delete_message('chat', 'turn:1')))
        # A turn without its fields is skipped, and the poll goes on.
        with self.assertRaises(ValueError):
            self.adapter._event({'id': 'turn', 'chat_id': 'chat', 'text': 'Hello'})
        # A photo file gets the notice of Hermes, which goes to the chat as text. No file leaves the Mac. Outside a
        # turn, the service refuses a chat that is not a team chat of this home.
        photo = Path(self.temp.name) / 'photo.png'
        photo.write_bytes(b'png')
        self.assertFalse(self.complete(self.adapter.send_image_file('chat', str(photo))).success)
        self.assertEqual(list(self.only.root.glob('**/photo.png')), [])

    def test_a_cron_send_that_the_chat_does_not_show_fails_and_another_send_does_not(self):
        async def runner(event):
            return None
        self.connect(runner)
        self.connect(chat=False)
        chat = chat_id(self.room, self.bot['id'])
        with self.assertLogs('hermes_plugins.coconut', logging.INFO):
            cron = self.complete(self.adapter.send(chat, 'The weekly report is ready.', metadata={'job_id': 'job1'}))
            other = self.complete(self.adapter.send(chat, 'The process is done.'))
        # The job records a failed delivery. Hermes then tries send_standalone, as nothing shows in the chat.
        self.assertFalse(cron.success)
        self.assertEqual(cron.error, 'Coconut did not show this message: The agent does not answer in this team chat.')
        self.assertEqual(cron.raw_response, {'coconut_refused': True})
        # A send that is not a cron delivery stays a success, as before.
        self.assertTrue(other.success)

    def test_a_cron_send_with_only_some_parts_in_the_log_is_a_partial_delivery(self):
        from coconut.team_turns import MAX_TEXT

        async def runner(event):
            return None
        self.connect(runner)
        real, writes = self.only.team_relay.write, []

        def first_only(*arguments, **options):
            writes.append(arguments)
            if len(writes) > 1:
                raise relay_error(503)
            return real(*arguments, **options)
        chat = chat_id(self.room, self.bot['id'])
        with mock.patch.object(team, 'STANDALONE_WAITS', (.01, .01)), \
                mock.patch.object(self.only.team_relay, 'write', side_effect=first_only), \
                self.assertLogs('hermes_plugins.coconut', logging.WARNING):
            result = self.complete(self.adapter.send(chat, 'a' * MAX_TEXT + 'b', metadata={'job_id': 'job1'}))
        # Three tries with one message ID: the second part never reaches the log.
        self.assertEqual(len(writes), 4)
        self.assertFalse(result.success)
        self.assertEqual(result.error, 'Coconut did not show this message: The chat shows only 1 of 2 parts of this '
                                       'message.')
        # Hermes sends nothing again after a partial delivery (BasePlatformAdapter._is_partial_delivery).
        self.assertEqual(result.raw_response, {'coconut_refused': True, 'partial_overflow': True})

    def test_only_the_team_adapter_says_that_it_splits_long_messages(self):
        # Hermes cuts a cron delivery over 4,000 characters for an adapter without the mark (gateway/delivery.py).
        self.assertIs(team.TeamAdapter.splits_long_messages, True)
        self.assertFalse(getattr(team.CoconutAdapter, 'splits_long_messages', False))


# A relay write that the relay took, but whose answer did not arrive (StandaloneSenderTests.relay_faults).
LOST = object()


def relay_error(status=503):
    from coconut.team_relay import TeamRelayError
    return TeamRelayError(status, f'The relay answered {status}.')


def http_error(status):
    """An HTTP answer of the service with this status and an empty body."""
    import io
    import urllib.error
    return urllib.error.HTTPError('http://127.0.0.1/', status, 'Error', {}, io.BytesIO())


class StandaloneSenderTests(DirectTurnMacs, unittest.TestCase):
    """A cron delivery into a team chat from a process without a gateway (team.send_standalone)."""

    def setUp(self):
        super().setUp()
        listen(self)

    def deliver(self, chat, text='The weekly report is ready.', bridge=None, home=None, extra=None, **options):
        from gateway.config import PlatformConfig
        config = PlatformConfig(extra or {'bridge': str(bridge or self.only.gateway_bridge.address_file),
                                          'mode': 'team'})
        with mock.patch.dict(os.environ, HERMES_HOME=str(home or self.home)):
            return asyncio.run(team.send_standalone(config, chat, text, thread_id=None, **options))

    def requests(self, *faults):
        """Count the requests of the sender to the bridge, as (message ID, timeout). The request number n raises
        faults[n] when there is one, and else goes to the service."""
        calls, real = [], team.Bridge.request

        def request(bridge, route, payload, timeout):
            calls.append((payload['message_id'], timeout))
            if len(calls) <= len(faults):
                raise faults[len(calls) - 1]
            return real(bridge, route, payload, timeout)
        patch = mock.patch.object(team.Bridge, 'request', autospec=True, side_effect=request)
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def quick(self):
        """Waits of 0.1 seconds between the tries of the sender."""
        patch = mock.patch.object(team, 'STANDALONE_WAITS', (.1, .1))
        patch.start()
        self.addCleanup(patch.stop)

    def outside(self, text, message_id='outside:report', chat=None):
        """A send outside a turn into the Team chat, or into `chat`, as the service gets it."""
        return self.only.gateway_message({'kind': 'send', 'home': str(self.home), 'message_id': message_id,
                                          'chat_id': chat or chat_id(self.room, self.bot['id']), 'text': text})

    def relay_faults(self, *faults):
        """Let the relay writes of the team-only Mac fail. The write number n raises faults[n] when it is an error.
        LOST writes the entry and then raises an error without an answer, as a relay whose answer did not arrive.
        The later writes go to the relay. Return the list of all writes."""
        from coconut.team_relay import TeamRelayError
        writes, real = [], self.only.team_relay.write

        def write(*arguments, **options):
            writes.append(arguments)
            fault = faults[len(writes) - 1] if len(writes) <= len(faults) else None
            if fault is LOST:
                real(*arguments, **options)
                raise TeamRelayError(0, 'The relay did not answer.')
            if fault is not None:
                raise fault
            return real(*arguments, **options)
        patch = mock.patch.object(self.only.team_relay, 'write', side_effect=write)
        patch.start()
        self.addCleanup(patch.stop)
        return writes

    def sent(self, result):
        self.assertEqual(set(result), {'success', 'message_id'})
        self.assertTrue(result['success'])
        self.assertTrue(result['message_id'].startswith('outside:'))

    def named(self, name='Design'):
        chat = self.call('POST', f'/v1/team/rooms/{self.room}/chats', {'name': name}, service=self.first)['chat']
        self.sync()
        return chat

    def of_chat(self, chat=None):
        rows = self.call('GET', f'/v1/team/rooms/{self.room}/messages', {'chat': chat} if chat else {},
                         service=self.first)['messages']
        return [(row['bot_name'] or row['member_name'], row['text']) for row in reversed(rows)]

    def test_a_delivery_to_the_team_chat_a_named_chat_and_a_direct_chat_is_a_message_of_the_bot(self):
        design = self.named()
        with self.assertLogs('hermes_plugins.coconut', logging.DEBUG) as logs:
            logging.getLogger('hermes_plugins.coconut').debug('start')
            self.sent(self.deliver(chat_id(self.room, self.bot['id'])))
            self.sent(self.deliver(chat_id(self.room, self.bot['id'], design), 'Design notes are ready.'))
            self.sent(self.deliver(self.direct_chat_id(), 'Your summary is ready.'))
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'The weekly report is ready.')])
        self.assertEqual(self.of_chat(design), [('Reporter', 'Design notes are ready.')])
        self.assertEqual(self.bot_replies(), ['Your summary is ready.'])
        # No log line has the text, a name or the token of the bridge.
        token = json.loads(Path(self.only.gateway_bridge.address_file).read_text())['token']
        for word in ('ready', 'Ada', 'Reporter', 'Example Team', 'Design', token):
            self.assertNotIn(word, '\n'.join(logs.output))

    def test_a_refusal_or_an_unreachable_service_is_an_error_and_no_exception(self):
        design = self.named()
        self.call('DELETE', f'/v1/team/rooms/{self.room}/chats/{design}', service=self.first)
        self.sync()
        with self.assertLogs('hermes_plugins.coconut', logging.WARNING):
            refused = {
                'other home': self.deliver('nobody:team:' + self.room),
                'removed chat': self.deliver(chat_id(self.room, self.bot['id'], design)),
                'empty text': self.deliver(chat_id(self.room, self.bot['id']), ' \n'),
                'no service': self.deliver(chat_id(self.room, self.bot['id']), bridge=Path(self.temp.name) / 'none'),
            }
        self.assertEqual(refused['other home'], {'error': 'Coconut refused this message: the chat is not a team chat '
                                                          'of an agent of this Hermes home.'})
        self.assertEqual(refused['removed chat'], {'error': 'Coconut did not show this message: The agent does not '
                                                            'answer in this team chat.'})
        self.assertEqual(refused['empty text'], {'error': 'Coconut sends no empty message into a team chat.'})
        self.assertEqual(refused['no service'], {'error': 'The Coconut service did not answer.'})
        self.sync()
        self.assertEqual([row for row in self.of_chat() if row[0] == 'Reporter'], [])

    def test_files_with_a_text_send_the_text(self):
        with self.assertLogs('hermes_plugins.coconut', logging.WARNING) as logs:
            result = self.deliver(chat_id(self.room, self.bot['id']), media_files=[('/tmp/report.pdf', False)])
        # No `warnings` of the plugin. Hermes adds its own warning for the files (tools/send_message_tool.py), and the
        # cron record then shows a failed delivery although the text arrived.
        self.sent(result)
        self.assertEqual(logs.output, ['WARNING:hermes_plugins.coconut.team:Coconut: a cron delivery into a team chat '
                                       'sent its text without its 1 files.'])
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'The weekly report is ready.')])

    def test_a_service_that_does_not_answer_gets_three_tries_with_one_message_id(self):
        # A restart of the service, then a bridge file that is missing for a moment.
        self.quick()
        calls = self.requests(ConnectionRefusedError(), FileNotFoundError())
        with self.assertLogs('hermes_plugins.coconut', logging.WARNING):
            self.sent(self.deliver(chat_id(self.room, self.bot['id'])))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({message_id for message_id, _ in calls}), 1)
        self.assertEqual({timeout for _, timeout in calls}, {team.STANDALONE_SECONDS})
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'The weekly report is ready.')])

    def test_a_5xx_answer_three_times_is_an_error(self):
        self.quick()
        calls = self.requests(*[http_error(503)] * 3)
        result = self.deliver(chat_id(self.room, self.bot['id']))
        self.assertEqual(result, {'error': 'The Coconut service did not answer.'})
        self.assertEqual(len(calls), 3)

    def test_a_refusal_names_its_http_status_and_gets_no_second_try(self):
        calls = self.requests(http_error(403))
        with self.assertLogs('hermes_plugins.coconut', logging.WARNING):
            result = self.deliver(chat_id(self.room, self.bot['id']))
        self.assertEqual(result, {'error': 'Coconut refused this message. (HTTP 403)'})
        self.assertEqual(len(calls), 1)

    def test_a_fault_of_the_sender_is_an_error_and_no_exception(self):
        with mock.patch('hermes_constants.get_hermes_home', side_effect=RuntimeError('no home')), \
                self.assertLogs('hermes_plugins.coconut', logging.WARNING) as logs:
            result = self.deliver(chat_id(self.room, self.bot['id']))
        self.assertEqual(result, {'error': 'Coconut did not take this message (RuntimeError).'})
        self.assertEqual(logs.output, ['WARNING:hermes_plugins.coconut.team:Coconut: a cron delivery into a team chat '
                                       'failed (RuntimeError).'])

    def test_a_config_without_a_bridge_uses_the_default_bridge_file(self):
        with mock.patch.object(team, 'DEFAULT_BRIDGE', Path(self.only.gateway_bridge.address_file)):
            self.sent(self.deliver(chat_id(self.room, self.bot['id']), extra={'mode': 'team'}))
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'The weekly report is ready.')])

    def test_a_job_of_a_named_profile_delivers_as_the_agent_of_that_profile(self):
        # The cron process of a named profile has the profile home as its Hermes home.
        profile = Path(self.temp.name) / 'hermes' / 'profiles' / 'writer'
        profile.mkdir(parents=True)
        writer = self.declare('Writer', home=profile)
        self.share(self.room, writer)
        self.connect(bot=writer)
        self.sync()
        self.sent(self.deliver(chat_id(self.room, writer['id']), 'The draft is ready.', home=profile))
        # An agent that exists, but has another home: each home delivers only as its own agent.
        with self.assertLogs('hermes_plugins.coconut', logging.WARNING):
            refused = [self.deliver(chat_id(self.room, writer['id'])),
                       self.deliver(chat_id(self.room, self.bot['id']), home=profile)]
        self.assertEqual(refused, [{'error': 'Coconut refused this message: the chat is not a team chat of an agent '
                                             'of this Hermes home.'}] * 2)
        self.sync()
        self.assertEqual(self.of_chat(), [('Writer', 'The draft is ready.')])

    def test_a_slow_relay_gives_no_false_failure(self):
        # The first try waits less than the write takes. The next try waits for the lock of the room, finds the entry
        # of the first try, and the service answers `recorded: true`.
        self.quick()
        calls = self.requests()
        real = self.only._team_write

        def slow(*arguments, **options):
            time.sleep(1)
            return real(*arguments, **options)
        with mock.patch.object(team, 'STANDALONE_SECONDS', .6), mock.patch.object(self.only, '_team_write', slow), \
                self.assertLogs('hermes_plugins.coconut', logging.WARNING):
            self.sent(self.deliver(chat_id(self.room, self.bot['id'])))
        self.assertGreater(len(calls), 1)
        self.assertEqual(len({message_id for message_id, _ in calls}), 1)
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'The weekly report is ready.')])

    def test_a_repeat_with_the_same_message_id_adds_no_message(self):
        self.assertEqual(self.outside('The weekly report is ready.'), {'recorded': True})
        self.assertEqual(self.outside('The weekly report is ready.'), {'recorded': True})
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'The weekly report is ready.')])

    def test_a_relay_failure_is_not_recorded_and_a_repeat_can_cure_it(self):
        # One 503 of the relay: the relay answered, so the service makes no second write in the same call.
        writes = self.relay_faults(relay_error(503))
        with self.assertLogs('coconut', logging.WARNING):
            self.assertEqual(self.outside('The weekly report is ready.'),
                             {'recorded': False, 'reason': 'The team log did not take this message.', 'retry': True})
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.outside('The weekly report is ready.'), {'recorded': True})
        self.assertEqual(len(writes), 2)
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'The weekly report is ready.')])

    def test_the_sender_repeats_after_a_relay_failure_with_the_same_message_id(self):
        self.quick()
        writes, calls = self.relay_faults(relay_error(429)), self.requests()
        with self.assertLogs('coconut', logging.WARNING):
            self.sent(self.deliver(chat_id(self.room, self.bot['id'])))
        self.assertEqual((len(calls), len(writes)), (2, 2))
        self.assertEqual(len({message_id for message_id, _ in calls}), 1)
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'The weekly report is ready.')])

    def test_a_relay_that_fails_at_each_try_is_an_error_after_three_tries(self):
        self.quick()
        writes, calls = self.relay_faults(*[relay_error(503)] * 3), self.requests()
        with self.assertLogs('coconut', logging.WARNING):
            result = self.deliver(chat_id(self.room, self.bot['id']))
        self.assertEqual(result, {'error': 'Coconut did not show this message: The team log did not take this '
                                           'message.'})
        self.assertEqual((len(calls), len(writes)), (3, 3))
        self.sync()
        self.assertEqual(self.of_chat(), [])

    def test_a_refusal_with_recorded_false_gets_no_second_try(self):
        design = self.named()
        self.call('DELETE', f'/v1/team/rooms/{self.room}/chats/{design}', service=self.first)
        self.sync()
        self.quick()
        calls = self.requests()
        self.assertEqual(self.deliver(chat_id(self.room, self.bot['id'], design)),
                         {'error': 'Coconut did not show this message: The agent does not answer in this team chat.'})
        self.assertEqual(len(calls), 1)

    def test_a_relay_answer_that_did_not_arrive_is_cured_by_a_repeat_without_a_second_write(self):
        # The relay has the entry, but the Mac got no answer (`_team_unsure`). The repeat reads the room first.
        writes = self.relay_faults(LOST)
        with self.assertLogs('coconut', logging.WARNING):
            self.assertEqual(self.outside('The weekly report is ready.'),
                             {'recorded': False, 'reason': 'The team log did not take this message.', 'retry': True})
        self.assertEqual(self.outside('The weekly report is ready.'), {'recorded': True})
        self.assertEqual(len(writes), 1)
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'The weekly report is ready.')])

    def test_a_relay_failure_in_a_named_chat_and_a_direct_chat(self):
        design = self.named()
        named, direct = chat_id(self.room, self.bot['id'], design), self.direct_chat_id()
        writes = self.relay_faults(relay_error(503), relay_error(503))
        # Each send has its own message ID, as each send of Hermes has.
        sends = (('outside:named', named), ('outside:direct', direct))
        with self.assertLogs('coconut', logging.WARNING):
            for message_id, chat in sends:
                self.assertEqual(self.outside('Design notes are ready.', message_id, chat),
                                 {'recorded': False, 'reason': 'The team log did not take this message.',
                                  'retry': True})
        for message_id, chat in sends:
            self.assertEqual(self.outside('Design notes are ready.', message_id, chat), {'recorded': True})
        self.assertEqual(len(writes), 4)
        self.sync()
        self.assertEqual(self.of_chat(design), [('Reporter', 'Design notes are ready.')])
        self.assertEqual(self.bot_replies(), ['Design notes are ready.'])

    def test_a_failed_middle_part_stops_the_send_and_a_repeat_writes_the_rest(self):
        from coconut.team_turns import MAX_TEXT
        text = 'a' * MAX_TEXT + 'b' * MAX_TEXT + 'c'
        writes = self.relay_faults(None, relay_error(503))
        with self.assertLogs('coconut', logging.WARNING):
            self.assertEqual(self.outside(text), {'recorded': False, 'retry': True,
                                                  'shown': 1,
                                                  'reason': 'The chat shows only 1 of 3 parts of this message.'})
        # The third part waits: the chat shows no part after a gap.
        self.assertEqual(len(writes), 2)
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'a' * MAX_TEXT)])
        # A repeat with the same message ID writes only the two parts that the log does not have.
        self.assertEqual(self.outside(text), {'recorded': True})
        self.assertEqual(len(writes), 4)
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'a' * MAX_TEXT), ('Reporter', 'b' * MAX_TEXT),
                                          ('Reporter', 'c')])

    def test_a_failed_last_part_is_not_recorded_and_a_repeat_writes_only_that_part(self):
        from coconut.team_turns import MAX_TEXT
        text = 'a' * MAX_TEXT + 'b' * MAX_TEXT + 'c'
        writes = self.relay_faults(None, None, relay_error(503))
        with self.assertLogs('coconut', logging.WARNING):
            self.assertEqual(self.outside(text), {'recorded': False, 'retry': True,
                                                  'shown': 2,
                                                  'reason': 'The chat shows only 2 of 3 parts of this message.'})
        self.assertEqual(self.outside(text), {'recorded': True})
        self.assertEqual(len(writes), 4)
        self.sync()
        self.assertEqual(self.of_chat(), [('Reporter', 'a' * MAX_TEXT), ('Reporter', 'b' * MAX_TEXT),
                                          ('Reporter', 'c')])


if __name__ == '__main__':
    unittest.main()
