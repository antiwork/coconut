import tempfile
import threading
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError, YieldForDecision


class AnswerProvider(FixtureProvider):
    def __init__(self, root):
        super().__init__(root)
        self.received = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def resume_task(self, task, bot, history, decision, emit):
        self.received.append((task['id'], decision['id'], decision['selected_option_id']))
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError('The test did not release the bot.')
        emit('completed', {'summary': 'Handled the selected answer.'})

    def start_task(self, *args):
        raise AssertionError('An answered card must resume with its saved decision.')


class DecisionBurstTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = AnswerProvider(self.tmp.name)
        self.service = Service(self.tmp.name, self.provider, start_workers=False)
        self.bot = self.write('/v1/bots', {'name': 'Desk', 'role': ''})

    def tearDown(self):
        self.provider.release.set()
        self.service.close()
        self.tmp.cleanup()

    def write(self, path, data, key=None):
        return self.service.mutate('POST', path, data, key or str(uuid4()))

    def task(self):
        task = self.write(f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this work', 'mode': 'queue'})
        with self.service.transaction():
            task = self.service._get('tasks', task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        return task

    def cards(self):
        cards = []
        for index in range(5):
            task = self.task()
            with self.assertRaises(YieldForDecision):
                self.service.emit(task['id'], 'native_question', {
                    'title': f'Check item {index}?',
                    'options': [{'id': f'option-{index}', 'label': f'Check item {index}'}]})
            cards.append(self.service._all('decisions')[-1])
        source = self.task()
        for index in range(5, 10):
            self.service.emit(source['id'], 'native_proposal', {
                'key': f'proposal-{index}', 'title': f'Check item {index}?',
                'options': [{'id': f'option-{index}', 'label': f'Check item {index}'}]})
            cards.append(self.service._all('decisions')[-1])
        self.service.emit(source['id'], 'completed', {'summary': 'Proposals prepared.'})
        return cards

    def answer(self, card):
        path = f"/v1/decisions/{card['id']}/answer"
        payload = {'option_id': card['options'][0]['id'], 'revision': card['revision']}
        key = str(uuid4())
        receipt = self.write(path, payload, key)
        # Repeat a request whose response the phone could have lost.
        self.assertEqual(self.write(path, payload, key), receipt)
        return path, payload, key, receipt

    def assert_drained(self, cards):
        deadline = time.monotonic() + 8
        with self.service.changed:
            while time.monotonic() < deadline:
                if all(self.service._get('decisions', card['id'])['status'] == 'executed' for card in cards):
                    break
                self.service.changed.wait(.05)
        expected = []
        for card in cards:
            saved = self.service._get('decisions', card['id'])
            self.assertEqual(saved['status'], 'executed')
            self.assertEqual(len(saved['_answers']), 1)
            task_id = saved.get('_execution_task_id', saved['task_id'])
            self.assertEqual(self.service._get('tasks', task_id)['status'], 'completed')
            expected.append((task_id, card['id'], card['options'][0]['id']))
        self.assertEqual(Counter(self.provider.received), Counter(expected))
        self.assertEqual(len({task_id for task_id, _, _ in expected}), 10)

    def test_rapid_answers_wait_for_busy_bot_and_resume_once_each(self):
        cards = self.cards()
        self.answer(cards[0])
        self.service.start()
        self.assertTrue(self.provider.entered.wait(2))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.answer, cards[1:]))
        self.assertEqual(len(self.provider.received), 1)
        for card in cards[1:]:
            saved = self.service._get('decisions', card['id'])
            task = self.service._get('tasks', saved.get('_execution_task_id', saved['task_id']))
            self.assertEqual(task['status'], 'queued')
        self.provider.release.set()
        self.assert_drained(cards)

    def test_saved_burst_and_request_receipts_survive_restart(self):
        cards = self.cards()
        receipts = [self.answer(card) for card in reversed(cards)]
        self.service.close()
        self.service = Service(self.tmp.name, self.provider, start_workers=False)
        for path, payload, key, receipt in receipts:
            self.assertEqual(self.write(path, payload, key), receipt)
            with self.assertRaises(ServiceError) as error:
                self.write(path, payload)
            self.assertEqual(error.exception.status, 409)
        self.provider.release.set()
        self.service.start()
        self.assert_drained(cards)


if __name__ == '__main__':
    unittest.main()
