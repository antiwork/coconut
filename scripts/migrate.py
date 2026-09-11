#!/usr/bin/env python3
"""Move an idle Coconut dataset to an empty, managed Mac mini installation."""
from __future__ import annotations

import argparse
import ast
from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from uuid import UUID, uuid4

import deploy
import host
import remote

ROOT = Path(__file__).resolve().parents[1]
MAX_DATABASE_BYTES = 32 * 1024 * 1024
TABLES = {'bots', 'tasks', 'messages', 'decisions', 'artifacts', 'routines',
          'events', 'requests', 'devices', 'pairing', 'pair_attempts', 'exchanges', 'deliveries',
          'routine_occurrences', 'contexts', 'native_receipts', 'bot_creation_receipts', 'bot_deletion_receipts', 'settings', 'receipts', 'sqlite_sequence', 'push_devices', 'push_outbox', 'push_routes'}
AUTH_TABLES = {'pairing', 'pair_attempts', 'push_devices', 'push_outbox', 'push_routes'}
IGNORED_PROFILE_FILES = {'home/logs/agent.log', 'home/logs/errors.log', 'home/cache/tool_discovery_cache.json'}


class MigrationError(Exception):
    pass


def safe(path, root):
    return deploy.safe_path(path, root)


def private(path, root):
    safe(path, root)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def identifier(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise MigrationError('The migration ID is invalid.')
    return value


@contextmanager
def locked(path, root):
    safe(path, root)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError('Another Coconut operation holds this lock.') from None
        yield


@contextmanager
def database(path):
    connection = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        yield connection
    finally:
        connection.close()


def metadata(path):
    if path.stat().st_size > MAX_DATABASE_BYTES:
        raise MigrationError('The database exceeds this transfer size limit.')
    with database(path) as connection:
        connection.execute('BEGIN')
        if connection.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise MigrationError('The database integrity check failed.')
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names != TABLES or connection.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('view','trigger')").fetchone()[0]:
            raise MigrationError('This database schema is outside the supported migration scope.')
        counts, hashes = {}, {}
        for table in sorted(TABLES):
            rows = sorted(connection.execute('SELECT * FROM "' + table + '"').fetchall(), key=repr)
            counts[table] = len(rows)
            if table not in AUTH_TABLES:
                if table == 'devices':
                    rows = [(row[0], row[1], row[3]) for row in rows]
                hashes[table] = deploy.sha256(deploy.canonical(rows))
        bots = [json.loads(row[0]) for row in connection.execute('SELECT data FROM bots')]
        tasks = [json.loads(row[0]) for row in connection.execute('SELECT data FROM tasks')]
        decisions = [json.loads(row[0]) for row in connection.execute('SELECT data FROM decisions')]
        identities = sorted([[bot['id'], bot.get('profile_id'), bot['session_id']] for bot in bots])
        if any(identifier(bot_id) != profile_id or not identifier(session_id) for bot_id, profile_id, session_id in identities):
            raise MigrationError('A bot profile identity is invalid.')
        bot_ids = {bot['id'] for bot in bots}
        task_ids = {task['id'] for task in tasks}
        if (any(task['bot_id'] not in bot_ids for task in tasks)
                or any(item['bot_id'] not in bot_ids or item['task_id'] not in task_ids for item in decisions)):
            raise MigrationError('A task or decision has an invalid owner.')
        return {'counts': counts, 'hashes': hashes, 'profiles': identities,
                'tasks': sorted([[task['id'], task['status']] for task in tasks]),
                'decisions': sorted([[item['id'], item['status']] for item in decisions]),
                'cursor': connection.execute('SELECT coalesce(max(seq),0) FROM events').fetchone()[0]}


def verify_started(path, expected):
    observed = metadata(path)
    for key in ('profiles', 'tasks', 'decisions'):
        if observed[key] != expected[key]:
            raise MigrationError('The destination identities or states changed after startup.')
    for table in TABLES - AUTH_TABLES - {'events', 'sqlite_sequence'}:
        if (observed['counts'][table] != expected['counts'][table]
                or observed['hashes'][table] != expected['hashes'][table]):
            raise MigrationError('The destination records changed after startup.')
    with database(path) as connection:
        prefix = sorted(connection.execute('SELECT * FROM events WHERE seq<=?', (expected['cursor'],)).fetchall(), key=repr)
        if deploy.sha256(deploy.canonical(prefix)) != expected['hashes']['events']:
            raise MigrationError('The original event history changed after startup.')
        bot_ids = {row[0] for row in expected['profiles']}
        extra = connection.execute('SELECT type,bot_id,task_id FROM events WHERE seq>?', (expected['cursor'],)).fetchall()
        if len(extra) != len(bot_ids) or any(kind != 'bot.updated' or bot not in bot_ids or task is not None for kind, bot, task in extra):
            raise MigrationError('The destination performed unexpected work after startup.')
    return observed


def supported(path, *, empty=False):
    result = metadata(path)
    counts = result['counts']
    if empty:
        if any(counts[name] for name in TABLES - {'devices', 'pairing', 'pair_attempts', 'settings', 'sqlite_sequence'}):
            raise MigrationError('The destination is not empty. Coconut did not replace it.')
        return result
    if not counts['bots'] or counts['receipts'] or counts['routines'] or counts['routine_occurrences'] or counts['contexts'] or counts['deliveries']:
        raise MigrationError('This transfer supports bots with completed work and pending decisions only.')
    if any(status not in {'completed', 'needs_you', 'cancelled', 'failed'} for _, status in result['tasks']):
        raise MigrationError('Finish or cancel active and scheduled tasks before transferring this dataset.')
    if any(status != 'pending' for _, status in result['decisions']):
        raise MigrationError('This transfer supports pending decisions without execution receipts only.')
    with database(path) as connection:
        if connection.execute("SELECT count(*) FROM artifacts WHERE json_extract(data,'$.kind')='file' AND json_extract(data,'$._content_base64') IS NULL").fetchone()[0]:
            raise MigrationError('External artifact files need a separate transfer. Coconut kept the source intact.')
        if connection.execute("SELECT count(*) FROM settings WHERE key='base_url' AND value NOT IN ('', 'null', '\"\"')").fetchone()[0]:
            raise MigrationError('A model endpoint needs a separate review before transfer.')
    return result


def snapshot(source, target):
    with database(source) as original, closing(sqlite3.connect(target)) as destination:
        original.backup(destination)
        destination.execute('PRAGMA journal_mode=DELETE')
    target.chmod(0o600)
    with target.open('rb') as handle:
        os.fsync(handle.fileno())
    if target.stat().st_size > MAX_DATABASE_BYTES:
        raise MigrationError('The database exceeds this transfer size limit.')


def revoke_devices(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        for (device_id,) in connection.execute('SELECT id FROM devices').fetchall():
            connection.execute('UPDATE devices SET token_hash=? WHERE id=?',
                               ('revoked:' + secrets.token_hex(32), device_id))
        connection.execute('DELETE FROM pairing')
        connection.execute('DELETE FROM pair_attempts')
        connection.execute('DELETE FROM push_devices')
        connection.execute('DELETE FROM push_outbox')
        connection.execute('DELETE FROM push_routes')
    with path.open('rb') as handle:
        os.fsync(handle.fileno())


def validate_files(root, data, staging, *, empty=False):
    for folder in ('artifacts', 'bots'):
        safe(data / folder, root)
    if (data / 'artifacts').exists() and any((data / 'artifacts').iterdir()):
        raise MigrationError('Artifact files need a separate transfer. No source files were removed.')
    bots_root = data / 'bots'
    if not bots_root.exists():
        return
    if empty:
        if any(bots_root.iterdir()):
            raise MigrationError('The destination contains existing bot profiles.')
        return
    sys.path.insert(0, str(root / 'backend'))
    from coconut.profiles import HermesProfiles
    with database(data / 'coconut.sqlite3') as connection:
        bots = [json.loads(row[0]) for row in connection.execute('SELECT data FROM bots')]
    by_id = {bot['id']: bot for bot in bots}
    default_soul = None
    source = safe(root / '.runtime/hermes/source/hermes_cli/default_soul.py', root)
    if source.is_file():
        for node in ast.parse(source.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(name, ast.Name) and name.id == 'DEFAULT_SOUL_MD' for name in node.targets):
                default_soul = ast.literal_eval(node.value)
    mirrors = private(staging / 'profile-mirrors', staging)
    for directory in bots_root.iterdir():
        safe(directory, root)
        if directory.name not in by_id or not directory.is_dir():
            raise MigrationError('An unknown bot profile needs a separate transfer.')
        bot = by_id[directory.name]
        HermesProfiles(mirrors).sync(bot)
        expected = mirrors / 'bots' / bot['id']
        for path in directory.rglob('*'):
            safe(path, root)
            if path.is_dir():
                continue
            name = path.relative_to(directory).as_posix()
            if not path.is_file():
                raise MigrationError('A profile contains an unsupported file type.')
            if name in IGNORED_PROFILE_FILES:
                continue
            mirror = expected / name
            if name in {'home/SOUL.md', 'home/COCONUT_MEMORY.md', 'home/profile.yaml', 'home/coconut-profile.json',
                        'home/config.yaml', 'home/.no-bundled-skills'}:
                if not mirror.is_file():
                    raise MigrationError('A custom profile file cannot regenerate from the database.')
                try:
                    if name.endswith(('.json', '.yaml')):
                        actual, generated = json.loads(path.read_text()), json.loads(mirror.read_text())
                        if name == 'home/config.yaml':
                            actual_cwd = actual.get('terminal', {}).get('cwd')
                            if not actual_cwd or Path(actual_cwd).resolve() != (directory / 'workspace').resolve():
                                raise MigrationError('A custom working directory needs a separate transfer.')
                            actual['terminal'].pop('cwd', None)
                            generated.get('terminal', {}).pop('cwd', None)
                        matches = actual == generated
                    else:
                        matches = path.read_bytes() == mirror.read_bytes()
                except ValueError:
                    matches = False
                if not matches:
                    raise MigrationError('A custom profile file cannot regenerate from the database.')
            elif name == 'home/SOUL.before-coconut.md':
                if default_soul is None or path.read_text() != default_soul:
                    raise MigrationError('A custom profile backup needs a separate transfer.')
            elif name == 'home/state.db':
                with database(path) as connection:
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if tables != {'async_delegations'} or connection.execute('SELECT count(*) FROM async_delegations').fetchone()[0]:
                        raise MigrationError('Hermes has durable profile state that needs a separate transfer.')
            else:
                raise MigrationError('A non-generated profile or workspace file needs a separate transfer.')


def assert_payload(stream, path, expected_hash):
    if not isinstance(expected_hash, str) or len(expected_hash) != 64 or any(c not in '0123456789abcdef' for c in expected_hash):
        raise MigrationError('The expected snapshot hash is invalid.')
    data = stream.read(MAX_DATABASE_BYTES + 1)
    if len(data) > MAX_DATABASE_BYTES or deploy.sha256(data) != expected_hash:
        raise MigrationError('The received snapshot does not match its hash or size limit.')
    with path.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)
    supported(path)
    with database(path) as connection:
        if (connection.execute("SELECT count(*) FROM devices WHERE token_hash NOT GLOB 'revoked:*'").fetchone()[0]
                or connection.execute('SELECT count(*) FROM pairing').fetchone()[0]
                or connection.execute('SELECT count(*) FROM pair_attempts').fetchone()[0]):
            raise MigrationError('The received snapshot still contains active pairing credentials.')


def migration_folder(root, migration_id):
    return safe(root / '.runtime/migrate' / identifier(migration_id), root)


def target_provenance(root, package_id):
    marker = deploy.read_marker(root)
    if not marker or marker['state'] != 'installed' or marker['package_id'] != package_id:
        raise MigrationError('Update the destination to this source package before transferring data.')
    for name, digest in marker['files'].items():
        path = safe(root / name, root)
        if not path.is_file() or deploy.sha256(path.read_bytes()) != digest:
            raise MigrationError('A destination source file differs from its deployment receipt.')
    return marker


def target_preflight(root, package_id, operations):
    marker = target_provenance(root, package_id)
    state = operations.inspect(root)
    if state['mode'] != 'managed':
        raise MigrationError('The destination must have its managed Coconut service running.')
    supported(safe(root / '.runtime/host/coconut.sqlite3', root), empty=True)
    return marker, state


def read_journal(root, migration_id):
    path = safe(migration_folder(root, migration_id) / 'result.json', root)
    return json.loads(path.read_text()) if path.is_file() else None


def receive(root, migration_id, package_id, expected_hash, stream, operations=None):
    operations = operations or deploy.Operations()
    migration_id = identifier(migration_id)
    private(root / '.runtime/deploy', root)
    with locked(root / '.runtime/deploy/deploy.lock', root):
        previous = read_journal(root, migration_id)
        if previous:
            if previous.get('snapshot_sha256') != expected_hash or previous.get('package_id') != package_id:
                raise MigrationError('This migration ID already belongs to another snapshot.')
            return previous
        marker, initial = target_preflight(root, package_id, operations)
        folder = private(migration_folder(root, migration_id), root)
        candidate = folder / 'incoming.sqlite3'
        assert_payload(stream, candidate, expected_hash)
        expected = metadata(candidate)
        data = safe(root / '.runtime/host', root)
        validate_files(root, data, folder, empty=True)
        record = dict(version=1, migration_id=migration_id, package_id=package_id,
                      snapshot_sha256=expected_hash, phase='prepared', state=initial, takeover=False)
        journal = folder / 'result.json'
        deploy.write_json(journal, record)
        stop_attempted = swap_attempted = False
        try:
            record['phase'] = 'stopping'
            deploy.write_json(journal, record)
            stop_attempted = True
            actual = operations.stop(root)
            if actual != initial:
                raise MigrationError('The destination service changed during transfer.')
            with locked(data / 'service.lock', root):
                supported(data / 'coconut.sqlite3', empty=True)
                validate_files(root, data, folder, empty=True)
                snapshot(data / 'coconut.sqlite3', folder / 'blank.sqlite3')
                record['phase'] = 'replacing'
                deploy.write_json(journal, record)
                for suffix in ('-wal', '-shm'):
                    safe(data / ('coconut.sqlite3' + suffix), root).unlink(missing_ok=True)
                swap_attempted = True
                os.replace(candidate, data / 'coconut.sqlite3')
                deploy.sync_directory(data)
                if metadata(data / 'coconut.sqlite3') != expected:
                    raise MigrationError('The installed snapshot does not match its source.')
                record.update(phase='starting', takeover=True)
                deploy.write_json(journal, record)
            # Starting can dispatch work. No automatic rollback is safe after this point.
            operations.restore(root, initial)
            health = operations.health(root, marker['address'])
            observed = verify_started(data / 'coconut.sqlite3', expected)
            record.update(phase='complete', health=health, source_metadata=expected, metadata=observed)
            deploy.write_json(journal, record)
            return record
        except BaseException:
            if not record['takeover']:
                if swap_attempted:
                    with locked(data / 'service.lock', root):
                        for suffix in ('-wal', '-shm'):
                            safe(data / ('coconut.sqlite3' + suffix), root).unlink(missing_ok=True)
                        deploy.copy_source(folder / 'blank.sqlite3', data / 'coconut.sqlite3')
                        if metadata(data / 'coconut.sqlite3') != metadata(folder / 'blank.sqlite3'):
                            raise MigrationError('The blank destination could not restore. Both hosts remain stopped.')
                if stop_attempted:
                    operations.restore(root, initial)
                record['phase'] = 'restored'
                deploy.write_json(journal, record)
                return record
            raise


def remote_call(config, operation, migration_id, package_id='', snapshot_hash='', input_file=None):
    program = remote.ROOT_CODE + """
command = [str(root / '.runtime/hermes/venv/bin/python'), str(root / 'scripts/migrate.py'), sys.argv[2], '--id', sys.argv[3]]
if sys.argv[2] in ('receive', 'preflight'):
    command += ['--package-id', sys.argv[4]]
if sys.argv[2] == 'receive':
    command += ['--sha256', sys.argv[5]]
os.execv(command[0], command)
"""
    result = remote.ssh(config, ['python3', '-c', program, config['directory'], operation, migration_id,
                                 package_id, snapshot_hash], input_file=input_file, timeout=120, allow_failure=True)
    lines = [line.split('=', 1)[1] for line in result.stdout.decode(errors='replace').splitlines()
             if line.startswith('COCONUT_MIGRATE_RESULT=')]
    if result.returncode or len(lines) != 1:
        raise MigrationError('The remote transfer result is unknown. The source remains stopped until status confirms it.')
    return json.loads(lines[0])


def source_restore(state, folder):
    saved = folder / 'source-agent.plist'
    if saved.exists():
        original = host.owned_agent()[0]
        if original.exists() or original.is_symlink():
            raise MigrationError('The source login service path changed. Restore it manually from the migration backup.')
        os.replace(saved, original)
    host.restore_after_update(state)


def transfer():
    config = remote.configuration()
    hashes = {name: deploy.sha256(content) for name, content in remote.source_files().items()}
    package_id = deploy.sha256(deploy.canonical(hashes))
    migration_id = str(uuid4())
    base = private(ROOT / '.runtime/migrate', ROOT)
    with locked(base / 'source.lock', ROOT):
        outgoing = safe(base / 'outgoing.json', ROOT)
        if outgoing.exists():
            raise MigrationError('An outgoing migration already exists. Run migrate.py status before another transfer.')
        remote_call(config, 'preflight', migration_id, package_id)
        folder = private(migration_folder(ROOT, migration_id), ROOT)
        state = None
        dispatched = False
        record = dict(version=1, migration_id=migration_id, package_id=package_id, phase='preparing', remote=config)
        try:
            state = host.stop_for_update()
            record['source_state'] = state
            deploy.write_json(outgoing, record)
            agent, loaded = host.owned_agent()
            if loaded:
                raise MigrationError('The source login service did not stop.')
            if agent.exists():
                os.replace(agent, folder / 'source-agent.plist')
            with host.stopped_service(host.DEFAULT_DATA, 'transferring its data'):
                source = safe(host.DEFAULT_DATA / 'coconut.sqlite3', ROOT)
                supported(source)
                validate_files(ROOT, host.DEFAULT_DATA, folder)
                candidate = folder / 'snapshot.sqlite3'
                snapshot(source, candidate)
                revoke_devices(candidate)
                record['metadata'] = metadata(candidate)
                record['snapshot_sha256'] = deploy.sha256(candidate.read_bytes())
                record['phase'] = 'dispatched'
                deploy.write_json(outgoing, record)
                deploy.write_json(safe(host.DEFAULT_DATA / 'moved-to.json', ROOT),
                                  {'address': config['address'], 'migration_id': migration_id})
                dispatched = True
                with candidate.open('rb') as handle:
                    receipt = remote_call(config, 'receive', migration_id, package_id, record['snapshot_sha256'], handle)
                if any(receipt.get(key) != record[key] for key in ('migration_id', 'package_id', 'snapshot_sha256')):
                    raise MigrationError('The destination receipt belongs to another snapshot.')
                if receipt.get('phase') != 'complete':
                    if receipt.get('phase') == 'restored' and not receipt.get('takeover'):
                        dispatched = False
                    raise MigrationError('The destination has not confirmed takeover. Inspect migrate.py status.')
                if receipt.get('source_metadata') != record['metadata']:
                    raise MigrationError('The destination receipt differs from the source snapshot.')
                record['phase'] = 'complete'
                deploy.write_json(outgoing, record)
            remote.verify_remote_health(config)
            return {'ok': True, 'migration_id': migration_id, 'source_stopped': True,
                    'profiles': len(record['metadata']['profiles']), 'tasks': len(record['metadata']['tasks']),
                    'pending_decisions': len(record['metadata']['decisions'])}
        except BaseException:
            if state and not dispatched:
                safe(host.DEFAULT_DATA / 'moved-to.json', ROOT).unlink(missing_ok=True)
                source_restore(state, folder)
                record['phase'] = 'source_restored'
                deploy.write_json(folder / 'source-result.json', record)
                outgoing.unlink(missing_ok=True)
            raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('transfer')
    commands.add_parser('status')
    for name in ('receive', 'preflight', 'receipt'):
        sub = commands.add_parser(name)
        sub.add_argument('--id', required=True)
        if name != 'receipt':
            sub.add_argument('--package-id', required=True)
        if name == 'receive':
            sub.add_argument('--sha256', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'transfer':
            print(json.dumps(transfer(), indent=2))
        elif args.command == 'status':
            path = safe(ROOT / '.runtime/migrate/outgoing.json', ROOT)
            if not path.exists():
                print(json.dumps({'outgoing_migration': False}))
            else:
                record = json.loads(path.read_text())
                receipt = remote_call(remote.validate_config(record['remote']), 'receipt', record['migration_id'])
                print(json.dumps({'migration_id': record['migration_id'], 'source_phase': record['phase'],
                                  'destination_phase': receipt.get('phase'), 'takeover': receipt.get('takeover')}, indent=2))
        else:
            identifier(args.id)
            if args.command == 'receive':
                result = receive(ROOT, args.id, args.package_id, args.sha256, sys.stdin.buffer)
            elif args.command == 'preflight':
                target_preflight(ROOT, args.package_id, deploy.Operations())
                result = {'ready': True}
            else:
                result = read_journal(ROOT, args.id) or {'phase': 'absent', 'takeover': False}
            print('COCONUT_MIGRATE_RESULT=' + json.dumps(result, sort_keys=True))
    except (MigrationError, deploy.DeployError) as error:
        raise SystemExit(str(error)) from None
    except (OSError, sqlite3.Error, ValueError):
        raise SystemExit('The transfer did not complete. Source data remains intact. Run migrate.py status before starting either host.') from None


if __name__ == '__main__':
    main()
