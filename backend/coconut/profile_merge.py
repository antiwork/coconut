"""Merge the named Hermes profiles of earlier releases into the default profile, without losing an entry.

Earlier releases gave each agent its own profile in `<Hermes home>/profiles/<name>`. Now every chat runs on the default
profile (bot_profiles.py). This script moves what the agents of a Coconut data folder kept in their own profiles:

- It first copies each named profile folder, the files of the default profile that it changes, and coconut.sqlite3
  into the backup folder. A SQLite store is copied with the SQLite backup API.
- Cron jobs: each job goes into the default cron list with its ID, its origin chat and its state. The job in the old
  profile is paused, so it never runs twice. A script that a job names is copied into `scripts/` when default lacks it.
- Memory: each entry of MEMORY.md and USER.md that default lacks is added with the tag `(from <agent>)`. Only an
  identical entry is dropped. The memory limits of default grow when the merged files need it.
- SOUL.md: the instructions of each agent go into the default SOUL.md as a section that applies only in that chat.
- Skills: a skill that default lacks is copied. A skill with the same name and other content stays where it is and is
  listed in the report.
- Coconut: each chat of the agent maps to the default profile. Its history in Coconut stays as it is.

It never copies `.env`, `auth.json`, OAuth tokens or keys. It lists the credentials that live only in a named profile,
so the person can connect them again in default. It never deletes an old profile folder.

Run it while the Coconut service is stopped: the script takes the service lock of the data folder and refuses
otherwise. Without --apply it only prints the plan. Standard library only.

    python3 -m coconut.profile_merge --hermes-home ~/.hermes --data-dir ~/Coconut/.runtime/host --backup-dir <folder> [--apply]
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import filecmp
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ENTRY_DELIMITER = '\n§\n'
MEMORY_FILES = {'MEMORY.md': ('memory.memory_char_limit', 2200), 'USER.md': ('memory.user_char_limit', 1375)}
# The files of a profile that can hold a credential. The script reads only their key names.
CREDENTIAL_FILES = ('.env', 'auth.json', '.anthropic_oauth.json')
CREDENTIAL_FOLDERS = ('mcp-tokens',)
SOUL_MARK = '<!-- coconut-merge: {name} -->'
PAUSED_REASON = 'Moved to the default Hermes profile by Coconut. The job runs there now.'
# What the copy of a profile skips: sockets, locks, live SQLite files (the backup API copies the stores), and caches
# that Hermes fills again. A cache can hold gigabytes, and the old folder stays on disk in any case.
SKIP = shutil.ignore_patterns('*.sock', '*.lock', 'gateway.pid', '*.db', '*.db-*', 'cache', 'lsp', 'audio_cache',
                              'image_cache', 'models_dev_cache.json')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def service_stopped(data_dir: Path):
    """Hold the service lock of the data folder, or refuse while the Coconut service runs."""
    path = data_dir / 'service.lock'
    with open(path, 'a') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('The Coconut service runs on this data folder. Stop it first, then run the merge again.')
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def jobs_lock(cron: Path):
    """The cross-process lock of Hermes cron (cron/jobs.py, `.jobs.lock`)."""
    cron.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(cron / '.jobs.lock', 'a') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='.coconut-merge-', delete=False) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    if path.exists():
        os.chmod(handle.name, path.stat().st_mode & 0o777)
    os.replace(handle.name, path)


def sqlite_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as db, \
            closing(sqlite3.connect(target)) as copy:
        db.backup(copy)


def read_jobs(home: Path) -> dict:
    try:
        data = json.loads((home / 'cron' / 'jobs.json').read_text())
    except FileNotFoundError:
        return {'jobs': []}
    if isinstance(data, list):
        data = {'jobs': data}
    data.setdefault('jobs', [])
    return data


def entries(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return []
    return [entry.strip() for entry in text.split(ENTRY_DELIMITER) if entry.strip()]


def skill_names(skills: Path) -> dict[str, Path]:
    """Each skill of a skills folder by its folder name: {name: folder}."""
    found = {}
    if skills.is_dir():
        for marker in sorted(skills.rglob('SKILL.md')):
            if not any(part.startswith('.') for part in marker.relative_to(skills).parts):
                found.setdefault(marker.parent.name, marker.parent)
    return found


def credentials(home: Path) -> dict[str, list[str]]:
    """The names of the credentials of a profile, never their values."""
    found = {}
    env = home / '.env'
    if env.is_file():
        names = [line.split('=', 1)[0].strip().removeprefix('export ').strip()
                 for line in env.read_text(errors='replace').splitlines()
                 if '=' in line and not line.lstrip().startswith('#') and line.split('=', 1)[1].strip()]
        if names:
            found['.env'] = sorted(names)
    for name in ('auth.json',):
        try:
            auth = json.loads((home / name).read_text())
        except (OSError, ValueError):
            continue
        names = []
        providers = auth.get('providers')
        names += [f'provider:{key}' for key in (providers if isinstance(providers, (list, dict)) else [])]
        pool = auth.get('credential_pool')
        names += [f'pool:{key}' for key in (pool if isinstance(pool, (list, dict)) else [])]
        if names:
            found[name] = sorted(names)
    if (home / '.anthropic_oauth.json').is_file():
        found['.anthropic_oauth.json'] = ['anthropic oauth']
    for folder in CREDENTIAL_FOLDERS:
        if (home / folder).is_dir():
            files = sorted(entry.name for entry in (home / folder).iterdir() if entry.is_file())
            if files:
                found[folder] = files
    return found


def agents(data_dir: Path, hermes_home: Path) -> dict[str, dict]:
    """The named profiles that a project of Coconut runs on: {profile: {name, bot_ids, home}}."""
    database = data_dir / 'coconut.sqlite3'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as db:
        rows = db.execute('SELECT p.bot_id, p.name, p.home, p.state, b.data FROM profiles p '
                          'LEFT JOIN bots b ON b.id = p.bot_id ORDER BY p.rowid').fetchall()
    result = {}
    for bot_id, name, _home, state, data in rows:
        bot = json.loads(data) if data else {}
        if name == 'default' or state == 'removed' or bot.get('_deleted'):
            continue
        # The folder comes from this Hermes home and the profile name, never from the stored path, so a run on a copy
        # changes only the copy.
        entry = result.setdefault(name, dict(profile=name, home=hermes_home / 'profiles' / name, name='', bot_ids=[]))
        entry['bot_ids'].append(bot_id)
        if not bot.get('chat_of') and not entry['name']:
            entry['name'] = bot.get('name') or name
    for entry in result.values():
        entry['name'] = entry['name'] or entry['profile']
    return result


class Merge:
    def __init__(self, hermes_home: Path, data_dir: Path, backup: Path, apply: bool, hermes: str):
        self.home = hermes_home
        self.data_dir = data_dir
        self.backup = backup
        self.apply = apply
        self.hermes = hermes
        self.report = dict(started_at=now(), hermes_home=str(hermes_home), data_dir=str(data_dir), backup=str(backup),
                           applied=apply, profiles=[], skipped_profiles=[], cron=[], memory={}, soul=[], skills=[],
                           skill_conflicts=[], scripts=[], chats=0, credentials=[], limits={})

    # 1. Back up everything that the merge reads or changes.
    def back_up(self, profiles: dict) -> None:
        if not self.apply:
            return
        self.backup.mkdir(parents=True, exist_ok=False, mode=0o700)
        for name in sorted(p.name for p in (self.home / 'profiles').iterdir() if p.is_dir() and not p.name.startswith('.')):
            source = self.home / 'profiles' / name
            target = self.backup / 'profiles' / name
            shutil.copytree(source, target, symlinks=True, ignore=SKIP)
            for store in source.glob('*.db'):
                sqlite_copy(store, target / store.name)
        default = self.backup / 'default'
        default.mkdir(parents=True, mode=0o700)
        for name in ('config.yaml', 'SOUL.md', 'memories/MEMORY.md', 'memories/USER.md', 'cron/jobs.json'):
            if (self.home / name).is_file():
                (default / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.home / name, default / name)
        if (self.home / 'skills').is_dir():
            shutil.copytree(self.home / 'skills', default / 'skills', symlinks=True)
        if (self.home / 'scripts').is_dir():
            shutil.copytree(self.home / 'scripts', default / 'scripts', symlinks=True)
        sqlite_copy(self.data_dir / 'coconut.sqlite3', self.backup / 'coconut.sqlite3')

    # 2. Cron jobs.
    def cron(self, profiles: dict) -> None:
        target = self.home / 'cron'
        with jobs_lock(target):
            merged = read_jobs(self.home)
            known = {job.get('id') for job in merged['jobs']}
            changed = False
            for entry in profiles.values():
                source = entry['home'] / 'cron'
                if not (source / 'jobs.json').is_file():
                    continue
                with jobs_lock(source):
                    data = read_jobs(entry['home'])
                    source_changed = False
                    for job in data['jobs']:
                        row = dict(agent=entry['name'], profile=entry['profile'], job=job.get('id'),
                                   name=job.get('name'), enabled=job.get('enabled', True))
                        if job.get('id') in known:
                            row['result'] = 'already in default'
                        else:
                            moved = json.loads(json.dumps(job))
                            moved.pop('fire_claim', None)
                            merged['jobs'].append(moved)
                            known.add(job.get('id'))
                            changed = True
                            row['result'] = 'copied'
                            if job.get('script'):
                                self.script(entry, job['script'])
                        if job.get('enabled', True) and job.get('paused_reason') != PAUSED_REASON:
                            job.update(enabled=False, state='paused', paused_at=now(), paused_reason=PAUSED_REASON)
                            source_changed = True
                            row['old_job'] = 'paused'
                        self.report['cron'].append(row)
                    if source_changed and self.apply:
                        data['updated_at'] = now()
                        write_atomic(source / 'jobs.json', json.dumps(data, indent=2) + '\n')
            if changed and self.apply:
                merged['updated_at'] = now()
                write_atomic(target / 'jobs.json', json.dumps(merged, indent=2) + '\n')

    def script(self, entry: dict, name: str) -> None:
        source = entry['home'] / 'scripts' / name
        target = self.home / 'scripts' / name
        row = dict(agent=entry['name'], script=name)
        if not source.is_file():
            row['result'] = 'missing in the old profile'
        elif target.exists():
            row['result'] = 'same' if filecmp.cmp(source, target, shallow=False) else 'conflict: default keeps its own'
        else:
            row['result'] = 'copied'
            if self.apply:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(source, target)
        self.report['scripts'].append(row)

    # 3. Memory.
    def memory(self, profiles: dict) -> None:
        for file, (setting, limit) in MEMORY_FILES.items():
            path = self.home / 'memories' / file
            merged = entries(path)
            before = len(merged)
            added = []
            for entry in profiles.values():
                for item in entries(entry['home'] / 'memories' / file):
                    tagged = f'(from {entry["name"]}) {item}'
                    if item in merged or tagged in merged:
                        continue
                    merged.append(tagged)
                    added.append(entry['name'])
            text = ENTRY_DELIMITER.join(merged)
            self.report['memory'][file] = dict(before=before, added=len(added), after=len(merged), chars=len(text),
                                               from_agents=sorted(set(added)))
            if added and self.apply:
                write_atomic(path, text)
            current = self.limit(setting, limit)
            if len(text) > current:
                # Room for every entry, rounded up, so the next save of a memory fits too.
                wanted = (len(text) // 500 + 2) * 500
                self.report['limits'][setting] = dict(before=current, after=wanted)
                if self.apply:
                    subprocess.run([self.hermes, 'config', 'set', setting, str(wanted)], check=True, capture_output=True,
                                   env=dict(os.environ, HERMES_HOME=str(self.home)), timeout=120)

    def limit(self, setting: str, fallback: int) -> int:
        key = setting.split('.')[1]
        try:
            text = (self.home / 'config.yaml').read_text()
        except OSError:
            return fallback
        section = re.search(r'^memory:\n((?:[ \t]+.*\n?)*)', text, re.MULTILINE)
        found = re.search(rf'^\s+{key}:\s*(\d+)', section[1], re.MULTILINE) if section else None
        return int(found[1]) if found else fallback

    # 4. SOUL.md.
    def soul(self, profiles: dict) -> None:
        path = self.home / 'SOUL.md'
        current = path.read_text(encoding='utf-8') if path.exists() else ''
        text = current
        for entry in profiles.values():
            try:
                own = (entry['home'] / 'SOUL.md').read_text(encoding='utf-8').strip()
            except FileNotFoundError:
                own = ''
            mark = SOUL_MARK.format(name=entry['profile'])
            if not own or own == current.strip() or mark in text:
                continue
            # The headings of the agent go one level down, under the heading of its chat.
            body = re.sub(r'^(#{1,5}) ', r'#\1 ', own, flags=re.MULTILINE)
            text = (text.rstrip() + f'\n\n{mark}\n## Only in the "{entry["name"]}" chat\n\n'
                    f'These instructions apply only in the chat named "{entry["name"]}" and its further chats.\n\n'
                    f'{body}\n')
            self.report['soul'].append(dict(agent=entry['name'], chars=len(own)))
        if text != current and self.apply:
            write_atomic(path, text)

    # 5. Skills.
    def skills(self, profiles: dict) -> None:
        """Copy each skill that default lacks. When several agents have a skill of that name, the newest one comes."""
        target = self.home / 'skills'
        present = skill_names(target)
        candidates = {}
        for entry in profiles.values():
            source = entry['home'] / 'skills'
            for name, folder in skill_names(source).items():
                candidates.setdefault(name, []).append((entry, source, folder))
        for name, found in sorted(candidates.items()):
            found.sort(key=lambda item: (item[2] / 'SKILL.md').stat().st_mtime, reverse=True)
            if name not in present:
                entry, source, folder = found[0]
                relative = folder.relative_to(source)
                self.report['skills'].append(dict(agent=entry['name'], skill=name, path=str(relative)))
                present[name] = folder
                if self.apply:
                    shutil.copytree(folder, target / relative, symlinks=True)
            for entry, _source, folder in found:
                if folder != present[name] and not filecmp.cmp(folder / 'SKILL.md', present[name] / 'SKILL.md', shallow=False):
                    # The other version stays in the old profile folder.
                    self.report['skill_conflicts'].append(dict(agent=entry['name'], skill=name, kept=str(folder)))

    # 6. Coconut: every chat of the merged profiles runs on default.
    def chats(self, profiles: dict) -> None:
        ids = [bot_id for entry in profiles.values() for bot_id in entry['bot_ids']]
        self.report['chats'] = len(ids)
        if not self.apply or not ids:
            return
        with closing(sqlite3.connect(self.data_dir / 'coconut.sqlite3', timeout=30)) as db:
            with db:
                for bot_id in ids:
                    db.execute("UPDATE profiles SET name='default', home=?, state='active', seen_at=? WHERE bot_id=?",
                               (str(self.home), now(), bot_id))
                    db.execute("UPDATE bots SET data=json_set(data, '$.profile', 'default', '$.profile_state', 'active')"
                               ' WHERE id=?', (bot_id,))

    # 7. Credentials that live only in a named profile.
    def credentials(self, profiles: dict) -> None:
        default = credentials(self.home)
        have = {(file, key) for file, keys in default.items() for key in keys}
        for entry in profiles.values():
            for file, keys in credentials(entry['home']).items():
                missing = [key for key in keys if (file, key) not in have and
                           not (file == 'auth.json' and key.startswith('provider:') and ('auth.json', 'pool:' + key[9:]) in have)]
                if missing:
                    self.report['credentials'].append(dict(agent=entry['name'], profile=entry['profile'], file=file,
                                                           names=missing))

    def run(self) -> dict:
        profiles = agents(self.data_dir, self.home)
        on_disk = {p.name for p in (self.home / 'profiles').iterdir() if p.is_dir()} if (self.home / 'profiles').is_dir() else set()
        self.report['profiles'] = [dict(profile=entry['profile'], agent=entry['name'], chats=len(entry['bot_ids']))
                                   for entry in profiles.values()]
        self.report['skipped_profiles'] = sorted(on_disk - set(profiles))
        self.back_up(profiles)
        self.cron(profiles)
        self.memory(profiles)
        self.soul(profiles)
        self.skills(profiles)
        self.credentials(profiles)
        self.chats(profiles)
        self.report['finished_at'] = now()
        if self.apply:
            write_atomic(self.backup / 'merge-report.json', json.dumps(self.report, indent=2) + '\n')
        return self.report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Merge the named Hermes profiles of Coconut agents into default.')
    parser.add_argument('--hermes-home', type=Path, default=Path(os.environ.get('HERMES_HOME') or '~/.hermes'))
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--backup-dir', type=Path, required=True, help='A new folder for the backup and the report.')
    parser.add_argument('--hermes', default='hermes', help='The hermes command, for `hermes config set`.')
    parser.add_argument('--apply', action='store_true', help='Change the files. Without it, print the plan only.')
    args = parser.parse_args(argv)
    hermes_home, data_dir = args.hermes_home.expanduser().absolute(), args.data_dir.expanduser().absolute()
    backup = args.backup_dir.expanduser().absolute()
    if args.apply and backup.exists():
        raise SystemExit(f'{backup} exists. Choose a new backup folder.')
    with service_stopped(data_dir):
        report = Merge(hermes_home, data_dir, backup, args.apply, args.hermes).run()
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
