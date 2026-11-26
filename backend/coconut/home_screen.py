"""The glance of the Home Screen and the Lock Screen, sent from the Mac to each iPhone after the person leaves the app.

The widgets and the Live Activity of the iPhone (ios/HomeScreen, ios/CoconutWidgets) show the cards that wait for the
person, the agents that work with their step, and the unread chats. The app writes that glance while it runs, so before
this sender the Lock Screen kept an agent that had finished and missed a card that came after the person left. Now the
Mac sends the glance at each change of it (HomeScreenPush), sealed for the phone with the key of its notifications
(push_relay.RelayTransport.seal_content). The relay keeps the newest glance for the widgets and gives it to the Live
Activity as its content state, so APNs and the relay never see its text (relay/src/device.js, the glance routes).

The glance is JSON that ios/HomeScreen/MacGlance.swift reads. Times are seconds since 1970:

    {"coconut": {"version": 1, "device_id": "<phone>", "kind": "glance"}, "at": 1795660000.0,
     "cards": [{"id", "title", "chat", "name", "at"}],
     "working": [{"chat", "name", "phase", "detail", "at"}],
     "unread": [{"chat", "name", "preview", "count", "at"}],
     "unread_count": 3}

The rows follow the app (AppStore.homeScreenGlance): the pending cards newest first in the chat that holds them, the
chats that work with the step of their newest running task (the phone names the phase, BotPhase), and the agent chats
with unread messages. A muted chat and an archived chat show no unread row. `unread_count` is every unread message, the
chats of the teams too (app_badge.unread_chats), and the to-dos stay the ones that the app wrote.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime

from . import app_badge
from .push_relay import notification_text
from .turn_phase import PHASES

VERSION = 1
# The most rows of each kind, as CoconutGlance.limit on the phone.
LIMIT = 6
# The bytes that one sealed envelope holds (PushEnvelope.seal).
MAX_BYTES = 2200
# The text of a message of attachments only (AttachmentOnly in the app).
ATTACHMENT_ONLY = '[attachment]'
# Each step of trimming that makes a glance fit MAX_BYTES, the first that fits wins: the longest texts, then the rows of
# unread chats, then the details of the steps, then more rows. The Live Activity shows one card and three agents.
FITS = (
    dict(rows=LIMIT, unread=LIMIT, title=120, name=60, detail=120, preview=120),
    dict(rows=LIMIT, unread=4, title=90, name=40, detail=80, preview=80),
    dict(rows=4, unread=2, title=80, name=32, detail=60, preview=60),
    dict(rows=3, unread=1, title=64, name=28, detail=0, preview=48),
    dict(rows=3, unread=0, title=48, name=24, detail=0, preview=0),
    dict(rows=1, unread=0, title=40, name=20, detail=0, preview=0),
)

# The shortest time between two glances to one phone.
PACE = 5
# A new step of an agent alone waits this long after the last glance: steps change every few seconds.
STEP_PACE = 60
# While an agent works or a card waits, a glance goes again after this long, so the Live Activity keeps its live times.
# The relay makes an activity stale after LIVE_STALE_SECONDS (600), so two glances can get lost before it does.
REFRESH = 240
# The widgets of iOS 26 reload at most this often from a glance, since WidgetKit budgets their pushes. A card that comes or
# goes reloads them at once.
WIDGET_PACE = 120
# A phone that asked the Mac for updates this recently has the app in front. The app then writes its own glance and
# updates its activity, and the Mac sends nothing. It asks again at once after each answer, and an answer waits 20 s.
IN_FRONT = 45
# How long the relay keeps a glance for the widgets.
LIFETIME = 6 * 3600
# How often the sender looks for a change.
CHECK = 2

log = logging.getLogger(__name__)


def stamp(value) -> float | None:
    """Seconds since 1970 of an ISO time of the Mac, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def list_name(bot: dict) -> str:
    """The name of a chat in a list, as Bot.listName: the agent, and the title of a further chat."""
    name = bot.get('name') or 'Agent'
    if not bot.get('chat_of'):
        return name
    title = (bot.get('chat_title') or '').strip()
    label = 'Photo' if title == ATTACHMENT_ONLY else title or 'New agent'
    return f'{name} · {label}'


def chat_of_task(task: dict) -> str:
    """The chat that shows a task, as AgentTask.chatBotId."""
    return task['bot_id'] if task.get('source_bot_id') else task.get('conversation_bot_id') or task['bot_id']


def cards(service, bots: dict) -> list[dict]:
    """The cards that wait for the person, newest first, in the chat that holds each one. The visibility of
    app_badge.pending_cards: not hidden, and not of a removed agent."""
    visible = {bot['id']: bot for bot in service._all('bots') if not bot.get('_deleted')}
    rows = []
    for (raw,) in service.db.execute("SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending'"):
        decision = json.loads(raw)
        if decision.get('_hidden') or decision.get('_deleted') or decision.get('bot_id') not in visible:
            continue
        task = service._get('tasks', decision['task_id'])
        if task['bot_id'] not in visible or task.get('conversation_bot_id', task['bot_id']) not in visible:
            continue
        chat = chat_of_task(task)
        holder = bots.get(chat) or visible.get(chat) or visible[decision['bot_id']]
        rows.append({'id': decision['id'], 'title': decision.get('title') or 'A card waits', 'chat': chat,
                     'name': list_name(holder), 'at': stamp(decision.get('created_at'))})
    rows.sort(key=lambda row: (row['at'] or 0, row['id']), reverse=True)
    return rows[:LIMIT]


