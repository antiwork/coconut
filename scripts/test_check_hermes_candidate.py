import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('check_hermes_candidate', Path(__file__).with_name('check_hermes_candidate.py'))
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
SECRET = 'sk-ant-candidate-check-secret'

# Each program receives the candidate root and the Coconut checkout, as the upgrade programs do.
LAYOUT = r'''
import json, os, sys, tempfile
from pathlib import Path
root, code = map(Path, sys.argv[1:3])
runtime = root.parent.parent
sys.path.insert(0, BACKEND)
from coconut.model_catalog import HermesModelCatalog, _contained
assert root.parent == runtime / 'checks'
assert (root / '.runtime/hermes/source').resolve() == runtime / 'source'
assert (root / 'backend').resolve() == code / 'backend' and (root / 'scripts').resolve() == code / 'scripts'
assert Path(tempfile.gettempdir()).resolve().is_relative_to(runtime)
assert Path(os.environ['HERMES_HOME']).resolve().is_relative_to(runtime)
assert not {'ANTHROPIC_API_KEY', 'HERMES_KANBAN_DB', 'GITHUB_TOKEN'} & set(os.environ)
with tempfile.TemporaryDirectory() as data:
    catalog = HermesModelCatalog(data, python=root / '.runtime/hermes/venv/bin/python', source=root / '.runtime/hermes/source')
    _contained(catalog.root, catalog.source.parent)
    _contained(catalog.private_home, catalog.source.parent)
    assert catalog.root.is_relative_to(runtime) and Path(data).resolve().is_relative_to(runtime)
print('COCONUT_CREATION_RESULT=' + json.dumps(dict(check='layout', passed=True)))
'''.replace('BACKEND', repr(str(BACKEND)))

MODEL = r'''
import os
assert os.environ['ANTHROPIC_API_KEY'] == SECRET and 'GITHUB_TOKEN' not in os.environ
print('The key is ' + os.environ['ANTHROPIC_API_KEY'])
print('PASS: model credentials reached the check.')
'''.replace('SECRET', repr(SECRET))


def git(path, *arguments):
    return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *arguments],
                          cwd=path, check=True, capture_output=True, text=True).stdout.strip()


class CandidateCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.code = base / 'code'
        for name in ('backend/module.py', 'scripts/tool.py', 'README.md'):
            (self.code / name).parent.mkdir(parents=True, exist_ok=True)
            (self.code / name).write_text('first\n')
        git(self.code, 'init', '-q')
        git(self.code, 'add', '.')
        git(self.code, 'commit', '-q', '-m', 'Candidate')
        # Match a bot worktree: the candidate runtime sits in the ignored .runtime folder.
        self.runtime = self.code / '.runtime/hermes-candidate'
        source = self.runtime / 'source'
        source.mkdir(parents=True)
        (source / 'run_agent.py').write_text('')
        git(source, 'init', '-q')
        git(source, 'remote', 'add', 'origin', check.hermes.REPOSITORY)
        git(source, 'add', '.')
        git(source, 'commit', '-q', '-m', 'Hermes')
        (self.runtime / 'venv/bin').mkdir(parents=True)
        (self.runtime / 'venv/bin/python').symlink_to(sys.executable)
        self.host = base / 'host'
        self.host.mkdir()
        (self.host / 'credentials.json').write_text(json.dumps({'ANTHROPIC_API_KEY': SECRET}))
        (self.host / 'credentials.json').chmod(0o600)
        environment = {key: value for key, value in os.environ.items()
                       if key not in check.host.MODEL_KEYS and key not in check.FENCE_KEYS}
        environment.update(HERMES_KANBAN_DB=str(base / 'live/kanban.db'), GITHUB_TOKEN='unrelated-token')
        environment_patch = patch.dict(os.environ, environment, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)

    def run_candidate(self, steps):
        def plan(root, code, python, texts):
            return [(name, model, [(label, [str(python), '-c', text, str(root), str(code)], kind, timeout)
                                   for label, text, kind, timeout in programs])
                    for name, model, programs in steps]
        with patch.object(check, 'plan', plan):
            return check.check_candidate(self.runtime, self.host, code=self.code, texts={})

    def test_result_names_hashes_and_keeps_every_temporary_folder_inside_the_runtime(self):
        path, result = self.run_candidate([('contracts', False, [('layout', LAYOUT, 'report', 60)]),
                                           ('relay', True, [('model', MODEL, 'pass', 60)])])
        self.assertTrue(result['passed'], json.dumps(result, indent=2))
        self.assertEqual(path, self.runtime / 'checks/results' / f"{result['source_sha']}-{result['hermes_sha']}.json")
        self.assertEqual(json.loads(path.read_text()), result)
        self.assertEqual(result['source_sha'], git(self.code, 'rev-parse', 'HEAD'))
        self.assertEqual(result['hermes_sha'], git(self.runtime / 'source', 'rev-parse', 'HEAD'))
        self.assertEqual(result['credentials'], ['ANTHROPIC_API_KEY'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(result['scratch_removed'])
        self.assertEqual(list((self.runtime / 'checks').glob('candidate-*')), [])
        saved = [path, *path.with_suffix('.logs').iterdir()]
        self.assertEqual(len(saved), 3)
        for file in saved:
            self.assertNotIn(SECRET, file.read_text())
        self.assertIn('[redacted]', (path.with_suffix('.logs') / 'model.log').read_text())

    def test_fenced_worker_is_refused_and_keeps_its_fence(self):
        marker = self.runtime / 'ran'
        program = f'from pathlib import Path\nPath({str(marker)!r}).write_text("ran")\nprint("PASS: ran")'
        with patch.dict(os.environ, {'HERMES_DELEGATED_CHILD_CONTEXT': '/live/kanban'}):
            path, result = self.run_candidate([('contracts', False, [('ran', program, 'pass', 60)])])
            self.assertEqual(os.environ['HERMES_DELEGATED_CHILD_CONTEXT'], '/live/kanban')
        self.assertFalse(result['passed'])
        self.assertEqual(result['error'], check.FENCED_MESSAGE)
        self.assertEqual(result['fence'], ['HERMES_DELEGATED_CHILD_CONTEXT'])
        self.assertEqual(json.loads(path.read_text()), result)
        self.assertFalse(marker.exists())
        self.assertNotIn('credentials', result)

    def test_uncommitted_executed_code_is_refused(self):
        (self.code / 'README.md').write_text('second\n')
        (self.code / 'backend/module.py').write_text('second\n')
        _, result = self.run_candidate([('contracts', False, [('layout', 'print("PASS: ran")', 'pass', 60)])])
        self.assertFalse(result['passed'])
        self.assertEqual(result['error'], 'Commit the Coconut changes before the checks: backend/module.py')
        self.assertNotIn('checks', result)
        (self.code / 'backend/module.py').write_text('first\n')
        (self.code / 'scripts/new.py').write_text('')
        _, result = self.run_candidate([('contracts', False, [('layout', 'print("PASS: ran")', 'pass', 60)])])
        self.assertEqual(result['error'], 'Commit the Coconut changes before the checks: scripts/new.py')
        (self.code / 'scripts/new.py').unlink()
        _, result = self.run_candidate([('contracts', False, [('layout', 'print("PASS: ran")', 'pass', 60)])])
        self.assertTrue(result['passed'])
        self.assertEqual(result['source_changes'], ['README.md'])

    def test_shared_credential_file_is_refused_in_the_result(self):
        (self.host / 'credentials.json').chmod(0o644)
        path, result = self.run_candidate([('contracts', False, [('layout', 'print("PASS: ran")', 'pass', 60)])])
        self.assertFalse(result['passed'])
        self.assertEqual(result['error'], 'The model credential file must be private to this Mac account.')
        self.assertEqual(json.loads(path.read_text()), result)

    def test_changed_hermes_source_is_refused(self):
        (self.runtime / 'source/run_agent.py').write_text('changed')
        _, result = self.run_candidate([('contracts', False, [('layout', 'print("PASS: ran")', 'pass', 60)])])
        self.assertFalse(result['passed'])
        self.assertEqual(result['error'], 'The candidate Hermes source has local changes: run_agent.py')

    def test_failed_contract_skips_the_native_checks(self):
        marker = self.runtime / 'kanban-ran'
        kanban = f'from pathlib import Path\nPath({str(marker)!r}).write_text("ran")'
        _, result = self.run_candidate([('contracts', False, [('contract', 'raise SystemExit(3)', 'exit', 60),
                                                              ('kanban', kanban, 'exit', 60)]),
                                        ('relay', True, [('relay', 'print("PASS: ran")', 'pass', 60)])])
        self.assertFalse(result['passed'])
        self.assertEqual(result['checks']['contracts']['status'], 'failed')
        self.assertEqual([entry['label'] for entry in result['checks']['contracts']['programs']], ['contract'])
        self.assertEqual(result['checks']['contracts']['programs'][0]['returncode'], 3)
        self.assertFalse(marker.exists())
        self.assertEqual(result['checks']['relay'], dict(passed=False, status='skipped', programs=[]))

    def test_report_must_pass_and_timeout_fails_the_check(self):
        failed = 'print("COCONUT_CREATION_RESULT={\\"passed\\": false}")'
        _, result = self.run_candidate([('contracts', False, [('contract', 'pass', 'exit', 60)]),
                                        ('routines', False, [('routines', failed, 'report', 60)]),
                                        ('relay', True, [('relay', 'import time; time.sleep(30)', 'pass', 1)])])
        self.assertFalse(result['passed'])
        self.assertEqual(result['checks']['routines']['programs'][0]['report'], {'passed': False})
        self.assertTrue(result['checks']['relay']['programs'][0]['timed_out'])
        self.assertTrue(result['checks']['contracts']['passed'])

    @unittest.skipUnless(shutil.which('lsof'), 'The cleanup finds leftover processes with lsof.')
    def test_timed_out_check_stops_a_worker_in_another_session(self):
        pid_file = self.runtime / 'worker.pid'
        program = f'''
import subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
worker = subprocess.Popen([sys.executable, '-c', 'import sys, time; handle = open(sys.argv[1], "w"); time.sleep(120)',
                           str(root / 'tmp/state')], start_new_session=True)
Path({str(pid_file)!r}).write_text(str(worker.pid))
while not (root / 'tmp/state').exists():
    time.sleep(.05)
time.sleep(60)
'''
        _, result = self.run_candidate([('contracts', False, [('contract', 'pass', 'exit', 60)]),
                                        ('relay', True, [('relay', program, 'pass', 3)])])
        entry = result['checks']['relay']['programs'][0]
        worker = int(pid_file.read_text())
        self.assertTrue(entry['timed_out'])
        self.assertEqual(entry['stopped_processes'], [worker])
        self.assertTrue(result['scratch_removed'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(worker, 0)
            except ProcessLookupError:
                break
            time.sleep(.1)
        else:
            self.fail('The detached worker survived the check.')

    def test_untracked_hermes_file_is_refused(self):
        (self.runtime / 'source/extra_module.py').write_text('')
        _, result = self.run_candidate([('contracts', False, [('layout', 'print("PASS: ran")', 'pass', 60)])])
        self.assertEqual(result['error'], 'The candidate Hermes source has local changes: extra_module.py')

    def test_custom_output_keeps_the_mode_of_its_folder(self):
        shared = self.runtime.parent / 'shared'
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        def plan(root, code, python, texts):
            return [('contracts', False, [('contract', [str(python), '-c', 'pass'], 'exit', 60)])]
        with patch.object(check, 'plan', plan):
            path, result = check.check_candidate(self.runtime, self.host, code=self.code, texts={},
                                                 output=shared / 'result.json')
        self.assertTrue(result['passed'])
        self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_plan_runs_every_upgrade_program_unchanged(self):
        texts = check.programs()
        python = self.runtime / 'venv/bin/python'
        steps = check.plan(Path('/root'), self.code, python, texts)
        self.assertEqual([(name, model) for name, model, _ in steps],
                         [('contracts', False), ('routines', False), ('script_routines', False),
                          ('relay', True), ('profiles', True)])
        programs = {label: arguments for _, _, entries in steps for label, arguments, _, _ in entries}
        import test_native_profiles, test_native_relay, test_native_routines, test_script_routines
        self.assertEqual(programs['contract'][2], check.hermes.CHECK_CODE)
        self.assertEqual(programs['routines'], [str(python), '-c', test_native_routines.PROGRAM, '/root', str(self.code)])
        self.assertEqual(programs['script-routines'][2], test_script_routines.PROGRAM)
        self.assertEqual(programs['relay'][2], test_native_relay.PROGRAM)
        self.assertEqual([programs['profiles-' + name][2] for name in ('metadata', 'approval', 'routine')],
                         list(test_native_profiles.PROGRAMS))
        self.assertEqual(programs['kanban'][1:], [str(self.code / 'scripts/test_native_kanban.py'), '--source',
                                                  '/root/.runtime/hermes/source', '--python', str(python)])


if __name__ == '__main__':
    unittest.main()
