import importlib.util
import os
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, patch

spec = importlib.util.spec_from_file_location('automatic_update', Path(__file__).with_name('automatic_update.py'))
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
REAL_GATE = update.check_hermes_candidate


def saved_result(result):
    """Return the saved gate result without its time."""
    return {key: value for key, value in result['state']['hermes_checks'].items() if key != 'checked_at'}


class AutomaticUpdateTests(unittest.TestCase):
    def setUp(self):
        # The Hermes candidate gate installs a runtime and runs model checks. Tests of other steps pass it.
        gate = patch.object(update, 'check_hermes_candidate')
        self.gate = gate.start()
        self.addCleanup(gate.stop)

    def test_only_successful_merged_commit_passes(self):
        pull = dict(merged_at='today', merge_commit_sha='abc', number=3, base=dict(ref='main'))
        record = dict(state='MERGED', mergeCommit=dict(oid='abc'), statusCheckRollup=[dict(conclusion='SUCCESS')])
        checked = dict(total_count=2, check_runs=[dict(name=name, head_sha='abc', app=dict(id=15368, slug='github-actions'), status='completed', conclusion='success') for name in ('contracts', 'native-kanban')])
        values = [json.dumps([pull]), json.dumps(record), json.dumps(checked), '{"statuses":[]}']
        with patch.object(update, 'run', side_effect=values) as calls:
            update.reviewed_merge('owner/repo', 'abc')
            self.assertIn('commits/abc/check-runs', calls.call_args_list[2].args[0][-1])
        for checks in ([], [dict(status='completed', conclusion='failure')], [dict(status='in_progress')]):
            checked = dict(total_count=len(checks), check_runs=checks)
            values = [json.dumps([pull]), json.dumps(record), json.dumps(checked), '{"statuses":[]}']
            with patch.object(update, 'run', side_effect=values):
                with self.assertRaises(ValueError):
                    update.reviewed_merge('owner/repo', 'abc')
        with patch.object(update, 'run', return_value='[]'):
            with self.assertRaises(ValueError):
                update.reviewed_merge('owner/repo', 'abc')

    def test_hash_ignores_backend_and_generated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            subprocess.run(['git', 'init', '-q', directory], check=True)
            (source / 'ios/Coconut').mkdir(parents=True)
            swift = source / 'ios/Coconut/App.swift'
            swift.write_text('first')
            subprocess.run(['git', 'add', '.'], cwd=source, check=True)
            before = update.ios_hash(source)
            (source / 'backend.py').write_text('backend change')
            (source / 'ios/Coconut/generated.tmp').write_text('generated')
            self.assertEqual(update.ios_hash(source), before)
            swift.write_text('second')
            self.assertNotEqual(update.ios_hash(source), before)

    def test_processing_build_does_not_upload_again(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            state = dict(pending_ios=dict(hash='hash', source_sha='sha', build=162))
            config = dict(release_helper='/release/release.py', release_source='/release/source')
            report = subprocess.CompletedProcess([], 1, json.dumps(dict(processing='PROCESSING', ready=False)), '')
            with patch.object(update.subprocess, 'run', return_value=report), patch.object(update, 'run') as command:
                update.release_ios(config, state, state_path)
                command.assert_not_called()
            self.assertIn('pending_ios', state)

    def test_ready_build_clears_pending_release(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            state = dict(pending_ios=dict(hash='hash', source_sha='sha', build=162))
            config = dict(release_helper='/release/release.py', release_source='/release/source')
            with patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')):
                update.release_ios(config, state, state_path)
            self.assertEqual(json.loads(state_path.read_text()), dict(ios_hash='hash', ios_build=162))

    def test_status_failure_does_not_trigger_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            state = dict(pending_ios=dict(hash='hash', source_sha='sha', build=162))
            config = dict(release_helper='/release/release.py', release_source='/release/source')
            report = subprocess.CompletedProcess([], 1, '', 'API unavailable')
            with patch.object(update.subprocess, 'run', return_value=report), patch.object(update, 'run') as command:
                with self.assertRaises(RuntimeError):
                    update.release_ios(config, state, Path(directory) / 'state.json')
                command.assert_not_called()

    def test_rejected_build_does_not_block_corrected_source(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            state = dict(pending_ios=dict(hash='bad-hash', source_sha='sha', build=162))
            config = dict(release_helper='/release/release.py', release_source='/release/source')
            report = subprocess.CompletedProcess([], 1, json.dumps(dict(processing='INVALID')), '')
            with patch.object(update.subprocess, 'run', return_value=report):
                with self.assertRaises(RuntimeError):
                    update.release_ios(config, state, state_path)
            self.assertNotIn('pending_ios', state)
            self.assertEqual(json.loads(state_path.read_text())['rejected_ios_hash'], 'bad-hash')

    def test_interrupted_runtime_and_unarchived_release_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, live = root / 'source', root / 'live'
            source.mkdir(); live.mkdir()
            (live / '.runtime/host').mkdir(parents=True)
            (live / '.runtime/host/connection.json').write_text(json.dumps(dict(address='https://localhost:8443')))
            (live / 'app.py').write_text('current')
            hashes = {'app.py': hashlib.sha256(b'current').hexdigest()}
            (live / '.coconut-install.json').write_text(json.dumps(dict(state='installed', files=hashes)))
            state_path = root / 'state.json'
            update.save(state_path, dict(pending_ios=dict(hash='old', source_sha='old')))
            received = []
            def command(args, **kwargs):
                if args[:3] == ['git', 'remote', 'get-url']: return 'https://github.com/owner/repo.git'
                if args[:2] == ['git', 'status']: return ''
                if args[:2] == ['git', 'rev-parse']: return 'corrected'
                if args[:2] == ['git', 'ls-files']: return 'app.py'
                if 'receive' in args:
                    received.append(args)
                    (live / '.runtime/hermes').mkdir()
                    (live / '.runtime/hermes/install.json').write_text(json.dumps(dict(commit='runtime')))
                    return 'COCONUT_DEPLOY_RESULT={"ok":true}'
                return ''
            def package(path):
                path.write_bytes(b'package')
                return dict(package_id='package')
            packaging = SimpleNamespace(package=package, source_files=lambda: {'app.py': b'current'})
            installer = SimpleNamespace(validate_package=lambda stream: SimpleNamespace(hashes=hashes, revision='runtime'),
                                        Operations=lambda: SimpleNamespace(health=lambda *args: None))
            config = dict(repository='owner/repo', source=str(source), live=str(live), release_helper='helper')
            with patch.object(update, 'run', side_effect=command), patch.object(update, 'module', side_effect=[packaging, installer]), \
                 patch.object(update, 'reviewed_merge'), patch.object(update, 'ios_hash', return_value='corrected'), \
                 patch.object(update, 'release_ios') as release, patch.object(update.Path, 'home', return_value=root):
                update.update(config, state_path)
            self.assertEqual(len(received), 1)
            self.assertIn('--update-runtime', received[0])
            # A runtime update runs the Hermes candidate gate before the live install. The order of source and live
            # matters: the gate installs into and cleans the first one.
            self.gate.assert_called_once_with(source, live, 'runtime', 'corrected', ANY, state_path)
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved['pending_ios']['source_sha'], 'corrected')
            release.assert_called_once()

    def test_a_source_only_deploy_does_not_run_the_hermes_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, live = root / 'source', root / 'live'
            source.mkdir(); live.mkdir()
            (live / '.runtime/host').mkdir(parents=True)
            (live / '.runtime/host/connection.json').write_text(json.dumps(dict(address='https://localhost:8443')))
            # The installed runtime already has the pin of the package.
            (live / '.runtime/hermes').mkdir()
            (live / '.runtime/hermes/install.json').write_text(json.dumps(dict(commit='runtime')))
            (live / 'app.py').write_text('current')
            hashes = {'app.py': hashlib.sha256(b'current').hexdigest()}
            (live / '.coconut-install.json').write_text(json.dumps(dict(state='installed', files=dict(hashes, old='x'))))
            state_path = root / 'state.json'
            update.save(state_path, {})
            received = []

            def command(args, **kwargs):
                if args[:3] == ['git', 'remote', 'get-url']: return 'https://github.com/owner/repo.git'
                if args[:2] == ['git', 'status']: return ''
                if args[:2] == ['git', 'rev-parse']: return 'next'
                if args[:2] == ['git', 'ls-files']: return 'app.py'
                if 'receive' in args:
                    received.append(args)
                    (live / '.coconut-install.json').write_text(json.dumps(dict(state='installed', files=hashes)))
                    return 'COCONUT_DEPLOY_RESULT={"ok":true}'
                return ''

            def package(path):
                path.write_bytes(b'package')
                return dict(package_id='package')
            packaging = SimpleNamespace(package=package, source_files=lambda: {'app.py': b'current'})
            installer = SimpleNamespace(validate_package=lambda stream: SimpleNamespace(hashes=hashes, revision='runtime'),
                                        Operations=lambda: SimpleNamespace(health=lambda *args: None))
            config = dict(repository='owner/repo', source=str(source), live=str(live))
            with patch.object(update, 'run', side_effect=command), patch.object(update, 'module', side_effect=[packaging, installer]), \
                    patch.object(update, 'reviewed_merge'), patch.object(update, 'ios_hash', return_value='ios'), \
                    patch.object(update.Path, 'home', return_value=root):
                update.update(config, state_path)
            self.assertEqual(len(received), 1)
            self.assertNotIn('--update-runtime', received[0])
            # The gate installs a runtime and runs model checks. A deploy that keeps the pin skips it.
            self.gate.assert_not_called()

    def test_a_failed_deploy_backs_off_for_the_same_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, live = root / 'source', root / 'live'
            source.mkdir(); live.mkdir()
            (live / '.runtime/host').mkdir(parents=True)
            (live / '.runtime/host/connection.json').write_text(json.dumps(dict(address='https://localhost:8443')))
            (live / 'app.py').write_text('current')
            hashes = {'app.py': hashlib.sha256(b'current').hexdigest()}
            (live / '.coconut-install.json').write_text(json.dumps(dict(state='installed', files=hashes)))
            state_path = root / 'state.json'
            update.save(state_path, {})
            head, received, outcome = ['first'], [], ['fail']

            def command(args, **kwargs):
                if args[:3] == ['git', 'remote', 'get-url']: return 'https://github.com/owner/repo.git'
                if args[:2] == ['git', 'status']: return ''
                if args[:2] == ['git', 'rev-parse']: return head[0]
                if args[:2] == ['git', 'ls-files']: return 'app.py'
                if 'receive' in args:
                    received.append(head[0])
                    if outcome[0] == 'fail':
                        raise RuntimeError('COCONUT_DEPLOY_RESULT={"ok": false, "error": "The service did not pass its local TLS health check."}')
                    (live / '.runtime/hermes').mkdir(exist_ok=True)
                    (live / '.runtime/hermes/install.json').write_text(json.dumps(dict(commit='runtime')))
                    return 'COCONUT_DEPLOY_RESULT={"ok":true}'
                return ''

            def package(path):
                path.write_bytes(b'package')
                return dict(package_id='package')

            def attempt(now):
                packaging = SimpleNamespace(package=package, source_files=lambda: {'app.py': b'current'})
                installer = SimpleNamespace(validate_package=lambda stream: SimpleNamespace(hashes=hashes, revision='runtime'),
                                            Operations=lambda: SimpleNamespace(health=lambda *args: None))
                config = dict(repository='owner/repo', source=str(source), live=str(live))
                with patch.object(update, 'run', side_effect=command), \
                        patch.object(update, 'module', side_effect=[packaging, installer]), \
                        patch.object(update, 'reviewed_merge'), patch.object(update, 'ios_hash', return_value='ios'), \
                        patch.object(update.time, 'time', return_value=now), patch.object(update.Path, 'home', return_value=root):
                    update.update(config, state_path)

            with self.assertRaises(RuntimeError):
                attempt(1000)
            failed = json.loads(state_path.read_text())['failed_deploy']
            self.assertEqual((failed['source_sha'], failed['attempts'], failed['retry_at']), ('first', 1, 1000 + update.FAILED_DEPLOY_SECONDS))
            # The same commit waits while launchd supervises the service, so the service does not restart again.
            with patch.object(update, 'service_supervised', return_value=True):
                attempt(1000 + update.FAILED_DEPLOY_SECONDS - 1)
            self.assertEqual(received, ['first'])
            # Without a loaded launch agent nothing restarts the service, so the installer runs at once to recover.
            with patch.object(update, 'service_supervised', return_value=False), self.assertRaises(RuntimeError):
                attempt(1000 + update.FAILED_DEPLOY_SECONDS - 1)
            self.assertEqual(received, ['first', 'first'])
            # A third failure doubles the wait again.
            failed = json.loads(state_path.read_text())['failed_deploy']
            self.assertEqual((failed['attempts'], failed['retry_at']), (2, 1000 + 3 * update.FAILED_DEPLOY_SECONDS - 1))
            retry_at = failed['retry_at']
            # A new merged commit deploys at once, and success clears the backoff.
            head[0], outcome[0] = 'second', 'ok'
            attempt(retry_at + 1)
            self.assertEqual(received, ['first', 'first', 'second'])
            saved = json.loads(state_path.read_text())
            self.assertNotIn('failed_deploy', saved)
            self.assertEqual(saved['source_sha'], 'second')

    def test_a_backoff_does_not_stall_a_pending_testflight_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, live = root / 'source', root / 'live'
            source.mkdir(); live.mkdir()
            state_path = root / 'state.json'
            update.save(state_path, dict(failed_deploy=dict(source_sha='bad', attempts=1, retry_at=2000),
                                         pending_ios=dict(hash='ios', source_sha='good')))

            def command(args, **kwargs):
                if args[:3] == ['git', 'remote', 'get-url']: return 'https://github.com/owner/repo.git'
                if args[:2] == ['git', 'status']: return ''
                if args[:2] == ['git', 'rev-parse']: return 'bad'
                return ''
            config = dict(repository='owner/repo', source=str(source), live=str(live), release_helper='helper')
            with patch.object(update, 'run', side_effect=command), patch.object(update, 'release_ios') as release, \
                    patch.object(update, 'service_supervised', return_value=True), \
                    patch.object(update.time, 'time', return_value=1000):
                update.update(config, state_path)
            release.assert_called_once()

    def test_the_supervision_check_reads_the_host_launch_agent(self):
        spec = importlib.util.spec_from_file_location('automatic_test_host', Path(__file__).with_name('host.py'))
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        self.assertEqual(update.SERVICE_LABEL, host.LABEL)
        with tempfile.TemporaryDirectory() as directory:
            live = Path(directory)
            (live / '.runtime/host').mkdir(parents=True)
            (live / '.runtime/host/connection.json').write_text(json.dumps(dict(address='https://localhost:1')))
            with patch.object(update.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as listing:
                self.assertTrue(update.service_supervised(live))
            self.assertEqual(listing.call_args.args[0], ['launchctl', 'print', f'gui/{os.getuid()}/{host.LABEL}'])
            # Without a loaded agent and without an answering port, nothing keeps the service running.
            with patch.object(update.subprocess, 'run', return_value=SimpleNamespace(returncode=113)), \
                    patch.object(update.socket, 'create_connection', side_effect=OSError):
                self.assertFalse(update.service_supervised(live))
            # A service in manual mode answers on its port without the agent.
            with patch.object(update.subprocess, 'run', return_value=SimpleNamespace(returncode=113)), \
                    patch.object(update.socket, 'create_connection', return_value=contextlib.nullcontext()):
                self.assertTrue(update.service_supervised(live))

    def gate_case(self, *, checks_pass, cached=None, install=None, installed='old', result_text=None, checks=None,
                  free=update.HERMES_CANDIDATE_FREE_BYTES):
        """Run check_hermes_candidate for a new pin. Return the run commands and the saved state."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, live = root / 'source', root / 'live'
            (source / '.runtime/hermes').mkdir(parents=True)
            (source / '.runtime/hermes/install.json').write_text(json.dumps(dict(commit=installed)))
            # Other project scripts keep private data in .runtime, for example remote.py its deploy key.
            (source / '.runtime/ssh').mkdir()
            (source / '.runtime/ssh/key').write_text('private')
            live.mkdir()
            state_path = root / 'state.json'
            state = dict(hermes_checks=cached) if cached else {}
            commands, calls = [], []

            def record(args, **kwargs):
                commands.append(args)
                return ''

            def script(command, *, cwd, timeout):
                # scripts/check_hermes_candidate.py writes its result to the --output path.
                output = Path(command[command.index('--output') + 1])
                output.write_text(result_text or json.dumps(dict(passed=checks_pass, hermes_sha='new', credentials=['KEY'],
                                                                 checks=dict(relay=dict(passed=checks_pass)))))
                return (0 if checks_pass else 1), 'check log'

            def bounded(command, *, cwd, timeout):
                if 'install' in command:
                    try:
                        (install or record)(command, cwd=cwd)
                    except RuntimeError as error:
                        return 1, str(error)
                    return 0, ''
                calls.append(command)
                return (checks or script)(command, cwd=cwd, timeout=timeout)
            # setUp patches the gate for other tests. These tests call the real function.
            stopped = []
            leftovers = SimpleNamespace(stop_leftovers=lambda folder: stopped.append(folder))
            with patch.object(update, 'run', side_effect=AssertionError('The gate runs its commands through run_bounded.')), \
                    patch.object(update, 'run_bounded', side_effect=bounded), \
                    patch.object(update, 'module', return_value=leftovers), \
                    patch.object(update.shutil, 'disk_usage', return_value=SimpleNamespace(free=free)):
                try:
                    REAL_GATE(source, live, 'new', 'sha2', state, state_path)
                    raised = None
                except (RuntimeError, OSError) as error:
                    raised = error
            receipt = json.loads((root / 'hermes-checks.json').read_text()) if (root / 'hermes-checks.json').exists() else None
            return dict(commands=commands, script_calls=len(calls), calls=calls, state=state, raised=raised, receipt=receipt,
                        candidate_left=(source / '.runtime/hermes').exists(),
                        runtime_left=any((source / '.runtime' / name).exists() for name in update.HERMES_GATE_FOLDERS),
                        other_left=(source / '.runtime/ssh/key').exists(), stopped=stopped, runtime=source / '.runtime/hermes')

    def test_a_new_hermes_pin_is_checked_before_the_live_install(self):
        # The folder already has the new pin, so only the cleanup after the checks can remove it.
        result = self.gate_case(checks_pass=True, installed='new')
        self.assertIsNone(result['raised'])
        self.assertEqual(result['commands'][0][-3:], ['install', '--revision', 'new'])
        self.assertEqual(result['script_calls'], 1)
        self.assertEqual(saved_result(result), dict(revision='new', sha='sha2', code='sha2', passed=True))
        self.assertTrue(result['receipt']['passed'])
        # The updater's copy of the runtime is removed after the checks, to keep disk space free.
        self.assertFalse(result['candidate_left'])

    def test_a_failed_hermes_check_stops_the_install(self):
        result = self.gate_case(checks_pass=False)
        self.assertIn('native upgrade checks failed', str(result['raised']))
        self.assertEqual(saved_result(result), dict(revision='new', sha='sha2', code='sha2', passed=False))
        self.assertFalse(result['receipt']['passed'])

    def test_a_failed_candidate_install_says_so_in_the_receipt(self):
        def install(args, **kwargs):
            raise RuntimeError('fatal: unable to access github.com')
        result = self.gate_case(checks_pass=True, install=install)
        self.assertIn('install failed before any check ran', str(result['raised']))
        self.assertIn('unable to access github.com', result['receipt']['output'])
        self.assertEqual(result['script_calls'], 0)

    def test_a_pending_journal_is_recovered_before_the_candidate_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            source, live = home / 'source', home / 'Coconut'
            (live / '.runtime/deploy').mkdir(parents=True)
            with patch.object(update.Path, 'home', return_value=home), patch.object(update, 'run', return_value='') as run:
                update.recover_journal(source, live)
                run.assert_not_called()
                (live / '.runtime/deploy/update.json').write_text('{}')
                update.recover_journal(source, live)
            # The recover command replays the journal and installs no package, so the refused pin cannot arrive.
            self.assertEqual(run.call_args.args[0][1:], [str(source / 'scripts/deploy.py'), 'recover', '--directory', 'Coconut'])

    def test_a_timed_out_check_fails_and_names_the_timeout(self):
        # The timeout stops the check script before it writes its result, so the gate writes one.
        result = self.gate_case(checks_pass=True, installed='new', checks=lambda command, **kwargs: (None, 'partial log'))
        self.assertIn('did not finish', str(result['raised']))
        self.assertIn('partial log', result['receipt']['output'])
        self.assertIn('ran longer than', result['receipt']['output'])
        self.assertFalse(result['receipt']['passed'])
        # A timeout is inconclusive, so it does not keep the pin as failed for a day.
        self.assertNotIn('hermes_checks', result['state'])
        # The leftover check programs stop before the gate removes the candidate runtime.
        self.assertEqual(result['stopped'], [result['runtime']])

    def test_a_setup_failure_does_not_keep_the_pin_as_failed(self):
        for result_text in (json.dumps(dict(passed=False, hermes_sha='new', credentials=[], checks=dict(relay=dict(passed=False)))),
                            json.dumps(dict(passed=False, hermes_sha='new', credentials=['KEY'], error='The source changed.'))):
            result = self.gate_case(checks_pass=False, result_text=result_text)
            self.assertIn('did not finish', str(result['raised']))
            self.assertNotIn('hermes_checks', result['state'])

    def test_the_check_script_gets_the_candidate_runtime_and_the_live_credentials(self):
        result = self.gate_case(checks_pass=True)
        command = result['calls'][0]
        self.assertTrue(command[1].endswith('scripts/check_hermes_candidate.py'))
        self.assertTrue(command[command.index('--runtime') + 1].endswith('source/.runtime/hermes'))
        self.assertTrue(command[command.index('--host-data') + 1].endswith('live/.runtime/host'))

    def test_a_result_for_another_hermes_commit_is_inconclusive(self):
        result = self.gate_case(checks_pass=True, result_text=json.dumps(dict(passed=True, hermes_sha='other', credentials=['KEY'],
                                                                              checks=dict(relay=dict(passed=True)))))
        self.assertIn('did not finish', str(result['raised']))
        self.assertNotIn('hermes_checks', result['state'])

    def test_a_full_disk_stops_the_candidate_install(self):
        result = self.gate_case(checks_pass=True, free=update.HERMES_CANDIDATE_FREE_BYTES - 1)
        self.assertIn('GiB of free disk space', str(result['raised']))
        self.assertIn('Nothing was installed', result['receipt']['output'])
        self.assertEqual((result['commands'], result['script_calls']), ([], 0))
        # The pass is not saved as a failed check, so the next pass with enough space runs the checks.
        self.assertNotIn('hermes_checks', result['state'])

    def test_a_failed_pin_is_not_checked_again_for_a_day_with_the_same_code(self):
        # gate_case has no Git checkout, so the commit stands for the code.
        failed = dict(revision='new', sha='sha2', code='sha2', passed=False, checked_at=time.time() - 60)
        result = self.gate_case(checks_pass=True, cached=failed)
        self.assertIn('failed for Hermes new', str(result['raised']))
        self.assertEqual((result['commands'], result['script_calls']), ([], 0))
        # A day later, a new pin, or a change of the checked code runs the checks again.
        for cached in (dict(failed, checked_at=time.time() - update.HERMES_FAILED_RETRY_SECONDS - 1),
                       dict(failed, revision='other'), dict(failed, code='other-tree')):
            self.assertEqual(self.gate_case(checks_pass=True, cached=cached)['script_calls'], 1)

    def test_a_saved_result_is_reused_by_the_next_pass(self):
        # Run the gate twice with the state that the first run saved. The second run must not run the checks again.
        first = self.gate_case(checks_pass=False)
        self.assertEqual(first['script_calls'], 1)
        second = self.gate_case(checks_pass=False, cached=first['state']['hermes_checks'])
        self.assertIn('failed for Hermes new', str(second['raised']))
        self.assertEqual(second['script_calls'], 0)

    def test_the_checked_code_is_the_tree_of_backend_and_scripts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            subprocess.run(['git', 'init', '-q'], cwd=source, check=True)
            for name in ('backend/a.py', 'scripts/b.py', 'ios/c.swift'):
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                (source / name).write_text(name)
            commit = ['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qam', 'x']
            subprocess.run(['git', 'add', '.'], cwd=source, check=True)
            subprocess.run(commit, cwd=source, check=True)
            first = update.checked_code(source, 'sha')
            (source / 'ios/c.swift').write_text('changed')
            subprocess.run(commit, cwd=source, check=True)
            self.assertEqual(update.checked_code(source, 'sha'), first)
            (source / 'backend/a.py').write_text('changed')
            subprocess.run(commit, cwd=source, check=True)
            self.assertNotEqual(update.checked_code(source, 'sha'), first)

    def test_the_gate_removes_its_runtime_folder_and_caches(self):
        def install(args, **kwargs):
            source = Path(kwargs['cwd'])
            # hermes.py puts its uv cache and its temporary files beside the runtime.
            for name in ('.runtime/cache/uv', '.runtime/tmp'):
                (source / name).mkdir(parents=True, exist_ok=True)
            return ''
        result = self.gate_case(checks_pass=True, install=install)
        self.assertIsNone(result['raised'])
        self.assertFalse(result['runtime_left'])
        self.assertTrue(result['other_left'])

    def test_a_stopped_run_frees_its_folder_before_the_space_check(self):
        # gate_case leaves a candidate folder, as a run that was stopped part way does.
        result = self.gate_case(checks_pass=True, free=update.HERMES_CANDIDATE_FREE_BYTES - 1)
        self.assertIn('GiB of free disk space', str(result['raised']))
        self.assertFalse(result['runtime_left'])

    def test_an_unexpected_gate_error_is_named_in_the_receipt(self):
        def broken(*args, **kwargs):
            raise OSError('The check script cannot run.')
        result = self.gate_case(checks_pass=True, checks=broken)
        self.assertIsInstance(result['raised'], OSError)
        self.assertIn('OSError: The check script cannot run.', result['receipt']['output'])
        self.assertFalse(result['receipt']['passed'])

    def test_a_passed_hermes_pin_is_not_checked_again(self):
        result = self.gate_case(checks_pass=True, cached=dict(revision='new', sha='sha2', code='sha2', passed=True))
        self.assertIsNone(result['raised'])
        self.assertEqual((result['commands'], result['script_calls']), ([], 0))

    def test_a_new_source_commit_checks_the_same_pin_again(self):
        # The checks run the Coconut code of the checkout too, so a pass for another commit does not count.
        result = self.gate_case(checks_pass=True, cached=dict(revision='new', sha='sha1', code='sha1', passed=True))
        self.assertEqual(result['script_calls'], 1)
        self.assertEqual(saved_result(result), dict(revision='new', sha='sha2', code='sha2', passed=True))

    def test_a_malformed_check_report_fails_the_gate(self):
        result = self.gate_case(checks_pass=True, installed='new', result_text='{"passed": tr')
        self.assertIn('did not finish', str(result['raised']))
        self.assertNotIn('hermes_checks', result['state'])
        # A script that exits before it writes a result is a setup problem too.
        result = self.gate_case(checks_pass=True, checks=lambda command, **kwargs: (1, 'Traceback: import failed'))
        self.assertIn('did not finish', str(result['raised']))
        self.assertIn('import failed', result['receipt']['output'])
        self.assertFalse(result['receipt']['passed'])

    def test_a_failed_health_check_after_install_backs_off(self):
        class DeployError(Exception):
            pass

        def refuse(*args):
            raise DeployError('The service did not pass its local TLS health check.')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, live = root / 'source', root / 'live'
            source.mkdir(); live.mkdir()
            (live / '.runtime/host').mkdir(parents=True)
            (live / '.runtime/host/connection.json').write_text(json.dumps(dict(address='https://localhost:8443')))
            (live / 'app.py').write_text('current')
            hashes = {'app.py': hashlib.sha256(b'current').hexdigest()}
            (live / '.coconut-install.json').write_text(json.dumps(dict(state='installed', files=hashes)))
            (live / '.runtime/hermes').mkdir()
            (live / '.runtime/hermes/install.json').write_text(json.dumps(dict(commit='runtime')))
            state_path = root / 'state.json'
            update.save(state_path, {})

            def command(args, **kwargs):
                if args[:3] == ['git', 'remote', 'get-url']: return 'https://github.com/owner/repo.git'
                if args[:2] == ['git', 'status']: return ''
                if args[:2] == ['git', 'rev-parse']: return 'installed'
                if args[:2] == ['git', 'ls-files']: return 'app.py'
                return ''

            def package(path):
                path.write_bytes(b'package')
                return dict(package_id='package')
            packaging = SimpleNamespace(package=package, source_files=lambda: {'app.py': b'current'})
            installer = SimpleNamespace(validate_package=lambda stream: SimpleNamespace(hashes=hashes, revision='runtime'),
                                        Operations=lambda: SimpleNamespace(health=refuse), DeployError=DeployError)
            config = dict(repository='owner/repo', source=str(source), live=str(live))
            with patch.object(update, 'run', side_effect=command), \
                    patch.object(update, 'module', side_effect=[packaging, installer]), \
                    patch.object(update, 'reviewed_merge'), patch.object(update, 'ios_hash', return_value='ios'), \
                    patch.object(update.time, 'time', return_value=1000), patch.object(update.Path, 'home', return_value=root):
                with self.assertRaises(DeployError):
                    update.update(config, state_path)
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved['failed_deploy']['source_sha'], 'installed')
            self.assertNotIn('source_sha', saved)

    def test_completed_archive_receipt_survives_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            receipt = Path(directory) / 'archive.json'
            receipt.write_text(json.dumps(dict(source=str(source), source_hash='hash', build=162, version='2026.9.19')))
            state = dict(pending_ios=dict(hash='hash', source_sha='sha', receipt=str(receipt)))
            config = dict(release_helper=str(Path(directory) / 'release.py'), release_source=str(source))
            with patch.object(update, 'run', side_effect=lambda args, **kw: 'sha' if args[:2] == ['git', 'rev-parse'] else '') as command, \
                 patch.object(update, 'ios_hash', return_value='hash'), \
                 patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')):
                update.release_ios(config, state, Path(directory) / 'state.json')
            self.assertFalse(any('archive' in call.args[0] for call in command.call_args_list))
            self.assertEqual(state['ios_build'], 162)




class BoundedRunTests(unittest.TestCase):
    def test_a_timeout_stops_the_process_group_and_returns_the_output(self):
        started = time.monotonic()
        program = 'import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]); print("started", flush=True); time.sleep(30)'
        code, output = update.run_bounded([sys.executable, '-c', program], cwd=None, timeout=1)
        self.assertIsNone(code)
        self.assertIn('started', output)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_process_in_its_own_session_cannot_block_the_updater(self):
        # A Hermes worker starts its own session and can keep the output open. The output is a file, not a pipe.
        program = ('import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], '
                   'start_new_session=True); print("done", flush=True)')
        started = time.monotonic()
        code, output = update.run_bounded([sys.executable, '-c', program], cwd=None, timeout=20)
        self.assertEqual((code, output.strip()), (0, 'done'))
        self.assertLess(time.monotonic() - started, 10)


class PostmergeRegressionTests(unittest.TestCase):
    """Real updater entry points; external operations use only temporary fixtures."""

    def setUp(self):
        # The Hermes candidate gate installs a runtime and runs model checks. Tests of other steps pass it.
        gate = patch.object(update, 'check_hermes_candidate')
        self.gate = gate.start()
        self.addCleanup(gate.stop)

    def ci(self, checks, *, total=None, statuses=None, merge_sha='abc'):
        values = [
            [dict(merged_at='today', merge_commit_sha='abc', number=3, base=dict(ref='main'))],
            dict(state='MERGED', mergeCommit=dict(oid=merge_sha), statusCheckRollup=[dict(conclusion='SUCCESS')]),
            dict(total_count=len(checks) if total is None else total, check_runs=checks),
            statuses or dict(statuses=[]),
        ]
        with patch.object(update, 'run', side_effect=[json.dumps(value) for value in values]):
            update.reviewed_merge('owner/repo', 'abc')

    def checks(self):
        return [dict(name=name, head_sha='abc', app=dict(id=15368, slug='github-actions'),
                     status='completed', conclusion='success') for name in ('contracts', 'native-kanban')]

    def test_expected_trusted_ci_passes(self):
        self.ci(self.checks())

    def test_unrelated_success_is_not_expected_ci(self):
        checks = self.checks()[:1]
        checks[0]['name'] = 'unrelated'
        with self.assertRaises(ValueError):
            self.ci(checks)

    def test_ci_provenance_completeness_and_failures(self):
        cases = [[], self.checks()[:1], [None], ['invalid']]
        for field, value in [('head_sha', 'different'), ('name', []), ('app', None), ('app', {}),
                             ('app', dict(id=999, slug='github-actions')),
                             ('app', dict(id=15368, slug='untrusted')),
                             ('conclusion', 'neutral'), ('conclusion', 'skipped'),
                             ('conclusion', 'failure'), ('status', 'in_progress')]:
            checks = self.checks()
            checks[0][field] = value
            cases.append(checks)
        for conclusion in ('failure', None):
            checks = self.checks()
            checks.append(dict(name='extra', head_sha='abc', status='completed', conclusion=conclusion))
            cases.append(checks)
        for checks in cases:
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                self.ci(checks)
        for options in [dict(total=101), dict(merge_sha='other'),
                        dict(statuses=dict(state='pending', statuses=[dict(state='pending')])),
                        dict(statuses=dict(state='failure', statuses=[dict(state='failure')])),
                        dict(statuses=dict(state='success', total_count=101, statuses=[dict(state='success')]))]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.ci(self.checks(), **options)

    def runtime_case(self, *, damaged=True, repaired=True, runtime_text='{"commit":', post_runtime=None):
        import sys
        tests = str(Path(__file__).resolve().parents[1] / 'backend/tests')
        with patch.object(sys, 'path', [tests] + sys.path):
            from test_runtime_deployment import deploy, archive, package_files, RuntimeOperations, NEW
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            live, source = root / 'live', root / 'source'
            source.mkdir()
            address = 'https://mini.example:8960'
            files = package_files(**{'scripts/hermes.py': f"REVISION = '{NEW}'\n".encode()})
            deploy.receive('live', address, archive(package_files() if damaged else files),
                           home=root, operations=RuntimeOperations(mode='managed'))
            if damaged:
                with self.assertRaises(deploy.DeployError):
                    deploy.receive('live', address, archive(files), home=root,
                                   operations=RuntimeOperations(mode='managed', fail='prepare', fail_stop_after=1),
                                   update_runtime=True)
                (live / '.runtime/hermes/install.json').write_text(runtime_text)
                self.assertTrue((live / '.runtime/deploy/update.json').exists())
            (live / '.runtime/host').mkdir(parents=True, exist_ok=True)
            (live / '.runtime/host/connection.json').write_text(json.dumps(dict(address=address)))
            state_path = root / 'state.json'
            received = []
            def command(args, **kwargs):
                if args[:3] == ['git', 'remote', 'get-url']: return 'https://github.com/owner/repo.git'
                if args[:2] == ['git', 'rev-parse']: return 'sha'
                if args[:2] == ['git', 'ls-files']: return '\n'.join(files)
                if 'receive' in args:
                    received.append(args)
                    if repaired:
                        result = deploy.receive('live', address, kwargs['stdin'], home=root,
                                                operations=RuntimeOperations(mode='managed'),
                                                update_runtime='--update-runtime' in args)
                        return 'COCONUT_DEPLOY_RESULT=' + json.dumps(dict(result, ok=True))
                    if post_runtime is not None:
                        (live / '.runtime/hermes/install.json').write_text(post_runtime)
                    return 'COCONUT_DEPLOY_RESULT={"ok":true}'
                return ''
            def package(path):
                path.write_bytes(archive(files).getvalue())
                return dict(package_id='package')
            packaging = SimpleNamespace(package=package, source_files=lambda: files)
            installer = SimpleNamespace(validate_package=deploy.validate_package,
                                        Operations=lambda: RuntimeOperations(mode='managed'))
            config = dict(repository='owner/repo', source=str(source), live=str(live))
            with patch.object(update, 'run', side_effect=command), \
                 patch.object(update, 'module', side_effect=lambda source, name: packaging if name == 'remote' else installer), \
                 patch.object(update, 'reviewed_merge'), patch.object(update, 'ios_hash', return_value='hash'), \
                 patch.object(update.Path, 'home', return_value=root):
                if not repaired:
                    with self.assertRaises((ValueError, RuntimeError)):
                        update.update(config, state_path)
                    # A failed check records only its backoff, never an installed commit.
                    saved = json.loads(state_path.read_text()) if state_path.exists() else {}
                    self.assertNotIn('source_sha', saved)
                    self.assertEqual(saved.get('failed_deploy', {}).get('source_sha'), 'sha')
                    return
                update.update(config, state_path)
                update.update(config, state_path)
            self.assertEqual(len(received), int(damaged))
            self.assertEqual(json.loads(state_path.read_text())['source_sha'], 'sha')
            self.assertEqual(json.loads((live / '.runtime/hermes/install.json').read_text())['commit'], NEW)
            self.assertFalse((live / '.runtime/deploy/update.json').exists())

    def test_truncated_runtime_reaches_real_receive_and_retry_is_noop(self):
        self.runtime_case()

    def test_wrong_type_runtime_reaches_recovery(self):
        for value in ('null', '[]', 'true', '42'):
            with self.subTest(receipt=value):
                self.runtime_case(runtime_text=value)

    def test_healthy_runtime_does_not_deploy(self):
        self.runtime_case(damaged=False)

    def test_unrepaired_runtime_cannot_record_success(self):
        for value in ('{', 'null', '[]', '{}', '{"commit":"wrong"}'):
            with self.subTest(receipt=value):
                self.runtime_case(repaired=False, post_runtime=value)

    def archive_case(self, receipt_text='{"source":', *, helper_change=None, helper_text=None,
                     status=None, expected_error=False, head='sha', recovered=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            helper = root / 'helper/release.py'
            helper.parent.mkdir()
            receipt = root / 'receipt.json'
            if receipt_text is not None:
                receipt.write_text(receipt_text)
            record = dict(source=str(source), source_hash='hash', build='162', version='2026.9.19')
            snapshot_record = record.copy()
            record.update(helper_change or {})
            authoritative = helper.parent / 'archive.json'
            authoritative.write_text(json.dumps(record) if helper_text is None else helper_text)
            before = authoritative.read_bytes()
            pending = dict(hash='hash', source_sha='sha', receipt=str(receipt),
                           archive_attempt=dict(helper=str(helper.resolve()), previous=None))
            if recovered:
                snapshot = root / 'recovered.json'
                snapshot.write_text(json.dumps(snapshot_record))
                pending['recovered_receipt'] = str(snapshot)
            state = dict(pending_ios=pending)
            state_path = root / 'state.json'
            update.save(state_path, state)
            events = []
            def command(args, **kwargs):
                events.append(args)
                if args[:2] == ['git', 'rev-parse']: return head
                if args[0] != 'git':
                    self.assertIn('upload', args)
                    self.assertIn('status', events[-2])
                return ''
            def check_status(args, **kwargs):
                events.append(args)
                return status or subprocess.CompletedProcess([], 0, '{}', '')
            config = dict(release_helper=str(helper), release_source=str(source))
            with patch.object(update, 'run', side_effect=command), patch.object(update, 'ios_hash', return_value='hash'), \
                 patch.object(update.subprocess, 'run', side_effect=check_status):
                if expected_error:
                    for _ in range(2):
                        with self.assertRaisesRegex(ValueError, 'recover archive receipt|source commit'):
                            update.release_ios(config, state, state_path)
                    self.assertFalse(any(args[0] != 'git' and ('status' in args or 'upload' in args) for args in events))
                    self.assertNotIn('build', state['pending_ios'])
                else:
                    update.release_ios(config, state, state_path)
                    self.assertEqual(pending['build'], 162)
                    self.assertEqual(pending['version'], '2026.9.19')
                    if receipt_text != json.dumps(record):
                        self.assertEqual(json.loads(Path(pending['recovered_receipt']).read_text()), snapshot_record if recovered else record)
                    if status and status.returncode:
                        update.release_ios(config, state, state_path)
            self.assertEqual(authoritative.read_bytes(), before)
            if receipt_text is not None:
                self.assertEqual(receipt.read_text(), receipt_text)
            else:
                self.assertFalse(receipt.exists())
            self.assertFalse(any('archive' in args for args in events))
            return events

    def test_partial_archive_recovers_authoritative_receipt_without_rebuild(self):
        events = self.archive_case()
        self.assertFalse(any('upload' in args for args in events))

    def test_saved_recovery_snapshot_keeps_original_build_identity(self):
        self.archive_case(recovered=True, helper_change=dict(build='163'))

    def test_archive_recovery_handles_empty_missing_and_incomplete_receipts(self):
        for receipt in ('', None, '{}', '{"build":"162"}',
                        '{"build":"162","version":', '{"source_hash":"hash","build":'):
            with self.subTest(receipt=receipt):
                self.archive_case(receipt)

    def test_archive_recovery_rejects_invalid_or_conflicting_records(self):
        for text in ('', '{', '[]', 'null', '162', '"record"'):
            with self.subTest(helper=text):
                self.archive_case(helper_text=text, expected_error=True)
        for change in (dict(source='/other'), dict(source_hash='other'), dict(build=True), dict(build=0),
                       dict(build=1.5), dict(build=[]), dict(version=None), dict(version='bad')):
            with self.subTest(change=change):
                self.archive_case(helper_change=change, expected_error=True)
        for text in ('[]', 'null', 'garbage', '{"build":"163"}', '{"version":"1.2.3"}',
                     '{"source_hash":"other"}', '{"build":"163","version":',
                     '{"source_hash":"other","build":', '{"version":"1.2.3",',
                     '{"source":"/other","source_hash":'):
            with self.subTest(receipt=text):
                self.archive_case(text, expected_error=True)
        self.archive_case(head='different', expected_error=True)

    def test_recovered_processing_build_never_uploads(self):
        events = self.archive_case(status=subprocess.CompletedProcess([], 1, '{"processing":"PROCESSING"}', ''))
        self.assertEqual(sum(args[0] != 'git' and 'status' in args for args in events), 2)
        self.assertFalse(any('upload' in args for args in events))

    def test_recovered_missing_build_checks_status_before_upload(self):
        events = self.archive_case(status=subprocess.CompletedProcess([], 1, 'Build is not listed yet.', ''))
        self.assertEqual(sum(args[0] != 'git' and 'status' in args for args in events), 2)
        self.assertTrue(any('upload' in args for args in events))


class ArchiveCrash(BaseException):
    """Abrupt process loss; callers must reload only persisted state."""


class ArchiveAttemptTests(unittest.TestCase):
    """Exercise release_ios/save/recovery; only git and release effects are synthetic."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.helper = self.root / 'helper/release.py'
        self.helper.parent.mkdir()
        self.record_path = self.helper.parent / 'archive.json'
        self.old = dict(source=str(self.source), source_hash='hash', build='159', version='2026.9.19')
        self.new = dict(self.old, build='162')
        self.record_path.write_text(json.dumps(self.old))
        self.state_path = self.root / 'state.json'
        update.save(self.state_path, dict(pending_ios=dict(hash='hash', source_sha='sha')))
        self.config = dict(release_helper=str(self.helper), release_source=str(self.source))
        self.events = []
        self.boundary = None
        self.status = subprocess.CompletedProcess([], 1, '{"processing":"PROCESSING"}', '')

    def command(self, args, **kwargs):
        if args[:2] == ['git', 'rev-parse']:
            return 'sha'
        if args[0] == 'git':
            return ''
        if 'upload' in args:
            self.events.append('upload')
            return ''
        self.assertIn('archive', args)
        self.events.append('invoke')
        if self.boundary == 'before_invocation':
            raise ArchiveCrash()
        receipt = Path(args[args.index('--receipt') + 1])
        self.assertFalse(receipt.exists(), 'exclusive helper receipt must never be overwritten')
        self.record_path.unlink(missing_ok=True)
        self.events.append('archive')
        if self.boundary == 'after_unlink':
            raise ArchiveCrash()
        if self.boundary == 'partial_helper':
            self.record_path.write_text('{"source":')
            raise ArchiveCrash()
        self.record_path.write_text(json.dumps(self.new))
        if self.boundary == 'after_helper':
            raise ArchiveCrash()
        with receipt.open('x') as stream:
            if self.boundary == 'empty_receipt':
                raise ArchiveCrash()
            if self.boundary == 'partial_receipt':
                stream.write('{"source":' + json.dumps(str(self.source)) + ',"build":"162","version":')
                raise ArchiveCrash()
            stream.write(json.dumps(self.new))
        if self.boundary == 'completed_receipt':
            raise ArchiveCrash()
        return ''

    def check_status(self, args, **kwargs):
        self.assertIn('status', args)
        self.assertEqual(args[-1], '162')
        self.events.append('status')
        return self.status

    def release(self, save_hook=None):
        # Reload disk every time, as an actual restart would.
        state = json.loads(self.state_path.read_text())
        with patch.object(update, 'run', side_effect=self.command), \
             patch.object(update, 'ios_hash', return_value='hash'), \
             patch.object(update.subprocess, 'run', side_effect=self.check_status):
            if save_hook:
                with patch.object(update, 'save', side_effect=save_hook):
                    update.release_ios(self.config, state, self.state_path)
            else:
                update.release_ios(self.config, state, self.state_path)
        return json.loads(self.state_path.read_text())

    def assert_recovered(self, archive_count):
        self.boundary = None
        state = self.release()
        self.assertEqual(state['pending_ios']['build'], 162)
        self.assertEqual(state['pending_ios']['hash'], 'hash')
        self.assertEqual(state['pending_ios']['source_sha'], 'sha')
        self.release()
        self.assertEqual(self.events.count('archive'), archive_count)
        self.assertNotIn('upload', self.events)
        self.status = subprocess.CompletedProcess([], 0, '{}', '')
        self.assertEqual(self.release(), dict(ios_hash='hash', ios_build=162))
        self.assertEqual(self.events.count('archive'), archive_count)
        self.assertNotIn('upload', self.events)

    def test_intent_save_crashes_with_stale_same_source_record(self):
        for after in (False, True):
            with self.subTest(after=after):
                self.setUp()
                original_save = update.save
                def crash(path, value):
                    if after:
                        original_save(path, value)
                    raise ArchiveCrash()
                with self.assertRaises(ArchiveCrash):
                    self.release(crash)
                self.assertEqual(json.loads(self.record_path.read_text()), self.old)
                self.assert_recovered(1)

    def test_persisted_intent_without_helper_record_retries_archive(self):
        self.record_path.unlink()
        original_save = update.save
        def crash(path, value):
            original_save(path, value)
            raise ArchiveCrash()
        with self.assertRaises(ArchiveCrash):
            self.release(crash)
        self.assert_recovered(1)

    def test_every_helper_write_boundary(self):
        for boundary, archives in [('before_invocation', 1), ('after_unlink', 2), ('after_helper', 1),
                                   ('empty_receipt', 1), ('partial_receipt', 1), ('completed_receipt', 1)]:
            with self.subTest(boundary=boundary):
                self.setUp()
                self.boundary = boundary
                with self.assertRaises(ArchiveCrash):
                    self.release()
                pending = json.loads(self.state_path.read_text())['pending_ios']
                receipt = Path(pending['receipt'])
                original_receipt = receipt.read_bytes() if receipt.exists() else None
                self.assert_recovered(archives)
                if original_receipt is not None:
                    self.assertEqual(receipt.read_bytes(), original_receipt)

    def test_partial_helper_record_blocks_without_rebuild_or_upload(self):
        self.boundary = 'partial_helper'
        with self.assertRaises(ArchiveCrash):
            self.release()
        self.boundary = None
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.release()
        self.assertEqual(self.events.count('archive'), 1)
        self.assertNotIn('status', self.events)
        self.assertNotIn('upload', self.events)
        self.assertEqual(self.record_path.read_text(), '{"source":')

    def test_empty_or_partial_receipt_cannot_adopt_unchanged_prior_record(self):
        for text in ('', '{"source":', '{}', '{"build":"159"}'):
            with self.subTest(receipt=text):
                self.setUp()
                self.boundary = 'before_invocation'
                with self.assertRaises(ArchiveCrash):
                    self.release()
                pending = json.loads(self.state_path.read_text())['pending_ios']
                Path(pending['receipt']).write_text(text)
                self.boundary = None
                for _ in range(2):
                    with self.assertRaises(ValueError):
                        self.release()
                self.assertNotIn('archive', self.events)
                self.assertNotIn('status', self.events)
                self.assertNotIn('upload', self.events)
                self.assertEqual(Path(pending['receipt']).read_text(), text)

    def test_legacy_missing_and_partial_receipts_require_reconciliation(self):
        for text in (None, '', '{"source":', '{}'):
            with self.subTest(receipt=text):
                self.setUp()
                receipt = self.root / 'legacy.json'
                if text is not None:
                    receipt.write_text(text)
                update.save(self.state_path, dict(pending_ios=dict(hash='hash', source_sha='sha', receipt=str(receipt))))
                with self.assertRaises(ValueError):
                    self.release()
                self.assertEqual(self.events, [])

    def test_build_identity_save_crashes_reuse_completed_receipt(self):
        for after in (False, True):
            with self.subTest(after=after):
                self.setUp()
                original_save = update.save
                def crash(path, value):
                    selected = 'build' in value.get('pending_ios', {})
                    if not selected or after:
                        original_save(path, value)
                    if selected:
                        raise ArchiveCrash()
                with self.assertRaises(ArchiveCrash):
                    self.release(crash)
                self.assert_recovered(1)

    def test_recovery_state_and_snapshot_crash_boundaries(self):
        for boundary in ('before_selection', 'after_selection', 'before_snapshot', 'after_snapshot'):
            with self.subTest(boundary=boundary):
                self.setUp()
                self.boundary = 'partial_receipt'
                with self.assertRaises(ArchiveCrash):
                    self.release()
                self.boundary = None
                original_save = update.save
                def crash(path, value):
                    selected = path == self.state_path and 'recovered_receipt' in value.get('pending_ios', {})
                    snapshot = path != self.state_path
                    hit = (selected and boundary.endswith('selection')) or (snapshot and boundary.endswith('snapshot'))
                    if not hit or boundary.startswith('after'):
                        original_save(path, value)
                    if hit:
                        raise ArchiveCrash()
                with self.assertRaises(ArchiveCrash):
                    self.release(crash)
                # Once selected durably, later helper state cannot replace it.
                if boundary != 'before_selection':
                    self.record_path.write_text(json.dumps(dict(self.new, build='163')))
                self.assert_recovered(1)

    def test_recovered_build_status_and_upload_guards(self):
        for report, uploaded, rejected in [
                (subprocess.CompletedProcess([], 1, '{"processing":"PROCESSING"}', ''), False, False),
                (subprocess.CompletedProcess([], 0, '{}', ''), False, False),
                (subprocess.CompletedProcess([], 1, 'Build is not listed yet.', ''), True, False),
                (subprocess.CompletedProcess([], 1, '', 'API failure'), False, True),
                (subprocess.CompletedProcess([], 1, '{"processing":"FAILED"}', ''), False, True)]:
            with self.subTest(report=report):
                self.setUp()
                self.boundary = 'partial_receipt'
                with self.assertRaises(ArchiveCrash):
                    self.release()
                self.boundary = None
                self.status = report
                if rejected:
                    with self.assertRaises(RuntimeError):
                        self.release()
                else:
                    self.release()
                self.assertEqual(self.events.count('archive'), 1)
                self.assertEqual('upload' in self.events, uploaded)
                if uploaded:
                    self.assertEqual(self.events[-2:], ['status', 'upload'])
                    self.status = subprocess.CompletedProcess([], 1, '{"processing":"PROCESSING"}', '')
                    self.release()
                    self.assertEqual(self.events.count('upload'), 1)

    def test_helper_lock_blocks_source_changes_and_recovery(self):
        import fcntl
        with (self.helper.parent / 'release.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(update, 'run') as command, patch.object(update.subprocess, 'run') as status:
                with self.assertRaises(BlockingIOError):
                    update.release_ios(self.config, json.loads(self.state_path.read_text()), self.state_path)
                command.assert_not_called()
                status.assert_not_called()

    def symlink_helper(self):
        self.helper.touch()
        link = self.root / 'linked-release.py'
        link.symlink_to(self.helper)
        self.config['release_helper'] = str(link)

    def test_symlinked_helper_uses_real_release_lock(self):
        self.symlink_helper()
        self.test_helper_lock_blocks_source_changes_and_recovery()
        self.assertFalse((self.root / 'release.lock').exists())

    def test_symlinked_helper_recovers_actual_archive_without_rebuild(self):
        self.symlink_helper()
        self.boundary = 'partial_receipt'
        with self.assertRaises(ArchiveCrash):
            self.release()
        self.assert_recovered(1)
        self.assertFalse((self.root / 'release.lock').exists())

    def test_real_child_inherits_helper_lock_and_preserves_cli_contract(self):
        # Run only this generated harmless helper, never the private helper.
        self.helper.write_text("""import fcntl, json, os, sys
from pathlib import Path
ROOT = Path(__file__).parent
def main():
    assert sys.argv[1] == 'archive'
    assert os.umask(0o077) == 0o077
    with (ROOT / 'release.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError('inherited lock is not held')
    source = sys.argv[sys.argv.index('--source') + 1]
    receipt = Path(sys.argv[sys.argv.index('--receipt') + 1])
    record = dict(source=source, source_hash='hash', build='162', version='2026.9.19')
    (ROOT / 'archive.json').unlink(missing_ok=True)
    (ROOT / 'archive.json').write_text(json.dumps(record))
    with receipt.open('x') as stream:
        json.dump(record, stream)
if __name__ == '__main__':
    raise AssertionError('script guard must not acquire a conflicting second lock')
""")
        original_run = update.run
        def command(args, **kwargs):
            if args[0] == 'git':
                return 'sha' if args[1] == 'rev-parse' else ''
            return original_run(args, **kwargs)
        state = json.loads(self.state_path.read_text())
        with patch.object(update, 'run', side_effect=command), patch.object(update, 'ios_hash', return_value='hash'):
            update.prepare_archive(state['pending_ios'], self.helper, self.source, state, self.state_path)
        self.assertEqual(state['pending_ios']['build'], 162)


if __name__ == '__main__':
    unittest.main()
