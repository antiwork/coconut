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
import sys
import threading
import time
import unittest
from unittest import mock

from coconut.server import APIServer
from hermes_env import PYTHON, SOURCE, needs_hermes
import test_native_gateway_chat
from test_native_gateway_chat import Model
from test_team_only import TeamOnlyMacs
from test_team_turns import AsThePerson

ROOT = Path(__file__).resolve().parents[2]
LAUNCH = ('import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ["hermes", *sys.argv[2:]]; '
          'from hermes_cli.main import main; sys.exit(main())')
FIRST = 'Three tasks are open.'
SECOND = 'Start with the export.'
# A turn that runs a slow command, then asks the model again with its result.
SLOW = 'SLOWTOOL'
AFTER = 'The slow command is done.'
# The files that the slow command writes before and after its sleep. A test sets them in its scratch folder.
MARKERS = {}
SLEEP_SECONDS = 6


def answer(request):
    """The reply of the stand-in model: a terminal call that takes some seconds for SLOW, then AFTER with its result;
    SECOND for a question that answers the bot; else FIRST."""
    messages = request.get('messages') or []
    users = [index for index, message in enumerate(messages) if message.get('role') == 'user']
    last = json.dumps(messages[users[-1]].get('content')) if users else ''
    if SLOW in last and request.get('tools'):
        if any(message.get('role') == 'tool' for message in messages[users[-1]:]):
            return dict(role='assistant', content=AFTER)
        arguments = dict(command=f"touch '{MARKERS['started']}'; sleep {SLEEP_SECONDS}; touch '{MARKERS['ended']}'")
        return dict(role='assistant', content=None, tool_calls=[dict(
            id='call-slow', type='function', function=dict(name='terminal', arguments=json.dumps(arguments)))])
    return dict(role='assistant', content=SECOND if 'Which one first' in last else FIRST)


def results(request) -> bool:
    """True for a model request that carries the result of the slow command: the turn went on after the tool."""
    return SLOW in json.dumps(request.get('messages')) and any(
        message.get('role') == 'tool' for message in request.get('messages') or [])


