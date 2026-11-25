"""Chat turns on mote instead of Hermes, for the agents that the person moves there.

mote (https://github.com/slavingia/mote) is a small agent: one loop, four tools and a home of plain files. An agent (a
project and its chats) runs on mote while the folder of its Hermes profile holds the file `mote.on`. The setting
`agent_runtime` of coconut_settings writes and removes that file (coconut_chat.change), and a shell can do the same.

A chat turn of such an agent goes to the gateway bridge (gateway_bridge.py) under the home `<profile>/mote`, the home of
mote for that agent, instead of the Hermes home of the profile, so the Hermes gateway never takes it
(gateway_chat._run_in_gateway). A thread of this service takes the turns of that home as the coconut adapter of Hermes
takes the others (hermes_plugin/coconut/adapter.py): it polls the bridge in this process, runs each turn in
`mote --json` with one mote session for each chat, and reports what mote does to the bridge. From the bridge on,
everything is as for Hermes: the phone, a stop, cards and the other Coconut tools, which mote calls with the shell
command `coconut` (mote_tool.py).

A card that a mote turn makes carries `_runtime: 'mote'` (home_runtime), and its check runs in the mote session of the
chat, which holds the conversation that the card came from (gateway_chat.run_gateway_context). The service takes only
coconut_refresh_decision from the check, and the chat gets no message of it (service._start_context). The instruction
revision of such a card hashes SOUL.md and BODY.md of the mote home, which the check reads (card_runtime).

What mote lacks stays on Hermes: the routines of the agent run in the Hermes gateway as before, and so do status
questions, the turns of a work session with a Kanban claim, the checks of the other cards and the turns of a team chat.
A turn that mote cannot take, for example on a Mac without the mote command, runs on Hermes.

The first turn makes the mote home from the profile: mote's own files, the profile's SOUL.md with the hint of the
Coconut platform, its memory in BODY.md and a copy of its skills. From then on mote keeps its own home, and Hermes keeps
its own. mote reads its model from `<profile>/mote/config.json` and the key from the environment of this service or
from `<profile>/mote.env` (KEY=value lines). Only the mote process gets these, and mote keeps its model keys out of its
shell commands. A Bedrock API key (AWS_BEARER_TOKEN_BEDROCK, with BEDROCK_REGION) never reaches mote: the service
relays mote's model calls to Bedrock (bedrock_relay.py), and mote gets the address and the token of the relay.
"""
from __future__ import annotations

import ast
import collections
import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

LOG = logging.getLogger(__name__)

RUNTIMES = ('hermes', 'mote')
MARKER = 'mote.on'
HOME = 'mote'
ENV_FILE = 'mote.env'
# The mote session of each chat of the agent, and the records (native_conversation.turn_records) that it holds.
SESSIONS = 'coconut-sessions.json'
POLL_SECONDS = 25
STOP_SECONDS = 15
# mote puts this many characters of BODY.md in each request. Its own BODY.md and a profile's memory fit.
BODY_IN_PROMPT = 6000
PLACEHOLDER = '(nothing yet)'
SKILL = 'coconut-tools'
INSTALL = 'uv tool install git+https://github.com/slavingia/mote'
MISSING = f'mote is not installed on this Mac. Install it with `{INSTALL}`, then switch again.'
# A tool call in mote's state, as a tool of Hermes that turn_phase.tool_phase knows, with the argument it reads.
KINDS = {'execute': ('terminal', 'command'), 'delete': ('terminal', 'command'), 'move': ('terminal', 'command'),
         'read': ('read_file', 'path'), 'edit': ('patch', 'path'), 'search': ('search_files', 'pattern'),
         'fetch': ('web_extract', 'url')}
