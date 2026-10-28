"""Routines in the gateway: a cron run of the gateway is a Coconut turn, and Coconut claims no routine that a gateway
ticks. test_gateway_adapter.py checks the plugin side, test_native_gateway_chat.py a real gateway."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from coconut.service import Service, ServiceError
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
                                          'job_name': 'Desk fast work intake', 'execution_id': execution})

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

    def test_a_question_of_a_cron_run_is_a_card_and_its_tool_waits_for_the_answer(self):
        started = self.start()
        bridge = self.service.gateway_bridge
        bridge.event({'turn_id': started['turn_id'], 'kind': 'question', 'question_id': 'q1', 'question_kind': 'ask',
                      'text': 'File these items?', 'choices': [{'id': 'choice-0', 'label': 'Yes'}]})
        deadline = time.monotonic() + 5
        card = None
        while card is None and time.monotonic() < deadline:
            with self.service.lock:
                card = next((d for d in self.service._all('decisions') if d['task_id'] == started['task_id']), None)
            time.sleep(.02)
        self.assertEqual(bridge.wait_answer({'turn_id': started['turn_id'], 'question_id': 'q1', 'wait': 0}), {})
        waiting = {}
        thread = threading.Thread(target=lambda: waiting.update(bridge.wait_answer(
            {'turn_id': started['turn_id'], 'question_id': 'q1', 'wait': 5})))
        thread.start()
        self.service.mutate('POST', f'/v1/decisions/{card["id"]}/answer', {'option_id': 'choice-0', 'revision': 1},
                            str(uuid4()), 'phone')
        thread.join(5)
        self.assertEqual(waiting['answer'], {'question_id': 'q1', 'text': 'Yes', 'choice': 'choice-0'})
        bridge.event({'turn_id': started['turn_id'], 'kind': 'done', 'outcome': 'success'})
        deadline = time.monotonic() + 5
        while bridge.task_of(started['turn_id']) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(bridge.wait_answer({'turn_id': started['turn_id'], 'question_id': 'q1'}), {'ended': True})

    def test_coconut_claims_no_routine_that_a_gateway_ticks(self):
        routine = dict(id=str(uuid5(UUID(self.bot['id']), 'cron:intake')), bot_id=self.bot['id'], name='Intake',
                       enabled=True, error=None, next_run_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
        with self.service.transaction():
            self.service._put('routines', routine)
        with patch.object(self.service, 'refresh_native_routines'), \
                patch.object(self.service, 'claim_native_routine') as claim:
            with patch.object(self.service, 'gateway_ticked_bots', return_value={self.bot['id']}):
                self.service._native_cron_next = 0
                self.service.tick_native_routines()
            claim.assert_not_called()
            # Without a gateway that ticks the profile, Coconut fires the routine, as before.
            with patch.object(self.service, 'gateway_ticked_bots', return_value=set()):
                self.service._native_cron_next = 0
                self.service.tick_native_routines()
            self.assertEqual(claim.call_args.args[0]['id'], routine['id'])


class TickedProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.home = root / 'hermes'
        for name in ('desk', 'care'):
            (self.home / 'profiles' / name).mkdir(parents=True)
            (self.home / 'profiles' / name / 'config.yaml').write_text('{}\n')
        (self.home / 'config.yaml').write_text('{}\n')
        self.service = Service(root / 'data', ControlledProvider(), start_workers=False, hermes_home=self.home)
        self.addCleanup(self.service.close)
        self.bots = {row['name']: row['bot_id'] for row in self.service.profiles.rows.values()}

    def gateway(self, home):
        home.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        (home / 'gateway.pid').write_text(json.dumps({'pid': process.pid}))
        return process

    def test_a_host_gateway_ticks_every_profile_but_a_parked_one_and_a_profile_gateway_ticks_its_own(self):
        from coconut import gateway_chat
        with patch.object(gateway_chat, 'live_gateway_pid', wraps=lambda home: _pid_file(home)):
            self.assertEqual(self.service.gateway_ticked_bots(), set())
            self.gateway(self.home / 'profiles/care')
            self.assertEqual(self.service.gateway_ticked_bots(), {self.bots['care']})
            self.gateway(self.home)
            (self.home / 'profiles/desk/gateway.parked').write_text('')
            self.assertEqual(self.service.gateway_ticked_bots(), {self.bots['default'], self.bots['care']})
        # The gateway that Coconut started ticks the profiles too, also before Hermes can prove its identity.
        (self.home / 'gateway.pid').unlink()
        with patch.object(gateway_chat, 'live_gateway_pid', return_value=None):
            self.assertEqual(self.service.gateway_ticked_bots(), set())
            self.service.gateway_process = self.gateway(self.home / 'unused')
            self.assertEqual(self.service.gateway_ticked_bots(), {self.bots['default'], self.bots['care']})
            self.service.gateway_process = None


def _pid_file(home):
    """The pid-file evidence of gateway_chat.live_gateway_pid, without the Hermes modules."""
    try:
        pid = json.loads((Path(home) / 'gateway.pid').read_text())['pid']
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError, KeyError):
        return None


if __name__ == '__main__':
    unittest.main()
