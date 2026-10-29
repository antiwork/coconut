"""Cron runs of the gateway as Coconut turns.

Every Hermes gateway ticks the cron store of each profile that it serves, and Hermes has no setting that keeps it
away from a profile while it serves the profile's chats. So the gateway fires the bots' routines, and the Coconut
service claims no scheduled routine (routine_coordinator.tick_native_routines).

A cron run executes in a Hermes cron session, often in a separate worker process, outside a Coconut chat turn. When a
run in a bot's profile starts, the hooks (hooks.py) open a Coconut turn for it with `POST /v1/gateway/cron`. The
service makes a routine task in the bot's chat. The run then reports to that turn as a chat turn does: streamed text,
commentary, the final answer (post_llm_call) and the end (on_session_end). The coconut tools act for that task, and
coconut_ask waits for its card with `POST /v1/gateway/answers`, because no adapter polls in a worker process.

The run finds its identity with `cron.execution_identity.current_cron_execution`, which Hermes binds for each fire.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid

from .adapter import ASK_SECONDS, DEFAULT_BRIDGE, REQUEST_SECONDS, Bridge, StreamRelay

LOG = logging.getLogger(__name__)
_RUNS: dict[str, 'CronRun'] = {}
_LOCK = threading.Lock()


def _bridge_path():
    try:
        from hermes_cli.config import load_config_readonly
        extra = (((load_config_readonly().get('gateway') or {}).get('platforms') or {}).get('coconut') or {}).get('extra')
        if isinstance(extra, dict) and extra.get('bridge'):
            return extra['bridge']
    except Exception:
        pass
    return DEFAULT_BRIDGE


class CronRun(StreamRelay):
    """The Coconut turn of one cron run. It offers what the hooks and the tools use of an adapter."""

    def __init__(self, execution):
        self.execution = execution
        self._bridge = Bridge(_bridge_path())
        self._init_relay()
        self.turn = None
        self.count = 0

    def open(self):
        from hermes_constants import get_hermes_home
        execution = self.execution
        try:
            answer = self._bridge.request('/v1/gateway/cron', {
                'event': 'start', 'home': os.path.realpath(get_hermes_home()), 'job_id': execution.job_id,
                'job_name': execution.job_name, 'execution_id': execution.execution_id}, REQUEST_SECONDS)
        except Exception as error:
            # Without the service, the run goes on as a plain cron run.
            LOG.info('Coconut: no Coconut turn for cron run %s (%s).', execution.execution_id, type(error).__name__)
            return
        if not answer.get('skip'):
            self.turn = answer.get('turn_id')

    def report(self, kind, **fields) -> bool:
        if self.turn is None:
            return False
        try:
            self._bridge.request('/v1/gateway/events', dict(fields, turn_id=self.turn, kind=kind), REQUEST_SECONDS)
            return True
        except Exception as error:
            LOG.warning('Coconut: the Coconut service did not take a %s event of a cron run (%s).', kind,
                        type(error).__name__)
            return False

    def send(self, text, interim=False):
        self.count += 1
        self.report('send', message_id=f'{self.turn}:{self.count}', text=str(text), interim=interim)

    def ask(self, turn, question, choices, detail=''):
        """coconut_ask in a cron run: show a card and wait for its answer through the service."""
        import time
        question_id = str(uuid.uuid4())
        self._bridge.request('/v1/gateway/events', {
            'turn_id': turn, 'kind': 'question', 'question_id': question_id, 'question_kind': 'ask',
            'text': question, 'detail': detail,
            'choices': [dict(id=f'choice-{index}', label=str(choice)) for index, choice in enumerate(choices)]},
            REQUEST_SECONDS)
        deadline = time.monotonic() + ASK_SECONDS
        while time.monotonic() < deadline:
            answer = self._bridge.request('/v1/gateway/answers', {'turn_id': turn, 'question_id': question_id},
                                          REQUEST_SECONDS + 30)
            if answer.get('ended'):
                return None
            if answer.get('answer') is not None:
                return answer['answer'].get('text')
        return None


def current():
    """The CronRun of the cron run of this context, or None outside a cron run or outside a bot's profile."""
    try:
        from cron.execution_identity import current_cron_execution
        execution = current_cron_execution()
    except Exception:
        return None
    if execution is None:
        return None
    with _LOCK:
        run = _RUNS.get(execution.execution_id)
        if run is None:
            run = _RUNS[execution.execution_id] = CronRun(execution)
            run.open()
    return run if run.turn is not None else None


def finish(outcome):
    """Report the end of the cron run of this context, and forget the run."""
    run = current()
    if run is not None:
        run.flush_stream(run.turn)
        for delay in (1, 2, 5, None):
            if run.report('done', outcome=outcome):
                break
            if delay is not None:
                import time
                time.sleep(delay)
    try:
        from cron.execution_identity import current_cron_execution
        execution = current_cron_execution()
    except Exception:
        return
    if execution is not None:
        with _LOCK:
            _RUNS.pop(execution.execution_id, None)
