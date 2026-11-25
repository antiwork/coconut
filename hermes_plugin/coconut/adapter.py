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
Hermes through a later poll. The hooks (hooks.py) join the gateway session of a chat with the bot's native
conversation. The tools (tools.py) reach the Coconut task of the chat.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import contextvars
import functools
import json
import logging
import os
from pathlib import Path
import re
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
# The requests to the service run on these threads (in_thread), never on the default executor of the gateway's loop.
# One gateway serves every profile of the Mac, and each adapter keeps a poll open for up to POLL_SECONDS. On the default
# executor (12 threads on an 8-core Mac), the polls of 17 profiles held every thread, so each `asyncio.to_thread` of
# Hermes waited about ten seconds. Hermes makes one such call for each message of a turn before it sends the reply, so
# the reply of a long turn arrived more than an hour after the agent wrote it. Each profile loads this module, so each
# has its own threads. A thread starts when no idle one is free, and it stays for the next request.
BRIDGE_THREADS = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix='coconut-bridge')
# The turns whose card ended them (coconut_ask), and the mode of a turn that is not a plain chat turn, for example
# `context`: a follow-up on a pending card. The pre_tool_call hook reads both (hooks.guard_tool).
PAUSED: set[str] = set()
# The turns that recorded a wait (coconut_wait) or the result of a card check (coconut_refresh_decision). Such a result
# ends its turn, so the hook blocks each further tool call.
WAITED: set[str] = set()
# The reply that ends a turn when the model chose to write no text (hooks.end_turn_text): after a card, a wait or a
# check, after the reminder of Hermes, or in a run that Hermes started. The chat does not show it (ends_quietly).
END_TEXT = 'This turn is complete.'
# The turns whose empty reply got END_TEXT from the plugin. A model that writes the same words itself is not in this set,
# and the chat shows its reply. The adapter reports the end of such a turn as quiet, and the chat shows no message for it.
CLOSED: set[str] = set()
# The turns whose model wrote no text after a tool result once. Hermes then reminds the model, and the next empty reply
# of the turn gets END_TEXT (hooks.end_turn_text).
NUDGED: set[str] = set()
# The turns of the runs that Hermes started by itself, for example for a finished background process. No person waits
# for the reply of such a run.
HERMES_RUNS: set[str] = set()
MODES: dict[str, str] = {}
# The context of each running Coconut turn: the whole turn text, which the model calls of the turn get, while Hermes
# saves only the turn's own text (hooks.add_turn_context). Set when the hooks registered the middleware.
CONTEXTS: dict[str, str] = {}
CONTEXT_MIDDLEWARE = {'registered': False}
# The records of each Coconut turn that its Hermes session keeps once (native_conversation.turn_records). The
# pre_gateway_dispatch hook adds to the turn's own text the records that the session does not hold yet
# (hooks.add_records), and the end of the turn notes them as held (hooks.commit_records).
RECORDS: dict[str, list] = {}
# The chats whose newest Coconut turn is a turn of a technical lane. Hermes runs its own turns in such a chat too,
# for example for the completion of a background process of the lane, and the hooks keep the lane holds on them
# (hooks.guard_tool). The next Coconut turn of the chat in another mode lifts them.
LANE_CHATS: set[str] = set()
# The working folder of a turn of a native outcome: the workspace of its Kanban claim (hooks.guard_tool). The Hermes
# task IDs of the turn whose terminal already has that folder.
FOLDERS: dict[str, str] = {}
FOLDER_TASKS: dict[str, set] = {}
# The Coconut turns that the gateway did not run, because Hermes already ran another turn in the session of the chat,
# for example its own turn for a finished background delegation (hooks.join_conversation). The adapter reports such a
# turn as `busy`, and the service gives it again when the session is free.
BUSY: set[str] = set()
# The ID of a message outside a Coconut turn starts with this prefix. Its edits and its delete take the same route.
OUTSIDE = 'outside:'
# The service keeps photos of at most 4 MB (gateway_chat.gateway_file).
PHOTO_LIMIT = 4 * 1024 * 1024
# The chats whose last model call reached the compression threshold of Hermes (hooks.note_prompt_size). After the turn,
# the adapter compresses the chat's session (CoconutAdapter._compress).
COMPRESS: set[str] = set()
# The message ID of a compression that Coconut starts begins with this prefix. Hermes sends its notice as a reply to
# that message, and the notice stays in the log.
COMPRESSION = 'compress:'


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


