import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import parse_qs

from coconut.hermes_worker import conversation_input
from coconut.pr_tracking import GitHubPRTracker
from coconut.service import YieldForDecision
import test_decision_sources as source_tests


URL = 'https://github.com/example/project/issues/42'
APPROVED = '2026-09-23T11:46:31Z'


def comment(identity, login, created_at, body):
    return dict(id=identity, user=dict(login=login, type='User'), created_at=created_at, updated_at=created_at,
                html_url=f'{URL}#issuecomment-{identity}', body=body)


class ThreadTracker(GitHubPRTracker):
    """Serve one GitHub thread from memory. Record each read and whether the service lock was free."""

    def __init__(self, service):
        super().__init__(token='')
        self.service = service
        self.updated_at = APPROVED
        self.comments = []
        self.paths = []
        self.lock_free = []
        self.offline = False

    def add(self, *comments):
        self.comments.extend(comments)
        self.updated_at = comments[-1]['updated_at']

    def _get(self, path, deadline):
        acquired = threading.Event()
        def check():
            with self.service.lock:
                acquired.set()
        reader = threading.Thread(target=check)
        reader.start()
        self.lock_free.append(acquired.wait(1))
        reader.join(1)
        self.paths.append(path)
        if self.offline:
            raise URLError('offline')
        route, _, query = path.partition('?')
        if route == '/repos/example/project/issues/42':
            return dict(state='open', title='Record correction', updated_at=self.updated_at,
                        comments=len(self.comments), state_reason=None)
        if route == '/repos/example/project/issues/42/comments':
            since = parse_qs(query)['since'][0]
            # GitHub includes comments updated at the exact `since` time.
            return [c for c in self.comments if c['updated_at'] >= since]
        raise AssertionError('Unexpected GitHub read: ' + path)


