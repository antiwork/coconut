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

    def test_a_run_that_hermes_starts_in_a_chat_is_a_turn_whose_tools_work(self):
        # Hermes runs a bot's chat by itself when a background process of an earlier turn finishes. Before, the run had
        # no Coconut turn, and coconut_message_bot refused it ("works only during a Coconut chat turn").
        other = self.service.mutate('POST', '/v1/bots', {'name': 'Gumroad Dev'}, str(uuid4()), 'phone')
        wake = {'home': str(self.home), 'chat_id': self.bot['id'], 'message_id': 'bg-1'}
        started = self.service.gateway_hermes_run(wake)
        task = self.read('tasks', started['task_id'])
        self.assertEqual((task['status'], task['bot_id']), ('working', self.bot['id']))
        # The run's input is a notice of Hermes, not a message of the person.
        with self.service.lock:
            self.assertFalse([m for m in self.service._task_messages(task['id']) if m['role'] == 'user'])
        # A second request for the same run finds the same turn.
        self.assertEqual(self.service.gateway_hermes_run(wake)['turn_id'], started['turn_id'])
        # While the run has its turn, the chat opens no second one.
        self.assertEqual(self.service.gateway_hermes_run(dict(wake, message_id='bg-2')), {'skip': True})
        result = self.service.gateway_tool({'turn_id': started['turn_id'], 'tool': 'message_bot',
                                            'arguments': {'bot': 'Gumroad Dev', 'text': 'CI is green.'}})
        self.assertNotIn('error', result)
        bridge = self.service.gateway_bridge
        for event in ({'kind': 'send', 'message_id': 'w:1', 'text': 'CI is green, and I told Gumroad Dev.'},
                      {'kind': 'done', 'outcome': 'success'}):
            self.assertEqual(bridge.event(dict(event, turn_id=started['turn_id'])), {'ok': True})
        deadline = time.monotonic() + 5
        while self.read('tasks', started['task_id'])['status'] == 'working' and time.monotonic() < deadline:
            time.sleep(.02)
        # The turn ended. The task waits for the reply of Gumroad Dev, as after any coconut_message_bot.
        self.assertEqual(self.read('tasks', started['task_id'])['status'], 'waiting')
        self.assertIn(('CI is green, and I told Gumroad Dev.', False), self.messages(started['task_id']))
        # A home that is not a bot's profile, and another chat of this profile, get no turn.
        self.assertEqual(self.service.gateway_hermes_run(dict(wake, home=self.temp.name, message_id='bg-3')), {'skip': True})
        self.assertEqual(self.service.gateway_hermes_run(dict(wake, chat_id=other['id'], message_id='bg-4')), {'skip': True})

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

    def test_a_restart_ends_a_running_routine_and_runs_no_chat_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Service(folder, Provider(), start_workers=False)
            bot = first.mutate('POST', '/v1/bots', {'name': 'Desk'}, str(uuid4()), 'phone')
            started = first.gateway_cron({'event': 'start', 'home': str(first.profiles.home(bot)), 'job_id': 'intake',
                                          'job_name': 'Desk fast work intake', 'execution_id': 'run-1'})
            # A second routine task is in a continuation of Coconut, for example after an answered card.
            continued = first.gateway_cron({'event': 'start', 'home': str(first.profiles.home(bot)), 'job_id': 'intake',
                                            'job_name': 'Desk fast work intake', 'execution_id': 'run-2'})
            with first.transaction():
                task = first._get('tasks', continued['task_id'])
                task['_gateway_cron'] = dict(task['_gateway_cron'], continued=True)
                first._put('tasks', task)
            first.close()
            # The new service has no turn of the old one. Hermes owns the routine, so nothing runs it as a chat turn.
            service = Service(folder, Provider(), start_workers=False)
            try:
                with service.lock:
                    task = service._get('tasks', started['task_id'])
                    event = service.db.execute("SELECT 1 FROM events WHERE type='task.updated' AND task_id=? "
                                               "AND json_extract(data,'$.status')='failed'", (task['id'],)).fetchone()
                self.assertEqual(task['status'], 'failed')
                self.assertIn('runs again at its next time', task['summary'])
                self.assertIsNotNone(event)
                with service.lock:
                    self.assertEqual(service._get('tasks', continued['task_id'])['status'], 'queued')
            finally:
                service.close()

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
        # A turn of Coconut continued the routine, so a restart during it queues it again like any other turn.
        self.assertTrue(self.read('tasks', started['task_id'])['_gateway_cron']['continued'])
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
                patch.object(self.service, 'run_native_routine') as claim:
            # The coconut adapter of the bot's profile is connected: the gateway serves the profile and fires the routine.
            self.service.gateway_bridge.poll({'home': str(self.home), 'wait': 0})
            self.assertEqual(self.service.gateway_served_bots(), {self.bot['id']})
            tick()
            claim.assert_not_called()
            # The switch: the adapter is gone, for example while the gateway starts again. Coconut fires nothing: only
            # a gateway runs the agent job of a profile. The routine shows that it is paused, and why.
            from coconut.routine_coordinator import ROUTINES_UNSERVED
            with patch.object(gateway_bridge, 'ADAPTER_SECONDS', 0):
                self.assertEqual(self.service.gateway_served_bots(), set())
                tick()
            claim.assert_not_called()
            self.assertEqual(self.read('routines', routine['id'])['error'], ROUTINES_UNSERVED)
            # The adapter is back: the routine runs on its schedule in the gateway, without a catch-up by Coconut.
            self.service.gateway_bridge.poll({'home': str(self.home), 'wait': 0})
            tick()
            claim.assert_not_called()
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
                patch.object(self.service, 'run_native_routine') as claim:
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
            self.service.run_native_routine(stored)
        # Also a routine that Hermes just returned without the mark, for example after an edit: the run checks the
        # gateway itself.
        fresh = dict(stored, error=None)
        with patch.object(self.provider, 'native_settings', create=True) as native:
            with self.assertRaisesRegex(ServiceError, 'is parked'), self.service.transaction():
                self.service.run_native_routine(fresh)
            native.assert_not_called()
        # The settings that the phone reads show the pause, also when the stored routine has no mark yet.
        with self.service.transaction():
            self.service._put('routines', fresh)
        shown = next(r for r in self.service.native_snapshot()['routines'] if r['id'] == routine['id'])
        self.assertIn('is parked', shown['error'])
        # A read of Hermes marks the pause and publishes it, once.
        def read(bots):
            return {'routines': [dict(fresh)]}
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

    def test_without_a_gateway_nothing_runs_a_routine(self):
        # Coconut no longer claims a routine in Hermes, so a Mac without a gateway (fixture mode) runs none: neither
        # on its schedule nor when the person starts it.
        from coconut.routine_coordinator import ROUTINES_NEED_GATEWAY
        from coconut.service import ServiceError
        due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        routine = dict(id=str(uuid5(UUID(self.bot['id']), 'cron:intake')), bot_id=self.bot['id'], name='Intake',
                       enabled=True, error=None, next_run_at=due, _native_id='intake')
        with self.service.transaction():
            self.service._put('routines', routine)
        self.service.gateway_enabled = lambda: False
        with patch.object(self.provider, 'native_settings', create=True) as native, \
                patch.object(self.service, 'refresh_native_routines'):
            self.service._native_cron_next = 0
            self.service.tick_native_routines()
            with self.assertRaisesRegex(ServiceError, ROUTINES_NEED_GATEWAY):
                self.service.mutate('POST', f'/v1/routines/{routine["id"]}/run', {}, str(uuid4()), 'phone')
            native.assert_not_called()
        with self.service.lock:
            self.assertEqual([t for t in self.service._all('tasks') if t.get('routine_id') == routine['id']], [])

    def test_a_run_now_of_a_script_check_that_finds_no_work_ends_without_a_turn(self):
        # Hermes runs the script of a gated routine first. When its last line is {"wakeAgent": false}, no agent runs,
        # so no gateway cron run takes the waiting task. The task ends with that run instead of a false timeout.
        from coconut.routine_coordinator import TRIGGER_QUIET, TRIGGER_SCRIPT_FAILED
        routine = self.served_routine()
        cases = (({'last_status': 'ok'}, 'completed', TRIGGER_QUIET),
                 ({'last_status': 'error', 'last_error': 'The gate could not read the inbox.'}, 'failed',
                  TRIGGER_SCRIPT_FAILED.format(error='The gate could not read the inbox.')))
        for result, status, summary in cases:
            with self.subTest(status=status):
                with self.service.transaction():
                    self.service._put('routines', dict(self.read('routines', routine['id']), _job=dict(id='intake')))
                with patch.object(self.provider, 'native_settings', create=True, return_value=dict(routine)):
                    queued = self.service.mutate('POST', f'/v1/routines/{routine["id"]}/run', {}, str(uuid4()), 'phone')
                tick = lambda: (setattr(self.service, '_native_cron_next', 0), self.service.tick_native_routines())
                with patch.object(self.service, 'refresh_native_routines'):
                    tick()
                    self.assertEqual(self.read('tasks', queued['id'])['status'], 'waiting')
                    # Hermes ran the job after the trigger, without an agent turn.
                    ran = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
                    with self.service.transaction():
                        self.service._put('routines', dict(self.read('routines', routine['id']), _job=dict(
                            id='intake', script='intake_gate.py', last_run_at=ran, **result)))
                    tick()
                task = self.read('tasks', queued['id'])
                self.assertEqual((task['status'], task['summary']), (status, summary))
                self.assertEqual(self.provider.starts, [])
        # A run that the gateway's cron took, and that now waits for another bot, stays waiting: neither the run's end
        # nor the time limit of a trigger ends it. A later run does not take it as its waiting task.
        with patch.object(self.provider, 'native_settings', create=True, return_value=dict(routine)):
            queued = self.service.mutate('POST', f'/v1/routines/{routine["id"]}/run', {}, str(uuid4()), 'phone')
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with self.service.transaction():
            task = self.service._get('tasks', queued['id'])
            task.update(status='waiting', _gateway_cron=dict(job_id='intake', execution_id='x', turn_id='t'),
                        _gateway_trigger=dict(task['_gateway_trigger'], at=old))
            self.service._put('tasks', task)
        with patch.object(self.service, 'refresh_native_routines'):
            self.service._native_cron_next = 0
            self.service.tick_native_routines()
        self.assertEqual(self.read('tasks', queued['id'])['status'], 'waiting')
        with self.service.lock:
            self.assertNotIn(queued['id'], [t['id'] for t in self.service._waiting_triggers()])

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
