"""The usage that each person of a team shares, for the leaderboard of the Usage page (docs/team-log.md, "Usage").

A person's Mac writes one kept `usage` entry in each team: the numbers of its agents by day (tokens, turns, tool calls,
time working and spend) for the days of the longest range of the Usage page. Each Mac reads the entries of the other
members, so `GET /v1/usage` ranks the people of each team (`teams`). The entry has numbers only: no chat, no model, no
project and no text. A Mac for a team only has no person and opens no Hermes store, so it writes none.

The sync of a room calls TeamUsage._team_share_usage after its other steps. It writes at most every USAGE_SECONDS, and
only when the days changed, so a quiet Mac writes nothing. Its read of the Hermes stores (Usage.usage_report) holds no
lock of a room.
"""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import hmac
import logging
import time

from . import team_keys
from .team_relay import TeamRelayError
from .team_store import ACTIVE, MAX_USAGE_DAYS, USAGE_COUNTS, same_fields

USAGE_LABEL = b'coconut room usage'
# The days of an entry: the longest range of the Usage page (UsageRange.quarter in ios/Coconut/Usage.swift). The app
# reads the same days, so the write and the page share the cache of Usage.usage_report.
USAGE_DAYS = 84
# The least time between two checks of the entry of a room, and the wait after a start before the first one, so a
# start reads no Hermes store before the rooms are in step.
USAGE_SECONDS = 30 * 60
USAGE_START_SECONDS = 60

log = logging.getLogger(__name__)


def usage_entry(token: str, room: str) -> str:
    """The entry ID of the `usage` entry of this Mac in a room. The member token is the key of the HMAC, as for a
    `seen` entry, so another member cannot take the ID first."""
    return hmac.new(token.encode(), USAGE_LABEL + bytes.fromhex(room), hashlib.sha256).hexdigest()[:32]


def person_days(report: dict) -> tuple[list[dict], bool]:
    """The days of a usage report of this Mac (Usage.usage_report) as one person: each day summed over the agents, the
    oldest first, a day with work only, and in each day only the numbers that are not zero. Also whether Hermes knows a
    price for a model of an agent."""
    days: dict[str, dict] = {}
    known = False
    for agent in report.get('agents', {}).values():
        known = known or bool(agent.get('cost_known'))
        for day in agent.get('days', []):
            total = days.setdefault(day['day'], {'day': day['day']})
            for field in USAGE_COUNTS:
                total[field] = total.get(field, 0) + int(day.get(field) or 0)
            total['cost_usd'] = total.get('cost_usd', 0.0) + float(day.get('cost_usd') or 0)
    result = []
    for name in sorted(days):
        total = dict(days[name], cost_usd=round(days[name]['cost_usd'], 4))
        kept = {field: value for field, value in total.items() if field == 'day' or value}
        if len(kept) > 1:
            result.append(kept)
    return result[-MAX_USAGE_DAYS:], known


def full_day(day: dict) -> dict:
    """A day with each number, as a day of an agent in GET /v1/usage."""
    return {'day': day['day'], **{field: int(day.get(field) or 0) for field in USAGE_COUNTS},
            'cost_usd': round(float(day.get('cost_usd') or 0), 4)}


class TeamUsage:
    """The sync step that writes the `usage` entry of this Mac, and `teams` of GET /v1/usage. Team mixes this class
    in."""

    def _init_team_usage(self):
        # The monotonic time of the last check of each room, and the time before which a start checks none.
        self._team_usage_checked: dict[str, float] = {}
        self._team_usage_after = time.monotonic() + USAGE_START_SECONDS

    @property
    def _team_shares_usage(self) -> bool:
        """Only a person's Mac shares its usage: Service reads the Hermes stores of its agents (usage.py). A Mac for a
        team only opens none."""
        return not self._team_bot_only and callable(getattr(self, 'usage_report', None))

    def _team_share_usage(self, room_id: str) -> None:
        """Write the `usage` entry of this Mac in the room when its days changed, at most every USAGE_SECONDS.

        A read of the stores that fails writes nothing until the next check. A refusal of the relay for the entry
        itself, for example when the kept entries are full, does not stop the sync of the room. A temporary failure of
        the relay fails the pass of the room, as each other write does, and the next pass checks the entry again.
        """
        if not self._team_shares_usage:
            return
        now = time.monotonic()
        if now < self._team_usage_after or now - self._team_usage_checked.get(room_id, float('-inf')) < USAGE_SECONDS:
            return
        self._team_usage_checked[room_id] = now
        try:
            days, known = person_days(self.usage_report(USAGE_DAYS))
        except Exception:
            log.warning('coconut.team_log room %s: the usage of this Mac did not read. The next check is in %d minutes.',
                        room_id[:8], USAGE_SECONDS // 60, exc_info=True)
            return
        fields = {'kind': 'usage', 'days': days, 'cost_known': known}
        with self._team_room_lock(room_id):
            with self.lock:
                room = self.team_store.room(room_id)
            if room is None or room['state'] != ACTIVE:
                return
            entry = usage_entry(team_keys.read(self.root, room_id)['token'], room_id)
            with self.lock:
                existing = self.team_store.entry(room_id, entry)
            if existing is not None and existing['kind'] == 'usage' and same_fields(existing['payload'], fields):
                return
            try:
                self._team_write(room, entry, True, fields)
            except TeamRelayError as error:
                # 507 says that the kept entries are full. That holds until this Mac deletes some, as for a page.
                if error.status in (401, 404) or (error.temporary and error.status != 507):
                    self._team_usage_checked.pop(room_id, None)
                    raise
                log.warning('coconut.team_log room %s: the relay refused the usage of this Mac with status %d.',
                            room_id[:8], error.status)

    def team_usage_view(self, own: list[dict], known: bool, days: int, today: str) -> list[dict]:
        """`teams` of GET /v1/usage: each active team with its people in the order of the Team screen. The person of
        this Mac has `own`, the days of now, and each other person the days of its newest `usage` entry, or null
        before its Mac shares any (an older Coconut). The days start `days` days before `today`. A Mac for a team only
        is not a person, so it is not in the list."""
        start = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()

        def window(rows: list[dict]) -> list[dict]:
            return [full_day(row) for row in rows if start <= row['day'] <= today]

        teams = []
        with self.lock:
            for room in self.team_store.rooms():
                if room['state'] != ACTIVE:
                    continue
                shared = self.team_store.usages(room['room'])
                people = []
                for member in self.team_store.members(room['room']):
                    if member['bot_only']:
                        continue
                    mine = member['member'] == room['member']
                    entry = None if mine else shared.get(member['member'])
                    people.append(dict(
                        member=member['member'], name=member['name'], avatar=member['avatar'], color=member['color'],
                        this_mac=mine, days=window(own) if mine else window(entry['days']) if entry else None,
                        cost_known=known if mine else bool(entry and entry['cost_known']),
                        updated_at=None if mine or entry is None else entry['updated_at']))
                teams.append(dict(room=room['room'], name=room['name'], people=people))
        return teams
