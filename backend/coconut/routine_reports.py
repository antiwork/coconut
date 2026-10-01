"""Proof and transactional consumption for owner-created routine reports.

This is not a routine-wide writer exemption. The caller still checks every
branch, receipt, decision and native claim in the report and requester chains.
"""
import copy
from datetime import datetime
import json
from uuid import UUID, uuid5


def value(receipt):
    try:
        result = json.loads(receipt.get('result', ''))
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def kanban_completions(receipts):
    """The successful kanban_complete receipts with their parsed results. Each ended one native run."""
    return [(r, value(r)) for r in receipts
            if (r.get('action') or {}).get('name') == 'kanban_complete' and value(r).get('ok') is True]


def delivered(deliveries, source, target):
    return [d for d in deliveries if d.get('source_task_id') == source['id']
            and d.get('target_bot_id') == target['bot_id'] and not d.get('_progress_only')
            and d.get('text') and d.get('receipt', {}).get('task_id') == target['id']
            and d['receipt'].get('target_bot_id') == target['bot_id']
            and d['receipt'].get('thread_id') == target['thread_id']
            and d['receipt'].get('status') == 'queued']


def callback_proof(callback, assignment, tasks, deliveries):
    """Exact saved return, not a shared execution, URL, or message text alone."""
    from .work_capacity import execution_key
    producer = tasks.get(callback.get('source_task_id'))
    if (not producer or not callback.get('_is_return')
            or callback.get('_assignment_task_id') != assignment['id']
            or callback.get('_resolved_dependency_id') != producer['id']
            or callback.get('parent_task_id') != producer['id']
            or callback.get('source_bot_id') != producer['bot_id']
            or producer.get('source_bot_id') != assignment['bot_id']
            or (producer.get('_reply_to_task_id') or producer.get('source_task_id')) != assignment['id']
            or (execution_key(callback) != execution_key(assignment)
                and not (callback['status'] == 'queued' and not callback.get('_execution_session_id')
                         and not callback.get('_kanban_claim') and not callback.get('_native_outcome_id')
                         and not callback.get('_conversation_epoch')))
            or any(callback.get(k) != assignment.get(k) for k in
                   ('bot_id', 'session_id', 'thread_id', 'conversation_bot_id', 'exchange_id'))):
        return None
    for delivery in delivered(deliveries, producer, callback):
        if any(m.get('sender_bot_id') == producer['bot_id'] and not m.get('progress_only')
               and m.get('result_status') == 'completed' and m.get('text') == delivery['text']
               for m in callback.get('_bot_messages', [])):
            return delivery
    return None


def created_occurrences(service, owner, tasks):
    """All exact owner-created occurrences, including overwritten PR reporters."""
    from .service import digest, encoded
    creates = {}
    for receipt in service._native_task_receipts(owner['id'], state='completed'):
        action = receipt.get('action') or {}
        result = value(receipt)
        if (action.get('name') == 'cronjob_manage' and action.get('arguments', {}).get('action') == 'create'
                and receipt.get('turn_id') and receipt.get('action_hash') == digest(encoded(action))
                and result.get('success') is True and isinstance(result.get('job_id'), str)):
            creates.setdefault(result['job_id'], []).append(receipt)
    occurrences = {}
    for reporter in tasks.values():
        job = reporter.get('_native_job') or {}
        fire = (job.get('fire_claim') or {}).get('by')
        receipts = creates.get(job.get('id'), [])
        if (len(receipts) != 1 or not fire
                or any(reporter.get(k) != owner.get(k) for k in ('bot_id', 'session_id', '_conversation_epoch'))
                or reporter.get('routine_id') != str(uuid5(UUID(owner['bot_id']), 'cron:' + job['id']))):
            continue
        create = receipts[0]
        try:
            if not (datetime.fromisoformat(create['completed_at'].replace('Z', '+00:00'))
                    <= datetime.fromisoformat(reporter['created_at'].replace('Z', '+00:00'))):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        triggers = [json.loads(raw) for raw, in service.db.execute(
            "SELECT data FROM events WHERE type='routine.triggered' AND task_id=? AND bot_id=?",
            (reporter['id'], reporter['bot_id']))]
        if triggers == [dict(routine_id=reporter['routine_id'], task_id=reporter['id'])]:
            occurrences[reporter['id']] = create
    return occurrences


