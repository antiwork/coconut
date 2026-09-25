"""Keep native kanban outcomes separate from phone conversation turns."""
import json
import threading
import time
from datetime import datetime, timezone
from uuid import UUID, uuid5


def outcome_owner_id(origin_id, native_id):
    return str(uuid5(UUID(origin_id), 'kanban:' + native_id))


CHAT_INSTRUCTIONS = (
    'Reply directly to this conversation’s sender. '
    'For work with an outcome beyond this reply, create one native kanban task before starting it. '
    'Use kanban_create with a clear title, the complete request, scope, approval limits, and acceptance criteria in body. '
    'Coconut supplies the bot, dispatch state, and a stable creation key. Hermes gives each outcome its own workspace. '
    'Use kanban_list and kanban_show to find existing work before creating another task. '
    'For an existing outcome, add new facts with kanban_comment. Do not create a second task for a retry or status question. '
    'A task comment is context, not new permission. Preserve human decisions. '
    'Create a separate native outcome for new work after a completed PR. Keep the earlier PR as context. '
    'Use coconut_ask only for a concrete decision. Ordinary local setup and planning need no card. '
)
WORK_INSTRUCTIONS = (
    'The native kanban task below owns this outcome. Its comments and previous attempts are the work record. '
    'Use kanban_heartbeat for a short progress description at phase changes. '
    'Use kanban_complete only after verifying every accepted outcome. A reply or plan does not complete the task. '
    'A question about a decision does not complete its unfinished outcome. Answer it and retain the remaining work. '
    'If a decision is still needed, create an updated card with the concrete next action. '
    'Use coconut_ask when a human decision is required. Coconut connects the card to this same native task. '
    'Use coconut_wait for an existing bot result. Finish the turn only after the wait succeeds. '
    'Before repeating an action from an earlier attempt, check its receipt and destination. '
    'An absent result does not prove the action failed. Preserve approval limits and unresolved effects. '
    'Use a native dependency or scheduled check for a valid external wait. Do not create a recovery task. '
)

CONTINUE_SCHEMA = {
    'name': 'coconut_continue_work',
    'description': 'Continue a quiescent queued, scheduled, review, blocked, or completed Hermes outcome with new facts. Use the native task ID from kanban_show. '
        'You must own the work or be its requesting bot. This keeps its task, workspace, decisions, and result route. '
        'Hermes preserves completed results and creates a linked follow-up with the same workspace. '
        'This does not approve actions. The result goes to the original requester.',
    'parameters': {'type': 'object', 'additionalProperties': False,
        'properties': {'task_id': {'type': 'string'}, 'message': {'type': 'string', 'maxLength': 16000}},
        'required': ['task_id', 'message']},
}


class _NativeSnapshotsNeeded(Exception):
    def __init__(self, identities):
        self.identities = identities


