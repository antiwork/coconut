"""Derive outcome state from attempts, dependencies, and decisions."""
from .background_processes import subscription


class WorkModel:
    def _work_outcome(self, item, tasks, records, decisions, contexts, timestamp, peers=None, queue_positions=None):
        peers = peers or [item]
        linked = {}
        owner = tasks.get(item.get('task_id'), {})
        if item['kind'] == 'pr':
            review = self._pr_review_owner(peers)
            owner = tasks.get(review['_review_task_id'], {}) if review else {}
            identities = [owner.get('id')]
            identities += [p.get('_activity_task_id') for p in peers
                           if p.get('_activity_at', '') >= owner.get('created_at', '')
                           and p.get('_activity_head') == item.get('head_sha')]
        else:
            identities = [owner.get('id')]
        if item['kind'] == 'pr' and owner:
            replacements = [tasks[i] for i in identities if i in tasks and i != owner['id']
                            and tasks[i]['exchange_id'] != owner['exchange_id']
                            and tasks[i]['status'] in ('queued', 'working', 'waiting', 'needs_you')
                            and not self._get('exchanges', tasks[i]['exchange_id']).get('cancelled')
                            and self._work_chain(tasks[i], tasks, records)]
            if replacements:
                replacement = max(replacements, key=lambda t: (t['created_at'], t['id']))
                identities = [i for i in identities if i in tasks and tasks[i]['exchange_id'] == replacement['exchange_id']]
        for identity in identities:
            if identity in tasks and not self._get('exchanges', tasks[identity]['exchange_id']).get('cancelled'):
                linked.update(self._work_chain(tasks[identity], tasks, records, include_continuations=True))
        attempts = [t for t, _ in linked.values() if not t.get('_work_status_only')]
        identities = {t['id'] for t in attempts}
        pending = [d for d in decisions if d['status'] == 'pending' and (
            d['task_id'] in identities or (item['kind'] == 'pr' and d.get('merge_pr_url') == item.get('url')))]
        replies = [c for c in contexts if c['task_id'] in identities and c['status'] in ('queued', 'working')]
        dependencies = []
        current_records = []
        for record in records:
            task = tasks.get(record['task_id'])
            if not task or task['id'] not in identities or task['status'] == 'cancelled':
                continue
            if task['status'] in ('completed', 'failed') and not self._can_resume_completion(task, self._get('bots', task['bot_id']), record):
                continue
            context = next((c for c in contexts if c['id'] == record.get('turn_id')), None)
            if context:
                decision = next((d for d in decisions if d['id'] == context['decision_id']), {})
                if context['status'] not in ('working', 'completed', 'failed') or context.get('_conversation_epoch') != task.get('_conversation_epoch') or decision.get('status') != 'pending':
                    continue
            current_records.append(record)
        for record in current_records:
            if record['task_id'] not in identities or record.get('_process_delivery'):
                continue
            kind, target = next(((kind, record[key]) for kind, key in (
                ('task', '_relay_task_id'), ('task', '_wait_task_id'), ('process', '_wait_process_receipt'),
                ('decision', '_wait_decision_id')) if record.get(key)), (None, None))
            if subscription(record):
                kind, target = 'process', subscription(record)
            if not kind:
                continue
            ready = bool(record.get('_completion_text'))
            if any(record.get(k) for k in ('_wait_task_id', '_wait_process_receipt', '_wait_decision_id')):
                ready = self._wait_completion(record, tasks[record['task_id']]) is not None
            elif record.get('_relay_task_id'):
                ready = bool(tasks.get(target, {}).get('_native_relay_result'))
            dependencies.append(dict(id=record['id'], kind=kind, target_id=target,
                                     state='ready' if ready else 'waiting',
                                     reason=record.get('action', {}).get('arguments', {}).get('reason')))
        for task in attempts:
            if task['status'] != 'waiting':
                continue
            for target in task.get('_waiting_on', []):
                if target in tasks and not any(d['kind'] == 'task' and d['target_id'] == target for d in dependencies):
                    dependencies.append(dict(id=task['id'] + ':' + target, kind='task', target_id=target,
                        state='ready' if (tasks[target]['status'] in ('failed', 'cancelled') or
                            (tasks[target]['status'] == 'completed' and not self._background_pending(target)
                             and not tasks[target].get('_waiting_on'))) else 'waiting', reason=None))
        live = [t for t in attempts if t['status'] in ('working', 'waiting')
                and self._work_live(t, current_records, timestamp)]
        live_reply = any(c['status'] == 'working' and self.context_threads.get(c['id'])
                         and self.context_threads[c['id']].is_alive() for c in replies)
        context_processes = [r for r in current_records if any(c['id'] == r.get('turn_id') for c in contexts)]
        live_reply = live_reply or any(self._work_live(tasks[r['task_id']], [r], timestamp) for r in context_processes)
        ready_reply = any(r.get('_completion_text') and not r.get('_process_delivery') for r in context_processes)
        reports = [p for p in peers if p.get('_activity_task_id') in identities
                   and p.get('_activity_head') == item.get('head_sha')
                   and p.get('_activity_turn') == tasks[p['_activity_task_id']].get('_native_turn_id')]
        blockers = []
        for task in attempts:
            report = next((p for p in sorted(reports, key=lambda p: p.get('_activity_at', ''), reverse=True)
                           if p['_activity_task_id'] == task['id']), {})
            if task['status'] == 'failed' and any(r['task_id'] == task['id'] and subscription(r)
                                                    and not r.get('_process_delivery') for r in current_records):
                continue
            if task['status'] != 'failed' and report.get('_activity_state') != 'blocked':
                continue
            descendants = self._work_chain(task, tasks, records, include_continuations=True)
            if any(t['id'] != task['id'] and t['status'] in ('queued', 'working', 'waiting', 'needs_you')
                   for t, _ in descendants.values()):
                continue
            blockers.append((task, report.get('_activity') if report.get('_activity_state') == 'blocked' else None))
        queued = [t for t in attempts if t['status'] in ('queued', 'scheduled')]
        evidence = item.get('completion_evidence')
        if not evidence and item['status'] == 'completed' and not item.get('_automatic_work'):
            evidence = item.get('activity')
        if item['kind'] == 'pr' and item.get('pr_state') in ('merged', 'closed'):
            evidence = 'Pull request ' + item['pr_state'] + '.'
        if item.get('_automatic_work'):
            root = next((t for t in owner.get('_work_todos', {}).get('todos', []) if t['id'] == 'coconut-assignment'), None)
            if root and root['status'] == 'completed':
                evidence = owner.get('summary') or root['content']
        if self._setting('work.cancelled.' + self._work_identity(item)) or (item['kind'] == 'work' and (owner.get('status') == 'cancelled' or item['status'] == 'cancelled')):
            state, activity, next_step = 'cancelled', 'Stopped', None
        elif evidence and item['kind'] == 'pr':
            state, activity, next_step = 'completed', 'Completed', None
        elif evidence and not dependencies and not pending and not replies:
            state, activity, next_step = 'completed', 'Completed', None
        elif pending and not live_reply and not replies and not ready_reply and not live:
            state, activity, next_step = 'needs_decision', 'Waiting for your decision', 'Review decision'
        elif blockers and not live_reply and not replies:
            state, activity, next_step = 'needs_recovery', blockers[-1][1] or 'Needs recovery', 'Ask for progress'
        elif live or live_reply:
            state, activity, next_step = 'working', 'Checking the requested information' if live_reply else 'In progress', item.get('next_step')
        elif replies or ready_reply:
            state, activity, next_step = 'queued', 'Continuing with the result', 'Read the result and update this work'
        elif pending:
            state, activity, next_step = 'needs_decision', 'Waiting for your decision', 'Review decision'
        elif queued:
            state, activity, next_step = 'queued', 'Queued', item.get('next_step') or 'Continue the next step'
        elif any(d['state'] == 'ready' for d in dependencies):
            state, activity, next_step = 'queued', 'Continuing with the result', 'Read the result and update this work'
        elif dependencies:
            state, activity, next_step = 'waiting', next((d['reason'] for d in dependencies if d.get('reason')), 'Waiting for another bot' if any(d['kind'] == 'task' for d in dependencies) else 'Waiting for a result'), 'Continue when the result arrives'
        elif evidence:
            state, activity, next_step = 'completed', 'Completed', None
        else:
            label = item.get('activity') if item['kind'] == 'work' and item.get('status') in ('ready', 'blocked') else None
            state, activity, next_step = 'needs_recovery', label or 'Needs recovery', item.get('next_step') or 'Ask for progress'
        description_at = None
        if state == 'working' and not live_reply:
            reports = [p for p in peers if p.get('_activity_task_id') in {t['id'] for t in live}
                       and p.get('_activity_head') == item.get('head_sha')
                       and p.get('_activity_turn') == tasks[p['_activity_task_id']].get('_native_turn_id')]
            if reports:
                report = max(reports, key=lambda p: p.get('_activity_at', ''))
                activity, description_at = report['_activity'], report.get('_activity_at')
            elif owner in live and item.get('_work_turn') == owner.get('_native_turn_id') and not item.get('_automatic_work') and not item.get('_work_resuming'):
                activity, description_at = item['activity'], item.get('observed_at')
            else:
                description = next(((todo['content'], task.get('_work_progress_at', task['created_at']))
                                    for task in reversed(live) for todo in task.get('_work_todos', {}).get('todos', [])
                                    if todo.get('status') == 'in_progress' and todo.get('id') != 'coconut-assignment'
                                    and todo.get('content')), None)
                if description:
                    activity, description_at = description
        if state == 'working':
            # Process health cannot renew a model's old description of its work.
            from .worker_activity import FRESH_SECONDS
            processes = [r for r in current_records if subscription(r) and not r.get('_process_delivery')
                         and r.get('_process_status') == 'running'
                         and self._work_live(tasks[r['task_id']], [r], timestamp)]
            if processes:
                from .worker_activity import progress_label
                process = max(processes, key=lambda r: (r.get('_worker_progress', {}).get('at', 0), r.get('created_at', '')))
                activity = progress_label(process, timestamp)
            elif description_at:
                from datetime import datetime
                reported = datetime.fromisoformat(description_at.replace('Z', '+00:00'))
                if (timestamp - reported).total_seconds() > FRESH_SECONDS:
                    activity = 'Waiting for a progress update'
        actor = owner.get('bot_id') or item['bot_id']
        if state == 'needs_recovery' and blockers:
            actor = blockers[-1][0]['bot_id']
        elif state == 'needs_decision':
            actor = pending[0]['bot_id']
        elif live_reply:
            actor = next((c['bot_id'] for c in replies), actor)
        elif live:
            actor = live[-1]['bot_id']
        elif queued:
            actor = queued[-1]['bot_id']
        queue_position = None
        if state == 'queued' and queue_positions is not None:
            candidates = [task for task in queued if task['id'] in queue_positions]
            if candidates:
                next_task = min(candidates, key=lambda task: (queue_positions[task['id']], task['created_at'], task['id']))
                queue_position, actor = queue_positions[next_task['id']], next_task['bot_id']
        return dict(work_state=state, activity=activity, work_next_step=next_step,
                    work_decision_id=pending[0]['id'] if pending and state == 'needs_decision' else None,
                    work_queue_position=queue_position,
                    completion_condition=item.get('completion_condition') or item['title'], completion_evidence=evidence,
                    work_attempts=[dict(id=t['id'], state=t['status']) for t in attempts],
                    work_dependencies=dependencies,
                    activity_bot_id=actor,
                    activity_observed_at=timestamp.isoformat(),
                    activity_state={'needs_decision': 'waiting', 'queued': 'waiting', 'needs_recovery': 'blocked'}.get(state, state))

    def _project_work_outcomes(self, artifacts, tasks, records, decisions, timestamp):
        contexts = self._all('contexts')
        queue_positions = self._queue_positions(tasks, timestamp.timestamp())
        groups = {}
        for item in artifacts:
            if item['kind'] in ('pr', 'work'):
                identity = item['url'].lower() if item['kind'] == 'pr' else item['id']
                groups.setdefault(identity, []).append(item)
        for identity, peers in groups.items():
            latest = max(peers, key=lambda p: (p.get('observed_at') or p['created_at'], p.get('checked_at') or '', p['id']))
            outcome = self._work_outcome(latest, tasks, records, decisions, contexts, timestamp, peers, queue_positions)
            for item in peers:
                item.update(outcome)
                if item['kind'] == 'work':
                    item['status'] = outcome['activity_state']
                key = identity if item['kind'] == 'pr' else 'work:' + identity
                item['work_dismissal_id'] = self._setting('work.dismissed.' + key)
