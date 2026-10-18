"""An automatic check withdrew a card in error. Maintenance reopens the same card once, without answering it.

The incident shape: a person replied on another outcome's card, the card check of a message draft read that reply as its own
answer, and it withdrew the draft. These tests exercise the service route that a worker receipt reaches (Service.emit).
"""
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import test_automatic_work as fixtures
import legacy_work_sync
from coconut.service import Service, ServiceError, digest, encoded

setUpModule, tearDownModule = legacy_work_sync.scope()

ISSUE = 'https://github.com/example/shop/issues/3730'
REVISION = '2026-10-18T18:11:41Z'
BODY = 'Prepare a plan for application-only physical sales.\n\nReturn it for review.'


class IssueTracker:
    """GitHub reads without a network, through the GitHubPRTracker record contract. `before_read` runs without the lock.

    `pull_request` makes the record a pull request, as GitHub reports it for `/issues/N` of a PR number.
    """

    def __init__(self, record):
        self.record = record
        self.pull_request = False
        self.reads = []
        self.before_read = None

    def observe(self, url):
        return SimpleNamespace(observed=False)

    def observe_discussion(self, url):
        return None

    def _issue_record(self, route, deadline, *, with_kind=False):
        self.reads.append('https://github.com/' + route)
        if self.before_read:
            self.before_read()
        facts = dict(self.record) if self.record is not None else None
        record = None if facts is None else dict(facts, number=int(route.rsplit('/', 1)[1]),
                                                  **({'pull_request': {'url': route}} if self.pull_request else {}))
        if with_kind:
            return (facts, record) if facts is not None else (None, False)
        return facts

    def observe_issue(self, url):
        from coconut.decision_sources import github_source
        return self._issue_record(github_source(url)[1], 0)


