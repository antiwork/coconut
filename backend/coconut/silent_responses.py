"""Keep the bot's silence control out of messages shown to the user."""


def is_silent_response(text):
    return isinstance(text, str) and text.strip() == '[SILENT]'


# The silence markers of Hermes (gateway/response_filters.py, LIVE_GATEWAY_SILENT_MARKERS).
SILENCE_MARKERS = ('[SILENT]', 'SILENT', 'NO_REPLY', 'NO REPLY')


def is_silence_reply(text):
    """Whether the reply of a turn that may stay silent is silence: empty, or a silence marker as the whole reply or as
    its first or last line. Hermes uses this rule for cron runs (cron.scheduler._is_cron_silence_response)."""
    if not isinstance(text, str) or not text.strip():
        return True
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return any(line.upper() in SILENCE_MARKERS for line in {lines[0], lines[-1]})


def is_silent_message(message):
    return (message.get('kind', 'text') in ('text', 'bot_exchange')
            and (message.get('role') == 'assistant' or message.get('sender_bot_id'))
            and not message.get('decision_id') and not message.get('artifact_id')
            and is_silent_response(message.get('text')))


class SilentResponses:
    def _migrate_silent_responses(self):
        if self._setting('silent_responses_migrated'):
            return
        messages = self._all('messages')
        for message in messages:
            if not message.get('_hidden') and is_silent_message(message):
                message['_hidden'] = True
                self._put('messages', message)
                self._event('message.updated', dict(self._public(message), _hidden=True), message['bot_id'], message.get('task_id'))
        for bot in self._all('bots'):
            if is_silent_response(bot.get('last_message')):
                latest = next((m for m in reversed(messages) if not m.get('_hidden') and not m.get('_deleted')
                               and not m.get('reaction_event') and (m['bot_id'] == bot['id'] or bot['id'] in m.get('recipient_bot_ids', []))), None)
                bot['last_message'] = latest['text'][:160] if latest else ''
                self._put('bots', bot)
        self.db.execute("INSERT INTO settings VALUES ('silent_responses_migrated','true')")
