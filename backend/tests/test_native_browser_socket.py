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
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from hermes_env import needs_hermes, PYTHON, SOURCE
from coconut.providers import HermesProvider

BACKEND = Path(__file__).resolve().parents[1]
# sizeof(sockaddr_un.sun_path) in <sys/un.h>, with the closing NUL: 104 bytes on macOS, 108 on Linux.
SUN_PATH = 104 if sys.platform == 'darwin' else 108
# Hermes accepts session names of up to 64 characters.
LONG_SESSION = 's' * 64
# A held harness reports once it listens. A child that stalls fails the test instead of blocking CI.
READY_SECONDS = 60
STOP_SECONDS = 10

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


# A harness daemon: it answers the harness's ping with its PID, and exits on the shutdown request of --reload. A delay
# stands in for a daemon whose shutdown waits on a slow remote stop.
DAEMON = r'''
import asyncio, json, os, sys, threading, time
from browser_harness import _ipc
name = sys.argv[1] if len(sys.argv) > 1 else 'default'
delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0
async def handler(reader, writer):
    request = json.loads(await reader.readline() or b'{}')
    stop = request.get('meta') == 'shutdown'
    writer.write((json.dumps({'ok': True} if stop else {'pong': True, 'pid': os.getpid()}) + '\n').encode())
    await writer.drain()
    writer.close()
    if stop:
        _ipc.cleanup_endpoint(name)
        threading.Thread(target=lambda: (time.sleep(delay), os._exit(0))).start()
def serve():
    try:
        asyncio.run(_ipc.serve(name, handler))
    except OSError:
        # Like the harness daemon, a daemon that cannot listen exits.
        os._exit(1)
threading.Thread(target=serve, daemon=True).start()
while not _ipc.ping(name):
    time.sleep(0.05)
print('listening', flush=True)
time.sleep(120)
'''

# As the harness CLI does, a short-lived process starts a daemon in its own session and exits, so the daemon is no child
# of the worker and a signal for the worker's group misses it. argv: the daemon code, then its arguments.
LAUNCH = ('import subprocess, sys; daemon = subprocess.Popen([sys.executable, "-c", *sys.argv[1:]], '
          'stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, '
          'start_new_session=True); assert daemon.stdout.readline().strip() == "listening"; print(daemon.pid)')

# A worker whose browser_exec drove that daemon. Its shutdown calls release_browser_runtime before os._exit.
RELEASE = r'''
import json, os, signal, subprocess, sys, time
from coconut import hermes_worker
hermes_worker.configure_browser_runtime()
runtime = os.environ['BH_RUNTIME_DIR']
pid, stopped = None, False
try:
    pid = int(subprocess.run([sys.executable, '-c', sys.argv[2], sys.argv[1]], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=30, check=True).stdout)
    from tools import browser_use_cli
    from tools.browser_harness_dirs import daemon_identity
    from hermes_constants import hermes_home_key
    # browser_exec records each daemon that it drives.
    browser_use_cli._driven_daemons[daemon_identity(browser_use_cli._base_subprocess_env())] = hermes_home_key()
    began = time.monotonic()
    hermes_worker.release_browser_runtime()
    seconds = time.monotonic() - began
    deadline = time.monotonic() + 15
    while not stopped and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            stopped = True
finally:
    if pid and not stopped:
        os.kill(pid, signal.SIGKILL)
print(json.dumps({'runtime': runtime, 'stopped': stopped, 'folder': os.path.exists(runtime), 'seconds': seconds}))
'''

