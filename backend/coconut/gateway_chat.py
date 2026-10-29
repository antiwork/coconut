"""The Hermes gateway that runs the chat turns of the bots.

A chat turn (gateway_bridge.plain_chat_turn) always goes to the `coconut` platform of the Hermes gateway. When no
adapter of the bot's profile is connected, for example while the supervised gateway starts again, the turn waits for
one, at most GATEWAY_WAIT_SECONDS, and the task says so. Then the task fails with the reason. The other turn types still
run in the Coconut worker until phase 5 moves them. Both continue the same native conversation of the bot
(hermes_plugin/coconut/hooks.py). A service without a Hermes home (fixture mode and tests) has no gateway, and its
provider runs every turn.

Hermes also sends to a bot's chat outside a Coconut turn, for example the reply of its own turn for the completion of a
background process (gateway_message). Such a message becomes a message of the bot in a finished task of its chat.

A gateway turn reaches Coconut in two more ways. A question of Hermes (clarify, command approval, coconut_ask) becomes a
decision card, and its answer returns to the waiting turn (record_gateway_question, answer_gateway_question). A tool of
the `coconut` toolset acts for the task of the turn (gateway_tool).

The service supervises one `hermes gateway run` process for the person's Hermes home. Hermes multiplexes the profiles
in that process. The service starts the process with its own start, stops it with its own stop, and writes its output
to `<data>/gateway/gateway.log`. When the process stops, the service starts it again after RESTART_SECONDS. When a
gateway that the service did not start already runs for that home, the service uses it and starts no second one.
GET /v1/setup says which gateway runs chats, or why none does.

The gateway's cron fires the routines of each bot that it serves, and each run is a Coconut turn (gateway_cron). A bot
whose profile no gateway serves, for example a profile that the person parked in Hermes, keeps its routines with
Coconut's scheduler (routine_coordinator.tick_native_routines). GET /v1/setup lists such profiles.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from . import hermes_runtime
from .gateway_bridge import WORKER_INPUTS, WORKER_MESSAGE_FIELDS, GatewayBridge, GatewayUnavailable, plain_chat_turn
from .native_conversation import reaction_only_turn

LOG = logging.getLogger(__name__)
CHECK_SECONDS = 5
RESTART_SECONDS = 30
STOP_SECONDS = 20
LOG_LIMIT_BYTES = 10 * 1024 * 1024
# `hermes <arguments>` in the Python of the person's Hermes. The source folder comes first on sys.path, as the `hermes`
# launcher puts it.
LAUNCH = ('import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ["hermes", *sys.argv[2:]]; '
          'from hermes_cli.main import main; sys.exit(main())')
# The workers get the same model keys. The service sets HERMES_DISABLE_LAZY_INSTALLS for its own Hermes processes.
ENVIRONMENT_KEYS = (*hermes_runtime.ENVIRONMENT_KEYS, 'TMPDIR', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY',
                    'OPENROUTER_API_KEY', 'HERMES_DISABLE_LAZY_INSTALLS')
USING = 'Coconut uses the Hermes gateway that already runs for {home} (process {pid}).'
NOT_LOADED = ('A Hermes gateway already runs for {home} (process {pid}), but it did not load the coconut platform. '
              'Restart that gateway after Coconut setup (hermes gateway restart), or stop it so that Coconut starts its '
              'own. Until then, chats wait.')
STOPPED = 'The Hermes gateway stopped with exit code {code}. Coconut starts it again soon. See {log}.'
# Messages outside a turn that arrive this close together share one task, for example a Kanban notice and the reply of
# the Hermes turn that it woke.
OUTSIDE_GROUP_SECONDS = 10 * 60
# A cron delivery repeats the answer of its run. A run in a bot's profile is a routine task in its chat (gateway_cron),
# so the delivery of a job with such a task in this time shows nothing new.
CRON_DELIVERY_SECONDS = 24 * 3600
# A chat turn waits this long for an adapter of the bot's profile, for example while the gateway starts again.
GATEWAY_WAIT_SECONDS = 5 * 60
WAITING = 'Waiting for the Hermes gateway. See Settings > Hermes when this takes long.'
NOT_CONNECTED = ('The Hermes gateway did not connect for this bot in {minutes} minutes. Send the message again after '
                 'the gateway runs. See GET /v1/setup and {log}.')
PARKED = ('The Hermes profile of this bot is parked ({path}), so no Hermes gateway serves its chats. Remove that file '
          'to chat with the bot. Its routines run with Coconut meanwhile.')


def gateway_turn_text(request) -> str | None:
    """The text of a chat turn for the gateway, with the context that the worker gives the same turn, or None for a
    turn whose input the gateway cannot take yet (WORKER_MESSAGE_FIELDS) or a turn without input.

    A quote, a reaction and a bot mention go into the text of each message (native_conversation.message_content). A
    reaction-only turn may stay silent. Earlier emoji feedback, the attached files and the pending cards follow the
    messages.
    """
    from .native_conversation import (REACTION_SILENCE, attachment_context, current_messages, message_content,
                                      pending_decisions_context, reaction_feedback_context)
    messages = current_messages(request)
    if any(message.get(field) for message in messages for field in WORKER_MESSAGE_FIELDS):
        return None
    task = request['task']
    text = '\n\n'.join(message_content(message) for message in messages)
    if reaction_only_turn(request):
        text += REACTION_SILENCE
    text += reaction_feedback_context(task.get('_reaction_feedback', []))
    text += attachment_context(task.get('attachments', []))
    if text.strip():
        text += pending_decisions_context(task)
    return text if text.strip() else None


def gateway_resume(task: dict, decision: dict, cron_session: str = '') -> bool:
    """Whether the gateway continues a task after an answered card.

    The task must be a chat turn that the gateway takes (plain_chat_turn), or a routine task of a gateway cron run
    whose Hermes session is known (`cron_session`): the answer continues that session, with the routine's prompt and
    tool results. A command approval of a worker turn stays with the worker: it allows one exact tool call that the
    worker recorded, and the worker repeats that call.
    """
    chat = plain_chat_turn(task) or (bool(cron_session) and not any(task.get(key) for key in WORKER_INPUTS))
    return chat and decision.get('action_type') != 'native_approval' and not decision.get('_receipt_id')


def live_gateway_pid(home) -> int | None:
    """The process ID of a running gateway of this Hermes home, or None. Hermes checks the identity of the process."""
    try:
        from gateway.status import live_gateway_pid_for_home
        return live_gateway_pid_for_home(Path(home))
    except Exception:
        pass
    # Without the Hermes modules, the pid file of the gateway is the evidence.
    try:
        text = (Path(home) / 'gateway.pid').read_text().strip()
        pid = int(json.loads(text)['pid']) if text.startswith('{') else int(text)
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError, KeyError, TypeError):
        return None


class GatewayChat:
    gateway_process = None
    gateway_supervisor = None

    def _init_gateway_chat(self):
        self.gateway_bridge = GatewayBridge(self.root)
        self.gateway_wake = threading.Event()
        self.gateway_state = {'state': 'off', 'pid': None, 'reason': None}
        self._gateway_retry_at = 0.0
        self._gateway_exit = None
        self.gateway_cron_threads: dict[str, threading.Thread] = {}
        # Earlier versions had a `chat_runtime` setting. The gateway is now the only chat runtime.
        self.db.execute("DELETE FROM settings WHERE key='chat_runtime'")

    def gateway_enabled(self) -> bool:
        """Whether this service runs on the person's Hermes and so has a gateway. Fixture mode and tests have none."""
        return bool(getattr(self, 'scan_profiles', False) and getattr(self.provider, 'python', None)
                    and getattr(self.provider, 'hermes_root', None))

    def gateway_status(self) -> dict:
        homes = set(self.gateway_bridge.connected_homes())
        profiles = sorted(row['name'] for row in self.profiles.rows.values()
                          if row.get('state') == 'active' and os.path.realpath(row['home']) in homes)
        # A profile that no gateway serves has no chats. Coconut runs its routines (gateway_served_bots).
        unserved = sorted(row['name'] for row in self.profiles.rows.values()
                          if row.get('state') == 'active' and os.path.realpath(row['home']) not in homes)
        return dict(self.gateway_state, connected_profiles=profiles, unserved_profiles=unserved)

    def gateway_served_bots(self) -> set[str]:
        """The bots whose profile a running gateway serves now: the coconut adapter of that home is connected.

        The gateway fires the routines of these bots. Coconut fires the routines of the other bots. The adapter
        connection is the evidence, not the config: a parked profile, a gateway that did not load the platform, or a
        gateway that starts again has no connected adapter.
        """
        if not self.gateway_enabled():
            return set()
        return {row['bot_id'] for row in list((self.profiles.rows or {}).values())
                if row.get('state') == 'active' and self.gateway_bridge.connected(row['home'])}

    def run_gateway_chat(self, task: dict, bot: dict, history: list, emit) -> bool:
        """Run a chat turn through the gateway. False: the turn is of another type, and the worker runs it."""
        if not self.gateway_enabled() or not plain_chat_turn(task):
            return False
        request = dict(operation='start', task=task, history=history)
        text = gateway_turn_text(request)
        if text is None:
            return False
        return self._run_in_gateway(task, bot, text, emit, silent=reaction_only_turn(request))

    def run_gateway_resume(self, task: dict, bot: dict, history: list, decision: dict, emit) -> bool:
        """Continue a task after an answered card in the gateway, as the worker resumes it. False: the worker does.

        `decision` is the stored card. The turn text is the worker's text for the same answer. A dismissed card has no
        answer, so it continues nothing. The card of a gateway cron run continues the Hermes session of that run."""
        if decision.get('status') == 'dismissed':
            return True
        with self.lock:
            cron_session = (self._get('tasks', task['id']).get('_gateway_cron') or {}).get('session_id') or ''
        if not self.gateway_enabled() or not gateway_resume(task, decision, cron_session):
            return False
        from .native_conversation import pending_decisions_context, resume_prompt
        card = self._public(decision) | {'action': decision.get('_action')}
        text = resume_prompt(dict(operation='resume', task=task, history=history, decision=card))
        return self._run_in_gateway(task, bot, text + pending_decisions_context(task), emit,
                                    session=cron_session or None)

    def run_gateway_context(self, task: dict, request_task: dict, bot: dict, history: list, context: dict,
                            emit) -> bool:
        """Answer a follow-up on a pending card in the gateway. False: the worker answers it.

        The worker answers a check of the card (`_decision_refresh`) and the results of background research, and each
        follow-up on a task that the gateway does not run. The turn cannot make a card or act for the task: the plugin
        blocks the card tools, and the service takes only text and coconut_resolve_reply from it.
        """
        if (not self.gateway_enabled() or not plain_chat_turn(task) or context.get('_decision_refresh')
                or context.get('_background_completion')):
            return False
        from .native_conversation import attachment_context, context_prompt
        from .service import ServiceError
        text = context_prompt(dict(operation='context', task=request_task, history=history,
                                   question=context['question']))
        text += attachment_context(request_task.get('attachments', []))

        def follow_up(kind, payload):
            # The answer is the text of the turn. The worker sends no completion for a follow-up either.
            if kind in ('phase', 'interim_message', 'interim_seal', 'completed'):
                return None
            if kind == 'gateway_question':
                raise ServiceError(409, 'A follow-up on a card cannot ask the person.')
            return emit(kind, payload)

        def wanted():
            return self._get('contexts', context['id'])['status'] == 'working'
        return self._run_in_gateway(task, bot, text, follow_up, context_id=context['id'], wanted=wanted)

    def _run_in_gateway(self, task: dict, bot: dict, text: str, emit, *, silent: bool = False,
                        context_id: str | None = None, wanted=None, session: str | None = None) -> bool:
        """Give a turn to the adapter of the bot's profile. Wait for one, at most GATEWAY_WAIT_SECONDS.

        The turn joins the bot's conversation, or the Hermes session `session`, for example of a cron run
        (hooks.join_conversation finds a session by its ID when no conversation has that root)."""
        from .native_conversation import session_root
        home = self.profiles.home(bot)
        deadline = time.monotonic() + GATEWAY_WAIT_SECONDS
        if wanted is None:
            def wanted():
                return self._get('tasks', task['id'])['status'] == 'working'
        try:
            while True:
                with self.lock:
                    if not wanted():
                        # The person cancelled the waiting turn.
                        return True
                if self.gateway_bridge.connected(home):
                    self._gateway_wait_summary(task['id'], None)
                    try:
                        # The worker resumes the chat of a bot from this root (native_conversation.NativeConversation).
                        self.gateway_bridge.run_turn(
                            task['id'], home, bot['id'], text, emit, chat_name=bot.get('name') or 'Coconut',
                            session_root=session or session_root(bot, epoch=task.get('_conversation_epoch')),
                            silent=silent,
                            context_id=context_id)
                        return True
                    except GatewayUnavailable:
                        # No adapter took the turn, so nothing ran. Wait for the next adapter.
                        LOG.warning('No Hermes gateway took chat turn %s. It waits for the gateway.', task['id'])
                parked = Path(home) / 'gateway.parked'
                if parked.exists():
                    # The person parked the profile in Hermes, so no gateway serves it. Waiting does not help.
                    raise RuntimeError(PARKED.format(path=parked))
                if time.monotonic() >= deadline or self.stopping.is_set():
                    raise RuntimeError(NOT_CONNECTED.format(minutes=GATEWAY_WAIT_SECONDS // 60,
                                                            log=self.gateway_bridge.folder / 'gateway.log'))
                if self._gateway_wait_summary(task['id'], WAITING):
                    # The supervisor checks the gateway now, not at its next check.
                    self.gateway_wake.set()
                self.stopping.wait(1)
        finally:
            self._gateway_wait_summary(task['id'], None)
            if context_id is None:
                self._close_gateway_questions(task['id'])

    def _gateway_wait_summary(self, task_id: str, text: str | None) -> bool:
        """Show on a working task that it waits for the gateway (text), or that it no longer waits (None). Return
        whether the task changed."""
        from .service import now
        with self.transaction():
            task = self._get('tasks', task_id)
            waiting = bool(task.get('_gateway_waiting'))
            if waiting == (text is not None) or (text is not None and task['status'] != 'working'):
                return False
            if text is None:
                task.pop('_gateway_waiting')
                if task['status'] == 'working':
                    task['summary'] = 'Working'
            else:
                task.update(_gateway_waiting=True, summary=text)
            task['updated_at'] = now()
            self._put('tasks', task)
            self._event('task.updated', self._public(task), task['bot_id'], task_id)
        return True

    def gateway_tool(self, data: dict) -> dict:
        """POST /v1/gateway/tools: a tool of the `coconut` toolset, for the task of a running gateway turn."""
        from .card_tools import PAUSED
        from .service import ServiceError, YieldForDecision
        try:
            return self._gateway_tool(data)
        except YieldForDecision as pause:
            if data.get('tool') == 'ask' and pause.decision_id:
                # The card is durable and the task waits for its answer, as after a card of the worker. The plugin
                # blocks each further tool call of the turn, and the answer continues the task in a later turn.
                return {'status': 'waiting', 'decision_id': pause.decision_id, 'paused': True, 'instruction': PAUSED}
            raise ServiceError(409, 'This Coconut turn no longer takes tool calls.') from None

    def _gateway_tool(self, data: dict) -> dict:
        from .service import ServiceError, require_text
        task_id = self.gateway_bridge.task_of(data.get('turn_id'))
        if task_id is None:
            raise ServiceError(409, 'This Coconut turn no longer runs.')
        arguments = data.get('arguments') if isinstance(data.get('arguments'), dict) else {}
        tool = data.get('tool')
        context_id = self.gateway_bridge.context_of(data.get('turn_id'))
        if context_id is not None:
            # A follow-up on a pending card answers the question and can record a typed answer to that card.
            if tool == 'resolve_reply':
                return self.resolve_decision_reply(context_id, arguments)
            raise ServiceError(409, 'Answer the current question. Its pending decision stays open.')
        if tool == 'ask':
            return self.native_event(task_id, 'native_question', arguments)
        if tool == 'suggest':
            return self.native_event(task_id, 'native_proposal', arguments)
        if tool == 'resolve_reply':
            return self.resolve_chat_decision(task_id, arguments)
        if data.get('tool') == 'track_pr':
            return self.emit(task_id, 'artifact', {'kind': 'pr', 'url': require_text(arguments, 'url', 500)})
        if data.get('tool') == 'message_bot':
            target = require_text(arguments, 'bot', 200).strip()
            with self.lock:
                source = self._get('tasks', task_id)
                bots = [bot for bot in self._all('bots') if not bot['archived'] and not bot.get('_deleted')
                        and bot['id'] != source['bot_id']]
            match = [bot for bot in bots if bot['id'] == target] or [
                bot for bot in bots if bot['name'].casefold() == target.casefold()]
            if len(match) != 1:
                names = ', '.join(sorted(bot['name'] for bot in bots)) or 'none'
                raise ServiceError(400, f'Name one other bot exactly. The other bots: {names}.')
            receipt = self.emit(task_id, 'bot_message', {'target_bot_id': match[0]['id'],
                                                         'text': require_text(arguments, 'text', 20000)})
            return {key: receipt[key] for key in ('status', 'target_bot_id', 'task_id') if key in receipt}
        raise ServiceError(404, 'This Coconut tool does not exist.')

    def gateway_message(self, data: dict) -> dict:
        """POST /v1/gateway/messages: a send, an edit or a delete of the gateway outside a Coconut turn.

        The chat must be the bot of the adapter's profile. A send becomes a message of the bot, and the push rules
        apply to it as to a reply. Return {recorded: false, reason} for a send that the chat does not show.
        """
        from .gateway_bridge import MAX_TEXT
        from .service import ServiceError, require_text
        kind, text = data.get('kind'), data.get('text', '')
        if kind not in ('send', 'edit', 'delete') or not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ServiceError(400, 'Send a send, an edit or a delete with a text.')
        home = os.path.realpath(require_text(data, 'home', 4096))
        chat_id, message_id = require_text(data, 'chat_id', 200), require_text(data, 'message_id', 200)
        if (self._gateway_profile(home) or {}).get('bot_id') != chat_id:
            raise ServiceError(404, 'This chat is not the bot of this Hermes profile.')
        with self.transaction():
            bot = self._get('bots', chat_id)
            if bot['archived'] or bot.get('_deleted'):
                return {'recorded': False, 'reason': 'The bot is archived or deleted.'}
            row = self.db.execute(
                "SELECT data FROM messages WHERE json_extract(data,'$.bot_id')=? "
                "AND json_extract(data,'$._gateway_message_id')=? ORDER BY rowid DESC LIMIT 1",
                (chat_id, message_id)).fetchone()
            message = json.loads(row[0]) if row else None
            if kind != 'send':
                if message is None:
                    return {'recorded': False, 'reason': 'The chat does not show this message.'}
                return self._change_gateway_message(bot, message, kind, text, bool(data.get('finalize')))
            if message is not None:
                return {'recorded': True, 'task_id': message['task_id']}
            if not text.strip():
                return {'recorded': False, 'reason': 'The message has no text.'}
            job_id = data.get('job_id')
            if isinstance(job_id, str) and job_id and self._gateway_cron_shown(chat_id, job_id):
                return {'recorded': False, 'reason': 'A routine task in the chat shows this cron run.'}
            # A streamed reply whose last edit never came ends with the next message.
            for (raw,) in self.db.execute(
                    "SELECT data FROM messages WHERE json_extract(data,'$.bot_id')=? "
                    "AND json_extract(data,'$._gateway_message_id') IS NOT NULL "
                    "AND json_extract(data,'$.streaming') IN (1, 'true')", (chat_id,)).fetchall():
                self._change_gateway_message(bot, json.loads(raw), 'edit', json.loads(raw)['text'], True)
            task = self._gateway_outside_task(bot, text)
            # A reply that Hermes streams arrives as a first piece and then edits. It pushes when the last edit ends it.
            message = self._message(bot['id'], text, task_id=task['id'], streaming=data.get('streaming') is True)
            message['_gateway_message_id'] = message_id
            self._put('messages', message)
            self._refresh_bot(bot['id'])
        LOG.info('The Hermes gateway sent a message to bot %s outside a Coconut turn (task %s).', chat_id, task['id'])
        return {'recorded': True, 'task_id': task['id']}

    def _gateway_profile(self, home: str) -> dict | None:
        """The active profile row of a Hermes home (a real path), or None for a home that is not a bot's profile."""
        return next((row for row in list((self.profiles.rows or {}).values())
                     if row.get('state') == 'active' and os.path.realpath(row['home']) == home), None)

    def _change_gateway_message(self, bot: dict, message: dict, kind: str, text: str, finalize: bool) -> dict:
        """Edit or delete a message outside a turn, and keep the bot's preview and unread count true. The caller holds
        the transaction."""
        if message.get('_hidden'):
            return {'recorded': False, 'reason': 'The chat no longer shows this message.'}
        old = message['text']
        finished = bool(message.get('streaming')) and finalize
        if kind == 'delete' or not text.strip():
            message.update(_hidden=True, streaming=False)
            bot['unread_count'] = max(0, bot['unread_count'] - 1)
            newest = self.db.execute(
                "SELECT data FROM messages WHERE json_extract(data,'$.bot_id')=? AND id<>? "
                "AND COALESCE(json_extract(data,'$._hidden'),0)=0 AND COALESCE(json_extract(data,'$._deleted'),0)=0 "
                "AND json_extract(data,'$.reaction_event') IS NULL ORDER BY rowid DESC LIMIT 1",
                (bot['id'], message['id'])).fetchone()
            if bot.get('last_message') == old[:160]:
                bot['last_message'] = json.loads(newest[0])['text'][:160] if newest else ''
        else:
            message['text'] = text
            if finished:
                message['streaming'] = False
            if bot.get('last_message') == old[:160]:
                bot['last_message'] = text[:160]
        self._put('bots', bot)
        self._put('messages', message)
        self._event('message.updated', dict(self._public(message), _hidden=bool(message.get('_hidden'))),
                    message['bot_id'], message['task_id'])
        if finished:
            self.push.record('message.completed', message, message['bot_id'], message['task_id'])
        self._refresh_bot(message['bot_id'])
        return {'recorded': True, 'task_id': message['task_id']}

    def _gateway_cron_shown(self, bot_id: str, job_id: str) -> bool:
        """Whether a recent routine task of the bot shows a cron run of this job. The caller holds the transaction."""
        from datetime import datetime, timedelta, timezone
        row = self.db.execute(
            "SELECT data FROM tasks WHERE json_extract(data,'$.bot_id')=? "
            "AND json_extract(data,'$._gateway_cron.job_id')=? ORDER BY rowid DESC LIMIT 1", (bot_id, job_id)).fetchone()
        if row is None:
            return False
        created = datetime.fromisoformat(json.loads(row[0])['created_at'].replace('Z', '+00:00'))
        return datetime.now(timezone.utc) - created < timedelta(seconds=CRON_DELIVERY_SECONDS)

    def _gateway_outside_task(self, bot: dict, text: str) -> dict:
        """The finished task in the bot's chat for a message outside a turn. The caller holds the transaction.

        The newest task of the bot takes the message when it holds a message outside a turn from a short time ago.
        Otherwise a new task starts. It has no request of the person and no run.
        """
        from .service import now, uid
        row = self.db.execute("SELECT data FROM tasks WHERE json_extract(data,'$.bot_id')=? ORDER BY rowid DESC LIMIT 1",
                              (bot['id'],)).fetchone()
        task = json.loads(row[0]) if row else None
        if (task is not None and task.get('_gateway_outside') and task['conversation_bot_id'] == bot['id']
                and time.time() - task['_gateway_outside'] < OUTSIDE_GROUP_SECONDS):
            task.update(_gateway_outside=time.time(), updated_at=now())
        else:
            task_id = uid()
            # The message pushes as a reply. A push for the completion would only repeat it.
            task = dict(id=task_id, thread_id=task_id, parent_task_id=None, bot_id=bot['id'],
                        conversation_bot_id=bot['id'], session_id=bot['session_id'], source_bot_id=None,
                        source_task_id=None, exchange_id=self._new_exchange(), exchange_depth=0,
                        title=' '.join(text.split())[:72], status='completed', summary='Complete.', created_at=now(),
                        updated_at=now(), scheduled_at=None, _attachment_ids=[], _gateway_outside=time.time(),
                        _silent_completion=True)
        self._put('tasks', task)
        self._event('task.updated', self._public(task), bot['id'], task['id'])
        return task

    def record_gateway_question(self, task: dict, payload: dict) -> None:
        """A question of a gateway turn becomes a decision card. The caller holds the transaction.

        The task stays working: the gateway turn waits for the answer and goes on after it.
        """
        from uuid import uuid4
        from .decision_inbox import decision_options
        from .service import now, digest, encoded
        approval = payload['question_kind'] == 'approval'
        choices = payload.get('choices') or []
        if choices:
            options = decision_options([dict(id=choice['id'], label=choice['label']) for choice in choices])
            for option, choice in zip(options, choices):
                if choice.get('deny'):
                    option['answer'] = 'reject'
        else:
            options = [dict(id='reply', label='Reply', answer='approve', explanation='Type the answer.',
                            risk_level=None, risk='')]
        action = {'type': 'gateway_' + payload['question_kind'], 'question': payload['text']}
        decision = dict(
            id=str(uuid4()), bot_id=task['bot_id'], task_id=task['id'], title=payload['text'][:160],
            explanation=(payload.get('detail') or '')[:2000],
            action_type='native_approval' if approval else 'question', resource='', consequences='',
            recommended='approve', options=options, recommended_option_id=None, status='pending', urgency='normal',
            needs_attention_now=False, risk_level=None, risk='', created_at=now(), decided_at=None, decided_by=None,
            decided_by_name=None, correction=None, outcome=None, revision=1, inbox_kind='decision',
            _action=action, _action_hash=digest(encoded(action)), _answers=[],
            _gateway_question=dict(turn_id=payload['turn_id'], question_id=payload['question_id'],
                                   kind=payload['question_kind']))
        decision.update(self.decision_origin(task))
        self._put('decisions', decision)
        self._message(task['bot_id'], decision['title'], task_id=task['id'], kind='decision', decision_id=decision['id'])
        self._event('decision.created', self._public(decision), task['bot_id'], task['id'])

    def answer_gateway_question(self, decision: dict, task: dict, data: dict, device: str) -> dict:
        """POST /v1/decisions/<id>/answer for a question of a gateway turn. The caller holds the transaction."""
        from .service import ServiceError, decision_correction, now
        if data.get('revision') != decision['revision']:
            raise ServiceError(409, 'This decision changed. Review it before answering.')
        option = next((item for item in decision['options'] if item['id'] == data.get('option_id')), None)
        correction = decision_correction(data).strip()
        if data.get('option_id') is not None and option is None:
            raise ServiceError(400, 'Choose a current option.')
        if option is None and not correction and data.get('choice') != 'reject':
            raise ServiceError(400, 'Enter a reply or choose an option.')
        question = decision['_gateway_question']
        if option is not None and option['id'] != 'reply':
            # A command approval takes the choice alone. A text there would be the reason of a denial.
            text, choice = ('' if question['kind'] == 'approval' else option['label']), option['id']
        else:
            text, choice = correction, ('deny' if data.get('choice') == 'reject' else '')
        if question['kind'] == 'approval' and not choice:
            # A typed reply to a command approval declines the command and tells Hermes why.
            choice = 'deny'
        rejected = choice == 'deny' or (option is not None and option['answer'] == 'reject') or data.get('choice') == 'reject'
        delivered = self.gateway_bridge.answer(question['turn_id'], dict(
            question_id=question['question_id'], text=text, choice=choice))
        decision.update(status='rejected' if rejected else 'approved' if option else 'replied',
                        selected_option_id=option['id'] if option else None,
                        selected_option_label=option['label'] if option else None, correction=correction or None,
                        decided_at=now(), decided_by=device, decided_by_name=self._actor_name(device),
                        outcome='Answer sent to Hermes.' if delivered else 'Hermes stopped waiting for this answer.')
        decision['_answers'].append(dict(choice=choice or 'reply', option_id=option['id'] if option else None,
                                          correction=correction or None, decided_by=device))
        self._put('decisions', decision)
        if correction:
            self._record_decision_correction(task, decision, correction)
            self._put('tasks', task)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        self._refresh_bot(task['bot_id'])
        return self._public(decision)

    def gateway_cron(self, data: dict) -> dict:
        """POST /v1/gateway/cron: a cron run of the gateway starts in a bot's profile. Open its Coconut turn.

        The run becomes a routine task in the bot's chat. Return {skip: true} for a profile that is not a bot.
        """
        from uuid import UUID, uuid5
        from .service import ServiceError, now, require_text, uid
        if data.get('event') != 'start':
            raise ServiceError(400, 'Send the start of a cron run.')
        home = os.path.realpath(require_text(data, 'home', 4096))
        job_id, execution_id = require_text(data, 'job_id', 200), require_text(data, 'execution_id', 200)
        name = str(data.get('job_name') or 'Routine').strip()[:200] or 'Routine'
        row = self._gateway_profile(home)
        if row is None:
            return {'skip': True}
        with self.transaction():
            previous = next((task for task in self._all('tasks')
                             if (task.get('_gateway_cron') or {}).get('execution_id') == execution_id), None)
            if previous is not None:
                turn = previous['_gateway_cron']['turn_id']
                if self.gateway_bridge.task_of(turn) == previous['id']:
                    return {'turn_id': turn, 'task_id': previous['id'], 'chat_id': previous['bot_id']}
                return {'skip': True}
            bot = self._get('bots', row['bot_id'])
            if bot['archived'] or bot.get('_deleted'):
                return {'skip': True}
            task = self._get('tasks', self._send(bot, {'text': name, 'mode': 'queue'}, record_message=False,
                                                 human_authored=False)['id'])
            turn = self.gateway_bridge.open_turn(task['id'], home, bot['id'])
            routine_id = str(uuid5(UUID(bot['id']), 'cron:' + job_id))
            # The gateway owns the native claim of this run, so Coconut has no native job to finish.
            task.update(status='working', summary='Working', title=name, routine_id=routine_id,
                        _native_job_finished=True, _turn_started_at=now(), _native_turn_id=uid(),
                        _gateway_cron=dict(job_id=job_id, execution_id=execution_id, turn_id=turn,
                                           session_id=str(data.get('session_id') or '')[:200]), updated_at=now())
            self._put('tasks', task)
            self._event('task.updated', self._public(task), bot['id'], task['id'])
            routine = self.db.execute('SELECT data FROM routines WHERE id=?', (routine_id,)).fetchone()
            if routine is not None:
                routine = json.loads(routine[0])
                routine['last_task_id'] = task['id']
                self._put('routines', routine)
            self._event('routine.triggered', {'routine_id': routine_id, 'task_id': task['id']}, bot['id'], task['id'])
            self._refresh_bot(bot['id'])
            # An update waits for these turns as it waits for worker turns (service_restart._service_busy). The
            # thread starts before the transaction ends, so no check sees the working task without a live thread.
            # If it cannot start, the transaction rolls back and the turn closes.
            thread = threading.Thread(target=self._run_gateway_cron, args=(task['id'], turn),
                                      name='coconut-gateway-cron', daemon=True)
            self.gateway_cron_threads = {key: value for key, value in self.gateway_cron_threads.items()
                                         if value.is_alive()}
            try:
                thread.start()
            except BaseException:
                self.gateway_bridge.cancel(task['id'])
                raise
            self.gateway_cron_threads[task['id']] = thread
        return {'turn_id': turn, 'task_id': task['id'], 'chat_id': bot['id']}

    def _run_gateway_cron(self, task_id: str, turn: str) -> None:
        """Translate the events of a cron run into its routine task, as _run_task does for a chat turn."""
        try:
            self.gateway_bridge.follow(turn, lambda kind, payload: self.emit(task_id, kind, payload))
        except Exception as error:
            if not self.stopping.is_set():
                with self.lock:
                    working = self._get('tasks', task_id)['status'] == 'working'
                if working:
                    try:
                        self.emit(task_id, 'failed', {'summary': f'The routine stopped: {str(error)[:200]}'})
                    except Exception:
                        LOG.exception('The routine task %s did not record its failure.', task_id)
        finally:
            self._close_gateway_questions(task_id)
            if not self.stopping.is_set():
                with self.transaction():
                    task = self._get('tasks', task_id)
                    for message in self._task_messages(task_id):
                        if message['task_id'] == task_id and message['streaming']:
                            message['streaming'] = False
                            self._put('messages', message)
                            self._event('message.updated', message, message['bot_id'], task_id)
                            self.push.record('message.completed', message, message['bot_id'], task_id)
                    self._refresh_bot(task['bot_id'])

    def _close_gateway_questions(self, task_id: str) -> None:
        """Close the open questions of a gateway turn that ended. Hermes no longer waits for their answers."""
        with self.transaction():
            for decision in self._all('decisions'):
                if (decision['task_id'] == task_id and decision.get('_gateway_question')
                        and decision['status'] == 'pending'):
                    decision.update(status='failed', outcome='Hermes stopped waiting for this answer.')
                    self._put('decisions', decision)
                    self._event('decision.updated', self._public(decision), decision['bot_id'], task_id)

    def _start_gateway_supervisor(self):
        # Only a service on the person's Hermes runs a gateway. Fixture mode and tests have no Hermes home.
        if self.gateway_supervisor is not None or not self.gateway_enabled():
            return
        self.gateway_supervisor = threading.Thread(target=self._supervise_gateway, name='coconut-gateway', daemon=True)
        self.gateway_supervisor.start()

    def _supervise_gateway(self):
        while not self.stopping.is_set():
            try:
                self._check_gateway()
            except Exception:
                LOG.exception('The check of the Hermes gateway failed.')
            self.gateway_wake.wait(CHECK_SECONDS)
            self.gateway_wake.clear()

    def _close_gateway_chat(self):
        self.gateway_bridge.close()
        self.gateway_wake.set()
        if self.gateway_supervisor is not None:
            self.gateway_supervisor.join(timeout=CHECK_SECONDS + 5)
        self._stop_gateway()

    def _check_gateway(self):
        home, process = self.hermes_home, self.gateway_process
        if process is not None and process.poll() is None:
            self.gateway_state = {'state': 'running', 'pid': process.pid, 'reason': None}
            return
        if process is not None:
            self.gateway_process, self._gateway_exit = None, process.returncode
            self._gateway_retry_at = time.monotonic() + RESTART_SECONDS
            LOG.warning('The Hermes gateway stopped with exit code %s.', process.returncode)
        pid = live_gateway_pid(home)
        if pid is not None:
            reason = (USING if self.gateway_bridge.connected(home) else NOT_LOADED).format(home=home, pid=pid)
            self.gateway_state = {'state': 'external', 'pid': pid, 'reason': reason}
            return
        if time.monotonic() < self._gateway_retry_at:
            self.gateway_state = {'state': 'stopped', 'pid': None, 'reason': STOPPED.format(
                code=self._gateway_exit, log=self.gateway_bridge.folder / 'gateway.log')}
            return
        entries = getattr(self, 'hermes_entries', None)
        if entries is not None and entries.is_alive():
            # The gateway loads the plugin and the platform settings at its start, so they come first. The next
            # check starts it.
            self.gateway_state = {'state': 'starting', 'pid': None,
                                  'reason': 'Coconut sets its entries in the Hermes profiles first.'}
            return
        if self.stopping.is_set():
            return
        self._start_gateway(home)

    def _start_gateway(self, home):
        folder = self.gateway_bridge.folder
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = folder / 'gateway.log'
        if log.exists() and log.stat().st_size > LOG_LIMIT_BYTES:
            os.replace(log, folder / 'gateway.log.1')
        environment = {key: os.environ[key] for key in ENVIRONMENT_KEYS if key in os.environ}
        environment.update(HERMES_HOME=str(home), HERMES_DISABLE_TELEMETRY='1', PYTHONUNBUFFERED='1')
        with open(log, 'ab') as output:
            os.chmod(log, 0o600)
            self.gateway_process = subprocess.Popen(
                [str(self.provider.python), '-c', LAUNCH, str(self.provider.hermes_root), 'gateway', 'run'],
                cwd=folder, env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True)
        LOG.info('Started the Hermes gateway for %s (process %s).', home, self.gateway_process.pid)
        self.gateway_state = {'state': 'running', 'pid': self.gateway_process.pid, 'reason': None}

    def _stop_gateway(self):
        process, self.gateway_process = self.gateway_process, None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=STOP_SECONDS)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
