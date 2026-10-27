"""Profiles rows for tests that run a provider without a service, with each bot home in `<root>/bots/<id>/home`.

The service keeps the real rows (bot_profiles). A provider test sets `provider.profile_rows = LayoutRows(root)`.
"""
from pathlib import Path


class LayoutRows(dict):
    def __init__(self, root):
        super().__init__()
        self.root = Path(root).resolve()

    def get(self, bot_id, default=None):
        if bot_id not in self:
            home = self.root / 'bots' / bot_id / 'home'
            home.mkdir(parents=True, exist_ok=True)
            self[bot_id] = dict(bot_id=bot_id, name=bot_id, home=str(home), state='active')
        return self[bot_id]
