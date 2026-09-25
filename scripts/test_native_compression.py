#!/usr/bin/env python3
"""Exercise the real native resolver/compressor, replacing ONLY provider I/O.

Run via backend/tests/test_native_compression.py: its subprocess has a fresh
HOME/HERMES_HOME and no inherited credentials. Provider responses below are
explicit synthetic fixtures, never evidence of a live provider's behavior.
"""
import copy
import contextvars
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# No test can accidentally reach the network, including discovery/catalogs.
def offline(*args, **kwargs):
    raise AssertionError('Network disabled in compression contract test')
socket.socket.connect = offline
socket.create_connection = offline

import httpx
from agent import auxiliary_client as aux
from agent.context_compressor import ContextCompressor, pin_summary_route, take_pinned_summary_route
from agent.conversation_compression import CompressionCommitFence
from coconut.native_compression import install
from coconut.native_fallback import configuration, for_turn
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

APPROVAL = 'Human approval: only inspect /projects/example; do NOT deploy or delete.'
UNKNOWN = 'External effect outcome UNKNOWN: payment call pay-17 timed out; do not retry.'
SUMMARY_BODY = 'Semantic fixture evidence.\n' + APPROVAL + '\n' + UNKNOWN
SUMMARY = '## Historical Task Snapshot\nInspect the fixture.\n\n## Goal\n' + SUMMARY_BODY


