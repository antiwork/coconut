"""Map Coconut worker preferences to native Hermes delegation settings."""
from functools import lru_cache
import os
import subprocess
import sys

if __package__:
    from .native_settings import raw_config, save_config
else:
    from native_settings import raw_config, save_config


def validate_limit(value):
    if value is not None and (type(value) is not int or not 1 <= value <= 64):
        raise ValueError('Choose between 1 and 64 workers, or use Automatic.')


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


def state(home):
    limit = (raw_config(home).get('delegation') or {}).get('max_concurrent_children')
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError('Hermes has an invalid delegation.max_concurrent_children setting.')
    cpus, memory = hardware()
    automatic = automatic_limit(cpus, memory)
    return dict(limit=limit, effective_limit=limit if limit is not None else automatic,
                automatic_limit=automatic)


def save(home, limit, *, inherit=False):
    validate_limit(limit)
    config = raw_config(home)
    delegation = config.setdefault('delegation', {})
    if limit is None and not inherit:
        delegation.pop('max_concurrent_children', None)
    else:
        delegation['max_concurrent_children'] = limit
    save_config(home, config)


def prepare_turn(limit):
    """Apply the worker limit of the Coconut settings through Hermes's supported fallback.

    An explicit delegation.max_concurrent_children in the profile takes precedence.
    """
    validate_limit(limit)
    if limit is None:
        os.environ.pop('DELEGATION_MAX_CONCURRENT_CHILDREN', None)
    else:
        os.environ['DELEGATION_MAX_CONCURRENT_CHILDREN'] = str(limit)
