"""The notice of Hermes for a background process that ended, as data for its own row in the chat.

When a background process of a gateway turn ends, Hermes sends one line to the chat
(gateway/run.py, `_format_concise_process_notification`, the default `concise` mode of
`display.background_process_notifications`):

    ✅ Background task finished — `cd ~/Developer/coconut && npm test` (5m 26s)
    ❌ Background task failed — `make build` (12s, exit 2). Last output:
    ```
    error: …
    ```
    Ask me to rerun it or show the full log.

Hermes cuts the command to 80 characters and puts it on one line. A failure adds the exit code, up to five lines of
output and a rerun hint. Each fixed part is a lookup in the message catalog of Hermes (locales/en.yaml,
`gateway.background.*`). When the catalog does not load, the lookup returns the bare name, so the line starts with
`gateway.background.task_finished`, and the output tail is lost.

`notice` reads a whole message of exactly this shape. The message keeps its text, which an older app shows, and gets
`process_notice` with the parts. Any other text, such as a reply that quotes the notice, gets no notice.
`readable_text` writes a notice with the raw names in the English words of Hermes, as hermes_notices does for other
raw names.

The English words come from locales/en.yaml of Hermes 0.22.4. Coconut reads no YAML, so a notice in another language
of Hermes stays a text message.
"""
from __future__ import annotations

import posixpath
import re

# The catalog keys of Hermes and their English text (locales/en.yaml, gateway.background).
KEYS = {
    'task_finished': '✅ Background task finished',
    'task_failed': '❌ Background task failed',
    'exit_code': 'exit {code}',
    'last_output': '. Last output:\n```\n{tail}\n```',
    'rerun_hint': '\nAsk me to rerun it or show the full log.',
}
RAW = {key: f'gateway.background.{key}' for key in KEYS}

_DURATION = r'(?:\d+h \d+m|\d+m \d+s|\d+s)'
_HEAD = re.compile(
    r'(?P<status>' + '|'.join(re.escape(KEYS[k]) + '|' + re.escape(RAW[k]) for k in ('task_finished', 'task_failed')) +
    r')(?: \u2014 `(?P<command>[^\n]+)`)?'
    r'(?: \((?:(?P<duration>' + _DURATION + r')(?:, (?P<exit>{exit}))?|(?P<exit_only>{exit}))\))?'.format(
        exit=r'exit -?\d+|' + re.escape(RAW['exit_code'])))
_TAIL = re.compile(r'\. Last output:\n```\n(?P<tail>.+?)\n```', re.S)
_RERUN = (KEYS['rerun_hint'], RAW['rerun_hint'])
_CD = re.compile(r'cd\s+(?P<path>[^\s&;]+)(?:\s*(?:&&|;)\s*(?P<rest>.*))?')


def notice(text) -> dict | None:
    """The parts of a background-process notice of Hermes, or None for each other text.

    The parts: ok, command (as Hermes shows it, at most 80 characters), label (the command without a leading `cd`),
    place (the repository or folder of that `cd`), duration ("5m 26s"), exit_code and output (the last lines of a
    failure). A part that the notice does not have is absent.
    """
    if not isinstance(text, str) or not text.startswith(('✅ Background task ', '❌ Background task ',
                                                         'gateway.background.task_')):
        return None
    head = _HEAD.match(text)
    exit_code = head and (head['exit'] or head['exit_only'])
    if head is None or not (head['command'] or head['duration'] or exit_code):
        return None
    ok = head['status'] in (KEYS['task_finished'], RAW['task_finished'])
    rest, output = text[head.end():], None
    if ok:
        if rest or exit_code:
            return None
    else:
        # A failure ends with the rerun hint. The output tail comes before the hint.
        if not rest.endswith(_RERUN):
            return None
        rest = rest[:-len(next(hint for hint in _RERUN if rest.endswith(hint)))]
        if rest == RAW['last_output']:
            rest = ''
        elif rest:
            tail = _TAIL.fullmatch(rest)
            if tail is None:
                return None
            output, rest = tail['tail'], ''
        if rest:
            return None
    result: dict = {'ok': ok}
    if head['command']:
        result['command'] = head['command']
        result.update(_label(head['command']))
    if head['duration']:
        result['duration'] = head['duration']
    if exit_code and exit_code.startswith('exit '):
        result['exit_code'] = int(exit_code[5:])
    if output:
        result['output'] = output
    return result


def _label(command: str) -> dict:
    """A short name for the command: without a leading `cd <path> &&`, and the repository or folder of that path."""
    match = _CD.fullmatch(command)
    if match is None:
        return {'label': command}
    path = match['path'].rstrip('…').rstrip('/')
    if '/.worktrees/' in path or path.endswith('/.worktrees'):
        # A Git worktree of a repository: Coconut and Hermes keep them in <repository>/.worktrees/<id>.
        place = posixpath.basename(path.split('/.worktrees')[0])
    else:
        place = posixpath.basename(path) or path
    rest = (match['rest'] or '').strip()
    return {'label': rest or place or command, **({'place': place} if place and rest else {})}


def readable_text(text: str) -> str:
    """The text of a notice with raw catalog names, in the English words of Hermes. Each other text stays."""
    if not isinstance(text, str) or not text.startswith(RAW['task_finished'][:-len('finished')]):
        return text
    parts = notice(text)
    if parts is None:
        return text
    line = KEYS['task_finished'] if parts['ok'] else KEYS['task_failed']
    if 'command' in parts:
        line += f" \u2014 `{parts['command']}`"
    details = [parts['duration']] if 'duration' in parts else []
    if not parts['ok']:
        # With raw names, Hermes writes no exit code and no output, so the English text has none either.
        details += [KEYS['exit_code'].format(code=parts['exit_code'])] if 'exit_code' in parts else []
    if details:
        line += f" ({', '.join(details)})"
    return line + ('' if parts['ok'] else KEYS['rerun_hint'])
