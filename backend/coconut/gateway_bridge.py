"""Plain chat turns through the Hermes gateway: the service side of the `coconut` gateway platform.

With the chat runtime `gateway` (gateway_chat), the service gives a plain chat turn to the `coconut` adapter of the
person's Hermes gateway (hermes_plugin/coconut) instead of to its own worker. The adapter calls two loopback routes on
the main listener, with the token of `<data>/gateway/bridge.json`:

- `POST /v1/gateway/poll` waits for turns of one Hermes home (one profile, one bot), and for turns to stop. The request
  lists the turns that still run in the gateway. An adapter of the team chat whose Hermes lacks a part of the chat
  names it (`lacking`) and gets no turn.
- `POST /v1/gateway/events` reports what the gateway does with a turn: `typing`, `send`, `edit`, `delete`, `question`
  and `done`.
- `POST /v1/gateway/tools` runs a Coconut tool of the `coconut` toolset for the task of a running turn (gateway_chat).
- `POST /v1/gateway/wake` opens a turn for a run that Hermes starts in a bot's chat by itself, for example after a
  background process (gateway_chat.gateway_hermes_run).
- `POST /v1/gateway/cron` opens a turn for a cron run of the gateway (gateway_chat.gateway_cron). The plugin reports
  its events like those of a chat turn.
- `POST /v1/gateway/messages` takes a send, an edit or a delete of the gateway outside a Coconut turn
  (gateway_chat.gateway_message).
- `POST /v1/gateway/files` keeps a photo that a bot saved on the Mac as an attachment of the bot
  (gateway_chat.gateway_file).

A poll also returns the answers to the questions of the adapter's turns. A question is a decision card on the phone.

The service thread of the turn translates these events into the provider events of the worker path (TurnEvents), so
the phone sees the same messages. A turn that no adapter takes in TAKE_SECONDS goes back to the service, which
waits for an adapter (gateway_chat.run_gateway_chat).
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import uuid

from .silent_responses import is_silence_reply

TAKE_SECONDS = 30
# The adapter polls at most POLL_SECONDS apart and lists its running turns in each poll.
POLL_SECONDS = 25
SILENT_SECONDS = 90
# A cron run reports no heartbeat between its events, and a tool can run for long. Hermes stops a run that is idle for
# HERMES_CRON_TIMEOUT (600 s by default), and the plugin then reports its end.
CRON_SILENT_SECONDS = 3 * 3600
ADAPTER_SECONDS = 60
# A turn of the team chat counts as taken only when the adapter that got it shows that it has it: an event of the
# turn, or a later poll that lists it in `active`. A living adapter does both at once: it lists a turn that it took in
# its next poll, which it sends as soon as it gave the turns to Hermes. A gateway that stops can still hold a poll for
# some seconds and take a turn that it drops. So a poll of the same home that starts this long after the hand-out and
# does not list the turn takes it again, and a turn that nobody shows for TAKE_SECONDS counts as not taken.
HANDOFF_SECONDS = 5
# What the Hermes of an adapter of the team chat lacks for the chat (hermes_plugin/coconut/team.py, `lacking` of a
# poll): field names of Hermes, separated by ", ". `coconut status` shows the text, so the bridge takes nothing else.
LACKING = re.compile(r'[a-z_]+(, [a-z_]+)*')
MAX_LACKING = 200
MAX_TEXT = 200_000
# `delta` stays known: a gateway that still runs the plugin of an earlier version sends partial text for each turn.
# The bridge drops it (event), so the turn goes on with its complete sends. Remove `delta` when no install has that plugin.
KINDS = ('typing', 'send', 'edit', 'delete', 'question', 'done', 'delta')
QUESTION_KINDS = ('clarify', 'approval')

# The chat of a turn (gateway_chat.run_gateway_chat). A turn of the bot's conversation runs in the bot's chat
# (plain_chat_turn). A task with its own work session runs in the chat of that session (gateway_work_turn). A turn of the
# bot's conversation that owns a native outcome runs in the bot's chat with the Kanban claim of its outcome
# (conversation_outcome_turn). A status question has its own chat (gateway_chat.run_gateway_status). Attachments,
# reactions, pending cards, the saved task list of an assignment, its dependencies, a recovery context, a direct reply,
# the bot exchange, GitHub source activity, a PR review, the receipts of earlier turns and the bot changes of a chat go
# to the gateway as text of the turn (gateway_chat.gateway_turn_text, work_notes).
#
# The inputs of a work session: the dispatchers give it the work session and the Kanban claim. A question about a Work
# item carries that item (`_work_context_id`, `_work_context`). An audit of a work source carries its text and the steps
# for an answer to its source (`_source_audit_prompt`, `_source_answer_steps`). A native outcome (`_native_outcome_id`)
# acts on its card with the board tools of its claim (gateway_chat.gateway_kanban_tool).
WORK_SESSION_INPUTS = ('_execution_session_id', '_kanban_claim', '_work_context_id', '_work_context',
                       '_source_audit_prompt', '_source_answer_steps', '_native_outcome_id')
# A task with one of these takes no chat turn: a routine run that an earlier version of Coconut claimed (`_native_job`),
# which no gateway runs (gateway_chat.gateway_unsupported), and a status question (run_gateway_status).
OTHER_PATHS = ('_native_job', '_work_status_only')

# A message with one of these fields carries a Work item. It goes with a turn that has that item. A quote, a reaction
# and a bot mention go into the text of the gateway message (native_conversation.message_content).
WORK_MESSAGE_FIELDS = ('work_context',)


def gateway_work_turn(task: dict) -> bool:
    """Whether a task with its own work session runs in the gateway, in a separate chat (gateway_chat.work_chat).

    Such a task is not a turn of the bot's conversation (`_chat_session`), for example a scheduled message, a question
    about a Work item or the audit of a work source. A native outcome (and a question about its Work item) runs here
    too, with the board tools of its claim (gateway_chat.gateway_kanban_tool), and so does its technical lane, with the
    holds of the lane (gateway_chat.LANE_EVENTS).
    """
    return (bool(task.get('_execution_session_id')) and not task.get('_chat_session')
            and not any(task.get(key) for key in OTHER_PATHS))


def conversation_outcome_turn(task: dict) -> bool:
    """Whether a turn of the bot's conversation (`_chat_session`) that owns a native outcome runs in the gateway.

    Such a turn starts from the Kanban claim of its outcome (work_capacity.chat_turn). It has no work session, so it
    runs in the bot's chat. It acts on its card with the board tools of its claim (gateway_chat.gateway_kanban_tool),
    but it has no worktree (gateway_chat.gateway_workspace).
    """
    return (bool(task.get('_chat_session')) and bool(task.get('_native_outcome_id'))
            and not any(task.get(key) for key in OTHER_PATHS))


def gateway_claim_turn(task: dict) -> bool:
    """Whether the gateway runs a task that a Kanban claim starts: a task of a work session (gateway_work_turn) or a
    turn of the bot's conversation that owns a native outcome (conversation_outcome_turn)."""
    return gateway_work_turn(task) or conversation_outcome_turn(task)


