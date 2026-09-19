import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('automatic_update', Path(__file__).with_name('automatic_update.py'))
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class AutomaticUpdateTests(unittest.TestCase):
    def test_only_successful_merged_commit_passes(self):
        pull = dict(merged_at='today', merge_commit_sha='abc', number=3, base=dict(ref='main'))
        record = dict(state='MERGED', mergeCommit=dict(oid='abc'), statusCheckRollup=[dict(conclusion='SUCCESS')])
        checked = dict(total_count=1, check_runs=[dict(status='completed', conclusion='success')])
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
                    (live / '.runtime/hermes/install.json').write_text('{}')
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
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved['pending_ios']['source_sha'], 'corrected')
            release.assert_called_once()

    def test_completed_archive_receipt_survives_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            receipt = Path(directory) / 'archive.json'
            receipt.write_text(json.dumps(dict(source=str(source), source_hash='hash', build=162)))
            state = dict(pending_ios=dict(hash='hash', source_sha='sha', receipt=str(receipt)))
            config = dict(release_helper='/release/release.py', release_source=str(source))
            with patch.object(update, 'run', return_value='') as command, \
                 patch.object(update, 'ios_hash', return_value='hash'), \
                 patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')):
                update.release_ios(config, state, Path(directory) / 'state.json')
            self.assertFalse(any('archive' in call.args[0] for call in command.call_args_list))
            self.assertEqual(state['ios_build'], 162)


if __name__ == '__main__':
    unittest.main()
