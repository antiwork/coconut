import unittest

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
