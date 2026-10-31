"""Plain chat turns through the Hermes gateway: the service side of the `coconut` gateway platform.

With the chat runtime `gateway` (gateway_chat), the service gives a plain chat turn to the `coconut` adapter of the
person's Hermes gateway (hermes_plugin/coconut) instead of to its own worker. The adapter calls two loopback routes on
the main listener, with the token of `<data>/gateway/bridge.json`:

- `POST /v1/gateway/poll` waits for turns of one Hermes home (one profile, one bot), and for turns to stop. The request
  lists the turns that still run in the gateway.
- `POST /v1/gateway/events` reports what the gateway does with a turn: `typing`, `delta`, `send`, `edit`, `delete`,
  `question` and `done`.
- `POST /v1/gateway/tools` runs a Coconut tool of the `coconut` toolset for the task of a running turn (gateway_chat).
- `POST /v1/gateway/cron` opens a turn for a cron run of the gateway (gateway_chat.gateway_cron). The plugin reports
  its events like those of a chat turn.
- `POST /v1/gateway/messages` takes a send, an edit or a delete of the gateway outside a Coconut turn
  (gateway_chat.gateway_message).

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
MAX_TEXT = 200_000
KINDS = ('typing', 'delta', 'send', 'edit', 'delete', 'question', 'done')
QUESTION_KINDS = ('clarify', 'approval')

# A turn with one of these inputs needs a Coconut tool or context that the gateway does not have yet: a work item with
# its context and workspace, native outcomes, routines, the audit of a work source, the results of a worker's
# background process, the receipts of a worker's tool calls and the technical lane of a native outcome. The worker runs
# it. An audit uses the Kanban tools of Coconut's own board (work_sources), which the gateway does not have. Attachments, reactions, pending cards, the saved task list of an assignment, its dependencies, a recovery
# context, a direct reply, the bot exchange, GitHub source activity, a PR review and the bot changes of a chat go to
# the gateway as text of the turn (gateway_chat.gateway_turn_text). The plugin has the card tools, coconut_wait,
# coconut_correct_withdrawal, coconut_message_bot, coconut_create_bot and coconut_delete_bot.
WORKER_INPUTS = (
    '_native_job', '_kanban_claim', '_native_outcome_id', '_execution_session_id',
    '_work_context_id', '_work_context', '_background_completion', '_source_answer_steps', '_source_audit_prompt',
    '_technical_lane', '_native_receipts', '_work_status_only')
# A task with its own work session runs in the gateway, in a chat of its own (gateway_work_turn), when it has no other
# worker input. The dispatchers give it the work session and the Kanban claim. A question about a Work item without a
# native outcome carries that item (`_work_context_id`, `_work_context`). An audit of a work source carries its text and
# the steps for an answer to its source (`_source_audit_prompt`, `_source_answer_steps`).
WORK_SESSION_INPUTS = ('_execution_session_id', '_kanban_claim', '_work_context_id', '_work_context',
                       '_source_audit_prompt', '_source_answer_steps')


# A message with one of these fields carries Coconut context that a gateway message does not: a work item. A quote, a
# reaction and a bot mention go into the text of the gateway message (native_conversation.message_content).
WORKER_MESSAGE_FIELDS = ('work_context',)


# The receipts of the board tools that the gateway runs on Coconut's board (gateway_chat.gateway_kanban_tool). The
# gateway continues a turn with them, so they are no worker input. A receipt of another tool is one.
GATEWAY_RECEIPTS = ('kanban_create', 'kanban_comment')


def worker_input(task: dict, key: str) -> bool:
    """Whether the task has the worker input `key`. The receipts of the turn count only when a worker made them."""
    if key == '_native_receipts' and isinstance(task.get(key), list):
        return any(not isinstance(receipt, dict) or (receipt.get('action') or {}).get('name') not in GATEWAY_RECEIPTS
                   for receipt in task[key])
    return bool(task.get(key))


def gateway_work_turn(task: dict) -> bool:
    """Whether a task with its own work session runs in the gateway, in a separate chat (gateway_chat.work_chat).

    Such a task is not a turn of the bot's conversation (`_chat_session`) and has no worker input but its work session,
    its Kanban claim, its Work item and its audit text, for example a scheduled message, a question about a Work item
    or the audit of a work source. A native outcome (and a question about its Work item) stays with the worker. A
    status question runs in the gateway on its own path (gateway_chat.run_gateway_status).
    """
    return (bool(task.get('_execution_session_id')) and not task.get('_chat_session')
            and not any(worker_input(task, key) for key in WORKER_INPUTS if key not in WORK_SESSION_INPUTS))


def plain_chat_turn(task: dict) -> bool:
    """Whether a turn can run in the gateway: a turn of the bot's conversation (`_chat_session`) without a worker input.

    With the gateway, a message of another bot, the return of a delegated task and the continuation of an accepted
    proposal are turns of the bot's conversation too (gateway_chat.join_conversation). A task with its own work session
    (`_execution_session_id`) stays with the worker.
    """
    return bool(task.get('_chat_session')) and not any(worker_input(task, key) for key in WORKER_INPUTS)


class GatewayUnavailable(RuntimeError):
    """No adapter took the turn. Nothing ran, so the turn can wait for another adapter."""


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
        self.silent_seconds = SILENT_SECONDS
        # A cron run of the gateway (open_turn). The adapter cannot stop it: it runs in a cron session, not in the chat.
        self.cron = False
        # A turn that may end in silence, such as a reaction (TurnEvents).
        self.silent = False
        # The context (a follow-up on a pending card) that this turn answers, or None (gateway_chat.run_gateway_context).
        self.context_id = None


class GatewayBridge:
    def __init__(self, root):
        self.folder = Path(root) / 'gateway'
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Condition()
        self.adapters: dict[str, float] = {}
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

    def poll(self, data: dict) -> dict:
        home = data.get('home')
        if not isinstance(home, str) or not home:
            raise ValueError('Name the Hermes home of the adapter.')
        home = os.path.realpath(home)
        active = {value for value in data.get('active') or [] if isinstance(value, str)}
        wait = max(0.0, min(float(data.get('wait', POLL_SECONDS)), POLL_SECONDS))
        deadline = time.monotonic() + wait
        with self.lock:
            while True:
                current = time.monotonic()
                self.adapters[home] = current
                mine = [turn for turn in self.turns.values() if turn.home == home]
                for turn in mine:
                    if turn.id in active:
                        turn.seen = current
                offered = [turn for turn in mine if turn.state == 'pending']
                stopping = [turn for turn in mine if turn.state == 'stopping']
                if offered or stopping or self.answers.get(home) or self.closed or current >= deadline:
                    break
                self.lock.wait(deadline - current)
            for turn in offered:
                turn.state, turn.seen = 'active', time.monotonic()
            for turn in stopping:
                self.turns.pop(turn.id, None)
            answers = self.answers.pop(home, [])
            self.lock.notify_all()
        result = {'turns': [turn.request for turn in offered], 'stop': [turn.chat_id for turn in stopping]}
        if answers:
            result['answers'] = answers
        return result

    def event(self, data: dict) -> dict:
        kind, text, message_id = data.get('kind'), data.get('text'), data.get('message_id')
        if kind not in KINDS or (text is not None and not isinstance(text, str)) or len(text or '') > MAX_TEXT:
            raise ValueError('Send a known gateway event.')
        if kind in ('send', 'edit', 'delete') and not isinstance(message_id, str):
            raise ValueError('Name the message of this event.')
        if kind == 'delta' and (type(data.get('stream')) is not int or not text):
            raise ValueError('Name the model call of the streamed text.')
        if kind == 'question':
            question_choices(data)
        with self.lock:
            turn = self.turns.get(data.get('turn_id'))
            if turn is None or turn.state != 'active':
                # A cancelled or finished turn. The gateway may still send its last messages.
                return {'ok': False}
            turn.seen = time.monotonic()
            turn.events.append({key: data[key] for key in ('kind', 'text', 'message_id', 'interim', 'outcome', 'stream',
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

    def open_turn(self, task_id: str, home, chat_id: str) -> str:
        """Register a turn that the gateway already runs, for example a cron run. Return its ID for follow()."""
        turn = _Turn(task_id, os.path.realpath(home), chat_id, '', 'Coconut')
        turn.state, turn.silent_seconds, turn.cron = 'active', CRON_SILENT_SECONDS, True
        with self.lock:
            self.turns[turn.id] = turn
        return turn.id

    def run_turn(self, task_id: str, home, chat_id: str, text: str, emit, *, chat_name: str = 'Coconut',
                 session_root: str | None = None, silent: bool = False, context_id: str | None = None,
                 mode: str | None = None, own: str | None = None) -> None:
        """Give the turn to the adapter of `home` and translate its events with `emit` until the turn ends.

        With `silent`, the turn may end in silence (TurnEvents). With `context_id`, the turn answers a follow-up on a
        pending card: the plugin blocks the card tools in it. With `own`, the turn's own text, Hermes saves only `own`
        in the session, and the plugin gives the whole `text` to the model calls of this turn (hooks.add_turn_context)."""
        turn = _Turn(task_id, os.path.realpath(home), chat_id, text, chat_name, session_root)
        if own is not None:
            turn.request['own'] = own
        turn.silent = silent
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
        self.follow(turn.id, emit)

    def follow(self, turn_id: str, emit) -> None:
        """Translate the events of a registered turn with `emit` until the turn ends."""
        with self.lock:
            turn = self.turns[turn_id]
        events = TurnEvents(emit, turn.id, silent=turn.silent)
        try:
            while True:
                with self.lock:
                    while not turn.events and turn.state in ('pending', 'active') and not self.closed:
                        current = time.monotonic()
                        if turn.state == 'pending' and current - turn.created > TAKE_SECONDS:
                            raise GatewayUnavailable('No Hermes gateway took this turn.')
                        if turn.state == 'active' and current - turn.seen > turn.silent_seconds:
                            raise RuntimeError('The Hermes gateway stopped answering during this turn.')
                        self.lock.wait(1)
                    if turn.state in ('cancelled', 'stopping'):
                        return
                    if self.closed and not turn.events:
                        raise RuntimeError('The Coconut service stops.')
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

    Streamed text (`delta`) from the hooks of the plugin fills the streaming reply while the model writes. Each model
    call has its own stream number: text of a new call starts a new segment.
    The gateway sends a reply in segments: each `send` starts one, and `edit` replaces its text while Hermes streams.
    The newest segment is the streaming reply of the turn. A new segment keeps the earlier one as a complete interim
    message. A send that the gateway marks as interim, such as commentary, is a complete message at once, and it ends
    the streamed segment of the same text. The first send that is not interim takes over from the streamed text: later
    deltas only repeat what the gateway sends and edits.

    A `silent` turn, such as a reaction, shows no commentary. When Hermes sends no reply, or a reply that is a silence
    marker, the turn ends without a message, as the worker ends it (silent_responses.is_silence_reply). Hermes drops a
    reply that is only a marker, but the plugin streams its text, or only its start, before that.
    """

    def __init__(self, emit, turn_id=None, silent=False):
        self.emit = emit
        self.turn_id = turn_id
        self.silent = silent
        self.stream = None
        self.text = ''
        self.phase = None
        # The stream number of the streamed segment, the numbers that ended, and whether the gateway sends the reply.
        self.delta_stream = None
        self.ended_streams = set()
        self.sent = False

    def apply(self, event: dict) -> bool:
        """Apply one event. Return True at the end of the turn."""
        kind, text = event['kind'], event.get('text') or ''
        if kind == 'typing':
            self._phase('thinking')
        elif kind == 'delta':
            number = event['stream']
            if self.sent or number in self.ended_streams or (self.delta_stream is not None and number < self.delta_stream):
                return False
            self._phase('writing')
            if self.stream is not None and self.delta_stream == number:
                self.emit('text_delta', {'text': text})
                self.text += text
            else:
                self._seal()
                self.emit('text_delta', {'text': text})
                self.stream, self.text, self.delta_stream = f'stream:{number}', text, number
        elif kind == 'question':
            self.emit('gateway_question', {key: event.get(key) for key in (
                'question_id', 'question_kind', 'text', 'detail', 'choices')} | {'turn_id': self.turn_id})
        elif kind == 'send' and text.strip():
            self._phase('writing')
            streamed = self.stream is not None and self.stream.startswith('stream:')
            if event.get('interim') and self.silent:
                # A reaction turn shows no commentary, as in the worker. Take back the streamed text of the commentary.
                if streamed:
                    self.emit('text_delta', {'text': '', 'replace': True})
                    self.ended_streams.add(self.delta_stream)
                    self.stream, self.text = None, ''
                return False
            if event.get('interim'):
                if streamed:
                    # Commentary of the model call that the stream shows. Keep its exact text as the interim message.
                    if text != self.text:
                        self.emit('text_delta', {'text': text, 'replace': True})
                    self._seal()
                else:
                    self._seal()
                    self.emit('interim_message', {'text': text})
            elif self.stream is not None and (text == self.text or streamed):
                # The send of a reply that the stream already shows, maybe with small differences.
                if text != self.text:
                    self.emit('text_delta', {'text': text, 'replace': True})
                self.stream, self.text, self.sent = event['message_id'], text, True
            else:
                self._seal()
                self.emit('text_delta', {'text': text})
                self.stream, self.text, self.sent = event['message_id'], text, True
        elif kind == 'edit' and event['message_id'] == self.stream and text != self.text:
            self.emit('text_delta', {'text': text, 'replace': True})
            self.text = text
        elif kind == 'delete' and event['message_id'] == self.stream:
            self.emit('text_delta', {'text': '', 'replace': True})
            self.stream, self.text = None, ''
        elif kind == 'done':
            # Hermes sends no reply when the model chose silence. The plugin may have streamed the marker, or only its
            # start, which Hermes holds back.
            if event.get('outcome') == 'success' and self.silent and (not self.sent or is_silence_reply(self.text)):
                if self.text:
                    # Take back the streamed marker.
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
        if self.stream is not None:
            self.emit('interim_seal', {})
            if self.delta_stream is not None and self.stream == f'stream:{self.delta_stream}':
                self.ended_streams.add(self.delta_stream)
            self.stream, self.text = None, ''

    def _phase(self, phase):
        if self.phase != phase:
            self.phase = phase
            # A phase only labels progress, as in the worker path.
            try:
                self.emit('phase', {'phase': phase})
            except Exception:
                pass
