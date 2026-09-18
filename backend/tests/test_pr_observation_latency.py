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
        finally:
            release.set()
            worker.join(5)
