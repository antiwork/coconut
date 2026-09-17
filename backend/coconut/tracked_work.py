"""Explicit work tracking outside pull requests."""
import re
from datetime import datetime, timezone


WORK_STATES = ('working', 'waiting', 'ready', 'blocked', 'completed', 'cancelled')
TRACK_WORK_SCHEMA = {
    'name': 'coconut_track_work',
    'description': 'Track a substantial task in Work when you start it or the person asks to follow it. '
        'Track the requested outcome. Preserve its completion condition across replies and handoffs. '
        'Use coconut_track_pr for PRs. Do not track quick replies, routine checks, or every delegated step. '
        'Reuse the same key for updates. This reports progress; it does not start work or grant permission.',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'key': {'type': 'string', 'maxLength': 80, 'description': 'A stable key for this bot, such as account-export. Reuse it across turns.'},
        'same_outcome_as': {'type': 'string', 'description': 'Existing parent Work ID when this delegated report tracks the same requested outcome. Keep distinct diagnostic outcomes separate.'},
        'title': {'type': 'string', 'maxLength': 120, 'description': 'The intended result in plain words.'},
        'activity': {'type': 'string', 'maxLength': 80, 'description': 'One short current activity or verified result. No counts, hashes, or jargon.'},
        'status': {'type': 'string', 'enum': list(WORK_STATES), 'description': 'Use completed only after verifying the requested result. Ready means ready for review. Waiting does not schedule a wake-up.'},
        'completion_condition': {'type': 'string', 'maxLength': 500, 'description': 'The observable result that completes this outcome. Preserve it across handoffs.'},
        'completion_evidence': {'type': 'string', 'maxLength': 2000, 'description': 'Required for completed work. Name the verified result and its evidence. Finishing a reply is not completion.'},
        'paused_by_decision_id': {'type': 'string', 'description': 'With blocked: the answered human decision that pauses this outcome. Preserve this link until later human direction changes the scope.'},
        'external_wait': {'type': 'object', 'additionalProperties': False, 'properties': {
            'job_id': {'type': 'string', 'description': 'Existing enabled Hermes routine in this bot that checks this source. Use its native job_id.'},
            'source': {'type': 'string', 'maxLength': 500, 'description': 'The exact issue, thread, or other source to check for the missing evidence. Read-only checks only.'},
        }, 'required': ['job_id', 'source'], 'description': 'Use with waiting when an external result is pending. Coconut includes this Work item in each run of the existing routine. This grants no new permission.'},
        'next_step': {'type': 'string', 'maxLength': 160, 'description': 'The next concrete step. This description does not queue work or grant permission.'},
    }, 'required': ['key', 'title', 'activity', 'status']},
}