@needs_hermes
class NativeTeamChatTests(AsThePerson, TeamOnlyMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        test_native_gateway_chat.answer, self.saved = answer, test_native_gateway_chat.answer
        self.addCleanup(setattr, test_native_gateway_chat, 'answer', self.saved)
        Model.requests.clear()
        MARKERS.update(started=Path(self.temp.name) / 'slow-started', ended=Path(self.temp.name) / 'slow-ended')
        self.addCleanup(MARKERS.clear)
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

    def start_chat(self):
        """Ada's team with the bot Reporter, its Hermes home connected as connect-chat --confirm does, the team-only
        service with its threads, and a real gateway for the home. Return the room and the bot."""
        room = self.team()
        bot = self.declare(home=self.hermes_home)
        self.share(room, bot)
        server = self.server = APIServer(('127.0.0.1', 0), self.only)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        bridge = self.only.root / 'gateway/bridge.json'
        self.only.gateway_bridge.publish(f'http://127.0.0.1:{server.server_address[1]}')
        # The events of the adapter, also those of a turn that the bridge no longer has.
        self.events = []
        record = self.only.gateway_bridge.event
        self.only.gateway_bridge.event = lambda event: (self.events.append(dict(event)), record(event))[1]
        # The edit of coconut team connect-chat --confirm, under the Python of Hermes.
        edit = subprocess.run([PYTHON, '-I', '-B', str(ROOT / 'backend/coconut/team_chat_config.py'), 'connect',
                               SOURCE, str(self.hermes_home), str(bridge)], env=self.environment, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(edit.returncode, 0, edit.stdout + edit.stderr)
        self.call('PATCH', f'/v1/team/bots/{bot["id"]}', {'chat': True})
        self.sync()
        self.only.start()
        self.log = Path(self.temp.name) / 'gateway.log'
        self.gateway = self.start_gateway()
        self.room = room
        self.mention = self.call('GET', '/v1/team/rooms', service=self.first)['rooms'][0]['bots'][0]['id']
        return room, bot

    def start_gateway(self):
        """Start `hermes gateway run` for the home, as the operator does, and wait until its adapter polls."""
        with open(self.log, 'ab') as output:
            gateway = subprocess.Popen([PYTHON, '-c', LAUNCH, SOURCE, 'gateway', 'run'], cwd=self.temp.name,
                                       env=self.environment, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.addCleanup(self.stop_gateway, gateway)
        started = time.monotonic()
        deadline = started + 180
        while time.monotonic() < deadline:
            self.assertIsNone(gateway.poll(), self.diagnostics())
            with self.only.gateway_bridge.lock:
                last = self.only.gateway_bridge.adapters.get(os.path.realpath(self.hermes_home))
            if last is not None and last > started:
                return gateway
            time.sleep(.5)
        self.fail(self.diagnostics())

    @staticmethod
    def stop_gateway(gateway):
        if gateway.poll() is None:
            os.killpg(gateway.pid, signal.SIGTERM)
            try:
                gateway.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(gateway.pid, signal.SIGKILL)
                gateway.wait(5)

    def diagnostics(self):
        parts = [f'Model requests: {len(Model.requests)}', 'gateway.log:', self.log.read_text(errors='replace')[-4000:]]
        for path in sorted((self.hermes_home / 'logs').glob('*.log')):
            parts += [path.name + ':', path.read_text(errors='replace')[-4000:]]
        return '\n'.join(parts)

    def send(self, text, **fields):
        return self.call('POST', f'/v1/team/rooms/{self.room}/messages', dict(fields, text=text),
                         service=self.first)['message']

    def bot_messages(self):
        self.first.team_sync_pass()
        rows = self.call('GET', f'/v1/team/rooms/{self.room}/messages', service=self.first)['messages']
        return [row for row in reversed(rows) if row['bot']]

    def wait_until(self, check, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.only.team_wake()
            found = check()
            if found:
                return found
            time.sleep(.5)
        self.fail(self.diagnostics())

    def test_ada_names_the_bot_and_its_reply_reaches_her_chat(self):
        self.start_chat()
        named = self.send('@Reporter what is open?', mentions=[self.mention])
        (first,) = self.wait_until(lambda: self.bot_messages()[:1])
        self.assertEqual((first['bot_name'], first['text'], first['reply_to']), ('Reporter', FIRST, named['id']))
        call = next(request for request in Model.requests if 'what is open' in json.dumps(request['messages']))
        # The sender and the team reach the model: Hermes names the sender in a group chat.
        self.assertIn('Ada', json.dumps(call['messages']))
        # The mode team registers no Coconut tool.
        self.assertNotIn('coconut_', json.dumps(call.get('tools') or []))

        answered = self.send('Which one first?', reply_to=first['id'])
        second = self.wait_until(lambda: self.bot_messages()[1:2])[0]
        self.assertEqual((second['text'], second['reply_to']), (SECOND, answered['id']))

        calls = len(Model.requests)
        self.send('Lunch is at noon.')
        self.only.team_wake()
        time.sleep(5)
        self.assertEqual(len(self.bot_messages()), 2)
        self.assertEqual(len(Model.requests), calls, 'A message that names nobody starts no turn.')
        self.assertNotIn('Traceback', self.log.read_text(errors='replace'))

    def test_ada_names_the_bot_in_a_named_chat_and_its_reply_stays_there(self):
        self.start_chat()
        design = self.call('POST', f'/v1/team/rooms/{self.room}/chats', {'name': 'Design'}, service=self.first)['chat']
        self.sync()
        named = self.send('@Reporter what is open in design?', mentions=[self.mention], chat=design)

        def replies():
            self.first.team_sync_pass()
            rows = self.call('GET', f'/v1/team/rooms/{self.room}/messages', {'chat': design},
                             service=self.first)['messages']
            return [row for row in reversed(rows) if row['bot']]
        (first,) = self.wait_until(lambda: replies()[:1])
        self.assertEqual((first['text'], first['reply_to'], first['chat']), (FIRST, named['id'], design))
        self.assertEqual(self.bot_messages(), [], 'The Team chat gets no reply.')
        # The chat is its own session in Hermes: the group chat has the name of the team and of the chat.
        call = next(request for request in Model.requests if 'open in design' in json.dumps(request['messages']))
        self.assertIn('Design', json.dumps(call['messages']))
        self.assertNotIn('Traceback', self.log.read_text(errors='replace'))

    def test_a_message_during_a_restart_of_the_gateway_gets_its_answer(self):
        from coconut import team_turns
        self.start_chat()
        for number in range(2):
            with self.subTest(restart=number):
                # The operator stops the gateway. A member names the bot at once, while the old process can still hold
                # its poll. Then the operator starts the gateway again.
                os.killpg(self.gateway.pid, signal.SIGTERM)
                named = self.send(f'@Reporter are you back {number}?', mentions=[self.mention])
                self.gateway.wait(60)
                self.gateway = self.start_gateway()
                answer = self.wait_until(lambda: [row for row in self.bot_messages() if row['reply_to'] == named['id']],
                                         seconds=110)
                self.assertEqual([row['text'] for row in answer], [FIRST], self.diagnostics())
                self.assertNotIn(team_turns.UNFINISHED, [row['text'] for row in self.bot_messages()])

    def stop_in_the_tool(self, stop):
        """Start a turn whose model runs a slow command, call `stop` while the command runs, and check that Hermes
        ends the turn: the model gets no request with the result of the command, and the adapter reports the end."""
        self.send(f'@Reporter {SLOW} run the slow one', mentions=[self.mention])
        first = self.wait_until(lambda: [request for request in Model.requests
                                         if SLOW in json.dumps(request['messages'])])[0]
        # The model has the terminal of Hermes, so the turn runs the slow command.
        self.assertIn('"terminal"', json.dumps(first.get('tools') or []), self.diagnostics())
        turn_id = self.wait_until(lambda: [turn.id for turn in self.only.gateway_bridge.turns.values()
                                           if turn.chat_id.startswith(self.only.team_store.answering(self.room)[0][0])
                                           and turn.state == 'active'])[0]
        # The command runs: it wrote its first marker and sleeps now.
        self.wait_until(lambda: MARKERS['started'].exists(), seconds=60)
        stop()
        self.wait_until(lambda: [event for event in self.events if event.get('turn_id') == turn_id
                                 and event['kind'] == 'done'], seconds=60)
        # Hermes interrupted the command: its second marker never comes, also after longer than its sleep, and the
        # model gets no request with its result.
        time.sleep(SLEEP_SECONDS + 4)
        self.assertFalse(MARKERS['ended'].exists(), self.diagnostics())
        self.assertEqual([request for request in Model.requests if results(request)], [], self.diagnostics())
        self.assertNotIn(AFTER, [row['text'] for row in self.bot_messages()])
        # The `/stop` reaches the session of the member itself. A `/stop` of another user would stop the turn only
        # through the chat scope of Hermes, which its log names.
        agent = (self.hermes_home / 'logs/agent.log').read_text(errors='replace')
        self.assertIn('STOP', agent, self.diagnostics())
        self.assertNotIn('stop_command_chat_scope', agent, self.diagnostics())
        return turn_id

    def test_a_disconnect_in_a_tool_call_ends_the_turn_in_hermes(self):
        room, bot = self.start_chat()
        self.stop_in_the_tool(lambda: self.call('PATCH', f'/v1/team/bots/{bot["id"]}', {'chat': False}))

    def stop_the_service(self, clean: bool):
        """Start a turn in the middle of a slow command, stop the service, and check that Hermes interrupts the
        command, the model gets no request with its result, and after a new start the chat has exactly the notice.
        Return the seconds from the stop to the interrupt of the command."""
        from coconut import team_turns
        self.start_chat()
        named = self.send(f'@Reporter {SLOW} run the slow one', mentions=[self.mention])
        self.wait_until(lambda: MARKERS['started'].exists(), seconds=60)
        agent = self.hermes_home / 'logs/agent.log'
        before = agent.read_text(errors='replace').count('Interrupt requested')
        started = time.monotonic()
        if clean:
            # The order of a clean stop (server.main): the adapters collect the stops while the listener answers.
            self.only.team_close_turns()
        self.server.shutdown()
        self.server.server_close()
        self.only.close()
        self.wait_until(lambda: agent.read_text(errors='replace').count('Interrupt requested') > before,
                        seconds=SLEEP_SECONDS - 5)
        took = time.monotonic() - started
        # Hermes interrupted the command: its second marker never comes, also after its sleep.
        time.sleep(max(0, SLEEP_SECONDS - took) + 4)
        self.assertFalse(MARKERS['ended'].exists(), self.diagnostics())
        self.assertEqual([request for request in Model.requests if results(request)], [], self.diagnostics())
        self.assertNotIn('stop_command_chat_scope', agent.read_text(errors='replace'))
        # The next start of the service posts the one notice, and no late reply comes.
        restarted = self.team_only_mac('only')
        restarted._turns_recover()
        self.first.team_sync_pass()
        self.assertEqual([(row['text'], row['reply_to']) for row in self.bot_messages()],
                         [(team_turns.UNFINISHED, named['id'])])
        return took

    def test_a_clean_stop_of_the_service_ends_the_turn_in_hermes_at_once(self):
        with mock.patch.object(sys.modules[__name__], 'SLEEP_SECONDS', 30):
            took = self.stop_the_service(clean=True)
        self.assertLess(took, 10)

    def test_a_service_that_dies_in_a_turn_ends_it_in_hermes_within_a_minute(self):
        # The adapter stops the turn after LOST_SECONDS (30) without the service, at its next try of the poll.
        with mock.patch.object(sys.modules[__name__], 'SLEEP_SECONDS', 80):
            took = self.stop_the_service(clean=False)
        self.assertLess(took, 65)

    def test_a_turn_past_its_time_ends_in_hermes(self):
        from coconut import team_turns
        self.start_chat()
        # The limit starts when the bridge registers the turn. The first marker comes about one second later, and the
        # limit ends while the command sleeps: the test checks both orders below.
        with mock.patch.object(team_turns, 'TURN_SECONDS', 4):
            started = time.monotonic()
            self.stop_in_the_tool(lambda: self.assertLess(time.monotonic() - started, 4, 'The limit came first.'))
        # The chat gets the notice of a turn past its time, and no text of the stop of Hermes.
        texts = self.wait_until(lambda: [row['text'] for row in self.bot_messages()])
        self.assertEqual(texts, [team_turns.UNFINISHED])
