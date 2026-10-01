"""Continue PR review through the bot that owns its work."""
import json
import threading

PR_MERGE_INSTRUCTIONS = (
    'Verify the current PR head, required checks, independent reviews, unresolved findings, conflicts, and required approvals before merging. '
    'Use the original human request and standing instructions in your native profile to determine merge permission. '
    'Standing approval applies only to its stated repository, changes, and conditions. Green CI alone does not grant permission. '
    'If that approval covers the current changes and all conditions pass, mark the draft ready and merge without another card. '
    'Preserve explicit human holds, rejected decisions, visual approval requirements, and native tool approval checks. '
    'While required checks run, finish the local verification the merge decision needs, such as tests that CI does not select, before waiting when possible. '
    'Before a merge decision or authorized merge, complete the required local verification for the current head, even if first resumed after CI passes. '
    'Reuse finished verification whose inputs are unchanged; perform any missing or invalidated checks within the approved scope. '
    'Once all required verification passes, follow the merge permission rules without starting unrelated local work or repeating unchanged verified work. '
    'If merge permission is missing, ask once. Offer one coconut_ask merge decision yourself when the evidence supports it. '
    'Coconut preserves the recorded conversation route. Do not wait for the requesting bot to present the card. '
    'Reuse an existing pending merge decision for this PR. Do not bypass its unanswered choice or duplicate its action. '
    'Recommend Merge when justified and include a Leave open option. Identify the PR and merge scope in each choice. '
    'Set merge_pr_url to the tracked PR URL so an external merge retires the card. '
    'Before an authorized merge, recheck the exact head and use a merge operation that rejects a changed head. '
    'Merge permission does not expand deployment or other external-action permission. '
)

PR_REVIEW_INSTRUCTIONS = (
    'Coconut observed new check or review results for a tracked pull request. This is a request to assess merge readiness, not permission to merge. '
    'Read the current PR head, required checks, reviews, unresolved findings, merge conflicts, and the original human instructions. '
    'Do not repeat finished implementation work. Respect any visual approval or other pending decision. '
    'A draft can be ready for a decision when its owner reports the current head ready. '
    'If the PR remains a draft and needs a merge decision, include marking it ready in the proposed action. '
    'If the PR head changed, checks are incomplete, or a requirement remains unmet, do not claim it is ready. '
    'Distinguish required checks from optional checks. A pending optional deployment must not silently block assessment. '
    'If required_checks_state is unverified, a required status passed but its expected GitHub App is unconfirmed. Verify its source before recommending merge. '
    'Read current-head Greptile findings, not only its successful check status. Resolve findings within the existing approval and verify the result. '
    'If CI failed, inspect the failure and continue the authorized fix. Do not ask permission for a routine retry or review. '
    'If an external requirement remains, record its specific reason on the existing task. '
    'Coconut monitors this PR without model calls and resumes you when results change. Do not start gh pr checks --watch. '
    'For a merged or closed PR, verify its current GitHub state and the original accepted scope. '
    'Verify whether the accepted outcome is complete and record evidence on the existing native task. '
    'A closed PR does not prove the accepted outcome is complete. Preserve unfinished work and human stops. '
    'Do not merge, reopen, publish, or deploy in response to a merged or closed observation. '
)


