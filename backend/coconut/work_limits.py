"""The limits of parallel work. Hermes config holds them, and Coconut fills only the defaults that Hermes lacks.

- Per agent: `kanban.max_in_progress_per_profile` of the board home. Automatic is 2: a chat and a work session.
- Helpers per task: `delegation.max_concurrent_children` of the default Hermes profile, which every chat shares (parallel_work).

There is no total limit: every agent works at once. Hermes still starts no new work while the memory of the host is
under critical pressure (kanban_db_dispatch._tick_spawn_budget).
"""
AGENT_AUTOMATIC = 2
BOARD_KEYS = {'per_agent': 'max_in_progress_per_profile'}


def validate_limit(value):
    if value is not None and (type(value) is not int or not 1 <= value <= 64):
        raise ValueError('Choose between 1 and 64, or use Automatic.')


def _configured(value):
    # Hermes ignores a value that is not a positive integer (kanban_db_dispatch._positive_int).
    return value if type(value) is int and value >= 1 else None


def board_limits(config):
    """The per-agent limit of a board home's Hermes config."""
    kanban = (config or {}).get('kanban') or {}
    automatic = {'per_agent': AGENT_AUTOMATIC}
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
