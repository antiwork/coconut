import tempfile
import threading
import unittest
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

    def test_poll_skips_deleted_bot(self):
        self.run_task()
        self.write('DELETE', f'/v1/bots/{self.bot["id"]}', {})
        self.service.refresh_pull_requests()
        self.assertEqual(self.tracker.urls, [URL])


if __name__ == '__main__':
    unittest.main()
