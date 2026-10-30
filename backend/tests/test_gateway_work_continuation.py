"""coconut_continue_work in a chat turn of the Hermes gateway.

The real `coconut` plugin (hermes_plugin/coconut) registers the tool with the worker's schema, and its handler calls the
loopback route of the service with the turn of its chat. The service continues the work as for a worker's call
(Service.emit 'work_continue', native_outcomes.continue_native_work), with each of its checks. The work is the saved
shape of test_delegated_report_continuation.GroupedOpenDelegatedPrTests: Dev's queued owner on its ready card, with two
open PRs of Desk's one Work.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from coconut import gateway_chat
from coconut.native_outcomes import CONTINUE_SCHEMA
from coconut.server import APIServer
import test_delegated_report_continuation as fixtures
from test_gateway_adapter import load_plugin

MESSAGE = 'Both PRs passed review. Finish the acceptance check of each.'


class GatewayWorkContinuationTests(unittest.TestCase):
    setUp_fixture = fixtures.GroupedOpenDelegatedPrTests.setUp
    tearDown_fixture = fixtures.GroupedOpenDelegatedPrTests.tearDown
    write = fixtures.GroupedOpenDelegatedPrTests.write
    prepare = fixtures.GroupedOpenDelegatedPrTests.prepare
    snapshot_result = fixtures.GroupedOpenDelegatedPrTests.snapshot_result
    scheduled_native = fixtures.GroupedOpenDelegatedPrTests.scheduled_native
    ready_native = fixtures.GroupedOpenDelegatedPrTests.ready_native
    grouped = fixtures.GroupedOpenDelegatedPrTests.grouped
    assert_grouped = fixtures.GroupedOpenDelegatedPrTests.assert_grouped
    SECOND_URL = fixtures.GroupedOpenDelegatedPrTests.SECOND_URL

    @classmethod
    def setUpClass(cls):
        cls.stubs = tempfile.TemporaryDirectory()
        cls.plugin, cls.adapter_module = load_plugin(Path(cls.stubs.name))
        cls.hooks = sys.modules['hermes_plugins.coconut.hooks']

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.stubs.name)
        cls.stubs.cleanup()

    def setUp(self):
        self.setUp_fixture()
        self.server = APIServer(('127.0.0.1', 0), self.service)
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
        self.service.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_address[1]}')
        # As on the person's Hermes: a message of the person is a chat turn, and the adapter of the test connects
        # through the bridge.
        self.provider.supports_native_outcomes = True
        self.service.gateway_enabled = lambda: True
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.adapters, self.calls = {}, {}
        # The real handlers of the registered tools, as Hermes calls them.
        self.registered = {}
        registered = self.registered

        class Context:
            def register_platform(self, **options):
                pass

            def register_tool(self, **options):
                registered[options['name']] = options

            def register_hook(self, name, callback):
                pass

            def register_system_prompt_section(self, *args, **options):
                pass
        self.plugin.register(Context())

    def tearDown(self):
        for adapter in self.adapters.values():
            asyncio.run_coroutine_threadsafe(adapter.disconnect(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.server.shutdown()
        self.server.server_close()
        self.tearDown_fixture()

    def call(self, name='coconut_continue_work'):
        return lambda arguments: json.loads(self.registered[name]['handler'](arguments))

    def connect(self, bot, calls):
        """The real adapter of the bot's profile. `calls()` runs in its next turn with the session context of the chat,
        as a tool call of Hermes does."""
        from gateway.config import PlatformConfig
        from gateway.session_context import SESSION
        self.calls[bot['id']] = calls
        if bot['id'] in self.adapters:
            return

        async def runner(event):
            def in_turn():
                token = SESSION.set({'HERMES_SESSION_PLATFORM': 'coconut',
                                     'HERMES_SESSION_CHAT_ID': event.source.chat_id})
                try:
                    self.calls[bot['id']]()
                finally:
                    SESSION.reset(token)
            await asyncio.to_thread(in_turn)
            return 'Done.'
        adapter = self.adapter_module.CoconutAdapter(
            PlatformConfig({'bridge': str(self.service.gateway_bridge.address_file)}))
        adapter.set_message_handler(runner)
        self.adapters[bot['id']] = adapter
        with patch.dict(os.environ, HERMES_HOME=str(self.service.profiles.home(bot))):
            self.assertTrue(asyncio.run_coroutine_threadsafe(adapter.connect(), self.loop).result(10))

    def chat_turn(self, bot, calls, status='completed'):
        """Run one chat turn of `bot` from a message of the person through the service and the real adapter. Return
        the task of the turn."""
        self.connect(bot, calls)
        task = self.write(f'/v1/bots/{bot["id"]}/messages', {'text': 'Continue the delegated work.'})
        self.service._run_task(task['id'])
        task = self.service._get('tasks', task['id'])
        self.assertEqual(task['status'], status, task.get('summary'))
        return task

    def state(self):
        return ({t['id']: t for t in self.service._all('tasks')}, {a['id']: a for a in self.service._all('artifacts')},
                {d['id']: d for d in self.service._all('deliveries')}, copy.deepcopy(self.service.kanban.tasks))

    def test_the_registered_tool_is_the_workers_tool(self):
        tool = self.registered['coconut_continue_work']
        self.assertEqual((tool['toolset'], tool['schema']), ('coconut', CONTINUE_SCHEMA))
        # The plugin loads the schema by its path and leaves the module name of the worker free.
        self.assertNotIn('technical_lane', sys.modules)

    def test_the_requester_continues_one_execution_with_both_open_prs_from_its_chat(self):
        owner, requester, work, prs = self.grouped()
        before = {a['id']: a for a in self.service._all('artifacts')}
        cards = copy.deepcopy(self.service.kanban.tasks)
        arguments = {'task_id': 't_existing', 'message': MESSAGE}
        results = []
        with patch.object(self.service, 'emit', wraps=self.service.emit) as emit:
            caller = self.chat_turn(self.desk, lambda: results.extend([self.call()(arguments), self.call()(arguments)]))
        # The service received exactly the call of the model, for the task of the chat turn, as a worker's receipt.
        continues = [c.args for c in emit.call_args_list if c.args[1] == 'work_continue']
        self.assertEqual(continues, [(caller['id'], 'work_continue', arguments)] * 2)
        self.assertEqual(caller['bot_id'], self.desk['id'])
        receipt = results[0]
        continued = self.assert_grouped(owner, prs, receipt, before, cards)
        # A repeated call gets the saved receipt and continues nothing again.
        self.assertEqual(results[1], receipt)
        self.assertEqual(len([d for d in self.service._all('deliveries') if d.get('receipt') == receipt]), 1)
        self.assertEqual(continued['_bot_messages'][-1]['sender_bot_id'], self.desk['id'])
        self.assertEqual([m['text'] for m in continued['_bot_messages']].count(MESSAGE), 1)
        self.assertEqual(self.service._get('artifacts', work['id']), work)

    def test_the_owner_continues_one_execution_with_both_open_prs_from_its_chat(self):
        owner, requester, work, prs = self.grouped()
        before = {a['id']: a for a in self.service._all('artifacts')}
        cards = copy.deepcopy(self.service.kanban.tasks)
        results = []
        self.chat_turn(self.dev, lambda: results.append(self.call()({'task_id': 't_existing', 'message': MESSAGE})))
        self.assert_grouped(owner, prs, results[0], before, cards)

    def assert_refused(self, bot, arguments, message, change=None):
        """The chat turn's call reaches the continuation of the service, which refuses it, and nothing changes."""
        results = []
        self.grouped()
        if change:
            change()
        before = self.state()
        with patch.object(self.service, 'continue_native_work', wraps=self.service.continue_native_work) as service:
            caller = self.chat_turn(bot, lambda: results.append(self.call()(arguments)))
        self.assertEqual(results, [{'error': message}])
        tasks, artifacts, deliveries, cards = self.state()
        self.assertEqual(({k: v for k, v in tasks.items() if k != caller['id']}, artifacts, deliveries, cards), before)
        return service, caller

    def refused(self, message, change=None):
        service, caller = self.assert_refused(self.desk, {'task_id': 't_existing', 'message': MESSAGE}, message, change)
        service.assert_called_once_with(caller['id'], {'task_id': 't_existing', 'message': MESSAGE})

    def update_owner(self, **changes):
        def change():
            owner = next(t for t in self.service._all('tasks') if t.get('_kanban_claim', {}).get('native_id') == 't_existing')
            self.service._put('tasks', dict(owner, **changes))
            return owner
        return change

    def test_a_foreign_bot_cannot_continue_the_work_from_its_chat(self):
        care = self.write('/v1/bots', {'name': 'Care'})
        self.assert_refused(care, {'task_id': 't_existing', 'message': MESSAGE},
                            'Only the owner or its requesting bot can continue this work.')

    def test_a_call_that_names_no_existing_outcome_continues_no_other_work(self):
        # The call names its native task. The service never picks another work for it.
        self.assert_refused(self.desk, {'task_id': 't_unknown', 'message': MESSAGE},
                            'Choose one existing native outcome from kanban_show.')

    def test_a_wait_of_the_work_still_refuses(self):
        self.refused('Wait for this task and its existing dependencies.', self.update_owner(_waiting_on=['dependency']))

    def test_a_pending_decision_of_the_work_still_refuses(self):
        def change():
            owner = self.update_owner()()
            self.service._put('decisions', dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'], status='pending'))
        self.refused('The existing decision must be answered before work continues.', change)

    def test_unread_queued_input_of_the_work_still_refuses(self):
        def change():
            # The queued owner on its scheduled card still has to read an answer.
            self.update_owner(_resume_decision='answered')()
            next(iter(self.service.kanban.tasks.values())).update(status='scheduled', dispatch_blocker=None)
        self.refused('Wait for this task to start its queued decision or review before continuing this work.', change)

    def test_an_undelivered_result_of_the_work_still_refuses(self):
        self.refused('Wait for the previous result to be delivered before continuing this work.',
                     self.update_owner(_return_pending=True))

    def test_the_technical_lane_stays_with_the_worker(self):
        lane = {'decision_id': 'pending', 'decision_revision': '1', 'scope': ['local_checks']}
        service, _ = self.assert_refused(self.desk, {'task_id': 't_existing', 'message': MESSAGE,
                                                     'independent_technical_work': lane},
                                         gateway_chat.GATEWAY_PLAIN_CONTINUATION)
        service.assert_not_called()

    def test_an_ended_turn_and_a_follow_up_on_a_card_cannot_continue_the_work(self):
        from coconut.service import ServiceError
        owner, requester, work, prs = self.grouped()
        before = self.state()
        arguments = {'task_id': 't_existing', 'message': MESSAGE}
        turns, results = [], []
        card = {'title': 'Merge both PRs?', 'context': 'Both PRs passed review.',
                'options': [{'id': 'merge', 'label': 'Merge', 'explanation': ''},
                            {'id': 'wait', 'label': 'Wait', 'explanation': ''}]}

        def ask():
            turns.append(self.adapter_module.turn_for_chat(self.desk['id'])[1])
            results.append(self.call('coconut_ask')(card))
        caller = self.chat_turn(self.desk, ask, status='needs_you')
        self.assertEqual(results.pop()['status'], 'waiting')
        # The turn ended: the plugin has no turn for the chat, and the service no task for the old turn.
        self.assertIn('works only during a Coconut chat turn', self.call()(arguments)['error'])
        with self.assertRaisesRegex(ServiceError, 'no longer runs'):
            self.service.gateway_tool({'turn_id': turns[0], 'tool': 'continue_work', 'arguments': arguments})
        # A follow-up on the pending card answers the question. The hook blocks the tool, and the service refuses it.
        decision = next(d for d in self.service._all('decisions') if d['task_id'] == caller['id'])
        self.write(f'/v1/decisions/{decision["id"]}/context', {'question': 'What changed?'})
        context = next(c for c in self.service._all('contexts') if c['decision_id'] == decision['id'])

        def follow_up():
            results.append(self.hooks.guard_tool(tool_name='coconut_continue_work'))
            results.append(self.call()(arguments))
        self.connect(self.desk, follow_up)
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(10)
        self.assertEqual(results, [{'action': 'block', 'message': self.hooks.CONTEXT_MESSAGE},
                                   {'error': 'Answer the current question. Its pending decision stays open.'}])
        self.assertEqual((self.service._get('contexts', context['id'])['status'],
                          self.service._get('decisions', decision['id'])['status']), ('completed', 'pending'))
        tasks, artifacts, deliveries, cards = self.state()
        self.assertEqual((tasks[owner['id']], artifacts, deliveries, cards), (before[0][owner['id']], *before[1:]))

    def test_a_lost_response_is_not_sent_again_and_the_same_call_gets_the_saved_receipt(self):
        owner, requester, work, prs = self.grouped()
        before = {a['id']: a for a in self.service._all('artifacts')}
        cards = copy.deepcopy(self.service.kanban.tasks)
        requests, results = [], []
        original = self.adapter_module.Bridge.request

        def lose(bridge, route, payload, timeout):
            # The service commits the call, and its answer never reaches the plugin.
            if payload.get('tool') != 'continue_work':
                return original(bridge, route, payload, timeout)
            requests.append(payload)
            answer = original(bridge, route, payload, timeout)
            if len(requests) == 1:
                results.append(('lost', answer))
                raise TimeoutError('timed out')
            return answer
        arguments = {'task_id': 't_existing', 'message': MESSAGE}
        with patch.object(self.adapter_module.Bridge, 'request', lose):
            def calls():
                results.append(self.call()(arguments))
                # The model repeats the exact call after the error.
                results.append(self.call()(arguments))
            self.chat_turn(self.desk, calls)
        lost, error, repeated = results
        self.assertEqual(error, {'error': 'The Coconut service did not answer (TimeoutError).'})
        # One request for each call of the model: the plugin sends no call again by itself.
        self.assertEqual([r['arguments'] for r in requests], [arguments] * 2)
        self.assertEqual(repeated, lost[1])
        continued = self.assert_grouped(owner, prs, repeated, before, cards)
        self.assertEqual([m['text'] for m in continued['_bot_messages']].count(MESSAGE), 1)
        self.assertEqual(len([d for d in self.service._all('deliveries') if d.get('receipt') == repeated]), 1)

    def test_the_route_needs_the_token_of_the_bridge(self):
        self.grouped()
        before = self.state()
        address = json.loads(self.service.gateway_bridge.address_file.read_text())
        request = urllib.request.Request(address['url'] + '/v1/gateway/tools', method='POST', data=json.dumps({
            'turn_id': 'any', 'tool': 'continue_work', 'arguments': {'task_id': 't_existing', 'message': MESSAGE}}).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer wrong'})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=10)
        raised.exception.close()
        self.assertEqual(raised.exception.code, 401)
        self.assertEqual(self.state(), before)


if __name__ == '__main__':
    unittest.main()
