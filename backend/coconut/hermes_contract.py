"""Check the installed Hermes interfaces without a model request."""
from __future__ import annotations

import inspect
import json
from pathlib import Path
import tempfile


def require_keywords(function, names):
    parameters = inspect.signature(function).parameters
    missing = sorted(name for name in names if name not in parameters or
                     parameters[name].kind == inspect.Parameter.POSITIONAL_ONLY)
    if missing:
        raise ValueError(f'Hermes API changed in {function.__qualname__}: ' + ', '.join(missing))


def verify_session_lifecycle_contract():
    """Session maintenance reads the store path and the end fields of a session row. Check them on a scratch store."""
    from hermes_state import SessionDB
    with tempfile.TemporaryDirectory(prefix='coconut-session-contract-') as temporary:
        db = SessionDB(db_path=Path(temporary) / 'state.db')
        try:
            if Path(db.db_path) != Path(temporary) / 'state.db':
                raise ValueError('Hermes no longer exposes the session store path.')
            db.create_session('coconut-contract', source='coconut')
            db.end_session('coconut-contract', 'coconut_idle')
            ended = db.get_session('coconut-contract')
            if not ended.get('ended_at') or ended.get('end_reason') != 'coconut_idle':
                raise ValueError('Hermes changed the end fields of a session.')
            db.reopen_session('coconut-contract')
            if db.get_session('coconut-contract').get('ended_at'):
                raise ValueError('Hermes no longer reopens an ended session.')
        finally:
            db.close()


