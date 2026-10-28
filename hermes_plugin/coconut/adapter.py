"""The gateway adapter for Coconut chats.

The Coconut service on this Mac owns the chat. When the person's chat runtime is `gateway`, the service gives a plain
chat turn to this adapter instead of to its own worker (backend/coconut/gateway_bridge.py). The adapter asks the service
for turns with a long poll on loopback, gives each turn to the gateway as an inbound message, and sends every reply,
edit and end of turn back. One chat is one Coconut bot: the chat ID is the bot ID.

The service writes its address and a token to `<data>/gateway/bridge.json` (mode 0600) at each start. The adapter
reads the file again after a failed request. `gateway.platforms.coconut.extra.bridge` can name another file.

Under a multiplexing gateway, each served profile has its own adapter. It connects in the scope of its profile, so the
Hermes home that it names to the service is the home of that profile.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import urllib.request

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from . import NAME

LOG = logging.getLogger(__name__)
# scripts/host.py runs the service with <install folder>/.runtime/host as its data folder.
DEFAULT_BRIDGE = Path(__file__).resolve().parents[2] / '.runtime/host/gateway/bridge.json'
POLL_SECONDS = 25
RETRY_SECONDS = (1, 2, 5, 10, 30)
REQUEST_SECONDS = 10
# Requests go to loopback only, never through a proxy from the environment.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Bridge:
    """The routes of the Coconut service. Each call blocks, so the adapter runs it in a thread."""

    def __init__(self, path):
        self.path = Path(path)
        self.address = None

    def request(self, route: str, payload: dict, timeout: float) -> dict:
        if self.address is None:
            address = json.loads(self.path.read_text())
            if not str(address.get('url', '')).startswith('http://127.0.0.1:') or not address.get('token'):
                raise ValueError(f'{self.path} does not name a Coconut service on loopback.')
            self.address = address
        request = urllib.request.Request(
            self.address['url'] + route, data=json.dumps(payload).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.address['token']})
        try:
            with OPENER.open(request, timeout=timeout) as response:
                return json.loads(response.read() or b'{}')
        except Exception:
            # The service writes a new address and token at each start.
            self.address = None
            raise


def turn_of(message_id) -> str:
    """The turn ID in a message ID that this adapter gave out (`<turn>:<number>`)."""
    return str(message_id).rpartition(':')[0]


class CoconutAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = True
    # A longer reply would arrive in parts, and Coconut would show the earlier parts as interim messages.
    MAX_MESSAGE_LENGTH = 100_000

    def __init__(self, config):
        super().__init__(config, Platform(NAME))
        extra = getattr(config, 'extra', None) or {}
        self._bridge = Bridge(extra.get('bridge') or DEFAULT_BRIDGE)
        self._home = None
        self._poller = None
        # The running turn of each chat. The service gives a bot one turn at a time.
        self._turns: dict[str, str] = {}
        # The turns that this adapter took and the gateway did not finish. Each poll lists them.
        self._active: set[str] = set()
        self._typing: set[str] = set()
        self._count = 0

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        from hermes_constants import get_hermes_home
        self._home = os.path.realpath(get_hermes_home())
        if self._poller is None or self._poller.done():
            self._poller = asyncio.create_task(self._poll())
        # The adapter is ready without the service. It asks again until the service answers.
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        poller, self._poller = self._poller, None
        if poller is not None:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await poller
        self._mark_disconnected()

    async def _request(self, route: str, payload: dict, timeout: float = REQUEST_SECONDS) -> dict:
        return await asyncio.to_thread(self._bridge.request, route, payload, timeout)

    async def _poll(self) -> None:
        failures = 0
        while True:
            try:
                answer = await self._request('/v1/gateway/poll', {
                    'home': self._home, 'active': sorted(self._active), 'wait': POLL_SECONDS},
                    POLL_SECONDS + REQUEST_SECONDS)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if failures == 0:
                    LOG.info('Coconut: the Coconut service does not answer (%s). Trying again.', type(error).__name__)
                await asyncio.sleep(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)])
                failures += 1
                continue
            for chat_id in answer.get('stop') or []:
                await self._stop(str(chat_id))
            for turn in answer.get('turns') or []:
                try:
                    event = self._event(turn)
                except (KeyError, TypeError, ValueError):
                    LOG.warning('Coconut: ignored a turn without its fields.')
                    continue
                self._active.add(event.message_id)
                try:
                    await self.handle_message(event)
                except Exception:
                    # The service ends a turn that the gateway never reports, so the poll goes on.
                    LOG.exception('Coconut: the gateway did not take a turn.')
                    self._active.discard(event.message_id)

    def _event(self, turn: dict) -> MessageEvent:
        source = self.build_source(chat_id=str(turn['chat_id']), chat_name=str(turn.get('chat_name') or 'Coconut'),
                                   chat_type='dm', user_id='coconut', user_name=str(turn.get('user_name') or 'Coconut'))
        # Text that starts with "/" stays a chat message. Coconut does not offer gateway commands yet.
        return MessageEvent(text=str(turn['text']), message_type=MessageType.TEXT, source=source,
                            message_id=str(turn['id']), allow_gateway_control=False)

    async def _stop(self, chat_id: str) -> None:
        """Stop the running turn of a chat, as `/stop` does. Coconut cancelled it."""
        turn = self._turns.get(chat_id)
        if turn is None:
            return
        source = self.build_source(chat_id=chat_id, chat_type='dm', user_id='coconut', user_name='Coconut')
        await self.handle_message(MessageEvent(text='/stop', message_type=MessageType.COMMAND, source=source,
                                               message_id=turn + ':stop'))

    async def _report(self, turn: str, kind: str, **fields) -> bool:
        try:
            await self._request('/v1/gateway/events', dict(fields, turn_id=turn, kind=kind))
            return True
        except Exception as error:
            LOG.warning('Coconut: the Coconut service did not take a %s event (%s).', kind, type(error).__name__)
            return False

    async def on_processing_start(self, event: MessageEvent) -> None:
        self._turns[event.source.chat_id] = event.message_id

    async def on_processing_complete(self, event: MessageEvent, outcome) -> None:
        if self._turns.get(event.source.chat_id) == event.message_id:
            del self._turns[event.source.chat_id]
        self._active.discard(event.message_id)
        self._typing.discard(event.message_id)
        await self._report(event.message_id, 'done', outcome=getattr(outcome, 'value', str(outcome)))

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        turn = self._turns.get(str(chat_id))
        if turn is None:
            # For example a cron delivery. Coconut shows only the replies of its own turns for now.
            return SendResult(success=False, error='Coconut takes messages only during its own turns.')
        self._count += 1
        message_id = f'{turn}:{self._count}'
        # The gateway marks status and commentary sends with _interim_send. Each is a complete message.
        interim = bool((metadata or {}).get('_interim_send'))
        if not await self._report(turn, 'send', message_id=message_id, text=str(content), interim=interim):
            return SendResult(success=False, error='The Coconut service did not answer.', retryable=True)
        return SendResult(success=True, message_id=message_id)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        if not await self._report(turn_of(message_id), 'edit', message_id=str(message_id), text=str(content)):
            return SendResult(success=False, error='The Coconut service did not answer.', retryable=True)
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return await self._report(turn_of(message_id), 'delete', message_id=str(message_id))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # The first typing signal of a turn says that Hermes works on it. The poll reports that the turn still runs.
        turn = self._turns.get(str(chat_id))
        if turn is not None and turn not in self._typing:
            self._typing.add(turn)
            await self._report(turn, 'typing')

    async def get_chat_info(self, chat_id: str) -> dict:
        return {'name': 'Coconut', 'type': 'dm', 'chat_id': chat_id}
