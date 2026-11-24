"""A project can have more than one chat. Each chat is a fresh conversation on the same Hermes profile.

A chat is a bot of its own with `chat_of`, the ID of its project. It has a new ID and a new session, so Hermes starts a
new conversation, and it has a row in the `profiles` table with the name and the folder of the project's profile
(profile_registry). Coconut creates no profile for it and copies no file. The Library is that of the project too
(`_agent_id`).

The first message of the person gives the chat its title (`chat_title`). The apps list the chats under their agent,
newest first. A chat can be archived or removed by itself. The settings belong to the agent: a chat takes only
`archived`, `pinned`, `notifications` and `chat_title`.

A quick to-do of the apps is such a chat with `todo: true`: the apps start it with the to-do as its first message, list
it on To-dos, and follow its state through the tasks of the chat. Archiving the chat checks the to-do off.

A chat goes to Archived by itself after a time without activity, as the person asked (archive_idle_chats). The time
is the setting auto_archive_hours: 48 hours until the person picks another time, or 0 for never, on You in the apps or
in the Coconut chat. A chat that still has something to do or to show stays. A message of the person brings an archived
chat back (_bring_back), so an old chat continues where it was.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging

from .unread import READ_AT

# What a chat takes from its agent when the person starts it, and again when the agent changes.
SHARED = ('name', 'role')
# The fields that the person changes on one chat.
OWN = frozenset({'archived', 'pinned', 'notifications', 'chat_title'})
TITLE_LENGTH = 72
# A chat without activity for this many hours goes to Archived (archive_idle_chats). The person changes the time, or
# turns it off with 0, on You in the apps or in the Coconut chat (PUT /v1/settings/auto-archive).
AUTO_ARCHIVE = 'auto_archive_hours'
AUTO_ARCHIVE_HOURS = 48
# The longest time that the setting takes: a year.
AUTO_ARCHIVE_MAX_HOURS = 24 * 365
# How often the dispatcher looks for such chats (Service._dispatch).
ARCHIVE_PASS_SECONDS = 600
# A routine in this state never runs again, so it keeps no chat out of Archived. A scheduled or paused one does.
FINISHED_ROUTINE = 'completed'
# The newest visible message of a chat. The index messages_chat finds it without a sort, as in unread._newest.
NEWEST_MESSAGE = ("SELECT json_extract(data,'$.created_at') FROM messages WHERE json_extract(data,'$.bot_id')=? "
                  "AND COALESCE(json_extract(data,'$._hidden'),0)=0 AND COALESCE(json_extract(data,'$._deleted'),0)=0 "
                  "ORDER BY rowid DESC LIMIT 1")
NEWEST_TASK = "SELECT json_extract(data,'$.updated_at') FROM tasks WHERE json_extract(data,'$.bot_id')=? ORDER BY rowid DESC LIMIT 1"
TASK_BOTS = "SELECT json_extract(data,'$.bot_id'), json_extract(data,'$.conversation_bot_id') FROM tasks WHERE id=?"


def moment(value) -> datetime | None:
    """A stored time as an aware datetime, or None. A time without a zone is UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def chat_title(text: str) -> str:
    """The title of a chat from its first message: the first line with text, at most TITLE_LENGTH characters."""
    line = next((line.strip() for line in str(text).splitlines() if line.strip()), '')
    return line if len(line) <= TITLE_LENGTH else line[:TITLE_LENGTH - 1].rstrip() + '…'


