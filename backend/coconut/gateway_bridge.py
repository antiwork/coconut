"""Plain chat turns through the Hermes gateway: the service side of the `coconut` gateway platform.

With the chat runtime `gateway` (gateway_chat), the service gives a plain chat turn to the `coconut` adapter of the
person's Hermes gateway (hermes_plugin/coconut) instead of to its own worker. The adapter calls two loopback routes on
the main listener, with the token of `<data>/gateway/bridge.json`:

- `POST /v1/gateway/poll` waits for turns of one Hermes home (one profile, one bot), and for turns to stop. The request
  lists the turns that still run in the gateway.
- `POST /v1/gateway/events` reports what the gateway does with a turn: `typing`, `send`, `edit`, `delete` and `done`.

The service thread of the turn translates these events into the provider events of the worker path (TurnEvents), so
the phone sees the same messages. A turn that no adapter takes in TAKE_SECONDS goes back to the worker.
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

TAKE_SECONDS = 30
# The adapter polls at most POLL_SECONDS apart and lists its running turns in each poll.
POLL_SECONDS = 25
SILENT_SECONDS = 90
ADAPTER_SECONDS = 60
MAX_TEXT = 200_000
KINDS = ('typing', 'send', 'edit', 'delete', 'done')

# A chat turn with one of these inputs needs Coconut's own tools, cards, work tracking or context. The worker runs it.
WORKER_INPUTS = (
    'attachments', '_pending_decisions', '_native_job', '_kanban_claim', '_native_outcome_id', '_execution_session_id',
    '_work_context_id', '_work_context', '_work_todos', '_work_resume', '_work_dependencies', '_recent_work_updates',
    '_tracked_prs', '_tracked_work', '_pr_review', '_background_completion', '_handoff_assignment', '_is_return',
    '_bot_messages', '_outgoing_bot_messages', '_source_activity', '_source_answer_steps', '_source_audit_prompt',
    '_reaction_feedback', '_technical_lane', '_self_recovery', '_native_receipts', '_bot_creation_receipts',
    '_bot_deletion_receipts', '_withdrawal_corrections', '_direct_reply_message_id', '_work_status_only')


def plain_chat_turn(task: dict) -> bool:
    """Whether a new turn is a plain chat turn: a person's text message in a bot's own chat, and nothing else."""
    return bool(task.get('_chat_session')) and not any(task.get(key) for key in WORKER_INPUTS)


class GatewayUnavailable(RuntimeError):
    """No adapter took the turn. Nothing ran, so the worker can run it."""


class _Turn:
    def __init__(self, task_id, home, chat_id, text, chat_name):
        self.id = str(uuid.uuid4())
        self.task_id, self.home, self.chat_id = task_id, home, chat_id
        self.request = {'id': self.id, 'chat_id': chat_id, 'chat_name': chat_name, 'text': text}
        self.state = 'pending'  # pending, active, done, cancelled, stopping
        self.created = self.seen = time.monotonic()
        self.events: list[dict] = []


class GatewayBridge:
    def __init__(self, root):
        self.folder = Path(root) / 'gateway'
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Condition()
        self.adapters: dict[str, float] = {}
        self.turns: dict[str, _Turn] = {}
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
                if offered or stopping or self.closed or current >= deadline:
                    break
                self.lock.wait(deadline - current)
            for turn in offered:
                turn.state, turn.seen = 'active', time.monotonic()
            for turn in stopping:
                self.turns.pop(turn.id, None)
            self.lock.notify_all()
        return {'turns': [turn.request for turn in offered], 'stop': [turn.chat_id for turn in stopping]}

    def event(self, data: dict) -> dict:
        kind, text, message_id = data.get('kind'), data.get('text'), data.get('message_id')
        if kind not in KINDS or (text is not None and not isinstance(text, str)) or len(text or '') > MAX_TEXT:
            raise ValueError('Send a known gateway event.')
        if kind in ('send', 'edit', 'delete') and not isinstance(message_id, str):
            raise ValueError('Name the message of this event.')
        with self.lock:
            turn = self.turns.get(data.get('turn_id'))
            if turn is None or turn.state != 'active':
                # A cancelled or finished turn. The gateway may still send its last messages.
                return {'ok': False}
            turn.seen = time.monotonic()
            turn.events.append({key: data[key] for key in ('kind', 'text', 'message_id', 'interim', 'outcome') if key in data})
            if kind == 'done':
                turn.state = 'done'
            self.lock.notify_all()
        return {'ok': True}

    def run_turn(self, task_id: str, home, chat_id: str, text: str, emit, *, chat_name: str = 'Coconut') -> None:
        """Give the turn to the adapter of `home` and translate its events with `emit` until the turn ends."""
        turn = _Turn(task_id, os.path.realpath(home), chat_id, text, chat_name)
        events = TurnEvents(emit)
        with self.lock:
            # A stop that no adapter collected is old news after this long.
            for old in [old for old in self.turns.values()
                        if old.state == 'stopping' and time.monotonic() - old.seen > SILENT_SECONDS]:
                del self.turns[old.id]
            self.turns[turn.id] = turn
            self.lock.notify_all()
        try:
            while True:
                with self.lock:
                    while not turn.events and turn.state in ('pending', 'active') and not self.closed:
                        current = time.monotonic()
                        if turn.state == 'pending' and current - turn.created > TAKE_SECONDS:
                            raise GatewayUnavailable('No Hermes gateway took this turn.')
                        if turn.state == 'active' and current - turn.seen > SILENT_SECONDS:
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
                    turn.state = 'stopping'
                    self.lock.notify_all()
            raise
        finally:
            with self.lock:
                if turn.state != 'stopping':
                    self.turns.pop(turn.id, None)

    def cancel(self, task_id: str) -> None:
        with self.lock:
            for turn in list(self.turns.values()):
                if turn.task_id == task_id and turn.state in ('pending', 'active'):
                    turn.state = 'stopping' if turn.state == 'active' else 'cancelled'
            self.lock.notify_all()

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.lock.notify_all()


class TurnEvents:
    """Translate the gateway events of one turn into provider events, as the worker sends them.

    The gateway sends a reply in segments: each `send` starts one, and `edit` replaces its text while Hermes streams.
    The newest segment is the streaming reply of the turn. A new segment keeps the earlier one as a complete interim
    message. A send that the gateway marks as interim, such as commentary, is a complete message at once.
    """

    def __init__(self, emit):
        self.emit = emit
        self.stream = None
        self.text = ''
        self.phase = None

    def apply(self, event: dict) -> bool:
        """Apply one event. Return True at the end of the turn."""
        kind, text = event['kind'], event.get('text') or ''
        if kind == 'typing':
            self._phase('thinking')
        elif kind == 'send' and text.strip():
            self._phase('writing')
            if event.get('interim'):
                self._seal()
                self.emit('interim_message', {'text': text})
            elif self.stream is not None and text == self.text:
                # The final send of a reply that the stream already shows.
                self.stream = event['message_id']
            else:
                self._seal()
                self.emit('text_delta', {'text': text})
                self.stream, self.text = event['message_id'], text
        elif kind == 'edit' and event['message_id'] == self.stream and text != self.text:
            self.emit('text_delta', {'text': text, 'replace': True})
            self.text = text
        elif kind == 'delete' and event['message_id'] == self.stream:
            self.emit('text_delta', {'text': '', 'replace': True})
            self.stream, self.text = None, ''
        elif kind == 'done':
            if event.get('outcome') == 'success':
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
            self.stream, self.text = None, ''

    def _phase(self, phase):
        if self.phase != phase:
            self.phase = phase
            # A phase only labels progress, as in the worker path.
            try:
                self.emit('phase', {'phase': phase})
            except Exception:
                pass
