"""A chat message goes through a real `hermes gateway run` and its reply reaches Coconut.

The turn continues the bot's native conversation from an earlier worker turn, gets the coconut tools, and streams. A
background process of a later turn ends, and the reply of the Hermes turn for its completion reaches the bot's chat
outside a Coconut turn. A routine of the bot fires in the gateway's cron, and its run is a Coconut turn in the bot's
chat.

Setup's edit (skill_dirs.py with the config writer of Hermes) installs the `coconut` platform in a new Hermes home. The
service starts the gateway for that home, the adapter connects over loopback, and a local stand-in of an OpenAI
compatible model answers. The test fails when the gateway loads no adapter, takes no turn, or sends no reply.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from hermes_env import needs_hermes, PYTHON, SOURCE

ROOT = Path(__file__).resolve().parents[2]
REPLY = 'Hello from the stub model.'
# A chat turn that starts a background process, and the reply of the Hermes turn for its completion.
START = 'Start the background job.'
STARTED = 'The job runs in the background.'
OUTPUT = 'background-job-output'
FINISHED = 'The background job finished.'
# An attached text file, and the reply for a turn that reads it.
ATTACHED = 'native-attachment-marker'
READ = 'I read the attached notes.'
REACTION = 'changed an emoji reaction'
# A chat turn that asks on a card, and the turn that continues after the answer. After its card the model writes no
# text, as models often do (hooks.end_turn_text).
ASK = 'Ask me on a card.'
ASKED = ''
# The nudge of Hermes after an empty reply that follows a tool call (agent/conversation_loop.py).
EMPTY_NUDGE = 'returned an empty response'
RESUMED = 'Continue the original task using this recorded answer.'
# A chat turn whose model runs a tool and then writes nothing, also after the reminder of Hermes.
QUIET = 'Note the result and stay quiet.'
SHIPPED = 'Shipping now.'
# A chat turn that leaves its task list unfinished, and the turn that Coconut starts to continue it.
LIST = 'Track the docs work in a list.'
LISTED = 'The list is ready.'
CONTINUE = 'Continue the unfinished assignment from its saved task list.'
FINISHED_LIST = 'The docs work is done.'
# A task with its own work session, which runs in the chat of that work session.
AUDIT = 'Reconcile these open assignments against the original sources.'
AUDITED = 'The audit found nothing new.'
# A question about a Work item. The turn gets the worktree of its work session and runs a command in it.
FIX = 'Fix the export in the repository.'
FIXED = 'The export fix is in the worktree.'
REPOSITORY = {}
# A chat turn whose model call reports a prompt above the compression threshold of Hermes.
LARGE = 'This prompt is large.'
LARGE_PROMPT = 100_000
CARD = dict(title='Ship the release?', context='The build passed and the notes are ready.',
            options=[dict(id='ship', label='Ship it', explanation=''), dict(id='wait', label='Wait a day', explanation='')])


def answer(request):
    """The assistant message for a request: a terminal call for START, FINISHED for the completion notice of the
    background process, and REPLY otherwise."""
    messages = request.get('messages') or []
    users = [index for index, message in enumerate(messages) if message.get('role') == 'user']
    last = json.dumps(messages[users[-1]].get('content')) if users else ''
    # The own text of the last user message. The context of the turn can follow it (hooks.add_turn_context).
    own = str(messages[users[-1]].get('content')) if users else ''
    if own.startswith(QUIET) or (EMPTY_NUDGE in own and QUIET in json.dumps(messages)):
        if own.startswith(QUIET) and request.get('tools') and not any(m.get('role') == 'tool' for m in messages[users[-1]:]):
            return dict(role='assistant', content=None, tool_calls=[dict(
                id='call-quiet', type='function', function=dict(name='terminal', arguments=json.dumps(dict(
                    command='echo noted'))))])
        return dict(role='assistant', content='')
    if OUTPUT in last:
        return dict(role='assistant', content=FINISHED)
    if ATTACHED in last:
        return dict(role='assistant', content=READ)
    if REACTION in last:
        return dict(role='assistant', content='[SILENT]')
    # The Work record of the question names the audit that made the item, so this check comes first.
    if FIX in last and request.get('tools'):
        done = [message for message in messages[users[-1]:] if message.get('role') == 'tool']
        if len(done) >= 2:
            return dict(role='assistant', content=FIXED)
        # Hermes lists plugin tools behind its tool search bridge. The terminal tool is a core tool.
        call = (dict(name='terminal', arguments=json.dumps(dict(command='pwd'))) if done else
                dict(name='tool_call', arguments=json.dumps(dict(name='coconut_workspace',
                                                                 arguments=dict(repository=REPOSITORY['path'])))))
        return dict(role='assistant', content=None, tool_calls=[dict(id=f'call-fix-{len(done)}', type='function',
                                                                     function=call)])
    if AUDIT in last:
        return dict(role='assistant', content=AUDITED)
    if RESUMED in last:
        return dict(role='assistant', content=SHIPPED)
    if (LIST in last or CONTINUE in last) and request.get('tools'):
        if any(message.get('role') == 'tool' for message in messages[users[-1]:]):
            return dict(role='assistant', content=FINISHED_LIST if CONTINUE in last else LISTED)
        status = 'completed' if CONTINUE in last else 'pending'
        todos = [dict(id='coconut-assignment', content=LIST, status=status), dict(id='docs', content='Write the docs.', status=status)]
        # A Hermes lists todo_list directly or behind its tool search bridge.
        names = {tool.get('function', {}).get('name') for tool in request['tools']}
        call = (dict(name='todo_list', arguments=json.dumps(dict(todos=todos))) if 'todo_list' in names else
                dict(name='tool_call', arguments=json.dumps(dict(name='todo_list', arguments=dict(todos=todos)))))
        return dict(role='assistant', content=None, tool_calls=[dict(id='call-todo', type='function', function=call)])
    if ASK in last and request.get('tools'):
        if any(message.get('role') == 'tool' for message in messages[users[-1]:]):
            return dict(role='assistant', content=ASKED)
        # Hermes lists plugin tools behind its tool search bridge, so the model calls coconut_ask through tool_call.
        arguments = dict(name='coconut_ask', arguments=CARD)
        return dict(role='assistant', content=None, tool_calls=[dict(
            id='call-card', type='function', function=dict(name='tool_call', arguments=json.dumps(arguments)))])
    if START in last and request.get('tools'):
        if any(message.get('role') == 'tool' for message in messages[users[-1]:]):
            return dict(role='assistant', content=STARTED)
        arguments = dict(command=f'sleep 1; echo {OUTPUT}', background=True, notify_on_complete=True)
        return dict(role='assistant', content=None, tool_calls=[dict(
            id='call-background', type='function', function=dict(name='terminal', arguments=json.dumps(arguments)))])
    return dict(role='assistant', content=REPLY)


class Model(BaseHTTPRequestHandler):
    """Answer each chat completion with answer(), streamed or not."""
    requests = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_error(404)

    def do_POST(self):
        if not self.path.endswith('/chat/completions'):
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Model.requests.append(request)
        message = answer(request)
        finish = 'tool_calls' if message.get('tool_calls') else 'stop'
        data = dict(id='stub', created=int(time.time()), model=request['model'])
        users = [m for m in request.get('messages') or [] if m.get('role') == 'user']
        prompt = LARGE_PROMPT if users and LARGE in json.dumps(users[-1].get('content')) else 1
        usage = dict(prompt_tokens=prompt, completion_tokens=1, total_tokens=prompt + 1)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if request.get('stream') else 'application/json')
        self.end_headers()
        if request.get('stream'):
            if message.get('tool_calls'):
                deltas = [dict(role='assistant', tool_calls=[dict(call, index=0) for call in message['tool_calls']])]
            else:
                deltas = [dict(role='assistant', content=message['content'][:6]), dict(content=message['content'][6:])]
            for index, delta in enumerate(deltas):
                data.update(object='chat.completion.chunk', choices=[
                    dict(index=0, delta=delta, finish_reason=finish if index == len(deltas) - 1 else None)])
                self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
                if message.get('content') == REPLY and index == 0:
                    # A streaming platform would show this first piece before the rest arrives.
                    self.wfile.flush()
                    time.sleep(.5)
            data.update(choices=[], usage=usage)
            self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            data.update(object='chat.completion', usage=usage,
                        choices=[dict(index=0, message=message, finish_reason=finish)])
            self.wfile.write(json.dumps(data).encode())


def adapter_text(name):
    """A constant of the plugin's adapter, read from its source without an import of the plugin."""
    import ast
    tree = ast.parse((ROOT / 'hermes_plugin/coconut/adapter.py').read_text())
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(getattr(target, 'id', None) == name for target in node.targets))


