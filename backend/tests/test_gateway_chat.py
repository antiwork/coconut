"""Plain chat turns through the Hermes gateway: the routing rule, the event translation, the wait for the gateway, the
gateway process, and the profile edit. test_gateway_adapter.py checks the adapter, test_native_gateway_chat.py a real gateway."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut import gateway_bridge, gateway_chat, gateway_plugin
from coconut.gateway_bridge import GatewayBridge, TurnEvents, plain_chat_turn
from coconut.service import Service, ServiceError
from test_service import ControlledProvider


class Adapter(threading.Thread):
    """Poll as the coconut adapter does, take one turn, and report `script` for it."""

    def __init__(self, bridge, home, script):
        super().__init__(daemon=True)
        self.bridge, self.home, self.script = bridge, str(home), script
        self.turns, self.stops, self.answers = [], [], []
        # The first poll connects the adapter.
        bridge.poll({'home': self.home, 'wait': 0})

    def run(self):
        deadline = time.monotonic() + 10
        while not self.turns and time.monotonic() < deadline:
            self.turns += self.bridge.poll({'home': self.home, 'wait': 1})['turns']
        for event in self.script:
            self.answers.append(self.bridge.event(dict(event, turn_id=self.turns[0]['id'])))


class TurnRecordTests(unittest.TestCase):
    """The comments of the native task, and the delivered and sent messages, are records that a session keeps once."""

    def task(self, status='queued'):
        return {'id': 't1', 'title': 'Ship the export', '_native_outcome_id': 'n1',
                '_native_outcome': {'context': '# Kanban task n1\n## Body\nShip it.\n',
                                    'comments': [{'id': 7, 'author': 'default', 'at': 1793546240, 'body': 'Found the bug.'}]},
                '_bot_messages': [{'sender_bot_id': 'b2', 'sender_name': 'Lin', 'text': 'The logs are clean.'}],
                '_outgoing_bot_messages': [{'target_bot_id': 'b2', 'text': 'Check the logs.', 'task_id': 't2',
                                            'status': status}]}

    def test_the_records_of_a_turn_leave_its_context(self):
        from coconut.native_conversation import turn_records
        task = self.task()
        records = turn_records(task)
        self.assertEqual([record['id'].split(':')[0] for record in records], ['comment', 'message', 'sent'])
        self.assertEqual(records[0]['id'], 'comment:7')
        self.assertIn('Found the bug.', records[0]['text'])
        self.assertIn('The logs are clean.', records[1]['text'])
        self.assertIn('Check the logs.', records[2]['text'])
        # The context of the turn has the native task, but not the records.
        from coconut.gateway_chat import work_notes
        text = work_notes(dict(operation="start", task=task, history=[]))
        self.assertIn('Ship it.', text)
        for record in ('Found the bug.', 'The logs are clean.', 'Check the logs.'):
            self.assertNotIn(record, text)
        # A record keeps its ID in a later turn. A sent message whose task changed is a new record.
        later = dict(task, _bot_messages=task['_bot_messages'] + [{'sender_bot_id': 'b2', 'sender_name': 'Lin',
                                                                   'text': 'One more thing.'}])
        self.assertEqual([r['id'] for r in turn_records(later)][:2], [r['id'] for r in records][:2])
        self.assertNotEqual(turn_records(self.task('completed'))[2]['id'], records[2]['id'])

    def test_a_claim_gives_its_turn_the_comments_apart_from_the_worker_context(self):
        from types import SimpleNamespace
        from coconut import kanban_runtime
        comments = [SimpleNamespace(id=number, author='default', created_at=number, body='Comment %d' % number)
                    for number in range(kanban_runtime.CLAIM_COMMENTS + 2)]
        # As Hermes builds the context: the body, then the comments that list_comments of its module returns. The body
        # holds the heading of the comment thread, as pasted Markdown may.
        body = '# Kanban task n1\n## Body\nShip it.\n## Comment thread\nPasted.\n'
        kb = SimpleNamespace(list_comments=lambda conn, identity: comments)
        kb.build_worker_context = lambda conn, identity: body + ''.join(
            c.body + '\n' for c in kb.list_comments(conn, identity))
        original = kb.list_comments
        context, records = kanban_runtime.worker_context(SimpleNamespace(kb=kb, conn=None), 'n1')
        self.assertEqual(context, body)
        self.assertIs(kb.list_comments, original)
        self.assertEqual(len(records), kanban_runtime.CLAIM_COMMENTS)
        self.assertEqual(records[-1], {'id': 51, 'author': 'default', 'at': 51, 'body': 'Comment 51'})


class RoutingRuleTests(unittest.TestCase):
    def test_only_a_plain_chat_turn_goes_to_the_gateway(self):
        plain = {'_chat_session': True, 'attachments': [], '_pending_decisions': [], '_tracked_pr_observations': []}
        self.assertTrue(plain_chat_turn(plain))
        # A task outside the bot's conversation gets its own work session from the dispatchers, which only the worker has.
        self.assertFalse(plain_chat_turn(dict(plain, _chat_session=False)))
        for key in ('_native_job', '_work_context_id', '_execution_session_id', '_work_status_only'):
            with self.subTest(key=key):
                self.assertFalse(plain_chat_turn(dict(plain, **{key: [{'id': 'x'}]})))
        # Attachments, reactions, pending cards and the work of the chat's assignment go to the gateway as text.
        for key in ('attachments', '_reaction_feedback', '_pending_decisions', '_work_todos', '_work_resume',
                    '_work_dependencies', '_self_recovery', '_withdrawal_corrections', '_direct_reply_message_id',
                    '_bot_messages', '_is_return', '_handoff_assignment', '_outgoing_bot_messages',
                    '_source_activity', '_pr_review', '_bot_creation_receipts', '_bot_deletion_receipts'):
            with self.subTest(key=key):
                self.assertTrue(plain_chat_turn(dict(plain, **{key: [{'id': 'x'}]})))

    def test_silence_is_a_marker_as_the_whole_reply_or_its_first_or_last_line(self):
        from coconut.silent_responses import is_silence_reply
        for text in ('', '  ', '[SILENT]', 'no_reply', '[SILENT]\nNothing to add.', 'Noted.\nSILENT'):
            with self.subTest(text=text):
                self.assertTrue(is_silence_reply(text))
        for text in ('Thanks!', 'I stay [SILENT] here.', 'First.\n[SILENT]\nLast.'):
            with self.subTest(text=text):
                self.assertFalse(is_silence_reply(text))


class WorkTurnRuleTests(unittest.TestCase):
    def test_a_task_with_its_own_work_session_runs_in_the_gateway_without_other_worker_inputs(self):
        from coconut.gateway_bridge import gateway_work_turn
        audit = {'_execution_session_id': 'work-1', '_kanban_claim': {'run_id': 1}}
        self.assertTrue(gateway_work_turn(audit))
        self.assertFalse(gateway_work_turn(dict(audit, _execution_session_id=None)))
        self.assertFalse(gateway_work_turn(dict(audit, _chat_session=True)))
        # A question about a Work item without a native outcome runs in the gateway too.
        self.assertTrue(gateway_work_turn(dict(audit, _work_context_id='work', _work_context={'id': 'work'})))
        # The audit of a work source runs in the gateway too. Coconut runs its board tools (gateway_kanban_tool).
        self.assertTrue(gateway_work_turn(dict(audit, _source_audit_prompt='Reconcile.', _source_answer_steps={'url': 'x'})))
        # Receipts are no worker input: the turn text indexes them, also the receipts of a worker's tools.
        board = [{'action': {'name': 'kanban_comment', 'arguments': {}}}]
        self.assertTrue(gateway_work_turn(dict(audit, _source_audit_prompt='Reconcile.', _native_receipts=board)))
        self.assertTrue(gateway_work_turn(dict(audit, _native_receipts=board + [{'action': {'name': 'terminal'}}])))
        # A native outcome runs in the gateway too, and so does its technical lane.
        self.assertTrue(gateway_work_turn(dict(audit, _native_outcome_id='t_1', _technical_lane={'scope': ['tests']})))
        # A message about the Work item of a native outcome goes to the turn of that outcome, which has its native task.
        from coconut.gateway_chat import gateway_turn_text
        message = dict(id='m1', role='user', kind='text', text='Is the export done?', task_id='t',
                       work_context={'id': 'work'})
        request = dict(operation='start', history=[message], task=dict(audit, id='t', title='Export', attachments=[]))
        self.assertIsNone(gateway_turn_text(request))
        request['task'].update(_native_outcome_id='t_1', _native_outcome={'context': 'Card body.'})
        self.assertTrue(gateway_turn_text(request).startswith('Is the export done?'))
        for key in ('_native_job', '_work_status_only'):
            with self.subTest(key=key):
                self.assertFalse(gateway_work_turn(dict(audit, **{key: 'x'})))


class TurnEventTests(unittest.TestCase):
    def translate(self, *events, silent=False, commentary=None):
        emitted = []
        # A reaction is silent without commentary, as most of these cases test.
        turn = TurnEvents(lambda kind, data: emitted.append((kind, data)), silent=silent,
                          commentary=not silent if commentary is None else commentary)
        return emitted, [turn.apply(event) for event in events]

    def test_a_silent_turn_shows_no_commentary_and_a_silence_marker_ends_it_without_a_message(self):
        emitted, _ = self.translate(
            {'kind': 'send', 'message_id': 'c:1', 'text': 'Let me look.', 'interim': True},
            {'kind': 'delta', 'stream': 1, 'text': '[SILENT]'},
            {'kind': 'done', 'outcome': 'success'}, silent=True)
        self.assertEqual(emitted, [
            ('phase', {'phase': 'writing'}), ('text_delta', {'text': '[SILENT]'}),
            ('text_delta', {'text': '', 'replace': True}),
            ('completed', {'summary': 'Complete.', 'silent_response': True, 'reaction_silent': True})])
        # Commentary that the plugin streamed before its interim send goes too, also when a later call follows.
        emitted, _ = self.translate(
            {'kind': 'delta', 'stream': 1, 'text': 'Let me look.'},
            {'kind': 'send', 'message_id': 'c:1', 'text': 'Let me look.', 'interim': True},
            {'kind': 'delta', 'stream': 2, 'text': '[SILENT]'},
            {'kind': 'done', 'outcome': 'success'}, silent=True)
        self.assertNotIn(('interim_seal', {}), emitted)
        self.assertEqual([data for kind, data in emitted if kind == 'text_delta'], [
            {'text': 'Let me look.'}, {'text': '', 'replace': True}, {'text': '[SILENT]'}, {'text': '', 'replace': True}])
        self.assertEqual(emitted[-1][1].get('silent_response'), True)
        # Hermes holds back the start of a marker and sends no reply.
        emitted, _ = self.translate({'kind': 'delta', 'stream': 1, 'text': '[SILEN'}, {'kind': 'done', 'outcome': 'success'},
                                    silent=True)
        self.assertEqual(emitted[-2:], [('text_delta', {'text': '', 'replace': True}),
                                        ('completed', {'summary': 'Complete.', 'silent_response': True, 'reaction_silent': True})])
        # A useful reply of a silent turn is an ordinary reply.
        emitted, _ = self.translate(
            {'kind': 'send', 'message_id': 'm:1', 'text': 'Glad it helped. One more detail: the cache is warm.'},
            {'kind': 'done', 'outcome': 'success'}, silent=True)
        self.assertEqual(emitted[-1], ('completed', {'summary': 'Complete.'}))

    def test_bot_traffic_keeps_its_commentary_and_may_end_in_silence(self):
        emitted, _ = self.translate(
            {'kind': 'send', 'message_id': 'c:1', 'text': 'Reading the report of Lin.', 'interim': True},
            {'kind': 'delta', 'stream': 1, 'text': '[SILENT]'},
            {'kind': 'done', 'outcome': 'success'}, silent=True, commentary=True)
        self.assertIn(('interim_message', {'text': 'Reading the report of Lin.'}), emitted)
        self.assertEqual(emitted[-1], ('completed', {'summary': 'Complete.', 'silent_response': True,
                                                     'reaction_silent': True}))

    def test_segments_become_interim_messages_and_the_last_one_is_the_reply(self):
        emitted, done = self.translate(
            {'kind': 'typing'},
            {'kind': 'send', 'message_id': 't:1', 'text': 'I will check.'},
            {'kind': 'edit', 'message_id': 't:1', 'text': 'I will check the test.'},
            {'kind': 'send', 'message_id': 't:2', 'text': 'Working on it.', 'interim': True},
            {'kind': 'send', 'message_id': 't:3', 'text': 'The test'},
            {'kind': 'edit', 'message_id': 't:1', 'text': 'An edit of an old segment.'},
            {'kind': 'edit', 'message_id': 't:3', 'text': 'The test passes.'},
            {'kind': 'send', 'message_id': 't:4', 'text': 'The test passes.'},
            {'kind': 'done', 'outcome': 'success'})
        self.assertEqual(emitted, [
            ('phase', {'phase': 'thinking'}), ('phase', {'phase': 'writing'}),
            ('text_delta', {'text': 'I will check.'}),
            ('text_delta', {'text': 'I will check the test.', 'replace': True}),
            ('interim_seal', {}), ('interim_message', {'text': 'Working on it.'}),
            ('text_delta', {'text': 'The test'}),
            ('text_delta', {'text': 'The test passes.', 'replace': True}),
            ('completed', {'summary': 'Complete.'})])
        self.assertEqual(done, [False] * 8 + [True])

    def test_streamed_text_becomes_the_reply_and_commentary_an_interim_message(self):
        emitted, _ = self.translate(
            {'kind': 'delta', 'stream': 1, 'text': 'I will '},
            {'kind': 'delta', 'stream': 1, 'text': 'check.'},
            # The gateway sends the commentary of that model call after it ends.
            {'kind': 'send', 'message_id': 't:1', 'text': 'I will check.', 'interim': True},
            # A late piece of the ended call changes nothing.
            {'kind': 'delta', 'stream': 1, 'text': ' Late.'},
            {'kind': 'delta', 'stream': 2, 'text': 'It passes'},
            {'kind': 'send', 'message_id': 't:2', 'text': 'It passes.'},
            # After the reply send, the gateway's own sends and edits lead.
            {'kind': 'delta', 'stream': 2, 'text': ' again'},
            {'kind': 'done', 'outcome': 'success'})
        self.assertEqual(emitted, [
            ('phase', {'phase': 'writing'}),
            ('text_delta', {'text': 'I will '}), ('text_delta', {'text': 'check.'}),
            ('interim_seal', {}),
            ('text_delta', {'text': 'It passes'}),
            ('text_delta', {'text': 'It passes.', 'replace': True}),
            ('completed', {'summary': 'Complete.'})])

    def test_a_question_event_asks_for_a_card(self):
        emitted, done = self.translate({'kind': 'question', 'question_id': 'q', 'question_kind': 'ask', 'text': 'Which?',
                                        'detail': '', 'choices': []})
        self.assertEqual(emitted, [('gateway_question', {'question_id': 'q', 'question_kind': 'ask', 'text': 'Which?',
                                                         'detail': '', 'choices': [], 'turn_id': None})])
        self.assertEqual(done, [False])

    def test_a_deleted_preview_and_a_failed_turn(self):
        emitted, _ = self.translate({'kind': 'send', 'message_id': 't:1', 'text': 'Draft'},
                                    {'kind': 'delete', 'message_id': 't:1'}, {'kind': 'done', 'outcome': 'failure'})
        self.assertEqual(emitted[-2:], [('text_delta', {'text': '', 'replace': True}),
                                        ('failed', {'summary': 'The Hermes gateway could not finish this turn.'})])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = GatewayBridge(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_the_address_file_is_private_and_the_token_is_checked(self):
        self.bridge.publish('http://127.0.0.1:8960')
        self.assertEqual(os.stat(self.bridge.address_file).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.bridge.folder).st_mode & 0o777, 0o700)
        self.assertTrue(self.bridge.authorized('Bearer ' + self.bridge.token))
        self.assertFalse(self.bridge.authorized('Bearer other'))
        self.assertFalse(self.bridge.authorized(''))

    def test_a_turn_that_no_adapter_takes_goes_back_to_the_worker(self):
        with patch.object(gateway_bridge, 'TAKE_SECONDS', 0.2):
            with self.assertRaises(gateway_bridge.GatewayUnavailable):
                self.bridge.run_turn('task', self.temp.name, 'bot', 'Hello', lambda *_: None)
        # The withdrawn turn is gone: a later poll gets nothing.
        self.assertEqual(self.bridge.poll({'home': self.temp.name, 'wait': 0}), {'turns': [], 'stop': []})

    def test_a_turn_that_the_gateway_stops_reporting_fails(self):
        home = self.temp.name
        def take():
            self.bridge.poll({'home': home, 'wait': 5})
        threading.Thread(target=take, daemon=True).start()
        with patch.object(gateway_bridge, 'SILENT_SECONDS', 0.3):
            with self.assertRaisesRegex(RuntimeError, 'stopped answering'):
                self.bridge.run_turn('task', home, 'bot', 'Hello', lambda *_: None)
        # The gateway may still run the turn, so the next poll tells the adapter to stop it.
        self.assertEqual(self.bridge.poll({'home': home, 'wait': 0})['stop'], ['bot'])

    def test_events_of_unknown_turns_are_refused_and_bad_events_raise(self):
        self.assertEqual(self.bridge.event({'turn_id': 'gone', 'kind': 'send', 'message_id': 'm', 'text': 'x'}), {'ok': False})
        for event in ({'kind': 'other'}, {'kind': 'send', 'text': 'no message'}, {'kind': 'edit', 'message_id': 'm', 'text': 1},
                      {'kind': 'delta', 'text': 'no stream'}, {'kind': 'question', 'question_id': 'q', 'question_kind': 'poll',
                                                               'text': 'x'},
                      {'kind': 'question', 'question_id': 'q', 'question_kind': 'ask', 'text': 'x',
                       'choices': [{'id': 'a', 'label': 'A'}, {'id': 'a', 'label': 'B'}]}):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.bridge.event(event)
        with self.assertRaises(ValueError):
            self.bridge.poll({'wait': 0})


class Provider(ControlledProvider):
    supports_native_outcomes = True


class ServiceRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = Provider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        # As on the person's Hermes. The bridge of the test stands in for the gateway.
        self.service.gateway_enabled = lambda: True
        self.bot = self.write('POST', '/v1/bots', {'name': 'Ada'})
        self.home = self.service.profiles.home(self.bot)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None):
        return self.service.mutate(method, path, data or {}, str(uuid4()), 'phone')

    def send(self, text):
        return self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': text})

    def decisions(self):
        # The thread of a turn uses the same database connection, so a read in the test takes the service lock.
        with self.service.lock:
            return self.service._all('decisions')

    def read(self, table, identity):
        with self.service.lock:
            return self.service._get(table, identity)

    def replies(self, task):
        return [(m['text'], m['streaming'], bool(m.get('_interim'))) for m in self.service._task_messages(task['id'])
                if m['role'] == 'assistant' and m['kind'] == 'text']

    def test_the_gateway_is_the_only_chat_runtime_and_the_old_setting_goes_at_start(self):
        self.assertEqual(self.service.hermes_setup()['gateway'],
                         {'state': 'off', 'pid': None, 'reason': None, 'connected_profiles': [],
                          'unserved_profiles': [self.service.profiles.row(self.bot)['name']]})
        with self.assertRaises(ServiceError):
            self.write('PUT', '/v1/settings/chat-runtime', {'chat_runtime': 'worker'})
        # An install of an earlier version has the setting. The next start removes it.
        self.service.db.execute("INSERT INTO settings VALUES ('chat_runtime', '\"worker\"')")
        self.service.close()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.assertIsNone(self.service._setting('chat_runtime'))
        # Fixture mode has no Hermes and so no gateway: its provider runs the chat turn.
        self.assertNotIn('gateway', self.service.hermes_setup())
        task = self.send('Fixture mode.')
        self.service._run_task(task['id'])
        self.assertEqual(len(self.provider.starts), 1)

    def test_a_plain_chat_turn_runs_in_the_gateway(self):
        task = self.send('How are you?')
        adapter = Adapter(self.service.gateway_bridge, self.home, [
            {'kind': 'typing'},
            {'kind': 'send', 'message_id': 'm:1', 'text': 'Let me think.', 'interim': True},
            {'kind': 'send', 'message_id': 'm:2', 'text': 'I am'},
            {'kind': 'edit', 'message_id': 'm:2', 'text': 'I am well.'},
            {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.service._run_task(task['id'])
        adapter.join(5)
        self.assertEqual(self.provider.starts, [])
        self.assertEqual(adapter.turns[0]['chat_id'], self.bot['id'])
        # The message comes first. The context of the turn follows, as the worker gives it.
        self.assertTrue(adapter.turns[0]['text'].startswith('How are you?\n'))
        self.assertIn('Direct human messages eligible to request bot creation', adapter.turns[0]['text'])
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.replies(task), [('Let me think.', False, True), ('I am well.', False, False)])
        self.assertEqual(self.read('bots', self.bot['id'])['last_message'], 'I am well.')
        self.assertEqual(self.service.gateway_status()['connected_profiles'], [self.service.profiles.row(self.bot)['name']])

    def test_a_chat_turn_waits_for_the_gateway_and_runs_when_an_adapter_connects(self):
        task = self.send('Wait for the gateway.')
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while self.read('tasks', task['id'])['summary'] != gateway_chat.WAITING and time.monotonic() < deadline:
            time.sleep(.02)
        # No adapter is connected, for example while the supervised gateway starts again. The task says so.
        self.assertEqual(self.read('tasks', task['id'])['status'], 'working')
        self.assertEqual(self.read('tasks', task['id'])['summary'], gateway_chat.WAITING)
        adapter = Adapter(self.service.gateway_bridge, self.home, [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'Here now.'}, {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        runner.join(10)
        adapter.join(5)
        self.assertEqual(self.provider.starts, [])
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.replies(task), [('Here now.', False, False)])

    def test_a_chat_turn_fails_with_the_reason_when_no_gateway_connects_and_never_uses_the_worker(self):
        with patch.object(gateway_chat, 'GATEWAY_WAIT_SECONDS', 1):
            task = self.send('Nobody is there.')
            self.service._run_task(task['id'])
            stored = self.read('tasks', task['id'])
            self.assertEqual(stored['status'], 'failed')
            self.assertIn('The Hermes gateway did not connect for this bot', stored['summary'])
            self.assertNotIn('_gateway_waiting', stored)
            # An adapter that is connected but takes no turn: nothing ran, so the turn waits and then fails too.
            Adapter(self.service.gateway_bridge, self.home, [])
            with patch.object(gateway_bridge, 'TAKE_SECONDS', 0.2):
                task = self.send('Nobody takes this one.')
                self.service._run_task(task['id'])
            self.assertEqual(self.read('tasks', task['id'])['status'], 'failed')
        self.assertEqual(self.provider.starts, [])

    def test_a_cancel_ends_the_wait_for_the_gateway(self):
        task = self.send('Never mind.')
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while self.read('tasks', task['id'])['summary'] != gateway_chat.WAITING and time.monotonic() < deadline:
            time.sleep(.02)
        self.write('POST', f'/v1/tasks/{task["id"]}/cancel')
        runner.join(5)
        self.assertFalse(runner.is_alive())
        stored = self.read('tasks', task['id'])
        self.assertEqual(stored['status'], 'cancelled')
        self.assertNotIn('_gateway_waiting', stored)
        self.assertEqual(self.provider.starts, [])

    def test_a_turn_with_the_results_of_a_background_process_runs_in_the_gateway(self):
        # A worker's background process ended, and its results continue the task. The gateway gets the worker's text,
        # and Hermes saves the results with the turn, as the worker's session did.
        from coconut.native_conversation import BACKGROUND_RESULTS
        adapter = Adapter(self.service.gateway_bridge, self.home, [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'The build passed.'}, {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        task = self.send('Also check the docs.')
        with self.service.transaction():
            stored = self.read('tasks', task['id'])
            stored['_background_completion'] = ['The build finished with exit code 0.']
            self.service._put('tasks', stored)
        self.service._run_task(task['id'])
        adapter.join(5)
        turn = adapter.turns[0]
        own = BACKGROUND_RESULTS + 'The build finished with exit code 0.\nNew message from the person:\nAlso check the docs.'
        self.assertTrue(turn['text'].startswith(own))
        self.assertEqual(turn['own'], own)
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.provider.starts, [])

    def dispatch(self, task_id):
        """Start the task through the dispatcher of the service, as in a running service, and wait for its turn."""
        self.service._dispatch_pending()
        worker = self.service.running.get(task_id)
        self.assertIsNotNone(worker, 'The dispatcher did not start the task.')
        worker.join(10)

    def run_in_gateway(self, task, script, dispatch=False):
        adapter = Adapter(self.service.gateway_bridge, self.home, script)
        adapter.start()
        if dispatch:
            self.dispatch(task['id'])
        else:
            self.service._run_task(task['id'])
        adapter.join(5)
        self.assertEqual(self.provider.starts, [])
        # The model gets the text with the records that the session does not hold yet: every record in a new session.
        from coconut.native_conversation import records_text
        return adapter.turns[0]['text'] + records_text(adapter.turns[0].get('records') or [])

    def test_an_attached_file_goes_to_the_gateway_as_text_of_the_turn(self):
        import base64
        notes = self.write('POST', '/v1/attachments', {'bot_id': self.bot['id'], 'filename': 'notes.txt',
                                                       'content_base64': base64.b64encode(b'The cache is warm.').decode()})
        image = self.write('POST', '/v1/attachments', {'bot_id': self.bot['id'], 'filename': 'photo.png',
                                                       'content_base64': base64.b64encode(b'\x89PNG\xff\x00').decode()})
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
                          {'text': 'Read these.', 'attachment_ids': [notes['id'], image['id']]})
        text = self.run_in_gateway(task, [{'kind': 'send', 'message_id': 'm:1', 'text': 'Read.'},
                                          {'kind': 'done', 'outcome': 'success'}])
        self.assertTrue(text.startswith('Read these.'))
        self.assertIn('Attached file: notes.txt\n<attachment_content>\nThe cache is warm.\n</attachment_content>', text)
        self.assertIn('Attached file: photo.png\nThis attachment is binary.', text)
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')

    def bot_reply(self, text):
        first = self.send('Hello.')
        self.run_in_gateway(first, [{'kind': 'send', 'message_id': 'm:1', 'text': text},
                                    {'kind': 'done', 'outcome': 'success'}])
        self.provider.starts.clear()
        with self.service.lock:
            return next(m for m in self.service._task_messages(first['id']) if m['role'] == 'assistant')

    def test_a_reaction_turn_runs_in_the_gateway_and_may_end_in_silence(self):
        reply = self.bot_reply('The build passed.')
        self.write('POST', f'/v1/messages/{reply["id"]}/reactions', {'emoji': '👍', 'active': True})
        with self.service.lock:
            task = next(t for t in self.service._all('tasks') if t['status'] == 'queued')
        text = self.run_in_gateway(task, [
            {'kind': 'send', 'message_id': 'c:1', 'text': 'Thinking about the reaction.', 'interim': True},
            {'kind': 'delta', 'stream': 1, 'text': '[SILEN'}, {'kind': 'done', 'outcome': 'success'}])
        self.assertIn('The person changed an emoji reaction on the quoted message.', text)
        self.assertIn('The build passed.', text)
        self.assertIn('return exactly [SILENT]. Do not explain the silence.', text)
        stored = self.read('tasks', task['id'])
        self.assertEqual((stored['status'], stored['_silent_completion']), ('completed', True))
        self.assertEqual(self.replies(stored), [])

    def test_earlier_emoji_feedback_goes_with_the_next_turn_and_is_then_done(self):
        reply = self.bot_reply('Use the blue one.')
        # A passive reaction starts no turn. It waits for the next turn of the bot.
        self.write('POST', f'/v1/messages/{reply["id"]}/reactions', {'emoji': '😄', 'active': True})
        self.assertTrue(self.read('bots', self.bot['id']).get('_pending_reactions'))
        task = self.send('And the size?')
        text = self.run_in_gateway(task, [{'kind': 'send', 'message_id': 'm:1', 'text': 'Large.'},
                                          {'kind': 'done', 'outcome': 'success'}])
        self.assertTrue(text.startswith('And the size?'))
        self.assertIn('Earlier emoji feedback, supplied with this work turn', text)
        self.assertIn('"excerpt": "Use the blue one."', text)
        self.assertFalse(self.read('bots', self.bot['id']).get('_pending_reactions'))
        self.assertEqual(self.replies(task), [('Large.', False, False)])

    def test_a_quoted_reply_carries_its_quote_and_the_turn_names_the_native_conversation(self):
        first = self.send('First.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [{'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.service._run_task(first['id'])
        adapter.join(5)
        with self.service.transaction():
            answer = self.service._message(self.bot['id'], 'Use the blue one.', task_id=first['id'])
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
                          {'text': 'Do that instead.', 'reply_to_message_id': answer['id']})
        adapter = Adapter(self.service.gateway_bridge, self.home, [{'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.service._run_task(task['id'])
        adapter.join(5)
        self.assertEqual(self.provider.starts, [])
        self.assertIn('Use the blue one.', adapter.turns[0]['text'])
        self.assertIn('Do that instead.\n', adapter.turns[0]['text'])
        from coconut.native_conversation import session_root
        stored = self.read('tasks', task['id'])
        self.assertEqual(adapter.turns[0]['session_root'],
                         session_root(self.read('bots', self.bot['id']), epoch=stored.get('_conversation_epoch')))

    def test_a_question_of_the_gateway_is_a_card_and_its_answer_returns_to_the_turn(self):
        task = self.send('Deploy it.')
        bridge = self.service.gateway_bridge
        adapter = Adapter(bridge, self.home, [{'kind': 'question', 'question_id': 'q1', 'question_kind': 'clarify',
                                               'text': 'Which region?', 'choices': [
                                                   {'id': 'choice-0', 'label': 'Europe'},
                                                   {'id': 'choice-1', 'label': 'America'}]}])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(d['task_id'] == task['id'] for d in self.decisions()):
            time.sleep(.02)
        card = next(d for d in self.decisions() if d['task_id'] == task['id'])
        self.assertEqual((card['title'], card['status'], [o['label'] for o in card['options']]),
                         ('Which region?', 'pending', ['Europe', 'America']))
        # The turn waits for the answer, so the task keeps working.
        self.assertEqual(self.read('tasks', task['id'])['status'], 'working')
        self.write('POST', f'/v1/decisions/{card["id"]}/answer', {'option_id': 'choice-1', 'revision': 1})
        answers = bridge.poll({'home': str(self.home), 'wait': 0, 'active': [adapter.turns[0]['id']]})['answers']
        self.assertEqual(answers, [{'question_id': 'q1', 'text': 'America', 'choice': 'choice-1'}])
        self.assertEqual(self.read('decisions', card['id'])['status'], 'approved')
        bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'send', 'message_id': 'm:1', 'text': 'Deployed in America.'})
        bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.replies(task), [('Deployed in America.', False, False)])

    def test_an_open_question_closes_when_the_turn_ends_and_a_typed_answer_declines_a_command(self):
        task = self.send('Clean up.')
        bridge = self.service.gateway_bridge
        adapter = Adapter(bridge, self.home, [
            {'kind': 'question', 'question_id': 'q1', 'question_kind': 'approval', 'text': 'Allow rm -rf build?',
             'detail': 'rm -rf build', 'choices': [{'id': 'once', 'label': 'Allow once'},
                                                   {'id': 'deny', 'label': 'Deny', 'deny': True}]},
            {'kind': 'question', 'question_id': 'q2', 'question_kind': 'clarify', 'text': 'Anything else?'}])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len([d for d in self.decisions() if d['task_id'] == task['id']]) < 2:
            time.sleep(.02)
        approval, ask = sorted((d for d in self.decisions() if d['task_id'] == task['id']),
                               key=lambda d: d['title'])
        self.assertEqual((approval['action_type'], [o['answer'] for o in approval['options']]),
                         ('native_approval', ['approve', 'reject']))
        self.write('POST', f'/v1/decisions/{approval["id"]}/answer',
                   {'choice': 'reply', 'correction': 'Keep the build folder.', 'revision': 1})
        answers = bridge.poll({'home': str(self.home), 'wait': 0, 'active': [adapter.turns[0]['id']]})['answers']
        self.assertEqual(answers, [{'question_id': 'q1', 'text': 'Keep the build folder.', 'choice': 'deny'}])
        bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        self.assertEqual(self.read('decisions', ask['id'])['status'], 'failed')
        with self.assertRaises(ServiceError):
            self.write('POST', f'/v1/decisions/{ask["id"]}/answer', {'choice': 'reply', 'correction': 'No.', 'revision': 1})

    def test_a_dismissed_card_tells_the_waiting_turn_that_it_has_no_answer(self):
        from coconut.decision_inbox import DISMISSED_RESPONSE
        task = self.send('Clean up.')
        bridge = self.service.gateway_bridge
        adapter = Adapter(bridge, self.home, [
            {'kind': 'question', 'question_id': 'q1', 'question_kind': 'approval', 'text': 'Allow rm?',
             'choices': [{'id': 'once', 'label': 'Allow once'}, {'id': 'deny', 'label': 'Deny', 'deny': True}]},
            {'kind': 'question', 'question_id': 'q2', 'question_kind': 'clarify', 'text': 'Which folder?'}])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len([d for d in self.decisions() if d['task_id'] == task['id']]) < 2:
            time.sleep(.02)
        cards = [d for d in self.decisions() if d['task_id'] == task['id']]
        for card in cards:
            self.write('POST', f'/v1/decisions/{card["id"]}/inbox', {'action': 'dismiss', 'revision': 1})
            with self.service.transaction():
                saved = self.service._get('decisions', card['id'])
                saved['inbox_undo_until'] = '2000-01-01T00:00:00Z'
                self.service._put('decisions', saved)
        self.service.finish_inbox_swipes()
        answers = bridge.poll({'home': str(self.home), 'wait': 0, 'active': [adapter.turns[0]['id']]})['answers']
        # An approval cannot run without an answer, so Hermes denies the command. The reason says it was a dismissal.
        self.assertEqual(sorted(answers, key=lambda a: a['question_id']), [
            {'question_id': 'q1', 'text': DISMISSED_RESPONSE, 'choice': 'deny', 'dismissed': True},
            {'question_id': 'q2', 'text': DISMISSED_RESPONSE, 'choice': '', 'dismissed': True}])
        for card in cards:
            saved = self.read('decisions', card['id'])
            self.assertEqual((saved['status'], saved['_answers'], saved['decided_at']), ('dismissed', [], None))
        self.assertEqual(self.read('tasks', task['id'])['status'], 'working')
        bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(5)

    def test_a_coconut_tool_of_the_gateway_acts_for_the_task_of_its_turn(self):
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        task = self.send('Ask Lin.')
        bridge = self.service.gateway_bridge
        adapter = Adapter(bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        receipt = self.service.gateway_tool({'turn_id': turn, 'tool': 'message_bot',
                                             'arguments': {'bot': 'lin', 'text': 'Please check the logs.'}})
        self.assertEqual((receipt['status'], receipt['target_bot_id']), ('queued', other['id']))
        delivered = self.read('tasks', receipt['task_id'])
        self.assertEqual((delivered['bot_id'], delivered['source_task_id']), (other['id'], task['id']))
        with self.assertRaisesRegex(ServiceError, 'Lin'):
            self.service.gateway_tool({'turn_id': turn, 'tool': 'message_bot', 'arguments': {'bot': 'Nobody', 'text': 'x'}})
        with self.assertRaises(ServiceError):
            self.service.gateway_tool({'turn_id': turn, 'tool': 'delete_everything', 'arguments': {}})
        bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        with self.assertRaisesRegex(ServiceError, 'no longer runs'):
            self.service.gateway_tool({'turn_id': turn, 'tool': 'message_bot', 'arguments': {'bot': 'Lin', 'text': 'x'}})

    CARD = {'title': 'Ship the release?', 'context': 'The build passed and the notes are ready.',
            'options': [{'id': 'ship', 'label': 'Ship it', 'explanation': ''},
                        {'id': 'wait', 'label': 'Wait a day', 'explanation': ''}]}

    def ask_in_gateway(self, text='Can we ship?'):
        """A gateway turn that shows a card with coconut_ask. Return the task, the card and the turn's tool result."""
        task = self.send(text)
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        result = self.service.gateway_tool({'turn_id': turn, 'tool': 'ask', 'arguments': self.CARD})
        # Hermes ends the turn. Its last text comes after the card, and the task no longer takes output.
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'send', 'message_id': 'm:1', 'text': 'I asked.'})
        runner.join(5)
        self.assertFalse(runner.is_alive())
        card = next(d for d in self.decisions() if d['task_id'] == task['id'])
        return task, card, result

    def test_a_gateway_card_must_list_its_recommended_choice_first(self):
        task = self.send('Can we ship?')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        card = dict(self.CARD, recommended_option_id='wait')
        with self.assertRaisesRegex(ServiceError, 'List the recommended choice first. Move the option with id "wait"'):
            self.service.gateway_tool({'turn_id': turn, 'tool': 'ask', 'arguments': card})
        self.assertEqual([d for d in self.decisions() if d['task_id'] == task['id']], [])
        # The turn continues, and the same card with the recommended choice first is created.
        card['options'] = card['options'][::-1]
        result = self.service.gateway_tool({'turn_id': turn, 'tool': 'ask', 'arguments': card})
        self.assertTrue(result['paused'])
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'send', 'message_id': 'm:1', 'text': 'I asked.'})
        runner.join(5)
        saved = next(d for d in self.decisions() if d['task_id'] == task['id'])
        self.assertEqual(([o['id'] for o in saved['options']], saved['recommended_option_id']), (['wait', 'ship'], 'wait'))

    def test_a_card_ends_the_gateway_turn_and_its_answer_continues_in_the_gateway(self):
        from coconut.card_tools import PAUSED
        task, card, result = self.ask_in_gateway()
        self.assertEqual(result, {'status': 'waiting', 'decision_id': card['id'], 'paused': True, 'instruction': PAUSED})
        self.assertEqual((card['title'], card['status'], [o['label'] for o in card['options']]),
                         ('Ship the release?', 'pending', ['Ship it', 'Wait a day']))
        self.assertEqual(self.read('tasks', task['id'])['status'], 'needs_you')
        self.assertEqual(self.replies(task), [])
        # Each further tool call of the ended turn is refused.
        self.write('POST', f'/v1/decisions/{card["id"]}/answer', {'option_id': 'ship', 'revision': 1})
        stored = self.read('tasks', task['id'])
        self.assertEqual((stored['status'], stored['_resume_decision']), ('queued', card['id']))
        text = self.run_in_gateway(stored, [{'kind': 'send', 'message_id': 'm:1', 'text': 'Shipped.'},
                                            {'kind': 'done', 'outcome': 'success'}])
        # The worker's text for the answer continues the conversation.
        self.assertTrue(text.startswith('Continue the original task using this recorded answer.'))
        self.assertIn('"selected_option_label": "Ship it"', text)
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.provider.resumes, [])
        self.assertEqual(self.replies(task), [('Shipped.', False, False)])

    def test_a_turn_saves_only_its_own_text_and_its_context_names_the_cards_that_wait_now(self):
        # A card waits, and another card was answered in a work session. Earlier turns may have named both.
        task, card, _ = self.ask_in_gateway()
        with self.service.transaction():
            answered = dict(self.service._get('decisions', card['id']), id=str(uuid4()), title='Move the login email?',
                            status='approved')
            self.service._put('decisions', answered)
        hey = self.send('Hey')
        adapter = Adapter(self.service.gateway_bridge, self.home, [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'Hi.'}, {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.service._run_task(hey['id'])
        adapter.join(5)
        turn = adapter.turns[0]
        # Hermes saves the person's message. The model calls get the whole text, with the live cards at its end.
        self.assertEqual(turn['own'], 'Hey')
        self.assertTrue(turn['text'].startswith('Hey'))
        cards = turn['text'][turn['text'].index('Cards that wait for the person now'):]
        self.assertIn('Only this list is current', cards)
        self.assertIn(card['id'] + ': Ship the release? (Ada)', cards)
        self.assertNotIn('Move the login email?', cards)
        stored = self.read('tasks', hey['id'])
        with self.service.lock:
            # A turn of another bot's conversation does not see this conversation's card.
            other = self.write('POST', '/v1/bots', {'name': 'Lin'})
            stranger = dict(stored, bot_id=other['id'], conversation_bot_id=other['id'])
            self.assertTrue(self.service.cards_note(stranger).endswith('Only this list is current:\nnone'))
            # A shortened list does not say that a card it leaves out was answered.
            for number in range(gateway_chat.CARDS_NOTE_LIMIT):
                self.service._put('decisions', dict(self.service._get('decisions', card['id']), id=str(uuid4()),
                                                    title=f'Card {number}', created_at=f'2099-01-01T00:00:{number:02d}Z'))
            # A delegate's check reads its message history once for all cards.
            delegate = dict(stored, bot_id=other['id'])
            with patch.object(self.service, '_history_decision_ids', wraps=self.service._history_decision_ids) as reads:
                self.service.cards_note(delegate)
            self.assertEqual(reads.call_count, gateway_chat.CARDS_NOTE_LIMIT + 1)
            calls = {call.args for call in reads.call_args_list}
            self.assertEqual(calls, {(other['id'],)})
            queries = []
            self.service.db.set_trace_callback(queries.append)
            try:
                self.service.cards_note(delegate)
            finally:
                self.service.db.set_trace_callback(None)
            self.assertEqual(sum('message_audience' in query for query in queries), 1)
            note = self.service.cards_note(stored)
            self.assertNotIn('Only this list is current', note)
            self.assertIn(f'oldest {gateway_chat.CARDS_NOTE_LIMIT} of {gateway_chat.CARDS_NOTE_LIMIT + 1} waiting cards', note)
            self.assertIn('A card that it does not name may still wait', note)
            self.assertIn('- and 1 more that wait', note)
            # With no card, the list says so.
            for decision in self.service._all('decisions'):
                self.service._put('decisions', dict(decision, status='dismissed'))
            self.assertTrue(self.service.cards_note(stored).endswith('Only this list is current:\nnone'))

    def test_a_typed_answer_in_chat_resolves_a_pending_card_in_the_gateway(self):
        task, card, _ = self.ask_in_gateway()
        reply = self.send('Yes, ship it today.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(reply['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        text = adapter.turns[0]['text']
        self.assertTrue(text.startswith('Yes, ship it today.'))
        self.assertIn('Pending decisions in this conversation follow.', text)
        self.assertIn(card['id'], text)
        source = self.read('tasks', reply['id'])['_turn_message_ids'][0]
        result = self.service.gateway_tool({'turn_id': adapter.turns[0]['id'], 'tool': 'resolve_reply', 'arguments': {
            'choice': 'approve', 'option_id': 'ship', 'quote': 'ship it today', 'decision_id': card['id'],
            'decision_revision': 1, 'source_message_id': source}})
        self.assertEqual(result['status'], 'recorded')
        self.service.gateway_bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        self.assertEqual(self.read('decisions', card['id'])['status'], 'approved')
        # The answered card continues its own task.
        self.assertEqual(self.read('tasks', task['id'])['_resume_decision'], card['id'])
        self.assertEqual(self.provider.starts, [])

    def test_a_follow_up_on_a_card_runs_in_the_gateway_and_makes_no_card(self):
        task, card, _ = self.ask_in_gateway()
        self.write('POST', f'/v1/decisions/{card["id"]}/context', {'question': 'What changed since the last release?'})
        context = next(c for c in self.service._all('contexts') if c['decision_id'] == card['id'])
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        self.service._start_context(context['id'])
        adapter.join(5)
        turn = adapter.turns[0]
        self.assertEqual(turn['mode'], 'context')
        self.assertTrue(turn['text'].startswith('Respond only to this follow-up.'))
        self.assertIn('What changed since the last release?', turn['text'])
        with self.assertRaisesRegex(ServiceError, 'pending decision stays open'):
            self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'ask', 'arguments': self.CARD})
        # A follow-up on a card of the bot's conversation has no work session, so it has no worktree.
        self.assertNotIn('folder', turn)
        with self.assertRaisesRegex(ServiceError, 'only in a turn of a work session'):
            self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'workspace', 'arguments': {}})
        bridge = self.service.gateway_bridge
        # Commentary streams before a tool call, and the gateway sends it as interim after that call.
        bridge.event({'turn_id': turn['id'], 'kind': 'delta', 'stream': 1, 'text': 'Only the notes.'})
        bridge.event({'turn_id': turn['id'], 'kind': 'send', 'message_id': 'c:1', 'text': 'Only the notes.', 'interim': True})
        bridge.event({'turn_id': turn['id'], 'kind': 'send', 'message_id': 'm:1', 'text': 'Two fixes and the notes.'})
        bridge.event({'turn_id': turn['id'], 'kind': 'done', 'outcome': 'success'})
        self.service.context_threads[context['id']].join(5)
        self.assertEqual(self.read('contexts', context['id'])['status'], 'completed')
        answer = [m for m in self.service._task_messages(task['id'])
                  if m['role'] == 'assistant' and m.get('decision_id') == card['id'] and m['kind'] == 'text']
        self.assertEqual([m['text'] for m in answer], ['Two fixes and the notes.'])
        # The card stays pending, and the task waits for its answer.
        self.assertEqual((self.read('decisions', card['id'])['status'], self.read('tasks', task['id'])['status']),
                         ('pending', 'needs_you'))
        self.assertEqual(self.provider.contexts, [])

    def test_a_follow_up_on_a_card_of_a_work_session_runs_in_its_chat_with_the_research_results(self):
        # The card's task has its own work session, for example a native outcome, and background research of a worker
        # returned to the follow-up. The gateway answers it in the chat of that work session, as the worker answers it
        # in that session, with the worker's text for the results.
        from coconut.work_capacity import execution_key
        task, card, _ = self.ask_in_gateway()
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored.pop('_chat_session', None)
            self.service._put('tasks', stored)
            self.service._ensure_execution_session(self.service._get('tasks', task['id']))
        self.write('POST', f'/v1/decisions/{card["id"]}/context', {'question': 'Did the benchmark finish?'})
        workspace = Path(self.temp.name) / 'claim-workspace'
        workspace.mkdir()
        with self.service.transaction():
            context = next(c for c in self.service._all('contexts') if c['decision_id'] == card['id'])
            context['_background_completion'] = ['The benchmark finished: 12% faster.']
            context['_kanban_claim'] = {'native_id': 't_card', 'run_id': 2, 'workspace': str(workspace)}
            self.service._put('contexts', context)
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        places = []
        with patch.object(self.service, '_process_observation_current', return_value=True):
            self.service._start_context(context['id'])
            adapter.join(5)
            turn = adapter.turns[0]
            # The follow-up runs in the workspace of its claim, and coconut_workspace gives it the worktree of the
            # work session, as the worker's follow-up had them.
            places.append(self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'workspace', 'arguments': {}}))
            bridge = self.service.gateway_bridge
            bridge.event({'turn_id': turn['id'], 'kind': 'send', 'message_id': 'm:1', 'text': 'Yes, 12% faster.'})
            bridge.event({'turn_id': turn['id'], 'kind': 'done', 'outcome': 'success'})
            self.service.context_threads[context['id']].join(5)
        self.assertEqual(turn['folder'], str(workspace))
        self.assertEqual(places, [{'root': str(Path(self.service.root) / 'workspaces'),
                                   'outcome': execution_key(self.read('tasks', task['id']))}])
        stored = self.read('tasks', task['id'])
        self.assertEqual(turn['chat_id'], self.bot['id'] + ':work:' + stored['_execution_session_id'])
        self.assertEqual(turn['mode'], 'context')
        self.assertIn('The requested background research has returned.', turn['text'])
        self.assertIn('12% faster', turn['text'])
        self.assertEqual(self.read('contexts', context['id'])['status'], 'completed')
        self.assertEqual(self.provider.contexts, [])
        # The Kanban claim of such a follow-up holds a placeholder process.
        pid = self.service.kanban_worker_pid(context['id'], 'work-1')
        self.assertEqual(pid, self.service.gateway_holders[context['id']].pid)
        self.service.stop_gateway_holder(context['id'])

    def test_a_follow_up_stops_when_hermes_ends_its_kanban_claim(self):
        # As a work turn: when Hermes ends the placeholder of the follow-up's claim, the follow-up stops and fails.
        task, card, _ = self.ask_in_gateway()
        self.write('POST', f'/v1/decisions/{card["id"]}/context', {'question': 'Is the build green?'})
        context = next(c for c in self.service._all('contexts') if c['decision_id'] == card['id'])
        self.service.start_gateway_holder(context['id'])
        holder = self.service.gateway_holders[context['id']]
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        self.service._start_context(context['id'])
        adapter.join(5)
        self.assertEqual(adapter.turns[0]['mode'], 'context')
        holder.kill()
        self.service.context_threads[context['id']].join(15)
        self.assertEqual(self.read('contexts', context['id'])['status'], 'failed')
        self.assertNotIn(context['id'], self.service.gateway_holders)

    def test_an_automatic_check_of_a_card_runs_in_the_gateway_and_records_only_its_result(self):
        from datetime import datetime, timedelta, timezone
        task, card, _ = self.ask_in_gateway()
        with self.service.transaction():
            # The card is old, and its work changed since the last check.
            decision = self.service._get('decisions', card['id'])
            decision['created_at'] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
            decision['_freshness']['checked_at'] = decision['created_at']
            self.service._put('decisions', decision)
            stored = self.service._get('tasks', task['id'])
            stored['summary'] = 'The release notes changed.'
            self.service._put('tasks', stored)
        self.service._queue_decision_refreshes()
        context = next(c for c in self.service._all('contexts') if c['decision_id'] == card['id'])
        self.assertTrue(context.get('_decision_refresh'))
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        self.service._start_context(context['id'])
        adapter.join(5)
        turn = adapter.turns[0]
        self.assertEqual((turn['mode'], turn.get('silent')), ('refresh', True))
        self.assertTrue(turn['text'].startswith('Check the current evidence for this pending question.'))
        self.assertIn('coconut_refresh_decision', turn['text'])
        # The check records only its result. It answers for nobody.
        with self.assertRaisesRegex(ServiceError, 'Record the check'):
            self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'resolve_reply', 'arguments': {
                'choice': 'approve', 'option_id': 'ship', 'quote': 'ship'}})
        result = self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'refresh_decision', 'arguments': {
            'status': 'current', 'evidence': 'The release notes still need a decision.'}})
        self.assertEqual(result.get('status'), 'recorded')
        self.service.gateway_bridge.event({'turn_id': turn['id'], 'kind': 'done', 'outcome': 'success'})
        self.service.context_threads[context['id']].join(5)
        self.assertEqual(self.read('contexts', context['id'])['status'], 'completed')
        self.assertEqual((self.read('decisions', card['id'])['status'], self.read('tasks', task['id'])['status']),
                         ('pending', 'needs_you'))
        # The check sends no message to the chat, and the worker did not run it.
        self.assertEqual([m for m in self.service._task_messages(task['id'])
                          if m['role'] == 'assistant' and m.get('decision_id') == card['id'] and m['kind'] == 'text'], [])
        self.assertEqual(self.provider.contexts, [])

    def test_a_follow_up_on_a_card_cannot_record_a_check_result(self):
        task, card, _ = self.ask_in_gateway()
        self.write('POST', f'/v1/decisions/{card["id"]}/context', {'question': 'Why now?'})
        context = next(c for c in self.service._all('contexts') if c['decision_id'] == card['id'])
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        self.service._start_context(context['id'])
        adapter.join(5)
        turn = adapter.turns[0]
        with self.assertRaisesRegex(ServiceError, 'pending decision stays open'):
            self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'refresh_decision', 'arguments': {
                'status': 'withdraw', 'evidence': 'Obsolete.'}})
        self.service.gateway_bridge.event({'turn_id': turn['id'], 'kind': 'done', 'outcome': 'success'})
        self.service.context_threads[context['id']].join(5)
        self.assertEqual(self.read('decisions', card['id'])['status'], 'pending')

    def test_a_typed_reply_to_a_card_continues_in_the_gateway(self):
        task, card, _ = self.ask_in_gateway()
        self.write('POST', f'/v1/decisions/{card["id"]}/answer',
                   {'choice': 'reply', 'correction': 'Wait until Monday.', 'revision': 1})
        stored = self.read('tasks', task['id'])
        # A typed reply names the person's message, and the task resumes with it.
        self.assertEqual((stored['status'], stored['_resume_decision']), ('queued', card['id']))
        self.assertTrue(stored['_direct_reply_message_id'])
        text = self.run_in_gateway(stored, [{'kind': 'send', 'message_id': 'm:1', 'text': 'I will wait.'},
                                            {'kind': 'done', 'outcome': 'success'}])
        self.assertTrue(text.startswith('The person replied with text.'))
        self.assertIn('Wait until Monday.', text)
        self.assertEqual(self.provider.resumes, [])
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')

    def test_a_recovery_context_and_a_direct_reply_go_into_the_turn_text(self):
        from coconut.gateway_chat import work_notes
        recovery = {'previous_task_id': 'task-1', 'unknown_tools': [{'id': 'receipt-1', 'action': {'name': 'terminal'}}]}
        text = work_notes(dict(operation='start', task={'_self_recovery': recovery, '_work_context': {'id': 'work-1'}}))
        self.assertIn('Coconut queued this recovery turn.', text)
        self.assertIn('"receipt-1"', text)
        self.assertIn('Current Work record (data):', text)
        # The worker's rule for a paused outcome applies, and the block check rules do not: a check stays with the worker.
        self.assertIn('paused_by_decision_id', text)
        self.assertNotIn('coconut_record_pause', text)
        text = work_notes(dict(operation='start', task={'_direct_reply_message_id': 'm-1', 'source_bot_id': 'bot-2'}))
        self.assertIn('The person addressed you directly.', text)
        self.assertEqual(work_notes(dict(operation='start', task={'_direct_reply_message_id': 'm-1'})), '')

    def test_a_card_correction_of_the_gateway_goes_to_the_service_check(self):
        task = self.send('Reopen the withdrawn draft.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        arguments = {'decision_id': 'card-1', 'expected_revision': 2, 'withdrawal_context_id': 'context-1',
                     'reason': 'The check read the reply of another card.'}
        with patch.object(self.service, 'correct_withdrawal', return_value={'status': 'reopened'}) as correct:
            self.assertEqual(self.service.gateway_tool({'turn_id': turn, 'tool': 'correct_withdrawal',
                                                        'arguments': arguments}), {'status': 'reopened'})
        correct.assert_called_once_with(task['id'], arguments)
        # Without a withdrawn card in the scope of the turn, the service check refuses it.
        with self.assertRaises(ServiceError):
            self.service.gateway_tool({'turn_id': turn, 'tool': 'correct_withdrawal', 'arguments': arguments})
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)

    def exchange(self, other, text, legacy=True):
        """A message of the bot `other` to Ada from a working task of `other`.

        A new exchange runs in a work session of Ada. With `legacy`, the exchange is a turn of Ada's conversation, as
        exchanges were before: such tasks can remain in a store, and the person's turn still goes first."""
        with self.service.transaction():
            requester = self.service._send(self.read('bots', other['id']), {'text': 'Ask Ada.', 'mode': 'queue'},
                                           human_authored=False)
            stored = self.service._get('tasks', requester['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        identity = self.service._deliver_bot_message(requester['id'], dict(target_bot_id=self.bot['id'], text=text,
                                                                           require_response=True))['task_id']
        if legacy:
            with self.service.transaction():
                stored = self.service._get('tasks', identity)
                stored['_chat_session'] = True
                self.service._put('tasks', stored)
        return identity

    def test_bot_traffic_runs_in_work_sessions_beside_the_person_chat(self):
        # A message of another bot is bot traffic. It runs in a work session of Ada, in a chat of its own, so the
        # person's chat neither waits for it nor holds it in its Hermes session.
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        exchange = self.exchange(other, 'Please check the release notes in detail.', legacy=False)
        stored = self.read('tasks', exchange)
        self.assertFalse(stored.get('_chat_session'))
        person = self.send('Are you there?')
        bridge = self.service.gateway_bridge
        turns = []
        done = threading.Event()
        self.addCleanup(done.set)
        def serve():
            while not done.is_set():
                for turn in bridge.poll({'home': str(self.home), 'wait': .1, 'active': [t['id'] for t in turns]})['turns']:
                    turns.append(turn)
                    if 'Are you there?' in turn['text']:
                        bridge.event({'turn_id': turn['id'], 'kind': 'send', 'message_id': 'm:1', 'text': 'Here.'})
                        bridge.event({'turn_id': turn['id'], 'kind': 'done', 'outcome': 'success'})
        threading.Thread(target=serve, daemon=True).start()
        bridge.poll({'home': str(self.home), 'wait': 0})
        # The exchange starts and keeps running. The person's turn runs beside it, in the person's chat.
        self.service._dispatch_pending()
        deadline = time.monotonic() + 5
        while len(turns) < 2 and time.monotonic() < deadline:
            self.service._dispatch_pending()
            time.sleep(.05)
        chats = {('Are you there?' in turn['text']): turn['chat_id'] for turn in turns}
        execution = self.read('tasks', exchange)['_execution_session_id']
        self.assertEqual(chats, {True: self.bot['id'], False: self.bot['id'] + ':work:' + execution})
        deadline = time.monotonic() + 5
        while self.read('tasks', person['id'])['status'] != 'completed' and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.read('tasks', person['id'])['status'], 'completed')
        self.assertEqual(self.read('tasks', exchange)['status'], 'working')
        exchange_turn = next(turn for turn in turns if 'Are you there?' not in turn['text'])
        bridge.event({'turn_id': exchange_turn['id'], 'kind': 'done', 'outcome': 'success'})

    def test_a_message_of_the_person_never_waits_behind_bot_traffic(self):
        from coconut.work_capacity import person_turn
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        turns, stops, done = [], [], set()
        lock = threading.Lock()
        bridge = self.service.gateway_bridge
        stop_adapter = threading.Event()
        self.addCleanup(stop_adapter.set)

        def adapter():
            # Each bot turn runs until Coconut stops it, as a long bot-to-bot turn. A turn of the person, and a bot turn
            # that runs again, ends at once.
            while not stop_adapter.is_set():
                with lock:
                    active = [turn['id'] for turn in turns if turn['id'] not in done]
                result = bridge.poll({'home': str(self.home), 'wait': .1, 'active': active})
                with lock:
                    turns.extend(dict(turn, at=time.monotonic()) for turn in result['turns'])
                    stops.extend(result['stop'])
                for turn in result['turns']:
                    quick = 'Are you there?' in turn['text'] or turn['text'].startswith(gateway_chat.PREEMPTED)
                    if quick:
                        bridge.event({'turn_id': turn['id'], 'kind': 'send', 'message_id': 'm:' + turn['id'],
                                      'text': 'Here.'})
                        bridge.event({'turn_id': turn['id'], 'kind': 'done', 'outcome': 'success'})
                        with lock:
                            done.add(turn['id'])
        threading.Thread(target=adapter, daemon=True).start()
        # The conversation dispatcher of the service runs as in a running service.
        dispatcher = threading.Thread(target=self.service._dispatch_conversations, daemon=True)
        dispatcher.start()
        self.addCleanup(self.service.stopping.set)

        def wait(predicate, seconds=10):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                with lock:
                    if predicate():
                        return True
                time.sleep(.05)
            return False

        # A background turn runs beside the conversation: the audit of a work source, in the chat of its work session.
        audit = self.source_audit()
        threading.Thread(target=self.dispatch, args=(audit['id'],), daemon=True).start()
        self.assertTrue(wait(lambda: any(':work:' in turn['chat_id'] for turn in turns)))
        # A bot exchange runs in Ada's conversation, and a second one waits in the queue.
        running = self.exchange(other, 'Please check the release notes in detail.')
        self.assertTrue(wait(lambda: any('release notes' in turn['text'] for turn in turns)))
        queued = self.exchange(other, 'Then check the changelog too.')
        self.assertFalse(person_turn(self.read('tasks', queued)))
        # The person writes. The running exchange stops, and the person's turn starts before the queued exchange.
        sent = time.monotonic()
        person = self.send('Are you there?')
        self.assertTrue(person_turn(self.read('tasks', person['id'])))
        self.assertTrue(wait(lambda: any('Are you there?' in turn['text'] for turn in turns)))
        with lock:
            started = next(turn for turn in turns if 'Are you there?' in turn['text'])
            order = [turn['text'] for turn in turns if turn['chat_id'] == self.bot['id']]
        self.assertLess(started['at'] - sent, 3)
        self.assertEqual(stops, [self.bot['id']])
        self.assertTrue(wait(lambda: self.read('tasks', person['id'])['status'] == 'completed'))
        # Then the stopped exchange runs again, with its input and a note, and then the queued one.
        self.assertTrue(wait(lambda: self.read('tasks', running)['status'] == 'completed'
                             and len([t for t in turns if t['chat_id'] == self.bot['id']]) >= 3))
        with lock:
            order = [turn['text'] for turn in turns if turn['chat_id'] == self.bot['id']]
        self.assertIn('release notes', order[0])
        self.assertIn('Are you there?', order[1])
        self.assertTrue(order[2].startswith(gateway_chat.PREEMPTED))
        self.assertIn('release notes', order[2])
        self.assertNotIn('_preempted', self.read('tasks', running))
        # The stopped turn kept no part of its streamed reply, and it did not fail.
        self.assertNotIn('failed', [self.read('tasks', identity)['status'] for identity in (running, person['id'])])
        # The audit still runs in its own chat. It did not stop.
        self.assertEqual(self.read('tasks', audit['id'])['status'], 'working')
        self.assertIn(queued, [t['id'] for t in self.service._all('tasks') if t['status'] in ('queued', 'working')])

    def test_the_person_gets_a_slot_when_bot_traffic_fills_the_capacity(self):
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        third = self.write('POST', '/v1/bots', {'name': 'Mo'})
        hold = threading.Event()
        self.addCleanup(hold.set)
        # Capacity 1: two bot exchanges of other bots run, and fill it.
        for bot in (other, third):
            with self.service.transaction():
                task = self.service._send(self.read('bots', bot['id']), {'text': 'Bot work', 'mode': 'queue'},
                                          human_authored=False)
                stored = self.service._get('tasks', task['id'])
                stored.update(status='working', _chat_session=True, _bot_messages=[{'text': 'x'}])
                self.service._put('tasks', stored)
                worker = threading.Thread(target=hold.wait, daemon=True)
                worker.start()
                self.service.running[task['id']] = worker
        exchange = self.exchange(other, 'Check the logs.')
        person = self.send('Are you there?')
        started = []
        before = set(self.service.running)
        with patch.object(self.service, '_run_task', side_effect=started.append):
            with self.service.lock:
                self.service._dispatch_chat(1)
            # Each started thread calls the stand-in of _run_task.
            for identity, worker in list(self.service.running.items()):
                if identity not in before:
                    worker.join(5)
        # The person's turn starts. The bot exchange waits for capacity.
        self.assertEqual(started, [person['id']])
        self.assertEqual(self.read('tasks', exchange)['status'], 'queued')

    def test_a_stop_before_the_gateway_has_the_turn_is_tried_again(self):
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        exchange = self.exchange(other, 'Please check the release notes.')
        person = self.send('Are you there?')
        with self.service.transaction():
            stored = self.service._get('tasks', exchange)
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        # The service still builds the context of the turn, so the gateway has no turn to stop. No mark stays.
        with self.service.lock:
            self.assertFalse(self.service.preempt_for_person(exchange, person['id']))
        self.assertNotIn('_preempted_by', self.read('tasks', exchange))
        # A later pass reaches the turn once the gateway has it.
        bridge = self.service.gateway_bridge
        runner = threading.Thread(target=bridge.run_turn, args=(exchange, self.home, self.bot['id'], 'Check.',
                                                                lambda kind, payload: None), daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while not any(turn.task_id == exchange for turn in bridge.turns.values()) and time.monotonic() < deadline:
            time.sleep(.02)
        with self.service.lock:
            self.assertTrue(self.service.preempt_for_person(exchange, person['id']))
        runner.join(5)
        self.assertFalse(runner.is_alive())
        self.assertEqual(self.read('tasks', exchange)['_preempted_by'], person['id'])

    def test_an_answer_of_the_person_to_a_card_of_a_bot_exchange_is_a_person_turn(self):
        from coconut.work_capacity import person_turn
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        exchange = self.read('tasks', self.exchange(other, 'Please check the release notes.'))
        self.assertFalse(person_turn(exchange))
        answered = dict(exchange, _resume_decision='card-1')
        self.assertTrue(person_turn(answered))
        # Another message of the person does not stop it.
        with self.service.transaction():
            self.service._put('tasks', dict(answered, status='working'))
        with self.service.lock:
            self.assertFalse(self.service.preempt_for_person(exchange['id'], 'other-person-task'))

    def test_one_pass_starts_no_more_bot_traffic_than_the_capacity(self):
        lin = self.write('POST', '/v1/bots', {'name': 'Lin'})
        mo = self.write('POST', '/v1/bots', {'name': 'Mo'})
        people = [self.write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Hello.'})['id'] for bot in (lin, mo)]
        exchanges = [self.exchange(lin, 'Check the logs.'), self.exchange(mo, 'Check the notes.')]
        started = []
        before = set(self.service.running)
        with patch.object(self.service, '_run_task', side_effect=started.append):
            with self.service.lock:
                self.service._dispatch_chat(1)
            # Each started thread calls the stand-in of _run_task.
            for identity, worker in list(self.service.running.items()):
                if identity not in before:
                    worker.join(5)
        # Capacity 1 and its reserved slot: the two people start, and the bot exchanges wait.
        self.assertEqual(sorted(started), sorted(people))
        self.assertTrue(all(self.read('tasks', identity)['status'] == 'queued' for identity in exchanges))

    def test_a_message_of_another_bot_and_its_return_run_in_the_gateway(self):
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        other_home = self.service.profiles.home(other)
        task = self.send('Ask Lin to check the logs.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        sent = self.service.gateway_tool({'turn_id': turn, 'tool': 'message_bot',
                                          'arguments': {'bot': 'Lin', 'text': 'Please check the error logs of today.'}})
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'send', 'message_id': 'm:1', 'text': 'I asked Lin.'})
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        # The person waits on this turn, so Hermes must answer it (MessageEvent.reply_expected).
        self.assertFalse(adapter.turns[0].get('silent'))
        # The message is bot traffic: Lin's task runs in a work session of Lin, in the chat of that session, not in
        # Lin's conversation with the person. The text has the delivered message, as the worker gives it.
        adapter = Adapter(self.service.gateway_bridge, other_home, [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'The logs are clean.'}, {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.dispatch(sent['task_id'])
        adapter.join(5)
        delivered = self.read('tasks', sent['task_id'])
        self.assertFalse(delivered.get('_chat_session'))
        # No person waits on bot traffic, so its turn may end in silence.
        self.assertTrue(adapter.turns[0].get('silent'))
        text = adapter.turns[0]['text']
        self.assertEqual(adapter.turns[0]['chat_id'], other['id'] + ':work:' + delivered['_execution_session_id'])
        # The delivered message is a record of the turn, which the session of the chat keeps once (turn_records).
        self.assertNotIn('delivered by the service', text)
        records = adapter.turns[0]['records']
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]['id'].startswith('message:0:'))
        self.assertIn('Explicit message delivered by the service.', records[0]['text'])
        self.assertIn('Please check the error logs of today.', records[0]['text'])
        self.assertEqual(self.read('tasks', sent['task_id'])['status'], 'completed')
        # The result returns to Ada as a new task, which also runs in a work session of Ada in the gateway.
        # The dispatcher of the service returns the result (Service._drain_returns).
        self.service._drain_returns()
        with self.service.lock:
            returned = next(t for t in self.service._all('tasks') if t['bot_id'] == self.bot['id']
                            and t.get('_is_return') and t['status'] == 'queued')
        text = self.run_in_gateway(returned, [{'kind': 'send', 'message_id': 'm:1', 'text': 'Lin says the logs are clean.'},
                                              {'kind': 'done', 'outcome': 'success'}], dispatch=True)
        self.assertIn('The logs are clean.', text)
        self.assertIn('This reply continues your original assignment.', text)
        self.assertEqual(self.read('tasks', returned['id'])['status'], 'completed')
        self.assertFalse(self.read('tasks', returned['id']).get('_chat_session'))
        self.assertTrue(self.read('tasks', returned['id'])['_execution_session_id'])

    def test_a_failed_exchange_can_be_asked_again_in_the_gateway(self):
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        other_home = self.service.profiles.home(other)
        task = self.send('Ask Lin to check the logs.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        sent = self.service.gateway_tool({'turn_id': turn, 'tool': 'message_bot',
                                          'arguments': {'bot': 'Lin', 'text': 'Please check the logs.'}})
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        # Lin's turn fails in the gateway, and the failure returns to Ada.
        adapter = Adapter(self.service.gateway_bridge, other_home, [{'kind': 'done', 'outcome': 'failure'}])
        adapter.start()
        self.dispatch(sent['task_id'])
        adapter.join(5)
        self.assertEqual(self.read('tasks', sent['task_id'])['status'], 'failed')
        # A store from before this change: Lin's failed task ran in Lin's conversation with the person.
        with self.service.transaction():
            failed = self.service._get('tasks', sent['task_id'])
            failed['_chat_session'] = True
            failed.pop('_execution_session_id', None)
            self.service._put('tasks', failed)
        self.service._drain_returns()
        with self.service.lock:
            returned = next(t for t in self.service._all('tasks') if t['bot_id'] == self.bot['id']
                            and t.get('_is_return') and t['status'] == 'queued')
        # Ada asks Lin again from the return. The retry is bot traffic too: it runs in a work session of Lin, and it
        # gets the context of the failed task, whose history is in Lin's chat with the person.
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        self.service._dispatch_pending()
        adapter.join(5)
        retry = self.service.gateway_tool({'turn_id': adapter.turns[0]['id'], 'tool': 'message_bot',
                                           'arguments': {'bot': 'Lin', 'text': 'Please try the logs again.'}})
        self.service.gateway_bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        self.service.running[returned['id']].join(10)
        stored = self.read('tasks', retry['task_id'])
        self.assertEqual((stored['status'], bool(stored.get('_chat_session'))), ('queued', False))
        self.assertEqual((stored['_continued_from']['task_id'], stored['_continued_from']['request']),
                         (sent['task_id'], 'Please check the logs.'))
        adapter = Adapter(self.service.gateway_bridge, other_home, [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'The logs are clean now.'}, {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.dispatch(retry['task_id'])
        adapter.join(5)
        self.assertIn('This message continues your failed task.', adapter.turns[0]['text'])
        self.assertIn('Please check the logs.', adapter.turns[0]['text'])
        self.assertEqual(self.read('tasks', retry['task_id'])['status'], 'completed')
        self.assertEqual(self.provider.starts, [])

    def test_an_accepted_proposal_continues_in_the_gateway(self):
        task = self.send('Fix the typo.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        self.service.gateway_tool({'turn_id': turn, 'tool': 'suggest', 'arguments': dict(self.CARD, key='release')})
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        card = next(d for d in self.decisions() if d['task_id'] == task['id'])
        self.write('POST', f'/v1/decisions/{card["id"]}/answer', {'option_id': 'ship', 'revision': 1})
        # The proposal continues in a new task of the bot's conversation that resumes the card.
        continuation = self.read('tasks', self.read('decisions', card['id'])['_execution_task_id'])
        text = self.run_in_gateway(continuation, [{'kind': 'send', 'message_id': 'm:1', 'text': 'Shipping.'},
                                                  {'kind': 'done', 'outcome': 'success'}], dispatch=True)
        self.assertNotIn('_execution_session_id', self.read('tasks', continuation['id']))
        self.assertTrue(text.startswith('Continue the original task using this recorded answer.'))
        self.assertEqual(self.read('tasks', continuation['id'])['status'], 'completed')

    def test_a_pr_review_of_a_chat_task_runs_in_the_gateway_with_the_worker_text(self):
        from coconut.pull_request_reviews import PR_REVIEW_INSTRUCTIONS
        task = self.send('Open the PR for the docs fix.')
        self.run_in_gateway(task, [{'kind': 'done', 'outcome': 'success'}])
        review = {'url': 'https://github.com/example/shop/pull/7', 'pr_state': 'open', 'head_sha': 'abc123',
                  'checks_state': 'success'}
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored.update(status='queued', _pr_review=review)
            self.service._put('tasks', stored)
        text = self.run_in_gateway(self.read('tasks', task['id']), [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'The PR is ready to merge.'}, {'kind': 'done', 'outcome': 'success'}],
            dispatch=True)
        self.assertTrue(text.startswith(PR_REVIEW_INSTRUCTIONS))
        self.assertIn('"head_sha": "abc123"', text)
        self.assertIn('Original task context:', text)
        self.assertEqual(self.provider.starts, [])

    def test_github_source_activity_goes_into_the_turn_text(self):
        from coconut.gateway_chat import work_notes
        activity = [{'url': 'https://github.com/example/shop/issues/3', 'new_activity': [{'body': 'Please ship it.'}]},
                    {'url': 'https://github.com/example/shop/issues/4', 'read_failed': True}]
        text = work_notes(dict(operation='start', task={'_source_activity': activity,
                                                        '_tracked_pr_observations': [{'url': 'pr-7', 'checks': 'green'}]}))
        self.assertIn('Current GitHub source activity since the last revision Coconut recorded', text)
        self.assertIn('Please ship it.', text)
        self.assertIn('Coconut could not read these GitHub sources: https://github.com/example/shop/issues/4', text)
        self.assertIn('Latest saved GitHub observations for these PRs', text)

    def test_a_chat_creates_and_deletes_a_bot_with_the_tools_of_the_gateway(self):
        task = self.send('Please create a bot named Scout that watches the build.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        text = adapter.turns[0]['text']
        source = self.read('tasks', task['id'])['_turn_message_ids'][0]
        # The turn lists the person's message that can ask for a bot, as the worker gives it.
        self.assertIn('Direct human messages eligible to request bot creation', text)
        self.assertIn(source, text)
        turn = adapter.turns[0]['id']
        created = self.service.gateway_tool({'turn_id': turn, 'tool': 'create_bot', 'arguments': {
            'source_message_id': source, 'name': 'Scout', 'role': 'Watches the build.', 'instructions': 'Watch the build.'}})
        self.assertEqual((created['status'], created['name']), ('created', 'Scout'))
        with self.assertRaises(ServiceError):
            self.service.gateway_tool({'turn_id': turn, 'tool': 'create_bot', 'arguments': {
                'source_message_id': 'not-a-message', 'name': 'Other', 'role': 'x', 'instructions': ''}})
        deleted = self.service.gateway_tool({'turn_id': turn, 'tool': 'delete_bot', 'arguments': {
            'source_message_id': source, 'target_bot_id': created['bot_id'], 'expected_name': 'Scout'}})
        self.assertEqual((deleted['status'], deleted['bot_id']), ('deleted', created['bot_id']))
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        self.assertEqual(self.provider.starts, [])

    def failed_exchange(self):
        """Ada asks Lin through the gateway, Lin's turn fails, and its failure returns to Ada. Ada's return turn runs.
        Return Lin's bot, the failed task and the running return task."""
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        other_home = self.service.profiles.home(other)
        task = self.send('Ask Lin to check the logs.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        sent = self.service.gateway_tool({'turn_id': adapter.turns[0]['id'], 'tool': 'message_bot',
                                          'arguments': {'bot': 'Lin', 'text': 'Please check the logs.'}})
        self.service.gateway_bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        adapter = Adapter(self.service.gateway_bridge, other_home, [{'kind': 'done', 'outcome': 'failure'}])
        adapter.start()
        self.dispatch(sent['task_id'])
        adapter.join(5)
        self.service._drain_returns()
        with self.service.lock:
            returned = next(t for t in self.service._all('tasks') if t['bot_id'] == self.bot['id']
                            and t.get('_is_return') and t['status'] == 'queued')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        self.service._dispatch_pending()
        adapter.join(5)
        return other, self.read('tasks', sent['task_id']), returned, adapter.turns[0]['id']

    def test_a_coordinator_cannot_continue_a_failed_gateway_task_that_the_person_stopped(self):
        other, failed, returned, turn = self.failed_exchange()
        # The person stopped the exchange of the failed task. The coordinator works in a later, active exchange.
        with self.service.transaction():
            exchange = self.service._get('exchanges', failed['exchange_id'])
            exchange['cancelled'] = True
            self.service._put('exchanges', exchange)
            stored = self.service._get('tasks', returned['id'])
            stored['exchange_id'] = self.service._new_exchange()
            self.service._put('tasks', stored)
        tasks = len(self.service._all('tasks'))
        with self.assertRaisesRegex(ServiceError, 'paused or cancelled'):
            self.service.emit(returned['id'], 'bot_message', {
                'target_bot_id': other['id'], 'text': 'Please continue the log check.', 'continuation_task_id': failed['id']})
        # Nothing starts the stopped work again.
        self.assertEqual(len(self.service._all('tasks')), tasks)
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        self.service.running[returned['id']].join(10)

    def test_a_coordinator_continues_a_failed_gateway_task_in_its_conversation(self):
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        other_home = self.service.profiles.home(other)
        task = self.send('Ask Lin to check the logs.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        sent = self.service.gateway_tool({'turn_id': adapter.turns[0]['id'], 'tool': 'message_bot',
                                          'arguments': {'bot': 'Lin', 'text': 'Please check the logs.'}})
        self.service.gateway_bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        adapter = Adapter(self.service.gateway_bridge, other_home, [{'kind': 'done', 'outcome': 'failure'}])
        adapter.start()
        self.dispatch(sent['task_id'])
        adapter.join(5)
        self.service._drain_returns()
        with self.service.lock:
            returned = next(t for t in self.service._all('tasks') if t['bot_id'] == self.bot['id']
                            and t.get('_is_return') and t['status'] == 'queued')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        self.service._dispatch_pending()
        adapter.join(5)
        # The coordinator names the failed task. The message is bot traffic, so it runs in a work session of Lin.
        retry = self.service.emit(returned['id'], 'bot_message', {
            'target_bot_id': other['id'], 'text': 'Please continue the log check.', 'continuation_task_id': sent['task_id']})
        self.service.gateway_bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        self.service.running[returned['id']].join(10)
        stored = self.read('tasks', retry['task_id'])
        self.assertEqual((stored['status'], bool(stored.get('_chat_session')), stored['bot_id']), ('queued', False, other['id']))

    def audit(self):
        """A task outside the bot's conversation, which the dispatchers give its own work session."""
        with self.service.transaction():
            task = self.service._send(self.read('bots', self.bot['id']),
                                      {'text': 'Reconcile these open assignments against the original sources.', 'mode': 'queue'},
                                      human_authored=False)
        return self.read('tasks', task['id'])

    def test_a_recorded_wait_ends_the_turn_and_a_repeat_records_nothing(self):
        # On the mini, Desk called coconut_wait with no_card thousands of times in one turn: each call returned only
        # "recorded", and the model called it again. A recorded wait ends the turn now.
        from coconut import gateway_chat
        task = self.audit()
        turn = self.running_turn(task)
        calls = []
        def wait_for_task(task_id, payload):
            calls.append(payload)
            if not payload:
                return {'tasks': [], 'decisions': []}
            if payload.get('task_id') == 'done-task':
                return {'status': 'completed', 'task_id': 'done-task', 'result': 'Done.'}
            return {'status': 'recorded', 'next_check_at': '2026-11-02T14:00:00+00:00'}
        def wait(**arguments):
            return self.service.gateway_tool({'turn_id': turn, 'tool': 'wait', 'arguments': arguments})
        no_card = dict(no_card=True, next_check_at='2026-11-02T14:00:00Z')
        with patch.object(self.service, 'wait_for_task', side_effect=wait_for_task):
            # A list of the waits and a finished task's result do not end the turn.
            self.assertNotIn('ended', wait())
            self.assertNotIn('ended', wait(task_id='done-task', reason='Read the result'))
            recorded = wait(reason='#4138 same rev', **no_card)
            self.assertEqual((recorded['status'], recorded['ended']), ('recorded', True))
            self.assertEqual(recorded['instruction'], gateway_chat.WAIT_FINISH)
            # The same wait again, in other words, records nothing and says to finish.
            again = wait(reason='#4138 rev same', **no_card)
            self.assertEqual((again['status'], again['ended']), ('already_recorded', True))
            self.assertEqual(len(calls), 3)
            # A turn has at most WAIT_CALLS wait calls.
            for number in range(gateway_chat.WAIT_CALLS - 3):
                wait(reason='Another', no_card=True, next_check_at='2026-11-0%dT14:00:00Z' % (3 + number))
            # The call after the limit ends the turn, so the plugin blocks each further tool call.
            spent = wait(reason='One more', no_card=True, next_check_at='2026-11-09T14:00:00Z')
            self.assertEqual((spent['status'], spent['ended']), ('limit', True))
            self.assertIn('Finish this turn now', spent['instruction'])
        self.assertEqual(len(calls), gateway_chat.WAIT_CALLS)

    def running_turn(self, task, **fields):
        """A gateway turn of `task`, which runs now, with these task fields."""
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored.update(status='working', **fields)
            self.service._put('tasks', stored)
        return self.service.gateway_bridge.open_turn(task['id'], self.home, self.bot['id'])

    def test_a_work_session_tracks_its_work_and_continues_an_outcome_with_the_gateway_tools(self):
        from coconut.gateway_chat import TRACK_WORK_CHAT, TRACK_WORK_OUTCOME
        task = self.audit()
        turn = self.running_turn(task, _execution_session_id=task['id'])
        tracked = self.service.gateway_tool({'turn_id': turn, 'tool': 'track_work', 'arguments': {
            'key': 'account-export', 'title': 'Export the accounts', 'activity': 'Writing the export',
            'status': 'working'}})
        with self.service.lock:
            item = next(a for a in self.service._all('artifacts') if a['kind'] == 'work')
        self.assertEqual((item['title'], item['task_id']), ('Export the accounts', task['id']))
        self.assertEqual(tracked['id'], item['id'])
        # coconut_continue_work goes to the service with its arguments, which checks the outcome and the owner.
        arguments = {'task_id': 't_export', 'message': 'The person added the missing column.'}
        with patch.object(self.service, 'continue_native_work', return_value={'status': 'queued'}) as continued:
            self.assertEqual(self.service.gateway_tool({'turn_id': turn, 'tool': 'continue_work', 'arguments': arguments}),
                             {'status': 'queued'})
        self.assertEqual(continued.call_args.args, (task['id'], arguments))
        # A turn of the bot's chat records an outcome on the board, and a native outcome reports on its card.
        chat = self.running_turn(self.send('Track the export.'))
        with self.assertRaisesRegex(ServiceError, 'coconut_kanban_create'):
            self.service.gateway_tool({'turn_id': chat, 'tool': 'track_work', 'arguments': {}})
        self.assertIn('coconut_kanban_create', TRACK_WORK_CHAT)
        outcome = self.running_turn(self.audit(), _execution_session_id='work-2', _native_outcome_id='t_export')
        with self.assertRaisesRegex(ServiceError, 'coconut_kanban_heartbeat'):
            self.service.gateway_tool({'turn_id': outcome, 'tool': 'track_work', 'arguments': {}})
        self.assertIn('coconut_kanban_heartbeat', TRACK_WORK_OUTCOME)
        # A technical lane holds both tools, as it holds the card tools.
        lane = self.running_turn(self.audit(), _execution_session_id='work-3', _technical_lane={'scope': ['diagnosis']})
        for tool in ('continue_work', 'track_work'):
            with self.subTest(tool=tool), self.assertRaises(ServiceError) as raised:
                self.service.gateway_tool({'turn_id': lane, 'tool': tool, 'arguments': {}})
            self.assertEqual(raised.exception.status, 403)

    def test_a_task_with_its_own_work_session_runs_in_the_gateway_in_the_chat_of_that_session(self):
        from coconut.native_conversation import session_root
        task = self.audit()
        adapter = Adapter(self.service.gateway_bridge, self.home, [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'No card is needed.'}, {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.dispatch(task['id'])
        adapter.join(5)
        stored = self.read('tasks', task['id'])
        execution = stored['_execution_session_id']
        turn = adapter.turns[0]
        # The work session has a chat of its own, beside the bot's conversation, with the worker's session root.
        self.assertEqual(turn['chat_id'], self.bot['id'] + ':work:' + execution)
        self.assertEqual(turn['session_root'], session_root(self.read('bots', self.bot['id']), execution=execution,
                                                            epoch=stored.get('_conversation_epoch')))
        self.assertTrue(turn['text'].startswith('Reconcile these open assignments against the original sources.'))
        self.assertEqual(stored['status'], 'completed')
        self.assertEqual(self.provider.starts, [])

    def work_question(self, text='Is the export ready?'):
        """A Work item without a native outcome, and a question of the person about it."""
        owner = self.audit()
        with self.service.transaction():
            stored = self.service._get('tasks', owner['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        item = self.service._register_work(owner['id'], dict(key='account-export', title='Export account data',
                                                             activity='Preparing the export', status='working'))
        with self.service.transaction():
            stored = self.service._get('tasks', owner['id'])
            stored['status'] = 'completed'
            self.service._put('tasks', stored)
        question = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
                              dict(text=text, mode='queue', work_artifact_id=item['id']))
        return item, self.read('tasks', question['id'])

    def test_a_question_about_a_work_item_runs_in_the_gateway_in_its_work_chat(self):
        from coconut.card_tools import WORKSPACE_INSTRUCTIONS
        item, question = self.work_question()
        self.assertTrue(question['_work_context_id'] and question['_execution_session_id'])
        self.assertTrue(gateway_chat.gateway_work_turn(question))
        adapter = Adapter(self.service.gateway_bridge, self.home, [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'The export is half done.'}, {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.dispatch(question['id'])
        adapter.join(5)
        turn = adapter.turns[0]
        stored = self.read('tasks', question['id'])
        self.assertEqual(turn['chat_id'], self.bot['id'] + ':work:' + stored['_execution_session_id'])
        # The worker's text for a question about a Work item, and its rule for the worktree of a work session.
        self.assertTrue(turn['text'].startswith('Is the export ready?'))
        self.assertIn('Attached work context, refreshed for this question.', turn['text'])
        self.assertIn('Export account data', turn['text'])
        self.assertIn(WORKSPACE_INSTRUCTIONS.strip(), turn['text'])
        self.assertEqual(stored['status'], 'completed')
        self.assertEqual(self.provider.starts, [])
        # A question about a Work item with a native outcome is a turn of that outcome, and it runs in the gateway too.
        self.assertTrue(gateway_chat.gateway_work_turn(dict(question, _native_outcome_id='native-1')))

    def test_a_turn_of_a_work_session_gets_the_place_of_its_worktree(self):
        from coconut.work_capacity import execution_key
        item, question = self.work_question('Fix the export.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.dispatch, args=(question['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        # The registry and the outcome that a prepared worker gets (COCONUT_WORKSPACE_ROOT, COCONUT_WORKSPACE_OUTCOME).
        self.assertEqual(self.service.gateway_tool({'turn_id': turn, 'tool': 'workspace', 'arguments': {}}),
                         {'root': str(Path(self.service.root) / 'workspaces'),
                          'outcome': execution_key(self.read('tasks', question['id']))})
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        # A turn of the bot's conversation has no work session, so it has no worktree.
        chat = self.send('Hello.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(chat['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        with self.assertRaisesRegex(ServiceError, 'only in a turn of a work session'):
            self.service.gateway_tool({'turn_id': adapter.turns[0]['id'], 'tool': 'workspace', 'arguments': {}})
        self.service.gateway_bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(5)

    def source_audit(self, text='Reconcile these open assignments against the original sources. Source data follows.'):
        """An audit of a work source, as work_sources queues it: a task without a message, with its audit text."""
        with self.service.transaction():
            task = self.service._send(self.read('bots', self.bot['id']), {'text': 'Review open assignment', 'mode': 'queue'},
                                      record_message=False, human_authored=False)
            task = self.service._get('tasks', task['id'])
            task['_source_audit_prompt'] = text
            self.service._put('tasks', task)
        return self.read('tasks', task['id'])

    def test_an_audit_of_a_work_source_runs_in_the_gateway_with_the_board_tools_of_coconut(self):
        from coconut.gateway_chat import AUDIT_BOARD_TOOLS
        audit = self.source_audit()
        adapter = Adapter(self.service.gateway_bridge, self.home, [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'No card is needed.'}, {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.dispatch(audit['id'])
        adapter.join(5)
        turn = adapter.turns[0]
        stored = self.read('tasks', audit['id'])
        self.assertEqual(turn['chat_id'], self.bot['id'] + ':work:' + stored['_execution_session_id'])
        # The worker's audit text, the board tools of this chat, and the mode in which the hooks block the gateway's board.
        self.assertTrue(turn['text'].startswith('Reconcile these open assignments against the original sources.'))
        self.assertIn(AUDIT_BOARD_TOOLS.strip(), turn['text'])
        self.assertEqual(turn['mode'], 'audit')
        self.assertEqual(stored['status'], 'completed')
        self.assertEqual(self.provider.starts, [])

    def test_the_board_tools_of_an_audit_run_on_the_board_of_coconut_with_receipts(self):
        import json as _json
        calls = []
        class Board:
            tasks = {}
            def call(self, request):
                calls.append(request)
                if request['arguments'].get('title'):
                    return {'result': _json.dumps({'ok': True, 'task_id': 't_new'})}
                return {'result': _json.dumps({'ok': True, 'task_id': request['arguments']['task_id'], 'comment_id': 7})}
            def close(self):
                pass
        audit = self.source_audit()
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.dispatch, args=(audit['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        # The turn runs. Coconut's board stands in for the Hermes Kanban runtime.
        self.service.kanban = Board()
        created = self.service.gateway_tool({'turn_id': turn, 'tool': 'kanban_create',
                                             'arguments': {'title': 'Fix the export', 'body': 'Scope and criteria.'}})
        commented = self.service.gateway_tool({'turn_id': turn, 'tool': 'kanban_comment',
                                               'arguments': {'task_id': 't_old', 'body': 'Source revision 3.'}})
        self.assertEqual((created['task_id'], commented['comment_id']), ('t_new', 7))
        # Coconut gives kanban_create the worker's arguments: the bot's tenant, a blocked card, and an idempotency key.
        arguments = calls[0]['arguments']
        self.assertEqual((calls[0]['op'], calls[0]['name'], arguments['tenant'], arguments['initial_status']),
                         ('tool', 'kanban_create', self.bot['id'], 'blocked'))
        self.assertTrue(arguments['idempotency_key'].startswith('coconut:' + audit['id'] + ':'))
        self.assertTrue(arguments['workspace_path'].startswith(str(Path(self.service.root) / 'kanban/kanban/workspaces')))
        self.assertEqual(calls[0]['profile'], self.service.profiles.row(self.read('bots', self.bot['id']))['name'])
        # Each call is a completed receipt of the turn, as for a worker. The comment records its board task.
        with self.service.lock:
            receipts = [r for r in self.service._all('native_receipts') if r['task_id'] == audit['id']]
        self.assertEqual([(r['action']['name'], r['state']) for r in receipts],
                         [('kanban_create', 'completed'), ('kanban_comment', 'completed')])
        self.assertEqual(receipts[0]['action']['arguments'], arguments)
        self.assertEqual(self.read('tasks', audit['id'])['_source_native_ids'], ['t_old'])
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        # A chat turn writes to the board of Coconut too, as a worker of the chat did. Only a native outcome acts on the
        # run of a card.
        chat = self.send('Hello.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(chat['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        self.service.gateway_tool({'turn_id': adapter.turns[0]['id'], 'tool': 'kanban_comment',
                                   'arguments': {'task_id': 't_old', 'body': 'x'}})
        self.assertEqual((calls[-1]['name'], calls[-1]['claim']), ('kanban_comment', None))
        for name in ('kanban_complete', 'kanban_block', 'kanban_heartbeat', 'kanban_schedule', 'kanban_request_review'):
            with self.assertRaisesRegex(ServiceError, 'Only a turn of a native outcome'):
                self.service.gateway_tool({'turn_id': adapter.turns[0]['id'], 'tool': name,
                                           'arguments': {'task_id': 't_old', 'summary': 'x'}})
        self.service.gateway_bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        self.assertEqual(len(calls), 3)

    def native_outcome(self):
        """A native outcome of the bot with the Kanban claim of its card, held by a placeholder, as the dispatcher
        claims it for a gateway turn."""
        task = self.audit()
        workspace = Path(self.temp.name) / 'card-workspace'
        workspace.mkdir()
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            self.service._ensure_execution_session(stored)
            stored = self.service._get('tasks', task['id'])
            stored.update(_native_outcome_id='t_card', _kanban_claim={
                'native_id': 't_card', 'run_id': 3, 'claim_lock': 'host:77', 'external_id': task['id'],
                'workspace': str(workspace), 'context': 'Card body: ship the export.'})
            self.service._put('tasks', stored)
        self.service.start_gateway_holder(task['id'])
        return self.read('tasks', task['id']), workspace

    def test_a_native_outcome_runs_in_the_gateway_and_its_board_tools_act_for_its_claim(self):
        import json as _json
        from coconut.gateway_chat import OUTCOME_BOARD_TOOLS
        calls = []
        bot = self.bot['id']
        class Board:
            tasks = {}
            def call(self, request):
                calls.append(request)
                if request['name'] == 'kanban_show':
                    tenant = 'other-bot' if request['arguments'].get('task_id') == 't_other' else bot
                    return {'result': _json.dumps({'task': {'id': request['arguments'].get('task_id') or 't_card',
                                                            'tenant': tenant}})}
                if request['name'] == 'kanban_list':
                    return {'result': _json.dumps({'tasks': []})}
                return {'result': _json.dumps({'ok': True, 'task_id': 't_card'})}
            def close(self):
                pass
        task, workspace = self.native_outcome()
        holder = self.service.gateway_holders[task['id']]
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]
        # The outcome runs in the chat of its work session, in the workspace of its claim, with the native task, the
        # outcome rules and the Coconut names of the board tools. The hooks block the gateway's own board.
        self.assertEqual(turn['chat_id'], bot + ':work:' + task['_execution_session_id'])
        self.assertEqual((turn['mode'], turn['folder']), ('outcome', str(workspace)))
        self.assertIn('Native kanban task:\nCard body: ship the export.', turn['text'])
        self.assertIn('Use kanban_complete only after verifying every accepted outcome.', turn['text'])
        self.assertIn(OUTCOME_BOARD_TOOLS.strip(), turn['text'])
        self.assertEqual(self.provider.starts, [])
        self.service.kanban = Board()
        def tool(name, **arguments):
            return self.service.gateway_tool({'turn_id': turn['id'], 'tool': name, 'arguments': arguments})
        claim = {'native_id': 't_card', 'run_id': 3, 'claim_lock': 'host:77'}
        # A worker's board tools act for the claim: the runtime gives them its worker env, and the board is Coconut's.
        self.assertTrue(tool('kanban_heartbeat', note='Exporting.', board='elsewhere')['ok'])
        self.assertEqual((calls[-1]['name'], calls[-1]['claim'], calls[-1]['arguments']),
                         ('kanban_heartbeat', claim, {'note': 'Exporting.'}))
        tool('kanban_show')
        self.assertEqual(calls[-1]['claim'], claim)
        # kanban_list and kanban_unblock route the board, so they run without the claim, and only for this bot.
        tool('kanban_list', tenant='other-bot')
        self.assertEqual((calls[-1]['claim'], calls[-1]['arguments']['tenant']), (None, bot))
        with self.assertRaisesRegex(ServiceError, 'only a Hermes task of this bot'):
            tool('kanban_unblock', task_id='t_other')
        self.assertTrue(tool('kanban_unblock', task_id='t_child')['ok'])
        self.assertEqual((calls[-1]['name'], calls[-1]['claim']), ('kanban_unblock', None))
        self.assertEqual(holder.poll(), None)
        # kanban_complete ends the run of the claim. Its placeholder stops, and the turn continues without a claim.
        # A PR that delivers the outcome names it, as with the worker's tool.
        with patch.object(self.service, 'emit', return_value={'status': 'recorded'}) as emit:
            tool('track_pr', url='https://github.com/example/app/pull/7', work_id='t_card')
        emit.assert_called_once_with(task['id'], 'artifact', {'kind': 'pr', 'url': 'https://github.com/example/app/pull/7',
                                                              'work_id': 't_card'})
        self.assertTrue(tool('kanban_complete', summary='The export ships.')['ok'])
        self.assertEqual(calls[-1]['claim'], claim)
        self.assertIsNotNone(holder.poll())
        with self.assertRaisesRegex(ServiceError, 'no longer holds the Kanban claim'):
            tool('kanban_heartbeat', note='Late.')
        time.sleep(2.5)
        self.service.gateway_bridge.event({'turn_id': turn['id'], 'kind': 'send', 'message_id': 'm:1', 'text': 'Shipped.'})
        self.service.gateway_bridge.event({'turn_id': turn['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(10)
        # The stopped placeholder is no lost claim, so the turn ends as it ran.
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')
        self.assertFalse(self.service.claim_finished(task['id']))
        # Each write is a receipt of the turn, as for a worker. A read has none.
        with self.service.lock:
            receipts = [r['action']['name'] for r in self.service._all('native_receipts') if r['task_id'] == task['id']]
        self.assertEqual(receipts, ['kanban_heartbeat', 'kanban_unblock', 'kanban_complete'])

    def test_a_stop_of_the_service_ends_the_wait_for_a_busy_chat(self):
        from coconut import gateway_bridge as bridge_module
        task = self.service.mutate('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Hello'}, str(uuid4()))
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        self.service.gateway_bridge.poll({'home': str(self.home), 'wait': 0})
        self.service.stopping.set()
        from coconut import gateway_chat
        with patch.object(self.service.gateway_bridge, 'run_turn', side_effect=bridge_module.GatewayBusy('busy')) as run_turn, \
                patch.object(gateway_chat, 'BUSY_WAIT_SECONDS', 1):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'service stops'):
                self.service._run_in_gateway(stored, self.bot, 'Hello', lambda *_: None)
        self.assertEqual(run_turn.call_count, 1)
        self.assertLess(time.monotonic() - started, 2)

    def test_a_turn_waits_while_hermes_runs_another_turn_in_its_chat(self):
        # After a restart, Hermes runs its own turn in the chat of a work session, for example for a finished
        # background delegation. The plugin then skips the Coconut turn and reports it as busy. The turn must not end
        # as a reply: its Kanban claim stays held, and the turn starts again when the chat is free.
        from coconut import gateway_chat
        task, _ = self.native_outcome()
        holder = self.service.gateway_holders[task['id']]
        busy = Adapter(self.service.gateway_bridge, self.home, [{'kind': 'done', 'outcome': 'busy'}])
        busy.start()
        with patch.object(gateway_chat, 'BUSY_RETRY_SECONDS', 0.2):
            runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
            runner.start()
            busy.join(5)
            self.assertEqual(busy.answers, [{'ok': True}])
            deadline = time.monotonic() + 5
            while self.read('tasks', task['id']).get('summary') != gateway_chat.BUSY_WAITING and time.monotonic() < deadline:
                time.sleep(0.05)
            waiting = self.read('tasks', task['id'])
            self.assertEqual((waiting['status'], waiting['summary']), ('working', gateway_chat.BUSY_WAITING))
            self.assertIsNone(holder.poll())
            # The chat is free: the gateway takes the same turn again, and it runs.
            free = Adapter(self.service.gateway_bridge, self.home, [
                {'kind': 'send', 'message_id': 'm:1', 'text': 'Scheduled the next check.'},
                {'kind': 'done', 'outcome': 'success'}])
            free.start()
            free.join(10)
            runner.join(10)
        self.assertEqual(free.turns[0]['chat_id'], busy.turns[0]['chat_id'])
        self.assertNotEqual(free.turns[0]['id'], busy.turns[0]['id'])
        replies = [m['text'] for m in self.service._task_messages(task['id']) if m['role'] == 'assistant' and m['kind'] == 'text']
        self.assertEqual(replies, ['Scheduled the next check.'])

    def test_a_native_outcome_turn_gets_its_worktree_proof_its_receipts_and_a_pause_only_in_a_block_check(self):
        import json as _json
        from coconut.work_capacity import execution_key
        task, _ = self.native_outcome()
        # An earlier attempt of this turn left a receipt.
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['_native_turn_id'] = 'turn-1'
            self.service._put('tasks', stored)
            self.service._put('native_receipts', dict(
                id='r-1', task_id=task['id'], turn_id='turn-1', state='completed', result='{"ok": true}',
                action={'name': 'kanban_comment', 'arguments': {'task_id': 't_card', 'body': 'Exported half.'}}))
        self.service.kanban = type('Board', (), {'tasks': {}, 'close': lambda self: None})()
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]
        # The turn text names the saved receipts, and coconut_read_receipts reads them, as in the worker.
        self.assertIn('Saved native receipt index', turn['text'])
        read = self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'read_receipts',
                                          'arguments': {'receipt_id': 'r-1'}})
        self.assertIn('Exported half.', _json.dumps(read))
        # coconut_workspace of an outcome carries the proof of its claim, for a successor of a retired worktree. The
        # service names the claim and the board.
        place = self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'workspace', 'arguments': {}})
        self.assertEqual(place['outcome'], execution_key(self.read('tasks', task['id'])))
        self.assertEqual(place['successor'], {
            'database': str(Path(self.service.root) / 'kanban' / 'kanban.db'), 'task_id': task['id'],
            'native_id': 't_card', 'tenant': self.bot['id'],
            'claim': {'native_id': 't_card', 'run_id': 3, 'claim_lock': 'host:77', 'external_id': task['id']}})
        # Only a check of a blocked native task records a pause.
        with self.assertRaisesRegex(ServiceError, 'Only a check of a blocked native task'):
            self.service.gateway_tool({'turn_id': turn['id'], 'tool': 'record_pause', 'arguments': {'decision_id': 'd'}})
        self.service.gateway_bridge.event({'turn_id': turn['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(10)
        self.service.kanban = None

    def test_audits_in_the_gateway_do_not_take_the_slots_of_chats(self):
        audits = [self.source_audit() for _ in range(3)]
        hold = threading.Event()
        self.addCleanup(hold.set)
        with self.service.transaction():
            for audit in audits:
                stored = self.service._get('tasks', audit['id'])
                self.service._ensure_execution_session(stored)
                stored['status'] = 'working'
                self.service._put('tasks', stored)
                worker = threading.Thread(target=hold.wait, daemon=True)
                worker.start()
                self.service.running[audit['id']] = worker
        chat = self.send('Are you there?')
        started = []
        with patch.object(self.service, '_run_task', side_effect=started.append), self.service.lock:
            self.service._dispatch_chat(1)
        self.assertEqual(started, [chat['id']])

    def test_the_placeholders_of_claims_end_with_the_service(self):
        # A service that stops ends the claims of its turns, so Hermes runs their cards again after the restart.
        with tempfile.TemporaryDirectory() as folder:
            service = Service(folder, Provider(), start_workers=False)
            service.start_gateway_holder('claim-1', 'work-1')
            holder = service.gateway_holders['claim-1']
            self.assertIsNone(holder.poll())
            service.close()
            self.assertIsNotNone(holder.poll())
            self.assertEqual(service.gateway_holders, {})

    def test_a_placeholder_ends_when_its_service_process_is_gone(self):
        # A service that crashed cannot stop its placeholders. Each one sees that its service is gone and ends, so
        # Hermes does not keep the claim of a turn that no longer runs.
        service = ('import os, subprocess, sys\n'
                   'holder = subprocess.Popen([sys.executable, "-c", sys.argv[1], str(os.getpid())],'
                   ' stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,'
                   ' start_new_session=True)\n'
                   'print(holder.pid, flush=True)\n')
        started = subprocess.run([sys.executable, '-c', service, gateway_chat.HOLDER], capture_output=True, text=True,
                                 timeout=30, check=True)
        holder = int(started.stdout)

        def alive():
            # A system without a reaper of orphans keeps an exited holder as a zombie (state Z). It has exited.
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(holder)], capture_output=True, text=True).stdout
            return bool(state.strip()) and not state.strip().startswith('Z')

        def stop():
            if alive():
                os.kill(holder, 9)
        self.addCleanup(stop)
        deadline = time.monotonic() + 15
        while alive() and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertFalse(alive())

    def test_a_kanban_claim_of_a_gateway_turn_holds_a_placeholder_process(self):
        task = self.audit()
        with self.service.transaction():
            self.service._ensure_execution_session(self.service._get('tasks', task['id']))
        pid = self.service.kanban_worker_pid(task['id'], 'work-1')
        holder = self.service.gateway_holders[task['id']]
        self.assertEqual((pid, holder.poll()), (holder.pid, None))
        # The placeholder carries the outcome of the work session, so Coconut sees the outcome as running.
        from coconut.work_workspace import process_outcomes
        self.assertIn('work-1', process_outcomes())
        # Also without a gateway (fixture mode), a claim holds a placeholder: Coconut starts no worker.
        with patch.object(self.service, 'gateway_enabled', return_value=False):
            pid = self.service.kanban_worker_pid('other', 'work-2')
        self.assertEqual(pid, self.service.gateway_holders['other'].pid)
        self.service.stop_gateway_holder('other')
        self.service.stop_gateway_holder(task['id'])
        self.assertIsNotNone(holder.poll())

    def test_a_turn_that_only_the_worker_could_run_fails_with_the_reason(self):
        from coconut.gateway_chat import UNSUPPORTED
        task, card, _ = self.ask_in_gateway()
        # A task that an earlier version gave to the worker: the gateway runs no turn of it.
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['_native_job'] = {'id': 'job-1'}
            self.service._put('tasks', stored)
        # A follow-up on its card.
        self.write('POST', f'/v1/decisions/{card["id"]}/context', {'question': 'Why now?'})
        context = next(c for c in self.service._all('contexts') if c['decision_id'] == card['id'])
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(5)
        stored = self.read('contexts', context['id'])
        self.assertEqual((stored['status'], stored['error_code']), ('failed', 'gateway_unsupported'))
        self.assertEqual(self.provider.contexts, [])
        # The answer to its card.
        self.write('POST', f'/v1/decisions/{card["id"]}/answer', {'option_id': 'ship', 'revision': 1})
        self.service._run_task(task['id'])
        stored = self.read('tasks', task['id'])
        self.assertEqual((stored['status'], stored['summary']), ('failed', UNSUPPORTED))
        self.assertEqual(self.provider.resumes, [])
        # A new turn of such a task.
        other = self.send('Check the queue.')
        with self.service.transaction():
            stored = self.service._get('tasks', other['id'])
            stored['_native_job'] = {'id': 'job-2'}
            self.service._put('tasks', stored)
        self.service._run_task(other['id'])
        stored = self.read('tasks', other['id'])
        self.assertEqual((stored['status'], stored['summary']), ('failed', UNSUPPORTED))
        self.assertEqual(self.provider.starts, [])

    def test_a_work_turn_stops_when_hermes_ends_its_kanban_claim(self):
        task = self.audit()
        with self.service.transaction():
            self.service._ensure_execution_session(self.service._get('tasks', task['id']))
        holder_pid = self.service.start_gateway_holder(task['id'])
        holder = self.service.gateway_holders[task['id']]
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        self.service._dispatch_pending()
        adapter.join(5)
        self.assertEqual(holder.pid, holder_pid)
        # Hermes ends the claim, for example after its run limit.
        holder.kill()
        self.service.running[task['id']].join(15)
        stored = self.read('tasks', task['id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertIn('Kanban claim', stored['summary'])

    def test_a_work_turn_that_waits_for_the_gateway_does_not_start_after_hermes_ends_its_kanban_claim(self):
        task = self.audit()
        with self.service.transaction():
            self.service._ensure_execution_session(self.service._get('tasks', task['id']))
        self.service.start_gateway_holder(task['id'])
        holder = self.service.gateway_holders[task['id']]
        # No adapter is connected, so the turn waits for the gateway. Hermes ends the claim during the wait.
        self.service._dispatch_pending()
        holder.kill()
        self.service.running[task['id']].join(15)
        self.assertFalse(self.service.running[task['id']].is_alive())
        stored = self.read('tasks', task['id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertIn('Kanban claim', stored['summary'])
        # An adapter that connects later gets no turn of this work.
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        adapter.join(3)
        self.assertEqual(adapter.turns, [])

    def test_a_status_question_runs_in_the_gateway_without_tools(self):
        task = self.send('How far is the docs work?')
        with self.service.transaction():
            working = self.service._get('tasks', task['id'])
            working['status'] = 'working'
            self.service._put('tasks', working)
        stored = dict(self.read('tasks', task['id']), _work_status_only=True,
                      _work_context={'id': 'work-1', 'title': 'Docs', 'activity': 'Writing the README.'})
        adapter = Adapter(self.service.gateway_bridge, self.home, [
            {'kind': 'send', 'message_id': 'm:1', 'text': 'The README is half done.'}, {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        emitted = []
        self.assertTrue(self.service.run_gateway_chat(stored, self.read('bots', self.bot['id']), [],
                                                      lambda kind, payload: emitted.append(kind)))
        adapter.join(5)
        turn = adapter.turns[0]
        self.assertEqual((turn['chat_id'], turn['mode']), (self.bot['id'] + ':status:' + task['id'], 'status'))
        self.assertIn('Writing the README.', turn['text'])
        self.assertIn('Do not call tools.', turn['text'])
        self.assertIn('completed', emitted)

    def test_a_command_approval_of_a_worker_turn_resumes_in_the_worker(self):
        from coconut.gateway_chat import gateway_resume
        plain = {'_chat_session': True}
        self.assertTrue(gateway_resume(plain, {'action_type': 'question'}))
        self.assertTrue(gateway_resume(plain, {'action_type': 'draft_message'}))
        # The worker recorded the exact tool call that the person allowed, and it repeats that call.
        self.assertFalse(gateway_resume(plain, {'action_type': 'native_approval'}))
        self.assertFalse(gateway_resume(plain, {'action_type': 'question', '_receipt_id': 'receipt'}))
        # A task of a later group stays with the worker.
        self.assertFalse(gateway_resume(dict(plain, _work_context_id='work'), {'action_type': 'question'}))
        # A task of the bot's conversation from another bot continues in the gateway too.
        self.assertTrue(gateway_resume(dict(plain, source_bot_id='bot-2'), {'action_type': 'question'}))
        self.assertTrue(gateway_resume({}, {'action_type': 'question'}, cron_session='cron-session'))

    def test_a_dismissed_card_continues_nothing(self):
        task, card, _ = self.ask_in_gateway()
        with self.service.lock:
            stored = self.service._get('decisions', card['id'])
        Adapter(self.service.gateway_bridge, self.home, [])
        self.assertTrue(self.service.run_gateway_resume(self.read('tasks', task['id']), self.bot, [],
                                                        dict(stored, status='dismissed'), lambda *_: None))
        # No turn reached the gateway, and the worker did not resume.
        self.assertEqual(self.service.gateway_bridge.poll({'home': str(self.home), 'wait': 0})['turns'], [])
        self.assertEqual(self.provider.resumes, [])

    def test_an_unfinished_task_list_continues_in_the_gateway_with_the_worker_text(self):
        from coconut.native_conversation import WORK_RESUME
        from coconut.work_progress import WORK_INSTRUCTIONS
        task = self.send('Update the docs.')
        todos = [{'id': 'readme', 'content': 'Edit the README.', 'status': 'pending'}]
        def save(turn, status):
            return self.service.gateway_tool({'turn_id': turn, 'tool': 'todos', 'arguments': {
                'todos': [dict(todos[0], status=status)], 'revision': 1}})
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        self.assertEqual(save(adapter.turns[0]['id'], 'in_progress'), {'status': 'recorded', 'saved': True})
        self.service.gateway_bridge.event({'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        stored = self.read('tasks', task['id'])
        # Hermes made a checklist. Coconut adds the root of the assignment and keeps the work, as for the worker.
        self.assertEqual([item['id'] for item in stored['_work_todos']['todos']], ['coconut-assignment', 'readme'])
        self.assertEqual((stored['status'], stored.get('_work_resume')), ('queued', True))
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        text = adapter.turns[0]['text']
        self.assertTrue(text.startswith(WORK_RESUME))
        self.assertIn(WORK_INSTRUCTIONS, text)
        self.assertIn('Saved task list of this assignment (data):', text)
        turn = adapter.turns[0]['id']
        self.service.gateway_tool({'turn_id': turn, 'tool': 'todos', 'arguments': {'todos': [
            {'id': 'coconut-assignment', 'content': 'Update the docs.', 'status': 'completed'},
            dict(todos[0], status='completed')], 'revision': 2}})
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'send', 'message_id': 'm:1', 'text': 'Done.'})
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        self.assertEqual(self.read('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.provider.starts, [])

    def test_a_checklist_without_the_assignment_root_completes_when_its_steps_finish(self):
        task = self.send('Fix the two typos.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        def save(status):
            # Hermes's own list: Coconut never adds its root there.
            self.service.gateway_tool({'turn_id': turn, 'tool': 'todos', 'arguments': {'todos': [
                {'id': 'one', 'content': 'Fix the first typo.', 'status': 'completed'},
                {'id': 'two', 'content': 'Fix the second typo.', 'status': status}], 'revision': 1}})
            return [(item['id'], item['status']) for item in self.read('tasks', task['id'])['_work_todos']['todos']]
        self.assertEqual(save('in_progress'), [('coconut-assignment', 'pending'), ('one', 'completed'),
                                               ('two', 'in_progress')])
        self.assertEqual(save('completed'), [('coconut-assignment', 'completed'), ('one', 'completed'),
                                             ('two', 'completed')])
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'send', 'message_id': 'm:1', 'text': 'Fixed.'})
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        # The work is finished, so Coconut queues no continuation.
        stored = self.read('tasks', task['id'])
        self.assertEqual((stored['status'], stored.get('_work_resume')), ('completed', None))

    def test_coconut_wait_sends_a_process_of_the_gateway_to_the_process_tool_of_hermes(self):
        from coconut.card_tools import GATEWAY_PROCESS_WAIT, WAIT_SCHEMA
        self.assertNotIn('process_id', WAIT_SCHEMA['parameters']['properties'])
        task = self.send('Run the build and wait for it.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        with self.assertRaisesRegex(ServiceError, 'native process tool') as raised:
            self.service.gateway_tool({'turn_id': turn, 'tool': 'wait', 'arguments': {
                'process_id': 'proc_1', 'reason': 'The build runs.'}})
        self.assertEqual(str(raised.exception), GATEWAY_PROCESS_WAIT)
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)

    def test_coconut_wait_records_a_wait_for_other_work_of_the_exchange(self):
        other = self.write('POST', '/v1/bots', {'name': 'Lin'})
        task = self.send('Ask Lin, then use the answer.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']
        sent = self.service.gateway_tool({'turn_id': turn, 'tool': 'message_bot',
                                          'arguments': {'bot': 'Lin', 'text': 'Check the logs.'}})
        listing = self.service.gateway_tool({'turn_id': turn, 'tool': 'wait', 'arguments': {}})
        self.assertIn(sent['task_id'], [item['id'] for item in listing['tasks']])
        waiting = self.service.gateway_tool({'turn_id': turn, 'tool': 'wait', 'arguments': {
            'task_id': sent['task_id'], 'reason': 'Needs the result of Lin.'}})
        self.assertEqual((waiting['status'], waiting['task_id']), ('waiting', sent['task_id']))
        with self.assertRaises(ServiceError):
            self.service.gateway_tool({'turn_id': turn, 'tool': 'wait', 'arguments': {'task_id': 'other', 'reason': 'x'}})
        self.service.gateway_bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
        runner.join(5)
        self.assertEqual(self.read('tasks', sent['task_id'])['bot_id'], other['id'])

    def test_a_cancelled_turn_stops_in_the_gateway(self):
        task = self.send('A long answer, please.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [{'kind': 'send', 'message_id': 'm:1', 'text': 'First'}])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        self.write('POST', f'/v1/tasks/{task["id"]}/cancel')
        runner.join(5)
        self.assertFalse(runner.is_alive())
        self.assertEqual(self.read('tasks', task['id'])['status'], 'cancelled')
        self.assertEqual(self.service.gateway_bridge.poll({'home': str(self.home), 'wait': 0})['stop'], [self.bot['id']])
        self.assertEqual(self.service.gateway_bridge.event(
            {'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'cancelled'}), {'ok': False})


class OutsideTurnTests(unittest.TestCase):
    """POST /v1/gateway/messages: a send of the gateway outside a Coconut turn becomes a message of the bot."""

    def setUp(self):
        from test_push import pair_phone
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, Provider(), start_workers=False)
        self.addCleanup(self.service.close)
        self.device = pair_phone(self.service)
        self.service.push.registration(self.device, dict(token='a' * 64, environment='development', enabled=True,
                                                         preferences=dict(level='every', messages=True)))
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Ada'}, str(uuid4()), self.device)
        self.home = str(self.service.profiles.home(self.bot))

    def message(self, kind='send', message_id='outside:1', text='The build finished.', **fields):
        return self.service.gateway_message(dict(dict(home=self.home, chat_id=self.bot['id'], kind=kind,
                                                      message_id=message_id, text=text), **fields))

    def chat(self):
        with self.service.lock:
            return [(m['text'], m['task_id'], bool(m.get('_hidden'))) for m in self.service._all('messages')
                    if m['bot_id'] == self.bot['id'] and m['role'] == 'assistant' and m['kind'] == 'text']

    def pushes(self):
        return self.service.db.execute('SELECT kind,task_id,message_id FROM push_items ORDER BY rowid').fetchall()

    def test_a_send_is_a_message_of_the_bot_in_a_finished_task_and_it_pushes_as_a_reply(self):
        answer = self.message()
        self.assertTrue(answer['recorded'])
        with self.service.lock:
            task = self.service._get('tasks', answer['task_id'])
            bot = self.service._get('bots', self.bot['id'])
        self.assertEqual((task['status'], task['bot_id'], task['conversation_bot_id'], task['title']),
                         ('completed', self.bot['id'], self.bot['id'], 'The build finished.'))
        self.assertEqual(self.chat(), [('The build finished.', task['id'], False)])
        self.assertEqual((bot['last_message'], bot['unread_count']), ('The build finished.', 1))
        message_id = self.service.db.execute('SELECT id FROM messages ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        # One push for the message. The task sends no second push for its completion.
        self.assertEqual(self.pushes(), [('message', task['id'], message_id)])
        # A second send of the same message, for example after a lost answer, shows nothing new.
        self.assertEqual(self.message(), {'recorded': True, 'task_id': task['id']})
        self.assertEqual(len(self.chat()), 1)

    def test_a_send_from_a_work_chat_of_the_bot_is_a_message_of_the_bot(self):
        answer = self.message(chat_id=self.bot['id'] + ':work:session-1', text='The work is done.')
        self.assertTrue(answer['recorded'])
        self.assertEqual(self.chat(), [('The work is done.', answer['task_id'], False)])
        self.assertEqual(self.message(chat_id=self.bot['id'] + ':work:session-1', text='The work is done.'),
                         {'recorded': True, 'task_id': answer['task_id']})
        # A send of another chat of the bot does not end a reply that the work chat still streams.
        work = self.bot['id'] + ':work:session-2'
        self.message(message_id='work:1', text='The', chat_id=work, streaming=True)
        self.message(message_id='main:1', text='Hello.')
        with self.service.lock:
            streamed = [m for m in self.service._all('messages') if m.get('_gateway_message_id') == 'work:1']
        self.assertTrue(streamed[0]['streaming'])
        # The same message ID in another chat is another message.
        self.assertTrue(self.message('edit', 'main:1', 'Hello again.', chat_id=work).get('recorded') is False)
        self.message(message_id='work:2', text='Done.', chat_id=work)
        with self.service.lock:
            streamed = [m for m in self.service._all('messages') if m.get('_gateway_message_id') == 'work:1']
        self.assertFalse(streamed[0]['streaming'])

    def test_sends_close_together_share_a_task_and_a_streamed_reply_is_edited_and_deleted(self):
        first = self.message(text='Task t_1 is done.')
        second = self.message(message_id='outside:2', text='The')
        self.assertEqual(second['task_id'], first['task_id'])
        self.assertEqual(self.message('edit', 'outside:2', 'The report is ready.')['recorded'], True)
        self.assertEqual([text for text, _, _ in self.chat()], ['Task t_1 is done.', 'The report is ready.'])
        self.assertTrue(self.message('delete', 'outside:2', '')['recorded'])
        self.assertEqual([hidden for _, _, hidden in self.chat()], [False, True])
        self.assertFalse(self.message('edit', 'outside:9', 'Unknown.')['recorded'])
        # A send after the grouping time starts a new task, and so does one after another task of the chat.
        with self.service.transaction():
            task = self.service._get('tasks', first['task_id'])
            task['_gateway_outside'] -= gateway_chat.OUTSIDE_GROUP_SECONDS + 1
            self.service._put('tasks', task)
        third = self.message(message_id='outside:3', text='Later news.')
        self.assertNotEqual(third['task_id'], first['task_id'])
        self.service.mutate('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Thanks.'}, str(uuid4()), self.device)
        self.assertNotEqual(self.message(message_id='outside:4', text='More news.')['task_id'], third['task_id'])

    def bot_row(self):
        with self.service.lock:
            bot = self.service._get('bots', self.bot['id'])
        return bot['last_message'], bot['unread_count']

    def test_a_streamed_reply_pushes_when_it_ends_and_a_deleted_reply_leaves_the_preview(self):
        first = self.message(text='Task t_1 is done.')
        self.assertEqual(len(self.pushes()), 1)
        self.message(message_id='outside:2', text='The', streaming=True)
        self.message('edit', 'outside:2', 'The report')
        # No push while Hermes still streams the reply.
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(self.bot_row(), ('The report', 2))
        self.message('edit', 'outside:2', 'The report is ready.', finalize=True)
        message_id = self.service.db.execute('SELECT id FROM messages ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        self.assertEqual(self.pushes()[-1], ('message', first['task_id'], message_id))
        self.assertEqual(self.bot_row(), ('The report is ready.', 2))
        # A deleted reply no longer counts as unread, and the preview shows the reply before it.
        self.message('delete', 'outside:2', '')
        self.assertEqual(self.bot_row(), ('Task t_1 is done.', 1))
        self.assertFalse(self.message('edit', 'outside:2', 'Back again.')['recorded'])
        # A streamed reply whose last edit never came ends with the next message, and it pushes then.
        self.message(message_id='outside:3', text='Half', streaming=True)
        self.message(message_id='outside:4', text='Next.')
        with self.service.lock:
            streaming = [m['streaming'] for m in self.service._all('messages') if m.get('_gateway_message_id')]
        self.assertEqual(streaming, [False] * 4)
        self.assertEqual([kind for kind, _, _ in self.pushes()], ['message'] * 4)

    def test_a_deleted_reply_that_the_person_read_leaves_the_unread_count(self):
        self.message(text='Task t_1 is done.')
        self.service.mutate('POST', f'/v1/bots/{self.bot["id"]}/read', {}, str(uuid4()), self.device)
        self.message(message_id='outside:2', text='The report is ready.')
        self.assertEqual(self.bot_row(), ('The report is ready.', 1))
        # The person read the first reply, so only the second reply stays unread.
        self.message('delete', 'outside:1', '')
        self.assertEqual(self.bot_row(), ('The report is ready.', 1))
        self.message('delete', 'outside:2', '')
        self.assertEqual(self.bot_row(), ('', 0))

    def test_an_edit_changes_the_preview_only_for_the_newest_message(self):
        self.message(text='Done.')
        self.service.mutate('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Done.'}, str(uuid4()), self.device)
        # The preview shows the message of the person, although the earlier reply has the same text.
        self.message('edit', 'outside:1', 'The build finished.')
        self.assertEqual(self.bot_row()[0], 'Done.')

    def test_a_cron_delivery_of_a_run_that_the_chat_shows_is_dropped(self):
        self.assertTrue(self.message(job_id='intake')['recorded'])
        started = self.service.gateway_cron({'event': 'start', 'home': self.home, 'job_id': 'intake',
                                             'job_name': 'Intake', 'execution_id': 'run-1'})
        self.service.gateway_bridge.event({'turn_id': started['turn_id'], 'kind': 'done', 'outcome': 'success'})
        self.service.gateway_cron_threads[started['task_id']].join(5)
        answer = self.message(message_id='outside:2', text='Two new items.', job_id='intake')
        self.assertEqual(answer, {'recorded': False, 'reason': 'A routine task in the chat shows this cron run.'})
        self.assertTrue(self.message(message_id='outside:3', text='Other job.', job_id='other')['recorded'])

    def test_only_the_bot_of_the_adapter_profile_takes_messages(self):
        other = self.service.mutate('POST', '/v1/bots', {'name': 'Grace'}, str(uuid4()), self.device)
        for fields in (dict(chat_id=other['id']), dict(chat_id='not-a-bot'), dict(home=self.temp.name)):
            with self.subTest(fields=fields), self.assertRaises(ServiceError) as raised:
                self.service.gateway_message(dict(dict(home=self.home, chat_id=self.bot['id'], kind='send',
                                                       message_id='outside:1', text='Hello.'), **fields))
            self.assertEqual(raised.exception.status, 404)
        with self.assertRaises(ServiceError):
            self.message(kind='typing')
        self.assertEqual(self.message(text='  '), {'recorded': False, 'reason': 'The message has no text.'})
        self.service.mutate('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': True}, str(uuid4()), self.device)
        self.assertFalse(self.message()['recorded'])
        self.assertEqual(self.chat(), [])


FAKE_HERMES = '''import os, sys, time
from pathlib import Path
def main():
    Path(os.environ["HERMES_HOME"], "started").write_text(" ".join(sys.argv))
    print("fake gateway runs", flush=True)
    time.sleep(60)
'''


class GatewayProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.home, source = root / 'hermes', root / 'source'
        (source / 'hermes_cli').mkdir(parents=True)
        (source / 'hermes_cli/__init__.py').write_text('')
        (source / 'hermes_cli/main.py').write_text(FAKE_HERMES)
        self.home.mkdir()
        (self.home / 'config.yaml').write_text('{}\n')
        self.provider = ControlledProvider()
        self.provider.python, self.provider.hermes_root = sys.executable, str(source)
        self.service = Service(root / 'data', self.provider, start_workers=False, hermes_home=self.home)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_the_service_runs_one_gateway_for_the_hermes_home_and_starts_it_again(self):
        self.assertTrue(self.service.gateway_enabled())
        self.service._check_gateway()
        process = self.service.gateway_process
        self.assertEqual(self.service.gateway_state, {'state': 'running', 'pid': process.pid, 'reason': None})
        deadline = time.monotonic() + 10
        while not (self.home / 'started').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual((self.home / 'started').read_text(), 'hermes gateway run')
        log = Path(self.service.root, 'gateway/gateway.log')
        self.assertEqual(os.stat(log).st_mode & 0o777, 0o600)
        self.service._check_gateway()
        self.assertIs(self.service.gateway_process, process)
        self.assertIn('fake gateway runs', log.read_text())
        # A gateway that stops starts again after RESTART_SECONDS. Until then, the setup says why none runs.
        process.kill()
        process.wait()
        self.service._check_gateway()
        self.assertIsNone(self.service.gateway_process)
        self.assertEqual(self.service.gateway_state['state'], 'stopped')
        with patch.object(gateway_chat.time, 'monotonic', return_value=time.monotonic() + gateway_chat.RESTART_SECONDS + 1):
            self.service._check_gateway()
        self.assertIsNot(self.service.gateway_process, None)
        self.assertEqual(self.service.gateway_state['state'], 'running')
        self.service._stop_gateway()

    def test_a_gateway_that_the_person_runs_is_used_and_no_second_one_starts(self):
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        (self.home / 'gateway.pid').write_text('{"pid": %d}' % other.pid)
        with patch.object(gateway_chat, 'live_gateway_pid', return_value=other.pid):
            self.service._check_gateway()
            self.assertIsNone(self.service.gateway_process)
            self.assertEqual(self.service.gateway_state['state'], 'external')
            self.assertIn('did not load the coconut platform', self.service.gateway_state['reason'])
            self.service.gateway_bridge.poll({'home': str(self.home), 'wait': 0})
            self.service._check_gateway()
        self.assertEqual(self.service.gateway_state['reason'], gateway_chat.USING.format(home=self.home, pid=other.pid))
        self.assertFalse((self.home / 'started').exists())
        # Without the Hermes modules, the pid file of the running gateway is the evidence.
        self.assertEqual(gateway_chat.live_gateway_pid(self.home), other.pid)


class ProfileEditTests(unittest.TestCase):
    def test_setup_enables_the_plugin_and_its_platform_and_removal_restores_the_config(self):
        config = {'model': 'gpt', 'plugins': {'enabled': ['memory']}, 'gateway': {'multiplex_profiles': True}}
        added = gateway_plugin.with_platform(config, True)
        self.assertEqual(added, {'model': 'gpt', 'plugins': {'enabled': ['memory', 'coconut']},
                                 'gateway': {'multiplex_profiles': True, 'platforms': {'coconut': {'enabled': True}}}})
        self.assertIsNone(gateway_plugin.with_platform(added, True))
        self.assertEqual(gateway_plugin.with_platform(added, False), config)
        self.assertIsNone(gateway_plugin.with_platform(config, False))
        # A missing list becomes a list. A removal keeps the empty list, which Hermes reads as "nothing enabled".
        self.assertEqual(gateway_plugin.with_platform(gateway_plugin.with_platform({}, True), False), {'plugins': {'enabled': []}})
        # Coconut sets its own keys of the platform block again. The other keys of the block stay.
        own = {'plugins': {'enabled': ['coconut']}, 'gateway': {'platforms': {'coconut': {'enabled': False, 'extra': {'a': 1}}}}}
        self.assertEqual(gateway_plugin.with_platform(own, True)['gateway'],
                         {'platforms': {'coconut': {'enabled': True, 'extra': {'a': 1}}}})
        for shape in ({'plugins': 'all'}, {'plugins': {'enabled': 'coconut'}}, {'gateway': []}):
            with self.subTest(shape=shape):
                self.assertIsNone(gateway_plugin.with_platform(shape, True))

    def test_the_plugin_link_points_to_the_install_and_never_replaces_another_plugin(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertEqual(gateway_plugin.link(home, gateway_plugin.PLUGIN, True), 'added')
            self.assertEqual((home / 'plugins/coconut').resolve(), gateway_plugin.PLUGIN)
            self.assertEqual(gateway_plugin.link(home, gateway_plugin.PLUGIN, True), 'unchanged')
            self.assertEqual(gateway_plugin.link(home, gateway_plugin.PLUGIN, False), 'removed')
            self.assertFalse((home / 'plugins/coconut').exists())
            (home / 'plugins/coconut').mkdir()
            # Another plugin of this name, and its settings, stay as they are in both directions.
            self.assertEqual(gateway_plugin.link(home, gateway_plugin.PLUGIN, True), 'taken')
            self.assertEqual(gateway_plugin.link(home, gateway_plugin.PLUGIN, False), 'taken')
            (home / 'config.yaml').write_text('{}')
            self.assertEqual(gateway_plugin.update(home, False), 'taken')
            self.assertTrue((home / 'plugins/coconut').is_dir())
            # Without a config.yaml, a home stays as it is.
            self.assertEqual(gateway_plugin.update(home / 'empty', True), 'skipped')


if __name__ == '__main__':
    unittest.main()
