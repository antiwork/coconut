"""Keep delegated work distinct from a completed Hermes reply."""
import copy

ROOT_ID = 'coconut-assignment'
WORK_INSTRUCTIONS = (
    'Track this assignment with the native todo_list tool. The coconut-assignment item represents the whole request. '
    'Keep that item until the assignment ends. Add steps for every requested outcome, including validation and authorized publishing. '
    'Track only this assignment\'s authorized scope. For a review or status request, complete the root after delivering the requested findings and limits. '
    'Do not extend that assignment to include the requester\'s later approval, publication, or merge unless they assigned those actions to you. '
    'Mark coconut-assignment completed with todo_list when all assigned outcomes are verified. '
    'Mark work completed only after verifying the outcome. A plan, status reply, or handoff is not completion. '
    'Keep blocked or delegated steps pending. Launch a real process or message_agent handoff before saying work is running or queued. '
    'Continue independent authorized work. Use coconut_ask if a human decision is required. '
    'Cancel an item only when the person cancels or changes its scope. Preserve all existing approval limits and skill requirements. '
    'Coconut retains unfinished items and continues the assignment after a reply ends. '
)


def snapshot(value):
    if not isinstance(value, dict) or not isinstance(value.get('todos'), list) or len(value['todos']) > 256:
        raise ValueError('Invalid native task list.')
    items = value['todos']
    if any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
           or not isinstance(item.get('content'), str) or len(item['content']) > 4000
           or item.get('status') not in ('pending', 'in_progress', 'completed', 'cancelled') for item in items):
        raise ValueError('Invalid native task item.')
    return {'todos': copy.deepcopy(items), 'revision': max(0, int(value.get('revision', 0)))}


class NativeWork:
    def __init__(self, request):
        task = request['task']
        self.enabled = request['operation'] != 'context' and not task.get('_pr_review') and bool(
            task.get('_work_todos') or (task.get('source_bot_id') and not task.get('_is_return'))
            or task.get('_handoff_assignment', {}).get('return_to_bot_id'))
        self.saved = task.get('_work_todos')
        self.request = (task.get('_handoff_assignment', {}).get('request')
                        or '\n'.join(m['text'] for m in task.get('_bot_messages', []) if m.get('text'))
                        or task['title'])[:4000]

    def restore(self, agent):
        if self.enabled:
            state = snapshot(self.saved) if self.saved else self.initial()
            agent._todo_store.restore(state['todos'], revision=state['revision'])

    def initial(self):
        return {'todos': [{'id': ROOT_ID, 'content': self.request, 'status': 'pending'}], 'revision': 1}

    def payload(self, agent):
        if not self.enabled or agent is None:
            return {}
        state = snapshot(agent._todo_store.snapshot())
        if not any(t['id'] == ROOT_ID for t in state['todos']):
            state['todos'] = self.initial()['todos'] + state['todos'][:255]
        return {'_work_todos': state}


class WorkProgress:
    @staticmethod
    def _unfinished_work(task):
        return any(t['status'] in ('pending', 'in_progress') for t in task.get('_work_todos', {}).get('todos', []))

    def _save_work_progress(self, task, payload):
        if '_work_todos' not in payload:
            return
        state = snapshot(payload['_work_todos'])
        assignment = self._assignment(task)
        task['_work_todos'] = state
        self._put('tasks', task)
        if assignment['id'] != task['id']:
            assignment['_work_todos'] = state
            self._put('tasks', assignment)

    def _continue_unfinished_work(self, task):
        from .service import now, uid
        state = task.get('_work_todos')
        if not state or task.get('_pr_review') or not self._unfinished_work(task):
            return None
        assignment = self._assignment(task)
        waiting_on = [identity for identity in assignment.get('_waiting_on', []) if identity != task.get('_resolved_dependency_id')]
        if waiting_on or self._background_pending(assignment['id'], finishing_task_id=task['id']):
            return None  # Existing durable dependencies own the next wakeup.
        attempts = task.get('_work_retry', 0)
        if attempts >= 2:
            return 'The assignment is unfinished. The bot ended repeated turns without completing it or recording a dependency.'
        epoch = task.get('_conversation_epoch')
        self._advance_input(task)
        task.update(status='queued', summary='Continuing unfinished work.', updated_at=now(),
                    _conversation_epoch=epoch, _work_retry=attempts + 1, _work_resume=True,
                    _background_return_after=now(), _background_return_id=uid())
        self._put('tasks', task)
        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
        self._refresh_bot(task['bot_id'])
        return 'queued'
