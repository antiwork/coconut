#!/usr/bin/env python3
"""Offline real-worker regression for the resumed stale-visible-reply shape.

Only provider I/O is synthetic. No installed config, session or credential is
read. The negative control executes the original PR87 worker source verbatim.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PR87 = '66218b4859ccec0245070242bf1559549bfadad8'
APPROVAL = 'Human approval permits reading evidence. Do not publish, deploy, delete or retry payment.'
UNKNOWN = 'Payment pay-17 outcome UNKNOWN. Obtain evidence; do not repeat the effect.'
VISIBLE = 'I will inspect now.'
CURRENT = 'Report current status only. ' + APPROVAL
SUMMARY = '## Historical Task Snapshot\nInspect saved evidence.\n\n## Goal\n' + APPROVAL + '\n' + UNKNOWN


def sized(text, size):
    return (text * (size // len(text) + 1))[:size]


def history(production=False, dangling=False, unknown_middle=False):
    from agent.context_compressor import SUMMARY_PREFIX
    summary_size = 14612 if production else 1200
    rows = [{'role': 'user', 'content': sized(SUMMARY_PREFIX + '\n' + SUMMARY + '\n', summary_size),
             '_compressed_summary': True}]
    def group(identity, content='Saved read result.'):
        return [{'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': identity, 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{"command":"read-only fixture"}'}}]},
                {'role': 'tool', 'content': content, 'tool_call_id': identity, 'tool_name': 'terminal'}]
    rows += group('unknown-effect', sized(UNKNOWN + ' ', 123))
    rows[-1]['effect_disposition'] = 'unknown'
    rows.append({'role': 'assistant', 'content': VISIBLE})
    sizes = (1056636, 1344252, 1585920) if production else (150000, 180000, 210000)
    for index, size in enumerate(sizes):
        rows.append({'role': 'user', 'content': sized(APPROVAL + '\n' + UNKNOWN + '\nsaved receipt evidence 12345\n', size)})
        # Exact initial role shape: snapshot, two empty assistant/tool pairs,
        # ordinary user, then hundreds of tool-only turns, with no visible reply.
        rows += group(f'{index}-a') + group(f'{index}-b')
        rows.append({'role': 'user', 'content': sized('Continue within the authorized scope. ', 981)})
        for turn in range(100 if production else 24):
            rows += group(f'{index}-{turn}')
    rows.append({'role': 'user', 'content': 'Retry the status report; the prior attempt was interrupted.'})
    rows += group('retry-read')
    if dangling:
        rows.append({'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'uncertain-delegate', 'type': 'function', 'function': {
                'name': 'delegate_task', 'arguments': '{"tasks":[{"goal":"Prior interrupted work"}]}'}}]})
        rows.append({'role': 'user', 'content': 'Previous continuation failed before a tool result.'})
    if unknown_middle:
        unresolved = group('unresolved-middle', UNKNOWN)
        unresolved[-1]['effect_disposition'] = 'unknown'
        rows[5:5] = unresolved
    return rows


def child(case, evidence, control):
    import socket
    import faulthandler
    faulthandler.dump_traceback_later(35, repeat=True)
    from unittest.mock import patch
    def offline(*args, **kwargs):
        raise AssertionError('Network forbidden; only provider I/O fixtures are allowed')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = offline
    import httpx
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from agent.context_compressor import ContextCompressor
    from agent.model_metadata import estimate_request_tokens_rough
    from coconut.native_fallback import configuration

    home = Path(os.environ['HERMES_HOME'])
    owner = home / 'owner'; owner.mkdir()
    # A large Sol window lets normal preflight defer to the simulated provider
    # failure. Native overflow parsing then learns Astra's smaller window.
    config = {'toolsets': [], 'model': {'provider': 'openai-api', 'default': 'gpt-6-sol',
              'base_url': 'https://sol.invalid/v1', 'context_length': 2097152},
              'agent': {'max_turns': 8}, 'approvals': {'mode': 'manual'},
              'auxiliary': {'background_review': {'enabled': False}}}
    (home / 'config.yaml').write_text(json.dumps(config))
    (owner / 'config.yaml').write_text(json.dumps({'fallback_providers': [
        {'provider': 'openai-api', 'model': 'gpt-6-astra', 'base_url': 'https://astra.invalid/v1',
         'api_mode': 'chat_completions', 'key_env': 'OPENAI_API_KEY'}]}))
    (owner / '.env').write_text('OPENAI_API_KEY=synthetic-owner-key\n')
    os.environ['COCONUT_DEFAULT_HOME'] = str(owner)
    session = '00000000-0000-4000-8000-000000000001'
    task_id = '00000000-0000-4000-8000-000000000002'
    receipts = ([{'id': 'uncertain-receipt', 'task_id': task_id, 'state': 'executing',
                  'action': {'name': 'delegate_task', 'arguments': {'tasks': [{'goal': 'Prior interrupted work'}]}}}]
                if 'dangling' in case else [])
    from coconut.native_bridge import fingerprint
    for saved in receipts:
        saved.update(action_hash=fingerprint(saved['action']), result=None)
    receipts_before = copy.deepcopy(receipts)
    db = SessionDB()
    db.create_session(session, source='coconut')
    db.set_meta('coconut.history-context.' + session, 'complete')
    rows = history('production' in case, dangling='dangling' in case, unknown_middle='unknown-middle' in case)
    declined = case.startswith(('original', 'held'))
    db.replace_messages(session, rows)
    before = db.get_messages(session)
    db.close()
    observations, events, agents = [], [], []
    summary_calls = 0
    tool_calls = 0
    initial_compressors = []
    original_init = AIAgent.__init__
    def observe_init(agent, *args, **kwargs):
        original_init(agent, *args, **kwargs)
        assert type(agent.context_compressor) is ContextCompressor
        initial_compressors.append(agent.context_compressor)
        agents.append(agent)
    def reply(client, request, **kwargs):
        nonlocal summary_calls, tool_calls
        if not request.url.path.endswith('/chat/completions'):
            return httpx.Response(404, request=request, json={'error': 'No fixture catalog'})
        data = json.loads(request.content)
        is_summary = not data.get('tools')
        estimate = estimate_request_tokens_rough(data['messages'], tools=data.get('tools'))
        observation = {'model': data['model'], 'host': request.url.host, 'summary': is_summary,
                       'input_estimate': estimate, 'rows': len(data['messages'])}
        observations.append(observation)
        if not is_summary and data['model'] == 'gpt-6-sol':
            observation['status'] = 429
            return httpx.Response(429, request=request, headers={'retry-after': '0'}, json={
                'error': {'message': 'Synthetic Sol rate limit', 'type': 'rate_limit_error', 'code': 'rate_limit_exceeded'}})
        if not is_summary and estimate > 65536:
            observation['status'] = 400
            return httpx.Response(400, request=request, json={'error': {
                'message': 'This model maximum context length is 65536 tokens. The input exceeds this context limit.',
                'type': 'invalid_request_error', 'code': 'context_length_exceeded'}})
        observation['status'] = 200
        if is_summary:
            summary_calls += 1
            telemetry = agents[0].context_compressor._last_compression_telemetry or {}
            observation['compression_regions'] = {key: telemetry.get(key) for key in (
                'protected_head_tokens', 'protected_tail_tokens', 'middle_window_tokens', 'chunk_count')}
            text = json.dumps(data['messages'])
            (evidence / (case + '-summary-input.json')).write_text(json.dumps(data, indent=2))
            assert APPROVAL in text and UNKNOWN in text, 'Summary input lost fixture constraints'
            message, finish = {'role': 'assistant', 'content': SUMMARY}, 'stop'
        elif tool_calls == 0:
            (evidence / (case + '-fitting-input.json')).write_text(json.dumps(data, indent=2))
            observation['wire_visible_row_exact'] = any(row.get('content') == VISIBLE for row in data['messages'])
            observation['visible_verbatim'] = any(str(row.get('content') or '').startswith(VISIBLE) for row in data['messages'])
            observation['current_verbatim'] = any(row.get('content') == current_prompt or
                ('dangling' in case and str(row.get('content') or '').endswith('\n\n' + current_prompt))
                for row in data['messages'])
            offered = {tool.get('function', tool).get('name') for tool in data.get('tools', [])}
            name, arguments = 'coconut_read_receipts', {}
            if name not in offered:
                assert 'tool_call' in offered
                name, arguments = 'tool_call', {'calls': [{'name': name, 'arguments': arguments}]}
            tool_calls += 1
            message, finish = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'fixture-read', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}, 'tool_calls'
        else:
            assert any(row['role'] == 'tool' and ('\"total\": ' + str(len(receipts))) in str(row.get('content')) for row in data['messages'])
            message, finish = {'role': 'assistant', 'content': 'Status verified. No external action repeated.'}, 'stop'
        value = {'id': 'fixture', 'created': 0, 'model': data['model']}
        if data.get('stream'):
            if message.get('tool_calls'):
                message['tool_calls'][0]['index'] = 0
            value.update(object='chat.completion.chunk', choices=[{'index': 0, 'delta': message, 'finish_reason': finish}])
            return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'},
                content=('data: ' + json.dumps(value) + '\n\ndata: [DONE]\n\n').encode())
        value.update(object='chat.completion', choices=[{'index': 0, 'message': message, 'finish_reason': finish}],
                     usage={'prompt_tokens': estimate, 'completion_tokens': 30, 'total_tokens': estimate + 30})
        return httpx.Response(200, request=request, json=value)
    sys.path.insert(0, str(ROOT / 'backend/coconut'))
    import types
    worker = types.ModuleType('fixture_worker')
    if case.startswith('original'):
        summary_control = types.ModuleType('native_compression')
        summary_path = control.with_name('original-pr87-compression.py')
        exec(compile(summary_path.read_text(), str(summary_path), 'exec'), summary_control.__dict__)
        sys.modules['native_compression'] = summary_control
    path = control if case.startswith('original') else ROOT / 'backend/coconut/hermes_worker.py'
    worker.__file__ = str(ROOT / 'backend/coconut/hermes_worker.py')
    exec(compile(path.read_text(), str(path), 'exec'), worker.__dict__)
    request = {'operation': 'start', 'task': {'id': task_id, 'title': 'Inspect retained evidence', '_native_receipts': receipts},
        'bot': {'id': '00000000-0000-4000-8000-000000000003', 'session_id': session, 'name': 'Offline fixture', 'instructions': ''},
        'history': [{'id': '00000000-0000-4000-8000-000000000004', 'task_id': task_id, 'role': 'user', 'text': CURRENT}],
        'model': 'gpt-6-sol', 'provider': 'openai-api',
        'runtime_config': {'provider': 'openai-api', 'api_mode': 'chat_completions', 'base_url': 'https://sol.invalid/v1', 'api_key': 'synthetic-sol-key'}}
    current_prompt, _ = worker.conversation_input(request)
    assert CURRENT in current_prompt
    def receipt(kind, payload):
        raise AssertionError('Read-only fixture unexpectedly requested an effect: ' + kind)
    def emit(kind, payload):
        events.append({'type': kind, 'data': payload})
        if kind == 'failed' and sys.exception() is not None:
            import traceback
            traceback.print_exception(sys.exception())
    try:
        with patch.object(httpx.Client, 'send', reply), patch.object(AIAgent, '__init__', observe_init), configuration(home):
            result = worker.run_turn(request, emit, receipt)
        assert receipts == receipts_before
        assert len(agents) == 1
        agent = agents[0]
        compressor = agent.context_compressor
        assert compressor is initial_compressors[0], 'Worker replaced the AIAgent-constructed compressor'
        assert agent.session_id == session
        assert any(o['status'] == 429 for o in observations), observations
        assert any(o['status'] == 400 for o in observations), observations
        db = SessionDB(read_only=True)
        after = db.get_messages(session)
        all_rows = db.get_messages(session, include_inactive=True)
        assert db.resolve_resume_session_id(session) == session
        # Exact original DB rows, not merely text fragments, remain archived.
        (evidence / (case + '-active.json')).write_text(json.dumps(after, indent=2))
        for row in before:
            saved = next((item for item in all_rows if item['id'] == row['id']), None)
            assert saved is not None
            differences = {key: (value, saved.get(key)) for key, value in row.items() if value != saved.get(key)}
            assert differences in ({}, {'active': (1, 0)}, {'active': (1, 0), 'compacted': (0, 1)}), ('Original row mutated', row['id'], differences)
            if not declined:
                assert saved['active'] == 0, ('Original row was not archived', row['id'])
        if declined:
            assert not summary_calls and not tool_calls
            assert not any(e['type'] == 'completed' for e in events), events
            assert compressor._last_compression_telemetry['middle_window_tokens'] == 0
            assert compressor._last_compression_telemetry['chunk_count'] == 0
        else:
            assert result == 0 and any(e['type'] == 'completed' for e in events), events
            assert summary_calls > 0 and tool_calls == 1, observations
            assert compressor.compression_count > 0
            assert not compressor._last_summary_fallback_used
            assert all(o['input_estimate'] <= 65536 for o in observations if not o['summary'] and o['status'] == 200)
            assert len(after) < len(before)
            assert any(row.get('content') == VISIBLE for row in after)
            assert any(row.get('content') == current_prompt or
                ('dangling' in case and str(row.get('content') or '').endswith('\n\n' + current_prompt))
                for row in after)
            assert all(o['visible_verbatim'] and o['current_verbatim'] for o in observations if 'visible_verbatim' in o)
            assert APPROVAL in json.dumps(after) and UNKNOWN in json.dumps(after)
            if receipts:
                assert 'uncertain-receipt' in json.dumps(after) and 'executing' in json.dumps(after)
            assert any(row.get('effect_disposition') == 'unknown' for row in after)
            assert 'synthetic-owner-key' not in json.dumps(all_rows)
            import sqlite3
            with sqlite3.connect(home / 'state.db') as connection:
                archived = connection.execute('SELECT COUNT(*) FROM messages WHERE session_id=? AND active=0', (session,)).fetchone()[0]
            assert archived >= len(before)
        db.close()
        report = {'case': case, 'original_rows': len(before), 'active_rows': len(after), 'summary_calls': summary_calls,
                  'initial_shape': [{'role': row['role'], 'chars': len(row.get('content') or ''),
                                     'tool_calls': bool(row.get('tool_calls'))} for row in rows[:10]],
                  'tool_calls': tool_calls, 'same_session': True, 'exact_original_rows_preserved': True,
                  'constructed_compressor_retained': True, 'telemetry': compressor._last_compression_telemetry,
                  'provider_io': observations}
        (evidence / (case + '.json')).write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        (evidence / (case + '-observations.json')).write_text(json.dumps({'provider_io': observations, 'events': events}, indent=2))
        for log in (home / 'logs').glob('*.log'):
            (evidence / (case + '-' + log.name)).write_bytes(log.read_bytes())


def contracts():
    import contextvars
    import socket
    import threading
    import time
    import unittest
    from types import SimpleNamespace
    from unittest.mock import patch
    def offline(*args, **kwargs):
        raise AssertionError('Network forbidden in native head contracts')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = offline
    import httpx
    from run_agent import AIAgent
    from agent.context_compressor import ContextCompressor
    from agent.conversation_compression import CompressionCommitFence
    from coconut.native_compression_head import install
    from coconut.native_fallback import configuration
    from hermes_state import SessionDB

    class HeadContracts(unittest.TestCase):
        def setUp(self):
            self.home = Path(tempfile.mkdtemp(dir=os.environ['HERMES_HOME']))
            self.config = {'model': {'default': 'gpt-4.1', 'provider': 'openai-api',
                           'base_url': 'https://head.invalid/v1', 'context_length': 65536}, 'toolsets': []}
            (self.home / 'config.yaml').write_text(json.dumps(self.config))
            self.scope = configuration(self.home)
            self.scope.__enter__()
            self.addCleanup(self.scope.__exit__, None, None, None)
            self.addCleanup(patch.stopall)
            self.calls = []
            self.response = None
            def send(client, request, **kwargs):
                if not request.url.path.endswith('/chat/completions'):
                    return httpx.Response(404, request=request)
                self.calls.append(json.loads(request.content))
                if self.response:
                    self.response()
                chunk = {'id': 'fixture', 'model': 'gpt-4.1', 'object': 'chat.completion.chunk', 'created': 0,
                         'choices': [{'index': 0, 'delta': {'content': SUMMARY}, 'finish_reason': 'stop'}]}
                if self.calls[-1].get('stream'):
                    return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'},
                        content=('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode())
                return httpx.Response(200, request=request, json={'id': 'fixture', 'model': 'gpt-4.1',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': SUMMARY}, 'finish_reason': 'stop'}]})
            patch.object(httpx.Client, 'send', send).start()

        def agent(self, install_adapter=True, config=None):
            if config:
                (self.home / 'config.yaml').write_text(json.dumps(config))
            db = SessionDB(db_path=self.home / ('session-' + str(len(self.calls)) + '-' + str(time.monotonic_ns()) + '.db'))
            self.addCleanup(db.close)
            db.create_session('fixture', source='coconut')
            agent = AIAgent(model='gpt-4.1', provider='openai-api', api_key='synthetic-fixture-key',
                base_url='https://head.invalid/v1', api_mode='chat_completions', quiet_mode=True,
                enabled_toolsets=[], disabled_toolsets=[], session_id='fixture', session_db=db,
                skip_context_files=True, skip_memory=True, skip_background_review=True, load_soul_identity=False)
            self.addCleanup(agent.release_clients)
            self.assertIs(type(agent.context_compressor), ContextCompressor)
            self.assertEqual(agent.context_compressor.context_length, (config or self.config)['model']['context_length'])
            native_tail = agent.context_compressor._find_tail_cut_by_tokens
            native_facade = agent._compress_context
            if install_adapter:
                self.assertTrue(install(agent, config or self.config))
                self.assertFalse(install(agent, config or self.config))
            self.assertEqual(agent.context_compressor._find_tail_cut_by_tokens, native_tail)
            self.assertEqual(agent._compress_context, native_facade)
            rows = history() + [{'role': 'user', 'content': CURRENT}]
            db.replace_messages('fixture', rows)
            return db, agent, rows

        def test_exact_shape_and_bound(self):
            _, agent, rows = self.agent()
            compressor = agent.context_compressor
            before = copy.deepcopy(rows)
            self.assertEqual(ContextCompressor._protect_head_size(compressor, rows), 0)
            self.assertEqual(compressor._find_tail_cut_by_tokens(rows, 0), 1)
            self.assertEqual(compressor._protect_head_size(rows), 4)
            self.assertGreater(compressor._find_tail_cut_by_tokens(rows, 4), 4)
            self.assertEqual(rows, before)

        def test_closed_tool_group_on_visible_reply(self):
            _, agent, rows = self.agent()
            rows[3]['tool_calls'] = [{'id': 'visible-read', 'type': 'function',
                'function': {'name': 'terminal', 'arguments': '{}'}}]
            rows.insert(4, {'role': 'tool', 'tool_call_id': 'visible-read', 'content': 'Complete.'})
            self.assertEqual(agent.context_compressor._protect_head_size(rows), 5)

        def test_unresolved_effect_window_is_not_summarized(self):
            _, agent, rows = self.agent()
            middle = copy.deepcopy(rows)
            middle[6]['effect_disposition'] = 'unknown'
            before = copy.deepcopy(middle)
            self.assertEqual(agent.context_compressor._protect_head_size(middle), 0)
            self.assertEqual(middle, before)
            tail = copy.deepcopy(rows)
            tail[-2]['effect_disposition'] = 'unknown'
            self.assertEqual(agent.context_compressor._protect_head_size(tail), 4)

        def test_native_tool_identifiers_and_reuse(self):
            _, agent, rows = self.agent()
            for call, result in (
                ({'id': 'response-item', 'call_id': 'pair'}, 'pair'),
                ({'id': 'pair|response-item'}, 'response-item'),
                ({'id': 'pair', 'response_item_id': 'response-item'}, 'pair|response-item'),
            ):
                with self.subTest(call=call):
                    variant = copy.deepcopy(rows)
                    variant[1]['tool_calls'][0].update(call)
                    variant[2]['tool_call_id'] = result
                    self.assertEqual(agent.context_compressor._protect_head_size(variant), 4)
            # A completed identifier can be reused in a later closed batch.
            reused = copy.deepcopy(rows)
            reused[5]['tool_calls'][0]['id'] = reused[1]['tool_calls'][0]['id']
            reused[6]['tool_call_id'] = reused[2]['tool_call_id']
            self.assertEqual(agent.context_compressor._protect_head_size(reused), 4)

        def test_ineligible_histories_unchanged(self):
            _, agent, rows = self.agent()
            variants = {}
            variants['fitting'] = [dict(row, content='short' if row['role'] == 'user' and not row.get('_compressed_summary') else row['content']) for row in rows]
            variants['oversized-prefix'] = copy.deepcopy(rows)
            variants['oversized-prefix'][2]['content'] = 'prefix ' * 10000
            variants['missing-result'] = rows[:2] + rows[3:]
            variants['orphan-result'] = copy.deepcopy(rows)
            variants['orphan-result'][2]['tool_call_id'] = 'wrong'
            variants['open-tail'] = rows + [{'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'pending', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{}'}}]}]
            variants['missing-current'] = [row for i, row in enumerate(rows) if i <= 3 or row['role'] != 'user']
            variants['metadata-scaffold-current'] = copy.deepcopy(rows)
            variants['metadata-scaffold-current'][-1]['_todo_snapshot_synthetic'] = True
            variants['later-visible'] = rows + [{'role': 'assistant', 'content': 'Recent final reply.'}]
            variants['non-summary-middle'] = copy.deepcopy(rows)
            variants['non-summary-middle'][0] = {'role': 'user', 'content': 'Initial real user request.'}
            variants['multimodal-visible'] = copy.deepcopy(rows)
            variants['multimodal-visible'][3]['content'] = [{'type': 'text', 'text': VISIBLE}]
            for name, variant in variants.items():
                with self.subTest(name=name):
                    before = copy.deepcopy(variant)
                    native = ContextCompressor._protect_head_size(agent.context_compressor, variant)
                    self.assertEqual(agent.context_compressor._protect_head_size(variant), native)
                    self.assertEqual(variant, before)

        def test_context_fraction_cap(self):
            from agent.context_compressor import _estimate_msg_budget_tokens
            config = dict(self.config, model=dict(self.config['model'], context_length=64000))
            _, agent, rows = self.agent(config=config)
            # Native AIAgent enforces a 64K minimum. Select a fixture prefix
            # between that window's one-eighth cap and the absolute 8192 cap.
            low, high = 0, 40000
            while low < high:
                size = (low + high) // 2
                rows[2]['content'] = 'x' * size
                added = sum(_estimate_msg_budget_tokens(row) for row in rows[:4])
                if added <= 64000 // 8:
                    low = size + 1
                else:
                    high = size
            rows[2]['content'] = 'x' * low
            added = sum(_estimate_msg_budget_tokens(row) for row in rows[:4])
            self.assertGreater(added, 64000 // 8)
            self.assertLess(added, 8192)
            self.assertEqual(agent.context_compressor._protect_head_size(rows), 0)

        def test_user_tail_promise_unchanged(self):
            config = dict(self.config, compression={'min_tail_user_messages': 100})
            _, agent, rows = self.agent(config=config)
            # A small real user inside the newly proposed head is still covered
            # by the configured N-user promise, even though native aligns to the
            # old tool parent and leaves only the leading summary as its middle.
            rows.insert(3, {'role': 'user', 'content': 'An earlier protected request.'})
            self.assertEqual(agent.context_compressor.min_tail_user_messages, 100)
            self.assertEqual(agent.context_compressor._protect_head_size(rows), 0)

        def test_custom_engine_and_instance_isolation(self):
            _, agent, rows = self.agent(install_adapter=False)
            native = agent.context_compressor._protect_head_size
            self.assertFalse(install(agent, {'context': {'engine': 'custom'}}))
            self.assertEqual(native, agent.context_compressor._protect_head_size)
            self.assertFalse(install(SimpleNamespace(context_compressor=SimpleNamespace()), {}))
            _, other, _ = self.agent()
            self.assertEqual(agent.context_compressor._protect_head_size(rows), 0)
            self.assertEqual(contextvars.copy_context().run(other.context_compressor._protect_head_size, rows), 4)
            self.assertEqual(ContextCompressor._protect_head_size(other.context_compressor, rows), 0)

        def test_concurrent_copied_contexts(self):
            from concurrent.futures import ThreadPoolExecutor
            _, agent, rows = self.agent()
            other_home = self.home / 'other'; other_home.mkdir()
            (other_home / 'config.yaml').write_text(json.dumps(self.config))
            with configuration(other_home):
                _, other, _ = self.agent(install_adapter=False)
                second = contextvars.copy_context()
            first = contextvars.copy_context()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(first.run, agent.context_compressor._protect_head_size, rows),
                           pool.submit(second.run, other.context_compressor._protect_head_size, rows)]
                self.assertEqual([future.result(timeout=5) for future in futures], [4, 0])

        def test_normal_preflight_uses_constructed_compressor(self):
            db, agent, rows = self.agent()
            compressor = agent.context_compressor
            # Learn usage omission from an actual synthetic provider response,
            # never by changing native usage flags or compression thresholds.
            warmup = agent.run_conversation('Read-only capability probe.', conversation_history=[])
            self.assertTrue(warmup.get('completed'), warmup)
            result = agent.run_conversation(CURRENT, conversation_history=rows[:-1])
            self.assertTrue(result.get('completed'), result)
            self.assertIs(agent.context_compressor, compressor)
            self.assertGreater(compressor.compression_count, 0)
            self.assertEqual(agent.session_id, 'fixture')
            self.assertTrue(any(row.get('content') == VISIBLE for row in db.get_messages('fixture')))
            self.assertTrue(any(row.get('effect_disposition') == 'unknown' for row in db.get_messages('fixture')))
            self.assertGreaterEqual(len(self.calls), 2)

        def test_original_preflight_empty_middle(self):
            _, agent, rows = self.agent(install_adapter=False)
            agent.run_conversation('Read-only capability probe.', conversation_history=[])
            agent.run_conversation(CURRENT, conversation_history=rows[:-1])
            self.assertEqual(agent.context_compressor.compression_count, 0)
            self.assertEqual(agent.context_compressor._last_compression_telemetry['middle_window_tokens'], 0)
            self.assertEqual(agent.context_compressor._last_compression_telemetry['chunk_count'], 0)
            self.assertFalse(any('You are a summarization agent' in json.dumps(call['messages']) for call in self.calls))

        def test_native_cancellation_fence(self):
            db, agent, rows = self.agent()
            before = copy.deepcopy(rows), db.get_messages('fixture')
            fence = CompressionCommitFence()
            self.response = lambda: self.assertTrue(fence.cancel_before_commit())
            result, _ = agent._compress_context(rows, 'Fixture system.', commit_fence=fence)
            self.assertEqual((result, db.get_messages('fixture')), before)
            self.assertEqual(rows, before[0])
            self.assertTrue(self.calls)

        def test_native_late_response_fence(self):
            db, agent, rows = self.agent()
            before = copy.deepcopy(rows), db.get_messages('fixture')
            started, release, finished = threading.Event(), threading.Event(), threading.Event()
            def blocked():
                started.set()
                release.wait(timeout=5)
                finished.set()
            self.response = blocked
            with patch('agent.conversation_compression.resolve_context_compression_timeouts', return_value=(0.3, 1.0)):
                try:
                    result, _ = agent._compress_context(rows, 'Fixture system.')
                    self.assertTrue(started.is_set())
                    self.assertEqual((result, db.get_messages('fixture')), before)
                finally:
                    release.set()
            self.assertTrue(finished.wait(timeout=5))
            time.sleep(0.1)
            self.assertEqual((rows, db.get_messages('fixture')), before)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(HeadContracts))
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(f'Native stale-head contracts passed ({result.testsRun} tests)')


def run(source, python, evidence, cases):
    evidence.mkdir(parents=True, exist_ok=True)
    control = evidence / 'original-pr87-worker.py'
    if any(case.startswith('original') for case in cases):
        for source_file, destination in (('hermes_worker.py', control),
                                          ('native_compression.py', control.with_name('original-pr87-compression.py'))):
            original = subprocess.run(['git', 'show', PR87 + ':backend/coconut/' + source_file],
                                      cwd=ROOT, check=True, capture_output=True).stdout
            destination.write_bytes(original)
            print('Original PR87 ' + source_file + ' SHA256:', hashlib.sha256(original).hexdigest(), flush=True)
    for case in cases:
        with tempfile.TemporaryDirectory(dir=evidence, prefix=case + '-') as temporary:
            env = {key: os.environ[key] for key in ('PATH',) if key in os.environ}
            env.update(HOME=temporary, HERMES_HOME=temporary, TMPDIR=temporary, HERMES_DISABLE_MCP='1',
                HERMES_SKIP_SKILL_SYNC='1', PYTHONDONTWRITEBYTECODE='1',
                PYTHONPATH=os.pathsep.join((str(source), str(ROOT / 'backend'))))
            try:
                result = subprocess.run([python, str(Path(__file__).resolve()), '--child', case,
                    '--evidence', str(evidence.resolve()), '--control', str(control.resolve())], cwd=ROOT,
                    env=env, text=True, capture_output=True, timeout=90)
            except subprocess.TimeoutExpired as error:
                (evidence / (case + '-timeout.log')).write_bytes((error.stdout or b'') + (error.stderr or b''))
                for log in (Path(temporary) / 'logs').glob('*.log'):
                    (evidence / (case + '-' + log.name)).write_bytes(log.read_bytes())
                raise
            (evidence / (case + '-stdout.log')).write_text(result.stdout)
            (evidence / (case + '-stderr.log')).write_text(result.stderr)
            if result.returncode:
                print(result.stdout, result.stderr)
                raise AssertionError(case + ' failed')
            print(case + ' passed', flush=True)
    print(f'Stale-head worker regressions passed ({len(cases)} cases)', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--python')
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--child')
    parser.add_argument('--control', type=Path)
    parser.add_argument('--cases', nargs='+', default=['original-compact', 'fixed-compact', 'original-production', 'fixed-production', 'original-production-dangling', 'fixed-production-dangling', 'held-unknown-middle', 'contracts'])
    args = parser.parse_args()
    if args.child == 'contracts':
        contracts()
    elif args.child:
        child(args.child, args.evidence, args.control)
    else:
        run(args.source, args.python, args.evidence, args.cases)
