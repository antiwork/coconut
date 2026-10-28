"""Bots are the Hermes profiles of the person, keyed by the profile name.

The service keeps the `profiles` table (profile_registry) in step with the Hermes home. It scans at its start and when
`<Hermes home>/profiles` changes:
- A new profile folder becomes a new bot.
- A folder that disappears makes its bot `missing`. The history stays, and the bot returns with a folder of that name.
- Coconut does not follow renames: a renamed folder is a missing bot and a new bot.

Coconut creates a profile only through the Hermes profile API, once per request. When that fails, the request fails; a
folder that Hermes left behind shows up as a bot at the next scan. "Remove from Coconut" keeps the folder. Only the
person deletes the Hermes profile itself, by typing its name, and never `default`. A bot cannot request that.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import threading

from . import profile_registry
from .profiles import NO_PROFILE, SKILLS, ProfileError, _write

LOG = logging.getLogger(__name__)
SCAN_SECONDS = 5
COMMAND_SECONDS = 300
DEFAULT_BOT_NAME = 'Hermes'

# Runs in the Python of the person's Hermes. argv[1] is the Hermes source folder. It follows the dashboard flow of
# hermes_cli/web_routers/profiles.py: create the profile, seed the bundled skills, and add the `<name>` command when
# that name is free. Then it adds Coconut's skills folder with skill_dirs and the `coconut` gateway platform with
# gateway_plugin, the same edits as setup.
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
            if request.get("skills"):
                try:
                    sys.path.append(request["coconut"])
                    from skill_dirs import update
                    result["skills"] = update(home, request["skills"], True, create=True)
                    import gateway_plugin
                    result["gateway_platform"] = gateway_plugin.update(home, True)
                except Exception as error:
                    result["skills_error"] = type(error).__name__ + ": " + str(error)[:300]
        elif request["operation"] == "delete":
            try:
                result = {"home": str(profiles.delete_profile(request["name"], yes=True))}
            except Exception as error:
                if type(error).__name__ != "ProfileIdentitySettlementPending":
                    raise
                result = {"home": str(error.path)}
        else:
            raise ValueError("Unknown profile operation.")
except Exception as error:
    result = {"error": type(error).__name__ + ": " + str(error)[:500]}
print(json.dumps(result))
'''


def run_profile_command(python, source, hermes_home, operation: str, *, environment: dict | None = None, **payload) -> dict:
    """Run PROFILE_COMMAND in the Python of the person's Hermes. The Hermes contract check runs it too."""
    if environment is None:
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR')
                       if key in os.environ}
    from .hermes_runtime import NO_LAZY_INSTALLS
    environment = dict(environment, **NO_LAZY_INSTALLS, HERMES_HOME=str(hermes_home), HERMES_DISABLE_TELEMETRY='1')
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


