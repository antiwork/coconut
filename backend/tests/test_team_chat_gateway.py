"""The mode `team` of the `coconut` plugin (hermes_plugin/coconut/team.py) against a team-only service.

The stand-ins of the Hermes modules are those of test_gateway_adapter.py, with the fields of a message event that a
group chat needs. Ada's Mac is a full Mac, and the team-only Mac serves its routes on a real loopback listener. The
adapter of the team mode connects to it as in a gateway, and the message handler plays the gateway runner. No model
runs. test_native_team_chat.py runs a real Hermes gateway.
"""
import asyncio
import dataclasses
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import test_gateway_adapter
from coconut.server import APIServer
from coconut.team_turns import ASKED, DENIED
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


class Context:
    """The plugin context of Hermes: it records each registration."""

    def __init__(self, fail=None):
        self.platforms, self.tools, self.hooks, self.fail = [], [], [], fail

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


def setUpModule():
    global STUBS, plugin, team
    STUBS = tempfile.TemporaryDirectory()
    with mock.patch.dict(test_gateway_adapter.STUBS, {'gateway/platforms/event.py': EVENT}):
        plugin, _ = test_gateway_adapter.load_plugin(Path(STUBS.name))
    import hermes_plugins.coconut.team as team


def tearDownModule():
    sys.path.remove(STUBS.name)
    STUBS.cleanup()


def team_mode(config=None):
    import hermes_cli.config
    value = {'gateway': {'platforms': {'coconut': {'enabled': True, 'extra': {'mode': 'team'}}}}}
    return mock.patch.object(hermes_cli.config, 'CONFIG', value if config is None else config)


class RegistrationTests(unittest.TestCase):
    def test_the_team_mode_registers_the_platform_and_nothing_else(self):
        context = Context()
        with team_mode():
            plugin.register(context)
        self.assertEqual((context.tools, context.hooks), ([], []))
        (options,) = context.platforms
        self.assertEqual((options['name'], options['trusted_inbound'], options['platform_hint']),
                         ('coconut', True, plugin.TEAM_HINT))
        self.assertIs(options['adapter_factory'], team.TeamAdapter)
        # Without the mode, the plugin registers what it registers for a full install.
        full = Context()
        plugin.register(full)
        self.assertEqual((len(full.platforms), len(full.tools)), (1, 29))
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
        self.assertEqual((context.platforms, context.tools, context.hooks), ([], [], []))
        self.assertEqual(len(logs.records), 1)
        self.assertIn('channel_context', logs.output[0])

    def test_a_hermes_that_raises_gets_one_log_line_and_no_channel(self):
        for fault in ('register', 'import'):
            with self.subTest(fault=fault):
                context = Context(fail=RuntimeError('boom') if fault == 'register' else None)
                modules = {'gateway.platform_registry': None} if fault == 'import' else {}
                with team_mode(), mock.patch.dict(sys.modules, modules), \
                        self.assertLogs('hermes_plugins.coconut', logging.WARNING) as logs:
                    plugin.register(context)
                self.assertEqual((context.platforms, context.tools, context.hooks), ([], [], []))
                self.assertEqual(len(logs.records), 1)
                self.assertIn('the team chat stays off', logs.output[0])


class TeamAdapterTests(TeamTurnMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = APIServer(('127.0.0.1', 0), self.only)
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.only.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_address[1]}')
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

    def test_a_turn_is_a_group_message_and_its_reply_reaches_the_chat(self):
        async def runner(event):
            return 'Three tasks are open.'
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


if __name__ == '__main__':
    unittest.main()
