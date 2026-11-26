"""The notification settings of the person, the same on each device of this Mac (docs/team-log.md, "Notifications").

One JSON object in the settings table, under `notifications`:

- `paused_until`: a time in UTC, or null. Until then nothing notifies, except a card that needs attention now. What
  arrives during a pause never notifies later.
- `people`: `all` (each message of the team), `mentions` (direct messages, notices, mentions and keywords), `nothing`.
- `agents`: `all` (cards and replies), `cards`, `urgent` (cards that need attention now).
- `keywords`: words that notify in a team chat as a mention does, whole words, in any case.
- `mobile_delay`: null, or the minutes without input at a Mac after which the iPhone notifies. While the person works
  at a Mac, the Mac shows the banners and the iPhone waits. What the person read in that time never reaches the iPhone.

`PUT /v1/settings/notifications` changes some keys and answers the whole object. Each iPhone keeps its work hours and
Outside work hours (push_schedule.py). Until the first change, each iPhone follows its own level and its switch for
replies, as before, and the apps show the settings that match the newest iPhone. The Mac banners follow the same
settings (ios/Coconut/NotificationSettings.swift).

A Mac app reports the idle time of its keyboard and mouse with `PUT /v1/device/presence {idle}` every 30 seconds while
`mobile_delay` is set. The service keeps the time of the last input of each Mac in memory: after a restart the iPhone
notifies at once until the next report."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import time

KEY = 'notifications'
PEOPLE = ('all', 'mentions', 'nothing')
AGENTS = ('all', 'cards', 'urgent')
DEFAULTS = {'paused_until': None, 'people': 'all', 'agents': 'all', 'keywords': [], 'mobile_delay': None}
KEYWORDS = 30
KEYWORD_LENGTH = 40
PAUSE_DAYS = 30
DELAY_MINUTES = 60
# A delay of 0 is "as soon as I'm away": a minute without input, as one report of the Mac can be 30 seconds old.
AWAY_AT_LEAST = 60
CHAT_KINDS = ('team', 'direct', 'notice')


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def instant(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def normalized(value) -> dict:
    """The stored object with each missing or unusable key at its default."""
    result = dict(DEFAULTS)
    if not isinstance(value, dict):
        return result
    if value.get('paused_until') is not None and instant(value['paused_until']):
        result['paused_until'] = stamp(instant(value['paused_until']))
    for key, choices in (('people', PEOPLE), ('agents', AGENTS)):
        if value.get(key) in choices:
            result[key] = value[key]
    words = value.get('keywords')
    if isinstance(words, list):
        result['keywords'] = [w for w in words if isinstance(w, str) and 0 < len(w) <= KEYWORD_LENGTH][:KEYWORDS]
    delay = value.get('mobile_delay')
    if isinstance(delay, int) and not isinstance(delay, bool) and 0 <= delay <= DELAY_MINUTES:
        result['mobile_delay'] = delay
    return result


def paused(settings: dict, current: float | None = None) -> bool:
    until = instant(settings.get('paused_until')) if settings.get('paused_until') else None
    return until is not None and until.timestamp() > (time.time() if current is None else current)


def notifies(settings: dict, kind: str, urgent=False, current: float | None = None) -> bool:
    """Whether the settings let an item of this kind notify (push.PushDelivery.record). A card that needs attention
    now always does. A team message also needs team_notifies, which reads the people level and the keywords."""
    if kind == 'decision' and urgent:
        return True
    if paused(settings, current):
        return False
    if kind in CHAT_KINDS:
        return settings['people'] != 'nothing'
    if kind == 'decision':
        return settings['agents'] != 'urgent'
    return settings['agents'] == 'all'


def keyword_match(keywords, text) -> bool:
    """Whether the text has one of the keywords as a whole word, in any case."""
    if not keywords or not text:
        return False
    pattern = '|'.join(re.escape(word) for word in keywords)
    return re.search(rf'(?<!\w)(?:{pattern})(?!\w)', text, re.IGNORECASE) is not None


def migrated(level: str | None, messages: bool | None) -> dict:
    """The settings that keep what the newest iPhone chose before these settings: its level and its switch for
    replies. Only urgent let no team message notify."""
    if level == 'urgent_only':
        return dict(DEFAULTS, people='nothing', agents='urgent')
    if level is None:
        return dict(DEFAULTS)
    return dict(DEFAULTS, agents='all' if messages else 'cards')


class NotificationSettings:
    def notification_settings(self) -> dict:
        """The settings that the apps show and follow. Before the first change they come from the newest iPhone."""
        stored = self.stored_notification_settings()
        if stored is not None:
            return stored
        try:
            row = self._read_one('''SELECT p.data FROM push_devices d JOIN push_preferences p ON p.device_id=d.device_id
                WHERE d.enabled=1 ORDER BY d.updated DESC LIMIT 1''', ())
        except Exception:
            row = None
        chosen = json.loads(row[0]) if row else {}
        return migrated(chosen.get('level'), chosen.get('messages'))

    def stored_notification_settings(self) -> dict | None:
        """The settings after their first change, or None. Until then each iPhone follows its own level and its switch
        for replies, as before these settings (push.PushDelivery.allows)."""
        stored = self._setting(KEY)
        return normalized(stored) if stored is not None else None

    def set_notification_settings(self, data) -> dict:
        """PUT /v1/settings/notifications: change the keys in the body, answer the whole settings. The apps re-read
        the settings on the event, so each device shows the change."""
        from .service_base import ServiceError
        if not isinstance(data, dict) or not data or set(data) - set(DEFAULTS):
            raise ServiceError(400, 'Send one or more of ' + ', '.join(DEFAULTS) + '.')
        settings = self.notification_settings()
        if 'paused_until' in data:
            value = data['paused_until']
            until = instant(value) if value is not None else None
            if value is not None and until is None:
                raise ServiceError(400, 'Supply paused_until as a time, or null to resume.')
            current = datetime.now(timezone.utc)
            if until is not None and until > current + timedelta(days=PAUSE_DAYS):
                raise ServiceError(400, f'Pause notifications for {PAUSE_DAYS} days at most.')
            settings['paused_until'] = stamp(until) if until is not None and until > current else None
        for key, choices in (('people', PEOPLE), ('agents', AGENTS)):
            if key in data:
                if data[key] not in choices:
                    raise ServiceError(400, f'Choose {key} from ' + ', '.join(choices) + '.')
                settings[key] = data[key]
        if 'keywords' in data:
            words = data['keywords']
            if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
                raise ServiceError(400, 'Supply keywords as a list of words.')
            kept, seen = [], set()
            for word in (w.strip() for w in words):
                if word and word.casefold() not in seen:
                    seen.add(word.casefold())
                    kept.append(word)
            if len(kept) > KEYWORDS or any(len(w) > KEYWORD_LENGTH for w in kept):
                raise ServiceError(400, f'Supply at most {KEYWORDS} keywords of at most {KEYWORD_LENGTH} characters.')
            settings['keywords'] = kept
        if 'mobile_delay' in data:
            delay = data['mobile_delay']
            if delay is not None and (not isinstance(delay, int) or isinstance(delay, bool) or not 0 <= delay <= DELAY_MINUTES):
                raise ServiceError(400, f'Choose minutes from 0 to {DELAY_MINUTES}, or null to notify the iPhone at once.')
            settings['mobile_delay'] = delay
        self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (KEY, json.dumps(settings)))
        self._event('settings.updated', {KEY: settings})
        return settings

    def note_presence(self, device: str, data) -> dict:
        """PUT /v1/device/presence: the seconds since the last input at this Mac."""
        from .service_base import ServiceError
        idle = data.get('idle') if isinstance(data, dict) and set(data) == {'idle'} else None
        if not isinstance(idle, (int, float)) or isinstance(idle, bool) or not 0 <= idle < 10 ** 9:
            raise ServiceError(400, 'Supply idle as the seconds since the last input.')
        inputs = self.__dict__.setdefault('_last_inputs', {})
        inputs[device] = max(inputs.get(device, 0), time.time() - idle)
        return {'away': round(self.away_seconds())}

    def away_seconds(self, current: float | None = None) -> float:
        """The seconds since the last input at any Mac that reports it. Infinite without a report."""
        inputs = self.__dict__.get('_last_inputs') or {}
        if not inputs:
            return float('inf')
        return max(0.0, (time.time() if current is None else current) - max(inputs.values()))

    def holds_phones(self, current: float | None = None, settings: dict | None = None) -> bool:
        """Whether the iPhone waits because the person works at a Mac (mobile_delay). Without a report of a Mac it
        never waits, and the push loop reads no settings."""
        if not self.__dict__.get('_last_inputs'):
            return False
        settings = settings or self.notification_settings()
        delay = settings.get('mobile_delay')
        return delay is not None and self.away_seconds(current) < max(delay * 60, AWAY_AT_LEAST)