# A real worker, as hermes_worker.py starts it, whose browser_exec drove several daemons. argv: the daemon code, the
# launcher, the number of daemons, their stop delay, and 'raise' when Hermes fails to stop a background process.
STOPPING = r'''
import json, os, signal, subprocess, sys, threading, time
from coconut import hermes_worker
hermes_worker.configure_browser_runtime()
daemon, launch, count, delay, cleanup = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
from tools import browser_use_cli
from tools.browser_harness_dirs import daemon_identity
from hermes_constants import hermes_home_key
env = browser_use_cli._base_subprocess_env()
pids = []
for index in range(count):
    name = f'session{index}'
    pids.append(int(subprocess.run([sys.executable, '-c', launch, daemon, name, delay], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=30, check=True).stdout))
    # browser_exec records each daemon that it drives.
    browser_use_cli._driven_daemons[daemon_identity({**env, 'BU_NAME': name})] = hermes_home_key()
# The harness leaves this lock file of a daemon start in the folder.
from browser_harness import admin
with admin._spawn_lock('session0', timeout=1):
    pass
assert os.path.exists(os.path.join(os.environ['BH_RUNTIME_DIR'], 'bu-session0.spawnlock'))
if cleanup == 'raise':
    def fail(*args, **kwargs):
        raise RuntimeError('Hermes did not stop a background process.')
    hermes_worker.stop_native_processes = fail
if cleanup == 'browser-fails':
    # Hermes cannot build the environment of a browser-harness command, so no daemon stop can start.
    def broken(*args, **kwargs):
        raise RuntimeError('Hermes could not build the browser environment.')
    browser_use_cli._base_subprocess_env = broken
    stop_native = hermes_worker.stop_native_processes
    def record(*args, **kwargs):
        open(os.path.join(os.environ['TMPDIR'], 'native-stopped'), 'w').close()
        return stop_native(*args, **kwargs)
    hermes_worker.stop_native_processes = record
if cleanup in ('late', 'bound'):
    def late():
        # A turn that still runs starts a daemon after the shutdown took the registry. 'late' records it as
        # browser_exec does. With 'bound', only its socket in the worker's folder shows it.
        while browser_use_cli._driven_daemons:
            time.sleep(0.01)
        pid = int(subprocess.run([sys.executable, '-c', launch, daemon, 'late', '0'], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=30, check=True).stdout)
        if cleanup == 'late':
            browser_use_cli._driven_daemons[daemon_identity({**env, 'BU_NAME': 'late'})] = hermes_home_key()
        with open(os.path.join(os.environ['TMPDIR'], 'late-daemon.pid'), 'w') as handle:
            handle.write(str(pid))
    threading.Thread(target=late, daemon=True).start()
report = os.fdopen(os.dup(1), 'w')
def ready():
    # main() installs the SIGTERM handler of the worker. Report once it is in place.
    while signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
        time.sleep(0.01)
    report.write(json.dumps({'pids': pids, 'runtime': os.environ['BH_RUNTIME_DIR']}) + '\n')
    report.flush()
threading.Thread(target=ready, daemon=True).start()
hermes_worker.main()
'''
# A stopping worker whose turn still runs. The turn starts a daemon after the worker released its folder, as a
# browser-harness command that the turn started just before the worker exits does. argv: the daemon code, the launcher.
BUSY = r'''
import json, os, stat, subprocess, sys, threading, time
from coconut import hermes_worker
hermes_worker.configure_browser_runtime()
daemon, launch = sys.argv[1], sys.argv[2]
runtime = os.environ['BH_RUNTIME_DIR']
released = threading.Event()
outcome = {}
def turn():
    released.wait(30)
    result = subprocess.run([sys.executable, '-c', launch, daemon, 'turn', '0'], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    outcome.update(code=result.returncode, pids=[int(result.stdout)] if result.returncode == 0 else [])
# The harness leaves this lock file of a daemon start in the folder.
from browser_harness import admin
with admin._spawn_lock('earlier', timeout=1):
    pass
thread = threading.Thread(target=turn, daemon=True)
thread.start()
began = time.monotonic()
try:
    hermes_worker.release_browser_runtime(deadline=began + 5, busy=thread.is_alive)
except TypeError:
    # The negative control runs the previous signature, which has no busy.
    hermes_worker.release_browser_runtime(deadline=began + 5)
seconds = time.monotonic() - began
released.set()
thread.join(30)
info = os.lstat(runtime) if os.path.lexists(runtime) else None
print(json.dumps({'runtime': runtime, 'seconds': seconds, **outcome,
                  'sealed': bool(info) and stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o500,
                  'entries': os.listdir(runtime) if info else []}), flush=True)
# Like the worker's shutdown, skip the stop of Hermes at exit.
os._exit(0)
'''
# providers._terminate kills a worker 10 seconds after its SIGTERM.
GRACE_SECONDS = 10
# Stopped one after another, three such daemons outlast the grace.
SLOW_STOP_SECONDS = 4


def test_daemons(pids):
    """The PIDs that still run a daemon of this test. Their command line identifies them, so no other process counts."""
    alive = []
    for pid in pids:
        command = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True).stdout
        if '_ipc.serve(name, handler)' in command:
            alive.append(pid)
    return alive


def kill_test_daemons(pids):
    for pid in test_daemons(pids):
        os.kill(pid, signal.SIGKILL)

def stop(process, seconds=STOP_SECONDS):
    """Close stdin, which stops a held daemon. Terminate, then kill, a child that does not exit. Reap it either way."""
    try:
        return process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        return process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.kill()
    return process.communicate()