class TrackedWork:
    def _external_work_routine(self, item):
        wait = item.get('external_wait')
        if not isinstance(wait, dict) or item.get('_work_resuming') or item.get('status') != 'waiting':
            return None
        if item.get('_work_parent_id'):
            parent = self._get('artifacts', item['_work_parent_id'])
            if parent.get('status') in ('completed', 'cancelled') or self._setting('work.cancelled.' + self._work_identity(parent)):
                return None
            if self._external_work_routine(parent):
                return None
        if item.get('task_id'):
            owner = self._get('tasks', item['task_id'])
            if (owner['status'] == 'cancelled' or self._get('exchanges', owner['exchange_id']).get('cancelled')
                    or self._setting('work.cancelled.' + self._work_identity(item))):
                return None
        return next((r for r in self._all('routines') if r.get('_native_id') == wait.get('job_id')
                     and r['bot_id'] == item['bot_id'] and r.get('enabled') and not r.get('error')
                     and r.get('next_run_at') and not r.get('_deleted')), None)

    def _retire_recovered_checks(self, item, task):
        recovery = self._assignment(task).get('_self_recovery') or {}
        known = {t['id'] for t in recovery.get('saved_tasks', [])}
        for child in self._all('artifacts'):
            identity = child.get('_external_wait_task_id')
            if child.get('_work_parent_id') != item['id'] or identity not in known:
                continue
            checking = self._get('tasks', identity)
            if checking['status'] not in ('completed', 'failed') or self._background_pending(self._assignment(checking)['id']):
                continue
            child['_work_previous_task_ids'] = list(dict.fromkeys(child.get('_work_previous_task_ids', [])
                + [self._assignment(checking)['id'], identity]))
            child.pop('_external_wait_task_id', None)
            child.update(status='waiting', activity=item['activity'])
            self._put('artifacts', child)

    def _work_title(self, task):
        parent = self._get('tasks', task['parent_task_id']) if task.get('parent_task_id') else None
        fallback = parent['title'] if parent and parent['status'] == 'failed' else task['title']
        return next((todo['content'] for todo in task.get('_work_todos', {}).get('todos', [])
                     if todo.get('id') == 'coconut-assignment' and todo.get('content')
                     and len(todo['content']) <= 120), fallback)

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
        checking = self._get('tasks', previous['_external_wait_task_id']) if previous.get('_external_wait_task_id') else None
        check_ids = [self._assignment(checking)['id'], checking['id']] if checking else []
        item['_work_previous_task_ids'] = list(dict.fromkeys(previous.get('_work_previous_task_ids', [])
            + [previous['task_id']] + check_ids
            + item.get('_work_previous_task_ids', [])))

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
        import json
        item = self._canonical_work(self._get('artifacts', item_id))
        item_id = item['id']
        tasks = {t['id']: t for t in self._all('tasks')}
        records = self._work_receipts()
        peers = [a for a in self._all('artifacts') if a['id'] == item_id or
                 (item['kind'] == 'pr' and a['kind'] == 'pr' and a['url'].lower() == item['url'].lower())
                 or a.get('_work_parent_id') == item_id]
        linked = {}
        for peer in peers:
            identities = [peer.get(key) for key in ('task_id', '_work_origin_task_id', '_review_task_id', '_activity_task_id')]
            for identity in identities + peer.get('_work_previous_task_ids', []):
                if identity in tasks:
                    linked.update(self._work_chain(tasks[identity], tasks, records, include_continuations=True))
        recent = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM native_receipts WHERE json_extract(data, '$.task_id') IN "
            "(SELECT value FROM json_each(?)) AND json_extract(data, '$.state')='completed' "
            "ORDER BY rowid DESC LIMIT 3", (json.dumps(sorted(linked)),))]
        projected = next(a for a in self._work_projection(peers) if a['id'] == item_id)
        latest = max((a for a in peers if a['id'] == item_id or not a.get('_work_parent_id')), key=lambda a: (a.get('observed_at') or a['created_at'], a.get('checked_at') or a['created_at'], a['id']))
        updates = [m for m in self._all('messages') if m.get('task_id') in linked
                   and m['role'] == 'assistant' and m['kind'] == 'text' and not m.get('streaming')]
        source = next((m for m in self._all('messages') if m.get('task_id') == item.get('_work_origin_task_id', item.get('task_id'))
                       and m['role'] == 'user'), None)
        return dict(id=item['id'], title=item['title'], url=item.get('url'), paused_by_decision_id=item.get('paused_by_decision_id'),
                    status=latest['status'], activity_state=projected.get('activity_state'), activity=projected.get('activity'),
                    **{k: projected.get(k) for k in ('work_state', 'work_next_step', 'completion_condition', 'completion_evidence',
                                                   'work_decision_id', 'work_attempts', 'work_dependencies')},
                    pull_request={k: latest.get(k) for k in ('pr_state', 'checks_state', 'head_sha', 'draft',
                                  'observed_at', 'checked_at', 'refresh_error', 'preview')} if item['kind'] == 'pr' else None,
                    related_work=[{k: a.get(k) for k in ('id', 'title', 'completion_evidence', 'external_wait')}
                                  for a in peers if a.get('_work_parent_id') == item_id],
                    source_message=dict(id=source['id'], text=source['text'][:1000]) if source else None,
                    updates=[dict(id=m['id'], text=m['text'][:1000], created_at=m['created_at']) for m in updates[-3:]],
                    tasks=[dict(id=t['id'], status=t['status'], summary=t['summary'][:200])
                           for t, _ in sorted(linked.values(), key=lambda pair: (pair[0]['id'] == item.get('task_id'),
                                   pair[0]['created_at'], pair[0]['id']))[-16:]],
                    recent_results=[dict(tool=r['action']['name'], result=str(r.get('_completion_text') or r.get('result', ''))[-1800:],
                                         delivered=bool(r.get('_process_delivery')),
                                         completed_at=r.get('completed_at'), process_status=r.get('_process_status'),
                                         process_observed_at=r.get('_process_observed_at'))
                                    for r in reversed(recent)],
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
        item = dict(id=root['id'], task_id=root['id'], bot_id=root['bot_id'], kind='work',
                    title=root['title'], status='working', activity='', _automatic_work=True)
        outcome = self._work_outcome(item, tasks, records, self._all('decisions'), self._all('contexts'), timestamp)
        return outcome['activity_state'], outcome['activity']

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
            unstarted = task['status'] == 'queued' and '_conversation_epoch' not in task
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

    def _sync_delegated_work(self, tasks, artifacts):
        # An explicit handoff with the same outcome key belongs in the parent's row.
        by_id = {a['id']: a for a in artifacts}
        records, chains = self._pending_completion_receipts(), {}
        def attempts(task):
            found = {}
            while task and task['id'] not in found:
                found[task['id']] = task
                previous = task.get('_assignment_task_id') or (task.get('_self_recovery') or {}).get('previous_task_id')
                task = tasks.get(previous)
            return found
        for item in artifacts:
            if item['kind'] != 'work' or not item.get('_work_key'):
                continue
            task = tasks.get(item.get('task_id'), {})
            task = tasks.get(task.get('_assignment_task_id'), task)
            if task.get('_work_context_id') == item['id']:
                task = self._work_execution_owner(task, tasks)
            source = task.get('source_task_id')
            source_assignment = tasks.get(source, {}).get('_assignment_task_id', source)
            explicit_parent = by_id.get(item.get('_same_outcome_as'))
            parent = explicit_parent or next((a for a in artifacts if source and a['kind'] == 'work'
                and a['id'] != item['id'] and a.get('_work_key') == item['_work_key']
                and {source, source_assignment}.intersection([a.get('task_id'), a.get('_work_origin_task_id'), *a.get('_work_previous_task_ids', [])])), None)
            seen = {item['id']}
            while parent and parent.get('_hidden') and parent['id'] not in seen:
                seen.add(parent['id'])
                parent = by_id.get(parent.get('_work_parent_id') or parent.get('_work_replaced_by'))
            # Existing links keep cancellation attached to earlier reports.
            if parent and (parent['id'] in seen or parent.get('_hidden')
                           or parent.get('status') == 'cancelled'
                           or (self._setting('work.cancelled.' + self._work_identity(parent))
                               and item.get('_work_parent_id') != parent['id'])):
                parent = None
            if parent:
                owner = tasks.get(parent.get('task_id'))
                if owner:
                    owner = tasks.get(owner.get('_assignment_task_id'), owner)
                if owner and parent['id'] not in chains:
                    chains[parent['id']] = self._work_report_chain(parent, tasks, records)
                completed_handoff = (owner and item.get('_work_parent_id') == parent['id']
                    and task.get('status') in ('completed', 'cancelled')
                    and (owner['id'] == item.get('_work_parent_task_id', source_assignment)
                         or (owner.get('_self_recovery') and item.get('_work_parent_task_id', source_assignment) in parent.get('_work_previous_task_ids', []))))
                related = task.get('id') in chains.get(parent['id'], {})
                if owner and explicit_parent and not related:
                    # Follow recorded attempts. A reused key alone does not link outcomes.
                    owners = set(attempts(owner))
                    if owner.get('_work_context_id') == parent['id']:
                        owners.update(parent.get('_work_previous_task_ids', []))
                    pending, visited = [task], set()
                    while pending and not related:
                        previous = pending.pop()
                        for identity, attempt in attempts(previous).items():
                            if identity in visited:
                                continue
                            visited.add(identity)
                            related = related or identity in owners
                            source = tasks.get(attempt.get('source_task_id'))
                            if source:
                                pending.append(source)
                if not owner or (not related and not completed_handoff):
                    parent = None
            inherited_alias = parent and parent.get('_work_key') != item['_work_key'] and item.get('_same_outcome_as') != parent['id']
            if parent and (not item.get('_hidden') or item.get('_work_parent_id') != parent['id'] or inherited_alias):
                if inherited_alias:
                    item['_same_outcome_as'] = parent['id']
                item.update(_hidden=True, _work_parent_id=parent['id'], _work_parent_task_id=owner['id'])
                self._put('artifacts', item)
                self._event('artifact.updated', dict(self._public(item), _hidden=True), item['bot_id'], item['task_id'])
            elif not parent and item.pop('_work_parent_id', None):
                item.pop('_same_outcome_as', None)
                item.pop('_work_parent_task_id', None)
                item['_hidden'] = False
                self._put('artifacts', item)
                self._event('artifact.updated', self._public(item), item['bot_id'], item['task_id'])

    def _sync_automatic_work(self):
        from .service import now, uid
        with self.transaction():
            tasks = {t['id']: t for t in self._all('tasks')}
            records = self._work_receipts()
            artifacts = self._all('artifacts')
            timestamp = datetime.now(timezone.utc)
            self._sync_followup_work(tasks, artifacts)
            self._resume_tracked_work(tasks, artifacts)
            self._sync_delegated_work(tasks, artifacts)
            covered = {a.get('task_id') for a in artifacts if a['kind'] in ('work', 'pr')}
            for task in tasks.values():
                if (task['id'] in covered or task.get('_chat_session') or task.get('_native_outcome_id') or task.get('source_bot_id')
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
                            title=self._work_title(task), activity='In progress', status='working',
                            created_at=now(), observed_at=now(), _work_key='task-' + task['id'], _automatic_work=True)
                artifacts.append(item)
                covered.add(task['id'])
                self._put('artifacts', item)
                self._event('artifact.created', self._public(item), bot['id'], task['id'])
            for item in artifacts:
                if not item.get('_automatic_work') or item.get('_hidden') or item.get('task_id') not in tasks:
                    continue
                task = tasks[item['task_id']]
                if not item.get('_work_resuming'):
                    title = self._work_title(task)
                    if title != item['title'] and item['title'] == task['title']:
                        item['title'] = title
                        self._put('artifacts', item)
                        self._event('artifact.updated', self._public(item), item['bot_id'], task['id'])
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
            if item and (self._setting('work.cancelled.' + self._work_identity(item))
                         or (item.get('_work_parent_id') and self._setting('work.cancelled.work:' + item['_work_parent_id']))):
                raise ServiceError(409, 'The person cancelled this work. Start new work only if they request it.')
            created = item is None
            details = {}
            for field, limit in (('completion_condition', 500), ('next_step', 160), ('completion_evidence', 2000)):
                if field in payload:
                    details[field] = require_text(payload, field, limit)
            if state == 'completed' and not details.get('completion_evidence'):
                raise ServiceError(400, 'State the verified result before completing this work.')
            external_wait = payload.get('external_wait')
            if external_wait is not None:
                if state != 'waiting' or not isinstance(external_wait, dict):
                    raise ServiceError(400, 'Use external_wait only with waiting work.')
                external_wait = dict(job_id=require_text(external_wait, 'job_id', 100),
                                     source=require_text(external_wait, 'source', 500))
                if not self._external_work_routine(dict(bot_id=bot['id'], status=state, external_wait=external_wait)):
                    raise ServiceError(409, 'Choose an enabled, synchronized Hermes routine with a next run in this bot.')
            binding = item.get('external_wait') if item else None
            routine_report = bool(binding and
                (self._assignment(task).get('_native_job') or {}).get('id') == binding['job_id'])
            report_only = bool(item and (self._assignment(task).get('_source_audit_prompt') or routine_report))
            if item:
                owner = self._get('tasks', item['task_id'])
                if owner['id'] != task_id and owner['created_at'] >= task['created_at']:
                    tasks = {t['id']: t for t in self._all('tasks')}
                    if task_id not in self._work_report_chain(item, tasks, self._pending_completion_receipts()):
                        raise ServiceError(409, 'A newer task owns this work item.')
                    report_only = True
                if owner['id'] != task_id and not report_only:
                    self._carry_work_history(item, dict(item))
            else:
                item = dict(id=uid(), bot_id=bot['id'], kind='work', url=None, preview='', created_at=now(), _work_key=key)
            if item.get('_same_outcome_as') and item.get('task_id') != task_id and not report_only and not payload.get('same_outcome_as'):
                parent = self._get('artifacts', item['_same_outcome_as'])
                tasks = {t['id']: t for t in self._all('tasks')}
                if task_id not in self._work_report_chain(parent, tasks, self._pending_completion_receipts()):
                    item.pop('_same_outcome_as')
            if payload.get('same_outcome_as') is not None:
                parent = self._get('artifacts', require_text(payload, 'same_outcome_as', 100))
                tasks = {t['id']: t for t in self._all('tasks')}
                parent_owner = tasks.get(parent.get('task_id'), {})
                parent_owner = tasks.get(parent_owner.get('_assignment_task_id'), parent_owner)
                parent_state = self._work_projection([dict(parent)])[0].get('work_state')
                if (parent['kind'] != 'work' or parent['id'] == item['id'] or parent.get('_hidden')
                        or parent_state in ('completed', 'cancelled') or not parent_owner
                        or self._setting('work.cancelled.' + self._work_identity(parent))
                        or task['id'] not in self._work_report_chain(parent, tasks, self._pending_completion_receipts())):
                    raise ServiceError(409, 'Choose an active parent Work item that delegates this task.')
                item['_same_outcome_as'] = parent['id']
            previous_tasks = self._followup_task_ids(task, self._all('decisions'))
            if previous_tasks:
                item['_work_previous_task_ids'] = list(dict.fromkeys(item.get('_work_previous_task_ids', []) + previous_tasks))
            item.pop('_work_resuming', None)
            # Audits report progress without replacing the task doing the work.
            if not report_only:
                item.update(task_id=task_id, _work_turn=task.get('_native_turn_id'))
            audit_report = self._assignment(task).get('_source_audit_prompt') or routine_report
            description_owner = self._get('tasks', item['task_id'] if audit_report else task_id)
            description_owner['_work_progress_at'] = now()
            self._put('tasks', description_owner)
            item.update(title=title, activity=activity, status=state,
                        observed_at=now(), _work_key=key, _automatic_work=False, _hidden=bool(item.get('_work_parent_id')))
            item.update(_work_report_task_id=description_owner['id'], _work_report_turn=description_owner.get('_native_turn_id'))
            item.update(details)
            self._apply_work_pause(item, task, payload, state)
            if routine_report and state == 'completed':
                item['_work_previous_task_ids'] = list(dict.fromkeys(item.get('_work_previous_task_ids', []) + [self._assignment(task)['id'], task_id]))
            item.pop('_external_wait_task_id', None)
            if routine_report and state not in ('completed', 'cancelled'):
                item['external_wait'] = external_wait or binding
                if state != 'waiting' or external_wait is None:
                    item['_external_wait_task_id'] = task_id
            else:
                item.pop('external_wait', None)
                if external_wait:
                    item['external_wait'] = external_wait
            if external_wait or state == 'completed':
                self._retire_recovered_checks(item, task)
            item.setdefault('completion_condition', title)
            if state != 'completed':
                item.pop('completion_evidence', None)
            if state not in ('completed', 'cancelled'):
                self.db.execute('DELETE FROM settings WHERE key=?', ('work.dismissed.work:' + item['id'],))
            self._put('artifacts', item)
            for automatic in self._all('artifacts'):
                if (automatic.get('_automatic_work') and not automatic.get('_hidden')
                        and automatic.get('task_id') == task_id and automatic['id'] != item['id']):
                    automatic['_hidden'] = True
                    self._put('artifacts', automatic)
                    self._event('artifact.updated', dict(self._public(automatic), _hidden=True), bot['id'], task_id)
            self._sync_delegated_work({t['id']: t for t in self._all('tasks')}, self._all('artifacts'))
            item = self._get('artifacts', item['id'])
            self._event('artifact.created' if created else 'artifact.updated',
                        dict(self._public(item), _hidden=bool(item.get('_hidden'))), bot['id'], task_id)
            return dict(self._public(item), key=key)

    def _tracked_items_input(self, task):
        items = [a for a in self._all('artifacts') if a['kind'] == 'work'
                 and (not a.get('_hidden') or a.get('_work_parent_id'))]
        tasks = {t['id']: t for t in self._all('tasks')}
        records = self._work_receipts()
        owned = {a['id'] for a in items if a.get('task_id') in tasks
                 and task['id'] in self._work_report_chain(a, tasks, records)}
        items = [a for a in items if a['bot_id'] == task['bot_id']
                 or (a['id'] in owned and not task.get('_native_job') and not self._assignment(task).get('_native_job'))]
        job_id = (tasks.get(task.get('_assignment_task_id'), task).get('_native_job') or {}).get('id')
        scheduled = {a['id'] for a in items if job_id and self._external_work_routine(a)
                     and a['external_wait']['job_id'] == job_id
                     and not self._setting('work.cancelled.' + self._work_identity(a))}
        assignment_id = tasks.get(task.get('_assignment_task_id'), task)['id']
        active_checks = set()
        for item in items:
            checking = tasks.get(item.get('_external_wait_task_id'), {})
            checking = tasks.get(checking.get('_assignment_task_id'), checking)
            if (job_id and checking.get('id') == assignment_id and not item.get('_work_resuming')
                    and (item.get('external_wait') or {}).get('job_id') == job_id
                    and not self._setting('work.cancelled.' + self._work_identity(item))
                    and not (item.get('_work_parent_id') and self._setting('work.cancelled.work:' + item['_work_parent_id']))):
                active_checks.add(item['id'])
        items = self._work_projection(items)
        active_checks &= {a['id'] for a in items if a.get('work_state') not in ('cancelled', 'completed')}
        owned |= active_checks
        scheduled &=  {a['id'] for a in items if a.get('work_state') == 'waiting'}
        items = [a for a in items if not a.get('_hidden') or a['id'] in scheduled | active_checks]
        items.sort(key=lambda a: (a['status'] not in ('completed', 'cancelled'), a['observed_at']), reverse=True)
        selected = [a for a in items if a['id'] in scheduled | active_checks] + [a for a in items if a['id'] not in scheduled | active_checks][:30]
        return [dict(id=a['id'], bot_id=a['bot_id'], key=a['_work_key'], title=a['title'], status=a['status'], activity=a.get('activity'),
                     task_id=a['task_id'], current_task=a['id'] in owned, work_state=a.get('work_state'),
                     completion_condition=a.get('completion_condition'), completion_evidence=a.get('completion_evidence'),
                     next_step=a.get('work_next_step'), external_wait=a.get('external_wait'), paused_by_decision_id=a.get('paused_by_decision_id'),
                     routine_check=a['id'] in scheduled,
                     dismissed=bool(self._setting('work.dismissed.work:' + a['id']))) for a in selected]
