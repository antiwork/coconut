import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from coconut import model_catalog
from coconut.model_catalog import (HermesModelCatalog, ModelCatalogError, native_home,
                                    public_endpoint, resolve_selection)

PROJECT = Path(__file__).resolve().parents[2]
MODULE = PROJECT / 'backend/coconut/model_catalog.py'
MANAGED_PYTHON = PROJECT / '.runtime/hermes/venv/bin/python'
HERMES_SOURCE = PROJECT / '.runtime/hermes/source'


def example_catalog():
    return {'default': {'model': 'first', 'provider': 'provider-a', 'base_url': 'https://one.example/v1',
                        'reasoning_effort': 'high'},
            'models': [{'id': 'second', 'name': 'second', 'provider': 'provider-b',
                        'base_url': 'https://two.example/v1', 'reasoning_efforts': ['none', 'low', 'high'],
                        'default_reasoning_effort': None}]}


class ModelSelectionTests(unittest.TestCase):
    def test_model_resolution_uses_the_same_account_home_as_bot_terminals(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'run_agent.py').touch()
            catalog = HermesModelCatalog(directory, source=source, python=sys.executable)
            with patch.object(model_catalog.subprocess, 'run',
                              return_value=subprocess.CompletedProcess([], 0, '{"result": {}}', '')) as run:
                catalog._call('runtime')
            from coconut.runtime_environment import profile_environment
            environment = run.call_args.kwargs['env']
            self.assertEqual(environment['HOME'], profile_environment(Path(directory) / 'bot')['HOME'])
            self.assertEqual(environment['HERMES_REAL_HOME'], environment['HOME'])
            self.assertEqual(environment['USER'], profile_environment(Path(directory) / 'bot')['USER'])
            self.assertEqual(environment['CODEX_HOME'], str(catalog.private_home / '.codex'))
            self.assertNotIn('CLAUDE_CONFIG_DIR', environment)
            self.assertNotIn('CLAUDE_SECURESTORAGE_CONFIG_DIR', environment)

    def test_combined_native_response_preserves_both_read_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'run_agent.py').touch()
            catalog = HermesModelCatalog(directory, source=source, python=sys.executable)
            expected = {'profiles': {'instructions': 'x' * 2_500_000},
                        'routines': [{'prompt': 'y' * 2_000_000}]}
            response = json.dumps({'result': expected})
            with patch.object(model_catalog.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, response, '')):
                self.assertEqual(catalog._call('native', native_operation='snapshot_read'), expected)
                with self.assertRaises(ModelCatalogError):
                    catalog._call('native', native_operation='read')
            oversized = json.dumps({'routines': 'x' * 8_000_000})
            with patch.object(model_catalog.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, oversized, '')):
                with self.assertRaises(ModelCatalogError):
                    catalog._call('native', native_operation='snapshot_read')

    def check_large_native_text(self, operation, field):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'run_agent.py').touch()
            catalog = HermesModelCatalog(directory, source=source, python=sys.executable)
            received, files = [], []
            output = '结果\n' * 100_000

            def run(args, *, input, env, **kwargs):
                files.extend((catalog.private_home / 'tmp').iterdir())
                response = io.StringIO()
                with patch.object(model_catalog.sys, 'stdin', io.StringIO(input)), \
                     patch.object(model_catalog.sys, 'stdout', response), \
                     patch.dict(os.environ, {'TMPDIR': env['TMPDIR']}), \
                     patch.object(model_catalog, '_worker', side_effect=lambda request: received.append(request) or {'ok': True}):
                    model_catalog.main()
                return subprocess.CompletedProcess(args, 0, response.getvalue(), '')

            with patch.object(model_catalog.subprocess, 'run', side_effect=run):
                result = catalog._call('native', native_operation=operation, **{field: output})
            self.assertTrue(result['ok'])
            self.assertEqual(received[0][field], output)
            self.assertTrue(all(not path.exists() for path in files))

    def test_large_routine_output_reaches_the_native_worker_without_truncation(self):
        self.check_large_native_text('routine_finish', 'output')

    def test_large_bot_reply_reaches_the_native_worker_without_truncation(self):
        self.check_large_native_text('relay_reply', 'text')

    def test_oversized_settings_request_rejects_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'run_agent.py').touch()
            catalog = HermesModelCatalog(directory, source=source, python=sys.executable)
            with patch.object(model_catalog.subprocess, 'run') as run:
                with self.assertRaisesRegex(ModelCatalogError, 'too large'):
                    catalog._call('native', native_operation='update_profile', changes={'instructions': 'x' * 200_000})
                run.assert_not_called()

    def test_routine_output_file_is_private_and_removed_after_a_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'run_agent.py').touch()
            catalog = HermesModelCatalog(directory, source=source, python=sys.executable)
            files = []

            def run(*args, **kwargs):
                files.extend((catalog.private_home / 'tmp').iterdir())
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
                raise subprocess.TimeoutExpired('worker', 30)

            with patch.object(model_catalog.subprocess, 'run', side_effect=run):
                with self.assertRaises(ModelCatalogError):
                    catalog._call('native', native_operation='routine_finish', output='Private result.')
            self.assertFalse(files[0].exists())

    def test_worker_rejects_linked_and_unrelated_routine_output_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'outside').write_text('Private result.')
            spool = root / 'tmp'
            spool.mkdir()
            (spool / 'coconut-native-linked').symlink_to(root / 'outside')
            for name in ('../outside', 'outside', 'coconut-native-linked'):
                request = {'operation': 'native', 'native_operation': 'routine_finish', 'native_text_file': name}
                response = io.StringIO()
                with self.subTest(name=name), patch.dict(os.environ, {'TMPDIR': str(spool)}), \
                     patch.object(model_catalog.sys, 'stdin', io.StringIO(json.dumps(request))), \
                     patch.object(model_catalog.sys, 'stdout', response), patch.object(model_catalog, '_worker') as worker:
                    model_catalog.main()
                    worker.assert_not_called()
                    self.assertIn('error', json.loads(response.getvalue()))

    def test_default_inherits_current_native_selection(self):
        catalog = example_catalog()
        result = resolve_selection(None, catalog)
        self.assertEqual(result, catalog['default'])
        result['model'] = 'changed'
        self.assertEqual(catalog['default']['model'], 'first')

    def test_override_default_effort_does_not_inherit_another_model_effort(self):
        result = resolve_selection({'model': 'second', 'provider': 'provider-b'}, example_catalog())
        self.assertIsNone(result['reasoning_effort'])
        self.assertEqual(result['base_url'], 'https://two.example/v1')

    def test_unknown_route_effort_and_supplied_endpoint_reject(self):
        for selection in ({'model': 'second', 'provider': 'provider-a'},
                          {'model': 'second', 'provider': 'provider-b', 'reasoning_effort': 'extreme'},
                          {'model': 'second', 'provider': 'provider-b', 'base_url': 'https://evil.example'}):
            with self.subTest(selection=selection), self.assertRaises(ModelCatalogError):
                resolve_selection(selection, example_catalog())

    def test_endpoint_does_not_expose_inline_credentials(self):
        for value in ('https://key@one.example/v1', 'https://one.example/v1?key=secret',
                      'https://one.example/v1#secret', 'http://one.example/v1'):
            with self.subTest(value=value), self.assertRaises(ModelCatalogError):
                public_endpoint(value)
        self.assertEqual(public_endpoint('http://127.0.0.1:1234/v1/'), 'http://127.0.0.1:1234/v1')

    def test_active_profile_cannot_escape_or_follow_a_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'active_profile').write_text('../outside')
            with self.assertRaises(ModelCatalogError):
                native_home(root)
            (root / 'profiles').mkdir()
            (root / 'profiles/linked').symlink_to(root, target_is_directory=True)
            (root / 'active_profile').write_text('linked')
            with self.assertRaises(ModelCatalogError):
                native_home(root)

    def test_cache_refreshes_after_native_config_or_profile_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            catalog = HermesModelCatalog(Path(directory) / 'data', source=source)
            catalog.root.mkdir(parents=True)
            config = catalog.root / 'config.yaml'
            config.write_text('model: first\n')
            with patch.object(catalog, '_call', return_value=example_catalog()) as call, patch.object(catalog, '_start_refresh'):
                catalog.get()
                catalog.get()
                self.assertEqual(call.call_count, 1)
                config.write_text('model: changed\n')
                catalog.get()
                self.assertEqual(call.call_count, 2)
                selected = catalog.root / 'profiles/other'
                selected.mkdir(parents=True)
                (selected / 'config.yaml').write_text('model: third\n')
                (catalog.root / 'active_profile').write_text('other')
                catalog.get()
                self.assertEqual(call.call_count, 3)

    def test_a_live_refresh_starts_at_most_once_in_the_given_time(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            catalog = HermesModelCatalog(Path(directory) / 'data', source=source)
            catalog.root.mkdir(parents=True)
            (catalog.root / 'config.yaml').write_text('model: first\n')
            calls = []
            def call(operation, **payload):
                calls.append((operation, payload.get('refresh')))
                return example_catalog()
            def settled():
                deadline = time.monotonic() + 5
                while catalog._refreshing and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(catalog._refreshing)
            with patch.object(catalog, '_call', side_effect=call):
                catalog.refresh_if_old(300)
                settled()
                live = [c for c in calls if c == ('get', True)]
                self.assertEqual(len(live), 1)
                # Opened again at once: the cached lists come back, and no second live look starts.
                catalog.refresh_if_old(300)
                settled()
                self.assertEqual(len([c for c in calls if c == ('get', True)]), 1)
                # Old enough: it looks again.
                catalog._live_blocked_until = float('-inf')
                catalog.refresh_if_old(300)
                settled()
                self.assertEqual(len([c for c in calls if c == ('get', True)]), 2)

    def test_a_live_request_during_an_ordinary_refresh_runs_after_it(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            catalog = HermesModelCatalog(Path(directory) / 'data', source=source)
            catalog.root.mkdir(parents=True)
            (catalog.root / 'config.yaml').write_text('model: first\n')
            calls, release, started = [], threading.Event(), threading.Event()
            def call(operation, **payload):
                calls.append((operation, payload.get('refresh')))
                if operation == 'get' and payload.get('refresh') is False:
                    started.set()
                    release.wait(5)
                return example_catalog()
            with patch.object(catalog, '_call', side_effect=call):
                catalog.get()
                self.assertTrue(started.wait(5))
                # The ordinary refresh is running. The live request would be dropped.
                catalog.refresh_if_old(300)
                self.assertNotIn(('get', True), calls)
                release.set()
                deadline = time.monotonic() + 5
                while ('get', True) not in calls and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIn(('get', True), calls)
                while catalog._refreshing and time.monotonic() < deadline:
                    time.sleep(0.01)

    def test_a_failed_live_refresh_waits_a_short_time_and_still_sends_the_update_event(self):
        from coconut.model_catalog import LIVE_RETRY_SECONDS
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            catalog = HermesModelCatalog(Path(directory) / 'data', source=source)
            catalog.root.mkdir(parents=True)
            (catalog.root / 'config.yaml').write_text('model: first\n')
            calls, events, fail = [], [], [True]
            catalog.on_change = lambda: events.append(1)
            def call(operation, **payload):
                calls.append((operation, payload.get('refresh')))
                if payload.get('refresh') is True and fail[0]:
                    raise ModelCatalogError('The provider did not answer.')
                return example_catalog()
            def settled():
                deadline = time.monotonic() + 5
                while catalog._refreshing and time.monotonic() < deadline:
                    time.sleep(0.01)
            live = lambda: len([c for c in calls if c == ('get', True)])
            with patch.object(catalog, '_call', side_effect=call):
                catalog.refresh_if_old(300)
                settled()
                self.assertEqual(live(), 1)
                # The event goes out even after a failure, so the app stops showing "Loading models". The read that the
                # app makes on it must not start the next look, or the two would loop for as long as Hermes fails.
                self.assertEqual(events, [1])
                catalog.refresh_if_old(300)
                settled()
                self.assertEqual(live(), 1)
                # After the short retry time, the next look starts. It succeeds and sends the event once.
                self.assertLessEqual(catalog._live_blocked_until - time.monotonic(), LIVE_RETRY_SECONDS)
                catalog._live_blocked_until = float('-inf')
                fail[0] = False
                catalog.refresh_if_old(300)
                settled()
                self.assertEqual(live(), 2)
                self.assertEqual(events, [1, 1])

    def test_two_settings_reads_at_once_start_one_live_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            catalog = HermesModelCatalog(Path(directory) / 'data', source=source)
            catalog.root.mkdir(parents=True)
            (catalog.root / 'config.yaml').write_text('model: first\n')
            calls, release = [], threading.Event()
            def call(operation, **payload):
                calls.append((operation, payload.get('refresh')))
                if payload.get('refresh') is True:
                    release.wait(5)
                return example_catalog()
            with patch.object(catalog, '_call', side_effect=call):
                catalog.get()
                deadline = time.monotonic() + 5
                while catalog._refreshing and time.monotonic() < deadline:
                    time.sleep(0.01)
                barrier = threading.Barrier(8)
                def open_settings():
                    barrier.wait()
                    catalog.refresh_if_old(300)
                threads = [threading.Thread(target=open_settings) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                release.set()
                deadline = time.monotonic() + 5
                while catalog._refreshing and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertEqual(len([c for c in calls if c == ('get', True)]), 1)

    def test_the_ordinary_refresh_does_not_count_as_a_live_one(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            catalog = HermesModelCatalog(Path(directory) / 'data', source=source)
            catalog.root.mkdir(parents=True)
            (catalog.root / 'config.yaml').write_text('model: first\n')
            with patch.object(catalog, '_call', return_value=example_catalog()):
                catalog.get()
                deadline = time.monotonic() + 5
                while catalog._refreshing and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertEqual(catalog._live_blocked_until, float('-inf'))

    def test_legacy_migration_leaves_an_existing_native_profile_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = HermesModelCatalog(directory, source=Path(directory) / 'source')
            selected = catalog.root / 'profiles/work'
            selected.mkdir(parents=True)
            (catalog.root / 'active_profile').write_text('work')
            config = selected / 'config.yaml'
            original = b'model: native-user-model\n'
            config.write_bytes(original)
            with patch.object(catalog, '_call') as call:
                self.assertFalse(catalog.migrate_legacy_default('legacy-model'))
                call.assert_not_called()
            self.assertEqual(config.read_bytes(), original)

    def test_native_failure_text_never_enters_the_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'run_agent.py').touch()
            catalog = HermesModelCatalog(directory, source=source, python=sys.executable)
            completed = subprocess.CompletedProcess([], 1, 'private-token', 'private-token')
            with patch('coconut.model_catalog.subprocess.run', return_value=completed):
                with self.assertRaises(ModelCatalogError) as error:
                    catalog._call('runtime', selection={})
            self.assertNotIn('private-token', str(error.exception))

    def test_cached_reads_continue_while_native_discovery_waits(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = HermesModelCatalog(directory, source=Path(directory) / 'source')
            waiting, release, changed = threading.Event(), threading.Event(), threading.Event()
            def call(operation, **kwargs):
                if operation == 'get':
                    waiting.set()
                    release.wait(2)
                return example_catalog()
            catalog.on_change = changed.set
            with patch.object(catalog, '_call', side_effect=call):
                first = catalog.get()
                self.assertEqual(first['default']['model'], 'first')
                self.assertTrue(waiting.wait(1))
                second = catalog.get()
                self.assertTrue(second['loading'])
                release.set()
                for unused in range(100):
                    if not catalog._refreshing:
                        break
                    threading.Event().wait(0.01)
                self.assertFalse(catalog._refreshing)
                self.assertTrue(changed.wait(1))

    def test_missing_runtime_returns_a_displayable_error_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = HermesModelCatalog(directory, python='/missing/runtime', source=Path(directory) / 'source')
            result = catalog.get()
            self.assertFalse(result['available'])
            self.assertEqual(result['default']['model'], '')
            self.assertTrue(result['error'])

    def test_blocking_validation_does_not_start_background_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = HermesModelCatalog(directory, source=Path(directory) / 'source')
            defaults = example_catalog()
            defaults['models'] = []
            with patch.object(catalog, '_call', side_effect=[defaults, example_catalog()]) as call, patch.object(catalog, '_start_refresh') as start:
                result = catalog.get_for_validation()
                self.assertEqual(result['models'][0]['id'], 'second')
                self.assertEqual([row.args[0] for row in call.call_args_list], ['defaults', 'get'])
                start.assert_not_called()

    def test_profile_cache_tracks_native_default_and_each_bot_file(self):
        from uuid import uuid4
        with tempfile.TemporaryDirectory() as directory:
            catalog = HermesModelCatalog(directory, source=Path(directory) / 'source')
            catalog.root.mkdir(parents=True)
            config = catalog.root / 'config.yaml'
            config.write_text('model: first\n')
            bots = [str(uuid4()), str(uuid4())]
            for bot_id in bots:
                home = catalog._profile_home(bot_id)
                home.mkdir(parents=True)
                (home / 'config.yaml').write_text('agent: {reasoning_effort: low}\n')
            calls = []
            def call(operation, homes):
                calls.append(set(homes))
                return {key: {'model_override': None, 'effective_model': None} for key in homes}
            with patch.object(catalog, '_call', side_effect=call):
                catalog.read_profiles(bots)
                catalog.read_profiles(bots)
                (catalog._profile_home(bots[0]) / 'config.yaml').write_text('agent: {reasoning_effort: high}\n')
                catalog.read_profiles(bots)
                config.write_text('model: changed\n')
                catalog.read_profiles(bots)
            self.assertEqual(calls, [set(bots), {bots[0]}, set(bots)])

    def test_a_failed_profile_read_is_retried_after_a_short_wait(self):
        from uuid import uuid4
        with tempfile.TemporaryDirectory() as directory:
            catalog = HermesModelCatalog(directory, source=Path(directory) / 'source')
            catalog.root.mkdir(parents=True)
            (catalog.root / 'config.yaml').write_text('model: first\n')
            bot = str(uuid4())
            home = catalog._profile_home(bot)
            home.mkdir(parents=True)
            (home / 'config.yaml').write_text('{}\n')
            calls = []
            def call(operation, homes):
                calls.append(operation)
                if len(calls) == 1:
                    raise ModelCatalogError('Hermes model discovery timed out. Try refreshing the list.')
                return {key: {'model_override': None, 'effective_model': None} for key in homes}
            now = [1000.0]
            with patch.object(catalog, '_call', side_effect=call), \
                    patch.object(model_catalog.time, 'monotonic', side_effect=lambda: now[0]):
                self.assertIn('error', catalog.read_profile(bot))
                # The failure stays briefly, so a burst of reads does not start a process each time.
                self.assertIn('error', catalog.read_profile(bot))
                self.assertEqual(len(calls), 1)
                now[0] += 60
                self.assertEqual(catalog.read_profile(bot), {'model_override': None, 'effective_model': None})
                # A successful read stays until a file changes.
                now[0] += 3600
                catalog.read_profile(bot)
            self.assertEqual(calls, ['read_profiles', 'read_profiles'])

    def test_profile_reads_allow_a_slow_settings_process(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'run_agent.py').touch()
            catalog = HermesModelCatalog(directory, source=source, python=sys.executable)
            with patch.object(model_catalog.subprocess, 'run',
                              return_value=subprocess.CompletedProcess([], 0, json.dumps({'result': {}}), '')) as run:
                catalog._call('read_profiles', homes={})
            self.assertEqual(run.call_args.kwargs['timeout'], 30)


@unittest.skipUnless(MANAGED_PYTHON.is_file() and HERMES_SOURCE.is_dir(), 'The local Hermes runtime is absent.')
class NativeHermesCatalogTests(unittest.TestCase):
    """Use installed Hermes parsing, effort logic, and atomic config writes without HTTP."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / 'hermes'
        self.home.mkdir()
        self.addCleanup(self.temporary.cleanup)

    def run_native(self, code, request=None):
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.root),
               'HERMES_REAL_HOME': str(self.root), 'HERMES_HOME': str(self.home),
               'PYTHONPATH': str(HERMES_SOURCE), 'HERMES_DISABLE_TELEMETRY': '1',
               'XDG_CACHE_HOME': str(self.root / 'cache'), 'XDG_CONFIG_HOME': str(self.root / 'config')}
        script = """import contextlib, importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location('catalog', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
request = json.loads(sys.stdin.read())
with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
""" + ''.join('    ' + line + '\n' for line in code.splitlines()) + '\nprint(json.dumps(result))\n'
        process = subprocess.run([str(MANAGED_PYTHON), '-c', script, str(MODULE)],
                                 input=json.dumps(request or {}), text=True, capture_output=True,
                                 env=env, cwd=self.root, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr[-1500:])
        return json.loads(process.stdout)

    def test_native_yaml_alias_and_per_model_effort_resolution(self):
        (self.home / 'config.yaml').write_text('model:\n  name: gpt-6-astra\n  provider: openai-codex\nagent:\n  reasoning_effort: low\n  reasoning_overrides:\n    gpt-6-astra: xhigh\n')
        result = self.run_native("config, model, provider, url = m._native_state()\nresult = m._catalog(config, model, provider, url, [{'slug': provider, 'models': [model], 'source': 'hermes'}])")
        self.assertEqual(result['default']['model'], 'gpt-6-astra')
        self.assertEqual(result['default']['reasoning_effort'], 'xhigh')
        self.assertEqual(result['models'][0]['default_reasoning_effort'], 'xhigh')
        expected = self.run_native("from agent.reasoning_effort import codex_supported_efforts\nresult = list(codex_supported_efforts('gpt-6-astra'))")
        self.assertEqual(result['models'][0]['reasoning_efforts'], expected)

    def test_an_xai_subscription_lists_its_grok_models(self):
        # xai-oauth is an xAI subscription through Hermes device-code sign-in.
        (self.home / 'config.yaml').write_text('model:\n  default: gpt-6-astra\n  provider: openai-codex\n')
        result = self.run_native("config, model, provider, url = m._native_state()\nresult = m._catalog(config, model, provider, url, ["
                                 "{'slug': provider, 'models': [model], 'source': 'hermes'}, "
                                 "{'slug': 'xai-oauth', 'models': ['grok-4.7', 'grok-4.6'], 'source': 'hermes'}, "
                                 "{'slug': 'unsupported-provider', 'models': ['other'], 'source': 'hermes'}])")
        grok = [m for m in result['models'] if m['id'] == 'grok-4.7']
        self.assertEqual([m['provider'] for m in grok], ['xai-oauth'])
        self.assertEqual([m['base_url'] for m in grok], ['https://api.x.ai/v1'])
        self.assertNotIn('other', [m['id'] for m in result['models']])
        # Coconut offers exactly the efforts that Hermes sends on the xAI route.
        expected = self.run_native("from agent.reasoning_effort import XAI_GROK46_EFFORTS as a, XAI_LEGACY_EFFORTS as b\nresult = [list(a), list(b)]")
        efforts = {m['id']: m['reasoning_efforts'] for m in result['models'] if m['provider'] == 'xai-oauth'}
        self.assertEqual(efforts, {'grok-4.6': expected[0], 'grok-4.7': expected[1]})
        self.assertNotIn('none', efforts['grok-4.7'])

    def test_a_saved_grok_effort_shows_what_hermes_sends(self):
        # Hermes clamps an unsupported level to the nearest weaker one, and cannot disable Grok reasoning.
        (self.home / 'config.yaml').write_text('model:\n  default: gpt-6-astra\n  provider: openai-codex\n'
                                              'agent:\n  reasoning_overrides:\n    grok-4.6: max\n    grok-4.7: none\n    grok-4.5: medium\n')
        result = self.run_native("config, model, provider, url = m._native_state()\nresult = m._catalog(config, model, provider, url, ["
                                 "{'slug': provider, 'models': [model], 'source': 'hermes'}, "
                                 "{'slug': 'xai-oauth', 'models': ['grok-4.6', 'grok-4.7', 'grok-4.5'], 'source': 'hermes'}])")
        efforts = {m['id']: m['default_reasoning_effort'] for m in result['models'] if m['provider'] == 'xai-oauth'}
        # A disable stays 'none', so the model call still omits the reasoning field.
        self.assertEqual(efforts, {'grok-4.6': 'xhigh', 'grok-4.7': 'none', 'grok-4.5': 'medium'})

    def test_a_grok_default_shows_the_effort_that_hermes_sends(self):
        (self.home / 'config.yaml').write_text('model:\n  default: grok-4.7\n  provider: xai-oauth\nagent:\n  reasoning_effort: max\n')
        result = self.run_native("config, model, provider, url = m._native_state()\nresult = m._catalog(config, model, provider, url, ["
                                 "{'slug': provider, 'models': [model], 'source': 'hermes'}])")
        self.assertEqual(result['default']['reasoning_effort'], 'high')
        self.assertIn(result['default']['reasoning_effort'], result['models'][0]['reasoning_efforts'])

    def test_a_bot_grok_override_shows_the_effort_that_hermes_sends(self):
        (self.home / 'config.yaml').write_text('model:\n  default: grok-4.7\n  provider: xai-oauth\nagent:\n  reasoning_effort: max\n')
        inherited = {'model': 'gpt-6-astra', 'provider': 'openai-codex', 'base_url': None, 'reasoning_effort': None}
        result = self.run_native(f"result = m._read_profile(os.environ['HERMES_HOME'], {inherited!r})")
        self.assertEqual(result['model_override'], {'model': 'grok-4.7', 'provider': 'xai-oauth', 'reasoning_effort': 'high'})
        self.assertEqual(result['effective_model']['reasoning_effort'], 'high')

    def test_active_profile_reads_its_own_native_config(self):
        profile = self.home / 'profiles/work'
        profile.mkdir(parents=True)
        (self.home / 'active_profile').write_text('work')
        (self.home / 'config.yaml').write_text('model: {default: wrong, provider: openai-codex}\n')
        (profile / 'config.yaml').write_text('model: {default: selected, provider: openai-codex}\nagent: {reasoning_effort: false}\n')
        result = self.run_native("m._native_rows = lambda config, model, provider, url, refresh: [{'slug': provider, 'models': [model]}]\nresult = m._worker({'operation': 'get'})")
        self.assertEqual(result['default']['model'], 'selected')
        self.assertEqual(result['default']['reasoning_effort'], 'none')

    def test_atomic_native_write_preserves_unrelated_secrets_and_removes_stale_route(self):
        path = self.home / 'config.yaml'
        path.write_text('model:\n  default: first\n  provider: provider-a\n  api_key: stale-private\n  extra_headers: {Authorization: stale-private}\nagent:\n  reasoning_effort: high\n  reasoning_overrides:\n    second: high\n    unrelated: low\nproviders:\n  another: {api_key: keep-private}\ncustom_setting: keep\n')
        path.chmod(0o600)
        selection = {'model': 'second', 'provider': 'provider-b', 'reasoning_effort': 'low'}
        result = self.run_native("m._write_default(request['selection'], request['catalog'])\nfrom hermes_cli.config import require_readable_config_before_write\nraw = require_readable_config_before_write()\nresult = {'model': raw['model'], 'agent': raw['agent'], 'kept': raw['providers']['another']['api_key'] == 'keep-private', 'custom': raw['custom_setting']}\n", {'selection': selection, 'catalog': example_catalog()})
        self.assertTrue(result['kept'])
        self.assertEqual(result['custom'], 'keep')
        self.assertNotIn('api_key', result['model'])
        self.assertNotIn('extra_headers', result['model'])
        self.assertEqual(result['agent'], {'reasoning_effort': 'low', 'reasoning_overrides': {'unrelated': 'low'}})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_native_yaml_is_not_overwritten(self):
        path = self.home / 'config.yaml'
        before = b'model: [invalid\n'
        path.write_bytes(before)
        result = self.run_native("try:\n    m._write_default({'model': 'second', 'provider': 'provider-b'}, request)\n    result = False\nexcept Exception:\n    result = True", example_catalog())
        self.assertTrue(result)
        self.assertEqual(path.read_bytes(), before)

    def test_auto_clears_only_selected_native_effort_overrides(self):
        path = self.home / 'config.yaml'
        path.write_text('model: {default: first, provider: provider-a}\nagent:\n  reasoning_effort: high\n  reasoning_overrides: {second: high, unrelated: low}\n')
        result = self.run_native("m._write_default({'model': 'second', 'provider': 'provider-b'}, request)\nfrom hermes_cli.config import load_config\nfrom hermes_constants import resolve_reasoning_config\nconfig = load_config()\nresult = [resolve_reasoning_config(config, 'second'), resolve_reasoning_config(config, 'unrelated')]", example_catalog())
        self.assertEqual(result, [None, {'enabled': True, 'effort': 'low'}])

    def test_private_native_runtime_keeps_only_constructor_arguments(self):
        result = self.run_native("from unittest.mock import patch\nwith patch('hermes_cli.runtime_provider.resolve_runtime_provider', return_value={'provider': 'openai-codex', 'api_mode': 'codex_responses', 'api_key': 'private-test-token', 'base_url': 'https://chatgpt.com/backend-api/codex', 'credential_pool': object(), 'source': 'private-source'}):\n    private = m._native_runtime({'model': 'gpt-6-astra', 'provider': 'openai-codex'})\nresult = {'keys': sorted(private), 'private_token_preserved': private['api_key'] == 'private-test-token'}")
        self.assertTrue(result['private_token_preserved'])
        self.assertEqual(result['keys'], ['api_key', 'api_mode', 'base_url', 'provider'])

    def test_native_profile_selection_and_reset_preserve_unrelated_settings(self):
        profile = self.root / 'bot'
        profile.mkdir()
        path = profile / 'config.yaml'
        path.write_text('# Keep this comment\ntools: {browser: false}\nproviders:\n  private: {api_key: "${PRIVATE_KEY}"}\n')
        result = self.run_native("inherited = request['catalog']['default']\nbefore = m._read_profile(request['home'], inherited)\nm._write_profile(request['home'], {'model': 'second', 'provider': 'provider-b', 'reasoning_effort': 'low'}, request['catalog'])\nselected = m._read_profile(request['home'], inherited)\nm._write_profile(request['home'], None, request['catalog'])\nafter = m._read_profile(request['home'], inherited)\nresult = {'before': before, 'selected': selected, 'after': after}", {'home': str(profile), 'catalog': example_catalog()})
        self.assertIsNone(result['before']['model_override'])
        self.assertEqual(result['selected']['model_override'], {'model': 'second', 'provider': 'provider-b', 'reasoning_effort': 'low'})
        self.assertEqual(result['selected']['effective_model']['base_url'], 'https://two.example/v1')
        self.assertIsNone(result['after']['model_override'])
        self.assertIn('# Keep this comment', path.read_text())
        self.assertIn('${PRIVATE_KEY}', path.read_text())
        self.assertIn('browser: false', path.read_text())

    def test_native_profile_reads_aliases_and_actual_endpoint(self):
        profile = self.root / 'bot'
        profile.mkdir()
        (profile / 'config.yaml').write_text('model:\n  name: actual-native-model\n  provider: custom\n  api_base: http://localhost:9911/v1\nagent: {reasoning_effort: false}\n')
        result = self.run_native("result = m._read_profile(request['home'], request['catalog']['default'])", {'home': str(profile), 'catalog': example_catalog()})
        self.assertEqual(result['model_override']['model'], 'actual-native-model')
        self.assertEqual(result['model_override']['reasoning_effort'], 'none')
        self.assertEqual(result['effective_model']['base_url'], 'http://localhost:9911/v1')

    def test_native_profile_auth_falls_back_to_canonical_store_without_copy(self):
        profile = self.root / 'bot'
        profile.mkdir()
        canonical_auth = self.home / 'auth.json'
        canonical_auth.write_text(json.dumps({'providers': {'openai-codex': {'access_token': 'private-test'}}}))
        result = self.run_native("from unittest.mock import patch\nfrom hermes_cli import auth\nfrom hermes_cli.config import get_config_path\ndef resolve(**kwargs):\n    state, origin = auth._load_provider_state_with_source(auth._load_auth_store(), 'openai-codex')\n    return {'provider': 'openai-codex', 'api_mode': 'codex_responses', 'api_key': state['access_token'], 'base_url': 'https://chatgpt.com/backend-api/codex', 'source': str(origin), 'config_path': str(get_config_path())}\nwith patch('hermes_cli.runtime_provider.resolve_runtime_provider', side_effect=resolve):\n    private = m._native_runtime({'model': 'gpt-6-astra', 'provider': 'openai-codex'}, request['profile'], request['canonical'], request['canonical'])\nresult = {'resolved': private['api_key'] == 'private-test', 'bot_auth_exists': os.path.exists(request['profile'] + '/auth.json')}", {'profile': str(profile), 'canonical': str(self.home)})
        self.assertTrue(result['resolved'])
        self.assertFalse(result['bot_auth_exists'])

    def test_invalid_profile_does_not_hide_another_bot(self):
        (self.home / 'config.yaml').write_text('model: {default: gpt-6-astra, provider: openai-codex}\n')
        good, bad = self.root / 'good', self.root / 'bad'
        good.mkdir()
        bad.mkdir()
        (bad / 'config.yaml').write_text('model: [bad\n')
        result = self.run_native("result = m._worker(request)", {'operation': 'read_profiles', 'data_root': str(self.root), 'homes': {'good': str(good), 'bad': str(bad)}})
        self.assertIsNone(result['good']['model_override'])
        self.assertNotIn('error', result['good'])
        self.assertIn('error', result['bad'])

    def test_native_pkce_fallback_keeps_its_active_profile_source(self):
        profile = self.root / 'bot'
        active = self.home / 'profiles/active'
        profile.mkdir()
        active.mkdir(parents=True)
        (active / '.anthropic_oauth.json').write_text('{}')
        result = self.run_native("from unittest.mock import patch\nfrom agent import anthropic_credentials\ndef resolve(**kwargs):\n    return {'provider': 'anthropic', 'api_mode': 'anthropic_messages', 'api_key': 'ok' if str(anthropic_credentials._root_hermes_oauth_file()) == request['expected'] else 'wrong'}\nwith patch('hermes_cli.runtime_provider.resolve_runtime_provider', side_effect=resolve):\n    private = m._native_runtime({'model': 'native-model', 'provider': 'anthropic'}, request['profile'], request['active'], request['canonical'])\nresult = private['api_key'] == 'ok'", {'profile': str(profile), 'active': str(active), 'canonical': str(self.home), 'expected': str(active / '.anthropic_oauth.json')})
        self.assertTrue(result)

    def test_custom_route_stays_visible_as_default_but_cannot_be_selected(self):
        result = self.run_native("config = {'model': {'default': 'native-custom-model', 'provider': 'custom:private', 'base_url': 'https://private.example/v1'}}\nrows = [{'slug': 'custom:private', 'models': ['native-custom-model'], 'api_url': 'https://private.example/v1', 'is_user_defined': True}]\nresult = m._catalog(config, 'native-custom-model', 'custom:private', 'https://private.example/v1', rows)")
        self.assertEqual(result['default']['model'], 'native-custom-model')
        self.assertFalse(result['default']['supported'])
        self.assertTrue(result['default']['unavailable_reason'])
        self.assertEqual(result['models'], [])

    def test_required_custom_fields_and_callable_keys_reject_before_filtering(self):
        result = self.run_native("from unittest.mock import patch\nresult = []\nfor field in ['extra_headers', 'extra_body', 'request_overrides', 'capabilities', 'key_cmd', 'api_key']:\n    runtime = {'provider': 'openai-codex', 'api_mode': 'codex_responses', 'api_key': 'private-test-token'}\n    runtime[field] = (lambda: 'private-test-token') if field == 'api_key' else {'required': 'private-test-token'}\n    with patch('hermes_cli.runtime_provider.resolve_runtime_provider', return_value=runtime):\n        try:\n            m._native_runtime({'model': 'gpt-6-astra', 'provider': 'openai-codex'})\n            result.append(False)\n        except m.UnsupportedModelRoute as error:\n            result.append('private-test-token' not in str(error))")
        self.assertEqual(result, [True] * 6)

    def test_builtin_route_with_required_private_headers_is_not_offered(self):
        result = self.run_native("config = {'model': {'provider': 'anthropic', 'default': 'native-model', 'default_headers': {'Authorization': 'private-test-token'}}}\nresult = m._catalog(config, 'native-model', 'anthropic', None, [{'slug': 'anthropic', 'models': ['native-model']}])")
        self.assertFalse(result['default']['supported'])
        self.assertEqual(result['models'], [])
        self.assertNotIn('private-test-token', json.dumps(result))

    def test_unsupported_lan_route_does_not_hide_supported_models(self):
        result = self.run_native("rows = [{'slug': 'custom:lan', 'api_url': 'http://192.168.1.2:11434/v1', 'models': ['lan-model']}, {'slug': 'openai-codex', 'models': ['gpt-6-astra']}]\nresult = m._catalog({'model': {'default': 'gpt-6-astra', 'provider': 'openai-codex'}}, 'gpt-6-astra', 'openai-codex', None, rows)")
        self.assertEqual([row['id'] for row in result['models']], ['gpt-6-astra'])
        self.assertTrue(result['default']['supported'])

    def test_unsupported_lan_default_keeps_its_name_and_omits_unsafe_url(self):
        path = self.home / 'config.yaml'
        original = 'model: {default: lan-model, provider: custom, base_url: "http://192.168.1.2:11434/v1?key=private-test"}\n'
        path.write_text(original)
        result = self.run_native("config, model, provider, url = m._native_state()\nresult = m._catalog(config, model, provider, url, [{'slug': 'openai-codex', 'models': ['gpt-6-astra']}])")
        self.assertEqual(result['default']['model'], 'lan-model')
        self.assertEqual(result['default']['provider'], 'custom')
        self.assertIsNone(result['default']['base_url'])
        self.assertFalse(result['default']['supported'])
        self.assertEqual(result['models'][0]['id'], 'gpt-6-astra')
        self.assertNotIn('private-test', json.dumps(result))
        self.assertEqual(path.read_text(), original)


if __name__ == '__main__':
    unittest.main()
