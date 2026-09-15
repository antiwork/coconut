"""Present PR activity from bot reports and current execution evidence."""
from datetime import datetime, timezone
from collections import deque


class WorkStatus:
    def _set_work_activity(self, artifact, task, payload):
        from .service import ServiceError, now, require_text
        if task.get('id') and self._pr_report_superseded(artifact, task):
            return
        self._restore_active_work(artifact)
        if 'activity' not in payload:
            return
        text = require_text(payload, 'activity', 80)
        state = payload.get('activity_state', 'working')
        if state not in ('working', 'ready', 'blocked') or '\n' in text:
            raise ServiceError(400, 'Use one short activity and a working, ready, or blocked state.')
        if not task.get('id'):
            raise ServiceError(400, 'Only the bot doing this task can report its activity.')
        task['_work_progress_at'] = now()
        self._put('tasks', task)
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
            if task.get('source_task_id') and ((task.get('_native_relay') and not task.get('_is_return'))
                    or (include_continuations and task.get('_is_return') and task.get('_assignment_task_id'))):
                linked.setdefault(task['source_task_id'], set()).add(task['id'])
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
            if not include_continuations and task.get('exchange_id') != root.get('exchange_id'):
                continue
            bot = self._get('bots', task['bot_id'])
            unstarted = task['status'] == 'queued' and not task.get('_native_turn_id') and '_conversation_epoch' not in task
            if (bot.get('_deleted') or task.get('session_id') != bot.get('session_id')
                    or (not unstarted and task.get('_conversation_epoch') != self._setting('conversation_epoch'))):
                continue
            found[identity] = (task, depth)
            for child in sorted(linked.get(identity, set())):
                if child == identity or child not in tasks:
                    continue
                # A human reply can change the parent's exchange while its saved wait remains valid.
                if (include_continuations and tasks[child].get('exchange_id') != task.get('exchange_id')
                        and child not in task.get('_waiting_on', [])):
                    continue
                pending.append((child, depth + 1))
        return found

    def _work_live(self, task, records, timestamp):
        from .background_processes import subscription
        worker = self.running.get(task['id'])
        if worker and worker.is_alive():
            return True
        return any(r['task_id'] == task['id'] and subscription(r) and not r.get('_process_delivery')
                   and (r.get('turn_id') == task.get('_native_turn_id')
                        or (self._completion_context(r) and self._can_resume_completion(task, self._get('bots', task['bot_id']), r)))
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
        self._project_work_outcomes(artifacts, {t['id']: t for t in self._all('tasks')},
                                    self._all('native_receipts'), self._all('decisions'), datetime.now(timezone.utc))
        return artifacts

    def _restore_active_work(self, artifact):
        if (artifact.get('pr_state') == 'open' and artifact.get('observed_at') and not artifact.get('refresh_error')
                and not self._setting('work.cancelled.' + self._work_identity(artifact))):
            self.db.execute('DELETE FROM settings WHERE key=?', ('work.dismissed.' + artifact['url'].lower(),))

    @staticmethod
    def _work_identity(item):
        return 'work:' + item['id'] if item['kind'] == 'work' else item['url'].lower()

    def _cancel_work(self, artifact_id, callbacks):
        from .service import ServiceError, encoded, now
        item = self._canonical_work(self._get('artifacts', artifact_id))
        if item['kind'] not in ('work', 'pr'):
            raise ServiceError(400, 'This item does not appear in Work.')
        identity = self._work_identity(item)
        if self._setting('work.cancelled.' + identity):
            return {'ok': True}
        projected = self._work_projection([dict(item)])[0]
        if projected['work_state'] in ('completed', 'cancelled'):
            raise ServiceError(409, 'This work has already finished.')
        tasks = {t['id']: t for t in self._all('tasks')}
        peers = [a for a in self._all('artifacts') if a['kind'] == item['kind']
                 and self._work_identity(a) == identity]
        fields = ('task_id',) if item['kind'] == 'work' else ('_review_task_id', '_activity_task_id')
        owned = {p.get(field) for p in peers for field in fields} & tasks.keys()
        owned |= {t['id'] for t in tasks.values() if t.get('_work_context_id') in {p['id'] for p in peers}}
        while True:
            # Wait targets can belong to other work. Only follow owned attempts.
            expanded = owned | {t['id'] for t in tasks.values()
                if any(t.get(field) in owned for field in ('source_task_id', '_assignment_task_id',
                                                          '_requester_reply_task_id', '_relay_callback_owner'))}
            if expanded == owned:
                break
            owned = expanded
        self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        ('work.cancelled.' + identity, encoded(now())))
        others = [a for a in self._all('artifacts') if a['kind'] in ('work', 'pr') and not a.get('_hidden')
                  and self._work_identity(a) != identity
                  and not self._setting('work.cancelled.' + self._work_identity(a))]
        shared = set()
        for other in self._work_projection(others):
            if other['work_state'] in ('completed', 'cancelled'):
                continue
            for field in ('task_id', '_review_task_id', '_activity_task_id'):
                root = tasks.get(other.get(field))
                if root:
                    shared.update(self._work_chain(root, tasks, self._all('native_receipts'), include_continuations=True))
        for task_id in sorted(owned):
            task = tasks[task_id]
            if task_id in shared:
                if task['status'] in self.ACTIVE:
                    text = f"Stop work on {item['title']} (Work ID {item['id']}). Continue the other requested work."
                    self._message(task['bot_id'], text, task_id=task_id, role='user', human_authored=True)
                    task.setdefault('_steering', []).append(text)
                    self._put('tasks', task)
                continue
            self._cancel_one(task, callbacks, include_finished=True)
        for decision in self._all('decisions'):
            url = (decision.get('merge_pr_url') or '').lower()
            exact = item['kind'] == 'pr' and url == identity
            # Unlinked questions on a shared task need a fresh scope after cancellation.
            changed_scope = item['kind'] == 'work' and not url and decision['task_id'] in owned & shared
            if decision['status'] != 'pending' or not (exact or changed_scope):
                continue
            resolution = 'work_scope_changed' if changed_scope else 'cancelled'
            response = (f"The person cancelled {item['title']}. Continue the other requested work. "
                        "This question is withdrawn because its work scope is unclear. Ask a new question only if the remaining work still needs approval."
                        if changed_scope else 'The person cancelled this work. Do not execute its action. Continue only other requested work.')
            decision.update(status='resolved', resolution=resolution, resolution_context=response,
                            outcome='Work scope changed.' if changed_scope else 'Work cancelled.',
                            resolved_at=now(), revision=decision['revision'] + 1)
            self._put('decisions', decision)
            if decision.get('_receipt_id'):
                record = self._get('native_receipts', decision['_receipt_id'])
                if record['state'] == 'awaiting_answer':
                    record.update(state='completed', completed_at=now(), result=encoded({
                        'response': response, 'resolution': resolution, 'url': item.get('url')}))
                    self._put('native_receipts', record)
            task = self._get('tasks', decision['task_id'])
            self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
            if task['status'] == 'needs_you' and not any(d['status'] == 'pending' and d['task_id'] == task['id'] for d in self._all('decisions')):
                task.update(status='queued', _resume_decision=decision['id'], updated_at=now())
                self._put('tasks', task)
                self._event('task.updated', self._public(task), task['bot_id'], task['id'])
                self._refresh_bot(task['bot_id'])
        for peer in peers:
            self._event('artifact.updated', self._work_projection([dict(peer)])[0], peer['bot_id'], peer.get('task_id'))
        return {'ok': True}

    def _resume_work(self, artifact_id):
        from .service import ServiceError, now
        item = self._canonical_work(self._get('artifacts', artifact_id))
        if item['kind'] not in ('work', 'pr'):
            raise ServiceError(400, 'This item does not appear in Work.')
        key = 'work.cancelled.' + self._work_identity(item)
        if not self._setting(key) and self._work_projection([dict(item)])[0]['work_state'] != 'cancelled':
            return {'ok': True}
        bot = self._get('bots', item['bot_id'])
        task = self._send(bot, dict(text=f"Resume {item['title']}. Inspect saved results before repeating any action. Keep the existing approval scope.", mode='queue'), work_item=item)
        task = self._get('tasks', task['id'])
        task['_conversation_epoch'] = self._setting('conversation_epoch')
        self._put('tasks', task)
        self.db.execute('DELETE FROM settings WHERE key=?', (key,))
        self.db.execute('DELETE FROM settings WHERE key=?', ('work.dismissed.' + self._work_identity(item),))
        if item['kind'] == 'work':
            self._carry_work_history(item, dict(item))
            item.update(task_id=task['id'], status='waiting', activity='Queued', observed_at=now(), _work_resuming=True)
        else:
            for peer in self._all('artifacts'):
                if peer['kind'] == 'pr' and self._work_identity(peer) == self._work_identity(item):
                    if peer['id'] == item['id']:
                        peer['_review_task_id'] = task['id']
                    else:
                        peer['_review_task_id'] = None
                    peer['_reviewed_heads'] = []
                    peer.pop('_reviewed_head', None)
                    self._put('artifacts', peer)
            item = self._get('artifacts', item['id'])
        self._put('artifacts', item)
        self._event('artifact.updated', self._work_projection([dict(item)])[0], item['bot_id'], task['id'])
        return {'ok': True}

    def _dismiss_work(self, artifact_id, data):
        from .service import ServiceError, encoded, require_text
        artifact = self._canonical_work(self._get('artifacts', artifact_id))
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
            finished = finished or bool(self._setting('work.cancelled.' + self._work_identity(artifact)))
            if not finished:
                raise ServiceError(409, 'Only completed work can be dismissed.')
            self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encoded(token)))
        self._event('work.updated', {'url': artifact['url'], 'id': artifact['id']})
        return {'ok': True}
