import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / 'backend'))
import migrate
from coconut.providers import FixtureProvider
from coconut.service import Service


class Operations:
    def __init__(self):
        self.stops = 0
        self.restores = 0
        self.fail_health = False
        self.fail_restore = False
        self.start_on_restore = True

    def inspect(self, root):
        return dict(mode='managed', project=str(root), data_dir=str(root / '.runtime/host'))

    def stop(self, root):
        self.stops += 1
        return self.inspect(root)

    def restore(self, root, state):
        self.restores += 1
        if self.fail_restore:
            raise RuntimeError('The start result is unknown.')
        if self.start_on_restore:
            data = root / '.runtime/host'
            service = Service(data, FixtureProvider(data), start_workers=False)
            service.close()

    def health(self, root, address):
        if self.fail_health:
            raise RuntimeError('The host became unreachable.')
        return dict(ok=True, api_version=1)

    def probe(self, root, address):
        return self.health(root, address)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.target = self.root / 'target'
        self.source_data = self.source / '.runtime/host'
        self.target_data = self.target / '.runtime/host'
        self.service = Service(self.source_data, FixtureProvider(self.source_data), start_workers=False)
        self.addCleanup(self.close_source)
        self.bots = [self.write('POST', '/v1/bots', dict(name=name, role='Testing')) for name in ('Ada', 'Lin')]
        for bot, text in zip(self.bots, ('Create report.md containing a review.', 'Check 17 multiplied by 19.')):
            response = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text=text, mode='queue'))
            self.service._run_task(response['id'])
        self.device_id = str(uuid4())
        self.service.db.execute('INSERT INTO devices VALUES (?,?,?,?)', (self.device_id, 'Original phone', 'original-token-hash', 'today'))
        self.service.db.execute('INSERT INTO pairing VALUES (?,?,?)', ('pair-code-hash', 100, 0))
        self.service.db.execute('INSERT INTO pair_attempts VALUES (?)', (100,))
        blank = Service(self.target_data, FixtureProvider(self.target_data), start_workers=False)
        blank.close()
        self.package_id = self.marker()
        self.staging = self.root / 'staging'
        self.staging.mkdir()
        self.snapshot = self.staging / 'snapshot.sqlite3'
        migrate.snapshot(self.source_data / 'coconut.sqlite3', self.snapshot)
        migrate.revoke_devices(self.snapshot)
        self.payload = self.snapshot.read_bytes()
        self.sha256 = migrate.deploy.sha256(self.payload)
        self.migration_id = str(uuid4())
        self.operations = Operations()

    def close_source(self):
        if self.service is not None:
            self.service.close()
            self.service = None

    def write(self, method, path, values):
        return self.service.mutate(method, path, values, str(uuid4()), 'test-phone')

    def marker(self):
        file = self.target / 'scripts/host.py'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'# Installed source.\n')
        hashes = {'scripts/host.py': migrate.deploy.sha256(file.read_bytes())}
        package_id = migrate.deploy.sha256(migrate.deploy.canonical(hashes))
        migrate.deploy.write_json(self.target / '.coconut-install.json', dict(version=1, installation_id=str(uuid4()),
            files=hashes, package_id=package_id, state='installed', address='https://mini.example.ts.net:8960'))
        return package_id

    def receive(self, **changes):
        kwargs = dict(root=self.target, migration_id=self.migration_id, package_id=self.package_id,
                      expected_hash=self.sha256, stream=io.BytesIO(self.payload), operations=self.operations)
        kwargs.update(changes)
        return migrate.receive(**kwargs)

    def test_live_wal_snapshot_preserves_history_ids_requests_and_pending_decision(self):
        before = migrate.metadata(self.source_data / 'coconut.sqlite3')
        copied = migrate.metadata(self.snapshot)
        self.assertEqual(before['hashes'], copied['hashes'])
        self.assertEqual(before['profiles'], copied['profiles'])
        self.assertEqual(before['decisions'], copied['decisions'])
        self.assertEqual(len(copied['decisions']), 1)
        self.assertEqual(copied['decisions'][0][1], 'pending')
        self.assertGreater(copied['counts']['requests'], 0)
        with migrate.database(self.snapshot) as connection:
            device = connection.execute('SELECT * FROM devices').fetchone()
            self.assertEqual((device[0], device[1], device[3]), (self.device_id, 'Original phone', 'today'))
            self.assertTrue(device[2].startswith('revoked:'))
            self.assertEqual(connection.execute('SELECT count(*) FROM pairing').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT count(*) FROM pair_attempts').fetchone()[0], 0)
        self.assertEqual(self.service.db.execute('SELECT token_hash FROM devices').fetchone()[0], 'original-token-hash')

    def test_receive_regenerates_profiles_and_preserves_target_connection_and_credentials(self):
        protected = ['server.crt', 'server.key', 'connection.json', 'credentials.json']
        for name in protected:
            (self.target_data / name).write_bytes(('target-' + name).encode())
        result = self.receive()
        self.assertEqual(result['phase'], 'complete')
        self.assertTrue(result['takeover'])
        self.assertEqual(result['source_metadata'], migrate.metadata(self.snapshot))
        self.assertEqual(result['metadata']['cursor'], result['source_metadata']['cursor'] + 2)
        self.assertEqual(self.operations.stops, 1)
        self.assertEqual(self.operations.restores, 1)
        for name in protected:
            self.assertEqual((self.target_data / name).read_bytes(), ('target-' + name).encode())
        for bot in self.bots:
            profile = self.target_data / 'bots' / bot['id'] / 'home/coconut-profile.json'
            self.assertEqual(json.loads(profile.read_text())['session_id'], bot['session_id'])
        blank = migrate.migration_folder(self.target, self.migration_id) / 'blank.sqlite3'
        self.assertEqual(migrate.metadata(blank)['counts']['bots'], 0)

    def test_completed_retry_never_rewinds_new_destination_work(self):
        first = self.receive()
        service = Service(self.target_data, FixtureProvider(self.target_data), start_workers=False)
        service.mutate('POST', '/v1/bots', dict(name='New after transfer'), str(uuid4()), self.device_id)
        service.close()
        before = migrate.metadata(self.target_data / 'coconut.sqlite3')
        second = self.receive(stream=io.BytesIO(b'Not read after a completed receipt.'))
        self.assertEqual(first, second)
        self.assertEqual(migrate.metadata(self.target_data / 'coconut.sqlite3'), before)
        self.assertEqual(self.operations.stops, 1)

    def test_chat_creation_receipt_and_profile_survive_transfer(self):
        task = self.write('POST', '/v1/bots/' + self.bots[1]['id'] + '/messages',
                          {'text': 'Create Scout for research.', 'mode': 'queue'})
        with self.service.transaction():
            task = self.service._get('tasks', task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        payload = dict(source_message_id=task['_preference_source_ids'][0], name='Scout',
                       role='Research', instructions='Check sources before answering.')
        receipt = self.service.emit(task['id'], 'bot_create', payload)
        self.service.emit(task['id'], 'completed', {'summary': 'Created Scout.'})
        migrate.snapshot(self.source_data / 'coconut.sqlite3', self.snapshot)
        migrate.revoke_devices(self.snapshot)
        self.payload = self.snapshot.read_bytes()
        self.sha256 = migrate.deploy.sha256(self.payload)

        result = self.receive()
        self.assertEqual(result['metadata']['counts']['bot_creation_receipts'], 1)
        self.assertEqual(result['metadata']['hashes']['bot_creation_receipts'],
                         result['source_metadata']['hashes']['bot_creation_receipts'])
        target = Service(self.target_data, FixtureProvider(self.target_data), start_workers=False)
        try:
            self.assertEqual(target._all('bot_creation_receipts'), self.service._all('bot_creation_receipts'))
            self.assertEqual(len(target.snapshot()['bots']), 3)
            home = self.target_data / 'bots' / receipt['profile_id'] / 'home'
            self.assertEqual((home / 'SOUL.md').read_text(), payload['instructions'])
            self.assertEqual(json.loads((home / 'coconut-profile.json').read_text())['session_id'], receipt['session_id'])
        finally:
            target.close()

    def test_nonempty_destination_rejects_before_stop_or_swap(self):
        service = Service(self.target_data, FixtureProvider(self.target_data), start_workers=False)
        service.mutate('POST', '/v1/bots', dict(name='Keep this bot'), str(uuid4()), 'phone')
        service.close()
        before = migrate.metadata(self.target_data / 'coconut.sqlite3')
        with self.assertRaisesRegex(migrate.MigrationError, 'not empty'):
            self.receive()
        self.assertEqual(migrate.metadata(self.target_data / 'coconut.sqlite3'), before)
        self.assertEqual(self.operations.stops, 0)

    def test_changed_source_receipt_or_file_rejects_before_stop(self):
        with self.assertRaisesRegex(migrate.MigrationError, 'Update the destination'):
            self.receive(package_id='0' * 64)
        (self.target / 'scripts/host.py').write_text('Changed locally')
        with self.assertRaisesRegex(migrate.MigrationError, 'differs'):
            self.receive()
        self.assertEqual(self.operations.stops, 0)

    def test_payload_hash_limit_and_active_tokens_reject_before_stop(self):
        with self.assertRaisesRegex(migrate.MigrationError, 'hash or size'):
            self.receive(stream=io.BytesIO(b'changed'))
        self.assertEqual(self.operations.stops, 0)
        candidate = self.staging / 'untouched.sqlite3'
        migrate.snapshot(self.source_data / 'coconut.sqlite3', candidate)
        payload = candidate.read_bytes()
        with self.assertRaisesRegex(migrate.MigrationError, 'active pairing'):
            self.receive(migration_id=str(uuid4()), stream=io.BytesIO(payload), expected_hash=migrate.deploy.sha256(payload))
        with patch.object(migrate, 'MAX_DATABASE_BYTES', 10):
            with self.assertRaisesRegex(migrate.MigrationError, 'size limit'):
                migrate.assert_payload(io.BytesIO(self.payload), self.staging / 'too-big.sqlite3', self.sha256)

    def test_generated_profiles_pass_but_custom_files_and_symlinks_fail(self):
        self.close_source()
        migrate.validate_files(self.source, self.source_data, self.staging)
        bot_home = self.source_data / 'bots' / self.bots[0]['id'] / 'home'
        for name in ('custom.md', '../workspace/custom.md'):
            path = bot_home / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('Do not omit this file.')
            with self.assertRaisesRegex(migrate.MigrationError, 'non-generated'):
                migrate.validate_files(self.source, self.source_data, self.staging)
            path.unlink()
        config = bot_home / 'config.yaml'
        original = config.read_bytes()
        config.write_text('model: custom')
        with self.assertRaisesRegex(migrate.MigrationError, 'custom profile'):
            migrate.validate_files(self.source, self.source_data, self.staging)
        config.write_text(json.dumps(json.loads(original), sort_keys=True))
        migrate.validate_files(self.source, self.source_data, self.staging)
        (bot_home / 'external').symlink_to(self.root)
        with self.assertRaisesRegex(migrate.deploy.DeployError, 'symbolic link'):
            migrate.validate_files(self.source, self.source_data, self.staging)

    def test_arbitrary_artifact_and_profile_state_are_not_silently_omitted(self):
        self.close_source()
        path = self.source_data / 'artifacts' / str(uuid4())
        path.write_text('Private output')
        with self.assertRaisesRegex(migrate.MigrationError, 'Artifact files'):
            migrate.validate_files(self.source, self.source_data, self.staging)
        path.unlink()
        state = self.source_data / 'bots' / self.bots[0]['id'] / 'home/state.db'
        with migrate.closing(sqlite3.connect(state)) as connection, connection:
            connection.execute('CREATE TABLE async_delegations(id TEXT)')
            connection.execute('INSERT INTO async_delegations VALUES (?)', ('active',))
        with self.assertRaisesRegex(migrate.MigrationError, 'durable profile state'):
            migrate.validate_files(self.source, self.source_data, self.staging)

    def test_failure_before_swap_restores_blank_target(self):
        before = migrate.metadata(self.target_data / 'coconut.sqlite3')
        original_snapshot = migrate.snapshot
        def fail_backup(source, target):
            if target.name == 'blank.sqlite3':
                raise OSError('Injected disk failure')
            original_snapshot(source, target)
        with patch.object(migrate, 'snapshot', side_effect=fail_backup):
            result = self.receive()
        self.assertEqual(result['phase'], 'restored')
        self.assertFalse(result['takeover'])
        self.assertEqual(migrate.metadata(self.target_data / 'coconut.sqlite3'), before)
        self.assertEqual(self.operations.restores, 1)

    def test_failure_after_swap_before_start_restores_only_blank_target(self):
        before = migrate.metadata(self.target_data / 'coconut.sqlite3')
        original_metadata = migrate.metadata
        def fail_import_check(path):
            result = original_metadata(path)
            if path == self.target_data / 'coconut.sqlite3' and result['counts']['bots']:
                raise migrate.MigrationError('Injected verification failure')
            return result
        with patch.object(migrate, 'metadata', side_effect=fail_import_check):
            result = self.receive()
        self.assertEqual(result['phase'], 'restored')
        self.assertFalse(result['takeover'])
        self.assertEqual(migrate.metadata(self.target_data / 'coconut.sqlite3'), before)

    def check_failed_takeover(self, failure):
        setattr(self.operations, failure, True)
        with self.assertRaises(RuntimeError):
            self.receive()
        saved = migrate.read_journal(self.target, self.migration_id)
        self.assertTrue(saved['takeover'])
        self.assertEqual(saved['phase'], 'starting')
        self.assertEqual(migrate.metadata(self.target_data / 'coconut.sqlite3')['counts']['bots'], 2)
        self.assertEqual(self.receive(), saved)
        self.assertEqual(self.operations.restores, 1)

    def test_interrupt_immediately_after_database_rename_restores_blank_before_receipt(self):
        before = migrate.metadata(self.target_data / 'coconut.sqlite3')
        original_replace = migrate.os.replace
        interrupted = False
        def interrupt_after_replace(source, target):
            nonlocal interrupted
            original_replace(source, target)
            if Path(source).name == 'incoming.sqlite3' and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
        with patch.object(migrate.os, 'replace', side_effect=interrupt_after_replace):
            result = self.receive()
        self.assertTrue(interrupted)
        self.assertEqual(result['phase'], 'restored')
        self.assertFalse(result['takeover'])
        self.assertEqual(migrate.metadata(self.target_data / 'coconut.sqlite3'), before)
        self.assertEqual(self.operations.restores, 1)

    def test_failed_start_never_restores_blank_over_imported_data(self):
        self.check_failed_takeover('fail_restore')

    def test_failed_health_never_restores_blank_over_imported_data(self):
        self.check_failed_takeover('fail_health')

    def test_safe_path_and_locks_reject_symlink_or_concurrent_change(self):
        (self.target / 'outside').symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(migrate.deploy.DeployError):
            migrate.safe(self.target / 'outside/file', self.target)
        lock = self.target / 'test.lock'
        with migrate.locked(lock, self.target):
            with self.assertRaisesRegex(migrate.MigrationError, 'holds this lock'):
                with migrate.locked(lock, self.target):
                    self.fail('The second operation took the same lock.')

    def test_source_plist_restore_preserves_exact_bytes(self):
        folder = self.root / 'migration'
        folder.mkdir()
        saved = folder / 'source-agent.plist'
        saved.write_bytes(b'Exact private launch configuration')
        original = self.root / 'Library/LaunchAgents/local.coconut.agent.plist'
        original.parent.mkdir(parents=True)
        with patch.object(migrate.host, 'owned_agent', return_value=(original, False)), \
             patch.object(migrate.host, 'restore_after_update') as restore:
            migrate.source_restore({'mode': 'managed'}, folder)
        self.assertEqual(original.read_bytes(), b'Exact private launch configuration')
        self.assertFalse(saved.exists())
        restore.assert_called_once_with({'mode': 'managed'})

    def transfer_setup(self, remote_result):
        self.close_source()
        agent = self.source / 'Library/LaunchAgents/local.coconut.agent.plist'
        agent.parent.mkdir(parents=True)
        agent.write_bytes(b'Exact launch configuration')
        state = dict(mode='managed', project=str(self.source), data_dir=str(self.source_data))
        config = dict(version=1, ssh='Coconut@mini.example.ts.net', directory='Coconut', address='https://mini.example.ts.net:8960')
        patches = [patch.object(migrate, 'ROOT', self.source),
                   patch.object(migrate.host, 'ROOT', self.source),
                   patch.object(migrate.host, 'DEFAULT_DATA', self.source_data),
                   patch.object(migrate.host, 'stop_for_update', return_value=state),
                   patch.object(migrate.host, 'owned_agent', side_effect=lambda: (agent, False)),
                   patch.object(migrate.host, 'restore_after_update'),
                   patch.object(migrate.remote, 'configuration', return_value=config),
                   patch.object(migrate.remote, 'source_files', return_value={'scripts/host.py': b'# Installed source.\n'}),
                   patch.object(migrate.remote, 'verify_remote_health')]
        for replacement in patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        def call(config, operation, migration_id, package_id='', snapshot_hash='', input_file=None):
            if operation == 'preflight':
                return {'ready': True}
            self.assertFalse(agent.exists())
            marker = json.loads((self.source_data / 'moved-to.json').read_text())
            self.assertEqual(marker['migration_id'], migration_id)
            return remote_result(migration_id, package_id, snapshot_hash, input_file)
        replacement = patch.object(migrate, 'remote_call', side_effect=call)
        replacement.start()
        self.addCleanup(replacement.stop)
        return agent

    def test_source_cutover_keeps_original_data_and_disables_login_restart(self):
        before = migrate.metadata(self.source_data / 'coconut.sqlite3')
        def result(identity, package_id, checksum, stream):
            return self.receive(migration_id=identity, package_id=package_id, expected_hash=checksum, stream=stream)
        agent = self.transfer_setup(result)
        summary = migrate.transfer()
        self.assertTrue(summary['source_stopped'])
        self.assertEqual(migrate.metadata(self.source_data / 'coconut.sqlite3'), before)
        folder = migrate.migration_folder(self.source, summary['migration_id'])
        self.assertEqual((folder / 'source-agent.plist').read_bytes(), b'Exact launch configuration')
        self.assertFalse(agent.exists())
        self.assertTrue((self.source_data / 'moved-to.json').is_file())
        migrate.host.restore_after_update.assert_not_called()

    def test_unknown_ssh_result_keeps_source_fenced_and_never_retries(self):
        def result(identity, package_id, checksum, stream):
            self.receive(migration_id=identity, package_id=package_id, expected_hash=checksum, stream=stream)
            raise migrate.MigrationError('Injected response loss after takeover.')
        agent = self.transfer_setup(result)
        with self.assertRaisesRegex(migrate.MigrationError, 'response loss'):
            migrate.transfer()
        self.assertFalse(agent.exists())
        self.assertTrue((self.source_data / 'moved-to.json').is_file())
        self.assertEqual(migrate.remote_call.call_count, 2)
        migrate.host.restore_after_update.assert_not_called()
        self.assertEqual(migrate.metadata(self.target_data / 'coconut.sqlite3')['counts']['bots'], 2)

    def test_confirmed_pre_takeover_failure_restores_source_mode_and_plist(self):
        agent = self.transfer_setup(lambda identity, package, checksum, stream: {
            'phase': 'restored', 'takeover': False, 'migration_id': identity,
            'package_id': package, 'snapshot_sha256': checksum})
        with self.assertRaisesRegex(migrate.MigrationError, 'not confirmed takeover'):
            migrate.transfer()
        self.assertEqual(agent.read_bytes(), b'Exact launch configuration')
        self.assertFalse((self.source_data / 'moved-to.json').exists())
        self.assertFalse((self.source / '.runtime/migrate/outgoing.json').exists())
        migrate.host.restore_after_update.assert_called_once()

    def test_source_validation_failure_restores_before_any_payload_is_sent(self):
        agent = self.transfer_setup(lambda *args: self.fail('Invalid data reached the receiver.'))
        (self.source_data / 'artifacts' / str(uuid4())).write_text('Keep this file.')
        with self.assertRaisesRegex(migrate.MigrationError, 'Artifact files'):
            migrate.transfer()
        self.assertTrue(agent.exists())
        self.assertEqual(migrate.remote_call.call_count, 1)
        migrate.host.restore_after_update.assert_called_once()


if __name__ == '__main__':
    unittest.main()
