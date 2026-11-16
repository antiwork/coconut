"""The hourly publisher coalesces commits, retries failures, and respects successful uploads in older workflows."""
from datetime import datetime, timedelta, timezone
import unittest

import release_gate


class ReleaseGateTests(unittest.TestCase):
    now = datetime(2026, 11, 16, 21, tzinfo=timezone.utc)

    def last(self, seconds=3600, state='success'):
        return dict(sha='abc', at=(self.now - timedelta(seconds=seconds)).isoformat(), state=state)

    def test_the_cooldown_applies_to_manual_runs_and_failed_releases_too(self):
        for state in ('success', 'failure', 'in_progress'):
            self.assertFalse(release_gate.should_release('mac', self.last(3599, state), self.now, ['backend/coconut/service.py'])[0])
            self.assertTrue(release_gate.should_release('mac', self.last(3600, state), self.now, ['backend/coconut/service.py'])[0])

    def test_a_service_change_updates_the_mac_and_leaves_testflight_alone(self):
        changed = ['backend/coconut/service.py']
        self.assertTrue(release_gate.should_release('mac', self.last(), self.now, changed)[0])
        self.assertFalse(release_gate.should_release('iphone', self.last(), self.now, changed)[0])
        self.assertTrue(release_gate.should_release('iphone', self.last(), self.now, ['ios/Coconut/AppStore.swift'])[0])

    def test_no_new_code_skips_and_a_failed_publication_retries_after_an_hour(self):
        self.assertFalse(release_gate.should_release('mac', self.last(), self.now, [])[0])
        self.assertTrue(release_gate.should_release('mac', self.last(state='failure'), self.now, [])[0])
        self.assertTrue(release_gate.should_release('iphone', None, self.now, [])[0])

    def test_the_cooldown_uses_completion_rather_than_the_start_of_a_long_upload(self):
        responses = [[dict(id=1, sha='abc', created_at='2026-11-16T18:00:00Z')],
                     [dict(state='success', created_at='2026-11-16T20:59:00Z')]]
        last = release_gate.previous('mac', lambda *args: responses.pop(0))
        self.assertTrue(release_gate.cooling_down(last, self.now))

    def test_migration_counts_an_upload_even_when_a_later_step_failed(self):
        responses = [[], dict(workflow_runs=[dict(id=2, head_sha='abc', conclusion='failure')]),
                     dict(jobs=[dict(steps=[dict(name='Sign for distribution and upload', conclusion='success',
                                                completed_at='2026-11-16T20:59:00Z')])])]
        last = release_gate.previous('iphone', lambda *args: responses.pop(0))
        self.assertEqual(last['sha'], 'abc')
        self.assertTrue(release_gate.cooling_down(last, self.now))
