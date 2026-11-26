"""How Support's customers felt, on the Support dashboard instead of in a message.

Gumclaw's CSAT lane (the `csat` cron of the Mac) measures every closed conversation of the support mailbox and keeps
one row per day in the Mac's Hermes home, and writes the day's read of that measurement beside it. This reads both,
so the dashboard shows the score, its move and the reasons, and the person asks for the work from the same page.

    GET /v1/support/csat

Nothing is measured twice here: the score of a day is the row the lane wrote, and this only places its numbers (the
move against yesterday, the week and the month, the fourteen days of the sparkline, the low and the high). The digest
is what the lane wrote in words: a headline, the reasons with their counts, and the notes.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

from .hermes_runtime import hermes_home

# The two files of the CSAT lane, in the Hermes home of the Mac.
HISTORY = 'cache/csat_sentiment_history.json'
DIGEST = 'cache/csat_digest.json'
# The days of the sparkline and of the low and the high.
SERIES_DAYS = 14
# A history row older than this many days makes the dashboard say how old it is instead of calling it today.
STALE_DAYS = 1


def _home() -> Path:
    return hermes_home()


def history_path() -> Path:
    return Path(os.environ.get('COCONUT_CSAT_HISTORY') or _home() / HISTORY).expanduser()


def digest_path() -> Path:
    return Path(os.environ.get('COCONUT_CSAT_DIGEST') or _home() / DIGEST).expanduser()


def _read(path: Path, default):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _day(value):
    """The date of a history key or of a digest, as a day, or None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _round(value):
    return None if value is None else round(value, 1)


def rows(history=None) -> list:
    """The days that the lane measured, oldest first: a row with a date and a score."""
    raw = history if history is not None else _read(history_path(), {})
    if not isinstance(raw, dict):
        return []
    out = []
    for key, row in raw.items():
        if not isinstance(row, dict):
            continue
        when = _day(row.get('date') or key)
        score = _number(row.get('csat_score'))
        if when is None or score is None:
            continue
        out.append(dict(row, date=when.isoformat(), csat_score=score))
    return sorted(out, key=lambda row: row['date'])


GRADES = ('amazing', 'great', 'good', 'bad')


def _today(row) -> dict:
    counts = row.get('counts') if isinstance(row.get('counts'), dict) else {}
    pcts = row.get('pcts') if isinstance(row.get('pcts'), dict) else {}
    # A grade without a share is left out rather than sent as null: the app reads the four as numbers.
    shares = {}
    for name in GRADES:
        share = _round(_number(pcts.get(name)))
        if share is not None:
            shares[name] = share
    return dict(date=row['date'],
                score=_round(row['csat_score']),
                happy_pct=_round(_number(row.get('happy_pct'))),
                classified=int(_number(row.get('classified_interactions')) or 0),
                counts={name: int(_number(counts.get(name)) or 0) for name in GRADES},
                pcts=shares,
                generated_at=row.get('generated_at'))


def _average(days: list):
    return sum(day['csat_score'] for day in days) / len(days) if days else None


def _digest(written=None, as_of=None) -> dict | None:
    """The read that the lane wrote in words, or None when it wrote none."""
    raw = written if written is not None else _read(digest_path(), {})
    if not isinstance(raw, dict) or not isinstance(raw.get('headline'), str):
        return None
    drivers = []
    for item in raw.get('drivers') or []:
        if isinstance(item, dict) and isinstance(item.get('name'), str) and item.get('name').strip():
            drivers.append(dict(name=item['name'].strip()[:60], count=int(_number(item.get('count')) or 0)))
    body = raw.get('body') if isinstance(raw.get('body'), str) else ''
    weekly = raw.get('weekly') if isinstance(raw.get('weekly'), str) else ''
    monthly = raw.get('monthly') if isinstance(raw.get('monthly'), str) else ''
    written_at = raw.get('written_at') if isinstance(raw.get('written_at'), str) else None
    return dict(date=raw.get('date') if isinstance(raw.get('date'), str) else None,
                headline=str(raw['headline']).strip()[:200],
                body=body.strip()[:4000],
                drivers=drivers[:5],
                weekly=weekly.strip()[:2000] or None,
                monthly=monthly.strip()[:2000] or None,
                written_at=written_at)


def csat(history=None, written=None, today=None) -> dict:
    """The mood of Support's customers: the newest measured day, its move, the series and the lane's own read.

    `history` and `written` replace the two files, and `today` the day of the Mac, for a check by hand.
    """
    days = rows(history)
    now = today or date.today()
    as_of = days[-1]['date'] if days else None
    series = [dict(date=day['date'], score=_round(day['csat_score'])) for day in days[-SERIES_DAYS:]]
    block = None
    moves = dict(yesterday=None, week=None, month=None)
    span = dict(days=SERIES_DAYS, low=None, low_date=None, high=None, high_date=None)
    if days:
        score = days[-1]['csat_score']
        block = _today(days[-1])
        if len(days) >= 2:
            moves['yesterday'] = _round(score - days[-2]['csat_score'])
        week = _average([day for day in days[-8:-1]])
        if week is not None:
            moves['week'] = _round(score - week)
        month = _average([day for day in days[-31:-1]])
        if month is not None:
            moves['month'] = _round(score - month)
        window = days[-SERIES_DAYS:]
        lowest = min(window, key=lambda day: day['csat_score'])
        highest = max(window, key=lambda day: day['csat_score'])
        span.update(days=len(window), low=_round(lowest['csat_score']), low_date=lowest['date'],
                    high=_round(highest['csat_score']), high_date=highest['date'])
    age = None
    measured = _day(as_of) if as_of else None
    if measured is not None:
        age = (now - measured).days
    return dict(as_of=as_of,
                fresh=age is not None and age <= STALE_DAYS,
                age_days=age,
                today=block,
                deltas=moves,
                series=series,
                range=span,
                digest=_digest(written, as_of) if written is not None else _digest(),
                read_at=datetime.now().astimezone().isoformat(timespec='seconds'))
