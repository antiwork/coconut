"""Continue PR review through the bot that owns its work."""
import json
import re
import threading

# How to review a PR, and when to merge without a card, is the person's workflow. It lives in the bot's own Hermes skills.
# Coconut keeps the merge card contract and the permission rails (card_tools.PR_MERGE_INSTRUCTIONS).

# The cards that name one PR as their merge. The index decisions_merge_url finds them.
MERGE_DECISIONS = "SELECT data FROM decisions WHERE lower(json_extract(data,'$.merge_pr_url'))=?"
# The pending cards, in row order. The index decisions_status finds them.
PENDING_CARDS = "SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending' ORDER BY rowid"
PENDING_MERGE_CARDS = ("SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending' "
                       "AND json_extract(data,'$.merge_pr_url') IS NOT NULL ORDER BY rowid")
MERGED_PRS = ("SELECT json_extract(data,'$.bot_id','$.url','$.observed_at','$.refresh_error') FROM artifacts "
              "WHERE json_extract(data,'$.kind')='pr' AND json_extract(data,'$.pr_state')='merged'")
# The PR rows, in row order. A PR poll decodes these rows and not the work and file rows.
PR_ARTIFACTS = "SELECT data FROM artifacts WHERE json_extract(data,'$.kind')='pr' ORDER BY rowid"
# _bot_pr_artifact finds the records of one PR URL through this index. Service.__init__ creates it.
PR_URL_INDEX = """
CREATE INDEX IF NOT EXISTS artifacts_pr_url ON artifacts (lower(json_extract(data,'$.url'))) WHERE json_extract(data,'$.kind')='pr';
"""

# The label of a merge card choice that defers the decision until named work ends: "Wait for <work>, then decide".
# No other choice defers. Leave open, a decline, or any other label keeps the PR head closed.
WAIT_THEN_DECIDE = re.compile(r'wait for \S.*?,? then decide\.?', re.IGNORECASE)