def ends_quietly(turn, text) -> bool:
    """Whether a send is the END_TEXT that the plugin gave an empty reply of the turn (CLOSED). The chat does not show
    it: after a card, the card is the message, and otherwise the model chose to say nothing new."""
    return turn in CLOSED and str(text or '').strip() == END_TEXT


def forget_turn(turn) -> bool:
    """Forget the state of a turn that ended. Return whether the plugin ended it with END_TEXT (CLOSED): the end of such
    a turn is quiet, and the chat shows no message for it (gateway_bridge.TurnEvents)."""
    quiet = turn in CLOSED
    for turns in (PAUSED, WAITED, CLOSED, NUDGED, HERMES_RUNS):
        turns.discard(turn)
    return quiet



# Coconut names the approval choices of Hermes in its own words, in English. A Hermes without its text catalog gives
# the keys of its texts, such as gateway.exec_approval.action_once, and a card must never show those.
APPROVAL_LABELS = {'once': 'Allow once', 'session': 'Allow for this session', 'always': 'Always allow',
                   'deny': 'Deny'}
TEXT_KEY = re.compile(r'^[a-z0-9_]+(\.[a-z0-9_]+)+$')


def approval_label(choice, label) -> str:
    """The button text of one approval choice of Hermes."""
    if choice in APPROVAL_LABELS:
        return APPROVAL_LABELS[choice]
    text = str(label or '').strip()
    if not text or TEXT_KEY.match(text):
        return str(choice).replace('_', ' ').capitalize()
    return text

def cached_images(images) -> tuple[list[str], list[str]]:
    """Save the attached images of a turn (native_conversation.turn_images) in the image cache of Hermes, and return
    their paths and types for the message. Hermes then gives each one to the model as an image, as for a photo of
    another platform. An image that Hermes refuses, for example bytes that are no image, is left out."""
    from gateway.platforms.base import cache_image_from_bytes
    urls, types = [], []
    for image in images or []:
        try:
            kind = str(image['type'])
            ext = {'image/jpeg': '.jpg'}.get(kind, '.' + kind.split('/')[-1])
            urls.append(cache_image_from_bytes(base64.b64decode(image['content_base64'], validate=True), ext))
            types.append(kind)
        except (KeyError, TypeError, ValueError, OSError):
            LOG.warning('Coconut: an attached image of a turn could not be saved for Hermes.')
    return urls, types


async def in_thread(function, *args):
    """Run a blocking call on BRIDGE_THREADS with the context of the caller, as `asyncio.to_thread` runs it on the
    default executor."""
    call = functools.partial(contextvars.copy_context().run, function, *args)
    return await asyncio.get_running_loop().run_in_executor(BRIDGE_THREADS, call)


class Bridge:
    """The routes of the Coconut service. Each call blocks, so the adapter runs it in a thread (in_thread)."""

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


async def report_outside(request, home, chat_id: str, fields: dict):
    """Report a message outside a turn to the service (`POST /v1/gateway/messages`) with `request`, the request
    function of an adapter. The answer of the service, or the failed SendResult. The adapter (_outside) and the sender
    of a cron run without a gateway (team.send_standalone) use it."""
    try:
        return await request('/v1/gateway/messages', dict(fields, home=home, chat_id=chat_id))
    except urllib.error.HTTPError as error:
        if error.code >= 500:
            return SendResult(success=False, error='The Coconut service did not answer.', retryable=True)
        # For example a chat that is not a bot of this profile. A second try or a plain text gets the same answer.
        LOG.warning('Coconut: the service refused a %s outside a Coconut turn for chat %s (HTTP %s).',
                    fields['kind'], chat_id, error.code)
        return SendResult(success=False, error='Coconut refused this message.',
                          raw_response={'coconut_refused': True, 'status': error.code})
    except Exception as error:
        LOG.warning('Coconut: the Coconut service did not take a %s outside a Coconut turn (%s).', fields['kind'],
                    type(error).__name__)
        return SendResult(success=False, error='The Coconut service did not answer.', retryable=True)


