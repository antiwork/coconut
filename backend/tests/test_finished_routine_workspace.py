"""A finished routine continuation keeps its own execution workspace after the scheduled one retires."""
import copy
import itertools
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut import native_routine_receipts as receipts
from coconut.native_bridge import NativeBridge, TurnPaused
from coconut import native_routine_execution
from coconut.native_routine_execution import job_context
from coconut.providers import HermesProvider
import test_native_scheduler as scheduler_fixtures
import test_worker_model_settings as worker_fixtures


class FinishedRoutineWorkspaceTests(unittest.TestCase):
    """The provider prepares a real worker process. Its native modules are the shared stub."""

    def setUp(self):
        worker_fixtures.WorkerModelProcessTests.setUp(self)
        self.addCleanup(self.temporary.cleanup)
        self.capture = self.root / 'capture.jsonl'
        # Record the scope that the worker gives the native terminal.
        (self.source / 'gateway/session_context.py').write_text(textwrap.dedent('''
            import json, os
            def set_session_vars(**kwargs):
                with open(os.environ['WORKER_CAPTURE'] + '.cwd', 'a') as handle:
                    handle.write(json.dumps(['session', kwargs]) + '\\n')
                return kwargs
            def clear_session_vars(tokens): pass
        '''))
        (self.source / 'tools/terminal_tool.py').write_text(textwrap.dedent('''
            import json, os
            def _record(*row):
                with open(os.environ['WORKER_CAPTURE'] + '.cwd', 'a') as handle:
                    handle.write(json.dumps(list(row)) + '\\n')
            def record_session_cwd(task_id, cwd): _record('terminal', task_id, cwd)
            def clear_session_cwd(task_id): _record('terminal_clear', task_id)
            def register_task_env_overrides(task_id, overrides): _record('override', task_id, overrides)
        '''))
        self.data = self.root / 'data'
        self.home = self.root / 'bot-home'
        self.home.mkdir()
        catalog = SimpleNamespace(root=self.home, resolve_runtime=lambda *args, **kwargs: dict(self.native))
        self.provider = HermesProvider(self.data, python=sys.executable, hermes_root=self.source, catalog=catalog)
        self.addCleanup(self.provider.close)
        self.provider.environment = lambda bot_id: ({
            'PATH': os.environ.get('PATH', ''), 'HOME': str(self.home), 'HERMES_HOME': str(self.home), 'HERMES_DISABLE_LAZY_INSTALLS': '1',
            'TMPDIR': str(self.root), 'PYTHONPATH': str(self.source), 'WORKER_CAPTURE': str(self.capture),
            'EXPECTED_NATIVE_KEY': self.native['api_key']}, self.root)
        self.execution = str(uuid4())
        workspaces = self.data / 'workspaces'
        self.workspace = workspaces / self.execution
        # Hermes creates the dir workspace of the native card before the claim.
        self.workspace.mkdir(parents=True)
        # The scheduled directory is the worktree of another outcome. It was retired normally.
        self.retired = self.root / 'worktrees' / 'scheduler-outcome'
        self.manifest = workspaces / 'worktrees.json'
        self.manifest.write_text(json.dumps({'scheduler-outcome': dict(
            outcome='scheduler-outcome', path=str(self.retired), branch='scheduler-branch',
            retired=dict(head='0' * 40, done=True))}))
        self.saved_manifest = self.manifest.read_text()

    def task(self, *, finished=True, workdir=None, proof='success'):
        identity = str(uuid4())
        job = dict(id='job', prompt='Verify the installation.', enabled=True, deliver='local',
                   workdir=str(self.retired if workdir is None else workdir),
                   fire_claim={'by': 'owner', 'at': '2026-10-15T20:52:22-04:00'})
        task = dict(id=identity, title='Verify the installation', bot_id=self.bot['id'],
                    _native_job=job, _native_job_finished=finished, _native_turn_id='turn',
                    _native_outcome_id='t_native', _execution_session_id=self.execution,
                    _kanban_external_id=identity, _kanban_workspace=str(self.workspace))
        if proof == 'success':
            task['_native_job_completion'] = dict(job_id='job', owner='owner', success=True, native_confirmed=True)
        elif proof == 'continuation':
            task['_native_job_completion'] = dict(job_id='job', owner='owner',
                completion=dict(job_id='job', owner='owner', success=False, native_confirmed=True),
                continuation=dict(task_id=identity, turn_id='turn', native_task_id='t_native', delivery_id='delivery'))
        elif proof is not None:
            task['_native_job_completion'] = proof
        return task

    def run_turn(self, task, *, retired_absent=True):
        sent = copy.deepcopy(task)
        events = []
        self.provider.prepare_kanban_worker(task['_kanban_external_id'], self.bot, str(self.workspace), self.execution)
        with patch.object(self.provider, 'observe_bot', return_value=self.bot), patch.object(
                self.provider, 'resolve_task_model', return_value=dict(
                    model='gpt-6-astra', provider='openai-codex', base_url=self.native['base_url'])):
            self.provider._run_turn('start', task, self.bot, [], lambda kind, data: events.append((kind, data)),
                                    isolated=True)
        # The provider gives the worker the saved routine unchanged. Its claim and schedule stay the original.
        self.assertEqual(task, sent)
        scopes = Path(str(self.capture) + '.cwd')
        rows = [json.loads(line) for line in scopes.read_text().splitlines()] if scopes.exists() else []
        scopes.unlink(missing_ok=True)
        prompts = Path(str(self.capture) + '.prompts')
        self.prompts = [json.loads(line) for line in prompts.read_text().splitlines()] if prompts.exists() else []
        if retired_absent:
            self.assertFalse(self.retired.exists(), 'A retired worktree must not be recreated or restored.')
        self.assertEqual(self.manifest.read_text(), self.saved_manifest)
        return events, rows

    def routine_scope(self, rows):
        return [row[1]['cwd'] for row in rows if row[0] == 'session' and row[1].get('cron_session') is False]

    def assertPreparationFailed(self, events, rows):
        failures = [data for kind, data in events if kind == 'failed']
        self.assertEqual(len(failures), 1, events)
        self.assertEqual(failures[0]['error_code'], 'worker_preparation_failed')
        self.assertRegex(failures[0]['error_detail'], r'^ValueError at native_routine_execution\.py:\d+ in job_context$')
        self.assertFalse(any(kind == 'completed' for kind, _ in events), events)
        self.assertEqual(self.routine_scope(rows), [])
        self.assertFalse(any(row[0] == 'terminal' for row in rows), rows)

    def test_completed_occurrence_continues_in_its_execution_workspace(self):
        task = self.task()
        events, rows = self.run_turn(task)
        self.assertIn(('completed', {'summary': 'Complete.'}), events)
        self.assertEqual(self.routine_scope(rows), [str(self.workspace)])
        self.assertIn(['terminal', task['id'], str(self.workspace)], rows)
        self.assertIn('Verify the installation.', self.prompts[-1])
        self.assertNotIn('Coconut retired the scheduled working directory', self.prompts[-1])

    def test_failed_occurrence_with_its_own_continuation_uses_its_workspace(self):
        task = self.task(proof='continuation')
        events, rows = self.run_turn(task)
        self.assertIn(('completed', {'summary': 'Complete.'}), events)
        self.assertEqual(self.routine_scope(rows), [str(self.workspace)])

    def save_bindings(self, bindings):
        self.manifest.write_text(json.dumps(bindings))
        self.saved_manifest = self.manifest.read_text()

    def incident_binding(self, **retired):
        # The saved shape of the failed checkpoints: a capacity recheck names the worktree of the outcome that
        # scheduled it, and an idle pass discarded that worktree before the first occurrence.
        repository = self.root / 'bot' / 'workspace' / 'desk-reviews' / 'repo'
        self.retired = repository / '.worktrees' / 'scheduler-outcome-11ce49fd843261dc'
        return {'scheduler-outcome-11ce49fd843261dc': dict(
            _head_log=dict(digest='f' * 64, size=358), branch='gianfranco/newsletter-cutover-exact-email',
            outcome='scheduler-outcome', path=str(self.retired), repository=str(repository),
            retired=dict(archive=str(self.data / 'workspaces/archive/done/scheduler-outcome-11ce49fd843261dc.tar.gz'),
                         at='2026-10-20T04:29:00.846584Z', changes='a' * 64, done=True, head='6c0f73f7' + '0' * 32,
                         **retired))}

    def test_first_occurrence_of_a_retired_worktree_uses_its_execution_workspace(self):
        self.save_bindings(self.incident_binding())
        # The service records the finished flag before the occurrence proves its own completion. It sends an empty
        # proof for an unproved occurrence (native_routine_completion_input), and an older saved task has no key.
        for finished, proof in itertools.product((False, True), ({}, None)):
            with self.subTest(finished=finished, proof=proof):
                task = self.task(finished=finished, proof=proof)
                self.assertEqual(task.get('_native_job_completion', 'absent'), 'absent' if proof is None else {})
                events, rows = self.run_turn(task)
                self.assertIn(('completed', {'summary': 'Complete.'}), events)
                self.assertEqual(self.routine_scope(rows), [str(self.workspace)])
                self.assertIn(['terminal', task['id'], str(self.workspace)], rows)
                self.assertIn('Verify the installation.', self.prompts[-1])
                self.assertIn(f'Coconut retired the scheduled working directory {self.retired}.', self.prompts[-1])
                self.assertIn('worktree of outcome scheduler-outcome', self.prompts[-1])
                self.assertNotIn('restore failed', self.prompts[-1])
        self.assertFalse(self.retired.parent.exists(), 'The retired worktree and its repository folder stay absent.')

    def test_failed_restore_keeps_its_fence_and_routes_it_to_bot_maintenance(self):
        self.save_bindings(self.incident_binding(restore_failed=dict(at='2026-10-20T05:00:00Z', error='Not empty.')))
        events, rows = self.run_turn(self.task(finished=False, proof=None))
        self.assertIn(('completed', {'summary': 'Complete.'}), events)
        self.assertEqual(self.routine_scope(rows), [str(self.workspace)])
        self.assertIn('Its earlier restore failed, so the path stays fenced: do not restore it here. Report the failure '
                      'on that outcome for bot maintenance.', self.prompts[-1])
        self.assertIn('scheduler-outcome-11ce49fd843261dc.tar.gz and the branch gianfranco/newsletter-cutover-exact-email',
                      self.prompts[-1])
        self.assertNotIn('operator', self.prompts[-1])

    def test_active_occurrence_still_requires_an_explained_directory(self):
        binding = self.incident_binding()
        (name, saved), = binding.items()
        live = dict(saved)
        live.pop('retired')
        other = dict(saved, outcome='another-outcome')
        cases = {
            'unregistered': ({}, None),
            'live binding': ({name: live}, None),
            'two bindings': ({name: saved, 'another-outcome-11ce49fd843261dc': other}, None),
            'other spelling': ({name: dict(saved, path=saved['path'] + '/')}, None),
            'empty outcome': ({name: dict(saved, outcome='')}, None),
            'malformed retirement': ({name: dict(saved, retired=True)}, None),
            'unrelated path': ({name: dict(saved, path=str(self.root / 'unrelated'))}, None),
            'no execution workspace': (binding, self.root / 'missing-execution'),
        }
        for label, (bindings, workspace) in cases.items():
            with self.subTest(label):
                self.save_bindings(bindings)
                task = self.task(finished=False, proof=None)
                if workspace:
                    task['_kanban_workspace'] = str(workspace)
                self.assertPreparationFailed(*self.run_turn(task))
                self.assertFalse(self.root.joinpath('missing-execution').exists())
        with self.subTest('occupied path'):
            # A retired binding does not explain a path that holds something else.
            self.save_bindings(binding)
            self.retired.parent.mkdir(parents=True)
            self.retired.write_text('not a checkout')
            try:
                task = self.task(finished=False, proof=None)
                self.assertPreparationFailed(*self.run_turn(task, retired_absent=False))
                self.assertEqual(self.retired.read_text(), 'not a checkout')
            finally:
                self.retired.unlink()
        with self.subTest('broken link'):
            self.save_bindings(binding)
            self.retired.parent.mkdir(parents=True, exist_ok=True)
            self.retired.symlink_to(self.root / 'gone')
            try:
                self.assertPreparationFailed(*self.run_turn(self.task(finished=False, proof=None), retired_absent=False))
                self.assertTrue(self.retired.is_symlink())
                self.assertFalse((self.root / 'gone').exists())
            finally:
                self.retired.unlink()
        with self.subTest('success proof of an active occurrence'):
            self.save_bindings({})
            self.assertPreparationFailed(*self.run_turn(self.task(finished=False, proof='success')))
        # A completion proof that does not prove this occurrence is not a first occurrence either.
        other = dict(job_id='job', owner='owner', completion={},
                     continuation=dict(task_id='another-task', turn_id='turn', native_task_id='t_native', delivery_id='d'))
        for proof in (dict(job_id='job', owner='later-owner', success=True), dict(job_id='job', owner='owner', success=False),
                      other):
            with self.subTest(retired_with_unproved=proof):
                self.save_bindings(binding)
                self.assertPreparationFailed(*self.run_turn(self.task(proof=proof)))

    def test_finished_flag_without_this_occurrence_proof_still_fails(self):
        # No retired binding explains this missing directory, so only this occurrence's own finish proof could.
        self.save_bindings({})
        other = dict(job_id='job', owner='owner', completion={},
                     continuation=dict(task_id='another-task', turn_id='turn', native_task_id='t_native', delivery_id='d'))
        for proof in (None, {}, dict(job_id='job', owner='later-owner', success=True),
                      dict(job_id='other-job', owner='owner', success=True),
                      dict(job_id='job', owner='owner', success=False), other):
            with self.subTest(proof=proof):
                self.assertPreparationFailed(*self.run_turn(self.task(proof=proof)))
        for field in ('delivery_id', 'turn_id'):
            with self.subTest(unbound=field):
                task = self.task(proof='continuation')
                task['_native_job_completion']['continuation'][field] = ''
                if field == 'turn_id':
                    task['_native_turn_id'] = ''
                self.assertPreparationFailed(*self.run_turn(task))

    def test_ordinary_routine_keeps_its_working_directory(self):
        scheduled = self.root / 'scheduled'
        scheduled.mkdir()
        for finished in (False, True):
            with self.subTest(finished=finished):
                task = self.task(finished=finished, workdir=scheduled)
                events, rows = self.run_turn(task)
                self.assertIn(('completed', {'summary': 'Complete.'}), events)
                self.assertEqual(self.routine_scope(rows), [str(scheduled)])
                self.assertIn(['terminal', task['id'], str(scheduled)], rows)


