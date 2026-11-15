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
import sqlite3
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
# Ada's message in her direct chat with the bot, and a message of the Team chat.
SECRET = 'ZEBRAFISH7731'
PRIVATE = f'My door code is {SECRET}. Keep it for me.'
PUBLIC_WORD = 'MARIGOLD42'
PUBLIC = f'@Reporter The launch word is {PUBLIC_WORD}.'
# A turn whose text has SEARCH calls a tool for each item of SEARCH_CALLS, one at a time. SEARCHES gets the
# tool results of each such turn.
SEARCH = 'DOSEARCH'
SEARCH_CALLS = []
SEARCHES = []
# The review of Hermes after a turn saves what it read, as a model can. REVIEWS gets the tool results of each review.
REVIEW = 'Review the conversation above'
REVIEWS = []
# A turn whose text has SAVE, and a pass of the curator of Hermes, save a fact and a skill. SAVES gets the tool results.
SAVE = 'SAVEFACT'
CURATOR = 'background skill CURATOR'
SAVES = []
PLACES = ('on the command line', 'in a cron job')


def tool_call(request, name, arguments, number):
    """A call of a tool. Hermes defers some tools behind its tool search bridge: such a call goes through
    `tool_call`."""
    if name in {tool['function']['name'] for tool in request['tools']}:
        function = dict(name=name, arguments=json.dumps(arguments))
    else:
        function = dict(name='tool_call', arguments=json.dumps({'calls': [{'name': name, 'arguments': arguments}]}))
    return dict(id=f'call-{number}', type='function', function=function)


def review(request, messages, last):
    """The review of a turn: a memory write and a new skill with what the turn said, then the end."""
    done = [message.get('content') for message in messages[last:] if message.get('role') == 'tool']
    fact = PRIVATE if SECRET in json.dumps(messages) else f'The launch word is {PUBLIC_WORD}.'
    calls = [('memory', dict(action='add', target='user', content=fact)),
             ('skill_manage', dict(operations=[dict(action='create', name=f'fact-{len(REVIEWS)}', content=(
                 f'---\nname: fact-{len(REVIEWS)}\ndescription: A fact of a chat.\n---\n{fact}\n'))]))]
    if len(done) < len(calls):
        name, arguments = calls[len(done)]
        return dict(role='assistant', content=None,
                    tool_calls=[tool_call(request, name, arguments, f'review-{len(done)}')])
    REVIEWS.append(dict(fact=fact, results=done))
    return dict(role='assistant', content='Saved.')


def save(request, messages, last, where):
    """A memory write and a new skill, then the end. A pass of the curator has no memory tool, and Hermes offers no
    skill_manage in a one-shot turn of the command line, so each of them makes only the write it can make."""
    done = [message.get('content') for message in messages[last:] if message.get('role') == 'tool']
    name = f'saved-{len(SAVES)}'
    calls = [('memory', dict(action='add', target='memory', content=f'Saved {where}.')),
             ('skill_manage', dict(operations=[dict(action='create', name=name, content=(
                 f'---\nname: {name}\ndescription: A skill of a test.\n---\nSaved {where}.\n'))]))]
    calls = {'curator': calls[1:], 'on the command line': calls[:1]}.get(where, calls)
    if len(done) < len(calls):
        tool, arguments = calls[len(done)]
        return dict(role='assistant', content=None,
                    tool_calls=[tool_call(request, tool, arguments, f'save-{len(done)}')])
    SAVES.append(dict(where=where, calls=[tool for tool, _ in calls], results=done))
    return dict(role='assistant', content='Saved.')