class Contracts(unittest.TestCase):
    def setUp(self):
        # Keep log targets alive until the parent removes the subprocess home;
        # Hermes's asynchronous log listener can outlive an individual test.
        self.home = Path(tempfile.mkdtemp(dir=os.environ['HERMES_HOME']))
        self.owner = self.home / 'owner'; self.owner.mkdir()
        self.bot = self.home / 'bot'; self.bot.mkdir()
        self.config = {'model': {'provider': 'openai-api', 'default': 'gpt-4.1', 'context_length': 128000},
                       'auxiliary': {'compression': {'provider': 'auto', 'model': 'auto'}},
                       'compression': {'enabled': True, 'in_place': True},
                       'approvals': {'mode': 'manual'}}
        self.write(self.bot, self.config)
        self.write(self.owner, {'fallback_providers': [
            {'provider': 'openai-api', 'model': 'gpt-4.1', 'base_url': 'https://shared.invalid/v1',
             'key_env': 'OPENAI_API_KEY', 'api_mode': 'chat_completions'}]})
        (self.owner / '.env').write_text('OPENAI_API_KEY=synthetic-owner-key\n')
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'COCONUT_DEFAULT_HOME': str(self.owner)}).start()
        aux.shutdown_cached_clients()
        aux._reset_aux_unhealthy_cache()
        self.scope = configuration(self.bot); self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.calls = []
        self.reply = None
        def send(client, request, **kwargs):
            if not request.url.path.endswith('/chat/completions'):
                return httpx.Response(404, request=request, json={'error': 'No fixture catalog'})
            data = json.loads(request.content)
            self.calls.append((str(request.url), data, request.headers.get('authorization')))
            if self.reply:
                response = self.reply(request, data)
                if response is not None:
                    return response
            if data.get('stream'):
                chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 0,
                         'model': data['model'], 'choices': [{'index': 0, 'delta': {'content': SUMMARY}, 'finish_reason': 'stop'}]}
                return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'},
                                      content=('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode())
            return httpx.Response(200, request=request, json={
                'id': 'fixture', 'object': 'chat.completion', 'created': 0, 'model': data['model'],
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': SUMMARY}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}})
        patch.object(httpx.Client, 'send', send).start()

    def write(self, home, config):
        (home / 'config.yaml').write_text(json.dumps(config))

    def compressor(self, *, install_adapter=True, config=None, primary=False):
        compressor = ContextCompressor(model='gpt-4.1', provider='openai-api',
            api_key='synthetic-primary-key' if primary else '',
            base_url='https://primary.invalid/v1' if primary else '', api_mode='chat_completions',
            config_context_length=128000, quiet_mode=True)
        if install_adapter:
            install(SimpleNamespace(context_compressor=compressor), config or self.config, for_turn())
        return compressor

    def summary(self, compressor):
        return compressor._call_summary_llm('Summarize this fixture. ' + UNKNOWN, time.monotonic())

    def test_original_control_and_inherited_route(self):
        with self.assertRaises(aux.AuxiliaryClientUnavailable):
            self.summary(self.compressor(install_adapter=False))
        before = (self.bot / 'config.yaml').read_bytes(), dict(os.environ)
        compressor = self.compressor()
        self.assertEqual(self.summary(compressor), SUMMARY)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][0].startswith('https://shared.invalid/'))
        self.assertEqual(self.calls[0][2], 'Bearer synthetic-owner-key')
        self.assertEqual(before, ((self.bot / 'config.yaml').read_bytes(), dict(os.environ)))
        self.assertEqual(compressor.model, 'gpt-4.1')
        self.assertEqual(compressor.api_key, '')
        self.assertIsNone(take_pinned_summary_route())
        with self.assertRaises(aux.AuxiliaryClientUnavailable):
            self.summary(self.compressor(install_adapter=False))

    def test_working_and_api_failing_primary_do_not_inherit(self):
        compressor = self.compressor(primary=True)
        self.assertEqual(self.summary(compressor), SUMMARY)
        self.assertTrue(all('primary.invalid' in call[0] for call in self.calls))
        self.calls.clear()
        self.reply = lambda request, data: httpx.Response(400, request=request,
            json={'error': {'message': 'fixture invalid request', 'type': 'invalid_request_error'}})
        with self.assertRaises(Exception):
            self.summary(compressor)
        self.assertTrue(self.calls)
        self.assertTrue(all('primary.invalid' in call[0] for call in self.calls))

    def test_explicit_policies_and_disabled_fallback(self):
        policies = [{'fallback_providers': []}, {'fallback_model': None},
                    {'fallback_providers': [{'provider': 'openai-api', 'model': 'gpt-4.1'}]}]
        policies += [{'auxiliary': {'compression': value}} for value in (
            {'provider': 'openai-api'}, {'model': 'explicit-model'}, {'base_url': 'https://explicit.invalid'},
            {'key_env': 'EXPLICIT_KEY'}, {'api_mode': 'chat_completions'}, {'fallback_chain': []})]
        for policy in policies:
            with self.subTest(policy=policy):
                compressor = self.compressor(install_adapter=False)
                original = compressor._call_summary_llm
                self.assertFalse(install(SimpleNamespace(context_compressor=compressor), policy, for_turn()))
                self.assertEqual(compressor._call_summary_llm, original)
        self.write(self.owner, {'fallback_providers': []})
        self.assertIsNone(for_turn())
        with self.assertRaises(aux.AuxiliaryClientUnavailable):
            self.summary(self.compressor())
        self.assertFalse(self.calls)

    def test_real_explicit_auxiliary_override(self):
        config = copy.deepcopy(self.config)
        config['auxiliary']['compression'] = {'provider': 'openai-api', 'model': 'gpt-4.1',
            'base_url': 'https://explicit.invalid/v1', 'api_key': 'synthetic-explicit-key',
            'api_mode': 'chat_completions'}
        self.write(self.bot, config)
        compressor = self.compressor(config=config)
        self.assertEqual(self.summary(compressor), SUMMARY)
        self.assertTrue(all('explicit.invalid' in call[0] for call in self.calls))

    def test_chain_order_and_native_pin(self):
        owner = json.loads((self.owner / 'config.yaml').read_text())
        owner['fallback_providers'].append(dict(owner['fallback_providers'][0], model='gpt-4.1-mini'))
        self.write(self.owner, owner)
        routes = for_turn()
        second = routes[1]
        compressor = self.compressor(install_adapter=False)
        self.assertTrue(install(SimpleNamespace(context_compressor=compressor), self.config, routes))
        self.assertFalse(install(SimpleNamespace(context_compressor=compressor), self.config, routes))
        self.summary(compressor)
        self.assertEqual(self.calls[-1][1]['model'], routes[0]['model'])
        with pin_summary_route(dict(second, base_url='https://native-pin.invalid/v1')):
            self.summary(compressor)
            self.assertIsNone(take_pinned_summary_route())
        self.assertTrue('native-pin.invalid' in self.calls[-1][0])
        self.assertEqual(self.calls[-1][1]['model'], second['model'])
        self.summary(compressor)
        self.assertTrue('shared.invalid' in self.calls[-1][0])
        self.assertIsNone(take_pinned_summary_route())

    def agent(self, *, adapter=True):
        from run_agent import AIAgent
        db = SessionDB(db_path=self.bot / 'state.db')
        self.addCleanup(db.close)
        db.create_session('existing-session', source='coconut')
        agent = AIAgent(model='gpt-4.1', provider='openai-api', api_key='synthetic-main-key',
                        base_url='https://primary.invalid/v1', api_mode='chat_completions',
                        quiet_mode=True, enabled_toolsets=[], disabled_toolsets=[],
                        session_db=db, session_id='existing-session', skip_context_files=True,
                        skip_memory=True, skip_background_review=True, load_soul_identity=False)
        # Preserve real facade/engine; make its primary aux route unresolvable.
        compressor = self.compressor(install_adapter=False)
        compressor.bind_session_state(db, agent.session_id)
        agent.context_compressor = compressor
        agent.compression_in_place = True
        agent._cached_system_prompt = 'Fixture system. Preserve approvals and unknown effects.'
        if adapter:
            self.assertTrue(install(agent, self.config, for_turn()))
        messages = [{'role': 'user', 'content': APPROVAL}]
        for index in range(30):
            messages += [{'role': 'assistant', 'content': 'Legacy receipt without provenance: ' + ('archive detail ' * 400)},
                         {'role': 'user', 'content': f'Check {index}. ' + UNKNOWN}]
        messages += [{'role': 'assistant', 'content': 'Await the next instruction.'},
                     {'role': 'user', 'content': 'Only report current status. ' + APPROVAL}]
        for message in messages:
            db.append_message(agent.session_id, message['role'], message['content'])
        return db, agent, messages

    def test_real_native_compaction_archives_existing_session(self):
        db, agent, messages = self.agent()
        old_id = agent.session_id
        old_rows = db.get_messages(old_id)
        old_primary = (agent.model, agent.provider, agent.api_key)
        result, system = agent._compress_context(messages, agent._cached_system_prompt,
                                                approx_tokens=120000)
        self.assertLess(len(result), len(old_rows))
        self.assertEqual(agent.session_id, old_id)
        self.assertEqual((agent.model, agent.provider, agent.api_key), old_primary)
        self.assertTrue(agent._last_compaction_in_place)
        self.assertFalse(agent.context_compressor._last_summary_fallback_used)
        self.assertIn(SUMMARY_BODY, '\n'.join(row.get('content', '') for row in result))
        self.assertTrue(any('shared.invalid' in call[0] for call in self.calls))
        self.assertIn(UNKNOWN, json.dumps(result))
        self.assertIn(APPROVAL, json.dumps(result))
        # Read the exact native SQLite target, including inactive archived rows.
        import sqlite3
        with sqlite3.connect(self.bot / 'state.db') as connection:
            rows = connection.execute('SELECT content FROM messages WHERE session_id=? AND active=0 ORDER BY id', (old_id,)).fetchall()
            sessions = connection.execute('SELECT id FROM sessions').fetchall()
            self.assertNotIn('synthetic-owner-key', '\n'.join(connection.iterdump()))
        archived = [row[0] for row in rows]
        self.assertEqual(archived, [row['content'] for row in old_rows])
        self.assertEqual(sessions, [(old_id,)])
        self.assertEqual(db.get_session(old_id)['source'], 'coconut')
        self.assertNotIn('synthetic-owner-key', json.dumps(db.get_messages(old_id)))
        self.assertEqual(json.loads((self.bot / 'config.yaml').read_text()), self.config)
        # Re-open the existing native store rather than resetting.
        db.close()
        reopened = SessionDB(db_path=self.bot / 'state.db')
        try:
            self.assertLess(len(reopened.get_messages(old_id)), len(old_rows))
        finally:
            reopened.close()

    def test_cancellation_preserves_native_archive_and_live_history(self):
        db, agent, messages = self.agent()
        before = copy.deepcopy(messages)
        rows = db.get_messages(agent.session_id)
        fence = CompressionCommitFence()
        def cancel(request, data):
            self.assertTrue(fence.cancel_before_commit())
        self.reply = cancel
        result, _ = agent._compress_context(messages, 'sys', approx_tokens=120000, commit_fence=fence)
        self.assertEqual(result, before)
        self.assertEqual(messages, before)
        self.assertEqual(db.get_messages(agent.session_id), rows)
        self.assertTrue(fence.is_cancelled)
        self.assertTrue(self.calls)
        self.assertIsNone(take_pinned_summary_route())

    def test_native_original_compression_control(self):
        db, agent, messages = self.agent(adapter=False)
        result, _ = agent._compress_context(messages, 'sys', approx_tokens=120000)
        # Current native default degrades to a marker, NOT a semantic summary.
        text = '\n'.join(row.get('content', '') for row in result)
        self.assertIn('This fallback was generated locally without an LLM summary call.', text)
        self.assertNotIn(SUMMARY, text)
        self.assertFalse(self.calls)

    def test_loaded_history_recovers_on_normal_native_turn(self):
        db, agent, messages = self.agent()
        agent.context_compressor.note_usage_less_response()
        agent.context_compressor.threshold_tokens = 10000
        history = db.get_resume_conversations(agent.session_id)[0]
        result = agent.run_conversation('Report status only; no new actions.', conversation_history=history)
        self.assertTrue(result.get('completed'), result)
        self.assertEqual(agent.session_id, 'existing-session')
        self.assertGreater(agent.context_compressor.compression_count, 0)
        self.assertTrue(any('shared.invalid' in call[0] for call in self.calls))
        self.assertTrue(any('primary.invalid' in call[0] for call in self.calls))
        self.assertLess(len(db.get_messages(agent.session_id)), len(history))

    def test_large_legacy_user_receipt_snapshots_recover_without_span_rewrite(self):
        db, agent, _ = self.agent()
        # The incident carried three full service snapshots as USER messages,
        # not assistant/tool output. Preserve their role and relative position.
        messages = [{'role': 'assistant', 'content': 'Prior tool result received.'},
                    {'role': 'user', 'content': APPROVAL},
                    {'role': 'assistant', 'content': 'Retain the original work.'}]
        for size in (1056636, 1344252, 1585920):
            unit = 'saved receipt evidence 12345 '
            payload = (unit * (size // len(unit) + 1))[:size]
            messages.append({'role': 'user', 'content': APPROVAL + '\n' + payload + '\n' + UNKNOWN})
            for index in range(32):
                messages.extend([{'role': 'assistant', 'content': 'Prior read completed.'},
                                 {'role': 'user', 'content': 'Continue within the recorded scope.'}])
        db.replace_messages(agent.session_id, messages, archive_dropped=True)
        agent.context_compressor.note_usage_less_response()
        agent.context_compressor.threshold_tokens = 10000
        result = agent.run_conversation('Only report status.',
                                        conversation_history=db.get_resume_conversations(agent.session_id)[0])
        self.assertTrue(result.get('completed'), result)
        self.assertEqual(agent.session_id, 'existing-session')
        self.assertTrue(any('shared.invalid' in call[0] for call in self.calls))
        main_calls = [call for call in self.calls if 'primary.invalid' in call[0]]
        self.assertTrue(main_calls)
        self.assertLess(len(json.dumps(main_calls[0][1]['messages'])), 500000)
        active = json.dumps(db.get_resume_conversations(agent.session_id)[0])
        self.assertIn(APPROVAL, active)
        self.assertIn(UNKNOWN, active)
        import sqlite3
        with sqlite3.connect(self.bot / 'state.db') as connection:
            archived = [row[0] for row in connection.execute(
                'SELECT content FROM messages WHERE session_id=? AND active=0', (agent.session_id,))]
        for message in messages:
            self.assertIn(message['content'], archived)

    def test_mid_turn_policy_change_and_explicit_aux_chain(self):
        compressor = self.compressor()
        changed = dict(self.config, fallback_providers=[])
        self.write(self.bot, changed)
        with self.assertRaises(aux.AuxiliaryClientUnavailable):
            self.summary(compressor)
        self.assertFalse(self.calls)
        changed = copy.deepcopy(self.config)
        changed['auxiliary']['compression'] = {
            'provider': 'anthropic', 'model': 'claude-sonnet-4-5',
            'fallback_chain': [{'provider': 'openai-api', 'model': 'gpt-4.1-mini',
                                'base_url': 'https://aux-chain.invalid/v1',
                                'api_key': 'synthetic-local-key', 'api_mode': 'chat_completions'}]}
        self.write(self.bot, changed)
        self.assertEqual(self.summary(compressor), SUMMARY)
        self.assertTrue(all('aux-chain.invalid' in call[0] for call in self.calls))

    def test_shared_owner_policy_changes_fail_closed_before_summary(self):
        original = json.loads((self.owner / 'config.yaml').read_text())
        for update in ({'fallback_providers': []}, {'fallback_model': None},
                       {'fallback_providers': [dict(original['fallback_providers'][0], model='gpt-4.1-mini')]},
                       {'fallback_providers': [dict(original['fallback_providers'][0], base_url='https://changed.invalid/v1')]}):
            with self.subTest(update=update):
                self.write(self.owner, original)
                compressor = self.compressor()
                self.write(self.owner, update)
                with self.assertRaises(aux.AuxiliaryClientUnavailable):
                    self.summary(compressor)
                self.assertFalse(self.calls)
                self.assertIsNone(take_pinned_summary_route())
        self.write(self.owner, original)
        compressor = self.compressor()
        (self.owner / '.env').write_text('OPENAI_API_KEY=synthetic-rotated-key\n')
        with self.assertRaises(aux.AuxiliaryClientUnavailable):
            self.summary(compressor)
        self.assertFalse(self.calls)

    def test_native_main_model_recursion_does_not_reissue_inherited_pin(self):
        owner = json.loads((self.owner / 'config.yaml').read_text())
        owner['fallback_providers'][0]['model'] = 'gpt-4.1-mini'
        self.write(self.owner, owner)
        routes = for_turn()
        compressor = self.compressor(install_adapter=False)
        install(SimpleNamespace(context_compressor=compressor), self.config, routes)
        def empty(request, data):
            return httpx.Response(200, request=request, json={
                'id': 'empty-fixture', 'model': data['model'],
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': ''}, 'finish_reason': 'stop'}]})
        self.reply = empty
        self.assertIsNone(compressor._generate_summary([{'role': 'user', 'content': UNKNOWN}]))
        self.assertTrue(compressor._summary_model_fallen_back)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(take_pinned_summary_route())
        # A fresh attempt is eligible again, without restoring any global state.
        compressor._clear_compression_failure_cooldown()
        self.reply = None
        self.assertIsNotNone(compressor._generate_summary([{'role': 'user', 'content': UNKNOWN}]))
        self.assertEqual(len(self.calls), 2)

    def test_host_timeout_does_not_commit_a_late_summary(self):
        db, agent, messages = self.agent()
        before = copy.deepcopy(messages)
        rows = db.get_messages(agent.session_id)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def blocked(request, data):
            started.set()
            release.wait(timeout=10)
            finished.set()
        self.reply = blocked
        with patch('agent.conversation_compression.resolve_context_compression_timeouts',
                   return_value=(0.5, 2.0)):
            try:
                result, _ = agent._compress_context(messages, 'sys', approx_tokens=120000)
                self.assertTrue(started.is_set())
                self.assertEqual(result, before)
                self.assertEqual(messages, before)
                self.assertEqual(db.get_messages(agent.session_id), rows)
            finally:
                release.set()
        self.assertTrue(finished.wait(timeout=5))
        # Allow the native cancelled worker to consume the late response.
        time.sleep(0.1)
        self.assertEqual(messages, before)
        self.assertEqual(db.get_messages(agent.session_id), rows)
        self.assertIsNone(take_pinned_summary_route())

    def test_concurrent_profiles_and_context_copies(self):
        other = self.home / 'other'; other.mkdir()
        self.write(other, {'fallback_providers': []})
        barrier = threading.Barrier(2)
        results = {}; errors = []
        def run(name, home):
            try:
                with configuration(home):
                    barrier.wait(timeout=5)
                    chain = for_turn()
                    compressor = self.compressor(install_adapter=False)
                    install(SimpleNamespace(context_compressor=compressor), json.loads((home / 'config.yaml').read_text()), chain)
                    context = contextvars.copy_context()
                    try:
                        value = context.run(self.summary, compressor)
                    except aux.AuxiliaryClientUnavailable:
                        value = 'unavailable'
                    results[name] = (value, get_hermes_home(), take_pinned_summary_route(), context.run(take_pinned_summary_route))
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=run, args=(name, home)) for name, home in (('inherit', self.bot), ('disabled', other))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=15)
        self.assertFalse(errors, errors)
        self.assertEqual(results['inherit'], (SUMMARY, self.bot, None, None))
        self.assertEqual(results['disabled'], ('unavailable', other, None, None))
        self.assertEqual(get_hermes_home(), self.bot)


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=2).result
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(f'Native compression contracts passed ({result.testsRun} tests)')
