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


def verify_contract():
    from run_agent import AIAgent
    from hermes_state import SessionDB
    from tools.registry import registry
    from toolsets import create_custom_toolset, get_toolset
    from hermes_cli import auth, config, models, profiles, runtime_provider, model_switch_providers
    from hermes_constants import (parse_reasoning_effort, resolve_reasoning_config,
                                  set_hermes_home_override, reset_hermes_home_override)
    from agent.reasoning_effort import codex_supported_efforts
    from utils import atomic_roundtrip_yaml_save
    from .hermes_worker import BOT_CREATION_SCHEMA, BOT_DELETION_SCHEMA, PROPOSAL_SCHEMA
    from .native_bridge import QUESTION_SCHEMA
    from cron import jobs
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
                     approval.register_gateway_notify, approval.resolve_gateway_approval, approval_context._get_approval_config):
        if not callable(function):
            raise ValueError('Hermes removed a required profile or scheduling interface.')
    if set(approval_context._VALID_MODES) != {'off', 'smart', 'manual'}:
        raise ValueError('Hermes approval modes changed. Review the adapter before updating.')
    from hermes_cli.plugins import PluginContext
    from hermes_cli.middleware import run_tool_execution_middleware
    require_keywords(PluginContext.register_middleware, {'kind', 'callback'})
    require_keywords(run_tool_execution_middleware, {'tool_name', 'args', 'next_call'})
    from .model_catalog import _read_profile, _write_profile

    require_keywords(AIAgent.__init__, {
        'model', 'provider', 'api_key', 'base_url', 'api_mode', 'reasoning_config',
        'enabled_toolsets', 'quiet_mode', 'ephemeral_system_prompt', 'session_id',
        'skip_context_files', 'skip_memory', 'skip_background_review', 'save_trajectories',
        'max_tokens', 'max_iterations', 'run_budget_seconds', 'load_soul_identity', 'gateway_session_key', 'clarify_callback', 'disabled_toolsets', 'session_db',
    })
    require_keywords(AIAgent.run_conversation, {'conversation_history', 'task_id', 'stream_callback', 'persist_user_message', 'persist_user_platform_id'})
    for name in ('close',):
        if not callable(getattr(AIAgent, name, None)):
            raise ValueError('Hermes removed native conversation cleanup.')
    for name in ('get_meta', 'set_meta', 'set_session_title', 'create_session', 'replace_messages', 'resolve_resume_session_id', 'get_resume_conversations', 'get_session', 'end_session', 'close'):
        if not callable(getattr(SessionDB, name, None)):
            raise ValueError('Hermes removed a required conversation storage interface: ' + name)
    require_keywords(SessionDB.replace_messages, {'archive_dropped', 'reject_active_turn_lease'})
    require_keywords(registry.register, {'name', 'toolset', 'schema', 'handler'})
    require_keywords(registry.dispatch, {'name', 'args'})
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
        auth._provider_state_in, auth._load_auth_store, auth._global_auth_file_path,
        parse_reasoning_effort, resolve_reasoning_config, codex_supported_efforts,
        set_hermes_home_override, reset_hermes_home_override, atomic_roundtrip_yaml_save,
    ):
        if not callable(function):
            raise ValueError('Hermes removed a required native settings interface.')

    from tools.bot_relay import write_remote_roster, claim_pending_envelopes, write_reply
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

    return {'provider_contract': 'compatible', 'tool_registration': True, 'tool_dispatch': True,
            'native_model_effort_roundtrip': True, 'profile_inheritance': True, 'model_request_sent': False}