def plain_chat_turn(task: dict) -> bool:
    """Whether a turn runs in the bot's chat: a turn of the bot's conversation (`_chat_session`) without the inputs of a
    work session.

    A message of another bot, the return of a delegated task and the continuation of an accepted proposal are turns of
    the bot's conversation too (gateway_chat.join_conversation).
    """
    return bool(task.get('_chat_session')) and not any(task.get(key) for key in OTHER_PATHS + WORK_SESSION_INPUTS)


# The commands of Hermes that the person may type in a Coconut chat (gateway_chat.gateway_command). Hermes asks to
# confirm /new, and the person answers with /approve, /always or /cancel, as Hermes's text says.
COMMANDS = ('/compress', '/new', '/approve', '/always', '/cancel')


class GatewayUnavailable(RuntimeError):
    """No adapter took the turn. Nothing ran, so the turn can wait for another adapter."""


class GatewayBusy(RuntimeError):
    """The gateway did not run the turn, because it already ran another turn in the session of the chat, for example
    its own turn for a finished background delegation (hooks.join_conversation). Nothing of the turn ran, so it can
    wait until the session is free."""


class _Turn:
    def __init__(self, task_id, home, chat_id, text, chat_name, session_root=None):
        self.id = str(uuid.uuid4())
        self.task_id, self.home, self.chat_id = task_id, home, chat_id
        self.request = {'id': self.id, 'chat_id': chat_id, 'chat_name': chat_name, 'text': text}
        if session_root:
            # The adapter joins the gateway session of the chat with this native conversation (hooks.py).
            self.request['session_root'] = session_root
        self.state = 'pending'  # pending, active, done, cancelled, stopping
        self.created = self.seen = time.monotonic()
        self.events: list[dict] = []
        # The waits that the turn recorded and the number of its wait calls (gateway_chat.gateway_wait).
        self.waits = {'recorded': set(), 'calls': 0}
        self.silent_seconds = SILENT_SECONDS
        # A cron run of the gateway (open_turn). The adapter cannot stop it: it runs in a cron session, not in the chat.
        self.cron = False
        # A turn that may end in silence, such as a reaction or bot traffic, and whether it shows commentary
        # (TurnEvents).
        self.silent, self.commentary = False, True
        # The context (a follow-up on a pending card) that this turn answers, or None (gateway_chat.run_gateway_context).
        self.context_id = None
        # The monotonic time at which a turn of the team chat stops (team_turns.TURN_SECONDS), or None.
        self.deadline = None
        # A turn of the team chat (team_turns.py), the time of its last hand-out to a poll, and whether the adapter
        # showed that it has it (HANDOFF_SECONDS).
        self.team, self.taken, self.shown = False, 0.0, False


