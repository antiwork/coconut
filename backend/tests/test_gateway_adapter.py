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
''',
    'gateway/platforms/base.py': '''
import asyncio
from dataclasses import dataclass
from typing import Optional
from gateway.platforms.event import ProcessingOutcome, SessionSource
@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
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
        self.write('PUT', '/v1/settings/chat-runtime', {'chat_runtime': 'gateway'})
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

    def replies(self, task):
        return [(m['text'], bool(m.get('_interim'))) for m in self.service._task_messages(task['id'])
                if m['role'] == 'assistant' and m['kind'] == 'text']

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
        self.assertEqual((event.text, event.source.chat_id, event.allow_gateway_control),
                         ('/new is plain text here', self.bot['id'], False))
        self.assertEqual(self.provider.starts, [])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.replies(task), [('I am looking.', True), ('Hello from the gateway.', False)])

    def test_a_final_reply_without_streaming_and_a_send_outside_a_turn(self):
        async def runner(event):
            return 'The final answer.'
        self.connect(runner)
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Answer once.'})
        self.service._run_task(task['id'])
        self.assertEqual(self.replies(task), [('The final answer.', False)])
        self.assertEqual(self.adapter._turns, {})
        # For example a cron delivery. Coconut takes no messages outside its own turns yet.
        self.assertFalse(self.complete(self.adapter.send(self.bot['id'], 'A cron result.')).success)
        # Gateway notices name gateway commands, so they stay in the gateway log.
        self.assertEqual(self.adapter.config.extra['notice_delivery'], 'private')
        self.assertTrue(self.complete(self.adapter.send_private_notice(self.bot['id'], 'coconut', 'Type /sethome.')).success)
        self.assertEqual(self.replies(task), [('The final answer.', False)])

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

    def test_registration_declares_a_trusted_service_platform_for_this_hermes(self):
        calls = []
        class Context:
            def register_platform(self, **options):
                calls.append(options)
        self.plugin.register(Context())
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