class Fixture:
    def setUp(self):
        fixtures.AutomaticWorkTests.setUp(self)
        self.issues = IssueTracker(dict(state='open', state_reason=None, title='Expand into physical goods',
                                        comments=1, updated_at=REVISION))
        self.service.pr_tracker = self.issues

    tearDown = fixtures.AutomaticWorkTests.tearDown
    start = fixtures.AutomaticWorkTests.start
    write = fixtures.AutomaticWorkTests.write

    def set_task(self, identity, **fields):
        with self.service.transaction():
            task = self.service._get('tasks', identity)
            task.update(fields)
            self.service._put('tasks', task)
        return task

    def draft(self, task_id=None, url=ISSUE, kind='issue'):
        if task_id is None:
            self.start()
            task_id = self.task['id']
        else:
            self.set_task(task_id, status='working', _native_turn_id='card-turn',
                          _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service.native_event(task_id, 'native_proposal', dict(
            key='issue-plan-request', title='Ask for an expansion plan?',
            context='Post the draft on the issue to request a plan.',
            sources=[dict(kind=kind, title='#3730', url=url)], source_revisions={url: REVISION},
            draft=dict(channel='GitHub', recipient=ISSUE, sender='owner', body=BODY)))
        self.service.running.clear()
        self.set_task(task_id, status='completed')
        return self.service._all('decisions')[-1]

    def other_outcome_reply(self, text='Gumclaw decides. And close the issue'):
        """A person replies on another outcome's card, in that outcome's chat."""
        other = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Reconcile issue 3698', 'mode': 'queue'})
        self.set_task(other['id'], status='working', _native_turn_id='other-turn',
                      _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service.native_event(other['id'], 'native_proposal', dict(
            key='other-card', title='Remove the approved label?', context='The backlog issue keeps its label.',
            options=[dict(id='remove', label='Remove')]))
        sibling = self.service._all('decisions')[-1]
        with self.service.transaction():
            shown = self.service._message(self.bot['id'], 'Reconciled #3698. It is unchanged.', task_id=other['id'])
        self.set_task(other['id'], status='completed')
        reply = self.write('POST', f'/v1/decisions/{sibling["id"]}/reply', dict(revision=sibling['revision'], text=text))
        for context in self.service._all('contexts'):
            if context['decision_id'] == sibling['id']:
                context['status'] = 'completed'
                self.service._put('contexts', context)
        message = self.service._get('messages', reply['id'])
        self.assertEqual((message['task_id'], message['decision_id']), (other['id'], sibling['id']))
        self.assertEqual(message['_visible_reply_context']['message_id'], shown['id'])
        return message, sibling

    def withdraw(self, decision, *, legacy=True):
        """Withdraw the card as the installed build did: the check took the other outcome's reply as its input."""
        unrelated, _ = self.other_outcome_reply()
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        self.assertTrue(context['_decision_refresh'])
        self.assertEqual(context['decision_id'], decision['id'])
        context.update(status='working', _human_input_ids=[unrelated['id']])
        self.service._put('contexts', context)
        self.service.refresh_decision(context['id'], dict(status='withdraw', evidence=(
            f'Original human message {unrelated["id"]} says: Gumclaw decides. And close the issue. '
            'The old Send or Discard question is obsolete.')))
        # The check ends after its result. The stale-check pass cancels it, like the incident record.
        self.service._cancel_stale_decision_refreshes()
        saved = self.service._get('decisions', decision['id'])
        if legacy:
            # Withdrawals before this change have no _withdrawal record.
            saved.pop('_withdrawal', None)
            self.service._put('decisions', saved)
        context = self.service._get('contexts', context['id'])
        self.assertEqual((context['status'], context['_refresh_recorded']), ('cancelled', True))
        self.assertEqual((saved['status'], saved['resolution'], saved['revision']), ('resolved', 'work_scope_changed', 2))
        return saved, context, unrelated

    def delegate(self, owner_id=None, name='Care', running=True):
        """A bot that the owning execution asked for help in the card's thread."""
        owner_id = owner_id or self.task['id']
        care = self.write('POST', '/v1/bots', {'name': name, 'role': 'Maintenance'})
        status = self.service._get('tasks', owner_id)['status']
        self.set_task(owner_id, status='working')
        receipt = self.service._deliver_bot_message(owner_id, {'target_bot_id': care['id'], 'text': 'Repair the withdrawn card.'})
        self.set_task(owner_id, status=status)
        if not running:
            return self.service._get('tasks', receipt['task_id'])
        return self.set_task(receipt['task_id'], status='working', _native_turn_id='care-turn')

    def request_message(self):
        return next(m for m in self.service._all('messages') if m['task_id'] == self.task['id'] and m['role'] == 'user'
                    and m.get('_human_device_id') and not m.get('decision_id'))

    def payload(self, decision, context, **changes):
        return dict(dict(decision_id=decision['id'], expected_revision=decision['revision'],
                         withdrawal_context_id=context['id'], draft_hash=decision['draft_hash'],
                         source_revisions={ISSUE: REVISION},
                         reason="The withdrawal read a reply to another outcome's card as this card's answer."), **changes)

    def correct(self, caller, payload):
        return self.service.emit(caller['id'], 'withdrawal_correction', payload)

    def assertRefused(self, caller, payload, status, decision):
        before = self.service._get('decisions', decision['id'])
        tasks, messages = self.service._all('tasks'), self.service._all('messages')
        with self.assertRaises(ServiceError) as raised:
            self.correct(caller, payload)
        self.assertEqual(raised.exception.status, status, str(raised.exception))
        self.assertEqual(self.service._get('decisions', decision['id']), before)
        self.assertEqual(self.service._all('tasks'), tasks)
        self.assertEqual(self.service._all('messages'), messages)
        return str(raised.exception)


class WithdrawalCorrectionTests(Fixture, unittest.TestCase):
    def test_delegate_restores_the_exact_card_once_and_persists(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        tasks, messages = self.service._all('tasks'), self.service._all('messages')
        reads = len(self.issues.reads)
        result = self.correct(helper, self.payload(withdrawn, context))
        self.assertEqual(result['status'], 'restored')
        self.assertEqual((result['decision_id'], result['revision']), (decision['id'], 3))
        # The service read the source during the call.
        self.assertEqual(self.issues.reads[reads:], [ISSUE])
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['revision'], 3)
        for field in ('draft', 'draft_hash', 'title', 'context', 'options', 'sources', 'task_id', 'bot_id', 'inbox_kind',
                      '_proposal_key', '_source_seen', 'created_at'):
            self.assertEqual(saved.get(field), decision.get(field), field)
        self.assertEqual(digest(encoded(saved['draft'])), decision['draft_hash'])
        self.assertEqual(saved['draft']['body'], BODY)
        for field in ('decided_at', 'decided_by', 'decided_by_name', 'selected_option_id', 'correction', 'outcome'):
            self.assertIsNone(saved.get(field), field)
        self.assertEqual(saved['_answers'], [])
        for field in ('resolution', 'resolution_context', 'resolved_at', '_withdrawal', '_source_check', '_execution_task_id'):
            self.assertNotIn(field, saved)
        # The withdrawal stays in the history, and the audit names the maintenance caller, not a person.
        correction, = saved['_withdrawal_corrections']
        self.assertEqual(correction['kind'], 'service_maintenance')
        self.assertEqual((correction['caller_bot_id'], correction['caller_task_id'], correction['authority']),
                         (helper['bot_id'], helper['id'], 'delegate'))
        self.assertEqual((correction['withdrawn_revision'], correction['restored_revision']), (2, 3))
        self.assertEqual(correction['withdrawal_context_id'], context['id'])
        self.assertEqual(correction['withdrawn']['resolution'], 'work_scope_changed')
        self.assertEqual(correction['withdrawn']['resolution_context'], withdrawn['resolution_context'])
        self.assertEqual(correction['withdrawn']['resolved_at'], withdrawn['resolved_at'])
        self.assertEqual(correction['withdrawn']['_freshness'], withdrawn['_freshness'])
        self.assertNotIn('decided_by', correction)
        # No answer, task, message, or external action.
        self.assertEqual(self.service._all('tasks'), tasks)
        self.assertEqual(self.service._all('messages'), messages)
        events = [json.loads(row[0]) for row in self.service.db.execute(
            "SELECT data FROM events WHERE type='maintenance.withdrawal_corrected'")]
        self.assertEqual([e['decision_id'] for e in events], [decision['id']])
        # A replay returns the same receipt and changes nothing.
        replay = self.correct(helper, self.payload(withdrawn, context))
        self.assertEqual(replay | {'status': 'restored'}, result)
        self.assertEqual(replay['status'], 'duplicate')
        self.assertEqual(self.service._get('decisions', decision['id']), saved)
        # The card and its audit survive a restart.
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.issues)
        self.assertEqual(self.service._get('decisions', decision['id']), saved)
        self.assertEqual(self.correct(self.service._get('tasks', helper['id']), self.payload(withdrawn, context))['status'], 'duplicate')
        self.assertEqual(self.service._get('decisions', decision['id'])['_withdrawal_corrections'], [correction])

    def test_reopened_draft_accepts_send_from_the_person(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        self.correct(self.delegate(), self.payload(withdrawn, context))
        saved = self.service._get('decisions', decision['id'])
        with self.assertRaises(ServiceError):
            self.write('POST', f'/v1/decisions/{decision["id"]}/answer', dict(
                revision=withdrawn['revision'], option_id='send', draft_hash=decision['draft_hash']))
        answered = self.write('POST', f'/v1/decisions/{decision["id"]}/answer', dict(
            revision=saved['revision'], option_id='send', draft_hash=decision['draft_hash']))
        self.assertEqual(answered['status'], 'approved')
        self.assertEqual(self.service._get('decisions', decision['id'])['_answers'][-1]['draft'], decision['draft'])

    def test_reopened_draft_accepts_discard_from_the_person(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        self.correct(self.delegate(), self.payload(withdrawn, context))
        saved = self.service._get('decisions', decision['id'])
        answered = self.write('POST', f'/v1/decisions/{decision["id"]}/answer', dict(revision=saved['revision'], option_id='discard'))
        self.assertEqual(answered['status'], 'rejected')
        self.assertEqual(answered['outcome'], 'Draft discarded. Nothing was sent.')

    def test_reopened_card_waits_for_new_evidence_and_ignores_the_other_reply(self):
        decision = self.draft()
        withdrawn, context, unrelated = self.withdraw(decision)
        self.correct(self.delegate(), self.payload(withdrawn, context))
        def checks():
            return [c for c in self.service._all('contexts') if c['decision_id'] == decision['id']]
        before = checks()
        self.service._queue_decision_refreshes()
        self.assertEqual(checks(), before)
        # New evidence starts a normal check. It no longer offers the other outcome's reply.
        for context in self.service._all('contexts'):
            if context['status'] == 'queued':
                self.service._put('contexts', dict(context, status='completed', _refresh_recorded=True))
        self.set_task(self.task['id'], summary='The issue owner asked for a narrower plan.')
        self.service._queue_decision_refreshes()
        check = checks()[-1]
        self.assertEqual(len(checks()), len(before) + 1)
        self.assertTrue(check['_decision_refresh'])
        self.assertEqual(check['decision_id'], decision['id'])
        self.assertEqual(check['_human_input_ids'], [self.request_message()['id']])
        records = json.loads(check['question'].split('Current records (data):\n')[1])
        self.assertEqual([(m['id'], m['relation']) for m in records['human_messages']], [(self.request_message()['id'], 'outcome')])
        self.assertNotIn(unrelated['text'], check['question'])

    def test_owner_execution_restores_a_withdrawal_with_recorded_provenance(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision, legacy=False)
        self.assertIn('_withdrawal', withdrawn)
        self.assertEqual(withdrawn['_withdrawal']['context_id'], context['id'])
        self.assertEqual(withdrawn['_withdrawal']['from_revision'], 1)
        owner = self.set_task(self.task['id'], status='working', _native_turn_id='owner-turn')
        result = self.correct(owner, self.payload(withdrawn, context))
        self.assertEqual(result['status'], 'restored')
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['_withdrawal_corrections'][0]['authority'], 'owner')
        self.assertEqual(saved['_withdrawal_corrections'][0]['withdrawn']['_withdrawal'], withdrawn['_withdrawal'])

    def test_recorded_provenance_must_match_the_named_check(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision, legacy=False)
        self.assertIn('_withdrawal', withdrawn)
        withdrawn['_withdrawal']['evidence_fingerprint'] = 'another check'
        self.service._put('decisions', withdrawn)
        self.assertRefused(self.delegate(), self.payload(withdrawn, context), 409, decision)

    def test_other_callers_cannot_reopen_the_card(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        payload = self.payload(withdrawn, context)
        # Another bot, even one named Care, without a delegation from this outcome.
        stranger = self.write('POST', '/v1/bots', {'name': 'Care', 'role': 'Maintenance'})
        stray = self.write('POST', f'/v1/bots/{stranger["id"]}/messages', {'text': 'Repair Desk cards', 'mode': 'queue'})
        stray = self.set_task(stray['id'], status='working', _native_turn_id='stray-turn')
        self.assertRefused(stray, payload, 403, decision)
        # The owner bot in another outcome.
        other = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Unrelated work', 'mode': 'queue'})
        other = self.set_task(other['id'], status='working', _native_turn_id='other-owner-turn')
        self.assertRefused(other, payload, 403, decision)
        # A bot delegated by another outcome of the owner bot.
        helper = self.delegate(owner_id=other['id'], name='Helper')
        self.assertRefused(helper, payload, 403, decision)
        # Forged delegation fields that the service did not set together.
        forged = self.write('POST', f'/v1/bots/{stranger["id"]}/messages', {'text': 'Repair it', 'mode': 'queue'})
        forged = self.set_task(forged['id'], status='working', _native_turn_id='forged-turn', source_task_id=self.task['id'])
        self.assertRefused(forged, payload, 403, decision)
        # The arguments cannot name a caller.
        delegate = self.delegate()
        self.assertRefused(delegate, dict(payload, caller_bot_id=self.bot['id']), 400, decision)
        # A delegate that is no longer running.
        self.set_task(delegate['id'], status='completed')
        self.assertRefused(self.service._get('tasks', delegate['id']), payload, 409, decision)

    def test_wrong_revision_context_draft_or_source_is_refused(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        # An earlier check of the same card that kept it current did not withdraw it.
        earlier = dict(context, id='earlier-check', status='completed', _decision_revision=0)
        self.service._put('contexts', earlier)
        self.assertRefused(helper, self.payload(withdrawn, context, expected_revision=1), 409, decision)
        self.assertRefused(helper, self.payload(withdrawn, context, expected_revision=3), 409, decision)
        self.assertRefused(helper, self.payload(withdrawn, context, withdrawal_context_id='another-check'), 409, decision)
        self.assertRefused(helper, self.payload(withdrawn, earlier), 409, decision)
        self.assertRefused(helper, self.payload(withdrawn, context, draft_hash='0' * 64), 409, decision)
        payload = self.payload(withdrawn, context)
        payload.pop('draft_hash')
        self.assertRefused(helper, payload, 409, decision)
        self.assertRefused(helper, self.payload(withdrawn, context, source_revisions={}), 409, decision)
        self.assertRefused(helper, self.payload(withdrawn, context, source_revisions={ISSUE: '2026-10-18T19:00:00Z'}), 409, decision)
        self.assertRefused(helper, self.payload(withdrawn, context, source_revisions={
            ISSUE: REVISION, 'https://github.com/example/shop/issues/1': REVISION}), 400, decision)
        self.assertRefused(helper, self.payload(withdrawn, context, reason=''), 400, decision)
        self.assertRefused(helper, self.payload(withdrawn, context, expected_revision='2'), 400, decision)

    def test_changed_or_unreadable_source_keeps_the_card_withdrawn(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        payload = self.payload(withdrawn, context)
        self.issues.record = dict(self.issues.record, updated_at='2026-10-18T22:00:00Z')
        self.assertRefused(helper, payload, 409, decision)
        self.issues.record = dict(self.issues.record, updated_at=REVISION, state='closed')
        self.assertRefused(helper, payload, 409, decision)
        self.issues.record = None
        self.assertRefused(helper, payload, 409, decision)

    def test_tampered_draft_is_refused(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        withdrawn['draft'] = dict(withdrawn['draft'], body='Close the issue now.')
        self.service._put('decisions', withdrawn)
        self.assertRefused(self.delegate(), self.payload(withdrawn, context), 409, decision)

    def test_human_answers_and_holds_are_kept(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        payload = self.payload(withdrawn, context)
        for field, value in (('decided_by', 'test-phone'), ('decided_at', '2026-10-18T21:00:00Z'),
                             ('selected_option_id', 'discard'), ('_answers', [dict(choice='reject', decided_by='test-phone')]),
                             ('_inbox_dismissed_at', '2026-10-18T21:00:00Z'), ('_inbox_reject_device', 'test-phone'),
                             ('_execution_task_id', self.task['id']), ('correction', 'Do not send it.')):
            with self.subTest(field=field):
                self.service._put('decisions', dict(withdrawn, **{field: value}))
                self.assertRefused(helper, payload, 409, decision)
        for status in ('approved', 'rejected', 'executed', 'dismissed', 'pending', 'failed'):
            with self.subTest(status=status):
                self.service._put('decisions', dict(withdrawn, status=status))
                self.assertRefused(helper, payload, 409, decision)
        self.service._put('decisions', dict(withdrawn, resolution='superseded'))
        self.assertRefused(helper, payload, 409, decision)
        self.service._put('decisions', dict(withdrawn, _deleted=True))
        self.assertRefused(helper, payload, 409, decision)
        self.service._put('decisions', withdrawn)
        self.assertEqual(self.correct(helper, payload)['status'], 'restored')

    def test_a_reply_on_the_card_is_kept(self):
        decision = self.draft()
        reply = self.write('POST', f'/v1/decisions/{decision["id"]}/reply', dict(revision=decision['revision'], text='Wait for Sahil.'))
        for context in self.service._all('contexts'):
            context['status'] = 'completed'
            self.service._put('contexts', context)
        self.assertEqual(self.service._get('messages', reply['id'])['decision_id'], decision['id'])
        decision = self.service._get('decisions', decision['id'])
        withdrawn, context, _ = self.withdraw(decision)
        self.assertRefused(self.delegate(), self.payload(withdrawn, context), 409, decision)

    def test_direction_in_the_outcome_after_the_card_is_kept(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        self.set_task(self.task['id'], status='working')
        steer = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Do not post anything on 3730.', 'mode': 'steer', 'task_id': self.task['id']})
        message = next(m for m in self.service._all('messages') if m['text'] == 'Do not post anything on 3730.')
        self.assertEqual(message['task_id'], self.task['id'], steer)
        self.set_task(self.task['id'], status='completed')
        self.assertRefused(helper, self.payload(withdrawn, context), 409, decision)

    def test_running_check_or_unknown_tool_result_is_refused(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        payload = self.payload(withdrawn, context)
        active = dict(context, id='later-check', status='queued', _refresh_recorded=False)
        self.service._put('contexts', active)
        self.assertRefused(helper, payload, 409, decision)
        self.service._put('contexts', dict(active, status='completed'))
        receipt = dict(id='unknown-write', task_id=self.task['id'], turn_id=context['id'], state='executing',
                       action=dict(name='terminal', arguments={'command': 'gh issue comment 3730'}), created_at=withdrawn['resolved_at'])
        self.service._put('native_receipts', receipt)
        self.assertRefused(helper, payload, 409, decision)
        self.service._put('native_receipts', dict(receipt, state='completed', result='{}'))
        self.service._put('contexts', dict(context, _recovery_unknown_tools=[dict(id='lost-write')]))
        self.assertRefused(helper, payload, 409, decision)
        self.service._put('contexts', context)
        self.assertEqual(self.correct(helper, payload)['status'], 'restored')

    def test_unrecorded_or_unrelated_check_is_refused(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        payload = self.payload(withdrawn, context)
        for changes in (dict(_refresh_recorded=False), dict(_decision_refresh=False), dict(status='working'),
                        dict(_decision_revision=2), dict(_conversation_epoch='another-epoch'), dict(task_id='another-task')):
            with self.subTest(changes=changes):
                self.service._put('contexts', dict(context, **changes))
                self.assertRefused(helper, payload, 409, decision)
        self.service._put('contexts', context)
        for changes in (dict(outcome='Another result.'), dict(resolution_context='A person closed it.'),
                        dict(_freshness=dict(withdrawn['_freshness'], context_id='another-check'))):
            with self.subTest(changes=changes):
                self.service._put('decisions', dict(withdrawn, **changes))
                self.assertRefused(helper, payload, 409, decision)

    def test_a_replacement_card_is_kept(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        replacement = dict(withdrawn, id='replacement', status='pending', revision=1, _proposal_key='newer-plan')
        replacement.pop('_freshness')
        self.service._put('decisions', replacement)
        self.assertRefused(helper, self.payload(withdrawn, context), 409, decision)
        self.service._put('decisions', dict(replacement, draft_hash='other', previous_decision_id=decision['id']))
        self.assertRefused(helper, self.payload(withdrawn, context), 409, decision)
        self.service._put('decisions', dict(replacement, draft_hash='other', _deleted=True))
        self.assertEqual(self.correct(helper, self.payload(withdrawn, context))['status'], 'restored')

    def test_paused_bot_or_cancelled_task_is_refused(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        self.set_task(self.task['id'], status='cancelled')
        # A cancelled owner also ends the delegation, so the delegate has no authority left.
        self.assertRefused(helper, self.payload(withdrawn, context), 403, decision)
        owner = self.set_task(self.task['id'], status='working', _native_turn_id='owner-turn')
        self.set_task(self.task['id'], status='cancelled')
        self.assertRefused(self.service._get('tasks', owner['id']), self.payload(withdrawn, context), 409, decision)
        self.set_task(self.task['id'], status='completed')
        self.write('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': True})
        self.assertRefused(helper, self.payload(withdrawn, context), 409, decision)

    def test_withdrawn_blocking_question_cannot_be_reopened(self):
        self.start()
        try:
            self.service.native_event(self.task['id'], 'native_question', dict(
                title='Install the test dependencies?', context='The requested fix needs local tests.',
                options=[dict(id='install', label='Install'), dict(id='leave', label='Leave')]))
        except Exception:
            pass
        self.service.running.clear()
        question = self.service._all('decisions')[-1]
        self.set_task(self.task['id'], summary='The dependency source changed.')
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        context['status'] = 'working'
        self.service._put('contexts', context)
        self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Routine setup within the request.'))
        withdrawn = self.service._get('decisions', question['id'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['_resume_decision'], question['id'])
        owner = self.set_task(self.task['id'], status='working', _native_turn_id='owner-turn')
        payload = self.payload(dict(withdrawn, draft_hash=None), context)
        payload.pop('draft_hash')
        payload['source_revisions'] = {}
        self.assertRefused(owner, payload, 409, question)

    def test_concurrent_corrections_restore_once(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        payload = self.payload(withdrawn, context)
        results = []

        def concurrent():
            self.issues.before_read = None
            results.append(self.correct(helper, payload))

        self.issues.before_read = concurrent
        second = self.correct(helper, payload)
        self.assertEqual([r['status'] for r in results], ['restored'])
        self.assertEqual(second['status'], 'duplicate')
        self.assertEqual(second['correction_id'], results[0]['correction_id'])
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['revision'], len(saved['_withdrawal_corrections'])), (3, 1))

    def test_a_human_change_during_the_source_read_wins(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()

        def dismissed():
            # Another path changes the card while the service reads GitHub without its lock.
            card = self.service._get('decisions', decision['id'])
            self.service._put('decisions', dict(card, revision=card['revision'] + 1, _inbox_dismissed_at='2026-10-18T22:00:00Z'))

        self.issues.before_read = dismissed
        with self.assertRaises(ServiceError) as raised:
            self.correct(helper, self.payload(withdrawn, context))
        self.assertEqual(raised.exception.status, 409)
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['status'], 'resolved')
        self.assertNotIn('_withdrawal_corrections', saved)

    def test_a_source_change_during_the_call_wins(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()

        def changed():
            self.issues.record = dict(self.issues.record, updated_at='2026-10-18T22:00:00Z')

        self.issues.before_read = changed
        self.assertRefused(helper, self.payload(withdrawn, context), 409, decision)


class DelegationAuthorityTests(Fixture, unittest.TestCase):
    """Only a genuine service delegation, not a return, progress reply, or hand-made source link, acts for the owner."""

    def delegated_card(self):
        """Ada delegates to Care, and Care's own task owns a withdrawn draft."""
        helper = self.delegate(running=False)
        decision = self.draft(helper['id'])
        withdrawn, context, _ = self.withdraw(decision)
        return self.service._get('tasks', helper['id']), decision, withdrawn, context

    def back_to_requester(self, helper, **options):
        """Care messages Ada, its requester. The service records this as a return to Ada's task."""
        self.set_task(helper['id'], status='working')
        receipt = self.service._deliver_bot_message(helper['id'], {'target_bot_id': self.bot['id'],
                                                                   'text': 'Status of the withdrawn draft.'}, **options)
        self.set_task(helper['id'], status='completed')
        callback = self.service._get('tasks', receipt['task_id'])
        self.assertTrue(callback['_is_return'], callback)
        self.assertEqual((callback['source_task_id'], callback['thread_id']), (helper['id'], helper['thread_id']))
        return self.set_task(callback['id'], status='working', _native_turn_id='callback-turn')

    def test_a_progress_reply_to_the_requester_cannot_reopen_the_delegates_card(self):
        helper, decision, withdrawn, context = self.delegated_card()
        callback = self.back_to_requester(helper)
        self.assertRefused(callback, self.payload(withdrawn, context), 403, decision)

    def test_a_returned_result_cannot_reopen_the_delegates_card(self):
        helper, decision, withdrawn, context = self.delegated_card()
        callback = self.back_to_requester(helper, returning=True)
        self.assertRefused(callback, self.payload(withdrawn, context), 403, decision)
        # The delegate's own execution still owns the card.
        self.set_task(helper['id'], status='working', _native_turn_id='care-turn')
        self.assertEqual(self.correct(self.service._get('tasks', helper['id']), self.payload(withdrawn, context))['status'], 'restored')

    def test_a_source_link_without_a_service_delegation_is_refused(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        payload = self.payload(withdrawn, context)
        stranger = self.write('POST', '/v1/bots', {'name': 'Care', 'role': 'Maintenance'})
        stray = self.write('POST', f'/v1/bots/{stranger["id"]}/messages', {'text': 'Repair it', 'mode': 'queue'})
        owner = self.service._get('tasks', self.task['id'])
        forged = self.set_task(stray['id'], status='working', _native_turn_id='forged-turn', source_task_id=owner['id'],
                               source_bot_id=owner['bot_id'], thread_id=owner['thread_id'],
                               conversation_bot_id=owner['conversation_bot_id'], _reply_to_task_id=owner['id'], _is_return=False)
        # Every link field matches, but no service delivery from the owner created this task.
        self.assertRefused(forged, payload, 403, decision)

    def test_a_cancelled_or_progress_only_delegation_gives_no_authority(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        payload = self.payload(withdrawn, context)
        delivery = next(json.loads(row[0]) for row in self.service.db.execute(
            "SELECT data FROM deliveries WHERE json_extract(data,'$.receipt.task_id')=?", (helper['id'],)))
        with self.service.transaction():
            self.service._put('deliveries', dict(delivery, _progress_only=True))
        self.assertRefused(helper, payload, 403, decision)
        with self.service.transaction():
            self.service._put('deliveries', delivery)
            exchange = self.service._get('exchanges', helper['exchange_id'])
            self.service._put('exchanges', dict(exchange, cancelled=True))
        self.assertRefused(helper, payload, 403, decision)
        with self.service.transaction():
            self.service._put('exchanges', exchange)
        self.assertEqual(self.correct(helper, payload)['status'], 'restored')

    def test_the_live_delegate_shape_keeps_authority_after_its_first_result(self):
        # The live Care task: a native relay delegation that already returned one result and was continued by its owner.
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        helper = self.set_task(helper['id'], _native_relay=dict(sender_bot_id=self.bot['id'], envelope_id='a' * 32),
                               _native_relay_result=dict(sender_bot_id=self.bot['id'], envelope_id='a' * 32, failed=False,
                                                         text='Same-card recovery is blocked.'))
        self.assertEqual(self.service._get('tasks', self.task['id']).get('_waiting_on', []), [helper['id']])
        self.set_task(self.task['id'], _waiting_on=[])
        result = self.correct(self.service._get('tasks', helper['id']), self.payload(withdrawn, context))
        self.assertEqual(result['status'], 'restored')
        self.assertEqual(self.service._get('decisions', decision['id'])['_withdrawal_corrections'][0]['authority'], 'delegate')

    def test_a_continuation_of_the_delegated_execution_keeps_authority(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate(running=False)
        # The delegate fails. The owner continues the same execution through the service continuation route.
        self.set_task(helper['id'], status='failed', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.set_task(self.task['id'], status='working', _waiting_on=[])
        receipt = self.service._deliver_bot_message(self.task['id'], {
            'target_bot_id': helper['bot_id'], 'text': 'Continue the repair.', 'continuation_task_id': helper['id']})
        self.set_task(self.task['id'], status='completed')
        continuation = self.service._get('tasks', receipt['task_id'])
        self.assertNotEqual(continuation['id'], helper['id'])
        self.assertEqual(continuation['_execution_session_id'], self.service._get('tasks', helper['id'])['_execution_session_id'])
        continuation = self.set_task(continuation['id'], status='working', _native_turn_id='continued-turn')
        self.assertEqual(self.correct(continuation, self.payload(withdrawn, context))['status'], 'restored')

    def test_a_later_turn_of_the_delegated_execution_keeps_authority(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate(running=False)
        with self.service.transaction():
            self.service._ensure_execution_session(self.service._get('tasks', helper['id']))
        helper = self.service._get('tasks', helper['id'])
        # A later turn of the same execution that carries no source link of its own.
        later = self.write('POST', f'/v1/bots/{helper["bot_id"]}/messages', {'text': 'Next step', 'mode': 'queue'})
        later = self.set_task(later['id'], status='working', _native_turn_id='later-turn',
                              _execution_session_id=helper['_execution_session_id'])
        self.assertIsNone(later.get('source_task_id'))
        self.assertEqual(self.correct(later, self.payload(withdrawn, context))['status'], 'restored')


class DurableToolEffectTests(Fixture, unittest.TestCase):
    """Maintenance prunes the recovery list of a settled card's checks. The receipt journal still decides."""

    def card_check(self, decision):
        """Queue this card's next normal check and mark it working."""
        saved = self.service._get('decisions', decision['id'])
        saved['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', saved)
        self.service._queue_decision_refreshes()
        check = self.service._all('contexts')[-1]
        self.assertEqual((check['decision_id'], check['status'], check['_decision_refresh']), (decision['id'], 'queued', True))
        self.service._put('contexts', dict(check, status='working'))
        return self.service._get('contexts', check['id'])

    def receipt(self, identity, turn, **fields):
        record = dict(dict(id=identity, task_id=self.task['id'], turn_id=turn, state='completed',
                           action=dict(name='terminal', arguments={'command': 'gh issue view 3730'}),
                           created_at='2026-10-18T20:00:00Z', completed_at='2026-10-18T20:00:01Z', result='{}'), **fields)
        self.service._put('native_receipts', record)
        return record

    def finish_withdrawal(self, decision, check, legacy=True):
        self.service.refresh_decision(check['id'], dict(status='withdraw', evidence='The issue owner closed this request.'))
        self.service._cancel_stale_decision_refreshes()
        withdrawn = self.service._get('decisions', decision['id'])
        if legacy:
            withdrawn.pop('_withdrawal', None)
            self.service._put('decisions', withdrawn)
        return withdrawn, self.service._get('contexts', check['id'])

    def age_outcome(self, days=10):
        """Leave every task finished and unchanged for `days`, so receipt cleanup treats the outcome as idle."""
        old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.service.transaction():
            for task in self.service._all('tasks'):
                status = task['status'] if task['status'] in ('completed', 'failed', 'cancelled') else 'completed'
                self.service._put('tasks', dict(task, status=status, created_at=old, updated_at=old))
        self.service.running.clear()

    def lost_delivery(self, legacy):
        """A process result reaches a check that is cancelled before it records anything. A retry withdraws the card."""
        decision = self.draft()
        check = self.card_check(decision)
        process = self.receipt('lost-delivery', check['id'],
                               result=json.dumps(dict(session_id='test-process', notify_on_complete=True)))
        self.service._put('contexts', dict(check, status='completed'))
        self.service._resume_process_context(check['id'], [(process, 'The tests passed.')])
        lost = self.service._all('contexts')[-1]
        self.service._put('contexts', dict(lost, status='cancelled'))
        withdrawn, retry = self.finish_withdrawal(decision, self.card_check(decision), legacy=legacy)
        return decision, withdrawn, retry, process

    def clean_up(self, kept=(), deleted=()):
        """Run the real receipt and context cleanup passes, then restart the service."""
        self.service._prune_receipts(time.time())
        present = {row[0] for row in self.service.db.execute('SELECT id FROM native_receipts')}
        self.assertTrue(set(kept) <= present, present)
        self.assertFalse(set(deleted) & present, present)
        self.service._prune_contexts()
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.issues)

    def read_check(self, legacy):
        """The withdrawing check runs one completed command. Nothing about it is unknown while its receipt exists."""
        decision = self.draft()
        check = self.card_check(decision)
        command = self.receipt('read-issue', check['id'], action=dict(name='read_file', arguments={'path': 'plan.md'}),
                               result='{"content": "plan"}')
        withdrawn, check = self.finish_withdrawal(decision, check, legacy=legacy)
        return decision, withdrawn, check, command

    def test_a_delivered_result_to_an_unrecorded_check_survives_cleanup_and_refuses(self):
        # Receipt cleanup keeps a delivered process result, so the lost delivery stays visible after both passes.
        decision, withdrawn, retry, process = self.lost_delivery(legacy=False)
        self.age_outcome()
        self.clean_up(kept=[process['id']])
        self.assertRefused(self.delegate(), self.payload(withdrawn, retry), 409, decision)

    def test_a_check_receipt_lost_to_cleanup_refuses(self):
        decision, withdrawn, check, command = self.read_check(legacy=False)
        self.assertEqual(withdrawn['_withdrawal']['receipt_ids'], [command['id']])
        self.age_outcome()
        self.clean_up(deleted=[command['id']])
        # The recorded receipt is gone, so Coconut cannot confirm that check's result any more.
        message = self.assertRefused(self.delegate(), self.payload(withdrawn, check), 409, decision)
        self.assertIn('deleted a receipt', message)

    def test_an_older_withdrawal_whose_receipts_may_be_gone_refuses(self):
        decision, withdrawn, check, command = self.read_check(legacy=True)
        self.assertNotIn('_withdrawal', withdrawn)
        self.age_outcome()
        self.clean_up(deleted=[command['id']])
        message = self.assertRefused(self.delegate(), self.payload(withdrawn, check), 409, decision)
        self.assertIn('predates its receipt record', message)

    def test_a_recorded_withdrawal_with_all_receipts_restores(self):
        decision = self.draft()
        check = self.card_check(decision)
        self.receipt('read-issue', check['id'])
        withdrawn, check = self.finish_withdrawal(decision, check, legacy=False)
        self.assertEqual(withdrawn['_withdrawal']['receipt_ids'], ['read-issue'])
        self.service._prune_contexts()
        self.assertEqual(self.correct(self.delegate(), self.payload(withdrawn, check))['status'], 'restored')

    def prune_and_reload(self):
        self.assertGreaterEqual(self.service._prune_contexts(), 1)
        self.assertFalse(any('_recovery_unknown_tools' in c for c in self.service._all('contexts')
                             if c['status'] in ('completed', 'failed', 'cancelled')))
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.issues)

    def test_an_interrupted_earlier_command_still_refuses_after_maintenance(self):
        decision = self.draft()
        first = self.card_check(decision)
        command = self.receipt('interrupted-comment', first['id'], state='executing', result=None,
                               action=dict(name='terminal', arguments={'command': 'gh issue comment 3730 --body-file plan.md'}))
        # The service restarts during the command. The check fails because the tool has no result.
        with self.service.transaction():
            self.assertTrue(self.service._block_uncertain_context(self.service._get('contexts', first['id'])))
        failed = self.service._get('contexts', first['id'])
        self.assertEqual((failed['status'], failed['error_code']), ('failed', 'tool_result_unknown'))
        self.service._put('contexts', dict(failed, _failed_at='2000-01-01T00:00:00Z'))
        # The retry inherits the unknown command, then records a withdrawal.
        retry = self.card_check(decision)
        self.assertEqual([r['id'] for r in retry['_recovery_unknown_tools']], [command['id']])
        withdrawn, retry = self.finish_withdrawal(decision, retry)
        self.assertEqual((retry['status'], retry['_refresh_recorded']), ('cancelled', True))
        self.assertIn('_recovery_unknown_tools', retry)
        self.prune_and_reload()
        self.assertNotIn('_recovery_unknown_tools', self.service._get('contexts', retry['id']))
        helper = self.delegate()
        message = self.assertRefused(helper, self.payload(withdrawn, retry), 409, decision)
        self.assertIn('no confirmed result', message)
        # Once the command has a recorded result, the card can be reopened.
        self.service._put('native_receipts', dict(command, state='completed', result='{"output": "commented"}'))
        self.assertEqual(self.correct(helper, self.payload(withdrawn, retry))['status'], 'restored')

    def test_a_process_result_awaiting_delivery_refuses(self):
        decision = self.draft()
        check = self.card_check(decision)
        process = self.receipt('waiting-tests', check['id'],
                               result=json.dumps(dict(session_id='test-process', notify_on_complete=True)))
        withdrawn, check = self.finish_withdrawal(decision, check)
        self.prune_and_reload()
        helper = self.delegate()
        self.assertRefused(helper, self.payload(withdrawn, check), 409, decision)
        # A delegated relay that still waits for its reply refuses too.
        self.service._put('native_receipts', dict(process, result='{}', _relay_task_id='relay-task'))
        self.assertRefused(helper, self.payload(withdrawn, check), 409, decision)

    def test_a_delivered_process_result_allows_the_correction(self):
        decision = self.draft()
        check = self.card_check(decision)
        process = self.receipt('delivered-tests', check['id'],
                               result=json.dumps(dict(session_id='test-process', notify_on_complete=True)))
        self.service._put('contexts', dict(check, status='completed'))
        # The service delivers the result into a continuation check, which records the withdrawal.
        self.service._resume_process_context(check['id'], [(process, 'The tests passed.')])
        callback = self.service._all('contexts')[-1]
        self.assertEqual(self.service._get('native_receipts', process['id'])['_process_delivery'], callback['id'])
        self.service._put('contexts', dict(callback, status='working'))
        withdrawn, callback = self.finish_withdrawal(decision, self.service._get('contexts', callback['id']))
        self.prune_and_reload()
        self.assertEqual(self.correct(self.delegate(), self.payload(withdrawn, callback))['status'], 'restored')

    def test_a_result_delivered_to_an_unrecorded_check_refuses(self):
        decision = self.draft()
        check = self.card_check(decision)
        process = self.receipt('lost-delivery', check['id'],
                               result=json.dumps(dict(session_id='test-process', notify_on_complete=True)))
        self.service._put('contexts', dict(check, status='completed'))
        self.service._resume_process_context(check['id'], [(process, 'The tests passed.')])
        lost = self.service._all('contexts')[-1]
        # The check that received the result was cancelled before it recorded anything.
        self.service._put('contexts', dict(lost, status='cancelled'))
        retry = self.card_check(decision)
        withdrawn, retry = self.finish_withdrawal(decision, retry)
        self.prune_and_reload()
        self.assertRefused(self.delegate(), self.payload(withdrawn, retry), 409, decision)

    def test_unknown_effects_outside_this_card_do_not_block(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        # An unconfirmed tool in the owner's own turn and in another card's check belong to other work.
        self.receipt('owner-turn-write', self.service._get('tasks', self.task['id'])['_native_turn_id'], state='executing', result=None)
        sibling_check = next(c for c in self.service._all('contexts') if c['decision_id'] != decision['id'])
        self.receipt('other-card-write', sibling_check['id'], state='executing', result=None)
        self.prune_and_reload()
        self.assertEqual(self.correct(self.delegate(), self.payload(withdrawn, context))['status'], 'restored')


class SourceBaselineTests(Fixture, unittest.TestCase):
    """The correction compares the live source with the facts that the withdrawing check saw."""

    def pull_request_withdrawal(self, legacy):
        pull = 'https://github.com/example/shop/pull/3731'
        decision = self.draft(url=pull, kind='pull_request')
        withdrawn, context, _ = self.withdraw(decision, legacy=legacy)
        # The issue API reports the same updated_at, which says nothing about a new head, review, or check.
        message = self.assertRefused(self.delegate(), self.payload(withdrawn, context, source_revisions={pull: REVISION}),
                                     409, decision)
        self.assertIn('pull request', message)

    def test_a_card_that_links_a_pull_request_is_refused(self):
        self.pull_request_withdrawal(legacy=False)

    def test_an_older_withdrawal_of_a_pull_request_card_is_refused(self):
        self.pull_request_withdrawal(legacy=True)

    def commented_withdrawal(self, legacy):
        decision = self.draft()
        # A comment arrives while the card is pending. The source refresh records it before the check withdraws.
        self.issues.record = dict(self.issues.record, updated_at='2026-10-18T19:00:00Z', comments=2)
        self.service._refresh_decision_sources({})
        self.assertEqual(self.service._get('decisions', decision['id'])['_source_facts'][ISSUE]['comments'], 2)
        withdrawn, context, _ = self.withdraw(self.service._get('decisions', decision['id']), legacy=legacy)
        baseline = (withdrawn['_source_check']['facts'] if legacy else withdrawn['_withdrawal']['source_facts'])[ISSUE]
        self.assertEqual((baseline['updated_at'], baseline['comments']), ('2026-10-18T19:00:00Z', 2))
        payload = self.payload(withdrawn, context, source_revisions={ISSUE: '2026-10-18T19:00:00Z'})
        self.assertEqual(self.correct(self.delegate(), payload)['status'], 'restored')

    def test_a_comment_before_the_withdrawal_is_part_of_the_recorded_baseline(self):
        self.commented_withdrawal(legacy=False)

    def test_a_comment_before_an_older_withdrawal_is_part_of_its_source_check(self):
        self.commented_withdrawal(legacy=True)

    def test_any_changed_issue_fact_refuses(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision, legacy=False)
        helper = self.delegate()
        for change in (dict(comments=2), dict(title='Physical goods, revised'), dict(state_reason='completed')):
            with self.subTest(change=change):
                self.issues.record = dict(self.issues.record, **change)
                self.assertRefused(helper, self.payload(withdrawn, context), 409, decision)
                self.issues.record = dict(self.issues.record, comments=1, title='Expand into physical goods', state_reason=None)
        self.assertEqual(self.correct(helper, self.payload(withdrawn, context))['status'], 'restored')

    def test_an_older_withdrawal_needs_its_recorded_source_check(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        # Without the snapshot that the check recorded, the version from card creation is no proof.
        for check in (None, dict(withdrawn['_source_check'], links=[]), dict(withdrawn['_source_check'], facts={})):
            with self.subTest(check=check):
                card = dict(withdrawn)
                card.pop('_source_check')
                if check is not None:
                    card['_source_check'] = check
                self.service._put('decisions', card)
                self.assertRefused(helper, self.payload(withdrawn, context), 409, decision)
        self.service._put('decisions', withdrawn)
        self.assertEqual(self.correct(helper, self.payload(withdrawn, context))['status'], 'restored')

    def test_a_recorded_withdrawal_without_its_source_facts_is_refused(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision, legacy=False)
        card = dict(withdrawn, _withdrawal={k: v for k, v in withdrawn['_withdrawal'].items() if k != 'source_facts'})
        self.service._put('decisions', card)
        self.assertRefused(self.delegate(), self.payload(withdrawn, context), 409, decision)


class IssueKindAndBaselineTests(Fixture, unittest.TestCase):
    """The record read proves that a linked number is an issue, and the baseline must hold every issue fact."""

    def pull_request_alias(self, legacy):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision, legacy=legacy)
        # GitHub reports a pull request for /issues/3730. Its issue facts are unchanged, so they say nothing about a new
        # head, review, or check.
        self.issues.pull_request = True
        message = self.assertRefused(self.delegate(), self.payload(withdrawn, context), 409, decision)
        self.assertIn('is a pull request', message)

    def test_a_pull_request_behind_an_issue_link_is_refused(self):
        self.pull_request_alias(legacy=False)

    def test_an_older_withdrawal_of_a_pull_request_behind_an_issue_link_is_refused(self):
        self.pull_request_alias(legacy=True)

    def test_an_unreadable_kind_refuses(self):
        from urllib.error import URLError
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision, legacy=False)
        helper = self.delegate()

        def unavailable():
            raise URLError('GitHub is unavailable.')

        self.issues.before_read = unavailable
        self.assertRefused(helper, self.payload(withdrawn, context), 409, decision)
        self.issues.before_read = None
        self.assertEqual(self.correct(helper, self.payload(withdrawn, context))['status'], 'restored')

    def test_a_partial_recorded_baseline_refuses(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision, legacy=False)
        helper = self.delegate()
        facts = withdrawn['_withdrawal']['source_facts'][ISSUE]
        self.assertIsNone(facts['state_reason'])
        for missing in ('title', 'comments', 'state_reason', 'state', 'updated_at'):
            with self.subTest(missing=missing):
                partial = {k: v for k, v in facts.items() if k != missing}
                self.service._put('decisions', dict(withdrawn, _withdrawal=dict(
                    withdrawn['_withdrawal'], source_facts={ISSUE: partial})))
                self.assertRefused(helper, self.payload(withdrawn, context), 409, decision)
        # A present null state_reason is a complete fact.
        self.service._put('decisions', withdrawn)
        self.assertEqual(self.correct(helper, self.payload(withdrawn, context))['status'], 'restored')

    def test_a_partial_legacy_source_check_refuses(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        facts = withdrawn['_source_check']['facts'][ISSUE]
        for missing in ('title', 'comments', 'state_reason'):
            with self.subTest(missing=missing):
                partial = {k: v for k, v in facts.items() if k != missing}
                self.service._put('decisions', dict(withdrawn, _source_check=dict(
                    withdrawn['_source_check'], facts={ISSUE: partial})))
                self.assertRefused(helper, self.payload(withdrawn, context), 409, decision)
        self.service._put('decisions', withdrawn)
        self.assertEqual(self.correct(helper, self.payload(withdrawn, context))['status'], 'restored')


class CorrectionSyncTests(Fixture, unittest.TestCase):
    def test_a_correction_reaches_clients_as_an_incremental_update(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate()
        after = self.service.updates(0)['cursor']
        self.correct(helper, self.payload(withdrawn, context))
        for deltas in (False, True):
            with self.subTest(collection_deltas=deltas):
                update = self.service.updates(after, collection_deltas=deltas)
                self.assertNotIn('reset', update)
                card = next(d for d in update['decisions'] if d['id'] == decision['id'])
                self.assertEqual((card['status'], card['revision']), ('pending', 3))


class RestoredCardLifecycleTests(Fixture, unittest.TestCase):
    """The reopened card follows the ordinary source refresh, card check, and answer paths."""

    def checks(self, decision):
        return [c for c in self.service._all('contexts') if c['decision_id'] == decision['id']]

    def queue_check(self, decision):
        """Run the normal queue until it offers this card a check. Other cards' checks finish without a result."""
        for _ in range(5):
            before = len(self.checks(decision))
            self.service._queue_decision_refreshes()
            if len(self.checks(decision)) > before:
                return self.checks(decision)[-1]
            queued = [c for c in self.service._all('contexts') if c['status'] in ('queued', 'working')]
            if not queued:
                return None
            for context in queued:
                self.service._put('contexts', dict(context, status='completed', _refresh_recorded=True))
        return None

    def test_source_refresh_check_and_send_after_restore(self):
        decision = self.draft()
        card = self.service._get('decisions', decision['id'])
        # The card carries a risk assessment, so a source change makes it stale.
        self.service._put('decisions', dict(card, risk_level='low', risk='The message only asks for a plan.'))
        withdrawn, context, _ = self.withdraw(self.service._get('decisions', decision['id']))
        self.correct(self.delegate(), self.payload(withdrawn, context))
        restored = self.service._get('decisions', decision['id'])
        self.assertNotIn('_source_check', restored)
        self.assertFalse(restored['assessment_stale'])
        # An unchanged source keeps the card current and asks for no check.
        self.service._refresh_decision_sources({})
        self.assertEqual(self.service._get('decisions', decision['id']), restored)
        self.assertIsNone(self.queue_check(decision))
        # A changed source makes the assessment stale, and the normal queue starts an authenticated check.
        self.issues.record = dict(self.issues.record, updated_at='2026-10-19T09:00:00Z', comments=2)
        self.service._refresh_decision_sources({})
        stale = self.service._get('decisions', decision['id'])
        self.assertTrue(stale['assessment_stale'])
        self.assertEqual(stale['_source_facts'][ISSUE]['updated_at'], '2026-10-19T09:00:00Z')
        check = self.queue_check(decision)
        self.assertIsNotNone(check)
        self.assertTrue(check['_decision_refresh'])
        self.assertEqual(check['_decision_revision'], restored['revision'])
        self.service._put('contexts', dict(check, status='working'))
        self.assertEqual(self.service.refresh_decision(check['id'], dict(status='current', evidence='Read issue 3730 and its new comment.'))['status'], 'recorded')
        checked = self.service._get('decisions', decision['id'])
        self.assertFalse(checked['assessment_stale'])
        self.assertEqual(checked['_source_check']['revision'], checked['revision'])
        self.assertEqual((checked['draft'], checked['draft_hash'], checked['status']), (decision['draft'], decision['draft_hash'], 'pending'))
        # The person sends the exact draft through the normal answer route.
        answered = self.write('POST', f'/v1/decisions/{decision["id"]}/answer', dict(
            revision=checked['revision'], option_id='send', draft_hash=decision['draft_hash']))
        self.assertEqual(answered['status'], 'approved')

    def test_send_works_before_any_new_check(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        self.correct(self.delegate(), self.payload(withdrawn, context))
        self.service._refresh_decision_sources({})
        restored = self.service._get('decisions', decision['id'])
        self.assertNotIn('_source_recheck', restored)
        answered = self.write('POST', f'/v1/decisions/{decision["id"]}/answer', dict(
            revision=restored['revision'], option_id='send', draft_hash=decision['draft_hash']))
        self.assertEqual(answered['status'], 'approved')


class DecisionHumanMessageTests(Fixture, unittest.TestCase):
    """Only this card's answers and its own outcome's direct direction count as human input for a card check."""

    def human(self, text, **fields):
        with self.service.transaction():
            message = self.service._message(self.bot['id'], text, task_id=fields.pop('task_id', self.task['id']), role='user')
            message.update(_human_device_id='test-phone', _human_source_kind='chat', **fields)
            self.service._put('messages', message)
        return message

    def steer(self, text, **data):
        """A person steers the card's own task through the normal chat route while its turn runs."""
        status = self.service._get('tasks', self.task['id'])['status']
        self.set_task(self.task['id'], status='working')
        self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', dict(text=text, mode='steer', task_id=self.task['id'], **data))
        self.set_task(self.task['id'], status=status)
        return next(m for m in reversed(self.service._all('messages')) if m['text'] == text)

    def evidence(self, decision):
        decision = self.service._get('decisions', decision['id'])
        tasks, artifacts = self.service._task_map(), self.service._all('artifacts')
        return self.service._decision_evidence(decision, tasks, self.service._decision_records(decision, tasks, artifacts), artifacts)[0]

    def queue_check(self, decision):
        """Run the normal queue until it offers this card a check. Other cards' checks finish without a result."""
        for _ in range(5):
            self.service._queue_decision_refreshes()
            queued = [c for c in self.service._all('contexts') if c['status'] in ('queued', 'working')]
            mine = [c for c in queued if c['decision_id'] == decision['id']]
            if mine:
                return mine[-1]
            if not queued:
                return None
            for context in queued:
                self.service._put('contexts', dict(context, status='completed', _refresh_recorded=True))
        return None

    def inputs(self, decision):
        decision = self.service._get('decisions', decision['id'])
        with self.service._shared_reads():
            return {m['id']: m for m in self.service._decision_human_messages(
                self.service._get('tasks', decision['task_id']), decision)}

    def test_the_other_outcome_reply_is_not_this_cards_input(self):
        decision = self.draft()
        unrelated, sibling = self.other_outcome_reply()
        self.service._queue_decision_refreshes()
        check = next(c for c in self.service._all('contexts') if c['decision_id'] == decision['id'])
        # Only the request that started this outcome remains its human input.
        self.assertEqual(check['_human_input_ids'], [self.request_message()['id']])
        self.assertNotIn(unrelated['text'], check['question'])
        self.assertNotIn(unrelated['id'], self.inputs(decision))
        # The reply still answers its own card.
        self.assertEqual(self.inputs(sibling)[unrelated['id']]['relation'], 'decision')

    def test_direct_direction_in_the_outcome_keeps_its_provenance(self):
        decision = self.draft()
        direction = self.human('Post it after Sahil replies.')
        found = self.inputs(decision)[direction['id']]
        self.assertEqual(found['relation'], 'outcome')
        self.assertEqual((found['task_id'], found['thread_id'], found['decision_id']),
                         (self.task['id'], self.task['thread_id'], None))
        shown = self.service._all('messages')
        reply = self.human('Use the shorter version.', _visible_reply_context=dict(
            message_id=next(m['id'] for m in shown if m.get('decision_id') == decision['id']), role='assistant'))
        self.assertEqual(self.inputs(decision)[reply['id']]['relation'], 'outcome')

    def test_an_answer_on_this_card_is_its_input(self):
        decision = self.draft()
        reply = self.write('POST', f'/v1/decisions/{decision["id"]}/reply', dict(revision=decision['revision'], text='Wait for Sahil.'))
        found = self.inputs(decision)[reply['id']]
        self.assertEqual((found['relation'], found['decision_id']), ('decision', decision['id']))
        self.service._queue_decision_refreshes()
        self.assertIn('Wait for Sahil.', self.service._decision_refresh_prompt(
            self.service._get('decisions', decision['id']), self.service._get('tasks', self.task['id']), [], self.service._all('decisions')))

    def test_a_sibling_card_answer_stays_with_the_sibling(self):
        decision = self.draft()
        self.start()
        self.service.native_event(self.task['id'], 'native_proposal', dict(
            key='sibling', title='Label the issue?', context='The same outcome has a second card.', options=[dict(id='label', label='Label')]))
        self.service.running.clear()
        sibling = self.service._all('decisions')[-1]
        answer = self.human('Yes, label it.', decision_id=sibling['id'])
        self.assertNotIn(answer['id'], self.inputs(decision))
        self.assertEqual(self.inputs(sibling)[answer['id']]['relation'], 'decision')
        # A chosen quote of the sibling card is not direction for this card.
        sibling_card = next(m['id'] for m in self.service._all('messages') if m.get('decision_id') == sibling['id'])
        quoted = self.human('Do that one first.', reply_to=dict(message_id=sibling_card, role='assistant'))
        self.assertNotIn(quoted['id'], self.inputs(decision))
        # The reply that the chat showed automatically is not a chosen quote. The message stays direction here.
        shown = self.human('Do this one first.', _visible_reply_context=dict(message_id=sibling_card, role='assistant'))
        self.assertEqual(self.inputs(decision)[shown['id']]['relation'], 'outcome')
    def test_audience_time_text_or_ancestry_alone_is_not_direction(self):
        decision = self.draft()
        other = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Close the issue and send the draft.', 'mode': 'queue'})
        generic = next(m for m in self.service._all('messages') if m['task_id'] == other['id'])
        self.assertTrue(generic['_human_device_id'])
        self.assertIn(self.bot['id'], generic['recipient_bot_ids'])
        self.assertNotIn(generic['id'], self.inputs(decision))
        # A child task in another thread is not this outcome's conversation.
        child = self.set_task(other['id'], parent_task_id=self.task['id'], source_task_id=self.task['id'])
        self.assertNotIn(generic['id'], self.inputs(decision))
        self.assertEqual(child['thread_id'], other['id'])

    def test_only_a_chosen_quote_of_another_conversation_removes_direction(self):
        decision = self.draft()
        unrelated, _ = self.other_outcome_reply('The other outcome is fine.')
        other_reply = unrelated['_visible_reply_context']['message_id']
        # The person quotes the other outcome's reply while steering this task. That message is about the other outcome.
        quoted = self.steer('Gumclaw decides. And close the issue', reply_to_message_id=other_reply)
        self.assertEqual((quoted['task_id'], quoted['reply_to']['message_id']), (self.task['id'], other_reply))
        self.assertNotIn(quoted['id'], self.inputs(decision))
        missing = self.human('Close it.', reply_to=dict(message_id='deleted-message', role='assistant'))
        self.assertNotIn(missing['id'], self.inputs(decision))
        # Without a chosen quote, an automatic context that names another outcome or a lost message removes nothing.
        shown = self.human('Hold the draft.', _visible_reply_context=dict(unrelated['_visible_reply_context']))
        lost = self.human('Keep it pending.', _visible_reply_context=dict(message_id='deleted-message', role='assistant'))
        found = self.inputs(decision)
        self.assertEqual((found[shown['id']]['relation'], found[lost['id']]['relation']), ('outcome', 'outcome'))
        self.assertIsNone(found[shown['id']]['quoted_message_id'])
    def test_a_chosen_quote_in_this_outcome_is_direction(self):
        decision = self.draft()
        unrelated, _ = self.other_outcome_reply('The other outcome is fine.')
        with self.service.transaction():
            own = self.service._message(self.bot['id'], 'The draft asks only for a plan.', task_id=self.task['id'])
        # The person quotes this outcome's reply through the chat route, after another outcome replied last.
        quoted = self.steer('Send it after Sahil replies.', reply_to_message_id=own['id'])
        self.assertEqual(quoted['_visible_reply_context']['message_id'], own['id'])
        found = self.inputs(decision)[quoted['id']]
        self.assertEqual((found['relation'], found['quoted_message_id']), ('outcome', own['id']))
        # A chosen quote of this card outranks an automatic context from another outcome.
        card = next(m for m in self.service._all('messages') if m.get('decision_id') == decision['id'] and m['role'] == 'assistant')
        card_quote = self.human('Send it as written.', reply_to=dict(message_id=card['id']),
                                _visible_reply_context=dict(unrelated['_visible_reply_context']))
        self.assertEqual(self.inputs(decision)[card_quote['id']]['relation'], 'outcome')
        other = self.human('Close it.', reply_to=dict(message_id=unrelated['_visible_reply_context']['message_id']))
        self.assertNotIn(other['id'], self.inputs(decision))

    def test_a_steer_after_another_outcomes_reply_reaches_the_card_check(self):
        decision = self.draft()
        unrelated, _ = self.other_outcome_reply('The other outcome is fine.')
        # A check of this card is running.
        check = self.queue_check(decision)
        self.assertIsNotNone(check)
        self.service._put('contexts', dict(check, status='working'))
        before = self.evidence(decision)
        # The person steers this outcome. The chat froze the other outcome's reply as the latest visible one.
        stop = self.steer('Stop. Do not post anything on 3730.')
        self.assertEqual(stop['task_id'], self.task['id'])
        self.assertEqual(stop['_visible_reply_context']['message_id'], unrelated['_visible_reply_context']['message_id'])
        self.assertNotIn('reply_to', stop)
        found = self.inputs(decision)[stop['id']]
        self.assertEqual((found['relation'], found['quoted_message_id']), ('outcome', None))
        self.assertNotEqual(self.evidence(decision), before)
        # The running check no longer matches its human input, so it stops.
        self.service._cancel_stale_decision_refreshes()
        self.assertEqual(self.service._get('contexts', check['id'])['status'], 'cancelled')
        # The next check offers the instruction, and the unrelated reply stays out.
        saved = self.service._get('decisions', decision['id'])
        saved['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', saved)
        following = self.queue_check(decision)
        self.assertIsNotNone(following)
        self.assertIn(stop['id'], following['_human_input_ids'])
        self.assertNotIn(unrelated['id'], following['_human_input_ids'])
        self.assertIn('Stop. Do not post anything on 3730.', following['question'])
        self.assertNotIn(unrelated['text'], following['question'])
    def test_provenance_does_not_change_the_evidence_fingerprint(self):
        decision = self.service._get('decisions', self.draft()['id'])
        self.human('Post it after Sahil replies.')
        tasks, artifacts = self.service._task_map(), self.service._all('artifacts')
        records = self.service._decision_records(decision, tasks, artifacts)
        fingerprint, _ = self.service._decision_evidence(decision, tasks, records, artifacts)
        with self.service._shared_reads():
            inputs = self.service._decision_human_messages(tasks[decision['task_id']], decision)
        self.assertTrue(inputs)
        self.assertTrue(all(set(m) == {'id', 'text', 'created_at', 'task_id', 'thread_id', 'decision_id', 'quoted_message_id', 'relation'}
                            for m in inputs))
        # The same messages without provenance hash the same. A card with unchanged inputs needs no new check.
        bare = [{k: m[k] for k in ('id', 'text', 'created_at')} for m in inputs]
        original = self.service._decision_human_messages
        self.service._decision_human_messages = lambda task, card: bare
        try:
            self.assertEqual(self.service._decision_evidence(decision, tasks, records, artifacts)[0], fingerprint)
        finally:
            self.service._decision_human_messages = original

if __name__ == '__main__':
    unittest.main()
