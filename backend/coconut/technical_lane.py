"""Continue already-authorized local technical work while one existing decision stays pending.

A requester or owner opts in with the optional `independent_technical_work` object of coconut_continue_work.
It names the exact pending question, its revision, and the already-authorized technical scopes. The lane keeps the
original owner, execution session, workspace, native outcome, dependencies, and result route. It grants no permission.

The lane does not sandbox local tools. Terminal, coding agents, file tools, and code execution keep full local access,
as in any turn. Exact scope instructions tell the bot to stay local and not publish, push, mark ready, or merge.
Coconut enforces only what its own boundaries can enforce. While the person has not answered the question, the
service holds the native actions that would change this outcome's lifecycle, the board, other bots, routines, or
decisions: Kanban writes other than notes on the owner's own task, bot messages, routines, new cards, decision
answers, waits, continuations, and dependency changes. Hermes approval requests are denied instead of creating a card.
A turn that ends while the question is pending returns the owner to it, so the native outcome stays in review.
The lane binds its own turns. Its holds apply to every lane turn of the owner's own task until that turn ends, even
when the question is answered, withdrawn, resolved, or deleted during the turn. A lane turn whose question ended cannot
finish the outcome: it returns the owner to the question's resume. Before the next turn, the dispatcher moves the owner
to a normal turn that resumes the question. That grants nothing: the normal task, decision, and pause rules apply.
"""
import json

SCOPES = ('diagnosis', 'tests', 'local_fixes')

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'description': 'Optional. Continue only already-authorized local technical work while one existing question stays '
        'pending. Give its exact ID and current revision, and only the scopes that the person already authorized. '
        'The owner must wait on that question in native review. This grants no permission and is not a sandbox. '
        'Publication, pushes, ready, and merge stay with the person.',
    'properties': {
        'decision_id': {'type': 'string'},
        'decision_revision': {'type': 'integer'},
        'scope': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'type': 'string', 'enum': list(SCOPES)}},
        'authorization_decision_id': {'type': 'string', 'description':
            'Required with local_fixes, and only then. The ID of the question in this execution that a person approved '
            'with the option that authorizes these fixes. Coconut checks it and gives the bot that exact recorded choice. '
            'diagnosis and tests need no authorization and grant no fix permission.'},
    },
    'required': ['decision_id', 'decision_revision', 'scope'],
    'allOf': [{'if': {'properties': {'scope': {'contains': {'const': 'local_fixes'}}}},
               'then': {'required': ['authorization_decision_id']},
               'else': {'not': {'required': ['authorization_decision_id']}}}],
}

SCOPE_INSTRUCTIONS = {
    'diagnosis': 'Diagnose the failures and findings, including CI logs.',
    'tests': 'Run the local tests and checks.',
    'local_fixes': ('Fix only the technical defects inside the choice that the person approved, as recorded below, '
                    'in the existing workspace, and commit them locally.'),
}


def instructions(lane):
    scope = lane['scope']
    limit = '' if 'local_fixes' in scope else 'Do not change product code. Report what a fix needs. '
    approved = ('The person approved this recorded choice. It is the whole fix scope: '
                + json.dumps(lane['authorization'], ensure_ascii=False) + ' ') if 'local_fixes' in scope else ''
    return ('Independent technical work. The pending decision below stays pending for the person. '
            + ' '.join(SCOPE_INSTRUCTIONS[item] for item in scope) + ' ' + limit + approved
            + 'Work only on this Mac. Do not push, publish, comment on or edit the PR or issue, mark it ready, merge, '
              'deploy, or answer or change any decision. Your local tools keep full access, so these limits are yours to '
              'keep. This lane grants no new permission. Coconut holds Kanban completion and board changes, bot messages, '
              'routines, new cards, and decision answers until the person answers. Record verified progress with '
              'kanban_comment on your own task, then finish the turn. Coconut returns this outcome to the decision.\n')


# Native actions that change the outcome lifecycle, the board, other bots, or routines. Local tools are not listed.
HELD_TOOLS = frozenset({'message_agent', 'cronjob_manage', 'clarify'})
KANBAN_READS = frozenset({'kanban_show', 'kanban_list', 'kanban_attachments'})
KANBAN_NOTES = frozenset({'kanban_comment', 'kanban_heartbeat'})
PENDING_RECEIPT_STATES = ('executing', 'awaiting_approval', 'awaiting_answer')
# Provider events that change decisions, ownership, routes, or other bots. Each needs a normal turn.
REFUSED_EVENTS = frozenset({'decision', 'artifact', 'bot_message', 'native_bot_message', 'bot_create', 'bot_delete',
                            'service_restart', 'withdrawal_correction', 'work_wait', 'work_continue', 'dependency_unlink',
                            'block_pause', 'native_approval', 'native_question', 'native_proposal', 'decision_response'})


