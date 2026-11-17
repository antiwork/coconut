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
"""
from __future__ import annotations

# What a chat takes from its agent when the person starts it, and again when the agent changes.
SHARED = ('name', 'role')
# The fields that the person changes on one chat.
OWN = frozenset({'archived', 'pinned', 'notifications', 'chat_title'})
TITLE_LENGTH = 72


def chat_title(text: str) -> str:
    """The title of a chat from its first message: the first line with text, at most TITLE_LENGTH characters."""
    line = next((line.strip() for line in str(text).splitlines() if line.strip()), '')
    return line if len(line) <= TITLE_LENGTH else line[:TITLE_LENGTH - 1].rstrip() + '…'


class AgentChats:
    def _agent_id(self, bot_id: str) -> str:
        """The agent of a bot: the bot itself, or the agent of a chat."""
        return self._get('bots', bot_id).get('chat_of') or bot_id

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

    def _delete_agent_chats(self, agent: dict, callbacks: list, *, audit: dict | None = None) -> None:
        """The chats of a removed agent go with it."""
        if agent.get('chat_of'):
            return
        for chat in self._agent_chats(agent['id']):
            self._delete_bot(chat, callbacks, audit=audit)