class BotProfiles:
    def _init_profiles(self, hermes_home) -> None:
        # Without a Hermes home (fixture mode and tests), profiles live in <data>/hermes, and the scan is off.
        self.scan_profiles = hermes_home is not None
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
        if self.scan_profiles and hasattr(catalog, 'use_hermes_home'):
            # Settings are native: the default settings are those of the default profile, `<Hermes home>/config.yaml`.
            catalog.use_hermes_home(self.hermes_home)
        self.profile_watcher = None

    def _reload_profile_rows(self) -> None:
        """Follow the table after a rolled back transaction. Other threads read the dict, so it never becomes empty."""
        fresh = profile_registry.rows(self.db)
        self.profiles.rows.update(fresh)
        for bot_id in set(self.profiles.rows) - set(fresh):
            self.profiles.rows.pop(bot_id, None)

    def _write_profile_row(self, row: dict) -> None:
        profile_registry.write(self.db, row)
        self.profiles.rows[row['bot_id']] = dict(row, home=str(row['home']))

    def rescan_profiles(self) -> list[tuple[str, str]]:
        """Match the bots with the profile folders by name. Return the changes as (change, name)."""
        if not self.scan_profiles or self.stopping.is_set():
            return []
        found = profile_registry.scan(self.hermes_home)
        if not found:
            # The Hermes home itself is gone, for example on an unmounted disk. Keep every bot as it is.
            return []
        changes = []
        with self.transaction():
            by_name = {row['name']: row for row in profile_registry.rows(self.db).values()}
            for name, home in found.items():
                row = by_name.get(name)
                if row is None:
                    title = profile_registry.meta_value(home, 'display_name')[:80] or (
                        DEFAULT_BOT_NAME if name == profile_registry.DEFAULT else name)
                    role = profile_registry.meta_value(home, 'description')[:160]
                    self._create_bot({'name': title, 'role': role}, audit={'hermes_profile': name},
                                     profile=dict(name=name, home=str(home)))
                    changes.append(('new', name))
                elif row['state'] == 'missing':
                    self._set_profile_state(dict(row, home=str(home)), 'active')
                    changes.append(('returned', name))
                elif row['state'] == 'active' and row['home'] != str(home):
                    # The service started with another Hermes home (HERMES_HOME). The name now maps to this folder.
                    self._write_profile_row(dict(row, home=str(home), seen_at=profile_registry.now()))
                    changes.append(('moved', name))
            for row in by_name.values():
                if row['state'] == 'active' and row['name'] not in found:
                    self._set_profile_state(row, 'missing')
                    changes.append(('missing', row['name']))
        if changes:
            LOG.info('The Hermes profiles changed: %s', ', '.join(f'{change} {name}' for change, name in changes))
        return changes

    def _set_profile_state(self, row: dict, state: str) -> None:
        self._write_profile_row(dict(row, state=state, seen_at=profile_registry.now()))
        bot = self._get('bots', row['bot_id'])
        if not bot.get('_deleted'):
            from .service import encoded
            bot['profile_state'] = state
            self._put('bots', bot)
            self._event('bot.updated', self._public(bot), bot['id'])
            self._note_published(bot['id'], encoded(self._public(bot)))

    def _watch_profiles(self) -> None:
        signature = None
        while not self.stopping.wait(SCAN_SECONDS):
            try:
                current = sorted(profile_registry.scan(self.hermes_home))
                if current != signature:
                    self.rescan_profiles()
                    signature = current
            except Exception:
                LOG.exception('The Hermes profile scan failed. The next scan tries again.')

    def _start_profile_watcher(self) -> None:
        if self.scan_profiles and self.profile_watcher is None:
            self.profile_watcher = threading.Thread(target=self._watch_profiles, name='coconut-profile-scan', daemon=True)
            self.profile_watcher.start()

    def _start_hermes_entries(self) -> None:
        """Set Coconut's own entries in the Hermes profiles again, in the background, at each start.

        The entries are the skills folder in `skills.external_dirs` and the `coconut` gateway platform (skill_dirs.py,
        gateway_plugin.py). Only these keys change. A failure is a warning: the service starts all the same.
        """
        python, source = getattr(self.provider, 'python', None), getattr(self.provider, 'hermes_root', None)
        if self.scan_profiles and python and source:
            self.hermes_entries = threading.Thread(target=self._set_hermes_entries, args=(python, source),
                                                   name='coconut-hermes-entries', daemon=True)
            self.hermes_entries.start()

    def _set_hermes_entries(self, python, source) -> bool:
        from .hermes_runtime import NO_LAZY_INSTALLS
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR')
                       if key in os.environ}
        environment.update(NO_LAZY_INSTALLS, HERMES_HOME=str(self.hermes_home), HERMES_DISABLE_TELEMETRY='1')
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

    def _attach_profile(self, bot: dict, profile: dict) -> None:
        self._write_profile_row(dict(bot_id=bot['id'], name=profile['name'], home=profile['home'], state='active'))
        bot.update(profile=profile['name'], profile_state='active')

    def _new_profile(self, bot: dict) -> dict:
        """Create the Hermes profile of a new bot, once. Return {name, home}."""
        taken = profile_registry.taken_names(self.hermes_home) | {row['name'] for row in self.profiles.rows.values()}
        name = profile_registry.slug(bot['name'], taken)
        python, source = getattr(self.provider, 'python', None), getattr(self.provider, 'hermes_root', None)
        if not self.scan_profiles or not python or not source:
            return self._fixture_profile(name, bot)
        result = run_profile_command(python, source, self.hermes_home, 'create', name=name, display_name=bot['name'],
                                     description=bot.get('role') or '', soul=bot.get('instructions', ''),
                                     skills=str(SKILLS), coconut=str(Path(__file__).resolve().parent))
        if result.get('skills_error'):
            LOG.warning('The profile %s did not get the Coconut skills folder or gateway platform: %s. Setup adds them.', name, result['skills_error'])
        home = Path(result['home']).absolute()
        if home != self.hermes_home / 'profiles' / name or not home.is_dir():
            raise ProfileError('Hermes created the profile in an unexpected folder.')
        return dict(name=name, home=str(home))

    def _fixture_profile(self, name: str, bot: dict) -> dict:
        """Fixture mode has no Hermes. It writes the smallest profile, inside the data directory."""
        home = self.hermes_home / 'profiles' / name
        home.mkdir(parents=True, mode=0o700)
        _write(home / 'SOUL.md', bot.get('instructions', ''), self.root)
        return dict(name=name, home=str(home))

    def _remove_profile_row(self, bot_id: str) -> None:
        """Remove from Coconut: the row stays `removed`, so a scan does not add the folder again."""
        row = self.profiles.rows.get(bot_id)
        if row is not None and row['state'] != 'removed':
            self._write_profile_row(dict(row, state='removed', seen_at=profile_registry.now()))

    def _delete_profile_request(self, bot: dict, data, callbacks: list, device: str) -> dict:
        """Delete the Hermes profile of a bot. The person types the profile name to confirm."""
        from .service import ServiceError
        row = self.profiles.rows.get(bot['id'])
        if row is None:
            raise ServiceError(409, NO_PROFILE)
        home = Path(row['home'])
        if row['name'] == profile_registry.DEFAULT or home.parent != self.hermes_home / 'profiles':
            raise ServiceError(409, 'Coconut deletes only a named profile in <Hermes home>/profiles, never default.')
        if not isinstance(data, dict) or set(data) != {'confirm'} or data['confirm'] != row['name']:
            raise ServiceError(400, f'Type the profile name, {row["name"]}, in confirm to delete this Hermes profile.')
        self._delete_bot(bot, callbacks, audit={'deleted_by': device, 'hermes_profile_deleted': row['name']})
        callbacks.append(lambda: self._finish_profile_deletion(bot['id'], row['name']))
        return {'ok': True, 'profile': row['name']}

    def _finish_profile_deletion(self, bot_id: str, name: str) -> None:
        from .service import ServiceError
        python, source = getattr(self.provider, 'python', None), getattr(self.provider, 'hermes_root', None)
        if (self.hermes_home / 'profiles' / name).exists():
            try:
                if not self.scan_profiles or not python or not source:
                    raise ProfileError('Hermes is not available on this Mac.')
                run_profile_command(python, source, self.hermes_home, 'delete', name=name)
            except ProfileError as error:
                raise ServiceError(503, 'The bot was removed, but Hermes did not delete its profile. Retry, or run '
                                   f'hermes profile delete {name}.') from error
        # The name is free again: a new profile with it becomes a new bot.
        with self.transaction():
            self.db.execute('DELETE FROM profiles WHERE bot_id=? AND name=?', (bot_id, name))
            self.profiles.rows.pop(bot_id, None)