COMMAND_TEXT = 'This agent runs on mote, which has no Hermes commands. Write what you want in words.'
FAILED = 'mote could not finish this turn.'
STOPPED = 'mote stopped this turn.'
RUNTIME_TEXT = {'hermes': 'Hermes Agent: every tool, routines, helpers and the board.',
                'mote': ('mote: a small agent with four tools and a home of plain files. The routines of the agent, '
                         'its work on the board, its status questions and the checks of cards from Hermes stay on '
                         'Hermes.')}
SWITCH_NOTE = ('The next turn of each chat of this agent runs there, and so do the checks of the cards that mote makes. '
               'Its routines, its work on the board, its status questions and the checks of cards from Hermes stay on '
               'Hermes.')
# The start of the text of a turn with a mode (gateway_bridge.submit_turn). The Coconut text of such a turn names the
# Coconut tools by their Hermes names, and mote has only shell commands.
# A check on mote can run any shell command, as each turn on mote can: mote asks for no approval, and the person who
# moves an agent to mote accepts that (docs/architecture.md). The service limits only the Coconut tools of the check.
MODE_TEXT = {
    'refresh': ('This turn runs on mote. Here coconut_refresh_decision is the command '
                "`coconut refresh_decision '<json>'`, and `coconut schema refresh_decision` gives its fields. It is the "
                'only Coconut tool of this check. Commands that only read are allowed.\n\n'),
}
TOOLS_NOTE = ('Here the tools of Coconut are the shell command `coconut` (skill coconut-tools): `coconut ask` for '
              'coconut_ask, `coconut suggest` for coconut_suggest, and so on. `coconut tools` lists them.')
SKILL_TEXT = """---
name: coconut-tools
description: Cards, the board, documents, other agents and the settings of Coconut, through the shell command `coconut`.
---

# Coconut tools

This agent runs in Coconut on mote. The tools of Coconut are one shell command, `coconut`, which acts for the turn
that runs it:

    coconut tools                 the tools, one line each
    coconut schema ask            the fields of one tool, as JSON Schema
    coconut ask '<json>'          call it with its fields as one JSON object; the answer is JSON
    coconut ask - <<'EOF'         the same, with the JSON on stdin, for a long text

Where the rules of Coconut name coconut_ask, coconut_suggest and so on, they mean these commands.

- Read `coconut schema <tool>` before the first call of a tool. The fields of a card are strict.
- `coconut ask` makes a card that waits for the person. It ends your turn: write your answer first, ask, then stop.
- `coconut suggest` proposes optional work and you go on. `coconut resolve_reply` records a typed answer to a card.
- `coconut kanban_create`, `kanban_list`, `kanban_show` and `kanban_comment` act on the board of Coconut.
- `coconut save_document`, `read_document` and `delete_document` keep pages in the Library of the app.
- `coconut message_bot` hands work to another agent of this Mac.
- `coconut settings` reads and changes the settings of Coconut, in the Coconut agent only. The setting
  `agent_runtime` moves an agent between Hermes and mote.
"""

_RUNTIME_LOCK = threading.Lock()


def command() -> str | None:
    """The mote command: COCONUT_MOTE, mote on the PATH, or the one that `uv tool install` puts in ~/.local/bin."""
    named = os.environ.get('COCONUT_MOTE')
    if named:
        return named if os.access(named, os.X_OK) else None
    found = shutil.which('mote')
    if found:
        return found
    local = Path.home() / '.local' / 'bin' / 'mote'
    return str(local) if os.access(local, os.X_OK) else None


def runtime(profile) -> str:
    """The runtime of the agent of a Hermes profile folder: mote while the folder holds the marker, else hermes."""
    return 'mote' if (Path(profile) / MARKER).is_file() else 'hermes'


def switch(profile, value: str) -> str:
    """Move the agent of a profile folder to `value`, hermes or mote. The next turn of each of its chats runs there."""
    from .service_base import ServiceError
    profile = Path(profile)
    if value not in RUNTIMES:
        raise ServiceError(400, 'Choose hermes or mote.')
    marker = profile / MARKER
    if value == 'hermes':
        marker.unlink(missing_ok=True)
        return value
    if command() is None:
        raise ServiceError(409, MISSING)
    try:
        prepare(profile)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise ServiceError(409, f'mote could not set up its home in {profile / HOME}: {error}') from None
    marker.write_text('This agent runs on mote. Remove this file to run it on Hermes again.\n')
    return value


