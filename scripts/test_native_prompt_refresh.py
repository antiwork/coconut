#!/usr/bin/env python3
"""Check current native instructions through the real worker without external I/O."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SESSION = '00000000-0000-4000-8000-000000000001'


def child():
    import socket
    from unittest.mock import patch

    def offline(*args, **kwargs):
        raise AssertionError('The native prompt check cannot use the network.')

    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = offline
    import httpx
    from hermes_state import SessionDB
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    from coconut.native_fallback import configuration
    from coconut.profiles import prompt_revision

    home = Path(os.environ['HERMES_HOME'])
    (home / '.no-bundled-skills').write_text('Use essential Hermes skills only.\n')
    owner = home / 'owner'
    owner.mkdir()
    (home / 'config.yaml').write_text(json.dumps({
        'toolsets': [], 'model': {'provider': 'openai-api', 'default': 'gpt-6-sol',
                                'base_url': 'https://sol.invalid/v1', 'context_length': 200000},
        'agent': {'max_turns': 4}, 'approvals': {'mode': 'manual'},
        'compression': {'enabled': False}, 'auxiliary': {'background_review': {'enabled': False}}}))
    (owner / 'config.yaml').write_text('{}')
    (owner / '.env').write_text('OPENAI_API_KEY=synthetic-owner-key\n')
    os.environ['COCONUT_DEFAULT_HOME'] = str(owner)
    memories = home / 'memories'
    memories.mkdir(exist_ok=True)
    old_rule = 'The owner performs every merge manually. Agents never merge any pull request.'
    new_rule = 'Only issue #3664 requires a manual merge. Other pull requests use their recorded merge approval.'
    old_identity, new_identity = 'Your identity is Prompt Fixture One.', 'Your identity is Prompt Fixture Two.'
    (memories / 'USER.md').write_text(old_rule)
    old_memory = 'Keep the original receipt for receipt-fixture-17.'
    new_memory = 'The original receipt for receipt-fixture-17 remains authoritative.'
    (memories / 'MEMORY.md').write_text(old_memory)
    (home / 'SOUL.md').write_text(old_identity)
    seed = []
    for index in range(64):
        seed.extend(({'role': 'user', 'content': f'Earlier request {index}.',
                      'platform_message_id': f'coconut:earlier-user-{index}'},
                     {'role': 'assistant', 'content': f'Earlier answer {index}.'}))
    seed.extend(({'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'receipt-fixture-17', 'type': 'function', 'function': {
            'name': 'terminal', 'arguments': '{"command":"fixture effect"}'}}]},
        {'role': 'tool', 'content': 'Original effect confirmed. Do not repeat.',
         'tool_call_id': 'receipt-fixture-17', 'tool_name': 'terminal'},
        {'role': 'assistant', 'content': 'The original effect is complete.'}))
    db = SessionDB()
    db.create_session(SESSION, source='coconut')
    db.set_meta('coconut.history-context.' + SESSION, 'complete')
    db.replace_messages(SESSION, seed)
    before = db.get_messages(SESSION)
    db.close()

    sent = []
    proposals = []

    def reply(client, request, **kwargs):
        if not request.url.path.endswith('/chat/completions'):
            return httpx.Response(404, request=request, json={'error': 'No fixture catalog'})
        data = json.loads(request.content)
        sent.append(data['messages'])
        if phase == 'during_turn':
            (memories / 'MEMORY.md').write_text(new_memory)
            from coconut.native_turn_scope import current
            # Bypass schema filtering to prove the worker also replaces an untrusted internal field.
            scope = current.get()
            scope.handlers['coconut_suggest']({'key': 'changed-profile', 'title': 'Mark the draft ready?',
                'options': [{'id': 'ready', 'label': 'Mark ready', 'answer': 'approve', 'explanation': 'Keep the merge separate.'}],
                '_worker_profile_revision': prompt_revision(home)})
        message = {'role': 'assistant', 'content': 'The recorded choice permits only marking the draft ready.'}
        value = {'id': 'fixture', 'created': 0, 'model': data['model']}
        if data.get('stream'):
            value.update(object='chat.completion.chunk', choices=[{'index': 0, 'delta': message, 'finish_reason': 'stop'}])
            return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'},
                content=('data: ' + json.dumps(value) + '\n\ndata: [DONE]\n\n').encode())
        value.update(object='chat.completion', choices=[{'index': 0, 'message': message, 'finish_reason': 'stop'}],
                     usage={'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})
        return httpx.Response(200, request=request, json=value)

    sys.path.insert(0, str(ROOT / 'backend/coconut'))
    from coconut.hermes_worker import run_turn
    events = []

    def emit(kind, payload):
        events.append((kind, payload))
        if kind == 'failed' and sys.exception() is not None:
            import traceback
            traceback.print_exception(sys.exception())

    def receipt(kind, payload):
        if kind == 'native_proposal':
            proposals.append(payload)
            return {'status': 'recorded', 'decision_id': 'changed-profile-card'}
        raise AssertionError('The prompt check requested an effect: ' + kind)

    decision = {'id': 'ready-only-card', 'status': 'approved', 'action_type': 'question',
                'title': 'Mark PR #42 ready for a manual merge?', 'selected_option_id': 'ready',
                'selected_option_label': 'Mark #42 ready for my merge',
                'answer': 'Mark #42 ready for my merge', 'request_text': 'Prepare PR #42.'}
    original_decision = json.dumps(decision, sort_keys=True)
    request = {'operation': 'start',
        'task': {'id': '00000000-0000-4000-8000-000000000011', 'title': 'Report the current policy.'},
        'bot': {'id': '00000000-0000-4000-8000-000000000003', 'session_id': SESSION,
                'name': 'Prompt Fixture', 'instructions': ''},
        'history': [], 'model': 'gpt-6-sol', 'provider': 'openai-api',
        'runtime_config': {'provider': 'openai-api', 'api_mode': 'chat_completions',
                           'base_url': 'https://sol.invalid/v1', 'api_key': 'synthetic-sol-key'}}
    hooks = []
    context = PluginContext(PluginManifest(name='prompt-refresh-check', version='1'), get_plugin_manager())
    registration = context.register_hook('on_session_start', lambda **payload: hooks.append(payload['session_id']))
    revision_key = 'coconut.prompt-input-revision.' + SESSION
    phases = (('initial', 1), ('unchanged', 1), ('user', 2), ('user_unchanged', 2), ('soul', 3),
              ('during_turn', 3), ('after_edit', 4), ('after_edit_unchanged', 4), ('legacy', 5), ('legacy_unchanged', 5))
    with ExitStack() as cleanup:
        cleanup.callback(registration.dispose)
        cleanup.enter_context(patch.object(httpx.Client, 'send', reply))
        cleanup.enter_context(configuration(home))
        for phase, hook_count in phases:
            operation = ('start' if phase in ('initial', 'unchanged') else
                         'resume' if phase.startswith('user') or phase == 'during_turn' else 'context')
            if phase == 'user':
                (memories / 'USER.md').write_text(new_rule)
                request['decision'] = decision
            if phase == 'soul':
                (home / 'SOUL.md').write_text(new_identity)
            if phase == 'legacy':
                db = SessionDB()
                db.set_meta(revision_key, '')
                db.close()
            if operation == 'context':
                request['task']['pending_decision'] = decision
                request['question'] = 'Does the recorded choice permit a merge?'
            request['operation'] = operation
            before_revision = prompt_revision(home)
            previous = len(sent)
            assert run_turn(request, emit, receipt) == 0, events
            assert len(sent) == previous + 1, sent
            assert hooks == [SESSION] * hook_count, (phase, hooks)
            current = sent[-1]
            system = current[0]['content']
            assert current[0]['role'] == 'system', current[0]
            if operation == 'start':
                assert old_rule in system and old_identity in system, system
            else:
                assert new_rule in system and old_rule not in system, 'The worker kept the old USER.md policy.'
                identity = old_identity if phase.startswith('user') else new_identity
                assert identity in system, 'The worker kept an old SOUL.md identity.'
                if operation == 'context':
                    assert old_identity not in system, 'The worker retained the old SOUL.md identity.'
                assert decision['selected_option_label'] in current[-1]['content'], current[-1]
            tools = [message for message in current if message['role'] == 'tool']
            assert any(message['tool_call_id'] == 'receipt-fixture-17'
                       and message['content'] == 'Original effect confirmed. Do not repeat.' for message in tools), tools
            assert any(message.get('content') == 'Earlier request 0.' for message in current), 'The worker lost early history.'
            db = SessionDB()
            assert db.get_meta(revision_key) == before_revision, 'The worker recorded files that its prompt did not load.'
            if phase == 'during_turn':
                assert before_revision != prompt_revision(home), 'The fixture did not edit memory during the turn.'
                assert len(proposals) == 1, proposals
                assert proposals[0]['_worker_profile_revision'] == before_revision, 'The card claimed the newer disk revision.'
                assert proposals[0]['_worker_profile_revision'] != prompt_revision(home), 'The worker trusted a model field.'
            expected_memory = new_memory if phase.startswith('after_edit') or phase.startswith('legacy') else old_memory
            assert expected_memory in system, (phase, 'The worker did not load the expected memory revision.')
            assert db.resolve_resume_session_id(SESSION) == SESSION, 'The worker changed the execution identity.'
            after = db.get_messages(SESSION)
            assert after[:len(before)] == before, 'The worker changed saved messages or tool receipts.'
            if operation != 'start':
                assert new_rule in db.get_session(SESSION)['system_prompt']
            db.close()
            assert json.dumps(decision, sort_keys=True) == original_decision, 'The worker changed the recorded card choice.'
    print('Native prompt refresh passed: changed files rebuild once; unchanged turns keep session hooks, history, receipts, and card scope.')


def run(source, python):
    with tempfile.TemporaryDirectory(prefix='native-prompt-refresh-') as temporary:
        home = Path(temporary) / 'bots' / '00000000-0000-4000-8000-000000000003' / 'home'
        home.mkdir(parents=True)
        env = {key: os.environ[key] for key in ('PATH',) if key in os.environ}
        env.update(HOME=temporary, HERMES_HOME=str(home), TMPDIR=temporary, HERMES_DISABLE_MCP='1',
                   HERMES_SKIP_SKILL_SYNC='1', PYTHONDONTWRITEBYTECODE='1', HERMES_DISABLE_LAZY_INSTALLS='1',
                   PYTHONPATH=os.pathsep.join((str(source), str(ROOT / 'backend'))))
        result = subprocess.run([python, str(Path(__file__).resolve()), '--child'], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=120)
        print(result.stdout, result.stderr if result.returncode else '', end='')
        if result.returncode:
            raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--python')
    parser.add_argument('--child', action='store_true')
    args = parser.parse_args()
    if args.child:
        child()
    else:
        run(args.source, args.python)
