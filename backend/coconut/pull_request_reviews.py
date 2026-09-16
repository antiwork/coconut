"""Continue PR review through the bot that owns its work."""
import json
import threading

PR_REVIEW_INSTRUCTIONS = (
    'Coconut observed passing CI for a tracked pull request. This is a request to assess merge readiness, not permission to merge. '
    'Read the current PR head, required checks, reviews, unresolved findings, merge conflicts, and the original human instructions. '
    'Do not repeat finished implementation work. Respect any visual approval or other pending decision. '
    'A draft can be ready for a decision when its owner reports the current head ready. '
    'If the PR remains a draft, include marking it ready in the proposed merge action. '
    'If the PR head changed, checks are incomplete, or a requirement remains unmet, do not claim it is ready. '
    'If another bot requested this work, return your verified result to that bot through the existing assignment. '
    'Ask the requesting bot to offer one coconut_ask merge decision when the evidence supports it. '
    'If this is your own human conversation, offer that decision yourself. Recommend Merge when justified and include a Leave open option. '
    'Reuse an existing pending merge decision for this PR. A choice must identify the PR and the scope of the merge. '
    'Set merge_pr_url on that coconut_ask card to the tracked PR URL so an external merge retires it. '
    'Do not merge now. After explicit authorization, recheck the exact head and use a merge operation that rejects a changed head. '
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

    def _pr_review_source(self, bot_id, task_id):
        if not task_id:
            return None
        from .service import ServiceError
        if not isinstance(task_id, str):
            raise ServiceError(400, 'Choose the task that owns this pull request.')
        task = self._get('tasks', task_id)
        if task['bot_id'] != bot_id:
            raise ServiceError(400, 'The pull request and its task must belong to the same bot.')
        return task

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
                self._put('artifacts', artifact)

    def _pr_review_owner(self, peers):
        candidates = [a for a in peers if self._pr_review_source_current(a)]
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

    def _queue_pr_reviews(self):
        from .service import now, uid
        with self.lock:
            bot_ids = {a['bot_id'] for a in self._all('artifacts') if a['kind'] == 'pr' and a.get('_review_task_id')}
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
                    for artifact in self._all('artifacts'):
                        if (artifact['bot_id'] != bot_id or artifact['kind'] != 'pr'
                                or not artifact.get('_review_task_id') or artifact.get('refresh_error')
                                or artifact.get('pr_state') != 'open'
                                or (artifact.get('draft') is not False and not (artifact.get('draft') is True
                                    and artifact.get('_activity_state') == 'ready'
                                    and artifact.get('_activity_head') == artifact.get('head_sha')))
                                or artifact.get('checks_state') != 'passed' or not artifact.get('head_sha')):
                            continue
                        task = self._get('tasks', artifact['_review_task_id'])
                        if not self._can_resume_process(task, bot) or self._task_paused(task):
                            continue
                        # One outstanding review per PR, even when several bots register it.
                        peers = [a for a in self._all('artifacts') if a['kind'] == 'pr' and a.get('url', '').lower() == artifact['url'].lower()]
                        if self._work_pause(artifact, peers=peers):
                            continue
                        owner = self._pr_review_owner(peers)
                        if not owner or owner['id'] != artifact['id']:
                            continue
                        if any(artifact['head_sha'] in self._pr_reviewed_heads(a) for a in peers):
                            from datetime import datetime, timezone
                            from .decision_freshness import age
                            checked = max((a.get('_review_checked_at') or self._get('tasks', a['_review_task_id']).get('updated_at', '')
                                           for a in peers if a.get('_review_task_id')), default='')
                            recorded_check = max((a.get('_review_checked_at', '') for a in peers), default='')
                            # Preserve unknown legacy choices until a new head receives a recorded review.
                            if any(d.get('merge_pr_url', '').lower() == artifact['url'].lower()
                                   and d['status'] in ('approved', 'rejected', 'executed', 'dismissed')
                                   and (d.get('_merge_pr_head') == artifact['head_sha']
                                        or (not d.get('_merge_pr_head') and (not recorded_check
                                            or (d.get('decided_at') or d['created_at']) >= recorded_check)))
                                   for d in self._all('decisions') if d.get('merge_pr_url')):
                                continue
                            if age(checked, datetime.now(timezone.utc)) < 3600:
                                continue
                            projected = next(a for a in self._work_projection([dict(p) for p in peers]) if a['id'] == artifact['id'])
                            if projected['work_state'] not in ('waiting', 'needs_recovery') or projected['work_dependencies']:
                                continue
                        assignment = self._assignment(task)
                        if assignment.get('_waiting_on') or self._background_pending(assignment['id']):
                            continue
                        if assignment['id'] != task['id'] and assignment['status'] == 'failed':
                            assignment.update(status='waiting', summary='Checking merge readiness.', updated_at=now())
                            self._put('tasks', assignment)
                            self._event('task.updated', self._public(assignment), assignment['bot_id'], assignment['id'])
                        epoch = task.get('_conversation_epoch')
                        self._advance_input(task)
                        task.update(status='queued', updated_at=now(), summary='Checking merge readiness.',
                            _conversation_epoch=epoch,
                            _pr_review={k: artifact[k] for k in ('url', 'head_sha', 'checked_at', 'draft')},
                            _background_return_after=now(), _background_return_id=uid())
                        artifact['_reviewed_heads'] = sorted(self._pr_reviewed_heads(artifact) | {artifact['head_sha']})
                        artifact['_reviewed_head'] = artifact['head_sha']
                        artifact['_review_checked_at'] = now()
                        self._put('artifacts', artifact)
                        self._put('tasks', task)
                        self._event('task.updated', self._public(task), task['bot_id'], task['id'])
                        self._refresh_bot(bot_id)
            finally:
                bot_lock.release()
