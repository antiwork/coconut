"""Reduce supported CLI events to bounded, non-sensitive progress evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

LABELS = {'starting': 'Starting worker', 'thinking': 'Worker is responding',
          'reading': 'Reading files', 'editing': 'Editing files', 'command': 'Running a command',
          'testing': 'Running tests', 'searching': 'Searching', 'github': 'Working in GitHub', 'finished': 'Worker finished',
          'failed': 'Worker failed'}
FRESH_SECONDS = 120


def progress_path(identity):
    from uuid import UUID
    UUID(identity)
    return Path(os.environ['HERMES_HOME']) / 'coconut-worker-progress' / (identity + '.json')


def command_phase(command):
    # Match executables, not words in comments or arbitrary tool output.
    try:
        words = shlex.split(command)
    except ValueError:
        return 'command'
    if words and Path(words[0]).name in ('bash', 'sh', 'zsh') and len(words) >= 3 and words[1] in ('-c', '-lc'):
        return command_phase(words[2])
    if words[:2] in (['bundle', 'exec'], ['uv', 'run']):
        words = words[2:]
    if words and Path(words[0]).name == 'gh':
        return 'github'
    if words and (Path(words[0]).name in ('rspec', 'pytest') or words[:3] in (
            ['python', '-m', 'pytest'], ['python3', '-m', 'pytest'],
            ['python', '-m', 'unittest'], ['python3', '-m', 'unittest'])
            or words[:2] in (['npm', 'test'], ['pnpm', 'test'], ['yarn', 'test'], ['swift', 'test'])):
        return 'testing'
    return 'command'


def event_phase(engine, event):
    if not isinstance(event, dict):
        return None
    kind = event.get('type')
    if engine == 'codex':
        item = event.get('item') or {}
        if kind in ('turn.failed', 'error'):
            return 'failed'
        if kind == 'turn.completed':
            return 'finished'
        if kind in ('item.started', 'item.updated', 'item.completed') and isinstance(item, dict):
            category = item.get('type')
            if category == 'command_execution':
                return command_phase(item.get('command', '')) if kind != 'item.completed' else 'thinking'
            return {'file_change': 'editing', 'web_search': 'searching', 'agent_message': 'thinking',
                    'reasoning': 'thinking', 'mcp_tool_call': 'command'}.get(category)
        return 'thinking' if kind == 'turn.started' else None
    if kind == 'result':
        return 'failed' if event.get('is_error') else 'finished'
    if kind == 'assistant':
        message = event.get('message') or {}
        blocks = message.get('content', []) if isinstance(message, dict) else []
        for block in reversed(blocks):
            if isinstance(block, dict) and block.get('type') == 'tool_use':
                name = block.get('name', '')
                if name == 'Bash':
                    return command_phase((block.get('input') or {}).get('command', ''))
                return {'Read': 'reading', 'Glob': 'searching', 'Grep': 'searching',
                        'Edit': 'editing', 'Write': 'editing', 'WebSearch': 'searching'}.get(name, 'command')
        return 'thinking'
    if kind == 'user':
        return 'thinking'
    return None


def read_progress(identity):
    try:
        path = progress_path(identity)
        if path.stat().st_size > 2048:
            return None
        value = json.loads(path.read_text())
        if value.get('phase') not in LABELS or not isinstance(value.get('at'), (int, float)):
            return None
        if not 0 <= time.time() - value['at'] < 86400:
            return None
        return {key: value[key] for key in ('phase', 'at')}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def progress_label(record, timestamp):
    from datetime import datetime
    progress = record.get('_worker_progress', {})
    started = datetime.fromisoformat(record.get('created_at', timestamp.isoformat()).replace('Z', '+00:00')).timestamp()
    age = max(0, timestamp.timestamp() - progress.get('at', started))
    phase = progress.get('phase')
    if phase in LABELS and age <= FRESH_SECONDS and phase not in ('finished', 'failed'):
        return LABELS[phase]
    if age <= FRESH_SECONDS:
        return 'Waiting for worker output'
    return f'Waiting for worker output · {int(age // 60)} min without an update'


def instrument(args, identity):
    """Wrap a simple headless invocation; leave other shell programs untouched."""
    command = args.get('command', '')
    if not isinstance(command, str) or any(c in command for c in ('\n', '$', '`', '*', '?')):
        return args
    # Keep the shell suffix byte-for-byte. Parsing file descriptors through shlex
    # would confuse a numeric prompt with a descriptor before a redirection.
    quote = None
    escaped = False
    redirect = len(command)
    for i, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char in '|;&(){}[]~#':
            return args
        elif char in '<>':
            redirect = i
            while redirect > 0 and command[redirect - 1].isdigit():
                redirect -= 1
            if redirect > 0 and not command[redirect - 1].isspace():
                return args
            break
    try:
        argv = shlex.split(command[:redirect])
    except ValueError:
        return args
    if '--' in argv or not argv or Path(argv[0]).name not in ('claude', 'codex'):
        return args
    suffix = command[redirect:]
    # Accept only simple quoted or unquoted file redirects, without shell expansion.
    target = r"(?:'[^']*'|\"[^\"]*\"|[^\s<>|;&(){}\[\]~]+)"
    if '#' in suffix:
        return args
    if suffix and not re.fullmatch(r"(?:\s*\d*(?:>>|>|<)\s*" + target + r")+\s*", suffix):
        return args
    engine = Path(argv[0]).name
    if engine == 'claude' and not any(w in ('-p', '--print') for w in argv[1:]):
        return args
    if engine == 'codex' and (len(argv) < 2 or argv[1] != 'exec'):
        return args
    # Structured/verbose callers already own their stream contract. Observe it unchanged.
    original = 'text'
    if engine == 'claude':
        if '--output-format' in argv:
            i = argv.index('--output-format')
            if i + 1 == len(argv) or argv[i + 1] not in ('text', 'json', 'stream-json'):
                return args
            original = argv[i + 1]
            argv[i + 1] = 'stream-json'
        elif any(w.startswith('--output-format=') for w in argv):
            return args
        else:
            argv += ['--output-format', 'stream-json']
        if original != 'stream-json' and '--verbose' in argv:
            return args
        if '--verbose' not in argv:
            argv.append('--verbose')
    else:
        original = 'stream-json' if '--json' in argv else 'text'
        if '--json' not in argv:
            argv.insert(2, '--json')
    wrapper = Path(__file__).with_name('worker_cli.py')
    wrapped = [sys.executable, str(wrapper), identity, engine, original, *argv]
    return dict(args, command=shlex.join(wrapped) + (' ' + suffix if suffix else ''))
