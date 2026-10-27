"""Bots are the Hermes profiles of the person.

The service keeps the `profiles` table (profile_registry) in step with `<Hermes home>/profiles`. It rescans at its start
and when that folder changes:
- A new profile folder becomes a new bot.
- A renamed folder (same device and inode) keeps its bot.
- A missing folder makes its bot `missing`. The history stays, and the bot reattaches when the folder returns.

Coconut creates and deletes profiles only through the Hermes profile API (hermes_cli.profiles), in a Hermes process.
"Remove from Coconut" keeps the folder. Only the person can delete the Hermes profile itself, with a typed confirmation,
and never the `default` profile. A bot cannot request that deletion.

Until scripts/migrate_bots_to_profiles.py moves the earlier bot homes (<data>/bots/<uuid>/home), the service keeps the
earlier behavior: it does not rescan, and a new bot gets a home in the data directory.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import threading

from . import profile_registry
from .profiles import ProfileError

LOG = logging.getLogger(__name__)
SCAN_SECONDS = 5
COMMAND_SECONDS = 300
DEFAULT_BOT_NAME = 'Hermes'

# Runs in the Python of the person's Hermes. argv[1] is the Hermes source folder. It follows the dashboard flow of
# hermes_cli/web_routers/profiles.py: create the profile, seed the bundled skills, and add the `<name>` command when
# that name is free.
PROFILE_COMMAND = r'''
import contextlib, inspect, json, sys
sys.path.insert(0, sys.argv[1])
request = json.loads(sys.stdin.read())
try:
    with contextlib.redirect_stdout(sys.stderr):
        from hermes_cli import profiles
        if request["operation"] == "create":
            accepted = inspect.signature(profiles.create_profile).parameters
            options = {"description": request.get("description") or None, "soul": request.get("soul")}
            home = profiles.create_profile(request["name"], **{k: v for k, v in options.items() if k in accepted})
            if "soul" not in accepted and request.get("soul") is not None:
                (home / "SOUL.md").write_text(request["soul"], encoding="utf-8")
            with contextlib.suppress(Exception):
                profiles.seed_profile_skills(home, quiet=True)
            with contextlib.suppress(Exception):
                if not profiles.check_alias_collision(request["name"]):
                    profiles.create_wrapper_script(request["name"])
            if request.get("display_name"):
                profiles.write_profile_meta(home, display_name=request["display_name"])
            result = {"home": str(home)}
        elif request["operation"] == "delete":
            try:
                result = {"home": str(profiles.delete_profile(request["name"], yes=True))}
            except Exception as error:
                if type(error).__name__ != "ProfileIdentitySettlementPending":
                    raise
                result = {"home": str(error.path), "settlement_pending": True}
        else:
            raise ValueError("Unknown profile operation.")
except Exception as error:
    result = {"error": type(error).__name__ + ": " + str(error)[:500]}
print(json.dumps(result))
'''


class BotProfiles:
    def _init_profiles(self, hermes_home) -> None:
        self.hermes_home = Path(hermes_home).expanduser().absolute() if hermes_home else None
        profile_registry.ensure_table(self.db)
        # One dict, shared with the provider and the model catalog, mirrors the table in this process.
        self.profiles.rows = profile_registry.rows(self.db)
        for target in (self.provider, getattr(self.provider, 'catalog', None)):
            if target is not None:
                try:
                    target.profile_rows = self.profiles.rows
                except AttributeError:
                    pass
        self.profile_watcher = None
        self._profile_scan_lock = threading.Lock()
        legacy = self._legacy_bots()
        # The earlier homes must move first. A deploy cannot do that safely while workers run, so the person runs the
        # migration script. Until then, Coconut keeps the earlier layout for every new bot too.
        self.profiles_ready = self.hermes_home is not None and not legacy
        if self.hermes_home is not None and legacy:
            LOG.warning('%d bots still use homes in %s. Run scripts/migrate_bots_to_profiles.py to move them into '
                        'Hermes profiles.', len(legacy), self.root / 'bots')

    def _legacy_bots(self) -> list[dict]:
        rows = self.profiles.rows
        return [bot for bot in self._all('bots') if not bot.get('_deleted') and bot['id'] not in rows
                and (self.root / 'bots' / bot['id'] / 'home').is_dir()]

    def _reload_profile_rows(self) -> None:
        """Read the table again after a rolled back write. Other threads read the dict, so it never becomes empty."""
        fresh = profile_registry.rows(self.db)
        self.profiles.rows.update(fresh)
        for bot_id in set(self.profiles.rows) - set(fresh):
            self.profiles.rows.pop(bot_id, None)

    def _write_profile_row(self, row: dict) -> None:
        profile_registry.write(self.db, row)
        self.profiles.rows[row['bot_id']] = dict(row, home=str(row['home']))

    def rescan_profiles(self) -> list[tuple[str, dict]]:
        """Match the bots with the profile folders of the person's Hermes. Return the changes."""
        if not self.profiles_ready or self.stopping.is_set():
            return []
        with self._profile_scan_lock:
            found = profile_registry.scan(self.hermes_home)
            if not found:
                # The Hermes home itself is gone, for example on an unmounted disk. Keep every bot as it is.
                return []
            try:
                changes = self._apply_profile_changes(found)
            except BaseException:
                with self.lock:
                    self._reload_profile_rows()
                raise
            if changes:
                LOG.info('The Hermes profiles changed: %s', ', '.join(f'{change} {row["name"]}' for change, row in changes))
            return changes

    def _apply_profile_changes(self, found) -> list[tuple[str, dict]]:
        with self.transaction():
            changes = profile_registry.plan(profile_registry.rows(self.db), found)
            for change, row in changes:
                if change == 'new':
                    name = profile_registry.display_name(row['home']) or (
                        DEFAULT_BOT_NAME if row['name'] == profile_registry.DEFAULT else row['name'])
                    role = profile_registry.meta_value(row['home'], 'description')[:160]
                    self._create_bot({'name': name, 'role': role}, audit={'hermes_profile': row['name']}, profile=row)
                    continue
                self._write_profile_row(row)
                bot = self._get('bots', row['bot_id'])
                if bot.get('_deleted') or row['state'] == 'removed':
                    continue
                bot.update(profile=row['name'], profile_state=row['state'])
                self._put('bots', bot)
                self._event('bot.updated', self._public(bot), bot['id'])
                from .service import encoded
                self._note_published(bot['id'], encoded(self._public(bot)))
        return changes

    def _watch_profiles(self) -> None:
        signature = None
        while not self.stopping.wait(SCAN_SECONDS):
            try:
                current = profile_registry.signature(self.hermes_home)
                if current != signature:
                    self.rescan_profiles()
                    signature = current
            except Exception:
                LOG.exception('The Hermes profile scan failed. The next scan tries again.')

    def _start_profile_watcher(self) -> None:
        if self.profiles_ready and self.profile_watcher is None:
            self.profile_watcher = threading.Thread(target=self._watch_profiles, name='coconut-profile-scan', daemon=True)
            self.profile_watcher.start()

    def _attach_profile(self, bot: dict, row: dict) -> None:
        row = dict(row, bot_id=bot['id'], state='active')
        self._write_profile_row(row)
        bot.update(profile=row['name'], profile_state='active')

    def _new_hermes_profile(self, bot: dict) -> dict:
        """Create a Hermes profile for a new bot with the Hermes profile API. Return its profiles row without bot_id."""
        taken = profile_registry.taken_names(self.hermes_home) | {row['name'] for row in self.profiles.rows.values()}
        name = profile_registry.slug(bot['name'], taken)
        result = self._hermes_profile_command('create', name=name, display_name=bot['name'], description=bot.get('role') or '',
                                              soul=bot.get('instructions', ''))
        home = Path(result['home']).absolute()
        if home != self.hermes_home / 'profiles' / name or not home.is_dir():
            raise ProfileError('Hermes created the profile in an unexpected folder.')
        state = home.stat()
        return dict(name=name, home=str(home), dev=state.st_dev, ino=state.st_ino, state='active')

    def _hermes_profile_command(self, operation: str, **payload) -> dict:
        python, source = getattr(self.provider, 'python', None), getattr(self.provider, 'hermes_root', None)
        if not python or not source or self.hermes_home is None:
            raise ProfileError('Hermes is not available on this Mac.')
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR')
                       if key in os.environ}
        from .hermes_runtime import NO_LAZY_INSTALLS
        environment.update(NO_LAZY_INSTALLS, HERMES_HOME=str(self.hermes_home), HERMES_DISABLE_TELEMETRY='1')
        try:
            completed = subprocess.run([str(python), '-I', '-c', PROFILE_COMMAND, str(source)],
                                       input=json.dumps(dict(payload, operation=operation)), env=environment,
                                       capture_output=True, text=True, timeout=COMMAND_SECONDS)
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as error:
            raise ProfileError('Hermes did not answer the profile request.') from error
        if not isinstance(result, dict) or 'error' in result or not isinstance(result.get('home'), str):
            raise ProfileError('Hermes refused the profile request: ' + str(result.get('error') if isinstance(result, dict) else result))
        return result

    def _remove_profile_row(self, bot_id: str) -> None:
        """Remove from Coconut: the row stays as `removed`, so a rescan does not add the folder again."""
        row = self.profiles.rows.get(bot_id)
        if row is not None and row['state'] != 'removed':
            self._write_profile_row(dict(row, state='removed', seen_at=profile_registry.now()))

    def _delete_profile_request(self, bot: dict, data, callbacks: list, device: str) -> dict:
        """Delete the Hermes profile of a bot. The person types the profile name to confirm."""
        from .service import ServiceError
        row = self.profiles.rows.get(bot['id'])
        if row is None:
            raise ServiceError(409, 'This bot has no Hermes profile. Remove it from Coconut instead.')
        if row['name'] == profile_registry.DEFAULT:
            raise ServiceError(409, 'Coconut never deletes the default Hermes profile. Remove the bot from Coconut instead.')
        if not isinstance(data, dict) or set(data) != {'confirm'} or data['confirm'] != row['name']:
            raise ServiceError(400, f'Type the profile name, {row["name"]}, in confirm to delete this Hermes profile.')
        self._delete_bot(bot, callbacks, audit={'deleted_by': device, 'hermes_profile_deleted': row['name']})
        callbacks.append(lambda: self._finish_profile_deletion(bot['id']))
        return {'ok': True, 'profile': row['name']}

    def _finish_profile_deletion(self, bot_id: str) -> None:
        from .service import ServiceError
        with self.lock:
            row = self.profiles.rows.get(bot_id)
        if row is None or row['name'] == profile_registry.DEFAULT:
            return
        if not Path(row['home']).exists():
            return
        try:
            self._hermes_profile_command('delete', name=row['name'])
        except ProfileError as error:
            raise ServiceError(503, 'The bot was removed, but Hermes did not delete its profile. Retry the exact request.') from error
