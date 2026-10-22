"""Reduce native tool calls to one short phase that the phone can label."""
from __future__ import annotations

if __package__:
    from .worker_activity import command_phase
else:
    from worker_activity import command_phase

# The phone owns the labels. Keep this list in step with BotPhase in the iOS app.
# The command phases come from worker_activity.command_phase, so CLI workers and Hermes agree.
PHASES = ('thinking', 'working', 'reading', 'editing', 'command', 'testing', 'searching',
          'browsing', 'memory', 'github', 'helpers', 'writing')
TOOLS = {'read_file': 'reading', 'write_file': 'editing', 'patch': 'editing', 'search_files': 'searching',
         'web_search': 'browsing', 'web_extract': 'browsing', 'memory': 'memory', 'session_search': 'memory',
         'delegate_task': 'helpers', 'coconut_track_pr': 'github'}
# These calls are bookkeeping. The previous phase describes the work better.
QUIET = ('todo', 'clarify')


def tool_phase(name, args):
    if not isinstance(name, str) or name in QUIET:
        return None
    if name in TOOLS:
        return TOOLS[name]
    if name.startswith('browser_'):
        return 'browsing'
    if name.startswith('mcp_github'):
        return 'github'
    if name.startswith('coconut_'):
        return None
    if name in ('terminal', 'process', 'execute_code'):
        command = args.get('command') if isinstance(args, dict) else None
        return command_phase(command) if isinstance(command, str) and command.strip() else 'command'
    return 'working'
