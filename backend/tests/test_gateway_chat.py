"""Plain chat turns through the Hermes gateway: the routing rule, the event translation, the setting, the gateway
process, and the profile edit. test_gateway_adapter.py checks the adapter, test_native_gateway_chat.py a real gateway."""
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


class RoutingRuleTests(unittest.TestCase):
    def test_only_a_plain_chat_turn_goes_to_the_gateway(self):
        plain = {'_chat_session': True, 'attachments': [], '_pending_decisions': [], '_tracked_pr_observations': []}
        self.assertTrue(plain_chat_turn(plain))
        self.assertFalse(plain_chat_turn(dict(plain, _chat_session=False)))
        for key in ('attachments', '_pending_decisions', '_native_job', '_work_context_id', '_bot_messages',
                    '_reaction_feedback', '_recent_work_updates', '_direct_reply_message_id'):
            with self.subTest(key=key):
                self.assertFalse(plain_chat_turn(dict(plain, **{key: [{'id': 'x'}]})))


class TurnEventTests(unittest.TestCase):
    def translate(self, *events):
        emitted = []
        turn = TurnEvents(lambda kind, data: emitted.append((kind, data)))
        return emitted, [turn.apply(event) for event in events]

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
        for event in ({'kind': 'other'}, {'kind': 'send', 'text': 'no message'}, {'kind': 'edit', 'message_id': 'm', 'text': 1}):
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
        self.bot = self.write('POST', '/v1/bots', {'name': 'Ada'})
        self.home = self.service.profiles.home(self.bot)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None):
        return self.service.mutate(method, path, data or {}, str(uuid4()), 'phone')

    def send(self, text):
        return self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': text})

    def replies(self, task):
        return [(m['text'], m['streaming'], bool(m.get('_interim'))) for m in self.service._task_messages(task['id'])
                if m['role'] == 'assistant' and m['kind'] == 'text']

    def test_the_setting_accepts_only_the_two_runtimes(self):
        self.assertEqual(self.service.chat_runtime(), 'worker')
        self.assertNotIn('gateway', self.service.hermes_setup())
        with self.assertRaises(ServiceError):
            self.write('PUT', '/v1/settings/chat-runtime', {'chat_runtime': 'cloud'})
        self.assertEqual(self.write('PUT', '/v1/settings/chat-runtime', {'chat_runtime': 'gateway'}),
                         {'chat_runtime': 'gateway'})
        self.assertEqual(self.service.chat_runtime(), 'gateway')
        # Without a Hermes home, no gateway runs, and the phone can read that.
        self.assertEqual(self.service.hermes_setup()['gateway'],
                         {'state': 'off', 'pid': None, 'reason': None, 'chat_runtime': 'gateway', 'connected_profiles': []})

    def test_a_plain_chat_turn_runs_in_the_gateway_with_the_gateway_setting(self):
        self.write('PUT', '/v1/settings/chat-runtime', {'chat_runtime': 'gateway'})
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
        self.assertEqual(adapter.turns[0]['text'], 'How are you?')
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')
        self.assertEqual(self.replies(task), [('Let me think.', False, True), ('I am well.', False, False)])
        self.assertEqual(self.service._get('bots', self.bot['id'])['last_message'], 'I am well.')
        self.assertEqual(self.service.gateway_status()['connected_profiles'], [self.service.profiles.row(self.bot)['name']])

    def test_the_worker_runs_the_turn_with_the_worker_setting_or_without_an_adapter(self):
        task = self.send('Worker, please.')
        Adapter(self.service.gateway_bridge, self.home, [])
        self.service._run_task(task['id'])
        self.assertEqual(len(self.provider.starts), 1)
        self.write('PUT', '/v1/settings/chat-runtime', {'chat_runtime': 'gateway'})
        # No adapter of this profile polled within ADAPTER_SECONDS.
        with patch.object(gateway_bridge, 'ADAPTER_SECONDS', 0):
            task = self.send('Still the worker.')
            self.service._run_task(task['id'])
        self.assertEqual(len(self.provider.starts), 2)
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')

    def test_a_turn_with_coconut_inputs_or_one_that_no_adapter_takes_runs_in_the_worker(self):
        self.write('PUT', '/v1/settings/chat-runtime', {'chat_runtime': 'gateway'})
        Adapter(self.service.gateway_bridge, self.home, [])
        task = self.send('A reaction is Coconut context.')
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['_reaction_feedback'] = [{'emoji': '👍'}]
            self.service._put('tasks', stored)
        self.service._run_task(task['id'])
        self.assertEqual(len(self.provider.starts), 1)
        task = self.send('Nobody takes this one.')
        with patch.object(gateway_bridge, 'TAKE_SECONDS', 0.2):
            self.service._run_task(task['id'])
        self.assertEqual(len(self.provider.starts), 2)
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')

    def test_a_cancelled_turn_stops_in_the_gateway(self):
        self.write('PUT', '/v1/settings/chat-runtime', {'chat_runtime': 'gateway'})
        task = self.send('A long answer, please.')
        adapter = Adapter(self.service.gateway_bridge, self.home, [{'kind': 'send', 'message_id': 'm:1', 'text': 'First'}])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        self.write('POST', f'/v1/tasks/{task["id"]}/cancel')
        runner.join(5)
        self.assertFalse(runner.is_alive())
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'cancelled')
        self.assertEqual(self.service.gateway_bridge.poll({'home': str(self.home), 'wait': 0})['stop'], [self.bot['id']])
        self.assertEqual(self.service.gateway_bridge.event(
            {'turn_id': adapter.turns[0]['id'], 'kind': 'done', 'outcome': 'cancelled'}), {'ok': False})


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

    def test_the_service_runs_one_gateway_for_the_hermes_home_only_with_the_gateway_setting(self):
        self.service._check_gateway()
        self.assertIsNone(self.service.gateway_process)
        with self.service.transaction():
            self.service.set_chat_runtime({'chat_runtime': 'gateway'})
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
        with self.service.transaction():
            self.service.set_chat_runtime({'chat_runtime': 'worker'})
        self.service._check_gateway()
        self.assertIsNone(self.service.gateway_process)
        self.assertIsNotNone(process.poll())
        self.assertIn('fake gateway runs', log.read_text())

    def test_a_gateway_that_the_person_runs_is_used_and_no_second_one_starts(self):
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        (self.home / 'gateway.pid').write_text('{"pid": %d}' % other.pid)
        with self.service.transaction():
            self.service.set_chat_runtime({'chat_runtime': 'gateway'})
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
        # The person's own block for the platform stays as it is.
        own = {'plugins': {'enabled': ['coconut']}, 'gateway': {'platforms': {'coconut': {'enabled': False}}}}
        self.assertIsNone(gateway_plugin.with_platform(own, True))
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
            self.assertEqual(gateway_plugin.link(home, gateway_plugin.PLUGIN, True), 'taken')
            self.assertEqual(gateway_plugin.link(home, gateway_plugin.PLUGIN, False), 'unchanged')
            self.assertTrue((home / 'plugins/coconut').is_dir())
            # Without a config.yaml, a home stays as it is.
            self.assertEqual(gateway_plugin.update(home / 'empty', True), 'skipped')


if __name__ == '__main__':
    unittest.main()