def verify_contract():
    import mcp
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch, kanban_db_workspace
    require_keywords(kanban_db_dispatch.dispatch_once, {'conn', 'spawn_fn', 'max_spawn', 'max_in_progress', 'reconcile_orphans'})
    require_keywords(kanban_db_dispatch.check_respawn_guard, {'conn', 'task_id'})
    require_keywords(kanban_db.create_task, {'conn', 'title', 'initial_status', 'idempotency_key', 'tenant', 'workspace_kind', 'workspace_path'})
    for operation in (kanban_db.complete_task, kanban_db.request_review, kanban_db.block_task, kanban_db.schedule_task):
        require_keywords(operation, {'conn', 'task_id', 'expected_run_id'})
    require_keywords(kanban_db_workspace.resolve_workspace, {'task'})
    assert callable(kanban_db_connect.connect) and callable(kanban_db.reopen_review_task)
    # The phone shows Hermes' own diagnostics and archives idle tasks with Hermes' archive.
    from hermes_cli.kanban_diagnostics import compute_task_diagnostics
    require_keywords(compute_task_diagnostics, {'task', 'events', 'runs'})
    require_keywords(kanban_db.archive_task, {'conn', 'task_id'})
    require_keywords(kanban_db.list_events, {'conn', 'task_id'})
    require_keywords(kanban_db.list_runs, {'conn', 'task_id'})
    from hermes_cli.mcp_config import _probe_single_server, _save_bearer_auth_token
    from tools.mcp_tool_config import _load_mcp_config
    from tools.mcp_tool_common import mcp_server_enabled
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    require_keywords(_probe_single_server, ['name', 'config', 'connect_timeout'])
    require_keywords(_save_bearer_auth_token, ['name', 'token'])
    from run_agent import AIAgent
    from hermes_state import SessionDB
    from tools.registry import registry
    from tools.process_registry import process_registry
    from tools.process_registry import CHECKPOINT_PATH
    if not isinstance(CHECKPOINT_PATH, Path):
        raise ValueError('Hermes changed the native process checkpoint path.')
    from tools import browser_tool
    if not isinstance(browser_tool._active_sessions, dict) or not hasattr(browser_tool._cleanup_lock, '__enter__'):
        raise ValueError('Hermes changed the native browser session registry.')
    # A stopping worker stops the harness daemons that it drove in parallel (hermes_worker.stop_browser_daemons).
    from tools import browser_use_cli
    if (not isinstance(browser_use_cli._driven_daemons, dict)
            or not hasattr(browser_use_cli._driven_daemons_lock, '__enter__')
            or not callable(browser_use_cli._reload_daemons)):
        raise ValueError('Hermes changed the browser-harness daemon registry.')
    from tools.terminal_tool import register_task_env_overrides
    require_keywords(register_task_env_overrides, {'task_id', 'overrides'})
    from toolsets import create_custom_toolset, get_toolset
    from hermes_cli import auth, config, models, profiles, runtime_provider, model_switch_providers
    from hermes_constants import (parse_reasoning_effort, resolve_reasoning_config,
                                  set_hermes_home_override, reset_hermes_home_override)
    from agent.reasoning_effort import codex_supported_efforts
    from utils import atomic_roundtrip_yaml_save
    from hermes_yaml import roundtrip_yaml
    from .hermes_worker import BOT_CREATION_SCHEMA, BOT_DELETION_SCHEMA, PROPOSAL_SCHEMA
    from .native_bridge import QUESTION_SCHEMA
    from cron import jobs
    from tools.terminal_tool import record_session_cwd, clear_session_cwd
    assert callable(record_session_cwd) and callable(clear_session_cwd)
    import cron.scheduler as scheduler
    from cron.scheduler import _is_cron_silence_response
    from cron.scheduler_prompt import _build_job_prompt, _guard_job_credential_exfil
    if hasattr(scheduler, '_check_model_drift'):
        require_keywords(scheduler._check_model_drift, {'job', 'job_id', 'cfg', 'runtime', 'primary_provider_for_drift', 'primary_model_for_drift'})
    elif hasattr(scheduler, '_snapshot_pin'):
        require_keywords(scheduler._snapshot_pin, {'job', 'axis', 'current', 'job_id'})
    require_keywords(_build_job_prompt, {'job'})
    require_keywords(_is_cron_silence_response, {'text'})
    require_keywords(_guard_job_credential_exfil, {'job'})
    from tools import approval, approval_context
    from hermes_cli.tools_config import _get_platform_tools
    require_keywords(jobs.claim_job_for_fire, {'job_id', 'force', 'return_job'})
    require_keywords(jobs.heartbeat_fire_claim, {'job_id', 'expected_owner'})
    require_keywords(jobs.fire_claim_fence, {'job_id', 'expected_owner'})
    require_keywords(jobs.mark_job_run, {'job_id', 'success', 'expected_fire_owner'})
    require_keywords(jobs._record_run_outcome, {'job', 'success', 'error', 'delivery_error', 'status', 'now'})
    require_keywords(jobs._advance_after_run, {'job', 'now'})
    require_keywords(jobs._under_fire_fence, {'job_id', 'fn'})
    require_keywords(jobs._with_job, {'job_id', 'fn', 'missing'})
    for function in (jobs.create_job, jobs.update_job, jobs.get_job, jobs.list_jobs, jobs.pause_job,
                     jobs.resume_job, jobs.remove_job, jobs.claim_dispatch, jobs.save_job_output, jobs.save_jobs, jobs._hermes_now,
                     profiles.read_profile_meta, profiles.write_profile_meta, _get_platform_tools,
                     approval.register_gateway_notify, approval.unregister_gateway_notify, approval.resolve_gateway_approval, approval_context._get_approval_config):
        if not callable(function):
            raise ValueError('Hermes removed a required profile or scheduling interface.')
    if set(approval_context._VALID_MODES) != {'off', 'smart', 'manual'}:
        raise ValueError('Hermes approval modes changed. Review the adapter before updating.')
    from hermes_cli.plugins import PluginContext
    from hermes_cli.middleware import run_tool_execution_middleware
    require_keywords(PluginContext.register_middleware, {'kind', 'callback'})
    require_keywords(PluginContext.register_hook, {'hook_name', 'callback'})
    require_keywords(run_tool_execution_middleware, {'tool_name', 'args', 'next_call'})
    from .model_catalog import _read_profile, _write_profile

    require_keywords(AIAgent.__init__, {
        'model', 'provider', 'api_key', 'base_url', 'api_mode', 'reasoning_config', 'fallback_model',
        'enabled_toolsets', 'quiet_mode', 'ephemeral_system_prompt', 'session_id',
        'skip_context_files', 'skip_memory', 'skip_background_review', 'save_trajectories',
        'max_tokens', 'max_iterations', 'run_budget_seconds', 'load_soul_identity', 'gateway_session_key', 'clarify_callback', 'interim_assistant_callback', 'disabled_toolsets', 'session_db',
    })
    require_keywords(AIAgent.run_conversation, {'conversation_history', 'task_id', 'stream_callback', 'persist_user_message', 'persist_user_platform_id'})
    for name in ('close', 'release_clients', 'hard_interrupt'):
        if not callable(getattr(AIAgent, name, None)):
            raise ValueError('Hermes removed native conversation cleanup.')
    for name in ('resolve_session_by_title', 'get_meta', 'set_meta', 'set_session_title', 'create_session', 'replace_messages', 'resolve_resume_session_id', 'get_resume_conversations', 'get_session', 'end_session', 'reopen_session', 'update_system_prompt',
                 'prune_sessions', 'vacuum', 'close'):
        if not callable(getattr(SessionDB, name, None)):
            raise ValueError('Hermes removed a required conversation storage interface: ' + name)
    require_keywords(SessionDB.replace_messages, {'archive_dropped', 'reject_active_turn_lease'})
    # The catalog offers only the reasoning efforts that Hermes sends to xAI.
    from agent.model_metadata import is_grok_46_family
    from agent.reasoning_effort import XAI_GROK46_EFFORTS, XAI_LEGACY_EFFORTS, clamp_effort
    assert callable(is_grok_46_family) and XAI_GROK46_EFFORTS and XAI_LEGACY_EFFORTS
    if clamp_effort('max', XAI_LEGACY_EFFORTS) != 'high':
        raise ValueError('Hermes changed how it clamps a reasoning effort.')
    from agent.context_compressor import ContextCompressor, pin_summary_route, take_pinned_summary_route
    from agent.auxiliary_client import AuxiliaryClientUnavailable
    assert callable(pin_summary_route) and callable(take_pinned_summary_route)
    assert issubclass(AuxiliaryClientUnavailable, Exception)
    require_keywords(pin_summary_route, {'route'})
    require_keywords(ContextCompressor._call_summary_llm, {'prompt', 'prompt_started_at'})
    require_keywords(ContextCompressor._generate_summary, {'turns_to_summarize'})
    for name, keywords in (
        ('_protect_head_size', {'messages'}),
        ('_find_tail_cut_by_tokens', {'messages', 'head_end'}),
        ('_find_last_assistant_message_idx', {'messages', 'head_end'}),
        ('_find_last_user_message_idx', {'messages', 'head_end'}),
        ('_real_user_indices_desc', {'messages', 'head_end'}),
        ('_align_boundary_forward', {'messages', 'idx'}),
        ('_strip_context_summary_handoff_message', {'message'}),
        ('_is_context_summary_message', {'message'}),
    ):
        require_keywords(getattr(ContextCompressor, name), keywords)
    from agent.context_compressor import _estimate_msg_budget_tokens
    from agent.conversation_compression import _is_real_user_message
    from agent.message_sanitization import tool_call_id_variants, tool_result_id_variants
    require_keywords(tool_call_id_variants, {'tc'})
    require_keywords(tool_result_id_variants, {'tool_call_id'})
    require_keywords(_estimate_msg_budget_tokens, {'msg'})
    assert callable(_is_real_user_message)
    # Coconut session maintenance calls these by keyword and reads the returned counts.
    require_keywords(SessionDB.prune_sessions, {'older_than_days', 'source', 'sessions_dir', 'exclude_active_write_guards'})
    from hermes_state_maintenance import _PRUNE_FILTER_NAMES
    if 'end_reason' not in _PRUNE_FILTER_NAMES:
        raise ValueError('Hermes no longer prunes sessions by end reason.')
    from hermes_state_holders import foreign_state_db_holders, in_process_state_db_holders
    require_keywords(in_process_state_db_holders, {'exclude'})
    require_keywords(registry.register, {'name', 'toolset', 'schema', 'handler'})
    require_keywords(registry.dispatch, {'name', 'args'})
    require_keywords(registry.deregister, {'name'})
    require_keywords(process_registry.kill_process, {'session_id', 'source', 'consume_output'})
    require_keywords(process_registry.drain_notifications, {'owns_event', 'skip_poll_observed'})
    require_keywords(process_registry.get, {'session_id'})
    require_keywords(process_registry.poll, {'session_id'})
    require_keywords(process_registry.is_completion_consumed, {'session_id'})
    from tools.process_registry_notifications import format_process_notification
    from gateway.session_context import set_session_vars, clear_session_vars
    require_keywords(set_session_vars, {'session_id', 'session_key', 'cwd', 'cron_session'})
    assert callable(clear_session_vars) and callable(format_process_notification)
    if not callable(process_registry.recover_from_checkpoint) or not callable(process_registry.list_sessions):
        raise ValueError('Hermes removed native background process cleanup.')
    require_keywords(create_custom_toolset, {'name', 'description', 'tools'})
    require_keywords(runtime_provider.resolve_runtime_provider, {'requested', 'target_model'})
    require_keywords(model_switch_providers.list_authenticated_providers, {
        'current_provider', 'current_model', 'current_base_url', 'user_providers', 'custom_providers',
        'excluded_providers', 'refresh', 'probe_custom_providers', 'probe_current_custom_provider', 'for_picker',
    })
    for function in (
        config.load_config, config.require_readable_config_before_write, config.split_model_config_default,
        config.clear_model_endpoint_credentials, config.get_config_path, config._normalize_root_model_keys,
        models.get_default_model_for_provider, profiles.resolve_profile_env, auth.resolve_provider,
        auth._provider_state_in, auth._load_auth_store, config.require_env_writable, config.save_env_value,
        parse_reasoning_effort, resolve_reasoning_config, codex_supported_efforts,
        set_hermes_home_override, reset_hermes_home_override, atomic_roundtrip_yaml_save, roundtrip_yaml,
    ):
        if not callable(function):
            raise ValueError('Hermes removed a required native settings interface.')

    from agent.interrupt_scope import InterruptScope, bind_interrupt_scope
    require_keywords(InterruptScope.cancel, {'reason'})
    require_keywords(bind_interrupt_scope, {'scope'})
    from tools.bot_relay import enqueue_envelope, write_remote_roster, claim_pending_envelopes, write_reply
    require_keywords(enqueue_envelope, {'root', 'target', 'message', 'sender_profile', 'sender_handle'})
    from tools.bot_mode_probe import BOT_CHAT_TITLE
    from tools.bot_mode_dm import message_agent_tool_schema, message_agent_tool
    from utils import atomic_yaml_write
    assert BOT_CHAT_TITLE == 'Bot Chat'
    native_message = message_agent_tool_schema()['function']
    assert native_message['name'] == 'message_agent'
    assert set(native_message['parameters']['required']) == {'target', 'message'}
    require_keywords(message_agent_tool, {'target', 'message', 'agent'})
    require_keywords(write_remote_roster, {'root'})
    require_keywords(claim_pending_envelopes, {'root'})
    require_keywords(write_reply, {'root', 'envelope_id', 'reply', 'error'})
    assert callable(atomic_yaml_write)

    schemas = [PROPOSAL_SCHEMA, BOT_CREATION_SCHEMA, BOT_DELETION_SCHEMA, QUESTION_SCHEMA]
    names = {schema['name'] for schema in schemas}
    for schema in schemas:
        registry.register(name=schema['name'], toolset='coconut', schema=schema,
                          handler=lambda arguments, **kwargs: json.dumps({'received': arguments}))
    create_custom_toolset('coconut', 'Coconut compatibility check', tools=sorted(names))
    if set(get_toolset('coconut')['tools']) != names:
        raise ValueError('Hermes did not retain the exact Coconut toolset.')
    definitions = registry.get_definitions(names, quiet=True)
    returned = {row['function']['name']: row['function'] for row in definitions if row.get('type') == 'function'}
    if returned != {schema['name']: schema for schema in schemas}:
        raise ValueError('Hermes changed the Coconut tool schemas.')
    arguments = {'name': 'Compatibility fixture', 'role': 'Test', 'instructions': 'Test instructions.',
                 'source_message_id': '00000000-0000-4000-8000-000000000001'}
    if json.loads(registry.dispatch('coconut_create_bot', arguments)) != {'received': arguments}:
        raise ValueError('Hermes changed the tool dispatch result.')
    deletion = {'source_message_id': arguments['source_message_id'],
                'target_bot_id': '00000000-0000-4000-8000-000000000002', 'expected_name': arguments['name']}
    if json.loads(registry.dispatch('coconut_delete_bot', deletion)) != {'received': deletion}:
        raise ValueError('Hermes changed the deletion tool dispatch result.')

    from hermes_cli.plugins import PluginManifest, get_plugin_manager
    calls = []
    def record(tool_name, args, next_call, **kwargs):
        calls.append(tool_name)
        return next_call(args)
    registration = PluginContext(PluginManifest(name='coconut-contract', version='1'), get_plugin_manager()).register_middleware(
        'tool_execution', record)
    if not callable(getattr(registration, 'dispose', None)):
        raise ValueError('Hermes removed middleware cleanup between turns.')
    def dispatch():
        return run_tool_execution_middleware(tool_name='coconut_create_bot', args=arguments,
                                             next_call=lambda args: registry.dispatch('coconut_create_bot', args))
    try:
        dispatch()
    finally:
        registration.dispose()
    dispatch()
    if calls != ['coconut_create_bot']:
        raise ValueError('Hermes did not remove the previous turn middleware.')
    from types import SimpleNamespace
    from hermes_cli.plugins import invoke_hook
    from .turn_timings import TurnTrace, native_request_timings
    traces = []
    trace = TurnTrace({'id': 'request-contract'}, 'start', lambda kind, data: traces.append(data))
    hooks = ('pre_api_request', 'post_api_request', 'api_request_error')
    with native_request_timings(trace, SimpleNamespace(session_id='request-contract')):
        for hook in hooks:
            invoke_hook(hook, task_id='request-contract', session_id='request-contract', api_call_count=1)
    for hook in hooks:
        invoke_hook(hook, task_id='request-contract', session_id='request-contract', api_call_count=2)
    if [row['stage'] for row in traces] != ['api_request_start', 'api_request_end', 'api_request_error']:
        raise ValueError('Hermes changed request hook delivery or cleanup.')
    for name in names:
        registry.deregister(name)
    if registry.get_definitions(names, quiet=True):
        raise ValueError('Hermes retained tools from the previous turn.')

    # These names are test values. The check never contacts a model provider.
    inherited = {'model': 'coconut-contract-default', 'provider': 'openai', 'base_url': None,
                 'reasoning_effort': 'low'}
    selection = {'model': 'coconut-contract-override', 'provider': 'openai', 'reasoning_effort': 'low'}
    catalog = {'default': inherited, 'models': [
        {'id': selection['model'], 'provider': 'openai', 'reasoning_efforts': ['low'], 'configured': True},
    ]}
    with tempfile.TemporaryDirectory(prefix='coconut-contract-') as temporary:
        home = Path(temporary)
        path = home / 'config.yaml'
        path.write_text('display:\n  theme: coconut-contract\n')
        if _read_profile(home, inherited)['model_override'] is not None:
            raise ValueError('An empty Hermes profile no longer inherits the default model.')
        saved = _write_profile(home, selection, catalog)
        if saved['model_override'] != selection:
            raise ValueError('Hermes did not retain the model and effort override.')
        cleared = _write_profile(home, None, catalog)
        if cleared['model_override'] is not None:
            raise ValueError('Hermes did not restore default model inheritance.')
        if config.require_readable_config_before_write(path)['display']['theme'] != 'coconut-contract':
            raise ValueError('The profile update changed unrelated Hermes settings.')

    verify_session_lifecycle_contract()
    verify_parallel_work_contract()
    verify_delegation_contract()
    verify_terminal_contract()
    verify_skills_contract()
    verify_profile_api_contract()

    return {'native_terminal_path': True, 'provider_contract': 'compatible', 'tool_registration': True, 'tool_dispatch': True,
            'native_model_effort_roundtrip': True,
            'native_essential_skills': True, 'model_request_sent': False}