def answer(request):
    """The reply of the stand-in model: a terminal call that takes some seconds for SLOW, then AFTER with its result;
    the calls of SEARCH_CALLS for SEARCH; a memory write and a skill in a review; SECOND for a question that answers
    the bot; else FIRST."""
    messages = request.get('messages') or []
    users = [index for index, message in enumerate(messages) if message.get('role') == 'user']
    last = json.dumps(messages[users[-1]].get('content')) if users else ''
    if (SAVE in last or CURATOR in last) and request.get('tools'):
        where = next((place for place in PLACES if place in last), 'curator')
        return save(request, messages, users[-1], where)
    if REVIEW in last and request.get('tools'):
        return review(request, messages, users[-1])
    if SEARCH in last and request.get('tools'):
        done = [message.get('content') for message in messages[users[-1]:] if message.get('role') == 'tool']
        if len(done) < len(SEARCH_CALLS):
            # An item is the arguments of session_search, or a function that gives a tool and its arguments when the
            # model calls it.
            item = SEARCH_CALLS[len(done)]
            tool, arguments = item() if callable(item) else ('session_search', item)
            return dict(role='assistant', content=None, tool_calls=[
                tool_call(request, tool, arguments, f'search-{len(SEARCHES)}-{len(done)}')])
        SEARCHES.append(dict(calls=list(SEARCH_CALLS), results=done))
        return dict(role='assistant', content=f'Search {len(SEARCHES)} done.')
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
        for found in (SEARCH_CALLS, SEARCHES, REVIEWS, SAVES):
            found.clear()
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

    def test_ada_writes_to_the_bot_in_a_direct_chat_and_its_reply_reaches_her_alone(self):
        from team_fakes import DEVICE
        self.start_chat()
        self.first.team_request('GET', '/v1/team/rooms', {}, '', DEVICE, True)
        for _ in range(2):
            self.sync()
        path = f'/v1/team/rooms/{self.room}/directs/{self.mention}/messages'
        self.call('POST', path, {'text': 'What is open for me?'}, service=self.first)

        def replies():
            self.first.team_sync_pass()
            rows = self.call('GET', path, service=self.first)['messages']
            return [row for row in reversed(rows) if not row['this_mac']]
        (first,) = self.wait_until(lambda: replies()[:1])
        self.assertEqual((first['text'], first['reply_to']), (FIRST, None))
        self.assertEqual(self.bot_messages(), [], 'The Team chat gets no reply.')
        # Hermes takes the chat as a group chat of its own: no pairing question, no first-contact note of a `dm`
        # chat, and no change of the config of the home.
        call = next(request for request in Model.requests if 'open for me' in json.dumps(request['messages']))
        self.assertIn('Ada', json.dumps(call['messages']))
        self.assertNotIn('DM with', json.dumps(call['messages']))
        # The hint of the platform and the rules of the chat agree: only Ada reads the reply.
        self.assertNotIn('the whole team reads your reply', json.dumps(call))
        self.assertIn('Only Ada reads your reply.', json.dumps(call))
        self.assertNotIn('profile_build_offered', (self.hermes_home / 'config.yaml').read_text())
        log = self.log.read_text(errors='replace')
        self.assertNotIn('Traceback', log)
        self.assertNotIn('Unauthorized user', log)

    def team_of_two(self):
        """start_chat with Grace in the team, a named chat Design, and direct messages on for the Macs of Ada and
        Grace. Return the named chat."""
        from team_fakes import DEVICE
        self.start_chat()
        self.join(self.second, self.invite({'room': self.room})['link'], 'Grace')
        self.sync_both()
        for service in (self.first, self.second):
            service.team_request('GET', '/v1/team/rooms', {}, '', DEVICE, True)
        design = self.call('POST', f'/v1/team/rooms/{self.room}/chats', {'name': 'Design'}, service=self.first)['chat']
        self.sync_both()
        return design

    def sync_both(self):
        for _ in range(2):
            self.second.team_sync_pass()
            self.sync()

    def say(self, service, text, chat=None):
        """Name the bot in the Team chat, or in the named chat `chat`, and wait for its reply."""
        query = {'chat': chat} if chat else {}

        def replies():
            service.team_sync_pass()
            rows = self.call('GET', f'/v1/team/rooms/{self.room}/messages', query, service=service)['messages']
            return [row for row in rows if row['bot']]
        count = len(replies())
        service.team_sync_pass()
        self.call('POST', f'/v1/team/rooms/{self.room}/messages',
                  dict(query, text=f'@Reporter {text}', mentions=[self.mention]), service=service)
        self.wait_until(lambda: len(replies()) > count)

    def direct(self, service, text):
        """Write to the bot in the direct chat of the person of `service`, and wait for its reply."""
        path = f'/v1/team/rooms/{self.room}/directs/{self.mention}/messages'

        def replies():
            service.team_sync_pass()
            return [row for row in self.call('GET', path, service=service)['messages'] if not row['this_mac']]
        count = len(replies())
        self.call('POST', path, {'text': text}, service=service)
        self.wait_until(lambda: len(replies()) > count)

    def search(self, calls, ask):
        """The tool results of a turn that `ask` starts, in which the model calls session_search with each of
        `calls`."""
        SEARCH_CALLS[:] = calls
        count = len(SEARCHES)
        ask()
        self.wait_until(lambda: len(SEARCHES) > count)
        return SEARCHES[count]['results']

    def rows(self, query, *values):
        connection = sqlite3.connect(f'{(self.hermes_home / "state.db").as_uri()}?mode=ro', uri=True)
        try:
            return connection.execute(query, values).fetchall()
        finally:
            connection.close()

    def test_a_direct_chat_stays_out_of_the_search_of_each_other_chat(self):
        design = self.team_of_two()
        self.say(self.first, PUBLIC.removeprefix('@Reporter '))
        self.direct(self.first, PRIVATE)
        (direct,) = self.rows("SELECT id FROM sessions WHERE source = 'coconut' AND chat_id LIKE '%:direct:%'")[0]
        (message,) = self.rows('SELECT id FROM messages WHERE session_id = ? AND content LIKE ? ORDER BY id LIMIT 1',
                               direct, f'%{SECRET}%')[0]

        # Ada's own chat reads its own session, and finds the Team chat.
        own = self.search([dict(session_id=direct), dict(query=PUBLIC_WORD)],
                          lambda: self.direct(self.first, f'{SEARCH} in my own chat'))
        self.assertIn('door code', own[0])
        self.assertIn('launch word is', own[1])

        # Each other chat: Grace in the Team chat, in a named chat and in her own direct chat, and the operator on the
        # command line of Hermes. The Team chat stays searchable from each of them.
        calls = [dict(query=SECRET), dict(), dict(query=PUBLIC_WORD),
                 dict(query=SECRET, role_filter='user,assistant,tool'),
                 dict(session_id=direct), dict(session_id=direct, around_message_id=message),
                 dict(session_id=direct, profile='default')]

        def command_line():
            shot = subprocess.run([PYTHON, '-c', LAUNCH, SOURCE, '-z', f'{SEARCH} from the command line'],
                                  cwd=self.temp.name, env=self.environment, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=150)
            self.assertEqual(shot.returncode, 0, shot.stdout + shot.stderr)
        others = {'Team chat': lambda: self.say(self.second, f'{SEARCH} now'),
                  'named chat': lambda: self.say(self.second, f'{SEARCH} now', chat=design),
                  'direct chat of Grace': lambda: self.direct(self.second, f'{SEARCH} now'),
                  'command line': command_line}
        for name, ask in others.items():
            with self.subTest(chat=name):
                found = self.search(calls, ask)
                self.assertEqual(len(found), len(calls), found)
                for result in found:
                    self.assertNotIn('door code', result)
                self.assertIn('"mode": "discover"', found[0])
                self.assertIn('"mode": "browse"', found[1])
                self.assertIn('launch word is', found[2])
                for result in found[4:]:
                    self.assertIn('Coconut refused this search', result)
        self.assertNotIn('Traceback', self.log.read_text(errors='replace'))

    def test_the_first_tool_call_of_a_direct_chat_finds_the_chat_in_its_row(self):
        # The guard knows a direct chat by the row of its session. Hermes writes that row before the first tool call of
        # the first turn: the read of the own session works only when the guard finds the chat of the turn there.
        self.team_of_two()

        def own():
            rows = self.rows("SELECT id FROM sessions WHERE source = 'coconut' AND chat_id LIKE '%:direct:%'")
            return 'session_search', dict(session_id=rows[0][0] if rows else 'no row yet')
        found = self.search([own, lambda: ('memory', dict(action='add', target='memory', content=PRIVATE))],
                            lambda: self.direct(self.first, f'{SEARCH} {PRIVATE}'))
        self.assertIn('"mode": "read"', found[0])
        self.assertIn('door code', found[0])
        self.assertIn('Coconut refused this memory write', found[1])
        self.assertNotIn(SECRET.encode(), b''.join(self.shared().values()))

    def test_the_guard_knows_each_argument_of_session_search(self):
        # The guard refuses each argument that it does not know. A new argument of Hermes would refuse each call.
        import importlib.util
        spec = importlib.util.spec_from_file_location('privacy', ROOT / 'hermes_plugin/coconut/privacy.py')
        privacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(privacy)
        script = ('import json, sys; sys.path.insert(1, sys.argv[1]); '
                  'from tools.session_search_tool import SESSION_SEARCH_SCHEMA as schema; '
                  'print(json.dumps(sorted(schema["parameters"]["properties"])))')
        shot = subprocess.run([PYTHON, '-c', script, SOURCE], cwd=self.temp.name, env=self.environment,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertEqual(shot.returncode, 0, shot.stderr)
        self.assertEqual(json.loads(shot.stdout.splitlines()[-1]), sorted(privacy.ARGUMENTS))

    def shared(self):
        """The files of the memory and of the skills of the Hermes home: each chat reads them."""
        return {str(path.relative_to(self.hermes_home)): path.read_bytes()
                for folder in ('memories', 'skills') for path in (self.hermes_home / folder).rglob('*')
                if path.is_file()}

    def system_prompt(self, text):
        """The system prompt of the first model request whose last message has `text`."""
        request = self.wait_until(lambda: [request for request in Model.requests
                                           if text in json.dumps(request['messages'][-1])])[0]
        return json.dumps([message for message in request['messages'] if message.get('role') == 'system'])

    def test_the_review_after_a_turn_of_a_direct_chat_writes_no_memory_and_no_skill(self):
        # Hermes reviews each turn, not each tenth one, so the test sees the review at once.
        config = json.loads((self.hermes_home / 'config.yaml').read_text())
        config.update(memory={'nudge_interval': 1}, skills={'creation_nudge_interval': 1})
        (self.hermes_home / 'config.yaml').write_text(json.dumps(config))
        design = self.team_of_two()
        before = self.shared()
        self.direct(self.first, PRIVATE)
        (private,) = self.wait_until(lambda: REVIEWS[:1])
        # The review tried both writes, and the guard refused each of them.
        self.assertEqual(private['fact'], PRIVATE)
        self.assertEqual(len(private['results']), 2, private)
        self.assertIn('Coconut refused this memory write', private['results'][0])
        self.assertIn('Coconut refused this skill change', private['results'][1])
        self.assertEqual(self.shared(), before)

        # Grace's first turn in the Team chat starts a new session: its system prompt has no word of Ada's chat.
        self.say(self.second, 'hello from Grace')
        self.assertNotIn(SECRET, self.system_prompt('hello from Grace'))
        # The review after a turn of the Team chat still writes the memory and a skill.
        (_, public) = self.wait_until(lambda: REVIEWS[1:2] and REVIEWS)
        self.assertNotIn('Coconut refused', json.dumps(public['results']))
        self.assertIn(PUBLIC_WORD, (self.hermes_home / 'memories/USER.md').read_text())
        self.assertIn(PUBLIC_WORD.encode(), b''.join(text for name, text in self.shared().items()
                                                     if name.startswith('skills')))
        # The next session gets that memory in its system prompt, and still no word of Ada's chat.
        self.say(self.second, 'hello in design', chat=design)
        prompt = self.system_prompt('hello in design')
        self.assertIn(PUBLIC_WORD, prompt)
        self.assertNotIn(SECRET, prompt)
        self.assertNotIn(SECRET.encode(), b''.join(self.shared().values()))

    def hermes(self, *arguments):
        shot = subprocess.run([PYTHON, '-c', LAUNCH, SOURCE, *arguments], cwd=self.temp.name, env=self.environment,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        self.assertEqual(shot.returncode, 0, shot.stdout + shot.stderr)
        return shot.stdout

    def test_the_agent_still_learns_outside_the_chats(self):
        # Most sessions of a team agent are no chat: command line turns, cron jobs and the curator. The guard is on
        # in each of them (the plugin loads in the mode team), and their memory and skill writes run.
        self.start_chat()
        self.hermes('-z', f'{SAVE} on the command line')
        self.hermes('cron', 'create', '30m', f'{SAVE} in a cron job', '--name', 'facts')
        jobs = json.loads((self.hermes_home / 'cron/jobs.json').read_text())
        (job,) = jobs['jobs'] if isinstance(jobs, dict) else jobs
        self.hermes('cron', 'run', job['id'])
        # The curator works on the skills that the agent made. Its session has no row in state.db.
        skill = self.hermes_home / 'skills/notes/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('---\nname: notes\ndescription: Notes of the team.\n---\nKeep notes short.\n')
        self.hermes('curator', 'adopt', 'notes', '--yes')
        self.hermes('curator', 'run', '--sync', '--consolidate')
        saved = {item['where']: item for item in SAVES}
        self.assertEqual(sorted(saved), ['curator', 'in a cron job', 'on the command line'], SAVES)
        for where, item in saved.items():
            with self.subTest(where=where):
                self.assertEqual(len(item['results']), len(item['calls']), item)
                self.assertNotIn('Coconut', json.dumps(item['results']), item)
        memory = (self.hermes_home / 'memories/MEMORY.md').read_text()
        self.assertIn('Saved on the command line.', memory)
        self.assertIn('Saved in a cron job.', memory)
        skills = b''.join(path.read_bytes() for path in (self.hermes_home / 'skills').rglob('SKILL.md'))
        for where in ('in a cron job', 'curator'):
            self.assertIn(f'Saved {where}.'.encode(), skills, SAVES)
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