def hint() -> str:
    """The hint of the Coconut platform of Hermes (hermes_plugin/coconut/__init__.py, HINT), for mote's SOUL.md."""
    path = Path(__file__).resolve().parents[2] / 'hermes_plugin' / 'coconut' / '__init__.py'
    try:
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(getattr(target, 'id', None) == 'HINT' for target in node.targets):
                return ast.literal_eval(node.value) + ' ' + TOOLS_NOTE
    except (OSError, SyntaxError, ValueError):
        LOG.warning('The hint of the Coconut platform is missing from the home of mote.')
    return TOOLS_NOTE


def _read(path: Path) -> str:
    try:
        return path.read_text(errors='replace')
    except OSError:
        return ''


def _write(path: Path, text: str) -> None:
    """Write a file whole, so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.' + path.name + '-', delete=False) as handle:
        handle.write(text)
    os.replace(handle.name, path)


def read_env(path: Path) -> dict:
    """KEY=value lines of a file, without comments and quotes."""
    values = {}
    for line in _read(path).splitlines():
        line = line.strip()
        if line.startswith('export '):
            line = line[len('export '):].strip()
        key, equals, value = line.partition('=')
        key = key.strip()
        if equals and key and not key.startswith('#') and key.replace('_', '').isalnum():
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
            values[key] = value
    return values


def base_env() -> dict:
    """The environment of mote: this service's, without Hermes, Coconut and Python settings, with the usual folders of
    commands on the PATH."""
    from .bedrock_relay import KEY, REGION
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('HERMES_', 'COCONUT_'))
           and key not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', KEY, REGION)}
    paths = (env.get('PATH') or '/usr/bin:/bin:/usr/sbin:/sbin').split(':')
    paths += ['/opt/homebrew/bin', '/usr/local/bin', str(Path.home() / '.local' / 'bin')]
    env['PATH'] = ':'.join(dict.fromkeys(path for path in paths if path))
    return env


def write_skill(home: Path) -> None:
    path = home / 'skills' / SKILL / 'SKILL.md'
    if _read(path) != SKILL_TEXT:
        _write(path, SKILL_TEXT)


def prepare(profile) -> Path:
    """The mote home of a profile folder. The first call makes it: mote writes its own files, then the home gets the
    profile's SOUL.md with the hint of Coconut, the profile's memory at the top of BODY.md, and a copy of its skills.
    mote reads skills one folder deep, so the skills of Hermes's categories land side by side."""
    profile = Path(profile)
    home = profile / HOME
    if (home / 'config.json').is_file():
        write_skill(home)
        return home
    mote = command()
    if mote is None:
        raise RuntimeError(MISSING)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mote makes SOUL.md, BODY.md and config.json in a home without config.json, and ends at the end of its input.
    subprocess.run([mote, '--json'], input='', env=dict(base_env(), MOTE_HOME=str(home)), capture_output=True,
                   text=True, timeout=60, cwd=str(home))
    if not (home / 'config.json').is_file():
        raise RuntimeError(f'{mote} made no config.json in {home}.')
    soul = _read(profile / 'SOUL.md').strip() or _read(home / 'SOUL.md').strip()
    _write(home / 'SOUL.md', soul + '\n\n' + hint() + '\n')
    memory = '\n\n'.join(text.strip().replace('\n§\n', '\n\n') for text in
                         (_read(profile / 'memories' / 'USER.md'), _read(profile / 'memories' / 'MEMORY.md'))
                         if text.strip())
    if memory:
        # mote's own BODY.md is a heading over the placeholder "(nothing yet)", which the memory replaces.
        body = '\n'.join(line for line in _read(home / 'BODY.md').strip().splitlines() if line.strip() != PLACEHOLDER)
        _write(home / 'BODY.md', body.strip() + '\n\n' + memory + '\n')
    skills = profile / 'skills'
    if skills.is_dir():
        for found in sorted(skills.rglob('SKILL.md')):
            target = home / 'skills' / found.parent.name
            if found.parent == skills or target.exists():
                continue
            shutil.copytree(found.parent, target, symlinks=True,
                            ignore=shutil.ignore_patterns('.env', '*.env', '__pycache__', '.git'))
    config = json.loads(_read(home / 'config.json') or '{}')
    config['body_in_prompt'] = max(int(config.get('body_in_prompt') or 0), BODY_IN_PROMPT)
    _write(home / 'config.json', json.dumps(config, indent=2) + '\n')
    write_skill(home)
    return home


