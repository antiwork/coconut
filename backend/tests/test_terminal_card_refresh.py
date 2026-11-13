"""A card whose linked PR merged or closed after the card was made closes. Nothing locks a card for a model check."""
import threading
import unittest
from dataclasses import replace

import test_pr_source_refresh as fixture
from coconut.service import ServiceError

# The cards of the fixture are from 2000. These ends are later, so they are after each card.
MERGED_AT = '2026-11-12T10:00:00Z'


def ended(state='merged', **fields):
    return replace(fixture.fixture.observation(state), pr_state=state, ended_at=MERGED_AT, **fields)


class TerminalCardTests(unittest.TestCase):
    setUp = fixture.PRSourceRefreshTests.setUp
    tearDown = fixture.PRSourceRefreshTests.tearDown
    write = fixture.PRSourceRefreshTests.write
    run_task = fixture.PRSourceRefreshTests.run_task
    cards = fixture.PRSourceRefreshTests.cards

    def card(self, **fields):
        self.cards()
        card = self.service._get('decisions', 'older-card')
        card.update(title='Check the follow-up?', options=[dict(id='check', label='Check follow-up', answer='approve')],
                    _answers=[], **fields)
        self.service._put('decisions', card)
        return card

    def source_pass(self, observation, **options):
        self.tracker.observe = lambda url: replace(observation, url=url)
        self.service._refresh_decision_sources({}, **options)
        return self.service._get('decisions', 'older-card')

    def assert_answerable(self, card):
        self.assertEqual(card['status'], 'pending')
        self.assertNotIn('source_refreshing', self.service._public(card))
        result = self.write('POST', f"/v1/decisions/{card['id']}/answer", dict(revision=card['revision'], option_id='check'))
        self.assertEqual(result['status'], 'approved')

    def test_the_poll_closes_a_card_whose_pr_merged_while_a_slow_discussion_read_runs(self):
        card = self.card()
        entered, release = threading.Event(), threading.Event()
        self.tracker.observe = lambda url: replace(ended(), url=url)

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
            # Until the pass ends, the card stays an ordinary pending card that accepts an answer.
            self.assertNotIn('source_refreshing', self.service._public(self.service._get('decisions', card['id'])))
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        saved = self.service._get('decisions', card['id'])
        self.assertEqual((saved['status'], saved['outcome']), ('resolved', 'Pull request #42 merged on GitHub.'))
        with self.assertRaisesRegex(ServiceError, 'no longer pending'):
            self.write('POST', f"/v1/decisions/{card['id']}/answer", dict(revision=saved['revision'], option_id='check'))

    def test_failed_observation_does_not_close_card(self):
        card = self.card()
        saved = self.source_pass(fixture.fixture.observation(error='offline'))
        self.assertEqual((saved['status'], saved['revision']), ('pending', card['revision']))

    def test_a_pr_that_ended_before_the_card_needs_no_check_before_an_answer(self):
        for state in ('merged', 'closed'):
            with self.subTest(state=state):
                card = self.card(created_at='2026-11-12T11:00:00Z')
                saved = self.source_pass(ended(state))
                self.assertEqual(saved['revision'], card['revision'])
                self.assert_answerable(saved)

    def test_a_card_after_the_merge_stays_through_later_passes(self):
        card = self.card(created_at='2026-11-12T11:00:00Z')
        for _ in range(2):
            saved = self.source_pass(ended())
        self.assertEqual(saved['revision'], card['revision'])
        self.assert_answerable(saved)

    def test_a_new_revision_does_not_move_the_time_of_the_card(self):
        card = self.card(created_at='2026-11-12T11:00:00Z')
        card['revision'] += 1
        self.service._put('decisions', card)
        self.assert_answerable(self.source_pass(ended()))

    def test_a_new_head_of_a_linked_pr_does_not_close_a_card_that_is_not_its_merge_card(self):
        card = self.card()
        saved = self.source_pass(replace(fixture.fixture.observation('ready', sha='c' * 40), pr_state='open'))
        self.assertEqual(saved['revision'], card['revision'])
        self.assert_answerable(saved)

    def test_native_approval_does_not_close(self):
        self.card(action_type='native_approval')
        self.assertEqual(self.source_pass(ended())['status'], 'pending')

    def test_cancelled_source_read_does_not_close_card(self):
        self.card()
        cancelled = threading.Event()

        def observe(url):
            cancelled.set()
            return replace(ended(), url=url)
        self.tracker.observe = observe
        self.service._refresh_decision_sources({}, cancelled=cancelled)
        self.assertEqual(self.service._get('decisions', 'older-card')['status'], 'pending')

    def test_a_link_with_a_mixed_case_repository_closes(self):
        self.card(sources=[dict(kind='pull_request', title='PR', url=fixture.fixture.URL.replace('example/project', 'Example/Project'))])
        saved = self.source_pass(ended())
        self.assertEqual((saved['status'], saved['outcome']), ('resolved', 'Pull request #42 merged on GitHub.'))


if __name__ == '__main__':
    unittest.main()
