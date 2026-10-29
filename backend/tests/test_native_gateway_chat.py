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
# A chat turn that asks on a card, and the turn that continues after the answer.
ASK = 'Ask me on a card.'
ASKED = 'I asked you on a card.'
RESUMED = 'Continue the original task using this recorded answer.'
SHIPPED = 'Shipping now.'
CARD = dict(title='Ship the release?', context='The build passed and the notes are ready.',
            options=[dict(id='ship', label='Ship it', explanation=''), dict(id='wait', label='Wait a day', explanation='')])


def answer(request):
    """The assistant message for a request: a terminal call for START, FINISHED for the completion notice of the
    background process, and REPLY otherwise."""
    messages = request.get('messages') or []
    users = [index for index, message in enumerate(messages) if message.get('role') == 'user']
    last = json.dumps(messages[users[-1]].get('content')) if users else ''
    if OUTPUT in last:
        return dict(role='assistant', content=FINISHED)
    if ATTACHED in last:
        return dict(role='assistant', content=READ)
    if REACTION in last:
        return dict(role='assistant', content='[SILENT]')
    if RESUMED in last:
        return dict(role='assistant', content=SHIPPED)
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
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            data.update(object='chat.completion', usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                        choices=[dict(index=0, message=message, finish_reason=finish)])
            self.wfile.write(json.dumps(data).encode())


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

        os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
        service = Service(data, Provider(data), start_workers=False, hermes_home=home)
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
            # The streamed pieces and the final send make one reply.
            self.assertEqual(replies, [REPLY], diagnostics())
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

            # A card of coconut_ask ends the gateway turn. The answer continues the conversation in a later turn.
            task = service.mutate('POST', f'/v1/bots/{bot["id"]}/messages', {'text': ASK}, str(uuid4()), 'phone')
            service._run_task(task['id'])
            self.assertEqual(service._get('tasks', task['id'])['status'], 'needs_you', diagnostics())
            card = next(d for d in service._all('decisions') if d['task_id'] == task['id'])
            self.assertEqual((card['title'], card['status']), (CARD['title'], 'pending'), diagnostics())
            service.mutate('POST', f'/v1/decisions/{card["id"]}/answer', {'option_id': 'ship', 'revision': 1},
                           str(uuid4()), 'phone')
            service._run_task(task['id'])
            replies = [m['text'] for m in service._task_messages(task['id']) if m['role'] == 'assistant' and m['kind'] == 'text']
            self.assertEqual((service._get('tasks', task['id'])['status'], replies[-1:]), ('completed', [SHIPPED]),
                             diagnostics())
            self.assertNotIn(ASKED, replies, diagnostics())

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
                                  'print(json.dumps(create_job("Run the routine check.", "every 1m", '
                                  'name="Routine check", deliver="local")["id"]))', SOURCE],
                                 env=environment, capture_output=True, text=True, timeout=120)
            self.assertEqual(job.returncode, 0, job.stderr[-4000:])
            # The gateway fires the routine, so a pass of Coconut claims none.
            service._native_cron_next = 0
            service.tick_native_routines()
            self.assertFalse(service._native_cron_error)
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
