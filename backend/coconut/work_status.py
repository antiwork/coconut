"""Present PR activity from bot reports and current execution evidence."""
from datetime import datetime, timezone
from collections import deque


class WorkStatus:
    def _set_work_activity(self, artifact, task, payload):
        from .service import ServiceError, now, require_text
        self._restore_active_work(artifact)
        if 'activity' not in payload:
            return
        text = require_text(payload, 'activity', 80)
        state = payload.get('activity_state', 'working')
        if state not in ('working', 'ready', 'blocked') or '\n' in text:
            raise ServiceError(400, 'Use one short activity and a working, ready, or blocked state.')
        if not task.get('id'):
            raise ServiceError(400, 'Only the bot doing this task can report its activity.')
        artifact.update(_activity=text, _activity_state=state, _activity_task_id=task['id'],
                        _activity_head=artifact.get('head_sha'), _activity_turn=task.get('_native_turn_id'), _activity_at=now())
        self._put('artifacts', artifact)
        self._event('artifact.updated', self._public(artifact), artifact['bot_id'], artifact.get('task_id'))

    def _work_chain(self, root, tasks, records, *, include_continuations=False):
        """Follow recorded callbacks, including replies that assign further work."""
        linked = {}
        for record in records:
            for key in ('_relay_task_id', '_wait_task_id'):
                if record.get(key) and not record.get('_process_delivery'):
                    linked.setdefault(record['task_id'], set()).add(record[key])
        for task in tasks.values():
            if include_continuations and task.get('_assignment_task_id'):
                linked.setdefault(task['_assignment_task_id'], set()).add(task['id'])
            for field in ('_requester_reply_task_id', '_relay_callback_owner'):
                if task.get(field):
                    linked.setdefault(task[field], set()).add(task['id'])
            linked.setdefault(task['id'], set()).update(task.get('_waiting_on', []))
        found, pending = {}, deque([(root['id'], 0)])
        while pending:
            identity, depth = pending.popleft()
            if identity in found or identity not in tasks:
                continue
            task = tasks[identity]
            if task.get('exchange_id') != root.get('exchange_id'):
                continue
            bot = self._get('bots', task['bot_id'])
            unstarted = task['status'] == 'queued' and not task.get('_native_turn_id') and '_conversation_epoch' not in task
            if (bot.get('_deleted') or task.get('session_id') != bot.get('session_id')
                    or (not unstarted and task.get('_conversation_epoch') != self._setting('conversation_epoch'))):
                continue
            found[identity] = (task, depth)
            pending.extend((child, depth + 1) for child in sorted(linked.get(identity, set())) if child != identity)
        return found

    def _work_live(self, task, records, timestamp):
        from .background_processes import subscription
        worker = self.running.get(task['id'])
        if worker and worker.is_alive():
            return True
        return any(r['task_id'] == task['id'] and subscription(r) and not r.get('_process_delivery')
                   and r.get('turn_id') == task.get('_native_turn_id')
                   and r.get('_process_status') == 'running' and r.get('_process_observed_at')
                   and (timestamp - datetime.fromisoformat(r['_process_observed_at'].replace('Z', '+00:00'))).total_seconds() < 45
                   for r in records)

    def _tracked_work_input(self, task):
        tasks = {t['id']: t for t in self._all('tasks')}
        records = self._all('native_receipts')
        urls = set()
        for artifact in self._all('artifacts'):
            if artifact['kind'] != 'pr' or not self._pr_review_source_current(artifact):
                continue
            root = tasks[artifact['_review_task_id']]
            if task['id'] in self._work_chain(root, tasks, records):
                urls.add(artifact['url'])
        return sorted(urls)

    def _work_projection(self, artifacts):
        groups = {}
        tasks = {t['id']: t for t in self._all('tasks')}
        records = self._all('native_receipts')
        decisions = self._all('decisions')
        timestamp = datetime.now(timezone.utc)
        for artifact in artifacts:
            if artifact['kind'] == 'pr':
                groups.setdefault(artifact['url'].lower(), []).append(artifact)
        for url, peers in groups.items():
            owner = self._pr_review_owner(peers)
            reported = [a for a in peers if a.get('_activity') and self._pr_review_source_current(a)]
            source = max(reported, key=lambda a: a['_activity_at']) if reported else owner
            if owner and tasks[owner['_review_task_id']]['status'] in ('queued', 'working', 'waiting', 'needs_you'):
                source = owner
            activity, state = None, None
            task = tasks.get((source or {}).get('_review_task_id'))
            if task:
                chain = self._work_chain(task, tasks, records)
                latest = max(peers, key=lambda a: (a.get('observed_at') or a['created_at'], a.get('checked_at') or a['created_at'], a['id']))
                current_reports = [a for a in reported if a.get('_activity_task_id') in tasks
                                   and a['_activity_at'] >= task['created_at']
                                   and not self._get('exchanges', tasks[a['_activity_task_id']]['exchange_id']).get('cancelled')
                                   and a.get('_activity_head') == latest.get('head_sha')
                                   and a.get('_activity_turn') == tasks[a['_activity_task_id']].get('_native_turn_id')
                                   and tasks[a['_activity_task_id']]['status'] != 'cancelled'
                                   and self._work_chain(tasks[a['_activity_task_id']], tasks, records)]
                if current_reports:
                    active_report = max(current_reports, key=lambda a: a['_activity_at'])
                    reported_task = tasks[active_report['_activity_task_id']]
                    # Reporting a new phase changes presentation, not the review's return route.
                    if reported_task['exchange_id'] != task['exchange_id']:
                        chain = {}
                    chain.update(self._work_chain(reported_task, tasks, records))
                def report(candidate):
                    matches = [a for a in peers if a.get('_activity')
                               and a.get('_activity_task_id') == candidate['id']
                               and a.get('_activity_turn') == candidate.get('_native_turn_id')
                               and a.get('_activity_head') == latest.get('head_sha')]
                    return max(matches, key=lambda a: a['_activity_at']) if matches else {}
                def priority(pair):
                    candidate, depth = pair
                    phase = report(candidate).get('_activity_state')
                    blocked = candidate['status'] == 'failed' or phase == 'blocked'
                    descendants = self._work_chain(candidate, tasks, records) if blocked else {}
                    progressing = any(t['id'] != candidate['id'] and t['status'] in ('queued', 'working', 'waiting', 'needs_you')
                                      for t, _ in descendants.values())
                    urgent = 4 if candidate['status'] == 'needs_you' else 3 if blocked and not progressing else 0
                    live = candidate['status'] in ('working', 'waiting') and self._work_live(candidate, records, timestamp)
                    return (max(urgent, 2 if live else 0),
                            candidate['status'] in ('queued', 'working', 'waiting', 'needs_you', 'paused', 'failed'),
                            depth, candidate['updated_at'], candidate['id'])
                if chain:
                    task, _ = max(chain.values(), key=priority)
                status = task['status']
                selected = report(task)
                label = selected.get('_activity')
                phase = selected.get('_activity_state', 'working')
                current = bool(selected)
                live = self._work_live(task, records, timestamp)
                if status == 'needs_you':
                    activity, state = 'Waiting for your decision', 'waiting'
                elif status == 'failed':
                    activity, state = 'Needs attention', 'blocked'
                elif status == 'cancelled':
                    activity, state = 'Stopped', 'blocked'
                elif label and current and phase == 'blocked':
                    activity, state = label, 'blocked'
                elif live and status in ('working', 'waiting'):
                    activity, state = (label if label and current else 'Working'), 'working'
                elif status == 'queued':
                    activity, state = 'Queued', 'waiting'
                elif status == 'waiting':
                    waits = [r for r in records if r['task_id'] == task['id'] and r.get('_wait_task_id') and not r.get('_process_delivery')]
                    reason = waits[-1]['action']['arguments']['reason'] if waits else None
                    activity, state = (reason or ('Waiting for another bot' if task.get('_waiting_on') else 'Activity unconfirmed')), 'waiting'
                elif status == 'completed':
                    if phase == 'ready':
                        observed = latest.get('observed_at')
                        current &= bool(observed and latest.get('checks_state') == 'passed' and not latest.get('refresh_error')
                                        and (timestamp - datetime.fromisoformat(observed.replace('Z', '+00:00'))).total_seconds() < 120)
                    activity = label if label and current and phase in ('ready', 'blocked') else ('Review finished' if task.get('_pr_review') else 'Work finished')
                    state = phase if label and current and phase in ('ready', 'blocked') else 'idle'
                else:
                    activity, state = 'Activity unconfirmed', 'waiting'
            if any(d['status'] == 'pending' and (d.get('merge_pr_url') or '').lower() == url for d in decisions):
                activity, state = 'Waiting for your decision', 'waiting'
            dismissed = self._setting('work.dismissed.' + url)
            for artifact in peers:
                artifact.update(activity=activity, activity_state=state, activity_observed_at=timestamp.isoformat(),
                                activity_bot_id=task['bot_id'] if task else None,
                                work_dismissal_id=dismissed)
        self._project_tracked_items(artifacts, tasks, records, timestamp)
        return artifacts

    def _restore_active_work(self, artifact):
        if artifact.get('pr_state') == 'open' and artifact.get('observed_at') and not artifact.get('refresh_error'):
            self.db.execute('DELETE FROM settings WHERE key=?', ('work.dismissed.' + artifact['url'].lower(),))

    def _dismiss_work(self, artifact_id, data):
        from .service import ServiceError, encoded, require_text
        artifact = self._get('artifacts', artifact_id)
        if artifact['kind'] not in ('pr', 'work'):
            raise ServiceError(400, 'This item does not appear in Work.')
        identity = 'work:' + artifact['id'] if artifact['kind'] == 'work' else artifact['url'].lower()
        key = 'work.dismissed.' + identity
        token = require_text(data, 'dismissal_id', 80)
        undo = data.get('undo') is True
        current = self._setting(key)
        if undo:
            if current and current != token:
                raise ServiceError(409, 'A newer dismissal replaced this action.')
            self.db.execute('DELETE FROM settings WHERE key=?', (key,))
        else:
            if artifact['kind'] == 'work':
                peers = self._work_projection([artifact])
            else:
                peers = [a for a in self._all('artifacts') if a['kind'] == 'pr' and a['url'].lower() == artifact['url'].lower()]
            latest = max(peers, key=lambda a: (a.get('observed_at') or a['created_at'], a.get('checked_at') or a['created_at'], a['id']))
            finished = latest['status'] in ('completed', 'cancelled') if artifact['kind'] == 'work' else (latest.get('pr_state') in ('merged', 'closed') or latest['status'] == 'merged')
            if not finished:
                raise ServiceError(409, 'Only completed work can be dismissed.')
            self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encoded(token)))
        self._event('work.updated', {'url': artifact['url'], 'id': artifact['id']})
        return {'ok': True}
