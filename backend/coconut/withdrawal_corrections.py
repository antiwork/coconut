"""Reopen a card that an automatic card check withdrew in error.

A card check can withdraw a pending card as obsolete (`refresh_decision`). When later maintenance shows that the check read
the wrong evidence, the outcome that owns the card can reopen the same card for the person. The correction is service
maintenance. It never answers, approves, sends, or discards, and it keeps the withdrawal in the card's history.

Who may call: a turn of the execution that owns the card, or a turn of an execution that the owner genuinely delegated
to (not a return, progress reply, or callback). Coconut reads both from service records, never from tool arguments or names.
A later human message in the card's outcome refuses the correction on purpose; see `_withdrawn_card`.
"""
import json


CORRECTION_SCHEMA = {
    'name': 'coconut_correct_withdrawal',
    'description': (
        'Service maintenance: reopen one card that an automatic card check withdrew in error. The same card, choices, '
        'and exact draft return to the person. Use only for a card of the outcome that owns this turn or delegated it to you. '
        'First read each linked source now, and confirm that no person answered, dismissed, or replaced the card. '
        'This never answers, approves, sends, or discards. Coconut refuses a card that a person handled or that changed.'),
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'decision_id': {'type': 'string', 'description': 'The withdrawn card.'},
        'expected_revision': {'type': 'integer', 'minimum': 1, 'description': 'The current revision of the withdrawn card.'},
        'withdrawal_context_id': {'type': 'string', 'description': 'The automatic card check that recorded the withdrawal.'},
        'draft_hash': {'type': 'string', 'description': 'Required for a message draft: its saved draft_hash. The draft stays unchanged.'},
        'source_revisions': {'type': 'object', 'additionalProperties': {'type': 'string'},
                             'description': 'Map each linked GitHub issue or PR URL to the exact updated_at you just read from the GitHub issues API.'},
        'reason': {'type': 'string', 'maxLength': 1000, 'description': 'The verified facts that show the withdrawal was wrong.'},
    }, 'required': ['decision_id', 'expected_revision', 'withdrawal_context_id', 'reason']},
}

# A person's answer, dismissal, or follow-up card leaves one of these on the card.
HUMAN_FIELDS = ('decided_at', 'decided_by', 'decided_by_name', 'selected_option_id', 'selected_option_label',
                'correction', '_answers', '_inbox_dismissed_at', '_inbox_reject_device', '_execution_task_id')


def correction_request(payload):
    from .service import ServiceError
    if not isinstance(payload, dict) or set(payload) - set(CORRECTION_SCHEMA['parameters']['properties']):
        raise ServiceError(400, 'Supply only the documented correction fields.')
    for key in ('decision_id', 'withdrawal_context_id'):
        if not isinstance(payload.get(key), str) or not payload[key].strip() or len(payload[key]) > 100:
            raise ServiceError(400, f'Supply the exact {key}.')
    if type(payload.get('expected_revision')) is not int or payload['expected_revision'] < 1:
        raise ServiceError(400, 'Supply the current revision of the withdrawn card.')
    reason = payload.get('reason')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ServiceError(400, 'State the verified facts that show the withdrawal was wrong, in at most 1000 characters.')
    if 'draft_hash' in payload and not isinstance(payload['draft_hash'], str):
        raise ServiceError(400, 'Supply draft_hash as text.')
    revisions = payload.get('source_revisions', {})
    if not isinstance(revisions, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in revisions.items()):
        raise ServiceError(400, 'Map each linked source URL to its updated_at text.')
    return dict(payload, reason=reason.strip(), source_revisions=revisions)


def issue_url(url):
    from .decision_sources import github_source
    owner, repo, _, number = github_source(url)[1].split('/')
    return f'https://github.com/{owner}/{repo}/issues/{number}'


