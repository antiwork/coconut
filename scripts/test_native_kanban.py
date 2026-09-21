#!/usr/bin/env python3
"""Exercise Coconut, native Kanban, workspaces, tools, and Inbox without external writes."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from coconut.providers import HermesProvider
from coconut.service import Service
from coconut.work_workspace import git


def check(source, python):
    failures, calls = [], []
    barrier = threading.Barrier(2)
    with tempfile.TemporaryDirectory(prefix='coconut-kanban-') as temporary:
        home = Path(temporary).resolve()
        repo = home / 'repo'
        repo.mkdir()
        git(repo, 'init')
        git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'Initial')
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                try:
                    request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if 'messages' not in request:
                        self.send_error(400, 'The local model uses chat completions.')
                        return
                    messages = request['messages']
                    results = [m for m in messages if m.get('role') == 'tool']
                    if len(results) > 12:
                        raise AssertionError('Unexpected tool retry: ' + results[-1]['content'][:1000])
                    chat = 'create one native kanban task before starting it' in json.dumps(messages[:1])
                    if chat:
                        last_user = max(i for i, m in enumerate(messages) if m['role'] == 'user')
                        recent = messages[last_user:]
                        if any(m.get('role') == 'tool' for m in recent):
                            name, arguments = None, None
                        else:
                            name, arguments = 'kanban_create', dict(title=messages[last_user]['content'][-120:],
                                body='Write proof.txt in an isolated worktree. Ask whether to accept the result. Verify the file after the answer.',
                                assignee='default')
                    elif not results:
                        barrier.wait(30)
                        name, arguments = 'coconut_workspace', {'repository': str(repo)}
                    elif 'human-accepted-probe' in json.dumps([m for m in messages if m.get('role') == 'user']):
                        if any('"ok": true' in m.get('content', '') and '"run_id":' in m.get('content', '') for m in results):
                            name, arguments = None, None
                        elif not any('workspace-restored' in m.get('content', '') for m in results):
                            name, arguments = 'terminal', {'command': "test -f proof.txt && printf 'workspace-restored\\n'"}
                        else:
                            name, arguments = 'kanban_complete', {'summary': 'Verified proof.txt in the isolated worktree after the person accepted the result.'}
                    elif len(results) == 1:
                        binding = json.loads(results[-1]['content'])
                        assert 'path' in binding, binding
                        name, arguments = 'terminal', {'command': "printf 'isolated change\\n' > proof.txt"}
                    elif len(results) == 2:
                        assert any('native process(action="wait")' in str(m.get('content')) for m in messages[:1])
                        name, arguments = 'terminal', dict(command="sleep 0.2; printf 'native-child-finished'",
                            background=True, notify=True)
                    elif len(results) == 3:
                        process = json.loads(results[-1]['content'])
                        assert process.get('notify_on_complete') is False, process
                        assert process.get('notify_unsupported'), process
                        name, arguments = 'process_manage', dict(action='wait', session_id=process['session_id'], timeout=5)
                    else:
                        process = json.loads(results[-1]['content'])
                        assert process.get('status') == 'exited' and process.get('exit_code') == 0, process
                        assert 'native-child-finished' in process.get('output', ''), process
                        name, arguments = 'coconut_ask', {'title': 'Accept the isolated result?',
                            'context': 'The local worker wrote proof.txt in its own Git worktree.',
                            'options': [{'id': 'accept', 'label': 'human-accepted-probe', 'explanation': 'Complete this local check.'}]}
                    calls.append(name or 'finish')
                    if os.environ.get('COCONUT_TEST_TRACE'):
                        print(json.dumps(dict(chat=chat, call=name, results=len(results), last=results[-1] if results else None)), flush=True)
                    if name:
                        offered = {t.get('function', t).get('name') for t in request.get('tools', [])}
                        if name not in offered:
                            arguments, name = {'calls': [{'name': name, 'arguments': arguments}]}, 'tool_call'
                        message = dict(role='assistant', content=None, tool_calls=[dict(id='call_' + str(uuid4()), type='function',
                            function=dict(name=name, arguments=json.dumps(arguments)))])
                        finish = 'tool_calls'
                    else:
                        message, finish = dict(role='assistant', content='The local check is complete.'), 'stop'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream' if request.get('stream') else 'application/json')
                    self.end_headers()
                    if request.get('stream'):
                        if name: message['tool_calls'][0]['index'] = 0
                        data = dict(id='probe', object='chat.completion.chunk', created=int(time.time()), model='kanban-probe',
                            choices=[dict(index=0, delta=message, finish_reason=finish)])
                        self.wfile.write(('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode())
                    else:
                        self.wfile.write(json.dumps(dict(id='probe', object='chat.completion', created=int(time.time()), model='kanban-probe',
                            choices=[dict(index=0, message=message, finish_reason=finish)], usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))).encode())
                except Exception as error:
                    failures.append(str(error))
                    self.send_error(500)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        address = f'http://127.0.0.1:{server.server_port}/v1'
        class Catalog:
            root = home / 'default'
            def resolve_runtime(self, *_, **__):
                return dict(provider='custom', api_mode='chat_completions', base_url=address, api_key='local-test')
        Catalog.root.mkdir()
        (Catalog.root / 'config.yaml').write_text('{}')
        class Provider(HermesProvider):
            def migrate_model_default(self, *_): pass
            def subscribe_model_catalog(self, *_): pass
            def native_settings(self, *_args, **_kwargs): return {'routines': []}
            def observe_bot(self, bot): return bot
            def model_catalog(self): return {'default': self.resolve_task_model({}, {}), 'models': []}
            def resolve_model(self, bot, catalog=None): return self.resolve_task_model(bot, {})
            def resolve_task_model(self, bot, task):
                return dict(model='kanban-probe', provider='custom', base_url=address, reasoning_effort=None)
        provider = Provider(home / 'data', python=python, hermes_root=source, catalog=Catalog())
        service = Service(home / 'data', provider, start_workers=False)
        def write(path, data, key=None):
            return service.mutate('POST', path, data, key or str(uuid4()))
        def settle(predicate):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                service._dispatch_pending()
                if failures: raise AssertionError(failures)
                if predicate(): return
            raise AssertionError({'tasks': [(t['status'], t.get('summary')) for t in service._all('tasks')], 'calls': calls[:12]})
        try:
            bot = write('/v1/bots', {'name': 'Local integration check', 'role': ''})
            service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 2}, str(uuid4()))
            tasks = [write('/v1/bots/' + bot['id'] + '/messages', {'text': 'Check local worktree ' + str(i), 'mode': 'queue'}) for i in range(2)]
            settle(lambda: len(service._all('decisions')) == 2 and all(n['status'] == 'review' for n in service.kanban.tasks.values()))
            chats = tasks
            assert all(service._get('tasks', t['id']).get('_chat_session') for t in chats)
            assert all(not service._get('tasks', t['id']).get('_execution_session_id') for t in chats)
            tasks = [t for t in service._all('tasks') if t.get('_native_outcome_id')]
            assert len(tasks) == 2
            assert len({t['_kanban_claim']['pid'] for t in tasks}) == 2
            bindings = json.loads((home / 'data/workspaces/worktrees.json').read_text())
            assert len(bindings) == 2
            assert len({b['path'] for b in bindings.values()}) == 2
            for binding in bindings.values():
                assert (Path(binding['path']) / 'proof.txt').read_text() == 'isolated change\n'
            assert not (repo / 'proof.txt').exists()
            # Replace both idle workers. The next turn must restore cwd without another workspace call.
            for process in list(provider._kanban_runtimes.values()):
                provider._terminate(process)
            # The same approval request can reach the service twice after a lost phone response.
            for card in service._all('decisions'):
                path = '/v1/decisions/' + card['id'] + '/answer'
                payload = {'option_id': 'accept', 'revision': card['revision']}
                key = str(uuid4())
                result = write(path, payload, key)
                assert write(path, payload, key) == result
            settle(lambda: all(t['status'] == 'completed' for t in service._all('tasks')))
            settle(lambda: all(n['status'] == 'done' for n in service.kanban.tasks.values()))
            assert calls.count('kanban_create') == 2, calls
            assert calls.count('kanban_complete') == 2, calls
            assert calls.count('terminal') == 6, calls
            assert calls.count('process_manage') == 2, calls
            assert not failures, failures
            print(json.dumps(dict(passed=True, native_workers=2, worktrees=2, isolated_tool_writes=2,
                                  review_waits=2, restored_workspaces=2, duplicate_answers='handled once', completed_tasks=2)))
        finally:
            service.close()
            server.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--python', required=True)
    args = parser.parse_args()
    check(args.source.resolve(), args.python)
