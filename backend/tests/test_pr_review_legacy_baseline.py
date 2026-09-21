"""Legacy reviewed heads acquire a baseline, then resume only for new evidence."""
import json
import unittest
from unittest.mock import patch

from coconut.service import Service
import test_pr_review_freshness as freshness
import test_pull_request_reviews as fixtures


class PRReviewLegacyBaselineTests(unittest.TestCase):
    setUp = fixtures.PRReviewTests.setUp
    tearDown = fixtures.PRReviewTests.tearDown
    write = fixtures.PRReviewTests.write

    def prepare(self, field):
        artifact, task = freshness.PRReviewFreshnessTests.prepare(self)
        for key in ('_reviewed_head', '_reviewed_heads', '_reviewed_states'):
            artifact.pop(key, None)
        artifact[field] = artifact['head_sha'] if field == '_reviewed_head' else [artifact['head_sha']]
        artifact.update(review_checks='review-before', required_checks_state='passed', check_failures=None)
        self.service._put('artifacts', artifact)
        return artifact, task

    def change(self, **changes):
        artifact = self.service._get('artifacts', self.artifact_id)
        artifact.update(changes)
        self.service._put('artifacts', artifact)
        self.service._queue_pr_reviews()

    def assert_signal_changes_resume_once(self, field):
        for changes in (dict(review_checks='review-after'), dict(checks_state='failed'),
                        dict(required_checks_state='failed'), dict(check_failures='new-failed-attempt')):
            with self.subTest(field=field, changes=changes):
                artifact, task = self.prepare(field)
                starts = len(self.provider.starts)
                tasks = len(self.service._all('tasks'))
                for _ in range(3):
                    self.service._queue_pr_reviews()
                    self.assertEqual(self.service._get('tasks', task['id']), task)
                self.change(**changes)
                queued = self.service._get('tasks', task['id'])
                self.assertEqual(queued['status'], 'queued')
                self.assertEqual(queued['_pr_review']['head_sha'], artifact['head_sha'])
                for key, value in changes.items():
                    self.assertEqual(queued['_pr_review'][key], value)
                # Repeated polls cannot enqueue another turn while this one is active.
                self.service._queue_pr_reviews()
                self.assertEqual(self.service._get('tasks', task['id']), queued)
                queued['status'] = 'completed'
                self.service._put('tasks', queued)
                # Neither the poll time nor elapsed hours create a new signal.
                with patch('coconut.service.now', return_value='2099-01-01T00:00:00Z'):
                    for _ in range(3):
                        self.change(checked_at='2099-01-01T00:00:00Z')
                        self.assertEqual(self.service._get('tasks', task['id']), queued)
                self.assertEqual(len(self.provider.starts), starts)
                self.assertEqual(len(self.service._all('tasks')), tasks)
                self.assertEqual(len(self.service._get('artifacts', artifact['id'])['_reviewed_states']), 1)

    def test_singular_legacy_head_reassesses_changed_signals_once(self):
        self.assert_signal_changes_resume_once('_reviewed_head')

    def test_plural_legacy_heads_reassess_changed_signals_once(self):
        self.assert_signal_changes_resume_once('_reviewed_heads')

    def test_baseline_is_persisted_once_without_rewriting_review_time_or_task(self):
        for field in ('_reviewed_head', '_reviewed_heads'):
            with self.subTest(field=field):
                artifact, task = self.prepare(field)
                self.service._queue_pr_reviews()
                saved = self.service._get('artifacts', artifact['id'])
                signal = saved.pop('_reviewed_states')
                self.assertEqual(saved, artifact)
                self.assertEqual(json.loads(signal[artifact['head_sha']]),
                                 [artifact['head_sha'], 'passed', 'passed', 'review-before', None])
                self.assertEqual(self.service._get('tasks', task['id']), task)
                with patch.object(self.service, '_put', wraps=self.service._put) as put:
                    for _ in range(3):
                        self.service._queue_pr_reviews()
                    put.assert_not_called()

    def test_migrated_baseline_survives_service_reload(self):
        for field in ('_reviewed_head', '_reviewed_heads'):
            with self.subTest(field=field):
                artifact, task = self.prepare(field)
                self.service._queue_pr_reviews()
                self.service.close()
                self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.tracker)
                self.service._queue_pr_reviews()
                self.assertEqual(self.service._get('tasks', task['id']), task)
                self.change(review_checks='review-after-reload')
                self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')

    def test_modern_signal_wins_over_legacy_peer_and_preserves_other_heads(self):
        artifact, task = self.prepare('_reviewed_heads')
        peer = dict(artifact, id='legacy-peer')
        peer.pop('_review_task_id')
        self.service._put('artifacts', peer)
        artifact['_reviewed_states'] = {
            artifact['head_sha']: json.dumps([artifact['head_sha'], 'passed', 'passed', 'review-before', None]),
            'earlier-head': 'earlier-signal',
        }
        self.service._put('artifacts', artifact)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id']), task)
        self.assertEqual(self.service._get('artifacts', artifact['id']), artifact)
        self.change(review_checks='new-modern-review')
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        self.assertEqual(self.service._get('artifacts', artifact['id'])['_reviewed_states']['earlier-head'],
                         'earlier-signal')
        self.assertEqual(self.service._get('artifacts', peer['id']), peer)

    def test_legacy_peer_baseline_is_saved_on_owner_without_losing_other_states(self):
        artifact, task = self.prepare('_reviewed_heads')
        peer = dict(artifact, id='legacy-peer')
        peer.pop('_review_task_id')
        self.service._put('artifacts', peer)
        artifact.pop('_reviewed_heads')
        artifact['_reviewed_states'] = {'earlier-head': 'earlier-signal'}
        self.service._put('artifacts', artifact)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', task['id']), task)
        saved = self.service._get('artifacts', artifact['id'])['_reviewed_states']
        self.assertIn(artifact['head_sha'], saved)
        self.assertEqual(saved['earlier-head'], 'earlier-signal')
        self.assertEqual(self.service._get('artifacts', peer['id']), peer)
        self.change(review_checks='new-peer-review')
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')

    def test_rejected_and_dismissed_decisions_block_migration_and_changed_signals(self):
        for field in ('_reviewed_head', '_reviewed_heads'):
            for migrated in (False, True):
                for status in ('rejected', 'dismissed'):
                    for headed in (False, True):
                        with self.subTest(field=field, migrated=migrated, status=status, headed=headed):
                            # Isolate the next case from the previous recorded choice.
                            self.service.db.execute("DELETE FROM decisions WHERE id='recorded-choice'")
                            artifact, task = self.prepare(field)
                            if migrated:
                                self.service._queue_pr_reviews()
                            decision = dict(id='recorded-choice', bot_id=task['bot_id'], task_id=task['id'],
                                status=status, merge_pr_url=artifact['url'], created_at='2098-01-01T00:00:00Z')
                            if headed:
                                decision['_merge_pr_head'] = artifact['head_sha']
                            self.service._put('decisions', decision)
                            before = self.service._get('artifacts', artifact['id'])
                            self.change(review_checks='review-after-decision')
                            self.assertEqual(self.service._get('tasks', task['id']), task)
                            self.assertEqual(self.service._get('decisions', decision['id']), decision)
                            self.assertEqual(self.service._get('artifacts', artifact['id']),
                                             dict(before, review_checks='review-after-decision'))

    def test_active_owner_blocks_migration_and_does_not_consume_changed_signal(self):
        for field in ('_reviewed_head', '_reviewed_heads'):
            for status in ('working', 'queued', 'needs_you'):
                with self.subTest(field=field, status=status):
                    artifact, task = self.prepare(field)
                    active = dict(task, status=status)
                    self.service._put('tasks', active)
                    self.service._queue_pr_reviews()
                    self.assertEqual(self.service._get('artifacts', artifact['id']), artifact)
                    self.assertEqual(self.service._get('tasks', task['id']), active)
                    self.service._put('tasks', task)
                    self.service._queue_pr_reviews()
                    baseline = self.service._get('artifacts', artifact['id'])
                    self.service._put('tasks', active)
                    self.change(review_checks='review-while-active')
                    self.assertEqual(self.service._get('tasks', task['id']), active)
                    self.assertEqual(self.service._get('artifacts', artifact['id']),
                                     dict(baseline, review_checks='review-while-active'))
                    self.service._put('tasks', task)
                    self.service._queue_pr_reviews()
                    self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')

    def test_pause_and_dependency_keep_changed_signal_available(self):
        for field in ('_reviewed_head', '_reviewed_heads'):
            for guard, value in (('_work_pause', {'id': 'human-stop'}), ('_background_pending', True)):
                with self.subTest(field=field, guard=guard):
                    artifact, task = self.prepare(field)
                    self.service._queue_pr_reviews()
                    baseline = self.service._get('artifacts', artifact['id'])
                    with patch.object(self.service, guard, return_value=value):
                        self.change(review_checks='review-while-blocked')
                    self.assertEqual(self.service._get('tasks', task['id']), task)
                    self.assertEqual(self.service._get('artifacts', artifact['id']),
                                     dict(baseline, review_checks='review-while-blocked'))
                    self.service._queue_pr_reviews()
                    self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
