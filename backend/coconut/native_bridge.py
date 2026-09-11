"""Connect native Hermes tools to Coconut receipts without changing Hermes source."""
from contextvars import ContextVar
import hashlib
import json
import threading


def fingerprint(action):
    return hashlib.sha256(json.dumps(action, separators=(',', ':'), sort_keys=True).encode()).hexdigest()


class NativeBridge:
    def __init__(self, request, emit, receipt):
        self.request, self.emit, self.receipt = request, emit, receipt
        self.local = threading.local()
        self.execution_lock = threading.RLock()
        self.execution_scope = ContextVar("coconut_tool_execution", default=None)
        self.approval_used = False
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
            name = tool_name
            if name.startswith('coconut_'):
                return next_call(args)
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
                confirmed = self.receipt('native_result', dict(id=record['id'], result=result))
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
        self.emit('native_approval', dict(receipt_id=call['id'], title='Allow this action?',
                                         explanation=data.get('description', ''), resource=data.get('command', '')))
        threading.Event().wait()

    def clarify(self, question, choices):
        options = [{'id': str(index), 'label': str(choice)} for index, choice in enumerate(choices or ['Continue'])]
        return self.ask({'title': question, 'options': options})

    def ask(self, arguments, **kwargs):
        if self.request['operation'] == 'context':
            return json.dumps({'error': 'Answer the current question. Its pending decision stays open.'})
        if not isinstance(arguments, dict) or not isinstance(arguments.get('options'), list):
            return json.dumps({'error': 'Supply a title and choices.'})
        call = getattr(self.local, 'call', None)
        self.emit('native_question', dict(arguments, receipt_id=call['id'] if call else None))
        threading.Event().wait()


QUESTION_SCHEMA = {'name': 'coconut_ask', 'description': 'Ask the person to choose. Use their standing preferences when writing choices. The Inbox keeps this question pending.',
                  'parameters': {'type': 'object', 'properties': {
                      'title': {'type': 'string'}, 'explanation': {'type': 'string'},
                      'options': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                                  'items': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'label': {'type': 'string'}}, 'required': ['id', 'label']}},
                      'recommended_option_id': {'type': 'string'}, 'predicted_option_id': {'type': 'string'}},
                  'required': ['title', 'options']}}
