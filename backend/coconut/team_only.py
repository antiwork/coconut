"""Coconut for a team only: a service for a Mac that runs a Hermes agent with no person at it (docs/team-log.md, "Mac").

`python3 -m coconut --team-only` (or COCONUT_MODE=team) runs this service object in server.py in place of Service. It
does four things:

1. It keeps the team rooms of this Mac in step with the relay: the store, the sync thread, the socket watcher and the
   helper of team.py, as on a full Mac.
2. It serves the team routes on the main listener with the token of <data>/gateway/bridge.json, so `coconut team` and
   scripts/team.py work against it.
3. It keeps the bots that this Mac declares for the team, and it saves their pages for the team.
4. It gives the messages of the team chat that name a bot with its chat connected to the Hermes gateway of that bot,
   and writes the replies into the chat (team_turns.py). The adapter of the `coconut` plugin in the mode `team` takes
   the turns with the routes poll, events and messages of the bridge (gateway_bridge.py).

It imports no Hermes module and none of the full runtime (service.py and what it loads). It reads and changes no file
under a Hermes home, starts no gateway, loads no plugin, copies no key, runs no scheduler, worker or kanban adapter,
opens no tunnel listener, pairs no phone and sends no push notification. Its only child process is the team helper.
Only the operator's command `coconut team connect-chat` adds the plugin to the Hermes home of a bot
(scripts/team_chat.py), in a process of its own. It sets `plugin` on the record of the bot before the first change
of the home, and `chat` after the last one.

A bot of this Mac is a record that the person declares (`POST /v1/team/bots`): a name, an avatar and a color with the
rules of a `member` entry, and the Hermes home that the bot stands for. The service stores the path of the home and
compares it. It never opens it. A page (`POST /v1/gateway/document`) names the home, so the home names the bot. No
person reads the Library on this Mac, so each page names the audience "team". A page of a bot that no team has yet
waits on this Mac, and the sync writes it when the bot is shared. The tooling deletes a page with
`DELETE /v1/gateway/document` and `{home, key}`, without an audience.

Routes, each with the token of the bridge file:

- `GET /v1/team/status`: the mode, the version of the code, whether the helper is there, the count of the bots and of
  the bots that answer in the team chat, and the rooms in short form.
- `GET /v1/team/bots`, `POST /v1/team/bots` with `{name, avatar, color, home}`,
  `PATCH /v1/team/bots/<bot>` with `{name, avatar, color, chat, plugin}`, `DELETE /v1/team/bots/<bot>`. `plugin` is
  true while the Hermes home of the bot has, or may have, the changes of `coconut team connect-chat`. Each bot in `GET`
  also has `adapter`: whether the adapter of its Hermes gateway polls now.

`chat` says that the bot answers in the team chat. A declared bot starts with false. The step that connects the bot
to its Hermes agent sets it to true. While it is true, the `bot` entry of the bot has `open: true`, so the app offers
the bot after `@`. A change of `chat` writes the entry again in each team of the bot at the next pass.
- The routes of team.py, `POST /v1/gateway/document` and `DELETE /v1/gateway/document`.
- `POST /v1/gateway/poll`, `/v1/gateway/events` and `/v1/gateway/messages` for the adapter of the team mode.
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
from .hermes_notices import readable_text
from .service_base import (MODE_MARK, TEAM_MODE, ServiceError, check_data_folder, encoded, now, open_store,
                           release_data_folder, take_data_folder, uid)
from .service_restart import ServiceRestart
from .team import SCHEMA as TEAM_SCHEMA, Team, member_profile
from .team_store import ACTIVE, unicode_text
from .team_turns import SAID, SAY_FAILED, TeamTurns, parse_chat, parts

MODE = TEAM_MODE
REFUSAL = 'This Mac runs Coconut for a team only, so it does not serve this request.'
MAX_HOME = 4096
HOME = 'Give the Hermes home of the agent as a full path that starts with /.'
TEAM_PAGE = ('This Mac runs Coconut for a team only, so no person reads a page here. Save the page with audience '
             '"team".')
UNKNOWN_HOME = ('This Hermes home is not the home of an agent of this Mac. Declare the agent first: coconut team new-agent '
                '<name> --home <path>.')
CHAT_CONNECTED = ('The Hermes home of the agent {name} has the team chat of Coconut. Run coconut team disconnect-chat '
                  '{name} first, then remove the agent.')
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


class TeamOnlyService(TeamTurns, Documents, Team):
    """The service object of the team-only mode. server.Handler serves it with Handler._team_only."""

    team_only = True
    # An update waits for a running turn of a bot (TeamTurns._team_poll_drain, host.drain_for_update).
    drains = True
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
        # Handler._team_only serves the poll, the events and the messages of the adapter of the team mode.
        self.gateway_bridge = GatewayBridge(self.root)
        self._init_team()
        self._init_team_turns()

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
            noun = 'Agent' if table == 'bots' else table[:-1].capitalize()
            raise ServiceError(404, f'{noun} was not found.')
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
        self._start_team_turns()

    def close(self):
        self.stopping.set()
        # A running turn ends at once, and the trigger keeps `started`: the next start posts the notice.
        self.gateway_bridge.close()
        self._close_team_turns()
        self._close_team()
        with self.lock:
            self.db.close()
            release_data_folder(self.process_lock)

    # Routes

    def _team_route(self, method: str, path: str, data: dict, repeat: str = '', device: str = '',
                    direct: bool = False) -> dict:
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
        return super()._team_route(method, path, data, repeat, device, direct)

    def team_rooms(self, direct: bool = False) -> dict:
        """As on a full Mac, with the mode, so `coconut team add-agent` can say how to declare a missing bot. No person
        is at this Mac, so `direct` is always false here (Team._team_direct_asked)."""
        return dict(super().team_rooms(direct), mode=MODE)

    def team_status(self) -> dict:
        """GET /v1/team/status: what this service is and how its rooms are, for a tool and for a person."""
        answer = self.team_rooms()
        rooms = [{'room': room['room'], 'name': room['name'], 'state': room['state'], 'members': len(room['members']),
                  'bots': len(room['bots']), 'synced_at': room['sync']['synced_at'], 'error': room['sync']['error'],
                  'socket': room['sync']['socket']} for room in answer['rooms']]
        with self.lock:
            chat = sum(bot.get('chat') is True for bot in self._all('bots'))
        return {'mode': MODE, 'version': self.version, 'helper': answer['helper'], 'bots': len(answer['mac_bots']),
                'chat_bots': chat, 'rooms': rooms}

    def team_mac_bots(self) -> list[dict]:
        with self.lock:
            return [self._bot_view(bot) for bot in self._all('bots')]

    def _bot_view(self, bot: dict) -> dict:
        """A bot record with the rooms that it is shared with. The caller holds the service lock."""
        return {'id': bot['id'], 'name': bot['name'], 'avatar': bot['avatar'], 'color': bot['color'],
                'home': bot['home'], 'chat': bot.get('chat') is True, 'plugin': bot.get('plugin') is True,
                'teams': self.team_store.rooms_of(bot['id']),
                'adapter': self.gateway_bridge.connected(bot['home']),
                'lacking': self.gateway_bridge.lacking_of(bot['home'])}

    def team_declare_bot(self, data: dict) -> dict:
        """Declare a bot for the Hermes home that it stands for. One home has one bot, and one name one bot, because
        a tool finds a bot by its name."""
        fields = member_profile(data, NO_BOT, field='name', noun='the name of the agent')
        home = checked_home(data['home']) if 'home' in data else default_home()
        with self.transaction():
            self._check_bot(fields['name'], home)
            # The bot answers in the team chat only after the step that connects it to its agent.
            bot = dict(fields, id=uid(), home=home, chat=False, archived=False, created_at=now())
            self._put('bots', bot)
            return {'bot': self._bot_view(bot)}

    def team_change_bot(self, bot_id: str, data: dict) -> dict:
        """Change the name, the avatar, the color or `chat` of a bot. The next pass writes its `bot` entry in each
        room. Only `coconut team connect-chat` sets `chat` true, and `disconnect-chat` sets it false before it takes
        the plugin away. A bot whose chat goes off stops its running turns and queues no new trigger."""
        if 'home' in data:
            raise ServiceError(400, 'An agent keeps its Hermes home. Remove the agent, then declare it again with the new '
                                    'home.')
        for flag in ('chat', 'plugin'):
            if flag in data and not isinstance(data[flag], bool):
                raise ServiceError(400, f'Give {flag} as true or false.')
        with self.transaction():
            bot = self._declared_bot(bot_id)
            bot.update(member_profile(data, bot, field='name', noun='the name of the agent'))
            for flag in ('chat', 'plugin'):
                if flag in data:
                    bot[flag] = data[flag]
            self._check_bot(bot['name'], bot['home'], bot['id'])
            self._put('bots', bot)
            answer = {'bot': self._bot_view(bot)}
        if data.get('chat') is False:
            self._team_stop_turns(bot=bot_id)
        self.team_wake()
        return answer

    def team_remove_bot(self, bot_id: str) -> dict:
        """Remove a bot and its pages. It first stops sharing the bot with each room, as team_unshare does, in the
        same transaction. The next pass replaces its entries with `delete` entries."""
        with self.transaction():
            bot = self._declared_bot(bot_id)
            # The plugin in the Hermes home would stay. Only the command that added it takes it away.
            if bot.get('chat') is True or bot.get('plugin') is True:
                raise ServiceError(409, CHAT_CONNECTED.format(name=bot['name']))
            for room in self.team_store.rooms_of(bot_id):
                self.team_store.unshare(room, bot_id)
            self._remove_bot_documents(bot_id)
            self.db.execute('DELETE FROM bots WHERE id=?', (bot_id,))
        self.team_wake()
        return {'bot': bot_id, 'removed': True}

    def _declared_bot(self, bot_id: str) -> dict:
        bot = self._team_bot(bot_id) if unicode_text(bot_id) else None
        if bot is None:
            raise ServiceError(404, 'This Mac has no agent with this ID.')
        return bot

    def _check_bot(self, name: str, home: str, bot_id: str | None = None) -> None:
        """Refuse a second bot for one home or one name. The caller holds the transaction."""
        for other in self._all('bots'):
            if other['id'] == bot_id:
                continue
            if other['home'] == home:
                raise ServiceError(409, f'The agent {other["name"]} already stands for this Hermes home. A Hermes home '
                                        'has one agent.')
            if other['name'].casefold() == name.casefold():
                raise ServiceError(409, f'This Mac already has an agent with the name {other["name"]}. Choose another '
                                        'name.')

    def gateway_document(self, data: dict) -> dict:
        """POST /v1/gateway/document: a page of a declared bot for its teams. The rules are those of a full Mac
        (Documents.publish_page), and each page names the audience "team"."""
        # The raw text, as at the declaration: a strip would change a home whose folder name ends with a space.
        home = checked_home(data.get('home'))
        if data.get('audience') != 'team':
            raise ServiceError(400, TEAM_PAGE)
        return self.publish_page(self._home_bot(home), data)

    def gateway_document_delete(self, data: dict) -> dict:
        """DELETE /v1/gateway/document: the tooling deletes a page of a declared bot (Documents.unpublish_page). A
        delete names no audience. The next pass of the sync replaces the entry of a team page with a `delete` entry."""
        return self.unpublish_page(self._home_bot(checked_home(data.get('home'))), data)

    def _home_bot(self, home: str) -> str:
        """The ID of the declared bot of a checked home, or the reason to refuse it."""
        with self.lock:
            row = self.db.execute("SELECT id FROM bots WHERE json_extract(data, '$.home')=?", (home,)).fetchone()
        if row is None:
            raise ServiceError(404, UNKNOWN_HOME)
        return row[0]

    def gateway_message(self, data: dict) -> dict:
        """POST /v1/gateway/messages: a send of Hermes in a chat of the team outside a turn of Coconut, for example the
        reply of its own turn after a background process. A send becomes a message of the bot without `reply_to`, and
        its entry ID comes from the message ID of Hermes, so a send that the adapter repeats adds none. A message of
        the chat is complete, so an edit and a delete change nothing. In a direct chat, a send is a direct message of
        the bot to the person of the chat, while that person is in the member list (TeamTurns._team_say). The answer is
        `recorded: false` with a reason when the log does not have each part of the text. After a failure of the relay
        or the helper it also has `retry: true`: a repeat with the same message ID can write the missing parts. The
        sender of a cron run without a gateway repeats then, and gives the reason of a last failure to Hermes."""
        home, chat, message_id = data.get('home'), parse_chat(data.get('chat_id')), data.get('message_id')
        if not isinstance(home, str) or chat is None or not isinstance(message_id, str):
            raise ServiceError(400, 'Name the home, the chat and the message.')
        room, bot_id, named, direct = chat
        with self.lock:
            bot = self._team_bot(bot_id) if unicode_text(bot_id) else None
        if bot is None or bot['home'] != os.path.realpath(home):
            raise ServiceError(404, 'This chat is not a team chat of an agent of this Hermes home.')
        text = data.get('text')
        if data.get('kind') != 'send' or not isinstance(text, str) or not text.strip():
            return {'recorded': False, 'reason': 'A message of the team chat is complete. It takes no edit.'}
        with self.lock:
            room_row = self.team_store.room(room)
            answers = room_row is not None and bot_id in dict(self.team_store.answering(room))
            # A send into a named chat that the team removed goes nowhere, and so does a send to a person who left.
            gone = named is not None and self.team_store.chat(room, named) is None
            gone = gone or answers and direct is not None and (direct == room_row['member']
                                                               or self.team_store.member(room, direct) is None)
        if not answers or room_row['state'] != ACTIVE or gone:
            return {'recorded': False, 'reason': 'The agent does not answer in this team chat.'}
        pieces = parts(readable_text(text))
        shown, result = 0, SAID
        for number, piece in enumerate(pieces):
            # A send outside a turn answers no message, so no request must still stand: only the bot must answer.
            # A part that the log did not take stops the send, so the chat shows no later part before it.
            result = self._team_say(room, bot_id, message_id, number, piece, request=False, chat=named, direct=direct)
            if result != SAID:
                break
            shown += 1
        # The answer is true only when the log has each part. A repeat with the same message ID writes only the parts
        # that the log does not have, so after a failure of the relay or the helper the answer has `retry: true`.
        if shown == len(pieces):
            return {'recorded': True}
        answer = {'recorded': False}
        if shown:
            # `shown`: the chat shows the first parts, so a send under a new message ID would show them two times.
            answer.update(reason=f'The chat shows only {shown} of {len(pieces)} parts of this message.', shown=shown)
        elif result == SAY_FAILED:
            answer['reason'] = 'The team log did not take this message.'
        else:
            answer['reason'] = 'The agent does not answer in this team chat.'
        if result == SAY_FAILED:
            answer['retry'] = True
        return answer
