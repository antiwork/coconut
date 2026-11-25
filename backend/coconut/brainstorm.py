"""Brainstorm by voice: the person talks an idea through with the model of a project, which then writes the next steps.

The person asked for it on Product, in the place of New to-do: "have brainstorming special button which enables live
chat voice mode with agent and then it turns it into next steps (chat box prefill) and then send to build". The iPhone
listens and speaks on the phone (ios/Coconut/Brainstorm.swift). Each thing that the person says comes here with the
talk so far, and the model of the project answers in a sentence or two. A Hermes turn takes minutes with its tools and
its long prompt, so the answer is one model call with what the profile knows instead (SOUL.md and the memory that
Hermes puts into each turn), and it comes in about two seconds. With `finish` the same model writes the message that
builds what the two worked out, and the app puts it into the message box of Chats, for the person to read and send.

The call runs in the Hermes Python of the project's profile (brainstorm_worker.py), so it uses the profile's model and
logins, and Coconut never reads a key. One worker stays up for each project between the turns of a talk, because the
first call of a process takes a second or two longer. It ends by itself after a quarter of an hour without a call.

`POST /v1/brainstorm` keeps no saved response: each call is a new answer (Service.mutate).
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from uuid import UUID

from .service_base import ServiceError

WORKER = Path(__file__).with_name('brainstorm_worker.py')
# The files that Hermes puts into every turn of a profile: what the agent of the project knows of it and of the person.
CONTEXT = ('SOUL.md', 'memories/USER.md', 'memories/MEMORY.md')
CONTEXT_CHARACTERS = 8000
LINES = 400
LINE_CHARACTERS = 4000
# A request carries the whole talk, up to about an hour of it. An answer reads only its newest part, so a long talk
# answers as fast as a short one, while the next steps read all of it.
TALK_CHARACTERS = 200000
ANSWER_CHARACTERS = 60000
REPLY_TOKENS = 400
STEPS_TOKENS = 1500
# The phone waits 45 seconds for an answer (TunnelConnection.longTimeout).
TIMEOUT = 40

SPOKEN = (
    'You are the agent of the {project} project in Coconut. The person is brainstorming out loud with you: what they '
    'say reaches you as a transcript, and your answer is read aloud on their phone. Answer in one to three short spoken '
    'sentences, without lists, Markdown, emoji or links. Move the idea forward: ask one question at a time, offer a '
    'concrete option or a tradeoff when it helps, and say so plainly when the idea conflicts with what you know of the '
    'project. The transcript can mishear words, so go by what the person most likely meant. You cannot run tools or '
    'look anything up during the talk; when something needs checking, say that the agent who builds it will check it, '
    'and keep going. When the person is done, they tap Next steps, and you write the message that builds what you '
    'worked out.')
WRITTEN = (
    'You are the agent of the {project} project in Coconut. You brainstormed out loud with the person. The talk '
    'follows as a transcript, which can mishear words.')
STEPS = (
    'The brainstorm is over. Write the message that I will send to an agent of {project} to build what we worked out. '
    'Start with one sentence that says what to build. Then list the next steps in order, numbered, each one concrete '
    'action. Then, only if there are any, add in a few short lines the decisions, limits and open questions from our '
    'talk that the agent needs. Write it as me, in plain sentences, with no title, preamble, headings or bold. Leave '
    'out what we dropped.')

_workers: dict[str, _Worker] = {}
_workers_lock = threading.Lock()


def brainstorm(service, data) -> dict:
    """The answer of the model of a project to the talk so far, or with `finish` the message that builds what it
    worked out. A talk without lines only gets the worker of the project ready, so its first answer comes sooner."""
    project_id, lines, finish = _request(data)
    bot = service._get('bots', project_id)
    if bot.get('chat_of'):
        # A chat of a project brainstorms with its project: they share one profile.
        bot = service._get('bots', bot['chat_of'])
    if bot.get('_deleted') or bot.get('archived'):
        raise ServiceError(409, 'Restore this project before you brainstorm with it.')
    provider = service.provider
    if not hasattr(provider, 'environment') or not getattr(provider, 'python', ''):
        raise ServiceError(503, 'Brainstorm needs Hermes on your Mac.')
    try:
        env, cwd = provider.environment(bot['id'])
    except Exception as error:  # The profile folder of the project is missing (profiles.ProfileError).
        raise ServiceError(409, str(error))
    if not lines:
        _worker(bot['id'], provider, env, cwd)
        return {'text': ''}
    messages = _messages(bot.get('name') or 'this', Path(env['HERMES_HOME']), lines, finish)
    if len(messages) == 1:
        return {'text': ''}
    for attempt in (1, 2):
        worker = _worker(bot['id'], provider, env, cwd)
        try:
            return {'text': worker.ask(messages, STEPS_TOKENS if finish else REPLY_TOKENS)}
        except _Gone:
            # The worker ended between two calls, or failed to start: one new worker tries again.
            with _workers_lock:
                if _workers.get(bot['id']) is worker:
                    del _workers[bot['id']]
    raise ServiceError(502, 'The brainstorm could not reach the model. Try again.')


def _request(data) -> tuple[str, list[tuple[str, str]], bool]:
    if not isinstance(data, dict) or set(data) - {'bot_id', 'messages', 'finish'}:
        raise ServiceError(400, 'Send the project, the talk and whether it is over.')
    try:
        project_id = str(UUID(str(data.get('bot_id'))))
    except ValueError:
        raise ServiceError(400, 'Name the project of the brainstorm.')
    messages, finish = data.get('messages', []), data.get('finish', False)
    if not isinstance(messages, list) or len(messages) > LINES or not isinstance(finish, bool):
        raise ServiceError(400, f'Send up to {LINES} lines of the talk.')
    lines = []
    for line in messages:
        if (not isinstance(line, dict) or line.get('role') not in ('user', 'assistant')
                or not isinstance(line.get('text'), str) or len(line['text']) > LINE_CHARACTERS):
            raise ServiceError(400, f'Each line of the talk needs a role and a text of up to {LINE_CHARACTERS:,} characters.')
        if line['text'].strip():
            lines.append((line['role'], line['text'].strip()))
    if sum(len(text) for _, text in lines) > TALK_CHARACTERS:
        raise ServiceError(413, 'This brainstorm is too long for the next steps. Start a new one.')
    if finish and not any(role == 'user' for role, _ in lines):
        raise ServiceError(400, 'Say what you want to build before you ask for the next steps.')
    return project_id, lines, finish


def _messages(project: str, home: Path, lines: list[tuple[str, str]], finish: bool) -> list[dict]:
    """The model call: what the profile knows, then the talk. The talk starts with the person, and two lines of one side
    in a row are one turn. An answer ends on what the person said; the next steps end on their request."""
    known = _context(home)
    system = (WRITTEN if finish else SPOKEN).format(project=project)
    if known:
        system += f'\n\nWhat you know about the project and the person:\n\n{known}'
    if not finish:
        while len(lines) > 1 and sum(len(text) for _, text in lines) > ANSWER_CHARACTERS:
            lines = lines[1:]
    talk: list[dict] = []
    for role, text in lines:
        if talk and talk[-1]['role'] == role:
            talk[-1]['content'] += '\n\n' + text
        elif talk or role == 'user':
            talk.append({'role': role, 'content': text})
    if finish:
        steps = STEPS.format(project=project)
        if talk and talk[-1]['role'] == 'user':
            talk[-1]['content'] += '\n\n' + steps
        else:
            talk.append({'role': 'user', 'content': steps})
    else:
        while talk and talk[-1]['role'] == 'assistant':
            talk.pop()
    return [{'role': 'system', 'content': system}, *talk]


def _context(home: Path) -> str:
    parts = []
    for name in CONTEXT:
        path = home / name
        try:
            text = path.read_text(errors='replace').strip() if path.is_file() else ''
        except OSError:
            text = ''
        if text:
            parts.append(text[:CONTEXT_CHARACTERS])
    return '\n\n'.join(parts)


def _worker(project_id: str, provider, env: dict, cwd) -> _Worker:
    with _workers_lock:
        worker = _workers.get(project_id)
        if worker is None or not worker.alive:
            worker = _Worker(provider, env, cwd, Path(env['TMPDIR']) / 'brainstorm.log')
            _workers[project_id] = worker
        return worker


class _Gone(Exception):
    """The worker ended: it stopped after its idle time, or Hermes failed to start in it."""


class _Worker:
    """One process of brainstorm_worker.py for one project. It answers one call at a time, and a reader thread hands
    each answer over. Its standard error goes to brainstorm.log in the bot's tmp folder, new for each worker."""

    def __init__(self, provider, env: dict, cwd, log: Path):
        with open(log, 'w') as errors:
            self.process = subprocess.Popen([provider.python, str(WORKER), str(provider.hermes_root)], cwd=cwd, env=env,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
        self.answers: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._read, name='brainstorm-worker', daemon=True).start()

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def _read(self):
        for line in self.process.stdout:
            try:
                self.answers.put(json.loads(line))
            except ValueError:
                continue
        self.answers.put(None)

    def ask(self, messages: list[dict], max_tokens: int) -> str:
        request_id = uuid.uuid4().hex
        with self.lock:
            try:
                self.process.stdin.write(json.dumps(dict(id=request_id, messages=messages, max_tokens=max_tokens,
                                                         timeout=TIMEOUT)) + '\n')
                self.process.stdin.flush()
            except (OSError, ValueError):
                raise _Gone()
            deadline = time.monotonic() + TIMEOUT
            while True:
                try:
                    answer = self.answers.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    # A late answer would come after the next call: a new worker takes the next call instead.
                    self.process.kill()
                    raise ServiceError(504, 'The model took too long to answer. Try again.')
                if answer is None:
                    raise _Gone()
                if answer.get('id') == request_id:
                    break
        if answer.get('error'):
            raise ServiceError(502, f"The model did not answer: {answer['error']}")
        text = str(answer.get('text') or '').strip()
        if not text:
            raise ServiceError(502, 'The model gave no answer. Try again.')
        return text