def scopes(service, owner, item, peers, tasks, deliveries):
    """Authenticate the immutable creation and relay before considering lifecycle."""
    from .service import digest, encoded
    result = {}
    requester = tasks.get(owner.get('source_task_id'))
    if not requester or requester['bot_id'] != owner.get('source_bot_id'):
        return result
    occurrences = created_occurrences(service, owner, tasks)
    for peer in peers:
        reporter = tasks.get(peer.get('_activity_task_id'))
        if (not reporter or peer['kind'] != 'pr' or peer['bot_id'] != owner['bot_id']
                or peer['task_id'] != owner['id'] or peer.get('_review_task_id') != owner['id']
                or not peer.get('url') or (item['kind'] == 'pr' and peer['id'] != item['id'])
                or (item['kind'] == 'work' and peer.get('work_outcome_id') != item['id'])
                or reporter.get('source_task_id') or reporter.get('source_bot_id')
                or reporter.get('_is_return') or reporter.get('_assignment_task_id')
                or reporter['status'] not in ('waiting', 'completed')
                or owner.get('_conversation_epoch') != service._setting('conversation_epoch')
                or reporter['id'] not in occurrences):
            continue
        job = reporter['_native_job']
        fire = job['fire_claim']['by']
        create = occurrences[reporter['id']]
        for coordinator in tasks.values():
            relay = coordinator.get('_native_relay') or {}
            envelope = relay.get('envelope_id')
            if (coordinator.get('source_task_id') != reporter['id']
                    or coordinator.get('source_bot_id') != reporter['bot_id']
                    or coordinator.get('_reply_to_task_id') != reporter['id']
                    or coordinator['bot_id'] != requester['bot_id'] or coordinator.get('_is_return')
                    or coordinator.get('session_id') != requester.get('session_id')
                    or coordinator.get('_conversation_epoch') != reporter.get('_conversation_epoch')
                    or any(coordinator.get(k) != reporter.get(k) for k in ('thread_id', 'conversation_bot_id', 'exchange_id'))
                    or not envelope or relay.get('sender_bot_id') != reporter['bot_id']):
                continue
            route = [d for d in delivered(deliveries, reporter, coordinator)
                     if d['id'] == digest(encoded([reporter['id'], envelope]))]
            if len(route) != 1:
                continue
            proof = dict(owner_id=owner['id'], reporter_id=reporter['id'], coordinator_id=coordinator['id'],
                         create_receipt_id=create['id'], create_turn_id=create['turn_id'],
                         create_result_hash=digest(create['result']), job_id=job['id'], occurrence=fire,
                         relay_delivery_id=route[0]['id'], sibling_reporter_ids=sorted(occurrences))
            # More than one requester relay is not an unambiguous report lineage.
            if (peer['id'], reporter['id']) in result:
                result[(peer['id'], reporter['id'])] = None
                break
            result[(peer['id'], reporter['id'])] = proof
    proved = [(peer_id, proof) for (peer_id, _), proof in result.items() if proof]
    for peer_id, proof in proved:
        coordinator = tasks[proof['coordinator_id']]
        owner_peer = next(peer for peer in peers if peer['id'] == peer_id)
        for peer in peers:
            activity = tasks.get(peer.get('_activity_task_id'))
            if (peer['kind'] == 'pr' and peer['bot_id'] == requester['bot_id']
                    and peer['task_id'] == requester['id'] and peer.get('_review_task_id') == requester['id']
                    and peer.get('url', '').lower() == owner_peer['url'].lower()
                    and activity and (activity['id'] == coordinator['id']
                        or callback_proof(activity, coordinator, tasks, deliveries))):
                result[(peer['id'], activity['id'])] = proof
    return result


def native_done(task, snapshots):
    claim = task.get('_kanban_claim') or {}
    card = snapshots.get(task['id'], {})
    return bool(claim.get('native_id') and claim.get('external_id') == task['id']
                and claim.get('run_id') and claim.get('input')
                and task.get('_native_outcome_id') in (None, claim['native_id'])
                and card.get('id') == claim['native_id']
                and (card.get('input_id') == claim['input']
                     or (not card.get('input_id') and not task.get('_native_outcome_id')))
                and card.get('status') == 'done' and not card.get('current_run_id') and not card.get('worker_pid'))


