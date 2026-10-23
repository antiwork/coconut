"""Routine scripts of a bot run on the worker's Python, not on <Hermes source>/venv/bin/python.

The fake modules copy how Hermes picks the Python of a cron script: the dependency environment that
<HERMES_HOME>/installs records, else <source>/venv/bin/python. A bot home has no such record, and the official
installer does not create <source>/venv.
"""
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from coconut import native_routine_execution

FAKE_HERMES = {
    'cron/__init__.py': '',
    'cron/scheduler_script.py': '''
        import sys
        from pathlib import Path
        _POSIX_SCRIPT_BOOTSTRAP = 'BOOTSTRAP'
        def _posix_cron_script_argv(script):
            from hermes_cli._launchers import resolve_store_python
            from pm.environments import project_python
            repo = Path(__file__).resolve().parents[1]
            if resolve_store_python(repo) is None:
                return [sys.executable, str(script)], {}
            python = project_python(repo)
            if not python.is_file():
                raise RuntimeError(f'dependency environment interpreter is missing: {python}')
            return [str(python), '-c', _POSIX_SCRIPT_BOOTSTRAP, str(repo), str(script)], {'HERMES_DISABLE_LAZY_INSTALLS': '1'}
        def _script_argv(path):
            return _posix_cron_script_argv(path)
    ''',
    'hermes_cli/__init__.py': '',
    'hermes_cli/_launchers.py': '''
        import os
        from pathlib import Path
        def resolve_store_python(repo):
            return Path(os.environ['FAKE_STORE_PYTHON']) if os.environ.get('FAKE_STORE_PYTHON') else None
    ''',
    'pm/__init__.py': '',
    'pm/environments.py': '''
        import os
        from pathlib import Path
        def project_python(repo):
            # Without an install record for the active home, Hermes falls back to the in-tree venv.
            return Path(os.environ.get('FAKE_PROJECT_PYTHON') or Path(repo) / 'venv/bin/python')
    ''',
}


class RoutineScriptPythonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'hermes-agent'
        for name, text in FAKE_HERMES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text))
        self.script = Path(temporary.name) / 'scripts/check.py'
        for patcher in (patch.object(sys, 'path', [str(self.root), *sys.path]), patch.dict(sys.modules),
                        patch.dict('os.environ', {'FAKE_STORE_PYTHON': '/store/bin/python3'})):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ('cron', 'cron.scheduler_script', 'hermes_cli', 'hermes_cli._launchers', 'pm', 'pm.environments'):
            sys.modules.pop(name, None)
        import cron.scheduler_script
        self.scripts = cron.scheduler_script

    def test_a_bot_home_without_an_install_record_runs_scripts_on_the_worker_python(self):
        # Without Coconut's change, Hermes refuses the script, because <source>/venv/bin/python does not exist.
        with self.assertRaisesRegex(RuntimeError, 'interpreter is missing: .*hermes-agent/venv/bin/python'):
            self.scripts._script_argv(self.script)
        native_routine_execution.use_worker_python_for_scripts()
        argv, overlay = self.scripts._script_argv(self.script)
        self.assertEqual(argv, [sys.executable, '-c', 'BOOTSTRAP', str(self.root.resolve()), str(self.script)])
        self.assertEqual(overlay, {'HERMES_DISABLE_LAZY_INSTALLS': '1'})

    def test_a_recorded_dependency_environment_keeps_the_choice_of_hermes(self):
        recorded = self.root.parent / 'installs/key/venv/bin/python'
        recorded.parent.mkdir(parents=True)
        recorded.touch()
        native_routine_execution.use_worker_python_for_scripts()
        with patch.dict('os.environ', {'FAKE_PROJECT_PYTHON': str(recorded)}):
            self.assertEqual(self.scripts._script_argv(self.script)[0][0], str(recorded))
        # A Hermes without a dependency store runs scripts on its own Python, as before.
        with patch.dict('os.environ', {'FAKE_STORE_PYTHON': ''}):
            self.assertEqual(self.scripts._script_argv(self.script), ([sys.executable, str(self.script)], {}))

    def test_the_change_applies_once(self):
        native_routine_execution.use_worker_python_for_scripts()
        installed = self.scripts._posix_cron_script_argv
        native_routine_execution.use_worker_python_for_scripts()
        self.assertIs(self.scripts._posix_cron_script_argv, installed)


if __name__ == '__main__':
    unittest.main()
