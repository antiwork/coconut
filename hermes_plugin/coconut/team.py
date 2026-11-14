"""The adapter of the mode `team`: the team chat of a Mac that runs Coconut for a team only.

`gateway.platforms.coconut.extra.mode: team` in the profile turns this mode on. Only the operator's command
`coconut team connect-chat` writes it (scripts/team_chat.py). In this mode the plugin registers the platform and this
adapter, and nothing else: no tool, no hook, no skill and no change of cron. See backend/coconut/team_turns.py for the
service side.

The service gives a turn for each message of the team chat that names the bot, and for each direct message of a person
to the bot. The adapter gives each turn to the gateway as a message of a group chat: the chat is the bot in one chat of
a team (`<bot>:team:<room>` for the Team chat, `<bot>:team:<room>:<chat>` for a named chat, and
`<bot>:team:<room>:direct:<member>` for a direct chat), the sender is the member of the team, and the earlier messages
of the chat are `channel_context`. A direct chat is a group chat too, not the chat type `dm`: Hermes adds a
first-contact note to a `dm` chat and writes to the config of the Hermes home. Its chat ID is its own, so it has its own
session, and its rules tell the agent that one person reads it. The adapter does not read the chat ID: it gives it back
to the service with each send. Each reply goes back to the service.

A production agent runs in this gateway. Each entry point of the adapter catches its own faults, so a fault of Coconut
never stops the gateway or its other channels. A Hermes without the fields that the adapter needs gets one log line
and no Coconut channel (`missing`). A Hermes without a field of the registration that the chat needs (`unregistered`)
gets a channel that takes no message: the adapter polls, each poll names what Hermes lacks, and the service tells the
team that Hermes needs an update (backend/coconut/team_turns.py).
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import functools
import logging
import time

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from .adapter import REQUEST_SECONDS, RETRY_SECONDS, CoconutAdapter

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


def missing() -> str | None:
    """What this Hermes lacks for the adapter of the team chat, or None."""
    fields = {field.name for field in dataclasses.fields(MessageEvent)}
    lacking = [name for name in EVENT_FIELDS if name not in fields]
    lacking += [name for name in BASE_METHODS if not callable(getattr(BasePlatformAdapter, name, None))]
    return ', '.join(lacking) or None


def unregistered(known) -> str | None:
    """The fields of REGISTRATION_FIELDS that are not in `known` (the fields of PlatformEntry), or None."""
    return ', '.join(name for name in REGISTRATION_FIELDS if name not in known) or None


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
