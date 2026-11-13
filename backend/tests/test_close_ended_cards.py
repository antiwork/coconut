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
    def test_a_new_head_closes_the_merge_card_and_allows_a_card_for_the_new_head(self):
        decision = self.card([], seen_hours=14, merge_pr_url=PR, _merge_pr_head=OLD_HEAD)
        self.source_pass(pr=pull('open', head=NEW_HEAD))
        self.assert_closed(decision, 'Pull request #14 has new commits.')
        # No service check binds an approval to the head. Only the close stops an approval of the old head.
        with self.assertRaisesRegex(ServiceError, 'This decision is no longer pending.'):
            self.answer(decision)
        task = self.service._get('tasks', decision['task_id'])
        new = dict(self.saved(decision), id='new-head-card', status='pending', _merge_pr_head=NEW_HEAD)
        self.service._validate_merge_decision(new, task)

    def test_the_same_head_keeps_the_merge_card(self):
        decision = self.card([], seen_hours=14, merge_pr_url=PR, _merge_pr_head=OLD_HEAD)
        self.source_pass(pr=pull('open', head=OLD_HEAD))
        self.assert_open(decision)

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

    def test_a_waiting_gateway_turn_learns_the_close(self):
        decision = self.card([ISSUE], seen_hours=14, _gateway_question=dict(turn_id='turn-1', question_id='q1', kind='clarify'))
        bridge = Mock()
        with patch.object(self.service, 'gateway_bridge', bridge, create=True):
            self.source_pass(issue_facts=issue('closed', closed_hours=2))
        self.assert_closed(decision, 'Issue #12 closed on GitHub.')
        (turn, answer), _ = bridge.answer.call_args
        self.assertEqual((turn, answer['question_id'], answer['choice']), ('turn-1', 'q1', ''))
        self.assertTrue(answer['text'].startswith('Issue #12 closed on GitHub. Coconut closed this card.'))

    def test_a_queued_check_of_the_card_stops(self):
        decision = self.card([ISSUE], seen_hours=14)
        self.service._put('contexts', dict(id='check', bot_id=decision['bot_id'], task_id=decision['task_id'],
            decision_id=decision['id'], question='Check.', status='queued', _decision_refresh=True, _decision_revision=1))
        self.source_pass(issue_facts=issue('closed', closed_hours=2))
        self.assertEqual(self.service._get('contexts', 'check')['status'], 'cancelled')
        self.service._queue_decision_refreshes()
        self.assertEqual([c['id'] for c in self.service._all('contexts')], ['check'])

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
