"""The gateway adapter for Coconut chats.

The Coconut service on this Mac owns the chat. When the person's chat runtime is `gateway`, the service gives a chat
turn to this adapter instead of to its own worker (backend/coconut/gateway_bridge.py). The adapter asks the service for
turns with a long poll on loopback, gives each turn to the gateway as an inbound message, and sends every reply, edit
and end of turn back. One chat is one Coconut bot: the chat ID is the bot ID.

The service writes its address and a token to `<data>/gateway/bridge.json` (mode 0600) at each start. The adapter
reads the file again after a failed request. `gateway.platforms.coconut.extra.bridge` can name another file.

Under a multiplexing gateway, each served profile has its own adapter. It connects in the scope of its profile, so the
Hermes home that it names to the service is the home of that profile.

Hermes also sends to a chat outside a Coconut turn. For example, it runs its own turn in the gateway session of the
chat for the completion of a background process, a Kanban wake or a delegated result, and sends the reply at the end.
Such a message goes to the service (`POST /v1/gateway/messages`), which shows it as a message of the bot in its chat.
A status send outside a turn, such as a shutdown notice, stays in the gateway log.

Clarify questions, command approvals and coconut_ask become decision cards on the phone, and the answer returns to
Hermes through a later poll. The hooks (hooks.py) relay the streamed text of a turn and join the gateway session of a
chat with the bot's native conversation. The tools (tools.py) reach the Coconut task of the chat.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import uuid
import weakref

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
# Streamed text goes to the service in pieces of at least this age or size.
STREAM_SECONDS = 0.2
STREAM_CHARACTERS = 400
# coconut_ask waits this long for the person.
ASK_SECONDS = 6 * 3600
# The ID of a message outside a Coconut turn starts with this prefix. Its edits and its delete take the same route.
OUTSIDE = 'outside:'
# The adapters of this process. The tools and the hooks find the adapter of a chat here.
ADAPTERS = weakref.WeakSet()


def session_root_for_chat(chat_id):
    """The root of the native conversation that the service named for the newest turn of a chat, or None."""
    for adapter in list(ADAPTERS):
        root = adapter._roots.get(str(chat_id))
        if root is not None:
            return root
    return None


def turn_for_chat(chat_id):
    """The adapter and the running turn of a Coconut chat, or (None, None)."""
    for adapter in list(ADAPTERS):
        turn = adapter._turns.get(str(chat_id))
        if turn is not None:
            return adapter, turn
    return None, None


class Bridge:
    """The routes of the Coconut service. Each call blocks, so the adapter runs it in a thread."""

    def __init__(self, path):
        self.path = Path(path)
        self.address = None

    def request(self, route: str, payload: dict, timeout: float) -> dict:
        address = self.address
        if address is None:
            address = json.loads(self.path.read_text())
            if not str(address.get('url', '')).startswith('http://127.0.0.1:') or not address.get('token'):
                raise ValueError(f'{self.path} does not name a Coconut service on loopback.')
            self.address = address
        request = urllib.request.Request(
            address['url'] + route, data=json.dumps(payload).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + address['token']})
        try:
            with OPENER.open(request, timeout=timeout) as response:
                return json.loads(response.read() or b'{}')
        except Exception:
            # The service writes a new address and token at each start.
            self.address = None
            raise


def turn_of(message_id) -> str:
    """The turn ID in a message ID that this adapter gave out (`<turn>:<number>`)."""
    return str(message_id).partition(':')[0]


class StreamRelay:
    """Send the streamed text of turns to the service in small pieces. The adapter and a cron run (cron.py) use it.

    hooks.py calls these methods in a Hermes dispatcher thread, never on the event loop.
    """

    def _init_relay(self):
        # Streamed text that waits for the service: turn -> [stream number, text, time of the last report].
        self._streams: dict[str, list] = {}
        self._streams_lock = threading.Lock()
        # The number of the current model call of each turn (hooks.stream_start).
        self._stream_numbers: dict[str, int] = {}

    def drop_stream(self, turn: str) -> None:
        with self._streams_lock:
            self._streams.pop(turn, None)
        self._stream_numbers.pop(turn, None)

    def stream(self, turn: str, number: int, text: str) -> None:
        pending = None
        with self._streams_lock:
            entry = self._streams.get(turn)
            if entry is None or entry[0] != number:
                if entry is not None and entry[1]:
                    pending = (entry[0], entry[1])
                entry = self._streams[turn] = [number, '', 0.0]
            entry[1] += text
            due = time.monotonic() - entry[2] >= STREAM_SECONDS or len(entry[1]) >= STREAM_CHARACTERS
        if pending is not None:
            self._send_stream(turn, *pending)
        if due:
            self.flush_stream(turn)

    def flush_stream(self, turn: str) -> None:
        with self._streams_lock:
            entry = self._streams.get(turn)
            if entry is None or not entry[1]:
                return
            number, text = entry[0], entry[1]
            entry[1], entry[2] = '', time.monotonic()
        self._send_stream(turn, number, text)

    def _send_stream(self, turn: str, number: int, text: str) -> None:
        try:
            self._bridge.request('/v1/gateway/events', {'turn_id': turn, 'kind': 'delta', 'stream': number,
                                                        'text': text}, REQUEST_SECONDS)
        except Exception as error:
            # The final send of the turn carries the whole reply, so a lost piece only delays the preview.
            LOG.debug('Coconut: a streamed piece did not reach the service (%s).', type(error).__name__)


class CoconutAdapter(StreamRelay, BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = True
    # A longer reply would arrive in parts, and Coconut would show the earlier parts as interim messages.
    MAX_MESSAGE_LENGTH = 100_000
    # The poll alone is not work: an auto-started gateway may idle out (NousResearch/hermes-agent#146209).
    keeps_gateway_up = False

    def __init__(self, config):
        super().__init__(config, Platform(NAME))
        extra = getattr(config, 'extra', None) or {}
        self._bridge = Bridge(extra.get('bridge') or DEFAULT_BRIDGE)
        if isinstance(getattr(config, 'extra', None), dict):
            # Setup and operational notices of the gateway, such as "No home channel is set", go to
            # send_private_notice. They name gateway commands that Coconut does not offer.
            config.extra.setdefault('notice_delivery', 'private')
        self._home = None
        self._poller = None
        # The running turn of each chat. The service gives a bot one turn at a time.
        self._turns: dict[str, str] = {}
        # The turns that this adapter took and the gateway did not finish. Each poll lists them.
        self._active: set[str] = set()
        self._typing: set[str] = set()
        self._count = 0
        # Open questions on the phone: question ID -> how the answer returns to Hermes.
        self._questions: dict[str, tuple] = {}
        # The root of the bot's native conversation for each chat, from the newest turn (hooks.join_conversation).
        # The gateway refuses inbound metadata of other platforms, so the root stays here.
        self._roots: dict[str, str] = {}
        self._init_relay()
        ADAPTERS.add(self)

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
        self._wake_questions()
        self._mark_disconnected()

    async def _request(self, route: str, payload: dict, timeout: float = REQUEST_SECONDS) -> dict:
        return await asyncio.to_thread(self._bridge.request, route, payload, timeout)

    def _starting(self) -> bool:
        """Whether the gateway still restores sessions after its start. Until then it queues each inbound message
        and ends the processing of that message without a reply, which Coconut would read as a finished turn.

        Hermes has no public signal for this. The Coconut contract check (hermes_contract.verify_gateway_contract)
        requires the attribute, and the adapter refuses to poll without it.
        """
        runner = self.gateway_runner
        if runner is not None and not hasattr(runner, '_startup_restore_in_progress'):
            raise RuntimeError('Hermes changed its startup restore. Update Coconut before it takes gateway turns.')
        return bool(getattr(runner, '_startup_restore_in_progress', False))

    async def _poll(self) -> None:
        failures = 0
        while True:
            try:
                starting = self._starting()
            except RuntimeError as error:
                # Coconut then shows no connected adapter, and chats wait for one.
                LOG.error('Coconut: %s', error)
                await asyncio.sleep(60)
                continue
            if starting:
                await asyncio.sleep(0.5)
                continue
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
            for item in answer.get('answers') or []:
                self._answer(item)
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
        if turn.get('session_root'):
            # hooks.join_conversation reads it before the gateway runs the turn.
            self._roots[str(turn['chat_id'])] = str(turn['session_root'])
        # A turn that may stay silent, such as a reaction, needs no reply. Hermes then keeps a silence marker silent
        # instead of a notice (gateway.response_filters.silence_allowed).
        return MessageEvent(text=str(turn['text']), message_type=MessageType.TEXT, source=source,
                            message_id=str(turn['id']), allow_gateway_control=False,
                            reply_expected=False if turn.get('silent') else None)

    async def _stop(self, chat_id: str) -> None:
        """Stop the running turn of a chat, as `/stop` does. Coconut cancelled it."""
        turn = self._turns.get(chat_id)
        if turn is None:
            return
        self._wake_questions(turn)
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
        # Only a turn that the service gave is a Coconut turn. Hermes also runs its own inputs through the adapter, for
        # example the completion of a background process. Their sends go to the chat as messages outside a turn.
        if event.message_id in self._active:
            self._turns[event.source.chat_id] = event.message_id

    async def on_processing_complete(self, event: MessageEvent, outcome) -> None:
        if event.message_id not in self._active:
            return
        if self._turns.get(event.source.chat_id) == event.message_id:
            del self._turns[event.source.chat_id]
        self._typing.discard(event.message_id)
        self._wake_questions(event.message_id)
        self.drop_stream(event.message_id)
        # The turn stays active in the polls until the service has its end. Otherwise the service would fail a
        # finished turn after a short outage.
        for delay in (*RETRY_SECONDS, None):
            if await self._report(event.message_id, 'done', outcome=getattr(outcome, 'value', str(outcome))):
                break
            if delay is not None:
                await asyncio.sleep(delay)
        self._active.discard(event.message_id)

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        turn = self._turns.get(str(chat_id))
        if turn is None:
            return await self._send_outside(str(chat_id), str(content), metadata or {})
        self._count += 1
        message_id = f'{turn}:{self._count}'
        # The gateway marks status and commentary sends with _interim_send. Each is a complete message.
        interim = bool((metadata or {}).get('_interim_send'))
        if not await self._report(turn, 'send', message_id=message_id, text=str(content), interim=interim):
            return SendResult(success=False, error='The Coconut service did not answer.', retryable=True)
        return SendResult(success=True, message_id=message_id)

    async def _send_outside(self, chat_id: str, content: str, metadata: dict) -> SendResult:
        """Send a message outside a Coconut turn to the bot's chat, for example the reply of a Hermes turn for the
        completion of a background process. The service records it as a message of the bot."""
        if metadata.get('_interim_send'):
            # The gateway marks status and advisory sends as interim, for example a shutdown notice. Outside a turn,
            # no reply follows them, and they name gateway behavior that Coconut does not show.
            LOG.info('Coconut: a gateway status message for chat %s stays in the log: %s', chat_id, content[:200])
            return SendResult(success=True)
        message_id = OUTSIDE + str(uuid.uuid4())
        # The stream consumer of Hermes marks the first piece of a streamed reply with `expect_edits`.
        fields = dict(kind='send', message_id=message_id, text=content, streaming=bool(metadata.get('expect_edits')))
        if metadata.get('job_id'):
            # A cron delivery. The service shows no second copy of a run that is already a routine task in the chat.
            fields['job_id'] = str(metadata['job_id'])
        result = await self._outside(chat_id, fields)
        return SendResult(success=True, message_id=message_id) if result is True else result

    async def _outside(self, chat_id: str, fields: dict):
        """Report a message outside a turn to the service. True, or the failed SendResult."""
        try:
            answer = await self._request('/v1/gateway/messages', dict(fields, home=self._home, chat_id=chat_id))
        except urllib.error.HTTPError as error:
            if error.code >= 500:
                return SendResult(success=False, error='The Coconut service did not answer.', retryable=True)
            # For example a chat that is not a bot of this profile. A second try or a plain text gets the same answer.
            LOG.warning('Coconut: the service refused a %s outside a Coconut turn for chat %s (HTTP %s).',
                        fields['kind'], chat_id, error.code)
            return SendResult(success=False, error='Coconut refused this message.',
                              raw_response={'coconut_refused': True})
        except Exception as error:
            LOG.warning('Coconut: the Coconut service did not take a %s outside a Coconut turn (%s).', fields['kind'],
                        type(error).__name__)
            return SendResult(success=False, error='The Coconut service did not answer.', retryable=True)
        if fields['kind'] == 'send':
            if answer.get('recorded'):
                LOG.info('Coconut: a message outside a Coconut turn went to chat %s (%d characters).', chat_id,
                         len(fields['text']))
            else:
                LOG.info('Coconut: chat %s does not show a message outside a Coconut turn: %s', chat_id,
                         answer.get('reason') or 'no reason')
        return True

    def _send_retry_is_final(self, result) -> bool:
        # Hermes then neither tries again nor sends a plain text copy.
        return isinstance(getattr(result, 'raw_response', None), dict) and bool(result.raw_response.get('coconut_refused'))

    async def send_private_notice(self, chat_id: str, user_id, content: str, reply_to=None, metadata=None) -> SendResult:
        # The phone shows the replies of Hermes, not its gateway notices. The gateway log keeps them.
        LOG.info('Coconut: a gateway notice for chat %s stays in the log: %s', chat_id, str(content)[:200])
        return SendResult(success=True)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        if str(message_id).startswith(OUTSIDE):
            # Hermes streams the reply of its own turn: a first send, then edits of the same message.
            # The edit with `finalize` ends the reply, and the service pushes it then.
            result = await self._outside(str(chat_id), dict(kind='edit', message_id=str(message_id), text=str(content),
                                                            finalize=bool(finalize)))
            return SendResult(success=True, message_id=message_id) if result is True else result
        if not await self._report(turn_of(message_id), 'edit', message_id=str(message_id), text=str(content)):
            return SendResult(success=False, error='The Coconut service did not answer.', retryable=True)
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        if str(message_id).startswith(OUTSIDE):
            return await self._outside(str(chat_id), dict(kind='delete', message_id=str(message_id))) is True
        return await self._report(turn_of(message_id), 'delete', message_id=str(message_id))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # The first typing signal of a turn says that Hermes works on it. The poll reports that the turn still runs.
        turn = self._turns.get(str(chat_id))
        if turn is not None and turn not in self._typing:
            self._typing.add(turn)
            await self._report(turn, 'typing')

    # Questions. The service shows each as a decision card and returns the answer in a later poll.

    async def _question(self, chat_id: str, route: tuple, **fields) -> SendResult:
        turn = self._turns.get(str(chat_id))
        if turn is None:
            return SendResult(success=False, error='Coconut takes questions only during its own turns.')
        question_id = str(uuid.uuid4())
        self._questions[question_id] = route
        if not await self._report(turn, 'question', question_id=question_id, **fields):
            self._questions.pop(question_id, None)
            return SendResult(success=False, error='The Coconut service did not answer.', retryable=True)
        return SendResult(success=True, message_id=f'{turn}:question')

    async def send_clarify(self, chat_id: str, question: str, choices, clarify_id: str, session_key: str,
                           metadata=None) -> SendResult:
        options = [dict(id=f'choice-{index}', label=str(choice)) for index, choice in enumerate(choices or [])]
        return await self._question(chat_id, ('clarify', clarify_id), question_kind='clarify', text=str(question),
                                    choices=options)

    async def _send_exec_approval_prompt(self, prompt) -> SendResult:
        options = [dict(id=choice, label=str(label), deny=choice == 'deny') for label, choice, _ in prompt.actions]
        return await self._question(prompt.chat_id, ('approval', prompt.session_key), question_kind='approval',
                                    text=str(prompt.description or 'Allow this command?'), detail=str(prompt.command),
                                    choices=options)

    def ask(self, turn: str, question: str, choices: list, detail: str = ''):
        """coconut_ask: show a question card and wait for the answer. Runs in the thread of the tool."""
        holder = {'event': threading.Event(), 'answer': None, 'turn': turn}
        question_id = str(uuid.uuid4())
        self._questions[question_id] = ('ask', holder)
        try:
            self._bridge.request('/v1/gateway/events', {
                'turn_id': turn, 'kind': 'question', 'question_id': question_id, 'question_kind': 'ask',
                'text': question, 'detail': detail,
                'choices': [dict(id=f'choice-{index}', label=str(choice)) for index, choice in enumerate(choices)]},
                REQUEST_SECONDS)
            holder['event'].wait(ASK_SECONDS)
            return holder['answer']
        finally:
            self._questions.pop(question_id, None)

    def _wake_questions(self, turn=None) -> None:
        """Wake each coconut_ask of a turn that stopped or ended, or of every turn. It returns no answer."""
        for route in list(self._questions.values()):
            if route[0] == 'ask' and (turn is None or route[1]['turn'] == turn):
                route[1]['event'].set()

    def _answer(self, item: dict) -> None:
        route = self._questions.pop(str(item.get('question_id')), None)
        if route is None:
            return
        text, choice = str(item.get('text') or ''), str(item.get('choice') or '')
        try:
            if route[0] == 'clarify':
                from tools.clarify_gateway import resolve_gateway_clarify
                resolve_gateway_clarify(route[1], text)
            elif route[0] == 'approval':
                from tools.approval import resolve_gateway_approval
                resolve_gateway_approval(route[1], choice or 'deny', reason=text or None)
            else:
                route[1]['answer'] = text
                route[1]['event'].set()
        except Exception:
            LOG.exception('Coconut: Hermes did not take the answer to a question.')

    async def get_chat_info(self, chat_id: str) -> dict:
        return {'name': 'Coconut', 'type': 'dm', 'chat_id': chat_id}
