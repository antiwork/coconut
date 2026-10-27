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

A declined merge is a second, narrower gate. A person can decline a merge card, and its answer can pause the whole
outcome. The requester then also names the card's exact merge_pr_url. The card must be a native merge question that a
person rejected without an option or text, and it must be the only pause of every Work item of the owner, with no new
direction since. The owner must be finished and idle in native triage, blocked, or scheduled. The decline stays as it
is, and so does the pause. The lane lifts the automatic pause only for its own turns, with the same holds as above. A
finished lane turn returns the owner to its earlier status and parks the native card in scheduled, so nothing
completes, retries, or replays the decline. If the decline or the pause ends, the owner leaves the lane through a
normal turn, which the normal pause rules hold. Nothing resumes the declined decision.

A held merge is a third gate, for read-only diagnosis only. A person can answer a merge card with its canonical
"Leave #<number> open" option, and that answer can pause the outcome. The requester then names the card's exact
merge_pr_url and that selected option. The card must be a native merge question that a person approved once with that
exact option, without text, and that already executed. Its task belongs to this execution or is a direct return of it,
and it must be the only pause of every Work item of the owner, with no new direction since. The scope is diagnosis
alone: no tests, no fixes, and no authorization. The owner must be completed and idle, its native card done or parked
in scheduled. A done card keeps its result: Hermes gives the new input its linked successor, and the lane binds that
successor before its turn starts. The lane turn runs and finishes like a declined-merge turn. The hold, its option,
and the pause stay, and the instructions keep the PR unready and unmerged.
"""
import json
import re

SCOPES = ('diagnosis', 'tests', 'local_fixes')
URL_LIMIT = 500
# Actors that the service records for its own rejections, such as a task stop. They are not a person's decline.
SYSTEM_DECIDERS = frozenset({'cancellation', 'archive', 'workflow_permission', 'local'})
# The one option of a merge card that holds the PR open. Any other choice, label, or option ID fails closed.
HELD_OPTION_ID = 'leave_open'
PR_URL = re.compile(r'https://github\.com/[^/\s]+/[^/\s]+/pull/([1-9][0-9]*)')

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'description': 'Optional. Continue only already-authorized local technical work while one existing question stays '
        'pending. Give its exact ID and current revision, and only the scopes that the person already authorized. '
        'The owner must wait on that question in native review. Or, with declined_merge_pr_url, continue a finished '
        'outcome that a person’s declined merge card pauses. Or, with held_merge_pr_url and held_merge_option_id and '
        'the scope ["diagnosis"] alone, diagnose read-only a completed outcome that a person’s "Leave #<number> open" '
        'answer to a merge card pauses. This grants no permission and is not a sandbox. '
        'Publication, pushes, ready, and merge stay with the person.',
    'properties': {
        'decision_id': {'type': 'string'},
        'decision_revision': {'type': 'integer'},
        'declined_merge_pr_url': {'type': 'string', 'maxLength': URL_LIMIT, 'description':
            'Only for a merge card that a person declined and whose answer pauses this work. Give its exact '
            'merge_pr_url. decision_id and decision_revision then name that declined card. The decline and the pause '
            'stay. The merge, and everything the card asked to approve, stays refused.'},
        'held_merge_pr_url': {'type': 'string', 'maxLength': URL_LIMIT, 'description':
            'Only with held_merge_option_id, for a merge card that a person answered with its "Leave #<number> open" '
            'option and whose answer pauses this work. Give its exact merge_pr_url. decision_id and decision_revision '
            'then name that card, and scope must be ["diagnosis"]. Not with declined_merge_pr_url. The hold stays: '
            'the PR stays unready and unmerged, and nothing in the workspace changes.'},
        'held_merge_option_id': {'type': 'string', 'maxLength': 100, 'description':
            'Only with held_merge_pr_url. The exact recorded selected option of that card, which must be leave_open.'},
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
    if lane.get('held_merge_pr_url'):
        # Read-only diagnosis under a person's hold. The recorded choice keeps the PR open; it is no permission.
        held = ('The person answered this recorded merge decision with the choice below, and that answer pauses this '
                'work: ' + json.dumps(lane.get('held', {}), ensure_ascii=False) + ' The hold and the pause stay. The PR '
                'stays unready and unmerged. This answer approves no merge, no ready, and no fix. This turn is '
                'read-only diagnosis: read the code, history, and CI logs. Do not edit, create, or delete any file in '
                'the workspace, do not commit, and do not run tests, builds, migrations, or services. ')
        return ('Independent technical work. ' + held + 'Report what you find and what a fix would need. '
                'Work only on this Mac. Do not push, publish, comment on or edit the PR or issue, mark it ready, merge, '
                'deploy, create cards, or answer or change any decision. Your local tools keep full access, so these '
                'limits are yours to keep. This lane grants no new permission. Coconut holds Kanban completion and board '
                'changes, bot messages, routines, new cards, and decision answers until the person gives new direction. '
                'Record verified findings with kanban_comment on your own task, then finish the turn. Coconut returns '
                'this outcome to its hold.\n')
    if lane.get('declined_merge_pr_url'):
        # The recorded card names what stays refused. Nothing in it is read as permission.
        held = ('The person declined this recorded decision, and its answer pauses this work: '
                + json.dumps(lane.get('declined', {}), ensure_ascii=False) + ' The decline and the pause stay. Do not do '
                  'anything that it asked the person to approve, in any environment. ')
        until, back = 'until the person gives new direction', 'to its pause'
    else:
        held, until, back = 'The pending decision below stays pending for the person. ', 'until the person answers', 'to the decision'
    return ('Independent technical work. ' + held
            + ' '.join(SCOPE_INSTRUCTIONS[item] for item in scope) + ' ' + limit + approved
            + 'Work only on this Mac. Do not push, publish, comment on or edit the PR or issue, mark it ready, merge, '
              'deploy, or answer or change any decision. Your local tools keep full access, so these limits are yours to '
              'keep. This lane grants no new permission. Coconut holds Kanban completion and board changes, bot messages, '
              'routines, new cards, and decision answers ' + until + '. Record verified progress with '
              'kanban_comment on your own task, then finish the turn. Coconut returns this outcome ' + back + '.\n')


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
    declined = isinstance(lane, dict) and 'declined_merge_pr_url' in lane
    held = isinstance(lane, dict) and bool({'held_merge_pr_url', 'held_merge_option_id'} & set(lane))
    keys = ({'decision_id', 'decision_revision', 'scope'} | ({'authorization_decision_id'} if fixes else set())
            | ({'declined_merge_pr_url'} if declined else set())
            | ({'held_merge_pr_url', 'held_merge_option_id'} if held else set()))
    if (not isinstance(lane, dict) or set(lane) != keys
            or any(not isinstance(lane[key], str) or not 0 < len(lane[key]) <= 100
                   for key in ('decision_id', 'authorization_decision_id', 'held_merge_option_id') if key in keys)
            or any(not isinstance(lane[key], str) or not 0 < len(lane[key]) <= URL_LIMIT
                   for key in ('declined_merge_pr_url', 'held_merge_pr_url') if key in keys)
            # A hold is read-only diagnosis alone, and a decline and a hold never combine.
            or held and (declined or scope != ['diagnosis'])
            or type(lane['decision_revision']) is not int
            or not isinstance(scope, list) or not scope or len(set(map(str, scope))) != len(scope)
            or any(item not in SCOPES for item in scope)):
        raise ServiceError(400, 'Give independent_technical_work the exact pending decision ID, its revision, and a local '
                                'scope. local_fixes, and only local_fixes, needs authorization_decision_id. '
                                'declined_merge_pr_url, if given, is the exact URL of the declined merge card. '
                                'held_merge_pr_url and held_merge_option_id go together, only with the scope '
                                '["diagnosis"] and without declined_merge_pr_url.')
    result = dict(decision_id=lane['decision_id'], decision_revision=lane['decision_revision'],
                  scope=[item for item in SCOPES if item in scope])
    if fixes:
        result['authorization_decision_id'] = lane['authorization_decision_id']
    if declined:
        result['declined_merge_pr_url'] = lane['declined_merge_pr_url']
    if held:
        result.update(held_merge_pr_url=lane['held_merge_pr_url'], held_merge_option_id=lane['held_merge_option_id'])
    return result


def parked(lane):
    """Whether the lane works under an answered merge card. Its turns park the card and never resume that answer."""
    return bool(lane and (lane.get('declined_merge_pr_url') or lane.get('held_merge_pr_url')))


def held_merge(decision, lane):
    """The selected option if this record is a person's canonical hold of the exact merge card that the lane names.

    Only structured fields count. The card must be an executed native merge question with one human answer that
    approves the one option whose ID is leave_open and whose label is "Leave #<number> open" for the card's own PR.
    A merge choice, another label or option, a correction, a draft, an action, or a system actor fails closed.
    """
    url = decision.get('merge_pr_url')
    number = PR_URL.fullmatch(url) if isinstance(url, str) else None
    options = decision.get('options') or []
    chosen = [o for o in options if isinstance(o, dict) and o.get('id') == decision.get('selected_option_id')]
    answers = [answer for answer in decision.get('_answers') or [] if answer.get('decided_by')]
    ok = bool(number and decision['id'] == lane['decision_id'] and decision.get('revision') == lane['decision_revision']
              and decision['status'] == 'executed' and decision.get('action_type') == 'question'
              and decision.get('inbox_kind', 'decision') == 'decision' and decision.get('_native')
              and url.lower() == lane['held_merge_pr_url'].lower()
              and decision.get('draft') is None and not decision.get('_receipt_id') and not decision.get('_action')
              and not decision.get('_proposal_action') and not decision.get('_source_recheck')
              and not decision.get('_hidden') and not decision.get('_deleted')
              and decision.get('decided_by') and decision['decided_by'] not in SYSTEM_DECIDERS
              and not (decision.get('correction') or '').strip()
              and len({o.get('id') for o in options if isinstance(o, dict)}) == len(options) and len(chosen) == 1
              and decision['selected_option_id'] == lane['held_merge_option_id'] == HELD_OPTION_ID
              and chosen[0].get('answer') == 'approve' and not chosen[0].get('action')
              and chosen[0].get('label') == decision.get('selected_option_label') == 'Leave #%s open' % number.group(1)
              and len(answers) == 1 and len(decision.get('_answers') or []) == 1
              and answers[0].get('choice') == 'approve' and answers[0].get('option_id') == HELD_OPTION_ID
              and not (answers[0].get('correction') or '').strip()
              and answers[0]['decided_by'] == decision['decided_by'])
    return chosen[0] if ok else None


def declined_merge(decision, lane):
    """Whether this record is the person's plain decline of the exact merge card that the lane names.

    Only structured fields count. A selected option, a correction, another answer, or a system actor fails closed.
    """
    answers = [answer for answer in decision.get('_answers') or [] if answer.get('decided_by')]
    url = decision.get('merge_pr_url')
    return bool(decision['id'] == lane['decision_id'] and decision.get('revision') == lane['decision_revision']
                and decision['status'] == 'rejected' and decision.get('action_type') == 'question'
                and decision.get('inbox_kind', 'decision') == 'decision' and decision.get('_native')
                and isinstance(url, str) and url.lower() == lane['declined_merge_pr_url'].lower()
                and decision.get('draft') is None and not decision.get('_receipt_id')
                and not decision.get('_source_recheck') and not decision.get('_hidden') and not decision.get('_deleted')
                and decision.get('decided_by') and decision['decided_by'] not in SYSTEM_DECIDERS
                and decision.get('selected_option_id') is None and not (decision.get('correction') or '').strip()
                and len(answers) == 1 and answers[0].get('choice') == 'reject' and answers[0].get('option_id') is None
                and not (answers[0].get('correction') or '').strip()
                and answers[0]['decided_by'] == decision['decided_by'])


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
        if lane.get('held_merge_pr_url'):
            # A held merge holds while the same canonical hold is the owner's whole pause, with no new direction.
            if not decision or not held_merge(decision, lane) or not self._held_pause(task, decision):
                return None, None
            return lane, decision
        if lane.get('declined_merge_pr_url'):
            # A declined merge holds while the same decline is the owner's whole pause, with no new direction.
            if not decision or not declined_merge(decision, lane) or not self._declined_pause(task, decision['id']):
                return None, None
            return lane, decision
        if not decision or decision['status'] != 'pending':
            return None, None
        return lane, decision

    def _declined_pause(self, owner, decision_id):
        """Whether this one answered decision pauses every Work item of the owner and nothing else pauses them."""
        items = self._task_work_items(owner)
        peers = [peer for item in items for peer in self._work_pause_peers(item, shared=True)]
        if not items or {peer.get('paused_by_decision_id') for peer in peers} - {None} != {decision_id}:
            return False
        return all((pause := self._work_pause(item)) and pause['id'] == decision_id
                   and not self._work_has_new_direction(owner, pause, item) for item in items)

    def _held_pause(self, owner, decision):
        """Whether the hold is the owner's whole pause with no new direction, here or in the execution that asked it.

        The asking execution is a return of the owner, below it, so the owner's own direction check does not read it.
        A person's message or answer there after the pause, or any refusal there, ends the hold for this lane. So does a
        person's answer after the pause to any other card of the owner's lineage, whatever that card's status is now.
        """
        from .work_capacity import execution_key
        if not self._declined_pause(owner, decision['id']):
            return False
        row = self._read_one('SELECT data FROM tasks WHERE id=?', (decision['task_id'],))
        asker = json.loads(row[0]) if row else None
        if not asker:
            return False
        with self.lock:
            asked = {asker['id']} | {json.loads(r[0])['id'] for r in self.db.execute(
                "SELECT data FROM tasks WHERE json_extract(data,'$._execution_session_id')=?", (execution_key(asker),))}
        peers = [peer for item in self._task_work_items(owner) for peer in self._work_pause_peers(item, shared=True)]
        after = max((p.get('_work_pause_at', '') for p in peers), default='') or decision.get('decided_at') or decision['created_at']
        roots, _ = self._direction_roots(owner)
        return not (self._human_direction_after(asked, after, decision)
                    or self._human_answer_after(asked | roots, after, decision)
                    or any(d['task_id'] in asked and d['status'] == 'rejected' for d in self._all_shared('decisions')))

    def _human_answer_after(self, task_ids, after, pause):
        """Whether a person answered another card of these tasks after the pause, whatever the card's status is now.

        An approved question becomes executed or failed when its turn ends, and the shared direction check reads only
        the current approved or modified status. The person's recorded answer is the provenance. A system actor is no
        answer. A native answer carries no time of its own, so the card's decided_at stands for it. That is the latest
        answer or a later system close, so an unclear time counts as new direction and fails closed.
        """
        for card in self._all_shared('decisions'):
            if card['task_id'] not in task_ids or card['id'] == pause['id']:
                continue
            answers = [a for a in card.get('_answers') or [] if a.get('decided_by') and a['decided_by'] not in SYSTEM_DECIDERS]
            if any((a.get('decided_at') or card.get('decided_at') or '') > after for a in answers):
                return True
        return False

    def _technical_declined_turn(self, task):
        """Whether the automatic pause lets this declined- or held-merge lane turn run. Only its own queued or working turn."""
        lane = self._technical_lane(task)
        return bool(parked(lane) and task['status'] in ('queued', 'working')
                    and self._technical_gate(task)[0])

    def _technical_native_state(self, task):
        """The native state of a finished declined- or held-merge lane turn, or None. Scheduled neither completes nor retries."""
        lane = self._technical_lane(task)
        if parked(lane) and task['status'] in ('completed', 'failed'):
            return 'scheduled'
        return None

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
        # A declined or held merge has no answer to resume. Its normal turn keeps the normal pause rules.
        if '_resume_decision' not in kept and not parked(lane) and self._technical_decision_exists(lane):
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

    def _declined_merge_gate(self, owner, native, lane, related):
        """Return the person's declined merge card that pauses this idle owner, or refuse. See the module docstring."""
        from .service import ServiceError
        ids = {t['id'] for t in related}
        decisions = [d for d in self._all('decisions') if d['task_id'] in ids]
        declined = next((d for d in decisions if d['id'] == lane['decision_id']), None)
        if (owner['status'] not in ('completed', 'failed') or native.get('status') not in ('triage', 'blocked', 'scheduled')
                or native.get('current_run_id') or native.get('worker_pid')
                or (self.running.get(owner['id']) and self.running[owner['id']].is_alive())):
            raise ServiceError(409, 'A declined merge continues only a finished owner whose native outcome waits idle.')
        if not declined or not declined_merge(declined, lane):
            raise ServiceError(409, 'The declined merge changed, or it is not a person’s plain decline of that exact '
                                    'merge card. Read it and retry with its exact ID, revision, and merge URL.')
        if any(d['status'] == 'rejected' and d['id'] != declined['id'] for d in decisions):
            raise ServiceError(409, 'A person declined another part of this execution. Continue it in a normal turn.')
        if not self._declined_pause(owner, declined['id']):
            raise ServiceError(409, 'This work is paused by another decision, has new direction, or is not paused by '
                                    'this declined merge.')
        if ((owner.get('_self_recovery') or {}).get('unknown_tools')
                or any(r['state'] in PENDING_RECEIPT_STATES for identity in ids for r in self._native_task_receipts(identity))):
            raise ServiceError(409, 'A tool of this execution has no confirmed result. Check it before continuing.')
        if any(c['task_id'] in ids and c['status'] in ('queued', 'working') for c in self._all('contexts')):
            raise ServiceError(409, 'This execution has an active request. Wait for it.')
        # A decline never authorizes fixes. local_fixes still needs a separate approved question of this execution.
        authorization = self._technical_authorization(lane, decisions, declined)
        if authorization:
            lane['authorization'] = authorization
        lane['declined'] = dict(decision_id=declined['id'], title=declined.get('title'),
                                merge_pr_url=declined['merge_pr_url'], decided_at=declined.get('decided_at'))
        lane['hold'] = dict(status=owner['status'], summary=owner.get('summary'))
        return declined

    def _held_merge_gate(self, owner, native, lane, related):
        """Return the person's canonical hold of this owner's merge card, or refuse. See the module docstring.

        The card can belong to the owner's execution or to a direct return of it, such as the requester's callback
        that asked the person about the owner's PR. Both executions must be quiet.
        """
        from .service import ServiceError
        from .work_capacity import execution_key
        ids = {t['id'] for t in related}
        if (owner['status'] != 'completed' or native.get('status') not in ('done', 'scheduled')
                or native.get('current_run_id') or native.get('worker_pid')
                or (self.running.get(owner['id']) and self.running[owner['id']].is_alive())):
            raise ServiceError(409, 'A held merge continues only a completed owner whose native card is done or parked.')
        decision = self._read_one('SELECT data FROM decisions WHERE id=?', (lane['decision_id'],))
        decision = json.loads(decision[0]) if decision else None
        asker = self._read_one('SELECT data FROM tasks WHERE id=?', (decision['task_id'],)) if decision else None
        asker = json.loads(asker[0]) if asker else None
        option = (asker and (asker['id'] in ids or asker.get('source_task_id') in ids)
                  and held_merge(decision, lane))
        items = self._task_work_items(owner)
        if not option or not any(item['kind'] == 'pr' and (item.get('url') or '').lower() == decision['merge_pr_url'].lower()
                                 for item in items):
            raise ServiceError(409, 'The held merge changed, or it is not a person’s "Leave #<number> open" answer to '
                                    'the exact merge card of this work’s PR. Read it and retry with its exact ID, '
                                    'revision, merge URL, and selected option.')
        tasks = self._all('tasks')
        asked = {t['id'] for t in tasks if execution_key(t) == execution_key(asker)}
        scope = ids | asked
        decisions = [d for d in self._all('decisions') if d['task_id'] in scope
                     or (d.get('merge_pr_url') or '').lower() == decision['merge_pr_url'].lower()]
        if any(d['status'] == 'pending' for d in decisions):
            raise ServiceError(409, 'A decision about this work or its PR is pending. It must be answered first.')
        if any(d['status'] == 'rejected' for d in decisions):
            raise ServiceError(409, 'A person declined part of this work or its PR. Continue it in a normal turn.')
        if any(t['id'] in asked and (t['status'] in self.ACTIVE or t.get('_waiting_on') or t.get('_return_pending'))
               for t in tasks):
            raise ServiceError(409, 'The execution that asked this card is still active. Wait for it.')
        if not self._held_pause(owner, decision):
            raise ServiceError(409, 'This work is paused by another decision, has new direction, or is not paused by '
                                    'this held merge.')
        if ((owner.get('_self_recovery') or {}).get('unknown_tools')
                or any(r['state'] in PENDING_RECEIPT_STATES for identity in scope for r in self._native_task_receipts(identity))):
            raise ServiceError(409, 'A tool of this work has no confirmed result. Check it before continuing.')
        if any(c['task_id'] in scope and c['status'] in ('queued', 'working') for c in self._all('contexts')):
            raise ServiceError(409, 'This work has an active request. Wait for it.')
        # The whole recorded card and its selected option, as data. The hold is never read as a merge or a fix permission.
        lane['held'] = dict(decision_id=decision['id'], revision=decision['revision'], title=decision.get('title'),
                            context=decision.get('context', ''), merge_pr_url=decision['merge_pr_url'],
                            selected_option={k: option.get(k) for k in ('id', 'label', 'answer', 'explanation')},
                            options=[{k: o.get(k) for k in ('id', 'label', 'answer')} for o in decision['options']],
                            decided_at=decision.get('decided_at'))
        lane['hold'] = dict(status=owner['status'], summary=owner.get('summary'))
        return decision

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
        """Return a finished lane turn to its pending decision, or to the pause of its declined merge.

        Its result route stays for the final result. A declined merge never resumes its decision.
        """
        from .service import now
        lane = self._technical_lane(task)
        if not lane:
            return False
        declined = parked(lane)
        if not self._technical_gate(task)[0]:
            # The question ended during this lane turn (an answer, a withdrawal, or a Work change). The lane turn cannot
            # finish the outcome, execute an approval, or return a result. The dispatcher resumes the question next.
            # A declined merge has no answer to resume: the dispatcher moves the owner to a normal turn, which the
            # normal pause rules hold.
            task.update(status='queued', summary=('Continuing after the held decision or its pause changed.'
                        if lane.get('held_merge_pr_url') else 'Continuing after the declined decision or its pause changed.'
                        if declined else 'Resuming the changed decision.'), updated_at=now())
            if not declined and self._technical_decision_exists(lane):
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
        if declined:
            # The owner returns to its earlier status and the pause holds it. The native card parks in scheduled.
            hold = lane.get('hold') or {}
            task.update(status=hold.get('status', 'completed'), summary=hold.get('summary') or decision['title'], updated_at=now())
        else:
            task.update(status='needs_you', summary=decision['title'], updated_at=now())
        if task.get('_steering'):
            # As in a normal failure, save the follow-up for the next continuation of this owner. A completed turn
            # with an unknown tool result saves it too, because the lane never leaves that turn.
            self._hold_failed_followup(task)
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
        note = 'Technical work stopped: ' if kind == 'failed' else 'Technical work finished: '
        kept = (' The hold and its pause stay.' if lane.get('held_merge_pr_url') else ' The decline and its pause stay.'
                if declined else ' The decision remains pending.')
        self._message(task['bot_id'], (note + str(summary or ''))[:500] + kept,
                      task_id=task['id'], kind='activity')
        self._refresh_bot(task['bot_id'])
        return True
