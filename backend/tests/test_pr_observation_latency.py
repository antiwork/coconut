"""Independent GitHub observations must not block one another."""
import threading
import time
import unittest
from dataclasses import replace

import test_service_pr as fixture


class PRObservationLatencyTests(unittest.TestCase):
    setUp = fixture.PRServiceTests.setUp
    tearDown = fixture.PRServiceTests.tearDown
    write = fixture.PRServiceTests.write
    run_task = fixture.PRServiceTests.run_task

    def test_registration_wakes_monitor(self):
        self.service.pr_refresh_requested.clear()
        self.run_task()
        self.assertTrue(self.service.pr_refresh_requested.is_set())

    def test_fast_pr_updates_while_another_pr_waits(self):
        first = self.run_task()['artifacts'][0]
        second_url = fixture.URL.replace('/42', '/43')
        with self.service.transaction():
            second = dict(first, id='second-pr', url=second_url)
            self.service._put('artifacts', second)
            self.service._put('decisions', dict(id='pending-card', bot_id=self.bot['id'],
                task_id=self.task['id'], status='pending', revision=1, created_at=first['created_at'],
                action_type='question', sources=[dict(kind='pull_request', title='PR', url=second_url)]))
        blocked, release, finished = threading.Event(), threading.Event(), threading.Event()
        def observe(url):
            if url == fixture.URL:
                blocked.set()
                release.wait(3)
            return replace(fixture.observation('ready', sha='b' * 40), url=url)
        self.tracker.observe = observe
        # Production constructs a tracker per profile, including credential lookup.
        # Keep that work on the same independent path as the GitHub request.
        self.service._pr_tracker_for = lambda bot_id: self.tracker
        scheduled = []
        scheduled_ready = threading.Event()
        schedule = self.service._queue_pr_reviews
        def queue_reviews(*, artifact_ids=None):
            scheduled.append(artifact_ids)
            schedule(artifact_ids=artifact_ids)
            scheduled_ready.set()
        self.service._queue_pr_reviews = queue_reviews
        def refresh():
            try:
                self.service.refresh_pull_requests()
            finally:
                finished.set()
        worker = threading.Thread(target=refresh)
        started = time.monotonic()
        worker.start()
        try:
            self.assertTrue(blocked.wait(1))
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline:
                with self.service.lock:
                    current = self.service._get('artifacts', second['id'])
                if current['head_sha'] == 'b' * 40:
                    break
                time.sleep(.005)
            print(f'Fast PR observation: {(time.monotonic() - started) * 1000:.2f} ms')
            self.assertEqual(current['head_sha'], 'b' * 40)
            self.assertFalse(finished.is_set())
            self.assertTrue(scheduled_ready.wait(1), 'The fresh observation must schedule its review.')
            self.assertEqual(scheduled, [{second['id']}], 'Only freshly observed PRs can start a review.')
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with self.service.lock:
                    card = self.service._get('decisions', 'pending-card')
                if card.get('_source_facts', {}).get(second_url, {}).get('head_sha') == 'b' * 40:
                    break
                time.sleep(.005)
            self.assertEqual(card['_source_facts'][second_url]['head_sha'], 'b' * 40)
            self.assertFalse(finished.is_set(), 'The card must refresh before the slow PR returns.')
        finally:
            release.set()
            worker.join(5)

    def test_slow_card_discussion_does_not_delay_another_pr(self):
        first = self.run_task()['artifacts'][0]
        second_url = fixture.URL.replace('/42', '/43')
        with self.service.transaction():
            self.service._put('artifacts', dict(first, id='second-pr', url=second_url))
            self.service._put('decisions', dict(id='pending-card', bot_id=self.bot['id'],
                task_id=self.task['id'], status='pending', revision=1, created_at=first['created_at'],
                action_type='question', sources=[dict(kind='pull_request', title='PR', url=fixture.URL)]))
        blocked, release = threading.Event(), threading.Event()
        def discussion(url):
            blocked.set()
            release.wait(3)
            return {'fingerprint': 'current'}
        def observe(url):
            if url == second_url:
                blocked.wait(2)
            return replace(fixture.observation('ready', sha='b' * 40), url=url)
        self.tracker.observe = observe
        self.tracker.observe_discussion = discussion
        worker = threading.Thread(target=self.service.refresh_pull_requests)
        worker.start()
        try:
            self.assertTrue(blocked.wait(1))
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline:
                with self.service.lock:
                    current = self.service._get('artifacts', 'second-pr')
                if current['head_sha'] == 'b' * 40:
                    break
                time.sleep(.005)
            self.assertEqual(current['head_sha'], 'b' * 40)
            self.service.stopping.set()
            worker.join(.5)
            self.assertFalse(worker.is_alive(), 'Shutdown must not drain blocked HTTP reads.')
        finally:
            release.set()
            worker.join(5)

    def test_cards_share_source_reads_within_one_refresh(self):
        first = self.run_task()['artifacts'][0]
        with self.service.transaction():
            for identity in ('first-card', 'second-card'):
                self.service._put('decisions', dict(id=identity, bot_id=self.bot['id'],
                    task_id=self.task['id'], status='pending', revision=1, created_at=first['created_at'],
                    action_type='question', sources=[dict(kind='pull_request', title='PR', url=fixture.URL)]))
        calls = []
        def discussion(url):
            calls.append(url)
            time.sleep(.05)
            return {'fingerprint': 'current'}
        self.tracker.observe_discussion = discussion
        self.service.refresh_pull_requests()
        self.assertEqual(calls, [fixture.URL])
        for identity in ('first-card', 'second-card'):
            card = self.service._get('decisions', identity)
            self.assertEqual(card['_source_facts'][fixture.URL]['discussion'], {'fingerprint': 'current'})