class RetiredCheckoutTests(unittest.TestCase):
    """Only the exact absent path of one retired binding in the worker's own registry explains a missing directory."""

    def test_registry_proof(self):
        retired_checkout = native_routine_execution.retired_checkout
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'workspaces'
            root.mkdir()
            path = str(Path(folder) / 'repo/.worktrees/outcome-key')
            binding = dict(outcome='outcome', path=path, retired=dict(done=True))
            (root / 'worktrees.json').write_text(json.dumps({'outcome-key': binding}))
            with patch.dict(os.environ, {'COCONUT_WORKSPACE_ROOT': str(root)}):
                self.assertEqual(retired_checkout(path), binding)
                self.assertIsNone(retired_checkout(path + '/'))
                self.assertIsNone(retired_checkout(str(Path(folder) / 'elsewhere')))
                self.assertIsNone(retired_checkout(''))
                Path(path).parent.mkdir(parents=True)
                os.symlink(Path(folder) / 'gone', path)
                self.assertIsNone(retired_checkout(path))
                os.unlink(path)
                Path(path).write_text('a file')
                self.assertIsNone(retired_checkout(path))
                os.unlink(path)
                # The provider reads the same registry first and stops on a malformed one. The worker refuses it too.
                no_outcome = {k: v for k, v in binding.items() if k != 'outcome'}
                for broken in (dict(binding, outcome=''), dict(binding, outcome=None), no_outcome, dict(binding, retired=True)):
                    (root / 'worktrees.json').write_text(json.dumps({'outcome-key': broken}))
                    self.assertIsNone(retired_checkout(path))
                (root / 'worktrees.json').write_text(json.dumps({'outcome-key': binding, 'other-key': dict(binding, outcome='other')}))
                self.assertIsNone(retired_checkout(path))
                (root / 'worktrees.json').write_text('not json')
                self.assertIsNone(retired_checkout(path))
                (root / 'worktrees.json').write_text(json.dumps([binding]))
                self.assertIsNone(retired_checkout(path))
            with patch.dict(os.environ):
                os.environ.pop('COCONUT_WORKSPACE_ROOT', None)
                self.assertIsNone(retired_checkout(path))


