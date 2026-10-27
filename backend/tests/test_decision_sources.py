import unittest
from unittest.mock import Mock, patch
import threading

from coconut.service import ServiceError, YieldForDecision
import test_decision_inbox as inbox_tests


class DecisionSourceTests(unittest.TestCase):
    setUp = inbox_tests.DecisionInboxTests.setUp
    tearDown = inbox_tests.DecisionInboxTests.tearDown
    write = inbox_tests.DecisionInboxTests.write
    event = inbox_tests.DecisionInboxTests.event
    restart = inbox_tests.DecisionInboxTests.restart
    reply = inbox_tests.DecisionInboxTests.reply

    def create(self, sources, optional=False):
        payload = dict(title='Include both alerts?', context='These reports miss the finance checks.',
                       options=[dict(id='include', label='Include both')], sources=sources, key='finance-routing')
        if optional:
            self.event('native_proposal', payload)
        else:
            with self.assertRaises(YieldForDecision):
                self.event('native_question', payload)
        return self.service.snapshot()['decisions'][-1]

    def test_sources_survive_creation_revision_and_restart(self):
        sources = [dict(kind='email', title='Weekly payout failures', url='https://example.com/mail/weekly'),
                   dict(kind='email', title='Negative-balance reports', url=None)]
        decision = self.create(sources)
        context = self.reply(decision, 'Explain the choice.')
        self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote=context['question'],
            revision=dict(title='Include both reports?', options=decision['options'])))
        self.restart()
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['sources'], sources)
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['revision'], 2)
        self.assertEqual(len(self.service.snapshot()['decisions']), 1)

    def test_source_revision_replaces_the_list_without_answering(self):
        decision = self.create([dict(kind='issue', title='project #42', url='https://example.com/issues/42')])
        for sources in ([dict(kind='pull_request', title='project #43', url='https://example.com/pull/43')], []):
            context = self.reply(decision, 'Update the sources.')
            result = self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote=context['question'],
                revision=dict(title=decision['title'], options=decision['options'], sources=sources)))
            decision = result['decision']
            self.assertEqual(decision['sources'], sources)
            self.assertEqual(decision['status'], 'pending')
            self.assertIsNone(decision.get('selected_option_id'))

    def test_optional_card_keeps_each_source_once(self):
        source = dict(kind='email', title='Weekly payout failures', url=None)
        decision = self.create([source, source], optional=True)
        self.assertEqual(decision['sources'], [source])

    def test_draft_revision_preserves_replaces_and_removes_sources(self):
        original = [dict(kind='email', title='Friday review', url='https://example.com/mail/friday')]
        draft = dict(channel='Gmail', recipient='sarah@example.com', subject='Friday review', body='Can we meet at 2 PM?')
        self.event('native_proposal', dict(key='friday', title='Send this reply?', draft=draft, sources=original))
        decision = self.service.snapshot()['decisions'][-1]
        replacement = [dict(kind='email', title='Monday review', url='https://example.com/mail/monday')]
        for extra, expected in (({}, original), ({'sources': replacement}, replacement), ({'sources': []}, [])):
            context = self.reply(decision, 'Revise the draft.')
            result = self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote=context['question'],
                revision=dict(draft=draft, **extra)))
            decision = result['decision']
            self.assertEqual(decision['sources'], expected)
            self.assertEqual(decision['status'], 'pending')
        self.restart()
        self.assertEqual(self.service._get('decisions', decision['id'])['sources'], [])

    def test_invalid_sources_create_no_card(self):
        malformed = [None, {}, [dict(kind='email', title='')], [dict(kind='unknown', title='Report')]]
        for url in ('file:///etc/passwd', 'javascript:alert(1)', 'https://', 'https://name:secret@example.com',
                    'https://example.com:bad', 'https://example.com/a b', 42):
            malformed.append([dict(kind='email', title='Report', url=url)])
        for sources in malformed:
            with self.subTest(sources=sources), self.assertRaises(ServiceError):
                self.create(sources)
        self.assertEqual(self.service.snapshot()['decisions'], [])