def hold(test, command, env=None, cwd=None, ready=READY_SECONDS, stop_seconds=STOP_SECONDS):
    """Start command and return its first stdout line as JSON. The test's cleanup stops and reaps the child."""
    process = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    test.addCleanup(stop, process, stop_seconds)
    lines = []
    reader = threading.Thread(target=lambda: lines.append(process.stdout.readline()), daemon=True)
    reader.start()
    reader.join(ready)
    if not lines or not lines[0]:
        _, error = stop(process, stop_seconds)
        test.fail(f'The child gave no report within {ready} seconds: {(error or "")[-2000:]}')
    return json.loads(lines[0]), process


class HoldTests(unittest.TestCase):
    """hold() stops waiting for a child that never reports, and reaps that child. No Hermes is needed."""

    def hold(self, command, **options):
        """hold() on command. Return the report, or None when hold() fails the test, and the child."""
        started, real = [], subprocess.Popen

        def record(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]
        with patch.object(subprocess, 'Popen', record):
            try:
                report, _ = hold(self, command, **options)
            except AssertionError as error:
                self.assertRegex(str(error), 'no report within 1 seconds')
                report = None
        return report, started[0]

    def test_a_reporting_child_stops_when_its_stdin_closes(self):
        code = 'import sys; print(\'{"ready": true}\', flush=True); sys.stdin.read()'
        report, process = self.hold([sys.executable, '-c', code])
        self.assertEqual(report, {'ready': True})
        self.doCleanups()
        self.assertEqual(process.returncode, 0)

    def test_a_silent_child_fails_at_the_deadline_and_is_reaped(self):
        began = time.monotonic()
        report, process = self.hold([sys.executable, '-c', 'import time; time.sleep(600)'], ready=1, stop_seconds=1)
        self.assertIsNone(report)
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(process.returncode, -signal.SIGTERM)

    def test_a_child_that_ignores_termination_is_killed(self):
        began = time.monotonic()
        # The shell ignores SIGTERM before exec, so sleep starts with it ignored.
        report, process = self.hold(['/bin/sh', '-c', 'trap "" TERM; exec sleep 600'], ready=1, stop_seconds=1)
        self.assertIsNone(report)
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(process.returncode, -signal.SIGKILL)


