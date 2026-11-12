"""Check the installed Hermes interfaces without a model request."""
from __future__ import annotations

import inspect
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
        finally:
            db.close()


def verify_contract():
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
    # The MCP settings of a bot (native_mcp).
    from hermes_cli.mcp_config import _probe_single_server, _save_bearer_auth_token
    require_keywords(_probe_single_server, ['name', 'config', 'connect_timeout'])
    require_keywords(_save_bearer_auth_token, ['name', 'token'])
    from hermes_state import SessionDB
    from hermes_cli import auth, config, models, profiles, model_switch_providers
    from hermes_constants import (parse_reasoning_effort, resolve_reasoning_config,
                                  set_hermes_home_override, reset_hermes_home_override)
    from agent.reasoning_effort import codex_supported_efforts
    from utils import atomic_roundtrip_yaml_save
    from hermes_yaml import roundtrip_yaml
    # The routines of a bot (native_routines) and the silence of a cron run (silent_responses).
    from cron import jobs
    from cron.scheduler import _is_cron_silence_response
    require_keywords(_is_cron_silence_response, {'text'})
    from tools import approval_context
    require_keywords(jobs.trigger_job, {'job_id'})
    for function in (jobs.create_job, jobs.update_job, jobs.list_jobs, jobs.pause_job, jobs.resume_job, jobs.remove_job,
                     profiles.read_profile_meta, profiles.write_profile_meta, approval_context._get_approval_config):
        if not callable(function):
            raise ValueError('Hermes removed a required profile or scheduling interface.')
    if set(approval_context._VALID_MODES) != {'off', 'smart', 'manual'}:
        raise ValueError('Hermes approval modes changed. Review the adapter before updating.')
    # The coconut plugin registers hooks and request middleware (hermes_plugin/coconut/hooks.py).
    from hermes_cli.plugins import PluginContext
    require_keywords(PluginContext.register_middleware, {'kind', 'callback'})
    require_keywords(PluginContext.register_hook, {'hook_name', 'callback'})
    from .model_catalog import _read_profile, _write_profile

    # Session maintenance (native_sessions), the conversation of a chat (hooks.join_conversation) and the records that
    # a session holds (hooks.add_records).
    for name in ('get_meta', 'set_meta', 'get_session', 'end_session', 'resolve_resume_session_id',
                 'get_resume_conversations', 'get_messages_as_conversation', 'prune_sessions', 'vacuum', 'close'):
        if not callable(getattr(SessionDB, name, None)):
            raise ValueError('Hermes removed a required conversation storage interface: ' + name)
    # The catalog offers only the reasoning efforts that Hermes sends to xAI.
    from agent.model_metadata import is_grok_46_family
    from agent.reasoning_effort import XAI_GROK46_EFFORTS, XAI_LEGACY_EFFORTS, clamp_effort
    assert callable(is_grok_46_family) and XAI_GROK46_EFFORTS and XAI_LEGACY_EFFORTS
    if clamp_effort('max', XAI_LEGACY_EFFORTS) != 'high':
        raise ValueError('Hermes changed how it clamps a reasoning effort.')
    # Coconut session maintenance calls these by keyword and reads the returned counts.
    require_keywords(SessionDB.prune_sessions, {'older_than_days', 'source', 'sessions_dir', 'exclude_active_write_guards'})
    from hermes_state_maintenance import _PRUNE_FILTER_NAMES
    if 'end_reason' not in _PRUNE_FILTER_NAMES:
        raise ValueError('Hermes no longer prunes sessions by end reason.')
    from hermes_state_holders import foreign_state_db_holders, in_process_state_db_holders
    assert callable(foreign_state_db_holders)
    require_keywords(in_process_state_db_holders, {'exclude'})
    require_keywords(model_switch_providers.list_authenticated_providers, {
        'current_provider', 'current_model', 'current_base_url', 'user_providers', 'custom_providers',
        'excluded_providers', 'refresh', 'probe_custom_providers', 'probe_current_custom_provider', 'for_picker',
    })
    for function in (
        config.load_config, config.require_readable_config_before_write, config.split_model_config_default,
        config.clear_model_endpoint_credentials, config.get_config_path, config._normalize_root_model_keys,
        models.get_default_model_for_provider, profiles.resolve_profile_env, auth.resolve_provider,
        config.save_env_value, parse_reasoning_effort, resolve_reasoning_config, codex_supported_efforts,
        set_hermes_home_override, reset_hermes_home_override, atomic_roundtrip_yaml_save, roundtrip_yaml,
    ):
        if not callable(function):
            raise ValueError('Hermes removed a required native settings interface.')

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
    verify_gateway_contract()
    verify_parallel_work_contract()
    verify_terminal_contract()
    verify_profile_api_contract()

    return {'native_terminal_path': True, 'provider_contract': 'compatible',
            'native_model_effort_roundtrip': True, 'native_profile_api': True, 'model_request_sent': False}


