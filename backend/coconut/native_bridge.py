"""Connect native Hermes tools to Coconut receipts without changing Hermes source."""
from contextvars import ContextVar
import hashlib
import json
import threading


def fingerprint(action):
    return hashlib.sha256(json.dumps(action, separators=(',', ':'), sort_keys=True).encode()).hexdigest()


class TurnPaused(BaseException):
    """Unwind the native turn after Coconut receives a decision."""


class NativeBridge:
    def __init__(self, request, emit, receipt):
        self.request, self.emit, self.receipt = request, emit, receipt
        self.local = threading.local()
        self.execution_lock = threading.RLock()
        self.execution_scope = ContextVar("coconut_tool_execution", default=None)
        self.approval_used = False
        self.paused = threading.Event()
        self.completed = {}
        for item in request['task'].get('_native_receipts', []):
            self.completed.setdefault(item['action_hash'], []).append(item['result'])
        self.decision = request.get('decision') or {}
        self.session_key = request['task']['id']

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
                next_call = lambda arguments: create_native_job(arguments, self.session_key)
            action = dict(name=name, arguments=args)
            key = fingerprint(action)
            pending = self.request['task'].get('pending_decision', {})
            if self.request['operation'] == 'context' and pending.get('action') == action:
                return json.dumps({'error': 'This action is still awaiting a decision.'})
            if self.completed.get(key):
                return self.completed[key].pop(0)
            if self.decision.get('action_type') == 'native_approval' and fingerprint(self.decision['action']) == key:
                if self.decision['status'] != 'approved':
                    return json.dumps({'error': 'The person did not approve this action.', 'correction': self.decision.get('correction')})
            record = self.receipt('native_call', action)
            if record.get('status') != 'recorded':
                return json.dumps({'error': 'The Mac did not persist the tool intent. The tool did not run.'})
            self.local.call = dict(id=record['id'], action=action)
            try:
                relay = getattr(self, 'relay', None)
                result = relay.deliver(args, next_call, record['id']) if name == 'message_agent' and relay else next_call(args)
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
                    # End the worker before the native middleware can swallow this failure.
                    import os
                    os._exit(74)
                return result
            finally:
                self.local.call = None
        def scoped(tool_name, args, next_call, **kwargs):
            active = threading.Event()
            active.set()
            token = self.execution_scope.set(active)
            try:
                return recorded(tool_name, args, next_call, **kwargs)
            finally:
                active.clear()
                self.execution_scope.reset(token)

        def serialized(tool_name, args, next_call, **kwargs):
            # Hermes propagates ContextVars into sandbox RPC threads. The parent
            # already holds the lock and cron fence while it waits for these calls.
            active = self.execution_scope.get()
            if active is not None and active.is_set():
                return recorded(tool_name, args, next_call, **kwargs)
            with self.execution_lock:
                job = self.request['task'].get('_native_job')
                if job:
                    from cron.jobs import fire_claim_fence
                    with fire_claim_fence(job['id'], expected_owner=job['fire_claim']['by']) as owns_claim:
                        if not owns_claim:
                            return json.dumps({'error': 'This routine no longer owns its native execution claim.'})
                        return scoped(tool_name, args, next_call, **kwargs)
                return scoped(tool_name, args, next_call, **kwargs)
        self.registration = PluginContext(PluginManifest(name='coconut-runtime', version='1'), get_plugin_manager()).register_middleware(TOOL_EXECUTION_MIDDLEWARE, serialized)

    def close(self):
        from tools.approval import unregister_gateway_notify
        unregister_gateway_notify(self.session_key)
        registration = getattr(self, 'registration', None)
        if registration is not None:
            registration.dispose()

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
        if not self.approval_used and self.decision.get('action_type') == 'native_approval' and self.decision.get('status') == 'approved' and fingerprint(call['action']) == fingerprint(self.decision['action']):
            self.approval_used = True
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
        self.pause('native_question', dict(arguments, receipt_id=call['id'] if call else None))


QUESTION_SCHEMA = {'name': 'coconut_ask', 'description': 'Create a required decision card in chat and Inbox, then wait for the human answer. Use for choices, permission, confirmation, missing information, and manual-step results. Never ask for these only in prose. The person can choose an option or type a reply. Honor standing preferences. Do not duplicate an existing pending card.',
                  'parameters': {'type': 'object', 'properties': {
                      'title': {'type': 'string', 'description': 'One plain question about the outcome, usually eight words or fewer.'},
                      'explanation': {'type': 'string', 'description': 'Optional supporting detail for chat. Put only facts that change the choice in context or option explanations.'},
                      'options': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                                  'items': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'label': {'type': 'string'},
                                      'explanation': {'type': 'string', 'maxLength': 500, 'description': 'Plain effect of this choice, usually fifteen words or fewer. Keep important limits. Omit hashes, tool names, process steps, and repeated context.'}}, 'required': ['id', 'label', 'explanation']}},
                      'recommended_option_id': {'type': 'string', 'description': 'Optional. Recommend one choice only when evidence and the user goal favor it. State the decisive reason once in context or that option explanation. Omit when no choice has a clear advantage.'}, 'predicted_option_id': {'type': 'string'}},
                  'required': ['title', 'context', 'options']}}

# These fields carry evidence, not model confidence scores.
QUESTION_SCHEMA['parameters']['properties'].update({
    'merge_pr_url': {'type': 'string', 'description': 'Required for a question whose only action is whether to merge one PR. Use its full GitHub URL after coconut_track_pr. Coconut closes this card after a verified external merge. Omit for reviews, releases, or choices with other actions.'},
    'context': {'type': 'string', 'description': 'Name the affected project or task and explain why this choice is needed. State the practical approval scope, including publishing limits. The card must make sense without nearby messages. Use short plain sentences; keep technical evidence in chat.'},
    'evidence': {'type': 'string', 'description': 'One short sentence naming the decisive check or linking its source. Do not repeat context or invent checks.'},
    'uncertainty': {'type': 'string', 'description': 'One short sentence: the material unknown that could change the choice. Omit if none.'},
    'risk': {'type': 'string', 'description': 'One short sentence: the material risk or recovery limit. Omit if none.'},
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