class WithdrawalCorrections:
    def _row(self, statement, parameters):
        # These guards read the current rows directly, never a cached read view.
        row = self.db.execute(statement, parameters).fetchone()
        return json.loads(row[0]) if row else None

    def _delegation_source(self, member):
        """Return the task that genuinely delegated this execution to `member`, or None.

        `_store_bot_message` writes a non-return delegation with `_reply_to_task_id` set to the sender's assignment, and
        `continue_native_work` may move it to the direct retry. Both write a `deliveries` row. A return, a progress reply,
        or a callback to the requester is `_is_return` and gets no `_reply_to_task_id`, so it never qualifies. A hand-made
        source link without these service records does not qualify either.
        """
        source_id = member.get('source_task_id')
        if not source_id or member.get('_is_return') or member['status'] == 'cancelled':
            return None
        source = self._row('SELECT data FROM tasks WHERE id=?', (source_id,))
        if (not source or source['bot_id'] != member.get('source_bot_id') or source['thread_id'] != member['thread_id']
                or source.get('conversation_bot_id') != member.get('conversation_bot_id')):
            return None
        assignment = self._row('SELECT data FROM tasks WHERE id=?', (source['_assignment_task_id'],)) \
            if source.get('_assignment_task_id') else source
        if not assignment or member.get('_reply_to_task_id') not in (assignment['id'], source['id']):
            return None
        exchange = self._row('SELECT data FROM exchanges WHERE id=?', (member['exchange_id'],)) if member.get('exchange_id') else None
        if 'cancelled' in (source['status'], assignment['status']) or (exchange or {}).get('cancelled'):
            return None
        senders = {source['id'], assignment['id']} | set(self._execution_members(source))
        delivered = any(row[0] in senders for row in self.db.execute(
            "SELECT json_extract(data,'$.source_task_id') FROM deliveries WHERE json_extract(data,'$.receipt.task_id')=? "
            "AND json_extract(data,'$.target_bot_id')=? AND COALESCE(json_extract(data,'$._progress_only'),0)=0",
            (member['id'], member['bot_id'])))
        return source if delivered else None

    def _execution_members(self, task):
        """The task and the same-bot tasks of its execution session."""
        ids = {task['id']}
        if task.get('_execution_session_id'):
            ids.update(row[0] for row in self.db.execute(
                "SELECT id FROM tasks WHERE json_extract(data,'$._execution_session_id')=? AND json_extract(data,'$.bot_id')=?",
                (task['_execution_session_id'], task['bot_id'])))
        return ids

    def _correction_owners(self, caller):
        """Return the task IDs whose cards this turn may reopen, and how it holds that authority.

        The caller's own execution owns its cards. A bot that an execution genuinely delegated to (see _delegation_source)
        acts for that execution, in any turn of the same delegated execution. Nothing here comes from tool arguments.
        """
        owners = [(caller, 'owner')]
        for identity in sorted(self._execution_members(caller)):
            member = caller if identity == caller['id'] else self._row('SELECT data FROM tasks WHERE id=?', (identity,))
            source = self._delegation_source(member) if member else None
            if source:
                owners.append((source, 'delegate'))
        scope = {}
        for owner, authority in owners:
            for identity in self._execution_members(owner):
                scope.setdefault(identity, authority)
        return scope

    def _withdrawal_corrections_available(self, caller):
        """Whether a turn may see the correction tool: some card in its scope is an automatic withdrawal."""
        scope = sorted(self._correction_owners(caller))
        return bool(self.db.execute(
            "SELECT 1 FROM decisions WHERE json_extract(data,'$.task_id') IN (" + ','.join('?' for _ in scope) + ") "
            "AND json_extract(data,'$.status')='resolved' AND json_extract(data,'$.resolution')='work_scope_changed' "
            "AND json_extract(data,'$._native') AND json_extract(data,'$.decided_by') IS NULL LIMIT 1", scope).fetchone())

    def _withdrawn_card(self, caller, request):
        """Check that the card is an automatic withdrawal that nobody handled since, and return its parts.

        Each refusal leaves the card unchanged. The same checks run again inside the transaction that reopens it.
        """
        from .service import ServiceError, digest, encoded
        from .decision_freshness import WITHDRAWAL_RESPONSE
        from .decision_sources import github_links
        if caller['status'] != 'working' or self.stopping.is_set() or self._task_paused(caller):
            raise ServiceError(409, 'This turn is not running.')
        decision = self._get('decisions', request['decision_id'])
        authority = self._correction_owners(caller).get(decision['task_id'])
        task = self._get('tasks', decision['task_id'])
        if authority is None or decision['bot_id'] != task['bot_id']:
            raise ServiceError(403, 'Only the execution that owns this card, or a bot it delegated the outcome to, can reopen it.')
        if task['status'] == 'cancelled' or self._task_paused(task):
            raise ServiceError(409, 'The card belongs to a cancelled or paused task.')
        if (not decision.get('_native') or decision.get('action_type') not in ('question', 'draft_message')
                or decision.get('inbox_kind') != 'proposal' or decision.get('_hidden') or decision.get('_deleted')):
            # A blocking question resumed its task when it was withdrawn. Reopening it cannot undo that turn.
            raise ServiceError(409, 'Only a withdrawn suggestion or message draft can be reopened.')
        if decision['status'] != 'resolved' or decision.get('resolution') != 'work_scope_changed':
            raise ServiceError(409, 'This card is not an automatic withdrawal.')
        if decision['revision'] != request['expected_revision']:
            raise ServiceError(409, 'The card changed after the withdrawal. Read its current state.')
        if any(decision.get(field) for field in HUMAN_FIELDS):
            raise ServiceError(409, 'A person answered or dismissed this card. Keep their answer.')
        if decision.get('_receipt_id') or decision.get('_source_recheck') or self.db.execute(
                "SELECT 1 FROM tasks WHERE json_extract(data,'$._resume_decision')=?", (decision['id'],)).fetchone():
            raise ServiceError(409, 'The withdrawal already continued work. Reopening the card cannot undo it.')
        if decision['action_type'] == 'draft_message':
            draft = decision.get('draft')
            if not isinstance(draft, dict) or digest(encoded(draft)) != decision.get('draft_hash'):
                raise ServiceError(409, 'The saved draft does not match its hash. Ask the person.')
            if request.get('draft_hash') != decision['draft_hash']:
                raise ServiceError(409, 'Supply the exact saved draft_hash. The draft cannot change in a correction.')
        elif 'draft_hash' in request:
            raise ServiceError(400, 'Only a message draft takes draft_hash.')

        # The exact check that withdrew the card, from its own records.
        context_id = request['withdrawal_context_id']
        context = self._row('SELECT data FROM contexts WHERE id=?', (context_id,))
        freshness = decision.get('_freshness', {})
        evidence = freshness.get('evidence')
        recorded = decision.get('_withdrawal')
        if (not context or not context.get('_decision_refresh') or context.get('_refresh_recorded') is not True
                or context['decision_id'] != decision['id'] or context['task_id'] != decision['task_id']
                or context['bot_id'] != decision['bot_id'] or context['status'] not in ('completed', 'cancelled', 'failed')
                or context.get('_decision_revision') != decision['revision'] - 1
                or context.get('_conversation_epoch') != task.get('_conversation_epoch')
                or freshness.get('context_id') != context_id or not isinstance(evidence, str) or not evidence
                or decision.get('outcome') != evidence or decision.get('resolution_context') != WITHDRAWAL_RESPONSE + evidence
                or (decision.get('_source_check') or {}).get('revision') != decision['revision']
                or (recorded is not None and (recorded.get('context_id') != context_id
                                              or recorded.get('revision') != decision['revision']
                                              or recorded.get('from_revision') != context['_decision_revision']
                                              or recorded.get('conversation_epoch') != context.get('_conversation_epoch')
                                              or recorded.get('evidence_fingerprint') != context.get('_evidence_fingerprint')))):
            raise ServiceError(409, 'That check did not record this withdrawal. Use the automatic check named in the card.')
        # Tool effects of every check and reply of this card, read from durable rows. Maintenance removes
        # `_recovery_unknown_tools` from the ended contexts of a settled card (DatabaseMaintenance._prune_contexts), so that
        # list alone cannot show that nothing is outstanding. The receipt journal keeps executing receipts and results that
        # wait for delivery (DatabaseMaintenance._prune_receipts). The rules match DecisionFreshness._refresh_recovery_records.
        contexts = {c['id']: c for c in (json.loads(r[0]) for r in self.db.execute(
            "SELECT data FROM contexts WHERE json_extract(data,'$.decision_id')=?", (decision['id'],)))}
        receipts = [json.loads(r[0]) for r in self.db.execute(
            "SELECT data FROM native_receipts WHERE json_extract(data,'$.turn_id') IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(contexts)),))]
        receipts += [self._row('SELECT data FROM native_receipts WHERE id=?', (r['id'],))
                     for r in context.get('_recovery_unknown_tools', [])]
        if any(self._unresolved_effect(r, contexts) for r in receipts):
            raise ServiceError(409, 'A tool of a check of this card has no confirmed result. Verify it before reopening the card.')

        # Nothing that a person did, and no newer work, may conflict with the reopened card.
        if any(c['status'] in ('queued', 'working') for c in contexts.values()):
            raise ServiceError(409, 'Another check or reply for this card is still running.')
        if self.db.execute("SELECT 1 FROM messages WHERE json_extract(data,'$.decision_id')=? "
                           "AND json_extract(data,'$._human_device_id') IS NOT NULL", (decision['id'],)).fetchone():
            raise ServiceError(409, 'A person replied on this card. Keep their reply.')
        # Deliberate fail-closed boundary. Any human message in this outcome after the card refuses, whatever it says or
        # replied to, and also when a person asked for this repair there. Coconut cannot tell a maintenance-only request
        # from direction about the card without reading prose, and a message from before the withdrawal may be what the
        # check rightly acted on, so neither withdrawal time nor the calling turn's own message is exempt. Such a card stays
        # with the person. A service-requested repair (an owner or delegate turn without new human messages here) can use it.
        with self._shared_reads():
            ids, threads = self._decision_outcome(task)
            later = [m for m in self._all_shared('messages') if m.get('_human_device_id') and not m.get('_deleted')
                     and m['created_at'] > decision['created_at']
                     and (m.get('task_id') in ids or m.get('thread_id') in threads)]
        if later:
            raise ServiceError(409, 'A person wrote in this outcome after the card. Follow their direction instead.')
        for other in (json.loads(r[0]) for r in self.db.execute(
                "SELECT data FROM decisions WHERE id<>? AND (json_extract(data,'$.previous_decision_id')=? "
                "OR (json_extract(data,'$.draft_hash') IS NOT NULL AND json_extract(data,'$.draft_hash')=?) "
                "OR (json_extract(data,'$.task_id')=? AND json_extract(data,'$._proposal_key')=?))",
                (decision['id'], decision['id'], decision.get('draft_hash'), decision['task_id'], decision.get('_proposal_key')))):
            if not other.get('_deleted'):
                raise ServiceError(409, 'Another card replaced this one. Keep the newer card.')

        links = github_links(decision)
        if set(request['source_revisions']) - set(links):
            raise ServiceError(400, 'source_revisions lists a URL that the card does not link.')
        expected = {}
        for url in links:
            revision = (decision.get('_source_facts', {}).get(url) or {}).get('updated_at') or decision.get('_source_seen', {}).get(url)
            if not isinstance(revision, str):
                raise ServiceError(409, 'The withdrawal recorded no version of ' + url + '. Ask the person.')
            if request['source_revisions'].get(url) != revision:
                raise ServiceError(409, 'The source changed after the withdrawal, or source_revisions is missing: ' + url
                                   + '. Read its current state and comments. Ask the person instead.')
            expected[url] = revision
        return dict(decision=decision, task=task, context=context, authority=authority, links=links, expected=expected)

    @staticmethod
    def _unresolved_effect(record, contexts):
        """Whether a receipt of this card's checks leaves an effect or result unknown. A missing receipt counts as unknown."""
        from .background_processes import subscription
        if not record or record.get('state') != 'completed':
            return True
        delivery = record.get('_process_delivery')
        if (subscription(record) or record.get('_relay_task_id')) and not delivery:
            return True
        destination = contexts.get(delivery, {})
        return destination.get('status') in ('cancelled', 'failed') and not destination.get('_refresh_recorded')

    def correct_withdrawal(self, task_id, payload):
        from .service import ServiceError, now, uid
        request = correction_request(payload)
        with self.lock:
            caller = self._get('tasks', task_id)
            decision = self._get('decisions', request['decision_id'])
            if self._correction_owners(caller).get(decision['task_id']) is None:
                raise ServiceError(403, 'Only the execution that owns this card, or a bot it delegated the outcome to, can reopen it.')
            previous = self._previous_correction(decision, request)
            if previous is not None:
                return previous
            card = self._withdrawn_card(caller, request)
        # Read the sources without the service lock. The transaction below rejects a card that changed meanwhile.
        observed = {}
        if card['links']:
            tracker = self._pr_tracker_for(card['decision']['bot_id'])
            for url in card['links']:
                current = tracker.observe_issue(issue_url(url))
                if not isinstance(current, dict) or current.get('updated_at') != card['expected'][url]:
                    raise ServiceError(409, 'Coconut could not confirm that ' + url + ' is unchanged since the withdrawal. '
                                       'The card stays withdrawn. Read the source again before retrying.')
                recorded = card['decision'].get('_source_facts', {}).get(url) or {}
                if recorded.get('state') and current.get('state') != recorded['state']:
                    raise ServiceError(409, 'The state of ' + url + ' changed since the withdrawal. The card stays withdrawn.')
                observed[url] = current
        with self.transaction():
            caller = self._get('tasks', task_id)
            decision = self._get('decisions', request['decision_id'])
            previous = self._previous_correction(decision, request)
            if previous is not None:
                return previous
            card = self._withdrawn_card(caller, request)
            decision, task, context = card['decision'], card['task'], card['context']
            withdrawn = {key: decision.get(key) for key in (
                'status', 'revision', 'resolution', 'resolution_context', 'outcome', 'resolved_at',
                '_withdrawal', '_freshness', '_source_check')}
            correction = dict(id=uid(), kind='service_maintenance', withdrawal_context_id=context['id'],
                              withdrawn_revision=decision['revision'], restored_revision=decision['revision'] + 1,
                              caller_bot_id=caller['bot_id'], caller_task_id=caller['id'],
                              caller_turn_id=caller.get('_native_turn_id'), authority=card['authority'],
                              reason=request['reason'], source_revisions=card['expected'],
                              draft_hash=decision.get('draft_hash'), corrected_at=now(), withdrawn=withdrawn)
            # Keep the withdrawal in the card's history. Human fields and inbox holds stay as they are.
            decision.setdefault('_withdrawal_corrections', []).append(correction)
            for key in ('resolution', 'resolution_context', 'resolved_at', '_withdrawal', '_source_check'):
                decision.pop(key, None)
            decision.update(status='pending', outcome=None, revision=correction['restored_revision'])
            if observed:
                decision['_source_facts'] = dict(decision.get('_source_facts', {}), **{
                    url: value for url, value in observed.items() if '/issues/' in url})
            # The current evidence, with this card pending again, becomes the baseline. A later change starts a normal check.
            self._put('decisions', decision)
            tasks, artifacts = self._task_map(), self._all('artifacts')
            records = self._decision_records(decision, tasks, artifacts)
            fingerprint, _ = self._decision_evidence(decision, tasks, records, artifacts)
            decision['_freshness'] = dict(fingerprint=fingerprint, checked_at=correction['corrected_at'],
                                          correction_id=correction['id'])
            self._put('decisions', decision)
            self._event('decision.updated', self._public(decision), task['bot_id'], task['id'])
            self._event('decision.withdrawal_corrected', {key: correction[key] for key in (
                'id', 'kind', 'withdrawal_context_id', 'withdrawn_revision', 'restored_revision',
                'caller_bot_id', 'caller_task_id', 'authority')} | {'decision_id': decision['id']}, task['bot_id'], task['id'])
            self._refresh_bot(task['bot_id'])
            return self._correction_result(decision['id'], correction, 'restored')

    @staticmethod
    def _previous_correction(decision, request):
        """Return the receipt of the same correction if it already ran. A replay changes nothing."""
        from .service import ServiceError
        for correction in decision.get('_withdrawal_corrections', []):
            if correction['withdrawal_context_id'] != request['withdrawal_context_id']:
                continue
            if correction['withdrawn_revision'] != request['expected_revision']:
                raise ServiceError(409, 'This withdrawal was already corrected at another revision.')
            return WithdrawalCorrections._correction_result(decision['id'], correction, 'duplicate')
        return None

    @staticmethod
    def _correction_result(decision_id, correction, status):
        return dict(status=status, decision_id=decision_id, revision=correction['restored_revision'],
                    correction_id=correction['id'],
                    detail='The card is pending for the person again. Nothing was answered, approved, sent, or discarded.')
