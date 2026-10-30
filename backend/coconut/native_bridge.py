"""Connect native Hermes tools to Coconut receipts without changing Hermes source."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import threading
import time


def fingerprint(action):
    return hashlib.sha256(json.dumps(action, separators=(',', ':'), sort_keys=True).encode()).hexdigest()


def kanban_arguments(args, task_id, turn_id, bot_id, workspaces=None):
    """The arguments of kanban_create for a Coconut turn: the bot's tenant, a blocked card, an idempotency key of
    the turn's input, and a folder of its own for a scratch workspace. `workspaces` is the workspace root of Coconut's
    board. The worker reads it from Hermes (HERMES_KANBAN_WORKSPACES_ROOT), and the service passes it."""
    input_id = turn_id or task_id
    intake_key = fingerprint([input_id, args])
    args = dict(args, assignee='default', tenant=bot_id, session_id=task_id, initial_status='blocked',
                idempotency_key='coconut:' + task_id + ':' + intake_key)
    if not args.get('workspace_kind') or args['workspace_kind'] == 'scratch':
        if workspaces is None:
            from hermes_cli.kanban_db import workspaces_root
            workspaces = workspaces_root()
        args.update(workspace_kind='dir', workspace_path=str(Path(workspaces) /
            ('coconut-' + fingerprint([task_id, intake_key]))))
    return args


class TurnPaused(BaseException):
    """Unwind the native turn after Coconut receives a decision."""


class NativeBridge:
    # Fast tool sequences share one update. The phone holds each label for 1.2 seconds anyway.
    PHASE_INTERVAL = 1.0

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
        self.phase = self.phase_sent = None
        self.phase_sent_at = -self.PHASE_INTERVAL
        self.phase_timer = None
        self.phases_stopped = False
        self.phase_lock = threading.RLock()
        # Running tools in start order, each with its phase. A finished tool leaves the label to the newest one left.
        self.tool_phases = {}

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
                if not owns_claim and self.request['task'].get('_native_job_finished'):
                    # Completion takes the receipt lock; claim/finish take that
                    # lock before the native fence, so release this failed fence.
                    try:
                        fence.close()
                        from cron import jobs
                        from hermes_constants import get_hermes_home
                        if __package__:
                            from .native_routine_receipts import completed, continued_failure
                        else:
                            from native_routine_receipts import completed, continued_failure
                        task = self.request['task']
                        provenance = task.get('_native_job_completion') or {}
                        completion = provenance.get('completion', provenance)
                        owns_claim = completed(get_hermes_home(), job['id'], job['fire_claim']['by'], jobs, completion)
                        continuation = provenance.get('continuation') or {}
                        if (not owns_claim and provenance == dict(job_id=job['id'], owner=job['fire_claim']['by'],
                                completion=completion, continuation=continuation)
                                and all(isinstance(continuation.get(key), str) and continuation[key]
                                        for key in ('delivery_id', 'task_id', 'turn_id', 'native_task_id'))
                                and continuation.get('task_id') == task['id']
                                and continuation.get('turn_id') == task.get('_native_turn_id')
                                and continuation.get('native_task_id') == task.get('_native_outcome_id')):
                            owns_claim = fence.enter_context(continued_failure(
                                get_hermes_home(), job['id'], job['fire_claim']['by'], jobs, completion, continuation))
                    except Exception as error:
                        raise TurnPaused('The routine completion could not be confirmed.') from error
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
            if record.get('status') == 'rejected':
                return json.dumps({'error': record.get('error') or 'The service refused this tool. The tool did not run.'})
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
            from .turn_phase import tool_phase
        else:
            from native_turn_scope import current as turn_context
            from turn_phase import tool_phase
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
                # The outer tool already describes this nested call.
                return recorded(tool_name, args, next_call, **kwargs)
            call = self.tool_phase_started(tool_phase(tool_name, args))
            try:
                return scoped(tool_name, args, next_call, **kwargs)
            finally:
                self.tool_phase_finished(call)
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

    def report_phase(self, phase):
        # The phone shows one short label. A lost label must not affect the tool call.
        if phase is None or self.request.get('operation') == 'context':
            return
        with self.phase_lock:
            if self.phases_stopped:
                return
            self.phase = phase
            wait = self.phase_sent_at + self.PHASE_INTERVAL - time.monotonic()
            if wait <= 0:
                self.send_phase()
            elif self.phase_timer is None:
                self.phase_timer = threading.Timer(wait, self.send_phase)
                self.phase_timer.daemon = True
                self.phase_timer.start()

    def send_phase(self):
        # Emit under the lock, so parallel tools cannot deliver labels out of order.
        with self.phase_lock:
            self.phase_timer = None
            if self.phases_stopped or self.phase == self.phase_sent:
                return
            self.phase_sent, self.phase_sent_at = self.phase, time.monotonic()
            try:
                self.emit('phase', {'phase': self.phase})
            except Exception:
                pass

    def tool_phase_started(self, phase):
        call = object()
        with self.phase_lock:
            self.tool_phases[call] = phase
            self.report_phase(phase)
        return call

    def tool_phase_finished(self, call):
        # Parallel tools share one label. The bot thinks again only after the last one returns.
        with self.phase_lock:
            self.tool_phases.pop(call, None)
            remaining = [phase for phase in self.tool_phases.values() if phase is not None]
            if remaining:
                self.report_phase(remaining[-1])
            elif not self.tool_phases:
                self.report_phase('thinking')

    def stop_phases(self):
        # The turn outcome is the last event. No delayed label may follow it.
        with self.phase_lock:
            self.phases_stopped = True
            if self.phase_timer is not None:
                self.phase_timer.cancel()
                self.phase_timer = None

    def close(self):
        self.stop_phases()
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
        lane = self.request['task'].get('_technical_lane')
        if self.request['operation'] == 'context' or lane:
            # Independent technical work cannot ask for a new permission while its decision is pending or declined.
            reason = ('The held decision and its pause stay.' if (lane or {}).get('held_merge_pr_url')
                      else 'The declined decision and its pause stay.' if (lane or {}).get('declined_merge_pr_url')
                      else 'The original decision remains pending.')
            resolve_gateway_approval(self.session_key, 'deny', request_id=data.get('request_id'), reason=reason)
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


# The card tools of the worker. The coconut plugin of the gateway offers the same tools (card_tools).
try:
    from .card_tools import QUESTION_SCHEMA, REPLY_SCHEMA, SUGGEST_SCHEMA
except ImportError:
    from card_tools import QUESTION_SCHEMA, REPLY_SCHEMA, SUGGEST_SCHEMA
