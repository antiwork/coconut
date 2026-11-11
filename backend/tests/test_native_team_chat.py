"""A bot of a bot-only Mac answers in the team chat through a real `hermes gateway run`.

Ada's Mac is a full Mac. The bot-only Mac runs the team-only service with the bot Reporter. Both are in one team
through the FakeRelay and the FakeHelper of team_fakes.py: this test does not run the relay of Miniflare or the
cryptography of the helper (scripts/test_team_log.py runs those). The edit of `coconut team connect-chat --confirm`
(team_chat_config.py) connects a new Hermes home, the test starts its gateway as the operator does, and a local
stand-in of an OpenAI compatible model answers. No real model runs.

Ada names Reporter. The reply shows in Ada's chat as a message of the bot that answers her message. A reply to the bot
gets an answer. A message that names nobody gets none. It skips without a Hermes (hermes_env.py).
"""
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import unittest

from coconut.server import APIServer
from hermes_env import PYTHON, SOURCE, needs_hermes
import test_native_gateway_chat
from test_native_gateway_chat import Model
from test_team_only import TeamOnlyMacs

ROOT = Path(__file__).resolve().parents[2]
LAUNCH = ('import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ["hermes", *sys.argv[2:]]; '
          'from hermes_cli.main import main; sys.exit(main())')
FIRST = 'Three tasks are open.'
SECOND = 'Start with the export.'


def answer(request):
    """The reply of the stand-in model: SECOND for a question that answers the bot, else FIRST."""
    users = [message for message in request.get('messages') or [] if message.get('role') == 'user']
    last = json.dumps(users[-1].get('content')) if users else ''
    return dict(role='assistant', content=SECOND if 'Which one first' in last else FIRST)


@needs_hermes
class NativeTeamChatTests(TeamOnlyMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        test_native_gateway_chat.answer, self.saved = answer, test_native_gateway_chat.answer
        self.addCleanup(setattr, test_native_gateway_chat, 'answer', self.saved)
        Model.requests.clear()
        self.model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.addCleanup(self.model.server_close)
        self.addCleanup(self.model.shutdown)
        # A Hermes home of its own, not the home of the team-only tests.
        self.hermes_home = Path(self.temp.name) / 'agent'
        self.hermes_home.mkdir()
        (self.hermes_home / 'config.yaml').write_text(json.dumps({'model': {
            'default': 'stub-model', 'provider': 'custom', 'api_key': 'stub-key', 'context_length': 131072,
            'base_url': f'http://127.0.0.1:{self.model.server_port}/v1'}}))
        self.environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LANG') if key in os.environ}
        self.environment.update(HERMES_HOME=str(self.hermes_home), HERMES_DISABLE_LAZY_INSTALLS='1',
                                HERMES_DISABLE_TELEMETRY='1')

    def test_ada_names_the_bot_and_its_reply_reaches_her_chat(self):
        room = self.team()
        bot = self.declare(home=self.hermes_home)
        self.share(room, bot)
        server = APIServer(('127.0.0.1', 0), self.only)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        bridge = self.only.root / 'gateway/bridge.json'
        self.only.gateway_bridge.publish(f'http://127.0.0.1:{server.server_address[1]}')
        # The edit of coconut team connect-chat --confirm, under the Python of Hermes.
        edit = subprocess.run([PYTHON, '-I', '-B', str(ROOT / 'backend/coconut/team_chat_config.py'), 'connect',
                               SOURCE, str(self.hermes_home), str(bridge)], env=self.environment, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(edit.returncode, 0, edit.stdout + edit.stderr)
        self.call('PATCH', f'/v1/team/bots/{bot["id"]}', {'chat': True})
        self.sync()
        self.only.start()
        log = Path(self.temp.name) / 'gateway.log'
        with open(log, 'ab') as output:
            gateway = subprocess.Popen([PYTHON, '-c', LAUNCH, SOURCE, 'gateway', 'run'], cwd=self.temp.name,
                                       env=self.environment, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)

        def stop_gateway():
            if gateway.poll() is None:
                os.killpg(gateway.pid, signal.SIGTERM)
                try:
                    gateway.wait(30)
                except subprocess.TimeoutExpired:
                    os.killpg(gateway.pid, signal.SIGKILL)
                    gateway.wait(5)
        self.addCleanup(stop_gateway)

        def diagnostics():
            parts = [f'Model requests: {len(Model.requests)}', 'gateway.log:', log.read_text(errors='replace')[-4000:]]
            for path in sorted((self.hermes_home / 'logs').glob('*.log')):
                parts += [path.name + ':', path.read_text(errors='replace')[-4000:]]
            return '\n'.join(parts)

        deadline = time.monotonic() + 180
        while not self.only.gateway_bridge.connected(self.hermes_home) and time.monotonic() < deadline:
            self.assertIsNone(gateway.poll(), diagnostics())
            time.sleep(.5)
        self.assertTrue(self.only.gateway_bridge.connected(self.hermes_home), diagnostics())
        mention = self.call('GET', '/v1/team/rooms', service=self.first)['rooms'][0]['bots'][0]['id']

        def send(text, **fields):
            return self.call('POST', f'/v1/team/rooms/{room}/messages', dict(fields, text=text),
                             service=self.first)['message']

        def bot_messages():
            self.first.team_sync_pass()
            rows = self.call('GET', f'/v1/team/rooms/{room}/messages', service=self.first)['messages']
            return [row for row in reversed(rows) if row['bot']]

        def wait_for(count):
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                self.only.team_wake()
                found = bot_messages()
                if len(found) >= count:
                    return found
                time.sleep(1)
            self.fail(diagnostics())

        named = send('@Reporter what is open?', mentions=[mention])
        (first,) = wait_for(1)
        self.assertEqual((first['bot_name'], first['text'], first['reply_to']), ('Reporter', FIRST, named['id']))
        call = next(request for request in Model.requests if 'what is open' in json.dumps(request['messages']))
        # The sender and the team reach the model: Hermes names the sender in a group chat.
        self.assertIn('Ada', json.dumps(call['messages']))
        # The mode team registers no Coconut tool.
        self.assertNotIn('coconut_', json.dumps(call.get('tools') or []))

        answered = send('Which one first?', reply_to=first['id'])
        second = wait_for(2)[1]
        self.assertEqual((second['text'], second['reply_to']), (SECOND, answered['id']))

        calls = len(Model.requests)
        send('Lunch is at noon.')
        self.only.team_wake()
        time.sleep(5)
        self.assertEqual(len(bot_messages()), 2)
        self.assertEqual(len(Model.requests), calls, 'A message that names nobody starts no turn.')
        self.assertNotIn('Traceback', log.read_text(errors='replace'))
