"""The adapter of the mode `team`: the team chat of a Mac that runs Coconut for a team only.

`gateway.platforms.coconut.extra.mode: team` in the profile turns this mode on. Only the operator's command
`coconut team connect-chat` writes it (scripts/team_chat.py). In this mode the plugin registers the platform, this
adapter and the guard of the direct chats (privacy.py), and nothing else: no tool, no hook, no skill and no change of
cron. See backend/coconut/team_turns.py for the service side.

The service gives a turn for each message of the team chat that names the bot, and for each direct message of a person
to the bot. The adapter gives each turn to the gateway as a message of a group chat: the chat is the bot in one chat of
a team (`<bot>:team:<room>` for the Team chat, `<bot>:team:<room>:<chat>` for a named chat, and
`<bot>:team:<room>:direct:<member>` for a direct chat), the sender is the member of the team, and the earlier messages
of the chat are `channel_context`. A direct chat is a group chat too, not the chat type `dm`: Hermes adds a
first-contact note to a `dm` chat and writes to the config of the Hermes home. Its chat ID is its own, so it has its own
session, its rules tell the agent that one person reads it, and the guard keeps its text out of the other chats. The
adapter does not read the chat ID: it gives it back to the service with each send. Each reply goes back to the service.

A production agent runs in this gateway. Each entry point of the adapter catches its own faults, so a fault of Coconut
never stops the gateway or its other channels. A Hermes without the fields that the adapter needs gets one log line
and no Coconut channel (`missing`). A Hermes without a field of the registration that the chat needs (`unregistered`),
or without the middleware of the guard, gets a channel that takes no message: the adapter polls, each poll names what
Hermes lacks, and the service tells the team that Hermes needs an update (backend/coconut/team_turns.py).

A cron job can deliver its result into a chat of the team: `deliver: coconut:<chat ID>`, for example
`coconut:<bot>:team:<room>`. A cron run can run in a process without a gateway. There Hermes calls the sender of the
registration (`standalone_sender_fn`, send_standalone), which sends the text as the adapter sends a message outside a
turn. Only the mode `team` registers it: see send_standalone.
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import functools
import logging
import os
import time
import uuid

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from .adapter import DEFAULT_BRIDGE, OUTSIDE, REQUEST_SECONDS, RETRY_SECONDS, Bridge, CoconutAdapter, report_outside

LOG = logging.getLogger(__name__)
# A running turn stops when the adapter cannot reach the service for this long: its reply can no longer reach the chat,
# and its tools must not run on unseen. A restart of the service takes some seconds, and the new process names the turn
# as unknown at once, which stops it too. An update drains the service first, so no turn runs while it restarts.
LOST_SECONDS = 30
# The fields of a message event that a turn of the team chat sets.
EVENT_FIELDS = ('text', 'message_type', 'source', 'message_id', 'user_id', 'user_name', 'allow_gateway_control',
                'reply_expected', 'channel_context', 'channel_prompt', 'reply_to_message_id', 'reply_to_text',
                'reply_to_is_own_message')
# What the adapter calls on the base adapter of Hermes.
BASE_METHODS = ('build_source', 'handle_message', 'send_image_file', '_mark_connected', '_mark_disconnected')
# The fields of the registration (PlatformEntry) without which the team chat does not work. Without
# `trusted_inbound`, Hermes refuses each member of the team as an unknown user and ends the turn with no reply. Without
# `display_overrides`, Hermes sends its tool progress into the chat as messages of the bot.
REGISTRATION_FIELDS = ('trusted_inbound', 'display_overrides')
# A cron delivery without a gateway (send_standalone) tries three times: the waits between the tries are the first two
# waits of the adapter. A refused connection fails at once, so the tries cover about 3 seconds of a service that is
# down or a bridge file that is missing: a short gap, not a whole restart of the service.
STANDALONE_WAITS = RETRY_SECONDS[:2]
# Each try waits this long for the service: one relay try of the service (team_relay.TIMEOUT_SECONDS). All tries take
# at most 3 x 15 + 1 + 2 = 48 seconds, under the bound of 60 seconds of Hermes (`cron.standalone_send_timeout_seconds`).
# One part can take the service about 46 seconds when the relay does not answer (3 x 15 + 2 x 0.5), and n parts n
# times that. A later try waits for the lock of the room and finds the entry of an earlier try, so the sender reports
# a false failure only when all parts together take longer than its tries.
STANDALONE_SECONDS = 15


def missing() -> str | None:
    """What this Hermes lacks for the adapter of the team chat, or None."""
    fields = {field.name for field in dataclasses.fields(MessageEvent)}
    lacking = [name for name in EVENT_FIELDS if name not in fields]
    lacking += [name for name in BASE_METHODS if not callable(getattr(BasePlatformAdapter, name, None))]
    return ', '.join(lacking) or None


def unregistered(known) -> str | None:
    """The fields of REGISTRATION_FIELDS that are not in `known` (the fields of PlatformEntry), or None."""
    return ', '.join(name for name in REGISTRATION_FIELDS if name not in known) or None


async def send_tries(request, home: str, chat_id: str, fields: dict):
    """Send a message outside a turn (`fields`, with its message ID) with `request`, in up to three tries with the
    waits of STANDALONE_WAITS. A try follows a service that did not answer, and an answer `recorded: false` with
    `retry: true`. Each try has the same message ID, so a repeat adds no second message. The last answer of the
    service, or the failed SendResult (report_outside)."""
    for wait in (*STANDALONE_WAITS, None):
        answer = await report_outside(request, home, chat_id, fields)
        retry = answer.retryable if isinstance(answer, SendResult) else (
            isinstance(answer, dict) and not answer.get('recorded') and answer.get('retry') is True)
        if wait is None or not retry:
            return answer
        await asyncio.sleep(wait)


async def send_standalone(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False,
                          **options) -> dict:
    """The sender of a cron delivery in a process without a gateway (`standalone_sender_fn` of the registration).

    It sends the text as the adapter sends a message outside a turn: `kind: send` with the Hermes home of this process
    and a new message ID, to the service of the bridge file. The service shows it as a message of the bot in the chat
    `chat_id`. A service that does not answer, and an answer `recorded: false` with `retry: true` (the relay did not
    take a part), get up to two more tries with the same message ID. The service derives the entry ID from the
    message ID, so a try that repeats a send that arrived adds no second message. A refusal gets no second try: an
    HTTP 4xx, `recorded: false` without `retry`, or an empty text. A refusal, and a last try that failed, become an
    `error` with the reason, so the record of the job shows a failed delivery. A fault never reaches Hermes. The team
    chat takes no file from this path: the text goes, and a log line says that the files did not go. Hermes itself
    adds a warning to the result of a send with files, and so records a failed delivery.

    Only the mode `team` registers this sender. On a person's Mac, the service drops a cron delivery whose run shows
    already as a routine task in the chat, by the job ID of the delivery (gateway_chat.py). This call has no job ID,
    so there the chat could show a run two times."""
    try:
        text = str(message or '')
        if not text.strip():
            return {'error': 'Coconut sends no empty message into a team chat.'}
        extra = getattr(pconfig, 'extra', None)
        bridge = Bridge((extra.get('bridge') if isinstance(extra, dict) else None) or DEFAULT_BRIDGE)
        from hermes_constants import get_hermes_home
        home = os.path.realpath(get_hermes_home())
        message_id = OUTSIDE + str(uuid.uuid4())

        async def request(route, payload):
            return await asyncio.to_thread(bridge.request, route, payload, STANDALONE_SECONDS)
        answer = await send_tries(request, home, str(chat_id), dict(kind='send', message_id=message_id, text=text))
        if isinstance(answer, SendResult):
            status = answer.raw_response.get('status') if isinstance(answer.raw_response, dict) else None
            if status == 404:
                return {'error': 'Coconut refused this message: the chat is not a team chat of an agent of this '
                                 'Hermes home.'}
            return {'error': answer.error + (f' (HTTP {status})' if status else '')}
        if not isinstance(answer, dict) or not answer.get('recorded'):
            reason = answer.get('reason') if isinstance(answer, dict) else None
            return {'error': 'Coconut did not show this message: ' + str(reason or 'no reason')}
        if media_files:
            LOG.warning('Coconut: a cron delivery into a team chat sent its text without its %d files.',
                        len(media_files))
        return {'success': True, 'message_id': message_id}
    except Exception as error:
        LOG.warning('Coconut: a cron delivery into a team chat failed (%s).', type(error).__name__)
        return {'error': f'Coconut did not take this message ({type(error).__name__}).'}


def guarded(result=None):
    """Catch each fault of an entry point of the adapter, log it once, and give `result` to Hermes."""
    def wrap(method):
        @functools.wraps(method)
        async def call(self, *arguments, **options):
            try:
                return await method(self, *arguments, **options)
            except Exception:
                LOG.exception('Coconut: the team chat did not finish %s.', method.__name__)
                return result() if callable(result) else result
        return call
    return wrap


def failed():
    return SendResult(success=False, error='Coconut did not take this message.')


class TeamAdapter(CoconutAdapter):
    """The adapter of CoconutAdapter, with group events and without the parts of a phone: no card, no session join,
    no compression, no photo file and no turn for a run of Hermes."""

    # What this Hermes lacks for the team chat (`unregistered`), or None. register_team sets it. With it, each poll
    # names it, the service gives no turn, and the adapter gives no turn to Hermes.
    lacking: str | None = None
    # The service splits a long text into parts itself (team_turns.parts). Without this mark, Hermes cuts a cron
    # delivery over 4,000 characters and adds the path of a file on this Mac (gateway/delivery.py,
    # _cap_oversized_output).
    splits_long_messages = True

    def __init__(self, config):
        super().__init__(config)
        # The name of each team chat, for get_chat_info.
        self._names: dict[str, str] = {}
        # The source of each running turn: its member is the user of its session key, when Hermes keeps a session for
        # each member of a group (group_sessions_per_user). A `/stop` with that source finds that session.
        self._sources: dict[str, object] = {}
        # The turns that this adapter gave to Hermes and that did not end. The service gives a turn again only when
        # no adapter showed it (gateway_bridge.HANDOFF_SECONDS), so this guard against a second run never acts on a
        # living adapter: it keeps the rule "at most once" also if the service is wrong. (The base adapter of Hermes
        # has its own `_running`, so the name is of this adapter.)
        self._team_runs: set[str] = set()
        # The turns that this adapter stopped, newest last. A late send of such a turn stays in the log, so the chat
        # never shows a reply after the notice of the service.
        self._stopped: collections.OrderedDict[str, None] = collections.OrderedDict()
        # The monotonic time of the last request that reached the service, and the stops that run (the loop keeps only
        # a weak reference to a task).
        self._reached = time.monotonic()
        self._stop_tasks: set = set()

    def _event(self, turn: dict) -> MessageEvent:
        if self.lacking:
            # Hermes would refuse the sender or show wrong content. The poll skips the turn, and the service ends it.
            raise ValueError('This Hermes cannot run a turn of the team chat.')
        try:
            team = turn['team']
            chat = str(turn['chat_id'])
            self._names[chat] = str(turn.get('chat_name') or 'Coconut')
            member, name = str(team['member']), str(team['member_name'])
            source = self.build_source(chat_id=chat, chat_name=self._names[chat], chat_type='group', user_id=member,
                                       user_name=name)
            answered = team.get('reply_to') or {}
            self._sources[str(turn['id'])] = source
            return MessageEvent(text=str(turn['text']), message_type=MessageType.TEXT, source=source,
                                message_id=str(turn['id']), user_id=member, user_name=name,
                                allow_gateway_control=False, reply_expected=True,
                                channel_context=team.get('context') or None, channel_prompt=team.get('rules') or None,
                                reply_to_message_id=answered.get('id'), reply_to_text=answered.get('text'),
                                reply_to_is_own_message=bool(answered.get('own')))
        except Exception as error:
            # The poll skips a turn without its fields, and the service ends it.
            raise ValueError('A turn of the team chat without its fields.') from error

    @guarded()
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return await super().connect(is_reconnect=is_reconnect)

    @guarded()
    async def disconnect(self) -> None:
        await super().disconnect()

    async def handle_message(self, event: MessageEvent) -> None:
        if event.message_type is MessageType.TEXT and event.message_id in self._team_runs:
            LOG.warning('Coconut: the team chat got a turn again that runs already. It runs once.')
            return
        if event.message_type is MessageType.TEXT:
            self._team_runs.add(event.message_id)
        await super().handle_message(event)

    @guarded()
    async def _stop(self, chat_id: str) -> None:
        """Stop the running turn of a chat, as `/stop` does, with the source of that turn: the session key of Hermes
        has the member of the turn, so a `/stop` of another user would reach another session."""
        turn = self._turns.get(chat_id)
        source = self._sources.get(turn) if turn is not None else None
        if source is None:
            return
        self._stopped[turn] = None
        while len(self._stopped) > 256:
            self._stopped.popitem(last=False)
        await self.handle_message(MessageEvent(text='/stop', message_type=MessageType.COMMAND, source=source,
                                               message_id=turn + ':stop'))

    @guarded()
    async def on_processing_start(self, event: MessageEvent) -> None:
        # Only a turn of the service is a Coconut turn. A run that Hermes starts by itself sends outside a turn.
        if event.message_id in self._active:
            self._turns[event.source.chat_id] = event.message_id

    @guarded()
    async def on_processing_complete(self, event: MessageEvent, outcome) -> None:
        if event.message_id not in self._active:
            return
        if self._turns.get(event.source.chat_id) == event.message_id:
            del self._turns[event.source.chat_id]
        self._typing.discard(event.message_id)
        self._sources.pop(event.message_id, None)
        self._team_runs.discard(event.message_id)
        for delay in (*RETRY_SECONDS, None):
            if await self._report(event.message_id, 'done', outcome=getattr(outcome, 'value', str(outcome))):
                break
            if delay is not None:
                await asyncio.sleep(delay)
        # The turn stays active in the polls until the service has its end.
        self._active.discard(event.message_id)

    @guarded(failed)
    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        anchor = str(reply_to or '')
        if anchor in self._stopped or anchor.removesuffix(':stop') in self._stopped:
            # The reply of a stopped turn, or the answer of Hermes to its `/stop`: the turn ended for the chat.
            LOG.info('Coconut: a send of a stopped turn of the team chat stays in the log.')
            return SendResult(success=True)
        return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def _send_outside(self, chat_id: str, content: str, metadata: dict) -> SendResult:
        """A cron delivery (a send with the `job_id` of Hermes) goes as send_standalone sends it: the same tries, and
        a failed SendResult when the chat does not show the whole text, so the record of the job shows a failed
        delivery. A failure without a shown part lets Hermes try send_standalone. A failure after some shown parts
        is a partial delivery (`partial_overflow`), so Hermes sends nothing again. Each other send outside a turn
        goes as in the full mode: an answer `recorded: false` is a success there."""
        if not metadata.get('job_id') or metadata.get('_interim_send'):
            return await super()._send_outside(chat_id, content, metadata)
        message_id = OUTSIDE + str(uuid.uuid4())

        async def request(route, payload):
            return await self._request(route, payload, STANDALONE_SECONDS)
        answer = await send_tries(request, self._home, chat_id, dict(kind='send', message_id=message_id,
                                                                     text=content, job_id=str(metadata['job_id'])))
        if isinstance(answer, SendResult):
            return answer
        if isinstance(answer, dict) and answer.get('recorded'):
            LOG.info('Coconut: a cron delivery went to chat %s (%d characters).', chat_id, len(content))
            return SendResult(success=True, message_id=message_id)
        reason = str((answer.get('reason') if isinstance(answer, dict) else None) or 'no reason')
        LOG.warning('Coconut: chat %s does not show a cron delivery: %s', chat_id, reason)
        raw = {'coconut_refused': True}
        if isinstance(answer, dict) and answer.get('shown'):
            raw['partial_overflow'] = True
        return SendResult(success=False, error='Coconut did not show this message: ' + reason, raw_response=raw)

    async def _request(self, route: str, payload: dict, timeout: float = REQUEST_SECONDS) -> dict:
        """A request to the service. A poll answer can name active turns that the service does not know, for example
        after its restart: the adapter stops them. A service that this adapter cannot reach for LOST_SECONDS stops
        each running turn. A poll names what this Hermes lacks for the team chat."""
        if route == '/v1/gateway/poll' and self.lacking:
            payload = dict(payload, lacking=self.lacking)
        try:
            answer = await super()._request(route, payload, timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            if time.monotonic() - self._reached > LOST_SECONDS:
                self._stop_turns(list(self._turns.values()), 'the Coconut service does not answer')
            raise
        self._reached = time.monotonic()
        if route == '/v1/gateway/poll' and isinstance(answer, dict) and answer.get('unknown'):
            self._stop_turns([str(turn) for turn in answer['unknown']], 'the Coconut service does not know them')
        return answer

    def _stop_turns(self, turns: list, reason: str) -> None:
        """Stop running turns of this adapter, each once. A fault stays here, as in each entry point."""
        try:
            chats = {turn: chat for chat, turn in self._turns.items()}
            for turn in turns:
                if turn in chats and turn not in self._stopped:
                    LOG.warning('Coconut: the team chat stops a turn, because %s.', reason)
                    task = asyncio.ensure_future(self._stop(chats[turn]))
                    self._stop_tasks.add(task)
                    task.add_done_callback(self._stop_tasks.discard)
        except Exception:
            LOG.exception('Coconut: the team chat did not stop a turn.')

    @guarded(failed)
    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        return await super().edit_message(chat_id, message_id, content, finalize=finalize)

    @guarded(False)
    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return await super().delete_message(chat_id, message_id)

    @guarded()
    async def send_typing(self, chat_id: str, metadata=None) -> None:
        await super().send_typing(chat_id, metadata=metadata)

    @guarded(failed)
    async def send_image_file(self, chat_id: str, image_path: str, caption=None, reply_to=None, metadata=None,
                              **kwargs) -> SendResult:
        # The team chat has no files. Hermes sends its notice, which does not name the path.
        return await BasePlatformAdapter.send_image_file(self, chat_id, image_path, caption=caption,
                                                         reply_to=reply_to, metadata=metadata, **kwargs)

    @guarded(failed)
    async def send_clarify(self, chat_id: str, question: str, choices, clarify_id: str, session_key: str,
                           metadata=None) -> SendResult:
        return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata=metadata)

    @guarded(failed)
    async def _send_exec_approval_prompt(self, prompt) -> SendResult:
        return await super()._send_exec_approval_prompt(prompt)

    async def get_chat_info(self, chat_id: str) -> dict:
        return {'name': self._names.get(str(chat_id), 'Coconut'), 'type': 'group', 'chat_id': chat_id}
