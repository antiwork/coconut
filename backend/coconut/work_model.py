"""Derive outcome state from attempts, dependencies, and decisions."""
from datetime import datetime
from .background_processes import subscription


class WorkModel:
    def _work_outcome(self, item, tasks, records, decisions, contexts, timestamp, peers=None, queue_positions=None):
        if item.get('_native_outcome_id'):
            return self._native_outcome_projection(item, tasks, records, decisions, timestamp, queue_positions, peers)
        peers = peers or [item]
        latest_prs = {p['url'].lower(): p for p in sorted((p for p in peers if p['kind'] == 'pr'), key=lambda p:
                 (p.get('observed_at') or p['created_at'], p.get('checked_at') or '', p['id']))}
        heads = {url: p.get('head_sha') for url, p in latest_prs.items()}
        linked = {}
        owner = tasks.get(item.get('task_id'), {})
        if item['kind'] == 'work':
            owner = tasks.get(owner.get('_assignment_task_id'), owner)
        if item['kind'] == 'pr':
            review = self._pr_review_owner(peers)
            owner = tasks.get(review['_review_task_id'], {}) if review else {}
            owner = self._work_execution_owner(owner, tasks)
            identities = [owner.get('id')]
            owner_chain = self._work_chain(owner, tasks, records, include_continuations=True) if owner else {}
            owner_active = bool(owner) and any(
                task['status'] == 'queued' or self._work_live(task, records, timestamp)
                for task, _ in owner_chain.values())
            identities += [p.get('_activity_task_id') for p in peers
                           if p.get('_activity_at', '') >= owner.get('created_at', '')
                           and p.get('_activity_head') == heads.get((p.get('url') or '').lower())
                           and not (owner_active and (tasks.get(p.get('_activity_task_id'), {}).get('_is_return')
                               or tasks.get(p.get('_activity_task_id'), {}).get('_source_audit_prompt')))]
        else:
            identities = list(dict.fromkeys(identity for peer in peers
                for identity in self._work_report_roots(peer, tasks, pr_heads=heads)))
        if item['kind'] == 'pr' and owner:
            replacements = [tasks[i] for i in identities if i in tasks and i != owner['id']
                            and tasks[i]['exchange_id'] != owner['exchange_id']
                            and tasks[i]['status'] in ('queued', 'working', 'waiting', 'needs_you')
                            and not self._get('exchanges', tasks[i]['exchange_id']).get('cancelled')
                            and self._work_chain(tasks[i], tasks, records)]
            if replacements:
                replacement = max(replacements, key=lambda t: (t['created_at'], t['id']))
                identities = [i for i in identities if i in tasks and tasks[i]['exchange_id'] == replacement['exchange_id']]
                # A newer status report does not replace an approved proposal's pending work.
                proposals = self._proposal_work_links(tasks)
                identities += [target for source in owner_chain for target in proposals.get(source, ())]
                identities += [t['id'] for t, _ in owner_chain.values()
                               if t.get('_execution_session_id') and t['status'] not in ('failed', 'cancelled')
                               and self._work_execution_owner(t, tasks)['id'] == owner['id']]
        for identity in identities:
            if identity in tasks and not self._get('exchanges', tasks[identity]['exchange_id']).get('cancelled'):
                linked.update(self._work_chain(tasks[identity], tasks, records, include_continuations=True))
        attempts = [t for t, _ in linked.values() if not t.get('_work_status_only')]
        identities = {t['id'] for t in attempts}
        pr_urls = {p.get('url') for p in peers if p['kind'] == 'pr'}
        pending = [d for d in decisions if d['status'] == 'pending' and (
            d['task_id'] in identities or d.get('merge_pr_url') in pr_urls)]
        replies = [c for c in contexts if c['task_id'] in identities and c['status'] in ('queued', 'working') and not c.get('_decision_refresh')]
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
                if context.get('_decision_refresh'):
                    continue
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
            for target in task.get('_waiting_on', []):
                if task['status'] != 'waiting' and target not in task.get('_explicit_work_waits', []):
                    continue
                if target in tasks and not any(d['kind'] == 'task' and d['target_id'] == target for d in dependencies):
                    dependencies.append(dict(id=task['id'] + ':' + target, kind='task', target_id=target,
                        state='ready' if (tasks[target]['status'] in ('failed', 'cancelled') or
                            (tasks[target]['status'] == 'completed' and not self._background_pending(target)
                             and not tasks[target].get('_waiting_on'))) else 'waiting', reason=None))
        waiting_decisions = {d['target_id'] for d in dependencies if d['kind'] == 'decision' and d['state'] == 'waiting'}
        pending_ids = {d['id'] for d in pending}
        pending += [d for d in decisions if d['id'] in waiting_decisions - pending_ids and d['status'] == 'pending']
        for report in peers:
            routine = self._external_work_routine(report) if report['kind'] == 'work' else None
            reporting_task = tasks.get(report.get('task_id'), {})
            if (routine and reporting_task.get('id') in identities and reporting_task.get('status') == 'completed'
                    and not any(r.get('state') == 'executing' and r['task_id'] in identities for r in records)):
                dependencies.append(dict(id=report['id'] + ':external', kind='routine', target_id=routine['id'],
                                         state='waiting', reason=report['activity']))
        live = [t for t in attempts if t['status'] in ('working', 'waiting')
                and self._work_live(t, current_records, timestamp)]
        live_reply = any(c['status'] == 'working' and self.context_threads.get(c['id'])
                         and self.context_threads[c['id']].is_alive() for c in replies)
        context_processes = [r for r in current_records if any(c['id'] == r.get('turn_id') for c in contexts)]
        live_reply = live_reply or any(self._work_live(tasks[r['task_id']], [r], timestamp) for r in context_processes)
        ready_reply = any(r.get('_completion_text') and not r.get('_process_delivery') for r in context_processes)
        reports = [p for p in peers if p.get('_activity_task_id') in identities
                   and p.get('_activity_head') == heads.get((p.get('url') or '').lower())
                   and p.get('_activity_turn') == tasks[p['_activity_task_id']].get('_native_turn_id')]
        blockers = []
        recovering = owner.get('_self_recovery', {})
        prior_failures = {t['id'] for t in recovering.get('saved_tasks', [])}
        recovery_turns = {t['id'] for t in attempts if t['id'] not in prior_failures
                          and (t['id'] == owner.get('id') or t.get('_assignment_task_id') == owner.get('id'))}
        recovery_receipts = {r['id'] for r in current_records if r['task_id'] in recovery_turns}
        if not (any(t['id'] in recovery_turns and t['status'] in ('queued', 'scheduled') for t in attempts)
                or any(t['id'] in recovery_turns for t in live)
                or any(d['id'] in recovery_receipts or d['id'].split(':', 1)[0] in recovery_turns for d in dependencies)):
            prior_failures = set()
        for task in attempts:
            if task['id'] in prior_failures:
                continue
            if task['status'] in ('queued', 'scheduled'):
                continue
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
        queued = [t for t in attempts if t['status'] in ('queued', 'scheduled') and not self._automatic_work_is_paused(t)]
        # A newer native turn can address an old failure. A background watcher cannot.
        resumed = queued + [t for t in live if self.running.get(t['id']) and self.running[t['id']].is_alive()]
        blockers = [(task, label) for task, label in blockers if task['status'] != 'failed' or label or not any(
            (current.get('updated_at') if current in queued else current.get('_turn_started_at', current['created_at']))
            > task.get('updated_at', task['created_at']) for current in resumed)]
        evidence = item.get('completion_evidence')
        if not evidence and item['status'] == 'completed' and not item.get('_automatic_work'):
            evidence = item.get('activity')
        if item['kind'] == 'pr' and item.get('pr_state') in ('merged', 'closed'):
            evidence = 'Pull request ' + item['pr_state'] + '.'
        if item.get('_automatic_work'):
            root = next((t for t in owner.get('_work_todos', {}).get('todos', []) if t['id'] == 'coconut-assignment'), None)
            if root and root['status'] == 'completed':
                evidence = owner.get('summary') or root['content']
        reporter = tasks.get(item.get('task_id'), {})
        report_roots = {tasks.get(p.get('task_id'), {}).get('_assignment_task_id', p.get('task_id')) for p in peers}
        report_tasks = {t['id'] for t in attempts if t['id'] in report_roots
                        or t.get('_assignment_task_id') in report_roots}
        explicit_waits = {target for identity in report_tasks for target in tasks[identity].get('_explicit_work_waits', [])}
        delegated_tasks = {identity for identity in report_tasks if identity != owner.get('id')
                           and tasks[identity].get('_assignment_task_id') != owner.get('id')}
        completion_blocked = item['kind'] == 'work' and (
            any(d['task_id'] in report_tasks for d in pending)
            or any(c['task_id'] in report_tasks for c in replies)
            or any(t['id'] in delegated_tasks for t in live + queued)
            or any(d['kind'] == 'task' and (d['target_id'] in explicit_waits
                or (d['target_id'] in report_tasks and (d['state'] != 'ready'
                    or tasks[d['target_id']]['status'] != 'completed'
                    or self._unfinished_work(tasks[d['target_id']])))) for d in dependencies)
            or any(r['task_id'] in report_tasks and (r.get('state') == 'executing'
                or ((subscription(r) or any(r.get(k) for k in ('_wait_process_receipt', '_wait_task_id', '_wait_decision_id')))
                    and not r.get('_process_delivery'))) for r in current_records))
        completed_report = (item['kind'] == 'work' and item['status'] == 'completed'
            and bool(item.get('completion_evidence')) and not item.get('_work_resuming')
            and all((not task.get('_work_todos') and not item.get('_automatic_work') and reporter.get('status') == 'completed')
                    or any(t.get('id') == 'coconut-assignment' and t.get('status') == 'completed'
                           for t in task.get('_work_todos', {}).get('todos', []))
                    for task in (reporter, owner)))
        # A delivered diagnosis ends its own scope even while the requester continues implementation.
        # Completing a review does not complete the PR or identify who owns the next step.
        latest_report = max(reports, key=lambda p: p.get('_activity_at', ''), default={})
        reviewed_pr = latest_prs.get((latest_report.get('url') or '').lower(), {})
        awaiting_pr = (attempts and all(t['status'] == 'completed' for t in attempts)
                       and ((reviewed_pr and reviewed_pr.get('pr_state') not in ('merged', 'closed')
                             and latest_report.get('_activity_state') == 'waiting')
                            or (item['kind'] == 'pr' and any(t['id'] == 'coconut-assignment' and t['status'] == 'completed'
                                   for t in owner.get('_work_todos', {}).get('todos', []))))
                       and not dependencies and not pending and not replies
                       and not any(self._has_uncertain_receipt(t['id'], t.get('_native_turn_id')) for t in attempts))
        pause = self._work_pause(item, decisions, peers)
        if self._setting('work.cancelled.' + self._work_identity(item)) or (item['kind'] == 'work' and ((owner.get('status') == 'cancelled' and not evidence) or item['status'] == 'cancelled')):
            state, activity, next_step = 'cancelled', 'Stopped', None
        elif evidence and item['kind'] == 'pr':
            state, activity, next_step = 'completed', 'Completed', None
        elif not completion_blocked and (completed_report or (evidence and not dependencies and not pending and not replies)):
            state, activity, next_step = 'completed', 'Completed', None
        elif pending and not live_reply and not replies and not ready_reply and not live:
            state, activity, next_step = 'needs_decision', 'Waiting for your decision', 'Review decision'
        elif pause and not live and not live_reply and not replies and not queued and not item.get('_work_resuming'):
            state, activity, next_step = 'waiting', 'Paused by your decision', item.get('next_step')
        elif awaiting_pr:
            state, activity, next_step = 'waiting', 'Review complete', 'Check remaining PR requirements'
        elif blockers and not live_reply and not replies:
            state, activity, next_step = 'needs_recovery', blockers[-1][1] or 'Needs recovery', 'Ask for progress'
        elif live or live_reply:
            state, activity, next_step = 'working', 'Checking the requested information' if live_reply else 'In progress', item.get('next_step')
        elif replies or ready_reply:
            state, activity, next_step = 'queued', 'Continuing with the result', 'Read the result and update this work'
        elif pending:
            state, activity, next_step = 'needs_decision', 'Waiting for your decision', 'Review decision'
        elif queued and all(t.get('_self_recovery') and t.get('scheduled_at')
                and datetime.fromisoformat(t['scheduled_at'].replace('Z', '+00:00')) > timestamp for t in queued):
            state, activity, next_step = 'waiting', 'Will retry automatically', 'Check saved results before continuing'
        elif queued:
            state, activity, next_step = 'queued', 'Queued', item.get('next_step') or 'Continue the next step'
        elif any(d['state'] == 'ready' for d in dependencies):
            state, activity, next_step = 'queued', 'Continuing with the result', 'Read the result and update this work'
        elif dependencies:
            state, activity, next_step = 'waiting', next((d['reason'] for d in dependencies if d.get('reason')), 'Waiting for another bot' if any(d['kind'] == 'task' for d in dependencies) else 'Waiting for a result'), 'Continue when the result arrives'
        elif evidence and not completion_blocked:
            state, activity, next_step = 'completed', 'Completed', None
        else:
            label = item.get('activity') if item['kind'] == 'work' and item.get('status') in ('ready', 'blocked') else None
            state, activity, next_step = 'needs_recovery', label or 'Needs recovery', item.get('next_step') or 'Ask for progress'
        description_at = None
        if state == 'working' and not live_reply:
            def live_report(report):
                identity = report.get('_work_report_task_id', report.get('task_id'))
                return (identity in {t['id'] for t in live}
                        and report.get('_work_report_turn', report.get('_work_turn')) == tasks[identity].get('_native_turn_id'))
            work_reports = [p for p in peers if p['kind'] == 'work' and live_report(p)
                            and p.get('status') != 'blocked'
                            and not p.get('_automatic_work') and not p.get('_work_resuming')]
            reports = [p for p in peers if p.get('_activity_task_id') in {t['id'] for t in live}
                       and p.get('_activity_head') == heads.get((p.get('url') or '').lower())
                       and p.get('_activity_turn') == tasks[p['_activity_task_id']].get('_native_turn_id')]
            reports = [p for p in reports if p.get('_activity_state') != 'blocked']
            if reports:
                report = max(reports, key=lambda p: p.get('_activity_at', ''))
                activity, description_at = report['_activity'], report.get('_activity_at')
            elif work_reports:
                report = max(work_reports, key=lambda p: p.get('observed_at', ''))
                activity, description_at = report['activity'], report.get('observed_at')
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
        previous = getattr(self, '_work_graphs', None)
        if previous is None:
            self._work_graphs = {}
        try:
            self._project_work_outcomes_inner(artifacts, tasks, records, decisions, timestamp)
        finally:
            self._work_graphs = previous

    def _project_work_outcomes_inner(self, artifacts, tasks, records, decisions, timestamp):
        contexts = self._all('contexts')
        queue_positions = self._queue_positions(tasks, timestamp.timestamp())
        groups = {}
        delegated = {}
        copies_by_outcome = {}
        for item in self._all('artifacts'):
            if item['kind'] in ('work', 'pr') and not item.get('_hidden'):
                copies_by_outcome.setdefault(self._work_identity(item), []).append(item)
            if item['kind'] == 'work' and item.get('_hidden') and item.get('_work_parent_id'):
                delegated.setdefault(item['_work_parent_id'], []).append(item)
        for item in artifacts:
            if item['kind'] in ('pr', 'work'):
                identity = self._work_identity(item)
                if item.get('work_outcome_id'):
                    item['work_outcome_id'] = self._work_outcome_artifact(item)['id']
                groups.setdefault(identity, []).append(item)
        for identity, peers in groups.items():
            latest = max(peers, key=lambda p: (p.get('observed_at') or p['created_at'], p.get('checked_at') or '', p['id']))
            if identity.startswith('work:'):
                latest = dict(self._work_outcome_artifact(latest))
            peer_ids = {p['id'] for p in peers}
            copies = [p for p in copies_by_outcome.get(identity, [])
                      if p['id'] not in peer_ids] if latest['kind'] == 'work' else []
            reports = peers + copies + (delegated.get(latest['id'], []) if latest['kind'] == 'work' else [])
            if latest['id'] not in {p['id'] for p in reports}:
                reports.append(latest)
            outcome = self._work_outcome(latest, tasks, records, decisions, contexts, timestamp, reports, queue_positions)
            for item in peers:
                item.update(outcome)
                if item['kind'] == 'work':
                    item['status'] = outcome['activity_state']
                key = self._work_identity(item)
                item['work_dismissal_id'] = self._setting('work.dismissed.' + key)
