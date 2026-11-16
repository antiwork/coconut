"""The hourly publisher coalesces commits, retries failures, and respects successful uploads in older workflows."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

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

    def test_an_urgent_release_skips_the_cooldown_but_still_needs_changed_inputs(self):
        last = self.last(60)
        self.assertTrue(release_gate.should_release('mac', last, self.now, ['ios/Coconut/ConversationView.swift'], urgent=True)[0])
        self.assertFalse(release_gate.should_release('mac', last, self.now, [], urgent=True)[0])

    def test_begin_requires_an_explicit_urgent_option_during_the_cooldown(self):
        for urgent in (False, True):
            with self.subTest(urgent=urgent), \
                 patch('sys.argv', ['release_gate.py', 'begin', '--channel', 'mac'] + (['--urgent'] if urgent else [])), \
                 patch.object(release_gate, 'previous', return_value=dict(sha='abc', at=datetime.now(timezone.utc).isoformat(), state='success')), \
                 patch.object(release_gate.subprocess, 'check_output', return_value='def\n'), \
                 patch.object(release_gate, 'api', return_value=dict(id=123)) as api, \
                 patch.object(release_gate, 'output') as output:
                if urgent:
                    release_gate.main()
                    self.assertEqual(api.call_count, 2)
                    self.assertEqual(api.call_args_list[0].args[:2], ('POST', 'deployments'))
                    api.assert_called_with('POST', 'deployments/123/statuses', {'state': 'in_progress'})
                    output.assert_called_once_with(id=123)
                else:
                    with self.assertRaisesRegex(SystemExit, 'within the last hour'):
                        release_gate.main()
                    api.assert_not_called()

    def test_an_urgent_plan_keeps_publication_for_the_begin_command(self):
        with patch('sys.argv', ['release_gate.py', 'plan', '--channel', 'mac', '--urgent']), \
             patch.object(release_gate, 'previous', return_value=dict(sha='abc', at=datetime.now(timezone.utc).isoformat(), state='failure')), \
             patch.object(release_gate.subprocess, 'check_output', return_value='def\n'), \
             patch.object(release_gate, 'api') as api, \
             patch.object(release_gate, 'output') as output:
            release_gate.main()
            api.assert_not_called()
            output.assert_called_once_with(skip='false', sha='def', previous_sha='')

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
