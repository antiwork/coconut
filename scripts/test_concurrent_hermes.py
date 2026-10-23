#!/usr/bin/env python3
"""Check parallel Coconut turns against an installed, unchanged Hermes runtime."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from coconut.providers import HermesProvider


def check(source, python):
    barrier = threading.Barrier(2)
    observations, failures = [], []
    peer_id = str(uuid4())
    cancellation = {}
    unconfirmed = {}

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if 'messages' not in request:
                    self.send_error(400, 'This test server supports chat completions only.')
                    return
                messages = request['messages']
                text = json.dumps([m for m in messages if m.get('role') == 'user'])
                owner = re.findall(r'parallel-probe-([a-f0-9-]{36})', text)[-1]
                results = [m for m in messages if m.get('role') == 'tool']
                if owner in cancellation:
                    entered, release = cancellation[owner]
                    entered.set()
                    assert release.wait(20), 'Cancellation check did not release the model.'
                    message, finish = dict(role='assistant', content=owner), 'stop'
                elif not results:
                    barrier.wait(20)
                    arguments = {'url': 'https://github.com/antiwork/coconut/pull/1'}
                    name = 'coconut_track_pr'
                    offered = {tool.get('function', tool).get('name') for tool in request.get('tools', [])}
                    if name not in offered:
                        arguments = {'calls': [{'name': name, 'arguments': arguments}]}
                        name = 'tool_call'
                    message = dict(role='assistant', content=None, tool_calls=[dict(id='call_' + owner, type='function',
                        function=dict(name=name, arguments=json.dumps(arguments)))])
                    finish = 'tool_calls'
                else:
                    raw = results[-1]['content']
                    if not isinstance(raw, str) or not raw.startswith('{'):
                        raise AssertionError('Unexpected native tool result: ' + repr(raw)[:500])
                    result = json.loads(raw)
                    if len(results) == 1:
                        assert result['owner'] == owner, 'A tool result crossed between work items.'
                        message = dict(role='assistant', content=None, tool_calls=[dict(id='relay_' + owner, type='function',
                            function=dict(name='tool_call', arguments=json.dumps({'calls': [{'name': 'coconut_message_bot',
                                'arguments': {'target': peer_id, 'message': 'Check ' + owner}}]})))])
                        finish = 'tool_calls'
                    else:
                        assert result['task_id'] == owner, 'A relay receipt crossed between work items.'
                        observations.append(owner)
                        message, finish = dict(role='assistant', content=owner), 'stop'
                self.send_response(200)
                streaming = request.get('stream', False)
                self.send_header('Content-Type', 'text/event-stream' if streaming else 'application/json')
                self.end_headers()
                if streaming:
                    if message.get('tool_calls'):
                        message['tool_calls'][0]['index'] = 0
                    chunk = dict(id='probe', object='chat.completion.chunk', created=int(time.time()), model='parallel-test',
                                 choices=[dict(index=0, delta=message, finish_reason=finish)])
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode())
                else:
                    self.wfile.write(json.dumps(dict(id='probe', object='chat.completion', created=int(time.time()), model='parallel-test',
                        choices=[dict(index=0, message=message, finish_reason=finish)], usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))).encode())
            except Exception as error:
                failures.append(type(error).__name__ + ': ' + str(error))
                self.send_error(500)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f'http://127.0.0.1:{server.server_port}/v1'
    with tempfile.TemporaryDirectory(prefix='coconut-concurrency-') as temporary:
        home = Path(temporary)
        class Catalog:
            root = home / 'default'
            def resolve_runtime(self, *_args, **_kwargs):
                return dict(provider='custom', api_mode='chat_completions', base_url=address, api_key='local-test')
            def worker_limit(self):
                return None
        Catalog.root.mkdir()
        (Catalog.root / 'config.yaml').write_text('{}')
        class Provider(HermesProvider):
            def observe_bot(self, bot): return bot
            def resolve_task_model(self, bot, task):
                return dict(model='parallel-test', provider='custom', base_url=address, reasoning_effort=None)
        provider = Provider(home / 'data', python=python, hermes_root=source, catalog=Catalog())
        bot = dict(id=str(uuid4()), session_id=str(uuid4()), name='Concurrency check', instructions='Complete the current check.')
        tasks = [dict(id=str(uuid4()), bot_id=bot['id'], title='Check concurrent routing', _execution_session_id=str(uuid4()), _conversation_epoch=str(uuid4()),
                      _available_bots=[dict(id=peer_id, name='Test peer', role='Checks')]) for _ in range(2)]
        outputs = {}
        def run(task):
            events = []
            def emit(kind, payload):
                events.append((kind, payload))
                if unconfirmed.get(task['id']) == kind:
                    return {'status': 'unconfirmed'}
                if kind == 'artifact': return {'owner': task['id']}
                if kind == 'native_bot_message':
                    assert payload['text'] == 'Check ' + task['id']
                    return dict(status='queued', task_id=task['id'], target_bot_id=peer_id, thread_id=task['id'], delivery_id=payload['native_envelope_id'])
                if kind == 'native_call': return {'status': 'recorded', 'id': str(uuid4())}
                if kind == 'native_result': return {'status': 'recorded'}
            try:
                history = [dict(id=str(uuid4()), role='user', task_id=task['id'], text='parallel-probe-' + task['id'])]
                provider.start_task(task, bot, history, emit)
                outputs[task['id']] = events
            except Exception as error:
                failures.append(type(error).__name__ + ': ' + str(error))
        try:
            threads = [threading.Thread(target=run, args=(task,), daemon=True) for task in tasks]
            for thread in threads: thread.start()
            for thread in threads: thread.join(75)
            assert not any(t.is_alive() for t in threads), 'Concurrent native turns did not finish.'
            assert not failures, (failures, outputs)
            assert set(observations) == {t['id'] for t in tasks}, outputs
            assert len(provider._runtimes) == 1
            for task in tasks:
                assert any(kind == 'text_delta' and data['text'] == task['id'] for kind, data in outputs[task['id']]), outputs
            envelopes = list((home / 'data/bots' / bot['id'] / 'home/bot_relay/claimed').glob('*.json'))
            assert len(envelopes) == 2, 'Each work item must produce one native relay envelope.'
            sys.path.insert(0, str(source))
            profile = home / 'data/bots' / bot['id'] / 'home'
            os.environ['HERMES_HOME'] = str(profile)
            from hermes_constants import set_hermes_home_override, reset_hermes_home_override
            from gateway.session_context import set_session_vars, clear_session_vars
            from tools import async_delegation as native
            from coconut.native_conversation import session_root
            token = set_hermes_home_override(profile)
            task = tasks[0]
            session_tokens = set_session_vars(session_id=session_root(bot, execution=task['_execution_session_id'], epoch=task['_conversation_epoch']), session_key=task['id'])
            try:
                result = native.dispatch_async_delegation(goal='Check delivery', context=None, toolsets=None,
                    role='general', model=None, session_key=task['id'], runner=lambda: {'status': 'completed', 'summary': 'Delivery verified'})
                item = dict(id='receipt', task_id=task['id'], execution_session_id=task['_execution_session_id'], epoch=task['_conversation_epoch'],
                            delegation_ids=[result['delegation_id']])
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    completion = provider.process_completions(bot, [item])
                    if completion and completion[0].get('text'):
                        break
                    time.sleep(.05)
                assert completion and 'Delivery verified' in completion[0].get('text', ''), completion
                assert provider.process_completions(bot, [item | {'execution_session_id': tasks[1]['_execution_session_id'], 'task_id': tasks[1]['id']}]) == []
                assert provider.process_completions(bot, [item | {'acknowledge': True}])[0]['acknowledged']
            finally:
                clear_session_vars(session_tokens)
                reset_hermes_home_override(token)
            runtime = next(iter(provider._runtimes.values()))
            for failure_kind in ('native_result', 'native_bot_message'):
                probes = [dict(id=str(uuid4()), bot_id=bot['id'], title='Check receipt isolation',
                               _execution_session_id=str(uuid4()), _available_bots=tasks[0]['_available_bots']) for _ in range(2)]
                unconfirmed[probes[0]['id']] = failure_kind
                threads = [threading.Thread(target=run, args=(candidate,), daemon=True) for candidate in probes]
                for thread in threads: thread.start()
                for thread in threads: thread.join(20)
                assert not any(thread.is_alive() for thread in threads), 'Unconfirmed receipt did not end its turn.'
                assert not failures, failures
                assert any(kind == 'failed' for kind, _ in outputs[probes[0]['id']]), outputs
                assert not any(kind == 'text_delta' for kind, _ in outputs[probes[0]['id']]), outputs
                assert any(kind == 'completed' for kind, _ in outputs[probes[1]['id']]), outputs
                assert next(iter(provider._runtimes.values())) is runtime, 'An uncertain receipt stopped the peer runtime.'
            cancelled_tasks = [dict(id=str(uuid4()), bot_id=bot['id'], title='Check cancellation',
                                    _execution_session_id=str(uuid4())) for _ in range(2)]
            for candidate in cancelled_tasks:
                cancellation[candidate['id']] = (threading.Event(), threading.Event())
            threads = [threading.Thread(target=run, args=(candidate,), daemon=True) for candidate in cancelled_tasks]
            for thread in threads: thread.start()
            for entered, _ in cancellation.values():
                assert entered.wait(20), 'Both tasks must start before cancellation.'
            provider.cancel_task(cancelled_tasks[0]['id'])
            time.sleep(1.2)
            assert next(iter(provider._runtimes.values())) is runtime, 'Cancellation replaced the shared runtime.'
            cancellation[cancelled_tasks[1]['id']][1].set()
            threads[1].join(10)
            assert not threads[1].is_alive(), 'Cancellation stopped the peer task.'
            cancellation[cancelled_tasks[0]['id']][1].set()
            threads[0].join(10)
            assert not threads[0].is_alive(), 'The cancelled task did not stop.'
            assert not failures, failures
            assert not any(kind == 'text_delta' for kind, _ in outputs[cancelled_tasks[0]['id']]), outputs
            assert any(kind == 'completed' for kind, _ in outputs[cancelled_tasks[1]['id']]), outputs
            print(json.dumps(dict(passed=True, concurrent_turns=2, native_runtimes=1, tool_results_isolated=True,
                                  native_relay_envelopes=2, delegation_completion=True, cancellation_isolated=True, restart_preserves_peers=True, uncertain_receipts_isolated=True)))
        finally:
            for _, release in cancellation.values(): release.set()
            provider.close()
            server.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--python', required=True)
    args = parser.parse_args()
    check(args.source, args.python)
