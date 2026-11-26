"""A project can have more than one chat. Each chat is a fresh conversation on the same Hermes profile.

A chat is a bot of its own with `chat_of`, the ID of its project. It has a new ID and a new session, so Hermes starts a
new conversation, and it has a row in the `profiles` table with the name and the folder of the project's profile
(profile_registry). Coconut creates no profile for it and copies no file. The Library is that of the project too
(`_agent_id`).

The first message of the person gives the chat its title (`chat_title`). The apps list the chats under their agent,
newest first. A chat can be archived or removed by itself. The settings belong to the agent: a chat takes only
`archived`, `pinned`, `notifications` and `chat_title`.

Coconut makes no quick to-do any more: the person removed to-do creation (github_todos.py). A chat that an older build
made as one keeps `todo: true`, which nothing reads, and archives like any other chat.

A chat goes to Archived by itself after a time without activity, as the person asked (archive_idle_chats). The time
is the setting auto_archive_hours: 14 hours until the person picks another time, or 0 for never, on You in the apps or
in the Coconut chat. A chat that still has something to do or to show stays. A message of the person brings an archived
chat back (_bring_back), so an old chat continues where it was.

The person merges two chats of one project by pinching their cards together in the grid of the iPhone (merge_chat). One
chat goes into the other: its agent stops, it goes to Archived, and the agent of the other chat gets its transcript and
takes over its work.
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
# turns it off with 0, on You in the apps or in the Coconut chat (PUT /v1/settings/auto-archive). The time is 14 hours
# until the person picks another (the person: "everything goes into there after n time (settings default 14h)"), so the
# lists keep the chats of the day. It was 48 hours before.
AUTO_ARCHIVE = 'auto_archive_hours'
AUTO_ARCHIVE_HOURS = 14
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
# The longest transcript of a merged chat that its merge message carries, and the longest message in it (merge_chat).
# The newest messages stay.
MERGE_TRANSCRIPT = 12000
MERGE_MESSAGE = 3000


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


def chat_name(bot: dict) -> str:
    """The name of a chat in a merge message, as the grid of the apps shows it: the title of a further chat, or the
    name of a project."""
    from .home_screen import chat_label
    return chat_label(bot) if bot.get('chat_of') else bot.get('name') or 'Agent'


def merge_transcript(messages: list[dict], cards: list[dict]) -> str:
    """The transcript of a merged chat for the agent that takes it over: its visible messages, the newest last, and the
    cards that waited for the person. Only the newest MERGE_TRANSCRIPT characters stay."""
    lines = []
    for message in reversed(messages):
        text = message['text'].strip()
        if len(text) > MERGE_MESSAGE:
            text = text[:MERGE_MESSAGE].rstrip() + ' […]'
        line = ('Person: ' if message['role'] == 'user' else 'Agent: ') + text
        if lines and sum(map(len, lines)) + len(line) > MERGE_TRANSCRIPT:
            lines.append('[Earlier messages are left out.]')
            break
        lines.append(line)
    lines.reverse()
    for card in cards:
        labels = [option.get('label') for option in card.get('options') or [] if isinstance(option, dict) and option.get('label')]
        lines.append(f'Card that waited for the person: {card.get("title") or "Untitled"}'
                     + (f' (choices: {", ".join(labels)})' if labels else ''))
    return '\n\n'.join(lines) if lines else 'The chat has no messages.'


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

    def _new_chat(self, bot: dict) -> dict:
        """Start a new chat with the agent of this bot. The caller holds the transaction."""
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
        work or card that its status shows."""
        return (bool(bot.get('chat_of')) and not bot.get('archived') and not bot.get('_deleted')
                and not bot.get('pinned') and not bot.get('unread_count')
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

    def merge_chat(self, into: dict, data, callbacks: list) -> dict:
        """POST /v1/bots/<id>/merge: the person pinched two cards of the grid together (ChatGridView in the apps), and
        the chat of `chat_id` goes into this one. Its agent stops, maybe in the middle of a step, and it goes to
        Archived, where it stays readable. A message of the person here carries its transcript and its open cards, so
        the agent of this chat takes over its work (native_conversation.message_content). Both are chats of one
        project, and a project never goes. Return the merged chat. The caller holds the transaction."""
        from .native_conversation import visible_history
        from .service_base import ServiceError
        chat_id = data.get('chat_id') if isinstance(data, dict) else None
        if not isinstance(chat_id, str) or not chat_id:
            raise ServiceError(400, 'Send the chat_id of the chat to merge.')
        if chat_id == into['id']:
            raise ServiceError(400, 'A chat cannot merge into itself.')
        chat = self._get('bots', chat_id)
        if chat.get('_deleted'):
            raise ServiceError(404, 'This chat was deleted.')
        if not chat.get('chat_of'):
            raise ServiceError(409, 'A project stays. Merge its chats into it instead.')
        if not self._same_agent(into['id'], chat['id']):
            raise ServiceError(409, 'Only chats of the same project can merge.')
        if into['archived'] or chat['archived']:
            raise ServiceError(409, 'Restore both chats before merging them.')
        # An archived chat fires no routine, so a merge would stop it without a word.
        if any(routine.get('bot_id') == chat['id'] and not routine.get('_deleted')
               and (routine.get('enabled') or routine.get('state') not in (None, FINISHED_ROUTINE))
               for routine in self._all('routines')):
            raise ServiceError(409, 'This chat has a routine. Stop it before merging the chat.')
        # A chat without a title takes the title of the chat that comes in, not of its merge message (_title_chat).
        if into.get('chat_of') and not into.get('chat_title') and chat.get('chat_title'):
            into = dict(self._get('bots', into['id']), chat_title=chat['chat_title'])
            self._put('bots', into)
        title, into_title = chat_name(chat), chat_name(into)
        cards = [card for card in self._all('decisions')
                 if card.get('status') == 'pending' and chat['id'] in (card.get('bot_id'), card.get('request_bot_id'))]
        transcript = merge_transcript(visible_history(self._history(chat['id'])), cards)
        active = [task['id'] for task in self._all('tasks')
                  if task['status'] in self.ACTIVE and chat['id'] in (task['bot_id'], task.get('conversation_bot_id'))]
        for task_id in active:
            task = self._get('tasks', task_id)
            if task['status'] in self.ACTIVE:
                self._cancel(task, callbacks)
        # The last line of the chat says where its work went, and the apps open that chat from it (MergedChatRow).
        self._message(chat['id'], f'Merged into “{into_title}”', role='system', kind='status',
                      merged_into=dict(id=into['id'], title=into_title))
        self._refresh_bot(chat['id'])
        chat = self._get('bots', chat['id'])
        chat.update(archived=True, _merged_into=into['id'])
        self._publish_archive(chat)
        self._message_interaction = dict(merged_chat=dict(id=chat['id'], title=title),
                                         _merged_chat=dict(title=title, transcript=transcript))
        try:
            self._send(self._get('bots', into['id']), dict(text=f'Merged “{title}”', mode='auto'))
        finally:
            self._message_interaction = None
        return self._bot_view(chat, self._request_model_catalog)

    def _with_merges(self, history: list[dict], task: dict) -> list[dict]:
        """The history of a turn, with the transcript on each merge message of the turn (merge_chat). The history leaves
        out private fields, so the apps never get the transcript and only the agent reads it."""
        identities = task.get('_turn_message_ids') or []
        merges = {identity: json.loads(raw) for identity, raw in self.db.execute(
            "SELECT id, json_extract(data,'$._merged_chat') FROM messages WHERE id IN (SELECT value FROM json_each(?)) "
            "AND json_type(data,'$._merged_chat')='object'", (json.dumps(identities),))} if identities else {}
        if not merges:
            return history
        return [dict(message, _merged_chat=merges[message['id']]) if message['id'] in merges else message
                for message in history]

    def _delete_agent_chats(self, agent: dict, callbacks: list, *, audit: dict | None = None) -> None:
        """The chats of a removed agent go with it."""
        if agent.get('chat_of'):
            return
        for chat in self._agent_chats(agent['id']):
            self._delete_bot(chat, callbacks, audit=audit)