class PullRequestReviews:
    def _merge_decision_url(self, payload, task):
        from .pr_tracking import parse_pr_url, PRTrackingError
        from .service import ServiceError
        value = payload.get('merge_pr_url')
        if value is None:
            return None
        try:
            owner, repo, number = parse_pr_url(value)
        except PRTrackingError as error:
            raise ServiceError(400, str(error)) from error
        url = f'https://github.com/{owner}/{repo}/pull/{number}'
        if not any(a['kind'] == 'pr' and a['bot_id'] == task['bot_id']
                   and a['url'].lower() == url.lower() for a in self._all('artifacts')):
            raise ServiceError(400, 'Track this PR with coconut_track_pr before creating its merge decision.')
        return url

    def _merge_decision_head(self, url, task):
        return next((a.get('head_sha') for a in self._all('artifacts')
                     if url and a['kind'] == 'pr' and a['bot_id'] == task['bot_id']
                     and a['url'].lower() == url.lower()), None)

    def _validate_merge_decision(self, decision, task):
        from .service import ServiceError
        url = decision.get('merge_pr_url')
        if not url:
            return
        requested_at = decision.get('_merge_request_at') or ''
        if decision.get('request_message_id'):
            source = self._get('messages', decision['request_message_id'])
            if (source.get('_human_device_id') and source.get('_human_source_kind') == 'chat'
                    and not source.get('reaction_event')):
                requested_at = max(requested_at, source['created_at'])
        if decision.get('previous_decision_id'):
            previous = self._get('decisions', decision['previous_decision_id'])
            if (previous.get('merge_pr_url') or '').lower() == url.lower():
                requested_at = max(requested_at, previous.get('_merge_request_at') or '')
                if previous['status'] in ('replied', 'modified'):
                    requested_at = max(requested_at, previous.get('decided_at') or '')
        for identity in task.get('_preference_source_ids', []):
            try:
                source = self._human_source(task, identity, task, 'reconsider a merge')
            except ServiceError:
                continue
            requested_at = max(requested_at, source['created_at'])
        for existing in self._all('decisions'):
            if (existing['id'] == decision['id'] or not existing.get('merge_pr_url')
                    or existing['merge_pr_url'].lower() != url.lower()):
                continue
            if existing['status'] == 'pending':
                owner = self._get('tasks', existing['task_id'])
                if (owner['status'] == 'cancelled' or any(self._get('bots', identity).get('_deleted')
                        for identity in (owner['bot_id'], owner['conversation_bot_id']))):
                    continue
            same_head = bool(decision.get('_merge_pr_head')
                             and decision['_merge_pr_head'] == existing.get('_merge_pr_head'))
            recorded_at = existing.get('decided_at') or existing.get('_inbox_dismissed_at')
            if not recorded_at and requested_at and existing['status'] == 'dismissed':
                dismissal = self.db.execute("SELECT created_at FROM events WHERE type='decision.updated' "
                    "AND task_id=? AND json_extract(data, '$.id')=? AND json_extract(data, '$.status')='dismissed' "
                    "AND json_extract(data, '$.inbox_swipe_id') IS NOT NULL ORDER BY seq DESC LIMIT 1",
                    (existing['task_id'], existing['id'])).fetchone()
                recorded_at = dismissal[0] if dismissal else existing['created_at']
            new_request = bool(recorded_at and requested_at > recorded_at)
            if (existing['status'] == 'pending' or (same_head and not new_request
                    and existing['status'] in ('approved', 'executed', 'rejected', 'dismissed'))):
                raise ServiceError(400, f"Merge decision {existing['id']} already exists ({existing['status']}). "
                    f"Task {existing['task_id']} owns its answer and follow-through. "
                    'Do not create another card or repeat its action. Reuse the recorded decision and its owner.')
        tasks = {t['id']: t for t in self._all('tasks')}
        records = self._work_receipts()
        linked = {}
        for artifact in self._all('artifacts'):
            if artifact['kind'] == 'pr' and artifact.get('url', '').lower() == url.lower():
                owner = tasks.get(artifact.get('_review_task_id'))
                if owner:
                    linked.update(self._work_chain(owner, tasks, records, include_continuations=True))
        # The request to offer this card cannot require the card's own answer first.
        decision_tasks = {task['id'], self._assignment(task)['id']}
        inert = self._pr_inert_waits(url, tasks, records, decision_tasks)
        for owner, _ in linked.values():
            if (owner['status'] == 'cancelled' or self._assignment(owner)['status'] == 'cancelled'
                    or self._get('exchanges', owner['exchange_id']).get('cancelled')):
                continue
            resolved = task.get('_resolved_dependency_id') if task.get('_assignment_task_id') == owner['id'] else None
            waiting = [identity for identity in owner.get('_waiting_on', [])
                       if identity != resolved and identity not in decision_tasks]
            if self._background_pending(owner['id'], finishing_task_id=task['id'],
                                        excluding_task_ids=decision_tasks, ignored_receipt_ids=inert) or waiting:
                raise ServiceError(400, 'The PR review still has a pending worker or delegated result. '
                    'Read its result before offering a merge decision.')
        return requested_at

    def _pr_inert_waits(self, url, tasks, records, keep):
        """The undelivered explicit waits that cannot hold a result of this pull request.

        A shared intake root links the work of every PR it receives, and a person's decision can keep a task from
        reading a finished wait. Only that wait is skipped, never its owner: any other receipt, process, relay,
        queued answer, or wait of the owner still gates. A wait qualifies only when all of these hold:
        - it is a completed coconut_wait on a task, and its target finished with the saved result;
        - its owner needs a person and has its own pending decision, so nothing can deliver the result now;
        - no record ties the owner, or the target and its work chain, or their executions to this PR: no PR record,
          claim, current review, or merge card;
        - the target is not work that serves this PR. That work starts at every task tied to this PR, except a
          human-origin intake that also holds provider-observed records of other PRs and has no recorded review of
          this PR. A recorded review is a current `_pr_review`, or reviewed heads saved on the PR record of the
          task's bot when the task owns that record or the record's owner has moved by a recorded transfer. The work
          includes everything delegated from there, and every task that its saved waits name, so the owner is in it
          whenever the target is. A worker's own execution, claims, or lineage never leave it;
        - every PR that does tie the owner is a provider-observed record of its bot, and one such record was
          created by the owner's own recorded lineage. A claim alone, or the bot tracking that PR, is not enough;
        - the target is the owner's own delegation, or has provider-observed PR records of its own.
        """
        candidates = [r for r in records if r.get('_wait_task_id') and not r.get('_process_delivery')
                      and r.get('state') == 'completed' and (r.get('action') or {}).get('name') == 'coconut_wait'
                      and not (r.get('_relay_task_id') or r.get('_wait_process_receipt') or r.get('_wait_decision_id'))
                      and r['task_id'] not in keep and r['task_id'] in tasks and r['_wait_task_id'] in tasks]
        if not candidates:
            return set()
        url = url.lower()
        artifacts = [a for a in self._all('artifacts') if a['kind'] == 'pr' and a.get('url')]
        observed = {(a['bot_id'], a['url'].lower()) for a in artifacts if a.get('observed_at')}
        decisions = self._all('decisions')
        executions = {identity: self._work_execution_owner(task, tasks)['id'] for identity, task in tasks.items()}
        def named(task_ids):
            """The PR URLs that records tie to these tasks."""
            found = set()
            for artifact in artifacts:
                if task_ids.intersection(artifact.get(f) for f in ('task_id', '_review_task_id', '_activity_task_id')):
                    found.add(artifact['url'].lower())
            for identity in task_ids:
                task = tasks[identity]
                for value in [*task.get('_reported_pr_urls', []), (task.get('_pr_review') or {}).get('url')]:
                    if value:
                        found.add(value.lower())
            found.update(d['merge_pr_url'].lower() for d in decisions
                         if d['task_id'] in task_ids and d.get('merge_pr_url'))
            return found
        def family(task):
            return {identity for identity, owner in executions.items() if owner == executions[task['id']]} | {task['id']}
        def lineage(task):
            seen, pending = set(), [task['id']]
            while pending:
                identity = pending.pop()
                if identity in seen or identity not in tasks:
                    continue
                seen.add(identity)
                pending.extend(tasks[identity].get(f) for f in ('source_task_id', 'parent_task_id', '_execution_session_id'))
            return seen
        target = set()
        for artifact in artifacts:
            if artifact['url'].lower() == url:
                target.update(artifact.get(f) for f in ('task_id', '_review_task_id', '_activity_task_id'))
        target.update(identity for identity, task in tasks.items()
                      if url in (value.lower() for value in task.get('_reported_pr_urls', []))
                      or ((task.get('_pr_review') or {}).get('url') or '').lower() == url)
        target.update(d['task_id'] for d in decisions if (d.get('merge_pr_url') or '').lower() == url)
        target_executions = {executions[identity] for identity in target if identity in executions}
        def tied(task):
            return task['id'] in target or executions[task['id']] in target_executions
        def reviewing(identity):
            # The monitor records a review of this PR on the review owner: its current request, or the heads it saved
            # on the owner's PR record. The request clears at the next turn. A transfer or a continuation moves the
            # owner and records the move, but the record keeps its heads and forgets every earlier owner, so after a
            # recorded move each task of the record's bot that tracks this PR may have been one of them.
            task = tasks[identity]
            return (((task.get('_pr_review') or {}).get('url') or '').lower() == url
                    or any(a['url'].lower() == url and a['bot_id'] == task['bot_id'] and self._pr_reviewed_heads(a)
                           and (a.get('_review_task_id') == identity or a.get('_review_ownership_at'))
                           for a in artifacts))
        def shared_intake(identity):
            # Only a task that a person started, that holds records of other PRs, and that no review of this PR
            # names serves several PRs at once. A recorded review of this PR makes it target work.
            task = tasks[identity]
            others = named({identity}) - {url}
            return (not task.get('source_task_id') and not task.get('parent_task_id') and bool(others)
                    and all((task['bot_id'], value) in observed for value in others) and not reviewing(identity))
        scope = {}
        for seed in sorted(identity for identity in target if identity in tasks and not shared_intake(identity)):
            if seed not in scope:
                scope.update(self._work_chain(tasks[seed], tasks, records, include_continuations=True))
        inert = set()
        for record in candidates:
            owner, source = tasks[record['task_id']], tasks[record['_wait_task_id']]
            if (owner['status'] != 'needs_you' or source['status'] != 'completed' or tied(owner)
                    or source['id'] in scope
                    or not self._wait_settled(record)
                    or not any(d['task_id'] == owner['id'] and d['status'] == 'pending' for d in decisions)):
                continue
            chain = self._work_chain(source, tasks, records, include_continuations=True)
            if any(tied(member) for member, _ in chain.values()):
                continue
            claims = named(family(owner))
            ancestry = lineage(owner)
            if (not claims or any((owner['bot_id'], value) not in observed for value in claims)
                    or not any(a['bot_id'] == owner['bot_id'] and a.get('observed_at') and a['url'].lower() in claims
                               and a.get('task_id') in ancestry for a in artifacts)):
                continue
            # The target must be the owner's own delegation or carry provider-observed records of its own.
            own = source.get('source_task_id') == owner['id'] and source.get('_requester_reply_task_id') == owner['id']
            claims = named(family(source))
            if (any((source['bot_id'], value) not in observed for value in claims)
                    or not (claims or own)):
                continue
            inert.add(record['id'])
        return inert

    def _resolve_merged_pr_decisions(self):
        from .service import now
        with self.transaction():
            merged = {(a['bot_id'], a['url'].lower()) for a in self._all('artifacts')
                      if a['kind'] == 'pr' and a.get('pr_state') == 'merged'
                      and a.get('observed_at') and not a.get('refresh_error')}
            for decision in self._all('decisions'):
                url = decision.get('merge_pr_url')
                if (not url or decision['status'] != 'pending' or decision['action_type'] != 'question'
                        or (decision['bot_id'], url.lower()) not in merged):
                    continue
                task = self._get('tasks', decision['task_id'])
                response = ('This PR has already merged. Coconut closed the merge question after verifying GitHub. '
                            'The person did not select an option. Do not merge again or treat this as approval for other work.')
                decision.update(status='resolved', outcome='Already merged.', resolved_at=now(),
                                revision=decision['revision'] + 1)
                self._put('decisions', decision)
                if decision.get('_receipt_id'):
                    record = self._get('native_receipts', decision['_receipt_id'])
                    if record['state'] == 'awaiting_answer':
                        record.update(state='completed', completed_at=now(),
                                      result=json.dumps({'response': response, 'resolution': 'merged', 'url': url}))
                        self._put('native_receipts', record)
                self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
                if decision.get('inbox_kind') != 'proposal' and task['status'] == 'needs_you':
                    task.update(status='queued', _resume_decision=decision['id'], updated_at=now())
                    self._put('tasks', task)
                    self._event('task.updated', self._public(task), task['bot_id'], task['id'])
                    self._refresh_bot(task['bot_id'])

    def _migrate_pr_review_sources(self):
        for artifact in self._all('artifacts'):
            if artifact['kind'] != 'pr' or '_review_task_id' in artifact or not artifact.get('task_id'):
                continue
            task = self._pr_review_source(artifact['bot_id'], artifact['task_id'])
            artifact['_review_task_id'] = task['id']
            self._put('artifacts', artifact)

    def _pr_review_source(self, bot_id, task_id, *, tasks=None):
        if not task_id:
            return None
        from .service import ServiceError
        if not isinstance(task_id, str):
            raise ServiceError(400, 'Choose the task that owns this pull request.')
        task = tasks[task_id] if tasks is not None else self._get('tasks', task_id)
        if task['bot_id'] != bot_id:
            raise ServiceError(400, 'The pull request and its task must belong to the same bot.')
        if tasks is None:
            tasks = {t['id']: t for t in self._all('tasks')}
        owner = self._work_execution_owner(task, tasks)
        return owner if owner.get('_native_outcome_id') else task

    def _pr_review_source_current(self, artifact):
        if self._setting('work.cancelled.' + self._work_identity(artifact)) or not artifact.get('_review_task_id'):
            return False
        task = self._get('tasks', artifact['_review_task_id'])
        bot = self._get('bots', task['bot_id'])
        origin = self._get('bots', task['conversation_bot_id'])
        assignment = self._assignment(task)
        # A pending decision or archived bot keeps ownership. A reset or deletion ends it.
        return (not bot.get('_deleted') and not origin.get('_deleted')
                and task['status'] != 'cancelled' and assignment['status'] != 'cancelled'
                and not self._get('exchanges', task['exchange_id']).get('cancelled')
                and task['session_id'] == bot['session_id']
                and task.get('_conversation_epoch') == self._setting('conversation_epoch'))

    def _pr_report_superseded(self, artifact, task):
        for peer in self._all('artifacts'):
            if peer['kind'] == 'pr' and peer.get('url', '').lower() == artifact['url'].lower() and peer.get('_review_task_id'):
                owner = self._get('tasks', peer['_review_task_id'])
                if owner.get('_work_context_id') and owner['created_at'] > task.get('created_at', ''):
                    return True
        return False

    def _bind_pr_review(self, artifact, task, *, transfer=False):
        # A late report cannot replace an explicit resume from the person.
        if task and not transfer and self._pr_report_superseded(artifact, task):
            return
        if task and (transfer or not self._pr_review_source_current(artifact)) and artifact.get('_review_task_id') != task['id']:
            with self.transaction():
                artifact['_review_task_id'] = task['id']
                if transfer:
                    from .service import now
                    artifact['_review_ownership_at'] = now()
                self._put('artifacts', artifact)
                self._event('artifact.updated', self._public(artifact), artifact['bot_id'], artifact.get('task_id'))

    def _bind_pr_progress(self, artifact, task):
        if (task.get('_is_return') or task.get('_source_audit_prompt')
                or self._setting('work.cancelled.' + self._work_identity(artifact))):
            return
        tasks = {t['id']: t for t in self._all('tasks')}
        current = tasks.get(artifact.get('_review_task_id'), {})
        owner = self._work_execution_owner(task, tasks)
        if (not current or current['id'] == owner['id'] or current.get('status') != 'completed'
                or current.get('_native_outcome_id')):
            return
        peers = [a for a in self._all('artifacts')
                 if a['kind'] == 'pr' and a.get('url', '').lower() == artifact['url'].lower()]
        if self._work_pause(artifact, peers=peers):
            return
        records = self._work_receipts()
        linked = {}
        for peer in peers:
            if self._pr_review_source_current(peer):
                root = self._work_execution_owner(tasks[peer['_review_task_id']], tasks)
                linked.update(self._work_chain(root, tasks, records,
                                              include_continuations=True))
        if (any(t['status'] not in ('completed', 'failed', 'cancelled')
                or self._background_pending(t['id']) for t, _ in linked.values())
                or any(d['task_id'] in linked and d['status'] == 'pending' for d in self._all('decisions'))):
            return
        # A new execution can continue a finished review. Status callbacks keep its return address.
        from .service import now
        artifact['_review_task_id'] = owner['id']
        artifact['_review_ownership_at'] = now()

    def _pr_review_owner(self, peers):
        candidates = [a for a in peers if self._pr_review_source_current(a)]
        transferred = [a for a in candidates if a.get('_review_ownership_at')]
        if transferred:
            return max(transferred, key=lambda a: (a['_review_ownership_at'], a['id']))
        def order(artifact):
            assignment = self._assignment(self._get('tasks', artifact['_review_task_id']))
            # A delegated worker owns readiness; its coordinator receives the result.
            return (not bool(assignment.get('source_bot_id')), -assignment.get('exchange_depth', 0),
                    artifact['created_at'], artifact['id'])
        return min(candidates, key=order) if candidates else None

    @staticmethod
    def _pr_reviewed_heads(artifact):
        heads = set(artifact.get('_reviewed_heads', []))
        if artifact.get('_reviewed_head'):
            heads.add(artifact['_reviewed_head'])
        return heads

    def _pr_has_recorded_merge_decision(self, artifact, peers, decisions=None):
        recorded_check = max((a.get('_review_checked_at', '') for a in peers), default='')
        return any(d.get('merge_pr_url', '').lower() == artifact['url'].lower()
            and d['status'] in ('approved', 'rejected', 'executed', 'dismissed')
            and (d.get('_merge_pr_head') == artifact.get('head_sha')
                or (not d.get('_merge_pr_head') and (not recorded_check
                    or (d.get('decided_at') or d['created_at']) >= recorded_check)))
            for d in (self._all('decisions') if decisions is None else decisions) if d.get('merge_pr_url'))

    def _queue_pr_reviews(self, *, artifact_ids=None):
        from .service import now, uid
        with self.transaction():
            tasks = {t['id']: t for t in self._all('tasks')}
            # A retry keeps the native execution and its delegated return route.
            # Repair saved retry bindings before comparing owners across bots.
            for artifact in self._all('artifacts'):
                if artifact['kind'] != 'pr' or not artifact.get('_review_task_id'):
                    continue
                owner = self._pr_review_source(artifact['bot_id'], artifact['_review_task_id'], tasks=tasks)
                if owner['id'] != artifact['_review_task_id']:
                    artifact['_review_task_id'] = owner['id']
                    self._put('artifacts', artifact)
            bot_ids = {a['bot_id'] for a in self._all('artifacts') if a['kind'] == 'pr' and a.get('_review_task_id')
                       and (artifact_ids is None or a['id'] in artifact_ids)}
        for bot_id in bot_ids:
            with self.lock:
                bot_lock = self.bot_locks.setdefault(bot_id, threading.Lock())
            if not bot_lock.acquire(blocking=False):
                continue
            try:
                with self.transaction():
                    if self.stopping.is_set() or self.restart_pending:
                        return
                    bot = self._get('bots', bot_id)
                    # Read the artifacts and decisions once. This loop changes only the artifact that it
                    # reviews, and that object is also in its peer list, so later peers see the change.
                    artifacts, decisions = self._all('artifacts'), self._all('decisions')
                    by_url = {}
                    for item in artifacts:
                        if item['kind'] == 'pr':
                            by_url.setdefault(item.get('url', '').lower(), []).append(item)
                    for artifact in artifacts:
                        if (artifact['bot_id'] != bot_id or artifact['kind'] != 'pr'
                                or (artifact_ids is not None and artifact['id'] not in artifact_ids)
                                or not artifact.get('_review_task_id') or artifact.get('refresh_error')
                                or (artifact.get('pr_state') != 'open' and not artifact.get('_terminal_review_pending'))
                                or not (artifact.get('_terminal_review_pending') or artifact.get('checks_state') in ('passed', 'failed')
                                        or artifact.get('required_checks_state') in ('passed', 'failed', 'unverified')
                                        or artifact.get('review_checks'))
                                or not artifact.get('head_sha')):
                            continue
                        terminal = artifact.get('pr_state') in ('merged', 'closed')
                        task = self._get('tasks', artifact['_review_task_id'])
                        native = self.kanban.tasks.get(task['id'], {}) if self.kanban else {}
                        if terminal:
                            if task.get('_native_outcome_id') and native.get('id') != task['_native_outcome_id']:
                                continue
                            if native.get('status') in ('done', 'archived'):
                                continue
                        if not self._can_resume_process(task, bot) or self._task_paused(task):
                            continue
                        # One outstanding review per PR, even when several bots register it.
                        peers = by_url.get(artifact['url'].lower(), [])
                        if self._work_pause(artifact, decisions=decisions, peers=peers):
                            continue
                        owner = self._pr_review_owner(peers)
                        if not owner or owner['id'] != artifact['id']:
                            continue
                        if not terminal and self._pr_has_recorded_merge_decision(artifact, peers, decisions):
                            continue
                        review_key = f"{artifact['pr_state']}:{artifact['head_sha']}" if terminal else artifact['head_sha']
                        signal = json.dumps([artifact['head_sha'], artifact.get('checks_state'),
                                             artifact.get('required_checks_state'), artifact.get('review_checks'),
                                             artifact.get('check_failures')])
                        assessed = [a for a in peers if review_key in a.get('_reviewed_states', {})]
                        latest = max(assessed, key=lambda a: a.get('_review_checked_at', ''), default=None)
                        if (not latest and not terminal
                                and any(artifact['head_sha'] in self._pr_reviewed_heads(a) for a in peers)):
                            # Legacy heads have no signal to compare. Save one baseline without
                            # repeating the review; later result changes use the normal path.
                            artifact['_reviewed_states'] = dict(artifact.get('_reviewed_states', {}), **{review_key: signal})
                            self._put('artifacts', artifact)
                            continue
                        if latest and latest['_reviewed_states'][review_key] == signal:
                            continue
                        summary = 'Checking the final PR result.' if terminal else 'Checking merge readiness.'
                        assignment = self._assignment(task)
                        if assignment.get('_waiting_on') or self._background_pending(assignment['id']):
                            continue
                        if assignment['id'] != task['id'] and assignment['status'] == 'failed':
                            assignment.update(status='waiting', summary=summary, updated_at=now())
                            self._put('tasks', assignment)
                            self._event('task.updated', self._public(assignment), assignment['bot_id'], assignment['id'])
                        epoch = task.get('_conversation_epoch')
                        self._advance_input(task)
                        task.update(status='queued', updated_at=now(), summary=summary,
                            _conversation_epoch=epoch,
                            _pr_review={k: artifact.get(k) for k in ('url', 'pr_state', 'head_sha', 'checked_at', 'draft',
                                                                  'checks_state', 'required_checks_state', 'review_checks', 'check_failures')},
                            _background_return_after=now(), _background_return_id=uid())
                        artifact['_reviewed_heads'] = sorted(self._pr_reviewed_heads(artifact) | {artifact['head_sha']})
                        artifact['_reviewed_head'] = artifact['head_sha']
                        artifact['_reviewed_states'] = dict(artifact.get('_reviewed_states', {}), **{review_key: signal})
                        artifact.pop('_terminal_review_pending', None)
                        artifact['_review_checked_at'] = now()
                        self._put('artifacts', artifact)
                        self._put('tasks', task)
                        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
                        self._refresh_bot(bot_id)
            finally:
                bot_lock.release()
