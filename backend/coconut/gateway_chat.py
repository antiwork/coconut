"""The chat runtime setting, and the Hermes gateway that runs chat turns for it.

`chat_runtime` is `worker` (the default) or `gateway`. `PUT /v1/settings/chat-runtime {"chat_runtime": ...}` sets it.
With `gateway`, a chat turn (gateway_bridge.plain_chat_turn) goes to the `coconut` platform of the Hermes gateway when
the adapter of the bot's profile is connected. Each other turn, and each turn that no adapter takes, runs in the
Coconut worker as before. Both runtimes continue the same native conversation of the bot (hermes_plugin/coconut/hooks.py).

A gateway turn reaches Coconut in two more ways. A question of Hermes (clarify, command approval, coconut_ask) becomes a
decision card, and its answer returns to the waiting turn (record_gateway_question, answer_gateway_question). A tool of
the `coconut` toolset acts for the task of the turn (gateway_tool).

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
        from .native_conversation import message_content, session_root
        turn = set(task.get('_turn_message_ids', []))
        messages = [message for message in history if message.get('id') in turn]
        if any(message.get(field) for message in messages for field in WORKER_MESSAGE_FIELDS):
            return False
        # A quote and a bot mention go into the text, as the worker writes them.
        text = '\n\n'.join(message_content(message) for message in messages)
        if not text.strip():
            return False
        try:
            # The worker resumes the chat of a bot from this root (native_conversation.NativeConversation).
            self.gateway_bridge.run_turn(task['id'], home, bot['id'], text, emit, chat_name=bot.get('name') or 'Coconut',
                                         session_root=session_root(bot, epoch=task.get('_conversation_epoch')))
        except GatewayUnavailable:
            LOG.warning('No Hermes gateway took chat turn %s. The worker runs it.', task['id'])
            return False
        finally:
            self._close_gateway_questions(task['id'])
        return True

    def gateway_tool(self, data: dict) -> dict:
        """POST /v1/gateway/tools: a tool of the `coconut` toolset, for the task of a running gateway turn."""
        from .service import ServiceError, YieldForDecision
        try:
            return self._gateway_tool(data)
        except YieldForDecision:
            raise ServiceError(409, 'This Coconut turn no longer takes tool calls.') from None

    def _gateway_tool(self, data: dict) -> dict:
        from .service import ServiceError, require_text
        task_id = self.gateway_bridge.task_of(data.get('turn_id'))
        if task_id is None:
            raise ServiceError(409, 'This Coconut turn no longer runs.')
        arguments = data.get('arguments') if isinstance(data.get('arguments'), dict) else {}
        if data.get('tool') == 'track_pr':
            return self.emit(task_id, 'artifact', {'kind': 'pr', 'url': require_text(arguments, 'url', 500)})
        if data.get('tool') == 'message_bot':
            target = require_text(arguments, 'bot', 200).strip()
            with self.lock:
                source = self._get('tasks', task_id)
                bots = [bot for bot in self._all('bots') if not bot['archived'] and not bot.get('_deleted')
                        and bot['id'] != source['bot_id']]
            match = [bot for bot in bots if bot['id'] == target] or [
                bot for bot in bots if bot['name'].casefold() == target.casefold()]
            if len(match) != 1:
                names = ', '.join(sorted(bot['name'] for bot in bots)) or 'none'
                raise ServiceError(400, f'Name one other bot exactly. The other bots: {names}.')
            receipt = self.emit(task_id, 'bot_message', {'target_bot_id': match[0]['id'],
                                                         'text': require_text(arguments, 'text', 20000)})
            return {key: receipt[key] for key in ('status', 'target_bot_id', 'task_id') if key in receipt}
        raise ServiceError(404, 'This Coconut tool does not exist.')

    def record_gateway_question(self, task: dict, payload: dict) -> None:
        """A question of a gateway turn becomes a decision card. The caller holds the transaction.

        The task stays working: the gateway turn waits for the answer and goes on after it.
        """
        from uuid import uuid4
        from .decision_inbox import decision_options
        from .service import now, digest, encoded
        approval = payload['question_kind'] == 'approval'
        choices = payload.get('choices') or []
        if choices:
            options = decision_options([dict(id=choice['id'], label=choice['label']) for choice in choices])
            for option, choice in zip(options, choices):
                if choice.get('deny'):
                    option['answer'] = 'reject'
        else:
            options = [dict(id='reply', label='Reply', answer='approve', explanation='Type the answer.',
                            risk_level=None, risk='')]
        action = {'type': 'gateway_' + payload['question_kind'], 'question': payload['text']}
        decision = dict(
            id=str(uuid4()), bot_id=task['bot_id'], task_id=task['id'], title=payload['text'][:160],
            explanation=(payload.get('detail') or '')[:2000],
            action_type='native_approval' if approval else 'question', resource='', consequences='',
            recommended='approve', options=options, recommended_option_id=None, status='pending', urgency='normal',
            needs_attention_now=False, risk_level=None, risk='', created_at=now(), decided_at=None, decided_by=None,
            decided_by_name=None, correction=None, outcome=None, revision=1, inbox_kind='decision',
            _action=action, _action_hash=digest(encoded(action)), _answers=[],
            _gateway_question=dict(turn_id=payload['turn_id'], question_id=payload['question_id'],
                                   kind=payload['question_kind']))
        decision.update(self.decision_origin(task))
        self._put('decisions', decision)
        self._message(task['bot_id'], decision['title'], task_id=task['id'], kind='decision', decision_id=decision['id'])
        self._event('decision.created', self._public(decision), task['bot_id'], task['id'])

    def answer_gateway_question(self, decision: dict, task: dict, data: dict, device: str) -> dict:
        """POST /v1/decisions/<id>/answer for a question of a gateway turn. The caller holds the transaction."""
        from .service import ServiceError, decision_correction, now
        if data.get('revision') != decision['revision']:
            raise ServiceError(409, 'This decision changed. Review it before answering.')
        option = next((item for item in decision['options'] if item['id'] == data.get('option_id')), None)
        correction = decision_correction(data).strip()
        if data.get('option_id') is not None and option is None:
            raise ServiceError(400, 'Choose a current option.')
        if option is None and not correction and data.get('choice') != 'reject':
            raise ServiceError(400, 'Enter a reply or choose an option.')
        question = decision['_gateway_question']
        if option is not None and option['id'] != 'reply':
            # A command approval takes the choice alone. A text there would be the reason of a denial.
            text, choice = ('' if question['kind'] == 'approval' else option['label']), option['id']
        else:
            text, choice = correction, ('deny' if data.get('choice') == 'reject' else '')
        if question['kind'] == 'approval' and not choice:
            # A typed reply to a command approval declines the command and tells Hermes why.
            choice = 'deny'
        rejected = choice == 'deny' or (option is not None and option['answer'] == 'reject') or data.get('choice') == 'reject'
        delivered = self.gateway_bridge.answer(question['turn_id'], dict(
            question_id=question['question_id'], text=text, choice=choice))
        decision.update(status='rejected' if rejected else 'approved' if option else 'replied',
                        selected_option_id=option['id'] if option else None,
                        selected_option_label=option['label'] if option else None, correction=correction or None,
                        decided_at=now(), decided_by=device, decided_by_name=self._actor_name(device),
                        outcome='Answer sent to Hermes.' if delivered else 'Hermes stopped waiting for this answer.')
        decision['_answers'].append(dict(choice=choice or 'reply', option_id=option['id'] if option else None,
                                          correction=correction or None, decided_by=device))
        self._put('decisions', decision)
        if correction:
            self._record_decision_correction(task, decision, correction)
            self._put('tasks', task)
        self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
        self._refresh_bot(task['bot_id'])
        return self._public(decision)

    def _close_gateway_questions(self, task_id: str) -> None:
        """Close the open questions of a gateway turn that ended. Hermes no longer waits for their answers."""
        with self.transaction():
            for decision in self._all('decisions'):
                if (decision['task_id'] == task_id and decision.get('_gateway_question')
                        and decision['status'] == 'pending'):
                    decision.update(status='failed', outcome='Hermes stopped waiting for this answer.')
                    self._put('decisions', decision)
                    self._event('decision.updated', self._public(decision), decision['bot_id'], task_id)

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
