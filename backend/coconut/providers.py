"""Execution adapters. The service owns decisions, permissions, and file writes."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional, Protocol
from uuid import UUID
from .worker_protocol import MAX_EVENT_CHARACTERS

Emit = Callable[[str, dict], Optional[dict]]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
        if task.get('_handoff_budget_remaining', 0) <= 0:
            emit('completed', {'summary': 'Fixture: the exchange delivery limit is reached.'})
            return True
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
    """A separate Hermes Python process per task, using structured JSONL only."""

    name = 'hermes'

    def __init__(self, data_dir, *, python=None, hermes_root=None, model=None, base_url=None, catalog=None):
        super().__init__(data_dir)
        runtime = PROJECT_ROOT / '.runtime' / 'hermes'
        self.python = str(python or os.environ.get('COCONUT_HERMES_PYTHON') or runtime / 'venv' / 'bin' / 'python')
        self.hermes_root = Path(hermes_root or os.environ.get('COCONUT_HERMES_ROOT') or runtime / 'source').resolve()
        from .model_catalog import HermesModelCatalog
        self.catalog = catalog or HermesModelCatalog(self.data_dir, python=self.python, source=self.hermes_root)
        if model:
            self.catalog.migrate_legacy_default(model, base_url)
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()

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

    def native_settings(self, operation='read', bots=(), **payload):
        return self.catalog._call('native', native_operation=operation,
                                  homes={bot['id']: str(self.catalog._profile_home(bot['id'])) for bot in bots}, **payload)

    def set_bot_settings(self, bot, changes):
        return self.native_settings('update_profile', [bot], bot_id=bot['id'], changes=changes)

    def observe_bot(self, bot):
        return self.observe_bots([bot])[bot['id']]

    def observe_bots(self, bots):
        from .profiles import HermesProfiles
        native = self.catalog.read_profiles([bot['id'] for bot in bots])
        settings = self.native_settings(bots=bots)['profiles']
        result = {}
        for bot in bots:
            observed = dict(bot)
            values = native.get(bot['id'], {})
            observed['model_override'] = values.get('model_override')
            if values.get('effective_model'):
                observed['_native_effective_model'] = values['effective_model']
            if values.get('error'):
                observed['_model_error'] = values['error']
            observed.update(settings.get(bot['id'], {}))
            result[bot['id']] = observed
        return result

    def set_bot_model(self, bot, selection):
        return self.catalog.set_profile(bot['id'], selection)

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
        for name in ('home', 'workspace', 'browser', 'tmp'):
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        # Homebrew manages one Mac installation, so its trust must have one scope.
        shared_config = self.data_dir / 'shared'
        shared_config.mkdir(exist_ok=True, mode=0o700)
        # No login, GitHub, cloud, shell, or desktop credentials enter the worker.
        env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY') if k in os.environ}
        env.update(HOME=str(root / 'home'), HERMES_HOME=str(root / 'home'), TMPDIR=str(root / 'tmp'), PYTHONPATH=str(self.hermes_root), PYTHONUNBUFFERED='1', COCONUT_BROWSER_PROFILE=str(root / 'browser'), HERMES_DISABLE_TELEMETRY='1', HOMEBREW_XDG_CONFIG_HOME=str(shared_config))
        return env, root / 'workspace'

    def start_task(self, task, bot, history, emit):
        self._run('start', task, bot, history, emit)

    def resume_task(self, task, bot, history, decision, emit):
        self._run('resume', task, bot, history, emit, decision=decision)

    def answer_context(self, task, bot, history, question, emit):
        self._run('context', task, bot, history, emit, question=question)

    def _run(self, operation, task, bot, history, emit, **extra):
        if (self.hermes_root / '.env').exists() or (self.hermes_root / '.env').is_symlink():
            raise RuntimeError('The Hermes checkout contains a shared .env file. Use a clean checkout for Coconut.')
        if not Path(self.python).is_file() or not (self.hermes_root / 'run_agent.py').is_file():
            raise RuntimeError('Install the Coconut Hermes runtime before starting this bot.')
        bot = self.observe_bot(bot)
        selection = self.resolve_model(bot)
        runtime_config = self.catalog.resolve_runtime(selection, bot_id=bot['id'])
        from .profiles import HermesProfiles
        HermesProfiles(self.data_dir).sync(bot)
        task_id = task['id']
        env, cwd = self.environment(bot['id'])
        env['COCONUT_DEFAULT_HOME'] = str(self.catalog.root)
        request = dict(operation=operation, task=task, bot=bot, history=history,
                       model=selection['model'], provider=selection['provider'], base_url=selection.get('base_url'),
                       reasoning_effort=selection.get('reasoning_effort'), runtime_config=runtime_config, **extra)
        process = subprocess.Popen([self.python, str(Path(__file__).with_name('hermes_worker.py'))], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        with self._lock:
            if task_id in self._cancelled:
                self._terminate(process)
                return
            self._processes[task_id] = process
        try:
            process.stdin.write(json.dumps(request) + '\n')
            process.stdin.flush()
            # Keep stdin open. EOF lets the worker stop if the service disappears.
            terminal_received = False
            for line in iter(lambda: process.stdout.readline(MAX_EVENT_CHARACTERS + 1), ''):
                if len(line) > MAX_EVENT_CHARACTERS:
                    raise RuntimeError('Hermes returned an oversized event.')
                event = json.loads(line)
                if event.get('type') not in ('text_delta', 'activity', 'decision', 'artifact', 'completed', 'failed', 'bot_message', 'native_bot_message', 'bot_create', 'bot_delete', 'native_call', 'native_result', 'native_approval', 'native_question') or not isinstance(event.get('data'), dict):
                    raise RuntimeError('Hermes returned an invalid structured event.')
                if event['type'] in ('bot_message', 'native_bot_message', 'bot_create', 'bot_delete', 'native_call', 'native_result'):
                    request_id = event.get('request_id')
                    if type(request_id) is not int or request_id < 1:
                        raise RuntimeError('Hermes returned an operation without a receipt request.')
                    try:
                        result = emit(event['type'], event['data'])
                        if event['type'] in ('native_call', 'native_result'):
                            receipt = result
                        elif event['type'] == 'bot_create':
                            from .hermes_worker import bot_creation_receipt
                            receipt = bot_creation_receipt(result, event['data'], bot)
                        elif event['type'] == 'bot_delete':
                            from .hermes_worker import bot_deletion_receipt
                            receipt = bot_deletion_receipt(result, event['data'], bot)
                        else:
                            if not isinstance(result, dict) or result.get('status') not in ('queued', 'paused', 'duplicate', 'limit_reached'):
                                raise ValueError('The service did not return a delivery receipt.')
                            receipt = {key: result[key] for key in ('task_id', 'target_bot_id', 'thread_id', 'status', 'delivery_id') if isinstance(result.get(key), str)}
                    except Exception as error:
                        from .service import ServiceError
                        if event['type'] in ('bot_create', 'bot_delete') and isinstance(error, ServiceError) and 400 <= error.status < 500:
                            message = {
                                'bot_create': 'The service rejected this bot creation. Use the same human source and exact requested bot details.',
                                'bot_delete': 'The service rejected this deletion. Check the current bot name, ID, task state, and human source.',
                            }[event['type']]
                            receipt = {'status': 'validation_failed', 'error': message}
                        else:
                            receipt = {'status': 'unconfirmed', 'error': 'The service did not confirm this operation. Retry only the exact same request.'}
                    process.stdin.write(json.dumps({'request_id': request_id, 'receipt': receipt}) + '\n')
                    process.stdin.flush()
                    continue
                terminal_received = terminal_received or event['type'] in ('completed', 'failed')
                emit(event['type'], event['data'])
            code = process.wait(timeout=5)
            if code and not terminal_received and task_id not in self._cancelled:
                raise RuntimeError(f'Hermes worker stopped with exit code {code}.')
        finally:
            self._terminate(process)
            with self._lock:
                if self._processes.get(task_id) is process:
                    del self._processes[task_id]


    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        for pipe in (process.stdin, process.stdout):
            if pipe:
                pipe.close()

    def cancel_task(self, task_id):
        with self._lock:
            self._cancelled.add(task_id)
            process = self._processes.get(task_id)
        if process:
            self._terminate(process)
