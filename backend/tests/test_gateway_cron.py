"""Routines in the gateway: a cron run of the gateway is a Coconut turn. The routines of a profile that no gateway
serves are paused, with the reason.
test_gateway_adapter.py checks the plugin side, test_native_gateway_chat.py a real gateway."""
from datetime import datetime, timedelta, timezone
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from coconut import gateway_bridge
from coconut.service import Service
from test_service import ControlledProvider


class Provider(ControlledProvider):
    supports_native_outcomes = True


class GatewayCronTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = Provider()
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'phone')
        self.home = self.service.profiles.home(self.bot)

    def read(self, table, identity):
        with self.service.lock:
            return self.service._get(table, identity)

    def messages(self, task_id):
        with self.service.lock:
            return [(m['text'], bool(m.get('_interim'))) for m in self.service._task_messages(task_id)
                    if m['role'] == 'assistant' and m['kind'] == 'text']

    def start(self, execution='run-1'):
        return self.service.gateway_cron({'event': 'start', 'home': str(self.home), 'job_id': 'intake',
                                          'job_name': 'Desk fast work intake', 'execution_id': execution,
                                          'session_id': 'cron-session-1'})

    def test_a_cron_run_of_the_gateway_is_a_routine_turn_in_the_bot_chat(self):
        started = self.start()
        task = self.read('tasks', started['task_id'])
        self.assertEqual((task['status'], task['title'], started['chat_id']), ('working', 'Desk fast work intake', self.bot['id']))
        self.assertEqual(task['routine_id'], str(uuid5(UUID(self.bot['id']), 'cron:intake')))
        # A second start of the same run, for example after a retry, finds the same turn.
        self.assertEqual(self.start()['turn_id'], started['turn_id'])
        bridge = self.service.gateway_bridge
        for event in ({'kind': 'delta', 'stream': 1, 'text': 'Checking the queue.'},
                      {'kind': 'send', 'message_id': 'c:1', 'text': 'Checking the queue.', 'interim': True},
                      {'kind': 'send', 'message_id': 'c:2', 'text': 'Two new items.'},
                      {'kind': 'done', 'outcome': 'success'}):
            self.assertEqual(bridge.event(dict(event, turn_id=started['turn_id'])), {'ok': True})
        deadline = time.monotonic() + 5
        while self.read('tasks', started['task_id'])['status'] == 'working' and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.read('tasks', started['task_id'])['status'], 'completed')
        self.assertEqual(self.messages(started['task_id']), [('Checking the queue.', True), ('Two new items.', False)])
        # A run that ended opens no new turn, and a home that is not a bot's profile has no turn.
        self.assertEqual(self.start(), {'skip': True})
        self.assertEqual(self.service.gateway_cron({'event': 'start', 'home': self.temp.name, 'job_id': 'x',
                                                    'execution_id': 'other'}), {'skip': True})

    def test_a_cron_start_finds_its_run_through_an_index(self):
        started = self.start()
        original = type(self.service)._all

        def guarded(service, table):
            if table == 'tasks':
                raise AssertionError('A cron start must not parse every task.')
            return original(service, table)

        with patch.object(type(self.service), '_all', guarded):
            self.assertEqual(self.start()['turn_id'], started['turn_id'])
        plan = ' '.join(row[-1] for row in self.service.db.execute(
            "EXPLAIN QUERY PLAN SELECT rowid, data FROM tasks INDEXED BY tasks_gateway_cron "
            "WHERE json_extract(data,'$._gateway_cron.execution_id')=?", ('run-1',)))
        self.assertIn('SEARCH tasks USING INDEX tasks_gateway_cron', plan)

    def test_an_update_waits_for_a_cron_run_and_its_cancel_stops_no_chat_turn(self):
        started = self.start()
        with self.service.lock:
            self.assertTrue(self.service._service_busy())
        bridge = self.service.gateway_bridge
        self.service.gateway_bridge.cancel(started['task_id'])
        # The adapter sends a stop to a chat. The cron run is not a turn of the bot's chat, so the poll names no chat.
        self.assertEqual(bridge.poll({'home': str(self.home), 'wait': 0})['stop'], [])
        self.service.gateway_cron_threads[started['task_id']].join(5)
        with self.service.lock:
            self.assertFalse(self.service._service_busy())

    def test_a_cron_turn_without_its_thread_leaves_no_working_task(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                self.start()
        with self.service.lock:
            self.assertFalse([t for t in self.service._all('tasks') if t.get('_gateway_cron')])
            self.assertFalse(self.service._service_busy())

    def test_a_card_of_a_cron_run_ends_the_run_and_the_routine_task_waits_for_the_answer(self):
        started = self.start()
        bridge = self.service.gateway_bridge
        card = {'title': 'File these items?', 'context': 'Two items wait in the queue.',
                'options': [{'id': 'file', 'label': 'File them', 'explanation': ''}]}
        result = self.service.gateway_tool({'turn_id': started['turn_id'], 'tool': 'ask', 'arguments': card})
        self.assertEqual((result['status'], result['paused']), ('waiting', True))
        self.assertEqual(self.read('tasks', started['task_id'])['status'], 'needs_you')
        # The end of the cron run does not complete the waiting task.
        bridge.event({'turn_id': started['turn_id'], 'kind': 'done', 'outcome': 'success'})
        deadline = time.monotonic() + 5
        while bridge.task_of(started['turn_id']) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.read('tasks', started['task_id'])['status'], 'needs_you')
        with self.service.lock:
            decision = next(d for d in self.service._all('decisions') if d['task_id'] == started['task_id'])
        self.assertEqual((decision['title'], decision['status']), ('File these items?', 'pending'))
        # The answer continues the Hermes session of the cron run, with the routine's prompt and tool results.
        self.service.mutate('POST', f'/v1/decisions/{decision["id"]}/answer', {'option_id': 'file', 'revision': 1},
                            str(uuid4()), 'phone')
        self.service.gateway_enabled = lambda: True
        from test_gateway_chat import Adapter
        adapter = Adapter(bridge, self.home, [{'kind': 'send', 'message_id': 'm:1', 'text': 'Filed both items.'},
                                              {'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.service._run_task(started['task_id'])
        adapter.join(5)
        turn = adapter.turns[0]
        self.assertEqual(turn['session_root'], 'cron-session-1')
        self.assertTrue(turn['text'].startswith('Continue the original task using this recorded answer.'))
        self.assertEqual(self.read('tasks', started['task_id'])['status'], 'completed')
        self.assertEqual(self.messages(started['task_id'])[-1], ('Filed both items.', False))
        self.assertEqual(self.provider.resumes, [])

    def test_the_gateway_fires_the_routines_of_a_served_profile_and_pauses_the_others(self):
        due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        routine = dict(id=str(uuid5(UUID(self.bot['id']), 'cron:intake')), bot_id=self.bot['id'], name='Intake',
                       enabled=True, error=None, next_run_at=due)
        with self.service.transaction():
            self.service._put('routines', routine)
        self.service.gateway_enabled = lambda: True

        def tick():
            self.service._native_cron_next = 0
            self.service.tick_native_routines()

        with patch.object(self.service, 'refresh_native_routines'), \
                patch.object(self.service, 'claim_native_routine') as claim:
            # The coconut adapter of the bot's profile is connected: the gateway serves the profile and fires the routine.
            self.service.gateway_bridge.poll({'home': str(self.home), 'wait': 0})
            self.assertEqual(self.service.gateway_served_bots(), {self.bot['id']})
            tick()
            claim.assert_not_called()
            # A claim that Coconut made before it stopped, for a run that the person started, still comes back.
            with self.service.transaction():
                self.service._put('routines', dict(routine, _recover_claim=True))
            tick()
            self.assertEqual(claim.call_count, 1)
            with self.service.transaction():
                self.service._put('routines', routine)
            # The switch: the adapter is gone, for example while the gateway starts again. Coconut fires nothing: only
            # a gateway runs the agent job of a profile. The routine shows that it is paused, and why.
            from coconut.routine_coordinator import ROUTINES_UNSERVED
            with patch.object(gateway_bridge, 'ADAPTER_SECONDS', 0):
                self.assertEqual(self.service.gateway_served_bots(), set())
                tick()
            self.assertEqual(claim.call_count, 1)
            self.assertEqual(self.read('routines', routine['id'])['error'], ROUTINES_UNSERVED)
            # The adapter is back: the routine runs on its schedule in the gateway, without a catch-up by Coconut.
            self.service.gateway_bridge.poll({'home': str(self.home), 'wait': 0})
            tick()
            self.assertEqual(claim.call_count, 1)
            self.assertIsNone(self.read('routines', routine['id'])['error'])

    def test_a_parked_profile_pauses_its_routines_and_its_chat_says_why(self):
        due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        routine = dict(id=str(uuid5(UUID(self.bot['id']), 'cron:intake')), bot_id=self.bot['id'], name='Intake',
                       enabled=True, error=None, next_run_at=due)
        with self.service.transaction():
            self.service._put('routines', routine)
        self.service.gateway_enabled = lambda: True
        # The person parked the profile in Hermes, so the gateway serves no adapter for it.
        (self.home / 'gateway.parked').write_text('')
        name = self.service.profiles.row(self.bot)['name']
        self.assertEqual(self.service.gateway_status()['unserved_profiles'], [name])
        with patch.object(self.service, 'refresh_native_routines'), \
                patch.object(self.service, 'claim_native_routine') as claim:
            self.service._native_cron_next = 0
            self.service.tick_native_routines()
        # The routine does not run, and it says why, as the chat does.
        claim.assert_not_called()
        stored = self.read('routines', routine['id'])
        self.assertIn('is parked (' + str(self.home / 'gateway.parked') + ')', stored['error'])
        self.assertIn('run them again on their schedule', stored['error'])
        # A run that the person starts gets the reason too.
        from coconut.service import ServiceError
        with self.assertRaisesRegex(ServiceError, 'is parked'), self.service.transaction():
            self.service.claim_native_routine(stored, manual=True)
        # Also a routine that Hermes just returned without the mark, for example after an edit: the claim checks the
        # gateway itself, and a scheduled claim takes nothing.
        fresh = dict(stored, error=None)
        with patch.object(self.provider, 'native_settings', create=True) as native:
            with self.assertRaisesRegex(ServiceError, 'is parked'), self.service.transaction():
                self.service.claim_native_routine(fresh, manual=True)
            with self.service.transaction():
                self.assertIsNone(self.service.claim_native_routine(fresh))
            native.assert_not_called()
        # The settings that the phone reads show the pause, also when the stored routine has no mark yet.
        with self.service.transaction():
            self.service._put('routines', fresh)
        shown = next(r for r in self.service.native_snapshot()['routines'] if r['id'] == routine['id'])
        self.assertIn('is parked', shown['error'])
        # A read of Hermes marks the pause and publishes it, once.
        def read(bots):
            return {'routines': [dict(fresh)], 'completions': {}}
        with patch.object(self.provider, 'native_settings', create=True), \
                patch.object(self.provider, 'native_routines', create=True, side_effect=read):
            cursor = self.service._cursor()
            self.service.refresh_native_routines()
            self.service.refresh_native_routines()
        events = [e for e in self.service.events(cursor)['events'] if e['type'] == 'routine.updated']
        self.assertEqual([e['data']['id'] for e in events], [routine['id']])
        self.assertIn('is parked', self.read('routines', routine['id'])['error'])
        # A chat turn fails at once with the reason, without the wait.
        task = self.service.mutate('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Hello?'}, str(uuid4()), 'phone')
        self.service._run_task(task['id'])
        stored = self.read('tasks', task['id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertIn('is parked', stored['summary'])
        self.assertEqual(self.provider.starts, [])

    def served_routine(self):
        """A routine of the bot, whose profile the gateway serves."""
        routine = dict(id=str(uuid5(UUID(self.bot['id']), 'cron:intake')), bot_id=self.bot['id'],
                       name='Desk fast work intake', enabled=True, error=None, next_run_at=None, _native_id='intake')
        with self.service.transaction():
            self.service._put('routines', routine)
        self.service.gateway_enabled = lambda: True
        self.service.gateway_bridge.poll({'home': str(self.home), 'wait': 0})
        return routine

    def test_a_run_that_the_person_starts_is_a_trigger_that_the_gateway_cron_runs(self):
        from coconut.routine_coordinator import TRIGGER_WAITING
        routine = self.served_routine()
        later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with patch.object(self.provider, 'native_settings', create=True,
                          return_value=dict(routine, next_run_at=later)) as native:
            with self.service.transaction():
                # A scheduled claim takes nothing: the gateway fires the routine itself.
                self.assertIsNone(self.service.claim_native_routine(dict(routine)))
            native.assert_not_called()
            # Run now asks Hermes to run the job at the next tick of the gateway, and Coconut runs no worker.
            queued = self.service.mutate('POST', f'/v1/routines/{routine["id"]}/run', {}, str(uuid4()), 'phone')
            # A second run now, before the gateway runs the job, finds the same task.
            again = self.service.mutate('POST', f'/v1/routines/{routine["id"]}/run', {}, str(uuid4()), 'phone')
        self.assertEqual(again['id'], queued['id'])
        self.assertEqual(native.call_count, 1)
        self.assertEqual(native.call_args.args[0], 'routine_trigger')
        self.assertEqual(native.call_args.kwargs['native_id'], 'intake')
        task = self.read('tasks', queued['id'])
        self.assertEqual((task['status'], task['summary'], task['routine_id']), ('waiting', TRIGGER_WAITING, routine['id']))
        self.assertEqual(self.read('routines', routine['id'])['next_run_at'], later)
        self.assertEqual(self.provider.starts, [])
        # The gateway's cron run takes that task, so the phone keeps the task that it got.
        started = self.start()
        self.assertEqual(started['task_id'], queued['id'])
        self.assertEqual(self.read('tasks', queued['id'])['status'], 'working')
        with self.service.lock:
            self.assertEqual([t['id'] for t in self.service._all('tasks') if t.get('routine_id') == routine['id']],
                             [queued['id']])

    def test_a_trigger_that_no_gateway_cron_run_takes_fails_with_the_reason(self):
        from coconut.routine_coordinator import TRIGGER_EXPIRED, TRIGGER_SECONDS
        routine = self.served_routine()
        with patch.object(self.provider, 'native_settings', create=True, return_value=dict(routine)):
            queued = self.service.mutate('POST', f'/v1/routines/{routine["id"]}/run', {}, str(uuid4()), 'phone')

        def tick():
            self.service._native_cron_next = 0
            with patch.object(self.service, 'refresh_native_routines'):
                self.service.tick_native_routines()

        tick()
        self.assertEqual(self.read('tasks', queued['id'])['status'], 'waiting')
        with self.service.transaction():
            task = self.service._get('tasks', queued['id'])
            old = datetime.now(timezone.utc) - timedelta(seconds=TRIGGER_SECONDS + 1)
            task['_gateway_trigger']['at'] = old.isoformat()
            self.service._put('tasks', task)
        tick()
        task = self.read('tasks', queued['id'])
        self.assertEqual((task['status'], task['summary']), ('failed', TRIGGER_EXPIRED))


if __name__ == '__main__':
    unittest.main()
