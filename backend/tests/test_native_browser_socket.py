"""browser_exec's harness daemon can listen in every bot, and no bot or work execution reaches another's daemon.

The browser-harness daemon listens on <BH_RUNTIME_DIR>/bu-<session>.sock. Without BH_RUNTIME_DIR the folder is in the
harness home: a bot whose XDG_CONFIG_HOME is a profile folder gets a path longer than an AF_UNIX socket allows, and the
bots that use the account's ~/.config share one folder. The test runs the harness's own server and ping in each bot's
environment. No browser starts and nothing is fetched.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from hermes_env import needs_hermes, PYTHON, SOURCE
from coconut.providers import HermesProvider

BACKEND = Path(__file__).resolve().parents[1]
# sizeof(sockaddr_un.sun_path) in <sys/un.h>, with the closing NUL: 104 bytes on macOS, 108 on Linux.
SUN_PATH = 104 if sys.platform == 'darwin' else 108
# Hermes accepts session names of up to 64 characters.
LONG_SESSION = 's' * 64

SERVE = r'''
import asyncio, json, os, sys, threading, time
from coconut import hermes_worker
# The worker calls this at start. The original worker has no such step, which the negative control runs.
getattr(hermes_worker, 'configure_browser_runtime', lambda: None)()
from browser_harness import _ipc
name, mode = sys.argv[1], sys.argv[2]
path = str(_ipc._sock_path(name))
report = {'path': path, 'runtime': os.environ.get('BH_RUNTIME_DIR'), 'error': None, 'served': False}
if mode == 'ping':
    report['served'] = _ipc.ping(name)
    print(json.dumps(report), flush=True)
    raise SystemExit
async def handler(reader, writer):
    await reader.readline()
    writer.write((json.dumps({'pong': True, 'pid': os.getpid()}) + '\n').encode())
    await writer.drain()
    writer.close()
def serve():
    try:
        asyncio.run(_ipc.serve(name, handler))
    except OSError as error:
        report['error'] = str(error)
threading.Thread(target=serve, daemon=True).start()
deadline = time.monotonic() + 10
while time.monotonic() < deadline and report['error'] is None and not report['served']:
    report['served'] = _ipc.ping(name)
    time.sleep(0.05)
print(json.dumps(report), flush=True)
if mode == 'hold' and report['served']:
    sys.stdin.read()
'''


@needs_hermes
class NativeBrowserSocketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.provider = HermesProvider(self.root / 'data', python=PYTHON, hermes_root=SOURCE, home=self.root / 'terminal')

    def environment(self, bot, execution=None, config=None):
        env, cwd = self.provider.environment(bot)
        env['PYTHONPATH'] = os.pathsep.join((str(BACKEND), env['PYTHONPATH']))
        env.update(PYTHONDONTWRITEBYTECODE='1', PATH='/usr/bin:/bin:/usr/sbin:/sbin',
                   # A profile .env can name its own folder, as the original Dev bot does. Otherwise ~/.config applies.
                   XDG_CONFIG_HOME=str(config or Path(env['HERMES_HOME']) / '.config'))
        if execution:
            env['COCONUT_WORKSPACE_OUTCOME'] = execution
        return env, cwd

    def run_harness(self, env, cwd, name, mode):
        result = subprocess.run([PYTHON, '-c', SERVE, name, mode], env=env, cwd=cwd, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.forget(json.loads(result.stdout.strip().splitlines()[-1]))

    def hold_harness(self, env, cwd, name):
        process = subprocess.Popen([PYTHON, '-c', SERVE, name, 'hold'], env=env, cwd=cwd, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Closing stdin stops the daemon.
        self.addCleanup(process.communicate, timeout=10)
        return self.forget(json.loads(process.stdout.readline()))

    def forget(self, report):
        if report['runtime']:
            self.addCleanup(shutil.rmtree, report['runtime'], True)
        return report

    def test_the_daemon_of_a_bot_profile_folder_listens(self):
        env, cwd = self.environment(str(uuid4()), 'execution')
        default = Path(env['XDG_CONFIG_HOME']) / 'browser-harness/runtime' / f'bu-{LONG_SESSION}.sock'
        # The folder that the harness would use for this bot cannot hold a socket.
        self.assertGreaterEqual(len(str(default).encode()), SUN_PATH)
        for name in ('default', LONG_SESSION):
            report = self.run_harness(env, cwd, name, 'serve')
            self.assertIsNone(report['error'])
            self.assertTrue(report['served'], report)
            self.assertLess(len(report['path'].encode()), SUN_PATH)
            self.assertEqual(Path(report['path']).parent, Path(report['runtime']))
        folder = os.stat(report['runtime'])
        self.assertEqual((folder.st_uid, stat.S_IMODE(folder.st_mode)), (os.getuid(), 0o700))

    def test_workers_do_not_share_a_daemon(self):
        # The bots without their own XDG_CONFIG_HOME use the account's ~/.config. A short folder stands in for it.
        shared = Path(tempfile.mkdtemp(prefix='cbt-', dir='/tmp'))
        self.addCleanup(shutil.rmtree, shared, True)
        owner, other = str(uuid4()), str(uuid4())
        env, cwd = self.environment(owner, 'first', shared)
        held = self.hold_harness(env, cwd, 'default')
        self.assertTrue(held['served'], held)
        # Another bot, another execution, a conversation, and a later worker of the same execution.
        for bot, execution in ((other, 'first'), (owner, 'second'), (owner, None), (owner, 'first')):
            env, cwd = self.environment(bot, execution, shared)
            seen = self.run_harness(env, cwd, 'default', 'ping')
            self.assertFalse(seen['served'], (bot, execution, seen))
            self.assertNotEqual(seen['runtime'], held['runtime'])
            # A worker releases its empty folder when it exits.
            self.assertFalse(os.path.exists(seen['runtime']))
        # The folder of a listening daemon stays.
        self.assertTrue(os.path.exists(held['path']))

if __name__ == '__main__':
    unittest.main()
