"""Present PR activity from bot reports and current execution evidence."""
import json
from datetime import datetime, timezone
from collections import deque

PAUSE_DECISION_STATES = ('approved', 'rejected', 'modified', 'replied', 'executed', 'failed')


# The condition of the partial index decisions_proposal_execution: a proposal that started an execution task.
PROPOSAL_EXECUTION = ("json_extract(data, '$.inbox_kind')='proposal' "
                      "AND json_extract(data, '$._execution_task_id') IS NOT NULL")


class _UnionLinks:
    """The union of two maps of ID sets, read like one dict. Neither map changes."""
    __slots__ = ('first', 'second')

    def __init__(self, first, second):
        self.first, self.second = first, second

    def get(self, key, default=None):
        first, second = self.first.get(key), self.second.get(key)
        if first is None and second is None:
            return default
        if first is None or second is None:
            return first if second is None else second
        return first | second

    def items(self):
        for key in self.first.keys() | self.second.keys():
            yield key, self.get(key)


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
        if decisions is None:
            with self.lock:
                decisions = [json.loads(row[0]) for row in self.db.execute(
                    'SELECT data FROM decisions WHERE id IN (SELECT value FROM json_each(?)) ORDER BY rowid',
                    (json.dumps(sorted(identities - {None})),))]
        return next((d for d in decisions
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

    def _direction_roots(self, task):
        """The task and its recorded parents, sources, and assignments, by ID. A person's direction there counts."""
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
        return roots, tasks

    def _work_has_new_direction(self, task, pause, item):
        roots, tasks = self._direction_roots(task)
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
        if task.get('_technical_lane') and self._technical_declined_turn(task):
            # The requested lane turn of a declined or held merge runs with its holds. The pause stays. See technical_lane.py.
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
            raise ServiceError(400, 'Only the agent doing this task can report its activity.')
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
    def _work_record_links(records):
        """The links and explicit waits that the receipts record."""
        linked = {}
        explicit_waits = {}
        for record in records:
            if record.get('_wait_task_id') and not record.get('_process_delivery'):
                explicit_waits.setdefault(record['task_id'], set()).add(record['_wait_task_id'])
            for key in ('_relay_task_id', '_wait_task_id'):
                if record.get(key) and not record.get('_process_delivery'):
                    linked.setdefault(record['task_id'], set()).add(record[key])
        return linked, explicit_waits

    @staticmethod
    def _work_task_link_pairs(tasks):
        """The task links without continuations (False) and with them (True), from one pass over the tasks."""
        plain, plain_waits, linked, explicit_waits = {}, {}, {}, {}

        def link(source, target, *, continuation=False):
            linked.setdefault(source, set()).add(target)
            if not continuation:
                plain.setdefault(source, set()).add(target)

        for task in tasks.values():
            continued = WorkStatus._continued_work_task(task, tasks)
            if continued:
                link(task['id'], continued['id'], continuation=True)
                explicit_waits.setdefault(task['id'], set()).add(continued['id'])
            if task['status'] not in ('failed', 'cancelled'):
                owner = WorkStatus._work_execution_owner(task, tasks)
                if owner['id'] != task['id']:
                    link(owner['id'], task['id'], continuation=True)
                    explicit_waits.setdefault(owner['id'], set()).add(task['id'])
            if task.get('source_task_id'):
                # The intake of another bot's message: a message of coconut_message_bot (_native_intake), or the
                # native relay of an earlier worker.
                if (task.get('_native_relay') or task.get('_native_intake')) and not task.get('_is_return'):
                    link(task['source_task_id'], task['id'])
                elif task.get('_is_return') and task.get('_assignment_task_id'):
                    link(task['source_task_id'], task['id'], continuation=True)
            if task.get('_assignment_task_id'):
                link(task['_assignment_task_id'], task['id'], continuation=True)
            for field in ('_requester_reply_task_id', '_relay_callback_owner'):
                if task.get(field):
                    link(task[field], task['id'])
            for mapping in (linked, plain):
                mapping.setdefault(task['id'], set()).update(task.get('_waiting_on', []))
        return {False: (plain, plain_waits), True: (linked, explicit_waits)}

    # The work graph builders read these tables: _work_task_link_pairs reads tasks, _work_record_links reads receipts,
    # _proposal_work_links reads decisions, and _link_external_work reads artifacts. A write to one of them drops the
    # shared graphs.
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
        # The Kanban pass builds this graph on each pass. The partial index reads the few executed proposals, not every
        # card. On the Mac mini, under memory pressure, the scan of every card read the disk under the service lock.
        rows = self.db.execute('SELECT data FROM decisions INDEXED BY decisions_proposal_execution WHERE ' + PROPOSAL_EXECUTION)
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

    def _work_graph(self, tasks, records, include_continuations):
        """The links and explicit waits of the work graph.

        The links of the tasks, the proposals and the external waits do not depend on the receipts. One read block
        builds them once for each task map, and each receipt list adds only its own links. A turn context reads two
        receipt lists, so it built the whole graph three times: about 0.27 s of each turn on a copy of the Mac mini data.
        """
        shared = self._graph_memo()
        task_key = ('task links', id(tasks))
        if shared is not None and task_key in shared:
            pairs = shared[task_key][1]
        else:
            # One pass builds the links with and without continuations. A turn context reads both.
            pairs = self._work_task_link_pairs(tasks)
            linked, explicit_waits = pairs[True]
            for source, targets in self._proposal_work_links(tasks).items():
                linked.setdefault(source, set()).update(targets)
                explicit_waits.setdefault(source, set()).update(targets)
            self._link_external_work(tasks, linked, explicit_waits)
            if shared is not None:
                # Keep the map alive so Python cannot reuse its ID within this read.
                shared[task_key] = tasks, pairs
        task_links = pairs[include_continuations]
        graphs = self._graph_memo(records)
        key = (id(tasks), id(records), include_continuations)
        if graphs is not None and key in graphs:
            return graphs[key][2], graphs[key][3]
        record_links = self._work_record_links(records)
        linked, explicit_waits = (_UnionLinks(task_links[0], record_links[0]), _UnionLinks(task_links[1], record_links[1]))
        if graphs is not None:
            # Keep inputs alive so Python cannot reuse their IDs within this read.
            graphs[key] = tasks, records, linked, explicit_waits
        return linked, explicit_waits

    def _work_node_current(self, task):
        """Whether a work chain may pass through this task: its bot, session and conversation are current."""
        bot = self._get('bots', task['bot_id'])
        # _advance_input reserves a new turn ID before the queued turn starts.
        unstarted = task['status'] == 'queued' and '_conversation_epoch' not in task
        return not (bot.get('_deleted') or task.get('session_id') != bot.get('session_id')
                    or (not unstarted and task.get('_conversation_epoch') != self._setting('conversation_epoch')))

    @staticmethod
    def _work_edge_current(parent, child, explicit_waits, include_continuations):
        # A human reply can change the parent's exchange while its saved wait remains valid.
        return not (include_continuations and child.get('exchange_id') != parent.get('exchange_id')
                    and child['id'] not in parent.get('_waiting_on', [])
                    and child['id'] not in explicit_waits.get(parent['id'], set()))

    def _work_chain(self, root, tasks, records, *, include_continuations=False):
        """Follow recorded callbacks, including replies that assign further work."""
        linked, explicit_waits = self._work_graph(tasks, records, include_continuations)
        found, pending = {}, deque([(root['id'], 0)])
        while pending:
            identity, depth = pending.popleft()
            if identity in found or identity not in tasks:
                continue
            task = tasks[identity]
            if not include_continuations and task.get('exchange_id') != root.get('exchange_id'):
                continue
            if not self._work_node_current(task):
                continue
            found[identity] = (task, depth)
            for child in sorted(linked.get(identity, set())):
                if child == identity or child not in tasks:
                    continue
                if not self._work_edge_current(task, tasks[child], explicit_waits, include_continuations):
                    continue
                pending.append((child, depth + 1))
        return found

    @staticmethod
    def _work_parents(linked):
        """{child: set of parents} of a link map, without links of a task to itself."""
        parents = {}
        for parent, children in linked.items():
            for child in children:
                if child != parent:
                    parents.setdefault(child, set()).add(parent)
        return parents

    def _work_reach(self, target_id, tasks, records, *, include_continuations=False):
        """The IDs of the roots whose _work_chain holds the target, by the same rules followed backwards.

        A context reader asked "is this task in the chain of that root?" for each artifact or peer, and each question
        walked a chain. On the Mac mini copy, one turn context walked about 2,000 chains. One backward walk answers all
        of them. The node and edge rules of _work_chain depend only on the node or the edge, so the set of roots does
        not depend on the order of the walk. Without continuations every node of a chain shares the root's exchange,
        so a backward walk keeps the target's exchange.
        """
        graphs = self._graph_memo(records)
        key = ('reach', target_id, id(tasks), id(records), include_continuations)
        if graphs is not None and key in graphs:
            return graphs[key][-1]
        linked, explicit_waits = self._work_graph(tasks, records, include_continuations)
        parents_key = ('parents', id(tasks), id(records), include_continuations)
        parents = graphs.get(parents_key, (None, None, None))[-1] if graphs is not None else None
        if parents is None:
            # The parents of the task links do not depend on the receipts. Keep them with the task links.
            shared = self._graph_memo()
            task_key = ('task parents', id(tasks), include_continuations)
            if shared is not None and task_key in shared:
                task_parents = shared[task_key][1]
            else:
                task_parents = self._work_parents(linked.first)
                if shared is not None:
                    shared[task_key] = tasks, task_parents
            parents = _UnionLinks(task_parents, self._work_parents(linked.second))
            if graphs is not None:
                graphs[parents_key] = tasks, records, parents
        target = tasks.get(target_id)
        reached = set()
        if target is not None and self._work_node_current(target):
            reached.add(target_id)
            pending = [target_id]
            while pending:
                child = tasks[pending.pop()]
                for identity in parents.get(child['id'], ()):
                    if identity in reached or identity not in tasks:
                        continue
                    parent = tasks[identity]
                    if not include_continuations and parent.get('exchange_id') != target.get('exchange_id'):
                        continue
                    if (not self._work_edge_current(parent, child, explicit_waits, include_continuations)
                            or not self._work_node_current(parent)):
                        continue
                    reached.add(identity)
                    pending.append(identity)
        if graphs is not None:
            graphs[key] = tasks, records, reached
        return reached

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
        reach = self._work_reach(task['id'], tasks, records)
        for artifact in self._all('artifacts'):
            # Ask the cheap question first: only a PR whose review task reaches this task can count. The ownership
            # check reads several rows for each PR.
            if (artifact['kind'] == 'pr' and artifact.get('_review_task_id') in reach
                    and self._pr_review_source_current(artifact)):
                urls.add(artifact['url'])
        return sorted(urls)

    def _work_projection(self, artifacts):
        return self._native_work_view(artifacts)

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
