"""Authenticated recovery through the real HTTP, lifecycle and Hermes kernel.

All databases, profiles, worktrees, HTTP servers and model responses are local
disposable fixtures. No installed service state or model credentials are used.
"""
import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider, HermesProvider
from coconut.server import APIServer
from coconut.service import Service, ServiceError
from coconut.work_workspace import acquire, git, preferred
from coconut.workspace_recovery import atomic_manifest
import coconut.workspace_recovery as recovery
import test_native_kanban as native
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class CheckoutFilterPreflightTests(unittest.TestCase):
    """Exercise recovery's real Git preflight without a native task database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        git(self.repo, 'init')
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'retained.txt').write_text('retained\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'base')
        self.attached = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'switch', '-c', 'saved')
        (self.repo / 'new.txt').write_text('saved bytes\n')
        git(self.repo, 'add', 'new.txt')
        git(self.repo, 'commit', '-m', 'saved file')
        self.saved = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'switch', '-c', 'attached', self.attached)
        self.marker = self.repo / 'filter-ran'
        git(self.repo, 'config', 'filter.unsafe.smudge', 'touch filter-ran; cat')

    def test_untracked_attributes_reject_saved_only_path_before_filter_runs(self):
        from coconut.workspace_recovery import reject_custom_git_commands
        (self.repo / '.gitattributes').write_text('new.txt filter=unsafe\n')
        before = git(self.repo, 'status', '--porcelain')
        with self.assertRaisesRegex(ValueError, 'filter'):
            reject_custom_git_commands(self.repo, (self.attached, self.saved))
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.repo / 'new.txt').exists())
        self.assertEqual(git(self.repo, 'status', '--porcelain'), before)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.attached)

    def test_checkout_recheck_rejects_worktree_attributes_on_target_tree(self):
        from coconut.workspace_recovery import checkout_obstructions
        (self.repo / '.gitattributes').write_text('new.txt filter=unsafe\n')
        request = dict(path=str(self.repo), attached_head=self.attached)
        with self.assertRaisesRegex(ValueError, 'filter'):
            checkout_obstructions(request, self.saved, {})
        self.assertFalse(self.marker.exists())

    def test_unused_filter_configuration_remains_allowed(self):
        from coconut.workspace_recovery import reject_custom_git_commands
        (self.repo / '.gitattributes').write_text('unrelated.txt filter=unsafe\n')
        reject_custom_git_commands(self.repo, (self.attached, self.saved))
        self.assertFalse(self.marker.exists())


class CheckoutPreviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'service'
        self.service = Service(self.data, FixtureProvider(self.data), start_workers=False)
        self.addCleanup(self.service.close)
        self.server = APIServer(('127.0.0.1', 0), self.service)
        self.addCleanup(self.server.server_close)
        self.token = self.http('POST', '/v1/pair', {'code': self.service.pairing_code(), 'name': 'Test phone'}, auth=False)[1]['token']
        bot = self.http('POST', '/v1/bots', {'name': 'Test bot', 'role': ''})[1]
        task = self.http('POST', '/v1/bots/' + bot['id'] + '/messages', {'text': 'Original checkout'})[1]
        self.task_id = task['id']
        owner = self.service._get('tasks', self.task_id)
        self.service._ensure_execution_session(owner)
        self.owner = owner['_execution_session_id']
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init')
        (self.repo / 'tracked.txt').write_text('base\n')
        git(self.repo, 'add', 'tracked.txt')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'base')
        self.workspace = self.root / 'original'
        git(self.repo, 'worktree', 'add', '-b', 'saved', str(self.workspace))
        git(self.repo, 'switch', '-c', 'attached')
        (self.repo / 'attached.txt').write_text('attached\n')
        git(self.repo, 'add', 'attached.txt')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'attached')
        git(self.workspace, 'switch', '-c', 'attached-copy', 'attached')
        (self.workspace / 'tracked.txt').write_text('dirty original\n')
        registry = self.data / 'workspaces'
        registry.mkdir(exist_ok=True)
        binding_id = self.owner + '-' + hashlib.sha256(str(self.repo).encode()).hexdigest()[:16]
        (registry / 'worktrees.json').write_text(json.dumps({binding_id: dict(outcome=self.owner,
            repository=str(self.repo), path=str(self.workspace), branch='saved')}))
        board = self.data / 'kanban'
        board.mkdir(exist_ok=True)
        with sqlite3.connect(board / 'kanban.db') as db:
            db.execute('CREATE TABLE tasks (id TEXT, status TEXT, tenant TEXT, worker_pid INTEGER, current_run_id TEXT, workspace_path TEXT)')
            db.execute('CREATE TABLE coconut_bindings (external_id TEXT, native_id TEXT)')
            db.execute('INSERT INTO tasks VALUES (?, ?, ?, NULL, NULL, ?)', ('native', 'archived', bot['id'], str(self.workspace)))
            db.execute('INSERT INTO coconut_bindings VALUES (?, ?)', (self.task_id, 'native'))
        owner.update(status='cancelled', _native_outcome_id='native', _work_context_id='work')
        self.service._put('tasks', owner)
        self.service._put('artifacts', dict(id='work', kind='work', task_id=self.task_id, bot_id=bot['id'],
            _native_outcome_id='native', status='cancelled', title='Original checkout', created_at=owner['created_at']))
        self.service.db.execute('INSERT INTO settings(key,value) VALUES (?, ?)', ('work.cancelled.work:work', json.dumps('first stop')))

    def http(self, method, path, data=None, *, auth=True, key=None):
        thread = threading.Thread(target=self.server.handle_request)
        thread.start()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': key or str(uuid4())}
        if auth and getattr(self, 'token', None):
            headers['Authorization'] = 'Bearer ' + self.token
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
            thread.join(20)

    def test_repeated_stop_route_invalidates_draining_consent_but_not_idempotent_retry(self):
        endpoint = '/v1/work/items/work/cancel'
        original = self.service._setting('work.cancelled.work:work')
        stop_key = str(uuid4())
        status, response = self.http('POST', endpoint, {}, key=stop_key)
        self.assertEqual(status, 200, response)
        self.assertIs(response['artifacts'][0]['checkout_recovery_stopped'], True)
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == 'work')
        self.assertIs(row['checkout_recovery_stopped'], True)
        self.assertNotIn('checkout_recovery_stopped', self.service._get('artifacts', 'work'))
        first = self.service._setting('work.cancelled.work:work')
        self.assertNotEqual(first, original)
        _, preview = self.http('GET', '/v1/tasks/' + self.task_id + '/checkout-recovery')
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        scope = self.service._checkout_scope(request)
        receipt = dict(request=request, device_id=self.service.authenticate(self.token),
                       dirty=recovery.dirty_state(self.workspace, read_only=True), cancellation=first)
        self.service._check_checkout_consent(receipt, scope)
        self.assertEqual(self.http('POST', endpoint, {}, key=stop_key), (200, response))
        self.assertEqual(self.service._setting('work.cancelled.work:work'), first)
        self.service._check_checkout_consent(receipt, scope)
        self.assertEqual(self.http('POST', endpoint, {}, key=str(uuid4()))[0], 200)
        self.assertNotEqual(self.service._setting('work.cancelled.work:work'), first)
        with self.assertRaisesRegex(ServiceError, 'stop changed'):
            self.service._check_checkout_consent(receipt, scope)
        self.assertEqual(self.http('POST', '/v1/maintenance/checkout-recovery', request,
                                   key=preview['key'])[0], 409)

    def test_task_stop_does_not_grant_work_stop_recovery_authority(self):
        self.service.db.execute('DELETE FROM settings WHERE key=?', ('work.cancelled.work:work',))
        task = self.service._get('tasks', self.task_id)
        task['status'] = 'queued'
        self.service._put('tasks', task)
        work = self.service._get('artifacts', 'work')
        work['status'] = 'waiting'
        self.service._put('artifacts', work)
        status, stopped = self.http('POST', '/v1/tasks/' + self.task_id + '/cancel', {})
        self.assertEqual(status, 200, stopped)
        self.assertEqual(self.service._get('tasks', self.task_id)['status'], 'cancelled')
        self.assertIsNone(self.service._setting('work.cancelled.work:work'))
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == 'work')
        self.assertNotIn('checkout_recovery_stopped', row)
        self.assertEqual(self.http('GET', '/v1/tasks/' + self.task_id + '/checkout-recovery')[0], 409)

    def test_clearing_work_stop_marker_removes_projected_recovery_flag(self):
        before = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == 'work')
        self.assertIs(before['checkout_recovery_stopped'], True)
        self.service.db.execute('DELETE FROM settings WHERE key=?', ('work.cancelled.work:work',))
        after = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == 'work')
        self.assertNotIn('checkout_recovery_stopped', after)
        self.assertNotIn('checkout_recovery_stopped', self.service._work_projection([dict(before)])[0])
        self.assertEqual(self.http('GET', '/v1/tasks/' + self.task_id + '/checkout-recovery')[0], 409)

    def test_preview_is_paired_read_only_and_stale_consent_fails(self):
        path = '/v1/tasks/' + self.task_id + '/checkout-recovery'
        self.assertEqual(self.http('GET', path, auth=False)[0], 401)
        before = (git(self.repo, 'show-ref'), git(self.workspace, 'status', '--porcelain'),
                  (self.data / 'workspaces' / 'worktrees.json').read_bytes(), (self.workspace / 'tracked.txt').read_bytes())
        original = recovery.checkout_git
        calls = []
        def checked(*args, **kwargs):
            calls.append(args[1:])
            if any(command in args[1:] for command in ('merge-tree', 'commit-tree', 'update-ref', 'switch')):
                self.fail('Discovery tried to plan or mutate Git.')
            return original(*args, **kwargs)
        with patch.object(recovery, 'checkout_git', checked), patch.object(recovery, 'checkout_plan', side_effect=AssertionError('planned')):
            status, preview = self.http('GET', path)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview['request']['expected_owner'], self.owner)
        self.assertEqual(preview['request']['attached_branch'], 'attached-copy')
        self.assertEqual(preview['dirty_count'], 1)
        self.assertTrue(calls)
        self.assertEqual((git(self.repo, 'show-ref'), git(self.workspace, 'status', '--porcelain'),
                          (self.data / 'workspaces' / 'worktrees.json').read_bytes(),
                          (self.workspace / 'tracked.txt').read_bytes()), before)
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        self.assertEqual(self.http('POST', '/v1/maintenance/checkout-recovery', request, auth=False)[0], 401)
        self.assertEqual(self.http('POST', '/v1/maintenance/checkout-recovery', preview['request'])[0], 409)
        self.service.db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps('second stop'), 'work.cancelled.work:work'))
        scope = self.service._checkout_scope(request)
        with self.assertRaisesRegex(ServiceError, 'Confirm'):
            self.service._checkout_cancellation(request, scope, self.service.authenticate(self.token),
                                                recovery.dirty_state(self.workspace, read_only=True))
        self.service.db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps('first stop'), 'work.cancelled.work:work'))
        changed = dict(request, saved_head='0' * len(request['saved_head']))
        with self.assertRaisesRegex(ServiceError, 'Confirm'):
            self.service._checkout_cancellation(changed, scope, self.service.authenticate(self.token),
                                                recovery.dirty_state(self.workspace, read_only=True))
        (self.workspace / 'tracked.txt').write_text('changed edit\n')
        with self.assertRaisesRegex(ServiceError, 'Confirm'):
            self.service._checkout_cancellation(request, scope, self.service.authenticate(self.token),
                                                recovery.dirty_state(self.workspace, read_only=True))
        self.assertEqual(self.service._all('workspace_recoveries'), [])

    def test_preview_rejects_other_owner_and_pending_decision(self):
        path = '/v1/tasks/' + self.task_id + '/checkout-recovery'
        decision = dict(id=str(uuid4()), task_id=self.task_id, status='pending')
        self.service._put('decisions', decision)
        self.assertEqual(self.http('GET', path)[0], 409)
        self.service.db.execute('DELETE FROM decisions WHERE id=?', (decision['id'],))
        task = self.service._get('tasks', self.task_id)
        task['_native_outcome_id'] = 'foreign'
        self.service._put('tasks', task)
        self.assertEqual(self.http('GET', path)[0], 409)
        self.assertEqual(self.service._all('workspace_recoveries'), [])

    def test_paired_post_rejects_missing_and_stale_consent_before_git_planning(self):
        _, preview = self.http('GET', '/v1/tasks/' + self.task_id + '/checkout-recovery')
        endpoint = '/v1/maintenance/checkout-recovery'
        self.service.restart_enabled = True
        self.service.kanban = SimpleNamespace(close=lambda: None, tasks={})
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        def post(body, **kwargs):
            return self.http('POST', endpoint, body, key=preview['key'], **kwargs)
        with patch.object(recovery, 'checkout_plan', side_effect=AssertionError('Git planning reached')) as planner:
            wrong_status, wrong_response = self.http('POST', endpoint, request, key=str(uuid4()))
            self.assertEqual(wrong_status, 409, wrong_response)
            self.assertIn('recovery branch must be unique', json.dumps(wrong_response))
            self.assertEqual(post(preview['request'])[0], 409)
            self.assertEqual(post(dict(preview['request'], resume_consent=preview['resume_consent']))[0], 400)
            self.assertEqual(post(request, auth=False)[0], 401)
            self.assertEqual(post(dict(request, expected_owner='foreign'))[0], 409)
            self.assertEqual(post(dict(request, attached_head='0' * len(request['attached_head'])))[0], 409)
            self.service.db.execute('UPDATE settings SET value=? WHERE key=?',
                                    (json.dumps('another phone stop'), 'work.cancelled.work:work'))
            self.assertEqual(post(request)[0], 409)
            self.service.db.execute('UPDATE settings SET value=? WHERE key=?',
                                    (json.dumps('first stop'), 'work.cancelled.work:work'))
            (self.workspace / 'tracked.txt').write_text('new dirty bytes\n')
            self.assertEqual(post(request)[0], 409)
            (self.workspace / 'tracked.txt').write_text('dirty original\n')
            decision = dict(id=str(uuid4()), task_id=self.task_id, status='pending')
            self.service._put('decisions', decision)
            self.assertEqual(post(request)[0], 409)
            self.service.db.execute('DELETE FROM decisions WHERE id=?', (decision['id'],))
            planner.assert_not_called()
            with self.assertRaisesRegex(AssertionError, 'Git planning reached'):
                self.service.request_checkout_recovery(request, preview['key'], self.token)
        self.assertEqual(self.service._all('workspace_recoveries'), [])

    def held_checkout(self):
        git(self.repo, 'switch', 'saved')
        (self.repo / 'saved.txt').write_text('saved-only change\n')
        git(self.repo, 'add', 'saved.txt')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-m', 'divergent saved branch')
        self.service.restart_enabled = True
        self.service.kanban = SimpleNamespace(close=lambda: None, tasks={})
        path = '/v1/tasks/' + self.task_id + '/checkout-recovery'
        status, preview = self.http('GET', path)
        self.assertEqual(status, 200, preview)
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        status, draining = self.http('POST', '/v1/maintenance/checkout-recovery', request, key=preview['key'])
        self.assertEqual((status, draining['state']), (200, 'draining'), draining)
        receipt = self.service._get('workspace_recoveries', preview['key'])
        return path, preview, receipt

    def test_paired_checkout_refuses_edits_changed_between_consent_and_plan(self):
        git(self.repo, 'switch', 'saved')
        (self.repo / 'saved.txt').write_text('saved-only change\n')
        git(self.repo, 'add', 'saved.txt')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-m', 'divergent saved branch')
        self.service.restart_enabled = True
        self.service.kanban = SimpleNamespace(close=lambda: None, tasks={})
        endpoint = '/v1/maintenance/checkout-recovery'
        status, preview = self.http('GET', '/v1/tasks/' + self.task_id + '/checkout-recovery')
        self.assertEqual(status, 200, preview)
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        original_plan = recovery.checkout_plan
        before_head = git(self.workspace, 'rev-parse', 'HEAD')
        before_branch = git(self.workspace, 'branch', '--show-current')

        def edit_before_plan(planned_request):
            (self.workspace / 'tracked.txt').write_text('changed after verification\n')
            return original_plan(planned_request)

        with patch.object(recovery, 'checkout_plan', side_effect=edit_before_plan) as planner:
            status, response = self.http('POST', endpoint, request, key=preview['key'])
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(status, 409, response)
        self.assertIn('tracked edits changed', json.dumps(response))
        self.assertEqual(self.service._all('workspace_recoveries'), [])
        self.assertFalse(self.service.restart_pending)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), before_branch)
        self.assertEqual(git(self.workspace, 'rev-parse', 'HEAD'), before_head)
        self.assertEqual((self.workspace / 'tracked.txt').read_text(), 'changed after verification\n')

        (self.workspace / 'tracked.txt').write_text('dirty original\n')
        status, unchanged = self.http('POST', endpoint, request, key=preview['key'])
        self.assertEqual(status, 200, unchanged)
        self.assertEqual(unchanged['state'], 'draining')
        self.assertEqual(self.service._get('workspace_recoveries', preview['key'])['dirty'],
                         recovery.dirty_state(self.workspace, read_only=True))
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), before_branch)

    def test_held_preview_rejects_partially_switched_checkout(self):
        path, preview, receipt = self.held_checkout()
        receipt['state'] = 'held'
        self.service._put('workspace_recoveries', receipt)
        git(self.repo, 'branch', preview['request']['recovery_branch'], receipt['merge_commit'])
        status, response = self.http('GET', path)
        self.assertEqual(status, 409, response)
        self.assertIn('inspect', json.dumps(response).lower())
        git(self.workspace, 'switch', '--no-overwrite-ignore', preview['request']['recovery_branch'])
        manifest = self.data / 'workspaces' / 'worktrees.json'
        bindings = json.loads(manifest.read_text())
        recovered = dict(receipt['binding'], branch=preview['request']['recovery_branch'],
                         _recovery_request_id=preview['key'])
        bindings[receipt['binding_id']] = recovered
        manifest.write_text(json.dumps(bindings))
        before = (manifest.read_bytes(), git(self.repo, 'show-ref'),
                  (self.workspace / 'tracked.txt').read_bytes())
        with patch.object(recovery, 'checkout_plan', side_effect=AssertionError('planned')):
            status, response = self.http('GET', path)
        self.assertEqual(status, 409, response)
        self.assertIn('inspect', json.dumps(response).lower())
        self.assertEqual((manifest.read_bytes(), git(self.repo, 'show-ref'),
                          (self.workspace / 'tracked.txt').read_bytes()), before)
        self.assertEqual(self.service._get('workspace_recoveries', preview['key'])['state'], 'held')

    def test_held_preview_allows_original_checkout_and_rejects_drain(self):
        path, preview, receipt = self.held_checkout()
        status, response = self.http('GET', path)
        self.assertEqual(status, 409, response)
        self.assertIn('receipt', json.dumps(response).lower())
        receipt['state'] = 'held'
        self.service._put('workspace_recoveries', receipt)
        manifest = self.data / 'workspaces' / 'worktrees.json'
        before = (manifest.read_bytes(), git(self.repo, 'show-ref'),
                  (self.workspace / 'tracked.txt').read_bytes())
        with patch.object(recovery, 'checkout_plan', side_effect=AssertionError('planned')):
            status, retry = self.http('GET', path)
        self.assertEqual(status, 200, retry)
        self.assertNotEqual(retry['key'], preview['key'])
        self.assertEqual({field: value for field, value in retry['request'].items() if field != 'recovery_branch'},
                         {field: value for field, value in preview['request'].items() if field != 'recovery_branch'})
        self.assertEqual((manifest.read_bytes(), git(self.repo, 'show-ref'),
                          (self.workspace / 'tracked.txt').read_bytes()), before)
        fresh = dict(retry['request'], resume_consent=retry['resume_consent'], resume_confirmed=True)
        status, result = self.http('POST', '/v1/maintenance/checkout-recovery', fresh, key=retry['key'])
        self.assertEqual((status, result['state']), (200, 'draining'), result)
        self.assertEqual(self.service._get('workspace_recoveries', preview['key'])['state'], 'superseded')


@unittest.skipUnless(native.SOURCE and native.PYTHON, 'Set Coconut Hermes paths for native integration checks.')
class WorkspaceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'service'
        self.provider = native.Provider(self.data)
        self.service = Service(self.data, self.provider, start_workers=False)
        self.addCleanup(lambda: self.service.close())
        self.server = APIServer(('127.0.0.1', 0), self.service)
        self.addCleanup(lambda: self.server.server_close())
        self.token = None
        status, pair = self.http('/v1/pair', {'code': self.service.pairing_code(), 'name': 'Synthetic operator'})
        self.assertEqual(status, 200, pair)
        self.token = pair['token']
        self.bot = self.write('/v1/bots', {'name': 'Recovery fixture', 'role': ''})
        self.task = self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Retain the approved outcome'})
        task = self.service._get('tasks', self.task['id'])
        self.service._ensure_execution_session(task)
        self.owner = task['_execution_session_id']
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'fixture')
        with patch.object(sys, 'path', [native.SOURCE, *sys.path]):
            self.binding = acquire(self.data / 'workspaces', self.owner, str(self.repo))
        self.workspace = Path(self.binding['path'])
        (self.workspace / 'retained.patch').write_text('Original approved Gumroad patch fixture\n')
        (self.workspace / '.env').write_text('SYNTHETIC=preserved\n')
        row = self.service._kanban_row(task, task, 'ready', 10)
        self.program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        self.native_id = self.service.kanban.tasks[task['id']]['id']
        self.program('board.bind(sys.argv[2], sys.argv[3]); board.kb.block_task(board.conn, sys.argv[2], reason="Inspect branch")', self.native_id, task['id'])
        task.update(status='failed', _native_outcome_id=self.native_id)
        self.service._put('tasks', task)
        self.service.kanban.call({'op': 'snapshot'})
        git(self.workspace, 'switch', '-c', 'reviewed-local-branch')
        self.manifest = self.data / 'workspaces' / 'worktrees.json'
        self.before = self.manifest.read_bytes()
        self.payload = dict(task_id=task['id'], native_task_id=self.native_id, expected_owner=self.owner,
            repository=str(self.repo), path=str(self.workspace), old_branch=self.binding['branch'], new_branch='reviewed-local-branch')
        self.key = str(uuid4())
        self.service.restart_enabled = True

    def http(self, path, data, *, key=None, token=True):
        thread = threading.Thread(target=self.server.handle_request)
        thread.start()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': key or str(uuid4())}
        if token and self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        try:
            connection.request('POST', path, json.dumps(data), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
            thread.join(20)
            self.assertFalse(thread.is_alive())

    def write(self, path, data):
        status, result = self.http(path, data)
        self.assertEqual(status, 200, result)
        return result

    def request(self, **kwargs):
        return self.http('/v1/maintenance/workspace-recovery', kwargs.pop('data', self.payload), key=kwargs.pop('key', self.key), **kwargs)

    def program(self, program, *args):
        result = subprocess.run([native.PYTHON, '-c', 'import sys\nfrom coconut.kanban_runtime import Board\nboard=Board(sys.argv[1])\n' + program,
            str(self.data / 'kanban'), *args], env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
            PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[1]), native.SOURCE))),
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def restart(self, provider=None):
        self.server.server_close()
        self.service.close()
        self.provider = provider or native.Provider(self.data)
        self.service = Service(self.data, self.provider, start_workers=False)
        self.service.restart_enabled = True
        self.server = APIServer(('127.0.0.1', 0), self.service)

    def receipt(self):
        return self.service._get('workspace_recoveries', self.key)

    def checkout_incident(self, *, stopped=False):
        saved = 'wt/1ae0ae9f-35c9-4813-b7d0-69188eeb1b97-11ce49fd843261dc'
        git(self.repo, 'branch', '-m', self.binding['branch'], saved)
        registry = json.loads(self.manifest.read_text())
        next(iter(registry.values()))['branch'] = saved
        self.manifest.write_text(json.dumps(registry))
        self.binding['branch'] = saved
        for name in ('checkout-a.txt', 'checkout-b.txt', 'checkout-c.txt'):
            (self.workspace / name).write_text('base\n')
        git(self.workspace, 'add', 'checkout-a.txt', 'checkout-b.txt', 'checkout-c.txt')
        git(self.workspace, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-m', 'common tracked base')
        git(self.repo, 'branch', '-f', saved, 'reviewed-local-branch')
        attached = 'gianfranco/checkout-confirmation-wording'
        git(self.workspace, 'switch', '-c', attached)
        for index in range(7):
            (self.workspace / f'attached-{index}.txt').write_text(f'attached {index}\n')
            git(self.workspace, 'add', f'attached-{index}.txt')
            git(self.workspace, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                'commit', '-m', f'attached {index}')
        git(self.repo, 'switch', saved)
        for index in range(3):
            (self.repo / f'saved-{index}.txt').write_text(f'saved {index}\n')
            git(self.repo, 'add', f'saved-{index}.txt')
            git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                'commit', '-m', f'saved {index}')
        dirty = {}
        for name in ('checkout-a.txt', 'checkout-b.txt', 'checkout-c.txt'):
            dirty[name] = (name + ': original dirty edit\n').encode()
            (self.workspace / name).write_bytes(dirty[name])
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        if not stopped:
            self.program('board.kb.archive_task(board.conn, sys.argv[2])', self.native_id)
        artifact = dict(id=str(uuid4()), kind='work', task_id=task['id'], bot_id=task['bot_id'],
            _native_outcome_id=self.native_id, status='waiting' if stopped else 'completed', title='Original Work', created_at=task['created_at'],
            _completed_history=dict(report='original-result', evidence='Original verified result'))
        self.service._put('artifacts', artifact)
        self.service.kanban.call({'op': 'snapshot'})
        payload = dict(task_id=task['id'], native_task_id=self.native_id, expected_owner=self.owner,
            repository=str(self.repo), path=str(self.workspace), saved_branch=saved, attached_branch=attached,
            saved_head=git(self.repo, 'rev-parse', saved), attached_head=git(self.workspace, 'rev-parse', 'HEAD'),
            recovery_branch='wt/recovered-' + self.key, message='Resume the original archived Work without approval.')
        return payload, dirty, artifact

    def test_phone_stopped_archived_checkout_needs_fresh_confirmation(self):
        payload, dirty, artifact = self.checkout_incident(stopped=True)
        self.assertEqual(self.http('/v1/work/items/' + artifact['id'] + '/cancel', {})[0], 200)
        self.program('board.kb.archive_task(board.conn, sys.argv[2])', self.native_id)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'cancelled')
        original = self.service._get('tasks', self.task['id'])
        refs = {branch: git(self.repo, 'rev-parse', branch) for branch in (payload['saved_branch'], payload['attached_branch'])}
        self.assertIsNotNone(self.service._setting('work.cancelled.work:' + artifact['id']))
        status, preview = self.http_get('/v1/tasks/' + self.task['id'] + '/checkout-recovery')
        self.assertEqual(status, 200, preview)
        self.key = preview['key']
        self.assertEqual(preview['request']['task_id'], payload['task_id'])
        self.assertEqual(preview['request']['saved_head'], payload['saved_head'])
        self.assertEqual(preview['dirty_count'], 3)
        self.assertEqual(self.http_get('/v1/tasks/' + self.task['id'] + '/checkout-recovery', token=False)[0], 401)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'cancelled')
        self.assertEqual(self.service._get('tasks', self.task['id']), original)
        self.assertEqual({branch: git(self.repo, 'rev-parse', branch) for branch in refs}, refs)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', preview['request'], key=self.key)[0], 409)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery',
            dict(preview['request'], resume_consent=preview['resume_consent']), key=self.key)[0], 400)
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', request, key=self.key, token=False)[0], 401)
        status, response = self.http('/v1/maintenance/checkout-recovery', request, key=self.key)
        self.assertEqual(status, 200, response)
        self.assertIsNotNone(self.service._setting('work.cancelled.work:' + artifact['id']))
        self.assertTrue(self.service.take_service_restart())
        self.restart()
        receipt = self.receipt()
        self.assertEqual(receipt['state'], 'applied', receipt)
        self.assertEqual(self.service._setting('work.cancelled.work:' + artifact['id']), None)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'cancelled')
        self.assertEqual(self.service._get('artifacts', artifact['id'])['task_id'], receipt['successor_task_id'])
        self.assertEqual(self.service._get('tasks', receipt['successor_task_id'])['_execution_session_id'], self.owner)
        self.assertEqual(preferred(self.data / 'workspaces', self.owner), self.workspace)
        for name, content in dirty.items():
            self.assertEqual((self.workspace / name).read_bytes(), content)
        self.service._dispatch_pending()
        successor_native = self.service.kanban.tasks[receipt['successor_task_id']]['id']
        self.assertNotEqual(successor_native, self.native_id)
        self.assertEqual(self.program('print(board.kb.get_task(board.conn, sys.argv[2]).status)', self.native_id), 'archived')
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', request, key=self.key)[1]['state'], 'applied')

    def test_phone_checkout_rejects_other_owner_and_changed_consent(self):
        payload, dirty, artifact = self.checkout_incident(stopped=True)
        self.assertEqual(self.http('/v1/work/items/' + artifact['id'] + '/cancel', {})[0], 200)
        self.program('board.kb.archive_task(board.conn, sys.argv[2])', self.native_id)
        self.service.kanban.call({'op': 'snapshot'})
        _, preview = self.http_get('/v1/tasks/' + self.task['id'] + '/checkout-recovery')
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        changed = dict(request, expected_owner='foreign-owner')
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', changed, key=preview['key'])[0], 409)
        changed = dict(request, saved_head='0' * len(payload['saved_head']))
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', changed, key=preview['key'])[0], 409)
        (self.workspace / 'checkout-a.txt').write_text('changed after preview\n')
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', request, key=preview['key'])[0], 409)
        self.assertEqual(self.service._all('workspace_recoveries'), [])
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])

    def test_phone_checkout_rejects_new_stop_and_pending_approval(self):
        payload, dirty, artifact = self.checkout_incident(stopped=True)
        self.assertEqual(self.http('/v1/work/items/' + artifact['id'] + '/cancel', {})[0], 200)
        self.program('board.kb.archive_task(board.conn, sys.argv[2])', self.native_id)
        self.service.kanban.call({'op': 'snapshot'})
        _, preview = self.http_get('/v1/tasks/' + self.task['id'] + '/checkout-recovery')
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        self.service.db.execute('UPDATE settings SET value=? WHERE key=?',
            (json.dumps('new cancellation generation'), 'work.cancelled.work:' + artifact['id']))
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', request, key=preview['key'])[0], 409)
        _, preview = self.http_get('/v1/tasks/' + self.task['id'] + '/checkout-recovery')
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        decision = dict(id=str(uuid4()), task_id=self.task['id'], status='pending')
        self.service._put('decisions', decision)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', request, key=preview['key'])[0], 409)
        self.assertEqual(self.http_get('/v1/tasks/' + self.task['id'] + '/checkout-recovery')[0], 409)
        self.assertEqual(self.service._all('workspace_recoveries'), [])

    def test_phone_checkout_replays_crash_before_receipt_without_duplicate_successor(self):
        payload, dirty, artifact = self.checkout_incident(stopped=True)
        self.assertEqual(self.http('/v1/work/items/' + artifact['id'] + '/cancel', {})[0], 200)
        self.program('board.kb.archive_task(board.conn, sys.argv[2])', self.native_id)
        self.service.kanban.call({'op': 'snapshot'})
        _, preview = self.http_get('/v1/tasks/' + self.task['id'] + '/checkout-recovery')
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', request, key=preview['key'])[0], 200)
        self.server.server_close()
        self.service.close()
        original_reply = Service._reply
        def crash(service, task, data):
            original_reply(service, task, data)
            raise SystemExit('synthetic crash after successor before commit')
        with patch.object(Service, '_reply', crash), self.assertRaises(SystemExit):
            Service(self.data, native.Provider(self.data), start_workers=False)
        self.service = Service(self.data, native.Provider(self.data), start_workers=False)
        self.provider = self.service.provider
        self.service.restart_enabled = True
        self.server = APIServer(('127.0.0.1', 0), self.service)
        receipt = self.service._get('workspace_recoveries', preview['key'])
        self.assertEqual(receipt['state'], 'applied', receipt)
        self.assertEqual(len([task for task in self.service._all('tasks')
                              if task.get('_work_context_id') == artifact['id'] and task['id'] != self.task['id']]), 1)
        self.assertEqual(self.service._setting('work.cancelled.work:' + artifact['id']), None)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', request, key=preview['key'])[1]['state'], 'applied')

    def test_phone_checkout_new_stop_during_drain_holds_before_git_write(self):
        payload, dirty, artifact = self.checkout_incident(stopped=True)
        stop_path = '/v1/work/items/' + artifact['id'] + '/cancel'
        first_key = str(uuid4())
        self.assertEqual(self.http(stop_path, {}, key=first_key)[0], 200)
        self.program('board.kb.archive_task(board.conn, sys.argv[2])', self.native_id)
        self.service.kanban.call({'op': 'snapshot'})
        _, preview = self.http_get('/v1/tasks/' + self.task['id'] + '/checkout-recovery')
        request = dict(preview['request'], resume_consent=preview['resume_consent'], resume_confirmed=True)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', request, key=preview['key'])[0], 200)
        first = self.service._setting('work.cancelled.work:' + artifact['id'])
        self.assertEqual(self.http(stop_path, {}, key=first_key)[0], 200)
        self.assertEqual(self.service._setting('work.cancelled.work:' + artifact['id']), first)
        self.assertEqual(self.http(stop_path, {}, key=str(uuid4()))[0], 200)
        self.assertNotEqual(self.service._setting('work.cancelled.work:' + artifact['id']), first)
        self.restart()
        receipt = self.service._get('workspace_recoveries', preview['key'])
        self.assertEqual(receipt['state'], 'held', receipt)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual(self.service._get('artifacts', artifact['id'])['task_id'], self.task['id'])
        self.assertIsNotNone(self.service._setting('work.cancelled.work:' + artifact['id']))

    def http_get(self, path, *, token=True):
        thread = threading.Thread(target=self.server.handle_request)
        thread.start()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        try:
            connection.request('GET', path, headers={'Authorization': 'Bearer ' + self.token} if token else {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
            thread.join(20)

    def test_checkout_recovery_preserves_divergence_and_admits_native_successor(self):
        payload, dirty, artifact = self.checkout_incident()
        original = self.service._get('tasks', self.task['id'])
        self.assertEqual(git(self.workspace, 'rev-list', '--count', payload['saved_head'] + '..' + payload['attached_head']), '7')
        self.assertEqual(git(self.workspace, 'rev-list', '--count', payload['attached_head'] + '..' + payload['saved_head']), '3')
        before = {name: git(self.repo, 'rev-parse', branch) for name, branch in
                  (('saved', payload['saved_branch']), ('attached', payload['attached_branch']))}
        status, response = self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)
        self.assertEqual(status, 200, response)
        self.assertEqual(response['state'], 'draining')
        self.assertTrue(self.service.take_service_restart())
        self.restart()
        receipt = self.receipt()
        self.assertEqual(receipt['state'], 'applied', receipt)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['recovery_branch'])
        self.assertEqual({name: git(self.repo, 'rev-parse', branch) for name, branch in
                          (('saved', payload['saved_branch']), ('attached', payload['attached_branch']))}, before)
        self.assertEqual(git(self.workspace, 'rev-list', '--parents', '-n', '1', 'HEAD').split()[1:],
                         [payload['attached_head'], payload['saved_head']])
        for name, content in dirty.items():
            self.assertEqual((self.workspace / name).read_bytes(), content)
        self.assertEqual((self.workspace / 'retained.patch').read_text(), 'Original approved Gumroad patch fixture\n')
        self.assertEqual((self.workspace / '.env').read_text(), 'SYNTHETIC=preserved\n')
        self.assertEqual(self.service._get('tasks', self.task['id']), original)
        retained = self.service._get('artifacts', artifact['id'])
        self.assertEqual(retained['task_id'], receipt['successor_task_id'])
        self.assertEqual(retained['_completed_history'], artifact['_completed_history'])
        self.assertIn(self.task['id'], retained['_work_previous_task_ids'])
        self.assertEqual(self.service._get('tasks', receipt['successor_task_id'])['_execution_session_id'], self.owner)
        self.assertEqual(preferred(self.data / 'workspaces', self.owner), self.workspace)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[1]['state'], 'applied')
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[1]['successor_task_id'],
                         receipt['successor_task_id'])
        self.service._dispatch_pending()
        self.assertEqual(self.program('print(board.kb.get_task(board.conn, sys.argv[2]).status)', self.native_id), 'archived')
        self.assertNotEqual(self.service.kanban.tasks[receipt['successor_task_id']]['id'], self.native_id)

    def test_checkout_recovery_refuses_active_no_checkout_bisect(self):
        payload, dirty, artifact = self.checkout_incident()
        bisect_log = Path(git(self.workspace, 'rev-parse', '--git-path', 'BISECT_LOG'))
        bisect_log.write_text('git bisect start --no-checkout\n')
        before = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)

    def test_checkout_recovery_requires_two_divergent_heads(self):
        payload, dirty, artifact = self.checkout_incident()
        before = self.manifest.read_bytes()
        common = git(self.workspace, 'merge-base', payload['attached_head'], payload['saved_head'])
        # The synthetic repository has the saved branch checked out separately.
        for new_head in (payload['attached_head'], common):
            git(self.repo, 'reset', '--hard', new_head)
            payload['saved_head'] = new_head
            self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
            self.assertEqual(self.manifest.read_bytes(), before)
            self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
            self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)

    def test_checkout_recovery_runs_deferred_provider_migrations_before_workers(self):
        payload, dirty, artifact = self.checkout_incident()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.assertTrue(self.service.take_service_restart())
        calls = []

        class MigrationProvider(native.Provider):
            def migrate_model_default(self, model, base_url):
                calls.append(('model', model, base_url))

            def native_settings(self, operation, bots):
                calls.append(('native', operation, len(bots)))
                return {'routines': []}

        self.restart(MigrationProvider(self.data))
        self.assertEqual(self.receipt()['state'], 'applied')
        self.assertEqual([entry[0] for entry in calls], ['model', 'native'])
        self.assertEqual(calls[1][1], 'migrate')
        self.assertTrue(calls[1][2])
        self.assertFalse(self.service._workspace_recovery_hold)

    def test_checkout_recovery_disables_post_checkout_hook(self):
        payload, dirty, artifact = self.checkout_incident()
        hook = self.repo / '.git' / 'hooks' / 'post-checkout'
        marker = self.root / 'hook-ran'
        hook.write_text('#!/bin/sh\nprintf unexpected > ' + str(marker) + '\n')
        hook.chmod(0o755)
        reference_hook = self.repo / '.git' / 'hooks' / 'reference-transaction'
        reference_hook.write_text('#!/bin/sh\nprintf unexpected > ' + str(marker) + '\n')
        reference_hook.chmod(0o755)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.assertTrue(self.service.take_service_restart())
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied', self.receipt())
        self.assertFalse(marker.exists())
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)

    def test_checkout_recovery_refuses_target_checkout_filter(self):
        payload, dirty, artifact = self.checkout_incident()
        (self.repo / '.gitattributes').write_text('saved-0.txt filter=unsafe\n')
        git(self.repo, 'add', '.gitattributes')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-m', 'mark saved checkout filter')
        payload['saved_head'] = git(self.repo, 'rev-parse', 'HEAD')
        before = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)

    def test_checkout_recovery_rejects_archived_conversation_before_git_write(self):
        payload, dirty, artifact = self.checkout_incident()
        conversation = self.write('/v1/bots', {'name': 'Archived conversation', 'role': ''})
        task = self.service._get('tasks', self.task['id'])
        task['conversation_bot_id'] = conversation['id']
        self.service._put('tasks', task)
        bot = self.service._get('bots', conversation['id'])
        bot['archived'] = True
        self.service._put('bots', bot)
        before = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)

    def test_checkout_recovery_rejects_other_bound_worktree_of_same_execution(self):
        payload, dirty, artifact = self.checkout_incident()
        other_repo = self.root / 'other-repo'
        other_repo.mkdir()
        git(other_repo, 'init')
        git(other_repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '--allow-empty', '-m', 'other repo')
        with patch.object(sys, 'path', [native.SOURCE, *sys.path]):
            other = acquire(self.data / 'workspaces', self.owner, str(other_repo))
        with sqlite3.connect(self.data / 'kanban' / 'kanban.db') as database:
            database.execute('UPDATE tasks SET workspace_path=? WHERE id=?', (other['path'], self.native_id))
        before = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)

    def test_checkout_recovery_refuses_custom_merge_drivers_before_planning(self):
        payload, dirty, artifact = self.checkout_incident()
        marker = self.root / 'merge-driver-ran'
        git(self.repo, 'config', 'merge.unsafe.driver', 'touch ' + str(marker))
        before = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        self.assertFalse(marker.exists())
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])

    def test_checkout_recovery_refuses_filters_before_merge_planning(self):
        payload, dirty, artifact = self.checkout_incident()
        marker = self.root / 'filter-ran'
        git(self.repo, 'config', 'merge.renormalize', 'true')
        git(self.repo, 'config', 'filter.unsafe.clean', 'touch ' + str(marker))
        (self.workspace / '.gitattributes').write_text('checkout-a.txt filter=unsafe\n')
        before = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        self.assertFalse(marker.exists())
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])

    def test_checkout_recovery_rejects_retired_binding_before_git_write(self):
        payload, dirty, artifact = self.checkout_incident()
        registry = json.loads(self.manifest.read_text())
        next(iter(registry.values()))['retired'] = dict(head=payload['saved_head'], archive='retained')
        self.manifest.write_text(json.dumps(registry))
        before = self.manifest.read_bytes()
        status, response = self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)
        self.assertEqual(status, 409, response)
        self.assertIn('retired', response['error'])
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)
        self.assertEqual(git(self.repo, 'branch', '--list', payload['recovery_branch']), '')
        self.assertEqual(self.service._all('workspace_recoveries'), [])

    def test_checkout_recovery_rejects_worktree_filter_for_saved_only_file(self):
        payload, dirty, artifact = self.checkout_incident()
        marker = self.root / 'filter-executed'
        git(self.repo, 'config', 'filter.unsafe.smudge', f'touch {marker}; cat')
        (self.workspace / '.gitattributes').write_text('saved-0.txt filter=unsafe\n')
        before = self.manifest.read_bytes()
        status, response = self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)
        self.assertEqual(status, 409, response)
        self.assertIn('filter', response['error'])
        self.assertFalse(marker.exists())
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'rev-parse', 'HEAD'), payload['attached_head'])
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)
        self.assertEqual(self.service._all('workspace_recoveries'), [])

    def test_checkout_recovery_ignores_unapplied_global_filter(self):
        payload, dirty, artifact = self.checkout_incident()
        git(self.repo, 'config', 'filter.unused.clean', 'false')
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)

    def test_checkout_recovery_refuses_disabled_git_filemode(self):
        payload, dirty, artifact = self.checkout_incident()
        git(self.repo, 'config', 'core.filemode', 'false')
        before = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)

    def test_checkout_recovery_retains_unstaged_tracked_deletion(self):
        payload, dirty, artifact = self.checkout_incident()
        (self.workspace / 'checkout-a.txt').unlink()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.assertTrue(self.service.take_service_restart())
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied', self.receipt())
        self.assertFalse((self.workspace / 'checkout-a.txt').exists())
        for name in ('checkout-b.txt', 'checkout-c.txt'):
            self.assertEqual((self.workspace / name).read_bytes(), dirty[name])
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['recovery_branch'])

    def test_checkout_recovery_rejects_blank_message_and_dirty_rename_source(self):
        payload, dirty, artifact = self.checkout_incident()
        blank = dict(payload, message='  \n  ')
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', blank, key=self.key)[0], 400)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        git(self.repo, 'mv', 'checkout-a.txt', 'renamed-a.txt')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-m', 'rename tracked source')
        payload['saved_head'] = git(self.repo, 'rev-parse', payload['saved_branch'])
        status, response = self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)
        self.assertEqual(status, 409, response)
        self.assertEqual((self.workspace / 'checkout-a.txt').read_bytes(), dirty['checkout-a.txt'])
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertIsNone(self.service.db.execute('SELECT data FROM workspace_recoveries WHERE id=?',
                                                  (self.key,)).fetchone())

    def test_checkout_recovery_refuses_hidden_index_edits_and_changed_gitlink(self):
        payload, dirty, artifact = self.checkout_incident()
        git(self.workspace, 'update-index', '--assume-unchanged', 'checkout-a.txt')
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        git(self.workspace, 'update-index', '--no-assume-unchanged', 'checkout-a.txt')
        git(self.workspace, 'update-index', '--skip-worktree', 'checkout-a.txt')
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        git(self.workspace, 'update-index', '--no-skip-worktree', 'checkout-a.txt')
        git(self.repo, 'update-index', '--add', '--cacheinfo',
            '160000,' + payload['saved_head'] + ',module')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-m', 'add synthetic gitlink')
        payload['saved_head'] = git(self.repo, 'rev-parse', payload['saved_branch'])
        status, response = self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)
        self.assertEqual(status, 409, response)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertIsNone(self.service.db.execute('SELECT data FROM workspace_recoveries WHERE id=?',
                                                  (self.key,)).fetchone())

    def test_checkout_recovery_rejects_foreign_work_and_active_writer(self):
        payload, dirty, artifact = self.checkout_incident()
        device_id = self.service.db.execute('SELECT id FROM devices LIMIT 1').fetchone()[0]
        with self.assertRaises(ServiceError):
            self.service.request_checkout_recovery(payload, self.key, device_id)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key, token=False)[0], 401)
        artifact['bot_id'] = str(uuid4())
        self.service._put('artifacts', artifact)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        artifact['bot_id'] = self.bot['id']
        self.service._put('artifacts', artifact)
        checkpoint = self.data / 'bots' / self.bot['id'] / 'home' / 'processes.json'
        checkpoint.write_text(json.dumps([dict(pid=os.getpid())]))
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.assertFalse(self.service.take_service_restart())
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])

    def test_checkout_recovery_refuses_simultaneous_native_writer(self):
        payload, dirty, artifact = self.checkout_incident()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        native_id = self.program('''
identity = board.kb.create_task(board.conn, title='Unrelated native writer', tenant=sys.argv[2],
    workspace_kind='dir', workspace_path=sys.argv[3], initial_status='blocked')
board.kb.unblock_task(board.conn, identity)
board.kb.claim_task(board.conn, identity)
board.dispatch._set_worker_pid(board.conn, identity, int(sys.argv[4]))
print(identity)
''', self.bot['id'], str(self.workspace), str(os.getpid()))
        self.assertFalse(self.service.take_service_restart())
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertEqual(self.program('print(board.kb.get_task(board.conn, sys.argv[2]).worker_pid)', native_id),
                         str(os.getpid()))
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual(self.provider.started, [])

    def test_checkout_recovery_refuses_changed_dirty_content_or_task_input(self):
        payload, dirty, artifact = self.checkout_incident()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        (self.workspace / 'checkout-a.txt').write_text('a newer edit\n')
        self.restart()
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual((self.workspace / 'checkout-a.txt').read_text(), 'a newer edit\n')
        self.assertEqual(self.provider.started, [])
        old_key = self.key
        self.key = str(uuid4())
        payload['recovery_branch'] = 'wt/recovered-' + self.key
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 409)
        self.assertEqual(self.service._get('workspace_recoveries', old_key)['state'], 'held')
        (self.workspace / 'checkout-a.txt').write_bytes(dirty['checkout-a.txt'])
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.assertEqual(self.service._get('workspace_recoveries', old_key)['state'], 'superseded')
        self.assertTrue(self.service.take_service_restart())
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied', self.receipt())
        self.assertEqual((self.workspace / 'checkout-a.txt').read_bytes(), dirty['checkout-a.txt'])

    def test_checkout_recovery_held_refuses_different_incident_and_manifest_change(self):
        payload, dirty, artifact = self.checkout_incident()
        original_manifest = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        (self.workspace / 'checkout-a.txt').write_text('changed after authorization\n')
        self.restart()
        original_key = self.key
        self.key = str(uuid4())
        replacement = dict(payload, recovery_branch='wt/recovered-' + self.key,
                           attached_head=payload['saved_head'])
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', replacement, key=self.key)[0], 409)
        self.assertEqual(self.service._get('workspace_recoveries', original_key)['state'], 'held')
        self.assertEqual(self.manifest.read_bytes(), original_manifest)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual(git(self.workspace, 'rev-parse', 'HEAD'), payload['attached_head'])
        self.assertEqual((self.workspace / 'checkout-a.txt').read_text(), 'changed after authorization\n')
        (self.workspace / 'checkout-a.txt').write_bytes(dirty['checkout-a.txt'])
        self.manifest.write_bytes(original_manifest + b' ')
        replacement['attached_head'] = payload['attached_head']
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', replacement, key=self.key)[0], 409)
        self.assertEqual(self.service._get('workspace_recoveries', original_key)['state'], 'held')
        self.assertEqual(self.manifest.read_bytes(), original_manifest + b' ')

    def test_checkout_recovery_ignored_obstruction_created_after_request_holds(self):
        payload, dirty, artifact = self.checkout_incident()
        git_dir = Path(git(self.workspace, 'rev-parse', '--git-common-dir'))
        (git_dir / 'info' / 'exclude').write_text('saved-0.txt\n')
        before = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        ignored = self.workspace / 'saved-0.txt'
        ignored.write_text('ignored after request\n')
        self.restart()
        self.assertEqual(self.receipt()['state'], 'held', self.receipt())
        self.assertEqual(ignored.read_text(), 'ignored after request\n')
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual(git(self.workspace, 'rev-parse', 'HEAD'), payload['attached_head'])
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)
        self.assertEqual(self.provider.started, [])

    def test_checkout_recovery_preserves_leading_space_tracked_filename(self):
        payload, dirty, artifact = self.checkout_incident()
        filename = ' leading-space.txt'
        (self.workspace / filename).write_text('tracked base\n')
        git(self.workspace, 'add', '--', filename)
        git(self.workspace, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-m', 'leading space tracked path')
        payload['attached_head'] = git(self.workspace, 'rev-parse', 'HEAD')
        (self.workspace / filename).write_text('tracked dirty edit\n')
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.assertIn(filename, self.receipt()['dirty'])
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied', self.receipt())
        self.assertEqual((self.workspace / filename).read_text(), 'tracked dirty edit\n')

    def test_checkout_recovery_held_after_switch_cannot_be_superseded(self):
        from coconut.workspace_recovery import checkout_git
        payload, dirty, artifact = self.checkout_incident()
        original_manifest = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.server.server_close()
        self.service.close()
        def crash(path, *arguments, **kwargs):
            result = checkout_git(path, *arguments, **kwargs)
            if 'switch' in arguments:
                raise SystemExit('simulated crash after branch switch')
            return result
        with patch('coconut.workspace_recovery.checkout_git', side_effect=crash), self.assertRaises(SystemExit):
            Service(self.data, native.Provider(self.data), start_workers=False)
        with sqlite3.connect(self.data / 'coconut.sqlite3') as database:
            task = json.loads(database.execute('SELECT data FROM tasks WHERE id=?', (self.task['id'],)).fetchone()[0])
            task['title'] = 'changed after partial switch'
            database.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(task), self.task['id']))
        self.service = Service(self.data, native.Provider(self.data), start_workers=False)
        self.provider = self.service.provider
        self.service.restart_enabled = True
        self.server = APIServer(('127.0.0.1', 0), self.service)
        original_key = self.key
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['recovery_branch'])
        self.key = str(uuid4())
        replacement = dict(payload, recovery_branch='wt/recovered-' + self.key)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', replacement, key=self.key)[0], 409)
        self.assertEqual(self.service._get('workspace_recoveries', original_key)['state'], 'held')
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['recovery_branch'])
        self.assertEqual(git(self.workspace, 'rev-parse', 'HEAD'), self.service._get('workspace_recoveries', original_key)['merge_commit'])
        self.assertEqual(self.manifest.read_bytes(), original_manifest)
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)
        self.assertEqual(self.provider.started, [])

    def test_checkout_recovery_held_after_manifest_write_cannot_be_superseded(self):
        payload, dirty, artifact = self.checkout_incident()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.server.server_close()
        self.service.close()
        def crash(path, data):
            atomic_manifest(path, data)
            raise SystemExit('simulated crash after manifest replacement')
        with patch('coconut.workspace_recovery.atomic_manifest', side_effect=crash), self.assertRaises(SystemExit):
            Service(self.data, native.Provider(self.data), start_workers=False)
        recovered_manifest = self.manifest.read_bytes()
        with sqlite3.connect(self.data / 'coconut.sqlite3') as database:
            task = json.loads(database.execute('SELECT data FROM tasks WHERE id=?', (self.task['id'],)).fetchone()[0])
            task['title'] = 'changed after manifest replacement'
            database.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(task), self.task['id']))
        self.service = Service(self.data, native.Provider(self.data), start_workers=False)
        self.provider = self.service.provider
        self.service.restart_enabled = True
        self.server = APIServer(('127.0.0.1', 0), self.service)
        original_key = self.key
        self.assertEqual(self.receipt()['state'], 'held')
        self.key = str(uuid4())
        replacement = dict(payload, recovery_branch='wt/recovered-' + self.key)
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', replacement, key=self.key)[0], 409)
        self.assertEqual(self.service._get('workspace_recoveries', original_key)['state'], 'held')
        self.assertEqual(self.manifest.read_bytes(), recovered_manifest)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['recovery_branch'])
        self.assertEqual(git(self.workspace, 'rev-parse', 'HEAD'), self.service._get('workspace_recoveries', original_key)['merge_commit'])
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)
        self.assertEqual(self.provider.started, [])

    def test_checkout_recovery_refuses_changed_owner_input_before_git_write(self):
        payload, dirty, artifact = self.checkout_incident()
        before = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        task = self.service._get('tasks', self.task['id'])
        task['title'] = 'A different approved scope'
        self.service._put('tasks', task)
        self.restart()
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['attached_branch'])
        self.assertEqual(self.provider.started, [])

    def test_checkout_recovery_replays_after_switch_and_manifest_crash(self):
        payload, dirty, artifact = self.checkout_incident()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.server.server_close()
        self.service.close()
        def crash(path, data):
            atomic_manifest(path, data)
            raise SystemExit('simulated crash after manifest replacement')
        with patch('coconut.workspace_recovery.atomic_manifest', side_effect=crash), self.assertRaises(SystemExit):
            Service(self.data, native.Provider(self.data), start_workers=False)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['recovery_branch'])
        self.assertEqual([(self.workspace / name).read_bytes() for name in dirty], list(dirty.values()))
        self.service = Service(self.data, native.Provider(self.data), start_workers=False)
        self.provider = self.service.provider
        self.service.restart_enabled = True
        self.server = APIServer(('127.0.0.1', 0), self.service)
        self.assertEqual(self.receipt()['state'], 'applied', self.receipt())
        successor_id = self.receipt()['successor_task_id']
        self.restart()
        self.assertEqual(self.receipt()['successor_task_id'], successor_id)
        self.assertEqual(len([task for task in self.service._all('tasks') if task.get('_work_context_id') == artifact['id']]), 1)

    def test_checkout_recovery_replays_after_branch_switch_before_manifest(self):
        from coconut.workspace_recovery import checkout_git
        payload, dirty, artifact = self.checkout_incident()
        original_manifest = self.manifest.read_bytes()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.server.server_close()
        self.service.close()
        def crash(path, *arguments, **kwargs):
            result = checkout_git(path, *arguments, **kwargs)
            if 'switch' in arguments:
                raise SystemExit('simulated crash after branch switch')
            return result
        with patch('coconut.workspace_recovery.checkout_git', side_effect=crash), self.assertRaises(SystemExit):
            Service(self.data, native.Provider(self.data), start_workers=False)
        self.assertEqual(self.manifest.read_bytes(), original_manifest)
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), payload['recovery_branch'])
        self.service = Service(self.data, native.Provider(self.data), start_workers=False)
        self.provider = self.service.provider
        self.service.restart_enabled = True
        self.server = APIServer(('127.0.0.1', 0), self.service)
        self.assertEqual(self.receipt()['state'], 'applied', self.receipt())
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)

    def test_checkout_recovery_replays_crash_after_successor_before_receipt(self):
        payload, dirty, artifact = self.checkout_incident()
        self.assertEqual(self.http('/v1/maintenance/checkout-recovery', payload, key=self.key)[0], 200)
        self.server.server_close()
        self.service.close()
        original_reply = Service._reply
        def crash(service, task, data):
            original_reply(service, task, data)
            raise SystemExit('simulated crash before successor transaction commits')
        with patch.object(Service, '_reply', crash), self.assertRaises(SystemExit):
            Service(self.data, native.Provider(self.data), start_workers=False)
        self.service = Service(self.data, native.Provider(self.data), start_workers=False)
        self.provider = self.service.provider
        self.service.restart_enabled = True
        self.server = APIServer(('127.0.0.1', 0), self.service)
        receipt = self.receipt()
        self.assertEqual(receipt['state'], 'applied', receipt)
        self.assertEqual(len([task for task in self.service._all('tasks')
                              if task.get('_work_context_id') == artifact['id']]), 1)
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in dirty}, dirty)

    def test_authenticated_drain_startup_and_exact_replay(self):
        with self.assertRaisesRegex(ValueError, 'changed branches'):
            preferred(self.data / 'workspaces', self.owner)
        self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Unrelated queued outcome'})
        untouched = {name: copy.deepcopy(self.service._all(name)) for name in ('tasks', 'artifacts', 'decisions')}
        status, receipt = self.request()
        self.assertEqual((status, receipt['state']), (200, 'draining'))
        self.service._dispatch_pending()
        self.assertEqual(self.provider.started, [])
        self.assertEqual(self.manifest.read_bytes(), self.before)
        with self.assertRaises(KeyboardInterrupt):
            self.server.service_actions()
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied')
        self.assertEqual(preferred(self.data / 'workspaces', self.owner), self.workspace)
        for name, rows in untouched.items():
            self.assertEqual(self.service._all(name), rows)
        self.assertEqual(self.request()[1]['state'], 'applied')
        self.assertFalse(self.service.restart_pending)
        self.assertEqual((self.workspace / '.env').read_text(), 'SYNTHETIC=preserved\n')

    def test_auth_owner_scope_and_stale_branch_refused_without_mutation(self):
        self.assertEqual(self.request(token=False)[0], 401)
        token, self.token = self.token, 'invalid-device-token'
        self.assertEqual(self.request()[0], 401)
        self.token = token
        with self.assertRaises(ServiceError):
            self.service.request_workspace_recovery(self.payload, self.key, 'local')
        with self.assertRaises(ServiceError):
            self.service.emit(self.task['id'], 'service_restart', self.payload)
        for field, value in (('expected_owner', str(uuid4())), ('native_task_id', 't_wrong'),
                             ('old_branch', 'stale'), ('new_branch', 'nonexistent'),
                             ('repository', str(self.root)), ('path', str(self.repo)), ('extra', 'forbidden')):
            with self.subTest(field=field):
                status, result = self.request(data=dict(self.payload, **{field: value}))
                self.assertIn(status, (400, 409), result)
                self.assertEqual(self.manifest.read_bytes(), self.before)
        self.assertEqual(self.service._all('workspace_recoveries'), [])

    def test_request_key_cannot_authorize_another_scope(self):
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request(data=dict(self.payload, new_branch='another'))[0], 409)

    def test_active_turn_drains_without_starting_queued_work(self):
        worker_done = threading.Event()
        worker = threading.Thread(target=worker_done.wait)
        worker.start()
        self.addCleanup(lambda: (worker_done.set(), worker.join()))
        self.service.running[self.task['id']] = worker
        self.assertEqual(self.request()[0], 200)
        self.service._dispatch_pending()
        self.assertFalse(self.service.take_service_restart())
        self.assertTrue(worker.is_alive())
        self.assertEqual(self.provider.started, [])
        worker_done.set()
        worker.join()
        self.assertTrue(self.service.take_service_restart())

    def test_ordinary_restart_deadline_cannot_release_workspace_recovery_drain(self):
        from coconut.service_restart import RESTART_DRAIN_SECONDS
        worker_done = threading.Event()
        worker = threading.Thread(target=worker_done.wait)
        worker.start()
        self.addCleanup(lambda: (worker_done.set(), worker.join()))
        self.service.running[self.task['id']] = worker
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        self.assertEqual(self.request()[0], 200)
        with patch('coconut.service_restart.time.monotonic', return_value=100) as clock:
            self.assertEqual(self.service.request_service_restart(task['id'], {})['status'], 'queued')
            clock.return_value += RESTART_DRAIN_SECONDS + 1
            for _ in range(2):
                self.assertFalse(self.service.take_service_restart())
                self.assertTrue(self.service.restart_pending)
                self.service._dispatch_pending()
        self.assertTrue(worker.is_alive())
        self.assertEqual(self.provider.started, [])
        self.assertEqual(self.manifest.read_bytes(), self.before)
        self.assertEqual(self.receipt()['state'], 'draining')
        self.assertEqual(self.service.db.execute(
            "SELECT count(*) FROM events WHERE type='service.restart_deferred'").fetchone()[0], 0)
        worker_done.set()
        worker.join()
        self.assertTrue(self.service.take_service_restart())
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied')
        self.assertFalse(self.service.restart_pending)
        self.assertIsNone(self.service.restart_deadline)

    def test_request_serializes_with_real_native_preparation(self):
        other = self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Already accepted work'})
        entered, release = threading.Event(), threading.Event()
        self.provider.release.clear()
        prepare = self.provider.prepare_kanban_worker
        def preparing(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError('Preparation was not released')
            return prepare(*args)
        self.provider.prepare_kanban_worker = preparing
        tick = threading.Thread(target=self.service._dispatch_pending)
        tick.start()
        self.addCleanup(lambda: (release.set(), self.provider.release.set(), tick.join(15)))
        self.assertTrue(entered.wait(10))
        accepted = []
        requester = threading.Thread(target=lambda: accepted.append(self.request()))
        requester.start()
        try:
            requester.join(.1)
            self.assertTrue(requester.is_alive())
            self.assertEqual(self.service._all('workspace_recoveries'), [])
        finally:
            release.set()
            requester.join(15)
            tick.join(15)
        self.assertEqual(accepted[0][0], 200)
        self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'Queued during drain'})
        self.service._dispatch_pending()
        self.assertEqual(self.provider.started, [other['id']])
        self.assertFalse(self.service.take_service_restart())
        self.provider.release.set()

    def test_active_or_ambiguous_durable_delegation_holds_repair(self):
        ledger = self.data / 'bots' / self.bot['id'] / 'home' / 'state.db'
        with sqlite3.connect(ledger) as db:
            db.execute('CREATE TABLE async_delegations (state TEXT)')
            db.execute("INSERT INTO async_delegations VALUES ('finalizing')")
        self.assertEqual(self.request()[0], 200)
        self.assertFalse(self.service.take_service_restart())
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertEqual(self.manifest.read_bytes(), self.before)

    def test_uncertain_applied_request_cannot_overwrite_a_later_binding(self):
        self.assertEqual(self.request()[0], 200)
        # Crash after the atomic filesystem commit but before the SQLite receipt.
        atomic_manifest(self.manifest, self.receipt()['after'])
        newer = json.loads(self.manifest.read_text())
        next(iter(newer.values()))['branch'] = 'a-later-owner-transition'
        self.manifest.write_text(json.dumps(newer))
        expected = self.manifest.read_bytes()
        self.restart()
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertEqual(self.manifest.read_bytes(), expected)

    def test_post_replace_io_failure_is_recognized_on_next_startup(self):
        self.assertEqual(self.request()[0], 200)
        def uncertain(path, data):
            atomic_manifest(path, data)
            raise OSError('simulated directory sync failure')
        with patch('coconut.workspace_recovery.atomic_manifest', side_effect=uncertain):
            self.restart()
        self.assertEqual(self.receipt()['state'], 'held')
        applied = self.manifest.read_bytes()
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied')
        self.assertNotIn('error', self.receipt())
        self.assertEqual(self.manifest.read_bytes(), applied)

    def test_active_background_and_ambiguous_checkpoint_refuse_without_killing(self):
        checkpoint = self.data / 'runtimes' / self.owner / 'fixture' / 'processes.json'
        checkpoint.parent.mkdir(parents=True)
        for entries in ([dict(pid=os.getpid(), pid_scope='host')], [dict(pid=99999999, pid_scope='container')], {'bad': []}):
            with self.subTest(entries=entries):
                checkpoint.write_text(json.dumps(entries))
                self.key = str(uuid4())
                self.assertEqual(self.request()[0], 200)
                self.assertFalse(self.service.take_service_restart())
                self.assertEqual(self.receipt()['state'], 'held')
                self.assertFalse(self.service.take_service_restart())
                self.assertEqual(self.manifest.read_bytes(), self.before)

    def test_ambiguous_native_preparation_is_not_reclaimed_during_drain(self):
        self.program('board.kb.unblock_task(board.conn, sys.argv[2]); board.kb.claim_task(board.conn, sys.argv[2])', self.native_id)
        self.assertEqual(self.request()[0], 200)
        self.service._dispatch_pending()
        self.assertFalse(self.service.take_service_restart())
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertEqual(self.program('print(board.kb.get_task(board.conn, sys.argv[2]).status)', self.native_id), 'running')

    def test_startup_active_writer_holds_before_recovery_or_dispatch(self):
        self.assertEqual(self.request()[0], 200)
        checkpoint = self.data / 'bots' / self.bot['id'] / 'home' / 'processes.json'
        checkpoint.write_text(json.dumps([dict(pid=os.getpid())]))
        with patch.object(Service, '_recover', side_effect=AssertionError('must not recover')):
            self.restart()
        self.assertEqual(self.receipt()['state'], 'held')
        self.service.start()
        self.assertIsNone(self.service.dispatcher)
        self.service._dispatch_pending()
        self.assertEqual(self.manifest.read_bytes(), self.before)
        checkpoint.unlink()
        self.key = str(uuid4())
        self.assertEqual(self.request()[0], 200)
        self.assertTrue(self.service.take_service_restart())
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied')

    def test_startup_refuses_newer_manifest_or_task_input(self):
        for kind in ('manifest', 'task'):
            with self.subTest(kind=kind):
                self.key = str(uuid4())
                self.assertEqual(self.request()[0], 200)
                original = self.service._get('tasks', self.task['id'])
                if kind == 'manifest':
                    newer = json.loads(self.manifest.read_text())
                    next(iter(newer.values()))['branch'] = 'a-newer-owner-branch'
                    self.manifest.write_text(json.dumps(newer))
                else:
                    self.service._put('tasks', dict(original, _native_turn_id='newer-input'))
                expected = self.manifest.read_bytes()
                self.restart()
                self.assertEqual(self.receipt()['state'], 'held')
                self.assertEqual(self.manifest.read_bytes(), expected)
                self.manifest.write_bytes(self.before)
                self.service._put('tasks', original)

    def test_an_unrelated_binding_change_does_not_hold_an_authorized_repair(self):
        self.assertEqual(self.request()[0], 200)
        # Worktree retirement can write another binding between authorization and startup.
        saved = json.loads(self.manifest.read_text())
        unrelated = dict(next(iter(saved.values())), outcome='another-outcome', path=str(self.root / 'other'),
                         retired=dict(at='2026-09-01T00:00:00Z', archive=str(self.root / 'other.tar.gz'), head='0', changes=''))
        saved['another-outcome-0000000000000000'] = unrelated
        self.manifest.write_text(json.dumps(saved))
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied')
        after = json.loads(self.manifest.read_text())
        self.assertEqual(after['another-outcome-0000000000000000'], unrelated)
        self.assertEqual(preferred(self.data / 'workspaces', self.owner), self.workspace)

    def test_a_later_binding_for_the_same_worktree_holds_the_repair(self):
        self.assertEqual(self.request()[0], 200)
        saved = json.loads(self.manifest.read_text())
        saved['another-outcome-0000000000000000'] = dict(next(iter(saved.values())), outcome='another-outcome')
        self.manifest.write_text(json.dumps(saved))
        expected = self.manifest.read_bytes()
        self.restart()
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertIn('Another binding', self.receipt()['error'])
        self.assertEqual(self.manifest.read_bytes(), expected)

    def test_crash_before_and_after_atomic_write_recovers_exact_transition(self):
        for applied in (False, True):
            with self.subTest(applied=applied):
                self.manifest.write_bytes(self.before)
                self.key = str(uuid4())
                self.assertEqual(self.request()[0], 200)
                self.server.server_close()
                self.service.close()
                def crash(path, data):
                    if applied:
                        atomic_manifest(path, data)
                    raise SystemExit('simulated power loss')
                with patch('coconut.workspace_recovery.atomic_manifest', side_effect=crash), self.assertRaises(SystemExit):
                    Service(self.data, native.Provider(self.data), start_workers=False)
                self.assertEqual(self.manifest.read_bytes() == self.before, not applied)
                self.service = Service(self.data, native.Provider(self.data), start_workers=False)
                self.provider = self.service.provider
                self.service.restart_enabled = True
                self.server = APIServer(('127.0.0.1', 0), self.service)
                self.assertEqual(self.receipt()['state'], 'applied')
                self.assertEqual(preferred(self.data / 'workspaces', self.owner), self.workspace)

    def test_lifetime_lock_excludes_second_startup(self):
        self.assertEqual(self.request()[0], 200)
        with self.assertRaisesRegex(ServiceError, 'already owns'):
            Service(self.data, native.Provider(self.data), start_workers=False)
        self.assertEqual(self.manifest.read_bytes(), self.before)

    def prepare_legacy_native_pr(self):
        self.assertEqual(self.request()[0], 200)
        self.assertTrue(self.service.take_service_restart())
        self.restart()
        self.program('board.kb.unblock_task(board.conn, sys.argv[2]); board.kb.complete_task(board.conn, sys.argv[2], summary="Approved followup retained")', self.native_id)
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        artifact = dict(id=str(uuid4()), kind='pr', task_id=task['id'], bot_id=task['bot_id'],
                        _native_outcome_id=self.native_id, status='open', title='Retained PR',
                        url='https://github.com/synthetic/recovery/pull/7748', created_at=task['created_at'])
        self.service._put('artifacts', artifact)
        self.restart()
        self.assertNotIn('_work_context_id', self.service._get('tasks', task['id']))
        return task, artifact

    def test_legacy_native_pr_followup_dispatches_after_startup(self):
        # A legacy PR owner has no _work_context_id. The native terminal parent
        # must stay terminal while its followup retains the execution workspace.
        task, artifact = self.prepare_legacy_native_pr()
        result = self.write('/v1/tasks/' + task['id'] + '/replies', {'text': 'Implement the approved followup'})
        self.assertEqual(self.service._get('tasks', result['id']).get('_execution_session_id'), self.owner)
        self.service._dispatch_pending()
        deadline = time.monotonic() + 10
        while not self.provider.started and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.provider.started, [result['id']])
        self.assertEqual(self.service.kanban.tasks[result['id']]['workspace_path'], str(self.workspace))

    def assert_legacy_continuation_owner(self, artifact, current_id, task_count):
        tasks = self.service._all('tasks')
        self.assertEqual(len(tasks), task_count)
        self.assertEqual({t['_execution_session_id'] for t in tasks}, {self.owner})
        self.assertEqual([t['id'] for t in tasks if t['status'] in self.service.ACTIVE], [current_id])
        item = self.service._get('artifacts', artifact['id'])
        self.assertEqual(item['task_id'], current_id)
        self.assertEqual(item['_work_origin_task_id'], self.task['id'])
        self.assertIn(self.task['id'], item['_work_previous_task_ids'])
        self.assertEqual(self.service.kanban.tasks[self.task['id']]['status'], 'done')
        self.assertLessEqual({identity for identity, native_task in self.service.kanban.tasks.items()
                              if native_task['status'] not in ('done', 'archived')}, {current_id})
        self.assertNotIn('_work_context_id', self.service._get('tasks', self.task['id']))

    def test_repeated_legacy_replies_reuse_queued_owner_after_artifact_moves(self):
        task, artifact = self.prepare_legacy_native_pr()
        path = '/v1/tasks/' + task['id'] + '/replies'
        first = self.write(path, {'text': 'Implement the approved followup'})
        self.assert_legacy_continuation_owner(artifact, first['id'], 2)
        self.restart()
        for text in ('Keep the original acceptance criteria', 'Also verify the saved result'):
            result = self.write(path, {'text': text})
            self.assertEqual(result['id'], first['id'])
            self.assert_legacy_continuation_owner(artifact, first['id'], 2)
        self.assertEqual(self.provider.started, [])
        self.assertEqual([m['text'] for m in self.service._all('messages') if m.get('task_id') == first['id']],
                         ['Implement the approved followup', 'Keep the original acceptance criteria',
                          'Also verify the saved result'])

    def test_repeated_legacy_replies_follow_active_and_terminal_native_successors(self):
        task, artifact = self.prepare_legacy_native_pr()
        path = '/v1/tasks/' + task['id'] + '/replies'
        previous_native = self.native_id
        previous_tasks = [task['id']]
        for generation in range(2):
            result = self.write(path, {'text': f'Implement approved followup {generation}'})
            self.assertNotIn(result['id'], previous_tasks)
            self.assert_legacy_continuation_owner(artifact, result['id'], generation + 2)
            self.provider.release.clear()
            try:
                self.service._dispatch_pending()
                deadline = time.monotonic() + 10
                while result['id'] not in self.provider.started and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(self.provider.started, [result['id']])
                current = self.service.kanban.tasks[result['id']]
                self.assertEqual(current['status'], 'running')
                self.assertNotEqual(current['id'], previous_native)
                self.assertEqual(current['workspace_path'], str(self.workspace))
                item = self.service._get('artifacts', artifact['id'])
                self.assertEqual(item['_native_outcome_id'], current['id'])
                self.assertEqual(self.service._get('tasks', result['id'])['_native_outcome_id'], current['id'])
                for text in ('Keep the followup local', 'Verify before completing'):
                    reply = self.write(path, {'text': text})
                    self.assertEqual(reply['id'], result['id'])
                    self.assert_legacy_continuation_owner(artifact, result['id'], generation + 2)
                self.assertEqual(self.service._get('tasks', result['id'])['_steering'],
                                 ['Keep the followup local', 'Verify before completing'])
            finally:
                self.provider.release.set()
                for worker in list(self.service.running.values()):
                    worker.join(10)
                    self.assertFalse(worker.is_alive())
            # Replies received during a turn are consumed at the next safe turn
            # on the same Coconut task and native successor.
            self.assertEqual(self.service._get('tasks', result['id'])['status'], 'queued')
            self.assert_legacy_continuation_owner(artifact, result['id'], generation + 2)
            captured = []
            start = self.provider.start_task
            def capture(task, bot, history, emit):
                captured.extend(m['text'] for m in history if m.get('role') == 'user')
                return start(task, bot, history, emit)
            with patch.object(self.provider, 'start_task', side_effect=capture):
                self.service._dispatch_pending()
                for worker in list(self.service.running.values()):
                    worker.join(10)
                    self.assertFalse(worker.is_alive())
            self.assertEqual(self.provider.started, [result['id'], result['id']])
            self.assertIn('Keep the followup local', captured)
            self.assertIn('Verify before completing', captured)
            self.assertEqual(self.service.kanban.tasks[result['id']]['id'], current['id'])
            self.assertFalse(self.service._get('tasks', result['id']).get('_steering'))
            claim = self.service._get('tasks', result['id'])['_kanban_claim']
            self.program('assert board.kb.complete_task(board.conn, sys.argv[2], summary="Verified followup", expected_run_id=int(sys.argv[3]))',
                         current['id'], str(claim['run_id']))
            self.service.kanban.call({'op': 'snapshot'})
            self.service._reconcile_kanban()
            self.assertEqual(self.service._get('tasks', result['id'])['status'], 'completed')
            self.assertEqual([t for t in self.service._all('tasks') if t['status'] in self.service.ACTIVE], [])
            self.assertEqual(int(self.program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (sys.argv[2],)).fetchone()[0])', current['id'])), 2)
            self.assertEqual(self.service.kanban.tasks[previous_tasks[-1]]['status'], 'done')
            previous_native = current['id']
            previous_tasks.append(result['id'])
            self.restart()
        item = self.service._get('artifacts', artifact['id'])
        self.assertEqual(item['_work_previous_task_ids'], previous_tasks[:-1])

    def test_legacy_continuation_rejects_ambiguous_or_unproven_ownership(self):
        task, artifact = self.prepare_legacy_native_pr()
        path = '/v1/tasks/' + task['id'] + '/replies'
        other = dict(artifact, id=str(uuid4()))
        self.service._put('artifacts', other)
        status, error = self.http(path, {'text': 'Ambiguous original owner'})
        self.assertEqual(status, 409, error)
        self.assertEqual(len(self.service._all('tasks')), 1)
        self.service.db.execute('DELETE FROM artifacts WHERE id=?', (other['id'],))
        first = self.write(path, {'text': 'Continue the retained outcome'})
        item = self.service._get('artifacts', artifact['id'])
        owner = self.service._get('tasks', first['id'])
        mutations = [
            ('artifacts', dict(item, bot_id=str(uuid4()))),
            ('artifacts', dict(item, _work_origin_task_id=None, _work_previous_task_ids=[])),
            ('artifacts', dict(item, _native_outcome_id='t_unrelated')),
            ('tasks', dict(owner, bot_id=str(uuid4()))),
            ('tasks', dict(owner, conversation_bot_id=str(uuid4()))),
            ('tasks', dict(owner, _execution_session_id=str(uuid4()))),
            ('tasks', dict(owner, _work_context_id=str(uuid4()))),
            ('tasks', dict(owner, _native_outcome_id='t_unrelated')),
            ('tasks', dict(task, _execution_session_id=str(uuid4()))),
        ]
        for table, changed in mutations:
            with self.subTest(table=table, changed=changed):
                self.service._put(table, changed)
                before = {name: copy.deepcopy(self.service._all(name)) for name in ('tasks', 'artifacts', 'messages')}
                status, error = self.http(path, {'text': 'Do not route through an unproven owner'})
                self.assertEqual(status, 409, error)
                for name, rows in before.items():
                    self.assertEqual(self.service._all(name), rows)
                self.service._put('tasks', task)
                self.service._put('tasks', owner)
                self.service._put('artifacts', item)
        # A second artifact retaining the same original native owner is still
        # ambiguous even though the first artifact now belongs to its successor.
        self.service._put('artifacts', other)
        status, error = self.http(path, {'text': 'Ambiguous retained history'})
        self.assertEqual(status, 409, error)
        self.assertEqual(len(self.service._all('tasks')), 2)

    def test_native_readiness_does_not_dispatch_excluded_request(self):
        # A native dependency retry becomes ready *inside* dispatch_once, after
        # Coconut has projected its completed turn. This reproduces the rejected
        # spawn rather than merely asserting a precomputed ready row.
        self.program('with board.kb.write_txn(board.conn): board.conn.execute("UPDATE tasks SET status=\'todo\' WHERE id=?", (sys.argv[2],))', self.native_id)
        task = self.service._get('tasks', self.task['id'])
        task.update(status='completed')
        self.service._put('tasks', task)
        self.service._dispatch_pending()
        self.assertEqual(self.provider.started, [])
        native_task = self.service.kanban.tasks[task['id']]
        self.assertEqual(native_task['consecutive_failures'], 0)
        self.assertEqual(native_task['status'], 'ready')
        self.assertIsNone(native_task['worker_pid'])
        self.assertIsNone(native_task['current_run_id'])

    def native_run_count(self):
        return int(self.program('print(board.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (sys.argv[2],)).fetchone()[0])', self.native_id))

    def continue_from_owner(self):
        source = self.write('/v1/bots/' + self.bot['id'] + '/messages', {'text': 'The missing facts are available'})
        source = self.service._get('tasks', source['id'])
        source['status'] = 'working'
        self.service._put('tasks', source)
        count = len(self.service._all('tasks'))
        result = self.service.emit(source['id'], 'work_continue',
            dict(task_id=self.native_id, message='Use the verified facts to finish this same outcome.'))
        self.assertEqual(result['task_id'], self.task['id'])
        self.assertEqual(len(self.service._all('tasks')), count)
        source['status'] = 'completed'
        self.service._put('tasks', source)
        return result

    def complete_continued_outcome(self):
        start = self.provider.start_task
        def complete(task, *args):
            self.program('assert board.kb.complete_task(board.conn, sys.argv[2], summary="Verified resumed outcome", expected_run_id=int(sys.argv[3]))',
                         self.native_id, str(self.service._get('tasks', task['id'])['_kanban_claim']['run_id']))
            return start(task, *args)
        with patch.object(self.provider, 'start_task', side_effect=complete):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.service._dispatch_pending()
                if self.service.kanban.tasks[self.task['id']]['status'] == 'done':
                    break
            for worker in list(self.service.running.values()):
                worker.join(5)
        for _ in range(3):
            self.service._dispatch_pending()
        current = self.service.kanban.tasks[self.task['id']]
        self.assertEqual(current['status'], 'done')
        self.assertEqual(current['id'], self.native_id)
        self.assertEqual(current['consecutive_failures'], 0)
        self.assertEqual(self.provider.started, [self.task['id']])
        self.assertEqual(self.service._get('tasks', self.task['id'])['_execution_session_id'], self.owner)

    def test_meaningful_block_survives_ticks_and_native_owner_continuation(self):
        self.program('board.kb.unblock_task(board.conn, sys.argv[2]); board.kb.block_task(board.conn, sys.argv[2], kind="needs_input", reason="Need the verified checksum")', self.native_id)
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        runs = self.native_run_count()
        for _ in range(3):
            self.service._dispatch_pending()
            current = self.service.kanban.tasks[self.task['id']]
            self.assertEqual(current['status'], 'blocked')
            self.assertEqual(current['block_kind'], 'needs_input')
            self.assertEqual(current['outcome_summary'], 'Need the verified checksum')
            self.assertEqual(current['consecutive_failures'], 0)
        self.assertEqual(self.native_run_count(), runs)
        self.assertEqual(self.provider.started, [])
        self.continue_from_owner()
        self.complete_continued_outcome()
        self.assertEqual(self.native_run_count(), runs + 1)

    def test_native_continuation_still_waits_for_dependency_then_dispatches_once(self):
        parent = self.program('parent=board.kb.create_task(board.conn, title="Prerequisite", initial_status="blocked", workspace_kind="dir", workspace_path=sys.argv[3]); board.kb.link_tasks(board.conn, parent_id=parent, child_id=sys.argv[2]); print(parent)', self.native_id, str(self.workspace))
        for _ in range(3):
            self.service._dispatch_pending()
            self.assertEqual(self.service.kanban.tasks[self.task['id']]['status'], 'blocked')
        self.continue_from_owner()
        runs = self.native_run_count()
        for _ in range(3):
            self.service._dispatch_pending()
            self.assertEqual(self.service.kanban.tasks[self.task['id']]['status'], 'todo')
        self.assertEqual(self.native_run_count(), runs)
        self.assertEqual(self.provider.started, [])
        self.program('board.kb.unblock_task(board.conn, sys.argv[2]); board.kb.complete_task(board.conn, sys.argv[2], summary="Prerequisite verified")', parent)
        self.complete_continued_outcome()
        self.assertEqual(self.native_run_count(), runs + 1)

    def test_unselected_dependency_wait_survives_until_native_promotion(self):
        parent = self.program('''
import inspect
board.kb.unblock_task(board.conn, sys.argv[2])
claim = board.kb.claim_task(board.conn, sys.argv[2])
assert claim
parent = board.kb.create_task(board.conn, title="Prerequisite", initial_status="blocked",
                              workspace_kind="dir", workspace_path=sys.argv[3])
# Newer kernels require the worker's claim when linking its running child.
kwargs = ({'expected_child_run_id': claim.current_run_id}
          if 'expected_child_run_id' in inspect.signature(board.kb.link_tasks).parameters else {})
board.kb.link_tasks(board.conn, parent_id=parent, child_id=sys.argv[2], **kwargs)
assert board.kb.block_task(board.conn, sys.argv[2], kind="dependency", reason="Wait for prerequisite")
print(parent)
''', self.native_id, str(self.workspace))
        runs = self.native_run_count()
        for _ in range(3):
            self.service._dispatch_pending()
            current = self.service.kanban.tasks[self.task['id']]
            self.assertEqual(current['status'], 'todo')
            self.assertEqual(current['block_kind'], 'dependency')
            self.assertEqual(current['outcome_summary'], 'Wait for prerequisite')
        self.assertEqual(self.native_run_count(), runs)
        self.program('board.kb.unblock_task(board.conn, sys.argv[2]); board.kb.complete_task(board.conn, sys.argv[2], summary="Prerequisite verified")', parent)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertEqual(self.service.kanban.tasks[self.task['id']]['status'], 'ready')
        self.complete_continued_outcome()
        self.assertEqual(self.native_run_count(), runs + 1)

    def test_dead_wrapper_with_live_child_group_holds_drain_and_startup(self):
        release = self.root / 'release-child'
        child_code = 'import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nend=time.monotonic()+60\nwhile not p.exists() and time.monotonic()<end: time.sleep(.02)\np.with_suffix(".stopped").touch()\n'
        wrapper = subprocess.Popen([sys.executable, '-B', '-c',
            'import subprocess,sys; p=subprocess.Popen([sys.executable,"-B","-c",sys.argv[1],sys.argv[2]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid)',
            child_code, str(release)], start_new_session=True, stdout=subprocess.PIPE, text=True)
        def release_fixture():
            # Wait for acknowledgement before the temporary directory goes
            # away, including when a negative control fails an assertion.
            release.touch()
            deadline = time.monotonic() + 5
            while not release.with_suffix('.stopped').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(release.with_suffix('.stopped').exists())
        self.addCleanup(release_fixture)
        output, _ = wrapper.communicate(timeout=5)
        child = int(output)
        self.assertEqual(wrapper.returncode, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(wrapper.pid, 0)
        self.assertEqual(os.getpgid(child), wrapper.pid)
        checkpoint = self.data / 'bots' / self.bot['id'] / 'home' / 'processes.json'
        evidence = json.dumps([dict(pid=wrapper.pid, pid_scope='host', host_start_time=123, systemd_unit='')])
        checkpoint.write_text(evidence)
        self.assertEqual(self.request()[0], 200)
        self.assertFalse(self.service.take_service_restart())
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertIn('process group', self.receipt()['error'])
        with patch.object(Service, '_recover', side_effect=AssertionError('must not recover')):
            self.restart()
        self.service._dispatch_pending()
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertEqual(self.manifest.read_bytes(), self.before)
        self.assertEqual(checkpoint.read_text(), evidence)
        self.assertEqual(self.provider.started, [])
        os.kill(child, 0)  # Recovery must not kill the surviving writer.

    def test_uninspectable_group_holds_without_cleanup(self):
        wrapper = subprocess.Popen([sys.executable, '-B', '-c', 'pass'], start_new_session=True)
        wrapper.wait(timeout=5)
        checkpoint = self.data / 'bots' / self.bot['id'] / 'home' / 'processes.json'
        evidence = json.dumps([dict(pid=wrapper.pid, pid_scope='host', host_start_time=123, systemd_unit='')])
        checkpoint.write_text(evidence)
        self.assertEqual(self.request()[0], 200)
        with patch('coconut.workspace_recovery.os.killpg', side_effect=PermissionError('inspection denied')) as group:
            self.assertFalse(self.service.take_service_restart())
            self.restart()
            self.service._dispatch_pending()
        self.assertTrue(group.call_args_list)
        self.assertTrue(all(c.args == (wrapper.pid, 0) for c in group.call_args_list))
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertIn('cannot be inspected', self.receipt()['error'])
        self.assertEqual(self.manifest.read_bytes(), self.before)
        self.assertEqual(checkpoint.read_text(), evidence)

    def test_systemd_scope_holds_with_dead_wrapper_and_absent_group(self):
        wrapper = subprocess.Popen([sys.executable, '-B', '-c', 'pass'], start_new_session=True)
        wrapper.wait(timeout=5)
        checkpoint = self.data / 'bots' / self.bot['id'] / 'home' / 'processes.json'
        for unit in ('hermes-worker-fixture.scope', None):
            with self.subTest(unit=unit):
                evidence = json.dumps([dict(pid=wrapper.pid, pid_scope='host', host_start_time=123, systemd_unit=unit)])
                checkpoint.write_text(evidence)
                self.key = str(uuid4())
                self.assertEqual(self.request()[0], 200)
                self.assertFalse(self.service.take_service_restart())
                self.restart()
                self.service._dispatch_pending()
                self.assertEqual(self.receipt()['state'], 'held')
                self.assertIn('systemd scope', self.receipt()['error'])
                self.assertEqual(self.manifest.read_bytes(), self.before)
                self.assertEqual(checkpoint.read_text(), evidence)

    def test_dead_wrapper_and_absent_group_allow_recovery(self):
        wrapper = subprocess.Popen([sys.executable, '-B', '-c', 'pass'], start_new_session=True)
        wrapper.wait(timeout=5)
        checkpoint = self.data / 'bots' / self.bot['id'] / 'home' / 'processes.json'
        checkpoint.write_text(json.dumps([dict(pid=wrapper.pid, pid_scope='host', systemd_unit='')]))
        self.assertEqual(self.request()[0], 200)
        self.assertTrue(self.service.take_service_restart())
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied')

    def test_live_native_pid_is_never_killed(self):
        self.program('board.kb.unblock_task(board.conn, sys.argv[2]); board.kb.claim_task(board.conn, sys.argv[2]); board.dispatch._set_worker_pid(board.conn, sys.argv[2], int(sys.argv[3]))', self.native_id, str(os.getpid()))
        self.assertEqual(self.request()[0], 200)
        self.assertFalse(self.service.take_service_restart())
        self.assertEqual(self.receipt()['state'], 'held')
        self.service._dispatch_pending()
        self.assertEqual(self.program('print(board.kb.get_task(board.conn, sys.argv[2]).worker_pid)', self.native_id), str(os.getpid()))

    def test_unrecorded_worker_from_older_source_blocks_startup(self):
        old = self.root / 'old-source' / 'hermes_worker.py'
        old.parent.mkdir()
        old.write_text('import time\ntime.sleep(90)\n')
        process = subprocess.Popen([sys.executable, str(old)], env=dict(os.environ,
            HERMES_HOME=str(self.data / 'bots' / self.bot['id'] / 'home')))
        self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
        self.assertEqual(self.request()[0], 200)
        self.restart()
        self.assertEqual(self.receipt()['state'], 'held')
        self.assertIsNone(process.poll())
        self.assertEqual(self.manifest.read_bytes(), self.before)

    def test_repair_does_not_answer_pending_native_approval(self):
        from coconut.service import YieldForDecision
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        with self.assertRaises(YieldForDecision):
            self.service.emit(task['id'], 'native_question', dict(title='Publish the retained patch?',
                options=[dict(id='approve', label='Publish')]))
        decisions = copy.deepcopy(self.service._all('decisions'))
        self.assertEqual(self.request()[0], 200)
        self.assertTrue(self.service.take_service_restart())
        self.restart()
        self.assertEqual(self.receipt()['state'], 'applied')
        self.assertEqual(self.http(self.receipt()['resume_path'], {'text': 'Continue without answering approval'})[0], 409)
        self.assertEqual(self.service._all('decisions'), decisions)
        self.assertEqual(self.provider.started, [])

    def test_original_task_reaches_real_provider_preferred_path_and_native_completion(self):
        calls = []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if 'messages' not in request:
                    self.send_error(400, 'This fixture supports chat completions.')
                    return
                calls.append(request)
                tools = [m for m in request['messages'] if m['role'] == 'tool']
                if not tools:
                    function = dict(name='kanban_complete', arguments=json.dumps({'summary': 'Verified retained local result.'}))
                    offered = {t.get('function', t).get('name') for t in request.get('tools', [])}
                    if function['name'] not in offered:
                        function = dict(name='tool_call', arguments=json.dumps({'calls': [dict(name='kanban_complete', arguments={'summary': 'Verified retained local result.'})]}))
                    message = dict(role='assistant', content=None, tool_calls=[dict(id='call_fixture', type='function', function=function)])
                else:
                    message = dict(role='assistant', content='The retained result is verified.')
                response = dict(id='fixture', object='chat.completion', created=int(time.time()), model='local-fixture',
                    choices=[dict(index=0, message=message, finish_reason='tool_calls' if not tools else 'stop')],
                    usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if request.get('stream') else 'application/json')
                self.end_headers()
                if request.get('stream'):
                    if message.get('tool_calls'):
                        message['tool_calls'][0]['index'] = 0
                    response['choices'][0]['delta'] = response['choices'][0].pop('message')
                    self.wfile.write(('data: ' + json.dumps(response) + '\n\ndata: [DONE]\n\n').encode())
                else:
                    self.wfile.write(json.dumps(response).encode())

        model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        thread = threading.Thread(target=model.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (model.shutdown(), model.server_close(), thread.join()))
        address = f'http://127.0.0.1:{model.server_port}/v1'
        class Catalog:
            root = self.root / 'model-default'
            def resolve_runtime(self, *_, **__):
                return dict(provider='custom', api_mode='chat_completions', base_url=address, api_key='synthetic-local')
        Catalog.root.mkdir()
        (Catalog.root / 'config.yaml').write_text('{}')
        class Provider(HermesProvider):
            def migrate_model_default(self, *_): pass
            def subscribe_model_catalog(self, *_): pass
            def native_settings(self, *_args, **_kwargs): return {'routines': []}
            def observe_bot(self, bot): return bot
            def model_catalog(self): return {'default': self.resolve_task_model({}, {}), 'models': []}
            def resolve_model(self, bot, catalog=None): return self.resolve_task_model(bot, {})
            def resolve_task_model(self, bot, task):
                return dict(model='local-fixture', provider='custom', base_url=address, reasoning_effort=None)
            def environment(self, bot_id):
                env, cwd = super().environment(bot_id)
                env['PYTHONDONTWRITEBYTECODE'] = '1'
                for key in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY'):
                    env.pop(key, None)
                return env, cwd

        # The operator can request recovery even though the original worker has
        # never started. The branch guard itself remains unmodified.
        self.assertEqual(self.request()[0], 200)
        self.service._dispatch_pending()
        with self.assertRaises(KeyboardInterrupt):
            self.server.service_actions()
        self.restart(Provider(self.data, python=native.PYTHON, hermes_root=Path(native.SOURCE), catalog=Catalog()))
        before = len(self.service._all('tasks'))
        result = self.write(self.receipt()['resume_path'], {'text': 'Verify the retained result within its original approvals.'})
        self.assertEqual(result['id'], self.task['id'])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            self.service._dispatch_pending()
            if self.service.kanban.tasks[self.task['id']]['status'] == 'done':
                break
        self.assertEqual(self.service.kanban.tasks[self.task['id']]['status'], 'done', self.service._get('tasks', self.task['id']))
        self.assertTrue(calls)
        self.assertEqual(len(self.service._all('tasks')), before)
        self.assertEqual(self.service.kanban.tasks[self.task['id']]['id'], self.native_id)
        self.assertEqual(self.service.kanban.tasks[self.task['id']]['workspace_path'], str(self.workspace))
        self.assertEqual((self.workspace / 'retained.patch').read_text(), 'Original approved Gumroad patch fixture\n')
        runtime = self.provider._kanban_runtimes[self.native_id]
        env, cwd = self.provider._runtime_envs[runtime]
        self.assertEqual(cwd, self.workspace)
        self.assertEqual(env['HOME'], os.environ['HOME'])
        self.assertEqual(env['HERMES_HOME'], str(self.data / 'bots' / self.bot['id'] / 'home'))
        self.assertEqual(env['CODEX_HOME'], env['HERMES_HOME'] + '/.codex')
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], env['HERMES_HOME'] + '/.claude')
        for worker in self.service.running.values():
            worker.join(5)
        # A real cached native runtime can drain without being killed to prove
        # quiescence. Its native resource probe must positively report idle.
        git(self.workspace, 'branch', '-m', 'second-reviewed-branch')
        self.payload.update(old_branch='reviewed-local-branch', new_branch='second-reviewed-branch')
        self.key = str(uuid4())
        self.assertEqual(self.request()[0], 200)
        self.assertTrue(self.service.take_service_restart(), self.receipt())
        self.assertIsNone(runtime.poll())


if __name__ == '__main__':
    unittest.main()
