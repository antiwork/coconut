"""A bot worker selects the tools of its own Hermes home, also when another home owns the Hermes source.

The official installer stamps its source folder, so Hermes reads the tool store of the installer's home by default. Each
bot has its own HERMES_HOME, and the bot's terminal `hermes` installs tools into <HERMES_HOME>/tools. The worker, its
terminal, and its routine scripts must agree with that store. No tool is downloaded or run: the test records the pinned
agent-browser of this Hermes in a scratch bot home, as an install would.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from uuid import uuid4

from hermes_env import needs_hermes, PYTHON, SOURCE
from coconut.providers import HermesProvider

BACKEND = Path(__file__).resolve().parents[1]

CHILD = r'''
import json, os, sys
from pathlib import Path
home = Path(os.environ['HERMES_HOME'])
if sys.argv[1] == 'record':
    from pm import paths
    from pm.lock import Lockfile
    from pm.registry import get_package
    from pm.store import Store, current_target
    package, lock, target = get_package('agent-browser'), Lockfile(paths.lockfile_path()), current_target()
    version = lock.version('agent-browser')
    entry = package.store_entry(version, target)
    binary = package.binary(Store(home / 'tools').entry(entry), target)
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    fact = dict(entry=entry, version=version, target=target,
                artifacts=[artifact['sha256'] for artifact in lock.artifacts('agent-browser', target)])
    (home / 'tools/facts.json').write_text(json.dumps({'schema': 1, 'packages': {'agent-browser': fact}}))
    print(json.dumps({'binary': str(binary)}))
    raise SystemExit
from pm import paths
from tools.browser_tool_install import _find_agent_browser
from tools.environments.local import LocalEnvironment
from coconut import native_routine_execution
try:
    browser = _find_agent_browser(validate=False)
except FileNotFoundError:
    browser = None
terminal = LocalEnvironment(cwd=os.getcwd(), timeout=30, persistent=True)
try:
    shell = terminal.execute('printf "%s" "$HERMES_RUNTIME_DIR"')
finally:
    terminal.cleanup()
native_routine_execution.use_worker_python_for_scripts()
from cron import scheduler_script
argv, _ = scheduler_script._posix_cron_script_argv(Path('routine.py'))
print(json.dumps({'store': str(paths.store_root()), 'browser': browser, 'shell': shell.get('output', ''),
                  'routine': argv[:2] == [sys.executable, '-c']}))
'''


@needs_hermes
class NativeToolStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.provider = HermesProvider(self.root / 'data', python=PYTHON, hermes_root=SOURCE, home=self.root / 'terminal')

    def run_bot(self, bot_id, operation):
        env, cwd = self.provider.environment(bot_id)
        env['PYTHONPATH'] = os.pathsep.join((str(BACKEND), env['PYTHONPATH']))
        env.update(PYTHONDONTWRITEBYTECODE='1', PATH='/usr/bin:/bin:/usr/sbin:/sbin')
        result = subprocess.run([PYTHON, '-c', CHILD, operation], env=env, cwd=cwd, capture_output=True, text=True,
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1]), env

    def test_the_worker_selects_the_browser_that_the_bot_installed(self):
        bot = str(uuid4())
        recorded, env = self.run_bot(bot, 'record')
        seen, _ = self.run_bot(bot, 'select')
        store = str(Path(env['HERMES_HOME']) / 'tools')
        self.assertEqual(seen['store'], store)
        self.assertEqual(seen['browser'], recorded['binary'])
        # The terminal of the worker, and a `hermes` started there, read the same store.
        self.assertEqual(seen['shell'], store)
        # Routine scripts keep the worker's Python, although the bot's store has no Python.
        self.assertTrue(seen['routine'], seen)

    def test_bots_do_not_share_a_tool_store(self):
        owner, other = str(uuid4()), str(uuid4())
        recorded, _ = self.run_bot(owner, 'record')
        seen, env = self.run_bot(other, 'select')
        self.assertEqual(seen['store'], str(Path(env['HERMES_HOME']) / 'tools'))
        self.assertNotEqual(seen['browser'], recorded['binary'])
        self.assertNotIn(owner, json.dumps(seen))


if __name__ == '__main__':
    unittest.main()
