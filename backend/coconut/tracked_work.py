"""Explicit work tracking outside pull requests."""
import re
from datetime import datetime, timezone


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
    def _canonical_work(self, item):
        seen = set()
        while item.get('_work_replaced_by') and item['id'] not in seen:
            seen.add(item['id'])
            replacement = self._get('artifacts', item['_work_replaced_by'])
            if replacement['kind'] != 'work' or replacement['bot_id'] != item['bot_id']:
                break
            item = replacement
        return item

    @staticmethod
    def _followup_task_ids(task, decisions):
        questions = {d['id']: d for d in decisions if d['bot_id'] == task['bot_id']}
        pending = [d.get('previous_decision_id') for d in questions.values() if d['task_id'] == task['id']]
        pending.append(task.get('_resume_decision'))
        seen, task_ids = set(), []
        while pending:
            identity = pending.pop()
            if identity in seen or identity not in questions:
                continue
            seen.add(identity)
            previous = questions[identity]
            task_ids.append(previous['task_id'])
            pending.append(previous.get('previous_decision_id'))
        return list(dict.fromkeys(task_ids))

    def _work_for_followup(self, task, artifacts, tasks, decisions):
        previous_tasks = set(self._followup_task_ids(task, decisions))
        candidates = []
        for item in artifacts:
            if item['kind'] != 'work' or item.get('_hidden') or item['bot_id'] != task['bot_id']:
                continue
            owner = tasks.get(item.get('task_id'))
            history = set(item.get('_work_previous_task_ids', []) + [item.get('task_id'), item.get('_work_origin_task_id')])
            if not owner or not previous_tasks.intersection(history):
                continue
            if owner['id'] != task['id']:
                if (item['status'] in ('completed', 'cancelled') or owner['status'] not in ('completed', 'failed')
                        or owner['created_at'] >= task['created_at']
                        or any(owner.get(k) != task.get(k) for k in ('session_id', 'conversation_bot_id', '_conversation_epoch'))):
                    continue
            candidates.append(item)
        earlier = [a for a in candidates if a['task_id'] != task['id']]
        candidates = earlier or candidates
        return candidates[0] if len(candidates) == 1 else None

    def _carry_work_history(self, item, previous):
        origins = [previous.get('_work_origin_task_id', previous['task_id'])]
        if item.get('_work_origin_task_id'):
            origins.append(item['_work_origin_task_id'])
        item['_work_origin_task_id'] = min(origins, key=lambda identity: (self._get('tasks', identity)['created_at'], identity))
        item['_work_previous_task_ids'] = list(dict.fromkeys(previous.get('_work_previous_task_ids', [])
            + [previous['task_id']] + item.get('_work_previous_task_ids', [])))

    def _sync_followup_work(self, tasks, artifacts):
        decisions = self._all('decisions')
        for item in sorted(artifacts, key=lambda a: (a['created_at'], a['id'])):
            if (item['kind'] != 'work' or item.get('_hidden') or item.get('_automatic_work')
                    or item.get('task_id') not in tasks or not item.get('_work_key', '').startswith('task-')
                    or item['_work_key'][5:] not in tasks):
                continue
            previous = self._work_for_followup(tasks[item['task_id']], artifacts, tasks, decisions)
            if not previous or previous['id'] == item['id']:
                continue
            # Keep the later report's ID so its links and any existing Undo still work.
            self._carry_work_history(item, previous)
            item['_work_previous_task_ids'] = list(dict.fromkeys(item['_work_previous_task_ids']
                + self._followup_task_ids(tasks[item['task_id']], decisions)))
            previous.update(_hidden=True, _work_replaced_by=item['id'])
            self._put('artifacts', previous)
            self._put('artifacts', item)
            self._event('artifact.updated', dict(self._public(previous), _hidden=True), previous['bot_id'], previous['task_id'])
            self._event('artifact.updated', self._public(item), item['bot_id'], item['task_id'])

    def _work_message_target(self, bot, data):
        from .service import ServiceError, require_text
        item = self._canonical_work(self._get('artifacts', require_text(data, 'work_artifact_id', 100)))
        if item['kind'] not in ('work', 'pr') or item.get('_hidden'):
            raise ServiceError(400, 'This work item is unavailable.')
        projected = next(a for a in self._work_projection(self._all('artifacts')) if a['id'] == item['id'])
        owners = {item['bot_id'], projected.get('activity_bot_id')}
        if item.get('_activity_task_id'):
            owners.add(self._get('tasks', item['_activity_task_id'])['bot_id'])
        if bot['id'] not in owners:
            raise ServiceError(400, 'Open this work item in its bot conversation.')
        if data.get('mode') == 'steer' or data.get('scheduled_at') or data.get('recipient_bot_ids'):
            raise ServiceError(400, 'Send work questions now in the selected conversation.')
        if not isinstance(data.get('work_status_only', False), bool):
            raise ServiceError(400, 'Use a valid update request.')
        return item

    def work_context_input(self, item_id):
        item = self._canonical_work(self._get('artifacts', item_id))
        item_id = item['id']
        tasks = {t['id']: t for t in self._all('tasks')}
        records = self._all('native_receipts')
        peers = [a for a in self._all('artifacts') if a['id'] == item_id or
                 (item['kind'] == 'pr' and a['kind'] == 'pr' and a['url'].lower() == item['url'].lower())]
        linked = {}
        for peer in peers:
            identities = [peer.get(key) for key in ('task_id', '_work_origin_task_id', '_review_task_id', '_activity_task_id')]
            for identity in identities + peer.get('_work_previous_task_ids', []):
                if identity in tasks:
                    linked.update(self._work_chain(tasks[identity], tasks, records, include_continuations=True))
        projected = next(a for a in self._work_projection(peers) if a['id'] == item_id)
        latest = max(peers, key=lambda a: (a.get('observed_at') or a['created_at'], a.get('checked_at') or a['created_at'], a['id']))
        updates = [m for m in self._all('messages') if m.get('task_id') in linked
                   and m['role'] == 'assistant' and m['kind'] == 'text' and not m.get('streaming')]
        source = next((m for m in self._all('messages') if m.get('task_id') == item.get('_work_origin_task_id', item.get('task_id'))
                       and m['role'] == 'user'), None)
        return dict(id=item['id'], title=item['title'], url=item.get('url'),
                    status=latest['status'], activity_state=projected.get('activity_state'), activity=projected.get('activity'),
                    pull_request={k: latest.get(k) for k in ('pr_state', 'checks_state', 'head_sha', 'draft',
                                  'observed_at', 'checked_at', 'refresh_error', 'preview')} if item['kind'] == 'pr' else None,
                    source_message=dict(id=source['id'], text=source['text'][:1000]) if source else None,
                    updates=[dict(id=m['id'], text=m['text'][:1000], created_at=m['created_at']) for m in updates[-3:]],
                    tasks=[dict(id=t['id'], status=t['status'], summary=t['summary'][:200])
                           for t, _ in sorted(linked.values(), key=lambda pair: (pair[0]['id'] == item.get('task_id'),
                                   pair[0]['created_at'], pair[0]['id']))[-16:]],
                    recent_results=[dict(tool=r['action']['name'], result=str(r.get('_completion_text') or r.get('result', ''))[-1800:],
                                         delivered=bool(r.get('_process_delivery')),
                                         completed_at=r.get('completed_at'), process_status=r.get('_process_status'),
                                         process_observed_at=r.get('_process_observed_at'))
                                    for r in [r for r in records if r['task_id'] in linked and r['state'] == 'completed'][-3:]],
                    decisions=[dict(id=d['id'], title=d['title'], context=d.get('context', ''))
                               for d in self._all('decisions') if d['task_id'] in linked and d['status'] == 'pending'])

    def recent_work_updates(self, task):
        tasks = [t for t in self._all('tasks') if t['bot_id'] == task['bot_id']
                 and t['session_id'] == task['session_id']]
        before = max((t.get('_turn_started_at', '') for t in tasks
                      if t['id'] != task['id'] and not t.get('_work_status_only')), default='')
        updates = {t['id']: t for t in tasks if t.get('_work_status_only')}
        messages = [m for m in self._all('messages') if m['task_id'] in updates
                    and m['role'] == 'assistant' and m['kind'] == 'text' and not m.get('streaming')
                    and not m.get('_hidden') and not m.get('_deleted') and m['created_at'] > before]
        return [dict(message_id=m['id'], text=m['text'][:1000], created_at=m['created_at'],
                     work_id=updates[m['task_id']]['_work_context_id']) for m in messages[-3:]]

    def _automatic_work_state(self, root, tasks, records, timestamp):
        chain = self._work_chain(root, tasks, records, include_continuations=True)
        turns = [t for t, _ in chain.values()] or [root]
        active = [t for t in turns if t['status'] in ('queued', 'working', 'waiting', 'needs_you')]
        if any(t['status'] == 'needs_you' for t in active):
            return 'waiting', 'Waiting for your decision'
        if any(self._work_live(t, records, timestamp) for t in active):
            return 'working', 'In progress'
        if any(t.get('_waiting_on') for t in active):
            return 'waiting', 'Waiting for another bot'
        if active:
            return 'waiting', 'Queued' if all(t['status'] == 'queued' for t in active) else 'Waiting'
        if root['status'] == 'completed':
            return 'completed', 'Completed'
        if root['status'] == 'cancelled':
            return 'cancelled', 'Stopped'
        return 'blocked', 'Needs attention'

    def _resume_tracked_work(self, tasks, artifacts):
        """Keep a failed request's row when a human continues that request."""
        from .service import now
        for task in sorted(tasks.values(), key=lambda t: (t['created_at'], t['id'])):
            parent = tasks.get(task.get('parent_task_id'))
            if (not parent or parent['status'] != 'failed' or task.get('source_bot_id')
                    or task.get('_work_context_id') or task.get('_assignment_task_id')
                    or task['thread_id'] != parent['thread_id']
                    or any(task.get(k) != parent.get(k) for k in ('bot_id', 'conversation_bot_id', 'session_id'))):
                continue
            bot = self._get('bots', task['bot_id'])
            unstarted = task['status'] == 'queued' and not task.get('_native_turn_id') and '_conversation_epoch' not in task
            if (bot.get('_deleted') or bot.get('archived') or task['session_id'] != bot['session_id']
                    or parent.get('_conversation_epoch') != self._setting('conversation_epoch')
                    or (not unstarted and task.get('_conversation_epoch') != self._setting('conversation_epoch'))):
                continue
            items = [a for a in artifacts if a['kind'] == 'work' and not a.get('_hidden')]
            previous = next((a for a in items if parent['id'] in [a.get('task_id'), a.get('_work_origin_task_id')] + a.get('_work_previous_task_ids', [])
                             and a['status'] not in ('completed', 'cancelled')), None)
            if not previous:
                continue
            owner = tasks[previous['task_id']]
            if owner['id'] != parent['id'] and (owner['status'] != 'failed' or owner['created_at'] >= task['created_at']):
                continue
            history = list(dict.fromkeys(previous.get('_work_previous_task_ids', []) + [owner['id']]))
            copies = [a for a in items if a.get('task_id') == task['id']]
            reported = [a for a in copies if not a.get('_automatic_work')
                        or self._setting('work.dismissed.work:' + a['id'])]
            if reported:
                # Preserve existing report links and the artifact ID used by Undo.
                if previous.get('_automatic_work'):
                    current = max(reported, key=lambda a: (a['observed_at'], a['id']))
                    current.setdefault('_work_origin_task_id', previous.get('_work_origin_task_id', parent['id']))
                    current['_work_previous_task_ids'] = list(dict.fromkeys(history + current.get('_work_previous_task_ids', [])))
                    self._put('artifacts', current)
                    previous['_hidden'] = True
                    self._put('artifacts', previous)
                    self._event('artifact.updated', dict(self._public(previous), _hidden=True), previous['bot_id'], parent['id'])
                continue
            previous.setdefault('_work_origin_task_id', parent['id'])
            previous['_work_previous_task_ids'] = history
            previous.update(task_id=task['id'], status='waiting', activity='Queued', observed_at=now(), _work_resuming=True)
            self._put('artifacts', previous)
            self._event('artifact.updated', self._public(previous), previous['bot_id'], task['id'])
            for duplicate in copies:
                duplicate['_hidden'] = True
                self._put('artifacts', duplicate)
                self._event('artifact.updated', dict(self._public(duplicate), _hidden=True), duplicate['bot_id'], task['id'])

    def _sync_automatic_work(self):
        from .service import now, uid
        with self.transaction():
            tasks = {t['id']: t for t in self._all('tasks')}
            records = self._all('native_receipts')
            artifacts = self._all('artifacts')
            timestamp = datetime.now(timezone.utc)
            self._sync_followup_work(tasks, artifacts)
            self._resume_tracked_work(tasks, artifacts)
            covered = {a.get('task_id') for a in artifacts if a['kind'] in ('work', 'pr')}
            for task in tasks.values():
                if (task['id'] in covered or task.get('source_bot_id')
                        or task.get('_native_job') or task.get('_source_audit_prompt')
                        or task.get('_assignment_task_id') or task.get('_work_context_id')
                        or task['status'] not in ('working', 'waiting', 'needs_you')):
                    continue
                bot = self._get('bots', task['bot_id'])
                if bot.get('_deleted') or bot.get('archived') or task.get('session_id') != bot.get('session_id'):
                    continue
                if task.get('_conversation_epoch') != self._setting('conversation_epoch'):
                    continue
                # Brief replies do not need a permanent Work entry.
                started = datetime.fromisoformat(task.get('_turn_started_at', task['created_at']).replace('Z', '+00:00'))
                if (timestamp - started).total_seconds() < 30:
                    continue
                human_request = self.db.execute("""SELECT 1 FROM messages
                    WHERE json_extract(data, '$.task_id')=? AND json_extract(data, '$.role')='user'
                    AND json_extract(data, '$.sender_bot_id') IS NULL
                    AND COALESCE(json_extract(data, '$.reaction_event'), 0)=0 LIMIT 1""", (task['id'],)).fetchone()
                if not human_request:
                    continue
                chain = self._work_chain(task, tasks, records, include_continuations=True)
                if covered.intersection(chain):
                    continue
                item = dict(id=uid(), bot_id=bot['id'], task_id=task['id'], kind='work', url=None, preview='',
                            title=task['title'][:120], activity='In progress', status='working',
                            created_at=now(), observed_at=now(), _work_key='task-' + task['id'], _automatic_work=True)
                artifacts.append(item)
                covered.add(task['id'])
                self._put('artifacts', item)
                self._event('artifact.created', self._public(item), bot['id'], task['id'])
            for item in artifacts:
                if not item.get('_automatic_work') or item.get('_hidden') or item.get('task_id') not in tasks:
                    continue
                task = tasks[item['task_id']]
                if task.get('_source_audit_prompt'):
                    item['_hidden'] = True
                    self._put('artifacts', item)
                    self._event('artifact.updated', dict(self._public(item), _hidden=True), item['bot_id'], item['task_id'])
                    continue
                if item['title'].startswith('Continue this proposal using the recorded reply.'):
                    proposal = next((d for d in self._all('decisions')
                                     if d.get('_execution_task_id') == task['id']), None)
                    if proposal:
                        item['title'] = proposal['title'][:120]
                        self._put('artifacts', item)
                        self._event('artifact.updated', self._public(item), item['bot_id'], task['id'])
                chain = self._work_chain(task, tasks, records, include_continuations=True)
                if any(a['kind'] == 'pr' and a.get('task_id') in chain for a in artifacts):
                    item['_hidden'] = True
                    self._put('artifacts', item)
                    self._event('artifact.updated', dict(self._public(item), _hidden=True), item['bot_id'], item['task_id'])
                    continue
                state, activity = self._automatic_work_state(tasks[item['task_id']], tasks, records, timestamp)
                if (item['status'], item['activity']) == (state, activity):
                    continue
                item.update(status=state, activity=activity, observed_at=now())
                if state not in ('completed', 'cancelled'):
                    self.db.execute('DELETE FROM settings WHERE key=?', ('work.dismissed.work:' + item['id'],))
                self._put('artifacts', item)
                self._event('artifact.updated', self._public(item), item['bot_id'], item['task_id'])

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
            if item is not None:
                item = self._canonical_work(item)
                key = item['_work_key']
            if item is None and key == 'task-' + task_id:
                item = self._work_for_followup(task, self._all('artifacts'),
                    {t['id']: t for t in self._all('tasks')}, self._all('decisions'))
                if item:
                    key = item['_work_key']
                    self._carry_work_history(item, dict(item))
            if item is None:
                item = next((a for a in self._all('artifacts') if a.get('_automatic_work') and a.get('task_id') == task_id), None)
            created = item is None
            report_only = bool(item and self._assignment(task).get('_source_audit_prompt'))
            if item:
                owner = self._get('tasks', item['task_id'])
                if owner['id'] != task_id and owner['created_at'] >= task['created_at']:
                    raise ServiceError(409, 'A newer task owns this work item.')
                if owner['id'] != task_id and not report_only:
                    self._carry_work_history(item, dict(item))
            else:
                item = dict(id=uid(), bot_id=bot['id'], kind='work', url=None, preview='', created_at=now(), _work_key=key)
            previous_tasks = self._followup_task_ids(task, self._all('decisions'))
            if previous_tasks:
                item['_work_previous_task_ids'] = list(dict.fromkeys(item.get('_work_previous_task_ids', []) + previous_tasks))
            item.pop('_work_resuming', None)
            # Audits report progress without replacing the task doing the work.
            if not report_only:
                item.update(task_id=task_id, _work_turn=task.get('_native_turn_id'))
            item.update(title=title, activity=activity, status=state,
                        observed_at=now(), _work_key=key, _automatic_work=False, _hidden=False)
            if state not in ('completed', 'cancelled'):
                self.db.execute('DELETE FROM settings WHERE key=?', ('work.dismissed.work:' + item['id'],))
            self._put('artifacts', item)
            for automatic in self._all('artifacts'):
                if (automatic.get('_automatic_work') and not automatic.get('_hidden')
                        and automatic.get('task_id') == task_id and automatic['id'] != item['id']):
                    automatic['_hidden'] = True
                    self._put('artifacts', automatic)
                    self._event('artifact.updated', dict(self._public(automatic), _hidden=True), bot['id'], task_id)
            self._event('artifact.created' if created else 'artifact.updated', self._public(item), bot['id'], task_id)
            return dict(self._public(item), key=key)

    def _tracked_items_input(self, task):
        items = [a for a in self._all('artifacts') if a['kind'] == 'work' and not a.get('_hidden') and a['bot_id'] == task['bot_id']]
        tasks = {t['id']: t for t in self._all('tasks')}
        records = self._all('native_receipts')
        owned = {a['id'] for a in items if a.get('task_id') in tasks
                 and task['id'] in self._work_chain(tasks[a['task_id']], tasks, records, include_continuations=True)}
        items = self._work_projection(items)
        items.sort(key=lambda a: (a['status'] not in ('completed', 'cancelled'), a['observed_at']), reverse=True)
        return [dict(key=a['_work_key'], title=a['title'], status=a['status'], activity=a.get('activity'),
                     task_id=a['task_id'], current_task=a['id'] in owned,
                     dismissed=bool(self._setting('work.dismissed.work:' + a['id']))) for a in items[:30]]

    def _project_tracked_items(self, artifacts, tasks, records, timestamp):
        for item in artifacts:
            if item['kind'] != 'work':
                continue
            task = tasks.get(item.get('task_id'))
            state, activity = item['status'], item['activity']
            if item.get('_automatic_work') and task:
                state, activity = self._automatic_work_state(task, tasks, records, timestamp)
            elif item.get('_work_resuming') and task:
                state, activity = self._automatic_work_state(task, tasks, records, timestamp)
                if state == 'completed':
                    state, activity = 'blocked', 'Needs attention'
            elif state not in ('completed', 'cancelled'):
                chain = self._work_chain(task, tasks, records, include_continuations=True) if task else {}
                active = [t for t, _ in chain.values() if t['id'] != task['id']
                          and t['status'] in ('queued', 'working', 'waiting', 'needs_you')]
                if active and task['status'] in ('completed', 'failed', 'waiting'):
                    if any(t['status'] == 'needs_you' for t in active):
                        state, activity = 'waiting', 'Waiting for your decision'
                    elif any(self._work_live(t, records, timestamp) for t in active):
                        state, activity = 'working', 'In progress'
                    else:
                        state, activity = 'waiting', 'Waiting for another bot'
                elif not task or task['status'] in ('failed', 'paused'):
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