def verify_gateway_contract():
    """The interfaces of the gateway that the `coconut` plugin uses (hermes_plugin/coconut).

    Most are public plugin interfaces. Three are not: the startup restore flag of the runner, which has no public signal
    (the adapter takes no turn before it ends), the session check of the runner (a Coconut turn waits while the gateway
    runs another turn in its session), and the session store methods that the pre_gateway_dispatch hook receives. A Hermes that changes them fails here instead of in a chat.
    """
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry, SessionStore
    from gateway.session_context import get_session_env
    from hermes_cli.plugins import VALID_HOOKS
    from tools.approval import resolve_gateway_approval
    from tools.clarify_gateway import resolve_gateway_clarify
    if not isinstance(getattr(GatewayRunner, '_startup_restore_in_progress', None), bool):
        raise ValueError('Hermes changed the startup restore of the gateway. Review the coconut adapter.')
    # A Coconut turn waits while the gateway runs another turn in its session (hooks.session_busy).
    require_keywords(GatewayRunner._is_session_running, ['session_key'])
    require_keywords(GatewayRunner._session_key_for_source, ['source'])
    missing = {'pre_gateway_dispatch', 'on_session_start', 'on_interim_message', 'post_llm_call', 'on_session_end',
               'pre_tool_call', 'post_api_request', 'on_session_reset'} - set(VALID_HOOKS)
    if missing:
        raise ValueError('Hermes removed plugin hooks of the coconut platform: ' + ', '.join(sorted(missing)))
    # The card rules go into the system prompt of each Coconut session with the hint of the platform
    # (hermes_plugin/coconut/__init__.py, platform_hint). The registration leaves out a field that Hermes does not know.
    from gateway.platform_registry import PlatformEntry
    if 'platform_hint' not in PlatformEntry.__dataclass_fields__:
        raise ValueError('Hermes removed the platform hint of a plugin platform, which holds the card rules of Coconut.')
    require_keywords(BasePlatformAdapter.send_clarify, ['chat_id', 'question', 'choices', 'clarify_id', 'session_key'])
    require_keywords(BasePlatformAdapter.on_processing_complete, ['event', 'outcome'])
    require_keywords(SessionStore.get_or_create_session, ['source', 'force_new'])
    require_keywords(SessionStore.switch_session, ['session_key', 'target_session_id'])
    require_keywords(resolve_gateway_approval, ['session_key', 'choice', 'reason'])
    require_keywords(resolve_gateway_clarify, ['clarify_id', 'response'])
    if not callable(get_session_env) or 'session_key' not in SessionEntry.__dataclass_fields__:
        raise ValueError('Hermes changed the gateway session context.')
    # A cron run of the gateway finds its job through the identity that Hermes binds for each fire.
    from cron.execution_identity import CronExecution, current_cron_execution
    if not callable(current_cron_execution) or not {'job_id', 'job_name', 'execution_id'} <= set(
            CronExecution.__dataclass_fields__):
        raise ValueError('Hermes changed the identity of a cron run.')
    # A reaction turn may stay silent. Without reply_expected=False, Hermes replaces a silence marker with a notice.
    from gateway.platforms.event import MessageEvent
    if 'reply_expected' not in getattr(MessageEvent, '__dataclass_fields__', {}):
        raise ValueError('Hermes changed how a platform marks a message that needs no reply.')
    # The plugin compresses a chat after a turn whose prompt reached the compression threshold (hooks.note_prompt_size).
    from agent.context_compressor import ContextCompressor
    from hermes_cli.config import load_config_readonly
    require_keywords(ContextCompressor._compute_threshold_tokens, ['context_length', 'threshold_percent', 'max_tokens'])
    require_keywords(ContextCompressor._effective_threshold_percent, ['context_length', 'threshold_percent'])
    if not callable(load_config_readonly):
        raise ValueError('Hermes changed how a plugin reads its config. Review the coconut hooks.')
    # A document tool outside a Coconut chat turn names its bot by the Hermes home of the running profile
    # (hermes_plugin/coconut/tools._outside_page). A multiplexing gateway scopes the home of each turn with a context
    # override, so the home must follow that override, or a bot could act for the profile of the process.
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    probe = '/coconut-contract-probe/profiles/probe'
    token = set_hermes_home_override(probe)
    try:
        scoped = str(get_hermes_home())
    finally:
        reset_hermes_home_override(token)
    if scoped != probe or str(get_hermes_home()) == probe:
        raise ValueError('Hermes changed how a turn scopes the home of its profile. Review the coconut document tools.')
    # A native outcome turn gets the board tools of a worker under Coconut names, with the schemas of Hermes's tools
    # (hermes_plugin/coconut/tools.claim_schemas), and the workspace of its claim (hooks.guard_tool).
    from tools import kanban_tools_schemas
    from tools.terminal_tool import register_task_env_overrides
    missing = [name for name in ('KANBAN_COMPLETE_SCHEMA', 'KANBAN_BLOCK_SCHEMA', 'KANBAN_SCHEDULE_SCHEMA',
                                 'KANBAN_HEARTBEAT_SCHEMA', 'KANBAN_REQUEST_REVIEW_SCHEMA', 'KANBAN_LINK_SCHEMA',
                                 'KANBAN_UNBLOCK_SCHEMA') if not isinstance(getattr(kanban_tools_schemas, name, None), dict)]
    if missing or not callable(register_task_env_overrides):
        raise ValueError('Hermes changed the board tools of a worker: ' + ', '.join(missing or ['terminal folders']))
    if not hasattr(BasePlatformAdapter, '_send_exec_approval_prompt'):
        raise ValueError('Hermes changed the approval prompt of a gateway platform.')
    # A send outside a turn that Coconut refuses must not come back as a plain text copy.
    from gateway.platforms.base import SendResult
    if not callable(getattr(BasePlatformAdapter, '_send_retry_is_final', None)) or (
            'raw_response' not in SendResult.__dataclass_fields__):
        raise ValueError('Hermes changed the retry of a failed send. Review the coconut adapter.')


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


def verify_parallel_work_contract():
    import os
    from unittest.mock import patch
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.delegate_tool_config import _get_max_concurrent_children
    from .parallel_work import save
    with tempfile.TemporaryDirectory(prefix='coconut-workers-contract-') as temporary:
        home = Path(temporary) / 'bot'
        home.mkdir()
        token = set_hermes_home_override(str(home))
        try:
            with patch.dict(os.environ, {'HERMES_HOME': str(home)}):
                save(home, 3)
                if _get_max_concurrent_children() != 3:
                    raise ValueError('Hermes did not use the bot worker limit.')
                save(home, 8)
                if _get_max_concurrent_children() != 8:
                    raise ValueError('Hermes did not apply the changed worker limit.')
        finally:
            reset_hermes_home_override(token)