def verify_skills_contract():
    from uuid import uuid4
    from agent.skill_utils import ESSENTIAL_SKILLS
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.skills_sync import sync_skills
    from .profiles import HermesProfiles
    with tempfile.TemporaryDirectory(prefix='coconut-skills-contract-') as temporary:
        profiles = HermesProfiles(temporary)
        bot = dict(id=str(uuid4()), session_id=str(uuid4()), name='Skills check')
        profiles.sync(bot)
        home = profiles.home(bot)
        token = set_hermes_home_override(str(home))
        try:
            result = sync_skills(quiet=True)
            if not ESSENTIAL_SKILLS or set(result['copied']) != set(ESSENTIAL_SKILLS):
                raise ValueError('Hermes did not seed exactly its essential skills.')
            custom = home / 'skills' / 'custom-check' / 'SKILL.md'
            custom.parent.mkdir(parents=True)
            custom.write_text('A user-created skill.\n')
            result = sync_skills(quiet=True)
            if result['copied'] or not result.get('skipped_opt_out'):
                raise ValueError('Hermes added bundled skills after profile setup.')
            if custom.read_text() != 'A user-created skill.\n':
                raise ValueError('Hermes changed a user-created skill during sync.')
        finally:
            reset_hermes_home_override(token)


def verify_profile_api_contract():
    """Create and delete a profile with the Hermes profile API, as bot_profiles does, in a temporary Hermes home."""
    import os
    import sys
    import hermes_cli
    from .bot_profiles import run_profile_command
    source = Path(hermes_cli.__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='coconut-profile-contract-') as temporary:
        root = Path(temporary)
        hermes_home = root / '.hermes'
        hermes_home.mkdir()
        (hermes_home / 'config.yaml').write_text('{}\n')
        # HOME is the temporary folder too, so the `<name>` command goes into its .local/bin.
        environment = dict(PATH=os.environ.get('PATH', '/usr/bin:/bin'), HOME=str(root), TMPDIR=str(root))
        created = run_profile_command(sys.executable, source, hermes_home, 'create', environment=environment,
                                      name='coconut-check', display_name='Coconut check', description='A check.',
                                      soul='Contract soul.\n', skills=str(root / 'skills'),
                                      coconut=str(Path(__file__).resolve().parent))
        home = hermes_home / 'profiles' / 'coconut-check'
        if Path(created['home']).resolve() != home.resolve() or (home / 'SOUL.md').read_text() != 'Contract soul.\n':
            raise ValueError('Hermes did not create the profile with its SOUL.')
        if created.get('skills') != 'added' or str(root / 'skills') not in (home / 'config.yaml').read_text():
            raise ValueError('Coconut could not add its skills folder to a new profile: ' + str(created.get('skills_error')))
        run_profile_command(sys.executable, source, hermes_home, 'delete', environment=environment, name='coconut-check')
        if home.exists():
            raise ValueError('Hermes did not delete the profile.')