class AgentChats:
    def _agent_id(self, bot_id: str) -> str:
        """The agent of a bot: the bot itself, or the agent of a chat."""
        return self._get('bots', bot_id).get('chat_of') or bot_id

    def _same_agent(self, first: str, second: str) -> bool:
        """Whether two bots are one agent: the same bot, two chats of one agent, or an agent and one of its chats. A
        bot that is not on the Mac is no agent."""
        from .service_base import ServiceError
        if first == second:
            return True
        try:
            return self._agent_id(first) == self._agent_id(second)
        except ServiceError:
            return False

    def _agent_chats(self, agent_id: str) -> list[dict]:
        return [bot for bot in self._all('bots') if bot.get('chat_of') == agent_id and not bot.get('_deleted')]

    def _check_chat_change(self, bot: dict, data: dict) -> None:
        from .service_base import ServiceError
        if bot.get('chat_of') and isinstance(data, dict) and set(data) - OWN:
            raise ServiceError(409, 'A chat shares the settings of its agent. Change them on the agent.')
        if 'chat_title' in data:
            if not bot.get('chat_of'):
                raise ServiceError(409, 'Only a chat of an agent has a title.')
            if not isinstance(data['chat_title'], str) or not data['chat_title'].strip() or len(data['chat_title']) > 160:
                raise ServiceError(400, 'chat_title must contain 1 to 160 characters.')

    def _new_chat(self, bot: dict, *, todo: bool = False) -> dict:
        """Start a new chat with the agent of this bot, a quick to-do with `todo`. The caller holds the transaction."""
        from .service_base import ServiceError, now, uid
        agent = self._get('bots', bot.get('chat_of') or bot['id'])
        if agent.get('_deleted'):
            raise ServiceError(404, 'This agent was deleted.')
        if agent['archived']:
            raise ServiceError(409, 'Restore this agent before starting a new chat.')
        if not self._is_project(agent['id']):
            from .bot_profiles import ONLY_PROJECTS
            raise ServiceError(409, ONLY_PROJECTS)
        row = self.profiles.rows.get(agent['id'])
        if row is None or row['state'] != 'active':
            raise ServiceError(409, 'This agent has no Hermes profile to chat with.')
        # Older apps read `avatar` and `color`. A chat has no face, so both stay empty.
        chat = dict(id=uid(), session_id=uid(), chat_of=agent['id'], chat_title='', avatar='', color='', notifications=agent.get('notifications', True),
                    status='idle', last_message='', unread_count=0, last_read_message_id=None, archived=False, pinned=False, created_at=now())
        if todo:
            chat['todo'] = True
        chat.update({field: agent[field] for field in SHARED if field in agent})
        chat['profile_id'] = chat['id']
        # The chat runs in the agent's profile. Hermes creates nothing, and no file is copied.
        self._attach_profile(chat, dict(name=row['name'], home=row['home']))
        self._put('bots', chat)
        self._event('bot.created', chat | {'chat_of': agent['id']}, chat['id'])
        return chat

    def _title_chat(self, bot: dict, text: str) -> None:
        """The first message of the person names the chat."""
        if bot.get('chat_of') and not bot.get('chat_title'):
            bot['chat_title'] = chat_title(text)
            self._put('bots', bot)

    def _follow_agent(self, agent: dict) -> None:
        """Give the chats of a project its changed name."""
        from .service_base import encoded
        if agent.get('chat_of'):
            return
        for chat in self._agent_chats(agent['id']):
            before = dict(chat)
            chat.update({field: agent[field] for field in SHARED if field in agent})
            if chat != before:
                self._put('bots', chat)
                self._event('bot.updated', self._public(chat), chat['id'])
                self._note_published(chat['id'], encoded(self._public(chat)))

    def auto_archive_hours(self) -> int:
        """The hours without activity after which a chat archives itself. 0 means never."""
        value = self._setting(AUTO_ARCHIVE, AUTO_ARCHIVE_HOURS)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= AUTO_ARCHIVE_MAX_HOURS:
            return value
        return AUTO_ARCHIVE_HOURS

    def set_auto_archive(self, data) -> dict:
        """PUT /v1/settings/auto-archive: the hours without activity after which a chat archives itself, 0 for never.
        The apps re-read the settings on the event, so You shows the new time on every device."""
        from .service_base import ServiceError
        hours = data.get('hours') if isinstance(data, dict) and set(data) == {'hours'} else None
        if not isinstance(hours, int) or isinstance(hours, bool) or not 0 <= hours <= AUTO_ARCHIVE_MAX_HOURS:
            raise ServiceError(400, f'Choose a number of hours from 1 to {AUTO_ARCHIVE_MAX_HOURS}, or 0 for never.')
        self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (AUTO_ARCHIVE, json.dumps(hours)))
        self._event('settings.updated', {AUTO_ARCHIVE: hours})
        return {AUTO_ARCHIVE: hours}

    def archive_idle_chats(self, current: datetime | None = None) -> list[str]:
        """Archive each chat without activity for the time of the setting, as Archive in the apps does, and return their
        IDs. A setting of 0 archives nothing.

        Activity is a visible message, a change of the chat's newest task, a read of a new reply (unread.READ_AT), the
        start of the chat, and a restore from Archived. Only a further chat of a project is a candidate, so a project
        and the Coconut chat never go (_may_archive), and a chat that still waits for something stays (_busy_chats).
        """
        from .service_base import now
        current = current or datetime.now(timezone.utc)
        hours = self.auto_archive_hours()
        if hours == 0:
            return []
        after = timedelta(hours=hours)
        archived = []
        with self.transaction():
            candidates = [bot for bot in self._all('bots') if self._may_archive(bot)]
            busy = self._busy_chats() if candidates else set()
            for bot in candidates:
                last = None if bot['id'] in busy else self._last_activity(bot)
                if last is None or current - last < after:
                    continue
                bot['archived'] = True
                bot['_auto_archived_at'] = now()
                self._publish_archive(bot)
                archived.append(bot['id'])
        if archived:
            logging.getLogger(__name__).info('Archived %d chats without activity for %d hours: %s',
                                             len(archived), hours, ', '.join(archived))
        return archived

    @staticmethod
    def _may_archive(bot: dict) -> bool:
        """A further chat that a list shows and that holds nothing for the person: no star, no unread reply, and no
        work or card that its status shows. A quick to-do stays until the person checks it off, because its archive
        checks it off."""
        return (bool(bot.get('chat_of')) and not bot.get('archived') and not bot.get('_deleted')
                and not bot.get('pinned') and not bot.get('todo') and not bot.get('unread_count')
                and bot.get('status', 'idle') in ('idle', 'failed'))

    def _busy_chats(self) -> set:
        """The bots that still wait for something. The caller holds the transaction.

        An archived chat runs no task, takes no result of a wait or of another agent, fires no routine and sends no
        notification, not even for a card. So each of these keeps its chat out of Archived: a task that is queued,
        runs, waits or is paused, a result on its way back (Service._deliver_returns), a wait of a turn without its
        delivery (coconut_wait, a background process), a card that waits, also after Later, Work that runs, and a
        routine that runs again or is paused.
        """
        from .background_processes import PENDING_COMPLETIONS
        busy = set()
        states = sorted(self.ACTIVE)
        busy.update(value for row in self.db.execute(
            "SELECT json_extract(data,'$.bot_id'), json_extract(data,'$.conversation_bot_id') FROM tasks "
            f"WHERE json_extract(data,'$.status') IN ({','.join('?' * len(states))})", states) for value in row)
        busy.update(value for row in self.db.execute(
            "SELECT json_extract(data,'$.bot_id'), json_extract(data,'$.conversation_bot_id'), "
            "json_extract(data,'$._return_pending.target_bot_id') FROM tasks "
            "WHERE json_type(data,'$._return_pending') IN ('object','true')") for value in row)
        waits = [row[0] for row in self.db.execute(
            "SELECT json_extract(data,'$.task_id') FROM native_receipts INDEXED BY native_receipts_pending_wait WHERE "
            + PENDING_COMPLETIONS)]
        cards = [json.loads(raw) for (raw,) in self.db.execute(
            "SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending'")]
        for card in cards:
            busy.update((card.get('bot_id'), card.get('request_bot_id')))
        for task_id in waits + [card.get('task_id') for card in cards]:
            row = self.db.execute(TASK_BOTS, (task_id,)).fetchone() if task_id else None
            busy.update(row or ())
        busy.update(row[0] for row in self.db.execute(
            "SELECT json_extract(data,'$.bot_id') FROM contexts "
            "WHERE json_extract(data,'$.status') NOT IN ('completed','cancelled','failed')"))
        for (raw,) in self.db.execute('SELECT data FROM routines'):
            routine = json.loads(raw)
            if not routine.get('_deleted') and (routine.get('enabled') or routine.get('state') not in (None, FINISHED_ROUTINE)):
                busy.add(routine.get('bot_id'))
        busy.discard(None)
        return busy

    def _last_activity(self, bot: dict) -> datetime | None:
        """The newest activity of a chat, or None for a chat that nobody wrote in: no list shows such a chat, and the
        next first message of its agent goes into it (AgentChats.spare in the apps), so it stays."""
        newest = self.db.execute(NEWEST_MESSAGE, (bot['id'],)).fetchone()
        if newest is None:
            return None
        task = self.db.execute(NEWEST_TASK, (bot['id'],)).fetchone()
        times = (newest[0], task[0] if task else None, bot.get('created_at'), bot.get(READ_AT), bot.get('_restored_at'))
        return max((value for value in map(moment, times) if value), default=None)

    @staticmethod
    def _note_restore(bot: dict) -> None:
        """A restore from Archived counts as activity, so the next pass does not archive the chat again at once."""
        from .service_base import now
        bot['_restored_at'] = now()
        bot.pop('_auto_archived_at', None)

    def _bring_back(self, bot: dict) -> None:
        """A message of the person brings an archived chat back, as in Telegram, where Coconut refused the message
        before ("Restore this conversation"). The caller holds the transaction."""
        if bot.get('archived') and not bot.get('_deleted'):
            bot['archived'] = False
            self._note_restore(bot)
            self._publish_archive(bot)

    def _publish_archive(self, bot: dict) -> None:
        """Store an archive or a restore of a bot and tell the apps, as a PATCH of `archived` does. The caller holds
        the transaction."""
        from .service_base import encoded
        self._sync_profile(bot)
        self._put('bots', bot)
        if self.team_store.shared(bot['id']):
            # The team sync writes the `bot` entry again, or deletes it for an archived bot (team_pages.py).
            self.team_wake()
        self._event('bot.updated', self._public(bot), bot['id'])
        self._note_published(bot['id'], encoded(self._public(bot)))
        self._event('settings.updated', {'bot_id': bot['id']}, bot['id'])

    def _delete_agent_chats(self, agent: dict, callbacks: list, *, audit: dict | None = None) -> None:
        """The chats of a removed agent go with it."""
        if agent.get('chat_of'):
            return
        for chat in self._agent_chats(agent['id']):
            self._delete_bot(chat, callbacks, audit=audit)