class SealedFolderTests(unittest.TestCase):
    """A later worker deletes only the sealed, empty socket folders of stopped workers of this account. No Hermes."""

    def test_only_stale_sealed_empty_folders_go(self):
        from coconut import hermes_worker
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        sealed, live, listening = root / 'cbh-sealed', root / 'cbh-live', root / 'cbh-listening'
        for folder in (sealed, live, listening):
            folder.mkdir(mode=0o700)
        (listening / 'bu-default.sock').touch()
        os.chmod(sealed, hermes_worker.SEALED_MODE)
        os.chmod(listening, hermes_worker.SEALED_MODE)
        self.addCleanup(os.chmod, listening, 0o700)
        hermes_worker.remove_sealed_browser_runtimes(root)
        # A folder that a worker sealed just now stays: a command of that worker can still start.
        self.assertTrue(sealed.exists())
        later = time.time() + hermes_worker.SEALED_SECONDS + 1
        with patch.object(hermes_worker.time, 'time', return_value=later):
            hermes_worker.remove_sealed_browser_runtimes(root)
        self.assertFalse(sealed.exists())
        # The folder of a running worker and a folder with a socket stay.
        self.assertTrue(live.exists())
        self.assertTrue(listening.exists())


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
        return self.forget(hold(self, [PYTHON, '-c', SERVE, name, 'hold'], env, cwd)[0])

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

    def test_a_stopping_worker_stops_its_daemons(self):
        env, cwd = self.environment(str(uuid4()), 'execution')
        env['BH_TELEMETRY'] = '0'
        result = subprocess.run([PYTHON, '-c', RELEASE, DAEMON, LAUNCH], env=env, cwd=cwd, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = self.forget(json.loads(result.stdout.strip().splitlines()[-1]))
        # No later worker could reach the daemon in this folder, so the worker stops it and deletes the folder.
        self.assertTrue(report['stopped'], report)
        self.assertFalse(report['folder'], report)
        # Coconut gives a stopping worker 10 seconds before it kills it.
        self.assertLess(report['seconds'], 10, report)


    def stop_worker(self, cleanup, runtime=None, count=3):
        """SIGTERM a worker that drove three slow daemons, as Coconut does. Return its report, exit code and times.

        With runtime, the worker's environment names the socket folder already, so the worker claims none.
        """
        env, cwd = self.environment(str(uuid4()), 'execution')
        env['BH_TELEMETRY'] = '0'
        if runtime:
            env.update(BH_RUNTIME_DIR=runtime, BH_RUNTIME_DIR_SHARED='1')
        report, worker = hold(self, [PYTHON, '-c', STOPPING, DAEMON, LAUNCH, str(count), str(SLOW_STOP_SECONDS),
                                     cleanup], env, cwd)
        if not runtime:
            self.forget(report)
        self.addCleanup(kill_test_daemons, report['pids'])
        self.assertEqual(test_daemons(report['pids']), report['pids'])
        began = time.monotonic()
        worker.send_signal(signal.SIGTERM)
        try:
            worker.wait(GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
        exited = time.monotonic() - began
        late = Path(env['TMPDIR']) / 'late-daemon.pid'
        if cleanup in ('late', 'bound'):
            self.assertTrue(late.exists(), report)
            report['pids'].append(int(late.read_text()))
            self.addCleanup(kill_test_daemons, report['pids'][-1:])
        while test_daemons(report['pids']) and time.monotonic() - began < GRACE_SECONDS:
            time.sleep(0.1)
        report['native_stopped'] = (Path(env['TMPDIR']) / 'native-stopped').exists()
        return report, worker.returncode, exited, test_daemons(report['pids'])

    def test_a_stopping_worker_stops_slow_daemons_within_the_grace(self):
        report, code, exited, alive = self.stop_worker('ok')
        self.assertEqual(alive, [], report)
        self.assertLess(exited, GRACE_SECONDS)
        # The worker exits once its daemons stopped. A lock file of the harness does not hold it to the deadline.
        self.assertLess(exited, SLOW_STOP_SECONDS + 3)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(report['runtime']))

    def test_a_failed_process_cleanup_still_stops_the_daemons(self):
        report, code, exited, alive = self.stop_worker('raise')
        self.assertEqual(alive, [], report)
        self.assertLess(exited, GRACE_SECONDS)
        # The worker still reports the failed cleanup.
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(report['runtime']))


    def test_a_worker_without_its_own_folder_still_stops_its_daemons(self):
        folder = tempfile.mkdtemp(prefix='cbt-', dir='/tmp')
        self.addCleanup(shutil.rmtree, folder, True)
        report, code, exited, alive = self.stop_worker('ok', folder)
        self.assertEqual(report['runtime'], folder)
        self.assertEqual(alive, [], report)
        self.assertLess(exited, GRACE_SECONDS)
        self.assertEqual(code, 0)
        # The folder is not the worker's own, so it stays.
        self.assertTrue(os.path.isdir(folder))


    def test_a_daemon_started_during_shutdown_is_stopped(self):
        for cleanup in ('late', 'bound'):
            with self.subTest(cleanup=cleanup):
                report, code, exited, alive = self.stop_worker(cleanup)
                self.assertEqual(len(report['pids']), 4)
                self.assertEqual(alive, [], report)
                self.assertLess(exited, GRACE_SECONDS)
                self.assertEqual(code, 0)
                self.assertFalse(os.path.exists(report['runtime']))

    def test_a_running_turn_cannot_start_a_daemon_after_the_worker_stops(self):
        env, cwd = self.environment(str(uuid4()), 'execution')
        env['BH_TELEMETRY'] = '0'
        result = subprocess.run([PYTHON, '-c', BUSY, DAEMON, LAUNCH], env=env, cwd=cwd, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = self.forget(json.loads(result.stdout.strip().splitlines()[-1]))
        self.addCleanup(kill_test_daemons, report['pids'])
        # The worker does not wait for the turn. Its folder stays, empty and closed, so the daemon cannot listen.
        self.assertLess(report['seconds'], 1)
        self.assertTrue(report['sealed'], report)
        self.assertEqual(report['entries'], [])
        self.assertNotEqual(report['code'], 0)
        self.assertEqual(test_daemons(report['pids']), [])

    def test_a_failed_browser_stop_still_stops_native_processes(self):
        report, code, exited, alive = self.stop_worker('browser-fails', count=1)
        # The worker still stops its native processes and exits at once, and it reports the failure.
        self.assertTrue(report['native_stopped'], report)
        self.assertLess(exited, 3)
        self.assertEqual(code, 1)

if __name__ == '__main__':
    unittest.main()