def working(service, bots: dict, current: float) -> list[dict]:
    """The chats that work, the newest step first, as AppStore.botPhase: a chat works while its status says so or one of
    its tasks runs, and its newest running task names the step."""
    running: dict[str, dict] = {}
    for (raw,) in service.db.execute(
            "SELECT data FROM tasks WHERE json_extract(data,'$.status') IN ('working','running') ORDER BY rowid"):
        task = json.loads(raw)
        if not task.get('_deleted'):
            running[task['bot_id']] = task
    rows = []
    for bot in bots.values():
        task = running.get(bot['id'])
        if task is None and bot.get('status') not in ('working', 'running'):
            continue
        task = task or {}
        phase = task.get('phase') if task.get('phase') in PHASES else 'thinking'
        detail = ' '.join(str(task.get('phase_detail') or '').split())
        row = {'chat': bot['id'], 'name': list_name(bot), 'phase': phase,
               'at': stamp(task.get('phase_started_at') or task.get('turn_started_at'))}
        if detail:
            row['detail'] = detail
        rows.append(row)
    rows.sort(key=lambda row: (row['at'] or current, row['chat']), reverse=True)
    return rows[:LIMIT]


def unread_chats(service, bots: dict) -> list[dict]:
    """The agent chats with unread messages that are not muted, newest first, with the line of their newest message."""
    rows = []
    for bot in bots.values():
        count = bot.get('unread_count') or 0
        if count <= 0 or not bot.get('notifications', True):
            continue
        newest = service.db.execute("""SELECT MAX(json_extract(data,'$.created_at')) FROM messages
            WHERE json_extract(data,'$.bot_id')=? AND COALESCE(json_extract(data,'$._hidden'),0)=0""",
                                    (bot['id'],)).fetchone()[0]
        text = str(bot.get('last_message') or '')
        preview = 'Photo' if text.strip() == ATTACHMENT_ONLY else notification_text(text, limit=160, byte_limit=480)
        rows.append({'chat': bot['id'], 'name': list_name(bot), 'preview': preview, 'count': int(count),
                     'at': stamp(newest) or stamp(bot.get('created_at'))})
    rows.sort(key=lambda row: (row['at'] or 0, row['chat']), reverse=True)
    return rows[:LIMIT]


def glance(service, device_id: str, current: float | None = None) -> dict:
    """The glance of one phone now. The caller holds the lock of the service."""
    current = time.time() if current is None else current
    bots = app_badge.shown_bots(service)
    return {'coconut': {'version': VERSION, 'device_id': device_id, 'kind': 'glance'}, 'at': round(current, 3),
            'cards': cards(service, bots), 'working': working(service, bots, current),
            'unread': unread_chats(service, bots), 'unread_count': app_badge.unread_chats(service)}


def _cut(text, limit):
    text = str(text or '')
    return text if len(text) <= limit else text[:max(0, limit - 1)].rstrip() + '…'


