import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coconut.providers import HermesProvider
from coconut.native_bridge import NativeBridge
from coconut.native_routines import unsupported


class RoutineModelBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.current = dict(model='current-model', provider='current-provider', reasoning_effort='high')
        self.catalog = Mock()
        self.provider = HermesProvider(self.tmp.name, catalog=self.catalog)
        self.provider.resolve_model = Mock(return_value=self.current)

    def test_pinned_route_survives_bot_default_change(self):
        pinned = dict(model='saved-model', provider='saved-provider', reasoning_effort='low')
        self.provider.resolve_model.side_effect = ValueError('The current default is unavailable')
        self.catalog.resolve.side_effect = ValueError('Not in the global inventory')
        actual = self.provider.resolve_task_model({'id': 'bot'}, {'_native_job': pinned})
        self.assertEqual(actual, pinned)
        self.catalog.resolve.assert_not_called()
        self.provider.resolve_model.assert_not_called()
        self.assertIsNone(unsupported(pinned))

    def test_unpinned_task_keeps_current_route(self):
        self.assertEqual(self.provider.resolve_task_model({'id': 'bot'}, {}), self.current)
        self.catalog.resolve.assert_not_called()

    def test_invalid_pin_fails_without_falling_back(self):
        with self.assertRaises(ValueError):
            self.provider.resolve_task_model({'id': 'bot'}, {'_native_job': {'model': 3, 'provider': 'saved'}})

    def test_app_created_routine_saves_effective_route(self):
        self.provider.observe_bot = Mock(return_value={'id': 'bot'})
        self.provider.native_settings('routine_create', [{'id': 'bot'}], bot_id='bot',
                                      changes={'prompt': 'Check assigned PRs'}, request_key='key')
        changes = self.catalog._call.call_args.kwargs['changes']
        self.assertEqual({k: changes[k] for k in self.current}, self.current)

    def test_native_tool_records_the_actual_route_before_creation(self):
        holder = {}
        receipts = []
        called = []
        class Context:
            def __init__(self, *args): pass
            def register_middleware(self, kind, callback):
                holder['callback'] = callback
                return SimpleNamespace(dispose=lambda: None)
        modules = {
            'tools.approval': SimpleNamespace(register_gateway_notify=lambda *a: None, unregister_gateway_notify=lambda *a: None),
            'hermes_cli.plugins': SimpleNamespace(PluginContext=Context, PluginManifest=lambda **kw: None, get_plugin_manager=lambda: None),
            'hermes_cli.middleware': SimpleNamespace(TOOL_EXECUTION_MIDDLEWARE='tool'),
            'native_routine_execution': SimpleNamespace(scheduler_result=lambda result, status: result,
                create_native_job=lambda args, task_id: called.append(args) or json.dumps({'success': True})),
        }
        def receipt(kind, data):
            receipts.append((kind, data))
            return {'status': 'recorded', 'id': 'receipt'}
        request = dict(self.current, operation='start', task={'id': 'task'})
        with patch.dict('sys.modules', modules):
            bridge = NativeBridge(request, lambda *args: None, receipt)
            bridge.install(None)
            self.addCleanup(bridge.registration.dispose)
            holder['callback']('cronjob_manage', {'action': 'create', 'prompt': 'Check PRs'},
                               lambda args: self.fail('The native model handler drops route pins'))
            self.assertEqual({k: called[0][k] for k in self.current}, self.current)
            self.assertEqual(receipts[0][1]['arguments'], called[0])
            explicit = dict(action='create', model='custom', provider='custom-provider')
            holder['callback']('cronjob_manage', explicit, lambda args: self.fail('Must use the programmatic entry point'))
            self.assertEqual(called[1], explicit)