class NativeOutcomes:
    def _retain_native_work(self, owner, native, snapshot_missing=None, snapshots=None, caller=None):
        from .service import ServiceError, now
        from .work_capacity import execution_key
        snapshots = self.kanban.tasks if snapshots is None else snapshots
        if owner.get('_work_context_id'):
            items = [self._get('artifacts', owner['_work_context_id'])]
        else:
            items = [a for a in self._all('artifacts') if a['kind'] in ('work', 'pr') and a['task_id'] == owner['id']]
        linked_parent = None
        audited_parent = None
        retained = []
        source = self._get('tasks', owner['source_task_id']) if owner.get('source_task_id') else None
        pending_decisions = {d['task_id'] for d in self._all('decisions') if d['status'] == 'pending'}
        open_contexts = {c['task_id'] for c in self._all('contexts') if c['status'] in ('queued', 'working')}

        def quiet(task):
            # Every live signal that the writer guard reads. A failed task can still hold a running tool.
            worker = self.running.get(task['id'])
            bound = task.get('_native_outcome_id') or (task.get('_kanban_claim') or {}).get('native_id')
            card = snapshots.get(task['id'])
            # Only a done, archived, or blocked card cannot start a writer by itself. A missing card counts as live.
            # A live branch stays in the guard, and the guard requests its missing card with the others in one batch.
            native_live = bool(bound) and (card is None or card.get('id') != bound
                                           or card.get('status') not in ('done', 'archived', 'blocked')
                                           or card.get('current_run_id') or card.get('worker_pid'))
            return not (task.get('_waiting_on') or task.get('_return_pending') or (worker and worker.is_alive())
                        or self._background_pending(task['id']) or self._native_task_receipts(task['id'], state='executing')
                        or native_live or task['id'] in pending_decisions or task['id'] in open_contexts)
        # The audit path skips the audit's own writer checks, so the audit must have finished.
        if (source and snapshot_missing and source['id'] not in snapshots
                and (source.get('_native_outcome_id') or (source.get('_kanban_claim') or {}).get('native_id'))):
            snapshot_missing([source['id']])
        audit_finished = bool(source and source['status'] == 'completed' and quiet(source) and not self._unfinished_work(source))
        for candidate in items:
            root = self._work_outcome_artifact(candidate)
            # A requester's source audit can delegate for an existing Work outcome of that requester.
            # _source_native_ids lists the native outcomes that the audit commented on. That alone does not
            # prove a delegation, so the Work's own task must also make this call.
            audited = bool(audit_finished and caller and caller['id'] == root['task_id'] and root.get('_native_outcome_id')
                           and root['_native_outcome_id'] in (source.get('_source_native_ids') or ()))
            if (root['bot_id'] != owner['bot_id'] and candidate['kind'] == 'pr'
                    and candidate['bot_id'] == owner['bot_id'] and candidate['task_id'] == owner['id']
                    and candidate.get('work_outcome_id') == root['id'] and root['kind'] == 'work'
                    and root['bot_id'] == owner.get('source_bot_id')
                    and (root['task_id'] == owner.get('source_task_id') or audited)):
                # A delegated PR shares the requester's Work presentation, not its execution.
                linked_parent = root
                if audited:
                    audited_parent = root
                retained.append(candidate)
            else:
                retained.append(root)
        items = list({a['id']: a for a in retained}.values())
        if len(items) > 1:
            raise ServiceError(409, 'Choose one retained Work item for this native outcome.')
        if items:
            item = items[0]
            if (item['kind'] not in ('work', 'pr') or item['bot_id'] != owner['bot_id']
                    or item.get('_native_outcome_id') not in (None, native['id'])):
                raise ServiceError(409, 'The retained Work item belongs to another native outcome.')
            peers = self._work_pause_peers(item)
            if self._setting('work.cancelled.' + self._work_identity(item)) or self._work_pause(item, peers=peers):
                raise ServiceError(409, 'This work is paused or cancelled.')
            tasks = {t['id']: t for t in self._all('tasks')}
            records = self._pending_completion_receipts()
            peer_chains = []
            # The requester's coordinating task can differ from the task that delegated this work.
            requesters = {owner.get('source_task_id')} | ({linked_parent['task_id']} if linked_parent else set())
            requesters.discard(None)
            # Only the caller is not a competing writer. When the coordinator calls for its audit, the audit stays guarded.
            callers = ({linked_parent['task_id']} if linked_parent and linked_parent['task_id'] != owner.get('source_task_id')
                       else {owner.get('source_task_id')})
            if audited_parent:
                # PR report fields are optional. Check the audited requester branches
                # even when every report points back to the owner under continuation.
                linked = {}
                for identity in requesters:
                    linked.update({key: value for key, value in self._work_chain(
                        tasks[identity], tasks, records, include_continuations=True).items()
                        if value[0]['status'] in self.ACTIVE or not quiet(value[0])
                        or (value[0]['status'] == 'completed' and self._unfinished_work(value[0]))})
                for identity in (*callers, owner['id']):
                    linked.pop(identity, None)
                native_tasks = [t for t, _ in linked.values()
                                if t.get('_native_outcome_id') or t.get('_kanban_claim')]
                peer_chains.append((item, source, linked, native_tasks, True))
            for peer in peers:
                if linked_parent and peer['id'] == linked_parent['id']:
                    # The coordinating Work may remain active while its delegated PR resumes.
                    # Other PRs linked to it remain independent writer/review peers.
                    continue
                requester_report = (item['kind'] == 'pr' and not item.get('work_outcome_id')
                    and peer['kind'] == 'pr' and peer['bot_id'] == owner.get('source_bot_id')
                    and peer['task_id'] == owner.get('source_task_id')
                    and peer.get('url') and item.get('url')
                    and peer['url'].lower() == item['url'].lower())
                for field in ('_review_task_id', '_activity_task_id'):
                    if not peer.get(field):
                        continue
                    if (field == '_review_task_id' and peer['kind'] == 'pr'
                            and ((linked_parent and peer['bot_id'] == linked_parent['bot_id']
                                  and peer.get('work_outcome_id') == linked_parent['id']
                                  and peer.get('url') and item.get('url')
                                  and peer['url'].lower() == item['url'].lower()
                                  and peer['task_id'] == linked_parent['task_id']
                                  and peer[field] == linked_parent['task_id'])
                                 or (requester_report and peer[field] == owner['source_task_id']))):
                        # The requester's own same-PR report is not a second writer.
                        continue
                    reporter = tasks[peer[field]]
                    if execution_key(reporter) == execution_key(owner):
                        continue
                    requester_activity = (requester_report and
                        self._work_execution_owner(reporter, tasks)['id'] == owner.get('source_task_id'))
                    reporter_source = tasks.get(reporter.get('source_task_id'))
                    owner_report = (peer['id'] == item['id'] and peer['task_id'] == owner['id']
                        and reporter['bot_id'] == owner['bot_id']
                        and reporter.get('_reply_to_task_id') in requesters
                        and reporter_source is not None
                        and self._work_execution_owner(reporter_source, tasks)['id'] in requesters)
                    passive_report = (field == '_activity_task_id' and peer['kind'] == 'pr'
                        and peer.get('url') and item.get('url')
                        and peer['url'].lower() == item['url'].lower()
                        and (requester_activity or owner_report))
                    # A passive same-PR report belongs to the original
                    # requester, not to an independent writer. Keep all of
                    # that requester's other branches in the guard.
                    root = tasks[owner['source_task_id']] if passive_report else self._work_execution_owner(reporter, tasks)
                    linked = self._work_chain(root, tasks, records, include_continuations=True)
                    if passive_report:
                        # The coordinating requester task has its own branches. Keep its live ones in the guard too.
                        # A failed or cancelled branch with no live signal cannot write. Its unfinished work items
                        # are expected. A completed branch with unfinished work items stays guarded.
                        for identity in requesters - {root['id']}:
                            if identity in tasks:
                                linked.update({key: value for key, value in self._work_chain(
                                    tasks[identity], tasks, records, include_continuations=True).items()
                                    if value[0]['status'] in self.ACTIVE or not quiet(value[0])
                                    or (value[0]['status'] == 'completed' and self._unfinished_work(value[0]))})
                    linked[root['id']] = (root, 0)
                    linked[reporter['id']] = (reporter, 0)
                    if owner_report and reporter_source and reporter_source['id'] not in (owner['id'], *requesters):
                        linked[reporter_source['id']] = (reporter_source, 0)
                    if passive_report:
                        # The coordinator and the owner under continuation
                        # are not competing writers. Their other descendants
                        # still need terminal claims, returns, and decisions.
                        for identity in (*callers, owner['id']):
                            linked.pop(identity, None)
                    native_tasks = [t for t, _ in linked.values()
                                    if t.get('_native_outcome_id') or t.get('_kanban_claim')]
                    peer_chains.append((peer, reporter, linked, native_tasks, passive_report))
            missing = sorted({t['id'] for _, _, _, native_tasks, _ in peer_chains for t in native_tasks
                              if t['id'] not in snapshots})
            if missing and snapshot_missing:
                snapshot_missing(missing)
            for peer, reporter, linked, native_tasks, passive_report in peer_chains:
                native_pending = any(
                    (snapshot := snapshots.get(t['id'], {})).get('status') != 'done'
                    or snapshot.get('id') != (t.get('_native_outcome_id') or t['_kanban_claim'].get('native_id'))
                    or snapshot.get('current_run_id') or snapshot.get('worker_pid') for t in native_tasks)
                if (native_pending or peer['kind'] != 'pr'
                        or ((peer['bot_id'] == owner['bot_id'] or reporter['bot_id'] == owner['bot_id'])
                            and not passive_report)
                        or any(t['status'] != 'completed' or t.get('_waiting_on') or t.get('_return_pending')
                               or self._task_paused(t) or self._get('exchanges', t['exchange_id']).get('cancelled')
                               or self._background_pending(t['id']) or self._unfinished_work(t)
                               or (self.running.get(t['id']) and self.running[t['id']].is_alive())
                               or self._native_task_receipts(t['id'], state='executing')
                               for t, _ in linked.values())
                        or any(d['status'] == 'pending' and d['task_id'] in linked for d in self._all('decisions'))
                        or any(c['status'] in ('queued', 'working') and c['task_id'] in linked
                               for c in self._all('contexts'))):
                    raise ServiceError(409, 'This Work item has another execution. Continue its current owner.')
            item['_native_outcome_id'] = native['id']
        else:
            title = native.get('title') or owner['title']
            item = dict(id=str(uuid5(UUID(owner['id']), 'work')), bot_id=owner['bot_id'],
                task_id=owner['id'], kind='work', title=title, status='queued', activity='Queued',
                preview='', url=None, created_at=now(), observed_at=now(),
                _work_key='native-' + native['id'], _native_outcome_id=native['id'],
                completion_condition=title)
        owner['_work_context_id'] = item['id']
        self._put('artifacts', item)
        self._event('artifact.updated' if items else 'artifact.created', self._public(item), owner['bot_id'], owner['id'])

    def _native_continuation_owner(self, identity, snapshot_missing=None, snapshots=None):
        """Resolve the caller's native ID through Coconut's durable owner mapping."""
        from .service import ServiceError
        if not self.kanban:
            raise ServiceError(409, 'Choose one existing native outcome from kanban_show.')
        snapshots = dict(self.kanban.tasks) if snapshots is None else snapshots
        contexts = [context for context in self._all('contexts') if identity in (
            (context.get('_kanban_claim') or {}).get('native_id'),
            snapshots.get(context['id'], {}).get('id'))]
        if contexts:
            if len(contexts) != 1 or contexts[0]['status'] != 'completed':
                raise ServiceError(409, 'Wait for the existing context task to finish.')
            context = contexts[0]
            if snapshot_missing:
                snapshot_missing([context['id']])
            else:
                result = self.kanban.call({'op': 'snapshot', 'external_ids': [context['id']]})
                snapshots = {t['external_id']: t for t in result['tasks']}
            context_native = snapshots.get(context['id'], {})
            if context_native.get('id') != identity:
                raise ServiceError(409, 'The saved Coconut and native context mapping does not match.')
            if (context_native.get('status') not in ('done', 'archived')
                    or context_native.get('current_run_id') or context_native.get('worker_pid')):
                raise ServiceError(409, 'Wait for the existing context task to finish.')
            parent = self._get('tasks', context['task_id'])
            if parent['bot_id'] != context['bot_id']:
                raise ServiceError(409, 'The context task does not match its owner.')
            # A context job shares its parent's execution and is not a new outcome.
            identity = parent.get('_native_outcome_id') or (parent.get('_kanban_claim') or {}).get('native_id')
            if not identity:
                raise ServiceError(409, 'The context task has no retained native outcome.')
        owners = self._all('tasks')
        missing = [owner['id'] for owner in owners if owner['id'] not in snapshots
            and identity in (owner.get('_native_outcome_id'), (owner.get('_kanban_claim') or {}).get('native_id'))]
        if missing:
            # Periodic snapshots omit older terminal bindings. Continuation
            # requests defer this lookup until their transaction has rolled back.
            if snapshot_missing:
                snapshot_missing(missing)
            else:
                result = self.kanban.call({'op': 'snapshot', 'external_ids': missing})
                snapshots = {t['external_id']: t for t in result['tasks']}
        candidates = []
        mismatch = False
        for owner in owners:
            explicit = owner.get('_native_outcome_id')
            claimed = (owner.get('_kanban_claim') or {}).get('native_id')
            native = snapshots.get(owner['id'], {})
            mapped = native.get('id')
            if identity not in {value for value in (explicit, claimed, mapped) if value}:
                continue
            if explicit:
                # A successor keeps the previous run's claim until it starts.
                if explicit != identity or (mapped and mapped != identity):
                    mismatch = True
                elif mapped == identity:
                    candidates.append((owner, native))
                continue
            if claimed:
                if claimed != identity or (mapped and mapped != identity):
                    mismatch = True
                elif mapped == identity and owner['_kanban_claim'].get('input'):
                    # Older inputs can finish before native outcome ownership
                    # is retained. The matching claim also identifies their input.
                    candidates.append((owner, native))
        if mismatch:
            raise ServiceError(409, 'The saved Coconut and native outcome mapping does not match.')
        if len(candidates) != 1:
            raise ServiceError(409, 'Choose one existing native outcome from kanban_show.')
        return candidates[0]

    def _native_continuation_item(self, task):
        from .service import ServiceError
        if task.get('_work_context_id'):
            return self._get('artifacts', task['_work_context_id'])
        # _send retains ownership history before moving the artifact; native
        # reconciliation then replaces its native ID with the successor's ID.
        # Neither mutable ID alone can resolve a reply to the original owner.
        tasks = {t['id']: t for t in self._all('tasks')}
        candidates = []
        for item in self._all('artifacts'):
            if item['kind'] not in ('work', 'pr') or item['bot_id'] != task['bot_id']:
                continue
            if item['task_id'] == task['id']:
                if item.get('_native_outcome_id') == task['_native_outcome_id']:
                    candidates.append(item)
                continue
            owner = tasks.get(item['task_id'], {})
            history = [item.get('_work_origin_task_id'), *item.get('_work_previous_task_ids', [])]
            if (task['id'] in history and owner.get('_work_context_id') == item['id']
                    and owner.get('bot_id') == task['bot_id']
                    and owner.get('conversation_bot_id') == task['conversation_bot_id']
                    and owner.get('_execution_session_id') == (task.get('_execution_session_id') or task['id'])
                    and item.get('_native_outcome_id')
                    and owner.get('_native_outcome_id') == item['_native_outcome_id']):
                candidates.append(item)
        if len(candidates) != 1:
            raise ServiceError(409, 'The native outcome needs one unambiguous retained Work or PR owner.')
        return candidates[0]

    def _restore_native_work_owner(self, owner):
        """An older progress report could replace the visible owner without moving its execution."""
        from .service import ServiceError
        from .background_processes import subscription
        from .work_capacity import execution_key
        if not owner.get('_work_context_id'):
            return
        item = self._get('artifacts', owner['_work_context_id'])
        if item['task_id'] == owner['id']:
            return
        reporter = self._get('tasks', item['task_id'])
        related = [t for t in self._all('tasks')
                   if t.get('session_id') == reporter.get('session_id')
                   and (t.get('_conversation_epoch') == reporter.get('_conversation_epoch')
                        or (t['status'] == 'queued' and '_conversation_epoch' not in t))
                   and (execution_key(t) == execution_key(reporter) or t.get('_assignment_task_id') == reporter['id'])]
        # A saved wait observes another task. It does not own this execution.
        executing = any(t['status'] in ('queued', 'working', 'needs_you', 'paused') for t in related)
        executing = executing or any(r['state'] == 'executing'
            or (not r.get('_process_delivery') and (subscription(r)
                or (r.get('_wait_decision_id') and self._decision_wait_pending(self._get('decisions', r['_wait_decision_id'])))))
            for t in related for r in self._native_task_receipts(t['id']))
        history = [item.get('_work_origin_task_id'), *item.get('_work_previous_task_ids', [])]
        if (item['kind'] != 'work' or item['bot_id'] != owner['bot_id'] or owner['id'] not in history
                or reporter.get('_native_outcome_id') or reporter.get('_work_context_id') == item['id']
                or any(reporter.get(k) != owner.get(k) for k in ('bot_id', 'session_id', 'conversation_bot_id', '_conversation_epoch'))
                or reporter['status'] not in ('completed', 'failed', 'waiting')
                or executing
                or any(d['task_id'] == reporter['id'] and d['status'] == 'pending' for d in self._all('decisions'))):
            raise ServiceError(409, 'This Work item has another execution. Continue its current owner.')
        self._carry_work_history(item, dict(item))
        item.update(task_id=owner['id'], _native_outcome_id=owner['_native_outcome_id'])
        self._put('artifacts', item)
        self._event('artifact.updated', self._public(item), item['bot_id'], owner['id'])

    def continue_native_work(self, source_id, payload):
        requested = set()
        snapshots = {}

        def snapshot_missing(identities):
            missing = set(identities) - requested
            if missing:
                raise _NativeSnapshotsNeeded(sorted(missing))

        # The scheduler holds its adapter lock while spawn takes self.lock.
        # Roll back and release the service transaction before querying it, then
        # re-read all ownership, peer links and pause guards in a new transaction.
        for attempt in range(5):
            try:
                return self._continue_native_work(source_id, payload, snapshot_missing, snapshots)
            except _NativeSnapshotsNeeded as pending:
                if attempt == 4:
                    break
                requested.update(pending.identities)
                result = self.kanban.call({'op': 'snapshot', 'external_ids': sorted(requested)})
                # A periodic snapshot can evict terminal bindings before we retry.
                # Replace the request-local fallback on every fetch so missing or
                # rebound bindings cannot reuse an earlier successful lookup.
                snapshots = {t['external_id']: t for t in result['tasks']}
        from .service import ServiceError
        raise ServiceError(409, 'The native work changed while checking it. Read its current activity and retry.')

    def _continue_native_work(self, source_id, payload, snapshot_missing, snapshots):
        from .service import ServiceError, now, uid, digest, encoded, require_text
        from .work_capacity import execution_key
        with self.transaction():
            # Read one cache generation for both owner and peer admission. Newer
            # cached claims win over the targeted fallback; eviction cannot erase it.
            snapshots = snapshots | (self.kanban.tasks if self.kanban else {})
            source = self._get('tasks', source_id)
            if source['status'] != 'working' or self._task_paused(source):
                raise ServiceError(409, 'This task cannot continue work now.')
            identity = require_text(payload, 'task_id', 100)
            text = require_text(payload, 'message', 16000)
            key = digest(encoded(['continue_work', source_id, source.get('_native_turn_id'), identity, text]))
            previous = self.db.execute('SELECT data FROM deliveries WHERE id=?', (key,)).fetchone()
            if previous:
                return json.loads(previous[0])['receipt']
            owner, native = self._native_continuation_owner(identity, snapshot_missing, snapshots)
            identity = native['id']
            if source['bot_id'] not in (owner['bot_id'], owner.get('source_bot_id')):
                raise ServiceError(403, 'Only the owner or its requesting bot can continue this work.')
            bot = self._get('bots', owner['bot_id'])
            if (self._task_paused(owner) or owner['session_id'] != bot['session_id']
                    or owner.get('_conversation_epoch') != self._setting('conversation_epoch')
                    or self._continuation_blocked(owner)
                    or self._get('exchanges', owner['exchange_id']).get('cancelled')
                    or self._task_work_is_paused(owner)):
                raise ServiceError(409, 'This work is paused, cancelled, or belongs to an earlier conversation.')
            scheduled = native.get('status') == 'scheduled'
            review = native.get('status') == 'review'
            queued = native.get('status') == 'ready' and owner['status'] == 'queued'
            if native.get('status') not in ('triage', 'blocked', 'done', 'scheduled', 'review') and not queued:
                raise ServiceError(409, 'This native outcome is not blocked or completed or quiescent queued/review work. Read its current result or activity.')
            # Fresh input can continue an existing PR, but no other respawn guard.
            # Native claim_task still enforces dependencies at dispatch time.
            # Fence Coconut dispatch as well as refreshing native reviewer claims.
            # A later reviewer claim is preserved by native sync and finish.
            if (queued or review) and (self._kanban_claiming or self._kanban_dispatch_uncertain):
                raise ServiceError(409, 'Native dispatch is in progress. Read its current activity and retry.')
            if review:
                snapshot_missing([owner['id']])
            if queued and native.get('dispatch_blocker') not in (None, 'active_pr'):
                raise ServiceError(409, 'This native outcome is guarded. Wait for its current activity.')
            if (owner['status'] not in ('completed', 'failed', 'waiting') and not
                    (scheduled and owner['status'] == 'scheduled') and not queued) or owner.get('_waiting_on'):
                raise ServiceError(409, 'Wait for this task and its existing dependencies.')
            if owner.get('_return_pending'):
                raise ServiceError(409, 'Wait for the previous result to be delivered before continuing this work.')
            related = [t for t in self._all('tasks') if execution_key(t) == execution_key(owner)]
            worker = self.running.get(owner['id'])
            active_worker = bool(worker and worker.is_alive())
            # Review can hold a reviewer claim or a human pause. Only fresh input
            # to a quiescent execution may reopen it; decisions remain gated below.
            if ((scheduled or queued or review) and (native.get('current_run_id') or native.get('worker_pid') or active_worker)) or any(
                    (t['id'] != owner['id'] and t['status'] in self.ACTIVE)
                    or self._background_pending(t['id']) for t in related):
                raise ServiceError(409, 'This execution has active work. Wait for its result.')
            if any(d['status'] == 'pending' and d['task_id'] in {t['id'] for t in related}
                   for d in self._all('decisions')):
                raise ServiceError(409, 'The existing decision must be answered before work continues.')
            if not owner.get('_native_outcome_id'):
                self._retain_native_work(owner, native, snapshot_missing, snapshots, caller=source)
            owner['_native_outcome_id'] = identity
            self._restore_native_work_owner(owner)
            related_ids = {t['id'] for t in related}
            recovery = owner.setdefault('_self_recovery', {})
            recovery.pop('from_block_check', None)
            recovery['previous_task_id'] = owner['id']
            unknown = [r for identity in related_ids for r in self._native_task_receipts(identity, state='executing')]
            recovery['unknown_tools'] = list({r['id']: r for r in recovery.get('unknown_tools', []) + unknown}.values())
            decisions = [self._public(d) for d in self._all('decisions') if d['task_id'] in related_ids]
            recovery['decisions'] = list({d['id']: d for d in recovery.get('decisions', []) + decisions}.values())
            completed = []
            if owner['status'] == 'failed':
                completed = owner.get('_continuation_receipt_ids', []) + [r['id'] for r in
                    self._native_task_receipts(owner['id'], state='completed') if r.get('turn_id') == owner.get('_native_turn_id')]
            pending = owner.pop('_blocked_followup', None)
            if pending:
                for field, source_key in (('_preference_source_ids', 'source_ids'), ('_attachment_ids', 'attachment_ids')):
                    owner[field] = list(dict.fromkeys(owner.get(field, []) + pending[source_key]))
                owner.update(_steering=[], steering_status='continued')
            # A failed provider may never have received the selected human input.
            retry_input = {field: owner[field] for field in
                ('_preference_source_ids', '_background_completion', '_pr_review') if field in owner}
            if owner['status'] == 'failed' or pending:
                retry_input['_input_after_message_id'] = owner.get('_input_after_message_id')
            else:
                retry_input = {}
            self._advance_input(owner)
            owner.update(retry_input)
            owner['_continuation_receipt_ids'] = list(dict.fromkeys(completed))
            owner.setdefault('_bot_messages', []).append(dict(sender_bot_id=source['bot_id'],
                sender_name=self._get('bots', source['bot_id'])['name'], text=text,
                result_status=None, progress_only=False))
            requester_id = source.get('parent_task_id')
            if requester_id and requester_id in (owner.get('_reply_to_task_id'), owner.get('source_task_id')):
                requester = self._get('tasks', requester_id)
                if (requester['status'] == 'failed' and source['bot_id'] == owner.get('source_bot_id')
                        and all(source.get(k) == requester.get(k) for k in
                                ('bot_id', 'session_id', 'thread_id', 'conversation_bot_id'))
                        and execution_key(source) == execution_key(requester)):
                    # A direct retry receives the result; the failed turn remains historical.
                    owner['_reply_to_task_id'] = source['id']
            owner.update(status='queued', summary='Continuing with the new information.', updated_at=now(),
                _background_return_after=now(), _background_return_id=uid())
            self._put('tasks', owner)
            receipt = dict(task_id=owner['id'], native_task_id=identity, status='queued')
            self._put('deliveries', dict(id=key, source_task_id=source_id, target_bot_id=owner['bot_id'],
                text=text, receipt=receipt, created_at=now()))
            self._event('task.updated', self._public(owner), owner['bot_id'], owner['id'])
            self._refresh_bot(owner['bot_id'])
            return receipt

    @staticmethod
    def _work_history_key(item):
        from .service import digest, encoded
        return digest(encoded({k: v for k, v in item.items() if k != '_completed_history'}))

    def _retained_terminal_work(self, item):
        if self._setting('work.cancelled.' + self._work_identity(item)) or item['status'] == 'cancelled':
            return dict(work_state='cancelled', activity_state='cancelled', activity='Stopped')
        if item['kind'] == 'pr' and (item['status'] == 'merged' or item.get('pr_state') in ('merged', 'closed')):
            return dict(work_state='completed', activity_state='completed', activity='Completed',
                completion_evidence='Pull request ' + (item.get('pr_state') or 'merged') + '.')
        history = item.get('_completed_history') or {}
        if history.get('report') == self._work_history_key(item):
            return dict(work_state='completed', activity_state='completed', activity='Completed',
                completion_evidence=history['evidence'])
        return None

    def _import_completed_work_history(self):
        from datetime import datetime, timezone
        # Import verified results once. Later conversations cannot rewrite completed history.
        candidates = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM artifacts WHERE json_extract(data,'$.kind')='work' "
            "AND json_extract(data,'$.status')='completed' AND json_extract(data,'$._native_outcome_id') IS NULL "
            "AND json_extract(data,'$._same_outcome_as') IS NULL AND NOT coalesce(json_extract(data,'$._hidden'),0)")]
        candidates = [a for a in candidates if not self._retained_terminal_work(a)]
        if not candidates:
            return
        projected = [dict(a) for a in candidates]
        self._project_work_outcomes(projected, {t['id']: t for t in self._all('tasks')},
            self._pending_completion_receipts(), self._all('decisions'), datetime.now(timezone.utc))
        for item, outcome in zip(candidates, projected):
            if outcome['work_state'] != 'completed':
                continue
            item['_completed_history'] = dict(report=self._work_history_key(item), evidence=outcome.get('completion_evidence'))
            self._put('artifacts', item)
            self._event('artifact.updated', self._public(item), item['bot_id'], item['task_id'])

    def _retained_pr_candidates(self):
        from .service import now
        groups = {}
        for row in self.db.execute("SELECT data FROM artifacts WHERE json_extract(data,'$.kind')='pr' "
                "AND NOT coalesce(json_extract(data,'$._hidden'),0) AND NOT coalesce(json_extract(data,'$._deleted'),0)"):
            item = json.loads(row[0])
            groups.setdefault(item['url'].lower(), []).append(item)
        candidates = []
        for url, peers in groups.items():
            if any(p.get('work_outcome_id') or p.get('_native_outcome_id') or self._retained_terminal_work(p) for p in peers):
                continue
            report = self._pr_review_owner(peers)
            if (not report or report.get('pr_state') != 'open' or not report.get('observed_at')
                    or report.get('refresh_error') or self._work_pause(report, peers=peers)
                    or self._pr_has_recorded_merge_decision(report, peers)):
                continue
            owner = self._get('tasks', report['_review_task_id'])
            if owner.get('_work_context_id') or owner.get('_native_outcome_id'):
                continue
            candidates.append(dict(id=str(uuid5(UUID(owner['id']), 'retained-pr:' + url)),
                bot_id=owner['bot_id'], task_id=owner['id'], kind='work', title=report['title'],
                status='waiting', activity=report.get('activity') or 'Checking the saved result', url=None, preview='',
                created_at=report['created_at'], observed_at=now(), _work_key='retained-pr:' + url,
                completion_condition='Complete the accepted request for ' + report['url'] + '. Preserve its approvals and remaining follow-up.',
                _pr_migration_reports=[p['id'] for p in peers]))
        return candidates

    def _migrate_native_work(self):
        if not self.kanban or not getattr(self.provider, 'supports_native_outcomes', False):
            return
        from .service import now
        self._import_completed_work_history()
        candidates = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM artifacts WHERE json_extract(data,'$.kind')='work' "
            "AND json_extract(data,'$._native_outcome_id') IS NULL "
            "AND json_extract(data,'$.status')!='cancelled' "
            "AND json_extract(data,'$._same_outcome_as') IS NULL "
            "AND coalesce(json_extract(data,'$._hidden'),0)=0")]
        candidates = [item for item in candidates if not self._retained_terminal_work(item)]
        candidates.extend(self._retained_pr_candidates())
        for item in candidates:
            owner = self._get('tasks', item['task_id'])
            if (owner['status'] in ('working', 'cancelled') or owner.get('_assignment_task_id')
                    or self._task_paused(owner) or self._automatic_work_is_paused(owner) or self._work_pause(item)
                    or owner.get('_native_outcome_id') or self._external_work_routine(item)
                    or sum(other['task_id'] == owner['id'] for other in candidates) != 1
                    or self._setting('work.cancelled.' + self._work_identity(item))):
                continue
            peers = [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM artifacts WHERE json_extract(data,'$._same_outcome_as')=? "
                "OR json_extract(data,'$._work_parent_id')=?", (item['id'], item['id']))]
            if any(self._external_work_routine(peer) for peer in peers):
                continue
            bot = self._get('bots', owner['bot_id'])
            if not self._process_observation_current(owner, bot):
                continue
            pr_reports = [self._get('artifacts', i) for i in item.get('_pr_migration_reports', [])]
            pr_urls = {p['url'].lower() for p in pr_reports}
            pending = any(d['status'] == 'pending' and (d['task_id'] == owner['id']
                or (d.get('merge_pr_url') or '').lower() in pr_urls) for d in self._all('decisions'))
            waiting = pending or self._background_pending(owner['id']) or bool(owner.get('_waiting_on'))
            waiting = waiting or owner['status'] in ('scheduled', 'paused')
            tasks = {t['id']: t for t in self._all('tasks')}
            records = self._work_receipts()
            attempts = {owner['id'], *(peer['task_id'] for peer in peers)}
            for previous in [item, *peers, *pr_reports]:
                attempts.update(previous.get('_work_previous_task_ids', []))
                if previous.get('kind') == 'pr':
                    attempts.update(previous.get(field) for field in ('task_id', '_review_task_id', '_activity_task_id') if previous.get(field))
            for task_id in tuple(attempts):
                if task_id in tasks:
                    attempts.update(self._work_chain(tasks[task_id], tasks, records, include_continuations=True))
            attempts.update(t['id'] for t in tasks.values() if t.get('_assignment_task_id') in attempts)
            related = attempts - {owner['id']}
            waiters = {i for i in related if pr_reports and i in tasks and tasks[i]['status'] == 'waiting'
                       and owner['id'] in tasks[i].get('_waiting_on', [])}
            if (any((i not in waiters and tasks[i]['status'] in self.ACTIVE)
                    or self._background_pending(i, excluding_task_ids={owner['id']} if i in waiters else ())
                    for i in related if i in tasks)
                    or any(d['task_id'] in related and d['status'] == 'pending' for d in self._all('decisions'))
                    or any(c['task_id'] in attempts and c['status'] in ('queued', 'working') for c in self._all('contexts'))):
                continue
            unknown = list({r['id']: r for r in (owner.get('_self_recovery') or {}).get('unknown_tools', [])
                + [r for r in records if r['task_id'] in attempts and r['state'] == 'executing']}.values())
            if waiting and unknown:
                continue
            state = 'review' if pending else 'scheduled' if waiting else 'ready'
            self._ensure_execution_session(owner)
            if not owner.get('_native_turn_id'):
                self._advance_input(owner)
            row = self._kanban_row(owner, owner, state, priority=10)
            row.update(title=item['title'], work_id=item['id'], body=json.dumps(dict(
                outcome=item['title'], acceptance=item.get('completion_condition'),
                last_report=item.get('activity'), request=owner.get('title'),
                instructions='Continue the saved outcome within its recorded approvals. '
                    'Read the existing conversation, decisions, and receipts before acting. '
                    'Verify unknown tool results before repeating an action. '
                    'Use kanban_complete only after verifying the accepted outcome.'), ensure_ascii=False))
            identity = self.kanban.call({'op': 'promote', 'row': row})['id']
            if not identity:
                continue
            owner.update(_native_outcome_id=identity, _work_context_id=item['id'])
            if unknown:
                owner['_self_recovery'] = dict(owner.get('_self_recovery') or {},
                    previous_task_id=owner['id'], failure=owner.get('summary'), unknown_tools=unknown)
            if not waiting and owner['status'] in ('completed', 'failed', 'waiting'):
                self._advance_input(owner)
                owner.update(status='queued', summary='Checking the saved result', updated_at=now())
            item['_native_outcome_id'] = identity
            item.pop('_pr_migration_reports', None)
            for report in pr_reports:
                report['work_outcome_id'] = item['id']
                self._put('artifacts', report)
                self._event('artifact.updated', self._public(report), report['bot_id'], report['task_id'])
            self._put('tasks', owner)
            self._put('artifacts', item)
            self._event('task.updated', self._public(owner), owner['bot_id'], owner['id'])
            self._event('artifact.updated', self._public(item), item['bot_id'], owner['id'])

    def _dispatch_chat(self, capacity):
        from .work_capacity import execution_key
        active = {execution_key(self._get('tasks', i)) for i in self.running}
        active.update(execution_key(self._get('tasks', self._get('contexts', i)['task_id'])) for i in self.context_threads)
        # Share host capacity, retaining one reserved slot for direct replies.
        slots = max(0, capacity + 1 - len(self.running) - len(self.context_threads))
        if self._kanban_dispatching:
            foreground = sum(bool(self._get('tasks', i).get('_chat_session') or
                self._get('tasks', i).get('_work_status_only')) for i in self.running)
            slots = min(slots, max(0, 1 - foreground))
        candidates = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM tasks WHERE json_extract(data,'$._chat_session') "
            "AND json_extract(data,'$.status') IN ('queued','scheduled') ORDER BY rowid")]
        for task in self._dispatch_order(candidates):
            if not slots:
                break
            if not task.get('_chat_session') or execution_key(task) in active:
                continue
            bot = self._get('bots', task['bot_id'])
            if bot['archived'] or bot.get('_deleted') or not self._dispatch_eligible(task, time.time()):
                continue
            worker = threading.Thread(target=self._run_task, args=(task['id'],), daemon=True, name='conversation-' + task['id'][:8])
            self.running[task['id']] = worker
            active.add(execution_key(task))
            worker.start()
            slots -= 1

    def _adopt_native_outcomes(self):
        from .service import now
        bindings = []
        with self.transaction():
            for native in self.kanban.tasks.values():
                if not native.get('unbound'):
                    continue
                # Hermes now retains only persisted native session IDs. Coconut's
                # creation receipt keeps the distinct task owner across that normalization.
                receipt = self.db.execute(
                    "SELECT data FROM native_receipts WHERE json_extract(data,'$.action.name')='kanban_create' "
                    "AND json_extract(data,'$.action.arguments.idempotency_key')=? LIMIT 1",
                    (native.get('idempotency_key'),)).fetchone()
                origin_id = native.get('session_id')
                if receipt:
                    creation = json.loads(receipt[0])
                    arguments = creation['action']['arguments']
                    if (arguments.get('session_id') != creation['task_id']
                            or arguments.get('tenant') != native.get('tenant')):
                        continue
                    origin_id = creation['task_id']
                row = self.db.execute('SELECT data FROM tasks WHERE id=?', (origin_id,)).fetchone()
                if not row:
                    continue
                origin = json.loads(row[0])
                if native.get('tenant') != origin['bot_id']:
                    continue
                identity = outcome_owner_id(origin_id, native['id'])
                if not self.db.execute('SELECT 1 FROM tasks WHERE id=?', (identity,)).fetchone():
                    receipt = self.db.execute(
                        "SELECT data FROM native_receipts WHERE json_extract(data,'$.task_id')=? "
                        "AND json_extract(data,'$.action.name')='kanban_create' "
                        "AND json_extract(data,'$.action.arguments.idempotency_key')=? LIMIT 1",
                        (origin_id, native.get('idempotency_key'))).fetchone()
                    attachments = json.loads(receipt[0]).get('_attachment_ids', []) if receipt else []
                    task = dict(id=identity, thread_id=identity, parent_task_id=origin_id,
                        bot_id=origin['bot_id'], conversation_bot_id=origin['conversation_bot_id'],
                        session_id=origin['session_id'], source_bot_id=origin.get('source_bot_id'),
                        source_task_id=origin.get('source_task_id'),
                        exchange_id=self._new_exchange(), exchange_depth=origin.get('exchange_depth', 0),
                        title=native['title'], status='queued', summary='Queued', scheduled_at=None,
                        created_at=now(), updated_at=now(), _attachment_ids=attachments,
                        _execution_session_id=identity, _native_outcome_id=native['id'])
                    callback = origin.get('_relay_callback_owner') or (origin_id if origin.get('source_bot_id') else None)
                    if callback:
                        task['_relay_callback_owner'] = callback
                        task['_return_dependency_id'] = origin.get('_return_dependency_id', origin_id)
                    if origin.get('_reply_to_task_id'):
                        task['_reply_to_task_id'] = origin['_reply_to_task_id']
                    self._retain_native_work(task, native)
                    self._put('tasks', task)
                    self._event('task.created', self._public(task), task['bot_id'], identity)
                bindings.append(dict(op='bind', id=native['id'], external_id=identity))
        for binding in bindings:
            self.kanban.call(binding)
        if bindings:
            self.kanban.call({'op': 'snapshot'})

    def _native_retry(self, task, native):
        from .service import now, uid
        if (task['status'] not in ('completed', 'failed') or native['status'] != 'ready'
                or self._task_paused(task) or self._automatic_work_is_paused(task)
                or any(d['task_id'] == task['id'] and d['status'] == 'pending' for d in self._all('decisions'))):
            return
        records = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM native_receipts WHERE json_extract(data,'$.task_id')=? "
            "AND json_extract(data,'$.state')='executing'", (task['id'],))]
        # A retry follows a cleared block. Drop the old block record from its context.
        # The retry takes over the context, so the block check no longer owns it.
        previous = {k: v for k, v in (task.get('_self_recovery') or {}).items() if k not in ('blocked', 'from_block_check')}
        records = list({r['id']: r for r in previous.get('unknown_tools', []) + records}.values())
        self._advance_input(task)
        task.update(status='queued', summary='Checking the previous result', updated_at=now(),
            _background_return_after=now(), _background_return_id=uid(),
            _self_recovery=dict(previous, attempt=native.get('consecutive_failures', 0), previous_task_id=task['id'],
                failure=native.get('last_failure_error'), unknown_tools=records,
                work=previous.get('work', []), decisions=previous.get('decisions', [])))
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])

    @staticmethod
    def _end_block_context(task):
        recovery = task.get('_self_recovery') or {}
        recovery.pop('blocked', None)
        if recovery.pop('from_block_check', None):
            recovery.pop('failure', None)
            # Keep tool calls without a result, so a later turn does not repeat an unknown write.
            if not recovery.get('unknown_tools'):
                task.pop('_self_recovery', None)

    def _native_block_check(self, task, native):
        # An explicit native block keeps its reason, but nothing else resumes it.
        # Queue one owner check, then back off, until a card or recheck takes over.
        from .service import now, uid
        from .work_recovery import block_check_delay
        check = task.get('_block_check')
        idle = (task['status'] in ('completed', 'failed', 'waiting', 'cancelled') and not native.get('current_run_id')
                and not native.get('worker_pid'))
        # A check unblocks the task. A repeated block of the same kind can land in native triage.
        stuck = native['status'] in ('blocked', 'triage') and native.get('block_kind') in ('capability', 'needs_input')
        if not stuck:
            if native['status'] in ('blocked', 'triage'):
                # A short dependency or retry block keeps the pending due time.
                return
            if check and idle and native['status'] in ('done', 'archived'):
                task.pop('_block_check')
                self._end_block_context(task)
                self._put('tasks', task)
            elif check and not check.get('cleared'):
                # Keep the backoff level. A later block starts its own settle delay.
                # A running check turn still needs its block record, so drop it only when idle.
                task['_block_check'] = dict(check, cleared=True)
                if idle:
                    self._end_block_context(task)
                self._put('tasks', task)
            elif check and idle and (task.get('_self_recovery') or {}).get('blocked'):
                self._end_block_context(task)
                self._put('tasks', task)
            return
        if task['status'] == 'cancelled' or not idle or task.get('_waiting_on'):
            return
        clock = time.time()
        if not check or check.get('cleared'):
            attempt = check['attempt'] if check else 0
            task['_block_check'] = dict(since=clock, attempt=attempt, next_at=clock + block_check_delay(attempt))
            self._put('tasks', task)
            return
        # Read the tables only when a check is due. Reconcile runs every second.
        if clock < check['next_at']:
            return
        def defer():
            # Defer without a turn. Keep the backoff level, so a lasting pause is read rarely.
            task['_block_check'] = dict(check, next_at=clock + block_check_delay(max(check['attempt'], 1)))
            self._put('tasks', task)
        if (not self._process_observation_current(task, self._get('bots', task['bot_id']))
                or self._task_paused(task) or self._background_pending(task['id'])):
            return defer()
        # Without an execution session, execution_key falls back to the bot. Keep the check to this outcome.
        session = task.get('_execution_session_id')
        related = {t['id'] for t in self._all('tasks') if session and t.get('_execution_session_id') == session} | {task['id']}
        decisions = [d for d in self._all('decisions') if d['task_id'] in related]
        if (self._task_work_is_paused(task) or self._automatic_work_is_paused(task)
                or any(d['status'] == 'pending' for d in decisions)):
            return defer()
        records = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM native_receipts WHERE json_extract(data,'$.task_id')=? "
            "AND json_extract(data,'$.state')='executing'", (task['id'],))]
        previous = task.get('_self_recovery') or {}
        records = list({r['id']: r for r in previous.get('unknown_tools', []) + records}.values())
        attempt = check['attempt'] + 1
        # A reply held from a failed turn joins this check, as it does for other continuations.
        held = task.pop('_blocked_followup', None)
        if held:
            for field, source_key in (('_preference_source_ids', 'source_ids'), ('_attachment_ids', 'attachment_ids')):
                task[field] = list(dict.fromkeys(task.get(field, []) + held[source_key]))
            task.update(_steering=[], steering_status='continued')
        # A failed provider may not have read its input, so keep that boundary as other continuations do.
        # Otherwise the new boundary follows the last turn, and the turn receives only the held reply.
        failed_input = task.get('_input_after_message_id') if task['status'] == 'failed' else None
        self._advance_input(task)
        if failed_input is not None:
            task['_input_after_message_id'] = failed_input
        # The check instructions apply only to this turn. New input rotates the turn ID.
        task.update(status='queued', summary='Checking why this work is blocked', updated_at=now(),
            _background_return_after=now(), _background_return_id=uid(),
            _block_check=dict(check, attempt=attempt, next_at=clock + block_check_delay(attempt)),
            # Keep the recovery attempt count. The block check keeps its own count in _block_check.
            _self_recovery=dict(previous, previous_task_id=task['id'],
                # Mark a context this check created, so it ends with the block.
                from_block_check=previous.get('from_block_check', not previous),
                failure='The native task is blocked: ' + native['block_kind'] + '.',
                blocked=dict(kind=native['block_kind'], turn=task.get('_native_turn_id'),
                             since=datetime.fromtimestamp(check['since'], timezone.utc).isoformat()),
                unknown_tools=records, work=previous.get('work', []),
                # The check reads earlier answers, so a declined card becomes a pause instead of a new question.
                decisions=list({d['id']: d for d in previous.get('decisions', []) + [self._public(d) for d in decisions]}.values())))
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])

    def _native_work_view(self, artifacts):
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc)
        groups = {}
        pending_decisions = None
        for item in artifacts:
            if item['kind'] not in ('work', 'pr'):
                continue
            root = self._work_outcome_artifact(item)
            if root['id'] != item['id'] or item.get('work_outcome_id'):
                item['work_outcome_id'] = root['id']
            if not root.get('_native_outcome_id'):
                terminal = self._retained_terminal_work(root)
                if terminal:
                    item.update(terminal, activity_observed_at=timestamp.isoformat(), work_decision_id=None,
                        work_next_step=None, work_queue_position=None,
                        work_dismissal_id=self._setting('work.dismissed.' + self._work_identity(root)))
                    if root['id'] != item['id'] or item.get('work_outcome_id'):
                        item['work_outcome_id'] = root['id']
                    if item['kind'] == 'work':
                        item['status'] = terminal['activity_state']
                else:
                    item.update(work_state="waiting", activity_state="waiting", activity="Status unavailable",
                        activity_observed_at=None, work_decision_id=None, work_next_step=None,
                        work_queue_position=None, work_dependencies=[], work_attempts=[])
                    if pending_decisions is None:
                        pending_decisions = [json.loads(row[0]) for row in self.db.execute(
                            "SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending' "
                            "AND NOT coalesce(json_extract(data,'$._hidden'),0) AND NOT coalesce(json_extract(data,'$._deleted'),0)")]
                    pending = next((d for d in pending_decisions if d['task_id'] == root['task_id'] or any(
                        p['kind'] == 'pr' and p.get('url') and d.get('merge_pr_url') == p['url']
                        for p in (root, item))), None)
                    if pending:
                        item.update(work_state='needs_decision', activity='Waiting for your decision',
                            work_decision_id=pending['id'], work_next_step='Review decision')
                    elif root.get('paused_by_decision_id'):
                        pause = self._get('decisions', root['paused_by_decision_id'])
                        if self._work_pause(root, decisions=[pause], peers=[root]):
                            item['activity'] = 'Paused by your decision'
                continue
            if root['id'] != item['id'] or item.get('work_outcome_id'):
                item['work_outcome_id'] = root['id']
            groups.setdefault(root['id'], (root, []))[1].append(item)
        if groups:
            identities = {root['task_id'] for root, _ in groups.values()} - {None}
            identities.update(n['external_id'] for n in self.kanban.tasks.values())
            tasks = {t['id']: t for t in (json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM tasks WHERE id IN (SELECT value FROM json_each(?))', (json.dumps(sorted(identities)),)))}
            decisions = [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending' "
                "AND NOT coalesce(json_extract(data,'$._hidden'),0) AND NOT coalesce(json_extract(data,'$._deleted'),0)")]
            reports = {}
            for row in self.db.execute(
                    "SELECT data FROM artifacts WHERE json_extract(data,'$.kind')='pr' "
                    "AND NOT coalesce(json_extract(data,'$._hidden'),0)"):
                linked = json.loads(row[0])
                root_id = self._work_outcome_artifact(linked)['id']
                if root_id in groups:
                    reports.setdefault(root_id, []).append(linked)
            positions = self._native_queue_positions(tasks, timestamp.timestamp())
            for root, peers in groups.values():
                outcome = self._native_outcome_projection(root, tasks, [], decisions, timestamp, positions,
                    peers + reports.get(root['id'], []))
                for item in peers:
                    item.update(outcome)
                    if item['kind'] == 'work':
                        item['status'] = outcome['activity_state']
                    item['work_dismissal_id'] = self._setting('work.dismissed.' + self._work_identity(root))
        return artifacts

    def _native_outcome_projection(self, item, tasks, records, decisions, timestamp, queue_positions, peers=None):
        native = next((n for n in self.kanban.tasks.values() if n['id'] == item['_native_outcome_id']), None) if self.kanban else None
        owner = tasks.get(item.get('task_id'), {})
        peers = peers or [item]
        owners = {t['id'] for t in tasks.values() if t.get('_native_outcome_id') == item['_native_outcome_id']}
        owners.add(item.get('task_id'))
        pending = [d for d in decisions if d['status'] == 'pending' and (d['task_id'] in owners or any(
            p['kind'] == 'pr' and d.get('merge_pr_url') == p.get('url') for p in peers))]
        status = native['status'] if native else None
        state = {'ready': 'queued', 'running': 'working', 'done': 'completed', 'archived': 'cancelled'}.get(status, 'waiting')
        activity = {'queued': 'Queued', 'working': 'In progress', 'completed': 'Completed', 'cancelled': 'Stopped'}.get(state, 'Waiting for a result')
        if native is None:
            activity = 'Status unavailable'
        terminal = state in ('completed', 'cancelled')
        if pending and not terminal:
            state, activity = 'needs_decision', 'Waiting for your decision'
        if self._setting('work.cancelled.' + self._work_identity(item)):
            state, activity = 'cancelled', 'Stopped'
        dependencies = native.get('dependencies', []) if native else []
        if native and state == 'waiting':
            if dependencies:
                activity = 'Waiting for ' + dependencies[0]['title']
                if len(dependencies) > 1:
                    activity += ' and ' + str(len(dependencies) - 1) + ' other tasks'
            else:
                activity = {'review': 'Waiting for review', 'scheduled': 'Waiting for a scheduled check',
                    'triage': 'Paused: the next step needs clarification',
                    'blocked': 'Blocked: the next step cannot run'}.get(status, activity)
                if status in ('blocked', 'triage'):
                    activity = {'needs_input': 'Paused: input needed',
                        'capability': 'Blocked: a required resource is unavailable',
                        'dependency': 'Waiting for a dependency',
                        'transient': 'Waiting to retry'}.get(native.get('block_kind'), activity)
        if native and state == 'working':
            activity = native.get('progress_note') or 'Waiting for worker output'
        return dict(title=native['title'] if native else item['title'], native_task_id=item['_native_outcome_id'],
            native_status=status, native_block_kind=native.get('block_kind') if native else None,
            progress_observed_at=native.get('progress_at') if native else None,
            work_state=state, activity=activity, work_next_step='Review decision' if state == 'needs_decision' else None,
            work_decision_id=pending[0]['id'] if state == 'needs_decision' else None,
            work_queue_position=(queue_positions or {}).get(owner.get('id')) if state == 'queued' else None,
            completion_condition=item.get('completion_condition') or item['title'],
            completion_evidence=native.get('outcome_summary') if native and state == 'completed' else None,
            work_attempts=[dict(id=identity, state=tasks[identity]['status']) for identity in sorted(owners) if identity in tasks],
            work_dependencies=[dict(id=parent['id'], title=parent['title'], state=parent['status']) for parent in dependencies],
            activity_bot_id=owner.get('bot_id', item['bot_id']),
            activity_observed_at=self.kanban.observed_at if self.kanban else None,
            activity_state={'needs_decision': 'waiting', 'queued': 'waiting'}.get(state, state))
