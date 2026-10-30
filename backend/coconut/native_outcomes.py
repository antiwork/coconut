"""Keep native kanban outcomes separate from phone conversation turns."""
import json
import threading
import time
from datetime import datetime, timezone
from uuid import UUID, uuid5

if __package__:
    from .technical_lane import (SCHEMA as TECHNICAL_LANE_SCHEMA, parked as technical_lane_parked,
                                 requested as technical_lane_request)
else:
    # Hermes workers import this module from the package directory for its tool schemas.
    from technical_lane import (SCHEMA as TECHNICAL_LANE_SCHEMA, parked as technical_lane_parked,
                                requested as technical_lane_request)


# A block check offers a delegate's answer with only these public fields, including the person's typed reply.
# The card's context and notes stay private. The fingerprint covers the answer itself.
DELEGATED_DECISION_FIELDS = ('id', 'bot_id', 'task_id', 'title', 'action_type', 'status', 'revision', 'decided_at',
                             'selected_option_id', 'selected_option_label', 'correction')
BLOCK_ANSWER_FIELDS = ('id', 'task_id', 'bot_id', 'status', 'revision', 'decided_by', 'decided_at',
                       'selected_option_id', 'selected_option_label', 'correction')


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
    'Use coconut_ask only for a concrete decision. '
)
WORK_INSTRUCTIONS = (
    'The native kanban task in the current message owns this outcome. Its comments and previous attempts are the work record. '
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

UNLINK_SCHEMA = {
    'name': 'coconut_unlink_dependency',
    'description': 'Remove one obsolete native parent→child dependency. You must be the child’s owner or requesting bot. '
        'The child must be TODO and both outcomes quiescent. Give the exact IDs from kanban_show and a reason. '
        'This preserves other dependencies, workspaces, results and decisions; it does not approve or complete work.',
    'parameters': {'type': 'object', 'additionalProperties': False,
        'properties': {'parent_id': {'type': 'string'}, 'child_id': {'type': 'string'},
                       'reason': {'type': 'string', 'maxLength': 4000}},
        'required': ['parent_id', 'child_id', 'reason']},
}


RETAINED_PR_URL_LIMIT = 500
# The refusal when the owner's delegated PRs of one Work all finished and the call names none of them.
FINISHED_PRS_AMBIGUOUS = ('Choose one retained Work item for this native outcome. Its delegated PRs of the same Work '
                          'have all finished. Name one of them with retained_pr_url.')
SELECTION_REFUSED = ('retained_pr_url names no finished delegated PR that this continuation can choose. Use it only '
                     'when every delegated PR of the Work finished and the plain call asks for it.')

CONTINUE_SCHEMA = {
    'name': 'coconut_continue_work',
    'description': 'Continue a quiescent queued, scheduled, review, blocked, failed, or completed Hermes outcome with new facts. Use the native task ID from kanban_show. '
        'You must own the work or be its requesting bot. This keeps its task, workspace, decisions, and result route. '
        'Hermes preserves completed results and creates a linked follow-up with the same workspace. '
        'This does not approve actions. The result goes to the original requester.',
    'parameters': {'type': 'object', 'additionalProperties': False,
        'properties': {'task_id': {'type': 'string'}, 'message': {'type': 'string', 'maxLength': 16000},
                       'independent_technical_work': TECHNICAL_LANE_SCHEMA,
                       'retained_pr_url': {'type': 'string', 'maxLength': RETAINED_PR_URL_LIMIT,
                           'description': 'Only when the plain call refuses because every delegated PR of the Work '
                               'finished: the URL of the one finished PR that this continuation keeps as its Work item. '
                               'It chooses the presentation only. It approves nothing and changes no task or Work.'},
                       'continue_closed_source': {'type': 'boolean',
                           'description': 'Only when the plain call refuses because Coconut ended this work after every '
                               'GitHub item that it works on closed, and the person or requester still needs this same work: '
                               'true continues the same execution. It approves nothing.'}},
        'required': ['task_id', 'message']},
}
CLOSED_SOURCE_REFUSED = ('Coconut ended this work because every GitHub item that it works on closed. Repeat the call with '
                         'continue_closed_source: true only when this same work must still continue.')


# These Hermes tools run a local command as a process of the execution.
# A delegate_task call runs its children inside the worker's own process tree with the worker's
# COCONUT_WORKSPACE_OUTCOME, so the same proof ends it. A worker that stopped before the call returned leaves no
# result, and Hermes marks that delegation abandoned. The receipt keeps its executing state and its unknown child
# effects for the owner's next turn. Only the writer guard reads the ended mark.
LOCAL_COMMAND_TOOLS = ('terminal', 'execute_code', 'delegate_task')
# The proposals of one task. decisions_task indexes this expression, so no lookup scans every decision.
PROPOSAL_DECISIONS = ("SELECT data FROM decisions WHERE json_extract(data,'$.task_id')=? "
                      "AND json_extract(data,'$.inbox_kind')='proposal'")


class _NativeSnapshotsNeeded(Exception):
    def __init__(self, identities):
        self.identities = identities


class _NativeCardNeeded(Exception):
    def __init__(self, identity):
        self.identity = identity


class NativeOutcomes:
    def _unlink_conversation_current(self, child):
        epoch = self._setting('conversation_epoch')
        # Preserve the original unrotated-conversation admission as well.
        if child.get('_conversation_epoch') == epoch:
            return True
        if '_conversation_epoch' in child:
            return False
        # _advance_input deliberately drops the epoch. Do not stamp a new one:
        # only an original owner created since the last rotation can qualify.
        if (not epoch or child['status'] != 'queued' or not child.get('_native_turn_id')
                or not child.get('_native_outcome_id') or child.get('_execution_session_id') != child['id']):
            return False
        rotation = self.db.execute("SELECT MAX(seq) FROM events WHERE type='bots.restarted'").fetchone()[0]
        created = self.db.execute("SELECT seq,bot_id,data FROM events WHERE type='task.created' AND task_id=?",
                                  (child['id'],)).fetchall()
        if not rotation or len(created) != 1 or created[0][0] <= rotation:
            return False
        original = json.loads(created[0][2])
        # Relay creation uses the conversation bot; native binding uses the owner.
        if (created[0][1] not in (child['conversation_bot_id'], child['bot_id']) or any(
                not child.get(key) or original.get(key) != child[key]
                for key in ('id', 'bot_id', 'session_id', 'thread_id', 'conversation_bot_id', 'exchange_id', 'created_at'))
                or any(original.get(key) != child.get(key) for key in ('source_task_id', 'source_bot_id', 'parent_task_id'))):
            return False
        queued = self.db.execute("SELECT seq,data FROM events WHERE type='task.updated' AND task_id=? ORDER BY seq DESC LIMIT 1",
                                 (child['id'],)).fetchone()
        if not queued or queued[0] <= created[0][0] or json.loads(queued[1]) != self._public(child):
            return False
        returned = self.db.execute("SELECT seq,bot_id,data FROM events WHERE type='bot_message.delivered' AND task_id=? "
                                   "ORDER BY seq DESC LIMIT 1", (child['id'],)).fetchone()
        if not returned or returned[0] <= queued[0]:
            return False
        tasks = {t['id']: t for t in self._all('tasks')}
        # Legacy recovery proves only an original producer's own completed return.
        # Continuation-owned results need separate provenance, not inferred ancestry.
        messages = None
        for delivery in self._all('deliveries'):
            receipt = delivery.get('receipt') or {}
            producer = tasks.get(delivery.get('source_task_id'))
            if (delivery.get('_progress_only') or not producer or producer['status'] != 'completed'
                    or producer.get('_conversation_epoch') != epoch
                    or producer.get('source_task_id') != child['id']
                    or producer.get('_reply_to_task_id') != child['id']
                    or producer.get('source_bot_id') != child['bot_id']
                    or producer.get('_assignment_task_id') not in (None, producer['id'])
                    or producer.get('_result_task_id') not in (None, producer['id'])
                    or any(producer.get(k) != child[k] for k in ('thread_id', 'conversation_bot_id', 'exchange_id'))
                    or delivery.get('target_bot_id') != child['bot_id']
                    or receipt.get('task_id') != child['id'] or receipt.get('target_bot_id') != child['bot_id']
                    or receipt.get('thread_id') != child['thread_id'] or receipt.get('status') != 'queued'
                    or not delivery.get('text')):
                continue
            route = dict(source_task_id=producer['id'], source_bot_id=producer['bot_id'],
                         target_bot_id=child['bot_id'], task_id=child['id'], thread_id=child['thread_id'],
                         conversation_bot_id=child['conversation_bot_id'], exchange_id=child['exchange_id'], is_return=True)
            # A historical completed return cannot authorize a newer queued input.
            if (returned[1] != child['conversation_bot_id']
                    or any(json.loads(returned[2]).get(k) != v for k, v in route.items())):
                continue
            message = (child.get('_bot_messages') or [{}])[-1]
            if (message.get('sender_bot_id') != producer['bot_id'] or message.get('text') != delivery['text']
                    or message.get('result_status') != 'completed' or message.get('progress_only')):
                continue
            if messages is None:
                messages = self._all('messages')
            if any(m.get('sender_bot_id') == producer['bot_id'] and m.get('thread_id') == child['thread_id']
                   and m.get('text') == delivery['text'] and m.get('role') == 'assistant'
                   and ((m.get('task_id') == producer['id'] and m.get('kind') == 'text'
                         and m.get('exchange_recipient_bot_ids') == [child['bot_id']])
                        or (m.get('task_id') == child['id'] and m.get('kind') == 'bot_exchange'
                            and m.get('recipient_bot_ids') == [child['bot_id']]))
                   and not any(m.get(k) for k in ('_hidden', '_deleted', '_direct_reply_message_id', 'streaming'))
                   for m in messages):
                return True
        return False

    def _dependency_unlink_tasks(self, tasks, parent, child):
        from .work_capacity import execution_key

        def scope(task):
            return execution_key(task) if task.get('_execution_session_id') else task['id']

        by_id = {t['id']: t for t in tasks}
        linked, _ = self._work_links(by_id, self._pending_completion_receipts(), True)
        executions = {}
        for task in tasks:
            executions.setdefault(scope(task), set()).add(task['id'])
            # Legacy delegated helpers predate native relay/assignment markers.
            # Follow their authenticated source downward, never requester siblings.
            source = by_id.get(task.get('source_task_id'))
            if source and task.get('source_bot_id') == source['bot_id']:
                linked.setdefault(source['id'], set()).add(task['id'])
        found, pending = {}, [parent['id'], child['id']]
        while pending:
            identity = pending.pop()
            if identity in found or identity not in by_id:
                continue
            task = found[identity] = by_id[identity]
            pending.extend(executions[scope(task)] - found.keys())
            pending.extend(linked.get(identity, set()) - found.keys())
        # Follow saved helpers and returns even across sessions, but never walk
        # up to a requester and pull in that bot's unrelated assignments.
        return found

    def unlink_native_dependency(self, source_id, payload):
        import re
        from .service import ServiceError, digest, encoded, require_text
        from .work_capacity import execution_key

        def native_identity(task):
            return task.get('_native_outcome_id') or (task.get('_kanban_claim') or {}).get('native_id')

        if set(payload) != {'parent_id', 'child_id', 'reason'}:
            raise ServiceError(400, 'Supply only parent_id, child_id, and reason.')
        parent_id, child_id = (payload[key] for key in ('parent_id', 'child_id'))
        if any(not isinstance(value, str) or not re.fullmatch(r't_[0-9a-f]{8}', value) for value in (parent_id, child_id)):
            raise ServiceError(400, 'Use exact native task IDs from kanban_show.')
        if parent_id == child_id:
            raise ServiceError(400, 'Choose two different native outcomes.')
        reason = require_text(payload, 'reason', 4000)
        if not self.kanban or not self.dispatch_gate.acquire(timeout=5):
            raise ServiceError(409, 'Native dispatch is in progress or unavailable. Retry after it finishes.')
        fenced = False
        try:
            # Never query the adapter under self.lock: a preceding tick can
            # need that lock to finish spawn bookkeeping.
            with self.lock:
                tasks = self._all('tasks')
                identities = [t['id'] for t in tasks if native_identity(t) in (parent_id, child_id)]
                owners = {identity: [t for t in tasks if native_identity(t) == identity]
                          for identity in (parent_id, child_id)}
                parent_owner = owners[parent_id][0] if len(owners[parent_id]) == 1 else None
                child_owner = owners[child_id][0] if len(owners[child_id]) == 1 else None
                # Done cards fall out of the default snapshot. Refresh the exact
                # execution/helper scope, without adding unrelated bot history.
                observed = []
                if parent_owner and child_owner:
                    observed = list(self._dependency_unlink_tasks(tasks, parent_owner, child_owner))
            snapshots = {t['external_id']: t for t in self.kanban.call(
                {'op': 'snapshot', 'external_ids': list(dict.fromkeys(identities + observed))})['tasks']}
            def authorize():
                source = self._get('tasks', source_id)
                if source['status'] != 'working' or self._task_paused(source):
                    raise ServiceError(409, 'This task cannot unlink a dependency now.')
                # Only exact task bindings, never context aliases or inferred owners.
                owners = []
                for identity in (parent_id, child_id):
                    candidates = [t for t in self._all('tasks') if
                        (t.get('_native_outcome_id') or (t.get('_kanban_claim') or {}).get('native_id')) == identity]
                    if len(candidates) != 1 or snapshots.get(candidates[0]['id'], {}).get('id') != identity:
                        raise ServiceError(409, 'Choose an exact retained native dependency from kanban_show.')
                    owners.append(candidates[0])
                parent, child = owners
                if source['bot_id'] not in (child['bot_id'], child.get('source_bot_id')):
                    raise ServiceError(403, 'Only the child owner or its requesting bot can unlink this dependency.')
                request = dict(op='unlink_dependency', parent_id=parent_id, child_id=child_id, reason=reason,
                    parent_external_id=parent['id'], parent_bot_id=parent['bot_id'],
                    child_external_id=child['id'], child_bot_id=child['bot_id'],
                    source_task_id=source_id, source_bot_id=source['bot_id'], source_turn_id=source.get('_native_turn_id'),
                    source_native_id=source.get('_native_outcome_id') or (source.get('_kanban_claim') or {}).get('native_id'),
                    guarded_native_ids=sorted({snapshots[identity]['id'] for identity in observed
                        if identity in snapshots and (identity != source_id or identity in (parent['id'], child['id']))}),
                    request_id=digest(encoded(['unlink_dependency', source_id, source.get('_native_turn_id'), parent_id, child_id, reason])))
                return source, parent, child, request

            with self.transaction():
                source, parent, child, request = authorize()
                admission = (child, self._setting('conversation_epoch'))
            try:
                saved = self.kanban.call(request | {'op': 'unlink_receipt'})['receipt']
            except RuntimeError as error:
                raise ServiceError(409, str(error)) from error
            with self.transaction():
                source, parent, child, current = authorize()
                if current != request:
                    raise ServiceError(409, 'The unlink authority changed. Read current work before retrying.')
                if saved is not None:
                    return saved
                if self._kanban_claiming or self._kanban_dispatch_uncertain:
                    raise ServiceError(409, 'Native dispatch is in progress. Retry after it finishes.')
                if admission != (child, self._setting('conversation_epoch')):
                    raise ServiceError(409, 'The child input changed. Read current work before retrying.')
                tasks = self._all('tasks')
                related = [t for t in tasks if execution_key(t) == execution_key(child)]
                child_ids = {t['id'] for t in related}
                bot = self._get('bots', child['bot_id'])
                if (bot['archived'] or bot.get('_deleted') or child['session_id'] != bot['session_id']
                        or not self._unlink_conversation_current(child)
                        or self._continuation_blocked(child)
                        or any(self._task_paused(t) or self._task_work_is_paused(t) or t['status'] in ('paused', 'cancelled', 'needs_you')
                               or self._get('exchanges', t['exchange_id']).get('cancelled') for t in related)):
                    raise ServiceError(409, 'The child work is paused, cancelled, or belongs to an earlier conversation.')
                if any(d['task_id'] in child_ids and d['status'] == 'pending' for d in self._all('decisions')):
                    raise ServiceError(409, 'The child’s existing decision must be answered before unlinking.')
                # Parent appearance/decision state is deliberately not interpreted
                # as permission for the child, or changed by this operation.
                guarded = self._dependency_unlink_tasks(tasks, parent, child)
                if guarded.keys() - set(observed):
                    raise ServiceError(409, 'The related work changed. Read current work before retrying.')
                for task in guarded.values():
                    if task['id'] == source_id and task['id'] not in (parent['id'], child['id']):
                        continue
                    worker = self.running.get(task['id'])
                    receipts = self._native_task_receipts(task['id'], state='executing')
                    card = snapshots.get(task['id'])
                    # A missing card is not proof the run stopped. A returned quiet card is.
                    # A live worker, native run, or local command that has not ended is still owner work.
                    # A queued task has no worker yet. A tool call of an earlier turn cannot still run, for example a
                    # process wait that the turn never closed.
                    stale_receipts = ((task['status'] in ('completed', 'failed')
                                       or task['status'] == 'queued' and all(r.get('turn_id') != task.get('_native_turn_id') for r in receipts))
                                      and card is not None
                                      and not (card.get('current_run_id') or card.get('worker_pid'))
                                      and not any((r.get('action') or {}).get('name') in LOCAL_COMMAND_TOOLS
                                                  and not r.get('_process_ended_at') for r in receipts))
                    if (task['status'] == 'working' or worker and worker.is_alive()
                            or card and (card.get('current_run_id') or card.get('worker_pid'))
                            or self._background_pending(task['id'], ignore_settled=True)
                            or (receipts and not stale_receipts)
                            or task.get('_return_pending') or task.get('_native_reply_pending')):
                        raise ServiceError(409, 'Wait for the existing owner work and tool results before unlinking.')
                if any(c['task_id'] in guarded and c['status'] in ('queued', 'working') for c in self._all('contexts')):
                    raise ServiceError(409, 'Wait for the existing owner context before unlinking.')
                # Native tool starts take a service transaction. Drain already
                # accepted graph calls before fencing new starts during unlink.
                if any(r['state'] == 'executing' and r.get('action', {}).get('name') in ('kanban_link', 'kanban_create')
                       for r in self._all('native_receipts')):
                    raise ServiceError(409, 'Wait for the existing native graph operation before unlinking.')
                self._dependency_unlinking = True
                fenced = True
            try:
                return self.kanban.call(request)
            except Exception as error:
                from .kanban_scheduler import KanbanRequestError
                # A native commit can precede an adapter error. Only an intact
                # journal read can distinguish rejection from an unknown effect.
                try:
                    saved = self.kanban.call(request | {'op': 'unlink_receipt'})['receipt']
                except Exception:
                    saved, confirmed = None, False
                else:
                    confirmed = isinstance(error, KanbanRequestError)
                if saved is not None:
                    return saved
                if confirmed:
                    raise ServiceError(409, str(error)) from error
                return dict(status='unconfirmed', request_id=request['request_id'], parent_id=parent_id,
                            child_id=child_id, error='The adapter did not confirm the unlink. Read the current dependencies before retrying.')
        finally:
            if fenced:
                with self.changed:
                    self._dependency_unlinking = False
                    self.changed.notify_all()
            self.dispatch_gate.release()

    def _requester_callback_foreground(self, caller, requester, tasks, snapshots):
        """Qualify only the authenticated caller's recorded requester continuation."""
        from .work_capacity import execution_key
        if (not caller or not requester or caller['id'] == requester['id']
                or not self._calling_native_turn(caller, snapshots)
                or any(caller.get(k) != requester.get(k) for k in
                       ('bot_id', 'session_id', 'thread_id', 'conversation_bot_id', '_conversation_epoch'))):
            return False
        # Execution identity alone is not return provenance. Follow reciprocal
        # continuation links, a person's answer to a proposal, or a callback's exact persisted delivery and route.
        current, execution, seen = caller, execution_key(caller), set()
        while current['id'] != requester['id']:
            if current['id'] in seen:
                return False
            seen.add(current['id'])
            previous = tasks.get(current.get('parent_task_id'))
            if previous and self._continued_work_task(previous, tasks) == current:
                current = previous
                continue
            if (previous and not current.get('_is_return') and execution == execution_key(current) == current['id']
                    and self._approved_proposal_execution(previous, current)):
                # The answer started a new execution from the proposing turn. That turn must lead to the requester.
                current, execution = previous, execution_key(previous)
                continue
            if not self._requester_return(current, requester, tasks):
                return False
            current = requester
        return execution == execution_key(requester)

    def _calling_native_turn(self, caller, snapshots):
        """The caller's current turn in the current conversation, running on its own native claim."""
        bot = self._get('bots', caller['bot_id'])
        if (caller['status'] != 'working' or not caller.get('_native_turn_id')
                or bot.get('archived') or bot.get('_deleted')
                or caller.get('session_id') != bot.get('session_id')
                or caller.get('_conversation_epoch') != self._setting('conversation_epoch')):
            return False
        claim = caller.get('_kanban_claim') or {}
        card = snapshots.get(caller['id'], {})
        # A different native run (including a reviewer) is never our foreground.
        # Hermes gives context only to the claim of a retained outcome. That claim must be the caller's own outcome,
        # holding the caller's current input.
        return bool(claim.get('native_id') and claim.get('run_id')
                    and claim.get('external_id') == caller['id']
                    and (not claim.get('context') or (caller.get('_native_outcome_id') == claim['native_id']
                         and claim.get('input') and card.get('input_id') == claim['input'] == caller['_native_turn_id']))
                    and caller.get('_native_outcome_id') in (None, claim['native_id'])
                    and card.get('id') == claim['native_id'] and card.get('status') == 'running'
                    and card.get('claim_source_status') != 'review'
                    and card.get('current_run_id') == claim['run_id']
                    and (not card.get('worker_pid') or card['worker_pid'] == claim.get('pid')))

    def _approved_proposal_execution(self, source, target):
        """The person's answer to the source task's proposal started the target task. See _proposal_work_links."""
        # The source's own decisions, through the decisions_task index. The predicate checks the target and the answer.
        rows = self.db.execute(PROPOSAL_DECISIONS, (source['id'],))
        return any(self._proposal_execution(json.loads(row[0]), source, target) for row in rows)

    def _requester_return(self, callback, requester, tasks):
        """A return callback of the requester, with its producer's exact persisted delivery and route."""
        producer = tasks.get(callback.get('source_task_id'))
        return bool(callback.get('_is_return') and callback.get('_assignment_task_id') == requester['id']
                    and producer and callback.get('source_bot_id') == producer['bot_id']
                    and producer.get('source_bot_id') == requester['bot_id']
                    and (producer.get('_reply_to_task_id') or producer.get('source_task_id')) == requester['id']
                    and callback.get('exchange_id') == producer.get('exchange_id')
                    and any(d.get('source_task_id') == producer['id']
                            and d.get('target_bot_id') == callback['bot_id']
                            and d.get('receipt', {}).get('task_id') == callback['id']
                            and d.get('receipt', {}).get('target_bot_id') == callback['bot_id']
                            and d.get('receipt', {}).get('thread_id') == callback['thread_id']
                            and d.get('receipt', {}).get('status') in ('queued', 'paused')
                            for d in self._all('deliveries')))

    def _requester_callback_root(self, owner, tasks):
        """The requester's coordinating root when the owner's source task is one of its return callbacks.

        The callback runs in the root's execution, and the owner's result goes to the root.
        """
        source = tasks.get(owner.get('source_task_id'))
        if not source:
            return None
        root = self._work_execution_owner(source, tasks)
        if (root['id'] == source['id'] or owner.get('_reply_to_task_id') != root['id']
                or owner.get('source_bot_id') != root['bot_id']
                or not self._requester_return(source, root, tasks)):
            return None
        return root

    def _linked_owner_report_coordinator(self, owner, item, peer, reporter, peers, tasks, get_deliveries):
        """Authenticate a completed activity report without transferring PR ownership."""
        if (item['kind'] != 'work' or item['task_id'] != owner['id']
                or owner.get('source_task_id') not in tasks
                or peer['kind'] != 'pr' or peer.get('work_outcome_id') != item['id']
                or peer['task_id'] != owner['id'] or peer.get('_review_task_id') != owner['id']
                or peer['bot_id'] != owner['bot_id'] or not peer.get('url')
                or reporter['bot_id'] != owner['bot_id'] or reporter['status'] != 'completed'
                or reporter.get('_is_return') or reporter.get('_assignment_task_id') not in (None, reporter['id'])):
            return None
        coordinator = tasks.get(reporter.get('source_task_id'))
        if (not coordinator or coordinator['id'] in (owner['id'], owner.get('source_task_id'))
                or coordinator['bot_id'] != owner.get('source_bot_id')
                or reporter.get('source_bot_id') != coordinator['bot_id']
                or reporter.get('_reply_to_task_id') != coordinator['id']
                or any(reporter.get(k) != coordinator.get(k) for k in
                       ('thread_id', 'conversation_bot_id', 'exchange_id'))):
            return None
        deliveries = get_deliveries()

        def delivered(sender, recipient):
            return [d for d in deliveries if not d.get('_progress_only') and d.get('text')
                    and d.get('source_task_id') == sender['id']
                    and d.get('target_bot_id') == recipient['bot_id']
                    and d.get('receipt', {}).get('task_id') == recipient['id']
                    and d.get('receipt', {}).get('target_bot_id') == recipient['bot_id']
                    and d.get('receipt', {}).get('thread_id') == recipient['thread_id']
                    and d.get('receipt', {}).get('status') in ('queued', 'paused')]

        if not delivered(coordinator, reporter):
            return None
        for other in peers:
            callback = tasks.get(other.get('_activity_task_id'))
            if (other['kind'] != 'pr' or other.get('work_outcome_id') != item['id']
                    or other['task_id'] != coordinator['id'] or other.get('_review_task_id') != coordinator['id']
                    or other['bot_id'] != coordinator['bot_id']
                    or not other.get('url') or other['url'].lower() != peer['url'].lower()
                    or not callback):
                continue
            # Native owners receive dependency results on themselves, not on a
            # separate _is_return callback. Authenticate that route separately;
            # the caller still guards the coordinator's full native/task chain.
            direct_native = (callback['id'] == coordinator['id'] and coordinator.get('_native_outcome_id')
                and not coordinator.get('_is_return')
                and coordinator.get('_assignment_task_id') in (None, coordinator['id'])
                and (coordinator.get('_kanban_claim') or {}).get('native_id') in
                    (None, coordinator['_native_outcome_id']))
            returned_callback = (callback.get('_is_return')
                and callback.get('_assignment_task_id') == coordinator['id']
                and callback.get('source_task_id') == reporter['id']
                and callback.get('source_bot_id') == reporter['bot_id']
                and all(callback.get(k) == coordinator.get(k) for k in
                        ('bot_id', 'session_id', 'thread_id', 'conversation_bot_id', 'exchange_id')))
            if not (direct_native or returned_callback):
                continue
            # Old direct-native messages have no per-report return identity.
            # Keep them blocked: text/audience (also rebuilt by transcript migration)
            # cannot distinguish two same-bot reports. Legacy callbacks above do
            # retain source_task_id and remain eligible without new message fields.
            if any(m.get('sender_bot_id') == reporter['bot_id'] and m.get('result_status') == 'completed'
                   and not m.get('progress_only') and m.get('text') == delivery['text']
                   and (not direct_native or (m.get('_returning') is True
                        and m.get('_return_source_task_id') == reporter['id']
                        and m.get('_return_delivery_id') == delivery['id']))
                   for delivery in delivered(reporter, callback) for m in callback.get('_bot_messages', [])):
                return coordinator
        return None

    def _completed_peer_continuation(self, previous, linked, tasks, snapshots, get_deliveries):
        """Qualify a failed historical peer, never its successor or other branches."""
        import json
        from .service import digest, encoded
        from .work_capacity import execution_key
        successor = self._continued_work_task(previous, tasks)
        if (previous['status'] != 'failed' or not successor or successor['id'] not in linked
                or successor['status'] != 'completed' or successor.get('parent_task_id') != previous['id']
                or successor.get('_is_return') or successor.get('_continued_task_id')
                or any(execution_key(t) == execution_key(previous) and t['id'] not in linked
                       for t in tasks.values())):
            return False
        # Reciprocal execution links alone do not prove an authorized continuation.
        # Join the saved native envelope to the service's exact delivery.
        sender = tasks.get(successor.get('source_task_id'))
        relay = successor.get('_native_relay') or {}
        envelope = relay.get('envelope_id')
        if (not sender or not envelope or relay.get('sender_bot_id') != sender['bot_id']
                or successor.get('source_bot_id') != sender['bot_id']
                or not any(d['id'] == digest(encoded([sender['id'], envelope]))
                    and d.get('source_task_id') == sender['id']
                    and d.get('target_bot_id') == successor['bot_id'] and not d.get('_progress_only')
                    and d.get('receipt', {}).get('task_id') == successor['id']
                    and d.get('receipt', {}).get('target_bot_id') == successor['bot_id']
                    and d.get('receipt', {}).get('thread_id') == successor['thread_id']
                    and d.get('receipt', {}).get('status') == 'queued' for d in get_deliveries())):
            return False
        for task in (previous, successor):
            claim = task.get('_kanban_claim') or {}
            bound = task.get('_native_outcome_id') or claim.get('native_id')
            card = snapshots.get(task['id'], {})
            worker = self.running.get(task['id'])
            if (not bound or card.get('id') != bound or claim.get('native_id') != bound
                    or claim.get('external_id') != task['id']
                    or card.get('status') not in ('done', 'archived', 'blocked')
                    or card.get('block_kind') or card.get('current_run_id') or card.get('worker_pid')
                    or (worker and worker.is_alive()) or self._background_pending(task['id'])
                    or any(task.get(key) for key in ('_waiting_on', '_return_pending', '_native_reply_pending',
                        '_steering', '_live_messages', '_blocked_followup'))):
                return False
        claim = successor['_kanban_claim']
        card = snapshots[successor['id']]
        turn = successor.get('_native_turn_id')
        if (not turn or not claim.get('run_id') or card.get('status') != 'done'
                or card.get('input_id') != turn or claim.get('input') != turn
                or successor.get('_native_return_prepared') != [card['id'], turn]):
            return False
        # A current done card and a successful receipt must identify the same
        # producing turn/run. A prior success cannot qualify a resumed input.
        completed = False
        for receipt in self._native_task_receipts(successor['id'], turn_id=turn, state='completed'):
            if (receipt.get('action') or {}).get('name') != 'kanban_complete':
                continue
            try:
                result = json.loads(receipt.get('result', ''))
            except (ValueError, TypeError):
                continue
            if (isinstance(result, dict) and result.get('ok') is True
                    and result.get('task_id') == card['id'] and result.get('run_id') == claim['run_id']):
                completed = True
                break
        if not completed:
            return False
        # Preserve unknown reads as unknown. Only the exact read intents carried
        # into this successful recovery cease to be independent writer fences.
        saved = {r['id']: r for r in (successor.get('_self_recovery') or {}).get('unknown_tools', [])}
        for receipt in self._native_task_receipts(previous['id'], state='executing'):
            if ((receipt.get('action') or {}).get('name') not in ('search_files', 'read_file')
                    or receipt['id'] not in saved or any(receipt.get(key) != saved[receipt['id']].get(key)
                        for key in ('task_id', 'turn_id', 'action', 'created_at'))):
                return False
        return True

    def _retain_native_work(self, owner, native, snapshot_missing=None, snapshots=None, caller=None, selected_pr=None,
                            held_pause=None):
        from .service import ServiceError, now
        from .work_capacity import execution_key
        snapshots = self.kanban.tasks if snapshots is None else snapshots
        if owner.get('_work_context_id'):
            items = [self._get('artifacts', owner['_work_context_id'])]
        else:
            items = [a for a in self._all('artifacts') if a['kind'] in ('work', 'pr') and a['task_id'] == owner['id']]
        linked_parent = None
        audited_parent = None
        registered_parent = None
        retained = []
        source = self._get('tasks', owner['source_task_id']) if owner.get('source_task_id') else None
        pending_decisions = {d['task_id'] for d in self._all('decisions') if d['status'] == 'pending'}
        contexts = self._all('contexts')
        open_contexts = {c['task_id'] for c in contexts if c['status'] in ('queued', 'working')}
        deliveries = None

        def get_deliveries():
            nonlocal deliveries
            if deliveries is None:
                deliveries = self._all('deliveries')
            return deliveries

        def running_tool(task):
            # A tool call without a result blocks while its task can still run it. The process poller marks a local
            # command ended after every process of its execution is gone (see _mark_ended_tool_calls). Its unknown
            # effect stays in the receipt for the owner's next turn. A wait or a relay ends with a delivery instead.
            receipts = self._native_task_receipts(task['id'], state='executing')
            if not receipts:
                return False
            worker = self.running.get(task['id'])
            card = snapshots.get(task['id']) or {}
            return bool((worker and worker.is_alive()) or card.get('current_run_id') or card.get('worker_pid')
                        or any((r.get('action') or {}).get('name') not in LOCAL_COMMAND_TOOLS
                               or not r.get('_process_ended_at') for r in receipts))

        def quiet(task):
            # Every live signal that the writer guard reads. A failed task can still hold a running tool.
            worker = self.running.get(task['id'])
            bound = task.get('_native_outcome_id') or (task.get('_kanban_claim') or {}).get('native_id')
            card = snapshots.get(task['id'])
            # Only a done, archived, or plainly blocked card cannot start a writer by itself. A block check restarts a
            # capability or input block, and Hermes resumes a dependency or transient block, so a block kind is live.
            # A missing card counts as live. A live branch stays in the guard, which requests its missing card in one batch.
            native_live = bool(bound) and (card is None or card.get('id') != bound
                                           or card.get('status') not in ('done', 'archived', 'blocked')
                                           or (card.get('status') == 'blocked' and card.get('block_kind'))
                                           or card.get('current_run_id') or card.get('worker_pid'))
            return not (task.get('_waiting_on') or task.get('_return_pending') or (worker and worker.is_alive())
                        or self._background_pending(task['id']) or running_tool(task)
                        or native_live or task['id'] in pending_decisions or task['id'] in open_contexts)

        def live(task):
            return (task['status'] in self.ACTIVE or not quiet(task)
                    or (task['status'] == 'completed' and self._unfinished_work(task)))

        def audit_finished():
            # The audit path skips the audit's own writer checks, so the audit must have finished.
            # Coconut reads the audit's card only for an audited delegation.
            if (snapshot_missing and source['id'] not in snapshots
                    and (source.get('_native_outcome_id') or (source.get('_kanban_claim') or {}).get('native_id'))):
                snapshot_missing([source['id']])
            # A failed or cancelled audit is terminal. Its unfinished work items are expected, as for a branch.
            return quiet(source) and (source['status'] in ('failed', 'cancelled') or (
                source['status'] == 'completed' and not self._unfinished_work(source)))

        def delegate_report(candidate, root):
            # The delegate's own hidden report of the requester's Work. _sync_delegated_work saved each link,
            # and _register_work saved the reporting task. A missing or different link is not this report.
            if not (candidate['kind'] == 'work' and candidate.get('_hidden') and root['kind'] == 'work'
                    and candidate['bot_id'] == owner['bot_id'] and candidate['task_id'] == owner['id']
                    and candidate.get('_work_report_task_id') == owner['id']
                    and candidate.get('_work_parent_id') == root['id']
                    and candidate.get('_same_outcome_as') in (None, root['id'])
                    and candidate.get('_native_outcome_id') in (None, native['id'])
                    and root['bot_id'] != owner['bot_id'] and root['bot_id'] == owner.get('source_bot_id')):
                return False
            row = self.db.execute('SELECT data FROM tasks WHERE id=?', (root['task_id'],)).fetchone()
            parent = json.loads(row[0]) if row else None
            return bool(parent and candidate.get('_work_parent_task_id') ==
                        (parent.get('_assignment_task_id') or parent['id']))

        def tool_results(task, name):
            # Successful results that the service saved for the task's own turns. A context job is not its turn.
            jobs = {c['id'] for c in contexts if c['task_id'] == task['id']}
            for receipt in self._native_task_receipts(task['id'], state='completed'):
                action = receipt.get('action') or {}
                arguments = action.get('arguments') or {}
                if action.get('name') != name or receipt.get('turn_id') in jobs or arguments.get('board'):
                    continue
                try:
                    result = json.loads(receipt.get('result', ''))
                except (ValueError, TypeError):
                    continue
                if isinstance(result, dict) and result.get('ok') is True:
                    yield arguments, result

        def registered_delegation(root):
            # The Work's own task made its native outcome wait for a native run of this exact owner.
            # kanban_complete names the card of the owner's own run, and kanban_link names the edge that the Work's
            # task added. A shared PR URL, bot, comment, or ancestor does not prove that the Work waits for this owner.
            identity = root.get('_native_outcome_id')
            row = self.db.execute('SELECT data FROM tasks WHERE id=?', (root['task_id'],)).fetchone()
            work = json.loads(row[0]) if row else None
            if not (identity and work and work['bot_id'] == root['bot_id']
                    and work.get('_native_outcome_id') == identity and work.get('_work_context_id') == root['id']):
                return False
            # Hermes completes an implicit card only for a claimed worker, and that card is the worker's own. A turn
            # without a claim can complete any card that it names, so a named card is not proof of the owner's run.
            runs = {native['id']} | {result['task_id'] for arguments, result in tool_results(owner, 'kanban_complete')
                                     if result.get('task_id') and 'task_id' not in arguments}
            parents = {result['parent_id'] for arguments, result in tool_results(work, 'kanban_link')
                       if result.get('child_id') == arguments.get('child_id') == identity
                       and result.get('parent_id') == arguments.get('parent_id') and result['parent_id'] in runs}
            if not parents:
                return False
            # An unlink removes the edge from the board, so the Work's current card must still have it. A cached card
            # can predate the unlink. Read it, and the audit's card, once in this request, even when the cache has them.
            # The retry reads them outside the service lock. The scheduler then replaces its cache with that snapshot,
            # and a newer snapshot still wins over the copy that this request read.
            if snapshot_missing:
                snapshot_missing([work['id'], source['id']])
            card = snapshots.get(work['id']) or {}
            return card.get('id') == identity and bool(parents & set(card.get('parent_ids') or ()))

        reports = []
        for candidate in items:
            root = self._work_outcome_artifact(candidate)
            if delegate_report(candidate, root):
                reports.append(root)
                continue
            # A requester's source audit can delegate for an existing Work outcome of that requester.
            # _source_native_ids lists the native outcomes that the audit commented on. That alone does not
            # prove a delegation, so the Work's own task must also make this call.
            audited = bool(source and caller and caller['id'] == root['task_id'] and root.get('_native_outcome_id')
                           and root['_native_outcome_id'] in (source.get('_source_native_ids') or ())
                           and root['task_id'] != source['id'] and audit_finished())
            delegated_pr = (root['bot_id'] != owner['bot_id'] and candidate['kind'] == 'pr'
                    and candidate['bot_id'] == owner['bot_id'] and candidate['task_id'] == owner['id']
                    and candidate.get('work_outcome_id') == root['id'] and root['kind'] == 'work'
                    and root['bot_id'] == owner.get('source_bot_id'))
            # Another task of the requester can own the Work. Its registered wait for this owner links the two,
            # once the audit that delegated has finished. Every requester branch stays guarded below.
            registered = bool(delegated_pr and source and not audited and root['task_id'] != source['id']
                              and registered_delegation(root) and audit_finished())
            if delegated_pr and (root['task_id'] == owner.get('source_task_id') or audited or registered):
                # A delegated PR shares the requester's Work presentation, not its execution.
                linked_parent = root
                if audited:
                    audited_parent = root
                if registered:
                    registered_parent = root
                retained.append(candidate)
            else:
                retained.append(root)
        # A merged or closed delegated PR of the same Work is an earlier finished step of this owner. With exactly
        # one open delegated PR left, that PR is the outcome under continuation. Any other mix stays ambiguous.
        prs = [a for a in retained if linked_parent and a['kind'] == 'pr' and a.get('work_outcome_id') == linked_parent['id']]
        finished = [a for a in prs if a.get('pr_state') in ('merged', 'closed')]
        if finished and len(prs) - len(finished) == 1:
            retained = [a for a in retained if a not in finished]
        # Two or more delegated PRs of the same authenticated Work, all finished, and nothing else: no PR is the
        # outcome by itself, and Coconut chooses none. The caller can name one with retained_pr_url. That keeps the
        # existing PR artifact and approves nothing. A report of another Work stays in `reports` and keeps the refusal.
        all_finished = (len(finished) > 1 and len(finished) == len(prs) and all(a in prs for a in retained)
                        and all(root['id'] == linked_parent['id'] for root in reports))
        if selected_pr is not None:
            chosen = [a for a in finished if all_finished and a.get('url') and a['url'].lower() == selected_pr.lower()]
            if len(chosen) != 1:
                raise ServiceError(409, SELECTION_REFUSED)
            retained = chosen
        # The report and the one delegated PR of the same Work describe one execution and one outcome.
        # Two PRs, another Work, or no authenticated PR keep the report's Work, so the choice stays ambiguous.
        delegated = {a['id'] for a in retained if linked_parent and a['kind'] == 'pr'
                     and a.get('work_outcome_id') == linked_parent['id']}
        retained.extend(root for root in reports if not (len(delegated) == 1 and root['id'] == linked_parent['id']))
        items = list({a['id']: a for a in retained}.values())
        # Two or more open delegated PRs of the same authenticated Work, with distinct URLs and nothing else: this one
        # execution works on all of them for that Work. No PR is the outcome by itself, and Coconut chooses none. The
        # owner retains every PR and keeps no single Work item. A finished PR, a report, or another item stays ambiguous.
        members = {a['url'].lower(): a for a in items if a['kind'] == 'pr' and a.get('url')}
        grouped = (len(items) > 1 and len(members) == len(items) and not reports
                   and all(a['id'] in delegated and a.get('pr_state') not in ('merged', 'closed') for a in items))
        if len(items) > 1 and not grouped:
            raise ServiceError(409, FINISHED_PRS_AMBIGUOUS if all_finished else 'Choose one retained Work item for this native outcome.')
        if items:
            if any(a['kind'] not in ('work', 'pr') or a['bot_id'] != owner['bot_id']
                   or a.get('_native_outcome_id') not in (None, native['id']) for a in items):
                raise ServiceError(409, 'The retained Work item belongs to another native outcome.')
            # Grouped PRs all link to the same Work, so they share its peers, pause, and cancellation.
            item = items[0]
            peers = self._work_pause_peers(item)
            pause = self._work_pause(item, peers=peers)
            # Only a held-merge lane passes, and only under the exact hold that its own gate checked as the sole pause.
            if self._setting('work.cancelled.' + self._work_identity(item)) or (pause and pause['id'] != held_pause):
                raise ServiceError(409, 'This work is paused or cancelled.')
            tasks = {t['id']: t for t in self._all('tasks')}
            records = self._pending_completion_receipts()
            from . import routine_reports
            routine_scopes = {}
            for member in items:
                # Each scope belongs to one PR and its URL, so the scopes of grouped PRs do not overlap.
                routine_scopes.update(routine_reports.scopes(self, owner, member, peers, tasks, get_deliveries()))
            report_changes = {}
            peer_chains = []
            finished_roots = {}
            linked_activities = set()
            # The requester's coordinating task can differ from the task that delegated this work.
            requesters = {owner.get('source_task_id')} | ({linked_parent['task_id']} if linked_parent else set())
            requesters.discard(None)
            # A return callback source reports and calls for its root. Its other live branches stay guarded.
            requester_root = self._requester_callback_root(owner, tasks)
            requester_roots = {requester_root['id']} if requester_root else set()
            # Only the caller is not a competing writer. When the coordinator calls for its audit, the audit stays guarded.
            callers = ({linked_parent['task_id']} if linked_parent and linked_parent['task_id'] != owner.get('source_task_id')
                       else {owner.get('source_task_id')})
            # A registered Work can be continued by the owner or a requester callback. The audit and the Work's
            # task stay guarded unless one of them makes this call. A requester's return callback can be the foreground.
            foregrounds = set(requester_roots)
            if registered_parent:
                callers = {caller['id']} & requesters if caller else set()
                foregrounds |= requesters
            foregrounds |= callers
            if audited_parent or registered_parent:
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
                peer_chains.append((item, source, linked, native_tasks, True, False))
            for peer in peers:
                if linked_parent and peer['id'] == linked_parent['id']:
                    # The coordinating Work may remain active while its delegated PR resumes.
                    # Other PRs linked to it remain independent writer/review peers.
                    continue
                # A peer of a grouped PR's URL is checked against that PR, exactly as for a single retained PR. A peer of
                # any other URL matches no grouped PR, so it stays a writer for every one of them.
                item = members.get((peer.get('url') or '').lower(), items[0])
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
                    # The guarded requester's report of a merged or closed PR of the same Work. That PR has no writer
                    # left. The requester is the direct requester, or the proven caller of an audited or registered
                    # delegation. Only that requester's own run state is exempt below; its decisions, unknown tools,
                    # returns, and every other branch stay in the guard.
                    guarded_requester = linked_parent and (linked_parent['task_id'] == owner.get('source_task_id') or (
                        (audited_parent or registered_parent) and linked_parent['task_id'] in callers))
                    finished_report = bool(field == '_review_task_id' and peer['kind'] == 'pr' and guarded_requester
                        and peer.get('pr_state') in ('merged', 'closed') and peer['bot_id'] == linked_parent['bot_id']
                        and peer.get('work_outcome_id') == linked_parent['id'] and peer['task_id'] == linked_parent['task_id']
                        and peer[field] == linked_parent['task_id'])
                    reporter = tasks[peer[field]]
                    if execution_key(reporter) == execution_key(owner):
                        continue
                    requester_activity = (requester_report and self._work_execution_owner(reporter, tasks)['id']
                        in {owner.get('source_task_id'), *requester_roots})
                    # The direct requester's own activity on this delegated PR of its Work. The requester stays
                    # in the guard. Only its own calling turn can be the foreground below.
                    linked_activity = bool(field == '_activity_task_id' and linked_parent and item['kind'] == 'pr'
                        and linked_parent['task_id'] == owner.get('source_task_id')
                        and item.get('work_outcome_id') == peer.get('work_outcome_id') == linked_parent['id']
                        and peer['kind'] == 'pr' and peer['bot_id'] == linked_parent['bot_id']
                        and peer['task_id'] == linked_parent['task_id']
                        and peer.get('url') and item.get('url') and peer['url'].lower() == item['url'].lower()
                        and self._work_execution_owner(reporter, tasks)['id'] == owner['source_task_id'])
                    if linked_activity:
                        linked_activities.add((peer['id'], reporter['id']))
                    reporter_source = tasks.get(reporter.get('source_task_id'))
                    owner_report = (peer['id'] == item['id'] and peer['task_id'] == owner['id']
                        and reporter['bot_id'] == owner['bot_id']
                        and reporter.get('_reply_to_task_id') in requesters
                        and reporter_source is not None
                        and self._work_execution_owner(reporter_source, tasks)['id'] in requesters)
                    passive_report = (field == '_activity_task_id' and peer['kind'] == 'pr'
                        and peer.get('url') and item.get('url')
                        and peer['url'].lower() == item['url'].lower()
                        and (requester_activity or owner_report or linked_activity))
                    report_coordinator = (self._linked_owner_report_coordinator(
                        owner, item, peer, reporter, peers, tasks, get_deliveries) if field == '_activity_task_id' else None)
                    # A passive same-PR report belongs to the original
                    # requester, not to an independent writer. Keep all of
                    # that requester's other branches in the guard.
                    root = tasks[owner['source_task_id']] if passive_report else self._work_execution_owner(reporter, tasks)
                    linked = self._work_chain(root, tasks, records, include_continuations=True)
                    if passive_report:
                        # The coordinating requester task has its own branches. Keep its live ones in the guard too.
                        # A failed or cancelled branch with no live signal cannot write. Its unfinished work items
                        # are expected. A completed branch with unfinished work items stays guarded.
                        for identity in (requesters | requester_roots) - {root['id']}:
                            if identity in tasks:
                                linked.update({key: value for key, value in self._work_chain(
                                    tasks[identity], tasks, records, include_continuations=True).items()
                                    if value[0]['status'] in self.ACTIVE or not quiet(value[0])
                                    or (value[0]['status'] == 'completed' and self._unfinished_work(value[0]))})
                    linked[root['id']] = (root, 0)
                    linked[reporter['id']] = (reporter, 0)
                    if finished_report and root['id'] == reporter['id']:
                        linked.pop(owner['id'], None)
                        finished_roots[(peer['id'], reporter['id'])] = root['id']
                    if owner_report and reporter_source and reporter_source['id'] not in (owner['id'], *requesters):
                        linked[reporter_source['id']] = (reporter_source, 0)
                    if passive_report:
                        # The coordinator and the owner under continuation
                        # are not competing writers. Their other descendants
                        # still need terminal claims, returns, and decisions.
                        for identity in (*callers, owner['id']):
                            value = linked.pop(identity, None)
                            if requester_roots and identity == source['id'] and value and live(value[0]):
                                # A root's source callback is an earlier turn, not the caller. It stays guarded while live.
                                linked[identity] = value
                            elif linked_activity and value and identity != owner['id']:
                                # The requester reported this activity itself, so it is not exempt as a caller.
                                linked[identity] = value
                            elif (value and identity != owner['id'] and caller and caller['id'] in linked
                                    and caller['bot_id'] == value[0]['bot_id']
                                    and execution_key(caller) != execution_key(value[0])):
                                # The requester's task in another execution, such as an approved proposal, makes this
                                # call. The requester is not calling, so its own decisions, tools, and returns still apply.
                                linked[identity] = value
                        if audited_parent:
                            # The same quiet-terminal rule applies to the audit's
                            # own chain, whether or not a passive report exists.
                            linked = {key: value for key, value in linked.items()
                                      if value[0]['status'] in self.ACTIVE or not quiet(value[0])
                                      or (value[0]['status'] == 'completed' and self._unfinished_work(value[0]))}
                    if report_coordinator:
                        requester_chain = self._work_chain(source, tasks, records, include_continuations=True)
                        requester_chain.pop(owner['id'], None)
                        # The original requester can still be active when another
                        # task invokes continuation. Only its own call is exempt.
                        if caller and caller['id'] in callers:
                            requester_chain.pop(caller['id'], None)
                        linked.update({key: value for key, value in requester_chain.items()
                                       if value[0]['status'] not in ('failed', 'cancelled')
                                       or value[0].get('_native_reply_pending') or not quiet(value[0])})
                        # Unlike the original requester, this coordinator and its
                        # callbacks must finish too. Do not exempt the caller.
                        linked.update(self._work_chain(report_coordinator, tasks, records, include_continuations=True))
                        linked[report_coordinator['id']] = (report_coordinator, 0)
                    routine_scope = routine_scopes.get((peer['id'], reporter['id']))
                    if routine_scope:
                        # Include the original requester and ALL report branches,
                        # not just the two tasks whose presentation links matched.
                        roots = {source['id'], routine_scope['coordinator_id'],
                                 *routine_scope['sibling_reporter_ids']}
                        for identity in roots:
                            linked.update(self._work_chain(tasks[identity], tasks, records, include_continuations=True))
                            linked[identity] = (tasks[identity], 0)
                        for task in tasks.values():
                            if task.get('_assignment_task_id') in roots or execution_key(task) in roots:
                                linked.setdefault(task['id'], (task, 0))
                                linked.update(self._work_chain(task, tasks, records, include_continuations=True))
                        linked.pop(owner['id'], None)
                    native_tasks = [t for t, _ in linked.values()
                                    if t.get('_native_outcome_id') or t.get('_kanban_claim')]
                    peer_chains.append((peer, reporter, linked, native_tasks, passive_report, bool(report_coordinator)))
            for _, _, linked, native_tasks, _, _ in peer_chains:
                # Failed execution roots do not own their siblings in _work_chain.
                # Before qualifying their history, guard every sibling and its
                # branches too, including input with no callback/wait edge.
                recovering = {execution_key(t) for t, _ in linked.values()
                              if t['status'] == 'failed' and self._continued_work_task(t, tasks)}
                for sibling in tasks.values():
                    if execution_key(sibling) in recovering:
                        linked.update(self._work_chain(sibling, tasks, records, include_continuations=True))
                        linked.setdefault(sibling['id'], (sibling, 0))
                native_tasks[:] = [t for t, _ in linked.values()
                                   if t.get('_native_outcome_id') or t.get('_kanban_claim')]
            missing = sorted({t['id'] for _, _, _, native_tasks, _, _ in peer_chains for t in native_tasks
                              if t['id'] not in snapshots})
            if missing and snapshot_missing:
                snapshot_missing(missing)
            for peer, reporter, linked, native_tasks, passive_report, linked_report in peer_chains:
                foreground = (caller['id'] if passive_report and caller and caller['id'] in linked
                    and any(self._requester_callback_foreground(caller, tasks.get(identity), tasks, snapshots)
                            for identity in foregrounds) else None)
                finished_root = finished_roots.get((peer['id'], reporter['id']))
                foreground = foreground or finished_root
                if ((peer['id'], reporter['id']) in linked_activities and not foreground and caller
                        and caller['id'] == owner['source_task_id'] and caller['id'] in linked
                        and self._calling_native_turn(caller, snapshots)):
                    # The direct requester's own native turn makes this call. Its waits, returns,
                    # decisions, unknown tools, and other branches stay in the guard.
                    foreground = caller['id']
                routine_scope = routine_scopes.get((peer['id'], reporter['id']))
                projection = (routine_reports.project(self, routine_scope, tasks, snapshots, get_deliveries(), caller)
                              if routine_scope else None)
                if projection:
                    if running_tool(owner):
                        raise ServiceError(409, 'Wait for the original owner to finish its tool call.')
                    changes, current = projection
                    report_changes.update(changes)
                    linked = {key: (changes.get(key, task), depth) for key, (task, depth) in linked.items()}
                    foreground = current
                    linked_report = True
                historical = {t['id'] for t, _ in linked.values() if self._completed_peer_continuation(
                    t, linked, tasks, snapshots, get_deliveries)}
                # A delivered result starts the caller's own turn (_resume_process_task) and stays its
                # _background_completion while that turn runs. That input is not pending work. Undelivered waits, relays,
                # and processes of the caller, and every other turn of its assignment, still count.
                own_turn = (foreground if caller and foreground == caller['id']
                            and self._calling_native_turn(caller, snapshots) else None)
                native_pending = any(
                    (snapshot := snapshots.get(t['id'], {})).get('status') != 'done'
                    or snapshot.get('id') != (t.get('_native_outcome_id') or t['_kanban_claim'].get('native_id'))
                    or snapshot.get('current_run_id') or snapshot.get('worker_pid')
                    for t in native_tasks if t['id'] != foreground and t['id'] not in historical)
                if (native_pending or peer['kind'] != 'pr'
                        or ((peer['bot_id'] == owner['bot_id'] or reporter['bot_id'] == owner['bot_id'])
                            and not (passive_report or linked_report))
                        or any((t['status'] != 'completed' and t['id'] != foreground and t['id'] not in historical)
                               or [i for i in t.get('_waiting_on') or [] if not (t['id'] == finished_root and i == owner['id'])]
                               or t.get('_return_pending')
                               or ((linked_report or historical) and t.get('_native_reply_pending'))
                               or self._task_paused(t) or self._get('exchanges', t['exchange_id']).get('cancelled')
                               or (self._background_pending(t['id'], finishing_task_id=own_turn) if t['id'] == own_turn
                                   else self._background_pending(t['id']))
                               or self._unfinished_work(t)
                               or (t['id'] != foreground and self.running.get(t['id']) and self.running[t['id']].is_alive())
                               or (t['id'] not in historical and running_tool(t))
                               or ((t['id'] == foreground or historical or projection) and (self._task_work_is_paused(t)
                                   or t.get('_steering') or t.get('_live_messages') or t.get('_blocked_followup')
                                   or t.get('_native_reply_pending')))
                               for t, _ in linked.values())
                        or any(d['status'] == 'pending' and d['task_id'] in linked for d in self._all('decisions'))
                        or any(c['status'] in ('queued', 'working') and c['task_id'] in linked
                               for c in self._all('contexts'))):
                    raise ServiceError(409, 'This Work item has another execution. Continue its current owner.')
            # Admission and consumption commit together. A failed peer check or
            # deferred snapshot rolls back without retiring any input or wait.
            for identity, changed in report_changes.items():
                if changed != tasks[identity]:
                    self._put('tasks', changed)
                    self._event('task.updated', self._public(changed), changed['bot_id'], identity)
            for member in items:
                member['_native_outcome_id'] = native['id']
        else:
            title = native.get('title') or owner['title']
            item = dict(id=str(uuid5(UUID(owner['id']), 'work')), bot_id=owner['bot_id'],
                task_id=owner['id'], kind='work', title=title, status='queued', activity='Queued',
                preview='', url=None, created_at=now(), observed_at=now(),
                _work_key='native-' + native['id'], _native_outcome_id=native['id'],
                completion_condition=title)
        # Grouped PRs keep their own task links. Messages about them reach their Work, and a reply to the finished owner
        # finds no single item to move, so it refuses rather than splitting them (_native_continuation_item).
        if not grouped:
            owner['_work_context_id'] = item['id']
        for member in items or [item]:
            self._put('artifacts', member)
            self._event('artifact.updated' if items else 'artifact.created', self._public(member), owner['bot_id'], owner['id'])

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
        cards = {}
        with self.lock:
            source = self._get('tasks', source_id)
            invocation = (source.get('_native_turn_id'), source.get('_kanban_claim'))

        def snapshot_missing(identities):
            missing = set(identities) - requested
            if missing:
                raise _NativeSnapshotsNeeded(sorted(missing))

        def completion(identity):
            if identity not in cards:
                raise _NativeCardNeeded(identity)
            return cards[identity]

        # The scheduler holds its adapter lock while spawn takes self.lock.
        # Roll back and release the service transaction before querying it, then
        # re-read all ownership, peer links and pause guards in a new transaction.
        for attempt in range(5):
            try:
                return self._continue_native_work(source_id, payload, snapshot_missing, snapshots, invocation, completion)
            except _NativeCardNeeded as pending:
                if attempt == 4:
                    break
                # An adapter that cannot answer proves nothing about the card.
                try:
                    cards[pending.identity] = self.kanban.call({'op': 'completion', 'id': pending.identity}).get('card') or {}
                except Exception:
                    cards[pending.identity] = {}
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

    def _execution_tasks(self, key):
        """Return the tasks with this execution key (see execution_key), in row order.

        A key is an execution session, a bot, or a status task. Indexes find the candidates, so a continuation does not
        parse the whole task table under the service lock.
        """
        from .work_capacity import execution_key
        rows = self.db.execute(
            "SELECT data FROM tasks WHERE json_extract(data,'$._execution_session_id')=? OR json_extract(data,'$.bot_id')=? "
            "OR id=? ORDER BY rowid", (key, key, key))
        return [task for task in (json.loads(row[0]) for row in rows) if execution_key(task) == key]

    def _continue_native_work(self, source_id, payload, snapshot_missing, snapshots, invocation, completion):
        from .service import ServiceError, now, uid, digest, encoded, require_text
        from .work_capacity import execution_key
        with self.transaction():
            # Read one cache generation for both owner and peer admission. Newer
            # cached claims win over the targeted fallback; eviction cannot erase it.
            snapshots = snapshots | (self.kanban.tasks if self.kanban else {})
            source = self._get('tasks', source_id)
            if (source.get('_native_turn_id'), source.get('_kanban_claim')) != invocation:
                raise ServiceError(409, 'The calling turn changed while checking this work. Read its current activity and retry.')
            if source['status'] != 'working' or self._task_paused(source):
                raise ServiceError(409, 'This task cannot continue work now.')
            identity = require_text(payload, 'task_id', 100)
            text = require_text(payload, 'message', 16000)
            lane = technical_lane_request(payload)
            parked = technical_lane_parked(lane)
            held_merge = bool(lane and lane.get('held_merge_pr_url'))
            selected_pr = require_text(payload, 'retained_pr_url', RETAINED_PR_URL_LIMIT) if 'retained_pr_url' in payload else None
            reopen = payload.get('continue_closed_source', False)
            if type(reopen) is not bool:
                raise ServiceError(400, 'continue_closed_source must be true or false.')
            key = digest(encoded(['continue_work', source_id, source.get('_native_turn_id'), identity, text] + ([lane] if lane else [])
                                 + ([{'retained_pr_url': selected_pr}] if selected_pr is not None else [])
                                 + ([{'continue_closed_source': True}] if reopen else [])))
            previous = self.db.execute('SELECT data FROM deliveries WHERE id=?', (key,)).fetchone()
            if previous:
                return json.loads(previous[0])['receipt']
            owner, native = self._native_continuation_owner(identity, snapshot_missing, snapshots)
            identity = native['id']
            if source['bot_id'] not in (owner['bot_id'], owner.get('source_bot_id')):
                raise ServiceError(403, 'Only the owner or its requesting bot can continue this work.')
            if owner.get('_native_job') and not owner.get('_native_job_finished'):
                raise ServiceError(409, 'Wait for the original routine occurrence to finish reconciling with Hermes.')
            bot = self._get('bots', owner['bot_id'])
            if (self._task_paused(owner) or owner['session_id'] != bot['session_id']
                    or owner.get('_conversation_epoch') != self._setting('conversation_epoch')
                    or self._continuation_blocked(owner)
                    or self._get('exchanges', owner['exchange_id']).get('cancelled')
                    or (self._task_work_is_paused(owner) and not parked)):
                # Only an explicit declined- or held-merge lane reads past a pause, and its own gate below checks it.
                raise ServiceError(409, 'This work is paused, cancelled, or belongs to an earlier conversation.')
            scheduled = native.get('status') == 'scheduled'
            review = native.get('status') == 'review'
            # A queued owner keeps a scheduled card while Coconut holds its dispatch (see _kanban_rows).
            queued = native.get('status') in ('ready', 'scheduled') and owner['status'] == 'queued'
            # Coconut archives the task of a failed request itself. Its routine occurrence still continues, and so does
            # a failed ordinary request. So does the scheduled card of a continuation of a successful occurrence or a
            # completed ordinary request, and the original card of a completed relayed request that its worker
            # scheduled. The settle pass archives those too.
            settled = native.get('status') == 'archived' and (
                self.settled_failed_occurrence(owner, native, lambda: completion(identity))
                or self.settled_failed_request(owner, native, lambda: completion(identity))
                or self.settled_scheduled_continuation(owner, native, lambda: completion(identity)))
            # The assignment pass ends idle work whose sources closed, and that stays the rule. Only the owner or its
            # requesting bot can still ask for that same work, and only explicitly.
            closed = (native.get('status') == 'archived' and not settled
                      and self.settled_closed_source(owner, native, lambda: completion(identity)))
            if closed and not reopen:
                raise ServiceError(409, CLOSED_SOURCE_REFUSED)
            if reopen and not closed:
                raise ServiceError(409, 'continue_closed_source applies only to work that Coconut ended because its sources closed.')
            settled = settled or closed
            # A later audit of the same source item may still wait for the person. Its answer comes first.
            if settled and self.pending_source_decision(owner):
                raise ServiceError(409, 'A decision about the same source is pending. It must be answered before this work continues.')
            if native.get('status') not in ('triage', 'blocked', 'done', 'scheduled', 'review') and not queued and not settled:
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
            related = self._execution_tasks(execution_key(owner))
            related_ids = {t['id'] for t in related}
            # Only an explicit lane may leave one exact question pending, or work under one declined or held merge.
            # See technical_lane.py.
            gate = self._technical_lane_gate(owner, native, lane, related) if lane and not parked else None
            held = ((self._held_merge_gate if held_merge else self._declined_merge_gate)(owner, native, lane, related)
                    if parked else None)
            if (owner['status'] not in ('completed', 'failed', 'waiting') and not
                    (scheduled and owner['status'] == 'scheduled') and not queued and not gate and not closed) or owner.get('_waiting_on'):
                raise ServiceError(409, 'Wait for this task and its existing dependencies.')
            if owner.get('_return_pending'):
                raise ServiceError(409, 'Wait for the previous result to be delivered before continuing this work.')
            if any(d['status'] == 'pending' and d['task_id'] in {t['id'] for t in related} and d['id'] != (gate or {}).get('id')
                   for d in self._all('decisions')):
                raise ServiceError(409, 'The existing decision must be answered before work continues.')
            worker = self.running.get(owner['id'])
            active_worker = bool(worker and worker.is_alive())
            # Review can hold a reviewer claim or a human pause. Only fresh input
            # to a quiescent execution may reopen it; decisions remain gated below.
            if ((scheduled or queued or review) and (native.get('current_run_id') or native.get('worker_pid') or active_worker)) or any(
                    (t['id'] != owner['id'] and t['status'] in self.ACTIVE)
                    or self._background_pending(t['id']) for t in related):
                raise ServiceError(409, 'This execution has active work. Wait for its result.')
            if queued and scheduled:
                # Coconut also holds it while a callback runs or still owes it a result (see _owed_helper_return).
                if any(c['task_id'] in related_ids and c['status'] in ('queued', 'working') for c in self._all('contexts')) or any(
                        t.get('_reply_to_task_id') in related_ids and (t['status'] in self.ACTIVE or t.get('_return_pending')
                                                                       or t.get('_native_reply_pending')) for t in self._all('tasks')):
                    raise ServiceError(409, 'This execution has active work. Wait for its result.')
                # Advancing the input would drop an answer or review that the held turn has not read yet. A process
                # result is held by _background_pending above. Fresh input replaces an automatic resume.
                if owner.get('_resume_decision') or owner.get('_pr_review'):
                    raise ServiceError(409, 'Wait for this task to start its queued decision or review before continuing this work.')
            if not owner.get('_native_outcome_id'):
                self._retain_native_work(owner, native, snapshot_missing, snapshots, caller=source, selected_pr=selected_pr,
                                         held_pause=held['id'] if held_merge else None)
            elif selected_pr is not None:
                # The owner already retains its Work item. A selection cannot replace it.
                raise ServiceError(409, SELECTION_REFUSED)
            owner['_native_outcome_id'] = identity
            self._restore_native_work_owner(owner)
            previous_lane = owner.pop('_technical_lane', None)
            if gate or held:
                owner['_technical_lane'] = dict(lane, native_task_id=identity, requester_task_id=source_id,
                                                requester_bot_id=source['bot_id'], admitted_at=now(),
                                                **self._technical_checkpoint(owner, previous_lane))
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
            if gate or held:
                self._technical_turn_advanced(owner)
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
                text=text, receipt=receipt, turn_id=owner['_native_turn_id'], created_at=now()))
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
        # Save the answer of a finished legacy row once. Later conversations cannot rewrite completed history.
        # The old reducer wrote the row status itself, so the status is the answer unless its owner is unfinished
        # or a decision is pending.
        candidates = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM artifacts WHERE json_extract(data,'$.kind')='work' "
            "AND json_extract(data,'$.status')='completed' AND json_extract(data,'$._native_outcome_id') IS NULL "
            "AND json_extract(data,'$._same_outcome_as') IS NULL AND NOT coalesce(json_extract(data,'$._hidden'),0)")]
        candidates = [a for a in candidates if not self._retained_terminal_work(a)]
        if not candidates:
            return
        waiting = {row[0] for row in self.db.execute(
            "SELECT json_extract(data,'$.task_id') FROM decisions WHERE json_extract(data,'$.status')='pending' "
            "AND NOT coalesce(json_extract(data,'$._hidden'),0) AND NOT coalesce(json_extract(data,'$._deleted'),0)")}
        for item in candidates:
            owner = self._get('tasks', item['task_id'])
            if owner['status'] != 'completed' or owner['id'] in waiting:
                continue
            evidence = item.get('completion_evidence') or (None if item.get('_automatic_work') else item.get('activity')) or owner.get('summary')
            item['_completed_history'] = dict(report=self._work_history_key(item), evidence=evidence)
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
        with self._shared_reads():
            return self._migrate_native_work_shared()

    def _migrate_native_work_shared(self):
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
            # The status indexes find the few open cards and requests. The tables hold thousands of finished rows.
            decisions = [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending'")]
            pending = any(d['task_id'] == owner['id'] or (d.get('merge_pr_url') or '').lower() in pr_urls for d in decisions)
            waiting = pending or self._background_pending(owner['id']) or bool(owner.get('_waiting_on'))
            waiting = waiting or owner['status'] in ('scheduled', 'paused')
            tasks = self._task_map()
            records = self._shared_work_receipts()
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
                    or any(d['task_id'] in related for d in decisions)
                    or any(row[0] in attempts for row in self.db.execute(
                        "SELECT json_extract(data,'$.task_id') FROM contexts WHERE json_extract(data,'$.status') IN ('queued','working')"))):
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
        """Start the queued turns of the bots' conversations. One conversation runs one turn at a time.

        A person's turn goes first (_dispatch_order) and never waits for bot traffic: it counts only the person's turns
        against capacity, and a bot-originated turn that the gateway runs in the same conversation stops for it and
        runs again after it (gateway_chat.preempt_for_person).
        """
        from .work_capacity import chat_turn, execution_key, person_turn
        running = {i: self._get('tasks', i) for i in self.running}
        active = {execution_key(task) for task in running.values()}
        active.update(execution_key(self._get('tasks', self._get('contexts', i)['task_id'])) for i in self.context_threads)
        if self._kanban_claiming:
            # A native pass may be claiming a ready row of the bot's own execution, such as a conversation turn that owns
            # an outcome. Its turn starts only after the claim, so no chat turn of that execution starts meanwhile.
            active.update(self._kanban_claim_outcomes)
        # Share host capacity, retaining one reserved slot for direct replies. An audit of a work source that the
        # gateway runs has its own capacity: the Kanban pass bounds it, and it does not delay the person's chats.
        audits = 0
        if self.gateway_enabled():
            from .gateway_bridge import gateway_work_turn
            audits = sum(1 for i in self.running if self._get('tasks', i).get('_source_audit_prompt')
                         and gateway_work_turn(self._get('tasks', i)))
        slots = max(0, capacity + 1 - (len(self.running) - audits) - len(self.context_threads))
        if self._kanban_dispatching:
            foreground = sum(bool(chat_turn(task) or task.get('_work_status_only')) for task in running.values())
            slots = min(slots, max(0, 1 - foreground))
        # The person's turns share the capacity only with each other, never with bot traffic.
        person_slots = max(0, capacity + 1 - sum(1 for task in running.values() if person_turn(task)))
        candidates = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM tasks WHERE json_extract(data,'$._chat_session') "
            "AND json_extract(data,'$.status') IN ('queued','scheduled') ORDER BY rowid")]
        for task in self._dispatch_order(candidates):
            person = person_turn(task)
            if not (person_slots if person else slots):
                continue
            # A conversation turn that owns a native outcome starts from the outcome's claim (chat_turn).
            if not chat_turn(task):
                continue
            bot = self._get('bots', task['bot_id'])
            if bot['archived'] or bot.get('_deleted') or not self._dispatch_eligible(task, time.time()):
                continue
            key = execution_key(task)
            if key in active:
                if person:
                    # The person's message stops a bot-originated turn of this conversation. It starts when that
                    # turn has stopped, and the stopped turn runs again after it.
                    for identity, other in running.items():
                        if execution_key(other) == key:
                            self.preempt_for_person(identity, task['id'])
                    # Later turns of this conversation wait for the person's turn.
                    active.add(key)
                continue
            worker = threading.Thread(target=self._run_task, args=(task['id'],), daemon=True, name='conversation-' + task['id'][:8])
            self.running[task['id']] = worker
            active.add(key)
            worker.start()
            if person:
                person_slots -= 1
            else:
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

    def _block_check_related(self, task):
        # Without an execution session, execution_key falls back to the bot. Keep the check to this outcome.
        session = task.get('_execution_session_id')
        return {t['id'] for t in self._all('tasks') if session and t.get('_execution_session_id') == session} | {task['id']}

    def _block_check_delegates(self, task, card, related):
        # Map each delegate execution this outcome waits for to the tasks whose human direction ends its pause.
        # This outcome's own execution added a native edge to the delegate's outcome, the board still has that edge,
        # and the service delivered the delegate's request from this bot through an audited native relay, to the
        # outcome's owner or to the intake whose kanban_create made it (see _block_check_relay_lineage).
        # An ancestor, sibling, comment, or bot report does not prove that this outcome waits for that delegate.
        identity = task.get('_native_outcome_id')
        parents = set(card.get('parent_ids') or ()) if card and identity and card.get('id') == identity else set()
        if not parents:
            return {}
        tasks = {t['id']: t for t in self._all('tasks')}
        jobs = {c['id'] for c in self._all('contexts') if c['task_id'] in related}
        linked = set()
        for owner in related:
            if tasks.get(owner, {}).get('bot_id') != task['bot_id']:
                continue
            for receipt in self._native_task_receipts(owner, state='completed'):
                action = receipt.get('action') or {}
                arguments = action.get('arguments') or {}
                if action.get('name') != 'kanban_link' or receipt.get('turn_id') in jobs or arguments.get('board'):
                    continue
                try:
                    result = json.loads(receipt.get('result', ''))
                except (ValueError, TypeError):
                    continue
                if (isinstance(result, dict) and result.get('ok') is True
                        and result.get('child_id') == arguments.get('child_id') == identity
                        and result.get('parent_id') == arguments.get('parent_id') and result['parent_id'] in parents):
                    linked.add(result['parent_id'])
        delegates = {}
        for parent in linked:
            owners = [t for t in tasks.values() if t.get('_native_outcome_id') == parent]
            delegate = owners[0] if len(owners) == 1 else {}
            if not (delegate and delegate['bot_id'] != task['bot_id'] and delegate.get('_execution_session_id') == delegate['id']):
                continue
            lineage = self._block_check_relay_lineage(task, delegate, tasks)
            if not lineage:
                continue
            intake = lineage[-1]
            source = tasks[intake['source_task_id']]
            # The answers stay those of the outcome's own execution. Human direction counts from that execution, the
            # tasks that created it, the intake that received the relay, and the requester that sent it.
            execution = {t['id'] for t in tasks.values()
                         if t.get('_execution_session_id') == delegate['id'] and t['bot_id'] == delegate['bot_id']}
            sessions = {(t.get('_execution_session_id') or t['id'], t['bot_id']) for t in lineage[1:] + [source]}
            direction = {t['id'] for t in tasks.values() if (t.get('_execution_session_id') or t['id'], t['bot_id']) in sessions}
            delegates[delegate['id']] = dict(bot_id=delegate['bot_id'], tasks=execution | {delegate['id']},
                                             direction=execution | {delegate['id']} | direction
                                             | {t['id'] for t in lineage} | {source['id']})
        return delegates

    def _block_check_relay_lineage(self, task, owner, tasks, depth=0):
        # Return [owner, ..., intake] when the intake received an audited native relay from this bot. An owner that
        # holds the relay is its own intake. Otherwise _adopt_native_outcomes made the owner for an outcome that its
        # parent task created: the owner ID derives from that parent and the outcome, the parent's own kanban_create
        # receipt returned that outcome, and the callback owner that adoption copied is the parent's verified intake.
        from .service import digest, encoded
        if depth > 8:
            return None
        relay = owner.get('_native_relay') or {}
        if relay:
            source = tasks.get(owner.get('source_task_id'), {})
            if not (owner['bot_id'] != task['bot_id'] and not owner.get('_is_return') and relay.get('envelope_id')
                    and owner.get('source_bot_id') == relay.get('sender_bot_id') == task['bot_id']
                    and source.get('bot_id') == task['bot_id'] and source.get('session_id') == task.get('session_id')):
                return None
            row = self._read_one('SELECT data FROM bots WHERE id=?', (owner['bot_id'],))
            bot = json.loads(row[0]) if row else {}
            row = self._read_one('SELECT data FROM deliveries WHERE id=?', (digest(encoded([source['id'], relay['envelope_id']])),))
            delivery = json.loads(row[0]) if row else {}
            if (not bot or bot.get('_deleted') or owner.get('session_id') != bot.get('session_id')
                    or delivery.get('source_task_id') != source['id'] or delivery.get('target_bot_id') != owner['bot_id']
                    or (delivery.get('receipt') or {}).get('task_id') != owner['id']):
                return None
            # The sender's own relay call returned this intake for this envelope.
            for result in self._block_check_results(source['id'], 'message_agent'):
                if result.get('task_id') == owner['id'] and result.get('delivery_id') == relay['envelope_id']:
                    return [owner]
            return None
        origin = tasks.get(owner.get('parent_task_id'), {})
        native = owner.get('_native_outcome_id')
        try:
            derived = bool(origin and native) and owner['id'] == outcome_owner_id(origin['id'], native)
        except ValueError:
            derived = False
        if not (derived and owner.get('_execution_session_id') == owner['id'] and origin['bot_id'] == owner['bot_id']
                and origin.get('session_id') == owner.get('session_id') and origin.get('source_bot_id') == task['bot_id']
                and owner.get('source_bot_id') == task['bot_id']):
            return None
        created = False
        for receipt in self._native_task_receipts(origin['id'], state='completed'):
            arguments = (receipt.get('action') or {}).get('arguments') or {}
            if ((receipt.get('action') or {}).get('name') != 'kanban_create' or arguments.get('session_id') != origin['id']
                    or arguments.get('tenant') != origin['bot_id']
                    or not str(arguments.get('idempotency_key', '')).startswith('coconut:' + origin['id'] + ':')):
                continue
            try:
                result = json.loads(receipt.get('result', ''))
            except (ValueError, TypeError):
                continue
            if isinstance(result, dict) and result.get('ok') is True and result.get('task_id') == native:
                created = True
                break
        lineage = self._block_check_relay_lineage(task, origin, tasks, depth + 1) if created else None
        callback = (origin.get('_relay_callback_owner') or origin['id']) if lineage else None
        if not lineage or callback != lineage[-1]['id'] or owner.get('_relay_callback_owner') != callback:
            return None
        return [owner] + lineage

    def _block_check_results(self, task_id, name):
        for receipt in self._native_task_receipts(task_id, state='completed'):
            if (receipt.get('action') or {}).get('name') != name:
                continue
            try:
                result = json.loads(receipt.get('result', ''))
            except (ValueError, TypeError):
                continue
            if isinstance(result, dict):
                yield result

    @staticmethod
    def _block_answer_fingerprint(decision):
        from .service import digest, encoded
        return digest(encoded({key: decision.get(key) for key in BLOCK_ANSWER_FIELDS}))

    def _delegated_block_answers(self, task, card, related):
        # Answered decisions of the delegates this outcome waits for, while no later human direction from this
        # execution, the delegate, or its requester overrides them. The check, its record, and its later
        # revalidation all use this one rule. A pending card of another bot stays with that bot.
        delegates = self._block_check_delegates(task, card, related)
        owners = {identity: delegate for delegate in delegates.values() for identity in delegate['tasks']}
        answers = {}
        for decision in self._all('decisions'):
            delegate = owners.get(decision['task_id'])
            # _block_pause_holds accepts only a person's answer.
            if (delegate and decision.get('bot_id') == delegate['bot_id']
                    and not decision.get('_hidden') and not decision.get('_deleted')
                    and self._block_pause_holds(decision, related | delegate['direction'])):
                answers[decision['id']] = decision
        return answers

    def _block_pause_holds(self, decision, related):
        # Any later message or approval from the person, including one sent during the check, is new direction.
        from .work_status import PAUSE_DECISION_STATES
        return bool(decision and decision.get('decided_by') and decision['status'] in PAUSE_DECISION_STATES
                    and not self._human_direction_after(related, decision.get('decided_at') or decision['created_at'], decision))

    def _block_check_paused(self, task, check, related, card):
        # The check turn recorded the human answer that controls this block. Another turn or new direction ends it.
        identity = check.get('paused_by_decision_id')
        if not identity or check.get('pause_turn') != task.get('_native_turn_id'):
            return False
        if check.get('pause_fingerprint'):
            # A delegate's answer holds while its provenance and the exact recorded answer hold.
            decision = self._delegated_block_answers(task, card, related).get(identity)
            return bool(decision and self._block_answer_fingerprint(decision) == check['pause_fingerprint'])
        row = self._read_one('SELECT data FROM decisions WHERE id=?', (identity,))
        return self._block_pause_holds(json.loads(row[0]) if row else None, related)

    def record_block_pause(self, task_id, payload):
        from .service import ServiceError, YieldForDecision, require_text
        from .work_recovery import BLOCK_PAUSE_FIELDS
        from .work_status import PAUSE_DECISION_STATES
        identity = require_text(payload, 'decision_id', 100)
        with self.transaction():
            task = self._get('tasks', task_id)
            if task['status'] != 'working' or self.stopping.is_set():
                raise YieldForDecision('This task no longer accepts provider output.')
            recovery = task.get('_self_recovery') or {}
            turn = task.get('_native_turn_id')
            check = task.get('_block_check')
            if not check or not turn or (recovery.get('blocked') or {}).get('turn') != turn:
                raise ServiceError(409, 'Only the check of a blocked task can record this pause.')
            # The check context lists the decisions of this outcome. An unrelated card cannot pause it.
            offered = {d.get('id'): d for d in recovery.get('decisions', [])}
            row = self._read_one('SELECT data FROM decisions WHERE id=?', (identity,)) if identity in offered else None
            decision = json.loads(row[0]) if row else None
            if not decision or not decision.get('decided_by') or decision['status'] not in PAUSE_DECISION_STATES:
                raise ServiceError(409, 'Choose an answered human decision from this check context.')
            related = self._block_check_related(task)
            fingerprint = offered[identity].get('answer_fingerprint')
            if fingerprint:
                # The check offered this answer of a delegate. Its provenance and the exact answer must still hold.
                card = (self.kanban.tasks if self.kanban else {}).get(task['id'])
                current = self._delegated_block_answers(task, card, related).get(identity)
                if not current or self._block_answer_fingerprint(current) != fingerprint:
                    raise ServiceError(409, 'This answer changed, no longer belongs to work this outcome waits for, '
                                            'or the person gave new direction after it. Follow the current state instead of pausing.')
            elif not self._block_pause_holds(decision, related):
                raise ServiceError(409, 'The person gave new direction after this answer. Follow it instead of pausing.')
            pause = dict(paused_by_decision_id=identity, pause_turn=turn, pause_fingerprint=fingerprint)
            if any(check.get(key) != value for key, value in pause.items()):
                task['_block_check'] = dict({k: v for k, v in check.items() if k not in BLOCK_PAUSE_FIELDS},
                                            **{key: value for key, value in pause.items() if value})
                self._put('tasks', task)
        return dict(status='recorded', decision_id=identity)

    def _native_block_check(self, task, native):
        # An explicit native block keeps its reason, but nothing else resumes it.
        # Queue one owner check, then back off, until a card or recheck takes over.
        from .service import now, uid
        from .work_recovery import BLOCK_PAUSE_FIELDS, block_check_delay, block_check_task
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
            if check and check.get('pause_turn') and check['pause_turn'] == task.get('_native_turn_id'):
                # The check turn that recorded the pause blocked the task again. A later turn's block gets a new check.
                task['_block_check'].update({key: check[key] for key in BLOCK_PAUSE_FIELDS if key in check})
            self._put('tasks', task)
            return
        # Hermes moves a task to triage when it blocks again with the same kind, so that a person acts.
        # When the check turn itself ended there, another check only repeats the loop, so wait for the person.
        # A message from the person starts a new turn and ends the check context. The next block then gets a check.
        if native['status'] == 'triage' and block_check_task(task):
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
        related = self._block_check_related(task)
        decisions = [d for d in self._all('decisions') if d['task_id'] in related]
        if (self._task_work_is_paused(task) or self._automatic_work_is_paused(task)
                or any(d['status'] == 'pending' for d in decisions)
                or self._block_check_paused(task, check, related, native)):
            return defer()
        # A delegate's answer joins the check only with its public answer fields and the fingerprint the record checks.
        # Refresh these entries each check, so an answer that no longer holds leaves the context.
        delegated = [dict({key: d.get(key) for key in DELEGATED_DECISION_FIELDS}, answer_fingerprint=self._block_answer_fingerprint(d))
                     for d in self._delegated_block_answers(task, native, related).values()]
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
            _block_check=dict({k: v for k, v in check.items() if k not in BLOCK_PAUSE_FIELDS},
                              attempt=attempt, next_at=clock + block_check_delay(attempt)),
            # Keep the recovery attempt count. The block check keeps its own count in _block_check.
            _self_recovery=dict(previous, previous_task_id=task['id'],
                # Mark a context this check created, so it ends with the block.
                from_block_check=previous.get('from_block_check', not previous),
                failure='The native task is blocked: ' + native['block_kind'] + '.',
                blocked=dict(kind=native['block_kind'], turn=task.get('_native_turn_id'),
                             since=datetime.fromtimestamp(check['since'], timezone.utc).isoformat()),
                unknown_tools=records, work=previous.get('work', []),
                # The check reads earlier answers, so a declined card becomes a pause instead of a new question.
                decisions=list({d['id']: d for d in [d for d in previous.get('decisions', []) if 'answer_fingerprint' not in d]
                                + [self._public(d) for d in decisions] + delegated}.values())))
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
            natives = {root['_native_outcome_id'] for root, _ in groups.values()}
            # The projection reads the root tasks and the tasks of these native outcomes. The queue reads only which
            # Kanban tasks exist, so the other rows stay in SQLite.
            rows = self.db.execute(
                "SELECT id,CASE WHEN id IN (SELECT value FROM json_each(?)) OR json_extract(data,'$._native_outcome_id') "
                "IN (SELECT value FROM json_each(?)) THEN data END FROM tasks WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(identities)), json.dumps(sorted(natives)),
                 json.dumps(sorted(identities | {n['external_id'] for n in self.kanban.tasks.values()})))).fetchall()
            tasks = {row[0]: json.loads(row[1]) for row in rows if row[1] is not None}
            decisions = [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending' "
                "AND NOT coalesce(json_extract(data,'$._hidden'),0) AND NOT coalesce(json_extract(data,'$._deleted'),0)")]
            reports, roots = {}, {}
            # Most pull requests belong to other outcomes. Read the links of each row first, as _work_outcome_artifact
            # follows them, and decode the rows of these outcomes. Many rows share one work_outcome_id.
            for row in self.db.execute(
                    "SELECT id,json_extract(data,'$.work_outcome_id','$._work_replaced_by','$._same_outcome_as',"
                    "'$._hidden','$._work_parent_id'),data FROM artifacts WHERE json_extract(data,'$.kind')='pr' "
                    "AND NOT coalesce(json_extract(data,'$._hidden'),0)"):
                target, replaced, same, hidden, parent = json.loads(row[1])
                linked = None
                if target:
                    if target not in roots:
                        roots[target] = self._work_outcome_artifact({'work_outcome_id': target})['id']
                    root_id = roots[target]
                elif not replaced and not same and not (hidden and parent):
                    root_id = row[0]
                else:
                    linked = json.loads(row[2])
                    root_id = self._work_outcome_artifact(linked)['id']
                if root_id in groups:
                    reports.setdefault(root_id, []).append(linked or json.loads(row[2]))
            positions = self._native_queue_positions({row[0] for row in rows}, timestamp.timestamp())
            # Index the native tasks and the attempts of each outcome once. A search of both lists for each outcome
            # took 1.2 s of a 1.9 s state read with 3,000 outcomes.
            natives, owners = {}, {}
            for native in (self.kanban.tasks.values() if self.kanban else ()):
                natives.setdefault(native['id'], native)
            for task in tasks.values():
                if task.get('_native_outcome_id'):
                    owners.setdefault(task['_native_outcome_id'], set()).add(task['id'])
            for root, peers in groups.values():
                outcome = self._native_outcome_projection(root, tasks, [], decisions, timestamp, positions,
                    peers + reports.get(root['id'], []), natives=natives, owners=owners)
                for item in peers:
                    item.update(outcome)
                    if item['kind'] == 'work':
                        item['status'] = outcome['activity_state']
                    item['work_dismissal_id'] = self._setting('work.dismissed.' + self._work_identity(root))
        return artifacts

    def archive_idle_work(self, bot_id, data):
        """Archive Hermes tasks of one bot that wait for someone to act, after the person confirms them on the phone."""
        from .service import ServiceError
        identities = data.get('native_task_ids') if isinstance(data, dict) else None
        if (not isinstance(identities, list) or not identities or len(identities) > 500
                or not all(isinstance(identity, str) and identity for identity in identities)):
            raise ServiceError(400, 'List the idle tasks to archive.')
        if not self.kanban:
            raise ServiceError(409, 'Hermes Kanban is not available.')
        with self.lock:
            self._get('bots', bot_id)
        archived = self.kanban.call({'op': 'archive_idle', 'tenant': bot_id, 'ids': identities})['archived']
        self._close_archived_cards(archived, 'Idle task archive')
        return {'archived': archived}

    def _close_archived_cards(self, archived, name):
        """Read the board again after an archive. An archived task asks nothing more, so its open cards close, as
        they do when a task stops."""
        self.kanban.call({'op': 'snapshot'})
        if archived:
            from .service import now
            gone = set(archived)
            with self.transaction():
                owners = ({external for external, native in self.kanban.tasks.items() if native['id'] in gone}
                          | {t['id'] for t in self._all('tasks') if t.get('_native_outcome_id') in gone})
                for decision in self._all('decisions'):
                    if (decision.get('_execution_task_id', decision['task_id']) in owners
                            and decision['status'] in ('pending', 'approved')):
                        decision.update(status='rejected', decided_at=now(), decided_by='archive', decided_by_name=name,
                                        outcome='Task archived.')
                        self._put('decisions', decision)
                        self._event('decision.updated', self._public(decision), decision['bot_id'], decision['task_id'])
        self._publish_kanban_changes()

    def _native_outcome_projection(self, item, tasks, records, decisions, timestamp, queue_positions, peers=None, *,
                                   natives=None, owners=None):
        """natives maps each native task ID to its task, and owners maps each outcome to the IDs of its tasks.

        A caller that projects many outcomes builds both maps once (see _native_work_view).
        """
        identity = item['_native_outcome_id']
        if natives is None:
            natives = {n['id']: n for n in reversed(list(self.kanban.tasks.values()))} if self.kanban else {}
        if owners is None:
            owners = {identity: {t['id'] for t in tasks.values() if t.get('_native_outcome_id') == identity}}
        native = natives.get(identity)
        owner = tasks.get(item.get('task_id'), {})
        peers = peers or [item]
        owners = set(owners.get(identity, ()))
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
            native_last_activity_at=(datetime.fromtimestamp(native['last_activity_at'], timezone.utc).isoformat().replace('+00:00', 'Z')
                                     if native and native.get('last_activity_at') else None),
            native_diagnostics=native.get('diagnostics', []) if native and not terminal else [],
            activity_state={'needs_decision': 'waiting', 'queued': 'waiting'}.get(state, state))