PR_REVIEW_INSTRUCTIONS = (
    'Coconut observed new check or review results for a tracked pull request. This is a request to assess merge readiness, not permission to merge. '
    'Assess it as your skills for pull request review and merge say. '
    'Do not repeat finished implementation work. Respect any visual approval or other pending decision. '
    'If the PR head changed, checks are incomplete, or a requirement remains unmet, do not claim it is ready. '
    'The observation reports required checks apart from optional checks. '
    'If required_checks_state is unverified, a required status passed but its expected GitHub App is unconfirmed. Verify its source before recommending merge. '
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

    def _merge_decision_artifact(self, url, task):
        return next((a for a in self._all('artifacts')
                     if url and a['kind'] == 'pr' and a['bot_id'] == task['bot_id']
                     and a['url'].lower() == url.lower()), {})

    def _refresh_merge_diff(self, artifact):
        """Keep the change size of this PR current on its pending merge cards. The size does not change the choice."""
        import json
        rows = self.db.execute("SELECT data FROM decisions WHERE json_extract(data,'$.status')='pending' "
                               "AND lower(json_extract(data,'$.merge_pr_url'))=?", ((artifact.get('url') or '').lower(),))
        for decision in [json.loads(row[0]) for row in rows]:
            if decision.get('bot_id') == artifact['bot_id'] and decision.get('merge_pr_diff') != artifact.get('diff'):
                decision['merge_pr_diff'] = artifact.get('diff')
                self._put('decisions', decision)
                self._event('decision.updated', self._public(decision), decision['bot_id'], decision['task_id'])

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
        # The executed cards of a chain of waits: each wait choice asked for the next card when its work ended.
        # Coconut checked each link when it made the card that records it (_fulfilled_wait).
        waited, link = set(), decision.get('_fulfilled_wait')
        while link and link['previous_decision_id'] not in waited:
            waited.add(link['previous_decision_id'])
            row = self.db.execute('SELECT data FROM decisions WHERE id=?', (link['previous_decision_id'],)).fetchone()
            link = json.loads(row[0]).get('_fulfilled_wait') if row else None
        for existing in self._all('decisions'):
            if (existing['id'] == decision['id'] or not existing.get('merge_pr_url')
                    or existing['merge_pr_url'].lower() != url.lower()):
                continue
            if existing['id'] in waited and existing['status'] == 'executed':
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
        # A review that still runs does not hold the card: when to offer a merge is the person's workflow in the bot's
        # skills. The card's context says that a review still runs. Coconut keeps the rails above: one card for each PR
        # and head, and the answer of the person before any merge.
        return requested_at

    def _fulfilled_wait(self, decision, task, payload):
        """The previous merge card whose wait choice has ended, for its one follow-up card of the same PR head.

        A person who chose "Wait for <work>, then decide" asked to decide again when that work ends. The owner of the card
        names the choice and the result that the work delivered to it. Coconut checks both against the record and
        changes neither. Without this, the recorded choice keeps the head closed (_validate_merge_decision)."""
        from .service import ServiceError
        claim = payload.get('fulfilled_wait')
        if claim is None:
            return None
        if (not isinstance(claim, dict) or set(claim) != {'option_id', 'result_message_id'}
                or not all(isinstance(value, str) and value.strip() for value in claim.values())):
            raise ServiceError(400, 'fulfilled_wait needs option_id and result_message_id.')
        url, head = decision.get('merge_pr_url'), decision.get('_merge_pr_head')
        row = self.db.execute('SELECT data FROM decisions WHERE id=?', (decision.get('previous_decision_id'),)).fetchone()
        previous = json.loads(row[0]) if row else {}
        option = next((o for o in previous.get('options', []) if o['id'] == claim['option_id']), None)
        # Revisions of the card before its answer (_revise_question) are not answers. The person's answer is the last entry.
        answers = previous.get('_answers') or [{}]
        answer = answers[-1]
        # Only the answer of a person on a paired device: not a cancellation, an archive, or another system answer.
        human = bool(previous.get('decided_by')) and self.db.execute(
            'SELECT 1 FROM devices WHERE id=?', (previous['decided_by'],)).fetchone() is not None
        if not (decision['action_type'] == 'question' and decision.get('inbox_kind') == 'decision'
                and url and head and previous.get('action_type') == 'question' and previous['status'] == 'executed'
                and previous['bot_id'] == task['bot_id']
                and previous['task_id'] in (task['id'], task.get('_assignment_task_id'), task.get('_execution_session_id'))
                and (previous.get('merge_pr_url') or '').lower() == url.lower() and previous.get('_merge_pr_head') == head
                and option and 'merge' not in option['label'].lower()
                and WAIT_THEN_DECIDE.fullmatch(option['label'].strip())
                and previous.get('selected_option_id') == option['id'] and not previous.get('correction')
                and all(a.get('choice') == 'revise' for a in answers[:-1]) and answer.get('choice') == 'approve'
                and answer.get('option_id') == option['id'] and not answer.get('correction')
                and answer.get('decided_by') == previous['decided_by'] and human):
            raise ServiceError(400, 'fulfilled_wait must name the Wait for ..., then decide choice that the person '
                                    'selected on the linked merge card of this task, for this PR and head.')
        row = self.db.execute('SELECT data FROM messages WHERE id=?', (claim['result_message_id'],)).fetchone()
        message = json.loads(row[0]) if row else {}
        row = self.db.execute('SELECT data FROM tasks WHERE id=?', (message.get('task_id'),)).fetchone()
        work = json.loads(row[0]) if row else {}
        # The waited work: a task that the card's agent requested from another agent, started before the answer and
        # finished since. It delivered this result to the card's agent after the answer, and it tracks this PR at this head.
        if not (work and message.get('role') == 'assistant' and message.get('kind') == 'text'
                and not message.get('_interim') and not message.get('streaming')
                and message.get('sender_bot_id') == work['bot_id'] != previous['bot_id']
                and previous['bot_id'] in (message.get('exchange_recipient_bot_ids') or [])
                and work.get('source_bot_id') == work.get('conversation_bot_id') == previous['bot_id']
                and work['status'] == 'completed'
                and work['created_at'] < previous['decided_at'] < message['created_at']
                and any(a['kind'] == 'pr' and a['url'].lower() == url.lower() and a.get('head_sha') == head
                        and work['id'] in (a.get('task_id'), a.get('_review_task_id')) for a in self._all('artifacts'))):
            raise ServiceError(400, 'fulfilled_wait must name the result that the waited work delivered to this agent '
                                    'after the answer. That work must be finished and track this PR at this head.')
        for other in self._all('decisions'):
            recorded = other.get('_fulfilled_wait') or {}
            if other['id'] != decision['id'] and (recorded.get('previous_decision_id') == previous['id']
                                                  or recorded.get('result_message_id') == message['id']):
                raise ServiceError(400, f"Decision {other['id']} already follows this wait. Reuse it and its owner.")
        return dict(previous_decision_id=previous['id'], option_id=option['id'], result_message_id=message['id'],
                    work_task_id=work['id'])

    def _resolve_merged_pr_decisions(self):
        with self.transaction():
            decisions = [json.loads(row[0]) for row in self.db.execute(PENDING_MERGE_CARDS)]
            merged = set()
            if decisions:
                for bot_id, url, observed, error in (json.loads(row[0]) for row in self.db.execute(MERGED_PRS)):
                    if observed and not error:
                        merged.add((bot_id, url.lower()))
            for decision in decisions:
                url = decision.get('merge_pr_url')
                if (not url or decision['status'] != 'pending' or decision['action_type'] != 'question'
                        or (decision['bot_id'], url.lower()) not in merged):
                    continue
                response = ('This PR has already merged. Coconut closed the merge question after verifying GitHub. '
                            'The person did not select an option. Do not merge again or treat this as approval for other work.')
                self._close_card_for_source(decision, url, response, 'merged', outcome='Already merged.')

    def _migrate_pr_review_sources(self):
        for artifact in self._all('artifacts'):
            if artifact['kind'] != 'pr' or '_review_task_id' in artifact or not artifact.get('task_id'):
                continue
            task = self._pr_review_source(artifact['bot_id'], artifact['task_id'])
            artifact['_review_task_id'] = task['id']
            self._put('artifacts', artifact)

    def _bot_pr_artifact(self, bot_id, url):
        """The first PR record of this bot for this URL. It reads only the records of the URL, not every artifact."""
        rows = self.db.execute("SELECT data FROM artifacts WHERE json_extract(data,'$.kind')='pr' "
                               "AND lower(json_extract(data,'$.url'))=? ORDER BY rowid", (url.lower(),)).fetchall()
        return next((artifact for artifact in (json.loads(row[0]) for row in rows)
                     if artifact['bot_id'] == bot_id and artifact['url'].lower() == url.lower()), None)

    def _pr_review_source(self, bot_id, task_id, *, tasks=None):
        if not task_id:
            return None
        from .service import ServiceError
        if not isinstance(task_id, str):
            raise ServiceError(400, 'Choose the task that owns this pull request.')
        task = tasks[task_id] if tasks is not None else self._get('tasks', task_id)
        if task['bot_id'] != bot_id:
            raise ServiceError(400, 'The pull request and its task must belong to the same agent.')
        if tasks is None:
            # Inside _shared_reads this is the kept task map, which a full parse under the service lock would cost.
            tasks = self._task_map()
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

    @staticmethod
    def _set_review_owner(artifact, identity):
        """Move a PR record to a new review owner and keep every owner it has had, in order.

        A record that predates the list keeps none, so its earlier owners stay unknown. A record that Coconut
        creates starts the list with its first owner (see _register_pr_artifact), so the list is complete. A list
        that is not consistent with the owner before this move has lost an owner, and a new entry would hide that
        loss, so the list is dropped. Whenever an owner leaves a record whose history is unknown, the record is
        marked so that the history stays unknown and later moves cannot make it look complete or unmoved.
        """
        owners, current = artifact.get('_review_owners'), artifact.get('_review_task_id')
        if owners is None or PullRequestReviews._review_owner_history(artifact) is None:
            artifact.pop('_review_owners', None)
            if current and current != identity:
                artifact['_review_history_unknown'] = True
        elif identity and (not owners or owners[-1] != identity):
            owners.append(identity)
        artifact['_review_task_id'] = identity

    @staticmethod
    def _review_owner_history(artifact):
        """The ordered owners of a PR record, or None when the record does not hold a complete, consistent list.

        The list is trusted only when each entry is a task ID and its last entry is the record's current owner, so
        an owner written by a path that did not record it, or a changed list, leaves the history unknown.
        """
        owners, current = artifact.get('_review_owners'), artifact.get('_review_task_id')
        if (not isinstance(owners, list) or not all(isinstance(identity, str) and identity for identity in owners)
                or (current and (not owners or owners[-1] != current))):
            return None
        return owners

    def _bind_pr_review(self, artifact, task, *, transfer=False):
        # A late report cannot replace an explicit resume from the person.
        if task and not transfer and self._pr_report_superseded(artifact, task):
            return
        if task and (transfer or not self._pr_review_source_current(artifact)) and artifact.get('_review_task_id') != task['id']:
            with self.transaction():
                self._set_review_owner(artifact, task['id'])
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
        self._set_review_owner(artifact, owner['id'])
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
        if decisions is None:
            with self.lock:
                decisions = [json.loads(row[0]) for row in self.db.execute(MERGE_DECISIONS, (artifact['url'].lower(),))]
        recorded_check = max((a.get('_review_checked_at', '') for a in peers), default='')
        return any(d.get('merge_pr_url', '').lower() == artifact['url'].lower()
            and d['status'] in ('approved', 'rejected', 'executed', 'dismissed')
            and (d.get('_merge_pr_head') == artifact.get('head_sha')
                or (not d.get('_merge_pr_head') and (not recorded_check
                    or (d.get('decided_at') or d['created_at']) >= recorded_check)))
            for d in decisions if d.get('merge_pr_url'))

    def _queue_pr_reviews(self, *, artifact_ids=None):
        from .service import now, uid
        def review_owners(service, reviewed):
            def tasks_by_id(identities):
                return {row[0]: json.loads(row[1]) for row in service.db.execute(
                    'SELECT id, data FROM tasks WHERE id IN (SELECT value FROM json_each(?))',
                    (json.dumps(sorted(i for i in identities if isinstance(i, str))),))}
            # The owner of a review is its task or that task's execution session. Read only those tasks.
            tasks = tasks_by_id({a['_review_task_id'] for a in reviewed})
            tasks.update(tasks_by_id({t.get('_execution_session_id') for t in tasks.values()} - tasks.keys()))
            return {a['id']: service._pr_review_source(a['bot_id'], a['_review_task_id'], tasks=tasks)['id'] for a in reviewed}
        # Every PR record is read without the service lock. Only a record whose owner moved is read again and written.
        with self._read_view() as view, view._read_transaction():
            reviewed = [a for a in (json.loads(row[0]) for row in view.db.execute(PR_ARTIFACTS)) if a.get('_review_task_id')]
            owners = review_owners(view, reviewed)
            moved = [a['id'] for a in reviewed if owners[a['id']] != a['_review_task_id']]
            bot_ids = {a['bot_id'] for a in reviewed if artifact_ids is None or a['id'] in artifact_ids}
        if moved:
            # A retry keeps the native execution and its delegated return route.
            # Repair saved retry bindings before comparing owners across bots.
            with self.transaction():
                for identity in moved:
                    artifact = self._get('artifacts', identity)
                    if not artifact.get('_review_task_id'):
                        continue
                    owner = review_owners(self, [artifact])[identity]
                    if owner != artifact['_review_task_id']:
                        self._set_review_owner(artifact, owner)
                        self._put('artifacts', artifact)
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
                    # Read the PR rows once. This loop changes only the artifact that it reviews, and that object
                    # is also in its peer list, so later peers see the change. The pause and merge checks read
                    # their few decisions through indexes.
                    artifacts = [json.loads(row[0]) for row in self.db.execute(PR_ARTIFACTS)]
                    by_url = {}
                    for item in artifacts:
                        by_url.setdefault(item.get('url', '').lower(), []).append(item)
                    for artifact in artifacts:
                        if (artifact['bot_id'] != bot_id
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
                        if self._work_pause(artifact, peers=peers):
                            continue
                        owner = self._pr_review_owner(peers)
                        if not owner or owner['id'] != artifact['id']:
                            continue
                        if not terminal and self._pr_has_recorded_merge_decision(artifact, peers):
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
