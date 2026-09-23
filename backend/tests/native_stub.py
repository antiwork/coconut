"""Minimal native APIs for process tests. Real APIs run in the mini contract checks."""
from pathlib import Path


def install_native_stub(source):
    source = Path(source)
    (source / 'hermes_state.py').write_text('''
class SessionDB:
    def __init__(self): self.meta, self.sessions = {}, {}
    def get_meta(self, key): return self.meta.get(key)
    def set_meta(self, key, value): self.meta[key] = value
    def create_session(self, session_id, source, **kwargs): self.sessions.setdefault(session_id, [])
    def replace_messages(self, session_id, messages, archive_dropped=False, reject_active_turn_lease=False):
        self.sessions[session_id] = messages
    def resolve_resume_session_id(self, session_id): return session_id
    def get_session(self, session_id): return {'id': session_id} if session_id in self.sessions else None
    def resolve_session_by_title(self, title): return None
    def set_session_title(self, session_id, title): pass
    def end_session(self, session_id, reason): pass
    def get_resume_conversations(self, session_id): return self.sessions.get(session_id, []), []
    def close(self): pass
''')
    (source / 'hermes_cli').mkdir(exist_ok=True)
    (source / 'hermes_cli/__init__.py').write_text('')
    (source / 'dotenv.py').write_text('def dotenv_values(path): return {}\n')
    (source / 'hermes_cli/config.py').write_text('''import json
from pathlib import Path
def require_readable_config_before_write(path):
    path=Path(path)
    return json.loads(path.read_text()) if path.exists() else {}
''')
    (source / 'tools/approval_context.py').write_text('''def _get_approval_config(): return {}
_VALID_MODES = ('manual', 'smart', 'off')
def _normalize_approval_mode(value): return 'off' if value is False else value
''')
    (source / 'tools/skills_sync.py').write_text('''from pathlib import Path
import os
def sync_skills(quiet=False):
    (Path(os.environ['HERMES_HOME']) / 'skill-sync-called').write_text(str(quiet))
    return {}
''')
    (source / 'tools/process_registry.py').write_text('''import os, signal, sys
class Registry:
    def recover_from_checkpoint(self): pass
    def list_sessions(self):
        return getattr(sys.modules.get('run_agent'), 'background', [])
    def kill_process(self, session_id, **kwargs):
        process = next(row for row in self.list_sessions() if row['session_id'] == session_id)
        try: os.killpg(process['pid'], signal.SIGTERM)
        except ProcessLookupError: pass
        try: os.waitpid(process['pid'], 0)
        except ChildProcessError: pass
        process['status'] = 'exited'
        return {'status': 'killed'}
process_registry = Registry()
''')
    (source / 'tools/approval.py').write_text('''def register_gateway_notify(*args): pass
def unregister_gateway_notify(*args): pass
def resolve_gateway_approval(*args, **kwargs): return 1
''')
    (source / 'hermes_cli/middleware.py').write_text("TOOL_EXECUTION_MIDDLEWARE='tool_execution'\n")
    (source / 'hermes_cli/plugins.py').write_text('''class PluginManifest:
    def __init__(self, **kwargs): pass
class PluginContext:
    def __init__(self, *args): pass
    def register_hook(self, name, callback):
        hooks.setdefault(name, []).append(callback)
        class Registration:
            def dispose(self): hooks[name].remove(callback)
        return Registration()
    def register_middleware(self, kind, callback):
        from tools.registry import registry
        original=registry.dispatch
        def dispatch(name, args, **kwargs):
            def execute(args):
                from native_turn_scope import current
                scope = current.get()
                started = getattr(scope.agent, 'tool_start_callback', None) if scope else None
                if started:
                    started('call', name, args)
                return original(name, args, **kwargs)
            return callback(name, args, execute)
        registry.dispatch=dispatch
        class Registration:
            def dispose(self): registry.dispatch=original
        return Registration()
hooks = {}
def get_plugin_manager(): return None
''')
    (source / 'hermes_cli/tools_config.py').write_text("def _get_platform_tools(config, platform): return config.get('platform_toolsets', {}).get(platform, ['hermes-cli'])\n")
    (source / 'agent').mkdir(exist_ok=True)
    (source / 'agent/__init__.py').write_text('')
    (source / 'agent/delegation_context.py').write_text('from contextlib import nullcontext\nnon_dispatcher_owned_context = nullcontext\n')
    (source / 'agent/interrupt_scope.py').write_text("""from contextlib import nullcontext
class InterruptScope:
    def request(self, reason=None): pass
def bind_interrupt_scope(scope): return nullcontext()
""")
    (source / 'agent/skill_utils.py').write_text('def parse_config_string_list(value): return value or []\n')
    (source / 'cron').mkdir(exist_ok=True)
    (source / 'cron/__init__.py').write_text('')
    (source / 'cron/scheduler.py').write_text('''def _check_model_drift(*args): pass
def _is_cron_silence_response(text): return text.strip() == '[SILENT]'
''')
    (source / 'cron/scheduler_prompt.py').write_text('''def _guard_job_credential_exfil(job): pass
def _build_job_prompt(job): return job.get('prompt', '')
''')
    (source / 'gateway').mkdir(exist_ok=True)
    (source / 'gateway/__init__.py').write_text('')
    (source / 'gateway/session_context.py').write_text('''def set_session_vars(**kwargs): return kwargs
def clear_session_vars(tokens): pass
''')
    (source / 'tools/terminal_tool.py').write_text('''def record_session_cwd(task_id, cwd): pass
def clear_session_cwd(task_id): pass
''')

    (source / 'yaml.py').write_text('import json\nsafe_load = json.loads\n')
    (source / 'utils.py').write_text('import json\nfrom pathlib import Path\ndef atomic_yaml_write(path, data, **kwargs): Path(path).write_text(json.dumps(data))\ndef atomic_roundtrip_yaml_save(path, data): atomic_yaml_write(path, data)\n')
    (source / 'tools/bot_mode_probe.py').write_text("BOT_CHAT_TITLE='Bot Chat'\n")
    (source / 'tools/bot_mode_dm.py').write_text("""def message_agent_tool_schema():
    return {'function': {'name': 'message_agent', 'description': 'Message a bot.',
        'parameters': {'type': 'object', 'properties': {'target': {'type': 'string'}, 'message': {'type': 'string'}}}}}
""")
    (source / 'tools/bot_relay.py').write_text('''import json
import os
import uuid
from pathlib import Path
def write_remote_roster(root, rows):
    directory = Path(root)/'bot_relay'
    directory.mkdir(exist_ok=True)
    (directory/'roster.json').write_text(json.dumps(rows))
def message_agent(arguments):
    home=Path(os.environ['HERMES_HOME'])
    envelope=dict(id=uuid.uuid4().hex, target_connection=arguments['target'], message=arguments['message'])
    directory=home/'bot_relay/outbox'
    directory.mkdir(exist_ok=True)
    (directory/(envelope['id']+'.json')).write_text(json.dumps(envelope))
    return json.dumps({'status':'sent'})
def claim_pending_envelopes(root):
    directory=Path(root)/'bot_relay/claimed'
    directory.mkdir(exist_ok=True)
    result=[]
    for path in (Path(root)/'bot_relay/outbox').glob('*.json'):
        target=directory/path.name
        path.rename(target)
        result.append(json.loads(target.read_text()))
    return result
''')
