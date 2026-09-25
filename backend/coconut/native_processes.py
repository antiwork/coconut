"""Read subscribed Hermes process completions without starting a model turn."""
from __future__ import annotations

import json
import sys
if __package__:
    from .native_conversation import resume_session, session_root
else:
    from native_conversation import resume_session, session_root


def delegation_completion(item, *, session_id=None, resolve_session=None):
    from tools import async_delegation as native

    native.recover_abandoned_delegations()
    rows = [native.get_durable_delegation(identity) for identity in item['delegation_ids']]
    # Receipt IDs identify the task. Hermes stores their origin as the native conversation.
    def owns(row):
        if row is None:
            return False
        origin = row['origin_session']
        return (origin == item['task_id'] or bool(session_id and resolve_session
                and resolve_session(origin) == session_id))
    if not all(owns(row) for row in rows):
        return []
    if item.get('acknowledge'):
        for row in rows:
            native.mark_completion_delivered(row['delegation_id'])
        return [dict(id=item['id'], acknowledged=True)]
    if any(row['state'] in ('running', 'finalizing') for row in rows):
        return [dict(id=item['id'], observation='running')]
    if all(row['delivery_state'] == 'delivered' for row in rows):
        return [dict(id=item['id'], consumed=True)]
    # Coconut saves the result before acknowledging it on the next poll. A crash
    # between these steps cannot lose the result or queue a second continuation.
    text = ('Background delegation finished. Read the recorded outcomes below. '
            'For an unknown outcome, inspect existing evidence before repeating any external action.\n'
            + json.dumps([dict(delegation_id=row['delegation_id'], state=row['state'], result=row['result'])
                          for row in rows], ensure_ascii=False))
    return [dict(id=item['id'], text=text)]


def completions(request):
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_state import SessionDB
    from tools.process_registry import process_registry as registry
    from tools.process_registry_notifications import format_process_notification

    results = []
    db = SessionDB()
    try:
        for item in request['processes']:
            root = session_root(request['bot'], execution=item.get('execution_session_id'), epoch=item.get('epoch'))
            session_id = resume_session(db, root)
            if item.get('delegation_ids'):
                results.extend(delegation_completion(item, session_id=session_id,
                    resolve_session=db.resolve_resume_session_id))
                continue
            tokens = set_session_vars(session_id=session_id, session_key=item['task_id'])
            try:
                process = registry.get(item['session_id'])
                if process is None or (process.owner_task_id or process.task_id) != item['task_id']:
                    continue
                if not process.notify_on_complete:
                    continue
                status = registry.poll(process.id)
                if status.get('status') != 'exited':
                    if __package__:
                        from .worker_activity import read_progress
                    else:
                        from worker_activity import read_progress
                    results.append(dict(id=item['id'], session_id=process.id,
                                        observation=status.get('status'), progress=read_progress(item['id'])))
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
    by_session = {result['session_id']: result for result in results if 'session_id' in result and 'observation' not in result}
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