class FinishedRoutineClaimTests(unittest.TestCase):
    """The workspace does not authorize tools. The bridge confirms the native claim as before."""
    setUp = scheduler_fixtures.NativeRoutineAdapterTests.setUp

    def request(self, workspace):
        job = dict(self.job, workdir=str(Path(self.tmp.name) / 'retired'), fire_claim={'by': 'owner'})
        task = dict(id='task', _native_job=job, _native_job_finished=True,
                    _native_job_completion=dict(job_id='job', owner='owner', success=True))
        return {'task': task, 'model': 'model', 'workspace': str(workspace)}

    def test_continuation_scope_keeps_claim_fence(self):
        workspace = Path(self.tmp.name) / 'execution'
        workspace.mkdir()
        request = self.request(workspace)
        saved = copy.deepcopy(request)
        owner = {'by': 'later-owner'}
        @contextmanager
        def fire_claim_fence(identity, *, expected_owner):
            yield owner['by'] == expected_owner
        jobs = SimpleNamespace(fire_claim_fence=fire_claim_fence, get_job=lambda identity: None)
        bridge = NativeBridge(request, lambda *args: None, lambda *args: None)
        with job_context(request, {'provider': 'native'}, {}):
            self.assertIn(('terminal', 'task', str(workspace)), self.events)
            with patch.dict('sys.modules', {'cron.jobs': jobs}), \
                    patch.object(receipts, 'completed', return_value=False):
                with self.assertRaises(TurnPaused):
                    with bridge.claim():
                        self.fail('A lost claim must not execute.')
            owner['by'] = 'owner'
            with patch.dict('sys.modules', {'cron.jobs': jobs}):
                with bridge.claim():
                    pass
        self.assertEqual(request, saved)

    def test_missing_execution_workspace_has_no_scheduled_cwd(self):
        request = self.request(Path(self.tmp.name) / 'missing-execution')
        with job_context(request, {'provider': 'native'}, {}):
            self.assertIn(('scope', {'cwd': '', 'cron_session': False, 'session_key': 'task', 'platform': 'coconut'}),
                          self.events)
            self.assertFalse(any(event[0] == 'terminal' for event in self.events if isinstance(event, tuple)))
        self.assertFalse((Path(self.tmp.name) / 'retired').exists())
        self.assertFalse((Path(self.tmp.name) / 'missing-execution').exists())


if __name__ == '__main__':
    unittest.main()
