"""A turn of a bot's conversation that owns a native outcome starts from the outcome's claim, not as a chat turn."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service


class Kanban:
    """The native board as the scheduler reads it: the cards by Coconut task ID."""
    def __init__(self):
        self.tasks = {}
        self.observed_at = None

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
        from coconut.work_capacity import chat_turn
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

    def test_with_the_gateway_a_claimed_outcome_turn_runs_in_the_bot_chat(self):
        from coconut.gateway_bridge import conversation_outcome_turn, gateway_work_turn, plain_chat_turn
        from test_gateway_chat import Adapter
        review = self.turn(self.dev, 'Check the saved review result', _native_outcome_id='t_review')
        self.assertFalse(plain_chat_turn(review))
        self.assertFalse(gateway_work_turn(review))
        self.assertTrue(conversation_outcome_turn(review))
        self.service.gateway_enabled = lambda: True
        with patch.object(self.provider, 'prepare_kanban_worker', create=True,
                          side_effect=AssertionError('Coconut starts no worker.')):
            pid = self.service.kanban_worker_pid(review['id'], review, self.dev, self.tmp.name, self.dev['id'],
                                                 't_review')
        # The claim holds the placeholder process of a gateway turn.
        self.assertEqual(pid, self.service.gateway_holders[review['id']].pid)
        claim = dict(native_id='t_review', external_id=review['id'], run_id=7, claim_lock='lock',
                     workspace=self.tmp.name, input=review['id'], pid=pid, context='Review the saved result.')
        self.service._put('tasks', dict(self.service._get('tasks', review['id']), _kanban_claim=claim))
        adapter = Adapter(self.service.gateway_bridge, self.service.profiles.home(self.dev), [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'The saved review is clean.'},
            {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        with patch.object(self.provider, 'start_task', side_effect=AssertionError('The worker must not run it.')):
            self.service._run_task(review['id'])
        adapter.join(5)
        # The turn runs in the bot's chat, as the worker ran it in the bot's conversation, with the rules of an outcome
        # and in the workspace of its claim.
        turn = adapter.turns[0]
        self.assertEqual((turn['chat_id'], turn['mode'], turn['folder']), (self.dev['id'], 'outcome', self.tmp.name))
        self.assertIn('kanban_complete', turn['text'])
        self.assertEqual(self.service._get('tasks', review['id'])['status'], 'completed')
        # It acts for the run of its card with the board tools of its claim, but it has no worktree.
        from coconut.service import ServiceError
        self.service._put('tasks', dict(self.service._get('tasks', review['id']), status='working'))
        calls = []
        self.service.kanban.call = lambda request: calls.append(request) or {'result': '{"ok": true}'}
        self.assertEqual(self.service.gateway_kanban_tool(review['id'], 'kanban_heartbeat', {'note': 'Checking.'}),
                         {'ok': True})
        self.assertEqual(calls[0]['claim'], {'native_id': 't_review', 'run_id': 7, 'claim_lock': 'lock'})
        with self.assertRaisesRegex(ServiceError, 'only in a turn of a work session'):
            self.service.gateway_workspace(review['id'])
        self.service.stop_gateway_holder(review['id'])

    def test_an_outcome_turn_without_its_claim_waits_queued_and_does_not_fail(self):
        # The live loop: the chat dispatcher started the turn without the claim, the turn failed on the missing claim,
        # and its retry failed the same way. Now a start without the claim leaves the task queued for its claim.
        review = self.turn(self.dev, 'Check the saved review result', _native_outcome_id='t_review')
        with patch.object(self.provider, 'start_task', side_effect=AssertionError('No turn starts without its claim.')), \
             self.assertLogs('coconut.service', 'WARNING') as logs:
            self.service._run_task(review['id'])
        self.assertIn('has no Kanban claim', logs.output[0])
        stored = self.service._get('tasks', review['id'])
        self.assertEqual(stored['status'], 'queued')
        self.assertNotIn('Work stopped', stored['summary'])
        self.assertEqual([m for m in self.service._task_messages(review['id']) if m['role'] == 'assistant'], [])

    def test_the_dispatchers_never_start_an_outcome_turn_without_its_claim(self):
        # Through the real dispatch pass: the chat dispatcher leaves the turn, and no turn of it runs without a claim.
        review = self.turn(self.dev, 'Check the saved review result', _native_outcome_id='t_review')
        starts = []
        self.provider.start_task = lambda task, *args: starts.append((task['id'], task.get('_kanban_claim')))
        with self.service.lock:
            self.service._dispatch_chat(4)
        for worker in list(self.service.running.values()):
            worker.join(5)
        self.assertEqual([start for start in starts if start[0] == review['id']], [])
        self.assertEqual(self.service._get('tasks', review['id'])['status'], 'queued')

    def test_an_outcome_that_stays_respawn_guarded_for_an_hour_is_shown(self):
        from coconut.kanban_scheduler import RESPAWN_GUARD_NOTICE_SECONDS
        review = self.turn(self.dev, 'Check the saved review result', _native_outcome_id='t_review', summary='Queued')
        native = dict(id='t_review', status='ready', dispatch_blocker='recent_success', title='Review')
        self.service.kanban.tasks[review['id']] = native
        hour = RESPAWN_GUARD_NOTICE_SECONDS
        start = 1_000_000.0
        def task():
            return self.service._get('tasks', review['id'])
        def work_activity():
            item = dict(id='work-1', kind='work', key='review', task_id=review['id'], bot_id=self.dev['id'],
                        title='Review', _native_outcome_id='t_review')
            return self.service._native_outcome_projection(item, {review['id']: task()}, [], [], start, {})['activity']
        self.service._note_respawn_guards(start)
        self.assertEqual(task()['_respawn_guard'], dict(reason='recent_success', since=start))
        # Before an hour nothing shows.
        self.service._note_respawn_guards(start + hour - 1)
        self.assertEqual((task()['summary'], work_activity()), ('Queued', 'Queued'))
        with self.assertLogs('coconut.kanban_scheduler', 'WARNING') as logs:
            self.service._note_respawn_guards(start + hour + 1)
        self.assertIn('t_review', logs.output[0])
        # The task and its Work item show the guard.
        self.assertIn('its respawn guard is recent_success', task()['summary'])
        self.assertIn('its respawn guard is recent_success', work_activity())
        # The warning comes once.
        with self.assertNoLogs('coconut.kanban_scheduler', 'WARNING'):
            self.service._note_respawn_guards(start + 2 * hour)
        # Another guard starts its own hour, and the old notice goes.
        native['dispatch_blocker'] = 'active_pr'
        self.service._note_respawn_guards(start + 3 * hour)
        self.assertEqual((task()['summary'], task()['_respawn_guard']), ('Queued', dict(reason='active_pr', since=start + 3 * hour)))
        self.assertEqual(work_activity(), 'Queued')
        # The record goes when the guard ends. Newer progress keeps its text.
        self.service._note_respawn_guards(start + 5 * hour)
        self.assertIn('its respawn guard is active_pr', task()['summary'])
        self.service._put('tasks', dict(task(), summary='Starting the review.'))
        native['dispatch_blocker'] = None
        self.service._note_respawn_guards(start + 6 * hour)
        self.assertNotIn('_respawn_guard', task())
        self.assertEqual(task()['summary'], 'Starting the review.')

    def test_the_saved_respawn_guards_are_found_by_an_index(self):
        from coconut.service import marked
        plan = ' '.join(str(row) for row in self.service.db.execute(
            'EXPLAIN QUERY PLAN SELECT rowid, data FROM tasks WHERE ' + marked('_respawn_guard')))
        self.assertIn('tasks_respawn_guard', plan)

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
