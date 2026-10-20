"""Present PR activity from bot reports and current execution evidence."""
import json
from datetime import datetime, timezone
from collections import deque
from uuid import uuid4

PAUSE_DECISION_STATES = ('approved', 'rejected', 'modified', 'replied', 'executed', 'failed')


class WorkStatus:
    def _work_pause_peers(self, item, *, shared=False):
        # shared=True is for a caller that only reads the peers. _save_work_pause changes them, so it asks for copies.
        artifacts = {a['id']: a for a in (self._all_shared('artifacts') if shared else self._all('artifacts'))}
        artifacts[item['id']] = item
        def identity(value):
            seen = set()
            while value['kind'] == 'work' and value['id'] not in seen:
                seen.add(value['id'])
                parent = value.get('_same_outcome_as') or (value.get('_work_parent_id') if value.get('_hidden') else None)
                if parent not in artifacts:
                    break
                value = artifacts[parent]
            return self._work_identity(value)
        key = identity(item)
        return [a for a in artifacts.values() if a['kind'] in ('pr', 'work') and identity(a) == key]

    def _work_pause(self, item, decisions=None, peers=None):
        if peers is None:
            peers = self._work_pause_peers(item, shared=True)
        identities = {p.get('paused_by_decision_id') for p in peers}
        return next((d for d in (decisions if decisions is not None else self._all_shared('decisions'))
                     if d['id'] in identities and d.get('decided_by')
                     and d['status'] in PAUSE_DECISION_STATES), None)

    def _save_work_pause(self, item, identity):
        from .service import now
        # Delegates and PR observers share the pause of their outcome.
        peers = [a for a in self._work_pause_peers(item) if a['id'] != item['id']]
        recorded_at = min((p['_work_pause_at'] for p in [item] + peers
                           if p.get('paused_by_decision_id') == identity and p.get('_work_pause_at')), default=now())
        for peer in [item] + peers:
            if identity:
                peer['paused_by_decision_id'] = identity
                peer['_work_pause_at'] = recorded_at
            else:
                peer.pop('paused_by_decision_id', None)
                peer.pop('_work_pause_at', None)
        for peer in peers:
            self._put('artifacts', peer)

    def _work_has_new_direction(self, task, pause, item):
        tasks = {}
        roots, pending = set(), [task['id']]
        while pending:
            identity = pending.pop()
            if not identity or identity in roots:
                continue
            row = self._read_one('SELECT data FROM tasks WHERE id=?', (identity,))
            if not row:
                continue
            tasks[identity] = json.loads(row[0])
            roots.add(identity)
            pending.extend(tasks[identity].get(key) for key in ('parent_task_id', 'source_task_id', '_assignment_task_id'))
        if any(tasks[identity].get('_work_status_only') for identity in roots):
            return False
        after = max((p.get('_work_pause_at', '') for p in self._work_pause_peers(item, shared=True)), default='')
        after = after or pause.get('decided_at') or pause['created_at']
        return self._human_direction_after(roots, after, pause)

    def _human_direction_after(self, task_ids, after, pause):
        # A person's message or approval after the pause is new direction, unless it answers the pausing card.
        return (any(m.get('_human_device_id') and m['role'] == 'user'
                    and m.get('decision_id') != pause['id'] and m['created_at'] > after
                    for identity in task_ids for row in self.db.execute(
                        "SELECT data FROM messages WHERE json_extract(data,'$.task_id')=?", (identity,))
                    for m in [json.loads(row[0])])
                or any(d['task_id'] in task_ids and d['id'] != pause['id'] and d.get('decided_by')
                       and d['status'] in ('approved', 'modified') and d.get('decided_at', '') > after
                       for d in self._all_shared('decisions')))

    def _task_work_items(self, task):
        identities = {task['id'], task.get('_assignment_task_id')} - {None}
        return [a for a in self._all_shared('artifacts') if a['kind'] in ('work', 'pr')
                and (not a.get('_hidden') or a.get('_work_parent_id'))
                and (a['status'] not in ('completed', 'cancelled') if a['kind'] == 'work'
                     else a.get('pr_state') not in ('closed', 'merged'))
                and (identities.intersection(a.get(key) for key in ('task_id', '_review_task_id', '_activity_task_id'))
                     or a['id'] == task.get('_work_context_id'))
                and not self._setting('work.cancelled.' + self._work_identity(a))]

    def _task_work_is_paused(self, task):
        items = self._task_work_items(task)
        return bool(items) and all(self._work_pause(item) for item in items)

    def _automatic_work_is_paused(self, task):
        if not any(task.get(key) for key in ('_self_recovery', '_pr_review', '_work_resume')):
            return False
        items = self._task_work_items(task)
        return bool(items) and all((pause := self._work_pause(item))
                                  and not self._work_has_new_direction(task, pause, item) for item in items)

    def _apply_work_pause(self, item, task, payload, state):
        from .service import ServiceError, require_text
        identity = payload.get('paused_by_decision_id')
        if identity is None:
            pause = self._work_pause(item)
            if state != 'blocked' and pause and self._work_has_new_direction(task, pause, item):
                self._save_work_pause(item, None)
            return
        decision = self._get('decisions', require_text(payload, 'paused_by_decision_id', 100))
        tasks = {t['id']: t for t in self._all('tasks')}
        roots = {task['id'], task.get('_assignment_task_id'), item.get('task_id'),
                 item.get('_review_task_id'), item.get('_work_origin_task_id'), *item.get('_work_previous_task_ids', [])}
        # Recovery replaces the owner. Follow its recorded parents to retain decisions made by a delegate.
        pending = list(roots)
        while pending:
            previous = tasks.get(pending.pop(), {})
            for key in ('parent_task_id', '_assignment_task_id'):
                parent = previous.get(key)
                if parent and parent not in roots:
                    roots.add(parent)
                    pending.append(parent)
        records = self._pending_completion_receipts()
        for identity in list(roots):
            if identity in tasks:
                roots.update(self._work_chain(tasks[identity], tasks, records, include_continuations=True))
        if (state != 'blocked' or not decision.get('decided_by')
                or decision['status'] not in PAUSE_DECISION_STATES
                or decision['task_id'] not in roots):
            raise ServiceError(409, 'Link an answered human decision from this Work item before pausing it.')
        self._save_work_pause(item, decision['id'])

    def _set_work_activity(self, artifact, task, payload):
        from .service import ServiceError, now, require_text
        if task.get('id') and artifact['kind'] == 'pr':
            task['_reported_pr_urls'] = sorted(set(task.get('_reported_pr_urls', [])) | {artifact['url'].lower()})
            self._put('tasks', task)
        if task.get('id') and self._pr_report_superseded(artifact, task):
            return
        self._link_pr_outcome(artifact, task, payload)
        self._restore_active_work(artifact)
        if 'activity' not in payload:
            if payload.get('paused_by_decision_id') is not None:
                raise ServiceError(400, 'Include activity text when recording a pause.')
            return
        if task.get('_is_return'):
            newer = [peer.get('_activity_at', '') for peer in self._all('artifacts')
                     if peer.get('kind') == 'pr' and peer.get('url', '').lower() == artifact['url'].lower()
                     and peer.get('_activity_head') == artifact.get('head_sha')
                     and peer.get('_activity_task_id') != task['id']
                     and peer.get('_activity_at', '') > task['created_at']]
            if newer:
                # A delayed callback needs fresh execution evidence before replacing newer worker progress.
                fresh = any(record.get('task_id') == task['id'] and record.get('state') == 'completed'
                            and (record.get('completed_at') or record.get('created_at', '')) > max(newer)
                            and (record.get('action', {}).get('name') in ('terminal', 'process', 'execute_code')
                                 or record.get('action', {}).get('name', '').startswith('mcp__'))
                            for record in self._native_task_receipts(task['id']))
                if not fresh:
                    return
        text = require_text(payload, 'activity', 80)
        state = payload.get('activity_state', 'working')
        if state not in ('working', 'waiting', 'ready', 'blocked') or '\n' in text:
            raise ServiceError(400, 'Use one short activity and a working, waiting, ready, or blocked state.')
        if not task.get('id'):
            raise ServiceError(400, 'Only the bot doing this task can report its activity.')
        self._apply_work_pause(artifact, task, payload, state)
        if state == 'working':
            self._bind_pr_progress(artifact, task)
        task['_work_progress_at'] = now()
        self._put('tasks', task)
        artifact.update(_activity=text, _activity_state=state, _activity_task_id=task['id'],
                        _activity_head=artifact.get('head_sha'), _activity_turn=task.get('_native_turn_id'), _activity_at=now())
        self._put('artifacts', artifact)
        self._event('artifact.updated', self._public(artifact), artifact['bot_id'], artifact.get('task_id'))

    def _link_pr_outcome(self, artifact, task, payload):
        from .service import ServiceError
        if artifact.get('kind') != 'pr':
            return
        peers = [artifact, *(p for p in self._all('artifacts') if p['id'] != artifact['id']
                  and p['kind'] == 'pr' and p.get('url', '').lower() == artifact['url'].lower())]
        existing = next((p['work_outcome_id'] for p in peers if p.get('work_outcome_id')), None)
        if 'work_id' in payload and payload['work_id'] is None:
            if task.get('id'):
                raise ServiceError(409, 'Only the authenticated client can remove a Work link.')
            for peer in peers:
                peer.pop('work_outcome_id', None)
                self._put('artifacts', peer)
                self._event('artifact.updated', self._public(peer), peer['bot_id'], peer.get('task_id'))
            return
        native = bool(task.get('_native_outcome_id') or task.get('_chat_session'))
        if native and not existing and not payload.get('work_id'):
            return
        inherited = bool(existing) and not payload.get('work_id')
        identity = payload.get('work_id') or existing or task.get('_work_context_id')
        if identity and identity.startswith('t_'):
            owner, _ = self._native_continuation_owner(identity)
            identity = owner.get('_work_context_id')
            if not identity:
                raise ServiceError(409, 'This native task does not have a Work outcome yet.')
        if not identity and task.get('id'):
            matches = {self._work_outcome_artifact(a)['id'] for a in self._all('artifacts')
                       if a['kind'] == 'work' and a.get('task_id') == task['id']}
            identity = next(iter(matches)) if len(matches) == 1 else None
        if not identity:
            return
        parent = self._work_outcome_artifact(self._get('artifacts', identity))
        if parent['kind'] != 'work':
            if 'work_id' in payload:
                raise ServiceError(409, 'Choose the Work outcome for this pull request.')
            return
        tasks = {t['id']: t for t in self._all('tasks')}
        reporter = task.get('id') or payload.get('review_task_id')
        context = self._get('artifacts', task['_work_context_id']) if task.get('_work_context_id') else None
        # The service validates this explicit context before creating the follow-up task.
        follows = context is not None and self._work_identity(context) == self._work_identity(parent)
        if not inherited and not follows and reporter not in self._work_report_chain(parent, tasks, self._pending_completion_receipts()):
            raise ServiceError(409, 'This task does not belong to that Work outcome.')
        for peer in peers:
            if peer.get('work_outcome_id') and self._work_identity(peer) != self._work_identity(parent):
                raise ServiceError(409, 'This pull request already belongs to another Work outcome.')
            if (self._work_identity(peer) != self._work_identity(parent)
                    and self._setting('work.cancelled.' + self._work_identity(peer))):
                raise ServiceError(409, 'Resume this pull request before linking it to another Work outcome.')
        for peer in peers:
            if peer.get('work_outcome_id') != parent['id']:
                peer['work_outcome_id'] = parent['id']
                self._put('artifacts', peer)
                self._event('artifact.updated', self._public(peer), peer['bot_id'], peer.get('task_id'))

    @staticmethod
    def _work_execution_owner(task, tasks):
        owner = tasks.get(task.get('_execution_session_id'), {})
        if (owner and (owner.get('_execution_session_id') or owner['id']) == owner['id']
                and owner.get('status') not in ('failed', 'cancelled')
                and (owner.get('conversation_bot_id') == task.get('conversation_bot_id')
                     or (task.get('_work_context_id') and not task.get('_work_status_only')))
                and all(owner.get(field) == task.get(field) for field in
                        ('bot_id', 'session_id', '_conversation_epoch'))):
            return owner
        return task

    @staticmethod
    def _continued_work_task(task, tasks):
        continued = tasks.get(task.get('_continued_task_id'))
        if (continued and task.get('_execution_session_id')
                and all(continued.get(key) == task.get(key) for key in (
                    '_execution_session_id', 'bot_id', 'session_id', 'thread_id', 'conversation_bot_id'))
                and (continued.get('_conversation_epoch') == task.get('_conversation_epoch')
                     or (continued['status'] == 'queued' and '_conversation_epoch' not in continued))):
            return continued
        return None

    @staticmethod
    def _work_links(tasks, records, include_continuations):
        linked = {}
        explicit_waits = {}
        for record in records:
            if record.get('_wait_task_id') and not record.get('_process_delivery'):
                explicit_waits.setdefault(record['task_id'], set()).add(record['_wait_task_id'])
            for key in ('_relay_task_id', '_wait_task_id'):
                if record.get(key) and not record.get('_process_delivery'):
                    linked.setdefault(record['task_id'], set()).add(record[key])
        for task in tasks.values():
            continued = WorkStatus._continued_work_task(task, tasks) if include_continuations else None
            if continued:
                linked.setdefault(task['id'], set()).add(continued['id'])
                explicit_waits.setdefault(task['id'], set()).add(continued['id'])
            if include_continuations and task['status'] not in ('failed', 'cancelled'):
                owner = WorkStatus._work_execution_owner(task, tasks)
                if owner['id'] != task['id']:
                    linked.setdefault(owner['id'], set()).add(task['id'])
                    explicit_waits.setdefault(owner['id'], set()).add(task['id'])
            if task.get('source_task_id') and ((task.get('_native_relay') and not task.get('_is_return'))
                    or (include_continuations and task.get('_is_return') and task.get('_assignment_task_id'))):
                linked.setdefault(task['source_task_id'], set()).add(task['id'])
            if include_continuations and task.get('_assignment_task_id'):
                linked.setdefault(task['_assignment_task_id'], set()).add(task['id'])
            for field in ('_requester_reply_task_id', '_relay_callback_owner'):
                if task.get(field):
                    linked.setdefault(task[field], set()).add(task['id'])
            linked.setdefault(task['id'], set()).update(task.get('_waiting_on', []))
        return linked, explicit_waits

    # The work graph builders read these tables: _work_links reads tasks and receipts, _proposal_work_links
    # reads decisions, and _link_external_work reads artifacts. A write to one of them drops the shared graphs.
    GRAPH_TABLES = frozenset(('tasks', 'native_receipts', 'decisions', 'artifacts'))

    def _graph_memo(self, records=None):
        shared = self._shared_cache()
        if shared is None:
            # A read scope sets _work_graphs. It does not write, so its graphs stay current.
            return getattr(self, '_work_graphs', None)
        # Inside _shared_reads, each _put drops these graphs. The shared receipt list repeats for the whole pass.
        # A card's own receipt list repeats only while Coconut checks that card, so one slot holds its graphs.
        if records is not None and records is not shared.get('pending receipts'):
            latest = shared.get('latest graphs')
            if latest is None or latest[0] is not records:
                latest = shared['latest graphs'] = (records, {})
            return latest[1]
        return shared.setdefault('graphs', {})

    @staticmethod
    def _proposal_execution(decision, source, target):
        """Whether a person's answer to this proposal of the source task started the target task."""
        return bool(source and target and decision.get('inbox_kind') == 'proposal'
                    and decision.get('task_id') == source['id'] and decision.get('_execution_task_id') == target['id']
                    and decision.get('decided_by')
                    and decision.get('status') in ('approved', 'modified', 'replied', 'executed', 'failed')
                    and decision.get('bot_id') == source['bot_id'] and target.get('parent_task_id') == source['id']
                    and all(source.get(k) == target.get(k) for k in ('bot_id', 'conversation_bot_id', 'session_id', 'thread_id')))

    def _proposal_work_links(self, tasks):
        import json
        graphs = self._graph_memo()
        key = ('proposals', id(tasks))
        if graphs is not None and key in graphs:
            return graphs[key][1]
        direct = {}
        rows = self.db.execute("SELECT data FROM decisions WHERE json_extract(data, '$.inbox_kind')='proposal' "
                               "AND json_extract(data, '$._execution_task_id') IS NOT NULL")
        for row in rows:
            decision = json.loads(row[0])
            source = tasks.get(decision.get('task_id'))
            target = tasks.get(decision.get('_execution_task_id'))
            if not self._proposal_execution(decision, source, target):
                continue
            direct.setdefault(source['id'], set()).add(target['id'])
        linked = {source: set(targets) for source, targets in direct.items()}
        for task in tasks.values():
            previous, seen, sources = task, set(), set()
            while previous and previous['id'] not in seen:
                seen.add(previous['id'])
                recovery = previous.get('_self_recovery') or {}
                sources.update(t['id'] for t in recovery.get('saved_tasks', []))
                identity = recovery.get('previous_task_id')
                previous = tasks.get(identity)
                if not previous or any(task.get(k) != previous.get(k) for k in ('bot_id', 'conversation_bot_id', 'session_id')):
                    break
                sources.add(identity)
            for source in sources:
                linked.setdefault(task['id'], set()).update(direct.get(source, set()))
        if graphs is not None:
            graphs[key] = tasks, linked
        return linked

    def _link_external_work(self, tasks, linked, explicit_waits):
        artifacts = {a['id']: a for a in self._all_shared('artifacts') if a['kind'] == 'work'}
        for item in artifacts.values():
            if item.get('_work_resuming') or not item.get('external_wait'):
                continue
            owner = tasks.get(item.get('task_id'), {})
            checking = tasks.get(item.get('_external_wait_task_id'), {})
            routine = tasks.get(checking.get('_assignment_task_id'), checking)
            edges = []
            if (owner and checking and checking['bot_id'] == item['bot_id']
                    and (routine.get('_native_job') or {}).get('id') == item['external_wait'].get('job_id')):
                edges.extend(((owner['id'], routine['id']), (owner['id'], checking['id'])))
            parent = artifacts.get(item.get('_work_parent_id'), {})
            parent_task = tasks.get(parent.get('task_id'), {})
            assignment = tasks.get(parent_task.get('_assignment_task_id'), parent_task)
            if (parent_task and owner.get('status') == 'completed' and item.get('_hidden')
                    and (item.get('_work_key') == parent.get('_work_key')
                         or item.get('_same_outcome_as') == parent.get('id'))
                    and (item.get('_work_parent_task_id') == assignment.get('id')
                         or (assignment.get('_self_recovery') and item.get('_work_parent_task_id') in parent.get('_work_previous_task_ids', [])))):
                edges.append((parent_task['id'], owner['id']))
            for source, target in edges:
                linked.setdefault(source, set()).add(target)
                explicit_waits.setdefault(source, set()).add(target)

    def _work_chain(self, root, tasks, records, *, include_continuations=False):
        """Follow recorded callbacks, including replies that assign further work."""
        graphs = self._graph_memo(records)
        key = (id(tasks), id(records), include_continuations)
        if graphs is not None and key in graphs:
            _, _, linked, explicit_waits = graphs[key]
        else:
            linked, explicit_waits = self._work_links(tasks, records, include_continuations)
            if include_continuations:
                for source, targets in self._proposal_work_links(tasks).items():
                    linked.setdefault(source, set()).update(targets)
                    explicit_waits.setdefault(source, set()).update(targets)
                self._link_external_work(tasks, linked, explicit_waits)
            if graphs is not None:
                # Keep inputs alive so Python cannot reuse their IDs within this read.
                graphs[key] = tasks, records, linked, explicit_waits
        found, pending = {}, deque([(root['id'], 0)])
        while pending:
            identity, depth = pending.popleft()
            if identity in found or identity not in tasks:
                continue
            task = tasks[identity]
            if not include_continuations and task.get('exchange_id') != root.get('exchange_id'):
                continue
            bot = self._get('bots', task['bot_id'])
            # _advance_input reserves a new turn ID before the queued turn starts.
            unstarted = task['status'] == 'queued' and '_conversation_epoch' not in task
            if (bot.get('_deleted') or task.get('session_id') != bot.get('session_id')
                    or (not unstarted and task.get('_conversation_epoch') != self._setting('conversation_epoch'))):
                continue
            found[identity] = (task, depth)
            for child in sorted(linked.get(identity, set())):
                if child == identity or child not in tasks:
                    continue
                # A human reply can change the parent's exchange while its saved wait remains valid.
                if (include_continuations and tasks[child].get('exchange_id') != task.get('exchange_id')
                        and child not in task.get('_waiting_on', [])
                        and child not in explicit_waits.get(identity, set())):
                    continue
                pending.append((child, depth + 1))
        return found

    def _work_report_roots(self, item, tasks, *, pr_heads=None):
        reporter = tasks.get(item.get('task_id'), {})
        if (item['kind'] == 'work' and item.get('_hidden') and item.get('_work_parent_id')
                and reporter.get('status') in ('failed', 'cancelled')):
            parent = self._work_outcome_artifact(item)
            parent_owner = tasks.get(parent.get('task_id'), {})
            parent_owner = tasks.get(parent_owner.get('_assignment_task_id'), parent_owner)
            if (item.get('_work_parent_task_id') in parent.get('_work_previous_task_ids', [])
                    and item.get('_work_parent_task_id') != parent_owner.get('id')
                    and not self._background_pending(reporter['id'])):
                return set()
        linked_pr = item['kind'] == 'pr' and bool(item.get('work_outcome_id'))
        if linked_pr:
            # The PR keeps its creation task for history; the current owner controls execution.
            reporter = (tasks.get(item.get('_review_task_id'))
                        or tasks.get(self._work_outcome_artifact(item).get('task_id'), {}))
        identities = {reporter.get('id'), reporter.get('_assignment_task_id')}
        if item['kind'] == 'pr':
            identities.add(item.get('_review_task_id'))
            head = pr_heads.get(item['url'].lower()) if pr_heads is not None else item.get('head_sha')
            if (item.get('_activity_head') == head
                    and (not linked_pr or item.get('_activity_at', '') >= reporter.get('created_at', ''))):
                identities.add(item.get('_activity_task_id'))
        if reporter.get('_work_context_id') == item['id']:
            # A discussion reports on the existing execution; it does not become its dependency.
            owner_id = self._work_execution_owner(reporter, tasks).get('id')
            identities.add(owner_id)
            for identity in item.get('_work_previous_task_ids', []):
                previous = tasks.get(identity, {})
                if (previous.get('_work_context_id') == item['id']
                        and self._work_execution_owner(previous, tasks).get('id') == owner_id):
                    identities.add(identity)
        return identities

    def _work_report_chain(self, item, tasks, records):
        linked = {}
        for identity in self._work_report_roots(item, tasks):
            if identity in tasks:
                linked.update(self._work_chain(tasks[identity], tasks, records, include_continuations=True))
        return linked

    def _tracked_work_input(self, task):
        tasks = self._task_map()
        records = self._shared_work_receipts()
        urls = set()
        for artifact in self._all('artifacts'):
            if artifact['kind'] != 'pr' or not self._pr_review_source_current(artifact):
                continue
            root = tasks[artifact['_review_task_id']]
            if task['id'] in self._work_chain(root, tasks, records):
                urls.add(artifact['url'])
        return sorted(urls)

    def _work_projection(self, artifacts):
        self._native_work_view(artifacts)
        for item in artifacts:
            item.pop('checkout_recovery_stopped', None)
            if (item['kind'] == 'work' and item.get('work_outcome_id') in (None, item['id'])
                    and self._work_identity(item) == 'work:' + item['id']
                    and self._setting('work.cancelled.work:' + item['id'])):
                item['checkout_recovery_stopped'] = True
        return artifacts

    def _tracked_pr_observations(self, task, urls):
        import json
        fields = ('url', 'head_sha', 'pr_state', 'draft', 'checks_state', 'required_checks_state',
                  'review_checks', 'check_failures', 'checked_at', 'refresh_error')
        artifacts = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM artifacts WHERE json_extract(data,'$.kind')='pr' ORDER BY rowid")]
        owners = {task['id']}
        if task.get('_is_return'):
            owners.update(filter(None, (task.get('source_task_id'), task.get('_resolved_dependency_id'))))
        related = {url.lower() for url in urls}
        sources = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM tasks WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(owners)),))]
        related.update(url for source in sources
                       for url in source.get('_reported_pr_urls', []))
        related.update(a['url'].lower() for a in artifacts
                       if owners.intersection((a.get('_review_task_id'), a.get('_activity_task_id')))
                       and self._pr_review_source_current(a))
        latest = {}
        timestamp = lambda a: (a.get('checked_at') or '', a.get('observed_at') or '', a['created_at'])
        for artifact in artifacts:
            url = artifact['url'].lower()
            if url not in related:
                continue
            if url not in latest or timestamp(artifact) > timestamp(latest[url]):
                latest[url] = artifact
        return [{key: artifact.get(key) for key in fields} for artifact in latest.values()]

    def _work_receipts(self):
        import json
        with self.lock:
            # Queue reads need unresolved actions and waits, not old tool output.
            records = {r['id']: r for r in self._pending_completion_receipts()}
            for row in self.db.execute("SELECT data FROM native_receipts WHERE json_extract(data, '$.state')='executing'"):
                record = json.loads(row[0])
                records[record['id']] = record
            return list(records.values())

    def _restore_active_work(self, artifact):
        if (artifact.get('pr_state') == 'open' and artifact.get('observed_at') and not artifact.get('refresh_error')
                and not self._setting('work.cancelled.' + self._work_identity(artifact))):
            identity = self._work_identity(artifact)
            key = 'work.dismissed.' + identity
            if (self._setting(key) and identity.startswith('work:')
                    and self._work_projection([dict(artifact)])[0]['work_state'] in ('completed', 'cancelled')):
                return
            self.db.execute('DELETE FROM settings WHERE key=?', (key,))

    def _work_outcome_artifact(self, item):
        # StateSync._retained_work_observations and NativeOutcomes._native_work_view read these links in SQLite first.
        # A new link field must go there too.
        if item.get('work_outcome_id'):
            item = self._get('artifacts', item['work_outcome_id'])
        item = self._canonical_work(item)
        seen = set()
        while item['id'] not in seen:
            link = item.get('_same_outcome_as') or (item.get('_work_parent_id') if item.get('_hidden') else None)
            if not link:
                break
            seen.add(item['id'])
            parent = self._canonical_work(self._get('artifacts', link))
            if parent['kind'] != 'work':
                break
            item = parent
        return item

    def _work_identity(self, item):
        outcome = self._work_outcome_artifact(item)
        return 'work:' + outcome['id'] if outcome['kind'] == 'work' else outcome['url'].lower()

    def _cancel_work(self, artifact_id, callbacks):
        from .service import ServiceError, encoded, now
        item = self._work_outcome_artifact(self._get('artifacts', artifact_id))
        if item['kind'] not in ('work', 'pr'):
            raise ServiceError(400, 'This item does not appear in Work.')
        identity = self._work_identity(item)
        if self._setting('work.cancelled.' + identity):
            self.db.execute('UPDATE settings SET value=? WHERE key=?',
                            (encoded(str(uuid4())), 'work.cancelled.' + identity))
            peers = [a for a in self._all('artifacts') if a['kind'] in ('pr', 'work')
                     and self._work_identity(a) == identity]
            return dict(ok=True, artifacts=[self._public(a) for a in self._work_projection(peers) if not a.get('_hidden')], cursor=self._cursor())
        projected = self._work_projection([dict(item)])[0]
        if projected['work_state'] in ('completed', 'cancelled'):
            raise ServiceError(409, 'This work has already finished.')
        tasks = {t['id']: t for t in self._all('tasks')}
        peers = [a for a in self._all('artifacts') if a['kind'] in ('pr', 'work')
                 and self._work_identity(a) == identity]
        owned = {p.get(field) for p in peers for field in
                 (('task_id',) if p['kind'] == 'work' else ('_review_task_id', '_activity_task_id'))} & tasks.keys()
        checks = {a.get('_external_wait_task_id') for a in self._all('artifacts')
                  if a['kind'] == 'work' and (self._work_identity(a) == identity or a.get('_work_parent_id') == item['id'])}
        owned |= checks & tasks.keys()
        owned |= {t['id'] for t in tasks.values() if t.get('_work_context_id') in {p['id'] for p in peers}}
        owned |= {tasks[i]['_assignment_task_id'] for i in list(owned)
                  if tasks[i].get('_assignment_task_id') in tasks}
        proposals = self._proposal_work_links(tasks)
        while True:
            # Wait targets can belong to other work. Only follow owned attempts.
            expanded = owned | {t['id'] for t in tasks.values()
                if any(t.get(field) in owned for field in ('source_task_id', '_assignment_task_id',
                                                          '_requester_reply_task_id', '_relay_callback_owner'))}
            expanded |= {target for source in owned for target in proposals.get(source, set())}
            expanded |= {continued['id'] for source in owned
                         if (continued := self._continued_work_task(tasks[source], tasks))}
            if expanded == owned:
                break
            owned = expanded
        self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        ('work.cancelled.' + identity, encoded(now())))
        others = [a for a in self._all('artifacts') if a['kind'] in ('work', 'pr') and not a.get('_hidden')
                  and self._work_identity(a) != identity
                  and not self._setting('work.cancelled.' + self._work_identity(a))]
        # Native intake routines serve other sources too. Steer the cancelled outcome only.
        shared = {i for i in checks & tasks.keys() if self._assignment(tasks[i]).get('_native_job')}
        shared |= {self._assignment(tasks[i])['id'] for i in list(shared)}
        records = self._pending_completion_receipts()
        for routine_task_id in list(shared):
            shared.update(self._work_chain(tasks[routine_task_id], tasks, records, include_continuations=True))
        for other in self._work_projection(others):
            if other['work_state'] in ('completed', 'cancelled'):
                continue
            for field in ('task_id', '_review_task_id', '_activity_task_id', '_external_wait_task_id'):
                root = tasks.get(other.get(field))
                if root:
                    shared.update(self._work_chain(root, tasks, records, include_continuations=True))
                    root = tasks.get(root.get('_assignment_task_id'), root)
                    shared.update(self._work_chain(root, tasks, records, include_continuations=True))
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
            exact = bool(url) and any(p['kind'] == 'pr' and p['url'].lower() == url for p in peers)
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
        projected = self._work_projection([dict(peer) for peer in peers])
        for peer in projected:
            self._event('artifact.updated', peer, peer['bot_id'], peer.get('task_id'))
        return dict(ok=True, artifacts=[self._public(peer) for peer in projected if not peer.get('_hidden')], cursor=self._cursor())

    def _resume_pr_reviews(self, item, task, *, reset_heads=True):
        from .service import uid
        groups = {}
        for peer in self._all('artifacts'):
            if peer['kind'] == 'pr' and self._work_identity(peer) == self._work_identity(item):
                groups.setdefault(peer['url'].lower(), []).append(peer)
        for peers in groups.values():
            selected = next((p for p in peers if p['bot_id'] == task['bot_id']), None)
            if selected is None:
                source = max(peers, key=lambda p: p.get('observed_at') or p['created_at'])
                selected = dict(source, id=uid(), bot_id=task['bot_id'], task_id=task['id'], _review_owners=[], _review_task_id=None,
                                _review_history_unknown=False)
                peers.append(selected)
                self._put('artifacts', selected)
                self._event('artifact.created', self._public(selected), task['bot_id'], task['id'])
            for peer in peers:
                self._set_review_owner(peer, task['id'] if peer['id'] == selected['id'] else None)
                if reset_heads:
                    peer['_reviewed_heads'] = []
                    peer.pop('_reviewed_head', None)
                self._put('artifacts', peer)

    def _resume_work(self, artifact_id):
        from .service import ServiceError, now
        item = self._work_outcome_artifact(self._get('artifacts', artifact_id))
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
            self._put('artifacts', item)
        self._resume_pr_reviews(item, task)
        item = self._get('artifacts', item['id'])
        self._event('artifact.updated', self._work_projection([dict(item)])[0], item['bot_id'], task['id'])
        return {'ok': True}

    def _dismiss_work(self, artifact_id, data):
        from .service import ServiceError, encoded, require_text
        artifact = self._work_outcome_artifact(self._get('artifacts', artifact_id))
        if artifact['kind'] not in ('pr', 'work'):
            raise ServiceError(400, 'This item does not appear in Work.')
        identity = self._work_identity(artifact)
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
