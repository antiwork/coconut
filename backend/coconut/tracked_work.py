"""Explicit work tracking outside pull requests."""
import re


WORK_STATES = ('working', 'waiting', 'ready', 'blocked', 'completed', 'cancelled')
TRACK_WORK_SCHEMA = {
    'name': 'coconut_track_work',
    'description': 'Track a substantial task in Work when you start it or the person asks to follow it. '
        'Use coconut_track_pr for PRs. Do not track quick replies, routine checks, or every delegated step. '
        'Reuse the same key for updates. This reports progress; it does not start work or grant permission.',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'key': {'type': 'string', 'maxLength': 80, 'description': 'A stable key for this bot, such as account-export. Reuse it across turns.'},
        'title': {'type': 'string', 'maxLength': 120, 'description': 'The intended result in plain words.'},
        'activity': {'type': 'string', 'maxLength': 80, 'description': 'One short current activity or verified result. No counts, hashes, or jargon.'},
        'status': {'type': 'string', 'enum': list(WORK_STATES), 'description': 'Use completed only after verifying the requested result. Ready means ready for review. Waiting does not schedule a wake-up.'},
    }, 'required': ['key', 'title', 'activity', 'status']},
}


class TrackedWork:
    def _register_work(self, task_id, payload):
        from .service import ServiceError, YieldForDecision, now, require_text, uid
        key = require_text(payload, 'key', 80)
        title = require_text(payload, 'title', 120)
        activity = require_text(payload, 'activity', 80)
        state = payload.get('status')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', key) or any('\n' in v or '\r' in v for v in (title, activity)) or state not in WORK_STATES:
            raise ServiceError(400, 'Use a stable work key, single-line text, and a supported work status.')
        with self.transaction():
            task = self._get('tasks', task_id)
            bot = self._get('bots', task['bot_id'])
            if bot.get('_deleted') or task['status'] != 'working' or self.stopping.is_set():
                raise YieldForDecision('This task no longer accepts provider output.')
            item = next((a for a in self._all('artifacts') if a['kind'] == 'work'
                         and a['bot_id'] == bot['id'] and a.get('_work_key') == key), None)
            created = item is None
            if item:
                owner = self._get('tasks', item['task_id'])
                if owner['id'] != task_id and owner['created_at'] >= task['created_at']:
                    raise ServiceError(409, 'A newer task owns this work item.')
            else:
                item = dict(id=uid(), bot_id=bot['id'], kind='work', url=None, preview='', created_at=now(), _work_key=key)
            item.update(task_id=task_id, title=title, activity=activity, status=state,
                        _work_turn=task.get('_native_turn_id'), observed_at=now())
            if state not in ('completed', 'cancelled'):
                self.db.execute('DELETE FROM settings WHERE key=?', ('work.dismissed.work:' + item['id'],))
            self._put('artifacts', item)
            self._event('artifact.created' if created else 'artifact.updated', self._public(item), bot['id'], task_id)
            return dict(self._public(item), key=key)

    def _tracked_items_input(self, task):
        items = [a for a in self._all('artifacts') if a['kind'] == 'work' and a['bot_id'] == task['bot_id']]
        items = self._work_projection(items)
        items.sort(key=lambda a: (a['status'] not in ('completed', 'cancelled'), a['observed_at']), reverse=True)
        return [dict(key=a['_work_key'], title=a['title'], status=a['status'],
                     dismissed=bool(self._setting('work.dismissed.work:' + a['id']))) for a in items[:30]]

    def _project_tracked_items(self, artifacts, tasks, records, timestamp):
        for item in artifacts:
            if item['kind'] != 'work':
                continue
            task = tasks.get(item.get('task_id'))
            state, activity = item['status'], item['activity']
            if state not in ('completed', 'cancelled'):
                if not task or task['status'] in ('failed', 'paused'):
                    state, activity = 'blocked', 'Needs attention'
                elif task['status'] == 'cancelled':
                    state, activity = 'cancelled', 'Stopped'
                elif task['status'] == 'needs_you':
                    state, activity = 'waiting', 'Waiting for your decision'
                elif state == 'working':
                    if task['status'] == 'completed':
                        state, activity = 'blocked', 'Needs attention'
                    elif task['status'] == 'queued':
                        state, activity = 'waiting', 'Queued'
                    elif task.get('_waiting_on'):
                        state, activity = 'waiting', 'Waiting for another bot'
                    elif item.get('_work_turn') != task.get('_native_turn_id') or not self._work_live(task, records, timestamp):
                        state, activity = 'waiting', 'Activity unconfirmed'
            item.update(status=state, activity=activity, activity_state=state,
                        activity_bot_id=item['bot_id'], activity_observed_at=timestamp.isoformat(),
                        work_dismissal_id=self._setting('work.dismissed.work:' + item['id']))