def verify_terminal_contract():
    import os
    from unittest.mock import patch
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.environments import local
    from tools.environments.local import LocalEnvironment
    from .terminal_setup import configure_terminal
    with tempfile.TemporaryDirectory(prefix='coconut-terminal-contract-') as temporary:
        directory = Path(temporary)
        home = directory / 'home'
        home.mkdir()
        (home / 'config.yaml').write_text('terminal:\n  auto_source_bashrc: false\n')
        token = set_hermes_home_override(str(home))
        original = local._resolve_shell_init_files
        try:
            with patch.dict(os.environ, {'HOME': str(home), 'HERMES_HOME': str(home), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}):
                configure_terminal(home)
                configure_terminal(home)
                files = local._resolve_shell_init_files()
                if files.count(str(directory / 'terminal' / 'coconut-terminal.sh')) != 1:
                    raise ValueError('Hermes did not load Coconut terminal initialization.')
                terminal = LocalEnvironment(cwd=str(home), timeout=10)
                try:
                    result = terminal.execute('printf "%s" "$PATH"')
                    paths = result['output'].strip().split(':')
                    for prefix in ('/opt/homebrew', '/usr/local'):
                        if Path(prefix + '/bin').is_dir() and paths.index(prefix + '/bin') > paths.index('/usr/bin'):
                            raise ValueError('Native Hermes terminal puts system tools before package tools.')
                    if result.get('returncode') != 0:
                        raise ValueError('The native Hermes terminal check failed.')
                finally:
                    terminal.cleanup()
        finally:
            local._resolve_shell_init_files = original
            reset_hermes_home_override(token)