def markdown_image(url, caption=None) -> str:
    """One Markdown image line for a photo: `![caption](url)`. The phone shows the line as a photo, and an app that
    does not know photos shows the image in the text."""
    alt = ' '.join(str(caption or '').split())
    for character in '\\[]':
        alt = alt.replace(character, '\\' + character)
    url = str(url).strip()
    # An address with a space or a parenthesis must stand in angle brackets.
    return f'![{alt}](<{url}>)' if any(character in url for character in ' ()') else f'![{alt}]({url})'


def turn_of(message_id) -> str:
    """The turn ID in a message ID that this adapter gave out (`<turn>:<number>`)."""
    return str(message_id).partition(':')[0]


class CoconutAdapter(BasePlatformAdapter):
    # The phone shows a message only when it is complete. Without message edits, Hermes streams no partial text to this
    # platform (gateway/run_turn_runner.py, _setup_stream_consumer), whatever the person's `streaming` setting. Hermes
    # still calls edit_message to replace a complete text.
    SUPPORTS_MESSAGE_EDITING = False
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
        # The compressions that this adapter started after a turn (_compress).
        self._compressions: set = set()
        # The runs that Hermes started by itself in a chat, such as the completion of a background process: the
        # message ID of the run -> the Coconut turn that the service opened for it (_open_hermes_run).
        self._hermes_runs: dict[str, str] = {}
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
        self._mark_disconnected()

    async def _request(self, route: str, payload: dict, timeout: float = REQUEST_SECONDS) -> dict:
        return await in_thread(self._bridge.request, route, payload, timeout)

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
        # Text that starts with "/" stays a chat message. The service names a command that the person typed (`command`).
        if turn.get('session_root'):
            # hooks.join_conversation reads it before the gateway runs the turn.
            self._roots[str(turn['chat_id'])] = str(turn['session_root'])
        if turn.get('mode'):
            MODES[str(turn['id'])] = str(turn['mode'])
        if turn.get('mode') == 'technical':
            LANE_CHATS.add(str(turn['chat_id']))
        else:
            LANE_CHATS.discard(str(turn['chat_id']))
        if turn.get('folder'):
            FOLDERS[str(turn['id'])] = str(turn['folder'])
        text = str(turn['text'])
        if turn.get('command'):
            # The person typed a command of Hermes (gateway_chat.gateway_command). It reaches Hermes as the command,
            # without Coconut's context.
            return MessageEvent(text=str(turn['command']), message_type=MessageType.COMMAND, source=source,
                                message_id=str(turn['id']))
        records = [record for record in turn.get('records') or [] if isinstance(record, dict)
                   and isinstance(record.get('id'), str) and isinstance(record.get('text'), str)]
        if turn.get('own') is not None and CONTEXT_MIDDLEWARE['registered']:
            # Hermes saves the turn's own text. The model calls of this turn get the whole text (hooks.add_turn_context).
            CONTEXTS[str(turn['id'])] = text
            text = str(turn['own'])
            if records:
                # hooks.add_records adds the records that the session of the chat does not hold yet.
                RECORDS[str(turn['id'])] = records
        elif records:
            # Hermes saves the whole text in each turn, so each turn gets every record. The note about changed
            # instructions would repeat in each turn, so it stays out (hooks.add_records).
            from .hooks import records_text
            text += records_text([record for record in records if record.get('kind') != 'instructions'])
        media_urls, media_types = cached_images(turn.get('images'))
        if len(media_urls) < len(turn.get('images') or []):
            note = '\n\nAn attached image could not be opened. Do not claim to have seen it.'
            text += note
            if str(turn['id']) in CONTEXTS:
                CONTEXTS[str(turn['id'])] += note
        # A turn that may stay silent, such as a reaction, needs no reply. Hermes then keeps a silence marker silent
        # instead of a notice (gateway.response_filters.silence_allowed).
        return MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                            message_id=str(turn['id']), allow_gateway_control=False,
                            reply_expected=False if turn.get('silent') else None,
                            media_urls=media_urls, media_types=media_types)

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
        # A turn that the service gave is a Coconut turn. Hermes also runs its own inputs through the adapter, for
        # example the completion of a background process. Such a run gets a Coconut turn of its own, so its messages
        # go to the chat and the coconut tools act for it.
        if event.message_id in self._active:
            self._turns[event.source.chat_id] = event.message_id
        elif getattr(event, 'internal', False) and event.source.chat_id not in self._turns:
            await self._open_hermes_run(event)

    async def _open_hermes_run(self, event: MessageEvent) -> None:
        """Ask the service for a turn for a run that Hermes started. Without the service, or while the chat runs
        another turn, the run's sends go to the chat as messages outside a turn, as before."""
        chat_id = str(event.source.chat_id)
        try:
            answer = await self._request('/v1/gateway/wake', {'home': self._home, 'chat_id': chat_id,
                                                              'message_id': str(event.message_id)})
        except Exception as error:
            LOG.warning('Coconut: the service did not open a turn for a Hermes run in chat %s (%s).', chat_id,
                        type(error).__name__)
            return
        turn = answer.get('turn_id')
        if turn and chat_id not in self._turns:
            self._hermes_runs[str(event.message_id)] = str(turn)
            self._turns[chat_id] = str(turn)
            HERMES_RUNS.add(str(turn))

    async def _close_hermes_run(self, event: MessageEvent, outcome) -> None:
        turn = self._hermes_runs.pop(str(event.message_id))
        if self._turns.get(event.source.chat_id) == turn:
            del self._turns[event.source.chat_id]
        quiet = forget_turn(turn)
        self._typing.discard(turn)
        for delay in (*RETRY_SECONDS, None):
            if await self._report(turn, 'done', outcome=getattr(outcome, 'value', str(outcome)), quiet=quiet):
                break
            if delay is not None:
                await asyncio.sleep(delay)
        self._compress_after(event.source.chat_id)

    def _compress_after(self, chat_id: str) -> None:
        """Compress a chat whose last turn reached the compression threshold of Hermes (note_prompt_size)."""
        if chat_id in COMPRESS:
            COMPRESS.discard(chat_id)
            # The loop keeps only a weak reference to a task.
            task = asyncio.ensure_future(self._compress(chat_id))
            self._compressions.add(task)
            task.add_done_callback(self._compressions.discard)

    async def on_processing_complete(self, event: MessageEvent, outcome) -> None:
        if str(event.message_id) in self._hermes_runs:
            await self._close_hermes_run(event, outcome)
            return
        if event.message_id not in self._active:
            return
        if self._turns.get(event.source.chat_id) == event.message_id:
            del self._turns[event.source.chat_id]
        self._typing.discard(event.message_id)
        quiet = forget_turn(event.message_id)
        MODES.pop(event.message_id, None)
        CONTEXTS.pop(event.message_id, None)
        FOLDERS.pop(event.message_id, None)
        FOLDER_TASKS.pop(event.message_id, None)
        # The turn stays active in the polls until the service has its end. Otherwise the service would fail a
        # finished turn after a short outage.
        RECORDS.pop(event.message_id, None)
        from .hooks import commit_records
        if event.message_id in BUSY:
            BUSY.discard(event.message_id)
            outcome = 'busy'
            commit_records(event.message_id, held=False)
        else:
            # The session saved the turn's text with its new records.
            commit_records(event.message_id)
        for delay in (*RETRY_SECONDS, None):
            if await self._report(event.message_id, 'done', outcome=getattr(outcome, 'value', str(outcome)),
                                  quiet=quiet):
                break
            if delay is not None:
                await asyncio.sleep(delay)
        self._active.discard(event.message_id)
        self._compress_after(event.source.chat_id)

    async def _compress(self, chat_id: str) -> None:
        """Compress the session of a chat, as `/compress` does, after a turn whose prompt reached the compression
        threshold of Hermes. Hermes compresses a chat before a model call only when the size of the call is anchored
        on real usage, and a gateway turn of one call never gets that anchor (docs/architecture.md). Hermes's notice of
        the compression stays in the log."""
        source = self.build_source(chat_id=chat_id, chat_type='dm', user_id='coconut', user_name='Coconut')
        try:
            await self.handle_message(MessageEvent(text='/compress', message_type=MessageType.COMMAND, source=source,
                                                   message_id=COMPRESSION + str(uuid.uuid4())))
        except Exception:
            LOG.exception('Coconut: the gateway did not compress chat %s.', chat_id)

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        turn = self._turns.get(str(chat_id))
        if str(reply_to or '').startswith(COMPRESSION):
            # The notice of a compression that Coconut started is no message for the person.
            LOG.info('Coconut: a compression notice for chat %s stays in the log: %s', chat_id, str(content)[:200])
            return SendResult(success=True)
        if turn is None:
            return await self._send_outside(str(chat_id), str(content), metadata or {})
        if ends_quietly(turn, content):
            LOG.info('Coconut: turn %s ended without text from the model. Its closing text stays in the log.', turn)
            return SendResult(success=True)
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
        fields = dict(kind='send', message_id=message_id, text=content)
        if metadata.get('job_id'):
            # A cron delivery. The service shows no second copy of a run that is already a routine task in the chat.
            fields['job_id'] = str(metadata['job_id'])
        result = await self._outside(chat_id, fields)
        return SendResult(success=True, message_id=message_id) if result is True else result

    async def _outside(self, chat_id: str, fields: dict):
        """Report a message outside a turn to the service. True, or the failed SendResult."""
        answer = await report_outside(self._request, self._home, chat_id, fields)
        if isinstance(answer, SendResult):
            return answer
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

    async def send_image(self, chat_id: str, image_url: str, caption=None, reply_to=None, metadata=None) -> SendResult:
        # Hermes takes each Markdown image out of a reply and sends it here. Without this method it sends the caption
        # and the address as two lines of text, which the phone can only guess is a photo.
        return await self.send(chat_id, markdown_image(image_url, caption), reply_to=reply_to, metadata=metadata)

    async def send_animation(self, chat_id: str, animation_url: str, caption=None, reply_to=None,
                             metadata=None) -> SendResult:
        return await self.send(chat_id, markdown_image(animation_url, caption), reply_to=reply_to, metadata=metadata)

    async def send_image_file(self, chat_id: str, image_path: str, caption=None, reply_to=None, metadata=None,
                              **kwargs) -> SendResult:
        """A photo that the bot saved on this Mac. The service keeps its bytes as an attachment of the bot, and the
        phone loads them through Coconut, so the photo needs no public address."""
        path = self.validate_media_delivery_path(image_path)
        try:
            content = Path(path).read_bytes() if path and os.path.getsize(path) <= PHOTO_LIMIT else None
        except OSError:
            content = None
        if content is not None:
            try:
                answer = await self._request('/v1/gateway/files', {
                    'home': self._home, 'chat_id': str(chat_id), 'filename': Path(path).name,
                    'content_base64': base64.b64encode(content).decode()})
            except Exception as error:
                LOG.warning('Coconut: the service did not keep a photo for chat %s (%s).', chat_id, type(error).__name__)
                answer = {}
            if answer.get('id'):
                return await self.send(chat_id, markdown_image(f'coconut-file://{answer["id"]}', caption),
                                       reply_to=reply_to, metadata=metadata)
        # A file that is not photo data, or that the service refused as a photo, goes as a file card instead.
        return await self.send_document(chat_id, image_path, caption=caption, reply_to=reply_to, metadata=metadata)

    async def send_document(self, chat_id: str, file_path: str, caption=None, file_name=None, reply_to=None,
                            metadata=None, **kwargs) -> SendResult:
        """A file that the bot saved on this Mac, for example a PDF or a CSV. The service keeps its bytes as an
        attachment of the bot and posts it in the chat as a file card that opens it. Hermes's own method only posts
        "Couldn't deliver the file attachment.", because the platform had no way to send a file."""
        path = self.validate_media_delivery_path(file_path)
        name = os.path.basename(file_name or file_path or '') or 'file'
        if caption:
            await self.send(chat_id, caption, reply_to=reply_to, metadata=metadata)
        try:
            size = os.path.getsize(path) if path else None
        except OSError:
            size = None
        if size is None:
            return await self.send(chat_id, f'{name} could not be sent: the file is not readable on this Mac.',
                                   reply_to=reply_to, metadata=metadata)
        if size > PHOTO_LIMIT:
            return await self.send(chat_id, f'{name} is {size / 1_048_576:.1f} MB, and Coconut sends files of at most '
                                   f'{PHOTO_LIMIT // 1_048_576} MB. It stays on this Mac.',
                                   reply_to=reply_to, metadata=metadata)
        try:
            content = Path(path).read_bytes()
            turn = turn_for_chat(str(chat_id))[1]
            answer = await self._request('/v1/gateway/files', {
                'home': self._home, 'chat_id': str(chat_id), 'filename': name, 'document': True,
                'content_base64': base64.b64encode(content).decode(), **({'turn_id': turn} if turn else {})})
        except Exception as error:
            LOG.warning('Coconut: the service did not keep a file for chat %s (%s).', chat_id, type(error).__name__)
            answer = {}
        if answer.get('message_id'):
            return SendResult(success=True, message_id=answer['message_id'])
        return await self.send(chat_id, f'{name} could not be sent to Coconut.', reply_to=reply_to, metadata=metadata)

    async def send_video(self, chat_id: str, video_path: str, caption=None, reply_to=None, metadata=None,
                         **kwargs) -> SendResult:
        # A video goes as a file: the file card plays it.
        return await self.send_document(chat_id, video_path, caption=caption, reply_to=reply_to, metadata=metadata)

    async def send_voice(self, chat_id: str, audio_path: str, caption=None, reply_to=None, metadata=None,
                         **kwargs) -> SendResult:
        # Audio goes as a file too.
        return await self.send_document(chat_id, audio_path, caption=caption, reply_to=reply_to, metadata=metadata)

    async def send_private_notice(self, chat_id: str, user_id, content: str, reply_to=None, metadata=None) -> SendResult:
        # The phone shows the replies of Hermes, not its gateway notices. The gateway log keeps them.
        LOG.info('Coconut: a gateway notice for chat %s stays in the log: %s', chat_id, str(content)[:200])
        return SendResult(success=True)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        if str(message_id).startswith(OUTSIDE):
            result = await self._outside(str(chat_id), dict(kind='edit', message_id=str(message_id), text=str(content)))
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
        options = [dict(id=choice, label=approval_label(choice, label), deny=choice == 'deny')
                   for label, choice, _ in prompt.actions]
        return await self._question(prompt.chat_id, ('approval', prompt.session_key), question_kind='approval',
                                    text=str(prompt.description or 'Allow this command?'), detail=str(prompt.command),
                                    choices=options)

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
        except Exception:
            LOG.exception('Coconut: Hermes did not take the answer to a question.')

    async def get_chat_info(self, chat_id: str) -> dict:
        return {'name': 'Coconut', 'type': 'dm', 'chat_id': chat_id}
