import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider, HermesProvider
from coconut.service import Service, ServiceError, YieldForDecision
import legacy_work_sync
import test_provider as fixtures


setUpModule, tearDownModule = legacy_work_sync.scope()


class ProviderProfileProvenanceTests(unittest.TestCase):
    setUp = fixtures.ProviderTests.setUp
    tearDown = fixtures.ProviderTests.tearDown
    fake_hermes = fixtures.ProviderTests.fake_hermes

    def invalid_worker(self, revision):
        provider = self.fake_hermes("raise AssertionError('The raw event fixture replaces this worker.')")
        script = self.root / 'raw-worker.py'
        control = self.root / 'raw-event.json'
        script.write_text(
            'import json,sys\nfrom pathlib import Path\n'
            'request=json.loads(sys.stdin.readline())\n'
            'event=json.loads(Path(' + repr(str(control)) + ').read_text())\n'
            'event.update(turn_id=request["turn_id"],request_id=1)\n'
            'print(json.dumps(event),flush=True)\n'
            'sys.stdin.readline()\n'
            'print(json.dumps(dict(type="turn_end",turn_id=request["turn_id"],data=dict(reusable=False))),flush=True)\n')
        launch = subprocess.Popen

        def process(command, *args, **kwargs):
            if len(command) > 1 and Path(command[1]).name == 'hermes_worker.py' and '--cleanup' not in command:
                command = [command[0], str(script)]
            return launch(command, *args, **kwargs)

        for kind in ('native_call', 'native_question', 'native_proposal', 'decision_response'):
            with self.subTest(kind=kind):
                data = {} if revision is None else {'_worker_profile_revision': revision}
                control.write_text(json.dumps(dict(type=kind, data=data)))
                events = []
                with patch('coconut.providers.subprocess.Popen', side_effect=process), \
                     patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}), \
                     self.assertRaisesRegex(RuntimeError, 'captured profile revision'):
                    def emit(kind, data):
                        events.append((kind, data))
                        return {'status': 'recorded', 'id': 'fixture'}
                    provider.start_task(self.task, self.bot, [], emit)
                self.assertEqual(events, [])

    def test_real_provider_rejects_missing_worker_provenance(self):
        self.invalid_worker(None)

    def test_real_provider_rejects_malformed_worker_provenance(self):
        self.invalid_worker('not-a-captured-revision')

    def test_real_worker_cannot_replace_its_old_revision_with_a_forged_new_one(self):
        provider = self.fake_hermes('''
            import os
            from pathlib import Path
            from profiles import prompt_revision
            home = Path(os.environ['HERMES_HOME'])
            (home / 'SOUL.md').write_text('The profile changes during this model call.')
            forged = prompt_revision(home)
            registry.dispatch('coconut_ask', {'title': 'Mark this draft ready?',
                'options': [{'id': 'ready', 'label': 'Mark ready'}], '_worker_profile_revision': forged})
            raise AssertionError('The worker must pause after the card.')
        ''')
        provider.__class__ = HermesProvider
        service = Service(provider.data_dir, FixtureProvider(provider.data_dir), start_workers=False)
        self.addCleanup(service.close)
        # The worker runs in the profile that the service created.
        provider.profile_rows = service.profiles.rows

        def write(path, data):
            return service.mutate('POST', path, data, str(uuid4()), 'test-phone')

        bot = write('/v1/bots', dict(name='Probe', instructions='The original profile instruction.'))
        task = write('/v1/bots/' + bot['id'] + '/messages', dict(text='Prepare this review.', mode='queue'))
        stored = service._get('tasks', task['id'])
        stored.update(status='working', _native_turn_id='provenance-turn')
        service._put('tasks', stored)
        captured = service.profiles.prompt_revision(bot)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}), self.assertRaises(YieldForDecision):
            provider.start_task(stored, bot, [], lambda kind, data: service.emit(task['id'], kind, data))
        current = service.profiles.prompt_revision(bot)
        self.assertNotEqual(current, captured)
        decision = service._all('decisions')[-1]
        self.assertEqual(decision['_profile_revision'], captured)
        self.assertTrue(decision['assessment_stale'])
        with self.assertRaises(ServiceError) as error:
            write('/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='ready'))
        self.assertEqual(error.exception.status, 409)
        with self.assertRaises(ServiceError) as raw_error:
            write('/v1/tasks/' + task['id'] + '/events', dict(type='native_question', data={'_worker_profile_revision': current}))
        self.assertEqual(raw_error.exception.status, 404)
        self.assertEqual(service._get('decisions', decision['id'])['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
