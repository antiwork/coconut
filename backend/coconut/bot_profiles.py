"""Coconut has three projects, Coconut, Gumroad and Personal, and each is served by its own Hermes profile.

A project is a bot without `chat_of`. Its chats are bots with `chat_of` (agent_chats.py), each a Hermes session of its
own on the project's profile. Nobody creates a project: the service makes the bot of a project once, when one of its profile
folders exists and no bot runs on it (`_ensure_projects`). Personal is free for everyone: without a profile of its own it
runs on the Hermes home, the `default` profile, which every Coconut has. Coconut creates no profile, scans no profile folder, and
deletes no profile.

The `profiles` table (profile_registry) maps each bot to the Hermes home that runs it. A bot of an earlier release that
is no project keeps its row and its chats, but takes no new chat (agent_chats._new_chat), and the apps list it under
no project.
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
# The projects: a key, the name, and the Hermes profiles that can serve it, first match wins. `default` is the Hermes
# home. The Coconut project is the Coconut chat (coconut_chat.py). Personal ran on `home` in earlier releases.
PROJECTS = (('coconut', 'Coconut', ('coconut',)), ('gumroad', 'Gumroad', ('gumroad',)),
            ('personal', 'Personal', ('personal', 'home', profile_registry.DEFAULT)))
ONLY_PROJECTS = 'Coconut has three projects, Coconut, Gumroad and Personal. Start a new chat in one of them.'
PROJECTS_STAY = 'Coconut, Gumroad and Personal stay: they are the projects of Coconut.'
SHARED_SETTINGS = ('A project has no settings of its own in Coconut. Change the model and the access in the Coconut '
                   'chat.')
# The fields that only a per-agent setting gave one bot.
PROFILE_FIELDS = ('instructions', 'approval_override', 'worker_limit')


class BotProfiles:
    def _init_profiles(self, hermes_home) -> None:
        # Without a Hermes home (fixture mode), the one profile lives in <data>/hermes.
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

    def _attach_profile(self, bot: dict, profile: dict) -> None:
        """Run the bot on `profile` ({name, home})."""
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

    def _project_ids(self) -> dict[str, str]:
        """The bot of each project: {bot ID: project key}. It is the oldest bot without `chat_of` that runs on one of
        the project's profiles, the first profile in PROJECTS first. A bot serves one project at most."""
        bots = sorted((bot for bot in self._all('bots') if not bot.get('chat_of') and not bot.get('_deleted')),
                      key=lambda bot: bot.get('created_at') or '')
        found: dict[str, str] = {}
        for key, _, names in PROJECTS:
            for name in names:
                match = next((bot for bot in bots if bot['id'] not in found
                              and (row := self.profiles.rows.get(bot['id'])) and row['state'] == 'active'
                              and row['name'] == name), None)
                if match:
                    found[match['id']] = key
                    break
        return found

    def _is_project(self, bot_id: str) -> bool:
        return bot_id in self._project_ids()

    def _project_home(self, names) -> tuple[str, Path] | None:
        """The first profile of a project that exists: (name, folder). Without Hermes (fixture mode), the
        last profile, the one that a fresh install has, lives in <data>/hermes."""
        for name in names:
            home = self.hermes_home if name == profile_registry.DEFAULT else self.hermes_home / 'profiles' / name
            if not self.uses_hermes and name == names[-1]:
                home.mkdir(parents=True, exist_ok=True, mode=0o700)
            if home.is_dir():
                return name, home
        return None

    def _ensure_projects(self) -> None:
        """Make the bot of each project that has a profile and no bot yet, and mark the Coconut project as the Coconut
        chat. The caller holds the transaction."""
        from .service_base import now, uid
        projects = {key: bot_id for bot_id, key in self._project_ids().items()}
        for key, title, names in PROJECTS:
            if key in projects:
                continue
            found = self._project_home(names)
            if found is None:
                LOG.warning('No Hermes profile serves the project %s.', title)
                continue
            # Older apps read `avatar` and `color`. A project has no face, so both stay empty.
            bot = dict(id=uid(), session_id=uid(), name=title, role='', avatar='', color='', instructions='',
                       notifications=True, status='idle', last_message='', unread_count=0, last_read_message_id=None,
                       archived=False, pinned=False, created_at=now(), model_override=None)
            bot['profile_id'] = bot['id']
            self._attach_profile(bot, dict(name=found[0], home=str(found[1])))
            self._put('bots', bot)
            self._event('bot.created', bot, bot['id'])
            projects[key] = bot['id']
        coconut = projects.get('coconut')
        for bot in self._all('bots'):
            marked = bot['id'] == coconut
            if bool(bot.get('coconut_chat')) != marked and not bot.get('chat_of'):
                if marked:
                    bot['coconut_chat'] = True
                else:
                    bot.pop('coconut_chat', None)
                self._put('bots', bot)

    def _refuse_project_removal(self, bot_id: str, data: dict | None = None) -> None:
        """A project stays: no archive and no delete. A chat of a project can go."""
        from .service_base import ServiceError
        if self._is_project(bot_id) and (data is None or data.get('archived') is True):
            raise ServiceError(409, PROJECTS_STAY)

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