class DecisionSourcePreflightTests(unittest.TestCase):
    setUp = DecisionSourceTests.setUp
    tearDown = DecisionSourceTests.tearDown
    event = DecisionSourceTests.event
    write = DecisionSourceTests.write

    url = 'https://github.com/example/project/issues/42'
    old_revision = '2026-09-23T11:40:00Z'
    revision = '2026-09-23T11:46:31Z'

    def payload(self, **changes):
        return dict(title='Repair the record?', context='The record needs a correction.',
            options=[dict(id='fix', label='Repair'), dict(id='hold', label='Hold')],
            sources=[dict(kind='issue', title='Record correction', url=self.url)],
            source_revisions={self.url: self.revision}, **changes)

    def tracker(self, value=True):
        current = dict(state='closed', state_reason='completed', title='Record correction',
                       comments=1, updated_at=self.revision) if value else None
        return Mock(observe_issue=Mock(return_value=current))

    def test_stale_or_missing_source_creates_no_card_message_or_notification(self):
        for kind in ('native_question', 'native_proposal'):
            for revisions in ({}, {self.url: self.old_revision}):
                with self.subTest(kind=kind, revisions=revisions):
                    payload = self.payload(key='repair')
                    payload['source_revisions'] = revisions
                    call = self.event('native_call', dict(name='coconut_ask', arguments=payload))
                    payload['receipt_id'] = call['id']
                    before = self.service.snapshot()
                    with patch.object(self.service, '_pr_tracker_for', return_value=self.tracker()):
                        with self.assertRaisesRegex(ServiceError, 'No card was created'):
                            self.event(kind, payload)
                    self.assertEqual(self.service.snapshot()['decisions'], before['decisions'])
                    self.assertEqual(self.service.snapshot()['messages'], before['messages'])
                    self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'working')
                    self.assertEqual(self.service._get('native_receipts', call['id'])['state'], 'executing')
                    self.assertFalse(self.service.db.execute("SELECT 1 FROM events WHERE type='decision.created'").fetchone())
                    self.assertFalse(self.service.db.execute('SELECT 1 FROM push_items').fetchone())
                    self.event('native_result', dict(id=call['id'], result='Source changed; continue within existing permission.'))

    def test_source_outage_does_not_publish_an_unchecked_card(self):
        with patch.object(self.service, '_pr_tracker_for', return_value=self.tracker(False)):
            with self.assertRaisesRegex(ServiceError, 'could not verify'):
                self.event('native_question', self.payload())
        self.assertEqual(self.service.snapshot()['decisions'], [])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'working')

    def test_current_closed_source_can_still_need_a_real_followup_choice(self):
        with patch.object(self.service, '_pr_tracker_for', return_value=self.tracker()):
            with self.assertRaises(YieldForDecision):
                self.event('native_question', self.payload())
        decision = self.service.snapshot()['decisions'][0]
        self.assertEqual(decision['status'], 'pending')
        self.assertIsNone(decision['decided_by'])
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['_source_facts'][self.url]['updated_at'], self.revision)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')

    def test_source_read_does_not_hold_service_lock(self):
        tracker = self.tracker()
        current = tracker.observe_issue.return_value
        def read(url):
            acquired = threading.Event()
            def check():
                with self.service.lock:
                    acquired.set()
            reader = threading.Thread(target=check)
            reader.start()
            self.assertTrue(acquired.wait(1), 'Source reads blocked phone requests')
            reader.join(1)
            return current
        tracker.observe_issue.side_effect = read
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            self.event('native_proposal', self.payload(key='repair'))
            # An exact retry returns the existing card even if source access fails later.
            tracker.observe_issue.side_effect = AssertionError('Duplicate card must not recheck')
            result = self.event('native_proposal', self.payload(key='repair'))
        self.assertEqual(result['decision_id'], self.service.snapshot()['decisions'][0]['id'])
        self.assertEqual(len(self.service.snapshot()['decisions']), 1)

    def test_pr_source_uses_issue_api_revision_and_strips_comment_fragment(self):
        tracker = self.tracker()
        payload = self.payload()
        url = self.url.replace('/issues/', '/pull/')
        payload.update(sources=[dict(kind='pull_request', title='PR review', url=url+'#issuecomment-12')],
                       source_revisions={url:self.revision}, key='review')
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            self.event('native_proposal', payload)
        tracker.observe_issue.assert_called_once_with(self.url)

    def test_repository_named_pulls_keeps_its_identity(self):
        tracker = self.tracker()
        for route in ('issues', 'pull'):
            url = f'https://github.com/example/pulls/{route}/42'
            payload = self.payload(key=route)
            payload.update(sources=[dict(kind='issue', title='Exact repository', url=url)],
                           source_revisions={url: self.revision})
            with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
                self.event('native_proposal', payload)
            self.assertEqual(tracker.observe_issue.call_args.args, ('https://github.com/example/pulls/issues/42',))
