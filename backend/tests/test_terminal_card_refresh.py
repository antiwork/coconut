import threading
import unittest
from dataclasses import replace

import test_pr_source_refresh as fixture
from coconut.service import ServiceError
from coconut.answer_undo import AnswerUndo
# These tests check what an answer does, so it applies at once. test_answer_undo.py tests the Undo window.
AnswerUndo.answer_undo_seconds = 0


class TerminalCardTests(unittest.TestCase):
    setUp = fixture.PRSourceRefreshTests.setUp
    tearDown = fixture.PRSourceRefreshTests.tearDown
    write = fixture.PRSourceRefreshTests.write
    run_task = fixture.PRSourceRefreshTests.run_task
    cards = fixture.PRSourceRefreshTests.cards

    def test_terminal_source_hides_card_before_slow_discussion_finishes(self):
        self.cards()
        card = self.service._get('decisions', 'older-card')
        card.update(title='Approve the appearance?', options=[dict(id='approve', label='Approve appearance', answer='approve')], _answers=[])
        self.service._put('decisions', card)
        entered, release = threading.Event(), threading.Event()
        self.tracker.observe = lambda url: replace(fixture.fixture.observation('merged'), url=url, pr_state='merged')
        def discussion(url):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Test did not release discussion')
            return 'merged discussion'
        self.tracker.observe_discussion = discussion
        worker = threading.Thread(target=self.service.refresh_pull_requests)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            card = self.service._get('decisions', 'older-card')
            self.assertEqual(card['status'], 'pending')
            self.assertTrue(self.service._public(card).get('source_refreshing'))
            visible = {d['id']: d for d in self.service.snapshot()['decisions']}
            self.assertTrue(visible[card['id']].get('source_refreshing'))
            with self.assertRaisesRegex(ServiceError, 'source changed'):
                self.write("POST", f"/v1/decisions/{card['id']}/answer", dict(revision=card['revision'], choice='approve', option_id='approve'))
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_failed_observation_does_not_hide_card(self):
        self.cards()
        self.service._invalidate_terminal_pr_cards(self.bot['id'], fixture.fixture.observation(error='offline'))
        self.assertFalse(self.service._public(self.service._get('decisions', 'older-card')).get('source_refreshing'))

    def terminal_card(self, state='merged'):
        self.cards()
        card = self.service._get('decisions', 'older-card')
        card.update(title='Check the follow-up?', options=[dict(id='check', label='Check follow-up', answer='approve')], _answers=[])
        card['_source_facts'][fixture.fixture.URL]['pr_state'] = state
        self.service._put('decisions', card)
        # Model the old service: terminal evidence and its matching fingerprint,
        # but no verified retirement check of this revision's choices.
        self.service._initialize_decision_freshness(card)
        return replace(fixture.fixture.observation(state), pr_state=state)

    def check_followup(self, observation, status='current'):
        self.service._invalidate_terminal_pr_cards(self.bot['id'], observation)
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        self.assertEqual(context['decision_id'], 'older-card')
        context['status'] = 'working'
        self.service._put('contexts', context)
        payload = dict(status=status, evidence='Verified the PR is terminal; the independent follow-up still needs a choice.')
        if status == 'revise':
            payload['revision'] = dict(title='Check the remaining follow-up?', options=[dict(id='check', label='Check follow-up')])
        self.service.refresh_decision(context['id'], payload)
        context = self.service._get('contexts', context['id'])
        context['status'] = 'completed'
        self.service._put('contexts', context)
        return self.service._get('decisions', 'older-card')

    def test_legacy_terminal_facts_require_current_revision_check(self):
        for state in ('merged', 'closed'):
            with self.subTest(state=state):
                observation = self.terminal_card(state)
                before = self.service._get('decisions', 'older-card')
                self.service._invalidate_terminal_pr_cards(self.bot['id'], observation)
                card = self.service._get('decisions', 'older-card')
                self.assertTrue(card.get('_source_recheck'))
                self.assertEqual(card['revision'], before['revision'] + 1)
                self.service._queue_decision_refreshes()
                self.assertEqual(self.service._all('contexts')[-1]['decision_id'], card['id'])
                # Finish this check before testing the other legacy terminal state.
                context = self.service._all('contexts')[-1]
                context['status'] = 'completed'
                self.service._put('contexts', context)

    def test_postmerge_followup_is_not_hidden_again(self):
        observation = self.terminal_card()
        card = self.check_followup(observation)
        self.service._invalidate_terminal_pr_cards(self.bot['id'], observation)
        saved = self.service._get('decisions', card['id'])
        self.assertFalse(saved.get('_source_recheck'))
        self.assertEqual(saved['revision'], card['revision'])
        result = self.write('POST', f"/v1/decisions/{card['id']}/answer", dict(revision=card['revision'], option_id='check'))
        self.assertEqual(result['status'], 'approved')

    def test_revised_followup_is_verified_but_later_revision_is_not(self):
        observation = self.terminal_card()
        card = self.check_followup(observation, 'revise')
        self.service._invalidate_terminal_pr_cards(self.bot['id'], observation)
        self.assertFalse(self.service._get('decisions', card['id']).get('_source_recheck'))
        card['revision'] += 1
        self.service._put('decisions', card)
        self.service._invalidate_terminal_pr_cards(self.bot['id'], observation)
        self.assertTrue(self.service._get('decisions', card['id']).get('_source_recheck'))

    def test_verified_check_does_not_cover_changed_links_or_head(self):
        observation = self.terminal_card()
        card = self.check_followup(observation)
        card['sources'].append(dict(kind='pull_request', title='Another PR', url=fixture.fixture.URL.replace('/42', '/99')))
        self.service._put('decisions', card)
        self.service._invalidate_terminal_pr_cards(self.bot['id'], observation)
        self.assertTrue(self.service._get('decisions', card['id']).get('_source_recheck'))
        card['sources'].pop()
        self.service._put('decisions', card)
        self.service._invalidate_terminal_pr_cards(self.bot['id'], replace(observation, head_sha='c' * 40))
        self.assertTrue(self.service._get('decisions', card['id']).get('_source_recheck'))

    def test_native_approval_without_refresh_support_stays_visible(self):
        self.cards()
        card = self.service._get('decisions', 'older-card')
        card['action_type'] = 'native_approval'
        self.service._put('decisions', card)
        self.service._invalidate_terminal_pr_cards(self.bot['id'], replace(fixture.fixture.observation('merged'), pr_state='merged'))
        self.assertFalse(self.service._public(self.service._get('decisions', 'older-card')).get('source_refreshing'))

    def test_cancelled_source_read_does_not_invalidate_card(self):
        self.cards()
        cancelled = threading.Event()
        def observe(url):
            cancelled.set()
            return replace(fixture.fixture.observation('merged'), url=url, pr_state='merged')
        self.tracker.observe = observe
        self.service._refresh_decision_sources({}, cancelled=cancelled)
        self.assertFalse(self.service._public(self.service._get('decisions', 'older-card')).get('source_refreshing'))

    def test_terminal_observation_matches_mixed_case_repository(self):
        self.cards()
        observation = replace(fixture.fixture.observation('merged'), pr_state='merged', url=fixture.fixture.URL.upper())
        self.service._invalidate_terminal_pr_cards(self.bot['id'], observation)
        self.assertTrue(self.service._public(self.service._get('decisions', 'older-card')).get('source_refreshing'))
