"""Coconut for a team only: a service for a Mac that runs a Hermes agent with no person at it (docs/team-log.md, "Mac").

`python3 -m coconut --team-only` (or COCONUT_MODE=team) runs this service object in server.py in place of Service. It
does three things:

1. It keeps the team rooms of this Mac in step with the relay: the store, the sync thread, the socket watcher and the
   helper of team.py, as on a full Mac.
2. It serves the team routes on the main listener with the token of <data>/gateway/bridge.json, so `coconut team` and
   scripts/team.py work against it.
3. It keeps the bots that this Mac declares for the team, and it saves their pages for the team.

It imports no Hermes module and none of the full runtime (service.py and what it loads). It reads and changes no file
under a Hermes home, starts no gateway, loads no plugin, copies no key, runs no scheduler, worker or kanban adapter,
opens no tunnel listener, pairs no phone and sends no push notification. Its only child process is the team helper.

A bot of this Mac is a record that the person declares (`POST /v1/team/bots`): a name, an avatar and a color with the
rules of a `member` entry, and the Hermes home that the bot stands for. The service stores the path of the home and
compares it. It never opens it. A page (`POST /v1/gateway/document`) names the home, so the home names the bot. No
person reads the Library on this Mac, so each page names the audience "team". A page of a bot that no team has yet
waits on this Mac, and the sync writes it when the bot is shared.

Routes, each with the token of the bridge file:

- `GET /v1/team/status`: the mode, the version of the code, whether the helper is there, and the rooms in short form.
- `GET /v1/team/bots`, `POST /v1/team/bots` with `{name, avatar, color, home}`,
  `PATCH /v1/team/bots/<bot>` with `{name, avatar, color}`, `DELETE /v1/team/bots/<bot>`.
- The routes of team.py, and `POST /v1/gateway/document`.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import threading

from .documents import SCHEMA as DOCUMENTS_SCHEMA, Documents
from .gateway_bridge import GatewayBridge
from .service_base import (MODE_MARK, TEAM_MODE, ServiceError, check_data_folder, encoded, now, open_store,
                           release_data_folder, take_data_folder, uid)
from .service_restart import ServiceRestart
from .team import SCHEMA as TEAM_SCHEMA, Team, member_profile
from .team_store import unicode_text

MODE = TEAM_MODE
REFUSAL = 'This Mac runs Coconut for a team only, so it does not serve this request.'
MAX_HOME = 4096
HOME = 'Give the Hermes home of the bot as a full path that starts with /.'
TEAM_PAGE = ('This Mac runs Coconut for a team only, so no person reads a page here. Save the page with audience '
             '"team".')
UNKNOWN_HOME = ('This Hermes home is not the home of a bot of this Mac. Declare the bot first: coconut team new-bot '
                '<name> --home <path>.')
NO_BOT = {'name': None, 'avatar': '', 'color': ''}
COMMIT = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
PROJECT = Path(__file__).resolve().parents[2]


def checked_home(value) -> str:
    """The real path of a Hermes home that a caller names. Only the text is checked: the service never opens it."""
    if not unicode_text(value) or not 0 < len(value) <= MAX_HOME or '\0' in value or not os.path.isabs(value):
        raise ServiceError(400, HOME)
    return os.path.realpath(value)


def default_home() -> str:
    """The Hermes home of this process: HERMES_HOME, else ~/.hermes, as Hermes finds it."""
    return checked_home(os.environ.get('HERMES_HOME') or os.path.expanduser('~/.hermes'))


def code_version(project: Path) -> str | None:
    """The commit of the Coconut checkout, read from its git files, or None for a folder that is not a checkout.

    No git process runs, because the only child process of this service is the team helper.
    """
    try:
        git = project / '.git'
        if git.is_file():
            # A worktree: the file names the git folder of the worktree, and its `commondir` names the shared one.
            text = git.read_text().strip()
            if not text.startswith('gitdir: '):
                return None
            git = (project / text.removeprefix('gitdir: ')).resolve()
        head = (git / 'HEAD').read_text().strip()
        common = (git / (git / 'commondir').read_text().strip()).resolve() if (git / 'commondir').is_file() else git
        if head.startswith('ref: '):
            ref = head.removeprefix('ref: ')
            loose = next((folder / ref for folder in (git, common) if (folder / ref).is_file()), None)
            if loose is not None:
                head = loose.read_text().strip()
            else:
                packed = (common / 'packed-refs').read_text().splitlines()
                head = next((line.split(' ', 1)[0] for line in packed if line.endswith(' ' + ref)), '')
        return head if COMMIT.fullmatch(head) else None
    except (OSError, UnicodeDecodeError):
        return None


class TeamOnlyService(Documents, Team):
    """The service object of the team-only mode. server.Handler serves it with Handler._team_only."""

    team_only = True
    # Nothing runs but the sync, so an update can stop this process at once. See host.drain_for_update.
    drains = False
    restart_enabled = False
    # No person reads the Library here (Documents._page_audience).
    library_reader = False

    def __init__(self, data_dir):
        self.root, self.process_lock = take_data_folder(data_dir)
        check_data_folder(self.root, self.process_lock, TEAM_MODE)
        try:
            self.db = open_store(self.root, timeout=10)
        except BaseException:
            release_data_folder(self.process_lock)
            raise
        try:
            # The mark makes this folder the folder of a team-only service (service_base.check_data_folder).
            self.db.executescript(MODE_MARK)
            # The same tables as in Service, so the team modules and Documents read them as on a full Mac.
            self.db.execute('CREATE TABLE IF NOT EXISTS bots (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            self.db.executescript(DOCUMENTS_SCHEMA)
            self.db.executescript(TEAM_SCHEMA)
        except BaseException:
            self.db.close()
            release_data_folder(self.process_lock)
            raise
        # The code that runs: a `git pull` while it runs changes the checkout, not this process.
        self.version = code_version(PROJECT)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        # Only its token and its file are of use here: Handler._team_only serves no route of the gateway adapter.
        self.gateway_bridge = GatewayBridge(self.root)
        self._init_team()

    # What the team modules and Documents need of a service

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def _all(self, table: str) -> list[dict]:
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(f'SELECT data FROM {table} ORDER BY rowid')]

    def _get(self, table: str, object_id: str) -> dict:
        with self.lock:
            row = self.db.execute(f'SELECT data FROM {table} WHERE id=?', (object_id,)).fetchone()
        if not row:
            raise ServiceError(404, f'{table[:-1].capitalize()} was not found.')
        return json.loads(row[0])

    def _put(self, table: str, obj: dict):
        self.db.execute(f'INSERT INTO {table}(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                        (obj['id'], encoded(obj)))

    def _event(self, event_type: str, data: dict, bot_id: str | None = None, task_id: str | None = None):
        """No event. On a full Mac the events table feeds the phone (GET /v1/updates). This Mac has no phone, so
        nothing reads an event, and a table that only grows would fill the disk of a Mac that runs for months."""

    # The process

    def refuse(self):
        raise ServiceError(503, REFUSAL, code='team_only')

    def take_service_restart(self) -> bool:
        return False

    clear_service_drain = ServiceRestart.clear_service_drain

    def start(self):
        self._start_team()

    def close(self):
        self.stopping.set()
        self._close_team()
        with self.lock:
            self.db.close()
            release_data_folder(self.process_lock)

    # Routes

    def _team_route(self, method: str, path: str, data: dict, repeat: str = '') -> dict:
        parts = path.strip('/').split('/')[2:]
        if parts == ['status'] and method == 'GET':
            return self.team_status()
        if parts == ['bots'] and method == 'GET':
            return {'bots': self.team_mac_bots()}
        if parts == ['bots'] and method == 'POST':
            return self.team_declare_bot(data)
        if len(parts) == 2 and parts[0] == 'bots' and method == 'PATCH':
            return self.team_change_bot(parts[1], data)
        if len(parts) == 2 and parts[0] == 'bots' and method == 'DELETE':
            return self.team_remove_bot(parts[1])
        return super()._team_route(method, path, data, repeat)

    def team_rooms(self) -> dict:
        """As on a full Mac, with the mode, so `coconut team add-bot` can say how to declare a missing bot."""
        return dict(super().team_rooms(), mode=MODE)

    def team_status(self) -> dict:
        """GET /v1/team/status: what this service is and how its rooms are, for a tool and for a person."""
        answer = self.team_rooms()
        rooms = [{'room': room['room'], 'name': room['name'], 'state': room['state'], 'members': len(room['members']),
                  'bots': len(room['bots']), 'synced_at': room['sync']['synced_at'], 'error': room['sync']['error'],
                  'socket': room['sync']['socket']} for room in answer['rooms']]
        return {'mode': MODE, 'version': self.version, 'helper': answer['helper'], 'bots': len(answer['mac_bots']),
                'rooms': rooms}

    def team_mac_bots(self) -> list[dict]:
        with self.lock:
            return [self._bot_view(bot) for bot in self._all('bots')]

    def _bot_view(self, bot: dict) -> dict:
        """A bot record with the rooms that it is shared with. The caller holds the service lock."""
        return {'id': bot['id'], 'name': bot['name'], 'avatar': bot['avatar'], 'color': bot['color'],
                'home': bot['home'], 'teams': self.team_store.rooms_of(bot['id'])}

    def team_declare_bot(self, data: dict) -> dict:
        """Declare a bot for the Hermes home that it stands for. One home has one bot, and one name one bot, because
        a tool finds a bot by its name."""
        fields = member_profile(data, NO_BOT, field='name', noun='the name of the bot')
        home = checked_home(data['home']) if 'home' in data else default_home()
        with self.transaction():
            self._check_bot(fields['name'], home)
            bot = dict(fields, id=uid(), home=home, archived=False, created_at=now())
            self._put('bots', bot)
            return {'bot': self._bot_view(bot)}

    def team_change_bot(self, bot_id: str, data: dict) -> dict:
        """Change the name, the avatar or the color of a bot. The next pass writes its `bot` entry in each room."""
        if 'home' in data:
            raise ServiceError(400, 'A bot keeps its Hermes home. Remove the bot, then declare it again with the new '
                                    'home.')
        with self.transaction():
            bot = self._declared_bot(bot_id)
            bot.update(member_profile(data, bot, field='name', noun='the name of the bot'))
            self._check_bot(bot['name'], bot['home'], bot['id'])
            self._put('bots', bot)
            answer = {'bot': self._bot_view(bot)}
        self.team_wake()
        return answer

    def team_remove_bot(self, bot_id: str) -> dict:
        """Remove a bot and its pages. It first stops sharing the bot with each room, as team_unshare does, in the
        same transaction. The next pass replaces its entries with `delete` entries."""
        with self.transaction():
            self._declared_bot(bot_id)
            for room in self.team_store.rooms_of(bot_id):
                self.team_store.unshare(room, bot_id)
            self._remove_bot_documents(bot_id)
            self.db.execute('DELETE FROM bots WHERE id=?', (bot_id,))
        self.team_wake()
        return {'bot': bot_id, 'removed': True}

    def _declared_bot(self, bot_id: str) -> dict:
        bot = self._team_bot(bot_id) if unicode_text(bot_id) else None
        if bot is None:
            raise ServiceError(404, 'This Mac has no bot with this ID.')
        return bot

    def _check_bot(self, name: str, home: str, bot_id: str | None = None) -> None:
        """Refuse a second bot for one home or one name. The caller holds the transaction."""
        for other in self._all('bots'):
            if other['id'] == bot_id:
                continue
            if other['home'] == home:
                raise ServiceError(409, f'The bot {other["name"]} already stands for this Hermes home. A Hermes home '
                                        'has one bot.')
            if other['name'].casefold() == name.casefold():
                raise ServiceError(409, f'This Mac already has a bot with the name {other["name"]}. Choose another '
                                        'name.')

    def gateway_document(self, data: dict) -> dict:
        """POST /v1/gateway/document: a page of a declared bot for its teams. The rules are those of a full Mac
        (Documents.publish_page), and each page names the audience "team"."""
        # The raw text, as at the declaration: a strip would change a home whose folder name ends with a space.
        home = checked_home(data.get('home'))
        if data.get('audience') != 'team':
            raise ServiceError(400, TEAM_PAGE)
        with self.lock:
            row = self.db.execute("SELECT id FROM bots WHERE json_extract(data, '$.home')=?", (home,)).fetchone()
        if row is None:
            raise ServiceError(404, UNKNOWN_HOME)
        return self.publish_page(row[0], data)