def requested(payload):
    """The validated opt-in, or None. Unknown fields or values fail closed."""
    from .service import ServiceError
    if 'independent_technical_work' not in payload:
        return None
    lane = payload['independent_technical_work']
    scope = lane.get('scope') if isinstance(lane, dict) else None
    fixes = isinstance(scope, list) and 'local_fixes' in scope
    keys = {'decision_id', 'decision_revision', 'scope'} | ({'authorization_decision_id'} if fixes else set())
    if (not isinstance(lane, dict) or set(lane) != keys
            or any(not isinstance(lane[key], str) or not 0 < len(lane[key]) <= 100
                   for key in ('decision_id', 'authorization_decision_id') if key in keys)
            or type(lane['decision_revision']) is not int
            or not isinstance(scope, list) or not scope or len(set(map(str, scope))) != len(scope)
            or any(item not in SCOPES for item in scope)):
        raise ServiceError(400, 'Give independent_technical_work the exact pending decision ID, its revision, and a local '
                                'scope. local_fixes, and only local_fixes, needs authorization_decision_id.')
    result = dict(decision_id=lane['decision_id'], decision_revision=lane['decision_revision'],
                  scope=[item for item in SCOPES if item in scope])
    if fixes:
        result['authorization_decision_id'] = lane['authorization_decision_id']
    return result


def held_action(action, native_task_id):
    """Why the lane holds this native action, or None. This names native actions only. It reads no command."""
    name, arguments = action.get('name') or '', action.get('arguments')
    arguments = arguments if isinstance(arguments, dict) else {}
    if name in HELD_TOOLS:
        return f'{name} waits for the person.'
    if not name.startswith('kanban_') or name in KANBAN_READS and not arguments.get('board'):
        return None
    if name in KANBAN_NOTES and not arguments.get('board') and arguments.get('task_id') in (None, native_task_id):
        return None
    return 'Only notes on this outcome’s own native task are available. Its lifecycle and the board wait for the person.'


