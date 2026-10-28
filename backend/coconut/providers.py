"""Execution adapters. The service owns decisions, permissions, and file writes."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
from copy import deepcopy
import time
from pathlib import Path
from typing import Callable, Optional, Protocol
from uuid import UUID, uuid4
from .worker_protocol import MAX_EVENT_CHARACTERS

Emit = Callable[[str, dict], Optional[dict]]


class ExecutionProvider(Protocol):
    name: str
    ready: bool
    model: str

    def start_task(self, task: dict, bot: dict, history: list, emit: Emit) -> None: ...
    def resume_task(self, task: dict, bot: dict, history: list, decision: dict, emit: Emit) -> None: ...
    def answer_context(self, task: dict, bot: dict, history: list, question: str, emit: Emit) -> None: ...
    def cancel_task(self, task_id: str) -> None: ...
    def retrieve_artifact(self, artifact_id: str) -> bytes: ...


class FixtureProvider:
    """Deterministic service fixture. This class never runs Hermes or a model."""

    name = 'fixture'
    ready = True
    model = 'Deterministic fixture'

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()

    def configure(self, *, model: str, base_url: str | None = None):
        pass

    def start_task(self, task, bot, history, emit):
        prompt = next((m.get('text', '') for m in reversed(history) if m.get('role') == 'user' and m.get('task_id') == task['id']), task.get('title', ''))
        if self._team_milestone(task, bot, prompt, emit):
            return
        if re.search(r'^\s*(create|write|draft|prepare)\b', prompt, re.I):
            match = re.search(r'\b([\w-]+\.(?:md|txt|json|csv))\b', prompt)
            filename = match.group(1) if match else 'morning-note.md'
            emit('decision', self.proposal(filename, '# Morning review\n\nThe independent check is complete. Review this proposed file before saving it.\n'))
            return
        text = 'Fixture: 17 × 19 = 323. The check is complete.'
        emit('text_delta', {'text': text})
        emit('completed', {'summary': 'Complete.'})

    @staticmethod
    def _team_milestone(task, bot, prompt, emit):
        from .hermes_worker import bot_directory, bot_message_payload, explicit_bot_messages
        incoming = explicit_bot_messages(task)
        message = incoming[-1] if incoming else None
        text = message['text'] if message else prompt
        if text.startswith('TEAM_MILESTONE_RESULT:'):
            emit('text_delta', {'text': 'Fixture: Lin reports that 17 × 19 = 323.'})
            emit('completed', {'summary': 'Fixture: the team result is ready.'})
            return True
        if not text.startswith(('TEAM_MILESTONE_HANDOFF:', 'TEAM_MILESTONE_REQUEST:')):
            return False
        directory = bot_directory({'task': task, 'bot': bot})
        if text.startswith('TEAM_MILESTONE_REQUEST:') and message:
            target = message['sender_bot_id']
            shared = 'TEAM_MILESTONE_RESULT: 17 multiplied by 19 equals 323.'
        else:
            target = next((peer['id'] for peer in directory if peer['name'] == 'Lin'), None)
            if target is None:
                raise ValueError('The fixture milestone needs an available bot named Lin.')
            shared = 'TEAM_MILESTONE_REQUEST: What is 17 multiplied by 19? Send the result to this message sender once. Do not acknowledge the result.'
        payload = bot_message_payload({'target_bot_id': target, 'text': shared}, {peer['id'] for peer in directory})
        receipt = emit('bot_message', payload)
        status = receipt.get('status', 'unconfirmed') if isinstance(receipt, dict) else 'unconfirmed'
        emit('completed', {'summary': 'Fixture: message delivery is ' + status + '.'})
        return True

    @staticmethod
    def proposal(filename, content):
        from .hermes_worker import file_proposal
        return file_proposal({
            'title': 'Review the morning note',
            'explanation': 'Choose the full note or a brief version before the service saves it.',
            'recommended_option_id': 'full', 'predicted_option_id': 'full',
            'options': [
                {'id': 'full', 'label': 'Full note', 'explanation': 'Keep all proposed details.', 'answer': 'approve', 'action': {'type': 'create_file', 'filename': filename, 'content': content}},
                {'id': 'brief', 'label': 'Brief note', 'explanation': 'Keep one short review note.', 'answer': 'approve', 'action': {'type': 'create_file', 'filename': filename, 'content': 'Morning review is ready for human review.\n'}},
                {'id': 'skip', 'label': 'Do not save', 'explanation': 'Finish without creating this file.', 'answer': 'reject'},
            ],
        })

    def resume_task(self, task, bot, history, decision, emit):
        if decision['status'] == 'replied':
            emit('text_delta', {'text': 'Fixture reply: I received your message. No action ran.'})
            emit('completed', {'summary': 'Reply handled.'})
            return
        if decision['status'] == 'modified':
            action = decision['action']
            content = action['content'] + '\nRequested change: ' + (decision.get('correction') or '') + '\n'
            emit('decision', self.proposal(action['filename'], content))
            return
        if decision['status'] != 'executed':
            raise RuntimeError('The service has not confirmed execution.')
        emit('text_delta', {'text': 'The file is ready. Its execution receipt prevents a repeated write.'})
        emit('completed', {'summary': 'Ready to review.'})

    def answer_context(self, task, bot, history, question, emit):
        proposal = task.get('pending_decision', {})
        resource = proposal.get('resource', 'the proposed file')
        emit('text_delta', {'text': f'Fixture context: approval saves {resource} in Coconut storage. It does not send or publish it. The decision remains pending.'})

    def cancel_task(self, task_id):
        pass

    def retrieve_artifact(self, artifact_id):
        UUID(artifact_id)
        path = self.data_dir / 'artifacts' / artifact_id
        if path.is_symlink():
            raise ValueError('Artifact links are not supported.')
        return path.read_bytes()


class HermesProvider(FixtureProvider):
    """Retain native Hermes runtimes with structured JSONL transport."""

    name = 'hermes'
    supports_native_outcomes = True

    def __init__(self, data_dir, *, python=None, hermes_root=None, home=None, model=None, base_url=None, catalog=None):
        super().__init__(data_dir)
        from .hermes_runtime import DEFAULT_HOME, configured
        from .model_catalog import MISSING_SOURCE, HermesModelCatalog
        # An explicit source keeps the shared home beside it, as the tests lay it out. See HermesModelCatalog.
        if home is None:
            home = Path(hermes_root).parent / 'terminal' if hermes_root is not None else DEFAULT_HOME
        python, hermes_root = configured(python, hermes_root)
        self.python = str(python or '')
        self.hermes_root = Path(hermes_root or MISSING_SOURCE).resolve()
        self.catalog = catalog or HermesModelCatalog(self.data_dir, python=self.python, source=self.hermes_root, home=home)
        if model:
            self.catalog.migrate_legacy_default(model, base_url)
        self._lock = threading.Lock()
        self._profile_settings_lock = threading.Lock()
        self._profile_settings: dict[str, tuple] = {}
        self._routines_lock = threading.Lock()
        self._routines: dict[str, tuple] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._cancelled_contexts: set[str] = set()
        self._runtimes: dict[str, subprocess.Popen] = {}
        self._paused_processes: dict[str, subprocess.Popen] = {}
        self._runtime_epochs: dict[str, tuple] = {}
        self._bot_locks: dict[str, threading.Lock] = {}
        self._closed = False
        self._input_lock = threading.Lock()
        self._runtime_envs = {}
        self._transports = {}
        self._prepared = {}
        self._kanban_runtimes = {}
        self._runtime_bots = {}
        self._stopped_bots = set()

    @property
    def ready(self):
        return bool(self.model_catalog().get('default_configured'))

    @property
    def model(self):
        return self.model_catalog()['default'].get('model') or ''

    @property
    def base_url(self):
        return self.model_catalog()['default'].get('base_url')

    def model_catalog(self):
        try:
            return self.catalog.get()
        except ValueError as error:
            return {'default': {'model': '', 'provider': '', 'base_url': None, 'reasoning_effort': None},
                    'models': [], 'available': False, 'default_configured': False,
                    'model_source': 'Hermes', 'default_source': 'Hermes config.yaml', 'error': str(error)}

    def refresh_model_catalog(self, after):
        """Ask for the live model lists of the providers, at most once in `after` seconds."""
        self.catalog.refresh_if_old(after)

    def validation_model_catalog(self):
        return self.catalog.get_for_validation()

    def resolve_model(self, bot, catalog=None):
        if bot.get('_model_error'):
            raise ValueError(bot['_model_error'])
        if bot.get('model_override') is not None and bot.get('_native_effective_model'):
            return dict(bot['_native_effective_model'])
        catalog = self.model_catalog() if catalog is None else catalog
        if catalog.get('error'):
            raise ValueError(catalog['error'])
        return self.catalog.resolve(bot.get('model_override'), catalog=catalog)

    def resolve_task_model(self, bot, task):
        job = task.get('_native_job') or {}
        if job.get('no_agent') is True and job.get('script'):
            return dict(model=None, provider=None)
        model = job.get('model') or job.get('model_snapshot')
        provider = job.get('provider') or job.get('provider_snapshot')
        selection = {} if model and provider else self.resolve_model(bot)
        if model or provider:
            from .model_catalog import _selection
            # The native runtime validates this route using the bot's credentials.
            # The global inventory can omit models configured only in this bot.
            selection = _selection({
                'model': model or selection['model'],
                'provider': provider or selection['provider'],
                'reasoning_effort': job.get('reasoning_effort'),
            })
        return selection

    def native_settings(self, operation='read', bots=(), **payload):
        if operation == 'routine_create':
            bot = next(bot for bot in bots if bot['id'] == payload['bot_id'])
            selection = self.resolve_model(self.observe_bot(bot))
            payload = dict(payload, changes=dict(payload['changes'],
                           model=selection['model'], provider=selection['provider'],
                           reasoning_effort=selection.get('reasoning_effort')))
        # A change needs the profile folder of each bot. A read also lists a bot whose folder is missing.
        homes = {bot['id']: str(self.catalog._profile_home(bot['id'])) for bot in bots}
        if operation not in ('read', 'routines_read', 'snapshot_read'):
            for home in homes.values():
                if not Path(home).is_dir():
                    raise RuntimeError(f'The Hermes profile folder {home} is missing. Restore it, or remove the bot from Coconut.')
        return self.catalog._call('native', native_operation=operation, homes=homes, **payload)

    def process_completions(self, bot, processes):
        groups = {}
        for item in processes:
            groups.setdefault(item.get('native_worker_id') or item.get('execution_session_id'), []).append(item)
        return [result for execution, items in groups.items()
                for result in self._process_completions(bot, items, execution)]

    def _process_completions(self, bot, processes, execution):
        import queue
        from .worker_transport import WorkerTransport
        request = dict(operation='process_completions', bot=bot, processes=processes)
        with self._lock:
            if self._closed:
                return []
            process = self._kanban_runtimes.get(execution) or self._runtimes.get(bot['id'])
            transport = self._transports.get(process)
        if process is not None and process.poll() is None and transport is not None:
            identity, channel, request = transport.open(request)
            try:
                self._send(process, request)
                frame = channel.get(timeout=10)
                if isinstance(frame, BaseException):
                    raise frame
                if frame.get('type') != 'process_completions' or frame['data'].get('error'):
                    raise RuntimeError('Hermes could not return its process status.')
                return frame['data']['results']
            except queue.Empty:
                return []
            finally:
                transport.release(identity)
        env, cwd = self.environment(bot['id'])
        result = subprocess.run([self.python, str(Path(__file__).with_name('native_processes.py'))],
                                input=json.dumps(request), text=True, capture_output=True,
                                cwd=cwd, env=env, timeout=15, check=True)
        return json.loads(result.stdout)

    def maintain_sessions(self, bot, conversations, outcomes, retention_days, *, allow_vacuum=False):
        """Record session tips, end the native sessions of idle outcomes, and prune Coconut sessions for one bot."""
        env, cwd = self.environment(bot['id'])
        from .session_maintenance import SESSION_HISTORY_DAYS
        request = dict(bot=bot, conversations=conversations, outcomes=outcomes, retention_days=retention_days,
                       history_days=SESSION_HISTORY_DAYS, allow_vacuum=allow_vacuum)
        # A first VACUUM of a large session store can take minutes. Hermes throttles later runs.
        result = subprocess.run([self.python, str(Path(__file__).with_name('native_sessions.py'))],
                                input=json.dumps(request), text=True, capture_output=True,
                                cwd=cwd, env=env, timeout=1800, check=True)
        return json.loads(result.stdout)

    def set_bot_settings(self, bot, changes):
        return self.native_settings('update_profile', [bot], bot_id=bot['id'], changes=changes)

    def observe_bot(self, bot):
        return self.observe_bots([bot])[bot['id']]

    def forget_profile_settings(self, bots):
        """Drop the saved settings of every bot that is not in this complete list, such as a deleted bot."""
        keep = {bot['id'] for bot in bots}
        with self._profile_settings_lock:
            for identity in [identity for identity in self._profile_settings if identity not in keep]:
                del self._profile_settings[identity]

    def observe_state(self, bots):
        self.forget_profile_settings(bots)
        settings = self.native_settings('snapshot_read', bots)
        return self.observe_bots(bots, settings=settings['profiles']), settings

    def observe_bots(self, bots, *, settings=None):
        native = self.catalog.read_profiles([bot['id'] for bot in bots])
        if settings is None:
            settings = self.profile_settings(bots)
        result = {}
        for bot in bots:
            observed = dict(bot)
            values = native.get(bot['id'], {})
            observed['model_override'] = values.get('model_override')
            if bot.get('model_follows_default'):
                # The profile holds a copy of the default model. The app shows the bot on the default model.
                observed['model_override'] = None
            elif values.get('effective_model'):
                observed['_native_effective_model'] = values['effective_model']
            if values.get('error'):
                observed['_model_error'] = values['error']
            observed.update(settings.get(bot['id'], {}))
            result[bot['id']] = observed
        return result

    def profile_settings(self, bots):
        """Read the native profile settings of these bots. Each turn reads them, and each read starts a Hermes process.

        A saved result stays valid while the files that profile_state reads are unchanged (see profile_signature).
        """
        signature = getattr(self.catalog, 'profile_signature', None)
        if signature is None:
            return self.native_settings(bots=bots)['profiles']
        try:
            signatures = {bot['id']: signature(bot['id']) for bot in bots}
        except (OSError, ValueError):
            return self.native_settings(bots=bots)['profiles']
        with self._profile_settings_lock:
            result = {}
            for identity, current in signatures.items():
                cached = self._profile_settings.get(identity)
                if cached and cached[0] == current:
                    result[identity] = deepcopy(cached[1])
        missing = [bot for bot in bots if bot['id'] not in result]
        if missing:
            values = self.native_settings(bots=missing)['profiles']
            with self._profile_settings_lock:
                for bot in missing:
                    if bot['id'] in values:
                        self._profile_settings[bot['id']] = (signatures[bot['id']], deepcopy(values[bot['id']]))
            result.update(values)
        return result

    def native_routines(self, bots):
        """Read the native routines and completion receipts of these bots. The routine tick reads them every 15 seconds.

        Each read starts a Hermes process. A saved result of a bot stays valid while the files that the read uses are
        unchanged (see routines_signature).
        """
        signature = getattr(self.catalog, 'routines_signature', None)
        if signature is None:
            return self.native_settings('routines_read', bots)
        try:
            # Take the signatures before the read. A file that changes during the read changes the next signature.
            signatures = {bot['id']: signature(bot['id']) for bot in bots}
        except (OSError, ValueError):
            return self.native_settings('routines_read', bots)
        with self._routines_lock:
            for identity in self._routines.keys() - signatures.keys():
                # A deleted bot is not read again.
                del self._routines[identity]
            saved = {identity: deepcopy(self._routines[identity][1]) for identity, current in signatures.items()
                     if identity in self._routines and self._routines[identity][0] == current}
        missing = [bot for bot in bots if bot['id'] not in saved]
        if missing:
            snapshot = self.native_settings('routines_read', missing)
            completions = snapshot.get('completions', {})
            with self._routines_lock:
                for bot in missing:
                    value = dict(routines=[r for r in snapshot['routines'] if r['bot_id'] == bot['id']],
                                 completions=completions.get(bot['id'], {}))
                    self._routines[bot['id']] = (signatures[bot['id']], deepcopy(value))
                    saved[bot['id']] = value
        # The read returns the routines of each bot in the order of the bots.
        return {'routines': [routine for bot in bots for routine in saved[bot['id']]['routines']],
                'completions': {bot['id']: saved[bot['id']]['completions'] for bot in bots}}

    def set_bot_model(self, bot, selection):
        return self.catalog.set_profile(bot['id'], selection)

    def follow_default_model(self, bot_ids, classify=()):
        return self.catalog.follow_default(bot_ids, classify)

    def set_model_default(self, selection):
        return self.catalog.set_default(selection)

    def subscribe_model_catalog(self, callback):
        self.catalog.on_change = callback

    def migrate_model_default(self, model, base_url):
        self.catalog.migrate_legacy_default(model, base_url)

    def configure(self, *, model, base_url=None):
        catalog = self.model_catalog()
        matches = [row for row in catalog['models'] if row['id'] == model and (base_url is None or row.get('base_url') == base_url)]
        if len(matches) != 1:
            raise ValueError('Choose one model and provider from the Hermes catalog.')
        self.set_model_default({'model': model, 'provider': matches[0]['provider'], 'reasoning_effort': None})

    def environment(self, bot_id):
        UUID(bot_id)
        root = self.data_dir / 'bots' / bot_id
        from .profiles import HermesProfiles
        # The worker runs in the bot's Hermes profile, so Coconut, `hermes -p <name>` and the gateway share its skills,
        # memories, config and cron jobs. Coconut's own files stay in <data>/bots/<uuid>.
        home = HermesProfiles(self.data_dir, getattr(self, 'profile_rows', None)).existing_home({'id': bot_id})
        for name in ('workspace', 'browser', 'tmp'):
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        # Homebrew manages one Mac installation, so its trust must have one scope.
        shared_config = self.data_dir / 'shared'
        shared_config.mkdir(exist_ok=True, mode=0o700)
        # Native tool configuration stays in the bot profile. HOME remains the OS account home.
        env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY') if k in os.environ}
        from .runtime_environment import login_folders, prepare_profile
        env.update(prepare_profile(home))
        # A bot that keeps its own logins sets their folders in its .env. The worker loads that file, and every other
        # process for this bot (work sources, process and session checks) must use the same logins.
        env.update(login_folders(home))
        env['COCONUT_GOOGLE_HOME'] = str(self.data_dir / 'connections' / 'google' / 'home')
        # Workspace recovery finds Coconut workers by COCONUT_DATA_DIR, not by HERMES_HOME, which the person's own Hermes
        # processes can share.
        env.update(COCONUT_DATA_DIR=str(self.data_dir), COCONUT_BOT_ID=bot_id, COCONUT_BOT_DIR=str(root))
        env.update(HERMES_HOME=str(home), TMPDIR=str(root / 'tmp'), PYTHONPATH=str(self.hermes_root), PYTHONUNBUFFERED='1', COCONUT_BROWSER_PROFILE=str(root / 'browser'), HERMES_DISABLE_TELEMETRY='1', HOMEBREW_XDG_CONFIG_HOME=str(shared_config))
        from .hermes_runtime import NO_LAZY_INSTALLS
        env.update(NO_LAZY_INSTALLS)
        # Hermes finds its tool store from the source folder, which the Hermes home of the official installer owns.
        # The bot's terminal `hermes` installs tools into <HERMES_HOME>/tools, so the worker reads that store too.
        env['HERMES_RUNTIME_DIR'] = str(home / 'tools')
        env.update(HERMES_KANBAN_DB=str(self.data_dir / 'kanban/kanban.db'),
                   HERMES_KANBAN_BOARD='default', HERMES_TENANT=bot_id,
                   HERMES_KANBAN_WORKSPACES_ROOT=str(self.data_dir / 'kanban/kanban/workspaces'))
        return env, root / 'workspace'

    def prepare_kanban_worker(self, identity, bot, workspace, execution_id, worker_id=None):
        """Start a dedicated process before Hermes records its claim PID."""
        cache_key = worker_id or execution_id
        env, _ = self.environment(bot['id'])
        with self._lock:
            if self._closed or bot['id'] in self._stopped_bots:
                raise RuntimeError('This bot stopped before worker preparation.')
            previous = self._kanban_runtimes.get(cache_key)
            if (previous is not None and previous.poll() is None
                    and not getattr(self._transports.get(previous), 'closed', False)
                    and self._runtime_envs.get(previous, ({},))[0].get('HERMES_HOME') == env['HERMES_HOME']):
                self._prepared[identity] = previous
                return previous.pid
        if previous is not None:
            self._terminate(previous)
        env['TERMINAL_CWD'] = workspace
        env['COCONUT_WORKSPACE_ROOT'] = str(self.data_dir / 'workspaces')
        env['COCONUT_WORKSPACE_OUTCOME'] = execution_id
        env['COCONUT_PROCESS_HOME'] = str(self.data_dir / 'runtimes' / execution_id / str(uuid4()))
        process = subprocess.Popen([self.python, str(Path(__file__).with_name('hermes_worker.py'))],
                                   cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        with self._lock:
            self._runtime_envs[process] = (env, Path(workspace))
            stopped = self._closed or bot['id'] in self._stopped_bots
            if not stopped:
                self._prepared[identity] = process
                self._kanban_runtimes[cache_key] = process
                self._runtime_bots[process] = bot['id']
        if stopped:
            self._terminate(process)
            raise RuntimeError('This bot stopped during worker preparation.')
        return process.pid

    def trim_kanban_workers(self, keep):
        with self._lock:
            busy = set(self._processes.values()) | set(self._prepared.values())
            idle = [(key, process) for key, process in self._kanban_runtimes.items()
                    if key not in keep and process not in busy]
        removed = [(key, process) for key, process in idle[:-1] if not self._has_live_resources(process)]
        with self._lock:
            busy = set(self._processes.values()) | set(self._prepared.values())
            removed = [(key, process) for key, process in removed
                       if self._kanban_runtimes.get(key) is process and process not in busy]
            for key, process in removed:
                self._kanban_runtimes.pop(key, None)
                self._paused_processes = {k: p for k, p in self._paused_processes.items() if p is not process}
        for _, process in removed:
            self._terminate(process)

    def _has_live_resources(self, process):
        if process.poll() is not None:
            return False
        from .worker_transport import WorkerTransport
        with self._lock:
            transport = self._transports.get(process)
            if transport is None:
                transport = self._transports[process] = WorkerTransport(process, MAX_EVENT_CHARACTERS)
        identity = None
        try:
            identity, channel, request = transport.open(dict(operation='runtime_state'))
            self._send(process, request)
            frame = channel.get(timeout=1)
            return not (isinstance(frame, dict) and frame.get('type') == 'runtime_state'
                        and frame.get('data', {}).get('live_processes') is False
                        and frame.get('data', {}).get('live_browsers') is False)
        except Exception:
            return True
        finally:
            if identity is not None:
                transport.release(identity)

    def release_kanban_preparation(self, identity):
        with self._lock:
            return self._prepared.pop(identity, None)

    def workspace_recovery_idle_pids(self):
        """Prove cached runtimes idle without cancelling any resource."""
        with self._lock:
            if self._processes or self._prepared:
                raise ValueError('A native worker is still executing or prepared.')
            processes = set(self._runtimes.values()) | set(self._kanban_runtimes.values())
        for process in processes:
            if self._has_live_resources(process):
                raise ValueError('A native runtime has active or ambiguous background resources.')
        return {p.pid for p in processes if p.poll() is None}

    def start_task(self, task, bot, history, emit):
        self._run('start', task, bot, history, emit)

    def resume_task(self, task, bot, history, decision, emit):
        self._run('resume', task, bot, history, emit, decision=decision)

    def answer_context(self, task, bot, history, question, emit):
        self._run('context', task, bot, history, emit, question=question)

    def _run(self, operation, task, bot, history, emit, **extra):
        if task.get('_work_status_only'):
            return self._run_turn(operation, task, bot, history, emit, isolated=True, **extra)
        with self._lock:
            from .work_capacity import execution_key
            lock = self._bot_locks.setdefault(execution_key(task), threading.Lock())
        with lock:
            self._run_turn(operation, task, bot, history, emit, **extra)

    def _run_turn(self, operation, task, bot, history, emit, *, isolated=False, **extra):
        from .turn_timings import TurnTimings
        timing = TurnTimings(task['id'], 'provider')
        if (self.hermes_root / '.env').exists() or (self.hermes_root / '.env').is_symlink():
            raise RuntimeError('The Hermes checkout contains a shared .env file. Use a clean checkout for Coconut.')
        if not Path(self.python).is_file() or not (self.hermes_root / 'run_agent.py').is_file():
            raise RuntimeError('Install Hermes on this Mac before starting this bot.')
        bot = self.observe_bot(bot)
        timing.mark('profile_read')
        selection = self.resolve_task_model(bot, task)
        script_only = (task.get('_native_job') or {}).get('no_agent') is True
        runtime_config = None if script_only else self.catalog.resolve_runtime(selection, bot_id=bot['id'])
        timing.mark('model_resolution')
        task_id = task['id']
        env, cwd = self.environment(bot['id'])
        timing.mark('profile_sync')
        request = dict(operation=operation, task=task, bot=bot, history=history,
                       model=selection['model'], provider=selection['provider'], base_url=selection.get('base_url'),
                       reasoning_effort=selection.get('reasoning_effort'), runtime_config=runtime_config, **extra)
        from .model_catalog import ModelCatalogError
        try:
            request['worker_limit'] = self.catalog.worker_limit()
        except ModelCatalogError:
            # The worker keeps the limit of its last turn, or Hermes's own default.
            pass
        if task.get('_kanban_external_id'):
            from .work_workspace import preferred
            from .work_capacity import execution_key
            from .work_workspace import retired_path
            root = self.data_dir / 'workspaces'
            workspace = preferred(root, execution_key(task))
            explicit = workspace != root / execution_key(task)
            hint = task.get('_kanban_workspace') or task.get('_native_outcome', {}).get('workspace')
            # A claim can carry the path of a worktree whose restore failed. Do not start work there.
            if hint and retired_path(root, execution_key(task), hint):
                hint = None
            request['workspace'] = str(workspace if explicit else hint or workspace)
        with self._lock:
            prepared = self._prepared.pop(task.get('_kanban_external_id'), None)
            if self._closed or task_id in self._cancelled or task.get('_context_id') in self._cancelled_contexts:
                return
            if task.get('_kanban_external_id') and prepared is None:
                raise RuntimeError('The native claim has no prepared worker.')
            process = prepared or (None if isolated else self._runtimes.get(bot['id']))
            if prepared:
                isolated = False
            # Work sessions apply their own conversation epoch without stopping peer turns.
            # A worker belongs to one HERMES_HOME. When the profile folder of the bot changes, a new worker starts.
            epoch = (bot['session_id'], None if task.get('_execution_session_id') else task.get('_conversation_epoch'),
                     env['HERMES_HOME'])
            if not prepared and (process is None or process.poll() is not None or self._runtime_epochs.get(bot['id']) != epoch
                    or (self._transports.get(process) is not None and self._transports[process].closed)):
                if process is not None:
                    self._terminate(process)
                process = subprocess.Popen([self.python, str(Path(__file__).with_name('hermes_worker.py'))], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
                if not isolated:
                    self._runtime_envs[process] = (env, cwd)
                    self._runtimes[bot['id']] = process
                    self._runtime_epochs[bot['id']] = epoch
            if operation != 'context':
                self._paused_processes.pop(task_id, None)
            from .worker_transport import WorkerTransport, events
            transport = self._transports.get(process)
            if transport is None:
                transport = self._transports[process] = WorkerTransport(process, MAX_EVENT_CHARACTERS)
            channel_id, channel, request = transport.open(request)
            try:
                self._send(process, request)
            except Exception:
                transport.release(channel_id)
                self._terminate(process)
                raise
            self._processes[task_id] = process
        reusable = False
        deferred = None
        paused = False
        try:
            timing.mark('worker_request')
            timing.report()
            # Keep stdin open. EOF lets the worker stop if the service disappears.
            terminal_received = False
            for event in events(channel):
                if event.get('type') in ('turn_timing', 'turn_trace'):
                    logging.getLogger(__name__).warning('coconut.%s %s', event['type'], json.dumps(event.get('data')))
                    continue
                if event.get('type') == 'process_completions':
                    continue  # A timed-out status request can finish before this turn.
                if event.get('type') == 'turn_end':
                    reusable = event.get('data', {}).get('reusable') is True
                    if deferred is not None:
                        raise deferred
                    return
                if event.get('type') == 'phase':
                    # A phase only labels progress. A rejected label never stops the turn.
                    try:
                        emit('phase', event.get('data') if isinstance(event.get('data'), dict) else {})
                    except Exception:
                        logging.getLogger(__name__).debug('Ignored a phase update for task %s', task_id, exc_info=True)
                    continue
                if event.get('type') not in ('steering_ready', 'steering_consumed', 'text_delta', 'interim_message', 'activity', 'decision', 'artifact', 'completed', 'failed', 'bot_message', 'native_bot_message', 'bot_create', 'bot_delete', 'service_restart', 'withdrawal_correction', 'work_wait', 'work_continue', 'dependency_unlink', 'block_pause', 'native_call', 'native_result', 'native_approval', 'native_question', 'native_proposal', 'decision_response') or not isinstance(event.get('data'), dict):
                    raise RuntimeError('Hermes returned an invalid structured event.')
                if event['type'] in ('native_call', 'native_question', 'native_proposal', 'decision_response'):
                    revision = event['data'].get('_worker_profile_revision')
                    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision):
                        raise RuntimeError('Hermes returned an event without its captured profile revision.')
                if (event['type'] in ('artifact', 'native_question') and event.get('request_id')) or event['type'] in ('bot_message', 'native_bot_message', 'bot_create', 'bot_delete', 'service_restart', 'withdrawal_correction', 'work_wait', 'work_continue', 'dependency_unlink', 'block_pause', 'native_call', 'native_result', 'native_proposal', 'decision_response'):
                    request_id = event.get('request_id')
                    if type(request_id) is not int or request_id < 1:
                        raise RuntimeError('Hermes returned an operation without a receipt request.')
                    receipt_started = time.monotonic()
                    try:
                        result = emit(event['type'], event['data'])
                        if event['type'] == 'native_question':
                            paused = True
                            receipt = {'status': 'recorded'}
                        elif event['type'] in ('artifact', 'native_call', 'native_result', 'service_restart', 'withdrawal_correction', 'work_wait', 'work_continue', 'dependency_unlink', 'block_pause', 'native_proposal', 'decision_response'):
                            receipt = result
                        elif event['type'] == 'bot_create':
                            from .hermes_worker import bot_creation_receipt
                            receipt = bot_creation_receipt(result, event['data'], bot)
                        elif event['type'] == 'bot_delete':
                            from .hermes_worker import bot_deletion_receipt
                            receipt = bot_deletion_receipt(result, event['data'], bot)
                        else:
                            statuses = ('queued', 'paused', 'duplicate', 'rejected') if event['type'] == 'native_bot_message' else ('queued', 'paused', 'duplicate')
                            if not isinstance(result, dict) or result.get('status') not in statuses:
                                raise ValueError('The service did not return a delivery receipt.')
                            receipt = {key: result[key] for key in ('task_id', 'target_bot_id', 'thread_id', 'status', 'delivery_id') if isinstance(result.get(key), str)}
                            if result['status'] == 'rejected' and isinstance(result.get('error'), str):
                                receipt['error'] = result['error']
                    except Exception as error:
                        from .service import ServiceError, YieldForDecision
                        if event['type'] == 'native_question':
                            if isinstance(error, ServiceError) and error.status == 400:
                                receipt = {'status': 'validation_failed', 'error': str(error)}
                            else:
                                paused = True
                                deferred = error
                                receipt = ({'status': 'recorded', 'decision_id': error.decision_id}
                                           if isinstance(error, YieldForDecision) else
                                           {'status': 'unconfirmed', 'error': 'The service did not confirm this decision.'})
                        elif event['type'] in ('artifact', 'withdrawal_correction', 'work_wait', 'work_continue', 'dependency_unlink', 'block_pause', 'decision_response', 'native_proposal', 'native_call') and isinstance(error, ServiceError) and 400 <= error.status < 500:
                            receipt = {'status': 'rejected', 'error': str(error)}
                        elif event['type'] in ('bot_create', 'bot_delete') and isinstance(error, ServiceError) and 400 <= error.status < 500:
                            message = {
                                'bot_create': 'The service rejected this bot creation. Use the same human source and exact requested bot details.',
                                'bot_delete': 'The service rejected this deletion. Check the current bot name, ID, task state, and human source.',
                            }[event['type']]
                            receipt = {'status': 'validation_failed', 'error': message}
                        else:
                            receipt = {'status': 'unconfirmed', 'error': 'The service did not confirm this operation. Retry only the exact same request.'}
                    self._send(process, {'request_id': request_id, 'receipt': receipt})
                    elapsed = time.monotonic() - receipt_started
                    if event['type'] in ('native_call', 'native_result') and (elapsed >= 5 or receipt.get('status') != 'recorded'):
                        logging.getLogger(__name__).warning(
                            'Tool confirmation task=%s kind=%s duration_ms=%d status=%s',
                            task_id, event['type'], round(elapsed * 1000), receipt.get('status'))
                    continue
                paused = paused or event['type'] in ('decision', 'native_approval', 'native_question')
                terminal_received = terminal_received or event['type'] in ('completed', 'failed')
                try:
                    emit(event['type'], event['data'])
                except Exception as error:
                    if event['type'] not in ('decision', 'native_approval', 'native_question'):
                        raise
                    # Drain the paused turn before another message can use its runtime.
                    deferred = error
            code = process.wait(timeout=5)
            if code and not terminal_received and task_id not in self._cancelled:
                raise RuntimeError(f'Hermes worker stopped with exit code {code}.')
        except RuntimeError:
            if task_id not in self._cancelled:
                raise
        finally:
            transport.release(channel_id)
            if isolated:
                reusable = False
            with self._lock:
                if reusable and paused and task_id not in self._cancelled and process.poll() is None:
                    self._paused_processes[task_id] = process
                if self._processes.get(task_id) is process:
                    del self._processes[task_id]
                self._paused_processes = {key: value for key, value in self._paused_processes.items() if value.poll() is None}
                # A failed transport affects every turn; a model failure ends only its own turn.
                if not reusable and self._runtimes.get(bot['id']) is process:
                    del self._runtimes[bot['id']]
            if not reusable:
                self._terminate(process)


    def _send(self, process, frame):
        with self._input_lock:
            process.stdin.write(json.dumps(frame) + '\n')
            process.stdin.flush()

    def _terminate(self, process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        self._transports.pop(process, None)
        self._runtime_bots.pop(process, None)
        environment = self._runtime_envs.pop(process, None)
        if environment is not None and process.returncode != 0:
            # Hermes checks PID identity before reaping children after a forced exit.
            env, cwd = environment
            subprocess.run([self.python, str(Path(__file__).with_name('hermes_worker.py')), '--cleanup'],
                           cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, check=True)
        for pipe in (process.stdin, process.stdout):
            if pipe:
                pipe.close()

    def cancel_context(self, task_id, context_id):
        with self._lock:
            self._cancelled_contexts.add(context_id)
            process = self._processes.get(task_id)
        if process is not None and process.poll() is None:
            self._send(process, {'cancel_context': context_id})

    def steer_task(self, task_id, turn_id, messages):
        with self._lock:
            process = self._processes.get(task_id)
        if process is not None and process.poll() is None:
            self._send(process, {'steer_task': task_id, 'turn_id': turn_id, 'messages': messages})

    def cancel_task(self, task_id):
        with self._lock:
            self._cancelled.add(task_id)
            process = self._processes.get(task_id)
            paused = self._paused_processes.pop(task_id, None)
            prepared = self._prepared.pop(task_id, None)
            process = process or prepared
        if process is not None and process.poll() is None:
            self._send(process, {'cancel_task': task_id})
            def reap_cancelled():
                # Escalate only after every active turn in this runtime is cancelled.
                while process.poll() is None:
                    threading.Event().wait(1)
                    with self._lock:
                        owners = {key for key, value in self._processes.items() if value is process}
                        owners.update(key for key, value in self._paused_processes.items() if value is process)
                        if task_id not in owners:
                            return
                        if owners <= self._cancelled:
                            self._terminate(process)
                            return
            threading.Thread(target=reap_cancelled, daemon=True, name='cancelled-runtime').start()
        elif paused is not None and paused.poll() is None:
            # Another turn can now own this runtime. Stop only the paused task's children.
            self._send(paused, {'cancel_task': task_id})

    def stop_bot(self, bot_id):
        with self._lock:
            self._stopped_bots.add(bot_id)
            process = self._runtimes.pop(bot_id, None)
            processes = {p for p, owner in self._runtime_bots.items() if owner == bot_id}
            if process is not None:
                processes.add(process)
            self._runtime_epochs.pop(bot_id, None)
            self._kanban_runtimes = {k: p for k, p in self._kanban_runtimes.items() if p not in processes}
            self._prepared = {k: p for k, p in self._prepared.items() if p not in processes}
            self._paused_processes = {k: p for k, p in self._paused_processes.items() if p not in processes}
        for runtime in processes:
            self._terminate(runtime)

    def close(self):
        with self._lock:
            self._closed = True
            processes = list(set(self._runtimes.values()) | set(self._prepared.values()) | set(self._kanban_runtimes.values()))
            self._kanban_runtimes.clear()
            self._prepared.clear()
            self._runtimes.clear()
            self._paused_processes.clear()
            self._runtime_epochs.clear()
        for process in processes:
            self._terminate(process)