def turn_home(service, bot: dict, home):
    """The home of the bridge for a chat turn of `bot`: the mote home while its agent runs on mote, else `home`, the
    Hermes home of its profile. Any trouble with mote leaves the turn on Hermes."""
    try:
        if runtime(home) != 'mote':
            return home
        with _RUNTIME_LOCK:
            runner = getattr(service, '_mote_runtime', None)
            if runner is None:
                runner = service._mote_runtime = MoteRuntime(service)
        return runner.home_for(Path(home))
    except Exception:
        LOG.exception('mote could not take a turn of %s, so it runs on Hermes.', home)
        return home


def home_runtime(service, home) -> str | None:
    """'mote' for the home of a turn that mote runs (a home that a mote thread of this service polls), else None."""
    runner = getattr(service, '_mote_runtime', None)
    if runner is None or not home:
        return None
    with runner.lock:
        return 'mote' if os.path.realpath(home) in runner.pollers else None


def card_runtime(service, decision: dict) -> str:
    """The runtime that checks a card, and so whose instructions its revision hashes: mote for a card that a mote turn
    made in a chat of an agent that still runs on mote, else hermes. The routing of the check
    (gateway_chat.run_gateway_context) and the revision of the card (decision_freshness) both use it. Without the mote
    command it gives hermes, as turn_home does. Only an unexpected error in turn_home, for example in prepare, still
    runs the check on Hermes once, and that check records the mote revision."""
    if decision.get('_runtime') != 'mote':
        return 'hermes'
    from .gateway_bridge import plain_chat_turn
    from .profiles import ProfileError
    from .service_base import ServiceError
    try:
        if not plain_chat_turn(service._get('tasks', decision['task_id'])) or command() is None:
            return 'hermes'
        return runtime(service.profiles.home(service._get('bots', decision['bot_id'])))
    except (OSError, ProfileError, ServiceError):  # a card whose task or agent is gone must not stop a card pass
        return 'hermes'