def foreground(service, caller, snapshots):
    if not caller:
        return False
    bot = service._get('bots', caller['bot_id'])
    claim = caller.get('_kanban_claim') or {}
    card = snapshots.get(caller['id'], {})
    return bool(caller['status'] == 'working' and caller.get('_native_turn_id')
                and caller.get('_conversation_epoch') == service._setting('conversation_epoch')
                and caller.get('session_id') == bot.get('session_id')
                and not bot.get('archived') and not bot.get('_deleted')
                and claim.get('native_id') and claim.get('run_id') and claim.get('input')
                and claim.get('external_id') == caller['id']
                and caller.get('_native_outcome_id') in (None, claim['native_id'])
                and card.get('id') == claim['native_id'] and card.get('status') == 'running'
                and (card.get('input_id') == claim['input']
                     or (not card.get('input_id') and not caller.get('_native_outcome_id')))
                and card.get('claim_source_status') != 'review'
                and card.get('current_run_id') == claim['run_id']
                and (not card.get('worker_pid') or card['worker_pid'] == claim.get('pid')))


def project(service, proof, tasks, snapshots, deliveries, caller):
    """Return a proposed exact consumption. No writes until *all* guards pass."""
    if service._kanban_claiming or service._kanban_dispatch_uncertain:
        # A captured queued callback can acquire a native claim outside this
        # transaction. Do not consume its input until dispatch is reconciled.
        return None
    reporter = tasks[proof['reporter_id']]
    coordinator = tasks[proof['coordinator_id']]
    requester = tasks[tasks[proof['owner_id']]['source_task_id']]
    if not native_done(reporter, snapshots) or coordinator['status'] != 'completed' or not native_done(coordinator, snapshots):
        return None
    # The reporter explicitly ended this occurrence, even if Coconut retained
    # its report wait. Claim.input can predate _native_turn_id on legacy inputs.
    if not any(result.get('task_id') == reporter['_kanban_claim']['native_id']
               and result.get('run_id') == reporter['_kanban_claim']['run_id']
               for _, result in kanban_completions(service._native_task_receipts(
                   reporter['id'], turn_id=reporter.get('_native_turn_id'), state='completed'))):
        return None
    producer = tasks.get(coordinator.get('_result_task_id'), coordinator)
    if (producer['status'] != 'completed' or not native_done(producer, snapshots)
            or (producer['id'] != coordinator['id']
                and not callback_proof(producer, coordinator, tasks, deliveries))):
        return None
    returns = [(t, callback_proof(t, reporter, tasks, deliveries)) for t in tasks.values()
               if t.get('source_task_id') == coordinator['id'] and t.get('_assignment_task_id') == reporter['id']]
    returns = [(t, d) for t, d in returns if d]
    if len(returns) != 1:
        return None
    callback, delivery = returns[0]
    # No worker/claim/input/tool is cancelled to consume a return. A completed
    # consumer may retain a terminal claim; an unstarted callback lacks an epoch.
    if (callback['status'] not in ('queued', 'completed')
            or (callback['status'] == 'queued' and (callback.get('_kanban_claim')
                or callback.get('_native_outcome_id') or callback.get('_conversation_epoch')
                or service._native_task_receipts(callback['id'])))
            or (callback['status'] == 'completed' and not native_done(callback, snapshots))
            or len(callback.get('_bot_messages', [])) != 1
            or delivery.get('created_at', '') < producer.get('updated_at', '')
            or callback.get('_native_relay')):
        return None
    changes = {reporter['id']: copy.deepcopy(reporter), callback['id']: copy.deepcopy(callback)}
    saved = dict(proof, delivery_id=delivery['id'], callback_id=callback['id'],
                 producer_id=producer['id'], producer_turn_id=producer.get('_native_turn_id'),
                 producer_native_id=producer['_kanban_claim']['native_id'],
                 producer_run_id=producer['_kanban_claim']['run_id'])
    changes[reporter['id']]['_routine_report_origin'] = proof
    changes[reporter['id']].setdefault('_routine_report_returns', {})[delivery['id']] = saved
    changes[reporter['id']]['_waiting_on'] = [i for i in reporter.get('_waiting_on', []) if i != coordinator['id']]
    if reporter['status'] == 'waiting' and not changes[reporter['id']]['_waiting_on']:
        changes[reporter['id']]['status'] = 'completed'
    changes[callback['id']].update(status='completed', _routine_report_consumption=saved)
    # Never suppress a nonterminal native card. The only exception is this
    # authenticated invocation, after joining its saved requester return route.
    current = None
    if foreground(service, caller, snapshots) and (
            caller['id'] == requester['id']
            or service._requester_callback_foreground(caller, requester, tasks, snapshots)
            or callback_proof(caller, coordinator, tasks, deliveries)
            or service._requester_callback_foreground(caller, coordinator, tasks, snapshots)):
        current = caller['id']
    return changes, current
