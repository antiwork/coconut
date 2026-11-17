"""Usage of each agent by day, read from the session store that Hermes keeps in the profile of the agent.

Hermes records the turns, the model calls and the tool calls as messages with a time. It records the tokens and the
cost only as a total of each session, per model. So a day gets the share of a session's tokens and cost that equals its
share of the model calls of that session. A turn of a chat (a message of the person up to the next one) gets its share
in the same way (`chat_turns`). Coconut keeps no copy: each read comes from `state.db`, opened read-only.
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
# The turns of a chat that a report lists, the newest last.
TURNS = 80
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


def chat_sessions(db, chat_id):
    """The sessions of one chat of Coconut: those that the gateway started for the chat, and the sessions that they
    started in turn (subagents and compressions name their parent)."""
    columns = _columns(db, 'sessions')
    if not {'chat_id', 'source'} <= columns:
        return set()
    found = {row[0] for row in db.execute("SELECT id FROM sessions WHERE source='coconut' AND chat_id=?", (chat_id,))}
    if 'parent_session_id' in columns:
        frontier = set(found)
        while frontier:
            marks = ','.join('?' * len(frontier))
            children = {row[0] for row in db.execute(
                f'SELECT id FROM sessions WHERE parent_session_id IN ({marks})', tuple(frontier))} - found
            found |= children
            frontier = children
    return found


def session_chats(db):
    """The chat of each session: its own `chat_id`, or the chat of the session that started it (a subagent or a
    compression). A session of a cron run or of a terminal has no chat and maps to an empty name."""
    columns = _columns(db, 'sessions')
    if 'chat_id' not in columns:
        return {}
    parent = 'parent_session_id' if 'parent_session_id' in columns else 'NULL'
    source = 'source' if 'source' in columns else "''"
    rows = {row[0]: row[1:] for row in db.execute(f'SELECT id, chat_id, {parent}, {source} FROM sessions')}
    chats = {}
    for session_id in rows:
        current, seen = session_id, set()
        while current in rows and current not in seen:
            seen.add(current)
            chat, up, kind = rows[current]
            if chat and kind == 'coconut':
                chats[session_id] = str(chat)
                break
            current = up
        else:
            chats.setdefault(session_id, '')
    return chats


def read_usage(path, days=DEFAULT_DAYS, now=None, chat_id=None):
    """The usage of one profile for the last `days` days in the time of the Mac, the oldest day first. With `chat_id`,
    only the sessions of that chat count, and the report has the totals of the chat since its start in `total`."""
    now = time.time() if now is None else now
    today = _day(now)
    window = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    result = {day: _empty_day(day) for day in window}
    models, cost_known, cost_exact = {}, False, True
    rows_by = {}
    turns = []
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return _report(result, models, False, False)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        if 'timestamp' not in _columns(db, 'messages'):
            return _report(result, models, False, False)
        usage, started = _session_usage(db)
        chat = chat_sessions(db, chat_id) if chat_id is not None else None
        if chat is not None:
            usage = {key: rows for key, rows in usage.items() if key in chat}
        # The share of each day in the model calls of a session, and the counts of each day.
        owners = session_chats(db) if chat is None else {}
        calls, session, previous = {}, None, None
        # The turns of a chat: each message of the person starts one. Its model calls are counted per turn.
        spans, begun = {}, {}
        for session_id, role, timestamp in db.execute(
                'SELECT session_id, role, timestamp FROM messages ORDER BY session_id, timestamp, id'):
            if timestamp is None or (chat is not None and session_id not in chat):
                continue
            if chat is not None:
                begun.setdefault(session_id, timestamp)
                if role == 'user':
                    spans.setdefault(session_id, []).append(dict(started_at=timestamp, ended_at=timestamp, calls=0))
                elif spans.get(session_id) and role in ('assistant', 'tool'):
                    spans[session_id][-1]['ended_at'] = timestamp
                    spans[session_id][-1]['calls'] += role == 'assistant'
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
                if chat is None:
                    spent = rows_by.setdefault((owners.get(session_id, ''), row['model'], day.isoformat()),
                                               dict(cost_usd=0.0, tokens=0.0))
                    spent['cost_usd'] += row['cost_usd'] * share
                    spent['tokens'] += sum(row[field] for field in TOKEN_FIELDS if field != 'reasoning_tokens') * share
                cost_known = cost_known or row['cost_known']
                cost_exact = cost_exact and row['cost_exact']
                model = models.setdefault((row['model'], row['provider']), dict(
                    model=row['model'], provider=row['provider'], cost_usd=0.0, **{field: 0 for field in TOKEN_FIELDS}))
                for field in TOKEN_FIELDS:
                    model[field] += row[field] * share
                model['cost_usd'] += row['cost_usd'] * share
    report = _report(result, models, cost_known, cost_known and cost_exact)
    if chat is not None:
        report['total'] = _total(usage)
        report['turns'] = _chat_turns(spans, usage, begun)
    else:
        report['rows'] = [dict(chat=key[0], model=key[1], day=key[2], cost_usd=round(value['cost_usd'], 4),
                               tokens=round(value['tokens']))
                          for key, value in sorted(rows_by.items()) if value['cost_usd'] > 0 or value['tokens'] >= 1]
    return report


def _chat_turns(spans, usage, begun):
    """The turns of a chat with their share of the tokens and the cost of their session: the share of the model
    calls. The model of a turn is the model that cost the session most. A session without a message of the person (a
    subagent) counts whole for the turn that was running when it began. The newest TURNS turns, oldest first."""
    result = []

    def weigh(rows, share):
        return (sum(row[field] * share for row in rows for field in TOKEN_FIELDS if field != 'reasoning_tokens'),
                sum(row['cost_usd'] for row in rows) * share)

    for session_id, listed in spans.items():
        rows = usage.get(session_id) or []
        calls = sum(span['calls'] for span in listed)
        if not rows or not calls:
            continue
        top = max(rows, key=lambda row: (row['cost_usd'], sum(row[field] for field in TOKEN_FIELDS)))
        for span in listed:
            if not span['calls']:
                continue
            tokens, cost = weigh(rows, span['calls'] / calls)
            result.append(dict(
                started_at=span['started_at'], ended_at=span['ended_at'], model=top['model'], provider=top['provider'],
                model_calls=span['calls'], tokens=tokens, cost_usd=cost,
                cost_known=any(row['cost_known'] for row in rows), cost_estimated=not all(row['cost_exact'] for row in rows)))
    result.sort(key=lambda turn: turn['started_at'])
    for session_id, rows in usage.items():
        if session_id in spans or session_id not in begun:
            continue
        running = [turn for turn in result if turn['started_at'] <= begun[session_id]]
        if running:
            tokens, cost = weigh(rows, 1)
            running[-1]['tokens'] += tokens
            running[-1]['cost_usd'] += cost
            running[-1]['cost_known'] = running[-1]['cost_known'] or any(row['cost_known'] for row in rows)
            running[-1]['cost_estimated'] = running[-1]['cost_estimated'] or not all(row['cost_exact'] for row in rows)
    for turn in result:
        turn.update(started_at=round(turn['started_at'], 3), ended_at=round(turn['ended_at'], 3),
                    tokens=round(turn['tokens']), cost_usd=round(turn['cost_usd'], 4))
    return result[-TURNS:]


def _total(usage):
    """The tokens and the cost of sessions since their start, by model and together."""
    total = dict(cost_usd=0.0, **{field: 0 for field in TOKEN_FIELDS})
    known, exact, models = False, True, {}
    for rows in usage.values():
        for row in rows:
            for field in TOKEN_FIELDS:
                total[field] += row[field]
            total['cost_usd'] += row['cost_usd']
            known, exact = known or row['cost_known'], exact and row['cost_exact']
            model = models.setdefault((row['model'], row['provider']), dict(
                model=row['model'], provider=row['provider'], cost_usd=0.0, **{field: 0 for field in TOKEN_FIELDS}))
            for field in TOKEN_FIELDS:
                model[field] += row[field]
            model['cost_usd'] += row['cost_usd']
    rows = sorted(models.values(), key=lambda model: (-model['cost_usd'], model['model']))
    total['cost_usd'] = round(total['cost_usd'], 4)
    for model in rows:
        model['cost_usd'] = round(model['cost_usd'], 4)
    return dict(total, models=[model for model in rows if model['model']], cost_known=known,
                cost_estimated=known and not exact)


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
                # The chats of an agent use its profile, so the agent has their usage.
                bots = [dict(id=bot['id']) for bot in view._all('bots') if not bot.get('chat_of')]
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

    def chat_usage(self, bot_id, days=DEFAULT_DAYS, now=None) -> dict:
        """`GET /v1/bots/<id>/usage`: the tokens and the cost of one chat, since its start and by day. A new chat of an
        agent uses the profile of the agent, and Hermes names the chat in its sessions."""
        days = max(1, min(int(days), MAX_DAYS))
        current = time.time() if now is None else now
        with self._read_view() as view, view._read_transaction():
            bot = view._get('bots', bot_id)
            agent = view._get('bots', bot['chat_of']) if bot.get('chat_of') else bot
        try:
            report = read_usage(self.profiles.home(agent) / 'state.db', days, current, chat_id=bot_id)
        except (OSError, sqlite3.Error, ValueError, ProfileError):
            report = _report({}, {}, False, False)
            report['total'] = _total({})
        # A chat without a store, or a store without its sessions, has nothing yet.
        report.setdefault('total', _total({}))
        report.setdefault('turns', [])
        return dict(report, bot_id=bot_id, today=date.fromtimestamp(current).isoformat())
