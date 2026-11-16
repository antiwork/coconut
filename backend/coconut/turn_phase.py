"""The short phases of a turn that the phone can label."""
from __future__ import annotations

# The phone owns the labels. Keep this list in step with BotPhase in the iOS app. The gateway reports `thinking` and
# `writing` (gateway_bridge.TurnEvents).
PHASES = ('thinking', 'working', 'reading', 'editing', 'command', 'testing', 'searching',
          'browsing', 'memory', 'github', 'helpers', 'writing')


# The longest detail of a phase. The phone shows it in one line.
DETAIL_LIMIT = 120
TEST_WORDS = ('pytest', 'unittest', ' test', 'xcodebuild test', 'npm test', 'jest', 'vitest', 'rspec', 'go test',
              'cargo test')


def tool_phase(tool: str, args) -> tuple[str, str]:
    """The phase of a tool call and one short line of what it does: the command, the file or the query."""
    args = args if isinstance(args, dict) else {}
    name = str(tool or '')

    def first(*keys):
        for key in keys:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ''

    def file_name(path):
        return path.rstrip('/').rsplit('/', 1)[-1] if path else ''

    if name in ('terminal', 'process', 'execute_code', 'shell'):
        command = first('command', 'code').split('\n', 1)[0]
        lowered = ' ' + command.lower()
        if any(word in lowered for word in TEST_WORDS):
            phase = 'testing'
        elif lowered.lstrip().startswith(('gh ', 'git push', 'git pull', 'git fetch')):
            phase = 'github'
        else:
            phase = 'command'
        detail = command
    elif name in ('read_file',):
        phase, detail = 'reading', file_name(first('path'))
    elif name in ('write_file', 'patch', 'edit_file'):
        phase, detail = 'editing', file_name(first('path'))
    elif name in ('search_files',):
        phase, detail = 'searching', first('pattern')
    elif name in ('web_search',):
        phase, detail = 'browsing', first('query')
    elif name in ('web_extract',) or name.startswith('browser'):
        urls = args.get('urls')
        url = urls[0] if isinstance(urls, list) and urls and isinstance(urls[0], str) else first('url')
        phase, detail = 'browsing', url.split('://', 1)[-1].split('/', 1)[0] if url else ''
    elif name in ('memory', 'context_notes') or name.startswith('skill'):
        phase, detail = 'memory', ''
    elif name in ('delegate_task',):
        phase, detail = 'helpers', ''
    elif name.startswith('github') or name.startswith('connectors__github'):
        phase, detail = 'github', ''
    else:
        phase, detail = 'working', name.replace('_', ' ')
    detail = ' '.join(detail.split())
    if len(detail) > DETAIL_LIMIT:
        detail = detail[:DETAIL_LIMIT - 1] + '…'
    return phase, detail
