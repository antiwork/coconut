"""Profiles rows for tests that run a provider without the service's copy of the table.

A bot with a row in the database of the data folder (a test that also runs a service) uses that profile. Any other bot
gets the home `<root>/bots/<id>/home`, as provider tests lay it out. Use `provider.profile_rows = LayoutRows(root)`.
"""
from pathlib import Path

from coconut import profile_registry


class LayoutRows(dict):
    def __init__(self, root):
        super().__init__()
        self.root = Path(root).resolve()

    def get(self, bot_id, default=None):
        if bot_id not in self:
            row = profile_registry.lookup(self.root, bot_id)
            if row is not None:
                return row
            home = self.root / 'bots' / bot_id / 'home'
            home.mkdir(parents=True, exist_ok=True)
            self[bot_id] = dict(bot_id=bot_id, name=bot_id, home=str(home), state='active')
        return self[bot_id]