class GatewayBridge:
    def __init__(self, root):
        self.folder = Path(root) / 'gateway'
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Condition()
        self.adapters: dict[str, float] = {}
        # What the Hermes of each home lacks for the team chat, while the polls of its adapter name it (LACKING).
        self.lacking: dict[str, str] = {}
        self.turns: dict[str, _Turn] = {}
        # Answers to questions, for the next poll of each Hermes home.
        self.answers: dict[str, list[dict]] = {}
        self.closed = False

    @property
    def address_file(self) -> Path:
        return self.folder / 'bridge.json'

    def publish(self, url: str) -> None:
        """Write the address of the main listener and this process's token for the adapter. Mode 0600."""
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.folder, 0o700)
        with tempfile.NamedTemporaryFile('w', dir=self.folder, prefix='.bridge-', delete=False) as handle:
            json.dump({'url': url, 'token': self.token}, handle)
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, self.address_file)

    def authorized(self, header: str) -> bool:
        return hmac.compare_digest(str(header or '').encode(), ('Bearer ' + self.token).encode())

    def connected(self, home) -> bool:
        with self.lock:
            last = self.adapters.get(os.path.realpath(home))
        return last is not None and time.monotonic() - last < ADAPTER_SECONDS

    def connected_homes(self) -> list[str]:
        with self.lock:
            return sorted(home for home, last in self.adapters.items() if time.monotonic() - last < ADAPTER_SECONDS)

    def lacking_of(self, home) -> str | None:
        """What the Hermes of a connected adapter of `home` lacks for the team chat, or None."""
        home = os.path.realpath(home)
        with self.lock:
            return self.lacking.get(home) if self.connected(home) else None

    def poll(self, data: dict) -> dict:
        home = data.get('home')
        if not isinstance(home, str) or not home:
            raise ValueError('Name the Hermes home of the adapter.')
        home = os.path.realpath(home)
        lacking = data.get('lacking')
        if lacking is not None and not (isinstance(lacking, str) and len(lacking) <= MAX_LACKING
                                        and LACKING.fullmatch(lacking)):
            raise ValueError('Name what Hermes lacks as a short list of field names.')
        active = {value for value in data.get('active') or [] if isinstance(value, str)}
        wait = max(0.0, min(float(data.get('wait', POLL_SECONDS)), POLL_SECONDS))
        started = time.monotonic()
        deadline = started + wait
        with self.lock:
            if lacking:
                self.lacking[home] = lacking
            else:
                self.lacking.pop(home, None)
            while True:
                current = time.monotonic()
                self.adapters[home] = current
                mine = [turn for turn in self.turns.values() if turn.home == home]
                for turn in mine:
                    if turn.id in active:
                        turn.seen, turn.shown = current, True
                    elif (turn.team and turn.state == 'active' and not turn.shown
                          and turn.taken + HANDOFF_SECONDS < started):
                        # The adapter that got this turn did not show it, and an adapter of the home polls without
                        # it: nothing of the turn ran, so this poll takes it again.
                        turn.state, turn.created = 'pending', current
                # A Hermes that lacks a part of the team chat gets no turn (team_turns.py posts the notice).
                offered = [turn for turn in mine if turn.state == 'pending' and not lacking]
                stopping = [turn for turn in mine if turn.state == 'stopping']
                if offered or stopping or self.answers.get(home) or self.closed or current >= deadline:
                    break
                self.lock.wait(deadline - current)
            for turn in offered:
                turn.state, turn.seen = 'active', time.monotonic()
                turn.taken, turn.shown = turn.seen, False
            for turn in stopping:
                self.turns.pop(turn.id, None)
            answers = self.answers.pop(home, [])
            self.lock.notify_all()
            # A turn that the adapter lists and that this process does not know, for example after a restart of the
            # service: the adapter of the team mode stops it (hermes_plugin/coconut/team.py). Another adapter ignores it.
            unknown = sorted(active - set(self.turns))
        result = {'turns': [turn.request for turn in offered], 'stop': [turn.chat_id for turn in stopping]}
        if answers:
            result['answers'] = answers
        if unknown:
            result['unknown'] = unknown
        return result

    def event(self, data: dict) -> dict:
        kind, text, message_id = data.get('kind'), data.get('text'), data.get('message_id')
        if kind not in KINDS or (text is not None and not isinstance(text, str)) or len(text or '') > MAX_TEXT:
            raise ValueError('Send a known gateway event.')
        if kind in ('send', 'edit', 'delete') and not isinstance(message_id, str):
            raise ValueError('Name the message of this event.')
        if kind == 'question':
            question_choices(data)
        with self.lock:
            turn = self.turns.get(data.get('turn_id'))
            if turn is None or turn.state != 'active':
                # A cancelled or finished turn. The gateway may still send its last messages.
                return {'ok': False}
            turn.seen, turn.shown = time.monotonic(), True
            if kind == 'delta':
                return {'ok': True}
            turn.events.append({key: data[key] for key in ('kind', 'text', 'message_id', 'interim', 'outcome',
                                                           'question_id', 'question_kind', 'detail', 'choices')
                                if key in data})
            if kind == 'done':
                turn.state = 'done'
            self.lock.notify_all()
        return {'ok': True}

    def task_of(self, turn_id) -> str | None:
        """The task of a running turn, for a tool of that turn."""
        with self.lock:
            turn = self.turns.get(turn_id)
            return turn.task_id if turn is not None and turn.state == 'active' else None

    def waits_of(self, turn_id) -> dict | None:
        """The wait record of a running turn (gateway_chat.gateway_wait), or None."""
        with self.lock:
            turn = self.turns.get(turn_id)
            return turn.waits if turn is not None and turn.state == 'active' else None

    def context_of(self, turn_id) -> str | None:
        """The context that a running turn answers, or None for a turn of its task."""
        with self.lock:
            turn = self.turns.get(turn_id)
            return turn.context_id if turn is not None and turn.state == 'active' else None

    def answer(self, turn_id: str, answer: dict) -> bool:
        """Give the answer to a question to the adapter of its turn, with its next poll."""
        with self.lock:
            turn = self.turns.get(turn_id)
            if turn is None or turn.state != 'active':
                return False
            self.answers.setdefault(turn.home, []).append(answer)
            self.lock.notify_all()
        return True

    def open_turn(self, task_id: str, home, chat_id: str, *, cron: bool = True) -> str:
        """Register a turn that the gateway already runs, for example a cron run. Return its ID for follow().

        A cron run cannot be stopped from a chat, so a cancel drops its turn. A run in a bot's chat (cron False, for
        example after a background process) stops like a chat turn: the adapter gets the stop of the chat."""
        turn = _Turn(task_id, os.path.realpath(home), chat_id, '', 'Coconut')
        turn.state, turn.silent_seconds, turn.cron = 'active', CRON_SILENT_SECONDS, cron
        with self.lock:
            self.turns[turn.id] = turn
        return turn.id

    def run_turn(self, task_id: str, home, chat_id: str, text: str, emit, **options) -> None:
        """Give the turn to the adapter of `home` and translate its events with `emit` until the turn ends. See
        submit_turn for the options."""
        self.follow(self.submit_turn(task_id, home, chat_id, text, **options), emit)

    def submit_turn(self, task_id: str, home, chat_id: str, text: str, *, chat_name: str = 'Coconut',
                 session_root: str | None = None, silent: bool = False, commentary: bool = True,
                 context_id: str | None = None, mode: str | None = None, own: str | None = None,
                 folder: str | None = None, command: str | None = None, records: list | None = None,
                 team: dict | None = None, deadline: float | None = None) -> str:
        """Register a turn for the adapter of `home`, and return its ID for follow(). From then on, cancel() of its
        task stops it, also before an adapter takes it.

        With `silent`, the turn may end in silence. Without `commentary`, it shows no commentary (TurnEvents). With
        `context_id`, the turn answers a follow-up on a pending card: the plugin blocks the card tools in it. With `own`, the turn's own text, Hermes saves only `own`
        in the session, and the plugin gives the whole `text` to the model calls of this turn (hooks.add_turn_context).
        With `folder`, the terminal commands of the turn run in that folder, as in a worker of a native outcome
        (hooks.guard_tool). With `command`, a command of Hermes that the person typed
        (gateway_chat.gateway_command), the adapter gives the gateway that command instead of a message. `records` are the
        records that the session of the chat keeps once (native_conversation.turn_records, hooks.add_records).
        `team` makes a turn of the team chat of a team-only Mac (team_turns.py): the sender, the context and the rules
        of the chat, which the adapter of the team mode reads. It has no task and no session root. `deadline` stops
        the turn at that monotonic time."""
        turn = _Turn(task_id, os.path.realpath(home), chat_id, text, chat_name, session_root)
        if own is not None:
            turn.request['own'] = own
        if folder:
            turn.request['folder'] = folder
        if command:
            turn.request['command'] = command
        if records:
            turn.request['records'] = records
        if team is not None:
            turn.request['team'] = team
            turn.team = True
        turn.deadline = deadline
        turn.silent, turn.commentary = silent, commentary
        if context_id:
            turn.context_id = context_id
            # `refresh` for an automatic check of the card: the plugin then allows only its check tool of Coconut.
            turn.request['mode'] = mode or 'context'
        elif mode:
            # For example `status`: the plugin blocks every tool call of the turn (hooks.guard_tool).
            turn.request['mode'] = mode
        if silent:
            # The adapter marks the message as one that needs no reply (MessageEvent.reply_expected).
            turn.request['silent'] = True
        with self.lock:
            # A stop that no adapter collected is old news after this long.
            for old in [old for old in self.turns.values()
                        if old.state == 'stopping' and time.monotonic() - old.seen > SILENT_SECONDS]:
                del self.turns[old.id]
            self.turns[turn.id] = turn
            self.lock.notify_all()
        return turn.id

    def follow(self, turn_id: str, emit) -> None:
        """Translate the events of a registered turn with `emit` until the turn ends."""
        with self.lock:
            turn = self.turns[turn_id]
        events = TurnEvents(emit, turn.id, silent=turn.silent, commentary=turn.commentary)
        try:
            while True:
                with self.lock:
                    while not turn.events and turn.state in ('pending', 'active') and not self.closed:
                        current = time.monotonic()
                        if turn.deadline is not None and current > turn.deadline:
                            raise TimeoutError('The turn ran past its time.')
                        # An adapter whose Hermes lacks a part of the team chat never takes the turn (poll).
                        if turn.state == 'pending' and (current - turn.created > TAKE_SECONDS
                                                        or turn.team and self.lacking.get(turn.home)):
                            raise GatewayUnavailable('No Hermes gateway took this turn.')
                        if turn.state == 'active' and current - turn.seen > turn.silent_seconds:
                            raise RuntimeError('The Hermes gateway stopped answering during this turn.')
                        if turn.team and turn.state == 'active' and not turn.shown and current - turn.taken > TAKE_SECONDS:
                            # A living adapter shows a turn at once. One that never does dropped it, so nothing ran.
                            raise GatewayUnavailable('No Hermes gateway kept this turn.')
                        self.lock.wait(1)
                    if turn.state in ('cancelled', 'stopping'):
                        return
                    if self.closed and not turn.events:
                        raise RuntimeError('The Coconut service stops.')
                    if turn.deadline is not None and time.monotonic() > turn.deadline:
                        # A turn that sends without a pause also stops at its time.
                        raise TimeoutError('The turn ran past its time.')
                    batch, turn.events = turn.events, []
                for event in batch:
                    if events.apply(event):
                        return
        except BaseException:
            with self.lock:
                if turn.state == 'active':
                    # The gateway may still run the turn. The next poll tells the adapter to stop it.
                    turn.state = 'cancelled' if turn.cron else 'stopping'
                    self.lock.notify_all()
            raise
        finally:
            with self.lock:
                if turn.state != 'stopping':
                    self.turns.pop(turn.id, None)

    def cancel(self, task_id: str) -> bool:
        """Stop the turns of a task. Return whether a registered turn stops."""
        stopped = False
        with self.lock:
            for turn in list(self.turns.values()):
                if turn.task_id == task_id and turn.state in ('pending', 'active'):
                    # A stop goes to the chat of the turn, so it must not name the chat for a cron run.
                    turn.state = 'stopping' if turn.state == 'active' and not turn.cron else 'cancelled'
                    stopped = True
            self.lock.notify_all()
        return stopped

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.lock.notify_all()


