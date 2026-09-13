"""Read subscribed Hermes process completions without starting a model turn."""
from __future__ import annotations

import json
import sys
from uuid import UUID, uuid5


def completions(request):
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_state import SessionDB
    from tools.process_registry import process_registry as registry
    from tools.process_registry_notifications import format_process_notification

    results = []
    db = SessionDB()
    try:
        for item in request['processes']:
            root = request['bot']['session_id']
            if item.get('epoch'):
                root = str(uuid5(UUID(root), item['epoch']))
            session_id = db.resolve_resume_session_id(root)
            tokens = set_session_vars(session_id=session_id, session_key=item['task_id'])
            try:
                process = registry.get(item['session_id'])
                if process is None or (process.owner_task_id or process.task_id) != item['task_id']:
                    continue
                if not process.notify_on_complete:
                    continue
                status = registry.poll(process.id)
                if status.get('status') != 'exited':
                    results.append(dict(id=item['id'], session_id=process.id, observation=status.get('status')))
                    continue
                consumed = registry.is_completion_consumed(process.id)
                event = dict(type='completion', session_id=process.id, owner_task_id=item['task_id'],
                             command=process.command, exit_code=status.get('exit_code'),
                             completion_reason=process.completion_reason, termination_source=process.termination_source,
                             output=status.get('output_preview', ''), started_at=process.started_at)
                results.append(dict(id=item['id'], session_id=process.id, consumed=consumed, text=format_process_notification(event)))
            finally:
                clear_session_vars(tokens)
    finally:
        db.close()
    by_session = {result['session_id']: result for result in results if 'observation' not in result}
    for event, text in registry.drain_notifications(
            owns_event=lambda event: event.get('type') == 'completion' and event.get('session_id') in by_session,
            skip_poll_observed=False):
        by_session[event['session_id']]['text'] = text
    return results


if __name__ == '__main__':
    request = json.loads(sys.stdin.read())
    output = sys.stdout
    sys.stdout = sys.stderr
    output.write(json.dumps(completions(request)) + '\n')
