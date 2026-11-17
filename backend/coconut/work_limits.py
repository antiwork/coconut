"""The limits of parallel work. Hermes config holds them, and Coconut fills only the defaults that Hermes lacks.

- Total: `kanban.max_in_progress` of Coconut's board home. Hermes derives no default when it cannot read the memory of
  the host, as on macOS, so Automatic is the limit of this Mac (automatic_limit).
- Per agent: `kanban.max_in_progress_per_profile` of the board home. Automatic is 2: a chat and a work session.
- Helpers per task: `delegation.max_concurrent_children` of the default Hermes profile, which every chat shares (parallel_work).
"""
from functools import lru_cache
import os
import subprocess
import sys

AGENT_AUTOMATIC = 2
BOARD_KEYS = {'total': 'max_in_progress', 'per_agent': 'max_in_progress_per_profile'}


def validate_limit(value):
    if value is not None and (type(value) is not int or not 1 <= value <= 64):
        raise ValueError('Choose between 1 and 64, or use Automatic.')


@lru_cache(maxsize=1)
def hardware():
    cpus = os.cpu_count() or 2
    memory = None
    try:
        if sys.platform == 'darwin':
            memory = int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], timeout=2))
        else:
            memory = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return cpus, memory


def automatic_limit(cpus, memory):
    if memory is None:
        return min(2, max(1, cpus // 2))
    # Leave CPU and memory available for the host, builds, and interactive work.
    return max(1, min(10, cpus // 2, (memory // (1024 ** 3) - 4) // 2))


def _configured(value):
    # Hermes ignores a value that is not a positive integer (kanban_db_dispatch._positive_int).
    return value if type(value) is int and value >= 1 else None


def board_limits(config):
    """The total and per-agent limits of a board home's Hermes config."""
    kanban = (config or {}).get('kanban') or {}
    automatic = {'total': automatic_limit(*hardware()), 'per_agent': AGENT_AUTOMATIC}
    result = {}
    for name, key in BOARD_KEYS.items():
        limit = _configured(kanban.get(key))
        result[name] = dict(limit=limit, effective_limit=limit or automatic[name], automatic_limit=automatic[name])
    return result


def set_board_limit(config, name, limit):
    validate_limit(limit)
    kanban = config.get('kanban')
    if not isinstance(kanban, dict):
        kanban = config['kanban'] = {}
    if limit is None:
        kanban.pop(BOARD_KEYS[name], None)
    else:
        kanban[BOARD_KEYS[name]] = limit
    return config
