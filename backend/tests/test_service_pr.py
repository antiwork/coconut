import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.pr_tracking import PRObservation
from coconut.service import Service


URL = 'https://github.com/example/project/pull/42'
SHA = 'a' * 40


def observation(state='checks_running', *, error=None, sha=SHA, date='2026-09-08T00:00:00Z'):
    return PRObservation(None if error else state, sha, 'Verified PR title', URL, error or 'Checks are queued or running.', date, error is None)


class Tracker:
    def __init__(self, *observations):
        self.observations = list(observations)
        self.urls = []
        self.before_read = None
        self.latest = {}

    def observe(self, url):
        self.urls.append(url)
        if self.before_read:
            self.before_read()
        if self.observations:
            self.latest[url] = self.observations.pop(0)
        return self.latest[url]

    def observe_discussion(self, url):
        return None

    def observe_issue(self, url):
        return None


class Provider:
    name = 'fixture-pr-tests'
    ready = True
    model = 'fixture'

    def __init__(self):
        self.payload = dict(kind='pr', url=URL, title='Untrusted title', status='merged')
        self.repeat = False

    def start_task(self, task, bot, history, emit):
        emit('artifact', self.payload)
        if self.repeat:
            emit('artifact', self.payload)
        emit('completed', {'summary': 'The existing PR is tracked.'})

    def cancel_task(self, task_id):
        pass


class PRServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = Provider()
        self.tracker = Tracker(observation())
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.tracker)
        self.bot = self.write('POST', '/v1/bots', {'name': 'Ada', 'role': 'Review'})
        self.task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Track an existing PR', 'mode': 'queue'})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), 'test-phone')

    def run_task(self):
        self.service._run_task(self.task['id'])
        return self.service.snapshot()

    def test_provider_artifact_uses_only_github_observed_state(self):
        artifact = self.run_task()['artifacts'][0]
        self.assertEqual(artifact['status'], 'checks_running')
        self.assertEqual(artifact['title'], 'Verified PR title')
        self.assertEqual(artifact['head_sha'], SHA)
        self.assertEqual(artifact['observed_at'], '2026-09-08T00:00:00Z')
        self.assertIsNone(artifact['refresh_error'])
        self.assertEqual(self.tracker.urls, [URL])

    def test_repeated_artifact_emission_keeps_one_card(self):
        self.provider.repeat = True
        state = self.run_task()
        self.assertEqual(len(state['artifacts']), 1)
        self.assertEqual(len([m for m in state['messages'] if m['kind'] == 'artifact']), 1)
        self.assertEqual(self.tracker.urls, [URL])

    def test_invalid_url_never_reaches_tracker(self):
        self.provider.payload['url'] = 'http://127.0.0.1/private'
        state = self.run_task()
        self.assertEqual(state['artifacts'], [])
        self.assertEqual(self.tracker.urls, [])
        self.assertEqual(state['tasks'][0]['status'], 'failed')

    def test_initial_observation_error_cannot_create_unverified_card(self):
        self.tracker.observations = [observation(error='GitHub refresh failed (HTTP 403).')]
        state = self.run_task()
        self.assertEqual(state['artifacts'], [])
        self.assertEqual(state['tasks'][0]['status'], 'failed')
        self.assertIn('HTTP 403', state['tasks'][0]['summary'])

    def test_network_read_releases_service_lock(self):
        acquired = threading.Event()

        def independent_read():
            self.service.snapshot()
            acquired.set()

        def probe():
            reader = threading.Thread(target=independent_read)
            reader.start()
            self.assertTrue(acquired.wait(1), 'The GitHub read holds the service lock.')
            reader.join()

        self.tracker.before_read = probe
        self.assertEqual(len(self.run_task()['artifacts']), 1)

    def test_cancellation_during_read_prevents_artifact_registration(self):
        self.tracker.before_read = lambda: self.write('POST', f'/v1/tasks/{self.task["id"]}/cancel', {})
        state = self.run_task()
        self.assertEqual(state['tasks'][0]['status'], 'cancelled')
        self.assertEqual(state['artifacts'], [])

    def test_poll_error_preserves_head_state_and_success_time(self):
        state = self.run_task()
        before = state['artifacts'][0]
        self.tracker.observations = [observation(error='GitHub is unreachable.', sha='b' * 40, date='2026-09-08T00:01:00Z')]
        self.service.refresh_pull_requests()
        artifact = self.service.snapshot()['artifacts'][0]
        self.assertEqual(artifact['id'], before['id'])
        self.assertEqual(artifact['status'], before['status'])
        self.assertEqual(artifact['head_sha'], before['head_sha'])
        self.assertEqual(artifact['observed_at'], before['observed_at'])
        self.assertEqual(artifact['checked_at'], '2026-09-08T00:01:00Z')
        self.assertEqual(artifact['refresh_error'], 'GitHub is unreachable.')
        self.assertEqual(artifact['preview'], 'GitHub is unreachable.')
        self.assertTrue(any(e['type'] == 'artifact.updated' for e in self.service.events(state['cursor'])['events']))

    def test_poll_that_moves_only_check_times_publishes_an_observed_card(self):
        state = self.run_task()
        before = state['artifacts'][0]
        self.tracker.observations = [observation(date='2026-09-08T00:01:00Z')]
        self.service.refresh_pull_requests()
        events = [e for e in self.service.events(state['cursor'])['events'] if e['type'].startswith('artifact.')]
        self.assertEqual([e['type'] for e in events], ['artifact.observed'])
        page = self.service.updates(state['cursor'], collection_deltas=True, native_events=True)
        self.assertEqual([(a['id'], a['observed_at'], a['checked_at']) for a in page['artifacts']],
                         [(before['id'], '2026-09-08T00:01:00Z', '2026-09-08T00:01:00Z')])
        cursor = page['cursor']
        self.tracker.observations = [observation('checks_passed', date='2026-09-08T00:02:00Z')]
        self.service.refresh_pull_requests()
        self.assertEqual([e['type'] for e in self.service.events(cursor)['events'] if e['type'].startswith('artifact.')],
                         ['artifact.updated'])

    def test_restart_keeps_card_and_poll_persists_merge_without_duplicate_message(self):
        before = self.run_task()['artifacts'][0]
        self.service.close()
        tracker = Tracker(observation('merged', date='2026-09-08T00:01:00Z'))
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=tracker)
        self.assertEqual(self.service.snapshot()['artifacts'][0]['id'], before['id'])
        self.service.refresh_pull_requests()
        state = self.service.snapshot()
        self.assertEqual(state['artifacts'][0]['status'], 'merged')
        self.assertEqual(len([m for m in state['messages'] if m['kind'] == 'artifact']), 1)
        self.service.refresh_pull_requests()
        self.assertEqual(tracker.urls, [URL])

    def test_shared_github_login_serves_every_bot(self):
        from coconut.pr_tracking import GitHubPRTracker
        self.service.pr_tracker = None
        with patch.object(type(self.service.github), 'token', return_value='shared-token'), \
             patch.object(GitHubPRTracker, 'for_profile') as own_login:
            tracker = self.service._pr_tracker_for(self.bot['id'])
        self.assertEqual(tracker._token, 'shared-token')
        own_login.assert_not_called()
        self.service._profile_trackers.clear()
        with patch.object(type(self.service.github), 'token', return_value=''), \
             patch.object(GitHubPRTracker, 'for_profile', return_value=GitHubPRTracker(token='bot-token')) as own_login:
            self.assertEqual(self.service._pr_tracker_for(self.bot['id'])._token, 'bot-token')
        own_login.assert_called_once()
        # A new shared login takes effect at once, although the bot's own tracker is still cached.
        with patch.object(type(self.service.github), 'token', return_value='new-shared-token'):
            self.assertEqual(self.service._pr_tracker_for(self.bot['id'])._token, 'new-shared-token')

    def test_retried_github_disconnect_does_not_run_again(self):
        key = str(uuid4())
        with patch.object(type(self.service.github), 'disconnect', return_value=dict(connected=False)) as disconnect, \
             patch.object(type(self.service.github), 'status', return_value=dict(connected=True)):
            self.assertEqual(self.service.mutate('POST', '/v1/settings/github/disconnect', {}, key, 'phone'), dict(connected=False))
            # A retry after a lost response returns the current state. A newer connection from another phone stays.
            self.assertEqual(self.service.mutate('POST', '/v1/settings/github/disconnect', {}, key, 'phone'), dict(connected=True))
        disconnect.assert_called_once()

    def test_overlapping_github_retries_run_the_action_once(self):
        key, started, release = str(uuid4()), threading.Event(), threading.Event()
        def slow_disconnect(connection):
            started.set()
            release.wait(5)
            return dict(connected=False)
        results = []
        with patch.object(type(self.service.github), 'disconnect', autospec=True, side_effect=slow_disconnect) as disconnect, \
             patch.object(type(self.service.github), 'status', return_value=dict(connected=False)):
            first = threading.Thread(target=lambda: results.append(
                self.service.mutate('POST', '/v1/settings/github/disconnect', {}, key, 'phone')))
            first.start()
            self.assertTrue(started.wait(5))
            second = threading.Thread(target=lambda: results.append(
                self.service.mutate('POST', '/v1/settings/github/disconnect', {}, key, 'phone')))
            second.start()
            release.set()
            first.join(5)
            second.join(5)
        self.assertEqual(disconnect.call_count, 1)
        self.assertEqual(results, [dict(connected=False), dict(connected=False)])

    def test_poll_skips_deleted_bot(self):
        self.run_task()
        self.write('DELETE', f'/v1/bots/{self.bot["id"]}', {})
        self.service.refresh_pull_requests()
        self.assertEqual(self.tracker.urls, [URL])


if __name__ == '__main__':
    unittest.main()