@needs_hermes
class EmptyReplyFormatTests(unittest.TestCase):
    """The closing text of hooks.end_turn_text in each response format, as the transports of this Hermes read it."""

    def setUp(self):
        if SOURCE not in sys.path:
            sys.path.insert(1, SOURCE)
        import importlib.util
        import types
        plugin = ROOT / 'hermes_plugin/coconut'
        sys.modules.setdefault('hermes_plugins', types.ModuleType('hermes_plugins')).__path__ = []
        spec = importlib.util.spec_from_file_location('hermes_plugins.coconut', plugin / '__init__.py',
                                                      submodule_search_locations=[str(plugin)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        import hermes_plugins.coconut.hooks as hooks
        self.hooks = hooks

    def test_an_empty_reply_gets_the_closing_text_in_each_format(self):
        from types import SimpleNamespace as N
        from agent.transports import get_transport
        end = self.hooks.with_end_text
        chat = lambda content, calls=None: N(choices=[N(index=0, finish_reason='tool_calls' if calls else 'stop', message=N(
            role='assistant', content=content, tool_calls=calls, reasoning_content=None))], usage=None)
        anthropic = lambda *blocks: N(content=list(blocks), stop_reason='end_turn', usage=None)
        responses = lambda *items: N(output=list(items), status='completed', usage=None)
        reasoning = N(type='reasoning', id='rs_1', summary=[], encrypted_content=None, status='completed')
        empty = {'chat_completions': chat(''), 'anthropic_messages': anthropic(),
                 'codex_responses': responses(reasoning)}
        for mode, response in empty.items():
            with self.subTest(mode):
                closed = end(response, mode)
                self.assertIsNotNone(closed)
                reply = get_transport(mode).normalize_response(closed)
                self.assertEqual((reply.content.strip(), reply.tool_calls, reply.finish_reason),
                                 (adapter_text('END_TEXT'), None, 'stop'))
        call = N(id='call_1', type='function', function=N(name='terminal', arguments='{}'))
        written = {'chat_completions': [chat('Done.'), chat(None, [call])],
                   'anthropic_messages': [anthropic(N(type='text', text='Done.')),
                                          anthropic(N(type='tool_use', id='toolu_1', name='terminal', input={}))],
                   'codex_responses': [responses(N(type='message', role='assistant', status='completed', phase='final_answer',
                                                   content=[N(type='output_text', text='Done.')]))]}
        for mode, items in written.items():
            for response in items:
                with self.subTest(mode, response=response):
                    self.assertIsNone(end(response, mode))

    def test_a_reply_of_only_reasoning_from_the_codex_backend_gets_the_closing_text(self):
        # The Codex backend of OpenAI can end with only a reasoning item. Hermes reads that reply as incomplete and asks
        # again, and after three such replies it tries its fallback model. The closed copy is a final reply.
        from types import SimpleNamespace as N
        from agent.transports import get_transport
        reasoning = N(type='reasoning', id='rs_1', encrypted_content='opaque', status='completed',
                      summary=[N(type='summary_text', text='Nothing to add.')])
        response = N(output=[reasoning], status='completed', usage=None)
        read = lambda raw: get_transport('codex_responses').normalize_response(raw, issuer_kind='codex_backend')
        self.assertEqual((read(response).content, read(response).finish_reason), ('', 'incomplete'))
        closed = self.hooks.with_end_text(response, 'codex_responses')
        self.assertIsNotNone(closed)
        reply = read(closed)
        self.assertEqual((reply.content.strip(), reply.tool_calls, reply.finish_reason),
                         (adapter_text('END_TEXT'), None, 'stop'))

    def test_a_rejected_model_call_logs_its_shape_through_the_middleware_runner_of_hermes(self):
        # The runner of Hermes wraps an error of the model call before it reaches a middleware
        # (hermes_cli/middleware.py). The plugin reads the status of the provider's error, logs the shape of the request
        # and lets the error through unchanged.
        from types import SimpleNamespace as N
        from unittest.mock import patch
        from hermes_cli.middleware import _run_execution_chain
        import hermes_cli.plugins as plugins

        class Rejected(Exception):
            status_code = 400

        error = Rejected('Bad Request')
        def provider(_request):
            raise error
        request = {'input': [{'role': 'user', 'content': 'Hello.'}], 'tools': []}
        manager = N(_middleware={'llm_execution': [self.hooks.end_turn_text]},
                    _report_hook_failure=lambda *args, **kwargs: self.fail('Hermes took the error as a fault of the plugin.'))
        with patch.object(plugins, '_delivery_manager', lambda: manager), \
                self.assertLogs('hermes_plugins.coconut.hooks', 'WARNING') as logs, \
                self.assertRaises(Rejected) as raised:
            _run_execution_chain('llm_execution', provider, request=request, api_mode='codex_responses')
        self.assertIs(raised.exception, error)
        self.assertEqual(len(logs.output), 1)
        for part in ('Rejected', 'HTTP 400', 'api_mode codex_responses', '1 items', 'user '):
            self.assertIn(part, logs.output[0])
        self.assertNotIn('Hello', logs.output[0])

    def test_the_last_item_of_a_request_is_a_tool_result_in_each_format(self):
        # hooks.after_tool_result on the requests that the converters of this Hermes build from the same messages.
        from agent.anthropic_message_convert import convert_messages_to_anthropic
        from agent.codex_responses_adapter import _chat_messages_to_responses_input
        typed = [{'role': 'user', 'content': 'Do it.'}]
        after_tool = typed + [
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_1', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'ok'}]
        reminded = after_tool + [{'role': 'assistant', 'content': '(empty)'},
                                 {'role': 'user', 'content': 'Continue.'}]
        for messages, expected in ((typed, False), (after_tool, True), (reminded, False)):
            requests = {'chat_completions': {'messages': messages},
                        'codex_responses': {'input': _chat_messages_to_responses_input(messages)},
                        'anthropic_messages': {'messages': convert_messages_to_anthropic(messages)[1]}}
            for mode, request in requests.items():
                with self.subTest(mode, last=messages[-1]['content']):
                    self.assertIs(self.hooks.after_tool_result(request), expected)

    def test_a_session_gets_every_record_again_after_hermes_compresses_it_in_place(self):
        # hooks.add_records with the state database of this Hermes. Its compression keeps the session ID and the key of
        # the records, and replaces the messages with a summary (SessionDB.archive_and_compact).
        from types import SimpleNamespace as N
        from unittest.mock import patch
        temp = tempfile.TemporaryDirectory(prefix='coconut-records-')
        self.addCleanup(temp.cleanup)
        with patch.dict(os.environ, HERMES_HOME=temp.name):
            from hermes_state import SessionDB
            path = Path(temp.name) / 'state.db'
            db = SessionDB(db_path=path)
            self.addCleanup(db.close)
            db.create_session('s1', source='coconut')
            # A record text that ends with white space. Hermes removes it when it saves the message.
            records = [{'id': 'comment:1', 'text': 'Comment one.'}, {'id': 'comment:2', 'text': 'Comment two.\r\n  '}]

            def turn(identity):
                result = self.hooks.add_records(N(message_id=identity, text='Own text.'), records, (path, 's1'))
                db.append_message('s1', role='user', content=result['text'] if result else 'Own text.')
                self.hooks.commit_records(identity)
                return [record['id'] for record in records if result and record['text'] in result['text']]

            self.assertEqual(turn('turn-1'), ['comment:1', 'comment:2'])
            self.assertEqual(turn('turn-2'), [])
            db.archive_and_compact('s1', [{'role': 'user', 'content': 'Summary of the earlier turns.'}])
            self.assertEqual(db.resolve_resume_session_id('s1'), 's1')
            self.assertIsNotNone(db.get_meta(self.hooks.RECORDS_KEY + 's1'))
            self.assertEqual(turn('turn-3'), ['comment:1', 'comment:2'])
            self.assertEqual(turn('turn-4'), [])

    def test_a_running_session_gets_the_note_about_changed_instructions_once(self):
        # hooks.add_records reads the start of a session from the session row of this Hermes (`started_at`).
        from types import SimpleNamespace as N
        from unittest.mock import patch
        temp = tempfile.TemporaryDirectory(prefix='coconut-instructions-')
        self.addCleanup(temp.cleanup)
        with patch.dict(os.environ, HERMES_HOME=temp.name):
            from hermes_state import SessionDB
            path = Path(temp.name) / 'state.db'
            db = SessionDB(db_path=path)
            self.addCleanup(db.close)
            db.create_session('s1', source='coconut')
            db.create_session('s2', source='coconut')
            # A session row without a message is new, so s2 holds one.
            db.append_message('s2', role='user', content='Earlier.')

            def turn(identity, session, changed_at):
                record = {'id': 'instructions:r1', 'kind': 'instructions', 'revision': 'r1', 'changed_at': changed_at,
                          'text': 'Read your instructions again.'}
                result = self.hooks.add_records(N(message_id=identity, text='Own text.'), [record], (path, session))
                self.hooks.commit_records(identity)
                return bool(result and record['text'] in result['text'])

            # Sessions without a revision on record. One started after the newest change: no note.
            self.assertFalse(turn('turn-1', 's1', time.time() - 3600))
            self.assertEqual(db.get_meta(self.hooks.INSTRUCTIONS_KEY + 's1'), 'r1')
            # One started before the newest change: the note once.
            self.assertTrue(turn('turn-2', 's2', time.time() + 3600))
            self.assertEqual(db.get_meta(self.hooks.INSTRUCTIONS_KEY + 's2'), 'r1')
            self.assertFalse(turn('turn-3', 's2', time.time() + 3600))


@needs_hermes
class NativeGatewayChatTests(unittest.TestCase):
    def setUp(self):
        if SOURCE not in sys.path:
            sys.path.insert(1, SOURCE)
        self.temp = tempfile.TemporaryDirectory(prefix='coconut-gateway-')
        self.addCleanup(self.temp.cleanup)
        self.model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.addCleanup(self.model.server_close)
        self.addCleanup(self.model.shutdown)

    def test_a_chat_message_goes_through_a_real_gateway_and_its_reply_reaches_coconut(self):
        from coconut.providers import FixtureProvider
        from coconut.server import APIServer
        from coconut.service import Service
        root = Path(self.temp.name)
        home, data = root / 'hermes', root / 'data'
        home.mkdir()
        (home / 'config.yaml').write_text(json.dumps({
            'model': {'default': 'stub-model', 'provider': 'custom', 'api_key': 'stub-key', 'context_length': 131072,
                      'base_url': f'http://127.0.0.1:{self.model.server_port}/v1'},
            # The person may stream to other platforms, here with each piece at once. Hermes streams nothing to the
            # coconut platform.
            'streaming': {'enabled': True, 'edit_interval': 0.05, 'buffer_threshold': 1},
            'gateway': {'platforms': {'coconut': {'enabled': True,
                                                  'extra': {'bridge': str(data / 'gateway/bridge.json')}}}}}))
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LANG') if key in os.environ}
        environment.update(HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_DISABLE_TELEMETRY='1')
        # Setup's edit, with the config writer of this Hermes.
        edit = subprocess.run([PYTHON, '-I', '-B', str(ROOT / 'backend/coconut/skill_dirs.py'), 'add', SOURCE,
                               str(ROOT / 'skills'), str(home)], env=environment, capture_output=True, text=True,
                              timeout=120)
        self.assertEqual(edit.returncode, 0, edit.stdout + edit.stderr)
        self.assertIn('coconut gateway platform added', edit.stdout)
        self.assertEqual((home / 'plugins/coconut').resolve(), ROOT / 'hermes_plugin/coconut')

        class Provider(FixtureProvider):
            supports_native_outcomes = True
            python, hermes_root = PYTHON, SOURCE

            def start_task(self, task, bot, history, emit):
                raise AssertionError('The worker must not run this turn.')

        def routine_settings(operation, bots=(), **request):
            # The routine operations with this Hermes, as the settings process of HermesProvider runs them.
            homes = {bot['id']: str(service.profiles.home(bot)) for bot in bots}
            code = ('import json, sys; sys.path[:0] = sys.argv[1:3]; from native_routines import worker; '
                    'print(json.dumps(worker(sys.argv[3], json.loads(sys.argv[4]), json.loads(sys.argv[5]))))')
            run = subprocess.run([PYTHON, '-c', code, SOURCE, str(ROOT / 'backend/coconut'), operation,
                                  json.dumps(request), json.dumps(homes)],
                                 env=environment, capture_output=True, text=True, timeout=120)
            if run.returncode:
                raise RuntimeError(run.stderr[-4000:])
            return json.loads(run.stdout.splitlines()[-1])

        os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
        service = Service(data, Provider(data), start_workers=False, hermes_home=home)
        events = []
        record = service.gateway_bridge.event
        service.gateway_bridge.event = lambda event: (events.append(dict(event)), record(event))[1]
        server = APIServer(('127.0.0.1', 0), service)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
        log = data / 'gateway/gateway.log'

        def diagnostics():
            parts = [f'Model requests: {len(Model.requests)}', 'gateway.log:', log.read_text()[-4000:]]
            for path in sorted((home / 'logs').glob('*.log')):
                parts += [str(path.name) + ':', path.read_text(errors='replace')[-6000:]]
            return '\n'.join(parts)
        try:
            service.gateway_bridge.publish(f'http://127.0.0.1:{server.server_address[1]}')
            service._check_gateway()
            self.assertEqual(service.gateway_state['state'], 'running', service.gateway_state)
            deadline = time.monotonic() + 180
            while not service.gateway_bridge.connected(home) and time.monotonic() < deadline:
                self.assertIsNone(service.gateway_process.poll(), diagnostics())
                time.sleep(.5)
            self.assertTrue(service.gateway_bridge.connected(home), diagnostics())
            self.assertEqual(service.gateway_status()['connected_profiles'], ['default'])
            bot = next(bot for bot in service.snapshot()['bots'] if service.profiles.row(bot)['name'] == 'default')
            # An earlier worker turn of this bot, in its native conversation (native_conversation.NativeConversation).
            from hermes_state import SessionDB
            from coconut.native_conversation import session_root
            root = session_root(service._get('bots', bot['id']), epoch=service._setting('conversation_epoch'))
            db = SessionDB(db_path=home / 'state.db')
            db.create_session(root, source='coconut', model='stub-model')
            db.replace_messages(root, [{'role': 'user', 'content': 'Remember the word mango.'},
                                       {'role': 'assistant', 'content': 'I will remember mango.'}])
            db.close()
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Say hello.'}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            replies = [m['text'] for m in service._task_messages(task['id']) if m['role'] == 'assistant' and m['kind'] == 'text']
            self.assertEqual(service._get('tasks', task['id'])['status'], 'completed', diagnostics())
            # The reply comes whole with one send. No partial text and no edit reach Coconut.
            self.assertEqual(replies, [REPLY], diagnostics())
            self.assertEqual([(event['kind'], event.get('text')) for event in events
                              if event['kind'] in ('delta', 'send', 'edit')], [('send', REPLY)], diagnostics())
            turn = [request for request in Model.requests if 'Say hello.' in json.dumps(request['messages'])]
            self.assertTrue(turn, diagnostics())
            # The gateway turn continues the bot's native conversation, and it has the coconut tools.
            self.assertIn('mango', json.dumps(turn[0]['messages']), diagnostics())
            # Hermes lists plugin tools behind its tool search bridge (tools/tool_search.py): the names are in the
            # listing of tool_search, and tool_call runs them.
            tools = json.dumps(turn[0].get('tools') or [])
            for name in ('coconut_ask', 'coconut_track_pr', 'coconut_message_bot'):
                self.assertIn(name, tools, diagnostics())

            # An attached text file goes to the gateway as text of the turn.
            import base64
            notes = service.mutate('POST', '/v1/attachments', {
                'bot_id': bot['id'], 'filename': 'notes.txt',
                'content_base64': base64.b64encode(ATTACHED.encode()).decode()}, str(uuid4()), 'phone')
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages',
                                  {'text': 'Read the notes.', 'attachment_ids': [notes['id']]}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            replies = [m['text'] for m in service._task_messages(task['id']) if m['role'] == 'assistant' and m['kind'] == 'text']
            self.assertEqual((service._get('tasks', task['id'])['status'], replies), ('completed', [READ]), diagnostics())
            # Hermes saves only each turn's own text. The context of a turn, with the live cards, goes to its calls only,
            # so a later call does not send the context of an earlier turn again (hooks.add_turn_context).
            read = next(r for r in Model.requests if 'Read the notes.' in json.dumps(r['messages']))
            users = [m for m in read['messages'] if m.get('role') == 'user']
            self.assertIn('Context of this turn from Coconut', json.dumps(users[-1]), diagnostics())
            self.assertIn('Cards that wait for the person now', json.dumps(users[-1]), diagnostics())
            earlier = [m for m in users[:-1] if 'Say hello.' in json.dumps(m)]
            self.assertTrue(earlier, diagnostics())
            self.assertNotIn('Context of this turn from Coconut', json.dumps(earlier), diagnostics())
            # A reaction to a reply is a turn in the gateway, and the bot may stay silent.
            reply = next(m for m in service._task_messages(task['id']) if m['role'] == 'assistant')
            service.mutate('POST', f'/v1/messages/{reply["id"]}/reactions', {'emoji': '👍', 'active': True},
                           str(uuid4()), 'phone')
            reaction = next(t for t in service._all('tasks') if t['status'] == 'queued')
            service._run_task(reaction['id'])
            stored = service._get('tasks', reaction['id'])
            self.assertEqual((stored['status'], stored.get('_silent_completion')), ('completed', True), diagnostics())
            self.assertFalse([m for m in service._task_messages(reaction['id']) if m['role'] == 'assistant'
                              and m['kind'] == 'text' and not m.get('_hidden')], diagnostics())
            self.assertTrue(any(REACTION in json.dumps(request['messages']) for request in Model.requests), diagnostics())

            # A record of the work goes into the turn's text once (hooks.add_records). The session holds it while a
            # user message of the session has its text. After a compression in place, the next turn gets it again.
            from unittest.mock import patch
            work_record = {'id': 'comment:1', 'text': 'Record of the native task.\r\nSecond line.  '}
            collapsed = ' '.join(work_record['text'].split())

            def record_turn(text):
                task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': text}, str(uuid4()), 'phone')
                with patch('coconut.native_conversation.turn_records', return_value=[work_record]):
                    service._run_task(task['id'])
                self.assertEqual(service._get('tasks', task['id'])['status'], 'completed', diagnostics())
                request = next(r for r in Model.requests if text in json.dumps(r['messages']))
                users = [' '.join(json.dumps(m.get('content')).replace('\\r', ' ').replace('\\n', ' ').split())
                         for m in request['messages'] if m.get('role') == 'user']
                return [collapsed in user for user in users]

            # The first turn gets the record. The second turn does not, because an earlier message holds it.
            self.assertTrue(record_turn('First turn with a record.')[-1], diagnostics())
            held = record_turn('Second turn with a record.')
            self.assertFalse(held[-1], diagnostics())
            self.assertTrue(any(held[:-1]), diagnostics())
            # Hermes compresses the session in place: the same session ID holds only a summary.
            db = SessionDB(db_path=home / 'state.db')
            session = db.resolve_resume_session_id(db.get_meta('coconut.session-tip.' + root) or root)
            db.archive_and_compact(session, [{'role': 'user', 'content': 'Summary of the earlier turns.'}])
            db.close()
            self.assertTrue(record_turn('Third turn with a record.')[-1], diagnostics())

            # A typed /compress reaches Hermes as its command, without Coconut's context, and the turn gets its reply.
            # The conversation is short, so Hermes compresses nothing.
            calls = len(Model.requests)
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': '/compress'}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            replies = [m['text'] for m in service._task_messages(task['id']) if m['role'] == 'assistant' and m['kind'] == 'text']
            self.assertEqual(service._get('tasks', task['id'])['status'], 'completed', diagnostics())
            self.assertTrue(replies and REPLY not in replies, (replies, diagnostics()))
            self.assertFalse([r for r in Model.requests[calls:] if '/compress' in json.dumps(r['messages'])], diagnostics())
            # A turn whose model call reached the compression threshold: after the turn, the adapter compresses the chat.
            # Hermes's notice of it stays in the log, and the bot's chat shows only the turn's reply.
            with service.lock:
                before = len([m for m in service._all('messages') if m['bot_id'] == bot['id']])
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': LARGE}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            self.assertEqual(service._get('tasks', task['id'])['status'], 'completed', diagnostics())

            def logs():
                return '\n'.join(path.read_text(errors='replace') for path in [log, *(home / 'logs').glob('*.log')]
                                 if path.exists())
            deadline = time.monotonic() + 60
            while 'compression notice for chat' not in logs() and time.monotonic() < deadline:
                time.sleep(.5)
            self.assertIn(f'{LARGE_PROMPT} prompt tokens', logs(), diagnostics())
            self.assertIn('compression notice for chat', logs(), diagnostics())
            with service.lock:
                after = [m for m in service._all('messages') if m['bot_id'] == bot['id']][before:]
            self.assertEqual([m['text'] for m in after if m['role'] == 'assistant'], [REPLY], diagnostics())
            # A typed /new starts a new session, and the bot's conversation continues there: the next turn no longer
            # has the earlier conversation.
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': '/new'}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            self.assertEqual(service._get('tasks', task['id'])['status'], 'completed', diagnostics())
            replies = [m['text'] for m in service._task_messages(task['id']) if m['role'] == 'assistant' and m['kind'] == 'text']
            # Hermes asks to confirm /new, and the person answers as its text says.
            self.assertIn('/approve', ' '.join(replies), diagnostics())
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': '/approve'}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            self.assertEqual(service._get('tasks', task['id'])['status'], 'completed', diagnostics())
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Say hello again.'}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            fresh = next(r for r in reversed(Model.requests) if 'Say hello again.' in json.dumps(r['messages']))
            self.assertNotIn('mango', json.dumps(fresh['messages']), diagnostics())
            self.assertNotIn(LARGE, json.dumps(fresh['messages']), diagnostics())

            # A card of coconut_ask ends the gateway turn. The answer continues the conversation in a later turn.
            asked = len(Model.requests)
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': ASK}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            self.assertEqual(service._get('tasks', task['id'])['status'], 'needs_you', diagnostics())
            from hermes_cli.middleware import VALID_MIDDLEWARE
            if 'llm_execution' in VALID_MIDDLEWARE:
                # The model wrote no text after its card. Hermes ended the turn with the closing text of the plugin. It
                # did not nudge the model, which is the first step before its retries and its fallback model.
                self.assertNotIn(EMPTY_NUDGE, json.dumps(Model.requests[asked:]), diagnostics())
            card = next(d for d in service._all('decisions') if d['task_id'] == task['id'])
            self.assertEqual((card['title'], card['status']), (CARD['title'], 'pending'), diagnostics())
            service.mutate('POST', f'/v1/decisions/{card["id"]}/answer', {'option_id': 'ship', 'revision': 1},
                           str(uuid4()), 'phone')
            service._run_task(task['id'])
            replies = [m['text'] for m in service._task_messages(task['id']) if m['role'] == 'assistant' and m['kind'] == 'text']
            self.assertEqual((service._get('tasks', task['id'])['status'], replies[-1:]), ('completed', [SHIPPED]),
                             diagnostics())
            self.assertNotIn(adapter_text('END_TEXT'), replies, diagnostics())

            if 'llm_execution' in VALID_MIDDLEWARE:
                # The model writes nothing after its tool result, and Hermes reminds it once. It stays silent, so the
                # turn ends with the closing text, without the retries and the fallback model of Hermes. The chat shows
                # nothing for it.
                calls = len(Model.requests)
                task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': QUIET}, str(uuid4()), 'phone')
                service._run_task(task['id'])
                stored = service._get('tasks', task['id'])
                self.assertEqual((stored['status'], stored.get('_silent_completion')), ('completed', True),
                                 diagnostics())
                self.assertEqual([m['text'] for m in service._task_messages(task['id']) if m['role'] == 'assistant'
                                  and m['kind'] in ('text', 'activity')], [], diagnostics())
                reminders = [r for r in Model.requests[calls:] if EMPTY_NUDGE in json.dumps(r['messages'])]
                self.assertEqual(len(reminders), 1, diagnostics())
                # The tool call, the empty reply and the empty reply after the reminder: no retry.
                self.assertEqual(len(Model.requests) - calls, 3, diagnostics())

            # The todo list of a gateway turn is the work of its task. An unfinished list continues in a later turn.
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': LIST}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            stored = service._get('tasks', task['id'])
            self.assertEqual((stored['status'], stored.get('_work_resume')), ('queued', True), diagnostics())
            self.assertEqual([item['id'] for item in stored['_work_todos']['todos']], ['coconut-assignment', 'docs'],
                             diagnostics())
            service._run_task(task['id'])
            replies = [m['text'] for m in service._task_messages(task['id']) if m['role'] == 'assistant' and m['kind'] == 'text']
            self.assertEqual((service._get('tasks', task['id'])['status'], replies[-1:]), ('completed', [FINISHED_LIST]),
                             diagnostics())

            # A task outside the bot's conversation has its own work session. It runs in the gateway in a chat of its own,
            # beside the bot's chat.
            with service.transaction():
                audit = service._send(service._get('bots', bot['id']), {'text': AUDIT, 'mode': 'queue'},
                                      human_authored=False)
                service._ensure_execution_session(service._get('tasks', audit['id']))
            service._run_task(audit['id'])
            replies = [m['text'] for m in service._task_messages(audit['id']) if m['role'] == 'assistant' and m['kind'] == 'text']
            self.assertEqual((service._get('tasks', audit['id'])['status'], replies), ('completed', [AUDITED]), diagnostics())
            # The bot's own conversation does not hold that work.
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Say hello.'}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            self.assertNotIn(AUDIT, json.dumps(Model.requests[-1]['messages']), diagnostics())

            # A question about a Work item runs in the chat of its work session. coconut_workspace gives the turn the
            # worktree of that session, and Hermes runs the turn's commands in it.
            repository = data.parent / 'repository'
            repository.mkdir()
            for command in (['init', '-q', '-b', 'main'], ['-c', 'user.name=T', '-c', 'user.email=t@example.com',
                                                            'commit', '-q', '--allow-empty', '-m', 'Start']):
                subprocess.run(['git', '-C', str(repository), *command], check=True)
            REPOSITORY['path'] = str(repository)
            with service.transaction():
                owner = service._get('tasks', audit['id'])
                owner['status'] = 'working'
                service._put('tasks', owner)
            item = service._register_work(audit['id'], dict(key='export', title='Export account data',
                                                            activity='Preparing the export', status='working'))
            with service.transaction():
                owner = service._get('tasks', audit['id'])
                owner['status'] = 'completed'
                service._put('tasks', owner)
            question = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages',
                                      {'text': FIX, 'mode': 'queue', 'work_artifact_id': item['id']}, str(uuid4()), 'phone')
            service._run_task(question['id'])
            replies = [m['text'] for m in service._task_messages(question['id']) if m['role'] == 'assistant' and m['kind'] == 'text']
            self.assertEqual((service._get('tasks', question['id'])['status'], replies), ('completed', [FIXED]), diagnostics())
            final = next(request for request in reversed(Model.requests) if FIX in json.dumps(request['messages']))
            results = [message.get('content') for message in final['messages'] if message.get('role') == 'tool']
            binding = json.loads(results[-2])
            binding = binding.get('result', binding) if isinstance(binding, dict) else binding
            binding = json.loads(binding) if isinstance(binding, str) else binding
            self.assertTrue(Path(binding['path']).is_dir(), diagnostics())
            # Coconut's registry of worktrees holds it for the outcome of the work session, as for a worker.
            from coconut.work_capacity import execution_key
            saved = json.loads((data / 'workspaces/worktrees.json').read_text())
            self.assertEqual([entry['path'] for entry in saved.values()
                              if entry['outcome'] == execution_key(service._get('tasks', question['id']))],
                             [binding['path']], saved)
            self.assertIn(binding['path'], json.dumps(results[-1]), (binding, results[-1]))

            # A background process of a chat turn. Hermes runs its own turn for the completion, outside a Coconut
            # turn, and its reply reaches the bot's chat as a message of the bot.
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': START}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            self.assertEqual(service._get('tasks', task['id'])['status'], 'completed', diagnostics())
            deadline = time.monotonic() + 120
            outside = []
            while FINISHED not in [m['text'] for m in outside] and time.monotonic() < deadline:
                time.sleep(.5)
                with service.lock:
                    outside = [m for m in service._all('messages')
                               if m['bot_id'] == bot['id'] and m.get('_gateway_message_id')]
            self.assertIn(FINISHED, [m['text'] for m in outside], diagnostics())
            with service.lock:
                finished = next(m for m in outside if m['text'] == FINISHED)
                holder = service._get('tasks', finished['task_id'])
            self.assertEqual((holder['status'], holder['bot_id'], finished['role']),
                             ('completed', bot['id'], 'assistant'))
            self.assertTrue(holder.get('_gateway_outside'))
            self.assertNotIn('refused a message outside a Coconut turn', log.read_text())

            # A routine of the bot: the gateway's cron fires it, and the run is a Coconut turn in the bot's chat.
            job = subprocess.run([PYTHON, '-c', 'import json, sys; sys.path.insert(0, sys.argv[1]); '
                                  'from cron.jobs import create_job; '
                                  'print(json.dumps(create_job("Run the routine check.", "every 1h", '
                                  'name="Routine check", deliver="local")["id"]))', SOURCE],
                                 env=environment, capture_output=True, text=True, timeout=120)
            self.assertEqual(job.returncode, 0, job.stderr[-4000:])
            # The gateway fires the routine, so a pass of Coconut claims none.
            service.provider.native_settings = routine_settings
            service._native_cron_next = 0
            cursor = service._cursor()
            service.tick_native_routines()
            self.assertNotIn('routines.unavailable', [e['type'] for e in service.events(cursor)['events']])
            with service.lock:
                routine = next(r for r in service._all('routines') if r['_native_id'] == json.loads(job.stdout))
                self.assertFalse(any(t.get('routine_id') == routine['id'] for t in service._all('tasks')))
            # The person runs it now. Hermes's trigger asks the gateway's cron to run it at its next tick, and the run
            # takes the task that waits for it.
            queued = service.mutate('POST', f'/v1/routines/{routine["id"]}/run', {}, str(uuid4()), 'phone')
            del service.provider.native_settings
            self.assertEqual(queued['status'], 'waiting')
            deadline = time.monotonic() + 240
            routine_task = None
            while time.monotonic() < deadline:
                with service.lock:
                    routine_task = next((t for t in service._all('tasks') if t.get('_gateway_cron')
                                         and t['status'] not in ('working', 'queued')), None)
                if routine_task is not None:
                    break
                time.sleep(1)
            self.assertIsNotNone(routine_task, diagnostics())
            self.assertEqual(routine_task['id'], queued['id'])
            self.assertEqual((routine_task['status'], routine_task['title']), ('completed', 'Routine check'), diagnostics())
            with service.lock:
                routine_replies = [m['text'] for m in service._task_messages(routine_task['id'])
                                   if m['role'] == 'assistant' and m['kind'] == 'text']
            self.assertEqual(routine_replies, [REPLY], diagnostics())
            self.assertFalse(any(t.get('_native_job') for t in service._all('tasks')))
            process = service.gateway_process
        finally:
            service.close()
            server.shutdown()
            server.server_close()
        # The gateway stops with the service.
        self.assertIsNotNone(process.poll())


if __name__ == '__main__':
    unittest.main()
