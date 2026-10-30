"""The `coconut` gateway adapter (hermes_plugin/coconut) against the real loopback routes of the service.

Small stand-ins replace the gateway modules of Hermes. The stand-in of BasePlatformAdapter runs a turn as the gateway
does: in the background, between on_processing_start and on_processing_complete, with the reply of the message handler
sent at the end. The message handler plays the gateway runner. test_native_gateway_chat.py runs a real gateway.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.server import APIServer
from coconut.service import Service
from test_gateway_chat import Provider

PLUGIN = Path(__file__).resolve().parents[2] / 'hermes_plugin/coconut'
STUBS = {
    'gateway/__init__.py': '',
    'gateway/platforms/__init__.py': '',
    'gateway/config.py': '''
class Platform(str):
    @property
    def value(self):
        return str(self)
class PlatformConfig:
    def __init__(self, extra=None):
        self.extra = extra or {}
''',
    'gateway/platform_registry.py': '''
from dataclasses import dataclass
from typing import Any
@dataclass
class PlatformEntry:
    name: str
    label: str
    adapter_factory: Any
    check_fn: Any
    trusted_inbound: bool = False
    display_tier: str = ""
    platform_hint: str = ""
    max_message_length: int = 0
    emoji: str = ""
''',
    'gateway/platforms/event.py': '''
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
    source: Any = None
    message_id: Optional[str] = None
    allow_gateway_control: bool = True
    metadata: dict = field(default_factory=dict)
    reply_expected: Optional[bool] = None
''',
    'gateway/platforms/base.py': '''
import asyncio
from dataclasses import dataclass
from typing import Any, Optional
from gateway.platforms.event import ProcessingOutcome, SessionSource
@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False
class BasePlatformAdapter:
    gateway_runner = None
    def __init__(self, config, platform):
        self.config, self.platform = config, platform
        self._message_handler, self.connected, self.events = None, False, []
    def _mark_connected(self):
        self.connected = True
    def _mark_disconnected(self):
        self.connected = False
    def set_message_handler(self, handler):
        self._message_handler = handler
    def build_source(self, chat_id, chat_name=None, chat_type="dm", user_id=None, user_name=None):
        return SessionSource(self.platform, chat_id, chat_name, chat_type, user_id, user_name)
    async def handle_message(self, event):
        # Hermes keeps a closed contract for inbound metadata: only WhatsApp may set it (gateway/session_envelope.py).
        if event.metadata:
            raise ValueError("invalid_params (metadata)")
        self.events.append(event)
        asyncio.get_running_loop().create_task(self._process(event))
    async def _process(self, event):
        await self.on_processing_start(event)
        try:
            response = await self._message_handler(event)
            if response:
                await self.send(event.source.chat_id, response)
            outcome = ProcessingOutcome.SUCCESS
        except Exception:
            outcome = ProcessingOutcome.FAILURE
        await self.on_processing_complete(event, outcome)
''',
    'hermes_constants.py': '''
import os
from pathlib import Path
def get_hermes_home():
    return Path(os.environ["HERMES_HOME"])
''',
    'gateway/session_context.py': '''
from contextvars import ContextVar
SESSION = ContextVar("session", default={})
def get_session_env(name, default=""):
    return SESSION.get().get(name, default)
''',
    'cron/__init__.py': '',
    'cron/execution_identity.py': '''
from contextvars import ContextVar
from dataclasses import dataclass
@dataclass
class CronExecution:
    job_id: str
    job_name: str
    execution_id: str
CURRENT = ContextVar("cron_execution", default=None)
def current_cron_execution():
    return CURRENT.get()
''',
    'hermes_cli/__init__.py': '',
    'hermes_cli/config.py': '''
CONFIG = {}
def load_config_readonly():
    return CONFIG
''',
    'tools/__init__.py': '',
    'tools/clarify_gateway.py': '''
calls = []
def resolve_gateway_clarify(clarify_id, response):
    calls.append((clarify_id, response))
    return True
''',
    'tools/approval.py': '''
calls = []
def resolve_gateway_approval(session_key, choice, resolve_all=False, reason=None, request_id=None):
    calls.append((session_key, choice, reason))
    return 1
''',
    'hermes_state.py': '''
class SessionDB:
    meta, sessions, lineage, messages = {}, set(), {}, {}
    def __init__(self, db_path=None):
        self.db_path = db_path
    def get_meta(self, key):
        return self.meta.get(key)
    def set_meta(self, key, value):
        self.meta[key] = value
    def get_session(self, session_id):
        return {"id": session_id} if session_id in self.sessions else None
    def get_resume_conversations(self, session_id):
        return self.messages.get(session_id, []), []
    def resolve_resume_session_id(self, session_id):
        while session_id in self.lineage:
            session_id = self.lineage[session_id]
        return session_id
    def close(self):
        pass
''',
}


def load_plugin(stubs: Path):
    """Import the plugin as Hermes does: the package `hermes_plugins.coconut` from its folder."""
    for name, text in STUBS.items():
        (stubs / name).parent.mkdir(parents=True, exist_ok=True)
        (stubs / name).write_text(text)
    sys.path.insert(0, str(stubs))
    namespace = types.ModuleType('hermes_plugins')
    namespace.__path__ = []
    sys.modules['hermes_plugins'] = namespace
    spec = importlib.util.spec_from_file_location('hermes_plugins.coconut', PLUGIN / '__init__.py',
                                                  submodule_search_locations=[str(PLUGIN)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    import hermes_plugins.coconut.adapter as adapter
    import hermes_plugins.coconut.hooks  # noqa: F401
    import hermes_plugins.coconut.tools  # noqa: F401
    return module, adapter


class GatewayAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stubs = tempfile.TemporaryDirectory()
        cls.plugin, cls.adapter_module = load_plugin(Path(cls.stubs.name))

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.stubs.name)
        cls.stubs.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = Provider()
        self.service = Service(Path(self.temp.name) / 'data', self.provider, start_workers=False)
        self.server = APIServer(('127.0.0.1', 0), self.service)
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
        self.service.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_address[1]}')
        self.bot = self.write('POST', '/v1/bots', {'name': 'Ada'})
        # As on the person's Hermes. The adapter of the test connects through the bridge.
        self.service.gateway_enabled = lambda: True
        self.home = self.service.profiles.home(self.bot)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def tearDown(self):
        if getattr(self, 'adapter', None) is not None:
            self.complete(self.adapter.disconnect())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.service.close()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def write(self, method, path, data=None):
        return self.service.mutate(method, path, data or {}, str(uuid4()), 'phone')

    def complete(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(10)

    def connect(self, runner, gateway=None):
        from gateway.config import PlatformConfig
        self.adapter = self.adapter_module.CoconutAdapter(
            PlatformConfig({'bridge': str(self.service.gateway_bridge.address_file)}))
        self.adapter.set_message_handler(runner)
        self.adapter.gateway_runner = gateway
        with patch.dict(os.environ, HERMES_HOME=str(self.home)):
            self.assertTrue(self.complete(self.adapter.connect()))
        if gateway is None:
            self.wait_connected()

    def wait_connected(self):
        deadline = time.monotonic() + 5
        while not self.service.gateway_bridge.connected(self.home) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.service.gateway_bridge.connected(self.home))

    def decisions(self):
        # The thread of a turn uses the same database connection, so a read in the test takes the service lock.
        with self.service.lock:
            return self.service._all('decisions')

    def read(self, table, identity):
        with self.service.lock:
            return self.service._get(table, identity)

    def replies(self, task):
        return [(m['text'], bool(m.get('_interim'))) for m in self.service._task_messages(task['id'])
                if m['role'] == 'assistant' and m['kind'] == 'text']

    def test_a_turn_that_may_stay_silent_needs_no_reply_in_hermes(self):
        from gateway.config import PlatformConfig
        adapter = self.adapter_module.CoconutAdapter(PlatformConfig({'bridge': str(self.service.gateway_bridge.address_file)}))
        turn = {'id': 'turn', 'chat_id': self.bot['id'], 'text': 'Reaction added: 👍'}
        self.assertIsNone(adapter._event(turn).reply_expected)
        self.assertIs(adapter._event(dict(turn, silent=True)).reply_expected, False)

    def test_a_streamed_reply_and_its_interim_message_reach_coconut(self):
        async def runner(event):
            chat = event.source.chat_id
            await self.adapter.send_typing(chat)
            await self.adapter.send(chat, 'I am looking.', metadata={'_interim_send': True})
            first = await self.adapter.send(chat, 'Hello')
            await self.adapter.edit_message(chat, first.message_id, 'Hello from the gateway.', finalize=True)
            return None
        self.connect(runner)
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': '/new is plain text here'})
        self.service._run_task(task['id'])
        event = self.adapter.events[0]
        self.assertEqual((event.text.split('\n')[0], event.source.chat_id, event.allow_gateway_control),
                         ('/new is plain text here', self.bot['id'], False))
        self.assertEqual(self.provider.starts, [])
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.replies(task), [('I am looking.', True), ('Hello from the gateway.', False)])

    def outside(self):
        """The messages of the bot outside its Coconut turns: (text, hidden)."""
        with self.service.lock:
            return [(m['text'], bool(m.get('_hidden'))) for m in self.service._all('messages')
                    if m['bot_id'] == self.bot['id'] and m.get('_gateway_message_id')]

    def test_a_final_reply_without_streaming_and_a_send_outside_a_turn(self):
        async def runner(event):
            return 'The final answer.'
        self.connect(runner)
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Answer once.'})
        self.service._run_task(task['id'])
        self.assertEqual(self.replies(task), [('The final answer.', False)])
        self.assertEqual(self.adapter._turns, {})
        # For example the reply of a Hermes turn for a background process. It goes to the bot's chat, not to the
        # finished turn.
        sent = self.complete(self.adapter.send(self.bot['id'], 'The background job finished.'))
        self.assertTrue(sent.success)
        self.assertTrue(sent.message_id.startswith('outside:'))
        # Hermes streams the reply of its own turn with edits of the same message.
        self.assertTrue(self.complete(self.adapter.edit_message(self.bot['id'], sent.message_id,
                                                                'The background job finished with no errors.')).success)
        self.assertEqual(self.outside(), [('The background job finished with no errors.', False)])
        self.assertTrue(self.complete(self.adapter.delete_message(self.bot['id'], sent.message_id)))
        self.assertEqual(self.outside(), [('The background job finished with no errors.', True)])
        self.assertEqual(self.replies(task), [('The final answer.', False)])
        # Gateway notices name gateway commands, so they stay in the gateway log. So do the status sends that the
        # gateway marks as interim, for example a shutdown notice.
        self.assertEqual(self.adapter.config.extra['notice_delivery'], 'private')
        self.assertTrue(self.complete(self.adapter.send_private_notice(self.bot['id'], 'coconut', 'Type /sethome.')).success)
        self.assertTrue(self.complete(self.adapter.send(self.bot['id'], 'The gateway restarts.',
                                                        metadata={'_interim_send': True})).success)
        self.assertEqual(len(self.outside()), 1)

    def test_a_send_outside_a_turn_to_another_chat_fails_without_a_retry(self):
        self.connect(None)
        result = self.complete(self.adapter.send('not-a-bot', 'Hello.'))
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        # Hermes then sends no plain text copy.
        self.assertTrue(self.adapter._send_retry_is_final(result))
        self.assertFalse(self.adapter._send_retry_is_final(self.adapter_module.SendResult(success=False)))
        # Without the service, Hermes tries again.
        self.adapter._bridge.address = {'url': 'http://127.0.0.1:1', 'token': 'x'}
        result = self.complete(self.adapter.send(self.bot['id'], 'Hello.'))
        self.assertEqual((result.success, result.retryable), (False, True))
        self.assertEqual(self.outside(), [])

    def test_a_hermes_input_through_the_adapter_does_not_take_the_turn_of_the_chat(self):
        # Hermes runs some of its own inputs through the adapter, for example the completion of a background process
        # without a session authority. The reply goes to the bot's chat as a message outside a turn.
        from gateway.platforms.event import MessageEvent
        async def runner(event):
            return 'The process exited with code 0.'
        self.connect(runner)
        source = self.adapter.build_source(chat_id=self.bot['id'], chat_type='dm', user_id='coconut')
        self.complete(self.adapter.handle_message(MessageEvent(text='[process finished]', source=source)))
        deadline = time.monotonic() + 5
        while not self.outside() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.outside(), [('The process exited with code 0.', False)])
        self.assertEqual((self.adapter._turns, self.adapter._active), ({}, set()))

    def test_the_adapter_takes_turns_only_after_the_gateway_restored_its_sessions(self):
        # While the gateway restores sessions after its start, it queues each inbound message and ends its
        # processing without a reply.
        gateway = types.SimpleNamespace(_startup_restore_in_progress=True)
        async def runner(event):
            return 'Ready.'
        self.connect(runner, gateway)
        time.sleep(1)
        self.assertFalse(self.service.gateway_bridge.connected(self.home))
        gateway._startup_restore_in_progress = False
        self.wait_connected()

    def test_an_idle_poll_is_no_activity_of_the_gateway(self):
        # Hermes stops a gateway that a client started when it is idle (gateway/run_idle_exit.py). Only the clients
        # of its own API stamp activity or count as attached. The adapter adds neither, also after many empty polls.
        gateway = types.SimpleNamespace(_startup_restore_in_progress=False, _idle_exit_last_activity=1.0,
                                        _idle_exit_clients=0)
        before = dict(vars(gateway))
        async def runner(event):
            return 'Unused.'
        with patch.object(self.adapter_module, 'POLL_SECONDS', .05):
            self.connect(runner, gateway)
            self.wait_connected()
            time.sleep(.5)
        self.assertEqual(vars(gateway), before)
        self.assertEqual((self.adapter.events, self.adapter._active), ([], set()))
        # With NousResearch/hermes-agent#146209, the enabled and connected platform also does not hold that gateway.
        self.assertIs(self.adapter_module.CoconutAdapter.keeps_gateway_up, False)

    def test_the_end_of_a_turn_is_reported_again_after_a_failure(self):
        async def runner(event):
            return 'Done.'
        self.connect(runner)
        report = self.adapter._report
        failures = []
        async def flaky(turn, kind, **fields):
            if kind == 'done' and not failures:
                failures.append(kind)
                # Until the service has the end, the polls still list the turn.
                self.assertIn(turn, self.adapter._active)
                return False
            return await report(turn, kind, **fields)
        self.adapter._report = flaky
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Finish.'})
        self.service._run_task(task['id'])
        self.assertEqual(failures, ['done'])
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.adapter._active, set())

    def test_the_adapter_asks_again_after_the_service_starts_with_a_new_token(self):
        bridge = self.adapter_module.Bridge(self.service.gateway_bridge.address_file)
        self.assertEqual(bridge.request('/v1/gateway/poll', {'home': str(self.home), 'wait': 0}, 5),
                         {'turns': [], 'stop': []})
        self.service.gateway_bridge.token = 'a-new-token'
        self.service.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_address[1]}')
        with self.assertRaises(Exception):
            # The adapter still has the old token. The failure makes it read the file again.
            bridge.request('/v1/gateway/poll', {'home': str(self.home), 'wait': 0}, 5)
        self.assertEqual(bridge.request('/v1/gateway/poll', {'home': str(self.home), 'wait': 0}, 5)['turns'], [])
        # The tunnel listener of the phone does not serve these routes.
        self.server.tunnel = True
        with self.assertRaises(Exception):
            bridge.request('/v1/gateway/poll', {'home': str(self.home), 'wait': 0}, 5)

    def test_the_bridge_file_must_name_a_loopback_service(self):
        path = Path(self.temp.name) / 'bridge.json'
        path.write_text(json.dumps({'url': 'http://example.com', 'token': 'x'}))
        with self.assertRaisesRegex(ValueError, 'loopback'):
            self.adapter_module.Bridge(path).request('/v1/gateway/poll', {}, 1)

    def session(self, chat_id):
        from gateway.session_context import SESSION
        return SESSION.set({'HERMES_SESSION_PLATFORM': 'coconut', 'HERMES_SESSION_CHAT_ID': chat_id})

    def test_streamed_text_of_the_hooks_reaches_the_reply_before_the_final_send(self):
        hooks = sys.modules['hermes_plugins.coconut.hooks']
        seen = []
        async def runner(event):
            def model_call():
                token = self.session(event.source.chat_id)
                try:
                    hooks.stream_start()
                    for piece in ('Hello', ' from', ' the stream.'):
                        hooks.stream_delta(delta=piece, kind='text', surface='coconut')
                    hooks.stream_delta(delta='private', kind='reasoning')
                    hooks.stream_end(final_text='Hello from the stream.')
                finally:
                    from gateway.session_context import SESSION
                    SESSION.reset(token)
            await asyncio.to_thread(model_call)
            # The service translates the streamed pieces in the thread of the turn.
            for _ in range(100):
                with self.service.lock:
                    texts = [m['text'] for m in self.service._task_messages(task['id']) if m['role'] == 'assistant']
                if texts == ['Hello from the stream.']:
                    break
                await asyncio.sleep(.02)
            seen.append(texts)
            return 'Hello from the stream.'
        self.connect(runner)
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Stream, please.'})
        self.service._run_task(task['id'])
        self.assertEqual(seen, [['Hello from the stream.']])
        self.assertEqual(self.replies(task), [('Hello from the stream.', False)])
        # Outside a Coconut chat the hooks do nothing.
        hooks.stream_delta(delta='ignored', kind='text')

    def test_a_clarify_question_and_an_approval_become_cards_and_their_answers_return_to_hermes(self):
        import tools.approval
        import tools.clarify_gateway
        from gateway.platforms.base import SendResult
        class Prompt:
            chat_id, session_key, command, description = None, 'agent:main:coconut:dm', 'rm -rf build', 'Delete files?'
            actions = [('Allow once', 'once', 'primary'), ('Deny', 'deny', 'danger')]
        async def runner(event):
            Prompt.chat_id = event.source.chat_id
            sent = await self.adapter.send_clarify(event.source.chat_id, 'Which region?', ['Europe', 'America'],
                                                   'clarify-1', 'agent:main:coconut:dm')
            assert isinstance(sent, SendResult) and sent.success
            await self.adapter._send_exec_approval_prompt(Prompt)
            for _ in range(200):
                if tools.clarify_gateway.calls and tools.approval.calls:
                    break
                await asyncio.sleep(.05)
            return 'Done.'
        self.connect(runner)
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Deploy.'})
        thread = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len([d for d in self.decisions() if d['task_id'] == task['id']]) < 2:
            time.sleep(.02)
        cards = {d['title']: d for d in self.decisions() if d['task_id'] == task['id']}
        self.write('POST', f'/v1/decisions/{cards["Which region?"]["id"]}/answer', {'option_id': 'choice-1', 'revision': 1})
        self.write('POST', f'/v1/decisions/{cards["Delete files?"]["id"]}/answer', {'option_id': 'once', 'revision': 1})
        thread.join(15)
        self.assertEqual(tools.clarify_gateway.calls, [('clarify-1', 'America')])
        self.assertEqual(tools.approval.calls, [('agent:main:coconut:dm', 'once', None)])
        self.assertEqual(cards['Delete files?']['explanation'], 'rm -rf build')
        self.assertEqual(self.replies(task), [('Done.', False)])

    def test_the_coconut_tools_act_for_the_turn_of_the_chat_and_a_card_ends_the_turn(self):
        tools = sys.modules['hermes_plugins.coconut.tools']
        hooks = sys.modules['hermes_plugins.coconut.hooks']
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        results = []
        card = {'title': 'Ship it?', 'context': 'The build passed.',
                'options': [{'id': 'ship', 'label': 'Ship it', 'explanation': ''},
                            {'id': 'wait', 'label': 'Wait', 'explanation': ''}]}
        async def runner(event):
            def tool_calls():
                token = self.session(event.source.chat_id)
                try:
                    results.append(json.loads(tools._service_tool('message_bot')({'bot': 'Lin', 'text': 'Check it.'})))
                    results.append(json.loads(tools._service_tool('message_bot')({'bot': 'Nobody', 'text': 'x'})))
                    results.append(json.loads(tools._service_tool('suggest')(dict(card, key='tests', title='Add a test?'))))
                    results.append(json.loads(tools._service_tool('ask')(card)))
                    # The card ended the turn: Hermes asks the hook before each further tool call.
                    results.append(hooks.guard_tool(tool_name='terminal', args={}))
                finally:
                    from gateway.session_context import SESSION
                    SESSION.reset(token)
            await asyncio.to_thread(tool_calls)
            return 'I asked you on a card.'
        self.connect(runner)
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Work with Lin.'})
        self.service._run_task(task['id'])
        self.assertEqual((results[0]['status'], results[0]['target_bot_id']), ('queued', other['id']))
        self.assertIn('Lin', results[1]['error'])
        self.assertEqual(results[2]['status'], 'recorded')
        self.assertEqual((results[3]['status'], results[3]['paused']), ('waiting', True))
        self.assertEqual(results[4], {'action': 'block', 'message': hooks.PAUSED_MESSAGE})
        cards = {d['title']: d for d in self.decisions() if d['task_id'] == task['id']}
        self.assertEqual((cards['Ship it?']['status'], cards['Add a test?']['inbox_kind']), ('pending', 'proposal'))
        # The task waits for the answer. The text after the card is not a reply.
        self.assertEqual(self.read('tasks', task['id'])['status'], 'needs_you')
        self.assertEqual(self.replies(task), [])
        # Outside a Coconut chat a tool refuses.
        self.assertIn('error', json.loads(tools._service_tool('ask')(card)))
        self.assertIn('error', json.loads(tools._service_tool('track_pr')({'url': 'x'})))

    def test_a_dismissed_card_of_coconut_ask_continues_nothing(self):
        tools = sys.modules['hermes_plugins.coconut.tools']
        results = []
        async def runner(event):
            def tool_calls():
                token = self.session(event.source.chat_id)
                try:
                    results.append(json.loads(tools._service_tool('ask')({
                        'title': 'Ship it?', 'context': 'The build passed.',
                        'options': [{'id': 'ship', 'label': 'Ship it', 'explanation': ''}]})))
                finally:
                    from gateway.session_context import SESSION
                    SESSION.reset(token)
            await asyncio.to_thread(tool_calls)
            return 'Asked.'
        self.connect(runner)
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Ship.'})
        self.service._run_task(task['id'])
        self.assertEqual(results[0]['status'], 'waiting')
        card = next(d for d in self.decisions() if d['task_id'] == task['id'])
        self.write('POST', f'/v1/decisions/{card["id"]}/inbox', {'action': 'dismiss', 'revision': 1})
        with self.service.transaction():
            saved = self.service._get('decisions', card['id'])
            saved['inbox_undo_until'] = '2000-01-01T00:00:00Z'
            self.service._put('decisions', saved)
        self.service.finish_inbox_swipes()
        # The card ended its turn. A dismissal records no answer and starts no turn.
        saved = self.read('decisions', card['id'])
        self.assertEqual((saved['status'], saved['_answers']), ('dismissed', []))
        stored = self.read('tasks', task['id'])
        self.assertNotEqual(stored['status'], 'queued')
        self.assertNotIn('_resume_decision', stored)

    def test_a_cron_run_in_a_bot_profile_reports_to_a_coconut_turn(self):
        hooks = sys.modules['hermes_plugins.coconut.hooks']
        tools = sys.modules['hermes_plugins.coconut.tools']
        from cron.execution_identity import CURRENT, CronExecution
        from hermes_cli.config import CONFIG
        CONFIG['gateway'] = {'platforms': {'coconut': {'extra': {'bridge': str(self.service.gateway_bridge.address_file)}}}}
        self.addCleanup(CONFIG.clear)
        proposals = []
        def cron_run():
            # As Hermes runs a cron job: in the job's profile, with the identity of the fire bound.
            token = CURRENT.set(CronExecution('intake', 'Desk fast work intake', 'execution-1'))
            try:
                with patch.dict(os.environ, HERMES_HOME=str(self.home)):
                    hooks.cron_start(session_id='cron-session', platform='cron')
                    hooks.stream_start()
                    hooks.stream_delta(delta='Checking the queue.', kind='text')
                    hooks.cron_interim(text='Checking the queue.')
                    proposals.append(json.loads(tools._service_tool('suggest')({
                        'key': 'intake', 'title': 'File two items?', 'context': 'Two items wait.',
                        'options': [{'id': 'file', 'label': 'File them', 'explanation': ''}]})))
                    hooks.cron_answer(assistant_response='Filed two items.')
                    hooks.cron_end(completed=True, failed=False, interrupted=False)
            finally:
                CURRENT.reset(token)
        thread = threading.Thread(target=cron_run, daemon=True)
        thread.start()
        thread.join(15)
        self.assertEqual(proposals[0]['status'], 'recorded')
        card = next(d for d in self.decisions() if d['title'] == 'File two items?')
        task = self.read('tasks', card['task_id'])
        deadline = time.monotonic() + 5
        while task['status'] == 'working' and time.monotonic() < deadline:
            time.sleep(.02)
            task = self.read('tasks', card['task_id'])
        self.assertEqual((task['status'], task['title'], task['_gateway_cron']['job_id']),
                         ('completed', 'Desk fast work intake', 'intake'))
        # The run names its Hermes session. An answer to a card of the run continues that session.
        self.assertEqual(task['_gateway_cron']['session_id'], 'cron-session')
        self.assertEqual(self.replies(task), [('Checking the queue.', True), ('Filed two items.', False)])
        # Outside a cron run and a Coconut chat, the hooks do nothing.
        hooks.cron_answer(assistant_response='Ignored.')
        hooks.cron_end(completed=True)

    def test_the_gateway_session_of_a_chat_joins_the_native_conversation_of_the_bot(self):
        hooks = sys.modules['hermes_plugins.coconut.hooks']
        from hermes_state import SessionDB
        from gateway.config import Platform
        from gateway.platforms.event import MessageEvent, SessionSource
        class Entry:
            def __init__(self, key, session_id):
                self.session_key, self.session_id = key, session_id
        class Store:
            def __init__(self, session_id):
                self.entry, self.switched = Entry('agent:main:coconut:dm:bot', session_id), []
            def get_or_create_session(self, source, force_new=False):
                if force_new:
                    self.entry = Entry(self.entry.session_key, 'gateway-new')
                    SessionDB.sessions.add('gateway-new')
                return self.entry
            def switch_session(self, session_key, target_session_id):
                self.switched.append((session_key, target_session_id))
                self.entry.session_id = target_session_id
        from gateway.config import PlatformConfig
        adapter = self.adapter_module.CoconutAdapter(PlatformConfig())
        def event(root):
            adapter._roots['bot'] = root
            return MessageEvent(text='Hi', source=SessionSource(Platform('coconut'), 'bot'))
        for table in (SessionDB.meta, SessionDB.sessions, SessionDB.lineage, SessionDB.messages):
            table.clear()
        with patch.dict(os.environ, HERMES_HOME=str(self.home)):
            # A bot without a conversation: the gateway session becomes the tip, for the worker.
            store = Store('gateway-1')
            self.assertIsNone(hooks.join_conversation(event=event('root-a'), session_store=store))
            self.assertEqual((store.switched, SessionDB.meta), ([], {hooks.TIP_KEY + 'root-a': 'gateway-1'}))
            # A bot with a worker conversation: the gateway session moves to its newest session.
            SessionDB.sessions.update({'root-b', 'worker-tip', 'worker-child'})
            SessionDB.meta[hooks.TIP_KEY + 'root-b'] = 'worker-tip'
            SessionDB.lineage['worker-tip'] = 'worker-child'
            store = Store('gateway-2')
            hooks.join_conversation(event=event('root-b'), session_store=store)
            self.assertEqual(store.switched, [('agent:main:coconut:dm:bot', 'worker-child')])
            # The next turn finds the same session and changes nothing.
            hooks.join_conversation(event=event('root-b'), session_store=store)
            self.assertEqual(len(store.switched), 1)
            # Another platform and a turn without a root stay as they are.
            adapter._roots['chat'] = 'root-b'
            other = MessageEvent(text='Hi', source=SessionSource(Platform('telegram'), 'chat'))
            hooks.join_conversation(event=other, session_store=store)
            hooks.join_conversation(event=event(''), session_store=store)
            self.assertEqual(len(store.switched), 1)
            # A new root, for example after a restart of the bots, starts a new session when the chat's session
            # holds the earlier conversation.
            SessionDB.messages['worker-child'] = [{'role': 'user', 'content': 'Old.'}]
            hooks.join_conversation(event=event('root-c'), session_store=store)
            self.assertEqual((store.entry.session_id, SessionDB.meta[hooks.TIP_KEY + 'root-c']), ('gateway-new', 'gateway-new'))

    def test_the_todo_list_of_a_turn_reaches_coconut_as_the_work_of_its_task(self):
        hooks = sys.modules['hermes_plugins.coconut.hooks']
        todos = [{'id': 'coconut-assignment', 'content': 'Update the docs.', 'status': 'in_progress'},
                 {'id': 'readme', 'content': 'Edit the README.', 'status': 'pending'}]
        async def runner(event):
            def tool_calls():
                token = self.session(event.source.chat_id)
                try:
                    # As Hermes reports the result of its todo tool, and of another tool.
                    hooks.save_todos(tool_name='todo_list', args={}, result=json.dumps({'todos': todos, 'revision': 2}))
                    hooks.save_todos(tool_name='terminal', args={}, result='{"todos": []}')
                finally:
                    from gateway.session_context import SESSION
                    SESSION.reset(token)
            await asyncio.to_thread(tool_calls)
            return 'Started.'
        self.connect(runner)
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Update the docs.'})
        self.service._run_task(task['id'])
        stored = self.read('tasks', task['id'])
        self.assertEqual([item['id'] for item in stored['_work_todos']['todos']], ['coconut-assignment', 'readme'])
        # The list is unfinished, so Coconut continues the assignment in a later turn, as after a worker turn.
        self.assertEqual((stored['status'], stored.get('_work_resume')), ('queued', True))
        # Outside a Coconut turn the hook does nothing.
        self.assertIsNone(hooks.save_todos(tool_name='todo_list', result=json.dumps({'todos': todos})))

    def test_the_hook_blocks_tools_after_a_card_and_cards_in_a_follow_up(self):
        hooks = sys.modules['hermes_plugins.coconut.hooks']
        adapter = self.adapter_module
        self.addCleanup(adapter.PAUSED.clear)
        self.addCleanup(adapter.MODES.clear)
        with patch.object(hooks, '_turn', return_value=(None, 'turn-a')):
            self.assertIsNone(hooks.guard_tool(tool_name='terminal'))
            adapter.MODES['turn-a'] = 'context'
            # A follow-up on a pending card answers the question. It makes no card and acts for nobody.
            self.assertEqual(hooks.guard_tool(tool_name='coconut_ask'), {'action': 'block', 'message': hooks.CONTEXT_MESSAGE})
            self.assertEqual(hooks.guard_tool(tool_name='clarify')['action'], 'block')
            self.assertIsNone(hooks.guard_tool(tool_name='coconut_resolve_reply'))
            self.assertIsNone(hooks.guard_tool(tool_name='read_file'))
            adapter.PAUSED.add('turn-a')
            self.assertEqual(hooks.guard_tool(tool_name='read_file'), {'action': 'block', 'message': hooks.PAUSED_MESSAGE})
            adapter.PAUSED.discard('turn-a')
            # A status question uses no tools at all.
            # Only an automatic check of a card records a check result.
            self.assertEqual(hooks.guard_tool(tool_name='coconut_refresh_decision'),
                             {'action': 'block', 'message': hooks.NOT_A_CHECK_MESSAGE})
            adapter.MODES['turn-a'] = 'status'
            self.assertEqual(hooks.guard_tool(tool_name='read_file'), {'action': 'block', 'message': hooks.STATUS_MESSAGE})
            # An automatic check of a card reads the evidence and records its result. It makes no card and answers
            # for nobody.
            adapter.MODES['turn-a'] = 'refresh'
            self.assertIsNone(hooks.guard_tool(tool_name='coconut_refresh_decision'))
            self.assertIsNone(hooks.guard_tool(tool_name='read_file'))
            for tool in ('coconut_ask', 'coconut_suggest', 'coconut_resolve_reply', 'clarify', 'coconut_message_bot'):
                self.assertEqual(hooks.guard_tool(tool_name=tool), {'action': 'block', 'message': hooks.REFRESH_MESSAGE})
            # An audit of a work source writes to the board of Coconut, never to the gateway's own board.
            adapter.MODES['turn-a'] = 'audit'
            for tool in ('kanban_create', 'kanban_comment', 'kanban_complete'):
                self.assertEqual(hooks.guard_tool(tool_name=tool), {'action': 'block', 'message': hooks.AUDIT_BOARD_MESSAGE})
            for tool in ('coconut_kanban_create', 'coconut_kanban_comment', 'coconut_ask', 'read_file'):
                self.assertIsNone(hooks.guard_tool(tool_name=tool))
        # Outside a Coconut turn the hook does nothing.
        with patch.object(hooks, '_turn', return_value=(None, None)):
            self.assertIsNone(hooks.guard_tool(tool_name='terminal'))
        # The turn of a follow-up names its mode, and the end of the turn forgets it.
        from gateway.config import PlatformConfig
        coconut = adapter.CoconutAdapter(PlatformConfig())
        coconut._event({'id': 'turn-b', 'chat_id': 'bot', 'text': 'Why?', 'mode': 'context'})
        self.assertEqual(adapter.MODES['turn-b'], 'context')

    def test_registration_declares_a_trusted_service_platform_for_this_hermes(self):
        calls, tool_names, hook_names, sections, schemas = [], [], [], {}, {}
        class Context:
            def register_platform(self, **options):
                calls.append(options)
            def register_tool(self, **options):
                tool_names.append((options['name'], options['toolset']))
                schemas[options['name']] = options['schema']
            def register_hook(self, name, callback):
                hook_names.append(name)
            def register_system_prompt_section(self, id, content, *, position='after_memory', max_chars=4000):
                text = content({'platform': 'coconut'})
                sections[id] = (text, content({'platform': 'telegram'}), len(text) <= max_chars)
        self.plugin.register(Context())
        self.assertEqual([name for name, _ in tool_names], ['coconut_ask', 'coconut_suggest', 'coconut_resolve_reply',
                                                             'coconut_refresh_decision',
                                                             'coconut_wait', 'coconut_correct_withdrawal',
                                                             'coconut_create_bot', 'coconut_delete_bot',
                                                             'coconut_workspace', 'coconut_kanban_create',
                                                             'coconut_kanban_comment',
                                                             'coconut_track_pr', 'coconut_message_bot'])
        self.assertEqual(hook_names, ['pre_gateway_dispatch', 'on_stream_start', 'on_stream_delta', 'on_stream_end',
                                      'on_session_start', 'on_interim_message', 'post_llm_call', 'on_session_end',
                                      'pre_tool_call', 'post_tool_call'])
        # The card rules of the worker go into each Coconut session, in parts that Hermes allows.
        from coconut.card_tools import DECISION_INSTRUCTIONS
        self.assertEqual(sorted(sections), ['coconut.cards.0', 'coconut.cards.1', 'coconut.cards.2', 'coconut.rules.0'])
        self.assertTrue(all(fits and text and other == '' for text, other, fits in sections.values()))
        self.assertEqual(' '.join(' '.join(sections[f'coconut.cards.{n}'][0] for n in range(3)).split()),
                         ' '.join(DECISION_INSTRUCTIONS.split()))
        # The rules of a merge card and of bot changes, as the worker gives them.
        from coconut.card_tools import BOT_CREATION_INSTRUCTIONS, PR_MERGE_INSTRUCTIONS
        self.assertIn(PR_MERGE_INSTRUCTIONS[:80], sections['coconut.rules.0'][0])
        self.assertIn(BOT_CREATION_INSTRUCTIONS.strip()[:80], sections['coconut.rules.0'][0])
        # A Coconut worker registers its own coconut tools.
        with patch.dict(os.environ, COCONUT_BOT_ID='bot'):
            self.plugin.register(Context())
        self.assertEqual(len(tool_names), 13)
        from coconut.card_tools import WORKSPACE_SCHEMA
        self.assertEqual(schemas['coconut_workspace'], WORKSPACE_SCHEMA)
        # The check tool has the revision fields of the worker's check tool: of a question and of a draft.
        from coconut.card_tools import refresh_schema
        revision = schemas['coconut_refresh_decision']['parameters']['properties']['revision']
        self.assertEqual(revision['anyOf'], [
            refresh_schema('question')['parameters']['properties']['revision'],
            refresh_schema('draft_message')['parameters']['properties']['revision']])
        self.assertIn('required', revision['anyOf'][0])
        # A gateway turn waits for its processes with the process tool of Hermes, not with coconut_wait.
        self.assertNotIn('process_id', schemas['coconut_wait']['parameters']['properties'])
        options = calls[0]
        self.assertEqual((options['name'], options['label'], options['trusted_inbound'], options['display_tier']),
                         ('coconut', 'Coconut', True, 'high'))
        # The stand-in PlatformEntry has no display_overrides. A field that Hermes does not know stays out.
        self.assertNotIn('display_overrides', options)
        self.assertTrue(options['check_fn']())
        from gateway.config import PlatformConfig
        self.assertIsInstance(options['adapter_factory'](PlatformConfig()), self.adapter_module.CoconutAdapter)


if __name__ == '__main__':
    unittest.main()