def verify_delegation_contract():
    import os
    import threading
    import time
    from unittest.mock import patch
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools import async_delegation as native
    from .native_processes import delegation_completion
    from agent.inline_tool_executors import tool_hook_ids
    from types import SimpleNamespace
    if tool_hook_ids(SimpleNamespace(), 'coconut-child', None).get('task_id') != 'coconut-child':
        raise ValueError('Hermes removed task ownership from tool middleware.')

    with tempfile.TemporaryDirectory(prefix='coconut-delegation-contract-') as temporary:
        token = set_hermes_home_override(temporary)
        release = threading.Event()
        child = {}
        try:
            def runner():
                child.update(pid=os.getpid(), outcome=os.environ.get('COCONUT_WORKSPACE_OUTCOME'))
                release.wait(5)
                return {'status': 'completed', 'summary': 'Coconut delegation contract'}
            # Coconut ends an interrupted delegate_task receipt from the worker's process proof. That proof holds only
            # while a child runs inside the worker process with the worker's COCONUT_WORKSPACE_OUTCOME.
            with patch.dict(os.environ, {'COCONUT_WORKSPACE_OUTCOME': 'coconut-contract'}):
                result = native.dispatch_async_delegation(goal='Check durable delivery', context=None, toolsets=None,
                            role='general', model=None, session_key='coconut-contract', runner=runner)
                deadline = time.monotonic() + 10
                while not child and time.monotonic() < deadline:
                    time.sleep(.05)
            if child != dict(pid=os.getpid(), outcome='coconut-contract'):
                raise ValueError('Hermes moved a delegated child out of the worker process or its outcome variable.')
            item = dict(id='receipt', task_id='coconut-contract', delegation_ids=[result['delegation_id']])
            if delegation_completion(item)[0].get('observation') != 'running':
                raise ValueError('Hermes lost the running delegation.')
            release.set()
            deadline = time.monotonic() + 10
            while native.get_durable_delegation(result['delegation_id'])['state'] in ('running', 'finalizing'):
                if time.monotonic() >= deadline:
                    raise ValueError('Hermes did not save the delegation result.')
                time.sleep(.05)
            if 'Coconut delegation contract' not in delegation_completion(item)[0].get('text', ''):
                raise ValueError('Hermes changed the durable delegation result.')
            if native.get_durable_delegation(result['delegation_id'])['delivery_state'] != 'pending':
                raise ValueError('Reading a delegation acknowledged it too early.')
            if delegation_completion(item | {'task_id': 'another-task'}):
                raise ValueError('A delegation result crossed task boundaries.')
            delegation_completion(item | {'acknowledge': True})
            if delegation_completion(item)[0].get('consumed') is not True:
                raise ValueError('Hermes did not acknowledge the saved delegation.')
        finally:
            release.set()
            reset_hermes_home_override(token)


def verify_parallel_work_contract():
    import os
    from unittest.mock import patch
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.delegate_tool_config import _get_max_concurrent_children
    from .parallel_work import prepare_turn, save, state
    with tempfile.TemporaryDirectory(prefix='coconut-workers-contract-') as temporary:
        root = Path(temporary)
        home = root / 'bot'
        home.mkdir()
        token = set_hermes_home_override(str(home))
        try:
            with patch.dict(os.environ, {'HERMES_HOME': str(home)}):
                save(root, 6)
                save(home, None, inherit=True)
                prepare_turn(state(root)['effective_limit'])
                if _get_max_concurrent_children() != 6:
                    raise ValueError('Hermes did not use the worker limit of the Coconut settings.')
                save(home, 3)
                if _get_max_concurrent_children() != 3:
                    raise ValueError('Hermes did not use the bot worker limit.')
                save(root, 8)
                save(home, None, inherit=True)
                prepare_turn(state(root)['effective_limit'])
                if _get_max_concurrent_children() != 8:
                    raise ValueError('Hermes did not apply the changed worker limit.')
        finally:
            reset_hermes_home_override(token)
