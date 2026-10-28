"""The chat runtime setting, and the Hermes gateway that runs chat turns for it.

`chat_runtime` is `worker` (the default) or `gateway`. `PUT /v1/settings/chat-runtime {"chat_runtime": ...}` sets it.
With `gateway`, a plain chat turn (gateway_bridge.plain_chat_turn) goes to the `coconut` platform of the Hermes
gateway when the adapter of the bot's profile is connected. Each other turn, and each turn that no adapter takes, runs
in the Coconut worker as before.

With `gateway`, the service supervises one `hermes gateway run` process for the person's Hermes home. Hermes
multiplexes the profiles in that process. The service starts the process with its own start or with the setting, stops
it with its own stop or with the setting, and writes its output to `<data>/gateway/gateway.log`. When a gateway that
the service did not start already runs for that home, the service uses it and starts no second one. GET /v1/setup
says which gateway runs chats, or why none does.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from . import hermes_runtime
from .gateway_bridge import WORKER_MESSAGE_FIELDS, GatewayBridge, GatewayUnavailable, plain_chat_turn

LOG = logging.getLogger(__name__)
RUNTIMES = ('worker', 'gateway')
CHECK_SECONDS = 5
RESTART_SECONDS = 30
STOP_SECONDS = 20
LOG_LIMIT_BYTES = 10 * 1024 * 1024
# `hermes <arguments>` in the Python of the person's Hermes. The source folder comes first on sys.path, as the `hermes`
# launcher puts it.
LAUNCH = ('import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ["hermes", *sys.argv[2:]]; '
          'from hermes_cli.main import main; sys.exit(main())')
# The workers get the same model keys. The service sets HERMES_DISABLE_LAZY_INSTALLS for its own Hermes processes.
ENVIRONMENT_KEYS = (*hermes_runtime.ENVIRONMENT_KEYS, 'TMPDIR', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY',
                    'OPENROUTER_API_KEY', 'HERMES_DISABLE_LAZY_INSTALLS')
USING = 'Coconut uses the Hermes gateway that already runs for {home} (process {pid}).'
NOT_LOADED = ('A Hermes gateway already runs for {home} (process {pid}), but it did not load the coconut platform. '
              'Restart that gateway after Coconut setup (hermes gateway restart), or stop it so that Coconut starts its '
              'own. Until then, chats use the Coconut worker.')
STOPPED = 'The Hermes gateway stopped with exit code {code}. Coconut starts it again soon. See {log}.'


def live_gateway_pid(home) -> int | None:
    """The process ID of a running gateway of this Hermes home, or None. Hermes checks the identity of the process."""
    try:
        from gateway.status import live_gateway_pid_for_home
        return live_gateway_pid_for_home(Path(home))
    except Exception:
        pass
    # Without the Hermes modules, the pid file of the gateway is the evidence.
    try:
        text = (Path(home) / 'gateway.pid').read_text().strip()
        pid = int(json.loads(text)['pid']) if text.startswith('{') else int(text)
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError, KeyError, TypeError):
        return None


class GatewayChat:
    gateway_process = None
    gateway_supervisor = None

    def _init_gateway_chat(self):
        self.gateway_bridge = GatewayBridge(self.root)
        self.gateway_wake = threading.Event()
        self.gateway_state = {'state': 'off', 'pid': None, 'reason': None}
        self._gateway_retry_at = 0.0
        self._gateway_exit = None

    def chat_runtime(self) -> str:
        value = self._setting('chat_runtime', 'worker')
        return value if value in RUNTIMES else 'worker'

    def set_chat_runtime(self, data: dict) -> dict:
        """PUT /v1/settings/chat-runtime. The caller holds the transaction."""
        from .service import ServiceError, encoded
        if set(data) != {'chat_runtime'} or data['chat_runtime'] not in RUNTIMES:
            raise ServiceError(400, 'Set chat_runtime to worker or gateway.')
        self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        ('chat_runtime', encoded(data['chat_runtime'])))
        self._event('settings.updated', {'chat_runtime': data['chat_runtime']})
        self.gateway_wake.set()
        return {'chat_runtime': data['chat_runtime']}

    def gateway_status(self) -> dict:
        homes = set(self.gateway_bridge.connected_homes())
        profiles = sorted(row['name'] for row in self.profiles.rows.values()
                          if row.get('state') == 'active' and os.path.realpath(row['home']) in homes)
        return dict(self.gateway_state, chat_runtime=self.chat_runtime(), connected_profiles=profiles)

    def run_gateway_chat(self, task: dict, bot: dict, history: list, emit) -> bool:
        """Run a new turn through the gateway when the chat runtime and the turn allow it. False: use the worker."""
        if self.chat_runtime() != 'gateway' or not plain_chat_turn(task):
            return False
        from .profiles import ProfileError
        try:
            home = self.profiles.home(bot)
        except ProfileError:
            return False
        if not self.gateway_bridge.connected(home):
            return False
        turn = set(task.get('_turn_message_ids', []))
        messages = [message for message in history if message.get('id') in turn]
        if any(message.get(field) for message in messages for field in WORKER_MESSAGE_FIELDS):
            return False
        text = '\n\n'.join(message['text'] for message in messages)
        if not text.strip():
            return False
        try:
            self.gateway_bridge.run_turn(task['id'], home, bot['id'], text, emit, chat_name=bot.get('name') or 'Coconut')
        except GatewayUnavailable:
            LOG.warning('No Hermes gateway took chat turn %s. The worker runs it.', task['id'])
            return False
        return True

    def _start_gateway_supervisor(self):
        # Only a service on the person's Hermes runs a gateway. Fixture mode and tests have no Hermes home.
        if self.gateway_supervisor is not None or not getattr(self, 'scan_profiles', False):
            return
        if not getattr(self.provider, 'python', None) or not getattr(self.provider, 'hermes_root', None):
            return
        self.gateway_supervisor = threading.Thread(target=self._supervise_gateway, name='coconut-gateway', daemon=True)
        self.gateway_supervisor.start()

    def _supervise_gateway(self):
        while not self.stopping.is_set():
            try:
                self._check_gateway()
            except Exception:
                LOG.exception('The check of the Hermes gateway failed.')
            self.gateway_wake.wait(CHECK_SECONDS)
            self.gateway_wake.clear()

    def _close_gateway_chat(self):
        self.gateway_bridge.close()
        self.gateway_wake.set()
        if self.gateway_supervisor is not None:
            self.gateway_supervisor.join(timeout=CHECK_SECONDS + 5)
        self._stop_gateway()

    def _check_gateway(self):
        home, process = self.hermes_home, self.gateway_process
        if self.chat_runtime() != 'gateway':
            self._stop_gateway()
            self.gateway_state = {'state': 'off', 'pid': None, 'reason': None}
            return
        if process is not None and process.poll() is None:
            self.gateway_state = {'state': 'running', 'pid': process.pid, 'reason': None}
            return
        if process is not None:
            self.gateway_process, self._gateway_exit = None, process.returncode
            self._gateway_retry_at = time.monotonic() + RESTART_SECONDS
            LOG.warning('The Hermes gateway stopped with exit code %s.', process.returncode)
        pid = live_gateway_pid(home)
        if pid is not None:
            reason = (USING if self.gateway_bridge.connected(home) else NOT_LOADED).format(home=home, pid=pid)
            self.gateway_state = {'state': 'external', 'pid': pid, 'reason': reason}
            return
        if time.monotonic() < self._gateway_retry_at:
            self.gateway_state = {'state': 'stopped', 'pid': None, 'reason': STOPPED.format(
                code=self._gateway_exit, log=self.gateway_bridge.folder / 'gateway.log')}
            return
        entries = getattr(self, 'hermes_entries', None)
        if entries is not None and entries.is_alive():
            # The gateway loads the plugin and the platform settings at its start, so they come first. The next
            # check starts it.
            self.gateway_state = {'state': 'starting', 'pid': None,
                                  'reason': 'Coconut sets its entries in the Hermes profiles first.'}
            return
        if self.stopping.is_set():
            return
        self._start_gateway(home)

    def _start_gateway(self, home):
        folder = self.gateway_bridge.folder
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = folder / 'gateway.log'
        if log.exists() and log.stat().st_size > LOG_LIMIT_BYTES:
            os.replace(log, folder / 'gateway.log.1')
        environment = {key: os.environ[key] for key in ENVIRONMENT_KEYS if key in os.environ}
        environment.update(HERMES_HOME=str(home), HERMES_DISABLE_TELEMETRY='1', PYTHONUNBUFFERED='1')
        with open(log, 'ab') as output:
            os.chmod(log, 0o600)
            self.gateway_process = subprocess.Popen(
                [str(self.provider.python), '-c', LAUNCH, str(self.provider.hermes_root), 'gateway', 'run'],
                cwd=folder, env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True)
        LOG.info('Started the Hermes gateway for %s (process %s).', home, self.gateway_process.pid)
        self.gateway_state = {'state': 'running', 'pid': self.gateway_process.pid, 'reason': None}

    def _stop_gateway(self):
        process, self.gateway_process = self.gateway_process, None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=STOP_SECONDS)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
