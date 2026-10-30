"""A turn of a bot's conversation that owns a native outcome starts from the outcome's claim, not as a chat turn."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service
from coconut.work_capacity import chat_turn


class Kanban:
    """The native board as the scheduler reads it: the cards by Coconut task ID."""
    def __init__(self):
        self.tasks = {}

    def close(self):
        pass


class ConversationOutcomeDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = FixtureProvider(self.tmp.name)
        self.provider.supports_native_outcomes = True
        self.service = Service(Path(self.tmp.name), self.provider, start_workers=False)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)
        self.service.kanban = Kanban()
        self.desk = self.write('/v1/bots', {'name': 'Desk', 'role': ''})
        self.dev = self.write('/v1/bots', {'name': 'Dev', 'role': ''})

    def write(self, path, data):
        return self.service.mutate('POST', path, data, str(uuid4()))

    def turn(self, bot, text, **fields):
        task = self.service._get('tasks', self.write('/v1/bots/' + bot['id'] + '/messages', {'text': text})['id'])
        self.assertTrue(task['_chat_session'])
        task.update(fields)
        self.service._put('tasks', task)
        return task

    def test_only_a_conversation_turn_without_an_outcome_is_a_chat_turn(self):
        self.assertTrue(chat_turn({'_chat_session': True}))
        self.assertFalse(chat_turn({'_chat_session': True, '_native_outcome_id': 't_review'}))
        self.assertFalse(chat_turn({'_native_outcome_id': 't_review'}))
        self.assertFalse(chat_turn({}))

    def test_scheduler_gives_the_outcome_turn_a_row_and_the_chat_dispatcher_leaves_it(self):
        review = self.turn(self.dev, 'Check the saved review result', _native_outcome_id='t_review')
        chat = self.turn(self.desk, 'A direct question')
        self.service.kanban.tasks[review['id']] = dict(id='t_review', status='ready')
        rows = {row['id']: row for row in self.service._kanban_rows()}
        self.assertNotIn(chat['id'], rows)
        self.assertEqual((rows[review['id']]['state'], rows[review['id']]['native_id']), ('ready', 't_review'))
        started = []
        with patch.object(self.service, '_run_task', side_effect=started.append), self.service.lock:
            self.service._dispatch_chat(4)
            for worker in self.service.running.values():
                worker.join(5)
        self.assertEqual(started, [chat['id']])
        self.assertEqual(self.service._get('tasks', review['id'])['status'], 'queued')

    def test_a_running_outcome_turn_does_not_hold_foreground_capacity(self):
        review = self.turn(self.dev, 'Check the saved review result', _native_outcome_id='t_review', status='working')
        chat = self.turn(self.desk, 'A direct question')
        hold = threading.Event()
        self.addCleanup(hold.set)
        worker = threading.Thread(target=hold.wait, daemon=True)
        worker.start()
        self.service.running[review['id']] = worker
        self.service._kanban_dispatching = True
        started = []
        with patch.object(self.service, '_run_task', side_effect=started.append), self.service.lock:
            self.service._dispatch_chat(4)
        # During a native pass only one foreground slot remains. A claimed outcome turn does not use it.
        self.assertEqual(started, [chat['id']])

    def test_with_the_gateway_a_claimed_outcome_turn_runs_in_the_worker(self):
        from coconut.gateway_bridge import gateway_work_turn, plain_chat_turn
        review = self.turn(self.dev, 'Check the saved review result', _native_outcome_id='t_review')
        self.assertFalse(plain_chat_turn(review))
        self.assertFalse(gateway_work_turn(review))
        prepared = []
        with patch.object(self.service, 'gateway_enabled', return_value=True), \
             patch.object(self.provider, 'prepare_kanban_worker', create=True,
                          side_effect=lambda *args: prepared.append(args) or 4242):
            pid = self.service.kanban_worker_pid(review['id'], review, self.dev, 'workspace', self.dev['id'], 't_review')
        # The claim prepares a worker, not the placeholder process of a gateway turn.
        self.assertEqual((pid, prepared[0][0], self.service.gateway_holders), (4242, review['id'], {}))
        claim = dict(native_id='t_review', external_id=review['id'], run_id=7, claim_lock='lock', workspace='workspace',
                     input=review['id'], pid=pid)
        self.service._put('tasks', dict(self.service._get('tasks', review['id']), _kanban_claim=claim))
        outcomes = []
        with patch.object(self.service, 'gateway_enabled', return_value=True), \
             patch.object(self.service, '_run_in_gateway', side_effect=AssertionError('The gateway must not run it.')), \
             patch.object(self.provider, 'start_task', side_effect=lambda task, *args: outcomes.append(task['_native_outcome'])):
            self.service._run_task(review['id'])
        self.assertEqual(outcomes, [claim])
        self.assertEqual(self.service._get('tasks', review['id'])['status'], 'completed')

    def test_no_chat_turn_starts_on_an_execution_that_a_native_pass_is_claiming(self):
        dev_chat = self.turn(self.dev, 'A direct question for Dev')
        desk_chat = self.turn(self.desk, 'A direct question for Desk')
        # The pass captured a ready row of Dev's own execution (bot key) and waits for the native claim.
        self.service._kanban_claiming = 1
        self.service._kanban_claim_outcomes = {self.dev['id']}
        started = []
        with patch.object(self.service, '_run_task', side_effect=started.append), self.service.lock:
            self.service._dispatch_chat(4)
        self.assertEqual(started, [desk_chat['id']])
        # After the pass, Dev's chat turn starts.
        self.service._kanban_claiming = 0
        self.service._kanban_claim_outcomes = set()
        self.service.running.clear()
        self.service._put('tasks', dict(self.service._get('tasks', desk_chat['id']), status='completed'))
        with patch.object(self.service, '_run_task', side_effect=started.append), self.service.lock:
            self.service._dispatch_chat(4)
        self.assertEqual(started, [desk_chat['id'], dev_chat['id']])


if __name__ == '__main__':
    unittest.main()
