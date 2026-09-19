import threading
import unittest
from dataclasses import replace

import test_pr_source_refresh as fixture
from coconut.service import ServiceError


class TerminalCardTests(unittest.TestCase):
    setUp = fixture.PRSourceRefreshTests.setUp
    tearDown = fixture.PRSourceRefreshTests.tearDown
    write = fixture.PRSourceRefreshTests.write
    run_task = fixture.PRSourceRefreshTests.run_task
    cards = fixture.PRSourceRefreshTests.cards

    def test_terminal_source_hides_card_before_slow_discussion_finishes(self):
        self.cards()
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
            self.assertEqual(self.service._public(card)['status'], 'refreshing')
            visible = {d['id']: d for d in self.service.snapshot()['decisions']}
            self.assertEqual(visible[card['id']]['status'], 'refreshing')
            with self.assertRaisesRegex(ServiceError, 'source changed'):
                self.write("POST", f"/v1/decisions/{card['id']}/answer", dict(revision=card['revision'], choice='approve'))
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_failed_observation_does_not_hide_card(self):
        self.cards()
        self.service._invalidate_terminal_pr_cards(self.bot['id'], fixture.fixture.observation(error='offline'))
        self.assertEqual(self.service._public(self.service._get('decisions', 'older-card'))['status'], 'pending')

    def test_postmerge_followup_is_not_hidden_again(self):
        self.cards()
        card = self.service._get('decisions', 'older-card')
        card['_source_facts'][fixture.fixture.URL]['pr_state'] = 'merged'
        self.service._put('decisions', card)
        self.service._invalidate_terminal_pr_cards(self.bot['id'], replace(fixture.fixture.observation('merged'), pr_state='merged'))
        self.assertEqual(self.service._public(self.service._get('decisions', 'older-card'))['status'], 'pending')

    def test_native_approval_without_refresh_support_stays_visible(self):
        self.cards()
        card = self.service._get('decisions', 'older-card')
        card['action_type'] = 'native_approval'
        self.service._put('decisions', card)
        self.service._invalidate_terminal_pr_cards(self.bot['id'], replace(fixture.fixture.observation('merged'), pr_state='merged'))
        self.assertEqual(self.service._public(self.service._get('decisions', 'older-card'))['status'], 'pending')

    def test_cancelled_source_read_does_not_invalidate_card(self):
        self.cards()
        cancelled = threading.Event()
        def observe(url):
            cancelled.set()
            return replace(fixture.fixture.observation('merged'), url=url, pr_state='merged')
        self.tracker.observe = observe
        self.service._refresh_decision_sources({}, cancelled=cancelled)
        self.assertEqual(self.service._public(self.service._get('decisions', 'older-card'))['status'], 'pending')

    def test_terminal_observation_matches_mixed_case_repository(self):
        self.cards()
        observation = replace(fixture.fixture.observation('merged'), pr_state='merged', url=fixture.fixture.URL.upper())
        self.service._invalidate_terminal_pr_cards(self.bot['id'], observation)
        self.assertEqual(self.service._public(self.service._get('decisions', 'older-card'))['status'], 'refreshing')
