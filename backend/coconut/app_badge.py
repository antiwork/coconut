"""The number on the app icon: pending cards, unread chats and the items of "Needs attention" on Home.

The iPhone gets it in each push (PushDelivery.badge_count), and the apps count the same things from their own state
(AppStore.appBadgeCount, with HomeFeed and AttentionRules). A card that waits long is one card, so the attention part
leaves out the rule for late cards. It also leaves out stale data, which only a phone can see.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .team_store import ACTIVE

# Work that got blocked or failed counts for this long.
WORK_WINDOW = timedelta(hours=24)
# A Deadline counts when it ends within this time.
DEADLINE_WINDOW = timedelta(hours=24)
STUCK_STATES = {'blocked', 'failed', 'needs_recovery'}
BANNER_TYPES = {'status', 'deadline'}


def parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def shown_bots(service) -> dict[str, dict]:
    """The agents that exist and are not archived, by ID."""
    return {bot['id']: bot for bot in service._all('bots') if not bot.get('_deleted') and not bot.get('archived')}


def pending_cards(service) -> int:
    """The pending cards that the person can see: not hidden, and not of a removed agent."""
    visible = {bot['id'] for bot in service._all('bots') if not bot.get('_deleted')}
    count = 0
    for (raw,) in service.db.execute("SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending'"):
        decision = json.loads(raw)
        if decision.get('_hidden') or decision.get('_deleted'):
            continue
        if decision['bot_id'] not in visible:
            continue
        task = service._get('tasks', decision['task_id'])
        if task['bot_id'] in visible and task.get('conversation_bot_id', task['bot_id']) in visible:
            count += 1
    return count


def team_unread(service) -> int:
    """The unread messages of the active teams: the Team chat, the named chats that are not muted, and the direct
    chats with a person or a bot."""
    store = service.team_store
    total = 0
    for room in store.rooms():
        if room['state'] != ACTIVE:
            continue
        name, member = room['room'], room['member']
        total += store.unread(name, member)
        muted = store.muted_chats(name)
        total += sum(store.unread(name, member, chat['entry']) for chat in store.chats(name) if chat['entry'] not in muted)
        total += sum(store.direct_unread(name, row['member']) for row in store.direct_chats(name))
        total += sum(store.direct_unread(name, row['member'], row['bot']) for row in store.bot_direct_chats(name))
    return total


def unread_chats(service) -> int:
    """The unread messages of the agent chats that are not muted, and of the teams."""
    return sum(max(0, bot.get('unread_count', 0)) for bot in shown_bots(service).values()
               if bot.get('notifications', True)) + team_unread(service)


def halted_agents(service) -> int:
    return sum(1 for bot in shown_bots(service).values() if bot.get('health'))


def stuck_work(service, now: datetime) -> int:
    """The To-dos and tasks that got blocked or failed within the window. A To-do and its task count one time."""
    active = set(shown_bots(service))

    def recent(value) -> bool:
        time = parse(value) if value else None
        return time is not None and now - time <= WORK_WINDOW
    todos = [item for item in service._all('artifacts')
             if item.get('kind') == 'work' and not item.get('_deleted') and item.get('status') in STUCK_STATES
             and item.get('bot_id') in active and recent(item.get('observed_at') or item.get('created_at'))]
    covered = {item.get('task_id') for item in todos if item.get('task_id')}
    tasks = [task for task in service._all('tasks')
             if not task.get('_deleted') and task.get('status') in STUCK_STATES
             and task.get('conversation_bot_id', task['bot_id']) in active and recent(task.get('updated_at'))
             and task['id'] not in covered]
    return len(todos) + len(tasks)


def urgent_notices(service, now: datetime) -> int:
    """The active Status and Deadline notices of the active teams that the person has not seen, and the active
    Deadlines that end within the window."""
    from .team_notices import active
    store = service.team_store
    stamp = now.isoformat(timespec='seconds').replace('+00:00', 'Z')
    count = 0
    for room in store.rooms():
        if room['state'] != ACTIVE:
            continue
        seen = store.seen(room['room']).get(room['member'], set())
        for notice in store.notices(room['room']):
            if notice['type'] not in BANNER_TYPES or not active(notice, stamp):
                continue
            if notice['entry'] not in seen:
                count += 1
                continue
            end = parse(notice['ends_at']) if notice.get('ends_at') else None
            if notice['type'] == 'deadline' and end is not None and now < end <= now + DEADLINE_WINDOW:
                count += 1
    return count


def attention_items(service, now: datetime) -> int:
    return halted_agents(service) + stuck_work(service, now) + urgent_notices(service, now)


def parts(service, now: datetime | None = None) -> dict[str, int]:
    """The three counts that make the badge. The caller needs no lock: each read takes it."""
    now = now or datetime.now(timezone.utc)
    return {'cards': pending_cards(service), 'unread': unread_chats(service),
            'attention': attention_items(service, now)}


def badge_count(service, now: datetime | None = None) -> int:
    return sum(parts(service, now).values())
