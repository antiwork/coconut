"""Coconut local execution service."""

__all__ = ['Service', 'ServiceError', 'YieldForDecision']


def __getattr__(name):
    # The package loads service.py only when a caller asks for one of these names. A team-only service (team_only.py)
    # never loads it, nor the modules of the full runtime that it imports.
    if name in __all__:
        from . import service
        return getattr(service, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