class SourceActivityTests(unittest.TestCase):
    setUp = source_tests.DecisionSourceTests.setUp
    tearDown = source_tests.DecisionSourceTests.tearDown
    event = source_tests.DecisionSourceTests.event
    write = source_tests.DecisionSourceTests.write

    def approve_card(self):
        self.tracker = ThreadTracker(self.service)
        self.patcher = patch.object(self.service, '_pr_tracker_for', return_value=self.tracker)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        payload = source_tests.DecisionSourcePreflightTests.payload(self)
        with self.assertRaises(YieldForDecision):
            self.event('native_question', payload)
        decision = self.service.snapshot()['decisions'][0]
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", {'option_id': 'fix', 'revision': decision['revision']})
        self.assertEqual(self.service._get('tasks', self.task['id'])['_resume_decision'], decision['id'])
        self.tracker.paths.clear()
        self.tracker.lock_free.clear()
        return decision

    url = URL
    revision = APPROVED

    def turn(self):
        """Run one turn and return the task and prompt that Hermes receives."""
        self.service.provider.resume_task = Mock()
        self.service.provider.start_task = Mock()
        self.service._run_task(self.task['id'])
        if self.service.provider.resume_task.called:
            args = self.service.provider.resume_task.call_args.args
            request = dict(operation='resume', task=args[0], history=[], decision=args[3])
        else:
            args = self.service.provider.start_task.call_args.args
            request = dict(operation='start', task=args[0], history=[])
        return args[0], conversation_input(request)[0]

    def requeue(self):
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'queued'
            self.service._put('tasks', task)

    def test_comments_after_approval_reach_the_next_turn_once(self):
        self.approve_card()
        self.tracker.add(comment(7, 'reviewer', '2026-09-23T12:10:00Z', 'This must handle 10x the current volume.'),
                         comment(8, 'helper-bot', '2026-09-23T12:15:00Z', 'Noted the scale requirement.'))
        task, prompt = self.turn()
        activity = task['_source_activity']
        self.assertEqual([c['author'] for c in activity[0]['new_comments']], ['reviewer', 'helper-bot'])
        self.assertEqual(activity[0]['since'], APPROVED)
        self.assertIn('/repos/example/project/issues/42/comments?since=2026-09-23T11%3A46%3A31Z&per_page=100&page=1',
                      self.tracker.paths)
        self.assertIn('Current GitHub source activity', prompt)
        self.assertIn('This must handle 10x the current volume.', prompt)
        self.assertIn('Read this activity before you act.', prompt)
        self.assertEqual(self.service._get('tasks', self.task['id'])['_source_activity_seen'],
                         {URL: '2026-09-23T12:15:00Z'})

        self.requeue()
        reads = len(self.tracker.paths)
        task, prompt = self.turn()
        self.assertNotIn('_source_activity', task)
        self.assertNotIn('10x the current volume', prompt)
        self.assertEqual(self.tracker.paths[reads:], ['/repos/example/project/issues/42'])

        self.tracker.add(comment(9, 'reviewer', '2026-09-23T13:00:00Z', 'Also keep the old export.'))
        self.requeue()
        task, prompt = self.turn()
        self.assertEqual([c['body'] for c in task['_source_activity'][0]['new_comments']], ['Also keep the old export.'])
        self.assertNotIn('10x the current volume', prompt)

    def test_read_failure_adds_a_note_and_does_not_block_the_turn(self):
        self.approve_card()
        self.tracker.offline = True
        task, prompt = self.turn()
        self.service.provider.resume_task.assert_called_once()
        self.assertEqual(task['_source_activity'], [dict(url=URL, read_failed=True)])
        self.assertIn('Coconut could not read these GitHub sources: ' + URL, prompt)
        self.assertIn('Read each thread and its latest comments yourself before you act.', prompt)
        self.assertNotIn('_source_activity_seen', self.service._get('tasks', self.task['id']))

    def test_source_reads_do_not_hold_the_service_lock(self):
        self.approve_card()
        self.tracker.add(comment(7, 'reviewer', '2026-09-23T12:10:00Z', 'Scale requirement.'))
        self.turn()
        self.assertTrue(self.tracker.lock_free)
        self.assertTrue(all(self.tracker.lock_free), 'A GitHub read blocked phone requests')

    def test_source_audit_uses_the_assignment_revision(self):
        self.tracker = ThreadTracker(self.service)
        self.tracker.add(comment(3, 'owner', '2026-09-20T09:00:00Z', 'Original request.'),
                         comment(4, 'reviewer', '2026-09-23T12:10:00Z', 'Please also cover refunds.'))
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task.update(status='queued', _source_assignment=dict(source_id='github', id=URL, updated_at='2026-09-20T09:00:00Z'))
            self.service._put('tasks', task)
        with patch.object(self.service, '_pr_tracker_for', return_value=self.tracker):
            task, prompt = self.turn()
        self.assertEqual([c['body'] for c in task['_source_activity'][0]['new_comments']], ['Please also cover refunds.'])
        self.assertIn('Please also cover refunds.', prompt)


class IssueActivityReadTests(unittest.TestCase):
    def test_activity_keeps_the_last_ten_comments_and_truncates_long_text(self):
        tracker = ThreadTracker(Mock(lock=threading.Lock()))
        tracker.add(*[comment(i, f'user{i}', f'2026-09-23T12:{i:02d}:00Z', 'x' * (2000 if i == 11 else 10)) for i in range(12)])
        result = tracker.observe_issue_activity(URL, APPROVED)
        self.assertEqual([c['author'] for c in result['new_comments']], [f'user{i}' for i in range(2, 12)])
        self.assertEqual(result['omitted_comments'], 2)
        self.assertEqual(len(result['new_comments'][-1]['body']), 1500)
        self.assertTrue(result['new_comments'][-1]['truncated'])
        self.assertEqual(result['new_comments'][0]['url'], URL + '#issuecomment-2')
        # An unchanged thread needs no comment read.
        tracker.paths.clear()
        self.assertEqual(tracker.observe_issue_activity(URL, tracker.updated_at)['new_comments'], [])
        self.assertEqual(tracker.paths, ['/repos/example/project/issues/42'])


if __name__ == '__main__':
    unittest.main()
