#!/usr/bin/env python3
"""Exercise completed/active/lost routine tools through a real Hermes worker."""
import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from coconut.model_catalog import HermesModelCatalog
from coconut.providers import HermesProvider, FixtureProvider
from coconut.service import Service


def check(source, python):
    with tempfile.TemporaryDirectory(prefix='routine-continuation-', dir=Path(source).resolve().parent) as temporary:
        root = Path(temporary).resolve()
        assert root.is_relative_to(Path(source).resolve().parent)
        proof = root / 'proof.txt'
        proof.write_text('Exact routine continuation proof')
        errors, tool_results = [], []
        hold_request, request_entered, release_request = threading.Event(), threading.Event(), threading.Event()
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                if not self.path.endswith('/chat/completions'):
                    self.send_error(404)
                    return
                try:
                    request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if hold_request.is_set():
                        request_entered.set()
                        assert release_request.wait(30), 'Migration did not release the worker'
                    messages = request['messages']
                    last_user = max(i for i, row in enumerate(messages) if row['role'] == 'user')
                    results = [row for row in messages[last_user:] if row['role'] == 'tool']
                    if results:
                        tool_results.extend(results)
                        assert 'Exact routine continuation proof' in str(results), results
                        message, finish = dict(role='assistant', content='Verified continuation tool.'), 'stop'
                    else:
                        message = dict(role='assistant', content=None, tool_calls=[dict(
                            id='routine-read', type='function', function=dict(name='read_file',
                            arguments=json.dumps(dict(path=str(proof)))))])
                        finish = 'tool_calls'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream' if request.get('stream') else 'application/json')
                    self.end_headers()
                    data = dict(id='routine-probe', created=int(time.time()), model=request['model'])
                    if request.get('stream'):
                        if message.get('tool_calls'): message['tool_calls'][0]['index'] = 0
                        data.update(object='chat.completion.chunk', choices=[dict(index=0, delta=message, finish_reason=finish)])
                        self.wfile.write(('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode())
                    else:
                        data.update(object='chat.completion', choices=[dict(index=0, message=message, finish_reason=finish)],
                                    usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
                        self.wfile.write(json.dumps(data).encode())
                except Exception as error:
                    errors.append(str(error))
                    self.send_error(500)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        address = f'http://127.0.0.1:{server.server_port}/v1'
        data = root / 'data'
        catalog = HermesModelCatalog(data, python=python, source=source, home=root)
        assert catalog.root.resolve().is_relative_to(root)
        assert catalog.private_home.resolve().is_relative_to(root)
        catalog.root.mkdir(parents=True, exist_ok=True)
        (catalog.root / 'config.yaml').write_text('{}')
        class Provider(HermesProvider):
            def observe_bot(self, bot): return bot
            def resolve_model(self, bot, catalog=None):
                return dict(model='routine-probe', provider='custom', base_url=address, reasoning_effort=None)
        catalog.resolve_runtime = lambda *args, **kwargs: dict(provider='custom', api_mode='chat_completions',
                                                               base_url=address, api_key='local-test')
        provider = Provider(data, python=python, hermes_root=source, catalog=catalog)
        # One data folder: the routine receipts of a bot stay in <data>/bots/<uuid>/runs.
        service = Service(data, FixtureProvider(data), start_workers=False)
        bot = service.mutate('POST', '/v1/bots', {'name': 'Probe'}, str(uuid4()))
        bot.update(instructions='Read the requested file.')
        # The worker and its settings run in the Hermes profile that the service created for the bot.
        provider.profile_rows = catalog.profile_rows = service.profiles.rows
        provider.environment(bot['id'])
        def native(operation, **kwargs):
            return provider.native_settings(operation, [bot], bot_id=bot['id'], **kwargs)
        execution_id = str(uuid4())
        def run(job, finished, allowed, provenance=None):
            task = dict(id=execution_id, bot_id=bot['id'], title='Read the requested file.',
                        _native_job=job, _native_job_finished=finished, _conversation_epoch=str(uuid4()),
                        _native_job_completion=provenance)
            if provenance and provenance.get('continuation'):
                continuation = provenance['continuation']
                task.update(id=continuation['task_id'], _native_turn_id=continuation['turn_id'],
                            _native_outcome_id=continuation['native_task_id'])
            original = copy.deepcopy(task)
            events = []
            def emit(kind, payload):
                events.append((kind, payload))
                if kind in ('native_call', 'native_result'):
                    return dict(status='recorded', id='receipt')
            provider.start_task(task, bot, [], emit)
            assert task == original, 'The continuation changed execution provenance'
            assert not errors, errors
            receipts = [(kind, value) for kind, value in events if kind == 'native_result']
            if allowed:
                assert len(receipts) == 1, events
                assert 'Exact routine continuation proof' in str(receipts), receipts
                assert any(kind == 'completed' for kind, _ in events), events
            else:
                assert not any(kind in ('native_call', 'native_result', 'completed') for kind, _ in events), events
                failures = [value for kind, value in events if kind == 'failed']
                assert len(failures) == 1 and failures[0]['error_code'] == 'native_turn_failed', events
        try:
            routine = native('routine_create', request_key='probe', changes=dict(name='Continuation probe',
                prompt='Read the requested file.', cron='every 1h', enabled=True))
            identity = routine['_native_id']
            job = native('routine_claim', native_id=identity, manual=True)['job']
            owner = job['fire_claim']['by']
            service.provider = provider
            with service.transaction():
                legacy_task = service.claim_native_routine(routine)
                stored = service._get('tasks', legacy_task['id'])
                stored['status'] = 'working'
                service._put('tasks', stored)
            service.emit(stored['id'], 'completed', {'summary': 'Routine succeeded'})
            run(job, False, True)
            assert native('routine_heartbeat', native_id=identity, owner=owner)['ok']
            assert native('routine_finish', native_id=identity, owner=owner, success=True, output='Done')['ok']
            run(job, True, True)
            # Reproduce a pre-repair completion, which lacks the occurrence receipt.
            from coconut.native_routine_receipts import completion_id
            receipt = data / 'bots' / bot['id'] / 'runs' / (completion_id(identity, owner) + '.json')
            receipt.unlink()
            jobs_path = service.profiles.home(bot) / 'cron/jobs.json'
            jobs_data = json.loads(jobs_path.read_text())
            for row in jobs_data['jobs']:
                if row['id'] == identity:
                    row.pop('_coconut_completion', None)
            jobs_path.write_text(json.dumps(jobs_data))
            run(job, True, False)
            service.refresh_native_routines = lambda: None
            service.tick_native_routines()
            stored = service._get('tasks', legacy_task['id'])
            assert stored['_native_job_finished'], stored
            provenance = service.native_routine_completion_input(stored)
            assert provenance == dict(job_id=identity, owner=owner, success=True), provenance
            run(job, True, True, provenance)
            # Exercise the actual service-to-provider request, including private fields.
            with service.transaction():
                stored['status'] = 'queued'
                service._advance_input(stored)
                service._put('tasks', stored)
            before = len(tool_results)
            service._run_task(stored['id'])
            assert len(tool_results) > before, service._get('tasks', stored['id'])
            assert service._get('tasks', stored['id'])['status'] == 'completed'

            later = native('routine_claim', native_id=identity, manual=True)['job']
            assert later['fire_claim']['by'] != owner
            run(job, True, True, provenance)
            assert receipt.exists(), 'Validated legacy completion proof was not persisted'
            with service.transaction():
                failed_task = service.claim_native_routine(routine)
                failed_owner = service._get('tasks', failed_task['id'])
                failed_owner.update(status='working', _native_outcome_id='t_failed_probe',
                    _execution_session_id=failed_owner['id'],
                    _conversation_epoch=service._setting('conversation_epoch'))
                service._put('tasks', failed_owner)
            service.emit(failed_owner['id'], 'failed', {'summary': 'Original occurrence failed.'})
            assert native('routine_finish', native_id=identity, owner=later['fire_claim']['by'], success=False)['ok']
            run(later, True, False)
            # The admission route uses a native-board snapshot fixture. The cron
            # store, service receipts, provider and Hermes worker are real.
            from unittest.mock import Mock
            with service.transaction():
                failed_owner = service._get('tasks', failed_owner['id'])
                failed_owner['_native_job_finished'] = True
                service._put('tasks', failed_owner)
                requester = service._send(bot, {'text': 'Continue the failed check.', 'mode': 'queue'}, human_authored=False)
                requester = service._get('tasks', requester['id'])
                requester['status'] = 'working'
                service._put('tasks', requester)
            service.kanban = Mock(tasks={failed_owner['id']: dict(id='t_failed_probe', status='blocked')})
            try:
                service.emit(requester['id'], 'work_continue', dict(task_id='t_failed_probe', message='Finish the existing check.'))
                failed_proof = service.native_routine_completion_input(service._get('tasks', failed_owner['id']))
                before_failure_recovery = jobs_path.read_bytes()
                run(later, True, True, failed_proof)
                assert jobs_path.read_bytes() == before_failure_recovery, 'Continuation refired or rewrote the failed occurrence'
            finally:
                service.kanban = None
            run(job, True, True)
            forged = copy.deepcopy(job)
            forged['fire_claim']['by'] = 'lost-owner'
            run(forged, True, False)
            # Migrate existing saved proof through the real service/provider boundary.
            unrelated = receipt.parent / 'completed-unrelated.json'
            unrelated.write_text('{"unrelated": true}')
            from coconut.routine_coordinator import NativeRoutines
            hold_request.set()
            execution_id = stored['id']
            worker_errors = []
            def inflight():
                try:
                    run(job, True, True)
                except BaseException as error:
                    worker_errors.append(error)
            worker = threading.Thread(target=inflight)
            service.running[stored['id']] = worker
            worker.start()
            try:
                assert request_entered.wait(30), 'Continuation never entered the model'
                NativeRoutines.refresh_native_routines(service)
                assert receipt.exists(), 'Migration revoked an in-flight request'
                next_job = native('routine_claim', native_id=identity, manual=True)['job']
                assert native('routine_finish', native_id=identity, owner=next_job['fire_claim']['by'], success=False)['ok']
            finally:
                release_request.set()
                worker.join(30)
                assert not worker.is_alive(), 'Continuation worker did not finish'
                service.running.pop(stored['id'], None)
            assert not worker_errors, worker_errors
            NativeRoutines.refresh_native_routines(service)
            assert not receipt.exists(), 'Saved completion file was not retired'
            assert unrelated.read_text() == '{"unrelated": true}'
            stored = service._get('tasks', legacy_task['id'])
            durable = service.native_routine_completion_input(stored)
            assert durable == dict(job_id=identity, owner=owner, success=True, native_confirmed=True), durable
            run(job, True, True, durable)
            run(forged, True, False, durable)
            run(later, True, False, durable)
            print(json.dumps(dict(passed=True, completion_retention=True, inflight_preserved=True, active_fenced=True, completed_continuation=True,
                legacy_completed_continuation=True, later_occurrence_preserved=True, failed_and_lost_rejected=True,
                failed_continuation=True, unexplained_pause_failed=True,
                execution_provenance_preserved=True, tool_results=len(tool_results))))
        finally:
            service.close()
            provider.close()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--python', required=True)
    args = parser.parse_args()
    check(args.source, args.python)