class MoteRuntime:
    """The threads that take the turns of the mote homes from the bridge and run them in mote."""

    def __init__(self, service):
        self.service = service
        self.bridge = service.gateway_bridge
        self.folder = self.bridge.folder.parent / 'mote'
        self.lock = threading.Lock()
        # The polling thread of each mote home, the home of each running turn, and the mote process of each chat.
        self.pollers: dict[str, threading.Thread] = {}
        self.active: dict[str, str] = {}
        self.running: dict[str, subprocess.Popen | None] = {}
        # The relay to Bedrock of each key and region (bedrock_relay.py).
        self.relays: dict[tuple[str, str], object] = {}

    def home_for(self, profile: Path) -> Path:
        if command() is None:
            LOG.warning('%s The agent of %s runs on Hermes.', MISSING, profile)
            return profile
        home = prepare(profile)
        key = os.path.realpath(home)
        with self.lock:
            thread = self.pollers.get(key)
            if thread is None or not thread.is_alive():
                thread = threading.Thread(target=self._poll, args=(home,), daemon=True, name=f'mote {profile.name}')
                self.pollers[key] = thread
                thread.start()
        # The first poll connects the home, so the turn does not wait for a gateway (gateway_chat._run_in_gateway).
        deadline = time.monotonic() + 3
        while not self.bridge.connected(home) and time.monotonic() < deadline:
            time.sleep(0.05)
        return home

    def _poll(self, home: Path) -> None:
        key = os.path.realpath(home)
        stopping = getattr(self.service, 'stopping', None) or threading.Event()
        while not self.bridge.closed and not stopping.is_set():
            with self.lock:
                active = sorted(turn for turn, owner in self.active.items() if owner == key)
            try:
                answer = self.bridge.poll({'home': key, 'active': active, 'wait': POLL_SECONDS})
            except Exception:
                LOG.exception('The mote thread of %s could not poll the bridge.', home)
                stopping.wait(5)
                continue
            for chat in answer.get('stop') or []:
                self._stop(str(chat))
            for turn in answer.get('turns') or []:
                if not isinstance(turn, dict) or not turn.get('id') or not turn.get('chat_id'):
                    continue
                with self.lock:
                    self.active[str(turn['id'])] = key
                threading.Thread(target=self._run, args=(home, turn), daemon=True, name='mote turn').start()

    def _run(self, home: Path, turn: dict) -> None:
        turn_id = str(turn['id'])
        try:
            outcome, detail = self._turn(home, turn)
        except Exception:
            LOG.exception('A turn of mote failed.')
            outcome, detail = 'failed', FAILED
        # The phone shows the detail of a turn that did not succeed (gateway_bridge.TurnEvents).
        self._report(turn_id, 'done', outcome=outcome, **({'detail': detail} if detail and outcome != 'success' else {}))
        with self.lock:
            self.active.pop(turn_id, None)

    def _turn(self, home: Path, turn: dict) -> tuple[str, str | None]:
        """Run one turn. Return its outcome and, for a turn that did not succeed, why."""
        turn_id, chat = str(turn['id']), str(turn['chat_id'])
        if turn.get('command'):
            self._report(turn_id, 'send', message_id=f'{turn_id}:command', text=COMMAND_TEXT)
            return 'success', None
        with self.lock:
            if chat in self.running:
                # One mote process for each session. The service runs the turn when the chat is free (GatewayBusy).
                return 'busy', None
            self.running[chat] = None
        try:
            return self._mote(home, turn, turn_id, chat)
        finally:
            with self.lock:
                self.running.pop(chat, None)

    def _mote(self, home: Path, turn: dict, turn_id: str, chat: str) -> tuple[str, str | None]:
        entry = dict(self._sessions(home).get(chat) or {})
        session = entry.get('session')
        if session and not (home / 'sessions' / f'{session}.jsonl').is_file():
            session, entry = None, {}
        # mote keeps each prompt in its session, so a record goes to a session once, as Hermes adds it once
        # (hermes_plugin/coconut/hooks.py, add_records).
        sent = set(entry.get('records') or [])
        records = [record for record in turn.get('records') or [] if isinstance(record, dict)
                   and isinstance(record.get('id'), str) and isinstance(record.get('text'), str)
                   and record.get('kind') != 'instructions' and record['id'] not in sent]
        # mote has no hook that holds the tools of a turn with a mode, as the Hermes plugin does (hooks.guard_tool). The
        # service refuses the Coconut tools that such a turn may not call, and the note says what the turn may do.
        text = MODE_TEXT.get(turn.get('mode'), '') + str(turn.get('text') or '')
        instructions = next((record for record in turn.get('records') or [] if isinstance(record, dict)
                             and record.get('kind') == 'instructions' and isinstance(record.get('text'), str)), None)
        if instructions is not None:
            if (not instructions.get('noted') and self._note_due(home, session, entry.get('instructions'), instructions)
                    and instructions['text'] not in text):
                text += '\n\n' + instructions['text']
        if records:
            from .native_conversation import records_text
            text += records_text(records)
        images = [f"data:{image['type']};base64,{image['content_base64']}" for image in turn.get('images') or []
                  if isinstance(image, dict) and image.get('content_base64') and '/' in str(image.get('type'))]
        pause = self.folder / 'turns' / f'{turn_id}.paused'
        pause.parent.mkdir(parents=True, exist_ok=True)
        env = base_env()
        env.update(self._model_env(home.parent))
        env.update(MOTE_HOME=str(home), COCONUT_TURN=turn_id, COCONUT_BRIDGE=str(self.bridge.address_file),
                   COCONUT_PAUSE=str(pause))
        env['PATH'] = f'{self._bin()}:{env["PATH"]}'
        folder = turn.get('folder')
        cwd = folder if isinstance(folder, str) and os.path.isdir(folder) else str(Path.home())
        args = [command() or 'mote', '--json'] + (['--session', session] if session else [])
        log = open(self.folder / 'mote.log', 'a')
        try:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, cwd=cwd,
                                       env=env, text=True, bufsize=1, start_new_session=True)
        finally:
            log.close()
        with self.lock:
            self.running[chat] = process
        self._report(turn_id, 'typing')
        prompt = {'type': 'prompt', 'text': text}
        if images:
            prompt['images'] = images
        if self._send(process, prompt) and instructions is not None:
            # The entry keeps the one revision that the session saw, once mote has the prompt. The session event saves it.
            entry['instructions'] = instructions.get('revision')
        calls, said, outcome, paused, stopped, ended = set(), set(), 'failed', False, False, False
        # What mote reports in grey. The error that ends a turn comes after the idle state of the turn.
        notes = collections.deque(maxlen=4)
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('type') == 'note' and str(event.get('text') or '').strip():
                    notes.append(' '.join(str(event['text']).split()))
                    continue
                if ended:
                    continue
                if event.get('type') == 'session' and isinstance(event.get('id'), str):
                    entry['session'] = event['id']
                    self._save_session(home, chat, entry)
                    continue
                if event.get('type') != 'state' or not isinstance(event.get('patch'), dict):
                    continue
                patch = event['patch']
                for call, change in (patch.get('tool_calls') or {}).items():
                    if isinstance(change, dict) and change.get('kind') and call not in calls:
                        calls.add(call)
                        tool, field = KINDS.get(change['kind'], ('terminal', 'command'))
                        if not self._report(turn_id, 'tool', tool=tool, args={field: str(change.get('title') or '')}):
                            stopped = self._stop_process(process, stopped)
                messages = [(key, message) for key, message in (patch.get('messages') or {}).items()
                            if isinstance(message, dict) and key not in said and str(message.get('text') or '').strip()]
                for key, message in sorted(messages, key=lambda item: item[1].get('at') or 0):
                    said.add(key)
                    if not self._report(turn_id, 'send', message_id=f'{turn_id}:{key}', text=str(message['text'])):
                        stopped = self._stop_process(process, stopped)
                if not paused and pause.exists():
                    # A card or a recorded wait ended the turn (mote_tool.py).
                    paused, stopped = True, self._stop_process(process, stopped)
                if patch.get('state') == 'idle':
                    stop = patch.get('stop')
                    if paused or stop == 'end_turn' or (said and stop in ('max_tokens', 'refusal')):
                        outcome = 'success'
                    elif stop == 'cancelled':
                        outcome = 'cancelled'
                    # mote then writes its last notes and exits at the end of its input.
                    ended = True
                    self._close(process)
                    timer = threading.Timer(STOP_SECONDS, self._kill, (process,))
                    timer.daemon = True
                    timer.start()
        finally:
            self._close(process)
            try:
                process.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            pause.unlink(missing_ok=True)
        if records and entry.get('session'):
            entry['records'] = sorted(sent | {record['id'] for record in records})
            self._save_session(home, chat, entry)
        if outcome == 'cancelled':
            return outcome, STOPPED
        if outcome != 'success':
            LOG.warning('mote did not finish a turn of the chat %s: %s', chat, ' | '.join(notes) or 'no note')
            return outcome, (FAILED[:-1] + ': ' + notes[-1])[:400] if notes else FAILED
        return outcome, None

    @staticmethod
    def _note_due(home: Path, session: str | None, seen: str | None, record: dict) -> bool:
        """Whether a mote session gets the note about changed instructions, by the rule of the Hermes plugin
        (hooks.instructions_note_due). mote writes the system prompt, with SOUL.md, in the first line of a new session
        and reuses it on --session. So a new session gets no note, and a continuing one gets it once for each change. A
        session without a revision on record gets it when it started before the change: the creation time of its
        session file (st_birthtime, which macOS has; without it, no note)."""
        if not session:
            return False
        if seen is not None:
            return seen != record.get('revision')
        try:
            started = getattr((home / 'sessions' / f'{session}.jsonl').stat(), 'st_birthtime', None)
        except OSError:
            return False
        changed = record.get('changed_at')
        return started is not None and isinstance(changed, (int, float)) and started < changed

    @staticmethod
    def _close(process) -> None:
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass

    @staticmethod
    def _kill(process) -> None:
        if process.poll() is None:
            process.kill()

    def _model_env(self, profile: Path) -> dict:
        """The variables of `<profile>/mote.env` for mote. With a Bedrock key and no Anthropic key, mote calls the relay
        of this service, which holds the Bedrock key (bedrock_relay.py)."""
        from .bedrock_relay import KEY, REGION, Relay
        values = read_env(profile / ENV_FILE)
        key = values.pop(KEY, None) or os.environ.get(KEY)
        region = values.pop(REGION, None) or os.environ.get(REGION) or 'us-east-1'
        if key and not any(values.get(name) or os.environ.get(name)
                           for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')):
            with self.lock:
                relay = self.relays.get((key, region))
                if relay is None:
                    relay = self.relays[(key, region)] = Relay(key, region)
            values.update(ANTHROPIC_BASE_URL=relay.url, ANTHROPIC_API_KEY=relay.token)
        return values

    def _send(self, process, message: dict) -> bool:
        with self.lock:
            try:
                process.stdin.write(json.dumps(message) + '\n')
                process.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def _stop_process(self, process, stopped: bool) -> bool:
        if not stopped:
            self._send(process, {'type': 'stop'})
        return True

    def _stop(self, chat: str) -> None:
        """Stop the running turn of a chat: Coconut cancelled it."""
        with self.lock:
            process = self.running.get(chat)
        if process is not None:
            self._send(process, {'type': 'stop'})

    def _report(self, turn_id: str, kind: str, **fields) -> bool:
        """Report an event of a turn to the bridge. False when the turn no longer runs there."""
        try:
            return bool(self.bridge.event(dict(fields, turn_id=turn_id, kind=kind)).get('ok'))
        except Exception:
            LOG.exception('The bridge did not take a %s of a turn of mote.', kind)
            return False

    def _sessions(self, home: Path) -> dict:
        try:
            value = json.loads(_read(home / SESSIONS) or '{}')
        except ValueError:
            value = {}
        return value if isinstance(value, dict) else {}

    def _save_session(self, home: Path, chat: str, entry: dict) -> None:
        with self.lock:
            sessions = self._sessions(home)
            sessions[chat] = entry
            _write(home / SESSIONS, json.dumps(sessions, indent=1) + '\n')

    def _bin(self) -> Path:
        """The folder of the command `coconut` (mote_tool.py), which mote finds on its PATH."""
        folder = self.folder / 'bin'
        script = folder / 'coconut'
        tool = Path(__file__).with_name('mote_tool.py')
        text = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(tool))} "$@"\n'
        if _read(script) != text:
            _write(script, text)
            script.chmod(0o755)
        return folder
