"""Cron runs of the gateway as Coconut turns.

Every Hermes gateway ticks the cron store of each profile that it serves, and Hermes has no setting that keeps it
away from a profile while it serves the profile's chats. So the gateway fires the routines of each bot that it serves,
and the routines of a bot that no gateway serves are paused (gateway_chat.gateway_served_bots).

A cron run executes in a Hermes cron session, often in a separate worker process, outside a Coconut chat turn. When a
run in a bot's profile starts, the hooks (hooks.py) open a Coconut turn for it with `POST /v1/gateway/cron`. The
service makes a routine task in the bot's chat. The run then reports to that turn as a chat turn does: commentary,
the final answer (post_llm_call) and the end (on_session_end). The coconut tools act for that task. A
card of coconut_ask ends the run, as in a chat turn, and its answer continues the routine task in the bot's chat.

The run finds its identity with `cron.execution_identity.current_cron_execution`, which Hermes binds for each fire.
"""
from __future__ import annotations

import logging
import os
import threading

from .adapter import DEFAULT_BRIDGE, PAUSED, REQUEST_SECONDS, WAITED, Bridge, ends_quietly

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


class CronRun:
    """The Coconut turn of one cron run. It offers what the hooks and the tools use of an adapter."""

    def __init__(self, execution, session_id=''):
        self.execution = execution
        # The Hermes session of the run. An answer to a card of the run continues this session.
        self.session_id = str(session_id or '')
        self._bridge = Bridge(_bridge_path())
        self.turn = None
        self.count = 0

    def open(self):
        from hermes_constants import get_hermes_home
        execution = self.execution
        try:
            answer = self._bridge.request('/v1/gateway/cron', {
                'event': 'start', 'home': os.path.realpath(get_hermes_home()), 'job_id': execution.job_id,
                'job_name': execution.job_name, 'execution_id': execution.execution_id,
                'session_id': self.session_id}, REQUEST_SECONDS)
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
        if not interim and ends_quietly(self.turn, text):
            return
        self.count += 1
        self.report('send', message_id=f'{self.turn}:{self.count}', text=str(text), interim=interim)


def current(session_id=''):
    """The CronRun of the cron run of this context, or None outside a cron run or outside a bot's profile.

    on_session_start comes first and names the session of the run (hooks.cron_start)."""
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
            run = _RUNS[execution.execution_id] = CronRun(execution, session_id)
            run.open()
    return run if run.turn is not None else None


def finish(outcome):
    """Report the end of the cron run of this context, and forget the run."""
    run = current()
    if run is not None:
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
            run = _RUNS.pop(execution.execution_id, None)
        if run is not None:
            PAUSED.discard(run.turn)
            WAITED.discard(run.turn)
