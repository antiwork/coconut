from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.model_catalog import resolve_selection
from coconut.profiles import HermesProfiles
from coconut.service import Service, ServiceError


class NativeProvider:
    name = 'hermes-test'
    ready = True

    def __init__(self, root):
        self.root = Path(root)
        self.service = None
        self.started = []
        self.writes = []
        self.models = [
            dict(id='model-a', name='Model A', provider='native-a', base_url='https://a.example', reasoning_efforts=['low', 'high'], default_reasoning_effort='high'),
            dict(id='model-b', name='Model B', provider='native-b', base_url='https://b.example', reasoning_efforts=['medium'], default_reasoning_effort=None),
        ]
        self.default = dict(model='model-a', provider='native-a', base_url='https://a.example', reasoning_effort='low')
        self.native = {}

    def outside_database_lock(self):
        if self.service is not None:
            assert not self.service.lock._is_owned(), 'Native settings must run outside the database lock.'

    def model_catalog(self):
        self.outside_database_lock()
        return deepcopy(dict(default=self.default, models=self.models, default_configured=True,
                             model_source='Native test inventory', default_source='Native test config'))

    def resolve_model(self, bot, catalog=None):
        if bot.get('_native_effective_model'):
            return dict(bot['_native_effective_model'])
        return resolve_selection(bot.get('model_override'), catalog or self.model_catalog())

    def observe_bots(self, bots):
        self.outside_database_lock()
        return {bot['id']: self.observe_bot(bot) for bot in bots}

    def observe_bot(self, bot):
        self.outside_database_lock()
        return dict(bot, model_override=deepcopy(self.native.get(bot['id'])),
                    instructions=HermesProfiles(self.root).read_instructions(bot))

    def set_bot_model(self, bot, selection):
        self.outside_database_lock()
        self.native[bot['id']] = deepcopy(selection)
        self.writes.append(('bot', bot['id'], deepcopy(selection)))

    def set_model_default(self, selection):
        self.outside_database_lock()
        route = resolve_selection(selection, self.model_catalog())
        route['reasoning_effort'] = selection.get('reasoning_effort')
        self.default = route
        self.writes.append(('default', deepcopy(selection)))
        return self.model_catalog()

    def migrate_model_default(self, model, base_url):
        pass

    def start_task(self, task, bot, history, emit):
        bot = self.observe_bot(bot)
        self.started.append(self.resolve_model(bot))
        emit('text_delta', {'text': '2'})
        emit('completed', {'summary': 'Complete.'})

    def cancel_task(self, task_id):
        pass


class BotModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.provider = NativeProvider(self.root)
        self.service = Service(self.root, self.provider, start_workers=False)
        self.provider.service = self.service

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def write(self, method, path, data, key=None):
        return self.service.mutate(method, path, data, key or str(uuid4()), 'test-phone')

    def bot(self, name='Ada'):
        return self.write('POST', '/v1/bots', {'name': name, 'instructions': 'Initial instructions.'})

    def selection(self, model='model-b', effort=None):
        return dict(model=model, provider='native-b' if model == 'model-b' else 'native-a', reasoning_effort=effort)

    def test_native_default_and_override_are_visible_without_database_lock(self):
        bot = self.bot()
        self.assertIsNone(bot['model_override'])
        state = self.service.snapshot()
        self.assertTrue(state['model_configured'])
        self.assertEqual(state['bots'][0]['effective_model']['model'], 'model-a')
        selected = self.selection(effort='medium')
        response = self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': selected})
        self.assertEqual(response['model_override'], selected)
        self.assertEqual(self.provider.native[bot['id']], selected)
        self.assertEqual(response['effective_model']['reasoning_effort'], 'medium')
        # Coconut keeps no identity file in the profile. The native config holds the model.
        self.assertFalse((self.root / 'bots' / bot['id'] / 'home/coconut-profile.json').exists())
        self.service.close()
        self.service = Service(self.root, self.provider, start_workers=False)
        self.provider.service = self.service
        self.assertEqual(self.service.snapshot()['bots'][0]['model_override'], selected)

    def test_external_native_changes_and_queued_dispatch_use_current_selection(self):
        first, second = self.bot(), self.bot('Lin')
        task = self.write('POST', '/v1/bots/' + first['id'] + '/messages', {'text': 'Check the answer', 'mode': 'queue'})
        self.provider.native[first['id']] = self.selection(effort='medium')
        self.provider.default['reasoning_effort'] = 'high'
        state = self.service.snapshot()
        self.assertEqual(state['bots'][0]['effective_model']['model'], 'model-b')
        self.assertEqual(state['bots'][1]['effective_model']['reasoning_effort'], 'high')
        self.service._run_task(task['id'])
        self.assertEqual(self.provider.started[0]['model'], 'model-b')
        self.write('PATCH', '/v1/bots/' + first['id'], {'model_override': None})
        self.assertIsNone(self.provider.native[first['id']])
        self.assertEqual(self.service.snapshot()['bots'][0]['effective_model']['model'], 'model-a')

    def test_invalid_selection_cannot_change_native_profile(self):
        bot = self.bot()
        for selection in (self.selection('missing'), self.selection(effort='high'), self.selection() | {'base_url': 'https://other.example'}):
            with self.subTest(selection=selection), self.assertRaises(ServiceError):
                self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': selection})
        self.assertEqual(self.provider.writes, [])
        self.assertIsNone(self.service.snapshot()['bots'][0]['model_override'])

    def test_cold_inventory_uses_bounded_validation_for_an_explicit_save(self):
        bot = self.bot()
        complete = self.provider.model_catalog()
        loading = dict(complete, models=[], loading=True)
        def validate():
            self.provider.outside_database_lock()
            return complete
        with patch.object(self.provider, 'model_catalog', return_value=loading), patch.object(self.provider, 'validation_model_catalog', side_effect=validate, create=True) as validation:
            response = self.write('PATCH', '/v1/bots/' + bot['id'], {'model_override': self.selection(effort='medium')})
        self.assertEqual(validation.call_count, 1)
        self.assertEqual(response['effective_model']['model'], 'model-b')

    def test_native_instructions_survive_rename_and_explicit_edit_can_clear(self):
        bot = self.bot()
        soul = self.root / 'bots' / bot['id'] / 'home/SOUL.md'
        soul.write_text('Instructions from Hermes Terminal.')
        self.write('PATCH', '/v1/bots/' + bot['id'], {'name': 'Renamed'})
        self.assertEqual(soul.read_text(), 'Instructions from Hermes Terminal.')
        self.assertEqual(self.service.snapshot()['bots'][0]['instructions'], soul.read_text())
        self.write('PATCH', '/v1/bots/' + bot['id'], {'instructions': ''})
        self.assertEqual(soul.read_text(), '')
        self.assertEqual(self.service.snapshot()['bots'][0]['instructions'], '')

    def test_native_default_receipt_failure_cannot_revert_a_newer_default(self):
        key = str(uuid4())
        older = self.selection('model-a', None)
        with patch.object(self.service, '_finish_model_update', side_effect=RuntimeError('receipt commit failed')):
            with self.assertRaises(RuntimeError):
                self.write('PUT', '/v1/settings/model', older, key)
        self.assertIsNone(self.provider.default['reasoning_effort'])
        newer = self.selection(effort='medium')
        self.write('PUT', '/v1/settings/model', newer)
        writes = len(self.provider.writes)
        recovered = self.write('PUT', '/v1/settings/model', older, key)
        self.assertEqual(recovered['model'], 'model-a')
        self.assertIsNone(recovered['model_catalog']['default']['reasoning_effort'])
        self.assertEqual(len(self.provider.writes), writes)
        self.assertEqual(self.provider.default['model'], 'model-b')

    def test_native_bot_receipt_failure_cannot_restore_older_metadata(self):
        bot = self.bot()
        key = str(uuid4())
        data = {'instructions': 'Native update', 'pinned': True}
        with patch.object(self.service, '_finish_model_update', side_effect=RuntimeError('receipt commit failed')):
            with self.assertRaises(RuntimeError):
                self.write('PATCH', '/v1/bots/' + bot['id'], data, key)
        self.write('PATCH', '/v1/bots/' + bot['id'], {'pinned': False})
        self.write('PATCH', '/v1/bots/' + bot['id'], data, key)
        observed = self.service.snapshot()['bots'][0]
        self.assertFalse(observed['pinned'])
        self.assertEqual(observed['instructions'], 'Native update')

    def test_bot_name_cannot_be_mistaken_for_a_pending_settings_intent(self):
        self.bot('_model_pending')
        response = self.write('PUT', '/v1/settings/model', self.selection())
        self.assertEqual(response['model'], 'model-b')



if __name__ == '__main__':
    unittest.main()
