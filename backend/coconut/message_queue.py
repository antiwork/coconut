"""The queue of a running task: the messages that the person sent while the agent works.

A message to a working task waits for the next safe turn (`_steering`, service._steer_task). The task shows these
messages as `queued_messages`, in the order of their delivery. Each item has the ID and the text of its message, the
IDs of its attachments, and a state: `queued`, or `steering` when the message already went to the running turn. The
apps show the queue above the message field and leave these messages out of the transcript until a turn takes them.

The person can change a queued message, remove it, and move it. Where the provider can give input to a running turn
(`steer_task`), the person can also send a text message into the turn at once. A message that is `steering` stays as
it is: the turn can have it already.

Routes, each with the task in its answer:
    PATCH  /v1/tasks/<task>/queue/<message>        {text}
    DELETE /v1/tasks/<task>/queue/<message>
    POST   /v1/tasks/<task>/queue/order            {message_ids}
    POST   /v1/tasks/<task>/queue/<message>/steer
"""
from __future__ import annotations

from .service_base import ServiceError, now, require_text

# A task with one of these states can hold messages for its next turn.
HOLDING = ('working', 'queued')
GONE = 'This message is not in the queue. A turn may have taken it.'


class MessageQueue:
    def _queue_blocks(self, task: dict) -> list[dict]:
        """The queued messages of a task, oldest first. Each block has the message, the position of its text in
        `_steering`, and the attachment messages that came with it."""
        waiting = list(enumerate(task.get('_steering') or []))
        if not waiting or task.get('status') not in HOLDING:
            return []
        taken = set(task.get('_turn_message_ids') or [])
        rows = [m for m in self._task_messages(task['id'])
                if m.get('role') == 'user' and not m.get('_hidden') and not m.get('_deleted')]
        blocks, files = [], []
        for message in reversed(rows):
            if message.get('kind') == 'artifact':
                files.append(message)
                continue
            if message.get('kind') != 'text' or message.get('decision_id'):
                continue
            # The text of another agent in `_steering` has no message of the person.
            if message['id'] == task.get('_input_after_message_id'):
                # The messages up to here went to earlier turns.
                break
            place = next((i for i in reversed(range(len(waiting))) if waiting[i][1] == message['text']), None)
            if place is None or message['id'] in taken:
                # The running turn took this message, or it is not of the queue. Its attachments are not either.
                files = []
                continue
            blocks.append(dict(message=message, index=waiting.pop(place)[0], files=list(reversed(files))))
            files = []
            if not waiting:
                break
        return list(reversed(blocks))

    def _queued_messages(self, task: dict) -> list[dict]:
        steers = getattr(self.provider, 'steer_task', None) is not None
        live = task.get('_live_messages') or {}
        return [dict(id=block['message']['id'], text=block['message']['text'], created_at=block['message']['created_at'],
                     attachment_ids=[m['artifact_id'] for m in block['files'] if m.get('artifact_id')],
                     state='steering' if steers and block['message']['id'] in live else 'queued',
                     can_steer=steers and task['status'] == 'working' and not block['files']
                     and block['message']['id'] not in live)
                for block in self._queue_blocks(task)]

    def _show_queue(self, task: dict) -> None:
        """Set `queued_messages` of a task before a write. A task without a queue has no such field."""
        queue = self._queued_messages(task) if task.get('_steering') or task.get('queued_messages') else []
        if queue:
            task['queued_messages'] = queue
        else:
            task.pop('queued_messages', None)

    def _queue_route(self, method: str, parts: list[str], data: dict) -> dict:
        """A change of the queue of the task `parts[2]`. The caller holds the transaction."""
        task = self._get('tasks', parts[2])
        blocks = self._queue_blocks(task)
        if parts[4:] == ['order'] and method == 'POST':
            return self._order_queue(task, blocks, data)
        block = next((b for b in blocks if b['message']['id'] == parts[4]), None) if len(parts) >= 5 else None
        if block is None:
            raise ServiceError(409, GONE)
        steers = getattr(self.provider, 'steer_task', None) is not None
        identity = block['message']['id']
        if steers and identity in (task.get('_live_messages') or {}):
            raise ServiceError(409, 'This message already went to the running turn.')
        if len(parts) == 5 and method == 'PATCH':
            text = require_text(data, 'text', 20000)
            message = block['message']
            message['text'] = text
            self._put('messages', message)
            self._event('message.updated', self._public(message), message['bot_id'], task['id'])
            task['_steering'][block['index']] = text
            if identity in (task.get('_live_messages') or {}):
                task['_live_messages'][identity] = text
        elif len(parts) == 5 and method == 'DELETE':
            del task['_steering'][block['index']]
            (task.get('_live_messages') or {}).pop(identity, None)
            if identity in task.get('_preference_source_ids', []):
                task['_preference_source_ids'].remove(identity)
            removed = {m['artifact_id'] for m in block['files']}
            task['_attachment_ids'] = [a for a in task.get('_attachment_ids', []) if a not in removed]
            for message in [block['message']] + block['files']:
                message['_deleted'] = True
                self._put('messages', message)
                self._event('message.updated', self._public(message) | {'_deleted': True}, message['bot_id'], task['id'])
            if not task['_steering']:
                task['steering_status'] = None
        elif parts[5:] == ['steer'] and method == 'POST':
            if not steers or task['status'] != 'working':
                raise ServiceError(409, 'This agent takes a new message at its next turn.')
            if block['files']:
                raise ServiceError(409, 'A message with attachments waits for the next turn.')
            task.setdefault('_live_messages', {})[identity] = block['message']['text']
            # Service.mutate gives the live messages of a task with this status to the running turn.
            task['steering_status'] = 'next_safe_turn'
        else:
            raise ServiceError(404, 'Not found.')
        return self._queue_changed(task)

    def _order_queue(self, task: dict, blocks: list[dict], data: dict) -> dict:
        wanted = data.get('message_ids')
        by_id = {block['message']['id']: block for block in blocks}
        if (not isinstance(wanted, list) or len(wanted) != len(by_id) or set(map(str, wanted)) != set(by_id)
                or not all(isinstance(identity, str) for identity in wanted)):
            raise ServiceError(409, 'The queue changed. Order the messages that it shows now.')
        ordered = [by_id[identity] for identity in wanted]
        if ordered == blocks:
            return self._queue_changed(task)
        # A turn reads its messages in the order of the rows, and the apps sort by the time. The messages go in again
        # in the new order, and they take the times of the queue in that order.
        times = sorted(m['created_at'] for block in blocks for m in [block['message']] + block['files'])
        texts = [task['_steering'][block['index']] for block in ordered]
        for place, text in zip(sorted(block['index'] for block in blocks), texts):
            task['_steering'][place] = text
        for message in [m for block in ordered for m in [block['message']] + block['files']]:
            message['created_at'] = times.pop(0)
            self.db.execute('DELETE FROM messages WHERE id=?', (message['id'],))
            self._put('messages', message)
            self._event('message.updated', self._public(message), message['bot_id'], task['id'])
        return self._queue_changed(task)

    def _queue_changed(self, task: dict) -> dict:
        task['updated_at'] = now()
        self._put('tasks', task)
        owner = task['bot_id'] if task.get('source_bot_id') else task['conversation_bot_id']
        self._event('task.updated', self._public(task), owner, task['id'])
        return self._public(task)
