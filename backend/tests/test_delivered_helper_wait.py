"""A native helper that finishes through a continuation returns its result to its requester exactly once."""
import json
import tempfile
import unittest
from uuid import uuid4

from coconut.service import Service
from test_service import ControlledProvider


class DeliveredHelperWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ControlledProvider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.desk, self.care, self.dev, self.ops = (
            self.service.mutate('POST', '/v1/bots', {'name': name}, str(uuid4()), 'test-phone')
            for name in ('Desk', 'Care', 'Dev', 'Ops'))
        self.replies = []
        self.provider.native_settings = lambda operation, bots, **payload: self.replies.append((operation, payload)) or {'ok': True}

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def task(self, identity):
        return self.service._get('tasks', identity)

    def working(self, identity):
        with self.service.transaction():
            task = self.task(identity)
            task['status'] = 'working'
            self.service._put('tasks', task)

    def finish(self, identity, text):
        self.working(identity)
        self.service.emit(identity, 'text_delta', {'text': text})
        self.service.emit(identity, 'completed', {'summary': 'Complete.'})

    def native(self, source, target, text):
        self.working(source)
        return self.service._deliver_bot_message(source, dict(target_bot_id=target['id'], text=text,
                                                               native_envelope_id=uuid4().hex))['task_id']

    def returns_of(self, helper):
        return [t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == helper]

    def delegate(self, questions=1):
        """The PR8673 chain: Dev's native message continues Desk's completed root, which asks Care, which asks Dev."""
        self.root = self.service.mutate('POST', f'/v1/bots/{self.desk["id"]}/messages', {'text': 'Track the PR'},
                                        str(uuid4()), 'test-phone')['id']
        self.working(self.root)
        product = self.service._deliver_bot_message(self.root, dict(target_bot_id=self.dev['id'], text='Build the fix'))['task_id']
        self.other = self.service._deliver_bot_message(self.root, dict(target_bot_id=self.ops['id'], text='Watch CI'))['task_id']
        self.finish(self.root, 'Delegated.')
        with self.service.transaction():
            # Hermes completed the root's native card while its dependencies remained, as it did for t_2cbb48b3.
            root = self.task(self.root)
            root['status'] = 'completed'
            self.service._put('tasks', root)
        self.requester = self.native(product, self.desk, 'Ask Care for the fixture.')
        self.finish(product, 'Asked Desk.')
        self.helper = self.native(self.requester, self.care, 'Check the fixture.')
        self.finish(self.requester, 'Asked Care.')
        asked = [self.native(self.helper, self.dev, f'Which fixture {n}?') for n in range(questions)]
        self.finish(self.helper, 'Asked Dev.')
        self.continuation = self.native(asked[0], self.care, 'Use fixture B.')
        self.finish(asked[0], 'Answered Care.')
        root = self.task(self.root)
        self.assertEqual((root['status'], root['_waiting_on']), ('completed', [self.other, self.helper]))
        self.assertEqual(self.task(self.continuation)['_relay_callback_owner'], self.helper)
        return asked

    def test_continuation_finish_returns_the_helper_result_to_its_completed_requester_once(self):
        self.delegate()
        self.finish(self.continuation, 'Fixture B passes.')
        helper = self.task(self.helper)
        self.assertEqual(helper['status'], 'completed')
        self.assertEqual(helper['_return_pending']['text'], 'Fixture B passes.')
        self.assertEqual(helper['_native_reply_pending']['text'], 'Fixture B passes.')
        self.service._drain_returns()
        self.service._drain_returns()
        relayed = [p for operation, p in self.replies if operation == 'relay_reply' and p['bot_id'] == self.desk['id']]
        self.assertEqual([(p['envelope_id'], p['text']) for p in relayed],
                         [(helper['_native_relay']['envelope_id'], 'Fixture B passes.')])
        returned = self.returns_of(self.helper)
        self.assertEqual(len(returned), 1)
        returned = returned[0]
        self.assertEqual((returned['bot_id'], returned['_assignment_task_id'], returned['_relay_callback_owner'],
                          returned['thread_id'], returned['_bot_messages'][0]['text']),
                         (self.desk['id'], self.root, self.requester, self.task(self.root)['thread_id'], 'Fixture B passes.'))
        tasks = len(self.service._all('tasks'))
        self.finish(returned['id'], 'Recorded the fixture.')
        self.service._drain_returns()
        self.assertEqual(self.task(self.root)['_waiting_on'], [self.other])
        self.assertEqual(self.task(self.helper)['status'], 'completed')
        self.assertEqual(len(self.service._all('tasks')), tasks)
        self.assertEqual(len(self.returns_of(self.helper)), 1)

    def test_a_process_resumed_continuation_returns_the_helper_result_once(self):
        self.delegate()
        self.working(self.continuation)
        record = self.service.emit(self.continuation, 'native_call', {'name': 'terminal', 'arguments': {'background': True, 'notify': True}})
        self.service.emit(self.continuation, 'native_result', {'id': record['id'], 'result': json.dumps(
            {'session_id': 'proc_fixture', 'notify_on_complete': True, 'status': 'running'})})
        self.service.emit(self.continuation, 'completed', {'summary': 'Running the fixture.'})
        self.assertEqual(self.task(self.helper)['status'], 'waiting')
        with self.service.transaction():
            # The finished process resumes the continuation with its saved output.
            self.service._resume_process_task(self.continuation, [(self.service._get('native_receipts', record['id']),
                                                                    'The fixture run finished.')])
        self.assertTrue(self.task(self.continuation)['_background_completion'])
        self.finish(self.continuation, 'Fixture B passes.')
        self.assertEqual(self.task(self.helper)['_return_pending']['text'], 'Fixture B passes.')
        self.service._drain_returns()
        self.service._drain_returns()
        self.assertEqual(len(self.returns_of(self.helper)), 1)
        self.assertEqual(len([p for operation, p in self.replies if p['bot_id'] == self.desk['id']]), 1)

    def test_a_partly_answered_helper_keeps_waiting_and_returns_nothing(self):
        asked = self.delegate(questions=2)
        self.finish(self.continuation, 'Fixture B passes.')
        helper = self.task(self.helper)
        self.assertEqual((helper['status'], helper['_waiting_on']), ('waiting', [asked[1]]))
        self.assertNotIn('_return_pending', helper)
        self.service._drain_returns()
        self.assertEqual(self.returns_of(self.helper), [])
        self.assertEqual([p for operation, p in self.replies if p['bot_id'] == self.desk['id']], [])
        self.assertEqual(self.task(self.root)['_waiting_on'], [self.other, self.helper])


if __name__ == '__main__':
    unittest.main()
