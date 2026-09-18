"""Connect native Hermes tools to Coconut receipts without changing Hermes source."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import hashlib
import json
import threading


def fingerprint(action):
    return hashlib.sha256(json.dumps(action, separators=(',', ':'), sort_keys=True).encode()).hexdigest()


def kanban_arguments(args, task_id, turn_id, bot_id):
    input_id = turn_id or task_id
    intake_key = fingerprint([input_id, args])
    args = dict(args, assignee='default', tenant=bot_id, session_id=task_id, initial_status='blocked',
                idempotency_key='coconut:' + task_id + ':' + intake_key)
    if not args.get('workspace_kind') or args['workspace_kind'] == 'scratch':
        from hermes_cli.kanban_db import workspaces_root
        args.update(workspace_kind='dir', workspace_path=str(workspaces_root() /
            ('coconut-' + fingerprint([task_id, intake_key]))))
    return args


class TurnPaused(BaseException):
    """Unwind the native turn after Coconut receives a decision."""


class NativeBridge:
    def __init__(self, request, emit, receipt):
        self.request, self.emit, self.receipt = request, emit, receipt
        self.local = threading.local()
        self.state_lock = threading.RLock()
        self.execution_scope = ContextVar("coconut_tool_execution", default=None)
        self.claim_held = ContextVar('coconut_routine_claim', default=False)
        self.approval_used = False
        self.paused = threading.Event()
        self.completed = {}
        for item in request['task'].get('_native_receipts', []):
            self.completed.setdefault(item['action_hash'], []).append(item['result'])
        self.decision = request.get('decision') or {}
        self.session_key = request['task']['id']
        self.execution_owner = ContextVar('coconut_execution_owner', default=self.session_key)

    @contextmanager
    def claim(self):
        job = self.request['task'].get('_native_job')
        if job and not self.claim_held.get():
            fence = ExitStack()
            try:
                try:
                    from cron.jobs import fire_claim_fence
                    owns_claim = fence.enter_context(fire_claim_fence(job['id'], expected_owner=job['fire_claim']['by']))
                except Exception as error:
                    # Native callbacks can ignore ordinary exceptions. A failed
                    # ownership check must stop execution instead.
                    raise TurnPaused('The routine claim could not be confirmed.') from error
                if not owns_claim:
                    raise TurnPaused('This routine no longer owns its native execution claim.')
                yield
            finally:
                try:
                    fence.close()
                except Exception as error:
                    raise TurnPaused('The routine claim could not be released.') from error
        else:
            yield

    def start_execution(self):
        if __package__:
            from .native_turn_scope import current
        else:
            from native_turn_scope import current
        scope = current.get()
        if self.paused.is_set() or (scope is not None and scope.cancelled.is_set()):
            raise TurnPaused()
        fence = getattr(self.local, 'fence', None)
        if fence is not None and not self.claim_held.get():
            # Hermes calls this after its dispatch gate. Holding the claim before
            # that gate blocks earlier calls behind later calls until its timeout.
            fence.enter_context(self.claim())
            self.claim_held.set(True)
        if self.paused.is_set() or (scope is not None and scope.cancelled.is_set()):
            raise TurnPaused()

    def install(self, registry):
        from tools.approval import register_gateway_notify
        register_gateway_notify(self.session_key, self.approval)
        from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
        from hermes_cli.middleware import TOOL_EXECUTION_MIDDLEWARE
        def recorded(tool_name, args, next_call, **kwargs):
            if self.paused.is_set():
                raise TurnPaused()
            name = tool_name
            if name.startswith('coconut_'):
                return next_call(args)
            if name == 'cronjob_manage' and args.get('action', '').strip().lower() == 'create':
                # Native auto-detection can differ from the route selected for this bot.
                if not any(args.get(key) for key in ('model', 'provider', 'base_url')):
                    args = dict(args, model=self.request['model'], provider=self.request['provider'],
                                reasoning_effort=args.get('reasoning_effort') or self.request.get('reasoning_effort'))
                from native_routine_execution import create_native_job
                def next_call(arguments):
                    self.start_execution()
                    return create_native_job(arguments, self.session_key)
            if name == 'kanban_create':
                # A continuation must match the original intake identity before creating another outcome.
                for saved in self.request['task'].get('_native_receipts', []):
                    if saved['action']['name'] != name:
                        continue
                    original = kanban_arguments(args, saved['task_id'], saved.get('turn_id'), self.request['bot']['id'])
                    if fingerprint(dict(name=name, arguments=original)) == saved['action_hash']:
                        args = original
                        break
                else:
                    args = kanban_arguments(args, self.session_key, self.request['task'].get('_native_turn_id'), self.request['bot']['id'])
            action = dict(name=name, arguments=args)
            key = fingerprint(action)
            pending = self.request['task'].get('pending_decision', {})
            if self.request['operation'] == 'context' and pending.get('action') == action:
                return json.dumps({'error': 'This action is still awaiting a decision.'})
            with self.state_lock:
                if self.completed.get(key):
                    return self.completed[key].pop(0)
            needs_new_approval = (self.decision.get('action_type') == 'native_approval'
                                  and fingerprint(self.decision['action']) == key and self.decision['status'] == 'replied')
            if self.decision.get('action_type') == 'native_approval' and fingerprint(self.decision['action']) == key:
                if self.decision['status'] != 'approved' and not needs_new_approval:
                    return json.dumps({'error': 'The person did not approve this action.', 'correction': self.decision.get('correction')})
            # Some receipts enqueue native runs. Protect that write separately,
            # then release the claim before Hermes waits at its dispatch gate.
            with self.claim():
                record = self.receipt('native_call', action)
            if record.get('status') != 'recorded':
                return json.dumps({'error': 'The Mac did not persist the tool intent. The tool did not run.'})
            if needs_new_approval:
                # A text reply closes the old card without granting its exact tool permission.
                self.pause('native_approval', dict(receipt_id=record['id'], title=self.decision['title'],
                    explanation=self.decision.get('explanation', ''), resource=self.decision.get('resource', '')))
            if 'routine_result' in record:
                # Coconut records the queued run atomically. Do not start a second native runner.
                return record['routine_result']
            self.local.call = dict(id=record['id'], action=action)
            try:
                relay = getattr(self, 'relay', None)
                execution_args = args
                if name == 'terminal':
                    if __package__:
                        from .worker_activity import instrument
                    else:
                        from worker_activity import instrument
                    execution_args = instrument(args, record['id'])
                if name == 'message_agent' and relay:
                    self.start_execution()
                    result = relay.deliver(args, next_call, record['id'])
                else:
                    result = next_call(execution_args)
                if name == 'cronjob_manage':
                    from native_routine_execution import scheduler_result
                    result = scheduler_result(result, record)
                consumed = []
                if name in ('process', 'process_manage') and args.get('session_id'):
                    from tools.process_registry import process_registry
                    process = process_registry.get(args['session_id'])
                    if process and process_registry.is_completion_consumed(process.id):
                        consumed.append(process.id)
                confirmed = self.receipt('native_result', dict(id=record['id'], result=result, consumed_process_ids=consumed))
                if confirmed.get('status') != 'recorded':
                    if __package__:
                        from .native_turn_scope import stop_unconfirmed
                    else:
                        from native_turn_scope import stop_unconfirmed
                    stop_unconfirmed()
                return result
            finally:
                self.local.call = None
        def scoped(tool_name, args, next_call, **kwargs):
            active = threading.Event()
            active.set()
            token = self.execution_scope.set(active)
            claim_token = self.claim_held.set(False)
            try:
                with ExitStack() as fence:
                    self.local.fence = fence
                    # Direct native tool dispatch has no agent start callback.
                    if agent is None:
                        self.start_execution()
                    return recorded(tool_name, args, next_call, **kwargs)
            finally:
                self.local.fence = None
                self.claim_held.reset(claim_token)
                active.clear()
                self.execution_scope.reset(token)

        if __package__:
            from .native_turn_scope import current as turn_context
        else:
            from native_turn_scope import current as turn_context
        requires_scope = turn_context.get() is not None
        agent = turn_context.get().agent if requires_scope else None
        def execute(tool_name, args, next_call, **kwargs):
            if __package__:
                from .native_turn_scope import current
            else:
                from native_turn_scope import current
            scope = current.get()
            if requires_scope and scope is None:
                return json.dumps({'error': 'The native tool lost its work session. The tool did not run.'})
            if scope is not None and scope.task_id != self.session_key:
                return next_call(args)
            if scope is not None and scope.cancelled.is_set():
                raise TurnPaused()
            owner = kwargs.get('task_id') or self.execution_owner.get()
            if owner != self.session_key:
                # Native children keep their own execution history. Their durable
                # delegation result belongs to this parent; individual calls do not.
                token = self.execution_owner.set(owner)
                try:
                    if tool_name.startswith('coconut_'):
                        return json.dumps({'error': 'Return this request to your parent in the delegation result.'})
                    return next_call(args)
                finally:
                    self.execution_owner.reset(token)
            # Sandbox RPC threads inherit the claim held by their parent tool.
            active = self.execution_scope.get()
            if active is not None and active.is_set():
                return recorded(tool_name, args, next_call, **kwargs)
            return scoped(tool_name, args, next_call, **kwargs)
        if agent is not None:
            self.agent = agent
            self.previous_tool_start = getattr(agent, 'tool_start_callback', None)
            def started(*args):
                self.start_execution()
                if self.previous_tool_start is not None:
                    self.previous_tool_start(*args)
            self.tool_start = started
            agent.tool_start_callback = started
        if self.request['task'].get('_execution_session_id'):
            self.model_registration = PluginContext(PluginManifest(name='coconut-model-tools', version='1'), get_plugin_manager()).register_middleware(
                'llm_request', turn_context.get().model_request)
        self.recorded = recorded
        self.registration = PluginContext(PluginManifest(name='coconut-runtime', version='1'), get_plugin_manager()).register_middleware(TOOL_EXECUTION_MIDDLEWARE, execute)

    def close(self):
        from tools.approval import unregister_gateway_notify
        unregister_gateway_notify(self.session_key)
        model_registration = getattr(self, 'model_registration', None)
        if model_registration is not None:
            model_registration.dispose()
        registration = getattr(self, 'registration', None)
        if registration is not None:
            registration.dispose()
        agent = getattr(self, 'agent', None)
        if agent is not None and agent.tool_start_callback is self.tool_start:
            agent.tool_start_callback = self.previous_tool_start

    def pause(self, kind, data):
        # Other tools in the native batch must stop after a decision, too.
        self.paused.set()
        self.emit(kind, data)
        raise TurnPaused()

    def approval(self, data):
        from tools.approval import resolve_gateway_approval
        if self.request['operation'] == 'context':
            resolve_gateway_approval(self.session_key, 'deny', request_id=data.get('request_id'), reason='The original decision remains pending.')
            return
        call = getattr(self.local, 'call', None)
        if not call:
            resolve_gateway_approval(self.session_key, 'deny', request_id=data.get('request_id'), reason='No durable tool intent.')
            return
        with self.state_lock:
            approved = (not self.approval_used and self.decision.get('action_type') == 'native_approval'
                and self.decision.get('status') == 'approved' and fingerprint(call['action']) == fingerprint(self.decision['action']))
            if approved:
                self.approval_used = True
        if approved:
            resolve_gateway_approval(self.session_key, 'once', request_id=data.get('request_id'))
            return
        self.pause('native_approval', dict(receipt_id=call['id'], title='Allow this action?',
                                         explanation=data.get('description', ''), resource=data.get('command', '')))

    def clarify(self, question, choices):
        options = [{'id': str(index), 'label': str(choice)} for index, choice in enumerate(choices or ['Continue'])]
        return self.ask({'title': question, 'options': options})

    def ask(self, arguments, **kwargs):
        if self.request['operation'] == 'context':
            return json.dumps({'error': 'Answer the current question. Its pending decision stays open.'})
        if not isinstance(arguments, dict) or not isinstance(arguments.get('options'), list):
            return json.dumps({'error': 'Supply a title and choices.'})
        call = getattr(self.local, 'call', None)
        result = self.receipt('native_question', dict(arguments, receipt_id=call['id'] if call else None))
        if result.get('status') == 'validation_failed':
            return json.dumps(result)
        # Pause only after the service accepts the card. An uncertain result must
        # also stop this batch until the service resolves whether it saved a card.
        self.paused.set()
        raise TurnPaused()


QUESTION_SCHEMA = {'name': 'coconut_ask', 'description': 'Create a required decision card in chat and Inbox, then wait for the human answer. Use for choices, permission, confirmation, missing information, and manual-step results. Never ask for these only in prose. The person can choose an option or type a reply. Honor standing preferences. Do not duplicate an existing pending card.',
                  'parameters': {'type': 'object', 'properties': {
                      'title': {'type': 'string', 'description': 'A short question naming the specific action and affected thing. Use Simplified Technical English. Example: Correct refunds in the tax report?'},
                      'explanation': {'type': 'string', 'description': 'Optional supporting detail for chat. Put only facts that change the choice in context or option explanations.'},
                      'options': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                                  'items': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'label': {'type': 'string', 'description': 'A specific, neutral action. Prefer Fix the PR or Leave unchanged to Fix or Leave blocked.'},
                                      'explanation': {'type': 'string', 'maxLength': 500, 'description': 'One short sentence adding the consequence or tradeoff of this option. Use an empty string if the label is sufficient. Preserve material limits; omit process steps and repeated context.'}}, 'required': ['id', 'label', 'explanation']}},
                      'recommended_option_id': {'type': 'string', 'description': 'Optional. Recommend one choice only when evidence and the user goal favor it. State the decisive reason once in context or that option explanation. Omit when no choice has a clear advantage. When revising, omit this field to remove the recommendation; do not send an empty ID.'}, 'predicted_option_id': {'type': 'string'}},
                  'required': ['title', 'context', 'options']}}

# These fields carry evidence, not model confidence scores.
QUESTION_SCHEMA['parameters']['properties'].update({
    'sources': {'type': 'array', 'maxItems': 8, 'description': 'Show the exact issues, PRs, emails, or documents behind this decision. Supply a short title and the exact source link when available. Do not invent links. Omit a URL when unavailable. On revision, omit sources to keep them, or replace the list; [] removes them.', 'items': {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['email', 'issue', 'pull_request', 'web']}, 'title': {'type': 'string', 'maxLength': 200}, 'url': {'type': ['string', 'null'], 'description': 'The full HTTP or HTTPS URL of the issue, PR, comment, email thread, or document.'}}, 'required': ['kind', 'title']}},
    'previews': {'type': 'array', 'maxItems': 6, 'description': 'Attach actual images inside this card for visual review. Supply local absolute PNG or JPEG paths, up to 4 MB each and 12 MB total. Images are copied when saved. On revision, omit to keep images or send a new list to replace them; [] removes them.', 'items': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'caption': {'type': 'string', 'maxLength': 160}}, 'required': ['path', 'caption']}},
    'previous_decision_id': {'type': 'string', 'description': 'For a follow-up question, the answered card ID from this conversation, including an approved draft or a completed earlier task. This links context only; it does not approve the new card. Explain the check result and why this next choice is needed in context. Coconut links the answer automatically during its immediate continuation.'},
    'merge_pr_url': {'type': ['string', 'null'], 'description': 'Required for a question whose only action is whether to merge one PR. Use its full GitHub URL after coconut_track_pr. Coconut closes this card after a verified external merge. For a new question, omit this for reviews, releases, or other actions. For a revision, omission keeps the link; use null to remove it.'},
    'context': {'type': 'string', 'description': 'Usually two or three short sentences: identify the work, explain the problem and its effect, then state what approval changes. Disclose public changes, external messages, cost, and other material limits in plain words. Keep process steps and detailed evidence in chat. Do not repeat risk or uncertainty.'},
    'evidence': {'type': 'string', 'description': 'One short sentence naming the decisive check or linking its source. Do not repeat context or invent checks.'},
    'uncertainty': {'type': 'string', 'description': 'One short sentence naming a remaining unknown that could change this decision. Resolve allowed checks before asking. Omit unrelated future caveats and unknowns already stated elsewhere.'},
    'risk': {'type': 'string', 'description': 'One short sentence stating what could go wrong with the proposed action and who or what it affects. Add a verified impact or recovery limit when useful. Omit if there is no material risk. Do not invent reassurance or a risk rating.'},
})
try:
    from .message_drafts import DRAFT_SCHEMA
except ImportError:
    from message_drafts import DRAFT_SCHEMA

SUGGEST_SCHEMA = {
    'name': 'coconut_suggest',
    'description': 'Create an optional proposal card in chat and Inbox while continuing authorized work. Use for offers such as investigating, fixing, or doing something extra instead of offering only in prose. Do not execute the proposal until authorized. Reuse the same key when retrying. Do not recreate a dismissed proposal.',
    'parameters': dict(QUESTION_SCHEMA['parameters'],
                       properties=dict(QUESTION_SCHEMA['parameters']['properties'], key={'type': 'string'}, draft=DRAFT_SCHEMA),
                       required=['key', 'title'],
                       anyOf=[{'required': ['options']}, {'required': ['draft']}]),
}
REPLY_SCHEMA = {
    'name': 'coconut_resolve_reply',
    'description': 'Record an unambiguous human answer to the current decision. Questions and uncertain intent must stay pending. Native tool approvals still require a visible choice.',
    'parameters': {'type': 'object', 'properties': {
        'choice': {'type': 'string', 'enum': ['approve', 'reject', 'modify']},
        'option_id': {'type': 'string', 'description': 'The current option ID for an explicit selection.'},
        'revision': dict(QUESTION_SCHEMA['parameters'],
                         properties=dict(QUESTION_SCHEMA['parameters']['properties'], draft=DRAFT_SCHEMA),
                         required=[], anyOf=[{'required': ['title', 'options']}, {'required': ['draft']}]),
        'quote': {'type': 'string', 'description': 'Exact words from this human reply that give the instruction.'},
    }, 'required': ['choice', 'quote']},
}