class TechnicalLane:
    def _technical_gate(self, task):
        """The lane and its decision while that exact decision is pending, or (None, None).

        Only the owner's own task has the lane. A callback that names the owner as its assignment keeps its normal
        route. An automated revision keeps the decision pending. An answer, a withdrawal, an automatic resolution, or a
        deletion ends the lane without granting anything.
        """
        lane = task.get('_technical_lane')
        if not lane:
            return None, None
        with self.lock:
            row = self.db.execute('SELECT data FROM decisions WHERE id=?', (lane['decision_id'],)).fetchone()
        decision = json.loads(row[0]) if row else None
        if not decision or decision['status'] != 'pending':
            return None, None
        return lane, decision

    def _technical_lane(self, task):
        """The lane that holds the task's current turn, or None. Only the dispatcher's move to a resumed turn ends it."""
        lane = task.get('_technical_lane')
        if lane and task.get('_native_turn_id') in (lane.get('turn_ids') or [lane.get('turn_id')]):
            return lane
        return None

    def _technical_decision_exists(self, lane):
        with self.lock:
            return self.db.execute('SELECT 1 FROM decisions WHERE id=?', (lane['decision_id'],)).fetchone() is not None

    def _technical_checkpoint(self, owner, previous):
        """The decision's own turn and its completed effects, which an answer resumes. Computed once per decision.

        An owner that is still on a lane turn keeps the earlier checkpoint, for example when the lane is admitted again
        after a failed turn. The results of a lane turn are never the decision's effects.
        """
        turn = owner.get('_native_turn_id')
        if previous and turn in (previous.get('turn_ids') or [previous.get('turn_id')]):
            return dict(gate_turn_id=previous.get('gate_turn_id'), gate_receipt_ids=list(previous.get('gate_receipt_ids', [])),
                        turn_ids=list(previous.get('turn_ids') or [previous.get('turn_id')]))
        receipts = owner.get('_continuation_receipt_ids', []) + ([r['id'] for r in self._native_task_receipts(
            owner['id'], turn_id=turn, state='completed')] if turn else [])
        return dict(gate_turn_id=turn, gate_receipt_ids=list(dict.fromkeys(receipts)), turn_ids=[])

    @staticmethod
    def _technical_turn_advanced(task):
        """Record the turn that _advance_input just gave a lane owner. Every lane turn advances here."""
        lane = task['_technical_lane']
        lane['turn_id'] = task['_native_turn_id']
        lane['turn_ids'] = list(dict.fromkeys([*lane.get('turn_ids', []), lane['turn_id']]))

    def _technical_unknown(self, task, lane):
        """Whether a lane turn has a tool without a confirmed result. A lane never leaves such a turn."""
        turns = set(lane.get('turn_ids') or [lane.get('turn_id')]) - {None}
        return any(r['state'] in PENDING_RECEIPT_STATES and r.get('turn_id') in turns
                   for r in self._native_task_receipts(task['id']))

    def _leave_technical_turn(self, task):
        """Resume an answered or ended decision in a new turn, never in the lane turn.

        The dispatcher calls this before it builds the native row, so the claim binds the new input. The new turn
        carries the completed effects of the decision's own turn, as a normal resume shows them, and does not replay
        the lane's results as its own. An unknown effect of the lane turn keeps that turn, so the existing receipt
        check fails the resume closed. A live worker or a pending decision keeps the lane turn too.
        """
        lane = self._technical_lane(task)
        if (not lane or task['status'] != 'queued' or task['id'] in self.running
                or task.get('_native_turn_id') != lane.get('turn_id')
                or self._technical_gate(task)[0] or self._technical_unknown(task, lane)):
            return False
        kept = {key: task[key] for key in ('_resume_decision', '_direct_reply_message_id', '_conversation_epoch') if key in task}
        if '_resume_decision' not in kept and self._technical_decision_exists(lane):
            # The question ended while a lane turn ran, for example after a restart. Its resume carries the resolution.
            kept['_resume_decision'] = lane['decision_id']
        self._advance_input(task)
        task.update(kept, _continuation_receipt_ids=list(lane.get('gate_receipt_ids', [])))
        task.pop('_technical_lane')
        self._put('tasks', task)
        return True

    def _technical_authorization(self, lane, decisions, gate):
        """The exact approved choice that local_fixes names, or a refusal. Provenance only; no text is interpreted."""
        from .service import ServiceError
        if 'local_fixes' not in lane['scope']:
            return None
        approval = next((d for d in decisions if d['id'] == lane['authorization_decision_id']), None)
        option = approval and next((o for o in approval.get('options') or [] if o.get('id') == approval.get('selected_option_id')), None)
        answer = approval and next((a for a in reversed(approval.get('_answers') or []) if a.get('decided_by')), None)
        if (not approval or approval['id'] == gate['id'] or approval['status'] not in ('approved', 'executed')
                or approval.get('action_type') != 'question' or approval.get('inbox_kind', 'decision') != 'decision'
                or not approval.get('_native') or approval.get('draft') is not None or approval.get('merge_pr_url')
                or not approval.get('decided_by') or not option or option.get('answer') != 'approve'
                or not answer or answer.get('choice') != 'approve' or answer.get('option_id') != option['id']
                or answer['decided_by'] != approval['decided_by']):
            # A requester's message cannot authorize product changes. Only a person's recorded choice in this execution can.
            raise ServiceError(409, 'local_fixes needs authorization_decision_id: a question of this execution that a person '
                                    'approved with a selected option. Use diagnosis or tests otherwise.')
        return dict(decision_id=approval['id'], title=approval.get('title'), context=approval.get('context', ''),
                    selected_option_id=option['id'], selected_option_label=option.get('label'),
                    selected_option_explanation=option.get('explanation', ''), sources=approval.get('sources') or [],
                    decided_at=approval.get('decided_at'))

    def _technical_lane_gate(self, owner, native, lane, related):
        """Return the exact pending decision that the lane may leave pending, or refuse."""
        from .service import ServiceError
        ids = {t['id'] for t in related}
        decisions = [d for d in self._all('decisions') if d['task_id'] in ids]
        pending = [d for d in decisions if d['status'] == 'pending']
        gate = pending[0] if len(pending) == 1 else None
        if native.get('status') != 'review' or owner['status'] != 'needs_you':
            raise ServiceError(409, 'Independent technical work continues only an owner that waits on its decision in native review.')
        if (not gate or gate['id'] != lane['decision_id'] or gate['revision'] != lane['decision_revision']):
            raise ServiceError(409, 'The pending decision changed. Read it and retry with its exact ID and revision.')
        if (gate.get('action_type') != 'question' or gate.get('inbox_kind', 'decision') != 'decision'
                or gate.get('_receipt_id') or gate.get('merge_pr_url') or gate.get('draft') is not None
                or gate.get('_source_recheck') or gate.get('decided_by') or gate.get('selected_option_id')
                or any(answer.get('decided_by') for answer in gate.get('_answers', []))):
            raise ServiceError(409, 'This decision holds a tool, a merge, a draft, or a recorded answer. Wait for it.')
        if any(d['status'] == 'rejected' for d in decisions):
            raise ServiceError(409, 'A person declined part of this execution. Continue it in a normal turn.')
        if any(self._work_pause(item) for item in self._task_work_items(owner)) or self._automatic_work_is_paused(owner):
            raise ServiceError(409, 'This work is paused.')
        if ((owner.get('_self_recovery') or {}).get('unknown_tools')
                or any(r['state'] in PENDING_RECEIPT_STATES for identity in ids for r in self._native_task_receipts(identity))):
            raise ServiceError(409, 'A tool of this execution has no confirmed result. Check it before continuing.')
        if any(c['task_id'] in ids and c['status'] in ('queued', 'working') for c in self._all('contexts')):
            raise ServiceError(409, 'This decision has an active request. Wait for it.')
        authorization = self._technical_authorization(lane, decisions, gate)
        if authorization:
            lane['authorization'] = authorization
        return gate

    def _refuse_technical_event(self, task_id, kind):
        from .service import ServiceError
        if kind not in REFUSED_EVENTS:
            return
        with self.lock:
            task = self._get('tasks', task_id)
        if self._technical_lane(task):
            # A refused question returns to the bot as a validation error, so the turn continues.
            raise ServiceError(400 if kind in ('native_question', 'native_proposal') else 403,
                               'Independent technical work cannot answer or create decisions, contact other bots, '
                               'or change work routes while its decision is pending.')

    def _refuse_technical_tool(self, task, action):
        from .service import ServiceError
        lane = self._technical_lane(task)
        reason = lane and held_action(action, lane['native_task_id'])
        if reason:
            raise ServiceError(403, 'Independent technical work holds this native action. ' + reason)

    def _finish_technical_lane(self, task, kind, summary):
        """Return a finished lane turn to its pending decision. Its result route stays for the final result."""
        from .service import now
        lane = self._technical_lane(task)
        if not lane:
            return False
        if not self._technical_gate(task)[0]:
            # The question ended during this lane turn (an answer, a withdrawal, or a Work change). The lane turn cannot
            # finish the outcome, execute an approval, or return a result. The dispatcher resumes the question next.
            task.update(status='queued', summary='Resuming the changed decision.', updated_at=now())
            if self._technical_decision_exists(lane):
                task['_resume_decision'] = lane['decision_id']
            self._put('tasks', task)
            self._event('task.updated', self._public(task), task['bot_id'], task['id'])
            self._refresh_bot(task['bot_id'])
            return True
        decision = self._technical_gate(task)[1]
        if kind == 'completed' and task.get('_steering') and not self._technical_unknown(task, lane):
            # The follow-up runs as the next lane turn of the same owner, at the same message boundary as a normal
            # follow-up. The decision, its approved scope, and the result route stay as they are.
            self._advance_input(task)
            self._technical_turn_advanced(task)
            task.update(status='queued', summary='Applying your follow-up at the next safe turn.', updated_at=now())
            self._put('tasks', task)
            self._event('task.updated', self._public(task), task['bot_id'], task['id'])
            self._refresh_bot(task['bot_id'])
            return True
        task.update(status='needs_you', summary=decision['title'], updated_at=now())
        if task.get('_steering'):
            # As in a normal failure, save the follow-up for the next continuation of this owner. A completed turn
            # with an unknown tool result saves it too, because the lane never leaves that turn.
            self._hold_failed_followup(task)
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
        note = 'Technical work stopped: ' if kind == 'failed' else 'Technical work finished: '
        self._message(task['bot_id'], (note + str(summary or ''))[:500] + ' The decision remains pending.',
                      task_id=task['id'], kind='activity')
        self._refresh_bot(task['bot_id'])
        return True
