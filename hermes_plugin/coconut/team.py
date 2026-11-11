"""The adapter of the mode `team`: the team chat of a Mac that runs Coconut for a team only.

`gateway.platforms.coconut.extra.mode: team` in the profile turns this mode on. Only the operator's command
`coconut team connect-chat` writes it (scripts/team_chat.py). In this mode the plugin registers the platform and this
adapter, and nothing else: no tool, no hook, no skill and no change of cron. See backend/coconut/team_turns.py for the
service side.

The service gives a turn for each message of the team chat that names the bot. The adapter gives it to the gateway
as a message of a group chat: the chat is the bot in one team (`<bot>:team:<room>`), the sender is the member of the
team, and the earlier messages of the chat are `channel_context`. Each reply goes back to the service.

A production agent runs in this gateway. Each entry point of the adapter catches its own faults, so a fault of Coconut
never stops the gateway or its other channels. A Hermes without the fields that the adapter needs gets one log line
and no Coconut channel (`missing`).
"""
from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from .adapter import RETRY_SECONDS, CoconutAdapter

LOG = logging.getLogger(__name__)
# The fields of a message event that a turn of the team chat sets.
EVENT_FIELDS = ('text', 'message_type', 'source', 'message_id', 'user_id', 'user_name', 'allow_gateway_control',
                'reply_expected', 'channel_context', 'channel_prompt', 'reply_to_message_id', 'reply_to_text',
                'reply_to_is_own_message')
# What the adapter calls on the base adapter of Hermes.
BASE_METHODS = ('build_source', 'handle_message', 'send_image_file', '_mark_connected', '_mark_disconnected')


def missing() -> str | None:
    """What this Hermes lacks for the team chat, or None."""
    fields = {field.name for field in dataclasses.fields(MessageEvent)}
    lacking = [name for name in EVENT_FIELDS if name not in fields]
    lacking += [name for name in BASE_METHODS if not callable(getattr(BasePlatformAdapter, name, None))]
    return ', '.join(lacking) or None


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

    def __init__(self, config):
        super().__init__(config)
        # The name of each team chat, for get_chat_info.
        self._names: dict[str, str] = {}

    def _event(self, turn: dict) -> MessageEvent:
        try:
            team = turn['team']
            chat = str(turn['chat_id'])
            self._names[chat] = str(turn.get('chat_name') or 'Coconut')
            member, name = str(team['member']), str(team['member_name'])
            source = self.build_source(chat_id=chat, chat_name=self._names[chat], chat_type='group', user_id=member,
                                       user_name=name)
            answered = team.get('reply_to') or {}
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

    @guarded()
    async def _stop(self, chat_id: str) -> None:
        """Stop the running turn of a chat, as `/stop` does, with the source of a group chat."""
        turn = self._turns.get(chat_id)
        if turn is None:
            return
        source = self.build_source(chat_id=chat_id, chat_name=self._names.get(chat_id), chat_type='group',
                                   user_id='coconut', user_name='Coconut')
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
        for delay in (*RETRY_SECONDS, None):
            if await self._report(event.message_id, 'done', outcome=getattr(outcome, 'value', str(outcome))):
                break
            if delay is not None:
                await asyncio.sleep(delay)
        # The turn stays active in the polls until the service has its end.
        self._active.discard(event.message_id)

    @guarded(failed)
    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

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