def question_choices(data: dict) -> list[dict]:
    """Check a question event and return its choices: up to eight, each with an ID and a label."""
    choices = data.get('choices') or []
    if (data.get('question_kind') not in QUESTION_KINDS or not isinstance(data.get('question_id'), str)
            or not isinstance(data.get('text'), str) or not data['text'].strip()
            or not isinstance(data.get('detail', ''), str) or not isinstance(choices, list) or len(choices) > 8
            or any(not isinstance(choice, dict) or not isinstance(choice.get('id'), str)
                   or not isinstance(choice.get('label'), str) or not choice['label'].strip() for choice in choices)
            or len({choice['id'] for choice in choices}) != len(choices)):
        raise ValueError('Send a question with a text and at most eight distinct choices.')
    return choices


class TurnEvents:
    """Translate the gateway events of one turn into provider events, as the worker sends them.

    The phone shows each message of the bot only when it is complete. The plugin sends no partial text, and Hermes
    streams none to the platform (hermes_plugin/coconut/adapter.py, SUPPORTS_MESSAGE_EDITING).
    The gateway sends a reply in segments: each `send` starts one with its complete text, and `edit` replaces that text.
    The newest segment is the reply of the turn. A new segment keeps the earlier one as a complete interim message. A
    send that the gateway marks as interim, such as commentary, is a complete interim message at once.

    A `silent` turn, such as a reaction or bot traffic, may end in silence: when Hermes sends no reply, or a reply that is
    a silence marker, the turn ends without a message, as the worker ends it (silent_responses.is_silence_reply). A turn
    without `commentary`, such as a reaction, shows no commentary.
    """

    def __init__(self, emit, turn_id=None, silent=False, commentary=True):
        self.emit = emit
        self.turn_id = turn_id
        self.silent, self.commentary = silent, commentary
        # The message ID and the text of the newest segment, and whether the gateway sent a reply.
        self.segment = None
        self.text = ''
        self.phase = None
        self.sent = False

    def apply(self, event: dict) -> bool:
        """Apply one event. Return True at the end of the turn."""
        kind, text = event['kind'], event.get('text') or ''
        if kind == 'typing':
            self._phase('thinking')
        elif kind == 'question':
            self.emit('gateway_question', {key: event.get(key) for key in (
                'question_id', 'question_kind', 'text', 'detail', 'choices')} | {'turn_id': self.turn_id})
        elif kind == 'send' and text.strip():
            self._phase('writing')
            if event.get('interim') and not self.commentary:
                # A reaction turn shows no commentary, as in the worker.
                return False
            if event.get('interim'):
                self._seal()
                self.emit('interim_message', {'text': text})
            elif self.segment is not None and text == self.text:
                # A send that repeats the newest segment, for example the final send after an edit. It adds no message.
                self.segment, self.sent = event['message_id'], True
            else:
                self._seal()
                self.emit('text_delta', {'text': text})
                self.segment, self.text, self.sent = event['message_id'], text, True
        elif kind == 'edit' and event['message_id'] == self.segment and text != self.text:
            self.emit('text_delta', {'text': text, 'replace': True})
            self.text = text
        elif kind == 'delete' and event['message_id'] == self.segment:
            self.emit('text_delta', {'text': '', 'replace': True})
            self.segment, self.text = None, ''
        elif kind == 'done' and event.get('outcome') == 'busy':
            if self.segment is not None:
                # Text of the other turn that reached this turn before the gateway skipped it.
                self.emit('text_delta', {'text': '', 'replace': True})
            raise GatewayBusy('The Hermes gateway runs another turn in the session of this chat.')
        elif kind == 'done':
            # Hermes sends no reply when the model chose silence, and drops a reply that is only the marker.
            if event.get('outcome') == 'success' and self.silent and (not self.sent or is_silence_reply(self.text)):
                if self.segment is not None:
                    # Take back a reply that is a silence marker.
                    self.emit('text_delta', {'text': '', 'replace': True})
                self.emit('completed', {'summary': 'Complete.', 'silent_response': True, 'reaction_silent': True})
            elif event.get('outcome') == 'success':
                self.emit('completed', {'summary': 'Complete.'})
            elif event.get('outcome') == 'cancelled':
                self.emit('failed', {'summary': 'Hermes stopped this turn.'})
            else:
                self.emit('failed', {'summary': 'The Hermes gateway could not finish this turn.'})
            return True
        return False

    def _seal(self):
        if self.segment is not None:
            self.emit('interim_seal', {})
            self.segment, self.text = None, ''

    def _phase(self, phase):
        if self.phase != phase:
            self.phase = phase
            # A phase only labels progress, as in the worker path.
            try:
                self.emit('phase', {'phase': phase})
            except Exception:
                pass
