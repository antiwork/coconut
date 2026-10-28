"""A chat message goes through a real `hermes gateway run` and its reply reaches Coconut.

The turn continues the bot's native conversation from an earlier worker turn, gets the coconut tools, and streams. A
routine of the bot fires in the gateway's cron, and its run is a Coconut turn in the bot's chat.

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


class Model(BaseHTTPRequestHandler):
    """Answer each chat completion with REPLY, streamed or not."""
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
        data = dict(id='stub', created=int(time.time()), model=request['model'])
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if request.get('stream') else 'application/json')
        self.end_headers()
        if request.get('stream'):
            for index, delta in enumerate((dict(role='assistant', content=REPLY[:6]), dict(content=REPLY[6:]))):
                data.update(object='chat.completion.chunk',
                            choices=[dict(index=0, delta=delta, finish_reason='stop' if index else None)])
                self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            data.update(object='chat.completion', usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                        choices=[dict(index=0, message=dict(role='assistant', content=REPLY), finish_reason='stop')])
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
            service.mutate('PUT', '/v1/settings/chat-runtime', {'chat_runtime': 'gateway'}, str(uuid4()), 'phone')
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

            # A routine of the bot: the gateway's cron fires it, and the run is a Coconut turn in the bot's chat.
            job = subprocess.run([PYTHON, '-c', 'import json, sys; sys.path.insert(0, sys.argv[1]); '
                                  'from cron.jobs import create_job; '
                                  'print(json.dumps(create_job("Run the routine check.", "every 1m", '
                                  'name="Routine check", deliver="local")["id"]))', SOURCE],
                                 env=environment, capture_output=True, text=True, timeout=120)
            self.assertEqual(job.returncode, 0, job.stderr[-4000:])
            # The gateway ticks this profile, so Coconut claims none of its routines.
            self.assertIn(bot['id'], service.gateway_ticked_bots())
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
