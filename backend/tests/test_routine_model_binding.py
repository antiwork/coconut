import tempfile
import unittest
from unittest.mock import Mock

from coconut.providers import HermesProvider


class RoutineModelBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.current = dict(model='current-model', provider='current-provider', reasoning_effort='high')
        self.catalog = Mock()
        # A change needs the profile folder of the bot.
        self.catalog._profile_home.return_value = self.tmp.name
        self.provider = HermesProvider(self.tmp.name, catalog=self.catalog)
        self.provider.resolve_model = Mock(return_value=self.current)

    def test_app_created_routine_saves_effective_route(self):
        self.provider.observe_bot = Mock(return_value={'id': 'bot'})
        self.provider.native_settings('routine_create', [{'id': 'bot'}], bot_id='bot',
                                      changes={'prompt': 'Check assigned PRs'}, request_key='key')
        changes = self.catalog._call.call_args.kwargs['changes']
        self.assertEqual({k: changes[k] for k in self.current}, self.current)
