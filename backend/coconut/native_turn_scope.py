"""Keep Coconut tool handlers local to a native Hermes session."""
from contextvars import ContextVar
import json
import threading


current = ContextVar('coconut_native_turn', default=None)
setup_lock = threading.RLock()
registered_names = set()
active_scopes = set()


class TurnUnconfirmed(BaseException):
    """Stop one turn while Coconut reconciles its durable tool receipt."""


def stop_unconfirmed():
    scope = current.get()
    if scope is not None:
        scope.unconfirmed = True
        scope.cancelled.set()
        if scope.interrupt_scope is not None:
            scope.interrupt_scope.cancel('Coconut must confirm the last tool result.')
    raise TurnUnconfirmed()


class TurnScope:
    def __init__(self, task_id):
        self.task_id = task_id
        self.handlers = {}
        self.schemas = {}
        self.agent = None
        self.interrupt_scope = None
        self.unconfirmed = False
        self.cancelled = threading.Event()

    def register(self, *, name, handler, **definition):
        from tools.registry import registry
        self.handlers[name] = handler
        self.schemas[name] = definition['schema']
        registered_names.add(name)
        def dispatch(arguments, **kwargs):
            scope = current.get()
            if scope is None or name not in scope.handlers:
                return json.dumps({'error': 'This tool is unavailable in the current turn.'})
            if kwargs.get('task_id') not in (None, scope.task_id):
                return json.dumps({'error': 'Return this request to your parent in the delegation result.'})
            if scope.cancelled.is_set():
                raise InterruptedError('The task was cancelled.')
            return scope.handlers[name](arguments, **kwargs)
        registry.register(name=name, handler=dispatch, **definition)

    def toolset(self, names):
        from toolsets import create_custom_toolset
        # Native tool membership uses its registered toolset, not an alias name.
        create_custom_toolset('coconut', 'Native Coconut actions', tools=sorted(registered_names))
        return 'coconut'

    def model_request(self, request, **kwargs):
        if current.get() is self and self.unconfirmed:
            raise TurnUnconfirmed()
        if current.get() is not self or not isinstance(request.get('tools'), list):
            return None
        tools = []
        for tool in request['tools']:
            schema = tool.get('function', tool)
            name = schema.get('name', '')
            if name.startswith('coconut_'):
                if name not in self.schemas:
                    continue
                definition = self.schemas[name]
                if 'function' in tool:
                    tool = dict(tool, function=dict(tool['function'], **definition))
                elif 'input_schema' in tool:
                    tool = dict(tool, description=definition.get('description', ''), input_schema=definition['parameters'])
                else:
                    tool = dict(tool, **definition)
            tools.append(tool)
        return {'request': dict(request, tools=tools)}
