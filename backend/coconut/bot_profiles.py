"""Every project chat runs on one Hermes profile: `default`, the Hermes home itself.

A project (a bot) is a name and its chats. Each chat is a Hermes session of its own on that one profile, keyed by the
chat ID, so the model, the memory, SOUL.md, the skills, the routines (cron) and the access are shared by all projects.
Coconut creates no profile, scans no profile folder, and deletes no profile.

The `profiles` table (profile_registry) still maps each bot to the Hermes home that runs it. A new bot maps to the
Hermes home. A bot of an earlier release can still map to a named profile in `<Hermes home>/profiles` until the
person merges that profile into `default` (profile_merge.py). Until then it keeps running there.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess

from . import profile_registry
from .profiles import SKILLS

LOG = logging.getLogger(__name__)
COMMAND_SECONDS = 300
# The name of a project before the first message of the person names it (agent_chats._title_chat).
NEW_CHAT_NAME = 'New chat'
SHARED_SETTINGS = ('All chats share one Hermes profile, so the model, instructions and access are the same for every '
                   'chat. Change them for all chats in the Coconut chat.')
# The fields that only a profile of its own could give one project.
PROFILE_FIELDS = ('instructions', 'approval_override', 'worker_limit')


class BotProfiles:
    def _init_profiles(self, hermes_home) -> None:
        # Without a Hermes home (fixture mode and tests), the one profile lives in <data>/hermes.
        self.uses_hermes = hermes_home is not None
        self.hermes_home = Path(hermes_home).expanduser().absolute() if hermes_home else self.root / 'hermes'
        profile_registry.ensure_table(self.db)
        # One dict, shared with the provider and the model catalog, holds the table in this process.
        self.profiles.rows = profile_registry.rows(self.db)
        catalog = getattr(self.provider, 'catalog', None)
        for target in (self.provider, catalog):
            if target is not None:
                try:
                    target.profile_rows = self.profiles.rows
                except AttributeError:
                    pass
        if self.uses_hermes and hasattr(catalog, 'use_hermes_home'):
            # The settings are those of the one profile, `<Hermes home>/config.yaml`.
            catalog.use_hermes_home(self.hermes_home)

    def _reload_profile_rows(self) -> None:
        """Follow the table after a rolled back transaction. Other threads read the dict, so it never becomes empty."""
        fresh = profile_registry.rows(self.db)
        self.profiles.rows.update(fresh)
        for bot_id in set(self.profiles.rows) - set(fresh):
            self.profiles.rows.pop(bot_id, None)

    def _write_profile_row(self, row: dict) -> None:
        profile_registry.write(self.db, row)
        self.profiles.rows[row['bot_id']] = dict(row, home=str(row['home']))

    def _attach_profile(self, bot: dict, profile: dict | None = None) -> None:
        """Run the bot on `profile` ({name, home}), by default the one profile: the Hermes home."""
        if profile is None:
            if not self.uses_hermes:
                # Fixture mode has no Hermes. The smallest profile lives inside the data directory.
                self.hermes_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            profile = dict(name=profile_registry.DEFAULT, home=str(self.hermes_home))
        self._write_profile_row(dict(bot_id=bot['id'], name=profile['name'], home=profile['home'], state='active'))
        bot.update(profile=profile['name'], profile_state='active')

    def _remove_profile_row(self, bot_id: str) -> None:
        """Remove from Coconut: the row stays `removed`. Hermes keeps the sessions of the chat."""
        row = self.profiles.rows.get(bot_id)
        if row is not None and row['state'] != 'removed':
            self._write_profile_row(dict(row, state='removed', seen_at=profile_registry.now()))

    def _check_shared_settings(self, data: dict) -> None:
        """Refuse a setting of one project that only a profile of its own could hold. Older apps still send them."""
        from .service_base import ServiceError
        if not isinstance(data, dict):
            return
        if data.get('model_override') is not None or any(field in data for field in PROFILE_FIELDS):
            raise ServiceError(409, SHARED_SETTINGS)

    def _mark_coconut_chat(self) -> None:
        """Keep the Coconut chat when every chat runs on the default profile.

        The Coconut chat was the agent of the default profile (coconut_chat.is_coconut). Now every chat maps to that
        profile, so the bot keeps the mark `coconut_chat` instead. The mark goes once to the agent that the default
        profile ran before this release. The caller holds the transaction.
        """
        if self._setting('coconut_chat_marked'):
            return
        bots = {bot['id']: bot for bot in self._all('bots')}
        for row in self.profiles.rows.values():
            bot = bots.get(row['bot_id'])
            if (bot and row['name'] == profile_registry.DEFAULT and row['state'] == 'active'
                    and not bot.get('chat_of') and not bot.get('_deleted')):
                bot['coconut_chat'] = True
                self._put('bots', bot)
                break
        from .service_base import encoded
        self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        ('coconut_chat_marked', encoded(True)))

    def _start_hermes_entries(self) -> None:
        """Set Coconut's own entries in the Hermes home again, in the background, at each start.

        The entries are the skills folder in `skills.external_dirs` and the `coconut` gateway platform (skill_dirs.py,
        gateway_plugin.py), and the provider keys that the `.env` lacks (model_keys.py). Only these keys change. A
        failure is a warning: the service starts all the same.
        """
        python, source = getattr(self.provider, 'python', None), getattr(self.provider, 'hermes_root', None)
        if self.uses_hermes and python and source:
            import threading
            self.hermes_entries = threading.Thread(target=self._set_hermes_entries, args=(python, source),
                                                   name='coconut-hermes-entries', daemon=True)
            self.hermes_entries.start()

    def _set_hermes_entries(self, python, source) -> bool:
        from .hermes_runtime import NO_LAZY_INSTALLS
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR')
                       if key in os.environ}
        environment.update(NO_LAZY_INSTALLS, HERMES_HOME=str(self.hermes_home), HERMES_DISABLE_TELEMETRY='1',
                           # Earlier versions saved model keys here. The edit moves them into the Hermes profiles.
                           COCONUT_SAVED_MODEL_KEYS=str(self.root / 'credentials.json'))
        try:
            result = subprocess.run([str(python), '-I', '-B', str(Path(__file__).with_name('skill_dirs.py')), 'add',
                                     str(source), str(SKILLS), str(self.hermes_home)], env=environment,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=COMMAND_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            LOG.warning('Coconut could not set its entries in the Hermes profiles: %s', error)
            return False
        if result.returncode:
            LOG.warning('Coconut could not set all its entries in the Hermes profiles: %s',
                        (result.stdout + result.stderr)[-2000:])
            return False
        changed = [line for line in result.stdout.splitlines() if not line.endswith('unchanged')]
        if changed:
            LOG.info('Coconut set its entries in the Hermes profiles again: %s', '; '.join(changed))
        return True
