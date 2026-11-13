"""A card whose GitHub source ended after the card was made closes in the source pass, without a model turn."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import legacy_work_sync
import test_decision_freshness as fixture
from coconut.service import ServiceError
from turn_text import turn_input

setUpModule, tearDownModule = legacy_work_sync.scope()

ISSUE = 'https://github.com/example/shop/issues/12'
PR = 'https://github.com/example/shop/pull/14'
OLD_HEAD, NEW_HEAD = 'a' * 40, 'b' * 40


NOW = datetime.now(timezone.utc)


def ago(hours):
    """A GitHub time `hours` before one fixed instant, so that equal hours give equal times."""
    return (NOW - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


def issue(state='open', closed_hours=None, updated_hours=1):
    return dict(state=state, title='Ada asks for a refund page', updated_at=ago(updated_hours), comments=1,
                state_reason='completed' if state == 'closed' else None,
                closed_at=ago(closed_hours) if closed_hours is not None else None)


def pull(state='open', ended_hours=None, head=OLD_HEAD, observed=True):
    return SimpleNamespace(observed=observed, url=PR, pr_state=state if observed else None, head_sha=head,
                           ended_at=ago(ended_hours) if ended_hours is not None else None, base_branch='main',
                           title='Add the refund page', draft=False, checks_state='passed', required_checks_state='passed',
                           review_checks=None, check_failures=None, required_checks_observed=True)


class CloseEndedCardsTests(unittest.TestCase):
    setUp = fixture.DecisionFreshnessTests.setUp
    tearDown = fixture.DecisionFreshnessTests.tearDown
    start = fixture.DecisionFreshnessTests.start
    write = fixture.DecisionFreshnessTests.write
    question = fixture.DecisionFreshnessTests.question

    def card(self, links, *, made_hours=14, seen_hours=None, task_status='needs_you', **fields):
        """A pending card that names `links`, made `made_hours` ago. The bot read the sources `seen_hours` ago."""
        decision = self.question(old=False)
        decision.update(created_at=ago(made_hours), sources=[
            dict(kind='issue' if '/issues/' in url else 'pull_request', title='Source', url=url) for url in links])
        if seen_hours is not None:
            decision['_source_seen'] = {url: ago(seen_hours) for url in links}
        decision.update(fields)
        self.service._put('decisions', decision)
        self.set_task(decision['task_id'], task_status)
        return decision

    def set_task(self, task_id, status):
        task = self.service._get('tasks', task_id)
        task['status'] = status
        self.service._put('tasks', task)

    def source_pass(self, *, issue_facts=None, pr=None):
        tracker = SimpleNamespace(observe_issue=lambda url: issue_facts, observe=lambda url: pr,
                                  observe_discussion=lambda url: None)
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            self.service._refresh_decision_sources({})

    def saved(self, decision):
        return self.service._get('decisions', decision['id'])

    def assert_closed(self, decision, outcome):
        saved = self.saved(decision)
        self.assertEqual((saved['status'], saved.get('resolution'), saved['outcome']), ('resolved', 'source_changed', outcome))
        self.assertEqual(saved['revision'], decision['revision'] + 1)
        self.assertTrue(saved['resolved_at'])
        self.assertIsNone(saved['decided_by'])
        self.assertNotIn('_source_recheck', saved)
        return saved

    def assert_open(self, decision):
        saved = self.saved(decision)
        self.assertEqual(saved['status'], 'pending')
        self.assertNotIn('_source_recheck', saved)
        self.assertNotIn('source_refreshing', self.service._public(saved))
        return saved

    def answer(self, decision):
        saved = self.saved(decision)
        return self.write('POST', f"/v1/decisions/{saved['id']}/answer", dict(revision=saved['revision'], option_id='install'))

    def events(self, decision):
        return [json.loads(row[0]) for row in self.service.db.execute(
            "SELECT data FROM events WHERE type='decision.updated' AND json_extract(data,'$.id')=? ORDER BY seq",
            (decision['id'],))]

    # 1
    def test_an_issue_that_closes_after_the_card_closes_the_card(self):
        decision = self.card([ISSUE], seen_hours=14)
        self.source_pass(issue_facts=issue('closed', closed_hours=2))
        saved = self.assert_closed(decision, 'Issue #12 closed on GitHub.')
        self.assertEqual(self.events(decision)[-1]['status'], 'resolved')
        task = self.service._get('tasks', decision['task_id'])
        self.assertEqual((task['status'], task['_resume_decision']), ('queued', decision['id']))
        prompt, _ = turn_input(dict(operation='resume', task=task, decision=saved, history=[]))
        self.assertIn('Issue #12 closed on GitHub. Coconut closed this card. The person chose nothing.', prompt)
        self.assertIn('A closed card is no approval and no rejection.', prompt)
        # Nobody answered the card, so the turn gets no note about changed instructions, also from an older answer record.
        from coconut.native_conversation import INSTRUCTIONS_CHANGED
        prompt, _ = turn_input(dict(operation='resume', task=task, decision=saved, history=[], instructions_changed=True))
        self.assertNotIn(INSTRUCTIONS_CHANGED.strip(), prompt)
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts'), [])

    def test_the_revision_that_the_bot_read_marks_the_time_of_the_card(self):
        # The bot read the open issue 15 hours ago. The issue closed after that read, before the card time.
        decision = self.card([ISSUE], made_hours=14, seen_hours=15)
        self.source_pass(issue_facts=issue('closed', closed_hours=14.5))
        self.assert_closed(decision, 'Issue #12 closed on GitHub.')

    # 2
    def test_an_issue_that_was_closed_before_the_card_keeps_the_card(self):
        decision = self.card([ISSUE], seen_hours=20)
        self.source_pass(issue_facts=issue('closed', closed_hours=20, updated_hours=20))
        self.assert_open(decision)
        self.assertEqual(self.answer(decision)['status'], 'approved')

    def test_a_pull_request_that_merged_before_the_card_keeps_the_card(self):
        decision = self.card([PR], seen_hours=20)
        self.source_pass(pr=pull('merged', ended_hours=20))
        self.assert_open(decision)
        self.assertEqual(self.answer(decision)['status'], 'approved')

    # 3
    def test_a_linked_pull_request_that_merges_after_the_card_closes_the_card(self):
        decision = self.card([PR], seen_hours=14)
        self.source_pass(pr=pull('merged', ended_hours=1))
        self.assert_closed(decision, 'Pull request #14 merged on GitHub.')

    def test_a_linked_pull_request_that_closes_after_the_card_closes_the_card(self):
        decision = self.card([PR], seen_hours=14)
        self.source_pass(pr=pull('closed', ended_hours=1))
        self.assert_closed(decision, 'Pull request #14 closed on GitHub.')

    # 4
    def test_a_merge_card_whose_own_pull_request_merged_says_already_merged(self):
        decision = self.card([], seen_hours=14, merge_pr_url=PR, _merge_pr_head=OLD_HEAD)
        self.service._put('artifacts', dict(id='shop-pr', kind='pr', bot_id=decision['bot_id'], task_id=decision['task_id'],
                                            url=PR, title='Add the refund page', status='merged', pr_state='merged',
                                            head_sha=OLD_HEAD, observed_at=ago(0), refresh_error=None,
                                            created_at=ago(20)))
        self.source_pass(pr=pull('merged', ended_hours=1))
        self.assertEqual(self.saved(decision)['status'], 'pending')
        self.service._resolve_merged_pr_decisions()
        saved = self.saved(decision)
        self.assertEqual((saved['status'], saved['outcome'], saved.get('resolution')), ('resolved', 'Already merged.', None))
        task = self.service._get('tasks', decision['task_id'])
        self.assertEqual((task['status'], task['_resume_decision']), ('queued', decision['id']))
        prompt, _ = turn_input(dict(operation='resume', task=task, decision=saved, history=[]))
        self.assertIn('GitHub confirms this PR already merged.', prompt)

    # 5
    def test_a_new_head_keeps_the_merge_card(self):
        # Coconut does not close a merge card for a new head. The bot's own review of the PR decides about a new card.
        for head in (OLD_HEAD, NEW_HEAD):
            with self.subTest(head=head):
                decision = self.card([], seen_hours=14, merge_pr_url=PR, _merge_pr_head=OLD_HEAD)
                self.source_pass(pr=pull('open', head=head))
                self.assert_open(decision)
                self.service._put('decisions', dict(self.saved(decision), status='cancelled'))

    def test_a_merge_card_whose_own_pr_closed_after_it_closes(self):
        decision = self.card([], seen_hours=14, merge_pr_url=PR, _merge_pr_head=OLD_HEAD)
        self.source_pass(pr=pull('closed', ended_hours=1))
        self.assert_closed(decision, 'Pull request #14 closed on GitHub.')

    # 6
    def test_a_suggestion_on_an_ended_task_closes_without_a_turn(self):
        for status in ('completed', 'cancelled'):
            with self.subTest(status=status):
                decision = self.card([ISSUE], seen_hours=14, task_status=status, inbox_kind='proposal')
                tasks = len(self.service._all('tasks'))
                self.source_pass(issue_facts=issue('closed', closed_hours=2))
                self.assert_closed(decision, 'Issue #12 closed on GitHub.')
                task = self.service._get('tasks', decision['task_id'])
                self.assertEqual(task['status'], status)
                self.assertNotIn('_resume_decision', task)
                self.assertEqual(len(self.service._all('tasks')), tasks)

    # 7
    def test_a_waiting_receipt_learns_the_close(self):
        decision = self.card([ISSUE], seen_hours=14, _receipt_id='ask-call')
        self.service._put('native_receipts', dict(id='ask-call', task_id=decision['task_id'], turn_id=None,
            action=dict(name='coconut_ask', arguments={}), action_hash='ask', state='awaiting_answer', created_at=ago(14)))
        self.source_pass(issue_facts=issue('closed', closed_hours=2))
        self.assert_closed(decision, 'Issue #12 closed on GitHub.')
        record = self.service._get('native_receipts', 'ask-call')
        self.assertEqual(record['state'], 'completed')
        result = json.loads(record['result'])
        self.assertEqual((result['resolution'], result['url']), ('source_changed', ISSUE))
        self.assertTrue(result['response'].startswith('Issue #12 closed on GitHub. Coconut closed this card.'))

    def test_a_card_that_is_not_native_stays(self):
        # A question of a gateway turn is such a card: record_gateway_question makes it without _native, sources or a
        # merge PR. So no waiting gateway turn can wait for a card that the source pass closes.
        decision = self.card([ISSUE], seen_hours=14, _native=False,
                             _gateway_question=dict(turn_id='turn-1', question_id='q1', kind='approval'))
        self.source_pass(issue_facts=issue('closed', closed_hours=2))
        self.assertEqual(self.saved(decision)['status'], 'pending')

    def test_a_question_of_a_gateway_turn_has_no_source_to_close_it(self):
        task = self.service._get('tasks', self.task['id'])
        with self.service.transaction(), patch.object(self.service, '_technical_lane', return_value=None):
            self.service.record_gateway_question(task, dict(turn_id='turn-1', question_id='q1', question_kind='clarify',
                                                            text='Which page? See ' + ISSUE))
        from coconut.decision_sources import github_links
        card = self.service._all('decisions')[-1]
        self.assertEqual((card.get('_native'), github_links(card), card.get('merge_pr_url')), (None, [], None))

    def test_a_queued_or_working_check_of_the_card_stops(self):
        for status in ('queued', 'working'):
            with self.subTest(status=status):
                decision = self.card([ISSUE], seen_hours=14)
                identity = 'check-' + status
                self.service._put('contexts', dict(id=identity, bot_id=decision['bot_id'], task_id=decision['task_id'],
                    decision_id=decision['id'], question='Check.', status=status, _decision_refresh=True,
                    _decision_revision=1))
                with patch.object(self.service.provider, 'cancel_context', create=True) as cancel:
                    self.source_pass(issue_facts=issue('closed', closed_hours=2))
                self.assertEqual(self.service._get('contexts', identity)['status'], 'cancelled')
                cancel.assert_called_once_with(decision['task_id'], identity)
                self.service._queue_decision_refreshes()
                self.assertEqual(len([c for c in self.service._all('contexts') if c['status'] == 'queued']), 0)

    # 8
    def test_another_pending_card_keeps_the_task_waiting(self):
        decision = self.card([ISSUE], seen_hours=14)
        other = dict(decision, id='other-card', sources=[], _source_seen={})
        self.service._put('decisions', other)
        self.source_pass(issue_facts=issue('closed', closed_hours=2))
        self.assert_closed(decision, 'Issue #12 closed on GitHub.')
        self.assertEqual(self.saved(other)['status'], 'pending')
        task = self.service._get('tasks', decision['task_id'])
        self.assertEqual(task['status'], 'needs_you')
        self.assertNotIn('_resume_decision', task)
        # The next turn of the task names the closed card once.
        self.assertEqual(task['_closed_cards'], [dict(title=decision['title'], outcome='Issue #12 closed on GitHub.')])
        self.assertEqual(self.answer(other)['status'], 'approved')
        task = self.service._get('tasks', decision['task_id'])
        prompt, _ = turn_input(dict(operation='resume', task=task, decision=self.saved(other), history=[]))
        self.assertTrue(prompt.startswith(f'Closed card "{decision["title"]}": Issue #12 closed on GitHub. '
                                          'The person chose nothing.\n'))

    def test_the_closed_cards_note_keeps_five_and_the_dispatch_clears_it(self):
        from coconut.decision_freshness import CLOSED_CARDS_KEEP
        decision = self.card([ISSUE], seen_hours=14)
        task = self.service._get('tasks', decision['task_id'])
        task['_closed_cards'] = [dict(title=f'Card {index}', outcome='Issue #9 closed on GitHub.') for index in range(5)]
        self.service._put('tasks', task)
        self.service._put('decisions', dict(decision, id='other-card', sources=[], _source_seen={}))
        self.source_pass(issue_facts=issue('closed', closed_hours=2))
        task = self.service._get('tasks', decision['task_id'])
        self.assertEqual(len(task['_closed_cards']), CLOSED_CARDS_KEEP)
        self.assertEqual(task['_closed_cards'][-1]['outcome'], 'Issue #12 closed on GitHub.')
        self.answer(dict(decision, id='other-card'))
        delivered = {}
        self.service.provider.resume_task = lambda task, *args: delivered.setdefault('task', task)
        self.service._run_task(decision['task_id'])
        self.assertEqual(len(delivered['task']['_closed_cards']), CLOSED_CARDS_KEEP)
        self.assertNotIn('_closed_cards', self.service._get('tasks', decision['task_id']))

    def test_a_suggestion_or_a_task_that_does_not_wait_gets_no_turn(self):
        for kind, status in (('proposal', 'needs_you'), ('decision', 'working'), ('decision', 'queued')):
            with self.subTest(kind=kind, status=status):
                decision = self.card([ISSUE], seen_hours=14, inbox_kind=kind, task_status=status)
                self.source_pass(issue_facts=issue('closed', closed_hours=2))
                self.assert_closed(decision, 'Issue #12 closed on GitHub.')
                task = self.service._get('tasks', decision['task_id'])
                self.assertEqual((task['status'], task.get('_resume_decision')), (status, None))

    # 9
    def test_a_stored_lock_closes_or_goes_away(self):
        # Both cards have the lock of the old service. The PR merged 20 hours ago: after the first card, before the second.
        ended = self.card([PR], made_hours=30, seen_hours=30, _source_recheck=True, revision=2)
        kept = dict(ended, id='kept-card', _source_seen={PR: ago(14)}, created_at=ago(14))
        self.service._put('decisions', kept)
        self.source_pass(pr=pull('merged', ended_hours=20))
        self.assert_closed(ended, 'Pull request #14 merged on GitHub.')
        self.assert_open(kept)
        self.assertNotIn('source_refreshing', self.events(kept)[-1])
        self.assertEqual(self.answer(kept)['status'], 'approved')

    # 10
    def test_an_answer_during_the_read_wins(self):
        decision = self.card([ISSUE], seen_hours=14)

        def observe_issue(url):
            self.answer(decision)
            return issue('closed', closed_hours=2)
        tracker = SimpleNamespace(observe_issue=observe_issue)
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            self.service._refresh_decision_sources({})
        saved = self.saved(decision)
        self.assertEqual((saved['status'], saved.get('resolution')), ('approved', None))
        task = self.service._get('tasks', decision['task_id'])
        self.assertEqual((task['status'], task['_resume_decision']), ('queued', decision['id']))

    def test_a_close_before_the_answer_wins(self):
        decision = self.card([ISSUE], seen_hours=14)
        self.source_pass(issue_facts=issue('closed', closed_hours=2))
        with self.assertRaises(ServiceError) as raised:
            self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(revision=decision['revision'], option_id='install'))
        self.assertEqual(raised.exception.status, 409)
        self.assert_closed(decision, 'Issue #12 closed on GitHub.')

    def revise_during_read(self, decision, **revision):
        task = self.service._get('tasks', decision['task_id'])

        def observe_issue(url):
            self.service._revise_question(self.saved(decision), task, dict(
                title='Add the refund page under issue #20?', options=[dict(id='install', label='Install')], **revision),
                'The issue changed.')
            return issue('closed', closed_hours=2)
        with patch.object(self.service, '_pr_tracker_for', return_value=SimpleNamespace(observe_issue=observe_issue)):
            self.service._refresh_decision_sources({})
        return self.saved(decision)

    def test_a_revision_during_the_read_keeps_the_card_for_this_pass(self):
        saved = self.revise_during_read(self.card([ISSUE], seen_hours=14))
        self.assertEqual((saved['status'], saved['revision']), ('pending', 2))

    def test_a_link_that_a_revision_removes_during_the_read_keeps_the_card(self):
        saved = self.revise_during_read(self.card([ISSUE], seen_hours=14), sources=[])
        self.assertEqual((saved['status'], saved['sources']), ('pending', []))

    def test_a_link_that_changes_at_the_same_revision_during_the_read_keeps_the_card(self):
        # The source facts follow the same rule (test_pr_source_refresh): a stored card can change its links at one revision.
        decision = self.card([ISSUE], seen_hours=14)

        def observe_issue(url):
            self.service._put('decisions', dict(self.saved(decision), sources=[]))
            return issue('closed', closed_hours=2)
        with patch.object(self.service, '_pr_tracker_for', return_value=SimpleNamespace(observe_issue=observe_issue)):
            self.service._refresh_decision_sources({})
        self.assertEqual(self.saved(decision)['status'], 'pending')

    def test_a_link_that_a_revision_adds_counts_from_the_revision(self):
        decision = self.card([PR], seen_hours=14)
        task = self.service._get('tasks', decision['task_id'])
        self.service._revise_question(self.saved(decision), task, dict(
            title='Issue #12 closed. Add the refund page under issue #20?', options=[dict(id='install', label='Install')],
            sources=[dict(kind='pull_request', title='PR', url=PR), dict(kind='issue', title='Issue', url=ISSUE)]),
            'The issue closed.')
        revised = self.saved(decision)
        self.assertEqual(set(revised['_source_linked_at']), {ISSUE})
        # The issue closed 5 hours ago: after the card, before the revision that added it.
        self.source_pass(issue_facts=issue('closed', closed_hours=5, updated_hours=5), pr=pull('open'))
        self.assert_open(revised)

    def test_a_reply_that_waits_for_its_turn_keeps_the_card(self):
        decision = self.card([ISSUE], seen_hours=14)
        self.write('POST', f"/v1/decisions/{decision['id']}/reply", dict(text='Yes, install them.', revision=1))
        reply = self.service._all('contexts')[-1]
        for status in ('queued', 'working'):
            self.service._put('contexts', dict(reply, status=status))
            self.source_pass(issue_facts=issue('closed', closed_hours=2))
            self.assertEqual(self.saved(decision)['status'], 'pending', status)
        # After the turn of the reply, the next pass closes the card.
        self.service._put('contexts', dict(reply, status='completed'))
        self.source_pass(issue_facts=issue('closed', closed_hours=2))
        self.assert_closed(decision, 'Issue #12 closed on GitHub.')

    def test_not_found_gone_moved_and_refused_reads_close_nothing(self):
        from io import BytesIO
        from urllib.error import HTTPError
        from coconut.pr_tracking import GitHubPRTracker
        decision = self.card([ISSUE, PR], seen_hours=14)
        for code in (404, 410, 301, 403, 500):
            with self.subTest(code=code):
                tracker = GitHubPRTracker(token='bot')

                def refuse(request, timeout=None, code=code):
                    raise HTTPError(request.full_url, code, 'refused', {}, BytesIO(b'{}'))
                tracker._opener = SimpleNamespace(open=refuse)
                with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
                    self.service._refresh_decision_sources({})
                self.assert_open(decision)

    def test_the_card_keeps_no_time_of_the_close(self):
        decision = self.card([ISSUE], seen_hours=14)
        self.source_pass(issue_facts=issue('open'))
        facts = self.saved(decision)['_source_facts'][ISSUE]
        self.assertEqual(facts['state'], 'open')
        self.assertNotIn('closed_at', facts)

    # 11
    def test_nothing_closes_without_a_read_or_after_a_reopen(self):
        decision = self.card([ISSUE, PR], seen_hours=14)
        self.source_pass(issue_facts=None, pr=pull(observed=False))
        self.assert_open(decision)
        self.source_pass(issue_facts=issue('open', closed_hours=None), pr=pull('open'))
        self.assert_open(decision)
        self.assertEqual(self.answer(decision)['status'], 'approved')

    # 12
    def test_a_new_card_about_the_ended_source_stays(self):
        from coconut import work_sources
        decision = self.card([ISSUE], seen_hours=14)
        self.source_pass(issue_facts=issue('closed', closed_hours=2))
        closed = self.assert_closed(decision, 'Issue #12 closed on GitHub.')
        item = dict(id=ISSUE, updated_at=ago(2))
        decisions = {closed['id']: closed}
        cards = work_sources.source_cards([item], decisions)
        self.assertFalse(work_sources.card_covers(item, {}, decisions, cards, {}))
        self.assertFalse(work_sources.dismissal_covers(item, cards))
        # The bot reads the closed issue and asks again. The read is not older than the close.
        new = self.card([ISSUE], made_hours=1, seen_hours=2)
        for _ in range(2):
            self.source_pass(issue_facts=issue('closed', closed_hours=2, updated_hours=2))
            self.assert_open(new)

    # 13
    def test_the_five_shapes_of_the_owner_store(self):
        template = self.card([], seen_hours=None)
        task = self.service._get('tasks', template['task_id'])
        cards = {}
        both = [ISSUE, PR]
        for name, status, kind, links, made, lock in (
                ('suggestion-1', 'completed', 'proposal', both, 24 * 11, True),
                ('suggestion-2', 'completed', 'proposal', both, 24 * 10, True),
                ('suggestion-3', 'completed', 'proposal', both, 24 * 9, True),
                ('decision', 'needs_you', 'decision', both, 24 * 4, True),
                ('question', 'needs_you', 'decision', [ISSUE], 14, False)):
            self.service._put('tasks', dict(task, id=name + '-task', status=status))
            card = dict(template, id=name, task_id=name + '-task', inbox_kind=kind, created_at=ago(made), revision=2,
                        sources=[dict(kind='issue' if '/issues/' in url else 'pull_request', title='Source', url=url)
                                 for url in links],
                        _source_seen={url: ago(made) for url in links})
            if lock:
                card['_source_recheck'] = True
            self.service._put('decisions', card)
            cards[name] = card
        self.service._put('decisions', dict(template, status='resolved'))
        self.source_pass(issue_facts=issue('closed', closed_hours=1), pr=pull('merged', ended_hours=48))
        for name in ('suggestion-1', 'suggestion-2', 'suggestion-3'):
            self.assert_closed(cards[name], 'Issue #12 closed on GitHub.')
            self.assertEqual(self.service._get('tasks', name + '-task')['status'], 'completed')
        for name in ('decision', 'question'):
            self.assert_closed(cards[name], 'Issue #12 closed on GitHub.')
            owner = self.service._get('tasks', name + '-task')
            self.assertEqual((owner['status'], owner['_resume_decision']), ('queued', name))
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts'), [])


if __name__ == '__main__':
    unittest.main()
