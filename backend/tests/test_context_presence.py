import threading
import unittest
from unittest.mock import patch

import test_decision_inbox as inbox_tests


class ContextPresenceTests(unittest.TestCase):
    setUp = inbox_tests.DecisionInboxTests.setUp
    tearDown = inbox_tests.DecisionInboxTests.tearDown
    write = inbox_tests.DecisionInboxTests.write
    event = inbox_tests.DecisionInboxTests.event
    question = inbox_tests.DecisionInboxTests.question

    def test_reply_publishes_activity_until_it_finishes_or_fails(self):
        decision = self.question()
        for failed in (False, True):
            with self.subTest(failed=failed):
                entered, release = threading.Event(), threading.Event()
                def answer_context(task, bot, history, question, emit):
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError('The test did not release the reply.')
                    if failed:
                        emit('failed', dict(summary='The check failed.'))
                    else:
                        emit('text_delta', dict(text='The original record is available.'))
                self.write('POST', f"/v1/decisions/{decision['id']}/reply", dict(text='Check the source.', revision=1))
                context = self.service._all('contexts')[-1]
                self.assertEqual(self.service.snapshot()['bots'][0]['status'], 'needs_you')
                with patch.object(self.service.provider, 'answer_context', answer_context):
                    self.service._start_context(context['id'])
                    try:
                        self.assertTrue(entered.wait(timeout=5))
                        state = self.service.snapshot()
                        self.assertEqual(state['bots'][0]['status'], 'working')
                        self.assertEqual(state['tasks'][0]['status'], 'needs_you')
                        self.assertEqual(state['decisions'][0]['status'], 'pending')
                        with self.service.transaction():
                            self.service._refresh_bot(self.bot['id'])
                        self.assertEqual(self.service.snapshot()['bots'][0]['status'], 'working')
                    finally:
                        release.set()
                        self.service.context_threads[context['id']].join(timeout=5)
                self.assertFalse(self.service.context_threads[context['id']].is_alive())
                self.assertEqual(self.service._get('contexts', context['id'])['status'], 'failed' if failed else 'completed')
                self.assertEqual(self.service.snapshot()['bots'][0]['status'], 'needs_you')
                events = self.service.db.execute("SELECT data FROM events WHERE type='bot.updated' ORDER BY rowid DESC LIMIT 2").fetchall()
                import json
                self.assertEqual([json.loads(row[0])['status'] for row in events], ['needs_you', 'working'])