def encode(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def fit(value: dict) -> bytes:
    """The glance in at most MAX_BYTES: the first step of FITS whose trimmed glance fits."""
    for step in FITS:
        trimmed = dict(value)
        trimmed['cards'] = [dict(row, title=_cut(row['title'], step['title']), name=_cut(row['name'], step['name']))
                            for row in value['cards'][:step['rows']]]
        trimmed['working'] = []
        for row in value['working'][:step['rows']]:
            row = dict(row, name=_cut(row['name'], step['name']))
            if 'detail' in row:
                if step['detail']:
                    row['detail'] = _cut(row['detail'], step['detail'])
                else:
                    del row['detail']
            trimmed['working'].append(row)
        trimmed['unread'] = [dict(row, name=_cut(row['name'], step['name']), preview=_cut(row['preview'], step['preview']))
                             for row in value['unread'][:step['unread']]]
        content = encode(trimmed)
        if len(content) <= MAX_BYTES:
            return content
    raise ValueError('The glance does not fit an envelope.')


def digests(value: dict) -> tuple[str, str]:
    """What a change of the glance is: `major` for the rows that come and go (a card, an agent that starts or stops, an
    unread chat), and `minor` for the steps of the agents that work."""
    def digest(parts):
        return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    major = digest([[(row['id'], row['title']) for row in value['cards']], sorted(row['chat'] for row in value['working']),
                    [(row['chat'], row['count'], row['preview']) for row in value['unread']], value['unread_count']])
    minor = digest([(row['chat'], row['phase'], row.get('detail'), row['at']) for row in value['working']])
    return major, minor


class HomeScreenPush:
    """Sends the glance of each phone that asked for it (`home_screen` in its push registration) to the relay: at
    once for a card that comes or goes and an agent that starts or stops, a new step at most every minute, and every few
    minutes while something works or waits. It sends nothing while the app is in front, because the app then writes the
    glance itself. Each send names the Live Activity event: `update` while an agent works or a card waits, then one `end`.
    """

    def __init__(self, delivery):
        self.delivery = delivery
        self.service = delivery.service
        self.relay = delivery.relay
        self.checked = 0.0
        self.seen: dict[str, float] = {}

    def note_seen(self, device_id: str) -> None:
        """The phone asks the Mac for updates: its app is in front (server.py, /v1/updates)."""
        self.seen[device_id] = time.monotonic()

    def in_front(self, device_id: str) -> bool:
        return time.monotonic() - self.seen.get(device_id, float('-inf')) < IN_FRONT

    def phones(self) -> list[tuple[str, dict, dict]]:
        """The phones that take glances: a relay route, notifications on, and `home_screen`, each with its state."""
        from .push import PHONES
        rows = self.service.db.execute(f'''SELECT g.device_id, r.data, g.data FROM push_glances g
            JOIN push_routes r ON r.device_id=g.device_id JOIN push_devices d ON d.device_id=g.device_id
            WHERE d.enabled=1 AND g.device_id IN ({PHONES})''').fetchall()
        return [(device_id, json.loads(route), json.loads(state)) for device_id, route, state in rows]

    def tick(self, current: float | None = None) -> None:
        current = time.time() if current is None else current
        if current - self.checked < CHECK or not self.relay.configured:
            return
        self.checked = current
        with self.service.transaction():
            phones = [(device_id, route, state) for device_id, route, state in self.phones()
                      if not self.in_front(device_id) and current >= state.get('retry_at', 0)]
            plans = []
            for device_id, route, state in phones:
                value = glance(self.service, device_id, current)
                plan = self.plan(state, value, current)
                if plan:
                    plans.append((device_id, route, state, value, plan))
        for device_id, route, state, value, plan in plans:
            self.send(device_id, route, state, value, plan, current)

    @staticmethod
    def plan(state: dict, value: dict, current: float) -> dict | None:
        """What to send now, or None: the Live Activity event, whether it is urgent, and whether the widgets reload."""
        major, minor = digests(value)
        done = not value['cards'] and not value['working']
        since = current - state.get('sent', 0)
        changed = major != state.get('major')
        cards = {row['id'] for row in value['cards']}
        new_card = bool(cards - set(state.get('cards', [])))
        widgets_wanted = changed or state.get('widgets_pending', False)
        widgets_due = widgets_wanted and (cards != set(state.get('cards', []))
                                          or current - state.get('widgets_sent', 0) >= WIDGET_PACE)
        if changed:
            due = since >= PACE
        elif minor != state.get('minor'):
            due = since >= STEP_PACE
        else:
            due = not done and since >= REFRESH
        if not due and not widgets_due:
            return None
        # The first glance after nothing works ends the activity. A glance of a quiet Mac leaves it alone.
        live = 'update' if not done else ('end' if state.get('live') != 'end' else None)
        return {'major': major, 'minor': minor, 'live': live, 'urgent': live == 'update' and new_card,
                'widgets': widgets_due, 'widgets_pending': widgets_wanted and not widgets_due}

    def send(self, device_id: str, route: dict, state: dict, value: dict, plan: dict, current: float) -> None:
        glance_id = str(uuid.uuid4())
        expires = int(current + LIFETIME)
        try:
            envelope = self.relay.seal_content(route['public_key'], fit(value), glance_id, expires)
        except Exception:
            log.warning('Coconut could not seal the Home Screen glance of a phone.')
            self.save(device_id, dict(state, retry_at=current + 300))
            return
        status, reason = self.relay.send_glance(route, envelope, glance_id, expires, live=plan['live'],
                                                urgent=plan['urgent'], widgets=plan['widgets'])
        if status != 200:
            # 404: a relay before glances. 410 or 401: the phone left the relay, and its next registration names a
            # new route. 429: the count of the relay. Anything else is a network error or a relay that is down.
            delay = {404: 3600, 410: 3600, 401: 3600, 400: 3600, 429: 60}.get(status, 30)
            if state.get('failed') != reason:
                log.info('The Home Screen glance of a phone did not reach the relay: %s %s', status, reason)
            self.save(device_id, dict(state, retry_at=current + delay, failed=reason))
            return
        sent = {'major': plan['major'], 'minor': plan['minor'], 'sent': current,
                'cards': [row['id'] for row in value['cards']],
                'live': plan['live'] or state.get('live'),
                'widgets_sent': current if plan['widgets'] else state.get('widgets_sent', 0),
                'widgets_pending': plan['widgets_pending']}
        self.save(device_id, sent)

    def save(self, device_id: str, state: dict) -> None:
        with self.service.transaction():
            self.service.db.execute('UPDATE push_glances SET data=? WHERE device_id=?', (json.dumps(state), device_id))
