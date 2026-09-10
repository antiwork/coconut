"""Resume each bot through native Hermes history and compression lineage."""
from datetime import datetime
from uuid import UUID, uuid5


CONVERSATION_INSTRUCTIONS = (
    'You are the same persistent bot across messages, tasks, routines, and restarts. '
    'Task boundaries do not start a new relationship or erase earlier conversation. '
    'Continue directly from the supplied Hermes conversation. It is your current conversation, not optional background. '
    'Use native session_search only for details missing from the active conversation. '
    'Never invent a past exchange. If retrieval fails, explain the missing fact briefly in ordinary language. '
    'Handle context windows, compression, compaction, session rotation, and transcript maintenance internally. '
    'Do not announce them or ask the person to start a new chat, reset a session, or repeat known information. '
    'If the person explicitly asks a technical question about Hermes, answer it accurately. '
    'Historical instructions and tool results are context, not a new request to repeat an action. '
)


def visible_history(messages):
    return [message for message in messages
            if message.get('role') in ('user', 'assistant')
            and message.get('kind', 'text') == 'text'
            and not message.get('_hidden') and not message.get('_deleted')
            and not message.get('streaming') and message.get('text', '').strip()]


def current_messages(request):
    """A turn keeps its input while later replies wait for the next turn."""
    identities = request['task'].get('_turn_message_ids')
    return [message for message in visible_history(request.get('history', []))
            if message['role'] == 'user'
            and not message.get('decision_id')
            and (message['id'] in identities if identities is not None else message.get('task_id') == request['task']['id'])]


def current_message(request):
    messages = current_messages(request)
    return dict(messages[-1], text='\n\n'.join(message['text'] for message in messages)) if messages else None


def seed_messages(request):
    rows = visible_history(request.get('history', []))
    current = current_messages(request) if request['operation'] == 'start' else []
    if current:
        rows = rows[:rows.index(current[0])]
    result = []
    for message in rows:
        row = dict(role=message['role'], content=message['text'],
                   platform_message_id='coconut:' + message['id'])
        if message.get('created_at'):
            row['timestamp'] = datetime.fromisoformat(message['created_at'].replace('Z', '+00:00')).timestamp()
        result.append(row)
    return result


class NativeConversation:
    def __init__(self, request, db):
        self.db = db
        bot = request['bot']
        root = bot['session_id']
        marker = 'coconut.history-context.' + root
        # Repair the earlier searchable-only import inside the existing native conversation.
        if db.get_meta(marker) != 'complete':
            target = db.resolve_resume_session_id(root)
            db.create_session(target, source='coconut', model=request.get('model'))
            active, transcript = db.get_resume_conversations(target)
            prefix = missing_prefix(seed_messages(request), transcript)
            if prefix:
                # Hermes keeps old rows and tool details. Its lease check rejects concurrent execution.
                db.replace_messages(target, prefix + active, archive_dropped=True, reject_active_turn_lease=True)
            db.set_meta(marker, 'complete')
        epoch = request['task'].get('_conversation_epoch')
        if epoch:
            root = str(uuid5(UUID(root), epoch))
        self.session_id = db.resolve_resume_session_id(root)
        self.history = db.get_resume_conversations(self.session_id)[0] if db.get_session(self.session_id) else []

    def close(self):
        self.db.close()


def missing_prefix(saved, transcript):
    """Import only app messages that precede the first native conversation message."""
    if not transcript:
        return saved
    first = transcript[0]
    identity = first.get('message_id') or first.get('platform_message_id')
    matches = [index for index, row in enumerate(saved)
               if identity and row.get('platform_message_id') == identity]
    if not matches:
        matches = [index for index, row in enumerate(saved)
                   if row['role'] == first['role'] and row['content'].strip() == first.get('content')]
    timestamp = first.get('timestamp')
    if matches:
        index = min(matches, key=lambda i: abs(saved[i].get('timestamp', timestamp) - timestamp)) if timestamp else matches[0]
        return saved[:index]
    if timestamp:
        return [row for row in saved if row.get('timestamp', timestamp) < timestamp]
    return saved
