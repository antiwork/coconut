"""Usage of each agent by day, read from the session store that Hermes keeps in the profile of the agent.

Hermes records the turns, the model calls and the tool calls as messages with a time. It records the tokens and the
cost only as a total of each session, per model. So a day gets the share of a session's tokens and cost that equals its
share of the model calls of that session. Coconut keeps no copy: each read comes from `state.db`, opened read-only.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
import threading
import time

from .profiles import ProfileError

DEFAULT_DAYS = 30
MAX_DAYS = 90
# A read of every profile takes a moment on a large store. The app asks again when a page opens.
CACHE_SECONDS = 60
# The time between two messages counts as work when the agent wrote the later one. A longer gap is a pause, for
# example a message that a routine added to a quiet chat.
WORK_GAP_SECONDS = 30 * 60
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens')
COUNT_FIELDS = ('turns', 'model_calls', 'tool_calls', 'working_seconds')


def _columns(db, table):
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})')}


def _day(timestamp):
    return datetime.fromtimestamp(timestamp).date()


def _empty_day(day):
    return dict(day=day.isoformat(), cost_usd=0.0, **{field: 0 for field in COUNT_FIELDS + TOKEN_FIELDS})


def _session_usage(db):
    """The tokens and the cost of each session, per model. The per-model table also has the calls of side tasks."""
    usage = {}
    sessions = _columns(db, 'sessions')
    if 'started_at' not in sessions:
        return usage, {}
    models = _columns(db, 'session_model_usage')
    seen = set()
    if models:
        fields = [field for field in TOKEN_FIELDS if field in models]
        cost = [name for name in ('actual_cost_usd', 'estimated_cost_usd') if name in models]
        query = ('SELECT session_id, model, ' + ('billing_provider' if 'billing_provider' in models else "''")
                 + ''.join(', ' + field for field in fields + cost) + ' FROM session_model_usage')
        for row in db.execute(query):
            values = dict(zip(fields + cost, row[3:]))
            usage.setdefault(row[0], []).append(_usage_row(row[1], row[2], values))
            seen.add(row[0])
    # A session from before the per-model table has only its own totals.
    fields = [field for field in TOKEN_FIELDS if field in sessions]
    cost = [name for name in ('actual_cost_usd', 'estimated_cost_usd') if name in sessions]
    provider = 'billing_provider' if 'billing_provider' in sessions else "''"
    model = 'model' if 'model' in sessions else "''"
    query = f'SELECT id, {model}, {provider}, started_at' + ''.join(', ' + field for field in fields + cost) + ' FROM sessions'
    started = {}
    for row in db.execute(query):
        started[row[0]] = row[3]
        if row[0] not in seen:
            usage.setdefault(row[0], []).append(_usage_row(row[1], row[2], dict(zip(fields + cost, row[4:]))))
    return usage, started


def _usage_row(model, provider, values):
    actual, estimated = values.get('actual_cost_usd') or 0, values.get('estimated_cost_usd')
    # Hermes knows the real cost of a few providers. For the others it estimates from its price list, or knows none.
    cost, known, exact = (actual, True, True) if actual else (estimated or 0, estimated is not None, False)
    return dict(model=model or '', provider=provider or '', cost_usd=float(cost), cost_known=known, cost_exact=exact,
                **{field: int(values.get(field) or 0) for field in TOKEN_FIELDS})


def read_usage(path, days=DEFAULT_DAYS, now=None):
    """The usage of one profile for the last `days` days in the time of the Mac, the oldest day first."""
    now = time.time() if now is None else now
    today = _day(now)
    window = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    result = {day: _empty_day(day) for day in window}
    models, cost_known, cost_exact = {}, False, True
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return _report(result, models, False, False)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        if 'timestamp' not in _columns(db, 'messages'):
            return _report(result, models, False, False)
        usage, started = _session_usage(db)
        # The share of each day in the model calls of a session, and the counts of each day.
        calls, session, previous = {}, None, None
        for session_id, role, timestamp in db.execute(
                'SELECT session_id, role, timestamp FROM messages ORDER BY session_id, timestamp, id'):
            if timestamp is None:
                continue
            if session_id != session:
                session, previous = session_id, None
            day = _day(timestamp)
            entry = result.get(day)
            if role == 'assistant':
                per_day = calls.setdefault(session_id, {})
                per_day[day] = per_day.get(day, 0) + 1
            if entry is not None:
                if role == 'user':
                    entry['turns'] += 1
                elif role == 'assistant':
                    entry['model_calls'] += 1
                elif role == 'tool':
                    entry['tool_calls'] += 1
                if role in ('assistant', 'tool') and previous is not None and 0 < timestamp - previous <= WORK_GAP_SECONDS:
                    entry['working_seconds'] += timestamp - previous
            previous = timestamp
    for session_id, rows in usage.items():
        per_day = calls.get(session_id)
        if not per_day:
            # A session without a model call in the store, for example after a prune of its messages.
            if started.get(session_id) is None:
                continue
            per_day = {_day(started[session_id]): 1}
        total = sum(per_day.values())
        for day, count in per_day.items():
            entry = result.get(day)
            if entry is None:
                continue
            share = count / total
            for row in rows:
                for field in TOKEN_FIELDS:
                    entry[field] += row[field] * share
                entry['cost_usd'] += row['cost_usd'] * share
                cost_known = cost_known or row['cost_known']
                cost_exact = cost_exact and row['cost_exact']
                model = models.setdefault((row['model'], row['provider']), dict(
                    model=row['model'], provider=row['provider'], cost_usd=0.0, **{field: 0 for field in TOKEN_FIELDS}))
                for field in TOKEN_FIELDS:
                    model[field] += row[field] * share
                model['cost_usd'] += row['cost_usd'] * share
    return _report(result, models, cost_known, cost_known and cost_exact)


def _report(result, models, cost_known, cost_exact):
    def rounded(entry):
        return {key: round(value, 4) if key == 'cost_usd' else round(value) if isinstance(value, float) else value
                for key, value in entry.items()}
    rows = sorted(models.values(), key=lambda model: (-model['cost_usd'], -model['input_tokens'] - model['output_tokens'], model['model']))
    return dict(days=[rounded(entry) for _, entry in sorted(result.items())],
                models=[rounded(model) for model in rows if model['model']],
                cost_known=cost_known, cost_estimated=cost_known and not cost_exact)


class Usage:
    """`GET /v1/usage`: the usage of each agent of this Mac."""

    def usage(self, days=DEFAULT_DAYS, now=None) -> dict:
        days = max(1, min(int(days), MAX_DAYS))
        current = time.time() if now is None else now
        guard = self.__dict__.setdefault('_usage_guard', threading.Lock())
        with guard:
            cached = getattr(self, '_usage_cache', None)
            if cached and cached[0] == days and now is None and current - cached[1] < CACHE_SECONDS:
                return cached[2]
            # The list of the agents comes from a read view, so the read of the stores holds no service lock.
            with self._read_view() as view, view._read_transaction():
                bots = [dict(id=bot['id']) for bot in view._all('bots')]
            agents = {}
            for bot in bots:
                try:
                    agents[bot['id']] = read_usage(self.profiles.home(bot) / 'state.db', days, current)
                except (OSError, sqlite3.Error, ValueError, ProfileError):
                    # An agent without a profile, or a store that Hermes rewrites now, shows no usage.
                    continue
            report = dict(days=days, today=date.fromtimestamp(current).isoformat(), agents=agents)
            if now is None:
                self._usage_cache = (days, current, report)
            return report
